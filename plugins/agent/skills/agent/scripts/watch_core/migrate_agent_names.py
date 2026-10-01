#!/usr/bin/env python3
"""Migracao dos dados do usuario para os nomes de agent (work-watch 0.19.0).

Os "vigias" viraram agents (work-watch-<instancia>, job-scout, travel-agent). O codigo ainda le os nomes velhos
nesta versao; esta migracao grava os novos no que ja esta em disco. Nenhum nome esta embutido aqui: o nome canonico vem
de watch_core.agents (config do usuario "legacy_agent_names", o mapa generico e as pastas de instancia do work-watch).

  a) ~/.config/work-watch/<instancia>/config/config.json: "vigia" -> "agent": "work-watch-<instancia>" (o nome da
     pasta, qualquer que fosse o valor velho); "gatilhos_vigia" ->
     "gatilhos_triagem" (nos ganchos); "google_dir" apontando para ~/.config/<nome>-vigia -> "secrets/google" (depois
     de copiar as credenciais, item d);
  b) lessons.md do watch-core: cabecalhos `## dd/mm/aaaa · <nome velho>` -> nome canonico;
  c) recording_owners.json: donos com nome velho -> canonico (fica a maior interseccao);
  d) copia (nunca apaga o original, nunca sobrescreve): ~/.config/vigia/notion.env -> ~/.config/watch-core/secrets/
     notion.env, os arquivos legados do watch-core (config.LEGACY) e client_secret.json + token.json de
     ~/.config/<nome>-vigia/ -> ~/.config/work-watch/<instancia>/secrets/google/.

Idempotente; antes de mudar um arquivo guarda uma copia `<arquivo>.bak-agent-names` (so a primeira: a segunda rodada
nao tem o que mudar). Sem --apply so lista o que faria.

  python3 -m watch_core.migrate_agent_names            # dry-run: lista
  python3 -m watch_core.migrate_agent_names --apply    # aplica

Caminhos lidos na hora da chamada (HOME, WATCH_CORE_HOME), para os testes rodarem num HOME temporario.
"""
import json, os, re, shutil, sys

from . import fileio
from .agents import canonical_agent, legacy_agent_names

BAK = '.bak-agent-names'
GOOGLE_FILES = ('client_secret.json', 'token.json')


def _home(p): return os.path.expanduser(p)
def _wc_root(): return _home(os.environ.get('WATCH_CORE_HOME') or '~/.config/watch-core')
def _ww_base(): return _home('~/.config/work-watch')


def _backup(path, apply):
    if apply and os.path.exists(path) and not os.path.exists(path + BAK): shutil.copy2(path, path + BAK)


def _write(path, text, apply):
    if not apply: return
    _backup(path, apply)
    fileio.write(path, text)          # keeps the file's mode


def _copy(src, dst, apply, secret=False):
    """Copia src -> dst se dst nao existe. Devolve a linha do relatorio ou None."""
    if not os.path.isfile(src) or os.path.exists(dst): return None
    if apply:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        if secret: os.chmod(dst, 0o600); os.chmod(os.path.dirname(dst), 0o700)
    return f'copia {src} -> {dst}'


def _legacy_dirs(inst, cfg):
    """~/.config/<nome>-vigia das credenciais antigas de uma instancia (nome da instancia, o "vigia" do config, os nomes
    velhos do agent)."""
    nomes = [inst, cfg.get('vigia') or ''] + legacy_agent_names(cfg.get('agent') or cfg.get('vigia') or inst)
    out = []
    for n in nomes:
        d = _home(f'~/.config/{n}-vigia') if n else ''
        if d and d not in out and os.path.isdir(d): out.append(d)
    return out


# ---- a) + d) instancias do work-watch ----------------------------------------------------------------------------------
def _instancia(inst, apply):
    out = []
    root = os.path.join(_ww_base(), inst)
    path = os.path.join(root, 'config', 'config.json')
    try: txt = open(path, encoding='utf-8').read(); cfg = json.loads(txt)
    except (OSError, ValueError) as e:
        return [f'{path}: nao li ({e.__class__.__name__}), pulei']
    novo, mudou = txt, []
    # credenciais do Google do agent antigo -> secrets/google da instancia
    dest = os.path.join(root, 'secrets', 'google')
    antigos = _legacy_dirs(inst, cfg)
    for d in antigos:
        for f in GOOGLE_FILES:
            r = _copy(os.path.join(d, f), os.path.join(dest, f), apply, secret=True)
            if r: out.append(r)
    tem_google = all(os.path.exists(os.path.join(dest, f)) or any(os.path.isfile(os.path.join(d, f)) for d in antigos)
                     for f in GOOGLE_FILES)
    legado = re.escape(_home('~/.config/')) + r'[a-z0-9_]+-vigia'

    def _google(m):
        if tem_google and re.fullmatch(legado, _home(m.group(2)).rstrip('/')):
            mudou.append(f'"google_dir": "{m.group(2)}" -> "secrets/google"'); return m.group(1) + '"secrets/google"'
        return m.group(0)
    novo = re.sub(r'("google_dir"\s*:\s*)"([^"]*)"', _google, novo)
    if 'agent' not in cfg and isinstance(cfg.get('vigia'), str):
        can = 'work-watch-' + inst                     # derivacao pela pasta da instancia, independe do valor velho
        novo = re.sub(r'"vigia"(\s*:\s*)"' + re.escape(cfg['vigia']) + '"', lambda m: f'"agent"{m.group(1)}"{can}"', novo, count=1)
        mudou.append(f'"vigia": "{cfg["vigia"]}" -> "agent": "{can}"')
    if '"gatilhos_vigia"' in novo:
        novo = novo.replace('"gatilhos_vigia"', '"gatilhos_triagem"'); mudou.append('"gatilhos_vigia" -> "gatilhos_triagem"')
    if novo != txt:
        try: json.loads(novo)
        except ValueError: return out + [f'{path}: a troca quebraria o JSON, pulei']
        _write(path, novo, apply)
        out.append(f'{path}: ' + '; '.join(dict.fromkeys(mudou)))
    return out


# ---- b) lessons.md ---------------------------------------------------------------------------------------------------
CAB = re.compile(r'(?m)^(## \d{2}/\d{2}/\d{4} · )(.+?)[ \t]*$')


def _lessons(path, apply):
    try: txt = open(path, encoding='utf-8').read()
    except FileNotFoundError: return []
    trocas = []

    def sub(m):
        can = canonical_agent(m.group(2))
        if can != m.group(2): trocas.append(m.group(2)); return m.group(1) + can
        return m.group(0)
    novo = CAB.sub(sub, txt)
    if novo == txt: return []
    _write(path, novo, apply)
    return [f'{path}: {len(trocas)} cabecalho(s) com nome velho ({", ".join(sorted(set(trocas)))}) -> canonico']


# ---- c) recording_owners.json ----------------------------------------------------------------------------------------
def _owners_novo(d):
    """(novo dict, quantos nomes velhos) de um recording_owners.json lido."""
    novo, n = {}, 0
    for ts, e in d.items():
        if not isinstance(e, dict): novo[ts] = e; continue
        x = {}
        for k, v in e.items():
            if k == '_forcado':
                c = canonical_agent(v); n += c != v; x[k] = c; continue
            if k.startswith('_'): x[k] = v; continue
            c = canonical_agent(k); n += c != k
            x[c] = max(x[c], v) if c in x and isinstance(v, (int, float)) and isinstance(x[c], (int, float)) else x.get(c, v)
        novo[ts] = x
    return novo, n


def _owners(path, apply):
    try: novo, n = _owners_novo(json.load(open(path, encoding='utf-8')))
    except FileNotFoundError: return []
    except ValueError: return [f'{path}: JSON invalido, pulei']
    if not n: return []
    if apply:
        import fcntl
        with open(path + '.lock', 'w') as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)              # recordings.reivindica grava com o mesmo lock
            novo, n = _owners_novo(json.load(open(path, encoding='utf-8')))
            _write(path, json.dumps(novo, indent=1), apply)
    return [f'{path}: {n} dono(s) com nome velho -> canonico']


# ---- d) arquivos legados do watch-core ---------------------------------------------------------------------------------
def _watch_core_legacy(apply):
    root, skills = _wc_root(), _home('~/.claude/skills')
    pares = [
        (os.path.join(skills, 'vigia-licoes', 'LICOES.md'), os.path.join(root, 'data', 'lessons.md'), False),
        (os.path.join(skills, 'vigia-licoes', 'REGRAS.md'), os.path.join(root, 'data', 'rules.md'), False),
        (_home('~/.config/vigia-gravacoes/donos.json'), os.path.join(root, 'data', 'recording_owners.json'), False),
        (_home('~/.config/vigia/notion.env'), os.path.join(root, 'secrets', 'notion.env'), True),
    ]
    return [r for a, b, sec in pares for r in [_copy(a, b, apply, secret=sec)] if r]


def migrate(apply=False):
    """Lista (e com apply=True faz) a migracao. Devolve as linhas do relatorio."""
    out = _watch_core_legacy(apply)
    base = _ww_base()
    try: insts = sorted(d for d in os.listdir(base) if os.path.isfile(os.path.join(base, d, 'config', 'config.json')))
    except FileNotFoundError: insts = []
    for i in insts: out += _instancia(i, apply)
    root = _wc_root()
    out += _lessons(os.path.join(root, 'data', 'lessons.md'), apply)
    out += _owners(os.path.join(root, 'data', 'recording_owners.json'), apply)
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    apply = '--apply' in argv
    linhas = migrate(apply)
    if not linhas: print('nada a migrar (ja esta com os nomes de agent)'); return 0
    print(('aplicado' if apply else 'dry-run (nada mudou; rode com --apply para aplicar)') + ':')
    for l in linhas: print('  ' + l)
    if apply: print(f'copias de seguranca: <arquivo>{BAK} ao lado de cada arquivo alterado')
    return 0


if __name__ == '__main__':
    sys.exit(main())
