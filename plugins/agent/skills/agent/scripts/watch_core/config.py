"""Where watch_core keeps the user's things. No module builds a ~/.config path on its own.

watch_core is shared by the agent plugins of this marketplace (work-watch, job-scout). The CODE ships inside each
plugin (a generated copy of shared/watch_core, see the repo README); what belongs to the user stays on the machine:

~/.config/watch-core/            (WATCH_CORE_HOME moves it)
  config.json    notion.dailies_page / notion.month_page (daily page), recordings.* (videos folder, archive folder,
                 minutes skill, user aliases), rules_dedupe.* (which instruction files to compare),
                 legacy_agent_names (old agent name -> canonical, for the names that only the user knows, e.g. the
                 short name a client's agent had before 0.19.0 of work-watch; see watch_core.agents):
                     "legacy_agent_names": {"acmecorp": "work-watch-acme", "globex": "work-watch-globex"}
  data/          lessons.md (daily lessons of every agent), rules.md (rules that hold for every agent),
                 recording_owners.json (which agent owns each recording)
  secrets/       notion.env (NOTION_TOKEN=... of the internal integration that writes the daily page), 0600

Agent folders (agent plugin 0.1.0): `agent_root(name)` is the one resolver every caller uses (the runners, planou,
transcript, the launcher's hooks). ~/.config/agent/<name>/ wins when it exists; otherwise the legacy place:
~/.config/work-watch/<x>/ for work-watch-<x>, ~/.config/<name>/ for any other agent. So an agent keeps working in place
until its folder moves, and a new agent (planou-dev) is born in ~/.config/agent/.

Legacy locations (before 25/09/2026, when these libraries lived loose in ~/.claude/skills/vigia-*) are still read
when the new file does not exist yet; `python3 -m watch_core.config --migrate` copies them over.
"""
import json, os, shutil, sys, tempfile

TEST_ENV = 'WATCH_CORE_TEST'   # "1" in the test suites: paths outside the temporary folder are refused

# Every path is resolved when it is read (module __getattr__ below), never frozen at import: a test that imports this
# module before another one moves HOME/WATCH_CORE_HOME must not keep pointing at the real ~/.config (28/09/2026 a test
# rewrote the user's config.json that way).
_NAMES = {'CONFIG': ('config.json',), 'DATA_DIR': ('data',), 'SECRETS_DIR': ('secrets',),
          'LESSONS_MD': ('data', 'lessons.md'), 'RULES_MD': ('data', 'rules.md'),
          'RECORDING_OWNERS': ('data', 'recording_owners.json'), 'NOTION_ENV': ('secrets', 'notion.env')}


def root():
    """~/.config/watch-core (or $WATCH_CORE_HOME), read now. Under WATCH_CORE_TEST=1 it must live in the temp folder."""
    r = os.path.expanduser(os.environ.get('WATCH_CORE_HOME') or '~/.config/watch-core')
    if os.environ.get(TEST_ENV) == '1':
        tmp = os.path.realpath(tempfile.gettempdir())
        if os.path.commonpath([os.path.realpath(r), tmp]) != tmp:
            raise RuntimeError(f'{TEST_ENV}=1 but watch_core points at {r} (outside {tmp}): refusing to touch the real config')
    return r


def path(name):
    """CONFIG, DATA_DIR, LESSONS_MD... resolved now (a value set on the module by a test's mock.patch.object wins)."""
    if name in globals(): return globals()[name]
    return os.path.join(root(), *_NAMES[name])


def legacy():
    """New location -> legacy location (before 25/09/2026, libraries loose in ~/.claude/skills/vigia-*)."""
    old_skills = os.path.expanduser('~/.claude/skills')
    return {
        path('LESSONS_MD'): os.path.join(old_skills, 'vigia-licoes', 'LICOES.md'),
        path('RULES_MD'): os.path.join(old_skills, 'vigia-licoes', 'REGRAS.md'),
        path('RECORDING_OWNERS'): os.path.expanduser('~/.config/vigia-gravacoes/donos.json'),
        path('NOTION_ENV'): os.path.expanduser('~/.config/vigia/notion.env'),
    }


def __getattr__(name):   # config.ROOT, config.CONFIG, config.LEGACY... keep working, always current
    if name == 'ROOT': return root()
    if name == 'LEGACY': return legacy()
    if name in _NAMES: return path(name)
    raise AttributeError(name)


_cache = None


def load():
    """config.json as a dict ({} when missing or invalid); cached per path (tests that move CONFIG read the new one)."""
    global _cache
    cfg = path('CONFIG')
    if _cache is None or _cache[0] != cfg:
        try: d = json.load(open(cfg, encoding='utf-8'))
        except (OSError, ValueError): d = {}
        _cache = (cfg, d if isinstance(d, dict) else {})
    return _cache[1]


def get(path, default=None):
    """get('notion.dailies_page') -> value in config.json, or default."""
    v = load()
    for k in path.split('.'):
        if not isinstance(v, dict) or k not in v: return default
        v = v[k]
    return v


def file(p):
    """The file to use: the new location when it exists, else the legacy one when that exists, else the new one."""
    old = legacy().get(p)
    if os.path.exists(p) or not old or not os.path.exists(old): return p
    return old


AGENT_DIR = '~/.config/agent'
WORK_WATCH_PREFIX = 'work-watch-'


def agent_base():
    """~/.config/agent, resolved at call time (tests move HOME)."""
    return os.path.expanduser(AGENT_DIR)


def legacy_agent_root(name):
    """Where an agent lived before the agent plugin: ~/.config/work-watch/<x> for work-watch-<x>, ~/.config/<name>."""
    if name.startswith(WORK_WATCH_PREFIX):
        return os.path.expanduser(f'~/.config/work-watch/{name[len(WORK_WATCH_PREFIX):]}')
    return os.path.expanduser(f'~/.config/{name}')


def agent_root(name):
    """Folder of an agent: ~/.config/agent/<name> when it exists, else the legacy folder (legacy_agent_root)."""
    new = os.path.join(agent_base(), name)
    return new if os.path.isdir(new) else legacy_agent_root(name)


def is_agent_layout(root):
    """True when `root` is an agent folder of the agent plugin (directly under ~/.config/agent)."""
    if not root: return False
    r = os.path.abspath(os.path.expanduser(str(root))).rstrip(os.sep)
    return os.path.dirname(r) == os.path.abspath(agent_base())


def migrate(dry=False):
    """Copies legacy files and the old page ids into ~/.config/watch-core (never overwrites, never deletes the old)."""
    out = []
    for new, old in legacy().items():
        if os.path.exists(new) or not os.path.exists(old): continue
        out.append(f'{old} -> {new}')
        if dry: continue
        os.makedirs(os.path.dirname(new), exist_ok=True)
        shutil.copy2(old, new)
        if new == path('NOTION_ENV'): os.chmod(new, 0o600); os.chmod(path('SECRETS_DIR'), 0o700)
    return out


if __name__ == '__main__':
    if '--migrate' in sys.argv:
        r = migrate('--dry' in sys.argv)
        print('\n'.join(r) if r else 'nothing to migrate')
    else:
        print(f'root: {root()}')
        for p in map(path, ('CONFIG', 'LESSONS_MD', 'RULES_MD', 'RECORDING_OWNERS', 'NOTION_ENV')):
            print(f'  {p}: {"ok" if os.path.exists(p) else "missing"}' + (f' (using legacy {file(p)})' if file(p) != p else ''))
