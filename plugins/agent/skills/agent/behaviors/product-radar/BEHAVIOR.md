---
name: product-radar
description: "Olha o que produtos parecidos lançam e propõe até 3 ideias por semana. Sempre ativa na instância que a liga em \"behaviors\"."
title: Radar de produto
summary: Olha o que produtos parecidos lançam e propõe até 3 ideias por semana.
layer: skill
kind: always
---
# product-radar: o que produtos parecidos lançam vira até 3 ideias por semana

O agente de radar de produto (instância `product-scout`, cargo "Analista de produto (inteligência competitiva)") olha a
concorrência e os projetos parecidos com o produto e propõe, no máximo 3 vezes por semana, uma ideia que valeria a pena
trazer. É o irmão do `tech-radar`: aquele olha a stack, este olha o produto. Vale com `task-queue` ligado. `PL` é o do
`task-queue`; `S` é a pasta da skill; `PR="python3 $S/scripts/product_radar.py <instância>"`;
`BL="python3 $S/scripts/backlog.py <instância>"`.

## Fontes (no `sources` do config, editáveis pela pessoa)

- `product_feeds`: changelogs, notas de versão e RSS públicos, cada feed com um `group`: `tarefas` (apps de tarefas),
  `agentes` (produtos de agentes como funcionários ou de orquestração) e `descoberta` (Product Hunt, Show HN). Os feeds
  de descoberta levam `"require_match": true` e só guardam o que cita um dos `terms`.
- `product_github`: repositórios novos da semana nos tópicos do GitHub do config (busca pública, sem login).
- Só URL de feed que existe e responde. Produto sem feed público fica de fora, anotado em `no_feed` da fonte, com o
  motivo; não se raspa página.

Cada fonte roda uma vez por semana (depois de `weekday` e `hour`, hora da instância), guarda o que já viu (a mesma
notícia não volta) e o que achou em `data/product-radar/<semana>.json`. Um feed que falha vira uma linha
`AVISO (product_feeds): ...` e os outros seguem; `FONTE QUEBRADA` só quando nenhum responde. O tick traz
`== RADAR DE PRODUTO <fonte> <semana> (n novas)` com uma linha por item.

Opções em `behavior_config.product-radar`:
- `project`: o projeto onde as ideias nascem no Backlog (padrão: o `planou.project` da instância).
- `max_ideas`: ideias por semana (padrão 3, nunca mais que 3).
- `terms`: termos genéricos dos feeds de descoberta (`"task|tarefa|todo"` = alternativas).
- `weekday` (0 = segunda) e `hour`: quando a varredura da semana roda (padrão segunda, 9 h).
- `window_days`: janela dos feeds, em dias (padrão 7).
- `planou_repo`: a pasta do repositório do produto, para ler o `README.md` e o `CHANGELOG.md` (o que já existe).
- `known_days`: quantos dias de tarefas fechadas do projeto contam como "já existe" (padrão 180).

## Quando chega `== RADAR DE PRODUTO ...` (uma vez por semana)

1. `$PR`: mostra os candidatos da semana (sem o que já virou ideia antes), o que o produto já tem (as tarefas abertas e
   as fechadas há menos de `known_days` do projeto, pela `/v1`, e o README e o CHANGELOG de `planou_repo`) e quantas
   vagas a semana ainda tem.
2. Ler os links dos candidatos que parecem bons (só links públicos) e escolher no máximo as vagas da semana: o que
   resolve um problema de quem usa o produto e que ele ainda não tem. Nada que já existe, que já está numa tarefa ou
   que foi proposto antes.
3. Escrever as ideias num JSON (formato no topo de `scripts/product_radar.py`), cada uma com:
   - o que o concorrente faz, com o link público (`competitor`, `what`, `url`);
   - por que serviria ao produto (`why`);
   - o esboço de como ficaria aqui, em até 3 linhas (`sketch`);
   - o tamanho P, M ou G (`size`) e o risco (`risk`).
   Com as próprias palavras: só a ideia, nunca a marca, o texto, o visual ou o código do outro produto.
4. `$PR --ideas <arquivo>`: confere cada ideia, descarta o que já existe (tarefa, README, CHANGELOG, ideia de outra
   semana, pelo link ou pelo título) e o que passa do limite, grava o plano e mostra a prévia do `backlog.py`. Descartou
   por engano (títulos parecidos, assunto diferente)? `"force": true` na ideia, depois de conferir.
5. `$BL <plano> --max <n>` (a linha `PUBLICAR:` do passo 4 já traz o comando). Nasce no Backlog do `project`, P4, sem
   selo `agent_can_do`: quem decide é a pessoa, e a vaga livre puxaria a tarefa para este agente, que não escreve
   código. Em modo teste (sem `"live": true`) só com `--dry`.
6. Uma linha ao usuário: quantas ideias e o RESUMO. Semana sem ideia boa: nada a dizer (melhor nenhuma que uma fraca).

## O que o product-scout não faz

- Não escreve código, não abre branch nem PR, não delega worker de código (sem `fila worker`).
- Não copia marca, texto, visual ou código de outro produto: só a ideia, com as próprias palavras e o link.
- Busca externa só com termos genéricos (os `terms`, os tópicos e as URLs públicas do config): nunca nome de cliente,
  caminho de repositório ou texto de tarefa.
- Não cria conta em serviço nenhum nem entra logado em site: só o que é público.
- Não publica fora do Planou (e-mail, chat): a ideia vive na tarefa.
- Não passa de 3 ideias por semana, nem com pedido: o limite é do comportamento.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido.

```permissions
tools: -
actions: planou.task
```
