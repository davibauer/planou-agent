#!/usr/bin/env python3
"""The team's agent list: ~/.config/team/agents.json, read by the `team` launcher, the team-terminals VS Code extension
(extensions/vscode-team-terminals) and the chief-of-staff. A new agent opens with `team <name>` once it has an entry
here; nobody edits a script.

  team_agents.py resolve <name-or-alias> [--seed FILE|-]   5 lines: name, cwd, skill, rotate (0/1), agent plugin (0/1)
  team_agents.py list [--json]                              every agent (name, enabled, aliases, cwd, skill)
  team_agents.py migrate [--seed FILE|-]                    creates agents.json when it does not exist; else nothing

The file is a JSON list, one object per agent:

  {"name": "acme-watch", "enabled": true, "order": 20, "aliases": ["acme"], "cwd": "~/src/acme",
   "skill": "agent acme-watch", "rotate": false}

  name     required; lowercase letters, digits and hyphen (the session, Remote Control and file name)
  enabled  default true; only the extension reads it (false = no terminal opened at start). `team <name>` still opens
  order    default: position x 10; the extension opens in this order
  aliases  default []; other names `team` accepts (unique across the whole file, never another agent's name)
  cwd      default: the instance's config "session.cwd", else "~"; where the session opens (~ is expanded)
  skill    default "agent <name>" for an instance of the agent plugin (~/.config/agent/<name>/config/config.json),
           else <name>; what the session runs at open (`/<skill>`)
  rotate   default: the instance's config "session.rotate", else false; daily new session + early compaction
Other fields are kept as they are (by this module and by the extension); team_status.py reads "runner_dir" (the runner
folder, when it is not one of the usual places).

Precedence: agents.json wins. A field an entry leaves out comes from the instance's config "session" block; a name that
is not in agents.json but is an instance (or a "session.aliases" entry of one) still resolves, so an instance whose
entry was not written yet opens anyway.

Migration: when agents.json does not exist (and only then; an unreadable or invalid file is an error, never
regenerated), `resolve` with --seed and `migrate` write it once from the seed (the launcher's old fixed table) plus
every instance of the agent plugin. `enabled` follows what the extension did without the file: true for an agent with
~/.config/team/<name>.session or an instance with "live": true, false otherwise. The write never replaces a file that
appeared in the meantime (temp file + link).

Writers (the local provisioner): `upsert(entry)` and `remove(name)` rewrite the file atomically under a lock, keeping
the other entries and unknown fields.
"""
import fcntl, glob, json, os, re, sys, tempfile

NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')
FIELDS = ('name', 'enabled', 'order', 'aliases', 'cwd', 'skill', 'rotate')


class AgentsError(Exception):
    """agents.json exists but cannot be used (unreadable, invalid JSON, bad field)."""


def home():
    return os.path.expanduser('~')


def team_dir():
    return os.path.join(home(), '.config', 'team')


def agents_file():
    return os.path.join(team_dir(), 'agents.json')


def instance_config(name):
    return os.path.join(home(), '.config', 'agent', name, 'config', 'config.json')


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f: return json.load(f)
    except Exception:
        return None


def instances():
    """{name: config dict} for every instance of the agent plugin (~/.config/agent/<name>/config/config.json)."""
    out = {}
    for f in sorted(glob.glob(os.path.join(home(), '.config', 'agent', '*', 'config', 'config.json'))):
        name = f.split(os.sep)[-3]
        c = _read_json(f)
        if NAME_RE.match(name) and isinstance(c, dict): out[name] = c
    return out


def _session(cfg):
    s = (cfg or {}).get('session')
    return s if isinstance(s, dict) else {}


def validate(data):
    """Checks the parsed file; returns the list of entries (dicts, unknown fields kept). Raises AgentsError."""
    if not isinstance(data, list):
        raise AgentsError('agents.json precisa ser uma lista: [{"name": "...", "enabled": true, "order": 10}]')
    seen, taken = set(), {}
    for i, e in enumerate(data):
        if not isinstance(e, dict): raise AgentsError(f'agents.json, item {i}: precisa ser um objeto')
        n = e.get('name')
        if not isinstance(n, str) or not NAME_RE.match(n):
            raise AgentsError(f'agents.json, item {i}: "name" inválido ({json.dumps(n)}); use letras minúsculas, dígitos e hífen')
        if n in seen: raise AgentsError(f'agents.json: "{n}" aparece duas vezes')
        seen.add(n)
        if 'enabled' in e and not isinstance(e['enabled'], bool): raise AgentsError(f'agents.json, "{n}": "enabled" precisa ser true ou false')
        if 'rotate' in e and not isinstance(e['rotate'], bool): raise AgentsError(f'agents.json, "{n}": "rotate" precisa ser true ou false')
        if 'order' in e and (isinstance(e['order'], bool) or not isinstance(e['order'], (int, float))):
            raise AgentsError(f'agents.json, "{n}": "order" precisa ser um número')
        for k in ('cwd', 'skill'):
            if k in e and (not isinstance(e[k], str) or not e[k].strip()): raise AgentsError(f'agents.json, "{n}": "{k}" precisa ser texto')
        al = e.get('aliases', [])
        if not isinstance(al, list) or not all(isinstance(a, str) and NAME_RE.match(a) for a in al):
            raise AgentsError(f'agents.json, "{n}": "aliases" precisa ser uma lista de nomes (letras minúsculas, dígitos e hífen)')
        for a in al:
            if a in taken and taken[a] != n: raise AgentsError(f'agents.json: o apelido "{a}" aparece em "{taken[a]}" e em "{n}"')
            taken[a] = n
    for a, n in taken.items():
        if a in seen and a != n: raise AgentsError(f'agents.json: "{a}" é agente e apelido de "{n}"')
    return data


def load():
    """(entries, exists). exists False = no file (ENOENT). Raises AgentsError when the file exists but is not usable."""
    path = agents_file()
    try:
        with open(path, encoding='utf-8') as f: text = f.read()
    except FileNotFoundError:
        return [], False
    except OSError as e:
        raise AgentsError(f'não consegui ler {path}: {e}')
    try:
        data = json.loads(text)
    except ValueError as e:
        raise AgentsError(f'agents.json não é JSON válido: {e}')
    return validate(data), True


def effective(entry, inst=None):
    """The entry with every launcher field filled in (defaults and the instance's "session" block)."""
    n = entry['name']
    inst = instances() if inst is None else inst
    cfg = inst.get(n)
    s = _session(cfg)
    cwd = entry.get('cwd') or s.get('cwd') or '~'
    cwd = os.path.expanduser(cwd)
    if len(cwd) > 1: cwd = cwd.rstrip('/')
    return {
        'name': n,
        'enabled': entry.get('enabled', True) is not False,
        'order': entry.get('order'),
        'aliases': list(entry.get('aliases') or []),
        'cwd': cwd,
        'skill': entry.get('skill') or (f'agent {n}' if cfg is not None else n),
        'rotate': entry.get('rotate') if isinstance(entry.get('rotate'), bool) else s.get('rotate') is True,
        'agent_plugin': cfg is not None,
    }


def _sorted(entries):
    keyed = [(e.get('order', (i + 1) * 10), e['name'], e) for i, e in enumerate(entries)]
    return [e for _, _, e in sorted(keyed, key=lambda t: (t[0], t[1]))]


def resolve(query, entries=None):
    """Effective entry for a name or alias; None when unknown. agents.json first, then the instances' configs."""
    if entries is None: entries, _ = load()
    inst = instances()
    for e in entries:
        if query == e['name'] or query in (e.get('aliases') or []): return effective(e, inst)
    listed = {e['name'] for e in entries}
    for n, cfg in inst.items():
        if n in listed: continue
        al = _session(cfg).get('aliases') or []
        if query == n or (isinstance(al, list) and query in al):
            return effective({'name': n, 'aliases': [a for a in al if isinstance(a, str)]}, inst)
    return None


def all_agents(entries=None):
    """Effective entries of agents.json in order, then instances missing from it (flag "listed": False)."""
    if entries is None: entries, _ = load()
    inst = instances()
    out = [dict(effective(e, inst), listed=True) for e in _sorted(entries)]
    listed = {e['name'] for e in entries}
    for n, cfg in inst.items():
        if n not in listed:
            al = [a for a in (_session(cfg).get('aliases') or []) if isinstance(a, str)]
            out.append(dict(effective({'name': n, 'aliases': al}, inst), listed=False))
    return out


# --- migration and writers ---

def _parse_seed(seed):
    if seed is None: return []
    data = json.loads(seed) if isinstance(seed, str) else seed
    return validate(data)


def build_initial(seed=None):
    """The list written by the migration: the seed entries plus every instance of the agent plugin."""
    inst = instances()
    by = {}
    for e in _parse_seed(seed):
        by[e['name']] = {k: e[k] for k in FIELDS if k in e and k not in ('enabled', 'order')}
    for n, cfg in inst.items():
        if n in by: continue
        s = _session(cfg)
        e = {'name': n}
        al = [a for a in (s.get('aliases') or []) if isinstance(a, str) and NAME_RE.match(a)]
        if al: e['aliases'] = al
        e['cwd'] = s.get('cwd') or '~'
        e['skill'] = f'agent {n}'
        e['rotate'] = s.get('rotate') is True
        by[n] = e
    sessions = {os.path.basename(p)[:-len('.session')] for p in glob.glob(os.path.join(team_dir(), '*.session'))}
    out = []
    for i, n in enumerate(sorted(by)):
        e = by[n]
        enabled = n in sessions or (inst.get(n) or {}).get('live') is True
        out.append({'name': n, 'enabled': enabled, 'order': (i + 1) * 10, **{k: v for k, v in e.items() if k != 'name'}})
    return validate(out)


def render(entries):
    return json.dumps(entries, ensure_ascii=False, indent=2) + '\n'


def migrate(seed=None):
    """Writes agents.json once when it does not exist. Returns the number of agents written, or None when it already
    existed (never replaced, even when it appears while this runs)."""
    path = agents_file()
    if os.path.lexists(path): return None
    entries = build_initial(seed)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.agents.', suffix='.tmp', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f: f.write(render(entries))
        os.chmod(tmp, 0o644)
        try:
            os.link(tmp, path)           # fails if the file appeared meanwhile: the other writer wins
        except FileExistsError:
            return None
    finally:
        try: os.unlink(tmp)
        except OSError: pass
    return len(entries)


def _write_locked(change):
    path = agents_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.lock', 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        entries, _ = load()
        entries = validate(change([dict(e) for e in entries]))
        fd, tmp = tempfile.mkstemp(prefix='.agents.', suffix='.tmp', dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f: f.write(render(entries))
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try: os.unlink(tmp)
            except OSError: pass
            raise
    return entries


def upsert(entry):
    """Adds or updates one agent (fields given replace the old ones; the rest of the entry and the file are kept).
    A new agent gets order = last + 10 when it has none."""
    if not isinstance(entry, dict) or not NAME_RE.match(str(entry.get('name', ''))):
        raise AgentsError('upsert precisa de {"name": "..."} com letras minúsculas, dígitos e hífen')

    def change(entries):
        for e in entries:
            if e['name'] == entry['name']:
                e.update(entry); return entries
        new = dict(entry)
        if 'order' not in new:
            orders = [e.get('order', (i + 1) * 10) for i, e in enumerate(entries)]
            new['order'] = (max(orders) if orders else 0) + 10
        return entries + [new]
    return _write_locked(change)


def remove(name):
    """Removes one agent from the file (the extension then closes its terminal). Unknown name: nothing changes."""
    return _write_locked(lambda entries: [e for e in entries if e['name'] != name])


# --- CLI ---

def _usage(entries):
    names = [a['name'] for a in all_agents(entries)]
    aliases = [x for a in all_agents(entries) for x in a['aliases']]
    msg = 'uso: team ' + ('|'.join(names) or '<agente>') + ' [new]'
    if aliases: msg += '\n     (atalhos: ' + ', '.join(aliases) + ')'
    return msg


def _seed_arg(argv):
    if '--seed' not in argv: return None
    i = argv.index('--seed')
    if i + 1 >= len(argv): raise SystemExit('--seed precisa de um arquivo (ou - para a entrada padrão)')
    src = argv[i + 1]
    return sys.stdin.read() if src == '-' else open(src, encoding='utf-8').read()


def main(argv):
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__.split('\n\n')[1]); return 0
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == 'migrate':
            n = migrate(_seed_arg(rest))
            print(f'agents.json criado com {n} agentes: {agents_file()}' if n is not None else f'agents.json já existe: {agents_file()}')
            return 0
        if cmd == 'resolve':
            if not rest or rest[0].startswith('-'): raise SystemExit('uso: team_agents.py resolve <nome-ou-apelido> [--seed ARQ|-]')
            seed = _seed_arg(rest)
            if seed is not None and not os.path.lexists(agents_file()):
                n = migrate(seed)
                if n is not None: print(f'>> agents.json criado com {n} agentes ({agents_file()})', file=sys.stderr)
            entries, _ = load()
            a = resolve(rest[0], entries)
            if not a:
                print(_usage(entries), file=sys.stderr); return 2
            print(a['name']); print(a['cwd']); print(a['skill'])
            print('1' if a['rotate'] else '0'); print('1' if a['agent_plugin'] else '0')
            return 0
        if cmd == 'list':
            agents = all_agents()
            if '--json' in rest:
                print(json.dumps(agents, ensure_ascii=False, indent=2)); return 0
            for a in agents:
                flags = ('' if a['enabled'] else ' (desligado)') + ('' if a['listed'] else ' (fora do agents.json)')
                al = f" [{', '.join(a['aliases'])}]" if a['aliases'] else ''
                print(f"{a['name']}{al}{flags}\t{a['cwd']}\t/{a['skill']}")
            return 0
    except AgentsError as e:
        print(f'team: {e}. Corrija o arquivo {agents_file()} (nada foi regravado).', file=sys.stderr)
        return 3
    print(f'comando desconhecido: {cmd}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
