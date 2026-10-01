"""Agent names in the shared history (lessons.md, recording_owners.json, config "agent").

An agent is a permanent member of the team: a plugin running in a loop in its own session (work-watch-acme,
job-scout, travel-agent...). Until 0.19.0 of work-watch they were called "vigias" and wrote short names in the shared
history ('acme', 'acmecorp', 'linkedin'). The canonical name is written from now on; the old one is still read.

Where the old -> canonical map comes from (first match wins):
  1. the user's watch-core config (config.json, see watch_core.config), key "legacy_agent_names":
         {"legacy_agent_names": {"acmecorp": "work-watch-acme"}}
     (names of the user's clients stay on the machine, never in this code);
  2. LEGACY_AGENT_NAMES below: only the generic, non personal renames of the plugins themselves;
  3. derivation: a short name X that is an existing work-watch instance (~/.config/work-watch/X/) -> work-watch-X.

    canonical_agent('acme')                     -> 'work-watch-acme'     (instance folder acme exists)
    canonical_agent('acmecorp')                 -> 'work-watch-acme'     (user config)
    legacy_agent_names('work-watch-acme')       -> ['acme', 'acmecorp']
    same_agent('linkedin', 'job-scout')         -> True

Everything is read lazily and tolerantly: a missing or invalid config leaves only the generic map (and derivation).
"""
import os

from . import config

LEGACY_AGENT_NAMES = {
    'linkedin': 'job-scout',
    'travel': 'travel-agent',
}

WORK_WATCH_PREFIX = 'work-watch-'


def _work_watch_base():
    """Folder of the work-watch instances (same place work-watch's paths.BASE uses), resolved at call time."""
    return os.path.expanduser('~/.config/work-watch')


def _user_map():
    """"legacy_agent_names" of the user's config, only string -> string pairs; {} when missing or invalid."""
    try: m = config.get('legacy_agent_names') or {}
    except Exception: return {}
    if not isinstance(m, dict): return {}
    return {str(k).strip(): str(v).strip() for k, v in m.items()
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}


def _instances():
    """Names of the existing work-watch instances ([] when there is none)."""
    base = _work_watch_base()
    try: return sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
    except OSError: return []


def _is_instance(name):
    return bool(name) and '/' not in name and not name.startswith('.') and not name.startswith(WORK_WATCH_PREFIX) \
        and os.path.isdir(os.path.join(_work_watch_base(), name))


def legacy_map():
    """The full old -> canonical map in effect now (user config, generic map, instance folders)."""
    out = {n: WORK_WATCH_PREFIX + n for n in _instances() if _is_instance(n)}
    out.update(LEGACY_AGENT_NAMES)
    out.update(_user_map())
    return out


def canonical_agent(name):
    """The canonical agent name for an old (or already canonical) one; unknown names come back unchanged."""
    if not name: return name
    n = str(name).strip()
    u = _user_map()
    if n in u: return u[n]
    if n in LEGACY_AGENT_NAMES: return LEGACY_AGENT_NAMES[n]
    if _is_instance(n): return WORK_WATCH_PREFIX + n
    return n


def legacy_agent_names(name):
    """The old names that map to this agent (to find data written before the rename)."""
    c = canonical_agent(name)
    return [old for old, new in legacy_map().items() if new == c and old != c and canonical_agent(old) == c]


def same_agent(a, b):
    return bool(a) and bool(b) and canonical_agent(a) == canonical_agent(b)


def agent_of(cfg, default=None):
    """Agent name of an instance config: "agent" (new) or "vigia" (up to work-watch 0.18), canonical; else default."""
    return canonical_agent((cfg or {}).get('agent') or (cfg or {}).get('vigia') or default)
