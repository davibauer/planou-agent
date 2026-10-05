---
name: flight-price-watch
description: "Monitora o preço das passagens numa grade de datas e avisa quando cai de verdade. Sempre ativa na instância que a liga em \"behaviors\"."
title: Agente de viagem
summary: Monitora o preço das passagens numa grade de datas e avisa quando cai de verdade.
layer: skill
kind: always
---
# flight-price-watch: agente de viagem

O agent `travel-agent` na forma de instância do plugin agent (E5 do plugin único): monitora o preço das passagens das
viagens do usuário numa grade de datas no Google Flights, guarda o histórico, separa as tarifas por número de conexões e
só avisa quando o preço cai de verdade, bate a meta ou fica abaixo da viagem de referência; lê no Gmail os alertas do
Google Flights, promoções e bônus de milhas; compara cada gasto com a última viagem ao mesmo destino; hotéis, dólar,
prazos e planilha. Veio do SKILL.md do plugin travel-agent; o motor é o mesmo, com os scripts em
`behaviors/flight-price-watch/scripts/`.

**Caminhos:** `S` é a pasta da skill `agent` (SKILL.md do agent). `T=$S/behaviors/flight-price-watch/scripts` (os scripts do
travel-agent: `agent.py`, `flights.py`, `gflights.py`, `miles.py`, `compare.py`, `sheets.py`...) e
`A="python3 $S/scripts/agent.py travel-agent"`: o tick e todos os comandos abaixo passam por ele, que roda o `agent.py`
do travel-agent num processo próprio com o Python do venv da instância (`cache/venv`, onde está o `fast-flights`).
Todo `agent.py --x` abaixo é `$A --x`. Argumento que o travel-agent não conhece é recusado (o motor dele faria um tick
inteiro). A pasta da instância é `~/.config/agent/travel-agent/` depois do `agent.py travel-agent --migrate`; o caminho
antigo `~/.config/travel-agent/` vira um link para ela, então todo `~/.config/travel-agent/...` abaixo continua valendo.
Pastas (caminhos em `$T/paths.py`): `config/` (`config.json` com as viagens, os pontos e as regras de alerta;
`instructions.md`; `past_trips.json`), `data/` (`state.json` com as marcas de alerta e o cursor do Gmail;
`prices.jsonl` com cada observação), `secrets/` e `cache/` (venv, runner). Backup = `config/` + `data/`. O
`config.json` é o mesmo arquivo para o travel-agent e para o plugin agent: as chaves do agent (`behaviors`, `session`,
`live`, `interval_s`, `planou`) ficam ao lado das viagens.

**Idioma:** falar com o usuário no idioma dele (padrão português).

**Instruções pessoais:** se existir `~/.config/travel-agent/config/instructions.md`, ler antes do primeiro tick da sessão. Prevalecem sobre este arquivo. Regra de comportamento só do usuário vai para lá (mostrar o trecho e gravar com o OK), nunca para este arquivo.

**Valores sempre somados para todos os viajantes da viagem** (ex.: "R$ 17.778 para 3"), em toda resposta, grade e comparação; preço por pessoa só entre parênteses.

**Sempre buscar a melhor combinação para o usuário**, não só a passagem mais barata: datas, conexões aceitas, dinheiro ou milhas (e qual programa), quantos viajantes em milhas e quantos em dinheiro, e se vale esperar um bônus de transferência. Ao recomendar, dizer a combinação escolhida, o total, quanto sai do bolso e o que ela ganha da segunda melhor.

**O agente nunca compra nada** e nunca faz login em companhia aérea, programa de milhas ou banco. Ele pesquisa, compara e avisa; o usuário compra.

## Fonte: Google Flights

- `$T/flights.py` busca a página pública (sem login) com o `fast-flights` do venv e lê o JSON `ds:1` com um parser próprio, que junta os "melhores voos" (índice 2) e os "outros voos" (índice 3). O `fast-flights` sozinho só lê o índice 3 e perde tarifas boas.
- **Preço por adulto:** o Google não devolve na página uma busca com criança ou bebê, então o agente busca 1 adulto e multiplica por `adults + children x child_factor` (padrão 1.0, conservador: a tarifa infantil costuma ser um pouco menor). Dizer isso ao mostrar o total.
- Preço é a tarifa mais barata do Google, em geral **sem mala despachada**. Antes de recomendar comprar, abrir o link e conferir bagagem e tarifa da criança.
- Cada célula da grade (ida x volta) guarda duas tarifas: a melhor com até `max_stops` conexões e a melhor com mais conexões.

## Fonte: alertas de preço do Google Flights no Gmail

Se o usuário acompanha rotas no Google Flights ("Acompanhar preços"), chegam e-mails de `noreply-travel@google.com` a cada mudança. Eles trazem o que a busca do agente não traz: **o preço real para o grupo inteiro, com crianças**, e cada queda rápida (tarifa promocional que abre e fecha em 1 ou 2 dias). `$T/gflights.py` lê o e-mail; as datas e aeroportos saem do link (`tfs`), com o ano.
- **Passo do Gmail** (junto do passo de promoções, em todo tick com conteúdo; o runner não lê o Gmail, então o tick imprime `== GMAIL: ...` quando a última leitura passou de `gmail.check_hours`, padrão 12 h, e isso acorda a sessão só para este passo): `$A --gflights` imprime cursor e query; para cada e-mail, `get_message` em texto puro, gravar num arquivo e `$A --gflights body < arquivo`; no fim `$A --gflights mark <epoch>`. A saída `== ALERTAS DO GOOGLE` só traz rotas da janela de uma viagem em watching, com o mesmo grupo (adultos e crianças): quedas, `NOVO MÍNIMO` do par, `META` e `ABAIXO DA REFERÊNCIA`. **Push** para META, ABAIXO DA REFERÊNCIA e NOVO MÍNIMO: essas quedas costumam durar pouco, então dizer que é para decidir no mesmo dia.
- **Histórico antigo** (setup ou quando o usuário mencionar os alertas): buscar todos os e-mails `from:noreply-travel@google.com <destino>`, montar um CSV (`email_date, depart_date, return_date, passengers, price_now_brl, price_was_brl, trend, airline, stops`) e `$A --gflights import <csv> <ORIGEM> <DESTINO>`. Com muitos e-mails, delegar a extração a um subagente.
- O `--report` mostra, por par de datas da janela, o último preço real e o menor já visto. Esse é o número a usar na recomendação quando existir: o total do agente (adulto x viajantes) é uma estimativa conservadora.
- Sugerir ao usuário acompanhar no Google Flights os pares de datas que estão sendo considerados (o agente não cria esses alertas).

## Hotéis

- **Expedia** (conector MCP `search_hotels`, se conectado): mesmo estoque do Hoteis.com (grupo Expedia), preço total com taxas em USD, inclusive hotéis de parque temático que outras fontes não vendem. Só respondeu com chamada simples: `destination` = nome do hotel, `search_radius: 1`, datas, `adult_count`, `children_age_list`, `user_locale: en-US` e `client_device_info`; com `user_location`, `query_text` ou locale pt-BR deu "Unknown error". Hotel que some da lista nem sempre está esgotado (já sumiu de uma fonte estando à venda em outra): confirmar em outra fonte antes de dizer que esgotou.
- **Booking.com** (conector MCP da sessão, se autenticado): preço real para as datas e o grupo (passar a idade das crianças). Não vende todos os hotéis de parque temático.
- **Hotéis de parque temático** fora das fontes acima: tabela oficial por temporada (sites de fãs costumam publicar) + o imposto local sobre hospedagem; dizer que é estimativa e o câmbio usado.
- **Hoteis.com / expedia.com pelo site** bloqueiam acesso automático (página "Você é ou não um robô?", HTTP 429): não insistir. Montar o link de busca com as datas e pedir o preço ao usuário, ou, se ele autorizar, abrir no Chrome dele pelo Claude in Chrome (só leitura, sem reservar).
- Todo preço de hotel encontrado vira `--quote` (por noite, com `--nights`) para a comparação com a viagem anterior.

## Setup (`/travel-agent setup`, uma vez)

1. `python3 $T/setup.py` cria as pastas, copia os modelos sem sobrescrever e cria o venv com o `fast-flights`. `--status` mostra o que falta.
2. **Viagem nova** (também em "monitora uma viagem para X"), uma pergunta por vez: de onde sai (aeroportos), para onde, janela de ida e de volta (aceita faixa `AAAA-MM-DD..AAAA-MM-DD`; limitar a grade a ~30 células, cada célula é uma busca), quem vai (adultos, crianças e idade), quantas conexões aceita, se aceita mais conexões quando for muito mais barato (quanto, em %), data limite para estar em casa (`home_by`: tira da grade as voltas depois dela; a busca de ida e volta não mostra o horário da volta, então antes de recomendar conferir a volta com `flights.search` só de ida e ver se chega a tempo), meta de preço total e uma viagem de referência (quanto pagou da última vez; o Gmail ajuda a achar: confirmação da companhia aérea). `keywords` = nomes do destino para achar promoção no Gmail. Mostrar o trecho do `config.json` e gravar com o OK.
3. **Pontos** (opcional): saldo de pontos, quanto valem em dinheiro (cashback) e para quais programas transferem e em que proporção → `points` no config (`award_taxes_estimate` = taxas por pessoa num resgate ida e volta, para o empate; padrão 700).
4. Rodar um tick de teste (`$A --dry`) e, com o OK, `"live": true` no config e armar o runner (SKILL.md do agent). Explicar em 2 linhas: roda a cada 6 h enquanto a sessão estiver aberta; `/travel-agent parar` desliga.

## O tick (runner do plugin agent)

O tick roda pelo runner do plugin agent (`bash $S/scripts/runner.sh travel-agent`, SKILL.md do agent: armar, relançar,
parar, `== RELANCAR`, `== RUNNER CICLO`, `== SEM ACAO`), que chama `$A travel-agent`, e este o `$A` do
travel-agent. O intervalo padrão é 6 h (`interval_s: 21600`, que o `--migrate` grava) e nunca menos de 3 h
(`interval_s` abaixo de 10800 vira 10800: cada tick é uma grade inteira de buscas). Tick vazio não acorda a sessão. Em
modo teste (sem `"live": true`) o tick é sempre `--dry`. Não existe mais `runner.sh` do plugin travel-agent nem
`ScheduleWakeup` como rede de segurança: textos antigos de "armar" estão obsoletos. As consultas não mexem no runner.

**Planou** (opcional, `planou` no config.json e a chave em `secrets/planou.env`): o runner faz a volta leve (fila,
conversa, sinal de vida) e, em volta de cada tick, o `$A` do plugin agent aplica os eventos, manda as ferramentas
do config, a aba Papel (só leitura: edição vinda de lá ainda não é aplicada nesta instância) e o sinal de vida `tick`,
com `AGENTE QUEBRADO (<fonte>)` como fonte quebrada.

O que fazer com a saída:
0. `AGENTE QUEBRADO (setup)` → rodar `setup.py`. `AGENTE QUEBRADO (google-flights)` → bloqueio ou o Google mudou a página: salvar o `ds:1` novo em `tests/travel_agent/fixtures/google_flights_ds1.json` (na raiz do plugin agent) e ajustar o `parse_payload` até os testes passarem.
1. `== VOOS <id>` seguido de:
   - `BASE:` primeira leitura da viagem. Mostrar o menor preço e rodar `$A --report <id>` para mostrar a grade inteira; apontar o padrão (dia da semana mais barato, datas que não valem).
   - `QUEDA:` preço total caiu pelo menos `min_drop_pct` desde o último aviso. Dizer quanto caiu, comparar com a referência e a meta, e dar uma opinião curta: compra agora ou espera (quanto falta para a viagem, se o preço já está entre os menores vistos no `--report`).
   - `SUBIU:` o menor preço ficou `rise_warn_pct` acima do último aviso (uma vez só). Se a viagem está perto, é sinal para decidir.
   - `META` / `ABAIXO DA REFERÊNCIA`: o total cruzou o limite; é o aviso mais importante. Push.
   - `MAIS CONEXÕES COMPENSA`: tarifa com mais conexões que o aceito, mas pelo menos `extra_stops_min_saving_pct` abaixo da referência. Mostrar as conexões e o tempo total antes de sugerir.
2. **Push** (`PushNotification`, uma linha começando por `travel-agent:`) só para META, ABAIXO DA REFERÊNCIA, QUEDA de 10% ou mais, MAIS CONEXÕES COMPENSA e agente quebrado.
3. **Promoções no Gmail** (se a sessão tiver o conector Gmail), no fim de todo tick com conteúdo e quando o usuário pedir `promocoes`: `$A --gmail` imprime o cursor, a query, os destinos e os programas. Buscar com a query, ler as threads em texto puro e procurar (a) promoção de passagem para um dos destinos ou para o aeroporto de origem, (b) bônus de transferência de pontos para um dos programas (a origem pode ser o banco do usuário, Livelo, Esfera). Relatar só o que casa, uma linha cada, com o link do e-mail; bônus de transferência vira `--bonus` (e resgate com preço em milhas para o destino vira `--award`), e a sugestão sai do `--miles`. No fim, `$A --gmail mark <epoch>`. Não marcar como lido, não arquivar.

## Passos do dia (conectores do modelo)

O runner não tem os conectores (Expedia, Booking, Gmail, Calendar, Artifact, Chrome). Quando algo depende deles, o tick imprime `== PASSOS: ...` (1x por dia cada, `steps_hours`) e isso acorda a sessão. Fazer cada passo e marcar com `$A --done <passo>`:
- **expedia** (`expedia_flights: true` no config): preço real para o grupo inteiro, com crianças. Para o par do `plan` da viagem (e os outros pares que o usuário está considerando), `search_flights` do Expedia com `origin`, `destination`, datas, `adult_count`, `children_age_list` (= `children_ages` da viagem), `number_of_stops` = `max_stops`, `filter_nearby_airport: true`, `limit: 5`, `user_locale: en-US`, `client_device_info`. Pegar a opção mais barata e registrar: `$A --real <viagem> <ida> <volta> <total> <moeda> --source expedia --flight "<cia, horários, conexões>" --bags "<regra da 1ª mala>"`. A saída avisa queda, novo mínimo, meta e referência como os alertas do Google. **Tarifa básica costuma vir sem mala despachada**: sempre dizer a regra da mala e somar ao comparar com a referência.
- **hoteis** (viagem com `hotels`): `$A --hotels` lista hotel, datas e grupo; cotar cada um no Expedia (`search_hotels`, regras da seção Hotéis) e no Booking quando vende, e registrar `$A --hotel-price <viagem> <hotel> <total> <moeda> --source <fonte>`. `HOTEL CAIU` = reserva feita com cancelamento grátis ficou mais cara que o preço de agora: avisar com push, dizer a economia e o prazo; o usuário reserva de novo e cancela a antiga (o agente não reserva).
- **calendario** (`calendar: true`): `$A --timeline calendar` lista os prazos ainda fora do calendário; criar um evento de dia inteiro para cada (Google Calendar do usuário, título = o prazo, descrição com a viagem e o que fazer) e gravar `$A --timeline synced <viagem> <id> <event_id>`.
- **painel** (`dashboard` no config, alternativa à planilha): `$A --dashboard <viagem>` escreve uma página; publicar com a ferramenta Artifact (primeira vez com `icon`; depois a mesma URL, guardada em `dashboard.urls.<viagem>`). Com a planilha configurada, preferir a planilha.
- `== MILHAS: bônus de X%...`: bônus de transferência de `miles_search_bonus`% ou mais (padrão 50) num programa do config. Pesquisar o resgate nas datas do plano no site do programa, pelo Chrome do usuário (só leitura, sem login, sem emitir), registrar com `--award` e marcar `--done miles:<programa>`. Sem bônus alto, não pesquisar: o empate raramente fecha.

## Cards no Planou (um por plano)

Com `"plan_cards": {"detail": true}` no config (e `planou` e `"live": true`), cada viagem do config vira um card no
projeto do Planou da instância, para acompanhar os planos sem abrir o terminal ou a planilha. Uma viagem com várias
opções de datas são várias viagens no config (como na aba Resumo da planilha): 4 planos, 4 cards.
- **Título**: nome, datas do plano, `(escolhido)` no plano que o usuário escolheu, voo e voo+hotéis para o grupo
  (`Plano A 10→24/11 (escolhido) · voo R$ 12,4 mil p/3 · voo+hotéis ~R$ 21,9 mil`).
- **Descrição**: roteiro (`itinerary`), voo (compra; senão o último preço real do par do `plan` para o grupo, do
  Expedia, alerta do Google ou `--real`; senão a grade do Google vezes os viajantes, marcada como estimativa), hotéis
  (reserva ou último `--hotel-price`), voo+hotéis, meta e referência.
- **Quando**: em todo tick ao vivo (inclusive com fonte quebrada) e logo depois de `$A --done expedia` e
  `$A --done hoteis`, para os preços novos do dia subirem na hora. `$A --plan-cards` sincroniza na hora;
  `--plan-cards --dry` só mostra o que subiria (JSON).
- **Estado**: viagem em `watching` = card aberto (sem motivo de pronto: fica como sugestão, nunca vira trabalho da
  fila). Voo comprado (`--booked ... voo`, ou `status: booked`/`done`) fecha o card como feito; `status: paused` (ou
  outro) fecha sem ação; viagem tirada do config fecha sem ação. Comprou um plano: pausar os outros (`status: paused`).
- **Por viagem**, opcional, `card`: `{"name": "Plano A", "chosen": true, "code": "plano-a"}`. `name` é o nome curto do
  título (padrão: o `name` da viagem); `chosen` dá prioridade alta (os outros ficam com a normal); `code` é a chave do
  card (padrão `plano-<id>`), para adotar um card que já existe no Planou com essa chave em vez de criar outro.
- **Confidencialidade**: o texto é só a viagem do próprio usuário. Com `"detail": true` ele sobe inteiro mesmo com a
  instância em `title`; com `"plan_cards": true` vale a confidencialidade da instância (em `title` o card fica só com o
  título); `minimum` sempre vence. Edição feita à mão na descrição pelo Planou não é sobrescrita.

## Planilha no Google Sheets

O jeito principal de acompanhar a viagem: uma planilha do usuário que o próprio agente reescreve a cada `sheet.every_hours` (padrão 6 h), sem depender do modelo. Abas do agente: **Resumo** (planos lado a lado), **Plano: <viagem>** (roteiro e custos contra a viagem anterior), **Voo** (preço real por dia de cada plano, com meta e referência, e um gráfico), **Voo (leituras)**, **Hotéis**, **Dólar** e **Prazos**. Abas com outro nome são do usuário e o agente nunca mexe nelas: sugerir que anotações dele fiquem numa aba própria.
- **Setup** (uma vez, com o usuário): com o `gcloud` numa configuração separada da conta pessoal dele (`gcloud config configurations create pessoal --no-activate` e login com `--configuration pessoal`; se o terminal não aceita digitar, deixar o `gcloud auth login --no-launch-browser` lendo de um FIFO mantido aberto e passar o código que o usuário colar), criar um projeto, `gcloud services enable sheets.googleapis.com`, criar a conta de serviço e a chave em `~/.config/travel-agent/secrets/google-service-account.json` (0600). Criar a planilha no Drive do usuário (conector do Google Drive, `create_file` com `application/vnd.google-apps.spreadsheet`), compartilhar com o e-mail da conta de serviço como `writer` (`share_file`) e gravar `sheet.id` no config. Nunca usar conta ou projeto de trabalho do usuário.
- `$A --sheet` reescreve na hora (depois de mudar o plano, registrar compra etc.).

## Dólar

Com `fx` no config (`currency`, `home`, `budget` = gastos previstos na moeda, `target` opcional), o tick busca a cotação 1x por dia (api.frankfurter.dev, grátis) e avisa `DÓLAR:` quando ela é a menor dos últimos 30 dias ou bate a meta. Sugerir comprar parte do orçamento nesses dias (conta em dólar, como Wise), sem prometer que não cai mais.

## Prazos

`deadlines` na viagem (data, título, notas) + o que vem das reservas (`--booked ... --cancel-by`) + check-in online do voo comprado. O tick lembra 14, 3 e 1 dia antes e no dia (`remind_days`), uma vez por marco: `PRAZO em N dias: ...`. Push para os de 1 dia e do dia. Na viagem nova, propor os prazos do destino (reservas de restaurante, fila rápida paga, compra de ingressos, documentos e passaporte de cada viajante, fim do cancelamento grátis) e gravar com o OK.

## Depois de comprar

- Toda compra vira `$A --booked <viagem> <categoria> <valor> <moeda> --label "..." --ref <localizador> [--cancel-by D] [--sender <e-mail da empresa>] [--hotel <id>] [--out D --ret D]`. Voo comprado para a busca de tarifas da viagem (o resto segue); hotel comprado passa a ser monitorado contra o preço pago; a cotação vira "reservado" na comparação e no painel.
- No passo do Gmail, ler também os e-mails dos `sender` das reservas: mudança de horário, cancelamento, check-in aberto. Mudança de voo é aviso com push.
- **Fechar a viagem** (depois da volta, ou quando o usuário pedir): somar o que foi gasto de fato por categoria (Gmail, extratos ou um sistema financeiro que o usuário tenha), gravar em `past_trips.json` como referência da próxima e mudar a viagem para `status: done`.

## Viagem anterior como referência

Se o usuário já foi ao destino, **toda cotação é comparada com o que ele pagou da outra vez**, por categoria: voo, hotel, parques, restaurantes, traslado, carro, carrinho, fotos, seguro etc. As viagens passadas ficam em `config/past_trips.json` (`destinations` em código IATA e `keywords`, datas, `nights`, `seats` e `items` com `category`, `amount`, `currency` e a unidade: `nights`, `days` ou `people`). `amount: null` = foi pago mas o valor não é conhecido.
- **Montar a viagem passada** (no setup, em "viagem nova" para um destino já visitado, ou quando o usuário pedir): procurar no Gmail as confirmações daquela época (companhia aérea, hotel ou agência, ingressos, traslado, aluguel de carro ou carrinho, restaurantes, seguro), extrair valor, moeda e unidade, mostrar a lista e gravar com o OK. O que o Gmail não trouxer (gastos no cartão, restaurantes pagos na hora) perguntar ao usuário ou deixar `null`.
- **Cotação da viagem nova**: todo preço encontrado para hotel, parques, restaurantes etc. (pesquisa, promoção no Gmail, o usuário) vira `$A --quote <viagem> <categoria> <valor> <moeda> [--nights N] [--days N] [--label "..."]`. O voo entra sozinho, pela melhor tarifa da última leitura.
- `$A --compare [id]` mostra cada categoria antes e agora, **na mesma moeda e na mesma unidade** (hotel por noite, parques por dia, voo no total da família quando o grupo tem o mesmo tamanho), com a diferença em %. Compara na moeda original para o câmbio não esconder aumento de preço; se precisar do total em reais, dizer o câmbio usado.
- Ao responder sobre qualquer preço de um destino já visitado, **dizer sempre quanto está pagando a mais ou a menos que da outra vez**. O tick já traz a linha `VS <viagem>:` junto de todo aviso de voo.

## Estratégia de milhas

Os pontos valem o que dariam em dinheiro (cashback): `points.cash_value / points.balance` por ponto. Um bônus de transferência de b% faz cada ponto virar `ratio x (1 + b)` milhas, então **o mesmo saldo compra mais milhas e o empate sobe na mesma proporção** (com 100%, dobra). Por isso bônus é estratégia:
- **Registrar todo bônus** achado no Gmail ou dito pelo usuário: `$A --bonus <programa> <pct> --until AAAA-MM-DD --source "<de onde>"` (só para os programas do config; bônus de outra origem, como Livelo ou Esfera, só se o usuário tiver pontos lá). Bônus vencido deixa de contar sozinho.
- **Registrar todo preço de resgate** que aparecer (promoção de milhas no Gmail, pesquisa do usuário no site do programa): `$A --award <viagem> <programa> <milhas por pessoa ida e volta> <taxas por pessoa> --note "<datas, fonte>"`. O agente não loga em programa de milhas; esses números vêm do usuário ou de promoção pública.
- O bloco `MILHAS:` (vem junto de todo aviso de voo) traz o valor dos pontos, os bônus ativos e o empate por programa: acima dele, dinheiro ganha. Com resgates registrados vem `MELHOR COMBINAÇÃO:` (k pessoas em milhas + o resto em dinheiro, respeitando o saldo), já somada para todos.
- **Quando recomendar transferir:** só com um resgate concreto que fique abaixo do empate *com* o bônus em vigor. Transferência é irreversível: nunca sugerir transferir "para ter milhas". Se não há bônus e o resgate está perto do empate, sugerir esperar: bônus de 80% a 100% para Smiles, LATAM Pass e Azul aparecem com frequência. Avisar quando um modo automático do banco transfere o saldo inteiro de uma vez (perde-se a opção do cashback).
- **Push** para bônus novo de 50% ou mais num programa do config e para MELHOR COMBINAÇÃO com milhas que ganhe de tudo em dinheiro.

## Consultas (não mexem no runner)

- `relatorio [id]`: `$A --report [id]`: grade da última leitura (total de todos os viajantes, em mil; `*` = com mais conexões sai mais barato), preço agora, menor já visto e referência.
- `comparar [id]` / "quanto estou gastando a mais?": `$A --compare [id]`.
- `combinacao [id]` / "qual a melhor opção?": `$A --miles [id]`: empate por programa com os bônus ativos e as combinações dinheiro/milhas, somadas para todos. Juntar com o `--report` (datas) numa recomendação só.
- `milhas`: `$A --points MILHAS TAXAS PRECO_EM_DINHEIRO [--bonus PCT] [--program nome]` diz se o resgate compensa contra pagar em dinheiro, avaliando os pontos pelo que valem em cashback. As milhas e taxas do resgate o usuário traz do site do programa (o agente não loga lá).
- `viagem nova`, `pausar <id>` (`status: paused`), `comprei <id>` (`status: booked`, guardar o que foi comprado e por quanto no `reference` da próxima viagem parecida).
- `cards`: `$A --plan-cards [--dry]` (seção Cards no Planou).
- `parar`: `bash $S/scripts/runner.sh travel-agent stop`.

```permissions
actions: notify
```
