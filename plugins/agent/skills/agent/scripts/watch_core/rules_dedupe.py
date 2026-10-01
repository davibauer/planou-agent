"""Deriva entre os prompts dos agents: frase que aparece em dois arquivos de instrucoes, ou num deles e no rules.md.

Burro de proposito — sobreposicao de frases normalizadas, sem semantica. Existe porque em 22/09/2026 a mesma
licao de 21/09 (o loop) estava em tres redacoes, em dois agents (entao chamados vigias), e em nenhum lugar compartilhado. A regra: o que
vale para todos mora no rules.md (base de todo agent) e em nenhuma instrucao de agent. Uso: python3 -m watch_core.rules_dedupe

Quais arquivos comparar: rules_dedupe.files no config.json do watch_core ({"nome": "caminho"}); sem isso, as
instrucoes de cada instancia do work-watch e a do job-scout. Pares com fonte em comum (texto parecido esperado):
rules_dedupe.shared_sources ([["nome1", "nome2", "fonte"]]).
"""
import glob, os, re
from pathlib import Path

from . import config

MIN = 70          # frase curta demais ("Reportar como o script imprime.") repete por acaso
IGNORA = ('base comum:', '#')   # o ponteiro e igual em todos de proposito; titulo de secao nao e regra
PONTEIRO = 'compartilhad'           # "biblioteca compartilhada watch_core": ponteiro, nao regra


def arquivos():
    """{nome: Path} dos arquivos de instrucoes comparados."""
    cfg = config.get('rules_dedupe.files')
    if cfg: return {k: Path(os.path.expanduser(v)) for k, v in cfg.items()}
    out = {Path(p).parts[-3]: Path(p) for p in sorted(glob.glob(os.path.expanduser('~/.config/work-watch/*/config/instructions.md')))}
    js = Path(os.path.expanduser('~/.config/job-scout/config/instructions.md'))
    if js.exists(): out['job-scout'] = js
    return out


def fonte_comum():
    return {frozenset(x[:2]): x[2] for x in config.get('rules_dedupe.shared_sources') or [] if len(x) >= 3}


def _frases(txt):
    txt = re.sub(r'\A---\n.*?\n---\n', ' ', txt, flags=re.S)       # frontmatter (description repete o vocabulario)
    txt = re.sub(r'```.*?```', ' ', txt, flags=re.S)                 # blocos de codigo tem parametros por agent
    out = set()
    for f in re.split(r'(?<=[.;:!?])\s+|\n+', txt):
        n = re.sub(r'[`*_>\-\[\]()]', '', f).lower()
        n = re.sub(r'/?[a-z]+-vigia', '<agent>', n)                   # nome antigo: "/a-vigia" == "/b-vigia"
        n = re.sub(r'\s+', ' ', n).strip()
        if len(n) >= MIN and not n.startswith(IGNORA) and PONTEIRO not in n:
            out.add(n)
    return out


def checar():
    """[(frase, [onde])] das frases repetidas."""
    regras = Path(config.file(config.RULES_MD))
    base = _frases(regras.read_text(encoding='utf-8')) if regras.exists() else set()
    por = {v: _frases(p.read_text(encoding='utf-8')) for v, p in arquivos().items() if p.exists()}
    comum = fonte_comum()
    onde = {}
    for v, fs in por.items():
        for f in fs:
            onde.setdefault(f, []).append(v)
    rep = []
    for f, vs in onde.items():
        if f in base:
            rep.append((f, vs + ['rules.md']))
        elif len(vs) >= 2 and frozenset(vs) not in comum:
            rep.append((f, vs))
    return sorted(rep, key=lambda x: -len(x[1]))


if __name__ == '__main__':
    r = checar()
    print(f'{len(r)} frase(s) repetida(s)')
    for f, vs in r:
        print(f'- [{", ".join(vs)}] {f[:150]}')
