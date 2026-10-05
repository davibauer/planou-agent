---
name: acceptance-testing
description: "Testa a entrega como quem usa, antes do release, e aprova ou devolve com o que falhou. Sempre ativa na instância que a liga em \"behaviors\"."
title: Teste como usuário
summary: Testa a entrega como quem usa, antes do release, e aprova ou devolve com o que falhou.
layer: skill
kind: always
---
# acceptance-testing: a entrega testada como usuário antes do release

O agente de QA é dono de uma coluna do projeto no Planou (ex.: "QA", "Ao terminar, vai para" o estado de release). A
tarefa chega pelo handoff de quem veio antes (o dev ou o revisor), com a PR (`pr_url`), e entra na fila do QA como
qualquer outra (`task-queue`). Ele sobe um ambiente descartável da branch, percorre o fluxo da tarefa como a pessoa
faria, confere acessibilidade e as larguras, compara com o critério de pronto e aprova (handoff) ou devolve com o
pedido de ajuste. Vale com `task-queue` ligado. `PL` é o do `task-queue`; `S` é a pasta da skill.

**Autonomia fixa do QA:** testa, registra e comenta. Nunca escreve código (nem no repositório, nem um spec novo), nunca
faz commit ou push, nunca faz merge, nunca roda deploy, nunca roda a suíte completa nem o e2e completo do repositório.
Achou o defeito: descreve o passo, o esperado e o que aconteceu; quem conserta é o dev.

Opções em `behavior_config.acceptance-testing` (conferidas pelo `--validate`). Nenhuma é obrigatória: sem `repos` e
sem `env_up`, o agente descobre sozinho (abaixo, "Repositório e ambiente descobertos").
- `env_up`: comando que sobe o ambiente de teste, rodado na worktree da branch.
- `env_url`: comando rodado depois do `env_up`, na mesma worktree, que imprime a URL do app numa linha (ex.:
  `scripts/e2e-env.sh url`). Saída diferente de 0 ou vazia: o ambiente não subiu (exit 1, ambiente derrubado). Sem
  ele, a URL sai da primeira `http(s)://` da saída do `env_up`, ou de `url`.
- `env_down`: comando que derruba o ambiente, rodado na mesma worktree antes de removê-la.
- `setup`: comando rodado na worktree antes do `env_up` (ex.: instalar as dependências do front).
- `setup`, `env_up`, `env_url` e `env_down` são opções de comando: só se editam no computador, nunca pela aba Papel do
  Planou.
- `url`: URL fixa do app, quando não há `env_url` e o `env_up` não a imprime.
- `served_urls`: expressões regulares das URLs do app servido (dados reais). Casou: `qa_env.py` e `qa_kit.cjs` recusam.
- `node_dir`: pasta `node_modules` da worktree que tem `@playwright/test` e `@axe-core/playwright` (vai no `NODE_PATH`).
  Opção de caminho: decide de onde o node carrega código, então só se edita no computador, nunca pela aba Papel do Planou.
- `widths`: larguras testadas, em px (padrão `[1360, 834, 390]`). Abaixo de 1024 é toque; abaixo de 600, celular.
- `min_target_px`: altura mínima dos alvos de toque nas larguras de toque (padrão 44).
- `axe_tags`: regras do axe (padrão `["wcag2a", "wcag2aa", "wcag21aa"]`).
- `video_width`: a única largura em que o roteiro é gravado em vídeo como evidência (padrão 1360 quando está em
  `widths`, senão a maior; `0` = sem vídeo). Precisa ser uma das `widths`.
- `up_timeout_s`: tempo máximo de cada comando (padrão 600).
- `pr_comment`: `true` = o parecer vai como comentário na PR (`gh pr comment`), que precisa estar em `can`.
- `send_back`: como devolver. `ajuste` (padrão) = `fila ajuste`, que volta direto a quem fez o trabalho (o dev), também
  numa esteira dev, revisão, QA (Planou 0.49.0). `pessoa` = `fila blocked` com o pedido, para a pessoa decidir a quem
  devolver: só quando a instância quer essa conferência humana.

## Repositório e ambiente descobertos

Agente criado na tela Time só com o papel: o config nasce com `repos` vazio e sem `env_up`. Na primeira tarefa, o
`qa_env.py ... --pr-url <pr_url>` tira o repositório da PR, clona em `~/src/<nome>` com o gh já logado na máquina (ou
reaproveita o clone que já está lá, quando a origem é a mesma; outra pasta com o mesmo nome é recusada) e lê o
repositório (`scripts/repo_discovery.py`). Só GitHub, e só de um dono confiável: a conta do gh da instância, as
organizações dela ou `repo_discovery.allowed_owners` do config; outro dono, outro servidor git ou caminho local nunca é
clonado (exit 4 com a pergunta). A leitura é da ponta da branch principal da origem, e é refeita quando ela muda.
Só roda sozinho o que está numa lista de permitidos: um comando simples (sem `$`, crase, `|`, `<`, `>`, `;`, `&`,
parênteses, `\` nem quebra de linha) que começa por um servidor de desenvolvimento da lista curta (vite, next dev,
nuxt dev, astro dev, webpack serve, ng serve, react-scripts start, parcel, dotnet run/watch, `python -m http.server`,
flask run, uvicorn, rails s, php -S, hugo server e poucos outros; `docker compose` não está nela), sempre escutando só
em loopback: `--host`/`-H`/`--bind` com outro endereço (também colado, como `-h0.0.0.0`, repetido, ou `vite --host` sem valor) recusa, e o script do package.json
sem o parâmetro ganha `-- --host 127.0.0.1` (ou o do servidor). Ordem das fontes:
script de ambiente, docker compose da raiz, alvo de Makefile (`up`, `dev`, `start`, `serve`...), script `dev` ou
`start` do package.json.
- Script de ambiente (`scripts/*env*.sh` ou `bin/*env*.sh` com `up)` e `down)`, e `url)` quando tem; o citado no
  README, CLAUDE.md ou AGENTS.md quando há mais de um): shell livre, então **nunca roda sozinho**. O `qa_env.py` sai 4
  com `PERGUNTAR: Achei scripts/x.sh em <dono/repo> (sha256 <12>), com up e down: posso usar...`. Como a sessão
  registra o OK: só com o OK explícito da pessoa a essa pergunta (nunca pela recomendada, por `--auto` nem por uma
  regra de autonomia), rodar o comando que a própria pergunta traz,
  `python3 $S/scripts/repo_discovery.py <instância> <dono/repo> --approve scripts/x.sh@<sha12>`, e mover a tarefa de
  volta para a fila do QA. O OK fica no estado da instância por repositório e por sha256 do script; se o script muda
  na branch principal, a pergunta volta (o `--approve` com outro sha sai 2).
- Docker compose: o compose chega ao host (privileged, docker.sock, caminho do host, `network_mode: host`), então
  **também só com o OK da pessoa**, pelo mesmo `--approve compose.yaml@<sha12>`. O sha256 cobre o arquivo padrão da
  raiz, o override (`compose.override.yaml`...) e o `.env` da raiz; mudou qualquer um, pergunta de novo. A pergunta
  cita o arquivo e avisa o que dá acesso ao host (privileged, docker.sock, volume de caminho do host, `network_mode`,
  `pid`/`ipc`/`userns_mode: host`, `cap_add`, `devices`, `security_opt`, variável do `.env`). Nem vira candidato:
  `include:` ou `extends:`, `build`/`context` fora do repositório, `env_file` que não seja amostra (`.env.example`,
  `.env.test`, `.env.sample`) ou arquivo ausente, compose sem porta publicada. Aprovado, roda com `-f <arquivo>` e
  projeto `qa-<pid>` próprio.
- Makefile: lido inteiro, e qualquer linha de topo que não seja regra ou `NOME = literal` sem `$` recusa o arquivo
  (include, define, export, override, SHELL, `.RECIPEPREFIX`, `.ONESHELL`, condicionais, `$(shell ...)`, variável de
  alvo, receita na mesma linha, `\` numa receita). A receita do alvo, linha a linha, segue o comando simples da lista;
  pré-requisito é arquivo do repositório ou alvo que passa na mesma regra. Regra cujo alvo é o próprio Makefile
  (`Makefile: FORCE`, `makefile`, `GNUmakefile`) ou com `%` recusa o arquivo, porque o make a roda antes do alvo. Sem
  `env_down` do Makefile (o `down` encerra o grupo de processos). Roda como `make -r <alvo>`, e `GNUmakefile` ou
  `makefile` ao lado (que o make leria antes) recusa a fonte. Servidor que escuta em todas as interfaces por padrão
  (next dev, `python -m http.server`, parcel) precisa do parâmetro de loopback na receita.
- package.json: o script segue o comando simples da lista, sem `pre`/`post` nem pacote com
  `preinstall`/`install`/`postinstall`/`prepare`, e sem configuração do gerenciador que mude o que roda (`.yarnrc`,
  `.yarnrc.yml`, qualquer `.pnpmfile.*`, `bunfig.toml`, ou `.npmrc` com chave fora das simples, como `script-shell` e
  `node-options`), em qualquer pasta da raiz até a do pacote (os gerenciadores procuram subindo de pasta em pasta); caminho do pacote que passa por link simbólico também vira pergunta, porque eles sobem pelo caminho real. Nome de configuração que é link, mesmo quebrado (pode apontar para `node_modules` depois do setup), conta como presente e vira pergunta; Makefile, compose, script de ambiente e package.json que são link nunca são lidos, e um package.json que é link em qualquer pasta da raiz até o pacote vira pergunta (lido como ausente, esconderia `workspaces` e scripts de instalação). Branch principal com link absoluto ou para fora do repositório: PERGUNTAR com o motivo, sem rodar nada. `pnpm-workspace.yaml` (o `scriptShell` dele troca o shell),
  `workspaces` no package.json (o do pacote, o da raiz ou o de uma pasta no meio) e dependência `file:`, `link:`, `portal:` ou `workspace:`
  viram pergunta, que cita o motivo.
- A leitura é da branch principal, mas o ambiente roda na branch da PR: antes do `setup`, o `qa_env.py` relê a
  worktree da branch e, se ela muda um comando descoberto (o script aprovado com outro sha256, o alvo ou o script do
  package.json virando outra coisa), sai 4 com `PERGUNTAR: A branch da PR muda o que sobe o ambiente...` sem rodar
  nada. Só entram nessa conferência os comandos descobertos: o que vem do config da pessoa nunca é comparado e
  sempre roda. Falha fechada: o `--approve` não aceita o sha da branch; para testar a PR, a pessoa revisa a mudança e define
  `env_up`/`env_down` no config.json da instância, ou espera o merge (a descoberta relê e pergunta de novo).
Alvo e script do package.json são servidores que não terminam: rodam em segundo plano (com `PORT` livre), a URL é a
primeira `http(s)://` do log e o `down` encerra o grupo de processos. O que cita deploy, prod, release, kubectl, helm,
terraform, aws, gcloud, az, ssh, scp ou rsync nunca vira ambiente (camada extra), e nome de arquivo ou pasta fora de
`[A-Za-z0-9._/-]` (ou começado por `-`) nunca entra num comando. Fora da lista, a pergunta cita o candidato (`Achei
Makefile (alvo dev): "node server.js", fora da lista...`). `setup` só sai da instalação pelo lock, sempre com
`--ignore-scripts` (`npm ci --ignore-scripts`, `pnpm install --frozen-lockfile --ignore-scripts`...), do pacote que não
tem script de instalação (o que precisa de um vira pergunta) (o com `@playwright/test`, que também dá o `node_dir`, ou o do servidor). O `down` de um
repositório descoberto roda o `env_down` descoberto, nunca o do config de outro repositório. O que achou fica no estado da instância
(`data/discovered.json`, com a origem de cada valor), nunca no `config.json`, e vale nas tarefas seguintes;
`python3 $S/scripts/repo_discovery.py <instância> show` mostra. O config da pessoa sempre vence: `repos` quando tem
entrada, e os comandos de ambiente (`setup`, `env_up`, `env_url`, `env_down`, `url`) como bloco quando tem `env_up`;
`node_dir` chave por chave. Nada que sirva: exit 4 com a linha `PERGUNTAR:` (abaixo).

## Tarefa liberada na coluna do QA

1. `$PL fila ver`: título, descrição (o pedido e o critério de pronto), autonomia da tarefa, `pr_url`. `$PL fila started
   <PID> --estimate-h <H>` (0.5 a 1.5 h).
2. **Achar a PR e a branch.** A PR vem na linha da fila (`... com a PR: <link>`) e em `pr_url` de `$PL fila ver` (o
   `pr_url` segue a tarefa em cada handoff). `gh pr view <link> --json title,body,headRefName,headRefOid,files` dá a
   branch e o commit. Sem PR, os mesmos caminhos do `code-review` (link na descrição, PID no título com `in:title`,
   branch pelo PID). Nenhuma ou mais de uma: `printf '%s' "Não achei a PR de <PID>: qual é?" | $PL fila blocked <PID> --note -`.
3. **Roteiro**, antes de abrir o navegador: de cada ponto do critério de pronto e da descrição (e do corpo da PR), um
   caso `N. <passos como usuário> -> <o que deve aparecer>`, mais os estados que a tela precisa aguentar (vazio, erro,
   texto longo) quando fizerem sentido. Gravar em `<pasta>/roteiro.md`, onde `<pasta>` é `<scratchpad da sessão>/qa-<pid>/`
   (sem scratchpad: `cache/qa/<PID>/` da instância). Nada disso vai para o repositório.
   **Nada para testar como usuário** (só documentação, teste, ou servidor sem efeito na tela, pelos arquivos da PR):
   aprovar com a nota "sem efeito na tela: QA não se aplica" e não subir ambiente.
4. **Ambiente**: `python3 $S/scripts/qa_env.py <instância> up <PID> --branch <headRefName> --pr-url <pr_url>`. Ele
   acha o repositório (o de `repos` no config; sem ele, o descoberto pela PR, clonado na primeira vez), cria uma worktree
   destacada da branch em `<worktrees>/qa-<pid>`, roda `setup` e `env_up` nela e imprime
   `QA AMBIENTE <PID>: <url> ...` e a linha `RODAR: QA_URL=... NODE_PATH=... node <script>`. Saídas:
   - exit 1 com o `env_up` falhando por causa da branch (build quebrado, migration que não aplica): é defeito da
     entrega, vai para o parecer como ajuste;
   - exit 1 por infraestrutura (docker parado, porta, disco) ou exit 2: `fila blocked <PID>` com a causa, sem devolver
     ao dev;
   - exit 3: a URL é a do app servido ou não é http(s): parar e avisar o usuário (config errado);
   - exit 4: sem `env_up` no config e nada no repositório que suba o ambiente, script de ambiente esperando o OK da
     pessoa (a pergunta traz o `--approve`), branch da PR que muda um comando descoberto, ou dono do repositório que a
     instância não reconhece: `printf '%s' "<a linha PERGUNTAR>" |
     $PL fila blocked <PID> --note -` (a pessoa define os comandos no `config.json`, no computador; comando nunca
     vem pelo Planou), sem devolver ao dev;
   - exit 1 com um comando descoberto (a mensagem diz `comando descoberto em <origem>`) que falha por um motivo que
     não é da branch (comando ou alvo que não sobe nada, serviço que falta): a descoberta errou, não a entrega;
     `fila blocked` com a mensagem, como no exit 4.
5. **Script efêmero** `<pasta>/run.cjs`, escrito para esta tarefa e nunca commitado:
   `const kit = require(process.env.QA_KIT); const qa = kit.config(); const r = kit.report(__dirname)`.
   - Conta de teste só no ambiente descartável, com dados falsos (e-mail `qa-<pid>-<hora>@exemplo.com`, senha aleatória),
     como o `instructions.md` da instância ensinar (cadastro, dados iniciais pela API). Nunca a credencial do usuário,
     nunca o app servido, nunca dado real de cliente.
   - Cada caso do roteiro como a pessoa faria: navegar, clicar e preencher pelo papel e pelo rótulo (`getByRole`,
     `getByLabel`), conferir o que aparece; cada conferência em `r.check('<caso N>: <o quê>', ok, detalhe)`.
   - Em cada largura de `widths` (`kit.pageAt(browser, largura)`), nas telas que a entrega muda:
     `await kit.screens(page, r, '<tela>-<largura>', qa)`, que grava a captura e confere o axe (`axe_tags`), os alvos de
     `min_target_px` nas larguras de toque e a rolagem horizontal.
   - **Vídeo do roteiro** (PLN0279): `kit.pageAt(browser, largura, {}, r)` grava o vídeo do contexto só na largura de
     `video_width` (uma por vez, nunca todas); o roteiro inteiro roda nela, do começo ao fim, e as outras larguras só
     conferem as telas. Feche cada página com `await kit.closePage(page, r)` num `finally` (também quando o caso falha:
     é aí que o vídeo mais vale), antes do `browser.close()`: ele salva `<pasta>/roteiro-<largura>.webm`. O Playwright
     reduz o vídeo para 800 px (VP8), cerca de 0,75 MB por minuto; acima do limite do anexo (50 MB) o kit apaga o vídeo
     e o QA segue só com as capturas. Vídeo nunca reprova nem aprova: é evidência.
   - Fim: `process.exit(r.finish())` (grava `resultado.json`, com o vídeo ou o motivo de não ter).
   Rodar com a linha `RODAR:`, com `| tail -n 40`. Um caso que falhou roda de novo uma vez: falhou nas duas, é defeito;
   passou na segunda, vai como "instável" no parecer e não reprova sozinho.
6. **Olhar as capturas uma a uma** (Read), comparando com o que o roteiro pede e com as regras visuais do repositório
   (arquivo `rules` de `repos`): texto cortado, sobreposição, contraste, alinhamento, o que some numa largura.
7. **Veredito**: aprovado quando todo caso do roteiro passou, o axe não achou nada, não há alvo abaixo de
   `min_target_px` nem rolagem horizontal. Qualquer outra coisa é ajuste, com o caso, o passo e a largura.
8. **Registro**: `<pasta>/qa-<pid>.md` com o parecer (abaixo), e `$PL attach <PID> <pasta>/qa-<pid>.md` mais as capturas
   que provam o parecer (até 6). Só dados de teste nas capturas. O vídeo vai por anexo explícito, à parte das capturas:
   `$PL attach <PID> <pasta>/roteiro-<largura>.webm` (vídeo nunca sobe por citação, PLN0302; só dados de teste nele).
   O parecer cita o vídeo pelo nome e pelo tamanho, nunca pelo caminho da pasta. Sem vídeo (`video_width` 0, apagado
   pelo tamanho ou não salvo), o parecer diz o motivo que o `resultado.json` traz.
   ```
   QA de <PID> (<sha7>): aprovado | ajuste pedido
   Ambiente: <branch> @ <sha7>; larguras <widths>; axe <axe_tags>
   Casos: N/M ok. 1. <caso>: ok | FALHA: <passo> -> <esperado>, veio <o quê> (<largura>)
   Tela: axe <ok | violações>; alvos <ok | lista>; rolagem <ok | px>
   Vídeo: roteiro-<largura>.webm (<N> MB, anexo na tarefa) | sem vídeo: <motivo>; só capturas
   Instáveis: ...                                            (se houver)
   ```
   Com `pr_comment`: `gh pr comment <N> -R <dono/nome> --body-file <pasta>/qa-<pid>.md` (o `env` da ferramenta `gh`
   de `tools`). Nunca `gh pr review --approve` nem `--request-changes`.
9. **Sempre derrubar**: `python3 $S/scripts/qa_env.py <instância> down <PID>` (roda `env_down` e remove a worktree),
   aprovando ou devolvendo. `qa_env.py <instância> status` mostra o que ficou de pé.

## Aprovar

`$PL fila done <PID> --note "QA aprovado (<sha7>): N casos, axe ok, <widths> [ver anexo: qa-<pid>.md]"`. O QA é dono
da coluna com próximo estado, então o `done` vira handoff (`PASSOU: <PID> foi para <estado> (<quem>)`). Contar ao
usuário em uma linha: tarefa, PR, "passou do QA". Se a coluna não tiver próximo, o `done` conclui a tarefa: erro de
configuração do projeto; avisar o usuário.

## Devolver com pedido de ajuste

1. O parecer com os casos que falharam na PR (acima) e anexado na tarefa.
2. Com `send_back` `ajuste` (padrão): `printf '%s' "Ajuste pedido no QA de <PID> (<sha7>): <casos que falharam, curto>.
   Parecer na PR: <link>" | $PL fila ajuste <PID> --text - [--pr-url <link>]` (até 2000 caracteres). A tarefa volta
   para a coluna de quem fez o trabalho (o dev, mesmo com uma revisão entre os dois) e para a frente da fila dele como
   retrabalho, na mesma branch e PR; ele recebe `-- AJUSTE PEDIDO <PID> (<função do QA> <nome>): ...`. A saída traz
   `DEVOLVIDA: <PID> voltou para <coluna> (<quem>)` e a vaga do QA libera. Com `"confidentiality": "minimum"` o texto
   não sobe (sem PR, vai "o parecer está na sessão do agente"; a branch da passagem própria fica só na instância). Sem `--to` vale o padrão do Planou (`author`, quem fez); `--to previous` devolve a quem passou a tarefa ao
   QA (o revisor), só quando o defeito é da revisão.
   Com `send_back` `pessoa`: `printf '%s' "Ajuste pedido no QA de <PID> (<sha7>): <lista curta>. Parecer na PR:
   <link>" | $PL fila blocked <PID> --note -`.
3. Saídas que não devolvem, como no `code-review`: `SEM DEV: ...` (a tarefa não veio por handoff: `fila blocked` com o
   pedido para a pessoa), `ESPERE: ...` (ainda não liberada), `PARE: ...` (a tarefa não está mais com o QA).
4. Contar ao usuário em uma linha: tarefa, o defeito em poucas palavras, o link do parecer.

Quando a entrega volta depois do ajuste, a tarefa chega de novo pela fila com a PR: subir o ambiente no commit novo,
rodar primeiro os casos que falharam (o `run.cjs` da `<pasta>` continua lá) e depois o roteiro inteiro, que é curto.

## Mesma instância que desenvolve

Quando esta instância também tem `delegate-to-worker` e é dona da coluna do dev e da coluna de QA (PLN0281: uma instância
faz o ciclo todo; instâncias separadas por papel continuam valendo), ou é a única no ar com o papel da coluna de QA
(coluna por papel, PLN0284: o Planou devolve a tarefa a quem a desenvolveu quando não há outra instância com o papel),
a tarefa que ela mesma desenvolveu chega aqui como
`-- FILA LIBERADA <PID>: ... -> passada por voce mesmo (mesma instancia que desenvolve) para a coluna <coluna>[, com a
PR: <link>][; nota: <branch>]`. Desde o Planou 0.76.0 (PLN0286) a passagem para si mesmo guarda quem passou (o
próprio agente, em `handed_off_by`) e a nota, como entre agentes; o plugin reconhece pelo nome (o da config ou o do
`whoami`) e `$PL fila ver` mostra em `mesma_instancia` (coluna, papel e nota com a branch). Sem `handed_off_by` (coluna
por papel que esperou vaga e voltou a esta instância), a tarefa que chegou pela coluna numa coluna de QA desta
instância conta igual quando ela também faz o dev (dona da coluna anterior ou com o papel dev); revisor puro com card
arrastado pela pessoa segue as linhas de sempre. A sessão viu a volta do dev, então **não faz QA: delega**. Contra a autoaprovação, o
worker de QA nunca é o worker que entregou, nem continuação dele por SendMessage.
1. `$PL fila ver` e `$PL fila started <PID> --estimate-h <H>`.
2. Um `Agent(worker)` NOVO, com:
   - o pedido literal da tarefa (título e descrição de `fila ver`), a PR ou a branch e o critério de pronto; nada do
     transcript, do resumo nem da volta do worker de dev;
   - o bloco de `$A --brief <repo> --role acceptance-testing` (`A="python3 $S/scripts/agent.py <instância>"`): a worktree do `qa_env.py`, a linha INDEPENDENCIA e o parecer como critério de pronto; os passos 2 a 9 de "Tarefa liberada na coluna do QA" (PR e branch, roteiro, ambiente, script efêmero, capturas, veredito, registro, derrubar) vão no pedido, com a pasta `<scratchpad do worker>/qa-<pid>/`;
   - as opções de `behavior_config.acceptance-testing`.
   Logo depois: `KEY=$($PL fila worker-start <PID> --role other --label 'QA independente de <PID>')` e, quando ele voltar,
   `$PL worker end <key> --result feito|parcial|falhou` (o `fila worker` de entregas só aceita `dev` e `integrator`).
3. O parecer começa com `QA independente (worker novo) de <PID> (<sha7>): aprovado | ajuste pedido`. Com `pr_comment`, quem comenta na PR é o worker; `fila ...` e o `$PL attach` do
   parecer, das capturas e do vídeo (os caminhos vêm na volta do worker, com o `roteiro-<largura>.webm` ou o motivo de
   não ter vídeo) são só da sessão.
4. Aprovado: `$PL fila done <PID> --note "QA independente aprovado (<sha7>): N casos, axe ok, <widths> [ver anexo: qa-<pid>.md]"` (handoff para a próxima coluna, como em Aprovar). Sem `fila worker-start <PID> --role other` aberto depois do
   a tarefa chegar na coluna, o `fila done` avisa (`AVISO`, não recusa): a aprovação tem de ser do worker novo.
5. Ajuste pedido: o mesmo `fila ajuste <PID> --text -` de "Devolver com pedido de ajuste", e o Planou devolve como
   entre agentes: a saída é `DEVOLVIDA: <PID> voltou para <coluna do dev> (<esta instância>, este mesmo agente)` (exit 0)
   e a entrada da QA termina. Daí em diante é o retrabalho normal: chega `-- AJUSTE PEDIDO <PID> (<papel> <nome>,
   voce mesmo: revisao independente)` (também quando a pessoa usa "Pedir ajuste" nesta coluna), espera a vaga e volta
   como `-- FILA LIBERADA <PID>: ... -> RETRABALHO (ajuste pedido)`: worker de dev (`delegate-to-worker`) na mesma branch e na
   mesma PR atendendo o parecer e, na volta dele, **`fila handoff <PID>`** (não `fila in_review`: com o mesmo dono na
   coluna seguinte o `in_review` não vira passagem). A tarefa volta a esta coluna pela fila e outro worker NOVO de QA
   confere só o que mudou desde o `sha7` do parecer, e o passo 4 ou 5 de novo.
6. `SEM DEV` continua querendo dizer que a tarefa não veio por passagem (a pessoa a pôs direto na coluna, ou a coluna
   por papel esperou vaga e o Planou não guardou quem passou): pedir à pessoa como acima. Mesmo assim, a QA é de um worker novo.
A vaga de QA é uma tarefa da fila como as outras: conta no mesmo "Ao mesmo tempo" da aba Fila, junto com as de dev.

## O que o QA não faz

- Não escreve nem edita arquivo do repositório: a worktree é só para rodar; o script e as capturas ficam fora dele.
- Não roda a suíte completa, o e2e completo nem os specs do repositório (isso é do dev e do integrador); roda só o
  próprio script, numa largura por vez.
- Não usa worker de código. Instância só de QA: o teste é da sessão. Na mesma instância que desenvolve, é de um
  worker NOVO (seção acima), que também não escreve código. Em nenhum dos dois manda `fila worker` (o Planou só aceita
  `dev` e `integrator` como papel da entrega).
- Não entra com a conta do usuário, não abre o navegador do usuário, não testa contra o app servido.
- Não faz merge, deploy nem release, não fecha nem edita a PR.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido.

```permissions
tools: cli:gh
actions: worktree, test.filtered, pr.comment, planou.queue, planou.note
```
