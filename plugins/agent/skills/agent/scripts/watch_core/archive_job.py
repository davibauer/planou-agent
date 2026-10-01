#!/usr/bin/env python3
"""Moves a recording to the archive folder, outside the tick (PLN0245). Started by recordings.arquiva_video as a
detached process that nobody waits for: the copy reads the whole .mp4 (gigabytes, often from one Windows drive to
another), and a read that stops answering there would hang whoever waited for it.

  python3 archive_job.py '<json>'    json: ts, src, dst, tam, workdir, novo_workdir, lock, err

Steps, all in this process: take the lock (a local file; a second job for the same recording leaves at once), rename
the ata_ folder to its final name, copy the video to <dst>.part, check the size, rename it to <dst>, remove the source
and write video.txt in the ata_ folder. A failure goes to `err` (a local file the next tick reports and removes).
Standard library only.
"""
import fcntl, json, os, shutil, sys


def job(a):
    os.makedirs(os.path.dirname(a['lock']), exist_ok=True)
    lk = open(a['lock'], 'w')
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0                                   # another job is moving this recording
    try:
        wd = a['workdir']
        if a.get('novo_workdir') and a['novo_workdir'] != wd and not os.path.exists(a['novo_workdir']):
            os.rename(wd, a['novo_workdir']); wd = a['novo_workdir']
        src, dst, tam = a['src'], a['dst'], a['tam']
        if os.path.exists(dst) and os.path.getsize(dst) == tam:
            os.remove(src)
        else:
            shutil.copy2(src, dst + '.part')
            if os.path.getsize(dst + '.part') != tam:
                os.remove(dst + '.part'); raise RuntimeError(f'copia incompleta para {dst}')
            os.replace(dst + '.part', dst); os.remove(src)
        with open(os.path.join(wd, 'video.txt'), 'w') as f: f.write(dst + '\n')
        return 0
    except Exception as e:
        try:
            with open(a['err'], 'w') as f: f.write(f'{type(e).__name__}: {e}\n')
        except OSError:
            pass
        return 1
    finally:
        lk.close()


if __name__ == '__main__':
    sys.exit(job(json.loads(sys.argv[1])))
