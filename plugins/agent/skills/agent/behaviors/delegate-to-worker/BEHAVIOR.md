---
name: delegate-to-worker
description: "Passa a um worker em background os pedidos de várias rodadas: implementar, corrigir, abrir PR. Sempre ativa na instância que a liga em \"behaviors\"."
title: Entrega por um worker
summary: Passa a um worker em background os pedidos de várias rodadas: implementar, corrigir, abrir PR.
layer: skill
kind: always
---
# delegate-to-worker: entregar por um worker

Quando um pedido exige várias rodadas de leitura, edição ou teste (implementar, corrigir, abrir PR, investigar a
fundo), a sessão não faz: delega ao subagent `worker` em background e fica livre para o runner e para o usuário.

## O pedido ao worker

Montar com tudo que ele precisa, sem ele ter de perguntar. O que é do projeto sai do config, não deste texto:
`$A --brief [repo]` (`A="python3 $S/scripts/agent.py <instância>"`, `repo` pelo `name` ou pela pasta; sem ele, o
primeiro de `repos`) imprime o bloco pronto, para colar no pedido: repositório, worktree e branch base, conta do
GitHub, arquivos de regras, mapa, arquivos prováveis, modelo, economia, testes filtrados, modo de release, critério de
pronto, autonomia e o formato da volta. **Sem `repos` no config** (agente criado na tela Time só com o papel), o
repositório vem da tarefa: `$A --brief <pr_url ou link do repositório>` clona em `~/src/<nome>` com o gh já logado (ou
reaproveita o clone que já está lá, se for o mesmo repositório; só GitHub, com o dono na conta do gh da instância, nas
organizações dela ou em `repo_discovery.allowed_owners` do config, senão a saída é a linha `PERGUNTAR`), lê as regras e grava o que achou no estado da
instância (`data/discovered.json`, por `scripts/repo_discovery.py`), e o bloco sai com a linha `DESCOBERTO`; as
tarefas seguintes usam o mesmo clone pelo `--brief` de sempre. Sem link nenhum na tarefa nem no projeto:
`printf '%s' "Qual é o repositório de <PID>? (link)" | $PL fila blocked <PID> --note -`. O `repos` do config, quando
existe, sempre vence. A autonomia do bloco é só a do papel: do `can`, fica em PODE o que as ações
deste comportamento cobrem (abaixo); o resto vai para a linha "NAO E DESTE PAPEL" (ex.: deploy, que é do integrador).

- **Pedido literal** do usuário (ou da tarefa da fila: título e descrição de `fila ver`) e o que a sessão já sabe:
  links, logs, decisões da conversa. Vai em cima do bloco do `--brief`.
- **Arquivos prováveis**: os arquivos e as funções que a mudança toca, na linha `ARQUIVOS PROVAVEIS` do bloco
  (`--files "<arquivos>"`, ou trocar o texto de exemplo). A sessão já sabe pela conversa, ou acha pelo mapa do
  repositório (a linha `MAPA`: `map` do repositório em `repos`, ou `docs/MAP.md` quando o repositório tem; no Planou, o
  mapa está no `CLAUDE.md` das regras) ou por uma busca. Sem isso o worker gasta rodadas se localizando: em 30/09 foram
  33 buscas de 81 rodadas numa entrega só.
- **Modelo**: `sonnet` ou `default`, na linha `MODELO` (`--model sonnet|default`). `sonnet` para mecânica (renomear,
  mover, trocar um valor, seguir um padrão que já existe), documentação e limpeza; `default` para desenho (algo novo,
  mais de um jeito de fazer) e mudança de contrato (API, formato de arquivo ou de config, o que outro agent ou programa
  lê). Na dúvida, `default`. `sonnet` vai no `Agent(worker, model: "sonnet")`; `default` não passa `model` (vale o do
  worker, que herda o da sessão).
- **Onde**: `path` do repositório em `repos`, a worktree em `<worktrees>/<nome-curto>` numa branch própria a partir
  de `base` (padrão `main`). Conta do GitHub: `gh_account` do repositório e o `env` da ferramenta `gh` em `tools`.
- **Regras**: o arquivo `rules` do repositório (ex.: `CLAUDE.md`), os `shared_rules` (regras comuns a vários
  repositórios) e a autonomia (as listas `can`, `ask_first`, `never` do config e a `autonomy` da tarefa, a mais
  restrita).
- **Testes**: `tests.filtered` do repositório, só o que a mudança afeta. `tests.full` é do integrador (batch-release).
- **Critério de pronto**, nesta ordem: o que o `instructions.md` da instância define para o repositório; senão o
  `done` do repositório; senão o padrão do `release` (`batch`: branch com rebase e push, testes filtrados verdes e
  `PRONTA PARA RELEASE: <branch>`; `pr`: PR com a CI verde, merge só se a autonomia deixar; `ci`: PR aberta com o
  rótulo `automerge` e os testes filtrados verdes, e o worker volta na hora; `none`: PR em rascunho com os testes
  filtrados verdes). Sem `release`, o repositório que é o `repo` do `batch-release` conta como `batch`. O `--brief` já traz o do config.
- **Formato da volta**: RESULTADO, O QUE MUDOU, LINKS, VALIDAÇÃO, PENDENTE DO USUÁRIO, RASCUNHOS, ESTADO SUGERIDO.

**Release pela CI** (`"release": "ci"`, PLN0259): o worker não espera a CI (em 30/09, até 54% do tempo de um worker
era espera da CI para merge, tag e publicação, e PRs em paralelo brigavam pelo número da versão). Ele abre a PR com
`gh pr create --label automerge`, título em Conventional Commits (o tipo define a versão: `feat` = minor, o resto =
patch) e o texto do changelog no `fragments` do repositório, sem mexer em versão nem em seção de versão do CHANGELOG,
e volta. A CI faz o merge (squash) quando fica verde, a versão, o CHANGELOG, a tag e as checagens do
`publish-plugin --check`; se algo falha, nada é publicado e chega um alerta no Planou com o link do run. A cópia em uso
e o arquivo de versões são atualizados pelo gancho `tag_pull` do runner ao ver a tag nova: o `== TAG NOVA` fecha a
tarefa (fila `done` quando estava em `in_review` esperando a publicação) e a sessão diz ao usuário quais runners
precisam ser relançados. `gh pr merge --auto` não serve: sem checagem obrigatória na main, ele faz o merge na hora.

Projeto novo com o mesmo jeito de trabalhar: uma entrada nova em `repos`, sem código novo (passo a passo em
`docs/dev-template.md` do plugin).

## Rodadas

- Uma entrega por worker. Dois workers em paralelo só quando não mexem nos mesmos arquivos.
- Pedido novo que chega com um worker rodando vai para a próxima rodada, não para o worker em andamento.
- Economia: testes filtrados e saída resumida (`| tail -n 20`); suíte completa só quem integra. O worker agrupa
  leituras e comandos independentes na mesma rodada e nunca lê arquivo grande inteiro (`grep -n` e `sed -n` do
  trecho): a linha `ECONOMIA` do `--brief` diz isso a ele.

## O que o worker nunca faz

- Mandar mensagem a pessoas (Slack, Teams, e-mail): volta como rascunho, e quem sobe é a sessão.
- Gravar estado do agent (pendências, lições, tarefas): volta como sugestão, e a sessão aplica.
- O que não está em `can`: merge, deploy, apagar dados, mexer em produção. Deixa pronto e volta como decisão pendente.

## Ao delegar

Logo depois do `Agent(worker)`, registrar o início no Planou: `$PL fila worker-start <PID> --role dev --label '<o que
ele faz, uma linha>'` (sem tarefa do Planou: `$PL worker start --role other --label '...'`). A saída é a key: guardar
na conversa, junto com o `output_file` que o `Agent` devolveu (o registro do worker; nunca ler esse arquivo na sessão).
Sem o `output_file` à mão, o `agentId` do worker serve. Retrabalho do mesmo worker ainda em andamento (continuado por mensagem) reusa a key; worker novo abre outra.

## Quando ele volta

Primeiro, com uma tarefa do Planou: `$PL fila worker <PID> --role dev --tokens <N> --steps <ferramentas> --duration-ms
<ms> --result feito|parcial|falhou --key <key> --phases-from <output_file>` (fecha o worker no Planou e manda o tempo
por fase dele, medido pelo registro: modelo, testes, CI, git/publish, leitura, edição, espera; registro que não
aparece só gera um aviso e a entrega vai sem as fases; sem tarefa: `$PL worker end <key> --result
feito|parcial|falhou`) com o uso que chegou com a volta do worker (tokens do subagente, ferramentas usadas,
duração), qualquer que seja o resultado (task-queue). Depois, contar ao usuário em até três linhas (o que ficou
pronto, link, o que falta dele). Tarefa da fila: seguir o
`task-queue` (`fila in_review` ou `fila blocked`). Rascunho que ele trouxe: seguir a regra de rascunhos do núcleo.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido. `pr.merge`, `tag` e `publish` só no release por PR (`"release": "pr"`) e se a autonomia deixar; no release em lote são do integrador; no release pela CI (`"release": "ci"`) são da CI, e o worker só abre a PR.

```permissions
tools: subagent:worker, cli:gh, cli:publish-plugin
actions: worktree, code, test.filtered, push, pr.open, pr.merge, tag, publish
```
