---
title: Pendências de trabalho
summary: Olha as fontes de trabalho da empresa a cada tick e mostra só o que é novo.
layer: skill
kind: always
---
# work-watch: pendências de trabalho de uma empresa

O agent de trabalho de uma empresa (`work-watch-<empresa>`): a cada tick olha as fontes da instância (GitHub, GitLab,
Azure DevOps, Jira, Slack, Teams, Google Chat, Outlook, Gmail, calendários, Kubernetes, gravações), imprime só o que é
novo, mantém a fila de pendências com lembretes em horário comercial e roda os ganchos (lições, custos, KPIs e o que a
instância ligar). Veio do SKILL.md do plugin work-watch (E3 do plugin único); o motor é o mesmo, com os adapters em
`scripts/sources/` e `scripts/hooks/`. `A="python3 $S/scripts/agent.py <instância>"` e
`PL="env PYTHONPATH=$S/scripts python3 -m watch_core.planou --agent <instância>"`.

Comportamentos que completam este, ligados pelo config quando a instância usa: `daily-report` (daily e tarefas na
página do dia), `recordings` (gravações x agenda), `push-alert` (aviso ativo e triagem leve).

## Antes do primeiro tick da sessão

Ler o `instructions.md` da instância, o `CONTEXT.md` do espaço de trabalho (`agent.py <instância> --load` lista os dois)
e a base comum `~/.config/watch-core/data/rules.md`. O `instructions.md` tem o que só vale para aquela empresa (fontes,
pessoas e papéis, o que é demanda em cada fonte, autonomia combinada, exceções) e **prevalece** sobre este arquivo.
Regra só de uma empresa vai para o `instructions.md` dela (mostrar o trecho e gravar com o OK); regra de todo agent vai
para `rules.md`; mecânica que vale para toda instância vai para este arquivo.

## Sessão e runner

A sessão sobe com `team <agent>` ou `/agent <instância>`; o tick roda pelo runner do plugin agent em background, sem o
modelo, e só acorda a sessão quando há conteúdo (SKILL.md do agent). Não existe mais "sessão dedicada" armada à mão, nem
`ScheduleWakeup` como tick, nem runner do plugin antigo: textos antigos de "Como armar" estão obsoletos. Consultas
(`--since`, `--pendentes`, `--github show`, `--agenda`...) executam o modo pedido e não agendam nada.

## O tick

0. **Exit code primeiro.** `exit 2` = `FONTE QUEBRADA (fonte): ...`; as outras fontes valem. Reportar em destaque,
   nunca como "nada novo". Falha passageira já foi tentada duas vezes (`(2 tentativas)`). A volta sai como
   `FONTE RECUPERADA`. As causas típicas de cada fonte (token recusado, sessão do navegador vencida, login do CLI
   vencido, rede) e o conserto estão no `instructions.md`. `-- AGENT PRECISA: ...` = opção marcada numa necessidade do
   agent na página do dia.
   `== CREDENCIAIS`: o primeiro tick do dia traz o painel; depois só o que mudou. `✗`/`⚠` com `renovar` que é comando
   de login: disparar em background e mandar Usuário + Link + Código.
1. **Blocos**, na ordem das fontes do config, com as strings de sempre (as regras de triagem dependem delas): `== GITHUB`,
   `== JIRA`, `== SLACK`, `== CHAT`, `== E-MAIL`, `== AGENDA`, `== GRAVACOES`, `== PENDENTES` (`-- pN [fonte] ...` lembrete,
   `✓ ... resolvida (como)`), `== JIRA DIARIO`, `LUNCH: ...`, `PRESENCA: ...`, `== DAILY`, `== LICOES`, `== TAREFAS`.
   Reportar só as demandas, com `[dd/mm HH:MM] Nome` e a conversa; o que não é demanda vira uma linha
   (`+N msgs sem demanda em X, Y`). O acervo que existia quando a fonte entrou (a base plantada) não é demanda: só o que
   muda depois.
2. **Triagem**: o que é demanda em cada fonte está no `instructions.md`. Mensagem de pessoa: transcrever na íntegra
   (`**Pergunta de <nome> (<canal>, dd/mm HH:MM):**` + o texto) e, logo abaixo de uma linha `Rascunho:` sozinha, a
   resposta em texto puro, impressa por inteiro: sem bloco de código (o celular não mostra), sem blockquote, sem
   markdown. Dizer se depende de algo do usuário. Link solto não é resposta; conferir a PR, o card ou o código antes de
   afirmar o que fazem. Ids sempre com título. O agent **nunca** responde, comenta, aprova ou mergeia: ele aponta; o
   usuário age. As exceções (status no próprio perfil, autonomia aprovada) estão no `instructions.md`.
3. **Marcar o acordar** (KPI de precisão): no fim da triagem de um tick que acordou a sessão, `$A --kpis acordou util`
   quando virou ação (rascunho, resposta, PR, tarefa, decisão) ou `$A --kpis acordou ruido`. Uma marca por tick;
   `$A --kpis semana` mostra os indicadores.

Não acordam a sessão (o runner filtra): tick só com linhas informativas (presença, republicação da daily, gravação sem
pedido, status de almoço posto ou mantido `LUNCH: status ...`, presença do Slack trocada `PRESENCA: away|auto no Slack`,
`AVISO (sugestoes): ...`), chat de sala sem menção (`+N msgs sem mencao em X`) e e-mail só automático. Essas linhas ficam
no `avisos.log` do runner e saem no tick.out quando outra coisa acorda; falha do gancho (`nao consegui setar`) acorda.
Tick só com `✓ resolvida` conta como nada novo.

## Fila de pendências

A fonte enfileira sozinha o que é demanda (DM, menção ou resposta na sua thread sem resposta sua; comentário de outro
em issue sua; ação sua numa ata com prazo curto; PR sua fora de Draft, CI verde e sem review há `lembrar_horas` horas
úteis) e resolve sozinha quando você responde. Lembrete depois de `lembrar_horas` horas úteis (horário `comercial`,
seg a sex, no fuso `fuso_horas`), no máximo `max_lembretes`; depois fica silenciosa e só aparece em `--pendentes`.
Fonte quebrada não cobra. **Podar no mesmo tick em que entra** o que não é demanda ("ok", "thanks", menção genérica,
convite já aceito). Lembrete (`-- pN ... lembrete k/N`) = repetir em uma linha o que está pendente e o rascunho, se
ainda não foi proposto. Ao resolver, dizer o estado da fila (`fila zerada` / `restam pN, pM`).

```bash
$A --pendentes
$A --resolver pN
$A --pendente pN status=ignorar nota="..."        # status=aberta reabre
```

## Lições (`== LICOES`, gancho `licoes`)

No primeiro tick a partir do fechamento (18:00) de dia útil, o gancho imprime a lição do dia anterior (conferir se foi
aplicada) e as pistas do dia. Reler o dia na sessão e escrever 3 a 6 itens (dia sem fricção: "nada a mudar"), cada um
`[aplicada em <onde>]`, `[proposta] <regra, limite e critério de segurança>` (amplia a autonomia: espera o OK),
`[a aplicar]` ou `[recusada]`. Gravar com `$A --licoes set "<itens>"` e mostrar em até 10 linhas. `--licoes propostas`
lista as propostas abertas desta instância, reapresentadas em toda retro até virarem aplicada ou recusada.
Regras gerais em `~/.config/watch-core/docs/lessons.md`.

## Custo e tempo por tarefa (gancho `custos`)

O gancho lê os transcripts da sessão e atribui cada turno à tarefa marcada; o custo é em tokens (dólar só com
`"unidade": "usd"` no gancho). Turno de tick do runner vai sozinho para "triagem".
- Ao começar a trabalhar numa tarefa, marcar no mesmo turno: `$A --tarefa <SIGLA>-N` (sem tarefa: um rótulo curto do
  assunto, como `planejamento`). Trocou de assunto, marca de novo; terminou sem outra, `$A --tarefa -`.
- Subagente lançado para uma tarefa: `$A --tarefa <SIGLA>-N --agente "<parte da descrição>"` logo depois de lançar.
- Esqueceu de marcar: `$A --tarefa <SIGLA>-N@HH:MM`, com a hora em que começou.
- Relatório: `$A --custos hoje|ontem|semana|mes|AAAA-MM-DD[..AAAA-MM-DD] [--csv arq]`.

## Planou (bloco `planou` do config)

O tick pesado publica a lista da seção `## <SIGLA>` da página do dia como tarefas, abre em Precisa de você as decisões
e o que o agent precisa do usuário, manda as ferramentas (fontes, credenciais e vencimento) e o sinal de vida. Concluir,
reabrir ou editar uma tarefa no Planou vale como a caixinha da página. O que chega além do núcleo:

- `-- DECISAO d-N <PID>: opcao X ...` ou `resposta livre "..."`: o usuário decidiu; agir como se ele tivesse marcado a
  opção (ou respondido no terminal) e fechar com `$A --acao done N "<opção ou resposta>"`. Pergunta da sessão
  (`(pergunta da sessao)`) não fecha ação.
- `-- ENVIEI r-N`: rascunho enviado, a pendência já foi resolvida. `-- DESCARTADO r-N`: perguntar só se ainda fizer
  sentido. `-- ALERTA VISTO`: nada. `-- ATRIBUIDA`: tarefa nova para a instância, registrar com `$A --acao add`.
- Decisão com recomendada dentro da autonomia desta família (núcleo, "Decisão com recomendada: seguir sem esperar"):
  seguir a recomendada, avisar em uma linha e registrar com `$PL pergunta --auto ... --recommended A [--task aN|pN]`.
  O que o limite abaixo reserva ao usuário (PR pronta, merge, deploy, cluster, GMUD, mensagem) continua pergunta
  direta, que vira pedido com as opções e a recomendada marcada (núcleo, "Pergunta ao usuário vira pedido com opções"):
  `$PL pergunta --title "<uma linha>" --option "A=..." --option "B=..." --recommended A [--task aN|pN]`; respondida aqui
  antes, `$PL cancel <código>`. A resposta dela não fecha ação com `--acao done`, mesmo com `--task`.
- Limite ou defeito do Planou ou do plugin (não da empresa): `$PL sugestao` (núcleo), sem nada da empresa no texto.
- **Pronta ou backlog**: tarefa nova nasce em Backlog. Pedido explícito de uma pessoa (ou do usuário), dentro da
  autonomia e com os dados completos: `$PL pronta pN|aN "<quem pediu o quê>"` (`pronta pN -` desfaz).
- **Rascunhos**: todo `Rascunho:` também sobe: `printf '%s' "<texto>" | $PL draft --channel teams|slack|email|chat
  --to "<quem>" --title "<uma linha>" --task pN --ref pN --text -`. O texto só sobe com `"drafts_in_planou": true`.
- Fila do agente (`"task_queue": true`): comportamento `planou-queue`, com o limite desta família: PR **em rascunho**
  com os testes passando, e nada de marcar pronta, merge, deploy, cluster, GMUD ou mensagem sem o OK do usuário, a não
  ser o que a autonomia do `instructions.md` liberar.

## Espaço de trabalho

`"workspace"` do config (`{instance}` = o nome curto da empresa; padrão `~/WorkWatch/<empresa>`): `CONTEXT.md` (ler ao
abrir, atualizar quando algo deixar de ser verdade), `artifacts/` (todo arquivo que o agent gera para o usuário, nunca só
no scratchpad), `messages/` (captura; `messages/shared/` guarda o material que pessoas compartilham, citado no tick como
`[guardado: caminho]`), `meetings/`, `decisions/`, `journal/`.

```bash
$A --workspace                      # onde está e o que tem
$A --workspace init                 # cria pastas, CONTEXT.md e README.md (não sobrescreve)
$A --workspace artifact ARQ [NOME] [--task pN|aN|PID]
                                    # copia com o prefixo de data e imprime o caminho; com --task, anexa na tarefa
$A --workspace meetings [--dry]     # copia as reuniões transcritas desta instância
```

**Arquivo de uma tarefa vai como anexo dela.** Todo arquivo local que o agent produz para uma tarefa (análise, proposta,
rascunho, ata, previsão) vai para `artifacts/` com `--task <tarefa>`, ou, se já está gravado, `$PL attach <tarefa> <ARQ>`.
Sem isso o usuário vê no Planou só um caminho que não abre. O tick também anexa o que a descrição de uma tarefa cita
(`/caminho/arquivo.md` ou entre crases) e troca o caminho por `[ver anexo: <nome>]`; só manda de novo quando o arquivo
muda (sha256). Com `"confidentiality": "minimum"` (o padrão do work-watch) nada sobe: o caminho fica só na sessão.

## Outros modos

```bash
$A --dry | --json | --so <fonte> | --since 4h
$A --github all | --github show repo#N        $A --jira all | --jira show KEY
$A --diario                                   # resumo diário do Jira agora, sem gravar
$A --lunch on|off | --away on|off             # ganchos do Slack
$A --acao [list|add "texto" ...|done N]
$A --licoes [regras|propostas|tudo|set "texto"]
$A --backfill-links [--dry]                   # link das pendências antigas de Slack/Teams
```

As opções de cada fonte e gancho estão no docstring do módulo em `scripts/sources/` e `scripts/hooks/`. Adapter só da
empresa: `<pasta da instância>/adapters/<tipo>.py`.

## Migração a partir do plugin work-watch

`agent.py work-watch-<x> --migrate --cwd <pasta da sessão>` move `~/.config/work-watch/<x>` para
`~/.config/agent/work-watch-<x>` e deixa um link no lugar antigo; as chaves em português do config continuam valendo.
Com o runner antigo ainda vivo, o runner deste plugin recusa subir: parar o antigo antes
(`runner.sh <x> stop` do work-watch). Desfazer: parar o runner deste plugin e `agent.py work-watch-<x> --undo`.
Comandos antigos `watch.py <x> <flags>` e scripts de vigia arquivados: usar `$A <flags>` (mesmas flags).

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido.

```permissions
tools: subagent:worker
actions: planou.task, planou.note, notion, presence
```
