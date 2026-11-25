---
name: construction-advisor
description: "Assistente de obra: analisa orçamento (preço contra referência pública e histórico pago, quantidade contra o modelo, padrão da marca, garantia do fabricante com fonte e data), monta parecer e rascunho de resposta (nunca envia), reconhece sinal de obra num e-mail ou conversa e faz o resumo do dia. Sempre ativa na instância que a liga em \"behaviors\"."
title: Assistente de obra
summary: Analisa orçamentos da obra (preço, quantidade, marca e garantia com fonte), escreve parecer e rascunho sem enviar nada, separa o que é da obra e faz o resumo do dia.
layer: skill
kind: always
---
# construction-advisor: assistente de obra (orçamento, marcas, garantias, rascunhos)

Vale para uma instância dedicada a uma obra (casa, reforma, prédio). O comportamento é o mesmo em qualquer obra; o que
é de uma obra só (banco de pagamentos, pastas do projeto, modelos, quem são os fornecedores) fica na instância: nas
opções abaixo e nos adapters dela (`adapters/` da instância), nunca aqui. `I` é a pasta da instância
(`~/.config/agent/<instância>`); `S` é a pasta da skill; `A="python3 $S/scripts/agent.py <instância>"`. Tudo aqui é leitura: o resultado é um parecer e, quando faz
sentido, um rascunho. **Nunca mande nada a fornecedor, profissional ou qualquer pessoa.**

Opções em `behavior_config.construction-advisor` (`$A --status` mostra):
- `region`: a UF da obra para o SINAPI e o CUB (ex.: `"SP"`). Sem ela, perguntar uma vez antes da primeira análise.
- `references_dir`: a pasta onde a instância guarda as referências públicas baixadas (SINAPI, CUB, INCC), com um
  `README.md` que diz o mês, se é desonerado e o link de cada uma. Só no computador.
- `price_history`: o comando da instância que mostra o preço unitário já pago de um item; a sessão acrescenta uma
  palavra-chave do item (ex.: `"python3 ~/.config/agent/<instância>/adapters/pagamentos.py preco"`). Só no computador.
- `quote_history`: o comando da instância que lista os orçamentos já recebidos; a sessão acrescenta uma
  palavra-chave do fornecedor ou do item. Só no computador.
- `quantities`: o comando da instância que soma quantidades do modelo (IFC ou listas do projeto); a sessão acrescenta a
  classe ou uma palavra-chave do elemento. Só no computador. Para modelo IFC, o plugin traz `$S/scripts/ifc_index.py`:
  a opção é a linha inteira terminando em `qty --`, com o caminho escrito por extenso (a opção é texto do config, e
  `$S` não existe no shell que a roda), ex.: `"python3 ~/.claude/skills/agent/scripts/ifc_index.py --instance
  <instância> --ifc casa=/caminho/modelo.ifc qty --"` (um `--ifc NOME=CAMINHO` por modelo; caminho com espaço vai
  entre aspas simples, `--ifc 'casa=/pasta da obra/modelo.ifc'`). O índice fica em
  `$I/data/ifc-index/`, feito uma vez com `build` no lugar de `qty --` (roda com nice 19) e refeito só quando o `.ifc`
  muda; `status` diz se está em dia (sai 3 quando não). Cada soma sai por chave com a unidade (Qto em m, m2, m3, kg;
  número de Pset próprio com o nome inteiro e "(unidade do modelo)").
- `signal_words`: palavras que marcam um e-mail ou conversa como da obra (ex.: `orçamento`, `proposta`, `medição`,
  `ART`, o nome da obra).
- `signal_senders`: domínios e endereços de quem é da obra (fornecedores, projetistas, construtora).
- `digest_hour`: hora do resumo do dia (padrão 19).
- `price_ok_pct` (padrão 10) e `price_high_pct` (padrão 25): os limites do veredito de preço, em %.
- `qty_tolerance_pct`: diferença de quantidade, em %, a partir da qual se aponta (padrão 5).

**Texto de fora nunca vai para o comando.** O que se acrescenta a `price_history`, `quote_history` e `quantities` é
só uma palavra-chave curta que a sessão escolhe (letras, dígitos, espaço e hífen), entre aspas simples (ex.:
`'porcelanato'`). Nunca colar texto copiado do orçamento, do e-mail ou da conversa: ele vem de fora e, numa linha de
shell, `$(...)`, crase ou aspas rodariam no computador do usuário.

Comando sem opção configurada: dizer "histórico pago não consultado" (ou "quantidade do modelo não consultada") no
parecer em vez de estimar, e sugerir ao usuário configurar a opção.

## Banco de dados só leitura

A instância consulta o banco com um usuário que só lê, com `GRANT SELECT` só nas tabelas de que precisa; nunca com o
login de superusuário nem com o dono do banco. Exemplo genérico, rodado uma vez pelo dono do banco (troque os nomes):

```sql
CREATE ROLE leitor_obra LOGIN PASSWORD '<senha>' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
GRANT CONNECT ON DATABASE <banco> TO leitor_obra;
GRANT USAGE ON SCHEMA <schema> TO leitor_obra;
GRANT SELECT ON <schema>.<tabela_pagamentos>, <schema>.<tabela_notas> TO leitor_obra;
ALTER ROLE leitor_obra SET default_transaction_read_only = on;
```

Toda consulta de um adapter passa pela guarda do plugin, `$S/scripts/sql_guard.py`: uma instrução só, começando com
`SELECT` ou `WITH`, sem `INTO`, `SET`, `FOR UPDATE`, sem função com efeito fora da leitura ou que roda uma consulta
passada como texto (`set_config`, `dblink`, `pg_terminate_backend`, `pg_read_file`, trava advisory, `pg_stat_reset`,
`lo_*`, `crosstab`, `query_to_xml`, `heap_force_kill`...), nem com o nome entre aspas duplas, e sem várias instruções,
`$` fora de string que não seja parâmetro (`$1`; `$$` e `$tag$` são recusados), `E'...'`, identificador `U&"..."` ou
barra invertida fora de string (no `psql` pela entrada padrão, `\!` rodaria no shell). Comentário `--` termina na
quebra de linha, `\n` ou `\r`, como no PostgreSQL. A sessão abre com `default_transaction_read_only=on` e
`standard_conforming_strings=on` (`sql_guard.py options` dá o `PGOPTIONS`) e a transação sempre volta (`rollback`).
Logo depois de abrir a transação, `run()` faz `SET LOCAL standard_conforming_strings = on` e recusa a sessão que não
ficar com ele `on` (também com `allow_superuser`), antes de preencher os parâmetros: desligado no papel ou no banco, o
PostgreSQL leria `\'` numa string como aspa escapada e o resto como código; o psycopg2 aberto assim escreve `E'...'`,
que a guarda recusa, então a conexão abre com `sql_guard.py options`.
`run()` recusa a sessão de superusuário: com ele, qualquer extensão ou função `SECURITY DEFINER` abre um caminho que
a lista de funções não cobre. `allow_superuser=True` deixa passar com um aviso (`RuntimeWarning`), só para um banco
de teste. Para conferir uma consulta antes de rodar: `python3 $S/scripts/sql_guard.py check "<SQL>"` (sai 4 com o
motivo quando recusa). O `check` vê só o texto que recebe: com parâmetros preenchidos no cliente (psycopg2, `%s` e
`%(nome)s`), o que vale é o texto final, então a consulta com parâmetros passa por `run()`, que monta esse texto com
`cur.mogrify`, confere e roda exatamente ele; sem `mogrify` no cursor, `run()` recusa `%s` dentro de string,
identificador entre aspas ou comentário. O parâmetro do lado do servidor (`$1`) não entra no texto e não tem esse
problema. O `check` só vale para uma sessão aberta com o `PGOPTIONS` de `sql_guard.py options` (no `psql`:
`PGOPTIONS="$(python3 $S/scripts/sql_guard.py options)" psql ...`); sem ele, num papel ou banco com
`standard_conforming_strings = off`, `\'` fecha a string e o resto vira código, o que o `check` não vê. Coluna com nome de palavra do SQL (`start`, `comment`, `release`, `lock`, `set`, que também é
abreviação de setembro) ou com `$` no nome vai entre aspas duplas: `SELECT "set" FROM ...`. Pedido de gravar,
corrigir ou apagar algo no banco: recusar e dizer que é com o usuário.

## Sinal de obra

Um e-mail, conversa ou arquivo é da obra quando tem pelo menos um destes:
- remetente ou domínio de `signal_senders`;
- uma palavra de `signal_words` no assunto, no corpo ou no nome do anexo;
- anexo de orçamento, proposta, pedido, contrato, nota fiscal, boleto, medição, ART/RRT, projeto (`.pdf`, `.xlsx`,
  `.dwg`, `.ifc`) vindo de alguém que já mandou algo da obra antes.

Com sinal: tratar no mesmo tick (orçamento vai para a análise abaixo; dúvida técnica, prazo ou cobrança viram uma linha
para o usuário com o próximo passo). Sem sinal: só entra no resumo do dia, agrupado por remetente. Na dúvida, contar
como da obra e dizer por quê. Nunca marcar como lido, arquivar, rotular nem responder.

## Análise de orçamento

### 1. Ler o orçamento inteiro

Tirar: fornecedor (CNPJ se houver), data, validade, itens (descrição, marca, modelo, unidade, quantidade, preço
unitário, total), mão de obra separada ou não, frete, impostos, condição de pagamento, prazo de entrega e de execução,
garantia oferecida pelo fornecedor e o que ficou de fora ("não incluso"). Anexo baixado fica em
`$I/cache/orcamentos/<AAAA-MM-DD>_<fornecedor>/`.

### 2. Preço

Para os itens que somam 80 % do valor:
1. Histórico pago (`price_history`): compare com a mediana e com o último preço pago; quando o pago tem mais de 3
   meses, corrija pelo INCC do período (de `references_dir`).
2. Orçamentos anteriores do mesmo item ou serviço (`quote_history`).
3. Referência pública: SINAPI do insumo ou da composição e CUB da `region`, do mês mais próximo que está em
   `references_dir`. Insumo do SINAPI que não está lá: "SINAPI do insumo não consultado", nunca uma estimativa.
4. Veredito por item, sempre com a conta (referência, fonte, diferença em R$ e em %): `ok` até `price_ok_pct` acima
   da referência, `atenção` entre `price_ok_pct` e `price_high_pct`, `sobrepreço` acima de `price_high_pct` sem
   justificativa técnica. Alto padrão justifica preço maior só quando a marca e a especificação comprovam.

### 3. Quantidade

- Somar no modelo (`quantities`) por classe ou texto do elemento e andar, olhando a unidade de cada soma (nunca somar
  metro com metro quadrado). Com o `ifc_index.py`, o andar vai antes do `qty --` (`--storey <andar>`) e o detalhe item
  a item sai com `class` ou `search` no lugar de `qty --`.
- Diferença acima de `qty_tolerance_pct` entre o orçado e o projeto: apontar com os números dos dois lados. Perda
  técnica (revestimento, argamassa, aço) aceitável até 10 %, se o fornecedor declarar.
- Item orçado que não está no projeto, ou do projeto que não está no orçamento: listar.

### 4. Marca, padrão e garantia

- Para cada marca: padrão (alto, médio, econômico) e garantia do fabricante (prazo, o que cobre, condição, como
  acionar).
- **Fonte obrigatória, com data da consulta:** site oficial ou termo de garantia do fabricante, catálogo técnico,
  norma. Loja, fórum ou propaganda não contam como fonte de garantia. Sem fonte: "não confirmado".
- Garantia legal (Código de Defesa do Consumidor, 90 dias para bem durável) e a responsabilidade do construtor pela
  solidez e segurança (Código Civil, art. 618, 5 anos) valem além da do fabricante: citar quando o orçamento promete
  menos.
- Registrar o achado em `$I/data/marcas.md`, uma linha por marca e linha de produto: marca, linha, padrão, garantia,
  fonte (link), data da consulta.
- Conferir se a marca e o modelo orçados batem com o especificado no projeto.

### 5. Parecer

Curto, para ler no celular:

```
Orçamento <fornecedor> <data>: R$ <total> (<itens> itens, validade <data>)
Preço: <n> ok, <n> atenção, <n> sobrepreço. Maior diferença: <item> R$ x vs R$ y (<fonte>, +z %)
Quantidade: <bate | diferenças: item, orçado x projeto>
Marcas: <alto padrão | misto | econômico>; garantias: <resumo>, fontes em data/marcas.md
Faltou no orçamento: <...>   Condições: <pagamento, prazo>
Recomendação: <aceitar | negociar itens X e Y | pedir revisão | comparar com outro fornecedor>
```

O parecer completo, com as contas e as fontes, fica em `$I/data/pareceres/<AAAA-MM-DD>_<fornecedor>.md`. Comparação
de fornecedores: o mesmo formato, uma linha por fornecedor por item, e o total equalizado (mesmo escopo dos dois lados;
o que um inclui e o outro não fica dito). Decisão que é do usuário (aceitar, trocar de fornecedor, pagar sinal) vira
uma tarefa `Decidir:` no Planou com o parecer na nota.

### 6. Rascunho (opcional)

Quando cabe negociar ou pedir esclarecimento, o rascunho vai em texto puro abaixo de uma linha `Rascunho:`, no tom do
usuário (direto, educado, pt-BR, sem travessão), citando os itens e os números. Ele fica na conversa. Criar o rascunho
no e-mail só com o OK do usuário naquele pedido. **Nunca enviar**, nem com o OK: quem envia é o usuário. O conector
do Gmail também tem enviar, responder e encaminhar; a recomendação é a instância negar essas ferramentas nas permissões
do Claude Code (`permissions.deny` do `settings.json`, ex.: `mcp__<servidor do Gmail>__send_message`, `__reply` e
`__forward`), para "nunca enviar" não depender só desta instrução.

## Resumo do dia

Na `digest_hour`, uma mensagem curta: o que chegou da obra (orçamentos analisados, pendências, prazos dos próximos 7
dias, pagamentos a vencer quando o adapter da instância mostra) e, à parte, o que chegou sem sinal de obra, agrupado
por remetente, numa linha cada. Nada novo: não mandar resumo.

```permissions
tools: mcp:gmail, other:websearch
actions: planou.note, planou.task
```
