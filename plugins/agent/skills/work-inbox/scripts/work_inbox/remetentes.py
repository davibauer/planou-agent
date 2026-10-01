"""Built-in automatic e-mail senders, decided by the address only (never the display name).

Shared by the gmail and outlook_email sources and by the work-inbox skill's outlook_pull (both in this plugin). This file
is the source; tools/sync_shared.py writes a byte-identical copy into the work-inbox skill, which runs on its own. Standard
library only: work-inbox imports it without the agent skill.

Two kinds of rule:
- TRECHOS: tokens no person's name contains (noreply, notification...), automatic anywhere in the address;
- short mailbox names that are also pieces of people's names (rh@ inside ...rh@, jira@ inside tajira@, hi@ inside
  sachi@, team@ inside steam@): automatic only as the first or last piece of the local part split on . _ + -.
"""
import re
from email.utils import parseaddr

# both sources; long tokens are safe anywhere in the address
TRECHOS = (r"no[-_.]?reply|do[-_.]?not[-_.]?reply|notification|mailer-daemon|newsletter|comunicacao|comunicado|"
           r"servicedesk|freshservice|azuredevops|atlassian|github\.com")
# the corporate inbox (Outlook): system and department mailboxes
CAIXAS_OUTLOOK = frozenset(('alert', 'alerts', 'marketing', 'rh', 'jira'))
AUTO_OUTLOOK = re.compile(TRECHOS + r"|@e\.", re.I)
# the personal inbox (Gmail): the Google Workspace noise and sales/support mailboxes on top
# chat-noreply = notificacao de mensagem do Chat (o agent le o Chat direto, entao aqui e' duplicata)
CAIXAS_GMAIL = CAIXAS_OUTLOOK | frozenset((
    'fale', 'contato', 'vendas', 'sales', 'hello', 'hi', 'team', 'suporte', 'support', 'billing'))
AUTO_GMAIL = re.compile(
    TRECHOS + r"|atendimento|invoice|calendar-notification@google|chat-noreply@google|drive-shares|docs\.google\.com|"
    r"accounts\.google|bitbucket|microsoft\.com|@(e|mail|email|info|news|updates?|notify|hello|mkt)\.", re.I)

_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(\.[\w-]+)+")


def endereco(de):
    """The e-mail address in a From value ('Ana <ana@x.com>', 'ana@x.com' or anything holding one), lower case; '' when
    there is none."""
    s = str(de or '')
    addr = parseaddr(s)[1]
    if '@' not in addr:
        m = _EMAIL.search(s)
        addr = m.group(0) if m else ''
    return addr.lower()


def pecas(addr):
    """Pieces of the local part split on . _ + - (empty pieces dropped)."""
    return [p for p in re.split(r'[._+-]', addr.rsplit('@', 1)[0]) if p]


def fixo(de, trechos, caixas):
    """Automatic by the address: `trechos` anywhere in it, or one of `caixas` as the first or last piece of the local part."""
    addr = endereco(de)
    if '@' not in addr: return False
    if trechos.search(addr): return True
    p = pecas(addr)
    return bool(p) and (p[0] in caixas or p[-1] in caixas)


def outlook(de):
    return fixo(de, AUTO_OUTLOOK, CAIXAS_OUTLOOK)


def gmail(de):
    return fixo(de, AUTO_GMAIL, CAIXAS_GMAIL)
