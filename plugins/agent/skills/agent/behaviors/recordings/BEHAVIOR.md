---
title: Gravações e atas
summary: Casa a gravação da reunião com a agenda, transcreve e escreve a ata com as ações.
layer: skill
kind: always
---
# recordings: gravações do OBS x agenda

Vale para a instância com a fonte `gravacoes`: o vídeo da reunião gravada é casado com o evento da agenda da
instância, transcrito e vira ata; as ações suas da ata viram pendências. O motor é o compartilhado
`watch_core.recordings` (regras completas em `~/.config/watch-core/docs/recordings.md`); a agenda vem da opção `agenda`
da fonte (`outlook`, `google` ou `cache`). `A="python3 $S/scripts/agent.py <instância>"`.

Opções em `behavior_config.recordings` (`$A --status` mostra):
- `calendar`: comando de consulta da agenda desta instância, para cruzar convites e conflitos (ex.: `"--agenda"` na
  agenda em cache; sem a opção, o que o `instructions.md` disser).
- `query_hours`: janela, em horas, da consulta do bloco (padrão 72).
- `push`: linhas do bloco que viram aviso ativo (padrão `["ATA PENDENTE", "ACAO SUA", "ERRO"]`).

## O bloco `== GRAVACOES`

Uma linha por mudança, nunca repetida: `GRAVANDO` -> `FINALIZANDO` -> `TRANSCREVENDO` (`AGUARDANDO GPU`) ->
`TRANSCRICAO PRONTA, ATA PENDENTE` + `pasta:` -> `ATA PRONTA` -> `ACAO SUA`; `NAO GRAVADA` sai uma vez (informativo).
- `ATA PENDENTE`: escrever a ata no mesmo tick (skill `meeting-minutes`, passos da ata; oratória e pronúncia só sob
  pedido), conferindo antes que o assunto da transcrição é o do evento casado. Assunto de outra reunião: dizer, e
  corrigir o dono (seção Consulta) em vez de escrever a ata errada.
- `ACAO SUA`: a ação da ata com você como responsável virou pendência `reuniao`; dizer em uma linha e propor o próximo
  passo. Prazo curto entra na fila; o resto vai para aguardando ou próximas.
- `ERRO`: dizer a causa em uma linha (vídeo sem moov, GPU, run.sh).
- Nunca transcrever reunião em andamento; o motor só entrega vídeo finalizado.
- Dono entre agents: o vídeo fica com a instância de maior interseção com a agenda; quem perde larga em silêncio.
- Primeira rodada: planta a base em silêncio (atas antigas não viram cobrança).

## Agenda em cache (`"agenda": {"tipo": "cache"}`)

Quando a agenda de trabalho só chega por um conector (ex.: free/busy compartilhado), o tick não busca: a sessão chama o
conector uma vez por dia, ou quando o bloco avisa que o cache venceu, e grava:

```bash
$A --agenda set '<JSON do conector>'     # atualiza o cache
$A --agenda nomear <id> "título"         # dá título a um bloco sem nome
$A --agenda                              # blocos das últimas e próximas 24 h e a idade do cache
```

## Consulta

```bash
$A --so gravacoes --since <query_hours>h --dry     # o bloco da janela, sem gravar nada
env PYTHONPATH=$S/scripts python3 -c 'from watch_core import recordings as r; r.forca_dono("<ts>", "<agent>")'
                                                  # o vídeo "<AAAA-MM-DD HH-MM-SS>" passa a ser deste agent
```

## Ferramentas e ações do papel

O que este papel usa (conferido pelo `--validate` contra o `autonomy` e o `tools` da instância; o `--brief` só passa ao worker o que o papel permite). Ler é sempre permitido.

```permissions
tools: -
actions: notion
```
