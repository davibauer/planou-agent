"""watch_core: code shared by the agent plugins of this marketplace (work-watch, job-scout).

Modules: config (where the user's files live), agents (canonical agent names, LEGACY_AGENT_NAMES, canonical_agent),
notion_decisions and notion_page (Notion REST for the daily page), daily (the == DAILY flow), tasks (task list on the
daily page), lessons (daily lessons), rules_dedupe (rule drift between agents), recordings (OBS recordings x calendar),
migrate_agent_names (one-off migration of the user's data to the agent names). Source of truth: shared/watch_core at
the repo root; each plugin carries a generated copy (tools/sync_shared.py), so an installed plugin needs nothing else
on disk.
"""
from .agents import LEGACY_AGENT_NAMES, canonical_agent, legacy_agent_names, same_agent, agent_of   # noqa: F401
