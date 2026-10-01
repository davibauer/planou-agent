#!/usr/bin/env python3
"""Material shared by people (links and files) kept in the instance workspace: `<workspace>/messages/shared/`.

Asked on 26/09/2026 ("quando for algum material compartilhado, seria interessante disponibilizar isso no desktop do
agente"): an article link or a PDF sent in a chat or e-mail used to live only in the tick output, and the file itself
stayed on somebody's OneDrive. Now the message sources hand it here and the tick prints the local path.

  messages/shared/INDEX.md                      one line per item: date · who · where · what (link or file)
  messages/shared/AAAA-MM-DD <name>             the file itself (Teams file attachment, e-mail attachment)

Only what PEOPLE send: the sources call this for messages that are not mine and not from bots/automatic senders.
Links to hosts in the ignore list (meeting joins, the sources' own APIs, the company's internal apps and git) are not
material and are skipped. Nothing is written on dry ticks or in test mode (the sources only call it when not dry).

Config (top level of config.json, all optional):
  "compartilhado": {"ignorar_hosts": ["git.empresa.com", ...],   # added to IGNORAR_PADRAO (substring of the host)
                    "max_mb": 25,                                # bigger attachments are listed, not downloaded
                    "desligado": false}
"""
import os, re, urllib.parse
from datetime import datetime

import paths

IGNORAR_PADRAO = ('teams.microsoft.com', 'teams.live.com', 'teams.cloud.microsoft', 'graph.microsoft.com',
                  'schema.skype.com', 'statics.teams.cdn.office.net', 'login.microsoftonline.com', 'aka.ms',
                  'outlook.office.com', 'outlook.office365.com', 'outlook.live.com', 'safelinks.protection.outlook.com',
                  'meet.google.com', 'zoom.us', 'mail.google.com', 'calendar.google.com', 'slack.com/archives',
                  'app.slack.com', 'w3.org', 'schemas.microsoft.com', 'schemas.openxmlformats.org')
IGNORAR = list(IGNORAR_PADRAO)
MAX_BYTES = 25 * 1024 * 1024
LIGADO = True
LINK = re.compile(r'https?://[^\s<>"\'\)\]]+')


def configura(cfg):
    global IGNORAR, MAX_BYTES, LIGADO
    c = cfg.get('compartilhado') or {}
    IGNORAR = list(IGNORAR_PADRAO) + [h.lower() for h in c.get('ignorar_hosts') or []]
    MAX_BYTES = int(float(c.get('max_mb', 25)) * 1024 * 1024)
    LIGADO = not c.get('desligado')


def pasta():
    return paths.area(os.path.join('messages', 'shared'), criar=True)


def _index():
    return os.path.join(pasta(), 'INDEX.md')


def _ja_tem(chave):
    try:
        with open(_index(), encoding='utf-8') as fh: return chave in fh.read()
    except FileNotFoundError: return False


def _linha(texto):
    f = _index()
    novo = not os.path.exists(f)
    with open(f, 'a', encoding='utf-8') as fh:
        if novo:
            fh.write('# Material compartilhado\n\nLinks e arquivos que as pessoas mandam nos chats e e-mails. '
                     'Um item por linha: data · quem · onde · o que é.\n\n')
        fh.write(texto.rstrip() + '\n')


def _dia(quando):
    try: return datetime.fromisoformat((quando or '').replace('Z', '+00:00')).date().isoformat()
    except ValueError: return datetime.now().date().isoformat()


def _limpa(s, n=90):
    s = re.sub(r'\s+', ' ', (s or '').replace('\n', ' ')).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + '…'


def links(texto):
    """Links worth keeping in a message text (dedup, order kept, ignore list applied)."""
    out = []
    for u in LINK.findall(texto or ''):
        u = u.rstrip('.,;:!?')
        low = u.lower()
        host = urllib.parse.urlsplit(low).netloc
        if any(i in host or (('/' in i) and i in low) for i in IGNORAR): continue
        if u not in out: out.append(u)
    return out


def links_html(html):
    """Only the anchors of an HTML body (href), so signature images and tracking pixels (src) stay out."""
    return ' '.join(re.findall(r'href\s*=\s*["\']([^"\']+)', html or '', flags=re.I))


def guarda_links(quando, quem, onde, texto, contexto=None):
    """Index the links of one message. Returns the new ones (already-indexed links are skipped). `contexto` is the
    snippet quoted next to the link (default: the text itself)."""
    if not LIGADO: return []
    novos = []
    for u in links(texto):
        if _ja_tem(u): continue
        _linha(f'- {_dia(quando)} · {quem} · {onde} · Link: {u} — "{_limpa(contexto if contexto is not None else texto)}"')
        novos.append(u)
    return novos


def _nome_livre(nome):
    nome = re.sub(r'[\\/:*?"<>|\r\n]+', '_', nome).strip() or 'arquivo'
    base, ext = os.path.splitext(nome)
    p, i = os.path.join(pasta(), nome), 2
    while os.path.exists(p):
        p = os.path.join(pasta(), f'{base} ({i}){ext}'); i += 1
    return p


def guarda_arquivo(quando, quem, onde, nome, baixar, chave=None, tamanho=None):
    """Save one attached file. `baixar` is a callable returning the bytes (called only when needed); `chave` (the
    attachment url or id) avoids saving the same file twice. Returns the local path, a note when skipped, or None
    when it was already there. Never raises: a failed download becomes a note in the index."""
    if not LIGADO: return None
    chave = chave or f'{_dia(quando)}|{quem}|{nome}'
    if _ja_tem(chave): return None
    dia = _dia(quando)
    if tamanho and tamanho > MAX_BYTES:
        nota = f'{nome} não baixado ({tamanho // (1024 * 1024)} MB, acima de max_mb)'
        _linha(f'- {dia} · {quem} · {onde} · Arquivo: {nota} <!-- {chave} -->')
        return nota
    try:
        dados = baixar()
    except Exception as e:
        nota = f'{nome} não baixado ({type(e).__name__}: {str(e)[:80]})'
        _linha(f'- {dia} · {quem} · {onde} · Arquivo: {nota} <!-- {chave} -->')
        return nota
    p = _nome_livre(f'{dia} {nome}')
    with open(p, 'wb') as fh: fh.write(dados)
    _linha(f'- {dia} · {quem} · {onde} · Arquivo: `{os.path.basename(p)}` <!-- {chave} -->')
    return p


def guarda_chats(chats):
    """Links of the messages people sent in a chat source's output ([{'chat', 'msgs': [{'eu', 'bot'?, 'de', 'texto',
    'quando'}]}]); sets m['guardados'] with what was new. For sources that only carry text (Slack, Google Chat)."""
    for c in chats:
        for m in c.get('msgs') or []:
            if m.get('eu') or m.get('bot'): continue
            try: g = guarda_links(m['quando'], m['de'], c.get('chat') or '?', m.get('texto') or '')
            except Exception as e: g = [f'(material nao guardado: {type(e).__name__}: {str(e)[:80]})']
            if g: m['guardados'] = g
