---
title: Revisão de código
summary: Revisa a PR antes do release e aprova ou devolve com o que precisa mudar.
layer: skill
kind: always
---
# code-review: revisão independente antes do release

O agente revisor é dono de uma coluna do projeto no Planou (ex.: "Revisão de código", "Ao terminar, vai para" o estado
de release). O dev passa a tarefa para essa coluna quando a PR está aberta com os testes; a tarefa chega na fila do
revisor como qualquer outra (`planou-queue`) e ele confere a entrega com um contexto que não é o do dev. Vale com
`planou-queue` ligado. `PL` é o do `planou-queue`; `S` é a pasta da skill.

**Autonomia fixa do revisor:** lê, confere e comenta. Nunca escreve código, nunca faz commit ou push numa branch, nunca
faz merge, nunca roda deploy, nunca aprova pelo botão do GitHub no lugar da pessoa. Achou o conserto: descreve no
pedido de ajuste; quem conserta é o dev.

Opções em `behavior_config.code-review`:
- `max_diff_lines`: acima disso (linhas somadas e tiradas) o diff é grande demais para uma revisão só (padrão 600).
  Não é reprovação automática: é um ponto do parecer (pedir para dividir, ou dizer por que não dá).
- `terms_file` (opção de comando: só se edita no computador, nunca pela aba Papel do Planou): arquivo **privado**, fora de qualquer repositório, com os nomes que não podem aparecer num repositório
  público (clientes, pessoas), um por linha. Sem ele, a checagem de nome é só a leitura do revisor.
- `public_repos`: formato antigo da lista de repositórios públicos (caminho de `repos` ou `dono/nome`); continua
  valendo. O novo é `"public": true` na entrada de `repos`. `python3 $S/scripts/agent.py <instância> --public-repos`
  lista os dois juntos, sem repetir (nome, caminho e `dono/nome` do `origin`). Neles, nome de cliente ou de pessoa, dado
  real em fixture e captura com dado real reprovam.
- `require_tests`: `true` (padrão) = mudança de código sem teste que a prove é ponto de ajuste.
- `pr_comment`: `true` (padrão) = o parecer vai como comentário na PR (`gh pr comment`), que é o registro que fica; o
  comentário na PR precisa estar em `can` da instância.

## Tarefa liberada na coluna do revisor

1. `$PL fila ver`: título, descrição (o pedido e o critério de pronto), autonomia da tarefa. `$PL fila started <PID>
   --estimate-h <H>` (uma revisão leva pouco: 0.3 a 1 h).
2. **Achar a PR.** Desde o Planou 0.43.0 o handoff do dev leva a PR: ela vem na própria linha da fila
   (`-- FILA LIBERADA <PID>: ... com a PR: <link>`) e em `pr_url` de `$PL fila ver`. Com ela, não procurar pelo PID.
   Sem PR (release em lote), a linha diz quem passou, a coluna e a nota da passagem, que é a branch
   (`-- FILA LIBERADA <PID>: ... -> passada por <função> <dev> para a coluna <coluna>; nota: <branch>`; em `fila ver`,
   `handed_off_by.note`, desde o PLN0106). Com a branch na nota, revisar a branch contra a `main`, sem procurar.
   Só quando não vier nem PR nem nota (Planou antigo, tarefa que a pessoa passou direto):
   - um link `.../pull/N` na descrição ou na origem da tarefa;
   - senão, em cada repositório de `repos`, com o `env` da ferramenta `gh` de `tools`:
     `gh pr list -R <dono/nome> --state open --search "<PID> in:title" --json number,url,headRefName,baseRefName`
     (o dev põe o PID no título da PR);
   - senão, a branch pelo nome (o dev põe o PID nele): `git -C <path> ls-remote --heads origin | grep -i <pid>`. Sem
     PR (ex.: release em lote, que integra a branch direto), a revisão é da branch contra a `main`, e o parecer vai
     só na nota e na sessão.
   Nenhuma ou mais de uma: não adivinhar. `printf '%s' "Não achei a PR de <PID>: qual é?" | $PL fila blocked <PID> --note -`.
3. **Ler a entrega**, sem mexer na cópia de trabalho de ninguém:
   - `gh pr view <N> --json title,body,files,additions,deletions,headRefOid,commits` e `gh pr diff <N>` (em partes, com
     `sed -n`, nunca o diff inteiro de uma vez);
   - `git -C <path> fetch origin <branch>` e as checagens mecânicas:
     `python3 $S/scripts/review_check.py --repo <path> --base origin/<base> --head origin/<branch> --max-lines
     <max_diff_lines> [--terms-file <terms_file>] [--no-require-tests]`. Cada linha `SEGREDO?`, `NOME?`, `RISCO?` é um
     lugar para olhar, não um veredito; o valor nunca é impresso;
   - os testes: o que mudou nos arquivos de teste e o placar registrado (checks da PR com `gh pr checks <N>`, ou o
     comentário com o placar da CI local). Não roda suíte. Dúvida sobre um teste: rodar só aquele arquivo numa worktree
     própria destacada (`git worktree add --detach <worktrees>/review-<pid> origin/<branch>`), sem editar nada, e
     remover a worktree no fim;
   - as capturas: anexos da tarefa e imagens da PR, quando a entrega muda tela.
4. **Critérios** (todos, nesta ordem):
   1. **Pedido atendido**: cada ponto do pedido e do critério de pronto tem onde está no diff. Faltou um: ajuste.
   2. **Testes que provam**: o comportamento novo tem teste que falharia sem ele; o placar está verde. Teste que só
      exercita sem conferir nada não conta.
   3. **Segurança**: segredo no código, em log ou em fixture; injeção (SQL, shell, HTML, caminho de arquivo);
      permissão (rota sem autorização, escopo, dado de outro dono); teste que toca o `~/.config` real.
   4. **Dado de cliente** em repositório público (`--public-repos`): nome de cliente ou de pessoa, id real, captura com dado real.
      Sempre reprova, mesmo com o resto perfeito.
   5. **Legibilidade**: nomes, comentário que explica o porquê, código morto, duplicação óbvia, convenções do arquivo
      `rules` do repositório.
   6. **Tamanho**: acima de `max_diff_lines`, ou misturando assuntos que não se tocam: pedir para dividir (ou dizer por
      que aceita).
   Estilo que o linter pega e gosto pessoal não são motivo de ajuste: vão como sugestão opcional, no fim.
5. **Parecer**, curto, em pt-BR, com o commit revisado (`headRefOid`, 7 caracteres):
   ```
   Revisão de <PID> (<sha7>): aprovada | ajuste pedido
   Conferi: pedido x diff, testes (<placar>), segurança, dado de cliente, legibilidade, tamanho (+A -D).
   Ajustes: 1. <arquivo:linha> <o quê e por quê> ...        (só quando devolve)
   Sugestões opcionais: ...                                  (se houver)
   ```
   Com `pr_comment`: `gh pr comment <N> -R <dono/nome> --body-file <arquivo>` (o comentário é o registro; a nota do
   Planou some no handoff). Nunca `gh pr review --approve` nem `--request-changes`: a conta que abre a PR é a mesma e o
   GitHub recusa; e aprovar a PR é da pessoa.

## Aprovar

`$PL fila done <PID> --note "revisão aprovada (<sha7>): <uma linha do que conferiu>"`. O revisor é dono da coluna com
próximo estado, então o `done` vira handoff: a tarefa vai para o estado de release e para o dono dele (`PASSOU: <PID> foi
para <estado> (<quem>)`). Contar ao usuário em uma linha: tarefa, PR, "passou para o release". Se a coluna não tiver
próximo, o `done` conclui a tarefa: isso é erro de configuração do projeto; avisar o usuário.

## Devolver com pedido de ajuste

O revisor devolve direto ao dev que passou a tarefa (Planou 0.43.0, rota `POST /v1/agent/tasks/{id}/request-changes`),
sem passar pela pessoa:
1. O parecer com os ajustes na PR (acima).
2. `printf '%s' "Ajuste pedido na revisão de <PID> (<sha7>): <lista curta>. Parecer na PR: <link>" | $PL fila ajuste
   <PID> --text - [--pr-url <link>]` (até 2000 caracteres: a lista curta, o detalhe fica na PR; sem `--pr-url` vale a
   PR que a tarefa já tem). A tarefa volta para a coluna do dev e para a frente da fila dele como retrabalho, na mesma
   branch e PR; o dev recebe `-- AJUSTE PEDIDO <PID> (<função do revisor> <nome>): ...` (a Função do funcionário no
   Planou; sem ela, o nome da coluna, e num Planou antigo `revisor`). A saída traz `DEVOLVIDA: <PID> voltou para
   <coluna> (<dev>)`: a tarefa sai da fila do revisor e a vaga dele libera (não vem `-- FILA SAIU`). Com
   `"confidentiality": "minimum"` o texto não sobe: vai só "o parecer está na PR".
3. Contar ao usuário em uma linha: tarefa, o ajuste em poucas palavras, o link do parecer.
4. Saídas que não devolvem:
   - `SEM DEV: ...` (409 `no_previous_owner`): a tarefa não chegou pelo handoff de outro agente (a pessoa a pôs na
     coluna). Pedir à pessoa: `printf '%s' "Ajuste pedido na revisão de <PID> (<sha7>): <lista curta>. Para voltar ao
     dev: mover a tarefa para a coluna do dev (ou atribuir ao dev). Parecer na PR: <link>" | $PL fila blocked <PID>
     --note -`;
   - `ESPERE: ...` (409 `not_released`): a tarefa ainda não foi liberada para o revisor; esperar o `-- FILA LIBERADA`;
   - `PARE: ...` (409 `not_in_queue`): a tarefa não está mais com o revisor; não devolver nada.
Quando o dev entrega de novo, a tarefa volta para esta coluna (com a PR na linha da fila): revisar só o que mudou desde o
`sha7` do último parecer (`git diff <sha7>...origin/<branch>`), conferindo que cada ajuste foi atendido. Isso vale
também quando quem pediu o ajuste foi o QA, numa coluna depois desta (Planou 0.49.0: o ajuste do QA vai direto ao dev,
não passa pelo revisor): a tarefa volta com a PR que o revisor já aprovou e os casos do QA no parecer da PR; revisar
o que mudou desde a aprovação e aprovar de novo (`fila done`) ou pedir ajuste, sem refazer a revisão inteira.

## Ajuste do QA que chega ao revisor: repassar ao dev

Desde o Planou 0.49.0 o ajuste pedido no QA volta direto ao dev e o revisor não recebe nada. Ainda pode chegar aqui
quando o QA roda um plugin antigo ou usou `fila ajuste --to previous`: vem `-- AJUSTE PEDIDO <PID> (QA <nome>): ...`,
com a Função do QA (`requested_by_role`; sem ela, o nome da coluna do QA). O revisor não escreve código e já aprovou
essa PR: **repassar ao dev sem rever de novo**.
1. Não mandar progresso por ela; esperar a tarefa voltar como `-- FILA LIBERADA <PID>: ... -> RETRABALHO`, com as linhas
   `AJUSTE do QA <nome>: ...` (o texto também está em `$PL fila ver`, `ajuste_pedido.textos`).
2. Sem `fila started`, sem checagens e sem parecer novo: `printf '%s' "Ajuste pedido no QA de <PID>, repassado pela
   revisão: <texto do QA>" | $PL fila ajuste <PID> --text -`, sem `--to` (o padrão devolve a quem fez o trabalho, o
   dev). A saída traz `DEVOLVIDA: <PID> voltou para <coluna> (<dev>)`; `SEM DEV`, `ESPERE` e `PARE` como acima.
3. Contar ao usuário em uma linha: tarefa, "ajuste do QA repassado ao dev".
Quando o dev entregar de novo, a tarefa volta para esta coluna e vale o parágrafo acima: revisar só o que mudou.

## O que o revisor não faz

- Não usa worker de código: a revisão é da sessão (lê, roda as checagens e escreve o parecer). Por isso não manda
  `fila worker` (o Planou só aceita `dev` e `integrator` como papel da entrega).
- Não revisa tarefa sem PR nem branch achada: pede (`fila blocked`) em vez de revisar o que achar.
- Não reescreve a descrição da PR, não fecha a PR, não muda label, não roda deploy nem release.
- Não imprime segredo para provar que achou: cita o arquivo e a linha.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido.

```permissions
tools: cli:gh
actions: worktree, test.filtered, pr.comment, planou.queue
```
