---
name: tech-radar
description: "Acompanha as novidades das stacks do time e devolve como sugestão, nunca como código. Sempre ativa na instância que a liga em \"behaviors\"."
title: Radar técnico
summary: Acompanha as novidades das stacks do time e devolve como sugestão, nunca como código.
layer: skill
kind: always
---
# tech-radar: novidades das stacks viram sugestões

O agente de radar técnico (instância `tech-scout`) acompanha o que sai de novo nas stacks do time e devolve isso como
sugestão, nunca como código. Vale com `task-queue` ligado. `PL` é o do `task-queue`; `S` é a pasta da skill;
`R="python3 $S/scripts/radar.py <instância>"`; `BL="python3 $S/scripts/backlog.py <instância>"`.

## Fontes (uma por família, no `sources` do config)

- `radar_deps`: notas de versão das dependências dos repositórios do config (`package.json`, `*.csproj`,
  `Directory.Packages.props`, `requirements*.txt`, `pyproject.toml`); versão nova minor ou major no npm, NuGet ou PyPI.
  Só os repositórios próprios; `client_repos` fica vazio até a pessoa listar.
- `radar_feeds`: blogs oficiais e canais do YouTube escolhidos pela pessoa, por RSS ou Atom.
- `radar_hn` e `radar_trending`: Hacker News e GitHub Trending da semana, só o que cita uma das `stacks`.

Cada fonte roda uma vez por semana (depois de `weekday` e `hour`, hora da instância), guarda o que já viu (a mesma
notícia não volta) e o que achou em `data/radar/<semana>.json`. O tick traz `== RADAR <fonte> <semana> (n novas)` com
uma linha por item. Buscas externas genéricas: nome de pacote, URL pública de feed e termos das stacks; nunca nome de
cliente, caminho de repositório ou dado de uma tarefa.

Opções em `behavior_config.tech-radar`:
- `stacks`: os termos das stacks (`"EF Core|Entity Framework"` = alternativas). Sem stacks, HN e Trending não rodam.
- `project`: o projeto do modo projeto (ex.: `RADAR`); vazio desliga o modo projeto.
- `column`: o nome da coluna do modo coluna (padrão "Radar técnico").
- `per_task`: sugestões por tarefa no modo coluna (padrão 3, no máximo 3).
- `max_hours`: prazo máximo de uma tarefa na coluna, em horas (padrão 24).
- `max_tasks`: sugestões por semana no modo projeto (padrão 8).
- `weekday` (0 = segunda) e `hour`: quando a varredura da semana roda (padrão segunda, 9 h).
- `window_days`: janela dos feeds e do HN, em dias (padrão 7).

## Modo projeto: a varredura da semana vira Backlog

Quando chega `== RADAR ...` (uma vez por semana):
1. `$R`: junta os itens da semana, ordena (versão major e item que cita mais stacks primeiro), escreve o plano em
   `data/radar/plan-<semana>.json` (formato do `backlog.py`) e mostra a prévia (`backlog.py --dry`).
2. Revisar o plano: tirar o que não interessa ao time, reescrever o `why` com o que muda para o projeto (pode ler as
   notas de versão e o post, só links públicos). Uma tarefa por sugestão, título "Avaliar ...", critério de pronto "a
   decisão anotada: adotar, esperar ou ignorar", P3 para versão major, P4 para o resto. Sem selo `agent_can_do`: quem
   decide é a pessoa, e a vaga livre do Planou puxaria a tarefa para este agente, que não escreve código.
3. `$BL <plano> --max <n>` (a linha `PUBLICAR:` do passo 1 já traz o comando). Tudo nasce no Backlog do `project`.
   Em modo teste (sem `"live": true`) só com `--dry`.
4. Uma linha ao usuário: quantas sugestões e o RESUMO. Semana sem novidade: nada a dizer.

## Modo coluna: até 3 sugestões por tarefa

A coluna `column` de um projeto tem este agente como dono e "Ao terminar, vai para" o próximo passo da esteira (ex.:
Refinamento, do `backlog-planning`, depois Radar técnico, depois A fazer, do dev). A tarefa que entra nela chega como
`-- FILA LIBERADA <PID>`:
1. `fila ver <PID>` e `fila started <PID> --estimate-h 0.3`.
2. `$R --match -` com o título e a descrição no stdin: devolve até `per_task` itens das últimas 4 semanas que cabem na
   tarefa (stacks em comum e palavras em comum). Guardado vazio: `$R --match - --dry` varre agora, sem gravar.
3. Escolher no máximo `per_task` que ajudam de verdade a fazer a tarefa (uma versão nova que resolve o problema, uma
   biblioteca, um post com o jeito certo). Nenhum serve: "Radar técnico: nada relevante para esta tarefa".
4. `printf '%s' "<nota>" | $PL fila done <PID> --note -`: a nota tem uma linha por sugestão (o que é, por que ajuda,
   link). O `done` do dono da coluna vira handoff (`PASSOU: ...`) e a tarefa segue para o próximo estado.
- **Prazo máximo:** a tarefa não fica na coluna mais que `max_hours`. Passou do prazo (a sessão caiu, a rede não
  respondeu), passar adiante com o que tiver, ou com "Radar técnico: sem sugestão no prazo". Nunca travar a esteira
  esperando uma fonte.
- O modo coluna não cria tarefa: a sugestão fica na nota da tarefa, e quem decide adotar é quem faz a tarefa.

## O que o tech-scout não faz

- Não escreve código, não abre branch nem PR, não delega worker de código (sem `fila worker`).
- Não lê repositório de cliente sem a pessoa listar em `client_repos`, e não põe nome de cliente, caminho de
  repositório ou texto de tarefa numa busca externa.
- Não cria o projeto RADAR nem a coluna: é decisão da pessoa (passo a passo em `docs/tech-radar.md`).
- Não publica fora do Planou (e-mail, chat): a sugestão vive na tarefa.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido.

```permissions
tools: -
actions: planou.task, planou.queue, planou.note
```
