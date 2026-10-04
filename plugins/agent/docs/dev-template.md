# Instância dev de um projeto: modelo e passo a passo

Uma instância dev é um agent de desenvolvimento de um projeto: pega tarefas da fila do Planou, delega cada entrega a
um worker e, quando o projeto publica em lote, dispara o integrador e avisa de cada deploy. O que muda de um projeto
para outro fica no `config.json` da instância, nunca nos comportamentos:

| O que | Onde no config |
|---|---|
| repositório, pasta das worktrees, branch base | `repos[].path`, `worktrees`, `base` (padrão `main`) |
| arquivo de regras do projeto e regras comuns | `repos[].rules` (relativo ao repositório), `shared_rules` |
| mapa curto do repositório (o que fica em cada pasta; o `--brief` aponta para ele) | `repos[].map` (relativo ao repositório; sem ele, `docs/MAP.md` quando existe) |
| testes filtrados e suíte completa | `repos[].tests.filtered`, `tests.full` |
| como a entrega chega na principal | `repos[].release`: `batch` (release em lote pelo integrador), `pr` (merge da PR com a CI verde), `none` (a pessoa faz o merge) |
| critério de pronto do worker | `repos[].done` (sem ele, o padrão do `release`) |
| repositório público (o `code-review` reprova nome de cliente ou de pessoa; o `--brief` avisa o worker) | `repos[].public: true` (o antigo `behavior_config.code-review.public_repos` continua valendo) |
| conta do GitHub | `repos[].gh_account` e o `env` da ferramenta `gh` em `tools` |
| deploy, travas, log de deploys, e2e completo | `behavior_config.batch-release`: `deploy_cmd`, `test_lock`, `deploy_lock`, `deploy_log`, `e2e_marker`, `e2e_every_h`, `fragments`, `check_url` |
| mínimo de branches e espera do release | seção Release do projeto no Planou (reserva: opções do gancho `release_due`) |

Os comportamentos são sempre os mesmos: `task-queue` (a fila), `delegate-to-worker` (o pedido ao worker) e, com release em
lote, `batch-release` (integrador e aviso de deploy). `agent.py <instância> --brief [repo]` junta o que o config diz
de um repositório no bloco que vai no pedido ao worker; `--validate` confere tudo.

## Ligar um projeto novo

1. Crie a pasta e copie o modelo (`S` é a pasta da skill agent):
   ```bash
   mkdir -p ~/.config/agent/<instância>/config
   cp $S/config-example/dev/config.json $S/config-example/dev/instructions.md ~/.config/agent/<instância>/config/
   ```
2. No `config.json`, troque os valores de `meu-projeto`: `session.cwd`, `repos` (caminho, regras, testes, release,
   conta), `behavior_config.batch-release` e a `path` do gancho `deploy_log` (a mesma do `deploy_log` do
   batch-release), `planou.project` (a sigla do projeto no Planou) e a `autonomy`.
   - Projeto sem release em lote (só PR): `"release": "pr"` ou `"none"`, e tire `batch-release` de `behaviors`, o
     bloco dele de `behavior_config` e os ganchos `release_due` e `deploy_log`.
   - Mais de um repositório: uma entrada em `repos` para cada, com `name` diferente; só um com `"release": "batch"`.
3. No `instructions.md`, troque os marcadores `<...>` e escreva só o que o config não diz.
4. Chave do agente no Planou em `~/.config/agent/<instância>/secrets/planou.env` (`PLANOU_AGENT_KEY=...`, 0600).
5. Confira, ainda em modo teste (`"live": false`):
   ```bash
   python3 $S/scripts/agent.py <instância> --validate
   python3 $S/scripts/agent.py <instância> --load
   python3 $S/scripts/agent.py <instância> --brief
   ```
   O `--validate` avisa quando um repositório com `"release": "batch"` não é o `repo` do batch-release, quando o
   batch-release não está em `behaviors` e quando o gancho `deploy_log` lê outro log.
6. Ligar de verdade (`"live": true`) e subir o runner é decisão do usuário.

## Exemplo: um projeto com release em lote e um só com PR

```json
"repos": [
  {"path": "~/src/app", "name": "app", "worktrees": "~/wt", "rules": "CLAUDE.md",
   "shared_rules": ["~/.config/app/worker-rules.md"], "gh_account": "exemplo",
   "tests": {"filtered": "dotnet test tests/App.Tests --filter \"FullyQualifiedName~<Classe>\"",
             "full": "dotnet test"},
   "release": "batch",
   "done": "branch feat/... ou fix/... com rebase na main e push, testes filtrados verdes, texto do changelog em docs/changelog.d/<branch>.md e capturas quando muda tela; voltar com PRONTA PARA RELEASE: <branch>"},
  {"path": "~/src/plugins", "name": "plugins", "worktrees": "~/wt", "rules": "README.md", "gh_account": "exemplo", "public": true,
   "tests": {"filtered": "python3 -m unittest discover -s plugins/<plugin>/tests"},
   "release": "ci", "fragments": "plugins/<plugin>/changelog.d/<branch>.md"}
],
"behavior_config": {"batch-release": {"repo": "~/src/app", "deploy_cmd": "scripts/deploy-local.sh",
  "test_lock": "~/.config/app/test.lock", "deploy_lock": "~/.config/app/deploy.lock",
  "deploy_log": "~/.config/app/deploys.log", "fragments": "docs/changelog.d",
  "e2e_marker": "~/.config/app/e2e-full.last", "e2e_every_h": 24, "check_url": "https://app.exemplo.com"}}
```

No repositório com `"release": "ci"` o worker abre a PR com o rótulo `automerge` e volta: a CI faz o merge, a versão,
a tag e a publicação (o `ci.yml` do repositório dos plugins é o modelo), e o gancho `tag_pull` atualiza a cópia em uso:
`{"type": "tag_pull", "path": "<cópia em uso>", "versions_file": "~/.config/publish-plugin.json"}` em `hooks`.

O integrador roda `flock -o <test_lock> <tests.full>` e `flock -o <deploy_lock> <deploy_cmd>`; o worker de
desenvolvimento só roda `tests.filtered` e nunca espera pela trava.
