#!/usr/bin/env python3
"""Fit matrix: job requirement x the user's real experience (request of 23/09/2026:
"um crosscheck do job description com a minha experiencia em cada coisa ... algo pra toda vaga").

Input (JSON, stdin or file): {"job": "<id>", "items": [{"req": "...", "level": "sim|parcial|nao", "evidence": "...", "study": "..."}]}
(the Portuguese keys of up to 0.8.x -- "vaga", "itens", "nivel", "evidencia", "estudar" -- are still accepted: schema.matrix_input)
  - req: requirement as the posting words it (one per item, in the posting's order)
  - level: sim = proven in real experience; parcial = adjacent/transferable or only part of the requirement; nao = does not have it
  - evidence: WHERE he did it (company, period, what) taken from profile.md/cv-base; never make it up
  - study: (parcial/nao) what to review before the interview; optional
Score: (sim + 0.5 x parcial) / total. Output: markdown table; with --save, stores match/matrix in the job's annotation
(state.json: jobs.tracked[id]) for Notion (Match column) and the application package.

Usage: matrix.py items.json [--save] [--md file.md]   (--md appends/replaces the '## Matriz de aderencia' section)
"""
import sys, json, os, re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema
from watch_core import fileio

ICON = {'sim': '✅', 'parcial': '🟡', 'nao': '❌'}

def score(items):
    n = len(items); s = sum(1 for i in items if i['level'] == 'sim'); p = sum(1 for i in items if i['level'] == 'parcial')
    return round(100 * (s + 0.5 * p) / n) if n else 0, s, p, n - s - p

def summary(items):
    pct, s, p, x = score(items)
    return f'{pct}% ({s}✅ {p}🟡 {x}❌ de {len(items)})'

def table(items):
    out = ['| # | Requisito da vaga | Você | Evidência (onde fez) | Preparar |', '|---|---|---|---|---|']
    for k, i in enumerate(items, 1):
        out.append(f"| {k} | {i['req']} | {ICON[i['level']]} | {i.get('evidence', '') or '-'} | {i.get('study', '') or '-'} |")
    out.append(f'\n**Aderência: {summary(items)}** (✅ = 1, 🟡 = 0,5, ❌ = 0)')
    return '\n'.join(out)

def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    d = schema.matrix_input(json.load(open(args[0])) if args else json.load(sys.stdin))
    items = d['items']
    for i in items:
        if i['level'] not in ICON: sys.exit(f"nivel invalido: {i['level']} (use sim|parcial|nao)")
    t = table(items); print(t)
    if '--save' in sys.argv or '--grava' in sys.argv:          # --grava: name up to 0.6.x
        import paths
        p = paths.STATE; paths.lock_state(); s = json.load(open(p))
        v = s['jobs']['tracked'].get(str(d['job']))
        if v is None: sys.exit(f"vaga {d['job']} nao anotada")
        v['match'] = summary(items); v['matrix'] = items
        fileio.write_json(p, s, indent=1, ensure_ascii=False)
        print(f"\n(gravado em {d['job']}: {v['match']})")
    if '--md' in sys.argv:
        f = sys.argv[sys.argv.index('--md') + 1]; text = open(f, encoding='utf-8').read()
        block = f'## Matriz de aderência (requisito x experiência)\n\n{t}\n'
        if '## Matriz de aderência' in text:
            text = re.sub(r'## Matriz de aderência.*?(?=\n---\n|\n## |\Z)', block.rstrip('\n'), text, flags=re.S)
        else:
            text = text.rstrip('\n') + '\n\n---\n\n' + block
        open(f, 'w', encoding='utf-8').write(text); print(f'(matriz escrita em {f})')

if __name__ == '__main__':
    main()
