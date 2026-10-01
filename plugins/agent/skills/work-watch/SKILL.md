---
name: work-watch
description: "work-watch, agent de trabalho com uma instância por empresa (work-watch-<empresa>): fontes de trabalho (repositórios, itens, chats, e-mail, agenda, clusters), fila de pendências e ganchos do dia. Atalho de `/agent work-watch-<empresa>`. Use quando o usuário invocar /work-watch <empresa>, pedir \"tem algo novo na <empresa>?\", \"chegou alguma demanda?\", \"pendências da <empresa>\", ou quando o runner da instância acordar a sessão."
---

# work-watch (atalho)

`/work-watch <x> [...]` é `/agent work-watch-<x> [...]`: siga `../agent/SKILL.md` com a instância `work-watch-<x>`.
Lá, `S` é a pasta da skill `agent`: aqui, `S=<a pasta desta skill>/../agent` (os scripts ficam em `$S/scripts/`).
Sem `<x>`: `python3 $S/scripts/agent.py` lista as instâncias; perguntar qual.

Modos (todos são `python3 $S/scripts/agent.py work-watch-<x> ...`):

| `/work-watch <x> ...` | `agent.py work-watch-<x> ...` |
|---|---|
| (nada) | carregar a instância e ligar o runner, como `/agent work-watch-<x>` |
| `pendentes` | `--pending` |
| `resolver pN` | `--resolve pN` |
| `--since 4h`, `--dry`, `--json`, `--so <fonte>` | iguais |
| `--diario`, `--daily`, `--acao`, `--licoes`, `--agenda`, `--github show REF`, `--jira show KEY` | iguais |
