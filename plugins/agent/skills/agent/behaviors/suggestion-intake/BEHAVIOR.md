---
name: suggestion-intake
description: "Transforma as sugestões dos outros agentes em tarefas do Planou. Sempre ativa na instância que a liga em \"behaviors\"."
title: Sugestões do time
summary: Transforma as sugestões dos outros agentes em tarefas do Planou.
layer: skill
kind: always
---
# suggestion-intake: sugestões dos outros agents viram tarefas do Planou

Vale para o planou (gancho `suggestions` no config). Os agents do time registram limites e defeitos do Planou e dos
plugins com `$PL sugestao` (núcleo, "Sugestões para o Planou e os plugins"); a sugestão cai na caixa
`data/suggestions.jsonl` desta instância e o gancho, a cada tick pesado, cria a tarefa no projeto Planou, no backlog,
com "Reportada por" o agent que sugeriu. A mesma sugestão (título e assunto) de outro agent ou de outro dia vira uma
linha "+1" na descrição da existente. `A="python3 $S/scripts/agent.py <instância>"`.

**Toda tarefa nova nasce sob um épico (PLN0250).** O gancho escolhe o épico pelo assunto e pelo título da sugestão:
primeiro um apelido da tabela `aliases` do config, depois as palavras em comum com o título dos épicos de `epics` (as
do assunto valem mais; palavras genéricas como planou, plugin e agente não decidem sozinhas). Sem épico que sirva, ele
cria um épico novo da área ("Plugins: <área>", "Planou: <área>") no mesmo sync, e as próximas sugestões da mesma área
entram nele. O "+1" nunca troca o épico, e a pessoa pode mudar o épico na tela.
- **A mesma regra vale para toda tarefa nova que a instância cria (PLN0275)**: o sync das fontes (a área é a fonte
  do item, só para tarefa nova do `planou.project`), o backlog do comportamento backlog-planning (plano sem `"epic"`: a área é
  a meta) e o gancho health (a área é `epic_subject`, padrão "saúde dos agentes"). A regra fica em
  `scripts/watch_core/epics.py`, e os épicos novos que ela cria ficam em `cache/planou/epics.json`, os mesmos para
  todos esses caminhos.
- **De onde vêm os épicos**: primeiro a lista de épicos abertos que o Planou dá para o projeto
  (`GET /v1/agent/projects/<projeto>/epics`, PLN0274), usada só quando o contrato /v1 do plugin já tem essa rota e
  ela responde; senão `planou.epics` do config da instância; senão `epics` deste gancho (onde ficavam antes). Os
  apelidos: `planou.epic_aliases`, senão `aliases` deste gancho. Épico novo que a pessoa criou na tela: incluir em
  `epics`; épico concluído: tirar. Apelido novo: `"<apelido>": "<título ou pid do épico>"`. Exemplo (dados fictícios):
  ```json
  {"planou": {"project": "ABC",
              "epics": [{"pid": "ABC0001", "title": "Relatórios e exportação"},
                        {"pid": "ABC0002", "title": "Agentes"}],
              "epic_aliases": {"exportar csv": "ABC0001", "saude": "ABC0002"}}}
  ```
  Com isso, a checagem de saúde quebrada abre a tarefa sob "Agentes" (o apelido "saude" casa com a área "saúde dos
  agentes"; "agentes" sozinho é palavra genérica e não decide). O mesmo `epics`/`aliases` dentro do gancho
  suggestions continua valendo quando não há `planou.epics`.
  O apelido é procurado como frase no assunto e no título da sugestão (o mais longo vence); o valor é o pid ou o
  título de um épico conhecido.
- **Sem `epics` (ausente ou vazio), nenhum épico é criado**: um épico da área duplicaria os épicos que a pessoa já tem.
  A tarefa nasce sem épico e a saída diz `-- sem epics no config: PLNxxxx ficou sem epico; ...`: dizer ao usuário e
  sugerir preencher `planou.epics` com os épicos abertos do projeto (o config da instância é da sessão). O health
  diz o mesmo na saída; o sync das fontes só anota em `cache/planou/warnings.log` (uma vez por dia e tarefa). Épico novo só nasce
  quando há `epics` e nenhum serve.
- `-- nova PLNxxxx: ... (...; epico [novo] PLNyyyy "<título>")`: dizer ao usuário em que épico entrou; com "novo",
  dizer também que o épico foi criado. `aviso do Planou: ...` abaixo da linha (épico desconhecido ou que não é
  épico): corrigir o `epics` do config.
- `sem epico (a pessoa apagou o epico da area)`: não recriar; se a área precisa de épico, perguntar à pessoa qual usar.

- `== SUGESTOES (n)`: uma linha ao usuário por tarefa nova (`-- nova PLNxxxx: ...`) ou "+1", sem pedir nada. A tarefa
  nasce no backlog. Não começar uma sugestão por conta própria; ela entra na fila como as outras.
- **Triagem e selo (PLN0440, delegação de 02/11):** a sessão do agente dono do projeto tria cada sugestão nova. Quando
  um agente faz sozinho (escopo fechado, sem decisão da pessoa, sem acesso novo, critério de pronto que se confere),
  selar: `$A --suggestions selar <PID|sug-chave> "<motivo curto>"`. Vai o `agent_can_do` {value, reason} com o título
  guardado aqui (o Planou exige; título que a pessoa mudou continua o dela) e o `base_version`, e a vaga livre do
  Planou pode puxá-la para o dono da coluna A fazer ou do projeto. Nunca selar decisão (`Decidir:`, `Decide:`,
  `Decisão:`) nem o que fala de custo, orçamento, preço, cobrança, fatura, billing, invoice, pagamento, payment,
  conta, account, segredo, secret, senha, password, token, chave, key, credencial, cliente, client, customer, produção,
  prod ou deploy: o comando recusa sem enviar nada
  (a mesma lista do backlog-planning), e essas ficam com a pessoa. `selar <PID> -` tira o selo.
  - "a pessoa mexeu na tarefa ... nada mudou": ela mudou a tarefa depois do último envio daqui. Nada fica gravado,
    nem a versão nova, então selar de novo dá o mesmo aviso: conferir com a pessoa, não insistir.
  - O selo vai uma vez: o "+1" não o reenvia e um segundo `selar` é recusado ("ja selada"), então o selo que a pessoa
    tirou no Planou fica tirado. Limite: o Planou não diz quem tirou o selo; a proteção é o `base_version`. Depois de
    um "+1" que voltou em conflito a versão daqui se atualiza, mas o "ja selada" ainda segura. Só `selar <PID> -`
    daqui libera selar de novo. Sem selo, a tarefa espera a pessoa aprovar.
- `-- limite: ...`: um agent passou de 3 sugestões no dia; a de fora fica só na caixa. Nada a fazer.
- `-- ... a pessoa apagou essa sugestao`: não recriar.
- `AVISO (sugestoes)`: o Planou não respondeu; as linhas esperam o próximo tick, nada se perde.
- `$A --suggestions`: tarefas criadas (com "(selada: ...)" nas que têm selo) e o que espera na caixa. A caixa nunca é
  editada à mão.
- Sugestão com dado de cliente que tenha passado: editar a tarefa no Planou para generalizar e avisar o usuário.

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido.

```permissions
tools: -
actions: planou.task
```
