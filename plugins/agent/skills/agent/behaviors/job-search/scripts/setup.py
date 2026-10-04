#!/usr/bin/env python3
"""First use of job-scout on the agent plugin: creates the instance folder (~/.config/agent/job-scout/: config/, data/,
secrets/, cache/; see paths.py) and copies the missing templates, without overwriting anything. A new config.json also
gets what the agent plugin needs to run the instance: "behaviors": ["job-search"], "interval_s": 1800 and "live": true. Then Claude runs the setup questions (SKILL.md, Setup section) and
rewrites profile.md and config.json with the answers.

Usage: setup.py            # creates the folders and copies the missing templates
       setup.py --status   # shows what is already configured
"""
import os, sys, json, shutil

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paths, schema

TEMPLATES = os.path.join(os.path.dirname(HERE), 'config-example')
AGENT_KEYS = {'behaviors': ['job-search'], 'interval_s': 1800, 'live': True}
FILES = {'config.json': paths.CONFIG, 'profile.md': paths.PROFILE, 'instructions.md': paths.INSTRUCTIONS}

def status():
    print(f'pasta: {paths.ROOT} ({"existe" if os.path.isdir(paths.ROOT) else "nao existe"})')
    for label, p in [('config/config.json', paths.CONFIG), ('config/profile.md', paths.PROFILE), ('config/instructions.md', paths.INSTRUCTIONS),
                   ('config/resume-base.md', paths.RESUME_BASE), ('data/state.json', paths.STATE), ('secrets/session.json', paths.SESSION),
                   ('secrets/notion.env', paths.NOTION_ENV)]:
        print(f'  {label:24s} {"ok" if os.path.exists(p) else "-"}')
    try: c = schema.config(json.load(open(paths.CONFIG)), force=True)
    except Exception: c = {}
    print(f'  fontes logadas (inbox/recomendadas): {"ligadas" if c.get("logged_in_sources") else "desligadas"}')
    print(f'  buscas configuradas: {len(c.get("searches") or [])}')

def main():
    if '--status' in sys.argv: return status()
    paths._mkdirs()
    for template, dst in FILES.items():
        if os.path.exists(dst): print(f'mantido  {dst}')
        elif template == 'config.json':
            c = json.load(open(os.path.join(TEMPLATES, template), encoding='utf-8'))
            c.update(AGENT_KEYS)
            with open(dst, 'w', encoding='utf-8') as f: f.write(json.dumps(c, ensure_ascii=False, indent=2) + '\n')
            print(f'criado   {dst}')
        else: shutil.copy(os.path.join(TEMPLATES, template), dst); print(f'criado   {dst}')
    print('\nproximo passo: responder as perguntas do setup para reescrever profile.md e config.json')

if __name__ == '__main__':
    main()
