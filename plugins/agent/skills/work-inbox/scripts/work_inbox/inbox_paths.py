"""Where work-inbox keeps the user's accounts (the code has none of it).

~/.config/work-inbox/            (WORK_INBOX_HOME moves it)
  profiles/<profile>/            config.json (account, tenant, connections, project paths; the Slack credential, 0600),
                                 chats.json and slack.json (caches), teams-web.json (Teams web session, optional)
  media/                         images downloaded from Teams messages

Before 25/09/2026 the plugin was called teams-chat and kept this in ~/.config/teams-chat: that folder is still used
while the new one does not exist (copy it over to migrate). Profile from the environment: WORK_INBOX_PROFILE
(TEAMS_CHAT_PROFILE, the old name, still works).
"""
import os

LEGACY = os.path.expanduser('~/.config/teams-chat')


def base():
    b = os.path.expanduser(os.environ.get('WORK_INBOX_HOME') or '~/.config/work-inbox')
    return LEGACY if not os.path.isdir(b) and not os.environ.get('WORK_INBOX_HOME') and os.path.isdir(LEGACY) else b


def env_profile():
    return os.environ.get('WORK_INBOX_PROFILE') or os.environ.get('TEAMS_CHAT_PROFILE')
