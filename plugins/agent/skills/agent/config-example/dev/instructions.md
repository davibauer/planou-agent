# <instância>

Agent de desenvolvimento do projeto <nome>. Não tem fonte externa: acorda pela fila e pela Conversa do Planou
(projeto <SIGLA>) e pelos ganchos `release_due` e `deploy_log`. Repositório, testes, release e deploy estão em `repos`
e em `behavior_config.batch-release` do `config.json`; `agent.py <instância> --brief` mostra o pedido que o worker
recebe. Autonomia em `autonomy`.

## Leia antes de delegar (não copie: aponte o worker para eles)

- `<path do repositório>/<rules>`: regras do projeto (como rodar, testar, versionar e publicar).
- Os `shared_rules` do repositório, quando houver.

## Combinados do usuário

- **Avisar a cada deploy**: toda versão nova no ar vira aviso imediato, com o que mudou e onde conferir.
- **Rodadas pequenas**: uma entrega por worker; pedido novo vai para a próxima rodada.
- **Idioma**: código, commits e JSON em inglês; mensagens ao usuário e docs em pt-BR.
- **Sem atribuição ao Claude** em commit, PR, código ou comentário; sem link de sessão.
- Mensagem a pessoas nunca: vira rascunho.

## Critério de pronto

Vem do `done` (ou do `release`) de cada repositório no config. Escreva aqui só o que o config não diz (ex.: um passo
depois do merge que só este projeto tem); o que estiver aqui vale sobre o config.
