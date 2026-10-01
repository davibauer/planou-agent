#!/usr/bin/env python3
"""What a role (behavior) may do, and whether an instance's autonomy gives more than its roles need (PLN0119).

Each BEHAVIOR.md declares the tools and the actions it uses in a fenced block:

    ```permissions
    tools: subagent:worker, cli:gh
    actions: worktree, code, test.filtered, push, pr.open
    ```

`tools` are `kind:name` like the config's "tools" entries; `actions` come from ACTIONS. Reading (sources, files, PRs)
is always allowed and never declared.

The config's "autonomy" is free text in pt-BR. `classify(phrase)` maps a phrase to the actions it grants with the
keyword table of ACTIONS (accents and case ignored). `--validate` warns (never errors: a running agent keeps working) when
a "can" phrase grants an action that no enabled behavior declares, when it matches no known action, when a config tool
is used by no enabled behavior, and when an enabled behavior declares nothing (then the check is skipped). "ask_first"
and "never" only restrict, so they are not checked. An instance without behaviors (not migrated yet) is not checked.

`--brief` keeps in PODE only the "can" phrases the worker's role allows (`role_allows`); the others go to a line of
their own, so the worker knows they are not his.
"""
import os, re, unicodedata

# action -> (what it means, pt-BR keywords of an autonomy phrase, on the text without accents and in lower case)
ACTIONS = {
    'worktree':      ('branch e worktree proprias', r'\bbranch|\bworktree|git fetch'),
    'code':          ('escrever codigo e commit', r'escrever codigo|\bcommits?\b|(editar|alterar) codigo'),
    'test.filtered': ('testes filtrados (uma classe, um spec, um arquivo)', r'testes? filtrad'),
    'test.full':     ('suite completa e e2e completo', r'suites? complet|e2e complet'),
    'push':          ('push de branch', r'\bpush\b'),
    'pr.open':       ('abrir PR', r'abrir (a |uma )?(pr|pull request)\b|criar (a |uma )?pr\b'),
    'pr.comment':    ('comentar na PR', r'coment\w*[^.;]*\bpr\b|gh pr comment'),
    'pr.merge':      ('merge (squash ou fast-forward)', r'\bmerge\b|fast-forward'),
    'tag':           ('tag de versao', r'\btags?\b'),
    'publish':       ('publicar plugin (publish-plugin)', r'publish-plugin|publicar (o )?plugin'),
    'release':       ('release em lote pelo integrador', r'release em lote|\bintegrador\b|fazer (o )?release'),
    'deploy':        ('deploy e voltar a versao', r'\bdeploy|\brollback|voltar a versao'),
    'planou.queue':  ('mover a tarefa na fila do Planou', r'\bfila\b|\bhandoff\b'),
    'planou.task':   ('criar tarefa no Planou', r'(abrir|criar)( uma)? tarefas?|\bbacklog\b'),
    'planou.note':   ('conversa, alerta e anotacao no Planou',
                      r'conversa do planou|relatorio|alerta no planou|\$pl alert|(anotar|comentar) na tarefa'),
    'notion':        ('escrever no Notion (pagina do dia)', r'\bnotion\b|pagina do dia'),
    'notify':        ('aviso no celular', r'aviso no celular|pushnotification|notificac'),
    'presence':      ('mudar o status de presenca', r'presenca|status do teams'),
    'message':       ('mandar mensagem a pessoas', r'(mandar|enviar|responder)\w*[^.;]*(mensage|e-?mail|slack|teams)|'
                                                   r'mensage\w* (a|para) pessoas'),
    'runner':        ('subir, parar ou relancar runner ou container', r'\brunner|\brestart|reinici|relanc|'
                                                                       r'(subir|parar)[^.;]*container'),
    'delete':        ('apagar dado, backup, volume ou imagem', r'\bapagar|\bexcluir|remover (o |os )?(dado|backup|volume|imagem)'),
}
READ_ONLY = re.compile(r'^(ler|leitura|consultar|checagens?)\b|so de leitura')
_KW = {a: re.compile(rx) for a, (_, rx) in ACTIONS.items()}
TOOL_RE = re.compile(r'(subagent|cli|skill|mcp|other):[A-Za-z0-9_.-]+')
BLOCK_RE = re.compile(r'^```permissions[ \t]*\n(.*?)^```', re.S | re.M)


def fold(text):
    """Lower case, no accents: 'Não' -> 'nao'."""
    t = unicodedata.normalize('NFKD', str(text)).encode('ascii', 'ignore').decode()
    return t.lower()


def classify(phrase):
    """The actions an autonomy phrase grants, as a set. {'read'} for a read-only phrase that names no action; empty
    when nothing matches."""
    t = fold(phrase).strip()
    got = {a for a, rx in _KW.items() if rx.search(t)}
    if got: return got                         # "ler a PR e fazer merge" still gives pr.merge
    return {'read'} if READ_ONLY.search(t) else set()


def parse(text):
    """The ```permissions block of a BEHAVIOR.md -> ({'tools': [...], 'actions': [...]}, problems). (None, []) when the
    file has no block."""
    m = BLOCK_RE.search(text or '')
    if not m: return None, []
    out, problems = {'tools': [], 'actions': []}, []
    for line in m.group(1).splitlines():
        if not line.strip(): continue
        k, _, v = line.partition(':')
        k = k.strip()
        if k not in out: problems.append(f'linha desconhecida no bloco permissions: {line.strip()!r}'); continue
        items = [x.strip() for x in v.split(',') if x.strip() and x.strip() != '-']
        for x in items:
            if k == 'actions' and x not in ACTIONS: problems.append(f'acao desconhecida {x!r}')
            if k == 'tools' and not TOOL_RE.fullmatch(x): problems.append(f'ferramenta {x!r} (formato kind:nome)')
        out[k] += items
    return out, problems


def read(path):
    """parse() of a BEHAVIOR.md file; (None, []) when it cannot be read."""
    try:
        with open(path, encoding='utf-8') as f: return parse(f.read())
    except OSError: return None, []


def union(behaviors, behavior_file):
    """(tools, actions, undeclared behaviors, problems) of the enabled behaviors."""
    tools, actions, missing, problems = set(), {'read'}, [], []
    for b in behaviors:
        f = behavior_file(b) if isinstance(b, str) else None
        if not f: continue                      # a missing behavior is already an error of validate()
        p, probs = read(f)
        problems += [f'"behaviors": "{b}": {x}' for x in probs]
        if p is None: missing.append(b); continue
        tools |= set(p['tools']); actions |= set(p['actions'])
    return tools, actions, missing, problems


def _tool_ok(t, declared):
    """A config tool {kind, name} is covered by a declared "kind:name"."""
    return f'{t.get("kind")}:{t.get("name")}' in declared


def warnings(c, behavior_file):
    """The --validate warnings about roles and autonomy of a normalized config."""
    b = c.get('behaviors')
    if not behavior_file or not isinstance(b, list) or not b: return []
    tools, actions, missing, w = union(b, behavior_file)
    if missing:
        w.append('"behaviors": ' + ', '.join(f'"{x}"' for x in missing) + ' nao declara(m) ferramentas e acoes (bloco '
                 '```permissions no BEHAVIOR.md): a autonomia e as ferramentas nao sao conferidas')
        return w
    names = ', '.join(b)
    for t in c.get('tools') or []:
        if isinstance(t, dict) and t.get('kind') and t.get('name') and not _tool_ok(t, tools):
            w.append(f'"tools": "{t["kind"]}:{t["name"]}" nao e usada por nenhum comportamento ligado ({names})')
    a = c.get('autonomy')
    for phrase in (a.get('can') or []) if isinstance(a, dict) and isinstance(a.get('can'), list) else []:
        if not isinstance(phrase, str): continue
        got = classify(phrase)
        if not got:
            w.append(f'"autonomy.can": "{phrase}" nao casa com nenhuma acao conhecida (permissions.ACTIONS): conferir a mao')
        elif got - actions:
            w.append(f'"autonomy.can": "{phrase}" da {", ".join(sorted(got - actions))}, que nenhum comportamento ligado '
                     f'declara ({names})')
    return w


def role(behavior, behavior_file):
    """The declared {'tools', 'actions'} of one behavior, or None (not declared, or no such behavior)."""
    f = behavior_file(behavior) if behavior_file else None
    return read(f)[0] if f else None


def role_allows(phrase, actions):
    """True when every action of the phrase is in the role's actions. A phrase that matches nothing stays (validate
    already warns about it) and reading is always allowed."""
    return not (classify(phrase) - set(actions) - {'read'})
