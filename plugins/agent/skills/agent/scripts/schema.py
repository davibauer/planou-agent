#!/usr/bin/env python3
"""The config of an agent instance (config/config.json): reader with aliases and validation.

Keys are in English (schema 1). The Portuguese keys of work-watch are read as aliases, so an old config works as is:
fontes -> sources, ganchos -> hooks, intervalo_s -> interval_s, fuso_horas -> tz_hours, comercial -> business_hours,
sigla -> code, max_lembretes -> max_reminders, nova_tentativa_s -> retry_s; inside a source or hook entry, tipo -> type
and nome -> name. Both spellings stay in the normalized config, top level and entries: the work-watch adapters read
`sigla`, `ganchos`, `intervalo_s`, `tipo` and `nome`. The new key wins when both exist.

  schema           int, 1. A job-scout instance (behavior "job-scout") keeps job-scout's own 2 (the version of its data
                   schema, which its scripts check and rewrite): the same file, both readers
  live             bool; anything but true is test mode (the tick is dry, the runner refuses)
  language         str ("pt-BR")
  tz_hours         number, timezone offset (default -3)
  business_hours   [start, end], hours of the business day, Mon-Fri (default [8, 19])
  interval_s       int >= 60, the heavy tick (default 300). 0 (no heavy tick) is refused for now: the heavy tick is where
                   Planou answers get acknowledged
  session          {cwd, aliases, rotate}: where the `team` launcher opens the session
  workspace        str, the user's workspace ({instance} is replaced); CONTEXT.md there is read by the session
  videos_dir, archive_dir  str, recordings folders of this instance (win over the shared watch-core config)
  code             str, the short code of the agent (Notion section, Planou project fallback)
  idioma_pagina    str, "pt" | "en": the labels of the tasks hook (hooks/tarefas.py, default "pt")
  meetings_keywords  [str]: words that make a meeting this instance's (workspace.py: title x10, start of the transcript)
  meetings_include   [str]: recording stems "YYYY-MM-DD HH-MM-SS" forced into this instance (workspace.py)
  sources, hooks   [{type, ...options}]: adapters. Checked here: the alertas_azure hook's nao_acordar and
                   nao_acordar_junto (lists of names; a name in nao_acordar_junto that no hook has is a warning), ambiente
                   and fonte_clusters (text)
  behaviors        [str]: behaviors loaded by the session, in this order (behaviors/<name>/BEHAVIOR.md). An old name
                   in BEHAVIOR_ALIASES is read as the new one (deduplicated, first position kept) and --validate warns
  behavior_config  {behavior: {...}}: options per behavior; the options of the plugin's behaviors are in
                   BEHAVIOR_OPTIONS (unknown option: warning; wrong type: error). The options of an old name move to
                   the new one (the new one's own keys win)
  tools            [{kind, name, ...}]: kind in subagent, cli, skill, mcp, other. --validate warns about a tool that no
                   enabled behavior declares (permissions.py)
  autonomy         {can, ask_first, never}: lists of short sentences. --validate warns about a "can" phrase that gives
                   more than the enabled behaviors declare, or that matches no known action (permissions.py)
  repos            [{path, name, worktrees, base, rules, map, shared_rules, gh_account, tests, release, done, public,
                   fragments}]: the
                   repositories of a dev instance (dev-worker, batch-release; `agent.py <instance> --brief [repo]` turns
                   one into the worker brief). Only `path` is required: name (default the folder name), base (default
                   "main"), rules (the repo's rules file, relative to path), map (the repo's short map, relative to path;
                   default docs/MAP.md when it exists), shared_rules [str] (common rules files),
                   tests {filtered, full} (commands), release in REPO_RELEASE (batch: integrator and deploy; pr: merge
                   of the PR; ci: the PR goes with the `automerge` label and the CI merges, versions and tags it, so the
                   worker does not wait (PLN0259); none: the person merges), fragments (where the worker writes its
                   changelog text, e.g. plugins/<plugin>/changelog.d/<branch>.md), done (the done criterion of a dev worker), public (bool: a
                   public repository; code-review holds it to the no-client-data rule, --brief tells the worker; the old
                   behavior_config.code-review.public_repos still counts, see public_repos())
  planou           {project, confidentiality, drafts_in_planou, publish, task_queue, conversation, roles}; roles [str]
                   replaces the roles the heartbeat declares (Planou PLN0284, column owned by a role; default: from the
                   behaviors, watch_core.planou.ROLE_OF_BEHAVIOR; [] declares none): lower case letters, digits, - and
                   _, up to 40 characters, at most 10
  runtime          {python: system|venv, requirements}

  python3 schema.py <config.json>   prints the normalized config and the problems (exit 1 on errors)
"""
import json, os, re, sys

import permissions

ALIASES = {'fontes': 'sources', 'ganchos': 'hooks', 'intervalo_s': 'interval_s', 'fuso_horas': 'tz_hours',
           'comercial': 'business_hours', 'sigla': 'code', 'max_lembretes': 'max_reminders',
           'nova_tentativa_s': 'retry_s'}
ENTRY_ALIASES = {'tipo': 'type', 'nome': 'name'}
KNOWN = {'schema', 'live', 'language', 'tz_hours', 'business_hours', 'interval_s', 'session', 'workspace', 'code',
         'sources', 'hooks', 'behaviors', 'behavior_config', 'tools', 'autonomy', 'repos', 'planou', 'runtime',
         'max_reminders', 'agent', 'retry_s', 'videos_dir', 'archive_dir', 'idioma_pagina', 'meetings_keywords',
         'meetings_include', 'wake'}
PAGE_LANGUAGES = ('pt', 'en')
TOOL_KINDS = ('subagent', 'cli', 'skill', 'mcp', 'other')
AUTONOMY_KEYS = ('can', 'ask_first', 'never')
CONFIDENTIALITY = ('minimum', 'title', 'detail')
# the roles of "planou.roles" (Planou PLN0284): as Planou takes them in the heartbeat
ROLE_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,39}$')
ROLES_MAX = 10
PLANOU_BOOLS = ('drafts_in_planou', 'publish', 'task_queue', 'conversation', 'live')
DEFAULTS = {'schema': 1, 'tz_hours': -3, 'business_hours': [8, 19], 'interval_s': 300, 'sources': [], 'hooks': [],
            'behaviors': [], 'tools': []}
# old behavior name -> the one that replaced it; accepted for a while, --validate warns
BEHAVIOR_ALIASES = {'deploy-notice': 'batch-release'}
# options of the plugin's behaviors, documented in each BEHAVIOR.md: name -> 'str' | 'int' | 'num' | 'bool' | 'strs' |
# 'ints' | 'command' | 'path'. 'command' is a text that ends up run as (or spliced into) a shell command on the agent's
# machine: qa_env.py runs setup/env_up/env_down/env_url with the shell; dev_brief.py puts deploy_cmd and the locks,
# unquoted, in the command lines the integrator runs; code-review's BEHAVIOR.md puts terms_file in the review_check.py
# line (and it is the list the leak check uses). 'path' is a file or folder on the machine that decides what code runs
# or what the session writes or deletes there: qa.node_dir goes in NODE_PATH (node loads code from it); the integrator
# overwrites batch-release.e2e_marker with a date, deletes the used fragments in batch-release.fragments (and workers
# write <fragments>/<branch>.md) and appends a line to batch-release.deploy_log; prototype.node_dir goes in NODE_PATH
# like qa's. Both validate like 'str', but an edit from Planou's Papel tab may not change, add or remove one
# (role_edit.py, machine_changes): they are edited on the machine only (PLN0229, PLN0233), or whoever gets into Planou
# would run code or overwrite files here.
BEHAVIOR_OPTIONS = {
    'batch-release': {'repo': 'str', 'test_lock': 'command', 'deploy_lock': 'command', 'e2e_marker': 'path',
                      'e2e_every_h': 'num', 'fragments': 'path', 'deploy_log': 'path', 'check_url': 'str',
                      'deploy_cmd': 'command'},
    'daily-report': {'language': 'str', 'closing_hour': 'int', 'speech_only': 'bool', 'tasks': 'bool'},
    'recordings': {'calendar': 'str', 'query_hours': 'int', 'push': 'strs', 'language': 'str'},
    'push-alert': {'prefix': 'str', 'max_chars': 'int', 'triggers': 'strs', 'quiet': 'strs'},
    'code-review': {'max_diff_lines': 'int', 'terms_file': 'command', 'public_repos': 'strs', 'require_tests': 'bool',
                    'pr_comment': 'bool'},
    'product': {'max_tasks': 'int'},
    'process-coach': {'project': 'str', 'deploys_log': 'str', 'report_weekday': 'int', 'report_hour': 'int', 'skip': 'strs',
                      'dev_agent': 'str'},
    'qa': {'setup': 'command', 'env_up': 'command', 'env_down': 'command', 'env_url': 'command', 'url': 'str',
           'served_urls': 'strs',
           'node_dir': 'path', 'widths': 'ints', 'min_target_px': 'int', 'axe_tags': 'strs', 'up_timeout_s': 'int', 'pr_comment': 'bool',
           'send_back': 'str', 'video_width': 'int'},
    'prototype': {'node_dir': 'path', 'widths': 'ints', 'themes': 'strs', 'reference': 'str', 'tokens': 'str'},
    'tech-radar': {'project': 'str', 'column': 'str', 'per_task': 'int', 'max_hours': 'num', 'max_tasks': 'int',
                   'stacks': 'strs', 'weekday': 'int', 'hour': 'int', 'window_days': 'int'},
    'product-radar': {'project': 'str', 'max_ideas': 'int', 'terms': 'strs', 'weekday': 'int', 'hour': 'int',
                      'window_days': 'int', 'planou_repo': 'str', 'known_days': 'int'},
}
REPO_RELEASE = ('batch', 'pr', 'ci', 'none')
# job-scout (behaviors/job-scout): its data schema version and the criteria keys its scripts read (criteria.py KEYS)
JOB_SCOUT_SCHEMA = 2
JOB_SCOUT_KEYS = {'searches', 'include', 'exclude', 'points', 'min_score', 'boards', 'companies', 'logged_in_sources',
                  'pipeline_md', 'pay_ranges', 'skills', 'cv_profiles', 'cv_dir', 'interviews_dir', 'applications_dir',
                  'tick_hooks', 'daily_tasks', 'kpi_targets', 'shortlist_skip_process', 'saturation', 'test', 'mode'}
# travel-agent (behaviors/travel-agent): the keys of its config.json (trips, points, alert rules; config-example)
TRAVEL_AGENT_KEYS = {'currency', 'pause_seconds', 'min_drop_pct', 'rise_warn_pct', 'points', 'gmail', 'trips', 'fx',
                     'expedia_flights', 'calendar', 'dashboard', 'remind_days', 'miles_search_bonus', 'steps_hours', 'sheet'}
REPO_STR_KEYS = ('name', 'worktrees', 'base', 'rules', 'map', 'gh_account', 'done', 'fragments')
# options that take one of a few values: (behavior, option) -> the values
OPTION_CHOICES = {('qa', 'send_back'): ('ajuste', 'pessoa')}
# options that take free text of a given shape: (behavior, option) -> (pattern, what it looks like). recordings.language
# goes to run.sh as ATA_LANG (whisper's short code: "pt", "en", "es"), so a locale like "pt-BR" would break the run
OPTION_PATTERNS = {('recordings', 'language'): (re.compile(r'[a-z]{2,3}'), 'codigo curto do idioma, como pt, en ou es')}
OPTION_TYPES = {'str': 'texto', 'int': 'inteiro', 'num': 'numero', 'bool': 'true ou false', 'strs': 'lista de textos',
                'ints': 'lista de inteiros maiores que zero', 'command': 'texto', 'path': 'texto'}
OPTION_SHORT = {'str': 'texto', 'int': 'inteiro', 'num': 'numero', 'bool': 'true/false', 'strs': 'lista de textos',
                'ints': 'lista de inteiros', 'command': 'comando (só no computador)',
                'path': 'caminho (só no computador)'}
COMMAND, PATH = 'command', 'path'
MACHINE_ONLY = (COMMAND, PATH)          # option types edited on the machine only, never from Planou
TYPE_RE = re.compile(r'[a-z][a-z0-9_]{0,60}')
BEHAVIOR_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,60}')


class ConfigError(SystemExit):
    pass


def _entry(e):
    if not isinstance(e, dict): return e
    e = dict(e)
    for old, new in ENTRY_ALIASES.items():
        if new not in e and old in e: e[new] = e[old]
        if new in e: e[old] = e[new]
    return e


def normalize(raw):
    """Aliases resolved and defaults filled. The input is never changed."""
    if not isinstance(raw, dict): raise ConfigError('config.json precisa ser um objeto JSON')
    c = dict(raw)
    for old, new in ALIASES.items():
        if old in c:
            v = c.pop(old)
            c.setdefault(new, v)
    for k, v in DEFAULTS.items():
        if c.get(k) is None: c[k] = json.loads(json.dumps(v))
    for k in ('sources', 'hooks'):
        if isinstance(c[k], list): c[k] = [_entry(e) for e in c[k]]
    for old, new in ALIASES.items():                   # the old spelling mirrors the new one (same object)
        if new in c: c[old] = c[new]
    _behavior_aliases(c)
    return c


def _behavior_aliases(c):
    """Old behavior names -> the new ones, in "behaviors" and "behavior_config". The old names used are kept in
    c['_renamed_behaviors'] for the --validate warning. Never written back to config.json."""
    b, renamed = c.get('behaviors'), {}
    if isinstance(b, list):
        out = []
        for name in b:
            new = BEHAVIOR_ALIASES.get(name) if isinstance(name, str) else None
            if new: renamed[name] = new; name = new
            if name in out and (new or name in renamed.values()): continue   # both names listed: one entry
            out.append(name)
        c['behaviors'] = out
    bc = c.get('behavior_config')
    if isinstance(bc, dict):
        bc = dict(bc)
        for old, new in BEHAVIOR_ALIASES.items():
            if old in bc:
                renamed[old] = new
                opts = bc.pop(old)
                if isinstance(opts, dict) and isinstance(bc.get(new, {}), dict): bc[new] = {**opts, **(bc.get(new) or {})}
                elif new not in bc: bc[new] = opts
        c['behavior_config'] = bc
    if renamed: c['_renamed_behaviors'] = renamed


def option_problems(name, opts):
    """(errors, unknown) of one behavior's options against BEHAVIOR_OPTIONS: a wrong type or a value out of
    OPTION_CHOICES is an error; an option the behavior does not use is "unknown" (--validate warns; an edit from Planou's
    Papel tab refuses it). A behavior without a spec (its own options, or none) has no problems here."""
    spec, err, unknown = BEHAVIOR_OPTIONS.get(name), [], []
    if not spec or not isinstance(opts, dict): return err, unknown
    for k, v in opts.items():
        if k not in spec: unknown.append(f'"behavior_config.{name}.{k}": opcao que o comportamento nao usa ({", ".join(spec)})')
        elif not _option_ok(spec[k], v): err.append(f'"behavior_config.{name}.{k}" precisa ser {OPTION_TYPES[spec[k]]}')
        elif (name, k) in OPTION_CHOICES and v not in OPTION_CHOICES[name, k]:
            err.append(f'"behavior_config.{name}.{k}": {v!r} ({", ".join(OPTION_CHOICES[name, k])})')
        elif (name, k) in OPTION_PATTERNS and not OPTION_PATTERNS[name, k][0].fullmatch(v):
            err.append(f'"behavior_config.{name}.{k}": {v!r} ({OPTION_PATTERNS[name, k][1]})')
    return err, unknown


def machine_options(name, kinds=MACHINE_ONLY):
    """The options of a behavior edited on the machine only (types in `kinds`: 'command', 'path'), in the spec's order."""
    return [k for k, t in (BEHAVIOR_OPTIONS.get(name) or {}).items() if t in kinds]


def command_options(name):
    """The options of a behavior that are run as a command (type 'command'), in the spec's order."""
    return machine_options(name, (COMMAND,))


def option_type(name, key):
    """The type of one option in BEHAVIOR_OPTIONS, or None."""
    return (BEHAVIOR_OPTIONS.get(name) or {}).get(key)


def machine_changes(old, new):
    """"behavior.option" of each machine-only option (command or path) that differs between two behavior_config
    objects: changed, added or removed. Old behavior names are resolved first (BEHAVIOR_ALIASES), so an option under an
    old name counts as the new behavior's."""
    def resolved(bc):
        c = {'behavior_config': dict(bc) if isinstance(bc, dict) else {}}
        _behavior_aliases(c)
        return c['behavior_config']
    old, new, out = resolved(old), resolved(new), []
    for name in sorted(set(old) | set(new)):
        a, b = old.get(name), new.get(name)
        a, b = a if isinstance(a, dict) else {}, b if isinstance(b, dict) else {}
        for k in machine_options(name):
            if (k in a) != (k in b) or a.get(k) != b.get(k): out.append(f'{name}.{k}')
    return out


def command_changes(old, new):
    """machine_changes limited to the 'command' options."""
    return [o for o in machine_changes(old, new) if option_type(*o.split('.', 1)) == COMMAND]


def option_summary(name):
    """The options of a behavior in one short line for people, grouped by type ("texto: repo, check_url; numero:
    e2e_every_h"; an option with fixed values as "send_back: ajuste|pessoa"), or None without a spec. The machine-only
    options come first, as "comando (só no computador): ..." and "caminho (só no computador): ...", so a cut description
    never hides them."""
    spec = BEHAVIOR_OPTIONS.get(name)
    if not spec: return None
    groups, fixed = {}, []
    for kind in MACHINE_ONLY:
        ks = machine_options(name, (kind,))
        if ks: groups[OPTION_SHORT[kind]] = ks
    for k, t in spec.items():
        if t in MACHINE_ONLY: continue
        if (name, k) in OPTION_CHOICES: fixed.append(f'{k}: {"|".join(OPTION_CHOICES[name, k])}')
        else: groups.setdefault(OPTION_SHORT[t], []).append(k)
    return '; '.join([f'{t}: {", ".join(ks)}' for t, ks in groups.items()] + fixed)


# the options' schema as Planou's Papel tab takes it (catalog[].options, Planou 0.61.0): {key: {type, choices?, label?}}.
# Planou knows text, bool, int, choice and command (read only, "só no computador"); a 'path' goes as 'command' so it
# shows read only too; num, strs and ints go with the plugin's own name, which Planou ignores (the key stays as it is).
PLANOU_TYPES = {'str': 'text', 'int': 'int', 'bool': 'bool', COMMAND: 'command', PATH: 'command'}
PLANOU_MAX_OPTIONS, PLANOU_KEY_RE, PLANOU_LABEL_MAX, PLANOU_CHOICES_MAX, PLANOU_CHOICE_MAX = \
    30, re.compile(r'[A-Za-z0-9._:-]{1,60}'), 120, 30, 100
PLANOU_LABELS = {PATH: 'caminho'}


def planou_options(name):
    """The `options` of a behavior in Planou's catalog, in the spec's order, or None without a spec. Within Planou's
    limits (up to 30 options, key up to 60 of letters, digits and . _ : -, label up to 120, 1 to 30 choices of up to
    100): a key out of them is left out, choices out of them go as text."""
    spec = BEHAVIOR_OPTIONS.get(name)
    if not spec: return None
    out = {}
    for k, t in spec.items():
        if len(out) >= PLANOU_MAX_OPTIONS: break
        if not PLANOU_KEY_RE.fullmatch(k): continue
        o = {'type': PLANOU_TYPES.get(t, t)}
        ch = OPTION_CHOICES.get((name, k))
        if ch and t == 'str' and len(ch) <= PLANOU_CHOICES_MAX and all(0 < len(c) <= PLANOU_CHOICE_MAX for c in ch):
            o = {'type': 'choice', 'choices': list(ch)}
        if t in PLANOU_LABELS: o['label'] = f'{k} ({PLANOU_LABELS[t]})'[:PLANOU_LABEL_MAX]
        out[k] = o
    return out or None


def _option_ok(kind, v):
    if kind in ('str',) + MACHINE_ONLY: return isinstance(v, str)
    if kind == 'int': return _is_int(v)
    if kind == 'num': return isinstance(v, (int, float)) and not isinstance(v, bool)
    if kind == 'bool': return isinstance(v, bool)
    if kind == 'strs': return _strs(v)
    if kind == 'ints': return isinstance(v, list) and bool(v) and all(_is_int(x) and x > 0 for x in v)
    return True


def load(path):
    """config.json -> normalized dict. Missing or broken file: ConfigError with a message for the person."""
    try:
        with open(path, encoding='utf-8') as f: raw = json.load(f)
    except FileNotFoundError: raise ConfigError(f'sem config: {path}')
    except ValueError as e: raise ConfigError(f'{path} invalido: {e}')
    return normalize(raw)


def _qa_url_error(k, v):
    """qa: "url" is http(s); each "served_urls" is a regular expression that compiles. None when fine."""
    if k == 'url':
        return None if not isinstance(v, str) or re.match(r'https?://', v) else 'precisa comecar com http:// ou https://'
    for x in v if isinstance(v, list) else []:
        try: re.compile(x)
        except re.error as e: return f'expressao regular invalida {x!r} ({e})'
    return None


def _strs(v):
    return isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)


def _alert_hook(e, i, hooks, err, warn):
    """alertas_azure (PLN0160): rules that never wake the session and the hooks that go quiet with them."""
    at = f'"hooks"[{i}] (alertas_azure)'
    for k in ('nao_acordar', 'nao_acordar_junto'):
        if e.get(k) is not None and not _strs(e[k]): err.append(f'{at}: "{k}" precisa ser uma lista de nomes')
    for k in ('ambiente', 'fonte_clusters', 'fonte'):
        if e.get(k) is not None and not isinstance(e[k], str): err.append(f'{at}: "{k}" precisa ser texto')
    if _strs(e.get('nao_acordar_junto') or []):
        nomes = {h.get('nome') or h.get('type') for h in hooks if isinstance(h, dict)}
        for n in e.get('nao_acordar_junto') or []:
            if n.strip() not in nomes: warn.append(f'{at}: "nao_acordar_junto" cita "{n}", que nao e nome de nenhum gancho')
        if not e.get('nao_acordar'): warn.append(f'{at}: "nao_acordar_junto" sem "nao_acordar" nao faz nada')


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def repo_name(e):
    """The short name of a repos entry: "name", or the last folder of "path"."""
    n = e.get('name')
    return n if isinstance(n, str) and n.strip() else os.path.basename(os.path.normpath(e.get('path') or '')) or '?'


def public_repos(c):
    """The public repositories, as the code-review reads them: the path of every `repos` entry with "public": true, then
    each item of the old option behavior_config.code-review.public_repos (a path or owner/name) that is not one of them
    already (same path, or the name of that entry). Order kept, no repeats."""
    out, names = [], []
    for e in c.get('repos') or []:
        if isinstance(e, dict) and e.get('public') is True and isinstance(e.get('path'), str) \
                and not any(_same_path(e['path'], x) for x in out):
            out.append(e['path']); names.append(repo_name(e))
    bc = c.get('behavior_config') if isinstance(c.get('behavior_config'), dict) else {}
    cr = bc.get('code-review') if isinstance(bc.get('code-review'), dict) else {}
    old = cr.get('public_repos')
    for x in old if _strs(old) else []:
        if x in out or x in names or any(_same_path(x, y) for y in out): continue
        out.append(x)
    return out


def is_public(c, e):
    """True when the `repos` entry `e` is public: "public": true, or its path or name is in the old public_repos."""
    if not isinstance(e, dict): return False
    if e.get('public') is True: return True
    return any(x == repo_name(e) or _same_path(x, e.get('path')) for x in public_repos(c))


def _release_warnings(c, repos):
    """A repo with release "batch" must be the one batch-release publishes, and the deploy_log hook must read the log the
    integrator writes: warnings, since each piece still works alone."""
    w = []
    b = c.get('behaviors') if isinstance(c.get('behaviors'), list) else []
    br = ((c.get('behavior_config') or {}).get('batch-release') or {}) if isinstance(c.get('behavior_config'), dict) else {}
    batch = [e for e in repos if isinstance(e, dict) and e.get('release') == 'batch']
    if batch and 'batch-release' not in b:
        w.append(f'"repos": "{repo_name(batch[0])}" tem "release": "batch", mas "batch-release" nao esta em "behaviors"')
    if len(batch) > 1:
        w.append('"repos": mais de um repositorio com "release": "batch" (o batch-release publica um so, o do "repo")')
    br_repo = br.get('repo') if isinstance(br, dict) else None
    for e in batch:
        if isinstance(br_repo, str) and _same_path(br_repo, e.get('path')) is False:
            w.append(f'"repos": "{repo_name(e)}" tem "release": "batch", mas "behavior_config.batch-release.repo" e {br_repo!r}')
    log = br.get('deploy_log') if isinstance(br, dict) else None
    for h in c.get('hooks') or []:
        if isinstance(h, dict) and h.get('type') == 'deploy_log' and isinstance(log, str) and isinstance(h.get('path'), str) \
                and _same_path(h['path'], log) is False:
            w.append(f'gancho "deploy_log" le {h["path"]!r}, mas "behavior_config.batch-release.deploy_log" e {log!r}')
    return w


def _same_path(a, b):
    if not isinstance(a, str) or not isinstance(b, str): return None
    norm = lambda x: os.path.normpath(x.replace('~', '<home>', 1) if x.startswith('~') else x)
    return norm(a) == norm(b)


def validate(c, behavior_file=None, adapter_exists=None):
    """(errors, warnings) of a normalized config. `behavior_file(name)` -> path or None and `adapter_exists(type)` ->
    bool check that what the config names exists; without them only the shape is checked."""
    err, warn = [], []
    job_scout = isinstance(c.get('behaviors'), list) and 'job-scout' in c['behaviors']
    if c.get('schema') != 1 and not (job_scout and c.get('schema') == JOB_SCOUT_SCHEMA):
        err.append(f'"schema": {c.get("schema")!r} (o formato conhecido e 1)')
    if 'live' in c and not isinstance(c['live'], bool): err.append('"live" precisa ser true ou false')
    if 'language' in c and not isinstance(c['language'], str): err.append('"language" precisa ser texto')
    tz = c.get('tz_hours')
    if not isinstance(tz, (int, float)) or isinstance(tz, bool) or not -12 <= tz <= 14: err.append(f'"tz_hours": {tz!r} (-12 a 14)')
    bh = c.get('business_hours')
    if not (isinstance(bh, list) and len(bh) == 2 and all(_is_int(x) for x in bh) and 0 <= bh[0] < bh[1] <= 24):
        err.append(f'"business_hours": {bh!r} ([inicio, fim], horas de 0 a 24)')
    iv = c.get('interval_s')
    if iv == 0: err.append('"interval_s": 0 (sem tick pesado) ainda nao existe: use 60 ou mais')
    elif not _is_int(iv) or iv < 60: err.append(f'"interval_s": {iv!r} (inteiro, 60 ou mais)')
    s = c.get('session')
    if s is not None:
        if not isinstance(s, dict): err.append('"session" precisa ser um objeto {cwd, aliases, rotate}')
        else:
            if 'cwd' in s and not isinstance(s['cwd'], str): err.append('"session.cwd" precisa ser texto')
            if 'aliases' in s and not _strs(s['aliases']): err.append('"session.aliases" precisa ser uma lista de textos')
            if 'rotate' in s and not isinstance(s['rotate'], bool): err.append('"session.rotate" precisa ser true ou false')
    for k in ('workspace', 'code', 'videos_dir', 'archive_dir'):
        if k in c and c[k] is not None and not isinstance(c[k], str): err.append(f'"{k}" precisa ser texto')
    ip = c.get('idioma_pagina')
    if ip is not None and (not isinstance(ip, str) or ip.lower() not in PAGE_LANGUAGES):
        err.append(f'"idioma_pagina": {ip!r} ({", ".join(PAGE_LANGUAGES)})')
    for k in ('meetings_keywords', 'meetings_include'):
        if c.get(k) is not None and not _strs(c[k]): err.append(f'"{k}" precisa ser uma lista de textos')
    if c.get('wake') is not None:                # PLN0247: blocks that wait for the next wake (wake.py)
        import wake
        err += wake.problems(c['wake'])
    for k in ('sources', 'hooks'):
        v = c.get(k)
        if not isinstance(v, list): err.append(f'"{k}" precisa ser uma lista'); continue
        for i, e in enumerate(v):
            t = e.get('type') if isinstance(e, dict) else None
            if not isinstance(t, str) or not TYPE_RE.fullmatch(t):
                err.append(f'"{k}"[{i}]: precisa de "type" (letras minusculas, digitos e _)'); continue
            if adapter_exists and not adapter_exists(t): err.append(f'"{k}"[{i}]: tipo "{t}" nao existe (nem no plugin nem em adapters/ da instancia)')
            if k == 'hooks' and t == 'alertas_azure': _alert_hook(e, i, v, err, warn)
    b = c.get('behaviors')
    if not isinstance(b, list): err.append('"behaviors" precisa ser uma lista de nomes')
    else:
        seen = set()
        for name in b:
            if not isinstance(name, str) or not BEHAVIOR_RE.fullmatch(name): err.append(f'"behaviors": nome invalido {name!r}'); continue
            if name in seen: err.append(f'"behaviors": "{name}" repetido')
            seen.add(name)
            if behavior_file and not behavior_file(name): err.append(f'"behaviors": "{name}" nao existe (behaviors/{name}/BEHAVIOR.md)')
    bc = c.get('behavior_config')
    if bc is not None:
        if not isinstance(bc, dict) or not all(isinstance(v, dict) for v in bc.values()):
            err.append('"behavior_config" precisa ser {comportamento: {opcoes}}')
        elif isinstance(b, list):
            for name, opts in bc.items():
                if name not in b: warn.append(f'"behavior_config": "{name}" nao esta em "behaviors"')
                bad, unknown = option_problems(name, opts)
                err += bad
                warn += unknown
                for k in ('url', 'served_urls') if name == 'qa' else ():
                    why = _qa_url_error(k, opts.get(k))
                    if why: err.append(f'"behavior_config.qa.{k}": {why}')
                vw = opts.get('video_width') if name == 'qa' else None
                if _is_int(vw) and vw < 0: err.append('"behavior_config.qa.video_width": precisa ser 0 (sem video) ou uma largura')
                elif _is_int(vw) and vw > 0 and vw not in (opts.get('widths') or [1360, 834, 390]):
                    warn.append(f'"behavior_config.qa.video_width": {vw} nao esta em "widths"; o QA nao grava video')
                if name == 'qa' and opts.get('env_url') and opts.get('url'):
                    warn.append('"behavior_config.qa": "env_url" e "url" juntos; vale o "env_url" (o "url" fica sem efeito)')
    same = [x for x in ('code-review', 'qa') if isinstance(b, list) and x in b and 'dev-worker' in b]
    if same:
        warn.append(f'"behaviors": "dev-worker" junto de {" e ".join(chr(34) + x + chr(34) for x in same)}: modo mesma instancia '
                    '(PLN0281). A revisao/QA da tarefa que esta instancia desenvolveu roda num worker NOVO, de contexto limpo '
                    '(nunca o worker que entregou nem continuacao dele), e o ajuste volta como retrabalho; as vagas de '
                    'revisao/QA contam no mesmo "Ao mesmo tempo" da aba Fila. Com instancias separadas por papel, tire-os daqui')
    if isinstance(b, list) and 'qa' in b and not ((bc if isinstance(bc, dict) else {}).get('qa') or {}).get('env_up'):
        warn.append('"behavior_config.qa.env_up" vazio: o qa nao sobe ambiente de teste (scripts/qa_env.py recusa)')
    for old, new in sorted((c.get('_renamed_behaviors') or {}).items()):
        warn.append(f'comportamento "{old}" agora faz parte de "{new}" (o nome antigo ainda vale por um tempo): '
                    f'trocar "{old}" por "{new}" em "behaviors" e em "behavior_config"')
    t = c.get('tools')
    if not isinstance(t, list): err.append('"tools" precisa ser uma lista')
    else:
        for i, e in enumerate(t):
            if not isinstance(e, dict) or e.get('kind') not in TOOL_KINDS or not isinstance(e.get('name'), str) or not e['name'].strip():
                err.append(f'"tools"[{i}]: precisa de "kind" ({", ".join(TOOL_KINDS)}) e "name"')
            elif 'env' in e and not isinstance(e['env'], dict): err.append(f'"tools"[{i}].env precisa ser um objeto')
    a = c.get('autonomy')
    if a is not None:
        if not isinstance(a, dict): err.append('"autonomy" precisa ser {can, ask_first, never}')
        else:
            for k, v in a.items():
                if k not in AUTONOMY_KEYS: err.append(f'"autonomy.{k}": chave desconhecida ({", ".join(AUTONOMY_KEYS)})')
                elif not _strs(v): err.append(f'"autonomy.{k}" precisa ser uma lista de frases curtas')
    r = c.get('repos')
    if r is not None:
        if not isinstance(r, list): err.append('"repos" precisa ser uma lista')
        else:
            for i, e in enumerate(r):
                if not isinstance(e, dict) or not isinstance(e.get('path'), str):
                    err.append(f'"repos"[{i}]: precisa de "path"'); continue
                for k in REPO_STR_KEYS:
                    if k in e and not isinstance(e[k], str): err.append(f'"repos"[{i}].{k} precisa ser texto')
                if 'shared_rules' in e and not _strs(e['shared_rules']): err.append(f'"repos"[{i}].shared_rules precisa ser uma lista de textos')
                tt = e.get('tests')
                if tt is not None and not (isinstance(tt, dict) and all(k in ('filtered', 'full') and isinstance(v, str) for k, v in tt.items())):
                    err.append(f'"repos"[{i}].tests precisa ser {{"filtered": "<comando>", "full": "<comando>"}}')
                if 'public' in e and not isinstance(e['public'], bool): err.append(f'"repos"[{i}].public precisa ser true ou false')
                if 'release' in e and e['release'] not in REPO_RELEASE:
                    err.append(f'"repos"[{i}].release: {e["release"]!r} ({", ".join(REPO_RELEASE)})')
            names = [repo_name(e) for e in r if isinstance(e, dict) and isinstance(e.get('path'), str)]
            for n in sorted({n for n in names if names.count(n) > 1}):
                err.append(f'"repos": nome "{n}" repetido (use "name" para diferenciar)')
            warn += _release_warnings(c, r)
    p = c.get('planou')
    if p is not None:
        if not isinstance(p, dict): err.append('"planou" precisa ser um objeto')
        else:
            if 'project' in p and not isinstance(p['project'], str): err.append('"planou.project" precisa ser texto')
            if 'confidentiality' in p and p['confidentiality'] not in CONFIDENTIALITY:
                err.append(f'"planou.confidentiality": {p["confidentiality"]!r} ({", ".join(CONFIDENTIALITY)})')
            for k in PLANOU_BOOLS:
                if k in p and not isinstance(p[k], bool): err.append(f'"planou.{k}" precisa ser true ou false')
            if 'roles' in p:
                rr = p['roles']
                if not isinstance(rr, list): err.append('"planou.roles" precisa ser uma lista de papeis (ex.: ["dev", "code-review"])')
                else:
                    for x in rr:
                        if not isinstance(x, str) or not ROLE_RE.fullmatch(x):
                            err.append(f'"planou.roles": papel invalido {x!r} (minusculas, numeros, - e _, ate 40 caracteres)')
                    if len(rr) > ROLES_MAX: err.append(f'"planou.roles": no maximo {ROLES_MAX} papeis')
                    dup = sorted({x for x in rr if isinstance(x, str) and rr.count(x) > 1})
                    if dup: warn.append(f'"planou.roles": repetido {", ".join(dup)}')
            if p.get('task_queue') is True and c.get('live') is not True:
                warn.append('"planou.task_queue" so vale com "live": true (em modo teste a fila fica desligada)')
    rt = c.get('runtime')
    if rt is not None:
        if not isinstance(rt, dict) or rt.get('python', 'system') not in ('system', 'venv'):
            err.append('"runtime" precisa ser {"python": "system"|"venv", "requirements": ...}')
    warn += permissions.warnings(c, behavior_file)
    travel = isinstance(c.get('behaviors'), list) and 'travel-agent' in c['behaviors']
    unknown = sorted(k for k in c if k not in KNOWN and k not in ALIASES and not k.startswith('_')
                     and not (job_scout and k in JOB_SCOUT_KEYS) and not (travel and k in TRAVEL_AGENT_KEYS))
    if travel and _is_int(iv) and iv < 10800:
        warn.append(f'"interval_s": {iv} no travel-agent vira 10800 (cada tick e uma grade inteira de buscas)')
    if job_scout and _is_int(iv) and iv < 1800:
        warn.append(f'"interval_s": {iv} no job-scout vira 1800 (a conta do LinkedIn e pessoal; o site detecta automacao)')
    if unknown: warn.append('chaves que o nucleo nao usa (ficam para os adapters): ' + ', '.join(unknown))
    return err, warn


if __name__ == '__main__':
    if len(sys.argv) != 2: sys.exit('uso: schema.py <config.json>')
    cfg = load(sys.argv[1])
    e, w = validate(cfg)
    print(json.dumps(cfg, ensure_ascii=False, indent=1))
    for x in w: print('aviso:', x)
    for x in e: print('ERRO:', x)
    sys.exit(1 if e else 0)
