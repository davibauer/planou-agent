# Agente de produto (comportamento `product`)

A pessoa escreve uma meta, ou um problema, e o agente de produto a quebra em tarefas pequenas no Backlog do projeto,
cada uma com o que fazer, por quê, critério de pronto, dependências e prioridade sugerida. A pessoa aprova; quem faz é
o dev (outro agente) ou ela. As regras do comportamento estão em
[`behaviors/product/BEHAVIOR.md`](../skills/agent/behaviors/product/BEHAVIOR.md); o formato do plano, em
[`scripts/backlog.py`](../skills/agent/scripts/backlog.py).

## Modelo de instância

`~/.config/agent/product/config/config.json` (o nome da instância é o nome do funcionário no Planou; troque `EXE` pela
sigla do projeto):

```json
{
  "schema": 1,
  "live": false,
  "language": "pt-BR",
  "tz_hours": -3,
  "business_hours": [8, 22],
  "interval_s": 600,
  "session": {"cwd": "~/src/exemplo", "aliases": ["produto"], "rotate": true},
  "sources": [],
  "hooks": [],
  "behaviors": ["planou-queue", "product"],
  "behavior_config": {"product": {"max_tasks": 8}},
  "tools": [],
  "autonomy": {
    "can": ["ler o repositório e a documentação", "criar tarefas no backlog do projeto", "perguntar à pessoa"],
    "ask_first": ["mais tarefas que max_tasks numa meta", "tarefa fora do projeto da meta"],
    "never": ["escrever código", "abrir branch ou PR", "atribuir tarefa a outro agente", "mandar mensagem a pessoas"]
  },
  "planou": {"project": "EXE", "confidentiality": "detail", "task_queue": true, "conversation": true}
}
```

`~/.config/agent/product/config/instructions.md`, curto:

```markdown
# product

Agente de produto do projeto EXE. Recebe metas pela fila e pela Conversa e as devolve como backlog refinado
(comportamento product). Não escreve código. Em dúvida sobre escopo, pergunta antes de quebrar.
```

- Sem fontes e sem ganchos: o agente acorda pela fila e pela Conversa. Não ligar o comportamento `work-watch` nesta
  instância: o sync das fontes dele apagaria as marcas de `pronta` das tarefas que o agente criou.
- `"confidentiality": "minimum"` não serve: o plano é recusado (a descrição e o critério de pronto são o trabalho).
- Com `"live": false` (como nasce toda instância) o `backlog.py` só roda com `--dry`.

## Passo a passo

1. **Funcionário e chave.** No Planou, criar o funcionário `product` e a chave (no repositório do Planou,
   `scripts/connect-agent.sh`, seção "Ligar um agente" do README), no mesmo projeto das metas. Gravar a chave com
   `python3 -m watch_core.planou --agent product key set` (lê do stdin; nunca imprimir).
2. **Config.** Criar as duas pastas e os dois arquivos acima e conferir:
   `python3 $S/scripts/agent.py product --validate`.
3. **Ensaio sem enviar.** Escrever um plano de exemplo em `~/.config/agent/product/data/plano.json` e rodar
   `python3 $S/scripts/backlog.py product ~/.config/agent/product/data/plano.json --dry`: mostra onde cada tarefa iria.
4. **Coluna da meta (opcional).** Em Estados do projeto, criar a coluna "Refinamento" (categoria Backlog ou
   Aguardando), dono `product`, "Ao terminar, vai para" o estado que a pessoa quiser (ex.: Concluída). A meta posta ali
   entra na fila do agente; o `fila done` dele passa a meta adiante.
5. **Projeto autônomo (opcional).** Para as tarefas prontas irem direto ao dev, dar ao dev a coluna A fazer (dono) e
   usar `"column": "A fazer"` e `ready_reason` no plano. Sem isso, tudo nasce no Backlog e a pessoa aprova.
6. **Ligar.** `"live": true` no config e `/agent product` numa sessão. Ligar é decisão da pessoa.

## Plano de exemplo

```json
{
  "goal": {"ref": "EXE0200", "title": "Exportar a lista em CSV"},
  "epic": true,
  "tasks": [
    {"code": "formato", "title": "Formato do CSV da lista", "what": "Definir colunas, separador e codificação",
     "why": "A rota e a tela dependem do formato", "done_when": ["formato descrito na documentação", "teste do formato"],
     "priority": "P2", "agent_can_do": "escopo fechado, só código e teste"},
    {"code": "rota", "title": "Rota que exporta a lista", "what": "GET que devolve a lista no formato definido",
     "why": "A tela chama a rota", "done_when": ["teste de API verde com 0, 1 e 1000 linhas"], "priority": "P2",
     "depends_on": ["formato", "limite"], "agent_can_do": "segue o formato definido"},
    {"code": "limite", "title": "Limite de linhas do CSV", "decide": true, "priority": "P3",
     "what": "Sem limite, 10 mil ou 100 mil linhas: sem limite pode estourar a memória, 10 mil corta listas grandes"}
  ]
}
```

Saída do envio: uma linha por tarefa e o `RESUMO`, que vai como nota da meta:

```
  CRIADA EXE0201 [P3] Exportar a lista em CSV (épico)
  CRIADA EXE0202 [P2] Formato do CSV da lista (Backlog); um agente pode fazer
  CRIADA EXE0203 [P2] Rota que exporta a lista (Backlog); depende de EXE0202, EXE0204; um agente pode fazer
  CRIADA EXE0204 [P3] Decidir: Limite de linhas do CSV (Backlog)
RESUMO: meta EXE0200 quebrada em 3 tarefas em EXE (3 no Backlog): EXE0202, EXE0203, EXE0204. Aprovar = mover para A fazer ou passar para o agente.
```
