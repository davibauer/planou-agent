"""Windows side of the meeting copilot: the microphone (the user) and the WASAPI loopback of the speakers (everyone
else in the call) as two separate channels, streamed as raw PCM to the WSL side on localhost. Nothing is written to
disk and nothing leaves the machine.

Runs with the venv made by `meeting-copilot setup-windows` (needs only pyaudiowpatch).
  python capture.py --list                      the WASAPI devices
  python capture.py --port 8766 [--mic NAME] [--loopback NAME] [--ffmpeg PATH]
                                                NAME = part of the device name; default = Windows default
The microphone falls back to ffmpeg (DirectShow) when WASAPI refuses it: the Store build of Python is a packaged app
and Windows keeps a separate microphone permission for it (Settings > Privacy > Microphone), while ffmpeg.exe counts
as a desktop app. The loopback needs no permission.
Protocol: one JSON line {"channels": {"0": {"name": "mic", "rate": R, "ch": C}, "1": {...loopback}}}, then frames
<B channel><I nbytes><int16 PCM>. Quits when the WSL side goes away for more than 15 s.
"""
import argparse, json, queue, socket, struct, subprocess, sys, threading, time

import pyaudiowpatch as pyaudio


def wasapi(p):
    return p.get_host_api_info_by_type(pyaudio.paWASAPI)


def list_devices(p):
    w = wasapi(p)
    print('default output:', p.get_device_info_by_index(w['defaultOutputDevice'])['name'])
    print('default input :', p.get_device_info_by_index(w['defaultInputDevice'])['name'])
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d['hostApi'] != w['index']: continue
        kind = 'loopback' if d.get('isLoopbackDevice') else ('input' if d['maxInputChannels'] else 'output')
        print(f"  [{i}] {kind:8s} {d['name']}  ({int(d['defaultSampleRate'])} Hz, in {d['maxInputChannels']})")


def pick_mic(p, name):
    w = wasapi(p)
    if name:
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d['hostApi'] == w['index'] and d['maxInputChannels'] and not d.get('isLoopbackDevice') and name.lower() in d['name'].lower():
                return d
        sys.exit(f'no WASAPI input device matching {name!r} (see --list)')
    return p.get_device_info_by_index(w['defaultInputDevice'])


def pick_loopback(p, name):
    w = wasapi(p)
    target = name or p.get_device_info_by_index(w['defaultOutputDevice'])['name']
    for d in p.get_loopback_device_info_generator():
        if target.lower() in d['name'].lower(): return d
    sys.exit(f'no loopback device for {target!r} (see --list)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1'); ap.add_argument('--port', type=int, default=8766)
    ap.add_argument('--mic', default=''); ap.add_argument('--loopback', default=''); ap.add_argument('--list', action='store_true')
    ap.add_argument('--seconds', type=float, default=0, help='stop after N seconds (tests)')
    ap.add_argument('--ffmpeg', default='ffmpeg.exe', help='for the DirectShow fallback of the microphone')
    ap.add_argument('--mic-backend', choices=['auto', 'wasapi', 'dshow'], default='auto')
    a = ap.parse_args()
    p = pyaudio.PyAudio()
    if a.list: list_devices(p); return
    mic, lb = pick_mic(p, a.mic), pick_loopback(p, a.loopback)
    q = queue.Queue(maxsize=2000)
    info, streams, procs = {}, [], []

    def open_wasapi(cid, name, d):
        ch = max(1, min(2, int(d['maxInputChannels'])))
        rate = int(d['defaultSampleRate'])

        def cb(data, frames, t, status):
            try: q.put_nowait((cid, data))
            except queue.Full: pass
            return (None, pyaudio.paContinue)
        st = p.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True, input_device_index=d['index'],
                    frames_per_buffer=rate // 10, stream_callback=cb)
        streams.append(st)
        info[str(cid)] = {'name': name, 'rate': rate, 'ch': ch, 'device': d['name'], 'backend': 'wasapi'}

    def open_dshow(cid, name, dev_name):
        pr = subprocess.Popen([a.ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'dshow', '-audio_buffer_size', '50',
                               '-i', f'audio={dev_name}', '-ac', '1', '-ar', '16000', '-f', 's16le', '-'],
                              stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        procs.append(pr)

        def pump():
            while True:
                data = pr.stdout.read(3200)
                if not data: break
                try: q.put_nowait((cid, data))
                except queue.Full: pass
        threading.Thread(target=pump, daemon=True).start()
        info[str(cid)] = {'name': name, 'rate': 16000, 'ch': 1, 'device': dev_name, 'backend': 'dshow'}

    if a.mic_backend == 'dshow':
        open_dshow(0, 'mic', mic['name'])
    else:
        try:
            open_wasapi(0, 'mic', mic)
        except OSError as e:
            if a.mic_backend == 'wasapi': raise
            print(f'WASAPI refused the microphone ({e}); using ffmpeg/DirectShow', flush=True)
            open_dshow(0, 'mic', mic['name'])
    open_wasapi(1, 'loopback', lb)
    print('capturing:', json.dumps(info), flush=True)
    t0, lost = time.time(), None
    while True:
        try:
            s = socket.create_connection((a.host, a.port), timeout=3)
        except OSError:
            lost = lost or time.time()
            if time.time() - lost > 15: print('WSL side gone, quitting', flush=True); break
            time.sleep(1); continue
        lost = None
        print('connected to', a.host, a.port, flush=True)
        try:
            s.sendall((json.dumps({'channels': info}) + '\n').encode())
            while not (a.seconds and time.time() - t0 > a.seconds):
                try: cid, data = q.get(timeout=1)
                except queue.Empty: continue
                s.sendall(struct.pack('<BI', cid, len(data)) + data)
            break
        except OSError:
            lost = time.time()
        finally:
            s.close()
    for st in streams: st.stop_stream(); st.close()
    for pr in procs: pr.kill()
    p.terminate()


if __name__ == '__main__':
    main()
