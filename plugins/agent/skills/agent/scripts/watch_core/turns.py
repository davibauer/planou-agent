"""Cost turns for Planou: the end-of-turn hook (spec section 8).

A turn starts at a user entry that is not only tool results and ends at the last model answer before the next one.
Each turn goes to Planou with the RAW token usage per model (Planou prices it with its own table and splits the
subscription), the active time (capped at 2 h), the task in progress when it started (watch_core.planou.current_task)
and its class: `triage` when the session was woken by the runner (a background task notification), `task` when a task
was marked, `no_task` otherwise.

Reading the transcripts follows team-cost: ~/.claude/projects/*/<session>.jsonl plus <session>/subagents/*.jsonl, one
count per message.id (an answer shows up in several lines).

Subagents (the workers): what each one spent since the last hook goes as its own turn, with `subagent` (the agentType and
the description of its agent-<id>.meta.json, up to 80 characters; only the agentType and a short id under
"minimum", since a description may name a client) and the task it works for: the queue task whose PID its description
names, else the task in progress when it started (fixed per subagent, so parallel workers do not follow whichever task
was marked last). Its active time is the span of what it wrote in that stretch (tool runs included). Planou adds the
session and the workers per task: the measured completed time and the cost of the task.

  python3 -m watch_core.turns hook        # Stop hook: JSON from Claude Code on stdin; always exits 0
  python3 -m watch_core.turns show FILE   # the turns of a transcript, as they would be sent (nothing is sent)

The agent comes from TEAM_AGENT (the team launcher exports it); without it, or in test mode, or without the "planou"
block in the agent config, the hook does nothing. Where it keeps its place: ~/.config/team/planou-turns/<session>.json
(byte offset of the transcript and the subagent answers already counted). A failed POST goes to the agent's
cache/planou/turns_outbox.jsonl and is sent again by the next hook. Standard library only.
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

from . import fileio, planou

MAX_ACTIVE_S = 7200
STATE_DIR = os.path.expanduser('~/.config/team/planou-turns')
# Openings that mean "the runner woke the session" (triage turns), besides the background task notification.
TRIAGE_MARKS = ('<task-notification>', 'conferir runner', 'conferir se o runner')


def _ts(entry):
    try:
        return datetime.fromisoformat(str(entry.get('timestamp')).replace('Z', '+00:00'))
    except ValueError:
        return None


def _text(content):
    if isinstance(content, str): return content
    if isinstance(content, list):
        return ' '.join(x.get('text') or '' for x in content if isinstance(x, dict) and x.get('type') == 'text')
    return ''


def opens_turn(entry):
    """A user entry typed or injected into the session, not the result of a tool call."""
    if entry.get('type') != 'user' or entry.get('isSidechain') or entry.get('isMeta'): return False
    c = (entry.get('message') or {}).get('content')
    if isinstance(c, str): return bool(c.strip())
    if isinstance(c, list):
        return any(isinstance(x, dict) and x.get('type') != 'tool_result' for x in c)
    return False


def usage_of(entry):
    """(message id, model key, counters) of an assistant entry with usage, or None."""
    if entry.get('type') != 'assistant': return None
    m = entry.get('message') or {}
    u = m.get('usage')
    if not isinstance(u, dict): return None
    cc = u.get('cache_creation') if isinstance(u.get('cache_creation'), dict) else None
    w1 = int((cc or {}).get('ephemeral_1h_input_tokens') or 0)
    w5 = int((cc or {}).get('ephemeral_5m_input_tokens') or 0) if cc else int(u.get('cache_creation_input_tokens') or 0)
    model = (m.get('model') or 'unknown') + ('/fast' if u.get('speed') == 'fast' else '')
    return (m.get('id') or entry.get('uuid'), model, {
        'input': int(u.get('input_tokens') or 0), 'cache_write_5m': w5, 'cache_write_1h': w1,
        'cache_read': int(u.get('cache_read_input_tokens') or 0), 'output': int(u.get('output_tokens') or 0)})


def _add(usage, model, counters):
    t = usage.setdefault(model, {'calls': 0, 'input': 0, 'cache_write_5m': 0, 'cache_write_1h': 0, 'cache_read': 0, 'output': 0})
    t['calls'] += 1
    for k, v in counters.items(): t[k] += v


def read_lines(path, offset):
    """Complete lines from the byte offset on, and the offset after the last complete line."""
    with open(path, 'rb') as f:
        f.seek(offset)
        raw = f.read()
    end = raw.rfind(b'\n')
    if end < 0: return [], offset
    out = []
    for ln in raw[:end + 1].splitlines():
        try: out.append(json.loads(ln))
        except ValueError: continue
    return out, offset + end + 1


def turns_of(entries):
    """Turns of a list of transcript entries: [{'start', 'end', 'opening', 'ids': [...], 'usage': {...}}]."""
    turns, cur, seen = [], None, {}
    for e in entries:
        if opens_turn(e):
            cur = {'start': _ts(e), 'end': _ts(e), 'opening': _text((e.get('message') or {}).get('content'))[:300],
                   'ids': [], 'usage': {}}
            turns.append(cur)
            continue
        u = usage_of(e)
        if not u or cur is None: continue
        mid, model, counters = u
        cur['end'] = _ts(e) or cur['end']
        if mid in seen: continue              # the same answer in several lines: counted once
        seen[mid] = True
        cur['ids'].append(mid)
        _add(cur['usage'], model, counters)
    return [t for t in turns if t['ids'] and t['start']]


def classify(turn, task):
    opening = turn['opening'].lstrip()
    if any(opening.startswith(m) or m in opening[:200] for m in TRIAGE_MARKS): return 'triage'
    return 'task' if task else 'no_task'


def build(turns, session, agent):
    out = []
    for t in turns:
        task = planou.current_task(at=t['start'])
        active = max(0, min(MAX_ACTIVE_S, int((t['end'] - t['start']).total_seconds())))
        out.append({'key': f'{session}:{t["ids"][0]}:{t["ids"][-1]}', 'agent': agent, 'session': session,
                    'started_at': t['start'].isoformat(), 'ended_at': t['end'].isoformat(), 'active_seconds': active,
                    'class': classify(t, task), 'task': task, 'usage': t['usage']})
    return out


def _subagent_usage(transcript, session, st, now):
    """Subagent answers written since the last run: [(timestamp, model, counters, id)], all subagents together (the
    shape of plugins up to agent 0.4, kept for `show`). Each subagent file is read from its own byte offset."""
    return [(ts, m, c, mid) for chunk in _subagent_chunks(transcript, session, st).values()
            for ts, m, c, mid in chunk['answers']]


def _meta(path):
    try:
        m = json.load(open(path[:-len('.jsonl')] + '.meta.json'))
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _subagent_chunks(transcript, session, st):
    """What each subagent wrote since the last run: {agent id: {'meta', 'first', 'last', 'answers': [(ts, model,
    counters, id)]}}. Each file is read from its own byte offset (st['sub_offsets']) and ids are deduplicated
    (st['counted']) in case a line repeats. 'first'/'last' span every entry read (tool runs count as active time)."""
    base = os.path.join(os.path.dirname(transcript), session, 'subagents')
    offsets = st.setdefault('sub_offsets', {})
    counted = st.setdefault('counted', {})
    out = {}
    for f in sorted(glob.glob(os.path.join(base, '*.jsonl'))):
        name = os.path.basename(f)
        try:
            entries, offsets[name] = read_lines(f, offsets.get(name, 0))
        except OSError:
            continue
        aid = name[:-len('.jsonl')]
        aid = aid[len('agent-'):] if aid.startswith('agent-') else aid
        stamps = [t for t in (_ts(e) for e in entries) if t]
        answers = []
        for e in entries:
            u = usage_of(e)
            ts = _ts(e)
            if not u or not ts or u[0] in counted: continue
            counted[u[0]] = True
            answers.append((ts, u[1], u[2], u[0]))
        if answers:
            out[aid] = {'meta': _meta(f), 'first': min(stamps), 'last': max(stamps), 'answers': answers}
    return out


def subagent_name(meta, aid):
    """'worker: Plugins: deadline and time' (80 characters at most); under "minimum" only 'worker a06ad1'."""
    kind = ' '.join(str(meta.get('agentType') or 'subagent').split())[:40]
    if planou._S.get('confidentiality') == 'minimum': return f'{kind} {aid[:6]}'
    desc = ' '.join(str(meta.get('description') or '').split())
    return (f'{kind}: {desc}' if desc else f'{kind} {aid[:6]}')[:80]


def subagent_task(meta, first, st, aid):
    """The task a subagent works for, fixed at its first stretch (st['sub_tasks']): the queue task (or the agent's own
    open task, `tarefa nova`) whose PID its description names, else the task in progress when it started."""
    tasks = st.setdefault('sub_tasks', {})
    if aid in tasks: return tasks[aid]
    pat = r'[A-Za-z]{2,6}-?\d{1,6}'
    # in the order they appear, the description before the prompt: the first PID named wins (never the lowest)
    words = list(dict.fromkeys(w.upper() for w in re.findall(pat, str(meta.get('description') or ''))
                               + re.findall(pat, str(meta.get('prompt') or '')[:300])))
    task = None
    try:
        # a queue task, or one of the agent's own open tasks (`tarefa nova`, PLN0396)
        mine = {str(v['pid']).upper() for v in planou.queue_tasks() if v.get('pid')} | planou.act_pids()
        task = next((w for w in words if w in mine), None)
    except Exception:
        task = None
    tasks[aid] = task or planou.current_task(at=first)
    return tasks[aid]


def build_subagents(chunks, session, agent, st):
    """One cost turn per subagent stretch, with `subagent` and the task it works for."""
    out = []
    for aid, c in sorted(chunks.items(), key=lambda kv: kv[1]['first']):
        usage = {}
        for _, model, counters, _ in c['answers']: _add(usage, model, counters)
        ids = [x[3] for x in sorted(c['answers'], key=lambda x: x[0])]
        task = subagent_task(c['meta'], c['first'], st, aid)
        active = max(0, min(MAX_ACTIVE_S, int((c['last'] - c['first']).total_seconds())))
        out.append({'key': f'{session}:{aid}:{ids[0]}:{ids[-1]}', 'agent': agent, 'session': session,
                    'started_at': c['first'].isoformat(), 'ended_at': c['last'].isoformat(), 'active_seconds': active,
                    'class': 'task' if task else 'no_task', 'task': task, 'subagent': subagent_name(c['meta'], aid),
                    'usage': usage})
    return out


def _outbox():
    return os.path.join(planou._cache_dir(), 'turns_outbox.jsonl')


def send(turns):
    """POST /turns with whatever the outbox holds first. On failure everything waits in the outbox."""
    box = _outbox()
    pending = []
    try:
        pending = [json.loads(l) for l in open(box) if l.strip()]
    except (OSError, ValueError):
        pending = []
    batch = pending + turns
    if not batch: return True
    try:
        for i in range(0, len(batch), 200):
            planou._call('POST', '/turns', {'turns': batch[i:i + 200]})
    except planou.PlanouError:
        os.makedirs(os.path.dirname(box), exist_ok=True)
        with open(box, 'w') as f:
            for t in batch: f.write(json.dumps(t, ensure_ascii=False) + '\n')
        return False
    if pending:
        try: os.remove(box)
        except OSError: pass
    return True


def run_hook(data, agent, now=None):
    """The hook body. Returns the turns sent (for tests); raises nothing on purpose only in main()."""
    now = now or datetime.now(timezone.utc)
    session, transcript = data.get('session_id'), data.get('transcript_path')
    if not session or not transcript or not os.path.exists(transcript): return []
    planou.configure_agent(agent)
    if not planou.active(): return []
    os.makedirs(STATE_DIR, exist_ok=True)
    state_file = os.path.join(STATE_DIR, f'{session}.json')
    try:
        st = json.load(open(state_file))
    except (OSError, ValueError):
        st = None
    first = st is None
    st = st or {'offset': 0}
    st['counted'] = {k: True for k in st.get('counted') or []}
    entries, offset = read_lines(transcript, st['offset'])
    turns = turns_of(entries)
    if first: turns = turns[-1:]            # first run in this session: only the turn that just ended, no backfill
    st.setdefault('sub_tasks', {})
    chunks = _subagent_chunks(transcript, session, st)
    if first:
        # older subagent history is not this turn's (and none at all without a turn)
        cut = turns[0]['start'] if turns else None
        for aid in list(chunks):
            c = chunks[aid]
            c['answers'] = [x for x in c['answers'] if cut and x[0] >= cut]
            if not c['answers']: del chunks[aid]
            else: c['first'] = max(c['first'], cut)
    agent = planou.name() or agent        # the name in Planou ("planou.agent", PLN0282)
    payload = build(turns, session, agent) + build_subagents(chunks, session, agent, st)
    old = [(datetime.fromisoformat(c[0]), c[1], c[2], c[3]) for c in st.get('carry') or []]
    if old:
        # subagent usage left over by plugins up to agent 0.4 (it waited for the next turn): its own turn now
        payload += build_subagents({'carry': {'meta': {'agentType': 'subagent'}, 'first': min(x[0] for x in old),
                                              'last': max(x[0] for x in old), 'answers': old}}, session, agent, st)
    send(payload)
    if any(e.get('type') == 'assistant' for e in entries) and not any(usage_of(e) for e in entries):
        # the .jsonl format is internal to Claude Code and may change (the team-cost warning)
        print('AVISO (planou): turno com atividade e nenhum uso lido; o formato do .jsonl pode ter mudado', file=sys.stderr)
    st = {'offset': offset, 'counted': list(st['counted'])[-5000:], 'sub_offsets': st.get('sub_offsets') or {},
          'sub_tasks': dict(list((st.get('sub_tasks') or {}).items())[-500:]), 'updated': now.isoformat()}
    fileio.write_json(state_file, st)
    planou.heartbeat('turn_end')
    return payload


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['show'] and len(argv) > 1:
        entries, _ = read_lines(argv[1], 0)
        planou.configure_agent(os.environ.get('TEAM_AGENT') or 'nobody')
        for t in build(turns_of(entries), os.path.basename(argv[1])[:-6], os.environ.get('TEAM_AGENT') or '?'):
            print(json.dumps(t, ensure_ascii=False))
        return 0
    if argv[:1] != ['hook']:
        print('use: python3 -m watch_core.turns hook | show FILE', file=sys.stderr)
        return 2
    agent = os.environ.get('TEAM_AGENT')
    if not agent: return 0
    try:
        run_hook(json.load(sys.stdin), agent)
    except Exception as e:                   # a broken global hook would break every session: never fail
        print(f'AVISO (planou): hook de custo: {type(e).__name__}: {str(e)[:200]}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
