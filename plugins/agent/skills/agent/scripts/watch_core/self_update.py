"""Whether the plugin copy in use updates by itself through the local provisioner (PLN0352, PLN0353).

One rule, read by two places: the provisioner decides with it whether to update the installer's copy (auto_update),
and the runner sends its answer to Planou in the heartbeat (plugin_self_update), so the ficha says "atualiza sozinho em
ate 30 min" or why not, never something the provisioner would not do.

The copy updates by itself when auto_update is not false in the provisioner's config.json and it is the installer's
archive copy (`.planou-install` at its root, no .git) or the installer's git clone that is fit for a fast-forward:
the installer's folder (PLANOU_INSTALL_DIR, default ~/.local/share/planou/claude-plugins), a repository of its own,
origin at davibauer/planou-agent, on main, no change in a tracked file, no commit origin lacks, and no untracked file
where origin's main (as last fetched) has a tracked one (git merge would refuse to overwrite it). Other untracked
files (a __pycache__, an instance's own folder) do not count. Local reads only, never the network.

Reasons are codes (the /v1 enum of plugin_self_update); the provisioner turns them into its log lines.
"""
import json, os, re, shutil, subprocess

GIT_REPO = 'https://github.com/davibauer/planou-agent.git'   # the only origin the installer's clone may follow
GIT_BRANCH = 'main'
GIT_TIMEOUT_S = 120
INSTALL_MARK = '.planou-install'   # install.sh's marker at the root of its archive copy
AUTO = 'auto'
# Why not, in the order they are checked. no_provisioner is only the heartbeat's: the provisioner is the one running.
REASONS = ('no_provisioner', 'disabled', 'development', 'no_git', 'not_own_repo', 'other_remote', 'other_branch',
           'local_changes', 'local_commits', 'untracked_conflict')


class GitError(Exception):
    """git missing or too slow: the caller decides (the provisioner logs it, the heartbeat omits the field)."""


def provisioner_dir(*p):
    return os.path.join(os.path.expanduser('~'), '.config', 'agent-provisioner', *p)


def has_provisioner():
    """A provisioner is set up on this computer: its credential file exists (never read here)."""
    return os.path.isfile(provisioner_dir('secrets', 'planou.env'))


def enabled():
    """auto_update from the provisioner's config.json: only an explicit false turns it off (an unreadable file does
    not: the provisioner refuses to start with one, and its default is on)."""
    try:
        with open(provisioner_dir('config.json')) as f: data = json.load(f)
    except (OSError, ValueError):
        return True
    return not (isinstance(data, dict) and data.get('auto_update') is False)


def installer_root():
    """The installer's folder holding the agent plugin (realpath), or None."""
    from . import rollout as ro
    inst = ro.installed_dir('agent')
    return os.path.realpath(os.path.dirname(os.path.dirname(inst))) if inst else None


def copy_root(live):
    """The folder two levels above the plugin folder `live` (<root>/plugins/<plugin>)."""
    return os.path.dirname(os.path.dirname(os.path.realpath(live)))


def archive_root(live):
    """The installer's archive copy holding `live` (<root>/.planou-install and no .git), or None."""
    if not live: return None
    root = copy_root(live)
    if os.path.isfile(os.path.join(root, INSTALL_MARK)) and not os.path.exists(os.path.join(root, '.git')): return root
    return None


def clone_root(live):
    """The git checkout holding `live` (the folder two levels above it, with a .git), or None."""
    if not live: return None
    root = copy_root(live)
    return root if os.path.exists(os.path.join(root, '.git')) else None


def repo_key(url):
    """A comparable form of a git remote: host/owner/repo (lower case, no scheme, user, .git or trailing /) for an URL
    or scp-like address, the real path for a local folder."""
    u = str(url or '').strip().rstrip('/')
    if u.endswith('.git'): u = u[:-4]
    m = re.fullmatch(r'(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)[:/]+(.+)', u, re.I)
    if m and not u.startswith(('/', '.', '~', 'file:')): return (m.group(1) + '/' + m.group(2).strip('/')).lower()
    if u.startswith('file://'): u = u[7:]
    return os.path.realpath(os.path.expanduser(u)) if u else ''


def git(root, *args, timeout=GIT_TIMEOUT_S, strip=True):
    """`git -C root args` with no prompt, no optional lock (a read never holds index.lock while the provisioner
    merges) and never walking above root to another repository. Returns (code, output); a missing git or a timeout
    raises GitError."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CEILING_DIRECTORIES=os.path.dirname(root), LC_ALL='C',
               GIT_OPTIONAL_LOCKS='0')
    env.setdefault('GIT_SSH_COMMAND', 'ssh -oBatchMode=yes')
    try:
        r = subprocess.run(['git', '-C', root, *args], capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise GitError('git nao encontrado')
    except subprocess.TimeoutExpired:
        raise GitError(f'git {args[0]} passou de {timeout} s')
    out = (r.stdout or '') if not strip else (r.stdout or '').strip()
    return r.returncode, out or (r.stderr or '').strip()


def kind(live, auto_update=None):
    """The cheap part of the rule (no git): 'disabled', 'development' (no installer copy holds `live`), or which
    installer copy it is, 'archive' or 'clone'. `auto_update` None reads the provisioner's config.json."""
    if (enabled() if auto_update is None else auto_update is not False) is False: return 'disabled'
    if archive_root(live): return 'archive'
    if clone_root(live): return 'clone'
    return 'development'


def git_skip(root, repo=None, timeout=GIT_TIMEOUT_S):
    """Why the git clone `root` may not be fast-forwarded to the release tag, as a code of REASONS, or None when it
    may. Local reads only."""
    inst = installer_root()
    if not inst or inst != os.path.realpath(root): return 'development'
    if shutil.which('git') is None: return 'no_git'
    code, top = git(root, 'rev-parse', '--show-toplevel', timeout=timeout)
    if code != 0 or os.path.realpath(top) != os.path.realpath(root): return 'not_own_repo'
    code, url = git(root, 'remote', 'get-url', 'origin', timeout=timeout)
    if code != 0 or repo_key(url) != repo_key(repo or GIT_REPO): return 'other_remote'
    code, branch = git(root, 'symbolic-ref', '-q', '--short', 'HEAD', timeout=timeout)
    if code != 0 or branch != GIT_BRANCH: return 'other_branch'
    code, out = git(root, 'status', '--porcelain', '--untracked-files=no', timeout=timeout)
    if code != 0 or out: return 'local_changes'
    code, out = git(root, 'rev-list', '--max-count=1', 'HEAD', '--not', '--remotes=origin', timeout=timeout)
    if code != 0 or out: return 'local_commits'
    code, out = git(root, 'ls-files', '-z', '--others', '--exclude-standard', timeout=timeout, strip=False)
    untracked = {p for p in out.split('\0') if p} if code == 0 else set()
    if untracked:
        code, tree = git(root, 'ls-tree', '-r', '-z', '--name-only', f'refs/remotes/origin/{GIT_BRANCH}', timeout=timeout,
                         strip=False)
        if code == 0 and untracked & {p for p in tree.split('\0') if p}: return 'untracked_conflict'
    return None


def status(live, auto_update=None, repo=None, timeout=GIT_TIMEOUT_S):
    """'auto' when the provisioner updates the copy `live` by itself, else the reason (a code of REASONS). Raises
    GitError when git does not answer."""
    k = kind(live, auto_update)
    if k in ('disabled', 'development'): return k
    if k == 'clone': return git_skip(clone_root(live), repo, timeout) or AUTO
    return AUTO


def report(live, timeout=10):
    """What the heartbeat sends as plugin_self_update for the copy `live`: no_provisioner first (nothing updates
    without one), then status(). None when it cannot tell (git too slow)."""
    if not live: return None
    if not has_provisioner(): return 'no_provisioner'
    try:
        return status(live, timeout=timeout)
    except GitError:
        return None
