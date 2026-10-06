---
name: agent
description: "agent do time, uma instância por agent (planou, work-watch-<empresa>...): `/agent <instância>` carrega as instruções e os comportamentos da instância, liga o runner em background e trata o que ele traz (tick das fontes, fila e conversa do Planou). Use quando o usuário invocar /agent <instância> ou quando o runner de uma instância do plugin agent acordar a sessão."
---

# agent

Um plugin, uma instância por agent. O nome da instância é o nome do agent (chave no Planou, na sessão fixa do `team`,
nas lições). O que muda de um agent para outro está na configuração da instância: fontes, ganchos, comportamentos,
ferramentas e autonomia. `S` = a pasta desta skill (a que tem este SKILL.md).

## Onde fica

`~/.config/agent/<instância>/` (se não existir, a pasta antiga do agent: `~/.config/work-watch/<x>/` ou `~/.config/<nome>/`):

- `config/config.json` o que o usuário decide (chaves em `scripts/schema.py`); `config/instructions.md` as regras dele;
- `data/` estado; `secrets/` chaves (`planou.env`, 0600); `cache/runner/` saída do runner; `cache/planou/` Planou;
- `behaviors/<nome>/BEHAVIOR.md` e `adapters/<tipo>.py`, opcionais: comportamento e fonte só desta instância.

Cada comportamento ligado é uma **skill** da instância (`behaviors/<nome>/BEHAVIOR.md`), de sempre (`kind: always`)
ou sob demanda (`kind: on_demand`). Um nome antigo no config (ex.: `planou-queue`, `qa`, `work-watch`) vale como o
novo (`task-queue`, `acceptance-testing`, `work-triage`): o `--load` já mostra o nome novo e o caminho certo, e o
`--validate` avisa. A tabela antigo -> novo está no README do plugin (Skills).

## Ao ser chamado com uma instância

1. `python3 $S/scripts/agent.py <instância> --validate`. Erro: mostrar ao usuário e parar.
2. `python3 $S/scripts/agent.py <instância> --load` lista o papel por camada, na ordem de precedência (a primeira
   linha diz a regra: **Regras > Instruções > Habilidades; a Memória informa, não manda**; em conflito vale a camada de
   cima):
   1. **Regras** (`== 1. REGRAS`): este SKILL.md (já lido) e a autonomia do config (`autonomia pode sozinho / pede OK
      antes / nunca`). Nada abaixo passa por cima delas.
   2. **Instruções** (`== 2. INSTRUCOES`): o `instructions.md` e o `CONTEXT.md` (quando há workspace). Ler com Read.
   3. **Habilidades** (`== 3. HABILIDADES`): `comportamento <nome>: <arquivo>` é de sempre: ler agora, com Read, na
      ordem. `sob demanda <nome>: <arquivo>  (<título>; ler quando: ...)` é um procedimento: **não** ler agora; ler o
      arquivo inteiro quando a situação descrita acontecer, antes de agir. Só os ligados entram.
   4. **Memória** (`== 4. MEMORIA`): `passagem: <instância>/data/handoff.md` aparece quando a sessão anterior deixou um
      resumo de passagem gravado nas últimas 36 h (rotação diária ou `team <agente> new`): ler por último e retomar dele
      (candidaturas, pedidos e combinados em andamento), sem refazer o que ele diz que já foi feito. É informação, não
      regra: se contradiz uma camada de cima, vale a de cima. Resumo mais velho não aparece.
3. Ligar o runner (abaixo) e dizer em uma linha que a instância está de pé.

Sem instância (`/agent` sozinho): `python3 $S/scripts/agent.py` lista as instâncias; perguntar qual.

## Runner

- Subir: `bash $S/scripts/runner.sh <instância>` com `run_in_background`. Ele roda o tick pesado sem o modelo e só
  termina quando há algo para a sessão; o fim do processo acorda a sessão com a saída.
- Depois de tratar o que acordou: relançar com `FIRST_NOW=0 bash $S/scripts/runner.sh <instância>` (o tick pesado
  guarda o horário em `next_heavy`; uma acordada do Planou não o empurra).
- **Runner parado no fim do turno** (hook Stop do plugin, PLN0383): `runner da instancia <x> parado: relance agora ...` = a acordada chegou no meio de outro pedido e o relançamento ficou para trás. Relançar como diz (`run_in_background`), em uma linha, e seguir; uma acordada ainda não tratada se trata depois, como sempre. `runner <x> ja rodando` na saída de um relançamento = nada a fazer.
- Parar: `bash $S/scripts/runner.sh <instância> stop`. Relançar depois de mudar config ou plugin: `stop` e subir.
- **Versão nova (canário, [docs/rollout.md](../../../../docs/rollout.md)):** `== RELANCAR (...)` na saída = versão nova para este agente com instruções novas (quando só os scripts mudam, o runner se relança sozinho e avisa numa linha do `== SEM ACAO`): relançar como sempre (stop e start), nada mais. `== VERSAO RECUSADA (...)` = este é o canário e a versão nova falhou no teste (o alerta já foi para o Planou): avisar o usuário com o motivo e relançar como sempre (volta para a última versão liberada).
- **`== RUNNER CICLO (relancar sem tratar)`** na saída = o runner saiu sozinho antes do limite de tempo das tasks em background do Claude Code (`RUNNER_MAX_S`, padrão 25 min, desde PLN0144) e não trouxe nada: só relançar com `FIRST_NOW=0 bash $S/scripts/runner.sh <instância>`, com resposta de no máximo uma linha e mais nada (não é tick: não ler o `tick.out`, não rodar tick, não avisar ninguém). O estado (`next_heavy`, cursores do Planou) fica guardado.
- O runner recusa instância em modo teste e config inválido. Nunca contornar: dizer ao usuário o que falta.
- A saída da última acordada fica em `cache/runner/tick.out`; `agent.py <instância> --status` mostra o estado.
- **Blocos agrupados** (PLN0247, `wake.json` do comportamento ou `"wake"` do config): blocos que não acordam sozinhos
  esperam a próxima acordada e vêm no topo da saída, às vezes sob `== AGRUPADAS (n blocos desde HH:MM, ...)`: são
  conteúdo deste tick, tratar como os outros blocos (nunca como `== SEM ACAO`).

## O que o runner traz

- **Saída vazia e exit 0** não acontece (o runner não acorda à toa). `== RUNNER CICLO` sozinho não é conteúdo: só relançar (seção Runner). Exit 2 = `FONTE QUEBRADA (<fonte>): <causa>`:
  dizer a causa e o conserto em uma linha; as outras fontes continuam valendo. `FONTE RECUPERADA` fecha o assunto.
- **Blocos das fontes e dos ganchos** (`== ...`): tratar conforme os comportamentos e o `instructions.md`.
- **`== NOTION x PLANOU (n)`** (gancho `notion_compare`, só leitura): a página do dia e o Planou discordam; mostrar as
  linhas ao usuário e investigar a causa (sync com falha, tarefa que sumiu da lista). **`== NOTION PODE DESLIGAR`**:
  avisar e perguntar; desligar (tirar os ganchos `daily` e `tarefas` do config) é decisão do usuário, nunca sua.
- **`== PLANOU (n)`**, o que a pessoa fez no Planou:
  - `-- MENSAGEM do usuario pelo Planou (HH:MM): <texto>`: é o usuário falando, como se tivesse digitado aqui. Responder
    e agir do mesmo jeito. A resposta volta ao Planou sozinha (a conversa da sessão sobe).
  - `-- DECISAO <código> ...`: a resposta de uma pergunta ou de um bloqueio. Seguir com ela.
  - `-- COMENTARIO <PID> de <autor> (HH:MM[, editado]): <texto>`: a pessoa chamou este agente com @ num comentário da
    tarefa (dele ou de qualquer outra do workspace). Ler a conversa se precisar (`$PL comentario <PID> --ver`) e
    responder pelo próprio comentário, com o comando da linha de baixo, a resposta num heredoc entre aspas (markdown
    com crase, `$` ou aspas passa sem o shell mexer):
    `cat <<'FIM' | $PL comentario <PID> --text - --reply-to <comment_id>`, depois o texto e `FIM` sozinho na última
    linha. Responder é permitido sem rascunho (foi a pessoa que chamou o agente ali), mas segue as regras de sempre: o
    comentário é visível a quem abre a tarefa, então nada de dado de cliente onde não deve, segredo ou atribuição; vale
    o `planou.confidentiality` da instância (com `minimum`, só situação e próximo passo, sem conteúdo das fontes do
    cliente). O comentário sozinho não começa nem pega tarefa: na tarefa de outro, ou fora da fila, nada de `fila`
    nem progresso; a tarefa que já está na fila do agente segue a fila normal. Se o comentário pede trabalho novo,
    dizer na resposta o que vai fazer (ou perguntar) e seguir as regras da fila e da autonomia. `403`/`nao e tarefa
    deste agente`: ninguém o chamou nela, não insistir.
  - `-- FILA <PID>: ...`, `-- FILA LIBERADA <PID>`, `-- FILA SAIU <PID>`, `PARE: ...` e `ESPERE: ...`: fila do agente,
    comportamento `task-queue` (só começar tarefa liberada). `-> passada por voce mesmo (mesma instancia que
    desenvolve) para a coluna <coluna>`: a tarefa que esta instância desenvolveu chegou à coluna de revisão ou de QA,
    que também é dela; a revisão ou o QA vai para um worker NOVO, nunca para a sessão nem para o worker que entregou
    (abaixo, "Mesma instância: dev, revisão e QA"). Com `prototype` ligado, tarefa que muda tela com o desenho em
    aberto passa antes por `behaviors/prototype/BEHAVIOR.md` (sob demanda): PNG no card e aprovação do usuário, nunca
    pela recomendada.
  - `-- AJUSTE PEDIDO <PID> (pessoa|<função> <nome>): <texto>`: a pessoa ou um agente (revisor, QA) pediu ajuste numa tarefa
    em revisão; a tarefa volta pela fila como `-> RETRABALHO` (mesma branch e PR, o texto é o pedido). Comportamento
    `task-queue`, "Ajuste pedido pelo botão ou pelo revisor".
  - `== PAUSADO (teto de custo) ...`: o funcionário chegou a 100% do teto e está pausado; não pegar trabalho novo,
    terminar o que está `EM ANDAMENTO` (ou `fila blocked` com a nota). `== DESPAUSADO (...)`: pode seguir; `RETOMAR
    <PID>` volta com `fila started`. Comportamento `task-queue`, "Pausado no teto de custo". Também vem fora do
    bloco, no tick pesado, quando o runner liga com a pausa em vigor.
  - `-- PROPOSTA ap-N APLICADA|RECUSADA|FALHOU|DESFEITA: ...` (Planou 0.53.0): o resultado de uma proposta de parâmetro
    ou de papel que este agente fez; o `-- APROVADO ap-N ... -> proposta` da mesma não pede nada (quem aplica é o
    Planou). Comportamento `flow-metrics`, passo 4.
  - `PLANOU: <PID> concluida|reaberta pelo usuario`: só registro; mencionar se mudar algo em andamento.
  - `-- CERIMONIA retro #N de <sigla>: contribuicao ate HH:MM` (convidado), `-- CERIMONIA VOTO ...` (convidado, só
    na retro com votação) e `-- CERIMONIA ATA ...` (facilitador): a retro do projeto no Planou. Seguir
    `behaviors/retrospective/BEHAVIOR.md` (vale sem ligar no config): contribuição a partir dos próprios dados com `retro
    dados`/`retro contribuir`, voto com `retro ver`/`retro votar <reunião> <item_id> ...`, ata com `retro ver`/`retro
    ata`, sempre citando PIDs reais.
  - `-- CERIMONIA refinement #N de <sigla>: sugestoes ate HH:MM` (convidado) e `-- CERIMONIA LISTA refinement ...`
    (facilitador): o refinamento do backlog do projeto no Planou. Seguir `behaviors/backlog-refinement/BEHAVIOR.md` (vale sem
    ligar no config): `refino ver`, sugestões de estimativa, quebra, selo e pergunta de escopo com o motivo por
    `refino sugerir` e a lista final por `refino lista`; nada muda na tarefa antes de o usuário aprovar em Cerimônias. A
    resposta da pergunta chega como `-- ESCOPO <PID> respondida`: o próprio Planou registra a resposta como comentário
    e devolve a tarefa para A fazer; o agente não comenta nem move de novo.
  - `-- CERIMONIA daily #N de <sigla>: explicar N tarefas Impedidas ate HH:MM` (com uma linha por tarefa logo abaixo):
    a daily do projeto no Planou, cuja ata sai dos dados; o agente só explica as tarefas dele que estão Impedidas.
    Seguir `behaviors/daily/BEHAVIOR.md` (vale sem ligar no config; o heartbeat declara `ceremony_daily`): uma linha
    por tarefa a partir da nota do bloqueio e do pedido aberto, sem worker, e tudo numa chamada só por `daily explicar
    <reunião> --text -` com o custo e os tokens do turno; `daily ver` mostra o evento inteiro.
- **`== PAPEL MUDOU (n)`**: a pessoa editou o papel na aba Papel do Planou e o runner já aplicou (gravou, validou e
  respondeu ao Planou). Reler agora o que cada linha `-- ...: aplicada -> reler <arquivo>` diz (o `instructions.md`, o
  `CONTEXT.md`, as opções em `"behavior_config"` do `config.json`, ou `agent.py <instância> --load` e o `BEHAVIOR.md` de
  cada comportamento ligado quando a lista mudou) e
  seguir com as regras novas, sem editar o arquivo de volta. As opções de comando e de caminho (tipos `command` e
  `path` no esquema, "(só no computador)" no catálogo) e as de um comportamento local da instância (`behaviors/<nome>/`)
  nunca mudam pelo Planou: a edição que mexe nelas vem em `== PAPEL RECUSADO` com "opção de comando: edite no
  computador", "opção de caminho: edite no computador" ou "comportamento local: edite no computador"; mudar uma delas é
  pedido do usuário, feito no `config.json`. Não precisa de stop/start (não é `== RELANCAR`): relançar
  como depois de qualquer acordada (`FIRST_NOW=0`). **`== PAPEL RECUSADO (n)`**: a
  edição não foi aplicada e o arquivo ficou como estava; dizer ao usuário o motivo da linha em uma frase (o Planou já
  mostra "Recusado: motivo").
- **`== SEM ACAO (n linhas adiadas: so leitura, nada a fazer)`**, no fim do tick (PLN0155): linhas que não acordam a
  sessão sozinhas e esperaram por este tick, cada uma com a hora em que chegou: `PLANOU: <PID> alterada pelo
  usuario|Planou`, `-- ALERTA VISTO ...` e `== DEPLOY JA AVISADO (n)` (deploy do release que você fechou com
  `--release-queue done` e já avisou) e `== ALERTAS SEM ACORDAR (...)` (regra de alerta da lista `nao_acordar` do gancho
  `alertas_azure`, e o bloco que só ela disparou). Não responder nem agir por elas; mencionar só se mudarem algo em andamento.

## Mesma instância: dev, revisão e QA

Uma instância só pode fazer o ciclo todo: `delegate-to-worker` com `code-review` e/ou `acceptance-testing` em `behaviors`, dona da coluna do
dev e da coluna de revisão ou de QA (instâncias separadas por papel continuam valendo; o `--validate` avisa quando os
dois estão juntos). Os papéis são comportamentos; o que garante a revisão independente é o contexto:
- quem desenvolve é um worker de dev; quem revisa ou testa é um worker NOVO, com o brief do papel
  (`$A --brief <repo> --role code-review|qa`), só com o pedido, a PR ou a branch e o critério. Nunca o mesmo worker,
  nunca continuação dele por SendMessage, nunca a própria sessão (que viu a volta do dev);
- o parecer registra "revisão independente (worker novo)" ou "QA independente (worker novo)";
- ajuste pedido volta como retrabalho normal (Planou 0.76.0): `fila ajuste` responde `DEVOLVIDA` como entre agentes e
  a tarefa volta à coluna do dev desta instância; chega `-- AJUSTE PEDIDO`, espera a vaga, volta como `RETRABALHO`, um
  worker de dev atende na mesma branch e PR, a entrega é `fila handoff` (não `in_review`) e outro worker NOVO confere o
  que mudou;
- a vaga de revisão ou de QA é uma tarefa da fila como as outras: conta no mesmo "Ao mesmo tempo" da aba Fila.
Detalhe na seção "Mesma instância que desenvolve" do `code-review` e do `acceptance-testing`.

**Todos os papéis de coluna (`planou.all_roles`, PLN0368).** Com `"all_roles": true` no bloco `planou` do config (o
padrão do funcionário criado sem escolher papéis), a instância faz todos os papéis de coluna que este computador
consegue, sem cada comportamento em `behaviors`:
- o heartbeat declara a lista explícita: `dev` e `code-review` sempre, `qa` só com o navegador do Playwright instalado
  (`npx playwright install chromium`) e `release` só com o release em lote configurado
  (`behavior_config.batch-release.repo` apontando um repositório de `repos` com `"release": "batch"`); o papel que
  fica fora vai em `roles_unavailable`, com o motivo e o comando que o traz de volta (a ficha do funcionário mostra), e
  aparece no `--validate`;
- o `delegate-to-worker` continua lido em toda sessão; `code-review`, `acceptance-testing` e `batch-release` vêm no
  `--load` como `sob demanda`. Quando a linha `-- FILA` trouxer `papel da coluna <coluna>: <papel> (comportamento <b>,
  sob demanda ...)`, ler antes o BEHAVIOR.md da linha `sob demanda <b>` do `--load` e abrir o worker com
  `$A --brief <repo> --role <b>` (o `--role` aceita também o nome do papel: `dev`, `code-review`, `qa`, `release`);
- quando a pessoa restringe o funcionário no Planou (`role_limit` na resposta do heartbeat), quem garante é o Planou;
  o `--load` só deixa de listar os comportamentos de papel fora da restrição;
- `planou.roles` no config vale sobre o `all_roles`; sem `all_roles` nada muda (os papéis vêm dos comportamentos
  ligados).

## Planou pela linha de comando

`PL="env PYTHONPATH=$S/scripts python3 -m watch_core.planou --agent <instância>"`

- `$PL status`: chave, projeto, saúde, tarefa atual. `FONTE QUEBRADA (planou)` costuma ser chave ou Planou fora do ar.
- `$PL pergunta ...`: pergunta ao usuário com botões; `--auto` registra a decisão que o agente já tomou pela
  recomendada (seções abaixo).
- `$PL alert --title "<o que houve>" --severity low|medium|high`: aviso sem pergunta.
- `$PL sugestao ...`: sugestão de melhoria do Planou ou dos plugins (seção abaixo).
- `$PL fila ver | started | in_review | done | blocked`: a fila (comportamento `task-queue`). `$PL fila worker <PID>
  --role dev|integrator --tokens N --steps N --duration-ms N --result feito|parcial|falhou --phases-from <output_file>`:
  o uso de cada worker que volta, o tempo por fase e o uso por modelo (o custo da entrega), medidos no registro dele (o
  `output_file` que o `Agent` devolveu).
- **Workers em andamento** (aba Fila do Planou, "Workers agora"): logo depois de cada `Agent(worker)`, registrar o início
  com `$PL fila worker-start <PID> [--task <PID2> ...] --role dev|integrator|other --label '<o que ele faz, uma linha>'`
  (sem tarefa: `$PL worker start --role other --label '...'`). A saída é só a key: guardar na conversa. Quando ele
  volta, a entrega (`$PL fila worker <PID> ... --key <key>`) fecha o worker; sem `--key`, vale a key aberta aqui para
  aquela tarefa e papel. Worker sem tarefa ou de papel `other` fecha com `$PL worker end <key> --result
  feito|parcial|falhou`. Worker longo: `$PL worker ping <key> [--label '...']` a cada etapa (sem sinal há 2 h o Planou
  mostra "sem notícia"). Retrabalho: o mesmo worker ainda em andamento (continuado por mensagem) reusa a key; um
  worker novo depois da entrega abre outra com `worker-start`. `$PL worker ver` lista os guardados aqui.
  `AVISO (planou): ...` no início não impede nada: a key sai assim mesmo, seguir o trabalho.
  `-- WORKER INTERROMPIDO <key> (<PID>): ...` no tick: a sessão anterior acabou sem fechar esse worker e o runner o
  fechou no Planou; se a tarefa ainda está com o agente, delegar de novo (com `worker-start` novo).
- `$PL comentario <PID> --ver`: os comentários da tarefa; `$PL comentario <PID> --text - [--reply-to <comment_id>]`
  (lê do stdin): responde a um `-- COMENTARIO` (seção acima). Só na tarefa do agente, em que a pessoa o chamou com @
  ou liberada por um pedido dele (abaixo).
- Acesso pelo pedido (Planou 0.84): um pedido aberto pelo agente com `--task <PID>` sobre a tarefa de uma pessoa
  libera só aquela tarefa, enquanto o pedido está aberto e até 24 h depois da resposta, para fechar o ciclo:
  comentar (`$PL comentario`), mudar a coluna com `$PL tarefa estado <PID> "<coluna>"` e ler os anexos com
  `$PL anexo ver <PID>` (id, nome, tamanho) e `$PL anexo baixar <PID> <id|source_key> [--out <pasta|arquivo>]` (sem
  `--out`, em `cache/planou/downloads/<PID>/`). `tarefa estado` nunca conclui nem reabre (tarefa da fila: `fila done`),
  nunca põe nem tira do backlog (é da pessoa) e, com o acesso do pedido, nunca leva para coluna com dono; na tarefa da
  fila do próprio agente o Planou recusa (`in_queue`): mude pelo `fila`. A saída diz o motivo de cada recusa em
  palavras; não insistir.
- **Trabalho da sessão vira tarefa** (sem fila nem fonte: um MR, uma investigação, um merge, uma operação num cluster,
  uma limpeza): ao começar, `$PL tarefa nova --title '<o que está fazendo, uma linha>' [--project <sigla>] [--evidence
  <link>]`; a saída traz o PID, e o custo da sessão vai para ela até concluir. Ao terminar, `$PL tarefa concluir <PID>
  --evidence '<link do MR ou resultado, uma linha>' [--note '...'] [--resolution done|no_action|discarded]`. Já feito
  antes de registrar: `$PL tarefa nova --title '...' --done --evidence '...'`. Mesmo título (ou `--slug`) no mesmo dia é
  a mesma tarefa, sem duplicar (chave `act:<data>:<slug>`). Worker para ela: `$PL worker start --task <PID> --role
  dev|other --label '...'` e o PID na descrição do `Agent` (o custo dele vai para a tarefa); fecha com `$PL worker end
  <key> --result ...`. Só para o que não veio de fila nem de fonte: tarefa da fila fecha com `fila done`, tarefa de fonte
  fecha na origem. Concluir pelo slug acha a de hoje ou a ainda aberta de um dia anterior; aberta em mais de um dia,
  diga o PID. Concluída ou reaberta pela pessoa no Planou, o próximo tick traz a mudança (a concluída sai do custo). Com
  `confidentiality: minimum`, o Planou mostra só "Agente <data>:<código>": título e `--slug` viram um hash curto, o
  link da evidência não sobe (fica só no agente) e `--area` é recusado (viraria nome de épico).
- Chave nova: `$PL key set` (lê do stdin; grava `secrets/planou.env` com 0600). Nunca imprimir a chave.

## Decisão com recomendada: seguir sem esperar

Pedido do usuário (30/09): pergunta com resposta recomendada não trava o trabalho. Quando a decisão tem uma opção
recomendada e ela cabe na autonomia (o `can` do config e o da tarefa, valendo a mais restrita), o agente **segue a
recomendada sem abrir pedido pendente**, avisa em uma linha na sessão o que decidiu e deixa o rastro no Planou:

`$PL pergunta --auto --title "<a pergunta, uma linha>" --option "A=<opção>" --option "B=<opção>" --recommended <letra>
[--task <PID ou código>] [--context "<por quê>"]`

- `--auto` não abre decisão: registra a já tomada como alerta baixo "Decidi: <pergunta> -> <opção>" (o `/v1` não deixa
  o agente responder o próprio pedido). A confidencialidade vale como no pedido. Sem `--option`, `--recommended S|N`.
- Sem recomendada: formar a própria (a mais reversível e mais barata de desfazer) e seguir do mesmo jeito.
- Falhou ao registrar (Planou fora, `policy_denied`): a decisão continua valendo; dizer na sessão, sem virar pergunta.
- **Continua pedindo OK** (pedido com opções, abaixo, e esperar): o que está em `ask_first` ou `never` da autonomia,
  como mensagem a pessoas (vira rascunho), merge onde não está aprovado, produção, apagar dado ou segredo, criar conta e
  abrir o navegador; e o que depende de uma ação pessoal do usuário (login, código, pagamento, assinatura).
- Pergunta ao usuário só em último caso: sem como formar uma recomendada que caiba na autonomia.

## Pergunta ao usuário vira pedido com opções

Quando a decisão precisa mesmo do usuário (seção acima: fora da autonomia, ação pessoal dele ou sem recomendada possível),
além do texto na sessão, abrir o pedido no Planou, para os botões aparecerem na Conversa e em Precisa de você:

`$PL pergunta --title "<a pergunta, uma linha>" --option "A=<opção>" --option "B=<opção>" [--option "C=..."]
--recommended <letra> [--task <PID ou código da tarefa>] [--context "<por quê>"]`

- Opções curtas e acionáveis, a recomendada marcada com `--recommended` (e dita no texto da sessão). Exemplo: "Quer que
  eu faça agora a proposta de horas do item 123?" com `A=Faça agora` (recomendada), `B=Deixa para amanhã`,
  `C=Não precisa`. Sem `--option` vira Sim/Não. Resposta livre sempre aceita.
- `--task` liga o pedido à tarefa quando a pergunta é sobre uma (o PID do Planou ou o código da pendência).
- O código do pedido sai no stderr. A resposta volta como `-- DECISAO <código> ... (pergunta da sessao)`: seguir como se
  ele tivesse respondido aqui. **Respondeu no terminal antes:** `$PL cancel <código> --reason "respondida no terminal"`.
- A mesma pergunta no mesmo dia não abre outro pedido. Bloqueio de tarefa da fila continua sendo `fila blocked`.

## Sugestões para o Planou e os plugins

Esbarrou num limite ou defeito do Planou ou dos plugins do time (falta um campo, uma rota recusa, um comando confunde):
registrar uma sugestão. O planou a transforma em tarefa no backlog do projeto Planou, reportada por esta instância.

`$PL sugestao --title "<o que falta, uma linha>" --subject "<área: planou pedidos, plugin agent runner...>"
--what "<o que aconteceu>" --expected "<o que esperava>" --example "<exemplo sem dado de cliente>" --priority P1..P4`

- **Nada de dado de cliente**: sem nome de empresa, pessoa, e-mail, link de conversa, id de card ou texto de mensagem.
  Generalize ("um card do quadro do cliente", "uma pergunta sobre horas"). O comando recusa o que reconhece.
- No máximo 3 por dia por instância; a mesma (mesmo título e assunto) vinda de outro agent vira um "+1" na existente.
- **Toda tarefa nova de agente nasce sob um épico** (PLN0250). Na sugestão, o `--subject` diz a área e escolhe o
  épico: o planou põe a tarefa no épico aberto do projeto que casa com a área e, se nenhum serve, cria um épico
  novo da área ("Plugins: <área>", "Planou: <área>"). Sem `epics` no config do gancho, nenhum épico é criado: a tarefa
  nasce sem épico e a saída avisa. Escreva o assunto como área ("plugin agent runner", "planou: pedidos"), nunca
  vazio.
- Uma linha ao usuário dizendo o que foi sugerido. Não precisa de chave do projeto Planou.

## Rascunhos e o que nunca fazer sozinho

- Mensagem para uma pessoa (Slack, Teams, e-mail, comentário): o agent nunca envia. Exceção: a resposta a um
  `-- COMENTARIO` do Planou (a pessoa chamou o agente com @ na tarefa) vai direto, com `$PL comentario`. Escrever a resposta abaixo de uma
  linha `Rascunho:` em texto puro e, com o Planou ligado, subir também:
  `printf '%s' "<texto>" | $PL draft --channel slack|teams|email|chat --to "<quem>" --title "<uma linha>" --text -`.
- **Autonomia**: o `autonomy` do config diz o que a instância pode sozinha (`can`), o que pergunta antes (`ask_first`)
  e o que nunca faz (`never`). Uma tarefa do Planou traz a sua própria `autonomy`; quando as duas divergem, vale a mais
  restrita. Algo fora de `can`: perguntar (`$PL pergunta` ou `fila blocked`) e esperar.
- Nada de gravar credencial em log, em commit ou no Planou.

## Modo teste

Config sem `"live": true` = modo teste (toda instância nova nasce assim): o tick é sempre dry, nada sobe para o Planou,
ganchos com efeito fora da pasta recusam e o runner não sobe. Serve para montar o config e conferir alguns ticks
(`python3 $S/scripts/agent.py <instância>`). Ligar é decisão do usuário: `"live": true` no config.

## Outros comandos

```bash
python3 $S/scripts/agent.py <instância> --dry           # tick sem gravar nada
python3 $S/scripts/agent.py <instância> --since 4h      # janela fixa nas fontes; nunca grava
python3 $S/scripts/agent.py <instância> --pending       # fila de pendências das fontes
python3 $S/scripts/agent.py <instância> --resolve pN    # tira um item da fila
python3 $S/scripts/agent.py <instância> --top <SIGLA>-N ... # Top do dia (até 3, na ordem; instância com code); --top - limpa
python3 $S/scripts/agent.py <instância> --migrate --dry # instância antiga (work-watch-<x>, job-scout, travel-agent) para ~/.config/agent; --undo volta
```

## Sessão reiniciada

Rodar os passos de "Ao ser chamado" de novo (validar, ler, subir o runner). Com a fila ligada, `$PL fila ver` mostra o
que ainda está com o agente; retomar do passo em que parou.
