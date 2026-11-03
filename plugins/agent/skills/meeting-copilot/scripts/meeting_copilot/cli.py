"""meeting-copilot: live suggestions during a meeting (what to answer, what to watch out for, what to ask) and running notes.

  meeting-copilot start  --instance <agent instance> [--title T] [--no-open]       live: Windows mic + system audio
  meeting-copilot replay <recording> --instance <i> [--start mm:ss] [--end mm:ss] [--speed N] [--no-open]
  meeting-copilot prepare --instance <i> [--blocks JSON] [--log FILE] [--prewarm-now]
                                         the ready context pack (incremental) and the pre-warm before a busy block;
                                         run by the agent's tick (hook meeting_copilot), prints nothing
  meeting-copilot eval [--meetings GLOB] [--n S] [--sweep LIST] [--sample K | --review [FILE] [--model [M]]]
                       [--gate v1|v2] [--topics MODE] [--gate-topics] [--no-suppress] [--split S] [--json]
                                         measures the gate "is this line for the user?" on the recorded meetings
                                         (offline; the model only with --model, cached; cache in
                                         ~/.cache/meeting-copilot/eval)
  meeting-copilot setup-windows          creates the Windows capture venv (pyaudiowpatch) under windows_tools_dir
  meeting-copilot devices                lists the Windows audio devices the capture can use

The panel is a local page (http://localhost:<port>), opened as a narrow app window in the Windows browser.
The GPU lock of meeting-minutes is held for the whole session: with it taken, start waits only with --wait-gpu.
"""
import argparse, asyncio, fcntl, json, os, shutil, signal, subprocess, sys, time

from . import config

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_DIR = os.path.join(os.path.dirname(HERE), 'win')


def parse_t(s):
    if s is None: return None
    parts = [float(p) for p in str(s).split(':')]
    t = 0.0
    for p in parts: t = t * 60 + p
    return t


def gpu_lock(wait):
    fd = open(config.GPU_LOCK, 'w')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if not wait:
            sys.exit(f'GPU busy: another transcription holds {config.GPU_LOCK}. Try again later or pass --wait-gpu.')
        print('GPU busy, waiting for the other transcription to finish...', flush=True)
        fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def win_env(var):
    try:
        return subprocess.run(['cmd.exe', '/c', f'echo %{var}%'], capture_output=True, text=True, timeout=15, cwd='/').stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ''


def win_tools_dir():
    """windows_tools_dir with %VARS% expanded -> (windows path, wsl path)."""
    import re
    w = config.load()['windows_tools_dir']
    w = re.sub(r'%(\w+)%', lambda m: win_env(m.group(1)), w)
    l = subprocess.run(['wslpath', '-u', w], capture_output=True, text=True).stdout.strip()
    return w, l


def win_python():
    w, l = win_tools_dir()
    return w + '\\venv\\Scripts\\python.exe', os.path.join(l, 'venv', 'Scripts', 'python.exe'), w, l


def setup_windows(_args):
    w, l = win_tools_dir()
    os.makedirs(l, exist_ok=True)
    shutil.copy(os.path.join(WIN_DIR, 'capture.py'), os.path.join(l, 'capture.py'))
    py_w, py_l, _, _ = win_python()
    if not os.path.exists(py_l):
        base = shutil.which('python.exe') or shutil.which('py.exe')
        if not base: sys.exit('No python.exe on Windows. Install Python 3 for Windows (python.org or the Store) and run again.')
        print(f'Creating the venv in {w}\\venv ...', flush=True)
        subprocess.run([base, '-m', 'venv', w + '\\venv'], check=True, cwd='/')
    subprocess.run([py_l, '-m', 'pip', 'install', '-q', '--disable-pip-version-check', 'pyaudiowpatch'], check=True, cwd='/')
    print('Windows capture ready:', w)


def devices(_args):
    py_w, py_l, w, l = win_python()
    if not os.path.exists(py_l): sys.exit('Run `meeting-copilot setup-windows` first.')
    shutil.copy(os.path.join(WIN_DIR, 'capture.py'), os.path.join(l, 'capture.py'))
    subprocess.run([py_l, w + '\\capture.py', '--list'], cwd='/')


def open_panel(port, args):
    if args.no_open: return
    url = f'http://localhost:{port}/'
    chrome = None
    for var in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        base = win_env(var)
        if not base: continue
        p = subprocess.run(['wslpath', '-u', base + '\\Google\\Chrome\\Application\\chrome.exe'], capture_output=True, text=True).stdout.strip()
        if p and os.path.exists(p): chrome = p; break
    if chrome:
        subprocess.Popen([chrome, f'--app={url}', '--window-size=460,940'], cwd='/', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif shutil.which('wslview'):
        subprocess.Popen(['wslview', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print('Panel:', url, flush=True)


async def serve(sess, start_source, port, args):
    from aiohttp import web
    from .app import make_app
    runner = web.AppRunner(make_app(sess)); await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', port).start()
    print(f'Session dir: {sess.dir}', flush=True)
    open_panel(port, args)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: sess.source and sess.source.stopped.set())
    await sess.run(start_source)
    m = sess.metric_summary()
    print('Ended. ' + ', '.join(f'{k}={v}' for k, v in m.items()), flush=True)
    print(f'Wrote {sess.dir}/transcript.md and notes.md', flush=True)
    if args.exit_when_done:
        await runner.cleanup(); return
    print('Panel stays up; Ctrl+C to quit.', flush=True)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop.set)
    await stop.wait(); await runner.cleanup()


def run_session(args, kind):
    from .app import Session
    from . import audio
    cfg = config.load()
    port = args.port or cfg['port']
    lock = gpu_lock(args.wait_gpu)
    sess = Session(args, kind)
    cap = None
    if kind == 'replay':
        def start_source(sink):
            s = audio.ReplaySource(args.file, sink, start=parse_t(args.start) or 0.0, end=parse_t(args.end),
                                   speed=args.speed, stream=args.stream)
            s.start(); return s
    else:
        cport = cfg['capture_port']
        def start_source(sink):
            nonlocal cap
            s = audio.TcpSource(cport, sink); s.start()
            if not args.no_capture_launch:
                py_w, py_l, w, l = win_python()
                if not os.path.exists(py_l): sys.exit('Run `meeting-copilot setup-windows` first.')
                shutil.copy(os.path.join(WIN_DIR, 'capture.py'), os.path.join(l, 'capture.py'))
                cmd = [py_l, w + '\\capture.py', '--port', str(cport)]
                if cfg['mic_device']: cmd += ['--mic', cfg['mic_device']]
                if cfg['loopback_device']: cmd += ['--loopback', cfg['loopback_device']]
                ff = shutil.which('ffmpeg.exe')     # the microphone's fallback when WASAPI refuses it (see win/capture.py)
                if ff: cmd += ['--ffmpeg', subprocess.run(['wslpath', '-w', ff], capture_output=True, text=True).stdout.strip()]
                if cfg.get('mic_backend'): cmd += ['--mic-backend', cfg['mic_backend']]
                cap = subprocess.Popen(cmd, cwd='/')
            return s
    try:
        asyncio.run(serve(sess, start_source, port, args))
    finally:
        if cap and cap.poll() is None: cap.terminate()
        lock.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog='meeting-copilot', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    def common(p):
        p.add_argument('--instance', default=config.load().get('default_instance') or os.environ.get('AGENT_INSTANCE'),
                       help='agent instance whose context is used (default: default_instance in the config)')
        p.add_argument('--title'); p.add_argument('--port', type=int); p.add_argument('--language')
        p.add_argument('--no-open', action='store_true', help='do not open the panel window')
        p.add_argument('--offline', action='store_true', help='context pack without GitHub/Jira')
        p.add_argument('--wait-gpu', action='store_true'); p.add_argument('--session-dir')
        p.add_argument('--exit-when-done', action='store_true', help='quit when the source ends (tests)')
        p.add_argument('--no-voiceprint', action='store_true', help='live: do not cluster the other voices')
        p.add_argument('--no-prime', action='store_true', help=argparse.SUPPRESS)       # measurement: no cache-warming call at start

    s = sub.add_parser('start', help='live meeting'); common(s)
    s.add_argument('--no-capture-launch', action='store_true', help='the Windows capture is started by hand')
    r = sub.add_parser('replay', help='feed a recording through the pipeline'); common(r)
    r.add_argument('file'); r.add_argument('--start'); r.add_argument('--end'); r.add_argument('--speed', type=float, default=1.0)
    r.add_argument('--stream', default='0:a:0', help='ffmpeg audio stream (default 0:a:0)')
    r.add_argument('--ready-pack', action='store_true', help=argparse.SUPPRESS)     # measurement: the live ready pack, not "as of the recording"
    pp = sub.add_parser('prepare', help='refresh the ready context pack (the agent tick runs it)')
    pp.add_argument('--instance', default=config.load().get('default_instance') or os.environ.get('AGENT_INSTANCE'))
    pp.add_argument('--blocks', help='JSON list of the next busy blocks [{"id", "ini"}] (agenda)')
    pp.add_argument('--log', help='one line per run that changed something or failed')
    pp.add_argument('--offline', action='store_true'); pp.add_argument('--prewarm-now', action='store_true')
    ev = sub.add_parser('eval', help='measure the gate "is this line for the user?" on the recorded meetings')
    ev.add_argument('--meetings', help='glob of meeting-minutes folders (default: <videos_dir>/ata_*)')
    ev.add_argument('--n', type=float, default=3.0, help='reply window in seconds for the automatic label (default 3)')
    ev.add_argument('--sweep', default='1.5,3,5,8', help='comma-separated reply windows to compare ("" = only --n)')
    ev.add_argument('--json', action='store_true'); ev.add_argument('--refresh', action='store_true', help='re-read the folders')
    ev.add_argument('--sample', type=int, help='write a blind hand-review file with this many pos and neg turns')
    ev.add_argument('--seed', type=int, default=7)
    ev.add_argument('--review', nargs='?', const='', help='score a verdicts file (default: review-verdicts.jsonl in the cache)')
    ev.add_argument('--jobs', type=int, help='worker processes (default: all cores)')
    ev.add_argument('--verbose', action='store_true')
    ev.add_argument('--gate', choices=('v1', 'v2'), default='v2', help='v1 = the old question-shaped gate (for before/after)')
    ev.add_argument('--topics', choices=('minutes', 'context', 'none'), default='minutes',
                    help="topic map (for the model stage, and stage 1 with --gate-topics): the instance's minutes before each meeting (default), plus today's CONTEXT.md (leaks), or none")
    ev.add_argument('--gate-topics', action='store_true', help='v2: the topic map also in the deterministic stage (config gate_topics)')
    ev.add_argument('--no-suppress', action='store_true', help='v2: keep the decision when a third person takes the floor first')
    ev.add_argument('--threshold', type=float, help='v2 score threshold (default detect.THRESHOLD)')
    ev.add_argument('--split', choices=('all', 'tune', 'check'), default='all', help='half of the meetings by id parity')
    ev.add_argument('--model', nargs='?', const='default', help='with --review: also the addressee model on the reviewed turns '
                    '(cached; default model: addressee_model in the config)')
    ev.add_argument('--model-limit', type=int, default=120, help='at most this many model calls not yet in the cache')
    sub.add_parser('setup-windows'); sub.add_parser('devices')
    args = ap.parse_args(argv)
    if args.cmd == 'eval':
        from . import eval as ev_mod
        return ev_mod.main(args)
    if args.cmd == 'setup-windows': return setup_windows(args)
    if args.cmd == 'devices': return devices(args)
    if args.cmd == 'prepare':
        from . import ready
        try: blocks = json.loads(args.blocks) if args.blocks else []
        except ValueError: blocks = []
        return ready.prepare(args.instance, blocks, args.log, online=not args.offline, prewarm_now=args.prewarm_now)
    if not args.instance: ap.error('--instance is required (or set default_instance in ~/.config/meeting-copilot/config.json)')
    if not os.path.isdir(config.instance_dir(args.instance)): ap.error(f'no agent instance at {config.instance_dir(args.instance)}')
    run_session(args, 'replay' if args.cmd == 'replay' else 'live')


if __name__ == '__main__':
    main()
