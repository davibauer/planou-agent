"""The session: audio source -> segmenter -> ASR + who -> transcript -> cards and notes -> the panel (SSE).

Everything the panel shows is also written to the session dir:
  transcript.jsonl  one line per transcript line (updates included)
  cards.jsonl       one record per model call for cards, with latency and tokens
  notes.json        the latest notes; metrics.jsonl per segment and per call
  context.md        the context pack the model got (system prompt)
At the end: transcript.md and notes.md (the meeting-minutes flow can reuse them) and summary.json.
"""
import asyncio, concurrent.futures, ipaddress, json, os, time
from datetime import datetime
from urllib.parse import urlsplit

from aiohttp import web

from . import addressee, audio, asr, brain, config, context, detect, llm, monitor, ready

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
MAX_CARD_CALLS = 2   # a follow-up question does not wait for the previous cards


class Session:
    def __init__(self, args, source_kind):
        self.args, self.kind = args, source_kind
        self.cfg = config.load()
        self.user, self.aliases = config.user()
        now = datetime.now()
        self.dir = args.session_dir or os.path.join(config.session_root(args.instance), now.strftime('%Y-%m-%d-%H%M%S'))
        os.makedirs(self.dir, exist_ok=True)
        self.title = args.title or ('Replay: ' + os.path.splitext(os.path.basename(args.file))[0] if source_kind == 'replay'
                                    else 'Meeting ' + now.strftime('%d/%m %H:%M'))
        self.lines, self.groups, self.cards, self.notes, self.fyi = [], [], [], [], []
        self.next_note = 1; self.notes_since = 0.0; self.notes_busy = False
        self.subs = set(); self.paused = False; self.ended = False
        self.focus = {}; self.prefs = monitor.settings(); self.dismissed = []     # the monitor (PLN0421)
        self.card_busy = 0; self.card_next = None     # calls in flight (at most MAX_CARD_CALLS) and the one waiting
        self.metrics = {'asr_ms': [], 'who_ms': [], 'endpoint_ms': [], 'card_ms': [], 'card_api_ms': [],
                        'card_total_ms': [], 'notes_ms': [], 'tokens': {'in': 0, 'cache_read': 0, 'cache_write': 0, 'out': 0},
                        'card_calls': 0, 'notes_calls': 0, 'cost_usd': 0.0}
        self.exec = concurrent.futures.ThreadPoolExecutor(1)       # GPU work, one at a time
        self.loop = None; self.source = None; self.segq = None; self.segmenters = {}
        self.detector = None; self.addr_pool = None; self.inflight = 0
        self.metrics.update(addressee_calls=0, addressee_yes=0, addressee_no=0, addressee_fail=0,
                            addressee_ms=[], gate_turns=0, gate_fired=0)

    # ---------- output ----------
    def _append(self, name, obj):
        with open(os.path.join(self.dir, name), 'a', encoding='utf-8') as f: f.write(json.dumps(obj, ensure_ascii=False) + '\n')

    VIEW_EVENTS = {'group', 'group_reset', 'card', 'notes', 'focus', 'settings', 'ended', 'paused'}

    def emit(self, ev):
        data = json.dumps(ev, ensure_ascii=False)
        for q in list(self.subs): q.put_nowait(data)
        if ev.get('type') in self.VIEW_EVENTS: self.emit({'type': 'view', 'view': self.view()})

    def view(self):
        return monitor.view({'groups': self.groups, 'cards': self.cards, 'notes': self.notes, 'focus': self.focus,
                             'ended': self.ended, 'paused': self.paused, 'user': self.user, 'aliases': self.aliases},
                            self.prefs, self.dismissed)

    def snapshot(self):
        return {'type': 'hello', 'session': {'title': self.title, 'kind': self.kind, 'dir': self.dir, 'user': self.user,
                                             'speed': getattr(self.args, 'speed', 1.0), 'sources': self.pack.sources,
                                             'models': {'cards': self.cfg['cards_model'], 'notes': self.cfg['notes_model'], 'addressee': addressee.model_name(self.cfg) or '',
                                                        'asr': self.cfg['asr_model']}},
                'lines': self.lines, 'groups': self.groups, 'cards': self.cards, 'notes': self.notes, 'fyi': self.fyi,
                'paused': self.paused, 'ended': self.ended, 'metrics': self.metric_summary(), 'view': self.view(),
                'pos': round(getattr(self.source, 'pos', 0.0) or 0.0, 1)}

    def metric_summary(self):
        m = self.metrics
        def med(v): return int(sorted(v)[len(v) // 2]) if v else None
        def p90(v): return int(sorted(v)[min(len(v) - 1, int(len(v) * 0.9))]) if v else None
        return {'asr_ms_med': med(m['asr_ms']), 'asr_ms_p90': p90(m['asr_ms']), 'who_ms_med': med(m['who_ms']),
                'endpoint_ms_med': med(m['endpoint_ms']), 'card_ms_med': med(m['card_ms']), 'card_api_ms_med': med(m['card_api_ms']),
                'card_total_ms_med': med(m['card_total_ms']), 'card_total_ms_max': max(m['card_total_ms']) if m['card_total_ms'] else None,
                'notes_ms_med': med(m['notes_ms']), 'card_calls': m['card_calls'], 'notes_calls': m['notes_calls'],
                'gate_turns': m.get('gate_turns', 0), 'gate_fired': m.get('gate_fired', 0), 'gate_dropped': m.get('gate_dropped', 0),
                'addressee_calls': m.get('addressee_calls', 0), 'addressee_yes': m.get('addressee_yes', 0),
                'addressee_no': m.get('addressee_no', 0), 'addressee_fail': m.get('addressee_fail', 0),
                'addressee_ms_med': med(m.get('addressee_ms') or []),
                'tokens': m['tokens'], 'cost_usd': round(m['cost_usd'], 4), 'segments': len(m['asr_ms']), 'lines': len(self.lines)}

    # ---------- audio ----------
    def sink(self, ch, x, t_end):
        """Called from the source thread."""
        sg = self.segmenters.get(ch)
        if sg is None:
            sg = self.segmenters[ch] = audio.Segmenter(ch, lambda seg: self.loop.call_soon_threadsafe(self.segq.put_nowait, seg),
                                                       speed=getattr(self.args, 'speed', 1.0))
        sg.feed(x, t_end)

    def _process(self, seg):
        """GPU thread: text and speaker of one segment. A mixed track (replay) can hold two people without a pause
        between them ("did you change it?" "yes"): each sentence Whisper returns gets its own voice check."""
        r = self.asr(seg['audio'])
        t = time.time()
        dur = seg['end'] - seg['start']
        pieces = []
        if r['text'] and not asr.is_junk(r['text'], dur, r['no_speech'], r['logprob']):
            parts = r['parts'] if seg['ch'] == 'mix' and len(r['parts']) > 1 else [(0.0, dur, r['text'])]
            for a, b, text in parts:
                a, b = max(0.0, a), min(dur, max(b, a + 0.05))
                sub = dict(seg, start=seg['start'] + a, end=seg['start'] + b,
                           audio=seg['audio'][int(a * audio.SR):int(b * audio.SR)] if len(parts) > 1 else seg['audio'])
                who, me, cu = self.voices.who(sub)
                if pieces and pieces[-1]['who'] == who:
                    pieces[-1]['text'] += ' ' + text; pieces[-1]['end'] = sub['end']
                else:
                    pieces.append({'who': who, 'me': me, 'cos_user': cu, 'text': text, 'start': sub['start'], 'end': sub['end']})
        r['who_ms'] = int((time.time() - t) * 1000)
        return r, pieces

    async def consume(self):
        while True:
            seg = await self.segq.get()
            if seg is None: break
            self.inflight += 1
            try: r, pieces = await self.loop.run_in_executor(self.exec, self._process, seg)
            finally: self.inflight -= 1
            self.metrics['asr_ms'].append(r['ms']); self.metrics['who_ms'].append(r['who_ms'])
            self.metrics['endpoint_ms'].append(int((seg['wall'] - seg['audio_end_wall']) * 1000))
            self._append('metrics.jsonl', {'kind': 'segment', 'start': round(seg['start'], 2), 'dur': round(seg['end'] - seg['start'], 2),
                                           'ch': seg['ch'], 'asr_ms': r['ms'], 'who_ms': r['who_ms'],
                                           'who': [p['who'] for p in pieces], 'cos_user': [p['cos_user'] for p in pieces],
                                           'no_speech': round(r['no_speech'], 3), 'logprob': round(r['logprob'], 3), 'text': r['text']})
            for p in pieces:
                self.add_line(dict(seg, start=p['start'], end=p['end']), p['text'], p['who'], p['me'])

    def add_line(self, seg, text, who, me):
        last = self.lines[-1] if self.lines else None
        piece = {'who': who, 'me': me, 'text': text, 'start': seg['start'], 'end': seg['end']}
        if last and last['who'] == who and seg['start'] - last['end'] < 2.0 and len(last['text']) < 500:
            last['text'] = (last['text'] + ' ' + text).strip(); last['end'] = seg['end']
            self.emit({'type': 'line_update', 'line': last})
        else:
            last = {'id': f'l{len(self.lines)}', 'tome': False, **piece}
            self.lines.append(last); self.emit({'type': 'line', 'line': last})
        self._append('transcript.jsonl', last)
        p = self.detector.seen(self.lines)
        if p:        # decide DECIDE_S after the turn's last word, unless the turn goes on or someone else takes it
            speed = max(0.1, float(getattr(self.args, 'speed', 1.0) or 1.0))
            delay = max(0.0, detect.DECIDE_S / speed - (time.time() - seg.get('audio_end_wall', time.time())))
            self.loop.call_later(delay, lambda v=p['v'], w=seg.get('audio_end_wall', time.time()): asyncio.ensure_future(self.decide(v, w)))

    def _asr_busy(self):
        """A segment is still being transcribed: its line may move or cancel the decision."""
        return self.inflight > 0 or self.segq.qsize() > 0

    def _floor_busy(self):
        """Someone other than the user is mid-sentence (speech not cut into a segment yet)."""
        return any(s.speaking for ch, s in self.segmenters.items() if ch != 'mic')

    POLL_S = 0.25

    async def decide(self, version, audio_end_wall, waited=0.0, floor_s=0.0):
        """Wait for the speech still on its way (its line moves or cancels the decision, as in the eval); someone
        else talking without a break for FLOOR_DROP_S has the floor: dropped, as the eval drops a turn when a third
        person starts before the decision (the same speaker going on would have moved it anyway). A slow ASR only
        waits longer (detect.wait_step)."""
        d = self.detector
        if not d.pending or d.pending['v'] != version: return
        step, floor_s = detect.wait_step(self._floor_busy(), self._asr_busy(), floor_s, waited, self.POLL_S)
        if step == 'drop':
            self.metrics['gate_dropped'] = self.metrics.get('gate_dropped', 0) + 1; return
        if step == 'wait':
            self.loop.call_later(self.POLL_S, lambda: asyncio.ensure_future(
                self.decide(version, audio_end_wall, waited + self.POLL_S, floor_s))); return
        r = d.decide(self.lines, version)
        if not r: return
        self.metrics['gate_turns'] += 1; self.metrics['gate_fired'] += int(r['fire'])
        line = self.lines[r['line']]
        self._append('metrics.jsonl', {'kind': 'gate', 'line': line['id'], 'fire': r['fire'], 'score': r['score'], 'why': r['why'],
                                       'cues': sorted(k for k, v in r['cues'].items() if v)})
        if not r['fire']: return
        piece = {k: line[k] for k in ('who', 'me', 'text', 'start', 'end')}
        job = {'line_id': line['id'], 'piece': piece, 'reason': ', '.join(r['why']), 'audio_end_wall': audio_end_wall, 'score': r['score'],
               'turn': [k for k in range(r['turn'], r['line'] + 1) if self.lines[k]['who'] == line['who']]}
        if self.card_busy >= MAX_CARD_CALLS: self.card_next = job
        else: asyncio.ensure_future(self.cards_for(job))

    # ---------- cards ----------
    async def cards_for(self, job):
        """One model call (raced against a twin when hedge > 1: the first to answer wins, the other is killed),
        streamed: the Answer card shows as soon as its lines are in, before Careful and Ask."""
        self.card_busy += 1
        try:
            line = next(l for l in self.lines if l['id'] == job['line_id'])
            q_text = job['piece']['text']
            mems = self.pack.retrieve(transcript_tail(self.lines, 90) + ' ' + q_text)
            # the same line growing (the speaker goes on) refreshes its own cards instead of opening a new group
            reuse = next((g for g in self.groups if g.get('line') == line['id']), None)
            recent = [g for g in self.groups if not g.get('fyi') and g is not reuse and line['start'] - g['t'] < 300]
            prev_me = [l for l in self.lines if l['me'] and l['end'] <= line['start'] + 0.5]
            hint = (f"the user spoke {max(0, int(line['start'] - prev_me[-1]['end']))} s before this line" if prev_me else '')
            if 'named' in job['reason']: hint = (hint + '; ' if hint else '') + 'the user was named'
            # the addressee model (stronger) decides "is it for the user?" (total deadline, any failure = yes); the cards
            # call only writes the cards, held until that decision
            verdict = None
            if self.addr_pool:
                turn = job.get('turn') or [self.lines.index(line)]
                aprompt = addressee.turn_prompt(self.lines[:turn[-1] + 1], turn, [job['reason']])
                verdict = asyncio.ensure_future(addressee.judge(self.addr_pool, aprompt))
            prompt = brain.card_prompt(line, self.lines, mems, recent, 'all', self.language_name(), hint, decided=bool(verdict))
            sc = {'g': None, 'shown': 0, 'first_card_ms': None, 'reuse': reuse}
            st, results = await addressee.gated_cards(self.cards_pool, prompt, max(1, int(self.cfg.get('hedge', 2))),
                                                      lambda d: self._show_cards(sc, line, d, job), verdict)
            main = results[st['winner'] if st['winner'] is not None else 0]
            tok = {'in': 0, 'cache_read': 0, 'cache_write': 0, 'out': 0}; cost = 0.0
            ar = st['addr']
            if ar:
                results = list(results) + [ar]
                self.metrics['addressee_calls'] += 1
                self.metrics['addressee_' + ar['status']] += 1        # yes / no / fail (a failure shows the cards)
                self.metrics['addressee_ms'].append(ar['ms'])
            for x in results:
                t = llm.tokens(x['usage'])
                for k in tok: tok[k] += t[k]
                cost += x['cost']
            for k in tok: self.metrics['tokens'][k] += tok[k]
            self.metrics['card_calls'] += 1; self.metrics['cost_usd'] += cost
            rec = {'kind': 'cards', 'line': line['id'], 'at': round(line['start'], 1), 'reason': job['reason'], 'text': line['text'],
                   'directed': bool(st['directed'] and st['gate'] != 'no'), 'shown': bool(sc['g']), 'ms': main['ms'],
                   'addressee': ({k: ar[k] for k in ('status', 'why', 'ms')} | {'reply': ar['text'][:200]} if ar else None), 'first_ms': main.get('first_ms'), 'api_ms': main['api_ms'],
                   'first_card_total_ms': sc['first_card_ms'], 'total_ms': int((time.time() - job['audio_end_wall']) * 1000),
                   'calls': len(results), 'tokens': tok, 'cost_usd': cost, 'memories': [s for s, _ in mems], 'reply': main['text'][:1500]}
            self._append('cards.jsonl', rec); self._append('metrics.jsonl', rec)
            if sc['g'] and ar and sc['g'].get('addr') != ar['status']:
                sc['g']['addr'] = ar['status']; self.emit({'type': 'group_reset', 'group': sc['g']})
            if sc['g']:
                self.metrics['card_ms'].append(main['ms']); self.metrics['card_api_ms'].append(main['api_ms'])
                sc['g']['tokens'] = tok
            self.emit({'type': 'metrics', 'metrics': self.metric_summary()})
        finally:
            self.card_busy -= 1
            nxt, self.card_next = self.card_next, None
            if nxt: asyncio.ensure_future(self.cards_for(nxt))

    def pack_topics(self):
        """The instance's own topic list from the local config (instances.<instance>.topics), never from the repo."""
        return config.instance_extra(self.args.instance).get('topics') or []

    def language_name(self):
        code = (self.args.language or self.cfg['language'] or 'en').lower()
        return {'en': 'English', 'pt': 'Portuguese', 'es': 'Spanish', 'de': 'German', 'fr': 'French', 'uk': 'Ukrainian',
                'pl': 'Polish'}.get(code, code)

    def _show_cards(self, st, line, d, job):
        if not d['cards']: return
        if st['g'] is None:
            total = int((time.time() - job['audio_end_wall']) * 1000)
            st['first_card_ms'] = total; self.metrics['card_total_ms'].append(total)
            q = brain.clean(d['question'] or line['text'])
            if st['reuse']:
                g = st['reuse']; g.update(q=q, latency_ms=total, score=job.get('score', 0.0))
                self.cards = [c for c in self.cards if c['g'] != g['id']]
                st['g'] = g; self.emit({'type': 'group_reset', 'group': g})
            else:
                g = {'id': f"g{len(self.groups)}", 'who': line['who'], 't': line['start'], 'q': q,
                     'fyi': False, 'line': line['id'], 'latency_ms': total, 'score': job.get('score', 0.0),
                     'addr': 'yes' if self.addr_pool else None}     # shown = the addressee said yes (a failure fixes it after)
                st['g'] = g; self.groups.append(g); self.emit({'type': 'group', 'group': g})
            line['tome'] = True; self.emit({'type': 'line_update', 'line': line})
        for c in d['cards'][st['shown']:]:
            card = {'id': f'c{len(self.cards)}', 'g': st['g']['id'], 'k': c['kind'], 'x': brain.clean(c['text']),
                    'why': brain.clean(c['why']), 'chips': c['based_on'][:5]}
            self.cards.append(card); self.emit({'type': 'card', 'card': card})
        st['shown'] = len(d['cards'])

    # ---------- notes ----------
    async def notes_loop(self):
        every = float(self.cfg['notes_every_s'])
        while not self.ended:
            await asyncio.sleep(every / max(1.0, min(getattr(self.args, 'speed', 1.0), 4.0)))
            await self.update_notes()

    async def update_notes(self):
        new = [l for l in self.lines if l['end'] > self.notes_since]
        if self.notes_busy or len(new) < 2: return
        self.notes_busy = True
        try:
            upto = self.lines[-1]['end']
            r = await self.notes_pool.ask(brain.notes_prompt(self.notes, self.lines, max(0.0, self.notes_since - 30), self.language_name()), timeout=120)
            d = llm.parse_json(r['text']) or {}
            tok = llm.tokens(r['usage'])
            for k in tok: self.metrics['tokens'][k] += tok[k]
            self.metrics['notes_calls'] += 1; self.metrics['notes_ms'].append(r['ms']); self.metrics['cost_usd'] += r['cost']
            self._append('metrics.jsonl', {'kind': 'notes', 'ms': r['ms'], 'api_ms': r['api_ms'], 'tokens': tok, 'cost_usd': r['cost'],
                                           'ok': bool(d)})
            if 'items' in d:
                self.notes, self.next_note, added = brain.merge_notes(self.notes, d.get('items'), self.next_note, d.get('drop'))
                self.notes_since = upto
                with open(os.path.join(self.dir, 'notes.json'), 'w', encoding='utf-8') as f: json.dump(self.notes, f, ensure_ascii=False, indent=1)
                self.emit({'type': 'notes', 'notes': self.notes, 'added': added})
            f = d.get('focus') if isinstance(d.get('focus'), dict) else None
            if f:
                ask = brain.clean(f.get('ask') or '')
                self.focus = {'topic': brain.clean(f.get('topic') or '') or self.focus.get('topic', ''),
                              'ask': {'text': ask, 'why': brain.clean(f.get('ask_why') or '')} if ask else None}
                self.emit({'type': 'focus', 'focus': self.focus})
            for it in d.get('fyi') or []:
                if not (it.get('text') or '').strip(): continue
                at = brain.parse_at(it.get('at')) or upto
                gid = f"g{len(self.groups)}"
                g = {'id': gid, 'who': it.get('who') or '', 't': at, 'q': brain.clean(it['text']), 'fyi': True}
                card = {'id': f'c{len(self.cards)}', 'g': gid, 'k': 'note', 'x': brain.clean(it['text']), 'why': 'heard in the meeting',
                        'chips': ['transcript']}
                self.groups.append(g); self.cards.append(card)
                self.emit({'type': 'group', 'group': g}); self.emit({'type': 'card', 'card': card})
            self.emit({'type': 'metrics', 'metrics': self.metric_summary()})
        finally:
            self.notes_busy = False

    # ---------- lifecycle ----------
    async def prepare(self):
        self.loop = asyncio.get_running_loop(); self.segq = asyncio.Queue()
        before = None
        if self.kind == 'replay':
            before = recording_time(self.args.file)
        self.pack = context.Pack(self.args.instance, before=before, online=not self.args.offline)
        t0 = time.time()
        if before and not getattr(self.args, 'ready_pack', False):
            text = await self.loop.run_in_executor(None, self.pack.build)        # a replay builds "as of the recording"
            how = 'built as of the recording'
        else:
            got = ready.load(self.args.instance)
            if got:
                text, self.pack.sources, age = got; self.pack.text = text
                how = f'ready pack, {int(age // 60)} min old'
            else:                                  # missing or older than 15 min: incremental top-up (a full build if none)
                had = bool(ready.load_meta(self.args.instance).get('parts'))
                self.pack, changed, _, _, _ = await self.loop.run_in_executor(
                    None, lambda: ready.refresh(self.args.instance, online=not self.args.offline))
                text = self.pack.text
                how = ('ready pack topped up' if had else 'built (no ready pack)') + (f', changed: {", ".join(changed)}' if changed else '')
        self._append('metrics.jsonl', {'kind': 'context', 'ms': int((time.time() - t0) * 1000), 'how': how})
        print(f'Context: {how} in {int((time.time() - t0) * 1000)} ms', flush=True)
        sysfile = os.path.join(self.dir, 'context.md')
        with open(sysfile, 'w', encoding='utf-8') as f: f.write(brain.system_prompt(self.user, self.aliases, text))
        self.cards_pool = llm.Pool(self.cfg['cards_model'], sysfile, MAX_CARD_CALLS * max(1, int(self.cfg.get('hedge', 2))))
        topics = detect.topic_terms(text, self.aliases) | {detect.fold(t) for t in self.pack_topics()}
        # the topic map goes to the model stage; in the deterministic stage only with gate_topics (measured: more
        # alerts, same recall), otherwise only the named things the user brings up in the meeting count there
        self.detector = detect.Detector(self.aliases, topics if self.cfg.get('gate_topics') else (),
                                        detect.meeting_type(self.title))
        amodel = addressee.model_name(self.cfg)
        if amodel:     # small fixed system prompt (names and topic map), not the context pack
            afile = os.path.join(self.dir, 'addressee.md')
            with open(afile, 'w', encoding='utf-8') as f: f.write(addressee.system_prompt(self.user, self.aliases, topics))
            self.addr_pool = llm.Pool(amodel, afile, MAX_CARD_CALLS)
            await self.addr_pool.warm()
        self.notes_pool = llm.Pool(self.cfg['notes_model'], sysfile, 1)
        await self.cards_pool.warm(); await self.notes_pool.warm()
        # one tiny call writes the context pack into the prompt cache, so the first real card reads it from there
        if not getattr(self.args, 'no_prime', False):
            await self.cards_pool.ask('Reply with OK.', timeout=60)
        mmc = config.mm()
        terms = ', '.join([self.user] + self.pack.asr_terms)
        def load():
            self.asr = asr.Transcriber(self.cfg['asr_model'], self.cfg['asr_compute'], self.args.language or self.cfg['language'],
                                       prompt=f'Meeting with {terms}.')
            self.voices = asr.Voices(mmc['voices'], self.user, mmc['config'].get('hf_token_path') or '~/.cache/huggingface/token',
                                     use_embeddings=(self.kind == 'replay' or not self.args.no_voiceprint))
        await self.loop.run_in_executor(self.exec, load)

    async def status_loop(self):
        while not self.ended:
            lv = {ch: round(s.level, 4) for ch, s in self.segmenters.items()}
            self.emit({'type': 'status', 'pos': round(getattr(self.source, 'pos', 0.0), 1), 'paused': self.paused, 'level': lv,
                       'connected': getattr(self.source, 'connected', None).is_set() if hasattr(self.source, 'connected') else True})
            await asyncio.sleep(0.25)

    async def finish(self):
        if self.ended: return
        for s in self.segmenters.values(): s.flush()
        await asyncio.sleep(0.2); self.segq.put_nowait(None)
        await self.consumer
        for _ in range(120):
            if not self.card_busy and not self.card_next: break
            await asyncio.sleep(0.5)
        self.notes_busy = False
        await self.update_notes()
        self.ended = True
        self.write_final()
        self.emit({'type': 'ended', 'metrics': self.metric_summary(), 'notes': self.notes})
        await self.cards_pool.close(); await self.notes_pool.close()
        if self.addr_pool: await self.addr_pool.close()

    def write_final(self):
        with open(os.path.join(self.dir, 'transcript.md'), 'w', encoding='utf-8') as f:
            f.write(f'# Transcript: {self.title}\n\n_Live transcript from the meeting copilot (ASR {self.cfg["asr_model"]}, '
                    f'"You" = {self.user})._\n\n')
            for l in self.lines:
                f.write(f"**[{brain.mmss(l['start'])}] {l['who']}:** {l['text']}\n\n")
        with open(os.path.join(self.dir, 'notes.md'), 'w', encoding='utf-8') as f:
            f.write(brain.notes_markdown(self.title, self.notes))
        with open(os.path.join(self.dir, 'summary.json'), 'w', encoding='utf-8') as f:
            json.dump({'title': self.title, 'kind': self.kind, 'metrics': self.metric_summary(), 'groups': self.groups}, f, indent=1)

    async def run(self, start_source):
        await self.prepare()
        self.source = start_source(self.sink)
        self.consumer = asyncio.ensure_future(self.consume())
        asyncio.ensure_future(self.notes_loop()); asyncio.ensure_future(self.status_loop())
        while not self.source.done.is_set() and not self.ended:
            await asyncio.sleep(0.5)
        await self.finish()


def transcript_tail(lines, secs):
    if not lines: return ''
    end = lines[-1]['end']
    return ' '.join(l['text'] for l in lines if l['end'] >= end - secs)


def recording_time(path):
    """OBS names files 'YYYY-MM-DD HH-MM-SS': the meeting's start, so the context pack leaves out what came later."""
    import re
    m = re.search(r'(\d{4}-\d{2}-\d{2})[ _T](\d{2})-(\d{2})-(\d{2})', os.path.basename(path))
    if m: return datetime.strptime(f'{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}', '%Y-%m-%d %H:%M:%S')
    return datetime.fromtimestamp(os.path.getmtime(path))


# ---------- web ----------
def is_loopback(remote):
    """True for 127.0.0.0/8 and ::1: the live transcript is private, nothing else on the network may read or drive it."""
    try: return ipaddress.ip_address((remote or '').split('%')[0]).is_loopback
    except ValueError: return False


def host_ok(host):
    """The Host header must name the loopback (blocks DNS rebinding from a page in the browser)."""
    try: name = urlsplit('//' + (host or '')).hostname
    except ValueError: return False
    return name in ('localhost', '127.0.0.1', '::1')


@web.middleware
async def guard(req, handler):
    if not is_loopback(req.remote): raise web.HTTPForbidden(text='loopback only')
    if not host_ok(req.headers.get('Host')): raise web.HTTPForbidden(text='bad host')
    if req.method == 'POST' and req.content_type != 'application/json':
        raise web.HTTPUnsupportedMediaType(text='application/json required')
    return await handler(req)


async def json_body(req):
    """The JSON object of a POST; anything else is the client's mistake (400), never a 500."""
    try: d = await req.json()
    except (ValueError, UnicodeDecodeError): raise web.HTTPBadRequest(text='invalid JSON')
    if not isinstance(d, dict): raise web.HTTPBadRequest(text='JSON object expected')
    return d


def make_app(sess):
    app = web.Application(middlewares=[guard])

    async def index(_):
        return web.FileResponse(os.path.join(WEB, 'panel.html'), headers={'Cache-Control': 'no-store'})

    async def events(req):
        resp = web.StreamResponse(headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache'})
        await resp.prepare(req)
        q = asyncio.Queue(); sess.subs.add(q)
        try:
            await resp.write(f"data: {json.dumps(sess.snapshot(), ensure_ascii=False)}\n\n".encode())
            while True:
                try: data = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError: data = None
                await resp.write((f'data: {data}\n\n' if data else ': ping\n\n').encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            sess.subs.discard(q)
        return resp

    async def state(_):
        return web.json_response(sess.snapshot())

    async def pause(req):
        d = await json_body(req)
        sess.paused = bool(d.get('paused'))
        if sess.source:
            (sess.source.paused.set if sess.paused else sess.source.paused.clear)()
        sess.emit({'type': 'paused', 'paused': sess.paused})
        return web.json_response({'paused': sess.paused})

    async def stop(_):
        if sess.source: sess.source.stopped.set()
        return web.json_response({'stopping': True})

    async def notes_md(_):
        return web.Response(text=brain.notes_markdown(sess.title, sess.notes), content_type='text/markdown')

    async def set_prefs(req):
        d = await json_body(req)
        sess.prefs = monitor.settings(sess.prefs, **{k: d.get(k) for k in ('threshold', 'mode')})
        sess.emit({'type': 'settings', 'settings': sess.prefs})
        return web.json_response(sess.prefs)

    async def dismiss(req):
        gid = str((await json_body(req)).get('group') or '')
        if gid and gid not in sess.dismissed: sess.dismissed.append(gid)
        sess.emit({'type': 'settings', 'settings': sess.prefs})
        return web.json_response({'dismissed': sess.dismissed})

    app.router.add_post('/api/settings', set_prefs); app.router.add_post('/api/dismiss', dismiss)
    app.router.add_get('/', index); app.router.add_get('/events', events); app.router.add_get('/api/state', state)
    app.router.add_post('/api/pause', pause); app.router.add_post('/api/stop', stop); app.router.add_get('/notes.md', notes_md)
    return app
