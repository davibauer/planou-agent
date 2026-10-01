#!/usr/bin/env python3
"""User profile for judging jobs: ~/.config/job-scout/perfil.md (short prose, read by the model every
tick) + LinkedIn cache (linkedin-profile.json) + decision log (state.json, jobs.tracked).

  linkedin_profile.py --show              # prints perfil.md (the model reads this before judging)
  linkedin_profile.py --refresh           # re-pulls headline/about/skills/experiences from LinkedIn (3 calls; do NOT run in the tick)
                                         #   and rewrites only the "## Do LinkedIn" section of perfil.md; the rest stays intact
  linkedin_profile.py --note "text"      # appends a dated lesson to "## Aprendizados" (user's reaction to a job)
  linkedin_profile.py --learn          # prints perfil.md + decisions since the last review: material for the model to
                                         #   rewrite "## O que faz sentido" (and propose terms for the linkedin_jobs.py gatekeeper)
  linkedin_profile.py --reviewed          # marks the review (date) after the model rewrites the profile

The user's LinkedIn profile stops in 07/2015 (13 experiences, old skills): the "Do LinkedIn" section weighs little;
what counts is "O que faz sentido" + "Aprendizados", written from the current work and the user's reactions.
"""
import os, sys, re, json, argparse
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import criteria, paths, schema
from watch_core import fileio
PROFILE = paths.PROFILE
CACHE = paths.LINKEDIN_PROFILE
STATE = paths.STATE
BRT = criteria.tz()   # historical name: it is the user's timezone (tz_hours in the config, default -3)
SECTIONS = ('## O que faz sentido', '## Do LinkedIn', '## Aprendizados')

def read_profile():
    try: return open(PROFILE).read()
    except FileNotFoundError: sys.exit(f'{PROFILE} nao existe (o SKILL.md tem a semente; ou rode --refresh e escreva as outras secoes)')

def save(t):
    os.makedirs(paths.CONFIG_DIR, exist_ok=True)
    fileio.write(PROFILE, t)

def section(t, heading):
    """(start, end) of the body of section '## X' in the text, or None."""
    m = re.search(rf'^{re.escape(heading)}\s*$', t, re.M)
    if not m: return None
    n = re.search(r'^## ', t[m.end():], re.M)
    return m.end(), (m.end() + n.start()) if n else len(t)

def replace_section(t, heading, body):
    s = section(t, heading)
    body = '\n' + body.strip('\n') + '\n\n'
    if s: return t[:s[0]] + body + t[s[1]:]
    return t.rstrip('\n') + f'\n\n{heading}' + body

def refresh():
    import linkedin_msgs as lm
    me = lm.me(); pid = me['urn'].split(':')[-1]
    p = lm.api(f'/voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity={me["public"]}')['elements'][0]
    sk = lm.api(f'/voyager/api/identity/dash/profileSkills?q=viewee&profileUrn=urn%3Ali%3Afsd_profile%3A{pid}&count=100')
    pos = lm.api(f'/voyager/api/identity/dash/profilePositions?q=viewee&profileUrn=urn%3Ali%3Afsd_profile%3A{pid}&count=100')
    exps = []
    for e in pos.get('elements') or []:
        dr = e.get('dateRange') or {}; a, b = dr.get('start') or {}, dr.get('end') or {}
        exps.append({'title': e.get('title'), 'company': e.get('companyName'), 'start': f'{a.get("month", "")}/{a.get("year", "")}',
                     'end': f'{b.get("month", "")}/{b.get("year", "")}' if b else 'atual', 'description': (e.get('description') or '')[:400]})
    exps.sort(key=lambda x: x['start'].split('/')[-1], reverse=True)
    d = {'when': datetime.now(timezone.utc).isoformat(), 'name': f'{p.get("firstName", "")} {p.get("lastName", "")}', 'public': me['public'],
         'headline': p.get('headline', ''), 'about': p.get('summary', ''), 'location': p.get('geoLocation', {}).get('geo', {}).get('defaultLocalizedName') if isinstance(p.get('geoLocation'), dict) else '',
         'skills': [e['name'] for e in sk.get('elements') or []], 'experiences': exps}
    os.makedirs(paths.CACHE_DIR, exist_ok=True); json.dump(d, open(CACHE, 'w'), ensure_ascii=False, indent=1)
    last = exps[0] if exps else None
    warning = f'**Atenção: a última experiência cadastrada termina em {last["end"]} ({last["title"]} @ {last["company"]})** — o LinkedIn calcula recomendações e TOP APPLICANT com isso; atualizar o perfil melhora os dois.' if last and last['end'] != 'atual' else ''
    body = (f'_Puxado do LinkedIn em {datetime.now(BRT).strftime("%d/%m/%Y")} (`--refresh`). Fonte fraca: o que está aqui é o que o LinkedIn vê, não necessariamente o atual._\n\n'
             f'- **Headline:** {d["headline"]}\n- **Sobre (resumo):** ' + re.sub(r'\s+', ' ', d['about'])[:900] + '…\n'
             f'- **Skills ({len(d["skills"])}):** ' + ', '.join(d['skills'][:25]) + ('…' if len(d['skills']) > 25 else '') + '\n'
             f'- **Experiências ({len(exps)}):** ' + '; '.join(f'{e["title"]} @ {e["company"]} ({e["start"]}–{e["end"]})' for e in exps[:5]) + ('…' if len(exps) > 5 else '') + '\n'
             + (f'\n{warning}\n' if warning else ''))
    t = read_profile() if os.path.exists(PROFILE) else '# Perfil para vagas\n'
    save(replace_section(t, '## Do LinkedIn', body))
    print(f'perfil do LinkedIn atualizado em {PROFILE} ({len(d["skills"])} skills, {len(exps)} experiências)')
    if warning: print(warning)

def add_note(text):
    t = read_profile()
    line = f'- {datetime.now(BRT).strftime("%d/%m/%Y")}: {text.strip()}'
    mk = re.search(r'\n*<!-- revisado: \S+ -->\s*$', t); marker = mk.group(0).strip() if mk else ''
    if mk: t = t[:mk.start()]
    s = section(t, '## Aprendizados')
    if s: t = t[:s[1]].rstrip('\n') + '\n' + line + '\n' + t[s[1]:]
    else: t = t.rstrip('\n') + '\n\n## Aprendizados\n' + line + '\n'
    if marker: t = t.rstrip('\n') + '\n\n' + marker + '\n'
    save(t); print(line)

def decisions(since=None):
    try: s = schema.state(json.load(open(STATE)))      # no-op on a migrated state; an old one is read through the migration
    except (FileNotFoundError, json.JSONDecodeError): return []
    tracked = (s.get('jobs') or {}).get('tracked') or {}
    out = [(k, v) for k, v in tracked.items() if not since or (v.get('when') or '') > since]
    return sorted(out, key=lambda kv: kv[1].get('when') or '')

def learn():
    t = read_profile()
    m = re.search(r'<!-- revisado: (\S+) -->', t); since = m.group(1) if m else None
    print(t); print('\n' + '=' * 60)
    ds = decisions(since)
    print(f'DECISÕES desde a última revisão ({since or "nunca"}): {len(ds)}')
    for k, v in ds:
        print(f'- {v.get("when", "")[:10]} · {schema.label(v.get("status", "?")):9s} · {v.get("title", "")} — {v.get("company", "")} · {v.get("note", "")} · https://www.linkedin.com/jobs/view/{k}')
    print('\nTarefa do modelo: reescrever "## O que faz sentido" a partir das decisões e dos aprendizados (curto, ≤25 linhas), '
          'manter "## Aprendizados" como histórico, propor ao usuário termos para entrar/sair do porteiro (EXCLUIR/INCLUIR do '
          'linkedin_jobs.py) só se um padrão se repetiu ≥3 vezes, e rodar --reviewed ao terminar.')

def mark_reviewed():
    t = read_profile(); now = datetime.now(timezone.utc).isoformat()
    t = re.sub(r'<!-- revisado: \S+ -->', f'<!-- revisado: {now} -->', t) if '<!-- revisado:' in t else t.rstrip('\n') + f'\n\n<!-- revisado: {now} -->\n'
    save(t); print(f'revisão marcada: {now}')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--show', action='store_true'); ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--note', '--anota', dest='anota', metavar='TEXT'); ap.add_argument('--learn', '--aprender', dest='aprender', action='store_true'); ap.add_argument('--reviewed', '--revisado', dest='revisado', action='store_true')
    a = ap.parse_args()
    if a.refresh: refresh()
    elif a.anota: add_note(a.anota)
    elif a.aprender: learn()
    elif a.revisado: mark_reviewed()
    else: print(read_profile())

if __name__ == '__main__':
    main()
