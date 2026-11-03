"""Model calls through the `claude` CLI, headless. Only text goes in: the transcript lines and the context pack.

The CLI takes ~2 s to start, which is most of a Haiku answer. So each call uses a process started beforehand
(stream-json input keeps it waiting for the message) and a new one is started right away for the next call.
No tools, no MCP servers, no settings or CLAUDE.md: the context pack goes in as the whole system prompt, the same
bytes every call, so the prompt cache serves it.
"""
import asyncio, json, os, time


class Pool:
    def __init__(self, model, system_file, size=2, effort='low'):
        self.model, self.system_file, self.size, self.effort = model, system_file, size, effort
        self.ready = []
        self.lock = asyncio.Lock()
        self.closed = False

    def _cmd(self):
        return ['claude', '-p', '--model', self.model, '--input-format', 'stream-json', '--output-format', 'stream-json',
                '--verbose', '--include-partial-messages', '--effort', self.effort, '--no-session-persistence', '--tools', '', '--setting-sources', '', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}', '--system-prompt-file', self.system_file]

    async def _spawn(self):
        t = asyncio.ensure_future(asyncio.create_subprocess_exec(
            *self._cmd(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=8 * 1024 * 1024, cwd=os.path.dirname(self.system_file)))
        try:
            return await asyncio.shield(t)
        except asyncio.CancelledError:           # cancelled mid-spawn: kill the process once it exists
            t.add_done_callback(_kill_spawned)
            raise

    async def warm(self):
        if self.closed: return
        async with self.lock:
            self.ready = [p for p in self.ready if p.returncode is None]
            while len(self.ready) < self.size:
                self.ready.append(await self._spawn())

    async def ask(self, text, timeout=60, on_text=None, cancel=None):
        """-> {'text', 'ms', 'first_ms', 'api_ms', 'usage', 'cost'}; text is '' on failure.
        on_text(accumulated) is called as the reply streams in; setting `cancel` (asyncio.Event) kills the call."""
        async with self.lock:
            self.ready = [p for p in self.ready if p.returncode is None]
            p = self.ready.pop(0) if self.ready else await self._spawn()
        asyncio.ensure_future(self.warm())
        t = time.time()
        msg = {'type': 'user', 'message': {'role': 'user', 'content': text}}
        out = {'text': '', 'ms': 0, 'first_ms': None, 'api_ms': 0, 'usage': {}, 'cost': 0.0, 'cancelled': False}
        acc = ''
        try:
            p.stdin.write((json.dumps(msg) + '\n').encode()); await p.stdin.drain()
            while True:
                rd = asyncio.ensure_future(p.stdout.readline())
                waits = [rd] + ([asyncio.ensure_future(cancel.wait())] if cancel else [])
                done, pend = await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                for w in pend:
                    if w is not rd: w.cancel()
                if rd not in done:
                    rd.cancel(); out['cancelled'] = bool(cancel and cancel.is_set()); break
                line = rd.result()
                if not line: break
                try: d = json.loads(line)
                except ValueError: continue
                if d.get('type') == 'stream_event':
                    ev = d.get('event') or {}
                    delta = ev.get('delta') or {}
                    if ev.get('type') == 'content_block_delta' and delta.get('type') == 'text_delta':
                        if out['first_ms'] is None: out['first_ms'] = int((time.time() - t) * 1000)
                        acc += delta.get('text', '')
                        if on_text: on_text(acc)
                elif d.get('type') == 'result':
                    out.update(text=d.get('result') or acc, api_ms=d.get('duration_api_ms') or 0,
                               usage=d.get('usage') or {}, cost=d.get('total_cost_usd') or 0.0)
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try: p.stdin.close()
            except Exception: pass
            try: p.kill()
            except ProcessLookupError: pass
        if not out['text']: out['text'] = acc
        out['ms'] = int((time.time() - t) * 1000)
        return out

    async def close(self):
        self.closed = True
        async with self.lock:
            for p in self.ready:
                try: p.kill()
                except ProcessLookupError: pass
                await p.wait()
            self.ready = []


def _kill_spawned(t):
    if t.cancelled() or t.exception(): return
    try: t.result().kill()
    except ProcessLookupError: pass


def parse_json(text):
    """The first JSON object in a model reply (tolerates ```json fences and prose around it)."""
    s = text.find('{'); e = text.rfind('}')
    if s < 0 or e <= s: return None
    try: return json.loads(text[s:e + 1])
    except ValueError: return None


def tokens(u):
    return {'in': u.get('input_tokens', 0), 'cache_read': u.get('cache_read_input_tokens', 0),
            'cache_write': u.get('cache_creation_input_tokens', 0), 'out': u.get('output_tokens', 0)}
