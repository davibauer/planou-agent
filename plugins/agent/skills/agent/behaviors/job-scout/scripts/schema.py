#!/usr/bin/env python3
"""Data schema of job-scout: key names of state.json, config.json and reputation.json, job status values, and the
migration from the Portuguese schema (up to 0.8.x) to the English one (0.9.0+).

Renames go by explicit PATH, never by key name alone: a key name can also be user data (a points label called "remoto",
a CV profile called "base", a company slug), and those must survive untouched. Dicts keyed by user data (job ids, company
slugs, CV profile names, experience titles, pay category names, points labels) are walked through, never renamed.

Kept in Portuguese on purpose (values, not keys): source names ('linkedin', 'recomendadas', 'mercado', ...), pending and
contact status ('aberta', 'resolvida', 'ignorar', 'nao', 'ok'), matrix levels ('sim', 'parcial', 'nao'), fit words in notes ('forte',
'médio', ...), closure kinds ('encerrada', 'removida'), Notion request words, pay category names. The 'licoes' subtree of
state.json belongs to a lessons library shared with other tools and is not touched.

Job status: stored in English, shown in Portuguese (`label()`); Portuguese input is accepted forever (`status_in()`).
"""
import json

SCHEMA = 2                      # 1 = Portuguese keys (up to 0.8.x), 2 = English keys

STATUS = {'interessa': 'interested', 'aplicado': 'applied', 'respondeu': 'replied', 'entrevista': 'interview',
          'proposta': 'offer', 'fechado': 'hired', 'perdido': 'lost', 'ignorar': 'ignored'}
LABEL = {en: pt for pt, en in STATUS.items()}          # what the user sees (terminal, pipeline.md, Notion)

def status_in(v):
    """Status typed by the user or the model, in either language -> stored value."""
    return STATUS.get(v, v)

def label(v):
    """Stored status -> Portuguese label shown to the user."""
    return LABEL.get(v, v)

# ---------------- key maps (old -> new), one per node kind ----------------
TOP = {'vagas': 'jobs', 'contatos': 'contacts', 'empresas': 'companies', 'mensagens': 'messages', 'mercado': 'boards',
       'notion_vagas': 'notion_jobs', 'origem': 'origin', 'pend_seq': 'pending_seq', 'pendencias': 'pending',
       'recomendadas': 'recommended', 'reengajar': 'reengage', 'ultima': 'last_run', 'chaves': 'dedup_keys',
       'rot_aplicadas': 'routine_applied', 'rot_empresas': 'routine_companies', 'rot_encerradas': 'routine_closed',
       'rot_entrevista': 'routine_interview', 'rot_funil': 'routine_pipeline', 'rot_reengajar': 'routine_reengage',
       'rot_semana': 'routine_week', 'rot_shortlist': 'routine_shortlist'}
SEEN = {'vistas': 'seen'}                                          # cursor + seen lists of each source
JOBS = {'anotadas': 'tracked', 'vistas': 'seen', 'ticks_sem_card': 'ticks_without_cards'}
MESSAGES = {'vistas': 'seen'}
ME = {'nome': 'name'}
NOTION = {'colunas': 'columns'}
DEDUP = {'desde': 'since'}
CONTACT = {'nota': 'note', 'quando': 'when'}
PENDING = {'assunto': 'subject', 'chave': 'key', 'criada': 'created', 'desde': 'since', 'fonte': 'source',
           'lembretes': 'reminders', 'nota': 'note', 'quem': 'who', 'ultimo_lembrete': 'last_reminder',
           'resolvida': 'resolved_at', 'como': 'how'}
JOB = {'aplicada_conferida': 'applied_checked', 'atividades_vistas': 'activities_seen', 'candidatura': 'application',
       'cv_perfil': 'cv_profile', 'cv_enviado': 'cv_sent', 'dias': 'days', 'empresa': 'company', 'faixa': 'pay_range',
       'faixa_cat': 'pay_category', 'fonte': 'source', 'formato': 'work_format', 'historico': 'history', 'local': 'location',
       'match_rapido': 'quick_match', 'matriz': 'matrix', 'nivel': 'level', 'nota': 'note', 'notion_marca': 'notion_mark',
       'notion_matriz_blocos': 'notion_matrix_blocks', 'notion_matriz_marca': 'notion_matrix_mark', 'postado': 'posted',
       'quando': 'when', 'recrutador': 'recruiter', 'recrutador_consultado': 'recruiter_checked',
       'rep_cobrada': 'reputation_asked', 'titulo': 'title', 'encerrada': 'closure', 'remoto': 'remote',
       'republicada': 'reposted', 'n_candidatos': 'n_applicants', 'candidatos': 'applicants', 'pontos': 'points',
       'resumo': 'snippet', 'nivel_fora': 'level_excluded', 'erro': 'error', 'detalhe': 'detailed', 'tipo': 'kind',
       'inscricao_ate': 'apply_until', 'candidatura_ate': 'apply_until', 'entrevista_em': 'interview_at'}
HISTORY = {'quando': 'when'}
MATRIX = {'estudar': 'study', 'evidencia': 'evidence', 'nivel': 'level'}
RECRUITER = {'nome': 'name', 'perfil': 'profile'}
CLOSURE = {'tipo': 'kind', 'quando': 'when'}

CONFIG = {'_nota': '_note', 'buscas': 'searches', 'cv_perfis': 'cv_profiles', 'empresas': 'companies',
          'entrevistas_dir': 'interviews_dir', 'excluir': 'exclude', 'faixas': 'pay_ranges', 'fontes_logadas': 'logged_in_sources',
          'habilidades': 'skills', 'incluir': 'include', 'mercado': 'boards', 'min_pontos': 'min_score', 'pontos': 'points',
          'fuso_horas': 'tz_hours', 'funil_md': 'pipeline_md'}
SEARCH = {'remoto': 'remote'}
BOARDS = {'fontes': 'sources'}
CV_PROFILE = {'arquivo': 'file_name', 'datas': 'dates', 'descricao': 'description', 'experiencias': 'experiences',
              'fontes': 'sources', 'novas': 'new_entries', 'paralelas': 'parallel', 'recortes': 'cuts'}
NEW_ENTRY = {'data': 'date', 'local': 'location', 'modalidade': 'work_mode', 'modelo': 'template',
             'modelo_fonte': 'template_source', 'secoes': 'sections', 'tecnologias': 'technologies', 'titulo': 'title'}
SECTION = {'itens': 'items', 'nome': 'name'}
CUT = {'cortar_a_partir_de': 'cut_from', 'titulo': 'title', 'modalidade': 'work_mode'}
PAY = {'_nota': '_note', 'cambio_usd_brl': 'usd_brl_rate', 'categorias': 'categories', 'horas_mes': 'hours_month',
       'referencia_usuario': 'user_reference', 'observadas': 'observed'}
PAY_CATEGORY = {'brl_mes': 'brl_month', 'usd_hora': 'usd_hour', 'usd_ano': 'usd_year', 'usd_mes': 'usd_month'}
USER_REF = {'pj_hora_brl': 'pj_hour_brl', 'pj_mes_brl': 'pj_month_brl'}
OBSERVED = {'empresa': 'company', 'cargo': 'role', 'valor': 'value', 'fonte': 'source', 'data': 'date'}

REPUTATION = {'data': 'date', 'tipo': 'kind', 'alertas': 'alerts', 'fontes': 'sources'}
MATRIX_INPUT = {'vaga': 'job', 'itens': 'items'}                   # JSON the model writes for matrix.py

def _ren(d, m):
    """Renames the keys of ONE dict (not recursive). An English key already present wins over the old one."""
    if not isinstance(d, dict): return d
    for old, new in m.items():
        if old in d:
            v = d.pop(old)
            d.setdefault(new, v)
    return d

def _each(d):
    return d.values() if isinstance(d, dict) else (d if isinstance(d, list) else ())

# ---------------- state.json ----------------
def job(v):
    """One tracked job (also used for `--job ID k=v` input)."""
    _ren(v, JOB)
    if 'status' in v: v['status'] = status_in(v['status'])
    for h in v.get('history') or []:
        _ren(h, HISTORY)
        if 'status' in h: h['status'] = status_in(h['status'])
    for i in v.get('matrix') or []: _ren(i, MATRIX)
    _ren(v.get('recruiter'), RECRUITER)
    _ren(v.get('closure'), CLOSURE)
    return v

def state(s):
    if not isinstance(s, dict) or s.get('schema') == SCHEMA: return s
    _ren(s, TOP)
    j = _ren(s.get('jobs'), JOBS) or {}
    for v in _each(j.get('tracked')): job(v)
    for k in ('messages', 'recommended', 'boards', 'gmail', 'companies'): _ren(s.get(k), SEEN)
    _ren((s.get('messages') or {}).get('me'), ME)
    _ren(s.get('notion_jobs'), NOTION)
    for d in _each(s.get('dedup_keys')): _ren(d, DEDUP)
    for c in _each(s.get('contacts')): _ren(c, CONTACT)
    for p in s.get('pending') or []: _ren(p, PENDING)
    for v in _each(j.get('tracked')): _remark(v)
    s['schema'] = SCHEMA
    return s

# ---------------- config.json ----------------
def config(c, force=False):
    """Also used on a partial config (the change file of `criteria.py --apply`): renames only what is there."""
    if not isinstance(c, dict) or (c.get('schema') == SCHEMA and not force): return c
    _ren(c, CONFIG)
    for b in c.get('searches') or []: _ren(b, SEARCH)
    _ren(c.get('boards'), BOARDS)
    for p in _each(c.get('cv_profiles')):
        _ren(p, CV_PROFILE)
        for n in _each(p.get('new_entries')):
            _ren(n, NEW_ENTRY)
            for sec in n.get('sections') or []: _ren(sec, SECTION)
        for cut in _each(p.get('cuts')): _ren(cut, CUT)
    pr = _ren(c.get('pay_ranges'), PAY)
    if isinstance(pr, dict):
        for cat in _each(pr.get('categories')): _ren(cat, PAY_CATEGORY)
        _ren(pr.get('user_reference'), USER_REF)
        for o in pr.get('observed') or []: _ren(o, OBSERVED)
    if not force: c['schema'] = SCHEMA
    return c

# ---------------- reputation.json and matrix input ----------------
def reputation(r):
    """{company: {date, kind, glassdoor, alerts, sources}}. Normalized on every run: the model writes this file by hand."""
    for e in _each(r): _ren(e, REPUTATION)
    return r

def matrix_input(d):
    _ren(d, MATRIX_INPUT)
    for i in d.get('items') or []: _ren(i, MATRIX)
    return d

# ---------------- Notion change marks ----------------
# Values only, in a fixed order, with the status as its label: renaming keys or status values does not re-push 500 rows.
ROW_FIELDS = ('status', 'note', 'title', 'company', 'work_format', 'top', 'cv_gdoc', 'closure', 'pay_range', 'match',
              'quick_match', 'source', 'recruiter')
PAGE_FIELDS = ('matrix', 'recruiter', 'cv_gdoc', 'cv_pdf', 'cv_sent', 'prep', 'quick_match')

def row_mark(v):
    return json.dumps([label(v.get('status')) if k == 'status' else v.get(k) for k in ROW_FIELDS], sort_keys=True, ensure_ascii=False)

def page_mark(v):
    return json.dumps([v.get(k) for k in PAGE_FIELDS], sort_keys=True, ensure_ascii=False)

_OLD_ROW = ('status', 'nota', 'titulo', 'empresa', 'formato', 'top', 'cv_gdoc', 'encerrada', 'faixa', 'match',
            'match_rapido', 'fonte', 'recrutador')
_OLD_PAGE = ('matriz', 'recrutador', 'cv_gdoc', 'cv_pdf', 'cv_enviado', 'prep', 'match_rapido')

def _remark(v):
    """Rows that were in sync with Notion before the migration stay in sync after it (no mass re-push)."""
    old = _snapshot_old(v)
    if v.get('notion_mark') is not None and v['notion_mark'] == json.dumps({k: old.get(k) for k in _OLD_ROW}, sort_keys=True, ensure_ascii=False):
        v['notion_mark'] = row_mark(v)
    if v.get('notion_matrix_mark') is not None and v['notion_matrix_mark'] == json.dumps({k: old.get(k) for k in _OLD_PAGE}, sort_keys=True, ensure_ascii=False):
        v['notion_matrix_mark'] = page_mark(v)

def _snapshot_old(v):
    """The already-migrated job seen with the old key names and values (to recompute the old marks)."""
    inv = {new: old for old, new in JOB.items()}
    o = {inv.get(k, k): x for k, x in v.items()}
    if 'status' in o: o['status'] = LABEL.get(o['status'], o['status'])
    if isinstance(o.get('matriz'), list):
        o['matriz'] = [{ {n: p for p, n in MATRIX.items()}.get(k, k): x for k, x in i.items()} for i in o['matriz']]
    if isinstance(o.get('recrutador'), dict):
        o['recrutador'] = {{n: p for p, n in RECRUITER.items()}.get(k, k): x for k, x in o['recrutador'].items()}
    if isinstance(o.get('encerrada'), dict):
        o['encerrada'] = {{n: p for p, n in CLOSURE.items()}.get(k, k): x for k, x in o['encerrada'].items()}
    return o
