#!/usr/bin/env python3
"""Due-date math of the construction-management behavior (PLN0975): what is overdue, due soon or waiting too long.

  python3 construction_dates.py [--today YYYY-MM-DD] [--warn-days N] [--followup-days N] <file.json|->

Input: a JSON list of items, or an object with a "stages" list (the behavior's stages_file). An item has a "title" and:
- "date" (or "end", for a stage) and "source": a deadline, an installment, the end of a stage. A date without a source
  never counts as a date ("A CONFIRMAR ... sem fonte"); no date at all is "A CONFIRMAR ... sem data";
- "since": a pending answer from a supplier, asked on that day (no source needed: the day of the request is the fact).
Items with "status": "done", "done": true or "paid": true (an installment already paid) are skipped. Dates are YYYY-MM-DD or DD/MM/YYYY.

Output, one line per item that needs attention, overdue first: VENCIDO, HOJE, VENCE (within --warn-days), PARADO (no
answer for --followup-days or more), A CONFIRMAR. Items outside the window print nothing.
Exit 0; 2 with the reason when the input is invalid (nothing printed but the errors).
"""
import argparse, json, sys
from datetime import date, datetime


def day(v):
    """YYYY-MM-DD or DD/MM/YYYY -> date; None for empty; ValueError otherwise."""
    if v in (None, ''): return None
    s = str(v).strip()
    for fmt in ('%Y-%m-%d', '%d/%m/%Y'):
        try: return datetime.strptime(s, fmt).date()
        except ValueError: pass
    raise ValueError(f'data invalida {s!r} (AAAA-MM-DD ou DD/MM/AAAA)')


def items_of(data):
    if isinstance(data, dict): data = data.get('stages')
    if not isinstance(data, list): raise ValueError('esperava uma lista JSON de itens, ou um objeto com "stages"')
    return data


def check(data, today, warn_days=7, followup_days=5):
    """[(order, line)] sorted, or ValueError listing every problem."""
    out, err = [], []
    for i, it in enumerate(items_of(data), 1):
        if not isinstance(it, dict): err.append(f'item {i}: precisa ser um objeto'); continue
        title = ' '.join(str(it.get('title') or '').split())
        if not title: err.append(f'item {i}: "title" vazio'); continue
        if it.get('done') is True or it.get('paid') is True or it.get('status') == 'done': continue
        try:
            since, due = day(it.get('since')), day(it.get('date', it.get('end')))
        except ValueError as e:
            err.append(f'item {i} ({title}): {e}'); continue
        src = ' '.join(str(it.get('source') or '').split())
        if since:
            n = (today - since).days
            if n >= followup_days: out.append((1, -n, f'PARADO {title}: sem resposta há {n} dias'))
        elif not due:
            out.append((3, 0, f'A CONFIRMAR {title}: sem data'))
        elif not src:
            out.append((3, 0, f'A CONFIRMAR {title}: sem fonte para {due:%d/%m/%Y}'))
        else:
            n = (due - today).days
            if n < 0: out.append((0, n, f'VENCIDO {title}: venceu há {-n} dia{"s" if n < -1 else ""} em {due:%d/%m} ({src})'))
            elif n == 0: out.append((0, 0, f'HOJE {title}: vence hoje ({src})'))
            elif n <= warn_days: out.append((2, n, f'VENCE {title}: em {n} dia{"s" if n > 1 else ""}, {due:%d/%m} ({src})'))
    if err: raise ValueError('\n'.join(err))
    return [line for *_, line in sorted(out, key=lambda x: (x[0], x[1]))]


def main(argv=None):
    ap = argparse.ArgumentParser(description='Prazos, parcelas e pendencias da obra: o que venceu, vence ou parou')
    ap.add_argument('file', help="lista JSON de itens, ou '-' para a entrada padrao")
    ap.add_argument('--today', help='o dia de hoje (AAAA-MM-DD), para teste')
    ap.add_argument('--warn-days', type=int, default=7, help='janela do VENCE, em dias (padrao 7)')
    ap.add_argument('--followup-days', type=int, default=5, help='dias sem resposta para PARADO (padrao 5)')
    a = ap.parse_args(argv)
    try:
        raw = sys.stdin.read() if a.file == '-' else open(a.file, encoding='utf-8').read()
        today = day(a.today) or date.today()
        if a.warn_days < 0 or a.followup_days < 1: raise ValueError('--warn-days >= 0 e --followup-days >= 1')
        lines = check(json.loads(raw), today, a.warn_days, a.followup_days)
    except (OSError, ValueError) as e:          # json.JSONDecodeError is a ValueError
        print(f'ERRO: {e}', file=sys.stderr)
        return 2
    print('\n'.join(lines) if lines else 'nada vence nem parou')
    return 0


if __name__ == '__main__':
    sys.exit(main())
