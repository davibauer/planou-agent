---
name: job-scout
description: "job-scout, olheiro de vagas (LinkedIn e sites de vaga remota): julga cada vaga contra o seu profile.md, reputação, faixa, matriz de aderência e funil. Atalho de `/agent job-scout`. Use quando o usuário invocar /job-scout, pedir \"tem vaga nova?\", \"não quero mais vaga de X\", \"busca também Y\", \"como está o funil?\", ou quando o runner da instância acordar a sessão."
---

# job-scout (atalho)

`/job-scout [...]` é `/agent job-scout [...]`: siga `../agent/SKILL.md` com a instância `job-scout`, que liga o
comportamento `job-search` (`../agent/behaviors/job-search/BEHAVIOR.md`). Lá, `S` é a pasta da skill `agent`: aqui,
`S=<a pasta desta skill>/../agent`, `J=$S/behaviors/job-search/scripts` e `V=$J/scout.py`.

Modos (os de consulta respondem e saem, sem mexer no runner):

| `/job-scout ...` | o que roda |
|---|---|
| (nada) | carregar a instância e ligar o runner, como `/agent job-scout` |
| `setup` | `python3 $J/setup.py`, depois as perguntas do Setup (BEHAVIOR.md) |
| `pendentes`, `resolver pN` | `python3 $S/scripts/agent.py job-scout --pending` / `--resolve pN` |
| `--since 4h`, `mensagens`, `recomendadas`, `vagas`, `mercado` | `python3 $V --since 4h` / `--only <fonte>` |
| `funil`, `semana`, `metricas`, `shortlist`, `painel`, `licoes` | `python3 $V --pipeline` / `--week` / `--metrics` / `--shortlist` / `--board` / `--lessons` |
| `vaga <id>`, `candidatura <id>`, `entrevista <id>`, `criterios`, `perfil`, `empresas`, `aprender`, `sessao` | seções do BEHAVIOR.md |
| `parar` | `bash $S/scripts/runner.sh job-scout stop` |
