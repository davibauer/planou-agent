"""Canary rollout of the team's plugins (PLN0056): a new version runs first in ONE instance, the others only after it
passed one full heavy tick.

Why: the runners call the plugin's Python from the shared copy on disk (the skill links point at it) on every heavy
tick, so a `git pull` there used to reach every agent at once (28/09/2026: a regression in the recordings source took
the four work-watch instances down together). With the team file `~/.config/team/rollout.json` naming a canary for a
plugin, a runner never runs the copy on disk directly: it runs a snapshot of ONE version of the plugin
(`~/.cache/team/plugins/<plugin>@<version>/`), chosen at start:

  - the canary runs the version on disk (unless that version was refused);
  - every other runner runs the version on disk only once it is "released", else the last released one.

After each heavy tick (a safe point: nothing is half done) the runner asks `tick`:

  - canary on a version still "canary": that tick is the test. A tick that ended with an error, a FONTE QUEBRADA of a
    source that was fine before the upgrade, a Traceback or a new kind of line in planou.err refuses the version
    (the runner stops and raises a high alert in Planou); anything else releases it. Planou being down is not the
    version's fault (PLN0131: 29/09/2026 a Planou deploy restarting its API refused job-scout 0.40.2 for
    "0 network [Errno 111] Connection refused" lines): new planou.err lines that only say Planou was unreachable
    (status 0 network, 502/503/504, connection refused/reset, timeout) and a FONTE QUEBRADA (planou) with such a cause
    neither refuse nor release: the decision waits for the next heavy tick (the version stays "canary", the others
    stay on the released one). Each such tick asks Planou a plain GET (/api/version on the host of its base_url). With no new network line in
    the next tick, the usual test decides; network lines in two heavy ticks in a row with Planou answering after both
    refuse the version (the old one did not fail that way with Planou up); while Planou does not answer, it waits
    again, with no limit;
  - any runner whose chosen version (the rule above) differs from the one it runs: ACTION=relaunch. The runner ends
    with a `== RELANCAR` block and the SESSION does the usual stop and start (the runner is a child of the session;
    the session relaunches after every wake anyway, re-reads the new SKILL.md, and the start picks the snapshot).

Nothing here touches the shared copy: a snapshot is a copy of the plugin folder made while that version is on disk (or,
for an older version, `git archive` of its tag `<plugin>--v<version>`). Without rollout.json, or with no canary for the
plugin, nothing changes: the runner runs the copy on disk as before.

rollout.json (only the canary and this CLI write it; flock + atomic rename):
  {"canary": {"work-watch": "work-watch-<instance>", "job-scout": "job-scout", "agent": "planou-dev"},
   "plugins": {"work-watch": {"versions": {"0.33.0": {"state": "released", "at": "...", "by": "canary work-watch-x"},
                                           "0.34.0": {"state": "canary", "agent": "work-watch-x", "at": "..."}}}}}
  state: "canary" (canario rodando), "released" (liberada), "refused" (recusada, with "reason").

CLI (the user and the chief-of-staff):
  python3 -m watch_core.rollout status [--json]
  python3 -m watch_core.rollout canary <plugin> <agent>        # choose the canary instance of a plugin
  python3 -m watch_core.rollout release <plugin> <version>     # release by hand (e.g. a refusal that was Planou down)
  python3 -m watch_core.rollout refuse <plugin> <version> [--reason ...]
Runner protocol (one line each; the runner reads PIN=/STOP= and ACTION=/ALERT=):
  start --scripts <dir> --agent <name> --runner-dir <dir>
  tick  --scripts <dir> --live <plugin dir> --agent <name> --runner-dir <dir> --rc <n> --tick <file>
"""
import argparse, fcntl, json, os, re, shutil, subprocess, sys, tempfile, time

STATES_PT = {'canary': 'canario rodando', 'released': 'liberada', 'refused': 'recusada'}
TEST_ENV = 'WATCH_CORE_TEST'
IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc', 'tests', 'research', '.pytest_cache')
BASELINE = 'rollout.baseline.json'
VERSION_FILE = 'runner.version'


def _guard(p):
    if os.environ.get(TEST_ENV) == '1':
        tmp = os.path.realpath(tempfile.gettempdir())
        if os.path.commonpath([os.path.realpath(p), tmp]) != tmp:
            raise RuntimeError(f'{TEST_ENV}=1 but rollout points at {p} (outside {tmp}): refusing to touch the real files')
    return p


def path():
    """~/.config/team/rollout.json ($XDG_CONFIG_HOME), resolved now."""
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return _guard(os.path.join(base, 'team', 'rollout.json'))


def cache_dir():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return _guard(os.path.join(base, 'team', 'plugins'))


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def vkey(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r'[.\-+]', str(v or '0')))


def plugin_dir(start):
    """The plugin folder above `start` (the first one holding .claude-plugin/plugin.json)."""
    d = os.path.realpath(start)
    while True:
        if os.path.isfile(os.path.join(d, '.claude-plugin', 'plugin.json')): return d
        up = os.path.dirname(d)
        if up == d: return None
        d = up


def plugin_info(pdir):
    """(name, version) of a plugin folder, or (None, None)."""
    try:
        j = json.loads(_read(os.path.join(pdir, '.claude-plugin', 'plugin.json')))
        return j.get('name'), j.get('version')
    except (OSError, ValueError, TypeError):
        return None, None


# ---------------------------------------------------------------- rollout.json

def load():
    try: return json.loads(_read(path()) or 'null')
    except ValueError: return None


def update(fn):
    """fn(data) under an exclusive lock; the file is rewritten atomically. Returns fn's result."""
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + '.lock', 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        data = load() or {}
        data.setdefault('canary', {}); data.setdefault('plugins', {})
        before = json.dumps(data, sort_keys=True)
        res = fn(data)
        if json.dumps(data, sort_keys=True) == before and os.path.exists(p): return res   # unchanged: never rewrite it
        tmp = f'{p}.tmp.{os.getpid()}'
        with open(tmp, 'w') as f: json.dump(data, f, indent=2, ensure_ascii=False); f.write('\n')
        os.replace(tmp, p)
        return res


def versions(data, plugin):
    return ((data or {}).get('plugins') or {}).get(plugin, {}).get('versions') or {}


def state(data, plugin, ver):
    return (versions(data, plugin).get(ver) or {}).get('state')


def last_released(data, plugin):
    rel = [v for v, r in versions(data, plugin).items() if (r or {}).get('state') == 'released']
    return max(rel, key=vkey) if rel else None


def is_canary(data, plugin, agent):
    return ((data or {}).get('canary') or {}).get(plugin) == agent


def active(data, plugin):
    return bool(plugin and ((data or {}).get('canary') or {}).get(plugin))


def decide(data, plugin, agent, disk):
    """(version to run, mode): the canary runs the version on disk unless it was refused; the others run it once
    released, else the last released one ('pinned'). (None, ...) when no version fits."""
    st = state(data, plugin, disk)
    if st == 'released': return disk, 'released'
    canary = is_canary(data, plugin, agent)
    if canary and st != 'refused': return disk, 'canary'
    lr = last_released(data, plugin)
    if lr: return lr, 'rollback' if canary else 'pinned'
    return None, 'refused' if st == 'refused' else 'none'


def _set(plugin, ver, **rec):
    def fn(d):
        vs = d['plugins'].setdefault(plugin, {}).setdefault('versions', {})
        vs[ver] = {**{k: v for k, v in rec.items() if v is not None}, 'at': now()}
    update(fn)


# ---------------------------------------------------------------- snapshots

def _snap_ok(d, ver):
    return plugin_info(d)[1] == ver


def snapshot(live, plugin, ver):
    """Folder of an immutable copy of `plugin` at `ver` (made now when missing), or None."""
    base = cache_dir()
    dest = os.path.join(base, f'{plugin}@{ver}')
    if _snap_ok(dest, ver): return dest
    os.makedirs(base, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=f'.{plugin}@{ver}.', dir=base)
    out = os.path.join(tmp, 'p')
    try:
        if plugin_info(live)[1] == ver:
            shutil.copytree(live, out, symlinks=True, ignore=IGNORE)
            if plugin_info(live)[1] != ver: return None          # the copy on disk moved during the copy
        else:
            prefix = subprocess.run(['git', '-C', live, 'rev-parse', '--show-prefix'], capture_output=True, text=True,
                                    timeout=30).stdout.strip().rstrip('/')
            if not prefix: return None
            os.makedirs(out)
            arch = subprocess.run(['git', '-C', live, 'archive', '--format=tar', f'{plugin}--v{ver}:{prefix}'],
                                  capture_output=True, timeout=120)
            if arch.returncode != 0: return None
            subprocess.run(['tar', '-x', '-C', out], input=arch.stdout, check=True, timeout=120)
        if not _snap_ok(out, ver): return None
        try: os.rename(out, dest)
        except OSError:
            if not _snap_ok(dest, ver): raise
        return dest
    except (OSError, subprocess.SubprocessError):
        return dest if _snap_ok(dest, ver) else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def in_use(plugin):
    """Versions of `plugin` whose snapshot a live process still runs, or None when that cannot be known (no /proc).

    A pinned runner is `bash <cache>/<plugin>@<version>/.../runner.sh` (the exec keeps that path in its command line) and
    its heavy tick `python3 <cache>/<plugin>@<version>/.../watch.py`, so a live process with an argument or its working
    directory inside a snapshot holds that version. Zombies have no command line and do not count (PLN0087: a prune
    after three quick releases removed the copy under a runner still on the old one)."""
    if not os.path.isdir('/proc'): return None
    base = cache_dir()
    pres = {os.path.join(b, plugin + '@') for b in (base, os.path.abspath(base), os.path.realpath(base))}
    used = set()
    for pid in os.listdir('/proc'):
        if not pid.isdigit(): continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f: args = f.read().decode(errors='replace').split('\0')
        except OSError: continue
        if not any(args): continue
        try: args.append(os.readlink(f'/proc/{pid}/cwd'))
        except OSError: pass
        for a in args:
            for pre in pres:
                if a.startswith(pre):
                    v = a[len(pre):].split('/', 1)[0]
                    if v: used.add(v)
    return used


def prune(plugin, data, keep=()):
    """Drops snapshots older than the release before the last one (runners on them relaunch at their next tick), never
    one a live process still runs (in_use): that runner relaunches at its next heavy tick and a later prune drops it."""
    rel = sorted((v for v, r in versions(data, plugin).items() if (r or {}).get('state') == 'released'), key=vkey)
    if len(rel) < 2: return
    floor = vkey(rel[-2])
    busy = in_use(plugin)
    if busy is None: return
    keep = set(keep) | busy
    base = cache_dir()
    for n in os.listdir(base) if os.path.isdir(base) else ():
        p, _, v = n.partition('@')
        if p == plugin and v and v not in keep and vkey(v) < floor:
            shutil.rmtree(os.path.join(base, n), ignore_errors=True)


# ---------------------------------------------------------------- canary test

# A broken source in the tick's protocol line: `FONTE QUEBRADA (<source>): <cause>` ('VIGIA QUEBRADO' up to job-scout 0.18).
BROKEN = r'^(?:FONTE QUEBRADA|VIGIA QUEBRADO) \(([a-z0-9_]*)\)'


def _norm(line):
    return re.sub(r'\d+', '#', line).strip()


def _broken(text):
    return sorted(set(re.findall(BROKEN, text or '', re.M)))


def _read(p):
    try:
        with open(p, errors='replace') as f: return f.read()
    except OSError: return ''


def write_baseline(rdir, ver):
    """What was already wrong before the canary started: planou.err line kinds and the broken sources."""
    err = os.path.join(rdir, 'planou.err')
    sig = _read(os.path.join(rdir, 'quebrado.sig'))
    broken = [] if re.search(r'^voltou', sig, re.M) else _broken(sig.replace(' FONTE', '\nFONTE').replace(' VIGIA', '\nVIGIA'))
    text = _read(err)
    kinds = sorted({_norm(l) for l in text.splitlines() if l.strip()})[-3000:]
    b = {'version': ver, 'at': now(), 'err_size': len(text.encode()), 'err_tail': _tail(text),
         'err_kinds': kinds, 'broken': broken}
    _save_baseline(rdir, b)


def _save_baseline(rdir, b):
    with open(os.path.join(rdir, BASELINE), 'w') as f: json.dump(b, f)


def _baseline(rdir, ver):
    try: b = json.loads(_read(os.path.join(rdir, BASELINE)) or '{}')
    except ValueError: b = {}
    return b if b.get('version') == ver else {'version': ver, 'err_size': 0, 'err_kinds': [], 'broken': []}


def _tail(text):
    """The end of planou.err, to find where the new lines start after the runner cut the file to its last 500 lines."""
    return text[-300:]


def _fresh(rdir, b):
    """(lines written to planou.err since the baseline, the whole file now)."""
    err = _read(os.path.join(rdir, 'planou.err'))
    raw = err.encode()
    if len(raw) >= b.get('err_size', 0):
        fresh = raw[b.get('err_size', 0):].decode(errors='replace')
    else:   # the runner cut the file: the new lines follow the old end, when it is still there
        anchor = b.get('err_tail') or ''
        i = err.rfind(anchor) if anchor else -1
        fresh = err[i + len(anchor):] if i >= 0 else err
    return [l.strip() for l in fresh.splitlines() if l.strip()], err


# Planou unreachable, not a bug of the version: the Planou client's status 0 network, a 502/503/504 (the tunnel or the
# API restarting), refused/reset/closed connections, timeouts, name resolution.
NETWORK = re.compile(r'(?:^|[\s:(])(?:0 network\b|50[234] )|Connection (?:refused|reset|aborted)|Remote end closed'
                     r'|timed out|\btimeout\b|Temporary failure in name resolution|Name or service not known'
                     r'|Network is unreachable|No route to host|Bad Gateway|Service Unavailable|Gateway Time-?out', re.I)


# An external source (LinkedIn, Teams...) that did not answer this once, not a bug of the version (PLN0149): the network
# causes above plus the TLS ones. A certificate that does not verify is not here: it does not pass on its own.
SSL = re.compile(r'SSLError|_ssl\.c|handshake|UNEXPECTED_EOF|EOF occurred in violation of protocol|RemoteDisconnected'
                 r'|IncompleteRead', re.I)


def _is_network(line):
    return bool(NETWORK.search(line or '')) and 'Traceback' not in line


def _is_transient(line):
    return (_is_network(line) or bool(SSL.search(line or ''))) and 'Traceback' not in line and 'CERTIFICATE_VERIFY' not in line


def _split_tick(tick_text):
    """(tick text without the broken-source lines whose cause is the network, the FONTE QUEBRADA (planou) ones among them,
    {external source: its line} for the others)."""
    keep, net, ext = [], [], {}
    for l in (tick_text or '').splitlines():
        m = re.match(BROKEN, l)
        if m and m.group(1) == 'planou' and _is_network(l): net.append(l)
        elif m and m.group(1) != 'planou' and _is_transient(l): ext.setdefault(m.group(1), l.strip())
        else: keep.append(l)
    return '\n'.join(keep), net, ext


def _source_exit(rc, tick_text):
    """The heavy tick exits 2 when a source broke (agent.py, scout.py): that exit is the broken-source lines' own, which
    are judged one by one. Any other exit, or 2 without such a line (python3 that could not open the file) or with a
    Traceback, is the version's."""
    t = tick_text or ''
    return rc == 2 and bool(re.search(BROKEN, t, re.M)) and 'Traceback (most recent call last)' not in t


def _new_trouble(rdir, ver, rc, tick_text, b):
    """(reasons that refuse `ver` at once, new lines that only say Planou was unreachable, {external source: line} broken
    this once by the network)."""
    rest, net, ext = _split_tick(tick_text)
    ext = {s: l for s, l in ext.items() if s not in b.get('broken', [])}   # already broken before the upgrade
    reasons = []
    if rc != 0 and not _source_exit(rc, tick_text): reasons.append(f'o tick pesado saiu com {rc}')
    new_broken = [s for s in _broken(rest) if s not in b.get('broken', [])]
    if new_broken: reasons.append('FONTE QUEBRADA (' + ', '.join(new_broken) + ')')
    if 'Traceback (most recent call last)' in rest: reasons.append('Traceback na saida do tick')
    kinds = set(b.get('err_kinds') or [])
    new = [l for l in _fresh(rdir, b)[0] if _norm(l) not in kinds]
    other = [l for l in new if not _is_network(l)]
    if other: reasons.append(f'{len(other)} erro(s) novo(s) no planou.err: ' + ' | '.join(dict.fromkeys(other[:3]))[:300])
    return reasons, [l.strip() for l in net] + [l for l in new if _is_network(l)], ext


def evaluate(rdir, ver, rc, tick_text):
    """Reasons to refuse `ver` after its heavy tick ([] = nothing against it). Lines that only say Planou was
    unreachable or that an external source did not answer this once are not here: `judge` decides on them."""
    return _new_trouble(rdir, ver, rc, tick_text, _baseline(rdir, ver))[0]


def planou_up(timeout=5):
    """Planou answers a plain GET of /api/version on the host of its base_url (any status below 500, even 401 or 404:
    the API is there; a 502/503/504 is the tunnel or the API restarting). No key and not the /v1 contract."""
    import urllib.error, urllib.parse, urllib.request
    try:
        from . import config
        u = urllib.parse.urlsplit(config.get('planou.base_url') or '')
    except Exception:
        return False
    if u.scheme not in ('http', 'https') or not u.netloc: return False
    try:
        with urllib.request.urlopen(urllib.request.Request(f'{u.scheme}://{u.netloc}/api/version',
                                                           headers={'User-Agent': 'planou-agent rollout'}),
                                    timeout=timeout):
            return True
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


def judge(rdir, ver, rc, tick_text):
    """('release' | 'refuse' | 'defer', reasons) after a heavy tick of the canary. 'defer' keeps the version in test
    and moves the baseline past the lines already seen, so the next heavy tick judges only what comes after them."""
    b = _baseline(rdir, ver)
    reasons, net, ext = _new_trouble(rdir, ver, rc, tick_text, b)
    if reasons: return 'refuse', reasons
    again = sorted(s for s in ext if s in (b.get('transient') or {}))
    if again:   # the same external source failed by the network in two heavy ticks in a row
        return 'refuse', [(f'FONTE QUEBRADA ({", ".join(again)}) por rede em dois ticks seguidos: '
                           + ' | '.join(ext[s] for s in again))[:400]]
    if not net and not ext: return 'release', []
    whys, up = [], False
    if net:
        sample = ' | '.join(dict.fromkeys(net[:3]))[:300]
        up = planou_up()
        if up and b.get('net_up', 0) >= 1:
            return 'refuse', [f'{len(net)} erro(s) de rede novo(s) no planou.err em dois ticks seguidos com o Planou no ar: '
                              f'{sample}'[:400]]
        if up: whys.append(f'{len(net)} erro(s) de rede do Planou, que responde agora (queda que ja passou?): {sample}')
        else: whys.append(f'Planou fora do ar ({len(net)} erro(s) de rede): {sample}')
    if ext:
        whys.append(f'fonte externa sem resposta desta vez ({", ".join(sorted(ext))}): '
                    + ' | '.join(ext[s] for s in sorted(ext))[:300])
    why = '; '.join(whys)
    err = _fresh(rdir, b)[1]
    # `transient` holds only this tick's sources: a source that answers in the next tick is forgotten, and two different
    # sources never add up
    b.update(deferred=b.get('deferred', 0) + 1, net_up=1 if up else 0, transient={s: 1 for s in ext},
             err_size=len(err.encode()), err_tail=_tail(err), deferred_at=now())
    b.setdefault('at', now())
    try: _save_baseline(rdir, b)
    except OSError: pass
    return 'defer', [why[:400]]


# ---------------------------------------------------------------- runner protocol

def _version_file(rdir, plugin, ver, mode):
    try:
        os.makedirs(rdir, exist_ok=True)
        with open(os.path.join(rdir, VERSION_FILE), 'w') as f: f.write(f'{plugin} {ver} {mode}\n')
    except OSError: pass


def cmd_start(scripts, agent, rdir):
    """One line: PIN=<scripts dir in a snapshot>, STOP=<why>, or nothing (run the copy on disk, as before)."""
    live = plugin_dir(scripts)
    plugin, disk = plugin_info(live) if live else (None, None)
    data = load()
    if not disk or not active(data, plugin):
        if plugin: _version_file(rdir, plugin, disk, 'live')
        return ''

    def fn(d):   # first sight of the plugin: the version on disk is the released baseline
        vs = d['plugins'].setdefault(plugin, {}).setdefault('versions', {})
        if not vs: vs[disk] = {'state': 'released', 'by': 'bootstrap', 'at': now()}
        target, mode = decide(d, plugin, agent, disk)
        if mode == 'canary' and state(d, plugin, disk) != 'released':
            if not state(d, plugin, disk): vs[disk] = {'state': 'canary', 'agent': agent, 'at': now()}
            try: have = json.loads(_read(os.path.join(rdir, BASELINE)) or '{}').get('version')
            except ValueError: have = None
            if have != disk:   # a relaunch during the test keeps the baseline taken before the upgrade
                os.makedirs(rdir, exist_ok=True)
                write_baseline(rdir, disk)
        return target, mode
    target, mode = update(fn)
    if not target:
        return f'STOP={plugin} {disk} foi recusada e nao ha versao liberada para voltar (rollout.json)'
    snap = snapshot(live, plugin, target)
    if not snap:
        if target == disk:
            _version_file(rdir, plugin, disk, 'live')
            return ''
        return f'STOP=sem copia da versao {target} de {plugin} (nem em ~/.cache/team/plugins nem na tag {plugin}--v{target})'
    prune(plugin, load(), keep=(target, disk))
    _version_file(rdir, plugin, target, mode)
    return 'PIN=' + os.path.join(snap, os.path.relpath(os.path.realpath(scripts), live))


def cmd_tick(scripts, live, agent, rdir, rc, tick_file):
    """ACTION=none|released|relaunch|refused|warn, then ALERT=<title> (refused) and the block for tick.out (warn: the
    canary reads an older copy than the installed one, see stale_copy)."""
    running_dir = plugin_dir(scripts)
    plugin, running = plugin_info(running_dir) if running_dir else (None, None)
    data = load()
    if not running or not active(data, plugin): return ['ACTION=none']
    tick_text = _read(tick_file)
    out, action = [], 'none'
    if is_canary(data, plugin, agent) and state(data, plugin, running) == 'canary':
        verdict, reasons = judge(rdir, running, rc, tick_text)
        if verdict == 'defer':
            why = reasons[0]
            _set(plugin, running, state='canary', agent=agent, reason=f'adiada: {why}'[:300])
            line = f'-- rollout: decisao sobre {plugin} {running} adiada para o proximo tick pesado ({why})'
            out.append(line)
            try:
                with open(os.path.join(rdir, 'avisos.log'), 'a') as f: f.write(f'{now()}\t{line}\n')
            except OSError: pass
        elif reasons:
            why = '; '.join(reasons)
            _set(plugin, running, state='refused', agent=agent, reason=why)
            back = last_released(load(), plugin)
            out += [f'ALERT=Versao {running} de {plugin} recusada no canario {agent}: {why}'[:250],
                    f'== VERSAO RECUSADA ({plugin} {running}, canario {agent})',
                    f'-- {why}',
                    f'-- o runner parou e a versao nao foi liberada (alerta alto no Planou); os outros agentes seguem na '
                    f'{back or "versao anterior"}.',
                    f'-- relancar como sempre (stop e start) sobe a {back or "versao anterior"} liberada; avisar o usuario.']
            _version_file(rdir, plugin, running, 'refused')
            return ['ACTION=refused'] + out
        else:
            _set(plugin, running, state='released', agent=agent, by=f'canary {agent}')
            try: os.remove(os.path.join(rdir, BASELINE))
            except OSError: pass
            action = 'released'
            out.append(f'-- rollout: {plugin} {running} liberada pelo canario {agent} ({now()})')
            _version_file(rdir, plugin, running, 'released')
    disk = plugin_info(live)[1]
    stale, lines = stale_copy(scripts, live, plugin, running, disk, rdir, is_canary(load(), plugin, agent))
    if stale:
        return [f'ACTION={stale}'] + out + lines
    if disk:
        target, mode = decide(load(), plugin, agent, disk)
        if target and target != running:
            why = 'canario' if mode == 'canary' else 'liberada' if mode == 'released' else mode
            out += [f'== RELANCAR ({plugin} {running} -> {target}, {why})',
                    '-- versao nova para este agente: relancar o runner como sempre (stop e start); nada mais a fazer.']
            action = 'relaunch'
    return [f'ACTION={action}'] + out


# ---------------------------------------------------------------- the copy in use moved (PLN0336)
STALE_FILE = 'rollout.stale'


def skill_name(scripts):
    """The skill folder of the runner's scripts (skills/<name>/scripts inside the plugin), or None."""
    pdir = plugin_dir(scripts) if scripts else None
    if not pdir: return None
    parts = os.path.relpath(os.path.realpath(scripts), pdir).split(os.sep)
    return parts[1] if len(parts) >= 2 and parts[0] == 'skills' else None


def installed_dir(plugin):
    """The plugin folder of the installer's copy (install.sh: ~/.local/share/planou/claude-plugins), or None."""
    root = os.environ.get('PLANOU_INSTALL_DIR') or os.path.expanduser('~/.local/share/planou/claude-plugins')
    d = os.path.join(root, 'plugins', plugin)
    return d if os.path.isfile(os.path.join(d, '.claude-plugin', 'plugin.json')) else None


def _git_root(d):
    while d and d != os.path.dirname(d):
        if os.path.exists(os.path.join(d, '.git')): return d
        d = os.path.dirname(d)
    return None


def stale_copy(scripts, live, plugin, running, disk, rdir, canary):
    """(action, lines) when this runner reads an older copy than the one in use (PLN0336, 04/10/2026: the canary read a
    clone stopped at 0.72.0 while the installer had put 0.85.0 in place; no version was released for 3 days and the
    provisioner stayed pinned on 0.73.0). ROLLOUT_LIVE is resolved once, at start:
      - the skill link (~/.claude/skills/<skill>) now resolves to another copy, newer than `live`: relaunch. The extra
        `-- ` lines keep the runner from relaunching itself (that exec goes back to `live`): the session's start, through
        the link, reads the new copy. Once per pair of versions, like the warning: a session that has not relaunched
        yet is not woken again on every heavy tick;
      - only the canary: the link still points at `live` (a clone kept on purpose) but the installer's copy is newer: one
        warning per pair of versions, with the way out. Nothing is switched (the clone may hold someone's work).
    (None, []) otherwise."""
    if not live or not disk: return None, []
    mark = os.path.join(rdir, STALE_FILE)

    def first(key):   # True the first time this key is seen (kept in rollout.stale)
        if _read(mark).strip() == key: return False
        try:
            os.makedirs(rdir, exist_ok=True)
            with open(mark, 'w') as f: f.write(key + '\n')
        except OSError: pass
        return True
    live_r = os.path.realpath(live)
    skill = skill_name(scripts)
    link = os.path.join(os.path.expanduser('~/.claude/skills'), skill) if skill else None
    use = plugin_dir(link) if link and os.path.isdir(link) else None
    if use and os.path.realpath(use) != live_r:
        v = plugin_info(use)[1]
        if v and vkey(v) > vkey(disk):
            if not first(f'relaunch {disk} {v}'): return None, []
            return 'relaunch', [
                f'== RELANCAR ({plugin} {running} -> {v}, copia em uso mudou)',
                f'-- a copia em uso ({link}) agora e {os.path.realpath(use)}, na {v}; este runner ainda le {live_r} ({disk}).',
                '-- relancar o runner como sempre (stop e start), pelo link: o relancamento sozinho voltaria para a copia velha.']
    if not canary: return None, []
    inst = installed_dir(plugin)
    if not inst or os.path.realpath(inst) == live_r: return None, []
    v = plugin_info(inst)[1]
    if not v or vkey(v) <= vkey(disk): return None, []
    if not first(f'warn {disk} {v}'): return None, []
    lr = last_released(load(), plugin)
    lines = [f'== CANARIO NUMA COPIA VELHA ({plugin} {disk} em {live_r}; a instalada e a {v})',
             f'-- enquanto o canario le esta copia, nenhuma versao nova de {plugin} e liberada: o provisionador e os outros '
             f'agentes ficam na {lr or disk}.']
    root = _git_root(live_r)
    if root: lines.append(f'-- atualizar o clone (se nao tiver trabalho local): git -C "{root}" pull --ff-only')
    if link: lines.append(f'-- ou usar a copia instalada: ln -sfn "{os.path.join(inst, "skills", skill)}" "{link}"')
    lines.append('-- depois relancar o runner (stop e start); avisar o usuario, nada foi trocado sozinho.')
    return 'warn', lines


# ---------------------------------------------------------------- CLI

def status():
    data = load() or {}
    res = {}
    for plugin in sorted(set(data.get('canary') or {}) | set(data.get('plugins') or {})):
        vs = versions(data, plugin)
        newest = max(vs, key=vkey) if vs else None
        res[plugin] = {'canary': (data.get('canary') or {}).get(plugin), 'released': last_released(data, plugin),
                       'newest': newest, 'newest_state': state(data, plugin, newest) if newest else None,
                       'reason': (vs.get(newest) or {}).get('reason') if newest else None}
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog='watch_core.rollout')
    ap.add_argument('cmd', choices=['start', 'tick', 'status', 'canary', 'release', 'refuse'])
    ap.add_argument('arg', nargs='*')
    for o in ('--scripts', '--live', '--agent', '--runner-dir', '--tick', '--reason'): ap.add_argument(o)
    ap.add_argument('--rc', type=int, default=0)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    if a.cmd == 'start':
        line = cmd_start(a.scripts, a.agent, a.runner_dir)
        if line: print(line)
    elif a.cmd == 'tick':
        print('\n'.join(cmd_tick(a.scripts, a.live, a.agent, a.runner_dir, a.rc, a.tick)))
    elif a.cmd == 'status':
        st = status()
        if a.json: print(json.dumps(st, ensure_ascii=False)); return 0
        if not st: print('rollout desligado (sem ~/.config/team/rollout.json)'); return 0
        for p, r in st.items():
            extra = ''
            if r['newest'] and r['newest'] != r['released']:
                extra = f"; {r['newest']}: {STATES_PT.get(r['newest_state'], r['newest_state'])}"
                if r['reason']: extra += f" ({r['reason']})"
            print(f"{p}: canario {r['canary'] or '-'}; liberada {r['released'] or '-'}{extra}")
    elif a.cmd == 'canary':
        if len(a.arg) != 2: ap.error('use: canary <plugin> <agent>')
        update(lambda d: d['canary'].__setitem__(a.arg[0], a.arg[1]))
        print(f'canario de {a.arg[0]}: {a.arg[1]}')
    else:
        if len(a.arg) != 2: ap.error(f'use: {a.cmd} <plugin> <version>')
        st = 'released' if a.cmd == 'release' else 'refused'
        _set(a.arg[0], a.arg[1], state=st, by='manual', reason=a.reason)
        print(f'{a.arg[0]} {a.arg[1]}: {STATES_PT[st]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
