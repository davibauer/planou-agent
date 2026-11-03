"""Where the copilot finds the user's things. The code holds none of it: names, paths and credentials come from config.

~/.config/meeting-copilot/config.json        (MEETING_COPILOT_HOME moves it; every key is optional)
  asr_model, asr_compute, cards_model, notes_model, addressee_model ('' = off), port, notes_every_s, windows_tools_dir,
  gate_topics (true = the topic map also in the deterministic gate, not only in the model stage),
  mic_device, loopback_device (substrings of the Windows device names; empty = the default ones),
  instances: {"<instance>": {"memory_dirs": [...], "extra_files": [...], "asr_terms": [...], "topics": [...]}}
  eval_exclude: [...] (folder names or ids the eval leaves out: personal recordings), eval_exclude_types: [...]
~/.config/meeting-minutes/config.json         user_name, user_aliases, hf_token_path (the meeting-minutes skill)
~/.config/meeting-minutes/state/voices/       the voice catalog (<name>.npy), used to tell the user from the others
~/.config/agent/<instance>/config/config.json workspace (CONTEXT.md, handoff.md, memory/, meetings/), sources github/jira
Session output: ~/.config/agent/<instance>/data/meeting-copilot/<YYYY-MM-DD-HHMMSS>/
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(os.path.dirname(HERE))
MM_SCRIPTS = os.path.join(os.path.dirname(SKILL), 'meeting-minutes', 'scripts', 'meeting_minutes')
HOME = os.path.expanduser(os.environ.get('MEETING_COPILOT_HOME') or '~/.config/meeting-copilot')
AGENT_HOME = os.path.expanduser(os.environ.get('AGENT_HOME') or '~/.config/agent')
GPU_LOCK = os.environ.get('ATA_GPU_LOCK') or '/tmp/ata-reuniao-whisperx.lock'   # shared with meeting-minutes

DEFAULTS = {
    'asr_model': 'large-v3', 'asr_compute': 'int8_float16', 'language': 'en',
    'cards_model': 'claude-haiku-5-5', 'notes_model': 'claude-sonnet-5-5', 'addressee_model': 'claude-sonnet-5-5',
    'port': 8765, 'capture_port': 8766, 'notes_every_s': 60, 'hedge': 2,
    'windows_tools_dir': '%LOCALAPPDATA%\\meeting-copilot', 'mic_device': '', 'loopback_device': '',
    'instances': {},
}


def _load(path):
    try:
        with open(path, encoding='utf-8') as f: return json.load(f)
    except (OSError, ValueError):
        return {}


def load():
    c = dict(DEFAULTS); c.update(_load(os.path.join(HOME, 'config.json'))); return c


def mm():
    """meeting-minutes config + its paths (voices, HF token)."""
    sys.path.insert(0, MM_SCRIPTS)
    try:
        import mm_config
        return {'config': mm_config.load(), 'voices': mm_config.VOICES}
    except Exception:
        return {'config': {}, 'voices': ''}


def user():
    c = mm()['config']
    name = c.get('user_name') or os.environ.get('USER', 'me')
    aliases = [a for a in (c.get('user_aliases') or []) if a] or [name]
    return name, aliases


def instance_dir(instance):
    return os.path.join(AGENT_HOME, instance)


def instance_config(instance):
    return _load(os.path.join(instance_dir(instance), 'config', 'config.json'))


def workspace(instance):
    """The instance's workspace folder ({instance} = the name without the 'work-watch-' prefix, as the agent does)."""
    ws = instance_config(instance).get('workspace') or ''
    if not ws: return ''
    short = instance.split('work-watch-', 1)[-1]
    for cand in (ws.replace('{instance}', short), ws.replace('{instance}', instance)):
        p = os.path.expanduser(cand)
        if os.path.isdir(p): return p
    return ''


def source(instance, kind):
    for s in instance_config(instance).get('sources') or []:
        if isinstance(s, dict) and s.get('type') == kind: return s
    return {}


def instance_extra(instance):
    return (load().get('instances') or {}).get(instance) or {}


def session_root(instance):
    return os.path.join(instance_dir(instance), 'data', 'meeting-copilot')
