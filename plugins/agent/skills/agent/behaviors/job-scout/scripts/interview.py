#!/usr/bin/env python3
"""Interview prep script, built from what the scout already knows about the job: matrix (what you have and what to
review), reputation, pay range, who posted it, description. Leaves marked the sections Claude fills in (likely
questions, STAR stories) only with facts from perfil.md.

  interview.py <id> [--dir FOLDER]    # writes <FOLDER>/<company>-<id>-entrevista.md and marks the job as prepared
Default folder: config "interviews_dir", else ~/.config/job-scout/data/interviews/.
"""
import os, re, sys, json, argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import criteria, paths, schema
from watch_core import fileio

QUESTIONS_TO_ASK = [
    'Como é o time (tamanho, senioridade) e com quem eu trabalharia no dia a dia?',
    'Formato de contratação (PJ/contractor/CLT), faixa e forma de pagamento; em moeda estrangeira, como é o câmbio?',
    'Tem on-call? Como funciona e como é compensado?',
    'Como é medido o sucesso nos primeiros 90 dias?',
    'Quais são os próximos passos do processo e o prazo de decisão?',
]
# reputation.json key -> label printed in the script (the old key names, so the text stays the same)
REP_LABEL = {'kind': 'tipo', 'glassdoor': 'glassdoor', 'alerts': 'alertas', 'process': 'processo seletivo', 'date': 'data'}

def _read(p, default):
    try: return json.load(open(p, encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError): return default

def build(jid, folder):
    sp = paths.STATE; paths.lock_state(); s = _read(sp, {})
    v = (s.get('jobs') or {}).get('tracked', {}).get(str(jid))
    if not v: sys.exit(f'vaga {jid} nao anotada')
    rep = schema.reputation(_read(paths.REPUTATION, {}))
    comp = v.get('company', '?'); r = next((x for k, x in rep.items() if k.lower() in comp.lower() or comp.lower() in k.lower()), None)
    ranges = paths.pay_ranges(criteria.load())
    desc = ''
    try:
        import linkedin_jobs as lj
        d = lj.detail(str(jid)); desc = d['description']
        if d.get('recruiter') and not v.get('recruiter'): v['recruiter'] = d['recruiter']
    except Exception as e:
        desc = f'(descricao indisponivel agora: {e})'
    url = v.get('url') or f'https://www.linkedin.com/jobs/view/{jid}'
    L = [f'# Entrevista: {v.get("title", "")} ({comp})', '',
         f'Preparado em {datetime.now(criteria.tz()).strftime("%d/%m/%Y")} pelo job-scout. Vaga: {url}', '',
         '## Resumo', '',
         f'- Encaixe: {(v.get("note") or "").split(":")[0] or "?"} · match: {v.get("match") or "-"} · formato: {v.get("work_format") or "-"}',
         f'- Faixa: {v.get("pay_range") or "-"}' + (f' · sua referência: {ranges.get("user_reference")}' if ranges.get('user_reference') else ''),
         f'- Nota do agent: {v.get("note") or "-"}']
    if v.get('recruiter'):
        rc = v['recruiter']; L.append(f'- Quem publicou: {rc["name"]} ({rc.get("headline", "")}) {rc.get("profile", "")}')
    if v.get('cv_gdoc') or v.get('cv_pdf'): L.append(f'- CV enviado: {v.get("cv_sent") or v.get("cv_pdf") or ""} {v.get("cv_gdoc") or ""}')
    L += ['', '## Empresa', '']
    if r: L += [f'- {REP_LABEL[k]}: {val}' for k, val in r.items() if k in REP_LABEL] + [f'- {k}: {val}' for k, val in (r.get('extra') or {}).items()] + ['']
    else: L += ['- (sem reputação em cache: pesquisar Glassdoor e notícias antes da conversa)', '']
    mat = v.get('matrix') or []
    L += ['## O que você tem (vira história para contar)', '']
    L += [f'- {i["req"]}: {i.get("evidence") or "-"}' for i in mat if i.get('level') == 'sim'] or ['- (sem matriz: rodar a matriz desta vaga primeiro)']
    L += ['', '## Revisar antes (🟡 parcial / ❌ não tem)', '']
    L += [f'- {"🟡" if i["level"] == "parcial" else "❌"} {i["req"]}: {i.get("study") or "revisar"}' for i in mat if i.get('level') != 'sim'] or ['- nada marcado']
    L += ['', '## Perguntas prováveis e respostas', '', '<!-- CLAUDE: 5-8 perguntas que esta vaga tende a fazer (a partir dos requisitos e da descrição) e, para cada uma, '
          'a resposta em 3-5 linhas no formato situação, ação, resultado, só com fatos do perfil.md -->', '',
          '## Perguntas para você fazer', ''] + [f'- {q}' for q in QUESTIONS_TO_ASK]
    L += ['', '## Descrição da vaga', '', desc.strip()[:6000], '']
    folder = os.path.expanduser(folder or criteria.load().get('interviews_dir') or paths.INTERVIEWS_DIR)
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, f'{re.sub(r"[^A-Za-z0-9]+", "", comp)[:30] or "vaga"}-{jid}-entrevista.md')
    open(out, 'w', encoding='utf-8').write('\n'.join(L))
    v['prep'] = out
    fileio.write_json(sp, s, indent=1, ensure_ascii=False)
    print(f'roteiro em {out}\nfalta o Claude completar: "Perguntas prováveis e respostas" (marcado com CLAUDE no arquivo)')

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('id'); ap.add_argument('--dir')
    a = ap.parse_args(); build(a.id, a.dir)
