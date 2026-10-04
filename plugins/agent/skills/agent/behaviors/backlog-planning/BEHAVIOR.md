---
name: backlog-planning
description: "Quebra uma meta em tarefas pequenas e refinadas no Backlog, para a pessoa aprovar. Sempre ativa na instância que a liga em \"behaviors\"."
title: Produto
summary: Quebra uma meta em tarefas pequenas e refinadas no Backlog, para a pessoa aprovar.
layer: skill
kind: always
---
# backlog-planning: a meta da pessoa vira backlog refinado

O agente de produto recebe uma meta (ou um problema) da pessoa e a quebra em tarefas pequenas, cada uma com o que
fazer, por quê, critério de pronto, dependências e prioridade sugerida, no Backlog do projeto, para a pessoa aprovar.
Não escreve código, não abre branch, não começa as tarefas que cria: quem faz é outro agente (o dev) ou a pessoa. Vale
com `task-queue` ligado. `PL` é o do `task-queue`; `S` é a pasta da skill;
`BL="python3 $S/scripts/backlog.py <instância>"`.

Opções em `behavior_config.backlog-planning`:
- `max_tasks`: máximo de tarefas por meta sem pedir OK (padrão 8). Acima disso, a pessoa aprova antes (abaixo).

## De onde vem a meta

- **Tarefa na fila** (`-- FILA LIBERADA <PID>` ou `-- FILA <PID>: ... -> tarefa da fila`): a pessoa atribuiu a meta ao
  agente, ou a pôs na coluna que ele é dono (ex.: "Refinamento", dono product, "Ao terminar, vai para" A fazer ou
  Concluída). Seguir "Tarefa liberada" do `task-queue` (`fila ver`, `fila started <PID> --estimate-h <H>`; uma quebra
  leva 0.3 a 1 h) e depois os passos abaixo. A referência da meta é o PID.
- **Mensagem na Conversa** (`-- MENSAGEM do usuario pelo Planou`): "quero que a pessoa consiga X", "o problema é Y".
  Mesmos passos; a referência da meta é um nome curto (`meta-exportar-csv` vira `exportar-csv`). Meta grande ou vaga
  pela Conversa: sugerir à pessoa criar a tarefa da meta, para ter onde guardar o resumo.
- Tarefa que o Planou puxou pela vaga livre e que **não é meta** (uma tarefa de execução que este agente criou): não
  fazer. `printf '%s' "Tarefa de execução, não de produto: passar para o dev" | $PL fila blocked <PID> --note -` e
  contar ao usuário em uma linha (o motivo está em "Projeto autônomo", abaixo).

## Antes de quebrar

1. Ler a meta inteira (descrição, anexos, comentários) e o contexto que ela cita (README, tarefas ligadas, o que já
   existe no produto). Sem repositório no config, não procurar código: perguntar.
2. **Escopo sem clareza** (para quem é, o que muda para a pessoa, o que fica de fora, prazo, restrição): não inventar.
   Tarefa da fila: `printf '%s' "<o que precisa acertar>" | $PL fila refinar <PID> --note -`. Conversa: perguntar
   com `$PL pergunta` (opções quando der). Esperar a resposta.
3. Conferir o que já existe para a mesma meta: `$BL --list <referência>` (uma meta já quebrada não se quebra de novo;
   ajuste é `--update`, abaixo).

## Como quebrar

- **Tarefa pequena = uma entrega de worker**: uma branch, uma PR, testável sozinha, de 1 a 4 h de trabalho. Maior que
  isso: dividir. Tela e API da mesma coisa podem ser duas tarefas quando a API se testa sozinha.
- **Título curto** (até 80 caracteres), com o verbo e o objeto: "Rota que exporta a lista em CSV". Sem o nome da meta
  repetido em todos.
- **Descrição** com três partes, que o comando monta: `what` (o que fazer, o comportamento esperado, sem desenhar a
  solução inteira), `why` (o que a pessoa ganha, ou o que destrava) e `done_when` (critérios que se conferem: um teste,
  uma tela, um número; "funciona" não é critério).
- **Dependências** (`depends_on`): só as reais (uma precisa da outra pronta para começar). O Planou não entrega uma
  tarefa com dependência aberta; dependência demais serializa tudo.
- **Prioridade sugerida** (`priority`, P1 a P4): P2 para o caminho principal da meta, P3 para o resto, P4 para o que é
  bom ter. P1 só quando a pessoa disse que é urgente. É sugestão: a pessoa muda.
- **Selo "o agente pode fazer"** (`agent_can_do` com o motivo curto): quando um agente faz sozinho, sem decisão da
  pessoa, sem acesso novo e com critério de pronto que se confere. Sem o selo (`false`): o que precisa de gosto,
  conta, credencial, conversa com alguém de fora ou decisão de negócio.
- **Decisão da pessoa** (`"decide": true`): o que só ela decide (escolha de produto, preço, texto final, limite) vira
  tarefa `Decidir: ...`, com o que decidir, as opções e o que cada uma muda em `what`. As tarefas que dependem da
  decisão levam `depends_on` nela. O Planou nunca entrega `Decidir:` a um agente.
- **Épico** (`"epic": true`): com 3 tarefas ou mais, um épico com o nome da meta agrupa tudo e mostra o progresso. Se a
  pessoa já tem um épico para isso, `"epic": "<PID>"`. Sem `"epic"` no plano, as tarefas novas nascem sob um épico
  pela regra do gancho suggestions (apelido, palavras em comum com a meta, ou um épico novo da área; PLN0275) e a saída
  diz `ÉPICO: ...`; sem épicos no config (`planou.epics`) ficam sem épico, com um `AVISO`. `"epic": false`: sem épico.
- **Limite**: até `max_tasks` tarefas por meta. Mais que isso: `$PL pergunta --title "A meta <ref> deu <n> tarefas:
  crio todas?" --option "A=Crio todas" --option "B=Crio só as <k> do caminho principal" --option "C=Reviso a quebra"
  --recommended B --task <PID>`, com a lista curta em `--context`, e rodar com `--max <n>` só depois do OK.

## Onde publicar

Escrever o plano num arquivo (JSON, formato em `scripts/backlog.py`) na pasta `data/` da instância e:

1. `$BL <plano.json> --dry`: confere o plano e mostra onde cada tarefa vai, sem enviar. Erro de plano sai com exit 2 e a
   lista do que corrigir (código repetido, dependência circular ou desconhecida, critério vazio, segredo num texto).
2. `$BL <plano.json>`: cria tudo num sync só. Uma linha por tarefa (`CRIADA <PID> ...`, `JA EXISTE`, `RECUSADA`,
   `AVISO`) e `RESUMO: ...` com os PIDs.

- **Padrão: Backlog.** Tudo nasce no Backlog, e a pessoa aprova (move para A fazer, ou "Passar para o agente").
- **Projeto autônomo** (`$PL autonomia` = `autonomous`): uma tarefa com `ready_reason` (o motivo curto de ela já estar
  pronta: pedido explícito, sem decisão pendente, escopo fechado) nasce em A fazer. Com `"column": "<estado>"` ela
  nasce nessa coluna; se a coluna tem dono (o dev), vai para a fila dele (a linha `COLUNA ...` diz o dono). Sem dono,
  fica com a pessoa. Fora de projeto autônomo o `ready_reason` é ignorado e a tarefa fica no Backlog (o comando avisa).
  - Cuidado do projeto autônomo: a vaga livre do Planou puxa do Backlog as tarefas com selo para o agente que **as
    acompanha**, e quem acompanha é quem criou (este agente), não o dev. Por isso, num projeto autônomo, a tarefa que
    fica no Backlog vai sem o selo, com a linha "Um agente pode fazer: ..." na descrição. Para o dev pegar sozinho, use
    `ready_reason` e `column` com a coluna dele.
- Plano mudou antes da aprovação (a pessoa comentou): editar o arquivo e rodar com `--update` (reenvia título,
  descrição, prioridade, dependências e selo das já criadas, sem mexer no estado; o que a pessoa mudou à mão vale).
  Tarefa nova no plano é criada normalmente. Tarefa que saiu do plano não se apaga: dizer à pessoa.

## Devolver a meta

- **Da fila:** `$PL fila done <PID> --note "<RESUMO do comando>"`. Se o agente é dono da coluna da meta com próximo
  estado, o `done` vira handoff (`PASSOU: ...`) e a meta segue para o próximo estado; senão, a meta é concluída (o
  trabalho continua nas tarefas). Anexar o plano com `$PL attach <PID> <plano.json>` antes, quando a pessoa quiser ver
  o arquivo.
- **Da Conversa:** responder com o RESUMO e a lista curta (PID, título, prioridade, selo, do que depende).
- Nas duas, uma linha ao usuário: quantas tarefas, onde (Backlog ou A fazer), quantas `Decidir:` esperam por ele.

## O que o agente de produto não faz

- Não escreve código nem abre PR; não delegar worker de código (sem `fila worker`).
- Não tira do Backlog o que criou (nem com `pronta`), a não ser pelo `ready_reason` num projeto autônomo, na criação.
- Não atribui a outro agente (o sync não permite): quem distribui é a pessoa, ou a coluna com dono.
- Não cria tarefa para a meta de outra pessoa sem ela pedir, nem mais que `max_tasks` sem OK.
- Nada de nome de cliente, pessoa de fora ou dado real no título e na descrição quando o projeto é de um repositório
  público: descrever o comportamento, não o caso real.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido.

```permissions
tools: -
actions: planou.task, planou.queue
```
