# meeting-copilot

Copiloto de reunião ao vivo. Enquanto a reunião acontece, ele transcreve na GPU local, percebe quando alguém pergunta
algo ao usuário e mostra em poucos segundos três cartões: **Answer** (o que dizer, no jeito de falar do usuário),
**Careful** (o risco de responder sem pensar) e **Ask** (o que perguntar de volta), cada um com o "based on" das fontes
do contexto. Na aba **Notes** ele vai juntando decisões, ações (dono e prazo como foram ditos) e perguntas abertas.
No modo "Everything" também aparecem cartões FYI do que os outros disseram e importa ao trabalho do usuário.

O áudio fica na máquina. Só texto (transcrição e contexto) vai ao modelo.

## Comandos

```bash
C=<pasta desta skill>/scripts/meeting-copilot      # ou: ln -s "$C" ~/.local/bin/meeting-copilot

"$C" setup-windows                                 # uma vez: venv de captura no Windows (pyaudiowpatch)
"$C" devices                                       # microfones e saídas que a captura enxerga
"$C" start  --instance <instância>                 # reunião ao vivo
"$C" replay <gravação.mp4> --instance <instância> [--start 24:30] [--end 45:00] [--speed 2]
"$C" eval [--meetings '<pasta>/ata_*'] [--n 3]       # mede o porteiro "é pra mim?" nas reuniões gravadas
```

Opções comuns: `--no-open` (não abre a janela), `--offline` (contexto sem GitHub e Jira), `--title`, `--port`,
`--language`, `--wait-gpu`, `--exit-when-done` (sai quando a fonte acaba, para teste).

## Como funciona

```
Windows: microfone (o usuário) + loopback WASAPI dos alto-falantes (os outros)
   │  PCM cru por TCP em localhost (win/capture.py)                 replay: ffmpeg lê a gravação no ritmo real
   ▼
WSL: VAD Silero corta a frase (0,6 s de silêncio, ou numa pausa depois de 7 s)
   ▼
faster-whisper large-v3 int8 na GPU (~0,5 s por frase) + quem falou
   │  ao vivo: canal do microfone = "You"; replay: impressão de voz do catálogo da meeting-minutes
   ▼
porteiro, etapa 1 (detect.py, sem modelo): a cada fim de turno de outra pessoa, 1,2 s depois da última palavra,
soma as pistas (nome, resposta ao usuário, vez dele na rodada da daily, "e você?", "você" ou algo que ele mesmo citou
na reunião junto de pergunta ou pedido); um terceiro que toma a palavra antes cancela
   ▼
porteiro, etapa 2 (addressee.py, Sonnet): "é pra ele?", com prompt pequeno e fixo (nomes e mapa de assuntos dele)
   ║ em paralelo
1 chamada ao modelo (claude CLI headless, Haiku) que só escreve os cartões, em linhas, com os tópicos primeiro,
transmitido aos poucos; duas iguais correm juntas e a primeira vence; os cartões aparecem quando a etapa 2 diz sim
   ▼
painel local (SSE) ← notas a cada ~60 s numa chamada separada (Sonnet)
```

- **Contexto** (montado na largada, só leitura): `CONTEXT.md` e `handoff.md` do workspace da instância, o índice de
  memórias (e o texto das memórias que casam com a pergunta), as últimas atas de `meetings/`, as PRs abertas do usuário
  (`gh`), as PRs dele abertas, atualizadas ou mergeadas nos últimos 14 dias com os arquivos mudados e o texto das
  mudanças de schema (migrations, `.sql`, entidades do ORM; ~18 mil caracteres no máximo), com uma linha de fatos
  contados por migration ou entidade (`V1_76_0 (repo#n): tabela +6 columns (...); no index`) que o modelo usa para
  qualquer contagem, e as issues abertas dele no Jira (token da fonte `jira` da instância). Tudo vai como system prompt fixo, então
  o cache de prompt serve as chamadas seguintes.
- **Modelo**: `claude -p` com `--tools ''`, `--strict-mcp-config`, `--setting-sources ''` e `--effort low`, em processos
  já iniciados (o CLI leva ~2 s para subir). Sem chave nova: usa o login do Claude Code.
- **GPU**: segura `/tmp/ata-reuniao-whisperx.lock` (o mesmo da meeting-minutes) a sessão inteira.
- **Replay**: o pacote de contexto só usa atas e PRs anteriores ao horário da gravação (PR criada depois fica de fora;
  mergeada depois aparece como aberta; PR que mudou depois entra com os arquivos até o último commit antes da gravação), para o teste não colar da ata
  da própria reunião.

## O monitor (painel)

Janela estreita ao lado da reunião (pensada para 360 a 420 px), no tema do sistema ou escolhido no menu `···`, com
uma cor de destaque só. A regra é ficar quieto até ter algo para o usuário (`monitor.py` decide o que aparece; o
painel só desenha):

- **Quieto**: o assunto de agora e, embaixo, a pergunta que vale fazer (lacuna, risco, ação sem dono), que vêm na
  chamada das notas. Uma pílula "Ouvindo" mostra que está vivo.
- **Pra você**: quando o porteiro passa do limiar, um cartão grande com a pergunta e 2 ou 3 tópicos do que dizer
  (tópicos, não frases para ler), a pergunta de volta e o cuidado, se houver. Tocar no cartão mostra de onde veio
  (PR, ata, memória); a frase pronta só aparece com `F`.
- **O resto da reunião**: tocar na pílula (ou `Espaço`) abre decidido, ações com dono e prazo (e as sem dono), e as
  perguntas em aberto.
- **Fim**: as notas na tela, já salvas em `notes.md` e `transcript.md` para a ata.

Controle de ruído no menu e no teclado: limiar de confiança Baixo, Médio (padrão) e Alto (`1`, `2`, `3`; confiança =
pontos do porteiro + 2 quando a etapa 2 disse sim); "Só o que é meu" ou "Tudo" (`M`: no "Tudo" entram as ações dos
outros, o que foi ouvido e o que ficou abaixo do limiar); `Esc` dispensa o cartão (não volta, nem ao reconectar);
`P` pausa; `T` troca o tema.

## Contexto pronto antes da reunião

Com o gancho `meeting_copilot` na instância (`"hooks": [..., {"type": "meeting_copilot"}]`), cada tick pesado do
runner dispara `meeting-copilot prepare` em segundo plano (o tick não espera e não imprime nada):

- reconstrói só a parte do pacote cuja fonte mudou (arquivos por mtime e tamanho; PRs, issues pela resposta; o texto
  de schema de uma PR só é buscado de novo quando a PR mudou) em `data/meeting-copilot/context.md`, `system.md` (o
  system prompt exato das chamadas de cartão) e `meta.json`;
- com um bloco ocupado da agenda (cache da fonte `gravacoes`) começando nos próximos 15 min, espera até ~2 min antes,
  fecha o pacote e manda uma chamada mínima com aquele system prompt, uma vez por bloco: o cache de prompt está quente
  quando a primeira pergunta chega. Nunca liga a captura de áudio;
- uma linha em `cache/runner/avisos.log` quando algo mudou ou falhou, nunca FONTE QUEBRADA; com a trava da GPU
  ocupada (reunião em andamento) não faz nada.

`start` usa o pacote pronto quando ele tem menos de 15 min; senão completa o que mudou (ou monta do zero, sem pacote
pronto). O replay continua montando o próprio pacote "como era na hora da gravação".

## Saída

`~/.config/agent/<instância>/data/meeting-copilot/<AAAA-MM-DD-HHMMSS>/`

| arquivo | o que é |
|---|---|
| `transcript.md` | transcrição com horário e quem falou (`You` = usuário) |
| `notes.md` | decisões, ações e perguntas abertas, no mesmo formato do link "Notas em Markdown" do painel (`/notes.md`) |
| `cards.jsonl` | cada chamada de cartões: pergunta, resposta bruta, latência, tokens |
| `metrics.jsonl` | por frase (ASR, quem falou, similaridade de voz) e por chamada |
| `summary.json` | totais e medianas da sessão |
| `context.md` | o system prompt que o modelo recebeu |

## Configuração (`~/.config/meeting-copilot/config.json`, tudo opcional)

```json
{
  "default_instance": "<instância>",
  "windows_tools_dir": "%USERPROFILE%\\<pasta>\\meeting-copilot",
  "cards_model": "claude-haiku-5-5", "notes_model": "claude-sonnet-5-5", "hedge": 2,
  "asr_model": "large-v3", "language": "en", "notes_every_s": 60,
  "mic_device": "", "loopback_device": "",
  "instances": {"<instância>": {"asr_terms": ["nomes e termos que o ASR erra"], "memory_dirs": [], "extra_files": []}}
}
```

- `addressee_model`: o modelo da etapa 2 do porteiro (padrão `claude-sonnet-5-5`; `""` desliga, e a chamada dos
  cartões volta a decidir sozinha). A etapa 2 tem 5,5 s no total; se falha, estoura o prazo ou responde algo ilegível,
  os cartões aparecem como se ela tivesse dito sim, e o `summary.json` conta à parte (`addressee_yes`, `addressee_no`,
  `addressee_fail`).
- `instances.<instância>.topics`: assuntos do usuário, somados aos que o copiloto tira do contexto (PRs e issues
  dele, linhas das atas que citam o nome dele). O mapa vai para a etapa 2; na etapa 1 só com `gate_topics: true`
  (medido: mais alertas sem ganho de recall). Fica só no config local, nunca no repositório.
- `eval_exclude` (pedaços do nome da pasta ou ids) e `eval_exclude_types`: o que o eval deixa de fora, como
  gravações pessoais.

Nome e apelidos do usuário vêm da `meeting-minutes` (`~/.config/meeting-minutes/config.json`), assim como o catálogo
de vozes e o token do Hugging Face.

## Medir o porteiro (eval)

`"$C" eval` mede, nas reuniões já transcritas pela `meeting-minutes` (`<videos_dir>/ata_*`: transcript.json,
speakers.json, voiceprints.npz, oratoria.md), se o porteiro acerta quando uma fala é dirigida ao usuário. Não
transcreve de novo e só chama o modelo com `--model`; roda em segundos e serve para comparar antes e depois de cada
mudança no porteiro.

- **Quem é o usuário:** os locutores cuja impressão de voz bate com a do catálogo da `meeting-minutes` (cosseno a partir
  de 0,5; dois = a diarização partiu o usuário); sem isso, o locutor que o `oratoria.md` cita. Reunião sem nenhum dos
  dois fica de fora e é contada.
- **Rótulo automático**, por turno de outra pessoa (o "aham" de um terceiro no meio não encerra o turno): `pos` se o
  usuário toma a palavra com uma resposta de verdade até N s depois do fim (sobreposição conta; um "ok" antes não
  atrapalha, desde que a resposta comece nos mesmos N s); `neg` se um terceiro toma a palavra nesse prazo; `weak` se o
  usuário só solta um "ok", ou se a resposta de verdade vem depois de N s (fica fora de P/R/F1); `excl` se ninguém fala
  em N s. O rótulo não olha a forma da frase, para o porteiro não ser medido contra ele mesmo.
- **O porteiro** é chamado como ao vivo: uma vez por segmento, com o histórico de linhas juntado como em `add_line`. A
  transcrição offline tem pontuação melhor que a ao vivo, então sai também a medida sem pontuação.
- **Saída:** precisão, recall, F1 e alertas por hora por idioma e por tipo de reunião (daily, planning, 1:1, outras),
  para vários N (`--sweep 1.5,3,5,8`), e por que o porteiro perdeu ou alarmou. `--json` para comparar execuções.
- **Revisão à mão:** `--sample 40` grava uma amostra cega (sem rótulo e sem o voto do porteiro) e `--review` compara
  o arquivo de veredictos (`{"id", "addressed", "asks"}` por linha) com o rótulo automático e com o porteiro: erro
  do rótulo por estrato (a taxa geral é a da amostra 50/50) e P/R/F1 re-ponderados para a proporção real de `pos` e `neg`.

- **Porteiro v1 e v2:** `--gate v1` mede o porteiro antigo (forma de pergunta), para comparar; o padrão é o v2
  (`detect.py`), decidido uma vez por fim de turno no mesmo momento em que o app decide ao vivo. A tabela sem
  pontuação vem primeiro: é a mais perto do ASR ao vivo e a que se otimiza.
- **Partes do v2:** `--no-suppress` mostra quanto do ganho vem de esperar o terceiro tomar a palavra; `--topics
  minutes|context|none` escolhe o mapa de assuntos (padrão: atas da mesma instância anteriores à reunião; `context`
  junta o CONTEXT.md de hoje, que vaza o futuro), que vai para a etapa 2 e, com `--gate-topics`, também para a 1; `--split tune|check` usa metade das reuniões (pesos ajustados na
  `tune`, conferidos na `check`); `--threshold` muda o limiar.
- **Etapa 2 (modelo):** `--review --model [MODELO]` roda o modelo de destinatário nos turnos revisados que passaram
  na etapa 1 e numa amostra de 40 turnos que passaram (para estimar alertas por hora depois do modelo). Cada resposta
  fica em cache em `~/.cache/meeting-copilot/eval/llm/`; no máximo `--model-limit` chamadas novas por execução; o
  relatório traz tokens, US$ e latência. Usa o login do Claude Code, como o resto do copiloto.

Tudo que tem fala de reunião (cache das reuniões, amostra, veredictos e respostas do modelo) fica em
`~/.cache/meeting-copilot/eval/`, nunca no repositório.

## Limites conhecidos

- No replay só existe a faixa mixada: quando duas pessoas falam sem pausa, a frase sai inteira com um só dono, e os
  outros locutores às vezes se dividem em mais de um "Speaker N". Ao vivo isso não acontece para o usuário (canal
  próprio).
- O modelo às vezes "pensa" antes de responder, mesmo com `--effort low`: é a cauda da latência. A corrida de duas
  chamadas corta boa parte dela, ao custo de tokens de leitura de cache.
- Ao vivo sem fone, o microfone também capta a voz dos outros saindo da caixa de som.
- Microfone no Windows: o Python da Microsoft Store é app empacotado e tem permissão de microfone própria (Configurações >
  Privacidade > Microfone). Negada, a captura usa o `ffmpeg.exe` por DirectShow para o microfone (precisa do ffmpeg no
  PATH do WSL); o loopback não pede permissão. `"mic_backend": "dshow"` no config força esse caminho.
- Notas: o modelo devolve só o que mudou e os ids a remover; o que ele não devolve fica como estava.
