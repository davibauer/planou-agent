#!/usr/bin/env python3
"""The job judge's side of the job-scout (PLN0249): judging each job runs in a disposable subagent, not in the session.

The week of 24-30/09/2026 had the job-scout session at 23% of the team's cost: every job was judged in the main
session (the posting, WebSearch for reputation and hiring process, the Gmail alerts, the fit matrix), so all of it stayed
in a context that grew to ~500k tokens. Now the session hands the tick's job blocks to one subagent per tick (the rules
are in ../JUDGE.md) and gets back one line per job; the detail stays in data/judgments/<id>.md.

  judge.py --brief            the judge's rules (JUDGE.md) and this instance's paths: the first thing the subagent runs
  judge.py --path <id|url>    the detail file of a job (creates the folder)
  judge.py --record FILE|-    records a batch of verdicts (JSON list) with ONE `scout.py --job` call and prints the lines
                              the subagent returns to the session:
                                -- <id> · aplicar|descartar · <fit>: <reason> · <detail file or ->
  judge.py --record FILE --dry   checks and prints, records nothing

A verdict: {"id": "<id or job link>", "decision": "aplicar|descartar", "fit": "forte|médio-forte|médio|médio-fraco|
fraco|fora", "reason": "<one line>", "pay_range": "...", "title": "...", "company": "..."} or, for a job already in the
pipeline (reputation, hiring process), {"id": "<id>", "note_add": "<sentence>"}. aplicar -> interested, descartar ->
ignored; forte and médio-forte must be aplicar, fraco and fora must be descartar, unless a profile rule takes the job
out: then "rule": "<the rule>" lets descartar go with ANY fit (the real fit stays in the note, followed by
"(regra do perfil: <rule>)", so the accuracy KPI does not count it as a judge miss). The note starts with the fit, because
scout.fit(), the Notion fit column, the reputation nag and the shortlist read it from there.
"""
import os, sys, re, json, argparse, subprocess, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths                                   # noqa: E402

RULES = os.path.join(os.path.dirname(HERE), 'JUDGE.md')
SCOUT = os.path.join(HERE, 'scout.py')
FITS = ('forte', 'médio-forte', 'médio', 'médio-fraco', 'fraco', 'fora')
MUST = {'forte': 'aplicar', 'médio-forte': 'aplicar', 'fraco': 'descartar', 'fora': 'descartar'}
STATUS = {'aplicar': 'interested', 'descartar': 'ignored'}
MAX_REASON = 200
DETAIL_FITS = {'forte', 'médio-forte', 'médio'}


def _fold(t):
    return unicodedata.normalize('NFKD', str(t)).encode('ascii', 'ignore').decode().lower().strip()


def norm_fit(t):
    """'Medio-Forte' / 'médio forte' -> 'médio-forte'; None when it is not a fit."""
    f = re.sub(r'[\s_]+', '-', _fold(t))
    for x in FITS:
        if _fold(x) == f: return x
    return None


def job_id(ref):
    """Job id from an id or from the job's link, the same ids the sources use (linkedin_jobs, job_boards)."""
    r = str(ref or '').strip()
    m = re.search(r'linkedin\.com/.*?jobs/view/(?:[^/?#]*?-)?(\d+)', r, re.I) or re.search(r'currentJobId=(\d+)', r)
    if m: return m.group(1)
    if re.match(r'https?://', r, re.I):
        path = re.sub(r'[?#].*$', '', r).rstrip('/')
        last = path.split('/')[-1]
        if re.search(r'remoteok\.com', r, re.I):
            m = re.search(r'(\d+)$', last)
            return f'rok-{m.group(1)}' if m else r
        if re.search(r'weworkremotely\.com', r, re.I): return 'wwr-' + re.sub(r'\W+', '-', last)[:60]
        if re.search(r'web3\.career', r, re.I):
            m = re.search(r'(\d+)$', last)
            return f'w3-{m.group(1)}' if m else r
    return r


def safe(jid):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', str(jid)).strip('_')[:80] or 'vaga'


def detail_path(jid, create=False):
    if create: os.makedirs(paths.JUDGMENTS_DIR, exist_ok=True)
    return os.path.join(paths.JUDGMENTS_DIR, safe(job_id(jid)) + '.md')


def _one_line(t):
    return re.sub(r'\s+', ' ', str(t or '')).strip()


def check(item):
    """One verdict -> (normalized dict, None) or (None, problem)."""
    if not isinstance(item, dict): return None, 'item nao e um objeto'
    jid = job_id(item.get('id') or item.get('url'))
    if not jid: return None, 'sem id'
    if 'note_add' in item and 'decision' not in item:
        add = _one_line(item['note_add'])
        return ({'id': jid, 'note_add': add[:MAX_REASON]}, None) if add else (None, 'note_add vazio')
    dec = _fold(item.get('decision'))
    if dec not in STATUS: return None, f'decision "{item.get("decision")}": aplicar ou descartar'
    fit = norm_fit(item.get('fit') or '')
    if not fit: return None, f'fit "{item.get("fit")}": {" | ".join(FITS)}'
    rule = _one_line(item.get('rule'))
    if rule and dec != 'descartar': return None, 'rule so vale com decision descartar'
    if MUST.get(fit, dec) != dec and not (rule and dec == 'descartar' and fit in ('forte', 'médio-forte')):
        return None, f'{fit} pede decision {MUST[fit]}' + (' (ou descartar com "rule": a regra do perfil que tira a vaga)' if MUST[fit] == 'aplicar' else '')
    reason = _one_line(item.get('reason'))
    if not reason: return None, 'reason vazio (o motivo numa linha)'
    out = {'id': jid, 'decision': dec, 'fit': fit, 'reason': reason[:MAX_REASON]}
    if rule: out['rule'] = rule[:MAX_REASON]
    for k in ('pay_range', 'title', 'company'):
        if _one_line(item.get(k)): out[k] = _one_line(item[k])
    return out, None


def _state():
    """Read only, no lock: the notes that note_add extends. The write goes through scout.py --job, which locks."""
    try:
        with open(paths.STATE, encoding='utf-8') as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return {}


def plan(items):
    """Verdicts -> (the --job groups, the lines to print, the problems)."""
    tracked = ((_state().get('jobs') or {}).get('tracked')) or {}
    groups, lines, problems, seen = [], [], [], set()
    for item in items:
        v, err = check(item)
        if err:
            problems.append(f'?? {(item or {}).get("id") if isinstance(item, dict) else item}: {err}'); continue
        if v['id'] in seen:
            problems.append(f'?? {v["id"]}: repetida no lote (fica a primeira)'); continue
        seen.add(v['id'])
        if 'note_add' in v:
            note = (tracked.get(v['id']) or {}).get('note') or ''
            if not note:
                problems.append(f'?? {v["id"]}: note_add numa vaga sem nota no funil (julgue antes com decision e fit)'); continue
            if v['note_add'] not in note:
                groups.append([v['id'], f'note={note.rstrip(" .")} · {v["note_add"]}'])
            lines.append(f'-- {v["id"]} · nota: + {v["note_add"]}'); continue
        det = detail_path(v['id'])
        has = os.path.exists(det)
        g = [v['id'], f'status={STATUS[v["decision"]]}', f'note={v["fit"]}: {v["reason"]}' + (f' (regra do perfil: {v["rule"]})' if v.get('rule') else '')]
        for k in ('pay_range', 'title', 'company'):
            if v.get(k): g.append(f'{k}={v[k]}')
        if has: g.append(f'judgment={det}')
        groups.append(g)
        miss = ' (sem arquivo de detalhe)' if v['fit'] in DETAIL_FITS and not has else ''
        lines.append(f'-- {v["id"]} · {v["decision"]} · {v["fit"]}: {v["reason"]} · {det if has else "-"}{miss}')
    return groups, lines, problems


def record(items, dry=False):
    groups, lines, problems = plan(items)
    rc = 0
    if groups and not dry:
        args = []
        for g in groups: args += (['+'] if args else []) + g
        r = subprocess.run([sys.executable, SCOUT, '--job'] + args, text=True, capture_output=True)
        if r.returncode:
            print(f'?? scout.py --job falhou ({r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}')
            return 1
    for l in lines: print(l)
    for p in problems: print(p)
    if problems: rc = 1
    return rc


def brief():
    with open(RULES, encoding='utf-8') as f: print(f.read().rstrip())
    print('\n## Caminhos desta instância\n')
    rows = [('J (scripts)', HERE), ('perfil', paths.PROFILE), ('instruções pessoais', paths.INSTRUCTIONS),
            ('config (pay_ranges)', paths.CONFIG), ('reputação', paths.REPUTATION),
            ('detalhe por vaga', os.path.join(paths.JUDGMENTS_DIR, '<id>.md'))]
    for k, p in rows:
        print(f'- {k}: {p}' + ('' if os.path.exists(p) or '<id>' in p else ' (não existe)'))


def main():
    ap = argparse.ArgumentParser(description='job judge (PLN0249): rules, detail path and verdict recording')
    ap.add_argument('--brief', action='store_true', help="the judge's rules and this instance's paths")
    ap.add_argument('--path', metavar='ID', help='the detail file of a job (creates the folder)')
    ap.add_argument('--record', metavar='FILE', help='records a JSON list of verdicts ("-" = stdin)')
    ap.add_argument('--dry', action='store_true', help='with --record: checks and prints, records nothing')
    a = ap.parse_args()
    if a.brief: brief(); return 0
    if a.path: print(detail_path(a.path, create=True)); return 0
    if a.record:
        try:
            if a.record == '-': raw = sys.stdin.read()
            else:
                with open(a.record, encoding='utf-8') as f: raw = f.read()
            items = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            print(f'?? veredito ilegivel: {e}'); return 1
        if isinstance(items, dict): items = [items]
        if not isinstance(items, list): print('?? o veredito e uma lista JSON'); return 1
        return record(items, dry=a.dry)
    ap.print_help(); return 2


if __name__ == '__main__':
    sys.exit(main())
