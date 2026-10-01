# Provisionador local: o agente nasce pelo Planou

O provisionador é um serviço do usuário (systemd, no WSL ou no Linux) que cria na máquina os agentes pedidos na tela
Time do Planou (**Novo funcionário**). Ele acompanha o Planou por `/v1/provisioner`, cria a instância, guarda a
chave e informa cada passo, que aparece no cartão do funcionário: **Criando, Validado, Terminal aberto, Runner de pé**,
ou **Erro** com a mensagem.

Com dois ou mais computadores conectados ao workspace, o Novo funcionário pergunta em qual deles criar o agente, e o
Planou só mostra o pedido ao provisionador daquele computador. A cada início, o provisionador informa a versão do
plugin (`POST /v1/provisioner/computer`), que a tela Time mostra ao lado do computador, no ar ou fora.

## O que ele faz com cada pedido

1. Informa `creating` e cria `~/.config/agent/<nome>/` a partir do modelo `config-example/dev/`:
   - `config/config.json` com os papéis como `behaviors` (sempre com `planou-queue`; sem papel, `dev-worker`), a pasta
     de trabalho em `session.cwd` e `repos`, o primeiro projeto em `planou.project`, a autonomia e `"live": false`,
     a não ser que a pessoa tenha marcado **ligar**;
   - `config/instructions.md` com os marcadores trocados e uma seção com papéis, autonomia e projetos;
   - `data/provisioned.json`, a marca de que a pasta é dele (só remove pasta com essa marca).
   - papel que não é de desenvolvimento e tem modelo próprio (`behaviors/<papel>/instance.json`, com
     `{"example": "<pasta de config-example>"}`, hoje o `product-radar`): as fontes e as opções do papel vêm desse
     modelo; sem `dev-worker` nem `batch-release`, a autonomia e o `instructions.md` também (o `never` do modelo dev
     continua).
2. Roda `agent.py <nome> --validate` antes de retirar a chave: config quebrado nunca gasta a chave.
3. Retira a chave uma vez e grava em `secrets/planou.env` (`PLANOU_AGENT_KEY=...`, arquivo 0600 e pasta 0700).
   Informa `validated`.
4. Roda o `config-snapshot` (quando existe; falha não segura o fluxo).
5. Põe o agente em `~/.config/team/agents.json` pelo `team_agents.upsert` (PLN0112: trava e troca atômica, os outros
   agentes e os campos deles ficam como estão), com `cwd`, `skill` (`agent <nome>`) e `rotate`, e informa
   `terminal_open`; a extensão Team Terminals abre o terminal com `team <nome>`. Sem o arquivo, é erro até alguém rodar
   `team --list` uma vez (ele cria a lista a partir da tabela do launcher; criar aqui deixaria de fora os agentes que
   só a tabela conhece, e a extensão fecharia os terminais deles). Arquivo quebrado nunca é regravado: vira `error`.
6. Informa `runner_up` quando `cache/runner/runner.pid` aponta um runner vivo da instância. Em modo teste o runner
   não sobe (o `runner.sh` recusa), então o cartão fica em Terminal aberto com o aviso de modo teste.
7. Terminal pedido e nenhum runner depois de `session_timeout_s` (2 min, PLN0217): informa `terminal_open` de novo, uma
   vez, com o motivo "terminal pedido, mas nenhuma sessao subiu: confira se a janela do VS Code com a extensao Team
   Terminals esta aberta ou rode "Team: abrir terminais"", que o cartão mostra. Vale para o terminal que o provisionador
   pediu em modo ligado (criar, Retomar e o Ligar de um agente adotado); o modo teste fica com o aviso dele.

**Pausar** para o runner, desliga a entrada no `agents.json` (`"enabled": false`, pelo `upsert`) e informa `paused`; **Retomar** liga
de novo e informa `terminal_open`. **Remover** para o runner, tira a entrada do `agents.json` (`team_agents.remove`), apaga os segredos, move a
pasta da instância (e o `.session` do `team`, se houver) para `~/.config/agent-provisioner/removed/<nome>-<hora>/` e
informa `removed`.

**Saiu deste computador.** Quando o funcionário muda de computador, ou quando este computador é revogado e o
funcionário vai para outro (PLN0203 do Planou), ele some da lista deste provisionador e a chave antiga é revogada. Numa
resposta válida da lista, cada instância com a marca `data/provisioned.json` cujo funcionário a lista não tem mais é
arquivada: o runner para, a entrada sai do `agents.json` (`team_agents.remove`), os segredos (a chave já revogada) são
apagados e a pasta, com o `.session` do `team`, vai para `~/.config/agent-provisioner/departed/<nome>-<hora>/`, com
config, dados e cache. Nada é informado ao Planou: o funcionário agora é do outro computador. Se ele voltar para este
computador, nasce de novo pela chave nova, e o arquivo fica onde está.

- Só a resposta válida da lista conta (um objeto com `agents` numa lista, cada item com `agent_id`). Falha de rede,
  Planou fora do ar, credencial recusada (401, 403) ou resposta fora do formato não arquivam nada.
- Instância sem a marca (um agente adotado, como o `planou-dev`, ou feito à mão) nunca é parada nem arquivada, mesmo
  com a lista vazia.
- Runner que não para ou `agents.json` quebrado: a instância fica como está, o log diz por quê, e a próxima volta
  tenta de novo; as outras seguem.

## Agentes que já existiam (adotados)

Os agentes que já rodavam na máquina antes do Novo funcionário (planou-dev, work-watch-*, tech-scout, job-scout,
travel-agent...) entram pelo **inventário**. A cada volta o provisionador manda ao Planou
(`POST /v1/provisioner/inventory`) cada agente do `~/.config/team/agents.json`, mais as instâncias do plugin agent que
ainda não estão nele, com `enabled`, a pasta (`work_dir`) e o estado do runner:

- `up`: o `runner.pid` aponta um `runner.sh` vivo;
- `stopped`: a entrada está desligada (instância que não está no `agents.json` conta como desligada: a extensão não
  abre o terminal dela), ou o agente não tem runner conhecido;
- `crashed`: a entrada está ligada e o runner não está de pé em duas voltas seguidas (uma volta só pode ser o
  relançamento depois de um tick; nela vale o último estado informado).

O nome que bate com um funcionário do workspace liga os dois, e o cartão da tela Time ganha **Ligar** e **Desligar**.
O runner de cada um sai do `team_agents.resolve`: instância do plugin agent usa o `runner.sh` dele (`runner.sh <nome>
stop`); quem tem plugin próprio usa `~/.claude/skills/<skill>/scripts/runner.sh`, com o verbo de parada que o `case
"$1"` do script aceita (`stop` no job-scout, `--stop` no travel-agent, onde um `stop` subiria outro runner). A pasta do
`runner.pid` é a do `team-status` (`runner_dir` da entrada, `~/.config/agent/<nome>/cache/runner`,
`~/.config/<nome>/cache/runner` ou `~/.config/<nome>/runner`).

- **Desligar** (`desired: "paused"`): para o runner, põe `"enabled": false` no `agents.json` (a extensão fecha o
  terminal) e informa `paused`, uma vez. Um runner que a pessoa suba à mão depois (`team <nome>`) fica.
- **Ligar** (`desired: "run"` depois de `paused`): põe `"enabled": true` (a extensão abre `team <nome>`) e informa
  `terminal_open`; `runner_up` quando o runner sobe.
- Instância do plugin que não estava no `agents.json` (tech-scout, por exemplo) é adotada desligada, a não ser que o
  runner esteja de pé; no Ligar ela ganha uma entrada só com `name`, `enabled` e `order`.
- Agente sem verbo de parada conhecido no `runner.sh` (chief-of-staff) tem o runner visto, mas Desligar vira erro
  ("pare pela sessão dele") e nada é desligado pela metade.

O provisionador **nunca** cria, muda a config ou apaga um agente adotado: o Planou não oferece Remover para ele, e o
remover dos criados só apaga pasta com a marca `data/provisioned.json` do mesmo funcionário. `agents.json` ilegível não
manda inventário (um vazio faria o Planou achar que todos saíram da máquina); um Planou sem a rota (antes da PLN0188)
responde 404 e o provisionador segue como antes.

**Gerar chave nova** na tela põe outra chave no cofre: o provisionador retira e grava por cima da antiga, e um pedido
em Erro é tentado de novo com ela. Um Erro sem chave nova é tentado de novo a cada `retry_s` (10 min).

Nome já usado na máquina (pasta em `~/.config/agent/`, pasta antiga do agent, `.session` do `team` ou entrada no
`agents.json`) vira Erro sem tocar em nada e sem retirar a chave.

## Segredos

- A chave do agente vai da resposta HTTP direto para o arquivo 0600. Nunca vai para log, saída, argumento de
  processo ou variável de ambiente; os processos filhos (`--validate`, `config-snapshot`, `runner.sh stop`) não a
  recebem.
- A credencial do provisionador (`PLANOU_PROVISIONER_KEY=pl_pv_...`) fica em
  `~/.config/agent-provisioner/secrets/planou.env`. Ele recusa o arquivo se não estiver 0600 e a guarda só na memória.
- O `base_url` precisa ser `https` (um computador conectado pelo `pair`, por exemplo `https://app.planou.com/v1`) ou
  `http` só em `127.0.0.1`, `localhost` ou `::1`: fora da máquina, a credencial nunca vai sem criptografia. O proxy
  HTTP do ambiente é ignorado (o Planou recusa a credencial do `connect-provisioner.sh` quando chega por proxy).
- Os logs passam por um filtro que mascara qualquer `pl_xx_...`.

## Instalar e ligar

O jeito curto é o instalador de um comando (PLN0192), que faz os passos abaixo sozinho e pede o código no fim, oculto:
`curl -fsSL https://raw.githubusercontent.com/davibauer/planou-agent/main/install.sh | sh` no Linux, no WSL e no
macOS (launchd, em `~/Library/LaunchAgents/app.planou.agent-provisioner.plist`) e
`irm https://raw.githubusercontent.com/davibauer/planou-agent/main/install.ps1 | iex` no Windows (tarefa agendada
que roda o provisionador no WSL, `provisioner/wsl-provisioner.sh`). Detalhes no
[README do plugin](../README.md#instalar-com-um-comando). O passo a passo à mão:

Pré-requisitos: o Planou com a PLN0110 no ar (versão com **Novo funcionário**; para os adotados, a PLN0188) e o plugin
agent instalado.

1. Credencial: conecte este computador (abaixo, em **Conectar este computador**) com o código da tela
   Configurações › Computadores:
   ```bash
   python3 $S/scripts/provisioner.py pair --base-url https://app.planou.com    # pergunta o código, oculto
   ```
   Na máquina em que o Planou roda, também vale o comando local do repositório do Planou (grava o arquivo 0600 sem
   mostrar; essa credencial só funciona nessa máquina, com `base_url` `http://127.0.0.1:5068/v1`):
   ```bash
   ./scripts/connect-provisioner.sh <seu e-mail> ~/.config/agent-provisioner/secrets/planou.env
   ```
2. Unit do systemd (`S` é a pasta da skill agent; grava `~/.config/systemd/user/agent-provisioner.service`, que roda
   `~/.claude/skills/agent/scripts/provisioner.py`, e `~/.config/agent-provisioner/config.json`, sem ligar nada):
   ```bash
   bash $S/provisioner/install-provisioner.sh
   ```
3. Conferir a credencial e a conversa com o Planou, e ver os pedidos:
   ```bash
   python3 $S/scripts/provisioner.py check
   python3 $S/scripts/provisioner.py status
   ```
4. Ligar (o WSL precisa do systemd ligado em `/etc/wsl.conf`):
   ```bash
   systemctl --user daemon-reload && systemctl --user enable --now agent-provisioner.service
   journalctl --user -u agent-provisioner -f
   ```
   O `install-provisioner.sh --enable` faz os passos 2 e 4 juntos; `--uninstall` desliga e tira a unit.

## Conectar este computador

Em Configurações › **Computadores**, o botão **Conectar este computador** mostra um código de uso único (`ABCD-EFGH`,
vale 10 minutos). No computador:

```bash
python3 $S/scripts/provisioner.py pair [--base-url URL]
```

- Sem o código no comando (PLN0192), ele pergunta com a digitação oculta; sem terminal, lê a primeira linha da entrada
  (`printf '%s\n' "$codigo" | provisioner.py pair`, como o instalador faz). Assim o código nunca aparece na lista de
  processos nem no histórico do shell. O `pair <código>` de antes continua valendo.

- Manda o código, o nome do computador (hostname, até 80 caracteres) e o sistema (o `PRETTY_NAME` do
  `/etc/os-release`, ou sistema e versão, até 40) para `POST /v1/provisioner/pair`. A chamada é pública: o código é a
  única prova, e não vai credencial nenhuma.
- A credencial volta uma vez só. Ela vai direto para `~/.config/agent-provisioner/secrets/planou.env`
  (`PLANOU_PROVISIONER_KEY=...`, arquivo 0600, pasta 0700), por cima da anterior, e as outras linhas do arquivo ficam.
  Nunca aparece na saída: ela mostra só o nome do computador e o prefixo da credencial, como a tela.
- Grava o `base_url` no `config.json` (as outras chaves ficam). `--base-url` aceita com ou sem o `/v1`
  (`https://app.planou.com` vira `https://app.planou.com/v1`). Sem ele, vale o `base_url` que já está no
  `config.json` e, sem nenhum, `https://app.planou.com/v1`.
- Antes de chamar o Planou, confere se consegue gravar na pasta e se o `config.json` é legível: a troca gasta o
  código, então nada que dependa do disco fica para depois.
- Código errado, expirado ou já usado: `codigo invalido, expirado ou ja usado`, e é gerar outro na tela. Muitas
  tentativas do mesmo endereço (429): esperar um minuto. Nos dois casos os arquivos ficam como estavam.
- Se o serviço já roda, reinicie para ele ler a credencial nova: `systemctl --user restart agent-provisioner`.

Cada computador tem a sua credencial, com o seu limite de chamadas e o seu último contato na tela, e **Revogar** a
corta na hora. Pela internet (`https://app.planou.com/v1`), a credencial pareada funciona a partir da versão do
Planou com a PLN0191; antes dela, o Planou só aceita qualquer credencial na própria máquina (`base_url` local).

## Atualização sozinho

O serviço roda o `provisioner.py` da cópia do plugin em uso (`~/.claude/skills/agent`, a mesma do runner), nunca uma
cópia instalada à parte, e segue o canário do time ([rollout](../../../docs/rollout.md)) como qualquer agente que não
é o canário:

- **Ao subir**, ele escolhe a versão pelo `~/.config/team/rollout.json` (só lê o arquivo; quem escreve é o canário):
  sem canário para o plugin agent, a versão em disco; com canário, a versão em disco só quando ela está liberada,
  senão a última liberada, rodada de uma cópia em `~/.cache/team/plugins/agent@<versão>/` (a mesma dos runners).
  Versão recusada nunca entra; sem nenhuma liberada, ele espera.
- **Versão liberada anterior ao provisionador** (PLN0186: a 0.31.0 liberada não tem `scripts/provisioner.py`): ele
  roda a versão mais nova que tem o script, desde que liberada ou em canário (nunca uma recusada nem uma que o canário
  ainda não viu), e fica nela até a nova ser liberada. Sem nenhuma, escreve no journal por que espera, confere a cada
  30 s por até 5 min e sai com o código 75: o systemd sobe de novo, sem loop rápido.
- **Entre uma volta e a próxima** (nunca no meio de um pedido), ele confere a versão de novo. Mudou (um `git pull` na
  cópia em uso sem canário, ou a versão nova liberada pelo canário), ele sai com o código 75 e o systemd
  (`Restart=always`) sobe de novo, já na versão escolhida.
- **A unit não muda a cada versão**: ela sempre roda o caminho da cópia em uso. Se uma versão nova do plugin trouxer
  outra unit, o provisionador regrava `~/.config/systemd/user/agent-provisioner.service` ao subir e pede
  `systemctl --user daemon-reload` (vale na próxima partida). Só mexe na unit que o instalador pôs (a primeira linha
  dela é a marca).

## Configuração

`~/.config/agent-provisioner/config.json` (todas as chaves são opcionais):

| Chave | Padrão | O que é |
|---|---|---|
| `base_url` | `http://127.0.0.1:5068/v1` | o `/v1` do Planou: `https://app.planou.com/v1` num computador conectado pelo `pair` (que grava esta chave), o local na máquina do Planou; em desenvolvimento, a porta da API |
| `interval_s` | 30 | segundos entre uma volta e a próxima (o Planou aceita 60 chamadas por minuto) |
| `retry_s` | 600 | espera antes de tentar de novo um pedido em Erro sem chave nova |
| `session_timeout_s` | 120 | espera pelo runner depois do `terminal_open`; passou dela, o motivo vai para o cartão |
| `template` | `config-example/dev` do plugin | pasta com `config.json` e `instructions.md` do modelo |
| `agents_file` | `~/.config/team/agents.json` | a lista do time (precisa ser a do `team_agents.py`) |
| `snapshot_cmd` | `config-snapshot` | rodado depois de criar; vazio desliga |
| `runner` | o `runner.sh` ao lado | usado só para parar o runner (pausar e remover) |

Comandos: `provisioner.py run` (o serviço), `once` (uma volta), `status`, `check` e `pair [<código>]`. Estado das tentativas em
`data/state.json` (sem chave nenhuma) e trava em `provisioner.lock`: um provisionador por vez.

## Limites de hoje

- Computador revogado que continua ligado recebe 401 em toda chamada e nunca vê uma lista válida: as instâncias dele
  ficam como estão (o runner delas também recebe 401, com a chave revogada) até ele ser conectado de novo pelo `pair`;
  aí a primeira volta arquiva as que foram para outro computador. Parar à mão: `runner.sh <nome> stop`.

- O `config.json` nasce do modelo dev: repositório, testes e conta do GitHub ficam para a pessoa revisar antes de
  ligar (a seção "Criado pelo provisionador" do `instructions.md` lembra isso).
- Só o primeiro projeto vai para `planou.project`; os outros ficam listados no `instructions.md`.
- A autonomia do Planou (`autonomous`, `semi_autonomous`, `manual`) vira o bloco `autonomy` por um mapeamento fixo e
  conservador (semiautônomo: pode branch, testes filtrados e push; pergunta antes de abrir PR).
