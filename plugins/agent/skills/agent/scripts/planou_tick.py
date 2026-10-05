#!/usr/bin/env python3
"""Planou for work-watch: each live tick reports the instance's task list to Planou and takes back what the person did.

Planou (the team's to-do app, agents as employees) talks to the agent through watch_core.planou. This module is the
work-watch side, called by watch.py in two steps of every live tick (never with --dry, --since, --so or in test mode):

  before(s)            before the hooks: the person's changes (complete, reopen, edit, answers) land in the state, so
                       the reminders and the Notion page of this same tick already see them
  after(ctx, ganchos)  after the hooks (the task engine is configured by then): the full task list (the same list as
                       the `## <SIGLA>` section of the daily page), the asks that come from the state (decisions and
                       what the agent needs), the tools list (ferramentas: sources, broken ones, credentials and their
                       expiry, for the Ferramentas tab) and the sign of life with the broken sources

Completing, reopening or editing a task in Planou has the same effect as the box on the daily page:
  completed ('Feito')  action done / pending item resolved (como "concluida no Planou")
  no action            pending item resolved as "sem ação no Planou" (it stays out of "Feito hoje")
  discarded            pending item ignored; action done with the note "descartada no Planou"
  reopened             action or pending item open again (the ticked box on today's page no longer closes it)
  edited               title and due date become the task's manual title and due date; "in progress" puts it in the
                       plan of the day; back to "to do" takes it out
Tasks from other sources (PRs, work items) close at the source: Planou keeps the person's change (conflict) until the
source catches up.

Answers ("Precisa de você"): "Enviei" on a draft tied to a pending item resolves it; a decision answered in Planou is
recorded like the option ticked on the page (the tick prints `-- DECISAO ...`, with the person's own words when the answer
was free; the session acts on it, the agent never does anything by itself). Every decision accepts a free answer, with
or without options: a question to the person never goes up as an alert, whose only button is "Ciente".

Config (instance config.json; missing block = off):
  "planou": {"project": "EMA", "confidentiality": "minimum" | "title" | "detail", "drafts_in_planou": false,
             "publish": true, "links": [{"pattern": "...", "url": "https://.../{1}", "label": "..."}]}
The link rules Planou uses to turn the agent's ids into links (Links automáticos do agente) are the "links" of the
tarefas hook (issue_url for SIGLA-N keys, repo_regex + pr_url for "repo #n") plus the optional "planou.links" above
(e.g. "#12345" to a work item, "!45" to a merge request). A rule written in code by an instance adapter (modulo) cannot
be sent: write it in "planou.links".
plus "planou.enabled" and "planou.base_url" in ~/.config/watch-core/config.json and the key in
~/.config/work-watch/<instance>/secrets/planou.env. The Notion page keeps being published (transition).

Output: a `== PLANOU` block only with what the person did. A Planou failure never prints a bare `AVISO (planou)` line
(runners before the light loop would wake the session for it): it goes to cache/planou/avisos.log, and after 60 minutes
of failure the tick prints `FONTE QUEBRADA (planou)`, which every runner already wakes for at most once an hour.
"""
import hashlib, json, os, re
from datetime import datetime, timezone

import core, paths
from watch_core import planou
from watch_core import deadlines
from watch_core import tasks as tc
from watch_core import daily as dvm
from watch_core import focus

# realpath: the skill is often installed as a symlink (~/.claude/skills/work-watch -> the plugin's skill folder), and
# three levels above the link is ~/.claude, not the plugin
PLUGIN_JSON = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(paths.SCRIPTS)))), '.claude-plugin',
                           'plugin.json')


def settings(cfg):
    pc = cfg.get('planou')
    return pc if isinstance(pc, dict) and pc.get('publish', True) is not False else None


def agent():
    return f'work-watch-{paths.NOME}'


def _version():
    try: return json.load(open(PLUGIN_JSON)).get('version')
    except (OSError, ValueError): return None


def liga(cfg):
    """Configures watch_core.planou for this instance. True when this tick talks to Planou (block, key, live)."""
    pc = settings(cfg)
    if not pc: return False
    planou.configure(planou.planou_name(agent(), pc), paths.ROOT, instance=agent(), project=pc.get('project'),
                     confidentiality=pc.get('confidentiality') or 'minimum', publish=True,
                     live=core.LIVE and pc.get('live', True) is not False, plugin_version=_version(),
                     drafts_text=pc.get('drafts_in_planou') is True)
    return planou.active()


# ---------------------------------------------------------------- the person's changes -> state

def aplica(s, cod, status, agora=None):
    """A task completed, closed or reopened in Planou. Returns True when the state changed."""
    agora = (agora or datetime.now(timezone.utc)).isoformat()
    if cod.startswith('a') and cod[1:].isdigit():
        a = next((x for x in s.get('acoes') or [] if x['id'] == int(cod[1:])), None)
        if not a: return False
        if status == 'A fazer':
            if a.get('status') != 'feita': return False
            a['status'] = 'aberta'
            for k in ('feita', 'resposta'): a.pop(k, None)
            _reaberta(s, cod, agora)
            return True
        nota = {'Feito': 'concluida no Planou', 'Sem ação': 'sem ação no Planou', 'Descartada': 'descartada no Planou'}.get(status)
        return bool(nota and dvm.acao_done(s, a['id'], nota))
    p = next((p for p in s.get('pendencias') or [] if p['chave'] == cod), None)
    if not p: return False            # PR, work item...: closes at the source
    if status == 'A fazer':
        if p.get('status') == 'aberta': return False
        p['status'] = 'aberta'
        for k in ('resolvida', 'como', 'nota'): p.pop(k, None)
        _reaberta(s, cod, agora)
        return True
    if p.get('status') != 'aberta': return False
    if status == 'Descartada':
        p['status'] = 'ignorar'; p['nota'] = 'descartada no Planou'
    elif status in ('Feito', 'Sem ação'):
        p['status'] = 'resolvida'; p['resolvida'] = agora
        p['como'] = 'concluida no Planou' if status == 'Feito' else 'sem ação no Planou'
    else:
        return False
    return True


def _reaberta(s, cod, agora):
    s.setdefault('tarefas_notion', {}).setdefault('reaberta', {})[cod] = agora


def altera(s, cod, p, enviado):
    """An edit in Planou (title, due date, state). `enviado` is what the tick last sent for that task. True = changed."""
    st = s.setdefault('tarefas_notion', {})
    mudou = False
    titulo = (p.get('title') or '').strip()
    if titulo and enviado.get('title') is not None and titulo != enviado.get('title'):
        st.setdefault('titulos', {})[cod] = titulo; mudou = True
    if 'due' in p and 'due' in enviado:
        novo = str(p.get('due') or '')[:10]
        if novo != (enviado.get('due') or ''):
            st.setdefault('prazos', {})[cod] = novo; mudou = True     # '' = no due date (manual)
    plano = st.setdefault('plano', {})
    if p.get('state') == 'in_progress' and enviado.get('state') == 'todo' and cod not in plano:
        plano[cod] = datetime.now(tc.BRT).date().isoformat(); mudou = True
    elif p.get('state') == 'todo' and enviado.get('state') == 'in_progress' and cod in plano:
        plano.pop(cod); mudou = True
    return mudou


def responde(s, ev, agora=None):
    """An answer given in "Precisa de você". True when it changed the state."""
    p = ev.get('payload') or {}
    chave = p.get('task_source_key') or ''
    cod = chave.split(':', 1)[1] if chave.startswith(agent() + ':') else ''
    tipo = ev.get('type')
    if tipo == 'draft_sent' and cod.startswith('p'):
        pend = next((x for x in s.get('pendencias') or [] if x['chave'] == cod and x.get('status') == 'aberta'), None)
        if not pend: return False
        pend.update(status='resolvida', resolvida=(agora or datetime.now(timezone.utc)).isoformat(),
                    como='respondido pelo usuario (Enviei no Planou)')
        return True
    if tipo == 'decision_answered' and p.get('value'):
        ref = planou.ask_ref(ev).split('#', 1)[0] or cod
        st = s.setdefault('tarefas_notion', {})
        if p['value'] == 'other':                       # free answer: kept for the session, which acts on it
            st.setdefault('dec_resposta', {})[ref] = (p.get('note') or '')[:2000]
        if ref.startswith('a') and ref[1:].isdigit():
            st.setdefault('dec_escolha', {})[ref] = p['value']; return True
        if ref.startswith('v-'):
            st.setdefault('v_escolha', {})[ref] = p['value']; return True
    return False


def before(s):
    """Heavy tick, before the hooks: events -> state, acknowledged. Returns lines."""
    agora = datetime.now(timezone.utc)
    return planou.apply_events(planou.poll(), lambda c, st: aplica(s, c, st, agora), lambda ev: responde(s, ev, agora),
                               lambda c, p, enviado: altera(s, c, p, enviado))


# ---------------------------------------------------------------- state -> Planou

def _limpo(t):
    return ' '.join(re.sub(r'\*\*|`', '', t or '').split())


def _h(*partes):
    return hashlib.sha256(json.dumps(partes, ensure_ascii=False).encode()).hexdigest()[:8]


def pedidos(s, its, conf):
    """The asks that come from the state: open decisions (actions in the "decisao" bucket, the "Decidir: ..." rows) and
    what the agent needs from the user (tasks.necessidades).

    A decision is always a Planou decision with a free answer ("Responder"), its options (from --opcoes or read from the
    text) as buttons when it has them. A need with options is a decision too; a need without options asks no question and
    is an alert. The "v2" in the refs replaced the alerts that 0.20.0 opened for decisions without options
    (reconcile_asks withdraws them and opens the decisions).

    A broken source and a credential near or past its date open no ask any more (0.28.0): they go in the tools list of
    the heartbeat (ferramentas() below) and Planou opens tool_failing / tool_expiring by itself; an ask here would show the
    same problem twice. reconcile_asks withdraws the alerts that older versions opened. A failure that only the person can
    fix asks a decision of its own instead (pedidos_credenciais, PLN0364)."""
    out = {}
    esc = (s.get('tarefas_notion') or {}).get('dec_escolha') or {}
    for a in s.get('acoes') or []:
        cod = f'a{a["id"]}'
        if a.get('status') != 'aberta' or a.get('balde') != 'decisao' or cod in esc: continue
        it = its.get(cod) or {}
        ops = tc.opcoes(a)[:8]
        uid = it.get('uid') or cod
        titulo = f'Decisão {uid}' if conf == 'minimum' else (it.get('curto') or _limpo(a.get('texto')))
        ctx = _limpo(a.get('texto'))[:4000] if conf == 'detail' else None
        ops = ops if len(ops) >= 2 else []              # a lone option is not a choice: free answer only
        ref = f'{cod}#v2-{_h(a.get("texto"), ops)}'
        out[ref] = {'kind': 'decision', 'title': titulo, 'context': ctx, 'task': cod, 'free_text': True,
                    'options': [(k, f'Opção {k}' if conf == 'minimum' else t, rec) for k, t, rec in ops]}
    quebrado = s.get('quebrado') or {}
    creds = (s.get('credenciais') or {}).get('itens') if isinstance(s.get('credenciais'), dict) else None
    de_credencial = {c.get('necessidade') for c in (creds.values() if isinstance(creds, dict) else ())
                     if isinstance(c, dict) and c.get('necessidade')}
    necs = tc.necessidades(s)
    for cod, txt, _prazo, ops in necs:
        ops = list(ops or [])[:8]
        fonte = cod[2:] if cod.startswith('v-') else ''
        if len(ops) < 2 and (fonte in quebrado or cod in de_credencial):
            continue            # a broken source or a credential: the tools list tells Planou (tool_failing / tool_expiring)
        ref = f'{cod}#v2-{_h(ops)}' if len(ops) >= 2 else f'{cod}#{_h(ops)}'
        titulo = (f'O agente precisa de você ({cod})' if conf == 'minimum'
                  else f'O agente precisa de você: {_limpo(txt)}')[:300]
        ctx = _limpo(txt)[:4000] if conf == 'detail' else None
        if len(ops) >= 2:
            out[ref] = {'kind': 'decision', 'title': titulo, 'context': ctx, 'free_text': True,
                        'options': [(k, f'Opção {k}' if conf == 'minimum' else t, rec) for k, t, rec in ops]}
        else:
            out[ref] = {'kind': 'alert', 'code': 'agent_needs', 'severity': 'medium', 'title': titulo, 'context': ctx}
    return out


# Planou "Ferramentas do agente": one line per source of the instance, by adapter type. The instance config wins for
# each source: "rotulo_planou" (label), "credencial_tipo" (token, session_cookie, oauth, api_key, none, other) and
# "conserto" (how to fix it). The adapters' own `conserto` defaults are English notes for the daily page: not used here.
FERRAMENTA = {   # tipo: (label, credential type, how to fix it)
    'slack': ('Slack', 'session_cookie', 'Entre no Slack pelo navegador e grave o token e o cookie de novo (work-inbox, slack_pull.py --token --cookie).'),
    'teams': ('Teams', 'token', 'Renove o login do Teams (az login ou o token do Teams) e rode o tick de novo.'),
    'teams_presenca': ('Presença do Teams', 'oauth', 'Refaça o consentimento por código de dispositivo da presença do Teams.'),
    'outlook_email': ('Outlook (e-mail)', 'oauth', 'Renove o login da conta Microsoft (az login) e rode o tick de novo.'),
    'outlook_calendar': ('Outlook (agenda)', 'oauth', 'Renove o login da conta Microsoft (az login) e rode o tick de novo.'),
    'gmail': ('Gmail', 'oauth', 'Renove o token OAuth do Google (adapters/google_auth.py).'),
    'google_chat': ('Google Chat', 'oauth', 'Renove o token OAuth do Google (adapters/google_auth.py).'),
    'google_calendar': ('Google Agenda', 'oauth', 'Renove o token OAuth do Google (adapters/google_auth.py).'),
    'github': ('GitHub', 'token', 'Confira o gh auth status ou gere um token novo.'),
    'gitlab': ('GitLab', 'token', 'Gere um token de API novo no GitLab e grave no arquivo do token.'),
    'jira': ('Jira', 'api_key', 'Gere um token de API novo no Jira e grave no secrets da instância.'),
    'ado_workitems': ('Azure DevOps (itens)', 'token', 'Gere um PAT novo no Azure DevOps e grave nas credenciais do git.'),
    'ado_prs': ('Azure DevOps (PRs)', 'token', 'Gere um PAT novo no Azure DevOps e grave nas credenciais do git.'),
    'ado_pipelines': ('Azure DevOps (pipelines)', 'oauth', 'Renove o az login e rode o tick de novo.'),
    'k8s': ('Kubernetes', 'other', 'Confira o kubeconfig e o login dos contextos (SSO) e rode o tick de novo.'),
    'gravacoes': ('Gravações de reunião', 'none', 'Confira a pasta das gravações e o cache da agenda (--agenda set).'),
}
CREDENCIAL = {'ok': 'ok', 'perto': 'expiring', 'vencida': 'failing', '?': 'failing'}


def ferramentas(ctx):
    """The tools list for Planou: every source of the config (failing while it is in s['quebrado'], since its `desde`),
    with the expiry of its credential when it is known (the source's vence(), or a credential of the credenciais hook
    that names the source in "fonte"), plus one line per other credential of that hook. Never a credential value."""
    s = ctx.s
    creds = (s.get('credenciais') or {}).get('itens') if isinstance(s.get('credenciais'), dict) else None
    creds = creds if isinstance(creds, dict) else {}
    por_fonte = {c.get('fonte'): c for c in creds.values() if isinstance(c, dict) and c.get('fonte')}
    fontes = []
    for nome, f in (getattr(ctx, 'fontes', None) or {}).items():
        rot, cred, fix = FERRAMENTA.get(f.tipo, (nome, None, None))
        cfg = f.cfg or {}
        c = por_fonte.get(nome) or {}
        try: vence = f.vence(s)
        except Exception: vence = None
        fontes.append({'name': nome, 'key': planou.tool_key(nome), 'label': cfg.get('rotulo_planou') or rot,
                       'credential': cfg.get('credencial_tipo') or c.get('tipo') or cred or 'other',
                       'expires_at': vence or c.get('vence') or None,
                       'fix': cfg.get('conserto') or fix or 'Veja o erro do tick e rode o tick de novo.'})
    nomes = {x['name'] for x in fontes}
    quebrado = {k: v for k, v in (s.get('quebrado') or {}).items() if k in nomes}
    out = planou.source_tools(fontes, quebrado)
    for nome, c in creds.items():
        if not isinstance(c, dict) or c.get('fonte') in nomes: continue      # already on its source's line
        est = CREDENCIAL.get(c.get('estado'), 'failing')
        out.append(planou.tool(f'cred:{(planou.tool_key(nome) or "x").lower()}', est, nome, 'other', c.get('tipo') or 'other',
                               c.get('vence') or None, c.get('detalhe') if est == 'failing' else None, None,
                               f'Renovar: {c["renovar"]}' if c.get('renovar') else None))
    return out


# PLN0364: a failure only the person can fix (a token refused or expired, an SSO session whose refresh failed) opens one
# "Precisa de você" ask per episode, with how to fix it and what stops working. Network, timeout, 5xx and drive errors
# never do: the source comes back by itself and the Ferramentas tab already shows it.
PESSOAL = re.compile(r'\b401\b|recusad|unauthori[sz]ed|bad credentials|invalid_grant|expired|expirad|vencid|venceu'
                     r'|refresh failed|reauth|n[aã]o encontrado em|not found in', re.I)
# a TLS failure ("certificate has expired") is the server's or the network's, never the person's credential
NAO_PESSOAL = re.compile(r'certificate|\bssl\b|\btls\b', re.I)
JA_RENOVEI = [('S', 'Já renovei', False), ('N', 'Ainda não', False)]


def _pessoal(erro):
    erro = str(erro or '')
    return bool(PESSOAL.search(erro)) and not NAO_PESSOAL.search(erro)


def _rotulo(nome, f):
    rot, _cred, fix = FERRAMENTA.get(f.tipo, (nome, None, None))
    cfg = f.cfg or {}
    return cfg.get('rotulo_planou') or rot, cfg.get('conserto') or fix or 'Veja o erro do tick e rode o tick de novo.'


def pedidos_credenciais(ctx, conf):
    """The asks for failures that need the person (PLN0364): {ref: decision} for reconcile_asks, which opens each ref
    once, keeps it while the failure lasts (an answered one is not asked again) and withdraws it when the tool is back.
    The ref carries the failure's start ('desde'), so a new episode on another day is a new ask.
      - a source in s['quebrado'] whose error looks personal (PESSOAL), unless a credential of the credenciais hook
        names that source and already asks;
      - a credential of the credenciais hook in state 'vencida' (a date in the past always; a command only when its
        error looks personal).
    The title and the fix are what the Ferramentas tab already shows (tool label and fix, sent with any
    confidentiality); the error itself goes only with "detail". Never a credential value."""
    s = ctx.s
    fontes = getattr(ctx, 'fontes', None) or {}
    creds = (s.get('credenciais') or {}).get('itens') if isinstance(s.get('credenciais'), dict) else None
    creds = creds if isinstance(creds, dict) else {}
    out, cobertas = {}, set()

    def pede(chave, rotulo, desde, fix, impacto, erro):
        ref = f'cred:{(planou.tool_key(chave) or "x").lower()}#{_h(desde)}'
        titulo = (f'Renove a credencial de {rotulo}' if conf != 'minimum' else f'Renove uma credencial do agente ({rotulo})')
        fix, impacto = planou.clean_text(fix, 1500) or 'veja a aba Ferramentas', planou.clean_text(impacto, 500) or '-'
        ctx_txt = (f'Como consertar: {fix}\nO que para de funcionar: {impacto}\n'
                   'Depois de renovar, responda "Já renovei": o próximo tick confere e este pedido sai sozinho quando a '
                   'ferramenta voltar.')
        if conf == 'detail' and erro: ctx_txt += f'\nErro: {planou.clean_text(erro) or "omitido"}'
        out[ref] = {'kind': 'decision', 'title': titulo[:300], 'context': ctx_txt[:4000], 'free_text': True,
                    'options': JA_RENOVEI}

    for nome, c in creds.items():
        if not isinstance(c, dict) or c.get('estado') != 'vencida': continue
        if c.get('comando') and not _pessoal(c.get('detalhe')): continue
        f = fontes.get(c.get('fonte'))
        rot = _rotulo(c['fonte'], f)[0] if f else nome
        fix = f'rode {c["renovar"]}' if c.get('renovar') else (_rotulo(c['fonte'], f)[1] if f else 'Renove a credencial.')
        impacto = c.get('impacto') or (f'o agente para de ler {rot} até a renovação' if f else
                                       f'o que o agente faz com {nome} para até a renovação')
        pede(nome, rot if not f else f'{rot} ({nome})', c.get('desde') or c.get('vence') or nome, fix,
             impacto, c.get('detalhe'))
        if c.get('fonte'): cobertas.add(c['fonte'])
    for nome, q in (s.get('quebrado') or {}).items():
        f = fontes.get(nome)
        if not f or nome in cobertas: continue
        q = q if isinstance(q, dict) else {'erro': str(q)}
        if ((f.cfg or {}).get('credencial_tipo') or FERRAMENTA.get(f.tipo, (None, None))[1]) == 'none': continue
        if not _pessoal(q.get('erro')): continue
        rot, fix = _rotulo(nome, f)
        pede(nome, rot, q.get('desde') or nome, fix,
             f'o agente para de ler {rot}: o que chegar lá não vira tarefa nem aviso até a renovação', q.get('erro'))
    return out


def decisoes(ctx, conf):
    """What the agent decided and did by itself during the tick, as the low alert of `pergunta --auto` (PLN0225): today
    the GitLab token rotation (PLN0364), from the note s['gitlab']['rotacao'] the source leaves (Planou is not
    configured while the sources tick). The note leaves the state once Planou took it. Returns AVISO lines."""
    st = ctx.s.get('gitlab') if isinstance(ctx.s.get('gitlab'), dict) else {}
    r = st.get('rotacao')
    if not isinstance(r, dict) or r.get('avisado'): return []
    f = (getattr(ctx, 'fontes', None) or {}).get(r.get('fonte'))
    rot = _rotulo(r.get('fonte'), f)[0] if f else 'GitLab'
    try: dia = datetime.fromisoformat(str(r.get('vence'))[:10]).strftime('%d/%m')
    except ValueError: dia = str(r.get('vence') or '?')
    if r.get('gravado') is False:
        titulo, sev = f'Renovei o token {rot}, mas não consegui gravar o novo no arquivo do token', 'high'
        ctx_txt = ('O token antigo já foi revogado pelo GitLab. ' +
                   (f'O novo ficou em {r["arquivo"]} (0600): mova para o arquivo do token.' if r.get('arquivo')
                    else 'Gere um token novo e grave no arquivo do token.'))
        code = 'agent'
    else:
        titulo, sev, code = f'Decidi: renovei o token {rot}, vence em {dia}', 'low', 'agent_decided'
        ctx_txt = ('O token de API ia vencer em poucos dias e ainda funcionava: o agente rodou a rotação pela API do GitLab '
                   'e gravou o novo no mesmo arquivo (0600). O antigo foi revogado. Nada a fazer.')
    ref = 'q-auto-token-' + _h(r.get('fonte'), r.get('vence'), r.get('quando'))
    try:
        planou.raise_alert(titulo[:300], code, sev, ctx_txt, None, ref)
    except planou.PlanouError as e:
        return [f'AVISO (planou): decisao {ref} fica para o proximo tick: {e.message}']
    r['avisado'] = True
    st.pop('rotacao', None)
    return []


NOME = {'slack': 'Slack', 'teams': 'Teams', 'google_chat': 'Google Chat', 'chat': 'Google Chat', 'email': 'E-mail',
        'gmail': 'E-mail', 'jira': 'Jira', 'calendar': 'Convite', 'pipelines': 'Pipeline', 'reuniao': 'Ata'}


MENSAGEM = ('slack', 'teams', 'google_chat', 'chat', 'email', 'gmail')    # sources whose pending item links to one message


def _reuniao(s, ref):
    """'2026-09-25 10-00-12#5' -> (meeting title, start, action number) from the recordings the agent keeps."""
    quando, _, n = str(ref or '').partition('#')
    for ev in ((s.get('gravacoes') or {}).get('eventos') or {}).values():
        if quando and os.path.basename(str(ev.get('video') or '')).startswith(quando):
            return ev.get('titulo') or '', ev.get('ini') or '', n
    return '', '', n


def _ata(ini, titulo):
    """The minutes file in the workspace (meetings/YYYY-MM-DD-HHMM-<slug>/minutes.md), or ''."""
    try:
        d = datetime.fromisoformat(str(ini).replace('Z', '+00:00')).astimezone(tc.BRT)
        base = os.path.join(paths.WORKSPACE or '', 'meetings')
        for nome in sorted(os.listdir(base)):
            if nome.startswith(d.strftime('%Y-%m-%d-%H%M')):
                f = os.path.join(base, nome, 'minutes.md')
                return f if os.path.exists(f) else os.path.join(base, nome)
    except (OSError, ValueError, TypeError):
        pass
    return ''


def enriquece(s, its, fontes=()):
    """Where each task came from, in the words of the state: origin (type, label, link, who, when) and a description
    with the source, the text that started it and what the agent understood must be done. Never empty: when the state
    does not know the origin, the description says so. watch_core.planou applies the confidentiality before sending.
    An open pending item also gets the reason to be ready in Planou when its source says it mirrors something already
    happening there (`pronta(p, s)` of the source that enqueues that `fonte`); without it, it is born in backlog.
    The Prazo (`deadline`) comes from the item or, failing that, from the source the row links to (`prazo()`)."""
    pend = {p['chave']: p for p in s.get('pendencias') or []}
    acoes = {f'a{a["id"]}': a for a in s.get('acoes') or []}
    manual = (s.get('tarefas_notion') or {}).get('links') or {}
    for cod, it in its.items():
        p, a = pend.get(cod), acoes.get(cod)
        if p:
            fonte = p.get('fonte') or ''
            quando = p.get('desde') or p.get('criada')
            quem, autor = (p.get('quem') or '').strip(), (p.get('autor') or '').strip()
            assunto = ' '.join((p.get('assunto') or '').split())
            org = planou.default_origin(cod, {**it, 'entrou': quando, 'autor': autor or quem})
            nome = NOME.get(fonte, org['label'].split(' · ')[0])
            if p.get('status') == 'aberta':
                for f in fontes or ():
                    if fonte in (getattr(f, 'pend', ()) or ()):
                        try: r = f.pronta(p, s)
                        except Exception: r = None
                        if r:
                            it['ready_reason'], it['ready_reason_min'] = r
                            break
            if fonte == 'reuniao':
                titulo, ini, n = _reuniao(s, p.get('ref'))
                org.update(type='meeting', author=None, at=ini or quando,
                           label=' · '.join(x for x in ('Ata', titulo or 'reunião sem título no estado',
                                                         planou.when_label(ini or quando), f'ação {n}' if n else '') if x))
                arq = _ata(ini, titulo) if ini else ''
            elif fonte in ('email', 'gmail'):
                org['label'] = ' · '.join(x for x in (nome, quem, f'"{assunto[:80]}"' if assunto else '', planou.when_label(quando)) if x)
                arq = ''
            elif fonte in ('slack', 'teams', 'google_chat', 'chat'):
                # "Slack · #canal · Fulano, 28/09 09:40": the conversation, then who wrote the linked message and when
                em = (p.get('msg') or {}).get('em') or quando
                org['at'] = em
                quem_msg = ', '.join(x for x in (autor or quem, planou.when_label(em)) if x)
                org['label'] = ' · '.join(x for x in (nome, quem if autor and autor != quem else '', quem_msg) if x)
                arq = ''
            else:
                org['label'] = ' · '.join(x for x in (nome, quem, planou.when_label(quando)) if x)
                arq = ''
            # the message itself (permalink kept by the source); a link the person set by hand (--link) still wins
            if fonte in MENSAGEM and p.get('link') and cod not in manual: org['url'] = p['link']
            it['planou_origin'] = org
            it['trecho'] = assunto
            linhas = [f'De onde veio: {org["label"]}.']
            linhas.append(f'Trecho: "{assunto[:600]}"' if assunto else 'Trecho: o estado do agente não guardou o texto.')
            if arq: linhas.append(f'Ata: {arq}')
            linhas.append(f'O que fazer: {it.get("curto") or assunto or cod}')
            if p.get('nota'): linhas.append(f'Nota do agente: {p["nota"]}')
            if p.get('como'): linhas.append(f'Resolvida: {p["como"]}.')
            it['planou_description'] = '\n'.join(linhas)
        elif a:
            quando = a.get('desde')
            it['planou_origin'] = {'type': 'daily', 'author': None, 'at': quando, 'url': None,
                                   'label': ' · '.join(x for x in ('Página do dia', f'ação {it.get("uid") or cod}',
                                                                  f'anotada em {planou.when_label(quando)}' if quando else '') if x)}
            linhas = [f'De onde veio: ação anotada pelo agente na página do dia'
                      + (f' em {planou.when_label(quando)}' if quando else '')
                      + '; a conversa, ata ou e-mail que a originou não ficou registrado no estado.',
                      f'O que fazer: {" ".join((a.get("texto") or "").split())[:1500]}']
            if a.get('balde') == 'aguardando' and a.get('quem'): linhas.append(f'Aguardando: {a["quem"]}.')
            ops = tc.opcoes(a)
            if ops: linhas.append('Opções: ' + '; '.join(f'{k}{" (recomendada)" if r else ""}: {t}' for k, t, r in ops))
            if a.get('resposta'): linhas.append(f'Resposta/evidência: {a["resposta"]}')
            it['planou_description'] = '\n'.join(linhas)
        # PRs, work items and other rows from the sources: the generic origin (source, who, link) and description
        # the Prazo (deadline): the one the item already has (minutes, a request in a message, a Jira row), else the
        # due date of the work item / issue it links to (each source's prazo()); none stated = none sent
        if not deadlines.valid(it.get('deadline')):
            for f in fontes or ():
                try: d = deadlines.valid(f.prazo(it, p, s))
                except Exception: d = None
                if d:
                    it['deadline'] = d
                    break
    return its


def _motor(ctx, ganchos):
    """watch_core.tasks configured for this instance: the plugin's tarefas hook, or the instance's own module that did it
    during the hooks; with neither, only the code (no extra rows)."""
    for g in ganchos or []:
        if getattr(g, 'tipo', None) == 'tarefas':
            try: g._configura_core(ctx)
            except Exception: pass
            break
    if tc.SIGLA is None: tc.configura(ctx.cfg.get('sigla'))


ISSUE_KEY = r'\b([A-Z][A-Z0-9]+-\d+)\b'


def regras_links(cfg):
    """The instance's link rules for Planou: the tarefas hook's "links" (issue_url with {key}, repo_regex + pr_url with
    {repo} and {n}) as {pattern, url, label} with {1}..{3}, then the "planou.links" rules as written."""
    out = []
    for g in cfg.get('ganchos') or []:
        if not isinstance(g, dict) or g.get('tipo') != 'tarefas' or not isinstance(g.get('links'), dict): continue
        lk = g['links']
        if lk.get('issue_url'):
            out.append({'pattern': ISSUE_KEY, 'url': str(lk['issue_url']).replace('{key}', '{1}'), 'label': 'Issue'})
        if lk.get('repo_regex') and lk.get('pr_url'):
            pattern = rf'\b({lk["repo_regex"]}) ?#(\d+)'
            try: n = re.compile(pattern).groups                  # groups inside repo_regex push the number further
            except re.error: n = 0
            if 2 <= n <= planou.MAX_LINK_GROUPS:
                out.append({'pattern': pattern, 'url': str(lk['pr_url']).replace('{repo}', '{1}').replace('{n}', '{%d}' % n),
                            'label': 'Pull request'})
    extra = (settings(cfg) or {}).get('links')
    out += [r for r in extra if isinstance(r, dict)] if isinstance(extra, list) else []
    return out


def prioridades(its, agora):
    """{pid: Planou priority} of the synced items (state.json keeps the pid, not the priority), for the Foco do dia."""
    hoje = agora.astimezone().date().isoformat()
    pids = {c: (t or {}).get('pid') for c, t in (planou._load('state.json', {}).get('tasks') or {}).items()}
    return {pids[c]: planou._priority(it.get('prio'), planou._date(it.get('prazo')), hoje) for c, it in its.items() if pids.get(c)}


def after(ctx, ganchos=()):
    """Heavy tick, after the hooks: full list, asks, sign of life. Returns lines."""
    s = ctx.s
    _motor(ctx, ganchos)
    agora = datetime.now(timezone.utc)
    its = enriquece(s, tc.itens(s, agora), list((getattr(ctx, 'fontes', None) or {}).values()))
    linhas = planou.sync(its, agora)
    conf = (settings(ctx.cfg) or {}).get('confidentiality') or 'minimum'
    falhou = any(l.startswith('FONTE QUEBRADA (planou)') or (l.startswith('AVISO (planou)') and ' recusada: ' not in l)
                 for l in linhas)
    if not falhou:                    # Planou down: the asks wait for the next tick (one failure line is enough)
        linhas += planou.reconcile_asks({**pedidos(s, its, conf), **pedidos_credenciais(ctx, conf)})
        linhas += decisoes(ctx, conf)
        if (settings(ctx.cfg) or {}).get('focus') is not False:     # Foco do dia: only the keeper writes (PLN0379)
            linhas += focus.keep(prioridades(its, agora), now=agora)
    try:
        planou.set_tools(ferramentas(ctx), agora)
    except Exception as e:                        # never costs the sign of life
        linhas.append(f'AVISO (planou): ferramentas: {type(e).__name__}: {str(e)[:120]}')
    try:
        planou.set_links(regras_links(ctx.cfg), agora)
    except Exception as e:
        linhas.append(f'AVISO (planou): regras de link: {type(e).__name__}: {str(e)[:120]}')
    linhas += planou.heartbeat('tick', broken_sources=[f for f, _ in ctx.quebrados], now=agora)
    return linhas


def saida(linhas):
    """(block or None, loose lines): what the person did goes under `== PLANOU`; FONTE QUEBRADA (planou) goes loose, for
    the runner's one-wake-per-hour rule; AVISO (planou) never reaches the output (cache/planou/avisos.log)."""
    avisos = [l for l in linhas if l.startswith('AVISO (planou)')]
    soltas = [l for l in linhas if l.startswith('FONTE QUEBRADA (planou)')]
    novas = [l for l in linhas if l not in avisos and l not in soltas]
    if avisos:
        try:
            d = os.path.join(paths.ROOT, 'cache', 'planou'); os.makedirs(d, exist_ok=True)
            f = os.path.join(d, 'avisos.log')
            velho = open(f).read().splitlines()[-499:] if os.path.exists(f) else []
            agora = datetime.now().isoformat(timespec='seconds')
            open(f, 'w').write('\n'.join(velho + [f'{agora}\t{l}' for l in avisos]) + '\n')
        except OSError:
            pass
    return ('\n'.join(['== PLANOU'] + novas) if novas else None), soltas
