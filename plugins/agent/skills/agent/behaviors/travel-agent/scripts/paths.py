#!/usr/bin/env python3
"""All travel-agent paths in one place. No script builds a ~/.config path on its own.

The travel-agent behavior of the agent plugin (E5 of docs/agent-plugin-design.md). The instance is `travel-agent` (or
the name in $AGENT_INSTANCE, which agent.py sets); its folder is the agent plugin's: ~/.config/agent/travel-agent/ once
`agent.py travel-agent --migrate` moved it, the old ~/.config/travel-agent/ before that (the link left there after the
move keeps every old path working), and ~/.config/agent/travel-agent/ on a machine that has neither (setup.py).

<root>/
  config/    what the user decides        config.json (trips, points, alert rules), instructions.md,
                                          past_trips.json (what was paid on past trips, by category)
  data/      what the agent accumulates   state.json (alert marks, Gmail cursor, bonuses, award prices, quotes),
                                          prices.jsonl (every observation), google_alerts.jsonl (Google Flights
                                          alert e-mails: real price for the whole party)
  secrets/   keys (0700), never in backup google-service-account.json (spreadsheet), planou.env
  cache/     disposable                   venv/ (fast-flights), runner/

Backup: config/ and data/.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from watch_core import config as _wc          # noqa: E402

NAME = os.environ.get('AGENT_INSTANCE') or 'travel-agent'


def _root(name):
    """~/.config/agent/<name> when it exists or when the old folder does not; else the old ~/.config/<name>."""
    new, old = os.path.join(_wc.agent_base(), name), _wc.legacy_agent_root(name)
    return new if os.path.isdir(new) or not os.path.isdir(old) else old


ROOT = _root(NAME)
CONFIG_DIR, DATA_DIR, CACHE_DIR, SECRETS_DIR = (os.path.join(ROOT, d) for d in ('config', 'data', 'cache', 'secrets'))
GOOGLE_KEY = os.path.join(SECRETS_DIR, 'google-service-account.json')   # service account that writes the spreadsheet

CONFIG = os.path.join(CONFIG_DIR, 'config.json')
INSTRUCTIONS = os.path.join(CONFIG_DIR, 'instructions.md')
PAST_TRIPS = os.path.join(CONFIG_DIR, 'past_trips.json')
STATE = os.path.join(DATA_DIR, 'state.json')
PRICES = os.path.join(DATA_DIR, 'prices.jsonl')
GOOGLE_ALERTS = os.path.join(DATA_DIR, 'google_alerts.jsonl')   # real prices for the whole party (Google alerts, Expedia, manual)
FX = os.path.join(DATA_DIR, 'fx.jsonl')
HOTEL_PRICES = os.path.join(DATA_DIR, 'hotel_prices.jsonl')
DASHBOARD_DIR = os.path.join(CACHE_DIR, 'dashboard')
VENV = os.path.join(CACHE_DIR, 'venv')
VENV_PYTHON = os.path.join(VENV, 'bin', 'python')
RUNNER_DIR = os.path.join(CACHE_DIR, 'runner')


def mkdirs():
    for d in (CONFIG_DIR, DATA_DIR, CACHE_DIR): os.makedirs(d, exist_ok=True)
