#!/usr/bin/env python3
"""Files that go with the interview and application tasks in Planou ("Anexos da tarefa"), sent by the heavy tick.

Every "Preparar entrevista" (i<id>) and "Aplicar" (a<id>) task the sync keeps gets, when the file exists:

  roteiro     the interview script (.md): the job's `prep` in the state, else <interviews_dir>/<Company>-<id>-entrevista.md
              (never the `-pt.md` copy nor a `.bak*`)
  cv          the CV sent (PDF): the job's `cv_sent`/`cv_pdf`, else the newest "*(<Company>).pdf" in cv_dir
  respostas   what was answered in the application and in the screening, in English, built here from the files of the
              applications folder (<applications_dir>/<Company>-triagem.md, and -candidatura.md, the record of the
              application sent, or else -aplicar.md, what the form was filled with): only the
              answer lines, with the Portuguese notes, contact data (phone, e-mail, links) and file paths taken out.
              The files themselves never leave the machine (they are instructions in Portuguese, with personal data).

The source_key of each kind is stable (`roteiro`, `cv`, `respostas`): the same file goes up once, a new version replaces
the old one (same attachment id). watch_core.planou.attach keeps the sha256 of what went up (cache/planou/) and retries
a task the sync has not created yet. Folders come from config.json: "interviews_dir", "cv_dir", "applications_dir".
"""
import glob, os, re

import criteria, paths

ROTEIRO, CV, RESPOSTAS = 'roteiro', 'cv', 'respostas'
NAMES = {ROTEIRO: 'Interview prep.md', RESPOSTAS: 'What I answered (application and screening).md'}
KINDS = ('i', 'a')            # Preparar entrevista, Aplicar


def slug(company):
    """The company part of the file names (the same rule as interview.py)."""
    return re.sub(r'[^A-Za-z0-9]+', '', company or '')[:30] or 'vaga'


def _dir(cfg, key, default=None):
    v = (cfg or {}).get(key) or default
    return os.path.expanduser(v) if v else None


def roteiro(jid, v, cfg):
    cands = [v.get('prep')] if v.get('prep') else []
    folder = _dir(cfg, 'interviews_dir', paths.INTERVIEWS_DIR)
    if folder: cands.append(os.path.join(folder, f'{slug(v.get("company"))}-{jid}-entrevista.md'))
    for p in cands:
        p = os.path.expanduser(p)
        if p.endswith('.md') and not p.endswith('-pt.md') and os.path.isfile(p): return p
    return None


def cv(jid, v, cfg):
    for k in ('cv_sent', 'cv_pdf'):
        p = os.path.expanduser(str(v.get(k) or ''))
        if p.lower().endswith('.pdf') and os.path.isfile(p): return p
    folder = _dir(cfg, 'cv_dir', paths.CV_DIR)
    company = re.sub(r'[,.]? (inc|ltda|s\.?a|llc|ltd|corp|co)\.?$', '', v.get('company') or '', flags=re.I).strip()
    if not folder or not company: return None
    found = [p for p in glob.glob(os.path.join(glob.escape(folder), '*.pdf'))
             if os.path.basename(p).lower().endswith(f'({company.lower()}).pdf')]
    return max(found, key=os.path.getmtime) if found else None


# ---------------------------------------------------------------- respostas

PT_WORDS = {'não', 'nao', 'você', 'voce', 'usuário', 'usuario', 'comentário', 'comentario', 'escolher', 'opção', 'opcao',
            'marcadas', 'marcada', 'desmarcadas', 'preencher', 'preencheu', 'preenchido', 'deixe', 'relate', 'branco',
            'perguntarem', 'perguntar', 'pergunta', 'dado', 'pelo', 'pela', 'lista', 'substituindo', 'duas', 'mão', 'mao',
            'se', 'ou', 'que', 'com', 'para', 'em', 'uma', 'um', 'anos', 'sim', 'decide', 'enviar', 'envio', 'vaga'}
PT_STOP = {'de', 'da', 'do', 'das', 'dos', 'na', 'no', 'nas', 'nos', 'e', 'o', 'os', 'as', 'a'}
PII = re.compile(r'\b(first|last|full)\s*name\b|\bnome\b|e-?mail|phone|telefone|celular|whatsapp|mobile|linkedin|github|'
                 r'website|portfolio|site\b|url\b|address|endere[cç]o|cpf|rg\b|passport|birth|nascimento', re.I)
PHONE = re.compile(r'\+?\d[\d\s().-]{7,}\d')
EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
LINK = re.compile(r'https?://|www\.|[A-Za-z]:\\|/mnt/|~/|\\\\')


def portuguese(text):
    """True for text written in Portuguese (a note to the browser agent), False for an answer in English."""
    t = (text or '').lower()
    if re.search(r'[ãõç]', t): return True
    words = re.findall(r"[a-zà-ú]+", t)
    if not words: return False
    if any(w in PT_WORDS for w in words): return True
    return len(words) >= 3 and sum(w in PT_STOP and w != 'a' for w in words) / len(words) > 0.3


def _clean(text):
    """The answer without Portuguese parentheses, Portuguese ';' parts, markup and a Portuguese 'e' between English words."""
    t = re.sub(r'\*\*|`', '', text or '').strip()
    t = re.sub(r'\s*\(([^()]*)\)', lambda m: '' if portuguese(m.group(1)) or LINK.search(m.group(1)) else m.group(0), t)
    parts = [p.strip() for p in re.split(r';\s*', t) if p.strip()]
    parts = [p for p in parts if not portuguese(p)]
    t = '; '.join(parts)
    t = re.sub(r'(?<=\w) e (?=[A-Z])', ' and ', t)
    return t.strip(' .;:')


def _line(label, value):
    """'- Label: value' or None when the pair must not leave (contact data, a path, a link, Portuguese)."""
    label, value = _clean(label), _clean(value)
    if not label or not value: return None
    if PII.search(label) or PHONE.search(value) or EMAIL.search(value) or LINK.search(value) or LINK.search(label): return None
    if portuguese(label) or portuguese(value): return None
    return f'- {label}: {value}'


def _section(md, *titles):
    """Lines of the first `## ` section whose title starts with one of `titles` (case-insensitive)."""
    out, on = [], False
    for ln in (md or '').splitlines():
        if ln.startswith('## '):
            on = any(ln[3:].strip().lower().startswith(t) for t in titles)
            continue
        if on: out.append(ln)
    return out


def _pairs_list(lines):
    """'1. Label: value' and '- Label: value' lines; a line with ' · ' holds several pairs."""
    out = []
    for ln in lines:
        m = re.match(r'\s*(?:\d+\.|[-*])\s+(.*)$', ln)
        if not m: continue
        for piece in m.group(1).split(' · '):
            label, sep, value = piece.partition(':')
            if sep: out.append((label, value))
    return out


def _pairs_table(lines):
    out = []
    for ln in lines:
        cells = [c.strip() for c in ln.strip().strip('|').split('|')]
        if len(cells) < 2 or set(cells[0]) <= set('-: ') or cells[0].lower() in ('campo', 'field', 'pergunta', 'question'):
            continue
        out.append((cells[0], cells[1]))
    return out


def _block(title, pairs):
    lines = [x for x in (_line(label, value) for label, value in pairs) if x]
    return [f'## {title}', ''] + lines + [''] if lines else []


def respostas(jid, v, cfg):
    """(bytes, name) of the English record of the answers, or None when there is nothing to send."""
    folder = _dir(cfg, 'applications_dir')
    if not folder: return None
    base = os.path.join(folder, slug(v.get('company')))
    read = lambda suffix: open(base + suffix, encoding='utf-8').read() if os.path.isfile(base + suffix) else ''
    body = []
    body += _block('Screening form', _pairs_list(_section(read('-triagem.md'), 'respostas', 'answers')))
    sent = _block('Application', _pairs_table(_section(read('-candidatura.md'), 'respostas', 'answers')))
    # the record of the application sent (-candidatura.md) wins over the list the form was filled from (-aplicar.md)
    body += sent or _block('Application', _pairs_list(_section(read('-aplicar.md'), 'dados', 'data', 'answers')))
    if not body: return None
    title = ', '.join(x for x in (v.get('company'), v.get('title')) if x) or f'Job {jid}'
    text = '\n'.join([f'# {title}: what I answered', ''] + body).rstrip() + '\n'
    return text.encode('utf-8'), NAMES[RESPOSTAS]


# ---------------------------------------------------------------- the tick

def send(s, codes, planou, cfg=None):
    """Attaches the files of each interview/application task in `codes` (the synced task codes). Returns lines."""
    cfg = cfg if cfg is not None else criteria.load()
    jobs = (s.get('jobs') or {}).get('tracked') or {}
    lines = []
    for code in sorted(codes):
        if code[:1] not in KINDS or code[1:] not in jobs: continue
        jid, v = code[1:], jobs[code[1:]]
        p = roteiro(jid, v, cfg)
        if p: lines += planou.attach(code, ROTEIRO, path=p, name=NAMES[ROTEIRO])
        p = cv(jid, v, cfg)
        if p: lines += planou.attach(code, CV, path=p)
        r = respostas(jid, v, cfg)
        if r: lines += planou.attach(code, RESPOSTAS, data=r[0], name=r[1])
    return lines
