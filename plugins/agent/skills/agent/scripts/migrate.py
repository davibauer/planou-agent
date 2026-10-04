#!/usr/bin/env python3
"""Moves a legacy instance folder into the agent plugin, and back (E3 of docs/agent-plugin-design.md).

  agent.py <instance> --migrate [--dry] [--cwd PATH] [--alias NAME]...
  agent.py <instance> --undo [--dry]          (also --migrate --undo)

--migrate, for work-watch-<x> (~/.config/work-watch/<x>) or any other legacy agent (~/.config/<name>):
  1. checks the config (schema.py, with every adapter it names) and that no heavy tick of the instance is running;
  2. renames the folder to ~/.config/agent/<instance> (same filesystem: state, caches, secrets/ with their modes and
     history move as they are) and puts a symbolic link at the old path right away, so the runner of the session that
     is still open, the chief-of-staff commands and any forgotten script keep finding everything;
  3. adds to the config only what the agent plugin needs and the old config lacks: "schema": 1, "behaviors"
     (["work-watch"] for work-watch-<x>, [<name>] when the plugin has a behavior of the agent's name, as job-scout;
     plus "task-queue" when "planou.task_queue" is on), "session" (cwd and aliases for the `team` launcher), for an
     agent of its own plugin (job-scout: it had no test mode) "live": true, the agent's old heavy tick interval
     ("interval_s": 1800 for job-scout, 21600 for travel-agent) and, for work-watch, the triage trigger `<command-name>/agent` of the custos
     hook. job-scout's "schema": 2 stays (the version of its data schema; the agent plugin reads it, schema.py). The Portuguese keys
     stay as they are: the old engine reads the same file through the link until the session is relaunched, and the
     agent plugin reads them as aliases (schema.py). A copy of the config before the change goes to
     config/config.json.pre-agent; what was added goes to .agent-migration.json, which --undo reads.

--undo: the other way. Removes what --migrate added to the config (edits made since then stay), removes the link and
renames the folder back. When nothing else changed in the config since --migrate, config.json.pre-agent comes back as it
was, byte for byte; otherwise the config is rewritten in its own layout (indent and final newline, as --migrate does). Refuses while the agent runner of the instance is alive (stop it first).

Never touches ~/.config/watch-core, ~/.config/team or ~/.config/work-inbox: they are keyed by the agent name, which
does not change.
"""
import json, os, re, shutil, subprocess, sys
from datetime import datetime

import paths
from watch_core import behavior_names, config as wc, fileio

MARKER = '.agent-migration.json'
LEGACY_INTERVAL_S = {'job-scout': 1800, 'travel-agent': 21600}   # the old runner's heavy tick, kept by the agent runner
TRIAGE_TRIGGER = '<command-name>/agent'


def _config_file(root):
    for f in (os.path.join(root, 'config', 'config.json'), os.path.join(root, 'config.json')):
        if os.path.isfile(f): return f
    return None


def _style(path):
    """(indent, trailing newline) of the JSON file at `path`: job-scout writes its config with indent 1, work-watch with
    2. The rewrite keeps the file's own layout, so the diff of --migrate is only the added keys."""
    try: text = open(path, encoding='utf-8').read()
    except OSError: return 2, True
    m = re.search(r'\n([ \t]+)\S', text)
    ind = m.group(1) if m else '  '
    return (len(ind) if set(ind) == {' '} else ind), text.endswith('\n')


def _write_json(path, data):
    indent, nl = _style(path)
    fileio.write(path, json.dumps(data, ensure_ascii=False, indent=indent) + ('\n' if nl else ''))   # keeps the old file's mode


def _same_home(pid):
    """True unless the process runs with another HOME: a tick in another HOME touches another ~/.config (a test's, or
    another CI job's on the same machine, or another user's: its environment is unreadable to us, ours never is). A
    process that vanished while we looked counts (it may be ours)."""
    try: env = open(f'/proc/{pid}/environ', 'rb').read().split(b'\0')
    except PermissionError: return False
    except OSError: return True
    home = next((e[5:] for e in env if e.startswith(b'HOME=')), None)
    return home is None or home.decode(errors='replace') == os.path.expanduser('~')


def _procs(pattern):
    """pids whose command line matches `pattern`, with this HOME (never this process)."""
    out = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit() or int(pid) == os.getpid(): continue
        try: cmd = open(f'/proc/{pid}/cmdline', 'rb').read().replace(b'\0', b' ').decode(errors='replace')
        except OSError: continue
        if re.search(pattern, cmd) and _same_home(pid): out.append((int(pid), cmd.strip()))
    return out


def heavy_ticks(name):
    """Heavy ticks of this instance running right now (old engine or new)."""
    short = paths.short_name(name)
    pat = r'python3? \S*(watch\.py %s|agent\.py %s)( |$)' % (re.escape(short), re.escape(name))
    if name == 'job-scout': pat += r'|python3? \S*/scout\.py( |$)'    # the old job-scout tick has no instance argument
    if name == 'travel-agent':       # nor travel-agent's (old plugin or behavior, venv python): .../travel-agent/scripts/agent.py
        pat += r'|python3? \S*/(travel-agent|flight-price-watch)/scripts/agent\.py( |$)'
    return [p for p in _procs(pat) if 'runner.sh' not in p[1] and '--migrate' not in p[1] and '--undo' not in p[1]]


def runner_pid(root):
    try: pid = open(os.path.join(root, 'cache', 'runner', 'runner.pid')).read().strip()
    except OSError: return None
    return int(pid) if pid.isdigit() and os.path.exists(f'/proc/{pid}') else None


def _runner_script(pid):
    try: return open(f'/proc/{pid}/cmdline', 'rb').read().replace(b'\0', b' ').decode(errors='replace')
    except OSError: return ''


def plan(name, cwd=None, aliases=None):
    """(legacy root, new root, config file, raw config, keys to add {key: value}, add the triage trigger?) or
    SystemExit with the reason."""
    new = os.path.join(wc.agent_base(), name)
    old = wc.legacy_agent_root(name)
    if os.path.lexists(new): raise SystemExit(f'{name}: ja esta em {new} (nada a migrar)')
    if os.path.islink(old) or not os.path.isdir(old): raise SystemExit(f'{name}: pasta antiga nao existe ou ja e um link: {old}')
    cfg_file = _config_file(old)
    if not cfg_file: raise SystemExit(f'{name}: sem config em {old}')
    raw = json.load(open(cfg_file, encoding='utf-8'))
    add = {}
    if 'schema' not in raw: add['schema'] = 1
    ww = name.startswith(paths.WORK_WATCH_PREFIX)
    if 'behaviors' not in raw:
        b = ['work-triage'] if ww else ([behavior_names.behavior_name(name)] if paths.behavior_file(name) else [])
        if isinstance(raw.get('planou'), dict) and raw['planou'].get('task_queue') is True: b.append('task-queue')
        add['behaviors'] = b
    if not ww and 'live' not in raw: add['live'] = True          # the old plugins of one agent had no test mode
    if 'interval_s' not in raw and 'intervalo_s' not in raw and name in LEGACY_INTERVAL_S:
        add['interval_s'] = LEGACY_INTERVAL_S[name]
    if 'session' not in raw:
        s = {'aliases': list(dict.fromkeys(aliases or [paths.short_name(name)])), 'rotate': False}
        if cwd: s['cwd'] = cwd
        add['session'] = s
    trigger = False
    for h in raw.get('ganchos') or raw.get('hooks') or []:
        if isinstance(h, dict) and (h.get('tipo') or h.get('type')) == 'custos' and isinstance(h.get('gatilhos_triagem'), list) \
                and name.startswith(paths.WORK_WATCH_PREFIX) and TRIAGE_TRIGGER not in h['gatilhos_triagem']:
            trigger = True
    return old, new, cfg_file, raw, add, trigger


def _check_config(name, old, raw, add):
    """Errors of the config as the agent plugin will read it (adapters looked up in the old folder)."""
    import schema, adapters
    paths.instance(name)                       # still the legacy folder: ~/.config/agent/<name> does not exist yet
    cfg = schema.normalize({**raw, **add})
    err, _ = schema.validate(cfg, paths.behavior_file, adapters.existe)
    return err


def _link_after_rename(old, new):
    """Renames old -> new and puts the link at old at once (a link made beforehand, renamed onto the free path). If
    something recreated the old folder in between (a runner writing), its files go into the new folder first."""
    tmp_link = old.rstrip('/') + '.agent-link'
    if os.path.lexists(tmp_link): os.remove(tmp_link)
    os.symlink(new, tmp_link)
    os.rename(old, new)
    for _ in range(3):
        try:
            os.rename(tmp_link, old)
            break
        except OSError:
            if os.path.isdir(old) and not os.path.islink(old):
                for dirpath, _dirs, files in os.walk(old):
                    rel = os.path.relpath(dirpath, old)
                    os.makedirs(os.path.join(new, rel), exist_ok=True)
                    for f in files: os.replace(os.path.join(dirpath, f), os.path.join(new, rel, f))
                shutil.rmtree(old)
    if not os.path.islink(old): raise SystemExit(f'ERRO: o link {old} -> {new} nao ficou no lugar; confira na mao')


def migrate(name, dry=False, cwd=None, aliases=None):
    old, new, cfg_file, raw, add, trigger = plan(name, cwd, aliases)
    err = _check_config(name, old, raw, add)
    if err: raise SystemExit(f'{name}: config com erro, nada foi movido:\n  ' + '\n  '.join(err))
    busy = heavy_ticks(name)
    if busy: raise SystemExit(f'{name}: tick pesado rodando agora (pid {busy[0][0]}); tente de novo em alguns segundos')
    rp = runner_pid(old)
    rel = os.path.relpath(cfg_file, old)
    print(f'{name}: {old} -> {new} (link no lugar antigo)')
    for k, v in add.items(): print(f'  config: + "{k}": {json.dumps(v, ensure_ascii=False)}')
    if trigger: print(f'  config: custos.gatilhos_triagem + "{TRIAGE_TRIGGER}"')
    if rp: print(f'  runner vivo (pid {rp}: {_runner_script(rp)[:120]}): segue pelo link ate a sessao ser relancada')
    if dry: print('(--dry: nada foi feito)'); return
    os.makedirs(wc.agent_base(), exist_ok=True)
    shutil.copy2(cfg_file, cfg_file + '.pre-agent')
    _link_after_rename(old, new)
    f = os.path.join(new, rel)
    cfg = json.load(open(f, encoding='utf-8'))
    cfg.update(add)
    if trigger:
        for h in cfg.get('ganchos') or cfg.get('hooks') or []:
            if isinstance(h, dict) and (h.get('tipo') or h.get('type')) == 'custos' and isinstance(h.get('gatilhos_triagem'), list):
                h['gatilhos_triagem'].append(TRIAGE_TRIGGER)
    _write_json(f, cfg)
    _write_json(os.path.join(new, MARKER), {'from': old, 'at': datetime.now().astimezone().isoformat(timespec='seconds'),
                                           'config': rel, 'added': sorted(add), 'triage_trigger': trigger})
    print(f'{name}: migrada. Desfazer: agent.py {name} --undo')


def _same_as(path, cfg):
    """True when the JSON file at `path` holds exactly `cfg`, keys in the same order."""
    try: pre = json.load(open(path, encoding='utf-8'))
    except (OSError, ValueError): return False
    return json.dumps(pre, sort_keys=False) == json.dumps(cfg, sort_keys=False)


def undo(name, dry=False):
    new = os.path.join(wc.agent_base(), name)
    old = wc.legacy_agent_root(name)
    if not os.path.isdir(new) or os.path.islink(new): raise SystemExit(f'{name}: nao esta em {new} (nada a desfazer)')
    if os.path.lexists(old) and not (os.path.islink(old) and os.path.realpath(old) == os.path.realpath(new)):
        raise SystemExit(f'{name}: {old} existe e nao e o link da migracao; confira na mao')
    rp = runner_pid(new)                        # an old work-watch runner through the link may stay: the move back suits it
    script = _runner_script(rp) if rp else ''
    if rp and re.search(r'runner\.sh %s( |$)' % re.escape(name), script):
        raise SystemExit(f'{name}: o runner do plugin agent esta vivo (pid {rp}); pare antes: runner.sh {name} stop')
    busy = heavy_ticks(name)
    if busy: raise SystemExit(f'{name}: tick pesado rodando agora (pid {busy[0][0]}); tente de novo em alguns segundos')
    try: mk = json.load(open(os.path.join(new, MARKER), encoding='utf-8'))
    except (OSError, ValueError): mk = {'added': [], 'triage_trigger': False}
    cfg_file = os.path.join(new, mk.get('config') or 'config/config.json')
    print(f'{name}: {new} -> {old}')
    for k in mk.get('added') or []: print(f'  config: - "{k}"')
    if mk.get('triage_trigger'): print(f'  config: custos.gatilhos_triagem - "{TRIAGE_TRIGGER}"')
    if dry: print('(--dry: nada foi feito)'); return
    if os.path.isfile(cfg_file):
        cfg = json.load(open(cfg_file, encoding='utf-8'))
        shutil.copy2(cfg_file, cfg_file + '.agent')
        for k in mk.get('added') or []: cfg.pop(k, None)
        if mk.get('triage_trigger'):
            for h in cfg.get('ganchos') or cfg.get('hooks') or []:
                if isinstance(h, dict) and isinstance(h.get('gatilhos_triagem'), list) and TRIAGE_TRIGGER in h['gatilhos_triagem']:
                    h['gatilhos_triagem'].remove(TRIAGE_TRIGGER)
        if _same_as(cfg_file + '.pre-agent', cfg):
            shutil.copy2(cfg_file + '.pre-agent', cfg_file)    # nothing edited since --migrate: the old file byte for byte
        else:
            _write_json(cfg_file, cfg)
    try: os.remove(os.path.join(new, MARKER))
    except OSError: pass
    if os.path.islink(old): os.remove(old)
    os.rename(new, old)
    print(f'{name}: de volta em {old}')


def cli(name, args):
    rest = [a for a in args if a not in ('--migrate', '--undo', '--dry')]
    cwd, aliases, i = None, [], 0
    while i < len(rest):
        if rest[i] == '--cwd' and i + 1 < len(rest): cwd = rest[i + 1]; i += 2
        elif rest[i] == '--alias' and i + 1 < len(rest): aliases.append(rest[i + 1]); i += 2
        else: sys.exit(f'argumento desconhecido: {rest[i]} (uso: --migrate [--dry] [--cwd PATH] [--alias NOME] | --undo [--dry])')
    if '--undo' in args: return undo(name, '--dry' in args)
    return migrate(name, '--dry' in args, cwd, aliases)
