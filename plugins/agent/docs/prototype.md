# Protótipo antes do dev: como ligar e como funciona

O comportamento `prototype` (skills/agent/behaviors/prototype/BEHAVIOR.md) põe um passo entre a tarefa liberada e o
dev: toda tarefa que muda tela com o desenho em aberto ganha um protótipo em PNG, anexado ao card, e só vai para o código com a aprovação do
usuário. Quem faz é a própria instância de desenvolvimento, por um worker; não há funcionário de UI/UX à parte.

Ligar é decisão do usuário: nenhuma instância vem com ele ligado.

## 1. O que precisa existir

- Uma instância dev com `task-queue` e `delegate-to-worker` (modelo em `docs/dev-template.md`).
- O projeto já usa Playwright: o `node_modules` dele tem `@playwright/test` e o Chromium baixado
  (`npx playwright install chromium` no projeto, se ainda não tem). O plugin não traz Playwright nem outra dependência.
- Um protótipo de referência e um arquivo de tokens de design. As regras do repositório (`rules` em `repos`) costumam
  dizer onde estão; as opções `reference` e `tokens` servem quando não dizem.

## 2. O config

```json
{
  "behaviors": ["task-queue", "delegate-to-worker", "prototype"],
  "behavior_config": {
    "prototype": {
      "node_dir": "web/node_modules",
      "widths": [1360, 834, 390],
      "themes": ["light", "dark"],
      "tokens": "web/src/styles/tokens.css"
    }
  }
}
```

`node_dir` e `tokens` são relativos ao `path` do repositório em `repos`. `node_dir` é opção de caminho (vai no
`NODE_PATH`, de onde o node carrega código): só se edita no computador, nunca pela aba Papel do Planou. Confira com
`python3 $S/scripts/agent.py <instância> --validate`.

## 3. O fluxo

1. Tarefa liberada que muda tela e deixa o desenho em aberto (ou em que o usuário pede protótipo). Desenho já
   decidido no pedido, ajuste pequeno de tela (texto, cor, ícone, espaçamento) ou tarefa sem tela seguem direto para
   o dev.
2. `fila started` e um worker com `--brief <repo> --role prototype`: o pedido dele não tem código, commit, push nem PR.
3. O worker monta um HTML com a tela e os estados (vazio, erro, carregando e com dados), só com os valores dos tokens
   e dados de teste, numa subpasta do scratchpad, e renderiza:

   ```bash
   NODE_PATH=<repo>/<node_dir> node $S/scripts/prototype_shot.cjs prototipo.html saida --name filtros
   # saida/filtros-1360-light.png, filtros-1360-dark.png, ... filtros-390-dark.png
   ```

   Um PNG de página inteira por largura e tema. O tema vai por `prefers-color-scheme` e por `data-theme` no `<html>`.
   Só arquivo `.html` local: um link (claude.ai ou qualquer outro) é recusado.
4. A sessão anexa os PNGs ao card (`$PL attach <PID> <arquivo>`) e põe a tarefa em `blocked` com a pergunta de
   aprovação e três respostas: A) aprovar; B) aprovar com ajustes; C) refazer.
5. A: o dev começa, com os PNGs no pedido. B: o dev começa sem novo protótipo, com os PNGs e os ajustes do usuário no
   pedido (linha `AJUSTES DO PROTOTIPO:`). C: o protótipo é refeito e volta ao passo 4.

A aprovação é do usuário: a revisão da coluna Aguardando não destrava essa tarefa pela recomendada, nem com
`pergunta --auto`.

## 4. Conferir o papel

```bash
python3 $S/scripts/evals.py prototype      # casos de referência, em modo seco
python3 -m unittest plugins/agent/tests/test_prototype.py
```
