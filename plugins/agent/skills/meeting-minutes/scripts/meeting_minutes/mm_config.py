#!/usr/bin/env python3
"""Where meeting-minutes keeps the user's things (the code has none of it).

~/.config/meeting-minutes/            (MEETING_MINUTES_HOME moves it)
  config.json         whisper_model, language, hf_token_path, audio_stream, batch_size, user_name, user_aliases,
                      phoneme_model, videos_dir (OBS folder; env ATA_VIDEOS_DIR wins). Missing = config-example.json
  speaker_refs.json   {"Name": [["ata_<stem>", "SPEAKER_NN"], ...]} voices already named in past minutes (xmatch.py)
  state/              watcher state (PAUSED, skip.tsv, sizes/, logs) and voices/ (voice catalog: biometric data,
                      never leaves the machine)

CLI (for the shell scripts):  mm_config.py home | config | state | voices | get <key> [default]
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser(os.environ.get('MEETING_MINUTES_HOME') or '~/.config/meeting-minutes')
CONFIG = os.path.join(HOME, 'config.json')
EXAMPLE = os.path.join(HERE, 'config-example.json')
STATE = os.path.join(HOME, 'state')
VOICES = os.path.join(STATE, 'voices')
SPEAKER_REFS = os.path.join(HOME, 'speaker_refs.json')


def config_path():
    return CONFIG if os.path.exists(CONFIG) else EXAMPLE


def load():
    try: return json.load(open(config_path(), encoding='utf-8'))
    except (OSError, ValueError): return {}


def videos_dir():
    return os.path.expanduser(os.environ.get('ATA_VIDEOS_DIR') or load().get('videos_dir') or '~/Videos')


if __name__ == '__main__':
    a = sys.argv[1:] or ['home']
    if a[0] == 'home': print(HOME)
    elif a[0] == 'config': print(config_path())
    elif a[0] == 'state': print(STATE)
    elif a[0] == 'voices': print(VOICES)
    elif a[0] == 'videos_dir': print(videos_dir())
    elif a[0] == 'get': print(load().get(a[1], a[2] if len(a) > 2 else ''))
    else: sys.exit(__doc__)
