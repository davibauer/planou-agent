# Team Terminals (extensão do VS Code)

Abre um terminal por agente do time, cada um rodando `team <agente>`, quando o VS Code abre. Serve para não abrir os
terminais à mão depois de reiniciar o PC. Roda do lado do WSL (`extensionKind: workspace`), sem dependências.

## O que faz

- **Ao abrir o VS Code** (e quando o `agents.json` muda, ver abaixo como a mudança é percebida): para cada agente habilitado sem terminal, cria um terminal com
  o nome do agente e digita `team <agente>`. Terminal já aberto com esse nome não é duplicado. Entre uma abertura e a
  próxima espera `teamTerminals.delaySeconds` (3 s), para não subir todas as sessões ao mesmo tempo.
- **Terminal restaurado parado:** depois de reiniciar o PC o VS Code pode restaurar os terminais com o shell novo e
  sem nada rodando. Se o shell do terminal do agente não tem processo filho, a extensão roda o comando nele em vez de
  abrir outro (`teamTerminals.reviveIdle`). Isso só acontece ao abrir o VS Code e no comando "Team: abrir terminais",
  nunca numa mudança do arquivo.
- **Agente desabilitado ou removido** do `agents.json`: se ele é instância do plugin agent
  (`~/.config/agent/<agente>/config/config.json`), roda `runner.sh <agente> stop` e depois fecha o terminal. Os outros
  agentes só têm o terminal fechado, com um aviso (o runner deles para pela própria sessão).
- Só fecha terminal de agente que estava habilitado antes ou que está com `"enabled": false`. Terminal com outro nome
  nunca é tocado. Arquivo quebrado (JSON inválido, meio salvo) não abre nem fecha nada: aparece um aviso.

**Como a mudança do arquivo é percebida.** O watcher do VS Code acompanha a pasta `~/.config/team`, mas, por ela estar
fora do workspace, às vezes perde o evento (30/09: o funcionário ligado pelo app ficou sem terminal). Por isso a
extensão também confere a data, o tamanho e o inode do `agents.json` a cada `teamTerminals.pollSeconds` (10 s) e
reconcilia quando mudam. As duas vias dividem a mesma espera de 700 ms, e uma mudança já vista pelo watcher não é
reconciliada de novo pela verificação. Só com `teamTerminals.autoOpen` ligado. No registro, a conferência aparece como
"arquivo" (watcher) ou "arquivo (verificação periódica)"; nenhuma das duas reaproveita terminal parado.

**Atualização automática.** Sem Marketplace nem conta: a extensão se atualiza a partir da cópia do repositório que os
agentes usam (a que recebe `git pull --ff-only` depois de cada merge). Ao abrir o VS Code e a cada 30 min ela compara a
versão de `extensions/vscode-team-terminals/package.json` dessa cópia com a instalada; se a da cópia for maior e o pacote
`extensions/vscode-team-terminals/dist/team-terminals.vsix` existir, instala o pacote e mostra "Team Terminals X.Y.Z
instalada: recarregar a janela", com o botão **Recarregar**. Os terminais persistentes voltam sozinhos depois do reload.
Versão igual ou menor, pasta ausente ou pacote faltando não fazem nada (só uma linha no registro). Até o reload, a
mesma versão não é instalada nem avisada de novo. A cópia é achada sozinha seguindo o link `~/.claude/skills/agent`
até a pasta que tem o pacote; `teamTerminals.updateSource` fixa outra pasta e `teamTerminals.autoUpdate` desliga.

O registro do que foi feito fica na saída **Team Terminals** (painel Output).

## agents.json

Fica em `~/.config/team/agents.json`. É a lista do time inteira: a extensão lê `name`, `enabled` e `order`; o launcher
`team` lê o resto (apelidos, pasta, skill, rotação), pelo `team_agents.py` do plugin agent
([`plugins/agent/skills/agent/scripts/team_agents.py`](../../plugins/agent/skills/agent/scripts/team_agents.py)).

```json
[
  { "name": "chief-of-staff", "enabled": true, "order": 10, "aliases": ["chief"], "cwd": "~/projetos", "skill": "chief-of-staff" },
  { "name": "meu-agente", "enabled": true, "order": 20, "aliases": ["meu"], "cwd": "~/src/meu", "skill": "agent meu-agente", "rotate": true },
  { "name": "outro-agente", "enabled": false, "order": 30 }
]
```

| Campo | Obrigatório | O que é |
|---|---|---|
| `name` | sim | nome aceito por `team <nome>`: letras minúsculas, dígitos e hífen |
| `enabled` | não (padrão `true`) | `false` = não abre; se o terminal estiver aberto, para o runner (plugin agent) e fecha. `team <nome>` continua abrindo |
| `order` | não (padrão: posição na lista x 10) | ordem de abertura, do menor para o maior; empate vai pelo nome |
| `aliases` | não | outros nomes que o `team` aceita (únicos no arquivo) |
| `cwd` | não (padrão: o `session.cwd` da instância, senão `~`) | pasta onde a sessão abre |
| `skill` | não (padrão: `agent <nome>` para instância do plugin agent, senão o nome) | o que a sessão roda ao abrir (`/<skill>`) |
| `rotate` | não (padrão: o `session.rotate` da instância, senão `false`) | sessão nova por dia e compactação mais cedo |

A extensão guarda os campos que não usa: o "Team: gerar agents.json" num arquivo que já existe só acrescenta os agentes
que faltam, sem mexer nos que já estão lá.

**Sem o arquivo**, a lista sai de `~/.config/team/*.session` mais as instâncias do plugin agent com `"live": true` em
`~/.config/agent/*/config/config.json`, todas habilitadas, em ordem alfabética. Nada é gravado pela extensão: o arquivo
nasce na primeira vez que o `team` roda sem ele, ou pelo comando "Team: gerar agents.json".

## Comandos

- **Team: abrir terminais**: confere tudo agora (abre os que faltam, reaproveita os parados, fecha os desabilitados).
- **Team: abrir terminal de um agente**: escolhe um agente da lista; se o terminal já existe, só mostra.
- **Team: gerar agents.json**: sem o arquivo, grava a lista gerada das sessões; com ele, acrescenta os agentes que faltam
  (pergunta antes). Depois abre para editar.

## Configuração

| Chave | Padrão | O que faz |
|---|---|---|
| `teamTerminals.autoOpen` | `true` | abrir ao iniciar o VS Code e acompanhar as mudanças do arquivo |
| `teamTerminals.pollSeconds` | `10` | além do watcher, conferir o `agents.json` a cada tantos segundos; `0` desliga |
| `teamTerminals.delaySeconds` | `3` | segundos entre uma abertura e a próxima |
| `teamTerminals.startupDelaySeconds` | `2` | espera antes da primeira conferência, para os terminais restaurados aparecerem |
| `teamTerminals.command` | `team ${agent}` | comando digitado no terminal; `${agent}` vira o nome |
| `teamTerminals.reviveIdle` | `true` | rodar o comando num terminal do agente restaurado e parado |
| `teamTerminals.autoUpdate` | `true` | ao abrir e a cada 30 min, instalar a versão maior que houver na cópia do repositório e oferecer recarregar |
| `teamTerminals.updateSource` | vazio | raiz da cópia do repositório de onde vem a atualização; vazio = a cópia para onde aponta `~/.claude/skills/agent` |
| `teamTerminals.agentRunner` | vazio | `runner.sh` do plugin agent para o stop; vazio = `~/.claude/skills/agent/scripts/runner.sh` |

## Instalar

O pacote fica versionado em `dist/team-terminals.vsix` (nome fixo, sem a versão). Depois da primeira instalação a
extensão se atualiza sozinha (ver Atualização automática).

1. Num terminal **da janela do VS Code conectada ao WSL** (assim a extensão vai para o lado do WSL, onde estão o `team` e o
   `~/.config`), na pasta desta extensão:

   ```bash
   code --install-extension ./dist/team-terminals.vsix
   ```

   Sem abrir a janela, pelo servidor do VS Code no WSL:
   `~/.vscode-server/bin/<commit>/bin/code-server --install-extension ./dist/team-terminals.vsix` (vale na próxima
   janela ou depois do "Developer: Reload Window").

2. Recarregar a janela (Ctrl+Shift+P, "Developer: Reload Window"). Na primeira vez ela já abre os terminais que faltam;
   para escolher quais, rode antes "Team: gerar agents.json", edite e salve.

**Versão nova (quem muda a extensão):** suba a `version` do `package.json`, escreva o CHANGELOG e gere o pacote de novo
na mesma PR (precisa de Node e acesso ao npm, nada é publicado):

```bash
npm run package        # mkdir -p dist && npx -y @vscode/vsce@4 package --no-dependencies -o dist/team-terminals.vsix
```

A CI confere que o `package.json`, o `extension.js` e o `lib/` dentro do pacote são iguais aos da pasta.

Desinstalar: `code --uninstall-extension team-agents.team-terminals`. Até a 0.2.0 o publisher tinha outro nome: quem instalou uma versão antiga desinstala a antiga (`code --list-extensions | grep team-terminals` mostra o id) antes de instalar esta.

## Testes

```bash
node --test test/*.test.js                                   # lógica (terminais e atualização automática), pastas temporárias
npm i --no-save @vscode/test-electron && xvfb-run -a node test/integration/run.js
```

Novidades de cada versão em [CHANGELOG.md](CHANGELOG.md).

O teste de integração baixa um VS Code próprio e roda com HOME temporário, comando e runner de mentira: não toca o
`~/.config` real nem abre `team` de verdade.
