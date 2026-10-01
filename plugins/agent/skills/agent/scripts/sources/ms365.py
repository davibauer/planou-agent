"""Helper (not an adapter): imports the Teams / Outlook clients (paths.WORK_INBOX: the work-inbox skill of this plugin)
with the instance profile fixed.

teams_pull / outlook_pull / calendar_outlook resolve the profile when imported (argv --profile, TEAMS_CHAT_PROFILE,
CALENDAR_PERFIL), so the profile is set before the first import and sys.argv is hidden from them. One profile per
process, which is what one instance per run means.
"""
import os, sys

import paths

TEAMS_DIR = paths.WORK_INBOX          # config "teams_chat_dir" overrides
PERFIL = None


def configura(perfil, teams_dir=None):
    global PERFIL, TEAMS_DIR
    if PERFIL and perfil and perfil != PERFIL: raise ValueError(f'dois perfis do work-inbox na mesma instancia: {PERFIL} e {perfil}')
    PERFIL = perfil or PERFIL
    if teams_dir: TEAMS_DIR = os.path.expanduser(teams_dir)
    if TEAMS_DIR not in sys.path: sys.path.insert(0, TEAMS_DIR)


def teams():
    """(teams_pull, outlook_pull) do perfil da instancia."""
    os.environ['TEAMS_CHAT_PROFILE'] = os.environ['WORK_INBOX_PROFILE'] = PERFIL
    argv, sys.argv = sys.argv, [sys.argv[0]]
    try:
        import teams_pull as tp, outlook_pull as op
    finally:
        sys.argv = argv
    return tp, op


def calendario():
    """calendar_outlook (work-inbox) do perfil da instancia."""
    os.environ['CALENDAR_PERFIL'] = PERFIL
    os.environ['TEAMS_CHAT_PROFILE'] = os.environ['WORK_INBOX_PROFILE'] = PERFIL
    argv, sys.argv = sys.argv, [sys.argv[0]]
    try:
        import calendar_outlook as kv
    finally:
        sys.argv = argv
    return kv
