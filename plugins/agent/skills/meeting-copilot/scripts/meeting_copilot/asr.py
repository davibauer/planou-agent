"""Speech to text (faster-whisper on the GPU) and who said it (the user's channel, or the voice catalog).

Who: in live capture the mic channel is the user and the loopback channel is everyone else, no guessing. A replay has
one mixed track, so each segment is compared with the user's voiceprint from the meeting-minutes catalog (the same
wespeaker model the diarization uses; >= 0.5 cosine = same person). The other voices get a catalog name when they
match one, or 'Speaker N' from an online clustering.
"""
import os, re, time
import numpy as np

SR = 16000
JUNK = {'you', 'you you', 'thank you', 'thanks for watching', 'bye', 'so', 'okay', 'uh', 'um', 'hmm'}


class Transcriber:
    def __init__(self, model='large-v3', compute='int8_float16', language='en', prompt=''):
        from faster_whisper import WhisperModel
        self.m = WhisperModel(model, device='cuda', compute_type=compute)
        self.language = language or None
        self.prompt = prompt or None

    def __call__(self, audio):
        t = time.time()
        segs, info = self.m.transcribe(audio, language=self.language, beam_size=1, vad_filter=False,
                                       condition_on_previous_text=False, without_timestamps=False,
                                       initial_prompt=self.prompt, temperature=0.0)
        segs = list(segs)
        text = ' '.join(s.text.strip() for s in segs).strip()
        nsp = max((s.no_speech_prob for s in segs), default=1.0)
        lp = min((s.avg_logprob for s in segs), default=-9.0)
        parts = [(max(0.0, s.start), s.end, s.text.strip()) for s in segs if s.text.strip()]
        return {'text': text, 'parts': parts, 'ms': int((time.time() - t) * 1000), 'no_speech': nsp, 'logprob': lp,
                'lang': info.language}


def is_junk(text, dur, no_speech=0.0, logprob=0.0):
    """Whisper's usual ghosts on silence and noise ('you', 'Thank you.'), and prompt echoes."""
    norm = re.sub(r'[^a-z ]', '', text.lower()).strip()
    if not norm: return True
    if norm in JUNK and dur < 2.5: return True
    if no_speech > 0.6 and logprob < -0.8: return True
    words = norm.split()
    if len(words) >= 4 and len(set(words)) == 1: return True
    return False


class Voices:
    MODEL = 'pyannote/wespeaker-voxceleb-resnet34-LM'
    USER_T, USER_KEEP_T, NAMED_T, CLUSTER_T = 0.45, 0.35, 0.60, 0.45
    MIN_S = 1.2

    def __init__(self, voices_dir, user_name, hf_token_path, use_embeddings=True):
        self.user = user_name
        self.catalog, self.clusters, self.last = {}, [], {}
        self.inf = None
        if not use_embeddings: return
        import glob, torch
        from pyannote.audio import Model, Inference
        for f in glob.glob(os.path.join(voices_dir, '*.npy')):
            self.catalog[os.path.splitext(os.path.basename(f))[0]] = np.load(f)
        tok = open(os.path.expanduser(hf_token_path)).read().strip()
        m = Model.from_pretrained(self.MODEL, use_auth_token=tok)
        self.inf = Inference(m, window='whole'); self.inf.to(torch.device('cuda' if torch.cuda.is_available() else 'cpu'))
        self.torch = torch

    def embed(self, audio):
        e = np.asarray(self.inf({'waveform': self.torch.from_numpy(audio[None, :]), 'sample_rate': SR})).reshape(-1)
        n = np.linalg.norm(e)
        return e / n if n > 1e-9 else None

    def _other(self, e):
        best, bc = None, -1.0
        for name, cen in self.catalog.items():
            if name == self.user: continue
            c = float(e @ cen)
            if c > bc: best, bc = name, c
        if best and bc >= self.NAMED_T: return best, bc
        for cl in self.clusters:
            c = float(e @ (cl['sum'] / np.linalg.norm(cl['sum'])))
            if c >= self.CLUSTER_T:
                cl['sum'] = cl['sum'] + e; return cl['name'], c
        name = f'Speaker {len(self.clusters) + 2}'   # the user is speaker 1
        self.clusters.append({'name': name, 'sum': e.copy()})
        return name, 0.0

    def who(self, seg):
        """-> (label, is_user, cosine with the user or None)."""
        ch, audio = seg['ch'], seg['audio']
        if ch == 'mic': return 'You', True, None
        dur = audio.size / SR
        if self.inf is None or dur < self.MIN_S:
            if ch == 'loopback' and self.inf is None: return 'Them', False, None
            prev = self.last.get(ch)
            if prev and seg['start'] - prev[3] < 1.5: return prev[0], prev[1], None
            return ('Them', False, None) if ch == 'loopback' else ('?', False, None)
        e = self.embed(audio)
        if e is None: return '?', False, None
        cu = float(e @ self.catalog[self.user]) if (ch == 'mix' and self.user in self.catalog) else -1.0
        prev = self.last.get(ch)
        recent = prev and seg['start'] - prev[3] < 2.0
        if cu >= self.USER_T or (recent and prev[1] and cu >= self.USER_KEEP_T): label, me = 'You', True
        else: (label, _), me = self._other(e), False
        self.last[ch] = (label, me, cu, seg['end'])
        return label, me, round(cu, 3)
