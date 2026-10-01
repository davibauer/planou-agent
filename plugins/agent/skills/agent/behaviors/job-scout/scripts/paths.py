#!/usr/bin/env python3
"""All job-scout paths in one place. No script builds a ~/.config path on its own.

The job-scout behavior of the agent plugin (E4 of docs/agent-plugin-design.md). The instance is `job-scout` (or the name
in $AGENT_INSTANCE, which agent.py sets); its folder is the agent plugin's: ~/.config/agent/job-scout/ once
`agent.py job-scout --migrate` moved it, the old ~/.config/job-scout/ before that (the link left there after the move
keeps every old path working), and ~/.config/agent/job-scout/ on a machine that has neither (setup.py).

<root>/
  config/    what the user decides       config.json (criteria, CV profiles, pay ranges), profile.md, instructions.md, resume-base.md
  data/      what the scout accumulates  state.json, reputation.json, pipeline.md, lessons.md, history/ (criteria.log, config versions),
                                         judgments/ (the judge's detail per job)
  secrets/   cookie and token (0600)     session.json, notion.env      -> never goes into backup
  cache/     disposable                  names.json, linkedin-profile.json, runner/

Backup: config/ and data/. Unlike the old plugin, importing this module changes nothing on disk: the migrations of
the old plugin (the ~/.config/linkedin-vigia folder, the pre-0.4 flat layout, the Portuguese keys up to 0.8.x) run only
with `paths.py --migrate` (a folder that the old plugin ran on is already migrated).
"""
import os, sys, json, shutil, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from watch_core import fileio, config as _wc

NAME = os.environ.get('AGENT_INSTANCE') or 'job-scout'


def _root(name):
    """~/.config/agent/<name> when it exists or when the old folder does not; else the old ~/.config/<name>."""
    new, old = os.path.join(_wc.agent_base(), name), _wc.legacy_agent_root(name)
    return new if os.path.isdir(new) or not os.path.isdir(old) else old


ROOT = _root(NAME)
OLD_ROOT = os.path.expanduser('~/.config/linkedin-vigia')        # plugin name up to 0.5.x
CONFIG_DIR, DATA_DIR = os.path.join(ROOT, 'config'), os.path.join(ROOT, 'data')
SECRETS_DIR, CACHE_DIR = os.path.join(ROOT, 'secrets'), os.path.join(ROOT, 'cache')
HISTORY_DIR = os.path.join(DATA_DIR, 'history')

CONFIG = os.path.join(CONFIG_DIR, 'config.json')
PROFILE = os.path.join(CONFIG_DIR, 'profile.md')
INSTRUCTIONS = os.path.join(CONFIG_DIR, 'instructions.md')
RESUME_BASE = os.path.join(CONFIG_DIR, 'resume-base.md')

STATE = os.path.join(DATA_DIR, 'state.json')
_LOCK = None

def lock_state(timeout=180):
    """Holds an exclusive lock on the state until the process ends (26/09/2026): a CLI write (--job, matrix, application)
    during a tick used to be lost, because the tick loaded state.json before and saved over it after. Whoever comes
    second waits (a tick takes about a minute); after `timeout` it goes on with a warning instead of hanging."""
    global _LOCK
    if _LOCK is not None: return True
    import fcntl, sys, time
    os.makedirs(DATA_DIR, exist_ok=True)
    f = open(os.path.join(DATA_DIR, 'state.lock'), 'w')
    end = time.time() + timeout
    while True:
        try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); _LOCK = f; return True
        except BlockingIOError:
            if time.time() > end:
                print(f'aviso: state.json travado por outro processo ha {timeout}s; seguindo sem a trava', file=sys.stderr); return False
            time.sleep(1)
REPUTATION = os.path.join(DATA_DIR, 'reputation.json')
PIPELINE = os.path.join(DATA_DIR, 'pipeline.md')
LESSONS = os.path.join(DATA_DIR, 'lessons.md')
CRITERIA_LOG = os.path.join(HISTORY_DIR, 'criteria.log')
INTERVIEWS_DIR = os.path.join(DATA_DIR, 'interviews')     # default; config "entrevistas_dir" overrides
CV_DIR = os.path.join(DATA_DIR, 'cv')                     # default; config "cv_dir" overrides
JUDGMENTS_DIR = os.path.join(DATA_DIR, 'judgments')       # one <id>.md per judged job, written by the judge subagent (judge.py)

SESSION = os.path.join(SECRETS_DIR, 'session.json')
NOTION_ENV = os.path.join(SECRETS_DIR, 'notion.env')

NAMES = os.path.join(CACHE_DIR, 'names.json')
LINKEDIN_PROFILE = os.path.join(CACHE_DIR, 'linkedin-profile.json')
RUNNER_DIR = os.path.join(CACHE_DIR, 'runner')

# old layout (up to 0.3.x): name in the root -> new path
LEGACY = {
    'config.json': CONFIG, 'perfil.md': PROFILE, 'instrucoes.md': INSTRUCTIONS, 'cv-base.md': RESUME_BASE,
    'state.json': STATE, 'reputacao.json': REPUTATION, 'funil.md': PIPELINE, 'licoes.md': LESSONS,
    'criterios.log': CRITERIA_LOG, 'session.json': SESSION, 'notion.env': NOTION_ENV,
    'names.json': NAMES, 'linkedin_perfil.json': LINKEDIN_PROFILE, 'runner': RUNNER_DIR,
    'entrevistas': INTERVIEWS_DIR, 'cv': CV_DIR,
}

def _mkdirs():
    for d in (CONFIG_DIR, DATA_DIR, HISTORY_DIR, CACHE_DIR): os.makedirs(d, exist_ok=True)
    os.makedirs(SECRETS_DIR, mode=0o700, exist_ok=True)

def migrate(verbose=False):
    """Old folder name, old layout and old data schema -> current. Idempotent: only acts when something old is still there."""
    done = _migrate_layout()
    if os.path.isdir(ROOT): done += _migrate_schema()
    if verbose: print('\n'.join(done))
    return done

def _rewrite(path, fn, backup_suffix=None):
    """Applies fn to a JSON file; writes (tmp + replace) only when something changed, keeping a backup copy once."""
    try: raw = open(path, encoding='utf-8').read(); d = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError): return False
    new = fn(json.loads(raw))
    if new == d: return False
    if backup_suffix and not os.path.exists(path + backup_suffix): shutil.copy2(path, path + backup_suffix)
    fileio.write_json(path, new, indent=1, ensure_ascii=False)
    return True

def _migrate_schema():
    import schema
    done = []
    if _rewrite(STATE, schema.state, '.pre-0.9'): done.append('state.json: chaves em ingles')
    if _rewrite(CONFIG, schema.config, '.pre-0.9'): done.append('config.json: chaves em ingles')
    if _rewrite(REPUTATION, schema.reputation, '.pre-0.9'): done.append('reputation.json: chaves em ingles')
    return done

def _migrate_layout():
    done = []
    if os.path.isdir(OLD_ROOT) and not os.path.islink(OLD_ROOT) and not os.path.exists(ROOT):
        shutil.move(OLD_ROOT, ROOT); done.append(f'{OLD_ROOT} -> {ROOT}')
    if not os.path.isdir(ROOT): return done
    pending = [n for n in LEGACY if os.path.exists(os.path.join(ROOT, n))]
    extras = glob.glob(os.path.join(ROOT, 'config.json.2*')) + [os.path.join(ROOT, 'faixas.json')] * os.path.exists(os.path.join(ROOT, 'faixas.json'))
    if not pending and not extras: return done
    _mkdirs()
    for name in pending:
        src, dst = os.path.join(ROOT, name), LEGACY[name]
        if os.path.exists(dst): continue                        # already migrated before (does not overwrite)
        shutil.move(src, dst); done.append(f'{name} -> {os.path.relpath(dst, ROOT)}')
    for p in glob.glob(os.path.join(ROOT, 'config.json.2*')):   # dated copies from criteria.py
        shutil.move(p, os.path.join(HISTORY_DIR, os.path.basename(p))); done.append(f'{os.path.basename(p)} -> data/history/')
    fx = os.path.join(ROOT, 'faixas.json')
    if os.path.exists(fx):                                      # pay ranges become the "faixas" key of config.json
        try: c = json.load(open(CONFIG, encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError): c = {}
        if 'faixas' not in c:
            c['faixas'] = json.load(open(fx, encoding='utf-8'))
            fileio.write_json(CONFIG, c, indent=1, ensure_ascii=False)
        shutil.move(fx, os.path.join(HISTORY_DIR, 'faixas.json.antes-da-0.4')); done.append('faixas.json -> chave "faixas" do config.json')
    if os.path.isdir(SECRETS_DIR):                              # everything in secrets/ is owner-only
        os.chmod(SECRETS_DIR, 0o700)
        for f in os.listdir(SECRETS_DIR): os.chmod(os.path.join(SECRETS_DIR, f), 0o600)
    pc = os.path.join(ROOT, '__pycache__')
    if os.path.isdir(pc): shutil.rmtree(pc, ignore_errors=True)
    return done

def pay_ranges(cfg=None):
    """Pay ranges (formerly faixas.json; now the "pay_ranges" key of config.json)."""
    if cfg is None:
        try: cfg = json.load(open(CONFIG, encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError): cfg = {}
    return cfg.get('pay_ranges') or {}

if __name__ == '__main__':
    import sys
    if '--migrate' in sys.argv or '--migrar' in sys.argv: print('\n'.join(migrate()) or 'nada a migrar')   # --migrar: name up to 0.6.x
    else:
        for n in ('CONFIG', 'PROFILE', 'INSTRUCTIONS', 'RESUME_BASE', 'STATE', 'REPUTATION', 'PIPELINE', 'LESSONS', 'CRITERIA_LOG',
                  'SESSION', 'NOTION_ENV', 'NAMES', 'LINKEDIN_PROFILE', 'RUNNER_DIR', 'JUDGMENTS_DIR'):
            p = globals()[n]; print(f'{n:17s} {"ok " if os.path.exists(p) else "-  "} {p}')
