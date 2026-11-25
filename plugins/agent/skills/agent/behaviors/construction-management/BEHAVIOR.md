---
name: construction-management
description: "Gestão de obra: mantém as etapas ativas e próximas da obra como tarefas no Planou, com dependência entre elas, avisa quando um prazo de contrato ou e-mail vence ou passa, põe as parcelas a pagar do banco de finanças como tarefas, acompanha a pendência com fornecedor (cobrança vira rascunho, nunca envia) e manda o resumo semanal do que andou, travou e vence. Sempre ativa na instância que a liga em \"behaviors\"."
title: Gestão de obra
summary: Mantém as etapas da obra como tarefas com dependência, avisa prazos e parcelas a vencer, acompanha pendências com fornecedor (cobrança só em rascunho) e manda o resumo semanal.
layer: skill
kind: always
---
# construction-management: gestão de obra (etapas, prazos, parcelas, pendências, resumo semanal)

O papel de gerente de uma instância dedicada a uma obra. O assistente (`construction-advisor`) analisa e responde:
orçamento, pergunta de fornecedor, rascunho de resposta e resumo do dia. A gestão (este comportamento) acompanha a obra
no tempo: etapas, prazos, parcelas, pendências com fornecedor e o resumo semanal. Os dois costumam andar juntos na
mesma instância e usam as mesmas fontes; o que é de uma obra só fica na instância (opções abaixo e `adapters/`), nunca
aqui. `I` é a pasta da instância (`~/.config/agent/<instância>`); `S` é a pasta da skill;
`A="python3 $S/scripts/agent.py <instância>"`; `PL` é o do núcleo (SKILL.md, "Planou pela linha de comando");
`BL="python3 $S/scripts/backlog.py <instância>"`. Vale com `task-queue` ligado e o bloco `planou` no config.
**Nunca mande nada a fornecedor, profissional ou qualquer pessoa.**

## Princípios

- **Poucas tarefas e boas.** Uma tarefa por etapa ativa ou próxima, nunca o cronograma inteiro de uma vez. Aviso só
  informativo não vira tarefa: vai para o resumo.
- **Título curto, nas palavras do agente.** Sem valor e sem nome de fornecedor: "Pagar parcela 3 de 10 da estrutura,
  vence 12/12", não o valor nem a empresa.
- **Nada de valor nem de fornecedor no plano.** O plano do `backlog.py` sobe título, descrição (`what`, `why`,
  `details`) e `done_when` ao Planou também em `confidentiality: title` (só o Mínimo recusa o plano). Neles a fonte vai
  genérica ("contrato da estrutura, cláusula 4", "e-mail de 01/12", "banco de finanças"); valor, conta, nome do
  fornecedor e a fonte completa ficam nos arquivos JSON da instância e no resumo da sessão.
- **Data só de fonte.** Prazo e data vêm de contrato, e-mail, conversa ou banco, sempre com a origem (arquivo e
  cláusula, e-mail com a data, linha do banco). Sem fonte fica "a confirmar", nunca estimado.
- **Nunca enviar.** Cobrança de fornecedor vira rascunho para o usuário aprovar e enviar.

## Opções

Em `behavior_config.construction-management` (`$A --status` mostra). As quatro primeiras são as mesmas do
`construction-advisor`, com o mesmo sentido; ausente aqui, vale a do `construction-advisor` da mesma instância (não
precisa repetir):
- `price_history`, `quote_history`, `quantities`: os comandos da instância que mostram o preço pago, os orçamentos
  recebidos e as quantidades do modelo. A regra do assistente vale igual: o que se acrescenta é só uma palavra-chave
  curta escolhida pela sessão, entre aspas simples, nunca texto de fora. Só no computador.
- `references_dir`: a pasta das referências públicas (SINAPI, CUB, INCC). Só no computador.
- `installments`: o comando da instância que imprime, em JSON, a lista das parcelas a pagar que ainda não venceram ou
  que venceram sem pagamento, uma por parcela: `{"title": "Parcela 3 de 10 da estrutura", "date": "AAAA-MM-DD",
  "source": "banco de finanças", "number": 3, "total": 10, "amount": 1234.5, "paid": false}` (`title`, `date` e
  `source` obrigatórios; o resto ajuda a montar a tarefa e o resumo). Assim a saída vai direto para a ferramenta
  (`<installments> | python3 $S/scripts/construction_dates.py -`). Roda como está, sem nada acrescentado. Só no
  computador.
- `stages_file`: o arquivo JSON das etapas da obra (padrão `$I/data/etapas.json`), formato abaixo. A sessão lê e
  atualiza. Só no computador.
- `warn_days`: com quantos dias de antecedência um prazo ou uma parcela vira aviso ou tarefa (padrão 7).
- `followup_days`: dias sem resposta a partir dos quais uma pendência com fornecedor vira cobrança em rascunho
  (padrão 5).
- `report_weekday`: dia do resumo semanal, 0 = segunda (padrão 4, sexta); `report_hour`: hora do resumo (padrão 17).

Comando sem opção configurada (nem aqui nem no assistente): dizer "parcelas não consultadas" (ou o que faltou) no
resumo em vez de estimar, e sugerir ao usuário configurar a opção.

## Etapas da obra

O arquivo `stages_file` guarda o cronograma inteiro, só no computador:

```json
{"stages": [
  {"code": "fundacao", "title": "Fundação", "status": "done", "started": "2026-08-03", "finished": "2026-09-18"},
  {"code": "estrutura", "title": "Estrutura", "status": "active", "depends_on": ["fundacao"],
   "end": "2026-12-20", "source": "contrato da estrutura, cláusula 4 (prazo de 90 dias)", "started": "2026-09-21",
   "done_when": ["laje de cobertura concretada", "medição final aprovada"]},
  {"code": "alvenaria", "title": "Alvenaria", "status": "todo", "depends_on": ["estrutura"]}
]}
```

- `code`: curto, minúsculas, números e hífen (é o `code` da tarefa no plano). `status`: `todo`, `active` ou `done`.
- `start` e `end` (AAAA-MM-DD): só com `source`. Sem fonte, não escrever a data: a etapa fica "a confirmar".
- `started` e `finished`: o dia real em que a etapa começou e terminou (medição, e-mail, foto, o usuário disse). São o
  "andou" do resumo semanal.
- `depends_on`: só a dependência real (uma etapa precisa da outra pronta para começar).
- O arquivo nasce do contrato e do cronograma que a obra já tem (construtora, projetista, o usuário). Sem cronograma
  com fonte, perguntar ao usuário quais etapas estão em andamento e quais vêm depois; nunca montar o cronograma de
  cabeça.

**Quais viram tarefa:** cada etapa `active` e cada etapa `todo` cujas dependências estão todas `done` ou `active` (a
próxima). O resto do cronograma fica só no arquivo. Uma etapa que começa ou termina muda o conjunto: atualizar o
arquivo (com `started`/`finished` e a fonte) e publicar de novo.

**Como publicar:** o plano do `backlog.py` (formato no topo de `$S/scripts/backlog.py`), com a meta `obra-etapas`,
gravado em `$I/data/plano-etapas.json`:
- uma tarefa por etapa do conjunto: `code` e `title` da etapa; `what` com o início e o fim e a fonte genérica de cada
  data (ou "a confirmar"); `why` com o que ela libera (as etapas que dependem dela); `done_when` com o `done_when` da etapa ou
  "etapa concluída e conferida pelo usuário" (o plano recusa critério vazio); `depends_on` com o `code` das etapas do
  conjunto de que ela depende (dependência já `done` não entra);
- `"agent_can_do": false` em todas: quem faz a etapa é a obra, não um agente; `"epic": false`;
- `ready_reason` só para a etapa `active` ("etapa em andamento na obra, <fonte>"): num projeto autônomo ela nasce em A
  fazer; o resto fica no Backlog até a pessoa mover.

`$BL $I/data/plano-etapas.json --dry` confere; depois `$BL $I/data/plano-etapas.json`. Etapa que já tem tarefa sai
`JA EXISTE`; mudou a data ou a dependência: `--update`. O plano manda o `blocked_by` (o Planou mostra que uma espera a
outra). Quem conclui a tarefa da etapa é o usuário: etapa `done` no arquivo com a tarefa ainda aberta vai para o
resumo ("Estrutura terminou em 18/12 pela medição: concluir a tarefa?").

**Data na tarefa:** o plano ainda não leva data de vencimento. A data com fonte vai no título quando é o que importa
("Estrutura, prazo 20/12") e sempre na descrição, com a origem.

## Prazos

Prazo de contrato, proposta, e-mail ou conversa (entrega de material, fim de etapa, validade de orçamento, vistoria,
resposta prometida): guardar em `$I/data/prazos.json`, um por linha da lista, `{"code", "title", "date", "source"}`,
com a fonte sempre (arquivo e cláusula, e-mail com a data). Sem data clara na fonte: `"date": null`, "a confirmar".

A cada tick com novidade e na manhã de cada dia (primeiro tick depois das 8 h):
- vence em até `warn_days` dias ou já passou: `$PL alert --title "<o que vence> vence em <N> dias" --severity medium
  --assunto prazo-<code>` (passou: `--title "<o que venceu> venceu há <N> dias" --severity high`). O mesmo
  `--assunto` substitui o aviso aberto, sem acumular. Título sem valor nem fornecedor.
- prazo que pede uma ação do usuário (assinar, escolher, pagar, liberar a frente): além do aviso, uma tarefa curta
  pelo mesmo plano das parcelas (abaixo), meta `obra-prazos-<AAAA-MM>`.
- o resto vai para o resumo semanal.

`python3 $S/scripts/construction_dates.py --warn-days <warn_days> $I/data/prazos.json` faz a conta (seção
"Ferramenta").

## Parcelas a pagar

`installments` (o comando, como está) imprime as parcelas a vencer em JSON; `<installments> | python3
$S/scripts/construction_dates.py --warn-days <warn_days> -` diz quais vencem na janela e quais venceram (a parcela
com `"paid": true` sai da conta). As que vencem em até `warn_days` dias, e as vencidas sem pagamento, viram tarefa:
- plano do `backlog.py` com a meta `obra-parcelas-<AAAA-MM>` (o mês do vencimento) em `$I/data/plano-parcelas.json`;
  `code` `parcela-<AAAA-MM-DD>-<n>` (estável: rodar de novo não duplica); `title` "Pagar parcela <n> de <total> de
  <o que é>, vence <DD/MM>", sem valor nem fornecedor; `what` "Parcela do banco de finanças com vencimento em
  <DD/MM/AAAA>" (o valor e a conta ficam na saída do `installments` e no resumo da sessão, nunca no plano);
  `done_when` "pagamento registrado no banco"; `"agent_can_do": false`; `"epic": false`;
- vencida sem pagamento: além da tarefa, `$PL alert --title "Parcela <n> de <o que é> venceu há <N> dias" --severity
  high --assunto parcela-<code>`;
- o banco mostra paga e a tarefa está aberta: vai para o resumo ("parcela 3 da estrutura paga em 10/12: concluir a
  tarefa?"). Quem conclui é o usuário.

Parcela que vence depois de `warn_days`: só no resumo semanal. Nunca pagar, agendar nem mexer no banco: o comando só lê.

## Pendências com fornecedor

Quando o usuário (ou a obra) pediu algo a um fornecedor, projetista ou construtora e espera resposta (orçamento,
medida, data de entrega, nota, revisão de projeto): guardar em `$I/data/pendencias.json`, `{"code", "title", "who",
"since", "source", "pid"}`, com `since` o dia do pedido (o e-mail ou a mensagem). `who` fica só na máquina.
- A tarefa do assunto vai para a coluna de espera do projeto: `$PL tarefa estado <PID> "<coluna de espera>" [--until
  AAAA-MM-DD]`, com `--until` só quando a fonte dá a data (o fornecedor prometeu responder até sexta); sem data, sem
  `--until` (o Planou deixa 7 dias). Vale na tarefa que o agente criou fora das fontes (pelo plano ou por `tarefa
  nova`). Tarefa que veio de um item de fonte segue o estado da fonte, e o próximo sync desfaria a mudança: nela, só a
  lista e o resumo. Tarefa da pessoa só muda com um pedido do agente aberto nela (`--task <PID>`); recusado, não
  insistir.
- Sem resposta há `followup_days` dias ou mais: a cobrança vira rascunho, nunca mensagem. `printf '%s' "<texto>" | $PL
  draft --channel email|chat --to "<quem>" --title "Cobrar resposta: <assunto curto>" --task <PID> --text -`. Texto no
  tom do usuário (direto, educado, pt-BR, sem travessão), citando o que foi pedido e quando. Quem envia é o usuário.
  Uma cobrança por pendência por vez: com um rascunho aberto, não abrir outro; sem resposta de novo depois de
  `followup_days`, um rascunho novo substitui o anterior.
- Chegou a resposta: tirar da lista, e o assunto segue no assistente (orçamento vai para a análise).
- No resumo semanal: "Sem resposta há 6 dias: orçamento de esquadrias" (título sem o nome em `title`).

## Resumo semanal

O plugin não acorda a sessão numa hora marcada: quem lembra é uma linha do tick, como no resumo do dia do assistente.
O gancho da instância imprime a linha do resumo semanal no `report_weekday` a partir do `report_hour`, e a da
conferência de prazos e parcelas uma vez por dia; sem essa linha, a sessão faz as duas no primeiro tick que passar da
hora. No `report_weekday`, a partir do `report_hour`, uma vez por semana: gravar a semana enviada (`AAAA-Www`) em
`$I/data/gestao-obra.json` e não mandar de novo na mesma semana. Uma mensagem curta, para ler no celular:

```
Obra, semana <DD/MM a DD/MM>
Andou: <etapas que começaram ou terminaram, com a fonte; parcelas pagas>
Travou: <etapa ativa passada do fim; próxima esperando dependência atrasada; pendência sem resposta há N dias>
Vence: <prazos e parcelas dos próximos 14 dias, vencidos primeiro, com a data e a fonte>
A confirmar: <datas sem fonte que importam para a próxima etapa>
```

Linha sem nada: omitir. Semana sem nada em nenhuma linha: não mandar. Valor e fornecedor podem ir no resumo (fica na
sessão), nunca num título que sobe ao Planou com `confidentiality: title`.

**Divisão com o resumo do dia:** com os dois comportamentos ligados, o resumo do dia continua sendo um só, do
`construction-advisor`, e a linha de prazos e parcelas dele vem desta gestão (a mesma conta de `warn_days`). O resumo
semanal é à parte e não repete o do dia.

## Ferramenta

`python3 $S/scripts/construction_dates.py [--today AAAA-MM-DD] [--warn-days N] [--followup-days N] <arquivo.json|->`
lê uma lista JSON de `{"title", "date", "source"}` (prazos, parcelas), o `stages_file` inteiro (o `end` de cada etapa
que não está `done`) ou as pendências (`since`, o dia do pedido). Imprime uma linha por item que pede atenção,
vencidos primeiro:
`VENCIDO <title>: venceu há N dias (fonte)`, `HOJE ...`, `VENCE <title>: em N dias (fonte)`, `PARADO <title>: sem
resposta há N dias`, e `A CONFIRMAR <title>: sem data` ou `sem fonte` (data sem fonte nunca conta como data). Item
fora da janela não aparece. Sai 0, e 2 com o motivo quando o arquivo está errado (data inválida, item sem título).
Para o resumo semanal, a mesma conta com `--warn-days 14`.

## O que a gestão não faz

- Não manda mensagem, e-mail nem cobrança a ninguém; não cria rascunho no e-mail sem o OK do usuário naquele pedido.
- Não paga, não agenda pagamento, não grava nada no banco de finanças.
- Não conclui a tarefa de etapa nem de parcela (é do usuário) e não atribui tarefa a outro agente.
- Não inventa data, prazo nem etapa: o que não tem fonte fica "a confirmar".
- Não faz a análise de orçamento nem responde pergunta de fornecedor: isso é do `construction-advisor`.

```permissions
tools: mcp:gmail
actions: planou.note, planou.task
```
