# watch_core

The agent plugin's library: Planou (the agent API client, the runner's light loop, the conversation), the daily page on
Notion, the task list on that page, the day's lessons, rule drift between agents and meeting recordings. An agent is a
permanent member of the team: an instance running in a loop in its own session (`planou-dev`, `work-watch-acme`...).

**Home and copies.** The source is `plugins/agent/skills/agent/scripts/watch_core/` (since agent 0.1.0; it was
`shared/watch-core/watch_core/` before). Its tests are in `plugins/agent/tests/`. The generated copies in the old plugins
and in `shared/watch-core/` left with them in E6: the `team` launcher (session_closed heartbeat) uses this one.

Agent folders: `config.agent_root(name)` is the one resolver (runners, planou, transcript, hooks):
`~/.config/agent/<name>/` when it exists, else `~/.config/work-watch/<x>/` for `work-watch-<x>` and `~/.config/<name>/`
for any other. Every agent under `~/.config/agent/` is run by the agent plugin's runner, so it declares the delivered
capabilities (`user_message`, `ceremony` (the retro; only an agent of this plugin), `task_comment` (the comment with @, only an agent of this plugin) and `task_queue` with `"planou": {"task_queue": true}`) and is live only with `"live": true`.

## Modules

| module | what it does |
|---|---|
| `config` | where the user's side lives (`~/.config/watch-core`, `WATCH_CORE_HOME` moves it); `python3 -m watch_core.config [--migrate]` |
| `agents` | canonical agent names: `canonical_agent`, `legacy_agent_names`, `same_agent`, `agent_of(cfg)` (config `agent`, old key `vigia`); the old -> canonical map comes from the user's config (`legacy_agent_names`, below), the generic `LEGACY_AGENT_NAMES` (`linkedin`, `travel`) and the work-watch instance folders (`acme` -> `work-watch-acme`) |
| `notion_decisions` | Notion REST reads: ticked option boxes and done items in a `## <CODE>` section |
| `notion_page` | the day page (`YYYY-MM-DD (day)` under the month page), one `## <CODE>` section per agent, published block by block |
| `daily` | the `== DAILY` flow: evidence of the day, live draft, manual actions, codes, publishing |
| `tasks` | the task checklist generated from an agent's state (`configura(code, **hooks)`, `tick`, `render_md`) |
| `lessons` | the day's lessons (`set`, `rules`, `proposals`, `all`) and the closing reminder |
| `rules_dedupe` | sentences repeated between the agents' instruction files (`python3 -m watch_core.rules_dedupe`) |
| `recordings` | OBS recordings x calendar: which meeting each video is, transcription queue, owners between agents (canonical names written, old ones read) |
| `planou` | agent side of the Planou agent API: `set_tools(list)`/`source_tools` (the Ferramentas tab: the list goes with the next heartbeat when it changed or hourly, texts cleaned with Planou's own secret patterns, never a credential value; a 422 is logged and not repeated), `attach(code, source_key, path|data)` (task attachments in multipart, only when the sha256 changed, 404 retried after the sync; the task may be a sync code, its pid or a queue task), `attach_file(task, path)`/`flush_attachments` (PLN0052: the files of a task, registered in `cache/planou/attach_files.json` and sent again when they change; the sync attaches the files a description cites and sends `[ver anexo: <name>]` instead of the path; `attachable()` is the local gate), `configure`/`configure_agent`, `sync(items)` (full-state upsert of the task list), `heartbeat`, `poll_block`/`poll_wait` + `cursor_commit` (the runner's light loop, a long poll with `poll --wait N` that exits 0 when Planou honoured the wait (header `Planou-Wait`), 3 when that Planou has no long poll (the runner sleeps its short interval) and 4 on failure (the runner backs off): answers become a `== PLANOU (n)` block, never acknowledged there; the person's messages from the Conversa tab (`user_message`) become `-- MENSAGEM do usuario pelo Planou` lines, acknowledged by `ack_delivered` only after the runner printed them, deduplicated by `message_id`; the heartbeat declares `capabilities`, `ceremony` only in the agent layout (Planou invites only those agents to a retro), `task_queue` only with `"task_queue": true` in the agent's `planou` block, `task_comment` only in the agent layout), the comment with @ of a person (`task_comment_mentioned`, Planou PLN0240: delivered like a message as `-- COMENTARIO <pid> de <author>`, answered with `post_comment()` for `comentario PID --text - [--reply-to ID]` and read with `task_comments()` for `comentario PID --ver`, POST/GET /agent/tasks/{id}/comments; 403 not_your_task when the task is not the agent's and nobody called it there), the agent's work queue (`task_queued_for_agent` delivered like a message as `-- FILA <pid>`, `task_dequeued` as `-- FILA SAIU`, `released: false` as `NA FILA, nao comecar` and `task_released` as `-- FILA LIBERADA` (capability `task_release`, declared with the queue), the payload kept in `cache/planou/queue.json`, `queue_view()` for `fila ver` (GET /agent/queue: `liberada` or `na fila`), `progress()` for started/in_review/done/blocked, a 409 `not_in_queue` means stop and a 409 `not_released` means wait), `request_changes()` for `fila ajuste PID --text T [--pr-url URL]` (a reviewer sends a task that came by a handoff back to the dev, POST /agent/tasks/{id}/request-changes; the reviewer's entry ends locally, `DEVOLVIDA`; 409 no_previous_owner prints `SEM DEV` and exits 5; on a task this same instance handed to its own review or QA column (PLN0281, Planou 0.76.0 PLN0286: `handed_off_by` names the agent itself, nothing kept locally; under confidentiality `minimum` the note never goes up, so the branch of a handoff to itself stays only in the instance's `data/planou_own_branches.json` (PLN0316) and comes back in the queue line, `fila ver`, `DEVOLVIDA` and the rework) it answers 200 like between agents, `DEVOLVIDA` back to this agent's dev column, and the `-- AJUSTE PEDIDO` rework is delivered with `fila handoff`, so the review goes to a new worker again; the dev's `-- AJUSTE PEDIDO <PID> (revisor <name>)` says who asked, and a queue line carries the handoff's `pr_url`), `worker_delivery()` for `fila worker` (one worker that came back: role, tokens, steps, duration, result; POST /agent/tasks/{id}/deliveries, key derived from the values so a repeat is unchanged, an empty 404 is an older Planou: a warning, exit 0), `waiting` ("Aguardando por" of waiting/blocked tasks, `who` only from the `title` level up), `apply_events` (heavy tick: the person's changes go through the agent's own "box ticked" handler and answers through `on_ask`, then are acknowledged), asks (`request_decision`, `ask_question` (a direct question from the session: Sim/Não or its options, always with a free answer), `request_approval`, `submit_draft`, `raise_alert`, `cancel_ask`, `report_result`), `reconcile_asks` (asks that follow the agent state: opened, withdrawn, never reopened once answered), edits through `on_change` and the current-task marker; `status` also shows what the next heartbeat declares (capabilities, tools and link rules saved) and, with the queue on, the limit "Ao mesmo tempo" (`wip`/`busy`); CLI `python3 -m watch_core.planou --agent X key set\|status\|active\|heartbeat\|poll\|cursor-commit\|ack-delivered\|draft\|approval\|decision\|pergunta\|alert\|tarefa\|fila\|comentario ...` |
| `fileio` | atomic writes and the lock of the state files: `write`/`write_json` (a unique temporary file in the same directory, then `os.replace`; the old file's mode is kept) and `locked(path)` (exclusive `flock` on `<path>.lock`, reentrant in the same thread). The runner and the CLI of the same agent write `cache/planou/` at the same time: `queue.json` and `state.json` (with `ready.json` and `sent.json`) are loaded, changed and saved under their lock, never across an HTTP call except `apply_events`, whose `ack` runs inside it; state.json is taken before queue.json |
| `turns` | end-of-turn cost hook (`python3 -m watch_core.turns hook`, Stop hook of Claude Code): turns of the session transcript with raw usage per model, class and task in progress, sent to Planou; never fails |
| `migrate_agent_names` | one-off migration of the user's data to the agent names (`python3 -m watch_core.migrate_agent_names [--apply]`; dry-run by default, `*.bak-agent-names` backups, idempotent) |

Commands and flags are in English; the Portuguese ones of the first version keep working as aliases:

- daily: `evidence`, `sections`, `codes`, `page <id>`, `seen`, `md "..."`, `publish [file]`, `set "..."`
- actions: `add "text" bucket=next|waiting|decision|blocker who=Name`, `done N`, `list`
- tasks: `--today`, `--due`, `--title`, `--detail`, `--options`, `--check`, `--merge`, `--link`, `--top`, `--id`, `--dry`
- notion_page: `python3 -m watch_core.notion_page day|section|publish ...`

## The user's side (`~/.config/watch-core`)

```
config.json   {"notion": {"dailies_page": "<id>", "month_page": "<id>", "last_sections": ["LKD"]},
               "recordings": {"videos_dir": "...", "archive_dir": "...", "minutes_skill": "...", "user_aliases": [...]},
               "rules_dedupe": {"files": {"name": "path"}, "shared_sources": [["a", "b", "why"]]},
               "legacy_agent_names": {"acmecorp": "work-watch-acme", "globex": "work-watch-globex"},
               "planou": {"enabled": true, "base_url": "https://app.planou.com/v1"}}
data/         lessons.md (`## dd/mm/yyyy · <agent>` sections), rules.md, recording_owners.json
secrets/      notion.env (NOTION_TOKEN=... of an internal Notion integration shared with the Dailies page), 0600
```

`notion.last_sections` keeps those sections at the bottom of the day page (a new section of any other code goes before
them). A section configured with `speech=False` (tasks only, no spoken text and no "Yesterday") never creates the day
page; it joins the page the other agents created.

`legacy_agent_names` maps an old agent name (as written in lessons.md, recording_owners.json or an instance config
before 0.19.0 of work-watch) to its canonical name. It is only needed for names the code cannot guess: a short name
that is not the instance folder (`acmecorp` for the instance `acme`). The code knows the generic renames of the plugins
(`linkedin` -> `job-scout`, `travel` -> `travel-agent`) and derives `work-watch-X` for any existing instance folder
`~/.config/work-watch/X/`. The user's entries win over both. Missing or invalid config: only the generic map and the
derivation. Client names belong here, never in the code of this public repository.

`planou` turns the Planou client on for every agent; each agent still needs its own `"planou"` block in its config
(project prefix, confidentiality, `publish`) and its key in `<agent folder>/secrets/planou.env` (0600, written by
`python3 -m watch_core.planou --agent <agent> key set`, which reads the key from stdin). Without any of these the module
does nothing. The confidentiality `minimum` sends a neutral title (origin and id) instead of the task text.

## Tests

`python3 -m unittest discover -s plugins/agent/tests` (standard library only, no network).
