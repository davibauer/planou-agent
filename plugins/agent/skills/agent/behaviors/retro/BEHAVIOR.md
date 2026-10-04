---
title: Retro do projeto
summary: Participa da retro do projeto no Planou: contribuição, votação e ata.
layer: skill
kind: on_demand
when: a saida do == PLANOU traz -- CERIMONIA retro (contribuicao, votacao ou ata)
---
# retro: a retro do projeto no Planou (contribuição, votação e ata)

Vale para todo agente ligado ao Planou: quem convida é o Planou (Cerimônias do projeto), então não precisa ligar nada no
config. O evento chega no `== PLANOU` como `-- CERIMONIA`. `PL="env PYTHONPATH=$S/scripts python3 -m watch_core.planou
--agent <instância>"`. Substitui a weekly-retro do chief-of-staff para os agentes ligados ao Planou.

## Contribuição (`-- CERIMONIA retro #N de ABC: contribuicao ate HH:MM`)

1. `$PL retro dados <meeting_id>`: o período da retro e, em `planou`, os fatos que o Planou mediu nas tarefas do projeto
   no período, por funcionário e cada um com o PID: `rework` (ajustes pedidos e reaberturas), `blocked` (horas em
   Impedida ou Aguardando), `needs_you` (horas de cada pedido em Precisa de você), `cost` (custo por tarefa) e, em
   `agents`, os totais de cada um. Ao lado vêm os dados que o runner já guarda: ajustes pedidos no período, tarefas
   bloqueadas ou esperando na sua fila, pedidos ainda abertos com o usuário e as ações das retros anteriores com o
   resultado (`done`, `open`, `discarded`). `planou: null` (com `planou_aviso`) quer dizer Planou antigo ou fora do ar:
   siga só com os dados locais.
2. **Abra conferindo o efeito das ações anteriores, antes dos itens novos.** Cada `previous_actions[]` com `metric` traz
   o valor de partida (`baseline`), o do período novo (`current`) e o `effect` que o Planou mediu: `improved`, `same`,
   `worse` ou `unknown` (falta um dos dois valores; `null` antes da conferência). A linha do `-- CERIMONIA` já lista as
   que ficaram iguais ou pioraram. Ação `same` ou `worse` não volta igual: o item diz o que mudar na abordagem (com
   caso real) ou propõe deixar a ação cair. `improved` pode virar `went_well` com os números (`"rework 4 -> 1"`).
   `unknown` não prova nada: não comemore nem condene.
3. Prefira os casos medidos pelo Planou (valem para todos e o facilitador confere) e cite os números como estão
   (`"bloqueada 6 h"`, `"2 ajustes pedidos"`). Junte com o que só você sabe do período: o seu log e as lições, o que o
   usuário corrigiu.
4. Escreva de 2 a 6 itens, cada um de um tipo: `went_well` (o que funcionou), `to_improve` (o que atrapalhou) e
   `proposal` (o que mudar). **Regra anti-teatro:** toda proposta cita pelo menos um caso real, a tarefa com o que
   aconteceu nela (`{"pid": "PLN0131", "note": "bloqueada 6 h"}`, `"2 ajustes pedidos"`); sem caso, não é proposta.
   Não repita como nova uma ação anterior `open`: diga por que ela não andou.
5. Texto generalizado: sem nome de cliente, de pessoa, valor, credencial ou trecho de conversa. Só o que o funcionário
   aprendeu, como nas sugestões.
6. Mande uma vez (reenviar substitui) antes do prazo, com o custo da sua parte:

```bash
$PL retro contribuir <meeting_id> --text - <<'JSON'
{"items": [
  {"kind": "to_improve", "text": "Fiquei bloqueado esperando revisão.", "cases": [{"pid": "PLN0131", "note": "bloqueada 6 h"}]},
  {"kind": "proposal", "text": "Pedir revisão antes de abrir a PR.", "cases": [{"pid": "PLN0131", "note": "2 ajustes pedidos"}]}
], "cost_usd": 0.12}
JSON
```

`ENVIADA` fecha o assunto; nada a dizer ao usuário além de uma linha. `RECUSADA` diz o que corrigir (caso faltando,
PID que não existe, texto com cara de credencial): corrija e mande de novo. `FECHADA`: a rodada já fechou (prazo, teto
de custo ou todos mandaram); não mande mais. `NAO CONVIDADO`: nada a fazer.

## Votação (`-- CERIMONIA VOTO retro #N de ABC: votos ate HH:MM (ate N de M itens)`)

Só vem quando a retro tem votação, depois que a rodada de contribuições fecha.

1. `$PL retro ver <meeting_id>`: em `cached.vote.items` estão os itens votáveis (`to_improve` e `proposal` de todos),
   cada um com `id`, texto, casos e autor.
2. Escolha no máximo `votes_per_voter` itens: os de mais efeito real nos casos (mais retrabalho, mais tempo parado,
   mais custo), não os de texto mais bonito. Pode votar nos seus próprios itens. Nenhum convence: voto em branco.
3. Mande uma vez (reenviar substitui), só os ids, sem texto nem citação:

```bash
$PL retro votar <meeting_id> <item_id> <item_id>      # sem item_id = voto em branco
```

`VOTO ENVIADO` fecha o assunto. `AINDA NAO`: a votação ainda não abriu. `FECHADA`: já fechou. `SEM VOTACAO`: a reunião
não tem votos. `RECUSADA`: id que não veio no evento; confira em `retro ver`.

## Ata, quando você facilita (`-- CERIMONIA ATA retro #N de ABC (voce facilita ...)`)

1. `$PL retro ver <meeting_id>`: o estado e, em `cached.facilitate`, todas as contribuições (de agentes e, se ela
   contribui, da pessoa), quem faltou, as regras e as ações anteriores. Com votação, cada item traz `votes` (`null` em
   `went_well` ou sem votação) e `vote_closed_by` diz como a votação fechou.
   `$PL retro dados <meeting_id>` traz em `planou` os fatos do período de todos os funcionários (não só os seus):
   use para conferir os casos das contribuições e achar o que ninguém trouxe (o maior bloqueio, a tarefa mais cara).
   Em `planou.metrics` vêm os valores do projeto no período (`autonomy`, `rework`, `blocked_hours`,
   `cost_per_delivery`, com `deliveries` e `autonomy_min_samples`): são as métricas que as ações podem declarar.
2. **Abra a ata conferindo o efeito das ações anteriores** (`previous_actions[].effect` em `cached.facilitate`, com
   `metric`, `baseline` e `current`), antes de agrupar os itens novos, e diga no `summary` o que melhorou e o que não
   mexeu. Ação anterior `same` ou `worse` não volta igual: ou a ação nova muda a abordagem (diga no `detail` o que muda
   e por quê, com `repeat_of` dela), ou ela cai (diga em `decisions`). `unknown` não decide nada sozinho.
3. Agrupe os itens parecidos e escreva a ata: `summary` (o que a semana mostrou, em poucas frases), `decisions` (o que
   ficou combinado), no máximo **3** `actions` (título curto e acionável, `detail` opcional e os `cases` que sustentam
   a ação; escolha as com mais casos e mais efeito) e `lessons` (lição generalizada). Com votação, discuta primeiro os
   itens mais votados e tire as ações deles, mas a regra anti-teatro continua: item votado sem caso real não vira ação.
4. **Cada ação manda a `metric` que deveria mexer**, uma de `rules.metrics` (`autonomy`: parte das entregas sem toque
   humano; `rework`: ajustes pedidos e reaberturas; `blocked_hours`: horas em Impedida ou Aguardando;
   `cost_per_delivery`: custo por entrega). Escolha olhando `planou.metrics` de `retro dados`: a que o caso da ação
   mostra e que tem valor medido (`autonomy` null, com menos entregas que `autonomy_min_samples`, ou
   `cost_per_delivery` null, sem entrega, não servem de partida). Não mande valor de partida: o Planou mede no período
   da reunião e devolve `baseline`; a retro seguinte traz o efeito. Ação que não mexe em nenhuma das quatro vai sem
   `metric` (é opcional), mas prefira as que mexem.
5. Lição com `agent_id` (o `by.id` de uma contribuição com `by.kind` `agent`) vira proposta de papel: o Planou junta as lições de cada agente numa
   proposta só, que acrescenta a seção "Lições das retros" ao `instructions.md` dele quando a pessoa aprovar em Precisa
   de você. Dê `agent_id` só à lição que é do papel daquele agente e escreva como regra durável ("Peça revisão antes de
   abrir PR que muda tela"). Lição que é só registro vai sem `agent_id` ou com `"propose_role": false`. Não abra
   proposta de papel à parte (`POST /v1/agent/proposals`) para as mesmas lições: sairiam duplicadas.
6. Ação que repete uma anterior ainda `open`: mande `"repeat_of": "<action_id>"` (o Planou também marca sozinho pelo
   título). Prefira uma ação diferente, ou diga no `detail` por que a anterior não andou.
7. Mande antes do prazo da ata:

```bash
$PL retro ata <meeting_id> --text - <<'JSON'
{"summary": "Semana com um bloqueio longo por revisão.",
 "decisions": ["Revisão antes do ajuste"],
 "actions": [{"title": "Pedir revisão antes de abrir a PR", "detail": "Checklist curto.",
              "cases": [{"pid": "PLN0131", "note": "2 ajustes"}], "metric": "rework"}],
 "lessons": [{"text": "Peça revisão antes de abrir PR que muda tela.", "agent_id": "<by.id do agente>"},
             {"text": "A semana teve feriado; o volume menor não é tendência.", "propose_role": false}],
 "cost_usd": 0.08}
JSON
```

`ATA ENVIADA` lista os PIDs das ações (com a métrica e o valor de partida que o Planou mediu): elas nascem no Backlog do projeto e só andam quando o usuário aprovar. Diga ao
usuário em uma linha que a ata está em Cerimônias do projeto, com os PIDs. Não comece as ações e não as marque como
"o agente pode fazer". Nunca edite o `instructions.md` de um agente: a lição com `agent_id` já é a proposta.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido. A contribuição e a ata ficam no Planou; as ações da ata viram tarefas no backlog, que a pessoa aprova.

```permissions
tools: -
actions: planou.note, planou.task
```
