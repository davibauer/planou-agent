#!/usr/bin/env python3
"""Hook security (PLN0121): the security agent's read-only scans. Dependencies with a known vulnerability, secrets in a
repository or a log, file permissions of the secrets and the agents' autonomy against their roles. A scan with a finding
opens a task in Planou with the evidence (never the secret itself), a finding that goes away is noted on the same task,
and nothing is ever fixed, rotated or deleted here.

It is the health hook (hooks/health.py) with other check kinds: the same config keys (checks, after, open_tasks, project,
priority, max_tasks_day, reopen_h, report_hour, task_note, timeout_s), the same task life (one per episode, reuse within
reopen_h, a task the person deleted is not created again, Planou down = next tick) and the same dry run. What changes:
  after        default 1 (a finding does not flicker)
  report_hour  default null (no daily report)
  timeout_s    default 300 (an audit asks the registry)
  every_h      per check; default 24 for deps and secrets, 1 for perms and agents. The CLI runs every check now
  priority     per kind when neither the check nor the hook gives one: secrets 1, deps 2, perms 2, agents 3
  note_changes a finding that changes while its task is open is noted on the task (-- MUDOU)
A scan that cannot run for a reason outside what it watches (npm or dotnet missing, a project not restored, no network
for the audit, pip-audit not installed) shows "nao rodou" and keeps its state: never a finding, never a task.

Check kinds (options besides id, kind, label, after, priority and every_h):
  deps     path (a git repository); ecosystems (["npm", "dotnet", "pip"], default all that the repository has);
           min_severity ("low", "moderate", "high" (default), "critical"); ignore (advisory ids accepted by the person,
           e.g. "GHSA-xxxx-xxxx-xxxx" or "CVE-2026-1234"); skip (fnmatch globs of paths relative to the repository).
           npm: `npm audit --json --package-lock-only` in each folder with a tracked package-lock.json (nothing is
           installed). dotnet: `dotnet list <sln> package --vulnerable --include-transitive --format json` for each tracked
           .sln/.slnx (the .csproj files when there is none); a project without restore is "nao rodou" (never runs
           restore). pip: `pip-audit -r <requirements*.txt> -f json` only when pip-audit is installed
  secrets  repo (a git repository: its tracked and untracked-not-ignored files) and/or logs (globs of log files);
           ignore (fnmatch globs: relative paths in the repository, full paths for logs). Every line is matched against
           SECRET_PATTERNS; a value that looks like a placeholder (test, fake, example, ${...}) or a line with
           "secret-scan: allow" is skipped. The match is never kept: the evidence is `<file>:<line>: <type> <mask>`, where
           the mask is the type's fixed prefix (ghp_, AKIA, sk-ant-...) and the length. A tracked .env, private key file
           or anything under a secrets/ folder is reported by name only and never opened; a log under a secrets/ folder
           is never opened either
  perms    paths (globs); max_file_mode ("0600") and max_dir_mode ("0700"). Only lstat: the path, the entries of a folder
           (two levels) and their owner. A mode with a bit beyond the maximum, or another owner, is a finding. Never opens
           a file
  agents   configs (globs of instance config.json, default ~/.config/agent/*/config/config.json). The same check as
           --validate (permissions.warnings): an "autonomy.can" phrase that gives an action no enabled behavior
           declares, and a tool no enabled behavior uses. An instance whose behaviors declare nothing is not checked

Output: `== SEGURANCA` (live: -- ACHOU, -- MUDOU, -- RESOLVIDO and the task lines; dry: every scan and "abriria tarefa").
CLI: agent.py <instance> --security   runs every scan now (nothing saved) and lists what it would open
"""
import fnmatch, glob, json, os, re, shutil, stat, subprocess

import paths, permissions, schema
from adapters import health
from adapters.health import Unavailable, _safe

KINDS = ('deps', 'secrets', 'perms', 'agents')
SEVERITY = {'info': 0, 'low': 1, 'moderate': 2, 'medium': 2, 'high': 3, 'critical': 4, 'unknown': 3}
SEVERITY_PT = {0: 'info', 1: 'baixa', 2: 'media', 3: 'alta', 4: 'critica'}
MAX_EVIDENCE = 20
LINE_CAP = 4000
EVERY_DEFAULT = {'deps': 24, 'secrets': 24, 'perms': 1, 'agents': 1}

# (type, regex, group whose text is a fixed prefix shown in the mask, or None). Group 'v' is the value checked against
# PLACEHOLDER (the whole match when there is none). The matched text never leaves _scan_line.
SECRET_PATTERNS = [
    ('token do GitHub', re.compile(r'\b(gh[pousr]_)[A-Za-z0-9]{36,}'), 1),
    ('token do GitHub', re.compile(r'\b(github_pat_)[A-Za-z0-9_]{50,}'), 1),
    ('token do GitLab', re.compile(r'\b(glpat-)[A-Za-z0-9_-]{20,}'), 1),
    ('chave de agente do Planou', re.compile(r'\b(pl_ag_)[A-Za-z0-9]{8}_[A-Za-z0-9]{30,}'), 1),
    ('token do Slack', re.compile(r'\b(xox[abeprs]-)[A-Za-z0-9-]{10,}'), 1),
    ('chave da Anthropic', re.compile(r'\b(sk-ant-)[A-Za-z0-9_-]{20,}'), 1),
    ('chave da OpenAI', re.compile(r'\b(sk-)(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,}'), 1),
    ('chave da AWS', re.compile(r'\b(AKIA|ASIA)[0-9A-Z]{16}\b'), 1),
    ('chave do Google', re.compile(r'\b(AIza)[0-9A-Za-z_-]{35}'), 1),
    ('chave privada', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY'), None),
    ('JWT', re.compile(r'\b(eyJ)[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'), 1),
    ('URL com senha', re.compile(r'\b[a-z][a-z0-9+.-]{1,20}://[^\s:/@"\']{1,64}:(?P<v>[^\s@/"\']{6,})@'), None),
    ('senha em texto', re.compile(r'(?i)\b(?P<k>password|passwd|senha|secret|api[_-]?key|access[_-]?token|client[_-]?secret)'
                                  r'\b["\']?\s*[:=]\s*["\'](?P<v>[^"\'\s]{12,})["\']'), None),
]
PLACEHOLDER = re.compile(r'(?i)test|fake|example|exemplo|dummy|sample|placeholder|changeme|change_me|redacted|your[_-]|'
                         r'xxxx|\*\*\*|\.\.\.|<|>|\$\{|\{\{|%\(|\$[A-Z_]|process\.env|os\.environ|localhost|127\.0\.0\.1')
ALLOW_MARK = 'secret-scan: allow'
# files that hold a secret by nature: reported by name when tracked, never opened
SECRET_FILE = re.compile(r'(^|/)\.env(\.[A-Za-z0-9_-]+)?$|(^|/)id_(rsa|dsa|ecdsa|ed25519)$|\.(p12|pfx|keystore|jks)$|'
                         r'(^|/)cookies?(\.[a-z]+)?$|(^|/)\.?credentials(\.json)?$', re.I)
SAFE_ENV = re.compile(r'\.(example|sample|template|dist)$', re.I)
SECRET_DIR = re.compile(r'(^|/)secrets?(/|$)')


def _tilde(p):
    h = os.path.expanduser('~')
    return '~' + p[len(h):] if p == h or p.startswith(h + os.sep) else p


def _json_out(argv, cwd, timeout, what):
    """Runs argv (never a shell) and returns its stdout as JSON. A missing tool or output that is not JSON is
    Unavailable; the exit code is not looked at (an audit exits 1 exactly when it finds something)."""
    env = {**os.environ, 'DOTNET_CLI_TELEMETRY_OPTOUT': '1', 'DOTNET_NOLOGO': '1', 'NO_UPDATE_NOTIFIER': '1',
           'npm_config_update_notifier': 'false', 'npm_config_fund': 'false'}
    try:
        r = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                           timeout=timeout, env=env)
    except FileNotFoundError:
        raise Unavailable(f'{argv[0]} nao instalado')
    except subprocess.TimeoutExpired:
        raise Unavailable(f'{what} nao respondeu em {timeout} s')
    out = r.stdout.decode('utf-8', 'replace').strip()
    try:
        return json.loads(out)
    except ValueError:
        err = (r.stderr.decode('utf-8', 'replace').strip().splitlines() or [''])[-1]
        raise Unavailable(f'{what} sem JSON (saiu com {r.returncode}{": " + _safe(err, 120) if err else ""})')


def _git_files(repo, others=False):
    if not os.path.isdir(os.path.join(repo, '.git')) and not os.path.isfile(os.path.join(repo, '.git')):
        raise Unavailable(f'{_tilde(repo)} nao e um repositorio git')
    argv = ['git', '-C', repo, 'ls-files', '-z', '--cached'] + (['--others', '--exclude-standard'] if others else [])
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=60)
    except FileNotFoundError:
        raise Unavailable('git nao instalado')
    except subprocess.TimeoutExpired:
        raise Unavailable('git ls-files nao respondeu em 60 s')
    if r.returncode != 0: raise Unavailable(f'git ls-files saiu com {r.returncode}')
    return sorted({f for f in r.stdout.decode('utf-8', 'replace').split('\0') if f})


def _skipped(rel, globs):
    return any(fnmatch.fnmatch(rel, g) for g in globs or [])


def _cap(ev):
    return ev if len(ev) <= MAX_EVIDENCE else ev[:MAX_EVIDENCE] + [f'... e mais {len(ev) - MAX_EVIDENCE}']


# ------------------------------------------------------------------ deps

def _advisory_id(url, fallback=''):
    m = re.search(r'(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}|CVE-\d{4}-\d{4,})', str(url or ''), re.I)
    return m.group(1) if m else str(fallback or '')


def _npm(repo, rel_dir, c, timeout):
    data = _json_out(['npm', 'audit', '--json', '--package-lock-only'], os.path.join(repo, rel_dir), timeout, 'npm audit')
    if isinstance(data.get('error'), dict):
        raise Unavailable('npm audit: ' + _safe(data['error'].get('code') or data['error'].get('summary') or 'erro', 80))
    out = []
    for name, v in sorted((data.get('vulnerabilities') or {}).items()):
        via = [x for x in v.get('via') or [] if isinstance(x, dict)]
        if not via: continue                                 # only pulled in by another vulnerable package
        ids = [i for i in (_advisory_id(x.get('url'), x.get('source')) for x in via) if i]
        if ids and all(i in c['ignore'] for i in ids): continue
        sev = max(SEVERITY.get(str(x.get('severity') or v.get('severity')).lower(), 3) for x in via)
        fix = v.get('fixAvailable')
        fix_t = ('correcao: npm audit fix' if fix is True else
                 f'correcao: {fix.get("name")}@{fix.get("version")}{" (major)" if fix.get("isSemVerMajor") else ""}'
                 if isinstance(fix, dict) else 'sem correcao publicada')
        out.append((sev, f'npm {rel_dir or "."}: {name} {v.get("range") or ""} ({SEVERITY_PT[sev]}) '
                         f'{", ".join(ids[:3])}; {fix_t}'))
    return out


def _dotnet(repo, target, c, timeout):
    data = _json_out(['dotnet', 'list', target, 'package', '--vulnerable', '--include-transitive', '--format', 'json'],
                     repo, timeout, 'dotnet list package')
    projects = data.get('projects') or []
    errors = [str(p.get('text') or '') for p in data.get('problems') or []
              if isinstance(p, dict) and str(p.get('level') or '').lower() == 'error']
    if errors:                                           # a partial answer is not a clean one
        if re.search(r'assets|restore', ' '.join(errors), re.I):
            raise Unavailable(f'{target}: sem restore (dotnet restore nao roda aqui)')
        raise Unavailable(f'{target}: {_safe(errors[0], 120)}')
    out, seen = [], set()
    for p in projects:
        pname = os.path.splitext(os.path.basename(str(p.get('path') or '')))[0]
        for fw in p.get('frameworks') or []:
            for kind, pkgs in (('direta', fw.get('topLevelPackages')), ('transitiva', fw.get('transitivePackages'))):
                for pkg in pkgs or []:
                    for v in pkg.get('vulnerabilities') or []:
                        aid = _advisory_id(v.get('advisoryurl'))
                        if aid in c['ignore']: continue
                        k = (pkg.get('id'), pkg.get('resolvedVersion'), aid)
                        if k in seen: continue
                        seen.add(k)
                        sev = SEVERITY.get(str(v.get('severity')).lower(), 3)
                        out.append((sev, f'nuget {pname}: {pkg.get("id")} {pkg.get("resolvedVersion") or ""} '
                                         f'({SEVERITY_PT[sev]}) {aid}; {kind}'))
    return out


def _pip(repo, req, c, timeout):
    if not shutil.which('pip-audit'): raise Unavailable('pip-audit nao instalado')
    data = _json_out(['pip-audit', '-r', os.path.join(repo, req), '-f', 'json', '--progress-spinner', 'off'], repo,
                     timeout, 'pip-audit')
    deps = data.get('dependencies') if isinstance(data, dict) else data
    out = []
    for d in deps or []:
        for v in d.get('vulns') or []:
            ids = [v.get('id')] + list(v.get('aliases') or [])
            if any(i in c['ignore'] for i in ids if i): continue
            fix = ', '.join(v.get('fix_versions') or []) or 'sem correcao publicada'
            out.append((SEVERITY['unknown'], f'pypi {req}: {d.get("name")} {d.get("version") or ""} (sem nota) '
                                             f'{v.get("id")}; correcao: {fix}'))
    return out


def check_deps(ch, t, prev):
    repo = os.path.expanduser(str(ch.get('path') or ''))
    if not repo: raise ValueError('"path" do repositorio')
    ecos = ch.get('ecosystems') or ['npm', 'dotnet', 'pip']
    c = {'ignore': set(ch.get('ignore') or [])}
    minimum = SEVERITY.get(str(ch.get('min_severity') or 'high').lower(), 3)
    files = [f for f in _git_files(repo) if not _skipped(f, ch.get('skip'))]
    jobs = []
    if 'npm' in ecos:
        jobs += [('npm', _npm, os.path.dirname(f)) for f in files
                 if os.path.basename(f) == 'package-lock.json' and 'node_modules/' not in f]
    if 'dotnet' in ecos:
        sln = [f for f in files if f.endswith(('.sln', '.slnx'))]
        jobs += [('dotnet', _dotnet, f) for f in (sln or [f for f in files if f.endswith('.csproj')])]
    if 'pip' in ecos:
        jobs += [('pip', _pip, f) for f in files if re.fullmatch(r'requirements[\w.-]*\.txt', os.path.basename(f))]
    if not jobs: return True, 'nenhum manifesto de dependencias (npm, NuGet, PyPI)', []
    found, down, ran = [], [], 0
    for eco, fn, target in jobs:
        try:
            found += [x for x in fn(repo, target, c, t) if x[0] >= minimum]
            ran += 1
        except Unavailable as e:
            down.append(f'{eco}: {e}')
    if not ran: raise Unavailable('; '.join(down[:3]))
    if not found and down and prev.get('fails'):
        # an open finding may be in the part that did not run: keep it, never a resolution by omission
        raise Unavailable('; '.join(down[:3]) + ' (achado aberto mantido)')
    if not found:
        return True, f'nenhuma falha conhecida ({ran} manifesto(s))' + (f'; nao rodou: {"; ".join(down[:2])}' if down else ''), []
    found.sort(key=lambda x: (-x[0], x[1]))
    by = {}
    for sev, _ in found: by[SEVERITY_PT[sev]] = by.get(SEVERITY_PT[sev], 0) + 1
    counts = ', '.join(f'{n} {s}' for s, n in by.items())
    ev = _cap([_safe(l, 240) for _, l in found]) + [_safe(f'{health.NOT_RUN} {d}', 240) for d in down[:3]]
    return False, f'{len(found)} pacote(s) com falha conhecida ({counts})', ev


# ------------------------------------------------------------------ secrets

def _scan_line(line):
    """[(type, mask)] of the secrets in one line. The matched text is never returned, only a fixed prefix and a length."""
    if ALLOW_MARK in line: return []
    out, seen = [], set()
    for kind, rx, prefix in SECRET_PATTERNS:
        for m in rx.finditer(line[:LINE_CAP]):
            value = m.groupdict().get('v') or m.group(0)
            if PLACEHOLDER.search(value): continue
            if kind in ('senha em texto', 'URL com senha') and not (re.search(r'[A-Za-z]', value) and re.search(r'\d', value)):
                continue
            if kind in seen: continue
            seen.add(kind)
            if prefix: mask = f'{m.group(prefix)}**** ({len(m.group(0))} caracteres)'
            elif kind == 'senha em texto': mask = f'{m.group("k").lower()}=****'
            elif kind == 'URL com senha': mask = '<esquema>://<usuario>:****@'
            else: mask = 'BEGIN ... PRIVATE KEY'
            out.append((kind, mask))
    return out


def _scan_file(path, where, max_bytes):
    """[evidence line] of one text file; binary files are skipped. Files bigger than max_bytes are read only at the end
    (the line numbers still count from the start)."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            start, lineno = max(0, size - max_bytes), 0
            while f.tell() < start:
                chunk = f.read(min(1 << 20, start - f.tell()))
                if not chunk: break
                lineno += chunk.count(b'\n')
            data = f.read()
    except OSError:
        return []
    if b'\0' in data[:8192]: return []
    text = data.decode('utf-8', 'replace').splitlines()
    if start: text = text[1:]; lineno += 1                  # the first line was cut in half
    out = []
    for i, line in enumerate(text, lineno + 1):
        for kind, mask in _scan_line(line):
            out.append(f'{where}:{i}: {kind} {mask}')
    return out


def check_secrets(ch, t, prev):
    repo = os.path.expanduser(str(ch.get('repo') or ''))
    logs, ignore = ch.get('logs') or [], ch.get('ignore') or []
    if not repo and not logs: raise ValueError('"repo" e/ou "logs"')
    max_bytes = int(ch.get('max_bytes') or 5_000_000)
    ev, files, scanned = [], set(), 0
    if repo:
        for rel in _git_files(repo, others=True):
            if _skipped(rel, ignore): continue
            if SECRET_DIR.search(rel) or (SECRET_FILE.search(rel) and not SAFE_ENV.search(rel)):
                ev.append(f'{rel}: arquivo de segredo no repositorio (nao aberto)'); files.add(rel); continue
            full = os.path.join(repo, rel)
            if not os.path.isfile(full) or os.path.islink(full): continue
            scanned += 1
            hits = _scan_file(full, rel, max_bytes)
            if hits: ev += hits; files.add(rel)
    for pat in logs:
        for f in sorted(glob.glob(os.path.expanduser(str(pat)))):
            if SECRET_DIR.search(f) or not os.path.isfile(f) or _skipped(f, ignore): continue
            scanned += 1
            hits = _scan_file(f, _tilde(f), max_bytes)
            if hits: ev += hits; files.add(f)
    if not ev: return True, f'nenhum segredo em {scanned} arquivo(s)', []
    return False, f'{len(ev)} segredo(s) em {len(files)} arquivo(s)', _cap(ev)


# ------------------------------------------------------------------ perms

def _mode(v, d):
    try: return int(str(v), 8) if v is not None else d
    except ValueError: return d


def check_perms(ch, t, prev):
    pats = ch.get('paths') or []
    if not isinstance(pats, list) or not pats: raise ValueError('"paths": globs das pastas e arquivos de segredo')
    fmax, dmax = _mode(ch.get('max_file_mode'), 0o600), _mode(ch.get('max_dir_mode'), 0o700)
    uid, ev, n = os.getuid(), [], 0

    def look(p, depth):
        nonlocal n
        try: st = os.lstat(p)
        except OSError: return
        if stat.S_ISLNK(st.st_mode): return
        n += 1
        isdir = stat.S_ISDIR(st.st_mode)
        mode, mx = stat.S_IMODE(st.st_mode), (dmax if isdir else fmax)
        if mode & ~mx: ev.append(f'{_tilde(p)}: {mode:04o} (esperado {mx:04o} ou mais fechado)')
        if st.st_uid != uid: ev.append(f'{_tilde(p)}: dono uid {st.st_uid}, nao o usuario do agente')
        if isdir and depth < 2:
            try: names = sorted(os.listdir(p))
            except OSError: return
            for name in names: look(os.path.join(p, name), depth + 1)

    for pat in pats:
        for p in sorted(glob.glob(os.path.expanduser(str(pat)))): look(p, 0)
    if not n: return True, 'nenhum caminho encontrado', []
    if not ev: return True, f'{n} caminho(s) com permissao fechada', []
    return False, f'{len(ev)} permissao(oes) aberta(s) em {n} caminho(s)', _cap(ev)


# ------------------------------------------------------------------ agents

def _behavior_resolver(root):
    def bf(name):
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,60}', name): return None
        for base in (os.path.join(root, 'behaviors'), paths.BEHAVIORS_DIR):
            f = os.path.join(base, name, 'BEHAVIOR.md')
            if os.path.isfile(f): return f
        return None
    return bf


def check_agents(ch, t, prev):
    pats = ch.get('configs') or ['~/.config/agent/*/config/config.json']
    ev, n, unchecked = [], 0, []
    for pat in pats:
        for f in sorted(glob.glob(os.path.expanduser(str(pat)))):
            d = os.path.dirname(f)
            root = os.path.dirname(d) if os.path.basename(d) == 'config' else d
            inst = os.path.basename(root)
            try:
                with open(f, encoding='utf-8') as fh: c = schema.normalize(json.load(fh))
            except (OSError, ValueError, schema.ConfigError) as e:
                ev.append(f'{inst}: config ilegivel ({type(e).__name__})'); continue
            n += 1
            if not c.get('behaviors'): unchecked.append(inst); continue
            for w in permissions.warnings(c, _behavior_resolver(root)):
                if 'nao declara(m) ferramentas' in w: unchecked.append(inst); continue
                if '"autonomy.can"' in w and 'nao casa com nenhuma acao' in w: continue
                ev.append(_safe(f'{inst}: {w}', 240))
    tail = f'; sem papeis declarados (nao conferidas): {", ".join(unchecked[:5])}' if unchecked else ''
    if not ev: return True, f'{n} instancia(s) conferida(s){tail}', []
    return False, f'{len(ev)} permissao(oes) alem do papel em {n} instancia(s){tail}', _cap(ev)


CHECKS = {'deps': check_deps, 'secrets': check_secrets, 'perms': check_perms, 'agents': check_agents}
HINT = {
    'deps': 'O que fazer: atualizar o pacote para a versao com a correcao numa entrega de worker, ou registrar o risco '
            'aceito no "ignore" da checagem. A decisao e da pessoa.',
    'secrets': 'O que fazer: girar a credencial no servico de origem, tirar do arquivo (e do historico do git, se foi '
               'versionada) e guardar em secrets/. Quem gira e a pessoa: o agente de seguranca so aponta.',
    'perms': 'O que fazer: fechar a permissao (chmod 600 no arquivo, 700 na pasta). Quem muda e a pessoa.',
    'agents': 'O que fazer: tirar a frase do autonomy.can da instancia, ou declarar a acao no papel (bloco permissions '
              'do BEHAVIOR.md), e rodar agent.py <instancia> --validate.',
}


class Gancho(health.Gancho):
    tipo = 'security'
    json_key = 'security'
    state_key = 'security'
    key_prefix = 'security'
    cli_flag = 'security'
    default_after = 1
    default_report_hour = None
    default_timeout = 300
    kind_priority = {'secrets': 1, 'deps': 2, 'perms': 2, 'agents': 3}
    note_changes = True
    cli_would = True
    kinds = KINDS
    checks_by_kind = CHECKS
    T = {'header': 'SEGURANCA', 'daily': 'SEGURANCA DIARIA', 'warn': 'seguranca', 'bad': 'com achado',
         'broke': 'ACHOU', 'back': 'RESOLVIDO', 'changed': 'MUDOU', 'back_since': 'aberto desde',
         'title': 'Seguranca: {label}: {summary}', 'back_note': 'Nao aparece mais na varredura de',
         'again_note': 'Apareceu de novo em', 'changed_note': 'Mudou na varredura de',
         'task_back': 'nao aparece mais (a pessoa confere e fecha)', 'task_again': 'apareceu de novo',
         'task_changed': 'o achado mudou', 'now': 'seguranca agora',
         'cli_help': 'roda as varreduras de seguranca agora (nada gravado) e diz o que abriria',
         'tasks_by': 'tarefa(s) aberta(s) pela varredura'}

    def configura(self):
        super().configura()
        for ch in self.checks:
            ch.setdefault('every_h', EVERY_DEFAULT.get(ch['kind'], 24))
            if ch['kind'] == 'secrets' and 'argv' in ch:
                self.problems.append(f'{ch["id"]}: "argv" nao e aceito numa varredura de segredos (so repo e logs)')
            if ch['kind'] == 'deps' and str(ch.get('min_severity') or 'high').lower() not in SEVERITY:
                self.problems.append(f'{ch["id"]}: "min_severity" (low, moderate, high, critical)')

    def description(self, ch, c, summary):
        lines = [f'Varredura "{self.label(ch)}" ({ch["kind"]}) com achado desde {health._hm(c.get("since"))}, vista pelo '
                 f'agente {paths.NAME} (so leitura: nada foi corrigido, girado nem apagado).', '', f'Resumo: {summary}']
        if c.get('evidence'):
            head = 'Evidencia (sem o segredo: arquivo, linha e tipo, mascarado):' if ch['kind'] == 'secrets' else 'Evidencia:'
            lines += ['', head] + [f'- {l}' for l in c['evidence']]
        lines += ['', HINT[ch['kind']]]
        if self.note: lines += ['', self.note]
        return '\n'.join(lines)
