#!/usr/bin/env python3
"""Live minutes: transcribe an OBS recording WHILE it is being recorded, one track per side, no diarization.

OBS (since 17/10/2026) records 3 audio tracks: 1 = mix, 2 = only the user's microphone, 3 = only the desktop audio
(everyone else). With "Hybrid MP4" the file is a fragmented MP4 while recording (ftyp + moov with mvex at the start,
then moof/mdat fragments), so ffprobe/ffmpeg read the part already written; on stop OBS turns it into a regular MP4.
The track of each side says who speaks: no diarization, no echo cancelling, no voice guessing.

  live.py tick [<video>...]      one live step (cron, the work-watch recordings tick or watch.sh call it): for each
                                 recording still growing (or <video>), transcribe the next 5 to 10 min of tracks 2
                                 and 3 in light mode. Skips the step (the meeting is then done afterwards) when the GPU
                                 is busy, on battery or when the file is not readable yet.
  live.py mode <video>           prints "tracks" when run.sh must use the tracks (live on and >= 3 audio tracks), else "mix"
  live.py finish <video> <json>  run.sh, under its GPU lock: transcribes only what the live steps did not, merges
                                 everything into a whisperx-like transcript.json at <json> and drops the live state.
                                 Without live steps (GPU busy or battery the whole meeting) it transcribes the whole
                                 tracks: that is the after-the-meeting fallback.
  live.py status                 the live recordings and how far each track went

Config (~/.config/meeting-minutes/config.json, key "live"; missing = off):
  {"enabled": true, "chunk_s": 300, "max_chunk_s": 600, "margin_s": 30, "min_free_mb": 4000, "max_gpu_util": 80,
   "tracks_from": "2026-10-17",
   "compute_type": "int8", "batch_size": 4, "user_track": "0:a:1", "others_track": "0:a:2", "others_label": "OUTROS"}
State (Linux disk, never inside ata_<stem>/, which other tools read as "transcribing"/"done"):
  ~/.config/meeting-minutes/state/live/<stem>.json   offsets, language and segments per track; live.log
The WAVs of each step live in a temp folder and are deleted right after the step.
"""
import fcntl, glob, json, os, re, shutil, subprocess, sys, tempfile, time

import mm_config

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_DIR = os.path.join(mm_config.STATE, 'live')
LOG = os.path.join(LIVE_DIR, 'live.log')
GPU_LOCK = os.environ.get('ATA_GPU_LOCK', '/tmp/ata-reuniao-whisperx.lock')   # the same lock as run.sh
WHISPERX = os.environ.get('ATA_WHISPERX') or shutil.which('whisperx') or os.path.expanduser('~/.local/bin/whisperx')
NVIDIA_SMI = os.environ.get('ATA_NVIDIA_SMI') or 'nvidia-smi'
POWER_DIR = os.environ.get('ATA_POWER_DIR') or '/sys/class/power_supply'
RECENT_S = 180          # tick without <video>: a recording is "live" while its mtime is younger than this
TAIL_S = 3.0            # a segment ending this close to the end of a step may be cut: it waits for the next step
STALE_S = 3 * 86400     # live state of a recording that never reached run.sh
DEFAULTS = {'enabled': False, 'chunk_s': 300, 'max_chunk_s': 600, 'margin_s': 30, 'min_free_mb': 4000, 'max_gpu_util': 80,
            'tracks_from': '2026-10-17',
            'compute_type': 'int8', 'batch_size': 4, 'user_track': '0:a:1', 'others_track': '0:a:2',
            'others_label': 'OUTROS'}


def cfg():
    c = mm_config.load()
    live = c.get('live') if isinstance(c.get('live'), dict) else {'enabled': c.get('live') is True}
    out = dict(DEFAULTS, **{k: v for k, v in live.items() if v is not None})
    out['user_label'] = (c.get('user_name') or '').strip() or 'USUARIO'
    out['model'] = c.get('whisper_model') or 'large-v2'
    out['language'] = (os.environ.get('ATA_LANG') or c.get('language') or '').strip()
    out['hf_token_path'] = os.path.expanduser(c.get('hf_token_path') or '~/.cache/huggingface/token')
    return out


def log(msg):
    os.makedirs(LIVE_DIR, exist_ok=True)
    line = f'[{time.strftime("%F %T")}] {msg}'
    with open(LOG, 'a', encoding='utf-8') as f: f.write(line + '\n')
    print(line)


def stem_of(video): return os.path.splitext(os.path.basename(video))[0]


def layout_ok(video, c):
    """The track layout (2 = mic, 3 = desktop) exists only from tracks_from on (OBS changed on 17/10/2026; older
    recordings have 5 tracks with other meanings). The date comes from the OBS name; a name without it = no."""
    d = stem_of(video)[:10]
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', d): return False
    return d >= (c.get('tracks_from') or '0000-00-00')
def state_path(video): return os.path.join(LIVE_DIR, stem_of(video) + '.json')


def load_state(video):
    try:
        with open(state_path(video), encoding='utf-8') as f: return json.load(f)
    except (OSError, ValueError): return None


def save_state(video, st):
    os.makedirs(LIVE_DIR, exist_ok=True)
    p = state_path(video)
    fd, tmp = tempfile.mkstemp(dir=LIVE_DIR, prefix='.state-', suffix='.tmp')   # unique name: two writers never collide
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f: json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, p)
    except BaseException:
        try: os.remove(tmp)
        except OSError: pass
        raise


def drop_state(video):
    try: os.remove(state_path(video))
    except OSError: pass


# ---- probes ----

def probe(video):
    """(readable duration in s or None, number of audio tracks). A Hybrid MP4 being recorded answers with the duration
    of the fragments already written; a regular MP4 without moov (old OBS setting) answers None."""
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_type', '-of', 'json', video],
                           capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout or '{}')
    except (OSError, ValueError, subprocess.TimeoutExpired): return None, 0
    n = sum(1 for s in d.get('streams', []) if s.get('codec_type') == 'audio')
    try: dur = float(d.get('format', {}).get('duration'))
    except (TypeError, ValueError): dur = None
    return dur, n


def on_battery():
    """True only when the machine says so: an AC adapter offline and a battery present (WSL shows AC1/BAT1).
    No power_supply info (desktop, container) = mains."""
    acs = glob.glob(os.path.join(POWER_DIR, 'AC*')) + glob.glob(os.path.join(POWER_DIR, 'ADP*'))
    bats = glob.glob(os.path.join(POWER_DIR, 'BAT*'))
    if not acs or not bats: return False
    def online(p):
        try: return open(os.path.join(p, 'online')).read().strip() == '1'
        except OSError: return True
    return not any(online(p) for p in acs)


def gpu_probe():
    """(free VRAM MiB, utilization %) of the busiest GPU, None without nvidia-smi. The copilot or a browser may hold
    the GPU without the lock, so the lock alone is not enough. One 6 min step of both tracks took ~3.4 GB (20/10)."""
    try:
        r = subprocess.run([NVIDIA_SMI, '--query-gpu=memory.free,utilization.gpu', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=20)
        rows = [[int(x) for x in ln.replace(' ', '').split(',')] for ln in r.stdout.splitlines() if ln.strip()]
        if r.returncode or not rows: return None
        return min(x[0] for x in rows), max(x[1] if len(x) > 1 else 0 for x in rows)
    except (OSError, ValueError, subprocess.TimeoutExpired): return None


def why_not_now(c):
    """None when a live step may run now; else the reason (the step is skipped, the meeting is done afterwards)."""
    if on_battery(): return 'na bateria'
    g = gpu_probe()
    if g is None: return 'sem GPU (nvidia-smi não respondeu)'
    free, util = g
    if free < int(c['min_free_mb']): return f'GPU ocupada ({free} MiB livres, preciso de {c["min_free_mb"]})'
    if util > int(c['max_gpu_util']): return f'GPU ocupada ({util}% de uso)'
    return None


# ---- transcription ----

def extract(video, track, start, end, wav):
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', f'{start:.3f}', '-i', video, '-map', track]
    if end is not None: cmd += ['-t', f'{max(0.0, end - start):.3f}']
    cmd += ['-ac', '1', '-ar', '16000', wav]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)


def whisper(wavs, outdir, c, language, timeout):
    """One whisperx call for all the WAVs (one model load), light mode: int8, small batch, no diarization. Alignment
    stays on: prosody.py needs the word times. Returns {wav_stem: whisperx json}."""
    cmd = [WHISPERX, *wavs, '--model', c['model'], '--compute_type', str(c['compute_type']), '--batch_size', str(c['batch_size']),
           '--output_dir', outdir, '--output_format', 'json']
    if language: cmd += ['--language', language]
    env = dict(os.environ, PATH=os.path.expanduser('~/.local/bin') + os.pathsep + os.environ.get('PATH', ''))
    if 'HF_TOKEN' not in env and os.path.exists(c['hf_token_path']):
        env['HF_TOKEN'] = open(c['hf_token_path']).read().strip()
    try: r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired: raise RuntimeError(f'whisperx passou de {timeout}s')   # never holds the GPU lock forever
    out = {}
    for w in wavs:
        k = os.path.splitext(os.path.basename(w))[0]
        try:
            with open(os.path.join(outdir, k + '.json'), encoding='utf-8') as f: out[k] = json.load(f)
        except (OSError, ValueError): pass
    if len(out) < len(wavs):
        raise RuntimeError(f'whisperx não gerou saída (rc={r.returncode}): {(r.stderr or "")[-300:]}')
    return out


def shift(seg, off, label):
    s = dict(seg, start=round(seg['start'] + off, 3), end=round(seg['end'] + off, 3), speaker=label)
    if s.get('words'):
        s['words'] = [dict(w, start=round(w['start'] + off, 3), end=round(w['end'] + off, 3), speaker=label) if 'start' in w and 'end' in w
                      else dict(w, speaker=label) for w in s['words']]
    return s


def keep(segs, start, end, final):
    """Segments of one step, already shifted. A segment that ends near the end of the step may be cut in half: it is
    left for the next step, which starts at the end of the last kept one. Returns (kept, next offset)."""
    segs = [s for s in segs if (s.get('text') or '').strip()]
    if final: return segs, end
    cut = end - TAIL_S
    kept = [s for s in segs if s['end'] <= cut]
    nxt = kept[-1]['end'] if kept else cut
    return kept, round(max(nxt, start + 1.0), 3)


def step(video, st, c, upto, final=False):
    """Transcribes each track from its offset to `upto` (s). Updates st in place."""
    tracks = [(k, c[k + '_track'], c[k + '_label']) for k in ('user', 'others')]
    todo = [(k, tr, lab) for k, tr, lab in tracks if upto - st['tracks'][k]['offset'] > 1.0]
    if not todo: return
    tmp = tempfile.mkdtemp(prefix='mm-live-')
    try:
        wavs = []
        for k, tr, _ in todo:
            w = os.path.join(tmp, k + '.wav'); extract(video, tr, st['tracks'][k]['offset'], upto, w); wavs.append(w)
        longest = max(upto - st['tracks'][k]['offset'] for k, _, _ in todo)
        res = whisper(wavs, os.path.join(tmp, 'out'), c, st.get('language') or c['language'], timeout=int(600 + 2 * longest))
        for k, _, lab in todo:
            t = st['tracks'][k]; off = t['offset']
            d = res[k]
            end = upto
            segs = [shift(s, off, lab) for s in d.get('segments', [])]
            kept, nxt = keep(segs, off, end, final)
            t['segments'] += kept; t['offset'] = nxt
        if not st.get('language'):          # pin the language: per-step detection flips on the mostly silent mic track
            best = max(todo, key=lambda x: sum(len((s.get('text') or '').split()) for s in res[x[0]].get('segments', [])))
            if res[best[0]].get('segments'): st['language'] = res[best[0]].get('language') or ''
    finally:
        shutil.rmtree(tmp, ignore_errors=True)   # the intermediate WAVs never stay


class GpuLock:
    """flock(2) on run.sh's lock; non-blocking for the live tick (busy = skip)."""
    def __init__(self, blocking=False): self.blocking = blocking; self.f = None
    def __enter__(self):
        self.f = open(GPU_LOCK, 'a')
        try: fcntl.flock(self.f, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
        except BlockingIOError: self.f.close(); self.f = None
        return self.f is not None
    def __exit__(self, *a):
        if self.f: fcntl.flock(self.f, fcntl.LOCK_UN); self.f.close()


def new_state(video, c):
    return {'video': os.path.abspath(video), 'language': c['language'], 'steps': 0,
            'tracks': {'user': {'offset': 0.0, 'segments': []}, 'others': {'offset': 0.0, 'segments': []}}}


def tick_one(video, c):
    """One live step for one recording. Returns a short status (for the log and the tests)."""
    stem = stem_of(video)
    if os.path.isdir(os.path.join(os.path.dirname(video), f'ata_{stem}')): return 'skip: ata_ já existe (run.sh cuida)'
    if not layout_ok(video, c): return f'skip: gravação de antes de {c["tracks_from"]} (outras faixas)'
    dur, n = probe(video)
    if n < 3: return f'skip: {n} faixa(s) de áudio (preciso de 3: mix, microfone, desktop)'
    if dur is None: return 'skip: ainda ilegível (sem índice: confira "Hybrid MP4" no OBS); fica para depois da reunião'
    st = load_state(video) or new_state(video, c)
    upto = dur - float(c['margin_s'])          # never decode the fragment OBS is still writing
    behind = upto - min(t['offset'] for t in st['tracks'].values())
    if behind < float(c['chunk_s']): return f'wait: {max(0, int(behind))}s novos de {c["chunk_s"]}s'
    why = why_not_now(c)
    if why: return f'skip: {why}; fica para depois da reunião'
    upto = min(upto, min(t['offset'] for t in st['tracks'].values()) + float(c['max_chunk_s']))
    with GpuLock() as got:
        if not got: return 'skip: GPU ocupada por outra transcrição'
        t0 = time.time()
        step(video, st, c, upto)
        st['steps'] += 1; st['updated'] = time.time()
        save_state(video, st)      # still under the lock: run.sh never reads the old state after the GPU is free
    return f'ok: até {int(upto)}s em {int(time.time() - t0)}s (idioma {st.get("language") or "?"})'


def live_videos():
    vdir = mm_config.videos_dir(); now = time.time(); out = []
    for v in sorted(glob.glob(os.path.join(vdir, '*.mp4'))):
        try:
            if now - os.path.getmtime(v) < RECENT_S: out.append(v)
        except OSError: pass
    return out


def prune():
    """Live state of a recording that is gone or never reached run.sh."""
    for p in glob.glob(os.path.join(LIVE_DIR, '*.json')):
        try:
            st = json.load(open(p, encoding='utf-8'))
            if not os.path.exists(st.get('video', '')) or time.time() - st.get('updated', 0) > STALE_S: os.remove(p)
        except (OSError, ValueError):
            try: os.remove(p)
            except OSError: pass


def cmd_tick(args):
    c = cfg()
    if not c['enabled']: return 0
    os.makedirs(LIVE_DIR, exist_ok=True)
    me = open(os.path.join(LIVE_DIR, 'tick.lock'), 'a')       # one live tick at a time (cron and runner may overlap)
    try: fcntl.flock(me, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: return 0
    prune()
    for v in (args or live_videos()):
        try: msg = tick_one(v, c)
        except Exception as e: msg = f'erro: {type(e).__name__}: {e}'
        if not msg.startswith('wait'): log(f'{stem_of(v)}: {msg}')
    return 0


def tracks_mode(video, c=None):
    c = c or cfg()
    if not c['enabled'] or not layout_ok(video, c): return False
    return probe(video)[1] >= 3


def cmd_finish(video, out_json):
    """Called by run.sh (which already holds the GPU lock): the rest of each track, then the merged transcript."""
    c = cfg()
    st = load_state(video) or new_state(video, c)
    done = sum(len(t['segments']) for t in st['tracks'].values())
    print(f'      faixas: {st["steps"]} trecho(s) já transcritos ao vivo ({done} falas); agora só o resto')
    dur, n = probe(video)
    if dur is None or n < 3: raise SystemExit(f'live: vídeo sem duração ou com {n} faixa(s); volto para a diarização')
    step(video, st, c, dur, final=True)
    segs = sorted(st['tracks']['user']['segments'] + st['tracks']['others']['segments'], key=lambda s: (s['start'], s['end']))
    os.makedirs(os.path.dirname(os.path.abspath(out_json)), exist_ok=True)
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump({'language': st.get('language') or c['language'] or '', 'segments': segs,
                   'source': {'mode': 'tracks', 'user': c['user_label'], 'others': c['others_label'], 'live_steps': st['steps']}},
                  f, ensure_ascii=False)
    drop_state(video)
    return 0


def cmd_status():
    c = cfg()
    print(f'modo ao vivo: {"LIGADO" if c["enabled"] else "desligado"} (config.json "live")')
    for p in sorted(glob.glob(os.path.join(LIVE_DIR, '*.json'))):
        try: st = json.load(open(p, encoding='utf-8'))
        except (OSError, ValueError): continue
        print(f'  {os.path.basename(p)[:-5]}: {st["steps"]} trecho(s), ' + ', '.join(
            f'{k} até {int(t["offset"])}s ({len(t["segments"])} falas)' for k, t in st['tracks'].items()))
    return 0


def main(a):
    if not a: sys.exit(__doc__)
    if a[0] == 'tick': return cmd_tick(a[1:])
    if a[0] == 'mode' and len(a) == 2: print('tracks' if tracks_mode(a[1]) else 'mix'); return 0
    if a[0] == 'finish' and len(a) == 3: return cmd_finish(a[1], a[2])
    if a[0] == 'status': return cmd_status()
    sys.exit(__doc__)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
