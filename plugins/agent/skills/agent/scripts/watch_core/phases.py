"""Time per phase of a worker (a subagent), read from its transcript: where the worker's time went (PLN0261).

The session closes a worker with `fila worker ... --phases-from <file>`; the file is the `output_file` the Agent tool
returned when the worker started (/tmp/claude-<uid>/<project>/<session>/tasks/<agentId>.output, a link to
~/.claude/projects/<project>/<session>/subagents/agent-<agentId>.jsonl). The agent id alone also works: the transcript
is looked up under ~/.claude/projects.

The phases (Planou's keys, seconds each):
  model        the model thinking and writing: from a tool result (or a prompt) to the next tool call, the lines of one
               streamed answer, and the advisor (a server tool the model waits on)
  tests        tests and build (unittest, pytest, dotnet test/build, vitest, e2e, npm test/build, tsc)
  ci           waiting for the CI of a PR (gh pr checks, gh run watch/view)
  git_publish  git, gh, tags and publish-plugin
  read_search  Read, Grep, Glob, web reads, and cat/grep/sed -n/ls/find in Bash
  edit         Edit, Write, NotebookEdit, and sed -i/tee/patch in Bash
  wait         sleeping (sleep, until loops, Monitor, TaskOutput) or idle after the answer while a background command
               runs; idle while a background CI or test runs counts as ci or tests
  other        any other tool (another subagent, skills, MCP tools, other commands)

How: every timestamped line is sorted by time and [first, last] is cut into consecutive intervals; each interval gets
exactly one phase from the state at its start, so the phases add up to the time the worker was active and parallel
tool calls are never counted twice. The state: a tool call waiting for its result (the oldest one decides), a server
tool (the advisor) waiting, idle after an end_turn (a background command still running decides; with none, the worker
was stopped between two runs, as when the session continues it by message, and that time is left out), or the model.

The same transcript also gives the worker's usage (PLN0101): the raw tokens per model of its answers (input, output,
cache writes 5 min / 1 h and cache reads, with the calls), counted once per message id the way the cost turns are
(watch_core.turns.usage_of), so Planou prices the delivery with the table it uses for the turns.

  python3 -m watch_core.phases <transcript or agent id>     # prints the phases and the usage, to check a worker by hand
"""
import glob, json, os, re, sys
from datetime import datetime

KEYS = ('model', 'tests', 'ci', 'git_publish', 'read_search', 'edit', 'wait', 'other')
LABELS = {'model': 'modelo', 'tests': 'testes', 'ci': 'CI', 'git_publish': 'git/publish', 'read_search': 'leitura/busca',
          'edit': 'edicao', 'wait': 'espera', 'other': 'outros'}

READ_TOOLS = {'Read', 'Grep', 'Glob', 'LSP', 'WebFetch', 'WebSearch', 'ToolSearch', 'NotebookRead'}
EDIT_TOOLS = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit'}
WAIT_TOOLS = {'Monitor', 'TaskOutput', 'BashOutput', 'Sleep'}

# Bash, first match wins: `cd x && git fetch && dotnet test` is tests, `sleep 60; gh pr checks 3` is ci.
_BASH = [
    ('ci', re.compile(r'\bgh\s+(pr\s+checks|run\s+(watch|view|list))\b')),
    ('tests', re.compile(r'\b(unittest|pytest|dotnet\s+(test|build)|vitest|playwright|jest|e2e(-env)?\.sh|npm\s+(run\s+)?(test|build)'
                         r'|npx\s+tsc|tsc\s+-b|go\s+test|cargo\s+(test|build)|mvn|gradle)\b')),
    ('git_publish', re.compile(r'\b(git|gh|publish-plugin)\b|claude\s+plugin\s+tag')),
    ('wait', re.compile(r'^\s*(sleep\s+\d|until\b|while\b.*\bsleep\b)')),
    ('edit', re.compile(r'\bsed\s+-i|\bperl\s+-p?i|\bpatch\b|\btee\b|\bcat\s*>')),
    ('read_search', re.compile(r'\b(grep|rg|sed\s+-n|cat|head|tail|find|ls|wc|jq|diff|stat|tree|awk|less|file)\b')),
]
_BG_ID = re.compile(r'(?:running in background|started)[^\n]*?\bID:\s*([A-Za-z0-9_-]+)', re.I)
_TASK = re.compile(r'<task-id>\s*([A-Za-z0-9_-]+)\s*</task-id>(.*?)(?=<task-id>|\Z)', re.S)
_STATUS = re.compile(r'<status>\s*(\w+)\s*</status>')
_ENDED = {'completed', 'failed', 'killed', 'stopped', 'cancelled', 'canceled', 'error', 'timeout'}


def classify(name, tool_input=None):
    """The phase of one tool call, by the tool and, for Bash, by the command."""
    if name == 'Bash':
        cmd = str((tool_input or {}).get('command') or '')
        for key, rx in _BASH:
            if rx.search(cmd): return key
        return 'other'
    if name in READ_TOOLS: return 'read_search'
    if name in EDIT_TOOLS: return 'edit'
    if name in WAIT_TOOLS: return 'wait'
    return 'other'


def _ts(s):
    try: return datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError): return None


def _text(content):
    if isinstance(content, str): return content
    if isinstance(content, list):
        return '\n'.join(_text(b.get('text') if b.get('type') == 'text' else b.get('content')) for b in content if isinstance(b, dict))
    return ''


def _notifications(text, bg):
    """A task notification in a prompt: a background command that ended leaves `bg` (a Monitor event keeps it)."""
    if '<task-id>' not in (text or ''): return
    for tid, rest in _TASK.findall(text):
        st = _STATUS.search(rest)
        if st and st.group(1).lower() in _ENDED: bg.pop(tid, None)


def compute(lines):
    """Seconds per phase (only the phases with time, rounded to 0.1 s) from the transcript's parsed lines."""
    rows = [(t, i, m) for i, m in enumerate(lines) if isinstance(m, dict) and (t := _ts(m.get('timestamp'))) is not None]
    rows.sort(key=lambda r: (r[0], r[1]))
    total = dict.fromkeys(KEYS, 0.0)
    pending = {}        # tool_use id -> (start, phase, message id)
    server = set()      # server tool ids (the advisor) waiting for their result
    bg = {}             # background task id -> phase of the command
    last_bg_phase = {}  # tool_use id -> phase, to know the phase of the background task its result names
    idle = False
    for n, (t, _, m) in enumerate(rows):
        if n:
            dt = t - rows[n - 1][0]
            if dt > 0:
                phase = _phase(pending, server, idle, bg)
                if phase: total[phase] += dt
        kind = m.get('type')
        msg = m.get('message') if isinstance(m.get('message'), dict) else {}
        content = msg.get('content')
        if kind == 'assistant':
            mid = msg.get('id')
            # a new answer means every call of the previous ones came back (a result lost in the log never sticks)
            for k in [k for k, v in pending.items() if mid and v[2] and v[2] != mid]: pending.pop(k)
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict): continue
                if b.get('type') == 'tool_use':
                    ph = classify(b.get('name'), b.get('input'))
                    pending[b.get('id')] = (t, ph, mid)
                    last_bg_phase[b.get('id')] = ph
                elif b.get('type') == 'server_tool_use':
                    server.add(b.get('id'))
                elif str(b.get('type', '')).endswith('_tool_result'):
                    server.discard(b.get('tool_use_id'))
            idle = msg.get('stop_reason') == 'end_turn'
            if idle: server = set()
        elif kind == 'user':
            results = [b for b in content if isinstance(b, dict) and b.get('type') == 'tool_result'] if isinstance(content, list) else []
            for b in results:
                tid = b.get('tool_use_id')
                pending.pop(tid, None)
                started = _BG_ID.search(_text(b.get('content')))
                if started: bg[started.group(1)] = last_bg_phase.get(tid, 'wait')
            if not results:
                idle = False
                _notifications(_text(content), bg)
        elif kind == 'attachment':
            att = m.get('attachment') if isinstance(m.get('attachment'), dict) else {}
            if att.get('type') == 'queued_command':
                _notifications(str(att.get('prompt') or ''), bg)
                idle = False
    return {k: round(v, 1) for k, v in total.items() if round(v, 1) > 0}


def _phase(pending, server, idle, bg):
    if pending: return min(pending.values(), key=lambda v: v[0])[1]
    if server: return 'model'
    if idle:
        if not bg: return None               # stopped between two runs: not the worker's time
        kinds = set(bg.values())
        return 'ci' if 'ci' in kinds else 'tests' if 'tests' in kinds else 'wait'
    return 'model'


def read(path):
    """The parsed lines of a transcript (a broken line is skipped)."""
    out = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: out.append(json.loads(line))
            except ValueError: continue
    return out


def resolve(ref, home=None):
    """The transcript of a worker: a file (the Agent tool's output_file, a link, is followed) or an agent id looked up in
    ~/.claude/projects/*/*/subagents. None when not found."""
    ref = str(ref or '').strip()
    if not ref: return None
    path = os.path.expanduser(ref)
    if os.path.isfile(path): return os.path.realpath(path)
    aid = os.path.basename(ref)
    for suffix in ('.output', '.jsonl'):
        if aid.endswith(suffix): aid = aid[:-len(suffix)]
    if aid.startswith('agent-'): aid = aid[len('agent-'):]
    if not re.fullmatch(r'[A-Za-z0-9_-]{6,80}', aid): return None
    from . import config
    found = sorted(glob.glob(os.path.join(glob.escape(config.claude_dir(home)), 'projects', '*', '*', 'subagents', f'agent-{aid}.jsonl')),
                   key=lambda p: os.path.getmtime(p), reverse=True)
    return found[0] if found else None


def usage(entries):
    """Raw usage per model of a worker's answers: {model: {'calls', 'input', 'cache_write_5m', 'cache_write_1h',
    'cache_read', 'output'}}, the shape of the cost turns. An answer written in several lines (one per content block)
    counts once, by its message id, taking its first line as the turns do. {} when no answer brought usage."""
    from . import turns as _turns          # lazy: turns imports planou, which imports this module
    out, seen = {}, set()
    for e in entries:
        u = _turns.usage_of(e)
        if not u or u[0] in seen: continue
        seen.add(u[0])
        _turns._add(out, u[1], u[2])
    return out


def measure(ref, home=None):
    """(phases, usage, None) or (None, None, why) for `fila worker --phases-from`; phases or usage may be empty."""
    path = resolve(ref, home)
    if not path: return None, None, f'registro do worker nao encontrado: {ref}'
    try: entries = read(path)
    except OSError as e: return None, None, f'registro do worker ilegivel: {e.strerror or e}'
    return compute(entries), usage(entries), None


def from_ref(ref, home=None):
    """(phases, None) or (None, why) for `fila worker --phases-from`."""
    phases, _, why = measure(ref, home)
    if why: return None, why
    if not phases: return None, 'registro do worker sem tempo medido'
    return phases, None


def fit(phases, duration_ms):
    """Scaled down to the worker's duration when they add up to more (the log and the session's clock differ a little);
    never scaled up: time left out (stopped between runs) stays out."""
    if not phases or not duration_ms: return phases
    total, limit = sum(phases.values()), duration_ms / 1000.0
    if total <= limit: return phases
    f = limit / total
    return {k: round(v * f, 1) for k, v in phases.items() if round(v * f, 1) > 0}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print('uso: python3 -m watch_core.phases <registro do worker ou agent id>', file=sys.stderr)
        return 2
    phases, used, why = measure(argv[0])
    if why:
        print(why, file=sys.stderr)
        return 1
    if not phases and not used:
        print('registro do worker sem tempo medido', file=sys.stderr)
        return 1
    total = sum((phases or {}).values())
    if total:
        print(f'total medido {total / 60:.1f} min')
        for k in sorted(phases, key=lambda k: -phases[k]):
            print(f'  {LABELS[k]:14s} {phases[k] / 60:5.1f} min  {phases[k] / total:4.0%}')
    for model, c in sorted((used or {}).items()):
        print(f'  uso {model}: {c["calls"]} respostas, entrada {c["input"]}, saida {c["output"]}, '
              f'cache 5m {c["cache_write_5m"]}, cache 1h {c["cache_write_1h"]}, leitura de cache {c["cache_read"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
