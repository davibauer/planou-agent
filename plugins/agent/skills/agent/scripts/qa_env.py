#!/usr/bin/env python3
"""Throwaway test environment of the qa behavior (PLN0116): a detached worktree of the delivery's branch, the
instance's `setup` and `env_up` commands run in it and the app URL read by the `env_url` command (PLN0197; plan B: the
first http(s) URL in the output of `setup`/`env_up`, or the fixed `url`). It never commits, pushes or edits the branch:
the worktree is the QA's own and goes away with `down`.

  python3 qa_env.py <instance> up <PID> --branch BRANCH [--repo PATH]
  python3 qa_env.py <instance> down <PID>
  python3 qa_env.py <instance> status

`up` prints:
  QA AMBIENTE <PID>: <url> (<branch> @ <sha7>, worktree <path>)
  RODAR: QA_URL=<url> QA_WIDTHS=1360,834,390 QA_MIN_TARGET=44 QA_AXE_TAGS=... QA_SERVED=... QA_VIDEO_WIDTH=1360 QA_KIT=<qa_kit.cjs>
         NODE_PATH=<worktree>/<node_dir> node <script>
Exit 0 = up; 1 = a command failed, or `env_url` exited non-zero or printed nothing (the environment did not come up)
(its last lines on stderr, secrets masked, the environment brought down); 2 = bad
call or config (no repository, no env_up, unknown branch); 3 = the URL is the served app or not http(s) (refused and
brought down). The options are in behavior_config.qa (BEHAVIOR.md of qa). Each run is kept in cache/qa/<PID>.json of
the instance, so `down` finds it after a restarted session.
"""
import argparse, json, os, re, shlex, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths, schema   # noqa: E402
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


def options(cfg):
    o = dict(DEFAULTS)
    o.update((cfg.get('behavior_config') or {}).get('qa') or {})
    return o


def repo_of(cfg, wanted=None):
    """The repository entry of the config: the only one, or the one whose path is `wanted`."""
    repos = [r for r in cfg.get('repos') or [] if isinstance(r, dict) and r.get('path')]
    if wanted:
        w = os.path.realpath(os.path.expanduser(wanted))
        repos = [r for r in repos if os.path.realpath(os.path.expanduser(r['path'])) == w]
    if len(repos) != 1:
        raise Refused(2, 'repositorio: ' + ('nenhum em "repos" do config' if not repos else 'mais de um em "repos"; use --repo'))
    r = repos[0]
    if not r.get('worktrees'): raise Refused(2, f'"repos" {r["path"]}: falta "worktrees" (onde a worktree do QA fica)')
    return os.path.expanduser(r['path']), os.path.expanduser(r['worktrees'])


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


def up(cfg, pid, branch, repo_path=None):
    o = options(cfg)
    if not o.get('env_up'): raise Refused(2, '"behavior_config.qa.env_up" vazio: nada para subir')
    repo, base = repo_of(cfg, repo_path)
    wt = os.path.join(base, f'qa-{pid.lower()}')
    git(repo, 'fetch', '-q', 'origin', branch)
    ref = f'origin/{branch}'
    sha = git(repo, 'rev-parse', '--verify', '-q', f'{ref}^{{commit}}')
    if os.path.isdir(wt):   # a retest after an adjustment: the same worktree on the new tip
        git(wt, 'checkout', '-q', '--detach', sha)
    else:
        git(repo, 'worktree', 'add', '-q', '--detach', wt, sha)
    st = {'pid': pid, 'branch': branch, 'sha': sha, 'repo': repo, 'worktree': wt, 'url': None,
          'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    save(st)
    env = {**os.environ, 'QA_PID': pid, 'QA_BRANCH': branch}
    output = ''
    for key in ('setup', 'env_up'):
        if not o.get(key): continue
        code, out = shell(o[key], wt, o['up_timeout_s'], env)
        output += out
        if code != 0:
            tail = '\n'.join(masked(out).strip().splitlines()[-20:])
            down(cfg, pid, quiet=True)
            raise Refused(1, f'"{key}" falhou (saida {code}); ambiente derrubado. Ultimas linhas:\n{tail}')
    if o.get('env_url'):   # the environment says where it is (e.g. `scripts/e2e-env.sh url`)
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
    """env_down in the worktree, then the worktree removed. Without a saved run, the worktree the config would use."""
    st = load(pid)
    if st: repo, wt = st['repo'], st['worktree']
    else:
        repo, base = repo_of(cfg)
        wt = os.path.join(base, f'qa-{pid.lower()}')
    o = options(cfg)
    note = ''
    if os.path.isdir(wt):
        if o.get('env_down'):
            code, out = shell(o['env_down'], wt, o['up_timeout_s'], {**os.environ, 'QA_PID': pid})
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
    ap = argparse.ArgumentParser(description='ambiente de teste do comportamento qa')
    ap.add_argument('instance')
    ap.add_argument('action', choices=('up', 'down', 'status'))
    ap.add_argument('pid', nargs='?')
    ap.add_argument('--branch')
    ap.add_argument('--repo')
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
        up(cfg, a.pid, a.branch, a.repo)
        return 0
    except Refused as e:
        print(f'QA AMBIENTE {a.pid or ""}: {e}', file=sys.stderr)
        return e.code
    except subprocess.TimeoutExpired as e:
        print(f'QA AMBIENTE {a.pid or ""}: git passou do tempo ({e.timeout} s)', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
