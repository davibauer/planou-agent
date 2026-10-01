---
title: Daily na página do dia
summary: Escreve a daily e as tarefas do dia na página do Notion.
layer: skill
kind: always
---
# daily-report: daily e tarefas na página do dia

Vale para a instância com os ganchos `daily` (o `== DAILY`) e `tarefas` (a seção `## <SIGLA>` da página do dia no
Notion). A biblioteca compartilhada `watch_core.daily` e o motor `watch_core.tasks` cuidam de formato, códigos,
publicação por diff, caixinhas e decisões; as regras completas estão em `~/.config/watch-core/docs/daily.md`. Este
texto diz o que a sessão faz. `A="python3 $S/scripts/agent.py <instância>"`.

Opções em `behavior_config.daily-report` (`$A --status` mostra):
- `language`: idioma da fala e dos blocos (`"pt"` ou `"en"`; padrão: `idioma_pagina` do config, senão `"pt"`). Em
  inglês a fala segue o estilo "On my side, ..." e os blocos "Yesterday / Today / Blockers / Pending / Waiting on".
- `closing_hour`: hora do fechamento que a sessão espera (padrão 18; a do gancho é a da biblioteca).
- `speech_only`: `true` (padrão) = o `--daily md` do fechamento é só a fala (um parágrafo); as listas o motor gera.
- `tasks`: `true` (padrão) quando o gancho `tarefas` gera a seção; `false` = a daily escreve as listas no texto.

## `== DAILY`: o fechamento

No primeiro tick a partir de `closing_hour` em dia útil o gancho imprime a evidência do dia. A sessão escreve a fala no
idioma de `language`, sem inflar (dia fraco: dizer), e grava:

```bash
$A --daily evidencia            # a matéria-prima: o que o agent viu hoje
$A --daily set "texto"          # grava a fala
$A --daily md "markdown"        # guarda o estruturado (com speech_only: só a fala)
$A --daily publicar             # reescreve a seção na página do dia
$A --daily                      # o que está gravado
```

Depois de gravada, o tick republica sozinho quando a seção muda (rascunho vivo). Antes de publicar à mão, ler a
seção atual e substituir o que está sob `## <SIGLA>`, mesmo texto de outra sessão (duas sessões inserindo = fala
duplicada). Mudança de formato da daily vale para todas as instâncias no mesmo dia.

## Tarefas na página do dia (`== TAREFAS`)

O estado da instância é a única fonte: ações `aN`, pendências `pN` e as linhas extras das fontes. Cada tarefa ganha id
fixo `<SIGLA>-N` (nunca renumerado); nas mensagens ao usuário, citar pelo `<SIGLA>-N`.
- **Trabalho do dia que não é ação nem pendência** (sessão, investigação, apply): `$A --acao add "texto"` e
  `$A --acao done N "<evidência>"`, senão some do Feito hoje e do Ontem. No fechamento, o `-- SEM TAREFA` lista o que
  o motor achou sem tarefa: cadastrar antes da fala.
- **Decisão sempre com opções e a recomendada**: `--acao add "..." balde=decisao` e as opções `A*: ...` / `B: ...`
  (`*` = recomendada) pelo motor de tarefas (`--opcoes`). Opção marcada na página chega como `-- DECISAO ...`: agir e
  fechar com `$A --acao done N "<opção>"`.
- **Baixa por evidência**: ação cujo feito dá para checar por comando ganha um verificador (`--verifica`) no mesmo
  turno em que nasce; quando a fonte do tick mostrar que uma ação foi cumprida, `--acao done` na hora, conferindo a
  fonte primária antes. Caixinha marcada na página = baixa no tick seguinte.
- `-- ⏰ ... RASCUNHO DE COBRANCA`: aguardando parado há dias úteis demais; escrever um rascunho curto de cobrança
  (formato de rascunho do `work-watch`), sem enviar.
- `-- ≈ X parece Y`: duplicadas prováveis; conferir e juntar (`--junta`) só se forem a mesma coisa.
- `-- TOP do dia nao definido`: escolher até 3 tarefas que mais importam e menos podem esperar (`--top`, ou `--top -`).
- Plano do dia, backlog, prazos, título curto e link: comandos do motor (`--hoje`, `--prazo`, `--titulo`, `--link`,
  `--detalhe`, `--id`), descritos em `watch_core/tasks.py`.

As fontes extras da instância (PRs, cards, issues que entram na lista e em que coluna) e o que a instância precisa do
usuário (`v-*`) são opções do gancho `tarefas` no config e adapters da instância, não texto de instruções.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido.

```permissions
tools: -
actions: notion
```
