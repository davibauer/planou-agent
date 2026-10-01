# planou-agent

O agente do [Planou](https://app.planou.com) para o Claude Code: o plugin `agent` (runner, fila, conversa e papéis de
cada funcionário), a extensão Team Terminals do VS Code e o provisionador local, que liga este computador ao Planou.

## Instalar

O jeito normal é pelo Planou: **Time › Adicionar funcionário › Outro computador** gera um comando que instala tudo
por aqui e registra o funcionário neste computador.

Sem o Planou guiando, um comando instala o agente e no fim pede o código de pareamento (Configurações › Computadores ›
**Conectar este computador**):

```bash
curl -fsSL https://raw.githubusercontent.com/davibauer/planou-agent/main/install.sh | sh    # Linux, WSL e macOS
```

```powershell
irm https://raw.githubusercontent.com/davibauer/planou-agent/main/install.ps1 | iex          # Windows (pelo WSL)
```

Rodar de novo atualiza. Não pede login no GitHub nem conta nova; o código de pareamento é digitado oculto.

## Versões

Cada versão é um commit da `main`, uma tag `vX.Y.Z` e uma Release com o pacote (`planou-agent-vX.Y.Z.tar.gz`) e o
SHA-256 dele (`.sha256`). O que mudou em cada uma está em
[plugins/agent/CHANGELOG.md](plugins/agent/CHANGELOG.md).

## Desenvolvimento

Este repositório só recebe o que é publicado: o agente é desenvolvido em outro lugar e cada versão chega aqui pronta.
Por isso não aceitamos pull request. Problemas e sugestões: relate pelo Planou (https://app.planou.com).

Versão publicada: 0.74.0.
