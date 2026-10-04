---
name: daily
description: "Explica na daily do projeto no Planou, em uma linha cada, as tarefas Impedidas do agente. Use quando a saida do == PLANOU traz -- CERIMONIA daily."
title: Daily do projeto
summary: Explica na daily do projeto no Planou, em uma linha cada, as tarefas Impedidas do agente.
layer: skill
kind: on_demand
when: a saida do == PLANOU traz -- CERIMONIA daily
---
# daily: a daily do projeto no Planou (explicar o que está Impedido)

Vale para todo agente ligado ao Planou: quem convida é o Planou (Cerimônias do projeto, card "Daily"), então não
precisa ligar nada no config; o runner já declara a capacidade `ceremony_daily` no heartbeat. O evento chega no
`== PLANOU` como `-- CERIMONIA daily`. `PL="env PYTHONPATH=$S/scripts python3 -m watch_core.planou --agent <instância>"`.

A ata da daily o Planou monta sozinho, a partir dos dados (o que andou, o que concluiu, o que travou, o custo, os
deploys). O agente só é chamado para uma coisa: explicar, em uma linha, cada tarefa dele que está **Impedida**.
Aguardando e "Bloqueada por" já têm o porquê nos dados e não chamam ninguém. É diferente da `daily-standup` (a daily do
próprio agent, na página dele) e do refinamento (que lê o Backlog).

## Explicar (`-- CERIMONIA daily #N de ABC: explicar N tarefas Impedidas ate HH:MM`)

O bloco já traz uma linha por tarefa: `PID título (horas impedida; nota: <nota do bloqueio>; pedido d-81; bloqueada
por ABC0040)`. Com mais de 12 tarefas, ou para ver o evento inteiro, `$PL daily ver <meeting_id>` (em `cached.started`:
`blocked` com `pid`, `title`, `since`, `hours`, `reason`, `blocked_by`, `ask_code`, e `text_max`).

1. Escreva **uma linha por tarefa** do bloco, a partir do que ele traz: a nota do bloqueio, o pedido aberto em Precisa
   de você e as tarefas de que ela depende. Diga o que falta e quem destrava (`"Esperando a chave de teste; pedi em
   Precisa de você (d-81)."`, `"Depende de ABC0040, que está em revisão."`). Se o bloqueio já caiu, diga o que vai
   fazer agora (`"A chave chegou; volto para ela hoje."`) e mova a tarefa na fila depois.
2. Sem modelo caro e sem worker: é a própria sessão que escreve, sem pesquisa, sem abrir repositório. Uma explicação
   custa centavos; se precisar investigar, explique o que se sabe e investigue depois, fora da daily.
3. Até `text_max` caracteres (300), uma linha (quebras viram espaço). Sem credencial (o Planou recusa), sem nome de
   pessoa nem trecho de conversa. Com `confidentiality: minimum` (o bloco avisa e não mostra título nem nota), texto
   neutro: o tipo de espera e o código do pedido (`"Esperando resposta da pessoa no pedido d-81."`), nunca nome,
   assunto ou dado de cliente.
4. Mande **tudo numa chamada só**, antes do prazo, com o custo e os tokens do turno (o que o turno gastou até aqui,
   como na retro; sem número confiável, deixe de fora). Reenviar substitui a anterior inteira, e quando você é o último
   chamado a daily fecha na hora: nunca mande uma tarefa por chamada.

```bash
$PL daily explicar <meeting_id> --text - <<'JSON'
{"cost_usd": 0.01, "tokens": 1800, "explanations": [
  {"pid": "ABC0131", "text": "Esperando a chave de teste; pedi em Precisa de você (d-81)."},
  {"pid": "ABC0135", "text": "Depende de ABC0040, que está em revisão; volto quando ela entrar."}
]}
JSON
```

O comando confere antes de mandar: só os PIDs do evento, um por tarefa, de 1 a `text_max` caracteres. Tarefa do
evento sem explicação dá `AVISO` (ela fica "sem explicação do agente" na ata). `--cost-usd N` e `--tokens N` valem
quando o JSON não traz os dois.

`ENVIADAS: N explicacoes (...; reuniao done)` quer dizer que a daily fechou com a sua parte. `RECUSADA` diz o que
corrigir (PID fora do evento, texto vazio ou longo, credencial): corrija e mande de novo, tudo junto. `FECHADA`: a
daily já fechou (prazo, teto ou a pessoa fechou); nada a mandar. Não precisa avisar o usuário: a ata está em
Cerimônias do projeto.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido. As explicações ficam na ata da daily no Planou; nada muda nas tarefas.

```permissions
tools: -
actions: planou.note
```
