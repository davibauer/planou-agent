"""The names of the agent plugin's behaviors (skills): old name -> current one.

PLN0295 gave the behaviors objective names (what they do). An old name keeps working for good: in "behaviors" and
"behavior_config" of an instance's config.json (schema.normalize; --validate warns), in an instance's own
behaviors/<old>/ folder (paths.behavior_file), in a list Planou stored before the rename (role_edit) and in the roles
the heartbeat declares (watch_core.planou.roles). Nothing here is written back to a config.json.
"""

BEHAVIOR_ALIASES = {
    'deploy-notice': 'batch-release',
    # PLN0295
    'planou-queue': 'task-queue',
    'dev-worker': 'delegate-to-worker',
    'refinement': 'backlog-refinement',
    'retro': 'retrospective',
    'qa': 'acceptance-testing',
    'product': 'backlog-planning',
    'process-coach': 'flow-metrics',
    'suggestions': 'suggestion-intake',
    'security': 'security-scan',
    'push-alert': 'mobile-alerts',
    'daily-report': 'daily-standup',
    'recordings': 'meeting-recordings',
    'work-watch': 'work-triage',
    'job-scout': 'job-search',
    'travel-agent': 'flight-price-watch',
}


def behavior_name(name):
    """The current name of a behavior: an old one read as the new one; anything else (a str) as it is."""
    return BEHAVIOR_ALIASES.get(name, name) if isinstance(name, str) else name


def current(names):
    """A list of behavior names with the old ones read as the new ones, without repeats (first position kept)."""
    return list(dict.fromkeys(behavior_name(n) for n in names or [] if isinstance(n, str)))


def old_names(name):
    """The old names of a behavior, in the table's order."""
    return [o for o, n in BEHAVIOR_ALIASES.items() if n == name]


def options(cfg, name):
    """The options of a behavior in a config read as it is on disk (not through schema.normalize): those under an old
    name, with the new name's own keys winning (schema.normalize's rule). {} when there are none."""
    bc = cfg.get('behavior_config') if isinstance(cfg, dict) else None
    if not isinstance(bc, dict): return {}
    out = {}
    for n in old_names(name) + [name]:
        if isinstance(bc.get(n), dict): out.update(bc[n])
    return out
