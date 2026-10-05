# Instância de QA: modelo e passo a passo

O comportamento `acceptance-testing` (skills/agent/behaviors/acceptance-testing/BEHAVIOR.md) testa a entrega como usuário antes do release: sobe um
ambiente descartável da branch, percorre o fluxo da tarefa com um script Playwright efêmero, confere axe, as larguras e
os alvos de toque, e aprova (handoff) ou devolve com o pedido de ajuste. Nunca escreve código.

Este guia monta a instância em **modo teste** (sem `"live": true`). Ligar é decisão do usuário.

**Sem config nenhum (agente criado na tela Time só com o papel).** Desde a PLN0345 o agente descobre sozinho o
repositório e o ambiente: na primeira tarefa, `qa_env.py <instância> up <PID> --branch <b> --pr-url <pr_url>` clona o
repositório da PR em `~/src/<nome>` com o gh já logado (ou reaproveita o clone que já está lá, quando a origem é a
mesma; só GitHub e só de dono confiável: a conta do gh da instância, as organizações dela ou
`"repo_discovery": {"allowed_owners": [...]}` no config), lê o repositório e grava o que achou em `data/discovered.json` da instância, com a origem de cada valor. Ordem:
script de ambiente (`scripts/*env*.sh` com `up)` e `down)`), docker compose da raiz com porta publicada, alvo de
Makefile e script `dev`/`start` do package.json (estes dois em segundo plano, a URL tirada do log). É uma lista de
permitidos: alvo e script só rodam sozinhos quando são um comando simples (sem `$`, `|`, `;`, `&`, redirecionamento,
parênteses nem `\`) que começa por um servidor de desenvolvimento conhecido (vite, next dev, dotnet run,
`python -m http.server`, flask run, uvicorn e poucos outros), escutando só em loopback; o Makefile é lido inteiro e qualquer
construção além de regras e `NOME = literal` (include, define, SHELL, `$(shell)`, condicionais...) recusa o arquivo;
regra que refaz o próprio Makefile também. O script de ambiente e o docker compose da raiz nunca
rodam sozinhos: viram a pergunta "achei scripts/x.sh (ou compose.yaml), posso usar?", que no compose avisa o que dá
acesso ao host (privileged, docker.sock, caminho do host...), e com o OK da pessoa a sessão roda
`repo_discovery.py <instância> <dono/repo> --approve <arquivo>@<sha12>` (vale para aquele sha256, que no compose cobre
também o override e o `.env`; mudou, pergunta de novo). O compose com `include:`/`extends:`, `build` fora do
repositório ou `env_file` que não seja amostra nem vira candidato. Como o ambiente roda na branch da PR, o `qa_env.py` relê a worktree antes de rodar e, se a branch
muda um comando descoberto (script aprovado com outro sha256, alvo ou script virando outra coisa), sai 4 com a
pergunta sem rodar nada. O Makefile roda com `make -r` e não vale com `GNUmakefile`/`makefile` ao lado; o package.json
não vale com `.yarnrc`, `.pnpmfile.cjs`, `.npmrc` que mude o shell ou o node, `pnpm-workspace.yaml` ou qualquer `.pnpmfile.*` (em qualquer pasta da raiz até a do pacote), caminho de pacote com link simbólico, arquivo de configuração que é link (mesmo quebrado), `workspaces` ou
dependência `file:`/`link:`/`portal:`. A conferência da branch só olha os comandos descobertos: o `env_up` do config
da pessoa sempre roda. O que cita deploy, produção, release ou ferramentas de nuvem e acesso remoto nunca vira ambiente,
e o que fica fora da lista vira a pergunta citando o candidato. `setup` sai da instalação pelo lock, com `--ignore-scripts`, e `node_dir` do
pacote com `@playwright/test`. `repo_discovery.py <instância> show` mostra o que ficou.
Nada que sirva: `qa_env.py` sai 4 com a linha `PERGUNTAR:`, que vira `fila blocked` para a pessoa definir os comandos
no `config.json` (no computador). O que está no config (abaixo) sempre vence o que foi descoberto.

## 1. A coluna no Planou

No projeto, em Estados:
- crie o estado `QA`, dono o funcionário do agente de QA (a chave dele no Planou), "Ao terminar, vai para" o estado de
  release;
- no estado que vem antes, "Ao terminar, vai para" `QA`.

O `fila ajuste` do QA devolve a quem fez o trabalho (o dev), onde quer que a coluna do QA esteja (Planou 0.49.0):
- `dev -> QA -> release`: o ajuste volta ao dev.
- `dev -> revisão -> QA -> release`: o ajuste também volta direto ao dev, e não ao revisor; depois do retrabalho, o
  handoff do dev passa de novo pela revisão.
Nos dois casos, `send_back: "ajuste"` (o padrão). O dev recebe `-- AJUSTE PEDIDO <PID> (<função do QA> <nome>): ...`.

## 2. A pasta da instância

```
~/.config/agent/qa/
  config/config.json
  config/instructions.md
  secrets/planou.env      (python3 -m watch_core.planou --agent qa key set; 0600)
```

`config/config.json`, com os valores do Planou (o `scripts/e2e-env.sh` sobe banco, API e front
descartáveis, um por worktree, em portas livres):

```json
{
  "schema": 1,
  "interval_s": 600,
  "sources": [],
  "hooks": [],
  "behaviors": ["task-queue", "acceptance-testing"],
  "behavior_config": {
    "acceptance-testing": {
      "setup": "cd src/Planou.Web && npm ci --no-audit --no-fund",
      "env_up": "scripts/e2e-env.sh up",
      "env_down": "scripts/e2e-env.sh down",
      "env_url": "scripts/e2e-env.sh url",
      "served_urls": ["^https?://(www\\.|app\\.)?planou\\.com(/|$)", "^https?://(localhost|127\\.0\\.0\\.1|\\[::1\\]):5066(/|$)"],
      "node_dir": "src/Planou.Web/node_modules",
      "widths": [1360, 834, 390],
      "min_target_px": 44,
      "axe_tags": ["wcag2a", "wcag2aa", "wcag21aa"],
      "up_timeout_s": 600,
      "pr_comment": true,
      "send_back": "ajuste",
      "video_width": 1360
    }
  },
  "tools": [{"kind": "cli", "name": "gh", "env": {"GH_CONFIG_DIR": "~/.config/gh-<conta>"}}],
  "autonomy": {
    "can": ["ler PR e diff", "subir e derrubar o ambiente de teste", "rodar script de QA no ambiente descartável",
            "comentar o parecer na PR", "anexar parecer, capturas e o vídeo do roteiro (dados de teste) na tarefa"],
    "ask_first": ["qualquer coisa fora do ambiente descartável"],
    "never": ["escrever código", "commit", "push", "merge", "deploy", "suíte completa", "entrar com a conta do usuário"]
  },
  "repos": [{"path": "~/src/planou", "worktrees": "~/wt", "rules": "CLAUDE.md"}],
  "planou": {"project": "PLN", "task_queue": true}
}
```

- `env_up` roda na worktree destacada `~/wt/qa-<pid>`. Depois dele, o `env_url` (na mesma worktree) imprime a URL do
  ambiente numa linha (`scripts/e2e-env.sh url`, Planou v0.51.0 ou mais). Saída diferente de 0 ou vazia quer dizer que
  o ambiente não subiu: o QA derruba tudo e sai com 1. Sem `env_url`, a URL sai da primeira `http(s)://` que o `env_up`
  imprime (ex.: `ambiente de e2e no ar: http://127.0.0.1:41234`) ou, com porta fixa, de `url`.
- `served_urls`: as URLs do app com dados reais (produção, a porta servida localmente). Casou, o QA recusa.
- `node_dir`: onde o script efêmero acha `@playwright/test` e `@axe-core/playwright` (ele roda fora do repositório,
  pelo `NODE_PATH`). O `setup` instala essas dependências na worktree.

`config/instructions.md`, exemplo:

```markdown
# qa

Agente de QA do projeto Planou (PLN): dono da coluna QA. Testa cada entrega como usuário antes do release.

- Regras do repositório: CLAUDE.md da worktree (visual, larguras, contraste, alvos de 44 px no celular).
- Conta de teste: o ambiente descartável aceita cadastro. No script, `GET /api/auth/csrf` e
  `POST /api/auth/register` com `{name, email, password}` falsos e o cabeçalho `X-XSRF-TOKEN`; depois entrar pela
  tela de login (rótulos "E-mail" e "Senha", botão "Entrar"). Dados iniciais pela API (`/api/projects`, `/api/tasks`).
- Olhar também o tema escuro nas telas que a entrega muda.
- Nunca: o app servido, a conta do usuário, dado real de cliente, `scripts/e2e.sh`, suíte completa.
```

## 3. Conferir

```bash
S=<pasta da skill agent>
python3 $S/scripts/agent.py qa --validate      # config e opções do qa
python3 $S/scripts/agent.py qa --load          # instructions.md, task-queue e qa, nessa ordem
python3 $S/scripts/qa_env.py qa status         # nenhum ambiente de QA
```

Um ensaio sem o Planou, numa branch qualquer já enviada ao GitHub:

```bash
python3 $S/scripts/qa_env.py qa up ENSAIO --branch <branch>    # QA AMBIENTE ENSAIO: <url> ... e a linha RODAR:
# sem "repos" no config: --pr-url <link da PR> (clona e descobre); o que achou: repo_discovery.py qa show
# um run.cjs curto no scratchpad (BEHAVIOR.md, passo 5), rodado com a linha RODAR:
python3 $S/scripts/qa_env.py qa down ENSAIO
```

## 4. Ligar (decisão do usuário)

`"live": true` no config, a chave do Planou gravada e `/agent qa` numa sessão. A partir daí, cada tarefa que chegar na
coluna QA segue o BEHAVIOR.md.
