"""Source product_feeds (PLN0226, behavior product-radar): public changelogs, release notes and RSS of task apps, of
agent products and of discovery sites (Product Hunt, Show HN). The fetch, the feed parser, the week gate and the seen
set are the tech radar's (scripts/radar.py); the ideas are built by scripts/product_radar.py.

Config: {"type": "product_feeds",
         "feeds": [{"name": "linear", "url": "https://linear.app/rss/changelog.xml", "group": "tarefas"},
                   {"name": "show-hn", "url": "https://hnrss.org/show", "group": "descoberta", "require_match": true}],
         "no_feed": [{"name": "...", "why": "..."}],     the sources left out (no public feed), for the person to see
         "fixtures": null}
group: tarefas | agentes | descoberta (the label of the line). A feed keeps every post of the window (`window_days` of
product-radar); with "require_match": true only the posts that name one of the `terms` (discovery feeds). One feed
down is a warning line and the others go on; all of them down breaks the source (FONTE QUEBRADA).
"""
from datetime import datetime, timezone

import product_radar
import radar
from adapters import Fonte as Base


class Fonte(Base):
    tipo = 'product_feeds'
    conserto = 'conferir as URLs de "feeds" da fonte product_feeds (só feeds públicos que respondem)'

    def feeds(self):
        return [dict(f, kind=f.get('group') or f.get('kind') or 'tarefas') for f in self.cfg.get('feeds') or []
                if isinstance(f, dict) and isinstance(f.get('url'), str) and f.get('name')]

    def tick(self, s, desde, dry):
        cfg = self.ctx.cfg if self.ctx else {}
        o, feeds, errors = product_radar.options(cfg), self.feeds(), []
        if not feeds: return None
        now = datetime.now(timezone.utc)

        def items():
            got = radar.collect_feeds(feeds, o['terms'], now, int(o['window_days']), self.cfg.get('fixtures'), errors)
            if errors and len(errors) == len(feeds): raise SystemExit('nenhum feed respondeu: ' + '; '.join(errors)[:300])
            return got
        r = radar.gate_and_mark(self, s, items, dry, now, o, product_radar.FOLDER)
        if r is not None and errors: r['errors'] = errors
        return r

    def imprime(self, r):
        shown = radar.print_block(self, r, product_radar.HEADER)
        for e in (r or {}).get('errors') or []: print(f'AVISO (product_feeds): {e}'); shown = True
        return shown
