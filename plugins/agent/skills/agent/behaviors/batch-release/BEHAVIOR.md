---
name: batch-release
description: "Junta as branches prontas, roda os testes completos, publica a versão e avisa de cada deploy. Use quando um worker volta com PRONTA PARA RELEASE, a saida traz == RELEASE ou == DEPLOY, ou vai disparar o integrador."
title: Release em lote
summary: Junta as branches prontas, roda os testes completos, publica a versão e avisa de cada deploy.
layer: skill
kind: on_demand
when: um worker volta com PRONTA PARA RELEASE, a saida traz == RELEASE ou == DEPLOY, ou vai disparar o integrador
---
# batch-release: release em lote pelo integrador e aviso de cada deploy

**Sob demanda (`planou.all_roles`, PLN0368).** Numa instância com `"all_roles": true` no bloco `planou`, este comportamento não precisa estar em `behaviors` e não é lido em toda sessão: o `--load` o lista como `sob demanda` e a sessão lê este arquivo quando a linha `-- FILA` traz `papel da coluna <coluna>: ...` com ele; o integrador leva o bloco de `$A --brief <repo> --role batch-release`. Só entra na lista quando o release em lote está configurado neste computador (`behavior_config.batch-release.repo` apontando um repositório de `repos` com `"release": "batch"`); sem isso, a coluna Release espera outro agente.

Vale para a instância que integra e publica um repositório: a lista `can` do `autonomy` inclui release em lote e
deploy. O worker de desenvolvimento termina numa branch pronta; quem junta, testa tudo, versiona e publica é um worker
só, o integrador, disparado por esta sessão. `A="python3 $S/scripts/agent.py <instância>"`.

**Regras do release: no Planou.** Mínimo de branches, espera máxima, e2e completo a cada N horas, regra de versão e
quem integra são da seção Release do projeto no Planou (menu "..." do projeto). `$A --release-queue` imprime a linha
`regras (Planou): ...` com as que valem agora; quando o Planou não responde, sai `regras (config local): ...` com as
opções do gancho `release_due` e o `e2e_every_h` abaixo. Passe essa linha ao integrador: o intervalo do e2e completo e a
regra de versão dele vêm dela, não do texto fixo.

As opções ficam em `behavior_config.batch-release` do config (`$A --status` mostra): `repo` (o repositório de `repos`
que tem release), `test_lock` e `deploy_lock` (arquivos de trava do `flock`), `e2e_marker` e `e2e_every_h` (e2e
completo no máximo uma vez nesse intervalo), `fragments` (pasta dos fragmentos de changelog no repositório) e
`deploy_log` (o log que o gancho `deploy_log` lê; cada linha nova também vai para o histórico de versões do projeto no
Planou), `check_url` (onde o usuário confere a versão nova, citado no aviso) e `deploy_cmd` (o comando de deploy,
rodado da raiz do repositório). `deploy_cmd`, `test_lock` e `deploy_lock` são opções de comando: só se editam no
computador, nunca pela aba Papel do Planou; `e2e_marker`, `fragments` e `deploy_log` são opções de caminho (o integrador
sobrescreve, apaga ou acrescenta nesses arquivos) e também só se editam no computador. A suíte completa é o `tests.full` da entrada do repositório em `repos`, que também
tem `"release": "batch"`. Sem `deploy_cmd` ou `tests.full`, o integrador segue os comandos do arquivo `rules` do
repositório; este texto só diz quando disparar, o que esperar de volta e como avisar. `$A --brief <repo> --role
batch-release` imprime o repositório, as regras, a suíte completa e o deploy já com as travas e a autonomia do
integrador, para colar no pedido ao integrador (sem o `--role`, o bloco é o do worker de desenvolvimento, sem deploy). Só quem faz release avisa de deploy: o antigo comportamento `deploy-notice` agora é a seção
"Aviso de deploy" abaixo (o nome antigo no config ainda vale por um tempo e o `--validate` avisa).

## A fila de release

- Worker de desenvolvimento voltou com `PRONTA PARA RELEASE: <branch>`: `$A --release-queue add <branch> <PID>` (o
  `<PID>` da tarefa, quando houver) e `fila in_review <PID> --note "<branch>"` (task-queue). A vaga fica livre.
- **Com revisão por outro agente** (comportamento `code-review`: a coluna do dev tem como próximo a coluna do revisor):
  a branch só entra na fila de release depois de aprovada. O dev passa a tarefa com `fila handoff <PID> --note
  "<branch>"`, sem `--release-queue add`; a tarefa volta a este agente como tarefa da coluna de release (dono dela) e
  aí `$A --release-queue add <branch> <PID>` e `fila in_review <PID> --note "<branch>"`. Ela chega como
  `-- FILA LIBERADA <PID>: ... -> passada por <revisor> para a coluna <release>; nota: revisão aprovada ...` (PLN0106):
  não é tarefa nova, nada de worker de código; a branch é a da nota que o dev deixou no handoff (o revisor a vê) ou a
  que tem o PID no nome (`git ls-remote --heads origin | grep -i <pid>`). Depois do deploy, `fila done <PID>`: a coluna de
  release não tem próximo, então conclui a tarefa. A branch leva o PID no nome
  (ex.: `feat/pln0123-filtro`), que é como o revisor e o integrador a acham.
- `$A --release-queue` mostra a fila e se o release está devido. Depois de um `add`, conferir: devido, disparar na hora,
  sem esperar o tick.
- O gancho `release_due` acorda a sessão com `== RELEASE DEVIDO` quando há `min_branches` ou mais prontas, ou uma
  esperando há mais de `max_wait_min` minutos (regras do Planou; as opções do gancho são a reserva). Nunca editar o
  arquivo da fila à mão.
- O gancho `fila_parada` (comportamento task-queue, seção "Fila parada") cobre também a coluna de release: tarefa
  parada nela, sem liberar com vaga livre, acorda a sessão com `== FILA PARADA <PID> (<motivo>)`.
- Cada `add`, `start`, `drop`, `done` e `abort` espelha a fila inteira no Planou (a pessoa vê em Release). Falhou:
  `AVISO (planou): fila de release nao espelhada`, o comando vale do mesmo jeito e o próximo tick manda de novo.

## Disparar o integrador

1. `$A --release-queue start`: marca as branches da fila como em integração e o gancho fica quieto. Um integrador por
   vez; branch que fica pronta durante a integração entra na fila e vai no próximo lote.
2. Um worker em background (delegate-to-worker) com o pedido: papel INTEGRADOR, as branches, a worktree de integração em
   `<worktrees>/release-<AAAAMMDD-HHMM>`, o bloco do `$A --brief <repo> --role batch-release`, a linha `regras (...)` do
   `$A --release-queue` e esta lista, que ele cumpre:
   - junta as branches numa branch de integração, com rebase na principal;
   - suítes completas uma vez por release, sempre sob `flock -o <test_lock> <tests.full>` (o `-o`
     não deixa um processo que fica vivo, como um servidor de compilação, herdar a trava); suíte verde não se repete;
   - e2e completo só quando `e2e_marker` não existe ou tem mais de N horas (o "e2e completo a cada" da linha de
     regras); passou, grava a data nele;
   - versão pela regra de versão da linha de regras (sem ela, a do arquivo `rules`);
     falha no e2e completo segura o release até ser explicada;
   - monta a seção da versão no changelog a partir dos fragmentos em `fragments` e apaga os fragmentos usados;
   - merge só por fast-forward, e confere que o fast-forward entrou ANTES de criar a tag; tag anotada na ponta e push
     da principal e da tag pela conta `gh_account` do repositório;
   - deploy sempre sob `flock -o <deploy_lock> <deploy_cmd>`;
   - uma linha nova em `deploy_log` por deploy: horário, commit, versão e o que mudou para quem usa, separados por tab;
   - suíte falhou: acha a branch culpada, devolve só ela (com o motivo) e solta o release com as outras;
   - devolve: versão, commit, branches que entraram, culpadas com o motivo, testes que rodaram.
   Logo depois de disparar: `$PL fila worker-start <PID da primeira branch com PID> [--task <outros PIDs>] --role
   integrator --label 'release em lote: <branches>'` (lote sem `<PID>`: `$PL worker start --role integrator --label
   '...'`), e guardar a key: o Planou mostra o integrador em andamento na aba Fila.
3. O integrador voltou:
   - antes de tudo, a entrega dele, uma vez só, na tarefa da **primeira branch do lote que tem `<PID>`** (um lote cobre
     várias tarefas; mandar em todas contaria os mesmos tokens várias vezes na média do projeto):
     `$PL fila worker <PID> --role integrator --tokens <N> --steps <ferramentas> --duration-ms <ms> --result
     feito|parcial|falhou --key <key> --phases-from <output_file do Agent>` (feito: release no ar; parcial: saiu com branch devolvida; falhou: nenhum
     deploy). Lote sem nenhum `<PID>`: não manda entrega e fecha com `$PL worker end <key> --result ...`;
   - `$A --release-queue done <branches que entraram>` (sem argumento: todas as da integração). A saída lista os
     `<PID>` que entraram: são as tarefas que o aviso de deploy fecha;
   - branch culpada: `$A --release-queue drop <branch>` e a tarefa volta para um worker na mesma branch
     (`fila started <PID>`); pronta de novo, `add` outra vez;
   - falhou sem deploy nenhum: `$A --release-queue abort` (as branches continuam na fila) e contar ao usuário em uma
     linha o que segurou;
   - o aviso do deploy é dado aqui, na volta do integrador, depois do `done` (seção abaixo); o gancho `deploy_log`
     não acorda de novo por ele.

## Aviso de deploy

Toda versão nova no ar vira aviso imediato ao usuário, com o que mudou e onde conferir. Deploy de um release desta
sessão: o aviso sai na volta do integrador, logo depois do `--release-queue done`, com o relatório dele. O gancho
`deploy_log` lê o log de deploys (opção `path` do gancho) e:
- enquanto a integração roda (`start` sem `done` nem `abort`), segura as linhas novas;
- deploy dentro da janela do último `done` (do `start` ao `done`): `== DEPLOY JA AVISADO (n)`, que não acorda a sessão
  e vai no fim do próximo tick que acordar (`== SEM ACAO`); não avisar de novo;
- qualquer outro deploy (feito à mão, rollback, release que esta sessão não fechou): acorda com `== DEPLOY NOVO (n)`, uma
  linha por deploy com a versão, o horário e o commit, e abaixo o texto do log. Esse é avisado quando chega. A volta
  automática do deploy sai como `-- volta para vX.Y.Z (rollback)`; a linha de alerta de saúde (a versão nova ficou no
  ar com a checagem falhando e a volta não trocou) sai como `-- ALERTA DE SAUDE da vX.Y.Z (ficou no ar)` e pede a decisão
  da pessoa (rollback `--restore` ou `--down`): avise com o texto do log, não como versão nova.
A primeira leitura só marca onde o log está, então deploy antigo nunca vira aviso.

O aviso é o texto final do turno. Com `"conversation": true` no bloco `planou` ele sobe sozinho para a Conversa do
Planou e aparece também aqui. Um aviso por deploy, curto, na língua de quem usa:
- `vX.Y.Z no ar (HH:MM)`;
- o que mudou para quem usa, em até 5 itens, sem nome de classe, rota, tabela ou migration (isso fica no log);
- onde conferir: `check_url` e a tela ou o menu que mudou;
- o que ficou fora do lote e por quê, quando houve branch culpada.

Sem a conversa ligada (`"conversation"` falso): o mesmo texto também em `$PL alert --title "vX.Y.Z no ar: <resumo>"
--severity low`.

Depois do aviso:
- Tarefas da fila que entraram nessa versão (os `<PID>` que o `--release-queue done` listou): `$PL fila done <PID>
  --note "no ar na vX.Y.Z"` (task-queue, passo 4).
- Deploy que não saiu desta sessão (feito na mão ou por outra sessão): avisar do mesmo jeito, dizendo que veio de fora,
  e não fechar tarefa nenhuma por ele sem conferir que a entrega dela está na versão.
- `== DEPLOY: log de deploys ilegivel`: dizer a causa em uma linha; o gancho avisa de novo só se o erro mudar.

## Limites

- Suíte completa, e2e completo, tag e deploy são só do integrador; o worker de desenvolvimento roda testes filtrados e
  nunca espera pela trava.
- `== RELEASE: integracao aberta desde ...` quer dizer que um `start` ficou sem `done` ou `abort`: conferir se o worker
  integrador ainda roda antes de fechar.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido. Quem usa são o integrador e o aviso de deploy; o worker de desenvolvimento nunca.

```permissions
tools: subagent:worker, cli:gh
actions: worktree, code, test.filtered, test.full, push, pr.merge, tag, release, deploy, planou.queue, planou.note
```
