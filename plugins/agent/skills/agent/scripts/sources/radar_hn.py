"""Source radar_hn (PLN0118, behavior tech-radar): Hacker News stories of the week (Algolia search API, one generic
request) kept only when the title names a stack and has at least `min_points`. Details in scripts/radar.py.

Config: {"type": "radar_hn", "min_points": 100, "fixtures": null}
"""
from datetime import datetime, timezone

import radar
from adapters import Fonte as Base


class Fonte(Base):
    tipo = 'radar_hn'
    conserto = 'conferir a rede até hn.algolia.com'

    def tick(self, s, desde, dry):
        cfg = self.ctx.cfg if self.ctx else {}
        o, now = radar.options(cfg), datetime.now(timezone.utc)
        if not o['stacks']: return None
        return radar.gate_and_mark(self, s, lambda: radar.collect_hn(
            o['stacks'], now, int(o['window_days']), int(self.cfg.get('min_points') or 100), self.cfg.get('fixtures')),
            dry, now)

    def imprime(self, r):
        return radar.print_block(self, r)
