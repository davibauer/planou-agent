"""Adapter interface of the agent plugin: sources (Fonte) and hooks (Gancho). The same interface as work-watch's, so
its adapters move over unchanged (they import `from adapters import Fonte`).

An instance config lists sources under "sources" and hooks under "hooks"; each entry is {"type": "<module>", ...options}
(the work-watch spelling {"tipo": ...} under "fontes"/"ganchos" is read too, schema.py). `carrega(tipo)` looks for the
module first in <instance>/adapters/<tipo>.py (instance-only code that never goes into the plugin), then in the
plugin's `sources/` and `hooks/` folders (imported as adapters.<tipo>, see __path__ below). The module exposes `Fonte`
or `Gancho` (a subclass of the classes below); the orchestrator instantiates it with its config entry and the run context.

SOURCE (Fonte) -- one block of the tick output
  nome                    key in the result (res[nome]), in --so and in "FONTE QUEBRADA (nome)"; default = tipo
  lembrar                 {queue name: business hours until the 1st reminder}; the queue names this source feeds
                          (e.g. {'slack': 4.0}; recordings feed 'reuniao'). Empty = feeds no reminders
  tick(s, desde, dry)     -> dict (goes to res[nome]) or None (no key this time). desde = datetime of --since or None
                          (cursor mode); dry = do not touch state. May enqueue with core.enfileira. Failure: raise
                          SystemExit(msg) or any exception -> "FONTE QUEBRADA (nome): msg", exit 2
  imprime(r)              prints the block (stdout); returns True if printed something
  resolvidas(r)           [(pendencia, como)] resolved by this tick (default r['resolvidas'])
  linha_ref(p)            line under a pending item of this source in --pendentes (default '   ref: <ref>[ · <link>]'; None = no line;
                          the config key "linha_ref" overrides it with a template or null)
  caminhos()              paths the tick reads or writes; one on a Windows drive (/mnt/<letter>) is checked first, with
                          a deadline (drive_probe.py): a drive that does not answer gives "FONTE QUEBRADA (nome): drive
                          X sem resposta" and the tick is skipped instead of hanging (PLN0236). Default: every path in
                          the source's config plus the instance workspace
  conserto                one line: how to fix it when broken (daily page and Planou; the config key "conserto" wins)
  vence(s)                optional: ISO expiry of the source's credential when the source knows it (e.g. the GitLab token
                          reads it from the API); None = unknown. Planou shows it on the Ferramentas tab
  tarefas_extras(s, now)  optional: extra rows for the Notion tasks board ({id: row})
  tarefas_fechadas(s, codes, now)  optional: closes for Planou tasks the list dropped, confirmed at the origin
  args(ap) / cli(a, ctx)  optional extra CLI flags; cli returns True when it handled the command

HOOK (Gancho) -- runs around the sources
  nome, json_key          json_key: top-level key in --json output (always present, None when it did not run)
  requer                  source names that must have run clean (e.g. the Jira summary needs 'jira')
  posicao                 where its text goes: 'fontes' (right after the source blocks), 'depois' (after == PENDENTES,
                          default) or 'fim' (after the FONTE QUEBRADA lines)
  roda_em_dry             True = also runs on dry ticks (read-only hooks)
  so_sem_filtro           True = skipped when --so is given
  cruza(ctx)              phase 1, after the sources and before reminders (not dry): cross-source rules; returns
                          [(pendencia, como)] it resolved
  run(ctx)                phase 2, after reminders: returns a value (json_key) or None
  texto(valor)            the block for that value (default str(valor)); None = nothing to print
  publica(ctx, blocos)    phase 3, after the text blocks are built (not dry): daily page, lessons, tasks; returns
                          extra blocks. Anything that leaves the instance folder calls core.exige_live() first
  ao_anotar(ctx, p)       --pendente pN k=v changed a pending item (before it is saved): mirror it elsewhere
  args(ap) / cli(a, ctx)  optional extra CLI flags

ctx (Contexto): s (state), res, quebrados [(nome, erro)], dry, agora, cfg (instance config), so, args, fontes
(instances by name), live.
"""
import os, sys, importlib, importlib.util

import paths


class Fonte:
    tipo = None
    nome_padrao = None       # result key when the config does not give "nome" (default: tipo)
    lembrar = {}
    conserto = ''

    def __init__(self, cfg, ctx=None):
        self.cfg = cfg or {}
        self.ctx = ctx
        self.nome = self.cfg.get('nome') or self.nome_padrao or self.tipo
        if 'lembrar_horas' in self.cfg:
            h = self.cfg['lembrar_horas']
            self.lembrar = dict(h) if isinstance(h, dict) else ({next(iter(self.lembrar), self.nome): float(h)} if h is not None else {})
        self.conserto = self.cfg.get('conserto', self.conserto)
        if 'linha_ref' in self.cfg:                  # config wins: a template ("   id: {ref}") or null (no line)
            modelo = self.cfg['linha_ref']
            self.linha_ref = (lambda p: modelo.format(**p)) if modelo else (lambda p: None)
        self.configura()

    def configura(self): pass
    def tick(self, s, desde, dry): raise NotImplementedError
    def caminhos(self):
        import drive_probe
        return drive_probe.paths_in(self.cfg) + ([paths.WORKSPACE] if paths.WORKSPACE else [])
    def imprime(self, r): return False
    def resolvidas(self, r): return (r or {}).get('resolvidas', [])
    def linha_ref(self, p): return f'   ref: {p["ref"]}' + (f' · {p["link"]}' if p.get('link') else '')
    def tarefas_extras(self, s, agora): return {}
    def tarefas_fechadas(self, s, codigos, agora):
        """{code: row} for codes Planou still has open that the task list no longer shows and this source confirms were
        closed at the origin (row: status Feito / Descartada / Sem ação, concluida, fecha_na_origem). Reads only the
        state: the network goes in tick(), under the source's budget (PLN0391)."""
        return {}
    pend = ()
    def pronta(self, p, s): return None
    def vence(self, s): return None
    def prazo(self, it, p, s):
        """The Prazo (Planou deadline, YYYY-MM-DD) this source states for a task row `it` (its pending item `p` when
        it has one), from the source state: the due date of the work item / issue it links to. None = no deadline."""
        return None
    def args(self, ap): pass
    def cli(self, a, ctx): return False


class Gancho:
    tipo = None
    json_key = None
    requer = ()
    posicao = 'depois'
    roda_em_dry = False
    so_sem_filtro = False

    def __init__(self, cfg, ctx=None):
        self.cfg = cfg or {}
        self.ctx = ctx
        self.nome = self.cfg.get('nome') or self.tipo
        self.configura()

    def configura(self): pass
    def cruza(self, ctx): return []
    def run(self, ctx): return None
    def texto(self, valor): return None if valor is None else str(valor)
    def publica(self, ctx, blocos): return []
    def ao_anotar(self, ctx, p): pass
    def args(self, ap): pass
    def cli(self, a, ctx): return False


PACKAGES = ('sources', 'hooks', 'adapters')
# One import name for every built-in adapter: adapters.<tipo>, wherever the file lives (sources/, hooks/ or here). The
# work-watch adapters import each other that way (`from adapters import ms365`, `from adapters.gmail import ...`) and so
# do instance adapters (`from adapters.ado_pipelines import Sessao`); loading one file under two names would split its
# module globals (tokens, caches).
_HERE = os.path.dirname(os.path.abspath(__file__))
__path__ = [_HERE] + [os.path.join(paths.SCRIPTS, pkg) for pkg in PACKAGES if pkg != 'adapters']


def _builtin(tipo):
    """Folder (sources, hooks or adapters) that has a built-in module for this type, or None."""
    for pkg in PACKAGES:
        if os.path.isfile(os.path.join(paths.SCRIPTS, pkg, f'{tipo}.py')): return pkg
    return None


def existe(tipo):
    """True when an adapter of this type exists (instance folder or plugin)."""
    if not tipo or not tipo.replace('_', '').isalnum(): return False
    local = os.path.join(paths.ADAPTERS_DIR or '', f'{tipo}.py')
    return bool(paths.ADAPTERS_DIR and os.path.isfile(local)) or _builtin(tipo) is not None


def carrega(tipo):
    """Module for an adapter type: instance folder first, then the plugin (sources, hooks, adapters)."""
    if not tipo or not tipo.replace('_', '').isalnum(): raise ValueError(f'tipo de adapter invalido: {tipo!r}')
    local = os.path.join(paths.ADAPTERS_DIR or '', f'{tipo}.py')
    if paths.ADAPTERS_DIR and os.path.isfile(local):
        if paths.ADAPTERS_DIR not in sys.path: sys.path.insert(0, paths.ADAPTERS_DIR)   # helpers of the instance adapters
        nome = f'agent_local_{tipo}'
        if nome in sys.modules: return sys.modules[nome]
        spec = importlib.util.spec_from_file_location(nome, local)
        mod = importlib.util.module_from_spec(spec); sys.modules[nome] = mod; spec.loader.exec_module(mod)
        return mod
    if not _builtin(tipo): raise ValueError(f'adapter {tipo} nao existe')
    return importlib.import_module(f'adapters.{tipo}')


def instancia(entrada, classe, ctx=None):
    """{"type": ..., ...} -> Fonte/Gancho instance."""
    tipo = entrada.get('type') or entrada.get('tipo')
    mod = carrega(tipo)
    cls = getattr(mod, classe, None)
    if cls is None: raise ValueError(f'adapter {tipo} nao define {classe}')
    obj = cls(entrada, ctx)
    obj.modulo = mod
    return obj
