#!/usr/bin/env python3
"""The job-scout instance on the agent plugin (E4 of docs/agent-plugin-design.md).

job-scout's engine moved over as it was: its scripts live in behaviors/job-search/scripts (scout.py, criteria.py,
matrix.py, application.py...), with their own paths.py and schema.py, and the watch_core they import is this plugin's
(behaviors/job-search/scripts/watch_core only points at scripts/watch_core). Two modules named `paths` and `schema` can
not live in one process, so agent.py runs scout.py in a process of its own:

  agent.py job-scout                  the heavy tick (the runner's call): scout.py, which syncs with Planou and sends the
                                      sign of life `tick` itself. Before it, the Papel tab's manifest is saved for that
                                      heartbeat, read only (no edit from Planou is applied to this instance yet)
  agent.py job-scout --dry | --json | --since 4h | --so vagas
                                      the same, as scout.py takes them; in test mode ("live" not true) always dry
  agent.py job-scout --pending | --resolve pN | --job ID k=v | --pipeline | --board | ...
                                      any other scout.py command, answered by scout.py

The instance name goes to scout.py in $AGENT_INSTANCE (its paths.py resolves the folder with watch_core.config, the same
resolver as this plugin's paths.py). The heavy tick is never shorter than MIN_INTERVAL_S (runner-env).
"""
import os, subprocess, sys

import paths
from watch_core import behavior_names

BEHAVIOR = 'job-search'       # old name: job-scout, still the name of the instance
INSTANCE = 'job-scout'
MIN_INTERVAL_S = 1800        # the LinkedIn account is personal and the site detects automation (the old runner's floor)
SCRIPTS = os.path.join(paths.BEHAVIORS_DIR, BEHAVIOR, 'scripts')
SCOUT = os.path.join(SCRIPTS, 'scout.py')
_TICK_SWITCHES = ('--dry', '--json')
_TICK_OPTIONS = ('--since', '--so', '--only')


def on(cfg):
    """True when the instance has the job-search behavior on (the job-scout engine)."""
    return BEHAVIOR in behavior_names.current(cfg.get('behaviors'))


def not_migrated(cfg):
    """A job-scout folder the migration has not touched yet (no "behaviors"): read as the job-search behavior, the one
    --migrate adds, so agent.py answers for it as scout.py does (the old engine keeps working on its folder)."""
    if paths.NAME == INSTANCE and not cfg.get('behaviors'): cfg['behaviors'] = [BEHAVIOR]
    return cfg


def is_tick(args):
    """True when the arguments ask for a tick (none, or only --dry, --json, --since X, --so X)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in _TICK_SWITCHES: i += 1
        elif a in _TICK_OPTIONS and i + 1 < len(args): i += 2
        elif a.split('=', 1)[0] in _TICK_OPTIONS and '=' in a: i += 1
        else: return False
    return True


def _dry(args):
    return '--dry' in args or any(a == '--since' or a.startswith('--since=') for a in args)


def env():
    return dict(os.environ, AGENT_INSTANCE=paths.NAME)


def read_only(manifest):
    """The Papel tab's manifest with nothing editable: no file content, no catalog of behaviors to turn on. scout.py
    has no handler for agent_docs_changed, so Planou must not offer an edit this instance would leave unapplied."""
    files = []
    for f in (manifest or {}).get('files') or []:
        f = {k: v for k, v in f.items() if k != 'content'}
        f['editable'] = False
        files.append(f)
    return {'files': files}


def save_docs(cfg, manifest):
    """Saves the manifest for the heartbeat of scout.py's Planou tick. Never fails the tick (an AVISO in avisos.log)."""
    try:
        from watch_core import planou
        if not isinstance(cfg.get('planou'), dict): return
        planou.configure_agent(paths.NAME, paths.ROOT)
        if planou.active(): planou.set_docs(read_only(manifest()))
    except Exception as e:
        try:
            d = os.path.join(paths.CACHE_DIR, 'planou'); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'avisos.log'), 'a', encoding='utf-8') as fh:
                fh.write(f'AVISO (planou): arquivos do papel: {type(e).__name__}: {str(e)[:120]}\n')
        except OSError:
            pass


def run(cfg, args, manifest=None):
    """Runs scout.py with `args` for this instance; returns its exit code (2 = FONTE QUEBRADA, as the runner reads it)."""
    args = list(args)
    if is_tick(args):
        if cfg.get('live') is not True and not _dry(args): args.append('--dry')      # test mode: always dry
        if not _dry(args) and manifest: save_docs(cfg, manifest)
    sys.stdout.flush()
    return subprocess.run([sys.executable, SCOUT] + args, env=env()).returncode
