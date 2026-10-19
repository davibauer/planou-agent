#!/usr/bin/env python3
"""The instance workspace (layout in paths.py): the context folder the user opens, searches and backs up.

CLI (through watch.py):
  watch.py <instance> --workspace                       where it is and what is there
  watch.py <instance> --workspace init                  creates the folders, CONTEXT.md (template) and README.md; never
                                                        overwrites what exists
  watch.py <instance> --workspace artifact FILE [SLUG] [--task CODE|PID]
                                                        copies FILE to artifacts/YYYY-MM-DD-<slug>.<ext> and prints the path
                                                        (what the watcher produces for the user goes there, not to a
                                                        session scratchpad the user cannot open); with --task, the copy
                                                        also goes to that Planou task as an attachment (watch_core.planou
                                                        attach_file: sent again whenever it changes)
  watch.py <instance> --workspace meetings [--dry]      copies the meetings already transcribed (ata_* folders of the
                                                        recordings library) that belong to this instance into
                                                        meetings/YYYY-MM-DD-HHMM-<title>/; never moves (watch_core.recordings
                                                        keeps using the original) and never overwrites; the
                                                        recordings source already does this by itself in the tick the
                                                        minutes get ready (PLN0409), so this is for the backlog

Which instance a meeting belongs to: the owner watch_core.recordings recorded (recording_owners.json, from 21/09/2026 on); otherwise the
words in config.json "meetings_keywords" of every instance, counted in the minutes' title (x10) and in the start of the
transcript — the instance with the highest score takes it, a tie, no hit or fewer than MINIMO points leaves it out
(listed, not copied). config.json "meetings_include": [stems "YYYY-MM-DD HH-MM-SS"] forces a meeting into an instance. Copied
(renamed to English): ata.md -> minutes.md, transcricao.md -> transcript.md, transcript.json, speakers.json, video.txt; not the voice prints nor the speaking diagnosis
(personal, and they belong to meeting-minutes).
"""
import json, os, re, shutil, sys
from datetime import datetime

import paths

CONTEXTO = """# {nome} — contexto

Visão viva desta instância: o que a sessão lê ao abrir, antes do primeiro tick. Curto (1–2 telas); o detalhe fica em
`decisions/`, `meetings/` e `journal/`. Atualizar quando algo aqui deixar de ser verdade.

## Projeto
-

## Pessoas
-

## Ambientes e acessos
-

## Decisões em aberto
-

## Combinados (o que o agent faz sozinho e o que espera ok)
-
"""

README = """# Espaço de trabalho — {nome}

Contexto desta instância do work-watch, separado do estado da máquina (`~/.config/work-watch/{nome}/`).

- `CONTEXT.md` — visão viva: projeto, pessoas, ambientes, decisões em aberto. Lido no início de cada sessão.
- `messages/` — captura bruta, só acrescenta: `YYYY-MM/<fonte>-YYYY-MM-DD.jsonl`.
- `meetings/` — uma pasta por reunião: `YYYY-MM-DD-HHMM-<assunto>/` (`minutes.md`, `transcript.md`, `transcript.json`, `speakers.json`).
- `artifacts/` — o que o agent gera para você usar ou mandar: `YYYY-MM-DD-<assunto>.<ext>`.
- `decisions/` — uma decisão por arquivo (ADR): `ADR-NNNN-<assunto>.md`.
- `journal/` — a daily e a retro do dia em texto: `YYYY-MM-DD.md`.

Nomes de pastas e arquivos fixos em inglês (padrão técnico); o conteúdo fica no idioma da instância.

Backup: a pasta inteira (não há segredo aqui). Sem prazo de guarda: o contexto pode continuar válido por muito tempo.
Nome começando pela data, formatos abertos (Markdown, JSONL): dá para buscar com `rg` sem depender de ferramenta.
"""


def slug(t):
    t = re.sub(r'[^\w.-]+', '-', (t or '').strip().lower(), flags=re.UNICODE).strip('-.')
    return t[:60] or 'artifact'


def init():
    novos = []
    os.makedirs(paths.WORKSPACE, exist_ok=True)
    for d in paths.WORKSPACE_DIRS:
        if not os.path.isdir(paths.area(d)): paths.area(d, criar=True); novos.append(d + '/')
    for nome, modelo in (('CONTEXT.md', CONTEXTO), ('README.md', README)):
        f = os.path.join(paths.WORKSPACE, nome)
        if not os.path.exists(f):
            open(f, 'w', encoding='utf-8').write(modelo.format(nome=paths.NOME)); novos.append(nome)
    print(f'{paths.NOME}: workspace em {paths.WORKSPACE}' + (f' (criado: {", ".join(novos)})' if novos else ' (ja existia)'))


def artefato(arq, nome=None):
    if not os.path.isfile(arq): sys.exit(f'nao existe: {arq}')
    base, ext = os.path.splitext(os.path.basename(arq))
    hoje = datetime.now().strftime('%Y-%m-%d')
    s = slug(nome or base)
    if s.startswith(hoje): s = s[len(hoje):].lstrip('-') or 'artifact'
    destino = os.path.join(paths.area('artifacts', criar=True), f'{hoje}-{s}{ext.lower()}')
    n = 2
    while os.path.exists(destino):
        destino = os.path.join(paths.area('artifacts'), f'{hoje}-{s}-{n}{ext.lower()}'); n += 1
    shutil.copy2(arq, destino)
    print(destino)
    return destino


VIDEOS = os.environ.get('ATA_VIDEOS_DIR') or os.path.expanduser('~/Videos')   # config.json "videos_dir" wins (watch.py)
from watch_core import config as _wc
from watch_core.agents import agent_of, same_agent
from watch_core import slowfs
DONOS = _wc.file(_wc.RECORDING_OWNERS)
MINIMO = 3                 # sem a palavra no titulo (10 pontos), a transcricao precisa citar a empresa ao menos 3 vezes
COPIA = {'ata.md': 'minutes.md', 'transcricao.md': 'transcript.md', 'transcript.json': 'transcript.json',
         'speakers.json': 'speakers.json', 'video.txt': 'video.txt'}
TS = re.compile(r'^ata_(\d{4}-\d{2}-\d{2}) (\d{2})-(\d{2})-\d{2}(?: - (.+))?$')


def _palavras():
    """{instance: [words]} of every instance (a meeting is only this instance's when it scores higher than the others)."""
    out = {}
    for i in paths.instancias():
        try: c = json.load(open(os.path.join(paths.BASE, i, 'config', 'config.json'), encoding='utf-8'))
        except (OSError, ValueError): continue
        ag = agent_of(c, f'work-watch-{i}')
        out[i] = ([w.lower() for w in c.get('meetings_keywords') or []], ag, set(c.get('meetings_include') or []))
    return out


def dono(pasta, stem, donos, palavras):
    """(instance | None, why)."""
    fixo = [i for i, (_, _, inc) in palavras.items() if stem in inc]
    if len(fixo) == 1: return fixo[0], 'meetings_include'
    d = donos.get(stem) or {}
    if d.get('_forcado'):
        v = d['_forcado']; return next((i for i, (_, ag, _i) in palavras.items() if same_agent(ag, v) or i == v), None), 'forcado'
    reais = {k: n for k, n in d.items() if not k.startswith('_')}
    if reais:
        v = max(reais, key=reais.get); return next((i for i, (_, ag, _i) in palavras.items() if same_agent(ag, v) or i == v), None), 'agenda'
    titulo = (_cabecalho(pasta) + ' ' + os.path.basename(pasta)).lower()
    try: corpo = slowfs.read_text(os.path.join(pasta, 'transcricao.md'), 20000).lower()   # drive of the videos: deadline
    except OSError: corpo = ''
    pontos = {i: 10 * sum(titulo.count(w) for w in ws) + sum(corpo.count(w) for w in ws) for i, (ws, _, _i) in palavras.items()}
    pontos = {i: n for i, n in pontos.items() if n}
    if not pontos: return None, 'sem palavra'
    if max(pontos.values()) < MINIMO: return None, f'pouca evidencia {pontos}'
    top = sorted(pontos.values(), reverse=True)
    if len(top) > 1 and top[0] == top[1]: return None, f'empate {pontos}'
    return max(pontos, key=pontos.get), f'palavras {pontos}'


def _cabecalho(pasta):
    """The '# ' line of ata.md ('' without one). The ata_ folder lives on the videos drive: read with the slowfs deadline
    (slowfs.Stuck goes up)."""
    try: texto = slowfs.read_text(os.path.join(pasta, 'ata.md'), 64 << 10)
    except OSError: return ''
    return next((l[2:] for l in texto.splitlines() if l.startswith('# ')), '')


def _titulo(pasta, rotulo):
    if rotulo: return rotulo
    titulo = _cabecalho(pasta)
    return re.sub(r'^(ata|minutes|meeting notes)\s*[—:-]\s*', '', titulo.strip(), flags=re.I) or 'reuniao'


def _ja_copiada(m, ja):
    """A meetings/ folder with the same YYYY-MM-DD-HHMM- prefix (even renamed by hand) means already copied."""
    dia, hh, mm, _ = m.groups()
    return any(n.startswith(f'{dia}-{hh}{mm}-') for n in ja)


def copia(pasta, porque, ja, dry=False):
    """Copies one transcribed meeting (the ata_ folder `pasta`) to meetings/YYYY-MM-DD-HHMM-<title>/ and returns
    (stem, title, destination), or None when it is not an ata_ folder or is already there. Never overwrites what is in
    meetings/ (the person may have edited it) and never leaves half a copy behind: the files go to a hidden temporary
    folder first, renamed at the end. `ja` = names already in meetings/ (gets the new one). Every read of `pasta` (on
    the videos drive) goes through slowfs, in a child with a deadline: a drive that does not answer raises slowfs.Stuck
    and never hangs the runner."""
    nome = os.path.basename(pasta.rstrip(os.sep))
    m = TS.match(nome)
    if not m or _ja_copiada(m, ja): return None
    dia, hh, mm, rotulo = m.groups()
    titulo = _titulo(pasta, rotulo)
    destino = os.path.join(paths.area('meetings'), f'{dia}-{hh}{mm}-{slug(titulo)[:50]}')
    if dry: return nome[4:4 + 19], titulo.strip()[:70], destino
    tmp = os.path.join(paths.area('meetings', criar=True), f'.copiando-{os.path.basename(destino)}')
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        os.makedirs(tmp)
        copiados = slowfs.copy_into(pasta, tmp, COPIA)
        open(os.path.join(tmp, 'source.md'), 'w', encoding='utf-8').write(
            f'Original: `{pasta}` (as gravacoes continuam usando esta pasta)\nDono: {porque}\n'
            + ('' if 'video.txt' in copiados else f'Video: `{nome[4:]}.mp4` (em {VIDEOS} ou F:\\Videos)\n'))
        os.rename(tmp, destino)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    ja.append(os.path.basename(destino))
    return nome[4:4 + 19], titulo.strip()[:70], destino


def copia_da_gravacao(workdir):
    """PLN0409: the recordings source calls this in the tick the minutes get ready (ata.md in the ata_ folder of a
    recording this instance claimed), before the video is archived. Returns the destination, or None when there is
    nothing to do: no workspace (not configured or not created with --workspace init), already copied, or a meeting
    forced into another instance (meetings_include / forced owner). Otherwise the calendar claim of the recordings
    engine decides: the engine only calls this for a recording this instance claimed (and did not lose), so it is
    copied even when the keywords, or an owners entry, point to another instance; on an exact tie of the claim both
    instances keep the recording and both copy it, as the engine already treats both as owners. Raises on a failed
    copy, slowfs.Stuck included (the source backs off and warns once)."""
    if not paths.WORKSPACE or not os.path.isdir(paths.WORKSPACE): return None
    m = TS.match(os.path.basename(workdir.rstrip(os.sep)))
    if not m: return None
    area = paths.area('meetings')
    ja = os.listdir(area) if os.path.isdir(area) else []
    if _ja_copiada(m, ja): return None                     # steady state: only meetings/ is read, never the videos drive
    try: donos = json.load(open(DONOS, encoding='utf-8'))
    except (OSError, ValueError): donos = {}
    stem = os.path.basename(workdir.rstrip(os.sep))[4:4 + 19]
    inst, porque = dono(workdir, stem, donos, _palavras())
    if inst != paths.NOME:
        if inst and porque in ('meetings_include', 'forcado'): return None
        porque = 'gravacao reivindicada pela agenda desta instancia'
    r = copia(workdir, porque, ja)
    return r and r[2]


def reunioes(dry=False):
    try: donos = json.load(open(DONOS, encoding='utf-8'))
    except (OSError, ValueError): donos = {}
    palavras = _palavras()
    if not (palavras.get(paths.NOME) or ([], None, None))[0]:
        print(f'{paths.NOME}: sem "meetings_keywords" no config — so entram as reunioes com dono gravado pela agenda')
    copiadas, fora = [], []
    ja = os.listdir(paths.area('meetings')) if os.path.isdir(paths.area('meetings')) else []
    for nome in sorted(os.listdir(VIDEOS)):
        m = TS.match(nome)
        pasta = os.path.join(VIDEOS, nome)
        if not m or not os.path.isdir(pasta) or 'DESCARTAR' in nome: continue
        stem = nome[4:4 + 19]
        try:
            inst, porque = dono(pasta, stem, donos, palavras)
            if inst != paths.NOME:
                if inst is None: fora.append(f'{stem} ({porque})')
                continue
            r = copia(pasta, porque, ja, dry)
        except slowfs.Stuck as e:
            fora.append(f'{stem} ({e})'); continue
        if r: copiadas.append((r[0], r[1], porque))
    print(f'{paths.NOME}: {len(copiadas)} reuniao(oes) {"a copiar" if dry else "copiada(s)"} para {paths.area("meetings")}')
    for stem, tit, porque in copiadas: print(f'  + {stem} · {tit} · {porque}')
    if fora: print(f'  sem dono (nao copiadas, {len(fora)}): ' + '; '.join(fora))


def mostra():
    print(f'{paths.NOME}: workspace {paths.WORKSPACE}' + ('' if os.path.isdir(paths.WORKSPACE) else ' (nao criado: --workspace init)'))
    if not os.path.isdir(paths.WORKSPACE): return
    for d in ('CONTEXT.md',) + paths.WORKSPACE_DIRS:
        p = os.path.join(paths.WORKSPACE, d)
        if os.path.isdir(p):
            n = sum(len(fs) for _, _, fs in os.walk(p)); print(f'  {d + "/":12s} {n} arquivo(s)')
        else:
            print(f'  {d:12s} {"ok" if os.path.exists(p) else "falta"}')


def anexa(tarefa, arq):
    """The artifact of a task also goes to it in Planou (PLN0052). Prints one line; never fails the copy."""
    from watch_core import planou
    agente = getattr(paths, 'NAME', None) or f'work-watch-{paths.NOME}'
    planou.configure_agent(agente, paths.ROOT)
    resultado, linhas = planou.attach_file(tarefa, arq)
    for l in linhas: print(l)
    print(planou.ATTACH_SAID[resultado].format(task=tarefa, name=os.path.basename(arq)))


def cli(args):
    """args = what comes after --workspace."""
    if not args: return mostra()
    if args[0] == 'init': return init()
    if args[0] in ('artifact', 'artefato') and len(args) >= 2:
        tarefa = None
        if '--task' in args:
            i = args.index('--task')
            if i + 1 >= len(args): sys.exit('uso: --workspace artifact ARQUIVO [NOME] --task CODIGO|PID')
            tarefa, args = args[i + 1], args[:i] + args[i + 2:]
        destino = artefato(args[1], ' '.join(args[2:]) or None)
        if tarefa: anexa(tarefa, destino)
        return destino
    if args[0] in ('meetings', 'reunioes'): return reunioes(dry='--dry' in args[1:])
    sys.exit('uso: --workspace [init | artifact ARQUIVO [NOME] [--task CODIGO|PID] | meetings [--dry]]')
