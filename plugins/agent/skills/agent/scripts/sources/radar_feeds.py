"""Source radar_feeds (PLN0118, behavior tech-radar): official blogs and curated YouTube channels, by RSS or Atom. The
details (week gate, seen set, stacks) are in scripts/radar.py.

Config: {"type": "radar_feeds",
         "feeds": [{"name": "blog-x", "url": "https://blog.exemplo.com/feed.xml", "stacks": ["Python"],
                    "require_match": false}],
         "youtube": [],                      channel ids (UC...), the person chooses them
         "fixtures": null}
A curated feed keeps every post of the window (`window_days` of the behavior); with "require_match": true only the
posts that name a stack. One feed down is a warning line; all of them down breaks the source.
A YouTube feed that fails (404, 5xx, timeout) is tried 3 times, waiting 1 s and 2 s, within 10 s per channel
(radar.fetch_retry, PLN0194); a 404 that persists is a warning, not "nothing new".
"""
import time
from datetime import datetime, timezone

import radar
from adapters import Fonte as Base

YOUTUBE = 'https://www.youtube.com/feeds/videos.xml?channel_id={}'


class Fonte(Base):
    tipo = 'radar_feeds'
    conserto = 'conferir as URLs de "feeds" e os ids de "youtube" da fonte radar_feeds'
    sleep = staticmethod(time.sleep)          # tests replace it (no real wait)

    def feeds(self):
        out = [dict(f, kind=f.get('kind') or 'blog') for f in self.cfg.get('feeds') or []
               if isinstance(f, dict) and isinstance(f.get('url'), str) and f.get('name')]
        out += [{'name': f'youtube:{c}', 'url': YOUTUBE.format(c), 'kind': 'youtube'}
                for c in self.cfg.get('youtube') or [] if isinstance(c, str) and c.strip()]
        return out

    def tick(self, s, desde, dry):
        cfg = self.ctx.cfg if self.ctx else {}
        o, feeds, errors = radar.options(cfg), self.feeds(), []
        if not feeds: return None
        now = datetime.now(timezone.utc)

        def items():
            got = radar.collect_feeds(feeds, o['stacks'], now, int(o['window_days']), self.cfg.get('fixtures'), errors,
                                     sleep=self.sleep)
            if errors and len(errors) == len(feeds): raise SystemExit('nenhum feed respondeu: ' + '; '.join(errors)[:300])
            return got
        r = radar.gate_and_mark(self, s, items, dry, now)
        if r is not None and errors: r['errors'] = errors
        return r

    def imprime(self, r):
        shown = radar.print_block(self, r)
        for e in (r or {}).get('errors') or []: print(f'AVISO (radar_feeds): {e}'); shown = True
        return shown
