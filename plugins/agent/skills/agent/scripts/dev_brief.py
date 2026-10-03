#!/usr/bin/env python3
"""The worker brief of a dev instance, from the config: `agent.py <instance> --brief [repo]`.

A dev instance is a repository + its rules file + its release rules. What changes from one project to another lives in
the `repos` entry (schema.py) and in `behavior_config.batch-release`, never in the behaviors: the session prints this
brief and pastes it into the worker request (dev-worker), with the literal request and the task on top.

`repo` picks the entry by `name`, by the last folder of `path` or by `path`; without it, the first entry. The done
criterion comes from `done`; without it, a default by `release` (a repo without `release` that is the
`batch-release.repo` counts as "batch"; any other one says nothing about release). The instance's instructions.md still wins over it
(dev-worker says so), which keeps an instance that wrote its criterion there working unchanged.

`--role <behavior>` (default `dev-worker` when it is enabled) is the worker's role (PLN0119): PODE keeps only the
"can" phrases whose actions the role declares (permissions.py), the others go to "NAO E DESTE PAPEL", FERRAMENTAS lists
the config tools the role uses, and the full suite and the deploy lines only show for a role that declares them
(`batch-release`, the integrator). A role without a declaration keeps the whole autonomy, as before.

PLN0259: release "ci" is a PR the CI merges and publishes (label automerge): the worker opens it and comes back without
waiting; `fragments` of the repo says where its changelog text goes.

PLN0260: MAPA points at the repo's short map (`map`, relative to path; without it, `docs/MAP.md` when the repo has
one), ARQUIVOS PROVAVEIS and MODELO are filled by the session (`--files`, `--model sonnet|default`; without them, a
placeholder the session replaces) and ECONOMIA tells the worker to batch commands and never read a large file whole.

PLN0281: `--role code-review` or `--role qa` is the brief of an independent review or QA worker (one instance that does
the whole cycle): a read-only worktree, no RELEASE line, the verdict as the done criterion and an INDEPENDENCIA line
(a new worker, not the one that delivered; the session sends `fila done` or `fila ajuste`, never the worker).
PLN0054: `--role prototype` is the brief of a worker whose output never enters the repository (SCRATCH_ROLES): no
worktree (the scratchpad), no release, the PNGs as the done criterion.
"""
import os, re, subprocess

import permissions, schema

RETURN = 'RESULTADO, O QUE MUDOU, LINKS, VALIDACAO, PENDENTE DO USUARIO, RASCUNHOS, ESTADO SUGERIDO'
# the independent review and QA workers (PLN0281): their worktree, their done criterion and the independence rule
REVIEW_ROLES = {
    'code-review': {
        'worktree': '{wt}/review-<pid>, destacada da branch da entrega (git worktree add --detach), so para ler e rodar um '
                    'teste filtrado; remover no fim; nunca direto em {path}',
        'done': 'o parecer do code-review (Revisao de <PID> (<sha7>): aprovada | ajuste pedido, com os criterios do '
                'BEHAVIOR), comentado na PR quando pr_comment; sem commit, sem push, sem merge',
        'what': 'revisao'},
    'qa': {
        'worktree': 'a do qa_env.py (<worktrees>/qa-<pid>, destacada da branch), que ele cria e remove; nunca direto em {path}',
        'done': 'o parecer do qa (qa-<pid>.md: casos, axe, larguras) com as capturas, comentado na PR quando pr_comment; '
                'ambiente derrubado (qa_env.py down); sem commit, sem push',
        'what': 'QA'},
}
# workers whose output never enters the repository (PLN0054): the prototype is PNGs in the scratchpad, read-only repo
SCRATCH_ROLES = {
    'prototype': {
        'worktree': 'nenhuma: o HTML e os PNGs ficam numa subpasta do scratchpad (<scratchpad>/<PID>-prototipo/); {path} '
                    'so para ler (regras, referencia, tokens, node_modules do node_dir)',
        'done': 'os PNGs do prototipo (prototype_shot.cjs, um por largura e tema) conferidos um a um, com os caminhos e o '
                'que cada um mostra na volta; sem codigo, sem commit, sem push, sem PR',
        'release': 'nenhum: o prototipo nao entra no repositorio; o dev so comeca depois da aprovacao do usuario'},
}
INDEPENDENT = ('{what} independente: este worker e NOVO, nao e o que entregou nem continuacao dele, e nao recebe o '
               'transcript nem o resumo de quem desenvolveu (so o pedido, a PR ou a branch e o criterio); o parecer diz '
               '"{what} independente (worker novo)"; quem manda fila done ou fila ajuste e a sessao, nunca o worker')
MODELS = ('sonnet', 'default')
MAP_DEFAULT = 'docs/MAP.md'
FILES_TODO = '(a sessao preenche: arquivos e funcoes que a mudanca toca, do mapa ou de uma busca)'
MODEL_TODO = '(a sessao escolhe sonnet ou default pela regra do dev-worker; na duvida, default)'
ECONOMY = ('comece pelos ARQUIVOS PROVAVEIS e pelo mapa, sem busca exploratoria de uma linha por vez; junte leituras e '
           'comandos independentes na mesma rodada; arquivo grande nunca inteiro (grep -n e sed -n do trecho); saida '
           'de teste e build resumida (| tail -n 20)')
RELEASE_TEXT = {
    'batch': 'em lote: o worker de desenvolvimento termina numa branch pronta; suite completa, versao, tag e deploy sao '
             'so do integrador (comportamento batch-release)',
    'pr': 'por PR: merge (squash) so com a CI verde e se a autonomia deixar',
    'ci': 'pela CI: o worker abre a PR com o rotulo automerge e volta na hora, sem esperar a CI; quando a CI fica verde '
          'ela faz o merge (squash), a versao (feat = minor, o resto = patch), o CHANGELOG, a tag e a publicacao. A PR '
          'nao mexe em versao nem em secao de versao do CHANGELOG',
    'none': 'a pessoa faz o merge: o worker deixa a PR pronta',
}


def pick(cfg, key=None):
    """The repos entry for `key` (name, folder name or path); the first one without key. None when there is none."""
    repos = [e for e in cfg.get('repos') or [] if isinstance(e, dict) and isinstance(e.get('path'), str)]
    if not key: return repos[0] if repos else None
    for e in repos:
        if key in (schema.repo_name(e), e['path'], os.path.basename(os.path.normpath(e['path']))): return e
    return None


def release_mode(repo, release_opts):
    """The repo's "release"; without it, "batch" when it is the repo batch-release publishes, else None (not said in
    the config: the instance's instructions.md says it)."""
    if repo.get('release'): return repo['release']
    if schema._same_path(release_opts.get('repo'), repo.get('path')): return 'batch'
    return None


def default_done(repo, release_opts):
    base = repo.get('base') or 'main'
    mode = release_mode(repo, release_opts)
    if mode == 'batch':
        frag = release_opts.get('fragments')
        extra = f', texto de changelog em {frag}/<branch>.md' if frag else ''
        return (f'branch propria com rebase na {base} e push, testes filtrados verdes{extra}; '
                f'voltar com "PRONTA PARA RELEASE: <branch>"')
    if mode == 'pr':
        return f'PR aberta contra a {base} com a CI verde e os testes filtrados verdes; merge (squash) so se a autonomia deixar'
    if mode == 'ci':
        frag = repo.get('fragments')
        extra = f', texto de changelog em {frag}' if frag else ''
        return (f'PR aberta contra a {base} com o rotulo automerge (gh pr create --label automerge), titulo em '
                f'Conventional Commits (feat/fix/chore/docs: define a versao) e os testes filtrados verdes{extra}; '
                f'voltar logo depois de abrir a PR, sem esperar a CI e sem merge, tag ou publish (a CI faz)')
    if mode is None: return 'o do instructions.md da instancia; sem ele, PR em rascunho com os testes filtrados verdes'
    return 'PR em rascunho com os testes filtrados verdes'


def map_of(repo):
    """The repo's short map as a path to print: `map` (relative to path), else docs/MAP.md when the repo has one."""
    m = repo.get('map')
    if not m:
        if not os.path.isfile(os.path.join(os.path.expanduser(repo['path']), MAP_DEFAULT)): return None
        m = MAP_DEFAULT
    return m if os.path.isabs(m) or m.startswith('~') else os.path.join(repo['path'], m)


def brief(cfg, key=None, perms=None, model=None, files=None):
    """The brief as a list of lines, or None when the repo is not in the config. `perms` is the role's declaration
    ({'tools', 'actions'}, permissions.role); None keeps everything. `model` (sonnet|default) and `files` fill MODELO
    and ARQUIVOS PROVAVEIS; without them the lines carry what the session must fill."""
    repo = pick(cfg, key)
    if repo is None: return None
    bc = cfg.get('behavior_config') if isinstance(cfg.get('behavior_config'), dict) else {}
    rel = bc.get('batch-release') if isinstance(bc.get('batch-release'), dict) else {}
    path, base, mode = repo['path'], repo.get('base') or 'main', release_mode(repo, rel)
    wt = repo.get('worktrees') or os.path.join(os.path.dirname(os.path.normpath(path)) or '.', 'wt')
    review = REVIEW_ROLES.get((perms or {}).get('role'))
    scratch = SCRATCH_ROLES.get((perms or {}).get('role'))
    own = review or scratch
    out = [f'REPOSITORIO: {schema.repo_name(repo)} ({path})',
           'WORKTREE: ' + (own['worktree'].format(wt=wt, path=path) if own
                           else f'{wt}/<nome-curto>, branch propria a partir de origin/{base}; nunca direto em {path}')]
    gh = [t for t in cfg.get('tools') or [] if isinstance(t, dict) and t.get('kind') == 'cli' and t.get('name') == 'gh']
    if repo.get('gh_account'):
        env = ' '.join(f'{k}={v}' for k, v in sorted(((gh[0].get('env') or {}) if gh else {}).items()))
        out.append(f'CONTA GITHUB: {repo["gh_account"]}' + (f' (gh com {env})' if env else ''))
    rules = []
    if repo.get('rules'): rules.append(repo['rules'] if os.path.isabs(repo['rules']) or repo['rules'].startswith('~')
                                       else os.path.join(path, repo['rules']))
    rules += repo.get('shared_rules') or []
    out.append('REGRAS: ' + ('; '.join(rules) if rules else '(nenhum arquivo no config: README e CONTRIBUTING do repositorio)'))
    mp = map_of(repo)
    if mp: out.append(f'MAPA: {mp} (o que fica em cada pasta; leia antes de buscar)')
    out.append('ARQUIVOS PROVAVEIS: ' + (files or FILES_TODO))
    out.append('MODELO: ' + (model or MODEL_TODO))
    out.append('ECONOMIA: ' + ECONOMY)
    if schema.is_public(cfg, repo):
        out.append('PUBLICO: sim; nenhum nome de cliente ou de pessoa, id real ou captura com dado real (o code-review reprova)')
    tests = repo.get('tests') or {}
    out.append('TESTES FILTRADOS (so o que a mudanca afeta): ' + (tests.get('filtered') or 'os do arquivo de regras'))
    if review: out.append('INDEPENDENCIA: ' + INDEPENDENT.format(what=review['what']))
    elif scratch: out.append('RELEASE: ' + scratch['release'])
    elif mode in RELEASE_TEXT: out.append(f'RELEASE: {mode}, {RELEASE_TEXT[mode]}')
    else: out.append('RELEASE: (nao definido no config: ver o instructions.md da instancia)')
    allowed = (lambda a: a in perms['actions']) if perms else (lambda a: True)
    if mode == 'batch' and (allowed('test.full') or allowed('deploy')):
        lock = rel.get('test_lock')
        full = tests.get('full')
        if not lock: suite = full or 'as do arquivo de regras'
        elif full: suite = f'flock -o {lock} {full}'
        else: suite = f'as do arquivo de regras, sob flock -o {lock}'
        if allowed('test.full'): out.append('SUITE COMPLETA (so o integrador): ' + suite)
        if allowed('deploy') and (rel.get('deploy_cmd') or rel.get('deploy_lock')):
            cmd, lk = rel.get('deploy_cmd'), rel.get('deploy_lock')
            out.append('DEPLOY (so o integrador): ' + (f'flock -o {lk} {cmd}' if cmd and lk else cmd if cmd
                                                      else f'o do arquivo de regras, sob flock -o {lk}')
                       + (f'; uma linha por deploy em {rel["deploy_log"]}' if rel.get('deploy_log') else ''))
    out.append('CRITERIO DE PRONTO: ' + (own['done'] if own else (repo.get('done') or default_done(repo, rel))
                                          + ' (o instructions.md da instancia vale sobre este, quando diz outra coisa)'))
    a = cfg.get('autonomy') if isinstance(cfg.get('autonomy'), dict) else {}
    can, other = list(a.get('can') or []), []
    if perms:
        tools = [f'{t["kind"]}:{t["name"]}' for t in cfg.get('tools') or [] if isinstance(t, dict)
                 and t.get('kind') != 'subagent' and f'{t.get("kind")}:{t.get("name")}' in perms['tools']]
        if tools: out.append('FERRAMENTAS: ' + ', '.join(tools))
        other = [x for x in can if not permissions.role_allows(x, perms['actions'])]
        can = [x for x in can if x not in other]
    for k, label, v in (('can', 'PODE', can), ('ask_first', 'PERGUNTAR ANTES', a.get('ask_first')),
                        ('never', 'NUNCA', a.get('never'))):
        if v: out.append(f'{label}: ' + '; '.join(v))
    if other: out.append(f'NAO E DESTE PAPEL ({perms["role"]}), nao fazer: ' + '; '.join(other))
    out.append('AUTONOMIA DA TAREFA: a mais restrita entre a de cima e a da tarefa do Planou')
    out.append('VOLTA: ' + RETURN + ('; no RESULTADO, o veredito (aprovada | ajuste pedido) e o sha7 revisado' if review else ''))
    return out


def role_of(cfg, rest, behavior_file):
    """(rest without --role, the role's declaration or None). --role must be an enabled behavior; without it,
    dev-worker when enabled."""
    rest, name = list(rest), None
    if '--role' in rest:
        i = rest.index('--role')
        if i + 1 >= len(rest): raise SystemExit('uso: --brief [repo] [--role <comportamento>]')
        name = rest[i + 1]; del rest[i:i + 2]
        if name not in (cfg.get('behaviors') or []):
            raise SystemExit(f'papel {name!r} nao esta em "behaviors" (tem: {", ".join(cfg.get("behaviors") or []) or "(nenhum)"})')
    elif 'dev-worker' in (cfg.get('behaviors') or []): name = 'dev-worker'
    p = permissions.role(name, behavior_file) if name else None
    if p is not None: p = {**p, 'role': name}
    return rest, p


def take(rest, flag):
    """(rest without `flag <value>`, the value or None)."""
    rest = list(rest)
    if flag not in rest: return rest, None
    i = rest.index(flag)
    if i + 1 >= len(rest): raise SystemExit(f'uso: --brief [repo] [--role <comportamento>] [--model sonnet|default] [--files "<arquivos>"]')
    v = rest[i + 1]; del rest[i:i + 2]
    return rest, v


def cli(cfg, rest, behavior_file=None):
    rest, perms = role_of(cfg, rest, behavior_file)
    rest, model = take(rest, '--model')
    rest, files = take(rest, '--files')
    if model is not None and model not in MODELS: raise SystemExit(f'--model: {model!r} ({", ".join(MODELS)})')
    key = rest[0] if rest else None
    lines = brief(cfg, key, perms, model, files)
    if lines is None:
        names = ', '.join(schema.repo_name(e) for e in cfg.get('repos') or [] if isinstance(e, dict)) or '(nenhum)'
        raise SystemExit(f'sem repositorio {key!r} em "repos" (tem: {names})' if key else 'sem "repos" no config')
    print('\n'.join(lines))


def slug(path):
    """owner/name of a GitHub repository, from its origin remote; None when there is none."""
    try:
        url = subprocess.run(['git', '-C', os.path.expanduser(path), 'remote', 'get-url', 'origin'],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$', url)
    return m.group(1) if m else None


def public(cfg):
    """The public repositories as rows (name, path, owner/name), '-' when unknown: the `repos` entries of
    schema.public_repos() with the GitHub name of their origin, then the old public_repos items that are none of them."""
    repos = [e for e in cfg.get('repos') or [] if isinstance(e, dict) and isinstance(e.get('path'), str)]
    rows, known = [], set()
    for x in schema.public_repos(cfg):
        e = next((r for r in repos if x == schema.repo_name(r) or schema._same_path(x, r['path'])), None)
        if e is not None:
            s = slug(e['path'])
            row = (schema.repo_name(e), e['path'], s or '-')
        elif x in known:
            continue
        else:
            row = ('-', '-', x) if '/' in x and not x.startswith(('~', '/', '.')) else ('-', x, '-')
        if row in rows: continue
        rows.append(row); known.update(v for v in row if v != '-')
    return rows


def public_cli(cfg):
    rows = public(cfg)
    if not rows: print('(nenhum repositorio publico: "public": true em "repos")'); return
    for r in rows: print('\t'.join(r))
