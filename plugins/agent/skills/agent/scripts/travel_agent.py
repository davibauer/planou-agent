#!/usr/bin/env python3
"""The travel-agent instance on the agent plugin (E5 of docs/agent-plugin-design.md).

travel-agent's engine moved over as it was: its scripts live in behaviors/travel-agent/scripts (agent.py, flights.py,
miles.py, compare.py, sheets.py...), with their own paths.py (the only one adapted: it finds the instance folder with
this plugin's watch_core, through behaviors/travel-agent/scripts/watch_core). Two modules named `agent` and `paths` can
not live in one process, so this plugin's agent.py runs travel-agent's in a process of its own:

  agent.py travel-agent                  the heavy tick (the runner's call): travel-agent's tick, with the instance venv's
                                         python (cache/venv, where setup.py installs fast-flights) when there is one.
                                         With "planou" in the config, the thin Planou tick around it: the events before,
                                         then the Papel manifest (read only), the config tools and the sign of life `tick`
                                         (broken_sources = the AGENTE QUEBRADO lines), as `== PLANOU`
  agent.py travel-agent --dry | --trip ID  the same, as travel-agent takes them; in test mode ("live" not true) always dry
  agent.py travel-agent --report | --miles | --compare | --hotels | --points ... | --gmail ... | --booked ...
                                         any other travel-agent command, answered by travel-agent's agent.py

Only travel-agent's own commands pass: its agent.py runs a full tick (hundreds of searches, state saved) for any
argument it does not know, so --pending, --since, --json or a typo are refused here. The instance name goes to the
scripts in $AGENT_INSTANCE. The heavy tick is never shorter than MIN_INTERVAL_S (runner-env), the old runner's floor.
"""
import os, re, subprocess, sys

import paths

BEHAVIOR = 'travel-agent'
MIN_INTERVAL_S = 10800       # each tick is a whole date grid of searches; fares do not move faster (the old runner's floor)
LEGACY_INTERVAL_S = 21600    # the old runner's default heavy tick (6 h)
SCRIPTS = os.path.join(paths.BEHAVIORS_DIR, BEHAVIOR, 'scripts')
ENGINE = os.path.join(SCRIPTS, 'agent.py')
# travel-agent's agent.py commands (its main), each answered without a tick
COMMANDS = ('--report', '--gmail', '--gflights', '--real', '--hotel-price', '--booked', '--timeline', '--done', '--hotels',
            '--dashboard', '--sheet', '--points', '--bonus', '--quote', '--compare', '--award', '--miles')
BROKEN = re.compile(r'^AGENTE QUEBRADO \(([^)]+)\)', re.M)


def on(cfg):
    """True when the instance has the travel-agent behavior on."""
    return BEHAVIOR in (cfg.get('behaviors') or [])


def not_migrated(cfg):
    """A travel-agent folder the migration has not touched yet (no "behaviors"): read as the travel-agent behavior, the
    one --migrate adds, so agent.py answers for it as travel-agent's agent.py does (the old engine keeps its folder)."""
    if paths.NAME == BEHAVIOR and not cfg.get('behaviors'): cfg['behaviors'] = [BEHAVIOR]
    return cfg


def kind(args):
    """'tick' (none, or only --dry and --trip ID), 'query' (one of travel-agent's commands first) or None (refused)."""
    i = 0
    while i < len(args):
        if args[i] == '--dry': i += 1
        elif args[i] == '--trip' and i + 1 < len(args) and not args[i + 1].startswith('-'): i += 2
        else: break
    else:
        return 'tick'
    return 'query' if args and args[0] in COMMANDS else None


def python():
    """The instance venv's python (fast-flights lives there) when it runs, else this one (the old runner's rule)."""
    py = os.path.join(paths.CACHE_DIR, 'venv', 'bin', 'python')
    return py if os.access(py, os.X_OK) else sys.executable


def env():
    return dict(os.environ, AGENT_INSTANCE=paths.NAME)


def broken(out):
    """Names of the broken sources in a tick's output (AGENTE QUEBRADO (<name>): ...), in order, once each."""
    return list(dict.fromkeys(BROKEN.findall(out or '')))


class Planou:
    """The thin Planou tick around travel-agent's tick (it never talked to Planou): what agent.py does for an instance
    without sources, with the Papel manifest read only (no agent_docs_changed handler for this instance yet)."""

    def __init__(self, cfg, manifest=None, tools=None):
        self.cfg, self.manifest, self.tools = cfg, manifest, tools

    def on(self):
        from watch_core import planou
        if not isinstance(self.cfg.get('planou'), dict): return False
        planou.configure_agent(paths.NAME, paths.ROOT)
        return planou.active()

    def before(self):
        from watch_core import planou
        return planou.apply_events(planou.poll(), lambda code, label: False)

    def after(self, broken_sources):
        from watch_core import planou
        import job_scout
        lines = []
        if self.tools:
            try: planou.set_tools(self.tools())
            except Exception as e: lines.append(f'AVISO (planou): ferramentas: {type(e).__name__}: {str(e)[:120]}')
        if self.manifest:
            try: planou.set_docs(job_scout.read_only(self.manifest()))
            except Exception as e: lines.append(f'AVISO (planou): arquivos do papel: {type(e).__name__}: {str(e)[:120]}')
        return lines + planou.heartbeat('tick', broken_sources=broken_sources)


def run(cfg, args, manifest=None, tools=None, output=None):
    """Runs travel-agent's agent.py with `args` for this instance; returns its exit code (2 = AGENTE QUEBRADO, as the
    runner reads it). `manifest()` and `tools()` feed the thin Planou tick; `output(lines)` -> (block, loose lines)."""
    args = list(args)
    k = kind(args)
    if k is None:
        print(f'{paths.NAME}: comando que o travel-agent nao conhece: {" ".join(args)} (os dele: --dry, --trip ID, '
              + ', '.join(COMMANDS) + ')', file=sys.stderr)
        return 2
    cmd = [python(), ENGINE]
    sys.stdout.flush()
    if k == 'query': return subprocess.run(cmd + args, env=env()).returncode
    if cfg.get('live') is not True and '--dry' not in args: args.append('--dry')      # test mode: always dry
    pl, lines = None, []
    if '--dry' not in args and '--trip' not in args:        # a partial or dry tick leaves Planou alone (as --so, --dry)
        try:
            p = Planou(cfg, manifest, tools)
            if p.on(): pl = p; lines += p.before()
        except Exception as e:
            lines.append(f'AVISO (planou): {type(e).__name__}: {str(e)[:160]}')
    r = subprocess.run(cmd + args, env=env(), stdout=subprocess.PIPE, text=True)
    out = r.stdout or ''
    sys.stdout.write(out)
    if pl:
        try: lines += pl.after(broken(out))
        except Exception as e: lines.append(f'AVISO (planou): {type(e).__name__}: {str(e)[:160]}')
    if lines and output:
        block, loose = output(lines)
        if block:
            if out.strip(): print()
            print(block)
        for l in loose: print(l)
    sys.stdout.flush()
    return r.returncode
