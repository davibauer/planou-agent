"""Audio in, speech segments out.

Sources push (channel, float32 mono 16 kHz samples, stream time) to a sink:
  ReplaySource  a recording read by ffmpeg and paced at real time (or N times faster): the test harness.
  TcpSource     the Windows capture (win/capture.py) connects to localhost and streams two channels:
                'mic' (the user) and 'loopback' (everyone else, what the computer plays). Raw int16 PCM at the device
                rate; resampled here. Audio never leaves the machine.
Segmenter cuts each channel at the end of a phrase (Silero VAD, the one bundled with faster-whisper): a segment ends
after ENDPOINT_S of silence, or at a pause once it is longer than MAX_S, or hard at HARD_S.
"""
import json, socket, struct, subprocess, threading, time
import numpy as np

SR = 16000
ENDPOINT_S = 0.6     # silence that closes a phrase: the floor of the card latency
MAX_S = 7.0          # past this, cut at the last pause
HARD_S = 11.0        # past this, cut anyway
CHECK_S = 0.3


class Segmenter:
    def __init__(self, ch, emit, speed=1.0):
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        self._vad, self._opts = get_speech_timestamps, VadOptions(threshold=0.5, min_silence_duration_ms=400,
                                                                  speech_pad_ms=120, min_speech_duration_ms=200)
        self.ch, self.emit, self.speed = ch, emit, speed
        self.buf = np.zeros(0, np.float32); self.t0 = 0.0; self.pending = 0; self.level = 0.0
        self.speaking = False        # speech in the buffer not cut yet (someone is mid-sentence on this channel)

    def feed(self, x, t_end):
        """x: samples whose last one is at stream time t_end."""
        if self.buf.size == 0: self.t0 = t_end - x.size / SR
        self.buf = np.concatenate([self.buf, x]); self.pending += x.size
        self.level = float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0
        if self.pending >= CHECK_S * SR:
            self.pending = 0; self._check()

    def flush(self):
        ts = self._vad(self.buf, self._opts) if self.buf.size > SR // 2 else []
        if ts: self._cut(ts[0]['start'], ts[-1]['end'])
        self.buf = np.zeros(0, np.float32)

    def _cut(self, a, b):
        seg = self.buf[a:b]
        if seg.size >= 0.3 * SR:
            now = time.time()
            # when the last word was spoken: now minus the audio already received after it (at the source's pace)
            spoken = now - (self.buf.size - b) / SR / self.speed
            self.emit({'ch': self.ch, 'start': self.t0 + a / SR, 'end': self.t0 + b / SR, 'audio': seg.copy(),
                       'wall': now, 'audio_end_wall': spoken})
        self.buf = self.buf[b:]; self.t0 += b / SR

    def _check(self):
        n = self.buf.size
        ts = self._vad(self.buf, self._opts)
        self.speaking = bool(ts)
        if not ts:                                   # silence: keep only a short tail
            keep = int(0.5 * SR)
            if n > keep: self.buf = self.buf[-keep:]; self.t0 += (n - keep) / SR
            return
        if ts[0]['start'] > int(0.5 * SR):           # drop leading silence
            d = ts[0]['start'] - int(0.3 * SR)
            self.buf = self.buf[d:]; self.t0 += d / SR
            for t in ts: t['start'] -= d; t['end'] -= d
            n = self.buf.size
        first, last = ts[0]['start'], ts[-1]['end']
        if n - last >= ENDPOINT_S * SR:
            self._cut(first, last); self.speaking = False
        elif n - first > MAX_S * SR and len(ts) > 1:
            self._cut(first, ts[-2]['end'])
        elif n - first > HARD_S * SR:
            self._cut(first, n)


class ReplaySource(threading.Thread):
    """A recording through the same pipeline, paced at `speed` x real time. Channel 'mix' (who speaks: voiceprint)."""
    def __init__(self, path, sink, start=0.0, end=None, speed=1.0, stream='0:a:0'):
        super().__init__(daemon=True)
        self.path, self.sink, self.start_s, self.end_s, self.speed, self.stream = path, sink, start, end, speed, stream
        self.paused = threading.Event(); self.stopped = threading.Event(); self.done = threading.Event()
        self.pos = start

    def run(self):
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-ss', str(self.start_s), '-i', self.path]
        if self.end_s: cmd += ['-t', str(self.end_s - self.start_s)]
        cmd += ['-map', self.stream, '-ac', '1', '-ar', str(SR), '-f', 's16le', '-']
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        chunk = int(0.1 * SR) * 2
        wall0, played = time.time(), 0.0
        try:
            while not self.stopped.is_set():
                if self.paused.is_set():
                    t = time.time(); time.sleep(0.1); wall0 += time.time() - t; continue
                data = p.stdout.read(chunk)
                if not data: break
                x = np.frombuffer(data, np.int16).astype(np.float32) / 32768.0
                played += x.size / SR
                wait = wall0 + played / self.speed - time.time()
                if wait > 0: time.sleep(wait)
                self.pos = self.start_s + played
                self.sink('mix', x, self.pos)
        finally:
            p.kill(); self.done.set()


def _resample(x, rate):
    if rate == SR: return x
    try:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(int(rate), SR)
        return resample_poly(x, SR // g, int(rate) // g).astype(np.float32)
    except ImportError:
        n = int(round(x.size * SR / rate))
        return np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype(np.float32)


class TcpSource(threading.Thread):
    """Listens on localhost:<port> (WSL forwards Windows localhost here). Protocol from win/capture.py:
    one JSON line {"channels": {"0": {"name": "mic", "rate": 48000, "ch": 1}, ...}}, then frames
    <B channel><I nbytes><int16 interleaved PCM>."""
    def __init__(self, port, sink, host='127.0.0.1'):
        super().__init__(daemon=True)
        self.port, self.host, self.sink = port, host, sink
        self.paused = threading.Event(); self.stopped = threading.Event(); self.done = threading.Event()
        self.connected = threading.Event(); self.info = {}; self.pos = 0.0; self.error = ''
        self.t_start = None

    def run(self):
        srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port)); srv.listen(1); srv.settimeout(1.0)
        try:
            while not self.stopped.is_set():
                try: conn, _ = srv.accept()
                except socket.timeout: continue
                self._serve(conn)
        finally:
            srv.close(); self.done.set()

    def _serve(self, conn):
        conn.settimeout(None); f = conn.makefile('rb')   # blocking: a timeout mid-frame would desync the stream
        try:
            self.info = json.loads(f.readline().decode())['channels']; self.connected.set()
            pend = {k: [] for k in self.info}; pend_n = {k: 0 for k in self.info}
            if self.t_start is None: self.t_start = time.time()
            while not self.stopped.is_set():
                hdr = f.read(5)
                if len(hdr) < 5: break
                cid, n = struct.unpack('<BI', hdr)
                data = f.read(n)
                c = self.info.get(str(cid))
                if not c or self.paused.is_set(): continue
                x = np.frombuffer(data, np.int16).astype(np.float32) / 32768.0
                if c['ch'] > 1: x = x.reshape(-1, c['ch']).mean(axis=1)
                k = str(cid); pend[k].append(x); pend_n[k] += x.size
                if pend_n[k] >= c['rate'] * 0.2:            # resample in 200 ms blocks
                    y = _resample(np.concatenate(pend[k]), c['rate']); pend[k] = []; pend_n[k] = 0
                    self.pos = time.time() - self.t_start
                    self.sink(c['name'], y, self.pos)
        except (OSError, ValueError, KeyError) as e:
            self.error = str(e)
        finally:
            self.connected.clear(); conn.close()
