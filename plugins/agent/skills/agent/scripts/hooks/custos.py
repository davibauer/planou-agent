#!/usr/bin/env python3
"""Hook custos: token cost and time per task, read from the Claude Code transcripts of the instance's sessions.

The agent itself spends no tokens (the runner ticks without the model); what costs is what the session does when it
wakes up (tick triage) and the work the user asks for. So cost is attributed per TURN of the session:

  - a turn starts at a user entry that is not only tool results (a prompt, a background-task notification, a wakeup);
  - a turn whose opening text matches one of `gatilhos_triagem` (the runner's notifications, the fallback wakeup) goes
    to the bucket "triagem" (named "vigia (triagem)" up to 0.18.x: days stored before are read as "triagem");
  - any other turn goes to the task marked active when it started: `--tarefa HAK-61` (from now on) or
    `--tarefa HAK-61@14:05` (from that time today); `--tarefa -` closes the current one. No marker = "(sem tarefa)";
  - a subagent (subagents/agent-*.jsonl next to the session) goes to the task active when it started, unless it was
    assigned one (`--tarefa X --agente <agent id or part of its description>`); its tokens add to the task, its time
    is a separate column (it runs in parallel with the session).

Per task: active time (sum over turns of the time from the opening entry to the last model message of the turn),
turns, tokens by kind and the cost at the configured prices (per million tokens; cache writes use the 5 min / 1 h
multipliers). Markers live in data/custos-marcas.jsonl (append-only, never in the state file the runner writes).

Config ("ganchos": [{"tipo": "custos", ...}]):
  transcripts     list of Claude Code project folders (~/.claude/projects/<encoded cwd>) whose sessions belong here
  sessoes         optional list of session ids to keep (default: every session in those folders)
  gatilhos_triagem regexes on the opening text of a turn that mark tick triage (old key gatilhos_vigia still read)
  precos          {model id: {"in": $/Mtok, "out": ..., "cache_read": ...}}; cache writes = in x 1.25 (5 min) / x 2 (1 h)
  nota_preco      one line printed under the report (e.g. that a subscription does not bill per token)
  unidade         "tokens" (default: the cost shown is the tokens the model processed — input, output, cache written
                  and read) or "usd" (also shows the price-table value; 24/09/2026: "o custo deve ser em tokens")

Stored with the tasks (24/09/2026, "o custo em tokens e o tempo poderia ficar junto no state"): every live tick
recomputes today (and yesterday until 03:00) into state['custos']['dias'][AAAA-MM-DD] = {tarefa: totals}, which is
also how a marker added after the fact (`@HH:MM`) is picked up; older days are recomputed only on request
(`--custos recalcular PERIODO`, a request file the next tick consumes — the CLI never writes the state the runner
owns). The daily page reads from there: each task line shows its accumulated time and cost, and a footer shows the day.

CLI: --tarefa X[@HH:MM] | --tarefa -   ·   --custos [hoje|ontem|semana|mes|AAAA-MM-DD[..AAAA-MM-DD]] [--csv arquivo]
     --custos recalcular PERIODO
"""
import os, re, json, glob, csv
from datetime import datetime, timezone, timedelta

import core, paths
from adapters import Gancho as Base

SEM_TAREFA = '(sem tarefa)'
TRIAGEM = 'triagem'
TRIAGEM_LEGADO = 'vigia (triagem)'          # label up to 0.18.x, still in state['custos'] of older days
TRIAGEM_TODOS = (TRIAGEM, TRIAGEM_LEGADO)


def rotulo(k):
    """Task label as reported: the old triage label becomes the new one."""
    return TRIAGEM if k == TRIAGEM_LEGADO else k
TURNO_MAX = timedelta(hours=2)          # a turn longer than this is idle time counted by mistake: capped


def _ts(x):
    try: return datetime.fromisoformat(str(x).replace('Z', '+00:00'))
    except (TypeError, ValueError): return None


def _texto(content):
    """Opening text of a user entry, or None when it only carries tool results."""
    if isinstance(content, str): return content
    if not isinstance(content, list): return None
    t = [b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text']
    return '\n'.join(t) if t else None


def _uso(entries):
    """Usage of a list of assistant entries, one count per message id (a message split in blocks repeats its usage)."""
    vistos, out = set(), []
    for e in entries:
        m = e.get('message') or {}
        mid = m.get('id') or e.get('requestId') or e.get('uuid')
        if mid in vistos or not m.get('usage'): continue
        vistos.add(mid); out.append((m.get('model') or '?', m['usage']))
    return out


GUARDA_DIAS = 120


def _limpa(linhas):
    return {k: {**v, 'sem_preco': sorted(v['sem_preco'])} for k, v in linhas.items()}


def _tok(x):
    return x.get('tokens', (x.get('entrada') or 0) + (x.get('cache_grava') or 0) + (x.get('cache_le') or 0) + (x.get('saida') or 0))


def _mt(n):
    return f'{n / 1e6:.1f}M' if n >= 1e6 else f'{n / 1e3:.0f}k'


def totais(s, uid):
    """Accumulated time and tokens of one task over every stored day: (active s, subagent s, tokens, usd) or None."""
    tot, ach = [0.0, 0.0, 0, 0.0], False
    for dia in ((s.get('custos') or {}).get('dias') or {}).values():
        x = dia.get(uid)
        if x: ach = True; tot[0] += x['segundos']; tot[1] += x['agente_segundos']; tot[2] += _tok(x); tot[3] += x.get('usd', 0)
    return tuple(tot) if ach else None


def _hm(sg):
    sg = int(sg); return f'{sg // 3600}h{sg % 3600 // 60:02d}' if sg >= 3600 else f'{max(sg // 60, 1)}min'


def extra_linha(s, uid, en=False, usd=False):
    """' · ⏱ 19min · 21.4M tokens' after a task line on the daily page ('' when nothing was recorded for it)."""
    t = totais(s, uid)
    if not t: return ''
    seg, ag, tok, val = t
    tempo = _hm(seg) + (f' + {_hm(ag)} agent' if ag and en else f' + {_hm(ag)} agente' if ag else '')
    return f' · ⏱ {tempo} · {_mt(tok)} tokens' + ((f' · ${val:.2f}' if en else f' · US$ {val:.2f}'.replace('.', ',')) if usd else '')


def rodape(s, data, en=False, usd=False):
    """Footer of the day's section: the day's tokens, the agent's own triage and what had no task."""
    dia = ((s.get('custos') or {}).get('dias') or {}).get(data)
    if not dia: return []
    tot = sum(_tok(x) for x in dia.values()); seg = sum(x['segundos'] for x in dia.values())
    tri, sem = sum(_tok(dia.get(k) or {}) for k in TRIAGEM_TODOS), _tok(dia.get(SEM_TAREFA) or {})
    val = sum(x.get('usd', 0) for x in dia.values())
    if en:
        return ['', f'*Cost of the day: {_mt(tot)} tokens in {_hm(seg)} of model time — agent triage {_mt(tri)}, '
                    f'no task {_mt(sem)}' + (f' (≈ ${val:.2f} at API prices)' if usd else '') + '*']
    return ['', f'*Custo do dia: {_mt(tot)} tokens em {_hm(seg)} de modelo — triagem do agent {_mt(tri)}, sem tarefa '
                f'{_mt(sem)}' + (f' (≈ US$ {val:.2f} em preço de API)'.replace('.', ',') if usd else '') + '*']


class Gancho(Base):
    tipo = 'custos'
    json_key = None

    def configura(self):
        self.dirs = [os.path.expanduser(d) for d in self.cfg.get('transcripts') or []]
        self.sessoes = set(self.cfg.get('sessoes') or [])
        self.gatilhos = [re.compile(g) for g in self.cfg.get('gatilhos_triagem') or self.cfg.get('gatilhos_vigia') or []]
        self.precos = self.cfg.get('precos') or {}
        self.nota = self.cfg.get('nota_preco', '')
        self.usd = self.cfg.get('unidade') == 'usd'

    # ---- markers -------------------------------------------------------------------------------------------------
    def _arq(self): return os.path.join(paths.DATA_DIR, 'custos-marcas.jsonl')

    def _linhas(self):
        try: linhas = open(self._arq(), encoding='utf-8').read().splitlines()
        except FileNotFoundError: return []
        out = []
        for l in linhas:
            try: out.append(json.loads(l))
            except ValueError: continue
        return out

    def marcas(self):
        out = [(_ts(d.get('ts')), d.get('tarefa')) for d in self._linhas() if not d.get('agente') and d.get('tarefa')]
        return sorted((t, x) for t, x in out if t)

    def agentes_fixos(self):
        """[(agent id or description fragment, task)], the latest assignment wins."""
        return [(d['agente'], d['tarefa']) for d in self._linhas() if d.get('agente') and d.get('tarefa')]

    def marca(self, arg, agora, agente=None):
        tarefa, _, hm = arg.partition('@')
        ts = agora
        if hm:
            h, m = (int(x) for x in hm.split(':'))
            ts = agora.astimezone(core.BRT).replace(hour=h, minute=m, second=0, microsecond=0).astimezone(timezone.utc)
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        with open(self._arq(), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': ts.isoformat(), 'tarefa': tarefa.strip() or '-', **({'agente': agente} if agente else {})}) + '\n')
        return ts, tarefa.strip() or '-'

    def tarefa_em(self, marcas, t):
        atual = None
        for mt, x in marcas:
            if mt <= t: atual = x
            else: break
        return SEM_TAREFA if not atual or atual == '-' else atual

    # ---- transcripts -----------------------------------------------------------------------------------------------
    def _arquivos(self):
        for d in self.dirs:
            for f in glob.glob(os.path.join(d, '*.jsonl')):
                sid = os.path.basename(f)[:-6]
                if self.sessoes and sid not in self.sessoes: continue
                yield sid, f, sorted(glob.glob(os.path.join(d, sid, 'subagents', 'agent-*.jsonl')))

    def custo(self, modelo, u):
        p = self.precos.get(modelo)
        if not p: return None
        cc = u.get('cache_creation') or {}
        w5, w1 = cc.get('ephemeral_5m_input_tokens'), cc.get('ephemeral_1h_input_tokens')
        if w5 is None and w1 is None: w5, w1 = u.get('cache_creation_input_tokens') or 0, 0
        return ((u.get('input_tokens') or 0) * p['in'] + (w5 or 0) * p['in'] * 1.25 + (w1 or 0) * p['in'] * 2
                + (u.get('cache_read_input_tokens') or 0) * p.get('cache_read', p['in'] * 0.1)
                + (u.get('output_tokens') or 0) * p['out']) / 1e6

    def _soma(self, linha, uso):
        for modelo, u in uso:
            cc = u.get('cache_creation_input_tokens') or 0
            linha['entrada'] += u.get('input_tokens') or 0
            linha['cache_grava'] += cc
            linha['cache_le'] += u.get('cache_read_input_tokens') or 0
            linha['saida'] += u.get('output_tokens') or 0
            linha['tokens'] += ((u.get('input_tokens') or 0) + cc + (u.get('cache_read_input_tokens') or 0)
                                + (u.get('output_tokens') or 0))
            c = self.custo(modelo, u)
            if c is None: linha['sem_preco'].add(modelo)
            else: linha['usd'] += c

    def relatorio(self, ini, fim):
        """{tarefa: linha} for turns that started in [ini, fim)."""
        marcas, fixos = self.marcas(), self.agentes_fixos()
        linhas = {}
        def linha(t):
            return linhas.setdefault(t, {'tarefa': t, 'turnos': 0, 'segundos': 0.0, 'agentes': 0, 'agente_segundos': 0.0,
                                         'entrada': 0, 'cache_grava': 0, 'cache_le': 0, 'saida': 0, 'tokens': 0,
                                         'usd': 0.0, 'sem_preco': set()})
        for sid, arq, agentes in self._arquivos():
            turno = None                      # (inicio, tarefa, [assistant entries])
            def fecha(tn):
                if not tn: return
                inicio, tarefa, ents = tn
                if not ents or not (ini <= inicio < fim): return
                fim_t = max((x for x in (_ts(e.get('timestamp')) for e in ents) if x), default=inicio)
                ln = linha(tarefa); ln['turnos'] += 1
                ln['segundos'] += min(fim_t - inicio, TURNO_MAX).total_seconds()
                self._soma(ln, _uso(ents))
            try: f = open(arq, encoding='utf-8')
            except OSError: continue
            with f:
                for l in f:
                    try: e = json.loads(l)
                    except ValueError: continue
                    tipo = e.get('type')
                    if tipo == 'user' and not e.get('isSidechain'):
                        txt = _texto((e.get('message') or {}).get('content'))
                        t = _ts(e.get('timestamp'))
                        if txt is None or not t: continue
                        fecha(turno)
                        triagem = any(g.search(txt) for g in self.gatilhos)
                        turno = (t, TRIAGEM if triagem else self.tarefa_em(marcas, t), [])
                    elif tipo == 'assistant' and turno is not None and not e.get('isSidechain'):
                        turno[2].append(e)
                fecha(turno)
            for ag in agentes:
                ents, ts = [], []
                try:
                    for l in open(ag, encoding='utf-8'):
                        try: e = json.loads(l)
                        except ValueError: continue
                        t = _ts(e.get('timestamp'))
                        if t: ts.append(t)
                        if e.get('type') == 'assistant': ents.append(e)
                except OSError: continue
                if not ts or not (ini <= ts[0] < fim): continue
                aid = os.path.basename(ag)[len('agent-'):-len('.jsonl')]
                try: desc = (json.load(open(ag[:-len('.jsonl')] + '.meta.json')).get('description') or '').lower()
                except (OSError, ValueError): desc = ''
                fixo = [x for k, x in fixos if k == aid or (k.lower() in desc and k.strip())]
                ln = linha(fixo[-1] if fixo else self.tarefa_em(marcas, ts[0]))
                ln['agentes'] += 1; ln['agente_segundos'] += (max(ts) - ts[0]).total_seconds()
                self._soma(ln, _uso(ents))
        return linhas

    # ---- every live tick: today (and yesterday before 03:00) into the state -----------------------------------------
    def _pedido(self): return os.path.join(paths.DATA_DIR, 'custos-recalcular.json')

    def run(self, ctx):
        if ctx.dry: return None
        agora = datetime.now(timezone.utc); local = agora.astimezone(core.BRT)
        dias = {local.date()}
        if local.hour < 3: dias.add(local.date() - timedelta(days=1))
        try:
            ped = json.load(open(self._pedido(), encoding='utf-8'))
            a, b = datetime.strptime(ped['de'], '%Y-%m-%d').date(), datetime.strptime(ped['ate'], '%Y-%m-%d').date()
            while a <= b: dias.add(a); a += timedelta(days=1)
            os.remove(self._pedido())
        except (FileNotFoundError, ValueError, KeyError): pass
        st = ctx.s.setdefault('custos', {}).setdefault('dias', {})
        for d in sorted(dias):
            ini = datetime(d.year, d.month, d.day, tzinfo=core.BRT)
            st[d.isoformat()] = _limpa(self.relatorio(ini, ini + timedelta(days=1)))
        for k in sorted(st)[:-GUARDA_DIAS]: st.pop(k)
        return None

    # ---- CLI -------------------------------------------------------------------------------------------------------
    def args(self, ap):
        ap.add_argument('--tarefa', metavar='X[@HH:MM]', help='marca a tarefa ativa (custo e tempo vao para ela); - fecha')
        ap.add_argument('--custos', nargs='*', metavar='PERIODO',
                        help='custo em tokens e tempo por tarefa: hoje|ontem|semana|mes|AAAA-MM-DD[..AAAA-MM-DD]; '
                             'recalcular PERIODO = refaz esses dias no proximo tick')
        ap.add_argument('--csv', metavar='ARQ', help='com --custos: grava tambem em CSV')
        ap.add_argument('--agente', metavar='ID|DESCRICAO', help='com --tarefa: atribui um subagente inteiro a tarefa')

    def periodo(self, p, agora):
        d = agora.astimezone(core.BRT).replace(hour=0, minute=0, second=0, microsecond=0)
        if p in (None, 'hoje'): return d, d + timedelta(days=1)
        if p == 'ontem': return d - timedelta(days=1), d
        if p == 'semana': return d - timedelta(days=d.weekday()), d + timedelta(days=1)
        if p == 'mes': return d.replace(day=1), d + timedelta(days=1)
        a, _, b = p.partition('..')
        ia = datetime.strptime(a, '%Y-%m-%d').replace(tzinfo=core.BRT)
        ib = datetime.strptime(b or a, '%Y-%m-%d').replace(tzinfo=core.BRT) + timedelta(days=1)
        return ia, ib

    def cli(self, a, ctx):
        agora = datetime.now(timezone.utc)
        if a.tarefa and a.agente:
            self.marca(a.tarefa, agora, a.agente)
            print(f'custos: subagente "{a.agente}" -> {a.tarefa}'); return True
        if a.tarefa:
            ts, t = self.marca(a.tarefa, agora)
            print(f'custos: tarefa ativa = {t if t != "-" else "(nenhuma)"} desde {ts.astimezone(core.BRT).strftime("%d/%m %H:%M")}')
            return True
        if a.custos is None: return False
        a.custos = ':'.join(a.custos) if a.custos[:1] == ['recalcular'] and len(a.custos) > 1 else (a.custos[0] if a.custos else 'hoje')
        if a.custos == 'recalcular':
            raise SystemExit('uso: --custos recalcular AAAA-MM-DD[..AAAA-MM-DD] (ver --tarefa para marcar antes)')
        if a.custos.startswith('recalcular:'):
            ini, fim = self.periodo(a.custos.split(':', 1)[1], agora)
            os.makedirs(paths.DATA_DIR, exist_ok=True)
            json.dump({'de': ini.date().isoformat(), 'ate': (fim - timedelta(days=1)).date().isoformat()},
                      open(self._pedido(), 'w', encoding='utf-8'))
            print(f'custos: {ini:%d/%m} a {fim - timedelta(days=1):%d/%m} recalculados no proximo tick'); return True
        ini, fim = self.periodo(a.custos, agora)
        # from the state (what the ticks stored); a day the state does not have is computed now, without writing
        guard = (ctx.s.get('custos') or {}).get('dias') or {}
        juntos = {}
        d = ini
        while d < fim:
            dia = guard.get(d.date().isoformat())
            if dia is None: dia = _limpa(self.relatorio(d, d + timedelta(days=1)))
            for k, v in dia.items():
                k = rotulo(k)
                j = juntos.setdefault(k, {'tarefa': k, 'turnos': 0, 'segundos': 0.0, 'agentes': 0, 'agente_segundos': 0.0,
                                          'entrada': 0, 'cache_grava': 0, 'cache_le': 0, 'saida': 0, 'tokens': 0, 'usd': 0.0,
                                          'sem_preco': set()})
                for c in ('turnos', 'segundos', 'agentes', 'agente_segundos', 'entrada', 'cache_grava', 'cache_le', 'saida', 'usd'):
                    j[c] += v.get(c, 0)
                j['tokens'] += _tok(v)
                j['sem_preco'] |= set(v['sem_preco'])
            d += timedelta(days=1)
        linhas = sorted(juntos.values(), key=lambda x: -x['tokens'])
        tot = {k: sum(x[k] for x in linhas) for k in ('turnos', 'segundos', 'agentes', 'agente_segundos', 'entrada',
                                                       'cache_grava', 'cache_le', 'saida', 'tokens', 'usd')}
        def hm(s): s = int(s); return f'{s // 3600}h{s % 3600 // 60:02d}' if s >= 3600 else f'{s // 60}min'
        def mt(n): return f'{n / 1e6:.1f}M' if n >= 1e6 else f'{n / 1e3:.0f}k'
        fim_d = (fim - timedelta(days=1)).strftime('%d/%m')
        print(f'== CUSTOS {paths.NOME} ({ini.strftime("%d/%m")}{"" if fim_d == ini.strftime("%d/%m") else " a " + fim_d})')
        print(f'{"tarefa":<22} {"tempo":>7} {"turnos":>6} {"agentes":>10} {"entrada":>8} {"saida":>7} {"cache lido":>10} '
              f'{"cache grav.":>11} {"TOKENS":>8}' + (f' {"US$":>8}' if self.usd else ''))
        for x in linhas + [dict(tot, tarefa='TOTAL', sem_preco=set().union(*(y['sem_preco'] for y in linhas)))]:
            ag = f'{x["agentes"]} ({hm(x["agente_segundos"])})' if x['agentes'] else '-'
            print(f'{x["tarefa"][:22]:<22} {hm(x["segundos"]):>7} {x["turnos"]:>6} {ag:>10} {mt(x["entrada"]):>8} {mt(x["saida"]):>7} '
                  f'{mt(x["cache_le"]):>10} {mt(x["cache_grava"]):>11} {mt(x["tokens"]):>8}'
                  + (f' {x["usd"]:>8.2f}' if self.usd else '')
                  + (f'  (sem preco: {", ".join(sorted(x["sem_preco"]))})' if self.usd and x['sem_preco'] else ''))
        if self.usd and self.nota: print(self.nota)
        if a.csv:
            with open(os.path.expanduser(a.csv), 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(['instancia', 'tarefa', 'inicio', 'fim', 'tempo_ativo_min', 'turnos', 'agentes', 'tempo_agentes_min',
                            'tokens_entrada', 'tokens_cache_gravado', 'tokens_cache_lido', 'tokens_saida', 'tokens_total']
                           + (['usd'] if self.usd else []))
                for x in linhas:
                    w.writerow([paths.NOME, x['tarefa'], ini.date().isoformat(), (fim - timedelta(days=1)).date().isoformat(),
                                round(x['segundos'] / 60, 1), x['turnos'], x['agentes'], round(x['agente_segundos'] / 60, 1),
                                x['entrada'], x['cache_grava'], x['cache_le'], x['saida'], x['tokens']]
                               + ([round(x['usd'], 4)] if self.usd else []))
            print(f'csv: {a.csv}')
        return True
