---
name: work-inbox
description: >-
  Lê mensagens do Microsoft Teams (1:1 ou grupo, por nome, tópico ou link), e-mails e calendário do Outlook e mensagens do Slack (canal, DM, thread, busca; por token de sessão, sem app) das contas corporativas do usuário, sob demanda: "últimas mensagens do gerente", "resume o grupo do time", "o que chegou de e-mail do fornecedor", "o que falaram no #dev do Slack", link de chat/mensagem. Várias contas/tenants/workspaces por perfil. Use quando o usuário perguntar o que alguém falou no Teams ou no Slack ou mandou por e-mail, pedir para resumir/interpretar/responder uma conversa ou thread, ou disser /work-inbox.
---

**Caminhos:** `S=<a pasta desta skill>` (a "Base directory for this skill" que o Claude Code mostra ao carregá-la); os scripts ficam em `$S/scripts/work_inbox/`. É uma skill do plugin agent: os agents de trabalho (`work-watch-<empresa>`) usam este mesmo código nas fontes de Teams, Outlook e Slack. Tudo que é da conta fica em `~/.config/work-inbox/profiles/<perfil>/`, nunca no plugin.

## Como funciona (e a que conta se conecta)

`teams_pull.py` → `az account get-access-token --resource https://service.flow.microsoft.com/` (tenant do **perfil**) → runtime de conectores do Power Platform → **conexão Microsoft Teams** que o usuário autorizou naquele tenant → Graph. Não há flow, app registration nem admin consent; a permissão é a do próprio usuário. Nada é enviado ao Teams.

**Perfis** = contas (tenant Microsoft e/ou workspace Slack), em `~/.config/work-inbox/profiles/<nome>/` (`config.json` com `conta`, `tenant` e `paths`; `chats.json` do Teams; `slack.json`). Listar: `ls ~/.config/work-inbox/profiles` (o `config.json` de cada um diz a conta e as pastas de projeto). Um perfil só-Slack faz `teams_pull.py`/`outlook_pull.py` pararem com "nao tem conexao Teams". Escolha do perfil, nesta ordem: `--profile` → env `WORK_INBOX_PROFILE` → **pasta do projeto** (`paths` do perfil contém o cwd) → único perfil existente. **Não há default**: fora de qualquer pasta conhecida, o script para e lista os perfis; aí **perguntar ao usuário qual conta** (AskUserQuestion) e repetir com `--profile`. Nunca assumir uma conta. Toda saída começa com `-- perfil X [origem]: conta (tenant …)`. Se o nome pedido só existir no cache de outro perfil, o script **recusa e aponta** o perfil certo (não troca sozinho); repetir com `--profile`. Se o pedido citar um cliente/empresa, passar `--profile` direto.

## Teams

```bash
P="python3 $S/scripts/work_inbox/teams_pull.py"
$P --from Maria                       # últimas 30 do 1:1 (nome parcial, sem acento serve)
$P --from "Time Dev" -n 60            # grupo pelo tópico
$P --from "https://teams.microsoft.com/l/chat/19:…@thread.v2/…"   # link ou id do chat
$P --from Ana --since 2d              # recorte temporal (30m, 4h, 2d, 3du, ISO)
$P --list                             # nomes conhecidos (1:1 e grupos)
$P --refresh                          # pessoa/chat novo: reidentifica (lê poucas msgs de cada 1:1 novo)
$P --json ...                         # cru, uma linha por mensagem
$P --profile <cliente> ...            # outra conta/tenant
```

Nome ambíguo (duas pessoas com o mesmo nome) → usa o mais recente e lista os outros no stderr; se importar, perguntar.

## Outlook (e-mail): mesmo perfil, mesmo token

```bash
O="python3 $S/scripts/work_inbox/outlook_pull.py"
$O                          # últimos 20 da Inbox (● = não lido, 📎 = anexo)
$O --from maria             # remetente por nome (filtro local) ou e-mail exato (filtro no servidor)
$O --subject GMUD -n 50     # assunto
$O --search "PR 1234"       # busca KQL do Outlook (texto, from:, hasattachment:yes, received:…)
$O --unread --since 1d      # não lidos recentes
$O --folder "Inbox/Projetos"
$O --read <id>              # corpo completo (texto) de um e-mail
$O --digest --since 3du     # TRIAGEM: pessoas × notificações (agrupadas por PR/assunto); Ndu = N dias úteis
```

`--from <nome>` vazio não é erro: a pessoa pode só aparecer em notificações de terceiros (ex.: Azure DevOps); tentar `--search <nome>`. Conector: Office 365 Outlook (`GetEmailsV3`/`GetEmailV2`), conexão em `outlook_connection`/`outlook_runtime` do perfil.

## Calendário do Outlook

```bash
CALENDAR_PERFIL=<perfil> python3 $S/scripts/work_inbox/calendar_outlook.py --next 7d       # agenda dos próximos N dias
CALENDAR_PERFIL=<perfil> python3 $S/scripts/work_inbox/calendar_outlook.py --event <id>     # um evento inteiro
```

Só leitura: não aceita, não recusa, não cria nada.

## Token

(Teams/Outlook; o Slack tem credencial própria, ver a seção Slack.) Automático pelo `az` (refresh token ~90 dias, renova a cada uso). Se sair **"Sem token valido"**, pedir ao usuário para rodar no prompt:
`! az login --tenant <tenant-do-perfil> --use-device-code --allow-no-subscriptions` (conta daquele cliente). Plano B: Bearer do portal make.powerautomate.com (F12 → Network → header `Authorization`) gravado com `--token`; vale ~1h e tem prioridade sobre o `az` enquanto vale. Não tentar renovar de outro jeito.

## Nova conta / cliente

```bash
az login --tenant <tenant-id> --use-device-code --allow-no-subscriptions   # o usuário, com a conta do cliente
python3 $S/scripts/work_inbox/teams_setup.py --profile <cliente> --tenant <tenant-id> [--outlook] [--default]
```
O setup cria a conexão Teams (e a Outlook, com `--outlook`) no Power Automate do cliente e **para** pedindo ao usuário para clicar em *Fix connection* no portal (login OAuth, único passo que não dá para automatizar); depois descobre o runtime e monta o cache. Idempotente: reusa conexão já autenticada. Para o perfil valer por pasta de projeto, pôr `"paths": ["~/Projects/cliente"]` no `config.json` dele.

## Imagens e presença (sessão do Teams web)

`teams_web.py --profile <perfil> image <hostedContentId ou url>` baixa uma imagem colada numa mensagem (o conector devolve 404 para elas) em `~/.config/work-inbox/media/`. Credencial por perfil: JSON `{tenant, client_id, brk_client_id, refresh_token}` em `teams-web.json` na pasta do perfil, ou no caminho de `"teams_web_cred"` do `config.json` do perfil. O refresh token é rotacionado a cada uso; nunca imprimir tokens.

## Triagem ("o que chegou?", "algo relevante nos últimos dias?")

`outlook_pull.py --digest --since 3du` (dias úteis: hoje conta como 1; o recorte vai ao servidor via KQL, ~5 s). Ler na ordem: **pessoas** (resposta pendente? convite?) → **notificações** agrupadas (PR concluída/aprovada, card movido). Depois cruzar com a memória do projeto: PR de promoção concluída costuma implicar próximo passo (merge, GMUD, deploy). Só então dizer "nada relevante".

## Slack: `slack_pull.py`, por token de sessão (sem app no workspace)

Workspaces que bloqueiam instalar apps não deixam usar o conector MCP do Slack. O caminho é a **Web API com a credencial da sessão do navegador do próprio usuário**: token `xoxc-…` **+ cookie `d=xoxd-…`** (os dois; o xoxc sozinho volta `invalid_auth`). Tokens de app (`xoxp-`/`xoxb-`) também funcionam, sem cookie, mas o bot só vê canais onde está e não faz `--search`.

```bash
L="python3 $S/scripts/work_inbox/slack_pull.py"
$L --from "#dev"                        # canal (público/privado); nome parcial serve
$L --from Ana -n 60                     # DM pelo nome (parcial, sem acento)
$L --from "https://<workspace>.slack.com/archives/C…/p1694…"   # link: canal → o canal; mensagem → contexto até ela; com thread_ts → a thread
$L --from "#dev" --since 2d --threads   # recorte no servidor (30m, 4h, 2d, 3du, ISO) + respostas das threads inline
$L --thread C…/1694….123456             # uma thread (canal/ts do pai; o ts aparece em "[thread: N respostas, ts=…]")
$L --search "audit report in:#dev from:@joao after:2026-09-01"   # busca do Slack
$L --list | --refresh | --json | --profile <cliente>
```

Mesmas regras de perfil: `--profile` → env → pasta do projeto → único; fora de pasta conhecida **perguntar a conta**. Nome que só existe no `slack.json` de outro perfil → recusa e aponta. Saída no mesmo formato `[dd/mm HH:MM] Nome: texto`, com `@menções` e `#canais` resolvidos pelo cache; mensagens de entrada/saída de canal são omitidas; anexos só pelo nome. `--search` mostra o canal antes de cada resultado.

**Credencial** (uma vez por workspace; cria o perfil se não existir, valida com `auth.test` e monta o cache):
```bash
$L --profile <cliente> --paths ~/Projects/<cliente> --token xoxc-… --cookie xoxd-…
```
Onde pegar: app.slack.com logado → F12 → *Application › Cookies › app.slack.com › `d`* (valor `xoxd-…`, colar sem o `d=`); token: *Network*, qualquer request `api/…`, *Payload* → campo `token` (`xoxc-…`), ou no console `JSON.parse(localStorage.localConfig_v2).teams` (chave `token` do workspace). Fica no `config.json` do perfil (0600): **é a sessão inteira do usuário**, nunca copiar para repositório ou plugin; env `SLACK_TOKEN`/`SLACK_COOKIE` sobrepõem. Vence quando a sessão do navegador vence (o script avisa `Slack recusou a credencial`): pegar o par de novo e regravar com `--token/--cookie`. Não há renovação automática. O conector do Slack no Power Automate só posta/lista canais, não lê mensagens; não tentar o caminho dos perfis Teams.

## Ao interpretar

- O objetivo quase sempre é **endereçar** algo: dúvida sem resposta, pedido pendente, decisão a confirmar. Achar isso primeiro; separar o que o usuário disse do que o outro disse. Um link solto do usuário não conta como resposta a uma pergunta.
- Cruzar com o contexto da sessão (repo aberto, cards, memória): a pergunta costuma ser sobre o que o usuário está fazendo agora. Se o rascunho depender do conteúdo de uma PR/card, conferir antes de afirmar o que ela faz.
- Propor a resposta como rascunho para o usuário mandar: o plugin **não envia** nada.
- Citar `[dd/mm HH:MM] Nome`. Anexos vêm só pelo nome. Dado do tenant do cliente: não copiar para serviços externos sem pedido explícito.

## Notas de infra

- `teams_feed.py` é só biblioteca (normalização/impressão). O modo "feed ao vivo" (flow webhook → OneDrive) foi removido por redundante; a pasta, se voltar a existir, vem de `TEAMS_FEED_DIR`.
- Rotas: conexões em `api.powerapps.com` (leitura exige token aud `service.powerapps.com`; criação aceita o do Flow); leitura de chats **só** no host do runtime (`…environment.api.powerplatform.com/connectors/runtime/invoke/…`), `listchats/chattypes/{oneOnOne|all}/topic/{all|isDefined}/expandmembers/false` e `/beta/chats/{id}/messages?$top=50`.
