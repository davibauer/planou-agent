#!/usr/bin/env python3
"""Due-date math of the construction-management behavior (PLN0975): what is overdue, due soon or waiting too long.

  python3 construction_dates.py [--today YYYY-MM-DD] [--warn-days N] [--followup-days N] <file.json|->

Input: a JSON list of items, or an object with a "stages" list (the behavior's stages_file). An item has a "title" and:
- "date" (or "end", for a stage) and "source": a deadline, an installment, the end of a stage. A date without a source
  never counts as a date ("A CONFIRMAR ... sem fonte"); no date at all is "A CONFIRMAR ... sem data";
- "since": a pending answer from a supplier, asked on that day (no source needed: the day of the request is the fact).
Items with "status": "done", "done": true or a true "paid" (an installment already paid: true, 1, or the text
"true", "sim" or "1") are skipped. Dates are YYYY-MM-DD or DD/MM/YYYY. In a stages file, A CONFIRMAR is printed only for the
stages that become tasks: the active ones and the next ones (todo with every dependency done or active).

Output, one line per item that needs attention, in this order: VENCIDO and HOJE (most overdue first), PARADO (no answer
for --followup-days or more, longest first), VENCE (within --warn-days, soonest first), A CONFIRMAR. Items outside the
window print nothing. --warn-days goes from 1 to 90, like the behavior option.
Exit 0; 2 with the reason when the input or an option is invalid (nothing printed but the errors).
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


PAID_TEXT = ('true', 'sim', '1')


def is_paid(v):
    """True for a paid installment: the bool true, the integer 1 or the text "true", "sim" or "1" (any case)."""
    if isinstance(v, bool): return v
    return (isinstance(v, int) and v == 1) or (isinstance(v, str) and v.strip().lower() in PAID_TEXT)


def items_of(data):
    """(items, later): later holds the index of each stage that is not yet a task (todo with a dependency that is not
    done or active, an unknown one included); empty for a plain list."""
    later = set()
    if isinstance(data, dict):
        data = data.get('stages')
        if isinstance(data, list):
            status = {s.get('code'): s.get('status') for s in data if isinstance(s, dict) and s.get('code')}
            for i, s in enumerate(data):
                deps = s.get('depends_on') if isinstance(s, dict) else None
                if (isinstance(s, dict) and s.get('status', 'todo') == 'todo' and isinstance(deps, list)
                        and any(status.get(d) not in ('done', 'active') for d in deps)):
                    later.add(i)
    if not isinstance(data, list): raise ValueError('esperava uma lista JSON de itens, ou um objeto com "stages"')
    return data, later


def check(data, today, warn_days=7, followup_days=5):
    """[(order, line)] sorted, or ValueError listing every problem."""
    out, err = [], []
    items, later = items_of(data)
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict): err.append(f'item {i}: precisa ser um objeto'); continue
        title = ' '.join(str(it.get('title') or '').split())
        if not title: err.append(f'item {i}: "title" vazio'); continue
        if it.get('done') is True or is_paid(it.get('paid')) or it.get('status') == 'done': continue
        try:
            since, due = day(it.get('since')), day(it.get('date', it.get('end')))
        except ValueError as e:
            err.append(f'item {i} ({title}): {e}'); continue
        src = ' '.join(str(it.get('source') or '').split())
        if since:
            n = (today - since).days
            if n >= followup_days: out.append((1, -n, f'PARADO {title}: sem resposta há {n} dias'))
        elif not due or not src:
            if i - 1 not in later:    # a stage that is not a task yet: its date is not needed now
                out.append((3, 0, f'A CONFIRMAR {title}: ' + ('sem data' if not due else f'sem fonte para {due:%d/%m/%Y}')))
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
    ap.add_argument('--warn-days', type=int, default=7, help='janela do VENCE, em dias, de 1 a 90 (padrao 7)')
    ap.add_argument('--followup-days', type=int, default=5, help='dias sem resposta para PARADO (padrao 5)')
    a = ap.parse_args(argv)
    try:
        raw = sys.stdin.read() if a.file == '-' else open(a.file, encoding='utf-8').read()
        today = day(a.today) or date.today()
        if not 1 <= a.warn_days <= 90: raise ValueError(f'--warn-days {a.warn_days}: de 1 a 90, como a opcao warn_days')
        if a.followup_days < 1: raise ValueError('--followup-days >= 1')
        lines = check(json.loads(raw), today, a.warn_days, a.followup_days)
    except (OSError, ValueError) as e:          # json.JSONDecodeError is a ValueError
        print(f'ERRO: {e}', file=sys.stderr)
        return 2
    print('\n'.join(lines) if lines else 'nada vence nem parou')
    return 0


if __name__ == '__main__':
    sys.exit(main())
