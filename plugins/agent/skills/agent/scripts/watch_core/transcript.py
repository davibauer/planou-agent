"""The session's own conversation, read by the runner and sent to Planou: messages, cost per turn and the session link.

The runner of an agent runs as a child of its Claude Code session. Every 5 s it compares the size and mtime of the
session transcript (a `stat`, no Python); only when that changed it calls `pump`, which reads what was appended since
the last read and sends it. This never wakes the session and never touches the heavy tick.

Where the session is:
  <claude dir>/sessions/<pid>.json    one per live session: sessionId, cwd, name (the agent), bridgeSessionId (the Remote
                                   Control id; the link is https://claude.ai/code/<bridgeSessionId>)
  <claude dir>/projects/<cwd with every non-alphanumeric character as '-'>/<sessionId>.jsonl
                                   the transcript; subagents in <sessionId>/subagents/*.jsonl
The session is the first ancestor of this process with a sessions/<pid>.json (the runner is a child of the session);
without one, the live session named after the agent.

What goes to Planou (the agent's "planou" block decides; test mode sends nothing):
  POST /agent/messages   the person's prompts (typed, queued, from the phone) as they appear, and the agent's final text
                         of each turn once the turn is closed, with the operational noise summarized in `ops`. Only with
                         "conversation": true in the block, which defaults to true when "confidentiality" is "detail"
                         (the text of a conversation is third-party text).
  POST /turns            the raw token usage of each closed turn per model, subagents included, the task in progress when
                         it started (watch_core.planou.current_task) and its class (triage when the runner woke it)
  heartbeat              session_id and remote_control_url ride on every heartbeat (watch_core.planou.heartbeat reads
                         them from cache/planou/session.json); a new session or a new link sends a "poll" heartbeat.
                         While a turn is open and the transcript grows, "working" at most once a minute; when that turn
                         closes, "turn_end" (so a long turn the person started is not taken for an idle agent)

A turn starts at a prompt (typed, a task notification, a scheduled prompt, a message from another session) and closes at
its `turn_duration` entry or at the next prompt. Only closed turns are sent: the cost key is <session>:<first answer
id>:<last answer id>, so a turn sent half-way would come back later under another key and count twice. The cursor
(cache/planou/transcript.json: session, inode, byte offset) stays at the start of the turn still open.

First read of a session (no cursor, another inode, a shorter file): no backfill. It starts at the last prompt within the
last few MB of the transcript, so the turn in progress is counted and the history is not (the cost hook of phase 3 read
the whole file and every subagent file from the start).

Planou unreachable: messages and turns wait in cache/planou/messages_outbox.jsonl and turns_outbox.jsonl (capped); a
batch the server refuses as invalid goes to *_rejected.jsonl so it never blocks the rest. None of this touches
health.json: the heavy tick's own calls report a Planou outage.

  python3 -m watch_core.transcript --agent job-scout pump      # the runner's call; prints nothing when all went well
  python3 -m watch_core.transcript --agent job-scout show [FILE] # what would be sent (nothing is sent)
  python3 -m watch_core.transcript --agent job-scout session   # the session found, as JSON

Standard library only.
"""
import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

from . import config, fileio, planou, turns

TAIL_BYTES = 4 * 1024 * 1024        # first read of a session: look for the last prompt within this much
BATCH_MESSAGES = 50
BATCH_TURNS = 200
OUTBOX_MAX = 5000
TEXT_MAX = 20000
NARRATION_MAX = 200                 # a short text right before a tool call is narration ("Let me read the file.")
REMOTE_URL = 'https://claude.ai/code/{}'
WORKING_EVERY_S = 60                # at most one "working" heartbeat per minute during a long turn
RETRY_STATUS = (401, 403, 404, 405, 408, 409, 425, 429)   # the server may not have the endpoint or the scope yet

# ---------------------------------------------------------------- where the session is


def _claude_dir():
    return config.claude_dir()


def _read_session(pid):
    try:
        with open(os.path.join(_claude_dir(), 'sessions', f'{pid}.json')) as f:
            d = json.load(f)
    except (OSError, ValueError):             # a session file may be half written
        return None
    return d if isinstance(d, dict) and d.get('sessionId') else None


def _parent(pid):
    try:
        raw = open(f'/proc/{pid}/stat').read()
        return int(raw[raw.rindex(')') + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _alive(pid):
    try:
        return os.path.exists(f'/proc/{int(pid)}')
    except (TypeError, ValueError):
        return False


def find_session(agent=None, pid=None):
    """The session this process belongs to (its sessions/<pid>.json as a dict), or None."""
    pid = pid or os.getpid()
    for _ in range(64):
        if pid <= 1: break
        d = _read_session(pid)
        if d: return d
        pid = _parent(pid)
    if not agent: return None
    found = []
    for f in glob.glob(os.path.join(_claude_dir(), 'sessions', '*.json')):
        name = os.path.basename(f)[:-5]
        if not name.isdigit(): continue
        d = _read_session(name)
        if d and d.get('name') == agent and _alive(d.get('pid') or name):
            found.append(d)
    return max(found, key=lambda d: d.get('updatedAt') or 0) if found else None


def session_alive(session_id):
    """True when a live Claude Code process has this session (its sessions/<pid>.json says so and the pid runs)."""
    if not session_id: return False
    for f in glob.glob(os.path.join(_claude_dir(), 'sessions', '*.json')):
        name = os.path.basename(f)[:-5]
        if not name.isdigit(): continue
        d = _read_session(name)
        if d and d.get('sessionId') == session_id and _alive(d.get('pid') or name):
            return True
    return False


def transcript_path(session):
    sid, cwd = session.get('sessionId'), session.get('cwd') or ''
    base = os.path.join(_claude_dir(), 'projects')
    for slug in (re.sub(r'[^A-Za-z0-9]', '-', cwd), re.sub(r'[/._]', '-', cwd)):
        p = os.path.join(base, slug, f'{sid}.jsonl')
        if os.path.exists(p): return p
    hits = glob.glob(os.path.join(base, '*', f'{sid}.jsonl'))
    return hits[0] if hits else None


def bridge_id(session, path=None):
    """The Remote Control id: from the session file, else the last bridge-session entry of the transcript."""
    if session.get('bridgeSessionId'): return session['bridgeSessionId']
    if not path: return None
    try:
        with open(path, 'rb') as f:
            f.seek(max(0, os.path.getsize(path) - TAIL_BYTES))
            raw = f.read()
    except OSError:
        return None
    found = None
    for ln in raw.splitlines():
        if b'"bridge-session"' not in ln: continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if e.get('type') == 'bridge-session' and e.get('bridgeSessionId'): found = e['bridgeSessionId']
    return found


# ---------------------------------------------------------------- reading the transcript


def _content(e):
    m = e.get('message') if isinstance(e.get('message'), dict) else {}
    return m.get('content')


def _text_of(content):
    if isinstance(content, str): return content
    if isinstance(content, list):
        return '\n'.join(x.get('text') or '' for x in content if isinstance(x, dict) and x.get('type') == 'text')
    return ''


def opener(e):
    """How a prompt opened a turn: 'human', 'notification' (the runner or a background task woke the session),
    'scheduled', 'peer' (another session), or None when the entry opens no turn (tool results, skill text injected
    with a command, local shell commands, anything of a subagent)."""
    if e.get('type') != 'user' or e.get('isSidechain'): return None
    c = _content(e)
    if isinstance(c, list):
        if not any(isinstance(x, dict) and x.get('type') != 'tool_result' for x in c): return None
    elif not (isinstance(c, str) and c.strip()):
        return None
    origin = (e.get('origin') or {}).get('kind') if isinstance(e.get('origin'), dict) else None
    how = e.get('turnOrigin')
    text = _text_of(c).lstrip()
    if origin == 'task-notification' or how == 'task_notification' or text.startswith('<task-notification>'):
        return 'notification'
    if origin == 'peer' or how == 'peer': return 'peer'
    if how == 'scheduled': return 'scheduled'
    if e.get('isMeta') or e.get('isCompactSummary') or e.get('isVisibleInTranscriptOnly'): return None   # compaction recap
    if text.startswith(('<local-command', '<bash-input>', '<bash-stdout>', '<bash-stderr>', '[Request interrupted by user')):
        return None
    return 'human'


def user_text(e):
    """The prompt as the person wrote it, or '' when it is not something a person typed."""
    text = _text_of(_content(e)).strip()
    if '<command-name>' in text or text.startswith('<command-message>'):
        name = re.search(r'<command-name>\s*/?([^<]*?)\s*</command-name>', text)
        args = re.search(r'<command-args>([\s\S]*?)</command-args>', text)
        if not name: return ''
        return ('/' + name.group(1) + (' ' + args.group(1).strip() if args and args.group(1).strip() else '')).strip()
    if text.startswith('<') and not text.startswith('<pasted_content'): return ''
    return text


def _ts(e):
    t = turns._ts(e)
    return t if t is None or t.tzinfo else t.replace(tzinfo=timezone.utc)


def read_from(path, offset):
    """[(byte offset of the line, entry)] of the complete lines from offset on, and the offset after the last one."""
    with open(path, 'rb') as f:
        f.seek(offset)
        raw = f.read()
    end = raw.rfind(b'\n')
    if end < 0: return [], offset
    out, pos = [], offset
    for ln in raw[:end + 1].split(b'\n')[:-1]:
        here, pos = pos, pos + len(ln) + 1
        if not ln.strip(): continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict): out.append((here, e))
    return out, offset + end + 1


def first_offset(path):
    """Where the first read of a session starts: the last prompt within the last TAIL_BYTES, else the end of file."""
    size = os.path.getsize(path)
    start = max(0, size - TAIL_BYTES)
    with open(path, 'rb') as f:
        f.seek(start)
        raw = f.read(size - start)
    if start:
        cut = raw.find(b'\n')
        if cut < 0: return size
        raw, start = raw[cut + 1:], start + cut + 1
    last, pos = None, start
    for ln in raw.split(b'\n'):
        if b'"user"' in ln:
            try:
                e = json.loads(ln)
            except ValueError:
                e = None
            if isinstance(e, dict) and opener(e): last = pos
        pos += len(ln) + 1
    return size if last is None else last


def _tool_label(block):
    inp = block.get('input') if isinstance(block.get('input'), dict) else {}
    return ' '.join(str(inp.get('description') or inp.get('prompt') or block.get('name') or '').split())[:120]


def parse(entries):
    """Turns of [(offset, entry)]: [{'offset', 'kind', 'opening', 'start', 'end', 'closed', 'duration_ms', 'answers'
    (message id -> (model, counters)), 'order' (ids), 'texts' [(uuid, ts, text, narration)], 'ops'}]."""
    out, cur = [], None
    for off, e in entries:
        kind = opener(e)
        if kind:
            if cur: cur['closed'] = True
            cur = {'offset': off, 'kind': kind, 'opening': e, 'start': _ts(e), 'end': _ts(e), 'closed': False,
                   'duration_ms': None, 'answers': {}, 'order': [], 'texts': [],
                   'ops': {'commands': 0, 'files_read': 0, 'background': []}}
            out.append(cur)
            continue
        if cur is None or cur['closed']: continue
        if e.get('type') == 'system' and e.get('subtype') == 'turn_duration':
            cur['closed'] = True
            cur['duration_ms'] = e.get('durationMs')
            cur['end'] = _ts(e) or cur['end']
            continue
        if e.get('type') != 'assistant' or e.get('isSidechain'): continue
        cur['end'] = _ts(e) or cur['end']
        u = turns.usage_of(e)
        if u:
            if u[0] not in cur['answers']: cur['order'].append(u[0])
            cur['answers'][u[0]] = (u[1], u[2])        # the same answer in several lines: the last one wins
        c = _content(e)
        for b in c if isinstance(c, list) else []:
            if not isinstance(b, dict): continue
            if b.get('type') == 'text' and (b.get('text') or '').strip():
                cur['texts'].append([e.get('uuid'), _ts(e), b['text'].strip(), False])
            elif b.get('type') == 'tool_use':
                if cur['texts'] and len(cur['texts'][-1][2]) <= NARRATION_MAX and '\n' not in cur['texts'][-1][2]:
                    cur['texts'][-1][3] = True             # the short line right before a tool call
                name, inp = b.get('name') or '', b.get('input') if isinstance(b.get('input'), dict) else {}
                if name in ('Bash', 'PowerShell'): cur['ops']['commands'] += 1
                if name in ('Read', 'NotebookRead'): cur['ops']['files_read'] += 1
                if inp.get('run_in_background') or name in ('Agent', 'Task'):
                    label = _tool_label(b)
                    if label: cur['ops']['background'].append(label)
    return out


# ---------------------------------------------------------------- what a turn becomes

REF_RE = re.compile(r'\b(?:[A-Z]{3}\d{4}|[A-Z][A-Z0-9]{1,9}-\d{1,6})\b')
REF_STOP = {'UTF', 'ISO', 'SHA', 'RFC', 'CVE', 'MD', 'TLS', 'SSL', 'HTTP', 'IPV', 'COVID', 'GPT', 'ES', 'PEP', 'X', 'AES',
            'RSA', 'CP', 'WIN', 'US', 'BR', 'EN', 'PT', 'GMT', 'UTC'}
QUIET_RE = re.compile(r'nada (mais )?(de )?nov|nada para (voc|fazer)|sem nada para|nada a fazer|sem novidade|nada relevante|'
                      r'rodando normalmente|nothing new|nothing for you', re.I)
WARNING_RE = re.compile(r'\bAVISO\b|FONTE QUEBRADA|\berro\b|\berror\b|falhou|falha\b|vencid|atrasad|⚠', re.I)


def task_refs(text):
    seen = []
    for m in REF_RE.findall(text or ''):
        if '-' in m and m.split('-')[0] in REF_STOP: continue
        if m not in seen: seen.append(m)
    return seen[:20]


def needs_action(text):
    if re.search(r'Rascunho:', text): return True
    if re.search(r'\bPosso\b[^?\n]{0,300}\?', text): return True
    tail = text.strip()[-400:]
    return bool(re.search(r'\?\s*(\*\*)?\s*$', tail, re.M))


def agent_text(turn):
    parts = [t[2] for t in turn['texts'] if not t[3]]
    if not parts and turn['texts']: parts = [turn['texts'][-1][2]]
    return '\n\n'.join(parts)[:TEXT_MAX]


def agent_message(turn, session):
    """The agent's message of a closed turn, or None when it wrote nothing."""
    text = agent_text(turn)
    if not text: return None
    last = [t for t in turn['texts'] if not t[3]] or turn['texts']
    if needs_action(text): kind = 'needs_action'
    elif WARNING_RE.search(text): kind = 'warning'
    elif turn['kind'] in ('human', 'peer'): kind = 'reply'
    else: kind = 'tick_summary'
    quiet = kind == 'tick_summary' and len(text) <= 400 and bool(QUIET_RE.search(text))
    dur = turn['duration_ms']
    secs = int(dur / 1000) if isinstance(dur, (int, float)) else int(((turn['end'] or turn['start']) - turn['start']).total_seconds())
    ops = {'duration_s': max(0, secs), 'commands': turn['ops']['commands'], 'files_read': turn['ops']['files_read'],
           'background': turn['ops']['background'][:20]}
    ts = last[-1][1] or turn['end'] or turn['start']
    return {'source_key': f'{session}:{last[-1][0]}', 'ts': ts.isoformat(), 'role': 'agent', 'text': text, 'kind': kind,
            'quiet': quiet, 'ops': ops, 'task_refs': task_refs(text)}


def user_message(turn, session):
    e = turn['opening']
    if turn['kind'] != 'human': return None
    text = user_text(e)
    if not text: return None
    return {'source_key': f'{session}:{e.get("uuid")}', 'ts': (turn['start'] or datetime.now(timezone.utc)).isoformat(),
            'role': 'user', 'text': text[:TEXT_MAX], 'kind': 'user', 'quiet': False,
            'ops': {'duration_s': 0, 'commands': 0, 'files_read': 0, 'background': []}, 'task_refs': task_refs(text)}


def cost_turn(turn, session, agent, extra=()):
    """The /turns item of a closed turn (same shape and key as watch_core.turns), or None without usage.
    extra: subagent answers [(ts, model, counters, id)] that belong to it."""
    if not turn['order'] or not turn['start']: return None
    usage = {}
    for mid in turn['order']:
        model, counters = turn['answers'][mid]
        turns._add(usage, model, counters)
    for _, model, counters, _ in extra:
        turns._add(usage, model, counters)
    task = planou.current_task(at=turn['start'])
    klass = 'triage' if turn['kind'] in ('notification', 'scheduled') else turns.classify(
        {'opening': _text_of(_content(turn['opening']))[:300]}, task)
    end = turn['end'] or turn['start']
    return {'key': f'{session}:{turn["order"][0]}:{turn["order"][-1]}', 'agent': agent, 'session': session,
            'started_at': turn['start'].isoformat(), 'ended_at': end.isoformat(),
            'active_seconds': max(0, min(turns.MAX_ACTIVE_S, int((end - turn['start']).total_seconds()))),
            'class': klass, 'task': task, 'usage': usage}


# ---------------------------------------------------------------- subagents


def _subagents(path, session, cur, since):
    """New subagent answers [(ts, model, counters, id)] since the last read, each file from its own offset. A file
    never seen on the first read of a session starts near its end, and anything older than `since` is dropped."""
    base = os.path.join(os.path.dirname(path), session, 'subagents')
    offsets = cur.setdefault('sub', {})
    counted = set(cur.get('counted') or [])
    out = []
    for f in sorted(glob.glob(os.path.join(base, '*.jsonl'))):
        name = os.path.basename(f)
        try:
            start = offsets.get(name)
            if start is None or start > os.path.getsize(f): start = max(0, os.path.getsize(f) - TAIL_BYTES) if since else 0
            entries, offsets[name] = read_from(f, start)
        except OSError:
            continue
        for _, e in entries:
            u, ts = turns.usage_of(e), _ts(e)
            if not u or not ts or u[0] in counted: continue
            if since and ts < since: continue
            counted.add(u[0])
            out.append((ts, u[1], u[2], u[0]))
    cur['counted'] = list(counted)[-5000:]
    return out


# ---------------------------------------------------------------- delivery


def _outbox(name):
    return os.path.join(planou._cache_dir(), name)


def deliver(name, items, path, key, batch):
    """Sends what waits in the outbox plus `items`, in batches. Returns (sent, left, warning or None)."""
    box = _outbox(name + '_outbox.jsonl')
    pending = []
    try:
        with open(box) as f:
            pending = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        pending = []
    queue = pending + list(items)
    if not queue: return 0, 0, None
    sent, left, warn, i = 0, [], None, 0
    while i < len(queue):
        chunk = queue[i:i + batch]
        try:
            planou._call('POST', path, {key: chunk})
            sent += len(chunk)
        except planou.PlanouError as e:
            if 400 <= e.status < 500 and e.status not in RETRY_STATUS:
                with open(_outbox(name + '_rejected.jsonl'), 'a') as f:
                    for it in chunk: f.write(json.dumps({'error': str(e)[:300], 'item': it}, ensure_ascii=False) + '\n')
                warn = f'AVISO (planou): {len(chunk)} {name} recusados ({e.status} {e.code})'
            else:
                left = queue[i:]
                warn = f'AVISO (planou): {name} na fila ({len(left)}): {e.status} {e.code} {e.message}'[:300]
                break
        i += batch
    os.makedirs(os.path.dirname(box), exist_ok=True)
    if left:
        fileio.write(box, ''.join(json.dumps(it, ensure_ascii=False) + '\n' for it in left[-OUTBOX_MAX:]))
    elif pending:
        try: os.remove(box)
        except OSError: pass
    return sent, len(left), warn


# ---------------------------------------------------------------- the pump


def _cursor_file():
    return os.path.join(planou._cache_dir(), 'transcript.json')


def _save_json(path, value):
    fileio.write_json(path, value, ensure_ascii=False)


def conversation_on(pc):
    """Messages carry the text of the conversation: only with "conversation": true, or "confidentiality": "detail"."""
    pc = pc or {}
    return pc.get('conversation', planou._S['confidentiality'] == 'detail') is True


def collect(path, session, agent, cur, conversation=True, whole=False):
    """Reads the transcript from the cursor: (messages, cost turns). Updates `cur` in place (offset of the open turn,
    subagent offsets, pool of subagent answers not yet given to a turn, prompts already sent). `whole` reads the file
    from the start (the `show` command only)."""
    st = os.stat(path)
    first = cur.get('session') != session or cur.get('inode') != st.st_ino or (cur.get('offset') or 0) > st.st_size
    if whole:
        cur.clear()
        cur.update(session=session, inode=st.st_ino, offset=0, sub={}, pool=[], counted=[], users=[])
        first = False
    if first:
        cur.clear()
        cur.update(session=session, inode=st.st_ino, offset=first_offset(path), sub={}, pool=[], counted=[], users=[])
    entries, end = read_from(path, cur['offset'])
    parsed = parse(entries)
    since = parsed[0]['start'] if first and parsed else None
    if first and not parsed: since = datetime.now(timezone.utc)
    pool = [(datetime.fromisoformat(p[0]), p[1], p[2], p[3]) for p in cur.get('pool') or []]
    pool += _subagents(path, session, cur, since)
    pool.sort(key=lambda x: x[0])
    users = list(cur.get('users') or [])
    messages, costs = [], []
    next_offset = end
    for t in parsed:
        um = user_message(t, session) if conversation else None
        if um and um['source_key'] not in users:
            messages.append(um)
            users.append(um['source_key'])
        if not t['closed']:
            next_offset = t['offset']            # re-read from this prompt next time
            break
        end_ts = t['end'] or t['start']
        mine = [p for p in pool if p[0] <= end_ts]   # everything not given to a turn yet, up to its end, is this turn's
        c = cost_turn(t, session, agent, mine)
        if c:
            costs.append(c)
            pool = [p for p in pool if p[0] > end_ts]
        if conversation:
            am = agent_message(t, session)
            if am: messages.append(am)
    cur['offset'] = next_offset
    cur['open'] = any(not t['closed'] for t in parsed)
    cur['pool'] = [(p[0].isoformat(), p[1], p[2], p[3]) for p in pool][-2000:]
    cur['users'] = users[-500:]
    return messages, costs


def _session_info():
    return planou._load('session.json', {})


def pump(agent, root=None, pid=None, now=None):
    """The runner's call. Returns lines for planou.err (warnings); never raises for a missing session."""
    pc = planou.configure_agent(agent, root)
    if not planou.active(): return []
    sess = find_session(agent, pid)
    if not sess: return []
    session = sess['sessionId']
    path = transcript_path(sess)
    if not path: return []
    bid = bridge_id(sess, path)
    info = {'session_id': session, 'remote_control_url': REMOTE_URL.format(bid) if bid else None, 'transcript': path}
    lines = []
    if info != _session_info():
        planou._save('session.json', info)
        with open(os.path.join(planou._cache_dir(), 'transcript.path'), 'w') as f:
            f.write(path + '\n')
        lines += planou.heartbeat('poll')
    try:
        with open(_cursor_file()) as f:
            cur = json.load(f)
    except (OSError, ValueError):
        cur = {}
    size = os.path.getsize(path)
    grew = size > (cur.get('size') or 0) and cur.get('session') == session
    was = {k: cur.get(k) for k in ('working', 'working_at')} if cur.get('session') == session else {}
    messages, costs = collect(path, session, agent, cur, conversation_on(pc))
    cur.update(was)
    cur['size'] = size
    _, _, w1 = deliver('turns', costs, '/turns', 'turns', BATCH_TURNS)
    _, _, w2 = deliver('messages', messages, '/agent/messages', 'messages', BATCH_MESSAGES)
    lines += working_signal(cur, grew, now)
    cur['updated'] = (now or datetime.now(timezone.utc)).isoformat()
    _save_json(_cursor_file(), cur)
    return lines + [w for w in (w1, w2) if w]


def working_signal(cur, grew, now=None):
    """A long turn keeps the agent "working" in Planou: while a turn is open and the transcript keeps growing, a
    "working" heartbeat at most once every WORKING_EVERY_S; when that turn closes, one "turn_end" (the normal state).
    A turn the runner never saw growing sends nothing: a woken session already sent "woke" and the runner is gone."""
    t = (now or datetime.now(timezone.utc)).timestamp()
    if cur.get('open'):
        if grew and t - (cur.get('working_at') or 0) >= WORKING_EVERY_S:
            cur['working'], cur['working_at'] = True, t
            return planou.heartbeat('working')
        return []
    if cur.get('working'):
        cur['working'] = False
        return planou.heartbeat('turn_end')
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python3 -m watch_core.transcript')
    ap.add_argument('--agent', required=True)
    ap.add_argument('--root')
    ap.add_argument('cmd', choices=['pump', 'show', 'session'])
    ap.add_argument('file', nargs='?')
    a = ap.parse_args(argv)
    if a.cmd == 'pump':
        try:
            for ln in pump(a.agent, a.root): print(ln, file=sys.stderr)
        except Exception as e:                 # the runner must never stop because of this
            print(f'AVISO (planou): leitura da conversa: {type(e).__name__}: {str(e)[:200]}', file=sys.stderr)
        return 0
    pc = planou.configure_agent(a.agent, a.root)
    sess = find_session(a.agent)
    if a.cmd == 'session':
        path = transcript_path(sess) if sess else None
        bid = bridge_id(sess, path) if sess else None
        print(json.dumps({'session_id': (sess or {}).get('sessionId'), 'transcript': path,
                          'remote_control_url': REMOTE_URL.format(bid) if bid else None,
                          'active': planou.active(), 'conversation': conversation_on(pc)}, ensure_ascii=False, indent=1))
        return 0
    path = a.file or (transcript_path(sess) if sess else None)
    if not path:
        print('sessao nao encontrada', file=sys.stderr)
        return 1
    session = os.path.basename(path)[:-6]
    messages, costs = collect(path, session, a.agent, {}, True, whole=True)
    for m in messages: print(json.dumps(m, ensure_ascii=False))
    for c in costs: print(json.dumps(c, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
