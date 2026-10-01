#!/usr/bin/env python3
"""Local provisioner: creates the agent instances a person asked for on Planou's Time screen (PLN0111).

A user service (systemd, see ../provisioner/) that follows Planou through /v1/provisioner (Planou's side is PLN0110)
and, for each agent the person asked for:

  1. reports `creating`, makes ~/.config/agent/<name>/ from the template (config-example/dev: config.json and
     instructions.md, with the roles as behaviors) and a marker data/provisioned.json with the agent id;
  2. runs `agent.py <name> --validate` (before the key leaves the vault, so a broken config never burns it);
  3. picks up the key once and writes it to secrets/planou.env (0600, folder 0700); reports `validated`;
  4. runs `config-snapshot` (when it exists; a failure does not stop the flow);
  5. adds the instance to ~/.config/team/agents.json (the Team Terminals extension opens `team <name>`) and reports
     `terminal_open`; later, `runner_up` once cache/runner/runner.pid names a live runner of the instance. No runner
     `session_timeout_s` (120) after a live `terminal_open` it reported: `terminal_open` again, with NO_SESSION as the
     detail (PLN0217: the extension lost the agents.json change and nothing opened the terminal).

Adopted agents (PLN0188): every pass also sends Planou the inventory of the machine (the agents of
~/.config/team/agents.json and the agent plugin's instances missing from it, through team_agents) with the state of
each runner: up, stopped (turned off) or crashed (turned on, runner not up on two passes in a row). A name that matches
an employee links the two; for those the provisioner follows "desired" without ever creating, changing the config of
or removing the instance: `paused` stops the runner (runner.sh of the agent plugin, or <skill>/scripts/runner.sh of an
agent with a plugin of its own, resolved by team_agents.resolve) and turns the entry off in agents.json (the Team
Terminals extension closes the terminal); `run` turns it on again (the extension opens `team <name>`).

It also follows "desired": `paused` stops the runner, turns the entry off in agents.json and reports `paused`; `run`
after that turns it on again (`terminal_open`); `remove` stops the runner, takes the entry out of agents.json, deletes
the secrets, moves the instance folder to ~/.config/agent-provisioner/removed/ and reports `removed`. It removes only an
instance it created (the marker with the same agent id). A new key put in the vault (the person clicked "Gerar chave
nova") is picked up and written over the old one, and a failed agent is tried again with it.

An employee that left this computer (PLN0205: moved to another computer, or taken off when the computer was revoked;
Planou's PLN0203) is no longer in the list, and its old key is revoked. Only on a valid answer of the list (never on a
network error, a Planou down, a 401 or an answer out of shape) each instance with this provisioner's marker whose agent
id the list no longer has is archived: the runner stopped, the entry taken out of agents.json (team_agents.remove), the
secrets deleted and the folder (with the team .session) moved to ~/.config/agent-provisioner/departed/, data kept.
Nothing goes to Planou about it (the employee is another computer's now). An instance without the marker (an adopted
one, or one made by hand) is never touched.

Secrets: the key is never printed, logged, passed as a process argument or put in an environment variable; it goes
from the HTTP answer straight to the 0600 file. The provisioner's own credential (PLANOU_PROVISIONER_KEY=pl_pv_...) is
read from ~/.config/agent-provisioner/secrets/planou.env, which must be 0600, and stays in memory. It comes from
`provisioner.py pair <code>` (PLN0196: the code of Configurações > Computadores, one credential per computer, which
Planou accepts from anywhere since PLN0191, e.g. https://app.planou.com/v1) or from Planou's
scripts/connect-provisioner.sh (the server's own, accepted only on that machine: http://127.0.0.1:5068/v1). The base URL
is https, or http on loopback only; HTTP proxies are ignored (Planou refuses the script credential through a proxy).

  ~/.config/agent-provisioner/
    config.json        optional: base_url, interval_s, template, agents_file, snapshot_cmd, runner, retry_s,
                       session_timeout_s
    secrets/planou.env PLANOU_PROVISIONER_KEY=pl_pv_...   (0600)
    data/state.json    retry and terminal_open bookkeeping per agent id (never a key)
    removed/           instances taken out by a removal, without their secrets
    departed/          instances of employees that left this computer (PLN0205), without their secrets
    provisioner.lock   one provisioner at a time

Usage:
  provisioner.py run [--interval S]   loop (the service)
  provisioner.py once                 one pass and leave
  provisioner.py status               what Planou asks and what exists on this machine (no key)
  provisioner.py check                credential file, base URL and one call to Planou
  provisioner.py pair [<code>] [--base-url URL]
                                      without <code>, read from a hidden prompt (terminal) or stdin (PLN0192);
                                      connect this computer: trades the code of Configurações > Computadores for the
                                      credential (written 0600, never shown) and sets base_url in config.json
"""
import fcntl, json, os, platform, re, shlex, socket, shutil, signal, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPTS)
BEHAVIORS_DIR = os.path.join(SKILL_DIR, 'behaviors')

NAME_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,60}')          # the agents.json rule (no "_"), also blocks path tricks
SECRET_RE = re.compile(r'pl_[a-z]{2}_[A-Za-z0-9_\-]+')
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
STATUSES = ('creating', 'validated', 'terminal_open', 'runner_up', 'paused', 'removed', 'error')
PUBLIC_BASE_URL = 'https://app.planou.com/v1'
DEFAULTS = {'base_url': 'http://127.0.0.1:5068/v1', 'interval_s': 30, 'retry_s': 600, 'template': None,
            'agents_file': '~/.config/team/agents.json', 'snapshot_cmd': 'config-snapshot', 'runner': None,
            'timeout_s': 15, 'session_timeout_s': 120}
BASE_BEHAVIOR = 'planou-queue'
DEFAULT_ROLES = ['dev-worker']
MARKER = 'provisioned.json'


class Fail(Exception):
    """A step failed: the message (pt-BR, one line, never a secret) goes to Planou as the `error` detail."""


class ApiError(Exception):
    def __init__(self, status, code):
        super().__init__(f'HTTP {status} {code}')
        self.status, self.code = status, code


# ---------------------------------------------------------------- paths and small helpers

def home(*p): return os.path.join(os.path.expanduser('~'), *p)
def prov_dir(*p): return home('.config', 'agent-provisioner', *p)
def agent_base(): return home('.config', 'agent')
def team_dir(): return home('.config', 'team')


def redact(text):
    return SECRET_RE.sub('pl_**_***', str(text))


def log(msg):
    print(f'{datetime.now().isoformat(timespec="seconds")} {redact(msg)}', flush=True)


def one_line(text, limit=300):
    t = ' '.join(redact(text).split())
    t = ''.join(ch for ch in t if ch.isprintable())
    return t if len(t) <= limit else t[:limit - 3] + '...'


def write_atomic(path, text, mode=0o600):
    """Temp file in the same folder, fsync, rename: a reader never sees half a file."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.', dir=d)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def read_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except (OSError, ValueError):
        return default


def load_settings():
    cfg = dict(DEFAULTS)
    f = prov_dir('config.json')
    if os.path.exists(f):
        try:
            with open(f) as fh: data = json.load(fh)
        except (OSError, ValueError) as e:
            raise SystemExit(f'provisionador: {f} ilegivel ({e.__class__.__name__})')
        if not isinstance(data, dict): raise SystemExit(f'provisionador: {f} precisa ser um objeto JSON')
        cfg.update({k: v for k, v in data.items() if k in DEFAULTS and v is not None})
    problem = base_url_problem(str(cfg['base_url']))
    if problem: raise SystemExit(f'provisionador: base_url {problem}')
    cfg['base_url'] = str(cfg['base_url']).rstrip('/')
    cfg['template'] = os.path.expanduser(cfg['template'] or os.path.join(SKILL_DIR, 'config-example', 'dev'))
    cfg['agents_file'] = os.path.expanduser(cfg['agents_file'])
    cfg['runner'] = os.path.expanduser(cfg['runner'] or os.path.join(SCRIPTS, 'runner.sh'))
    return cfg


def base_url_problem(url):
    """None when fine: https anywhere (a paired computer, PLN0191), or http only on loopback (the credential never
    crosses a network in clear text)."""
    u = urllib.parse.urlparse(url)
    if u.scheme == 'https' and u.hostname: return None
    if u.scheme == 'http' and u.hostname in LOOPBACK: return None
    return ('precisa ser https (ex.: https://app.planou.com/v1) ou http na propria maquina '
            '(127.0.0.1, localhost ou ::1, sem loopback a credencial iria sem criptografia)')


def normalize_base_url(url):
    """'https://app.planou.com' and 'https://app.planou.com/v1/' both become 'https://app.planou.com/v1'."""
    url = url.strip().rstrip('/')
    return url if url.endswith('/v1') else url + '/v1'


def load_credential():
    """PLANOU_PROVISIONER_KEY from the 0600 file. Refuses a file others can read. Never printed."""
    f = prov_dir('secrets', 'planou.env')
    try: st = os.stat(f)
    except OSError:
        raise SystemExit(f'provisionador: sem a credencial em {f} (conecte este computador com '
                         f'"provisioner.py pair <codigo>", o codigo sai em Configuracoes > Computadores do Planou)')
    if st.st_mode & 0o077:
        raise SystemExit(f'provisionador: {f} precisa ser 0600 (chmod 600 {shlex.quote(f)})')
    with open(f) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith('PLANOU_PROVISIONER_KEY='):
                v = line.split('=', 1)[1].strip().strip('"\'')
                if v: return v
    raise SystemExit(f'provisionador: {f} sem PLANOU_PROVISIONER_KEY')


# ---------------------------------------------------------------- Planou /v1/provisioner

class Planou:
    def __init__(self, base_url, credential, timeout):
        self.base, self._cred, self.timeout = base_url, credential, timeout
        # no proxy, ever: a proxy adds forwarding headers and Planou refuses them (403 not_local)
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

    def _call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data if data is not None else (b'' if method == 'POST' else None),
                                     method=method)
        req.add_header('Authorization', 'Bearer ' + self._cred)
        req.add_header('Accept', 'application/json')
        if data is not None: req.add_header('Content-Type', 'application/json')
        try:
            with self._open(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            code = 'erro'
            try: code = str((json.loads(e.read() or b'{}').get('error') or {}).get('code') or 'erro')
            except (ValueError, AttributeError, OSError): pass
            raise ApiError(e.code, re.sub(r'[^a-z0-9_]', '', code.lower())[:40] or 'erro') from None
        return json.loads(raw or b'{}')

    def agents_body(self):
        return self._call('GET', '/provisioner/agents')

    def agents(self):
        body = self.agents_body()
        return (body.get('agents') if isinstance(body, dict) else None) or []

    def pick_up_key(self, agent_id):
        return self._call('POST', f'/provisioner/agents/{urllib.parse.quote(str(agent_id))}/key')   # no body

    def report(self, agent_id, status, detail=None):
        return self._call('POST', f'/provisioner/agents/{urllib.parse.quote(str(agent_id))}/status',
                          {'status': status, 'detail': detail})

    def inventory(self, items):
        return self._call('POST', '/provisioner/inventory', {'agents': items})

    def computer(self, plugin_version):
        """Tells Planou which plugin version runs on this computer (PLN0193: the Time screen shows it)."""
        return self._call('POST', '/provisioner/computer', {'plugin_version': plugin_version})


def tell_version(api, version):
    """Once per start: the plugin version, for the Time screen. True when done or never possible (a Planou without the
    route answers 404: nothing to tell); False to try again on the next pass. Never stops a pass."""
    if not version: return True
    try:
        api.computer(version)
        return True
    except ApiError as e:
        return e.status in (404, 405, 422)
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------- agents.json (Team Terminals)

def check_agents_file(path):
    """Fail unless agents.json exists and team_agents reads it: nothing is made for a list that cannot take the agent."""
    import team_agents
    if not os.path.lexists(path):
        raise Fail(f'{path} ainda nao existe; rode `team --list` uma vez (ele cria a lista do time) e o provisionador '
                   'tenta de novo')
    try: team_agents.load()
    except team_agents.AgentsError as e:
        raise Fail(one_line(f'{e}; corrija o arquivo e o provisionador tenta de novo')) from None


def set_agent_entry(path, name, enabled, extra=None):
    """enabled True/False: adds or updates the entry; None: takes it out. Always through team_agents.upsert/remove
    (PLN0112: lock and atomic rename, other agents and fields kept); `extra` (cwd, skill, rotate) goes only into a new
    entry. No file yet: an error asking for `team --list`, which creates it from the launcher's table; seeding it here
    would leave out the agents only that table knows, and the extension would close their terminals."""
    import team_agents
    if os.path.abspath(path) != os.path.abspath(team_agents.agents_file()):
        raise Fail(f'agents_file {path} nao e o do team ({team_agents.agents_file()})')
    check_agents_file(path)
    try:
        cur = agent_entry(path, name, strict=True)
        if enabled is None:
            if cur is None: return False
            team_agents.remove(name)
        elif cur is not None:
            if cur.get('enabled', True) is enabled: return False
            team_agents.upsert({'name': name, 'enabled': enabled})
        else:
            team_agents.upsert({'name': name, 'enabled': enabled, **(extra or {})})
    except team_agents.AgentsError as e:
        raise Fail(one_line(str(e))) from None
    return True


def agent_entry(path, name, strict=False):
    """The agents.json entry of `name`, or None. strict: a broken file raises (team_agents.AgentsError)."""
    import team_agents
    try: entries, _ = team_agents.load()
    except team_agents.AgentsError:
        if strict: raise
        return None
    return next((e for e in entries if e.get('name') == name), None)


# ---------------------------------------------------------------- the instance

def instance_root(name): return os.path.join(agent_base(), name)


def marker(name):
    return read_json(os.path.join(instance_root(name), 'data', MARKER), None)


def ours(name, agent_id):
    m = marker(name)
    return isinstance(m, dict) and m.get('agent_id') == str(agent_id)


def name_in_use(name, agents_file):
    """Why this name cannot be created here, or None. Legacy folders count: agent_root falls back to them."""
    if os.path.lexists(instance_root(name)): return f'a pasta {instance_root(name)} ja existe'
    for legacy in (home('.config', name), home('.config', 'work-watch', name[len('work-watch-'):])
                   if name.startswith('work-watch-') else None):
        if legacy and os.path.isdir(legacy): return f'o nome ja e de um agent antigo ({legacy})'
    if os.path.exists(os.path.join(team_dir(), name + '.session')): return f'o nome ja tem sessao em {team_dir()}'
    if agent_entry(agents_file, name) is not None: return f'o nome ja esta em {agents_file}'
    return None


def slug(text):
    s = re.sub(r'[^a-z0-9-]+', '-', str(text).lower()).strip('-')
    return s or 'projeto'


AUTONOMY = {
    'autonomous': {'can': ['branch e worktree', 'testes filtrados', 'push de branch', 'abrir PR'],
                   'ask_first': ['dependencia nova', 'migration destrutiva ou que apaga dado']},
    'semi_autonomous': {'can': ['branch e worktree', 'testes filtrados', 'push de branch'],
                        'ask_first': ['abrir PR', 'dependencia nova', 'migration destrutiva ou que apaga dado']},
    'manual': {'can': ['testes filtrados'],
               'ask_first': ['branch e worktree', 'push de branch', 'abrir PR', 'dependencia nova',
                             'migration destrutiva ou que apaga dado']},
}
RELEASE_CAN = ['release em lote pelo integrador (fast-forward na main, tag, push)',
               'deploy com trava e linha no log de deploys']


def role_defaults(cfg, role):
    """A role that is not a dev role (product-radar) names its own model in behaviors/<role>/instance.json,
    {"example": "<config-example folder>"}: the sources of that model whose type the config lacks and the model's
    options of the role come in (PLN0226). Returns the model ({"config", "instructions"}) for render() to take its
    autonomy and instructions, or None without the file (nothing changes)."""
    ref = read_json(os.path.join(BEHAVIORS_DIR, role, 'instance.json'), None)
    ex = ref.get('example') if isinstance(ref, dict) else None
    if not isinstance(ex, str) or not NAME_RE.fullmatch(ex): return None
    folder = os.path.join(SKILL_DIR, 'config-example', ex)
    src = read_json(os.path.join(folder, 'config.json'), None)
    if not isinstance(src, dict): raise Fail(f'o papel {role} aponta para um modelo sem config.json: {ex}')
    have = {e.get('type') for e in cfg.get('sources') or [] if isinstance(e, dict)}
    cfg['sources'] = list(cfg.get('sources') or []) + [
        e for e in src.get('sources') or [] if isinstance(e, dict) and e.get('type') not in have]
    opts = (src.get('behavior_config') or {}).get(role)
    if isinstance(opts, dict): cfg.setdefault('behavior_config', {}).setdefault(role, opts)
    try:
        with open(os.path.join(folder, 'instructions.md')) as f: instr = f.read()
    except OSError:
        instr = None
    return {'config': src, 'instructions': instr}


def render(a, template):
    """(config dict, instructions text) for the agent `a` from the template folder."""
    cfg = read_json(os.path.join(template, 'config.json'), None)
    if not isinstance(cfg, dict): raise Fail(f'modelo sem config.json valido em {template}')
    try:
        with open(os.path.join(template, 'instructions.md')) as f: instr = f.read()
    except OSError:
        raise Fail(f'modelo sem instructions.md em {template}')
    name, work_dir = a['name'], a.get('work_dir') or '~'
    projects = [p for p in a.get('projects') or [] if isinstance(p, dict)]
    prefix = str(projects[0].get('prefix') or '') if projects else ''
    roles = [r for r in a.get('roles') or [] if isinstance(r, str)] or list(DEFAULT_ROLES)
    for r in roles:
        if not NAME_RE.fullmatch(r) or not os.path.isfile(os.path.join(BEHAVIORS_DIR, r, 'BEHAVIOR.md')):
            raise Fail(f'papel desconhecido nesta maquina: {r} (atualize o plugin agent)')
    behaviors = list(dict.fromkeys([BASE_BEHAVIOR] + roles))
    batch = 'batch-release' in behaviors
    repo = slug(os.path.basename(work_dir.rstrip('/')) or name)

    # the template's placeholder project (meu-projeto, MPJ) becomes this agent's
    text = json.dumps(cfg, ensure_ascii=False).replace('meu-projeto', repo)
    cfg = json.loads(text)
    cfg['live'] = a.get('mode') == 'live'
    cfg['session'] = {'cwd': work_dir, 'aliases': [name], 'rotate': True}
    cfg['behaviors'] = behaviors
    if not batch:
        (cfg.get('behavior_config') or {}).pop('batch-release', None)
        cfg['hooks'] = [h for h in cfg.get('hooks') or [] if h.get('type') not in ('release_due', 'deploy_log')]
    else:
        cfg.setdefault('behavior_config', {}).setdefault('batch-release', {})['repo'] = work_dir
    models = [m for m in (role_defaults(cfg, r) for r in roles) if m]
    own = models if models and not batch and 'dev-worker' not in behaviors else []   # not a dev agent at all
    if own and own[0]['instructions']: instr = own[0]['instructions']
    if not cfg.get('behavior_config'): cfg.pop('behavior_config', None)
    # the gh tool of the template points at an example account: the person adds the real one
    cfg['tools'] = [t for t in cfg.get('tools') or [] if not (isinstance(t, dict) and t.get('kind') == 'cli')]
    auto = AUTONOMY.get(a.get('autonomy'), AUTONOMY['semi_autonomous'])
    never = list((cfg.get('autonomy') or {}).get('never') or [])
    can, ask = list(auto['can']), list(auto['ask_first'])
    if batch: (can if a.get('autonomy') == 'autonomous' else ask).extend(RELEASE_CAN)
    if own:                             # the role's own autonomy instead of the dev one (branch, push, PR)
        can, ask = [], []
        for m in own:
            au = m['config'].get('autonomy') or {}
            can += [x for x in au.get('can') or [] if x not in can]
            ask += [x for x in au.get('ask_first') or [] if x not in ask]
            never += [x for x in au.get('never') or [] if x not in never]
    cfg['autonomy'] = {'can': can, 'ask_first': ask, 'never': never}
    base_repo = dict((cfg.get('repos') or [{}])[0])
    base_repo.pop('gh_account', None)
    base_repo.update({'path': work_dir, 'name': repo, 'release': 'batch' if batch else 'pr'})
    cfg['repos'] = [base_repo]
    planou = dict(cfg.get('planou') or {})
    planou['project'] = prefix
    if not cfg['live']: planou.pop('task_queue', None)       # the queue only runs live (schema warns otherwise)
    cfg['planou'] = planou

    for k, v in (('<instância>', name), ('<nome>', a.get('display_name') or name), ('<SIGLA>', prefix or '?'),
                 ('<path do repositório>', work_dir), ('<rules>', base_repo.get('rules') or 'CLAUDE.md')):
        instr = instr.replace(k, str(v))
    lines = ['', '## Criado pelo provisionador', '',
             f'Pedido na tela Time do Planou. Revise o `config.json` (repositório, testes, conta do GitHub) antes de '
             f'ligar{" (a instância nasceu em modo teste)" if not cfg["live"] else ""}.', '',
             f'- Papéis: {", ".join(behaviors)}',
             f'- Autonomia no Planou: {a.get("autonomy") or "semi_autonomous"}',
             '- Projetos: ' + (', '.join(f'{p.get("prefix")} ({p.get("name")})' for p in projects) or 'nenhum'), '']
    return cfg, instr.rstrip('\n') + '\n' + '\n'.join(lines)


def run_validate(name):
    """agent.py <name> --validate, in a child without our credential. Fail with the first error lines."""
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'agent.py'), name, '--validate'],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        errs = [l[5:].strip() for l in r.stdout.splitlines() if l.startswith('ERRO:')] or \
               (r.stdout.strip().splitlines() + r.stderr.strip().splitlines())[-1:]
        raise Fail('config invalida: ' + '; '.join(errs[:3]))


def write_key(name, key):
    """secrets/planou.env (0600, folder 0700), replacing an older key atomically."""
    d = os.path.join(instance_root(name), 'secrets')
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    write_atomic(os.path.join(d, 'planou.env'), f'PLANOU_AGENT_KEY={key}\n', 0o600)


def key_file(name): return os.path.join(instance_root(name), 'secrets', 'planou.env')


def pid_alive(pidfile, arg=None):
    """runner.pid names a live runner.sh (with `arg` among its arguments, for an instance of the agent plugin)."""
    try:
        with open(pidfile) as f: pid = f.read().strip()
        if not pid.isdigit(): return False
        with open(f'/proc/{pid}/cmdline', 'rb') as f: argv = f.read().split(b'\0')
    except OSError:
        return False
    return any(x.endswith(b'runner.sh') for x in argv) and (arg is None or arg.encode() in argv)


def runner_alive(name):
    return pid_alive(os.path.join(instance_root(name), 'cache', 'runner', 'runner.pid'), name)


def stop_runner(cfg, name):
    if not runner_alive(name): return
    r = subprocess.run(['bash', cfg['runner'], name, 'stop'], stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, timeout=90)
    if r.returncode != 0 or runner_alive(name):
        raise Fail(f'nao consegui parar o runner de {name}')


def snapshot(cfg):
    cmd = cfg.get('snapshot_cmd')
    if not cmd: return
    argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    if not argv or not shutil.which(os.path.expanduser(argv[0])):
        log(f'aviso: {argv[0] if argv else "snapshot_cmd"} nao encontrado; sigo sem config-snapshot'); return
    try:
        r = subprocess.run([os.path.expanduser(argv[0])] + argv[1:], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if r.returncode: log(f'aviso: config-snapshot saiu com {r.returncode}; sigo')
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f'aviso: config-snapshot falhou ({e.__class__.__name__}); sigo')


# ---------------------------------------------------------------- adopted agents (PLN0188)

MAX_INVENTORY = 200            # Planou's limit per inventory


def stop_verb(script):
    """How `script` (a runner.sh of a plugin of its own) is stopped: the label of its `case "$1" in` that says stop.
    job-scout takes `stop`; travel-agent only `--stop` (a bare `stop` there would START a runner). None: unknown."""
    try:
        with open(script) as f: text = f.read()
    except OSError:
        return None
    labels = set()
    for block in re.findall(r'case "\$1" in(.*?)\besac\b', text, re.S):
        for lab in re.findall(r'^\s*([^\s()#][^)\n]*)\)', block, re.M):
            labels.update(x.strip() for x in lab.split('|'))
    return 'stop' if 'stop' in labels else '--stop' if '--stop' in labels else None


def runner_of(name, cfg, entries):
    """The runner of a machine agent, from team_agents.resolve: {'script', 'stop' (argv after the script), 'pid' (its
    runner.pid), 'arg'}. The agent plugin's runner.sh for its instances; else <skill>/scripts/runner.sh of the skill the
    agent opens with (job-scout, travel-agent), with its folder as team_status finds it. None: no runner known."""
    import team_agents, team_status
    a = team_agents.resolve(name, entries)
    if not a: return None
    entry = next((e for e in entries if e.get('name') == name), None)
    folder = team_status.runner_dir(a, entry)
    if a['agent_plugin']:
        return {'script': cfg['runner'], 'stop': [name, 'stop'], 'pid': os.path.join(folder, 'runner.pid'), 'arg': name}
    skill = (a.get('skill') or name).split()[0]
    if not NAME_RE.fullmatch(skill) or not folder: return None
    script = home('.claude', 'skills', skill, 'scripts', 'runner.sh')
    verb = stop_verb(script)
    # no stop known (chief-of-staff): its runner is still seen, it is just never stopped from here
    return {'script': script if verb else None, 'stop': [verb] if verb else None, 'pid': os.path.join(folder, 'runner.pid'),
            'arg': None}


def stop_adopted(r, name):
    """Stops an adopted agent's runner with its own runner.sh (the session and its config are left alone)."""
    if not r or not pid_alive(r['pid'], r['arg']): return
    if not r['script']: raise Fail(f'nao sei parar o runner de {name}; pare pela sessao dele')
    p = subprocess.run(['bash', r['script'], *r['stop']], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       timeout=90)
    if p.returncode != 0 or pid_alive(r['pid'], r['arg']):
        raise Fail(f'nao consegui parar o runner de {name}')


def tilde(path):
    """~ for the home folder (Planou shows the folder and takes only ~/... or /...)."""
    h = home()
    p = str(path or '')
    if p == h: return '~'
    if p.startswith(h + '/'): return '~' + p[len(h):]
    return p if p.startswith('/') else None


# ---------------------------------------------------------------- employees that left this computer (PLN0205)

def listed_ids(body):
    """The agent ids of a valid answer of GET /provisioner/agents, or None when the answer is out of shape (then nobody
    counts as gone: an empty or broken answer must never look like "every employee left")."""
    if not isinstance(body, dict) or not isinstance(body.get('agents'), list): return None
    ids = set()
    for a in body['agents']:
        if not isinstance(a, dict) or not isinstance(a.get('agent_id'), str) or not a['agent_id']: return None
        ids.add(a['agent_id'])
    return ids


def own_instances():
    """(name, agent id) of every instance in ~/.config/agent with this provisioner's marker (data/provisioned.json)."""
    try: names = sorted(os.listdir(agent_base()))
    except OSError: return []
    out = []
    for name in names:
        if not NAME_RE.fullmatch(name): continue
        m = marker(name)
        if isinstance(m, dict) and isinstance(m.get('agent_id'), str) and m['agent_id']:
            out.append((name, m['agent_id']))
    return out


# ---------------------------------------------------------------- one pass

class Provisioner:
    def __init__(self, cfg, api):
        self.cfg, self.api = cfg, api
        self.state_file = prov_dir('data', 'state.json')
        self.state = read_json(self.state_file, {})
        if not isinstance(self.state, dict): self.state = {}

    def save_state(self):
        write_atomic(self.state_file, json.dumps(self.state, ensure_ascii=False, indent=1) + '\n', 0o600)

    def report(self, a, status, detail=None):
        """Only on a change: Planou keeps a history and limits calls per minute."""
        detail = one_line(detail) if detail else None
        if a.get('status') == status and (a.get('detail') or None) == detail: return
        self.api.report(a['agent_id'], status, detail)
        opened = status == 'terminal_open' and a.get('status') != 'terminal_open'
        a['status'], a['detail'] = status, detail
        log(f'{a["name"]}: {status}' + (f' ({detail})' if detail else ''))
        # the clock of "no session came up": only a live terminal_open this provisioner asked for (a test-mode instance
        # never starts its runner, and an adopted agent Planou lists as terminal_open was not opened by us)
        aid = str(a['agent_id'])
        st = self.state.get(aid) or {}
        if opened and detail is None: st['terminal_open_at'] = int(time.time())
        elif status != 'terminal_open':
            st.pop('terminal_open_at', None); st.pop('no_session_told', None)
        if st: self.state[aid] = st
        else: self.state.pop(aid, None)

    def no_session(self, a):
        """terminal_open without the runner: after session_timeout_s since this provisioner asked for the terminal, the
        same status goes again with a readable cause. Once: the inventory answer has no `detail` to compare with."""
        st = self.state.get(str(a['agent_id'])) or {}
        since = st.get('terminal_open_at')
        if not since or st.get('no_session_told') or time.time() - since < self.cfg['session_timeout_s']: return
        self.report(a, 'terminal_open', NO_SESSION)
        st['no_session_told'] = True
        self.state[str(a['agent_id'])] = st

    def pass_once(self):
        body = self.api.agents_body()       # a network error or a 401/403 leaves here: nothing below runs
        listed = body.get('agents') if isinstance(body, dict) else None
        for a in listed if isinstance(listed, list) else []:
            if not isinstance(a, dict) or not a.get('agent_id'): continue
            try:
                self.handle(a)
            except Fail as e:
                self.fail(a, str(e))
            except ApiError as e:
                if e.status in (401, 403): raise
                self.fail(a, f'o Planou recusou ({e.status} {e.code})')
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                self.fail(a, f'falha local ({e.__class__.__name__})')
        self.departed(listed_ids(body))     # before the inventory: an archived instance is no longer on this machine
        self.inventory()
        self.save_state()

    # ------------------------------------------------------------ employees that left this computer (PLN0205)

    def departed(self, ids):
        """Archives each instance of ours whose employee the valid list no longer has. ids None: the list was out of
        shape, nothing is archived. One instance that cannot be archived now (a runner that does not stop, a broken
        agents.json) stays as it is and is tried again on the next pass; the others go on."""
        if ids is None:
            log('aviso: lista do Planou fora do formato; nenhuma instancia arquivada nesta volta'); return
        for name, aid in own_instances():
            if aid in ids: continue
            try:
                self.archive(name, aid)
            except Fail as e:
                log(f'{name}: saiu deste computador, mas {e}; tento de novo na proxima volta')
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                log(f'{name}: saiu deste computador, mas nao consegui arquivar ({e.__class__.__name__}); tento de novo')

    def archive(self, name, aid):
        """Stops the runner, takes the entry out of agents.json and moves the instance (and its .session) to departed/,
        without the secrets (the key was revoked when the employee left). Planou is not told: the row is another
        computer's now (a report here would be 404)."""
        root = instance_root(name)
        stop_runner(self.cfg, name)
        if os.path.lexists(self.cfg['agents_file']):     # no team list at all: no entry to take out
            set_agent_entry(self.cfg['agents_file'], name, None)
        shutil.rmtree(os.path.join(root, 'secrets'), ignore_errors=True)
        dest = prov_dir('departed', f'{name}-{datetime.now().strftime("%Y%m%d%H%M%S")}')
        base, n = dest, 1
        while os.path.lexists(dest): dest, n = f'{base}~{n}', n + 1        # left twice in the same second
        os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
        shutil.move(root, dest)
        sess = os.path.join(team_dir(), name + '.session')
        if os.path.isfile(sess): shutil.move(sess, os.path.join(dest, name + '.session'))
        self.state.pop(aid, None)
        log(f'{name}: o funcionario saiu deste computador; runner parado, fora do agents.json e instancia arquivada em '
            f'{dest} (sem os segredos)')

    # ------------------------------------------------------------ adopted agents (PLN0188)

    def machine(self):
        """(entries of agents.json, effective agents of the machine), or None when agents.json cannot be read (then no
        inventory goes: an empty one would make Planou think every agent left the machine)."""
        import team_agents
        try:
            entries, _ = team_agents.load()
            return entries, team_agents.all_agents(entries)
        except team_agents.AgentsError as e:
            log(f'aviso: inventario nao enviado: {one_line(e)}')
            return None

    def runner_state(self, name, alive, enabled, known):
        """up; stopped when turned off (or no runner known); crashed only after two passes in a row without the runner
        (a relaunch after a tick takes a moment); in between, the last state told."""
        st = self.state.setdefault('runners', {}).setdefault(name, {})
        if alive:
            st['misses'], state = 0, 'up'
        else:
            st['misses'] = int(st.get('misses') or 0) + 1
            if not enabled or not known: state = 'stopped'
            elif st['misses'] >= 2: state = 'crashed'
            else: state = st.get('last') or 'stopped'
        st['last'] = state
        return state

    def inventory(self):
        m = self.machine()
        if m is None: return
        entries, agents = m
        items, found = [], {}
        for a in agents[:MAX_INVENTORY]:
            name = a['name']
            if not NAME_RE.fullmatch(name) or name in found: continue
            r = runner_of(name, self.cfg, entries)
            alive = bool(r) and pid_alive(r['pid'], r['arg'])
            # an instance missing from agents.json is not opened by the extension: it counts as turned off
            on = a['enabled'] and a.get('listed', True)
            a['enabled'] = on
            item = {'name': name, 'runner': self.runner_state(name, alive, on, bool(r)), 'enabled': on}
            wd = tilde(a.get('cwd'))
            if wd and len(wd) <= 300 and '..' not in wd.split('/'): item['work_dir'] = wd
            items.append(item)
            found[name] = (a, r)
        runners = self.state.get('runners') or {}
        for gone in [n for n in runners if n not in found]: runners.pop(gone)
        try:
            answer = self.api.inventory(items)
        except ApiError as e:
            if e.status in (401, 403): raise
            if e.status == 404: return                   # a Planou before PLN0188: nothing to adopt
            log(f'aviso: inventario recusado ({e})'); return
        for a in answer.get('agents') or []:
            if not isinstance(a, dict) or a.get('name') not in found or not a.get('agent_id'): continue
            try:
                self.adopted(a, *found[a['name']])
            except Fail as e:
                self.fail(a, str(e))
            except ApiError as e:
                if e.status in (401, 403): raise
                log(f'{a["name"]}: o Planou recusou ({e})')
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                self.fail(a, f'falha local ({e.__class__.__name__})')

    def adopted(self, a, agent, r):
        """Ligar e Desligar for an agent that already ran here: agents.json "enabled" and the runner, nothing else. Its
        instance is never created, changed nor removed (there is no `remove` for it)."""
        name, desired, status = a['name'], a.get('desired'), a.get('status')
        if desired == 'paused':
            # once: a runner the person starts by hand afterwards (`team <name>`) is theirs
            if status == 'paused': return
            stop_adopted(r, name)
            set_agent_entry(self.cfg['agents_file'], name, False)
            return self.report(a, 'paused')
        if desired != 'run': return
        # only after Desligar (or a failed step): an agent the person runs by hand, off the team list, is left as it is
        turned_on = status in ('paused', 'error') and not agent['enabled'] and set_agent_entry(self.cfg['agents_file'], name, True)
        if r and pid_alive(r['pid'], r['arg']): return self.report(a, 'runner_up')
        if turned_on or status in ('paused', 'error'): return self.report(a, 'terminal_open')
        if status == 'terminal_open' and agent['enabled']: self.no_session(a)

    def fail(self, a, msg):
        st = self.state.setdefault(str(a['agent_id']), {})
        st['failed_at'] = int(time.time())
        st['failed_prefix'] = (a.get('key') or {}).get('prefix')
        try: self.report(a, 'error', msg)
        except ApiError as e: log(f'{a.get("name")}: erro ao informar o estado ({e})')

    def handle(self, a):
        name, desired, status = str(a.get('name') or ''), a.get('desired'), a.get('status')
        key = a.get('key') or {}
        if not NAME_RE.fullmatch(name): raise Fail(f'nome invalido para esta maquina: {name!r}')
        if desired == 'remove': return self.remove(a)
        if desired == 'paused':
            if status in ('terminal_open', 'runner_up', 'validated'): self.pause(a)
            return
        if desired != 'run': return
        if status == 'error':
            st = self.state.get(str(a['agent_id'])) or {}
            new_key = key.get('state') == 'waiting' and key.get('prefix') != st.get('failed_prefix')
            if not new_key and time.time() - (st.get('failed_at') or 0) < self.cfg['retry_s']: return
            if key.get('state') != 'waiting' and not (ours(name, a['agent_id']) and os.path.isfile(key_file(name))):
                return                                   # nothing new to try with: wait for a new key
            return self.create(a)
        if status in ('requested', 'creating', 'validated'):
            return self.create(a)
        if key.get('state') == 'waiting' and ours(name, a['agent_id']):
            self.take_key(a)                             # a new key for a running agent: written over the old one
        if status == 'paused':
            set_agent_entry(self.cfg['agents_file'], name, True)
            return self.report(a, 'terminal_open', None if a.get('mode') == 'live' else TEST_MODE)
        if status == 'terminal_open':
            return self.report(a, 'runner_up') if runner_alive(name) else self.no_session(a)

    def take_key(self, a):
        r = self.api.pick_up_key(a['agent_id'])
        key = r.get('key') if isinstance(r, dict) else None
        if not isinstance(key, str) or not key.startswith('pl_ag_') or any(c.isspace() for c in key):
            raise Fail('o Planou devolveu uma chave em formato inesperado')
        write_key(a['name'], key)
        m = marker(a['name']) or {}
        m['key_prefix'] = r.get('key_prefix'); m['key_written_at'] = datetime.now().isoformat(timespec='seconds')
        write_atomic(os.path.join(instance_root(a['name']), 'data', MARKER), json.dumps(m, indent=1) + '\n', 0o600)
        log(f'{a["name"]}: chave retirada e gravada em {key_file(a["name"])} (0600)')

    def create(self, a):
        name, aid, key = a['name'], str(a['agent_id']), a.get('key') or {}
        mine = ours(name, aid)
        if not mine:
            why = name_in_use(name, self.cfg['agents_file'])
            if why: raise Fail(why + '; escolha outro nome ou remova o antigo')
            check_agents_file(self.cfg['agents_file'])  # a missing or broken agents.json stops here, before anything is made
            # rendered before `creating`: a request that cannot be made (unknown role) is one `error`, retried quietly
            cfg, instr = render(a, self.cfg['template'])
        self.report(a, 'creating')
        root = instance_root(name)
        if not mine:
            os.makedirs(os.path.join(root, 'data'), exist_ok=True)
            write_atomic(os.path.join(root, 'data', MARKER), json.dumps(
                {'agent_id': aid, 'name': name, 'mode': a.get('mode'),
                 'created_at': datetime.now().isoformat(timespec='seconds')}, indent=1) + '\n', 0o600)
            write_atomic(os.path.join(root, 'config', 'config.json'),
                         json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', 0o644)
            write_atomic(os.path.join(root, 'config', 'instructions.md'), instr, 0o644)
            log(f'{name}: instancia criada em {root} ({"ligada" if cfg["live"] else "modo teste"})')
        run_validate(name)
        if key.get('state') == 'waiting':
            self.take_key(a)
        elif not os.path.isfile(key_file(name)):
            raise Fail({'expired': 'a chave expirou no cofre; gere outra na tela Time',
                        'picked_up': 'a chave ja foi retirada e nao esta nesta maquina; gere outra na tela Time'}
                       .get(key.get('state'), 'o funcionario nao tem chave no cofre; gere uma na tela Time'))
        self.report(a, 'validated')
        snapshot(self.cfg)
        set_agent_entry(self.cfg['agents_file'], name, True,
                        {'cwd': a.get('work_dir') or '~', 'skill': f'agent {name}', 'rotate': True})
        self.state.pop(aid, None)
        self.report(a, 'terminal_open', None if a.get('mode') == 'live' else TEST_MODE)

    def pause(self, a):
        stop_runner(self.cfg, a['name'])
        set_agent_entry(self.cfg['agents_file'], a['name'], False)
        self.report(a, 'paused')

    def remove(self, a):
        name, aid, root = a['name'], str(a['agent_id']), instance_root(a['name'])
        if os.path.lexists(root) and not ours(name, aid):
            raise Fail(f'{root} nao foi criada pelo provisionador para este funcionario; remova a mao')
        if os.path.lexists(root):
            stop_runner(self.cfg, name)
        if ours(name, aid):
            set_agent_entry(self.cfg['agents_file'], name, None)
        if os.path.lexists(root):
            shutil.rmtree(os.path.join(root, 'secrets'), ignore_errors=True)
            dest = prov_dir('removed', f'{name}-{datetime.now().strftime("%Y%m%d%H%M%S")}')
            os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
            shutil.move(root, dest)
            sess = os.path.join(team_dir(), name + '.session')
            if os.path.isfile(sess): shutil.move(sess, os.path.join(dest, name + '.session'))
            log(f'{name}: instancia arquivada em {dest} (sem os segredos)')
        self.state.pop(aid, None)
        self.report(a, 'removed')


TEST_MODE = 'modo teste: o runner so sobe com "live": true no config.json'
NO_SESSION = ('terminal pedido, mas nenhuma sessao subiu: confira se a janela do VS Code com a extensao Team Terminals '
              'esta aberta ou rode "Team: abrir terminais"')


# ---------------------------------------------------------------- CLI

def lock_or_exit():
    os.makedirs(prov_dir(), mode=0o700, exist_ok=True)
    fd = os.open(prov_dir('provisioner.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('provisionador: ja tem outro rodando (provisioner.lock)')
    return fd


def make():
    cfg = load_settings()
    return cfg, Planou(cfg['base_url'], load_credential(), cfg['timeout_s'])


# ---------------------------------------------------------------- self-update (rollout) and the unit

EXIT_RESTART = 75           # "restart me": systemd (Restart=always) starts the service again, on the chosen version
ROLLOUT_AGENT = 'agent-provisioner'
UNIT_NAME = 'agent-provisioner.service'
UNIT_MARK = '# Local provisioner of the agent plugin'
if SCRIPTS not in sys.path: sys.path.insert(0, SCRIPTS)


def live_dir():
    """The plugin copy in use (the one ~/.claude/skills/agent points at). A pinned provisioner runs from a snapshot in
    ~/.cache/team/plugins and gets the live folder in PROVISIONER_LIVE."""
    from watch_core import rollout as ro
    return os.environ.get('PROVISIONER_LIVE') or ro.plugin_dir(SCRIPTS)


def choose():
    """(plugin, version to run, why) from the copy in use and ~/.config/team/rollout.json, read only (the runners'
    canary writes it): without a canary for the plugin, the version on disk; with one, the version on disk once
    released, else the last released one. (plugin, None, why) when no version may run."""
    from watch_core import rollout as ro
    live = live_dir()
    plugin, disk = ro.plugin_info(live) if live else (None, None)
    if not disk: return plugin, None, 'sem plugin.json na copia em uso'
    data = ro.load()
    if not ro.active(data, plugin) or not ro.versions(data, plugin): return plugin, disk, 'disco'
    target, mode = ro.decide(data, plugin, ROLLOUT_AGENT, disk)
    return plugin, target, mode


WAIT_S = int(os.environ.get('PROVISIONER_WAIT_S') or 300)   # waiting for a version it can run (tests shorten it)
RUNNABLE = ('released', 'canary')


def script_of(snap):
    """provisioner.py inside the plugin copy `snap`, at the place this one has in its own copy (live or snapshot)."""
    from watch_core import rollout as ro
    own = ro.plugin_dir(SCRIPTS)
    return os.path.join(snap, os.path.relpath(os.path.realpath(SCRIPTS), os.path.realpath(own)), 'provisioner.py')


def resolve(current):
    """(plugin, version, why, script) to run: script None when it is this code; version None when none can run now
    (why says why, for the log). PLN0186: a released version older than the provisioner has no provisioner.py, and
    running it was "can't open file", exit 2 and a restart every 5 s. Then the newest newer version released or in
    canary that has the script runs instead (never a refused one, nor one the canary has not seen); none, it waits."""
    from watch_core import rollout as ro
    plugin, target, why = choose()
    if target is None: return plugin, None, f'nenhuma versao liberada de {plugin} ({why})', None
    if target == current: return plugin, target, why, None
    live = live_dir()
    snap = ro.snapshot(live, plugin, target)
    if not snap: return plugin, None, f'sem copia da versao {target} de {plugin}', None
    if os.path.isfile(script_of(snap)): return plugin, target, why, script_of(snap)
    data = ro.load()
    for v in sorted(ro.versions(data, plugin), key=ro.vkey, reverse=True):
        st = ro.state(data, plugin, v)
        if ro.vkey(v) <= ro.vkey(target) or st not in RUNNABLE: continue
        note = f'a {target} ({why}) nao tem o provisionador; {v} {ro.STATES_PT[st]}'
        if v == current: return plugin, v, note, None
        s = ro.snapshot(live, plugin, v)
        if s and os.path.isfile(script_of(s)): return plugin, v, note, script_of(s)
    return plugin, None, (f'a versao {target} de {plugin} ({why}) nao tem o provisionador (scripts/provisioner.py) e '
                          f'nenhuma versao mais nova liberada ou em canario tem'), None


def wait_and_restart(current, why):
    """No version to run: one clear line, then wait (checking every little while whether one can run now) and exit 75
    so systemd starts it again. Never a fast loop under Restart=always."""
    log(f'aviso: {why}; espero ate {WAIT_S} s por uma versao que possa rodar e reinicio (codigo {EXIT_RESTART})')
    step, end = max(1, min(30, WAIT_S // 10)), time.monotonic() + WAIT_S
    while time.monotonic() < end:
        time.sleep(min(step, max(0, end - time.monotonic())))
        try: _p, ver, _w, _s = resolve(current)
        except Exception: continue
        if ver: log(f'versao {ver} pode rodar; reinicio'); break
    sys.exit(EXIT_RESTART)


def pin(argv, current):
    """At start: when the version to run is not this code's, exec the provisioner of a snapshot of it. Returns only
    when this code is the one to run (or no other can be had)."""
    try: plugin, target, why, script = resolve(current)
    except Exception as e:                              # a broken rollout.json never stops the service
        log(f'aviso: rollout ilegivel ({e.__class__.__name__}); sigo na {current}'); return
    if target is None: wait_and_restart(current, why)
    if script is None:
        if 'nao tem o provisionador' in why: log(f'aviso: {why}: sigo na {target}')
        return
    log(f'versao {target} ({why}): rodando a copia {script}')
    os.environ['PROVISIONER_LIVE'] = live_dir()
    os.execv(sys.executable, [sys.executable, script, 'run', *argv])


def maintain_unit():
    """The installed unit follows this version's template: rewritten and daemon-reload when it differs. Only a unit this
    plugin installed (its first line) is touched."""
    unit = os.path.join(os.environ.get('XDG_CONFIG_HOME') or home('.config'), 'systemd', 'user', UNIT_NAME)
    try:
        with open(os.path.join(SKILL_DIR, 'provisioner', UNIT_NAME)) as f: want = f.read()
        with open(unit) as f: have = f.read()
    except OSError:
        return
    if have == want or not have.startswith(UNIT_MARK): return
    write_atomic(unit, want, 0o644)
    ctl = os.environ.get('SYSTEMCTL') or 'systemctl'
    try:
        r = subprocess.run([ctl, '--user', 'daemon-reload'], stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        log(f'unit atualizada ({unit}); daemon-reload ' + ('ok' if r.returncode == 0 else f'saiu com {r.returncode}'))
    except (OSError, subprocess.SubprocessError) as e:
        log(f'unit atualizada ({unit}); daemon-reload falhou ({e.__class__.__name__})')


def cmd_run(argv):
    interval = None
    if argv[:1] == ['--interval'] and len(argv) > 1 and argv[1].isdigit(): interval = int(argv[1])
    # an explicit handler: a SIGTERM inherited as ignored would otherwise keep the service from stopping
    signal.signal(signal.SIGTERM, lambda *_: (log('provisionador parando'), sys.exit(0)))
    from watch_core import rollout as ro
    import team_agents, team_status                     # noqa: F401  loaded now: a git pull later never mixes versions
    plugin, current = ro.plugin_info(ro.plugin_dir(SCRIPTS))
    pin(argv, current)
    try:
        cfg, api = make()
    except SystemExit as e:                             # no credential yet: do not spin under Restart=always
        log(str(e.code)); time.sleep(60); raise
    _lock = lock_or_exit()
    maintain_unit()
    every = interval or int(cfg['interval_s'])
    log(f'provisionador no ar: {plugin} {current}, {cfg["base_url"]}, a cada {every} s')
    last_err, wait, told = None, every, False
    while True:
        try:
            told = told or tell_version(api, current)
            Provisioner(cfg, api).pass_once()
            if last_err: log('Planou respondeu de novo')
            last_err, wait = None, every
        except ApiError as e:
            msg = ('credencial recusada; gere outra com scripts/connect-provisioner.sh' if e.status == 401 else
                   'o Planou recusou por nao ser local (403 not_local); use a porta local' if e.status == 403 else str(e))
            if msg != last_err: log(f'aviso: {msg}')
            last_err, wait = msg, min(max(wait * 2, every), 300)
        except (urllib.error.URLError, OSError, ValueError) as e:
            msg = f'Planou fora do ar ({e.__class__.__name__})'
            if msg != last_err: log(f'aviso: {msg}')
            last_err, wait = msg, min(max(wait * 2, every), 300)
        # between two passes (never in the middle of a request): a new version to run means exit and be restarted
        try: _p, target, why, _s = resolve(current)
        except Exception: target = current
        if target and target != current:
            log(f'versao nova de {plugin}: {current} -> {target} ({why}); saindo para o systemd reiniciar')
            sys.exit(EXIT_RESTART)
        time.sleep(wait)


def cmd_once(_argv):
    cfg, api = make()
    _lock = lock_or_exit()
    try:
        Provisioner(cfg, api).pass_once()
    except ApiError as e:
        raise SystemExit(f'provisionador: {e}')
    except urllib.error.URLError as e:
        raise SystemExit(f'provisionador: Planou fora do ar ({e.__class__.__name__})')


def cmd_status(_argv):
    cfg, api = make()
    m = Provisioner(cfg, api).machine()
    if m:
        entries, agents = m
        for a in agents:
            r = runner_of(a['name'], cfg, entries)
            run = 'sem runner conhecido' if not r else ('runner de pe' if pid_alive(r['pid'], r['arg']) else 'runner parado')
            print(f'{a["name"]} (maquina): {"ligado" if a["enabled"] else "desligado"}, {run}')
    for a in api.agents():
        name, k = a.get('name'), a.get('key') or {}
        local = ('instancia nossa' if ours(name, a.get('agent_id')) else
                 'pasta de outro' if os.path.lexists(instance_root(name)) else 'sem pasta')
        entry = agent_entry(cfg['agents_file'], name)
        term = 'fora do agents.json' if entry is None else ('ligado' if entry.get('enabled', True) else 'desligado')
        print(f'{name}: desired={a.get("desired")} status={a.get("status")} chave={k.get("state")} '
              f'modo={a.get("mode")} | {local}, {term}, runner {"de pe" if runner_alive(name) else "parado"}')


def cmd_check(_argv):
    cfg, api = make()
    print(f'credencial: ok (0600); base_url: {cfg["base_url"]}')
    try:
        n = len(api.agents())
    except ApiError as e:
        raise SystemExit(f'Planou: {e}')
    except urllib.error.URLError as e:
        raise SystemExit(f'Planou fora do ar ({e.__class__.__name__})')
    print(f'Planou: ok ({n} funcionario(s) pedido(s))')


# ---------------------------------------------------------------- pair: "Conectar este computador" (PLN0196)

PAIR_USAGE = 'uso: provisioner.py pair [<codigo>] [--base-url URL]  (sem o codigo, ele e lido do terminal, oculto, ou da entrada)'


def clean_text(text, limit):
    t = ''.join(ch for ch in ' '.join(str(text).split()) if ch.isprintable())
    return t[:limit].strip()


def computer_name():
    return clean_text(socket.gethostname(), 80) or 'computador'


def computer_os():
    """'Ubuntu 24.04.1 LTS' from /etc/os-release when there is one, else 'Linux 6.8' (at most 40 characters)."""
    try:
        with open('/etc/os-release') as f:
            for line in f:
                if line.startswith('PRETTY_NAME='):
                    v = clean_text(line.split('=', 1)[1].strip().strip('"\''), 40)
                    if v: return v
    except OSError:
        pass
    return clean_text(f'{platform.system()} {platform.release()}', 40) or 'desconhecido'


def env_with_key(path, key):
    """The file's other lines stay; PLANOU_PROVISIONER_KEY is replaced (or added)."""
    lines = []
    try:
        with open(path) as f: lines = f.read().splitlines()
    except OSError:
        pass
    kept = [ln for ln in lines if not ln.strip().startswith('PLANOU_PROVISIONER_KEY=')]
    return '\n'.join(kept + [f'PLANOU_PROVISIONER_KEY={key}']) + '\n'


def post_pair(base_url, code, name, os_name, timeout):
    """POST /provisioner/pair (public: the code is the only proof). Returns (status, body)."""
    data = json.dumps({'code': code, 'name': name, 'os': os_name}).encode()
    req = urllib.request.Request(base_url + '/provisioner/pair', data=data, method='POST')
    req.add_header('Accept', 'application/json')
    req.add_header('Content-Type', 'application/json')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try: body = json.loads(e.read() or b'{}')
        except (ValueError, OSError): body = {}
        return e.code, body if isinstance(body, dict) else {}


def read_pair_code():
    """The code typed by the person, never in the process arguments (PLN0192): a hidden prompt on a terminal, else the first
    line of stdin (the one-line installer pipes it in)."""
    try:
        if sys.stdin.isatty():
            import getpass
            return getpass.getpass('codigo de pareamento (Configuracoes > Computadores): ')
        return sys.stdin.readline()
    except (EOFError, KeyboardInterrupt, OSError):
        return ''


def cmd_pair(argv):
    code, base = None, None
    rest = list(argv)
    while rest:
        a = rest.pop(0)
        if a == '--base-url' and rest: base = rest.pop(0)
        elif a.startswith('--base-url='): base = a.split('=', 1)[1]
        elif not a.startswith('-') and code is None: code = a
        else: raise SystemExit(PAIR_USAGE)
    if code is not None and not code.strip(): raise SystemExit(PAIR_USAGE)

    # Everything that can fail here fails BEFORE the call: a successful call spends the code.
    cfg_file = prov_dir('config.json')
    cfg = {}
    if os.path.exists(cfg_file):
        cfg = read_json(cfg_file, None)
        if not isinstance(cfg, dict):
            raise SystemExit(f'provisionador: {cfg_file} ilegivel; corrija ou apague antes de conectar')
    base = normalize_base_url(base or cfg.get('base_url') or PUBLIC_BASE_URL)
    problem = base_url_problem(base)
    if problem: raise SystemExit(f'provisionador: --base-url {problem}')
    secrets = prov_dir('secrets')
    key_path = os.path.join(secrets, 'planou.env')
    try:
        os.makedirs(secrets, mode=0o700, exist_ok=True)
        os.chmod(secrets, 0o700)
        write_atomic(os.path.join(secrets, '.pair-check'), '')
        os.unlink(os.path.join(secrets, '.pair-check'))
        if os.path.exists(key_path) and not os.access(key_path, os.W_OK): raise PermissionError(key_path)
    except OSError as e:
        raise SystemExit(f'provisionador: sem como gravar em {secrets} ({e.__class__.__name__}); nada foi enviado')

    if code is None: code = read_pair_code()      # asked only once the disk is known to be fine
    if not code.strip(): raise SystemExit(f'provisionador: nenhum codigo digitado; nada foi enviado\n{PAIR_USAGE}')
    name, os_name = computer_name(), computer_os()
    try:
        status, body = post_pair(base, code.strip(), name, os_name, DEFAULTS['timeout_s'])
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(f'provisionador: Planou fora do alcance em {base} ({e.__class__.__name__}); '
                         f'o codigo nao foi usado, tente de novo')
    err = (body.get('error') or {}) if isinstance(body.get('error'), dict) else {}
    if status == 400 and err.get('code') == 'invalid_code':
        raise SystemExit('provisionador: codigo invalido, expirado ou ja usado. '
                         'Gere outro em Configuracoes > Computadores e rode o pair de novo (ele vale 10 minutos).')
    if status == 429:
        raise SystemExit('provisionador: muitas tentativas deste endereco. Espere um minuto e tente de novo.')
    if status == 404:
        raise SystemExit(f'provisionador: {base} nao tem /provisioner/pair (Planou anterior a v0.50.0, ou base_url errado)')
    key = body.get('key') if status == 200 else None
    if not isinstance(key, str) or not key.startswith('pl_pv_'):
        detail = clean_text(err.get('message') or err.get('code') or 'resposta inesperada', 200)
        raise SystemExit(f'provisionador: o Planou recusou (HTTP {status}: {redact(detail)})')

    write_atomic(key_path, env_with_key(key_path, key), 0o600)
    cfg['base_url'] = base
    write_atomic(cfg_file, json.dumps(cfg, indent=2, ensure_ascii=False) + '\n', 0o600)
    prefix = str(body.get('key_prefix') or '')
    shown = prefix if re.fullmatch(r'pl_pv_[A-Za-z0-9]{1,16}', prefix) else 'pl_pv_'
    print(f'computador "{clean_text(body.get("name") or name, 80)}" ({os_name}) conectado ao Planou.')
    print(f'credencial {shown}... gravada em {key_path} (0600); base_url: {base}')
    print('se o servico ja roda, reinicie para ler a credencial nova: systemctl --user restart agent-provisioner')


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmds = {'run': cmd_run, 'once': cmd_once, 'status': cmd_status, 'check': cmd_check, 'pair': cmd_pair}
    if not argv or argv[0] not in cmds:
        print(__doc__.split('Usage:')[1].rstrip() if argv[:1] in (['-h'], ['--help']) else
              'uso: provisioner.py run [--interval S] | once | status | check | pair [<codigo>] [--base-url URL]')
        sys.exit(0 if argv[:1] in (['-h'], ['--help']) else 2)
    os.umask(0o077)
    cmds[argv[0]](argv[1:])


if __name__ == '__main__':
    main()
