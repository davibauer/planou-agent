---
name: meeting-minutes
description: Transcreve e diariza um vídeo/áudio de reunião (local, na GPU) e escreve a ata em Markdown (participantes, decisões, ações). Sob pedido explícito, também o diagnóstico de oratória do usuário (pitch/semitons, ritmo, pausas, muletas, erros de inglês) e o de pronúncia. Use quando o usuário pedir ata/minuta/resumo de uma reunião, call, entrevista ou gravação, ou passar o caminho de um vídeo de reunião.
---

**Caminhos:** `M=<a pasta desta skill>/scripts/meeting_minutes` (a "Base directory for this skill" que o Claude Code mostra ao carregá-la). É uma skill do plugin agent: a fonte de gravações dos agents de trabalho (`work-watch-<empresa>`) chama este mesmo `run.sh`. Scripts de investigação (cruzar vozes entre atas, métricas extras de oratória) ficam fora da skill, em `research/meeting-minutes/` na raiz do plugin. O que é do usuário fica em `~/.config/meeting-minutes/` (`MEETING_MINUTES_HOME` muda): `config.json` (modelo, idioma, token HF, `user_name`, `user_aliases`, `videos_dir`; sem ele vale o `config-example.json`), `speaker_refs.json` (vozes já nomeadas em atas antigas, para o `xmatch.py`) e `state/` (estado do watcher e o catálogo de vozes, dado biométrico que nunca sai da máquina). Os nomes dos arquivos de saída (`ata.md`, `transcricao.md`, `oratoria.json`…) ficam como estão: outras ferramentas leem por esses nomes.

## Quando usar

- "faz a ata dessa reunião", "transcreve esse vídeo", "resume essa call", "/meeting-minutes <vídeo>".
- Qualquer gravação de conversa: reunião, entrevista, call de cliente, 1:1, apresentação.
- **Não** use para aula ou treino de idioma com dashboard de evolução: isso é outra ferramenta.

**O padrão é só a ata.** Os passos 4 (oratória) e 5 (pronúncia) ficam **DESLIGADOS** e só rodam quando o usuário pedir de forma explícita — "analisa minha oratória", "como eu falei", "roda a pronúncia". Sem pedido: passo 2 → passo 3 → passo 6, sem `oratoria.md` e sem `pronuncia.json`. Pedido do usuário em 22/09/2026 (a pronúncia de manhã, a oratória logo depois): é análise cara e nem sempre desejada, e em fala curta não sai diagnóstico nenhum.

Quando a reunião for em inglês e o usuário **pedir** a oratória, ela sai junto com os **erros de inglês** (fim do passo 5) — isso faz parte do passo 4, não exige a pronúncia.

## O que produz (em `<pasta_do_vídeo>/ata_<nome>/`)

| arquivo | quem escreve |
|---|---|
| `ata.md` | **você** (passo 3) |
| `oratoria.md` | **você** (passo 4) — **opcional**, só quando o usuário pede a oratória |
| `transcricao.md` | script — transcrição diarizada com timestamps |
| `speakers.json` | script — estatísticas por locutor, ruídos, nomes citados |
| `oratoria.json` | script — prosódia por locutor (matéria-prima do passo 4) |
| `pronuncia.json` | script — **opcional**, só quando o usuário pede a análise de pronúncia |
| `transcript.json` | script — dados brutos do whisperx |

## Passos

### 1. Rodar o pipeline

```bash
bash "$M/run.sh" "<CAMINHO_DO_VIDEO>"
```

Roda **em segundo plano** — ~1 min de GPU por 6 min de áudio (large-v2, GPU de 8 GB). Tudo local: nenhum áudio sai da máquina. Requer `whisperx`, `ffmpeg`, GPU NVIDIA e token HuggingFace (`hf_token_path` do config, padrão `~/.cache/huggingface/token`); instalação no README do plugin.

Idioma **detectado automaticamente** (não presuma português — confira `Idioma detectado` no topo do `transcricao.md`). Vídeo com várias faixas de áudio (OBS): usa a faixa 0 (o mix), que é o certo para diarização; só mexa em `audio_stream` se alguma voz sumir.

### 2. Descobrir quem é quem — **antes** de escrever qualquer coisa

O whisperx entrega `SPEAKER_00`, `SPEAKER_01`… Uma ata com "SPEAKER_01 ficou responsável por X" é inútil, e a análise de oratória fica **errada** se você medir o locutor errado. Então:

**Primeiro, a impressão de voz (determinística) — não o nome citado.** Rode:
```bash
python3 "$M/voiceprint.py" identify "<WORKDIR>" "<VIDEO>"
```
Isso compara cada `SPEAKER_XX` com o catálogo de vozes conhecidas (`~/.config/meeting-minutes/state/voices/`). Para o **usuário** (`user_name` do config, com a voz dele registrada por `voiceprint.py add-ref`) o resultado é confiável (cosseno ~0.8–0.95 quando é ele; ~0.1 quando não é — validado em 14/07/2026). **Use isto como a verdade sobre quem é o usuário**, inclusive se ele aparecer como `desconhecido` em todos os locutores → então ele **não fala** nessa reunião (não invente uma fala dele).

> Por que primeiro: o método por nome citado JÁ FALHOU (08/07/2026: um nome dito numa despedida apontou o locutor errado; a voz mostrou que o usuário era outro). Nome citado agora é só **confirmação**, não a fonte. Quando voz e nome discordam, a **voz vence** — e essa discordância é sinal de que a diarização atribuiu mal um turno.

Depois:
- Leia `transcricao.md` inteiro (não só o começo).
- Nomes dos OUTROS participantes: as pessoas se chamam pelo nome. Em `speakers.json`, `nomes_citados` mostra **quem falou** cada nome — quem é *chamado* pelo nome costuma ser o **outro** locutor do turno. (Para vozes já no catálogo, o `identify` já dá o nome.)
- Locutores marcados `provavel_ruido` (fatia mínima do tempo) são silêncio, eco ou fragmentos mal atribuídos — não os liste como participantes.
- Se um nome não aparecer no áudio nem no catálogo, **não invente**: use o papel ("Recrutador(a)", "Cliente") e pergunte ao usuário no final.
- Sem o locutor do usuário identificado, não faça os passos 4 e 5.

### 3. Escrever a `ata.md`

Leia a transcrição de verdade — não é template automático. Estrutura (corte o que não se aplica):

- **Cabeçalho**: data, duração, idioma, participantes (nome + papel), mencionados (citados mas ausentes).
- **Contexto / objetivo** em 1–3 linhas.
- **Pontos discutidos** agrupados por tema, não em ordem cronológica crua.
- **Decisões** tomadas.
- **Pendências / dúvidas em aberto.**
- **Ações** — tabela `| # | Ação | Responsável | Prazo |`. Só o que foi realmente combinado.
  **Responsável = quem recebeu a instrução ou se ofereceu, na transcrição.** Crítica a um hábito não é atribuição: se o que vem colado na crítica é outra instrução ("transfere isso pro Bruno"), a ação é essa, e a correção do hábito só entra se alguém disser quem faz. Na dúvida, `Responsável: a confirmar` e o trecho vai para *Pontos a confirmar*. (22/09/2026: a ata do refinamento atribuiu ao usuário "reorganizar as stories do M2" a partir de uma crítica; a transcrição só mandava repassar o conhecimento.)
  **Prazo como foi combinado, sem inventar data.** Prazo que depende de outra coisa ou de outra pessoa vai como a condição, com o nome de quem ela depende: `depois que <Nome> mandar o material`, `quando o acesso for liberado`, `a definir`. Prazo distante vai como data absoluta ou mês/ano (`15/11/2026`, `novembro/2026`). O work-watch lê essa coluna: condicional ou "a definir" vira item em *Aguardando por* (sem lembrete), mais de 14 dias depois da reunião vira item em *Próximas*, e só o prazo curto e claro vira pendência com lembrete.
- Timestamps `[mm:ss]` nos pontos importantes.

Regras que fazem a diferença:

- **Ata é síntese, não transcrição parafraseada.** Se a pessoa deu voltas, resuma a conclusão.
- **Nunca invente número, nome, data ou combinado.** O ambíguo vai numa seção *"Pontos a confirmar"*, não no corpo como fato.
- Datas relativas ("próxima segunda") viram **data absoluta** — calcule a partir da data da gravação e confira o dia da semana. Prazo vencido ou hoje: **avise em destaque**.
- Escreva em **pt-BR**, mesmo que a reunião tenha sido em outro idioma.

### 4. Escrever a `oratoria.md` — **opcional, desligada por padrão**

**Não rode este passo sozinho.** Só quando o usuário pedir a oratória de forma explícita. Sem pedido: pule para o passo 6 e não mencione prosódia na entrega.

Leia `oratoria.json` → `locutores[<locutor do usuário>]`. **Ignore os outros locutores** (servem de contraste, mas não são a régua: um deles pode falar mal também).

O JSON traz `faixas_referencia` — **use essas faixas, não invente benchmark.** As armadilhas, todas já pagas caro aqui:

- **Ritmo:** classifique pelo `wpm_bruto` (faixa de conversa: 110–160). O `wpm_articulado` roda naturalmente mais alto (~150–210) porque desconta as pausas — aplicar a faixa de conversa nele acusa "acelerado" em quem fala num ritmo perfeitamente normal.
- **Pitch:** o que importa é `sd_semitons` (variação em semitons re a mediana do próprio locutor) — é isso que soa como "monótono" (<2.0) ou "expressivo" (>2.8). A mediana em Hz é só timbre, não é qualidade.
- **Volume:** só `volume_intra_sessao`. **Nunca compare dB entre reuniões** — muda com microfone e ganho, não com a voz.
- **Muletas:** são **piso, não taxa real** — o whisper limpa boa parte dos "é/uh/um" na transcrição. Diga isso. As `candidatas` (tipo, né, so, like, i mean) incluem palavras legítimas: confira no `transcricao.md` se são muleta mesmo antes de acusar.
- **Insegurança (`inseguranca`):** só significa algo **comparado aos outros locutores da mesma call** — taxa isolada não diz nada. E o sinal **inverte nos dois sentidos**: nativo usa "I think / kind of / maybe" como **educação e rapport**, não só como dúvida, então ficar muito *abaixo* da referência pode soar seco/abrupto, não confiante. (Medido em 13/07/2026: usuário 0,9/100 palavras vs 2,0 do entrevistador nativo — hedging não era problema dele.) Leia os `exemplos` antes de afirmar qualquer coisa: `just` e `só` são legítimos na maior parte das vezes.

Estruture como **coaching, não como dump de métricas**: 2–4 pontos acionáveis, cada um ancorado num timestamp de `momentos` para ele ouvir o trecho no vídeo ("trecho monótono em [12:30]", "pausa de 7s em [20:49]"). Termine com **o que treinar na próxima**.

### 5. Pronúncia — **opcional, desligada por padrão**

**Não rode este passo sozinho.** Só quando o usuário pedir a pronúncia de forma explícita (e aí, claro, só se `idioma` for `en` e o passo 4 tiver sido pedido também). Sem pedido: pule direto para o passo 6 e não escreva seção de pronúncia na `oratoria.md`.

```bash
python3 "$M/pronunciation.py" "<WORKDIR>" "<VIDEO>" <SPEAKER_DO_USUARIO> "$(python3 "$M/mm_config.py" config)"
```

Roda **depois** do passo 2 (precisa saber qual locutor é o usuário) e leva alguns minutos de GPU.

**Como ler o `pronuncia.json` — leia isto antes de reportar qualquer coisa:**

O modelo fonético tem **viés próprio por fonema**: num áudio de reunião ele só "ouve" o /ŋ/ de *-ing* em ~60% das vezes e o /æ/ em ~30% **mesmo num falante nativo**. Por isso a saída é **comparativa** — a taxa do usuário contra a dos outros locutores da mesma gravação, o que cancela esse viés.

- Reporte **apenas os grupos com `lacuna: true`**. Os demais são ruído do modelo, ainda que a porcentagem pareça baixa. (Medido em 13/07/2026: TH 34% vs 63% do entrevistador = lacuna real; -ING 60% vs 62% = puro viés do modelo.)
- **Nunca leia uma taxa ALTA como elogio.** No mesmo áudio, o usuário fez 51% de /æ/ contra 28% do entrevistador nativo — e isso é *ruim* para ele: o nativo "perde" porque **reduz vogal átona** (`and` → /ən/, `can` → /kən/ — soa nativo justamente por isso), enquanto o usuário **articula a palavra funcional inteira** ("ÆND"), o que é sotaque. A métrica mede abertura de vogal, não acerto. Ficar acima da referência num som pode significar o oposto de "melhor".
- Se `controle_audio.ok` for falso, ou se não houver locutor de referência (reunião só com brasileiros), **não reporte pronúncia** — diga que o áudio/contexto não permitiu isolar. Silêncio é melhor que erro inventado.
- A `premissa` é que a referência pronuncia certo — ela **pode não ser nativa**. Diga isso quando reportar.
- Use `exemplos_para_ouvir` (palavra + timestamp + o que o modelo ouviu) como lista de treino: ouvir no vídeo com **loop + 0.75×**. Não é nota.

**Erros de inglês além da pronúncia:** o usuário quer saber os erros dele. Leia as falas dele em inglês no `transcricao.md` e liste gramática, colocação e naturalidade (com a correção e o timestamp). Isso é julgamento seu, não script. Lembre que a transcrição por IA **limpa** escorregões pequenos — então o diagnóstico subestima; diga isso.

### 6. Entregar

Caminho do `ata.md`, o que foi decidido e as ações com prazo. A oratória (2–3 pontos + caminho do `oratoria.md`) e as lacunas de pronúncia entram **só quando os passos 4 e 5 tiverem sido pedidos**. Pergunte sobre qualquer participante que você não conseguiu nomear pelo áudio.

## Automação (já roda sozinha)

Opcional, no Windows com WSL: o **FileSystemWatcher** (`watch-fs.ps1`, desta pasta, iniciado no logon com `-Cmd <caminho do watch.sh no WSL>`) dispara ~45 s depois que o OBS para de gravar; um **cron de 5 min** com `watch.sh` é a rede de segurança. Com o work-watch, a fonte de gravações dele já dispara o `run.sh` e esta automação pode ficar pausada. Ambos chamam `watch.sh`, que só processa vídeo **finalizado** (tem *moov atom* — o OBS só escreve o índice do MP4 ao parar a gravação) e com tamanho estável.

Ou seja: **quando o usuário abrir a sessão, a transcrição + prosódia da reunião nova provavelmente já está pronta.** Veja `~/.config/meeting-minutes/state/pendentes.txt` — lista as transcrições sem `ata.md`. A ata e a oratória continuam sendo escritas por você (passos 2–5), de propósito.

| comando | para quê |
|---|---|
| `bash "$M/pause.sh"` | pausa tudo (cron, watcher e backfill): cria `state/PAUSED` |
| `bash "$M/resume.sh"` | retoma de onde parou (pula o que já tem `transcript.json`) |
| `bash "$M/watch.sh" --dry-run` | mostra o que o cron faria agora, sem rodar |
| `bash "$M/rerun_prosody.sh"` | recalcula `oratoria.json` em todas as pastas (após mudar o `prosody.py`) |

**Se nada estiver processando, cheque `~/.config/meeting-minutes/state/PAUSED` antes de sair caçando bug.**

O lock da GPU fica **dentro do `run.sh`** (`/tmp/ata-reuniao-whisperx.lock`), não em quem chama: outra sessão do Claude pode invocar `run.sh` direto, e com o lock do lado de fora dois whisperx dividiam a mesma GPU de 8 GB.

## Notas

- Tudo **local**, na GPU. Nenhum áudio sai da máquina.
- O objetivo da oratória é **melhorar a cada reunião**: `sd_semitons`, `wpm_bruto`, `pausa_ratio_pct` e muletas são comparáveis entre gravações; **dB não é**.
