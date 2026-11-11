#!/usr/bin/env python3
"""The autonomy an instance follows now: its config's, under the current autonomy of its Planou project (PLN0744).

The project decides (PLN0534): provisioner.render writes the config's "can" and "ask_first" once, from the autonomy the
default project had the day the instance was created (AUTONOMY below). When the person changes the project's autonomy
on Planou later, the session and the worker still read those lists: a project made Autonomo kept asking before opening
a PR, a project made Semi kept asking before branch and push. The config is never rewritten here; it is read through
the project's current autonomy instead:

- effective(cfg, level, born): pure. `born` is the level the instance was created with (the provisioner writes it in
  the instance's instructions.md as "- Autonomia no Planou: <level>", PLN0111). A config whose "can" and "ask_first"
  are still exactly born's table (as render wrote them, with the same batch-release flag) follows `level`: both lists
  become that level's table, looser or stricter. Equal to another level's table is not untouched: a config made
  Autonomo and tightened by hand until it reads like Semi stays tightened. An edited config, or one without `born`
  (written by hand), is the person's: it only gets stricter (the phrases the level puts in "ask_first" leave "can"),
  never looser. Either way a phrase in "never" leaves "can" and "ask_first" (never wins, the brief never shows it as
  allowed), also when Planou did not answer (without_never: it only tightens). "never" and any other key stay. Unknown
  level or no "autonomy": the config as it is, but for that. A non-text item in the lists never raises (the config
  counts as edited).
  Not covered: a line of the person's own in "can" that no table names (e.g. "merge da PR") stays in "can" whatever
  the project's level; the task's own autonomy (`fila ver`) still holds it back.
- born(path): the creation level written in that instructions.md, or None (no file, no line, an unknown value).
- level(): the current autonomy of the configured project (planou_release.autonomy, cached 10 min), or None when the
  instance does not talk to Planou or Planou did not answer.
- current(cfg): effective(cfg, level(), born(paths.INSTRUCTIONS)); never raises (any failure is the config as it is,
  with never still out of the lists).

Readers: dev_brief (PODE / PERGUNTAR ANTES of the worker brief) and agent.py `_autonomy` (the rules layer of `--load`
and the autonomy sent with the Papel tab). `--validate` and permissions.py keep checking the config as written.
Each task still carries its own autonomy from Planou (`fila ver`); the stricter of the two wins, as before.
"""
import re

AUTONOMY = {
    'autonomous': {'can': ['branch e worktree', 'testes filtrados', 'push de branch', 'abrir PR'],
                   'ask_first': ['dependencia nova', 'migration destrutiva ou que apaga dado']},
    'semi_autonomous': {'can': ['branch e worktree', 'testes filtrados', 'push de branch'],
                        'ask_first': ['abrir PR', 'dependencia nova', 'migration destrutiva ou que apaga dado']},
    'manual': {'can': ['testes filtrados'],
               'ask_first': ['branch e worktree', 'push de branch', 'abrir PR', 'dependencia nova',
                             'migration destrutiva ou que apaga dado']},
}
# Only an autonomous project lets a batch release go alone (planou_release.limit checks it again at `start`, PLN0566).
RELEASE_CAN = ['release em lote pelo integrador (fast-forward na main, tag, push)',
               'deploy com trava e linha no log de deploys']


def table(level, batch=False):
    """(can, ask_first) of a project autonomy level, as render writes them; an unknown level is semi_autonomous."""
    auto = AUTONOMY.get(level, AUTONOMY['semi_autonomous'])
    can, ask = list(auto['can']), list(auto['ask_first'])
    if batch: (can if level == 'autonomous' else ask).extend(RELEASE_CAN)
    return can, ask


def without_never(a):
    """`a` with the phrases of its "never" out of "can" and "ask_first" (never wins); `a` itself when nothing changes.
    Only ever stricter, so it also runs when Planou did not answer."""
    if not isinstance(a, dict): return a
    never = {x for x in a.get('never') or [] if isinstance(x, str)}
    out = dict(a)
    for k in ('can', 'ask_first'):
        v = a.get(k)
        if isinstance(v, list) and any(isinstance(x, str) and x in never for x in v):
            out[k] = [x for x in v if not (isinstance(x, str) and x in never)]
    return a if out == a else out


def effective(cfg, level, born=None):
    """The config's "autonomy" under the project level `level`, for an instance created with `born` (see the module
    doc). Returns the dict to read, or the config's own value (possibly None) when there is nothing to apply."""
    a = cfg.get('autonomy') if isinstance(cfg, dict) and isinstance(cfg.get('autonomy'), dict) else None
    if not a or level not in AUTONOMY: return without_never(a)
    batch = 'batch-release' in (cfg.get('behaviors') or [])
    can, ask = list(a.get('can') or []), list(a.get('ask_first') or [])
    texts = all(isinstance(x, str) for x in can + ask)           # a non-text item: never untouched, never raises
    if texts and born in AUTONOMY and (set(can), set(ask)) == tuple(set(x) for x in table(born, batch)):
        can, ask = table(level, batch)                            # untouched since render: the project decides
    else:
        held = [x for x in can if x in table(level, batch)[1]]
        can, ask = [x for x in can if x not in held], ask + [x for x in held if x not in ask]
    if can == list(a.get('can') or []) and ask == list(a.get('ask_first') or []): return without_never(a)
    return without_never({**a, 'can': can, 'ask_first': ask})


BORN_RE = re.compile(r'^- Autonomia no Planou: (\S+)[ \t]*$', re.M)


def born(path):
    """The autonomy the instance was created with, from its instructions.md (provisioner.render), or None."""
    try:
        with open(path, encoding='utf-8') as f: found = set(BORN_RE.findall(f.read()))
    except (OSError, TypeError, ValueError):
        return None
    v = found.pop() if len(found) == 1 else None
    return v if v in AUTONOMY else None


def level(now=None):
    try:
        import planou_release
        return planou_release.autonomy(now=now)
    except Exception:
        return None


def current(cfg, now=None):
    try:
        import paths
        return effective(cfg, level(now), born(getattr(paths, 'INSTRUCTIONS', None)))
    except Exception:
        try: return without_never(cfg.get('autonomy') if isinstance(cfg, dict) else None)
        except Exception: return cfg.get('autonomy') if isinstance(cfg, dict) else None
