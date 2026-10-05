#!/usr/bin/env python3
"""The repository and the test environment of an instance, found by the agent itself (PLN0345).

An agent created on Planou's Time screen only gets a role: no working folder, no `repos`, no `env_up`. On its first
task it takes the repository from the task's `pr_url` (or a repository URL from the task or the project), clones it in
`~/src/<name>` (or reuses the clone there when its origin is the same repository) with the gh already logged in on the
machine, and reads the repository to find how to bring a test environment up. What it finds goes in the instance's
STATE (`data/discovered.json`), never in the person's config.json, with the source of each value; the next tasks use
it. The person's config always wins: `repos` when it has entries, the environment commands of
`behavior_config.acceptance-testing` as a block when `env_up` is set, `node_dir` key by key.

  python3 repo_discovery.py <instance> <pr_url | repository URL | owner/name> [--project PFX] [--refresh]
  python3 repo_discovery.py <instance> <owner/name> --approve <script>@<sha256 prefix>
  python3 repo_discovery.py <instance> show

Only GitHub, and only an owner the instance trusts (review of PLN0345): the account of the instance's gh and its
organizations (`gh api user`, `gh api user/orgs`), or the list `repo_discovery.allowed_owners` of the config. Any
other owner, another git host, a local path or file:// is never cloned: exit 4 with a PERGUNTAR line. A pr_url names
the repository by owner/name; the person's `repos` count only when their origin is that repository.

The discovery reads the snapshot of the default branch of origin after a fetch (git archive of origin/HEAD, never the
working tree of the clone) and is read again whenever that sha changes.

An ALLOWLIST (round 3 of the review): a command found in the repository runs by itself only when it is ONE simple
command (no $ ` | < > ; & ( ) \ nor a newline, plain tokens, no ".." nor absolute path) whose first words are a dev
server of DEV_SERVERS (vite, next dev, dotnet run, python -m http.server...; never docker compose), listening on
loopback only (a host flag with another address is refused; a package.json script without one gets it). Anything
else is a PERGUNTAR line that cites the candidate (found['candidate']). On top of it, a path or content naming deploy,
prod/production, release, kubectl, helm, terraform, aws, gcloud, az, ssh, scp or rsync is never used, and a path that
goes into a command must match [A-Za-z0-9._/-] (no part starting with "-") and is shell-quoted anyway.

Sources of the environment, in this order:
  1. an environment script, `scripts/*env*.sh` or `bin/*env*.sh` with `up)` and `down)` case branches (`url)` when it
     has one; with several, the one README.md, CLAUDE.md or AGENTS.md cites). Free shell, so NEVER run by itself: it
     is a candidate (found['env_script'] with its sha256) and the question asks the person; with their OK the session
     runs `--approve <script>@<sha>`, kept in the state per repository and sha256 (entry['approved']). An approved
     script gives env_up/env_down (env_url); when its sha256 changes, the question comes back. While a candidate waits,
     the other sources are not used.
  2. the docker compose file at the repository root (standard names only): a candidate too (round 4: it reaches the
     host), approved the same way by the sha256 of the root file, its override and the root .env; the question names
     what reaches the host (privileged, docker.sock, host paths, network_mode/pid host, cap_add...). Not even a
     candidate with include: or extends:, a build context outside the repository, an env_file that is not a sample
     (.env.example, .env.test, .env.sample) nor absent, or no published port. Approved: its own project name per task
     (`-p "$QA_PROJECT"`) and the URL from the published port of the app (a service named web, app, front...; else
     the first one that is not a database, cache, broker or mail port: NOT_APP_PORTS; PLN0351);
  3. a Makefile target (env-up, qa-up, up, dev, start, serve, run), run in the background by qa_env.py (the URL is the
     first http(s) URL in its log). The Makefile is read whole after joining backslash continuations: any top-level
     line that is neither a plain rule nor `NAME = literal` without `$` refuses the file (include, define, export,
     override, SHELL, MAKEFLAGS, .RECIPEPREFIX, .ONESHELL, conditionals, $(shell), target-specific variables, an inline
     recipe, a continued recipe line). Each recipe line is an allowlisted simple command; a prerequisite is a file of
     the repository or a target that passes the same rule. A rule that remakes the Makefile itself (Makefile,
     makefile, GNUmakefile as a target) or a pattern rule refuses the file. No env_down from a Makefile;
  4. a `dev` or `start` script of a package.json (root first, then one or two folders down) that is an allowlisted
     simple command with no pre/post hook, in a package with no preinstall/install/postinstall/prepare, no workspaces
     (its own or the root's), no file:/link:/portal:/workspace: dependency and no pnpm-workspace.yaml; background too.
The Makefile runs as `make -r <target>` and is refused when a GNUmakefile or makefile (which make reads first) sits next
to it; a package is refused with a package-manager config that changes what runs (.yarnrc, .yarnrc.yml, .pnpmfile,
bunfig.toml, an .npmrc key outside the plain ones). All this reads the default branch, while qa_env.py runs the PR
branch: before the setup it calls branch_check() on the worktree, and a discovered command the branch changes (an
approved script with another sha256 included) is exit 4 with a PERGUNTAR line, never run.
`setup` (always with --ignore-scripts) is only the install of the package with @playwright/test (or of the package of step 4) by its lock file, never
for a package with install scripts; `node_dir` is the node_modules of the package that declares @playwright/test.

Exit 0 = cloned (or reused) and read; without anything to bring an environment up it still exits 0 (a dev worker only
needs the clone) and prints a PERGUNTAR line, the question for the person (qa_env.py exits 4 on it); 2 = the reference
is not a GitHub repository, the clone failed, ~/src/<name> is another repository, or --approve names another script or
another sha256; 4 = an owner the instance does not
trust (PERGUNTAR line).
"""
import argparse, glob, hashlib, io, json, os, re, shlex, shutil, subprocess, sys, tarfile, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths   # noqa: E402
from watch_core import behavior_names, fileio, procgroup   # noqa: E402

QA = 'acceptance-testing'
STATE_FILE = 'discovered.json'
ENV_KEYS = ('setup', 'env_up', 'env_url', 'env_down', 'url')     # one block: the person's env_up takes all of them
SRC_DIR = '~/src'
WORKTREES = '~/wt'
GH_RE = re.compile(r'^(?:https?://|ssh://git@|git@)?(?:www\.)?github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)'
                   r'(?:\.git)?(?:/(?:pull|pulls|tree|blob|issues)(?:/.*)?)?/?$')
SLUG_RE = re.compile(r'^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$')
NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
COMPOSE_FILES = ('compose.yaml', 'compose.yml', 'docker-compose.yml', 'docker-compose.yaml')
MAKE_UP = ('env-up', 'qa-up', 'up', 'dev', 'start', 'serve', 'run')
PKG_UP = ('dev', 'start')
DOCS = ('CLAUDE.md', 'AGENTS.md', 'README.md')
RULES = ('CLAUDE.md', 'AGENTS.md', 'CONTRIBUTING.md', 'README.md')
SAFE_PATH = re.compile(r'[A-Za-z0-9._/-]+')
# never an environment: deploy, production, release (word-ish, so "produção" in a comment or "reproduce" do not count)
DANGER = re.compile(r'(?<![A-Za-z])(deploy\w*|prod|production|release\w*|kubectl|helm|terraform|aws|gcloud|az|ssh|scp|'
                    r'rsync)(?![A-Za-z])', re.I)
# the allowlist (review of PLN0345, round 3): one simple command whose first words are one of these dev servers
DEV_SERVERS = (('vite',), ('next', 'dev'), ('nuxt', 'dev'), ('nuxi', 'dev'), ('astro', 'dev'), ('webpack', 'serve'),
               ('webpack-dev-server',), ('ng', 'serve'), ('react-scripts', 'start'), ('parcel',),
               ('vue-cli-service', 'serve'), ('svelte-kit', 'dev'), ('remix', 'dev'),
               ('dotnet', 'run'), ('dotnet', 'watch'), ('python', '-m', 'http.server'), ('python3', '-m', 'http.server'),
               ('flask', 'run'), ('uvicorn',), ('rails', 's'), ('rails', 'server'), ('php', '-S'), ('hugo', 'server'),
               ('jekyll', 'serve'))
# the flag that sets the listening host, per dev server (round 4): loopback only; with no flag, it is added when the
# command comes from a package.json (`npm run dev -- --host 127.0.0.1`), and a server that listens on every interface
# by default (OPEN_BY_DEFAULT) without it is refused in a Makefile (nothing can be added there)
HOST_FLAGS = {('vite',): ('--host',), ('next', 'dev'): ('-H', '--hostname'), ('nuxt', 'dev'): ('--host',),
              ('nuxi', 'dev'): ('--host',), ('astro', 'dev'): ('--host',), ('webpack', 'serve'): ('--host',),
              ('webpack-dev-server',): ('--host',), ('ng', 'serve'): ('--host',), ('parcel',): ('--host',),
              ('vue-cli-service', 'serve'): ('--host',), ('svelte-kit', 'dev'): ('--host',),
              ('python', '-m', 'http.server'): ('--bind', '-b'), ('python3', '-m', 'http.server'): ('--bind', '-b'),
              ('flask', 'run'): ('--host', '-h'), ('uvicorn',): ('--host',), ('rails', 's'): ('-b', '--binding'),
              ('rails', 'server'): ('-b', '--binding'), ('hugo', 'server'): ('--bind',), ('jekyll', 'serve'): ('--host', '-H')}
OPEN_BY_DEFAULT = {('next', 'dev'), ('python', '-m', 'http.server'), ('python3', '-m', 'http.server'), ('parcel',)}
LOOPBACK = {'127.0.0.1', 'localhost', '::1'}
MAKEFILE_NAMES = {'Makefile', 'makefile', 'GNUmakefile'}
TOKEN_RE = re.compile(r'[A-Za-z0-9._=:/@,+%-]+')
MAKE_SPECIAL_VARS = {'SHELL', 'MFLAGS', 'GNUMAKEFLAGS', 'VPATH', 'SUFFIXES'}
SAMPLE_ENV = {'.env.example', '.env.test', '.env.sample'}
LIFECYCLE = ('preinstall', 'install', 'postinstall', 'prepare')
LOCAL_DEP = re.compile(r'^(file|link|portal|workspace):')
COMPOSE_OVERRIDES = ('compose.override.yaml', 'compose.override.yml', 'docker-compose.override.yml',
                     'docker-compose.override.yaml')
HOST_ACCESS = (('privileged', r'(?m)^\s*privileged\s*:\s*true'), ('docker.sock', r'docker\.sock'),
               ('network_mode host', r'(?m)^\s*network_mode\s*:\s*["\']?host'),
               ('pid/ipc/userns host', r'(?m)^\s*(pid|ipc|userns_mode)\s*:\s*["\']?host'),
               ('cap_add', r'(?m)^\s*cap_add\s*:'), ('devices', r'(?m)^\s*devices\s*:'),
               ('security_opt', r'(?m)^\s*security_opt\s*:'),
               ('volume de caminho do host', r'(?m)^\s*-\s*["\']?(/|~|\$)[^:\s]*:'),
               # its own label (PLN0351): an approval saved before this rule did not see it, so the question comes back
               ('volume acima do repositorio (../)', r'(?m)^\s*-\s*["\']?\.\./[^:\s]*:'),
               ('bind em sintaxe longa', r'(?m)^\s*-?\s*type\s*:\s*["\']?bind\b'),
               ('secrets/configs de arquivo (file:)', r'(?m)^\s*file\s*:'),
               ('variavel do .env', r'\$\{?[A-Za-z_]'))
# what two test environments of the same compose share by name (PLN0351): the second up collides or mixes data
SHARED = (('container_name fixo', r'(?m)^\s*container_name\s*:'), ('volume ou rede externa', r'(?m)^\s*external\s*:\s*true'),
          ('volume ou rede com name fixo', r'(?m)^\s{2,}name\s*:'))
# published ports that are not the app (database, cache, broker, mail): never the URL to test (PLN0351)
NOT_APP_PORTS = {25, 587, 1025, 1433, 1521, 2181, 3306, 4222, 5432, 5672, 6379, 6380, 7687, 8025, 9042, 9092, 9200,
                 9300, 11211, 15672, 26257, 27017, 33060}
APP_SERVICES = ('web', 'app', 'front', 'frontend', 'site', 'ui', 'client', 'server', 'api', 'nginx', 'proxy')
REF_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}')
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'vendor', '.venv', 'venv', 'bin', 'obj'}


def _rules_version():
    """The version of the discovery rules: the sha256 of this file. A saved discovery made by other rules is read
    again (PLN0351: a new risk rule reaches what was already discovered without waiting for the repository's main to
    move). Computed from the file, so no rule change can forget to bump it."""
    try:
        with open(os.path.abspath(__file__), 'rb') as f: return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return ''


RULES_VERSION = _rules_version()


def current(entry):
    """A saved discovery made by these rules."""
    return bool(entry) and entry.get('rules_version') == RULES_VERSION


class Failed(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# ------------------------------------------------------------------ the reference: pr_url, repository URL, owner/name

def parse(ref):
    """(slug, name, clone source, kind) of a GitHub PR or repository reference (link, ssh form or owner/name). kind is
    always 'github'. Raises Failed(2) for anything else: another git host, a local path, file://, a github.com link this
    does not read (never a guessed name)."""
    ref = (ref or '').strip()
    m = GH_RE.match(ref) or SLUG_RE.match(ref)
    if m and NAME_RE.fullmatch(m.group(1)) and NAME_RE.fullmatch(m.group(2)) and not m.group(2).endswith('.'):
        slug = f'{m.group(1)}/{m.group(2)}'
        return slug, m.group(2), slug, 'github'
    raise Failed(2, f'{ref!r} nao e link de PR nem de repositorio do GitHub (nem dono/nome): so o GitHub e descoberto')


def origin_slug(url):
    """owner/name of a GitHub origin URL, or None (another host, an ssh alias, a local path)."""
    m = GH_RE.match((url or '').strip())
    return f'{m.group(1)}/{m.group(2)}' if m else None


def loose_slug(url):
    """owner/name at the end of any git URL (an ssh alias such as git@alias:owner/name.git), or None."""
    m = re.search(r'[:/]([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*?)(?:\.git)?/?$', (url or '').strip())
    return f'{m.group(1)}/{m.group(2)}' if m else None


def same_origin(url, slug, kind='github'):
    """True when the origin URL of a clone is the GitHub repository `slug`."""
    o = origin_slug(url)
    return bool(o) and o.lower() == slug.lower()


# ------------------------------------------------------------------ state (data/discovered.json of the instance)

def state_path():
    return os.path.join(paths.DATA_DIR, STATE_FILE)


def load_state():
    if not paths.DATA_DIR: return {'schema': 1, 'repos': {}}      # no instance chosen (a unit test of the brief)
    try:
        with open(state_path(), encoding='utf-8') as f: st = json.load(f)
    except (OSError, ValueError):
        st = None
    if not isinstance(st, dict) or not isinstance(st.get('repos'), dict): st = {'schema': 1, 'repos': {}}
    return st


def save_state(st):
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    fileio.write_json(state_path(), st, indent=1, ensure_ascii=False)


def tilde(p):
    h = os.path.expanduser('~')
    return '~' + p[len(h):] if p == h or p.startswith(h + '/') else p


# ------------------------------------------------------------------ clone

def gh_env(cfg):
    """The environment of the instance's `gh` tool (e.g. its GH_CONFIG_DIR) over ours: the account already logged in."""
    env = dict(os.environ)
    for t in (cfg or {}).get('tools') or []:
        if isinstance(t, dict) and t.get('kind') == 'cli' and t.get('name') == 'gh':
            env.update({k: os.path.expanduser(str(v)) for k, v in (t.get('env') or {}).items()})
    env['GIT_TERMINAL_PROMPT'] = '0'       # never ask for a credential: the gh logged in, or a clear failure
    return env


def run(cmd, env=None, cwd=None, timeout=600, stdout_only=False):
    try:
        r = procgroup.run(cmd, timeout, text=True, env=env, cwd=cwd)     # a timeout stops git-remote-* too (PLN0351)
        return r.returncode, (r.stdout or '') + ('' if stdout_only else (r.stderr or ''))
    except subprocess.TimeoutExpired:
        return 124, f'passou de {timeout} s'
    except OSError as e:
        return 127, str(e)


def allowed_owners(cfg):
    """(owners the instance trusts, lowercase; where they came from). The config's repo_discovery.allowed_owners, plus
    the account of the instance's gh and its organizations."""
    rd = (cfg or {}).get('repo_discovery') if isinstance((cfg or {}).get('repo_discovery'), dict) else {}
    own = {o.lower() for o in rd.get('allowed_owners') or [] if isinstance(o, str) and o.strip()}
    env = gh_env(cfg)
    gh = set()
    if shutil.which('gh', path=env.get('PATH')):
        for api, jq in (('user', '.login'), ('user/orgs', '.[].login')):
            code, out = run(['gh', 'api', api, '--jq', jq], env, timeout=30, stdout_only=True)
            if code == 0: gh |= {x.strip().lower() for x in out.splitlines() if NAME_RE.fullmatch(x.strip())}
    return own | gh, ('config' if own else '') + (' e ' if own and gh else '') + ('conta do gh' if gh else '')


def check_owner(cfg, slug):
    owners, _ = allowed_owners(cfg)
    owner = slug.split('/')[0].lower()
    if owner not in owners:
        raise Failed(4, f'o dono {slug.split("/")[0]} de {slug} nao e a conta do gh desta instancia, nem uma organizacao '
                        f'dela, nem esta em "repo_discovery.allowed_owners": nada foi clonado.\nPERGUNTAR: O repositorio '
                        f'{slug} e confiavel para eu clonar e rodar os comandos dele? Se for, ponha "{slug.split("/")[0]}" '
                        f'em "repo_discovery.allowed_owners" do config.json da instancia (no computador), ou o '
                        f'repositorio em "repos".')


def clone(cfg, ref):
    """(path, slug, url, kind): the clone in ~/src/<name>, made now or reused when its origin is the same repository."""
    slug, name, source, kind = parse(ref)
    check_owner(cfg, slug)
    dest = os.path.join(os.path.expanduser(SRC_DIR), name)
    env = gh_env(cfg)
    if os.path.exists(dest):
        code, out = run(['git', '-C', dest, 'config', '--get', 'remote.origin.url'], env)
        if code != 0 or not same_origin(out.strip(), slug, kind):
            raise Failed(2, f'{tilde(dest)} ja existe e nao e um clone de {slug}: nao mexo nele (renomeie a pasta ou '
                            f'ponha o repositorio em "repos" do config)')
        run(['git', '-C', dest, 'fetch', '-q', 'origin'], env, timeout=300)
        return dest, slug, source, kind
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if shutil.which('gh', path=env.get('PATH')):
        code, out = run(['gh', 'repo', 'clone', slug, dest, '--', '-q'], env)
    else:
        code, out = run(['git', 'clone', '-q', f'https://github.com/{slug}.git', dest], env)
    if code != 0:
        shutil.rmtree(dest, ignore_errors=True)
        tail = ' '.join(out.strip().splitlines()[-3:])[-300:]
        raise Failed(2, f'clone de {slug} falhou (saida {code}): {tail}. O gh desta maquina enxerga o repositorio? '
                        f'(gh auth status; a conta e a do "env" da ferramenta gh em "tools")')
    return dest, slug, source, kind


# ------------------------------------------------------------------ what the repository says (an allowlist)

def read(path, limit=200_000):
    try:
        with open(path, encoding='utf-8', errors='replace') as f: return f.read(limit)
    except OSError:
        return ''


def code_of(text):
    """The text without shell/Makefile/YAML comments (a comment may say "production" harmlessly). A `#` inside quotes
    is text, not a comment, so it never hides the rest of the line from the checks."""
    out = []
    for ln in text.splitlines():
        quote, cut = None, len(ln)
        for i, ch in enumerate(ln):
            if quote:
                if ch == quote: quote = None
            elif ch in '"\'': quote = ch
            elif ch == '#' and (i == 0 or ln[i - 1].isspace()): cut = i; break
        out.append(ln[:cut])
    return '\n'.join(out)


def present(path):
    """The name is there, as a file, a folder or a link (even a broken one: a link to node_modules/... exists after the
    setup). For a config name, being there at all is the point, so never os.path.exists, which follows links."""
    return os.path.lexists(path)


def linked(root, *names):
    return any(os.path.islink(os.path.join(root, n)) for n in names)


def safe(rel):
    """A relative path that may go into a command: [A-Za-z0-9._/-] only, and no part starting with "-"."""
    return bool(SAFE_PATH.fullmatch(rel)) and not any(part.startswith('-') for part in rel.split('/'))


def risky(*texts):
    return any(DANGER.search(t or '') for t in texts)


def q(path):
    return shlex.quote(path)


def simple_command(line, root=None, allowed=DEV_SERVERS):
    """The argv of `line` when it is ONE simple command of the allowlist, else None: no shell syntax at all
    ($ ` | < > ; & ( ) \\ or a newline), plain tokens only (no "..", no absolute or home path), the first words one of
    `allowed`, no risky word, and never a listening host other than loopback."""
    line = line.strip()
    if not line or any(c in line for c in '$`|<>;&()\\\n\r'): return None
    try: argv = shlex.split(line)
    except ValueError: return None
    if not argv or risky(line): return None
    if not all(TOKEN_RE.fullmatch(t) and '..' not in t and not t.startswith(('/', '~')) for t in argv): return None
    if not any(tuple(argv[:len(p)]) == p for p in allowed): return None
    if binding(argv) == 'bad': return None
    return argv


def binding(argv):
    """'ok' (loopback, or a server with no host flag), 'add' (a host flag exists and is missing) or 'bad' (another host,
    or a bare --host that means every interface)."""
    if argv[:2] == ['php', '-S']:
        return 'ok' if len(argv) > 2 and argv[2].rsplit(':', 1)[0] in LOOPBACK else 'bad'
    server = next((p for p in HOST_FLAGS if tuple(argv[:len(p)]) == p), None)
    if not server: return 'ok'
    flags, rest = HOST_FLAGS[server], argv[len(server):]
    seen = []                      # every occurrence: the last one usually wins, so all of them must be loopback
    for i, t in enumerate(rest):
        for f in flags:
            if t == f: seen.append(rest[i + 1] if i + 1 < len(rest) else '')
            elif t.startswith(f + '='): seen.append(t[len(f) + 1:])
            elif len(f) == 2 and t.startswith(f) and len(t) > 2: seen.append(t[2:])    # glued: -h0.0.0.0
    if not seen: return 'add'
    return 'ok' if all(v in LOOPBACK for v in seen) else 'bad'


def host_args(argv):
    """The flag that keeps a dev server on loopback, when its command does not say it: ['--host', '127.0.0.1']."""
    server = next((p for p in HOST_FLAGS if tuple(argv[:len(p)]) == p), None)
    return [HOST_FLAGS[server][0], '127.0.0.1'] if server and binding(argv) == 'add' else []


def env_script(root, docs):
    """The candidate environment script ({value, source, sha256, url}) or None. Never run by itself: it is free shell,
    so the person approves it first (--approve, kept per repository and sha256 of the script)."""
    found = []
    for pat in ('scripts/*env*.sh', 'bin/*env*.sh'):
        for p in sorted(glob.glob(os.path.join(root, pat))):
            if os.path.islink(p): continue
            rel, text = os.path.relpath(p, root), read(p)
            if not safe(rel) or risky(rel, code_of(text)): continue
            if has_case(text, 'up') and has_case(text, 'down'):
                found.append((rel, has_case(text, 'url'), hashlib.sha256(text.encode()).hexdigest()))
    if not found: return None
    cited = [x for x in found if any(x[0] in d for d in docs.values())]
    rel, url, h = (cited or found)[0]
    by = next((n for n, d in docs.items() if rel in d), None)
    return {'value': rel, 'source': rel + (f' (citado no {by})' if by else ''), 'sha256': h, 'url': url}


def has_case(text, label):
    """A shell case branch for `label`: `up)`, `"up")`, `up|start)`."""
    return bool(re.search(rf'(?m)^\s*\(?\s*["\']?(?:[\w-]+\s*\|\s*)*{label}(?:\s*\|\s*[\w-]+)*["\']?\s*\)', text))


def env_files_ok(root, text):
    """Every env_file of the compose is absent from the repository or a sample (.env.example, .env.test, .env.sample);
    an env_file this does not read (a mapping, a variable) fails closed."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r'^(\s*)-?\s*env_file\s*:\s*(.*)$', ln)
        if not m: continue
        rest, values = m.group(2).strip(), []
        if rest:
            values = [v.strip().strip('"\'') for v in rest.strip('[]').split(',')]
        else:
            ind = len(m.group(1))
            for nxt in lines[i + 1:]:
                if not nxt.strip(): continue
                if len(nxt) - len(nxt.lstrip()) <= ind: break
                mm = re.match(r'^\s*-\s*(?:path\s*:\s*)?(.+)$', nxt)
                if mm: values.append(mm.group(1).strip().strip('"\''))
                elif re.match(r'^\s*(required|format)\s*:', nxt): continue
                else: return False
        for v in values:
            if not v or not SAFE_PATH.fullmatch(v) or '..' in v: return False
            if os.path.basename(v) not in SAMPLE_ENV and present(os.path.join(root, v)): return False
            if os.path.islink(os.path.join(root, v)): return False
    return True


def compose(root):
    """The root compose file (the standard names only) as a CANDIDATE, or None. Never run by itself (round 4: a compose
    file can reach the host: privileged, docker.sock, host paths, network_mode host...): the person approves it by the
    sha256 of the root file, its override and the .env of the root (what `docker compose` reads besides it). Refused
    outright: include or extends at all (other files the sha would not cover), a build context outside the repository,
    a risky word, an env_file that is not a sample, no published port (no URL to test)."""
    f = next((n for n in COMPOSE_FILES if present(os.path.join(root, n))), None)
    if not f or linked(root, *COMPOSE_FILES, *COMPOSE_OVERRIDES, '.env'): return None
    text = read(os.path.join(root, f))
    code = code_of(text)
    if risky(re.sub(r'(?m)^\s*deploy:\s*$', '', code)): return None
    if re.search(r'(?m)^\s*-?\s*(include|extends)\s*:', code) or re.search(r'[{,]\s*(include|extends)\s*:', code):
        return None
    if not env_files_ok(root, code): return None
    for m in re.finditer(r'(?m)^[ \t]*(?:build|context)[ \t]*:[ \t]*["\']?([^\s"\'{#]+)', code):
        v = m.group(1)
        if v.startswith(('/', '~', '$')) or '..' in v.split('/') or '://' in v: return None
    published = compose_ports(code)
    app = [p for p in published if p[2] not in NOT_APP_PORTS]
    if not app: return None
    service, port, _target = next((p for name in APP_SERVICES for p in app if p[0].lower() == name), app[0])   # tuple order
    h = hashlib.sha256()
    covered = [f] + [n for n in COMPOSE_OVERRIDES + ('.env',) if os.path.isfile(os.path.join(root, n))]
    for n in covered:
        h.update(n.encode() + b'\0' + read(os.path.join(root, n)).encode() + b'\0')
    notes = [label for label, rx in HOST_ACCESS if any(re.search(rx, code_of(read(os.path.join(root, n))))
                                                       for n in covered if n != '.env')]
    shared = [label for label, rx in SHARED if any(re.search(rx, code_of(read(os.path.join(root, n))))
                                                   for n in covered if n != '.env')]
    return {'value': f, 'source': f'{f} (docker compose da raiz)', 'sha256': h.hexdigest(), 'kind': 'compose',
            'port': str(port), 'service': service, 'ports': sorted({p[1] for p in published}), 'covers': covered,
            'notes': notes, 'shared': shared}


def compose_ports(code):
    """[(service, host port, container port)] published by the compose text, in order: the short syntax
    ("8080:80", "127.0.0.1:8080:80") and the long one (published: / target:). A range or a port without a fixed host
    side publishes nothing fixed and is left out."""
    out, service, svc_indent, in_services, item = [], None, None, False, {}
    def flush():
        if item.get('published') and item.get('target'): out.append((service or '?', item['published'], item['target']))
        item.clear()
    for line in code.splitlines():
        if not line.strip(): continue
        ind = len(line) - len(line.lstrip())
        if ind == 0:
            flush(); in_services = bool(re.match(r'^services\s*:\s*$', line)); service = svc_indent = None; continue
        m = re.match(r'^\s*([A-Za-z0-9._-]+)\s*:\s*$', line)
        if in_services and m and (svc_indent is None or ind <= svc_indent):
            flush(); svc_indent, service = ind, m.group(1); continue
        m = re.match(r'^\s*-\s*["\']?(?:(?:\d{1,3}\.){3}\d{1,3}:)?(\d{2,5}):(\d{2,5})(?:/tcp)?["\']?\s*$', line)
        if m:
            flush(); out.append((service or '?', int(m.group(1)), int(m.group(2)))); continue
        m = re.match(r'^\s*(-\s*)?(published|target)\s*:\s*["\']?(\d{2,5})["\']?\s*$', line)
        if m:
            if m.group(1): flush()
            item[m.group(2)] = int(m.group(3))
        elif line.lstrip().startswith('-'): flush()
    flush()
    return out


def make_rules(root):
    """{target: (prerequisites, recipe lines)} of the root Makefile, read whole, or None when any top-level line is not
    a plain rule nor `NAME = literal` without `$` (include, define, export, override, SHELL, .RECIPEPREFIX, .ONESHELL,
    conditionals, $(shell ...), target-specific variables...). Continuations with a backslash are joined first."""
    if any(present(os.path.join(root, n)) for n in ('GNUmakefile', 'makefile')): return None   # make reads those first
    if linked(root, 'Makefile'): return None
    text = read(os.path.join(root, 'Makefile'))
    if not text: return {}
    joined, buf, cont = [], '', False
    for ln in text.split('\n'):
        if ln.endswith('\\'): buf += ln[:-1] + ' '; cont = True; continue
        joined.append((buf + ln, cont)); buf, cont = '', False
    if buf: joined.append((buf, True))
    rules, cur = {}, None
    for ln, cont in joined:
        if ln.startswith('\t'):
            if cur is None or cont: return None       # a recipe line continued with a backslash: not read
            if ln[1:].strip().startswith('#'): continue
            for n in cur: rules[n][1].append(ln[1:])
            continue
        s = code_of(ln).rstrip()
        if not s.strip(): continue
        a = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?:=|:=|::=|\?=)[ \t]*([^$]*)', s)
        if a:
            if a.group(1) in MAKE_SPECIAL_VARS or a.group(1).startswith('MAKE'): return None
            cur = None
            continue
        r = re.fullmatch(r'([A-Za-z0-9_.%/-]+(?:[ \t]+[A-Za-z0-9_.%/-]+)*)[ \t]*:(?![:=])[ \t]*([A-Za-z0-9_.%/ \t-]*)', s)
        if not r: return None
        targets = r.group(1).split()
        if any(t.startswith('.') and t != '.PHONY' for t in targets): return None
        if any('%' in t or os.path.basename(t) in MAKEFILE_NAMES for t in targets): return None   # make runs those first
        for t in targets:
            rules.setdefault(t, ([], []))[0].extend(r.group(2).split())
        cur = targets
    return rules


def target_ok(t, rules, root, seen=()):
    """The target runs only allowlisted simple commands, and so does every prerequisite with a rule; a prerequisite
    without a rule must be a file of the repository. A dev server that listens on every interface by default needs its
    loopback flag in the recipe (nothing can be added to a make target)."""
    if t in seen or t not in rules: return False
    pre, recipe = rules[t]
    for ln in (ln.strip().lstrip('@+-').strip() for ln in recipe if ln.strip()):
        argv = simple_command(ln, root)
        if not argv: return False
        if host_args(argv) and next((p for p in OPEN_BY_DEFAULT if tuple(argv[:len(p)]) == p), None): return False
    for d in pre:
        if d in rules:
            if not target_ok(d, rules, root, seen + (t,)): return False
        elif not os.path.isfile(os.path.join(root, d)):
            return False
    return True


def makefile(root):
    rules = make_rules(root)
    if not rules: return None
    up = next((t for t in MAKE_UP if t in rules and rules[t][1] and target_ok(t, rules, root)), None)
    if not up: return None
    return {'env_up': (f'make -r {up}', f'Makefile (alvo {up})', 'background')}   # -r: no built-in implicit rules


def clip(text):
    return ' '.join(str(text).split())[:120]


def make_candidate(root):
    """What the Makefile offers that the allowlist does not take, for the question: (command text, source) or None."""
    if not present(os.path.join(root, 'Makefile')): return None
    rules = make_rules(root)
    if rules is None:
        return ('Makefile', 'Makefile (usa include, variaveis com $, define, SHELL, regra que refaz o proprio Makefile '
                            'ou com %, GNUmakefile/makefile ao lado ou outra construcao que nao leio)')
    t = next((t for t in MAKE_UP if t in rules and rules[t][1]), None)
    return (clip(' ; '.join(ln.strip() for ln in rules[t][1])), f'Makefile (alvo {t})') if t else None


def package_candidate(root, pkgs):
    for rel, pkg in pkgs:
        scripts = scripts_of(pkg)
        s = next((x for x in PKG_UP if isinstance(scripts.get(x), str)), None)
        if s:
            where = os.path.join(rel, 'package.json') if rel != '.' else 'package.json'
            why = package_block(root, rel, pkg, dict(pkgs).get('.'))
            return clip(scripts[s]), f'{where} (script {s}' + (f'; {why}' if why else '') + ')'
    return None


def packages(root):
    """[(relative dir, package.json dict)] of the root and one or two folders down, root first."""
    out = []
    for depth in (0, 1, 2):
        pat = os.path.join(root, *(['*'] * depth), 'package.json')
        for p in sorted(glob.glob(pat)):
            if os.path.islink(p): continue                   # a package.json that is a link: never read
            rel = os.path.relpath(os.path.dirname(p), root)
            if not safe(rel): continue
            if any(part in SKIP_DIRS or part.startswith('.') for part in rel.split(os.sep) if part != '.'): continue
            try: pkg = json.loads(read(p))
            except ValueError: continue
            if isinstance(pkg, dict): out.append(('.' if rel == '.' else rel, pkg))
    return out


def in_dir(rel, cmd):
    return cmd if rel == '.' else f'cd {q(rel)} && {cmd}'


def runner_of(root, rel):
    d = os.path.join(root, rel)
    if os.path.isfile(os.path.join(d, 'pnpm-lock.yaml')): return 'pnpm', 'pnpm install --frozen-lockfile --ignore-scripts'
    if os.path.isfile(os.path.join(d, 'yarn.lock')): return 'yarn', 'yarn install --frozen-lockfile --ignore-scripts'
    if os.path.isfile(os.path.join(d, 'package-lock.json')): return 'npm', 'npm ci --ignore-scripts'
    return 'npm', 'npm install --ignore-scripts'


def scripts_of(pkg):
    return pkg.get('scripts') if isinstance(pkg.get('scripts'), dict) else {}


NPMRC_KEYS = re.compile(r'(@[\w.-]+:)?registry|save-exact|save-prefix|engine-strict|legacy-peer-deps|fund|audit|'
                        r'strict-peer-dependencies|auto-install-peers|package-lock|prefer-offline|progress|loglevel')
PM_CONFIG = ('.yarnrc', '.yarnrc.yml', '.pnpmfile.cjs', '.pnpmfile.js', 'bunfig.toml')


def chain(root, rel):
    """Every folder from the root down to the package, both included: package managers look for their config (and a
    workspace root) walking up from the package, so any of them counts."""
    out, d = [root], root
    for part in ([] if rel in ('.', '') else rel.split('/')):
        d = os.path.join(d, part); out.append(d)
    return out


def pm_config_ok(root, rel):
    """The package manager runs what the scripts say: no .yarnrc/.yarnrc.yml (yarnPath, plugins), no .pnpmfile, no
    bunfig, and an .npmrc with plain keys only (never script-shell, node-options, shell, onload-script...), in any
    folder from the root down to the package."""
    for d in chain(root, rel):
        if any(present(os.path.join(d, n)) for n in PM_CONFIG): return False
        if glob.glob(os.path.join(glob.escape(d), '.pnpmfile.*')): return False      # .cjs, .mjs, .js, .ts...
        npmrc = os.path.join(d, '.npmrc')
        if os.path.islink(npmrc): return False         # even broken: it may point at node_modules after the setup
        if present(npmrc):
            for ln in code_of(read(npmrc)).splitlines():
                ln = ln.strip()
                if ln.startswith(';') or not ln: continue
                k, eq, v = ln.partition('=')
                if not eq or not NPMRC_KEYS.fullmatch(k.strip()) or '$' in v: return False
    return True


def package_block(root, rel, pkg, top=None):
    """Why the package is not used by itself (for the question), or None: install scripts of its own (the setup runs
    with --ignore-scripts, so a package that needs them is the person's call), workspaces or a local dependency
    (file:, link:, portal:, workspace:, whose own scripts nobody read), pnpm-workspace.yaml (its settings change the
    shell, scriptShell), or a package-manager config that changes what runs."""
    if os.path.realpath(os.path.join(root, rel)) != os.path.join(os.path.realpath(root), *([] if rel == '.' else [rel])):
        return 'o caminho do pacote passa por um link simbolico'     # npm and pnpm walk the real path, not this one
    if any(os.path.islink(os.path.join(d, 'package.json')) for d in chain(root, rel)):
        return 'um package.json da raiz ate o pacote e link simbolico'   # read as absent, it would hide workspaces
    middle = []
    for d in chain(root, rel)[1:-1]:          # a package.json between the root and the package (a workspace root)
        try: middle.append(json.loads(read(os.path.join(d, 'package.json')) or 'null'))
        except ValueError: return 'package.json ilegivel numa pasta acima do pacote'
    for p in (pkg, top if rel != '.' else None, *middle):
        if not isinstance(p, dict): continue
        if not installs_clean(p): return 'o pacote tem script de instalacao (preinstall/install/postinstall/prepare)'
        if 'workspaces' in p: return 'workspaces no package.json'
        deps = {}
        for k in ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'):
            if isinstance(p.get(k), dict): deps.update(p[k])
        if any(isinstance(v, str) and LOCAL_DEP.match(v) for v in deps.values()):
            return 'dependencia local file:/link:/portal:/workspace:'
    if any(present(os.path.join(d, 'pnpm-workspace.yaml')) for d in chain(root, rel)):
        return 'pnpm-workspace.yaml'
    if not pm_config_ok(root, rel): return 'configuracao do gerenciador de pacotes (.npmrc, .yarnrc, .pnpmfile, bunfig)'
    return None


def installs_clean(pkg):
    """False when installing the package runs its own lifecycle scripts (preinstall, install, postinstall, prepare)."""
    return not any(k in scripts_of(pkg) for k in LIFECYCLE)


def script_ok(scripts, name, root):
    """A package.json script that is one allowlisted simple command, with no pre/post hook."""
    body = scripts.get(name)
    return (isinstance(body, str) and f'pre{name}' not in scripts and f'post{name}' not in scripts
            and simple_command(body, root) is not None)


def package_env(root, pkgs):
    top = dict(pkgs).get('.')
    for rel, pkg in pkgs:
        scripts = scripts_of(pkg)
        if package_block(root, rel, pkg, top): continue
        s = next((x for x in PKG_UP if script_ok(scripts, x, os.path.join(root, rel))), None)
        if s:
            tool = runner_of(root, rel)[0]
            where = os.path.join(rel, 'package.json') if rel != '.' else 'package.json'
            extra = host_args(shlex.split(scripts[s]))
            cmd = f'{tool} run {s}' + ((' -- ' if tool == 'npm' else ' ') + ' '.join(extra) if extra else '')
            return rel, {'env_up': (in_dir(rel, cmd), f'{where} (script {s})', 'background')}
    return None, None


def playwright_pkg(pkgs):
    for rel, pkg in pkgs:
        deps = {**(pkg.get('dependencies') or {}), **(pkg.get('devDependencies') or {})}
        if '@playwright/test' in deps: return rel
    return None


def discover(root, base=None):
    """{key: {value, source[, mode]}} of what the repository files at `root` say (a snapshot, or a working tree).
    Keys: rules, base, setup, env_up, env_url, env_down, url, node_dir, and env_script (a candidate the person has to
    approve; while it is there, the other sources are not used: the question comes first)."""
    docs = {n: read(os.path.join(root, n)) for n in DOCS if os.path.isfile(os.path.join(root, n))}
    found = {}

    def put(k, v):
        found[k] = {'value': v[0], 'source': v[1], **({'mode': v[2]} if len(v) > 2 else {})}

    rules = next((n for n in RULES if os.path.isfile(os.path.join(root, n))), None)
    if rules: put('rules', (rules, rules))
    if base is None:
        code, out = run(['git', '-C', root, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD'])
        if code == 0 and out.strip().startswith('origin/'): base = out.strip()[7:]
    if base and REF_RE.fullmatch(base) and '..' not in base: put('base', (base, 'origin/HEAD'))
    pkgs = packages(root)
    env_pkg, env = None, None
    script = env_script(root, docs) or compose(root)
    if script: found['env_script'] = script
    else:
        env = makefile(root)
        if not env: env_pkg, env = package_env(root, pkgs)
        if not env:
            cand = make_candidate(root) or package_candidate(root, pkgs)
            if cand: found['candidate'] = {'value': cand[0], 'source': cand[1]}
    for k, v in (env or {}).items(): put(k, v)
    pw = playwright_pkg(pkgs)
    if pw is not None:
        put('node_dir', (os.path.normpath(os.path.join(pw, 'node_modules')), os.path.join(pw, 'package.json')
                         if pw != '.' else 'package.json'))
    rel = pw if pw is not None else env_pkg
    pkg = dict(pkgs).get(rel) if rel is not None else None
    if rel is not None and not package_block(root, rel, pkg or {}, dict(pkgs).get('.')):
        install = runner_of(root, rel)[1]
        put('setup', (in_dir(rel, install), (os.path.join(rel, 'package.json') if rel != '.' else 'package.json')
                      + ' (lock)'))
    return found


# the warnings the question had before PLN0351: an approval saved without its warnings saw at most these
OLD_WARNINGS = {'privileged', 'docker.sock', 'network_mode host', 'pid/ipc/userns host', 'cap_add', 'devices',
                'security_opt', 'volume de caminho do host', 'variavel do .env'}


def warnings_of(cand):
    return sorted(set(cand.get('notes') or []) | set(cand.get('shared') or []))


def unseen_warnings(cand, ok):
    """The warnings of the candidate the person did not see when approving (a rule added later, same sha256): the
    approval does not cover them, the question comes back."""
    seen = set(ok['warnings']) if isinstance(ok.get('warnings'), list) else OLD_WARNINGS
    return [w for w in warnings_of(cand) if w not in seen]


def apply_approval(entry):
    """The environment of an approved script: env_up/env_down (and env_url) from it when the person approved this
    sha256 for this repository; while not approved, no environment (the question). Idempotent."""
    found = entry.setdefault('found', {})
    cand = found.get('env_script')
    if not cand: return entry
    for k in ENV_KEYS:
        if k != 'setup': found.pop(k, None)
    ok = (entry.get('approved') or {}).get(cand['value'])
    if ok and ok.get('sha256') == cand['sha256'] and not unseen_warnings(cand, ok):
        src = f'{cand["value"]} (aprovado pela pessoa em {ok.get("at")}, sha256 {cand["sha256"][:12]})'
        rel = q(cand['value'])
        if cand.get('kind') == 'compose':
            base = f'docker compose -f {rel} -p "$QA_PROJECT"'
            found['env_up'] = {'value': f'{base} up -d --build', 'source': src}
            found['env_down'] = {'value': f'{base} down -v', 'source': src}
            found['url'] = {'value': f'http://127.0.0.1:{cand["port"]}', 'source': src + ' (porta publicada)'}
            return entry
        found['env_up'] = {'value': f'{rel} up', 'source': src}
        found['env_down'] = {'value': f'{rel} down', 'source': src}
        if cand.get('url'): found['env_url'] = {'value': f'{rel} url', 'source': src}
    return entry


# ------------------------------------------------------------------ resolve: clone + discover + save

def default_tip(path):
    """(default branch, sha of origin/<it>) of a clone after its fetch; ('', '') when origin/HEAD is unknown."""
    code, out = run(['git', '-C', path, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD'])
    if code != 0 or not out.strip().startswith('origin/'):
        run(['git', '-C', path, 'remote', 'set-head', 'origin', '--auto'], timeout=60)
        code, out = run(['git', '-C', path, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD'])
    if code != 0 or not out.strip().startswith('origin/'): return '', ''
    base = out.strip()[7:]
    code, sha = run(['git', '-C', path, 'rev-parse', '--verify', '-q', f'origin/{base}^{{commit}}'])
    return base, (sha.strip() if code == 0 else '')


def discover_at(path, sha, base):
    """discover() on the files of commit `sha` (git archive into a temporary folder), never the clone's working tree,
    which the reuse never moves."""
    tmp = tempfile.mkdtemp(prefix='discovery-')
    try:
        r = subprocess.run(['git', '-C', path, 'archive', '--format=tar', sha], capture_output=True, timeout=300)
        if r.returncode != 0: return discover(path, base)
        try:
            with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
                if hasattr(tarfile, 'data_filter'): tf.extractall(tmp, filter='data')
                else: tf.extractall(tmp, members=[m for m in tf.getmembers() if m.isfile() or m.isdir()])
        except (tarfile.TarError, OSError) as e:     # an absolute link, a link out of the repository...
            name = getattr(getattr(e, 'tarinfo', None), 'name', None)
            why = (f'o repositorio tem {clip(name)}, um link absoluto ou para fora dele' if name
                   else f'nao consegui ler a branch principal ({clip(type(e).__name__)})')
            return {'refused': {'value': why, 'source': 'git archive da branch principal'}}
        return discover(tmp, base)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def resolve(cfg, ref, project=None, refresh=False):
    """The discovered entry of `ref` (owner checked, cloned or reused and fetched, discovered, saved). The saved
    discovery is kept while the sha of origin's default branch does not change, unless `refresh`."""
    path, slug, url, kind = clone(cfg, ref)
    st = load_state()
    base, sha = default_tip(path)
    old = st['repos'].get(slug)
    if old and not refresh and sha and old.get('sha') == sha and current(old):
        entry = old
    else:
        found = discover_at(path, sha, base) if sha else discover(path)
        entry = {'slug': slug, 'url': url, 'kind': kind, 'path': tilde(path), 'worktrees': WORKTREES,
                 'sha': sha, 'rules_version': RULES_VERSION, 'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'found': found,
                 'projects': list((old or {}).get('projects') or []), 'approved': dict((old or {}).get('approved') or {})}
    apply_approval(entry)
    if project and project not in entry['projects']: entry['projects'].append(project)
    st['repos'][slug] = entry
    save_state(st)
    return entry


def remember(path):
    """The discovered entry of a repository already on disk (a `repos` entry of the person without env_up): read and
    saved under its origin, like a clone. None when `path` is not a git repository."""
    path = os.path.expanduser(path)
    code, origin = run(['git', '-C', path, 'config', '--get', 'remote.origin.url'])
    if not os.path.isdir(path) or run(['git', '-C', path, 'rev-parse', '--git-dir'])[0] != 0: return None
    origin = origin.strip() if code == 0 else ''
    slug = origin_slug(origin) or tilde(path)
    url, kind = origin or None, 'github' if origin_slug(origin) else 'local'
    code, sha = run(['git', '-C', path, 'rev-parse', 'HEAD'])
    st = load_state()
    old = st['repos'].get(slug)
    if old and old.get('sha') == sha.strip() and current(old): return old
    entry = {'slug': slug, 'url': url, 'kind': kind, 'path': tilde(path), 'worktrees': WORKTREES, 'sha': sha.strip(),
             'rules_version': RULES_VERSION, 'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'found': discover(path),
             'projects': list((old or {}).get('projects') or []), 'approved': dict((old or {}).get('approved') or {})}
    apply_approval(entry)
    st['repos'][slug] = entry
    save_state(st)
    return entry


def as_repo(entry):
    """A `repos` entry (schema.py) from a discovered one, marked "discovered"."""
    f = entry.get('found') or {}
    r = {'path': entry['path'], 'name': os.path.basename(entry['path'].rstrip('/')), 'worktrees': entry.get('worktrees')
         or WORKTREES, 'discovered': True, 'slug': entry['slug'], 'discovered_at': entry.get('at')}
    for k in ('base', 'rules'):
        if f.get(k): r[k] = f[k]['value']
    return r


def discovered_repos():
    return [as_repo(e) for e in load_state()['repos'].values() if isinstance(e, dict) and e.get('path')]


def effective_repos(cfg):
    """The person's `repos` when there are any (they always win); else the discovered ones."""
    return own_repos(cfg) or discovered_repos()


def with_discovered(cfg):
    """A copy of the config whose `repos` are the effective ones (never written to disk)."""
    return {**cfg, 'repos': effective_repos(cfg)}


def entry_of(repo):
    """The discovered entry behind a `repos` entry (by slug, or by path), or None."""
    repos = load_state()['repos']
    if repo.get('slug') in repos: return repos[repo['slug']]
    want = os.path.realpath(os.path.expanduser(repo.get('path') or ''))
    return next((e for e in repos.values() if os.path.realpath(os.path.expanduser(e.get('path') or '')) == want), None)


def own_repos(cfg):
    return [r for r in cfg.get('repos') or [] if isinstance(r, dict) and r.get('path')]


def find(cfg, ref):
    """The `repos` entry for a pr_url / URL / owner/name, or None (nothing for it yet: the caller discovers, with the
    owner rule, or asks). The person's repository whose origin is that one wins (an ssh alias or mirror by the
    owner/name at the end of its URL); one whose origin is another repository, or cannot be told, never does. Then
    the discovered entry of that repository."""
    try: slug, _, _, kind = parse(ref)
    except Failed: return None
    for r in own_repos(cfg):
        code, out = run(['git', '-C', os.path.expanduser(r['path']), 'config', '--get', 'remote.origin.url'],
                        stdout_only=True)
        o = (origin_slug(out) or loose_slug(out)) if code == 0 else None
        if o and o.lower() == slug.lower(): return r
    for r in discovered_repos():
        if (r.get('slug') or '').lower() == slug.lower(): return r
    return None


def qa_options(cfg, repo=None):
    """(options of acceptance-testing, {key: source}): the person's options; without the person's env_up, the
    environment block discovered for `repo`; node_dir key by key. Sources: "config" or the discovered file."""
    o = dict(behavior_names.options(cfg, QA))
    if repo and repo.get('discovered') and own_repos(cfg):
        # a repository the person's config does not name: their environment commands are for their repositories
        for k in ENV_KEYS + ('node_dir',): o.pop(k, None)
    src = {k: 'config' for k in o}
    e = entry_of(repo) if repo else None
    found = (e or {}).get('found') or {}
    if not o.get('env_up') and found.get('env_up'):
        for k in ENV_KEYS:
            o.pop(k, None); src.pop(k, None)
            if found.get(k): o[k] = found[k]['value']; src[k] = found[k]['source']
        if found['env_up'].get('mode') == 'background': o['_background'] = True
    if not o.get('node_dir') and found.get('node_dir'):
        o['node_dir'] = found['node_dir']['value']; src['node_dir'] = found['node_dir']['source']
    return o, src


def approve(cfg, ref, spec):
    """Records the person's OK for the candidate script `spec` (<path>@<sha256 prefix, 7+>) of the repository `ref`:
    only when it is the candidate of the current discovery and the sha256 is the one the question showed."""
    rel, _, want = spec.partition('@')
    entry = resolve(cfg, ref)
    cand = (entry.get('found') or {}).get('env_script')
    if not cand or cand['value'] != rel:
        raise Failed(2, f'{rel} nao e o script candidato de {entry["slug"]}' + (f' (o candidato e {cand["value"]})' if cand else ''))
    if len(want) < 7 or not cand['sha256'].startswith(want.lower()):
        raise Failed(2, f'{rel} mudou desde a pergunta (sha256 agora {cand["sha256"][:12]}): pergunte de novo')
    entry.setdefault('approved', {})[rel] = {'sha256': cand['sha256'], 'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                                             'warnings': warnings_of(cand)}
    apply_approval(entry)
    st = load_state()
    st['repos'][entry['slug']] = entry
    save_state(st)
    return entry


def branch_check(entry, wt, used):
    """None when the PR branch checked out at `wt` gives the same discovered commands `used` ({key: value}) as the
    default branch they were read from; else the PERGUNTAR text. The branch is what qa_env runs, so an approved script
    changed by the PR, or a Makefile/package.json/compose the PR turns into something else, never runs unchecked."""
    if not used: return None
    tmp = apply_approval({'slug': entry.get('slug'), 'found': discover(wt, base='main'),
                          'approved': dict(entry.get('approved') or {})})
    got = {k: v['value'] for k, v in tmp['found'].items()}
    diff = sorted(k for k, v in used.items() if got.get(k) != v)
    if not diff: return None
    cand = tmp['found'].get('env_script') or tmp['found'].get('candidate')
    what = ''
    if cand and cand.get('sha256'):
        what = f' (na branch, {cand["value"]} tem sha256 {cand["sha256"][:12]}, diferente do aprovado)'
    elif cand:
        what = f' (na branch: {cand["source"]}: "{cand["value"]}")'
    return (f'A branch da PR muda o que sobe o ambiente de teste de {entry.get("slug")}: {", ".join(diff)} '
            f'nao e mais o que foi conferido na branch principal{what}. Nao rodo comando da branch sem conferir. '
            f'Para testar esta PR, a pessoa revisa a mudanca e define "env_up" (e "env_down") em '
            f'behavior_config.acceptance-testing do config.json da instancia, no computador; ou, depois do merge, a '
            f'descoberta le a versao nova e pergunta de novo.')


def question(entry):
    """The question for the person: approve the candidate script, or say how to bring the environment up."""
    slug = entry if isinstance(entry, str) else (entry or {}).get('slug') or '?'
    cand = ((entry or {}).get('found') or {}).get('env_script') if isinstance(entry, dict) else None
    if cand and cand.get('kind') == 'compose' and not ((entry.get('found') or {}).get('env_up')):
        warn = f' Atencao, ele pede acesso ao host: {", ".join(cand["notes"])}.' if cand.get('notes') else ''
        if cand.get('shared'):
            warn += (f' Dois testes ao mesmo tempo colidem ou misturam dados: {", ".join(cand["shared"])}.')
        where = f'porta {cand["port"]}' + (f' do servico {cand["service"]}' if cand.get('service') else '')
        return (f'Achei {cand["value"]} em {slug} (sha256 {cand["sha256"][:12]}, cobre {", ".join(cand["covers"])}), com '
                f'{where} publicada: posso subir o ambiente de teste com docker compose desse arquivo?{warn} '
                f'Com o OK da pessoa (nunca pela recomendada): python3 $S/scripts/repo_discovery.py <instancia> {slug} '
                f'--approve {cand["value"]}@{cand["sha256"][:12]}')
    if cand and not ((entry.get('found') or {}).get('env_up')):
        return (f'Achei {cand["value"]} em {slug} (sha256 {cand["sha256"][:12]}), com up e down: posso usar esse '
                f'script para subir e derrubar o ambiente de teste? Ele roda shell livre na maquina. Com o OK da '
                f'pessoa (nunca pela recomendada): python3 $S/scripts/repo_discovery.py <instancia> {slug} --approve '
                f'{cand["value"]}@{cand["sha256"][:12]}')
    refused = ((entry or {}).get('found') or {}).get('refused') if isinstance(entry, dict) else None
    if refused:
        return (f'Nao li {slug} para achar o ambiente de teste: {refused["value"]}. Nao rodo nada dele. Defina "env_up", '
                f'"env_url" e "env_down" em behavior_config.acceptance-testing do config.json da instancia, no '
                f'computador (comandos nao se editam pelo Planou).')
    other = ((entry or {}).get('found') or {}).get('candidate') if isinstance(entry, dict) else None
    seen = (f'Achei {other["source"]}: "{other["value"]}", fora da lista de servidores de desenvolvimento que subo '
            f'sozinho (vite, next dev e outros simples, sem encadeamento, so em loopback). ') if other else ''
    return (seen + f'Nao achei como subir o ambiente de teste de {slug}: procurei scripts/*env*.sh com up e down, docker '
            f'compose na raiz com porta publicada (os dois com o OK da pessoa), alvo de Makefile (up, dev, start) e script dev ou start do '
            f'package.json. Qual comando sobe o ambiente, qual imprime a URL e qual derruba? Defina "env_up", '
            f'"env_url" e "env_down" em behavior_config.acceptance-testing do config.json da instancia, no computador '
            f'(comandos nao se editam pelo Planou).')


def describe(entry):
    lines = [f'REPOSITORIO DESCOBERTO: {entry["slug"]} em {entry["path"]} ({(entry.get("sha") or "")[:7]}, '
             f'{entry.get("at")}; estado em {tilde(state_path())})']
    for k, v in (entry.get('found') or {}).items():
        extra = f'  [candidato: precisa do OK da pessoa, sha256 {v["sha256"][:12]}]' if k == 'env_script' else ''
        lines.append(f'  {k}: {v["value"]}  <- {v["source"]}' + ('  [em segundo plano]' if v.get('mode') else '') + extra)
    if not (entry.get('found') or {}).get('env_up'):
        lines.append('PERGUNTAR: ' + question(entry))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description='descobre e clona o repositorio e o ambiente de teste de uma instancia')
    ap.add_argument('instance')
    ap.add_argument('ref', help='link da PR, URL do repositorio, dono/nome, ou "show"')
    ap.add_argument('--project')
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--approve', metavar='SCRIPT@SHA', help='registra o OK da pessoa para o script candidato')
    a = ap.parse_args(argv)
    paths.instance(a.instance)
    if a.ref == 'show':
        repos = load_state()['repos']
        if not repos: print('nada descoberto ainda'); return 0
        for e in repos.values(): print('\n'.join(describe(e)))
        return 0
    try:
        import schema
        try: cfg = schema.load(paths.CONFIG)
        except schema.ConfigError: cfg = {}
        entry = approve(cfg, a.ref, a.approve) if a.approve else resolve(cfg, a.ref, a.project, a.refresh)
    except Failed as e:
        print(f'DESCOBERTA: {e}', file=sys.stderr)
        return e.code
    print('\n'.join(describe(entry)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
