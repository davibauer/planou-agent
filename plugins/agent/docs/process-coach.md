# Coordenador de desenvolvimento (comportamento `flow-metrics`)

O coordenador mede o fluxo de um projeto no Planou, aponta o maior gargalo da semana com casos reais e propõe no
máximo 3 mudanças, cada uma com a métrica que deve mexer; na semana seguinte, mede o efeito. As regras estão em
[`behaviors/flow-metrics/BEHAVIOR.md`](../skills/agent/behaviors/flow-metrics/BEHAVIOR.md); o cálculo, em
[`scripts/process_report.py`](../skills/agent/scripts/process_report.py).

## Modelo de instância

`~/.config/agent/coordenador/config/config.json` (troque `EXE` pela sigla do projeto):

```json
{
  "schema": 1,
  "language": "pt-BR",
  "tz_hours": -3,
  "business_hours": [8, 22],
  "interval_s": 900,
  "session": {"cwd": "~/src", "aliases": ["coordenador"], "rotate": true},
  "sources": [],
  "hooks": [],
  "behaviors": ["task-queue", "flow-metrics"],
  "behavior_config": {
    "flow-metrics": {"project": "EXE", "dev_agent": "dev-exemplo", "deploys_log": "~/.config/exemplo/deploys.log",
                      "report_weekday": 4, "report_hour": 16}
  },
  "tools": [],
  "autonomy": {
    "can": ["ler o fluxo do projeto no Planou e o histórico de versões", "criar até 3 propostas por semana no backlog",
            "propor no Planou mudança de WIP, regra do lote, prioridade ou papel, para a pessoa aprovar"],
    "ask_first": ["mudar WIP, regra do lote ou prioridade da fila", "mudar a regra ou o papel de um agente"],
    "never": ["escrever código", "abrir branch ou PR", "mover tarefa de outro agente ou da pessoa", "mandar mensagem a pessoas"]
  },
  "planou": {"project": "EXE", "confidentiality": "detail", "task_queue": true, "conversation": true}
}
```

`~/.config/agent/coordenador/config/instructions.md`, curto:

```markdown
# coordenador

Coordenador de desenvolvimento do projeto EXE (comportamento flow-metrics). Mede o lead time por estado, acha o
gargalo da semana e propõe no máximo 3 mudanças com métrica. Não escreve código e não move tarefa de ninguém.
```

- Sem fontes e sem ganchos: a cadência (todo dia, resumo semanal) está no BEHAVIOR.md e o agente acorda pelo tick, pela
  fila e pela Conversa.
- Histórico reconstruído: quando o período tem entregas com transições de backfill (o Planou reconstruiu o histórico
  de antes da PLN0176), o gargalo e os casos saem só das entregas de histórico real, e o relatório diz quantas ficaram
  de fora e qual seria o gargalo com elas. Se todas as entregas têm backfill, o gargalo usa todas e o relatório avisa.
  A tabela de estados e os outros números continuam com todas as entregas.
- Sem `"live": true` a instância fica em modo teste: o relatório roda (é leitura), o `backlog.py` só com `--dry` e o
  `--record` não grava.

## Passo a passo

1. **Funcionário e chave.** No Planou, criar o funcionário `coordenador` e a chave (`scripts/connect-agent.sh` no
   repositório do Planou), com os escopos `tasks:own` e `asks:create` (as propostas). Gravar a chave com
   `python3 -m watch_core.planou --agent coordenador key set` (lê do stdin; nunca imprimir).
2. **Config.** Criar as pastas e os arquivos acima e conferir: `python3 $S/scripts/agent.py coordenador --validate`.
3. **Relatório seco.** `python3 $S/scripts/process_report.py coordenador` (7 dias). Planou sem a rota `/flow`: exportar
   o mesmo JSON e usar `--from-file`.
4. **Ensaio das propostas.** `--plan-out data/coach.json`, `--propose data/coach.json` (em modo teste só mostra o que
   mandaria ao Planou) e `python3 $S/scripts/backlog.py coordenador data/coach.json --dry`.
- Propostas (Planou 0.53.0): `param` e `role` vão pela rota `POST /v1/agent/proposals` e a pessoa aprova em Precisa de
  você. A proposta de papel manda o `instructions.md` inteiro do `dev_agent` (lido nesta máquina) com a regra no fim;
  enquanto o plugin não informa o papel do agente ao Planou, ela volta como `409 not_reported` e vira `Decidir:`, com
  aviso. O que o Planou recusa sempre volta assim.
5. **Ligar.** `"live": true` no config, com o OK da pessoa; o runner sobe pela sessão (`/agent coordenador`).
