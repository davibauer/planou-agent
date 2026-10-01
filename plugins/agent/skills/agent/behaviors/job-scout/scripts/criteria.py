#!/usr/bin/env python3
"""Scout criteria (searches, gatekeeper, points, market, companies): a single JSON, adjusted by the user THROUGH Claude.

Single source: ~/.config/job-scout/config.json on top of config-example/config.json (key by key; whatever the user
did not define comes from the example). The scripts have no criteria in the code: they read from here.

  criteria.py --show                       # readable summary of what is in effect today (and where each key came from)
  criteria.py --validate [file.json]           # valid JSON, regexes compile, right types
  criteria.py --apply change.json --reason "what the user asked for"
        # change.json carries ONLY the keys that change, each with its whole new value (e.g. {"exclude": "..."}; null deletes the
        # key and it goes back to the default; old Portuguese key names are still accepted and renamed, see schema.py). Validates, shows the diff, keeps a copy (config.json.YYYYMMDD-HHMMSS) and logs to criteria.log
Claude builds the change from --show + the user's request ("drop Java jobs", "also search in Portugal"),
shows the diff (--apply ... --dry-run) and only really applies it with the user's ok.
"""
import os, re, sys, json, shutil, difflib, argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(os.path.dirname(HERE), 'config-example', 'config.json')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths, schema
from watch_core import fileio
DIR = paths.ROOT
CONFIG = paths.CONFIG
LOG = paths.CRITERIA_LOG
KEYS = {'searches': list, 'include': str, 'exclude': str, 'points': list, 'min_score': int,
          'boards': dict, 'companies': dict, 'logged_in_sources': bool, 'tz_hours': (int, float), 'pipeline_md': str, 'pay_ranges': dict,
          'skills': list, 'cv_profiles': dict, 'cv_dir': str, 'interviews_dir': str, 'applications_dir': str, 'tick_hooks': list,
          'daily_tasks': dict, 'kpi_targets': dict, 'shortlist_skip_process': str, 'saturation': dict}
META = ('schema',)          # bookkeeping keys written by the migration (schema.py), not criteria
# the agent's own settings, read by watch_core (planou.agent_settings) and by the agent plugin (its schema.py: behaviors,
# session, interval_s...): known keys, validated there, not here
AGENT = ('planou', 'live', 'test', 'mode', 'behaviors', 'behavior_config', 'session', 'interval_s', 'language', 'business_hours',
         'tools', 'autonomy', 'workspace', 'code', 'agent', 'max_reminders', 'retry_s', 'sources', 'hooks', 'runtime')

def _read(p):
    try: return json.load(open(p, encoding='utf-8'))
    except FileNotFoundError: return {}

def _user():
    """The user's config.json with the current key names (an old-format file restored by hand still works)."""
    return schema.config(_read(CONFIG), force=True)

def load():
    """Example + user (user wins key by key)."""
    c = {k: v for k, v in schema.config(_read(EXAMPLE), force=True).items() if not k.startswith('_') and k not in META}
    c.update({k: v for k, v in _user().items() if not k.startswith('_') and k not in META})
    return c

def tz():
    """User time zone (config tz_hours, default -3 = Brasilia): business hours, the 9am/6pm routines and printed dates."""
    from datetime import timezone, timedelta
    return timezone(timedelta(hours=float(load().get('tz_hours', -3))))

def pipeline_md():
    """Path of the markdown pipeline (config pipeline_md; default ~/.config/job-scout/funil.md; "" turns it off)."""
    v = load().get('pipeline_md', paths.PIPELINE)
    return os.path.expanduser(v) if v else ''

def errors(c):
    out = []
    for k, v in c.items():
        if k.startswith('_') or k in META or k in AGENT: continue
        if k not in KEYS: out.append(f'chave desconhecida: {k}'); continue
        if not isinstance(v, KEYS[k]) or (KEYS[k] is int and isinstance(v, bool)):
            t = KEYS[k]; out.append(f'{k}: esperado {t.__name__ if isinstance(t, type) else "numero"}')
    for k, name in (('include', 'incluir'), ('exclude', 'excluir')):
        try: re.compile(c.get(k) or '')
        except re.error as e: out.append(f'{name}: regex invalida ({e})')
    for i, p in enumerate(c.get('points') or []):
        if not (isinstance(p, list) and len(p) == 2): out.append(f'pontos[{i}]: use ["<regex>", "<rotulo>"]'); continue
        try: re.compile(p[0])
        except re.error as e: out.append(f'pontos[{i}] ({p[1]}): regex invalida ({e})')
    for i, b in enumerate(c.get('searches') or []):
        if not isinstance(b, dict) or not b.get('keywords') or not b.get('location'): out.append(f'searches[{i}]: precisa de keywords e location')
    for i, h in enumerate(c.get('skills') or []):
        if isinstance(h, list):
            try: re.compile(h[0])
            except (re.error, IndexError) as e: out.append(f'habilidades[{i}]: use "<rotulo do vocabulario>" ou ["<regex>", "<rotulo>"] ({e})')
        elif not isinstance(h, str): out.append(f'habilidades[{i}]: texto ou [regex, rotulo]')
    m = c.get('boards') or {}
    for f in m.get('sources', []):
        if f not in ('remoteok', 'wwr', 'web3career'): out.append(f'boards.sources: fonte desconhecida {f}')
    return out

def show():
    u, c = _user(), load()
    orig = lambda k: 'seu' if k in u else 'padrao'
    print(f'config: {CONFIG}')
    print(f'buscas ({orig("searches")}):')
    for b in c.get('searches', []): print(f'  - {b["keywords"]}  @ {b["location"]}{" (remoto)" if b.get("remote") else ""}')
    print(f'incluir no titulo ({orig("include")}): {c.get("include")}')
    print(f'excluir do titulo ({orig("exclude")}): {c.get("exclude")}')
    print(f'pontos ({orig("points")}): ' + ', '.join(p[1] for p in c.get('points', [])) + f'  | minimo para aparecer: {c.get("min_score")}')
    m = c.get('boards', {})
    print(f'mercado ({orig("boards")}): ' + ', '.join(m.get('sources', [])) + ''.join(f' | {f}: {", ".join(m[f])}' for f in ('wwr', 'web3career') if f in m.get('sources', []) and m.get(f)))
    print(f'habilidades ({orig("skills")}): ' + (', '.join(h if isinstance(h, str) else h[1] for h in c.get('skills', [])) or 'nenhuma (sem match rapido)'))
    print(f'perfis de CV ({orig("cv_profiles")}): ' + (', '.join(c.get('cv_profiles', {})) or 'nenhum'))
    print(f'empresas ({orig("companies")}): ' + (', '.join(v[0] for v in c.get('companies', {}).values()) or 'nenhuma'))
    print(f'inbox/recomendadas logadas: {"ligadas" if c.get("logged_in_sources") else "desligadas"}')
    print(f'fuso: UTC{c.get("tz_hours", -3):+g} | funil em markdown: {pipeline_md() or "desligado"}')

def apply(path, reason, dry_run=False):
    new = dict(_user())
    for k, v in schema.config(json.load(open(path, encoding='utf-8')), force=True).items():
        if v is None: new.pop(k, None)
        else: new[k] = v
    e = errors({**load(), **new})
    if e: sys.exit('nao aplicado:\n  ' + '\n  '.join(e))
    before = json.dumps(_user(), indent=1, ensure_ascii=False).splitlines()
    after = json.dumps(new, indent=1, ensure_ascii=False).splitlines()
    diff = list(difflib.unified_diff(before, after, 'antes', 'depois', lineterm='', n=0))
    if not diff: print('nada mudou'); return
    print('\n'.join(diff))
    if dry_run: print('\n(simulacao: nada gravado)'); return
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    os.makedirs(paths.CONFIG_DIR, exist_ok=True); os.makedirs(paths.HISTORY_DIR, exist_ok=True)
    if os.path.exists(CONFIG): shutil.copy2(CONFIG, os.path.join(paths.HISTORY_DIR, f'config.json.{ts}'))
    fileio.write_json(CONFIG, new, indent=1, ensure_ascii=False)
    with open(LOG, 'a', encoding='utf-8') as f: f.write(f'{ts} {reason}\n')
    print(f'\naplicado (copia anterior: data/history/config.json.{ts}; registro em data/history/criteria.log)')

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--show', '--mostra', dest='mostra', action='store_true'); ap.add_argument('--validate', '--valida', dest='valida', nargs='?', const='')
    ap.add_argument('--apply', '--aplica', dest='aplica'); ap.add_argument('--reason', '--motivo', dest='motivo', default=''); ap.add_argument('--dry-run', '--simula', dest='simula', action='store_true')
    a = ap.parse_args()
    if a.aplica:
        if not a.motivo and not a.simula: sys.exit('--reason e obrigatorio (o que o usuario pediu)')
        return apply(a.aplica, a.motivo, a.simula)
    if a.valida is not None:
        c = {**load(), **schema.config(json.load(open(a.valida, encoding='utf-8')), force=True)} if a.valida else load()
        e = errors(c); print('ok' if not e else '\n'.join(e)); sys.exit(1 if e else 0)
    show()

if __name__ == '__main__':
    main()
