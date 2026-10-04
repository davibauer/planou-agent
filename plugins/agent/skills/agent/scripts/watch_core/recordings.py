#!/usr/bin/env python3
"""Gravacoes do OBS x agenda — motor COMPARTILHADO dos agents (todas as instancias).

Uma copia so', como watch_core.daily e watch_core.lessons (pedido de 23/09/2026: "coisas comuns nao duplicadas, pra
melhoria refletir em todas"). Cada agent entra apenas com a PROPRIA AGENDA — uma funcao
`eventos(ini, fim) -> [{'id','titulo','ini','fim', ...}]` ja filtrada para reunioes — e chama:

    from watch_core import recordings as gc
    r = gc.tick(s, AGENT, eventos, enfileira, desde, dry)      # estado em s['gravacoes']
    gc.imprime_bloco(r)                                         # bloco == GRAVACOES (True se imprimiu)

O OBS grava na pasta de videos (recordings.videos_dir no config) como `YYYY-MM-DD HH-MM-SS.mp4` (hora local = BRT, inicio da gravacao).
Um video e' de uma reuniao quando cruza pelo menos SOBREPOSICAO_MIN do evento (REC apertado ate ANTES_MAX antes).
A pasta e' compartilhada por todos os clientes. Duas regras de dono:
  - dentro de uma agenda: dois eventos pegam o mesmo video -> fica o de maior interseccao;
  - entre agents: cada um registra a interseccao em DONOS (~/.config/watch-core/data/recording_owners.json), com o
    nome canonico do agent (os nomes antigos gravados antes — acme, acmecorp — contam como o mesmo agent);
    o agent com interseccao MENOR que a de outro larga o video em silencio (23/09/2026: a daily de um cliente
    09:46-10:00 foi tomada pela reuniao recorrente de outro cliente das 10:00 porque so aquele agent olhava gravacoes).

Video AINDA SENDO GRAVADO ou MOVIDO nunca e' aberto, transcrito nem arquivado (PLN0245): "estavel" = tamanho e mtime
iguais aos do tick anterior E mtime com mais de ESTAVEL_S (60 s; ATA_ESTAVEL_S ou recordings.stable_s), sem .part/.tmp
ao lado. Tudo o que o tick le nas pastas de video e de arquivo (que costumam morar num drive do Windows, /mnt/<letra>)
passa por watch_core.slowfs, com prazo e num processo filho: a pasta inteira sai de uma leitura so' de metadados
(lstat, sem abrir video), a ata.md de uma leitura curta, e o ffprobe roda uma vez por video estavel (a duracao fica no
estado). Prazo estourado vira slowfs.Stuck -> "FONTE QUEBRADA (gravacoes): drive X sem resposta (...)", sem travar o
tick. O .mp4 vai para a pasta de arquivo num processo destacado (archive_job.py), nunca dentro do tick. Quem transcreve e' o run.sh da skill meeting-minutes (do mesmo plugin; recordings.minutes_dir no config sobrepoe) (whisperx, flock proprio da GPU). A ata (ata.md) e'
escrita pelo modelo na sessao; este modulo so le a tabela de acoes dela (acoes do usuario viram pendencia 'reuniao').

Primeira rodada de um agent (sem s['gravacoes']): planta a base em silencio — nao reporta NAO GRAVADA de reunioes
antigas nem cobra acoes de atas antigas; transcricao pendente e ata pendente seguem normalmente.
"""
import os, re, sys, json, argparse, subprocess

from . import config, fileio, slowfs
from .agents import canonical_agent
from datetime import datetime, timezone, timedelta

VIDEOS = os.path.expanduser(os.environ.get('ATA_VIDEOS_DIR') or config.get('recordings.videos_dir') or '~/Videos')
ARQUIVO = os.path.expanduser(os.environ.get('ATA_ARQUIVO_DIR') or config.get('recordings.archive_dir') or VIDEOS)   # para onde o .mp4 vai depois da ata (pedido de 21/09); a pasta ata_<stem>/ fica em VIDEOS
MM_HOME = os.path.expanduser(os.environ.get('MEETING_MINUTES_HOME') or '~/.config/meeting-minutes')   # config e estado do meeting-minutes


LANGUAGE = ''      # the instance's behavior_config.meeting-recordings.language: run.sh gets it as ATA_LANG (empty = whisper detects)


def configure(videos_dir=None, archive_dir=None, language=None):
    """The instance's folders win (work-watch config.json "videos_dir"/"archive_dir"), over $ATA_VIDEOS_DIR and the shared
    recordings.* of ~/.config/watch-core. Without an archive folder anywhere the .mp4 stays in the videos folder."""
    global VIDEOS, ARQUIVO, LANGUAGE
    if isinstance(language, str): LANGUAGE = language.strip()
    if videos_dir: VIDEOS = os.path.expanduser(videos_dir)
    ARQUIVO = os.path.expanduser(archive_dir or os.environ.get('ATA_ARQUIVO_DIR') or config.get('recordings.archive_dir') or VIDEOS)


def _minutes_dir():
    """Scripts do meeting-minutes: recordings.minutes_dir do config; senao a skill meeting-minutes do mesmo plugin
    (plugin agent: skills/meeting-minutes/scripts/meeting_minutes, irma da skill agent onde mora este pacote); senao a
    skill instalada em ~/.claude/skills."""
    c = config.get('recordings.minutes_dir')
    if c: return os.path.expanduser(c)
    skills = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    irma = os.path.join(skills, 'meeting-minutes', 'scripts', 'meeting_minutes')
    return irma if os.path.isdir(irma) else os.path.expanduser('~/.claude/skills/meeting-minutes/scripts/meeting_minutes')


ATA_SKILL = _minutes_dir()
RUN_SH = f'{ATA_SKILL}/run.sh'
BRT = timezone(timedelta(hours=-3))
STEM_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}) (\d{2})-(\d{2})-(\d{2})(?: - .+)?$')   # OBS puro, ou ja com ' - <reuniao>' no fim
TS_LEN = 19                            # 'YYYY-MM-DD HH-MM-SS'
SOBREPOSICAO_MIN = timedelta(minutes=5)  # o video tem que cobrir ao menos isto do evento (ou metade, se o evento for curto)
ANTES_MAX = timedelta(minutes=20)     # REC apertado ate 20 min antes do inicio do evento ainda e' a reuniao
NAO_GRAVADA_APOS = timedelta(minutes=30)   # evento terminado ha mais que isso sem video = "nao gravada"
MIN_DUR = 60                          # segundos; menos que isso nao e' reuniao (teste de OBS)
ESTAVEL_S = float(os.environ.get('ATA_ESTAVEL_S') or config.get('recordings.stable_s') or 60)   # mtime mais novo = ainda gravando/movendo
FFPROBE = os.environ.get('ATA_FFPROBE') or 'ffprobe'
EM_USO = ('.part', '.tmp', '.crdownload', '.partial')      # no nome ou ao lado: copia/gravacao em andamento
ALIASES = tuple(a.lower() for a in config.get('recordings.user_aliases') or ())   # fallback; user_aliases do config do meeting-minutes vale antes
BLOCOS = ('busy', 'ocupado', 'out of office', 'fora do escritorio', 'fora do escritório')   # titulo de bloco de agenda, nao reuniao


def reuniao(e):
    """Evento que e' reuniao de verdade (formato normalizado das agendas de Outlook e de Google): fora os blocos
    seus de OOF/livre, os blocos 'busy' (compromisso de outro cliente), dia inteiro, cancelado e recusado."""
    if e.get('dia_inteiro') or e.get('status') == 'cancelled' or (e.get('titulo') or '').startswith(('Cancelado:', 'Canceled:', 'Cancelled:')): return False
    if e.get('org_eu') and (e.get('showAs') in ('oof', 'free') or (e.get('titulo') or '').strip().lower() in BLOCOS): return False
    if (e.get('titulo') or '').strip().lower() in BLOCOS: return False
    return e.get('resposta') != 'declined' and e.get('minha_resposta') != 'declined'


def _cfg_aliases():
    try:
        c = json.load(open(os.path.join(MM_HOME, 'config.json')))
        return tuple(a.lower() for a in c.get('user_aliases', [])) or ALIASES
    except Exception:
        return ALIASES


def stem_dt(stem):
    m = STEM_RE.match(stem)
    if not m: return None
    y, mo, d = (int(x) for x in m.group(1).split('-'))
    return datetime(y, mo, d, int(m.group(2)), int(m.group(3)), int(m.group(4)), tzinfo=BRT)


def duracao(path):
    """Segundos pelo ffprobe; None = sem moov atom (arquivo morto) ou sem ffprobe. Roda com prazo e sem ninguem esperar
    por ele (slowfs.run): um ffprobe preso num drive sem resposta vira slowfs.Stuck, nunca trava o tick. So' e' chamado
    para video estavel, uma vez por video (a duracao fica em s['gravacoes']['duracoes'])."""
    try:
        rc, out = slowfs.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nk=1:nw=1', path], path)
        return float(out.strip()) if rc == 0 and out.strip() else None
    except (OSError, ValueError):
        return None


_ARQ = {}          # pasta de arquivo na ultima leitura de videos(): existe e nomes


def _em_uso(f, nomes):
    """Arquivo com .part/.tmp no nome ou ao lado: alguem ainda esta gravando ou copiando."""
    return any(x in f for x in EM_USO) or any(f + x in nomes for x in EM_USO)


def _mesmo(antes, tam, mtime):
    return isinstance(antes, (list, tuple)) and len(antes) == 2 and antes[0] == tam and antes[1] == int(mtime)


def videos(desde, antes=None, duracoes=None, agora=None):
    """Gravacoes com inicio >= desde: [{stem, path, ini, tam, mtime, dur, estavel, workdir, transcrito, ata, ...}].
    So' metadados (slowfs.scan: lstat da pasta num processo filho com prazo); nenhum video e' aberto, a nao ser o ffprobe
    de um video estavel ainda sem pasta ata_ nem duracao em `duracoes` (o candidato a transcricao). Os outros (ja
    transcritos ou arquivados) ficam com a duracao estimada pelo mtime.
    antes = {stem: [tam, mtime]} do tick anterior (None = sem leitura anterior: basta o mtime velho); estavel = mesmo
    tamanho e mtime que antes, mtime com mais de ESTAVEL_S e nada .part/.tmp ao lado. Enquanto nao estavel:
    mtime recente -> dur None ('gravando'); mtime velho -> dur estimada pelo mtime ('finalizando')."""
    duracoes = {} if duracoes is None else duracoes
    agora = (agora or datetime.now(timezone.utc)).timestamp()
    snap = slowfs.scan(VIDEOS, into='ata_')
    if snap is None: raise FileNotFoundError(2, 'pasta de videos nao existe', VIDEOS)
    nomes = sorted(snap)
    wds = {f[4:4 + TS_LEN]: f for f in nomes if f.startswith('ata_') and snap[f]['d'] and stem_dt(f[4:])}   # ts -> pasta ata_ (com ou sem ' - reuniao')

    def item(pasta, f, e, lista, ts, wd_nome, arquivado):
        stem = f[:-4]; ini = stem_dt(stem); path = f'{pasta}/{f}'
        tam, mtime = e['s'], e['m']
        arqs = set(snap[wd_nome].get('f') or ()) if wd_nome in snap else None
        recente = agora - mtime < ESTAVEL_S or _em_uso(f, lista)
        estavel = not recente and (antes is None or _mesmo(antes.get(stem), tam, mtime))
        if recente: dur = None
        elif estavel and not arquivado and arqs is None:          # candidato a transcricao: o ffprobe confere o video, uma vez
            dur = duracoes.get(ts)
            if dur is None:
                dur = duracao(path)
                if dur is not None: duracoes[ts] = dur
        else: dur = duracoes.get(ts) or max(0.0, mtime - ini.timestamp())   # ja transcrito ou arquivado: nao abre o video
        return {'stem': stem, 'ts': ts, 'path': path, 'ini': ini, 'tam': tam, 'mtime': mtime, 'dur': dur, 'estavel': estavel,
                'workdir': f'{VIDEOS}/{wd_nome}', 'arquivos_wd': arqs, 'transcrito': bool(arqs and 'transcricao.md' in arqs),
                'ata': bool(arqs and 'ata.md' in arqs), 'arquivado': arquivado}

    out, vistos = [], set()
    for f in nomes:
        if not f.endswith('.mp4') or snap[f]['d']: continue
        stem = f[:-4]; ini = stem_dt(stem)
        if not ini or ini < desde: continue
        ts = stem[:TS_LEN]; vistos.add(ts)
        out.append(item(VIDEOS, f, snap[f], snap, ts, wds.get(ts) or f'ata_{stem}', False))
    asnap = snap if os.path.abspath(ARQUIVO) == os.path.abspath(VIDEOS) else slowfs.scan(ARQUIVO)
    arq = {f[:TS_LEN]: f for f, e in (asnap or {}).items() if f.endswith('.mp4') and not e['d'] and stem_dt(f[:-4])}
    _ARQ.clear(); _ARQ.update(existe=asnap is not None, nomes=frozenset(asnap or ()))   # para arquiva_video, sem ler de novo
    for ts, wd in wds.items():                          # ata_<ts>/ cujo .mp4 ja foi para o ARQUIVO: continua contando como gravacao
        if ts in vistos or ts not in arq: continue
        ini = stem_dt(ts)
        if not ini or ini < desde: continue
        out.append(item(ARQUIVO, arq[ts], asnap[arq[ts]], asnap, ts, wd, True))
    return out


def nome_arquivo(titulo):
    """Titulo da reuniao como sufixo de nome de arquivo (sem caracteres invalidos no Windows, ate 60 chars)."""
    t = re.sub(r'[\\/:*?"<>|]+', ' ', titulo or '').strip()
    t = re.sub(r'\s+', ' ', t)
    return t[:60].rstrip(' .')


def pipeline_terminou(v):
    """run.sh acabou (voiceprints.npz e' o ultimo passo) e nenhum run.sh esta com o video aberto."""
    arqs = v.get('arquivos_wd')
    if not (('voiceprints.npz' in arqs) if arqs is not None else os.path.exists(f'{v["workdir"]}/voiceprints.npz')): return False
    r = subprocess.run(['pgrep', '-f', f'run.sh .*{re.escape(v["ts"])}'], capture_output=True, text=True)
    return r.returncode != 0


def _arquivo_job(ts):
    """Arquivos locais do job de arquivamento de um video: (trava, erro). Locais (~/.cache), nunca no drive."""
    d = os.path.join(os.path.dirname(slowfs.cache_dir()), 'recordings-archive')
    k = re.sub(r'[^A-Za-z0-9_.-]', '_', ts)
    return os.path.join(d, k + '.lock'), os.path.join(d, k + '.err')


def arquivando(ts):
    """Um job de arquivamento deste video esta vivo (segura a trava local)?"""
    import fcntl
    lock = _arquivo_job(ts)[0]
    if not os.path.exists(lock): return False
    try:
        with open(lock) as f:
            try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError: return True
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def erro_arquivar(ts):
    """Erro deixado pelo ultimo job deste video (lido uma vez e apagado), ou None."""
    err = _arquivo_job(ts)[1]
    try:
        with open(err) as f: msg = f.read().strip()
    except OSError:
        return None
    try: os.unlink(err)
    except OSError: pass
    return msg or None


def arquiva_video(v, titulo=None, arquivo_existe=True, em_andamento=()):
    """Depois da ata: renomeia com ' - <reuniao>' no fim (pedido de 21/09) e move o .mp4 de VIDEOS para ARQUIVO (o .mp4 e'
    pesado; a pasta ata_ fica, renomeada igual). So depois da ata porque voiceprint.py identify e pronuncia.py leem o
    video; video.txt no workdir aponta o novo caminho.
    PLN0245: so' com o video estavel, e a copia (que le o video inteiro) roda num processo destacado (archive_job.py) que
    ninguem espera; o proximo tick ve o .mp4 ja no ARQUIVO. Devolve o destino quando lancou o job, senao None.
    em_andamento = nomes do ARQUIVO (um <destino>.part la e' outra copia em andamento)."""
    if v['arquivado'] or not arquivo_existe or not v.get('estavel', True) or not (v['ata'] and pipeline_terminou(v)): return None
    novo = v['stem'] if len(v['stem']) > TS_LEN or not nome_arquivo(titulo) else f'{v["ts"]} - {nome_arquivo(titulo)}'
    novo_wd = v['workdir'] if os.path.basename(v['workdir']) == f'ata_{novo}' else f'{VIDEOS}/ata_{novo}'
    dst = f'{ARQUIVO}/{novo}.mp4'
    if f'{novo}.mp4.part' in em_andamento or arquivando(v['ts']): return None
    lock, err = _arquivo_job(v['ts'])
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    job = {'ts': v['ts'], 'src': v['path'], 'dst': dst, 'tam': v['tam'], 'workdir': v['workdir'], 'novo_workdir': novo_wd,
           'lock': lock, 'err': err}
    subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'archive_job.py'), json.dumps(job)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    return dst


GENERICO = re.compile(r'^reuniao \w+ \d{2}:\d{2}$')   # titulo default da agenda sem nome (freeBusy de uma agenda: "Reuniao Cliente 13:30")
VAZIAS = {'reuniao', 'meeting', 'call', 'sync', 'com', 'para', 'sobre', 'the', 'and', 'with'}


def _palavras(t):
    import unicodedata
    t = unicodedata.normalize('NFKD', (t or '').lower()).encode('ascii', 'ignore').decode()
    return {w for w in re.split(r'[^a-z0-9]+', t) if len(w) >= 4 and w not in VAZIAS}


def mesmo_assunto(sufixo, titulo):
    """O nome dado ao video depois da ata (livre: 'Daily Cliente') combina com o titulo do evento
    ('Daily - eRegulatorio [Portal Cliente]')? Basta uma palavra significativa em comum; titulo generico nao filtra."""
    tl = ' '.join(sorted(_palavras(titulo)))
    if not titulo or GENERICO.match(re.sub(r'\s+', ' ', (titulo or '').strip().lower().replace('ã', 'a'))): return True
    return bool(_palavras(sufixo) & _palavras(titulo)) or not tl


def casa(ev, v):
    """O video cruza o evento? Sem duracao (gravando), assume que segue ate agora."""
    ini = datetime.fromisoformat(ev['ini']); fim = datetime.fromisoformat(ev['fim'])
    v_ini = v['ini']; v_fim = v_ini + timedelta(seconds=v['dur']) if v['dur'] else datetime.now(timezone.utc)
    # precisa cruzar pelo menos SOBREPOSICAO_MIN do evento: 23/09/2026 a daily de um cliente (09:46-10:00:29)
    # casou com a reuniao recorrente de outro cliente (10:00) por 29 s de sobra no fim do video
    # video ja batizado com o nome de OUTRA reuniao (a ata saiu por outro agent/agenda) nao e' deste evento:
    # 23/09/2026 a ata de uma call (10:29) casava com a reuniao recorrente de outro cliente (10:00-11:00)
    if len(v['stem']) > TS_LEN and not mesmo_assunto(v['stem'][TS_LEN + 3:], ev.get('titulo')): return False
    return v_ini >= ini - ANTES_MAX and v_ini <= fim and v_fim >= ini + min(SOBREPOSICAO_MIN, (fim - ini) / 2)


def acoes_da_ata(workdir, aliases=None):
    """Linhas da tabela de acoes (| # | acao | responsavel | prazo |) de ata.md; 'minha' quando o responsavel e' o usuario."""
    aliases = aliases or _cfg_aliases()
    try: linhas = slowfs.read_text(f'{workdir}/ata.md').splitlines()        # com prazo: a pasta ata_ mora no drive dos videos
    except FileNotFoundError: return []
    out, em_tabela = [], False
    for ln in linhas:
        # `**negrito**` no meio da celula tambem sai: a pendencia e' texto puro, nao markdown
        cels = [re.sub(r'\*\*|__', '', c).strip().strip('*').strip() for c in ln.strip().strip('|').split('|')] if ln.strip().startswith('|') else None
        if cels and cels[0] in ('#', 'Nº', 'N°') and len(cels) >= 3: em_tabela = True; continue
        if not cels: em_tabela = False; continue
        if not em_tabela or set(''.join(cels)) <= set('-: '): continue
        n, acao, resp = cels[0], cels[1], (cels[2] if len(cels) > 2 else '')
        prazo = cels[3] if len(cels) > 3 else ''
        rl = resp.lower()
        minha = any(a in rl for a in aliases)
        out.append({'n': n, 'acao': acao, 'resp': resp, 'prazo': prazo, 'minha': minha})
    return out


# ---------------- prazo da acao: pendencia com lembrete, aguardando ou proximas (decisao d-35, 28/09/2026) ----------------
# Uma acao sua da ata virava sempre pendencia com lembrete, e cobrava cedo demais quando o prazo era condicional
# ("quando o material chegar") ou distante ("em novembro"). Agora:
#   condicional / depende de terceiro / a definir -> acao no balde 'aguardando' (quem = pessoa citada, senao 'a definir');
#   data ou mes a mais de PRAZO_DISTANTE_DIAS da reuniao -> acao no balde 'proximas' (com a data quando der);
#   o resto (prazo curto e claro, ou sem prazo) -> pendencia 'reuniao' com lembrete, como antes.
# Heuristica deterministica: para estender, acrescente um padrao nas tuplas abaixo (pt e en).
PRAZO_DISTANTE_DIAS = 14
# condicao em cima de outra coisa/pessoa; vale na celula de prazo e no texto da acao
PRAZO_CONDICIONAL = (
    r'\bquando (?:o|a|os|as|ele|ela|eles|elas|\w+) [\w\s]{0,40}?(?:chegar|chegarem|vier|vierem|mandar|mandarem|enviar|enviarem|'
    r'liberar|liberarem|aprovar|aprovarem|responder|responderem|confirmar|confirmarem|definir|definirem|estiver|estiverem|ficar|ficarem)\b',
    r'\bdepois que\b', r'\bassim que\b', r'\bap[oó]s (?:o|a|os|as) \w+ (?:mandar|enviar|chegar|liberar|aprovar|responder|confirmar|definir)\b',
    r'\baguardando\b', r'\bdepende(?:ndo)? d[eoa]s?\b', r'\bcondicionad[oa]\b', r'\bconforme retorno\b', r'\bap[oó]s retorno\b',
    r'\bonce\b', r'\bas soon as\b', r'\bwhen (?:the |he |she |they |\w+ )[\w\s]{0,40}?(?:arrives?|sends?|sent|delivers?|approves?|'
    r'confirms?|replies|responds?|is ready|are ready|gets back)\b', r'\bafter (?:\w+ )?(?:sends?|delivers?|approves?|confirms?|replies)\b',
    r'\bwaiting (?:for|on)\b', r'\bdepends? on\b', r'\bpending (?:on|from)\b', r'\bupon\b',
)
# prazo em aberto: so na celula de prazo (no texto da acao "definir" e' o proprio trabalho)
PRAZO_INDEFINIDO = (r'^\s*(?:a|à) (?:definir|combinar|confirmar)\b', r'\bsem (?:prazo|data)\b', r'^\s*(?:tbd|tba|tbc)\b',
                    r'\bto be (?:defined|decided|confirmed|agreed)\b', r'\bno (?:deadline|date)\b', r'^\s*\?+\s*$')
# quem: o nome logo depois do conector ("depois que Fulano mandar", "aguardando Fulano", "once Jane sends")
PRAZO_QUEM = (
    # conector sem caixa, nome com inicial maiuscula (sem re.I: "depois que o material chegar" nao tem nome)
    r'(?i:depois que|assim que|quando|ap[oó]s|aguardando|depende(?:ndo)? d[eoa]|once|as soon as|when|after|waiting (?:for|on)|'
    r'depends? on|pending (?:on|from))\s+(?i:o |a |do |da |the )?'
    r'([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\w\-]+(?: (?:de |da |do )?[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\w\-]+)?)',
)
MESES = {'jan': 1, 'janeiro': 1, 'january': 1, 'fev': 2, 'fevereiro': 2, 'feb': 2, 'february': 2, 'mar': 3, 'marco': 3, 'março': 3,
         'march': 3, 'abr': 4, 'abril': 4, 'apr': 4, 'april': 4, 'mai': 5, 'maio': 5, 'may': 5, 'jun': 6, 'junho': 6, 'june': 6,
         'jul': 7, 'julho': 7, 'july': 7, 'ago': 8, 'agosto': 8, 'aug': 8, 'august': 8, 'set': 9, 'setembro': 9, 'sep': 9, 'sept': 9,
         'september': 9, 'out': 10, 'outubro': 10, 'oct': 10, 'october': 10, 'nov': 11, 'novembro': 11, 'november': 11,
         'dez': 12, 'dezembro': 12, 'dec': 12, 'december': 12}
MESES_AMBIGUOS = {k for k in MESES if len(k) <= 3} | {'sept', 'marco'}     # abreviacoes e palavras comuns ("marco", "may", "set")
_UNIDADE = {'dia': 1, 'dias': 1, 'day': 1, 'days': 1, 'semana': 7, 'semanas': 7, 'week': 7, 'weeks': 7,
            'mes': 30, 'mês': 30, 'meses': 30, 'month': 30, 'months': 30}


def _data_do_prazo(prazo, ref):
    """Primeira data que o texto do prazo cita, relativa a `ref` (date da reuniao): (date, rotulo) ou None.
    dd/mm[/aaaa], aaaa-mm-dd, "em N dias|semanas|meses" / "in N days|weeks|months" e mes por extenso ("novembro/2026",
    "em novembro", "Nov 2026"). Mes sem dia conta do dia 1 (o prazo cai dentro do mes; se o dia 1 ja esta longe, o mes
    inteiro esta). Sem ano: a proxima ocorrencia a partir da reuniao."""
    from datetime import date
    t = prazo.lower()
    m = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', t)
    if m:
        try: d = date(int(m[1]), int(m[2]), int(m[3])); return d, d.strftime('%d/%m/%Y')
        except ValueError: pass
    m = re.search(r'\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b', t)
    if m:
        a = int(m[3]) if m[3] else ref.year
        if a < 100: a += 2000
        try:
            d = date(a, int(m[2]), int(m[1]))
            if not m[3] and d < ref - timedelta(days=60): d = d.replace(year=d.year + 1)     # "10/01" dito em novembro
            return d, d.strftime('%d/%m/%Y')
        except ValueError: pass
    m = re.search(r'\b(?:em|in|within|dentro de)\s+(\d{1,3})\s+(dias?|semanas?|m[eê]s(?:es)?|days?|weeks?|months?)\b', t)
    if m:
        d = ref + timedelta(days=int(m[1]) * _UNIDADE.get(m[2], 1)); return d, d.strftime('%d/%m/%Y')
    # mes por extenso vale sozinho; abreviado ("mar", "set", "out", "may" tambem sao palavras) so' com dia, ano ou preposicao
    cheios = '|'.join(sorted((k for k in MESES if k not in MESES_AMBIGUOS), key=len, reverse=True))
    curtos = '|'.join(sorted(MESES_AMBIGUOS, key=len, reverse=True))
    ano = r'(?:\s*(?:/|de|of|,)?\s*(?P<a>\d{4}))'
    todos = cheios + '|' + curtos
    sem_palavra = '|'.join(sorted((k for k in MESES if k not in ('marco', 'mar', 'may', 'set', 'out', 'dez', 'ago')), key=len, reverse=True))
    m = (re.search(r'\b(?P<d>\d{1,2})\s*(?:de\s+)?(?P<m>' + todos + r')\b\.?' + ano + '?', t)          # 20 de novembro, 20 Nov
         or re.search(r'\b(?P<m>' + sem_palavra + r')\.?\s+(?P<d>\d{1,2})(?!\d)(?:st|nd|rd|th)?\b(?:,?\s*(?P<a>\d{4}))?', t)   # Nov 20, 2026
         or re.search(r'\b(?P<m>' + cheios + r')\b' + ano + '?', t)                                     # novembro, novembro/2026
         or re.search(r'\b(?P<m>' + curtos + r')\b\.?' + ano, t)                                        # nov/2026
         or re.search(r'\b(?:em|in|at[eé]|by|until|durante|during)\s+(?P<m>' + curtos + r')\b', t))   # em nov
    if m:
        g = m.groupdict(); mes = MESES[g['m']]; a = int(g['a']) if g.get('a') else ref.year
        try: d = date(a, mes, int(g['d']) if g.get('d') else 1)
        except ValueError: d = date(a, mes, 1)
        if not g.get('a') and d < ref and (d.year, d.month) != (ref.year, ref.month): d = d.replace(year=d.year + 1)
        return d, d.strftime('%d/%m/%Y' if g.get('d') else '%m/%Y')
    return None


def classifica_prazo(prazo, acao='', ref=None):
    """Para onde vai uma acao sua da ata: ('pendencia', None) | ('aguardando', quem) | ('proximas', data ou None).
    `ref` = date da reuniao (a distancia conta dela, nao do tick: ata escrita dias depois classifica igual)."""
    prazo, acao = (prazo or '').strip(), (acao or '').strip()
    ref = ref or datetime.now(BRT).date()
    cond = next((m for txt in (prazo, acao) for p in PRAZO_CONDICIONAL for m in [re.search(p, txt, re.I)] if m), None)
    indef = any(re.search(p, prazo, re.I) for p in PRAZO_INDEFINIDO)
    if cond or indef:
        quem = next((m.group(1) for txt in (prazo, acao) for p in PRAZO_QUEM for m in [re.search(p, txt)] if m), None)
        return 'aguardando', quem or 'a definir'
    d = _data_do_prazo(prazo, ref) if prazo else None
    if d and (d[0] - ref).days > PRAZO_DISTANTE_DIAS: return 'proximas', d[1]
    return 'pendencia', None


def lanca_transcricao(path):
    """run.sh em background (nohup); o proprio run.sh faz flock da GPU. Devolve o pid."""
    os.makedirs(os.path.join(MM_HOME, 'state'), exist_ok=True)
    log = open(os.path.join(MM_HOME, 'state', 'agent-run.log'), 'a')
    env = dict(os.environ)
    if LANGUAGE: env['ATA_LANG'] = LANGUAGE   # the instance's language wins over the shared meeting-minutes config.json
    p = subprocess.Popen(['nohup', 'bash', RUN_SH, path], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=ATA_SKILL, env=env)
    return p.pid


def gpu_ocupada():
    lock = os.environ.get('ATA_GPU_LOCK', '/tmp/ata-reuniao-whisperx.lock')
    if not os.path.exists(lock): return False
    r = subprocess.run(['flock', '-n', lock, 'true'], capture_output=True)
    return r.returncode != 0


def situacao(eventos, vids, agora=None, tamanhos_antes=None):
    """Por evento (que ja comecou): {'ev', 'video', 'estado', 'acoes'}; estado em
    nao_gravada | gravando | finalizando | aguardando_gpu | transcrevendo | ata_pendente | ata_pronta | sem_video_ainda."""
    agora = agora or datetime.now(timezone.utc); tamanhos_antes = tamanhos_antes or {}
    out = []
    for ev in eventos:
        ini = datetime.fromisoformat(ev['ini']); fim = datetime.fromisoformat(ev['fim'])
        if ini > agora: continue
        cands = [v for v in vids if casa(ev, v)]
        if not cands:
            est = 'nao_gravada' if agora - fim > NAO_GRAVADA_APOS else 'sem_video_ainda'
            out.append({'ev': ev, 'video': None, 'estado': est, 'acoes': []}); continue
        v = max(cands, key=lambda v: (v['dur'] or 0, v['tam']))
        if v['ata']: est, ac = 'ata_pronta', acoes_da_ata(v['workdir'])
        elif v['transcrito']: est, ac = 'ata_pendente', []
        elif v['dur'] is None: est, ac = 'gravando', []
        elif not (v['estavel'] if 'estavel' in v else tamanhos_antes.get(v['stem']) == v['tam']): est, ac = 'finalizando', []   # espera tamanho e mtime pararem
        elif v['dur'] < MIN_DUR: est, ac = 'nao_gravada', []
        elif (v['arquivos_wd'] is not None) if 'arquivos_wd' in v else os.path.exists(v['workdir']): est, ac = 'transcrevendo', []   # run.sh criou a pasta e ainda nao terminou
        else: est, ac = ('aguardando_gpu' if gpu_ocupada() else 'pronta_para_transcrever'), []
        out.append({'ev': ev, 'video': v, 'estado': est, 'acoes': ac})
    return _dono_local(out)


def _hora(iso): return datetime.fromisoformat(iso).astimezone(BRT).strftime('%a %d/%m %H:%M')


def imprime(sit):
    for s in sit:
        ev, v = s['ev'], s['video']
        base = f'{ev["titulo"]} · {_hora(ev["ini"])}–{datetime.fromisoformat(ev["fim"]).astimezone(BRT).strftime("%H:%M")}'
        if not v: print(f'-- {base} · {s["estado"].replace("_", " ")}'); continue
        dur = f'{int(v["dur"] // 60)} min' if v['dur'] else 'gravando'
        print(f'-- {base} · {v["stem"]}.mp4 ({dur}, {v["tam"] / 1e6:.0f} MB) · {s["estado"].replace("_", " ")}')
        for a in s['acoes']:
            print(f'   {"★" if a["minha"] else " "} #{a["n"]} {a["acao"]} · {a["resp"]}' + (f' · prazo {a["prazo"]}' if a['prazo'] else ''))


# ---------------- dono do video ----------------
DONOS = config.file(config.RECORDING_OWNERS)
GRAVACOES_JANELA = timedelta(hours=48)   # ate onde a agenda e' cruzada com a pasta de videos


def sobrepoe(ev, v):
    """Segundos de interseccao entre o evento e o video (sem duracao = ate agora)."""
    ini = datetime.fromisoformat(ev['ini']); fim = datetime.fromisoformat(ev['fim'])
    v_ini = v['ini']; v_fim = v_ini + timedelta(seconds=v['dur']) if v['dur'] else datetime.now(timezone.utc)
    return max(0.0, (min(fim, v_fim) - max(ini, v_ini)).total_seconds())


def _dono_local(sit):
    """Dois eventos da mesma agenda com o mesmo video (freeBusy 08:45-10:00 engole a daily das 09:00): fica o de maior interseccao."""
    dono = {}
    for x in sit:
        v = x['video']
        if not v: continue
        k = v['ts']
        if k not in dono or sobrepoe(x['ev'], v) > sobrepoe(dono[k]['ev'], v): dono[k] = x
    return [x for x in sit if not x['video'] or dono.get(x['video']['ts']) is x]


def _dono_canonico(e):
    """Entrada de um video em DONOS com os nomes antigos de agent dobrados no canonico (fica a maior interseccao)."""
    out = {}
    for k, v in e.items():
        if k == '_forcado': out[k] = canonical_agent(v); continue
        if k.startswith('_'): out[k] = v; continue
        c = canonical_agent(k)
        out[c] = max(out.get(c, v), v) if isinstance(v, (int, float)) else v
    return out


def reivindica(agent, pares, dry=False):
    """pares = [(ts, segundos)]. Registra a interseccao deste agent e devolve os ts em que OUTRO agent tem interseccao
    maior (este deve largar). Arquivo com flock: os agents rodam em sessoes paralelas. Grava so' o nome canonico; uma
    entrada gravada com o nome antigo do mesmo agent conta como dele."""
    import fcntl
    agent = canonical_agent(agent)
    os.makedirs(os.path.dirname(DONOS), exist_ok=True)
    with open(DONOS + '.lock', 'w') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try: d = json.load(open(DONOS))
        except (FileNotFoundError, ValueError): d = {}
        perde = set()
        for ts, seg in pares:
            e = d[ts] = _dono_canonico(d.get(ts) or {})
            f = e.get('_forcado')                  # dono na mao (--dono) vence qualquer interseccao, e o tick nao sobrescreve
            if f:
                if f != agent: perde.add(ts)
                continue
            if not dry: e[agent] = round(seg)
            if any(o != agent and not o.startswith('_') and s > seg for o, s in e.items()): perde.add(ts)
        corte = (datetime.now(BRT) - timedelta(days=7)).strftime('%Y-%m-%d')
        d = {k: v for k, v in d.items() if k[:10] >= corte}
        if not dry:
            fileio.write_json(DONOS, d, indent=1)
    return perde


def forca_dono(ts, agent):
    """Dono na mao (a transcricao mostrou que o video e' de outra reuniao): este agent fica com o video e os outros largam."""
    import fcntl
    os.makedirs(os.path.dirname(DONOS), exist_ok=True)
    with open(DONOS + '.lock', 'w') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try: d = json.load(open(DONOS))
        except (FileNotFoundError, ValueError): d = {}
        d[ts] = {'_forcado': canonical_agent(agent)}   # 23/09/2026: {agent: 10**9} era sobrescrito pelo tick do proprio agent com a interseccao real
        fileio.write_json(DONOS, d, indent=1)


def roteia_acao(s, a, ev, ref, agora, enfileira, acao_add):
    """Uma acao sua da ata vai para a fila de pendencias (prazo curto, com lembrete) ou para a daily (aguardando /
    proximas, sem lembrete). Devolve o destino: ('pendencia', None) | ('aguardando', quem) | ('proximas', data|None)."""
    dia = datetime.fromisoformat(ev['ini']).astimezone(BRT).date() if ev.get('ini') else agora.astimezone(BRT).date()
    balde, extra = classifica_prazo(a['prazo'], a['acao'], dia)
    texto = f'{a["acao"]} (ata {ev["titulo"]})'
    # the Prazo in Planou (deadline): the cell resolved against the meeting day ("quarta" = the Wednesday after it);
    # a conditional action has none
    from .deadlines import resolve
    prazo = resolve(a['prazo'], dia, marker=False) if a['prazo'] and balde != 'aguardando' else None
    if balde == 'aguardando':
        acao_add(s, (texto + (f' · prazo {a["prazo"]}' if a['prazo'] else ''))[:200], 'aguardando', extra)
    elif balde == 'proximas':
        x = acao_add(s, (texto + f' · ate {extra or a["prazo"]}')[:200], 'proximas', None)
        if prazo and isinstance(x, dict): x['deadline'] = prazo
    else:
        p = enfileira(s, 'reuniao', ref, f'Ata: {ev["titulo"]}', (a['acao'] + (f' · prazo {a["prazo"]}' if a['prazo'] else ''))[:160], agora.isoformat(), agora)
        if prazo and isinstance(p, dict): p['deadline'] = prazo
    return balde, extra


# ---------------- tick e bloco == GRAVACOES (iguais em todos os agents) ----------------
def tick(s, agent, eventos, enfileira, desde=None, dry=False, janela=GRAVACOES_JANELA, acao_add=None):
    """Reuniao da agenda gravada? transcrita? ata escrita? Acoes do usuario na ata viram pendencias 'reuniao', ou acoes
    nos baldes 'aguardando'/'proximas' da daily quando o prazo e' condicional ou distante (classifica_prazo).
    eventos(ini, fim) = reunioes da agenda deste agent; acao_add(s, texto, balde, quem) = daily.acao_add por padrao.
    Um relato por transicao de estado."""
    if acao_add is None:
        from .daily import acao_add
    primeira = 'gravacoes' not in s and desde is None
    st = s.setdefault('gravacoes', {'eventos': {}, 'tamanhos': {}, 'acoes': []})
    fixo = desde is not None
    agora = datetime.now(timezone.utc)
    ini = (desde if desde is not None else agora - janela)
    evs = eventos(ini, agora)
    duracoes = dict(st.get('duracoes') or {})
    vids = videos(ini - timedelta(hours=1), None if fixo else (st.get('tamanhos') or {}), duracoes, agora)
    sit = situacao(evs, vids, agora, {} if fixo else st.get('tamanhos') or {})
    perde = reivindica(agent, [(x['video']['ts'], sobrepoe(x['ev'], x['video'])) for x in sit if x['video'] and x['video']['dur']], dry or fixo)
    sit = [x for x in sit if not (x['video'] and x['video']['ts'] in perde)]
    out, lancadas, acoes_novas = [], [], []
    for x in sit:
        ev, v, est = x['ev'], x['video'], x['estado']
        chave = ev['id']
        antes = (st['eventos'].get(chave) or {}).get('estado')
        if est == 'pronta_para_transcrever' and not (dry or fixo):
            try: lanca_transcricao(v['path']); est = 'transcrevendo'; lancadas.append(x)
            except Exception as e: x['erro'] = f'{type(e).__name__}: {e}'
        x['estado'] = est
        if est == 'ata_pronta' and not (dry or fixo):
            for a in x['acoes']:
                if not a['minha']: continue
                ref = f'{v["ts"]}#{a["n"]}'          # ts, nao stem: depois da ata o .mp4 ganha " - <reuniao>" e o stem muda
                if ref in st['acoes']: continue
                st['acoes'].append(ref)
                if primeira: continue                # ata antiga: base plantada, nao cobra
                destino = roteia_acao(s, a, ev, ref, agora, enfileira, acao_add)
                acoes_novas.append((x, dict(a, destino=destino)))
        if est == 'ata_pronta' and v and not v['arquivado'] and not (dry or fixo):        # .mp4 vai para F:\\Videos; ata_<stem>/ fica
            erro = erro_arquivar(v['ts'])
            if erro: x['erro'] = f'arquivar: {erro}'; out.append(x)
            else:
                try:
                    dst = arquiva_video(v, ev['titulo'], _ARQ.get('existe', True), _ARQ.get('nomes', ()))
                    if dst: x['arquivado'] = dst; out.append(x)
                except Exception as e: x['erro'] = f'arquivar: {type(e).__name__}: {e}'; out.append(x)
        silencio = primeira and est in ('nao_gravada', 'ata_pronta', 'gravando', 'finalizando')
        if est != antes and est != 'sem_video_ainda' and not silencio and x not in out: out.append(x)
        if not (dry or fixo): st['eventos'][chave] = {'estado': est, 'titulo': ev['titulo'], 'ini': ev['ini'], 'video': v['stem'] if v else None, 'visto': agora.isoformat()}
    if not (dry or fixo):
        st['tamanhos'] = {v['stem']: [v['tam'], int(v['mtime'])] if 'mtime' in v else v['tam'] for v in vids}
        st['duracoes'] = {v['ts']: duracoes[v['ts']] for v in vids if v['ts'] in duracoes}
        corte = (agora - timedelta(days=7)).isoformat()
        st['eventos'] = {k: e for k, e in st['eventos'].items() if e.get('visto', '') >= corte}
    return {'mudancas': out, 'lancadas': lancadas, 'acoes': acoes_novas, 'todas': sit if fixo else [], 'base': primeira}


ROTULO = {'nao_gravada': 'NAO GRAVADA', 'gravando': 'GRAVANDO (video ainda aberto — nao transcreve)', 'finalizando': 'FINALIZANDO (espera o tamanho parar)',
          'aguardando_gpu': 'AGUARDANDO GPU (outro run.sh rodando)', 'pronta_para_transcrever': 'PRONTA PARA TRANSCREVER', 'transcrevendo': 'TRANSCREVENDO (run.sh lancado)',
          'ata_pendente': 'TRANSCRICAO PRONTA, ATA PENDENTE -> escrever a ata (meeting-minutes passos 2, 3 e 6)', 'ata_pronta': 'ATA PRONTA', 'sem_video_ainda': 'sem video ainda'}


def imprime_bloco(r, extras=()):
    """Bloco == GRAVACOES. extras = linhas do agent antes dos itens (ex.: agenda de uma instancia vencida). True se imprimiu."""
    itens = r['todas'] or r['mudancas']
    if not (itens or r['acoes'] or extras): return False
    print('== GRAVACOES')
    for ln in extras: print(ln)
    for x in itens:
        ev, v = x['ev'], x['video']
        quando = f'{_hora(ev["ini"])}–{datetime.fromisoformat(ev["fim"]).astimezone(BRT).strftime("%H:%M")}'
        vid = f' · {v["stem"]}.mp4 ({int(v["dur"] // 60)} min, {v["tam"] / 1e6:.0f} MB)' if v and v['dur'] else (f' · {v["stem"]}.mp4 ({v["tam"] / 1e6:.0f} MB)' if v else '')
        print(f'-- {ROTULO.get(x["estado"], x["estado"])}: {ev["titulo"]} · {quando}{vid}' + (f' · ERRO {x["erro"]}' if x.get('erro') else ''))
        if v and x['estado'] in ('ata_pendente', 'ata_pronta'): print(f'   pasta: {v["workdir"]}')
        if x.get('arquivado'): print(f'   ✓ video indo para {x["arquivado"]} em segundo plano (ata e transcricao ficam na pasta de origem)')
    for x, a in r['acoes']:
        balde, extra = a.get('destino') or ('pendencia', None)
        onde = {'aguardando': f' -> aguardando {extra} (sem lembrete)', 'proximas': f' -> proximas, ate {extra or a["prazo"]} (sem lembrete)'}.get(balde, '')
        print(f'-- ACAO SUA (ata {x["ev"]["titulo"]}): #{a["n"]} {a["acao"]}' + (f' · prazo {a["prazo"]}' if a['prazo'] else '') + onde)
    return True


def cli(eventos, rotulo='da agenda', aviso=None):
    """CLI de consulta comum: --horas N | --acoes <workdir> | --json."""
    ap = argparse.ArgumentParser()
    ap.add_argument('--horas', type=float, default=24); ap.add_argument('--acoes', metavar='WORKDIR'); ap.add_argument('--json', action='store_true')
    ap.add_argument('--dono', nargs=2, metavar=('TS', 'AGENT'), help="'YYYY-MM-DD HH-MM-SS' agent — o video passa a ser deste agent (os outros largam)")
    a = ap.parse_args()
    if a.dono: forca_dono(a.dono[0], a.dono[1]); print(f'{a.dono[0]} -> {a.dono[1]}'); return
    if a.acoes:
        for x in acoes_da_ata(a.acoes): print(f'{"★" if x["minha"] else " "} #{x["n"]} {x["acao"]} · {x["resp"]}' + (f' · prazo {x["prazo"]}' if x['prazo'] else ''))
        return
    agora = datetime.now(timezone.utc); desde = agora - timedelta(hours=a.horas)
    sit = situacao(eventos(desde, agora), videos(desde - timedelta(hours=1)), agora)
    if a.json: print(json.dumps(sit, ensure_ascii=False, indent=1, default=str)); return
    if aviso: aviso(agora)
    if not sit: print(f'nenhuma reuniao {rotulo} nas ultimas {a.horas:g} h'); return
    imprime(sit)
