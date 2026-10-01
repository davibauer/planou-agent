"""Deadlines (Planou "Prazo", the `deadline` of the sync): the day a source says a task is due by, as a local YYYY-MM-DD.

The Prazo is not the Data (`due`, the day to do it): it is sent only when the origin states one, and never invented.

  resolve(text, ref)            a deadline written in free text ("até sexta", "prazo 03/10", "by Friday", "até amanhã"),
                                resolved against `ref`, the date the text was written (the meeting, the message); only
                                right after a deadline word ("até", "prazo", "antes de", "by", "until", "due"...), so
                                "sexta tem reunião" is not a deadline
  resolve(cell, ref, marker=False)
                                the deadline cell of the minutes' action table: the whole cell is the deadline ("quarta",
                                "até 03/10", "em 2 semanas"); a conditional or open cell ("quando o material chegar",
                                "a definir") or a month without a day ("novembro") has none
  from_source(value)            a date or datetime from a tracker API (ADO DueDate, Jira duedate, GitLab due_date),
                                converted to the local day
  valid(value)                  the value when it is a real YYYY-MM-DD, else None (a bad format fails the task in Planou)

A weekday is the next one from `ref`, counting `ref` itself ("até sexta" written on a Friday = that day); "próxima"/"next"
skips `ref`. Standard library only.
"""
import re
from datetime import date, datetime, timedelta, timezone

BRT = timezone(timedelta(hours=-3))

WEEKDAYS = {'segunda': 0, 'terca': 1, 'terça': 1, 'quarta': 2, 'quinta': 3, 'sexta': 4, 'sabado': 5, 'sábado': 5,
            'domingo': 6, 'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3, 'friday': 4, 'saturday': 5, 'sunday': 6}
_WD = '|'.join(sorted(WEEKDAYS, key=len, reverse=True))
# the word that makes what follows a deadline (pt and en)
MARKERS = (r'(?:at[eé]|antes d[eoa]s?|prazo(?:\s*(?:final|de entrega|para entrega))?(?:\s*(?:[eé]|de|at[eé]|:|-))?|'
           r'no m[aá]ximo|vence(?:ndo)?(?:\s+(?:em|na|no))?|entregar (?:na|no|em)|by|until|till|before|no later than|'
           r'due(?:\s+(?:on|by|date:?))?|deadline(?:\s*(?:is|of|:))?)')
# job ads: only an explicit closing date for applications is a deadline (the posting's expiry is not)
APPLY_MARKERS = (r'(?:apply (?:by|before|until)|applications? (?:close|closes|are due|due|deadline)(?:\s+(?:on|by|is|:))?|'
                 r'application deadline(?:\s*(?:is|:))?|deadline (?:to|for) appl(?:y|ications?)(?:\s*(?:is|:))?|'
                 r'inscri[cç][oõ]es (?:at[eé]|encerram(?: em)?)|candidaturas? at[eé]|'
                 r'prazo (?:de|para) (?:inscri[cç][aã]o|inscri[cç][oõ]es|candidatura)(?:\s*(?:[eé]|at[eé]|:))?)')
# a goodbye or a thanks is no request: "valeu, até amanhã!", "ok, até segunda", "bom fds, até sexta"
FAREWELL = (r'(?:valeu|vlw|obrigad[oa]s?|brigad[oa]|tchau|abra[cç]os?|abs|beijos?|bjs|bom (?:fds|fim de semana|descanso|dia)|'
            r'boa (?:noite|tarde|semana|sorte)|beleza|blz|ok|okay|falou|flw|combinado|fechado|perfeito|[oó]timo|legal|show|'
            r'at[eé] mais|thanks|thank you|thx|cheers|bye|see you|talk soon|ttyl|great|cool|sure|sounds good)')
_LEAD = r'\s*(?:o dia |a |o |na |no |dia |the |this |esta |essa |nesta |nessa |)'
_NEXT = r'(?:(?P<next>pr[oó]xim[ao]|next|que vem)\s+)?'


def valid(value):
    """The value when it is an ISO date (YYYY-MM-DD) that exists, else None."""
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value): return None
    try: date.fromisoformat(value)
    except ValueError: return None
    return value


def from_source(value, tz=BRT):
    """'2026-10-03', '2026-10-03T03:00:00Z' or '2026-10-03T00:00:00-03:00' -> the local day, or None."""
    if not value: return None
    v = str(value).strip()
    if valid(v[:10]) and len(v) == 10: return v
    try:
        d = datetime.fromisoformat(v.replace('Z', '+00:00'))
    except ValueError:
        return None
    return (d.astimezone(tz) if d.tzinfo else d).date().isoformat()


def _ref(ref):
    if isinstance(ref, datetime): return (ref.astimezone(BRT) if ref.tzinfo else ref).date()
    if isinstance(ref, date): return ref
    if isinstance(ref, str) and ref:
        try:
            d = datetime.fromisoformat(ref.replace('Z', '+00:00'))
            return (d.astimezone(BRT) if d.tzinfo else d).date()
        except ValueError:
            pass
    return datetime.now(BRT).date()


def _relative(t, ref):
    """A deadline at the start of `t` (lower case): weekday, hoje/amanhã, end of the day/week/month; None otherwise."""
    m = re.match(_LEAD + _NEXT + r'(?P<wd>' + _WD + r')(?:[- ]feira)?\b(?:\s+(?P<next2>que vem))?', t)
    if m:
        delta = (WEEKDAYS[m['wd']] - ref.weekday()) % 7
        if (m['next'] or m['next2']) and delta == 0: delta = 7
        return ref + timedelta(days=delta)
    if re.match(_LEAD + r'(?:hoje|today|tonight|o fim do dia|fim do dia|final do dia|end of (?:the )?day|eod|cob)\b', t):
        return ref
    if re.match(_LEAD + r'(?:amanh[aã]|tomorrow)\b', t):
        return ref + timedelta(days=1)
    if re.match(_LEAD + r'(?:(?:o )?(?:fim|final) d[ae]s?(?:ta|sa)? semana|end of (?:the |this )?week|eow)\b', t):
        return ref + timedelta(days=(4 - ref.weekday()) % 7)
    if re.match(_LEAD + r'(?:(?:o )?(?:fim|final) d[oe]s?(?:te|se)? m[eê]s|end of (?:the |this )?month|eom)\b', t):
        nxt = (ref.replace(day=28) + timedelta(days=4)).replace(day=1)
        return nxt - timedelta(days=1)
    return None


def _absolute(t, ref):
    """A date at the start of `t`: dd/mm[/aaaa], aaaa-mm-dd, "3 de outubro", "Oct 3" (watch_core.recordings rules)."""
    from .recordings import _data_do_prazo
    if not re.match(_LEAD + r'(?:\d{1,2}\s*(?:/|de\s|[a-z])|\d{4}-\d{2}-\d{2}|[a-zç]{3,}\.?\s+\d{1,2}\b)', t): return None
    d = _data_do_prazo(t[:30], ref)
    return d[0] if d and len(d[1]) == 10 else None          # a month without a day ("novembro") is no clear deadline


def _asks(t, start):
    """In a message: something is asked before the deadline word, in the same sentence and clause. A clause empty or
    made only of a goodbye/thanks is not a request ("valeu, até amanhã!", "até sexta, bom fds")."""
    clause = re.split(r'[.!?;\n]|,', t[:start])[-1].strip()
    if not clause: return False
    return not re.fullmatch(r'(?:' + FAREWELL + r'[\s,]*)+', clause)


def resolve(text, ref=None, marker=True, markers=MARKERS, message=False):
    """The deadline in `text` (YYYY-MM-DD) resolved against `ref` (date, datetime or ISO string of when it was
    written), or None. marker=True: only a date right after a deadline word; marker=False: the whole text is a deadline
    cell (conditional or open cells have none). message=True (a chat or e-mail): the deadline word must follow a
    request in the same clause (a goodbye "até amanhã" is none), and "até hoje não ..." (= so far) is none."""
    t = ' '.join(str(text or '').lower().split())
    if not t: return None
    r = _ref(ref)
    for m in re.finditer(r'(?<![\w-])' + markers + r'(?![\w])', t):
        tail = t[m.end():m.end() + 40]
        if message:
            if not _asks(t, m.start()): continue
            if re.match(r'\s*(?:hoje|agora|now|today)\b[\s,]*(?:n[aã]o|ningu[eé]m|nada|nunca|nenhum|no\b|nobody|nothing|never)', tail):
                continue
        d = _relative(tail, r) or _absolute(tail, r)
        if d: return d.isoformat()
    if marker: return None
    from .recordings import PRAZO_CONDICIONAL, PRAZO_INDEFINIDO
    if any(re.search(p, t, re.I) for p in PRAZO_CONDICIONAL + PRAZO_INDEFINIDO): return None
    d = _relative(t, r)
    if d: return d.isoformat()
    from .recordings import _data_do_prazo
    d = _data_do_prazo(t, r)
    return d[0].isoformat() if d and len(d[1]) == 10 else None
