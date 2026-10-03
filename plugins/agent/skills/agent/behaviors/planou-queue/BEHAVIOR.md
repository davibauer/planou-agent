---
title: Fila no Planou
summary: Trabalha as tarefas atribuídas no Planou dentro do limite de vagas e presta contas em cada uma.
layer: skill
kind: always
---
# planou-queue: a fila do agente no Planou

Vale só com `"planou": {"task_queue": true}` e `"live": true` no config da instância. O heartbeat declara a fila e o
Planou entrega as tarefas atribuídas a este agente (só a atribuição põe na fila: a pessoa, ou o próprio agente num
projeto autônomo, ou o Planou pela vaga livre de um projeto autônomo; o limite de vagas fica na ficha do funcionário, aba
Fila; o antigo modo automático saiu no Planou 0.25.0 e `agent_can_do` é o selo que a vaga livre considera). `PL` é o comando do núcleo: `env PYTHONPATH=$S/scripts python3 -m watch_core.planou
--agent <instância>`.

## Limite de trabalho: só tarefa liberada

O "Ao mesmo tempo" da aba Fila é o limite real de trabalho deste agente. O Planou avisa de toda tarefa que entra na
fila, mas **só se começa tarefa liberada**:
- `-- FILA <PID>: ... -> NA FILA, nao comecar` (`released: false`): anotar e esperar. Não delegar worker, não mandar
  `started`, não abrir branch por ela.
- `-- FILA LIBERADA <PID>: ...` (`task_released`, a vaga abriu) ou `-- FILA <PID>: ... -> tarefa da fila`: pode
  começar, pelo passo 1 abaixo.
- `$PL fila ver` diz, para cada tarefa, `status: liberada` ou `na fila`, na ordem da fila.
- `ESPERE: ...` na saída de um `fila started` (409 `not_released`): a tarefa não está liberada; parar o que começou
  por ela e esperar o `-- FILA LIBERADA`.
- Nenhuma outra regra de quantos workers rodar vale acima desta: o limite é o do Planou.

## Fila parada: `== FILA PARADA <PID> (<motivo>)`

O gancho `fila_parada` acorda a sessão quando uma tarefa fica `queued`, com `released: false`, por mais de 20 minutos
(`after_min`) sem `blocked_by` aberto e com vaga livre (`busy` menor que `wip`): o Planou devia ter liberado e não
liberou. Sai uma vez por tarefa; só volta a avisar se ela sair da condição (liberada, bloqueada, tirada da fila) e
voltar a ficar parada. O motivo vem do campo `release_reason` (ou `reason`) quando o Planou manda; sem campo, vem
`sem motivo visível`. Ao receber:
- `$PL fila ver`: confirmar `wip`, `busy` e que a tarefa segue `na fila` e sem dependência aberta. Se já foi
  liberada, não há o que fazer (siga "Tarefa liberada").
- Conferir a **categoria da coluna** da tarefa: coluna de categoria que pausa (ex.: bloqueado, aguardando) não libera;
  era a causa de 30/09. Conferir o **dono** (a tarefa está atribuída a este agente, ativo e não pausado) e a **sessão**
  (heartbeat chegando, `fila ver` com `source: planou`, agente não pausado no teto de custo).
- Se for configuração (coluna na categoria errada, dono errado), corrigir pela `/api` do Planou (https, UA, CSRF) e
  conferir no `fila ver` que a próxima vaga libera; contar ao usuário em uma linha o que foi corrigido.
- Se não for configuração (motivo do Planou que só a pessoa resolve, ou nada achado): não forçar `started`; abrir uma
  pendência com o PID e o motivo e contar ao usuário.
- No `batch-release`, o mesmo gancho cobre a coluna de release: a tarefa parada nela também é `queued` e não liberada.

## Pausado no teto de custo: `== PAUSADO (teto de custo)` e `== DESPAUSADO`

A 100% do teto de custo do ciclo o Planou pausa o funcionário até o ciclo virar, a pessoa subir ou tirar o teto ou
clicar Despausar. Enquanto durar:
- nada novo: nenhum `fila started` em tarefa nova, nenhum `pronta --self`, nada puxado do backlog (vaga ociosa não
  conta). `fila started` de tarefa nova responde `ESPERE: agente pausado ...` (exit 5): não começar;
- cada `EM ANDAMENTO <PID>` do bloco: terminar (preferível) e dizer ao usuário que terminou mesmo pausado; se não der,
  parar o worker e `$PL fila blocked <PID> --note "parou no teto de custo"` (sem `--note`, vai essa nota);
- `$PL fila ver` traz `pausado` (até quando, o que estava em andamento, o que travou pela pausa).

`== DESPAUSADO (...)`: pode pegar trabalho de novo. Cada `RETOMAR <PID>` travou pela pausa: `$PL fila started <PID>` e
o worker de volta na mesma branch. Sessão nova com a pausa em vigor: o tick pesado traz o `== PAUSADO` de novo.

## Tarefa liberada: `-- FILA LIBERADA <PID>` ou `-- FILA <PID>: ... -> tarefa da fila`

1. `$PL fila ver`: descrição, origem, data, prazo (`deadline`) e a `autonomy` da tarefa. Ler antes de começar.
   **Escopo sem clareza** (objetivo, entregável, o que conta como pronto, restrições, acessos): não começar.
   `printf '%s' "<o que precisa acertar>" | $PL fila refinar <PID> --note -`. Tarefa do próprio agente vai para
   **Em refinamento** com a pergunta em Precisa de você (não ocupa vaga); tarefa criada pela pessoa vai para `blocked`
   com a pergunta. Depois, esperar: a resposta devolve a tarefa para A fazer e ela volta pela fila (`-- FILA`). Com
   `"confidentiality": "minimum"` a pergunta não sobe: dizer o que precisa também na sessão.
   Com o comportamento `prototype` ligado, tarefa que muda tela com o desenho em aberto passa por ele antes do dev (protótipo em PNG no card e
   a aprovação do usuário). A aprovação do protótipo é decisão pessoal do usuário: nunca destravar pela recomendada nem
   com `pergunta --auto`.
2. `$PL fila started <PID> --estimate-h <H>` (a estimativa do agente, em horas, do trabalho inteiro) e delegar a
   entrega a um worker, em background, conforme o comportamento `dev-worker` (ou fazer na sessão, quando é só
   investigação curta), com o `<PID>` na descrição do worker. A sessão fica livre para o runner.
   Logo depois do `Agent(worker)`: `KEY=$($PL fila worker-start <PID> --role dev --label '<o que ele faz>')` e guardar
   na conversa a key e o `output_file` que o `Agent` devolveu (o registro do worker, sem ler; ou o `agentId`); o Planou mostra o worker em andamento na aba Fila até a entrega com a mesma key.
   O `started` marca a tarefa corrente: os turnos da sessão e os dos workers vão ao Planou ligados a ela (o worker
   pelo `<PID>` da descrição, ou pela tarefa corrente quando ele começou), e o Planou mostra o completado medido e o
   custo da tarefa. Estimativa que a pessoa mudou é dela: o `ignored_fields` da resposta diz; não insistir.
   **A cada avanço** (worker voltou, etapa concluída, escopo mudou): `$PL fila tempo <PID> --remaining-h <H>` (o que
   falta, em horas; manda o estado atual de novo, sem mudar nada). Tarefa em `blocked` leva o tempo no próximo passo.
   **Todo worker que volta** (pronto, parcial ou falhou), antes de qualquer outro `fila ...` por ele: mandar o uso
   que a sessão recebeu na volta dele (tokens do subagente, ferramentas usadas, duração em ms):
   `$PL fila worker <PID> --role dev --tokens <N> --steps <ferramentas> --duration-ms <ms> --result feito|parcial|falhou
   --key <key> --phases-from <output_file>` (`--model <modelo>` quando souber; sem `--key`, a aberta aqui para a tarefa
   e o papel). O `--phases-from` mede no registro do worker onde o tempo foi (modelo, testes, CI, git/publish, leitura,
   edição, espera) e o Planou mostra a barra por fase na tarefa, na aba Fila e o gargalo da semana em Métricas; do mesmo
   registro sai o uso por modelo (entrada, saída e cache), e o Planou mostra o custo da entrega; registro que não
   aparece é só um aviso. Ela fecha o
   worker no Planou. Vira uma linha em Entregas na tarefa e entra na média de Métricas. Um worker,
   uma chamada: repetir o mesmo comando não duplica. `AVISO (planou): ... versao antiga` é um Planou sem a rota: seguir.
3. Worker voltou pronto: `$PL fila in_review <PID> --pr-url <link> --remaining-h <H>` (sem PR: `--note "<branch ou
   resultado>"`; `<H>` é o que ainda falta: revisão, ajuste, merge).
   **Em revisão libera a vaga**: o Planou pode entregar a próxima tarefa enquanto esta espera, e o agente continua
   responsável por ela (acompanha a PR e atende o ajuste). Contar ao usuário em uma linha: tarefa, PR ou branch, o que
   falta dele. Com mais de uma tarefa com o agente, todo `fila ...` leva o `<PID>`.
   **Ajuste pedido na Conversa ou na PR** numa tarefa em revisão: `$PL fila started <PID>` ao retomar (volta a ocupar
   a vaga), o worker ajusta a mesma branch e a mesma PR, e no fim `$PL fila in_review <PID> --pr-url <link>` de novo.
   Pelo botão "Pedir ajuste" do Planou é outro caminho: ver a seção abaixo.
4. Fechar (`$PL fila done <PID>`, que manda o restante 0) segue a autonomia da instância:
   - a lista `can` do config inclui merge ou deploy (ex.: release em lote pelo integrador, deploy com trava) ou o
     repositório da tarefa tem `"release": "batch"` em `repos`: fechar quando a entrega estiver no ar, com a versão
     numa nota (`--note "no ar na vX.Y.Z"`); com `"release": "pr"` e merge em `can`, fechar quando a PR entrar;
   - senão: fechar só quando o usuário disser que a PR entrou. Nunca por conta própria depois de abrir a PR.
   - investigação sem código: fechar ao entregar a resposta.

   **Dono de coluna (handoff, Planou 0.27.0).** Numa esteira, o agente pode ser dono de uma coluna do projeto (ex.:
   "Análise UI/UX", dono ux-designer, "Ao terminar, vai para" A fazer). Não há comando novo a lembrar: o mesmo
   `$PL fila done <PID> [--note ...]` decide sozinho. Ele lê os estados do projeto da tarefa na hora
   (`GET /v1/agent/projects/{key}/states`, `owner.is_me` e `next_state`) e, se o agente é dono de alguma coluna com
   próximo estado, manda `{"state": "handoff"}` em vez de `done`: a tarefa vai para o próximo estado e para a fila do dono
   dele (sem dono, volta para a pessoa), e a saída traz `PASSOU: <PID> foi para <estado> (<quem>)`. Contar ao usuário que
   a tarefa **passou adiante**, não que foi concluída. Se a coluna em que a tarefa está não tem próximo (409
   `no_next_state`), o próprio comando conclui com `done`; próximo estado da categoria concluída também conclui. O
   handoff não manda restante 0 (o trabalho continua com o próximo dono).
   - O ponto em que se fecha é o mesmo de cima: a coluna termina quando a parte do agente termina (análise entregue,
     ou a PR entrou conforme a autonomia).
   - `$PL fila handoff <PID> [--note ...]` força o handoff, sem cair para `done` (erro se a coluna não tem próximo).
     Raramente necessário.
   - **Revisão por outro agente** (a coluna do dev tem como próximo uma coluna de revisão com dono, comportamento
     `code-review`): a parte do dev termina na entrega pronta (PR aberta ou branch com push) com os testes verdes, e o
     PID no título da PR e no nome da branch (ex.: `feat/pln0123-filtro`). Passar com `$PL fila handoff <PID> --note
     "<PR ou branch>"` (a nota vai com a tarefa para o revisor em `handed_off_by.note`; sem PR, é a branch) e
     `--pr-url <link>` quando há PR (o Planou 0.43.0 leva o `pr_url` ao revisor; nunca `done`:
     sem a coluna configurada, o `done` concluiria a tarefa), sem esperar o merge. O ajuste pedido pelo revisor volta
     direto ao dev como `-- AJUSTE PEDIDO <PID> (<função> <nome>)`, pela seção abaixo; o do QA também (Planou 0.49.0).
   - **Revisão ou QA na mesma instância** (PLN0281: a coluna seguinte também é deste agente, com `code-review` ou `qa`
     ligado junto do `dev-worker`): o mesmo `fila handoff` passa a tarefa a si mesmo. O Planou não guarda quem passou
     nem a nota nesse caso, então o plugin guarda (`cache/planou/self_handoffs.json`) e a tarefa volta como
     `-- FILA LIBERADA <PID>: ... -> passada por voce mesmo (mesma instancia que desenvolve) para a coluna <coluna>`.
     A parte da coluna vai para um worker NOVO, nunca para a sessão nem para o worker que entregou (seção "Mesma
     instância que desenvolve" do `code-review` e do `qa`). A vaga dessa revisão conta no mesmo "Ao mesmo tempo".
     O registro só vale enquanto a tarefa está na coluna para onde foi passada e só para colunas de revisão ou QA:
     de volta à coluna do dev, numa coluna de outro tipo ou fora da fila, as linhas de sempre.
   - O Planou move pela coluna em que a tarefa está, não pela coluna do agente: uma tarefa que a pessoa atribuiu direto
     ao agente numa coluna com próximo estado também anda para a frente no `fila done`, se o agente for dono de alguma
     coluna com próximo no projeto.
5. Precisa do usuário (acesso, escolha de desenho, algo de `ask_first`):
   `printf '%s' "<o que precisa, curto>" | $PL fila blocked <PID> --note -`. Abre a pergunta em Precisa de você; a
   resposta volta como `-- DECISAO` e aí `$PL fila started <PID>` de novo para seguir. Com `"confidentiality":
   "minimum"` a nota não sobe: dizer o que precisa também na sessão.

## Ajuste pedido pelo botão ou pelo revisor: `-- AJUSTE PEDIDO <PID> (<quem>): <texto>`

A pessoa apertou "Pedir ajuste" numa tarefa em revisão feita por este agente (`(pessoa)`, Planou 0.38.0), ou o agente
revisor ou de QA que recebeu a tarefa depois dela a devolveu (`(<função> <nome>)`: a Função do funcionário que pediu,
como `(QA qa-1)`; sem ela o nome da coluna, e num Planou antigo `(revisor <nome>)`; Planou 0.43.0 e 0.49.0,
comportamentos `code-review` e `qa`; o texto é a lista curta e o parecer completo está na PR). Mesmo evento `task_changes_requested`. A tarefa volta para a
fila do agente, na frente, e espera a vaga como qualquer outra. O texto fica guardado (`cache/planou/changes.json`) e é
o briefing do retrabalho.
1. Ao ver `-- AJUSTE PEDIDO`: anotar e **não mandar progresso** por ela (nem `started`, nem `tempo`). O `fila ...` dela
   responde `ESPERE: ajuste pedido ...` até a nova entrega, e um 409 `not_in_queue` nesse intervalo não é `PARE`: não
   parar nada, a branch e a PR continuam valendo. Contar ao usuário em uma linha que o ajuste chegou.
2. A tarefa volta como `-- FILA LIBERADA <PID>: ... -> RETRABALHO (ajuste pedido)` (ou `-- FILA <PID>` liberada), com o
   texto nas linhas `AJUSTE:` (`AJUSTE do <função> <nome>:` quando veio de outro agente: ler também o parecer na PR). É **retrabalho de tarefa conhecida, não tarefa nova**: sem refinamento e sem branch nova.
   `-> RETRABALHO NA FILA, nao comecar` = ainda sem vaga: esperar.
3. `$PL fila started <PID> --estimate-h <H>` e delegar a um worker (comportamento `dev-worker`) **na mesma branch e na
   mesma PR** (`pr_url` da linha e de `$PL fila ver`), com o texto do ajuste como pedido literal e o critério de pronto:
   atender o texto, testes passando, PR atualizada.
   O worker do ajuste abre key nova (`fila worker-start`), porque o da entrega anterior já fechou.
4. Worker voltou: `$PL fila worker ... --key <key> --phases-from <output_file>` e `$PL fila in_review <PID> --pr-url
   <link>` de novo (a mesma PR). A entrega encerra o ajuste; contar ao usuário em uma linha o que mudou.
`$PL fila ver` mostra o texto em `ajuste_pedido` (`fase`: `esperando a fila` ou `retrabalho`) até a nova entrega.

## Arquivos da tarefa vão como anexo

Todo arquivo local que o agente (ou o worker) produz para uma tarefa da fila (análise, proposta, rascunho, ata, previsão)
vai como anexo dela: `$PL attach <PID> <arquivo>` assim que estiver gravado, e de novo sempre que mudar (só sobe quando o
sha256 muda; o tick reenvia sozinho o que já foi anexado uma vez). Na nota de `fila in_review|blocked|done`, citar o
arquivo pelo caminho: ele sobe como anexo e a nota diz `[ver anexo: <nome>]`. O `fila started` anexa os arquivos locais
que a descrição da tarefa cita. Tipos aceitos: .md, .txt, .pdf, .docx e imagens, até 10 MB; nada de `secrets/`, `.env`,
cache ou arquivo com credencial (o `attach` recusa e diz por quê). Com `"confidentiality": "minimum"` nada sobe.

## Tarefa corrente (custo e tempo)

`in_review`, `blocked`, `done`, `PARE` e `-- FILA SAIU` desmarcam a tarefa corrente sozinhos. Ao sair dela por outro
motivo (a sessão vai tratar outra coisa com a tarefa ainda em andamento), `$PL tarefa -`; ao voltar, `$PL tarefa <PID>`
(ou `fila started <PID>` de novo, se ela estava em revisão). `$PL tarefa` mostra a marcada. O completado não se informa:
sai dos turnos.

## A tarefa saiu

`-- FILA SAIU <PID>` ou `PARE: ...` na saída de um `fila ...` (a tarefa não está mais com o agente): parar o worker
dela (TaskStop), não abrir nem atualizar PR por ela e contar ao usuário em uma linha o que ficou feito (branch, PR).
`ESPERE: ...` (409 `not_in_queue` de uma tarefa em refinamento): esperar a resposta, sem parar nem começar nada.

## Fila vazia e tarefas novas

A autonomia do projeto vem do Planou (`$PL autonomia`, guardada 1 h):
- `autonomous`: **quem puxa do backlog é o Planou** (0.42.0), não o agente. Com vaga livre e nada liberável na fila, ele
  tira do backlog a próxima tarefa que o agente pode pegar (selo "o agente pode fazer", sem `Decidir:`, sem dependência
  aberta, não é épico) e a entrega como `-- FILA LIBERADA <PID>: ... (vaga livre, projeto autônomo) -> tarefa da fila`
  (`queued_by: "auto"`). É uma tarefa liberada como outra qualquer: seguir "Tarefa liberada". Escopo sem clareza volta
  para refinamento (`fila refinar`, passo 1). **Nunca marcar pronta com `--self` só para ocupar vaga ociosa**: os dois
  disputariam a mesma vaga. O `task_changed` do Planou (`by: "auto"`) que tira a tarefa do backlog aparece como
  `PLANOU: <PID> alterada pelo Planou (saiu do backlog pela vaga livre)` e já vale no estado local.
- `semi_autonomous` (o padrão): nada sai do backlog sozinho; espera a pessoa aprovar ou atribuir.
- `manual`: tudo o que o agente cria nasce em backlog; nunca tentar tirar.

`$PL pronta <código|PID> "<motivo curto>" [--self]` fica para o **pedido explícito do usuário** (dentro da autonomia,
sem decisão pendente, dados completos) numa tarefa **que o próprio agente criou** (source_key dele). Se o agente já
sincronizou a tarefa, a marca vai na hora, num sync só dela (`enviado agora`, e diz se o Planou a manteve no backlog ou
não mudou o responsável): vale também numa instância sem fontes. Item de fonte que ainda não subiu vai no sync das fontes.
Tarefa criada pela pessoa (PID que não é do agente) não se marca: o comando recusa e diz por quê (o sync não a toca).

Tarefa nova que o agente cria sem motivo (`ready_reason`) nasce em Backlog. O sync manda o motivo quando a tarefa espelha
algo que já acontece na origem (PR em revisão, card em andamento, pedido explícito numa mensagem, reunião marcada,
prazo) ou quando o agente a marcou pronta. Os avisos do sync vão para `cache/planou/warnings.log`, um por tarefa por dia.

**Decisão da pessoa**: o título começa com `Decidir:` (com os dois pontos; "Decidir em lote" não conta). O Planou nunca
entrega nem puxa essas tarefas e elas não ocupam vaga. O sync manda toda decisão aberta assim, também com
`"confidentiality": "minimum"` e numa instância em inglês (`Decide:` vira `Decidir:`). Tarefa que o agente cria à mão
para a pessoa decidir leva o mesmo prefixo.

## Limite

Vale a autonomia mais restrita entre a da tarefa (`fila ver`) e a da instância (`autonomy` do config). O que está em
`never` não se faz nem com a tarefa pedindo; o que está em `ask_first` vira `fila blocked` antes. Mandar mensagem a
alguém nunca é do agent: vira rascunho.

## Sessão reiniciada

`$PL fila ver` mostra o que ainda está com o agente (pode haver uma em revisão e outra em andamento) e o que espera na
fila; retomar do passo em que parou só nas liberadas (sem novo `started` para o que já está em revisão, a não ser para um ajuste pedido).
Tarefa com `ajuste_pedido` em `esperando a fila` espera a vaga; em `retrabalho`, seguir a seção "Ajuste pedido pelo botão ou pelo revisor".

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido.

```permissions
tools: -
actions: planou.queue, planou.task, planou.note
```
