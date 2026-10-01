# Radar técnico (comportamento `tech-radar`, instância `tech-scout`)

O tech-scout acompanha o que sai de novo nas stacks do time e devolve isso como sugestão no Planou, nunca como código.
As regras do comportamento estão em
[`behaviors/tech-radar/BEHAVIOR.md`](../skills/agent/behaviors/tech-radar/BEHAVIOR.md); as fontes e o plano, em
[`scripts/radar.py`](../skills/agent/scripts/radar.py).

- **Fontes** (uma por família, cada uma com o seu `FONTE QUEBRADA`): `radar_deps` (versões novas das dependências dos
  repositórios do config), `radar_feeds` (blogs oficiais e canais do YouTube, por RSS ou Atom), `radar_hn` (Hacker
  News) e `radar_trending` (GitHub Trending). HN e Trending só guardam o que cita uma das `stacks`.
- **Feeds do YouTube tentam de novo**: o RSS do YouTube costuma responder 404 ou 500 algumas vezes antes de responder.
  Cada canal tem 3 tentativas, com espera de 1 s e depois 2 s, em no máximo uns 10 s por canal. Um canal que falha nas
  3 vira uma linha `AVISO (radar_feeds): youtube:UC...: HTTP 404 em 3 tentativas` (um 404 não é mais "sem novidade"),
  e a fonte segue com os outros feeds. `FONTE QUEBRADA (radar_feeds)` só quando nenhum feed responde. Blogs não tentam
  de novo.
- **Uma vez por semana** (`weekday`, `hour`), cada fonte guarda o que é novo em `data/radar/<semana>.json`; o que já
  apareceu não volta.
- **Modo projeto**: `radar.py <instância>` monta o plano da semana (uma tarefa "Avaliar ..." por sugestão, até
  `max_tasks`) e o `backlog.py` o publica no Backlog do projeto do radar (ex.: `RADAR`).
- **Modo coluna**: a tarefa que entra na coluna "Radar técnico" (dono tech-scout) ganha até 3 sugestões na nota e segue
  para o próximo estado; `max_hours` é o prazo máximo dela na coluna.

## Modelo de instância

Em [`config-example/tech-scout/`](../skills/agent/config-example/tech-scout/): `config.json` (em modo teste, sem
`"live": true`) e `instructions.md`. Copie para `~/.config/agent/tech-scout/config/` e troque os repositórios, os feeds,
as `stacks` e o projeto.

Fica com a pessoa, vazio no modelo:
- `client_repos` da fonte `radar_deps`: repositórios de cliente. Os nomes dos pacotes deles vão para os registros
  públicos (npm, NuGet, PyPI); pacote privado entra em `skip` (`"@minha-empresa/*"`) e nunca sai da máquina.
- `youtube` da fonte `radar_feeds`: ids dos canais (`UC...`). Exemplo:
  `"youtube": ["UCxxxxxxxxxxxxxxxxxxxxxx"]`.
- o nome do funcionário no Planou (o nome da instância é `tech-scout`; o nome de exibição é escolha da pessoa).

## Passo a passo

1. **Config.** Criar `~/.config/agent/tech-scout/config/` com os dois arquivos e conferir:
   `python3 $S/scripts/agent.py tech-scout --validate`.
2. **Ensaio sem rede.** `python3 $S/scripts/radar.py tech-scout --dry --fixtures $S/behaviors/tech-radar/fixtures`:
   lê os repositórios do config, responde as buscas pelas fixtures (dados sintéticos) e mostra as sugestões e a prévia
   do `backlog.py`. Nada é gravado.
3. **Ensaio com rede.** `python3 $S/scripts/agent.py tech-scout --dry`: roda as quatro fontes de verdade, sem gravar
   (em modo teste todo tick é simulado).
4. **Funcionário e chave.** No Planou, criar o funcionário `tech-scout` e a chave (no repositório do Planou,
   `scripts/connect-agent.sh`, seção "Ligar um agente" do README). Gravar a chave com
   `python3 -m watch_core.planou --agent tech-scout key set` (lê do stdin; nunca imprimir).
5. **Modo projeto (decisão da pessoa).** Criar o projeto `RADAR` no Planou e dar acesso ao funcionário. Sem ele,
   deixar `"project": ""` em `behavior_config.tech-radar` e o modo projeto fica desligado.
6. **Modo coluna (decisão da pessoa).** Em Estados do projeto onde a esteira roda, criar a coluna "Radar técnico"
   (categoria Backlog ou A fazer), dono `tech-scout`, "Ao terminar, vai para" o próximo passo (ex.: A fazer, dono o
   dev). Sem a coluna, o modo coluna não recebe nada.
7. **Ligar.** `"live": true` no config e `/agent tech-scout` numa sessão. Ligar é decisão da pessoa.

## Saídas

Tick da semana (uma fonte):

```
== RADAR radar_deps 2026-W40 (2 novas)
  [dependência major] [React] react 20.0.0 (npm; em uso 19.3.0) em meu-projeto: https://www.npmjs.com/package/react/v/20.0.0
  [dependência minor] [Vite] vite 8.5.0 (npm; em uso 8.3.1) em meu-projeto: https://www.npmjs.com/package/vite/v/8.5.0
```

Modo coluna (`radar.py tech-scout --match -`, com o texto da tarefa no stdin):

```
RADAR PARA A TAREFA: 2 de 9 itens
1. [dependência minor] [Vite] vite 8.5.0 (npm; em uso 8.3.1) em meu-projeto: https://www.npmjs.com/package/vite/v/8.5.0
2. [GitHub Trending] [Vite] example-org/vite-plugin-inspect-lite: https://github.com/example-org/vite-plugin-inspect-lite
```
