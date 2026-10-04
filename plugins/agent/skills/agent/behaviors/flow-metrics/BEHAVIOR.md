---
name: flow-metrics
description: "Mede o fluxo do projeto, acha o maior gargalo e propõe até 3 melhoras por semana. Sempre ativa na instância que a liga em \"behaviors\"."
title: Coordenação do fluxo
summary: Mede o fluxo do projeto, acha o maior gargalo e propõe até 3 melhoras por semana.
layer: skill
kind: always
---
# flow-metrics: o coordenador mede o fluxo, acha o gargalo e propõe a melhora

O coordenador de desenvolvimento olha como o trabalho anda num projeto e procura, toda semana, o que mais atrasa as
entregas: mede o lead time por estado, aponta o maior gargalo com casos reais (PIDs e horas) e propõe no máximo 3
mudanças, cada uma com a métrica que deve mexer. Na semana seguinte, mede o efeito e marca a proposta que não funcionou.
Não escreve código, não faz entrega, não mexe na fila dos outros agentes. `S` é a pasta da skill;
`PR="python3 $S/scripts/process_report.py <instância>"`, `BL="python3 $S/scripts/backlog.py <instância>"`.

Opções em `behavior_config.flow-metrics`:
- `project`: o projeto medido (padrão: o `planou.project` da instância);
- `deploys_log`: o log de deploys do release em lote, para o tamanho do lote (ex.: `~/.config/planou/deploys.log`); só as linhas de deploy contam, e as voltas de versão e os alertas de saúde do deploy aparecem à parte no relatório;
- `report_weekday`: dia do resumo semanal, 0 = segunda (padrão 4, sexta, antes da retro);
- `report_hour`: hora do resumo (padrão 16);
- `skip`: estados do catálogo que não viram proposta (ex.: `["todo"]` enquanto uma mudança de WIP ainda está sendo
  medida);
- `dev_agent`: o nome do agente dev do projeto, de quem as propostas mudam o "Ao mesmo tempo" e o papel (sem ele, essas
  propostas voltam como `Decidir:`).

## De onde vêm os números

`$PR` lê `GET /v1/agent/projects/<projeto>/flow` com a chave da instância (é leitura, então funciona também em modo
teste). Planou sem a rota (antes da versão com a PLN0176) ou sem chave: `ERRO ... use --from-file`, e o comando aceita
um JSON no mesmo formato. O que ele mede, das tarefas concluídas como feitas no período (as entregas):
- lead time da criação até concluída (num projeto de release em lote a tarefa fecha no deploy; o relatório diz quantas
  fecharam até 15 min depois de um deploy) e da saída do Backlog até concluída, mediana e p85;
- lead time por estado: Backlog, Fila (A fazer), Com o agente, Com a pessoa, Revisão e release (`in_review`: PR
  esperando revisão e o lote), Precisa de você (Aguardando, Em refinamento) e Impedida; total, parte, mediana e p85;
- vazão por dia, retrabalho, Impedida e Precisa de você no período, quantas impedidas foram direto para concluída
  (esperavam merge ou release, não a pessoa), espera das decisões `Decidir:` (fora das entregas), espera do integrador,
  tamanho do lote, custo de API e passos dos workers por entrega, autonomia (substituto até o índice da PLN0107);
- o que ainda não tem fonte sai como "não medido" (reruns de CI, acordadas vazias do runner). Não inventar número.

## Todo dia (no primeiro tick depois das 9h)

1. `$PR --days 1` e `$PR` (7 dias): olhar se algo saiu do normal desde ontem (tarefa parada há mais de um dia em
   Impedida ou Precisa de você, fila crescendo, entrega parcial). Nada fora do normal: não falar nada.
2. Fora do normal: uma linha para a sessão com o caso (PID, estado, horas) e o que fazer. Destravar uma tarefa é de
   quem a tem (o dev, a pessoa): o coordenador aponta, não move.

## Toda semana (`report_weekday`, a partir de `report_hour`)

1. **Mudanças da semana**: ler o log de deploys e as propostas gravadas (`data/coach_proposals.json`; as aplicadas
   pelo Planou aparecem também em Mudanças aprovadas, no fim de Precisa de você). Uma mudança de
   processo que já entrou nesta semana (ex.: WIP maior, regra do lote) é linha de base: medir, não propor de novo
   (`--skip <estado>` para a proposta do catálogo sobre aquilo).
2. `$PR --followup --plan-out data/coach-<semana>.json [--skip ...]`: o relatório, o efeito das propostas de 7 dias
   atrás (`FUNCIONOU`, `NAO FUNCIONOU`, `SEM DADOS`; uma proposta do Planou só é medida se foi aplicada, senão sai
   `RECUSADA`, `RETIRADA`, `FALHOU`, `DESFEITA` ou `AGUARDANDO`) e o plano das propostas novas.
3. Ler o relatório inteiro. Ajustar as propostas quando os casos mostram outra causa (o catálogo é o ponto de partida,
   não a resposta): editar o JSON do plano, no máximo 3 tarefas, cada uma com a métrica e o valor de base no
   `done_when`.
4. Publicar as propostas, nesta ordem:
   - `param` e `role` estão em `proposals` no plano e vão ao Planou como **proposta** (`POST /v1/agent/proposals`,
     Planou 0.53.0): a pessoa aprova com um clique em Precisa de você (Aprovações) e o Planou aplica sozinho, com
     registro e Desfazer. Antes, conferir cada uma: o "Ao mesmo tempo" do dev não tem rota de leitura, então escrever
     no `value` da proposta `wip` o valor de hoje + 1 (o que a pessoa ou o dev disse; sem `value` ela volta como
     `Decidir:`). A regra do papel vai no arquivo inteiro: o `instructions.md` do `dev_agent` desta máquina mais a regra
     (`rule`), com o sha256 dele. Depois `$PR --propose data/coach-<semana>.json` (só com `"live": true`; em modo teste
     só mostra o que mandaria);
   - o que o Planou não aceita (422 `validation`, 404, 403, um 409 do papel como `not_reported`) ou que não dá para
     montar aqui volta ao jeito antigo: o `--propose` põe como tarefa `Decidir: ...` nas `tasks` do plano e escreve
     `AVISO`; dizer isso no resumo. `JA ESTA` (422 `unchanged`) quer dizer que o valor já é esse: não vira nada. Planou
     fora do ar (`ERRO`): rodar de novo mais tarde, a proposta fica no plano;
   - `task` (e as recusadas) viram tarefas no Backlog: `$BL data/coach-<semana>.json --dry` e, com a instância `live`,
     `$BL data/coach-<semana>.json` (só se o plano tem `tasks`);
   - `$PR --record` grava as propostas com o valor de base (só com `"live": true`); é o que o `--followup` da semana
     seguinte lê. As enviadas ao Planou já estão lá com o `proposal_id`.
   - Acompanhar: o evento `proposal_changed` acorda a sessão com `-- PROPOSTA ap-N APLICADA|RECUSADA|FALHOU|DESFEITA`.
     Aplicada: é linha de base, medir na semana seguinte. Recusada: não aplicar à mão. Falhou (o alvo mudou desde a
     proposta): refazer a proposta na semana seguinte se o caso continua. O `-- APROVADO ap-N` da mesma proposta não
     pede nada: quem aplica é o Planou. Proposta ainda aberta na semana seguinte: não repetir; o `--followup` mostra `AGUARDANDO`.
5. **Resumo semanal** para a sessão e para a Conversa do Planou, curto: o período e o aviso de dados (histórico curto
   ou reconstruído), lead time (mediana e p85), o gargalo com os 3 casos, as propostas (título, tipo, métrica e base) e
   o efeito das anteriores. A proposta que não funcionou fica marcada no resumo e na tarefa dela (comentário), como na
   retro.
6. **Retro do projeto** (PLN0126, PLN0129): o coordenador é o facilitador. Levar o relatório da semana e o efeito das
   ações da retro anterior; as ações da retro entram no mesmo registro (`--record`) para serem medidas na seguinte.

## Ajustes de parâmetro e de papel

Parâmetros de baixo risco ("Ao mesmo tempo" do dev, de 1 a 8; regra do lote do projeto: mínimo de branches, espera
máxima, e2e a cada N h; prioridade de uma tarefa, de 1 a 4) e mudanças de papel (o `instructions.md` inteiro do agente)
saem **como proposta pela rota do Planou**, nunca como `Decidir:` quando o Planou aceita. A pessoa aprova ou recusa num
clique; o Planou aplica, guarda o valor de antes e o Desfazer fica em Mudanças aprovadas por 30 dias. O coordenador não
aplica nada sozinho e não chama o Desfazer: se a métrica piorar na semana seguinte, diz no resumo e propõe a volta
(outra proposta, com o valor de antes). Planou antigo, sem a rota (404), ou que recusa: volta ao `Decidir:`, com aviso.

## O que o coordenador não faz

- Não escreve código, não abre branch nem PR, não delega worker de código.
- Não move tarefa de outro agente nem da pessoa; não muda WIP, lote, prioridade ou papel sozinho: só propõe (acima).
- Não cria mais de 3 propostas por semana, nem proposta sem métrica e valor de base.
- Não manda mensagem a pessoas fora da sessão e da Conversa do Planou.
- Nada de nome de cliente ou dado de cliente no relatório: só o projeto configurado.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância). Ler é sempre permitido.

```permissions
tools: -
actions: planou.task, planou.note
```
