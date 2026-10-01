# agent

Claude Code plugin for the team's agents: one plugin, one instance per agent. What changes from one agent to another
(sources, hooks, behaviors, tools, autonomy) lives in the instance config. Design and migration plan (Portuguese):
[docs/agent-plugin-design.md](../../docs/agent-plugin-design.md).

Status (0.26.0): core with the runner, the Planou queue and conversation; a dev instance of any project from the config (`repos` with tests, release, done criterion and `public`; `--brief` builds the worker request); development (`dev-worker`, `batch-release`), review (`code-review`), QA (`qa`), product and suggestion behaviors; release, deploy and health hooks. work-watch, job-scout (since 0.59.0, `job-scout` behavior and `/job-scout` shortcut; migrate with `agent.py job-scout --migrate`) and travel-agent (since 0.60.0, `travel-agent` behavior and `/travel-agent` shortcut; `agent.py travel-agent --migrate`) run as instances of this plugin.

Since 0.62.0 the job-scout session does not judge jobs itself: each tick's job blocks, the reputation and hiring
process research and the Gmail step go to one disposable subagent per tick, `job-judge` (`agents/job-judge.md`, sonnet
model) or `general-purpose` with sonnet while the plugin is not installed from the marketplace. Its rules are in
`behaviors/job-scout/JUDGE.md`; `judge.py --record` stores the batch of verdicts with a single `scout.py --job` and
returns one line per job, with the detail in `data/judgments/<id>.md`.

- Instance folder: `~/.config/agent/<instance>/` (`config/config.json`, `config/instructions.md`, `secrets/planou.env`,
  optional `behaviors/` and `adapters/`); without it, the agent's legacy folder.
- `scripts/agent.py <instance>`: a tick, `--validate`, `--load` (the role by layer in precedence order: rules, instructions, skills with on_demand ones as an index line, then memory, `data/handoff.md` when written in the last 36 h), `--status`, `--brief [repo] [--role <behavior>] [--model sonnet|default] [--files "<files>"]` (the worker brief of a repo in `repos`, with only what the role allows, plus the repo map, the likely files and the model the session picks), `--evals` (dry run of the reference cases of the instance's roles), `--docs` (the manifest of Planou's Papel tab, as the heartbeat sends it), `--pending`, `--resolve`.
- Permission per role: each `BEHAVIOR.md` ends with a `permissions` block declaring the tools (`kind:name`) and actions the role uses (`scripts/permissions.py`). `--validate` warns (never an error) when an `autonomy.can` phrase grants an action no enabled behavior declares, matches no known action, or when a config tool is used by no enabled behavior. `--brief` keeps in PODE only the `can` phrases of the worker's role (`dev-worker` by default, `--role batch-release` for the integrator).
- Name and sentence of each behavior: every plugin `BEHAVIOR.md` starts with a frontmatter (`title`, the name in Portuguese, up to 80; `summary`, one sentence of what it does, up to 300; optional `layer` and `kind`). The manifest sends `title` and `summary` in the catalog and in each behavior on (and `layer` and `kind` in the catalog) for the skill cards of Planou's Papel tab; the description is still the heading after the frontmatter. A Planou up to 0.64.0 refuses these fields: the runner then sends the manifest without them for a day and tries again (one line in `cache/planou/docs.log`).
- Reference cases: `behaviors/<name>/evals/cases.json`, 5 to 10 Planou tasks or generic examples per role with the actions, the expected decision (`faz`, `pergunta`, `recusa`) and the expected result. `scripts/evals.py` runs them dry (no model): each case is checked against the role's declaration and the task's autonomy. `--prompt <case>` and `--grade <case> <answer>` cover a real run by hand. The plugin tests run them, so CI catches a role change that breaks its cases; every role has cases, and a new behavior without its `evals/cases.json` fails the tests. Planou's Papel tab shows them: the heavy tick sends, in the heartbeat `docs`, the files the session reads (instructions.md and CONTEXT.md editable with their content, plus the catalog of behaviors) and each behavior's `--evals --json` result, when it changes or once a day (Planou 0.49 or later). An edit made there (`agent_docs_changed`: instructions, context, the behaviors on, the behaviors' options as `behavior_config.json`, whose schema goes in each catalog entry as `options` (`{key: {type, choices?, label?}}`: `text`, `int`, `bool`, `choice`, and `command` for command and path options, read only in the form; `num`, `strs` and `ints` keep the plugin's name, which Planou ignores; Planou's limits applied; local behaviors go without `options`; needs Planou 0.61.0 or later, an older one refuses the whole manifest with 422 `invalid_docs`); an option run as a command on the machine, type `command` in the schema (`qa` setup, env_up, env_down and env_url; `batch-release` deploy_cmd, test_lock and deploy_lock; `code-review` terms_file), shows as "(só no computador)" and an edit that changes, adds or removes one is refused with "opção de comando: edite no computador", while an edit of the other options applies with the command ones untouched; the same goes for path options, type `path` (`qa` node_dir, which goes in NODE_PATH; `batch-release` e2e_marker, fragments and deploy_log, files the integrator overwrites, deletes or appends to), refused with "opção de caminho: edite no computador", and for the options of a behavior local to the instance (`behaviors/<name>/`, also a local copy of a plugin behavior), which declares no schema, refused with "comportamento local: edite no computador") is applied by the heavy tick (`scripts/role_edit.py`: sha256 and base check, `config-snapshot --wait`, exact write, `--validate` with rollback), answered on `POST /v1/agent/docs/ack` and announced to the session as `== PAPEL MUDOU` (what to read again) or `== PAPEL RECUSADO` (why); a BEHAVIOR.md is never edited this way. Work-watch instances (Planou through `planou_tick.py`) send the manifest and apply the edits too; under confidentiality "minimum" (their default) no content leaves the machine, the reference cases go without the task text and the why, and only the list of behaviors is edited.
- Dev instance of any project: `repos` entries carry `tests` (`filtered`, `full`), `release` (`batch`, `pr`, `ci`: the CI merges, versions and tags the PR labeled `automerge`, the worker does not wait; `none`), `fragments` (where the worker writes its changelog text), `done`, `base`, `shared_rules`, `map` (the repo's short map, relative to `path`; default `docs/MAP.md` when it exists) and `public` (a public repository, read by `code-review` and `--brief`; `--public-repos` lists them), and `batch-release` takes `deploy_cmd`; template in `skills/agent/config-example/dev/`, step by step (pt-BR) in `docs/dev-template.md`.
- `scripts/runner.sh <instance>` (and `stop`): the one runner, heavy tick plus the Planou light loop (long poll), the
  session conversation and the agent's queue.
- Workers in progress (Planou's Fila tab, "Workers agora"): right after starting a worker the session runs
  `$PL fila worker-start <PID> [--task PID2 ...] --role dev|integrator|other --label '<one line>'`, which opens it on
  Planou (`POST /v1/agent/workers`) and prints its key (kept in `cache/planou/workers.json`); the delivery
  (`$PL fila worker <PID> ... --key <key>`, or the key open locally for the task and role) closes it; `$PL worker end
  <key> --result feito|parcial|falhou` closes a worker without a task or of role `other`; `$PL worker ping <key>` is the
  sign of life (PATCH). The heavy tick of a new session closes on Planou the workers a session that died without
  `session_closed` left open (`-- WORKER INTERROMPIDO`).
- Config schema 1 in English (`scripts/schema.py`), work-watch's Portuguese keys accepted as aliases.
- Behaviors: `planou-queue`, `dev-worker`, `batch-release` (includes the deploy notice; the old name `deploy-notice` is still accepted), `suggestions`, `work-watch`, `daily-report`, `recordings`, `push-alert`, `code-review` (independent review before release: reads the PR the dev's handoff brought, sends the task back to the dev with `fila ajuste`; `scripts/review_check.py` for the mechanical checks), `qa` (tests the delivery as a user before release: a throwaway environment of the PR branch with `scripts/qa_env.py`, an ephemeral Playwright script with `scripts/qa_kit.cjs` for axe, widths and touch targets; approves with a handoff or sends back with `fila ajuste`; never writes code; template in `docs/qa.md`), `tech-radar` (the `tech-scout` instance: four weekly sources, dependency releases of the configured repositories, official blogs and chosen YouTube channels by RSS, Hacker News and GitHub Trending filtered by the stacks; the week becomes Backlog suggestions of a radar project through `scripts/radar.py` and `scripts/backlog.py`, and a task in the "Radar técnico" column gets up to 3 suggestions in its note; generic outside lookups only, `--dry --fixtures` runs offline; never writes code; template in `docs/tech-radar.md`), `product-radar` (the `product-scout` instance, sibling of `tech-radar` for the product instead of the stack: public changelogs and RSS of task apps, of agent products, Product Hunt and Show HN (`product_feeds`) and new repositories under GitHub topics (`product_github`), once a week; `scripts/product_radar.py` shows the candidates next to what the product already has (project tasks via `/v1/agent/projects/{key}/flow`, README and CHANGELOG) and turns the session's ideas into at most 3 Backlog tasks a week, each with the public link, why, a 3-line sketch, size P/M/G and risk, dropping what already exists or was proposed before; only the idea, never another product's brand, text, look or code; template in `docs/product-radar.md`), `process-coach` (development coordinator: measures the project's flow through Planou, lead time per state, the week's bottleneck with real cases and at most 3 proposals with the metric each should move, then checks their effect the next week; parameter and role changes go to Planou as proposals the person approves with one click (`--propose`, Planou 0.53.0), a refused one falls back to a `Decidir:` task; `scripts/process_report.py`; never writes code; template in `docs/process-coach.md`) (`skills/agent/behaviors/<name>/BEHAVIOR.md`).
- The `team` launcher: `scripts/team.sh <agent> [new]` opens or resumes the agent's session (one per agent, by two locks); `~/.local/bin/team` is a stub that runs it from the plugin copy in use. A shell it leaves behind never keeps the locks, and a lock held by a leftover idle shell of an older launcher is explained by `scripts/team_lock.py` (pid, terminal and how to release it) instead of "already running"; `team_status.py` shows it as an idle-shell lock, not an open session.
- A Windows drive that stops answering (`/mnt/<letter>`) puts any stat or open on it in state D for good. The tick checks the paths of each source that live on a drive first, in a child process with a deadline (`scripts/drive_probe.py`, 5 s): a dead drive gives `FONTE QUEBRADA (<source>): drive <X> sem resposta` and the tick goes on. `team_status.py` reads the lock holders by the name of the `/proc/<pid>/fd` link, never following it, and skips a runner folder on a dead drive.
- Grouped blocks (PLN0247): a behavior may declare in `behaviors/<name>/wake.json` (and an instance add, with `"wake"` in its config.json) blocks that never wake the session alone. The runner moves them out of the heavy tick into `data/retido.json` (`hold`: every copy; `latest`: only the newest, dropped when the tick stops printing it); the next wake of any kind puts them at the top of tick.out as content and only then deletes the file. The buffer wakes by itself on an `urgent` line, when its oldest block passes `hold_max_min` (180), past `hold_max_kb` (64; nothing is dropped) and on a session's first heavy tick. `broken_repeat_min` sets when a known FONTE QUEBRADA wakes again (default 60; 0 = only a new one). job-scout groups the jobs that passed the filter, the reputation and hiring-process nags, `== GMAIL ATRASADO` and the Notion sync line; TOP APPLICANT wakes at once.
- A folder may answer while a file inside it does not (PLN0245). Every read the sources make on `/mnt/<letter>` goes through `watch_core/slowfs.py`, in a child process with a deadline (20 s, `AGENT_FS_READ_S`): `FONTE QUEBRADA (<source>): drive <X> sem resposta (lendo <file>)`. The `gravacoes` source reads the video and archive folders by metadata only, opens (`ffprobe`, once), transcribes or archives a video only when its size and mtime did not change since the previous tick and the mtime is older than 60 s (`ATA_ESTAVEL_S`), and copies it to the archive in a detached process (`watch_core/archive_job.py`).
- Local provisioner: `scripts/provisioner.py`, a systemd user service (unit and installer in `skills/agent/provisioner/`), follows Planou's `/v1/provisioner` (over loopback, or over https from a computer connected with `provisioner.py pair` (the code asked hidden or read from stdin, never in the arguments), which trades the code of Settings › Computers for the computer's credential, written 0600 and never shown, and sets `base_url`) and creates the agents asked for on the Time screen: `~/.config/agent/<name>/` from the dev template (test mode unless the person chose live), the key picked up once into `secrets/planou.env` (0600, never logged or passed to a process), `--validate`, `config-snapshot`, and an entry in `~/.config/team/agents.json` for the Team Terminals extension; it also pauses, resumes and removes, and reports each step. The agents that already ran on the machine (agents.json and the plugin's instances) are adopted through an inventory: it reports each runner (up, stopped, crashed) and follows the screen's turn on and turn off, never creating, changing the config of or deleting one of them. An employee it created that left this computer (moved to another one, or taken by another after this one was revoked) gets its runner stopped and its instance archived in `~/.config/agent-provisioner/departed/`, only on a valid answer of Planou's list. It runs from the plugin copy in use and updates itself: between passes, when the canary (`rollout.json`) released another version (or the copy in use changed, without a canary), it exits with 75 and systemd (`Restart=always`) starts it on the released version; a refused one never runs. The unit stays the same across versions; when a new one differs, the provisioner rewrites it and asks for `daemon-reload`. Setup (pt-BR) in `docs/provisioner.md`.
- Shortcut skill `work-watch`: `/work-watch <x>` is `/agent work-watch-<x>` (`pendentes` -> `--pending`, `resolver pN` -> `--resolve pN`, other options pass as is).
- Hooks: `release_due` (a batch release is due; the queue is driven by `agent.py <instance> --release-queue`) and
  `deploy_log` (one wake per new line of the deploy log; a deploy of the release the session closed does not wake). With Planou on, the release rules come from the project's
  Release section (local options are the fallback), the queue is mirrored there and each deploy goes to the project's
  version history. `suggestions` (planou-dev) turns the other agents' suggestions (`watch_core.planou ... sugestao`,
  an inbox file, 3 per agent per day, client data refused) into backlog tasks of the Planou project reported by the
  agent that suggested them, with "+1" for the same one again. `health` runs read-only checks (http, command, disk,
  file freshness, runners, broken sources); a check broken for `after` ticks in a row opens a backlog task with the
  evidence, its recovery is noted on the same task, and a short report comes once a day (`--health` checks now).
  `alertas_azure` accepts `nao_acordar` (alert rule names that never wake the session: their lines go to
  `== ALERTAS SEM ACORDAR (n)`, to `avisos.log` and to the next wake's `== SEM ACAO`), `nao_acordar_junto` (hooks whose
  block goes quiet with them, when every alert of the tick is on the list) and `ambiente` (a pod in trouble there in
  the same tick lifts the silence).
  `security` (the security agent) reuses it with read-only scans: dependencies with a known vulnerability (npm audit,
  dotnet list package --vulnerable, pip-audit when installed), secrets in a repository or a log (file, line and type,
  masked: the secret never leaves), permissions of the secret files (lstat only) and the agents' autonomy against their
  roles. A finding opens a backlog task with the evidence; nothing is fixed, rotated or deleted (`--security` scans now).
- Questions to the user become Planou asks with options and the recommended one marked (`pergunta --recommended`).
  A decision whose recommended option fits the autonomy (the config's and the task's `can`, the stricter one wins) does
  not wait: the agent follows it, says so in one line and records it with `pergunta --auto` (a low alert "Decidi:
  <question> -> <option>", nothing left open in Precisa de você). What is in `ask_first` or `never` (messages to people,
  unapproved merge, production, deleting data or secrets, creating accounts, opening the browser) and what needs the
  user's own action still become asks.
- Tests: `python3 -m unittest discover -s plugins/agent/tests`.

## One-command install

On a new computer, one command installs the plugin, the Team Terminals VS Code extension and the local provisioner as a
user service, and last asks for Planou's pairing code (Settings › Computers › **Connect this computer**). No new account
and no secret on the terminal: the code is typed hidden and goes to `provisioner.py pair` on stdin; the credential goes
from Planou straight to a 0600 file.

Linux, WSL and macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/davibauer/planou-agent/main/install.sh | sh
```

Windows (PowerShell; the agents run in WSL, which must be installed with a distribution and your Linux user):

```powershell
irm https://raw.githubusercontent.com/davibauer/planou-agent/main/install.ps1 | iex
```

| What | Linux and WSL | macOS | Windows |
|---|---|---|---|
| agent plugin | a copy of this repository in `~/.local/share/planou/claude-plugins` (git, or the `.tar.gz` without git), the skills `~/.claude/skills/agent` and `work-watch` pointing at it, `team` in `~/.local/bin` and an empty `~/.config/team/agents.json` | same | same, inside WSL |
| Team Terminals extension | `extensions/vscode-team-terminals/dist/team-terminals.vsix` through `code --install-extension`; it then updates itself from the same copy | same | through WSL's `code`, which installs it in the VS Code of the WSL windows |
| provisioner | `systemd --user` (`agent-provisioner.service`) | `launchd` (`~/Library/LaunchAgents/app.planou.agent-provisioner.plist`, log in `~/Library/Logs/agent-provisioner.log`) | a scheduled task of the user, at logon, that runs the provisioner in WSL (`provisioner/wsl-provisioner.sh`) and keeps WSL up; with systemd on in WSL, the unit runs it and the task only keeps WSL up |

- Running it again updates everything: `git pull --ff-only` on the copy, the extension and the service. A computer
  already connected keeps its credential and is not asked for a code; the service restarts on the new version.
- What is not the installer's stays as it is: an `agent` skill already pointing at another copy, an existing `team`
  and an existing `agents.json`.
- The service is turned on only after pairing. Without a terminal (or with an empty Enter) the installer prints the
  command to connect later: `python3 <copy>/plugins/agent/skills/agent/scripts/provisioner.py pair --base-url <Planou>`
  (no code in the command: it asks, hidden).
- Options: `curl ... | sh -s -- --base-url URL` (default `https://app.planou.com`), `--no-vscode`, `--no-pair`,
  `--service none`, `--dir DIR`, `--ref REF`; on Windows `-BaseUrl`, `-Distro`, `-NoVSCode`, `-NoPair` (with
  `& ([scriptblock]::Create((irm <url>))) -BaseUrl URL`). The full list is at the top of each script.
- Requirements: `python3` 3.8 or newer and `git` or `curl`; Claude Code (`claude`) for the agents to run. On WSL
  without systemd, set `[boot] systemd=true` in `/etc/wsl.conf` or install with `install.ps1` from Windows.
- On macOS the provisioner runs, but its runner-alive check reads `/proc`, which macOS lacks: the runner state on the
  Time screen is not reliable there.

## Retired plugins (E6)

The job-scout and travel-agent plugins left this repository and the marketplace in agent 0.62.1: both instances run on
this plugin (the `job-scout` and `travel-agent` behaviors, with their whole engines under `behaviors/`), and the
`/job-scout` and `/travel-agent` shortcuts are skills of this plugin. Their history stays in git; the last published
versions are the tags `job-scout--v0.42.23` and `travel-agent--v0.4.6`. `--migrate`/`--undo` and the runner's refusal to
start next to a live old runner stay, for an old install that has not migrated yet. The work-watch plugin left too (PLN0262):
the `work-inbox` (read Teams, Outlook and Slack on demand) and `meeting-minutes` (local meeting minutes on the GPU) skills
now live here, in `skills/work-inbox` and `skills/meeting-minutes` (the minutes research scripts in
`research/meeting-minutes/`), and the work-watch sources reach them by path inside the plugin. Its last published
version is the tag `work-watch--v0.43.24`; its `watch_core` copy and `shared/watch-core` left with it (`team` uses this
plugin's `watch_core` for every agent). On a linked install, `~/.claude/skills/work-inbox` and
`~/.claude/skills/meeting-minutes` point at `plugins/agent/skills/<skill>`.
