"""What the monitor shows (PLN0421, prototype A "Caderno"): one pure function from the session state and the user's
noise settings to the screen, so the rules are tested without a browser and the panel only renders.

States: 'quiet' (current topic and the question worth asking now), 'alert' (one big card "for you" with 2 or 3 topics
to say), 'ended' (the notes saved for the minutes). The rest of the meeting (decisions, actions with owner and due,
what has no owner, open questions) is the drawer, always computed, opened by the user.

Noise control:
  threshold  'low' | 'mid' | 'high': the confidence a question to the user needs to become the big card. Confidence =
             the gate score (detect.py) plus the addressee model's word (yes = +2; no model, the cards call confirmed
             it = +1; the model failed = 0). Below the threshold it never interrupts; in mode 'all' it is listed in the
             drawer under "Talvez pra você".
  mode       'mine' (only what is mine: actions of the user or with no owner, no FYI) | 'all' (everything).
  dismissed  group ids the user closed: they never come back, not even after a reconnect.
"""

LEVELS = {'low': 2.0, 'mid': 3.0, 'high': 5.0}
DEFAULT_SETTINGS = {'threshold': 'mid', 'mode': 'mine'}
ADDR_BONUS = {'yes': 2.0, None: 1.0, 'fail': 0.0}
KINDS = ('topics', 'careful', 'ask', 'answer')      # the order on the card; 'answer' (the sentence) only on request


def settings(old=None, **new):
    """Merge a change into the settings, ignoring unknown values. -> a new dict."""
    s = dict(DEFAULT_SETTINGS, **(old or {}))
    if isinstance(new.get('threshold'), str) and new['threshold'] in LEVELS: s['threshold'] = new['threshold']
    if isinstance(new.get('mode'), str) and new['mode'] in ('mine', 'all'): s['mode'] = new['mode']
    return s


def confidence(group):
    return float(group.get('score') or 0.0) + ADDR_BONUS.get(group.get('addr'), 0.0)


def is_mine(owner, user_names=()):
    o = (owner or '').strip().lower()
    return not o or o in ('you', 'voce', 'você') or o in {n.lower() for n in user_names}


def view(state, prefs=None, dismissed=()):
    """state: the session snapshot (groups, cards, notes, focus, ended, paused, title, user, aliases).
    -> {'state', 'topic', 'ask', 'alert', 'drawer', 'settings', 'paused'}."""
    s = settings(prefs)
    need = LEVELS[s['threshold']]
    names = [state.get('user') or ''] + list(state.get('aliases') or [])
    cards = {}
    for c in state.get('cards') or []:
        cards.setdefault(c['g'], []).append(c)
    gone = set(dismissed)

    alert, maybe = None, []
    for g in reversed(state.get('groups') or []):
        if g.get('fyi') or g['id'] in gone or g['id'] not in cards: continue
        if confidence(g) >= need:
            if alert is None: alert = g
        else:
            maybe.append(g)
    if alert:
        own = sorted(cards[alert['id']], key=lambda c: KINDS.index(c['k']) if c['k'] in KINDS else 9)
        alert = {'id': alert['id'], 'who': alert.get('who') or '', 'q': alert.get('q') or '', 'conf': confidence(alert),
                 'topics': [x for c in own if c['k'] == 'topics' for x in split_topics(c['x'])][:3],
                 'cards': [c for c in own if c['k'] != 'topics']}
        if not alert['topics']:     # an older reply without TOPICS: its answer still shows, as the only topic
            alert['topics'] = [c['x'] for c in alert['cards'] if c['k'] == 'answer'][:1]
            alert['cards'] = [c for c in alert['cards'] if c['k'] != 'answer']
        alert['sources'] = sorted({x for c in own for x in c.get('chips') or []})

    notes = state.get('notes') or []
    actions = [n for n in notes if n['kind'] == 'action']
    if s['mode'] == 'mine': actions = [n for n in actions if is_mine(n.get('owner'), names)]
    drawer = {'decided': [n for n in notes if n['kind'] == 'decision'], 'actions': actions,
              'unowned': [n for n in actions if not (n.get('owner') or '').strip()],
              'open': [n for n in notes if n['kind'] == 'question'], 'maybe': [], 'fyi': []}
    if s['mode'] == 'all':
        drawer['maybe'] = [{'id': g['id'], 'who': g.get('who') or '', 'q': g.get('q') or ''} for g in maybe]
        drawer['fyi'] = [{'id': g['id'], 'who': g.get('who') or '', 'q': g.get('q') or ''}
                         for g in state.get('groups') or [] if g.get('fyi') and g['id'] not in gone]
    focus = state.get('focus') or {}
    st = 'ended' if state.get('ended') else ('alert' if alert else 'quiet')
    return {'state': st, 'topic': focus.get('topic') or '', 'ask': focus.get('ask') or None,
            'alert': alert if st == 'alert' else None, 'drawer': drawer, 'settings': s, 'paused': bool(state.get('paused'))}


def split_topics(text):
    return [t.strip(' .;') for t in (text or '').replace('|', ';').split(';') if t.strip(' .;')]
