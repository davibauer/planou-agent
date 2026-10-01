---
title: Aviso no celular
summary: Tria o que chega e avisa no celular só o que precisa da pessoa.
layer: skill
kind: always
---
# push-alert: aviso ativo e triagem leve

Vale para a instância que avisa o usuário no celular (`PushNotification`, via Remote Control) quando o tick traz algo
que exige ação dele, e que tria cada item antes de mostrar. `A="python3 $S/scripts/agent.py <instância>"`.

Opções em `behavior_config.push-alert` (`$A --status` mostra):
- `prefix`: começo de todo aviso (padrão: o nome do agent seguido de `:`, ex. `work-watch-acme:`).
- `max_chars`: tamanho máximo do aviso (padrão 200).
- `triggers`: o que dispara nesta instância, uma frase curta por item (ex.: `"PR nova não-Draft de outro para main"`).
- `quiet`: o que não dispara nesta instância, mesmo formato.
Sem `triggers`/`quiet`, as listas estão no `instructions.md`; as duas fontes valem juntas.

## O aviso

- Só quando o tick traz algo que exige o usuário, **uma notificação por tick**, no fim da triagem. Consultas nunca
  avisam. Resultado "not sent" (terminal em foco) é normal: seguir sem repetir. `Remote Control inactive`: dizer uma vez.
- Uma linha, até `max_chars`, sem markdown, começando por `prefix`. **Contar, não descrever**: um item por assunto,
  separados por ` · `, na ordem em que o usuário vai agir; idade nas pendências (`· pendente: <quem> (<canal>, 5h)`).
  O detalhe está na sessão.
- Fonte quebrada: `<prefix sem ':'> QUEBRADO (<fonte>): <causa curta e o conserto>`.
- Dispara sempre, em qualquer instância: lembrete em `== PENDENTES`; mensagem de pessoa que é demanda (DM ou menção
  sem resposta sua, resposta na sua thread); `FONTE QUEBRADA`; linhas de `push` do comportamento `recordings`.
- Não dispara nunca: nada novo; só `✓ resolvida`; só avisos (`(so aviso)`, Draft nova, `SAIRAM DA LISTA`, commits ou
  aprovação em item de outro); grupo já respondido; automáticos sem ação; `+N msgs sem mencao`; `✓ normalizou`.
- O resto (o que é demanda em cada fonte da empresa) vem de `triggers` e `quiet` ou do `instructions.md`.

## Triagem leve

Cada item que é demanda ganha, antes do aviso, a triagem que deixa a resposta do usuário em uma palavra. Até 8 linhas
por item.
- **PR ou MR pedindo sua revisão, nova ou saiu de Draft**: o que muda, CI verde?, conflito?, e os pontos propostos da
  revisão (o usuário comenta; o agent não). Sem a API: checkout do número no clone e diff contra a principal.
  `$A --github show repo#N` (ou a fonte da instância) traz descrição, checks, reviews, threads e arquivos.
- **CI quebrada em PR sua ou na principal**: qual check, a causa pelo fim do log e o conserto provável; cruzar com as
  causas conhecidas da memória e do `instructions.md`. Erro genérico de task ("exited with code 1") nunca é a resposta:
  abrir o log.
- **Comentário, review ou menção em item seu**: o texto na íntegra e, abaixo, o rascunho (`work-watch`, Triagem),
  conferindo o diff antes de afirmar o que a PR faz. Sem nomes de pessoas no que vai para a PR.
- **Issue ou card novo**: o que pede, repositório provável, estimativa, bloqueio óbvio (descrição vazia, depende de
  outro, já tem PR). Defeito que dá para reproduzir no código: além do rascunho, o card pronto e o plano do conserto em
  3 linhas (arquivo, mudança, teste).
- **Convite, reunião movida ou cancelada**: uma linha com quando, de quem e se conflita com a agenda; sem rascunho (a
  resposta é o botão do calendário). `EM BREVE`: só o link da chamada.
- **Pipeline, cluster, alerta, credencial**: uma linha por item com a causa provável e o próximo passo; sem rascunho.
  Com evidência histórica disponível (logs, métricas), a causa sai dela, não do palpite.
- **Hipótese testável de graça** (uma chamada só leitura): testar antes de apresentar; nunca "(a) ou (b), aposto em (a)".
- **Acervo antigo** (conflito, parada, crônico que já era base): só é ação se ainda vale; crônico com conserto pronto em
  outra branch se propõe uma vez, no primeiro tick do dia, não a cada tick.

A triagem só aponta: responder, comentar, aprovar, mergear ou mexer em ambiente é do usuário, salvo a autonomia do
`instructions.md`. Pedido "faz" vai para o subagent `worker` (comportamento `dev-worker`, quando ligado).

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido.

```permissions
tools: -
actions: notify
```
