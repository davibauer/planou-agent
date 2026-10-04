"""Suggestions for Planou and the plugins (PLN0007): any agent that runs into a limit or a defect of Planou or of the team
plugins leaves a suggestion; the planou agent turns it into a task of the Planou project, in backlog, reported by the
agent that suggested it.

Why an inbox file and not a direct sync to the Planou project: the /v1 sync would accept `project: "PLN"` from any agent
of the workspace, but a sync only touches the caller's own source keys (`<agent>:...`). One agent could never add the
"+1" to a suggestion another agent made, so the same limit reported by three agents would become three tasks. A single
writer (planou) dedupes across agents, is the one who follows the tasks ("quem acompanha") and registers each one in
the name of whoever suggested it (`reporter`, Planou 0.31.0, made for this). `assignee` cannot name the planou agent from
the sync (only `self` or `person`, and `self` is ignored in backlog): the task stays with the person until approved.

  write side (any agent, no Planou key needed):
    python3 -m watch_core.planou --agent <agent> sugestao --title "..." --what "..." --expected "..." --example "..."
        [--subject "planou: pedidos"] [--priority P1..P4]
  appends one JSON line to ~/.config/agent/<target>/data/suggestions.jsonl (under flock), after these checks:
    - at most PER_DAY (3) suggestions per agent per local day (the 4th is refused);
    - the same title and subject from the same agent on the same day is a duplicate (not written, not counted);
    - no client data: emails, links (other than Planou's), secrets and the client names this machine knows (every
      work-watch instance name, org/site/tenant/workspace/domain of the agent's own sources) are refused ("generalize").
  read side: the planou hook `suggestions` (plugins/agent hooks/suggestions.py) reads the new lines on each heavy
  tick, enforces the same daily limit, and creates or "+1"s the task.

The target (PLN0282, the planou-dev instance renamed to planou): the first of
    - the environment variable AGENT_SUGGESTIONS_TARGET;
    - "suggestions_target" in the writer's own config (an instance name);
    - TARGET ("planou") when ~/.config/agent/planou exists;
    - the old name, LEGACY_TARGETS ("planou-dev"), when only that folder exists (a machine not yet renamed; after the
      rename ~/.config/agent/planou-dev may stay as a link to planou, the same folder).
  Only folders under ~/.config/agent count: ~/.config/planou is Planou's own folder (locks, deploy log), never an inbox.
  A configured target missing from this machine falls back to the default ones. None found: the suggestion is refused.
  The reader needs nothing: the hook reads its own instance's data/suggestions.jsonl.

The dedupe key is the normalized subject plus the normalized title; the task's source key is derived from it, so a resend
is `unchanged` and a suggestion the person deleted stays deleted.
"""
import fcntl
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

from . import config

VERSION = '0.1.0'
TARGET = 'planou'
LEGACY_TARGETS = ('planou-dev',)
TARGET_ENV = 'AGENT_SUGGESTIONS_TARGET'
TARGET_KEY = 'suggestions_target'
NAME_RE = re.compile(r'[a-z0-9][a-z0-9_-]{0,60}')
INBOX_REL = os.path.join('data', 'suggestions.jsonl')
PER_DAY = 3
LIMITS = {'title': 200, 'subject': 120, 'what': 1500, 'expected': 1500, 'example': 1500}
PRIORITIES = {'1': 1, '2': 2, '3': 3, '4': 4, 'p1': 1, 'p2': 2, 'p3': 3, 'p4': 4, 'urgente': 1, 'alta': 2, 'media': 3,
              'média': 3, 'baixa': 4}
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
URL_RE = re.compile(r'https?://([^/\s]+)', re.I)
ALLOWED_HOSTS = ('planou.com',)
# keys of a source (or of the instance config) whose value names the client
CLIENT_KEYS = ('org', 'organization', 'site', 'tenant', 'workspace', 'domain', 'group', 'company', 'empresa', 'cliente',
               'client', 'account', 'namespace', 'host')


class SuggestionError(Exception):
    pass


def norm(text):
    t = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode().lower()
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', t).split())


def key_of(title, subject=None):
    return hashlib.sha256(f'{norm(subject)}|{norm(title)}'.encode()).hexdigest()[:16]


def target_candidates(agent=None, cfg=None):
    """The targets to try, in order: the env var, the writer's "suggestions_target", then TARGET and LEGACY_TARGETS."""
    if cfg is None: cfg = _agent_config(agent) if agent else {}
    out = []
    for v in (os.environ.get(TARGET_ENV), (cfg or {}).get(TARGET_KEY), TARGET, *LEGACY_TARGETS):
        v = str(v or '').strip()
        if v and NAME_RE.fullmatch(v) and v not in out: out.append(v)
    return out


def target_root(agent=None, cfg=None, target=None):
    """(name, folder) of the instance that receives the suggestions: the first candidate (or `target`, when given) with
    a folder under ~/.config/agent. (None, None) when none exists here."""
    names = [target] if target else target_candidates(agent, cfg)
    for name in names:
        root = os.path.join(config.agent_base(), name)
        if os.path.isdir(root): return name, root
    return None, None


def inbox_path(target=None, agent=None):
    """The inbox file of the target (resolved as in target_root); None when no target exists on this machine."""
    _, root = target_root(agent, target=target)
    return os.path.join(root, INBOX_REL) if root else None


def priority(value):
    if value in (None, ''): return 3
    p = PRIORITIES.get(str(value).strip().lower())
    if p is None: raise SuggestionError(f'prioridade {value!r}: use P1, P2, P3 ou P4')
    return p


def _agent_config(agent):
    root = config.agent_root(agent)
    for rel in ('config/config.json', 'config.json'):
        try:
            with open(os.path.join(root, rel), encoding='utf-8') as f:
                c = json.load(f)
            return c if isinstance(c, dict) else {}
        except (OSError, ValueError):
            continue
    return {}


def client_terms(agent, cfg=None):
    """Names the agent's own config knows as the client's: the part after `work-watch-` and the values of org/site/...
    in its sources. Short or generic values (under 4 characters) are skipped."""
    cfg = _agent_config(agent) if cfg is None else cfg
    terms = set()
    if str(agent).startswith('work-watch-'): terms.add(agent[len('work-watch-'):])

    def walk(v, depth=0):
        if depth > 4: return
        if isinstance(v, dict):
            for k, x in v.items():
                if k in CLIENT_KEYS and isinstance(x, str): terms.update(p for p in re.split(r'[./\s]', x) if p)
                else: walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v: walk(x, depth + 1)
    walk(cfg.get('sources') or cfg.get('fontes') or [])
    terms.update(instance_names())
    generic = {'com', 'www', 'net', 'org', 'http', 'https', 'atlassian', 'slack', 'github', 'gitlab', 'azure', 'visualstudio',
               'microsoft', 'google', 'teams', 'outlook', 'office', 'dev', 'prod', 'default'}
    return sorted(t for t in {norm(x) for x in terms} if len(t) >= 4 and t not in generic)


def instance_names():
    """Every work-watch instance on this machine is named after a client: ~/.config/agent/work-watch-<x> and
    ~/.config/work-watch/<x>. Any agent (the weekly retro reads them all) must not write those names."""
    out = set()
    base = config.agent_base()
    try: out.update(n[len('work-watch-'):] for n in os.listdir(base) if n.startswith('work-watch-'))
    except OSError: pass
    legacy = os.path.dirname(config.legacy_agent_root('work-watch-x'))
    try: out.update(n for n in os.listdir(legacy) if os.path.isdir(os.path.join(legacy, n)) and not n.startswith('.'))
    except OSError: pass
    return out


def _looks_secret(text):
    try:
        from .planou import looks_secret
        return looks_secret(text)
    except Exception:
        return False


def check_text(agent, fields, cfg=None):
    """Refuses (SuggestionError) text with client data. The message says what to generalize, never echoes a secret."""
    terms = client_terms(agent, cfg)
    for name, value in fields.items():
        if not value: continue
        if EMAIL_RE.search(value): raise SuggestionError(f'{name}: tem um e-mail; generalize (ex.: "uma pessoa do cliente")')
        for host in URL_RE.findall(value):
            if not any(host.lower() == h or host.lower().endswith('.' + h) for h in ALLOWED_HOSTS):
                raise SuggestionError(f'{name}: tem um link ({host}); generalize, sem link de cliente ou de conversa')
        if _looks_secret(value): raise SuggestionError(f'{name}: parece ter um segredo (token, chave); tire e generalize')
        words = f' {norm(value)} '
        for t in terms:
            if f' {t} ' in words: raise SuggestionError(f'{name}: cita "{t}", nome do cliente desta instancia; generalize')


def _test_mode(agent, cfg):
    strict = str(agent).startswith('work-watch-') or config.is_agent_layout(config.agent_root(agent))
    return bool(cfg) and (cfg.get('test') is True or cfg.get('mode') == 'test' or (strict and cfg.get('live') is not True))


def read_inbox(path):
    out = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if isinstance(e, dict): out.append(e)
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def suggest(agent, title, what, expected, example, subject=None, prio=None, now=None, target=None):
    """Appends the suggestion to the target's inbox (target_root; `target` forces one instance). Returns
    {"result": "written"|"duplicate", "key", "left", "path", "target"}.
    Raises SuggestionError (refused: missing field, client data, limit, test mode, no target on this machine)."""
    now = now or datetime.now(timezone.utc)
    fields = {k: ' '.join(str(v or '').split()) for k, v in
              {'title': title, 'subject': subject, 'what': what, 'expected': expected, 'example': example}.items()}
    for k in ('title', 'what', 'expected', 'example'):
        if not fields[k]: raise SuggestionError(f'falta --{k}')
    for k, n in LIMITS.items():
        if len(fields[k]) > n: raise SuggestionError(f'{k}: ate {n} caracteres')
    p = priority(prio)
    cfg = _agent_config(agent)
    if _test_mode(agent, cfg): raise SuggestionError(f'{agent} esta em modo teste: sugestao nao sai')
    check_text(agent, fields, cfg)
    name, root = target_root(agent, cfg, target)
    if not root:
        tried = ', '.join([target] if target else target_candidates(agent, cfg))
        raise SuggestionError(f'nenhuma instancia para receber a sugestao nesta maquina ({tried} em {config.agent_base()})')
    path = os.path.join(root, INBOX_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = key_of(fields['title'], fields['subject'])
    day = now.astimezone().date().isoformat()
    with open(path + '.lock', 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        mine = [e for e in read_inbox(path) if e.get('agent') == agent and e.get('day') == day]
        if any(e.get('key') == key for e in mine):
            return {'result': 'duplicate', 'key': key, 'left': max(0, PER_DAY - len(mine)), 'path': path, 'target': name}
        if len(mine) >= PER_DAY:
            raise SuggestionError(f'limite de {PER_DAY} sugestoes por dia para {agent}; guarde para amanha')
        entry = {'id': hashlib.sha256(f'{agent}|{key}|{now.isoformat()}'.encode()).hexdigest()[:16], 'at': now.isoformat(),
                 'day': day, 'agent': agent, 'key': key, 'title': fields['title'], 'subject': fields['subject'] or None,
                 'what': fields['what'], 'expected': fields['expected'], 'example': fields['example'], 'priority': p}
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return {'result': 'written', 'key': key, 'left': PER_DAY - len(mine) - 1, 'path': path, 'target': name}


def cli(agent, a):
    """`sugestao` of the planou CLI. Exit 0 written or duplicate, 1 refused (message on stderr)."""
    try:
        res = suggest(agent, a.title, a.what, a.expected, a.example, a.subject, a.priority)
    except SuggestionError as e:
        print(f'SUGESTAO RECUSADA: {e}', file=sys.stderr)
        return 1
    if res['result'] == 'duplicate':
        print(f'sugestao ja registrada hoje por {agent} (mesmo titulo e assunto); nada novo')
    else:
        print(f'sugestao registrada para o {res["target"]} (vira tarefa no backlog do Planou no proximo tick dele); '
              f'restam {res["left"]} hoje')
    return 0
