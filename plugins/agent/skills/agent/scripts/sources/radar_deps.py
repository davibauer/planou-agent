"""Source radar_deps (PLN0118, behavior tech-radar): new minor and major versions of the dependencies declared in the
repositories of the config. The details (files read, registries, week gate, seen set) are in scripts/radar.py.

Config: {"type": "radar_deps", "repos": ["~/src/meu-projeto"], "client_repos": [], "include_patch": false,
         "max_lookups": 80, "skip": ["@minha-empresa/*"], "fixtures": null}
`repos` defaults to the "path" of each entry of the instance's top-level "repos". `client_repos` (a customer's
repositories) stays empty until the person lists them: their package names go to the public registries. `skip` leaves
out private packages (the name, with * as a wildcard) so they never reach a registry.
"""
import os
from datetime import datetime, timezone

import radar
from adapters import Fonte as Base


def _entries(v):
    out = []
    for e in v or []:
        if isinstance(e, str): out.append((os.path.basename(os.path.expanduser(e).rstrip('/')), e))
        elif isinstance(e, dict) and isinstance(e.get('path'), str):
            out.append((e.get('name') or os.path.basename(os.path.expanduser(e['path']).rstrip('/')), e['path']))
    return out


class Fonte(Base):
    tipo = 'radar_deps'
    conserto = 'conferir "repos" da fonte radar_deps (caminhos que existem) e a rede até npm, NuGet e PyPI'

    def repos(self):
        top = (self.ctx.cfg.get('repos') if self.ctx else None) or []
        own = _entries(self.cfg.get('repos')) if 'repos' in self.cfg else _entries(top)
        return own + _entries(self.cfg.get('client_repos'))

    def tick(self, s, desde, dry):
        cfg = self.ctx.cfg if self.ctx else {}
        o = radar.options(cfg)
        repos = [(n, p) for n, p in self.repos() if os.path.isdir(os.path.expanduser(p))]
        if not repos: raise SystemExit('nenhum repositório para ler (radar_deps: "repos" ou "repos" da instância)')
        return radar.gate_and_mark(self, s, lambda: radar.collect_deps(
            repos, o['stacks'], self.cfg.get('fixtures'), bool(self.cfg.get('include_patch')),
            int(self.cfg.get('max_lookups') or 80), self.cfg.get('skip') or ()), dry, datetime.now(timezone.utc))

    def imprime(self, r):
        return radar.print_block(self, r)
