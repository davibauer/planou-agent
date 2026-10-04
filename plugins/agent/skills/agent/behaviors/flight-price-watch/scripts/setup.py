#!/usr/bin/env python3
"""First use of travel-agent: creates ~/.config/travel-agent/ (see paths.py), copies the missing templates without
overwriting anything and creates the venv with fast-flights (the page fetcher of the Google Flights source).

Usage: setup.py            # folders, templates and venv
       setup.py --status   # what is already configured
"""
import os, sys, json, shutil, subprocess

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paths

TEMPLATES = os.path.join(os.path.dirname(HERE), 'config-example')
FILES = {'config.json': paths.CONFIG, 'instructions.md': paths.INSTRUCTIONS, 'past_trips.json': paths.PAST_TRIPS}
PACKAGES = ['fast-flights', 'typing_extensions',     # fast-flights 3.1 imports typing_extensions without declaring it
            'google-auth', 'requests']                # the spreadsheet (sheets.py), optional


def venv_ok():
    if not os.path.exists(paths.VENV_PYTHON): return False
    return subprocess.run([paths.VENV_PYTHON, '-c', 'import fast_flights, typing_extensions, google.auth, requests'], capture_output=True).returncode == 0


def status():
    print(f'pasta: {paths.ROOT} ({"existe" if os.path.isdir(paths.ROOT) else "nao existe"})')
    for label, p in [('config/config.json', paths.CONFIG), ('config/instructions.md', paths.INSTRUCTIONS), ('config/past_trips.json', paths.PAST_TRIPS),
                     ('data/state.json', paths.STATE), ('data/prices.jsonl', paths.PRICES)]:
        print(f'  {label:24s} {"ok" if os.path.exists(p) else "-"}')
    print(f'  {"cache/venv (fast-flights)":24s} {"ok" if venv_ok() else "-"}')
    try: c = json.load(open(paths.CONFIG, encoding='utf-8'))
    except Exception: c = {}
    trips = c.get('trips') or []
    print(f'  viagens: {len(trips)} ({", ".join(t["id"] + ":" + t.get("status", "watching") for t in trips) or "-"})')


def main():
    if '--status' in sys.argv: return status()
    paths.mkdirs()
    for name, dest in FILES.items():
        if not os.path.exists(dest): shutil.copy2(os.path.join(TEMPLATES, name), dest); print(f'criado: {dest}')
    if not venv_ok():
        subprocess.run([sys.executable, '-m', 'venv', paths.VENV], check=True)
        subprocess.run([paths.VENV_PYTHON, '-m', 'pip', 'install', '-q', *PACKAGES], check=True)
        print(f'venv pronto: {paths.VENV}')
    status()


if __name__ == '__main__':
    main()
