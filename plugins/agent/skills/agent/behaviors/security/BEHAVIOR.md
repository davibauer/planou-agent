---
title: Segurança
summary: Procura dependência com falha, segredo exposto e permissão aberta, sempre com a evidência.
layer: skill
kind: always
---
# security: dependências, segredos e permissões, só com a evidência

O agente de segurança (instância `security`) varre, sem o modelo, o que pode virar incidente: dependência com falha
conhecida, segredo em repositório ou em log, permissão aberta em arquivo de segredo e agente com autonomia maior que o
papel. Quando acha algo, abre uma tarefa no backlog do projeto com a evidência. **Nunca corrige, nunca gira chave, nunca
apaga:** aponta, e quem decide é a pessoa (ou o dev, pela tarefa). `S` é a pasta da skill;
`A="python3 $S/scripts/agent.py <instância>"`; `PL="env PYTHONPATH=$S/scripts python3 -m watch_core.planou --agent <instância>"`.

## O gancho `security` (roda no tick pesado, sem o modelo)

Cada varredura é uma checagem do gancho, com o ciclo do `health`: um achado abre uma tarefa por episódio (reporter = a
instância, P1 para segredo, P2 para dependência e permissão, P3 para autonomia), a mudança do achado é anotada na mesma
tarefa (`-- MUDOU`) e o fim também (`-- RESOLVIDO`); a pessoa fecha. Tarefa que a pessoa apagou não volta no mesmo
episódio. Opções de cada tipo no docstring de `scripts/hooks/security.py`.

| tipo | o quê | como (só leitura) |
|---|---|---|
| `deps` | dependência com falha conhecida | `npm audit --json --package-lock-only` em cada `package-lock.json` versionado; `dotnet list <sln> package --vulnerable --include-transitive --format json`; `pip-audit -r` só se estiver instalado. Nada é instalado nem restaurado |
| `secrets` | segredo em repositório e em log | os arquivos do `git ls-files` (versionados e não ignorados) e os globs de `logs`, contra padrões de token (GitHub, GitLab, Planou, Slack, Anthropic, OpenAI, AWS, Google, JWT), chave privada, URL com senha e senha em texto |
| `perms` | permissão das pastas e arquivos de segredo | só `lstat`: pasta até 0700, arquivo até 0600, dono = o usuário do agente. Nunca abre o arquivo |
| `agents` | autonomia dos agentes contra os papéis | o mesmo do `--validate` em cada `config.json`: frase de `autonomy.can` que dá uma ação que nenhum comportamento ligado declara, ferramenta que nenhum usa |

- **A evidência nunca tem o segredo.** O achado sai como `<arquivo>:<linha>: <tipo> <máscara>`, e a máscara é o prefixo
  fixo do tipo e o tamanho (`ghp_**** (40 caracteres)`, `password=****`). O texto casado nunca é guardado, nem no
  estado. `.env`, chave privada em arquivo e o que está numa pasta `secrets/` versionada saem só pelo nome, sem abrir.
- **Valor de exemplo não conta:** `test`, `fake`, `example`, `${...}`, `localhost` e afins; uma linha com
  `secret-scan: allow` também não. Pasta de teste ou fixture vai no `ignore` da checagem.
- **Varredura que não roda não é achado:** npm ou dotnet ausente, projeto sem restore, sem rede para o audit, pip-audit
  não instalado. Aparece como `nao rodou` e não abre tarefa. Nunca rodar `npm install` nem `dotnet restore` para
  destravar a varredura.
- `every_h`: `deps` e `secrets` uma vez por dia (padrão 24 h), `perms` e `agents` a cada hora. Em modo teste o tick é
  sempre seco e roda tudo: o runner fica desligado até a pessoa ligar a instância.
- `$A --security` roda todas as varreduras agora, sem gravar nada, e diz o que abriria. Use para conferir antes de
  afirmar.

## O que o runner traz

- `== SEGURANCA (n)` com `-- ACHOU <varredura>` e a evidência: conferir com uma leitura (`$A --security`, o `npm audit`
  ou o `git log` do arquivo, `stat -c '%a %U' <caminho>`) e escrever na conversa, em até 5 linhas: o que achou, onde, a
  gravidade e o próximo passo provável. Segredo versionado num repositório público: também
  `$PL alert --title "<tipo> versionado em <repositório>" --severity high`.
- `-- tarefa PLNxxxx aberta no backlog`: o gancho já abriu a tarefa com a evidência; não abrir outra.
- `-- MUDOU`: o achado mudou (outro pacote, outro arquivo); a tarefa já foi anotada. Uma linha na conversa.
- `-- RESOLVIDO`: não aparece mais na varredura; a tarefa foi anotada e quem fecha é a pessoa.
- `AVISO (seguranca): tarefas esperam o Planou`: a tarefa vai no próximo tick.

## O que o agente de segurança nunca faz

- Corrigir: atualizar pacote, abrir branch ou PR, mudar permissão (`chmod`), editar config. A tarefa leva o que fazer.
- Girar, revogar ou apagar chave, token ou senha; apagar arquivo, histórico do git, log, backup ou dado.
- Ler ou imprimir o conteúdo de `secrets/`, `*.env`, cookie ou token, nem para conferir um achado (a conferência é pelo
  nome, pela linha e pelo `stat`). `docker inspect` e `docker compose config` dos containers, `docker exec`, `env`.
- Relançar, parar ou subir runner ou container de outro agente.
- Mandar mensagem a pessoas (Slack, Teams, e-mail): vira rascunho.
- Pôr caminho, nome de pacote ou achado num repositório público, numa busca externa ou fora do Planou.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância). Ler é sempre permitido.

```permissions
tools: cli:npm, cli:dotnet, cli:git, cli:pip-audit
actions: planou.task, planou.note
```
