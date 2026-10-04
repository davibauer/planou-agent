# Agente de segurança (comportamento `security-scan`, gancho `security`, instância `security`)

O agente de segurança varre, sem o modelo, o que pode virar incidente e abre uma tarefa no Planou com a evidência. Só
lê: nunca corrige, nunca gira chave, nunca apaga. As regras do papel estão em
[`behaviors/security-scan/BEHAVIOR.md`](../skills/agent/behaviors/security-scan/BEHAVIOR.md); as varreduras, em
[`scripts/hooks/security.py`](../skills/agent/scripts/hooks/security.py), que reaproveita o ciclo de tarefa do gancho
`health` (o do agente de operação).

- **Dependências** (`deps`): `npm audit --json --package-lock-only` em cada `package-lock.json` versionado,
  `dotnet list <sln> package --vulnerable --include-transitive --format json` e `pip-audit -r` quando estiver
  instalado. Nada é instalado nem restaurado; projeto sem restore sai como `nao rodou`. `min_severity` (padrão `high`),
  `ignore` para o risco aceito pela pessoa (id GHSA ou CVE).
- **Segredos** (`secrets`): os arquivos do `git ls-files` (versionados e não ignorados) e os globs de `logs`. A
  evidência é `<arquivo>:<linha>: <tipo> <máscara>`, com o prefixo fixo do tipo e o tamanho: o valor nunca é guardado
  nem mostrado. `.env`, arquivo de chave e o que está numa pasta `secrets/` versionada saem só pelo nome. Valor de
  exemplo (`test`, `fake`, `example`, `${...}`) e linha com `secret-scan: allow` não contam; teste e fixture vão no
  `ignore`.
- **Permissões** (`perms`): só `lstat` nos globs de `paths` e nas entradas das pastas (dois níveis). Pasta até 0700,
  arquivo até 0600, dono = o usuário do agente. O conteúdo nunca é aberto.
- **Agentes** (`agents`): o mesmo do `--validate` em cada instância: frase de `autonomy.can` que dá uma ação que nenhum
  comportamento ligado declara e ferramenta que nenhum usa.
- **Tarefas**: um achado abre uma tarefa por episódio no backlog do projeto (reporter = a instância, P1 segredo, P2
  dependência e permissão, P3 autonomia). O que muda é anotado na mesma tarefa (`-- MUDOU`), o fim também
  (`-- RESOLVIDO`), e quem fecha é a pessoa. Tarefa apagada não volta no mesmo episódio. Varredura que não roda nunca
  abre tarefa.
- **Frequência**: `every_h` por checagem, 24 h para `deps` e `secrets`, 1 h para `perms` e `agents`.

## Modelo de instância

Em [`config-example/security/`](../skills/agent/config-example/security/): `config.json` (em modo teste, sem
`"live": true`) e `instructions.md`. Copie para `~/.config/agent/security/config/` e troque os repositórios, os logs e
o projeto. Repositório de cliente só quando a pessoa listar.

## Passo a passo

1. `python3 $S/scripts/agent.py security --validate`: sem erro e sem aviso de autonomia.
2. `python3 $S/scripts/agent.py security --evals`: os casos do papel no modo seco.
3. `python3 $S/scripts/agent.py security --security`: todas as varreduras agora, nada gravado, e o que abriria.
4. `python3 $S/scripts/agent.py security --dry`: o tick seco, com `== SEGURANCA (teste, nada gravado)`.
5. Ligar é decisão da pessoa: a chave do agente no Planou (`secrets/planou.env`), `"live": true` e o runner. Em modo
   teste o tick é sempre seco e roda todas as varreduras a cada tick; por isso o runner fica desligado até ligar.
