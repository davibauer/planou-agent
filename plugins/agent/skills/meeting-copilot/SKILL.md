---
name: meeting-copilot
description: "Copiloto de reunião ao vivo: escuta a reunião na máquina (microfone e áudio do sistema no Windows, transcrição local na GPU), percebe quando alguém pergunta algo ao usuário e sugere em segundos o que responder, com o que tomar cuidado e o que perguntar de volta, citando a base (memórias, atas, PRs, issues); vai anotando decisões, ações e perguntas abertas durante a reunião. Use quando o usuário pedir para ligar o copiloto, ajuda ao vivo numa reunião ou call, ou para testar o copiloto com uma gravação (replay)."
---

**Caminhos:** `C=<a pasta desta skill>/scripts/meeting-copilot` (o executável). Detalhes, configuração e números medidos: `README.md` desta skill.

## Quando usar

- "liga o copiloto", "me ajuda nessa reunião", "vou entrar na daily, fica ouvindo": `"$C" start --instance <instância>`.
- "testa o copiloto com a gravação de hoje": `"$C" replay <vídeo> --instance <instância> [--start mm:ss --end mm:ss] [--speed 2]`.
- "o copiloto está acertando quem é perguntado?", antes e depois de mexer no porteiro: `"$C" eval` (reuniões gravadas,
  sem modelo; `--review --model` mede também a etapa do modelo, com custo; detalhes no README da skill).
- Primeira vez numa máquina: `"$C" setup-windows` (cria o venv de captura no Windows) e `"$C" devices` (lista microfones e saídas).

A instância é a do agent (`~/.config/agent/<instância>`): dela vêm o contexto (CONTEXT.md, handoff.md, memórias e atas
do workspace; PRs abertas pelo `gh`; issues abertas pelo token do Jira da fonte `jira`). O painel abre como janela
estreita do navegador do Windows; o áudio nunca sai da máquina, só texto vai ao modelo.

## Como roda

1. Rode em background (`run_in_background`), porque a sessão fica ativa até a reunião acabar. Ele segura o lock da GPU
   da meeting-minutes (`/tmp/ata-reuniao-whisperx.lock`): se uma ata estiver transcrevendo, ele avisa e sai (ou espera
   com `--wait-gpu`).
2. O usuário encerra pelo botão de parar do painel (ou Ctrl+C). Ao fim ficam em
   `~/.config/agent/<instância>/data/meeting-copilot/<data-hora>/`: `transcript.md`, `notes.md`, `cards.jsonl`,
   `metrics.jsonl`, `summary.json` e o `context.md` que o modelo recebeu.
3. Depois, a ata continua sendo da `meeting-minutes` (gravação do OBS); o `transcript.md` e o `notes.md` da sessão
   ajudam a conferir decisões e ações.

## Regras

- Nada é enviado a ninguém: o copiloto só mostra sugestões ao usuário. Não posta em Slack, Jira, GitHub nem e-mail.
- Credenciais só de leitura e já existentes (gh logado, token do Jira da instância); o token nunca é impresso.
- No replay, o pacote de contexto só usa atas e PRs anteriores ao horário da gravação (pelo nome do arquivo do OBS),
  para o teste não "colar" da ata da própria reunião.
