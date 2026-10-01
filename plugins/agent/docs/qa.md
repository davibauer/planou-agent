# Instância de QA: modelo e passo a passo

O comportamento `qa` (skills/agent/behaviors/qa/BEHAVIOR.md) testa a entrega como usuário antes do release: sobe um
ambiente descartável da branch, percorre o fluxo da tarefa com um script Playwright efêmero, confere axe, as larguras e
os alvos de toque, e aprova (handoff) ou devolve com o pedido de ajuste. Nunca escreve código.

Este guia monta a instância em **modo teste** (sem `"live": true`). Ligar é decisão do usuário.

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
  "behaviors": ["planou-queue", "qa"],
  "behavior_config": {
    "qa": {
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
      "send_back": "ajuste"
    }
  },
  "tools": [{"kind": "cli", "name": "gh", "env": {"GH_CONFIG_DIR": "~/.config/gh-<conta>"}}],
  "autonomy": {
    "can": ["ler PR e diff", "subir e derrubar o ambiente de teste", "rodar script de QA no ambiente descartável",
            "comentar o parecer na PR", "anexar parecer e capturas na tarefa"],
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
python3 $S/scripts/agent.py qa --load          # instructions.md, planou-queue e qa, nessa ordem
python3 $S/scripts/qa_env.py qa status         # nenhum ambiente de QA
```

Um ensaio sem o Planou, numa branch qualquer já enviada ao GitHub:

```bash
python3 $S/scripts/qa_env.py qa up ENSAIO --branch <branch>    # QA AMBIENTE ENSAIO: <url> ... e a linha RODAR:
# um run.cjs curto no scratchpad (BEHAVIOR.md, passo 5), rodado com a linha RODAR:
python3 $S/scripts/qa_env.py qa down ENSAIO
```

## 4. Ligar (decisão do usuário)

`"live": true` no config, a chave do Planou gravada e `/agent qa` numa sessão. A partir daí, cada tarefa que chegar na
coluna QA segue o BEHAVIOR.md.
