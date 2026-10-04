---
name: travel-agent
description: "travel-agent, agente de viagem: monitora o preço das passagens das viagens do usuário numa grade de datas no Google Flights, só avisa quando o preço cai de verdade, bate a meta ou fica abaixo da viagem de referência; alertas do Google Flights e promoções de milhas no Gmail, milhas contra dinheiro, hotéis, dólar, prazos e planilha. Atalho de `/agent travel-agent`. Use quando o usuário invocar /travel-agent, perguntar \"como está o preço da viagem?\", \"caiu a passagem?\", \"vale pagar com milhas?\", \"monitora uma viagem para X\", ou quando o runner da instância acordar a sessão."
---

# travel-agent (atalho)

`/travel-agent [...]` é `/agent travel-agent [...]`: siga `../agent/SKILL.md` com a instância `travel-agent`, que liga o
comportamento `flight-price-watch` (`../agent/behaviors/flight-price-watch/BEHAVIOR.md`). Lá, `S` é a pasta da skill `agent`: aqui,
`S=<a pasta desta skill>/../agent`, `T=$S/behaviors/flight-price-watch/scripts` e `A="python3 $S/scripts/agent.py travel-agent"`.

Modos (os de consulta respondem e saem, sem mexer no runner):

| `/travel-agent ...` | o que roda |
|---|---|
| (nada) | carregar a instância e ligar o runner, como `/agent travel-agent` |
| `setup`, `viagem nova` | `python3 $T/setup.py`, depois as perguntas do Setup (BEHAVIOR.md) |
| `relatorio [id]`, `comparar [id]`, `combinacao [id]` | `$A --report [id]` / `--compare [id]` / `--miles [id]` |
| `milhas` | `$A --points MILHAS TAXAS PRECO [--bonus PCT] [--program nome]` |
| `promocoes`, `alertas` | `$A --gmail` / `--gflights` (seções do BEHAVIOR.md) |
| `hoteis`, `prazos`, `planilha`, `painel <id>` | `$A --hotels` / `--timeline` / `--sheet` / `--dashboard <id>` |
| `comprei`, `fechar` | `$A --booked ...` e as seções Depois de comprar do BEHAVIOR.md |
| `parar` | `bash $S/scripts/runner.sh travel-agent stop` |
