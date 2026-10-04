"""role_edit: applies the Papel tab's edits (Planou `agent_docs_changed`) to this instance and answers them.

The person edits, in Planou's Papel tab, what the manifest (agent.py docs_manifest) offers: instructions.md, CONTEXT.md
(when the instance has one), the list of behaviors that are on (the catalog, with each behavior's options schema in
`options`) and the options of the behaviors (behavior_config.json: the "behavior_config" object of config.json as
text, options_text()). Planou never writes on the machine: the edit is a pending version and an
`agent_docs_changed` event. The heavy tick hands the event here (watch_core.planou set_docs_handler, called from apply_events before the cursor is acknowledged) and, per event:

  1. picks the target by `kind` only: instructions -> instructions.md, context -> CONTEXT.md, behaviors -> "behaviors"
     of config.json, behavior_config -> "behavior_config" of config.json (PLN0222). Anything else (a BEHAVIOR.md, the
     core SKILL.md) is refused: a path never comes from the event, so the plugin's BEHAVIOR.md is never edited this way
     (it changes by PR, for every agent);
  2. checks the content against the event's sha256, the size (200 KB) and, for the list, the catalog; the options must
     be {behavior of the catalog: {option: value}} and each behavior with a spec (schema.BEHAVIOR_OPTIONS) takes only
     its options, with their types; an option of type 'command' (run as a shell command on this machine) or 'path' (a
     file or folder that decides what code runs, or what the session writes or deletes here) may not be changed, added or
     removed from Planou (PLN0229, PLN0233: whoever gets into Planou would run code or overwrite files here), and neither
     may the options of a behavior of the instance itself (behaviors/<name>/, also one that shadows the plugin's: it has
     no schema, so nothing here says how it uses them), so the edit keeps them as they are here or is refused; checks that the file here is still the version the edit started from
     (`base_sha256`), else refuses;
  3. runs config-snapshot (`config-snapshot --wait`, AGENT_SNAPSHOT_CMD overrides, empty turns it off; missing = skip)
     and writes the content exactly as it came (bytes), then reads it back and checks the sha256;
  4. runs `agent.py <instance> --validate`; on failure puts the previous bytes back and refuses with the reason;
  5. answers POST /v1/agent/docs/ack (applied with the sha256, or refused with the reason) and prints for the session
     `== PAPEL MUDOU` (what to read again) or `== PAPEL RECUSADO` (tell the user why).

cache/planou/role_edits.json keeps each version's result, so a tick that could not reach Planou answers the same thing
again without writing twice. An edit whose content is already here is answered `applied` without writing.
"""
import hashlib, json, os, shlex, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths, schema                                            # noqa: E402
from watch_core import behavior_names                          # noqa: E402

MAX_BYTES = 200_000                     # Planou's limit for a content
CATALOG_MAX = 50
KINDS = {'instructions': 'instructions.md', 'context': 'CONTEXT.md', 'behaviors': 'comportamentos ligados',
         'behavior_config': 'opcoes dos comportamentos'}
READ_ONLY = {'behavior': 'o BEHAVIOR.md de um comportamento muda por PR no plugin, para todos os agentes que o usam',
             'core': 'o SKILL.md do plugin muda por PR no plugin'}
COMMAND_REFUSED = 'opção de comando: edite no computador'
PATH_REFUSED = 'opção de caminho: edite no computador'
LOCAL_REFUSED = 'comportamento local: edite no computador'
OPTIONS_NAME = 'behavior_config.json'   # the name of the options in the manifest (they live in config.json)
SNAPSHOT_CMD = 'config-snapshot --wait'
KEEP = 50
WORK_WATCH = 'work-triage'              # the behavior that turns on work-watch's tick (agent.py; old name work-watch)
_OUT = {'applied': [], 'refused': [], 'config': False}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def behaviors_content(names):
    """The list of behaviors as Planou versions it (AgentDocs.BehaviorsContent): a sorted JSON array, no spaces."""
    return json.dumps(sorted(set(names)), separators=(',', ':'))


FRONT_KEYS = {'title': 80, 'summary': 300, 'layer': 20, 'kind': 20}
# `when` (when to read an on_demand behavior, for the index of --load) stays on the machine: Planou does not know it, so
# frontmatter() leaves it out unless asked (load_meta), and the catalog never carries it.
LOAD_KEYS = dict(FRONT_KEYS, when=300)
LAYERS = ('rules', 'instructions', 'skill', 'memory')
BEHAVIOR_KINDS = ('always', 'on_demand')


def _front_lines(fh):
    """The `key: value` lines of the frontmatter at the top of a BEHAVIOR.md (between two `---` lines), consuming them
    from the open file; [] and nothing consumed past the first line when there is none."""
    first = fh.readline()
    if first.strip() != '---':
        return [], first
    lines = []
    for line in fh:
        if line.strip() == '---': return lines, ''
        lines.append(line)
    return [], ''                       # never closed: not a frontmatter


def frontmatter(f, keys=FRONT_KEYS):
    """The optional frontmatter of a BEHAVIOR.md, for Planou's Papel tab: `title` (its name in plain Portuguese, one
    line, up to 80), `summary` (one sentence of what it does, up to 300), and `layer` (rules, instructions, skill or
    memory) and `kind` (always or on_demand), kept only with a known value. {} when the file has none or cannot be read.
    `keys` = LOAD_KEYS also keeps `when` (load_meta)."""
    out = {}
    try:
        with open(f, encoding='utf-8') as fh:
            lines, _ = _front_lines(fh)
    except (OSError, UnicodeDecodeError):
        return out
    for line in lines:
        k, sep, v = line.partition(':')
        k, v = k.strip(), ' '.join(v.split())
        if not sep or k not in keys or not v: continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'': v = v[1:-1].strip()
        if k == 'layer' and v not in LAYERS: continue
        if k == 'kind' and v not in BEHAVIOR_KINDS: continue
        if k == 'title' and len(v) > keys['title']: continue
        out[k] = v[:keys[k]]
    return out


def load_meta(f):
    """Where a behavior goes in --load: its layer (default skill), its kind (default always: a behavior without
    frontmatter is read every session, as before), and its title, summary and `when` for the on_demand index."""
    fm = frontmatter(f, LOAD_KEYS) if f else {}
    return dict(fm, layer=fm.get('layer') or 'skill', kind=fm.get('kind') or 'always')


def _description(f):
    """The BEHAVIOR.md heading after "<name>: " (the first line of text when there is no heading, past the
    frontmatter), up to 300."""
    try:
        with open(f, encoding='utf-8') as fh:
            _, first = _front_lines(fh)
            for line in ([first] if first else []) + list(fh):
                t = line.strip()
                if not t: continue
                t = t.lstrip('#').strip()
                if ':' in t and line.startswith('#'): t = t.split(':', 1)[1].strip()
                return ' '.join(t.split())[:300] or None
    except (OSError, UnicodeDecodeError):
        return None
    return None


LOCAL_NOTE = 'Opcoes: só no computador (comportamento local).'


def catalog():
    """The behaviors this instance can turn on: its own (behaviors/<name>/) and the plugin's, by name. A plugin behavior
    with a schema goes with `options` (schema.planou_options: the Papel tab edits them in a form, the machine-only ones
    read only; Planou 0.61.0 or later); a local one goes without, and its description says its options are edited on
    the machine only. `title` and `summary` (and `layer` and `kind`, when known) come from the frontmatter of the
    BEHAVIOR.md (frontmatter()); watch_core.planou drops them for a Planou that does not know them yet."""
    names = set(paths.plugin_behaviors())
    try:
        names.update(behavior_names.behavior_name(d) for d in os.listdir(paths.LOCAL_BEHAVIORS or '') if paths.behavior_file(d))
    except OSError:
        pass
    local = local_behaviors()
    out = []
    for n in sorted(n for n in names if paths.behavior_file(n)):
        item = {'name': n}
        d = _description(paths.behavior_file(n))
        if n in local:
            room = 300 - len(LOCAL_NOTE) - 1
            d = ((d if len(d) <= room else d[:room - 1].rstrip() + '…') + ' ' if d else '') + LOCAL_NOTE
        else:
            opts = schema.planou_options(n)
            if opts: item['options'] = opts
        if d: item['description'] = d
        item.update(frontmatter(paths.behavior_file(n)))
        out.append(item)
    return out[:CATALOG_MAX]


# ---------------------------------------------------------------- the options (behavior_config)

def _options_file():
    return os.path.join(paths.CACHE_DIR, 'planou', 'behavior_config.txt')


def options_text(bc):
    """The options as the manifest shows them: the text of the last edit applied from Planou when it still says the same
    (so the version Planou has keeps its sha256), else indented JSON in the file's order."""
    bc = bc if isinstance(bc, dict) else {}
    kept = _read(_options_file())
    if kept is not None:
        try:
            text = kept.decode('utf-8')
            if json.loads(text) == bc: return text
        except (UnicodeDecodeError, ValueError):
            pass
    return json.dumps(bc, ensure_ascii=False, indent=2)


def _options_on_disk(raw):
    try: bc = schema.normalize(raw).get('behavior_config')
    except schema.ConfigError: return None
    return bc if isinstance(bc, dict) else {}


def editable_content(data):
    """The text Planou may edit, or None: valid UTF-8 up to 200 KB (the sha256 goes over these same bytes)."""
    if len(data) > MAX_BYTES: return None
    try: return data.decode('utf-8')
    except UnicodeDecodeError: return None


def work_watch(cfg):
    """A work-watch instance: the behavior on, or a work-watch-<x> name whose config lists no behaviors (agent.py)."""
    b = behavior_names.current(cfg.get('behaviors'))
    return WORK_WATCH in b or (not b and (paths.NAME or '').startswith(paths.WORK_WATCH_PREFIX))


def minimum(cfg):
    """Confidentiality "minimum" (the default of a work-watch instance, as in planou_tick.liga and
    watch_core.planou.configure_agent): no content leaves the machine and only the list of behaviors is edited from
    Planou."""
    default = 'minimum' if work_watch(cfg) else 'title'
    return ((cfg.get('planou') or {}).get('confidentiality') or default) == 'minimum'


# ---------------------------------------------------------------- files

def _read(path):
    try:
        with open(path, 'rb') as f: return f.read()
    except OSError:
        return None


def _write(path, data):
    """Atomic write of bytes (temporary file in the same folder, then os.replace), keeping the old file's mode."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f: f.write(data)
        try: os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError: os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try: os.remove(tmp)
        except OSError: pass
        raise


def _label(path):
    home = os.path.expanduser('~')
    return '~' + path[len(home):] if path.startswith(home + os.sep) else path


def snapshot():
    """config-snapshot before writing (git of ~/.config). Returns a note when it did not run; never stops the edit (the
    previous bytes are kept here for the rollback)."""
    cmd = os.environ.get('AGENT_SNAPSHOT_CMD', SNAPSHOT_CMD)
    argv = shlex.split(cmd) if cmd else []
    if not argv: return 'config-snapshot desligado'
    exe = shutil.which(os.path.expanduser(argv[0]))
    if not exe: return f'{argv[0]} nao encontrado'
    try:
        r = subprocess.run([exe] + argv[1:], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=120)
        return f'config-snapshot saiu com {r.returncode}' if r.returncode else None
    except (OSError, subprocess.TimeoutExpired) as e:
        return f'config-snapshot falhou ({type(e).__name__})'


def validate():
    """`agent.py <instance> --validate` in a subprocess: (ok, the error lines in one line)."""
    agent_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agent.py')
    try:
        r = subprocess.run([sys.executable, agent_py, paths.NAME, '--validate'], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f'--validate nao rodou ({type(e).__name__})'
    if r.returncode == 0: return True, None
    errs = [l[len('ERRO:'):].strip() for l in (r.stdout or '').splitlines() if l.startswith('ERRO:')]
    why = '; '.join(errs) or ((r.stderr or r.stdout or '').strip().splitlines() or ['sem detalhe'])[-1]
    return False, ' '.join(f'--validate falhou: {why}'.split())[:280]


# ---------------------------------------------------------------- one edit

class Refused(Exception):
    pass


def _behaviors_target(cfg, content):
    """(config.json path, current bytes, new bytes, current sha of the list, what changed, sha of the list as written)
    for a list of behaviors. The list written is the edit's, except that work-watch stays on (see below)."""
    try: names = json.loads(content)
    except ValueError: raise Refused('a lista de comportamentos nao e JSON')
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise Refused('a lista de comportamentos precisa ser uma lista de nomes')
    # a list Planou stored before the rename (PLN0295) names the behaviors by their old names: read as the new ones
    names = behavior_names.current(names)
    known = {c['name'] for c in catalog()}
    unknown = [n for n in names if n not in known]
    if unknown: raise Refused(f'{unknown[0]} nao existe nesta instancia (nem no plugin nem em behaviors/ dela)')
    old = _read(paths.CONFIG)
    if old is None: raise Refused('config.json nao encontrado')
    try: raw = json.loads(old)
    except ValueError: raise Refused('config.json ilegivel')
    have = raw.get('behaviors') if isinstance(raw.get('behaviors'), list) else []
    # what is on now comes from the file (an edit earlier in this tick already changed it), never from the tick's config
    before = set(_behaviors_on_disk(raw))
    alias = behavior_names.behavior_name
    target = set(names)
    # the order is the session's load order: the ones kept stay where they are, the new ones go at the end; a name with
    # no BEHAVIOR.md (Planou never saw it) is left alone
    new = [b for b in have if alias(b) in target or not (isinstance(b, str) and paths.behavior_file(alias(b)))]
    kept = {alias(b) for b in new}
    new += [n for n in names if n not in kept and not kept.add(n)]
    if not have and new and WORK_WATCH not in map(alias, new) and work_watch({}):
        new.insert(0, WORK_WATCH)     # a work-watch-<x> with no list ran work-watch's tick: turning one on keeps it
    raw['behaviors'] = new
    indent = 2
    lines = old.decode('utf-8', 'replace').splitlines()
    data = _dump_config(old, raw)
    on, off = sorted(target - before), sorted(before - target)
    what = '; '.join(x for x in (('+ ' + ', '.join(on)) if on else '', ('- ' + ', '.join(off)) if off else '') if x)
    written = sha256(behaviors_content(_behaviors_on_disk(raw)).encode())
    return paths.CONFIG, old, data, sha256(behaviors_content(before).encode()), what, written, \
        sha256(behaviors_content(names).encode()), _old_spellings(before)


def _dump_config(old, raw):
    """config.json rewritten with the indentation it had."""
    indent = 2
    lines = old.decode('utf-8', 'replace').splitlines()
    if len(lines) > 1 and lines[1][:1] in (' ', '\t'):
        indent = len(lines[1]) - len(lines[1].lstrip(' ')) or 2
    return (json.dumps(raw, ensure_ascii=False, indent=indent) + '\n').encode('utf-8')


def local_behaviors():
    """The behaviors whose BEHAVIOR.md is the instance's own (behaviors/<name>/BEHAVIOR.md), also one that shadows the
    plugin's: the session reads that file, which declares no schema, so their options are edited on the machine only."""
    base = paths.LOCAL_BEHAVIORS
    try: names = os.listdir(base or '')
    except OSError: return set()
    return {behavior_names.behavior_name(n) for n in names if os.path.isfile(os.path.join(base, n, 'BEHAVIOR.md'))}


def _local_changes(old, new, local):
    """The local behaviors whose options differ between two behavior_config objects (missing and {} are the same)."""
    def opts(bc, n):
        v = bc.get(n)
        return v if v not in (None, {}) else {}
    return sorted(n for n in local if opts(old, n) != opts(new, n))


def _options_target(content):
    """(config.json path, current bytes, new bytes, current sha of the options, the options) for an edit of
    behavior_config: an object with one object per behavior of the catalog; the options of a local behavior exactly as
    they are here (local_behaviors); a behavior with a spec takes only its options, with their types
    (schema.option_problems), and the machine-only options (command, path) exactly as they are here
    (schema.machine_changes)."""
    try: bc = json.loads(content)
    except ValueError: raise Refused('as opcoes nao sao JSON')
    if not isinstance(bc, dict) or not all(isinstance(v, dict) for v in bc.values()):
        raise Refused('as opcoes precisam ser {comportamento: {opcao: valor}}')
    bc = _resolved_options(bc)          # options Planou stored under an old name (PLN0295) belong to the new one
    known = {c['name'] for c in catalog()}
    unknown = [n for n in bc if n not in known]
    if unknown: raise Refused(f'{unknown[0]} nao existe nesta instancia (nem no plugin nem em behaviors/ dela)')
    old = _read(paths.CONFIG)
    if old is None: raise Refused('config.json nao encontrado')
    try: raw = json.loads(old)
    except ValueError: raise Refused('config.json ilegivel')
    current = _options_on_disk(raw)
    if current is None: raise Refused('config.json ilegivel')
    local = local_behaviors()
    changed = _local_changes(current, bc, local)
    if changed: raise Refused(f'{LOCAL_REFUSED} ({", ".join(changed)})')
    for name, opts in bc.items():
        if name in local: continue                     # unchanged: what is here, read by the instance's own BEHAVIOR.md
        err, extra = schema.option_problems(name, opts)
        if err or extra: raise Refused(' '.join((err + extra)[0].split()))
    changed = schema.machine_changes(current, bc)
    if changed:
        by = {}
        for o in changed:
            by.setdefault(schema.option_type(*o.split('.', 1)), []).append(o)
        raise Refused('; '.join(f'{msg} ({", ".join(by[k])})' for k, msg in ((schema.COMMAND, COMMAND_REFUSED),
                                                                              (schema.PATH, PATH_REFUSED)) if by.get(k)))
    shown_before = _options_shown_before(current, raw)
    raw['behavior_config'] = bc
    return paths.CONFIG, old, _dump_config(old, raw), sha256(options_text(current).encode('utf-8')), bc, shown_before


def _resolved_options(bc):
    """behavior_config with the old behavior names read as the new ones (schema.normalize's rule)."""
    c = {'behavior_config': dict(bc)}
    schema._behavior_aliases(c)
    return c['behavior_config']


def _options_shown_before(current, raw):
    """The sha of the options as a runner before PLN0295 showed them, when they name a behavior by its old name and say
    the same as `current`: the last text applied from Planou, or config.json's "behavior_config" as indented JSON. Planou
    may still hold that version as the edit's base until the heartbeat brings the new manifest."""
    out, texts = set(), []
    kept = _read(_options_file())
    try: texts.append(kept.decode('utf-8') if kept is not None else None)
    except UnicodeDecodeError: pass
    bc = raw.get('behavior_config')
    if isinstance(bc, dict): texts.append(json.dumps(bc, ensure_ascii=False, indent=2))
    for text in texts:
        try: v = json.loads(text) if text else None
        except ValueError: continue
        if isinstance(v, dict) and v != current and _resolved_options(v) == current: out.add(sha256(text.encode('utf-8')))
    return out


def _options_match(path, bc):
    try: return _options_on_disk(json.loads(_read(path) or b'{}')) == bc
    except ValueError: return False


def _file_target(kind):
    path = paths.INSTRUCTIONS if kind == 'instructions' else paths.CONTEXT
    if not path or not os.path.isfile(path):
        raise Refused(f'{KINDS[kind]} nao existe nesta instancia')
    old = _read(path)
    if old is None: raise Refused(f'{KINDS[kind]} ilegivel')
    return path, old, sha256(old)


def apply(cfg, p):
    """Applies one edit. Returns (result, reason, line): result 'applied' or 'refused', the reason when refused and the
    line for the session."""
    kind, content, want = p.get('kind'), p.get('content'), str(p.get('sha256') or '').lower()
    base = str(p.get('base_sha256') or '').lower() or None
    by = (p.get('by') or {}).get('display_name') or (p.get('by') or {}).get('name')
    what = f'{KINDS.get(kind) or p.get("name") or kind} v{p.get("version")}' + (f' (por {" ".join(str(by).split())[:60]})' if by else '')
    try:
        if kind not in KINDS:
            raise Refused(READ_ONLY.get(kind) or f'{kind}: nao se edita pelo Planou')
        if not isinstance(content, str): raise Refused('evento sem content')
        data = content.encode('utf-8')
        if len(data) > MAX_BYTES: raise Refused('conteudo maior que 200 KB')
        if sha256(data) != want: raise Refused('o sha256 do evento nao confere com o content')
        bases = set()
        if kind == 'behaviors':
            path, old, new, current, change, written, want, spellings = _behaviors_target(cfg, content)
            reread = lambda: _behaviors_sha_on_disk() == written
            bases |= spellings
        elif kind == 'behavior_config':
            if minimum(cfg): raise Refused('confidencialidade minima: as opcoes nao se editam pelo Planou')
            path, old, new, current, bc, shown_before = _options_target(content)
            change = None
            bases |= shown_before
            reread = lambda: _options_match(path, bc)
        else:
            if minimum(cfg): raise Refused('confidencialidade minima: o conteudo nao se edita pelo Planou')
            path, old, current = _file_target(kind)
            new, change = data, None
            reread = lambda: sha256(_read(path) or b'') == want
        reread_hint = (f'agent.py {paths.NAME} --load (e o BEHAVIOR.md de cada comportamento ligado)' if kind == 'behaviors'
                       else f'"behavior_config" em {_label(path)}' if kind == 'behavior_config' else _label(path))
        applied_line = (f'-- {what}: aplicada' + (f' ({change})' if change else '') + f' -> reler {reread_hint}')
        if current == want:
            return 'applied', None, applied_line + ' (ja estava assim)'
        if base and base != current and base not in bases:
            raise Refused(f'{KINDS[kind]} mudou aqui depois da versao em que a edicao foi feita (base {base[:12]}, aqui '
                          f'{current[:12]}); o manifesto novo mostra o atual')
        note = snapshot()
        _write(path, new)
        ok, why = (True, None) if reread() else (False, 'o arquivo gravado nao confere com o sha256')
        if ok: ok, why = validate()
        if not ok:
            _write(path, old)
            raise Refused(f'{why}; voltou ao anterior')
        if path == paths.CONFIG: _OUT['config'] = True
        if kind == 'behavior_config': _write_kept(content)
        return 'applied', None, applied_line + (f' [{note}]' if note and note != 'config-snapshot desligado' else '')
    except Refused as e:
        reason = ' '.join(str(e).split())[:300]
        return 'refused', reason, f'-- {what}: {reason} -> o arquivo ficou como estava; avisar o usuario'


def _write_kept(text):
    """The options exactly as Planou sent them, for options_text(); a failure here only costs a new "observed" version."""
    try:
        os.makedirs(os.path.dirname(_options_file()), exist_ok=True)
        _write(_options_file(), text.encode('utf-8'))
    except OSError:
        pass


def _behaviors_on_disk(raw):
    """The behaviors on in a config.json as Planou sees them (old names read as the new ones, only with a BEHAVIOR.md)."""
    names = [behavior_names.behavior_name(b) for b in raw.get('behaviors') or [] if isinstance(b, str)]
    return [b for b in names if paths.behavior_file(b)]


def _old_spellings(names, limit=256):
    """The sha of every spelling of the list `names` with old behavior names (PLN0295), the current one left out. Planou
    keeps the list as the runner showed it, or as the person sent it, so the base of an edit may name a behavior by its
    old name until the heartbeat brings the new manifest: it is the same list, so it is no conflict."""
    import itertools
    choices = [[n] + behavior_names.old_names(n) for n in sorted(set(names))]
    out = set()
    for combo in itertools.islice(itertools.product(*choices), limit):
        out.add(sha256(behaviors_content(combo).encode()))
    out.discard(sha256(behaviors_content(names).encode()))
    return out


def _behaviors_sha_on_disk():
    try: raw = json.loads(_read(paths.CONFIG) or b'{}')
    except ValueError: return None
    return sha256(behaviors_content(_behaviors_on_disk(raw)).encode())


# ---------------------------------------------------------------- the handler and the output

def _records_file():
    return os.path.join(paths.CACHE_DIR, 'planou', 'role_edits.json')


def _records():
    try:
        with open(_records_file()) as f: r = json.load(f)
        return r if isinstance(r, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(recs):
    keep = dict(sorted(recs.items(), key=lambda kv: (kv[1] or {}).get('at') or '')[-KEEP:])
    os.makedirs(os.path.dirname(_records_file()), exist_ok=True)
    _write(_records_file(), json.dumps(keep, ensure_ascii=False, indent=1).encode())


def handle(cfg, ev, now=None):
    """apply_events' handler for one agent_docs_changed: applies it once (role_edits.json), answers Planou and returns
    False when the answer did not get there (the event waits for the next tick, which only answers again)."""
    from watch_core import planou
    p = ev.get('payload') or {}
    vid = str(p.get('version_id') or '')
    if not vid: return True
    recs = _records()
    r = recs.get(vid)
    if not isinstance(r, dict):
        try:
            result, reason, line = apply(cfg, p)
        except Exception as e:                  # never leave an edit unanswered
            result, reason = 'refused', f'erro ao aplicar ({type(e).__name__})'
            line = f'-- {KINDS.get(p.get("kind")) or p.get("kind")} v{p.get("version")}: {reason} -> avisar o usuario'
        r = {'result': result, 'sha256': str(p.get('sha256') or '').lower() if result == 'applied' else None,
             'reason': reason, 'kind': p.get('kind'), 'version': p.get('version'),
             'at': (now or datetime.now(timezone.utc)).isoformat(timespec='seconds'), 'acked': False}
        recs[vid] = r
        _save(recs)
        _OUT[result].append(line)
    if r.get('acked'): return True
    done, note = planou.docs_ack(vid, r['result'], r.get('sha256'), r.get('reason'))
    if done:
        r.update(acked=True, ack_note=note)
        _save(recs)
    return done


def handler(cfg):
    return lambda ev: handle(cfg, ev)


def config_changed():
    """True when an edit of this tick rewrote config.json (the manifest then comes from the config on disk)."""
    return _OUT['config']


def block():
    """The blocks for the session (None when nothing happened this tick): `== PAPEL MUDOU (n)` says what to read again
    before going on, `== PAPEL RECUSADO (n)` what was refused and why."""
    out = []
    if _OUT['applied']:
        out += [f'== PAPEL MUDOU ({len(_OUT["applied"])}): edicao feita no Planou, aba Papel; reler antes de seguir '
                f'(sem stop/start: relancar como sempre, FIRST_NOW=0)'] + _OUT['applied']
    if _OUT['refused']:
        if out: out.append('')
        out += [f'== PAPEL RECUSADO ({len(_OUT["refused"])}): edicao do Planou nao aplicada'] + _OUT['refused']
    _OUT['applied'], _OUT['refused'] = [], []
    return '\n'.join(out) or None
