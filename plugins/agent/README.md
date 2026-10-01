# agent

Plugin do Claude Code para os agents do time: um plugin, uma instância por agent. O que muda de um agent para outro
(fontes, ganchos, comportamentos, ferramentas, autonomia) está na configuração da instância. Desenho e plano de
migração: [docs/agent-plugin-design.md](../../docs/agent-plugin-design.md).

Estado (0.26.0): núcleo com runner, fila e conversa do Planou; instância dev de qualquer projeto pelo config (`repos` com testes, release, critério de pronto e `public`; `--brief` monta o pedido ao worker); comportamentos de desenvolvimento (`dev-worker`, `batch-release`), revisão (`code-review`), QA (`qa`), produto e sugestões; ganchos de release, deploy e saúde. work-watch, job-scout (desde a 0.59.0) e travel-agent (desde a 0.60.0) são instâncias deste plugin.

## Uma instância

```
~/.config/agent/<instância>/
  config/config.json      o que o usuário decide (modelo em skills/agent/config-example/config.json)
  config/instructions.md  regras da instância, lidas pela sessão
  secrets/planou.env      PLANOU_AGENT_KEY=... (0600)
  behaviors/<nome>/       comportamento só desta instância (opcional)
  adapters/<tipo>.py      fonte ou gancho só desta instância (opcional)
```

Sem `~/.config/agent/<nome>/`, vale a pasta antiga do agent (`~/.config/work-watch/<x>/`, `~/.config/<nome>/`).

## Usar

```bash
S=<pasta da skill agent>
python3 $S/scripts/agent.py <instância> --validate   # confere o config
python3 $S/scripts/agent.py <instância> --load       # o papel por camada (Regras, Instruções, Habilidades, Memória), na ordem de precedência
python3 $S/scripts/agent.py <instância> --public-repos  # os repositórios públicos (`public: true` e o `public_repos` antigo)
python3 $S/scripts/agent.py <instância> --brief [repo] [--role batch-release] [--model sonnet|default] [--files "<arquivos>"]  # o pedido ao worker, só com o que o papel permite
python3 $S/scripts/team_agents.py list        # o time em ~/.config/team/agents.json (o que o `team` e o chief-of-staff leem)
python3 $S/scripts/team_status.py [--json]    # o time numa tabela: sessão, runner, último tick, tokens e versão (o `team-status`)
bash $S/scripts/team.sh <agente> [new]       # o launcher `team`: abre ou retoma a sessão do agente (uma por agente, com trava)
python3 $S/scripts/team_lock.py explain <agente>  # quem segura a trava do `team`: a sessão rodando ou um shell ocioso que sobrou
python3 $S/scripts/agent.py <instância> --evals      # casos de referência dos papéis da instância, em modo seco
python3 $S/scripts/agent.py <instância> --docs       # o manifesto da aba Papel do Planou, como o heartbeat manda
python3 $S/scripts/evals.py [comportamento ...]      # os mesmos casos, de todos os comportamentos do plugin
python3 $S/scripts/agent.py <instância>              # um tick (modo teste = dry)
bash $S/scripts/runner.sh <instância>                # runner em background (instância live)
bash $S/scripts/runner.sh <instância> stop
```

O launcher `team` mora em `scripts/team.sh` desde a 0.49.1 (PLN0215). O `~/.local/bin/team` é só um atalho que roda o
arquivo da cópia do plugin em uso, e o `git pull` nessa cópia já atualiza o launcher:

```bash
#!/usr/bin/env bash
exec bash "<cópia do repositório>/plugins/agent/skills/agent/scripts/team.sh" "$@"
```

O shell que o launcher deixa (depois do "já está rodando" ou quando o claude sai) nunca fica com a trava. Se a trava
estiver presa por um shell ocioso de um launcher antigo, o `team` diz o pid, o terminal e como liberar (fechar aquele
terminal, ou `kill -KILL <pid>`; um `kill -HUP` não basta), e o `team-status` mostra "trava de shell ocioso" em vez de
sessão aberta.

Drive do Windows sem resposta (`/mnt/<letra>`, PLN0236): um stat ou open num drive que parou de responder deixa o
processo em estado D para sempre (nem o `kill -KILL` derruba). Por isso o tick confere antes, num processo filho com
prazo (`scripts/drive_probe.py`, 5 s), os caminhos de cada fonte que moram num drive (os do config da fonte, o
workspace e, na `gravacoes`, as pastas de vídeo e de arquivo): drive sem resposta vira `FONTE QUEBRADA (<fonte>): drive
<X> sem resposta` e o tick segue com as outras fontes. O `team-status` lê as travas pelo nome do link em
`/proc/<pid>/fd`, sem seguir o link, e não lê a pasta do runner num drive sem resposta ("drive X sem resposta"). A
sonda que ficou presa não é refeita a cada tick enquanto estiver viva (`~/.cache/agent/drive-probe/<X>.pid`).

Arquivo num drive do Windows (PLN0245): a pasta pode responder e um arquivo dentro dela não. Toda leitura que as fontes
fazem num `/mnt/<letra>` passa por `watch_core/slowfs.py`, num processo filho com prazo (20 s, `AGENT_FS_READ_S`):
prazo estourado vira `FONTE QUEBRADA (<fonte>): drive <X> sem resposta (lendo <arquivo>)`. Na `gravacoes`, as pastas de
vídeo e de arquivo são lidas só por metadados; um vídeo só é aberto (`ffprobe`, uma vez), transcrito ou arquivado
quando tamanho e data não mudaram desde o tick anterior e a data tem mais de 60 s (`ATA_ESTAVEL_S`), sem `.part` ou
`.tmp` ao lado. A cópia para a pasta de arquivo roda num processo à parte (`watch_core/archive_job.py`), fora do tick.

Tick preso (PLN0241): se mesmo assim o tick pesado ficar preso (estado D, ou sem terminar), o runner não espera por ele
para sempre. Ele confere o processo por `/proc`, sem `wait` bloqueante, e depois de `RUNNER_TICK_STUCK_S` (padrão: o
teto do tick mais 30 s, e nunca depois do fim do ciclo) deixa o tick para trás: anota em `cache/runner/tick.preso` (pid,
início, estado e o `wchan` da thread em D), abre um alerta médio no Planou "tick de <instância> preso (<wchan>)", um por
pid, e acorda a sessão uma vez com `== TICK PRESO (pid, desde, wchan)`. Enquanto esse processo viver, nenhum tick novo
abre (a conferência volta a cada minuto, sem acordar); quando ele some, a linha `tick preso ... terminou` vai no próximo
acordar. Chamada ao Planou presa por mais de 60 s (`FG_MAX_S`) também é deixada para trás, com uma linha no `avisos.log`.

Linha dura do ciclo (PLN0235, PLN0237): toda saída do runner vem antes de `RUNNER_MAX_S` + `RUNNER_MARGIN_S` (padrão
25 + 4 min = 29 min), antes do limite de 30 min das tasks em background do Claude Code. Os últimos `RUNNER_RESERVE_S`
(padrão 120 s) ficam para o que vem depois do tick pesado. Um tick pesado só começa quando o teto dele (mais os 20 s do
`timeout -k`) acaba antes disso, e o teto é limitado para caber sempre num runner recém-lançado; tick vivo nessa hora é
tick preso. Depois do tick, cada passo (blocos agrupados, rollout, chamadas ao Planou) só usa o tempo que sobra, e o
rollout fica para o próximo tick quando falta tempo. Uma espera longa do Planou pendurada também é cortada nessa hora.

Blocos agrupados (PLN0247): um comportamento pode declarar em `behaviors/<nome>/wake.json` (e a instância acrescentar
com `"wake"` no `config.json`, mesmas chaves) blocos que não acordam a sessão sozinhos. O runner tira esses blocos do
tick pesado e os guarda em `data/retido.json` (`hold`: todos; `latest`: só a cópia mais nova, e sai quando o tick
não o imprime mais); a próxima acordada, qualquer que seja (outro bloco no tick, Planou, tick preso), traz o que está
guardado no topo do `tick.out`, como conteúdo a tratar, e só então apaga o arquivo. O guardado acorda sozinho com uma
linha `urgent`, quando o bloco mais velho passa de `hold_max_min` (padrão 180), quando passa de `hold_max_kb` (padrão
64; nada é descartado) e no primeiro tick pesado de uma sessão. Tick que só guardou entra no `metricas.tsv` como tick
vazio. `broken_repeat_min` diz quando uma `FONTE QUEBRADA` já conhecida acorda de novo (padrão 60; 0 = só fonte nova, ou
a que quebrou de novo depois de voltar). O job-scout agrupa as vagas que passaram no filtro (`== VAGAS`, `== MERCADO`,
`== RECOMENDADAS`), os pedidos de reputação e de processo seletivo, o `== GMAIL ATRASADO` e a linha de sincronia do
Notion; TOP APPLICANT acorda na hora. Regras: `python3 $S/scripts/wake.py rules --root <pasta da instância>`.

Na sessão: `/agent <instância>` (skill `agent`).

Decisão com recomendada que cabe na autonomia (o `can` do config e o da tarefa, valendo a mais restrita) não trava o
agente: ele segue a recomendada, avisa em uma linha e registra a decisão já tomada no Planou com
`watch_core.planou ... pergunta --auto --title "..." --option "A=..." --option "B=..." --recommended A`, que manda um
alerta baixo "Decidi: <pergunta> -> <opção>" e não deixa pedido esperando em Precisa de você. O que está em
`ask_first` ou `never` (mensagem a pessoas, merge não aprovado, produção, apagar dado ou segredo, criar conta, abrir o
navegador) e o que depende de uma ação pessoal do usuário continuam como pedido com opções.

## Instalar com um comando

Num computador novo, um comando instala o plugin, a extensão Team Terminals do VS Code e o provisionador local como
serviço do usuário, e no fim pede o código de pareamento do Planou (Configurações › Computadores › **Conectar este
computador**). Sem conta nova e sem segredo no terminal: o código é digitado oculto e vai para o
`provisioner.py pair` pela entrada; a credencial vai do Planou direto para um arquivo 0600.

Linux, WSL e macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/davibauer/planou-agent/main/install.sh | sh
```

Windows (PowerShell; os agentes rodam no WSL, que precisa estar instalado com uma distribuição e o seu usuário Linux):

```powershell
irm https://raw.githubusercontent.com/davibauer/planou-agent/main/install.ps1 | iex
```

| O quê | Linux e WSL | macOS | Windows |
|---|---|---|---|
| plugin agent | cópia do repositório em `~/.local/share/planou/claude-plugins` (git, ou o arquivo `.tar.gz` sem git), as skills `~/.claude/skills/agent` e `work-watch` apontando para ela, o `team` em `~/.local/bin` e o `~/.config/team/agents.json` vazio | igual | igual, dentro do WSL |
| extensão Team Terminals | `extensions/vscode-team-terminals/dist/team-terminals.vsix` por `code --install-extension`; depois ela se atualiza sozinha da mesma cópia | igual | pelo `code` do WSL, que instala no VS Code das janelas do WSL |
| provisionador | `systemd --user` (`agent-provisioner.service`) | `launchd` (`~/Library/LaunchAgents/app.planou.agent-provisioner.plist`, log em `~/Library/Logs/agent-provisioner.log`) | tarefa agendada do usuário, ao entrar no Windows, que roda o provisionador no WSL (`provisioner/wsl-provisioner.sh`) e mantém o WSL de pé; com o systemd ligado no WSL, a unit roda e a tarefa só mantém o WSL |

- Rodar de novo atualiza tudo: `git pull --ff-only` na cópia, a extensão e o serviço. Computador já conectado mantém a
  credencial e não pede código; o serviço reinicia na versão nova.
- O que não é do instalador fica como está: uma skill `agent` que já aponta para outra cópia, um `team` que já existe
  e um `agents.json` que já existe.
- O serviço só liga depois do pareamento. Sem terminal (ou com Enter vazio), o instalador mostra o comando para
  conectar depois: `python3 <cópia>/plugins/agent/skills/agent/scripts/provisioner.py pair --base-url <Planou>`
  (sem o código no comando: ele pergunta, oculto).
- Opções: `curl ... | sh -s -- --base-url URL` (padrão `https://app.planou.com`), `--no-vscode`, `--no-pair`,
  `--service none`, `--dir DIR`, `--ref REF`; no Windows, `-BaseUrl`, `-Distro`, `-NoVSCode`, `-NoPair` (com
  `& ([scriptblock]::Create((irm <url>))) -BaseUrl URL`). A lista inteira está no começo de cada script.
- Pré-requisitos: `python3` 3.8 ou mais novo e `git` ou `curl`; o Claude Code (`claude`) para os agentes rodarem. No
  WSL sem systemd, ligue `[boot] systemd=true` em `/etc/wsl.conf` ou instale pelo `install.ps1` do Windows.
- No macOS, o provisionador roda, mas a conferência de runner de pé lê `/proc`, que o macOS não tem: o estado do runner
  na tela Time não vale lá.

## Provisionador local

Agente novo pedido na tela Time do Planou (**Novo funcionário**) nasce sozinho na máquina: o serviço
`scripts/provisioner.py` (systemd do usuário) cria `~/.config/agent/<nome>/` a partir do modelo dev, retira a chave uma
vez para `secrets/planou.env` (0600), roda `--validate` e o `config-snapshot`, põe o agente no
`~/.config/team/agents.json` (a extensão Team Terminals abre o terminal) e informa cada passo ao Planou. Pausar,
retomar e remover também saem da tela. Nasce em modo teste, a não ser que a pessoa marque ligar. O serviço roda da
cópia do plugin em uso e se atualiza sozinho: entre uma volta e a próxima, se o canário (`rollout.json`) liberou outra
versão (ou a cópia em uso mudou, sem canário), ele sai com o código 75 e o systemd (`Restart=always`) o sobe na versão
liberada; versão recusada não entra. A unit é a mesma em toda versão; se mudar, o próprio provisionador a regrava e
pede `daemon-reload`. Os agentes que já rodavam na máquina (os do `agents.json` e as instâncias do plugin) são
adotados pelo inventário: o provisionador informa o runner de cada um (de pé, parado, caiu) e segue o Ligar e o Desligar
da tela, sem nunca criar, mudar a config ou apagar um deles. Funcionário criado por ele que saiu deste computador
(mudou de computador ou foi para outro depois de uma revogação) tem o runner parado e a instância arquivada em
`~/.config/agent-provisioner/departed/`, só numa resposta válida da lista do Planou. Instalar, ligar e a
configuração: [docs/provisioner.md](docs/provisioner.md).
`provisioner.py pair` conecta o computador com o código de Configurações › Computadores (sem o código no comando, ele
pergunta oculto ou lê da entrada): grava a credencial (0600, sem mostrar) e o `base_url`; com ela, o provisionador fala com `https://app.planou.com/v1` de qualquer máquina.

## Config (schema 1)

| Chave | Para quê |
|---|---|
| `schema`, `live` | versão (1); fora de `live: true` a instância está em modo teste |
| `language`, `tz_hours`, `business_hours` | idioma, fuso, horário comercial |
| `interval_s` | tick pesado (60 ou mais) |
| `session` | `cwd`, `aliases`, `rotate`: onde o launcher abre a sessão, quando a entrada da instância em `~/.config/team/agents.json` não diz (o arquivo vale primeiro; ver `scripts/team_agents.py`) |
| `workspace` | pasta do usuário com o `CONTEXT.md` |
| `sources`, `hooks` | adapters, `{type, ...}` |
| `behaviors`, `behavior_config` | comportamentos carregados, na ordem, e as opções de cada um |
| `tools` | `{kind, name}`: subagent, cli, skill, mcp, other |
| `autonomy` | `can`, `ask_first`, `never` |
| `repos` | `path`, `name`, `worktrees`, `base`, `rules`, `shared_rules`, `gh_account`, `tests` (`filtered`, `full`), `release` (`batch`, `pr`, `ci`: a PR com o rótulo `automerge` entra e é publicada pela CI, e o worker não espera; `none`), `fragments` (onde o worker escreve o texto do changelog), `done`, `public` (repositório público: o `code-review` e o `--brief` leem), `map` (mapa curto do repositório, relativo a `path`; sem ele, `docs/MAP.md` quando existe: a linha `MAPA` do `--brief`): o projeto de uma instância dev (`dev-worker`, `batch-release`, `--brief`). Modelo em `config-example/dev/` e passo a passo em [docs/dev-template.md](docs/dev-template.md) |
| `planou` | `project`, `confidentiality`, `drafts_in_planou`, `publish`, `task_queue`, `conversation`, `links` (regras de link extras para o Planou, `[{pattern, url, label}]`) |
| `runtime` | `python` (`system` ou `venv`), `requirements` |

As chaves em português do work-watch (`fontes`, `ganchos`, `intervalo_s`, `fuso_horas`, `comercial`, `sigla`) valem
como apelido.

## Comportamentos

| Nome | O que faz |
|---|---|
| `planou-queue` | fila do agente no Planou: `fila ver`, `started`, delegar, `in_review` (libera a vaga), `blocked`, `done` conforme a autonomia (dono de coluna com próximo estado: o `done` vira `handoff` e passa a tarefa adiante). Cada worker delegado aparece na aba Fila em "Workers agora": `fila worker-start <PID> --role dev\|integrator\|other --label '...'` logo depois do `Agent(worker)` imprime a key (guardada em `cache/planou/workers.json`), a entrega com a mesma key (`fila worker ... --key`) o fecha (com `--phases-from <output_file do Agent>` manda também o tempo por fase do worker, medido no registro dele), `worker end <key> --result ...` fecha o que não tem tarefa ou é de papel `other` e `worker ping <key>` dá sinal de vida. O tick pesado de uma sessão nova fecha no Planou os que uma sessão que morreu sem `session_closed` deixou abertos (`-- WORKER INTERROMPIDO`) |
| `dev-worker` | como montar o pedido ao subagent `worker`: repositório, worktree, regras, critério de pronto |
| `batch-release` | release em lote: quando disparar o worker integrador, o que ele cumpre (trava das suítes, e2e completo no máximo uma vez por intervalo, fragmentos de changelog, fast-forward antes da tag, deploy com trava, log de deploys) e o que fazer quando ele volta; todo deploy novo vira aviso ao usuário (Conversa do Planou e sessão) com o que mudou e onde conferir (`check_url`), e fecha as tarefas que entraram na versão. O nome antigo `deploy-notice` ainda vale como sinônimo (o `--validate` avisa) |
| `retro` | a retro do projeto no Planou (Cerimônias): o convidado manda a contribuição a partir dos fatos medidos no Planou e dos próprios dados (`retro dados`, `retro contribuir`), toda proposta citando PIDs reais; na retro com votação, cada convidado vota nos itens de mais efeito (`retro votar`); quem facilita junta as contribuições, começa pelos mais votados e manda a ata com no máximo 3 ações (`retro ver`, `retro ata`), que nascem no backlog para o usuário aprovar, e lições que viram proposta de papel do agente. Vale sem ligar no config: quem convida é o Planou |
| `refinement` | o refinamento do backlog no Planou (Cerimônias): cada convidado lê o Backlog do projeto e sugere estimativa, quebra em tarefas menores e o selo "o agente pode fazer", sempre com o motivo (`refino ver`, `refino sugerir`); quem facilita junta numa lista só, uma sugestão de cada tipo por tarefa (`refino lista`), e o usuário aprova cada uma na tela. Vale sem ligar no config: quem convida é o Planou |
| `fila_parada` | acorda a sessão com `== FILA PARADA <PID> (<motivo>)` quando uma tarefa `queued` fica sem ser liberada por mais de `after_min` minutos, sem `blocked_by` aberto, com vaga livre (`busy` menor que `wip` na fila do Planou, a mesma resposta do `fila ver`). O Planou não manda o horário, então o relógio começa no primeiro tick que vê a tarefa parada. O motivo é o `release_reason` (ou `reason`) quando o Planou manda; senão, `sem motivo visível`. Uma vez por tarefa: só avisa de novo se ela sair da condição e voltar. Quieto com o agente pausado no teto de custo e com o Planou fora do ar | `after_min` (20) |
| `suggestions` | as sugestões dos outros agents (`sugestao`, limite ou defeito do Planou e dos plugins) viram tarefas no backlog do projeto Planou, reportadas por quem sugeriu; o que dizer ao usuário com `== SUGESTOES` |
| `work-watch` | pendências de trabalho de uma empresa (vindo do plugin work-watch): triagem das fontes, fila de pendências com lembrete em horário comercial, Planou (lista de tarefas, decisões, rascunhos), espaço de trabalho, lições, custo e tempo por tarefa. Liga o tick do work-watch: `--pendente`, `--backfill-links`, `--workspace`, `"separador": "fixo"` e o Planou do `planou_tick.py` |
| `job-scout` | olheiro de vagas (vindo do plugin job-scout): busca pública do LinkedIn e sites de vaga remota (e, se ligadas, inbox e recomendadas do LinkedIn logado), julgamento de cada vaga contra o `profile.md`, reputação, faixa, matriz de aderência, candidatura, currículo e funil (Planou e, se ligado, Notion). Os scripts vêm iguais em `behaviors/job-scout/scripts/`; o `agent.py job-scout` roda o `scout.py` num processo próprio. Intervalo mínimo de 30 min. Aceita o `"schema": 2` e as chaves de critérios do config do job-scout |
| `travel-agent` | agente de viagem (vindo do plugin travel-agent): grade de preços das passagens no Google Flights com alertas de queda, meta e referência, alertas do Google Flights e promoções de milhas no Gmail, milhas contra dinheiro, comparação com a viagem anterior, hotéis, dólar, prazos e planilha. Os scripts vêm iguais em `behaviors/travel-agent/scripts/` (só o `paths.py` muda); o `agent.py travel-agent` roda o `agent.py` dele num processo próprio, com o Python do venv da instância. Intervalo mínimo de 3 h. Aceita as chaves do config do travel-agent |
| `daily-report` | a daily do fechamento (`== DAILY`) e as tarefas na página do dia (`== TAREFAS`): fala, decisões com opções, baixa por evidência. Opções: `language`, `closing_hour`, `speech_only`, `tasks` |
| `recordings` | gravações do OBS x agenda (`== GRAVACOES`): ata no mesmo tick, ações suas, agenda em cache. Opções: `calendar`, `query_hours`, `push` |
| `push-alert` | aviso ativo no celular (uma linha por tick, contar e não descrever) e a triagem leve de cada item. Opções: `prefix`, `max_chars`, `triggers`, `quiet` |
| `code-review` | revisor independente, dono da coluna de revisão: lê a PR que o handoff do dev trouxe (`pr_url`; pelo PID só quando ela não vem), confere pedido, testes, segurança, dado de cliente em repositório público, legibilidade e tamanho (`scripts/review_check.py`), comenta o parecer na PR e aprova (`fila done`, handoff para o release) ou devolve direto ao dev (`fila ajuste <PID> --text ...`, Planou 0.43.0; `fila blocked` quando a tarefa não veio por handoff). Nunca escreve código, merge ou deploy. Repositório público: `"public": true` em `repos` (o `public_repos` antigo continua valendo; `--public-repos` lista). Opções: `max_diff_lines`, `terms_file`, `public_repos`, `require_tests`, `pr_comment` |
| `product` | agente de produto: a meta (ou o problema) da pessoa, pela fila ou pela Conversa, vira tarefas pequenas no Backlog do projeto, cada uma com o que, por quê e critério de pronto, dependências, prioridade sugerida, selo "o agente pode fazer" e `Decidir:` para o que é da pessoa, num sync só (`scripts/backlog.py <instância> <plano.json>`, `--dry`, `--update`, `--list`); em projeto autônomo, com `ready_reason`, direto em A fazer ou na coluna do dev; devolve a meta com os PIDs criados. Nunca escreve código. Opção: `max_tasks` (padrão 8; acima pede OK). Modelo de instância e passo a passo em [docs/product.md](docs/product.md) |
| `process-coach` | coordenador de desenvolvimento: mede o fluxo do projeto pelo Planou (`GET /v1/agent/projects/<projeto>/flow`, Planou com a PLN0176) e todo dia aponta o que saiu do normal; toda semana, o resumo com o lead time por estado (criação até concluída, saída do Backlog até concluída), o maior gargalo com 3 casos (PIDs e horas), vazão, retrabalho, impedidas, espera das decisões, custo e passos por entrega, e no máximo 3 propostas, cada uma com a métrica e o valor de base (`scripts/process_report.py <instância>`, `--from-file`, `--json`, `--plan-out`, `--record`, `--followup`, `--skip`). Ajuste de parâmetro (WIP, lote, prioridade) e mudança de papel vão ao Planou como proposta (`--propose`, `POST /v1/agent/proposals`, Planou 0.53.0): a pessoa aprova num clique em Precisa de você e o Planou aplica, com registro e Desfazer; o evento `proposal_changed` avisa o resultado. O que o Planou recusa volta como `Decidir:`, com aviso. As outras propostas viram tarefas no Backlog pelo `backlog.py`; na semana seguinte marca a que não funcionou (e só mede a proposta aplicada). Nunca escreve código. Opções: `project`, `dev_agent`, `deploys_log`, `report_weekday`, `report_hour`, `skip`. Modelo de instância em [docs/process-coach.md](docs/process-coach.md) |
| `qa` | agente de QA, dono da coluna de QA: sobe um ambiente descartável da branch da PR (`scripts/qa_env.py`: worktree destacada, `setup` e `env_up` da instância, URL pelo `env_url` ou da saída, app servido recusado), percorre o fluxo da tarefa como usuário com um script Playwright efêmero fora do repositório (`scripts/qa_kit.cjs`: axe, capturas nas larguras, alvos de toque, rolagem horizontal), compara com o critério de pronto e aprova (`fila done`, handoff) ou devolve (`fila ajuste`, que volta direto ao dev mesmo com uma revisão no meio; `fila blocked` com `send_back: pessoa`). Nunca escreve código, merge, deploy nem suíte completa. Opções: `setup`, `env_up`, `env_down`, `env_url`, `url`, `served_urls`, `node_dir`, `widths`, `min_target_px`, `axe_tags`, `up_timeout_s`, `pr_comment`, `send_back`. Modelo de instância e passo a passo: [docs/qa.md](docs/qa.md) |
| `tech-radar` | radar técnico (instância `tech-scout`): quatro fontes, uma por família (`radar_deps`: versões novas das dependências dos repositórios do config no npm, NuGet e PyPI; `radar_feeds`: blogs oficiais e canais do YouTube por RSS; `radar_hn` e `radar_trending`: Hacker News e GitHub Trending só com o que cita uma das `stacks`), uma vez por semana e sem repetir notícia. Modo projeto: `scripts/radar.py <instância>` monta o plano da semana e o `backlog.py` o publica no Backlog do projeto do radar, sem selo. Modo coluna: a tarefa que entra na coluna "Radar técnico" ganha até 3 sugestões na nota (`radar.py --match -`) e segue a esteira, com prazo máximo. Busca externa só genérica; `--dry --fixtures` ensaia sem rede. Nunca escreve código. Opções: `stacks`, `project`, `column`, `per_task`, `max_hours`, `max_tasks`, `weekday`, `hour`, `window_days`. Modelo e passo a passo em [docs/tech-radar.md](docs/tech-radar.md) |
| `product-radar` | radar de produto (instância `product-scout`, cargo "Analista de produto (inteligência competitiva)"), irmão do `tech-radar` para o produto em vez da stack: duas fontes (`product_feeds`: changelogs, notas de versão e RSS públicos de apps de tarefas, de produtos de agentes, do Product Hunt e do Show HN, estes dois só com o que cita um dos `terms`; `product_github`: repositórios novos da semana nos tópicos do GitHub do config), uma vez por semana e sem repetir notícia. `scripts/product_radar.py <instância>` mostra os candidatos ao lado do que o produto já tem (tarefas do projeto pela `/v1/agent/projects/{key}/flow`, README e CHANGELOG de `planou_repo`); a sessão escreve as ideias e `--ideas` confere cada uma (link público, porquê, esboço de até 3 linhas, tamanho P, M ou G, risco), descarta o que já existe ou já foi proposto e guarda no máximo 3 por semana para o `backlog.py` publicar no Backlog, sem selo. Só a ideia: nunca marca, texto, visual ou código de outro produto. Opções: `project`, `max_ideas`, `terms`, `weekday`, `hour`, `window_days`, `planou_repo`, `known_days`. Modelo e passo a passo em [docs/product-radar.md](docs/product-radar.md) |
| `security` | agente de segurança (instância `security`), com o gancho `security`: dependências com falha conhecida (`npm audit`, `dotnet list package --vulnerable`, `pip-audit` se instalado), segredos em repositório e em log (arquivo, linha e tipo, mascarado: o segredo nunca sai), permissão das pastas e arquivos de segredo (só `lstat`) e autonomia dos agentes contra os papéis. Cada achado abre uma tarefa no backlog com a evidência; nunca corrige, gira chave nem apaga. Casos de referência em `evals/`. Passo a passo em `docs/security.md` |

As opções ficam em `behavior_config.<comportamento>`; o `--validate` confere o tipo de cada uma (tipo errado é erro,
opção desconhecida é aviso). As de comando (tipo `command`) e as de caminho (tipo `path`) só se editam no computador (ver
Aba Papel abaixo).

### Camadas do papel

O papel de uma instância tem quatro camadas, na ordem de precedência (em conflito vale a de cima):

1. **Regras**: o núcleo (o SKILL.md) e a autonomia do config (`can`, `ask_first`, `never`). Ninguém sobrepõe.
2. **Instruções**: o `instructions.md` da instância (e o `CONTEXT.md` do workspace), editável pela aba Papel.
3. **Habilidades**: os comportamentos ligados. Regra ou procedimento é o `kind` de cada um: `always` (lido em toda
   sessão) ou `on_demand` (o `--load` mostra só uma linha de índice com o nome, quando usar e o caminho; a sessão lê o
   arquivo quando vai usar). Ex.: `batch-release`, `retro` e `refinement` são sob demanda.
4. **Memória**: o resumo de passagem (`data/handoff.md`) e as lições. Informação, não regra.

O `--load` imprime nessa ordem, com a frase de precedência no topo. A aba Papel do Planou mostra as mesmas camadas: o
manifesto leva a autonomia (`autonomy`, fora da confidencialidade `minimum`) e o resumo de passagem como arquivo
`memory` com a data e as primeiras linhas (`excerpt`, até 6 linhas e 600 caracteres, fora da `minimum`). Um Planou que
ainda não conhece esses campos (até a 0.66.0) recusa o manifesto por eles, e o runner manda sem eles por um dia (sem
perder o nome e a frase das habilidades).

### Nome e frase de cada comportamento

Cada `BEHAVIOR.md` do plugin começa com um frontmatter que a aba Papel do Planou usa no cartão da habilidade:

```markdown
---
title: Fila no Planou
summary: Trabalha as tarefas atribuídas no Planou dentro do limite de vagas e presta contas em cada uma.
layer: skill
kind: always
---
# planou-queue: a fila do agente no Planou
```

`title` é o nome em português (uma linha, até 80 caracteres) e `summary` uma frase do que o comportamento faz (até
300); os dois são opcionais (um comportamento local sem eles aparece pelo nome técnico). `layer` (`rules`,
`instructions`, `skill` ou `memory`; sem ele, `skill`) diz em que camada o comportamento entra e `kind` (`always` ou
`on_demand`; sem ele, `always`) se é lido em toda sessão ou só quando usado; os dois vão no catálogo e a aba Papel
mostra o selo "sempre" ou "sob demanda". `when` (só num `on_demand`) diz quando ler; fica no computador (o `--load` o
usa no índice) e nunca vai ao Planou. O título depois do frontmatter continua sendo a `description`. O manifesto leva `title` e
`summary` no catálogo e em cada comportamento ligado; um Planou que ainda não os conhece (até a 0.64.0) recusa o
manifesto por esses campos, e o runner manda o manifesto sem eles por um dia e tenta de novo depois (fica uma linha
em `cache/planou/docs.log`). Os testes conferem que todo comportamento do plugin tem `title`, `summary`, `layer` e
`kind`, e que um `on_demand` tem `when`.

### Permissão por papel

Cada `BEHAVIOR.md` termina com um bloco `permissions` que declara as ferramentas (`kind:nome`, como em `tools`) e as
ações que o papel usa (`worktree`, `code`, `test.filtered`, `test.full`, `push`, `pr.open`, `pr.comment`, `pr.merge`,
`tag`, `publish`, `release`, `deploy`, `planou.queue`, `planou.task`, `planou.note`, `notion`, `notify`, `presence`;
`message`, `runner` e `delete` nenhum papel declara). Ler é sempre permitido. O mapa das palavras de cada ação está em
`scripts/permissions.py`.

- `--validate` avisa (nunca é erro) quando uma frase de `autonomy.can` dá uma ação que nenhum comportamento ligado
  declara, quando uma frase não casa com nenhuma ação conhecida, quando uma ferramenta de `tools` não é de nenhum
  comportamento ligado e quando um comportamento ligado não declara nada (aí a conferência não roda). `ask_first` e
  `never` só restringem e não são conferidos. Instância sem `behaviors` não é conferida.
- `--brief` passa ao worker só o papel dele: sem `--role`, o `dev-worker` (sem suíte completa e sem deploy, que são do
  integrador); `--role batch-release` para o integrador. As frases de `can` fora do papel vão para a linha
  `NAO E DESTE PAPEL`.

### Casos de referência

`behaviors/<nome>/evals/cases.json`: de 5 a 10 tarefas por papel (tarefas do Planou ou exemplos genéricos, nunca dado
de cliente), cada uma com as ações que pede, a decisão esperada (`faz`, `pergunta` ou `recusa`) e o resultado esperado.
Rode antes de trocar um `BEHAVIOR.md` ou o `instructions.md` de uma instância: `agent.py <instância> --evals` (ou
`scripts/evals.py`). O modo seco não chama modelo: confere cada caso contra a declaração do papel e a autonomia da
tarefa, e falha quando uma mudança tira do papel o que as tarefas dele pedem ou dá o que elas devem recusar. A resposta
real do modelo fica "não verificada" no modo seco; para conferir à mão, `evals.py --prompt <caso>` imprime o pedido e
`evals.py --grade <caso> <resposta>` confere a resposta. Os testes do plugin rodam os casos de todos os papéis, então
uma PR que mexe num `BEHAVIOR.md` passa por eles na CI. Todo papel tem casos, e o teste falha quando um comportamento
novo chega sem o `evals/cases.json` dele.

No Planou, a aba Testes do funcionário mostra esses casos como frase, com o resultado, e a aba Papel mostra o selo
"N situações conferidas" no cartão de cada habilidade. A cada tick pesado o runner monta o manifesto
dos arquivos que a sessão lê, na ordem (o `SKILL.md` do plugin, o `instructions.md`, o `CONTEXT.md` e cada
comportamento ligado, com tamanho, sha256 e data), e cada comportamento com casos leva o resultado de `--evals --json`
(quantos casos, quantos passaram, as falhas, cada caso com a decisão esperada e a que deu, o sha256 do `BEHAVIOR.md` e a
hora). O manifesto vai no heartbeat quando muda (rodar de novo com o mesmo resultado não conta) ou uma vez por dia.
`agent.py <instância> --docs` mostra o manifesto como ele sai. Vale também para as instâncias work-watch (tick do
Planou pelo `planou_tick.py`): o manifesto vai no mesmo heartbeat e as edições se aplicam do mesmo jeito.

Na aba Papel a pessoa edita o `instructions.md`, o `CONTEXT.md` (quando a instância tem) e liga ou desliga
comportamentos do catálogo (os do plugin e os de `behaviors/` da instância). Por isso esses dois arquivos vão com
`editable: true` e o conteúdo (UTF-8, até 200 KB), e o manifesto leva o `catalog`. As opções dos comportamentos (o
`behavior_config` do `config.json`) vão por último, como `behavior_config.json`, e cada comportamento do plugin com
esquema vai no catálogo com `options` (`{chave: {type, choices?, label?}}`, na ordem do esquema), com que a aba Papel
edita as opções num formulário: texto vai como `text`, inteiro como `int`, true/false como `bool`, opção de valores
fixos como `choice` e as de comando e de caminho como `command` (a tela mostra só leitura, "só no computador"); número
e listas vão com o nome do plugin (`num`, `strs`, `ints`), que o Planou ignora, e essas continuam no "Ver JSON". Os
limites do Planou (até 30 opções, chave até 60, rótulo até 120, de 1 a 30 escolhas de até 100) valem no envio. Um
comportamento local vai sem `options`, com "Opcoes: só no computador (comportamento local)." na descrição, e um do
plugin sem esquema vai sem `options` (edição em JSON). Precisa do Planou 0.61.0 ou mais novo: um Planou mais antigo
recusa o manifesto inteiro (`422 invalid_docs`, o sinal de vida vale e o manifesto anterior fica; o heartbeat não traz
a versão do Planou para o plugin decidir sozinho). Uma edição das opções só passa com
comportamentos do catálogo e, nos que têm opções conhecidas, só com elas e no tipo certo; o resto do `config.json` fica
como estava. As opções que viram comando executado no computador do agente (tipo `command` no esquema: `setup`,
`env_up`, `env_down` e `env_url` do `qa`; `deploy_cmd`, `test_lock` e `deploy_lock` do `batch-release`; `terms_file` do `code-review`) aparecem no
catálogo como "(só no computador)" e não mudam pelo Planou: a edição que muda, acrescenta ou tira uma delas é recusada
com "opção de comando: edite no computador", e a que mexe só nas outras se aplica com as de comando como estavam. O
mesmo vale para as opções de caminho (tipo `path`: o `node_dir` do `qa`, que vai no `NODE_PATH` e decide de onde o node
carrega código; `e2e_marker`, `fragments` e `deploy_log` do `batch-release`, arquivos que o integrador sobrescreve, apaga
ou acrescenta), recusadas com "opção de caminho: edite no computador", e para as opções de um comportamento local da
instância (`behaviors/<nome>/`, também a cópia local de um comportamento do plugin): ele não declara esquema, então nada
aqui diz como as usa, e a edição que muda, acrescenta ou tira alguma é recusada com "comportamento local: edite no
computador" até ele declarar um. Na confidencialidade mínima (o
padrão das instâncias work-watch) nenhum conteúdo sai da máquina, os casos de referência vão sem o texto da tarefa e
sem o porquê, e só a lista de comportamentos se edita. Ligar um comportamento numa instância work-watch sem a lista
`behaviors` mantém o `work-watch` ligado. O `SKILL.md` e cada `BEHAVIOR.md` ficam só leitura: um
comportamento muda por PR no plugin, para todos os agentes que o usam. A edição chega como o evento
`agent_docs_changed` e o tick pesado a aplica (`scripts/role_edit.py`): confere o sha256 do conteúdo e se o arquivo
daqui ainda é a versão de onde a edição partiu (`base_sha256`), roda o `config-snapshot --wait` (a variável
`AGENT_SNAPSHOT_CMD` troca o comando; vazia, desliga), grava o conteúdo exatamente como veio, confere o sha256 e roda
`agent.py <instância> --validate`. Se falhar, volta o arquivo anterior. Depois responde em `POST /v1/agent/docs/ack`
(`applied` com o sha256, ou `refused` com o motivo) e acorda a sessão com `== PAPEL MUDOU (n)`, dizendo o que reler, ou
`== PAPEL RECUSADO (n)`, com o motivo. A resposta que não chegou ao Planou é reenviada no tick seguinte, sem gravar de
novo (`cache/planou/role_edits.json`). Precisa do Planou 0.49 ou mais novo (um Planou mais velho recusa o campo
`evals` com 422 e o runner anota em `cache/planou/docs.log`, sem repetir o mesmo manifesto).

## Fontes e ganchos do work-watch

Os adapters do work-watch moram em `scripts/sources/` (GitHub, GitLab, Azure DevOps, Jira, Slack, Teams, Google Chat,
Outlook, Gmail, calendários, Kubernetes, gravações) e `scripts/hooks/` (daily, lições, tarefas, custos, KPIs,
credenciais, almoço, presença, `notion_compare`...), importados como `adapters.<tipo>`; as opções estão no docstring de cada módulo. O
plugin work-watch, que guardava cópias idênticas deles, saiu na E6.

Saída do Notion: o gancho `notion_compare` (desligado por padrão) compara uma vez por dia útil a página do dia com o
Planou, só lendo, e avisa quando a instância passou 14 dias úteis seguidos sem diferença. Levantamento, regras e como
ligar em [docs/notion-exit.md](docs/notion-exit.md).

Atalho `/work-watch <x>` (skill `work-watch` deste plugin): é `/agent work-watch-<x>`; `pendentes` vira `--pending`,
`resolver pN` vira `--resolve pN` e as outras opções passam iguais.

Migrar uma instância do work-watch (E3), com o tick pesado dela parado naquele instante:

```bash
python3 scripts/agent.py work-watch-<x> --migrate --dry                 # o que vai mudar
python3 scripts/agent.py work-watch-<x> --migrate --cwd <pasta da sessão>
python3 scripts/agent.py work-watch-<x> --undo                          # volta (runner deste plugin parado)
```

O `--migrate` move `~/.config/work-watch/<x>` para `~/.config/agent/work-watch-<x>`, deixa um link no lugar antigo e
acrescenta ao config só `schema`, `behaviors` e `session` (e o gatilho `<command-name>/agent` do gancho `custos`); as
chaves em português ficam, porque o runner antigo da sessão aberta segue lendo o mesmo arquivo pelo link até a sessão
ser relançada. O runner deste plugin recusa subir (e o `stop` recusa mexer) enquanto o runner antigo da mesma instância
estiver vivo.

## job-scout (E4)

O motor do job-scout veio inteiro: `skills/agent/behaviors/job-scout/scripts/` (`scout.py`, `criteria.py`, `matrix.py`,
`application.py`, currículo, LinkedIn, Notion...), com o `watch_core` deste plugin. `agent.py job-scout` repassa tudo ao
`scout.py` num processo próprio (os dois têm módulos `paths` e `schema`), com `AGENT_INSTANCE` no ambiente; em modo
teste (sem `"live": true`) o tick é sempre `--dry`. `interval_s` abaixo de 1800 vira 1800 (a conta do LinkedIn é
pessoal). A aba Papel do Planou mostra os arquivos só para leitura: o `scout.py` ainda não aplica edição vinda de lá.
Atalho `/job-scout` (skill `job-scout` deste plugin) é `/agent job-scout`. Testes: `tests/job_scout/` (vindos do plugin
antigo) e `tests/test_job_scout_migration.py` (mesmas respostas antes e depois da migração).

**Juiz de vagas num subagent** (0.62.0): a sessão do job-scout não julga vaga. Os blocos de vagas de cada tick, as
pesquisas de reputação e de processo seletivo e o passo do Gmail vão para um subagent por tick, o `job-judge`
(`agents/job-judge.md`, modelo sonnet) ou o `general-purpose` com sonnet enquanto o plugin não estiver instalado pelo
marketplace. As regras estão em `behaviors/job-scout/JUDGE.md`; o `judge.py --record` grava o lote de vereditos com um
`scout.py --job` só e devolve uma linha por vaga, com o detalhe em `data/judgments/<id>.md`.

Migrar uma instalação antiga, com o runner antigo parado (o runner deste plugin recusa subir enquanto ele estiver vivo;
o plugin job-scout saiu do marketplace na E6, então o `runner.sh` é o da cópia que ainda estiver instalada):

```bash
bash <cópia antiga do plugin job-scout>/skills/job-scout/scripts/runner.sh stop
python3 scripts/agent.py job-scout --migrate --dry                     # o que vai mudar
python3 scripts/agent.py job-scout --migrate --cwd <pasta da sessão>
python3 scripts/agent.py job-scout --undo                              # volta (runner deste plugin parado)
```

O `--migrate` move `~/.config/job-scout` para `~/.config/agent/job-scout`, deixa um link no lugar antigo e acrescenta ao
config só `behaviors` (`job-scout`, mais `planou-queue` com `planou.task_queue`), `live: true`, `interval_s: 1800` e
`session`; o `"schema": 2` e os critérios ficam, e uma cópia antiga do plugin job-scout ainda instalada segue lendo pelo link. O arquivo mantém a indentação dele (só entram as chaves novas) e o `--undo` sem edição no meio devolve o
`config.json` byte a byte. Depois, a entrada do
`team` passa a `"skill": "agent job-scout"`.

## travel-agent (E5)

O motor do travel-agent veio inteiro: `skills/agent/behaviors/travel-agent/scripts/` (`agent.py`, `flights.py`,
`gflights.py`, `miles.py`, `compare.py`, `sheets.py`...), e só o `paths.py` mudou, para achar a pasta da instância pelo
`watch_core` deste plugin. `agent.py travel-agent` repassa o tick e os comandos do travel-agent (`--report`, `--miles`,
`--compare`, `--gmail`, `--booked`...) ao `agent.py` dele num processo próprio (os dois se chamam `agent.py` e têm
`paths.py`), com `AGENT_INSTANCE` no ambiente e o Python do venv da instância (`cache/venv`, onde o `setup.py` instala o
`fast-flights`) quando existe. Argumento que o travel-agent não conhece é recusado: o motor dele faria um tick inteiro.
Em modo teste (sem `"live": true`) o tick é sempre `--dry`. `interval_s` abaixo de 10800 vira 10800 (cada tick é uma
grade inteira de buscas). O travel-agent nunca falou com o Planou: com `planou` no config, o tick ganha a volta fina
(eventos antes; ferramentas do config, aba Papel só para leitura e sinal de vida `tick` depois, com
`AGENTE QUEBRADO (<fonte>)` como fonte quebrada). Atalho `/travel-agent` (skill `travel-agent` deste plugin) é
`/agent travel-agent`. Testes: `tests/travel_agent/` (vindos do plugin antigo, menos os do runner dele) e
`tests/test_travel_agent_migration.py` (mesmas respostas antes e depois da migração).

Migrar uma instalação antiga, com o runner antigo parado (o runner deste plugin recusa subir enquanto ele estiver vivo;
o plugin travel-agent saiu do marketplace na E6, então o `runner.sh` é o da cópia que ainda estiver instalada):

```bash
bash <cópia antiga do plugin travel-agent>/skills/travel-agent/scripts/runner.sh --stop
python3 scripts/agent.py travel-agent --migrate --dry                  # o que vai mudar
python3 scripts/agent.py travel-agent --migrate --cwd <pasta da sessão>
python3 scripts/agent.py travel-agent --undo                           # volta (runner deste plugin parado)
```

O `--migrate` move `~/.config/travel-agent` (com o `cache/venv`) para `~/.config/agent/travel-agent`, deixa um link no
lugar antigo e acrescenta ao config só `schema`, `behaviors` (`travel-agent`), `live: true`, `interval_s: 21600` e
`session`; as viagens, os pontos e as regras ficam, e uma cópia antiga do plugin travel-agent ainda instalada segue lendo pelo link. Depois, a
entrada do `team` passa a `"skill": "agent travel-agent"`. O Planou é opcional: bloco `planou` no config e a chave em
`secrets/planou.env`.

## Plugins antigos retirados (E6)

Os plugins job-scout e travel-agent saíram do repositório e do marketplace na 0.62.1: as duas instâncias rodam neste
plugin (comportamentos `job-scout` e `travel-agent`, com os motores inteiros em `behaviors/`) e os atalhos `/job-scout` e
`/travel-agent` são skills daqui. O histórico deles (código, CHANGELOG e testes) fica no git: as últimas versões
publicadas são as tags `job-scout--v0.42.23` e `travel-agent--v0.4.6`.
O `--migrate`/`--undo` e a recusa do runner ao lado de um runner antigo vivo continuam, para uma instalação antiga que
ainda não migrou.

O plugin work-watch saiu também (PLN0262): as skills `work-inbox` (ler Teams, Outlook e Slack sob demanda) e
`meeting-minutes` (ata de reunião local na GPU) moram agora aqui, em `skills/work-inbox` e `skills/meeting-minutes`, e os
scripts de investigação da ata em `research/meeting-minutes/`. As fontes das instâncias work-watch chegam a elas por
caminho dentro do plugin (`paths.WORK_INBOX`, `paths.MEETING_MINUTES`). A última versão publicada do plugin work-watch é
a tag `work-watch--v0.43.24`; com ele saíram a cópia do `watch_core` e `shared/watch-core` (o `team` usa o `watch_core`
deste plugin para todos os agents). Numa instalação por link, `~/.claude/skills/work-inbox` e
`~/.claude/skills/meeting-minutes` passam a apontar para `plugins/agent/skills/<skill>`.

## Ganchos do plugin

| Tipo | O que faz | Opções |
|---|---|---|
| `release_due` | acorda a sessão com `== RELEASE DEVIDO` quando há `min_branches` ou mais branches prontas, ou uma esperando há mais de `max_wait_min` minutos; quieto durante a integração. A fila se mexe por `agent.py <instância> --release-queue list\|add\|start\|drop\|done\|abort`. Com o Planou ligado, as regras vêm da seção Release do projeto (guardadas 10 min; as opções ao lado são a reserva quando o Planou não responde) e a fila é espelhada lá a cada mudança | `ready_file` (padrão `data/release_queue.json`), `min_branches` (2), `max_wait_min` (30), `remind_min` (60), `stale_h` (3) |
| `deploy_log` | acorda a sessão com `== DEPLOY NOVO (n)` uma vez por linha nova no log de deploys; a primeira leitura só marca onde o log está. Deploy do release que a sessão fechou (`--release-queue done`, janela do `start` ao `done`) sai como `== DEPLOY JA AVISADO (n)`, que não acorda; com a integração rodando, as linhas novas esperam. A linha de alerta de saúde do deploy (`alerta de saúde`: a versão nova ficou no ar com a checagem falhando) sai como `-- ALERTA DE SAUDE da vX.Y.Z (ficou no ar)` e a volta automática como `-- volta para vX.Y.Z (rollback)`. Com o Planou ligado, cada deploy (e cada rollback) vai para o histórico de versões do projeto, com cursor próprio: falhou, tenta no próximo tick; o alerta nunca vai, porque não é versão nova | `path` (obrigatório), `max_lines` (5), `to_planou` (true), `post_max` (10 por tick), `release_file` (o do `release_due`), `hold_h` (3) |
| `tag_pull` | acorda a sessão com `== TAG NOVA (n)` quando o repositório ganha uma tag de release (`<plugin>--vX.Y.Z`, publicada pela CI): faz `git fetch --tags` e o fast-forward da cópia em uso (`git merge --ff-only origin/<branch>`, o `git pull --ff-only`) e anota a versão em `versoes[<plugin>]` do arquivo de versões, sem nunca voltar versão. A primeira leitura só marca as tags que existem. Cópia em outra branch, com mudança sem commit ou sem fast-forward: nada é forçado, sai `== TAG NOVA: copia em uso NAO atualizada` uma vez por motivo e tenta de novo no próximo tick. Não relança runner: a sessão diz ao usuário quais precisam | `path` (obrigatório, a cópia em uso), `branch` (`main`), `pattern` (`*--v*`), `versions_file`, `remote` (`origin`), `timeout_s` (120) |
| `suggestions` | lê a caixa de sugestões dos agents (`data/suggestions.jsonl`, escrita por `python3 -m watch_core.planou --agent <agent> sugestao ...`) e, num sync só, cria a tarefa no projeto (backlog, `reporter` = quem sugeriu, descrição com o que aconteceu, o que esperava, exemplo e prioridade) ou soma um "+1" na descrição da existente (mesmo título e assunto). Toda tarefa nova nasce sob um épico: o de `epics` que casa por apelido (`aliases`) ou pelas palavras do assunto e do título, ou um épico novo da área ("Plugins: <área>", "Planou: <área>") criado no mesmo sync; sem `epics` no config nenhum épico é criado (a tarefa nasce sem épico e a saída avisa com `-- sem epics no config`); o "+1" nunca troca o épico. No máximo `per_day` por agent por dia; a apagada pela pessoa não volta; sem Planou as linhas esperam. `agent.py <instância> --suggestions` lista | `inbox` (`data/suggestions.jsonl`), `project` (o `planou.project`), `per_day` (3), `epics` (`[{"pid", "title"}]`, os épicos abertos do projeto: a /v1 ainda não lista épicos), `aliases` (`{"<apelido>": "<título ou pid de um épico de epics>"}`; apelido para épico fora de `epics` é ignorado e avisado). Item de pendência com `epic` (pid ou source_key) leva o épico no sync das fontes |
| `health` | checagens só de leitura (`http`, `cmd` sem shell, `disk`, `fresh` para backups, `runners` pelo `team-status --json`, `sources` pelo `quebrado.sig` dos runners). Checagem quebrada em `after` ticks seguidos: `== SAUDE` com `-- QUEBROU` e a evidência (sempre pelo `clean_text`) e, com o Planou ligado, uma tarefa no backlog do projeto (reporter = a instância, uma por episódio); a volta (`-- VOLTOU`) é anotada na mesma tarefa, e quebrar de novo em até `reopen_h` reusa a tarefa. `== SAUDE DIARIA` uma vez por dia. Modo teste mostra todas as checagens e o que abriria. `action` de uma checagem (ex.: `rollback`) é só reservada: nada roda. `agent.py <instância> --health` checa agora | `checks` (lista; opções de cada tipo no docstring de `hooks/health.py`), `after` (2), `open_tasks` (true), `project` (o `planou.project`), `priority` (2), `max_tasks_day` (5), `reopen_h` (12), `report_hour` (8), `task_note`, `timeout_s` (20) |
| `alertas_azure` | bloco `== ALERTAS` com os alertas do Azure Monitor que a fonte de e-mail virou estado (`"alertas_azure": true` na fonte `gmail`): ATIVOU, AINDA ATIVO e desativou. Regra listada em `nao_acordar` (a que oscila o dia todo com os pods saudáveis) sai em `== ALERTAS SEM ACORDAR (n)`, que não acorda a sessão: as linhas vão para o `avisos.log` e esperam em `cache/runner/adiado` a próxima acordada (`== SEM ACAO`). Quando todos os alertas do tick estão na lista, o bloco dos ganchos de `nao_acordar_junto` (a consulta de histórico que o alerta dispara) vai junto, recuado, e também não acorda. Pod com problema novo ou reinício no mesmo `ambiente` (fonte `k8s`), no mesmo tick, desfaz o silêncio: tudo sai como antes e a sessão acorda. O nome da regra é comparado inteiro, sem diferença de maiúsculas | `fonte` (`email`), `nao_acordar` ([]), `nao_acordar_junto` ([], nome do gancho ou o tipo dele), `ambiente` (uma chave de `envs` da fonte `k8s`; vazio = qualquer ambiente), `fonte_clusters` (`clusters`) |
| `security` | as varreduras do agente de segurança, com o ciclo do `health` (uma tarefa por episódio, reporter = a instância, `-- MUDOU` e `-- RESOLVIDO` anotados na mesma tarefa, tarefa apagada não volta): `deps` (npm, NuGet e PyPI, sem instalar nem restaurar), `secrets` (arquivos do `git ls-files` e globs de log; evidência `<arquivo>:<linha>: <tipo> <máscara>`, sem o valor; `.env` e pasta `secrets/` versionados só pelo nome), `perms` (só `lstat`: pasta até 0700, arquivo até 0600) e `agents` (o `permissions.warnings` do `--validate` em cada instância). Varredura que não roda (ferramenta ausente, sem restore, sem rede) sai como `nao rodou` e nunca abre tarefa. `agent.py <instância> --security` varre agora | `checks` (opções de cada tipo no docstring de `hooks/security.py`), `after` (1), `every_h` por checagem (24 para `deps` e `secrets`, 1 para `perms` e `agents`), `priority` por tipo (segredo 1, dependência e permissão 2, autonomia 3), mais as do `health` |

## Testes

```bash
python3 -m unittest discover -s plugins/agent/tests
```

O runner roda de verdade num subprocesso, com HOME temporário e um Planou falso em 127.0.0.1.
