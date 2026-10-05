#!/usr/bin/env python3
"""The travel-agent instance on the agent plugin (E5 of docs/agent-plugin-design.md).

travel-agent's engine moved over as it was: its scripts live in behaviors/flight-price-watch/scripts (agent.py, flights.py,
miles.py, compare.py, sheets.py...), with their own paths.py (the only one adapted: it finds the instance folder with
this plugin's watch_core, through behaviors/flight-price-watch/scripts/watch_core). Two modules named `agent` and `paths` can
not live in one process, so this plugin's agent.py runs travel-agent's in a process of its own:

  agent.py travel-agent                  the heavy tick (the runner's call): travel-agent's tick, with the instance venv's
                                         python (cache/venv, where setup.py installs fast-flights) when there is one.
                                         With "planou" in the config, the thin Planou tick around it: the events before,
                                         then the Papel manifest (read only), the config tools and the sign of life `tick`
                                         (broken_sources = the AGENTE QUEBRADO lines), as `== PLANOU`
  agent.py travel-agent --dry | --trip ID  the same, as travel-agent takes them; in test mode ("live" not true) always dry
  agent.py travel-agent --report | --miles | --compare | --hotels | --points ... | --gmail ... | --booked ...
                                         any other travel-agent command, answered by travel-agent's agent.py
  agent.py travel-agent --plan-cards [--dry]
                                         one Planou card per trip plan (PLN0374, "plan_cards" in the config): synced now,
                                         or with --dry (or in test mode, or without Planou) only printed as JSON

Plan cards: with "plan_cards" in the config, a live full tick and the steps that bring new prices (--done expedia,
--done hoteis) sync the cards the engine builds (behaviors/flight-price-watch/scripts/cards.py). A closed plan goes up
only while Planou still has its card open; a plan taken out of the config closes its card ("Sem ação").

Only travel-agent's own commands pass: its agent.py runs a full tick (hundreds of searches, state saved) for any
argument it does not know, so --pending, --since, --json or a typo are refused here. The instance name goes to the
scripts in $AGENT_INSTANCE. The heavy tick is never shorter than MIN_INTERVAL_S (runner-env), the old runner's floor.
"""
import json, os, re, subprocess, sys

import paths
from watch_core import behavior_names

BEHAVIOR = 'flight-price-watch'   # old name: travel-agent, still the name of the instance
INSTANCE = 'travel-agent'
MIN_INTERVAL_S = 10800       # each tick is a whole date grid of searches; fares do not move faster (the old runner's floor)
LEGACY_INTERVAL_S = 21600    # the old runner's default heavy tick (6 h)
SCRIPTS = os.path.join(paths.BEHAVIORS_DIR, BEHAVIOR, 'scripts')
ENGINE = os.path.join(SCRIPTS, 'agent.py')
# travel-agent's agent.py commands (its main), each answered without a tick
COMMANDS = ('--report', '--gmail', '--gflights', '--real', '--hotel-price', '--booked', '--timeline', '--done', '--hotels',
            '--dashboard', '--sheet', '--points', '--bonus', '--quote', '--compare', '--award', '--miles', '--plan-cards')
CARD_STEPS = ('expedia', 'hoteis')     # --done <step> after new prices: the plan cards are synced right after
CARDS_FILE = 'plan_cards.json'         # cache/planou: {code: {title, closed_on}} of the plan cards sent
REMOVED = 'Plano tirado da configuração do agente de viagem: card fechado.'
BROKEN = re.compile(r'^AGENTE QUEBRADO \(([^)]+)\)', re.M)


def on(cfg):
    """True when the instance has the flight-price-watch behavior on (the travel-agent engine)."""
    return BEHAVIOR in behavior_names.current(cfg.get('behaviors'))


def not_migrated(cfg):
    """A travel-agent folder the migration has not touched yet (no "behaviors"): read as the flight-price-watch behavior, the
    one --migrate adds, so agent.py answers for it as travel-agent's agent.py does (the old engine keeps its folder)."""
    if paths.NAME == INSTANCE and not cfg.get('behaviors'): cfg['behaviors'] = [BEHAVIOR]
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


def cards_on(cfg):
    """Plan cards on: "plan_cards" true (or an object) in the config, live and with "planou"."""
    return bool(cfg.get('plan_cards')) and cfg.get('live') is True and isinstance(cfg.get('planou'), dict)


def plan_items():
    """{code: item} of the engine's --plan-cards (read only)."""
    r = subprocess.run([python(), ENGINE, '--plan-cards'], env=env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode: raise RuntimeError(f'--plan-cards saiu com {r.returncode}: {(r.stderr or "").strip()[-160:]}')
    return json.loads(r.stdout or '{}')


def card_batch(items, known, opened, today):
    """What goes up and what is remembered: the open plans always; a closed plan only while Planou still has its card
    open (its close date fixed the first time it was seen closed); a plan known before and gone from the config closes as
    "Sem ação". Returns (batch, known after)."""
    items = dict(items)
    for code, prev in known.items():
        if code not in items:
            items[code] = {'titulo': prev.get('title') or code, 'status': 'Sem ação', 'planou_description': REMOVED,
                           'origem': 'travel-agent', 'area': 'viagem'}
    batch, after = {}, {}
    for code, it in items.items():
        if it.get('status') == 'A fazer':
            batch[code] = it
            after[code] = {'title': it.get('titulo')}
        elif code in opened:
            it = {**it, 'concluida': it.get('concluida') or (known.get(code) or {}).get('closed_on') or today}
            batch[code] = it
            after[code] = {'title': it.get('titulo'), 'closed_on': it['concluida']}
    return batch, after


def sync_cards(cfg, now=None):
    """Syncs one card per trip plan; returns the lines for the output (empty when all went well). The text is the
    user's own trip: with "plan_cards": {"detail": true} it goes whole even when the instance is at "title"; "minimum"
    always wins."""
    from watch_core import planou
    import datetime as dt
    items = plan_items()
    known = planou._load(CARDS_FILE, {})
    batch, after = card_batch(items, known, planou.open_codes(), dt.date.today().isoformat())
    if not batch: return []
    opts = cfg.get('plan_cards') if isinstance(cfg.get('plan_cards'), dict) else {}
    conf = planou._S['confidentiality']
    if opts.get('detail') is True and conf != 'minimum': planou._S['confidentiality'] = 'detail'
    try:
        lines = planou.sync(batch, now)
    finally:
        planou._S['confidentiality'] = conf
    still = planou.open_codes()
    planou._save(CARDS_FILE, {c: v for c, v in after.items() if 'closed_on' not in v or c in still})
    return lines


def cards_after(cfg, lines_out):
    """sync_cards behind the Planou check; any failure becomes one AVISO line."""
    try:
        if Planou(cfg).on(): lines_out += sync_cards(cfg)
    except Exception as e:
        lines_out.append(f'AVISO (planou): cards dos planos: {type(e).__name__}: {str(e)[:160]}')
    return lines_out


def plan_cards_cmd(cfg, args):
    """--plan-cards: sync now; --dry, test mode or no Planou: the engine's JSON only."""
    if '--dry' in args or not cards_on(cfg) or not Planou(cfg).on():
        sys.stdout.flush()
        return subprocess.run([python(), ENGINE, '--plan-cards'], env=env()).returncode
    lines = cards_after(cfg, [])
    for l in lines: print(l)
    if not any(l.startswith('AVISO') for l in lines): print('cards dos planos sincronizados com o Planou')
    return 1 if any(l.startswith('AVISO') for l in lines) else 0


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
    if k == 'query':
        if args[0] == '--plan-cards': return plan_cards_cmd(cfg, args)
        rc = subprocess.run(cmd + args, env=env()).returncode
        if rc == 0 and args[0] == '--done' and args[1:2] and args[1] in CARD_STEPS and cards_on(cfg):
            for l in cards_after(cfg, []): print(l)
        return rc
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
        if cards_on(cfg): cards_after(cfg, lines)
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
