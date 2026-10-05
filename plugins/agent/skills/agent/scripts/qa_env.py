#!/usr/bin/env python3
"""Throwaway test environment of the acceptance-testing behavior (old name qa; PLN0116): a detached worktree of the delivery's branch, the
instance's `setup` and `env_up` commands run in it and the app URL read by the `env_url` command (PLN0197; plan B: the
first http(s) URL in the output of `setup`/`env_up`, or the fixed `url`). It never commits, pushes or edits the branch:
the worktree is the QA's own and goes away with `down`.

  python3 qa_env.py <instance> up <PID> --branch BRANCH [--repo PATH | --pr-url URL]
  python3 qa_env.py <instance> down <PID>
  python3 qa_env.py <instance> status

`up` prints:
  QA AMBIENTE <PID>: <url> (<branch> @ <sha7>, worktree <path>)
  RODAR: QA_URL=<url> QA_WIDTHS=1360,834,390 QA_MIN_TARGET=44 QA_AXE_TAGS=... QA_SERVED=... QA_VIDEO_WIDTH=1360 QA_KIT=<qa_kit.cjs>
         NODE_PATH=<worktree>/<node_dir> node <script>
Exit 0 = up; 1 = a command failed, or `env_url` exited non-zero or printed nothing (the environment did not come up)
(its last lines on stderr, secrets masked, the environment brought down); 2 = bad
call or config (no repository and no --pr-url, clone failed, unknown branch); 3 = the URL is the served app or not
http(s) (refused and brought down); 4 = no env_up in the config and nothing found in the repository to bring an
environment up, a repository script waiting for the person's OK (--approve), an untrusted owner, or a PR branch that
changes a discovered command (the PERGUNTAR line on stderr is the question for the person). The options are in
behavior_config.acceptance-testing (its BEHAVIOR.md; the old key qa still counts). Each run is kept in cache/qa/<PID>.json of
the instance, so `down` finds it after a restarted session.

PLN0345: without `repos` (an agent created on the Time screen with only its role), `--pr-url` names the repository:
repo_discovery.py clones it in ~/src/<name> (or reuses that clone) and reads how to bring the environment up. The
person's config wins (`repos`, and the environment commands as a block when `env_up` is set); then what was discovered
(data/discovered.json of the instance). A discovered Makefile or package.json command is a dev server that does not
exit: it runs in the background (its own process group, output in .qa-env.log of the worktree, PORT set to a free
port), the URL is the first http(s) URL of that log, and `down` stops the group.
"""
import argparse, json, os, re, shlex, signal, socket, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths, repo_discovery, schema   # noqa: E402
from review_check import SECRETS   # noqa: E402
from watch_core import fileio   # noqa: E402

KIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qa_kit.cjs')
DEFAULTS = {'widths': [1360, 834, 390], 'min_target_px': 44, 'axe_tags': ['wcag2a', 'wcag2aa', 'wcag21aa'],
            'up_timeout_s': 600, 'served_urls': []}
ENV_URL_TIMEOUT_S = 60
PID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,39}')
BRANCH_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}')
URL_RE = re.compile(r'https?://[^\s\'"<>)]+')


class Refused(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def masked(text):
    for _, rx in SECRETS: text = rx.sub('***', text)
    return text


def options(cfg, repo=None):
    """The options: the defaults, the person's (behavior_config.acceptance-testing, old key qa) and, without the
    person's env_up, what was discovered for `repo` (repo_discovery.qa_options). `_sources` says where each came from."""
    o = dict(DEFAULTS)
    own, src = repo_discovery.qa_options(cfg, repo)
    o.update(own)
    o['_sources'] = src
    return o


def repo_entry(cfg, wanted=None, pr_url=None):
    """The `repos` entry to test: the person's (`repos`) first, then the discovered ones. `pr_url` (or a repository URL)
    picks by repository and clones it when nothing has it yet; `wanted` by path; without either, the only one."""
    def fresh(ref):     # owner checked, fetched, discovery read again when origin's default branch moved
        try: return repo_discovery.as_repo(repo_discovery.resolve(cfg, ref))
        except repo_discovery.Failed as e: raise Refused(e.code, str(e))
    if pr_url:
        r = repo_discovery.find(cfg, pr_url)
        return fresh(pr_url) if r is None or r.get('discovered') else r
    repos = repo_discovery.effective_repos(cfg)
    if wanted:
        w = os.path.realpath(os.path.expanduser(wanted))
        repos = [r for r in repos if os.path.realpath(os.path.expanduser(r['path'])) == w]
    if len(repos) != 1:
        raise Refused(2, 'repositorio: ' + ('nenhum em "repos" do config nem descoberto: passe --pr-url <link da PR> '
                                            '(o repositorio e clonado em ~/src e o ambiente descoberto)' if not repos
                                            else 'mais de um em "repos"; use --repo ou --pr-url'))
    return fresh(repos[0]['slug']) if repos[0].get('discovered') else repos[0]


def repo_of(cfg, wanted=None, repo=None):
    """(repository path, worktrees folder) of the entry (or of repo_entry(cfg, wanted))."""
    r = repo or repo_entry(cfg, wanted)
    if not r.get('worktrees'): raise Refused(2, f'"repos" {r["path"]}: falta "worktrees" (onde a worktree do QA fica)')
    return os.path.expanduser(r['path']), os.path.expanduser(r['worktrees'])


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def background(cmd, wt, timeout, env, st):
    """A discovered dev server (Makefile or package.json): started in its own process group with its output in
    <worktree>/.qa-env.log; the URL is the first http(s) URL of the log. (url, log text); url None = it exited or
    timed out without one."""
    log = os.path.join(wt, '.qa-env.log')
    with open(log, 'wb') as out:
        p = subprocess.Popen(cmd, shell=True, cwd=wt, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             env=env, start_new_session=True)
    st['pgid'] = p.pid
    save(st)
    end = time.time() + timeout
    text = ''
    while time.time() < end:
        try:
            with open(log, encoding='utf-8', errors='replace') as f: text = ANSI.sub('', f.read())
        except OSError: text = ''
        m = URL_RE.search(text)
        if m: return re.sub(r'//(0\.0\.0\.0|\[::\])', '//127.0.0.1', m.group(0).rstrip('.,;')), text
        if p.poll() is not None: return None, text + f'\n(saiu {p.returncode} sem imprimir URL)'
        time.sleep(0.5)
    return None, text + f'\n(passou de {timeout} s sem imprimir URL)'


def stop_group(pgid):
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 0)):
        try: os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError): return
        for _ in range(int(wait * 10)):
            try: os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError): return
            time.sleep(0.1)


def origin_of(o, key):
    """' (comando descoberto em <source>)' when the value came from the repository, not from the config."""
    s = (o.get('_sources') or {}).get(key)
    return f' (comando descoberto em {s}, nao no config; se nao serve, a pessoa define no config.json)' if s and s != 'config' else ''


def state_file(pid):
    return os.path.join(paths.CACHE_DIR, 'qa', f'{pid}.json')


def git(repo, *args, timeout=300):
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0: raise Refused(2, f'git {args[0]}: {masked(r.stderr.strip())[-400:]}')
    return r.stdout.strip()


def shell(cmd, cwd, timeout, env):
    """A command of the instance's config, run by the shell in the worktree (the person wrote it, like a Makefile)."""
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors='replace') if isinstance(e.stdout, bytes) else (e.stdout or '')
        return 124, out + f'\n(passou de {timeout} s)'


def served(url, patterns):
    return any(re.search(p, url, re.I) for p in patterns)


def video_width(o):
    """The one width whose run qa_kit records (PLN0279): `video_width`, else 1360 when tested, else the largest; 0 = none."""
    w = o.get('video_width')
    if w is None: w = 1360 if 1360 in o['widths'] else max(o['widths'])
    return w if w in o['widths'] else 0


def run_line(url, o, wt):
    env = [f'QA_URL={url}', f'QA_WIDTHS={",".join(map(str, o["widths"]))}', f'QA_MIN_TARGET={o["min_target_px"]}',
           f'QA_AXE_TAGS={",".join(o["axe_tags"])}', f'QA_SERVED={json.dumps(o["served_urls"])}', f'QA_VIDEO_WIDTH={video_width(o)}', f'QA_KIT={KIT}']
    if o.get('node_dir'): env.append(f'NODE_PATH={os.path.join(wt, o["node_dir"])}')
    return ' '.join(shlex.quote(e) for e in env) + ' node <script>'


def up(cfg, pid, branch, repo_path=None, pr_url=None):
    prev = load(pid)
    if prev and prev.get('pgid'):          # a retest: the server of the previous up goes first, never left orphaned
        stop_group(prev['pgid'])
        prev.pop('pgid', None); save(prev)
    entry = repo_entry(cfg, repo_path, pr_url)
    o = options(cfg, entry)
    if not o.get('env_up') and not entry.get('discovered') and repo_discovery.entry_of(entry) is None:
        repo_discovery.remember(entry['path'])          # the person's repo without env_up: read it once
        o = options(cfg, entry)
    if not o.get('env_up'):
        e = repo_discovery.entry_of(entry)
        raise Refused(4, f'"behavior_config.{schema.QA}.env_up" vazio e nada descoberto no repositorio.\n'
                         'PERGUNTAR: ' + repo_discovery.question(e or entry['path']))
    repo, base = repo_of(cfg, repo=entry)
    wt = os.path.join(base, f'qa-{pid.lower()}')
    git(repo, 'fetch', '-q', 'origin', branch)
    ref = f'origin/{branch}'
    sha = git(repo, 'rev-parse', '--verify', '-q', f'{ref}^{{commit}}')
    if os.path.isdir(wt):   # a retest after an adjustment: the same worktree on the new tip
        git(wt, 'checkout', '-q', '--detach', sha)
    else:
        git(repo, 'worktree', 'add', '-q', '--detach', wt, sha)
    st = {'pid': pid, 'branch': branch, 'sha': sha, 'repo': repo, 'worktree': wt, 'url': None,
          'discovered': bool(entry.get('discovered')), 'slug': entry.get('slug'),
          'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    save(st)
    e = repo_discovery.entry_of(entry)
    src = o.get('_sources') or {}
    used = {k: o[k] for k in repo_discovery.ENV_KEYS if o.get(k) and src.get(k) not in (None, 'config')}   # never the person's
    drift = repo_discovery.branch_check(e or entry, wt, used) if e else None
    if drift:          # the branch is what runs: a discovered command it changed is never run unchecked
        down(cfg, pid, quiet=True)
        raise Refused(4, 'PERGUNTAR: ' + drift)
    env = {**os.environ, 'QA_PID': pid, 'QA_BRANCH': branch, 'QA_PROJECT': f'qa-{pid.lower()}'}
    if o.get('_background'): env.update(PORT=str(free_port()), PYTHONUNBUFFERED='1', HOST='127.0.0.1')
    output, url = '', None
    for key in ('setup', 'env_up'):
        if not o.get(key): continue
        if key == 'env_up' and o.get('_background'):
            url, out = background(o[key], wt, o['up_timeout_s'], env, st)
            output += out
            if not url:
                tail = '\n'.join(masked(out).strip().splitlines()[-20:])
                down(cfg, pid, quiet=True)
                raise Refused(1, f'"env_up" nao imprimiu URL{origin_of(o, key)}; ambiente derrubado. Ultimas linhas:\n{tail}')
            continue
        code, out = shell(o[key], wt, o['up_timeout_s'], env)
        output += out
        if code != 0:
            tail = '\n'.join(masked(out).strip().splitlines()[-20:])
            down(cfg, pid, quiet=True)
            raise Refused(1, f'"{key}" falhou (saida {code}){origin_of(o, key)}; ambiente derrubado. Ultimas linhas:\n{tail}')
    if url:
        where = '"env_up" (em segundo plano)'
    elif o.get('env_url'):   # the environment says where it is (e.g. `scripts/e2e-env.sh url`)
        code, out = shell(o['env_url'], wt, min(ENV_URL_TIMEOUT_S, o['up_timeout_s']), env)
        url = next((ln.strip() for ln in out.splitlines() if ln.strip()), '')
        if code != 0 or not url:
            tail = '\n'.join(masked(out).strip().splitlines()[-20:])
            down(cfg, pid, quiet=True)
            why = f'saiu {code}' if code != 0 else 'nao imprimiu nada'
            raise Refused(1, f'"env_url" {why}: o ambiente nao subiu; ambiente derrubado.' + (f' Ultimas linhas:\n{tail}' if tail else ''))
        where = '"env_url"'
    else:   # plan B: the fixed url, or the first URL the setup/env_up printed
        url = o.get('url') or next(iter(URL_RE.findall(output)), None)
        where = '"env_up" (defina "env_url" ou "url")'
    if not url or not re.match(r'https?://', url):
        down(cfg, pid, quiet=True)
        raise Refused(3, f'nenhuma URL http(s) na saida do {where}; ambiente derrubado')
    if served(url, o['served_urls']):
        down(cfg, pid, quiet=True)
        raise Refused(3, f'{url} e o app servido ("served_urls"): recusado, ambiente derrubado')
    st['url'] = url
    save(st)
    print(f'QA AMBIENTE {pid}: {url} ({branch} @ {sha[:7]}, worktree {wt})')
    print('RODAR: ' + run_line(url, o, wt))
    return st


def save(st):
    p = state_file(st['pid'])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fileio.write_json(p, st, indent=2)


def load(pid):
    try:
        with open(state_file(pid), encoding='utf-8') as f: return json.load(f)
    except (OSError, ValueError):
        return None


def down(cfg, pid, quiet=False):
    """The background group stopped, env_down in the worktree, then the worktree removed. Without a saved run, the
    worktree of the only repository (config, else discovered)."""
    st = load(pid)
    if st:
        repo, wt = st['repo'], st['worktree']
        # the same options as the up: a discovered repository runs its discovered env_down, whatever the config says
        entry = {'path': repo, 'discovered': st.get('discovered'), 'slug': st.get('slug')}
    else:
        entry = repo_entry(cfg)
        repo, base = repo_of(cfg, repo=entry)
        wt = os.path.join(base, f'qa-{pid.lower()}')
    o = options(cfg, entry)
    note = ''
    if st and st.get('pgid'): stop_group(st['pgid'])
    if os.path.isdir(wt):
        if o.get('env_down'):
            code, out = shell(o['env_down'], wt, o['up_timeout_s'], {**os.environ, 'QA_PID': pid,
                                                                     'QA_PROJECT': f'qa-{pid.lower()}'})
            if code != 0: note = f' ("env_down" saiu {code}: {masked(out).strip()[-200:]})'
        r = subprocess.run(['git', '-C', repo, 'worktree', 'remove', '--force', wt], capture_output=True, text=True)
        if r.returncode != 0: note += f' (worktree ficou: {r.stderr.strip()[-200:]})'
    subprocess.run(['git', '-C', repo, 'worktree', 'prune'], capture_output=True)
    try: os.remove(state_file(pid))
    except OSError: pass
    if not quiet: print(f'QA AMBIENTE REMOVIDO {pid}{note}')


def status():
    d = os.path.join(paths.CACHE_DIR, 'qa')
    runs = sorted(f[:-5] for f in (os.listdir(d) if os.path.isdir(d) else []) if f.endswith('.json'))
    if not runs: print('nenhum ambiente de QA'); return
    for pid in runs:
        st = load(pid) or {}
        alive = 'worktree presente' if os.path.isdir(st.get('worktree') or '') else 'SEM worktree'
        print(f'{pid}: {st.get("url") or "(sem URL)"} ({st.get("branch")} @ {(st.get("sha") or "")[:7]}, {alive}, desde {st.get("at")})')


def main(argv=None):
    ap = argparse.ArgumentParser(description='ambiente de teste do comportamento acceptance-testing')
    ap.add_argument('instance')
    ap.add_argument('action', choices=('up', 'down', 'status'))
    ap.add_argument('pid', nargs='?')
    ap.add_argument('--branch')
    ap.add_argument('--repo')
    ap.add_argument('--pr-url')
    a = ap.parse_args(argv)
    paths.instance(a.instance)
    try:
        if a.action == 'status': status(); return 0
        if not a.pid or not PID_RE.fullmatch(a.pid): raise Refused(2, f'PID invalido: {a.pid!r}')
        try: cfg = schema.load(paths.CONFIG)
        except schema.ConfigError as e: raise Refused(2, str(e))
        if a.action == 'down': down(cfg, a.pid); return 0
        if not a.branch or not BRANCH_RE.fullmatch(a.branch) or '..' in a.branch:
            raise Refused(2, f'--branch invalida: {a.branch!r}')
        up(cfg, a.pid, a.branch, a.repo, a.pr_url)
        return 0
    except Refused as e:
        print(f'QA AMBIENTE {a.pid or ""}: {e}', file=sys.stderr)
        return e.code
    except subprocess.TimeoutExpired as e:
        print(f'QA AMBIENTE {a.pid or ""}: git passou do tempo ({e.timeout} s)', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
