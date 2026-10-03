---
title: Refinamento do backlog
summary: Participa do refinamento do backlog no Planou com sugestões e a lista para aprovar.
layer: skill
kind: on_demand
when: a saida do == PLANOU traz -- CERIMONIA refinement
---
# refinement: o refinamento do backlog no Planou (sugestões e lista para aprovar)

Vale para todo agente ligado ao Planou: quem convida é o Planou (Cerimônias do projeto, "Refinamento do backlog"),
então não precisa ligar nada no config. O evento chega no `== PLANOU` como `-- CERIMONIA refinement`.
`PL="env PYTHONPATH=$S/scripts python3 -m watch_core.planou --agent <instância>"`. É diferente da daily, que lê a
fila: o refinamento lê o Backlog do projeto (tarefas ainda não passadas a ninguém) e só sugere; nada muda na tarefa
antes de o usuário aprovar na tela.

## Sugestões (`-- CERIMONIA refinement #N de ABC: sugestoes ate HH:MM`)

1. `$PL refino ver <meeting_id>`: a reunião e, em `cached`, o evento com `backlog` (até 30 tarefas: as sem estimativa
   primeiro, depois por prioridade e idade, cada uma com `pid`, `title`, `description`, `priority`, `kind` (`task` ou
   `epic`), `epic` (o PID do épico pai, se houver), `estimate_h` e `agent_can_do`), `backlog_total` e `rules` (`max_suggestions`, `max_parts`, `max_estimate_h`).
2. Para cada tarefa, só onde houver motivo real, uma sugestão de cada tipo no máximo:
   - `estimate`: `estimate_h` em horas (maior que 0). Estime pelo que a tarefa pede e pelo que você já fez parecido;
     não mexa numa estimativa que já existe sem dizer por que ela está errada.
     Tarefa que muda tela com o desenho em aberto, com o comportamento `prototype` ligado, conta também o protótipo antes do dev.
   - `split`: de 2 a `max_parts` partes (`title`, `detail` opcional, `estimate_h` opcional) quando a tarefa junta
     entregas que andam sozinhas. Não quebre o que cabe num dia de trabalho.
   - `agent_can_do`: `true` quando a tarefa está clara o bastante para um agente começar sozinho (o que fazer e como
     saber que acabou); `false` para tirar o selo de uma que depende de decisão do usuário. Nunca numa tarefa de `kind` `epic`.
3. Todo `reason` diz o motivo em uma frase, a partir da tarefa (`"a descrição já diz a tela e o critério de
   pronto"`), sem nome de cliente, de pessoa, valor ou credencial.
4. Mande uma vez (reenviar substitui) antes do prazo, com o custo da sua parte; `[]` quando nada precisa mudar:

```bash
$PL refino sugerir <meeting_id> --text - <<'JSON'
{"cost_usd": 0.04, "suggestions": [
  {"kind": "estimate", "pid": "ABC0012", "reason": "igual ao filtro de datas, que levou 5 h", "estimate_h": 6},
  {"kind": "agent_can_do", "pid": "ABC0012", "reason": "a descrição já diz a tela e o critério de pronto", "agent_can_do": true},
  {"kind": "split", "pid": "ABC0015", "reason": "junta API e tela, que andam sozinhas",
   "parts": [{"title": "Rota de exportação", "estimate_h": 4}, {"title": "Botão Exportar na lista", "estimate_h": 2}]}
]}
JSON
```

`RECUSADA` diz o campo errado (tarefa fora do backlog, estimativa fora do limite, quebra com uma parte): corrija e mande
de novo. `FECHADA` quer dizer que a rodada já fechou: nada a mandar.

## Lista final (`-- CERIMONIA LISTA refinement #N de ABC (voce facilita ...)`)

1. `$PL refino ver <meeting_id>`: em `cached`, as sugestões de cada convidado (`contributions`), quem faltou
   (`missing`) e o backlog.
2. Junte numa lista só, uma sugestão de cada tipo por tarefa: quando duas estimativas divergem, fique com a de melhor
   motivo e diga a outra no `reason`; descarte o que não tem motivo real. Quebra e selo só com o motivo de quem sugeriu.
3. `summary` em uma ou duas frases (o que o backlog precisa); `decisions` opcional. Não mande `actions` nem
   `lessons`: o refinamento não cria tarefa sozinho.
4. Mande antes do prazo:

```bash
$PL refino lista <meeting_id> --text - <<'JSON'
{"summary": "Backlog com 3 tarefas sem estimativa; uma grande demais.", "decisions": [], "cost_usd": 0.06,
 "suggestions": [ ... as mesmas chaves do refino sugerir ... ]}
JSON
```

`LISTA ENVIADA` diz quantas sugestões esperam o usuário em Cerimônias. Diga isso ao usuário em uma linha. Não mude a
estimativa, não quebre e não marque o selo por conta própria: quem aplica é a aprovação na tela (a quebra cria as
partes no Backlog).

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido. As sugestões e a lista ficam no Planou; o que muda nas tarefas é o usuário que aprova.

```permissions
tools: -
actions: planou.note
```
