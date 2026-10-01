"""Source product_github (PLN0226, behavior product-radar): repositories created in the window under the GitHub topics
of the config (public search API, no login), the most starred first. Week gate and seen set: scripts/radar.py.

Config: {"type": "product_github", "topics": ["ai-agents", "task-management"], "min_stars": 50, "per_topic": 5,
         "fixtures": null}
Topics are generic words chosen by the person (never a customer or a repository of the team). One topic that fails is a
warning line; all of them failing breaks the source.
"""
import urllib.parse
from datetime import datetime, timedelta, timezone

import product_radar
import radar
from adapters import Fonte as Base

API = 'https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page={n}'


def collect(topics, terms, now, window_days=7, min_stars=50, per_topic=5, fixtures=None, errors=None):
    since = (now - timedelta(days=window_days)).date().isoformat()
    items, seen = [], set()
    for t in topics:
        url = API.format(q=urllib.parse.quote(f'topic:{t} created:>{since}', safe=':>'), n=max(1, min(20, per_topic)))
        try:
            d = radar.fetch_json(url, fixtures)
            if not isinstance(d, dict) or not isinstance(d.get('items'), list): raise ValueError('resposta sem "items"')
        except Exception as e:
            if errors is not None: errors.append(f'{t}: {type(e).__name__}: {str(e)[:100]}')
            continue
        for r in d['items']:
            name, stars = r.get('full_name') or '', int(r.get('stargazers_count') or 0)
            if not name or stars < min_stars or name.lower() in seen: continue
            seen.add(name.lower())
            summary = radar.clean(r.get('description'), 300)
            st = radar.match_stacks(name.replace('/', ' ').replace('-', ' ') + ' ' + summary, terms)
            items.append({'id': radar.item_id('gh', name.lower()), 'kind': 'github', 'title': name,
                          'url': r.get('html_url') or f'https://github.com/{name}', 'summary': summary, 'stacks': st,
                          'source': f'topic:{t}', 'stars': stars, 'score': 1 + min(4, stars // 200) + 2 * len(st)})
    return items


class Fonte(Base):
    tipo = 'product_github'
    conserto = 'conferir a rede até api.github.com e os "topics" da fonte product_github'

    def tick(self, s, desde, dry):
        cfg = self.ctx.cfg if self.ctx else {}
        o, now, errors = product_radar.options(cfg), datetime.now(timezone.utc), []
        topics = [t for t in self.cfg.get('topics') or [] if isinstance(t, str) and t.strip()]
        if not topics: return None

        def items():
            got = collect(topics, o['terms'], now, int(o['window_days']), int(self.cfg.get('min_stars', 50) or 0),
                          int(self.cfg.get('per_topic') or 5), self.cfg.get('fixtures'), errors)
            if errors and len(errors) == len(topics): raise SystemExit('nenhum tópico respondeu: ' + '; '.join(errors)[:300])
            return got
        r = radar.gate_and_mark(self, s, items, dry, now, o, product_radar.FOLDER)
        if r is not None and errors: r['errors'] = errors
        return r

    def imprime(self, r):
        shown = radar.print_block(self, r, product_radar.HEADER)
        for e in (r or {}).get('errors') or []: print(f'AVISO (product_github): {e}'); shown = True
        return shown
