#!/usr/bin/env python3
"""Direct links to the message that started a pending item (Slack, Teams, Google Chat, Outlook, Gmail).

The sources call `guarda(p, url, **ids)` right after core.enfileira() creates or updates a pending item: the item keeps
the raw ids of the message (p['msg']) and its link (p['link']). watch_core.tasks copies p['link'] into the task, and
planou_tick sends it as the task origin url, so the origin in Planou opens the exact message.

Nothing here writes a domain or a tenant: the Slack workspace URL comes from the profile (auth.test), the Google account
from the source config. A builder without the data it needs returns '' (no link) instead of guessing.
"""
import re
import urllib.parse


def slack(base, channel, ts, thread_ts=None):
    """https://<workspace>.slack.com/archives/<channel>/p<ts without the dot>[?thread_ts=<thread_ts>&cid=<channel>].
    `base` is the workspace URL from auth.test (e.g. 'https://exemplo.slack.com/')."""
    base = (base or '').strip().rstrip('/')
    if not (base.startswith('https://') and channel and ts): return ''
    url = f'{base}/archives/{channel}/p{str(ts).replace(".", "")}'
    if thread_ts and str(thread_ts) != str(ts):
        url += '?' + urllib.parse.urlencode({'thread_ts': thread_ts, 'cid': channel})
    return url


def slack_thread_ts(permalink):
    """thread_ts of a reply, read from the permalink Slack returns, or None."""
    m = re.search(r'[?&]thread_ts=(\d+\.\d+)', permalink or '')
    return m.group(1) if m else None


TEAMS_CHAT_CONTEXT = urllib.parse.quote('{"contextType":"chat"}', safe='')


def teams(chat_id, message_id, web_url=None):
    """Graph's webUrl when the source has it; otherwise the documented chat message deep link
    https://teams.microsoft.com/l/message/<chatId>/<messageId>?context={"contextType":"chat"}."""
    if web_url and str(web_url).startswith('https://'): return web_url
    if not (chat_id and message_id): return ''
    return (f'https://teams.microsoft.com/l/message/{urllib.parse.quote(str(chat_id), safe="")}/'
            f'{urllib.parse.quote(str(message_id), safe="")}?context={TEAMS_CHAT_CONTEXT}')


def outlook(message_id, web_link=None):
    """Graph's webLink when the source has it; otherwise the same OWA read link built from the message id."""
    if web_link and str(web_link).startswith('https://'): return web_link
    if not message_id: return ''
    return ('https://outlook.office365.com/owa/?ItemID=' + urllib.parse.quote(str(message_id), safe='')
            + '&exvsurl=1&viewmodel=ReadMessageItem')


def gmail(message_id, account=None):
    """The message in Gmail, in the account of the source (authuser) when the config names it."""
    if not message_id: return ''
    conta = f'?authuser={urllib.parse.quote(account, safe="@")}' if account else 'u/0/'
    return f'https://mail.google.com/mail/{conta}#all/{urllib.parse.quote(str(message_id), safe="")}'


def google_chat(message_name, space_type=None, account=None):
    """spaces/<space>/messages/<thread>.<message> -> https://chat.google.com/{room|dm}/<space>/<thread>/<message>."""
    m = re.fullmatch(r'spaces/([^/]+)/messages/([^/.]+)\.([^/]+)', message_name or '')
    if not m: return ''
    tipo = 'dm' if space_type in ('dm', 'DIRECT_MESSAGE') else 'room'
    conta = f'?authuser={urllib.parse.quote(account, safe="@")}' if account else ''
    return f'https://chat.google.com/{tipo}/{m.group(1)}/{m.group(2)}/{m.group(3)}{conta}'


def guarda(p, url, quando=None, **ids):
    """Records the message behind pending item p: p['msg'] (raw ids + when) and p['link'] (only when there is one; an
    old link is never replaced by nothing)."""
    msg = {k: v for k, v in ids.items() if v}
    if quando: msg['em'] = quando
    if msg: p['msg'] = msg
    if url: p['link'] = url
    return p
