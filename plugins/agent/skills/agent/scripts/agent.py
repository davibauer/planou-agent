#!/usr/bin/env python3
"""agent: one CLI for every instance of the agent plugin. The instance name is the agent name (planou,
work-watch-acme...); its folder comes from watch_core.config.agent_root (paths.py).

Usage:
  agent.py                              lists the instances (~/.config/agent/*)
  agent.py <instance>                   heavy tick: sources, hooks, Planou; prints only what is new (test mode = dry)
  agent.py <instance> --dry | --json | --so <source> | --since 4h
  agent.py <instance> --validate        checks the config (exit 1 on errors)
  agent.py <instance> --load            what the session reads, by layer and in precedence order (load_plan): the
                                        rules (the autonomy), the instructions (instructions.md, CONTEXT.md), the
                                        skills (BEHAVIOR.md of each behavior "always"; an "on_demand" one only as an
                                        index line, read when used), then the memory: data/handoff.md (the summary the
                                        previous session left) when it is recent (handoff_file)
  agent.py <instance> --status          the instance at a glance (JSON)
  agent.py <instance> --brief [repo] [--role <behavior>] [--model sonnet|default] [--files "<files>"]
                                        the worker brief of a repo in "repos", with what the role allows (dev_brief.py)
  agent.py <instance> --docs            the manifest of the Papel tab in Planou (files the session reads, instructions.md
                                        and CONTEXT.md editable with their content, the catalog of behaviors and the
                                        reference cases of each behavior), as the heartbeat sends it
  agent.py <instance> --evals [--json]  dry run of the reference cases of the instance's behaviors (evals.py);
                                        --prompt <case> and --grade <case> <answer> for a real run
  agent.py <instance> --public-repos    the public repositories (name, path, owner/name): "public": true in "repos"
                                        plus the old behavior_config.code-review.public_repos (dev_brief.py)
  agent.py <instance> --pending         pending queue (alias --pendentes)
  agent.py <instance> --resolve pN      takes an item out of the queue (alias --resolver)
  agent.py <instance> --runner-env      shell assignments for runner.sh (ROOT, LIVE, INTERVAL, ERR, WAKE, BROKEN_S: the
                                        grouped blocks of wake.py); never fails on a
                                        bad config, it reports it in ERR
  agent.py <instance> --migrate [--dry] moves a legacy instance folder into ~/.config/agent/<instance> (migrate.py)
  agent.py <instance> --undo [--dry]    puts it back (also --migrate --undo)

Work-watch instances (behavior "work-watch", E3 of the one-plugin plan) get work-watch's tick as it was in watch.py: the
same sources and hooks (sources/, hooks/), --pendente pN k=v, --backfill-links, --workspace, the fixed separator
("separador": "fixo") and the Planou tick of planou_tick.py (task list, asks, tools with credentials).

The job-scout instance (behavior "job-scout", E4 of the one-plugin plan) gets job-scout's tick as it was: every command
that is not one of the agent's own (--validate, --load, --status, --docs, --evals, --migrate...) goes to
behaviors/job-search/scripts/scout.py in a process of its own (job_scout.py): the tick and its Planou sync, --pending,
--job, --pipeline... The scout's scripts keep their own paths.py and schema.py, which is why they never share a process
with this one. A tick in test mode ("live" not true) is dry. The heavy tick is never shorter than 30 minutes (runner-env):
the LinkedIn account is personal and the site detects automation.

The travel-agent instance (behavior "travel-agent", E5) does the same with travel-agent's engine
(behaviors/flight-price-watch/scripts/agent.py, in a process of its own: travel_agent.py): its tick and its commands (--report,
--miles, --compare, --gmail...); any other argument is refused, since that engine ticks for what it does not know. It
never talked to Planou: with "planou" in the config the thin Planou tick goes around its tick (events, the read-only Papel
manifest, the config tools, the sign of life `tick`). A tick in test mode is dry; the heavy tick is never shorter than
3 hours (runner-env), the old runner's floor.

The heavy tick of an instance without sources (planou) still talks to Planou: it applies and acknowledges the
answers the light loop already delivered, sends the tools list (the sources plus the config "tools") and the sign of
life `tick`. Nothing is synced as a task list (no source feeds one).

Output strings stay the ones the runner and the session know: `FONTE QUEBRADA (x): ...` (exit 2), `== PLANOU` for what
the person did in Planou, `== PENDENTES`, `== PAPEL MUDOU` / `== PAPEL RECUSADO` for an edit of the Papel tab
(role_edit.py). A Planou failure never prints `AVISO (planou)` (it goes to
cache/planou/avisos.log); a failure longer than 60 minutes prints `FONTE QUEBRADA (planou)`.
"""
import argparse, contextlib, io, json, os, re, shlex, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from watch_core import column_roles                             # noqa: E402
import paths, schema, core, adapters                           # noqa: E402

_INICIO = time.monotonic()      # the runner's `timeout` counts from the launch, so the tick's deadlines do too (PLN0385)


class Contexto:
    def __init__(self, cfg):
        self.cfg = cfg
        self.s, self.res, self.quebrados = {}, {}, []
        self.dry, self.agora, self.so, self.args = True, None, None, None
        self.fontes = {}
        self.live = cfg.get('live') is True


def work_watch(cfg):
    """True for the behavior "work-triage" (old name work-watch; work-watch's tick: planou_tick, workspace, shared material), and for a
    work-watch-<x> instance whose config lists no behaviors (not migrated yet)."""
    import role_edit
    return role_edit.work_watch(cfg)


def load_config():
    """(normalized config, errors, warnings)."""
    cfg = schema.load(paths.CONFIG)
    import job_scout
    job_scout.not_migrated(cfg)                        # the old ~/.config/job-scout, before --migrate: its behavior
    import travel_agent
    travel_agent.not_migrated(cfg)                     # the same for the old ~/.config/travel-agent
    paths.use_workspace(cfg.get('workspace'))
    import compartilhado; compartilhado.configura(cfg)   # shared material -> <workspace>/messages/shared
    video_folders(cfg)
    err, warn = schema.validate(cfg, paths.behavior_file, adapters.existe)
    for f in (paths.INSTRUCTIONS, paths.CONTEXT):
        # the session reads them as text: a NUL byte (a binary file, a bad edit from Planou) is an error
        try:
            with open(f or '', 'rb') as fh:
                if b'\x00' in fh.read(): err.append(f'{os.path.basename(f)}: tem byte NUL (nao e texto); a sessao nao le')
        except OSError:
            pass
    return cfg, err, warn


def video_folders(cfg):
    """"videos_dir"/"archive_dir" of the instance win over $ATA_VIDEOS_DIR and the shared recordings.* of
    ~/.config/watch-core (the same rule as work-watch's watch.py since 0.30.2)."""
    lang = ((cfg.get('behavior_config') or {}).get('meeting-recordings') or {}).get('language')
    if isinstance(cfg.get('videos_dir'), str) or isinstance(cfg.get('archive_dir'), str) or isinstance(lang, str):
        from watch_core import recordings
        recordings.configure(cfg.get('videos_dir'), cfg.get('archive_dir'), lang)
        import workspace
        workspace.VIDEOS = recordings.VIDEOS


PASSAGEIRA = re.compile(r'(?i)time[d ]?out|timed out|connection (refused|reset|aborted)|temporar|errno 11[01]|'
                        r'remote end closed|urlopen error|\b(429|500|502|503|504)\b|curl rc=(7|28|35|52|56)\b')
CREDENCIAL = re.compile(r'(?i)\b(401|403)\b|token|credencial|unauthori[sz]ed|forbidden|invalid_grant|expired|vencid')


def _passageira(err):
    return bool(PASSAGEIRA.search(err or '')) and not CREDENCIAL.search(err or '')


class FonteLenta(BaseException):
    """Raised inside a source that ran past its time budget (PLN0385). A BaseException, so a source's own `except
    Exception` does not swallow it."""


def teto_tick(cfg):
    """The ceiling the runner gives the heavy tick (runner.sh: TEMPO = min(600, interval - 20)), in seconds.
    AGENT_TICK_CEILING_S overrides it, for a caller that gives the tick another `timeout` (the tests)."""
    env = os.environ.get('AGENT_TICK_CEILING_S', '')
    if env.isdigit() and int(env) > 0: return int(env)
    iv = cfg.get('interval_s')
    iv = iv if isinstance(iv, (int, float)) and not isinstance(iv, bool) else 300
    return max(min(600, int(iv) - 20), 10)


def orcamentos(cfg):
    """(per source, whole source loop) in seconds, from the tick ceiling. One source never takes more than half of it,
    and the loop stops starting sources at three quarters of it, leaving the rest for the hooks, Planou and the output.
    "source_budget_s" in the config sets the per-source one (0 turns off only that one; the loop still stops at three
    quarters). A value that is not a number >= 0 (--validate flags it) falls back to the default, never breaks the tick."""
    teto = teto_tick(cfg)
    por = cfg.get('source_budget_s')
    if not isinstance(por, (int, float)) or isinstance(por, bool) or por < 0: por = teto / 2
    return float(por), teto * 0.75


def _aviso_corte(cortes, fim_tick):
    return (f'AVISO (tick): sem tempo para {cortes[0]} (o tick passou de {int(fim_tick - _INICIO)}s); '
            'o resto fica para o proximo tick, os itens das fontes acima ja estao salvos')


def _sai(codigo, cortou):
    """Flushes what was printed (the runner's SIGTERM would drop the buffer) and leaves. After a cut (PLN0385) a worker
    thread of the cut source may still be in a request: the normal exit would join it, so the process leaves at once
    (every save was already synchronous)."""
    sys.stdout.flush(); sys.stderr.flush()
    if cortou:
        import threading
        if any(t.is_alive() and not t.daemon for t in threading.enumerate() if t is not threading.main_thread()):
            os._exit(codigo)
    sys.exit(codigo)


@contextlib.contextmanager
def prazo(segundos):
    """Interrupts the block with FonteLenta after `segundos` (SIGALRM; no-op when <= 0 or off the main thread)."""
    import signal, threading
    if segundos <= 0 or threading.current_thread() is not threading.main_thread():
        yield; return
    def _estoura(*_): raise FonteLenta()
    antes = signal.signal(signal.SIGALRM, _estoura)
    signal.setitimer(signal.ITIMER_REAL, segundos)
    try: yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, antes)


def monta(cfg):
    core.configura(cfg)
    ctx = Contexto(cfg)
    fontes = [adapters.instancia(e, 'Fonte', ctx) for e in cfg.get('sources') or []]
    ctx.fontes = {f.nome: f for f in fontes}
    ganchos = [adapters.instancia(e, 'Gancho', ctx) for e in cfg.get('hooks') or []]
    lembrar = {}
    for f in fontes: lembrar.update(f.lembrar)
    core.LEMBRAR = lembrar
    return ctx, fontes, ganchos


def _captura(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): ok = fn(*a)
    return buf.getvalue().rstrip('\n') if ok else None


# ---------------------------------------------------------------- Planou (heavy tick)

def planou_on(cfg):
    """Configures watch_core.planou for this instance from its own config. True when this tick talks to Planou."""
    from watch_core import planou
    if not isinstance(cfg.get('planou'), dict): return False
    planou.configure_agent(paths.NAME, paths.ROOT)
    return planou.active()


def tools(cfg, fontes, state):
    """The Ferramentas tab: the sources (failing ones with their error) plus the config "tools". Never an env value."""
    from watch_core import planou
    out = planou.source_tools([{'name': f.nome, 'fix': f.conserto or None} for f in fontes], state.get('quebrado') or {})
    for t in cfg.get('tools') or []:
        kind, name = t.get('kind'), str(t.get('name') or '').strip()
        if not name: continue
        out.append(planou.tool(f'{kind}:{name}', 'off' if t.get('off') else 'ok', label=t.get('label') or f'{name} ({kind})',
                               kind='mcp' if kind == 'mcp' else 'other'))
    return out


def planou_before():
    from watch_core import planou
    return planou.apply_events(planou.poll(), lambda code, label: False)


def _path_label(path):
    """Where a file is, only to show: plugin files from the plugin folder, the others with ~ for the home."""
    real, plugin = os.path.realpath(path), os.path.dirname(os.path.dirname(paths.SKILL_DIR))
    if real.startswith(os.path.realpath(plugin) + os.sep):
        label = os.path.basename(plugin) + '/' + os.path.relpath(real, os.path.realpath(plugin))
    else:
        home = os.path.expanduser('~')
        label = '~' + path[len(home):] if path.startswith(home + os.sep) else path
    return label if len(label) <= 200 else '...' + label[-197:]


def _clip(v, n):
    return v if not isinstance(v, str) or len(v) <= n else v[:n - 1].rstrip() + '…'


EVAL_LIMITS = {'id': 80, 'task': 200, 'expected': 20, 'got': 20, 'why': 300}


def _eval_cases(cases, minimum=False):
    """The cases as Planou takes them: text fields only, clipped, one per id (a repeated id is a problem of cases.json,
    already in "problems"), at most 50. Under confidentiality "minimum" only the id and the decisions (no task, no why)."""
    limits = {k: n for k, n in EVAL_LIMITS.items() if not minimum or k not in ('task', 'why')}
    out, seen = [], set()
    for c in cases or []:
        cid = c.get('id') if isinstance(c, dict) else None
        if not isinstance(cid, str) or not cid or len(cid) > EVAL_LIMITS['id'] or cid in seen: continue
        seen.add(cid)
        out.append({k: _clip(c[k], n) for k, n in limits.items() if isinstance(c.get(k), str) and c[k]})
        if len(out) >= 50: break
    return out


def docs_manifest(cfg, now=None):
    """The Papel tab's manifest (Planou "docs"): the files the session reads, in order (the plugin's SKILL.md, then
    load_list: instructions.md, CONTEXT.md, each behavior on, then the options of the behaviors), with size, sha256 and
    date. instructions.md, CONTEXT.md and the options (behavior_config.json) go "editable": true with their content
    (UTF-8, up to 200 KB; not under confidentiality "minimum"), since the runner applies agent_docs_changed
    (role_edit.py); the SKILL.md and each BEHAVIOR.md stay read only (a behavior carries the title and summary of its
    frontmatter, Planou after 0.64.0: watch_core.planou drops them for an older one). "catalog" lists the
    behaviors the instance can turn on (the list of behaviors on is edited from it). Each behavior with reference cases
    carries "evals", its result of `--evals --json` (evals.py, dry)."""
    import drive_probe, evals, hashlib, role_edit
    now = now or datetime.now(timezone.utc)
    minimum = role_edit.minimum(cfg)
    ran_at = now.isoformat(timespec='seconds')
    kinds = {'instrucoes': 'instructions', 'contexto': 'context'}
    todo = [('core', 'SKILL.md', os.path.join(paths.SKILL_DIR, 'SKILL.md'))]
    for label, f in load_list(cfg):
        if label.startswith('comportamento '): todo.append(('behavior', label.split(' ', 1)[1], f))
        elif label in kinds: todo.append((kinds[label], os.path.basename(f or ''), f))
    files = []
    for kind, name, f in todo:
        if not f or not name or drive_probe.stuck(f) or not os.path.isfile(f): continue
        if kind == 'behavior' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,59}', name): continue
        try:
            with open(f, 'rb') as fh: data = fh.read()
            mtime = os.stat(f).st_mtime
        except OSError:
            continue
        item = {'kind': kind, 'name': name, 'path_label': _path_label(f), 'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest(),
                'updated_at': datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec='seconds'), 'editable': False}
        text = role_edit.editable_content(data) if kind in ('instructions', 'context') and not minimum else None
        if text is not None: item.update(editable=True, content=text)
        if kind == 'behavior':
            fm = role_edit.frontmatter(f)
            item.update({k: fm[k] for k in ('title', 'summary') if k in fm})
            try: r = evals.run_behavior(name, paths.behavior_file)
            except Exception: r = None
            if r:
                item['evals'] = {'behavior': name, 'mode': r.get('mode') or 'dry', 'cases': min(r['cases'], 1000),
                                 'passed': min(r['passed'], r['cases'], 1000), 'sha256': r['sha256'], 'ran_at': ran_at,
                                 'failures': _eval_cases(r['failures'], minimum), 'results': _eval_cases(r.get('results'), minimum),
                                 'problems': [] if minimum else
                                 [_clip(p, 300) for p in r['problems'] if isinstance(p, str) and p.strip()][:20]}
        files.append(item)
        if len(files) >= 37: break                 # + lessons, handoff and options: 40 at most
    lessons = _lessons_item(cfg, minimum, now)
    if lessons: files.append(lessons)
    memory = _handoff_item(minimum)
    if memory: files.append(memory)
    files.append(_options_item(cfg, minimum))
    out = {'files': files}
    autonomy = _autonomy(cfg)
    if autonomy and not minimum: out['autonomy'] = autonomy
    cat = role_edit.catalog()
    if cat: out['catalog'] = cat
    return out


def _lessons_item(cfg, minimum, now):
    """The lessons of the Memory layer (PLN0292): this agent's sections of the shared lessons.md from the last days
    (watch_core.lessons.recentes), as a read-only "lessons" file whose content and sha256 are that text, so the Papel
    tab shows each lesson with its state ([aplicada em ...], [proposta], [recusada], [a aplicar]) and the manifest goes
    again when a state changes. Under confidentiality "minimum", or with no lesson in those days, only the file's size
    and date. None when there is no lessons.md. An older Planou refuses the content: watch_core.planou drops it."""
    import hashlib
    from watch_core import lessons
    f = lessons.LICOES_MD
    try:
        with open(f, 'rb') as fh: data = fh.read()
        mtime = os.stat(f).st_mtime
    except OSError:
        return None
    item = {'kind': 'lessons', 'name': 'lessons.md', 'path_label': _path_label(f), 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
            'updated_at': datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec='seconds'), 'editable': False}
    text = '' if minimum else lessons.recentes(paths.agent(cfg), hoje=now.astimezone(lessons.BRT).date())
    if text:
        raw = text.encode('utf-8')
        item.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), content=text)
    return item


HANDOFF_EXCERPT_LINES = 6
HANDOFF_EXCERPT_CHARS = 600


def _handoff_item(minimum):
    """The memory layer of the Papel tab (PLN0257): data/handoff.md, the summary the previous session left, as a read
    only "memory" file with its date and, outside confidentiality "minimum", its first lines (`excerpt`, up to
    HANDOFF_EXCERPT_LINES lines and HANDOFF_EXCERPT_CHARS characters; none when they look like a credential). Sent at
    any age (the date tells how old it is); None when it is missing or empty. Information, not a rule: never editable."""
    import hashlib
    from watch_core import planou
    f = os.path.join(paths.DATA_DIR, 'handoff.md')
    try:
        with open(f, 'rb') as fh: data = fh.read()
        mtime = os.stat(f).st_mtime
    except OSError:
        return None
    if not data.strip(): return None
    item = {'kind': 'memory', 'name': 'handoff.md', 'path_label': _path_label(f), 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
            'updated_at': datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec='seconds'), 'editable': False}
    if minimum: return item
    lines = [' '.join(l.split()) for l in data.decode('utf-8', 'replace').splitlines()]
    lines = [l for l in lines if l and l != '---'][:HANDOFF_EXCERPT_LINES]
    excerpt = '\n'.join(lines)
    if len(excerpt) > HANDOFF_EXCERPT_CHARS: excerpt = excerpt[:HANDOFF_EXCERPT_CHARS - 1].rstrip() + '…'
    if excerpt and not planou.looks_secret(excerpt): item['excerpt'] = excerpt
    return item


AUTONOMY_MAX = 30          # phrases per list, as Planou takes them (AgentDocs.MaxAutonomyItems)


def _autonomy(cfg):
    """The instance's autonomy (config "autonomy": can, ask_first, never), the read only part of the Rules layer of the
    Papel tab (PLN0257). Each list keeps up to AUTONOMY_MAX one-line phrases of up to 200 characters. None without it."""
    a = cfg.get('autonomy') if isinstance(cfg.get('autonomy'), dict) else None
    if not a: return None
    out = {}
    for k in ('can', 'ask_first', 'never'):
        v = [' '.join(p.split())[:200] for p in a.get(k) or [] if isinstance(p, str) and p.strip()]
        out[k] = v[:AUTONOMY_MAX]
    return out if any(out.values()) else None


def _options_item(cfg, minimum):
    """The options of the behaviors (behavior_config of config.json) as the manifest's behavior_config.json: editable as
    JSON with its content (role_edit.options_text), read only under confidentiality "minimum" or above 200 KB. The
    schema of each behavior's options goes in the catalog's `options` (role_edit.catalog, Planou 0.61.0 or later)."""
    import hashlib, role_edit
    data = role_edit.options_text(cfg.get('behavior_config')).encode('utf-8')
    try: mtime = os.stat(paths.CONFIG).st_mtime
    except OSError: mtime = time.time()
    item = {'kind': 'behavior_config', 'name': role_edit.OPTIONS_NAME, 'path_label': _path_label(paths.CONFIG),
            'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'updated_at': datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec='seconds'), 'editable': False}
    text = None if minimum else role_edit.editable_content(data)
    if text is not None: item.update(editable=True, content=text)
    return item


def planou_after(ctx, fontes):
    from watch_core import planou
    agora = datetime.now(timezone.utc)
    lines = []
    try: planou.set_tools(tools(ctx.cfg, fontes, ctx.s), agora)
    except Exception as e: lines.append(f'AVISO (planou): ferramentas: {type(e).__name__}: {str(e)[:120]}')
    lines += send_docs(ctx.cfg, agora)
    if (ctx.cfg.get('planou') or {}).get('focus') is not False:      # Foco do dia: only the keeper writes (PLN0379)
        from watch_core import focus
        lines += focus.keep(now=agora)
    return lines + planou.heartbeat('tick', broken_sources=[f for f, _ in ctx.quebrados], now=agora)


def send_docs(cfg, now):
    """The Papel tab's manifest waits for the tick's heartbeat (thin tick and work-watch's planou_tick alike): built
    from the config on disk when an edit of this tick rewrote it. Returns the AVISO line when it could not be built."""
    from watch_core import planou
    import role_edit
    try: planou.set_docs(docs_manifest(load_config()[0] if role_edit.config_changed() else cfg, now), now)
    except Exception as e: return [f'AVISO (planou): arquivos do papel: {type(e).__name__}: {str(e)[:120]}']
    return []


def planou_output(lines):
    """(block or None, loose lines). AVISO (planou) goes to cache/planou/avisos.log, never to the output."""
    avisos = [l for l in lines if l.startswith('AVISO (planou)')]
    soltas = [l for l in lines if l.startswith('FONTE QUEBRADA (planou)')]
    novas = [l for l in lines if l not in avisos and l not in soltas]
    if avisos:
        try:
            d = os.path.join(paths.CACHE_DIR, 'planou'); os.makedirs(d, exist_ok=True)
            f = os.path.join(d, 'avisos.log')
            old = open(f).read().splitlines()[-499:] if os.path.exists(f) else []
            now = datetime.now().isoformat(timespec='seconds')
            open(f, 'w').write('\n'.join(old + [f'{now}\t{l}' for l in avisos]) + '\n')
        except OSError:
            pass
    return ('\n'.join(['== PLANOU'] + novas) if novas else None), soltas


# ---------------------------------------------------------------- commands that answer and leave

def runner_env(name):
    """Shell assignments for runner.sh. Always exit 0 for a valid name: problems go in ERR."""
    out = {'ROOT': paths.ROOT, 'LIVE': '0', 'INTERVAL': '300', 'ERR': ''}
    try:
        cfg, err, _ = load_config()
        out['LIVE'] = '1' if cfg.get('live') is True else '0'
        if isinstance(cfg.get('interval_s'), int): out['INTERVAL'] = str(cfg['interval_s'])
        import job_scout
        if job_scout.on(cfg): out['INTERVAL'] = str(max(int(out['INTERVAL']), job_scout.MIN_INTERVAL_S))
        import travel_agent
        if travel_agent.on(cfg): out['INTERVAL'] = str(max(int(out['INTERVAL']), travel_agent.MIN_INTERVAL_S))
        import wake                                # PLN0247: blocks that wait for the next wake (runner.sh, wake.py)
        rl = wake.rules(cfg, paths.ROOT)
        out['WAKE'] = '1' if rl.get('hold') or rl.get('latest') else '0'
        if 'broken_repeat_min' in rl: out['BROKEN_S'] = str(60 * rl['broken_repeat_min'])
        if err: out['ERR'] = 'config invalido: ' + '; '.join(err)
    except SystemExit as e:
        out['ERR'] = str(e.code)
    print('\n'.join(f'{k}={shlex.quote(v)}' for k, v in out.items()))


def load_list(cfg):
    """Files of the role, in the order of the manifest: instructions.md, CONTEXT.md, each behavior on (always and
    on_demand alike: the Papel tab lists them all). --load groups them by layer (load_plan)."""
    files = [('instrucoes', paths.INSTRUCTIONS)]
    if paths.CONTEXT and os.path.isfile(paths.CONTEXT): files.append(('contexto', paths.CONTEXT))
    for b in cfg.get('behaviors') or []:
        files.append((f'comportamento {b}', paths.behavior_file(b)))
    return files


PRECEDENCE = ('precedencia: Regras > Instrucoes > Habilidades; a Memoria informa, nao manda. '
              'Em conflito vale a camada de cima.')
LAYER_HEADS = (
    ('rules', '== 1. REGRAS: o nucleo (o SKILL.md do plugin, ja lido) e a autonomia; ninguem sobrepoe'),
    ('instructions', '== 2. INSTRUCOES: o jeito desta instancia; vale sobre as habilidades'),
    ('skill', '== 3. HABILIDADES: ler agora as de sempre; uma sob demanda so quando for usar'),
    ('memory', '== 4. MEMORIA: informacao do que ja aconteceu, nao regra'),
)
AUTONOMY_LABELS = (('can', 'pode sozinho'), ('ask_first', 'pede OK antes'), ('never', 'nunca'))


def _role_limit():
    """The role_limit Planou answered in the last heartbeat (cache/planou/role_limit.json, watch_core.planou.role_limit),
    or None (no restriction, or not known yet)."""
    try:
        with open(os.path.join(paths.CACHE_DIR or '', 'planou', 'role_limit.json')) as f: v = json.load(f).get('role_limit')
    except (OSError, ValueError, AttributeError):
        return None
    return [r for r in v if isinstance(r, str)] if isinstance(v, list) else None


def load_plan(cfg, now=None):
    """--load: what the session reads, by layer in precedence order (PLN0257). Rules: the autonomy lines (the SKILL.md
    itself is already loaded); instructions: instructions.md and CONTEXT.md; skills: each behavior on by its frontmatter
    `layer` (default skill) and `kind` (default always): an always one is read now, an on_demand one goes as an index
    line (name, title, when to use, path) and is read only when used; memory: data/handoff.md when recent. A behavior
    that declares another layer goes in that layer. Returns the lines to print."""
    import role_edit
    by = {layer: [] for layer, _ in LAYER_HEADS}
    a = _autonomy(cfg) or {}
    for k, label in AUTONOMY_LABELS:
        if a.get(k): by['rules'].append(f'autonomia {label}: ' + '; '.join(a[k]))
    exists = lambda f: '' if f and os.path.isfile(f) else '  (nao existe)'
    items = load_list(cfg)
    every = column_roles.all_roles(cfg)
    if every:       # PLN0368: every column role's behavior, on or not; review, QA and release read on demand
        on = {label.split(' ', 1)[1] for label, _ in items if label.startswith('comportamento ')}
        items += [(f'comportamento {b}', paths.behavior_file(b)) for _, b in column_roles.CATALOG if b not in on]
        gaps = column_roles.gaps(cfg)
        limit = _role_limit()
    for label, f in items:
        if not label.startswith('comportamento '):
            by['instructions'].append(f'{label}: {f}{exists(f)}')
            continue
        name = label.split(' ', 1)[1]
        m = role_edit.load_meta(f) if f and os.path.isfile(f) else {'layer': 'skill', 'kind': 'always'}
        if every and name in column_roles.ON_DEMAND:
            role = column_roles.ROLE_OF[name]
            m = {**m, 'kind': 'on_demand', 'when': f'tarefa da coluna de {role} (`$A --brief <repo> --role {name}`)'}
            if limit is not None and role not in limit and name not in (cfg.get('behaviors') or []):
                continue            # the person restricted this agent in Planou: a prompt it will not need
            if role in gaps:
                m['when'] += f'; INDISPONIVEL neste computador: {gaps[role][0]} ({gaps[role][1]})'
        if m['kind'] == 'on_demand':
            hint = m.get('when') or m.get('summary') or ''
            title = m.get('title') or name
            by[m['layer']].append(f'sob demanda {name}: {f}{exists(f)}  ({title}; ler quando: {hint})' if hint
                                  else f'sob demanda {name}: {f}{exists(f)}  ({title})')
        else:
            by[m['layer']].append(f'{label}: {f}{exists(f)}')
    h = handoff_file(now)
    if h: by['memory'].append(f'passagem: {h[0]}  (resumo da sessao anterior, gravado em {h[1]})')
    out = [PRECEDENCE]
    for layer, head in LAYER_HEADS:
        if by[layer] or layer in ('rules', 'instructions'): out += [head, *by[layer]]
    return out


HANDOFF_MAX_AGE_H = 36     # a daily rotation plus a working day of slack; older ones describe a state long gone


def handoff_file(now=None):
    """data/handoff.md, the summary a session writes before it is retired (daily rotation of "rotate": true, or
    `team <agent> new`), so the next session picks up from it. It goes last in --load when it is recent: modified in
    the last HANDOFF_MAX_AGE_H hours. None when it is missing, empty or older. It is data, not a role file: the Papel
    tab gets it only in its Memory layer, read only, with the date and the first lines (_handoff_item)."""
    f = os.path.join(paths.DATA_DIR, 'handoff.md')
    try: st = os.stat(f)
    except OSError: return None
    now = time.time() if now is None else now
    if not st.st_size or now - st.st_mtime > HANDOFF_MAX_AGE_H * 3600: return None
    return f, datetime.fromtimestamp(st.st_mtime).strftime('%d/%m %H:%M')


def status(cfg, err):
    rd = paths.RUNNER_DIR
    def rd_file(n):
        try: return open(os.path.join(rd, n)).read().strip()
        except OSError: return None
    pid = rd_file('runner.pid')
    alive = bool(pid and pid.isdigit() and os.path.exists(f'/proc/{pid}'))
    return {'instance': paths.NAME, 'root': paths.ROOT, 'config': paths.CONFIG, 'live': cfg.get('live') is True,
            'interval_s': cfg.get('interval_s'), 'sources': [e.get('type') for e in cfg.get('sources') or []],
            'hooks': [e.get('type') for e in cfg.get('hooks') or []], 'behaviors': cfg.get('behaviors'),
            'tools': [f'{t.get("kind")}:{t.get("name")}' for t in cfg.get('tools') or [] if isinstance(t, dict)],
            'planou': {k: v for k, v in (cfg.get('planou') or {}).items() if k in ('project', 'task_queue', 'conversation', 'confidentiality', 'agent')},
            'behavior_config': cfg.get('behavior_config') or {},
            'errors': err, 'runner': {'pid': pid if alive else None, 'last_poll': rd_file('ultimo_poll'),
                                      'last_empty_tick': rd_file('ultimo_vazio'), 'next_heavy': rd_file('next_heavy')}}


def main(argv=None):
    global _INICIO
    _INICIO = time.monotonic()                   # also when a caller imports agent and runs main() later
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith('-'):
        insts = paths.instances()
        print('uso: agent.py <instancia> [--dry] [--json] [--so FONTE] [--since 4h] [--validate] [--load] [--status] [--pending] ...')
        print('instancias: ' + (', '.join(insts) if insts else '(nenhuma em ~/.config/agent)'))
        sys.exit(0 if argv[:1] in (['-h'], ['--help']) else 2)
    paths.instance(argv[0])
    rest = argv[1:]
    if rest == ['--runner-env']: return runner_env(argv[0])
    if rest[:1] in (['--migrate'], ['--undo']):
        import migrate
        return migrate.cli(paths.NAME, rest)
    try: cfg, err, warn = load_config()
    except schema.ConfigError as e: sys.exit(f'{paths.NAME}: {e.code}')
    if rest == ['--validate']:
        for w in warn: print('aviso:', w)
        for e in err: print('ERRO:', e)
        print(f'{paths.NAME}: ' + ('config com erro' if err else f'config ok ({paths.CONFIG})'))
        alias = (cfg.get('planou') or {}).get('agent') if isinstance(cfg.get('planou'), dict) else None
        if not err and alias and alias != paths.NAME:
            print(f'{paths.NAME}: no Planou fala como {alias} ("planou.agent")')
        sys.exit(1 if err else 0)
    if rest == ['--status']:
        print(json.dumps(status(cfg, err), ensure_ascii=False, indent=1)); return
    if rest == ['--load']:
        if err: sys.exit(f'{paths.NAME}: config com erro (agent.py {paths.NAME} --validate)')
        print('\n'.join(load_plan(cfg)))
        return
    if rest[:1] == ['--brief']:
        if err: sys.exit(f'{paths.NAME}: config com erro (agent.py {paths.NAME} --validate)')
        import dev_brief
        return dev_brief.cli(cfg, rest[1:], paths.behavior_file)
    if rest == ['--docs']:                         # the Papel tab's manifest the heartbeat sends (docs_manifest)
        print(json.dumps(docs_manifest(cfg), ensure_ascii=False, indent=1)); return
    if rest[:1] == ['--evals']:                    # reference cases of the roles, dry (evals.py)
        import evals
        sys.exit(evals.cli(rest[1:], paths.behavior_file, [b for b in cfg.get('behaviors') or [] if isinstance(b, str)]))
    if rest == ['--public-repos']:
        import dev_brief
        return dev_brief.public_cli(cfg)
    if '--workspace' in rest:                      # the instance workspace (workspace.py); answers and leaves
        import workspace
        return workspace.cli(rest[rest.index('--workspace') + 1:])
    if err: sys.exit(f'{paths.NAME}: config com erro: ' + '; '.join(err))
    import job_scout
    if job_scout.on(cfg): sys.exit(job_scout.run(cfg, rest, lambda: docs_manifest(cfg)))
    import travel_agent
    if travel_agent.on(cfg):
        sys.exit(travel_agent.run(cfg, rest, lambda: docs_manifest(cfg), lambda: tools(cfg, [], {}), planou_output))
    ctx, fontes, ganchos = monta(cfg)
    ww = work_watch(cfg)

    ap = argparse.ArgumentParser(prog=f'agent.py {paths.NAME}')
    ap.add_argument('--dry', action='store_true'); ap.add_argument('--json', action='store_true')
    ap.add_argument('--so', choices=[f.nome for f in fontes])
    ap.add_argument('--since', help='recorte fixo (30m, 4h, 2d, 3du, ISO) em vez dos cursores; implica --dry')
    ap.add_argument('--pending', '--pendentes', action='store_true', help='lista a fila de pendencias e sai')
    ap.add_argument('--resolve', '--resolver', metavar='pN', help='marca a pendencia como resolvida (manual) e sai')
    ap.add_argument('--pendente', nargs='+', metavar=('pN', 'k=v'), help='anota a pendencia: status=ignorar|aberta, nota="..."')
    ap.add_argument('--backfill-links', action='store_true',
                    help='manutencao: acha a mensagem das pendencias antigas de Slack/Teams sem link e grava o link; com --dry so mostra')
    for x in ganchos + fontes: x.args(ap)
    so_top = []
    if cfg.get('code') and '--top' not in ap._option_string_actions:   # tasks engine inside an instance adapter
        import importlib
        so_top = [importlib.import_module('adapters.tarefas').SoTop(None, ctx)]
        so_top[0].args(ap)
    a = ap.parse_args(rest)
    ctx.args, ctx.so = a, a.so

    # ---- commands that answer and leave ----
    ctx.s = core.load_state()
    for x in ganchos + fontes + so_top:
        if x.cli(a, ctx): return
    if a.backfill_links:
        import backfill_links
        return backfill_links.cli(ctx.s, fontes, a.dry)
    if a.pending:
        refs = {q: f.linha_ref for f in fontes for q in f.lembrar}
        core.lista_pendencias(ctx.s, lambda p: refs.get(p['fonte'], core.linha_ref_padrao)(p)); return
    if a.resolve or a.pendente:
        chave = a.resolve or a.pendente[0]
        p = next((p for p in core.pendencias(ctx.s) if p['chave'] == chave), None)
        if not p: sys.exit(f'pendencia {chave} nao existe (ver --pending)')
        if a.resolve: core.resolve(p, datetime.now(timezone.utc), 'manual')
        else:
            for kv in a.pendente[1:]:
                k, _, v = kv.partition('=')
                if k == 'status' and v not in ('ignorar', 'aberta'): sys.exit('status: ignorar|aberta')
                p[k] = v
            for g in ganchos: g.ao_anotar(ctx, p)
        core.save_state(ctx.s)
        print(f'{p["chave"]} [{p["fonte"]}] {p["quem"]} — {p["assunto"]}: {p["status"]}'); return

    # ---- tick ----
    dry = a.dry or bool(a.since) or not core.LIVE          # test mode: always dry
    desde = core.parse_since(a.since) if a.since else None
    s = ctx.s
    sel = [f for f in fontes if not a.so or f.nome == a.so]
    res, quebrados = {}, []
    ctx.res, ctx.quebrados, ctx.dry = res, quebrados, dry
    espera = float(cfg.get('retry_s', 5))
    import copy, drive_probe
    from watch_core.slowfs import Stuck
    # PLN0385: a source slower than the tick ceiling got the whole tick killed before the one save at the end, freezing
    # every source's cursor. Now each source has a time budget (past it: rolled back to its state before this tick, and
    # FONTE QUEBRADA (x): FONTE LENTA, throttled by the runner like any broken source) and the state is saved after each
    # source, so a later one that still gets killed never freezes the ones before it.
    por_fonte, laco = orcamentos(cfg)
    fim_laco = _INICIO + laco
    cortou = False
    for f in sel:
        try: preso = drive_probe.stuck_any(f.caminhos())     # PLN0236: a dead Windows drive must not hang the tick
        except Exception: preso = None
        if preso:
            quebrados.append((f.nome, drive_probe.message(preso))); continue
        limite = fim_laco - time.monotonic()
        if limite < 1:                                            # also with source_budget_s 0: only the per-source one is off
            quebrados.append((f.nome, f'FONTE LENTA: sem tempo no tick (as fontes antes dela levaram {int(laco)}s); fica para o proximo'))
            continue
        seg = min(por_fonte, limite) if por_fonte > 0 else 0      # 0: this source runs without an alarm
        prazo_fonte = time.monotonic() + seg
        antes = copy.deepcopy(s) if seg > 0 else None
        base_antes = core._BASE.get(id(s))                       # a save inside the source moves it; rolled back too
        for tentativa in (1, 2):
            try:
                with prazo(max(prazo_fonte - time.monotonic(), 0.01) if seg > 0 else 0):
                    r = f.tick(s, desde, dry)
                if r is not None: res[f.nome] = r
                break
            except FonteLenta:                                   # never retried; its half-done changes are undone
                s.clear(); s.update(antes)                       # same object: core._BASE is keyed by id(s)
                if base_antes: core._BASE[id(s)] = base_antes    # else an action merged in by that save reads as dropped
                quebrados.append((f.nome, f'FONTE LENTA: passou de {int(seg)}s e foi pulada neste tick; '
                                          'retoma do cursor do ultimo tick que terminou'))
                cortou = True
                break
            except SystemExit as e:
                erro = str(e.code)
            except Stuck as e:                                   # PLN0245: a read on a drive ran out of time; never retried
                quebrados.append((f.nome, str(e))); break
            except Exception as e:
                erro = f'{type(e).__name__}: {e}'
            if tentativa == 1 and espera >= 0 and _passageira(erro) and (seg <= 0 or prazo_fonte - time.monotonic() > espera + 1):
                time.sleep(espera); continue
            quebrados.append((f.nome, erro + (' (2 tentativas)' if tentativa == 2 else '')))
            break
        if not dry: core.save_state(s)                           # this source's cursor survives a later kill
    if not dry:
        qb = s.setdefault('quebrado', {}); dq = dict(quebrados)
        for f in sel:
            if f.nome in dq: qb.setdefault(f.nome, {'erro': dq[f.nome], 'desde': datetime.now(timezone.utc).isoformat()})['erro'] = dq[f.nome]
            else: qb.pop(f.nome, None)
        core.save_state(s)                        # the cursors are on disk; what follows has a deadline (PLN0385)
    # PLN0385: the cursors are saved before the output is printed, so a tick killed in the hooks or in Planou lost the
    # new items for good. The two phases below run under the time left of the ceiling (less a tenth for the output);
    # past it the rest is skipped with AVISO (tick) and the output is printed all the same.
    fim_tick = _INICIO + teto_tick(cfg) * 0.9
    cortes = []
    pl, pl_ww, pl_lines = False, None, []
    lembrar, resolvidas, valores = [], [], {}
    ctx.agora = datetime.now(timezone.utc)
    ativos = [g for g in ganchos if not (g.so_sem_filtro and a.so) and all(r in res for r in g.requer)]

    def _fase(nome, corpo):
        if cortes: return
        resta = fim_tick - time.monotonic()
        if resta <= 0: cortes.append(nome); return
        try:
            with prazo(resta): corpo()
        except FonteLenta: cortes.append(nome)

    def _antes():
        # Planou: what the person did there lands in the state before the hooks. A work-watch instance keeps
        # work-watch's Planou tick (planou_tick.py: its task list, asks and tools go up after the hooks); any other
        # instance the thin one.
        nonlocal pl, pl_ww, lembrar, resolvidas
        if not dry and not a.so:
            try:
                from watch_core import planou
                import role_edit                   # the Papel tab's edits (agent_docs_changed), applied by apply_events
                planou.set_docs_handler(role_edit.handler(cfg))
                if ww:
                    if cfg.get('planou'):
                        import planou_tick
                        if planou_tick.liga(cfg): pl_ww = planou_tick; pl_lines.extend(pl_ww.before(s))
                else:
                    pl = planou_on(cfg)
                    if pl: pl_lines.extend(planou_before())
            except Exception as e:
                pl_lines.append(f'AVISO (planou): {type(e).__name__}: {str(e)[:160]}')
        if not dry:
            cruzadas = [r for g in ativos for r in (g.cruza(ctx) or [])]
            fontes_ok = [q for f in sel if f.nome in res for q in f.lembrar]     # broken source: no reminder without evidence
            lembrar = core.lembretes_devidos(s, ctx.agora, fontes_ok)
            for p in lembrar: p['lembretes'] += 1; p['ultimo_lembrete'] = ctx.agora.isoformat()
            resolvidas = [r for f in sel if f.nome in res for r in f.resolvidas(res[f.nome])] + cruzadas
        for g in ativos:
            if dry and not g.roda_em_dry: continue
            valores[g.nome] = g.run(ctx)

    _fase('ganchos', _antes)
    if not dry:
        s['ultima'] = ctx.agora.isoformat(); core.save_state(s)
    if a.json:
        out = {'res': res, 'quebrados': quebrados, 'lembrar': lembrar}
        for g in ganchos:
            if g.json_key: out[g.json_key] = valores.get(g.nome)
        if cortes: out['avisos'] = [_aviso_corte(cortes, fim_tick)]
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        _sai(2 if quebrados else 0, cortou or bool(cortes))
    textos = {g.nome: g.texto(valores[g.nome]) for g in ganchos if valores.get(g.nome) is not None}
    for g in ganchos:                            # a hook may move another's block (alertas_azure: nao_acordar_junto)
        if valores.get(g.nome) is not None and hasattr(g, 'ajusta_textos'): g.ajusta_textos(ctx, valores, textos)
    blocos, pecas = [], []                      # pecas: (fixed, block) in output order
    fixo_padrao = cfg.get('separador') == 'fixo'

    def _fontes(depois):
        for f in sel:
            if f.nome in res and (f.cfg.get('posicao') == 'depois') == depois:
                b = _captura(f.imprime, res[f.nome])
                if b: blocos.append(b)
                fixo = f.cfg.get('separador_fixo', fixo_padrao)
                if b or fixo: pecas.append((fixo, b))

    def _add(b):
        blocos.append(b); pecas.append((False, b))

    _fontes(False)
    for g in ganchos:
        if g.posicao == 'fontes' and textos.get(g.nome): _add(textos[g.nome])
    b = _captura(core.imprime_pendencias, s, lembrar, resolvidas)
    if b: _add(b)
    _fontes(True)                                 # a source with "posicao": "depois" comes after == PENDENTES
    for g in ganchos:
        if g.posicao == 'depois' and textos.get(g.nome): _add(textos[g.nome])
    def _depois():
        for g in ativos:
            for x in (g.publica(ctx, list(blocos)) or []):
                if x: _add(x)
        try:
            if pl_ww:                              # the manifest first: after() ends with the heartbeat that takes it
                pl_lines.extend(send_docs(ctx.cfg, datetime.now(timezone.utc)) + pl_ww.after(ctx, ganchos))
            elif pl: pl_lines.extend(planou_after(ctx, fontes))
        except Exception as e:
            pl_lines.append(f'AVISO (planou): {type(e).__name__}: {str(e)[:160]}')

    if not dry:
        _fase('publicacao e Planou', _depois)
        core.save_state(s)
    b, soltas = planou_output(pl_lines) if pl_lines else (None, [])
    if b: _add(b)
    if not dry and not a.so:
        import role_edit
        b = role_edit.block()                     # == PAPEL MUDOU / == PAPEL RECUSADO: what to read again
        if b: _add(b)
    # separator: one blank line between blocks; a source with the fixed separator (the old watchers printed the line
    # before knowing whether the source had anything) keeps the line even without a block
    algo, saida = False, io.StringIO()
    for fixo, b in pecas:
        if algo and (b or fixo): saida.write('\n')
        if b: saida.write(b + '\n'); algo = True
    print(saida.getvalue(), end='')
    for f, erro in quebrados: print(f'FONTE QUEBRADA ({f}): {erro}')
    for l in soltas: print(l)
    if cortes: print(_aviso_corte(cortes, fim_tick))
    fim = [textos[g.nome] for g in ganchos if g.posicao == 'fim' and textos.get(g.nome)]
    if fim:
        if algo or quebrados: print()
        print('\n\n'.join(fim))
    _sai(2 if quebrados else 0, cortou or bool(cortes))


if __name__ == '__main__':
    main()
