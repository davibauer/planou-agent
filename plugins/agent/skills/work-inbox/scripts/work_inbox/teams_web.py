#!/usr/bin/env python3
"""Sessao do Teams WEB (refresh token do cliente web) — o que o conector do Power Automate nao alcanca.

Uso: imagens coladas nas mensagens (hostedContents -> objetos do ASM, que o runtime do conector devolve 404) e presenca.
As MENSAGENS continuam vindo pelo conector (teams_pull.py): esta sessao so' entra para midia/presenca (decisao de 21/09/2026).

Credencial (por perfil): JSON {tenant, client_id, brk_client_id, refresh_token} colado pelo usuario a partir da chamada
`oauth2/v2.0/token` (grant_type=refresh_token) no DevTools do teams.cloud.microsoft. Um arquivo so' por perfil — o refresh
token e' ROTACIONADO a cada uso, entao presenca e midia tem que ler/gravar o mesmo arquivo. Nunca imprimir tokens.

  teams_web.py --profile <perfil> image <hostedContentId ou url do graph> [saida.png]   (imagem = apelido antigo)
"""
import base64, fcntl, json, os, re, sys, tempfile, urllib.request, urllib.parse, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inbox_paths
from datetime import datetime, timezone, timedelta

# teams-desktop.json = login por device code no cliente Teams desktop (1fec8e78, FOCI): refresh token deslizante (~90 dias),
# diferente do cliente web (SPA), cujo refresh morre em 24 h fixas (AADSTS700084). Trocado em 23/09/2026.
BRT = timezone(timedelta(hours=-3))
_CACHE = {}


def cred_path(perfil):
    """Arquivo da credencial do Teams web do perfil: "teams_web_cred" no config.json do perfil (um caminho), senao
    teams-web.json na pasta do perfil."""
    d = os.path.join(inbox_paths.base(), 'profiles', perfil)
    try: p = json.load(open(f'{d}/config.json')).get('teams_web_cred')
    except (OSError, ValueError): p = None
    return os.path.expanduser(p) if p else f'{d}/teams-web.json'


def access_token(perfil, scope, client=None):
    """Access token para `scope` via refresh token do cliente web; rotaciona o refresh no arquivo (com lock: o tick da presenca
    e este script rotacionam o MESMO arquivo — sem lock um deles fica com o refresh velho e leva 400 invalid_grant, 21/09/2026).
    client: None = fluxo do cliente web via broker (ceb96695 + brk); 'broker' = direto no app Microsoft Teams (5e3ce6c0), que e'
    o appid que o ASM (imagens) aceita. Os dois aceitam o mesmo refresh (family token). Cache por (perfil, scope, client)."""
    k = (perfil, scope, client)
    if k in _CACHE and _CACHE[k][1] > datetime.now(timezone.utc): return _CACHE[k][0]
    p = cred_path(perfil)
    with open(p + '.lock', 'w') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        c = json.load(open(p))
        desktop = 'brk_client_id' not in c   # credencial do cliente desktop: um client_id so', sem broker
        if desktop: client = 'broker'       # pula os parametros do broker do cliente web; o appid do desktop serve p/ ASM e presenca
        cid = c['client_id'] if desktop else (c['brk_client_id'] if client == 'broker' else c['client_id'])
        d = {'client_id': cid, 'scope': scope + ' openid profile offline_access', 'grant_type': 'refresh_token', 'client_info': '1', 'refresh_token': c['refresh_token']}
        q = ''
        if client != 'broker':
            q = '?' + urllib.parse.urlencode({'client_id': cid, 'brk_client_id': c['brk_client_id'], 'brk_redirect_uri': 'https://teams.cloud.microsoft/v2/authv2'})
            d.update({'redirect_uri': 'brk-multihub://outlook.office.com', 'brk_client_id': c['brk_client_id'], 'brk_redirect_uri': 'https://teams.cloud.microsoft/v2/authv2'})
        hdr = {'content-type': 'application/x-www-form-urlencoded;charset=utf-8'}
        if not desktop: hdr.update({'Origin': 'https://teams.cloud.microsoft', 'Referer': 'https://teams.cloud.microsoft/'})
        req = urllib.request.Request(f'https://login.microsoftonline.com/{c["tenant"]}/oauth2/v2.0/token' + q, data=urllib.parse.urlencode(d).encode(), headers=hdr)
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'refresh do token do Teams web ({perfil}) falhou ({e.code}): {e.read()[:160].decode(errors="replace")} — refazer o login do Teams (device code, cliente desktop)')
        if r.get('refresh_token'):
            c['refresh_token'] = r['refresh_token']; c['renovado'] = datetime.now(BRT).isoformat()
            # unique temporary file (mkstemp creates it 0600): a fixed '<p>.tmp' breaks when two processes refresh at once
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=os.path.basename(p) + '.', suffix='.tmp')
            with os.fdopen(fd, 'w') as f: json.dump(c, f)
            os.replace(tmp, p)
    _CACHE[k] = (r['access_token'], datetime.now(timezone.utc) + timedelta(seconds=int(r.get('expires_in', 3000)) - 120))
    return r['access_token']


def token_asm(perfil):
    """O ASM (imagens coladas) aceita o Bearer do proprio app Microsoft Teams (appid 5e3ce6c0, aud ic3) — e' o cookie
    authtoken_asm do cliente web; o authz/skypetoken classico devolve 401 para este cliente."""
    return access_token(perfil, 'https://ic3.teams.office.com/.default', client='broker')


def url_objeto(hosted):
    """hostedContents id (base64 de 'id=…,type=1,url=https://…/objects/…/views/imgo') ou url do graph -> url do objeto no ASM."""
    m = re.search(r'/hostedContents/([^/]+)', hosted)
    hid = m.group(1) if m else hosted
    dec = base64.b64decode(hid + '=' * (-len(hid) % 4)).decode(errors='replace')
    m = re.search(r'url=(https?://\S+)', dec)
    if not m: raise ValueError(f'hostedContents sem url: {dec[:120]}')
    return m.group(1)


def baixar_imagem(perfil, hosted, destino=None):
    """Baixa a imagem colada (hostedContents) e devolve o caminho do arquivo."""
    url = url_objeto(hosted)
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token_asm(perfil)}', 'Origin': 'https://teams.cloud.microsoft', 'Referer': 'https://teams.cloud.microsoft/'})
    with urllib.request.urlopen(req, timeout=60) as r:
        dados = r.read(); ct = r.headers.get('Content-Type', '')
    ext = '.png' if 'png' in ct else '.jpg' if 'jpe' in ct else '.gif' if 'gif' in ct else '.bin'
    if not destino:
        os.makedirs(os.path.join(inbox_paths.base(), 'media'), exist_ok=True)
        destino = os.path.join(inbox_paths.base(), 'media', f'{re.sub(r"[^0-9a-zA-Z_-]", "_", url.rsplit("/objects/", 1)[-1].split("/")[0])}{ext}')
    open(destino, 'wb').write(dados)
    return destino


if __name__ == '__main__':
    a = sys.argv[1:]; perfil = inbox_paths.env_profile()
    if a[:1] == ['--profile']: perfil = a[1]; a = a[2:]
    if a and a[0] in ('image', 'imagem') and not perfil: sys.exit('teams_web.py: --profile <perfil> (ou WORK_INBOX_PROFILE)')
    if a and a[0] in ('image', 'imagem'): print(baixar_imagem(perfil, a[1], a[2] if len(a) > 2 else None))
    else: print(__doc__)
