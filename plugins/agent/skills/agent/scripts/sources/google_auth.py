"""Helper (not an adapter): OAuth credential of one Google Workspace account for Gmail + Google Chat + Calendar.

One OAuth client ("Desktop app", consent screen Internal) for the three sources: one refresh token. Files:
  client_secret.json  the client downloaded from the Cloud console
  token.json          access/refresh token written by the flow (chmod 600)
Both live in the folder given by the source config "google_dir" (default secrets/google of the instance), resolved by
paths.google_dir: a config still pointing at the old agent folder (~/.config/<name>-vigia) uses secrets/google once
watch_core.migrate_agent_names copied the files there.

Test mode: when the token file is outside the instance folder and the instance is not live, an expired access token is
refreshed in memory only (the file is not rewritten; Google refresh tokens do not rotate).

  python3 -m adapters.google_auth <dir>           # interactive flow (prints the URL, waits for the localhost redirect)
"""
import os, sys

DIR = None
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/chat.spaces.readonly',
    'https://www.googleapis.com/auth/chat.messages.readonly',
    'https://www.googleapis.com/auth/chat.memberships.readonly',
    'https://www.googleapis.com/auth/calendar.readonly',
]
PORT = 8765
_c = None


def configura(d):
    global DIR
    if DIR and d and os.path.realpath(d) != os.path.realpath(DIR): raise ValueError(f'duas contas Google na mesma instancia: {DIR} e {d}')
    DIR = d or DIR


def _token_file(): return os.path.join(DIR, 'token.json')


def creds():
    global _c
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    import core, paths
    tok = _token_file()
    if _c is not None and _c.valid: return _c
    if not os.path.exists(tok):
        raise RuntimeError(f'sem token em {tok}: rodar o fluxo (adapters/google_auth.py)')
    c = Credentials.from_authorized_user_file(tok, SCOPES)
    if c.valid:
        _c = c; return c
    if c.expired and c.refresh_token:
        c.refresh(Request())
        if core.LIVE or paths.dentro(tok): _save(c)
        _c = c; return c
    raise RuntimeError('token invalido e sem refresh_token: rodar o fluxo (adapters/google_auth.py)')


def _save(c):
    from watch_core import fileio
    os.makedirs(DIR, exist_ok=True)
    fileio.write(_token_file(), c.to_json(), mode=0o600)


def flow():
    from google_auth_oauthlib.flow import InstalledAppFlow
    fl = InstalledAppFlow.from_client_secrets_file(os.path.join(DIR, 'client_secret.json'), SCOPES)
    print(f'Abra no navegador (a conta do Workspace); o redirect volta em http://localhost:{PORT}/', flush=True)
    c = fl.run_local_server(port=PORT, open_browser=False, access_type='offline', prompt='consent',
                            authorization_prompt_message='URL: {url}')
    _save(c); print('token gravado em', _token_file())
    return c


if __name__ == '__main__':
    configura(os.path.expanduser(sys.argv[1])); flow()
