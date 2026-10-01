#!/usr/bin/env python3
"""Mechanical checks of a branch diff for the code-review behavior (PLN0106). Read only: it never checks out, writes
or pushes anything; the verdict is the reviewer's, this only points where to look.

  python3 review_check.py --repo PATH --head REF [--base origin/main] [--max-lines 600] [--terms-file FILE]
                          [--require-tests | --no-require-tests]

Prints one line per finding and a last line OK or ATENCAO. Exit 0 = nothing found, 1 = something to look at,
2 = the diff could not be read.
  REVISAO: <head> sobre <base>: N arquivos, +A -D (limite L: ok|ACIMA)
  TESTES: ... | TESTES: nenhum arquivo de teste mudou
  SEGREDO? file:line: kind (valor mascarado)        a secret-looking value in an added line; the value is never printed
  NOME? file:line: termo "c***"                     a term of the private list (client, person) in an added line
  RISCO? file:line: kind                            a pattern that asks for a look (shell=True, eval, raw SQL, innerHTML)
  CAPTURAS: files                                   images in the diff
The terms file is private (outside the repository), one term per line, '#' for comments; terms are matched as whole
words, case-insensitive, and printed masked, so the output can go to a PR comment or to Planou.
"""
import argparse, os, re, subprocess, sys

SECRETS = [
    ('chave privada', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('token do GitHub', re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})')),
    ('chave AWS', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('token do Slack', re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{10,}')),
    ('chave do Planou', re.compile(r'\bpl_ag_[A-Za-z0-9_]{20,}')),
    ('chave de API', re.compile(r'\b(sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,})')),
    ('JWT', re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')),
    ('senha ou token literal', re.compile(
        r'(?i)\b(password|passwd|senha|secret|token|api[_-]?key|client[_-]?secret)\b["\']?\s*[:=]\s*["\'][^"\'\s]{8,}["\']')),
]
# placeholders a test or a doc uses on purpose
FAKE = re.compile(r'(?i)(TEST|EXAMPLE|exemplo|fake|dummy|xxxx|changeme|<[^>]+>|\$\{|\{\{)')
RISKS = [
    ('shell=True', re.compile(r'\bshell\s*=\s*True\b')),
    ('eval/exec', re.compile(r'(?<![\w.])(eval|exec)\s*\(')),
    ('pickle/yaml.load inseguro', re.compile(r'\bpickle\.loads?\s*\(|\byaml\.load\s*\((?![^)]*Loader)')),
    ('SQL montado com texto', re.compile(r'(FromSqlRaw|ExecuteSqlRaw)\s*\(\s*\$"|\.execute\s*\(\s*f["\']|\.execute\s*\([^)]*%\s*\(')),
    ('HTML sem escape', re.compile(r'\.innerHTML\s*=|dangerouslySetInnerHTML|\bv-html\b|\|\s*safe\b')),
    ('TLS desligado', re.compile(r'verify\s*=\s*False|rejectUnauthorized\s*:\s*false|ServerCertificateCustomValidationCallback')),
    ('permissao aberta', re.compile(r'chmod\s+(-R\s+)?777|\b0o?777\b|AllowAnonymous')),
]
TEST_PATH = re.compile(r'(^|/)(tests?|__tests__|spec|e2e)(/|$)|(^|/)test_[^/]+$|[._-](test|spec|tests)\.[a-z]+$|Tests?/|\.Tests?\.',
                       re.IGNORECASE)
IMAGE = re.compile(r'\.(png|jpe?g|gif|webp|svg)$', re.IGNORECASE)


def git(repo, *args):
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)
    if r.returncode != 0: raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else 'git falhou')
    return r.stdout


def mask(text):
    text = text.strip()
    return text[:1] + '***' if len(text) > 1 else '***'


def load_terms(path):
    if not path: return []
    with open(os.path.expanduser(path), encoding='utf-8') as f:
        return [t.strip() for t in f if t.strip() and not t.lstrip().startswith('#')]


def added_lines(diff):
    """(file, line number, text) of each added line of a unified diff with -U0."""
    file, n = None, 0
    for raw in diff.splitlines():
        if raw.startswith('+++ '):
            file = raw[6:] if raw.startswith('+++ b/') else None
        elif raw.startswith('@@'):
            m = re.search(r'\+(\d+)', raw)
            n = int(m.group(1)) if m else 0
        elif raw.startswith('+') and file:
            yield file, n, raw[1:]
            n += 1


def check(repo, base, head, max_lines=600, terms=(), require_tests=True):
    """-> (lines, findings): the report and how many things to look at."""
    rng = f'{base}...{head}'
    stats = []
    for row in git(repo, 'diff', '--numstat', rng).splitlines():
        a, d, path = row.split('\t', 2)
        stats.append((0 if a == '-' else int(a), 0 if d == '-' else int(d), path))
    add, dele = sum(s[0] for s in stats), sum(s[1] for s in stats)
    over = add + dele > max_lines
    out = [f'REVISAO: {head} sobre {base}: {len(stats)} arquivos, +{add} -{dele} '
           f'(limite {max_lines}: {"ACIMA" if over else "ok"})']
    found = int(over)
    tests = [p for _, _, p in stats if TEST_PATH.search(p)]
    code = [p for _, _, p in stats if not TEST_PATH.search(p) and not p.lower().endswith(('.md', '.txt'))]
    if tests: out.append(f'TESTES: {len(tests)} arquivos de teste mudaram: ' + ', '.join(tests[:8]) + (' ...' if len(tests) > 8 else ''))
    else:
        out.append('TESTES: nenhum arquivo de teste mudou')
        if require_tests and code: found += 1
    words = [(t, re.compile(r'(?<![\w])' + re.escape(t) + r'(?![\w])', re.IGNORECASE)) for t in terms]
    for file, n, text in added_lines(git(repo, 'diff', '-U0', '--no-color', '--no-ext-diff', rng)):
        for kind, rx in SECRETS:
            m = rx.search(text)
            if m and not FAKE.search(m.group(0)):
                out.append(f'SEGREDO? {file}:{n}: {kind} (valor mascarado)'); found += 1; break
        for t, rx in words:
            if rx.search(text):
                out.append(f'NOME? {file}:{n}: termo "{mask(t)}"'); found += 1
        for kind, rx in RISKS:
            if rx.search(text):
                out.append(f'RISCO? {file}:{n}: {kind}'); found += 1
    images = [p for _, _, p in stats if IMAGE.search(p)]
    if images: out.append('CAPTURAS: ' + ', '.join(images[:10]) + (' ...' if len(images) > 10 else ''))
    out.append(f'ATENCAO: {found} pontos para conferir' if found else 'OK: nada mecanico para conferir')
    return out, found


def main(argv=None):
    ap = argparse.ArgumentParser(description='checagens mecanicas do diff de uma branch (so leitura)')
    ap.add_argument('--repo', required=True)
    ap.add_argument('--head', required=True, help='a branch ou o commit revisado (ex.: origin/feat/x)')
    ap.add_argument('--base', default='origin/main')
    ap.add_argument('--max-lines', type=int, default=600)
    ap.add_argument('--terms-file', help='lista privada de nomes que nao podem ir para repositorio publico')
    ap.add_argument('--require-tests', dest='require_tests', action='store_true', default=True)
    ap.add_argument('--no-require-tests', dest='require_tests', action='store_false')
    a = ap.parse_args(argv)
    try:
        lines, found = check(os.path.expanduser(a.repo), a.base, a.head, a.max_lines, load_terms(a.terms_file), a.require_tests)
    except (RuntimeError, OSError, ValueError) as e:
        print(f'ERRO: nao consegui ler o diff: {e}', file=sys.stderr)
        return 2
    print('\n'.join(lines))
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
