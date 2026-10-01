# Changelog

## 0.3.0 (2026-09-30)
- Atualização automática sem Marketplace nem conta: ao abrir o VS Code e a cada 30 min, a extensão compara a versão da
  cópia do repositório que os agentes usam (achada pelo link `~/.claude/skills/agent`) com a instalada; se for maior,
  instala `extensions/vscode-team-terminals/dist/team-terminals.vsix` e mostra "Team Terminals X.Y.Z instalada:
  recarregar a janela", com o botão Recarregar. `teamTerminals.autoUpdate` (ligado) desliga e
  `teamTerminals.updateSource` fixa outra pasta. O pacote agora fica versionado em `dist/`, e a CI roda os testes da
  extensão e confere que o pacote bate com o código. PLN0218

## 0.2.2 (2026-09-30)
- O watcher do VS Code perdia a mudança do `agents.json` (a pasta `~/.config/team` fica fora do workspace): o
  funcionário ligado pelo app não ganhava terminal. Agora a extensão também confere a data, o tamanho e o inode do
  arquivo a cada `teamTerminals.pollSeconds` (10 s; `0` desliga) e reconcilia quando mudam, com a mesma espera de
  700 ms do watcher e sem reconciliar duas vezes a mesma mudança. Só com `teamTerminals.autoOpen` ligado; no registro
  aparece como "arquivo (verificação periódica)". PLN0217

Versões até a 0.2.1: histórico do git desta pasta.
