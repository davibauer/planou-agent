#!/usr/bin/env python3
"""Every path of an agent instance in one place. No script builds a ~/.config path on its own.

One plugin, one instance per agent. The instance name IS the agent name (planou-dev, work-watch-acme, job-scout): the
key in Planou, in the fixed session of the `team` launcher, in the lessons. Its folder comes from the resolver
`watch_core.config.agent_root(name)`: ~/.config/agent/<name>/ when it exists, else the legacy folder of the old plugins
(~/.config/work-watch/<x>/ for work-watch-<x>, ~/.config/<name>/ for any other), so an instance keeps working in place
until it moves.

<root>/
  config/     what the user decides       config.json (schema.py), instructions.md
  data/       what the agent accumulates  state.json (cursors, pending queue)
  secrets/    tokens (0700 / 0600)        planou.env and whatever a source needs; never goes into backup
  cache/      disposable                  runner output (cache/runner), Planou cache (cache/planou)
  behaviors/  optional                    instance-only behaviors (<name>/BEHAVIOR.md), read before the plugin's
  adapters/   optional                    instance-only adapters (<type>.py), loaded before the plugin's

The config is config/config.json; a config.json right in the root is read when config/ has none.
"""
import os, re, sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPTS)
BEHAVIORS_DIR = os.path.join(SKILL_DIR, 'behaviors')
if SCRIPTS not in sys.path: sys.path.insert(0, SCRIPTS)

from watch_core import config as wc          # noqa: E402

# old module names some instance adapters still import (see work-watch's paths.py): same module object
LEGACY_MODULES = {'daily_vigia': 'daily', 'notion_daily': 'notion_page', 'decisoes_notion': 'notion_decisions',
                  'tarefas_core': 'tasks', 'licoes_vigia': 'lessons', 'checar_duplicidade': 'rules_dedupe',
                  'gravacoes_core': 'recordings'}
NAME_RE = re.compile(r'[a-z0-9][a-z0-9_-]{0,60}')

WORK_WATCH_PREFIX = 'work-watch-'
BASE = os.path.expanduser('~/.config/work-watch')      # legacy home of the work-watch instances (paths.instancias)
WORKSPACE_DIRS = ('messages', 'meetings', 'artifacts', 'decisions', 'journal')


def _skill_dir(skill, sub):
    """Scripts of a sibling skill of this plugin (work-inbox and meeting-minutes live here since E6), else the
    one linked in ~/.claude/skills."""
    own = os.path.join(os.path.dirname(os.path.realpath(SKILL_DIR)), skill, 'scripts', sub)
    linked = os.path.join(os.path.expanduser('~/.claude/skills'), skill, 'scripts', sub)
    return linked if not os.path.isdir(own) and os.path.isdir(linked) else own


# the work-watch adapters reach these skills by path (Teams/Outlook/Slack clients, recording transcription)
WORK_INBOX = _skill_dir('work-inbox', 'work_inbox')
TEAMS_CHAT = WORK_INBOX
MEETING_MINUTES = _skill_dir('meeting-minutes', 'meeting_minutes')

NAME = ROOT = CONFIG_DIR = DATA_DIR = SECRETS_DIR = CACHE_DIR = ADAPTERS_DIR = LOCAL_BEHAVIORS = None
CONFIG = STATE = INSTRUCTIONS = CONTEXT = RUNNER_DIR = WORKSPACE = None
NOME = None       # the short name the work-watch adapters know: <x> for work-watch-<x> (== CUSTOS <x>, workspace
                  # {instance}, meetings of the instance), the full name for any other instance


def valid_name(name):
    return bool(NAME_RE.fullmatch(name or ''))


def instance(name):
    """Points every path at the instance folder. Creates nothing (folders are made on the first write)."""
    global NAME, NOME, ROOT, CONFIG_DIR, DATA_DIR, SECRETS_DIR, CACHE_DIR, ADAPTERS_DIR, LOCAL_BEHAVIORS
    global CONFIG, STATE, INSTRUCTIONS, CONTEXT, RUNNER_DIR, WORKSPACE
    if not valid_name(name):
        sys.exit(f'instancia invalida: {name!r} (letras minusculas, digitos, - e _)')
    NAME = name
    NOME = short_name(name)
    ROOT = wc.agent_root(name)
    CONFIG_DIR, DATA_DIR = os.path.join(ROOT, 'config'), os.path.join(ROOT, 'data')
    SECRETS_DIR, CACHE_DIR = os.path.join(ROOT, 'secrets'), os.path.join(ROOT, 'cache')
    ADAPTERS_DIR, LOCAL_BEHAVIORS = os.path.join(ROOT, 'adapters'), os.path.join(ROOT, 'behaviors')
    CONFIG = os.path.join(CONFIG_DIR, 'config.json')
    if not os.path.isfile(CONFIG) and os.path.isfile(os.path.join(ROOT, 'config.json')):
        CONFIG = os.path.join(ROOT, 'config.json')
    INSTRUCTIONS = os.path.join(CONFIG_DIR, 'instructions.md')
    STATE = os.path.join(DATA_DIR, 'state.json')
    RUNNER_DIR = os.path.join(CACHE_DIR, 'runner')
    WORKSPACE = None
    CONTEXT = None
    return ROOT


def short_name(name):
    """work-watch-<x> -> <x>; any other name as is."""
    return name[len(WORK_WATCH_PREFIX):] if name and name.startswith(WORK_WATCH_PREFIX) and len(name) > len(WORK_WATCH_PREFIX) else name


def use_workspace(p):
    """config "workspace" ({instance} = the short name, ~ expanded; absolute). The workspace keeps CONTEXT.md for the
    session. A work-watch instance without "workspace" keeps work-watch's default, ~/WorkWatch/<x>."""
    global WORKSPACE, CONTEXT
    if not p and NAME and NAME.startswith(WORK_WATCH_PREFIX): p = '~/WorkWatch/{instance}'
    if not p: return None
    p = os.path.expanduser(p.replace('{instance}', NOME or ''))
    if not os.path.isabs(p): sys.exit(f'"workspace" precisa ser um caminho absoluto: {p!r}')
    WORKSPACE, CONTEXT = p, os.path.join(p, 'CONTEXT.md')
    return WORKSPACE


usa_workspace = use_workspace


def inside(path):
    """True when `path` is inside the instance folder (the only place the plugin writes state)."""
    if not ROOT or not path: return False
    p, r = os.path.realpath(os.path.expanduser(path)), os.path.realpath(ROOT)
    return p == r or p.startswith(r + os.sep)


dentro = inside


def expand(p):
    """Path from the config: ~ and {instance} expanded; relative paths are relative to the instance folder."""
    if not p: return p
    p = os.path.expanduser(p.replace('{instance}', NOME or ''))
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


expande = expand


def area(sub=None, criar=False):
    """Workspace folder (or one of WORKSPACE_DIRS inside it); criar=True makes it on the way (work-watch's paths.area)."""
    d = WORKSPACE if not sub else os.path.join(WORKSPACE, sub)
    if criar: os.makedirs(d, exist_ok=True)
    return d


def agent(cfg=None):
    """Name of this instance's agent in the shared history (lessons, recording owners, KPIs): config "agent", canonical
    (watch_core.agents); default the instance name."""
    from watch_core.agents import agent_of
    return agent_of(cfg, NAME)


def _legacy_config_dir(p):
    """True for a ~/.config/<name>-vigia folder (where the old agents kept their credentials)."""
    return bool(p) and re.fullmatch(re.escape(os.path.expanduser('~/.config/')) + r'[a-z0-9_]+-vigia', p.rstrip('/')) is not None


def google_dir(p=None):
    """Folder of the Google OAuth files of the instance (work-watch's paths.google_dir): config "google_dir" (default
    secrets/google); the old ~/.config/<name>-vigia folder only when the new place has nothing."""
    novo = os.path.join(SECRETS_DIR, 'google')
    tem = lambda d: os.path.isfile(os.path.join(d, 'token.json')) or os.path.isfile(os.path.join(d, 'client_secret.json'))
    d = expand(p) if p else novo
    if _legacy_config_dir(d) and tem(novo): return novo
    if not p and not tem(novo):
        from watch_core.agents import legacy_agent_names
        for n in [NOME] + legacy_agent_names(NAME):
            old = os.path.expanduser(f'~/.config/{n}-vigia')
            if tem(old): return old
    return d


def behavior_file(name):
    """BEHAVIOR.md of a behavior: the instance's own (behaviors/<name>/) first, then the plugin's. None when missing."""
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,60}', name or ''): return None
    for base in (LOCAL_BEHAVIORS, BEHAVIORS_DIR):
        f = os.path.join(base or '', name, 'BEHAVIOR.md')
        if base and os.path.isfile(f): return f
    return None


def plugin_behaviors():
    try: return sorted(d for d in os.listdir(BEHAVIORS_DIR) if os.path.isfile(os.path.join(BEHAVIORS_DIR, d, 'BEHAVIOR.md')))
    except OSError: return []


def instances():
    """Instances of the agent plugin: folders under ~/.config/agent with a config."""
    base = wc.agent_base()
    try: names = os.listdir(base)
    except OSError: return []
    return sorted(n for n in names if valid_name(n) and (os.path.isfile(os.path.join(base, n, 'config', 'config.json'))
                                                         or os.path.isfile(os.path.join(base, n, 'config.json'))))


def instancias():
    """Short names of the work-watch instances, migrated or not (work-watch's paths.instancias: the meetings of the
    workspace are split among them)."""
    out = set()
    try: out |= {d for d in os.listdir(BASE) if os.path.isfile(os.path.join(BASE, d, 'config', 'config.json'))}
    except OSError: pass
    out |= {short_name(n) for n in instances() if n.startswith(WORK_WATCH_PREFIX)}
    return sorted(out)


instancia = instance


def lib():
    """Registers the old module names as aliases of watch_core modules. Returns the package folder."""
    import importlib
    for old, new in LEGACY_MODULES.items():
        if old not in sys.modules: sys.modules[old] = importlib.import_module(f'watch_core.{new}')
    return os.path.join(SCRIPTS, 'watch_core')


lib()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('instancias:', ', '.join(instances()) or '(nenhuma)'); sys.exit(0)
    instance(sys.argv[1])
    for n in ('ROOT', 'CONFIG', 'INSTRUCTIONS', 'STATE', 'SECRETS_DIR', 'CACHE_DIR', 'ADAPTERS_DIR', 'LOCAL_BEHAVIORS'):
        p = globals()[n]; print(f'{n:15s} {"ok " if os.path.exists(p) else "-  "} {p}')
