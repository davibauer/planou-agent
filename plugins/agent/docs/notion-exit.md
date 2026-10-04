# Saída do Notion

Estado: levantamento e comparador (só leitura). Nada aqui desliga o Notion.

Plano: enquanto uma instância grava no Notion e no Planou ao mesmo tempo, um comparador diário confere se os dois dizem
a mesma coisa. Depois de 14 dias úteis seguidos sem diferença, o agente avisa que o Notion daquela instância pode ser
desligado. Desligar é decisão do usuário, feita no config da instância, nunca automática.

## 1. O que cada agente grava no Notion hoje

Todo acesso ao Notion é pela REST, com o token de uma integração interna. Os agentes de trabalho usam o token do
`watch_core` (`~/.config/watch-core/secrets/notion.env`); o job-scout usa o dele (`~/.config/job-scout/secrets/notion.env`)
e cai no do `watch_core` quando não tem. As páginas ficam em `Dailies / AAAA/MM / AAAA-MM-DD (dia)`
(`notion.dailies_page` e `notion.month_page` no config do `watch_core`).

| Tipo | Quem grava | Onde no Notion | Código | O que escreve | O que lê de volta |
|---|---|---|---|---|---|
| Daily (fala) | instâncias do work-watch com o gancho `daily` | seção `## <sigla>` da página do dia (cria a página do mês e a do dia quando faltam) | `hooks/daily.py`, `watch_core/daily.py`, `watch_core/notion_page.py` | a fala do dia (Ontem, Hoje, Impedimentos), escrita pela sessão com `--daily md` e republicada no fechamento | nada |
| Tarefas | instâncias do work-watch com o gancho `tarefas` | a mesma seção `## <sigla>`, como checklist nativo | `hooks/tarefas.py`, `watch_core/tasks.py` | cada ação (`aN`) e pendência (`pN`) do estado, com id fixo `<sigla>-N`: Top do dia, Impedimentos, A fazer hoje, Backlog, Feito hoje, Aguardando por, Ontem; só os blocos que mudaram | caixinha marcada = feito (ação concluída, pendência resolvida); desmarcada = reaberta |
| Decisões | as mesmas instâncias | opções aninhadas no item da decisão (`- [ ] <sigla>-N-A ...`) e o que o agent precisa (`v-*`) | `watch_core/notion_decisions.py`, `watch_core/tasks.py` (`le_caixinhas`) | as opções, com a recomendada marcada | a opção marcada vira a resposta da decisão |
| Ações manuais | as mesmas instâncias | só um link guardado no estado (`--acao notion N URL`) | `watch_core/daily.py` | nada | nada |
| Busca de vaga na página do dia | job-scout com `daily_tasks` no config | seção `## <code>` da página do dia, sem fala e sem Ontem; nunca cria a página | `job-scout/scripts/daily_tasks.py` (o mesmo motor `watch_core.tasks`) | A fazer hoje (mensagens de recrutador, entrevistas), Shortlist do dia, Aguardando por (candidaturas), Feito hoje | caixinha marcada: "Aplicar" vira `applied`, mensagem resolvida |
| Funil de vagas | job-scout com a base criada (`notion_jobs.py --create`) | base de vagas do job-scout (uma linha por vaga) | `job-scout/scripts/notion_jobs.py`, `notion_min.py` | status, encaixe, formato, faixa, Match, fonte, link, CV, Candidatura; linha arquivada quando a vaga sai do funil | a coluna Pedido (`detalhar`, `candidatura`, `aplicar`, `descartar`), limpa depois de feita |
| Matriz requisito x experiência | job-scout | corpo da página da vaga na base | `job-scout/scripts/matrix.py`, `notion_jobs.py` | a tabela da matriz (os blocos antigos são apagados antes) | nada |

Não gravam no Notion: travel-agent (planilha no Google Sheets), planou e as instâncias só do plugin `agent` sem os
ganchos `daily` e `tarefas`.

## 2. O equivalente no Planou

| Tipo | No Planou | Como chega | Lacuna |
|---|---|---|---|
| Daily (fala) | nada | nada | **sem par**: a fala do dia não tem lugar no Planou. Desligar o Notion de uma instância que usa o gancho `daily` perde a fala, mesmo com 14 dias limpos. Antes de desligar: decidir se a fala some, vai para a Conversa ou vira um recurso novo do Planou |
| Tarefas | tarefas do agente no projeto da sigla, com o mesmo `source_key` (`<agente>:<código>`) | `POST /v1/agent/sync` com a mesma lista do motor (`planou_tick.after`); concluir, reabrir, editar no Planou volta por evento (`task_completed`, `task_reopened`, `task_changed`) | o Top do dia e o plano do dia (`--hoje`) não têm par no Planou |
| Decisões e o que o agent precisa | "Precisa de você" (asks com opções e a recomendada) | `reconcile_asks` a cada tick; a resposta volta como `decision_answered` | nenhuma |
| Busca de vaga na página do dia | tarefas do job-scout, com as etapas do funil como estados do projeto (`planou.stage_map`) | `daily_tasks.planou_tick` | a Shortlist é uma lista do dia; no Planou são tarefas "Aplicar" |
| Funil de vagas | parcial: as etapas viram estados do projeto; as colunas da base (encaixe, faixa, Match, CV) não têm campo | `daily_tasks.planou_tick` | colunas sem campo; a coluna Pedido tem par na Conversa (mensagem do usuário), não num campo da tarefa |
| Matriz | anexo da tarefa, quando os anexos automáticos entrarem (PR de anexos em aberto) | `attach` | enquanto isso, só no Notion e no `pipeline.md` |

## 3. O comparador (`notion_compare`)

Gancho do plugin `agent`, com cópia idêntica no work-watch. Desligado por padrão: só roda na instância que o tiver na
lista de ganchos. Nunca escreve no Notion nem no Planou.

### O que compara

- **Lado Notion**: a seção `## <sigla>` da página do dia. Por padrão (`"from": "local"`), o markdown que o motor de
  tarefas publicou por último para aquele dia (`tarefas_notion.render[<dia>]` no estado), sem rede. Com
  `"from": "api"`, a mesma seção lida do Notion só com GET, com o token que a instância já usa, na página que o motor
  registrou no estado (`daily.notion.pagina_dia`); pega também caixinha marcada à mão que o tick ainda não aplicou.
- **Lado Planou**: `cache/planou/state.json` da instância, o que o Planou confirmou no último sync (pid, título, estado
  por código). A API do agente (`/v1`) não tem rota que liste as tarefas de um agente; uma edição da pessoa no Planou
  entra quando o evento dela chega ao agente (tick seguinte). Uma rota de leitura no Planou deixaria o lado Planou
  independente do agente.
- **Chave**: o código interno da tarefa (`a3`, `p12`); a saída mostra o id da página (`<sigla>-N`) e o pid.

### O que é diferença

| Caso | Linha |
|---|---|
| tarefa na página que o Planou não conhece | `-- TST-4: falta no Planou` |
| tarefa aberta no Planou que a página não mostra | `-- TST-9: aberta no Planou, fora do Notion · Planou TST009` |
| estado por grupo diferente (aberta, aguardando, impedimento, fechada) | `-- TST-2: estado: Notion aguardando, Planou fechada · Planou TST002` |
| título diferente (a mesma troca de crase e colchete que a página faz; sem o prefixo "Decidir:") | `-- TST-1: titulo difere · Planou TST001 · "<título curto>"` |

Não é diferença: tarefa fechada que o Planou tem e a página não mostra (mais antiga que ontem, "Sem ação",
"Descartada"); tarefa que o Planou guarda em backlog ou em refinamento comparada como aberta; título sob
`"confidentiality": "minimum"` (o Planou recebe um título neutro por desenho). Sob `minimum`, a saída nunca traz título.

### Quando roda e a sequência

- Uma vez por dia útil (seg a sex, fuso da instância), no primeiro tick pesado em modo live a partir de `at_hour`
  (20 h). Tick `--dry`, `--since` e instância em modo teste não rodam.
- Cada dia vira uma linha em `data/notion_compare.jsonl`: `day`, `at`, `page`, `from`, `status` (`ok`, `diff`,
  `skipped`), `reason`, `notion`, `planou`, `diffs` (código, id, pid, tipo; nunca título) e `streak`.
- **Sequência**: dias úteis com comparação limpa, contados para trás a partir da última. Um dia com diferença zera.
  Um dia pulado (nada publicado no Notion naquele dia, sem cache do Planou) não conta nem zera. Um buraco de mais de
  `max_gap_days` (4) dias corridos entre duas comparações (runner parado, férias) zera: sem conferir, não conta.
- Diferença acorda a sessão com `== NOTION x PLANOU (n)` e uma linha por tarefa.
- Na primeira vez que a sequência chega a `days` (14): `== NOTION PODE DESLIGAR (<instância>)`, uma vez só por
  sequência.

### Ligar numa instância

No config da instância (`~/.config/agent/<instância>/config/config.json`, ou `~/.config/work-watch/<x>/config/config.json`
enquanto ela roda no work-watch), acrescentar o gancho à lista existente, depois dos ganchos `daily` e `tarefas`:

```json
{"type": "notion_compare", "at_hour": 20, "days": 14}
```

No work-watch a grafia antiga também vale: `{"tipo": "notion_compare"}` em `ganchos`. Opções: `at_hour`, `days`,
`max_gap_days`, `from` (`local` ou `api`), `code` (padrão: a sigla da instância). Antes de mexer no config, conferir o
`config-snapshot`. Vale no próximo tick (sem relançar a sessão, se o plugin em uso já tiver o gancho).

Consultar ou comparar na hora:

```bash
python3 scripts/agent.py <instância> --notion-compare          # última comparação e a sequência
python3 scripts/agent.py <instância> --notion-compare agora    # compara agora (conta como a de hoje)
```

### Desligar o Notion (decisão do usuário)

Não há chave de desligar. Desligar o Notion de uma instância é tirar os ganchos `daily` e `tarefas` do config dela (e,
no job-scout, o `daily_tasks` e a base do funil). Antes, resolver a lacuna da fala da daily (seção 2). Com os ganchos
fora, o comparador passa a registrar `skipped` e pode sair também.
