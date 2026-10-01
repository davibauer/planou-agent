# Radar de produto (comportamento `product-radar`, instância `product-scout`)

O product-scout ("Radar de produto", cargo "Analista de produto (inteligência competitiva)") olha a concorrência e os
projetos parecidos com o produto e propõe no máximo 3 ideias por semana no Backlog, nunca código. É o irmão do
[radar técnico](tech-radar.md): aquele olha a stack, este olha o produto, com a mesma coleta (busca, leitura de RSS e
Atom, uma varredura por semana, nada repetido). As regras estão em
[`behaviors/product-radar/BEHAVIOR.md`](../skills/agent/behaviors/product-radar/BEHAVIOR.md); as ideias, em
[`scripts/product_radar.py`](../skills/agent/scripts/product_radar.py).

- **Fontes**: `product_feeds` (changelogs, notas de versão e RSS públicos de apps de tarefas, de produtos de agentes, do
  Product Hunt e do Show HN) e `product_github` (repositórios novos da semana nos tópicos do GitHub). Um feed que cai
  vira `AVISO` e os outros seguem; `FONTE QUEBRADA` só quando nenhum responde.
- **Uma vez por semana** (`weekday`, `hour`), cada fonte guarda o que é novo em `data/product-radar/<semana>.json`.
- **Compara antes de propor**: as tarefas do projeto (abertas e fechadas há menos de `known_days`, por
  `GET /v1/agent/projects/{key}/flow`), o README e o CHANGELOG do produto (`planou_repo`) e as ideias de outras semanas
  (pelo link e pelo título).
- **No máximo 3 por semana**, contando o que já foi publicado na semana. Cada ideia traz o que o concorrente faz com o
  link público, por que serviria ao produto, um esboço de até 3 linhas, o tamanho (P, M ou G) e o risco. Nasce no
  Backlog, P4, sem o selo "o agente pode fazer".
- **Só a ideia**: nunca a marca, o texto, o visual ou o código de outro produto. Busca externa só com termos genéricos.

## Fontes do modelo (conferidas em 30/09/2026 com o mesmo código do runner)

| grupo | produto | feed |
|---|---|---|
| tarefas | Linear | `https://linear.app/rss/changelog.xml` |
| tarefas | Asana (notas de versão e novidades do fórum oficial) | `https://forum.asana.com/c/forum-en/news/release-notes/58.rss`, `.../product-updates/60.rss` |
| tarefas | ClickUp | `https://feedback.clickup.com/api/changelog/feed.rss` |
| agentes | Devin | `https://docs.devin.ai/release-notes/overview/rss.xml` |
| agentes | Factory | `https://docs.factory.com/changelog/rss.xml` |
| agentes | CrewAI, OpenHands | `releases.atom` do GitHub de cada um |
| agentes | agentes do GitHub Copilot | `https://github.blog/changelog/label/copilot/feed/` |
| agentes | Cursor (background agents) | `https://cursor.com/changelog/rss.xml` |
| agentes | Taskade | `https://www.taskade.com/changelog/feed.xml` |
| descoberta | Product Hunt (produtividade) | `https://www.producthunt.com/feed?category=productivity` |
| descoberta | Show HN | `https://hnrss.org/show` |
| descoberta | tópicos do GitHub | busca pública `api.github.com/search/repositories` (`ai-agents`, `multi-agent`, `task-management`, `todo-app`) |

Ficaram de fora, sem feed público (anotados em `no_feed` da fonte): Todoist, Things, TickTick (o RSS do blog parou em
2021), Height (o site não respondeu; produto descontinuado), Motion, Lindy e Relevance AI. Quando um deles publicar um
feed, basta acrescentar em `feeds`.

## Modelo de instância

Em [`config-example/product-scout/`](../skills/agent/config-example/product-scout/): `config.json` (em modo teste, sem
`"live": true`) e `instructions.md`. O Novo funcionário do Planou com o papel `product-radar` já traz as fontes e as
opções deste modelo (`behaviors/product-radar/instance.json`); a pessoa edita os feeds, os tópicos e os `terms` depois.

## Passo a passo

1. **Funcionário.** No Planou, Time > Novo funcionário: nome "Radar de produto", **nome técnico `product-scout`** (o
   campo sugere `radar-de-produto`; troque), papel `product-radar`, projeto PLN (o primeiro marcado é o padrão), pasta
   de trabalho `~`, autonomia semi-autônoma, sem ligar (modo teste). O provisionador cria a instância com as fontes, as
   opções, a autonomia e o `instructions.md` do modelo. O papel só aparece na lista depois que um agente com o plugin
   atualizado manda o catálogo (aba Papel), e o provisionador só traz as fontes depois de reiniciar com o plugin novo
   (`systemctl --user restart agent-provisioner`).
2. **Conferir.** `python3 $S/scripts/agent.py product-scout --validate` e, no `config.json`, `planou_repo` apontando para
   a pasta do repositório do produto.
3. **Ensaio sem rede.** `python3 $S/scripts/product_radar.py product-scout --dry --fixtures
   $S/behaviors/product-radar/fixtures`: fixtures sintéticas, nada gravado.
4. **Ensaio com rede.** `python3 $S/scripts/agent.py product-scout --dry`: as duas fontes de verdade, sem gravar.
5. **Ligar.** `"live": true` no `config.json` da instância. Ligar é decisão da pessoa.

A chave do funcionário precisa do escopo de tarefas próprias (`tasks:own`, que já vem na chave emitida pelo Novo
funcionário) e do projeto PLN marcado no funcionário: é o que cria as tarefas no PLN pelo `sync` e lê o `/flow` do
projeto. Nenhuma conta em serviço externo: tudo é público.

## Saídas

Tick da semana:

```
== RADAR DE PRODUTO product_feeds 2026-W40 (3 novas)
  [app de tarefas] Location reminders (app-exemplo): https://changelog.example.com/location-reminders
  [descoberta] [agent] Agent board: hire AI agents as teammates for your task list (lancamentos): https://launches.example.com/agent-board
```

Ideias (`product_radar.py product-scout --ideas ideias.json`):

```
DESCARTADA: Lembretes por localização: já existe: PLN0100 Lembrete por localização
IDEIA: [M] Quadro de agentes por equipe (App de exemplo): https://launches.example.com/agent-board
PLANO: .../data/product-radar/plan-2026-W40.json
PUBLICAR: python3 .../backlog.py product-scout .../plan-2026-W40.json --max 1
```
