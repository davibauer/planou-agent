"""Source radar_trending (PLN0118, behavior tech-radar): GitHub Trending of the week per language, kept only when the
repository name or description names a stack. Details in scripts/radar.py.

Config: {"type": "radar_trending", "languages": ["python", "typescript"], "min_stars": 0, "fixtures": null}
"""
from datetime import datetime, timezone

import radar
from adapters import Fonte as Base


class Fonte(Base):
    tipo = 'radar_trending'
    conserto = 'conferir "languages" da fonte radar_trending e a rede até github.com'

    def tick(self, s, desde, dry):
        cfg = self.ctx.cfg if self.ctx else {}
        o, now, errors = radar.options(cfg), datetime.now(timezone.utc), []
        langs = [l for l in self.cfg.get('languages') or [] if isinstance(l, str) and l.strip()]
        if not langs or not o['stacks']: return None

        def items():
            got = radar.collect_trending(langs, o['stacks'], self.cfg.get('fixtures'), int(self.cfg.get('min_stars') or 0), errors)
            if errors and len(errors) == len(langs): raise SystemExit('GitHub Trending não respondeu: ' + '; '.join(errors)[:300])
            return got
        r = radar.gate_and_mark(self, s, items, dry, now)
        if r is not None and errors: r['errors'] = errors
        return r

    def imprime(self, r):
        shown = radar.print_block(self, r)
        for e in (r or {}).get('errors') or []: print(f'AVISO (radar_trending): {e}'); shown = True
        return shown
