# Fonte whatsapp: aparelho conectado, somente leitura

A fonte `whatsapp` (`skills/agent/scripts/sources/whatsapp.py`) traz para o tick as mensagens dos grupos e contatos do
WhatsApp que a pessoa listou no config da instância. A instância entra na conta como um **aparelho conectado** (como o
WhatsApp Web): a pessoa escaneia um QR no celular uma vez e, daí em diante, cada tick conecta, recebe o que chegou desde a
última vez, grava e desconecta.

## O risco, antes de ligar

É um **cliente não oficial**: a ponte usa a biblioteca [whatsmeow](https://github.com/tulir/whatsmeow), que fala o
protocolo do WhatsApp Web sem ser do WhatsApp. O WhatsApp pode bloquear (por um tempo ou de vez) uma conta que usa
cliente não oficial. Por isso o desenho é o mais discreto possível, e mesmo assim o risco não é zero:

- **somente leitura**: não existe caminho de envio no código. Nada de mensagem, reação, edição, status, "digitando",
  confirmação de leitura ou presença. O teste `tests/test_whatsapp_source.py` procura chamadas de envio na ponte e falha
  se aparecer alguma;
- **presença sempre indisponível**: a ponte nunca chama `SendPresence`, então o aparelho nunca aparece online e as
  confirmações de entrega que o protocolo exige saem como "inativas". Este aparelho não acrescenta tique nenhum e o
  azul nunca sai daqui (os dois tiques cinza o remetente já vê de qualquer forma, porque o celular da conta confirma a
  entrega por conta própria). Confirmação de leitura nunca sai. O que a biblioteca manda sozinha por ser protocolo, e
  não dá para desligar: o ack ao servidor, a entrega inativa, a confirmação do histórico ao **próprio celular** e, quando
  uma mensagem não decifra neste aparelho, um **pedido de nova chave ao aparelho de quem mandou**. Esse pedido é o único
  envio que chega a outra pessoa: é protocolo, não aparece na tela dela, mas sai da conta. Além disso a ponte apaga do
  servidor de mídia o bloco de histórico que o celular subiu para este aparelho, depois de gravar todas as mensagens
  dele, como a biblioteca faz por padrão. Para os grupos listados pelo nome, a ponte pergunta ao servidor a lista de
  grupos da conta e, para um grupo que não está nessa lista, os dados só desse grupo: são consultas, nada chega a outra
  pessoa;
- **conexão curta**: um tick conecta, lê a fila e sai (`timeout_s`, padrão 60 s). Sem daemon, sem serviço do systemd, sem
  processo que fica no ar.

## Lista de permissão

Só os chats listados são lidos; o resto é descartado pela ponte **antes de gravar** (e conferido de novo pela fonte).
Lista vazia não lê nada e o tick avisa o que falta.

```json
"sources": [
  {"type": "whatsapp",
   "chats": ["Nome Exato do Grupo", "120363000000000000@g.us"],
   "contatos": ["Nome Exato do Contato", "+55 00 00000-0000"]}
]
```

| opção | o que é |
|---|---|
| `chats` | grupos: o id do grupo (`...@g.us`, o recomendado) ou o nome exato como aparece no celular (maiúsculas e acentos contam); um id de conversa 1:1 também vale aqui |
| `contatos` | conversas 1:1: o nome exato do contato **como está salvo na agenda do celular**, o número com DDI (espaços, `+`, parênteses e traços são ignorados) ou o id (`...@s.whatsapp.net`, `...@lid`) |
| `anexos` | baixa os anexos dos chats permitidos (padrão `true`) |
| `max_anexo_mb` | anexo maior que isso é listado e não baixado (padrão 16) |
| `timeout_s` | o máximo de uma conexão do tick (padrão 60); o que não recebeu confirmação volta no próximo |
| `historico_dias` | dias de histórico pedidos no pareamento (padrão 30) |
| `guardar_dias` | linhas do spool mais velhas que isso saem nos ticks ao vivo (padrão 90) |
| `max_por_chat` | mensagens mostradas por chat no bloco (padrão 20) |

Os nomes reais ficam só no config da instância (`~/.config/agent/<instância>/config/config.json`), nunca no repositório.
O contato da lista vale só para a conversa 1:1 com ele: uma mensagem dele num grupo fora da lista não entra. O nome do
contato é comparado só com o nome salvo na agenda, nunca com o nome que a pessoa escolhe no próprio perfil (quem não está
na agenda pode pôr qualquer nome ali); para alguém fora da agenda, use o número.

Contato que esconde o número: quem não está na agenda da conta pelo número pode aparecer só com um id `...@lid`, sem o
número junto (o servidor não manda o número e a sessão não tem o par id/número). Nesse caso nem o nome da agenda nem o
número casam, e a mensagem é descartada como fora da lista. Para ler essa conversa, ponha em `contatos` o id `...@lid`
dela. O celular não mostra esse id, e a ponte hoje só conta as mensagens descartadas, sem mostrar de quem são.

Grupo pelo nome: o nome serve para achar o grupo uma vez. Daí em diante vale o id que a ponte achou (guardado em
`secrets/whatsapp/grupos-permitidos.json`): outro grupo com o mesmo nome (criado por alguém que adicionou a conta, ou um
grupo renomeado) fica de fora e o tick avisa; o grupo achado continua valendo mesmo se mudar de nome. Se dois grupos da
conta já têm o nome quando a ponte procura, nenhum é lido e o aviso traz os ids dos dois. Um grupo que não veio na
lista de grupos da conta (entrou depois da consulta, por exemplo), enquanto algum nome da lista ainda não achou o seu
grupo, é consultado sozinho no servidor (uma vez por tick; a ponte usa só o nome que vem na resposta) e decidido na hora pelas mesmas regras: com o
nome da lista e nenhum outro grupo com esse nome, passa a valer e o id vai para `grupos-permitidos.json`; com outro
nome, fica de fora. Se a consulta falhar, as mensagens dele ficam sem decisão e voltam no próximo tick, com aviso; se o
servidor responder que a conta não está no grupo (ou que ele não existe mais), o nome não tem como ser conhecido e as
mensagens saem com um aviso que traz o id. Por isso o id é o recomendado:
o pareamento mostra o nome e o id dos grupos da conta quando algum nome da lista não casa ou é de mais de um grupo. Para a ponte procurar o nome de novo,
apague `grupos-permitidos.json`. Status,
canais e listas de transmissão nunca entram. O `--validate` recusa `chats`/`contatos` que não sejam listas de textos e
avisa quando as duas estão vazias.

## Parear (uma vez, pela pessoa)

Preencha a lista **antes**: o histórico dos chats permitidos só vem uma vez, no pareamento, e o comando recusa lista
vazia. Depois, num terminal:

```bash
python3 <pasta da skill agent>/scripts/agent.py <instância> --whatsapp-pair
```

Na primeira vez a ponte é compilada (`go build`, alguns minutos; precisa de Go e gcc). O QR aparece no terminal e também
em `cache/whatsapp/qr.png` da instância; no celular: WhatsApp, Aparelhos conectados, Conectar um aparelho. O primeiro
código vale 60 s e os seguintes 20 s, por uns 3 min. Pareado, a ponte espera o histórico (até 3 min), grava o dos chats permitidos e sai. Se algum
nome da lista não casou com a conta, o comando mostra quais e, só na tela, o nome e o id de cada grupo da conta para corrigir (o id é o recomendado na lista).

Para desfazer: remova o aparelho em Aparelhos conectados no celular e apague `secrets/whatsapp/` da instância. Se o
celular desconectar o aparelho, o tick mostra `FONTE QUEBRADA (whatsapp): sessao encerrada no celular ...`; pareie de novo.

## No tick

```
== WHATSAPP (3 msgs novas em 2 chats)
-- Fulano (1:1) · SEM RESPOSTA SUA
   [05/01 09:01] Fulano: segue o orçamento
      [guardado: ~/.config/agent/<instância>/cache/whatsapp/anexos/<chat>/2026-01-05_<id>_orcamento.pdf]
-- Obra (grupo) · ultima msg e sua
   [05/01 09:10] Ciclano: chegou o cimento
   [05/01 09:12] eu: ok
```

Cursor por chat: cada mensagem aparece uma vez. A ponte só confirma ao servidor a mensagem que já gravou (ou que
descartou por estar fora da lista). A que ficou sem decisão (grupo listado por nome num tick em que os nomes dos grupos
não vieram, ou grupo fora da lista de grupos cuja consulta falhou), a que não deu para gravar, a que encontrou um erro
interno da ponte e a que ainda estava sendo tratada no fim do prazo (anexo baixando, bloco de histórico) ficam sem
confirmação e voltam na próxima conexão. Para isso a biblioteca guarda o texto já decifrado dessas
mensagens em `secrets/whatsapp/session.db` até um tick tratá-las (sem essa cópia, a mensagem reentregue não decifraria
mais e se perderia). Toda mensagem passa por ali por um instante, inclusive a de chat fora da lista, que é decifrada
antes do filtro; tratada, a cópia sai e o sqlite sobrescreve o espaço apagado (`secure_delete`). O diário de transação
do sqlite pode guardar um resto até ser reaproveitado: fica na mesma pasta `secrets/` (0700). Antes de desconectar, o
tick espera terminar o que está em andamento, até `timeout_s` mais 20 s. Falha da ponte (sem rede, tempo esgotado, sessão encerrada) vira
`FONTE QUEBRADA (whatsapp): <causa curta>` sem derrubar as outras fontes. A fonte não abre pendência nem lembrete: o
bloco é para o agente ler e avisar.

## Arquivos da instância

| caminho | o que guarda |
|---|---|
| `secrets/whatsapp/` | a sessão do aparelho (`session.db`, 0600; pasta 0700). Nunca vai para log nem para a saída |
| `data/whatsapp/spool.jsonl` | as mensagens permitidas, uma linha JSON cada (0600), gravadas antes da confirmação ao servidor: um tick em modo teste (sem cursor) ou uma queda não perdem nada |
| `cache/whatsapp/anexos/` | anexos dos chats permitidos, uma pasta por chat (pelo id, sem nome) |
| `cache/whatsapp/bin/` | a ponte compilada, uma por versão do código dela |

Em modo teste (`live` falso) o cursor não anda: cada tick mostra de novo o que chegou desde o pareamento (até
`max_por_chat` por chat). A conexão e o spool funcionam igual, porque a fila do servidor esvazia quando a ponte confirma.

## A ponte

`scripts/sources/whatsapp_bridge/` (Go): `main.go` (os dois comandos, `pair` e `pull`), `allow.go` (a lista de permissão
e o texto de cada mensagem), `allow_test.go` e `reader_test.go` (o tratamento das mensagens sobre uma sessão de teste,
sem conectar). A whatsmeow está fixada por commit no `go.mod` e no `go.sum`, e a ponte
compila com `-mod=readonly` (só as versões do `go.sum`). Atualizar a whatsmeow: na pasta da ponte,
`go get go.mau.fi/whatsmeow@<commit> && go mod tidy`, `go vet ./... && go test ./...`, e a PR com o `go.mod` e o `go.sum`.
A lista e os limites vão pela entrada padrão em JSON, nunca na linha de comando; a saída é um JSON na saída padrão e o log
(só erros) vai para a saída de erro, que a fonte descarta.
