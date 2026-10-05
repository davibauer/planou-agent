"""The column roles of a Planou project (PLN0368): dev, code-review, qa and release.

A column of a project may be owned by a role (Planou PLN0284): the task that enters it goes to an agent that declares
the role in the heartbeat (`roles`). With "planou.all_roles": true in its config, an instance plays every column role
this computer can run, without each behavior in "behaviors":
- the heartbeat declares the EXPLICIT list (never [] = all: [] stays "none", what job-scout, travel-agent and
  work-watch send), minus the roles whose tool is missing here (gaps()), which go in `roles_unavailable`
  [{role, reason, fix}] (watch_core.planou.roles_unavailable);
- `--load` keeps delegate-to-worker read every session and lists code-review, acceptance-testing and batch-release as
  "sob demanda": the session reads the BEHAVIOR.md only when a task of that column comes;
- `--brief <repo> --role <role>` takes the role a column calls for even when its behavior is not in "behaviors".

What a role needs on this computer (gaps):
- qa: a Playwright browser (chromium) in the browsers folder ($PLAYWRIGHT_BROWSERS_PATH, else the platform's cache:
  ~/.cache/ms-playwright, ~/Library/Caches/ms-playwright or %LOCALAPPDATA%\\ms-playwright). Files only, no
  subprocess: roles() runs on every heartbeat;
- release: the batch release configured (merge, tag and deploy): "behavior_config.batch-release.repo" is the path of a
  "repos" entry with "release": "batch". Release grants merge, tag and deploy, so it is never declared by default.

Standard library only: schema.py, dev_brief.py, agent.py and watch_core.planou all read it.
"""
import glob
import os
import sys

# role (as Planou keeps a column's owner_role) -> the plugin behavior that does that column's part, in catalog order
CATALOG = (('dev', 'delegate-to-worker'), ('code-review', 'code-review'), ('qa', 'acceptance-testing'),
           ('release', 'batch-release'))
BEHAVIOR_OF = dict(CATALOG)
ROLE_OF = {b: r for r, b in CATALOG}
TITLES = {'dev': 'Dev', 'code-review': 'Revisao de codigo', 'qa': 'QA', 'release': 'Release'}
# the behaviors read only when a task of their column comes (decision 6: the dev one stays read every session)
ON_DEMAND = ('code-review', 'acceptance-testing', 'batch-release')
QA_FIX = 'rode `npx playwright install chromium` neste computador'
# the bare command Planou shows as "Rode <fix> e ele volta sozinho" (roles_unavailable); release has none
FIX_CMD = {'qa': 'npx playwright install chromium'}
RELEASE_FIX = ('configure o release em lote: "behavior_config.batch-release.repo" com o caminho de um repositorio de '
               '"repos" que tenha "release": "batch" (comportamento batch-release)')


def all_roles(cfg):
    """True when the instance's config turns on "planou.all_roles"."""
    p = cfg.get('planou') if isinstance(cfg, dict) else None
    return isinstance(p, dict) and p.get('all_roles') is True


def behavior_of(role):
    """The behavior of a column role ('qa' -> 'acceptance-testing'), or None for a role outside the catalog."""
    return BEHAVIOR_OF.get(role)


def browsers_dirs(env=None):
    """Where Playwright keeps its browsers on this computer (the first that applies), [] when it cannot tell."""
    env = os.environ if env is None else env
    own = env.get('PLAYWRIGHT_BROWSERS_PATH')
    if own == '0': return []                       # inside node_modules of each project: nothing to look at here
    if own: return [os.path.expanduser(own)]
    home = os.path.expanduser('~')
    if sys.platform == 'darwin': return [os.path.join(home, 'Library', 'Caches', 'ms-playwright')]
    if os.name == 'nt': return [os.path.join(env.get('LOCALAPPDATA') or os.path.join(home, 'AppData', 'Local'), 'ms-playwright')]
    return [os.path.join(env.get('XDG_CACHE_HOME') or os.path.join(home, '.cache'), 'ms-playwright')]


def browser_ok(env=None):
    """True when a Playwright chromium is installed (or the browsers live inside each project: unknown counts as ok)."""
    env = os.environ if env is None else env
    if env.get('PLAYWRIGHT_BROWSERS_PATH') == '0': return True
    return any(glob.glob(os.path.join(d, 'chromium*')) for d in browsers_dirs(env))


def _norm(path):
    return os.path.normpath(os.path.expanduser(path)) if isinstance(path, str) and path.strip() else None


def release_ok(cfg):
    """True when the batch release is configured: batch-release.repo is the path of a repos entry with release batch."""
    bc = cfg.get('behavior_config') if isinstance(cfg.get('behavior_config'), dict) else {}
    br = bc.get('batch-release') or bc.get('deploy-notice') or {}
    repo = _norm(br.get('repo')) if isinstance(br, dict) else None
    if not repo: return False
    return any(isinstance(e, dict) and e.get('release') == 'batch' and _norm(e.get('path')) == repo
               for e in cfg.get('repos') or [])


def gaps(cfg, env=None):
    """{role: (why, fix)} of the catalog roles this computer cannot run now (pt-BR, for --load, --validate and Planou)."""
    out = {}
    if not browser_ok(env):
        out['qa'] = ('sem navegador neste computador', QA_FIX)
    if not release_ok(cfg):
        out['release'] = ('release nao configurado neste computador (merge, tag e deploy)', RELEASE_FIX)
    return out


def available(cfg, env=None):
    """The catalog roles this instance declares under all_roles, in catalog order (dev is always there)."""
    g = gaps(cfg, env)
    return [r for r, _ in CATALOG if r not in g]
