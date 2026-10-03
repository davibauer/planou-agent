---
title: Protótipo antes do dev
summary: Tarefa que muda tela com o desenho em aberto ganha um protótipo em PNG anexado ao card e só vai para o dev com a aprovação do usuário.
layer: skill
kind: on_demand
when: uma tarefa da fila vai para o dev, muda tela e deixa o desenho em aberto, ou uma tarefa espera aprovação de protótipo (revisão do Aguardando ou o -- DECISAO dela)
---
# prototype: protótipo em PNG anexado ao card antes do dev

Na análise de cada tarefa liberada (`-- FILA LIBERADA <PID>` ou `-- FILA <PID>`), antes do `dev-worker`, a sessão vê se
ela muda tela e deixa o desenho em aberto. Se sim: primeiro o protótipo, renderizado em PNG e anexado ao card, e a
tarefa espera a aprovação do usuário; só depois o dev. Não há funcionário de UI/UX à parte: quem faz é o próprio
agente de desenvolvimento, por um worker. `PL="env PYTHONPATH=$S/scripts python3 -m watch_core.planou --agent <instância>"`.

## Quando vale

Só quando as duas coisas valem juntas, ou quando o usuário pede protótipo na tarefa:
- **Muda tela**: tela ou rota nova, mudança de layout ou de componente visível, estado novo (vazio, erro, carregando),
  texto da interface que muda o que a pessoa lê ou onde clica.
- **Desenho em aberto**: o pedido não diz como a tela fica (onde fica cada coisa, que componente, que estados, que
  texto), e há mais de um jeito razoável de fazer.

Vai direto para o dev, sem protótipo:
- **Desenho já decidido**: o pedido traz o desenho (captura, protótipo, descrição que fixa o layout e os textos) ou
  segue um padrão que já existe na tela vizinha sem escolha nova.
- **Ajuste pequeno de tela**: trocar um texto, uma cor de token, um ícone, um espaçamento, alinhar ou esconder um
  elemento, sem layout novo.
- **Não muda tela**: só servidor, API, banco, teste, documentação, desempenho sem efeito visível.
- **Já tem protótipo aprovado**: o card tem os PNGs anexados e a decisão de aprovação (`-- DECISAO` com A ou B, ou a
  descrição da tarefa diz que o usuário aprovou). Também segue direto. Retrabalho (`-- AJUSTE PEDIDO`) de uma entrega
  que já passou pelo protótipo não pede outro, a não ser que o ajuste mude a tela de novo.
Na dúvida se o desenho está em aberto, faça o protótipo: é mais barato que refazer a tela depois.

## Opções (`behavior_config.prototype`)

- `node_dir`: pasta `node_modules`, relativa ao `path` do repositório em `repos`, que tem `@playwright/test` (vai no
  `NODE_PATH` do `prototype_shot.cjs`; o plugin não traz o Playwright). Opção de caminho: decide de onde o node carrega
  código, então só se edita no computador, nunca pela aba Papel do Planou.
- `widths`: larguras do protótipo, em px (padrão `[1360, 834, 390]`).
- `themes`: temas (padrão `["light", "dark"]`, claro e escuro).
- `reference`: onde está o protótipo de referência do produto (ex.: o link e o arquivo que a seção de visual das regras
  do repositório indica). Sem ela, vale o que as regras do repositório (`rules` em `repos`) dizem.
- `tokens`: arquivo de tokens de design, relativo ao `path` do repositório (cores, fontes, espaçamento, raios, sombras).
  Sem ela, o que as regras do repositório dizem.

## Passo a passo

1. `$PL fila ver`: título, descrição e critério de pronto. Escopo sem clareza segue o `planou-queue` (`fila refinar`).
2. `$PL fila started <PID> --estimate-h <H>` (a estimativa conta o protótipo e o dev) e delegar a um worker em
   background (`dev-worker`), com `$A --brief <repo> --role prototype` (o PODE fica só com o que este papel faz: nada
   de código, commit, push nem PR; sem worktree, o critério de pronto são os PNGs) e, em cima do bloco, o pedido
   abaixo. Logo depois do `Agent(worker)`: `$PL fila worker-start <PID> --role dev --label 'protótipo <PID>'`. O
   papel é `dev`, e não `other`, de propósito: a volta pelo `fila worker` (que só aceita `dev` ou `integrator`) leva o
   uso do worker para a tarefa, e o custo do protótipo entra no custo da entrega.
3. Na volta do worker: `$PL fila worker <PID> ... --key <key>` como sempre, e cada PNG vai para o card com
   `$PL attach <PID> <arquivo.png>` (até 10 MB cada; o `attach` recusa e diz por quê).
4. A tarefa espera o usuário: `printf '%s' "<pergunta>" | $PL fila blocked <PID> --note -`. A nota começa sempre por
   `Aprovação de protótipo (decisão do usuário, não destravar sozinho):` (é essa abertura que a revisão do Aguardando
   lê), lista as escolhas de desenho que o usuário precisa ver e termina com as três respostas:

   ```
   Aprovação de protótipo (decisão do usuário, não destravar sozinho): protótipo de <tela> anexado
   (<n> PNGs: 1360, 834 e 390, claro e escuro; estados vazio, erro e carregando). Escolhas: <as que importam>.
   Responda: A) aprovar e passar para o dev; B) aprovar com ajustes (diga quais: vão para o dev sem novo protótipo);
   C) refazer (diga o quê: volta um protótipo novo para aprovar).
   ```

   Cite os arquivos pelo caminho: sobem como anexo e a nota diz `[ver anexo: <nome>]`. Diga na sessão, em uma linha,
   que o protótipo espera aprovação.
5. A resposta chega como `-- DECISAO`; siga a opção que o usuário escolheu (resposta em texto livre: a que ela
   descreve; na dúvida entre B e C, pergunte de novo com `fila blocked`, nunca escolha sozinho):
   - **A) aprovar**: `$PL fila started <PID>` e o dev pelo `dev-worker`, com os PNGs aprovados no pedido (caminhos e
     o que cada um mostra). O dev segue o protótipo; o que divergir dele vai na volta, com o motivo.
   - **B) aprovar com ajustes**: não refaz o protótipo. `$PL fila started <PID>` e o dev pelo `dev-worker`, com os PNGs
     aprovados e, em cima deles no pedido, os ajustes do usuário, literais, numa linha `AJUSTES DO PROTOTIPO:`
     (valem sobre o PNG onde os dois divergirem).
   - **C) refazer**: refazer o protótipo com o pedido (o mesmo nome de arquivo: o `attach` só sobe de novo o que
     mudou) e voltar ao passo 4.

## A aprovação é do usuário

Aprovar o visual é decisão pessoal do usuário. Nunca registrar com `pergunta --auto`, nunca destravar pela recomendada
na revisão da coluna Aguardando e nunca começar o dev da tela sem o `-- DECISAO` de aprovação (A ou B). Sem resposta, a
tarefa fica em `blocked`; a vaga fica livre para outra tarefa. Com `"confidentiality": "minimum"` nada sobe ao Planou:
os PNGs ficam no computador, o caminho e a pergunta vão na sessão e a aprovação vem por lá.

## O pedido ao worker (protótipo, sem código no repositório)

- **Base**: o protótipo de referência (`reference`, ou o que as regras do repositório indicam; um Artifact se lê com o
  `Artifact` action `read` e o `path` do arquivo) e os tokens (`tokens`). Copie os valores dos tokens para o HTML; não
  invente cor, fonte, espaçamento, raio ou sombra fora deles.
- **O que desenhar**: a tela da tarefa e os estados dela, cada um numa seção com título (vazio, erro, carregando e o
  estado com dados), num HTML só, com dados de teste fictícios. Nada de dado real, nome de cliente ou de pessoa.
- **Critérios**: hierarquia clara (o que importa primeiro, uma ação principal por tela), contraste AA nos dois temas,
  alvos de toque de 44 px nas larguras de toque, sem rolagem horizontal, microcópia em pt-BR curta e sem travessão,
  coerência com as telas vizinhas da referência.
- **Onde**: o HTML e os PNGs numa subpasta do scratchpad com o PID (`<scratchpad>/<PID>-prototipo/`), nunca no
  repositório nem em commit.
- **Renderizar**: `NODE_PATH=<path do repo>/<node_dir> node $S/scripts/prototype_shot.cjs <arquivo.html> <pasta>
  [--widths 1360,834,390] [--themes light,dark] [--name <nome-curto>]`. Gera um PNG por largura e tema
  (`<nome>-<largura>-<tema>.png`, página inteira), aplicando o tema por `prefers-color-scheme` e por `data-theme` no
  `<html>`. Só aceita arquivo `.html` local: link é recusado.
- **Conferir**: abrir cada PNG (Read) e corrigir o que fugir dos critérios antes de voltar.
- **Volta**: os caminhos dos PNGs, o que cada um mostra e as escolhas de desenho que o usuário precisa ver.

## Nunca

- Link do claude.ai (nem publicar o protótipo como Artifact): o card recebe só PNG.
- Código, commit, branch ou PR para o protótipo; o código vem no dev, depois da aprovação.
- Mudança de visual fora do que a tarefa pede: vira pergunta junto com o protótipo.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao
worker o que o papel permite). Ler é sempre permitido. Os PNGs vão como anexo e a tarefa espera em `blocked`; o código
é do `dev-worker`, depois da aprovação.

```permissions
tools: subagent:worker
actions: planou.queue, planou.note
```
