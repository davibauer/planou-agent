"""Planou client: an agent reports its tasks to Planou and learns what the person did there.

Planou (app.planou.com) is the to-do app where the team's agents are employees. This module is the agent side of the
agent API (/v1): a small REST client, standard library only, that runs inside the heavy tick without the model.

  sync(items)        full-state upsert of the agent's task list (the same list that renders the Notion section);
                     whatever is missing from a batch is never deleted on the server
  heartbeat(phase)   sign of life (poll, tick, woke, working, turn_end, session_open, session_closed), with the session
                     id and its Remote Control link when the runner found them (watch_core.transcript)
  poll()             events waiting for this agent (the person completed, reopened, changed, deleted, restored or
                     assigned a task, wrote to
                     the agent in the Conversa tab, handed a task to the agent's queue)
  progress()         the agent's work on a task of its queue: started, in_review (draft PR), done, blocked
  apply_events()     applies those events to the agent state through a callback, then acknowledges them; an event with
                     `changed` applies only those fields, and a local value that did not go up yet wins (resent)
  project_states()   the states of a project (GET /agent/projects/{key}/states), cached for an hour: a task may carry the
                     item's `project_state` (a state name of its project) next to `state` (its category)
  autonomy()         the project's autonomy (autonomous, semi_autonomous, manual), from the same cached answer

Ready or backlog (Planou 0.25.0): a new task without `ready_reason` is born in backlog. payload() sends the reason only
when the item mirrors something already happening at the source (the item's `ready_reason`, set by the source: a PR
waiting for review, a card in progress, a meeting booked...), when it has a due date, is in the plan of the day or waits
on someone, or when the agent marked it ready (`pronta CODE "motivo"`, cache/planou/ready.json: the user asked for it,
within the autonomy, nothing to decide, complete data). Under "minimum" the reason is a neutral one (no names, no
subject). A manual project never gets a reason. A task Planou keeps in backlog (the sync said so) goes without `state`
from then on, so the tick does not ask again on every run; in an autonomous project a reason takes it out. Warnings and
reasons of the sync go to cache/planou/warnings.log once per task, text and day. `pronta` on a task this agent already
synced also sends the mark right away (ready_now, a one-task sync): an instance with no sources has no full sync. On a
task the person created it marks nothing (the sync never touches those). An open decision goes up titled "Decidir: ..."
(also under minimum), Planou's mark of a person's decision: it never takes a slot nor leaves the backlog by itself.

Where things live (the agent passes its own root, e.g. ~/.config/job-scout):

  ~/.config/watch-core/config.json   "planou": {"enabled": true, "base_url": "https://app.planou.com/v1"}
  <agent config>                     "planou": {"project": "CAR", "confidentiality": "title", "publish": true,
                                                "drafts_in_planou": true}
  <root>/secrets/planou.env          PLANOU_AGENT_KEY=pl_ag_...   (0600; `key set` writes it from stdin)
  <root>/cache/planou/               state.json (pid, version and what was last sent per task code), health.json
                                     (last failure), open_asks.json (asks the tick keeps open, by the agent's ref)

Confidentiality (spec 9.6), applied here before anything leaves the machine: "minimum" sends a neutral title
(origin + id), "title" the short title, "detail" the short title plus the context of the asks. The draft text goes
up only with "drafts_in_planou": true (default true for job-scout, false for work-watch). A work-watch instance
without "live": true in its config is in test mode: nothing is sent.

Missing config or key means the module does nothing and never breaks the tick. A failure prints one
`AVISO (planou): ...` line; failures for more than 60 minutes print `FONTE QUEBRADA (planou)` instead, the line the
runners already know.

Light loop (spec 6.2): between heavy ticks the runner calls `poll --wait 50 --next-heavy EPOCH --poll-s 90` in a
continuous loop. With --wait the server holds the request until an event for this agent comes (a person's message right
when its 5 s undo window closes) or the time is up, so a message reaches the session in seconds; the call exits 0 when
the server honoured the wait (answer header Planou-Wait; poll again right away), 3 when it did not (a Planou without long
polling: the runner sleeps its short interval, 90 s, as before) and 4 on failure (the runner backs off, never wakes the
session). Without --wait it is the old short poll, exit always 0 (runners up to 0.26 call it that way). It
prints a `== PLANOU (n)` block only for answers and hand-overs never printed before, keeps the events in
cache/planou/inbox.json and never acknowledges: the heavy tick applies them to the state and acknowledges
(apply_events). `cursor-commit` marks the printed block as delivered once it is in tick.out.

Messages from the person (spec 6.4, event `user_message`): the heartbeat declares `"capabilities": ["user_message"]`
(CAPABILITIES, from the EVENTS registry). Only the light loop delivers them: `-- MENSAGEM do usuario pelo Planou (HH:MM):
<text>` in the `== PLANOU (n)` block (continuation lines indented), which wakes the session like a line typed in the
terminal. `cursor-commit` records the message_id as delivered (cache/planou/messages.json) and `ack-delivered`, which the
runner calls only after printing tick.out, acknowledges it. The same message_id again (at-least-once delivery) is never
printed twice, only acknowledged. The heavy tick never prints a message and never acknowledges past one not delivered
yet: it stops right before it and applies the rest on a later tick. Nothing mirrors the line to /agent/messages (the
runner's pump sends typed prompts only); a mirror would use source_key `planou:<message_id>`.

The agent's work queue (Planou "Fila do agente", event `task_queued_for_agent`): only an agent whose own config says
`"planou": {"task_queue": true}` declares the `task_queue` capability (and only work-watch-* and job-scout, whose runner
delivers events); without it Planou hands nothing out. Only an assignment puts a task in the queue (the person, or the
agent itself with `"assignee": "self"` in an autonomous project); the WIP limit (1) lives in Planou, on the agent's Fila
tab. The event is delivered like a person's message: the light loop prints
`-- FILA <pid>: <title> ...`, the runner acknowledges it only after printing tick.out and the heavy tick never
acknowledges past one not delivered yet (a task handed out and never seen would hold the only WIP slot). The whole
payload (description, origin, autonomy) stays in cache/planou/queue.json (`fila ver`). `task_dequeued` prints
`-- FILA SAIU <pid>: ...` and drops the task locally. The session reports its work with `fila started|in_review|done|
blocked` (POST /agent/tasks/{task_id}/progress); a 409 not_in_queue drops the task locally and prints `PARE: ...`.
`fila refinar [PID] --note TEXT` asks the person about the scope before starting: the agent's own task goes to the
project state "Em refinamento" with the question as an ask tied to it (it frees the slot; the answer brings the task back
to A fazer and to the queue); a task the person created goes `blocked` with the question as its note. A 409 not_in_queue
on a task in refinement means wait for the answer (`ESPERE: ...`), not stop.
The limit "Ao mesmo tempo" of the Fila tab is the agent's real limit of work: with the queue on, the heartbeat also
declares `task_release`, and Planou announces every task that enters the queue (`task_queued_for_agent` with
`released: false` = it waits, printed `-- FILA <pid>: ... NA FILA, nao comecar`) and sends `task_released` when its slot
comes (`-- FILA LIBERADA <pid>: ...`). Only a released task is started (`released` absent = an older Planou: released).
`fila ver` asks Planou (GET /agent/queue) and marks each task `liberada` or `na fila`; `fila started` on a task not
released gets 409 not_released and prints `ESPERE: ...`.
Idle pull (Planou 0.42.0): in an autonomous project Planou itself fills a free slot from the backlog (`queued_by: "auto"`,
`reason: "vaga livre, projeto autonomo"`), with a task_changed `by: "auto"` that takes the task out of the backlog; the
agent never pulls by itself. The queue line shows the reason and it is a released task like any other (unclear scope:
`fila refinar`); the task_changed clears the local backlog flag, so the next sync sends the state again.
"Pedir ajuste" (Planou 0.38.0, event `task_changes_requested`): the person asks for an adjustment on a task in review
the agent did; the task goes back to the agent's queue and waits for a slot. The text is kept by task_id in
cache/planou/changes.json (the brief of the rework: same branch, same PR in `pr_url`) and the tick prints
`-- AJUSTE PEDIDO <PID>: <text>`; queue.json and, for the agent's own task, state.json take the event's version, state
and project state (the next sync neither conflicts nor puts the task back in review). The task_queued_for_agent or
task_released that hands it out again is rework, not a new task: `-- FILA LIBERADA <PID>: ... -> RETRABALHO` with the
text. Until then no progress goes up (`fila started|tempo|...` prints `ESPERE: ...`), and a 409 not_in_queue in that
interval is not a `PARE`. `fila ver` shows the text under `ajuste_pedido`; in_review, done or handoff answers it. An
event type this runner does not know, about one of its tasks and with `changed`, is applied as a task_changed.
Reviewer (Planou 0.43.0, PLN0139): the dev's handoff carries the PR, so the reviewer's task_queued_for_agent (and a
rework's task_released) comes with `pr_url`, printed on the queue line (`PR: <url>`) and kept in queue.json. The reviewer
sends the task back with `fila ajuste <PID> --text TEXT [--pr-url URL]` (POST /agent/tasks/{id}/request-changes): the
task returns to the dev's column and queue as rework and the reviewer's entry ends (done, no task_dequeued), so it is
dropped locally (`DEVOLVIDA: ...`). The dev gets task_changes_requested with `requested_by.kind: "agent"` and
`comment_id: null` (the text is no comment): the line says who asked (`-- AJUSTE PEDIDO <PID> (<role> <name>): ...` or
`(pessoa)`). Since Planou 0.49.0 (PLN0164) the event carries `requested_by_role` (the Funcao of the agent that asked,
null for the person) and `requested_in` ({id, name} of the column where it was asked); the line uses the role, else the
column name, else `revisor` (an older Planou). In a dev -> review -> QA flow the QA's ask goes straight back to the dev
(`to: "author"`, the server's default); `fila ajuste --to previous` sends it to whoever passed the task to the current
column (the old behavior). 409 no_previous_owner (the task did not come by another agent's handoff) prints
`SEM DEV: ...`: the reviewer falls back to `fila blocked`.
Time and cost of a queue task (Planou 0.26.0): `fila started <PID> --estimate-h H` sends the estimate and marks the task
as the current one (current_task, cache/planou/current_task.jsonl): from then on the session's cost turns and its
workers' (watch_core.turns, `task` and `subagent`) go to Planou tied to it, and Planou shows the measured completed time
and the task's cost. `fila tempo <PID> --remaining-h H` updates what is left on every step (it resends the current
state); `done` sends remaining 0. in_review, blocked, done, a 409 not_in_queue and `-- FILA SAIU` close the marker.
Handoff (Planou 0.27.0, owners of columns): when the agent owns a column with a next state in the task's project
(GET /agent/projects/{key}/states, `owner.is_me` and `next_state`, read fresh), `fila done` sends `handoff` instead: the
task goes to the next column and to its owner (`PASSOU: ...`), and a 409 no_next_state falls back to done. `fila handoff`
asks for it explicitly, without the fallback.
One instance, the whole cycle (PLN0281): when the next column is this same agent's (dev, then review or QA), Planou
queues the task to it again without `handed_off_by` nor the note; progress() keeps a local record of that handoff
(cache/planou/self_handoffs.json), the queue line says `passada por voce mesmo ...` and sends the review or QA to a NEW
worker (never the one that delivered), `fila ver` shows `mesma_instancia`, and the 409 no_previous_owner of `fila
ajuste` on such a task prints `RETRABALHO PROPRIO: ...` (rework in the same column, then a new review worker) and leaves
the ask on the task as the agent's comment (POST /agent/tasks/{id}/comments; under "minimum" a neutral line), since
without a PR nothing else shows it in Planou. `fila done` of such a task warns (stderr, not a refusal) when no worker
of role other (`fila worker-start --role other`) was opened for it after the handoff or the last ask.
The Prazo (`deadline`) goes in the sync when the source states one (watch_core.deadlines): absent keeps Planou's.
Worker deliveries (Planou "Entregas", PLN0045): `fila worker <PID> --role dev|integrator --tokens N --steps N
--duration-ms N --result feito|parcial|falhou [--model M]` reports a worker (subagent) that came back, with the usage the
session got (subagent tokens, tool uses, duration): POST /agent/tasks/{id}/deliveries, for any task of the agent (not
only the queue). The key is derived from the values, so running the same command again is `unchanged`. A 404 without an
error body is an older Planou without the route: a warning, not an error (exit 0). `--phases-from <output_file of the
Agent tool, or the agent id>` also sends where the worker's time went (`phases`, seconds per phase, PLN0261), measured
from the subagent's transcript by watch_core.phases; a transcript not found or unreadable is a warning and the delivery
goes without phases. The phases stay out of the derived key, so a repeat with them is still `unchanged`. The same
transcript gives the worker's usage per model (`usage`, PLN0101: input, output, cache writes and reads, once per message
id), so Planou shows the delivery's real API price; it also stays out of the key, and an older Planou ignores the field.
Comment with @ (Planou PLN0240, event `task_comment_mentioned`): a person calls the agent with @name in a task comment,
on its own task or on any task of the workspace. Planou sends the event only to an agent that declares the
`task_comment` capability (declared by every agent of this plugin with Planou on, AGENT_ONLY), so it is delivered like a
person's message (`deliver`: acknowledged only after the runner printed it, never acknowledged unread):
`-- COMENTARIO <pid> de <author> (HH:MM): <text>` with an indented line that tells the session how to answer. The
session answers by comment, `comentario PID --text - [--reply-to COMMENT_ID]` (POST /agent/tasks/{id}/comments), and
reads the thread with `comentario PID --ver` (GET); answering is not taking the task. 403 not_your_task: the task is
not the agent's and nobody called it there.
Workers in progress (Planou "Workers agora", PLN0220): `fila worker-start <PID> [--task PID2 ...] --role
dev|integrator|other --label TEXT` registers a worker right after the session starts it (POST /agent/workers) and prints
its key (kept in cache/planou/workers.json); `fila worker` with the same key (`--key`, or the one open here for the task
and role) closes it with the delivery; `worker end KEY --result ...` closes one without a delivery (no task, role other);
`worker ping KEY [--label ...]` is the PATCH (sign of life). The heavy tick of a new session closes the ones left open by
a session that died without session_closed (`-- WORKER INTERROMPIDO`).

Asks ("Precisa de você"): request_decision, request_approval, submit_draft, raise_alert, cancel_ask, report_result. In
this phase an approval only records the person's OK: nothing is sent because of it. reconcile_asks(desired) keeps the
asks that come from the agent state (decisions, what the agent needs) in step with it: opens the missing ones, withdraws
the ones that no longer apply and never reopens one the person already answered.

CLI (the agent's config is read from its own folder; without a "planou" block nothing is sent):
  python3 -m watch_core.planou --agent job-scout key set | status | active |
      heartbeat PHASE [--next-heavy EPOCH] [--session-id ID] |
      poll [--next-heavy EPOCH] [--poll-s 90] [--wait 50] | cursor-commit | ack-delivered | events | ack SEQ | tarefa [PID | -] |
      fila ver | fila started|in_review|done|blocked|handoff [PID] [--pr-url URL] [--note TEXT|-] [--estimate-h H] [--remaining-h H] |
      fila tempo [PID] --remaining-h H [--estimate-h H] | fila refinar [PID] --note TEXT|- |
      fila worker PID --role dev|integrator --tokens N --steps N --duration-ms N --result feito|parcial|falhou [--model M] [--key K] [--phases-from ARQ] |
      fila worker-start PID [--task PID2 ...] --role dev|integrator|other --label TEXT |
      worker start [PID ...] --role R --label TEXT | worker ping KEY [--label TEXT] [--task PID ...] |
      worker end KEY --result feito|parcial|falhou | worker ver |
      pronta CODE|PID "motivo" [--self] | pronta CODE|PID - | autonomia |
      draft --channel linkedin --to "..." --title "..." --text - [--task m12] [--context ...] [--ref ...] |
      approval --type send_application --verb "enviar a candidatura" --title "..." [--task a44] [--ref ...] |
      decision --title "..." [--option "A=..." --option "B=..." [--recommended A]] [--no-free-text] | alert --title "..." |
      pergunta --title "Quer que eu ...?" [--option "A=..." --option "B=..." --recommended A] [--auto] [--task a12] [--context ...] [--ref ...] |
      cancel CODE [--reason ...] | result CODE executed|failed [--reason ...] |
      comentario PID --text - [--reply-to COMMENT_ID] | comentario PID --ver |
      retro dados|ver|contribuir|votar|ata MEETING | refino ver|sugerir|lista MEETING |
      daily ver MEETING | daily explicar MEETING --text - [--cost-usd N] [--tokens N] |
      tools | anexo CODE SOURCE_KEY FILE [--name NAME] | attach TASK FILE [--name NAME] |
      sugestao --title "..." --what "..." --expected "..." --example "..." [--subject "..."] [--priority P1..P4]

`status` (PLN0077) also shows what the next heartbeat declares ("heartbeat": the capabilities, and how many tools and
link rules are saved to go with it) and, with the queue on, the limit "Ao mesmo tempo" as Planou has it ("queue": wip and
busy from GET /agent/queue, the same numbers as `fila ver`; source "cache" and nulls when Planou does not answer).

Suggestions (PLN0007, watch_core.suggestions): `sugestao` leaves a suggestion about a limit or a defect of Planou or of
the plugins in the planou-dev inbox (no Planou key needed; 3 per agent per day; client data refused); planou-dev turns
it into a backlog task of the Planou project reported by this agent.

Tools (Planou "Ferramentas do agente"): the heavy tick calls set_tools(list) with one entry per source (source_tools
builds them from the config, the broken sources of the state and how to fix each one); the list waits in
cache/planou/tools.json and goes with the next heartbeat of any phase when it changed or an hour went by (a list older than
3 h is not sent: the heavy tick stopped and Planou shows it stale). `last_error`, `label` and `fix` are cleaned with the
same patterns Planou uses to refuse a credential; a credential goes only as its type and a known expiry. A 422
(secret_refused, invalid_tools) is logged in cache/planou/tools.log with the field paths and the same list is not sent
again until it changes; the sign of life counted anyway. Failure and expiry alerts are Planou's (tool_failing,
tool_expiring): the agent raises none.

Autolinks (Planou "Links automáticos do agente", the Planou version that brings "links"): the heavy tick calls set_links(rules) with the instance's link
rules ([{pattern, url, label?}]: a regex with 1 to 3 groups, an http(s) url with {1}..{3}); they wait in
cache/planou/links.json and go with the next heartbeat when they changed or a day went by, in the "links" field (an older
Planou ignores it). Planou then turns the ids the agent writes into links on its screens. A 422 invalid_links is logged in
cache/planou/links.log with the field paths and the same rules are not sent again until they change.

Role (Planou "Papel do agente"): the agent plugin's heavy tick calls set_docs(manifest) with the files its session reads,
in order (core SKILL.md, instructions.md, CONTEXT.md, each behavior on), each with size, sha256 and date; instructions.md
and CONTEXT.md go editable with their content, next to the catalog of behaviors the agent can turn on. The person's edit
comes back as `agent_docs_changed`: apply_events hands it to the handler of set_docs_handler() (the agent plugin's
role_edit.py writes, validates, rolls back) before it acknowledges the cursor, and docs_ack() answers
POST /v1/agent/docs/ack (applied with the sha256, or refused with the reason). Each behavior with reference cases carries
"evals", the result of `agent.py <instance> --evals --json` for it (cases, passed, failures, results, problems, the sha256
of the BEHAVIOR.md and ran_at; Planou 0.49 or later). The manifest waits in cache/planou/docs.json and goes with the next
heartbeat when it changed (ran_at does not count) or a day went by. A 422 invalid_docs / secret_refused on it is logged in
cache/planou/docs.log with the field paths and the same manifest is not sent again until it changes.

Attachments (Planou "Anexos da tarefa"): attach(code, source_key, path|data, name) sends a file to the agent's task in
multipart, only when its sha256 or name changed (cache/planou/attachments.json); 404 (task not synced yet) is retried on a
later tick; nothing is sent under "minimum". The task may be a code of the agent's sync, its pid or a task of the queue.
An attachment the person deleted in Planou comes back as 202 `result: ignored` (PLN0175): the key is marked ignored in
the cache and that key of that task is never sent again, even when the file changes.
Files of a task (PLN0052): every local file the agent produces for a task, or that its text cites, goes as an attachment.
attach_file(task, path) (CLI `attach TASK FILE`) sends it under `file:<name>` and registers it in
cache/planou/attach_files.json, so the tick retries it and sends each new version (flush_attachments, from the sync and
the tick heartbeat). The sync attaches the files a task description cites and sends the description with
"[ver anexo: <name>]" in place of the local path; a `fila` note does the same, and `fila started` attaches the files the
queue task's description cites. Only what passes attachable() goes: a type Planou accepts, 1 byte to 10 MB, outside
secrets/, .ssh, caches and git, no credential-looking name, no credential in a text file.

Proposals (Planou 0.53.0, "Propostas do agente"): propose(change, title, ref, ...) asks the person to approve a parameter
("Ao mesmo tempo" of an agent, the project's release rules, a task's priority) or a Papel change (a whole file or the list
of behaviors) with POST /v1/agent/proposals; she approves with one click in Precisa de você and Planou applies it itself,
with a record and Desfazer. The idempotency key is `<agent>:proposal:<ref>`, so a rerun gets the same proposal back
(`reused`). The proposal and its ask code are kept in cache/planou/proposals.json; proposal_state(id) reads it again (GET)
and cancel_proposal(id) withdraws one not answered yet. The event `proposal_changed` (applied, rejected, failed,
reverted) wakes the session with `-- PROPOSTA <ap-N> ...`; the `approved`/`rejected` of the same ask says Planou
applies it (nothing for the agent to do).

Allocation (Planou 0.63.0, PLN0227/PLN0234): an employee creates tasks only in the projects it is allocated to. Before the
sync, allocation() reads GET /v1/agent/projects (default_project and the allocated keys), cached 5 min in
cache/planou/projects.json (a failure, an older Planou without the route or an empty list is cached as unknown: nothing
is filtered, as before). A new item (no pid known here) of a project not allocated stays out of the batch, and
`AVISO (planou): projeto X não alocado: aloque o funcionário no Planou` goes to the tick output once per project and day
(cache/planou/not_allocated_seen.json, also in warnings.log). A task Planou already has goes in any project (updates are
allowed). A new item without a project goes to the configured project, else to Planou's default_project, never to a
fixed one (the work-watch `sigla` is no longer a fallback). When Planou still answers 403 project_not_allocated (the
cache was stale), the same batch goes once more without the `fields.source_keys` it named, the cache is dropped and the
projects of `fields.projects` are warned; a second refusal is a normal failure, never a loop.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import config
from . import deadlines as _deadlines
from . import fileio

VERSION = '0.26.0'
TIMEOUT_S = 10
MAX_WAIT_S = 55                            # long poll: the server caps it there (Cloudflare cuts a request at ~100 s)
WAIT_HEADER = 'Planou-Wait'                # sent only by a server that honoured ?wait= (an older one ignores it)
POLL_LONG, POLL_SHORT, POLL_FAILED = 0, 3, 4   # exit codes of `poll --wait`
BROKEN_AFTER = timedelta(minutes=60)
CLOSED_WINDOW = timedelta(days=2)          # closed older than this: sent only while Planou still has it open (the server does not create it)
PHASES = ('poll', 'tick', 'woke', 'working', 'turn_end', 'session_open', 'session_closed')

# Status labels produced by watch_core.tasks (data values of the task engine) -> Planou state and resolution.
STATE_OF = {
    'A fazer': ('todo', None), 'Fazendo': ('in_progress', None), 'Aguardando': ('waiting', None),
    'Impedimento': ('blocked', None), 'Decisão': ('todo', None),
    'Feito': ('closed', 'done'), 'Sem ação': ('closed', 'no_action'), 'Descartada': ('closed', 'discarded'),
}
# Planou events -> the status label the agent's "box ticked" handler understands.
STATUS_OF_RESOLUTION = {'done': 'Feito', 'no_action': 'Sem ação', 'discarded': 'Descartada'}

_S = {'agent': None, 'root': None, 'project': None, 'confidentiality': 'title', 'publish': True, 'live': True,
      'base_url': None, 'plugin_version': None, 'drafts_text': True, 'warnings': [], 'cited': {}}


class PlanouError(Exception):
    def __init__(self, status, code, message, fields=None):
        super().__init__(f'{status} {code}: {message}')
        self.status, self.code, self.message = status, code, message
        self.fields = list(fields or ())          # the paths the server named (never the values)
        self.detail = dict(fields) if isinstance(fields, dict) else {}   # {path: [values]} (project_not_allocated)


# ---------------------------------------------------------------- configuration

def _plugin_manifest(start=None):
    """The first `.claude-plugin/plugin.json` above the real path of `start` (this file by default), as a dict; {} when
    there is none (the shared source belongs to no plugin) or it cannot be read. A skill installed as a symlink resolves
    to the plugin folder."""
    d = os.path.dirname(os.path.realpath(start or __file__))
    for _ in range(8):
        f = os.path.join(d, '.claude-plugin', 'plugin.json')
        if os.path.isfile(f):
            try:
                m = json.load(open(f))
            except (OSError, ValueError):
                return {}
            return m if isinstance(m, dict) else {}
        up = os.path.dirname(d)
        if up == d: break
        d = up
    return {}


def find_plugin_version(start=None):
    """Version of the plugin this copy of watch_core ships in. None for the shared source, which belongs to no plugin."""
    v = _plugin_manifest(start).get('version')
    return str(v) if v else None


def find_plugin_name(start=None):
    """Name of the plugin this copy of watch_core ships in; 'watch-core' for the shared source."""
    n = _plugin_manifest(start).get('name')
    return str(n) if n else 'watch-core'


def user_agent(plugin=None):
    """User-Agent of every call to the Planou: `planou-agent/<watch_core version> (+<plugin>)`. The Cloudflare in front
    of app.planou.com answers 403 "error code: 1010" to urllib's default (Python-urllib/3.x)."""
    return f'planou-agent/{VERSION} (+{plugin or _own_name()})'


def configure(agent, root, project=None, confidentiality='title', publish=True, live=True, base_url=None, plugin_version=None,
              drafts_text=True):
    """Called once per tick by the plugin. `publish=False` turns this agent off; `live=False` (test mode) sends nothing;
    `drafts_text=False` keeps the text of the drafts on this machine (the ask goes up without it). Without
    `plugin_version`, the version of the plugin this copy ships in (the runner, the CLI and job-scout pass none)."""
    _S.update(agent=agent, root=os.path.expanduser(root), project=project, confidentiality=confidentiality or 'title',
              publish=publish is not False, live=live is not False,
              base_url=(base_url or config.get('planou.base_url') or '').rstrip('/') or None,
              plugin_version=plugin_version or _own_version(), drafts_text=drafts_text is not False)


_VERSION_CACHE = []


def _own_version():
    if not _VERSION_CACHE: _VERSION_CACHE.append(find_plugin_version())
    return _VERSION_CACHE[0]


_NAME_CACHE = []


def _own_name():
    if not _NAME_CACHE: _NAME_CACHE.append(find_plugin_name())
    return _NAME_CACHE[0]


def _key_file():
    return os.path.join(_S['root'], 'secrets', 'planou.env')


def _cache_dir():
    return os.path.join(_S['root'], 'cache', 'planou')


def _key():
    try:
        with open(_key_file()) as f:
            for line in f:
                line = line.strip()
                if line.startswith('PLANOU_AGENT_KEY='):
                    return line.split('=', 1)[1].strip().strip('"\'') or None
    except OSError:
        return None
    return None


def active():
    """True when watch_core has Planou enabled, this agent publishes, it is live, and a key and a base URL exist."""
    if not _S['agent'] or not _S['root']: return False
    if not config.get('planou.enabled'): return False
    if config.get('planou.publish.planou') is False: return False
    return bool(_S['publish'] and _S['live'] and _S['base_url'] and _key())


# ---------------------------------------------------------------- HTTP

def _request(method, path, body=None, timeout=None, headers=None, raw=None, content_type=None):
    """(status, json). `headers`, a dict, receives the response headers (the long poll reads Planou-Wait). `raw` (bytes)
    with its `content_type` goes instead of a JSON body (the multipart of an attachment)."""
    url = _S['base_url'] + path
    data = raw if raw is not None else None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': f'Bearer {_key()}',
        'Content-Type': content_type or 'application/json',
        'Accept': 'application/json',
        'User-Agent': user_agent(),
        'X-Planou-Client': f'watch_core/{VERSION} {_S["agent"]}/{_S["plugin_version"] or "?"}',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT_S) as r:
            raw = r.read()
            if headers is not None: headers.update({k.lower(): v for k, v in r.headers.items()})
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            err = (json.loads(e.read() or b'{}') or {}).get('error') or {}
        except ValueError:
            err = {}
        fields = err.get('fields') if isinstance(err.get('fields'), dict) else {}
        raise PlanouError(e.code, err.get('code') or 'http', err.get('message') or e.reason, fields) from None


def _load(name, default):
    try:
        with open(os.path.join(_cache_dir(), name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(name, value):
    # a unique temporary file: the runner and the CLI may save the same file at the same moment
    fileio.write_json(os.path.join(_cache_dir(), name), value, ensure_ascii=False, indent=1)


def _locked(name):
    """Exclusive lock (cross-process, reentrant) for a load, change, save of cache/planou/<name> (queue.json,
    state.json). Order when both are held: state.json first, then queue.json."""
    return fileio.locked(os.path.join(_cache_dir(), name))


def _holding(name):
    return fileio.holding(lambda: os.path.join(_cache_dir(), name))


def _ok():
    h = _load('health.json', {})
    if h.get('failing_since'): _save('health.json', {})


def _failed(err, now=None):
    """Records the failure and returns the line for the tick output."""
    now = now or datetime.now(timezone.utc)
    h = _load('health.json', {})
    since = h.get('failing_since') or now.isoformat()
    _save('health.json', {'failing_since': since, 'last_error': str(err)[:300]})
    if now - datetime.fromisoformat(since) > BROKEN_AFTER:
        return f'FONTE QUEBRADA (planou): {err}'
    return f'AVISO (planou): {err}'


def _call(method, path, body=None, **kw):
    """(status, json) or raises PlanouError; network problems become PlanouError too."""
    try:
        return _request(method, path, body, **kw)
    except PlanouError:
        raise
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        raise PlanouError(0, 'network', str(getattr(e, 'reason', e))) from None


# ---------------------------------------------------------------- sync

def _date(v):
    if not v: return None
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        return None


def _completed_at(v):
    """completed_at for the sync: the real instant (ISO 8601 with its offset) when the agent knows the time (it closed
    the task itself, or the origin gave it: PR merged_at, issue closed); a bare date (aaaa-mm-dd) is still sent as that
    day at 12:00Z, as before."""
    d = _date(v)
    if not d: return None
    s = str(v).strip()
    t = _date_time(s) if len(s) > 10 else None
    return t.isoformat() if t else d + 'T12:00:00Z'


def _priority(prio, due, today):
    """Spec table: prio 0 and 1 -> 1; 2 -> 2 when due today, else 3; 3 or more -> 4."""
    p = 3 if prio is None else int(prio)
    if p <= 1: return 1
    if p == 2: return 2 if due == today else 3
    return 4


# Planou's mark of a person's decision (BacklogPull.cs, PersonDecisions): a title starting with "Decidir:" (case ignored,
# the colon counts) never takes a slot and is never pulled from the backlog
DECISION_PREFIX = 'Decidir: '
_DECISION_HEAD = re.compile(r'^\s*(decidir|decide)\b\s*:?\s*', re.I)


def _title(code, it):
    """minimum: origin + id, no third-party text; title and detail: the short title of the daily page. An open decision
    ("Decisão") always goes as "Decidir: ...", also under minimum and in an English instance ("Decide: ..."), so Planou
    never hands it to an agent's slot."""
    if _S['confidentiality'] == 'minimum':
        t = f'{(it.get("origem") or "Tarefa").capitalize()} {it.get("uid") or code}'
    else:
        t = (it.get('curto') or it.get('titulo') or code).strip()
    if it.get('status') == 'Decisão' and not t.lower().startswith(DECISION_PREFIX.strip().lower()):
        t = DECISION_PREFIX + (_DECISION_HEAD.sub('', t) or t)
    return t[:200]


# origin of a task: the item's "origem" (the source of a pending item, 'ação', 'extra', ...) -> Planou origin type
ORIGIN_TYPE = {'slack': 'slack', 'teams': 'teams', 'chat': 'chat', 'google_chat': 'chat', 'email': 'email', 'gmail': 'email',
               'outlook_email': 'email', 'reuniao': 'meeting', 'gravacoes': 'meeting', 'calendar': 'calendar',
               'google_calendar': 'calendar', 'outlook_calendar': 'calendar', 'jira': 'jira', 'ado': 'ado',
               'ado_workitems': 'ado', 'ado_prs': 'ado', 'pipelines': 'pipeline', 'ado_pipelines': 'pipeline',
               'github': 'github', 'gitlab': 'gitlab', 'ação': 'daily', 'linkedin': 'linkedin', 'mensagens': 'linkedin'}
ORIGIN_NAME = {'slack': 'Slack', 'teams': 'Teams', 'chat': 'Google Chat', 'email': 'E-mail', 'meeting': 'Ata', 'calendar': 'Convite',
               'jira': 'Jira', 'ado': 'Azure DevOps', 'github': 'GitHub', 'gitlab': 'GitLab', 'pipeline': 'Pipeline',
               'daily': 'Página do dia', 'linkedin': 'LinkedIn', 'job_board': 'Site de vagas', 'other': 'Origem'}


def when_label(iso):
    """'28/09 09:40' in local time, or ''."""
    if not iso: return ''
    try:
        d = datetime.fromisoformat(re.sub(r'(\.\d{6})\d+', r'\1', str(iso).replace('Z', '+00:00')))
        return (d.astimezone() if d.tzinfo else d).strftime('%d/%m %H:%M')
    except ValueError:
        return ''


def default_origin(code, it):
    """The origin of an item from the generic fields every agent fills (origem, quem, entrou, link). Agents may pass
    their own as it['planou_origin'] (same shape)."""
    kind = ORIGIN_TYPE.get(str(it.get('origem') or '').lower(), 'other')
    who = (it.get('autor') or it.get('quem') or '').strip() or None
    at = it.get('entrou')
    label = ' · '.join(x for x in (ORIGIN_NAME[kind], who, when_label(at)) if x)
    link = it.get('link') or ''
    return {'type': kind, 'label': label, 'url': link if link.startswith(('http://', 'https://')) else None, 'author': who, 'at': at}


def default_description(code, it, origin):
    """Where it came from, the text that started it and what the agent understood must be done. Never empty; says so when
    the state does not know the origin."""
    lines = [f'De onde veio: {origin["label"]}.' if origin.get('type') != 'other' or origin.get('author')
             else 'De onde veio: o estado do agente não guarda a origem desta tarefa.']
    trecho = ' '.join(str(it.get('trecho') or '').split())
    if trecho: lines.append(f'Trecho: "{trecho[:600]}"')
    fazer = ' '.join(str(it.get('titulo') or it.get('curto') or code).split())
    lines.append(f'O que fazer: {fazer[:1500]}')
    if it.get('status') == 'Aguardando' and it.get('quem'): lines.append(f'Aguardando: {it["quem"]}.')
    if it.get('evidencia'): lines.append(f'Evidência: {it["evidencia"]}')
    return '\n'.join(lines)


def _origin_and_description(code, it):
    """Confidentiality applied here: minimum = type and date only; title = who and when, no third-party text;
    detail = everything, with the excerpt."""
    origin = dict(it.get('planou_origin') or default_origin(code, it))
    conf = _S['confidentiality']
    name = ORIGIN_NAME.get(origin.get('type'), 'Origem')
    if conf == 'minimum':
        origin = {'type': origin.get('type') or 'other', 'label': ' · '.join(x for x in (name, when_label(origin.get('at'))) if x),
                  'url': origin.get('url'), 'author': None, 'at': origin.get('at')}
        return origin, f'De onde veio: {origin["label"]}. Tarefa {it.get("uid") or code}.'
    desc = it.get('planou_description') or default_description(code, it, origin)
    if conf != 'detail':
        desc = default_description(code, {k: v for k, v in it.items() if k not in ('trecho', 'titulo')}, origin)
    return origin, desc


def waiting(it, now=None):
    """"Aguardando por" of a waiting or blocked task: {"who", "since"} (spec: aguardando {quem, desde}). The item may say it
    explicitly (aguardando_quem, aguardando_desde: job-scout's company and application date); otherwise who is the item's
    `quem` and since the day it entered (`entrou`), as on the daily page. `who` is `title` level (spec 9.6): "minimum"
    sends only since. A since in the future is dropped (the server would ignore it). None when there is nothing to say."""
    who = ' '.join(str(it.get('aguardando_quem') or it.get('quem') or '').split())[:120] or None
    if _S['confidentiality'] == 'minimum': who = None
    since = _date(it.get('aguardando_desde') or it.get('entrou'))
    if since and since > (now or datetime.now(timezone.utc)).astimezone().date().isoformat(): since = None
    return {'who': who, 'since': since} if who or since else None


READY_MAX = 200
REFINEMENT = 'Em refinamento'               # the built-in project state for "working out the scope with the person"
AUTONOMIES = ('autonomous', 'semi_autonomous', 'manual')
# neutral reasons for "minimum" (no names, no subject, no numbers of the source)
READY_USER_MIN = 'pedido explícito do usuário, dentro da autonomia, sem decisão pendente'
READY_GENERIC_MIN = 'espelha um item ativo na origem'
# what Planou says when it keeps a task in backlog (warnings of a creation, `reason` of an update)
BACKLOG_NOTES = ('created in backlog', 'in or out of backlog', 'out of backlog', 'back to backlog')


def _one(text, limit=READY_MAX):
    return ' '.join(str(text or '').split())[:limit].strip()


def ready_reason(code, it, state, due=None, marked=None):
    """Why the agent puts this task outside backlog by itself (Planou "o agente começou porque ..."), or None (it is born
    in backlog: a suggestion). In order: the agent marked it ready (`marked`, from ready.json: the user asked for it), the
    source said so (the item's `ready_reason`/`ready_reason_min`: something already happening there), the plan of the day
    ("Fazendo"), a waiting task with someone to wait on, a due date. Closed and backlog tasks need none. Under "minimum"
    the neutral variant goes up; a text that looks like a secret never goes."""
    if state in ('closed', 'backlog'): return None
    minimum = _S['confidentiality'] == 'minimum'
    if marked and marked.get('reason'):
        full, short = marked['reason'], READY_USER_MIN
    elif it.get('ready_reason'):
        full, short = it['ready_reason'], it.get('ready_reason_min') or READY_GENERIC_MIN
    elif it.get('status') == 'Fazendo':
        full = short = 'em andamento: está no plano do dia'
    elif state in ('waiting', 'blocked') and (it.get('aguardando_quem') or it.get('quem')):
        full = f'aguardando {it.get("aguardando_quem") or it.get("quem")}'
        short = 'aguardando resposta de outra pessoa' if state == 'waiting' else 'impedida por outra pessoa'
    elif due:
        full = short = f'prazo definido: {due[8:10]}/{due[5:7]}'
    else:
        return None
    text = _one(short if minimum else full)
    if not text or looks_secret(text): text = _one(short)
    return text or None


def _still_open(entry):
    return bool(entry and entry.get('pid') and entry.get('state') != 'closed')


def open_codes():
    """Codes Planou has and did not confirm closed (state.json): a close for them goes whatever its age (CLOSED_WINDOW
    only bounds a task Planou never had). A source that drops old closed items keeps these. Empty when not configured."""
    if not _S['root']: return set()
    return {c for c, e in (_load('state.json', {}).get('tasks') or {}).items() if _still_open(e)}


def payload(items, now=None, autonomy=None, default_project=None):
    """The sync batch for {code: item} from watch_core.tasks.itens(). Pure: reads only local files (the version map,
    ready.json). `autonomy` is the project's (sync() reads it): manual never sends a reason; a task Planou keeps in
    backlog goes without state (autonomous: with a reason, the state goes and takes it out)."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone().date().isoformat()
    known = _load('state.json', {}).get('tasks') or {}
    marked = _load('ready.json', {})
    _S['cited'] = {}
    out = []
    for code, it in items.items():
        state, resolution = STATE_OF.get(it.get('status'), ('todo', None))
        completed = _date(it.get('concluida'))
        kt = known.get(code) or {}
        if (state == 'closed' and completed and date.fromisoformat(completed) < (now - CLOSED_WINDOW).date()
                and not _still_open(kt)):
            # closed long ago: Planou does not create it, and one it already has closed needs nothing. One it still has
            # open (the runner stopped past the window) goes whatever its age, until an answer confirms the close
            continue
        due = _date(it.get('prazo'))
        link = it.get('link') or ''
        task = {
            'source_key': f'{_S["agent"]}:{code}',
            'source_id': it.get('uid') or code,
            'title': _title(code, it),
            'state': state,
            'resolution': resolution,
            'priority': _priority(it.get('prio'), due, today),
            'due': due,
            'completed_at': _completed_at(it.get('concluida')) if state == 'closed' and completed else None,
            'origins': [{'system': it.get('origem') or None, 'url': link}] if link.startswith(('http://', 'https://')) else [],
            'base_version': kt.get('version'),
        }
        task['origin'], task['description'] = _origin_and_description(code, it)
        if _S['confidentiality'] != 'minimum':
            # a local file the description cites goes as an attachment; the text says "ver anexo" (PLN0052)
            task['description'], files = without_paths(task['description'])
            if files: _S['cited'][code] = files
        if state in ('waiting', 'blocked'):
            w = waiting(it, now)
            if w: task['waiting'] = w
        if task['origin'].get('at'):
            try:
                a = datetime.fromisoformat(str(task['origin']['at']).replace('Z', '+00:00'))
                task['origin']['at'] = (a if a.tzinfo else a.astimezone()).isoformat()
            except ValueError:
                task['origin']['at'] = None
        dl = _deadlines.valid(it.get('deadline'))
        if dl: task['deadline'] = dl          # absent keeps Planou's (never null: a missing one is not "erase")
        # the item's project, else the configured one; a new item without either goes to Planou's default (allocation)
        proj = it.get('project') or _S['project'] or (default_project if not kt.get('pid') else None)
        if proj: task['project'] = str(proj)
        if it.get('project_state'): task['project_state'] = str(it['project_state'])[:200]
        # the epic of the item (pid or source key, PLN0250): absent keeps Planou's (never null: that would take it out)
        if it.get('epic'): task['epic'] = str(it['epic'])[:200]
        mk = marked.get(code) if isinstance(marked.get(code), dict) else None
        reason = None if autonomy == 'manual' else ready_reason(code, it, state, due, mk)
        if reason: task['ready_reason'] = reason
        if kt.get('refining') and state != 'closed':
            # scope being worked out with the person: the task sits in "Em refinamento" until she answers or moves it
            for k in ('state', 'resolution', 'completed_at', 'waiting'): task.pop(k, None)
            task['project_state'] = REFINEMENT
        elif kt.get('backlog') and state != 'closed' and not (autonomy == 'autonomous' and reason):
            # Planou keeps it in backlog (only the person takes it out here): no state, so no warning on every tick. A
            # close always goes: at worst one logged reason a day, and an accepted close tells the flag was stale
            for k in ('state', 'resolution', 'completed_at', 'waiting', 'project_state'): task.pop(k, None)
        if (mk and mk.get('self') and not kt.get('self_sent') and reason and autonomy != 'manual'
                and (not kt.get('pid') or autonomy == 'autonomous')):
            task['assignee'] = 'self'           # the agent takes it: at creation, or on an update in an autonomous project
        out.append(task)
    return {'agent': _S['agent'], 'generated_at': now.isoformat(), 'tasks': out}


def _in_backlog(notes):
    return any(n and any(b in str(n) for b in BACKLOG_NOTES) for n in notes)


def note_warnings(code, notes, now=None):
    """Logs each warning or reason of a task in cache/planou/warnings.log once per task, text and day (a task kept in
    backlog would repeat the same line on every tick). Returns the lines that were new today."""
    now = now or datetime.now(timezone.utc)
    day = now.astimezone().date().isoformat()
    seen = _load('warnings_seen.json', {})
    if seen.get('day') != day: seen = {'day': day, 'keys': []}
    new = []
    for n in notes:
        if not n: continue
        k = hashlib.sha256(f'{code}|{" ".join(str(n).lower().split())}'.encode()).hexdigest()[:16]
        if k in seen['keys']: continue
        seen['keys'].append(k)
        new.append(f'{code}: {_one(n, 300)}')
    if new:
        for ln in new: _log('warnings.log', ln, now)
        _save('warnings_seen.json', seen)
    return new


def sync(items, now=None):
    """Sends the whole list. Returns lines for the tick output (empty when all went well and nothing needs attention)."""
    _S['warnings'] = []
    if not active(): return []
    alloc = allocation(now)
    aut = autonomy(_S['project'] or (alloc or {}).get('default'), now)
    body = payload(items, now, aut, (alloc or {}).get('default'))
    body['tasks'], lines = not_allocated(body['tasks'], alloc, now)
    for attempt in (1, 2):
        try:
            _, res = _call('POST', '/agent/sync', body)
            break
        except PlanouError as e:
            drop = set(e.detail.get('source_keys') or []) if e.code == NOT_ALLOCATED else set()
            if attempt == 1 and e.status == 403 and drop:
                # the cached allocation was stale: the same batch once more without the refused new items, never a loop
                forget_allocation()
                lines += not_allocated_warning(e.detail.get('projects') or [], now)
                body['tasks'] = [t for t in body['tasks'] if t.get('source_key') not in drop]
                continue
            return lines + [_failed(e.message if e.status else e, now)]
    _ok()
    with _locked('state.json'):     # the answer is in: state.json is loaded, changed and saved under its lock
        st = _load('state.json', {})
        tasks = st.setdefault('tasks', {})
        prefix = _S['agent'] + ':'
        sent = {t['source_key']: t for t in body['tasks']}
        kept = _load('sent.json', {})
        for r in (res or {}).get('tasks') or []:
            code = (r.get('source_key') or '')[len(prefix):]
            t = sent.get(r.get('source_key')) or {}
            warnings = list(r.get('warnings') or [])
            notes = warnings + ([r['reason']] if r.get('reason') and r.get('result') != 'error' else [])
            if r.get('result') in ('created', 'updated', 'unchanged') and r.get('pid'):
                prev = tasks.get(code) or {}
                # what was sent tells a person's edit (task_changed) apart from the agent's own values
                entry = {'pid': r['pid'], 'version': r.get('version'), 'title': t.get('title'), 'due': t.get('due'),
                         'state': t.get('state') or prev.get('state'),
                         'project_state': t.get('project_state') if 'state' in t else prev.get('project_state')}
                for k in ('refining', 'backlog', 'self_sent'):
                    if prev.get(k): entry[k] = prev[k]
                if _in_backlog(notes): entry['backlog'] = True
                elif t.get('state') and 'state' not in (r.get('ignored_fields') or []): entry.pop('backlog', None)
                if t.get('assignee') == 'self': entry['self_sent'] = True
                tasks[code] = entry
                # the full body without the description (absent = left as it is), for a one-task sync (fila refinar)
                kept[code] = {k: v for k, v in t.items() if k not in ('description', 'base_version', 'assignee')}
            elif r.get('result') == 'conflict' and code in tasks:
                # what the agent wanted and did not go up: the local value an event's `changed` is compared with
                kept[code] = {k: v for k, v in t.items() if k not in ('description', 'base_version', 'assignee')}
            elif r.get('result') == 'ignored' and t.get('state') == 'closed' and code in tasks:
                # a close Planou ignores (the person deleted the task) is not sent again on every tick
                tasks[code]['state'] = 'closed'
            elif r.get('result') == 'error':
                lines.append(f'AVISO (planou): {code} recusada: {r.get("reason")}')
            # conflict: the version is NOT stored; the event carries it once the agent applied the person's change
            note_warnings(code, notes, now)
            for w in warnings:
                _S['warnings'].append(f'{code}: {w}')
                # a project_state the project no longer has: the cached states are stale, the next tick asks again
                if 'project_state' in str(w): forget_project_states()
        _save('state.json', st)
        _save('sent.json', {k: v for k, v in kept.items() if k in tasks})
        marks = _load('ready.json', {})      # ready.json too: mark_ready writes it under the same lock
        if any(c not in items for c in marks): _save('ready.json', {c: m for c, m in marks.items() if c in items})
    for code, files in (_S.get('cited') or {}).items():      # outside the lock: the attachments go over the network
        if code in tasks: register_files(code, files, now)
    lines += flush_attachments(now)
    return lines


def sync_warnings():
    """The warnings of the last sync (["<code>: project_state \"X\" not found in project CAR; ..."]), for the agent to
    report in its own way (the sync itself prints none: a name the project lost would print on every tick; every
    warning also goes to cache/planou/warnings.log once a day, note_warnings)."""
    return list(_S['warnings'])


@_holding('state.json')
def mark_ready(ref, reason=None, take=False, now=None):
    """The agent marks a task ready by itself (rule b: the user asked for it, within the autonomy, nothing to decide,
    complete data): cache/planou/ready.json {code: {reason, self, at}}; the next sync sends the reason (and
    `"assignee": "self"` once with `take`). `ref`: the task code or its pid. reason None or '-' drops the mark.
    Returns the code."""
    known = _load('state.json', {}).get('tasks') or {}
    r = str(ref or '').strip()
    code = next((c for c, v in known.items() if str(v.get('pid') or '').upper() == r.upper()), r)
    if not code: raise PlanouError(0, 'which_task', 'diga o código ou o PID da tarefa')
    marks = _load('ready.json', {})
    if not reason or reason == '-':
        marks.pop(code, None)
    else:
        text = _one(reason)
        if looks_secret(text): raise PlanouError(0, 'secret', 'o motivo parece ter um segredo: reescreva sem ele')
        marks[code] = {'reason': text, 'self': bool(take), 'at': (now or datetime.now(timezone.utc)).isoformat()}
        if take and code in known: known[code].pop('self_sent', None)
    _save('ready.json', marks)
    if take and code in known:
        st = _load('state.json', {}); st.setdefault('tasks', {})[code] = known[code]; _save('state.json', st)
    return code


_PID = re.compile(r'^[A-Z]{2,6}\d{3,}$')


def ready_now(code, now=None):
    """Sends a `pronta` mark right away, in a one-task sync of the agent's own task (its source_key): the kept body (the
    last one the full sync sent, as `fila refinar` does) with `state: todo`, the mark's `ready_reason` and, with
    --self, `"assignee": "self"`. An instance with no sources has no full sync, so the mark would never go otherwise.
    Returns {"result", "pid", "state_ignored", "assignee_ignored", "notes"}, or None when there is nothing to send here:
    no mark, Planou off, or a task this agent never synced (a new source item goes on the next full sync; a task the
    person created is never touched by the sync)."""
    if not active(): return None
    mk = _load('ready.json', {}).get(code)
    entry = (_load('state.json', {}).get('tasks') or {}).get(code)
    if not isinstance(mk, dict) or not mk.get('reason') or not entry or not entry.get('pid'): return None
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    body = _load('sent.json', {}).get(code) or {}
    t = {k: v for k, v in body.items() if k not in ('state', 'resolution', 'completed_at', 'waiting', 'ready_reason',
                                                    'project_state', 'assignee')}
    t.update({'source_key': f'{_S["agent"]}:{code}', 'state': 'todo', 'ready_reason': mk['reason'],
              'base_version': entry.get('version')})
    if not t.get('title') and entry.get('title'): t['title'] = entry['title']
    if _S['project'] and not t.get('project'): t['project'] = _S['project']
    if mk.get('self'): t['assignee'] = 'self'
    _, res = _call('POST', '/agent/sync', {'agent': _S['agent'], 'generated_at': stamp, 'tasks': [t]})
    r = next((x for x in (res or {}).get('tasks') or [] if x.get('source_key') == t['source_key']), None) or {}
    ignored = r.get('ignored_fields') or []
    notes = list(r.get('warnings') or []) + ([r['reason']] if r.get('reason') else [])
    out = {'result': r.get('result'), 'pid': r.get('pid') or entry.get('pid'), 'notes': notes,
           'state_ignored': 'state' in ignored or _in_backlog(notes),
           'assignee_ignored': bool(mk.get('self')) and ('assignee' in ignored or not (r.get('assignee') or {}).get('self'))}
    if r.get('result') in ('created', 'updated', 'unchanged') and r.get('pid'):
        with _locked('state.json'):
            st = _load('state.json', {})
            e = (st.get('tasks') or {}).get(code)
            if e is not None:
                e.update(pid=r['pid'], version=r.get('version'))
                if not out['state_ignored']: e.pop('backlog', None); e['state'] = 'todo'
                if mk.get('self') and not out['assignee_ignored']: e['self_sent'] = True
                _save('state.json', st)
        if notes: note_warnings(code, notes, now)
    return out


# ---------------------------------------------------------------- allocation (Planou 0.63.0)

PROJECTS_TTL = timedelta(minutes=5)
NOT_ALLOCATED = 'project_not_allocated'
NOT_ALLOCATED_LINE = 'AVISO (planou): projeto {p} não alocado: aloque o funcionário no Planou'


def allocation(now=None, ttl=PROJECTS_TTL):
    """{'default': 'PLN', 'keys': ['PLN', ...]}: the projects this employee is allocated to (GET /agent/projects), from
    cache/planou/projects.json while younger than `ttl`. None when unknown (Planou off, the call failed, an older Planou
    without the route, an empty list): the caller then filters nothing. A failure is cached too."""
    if not active(): return None
    now = now or datetime.now(timezone.utc)
    hit = _load('projects.json', {})
    try: fresh = now - datetime.fromisoformat(hit.get('fetched_at')) < ttl
    except (TypeError, ValueError): fresh = False
    if not fresh:
        try:
            _, data = _call('GET', '/agent/projects')
        except PlanouError:
            data = None
        hit = {'fetched_at': now.isoformat(), 'data': data if isinstance(data, dict) else None}
        _save('projects.json', hit)
    data = hit.get('data') or {}
    projects = [p for p in data.get('projects') or [] if isinstance(p, dict) and p.get('key')]
    if not projects: return None
    default = (data.get('default_project') or next((p['key'] for p in projects if p.get('is_default')), None)
               or projects[0]['key'])
    return {'default': str(default), 'keys': [str(p['key']) for p in projects]}


def forget_allocation():
    """Drops the cached allocation: the next allocation() asks Planou."""
    _save('projects.json', {})


def allocated(project, alloc):
    """False only when the allocation is known and `project` (a key, any case) is not in it."""
    return not alloc or not project or state_key(project) in {state_key(k) for k in alloc['keys']}


def not_allocated_warning(projects, now=None):
    """One line per project not allocated, once per project and day (also in warnings.log)."""
    now = now or datetime.now(timezone.utc)
    day = now.astimezone().date().isoformat()
    seen = _load('not_allocated_seen.json', {})
    if seen.get('day') != day: seen = {'day': day, 'projects': []}
    lines = []
    for p in projects:
        if not p or state_key(p) in seen['projects']: continue
        seen['projects'].append(state_key(p))
        lines.append(NOT_ALLOCATED_LINE.format(p=one_line(p, 40)))
        _log('warnings.log', lines[-1], now)
    if lines: _save('not_allocated_seen.json', seen)
    return lines


def not_allocated(tasks, alloc, now=None):
    """(tasks to send, warning lines): a new task (no pid known here) of a project not allocated stays out; a task Planou
    already has goes in any project."""
    if not alloc: return tasks, []
    known = _load('state.json', {}).get('tasks') or {}
    prefix = _S['agent'] + ':'
    keep, out = [], []
    for t in tasks:
        code = (t.get('source_key') or '')[len(prefix):]
        if allocated(t.get('project'), alloc) or (known.get(code) or {}).get('pid'):
            keep.append(t)
        elif t['project'] not in out:
            out.append(t['project'])
    return keep, not_allocated_warning(out, now)


# ---------------------------------------------------------------- project states

STATES_TTL = timedelta(hours=1)
STATE_CATEGORIES = ('backlog', 'todo', 'in_progress', 'waiting', 'blocked', 'closed')


def state_key(name):
    """A state name as Planou compares it: no case, no accents, single spaces."""
    n = unicodedata.normalize('NFKD', str(name or ''))
    return ' '.join(''.join(c for c in n if not unicodedata.combining(c)).casefold().split())


def project_states(project=None, now=None, ttl=STATES_TTL):
    """{project, project_id, customized, states: [{name, category, is_default, color}]} of the project (the configured one
    by default), from cache/planou/project_states.json while younger than `ttl`, else from Planou. None when there is no
    project, Planou is off or the call failed (an older Planou has no such route): the caller sends only `state`. A
    failure is cached too, so a missing route is not asked again on every tick."""
    project = project or _S['project']
    if not project or not active(): return None
    now = now or datetime.now(timezone.utc)
    cache = _load('project_states.json', {})
    hit = cache.get(project) or {}
    try: fresh = now - datetime.fromisoformat(hit.get('fetched_at')) < ttl
    except (TypeError, ValueError): fresh = False
    if fresh: return hit.get('data')
    try:
        _, data = _call('GET', f'/agent/projects/{urllib.parse.quote(project)}/states')
        if not isinstance(data, dict) or not isinstance(data.get('states'), list): data = None
    except PlanouError:
        data = None
    cache[project] = {'fetched_at': now.isoformat(), 'data': data}
    _save('project_states.json', cache)
    return data


def forget_project_states(project=None):
    """Drops the cached states (all projects by default): the next project_states() asks Planou."""
    cache = _load('project_states.json', {})
    if project: cache.pop(project, None)
    else: cache = {}
    _save('project_states.json', cache)


def autonomy(project=None, now=None):
    """The project's autonomy ('autonomous', 'semi_autonomous' or 'manual'), from the cached project_states() answer (one
    call an hour). None when it is unknown (no project, Planou off, an older Planou): the sync then acts as in a
    semi-autonomous project (a reason only when creating; nothing leaves backlog by itself)."""
    data = project_states(project, now)
    a = (data or {}).get('autonomy')
    return a if a in AUTONOMIES else ('semi_autonomous' if data else None)


# ---------------------------------------------------------------- heartbeat and events

# Agents whose runner delivers the `deliver` events (runner.sh of work-watch and job-scout: light loop + ack-delivered).
# Every agent of the agent plugin (its folder under ~/.config/agent, watch_core.config.agent_root) delivers too: the
# plugin has one runner. Any other agent (the `team` launcher sends session_closed for all of them) declares nothing, so Planou never offers
# the reply box to an agent that would leave the message unread.
DELIVERING_RUNNERS = ('work-watch-', 'job-scout')


# Capabilities an agent declares only when its own config turns the feature on: capability -> key of the "planou" block.
OPT_IN = {'task_queue': 'task_queue', 'task_release': 'task_queue'}

# Capabilities only an agent of the agent plugin declares: the retro behavior (behaviors/retro) lives there. Planou
# (PLN0208) invites to a ceremony only the agents that declare `ceremony`; a runner that does not handle the events
# would ack and drop them, and the round, the vote and the minutes would wait until due for it. The same for
# `task_comment` (PLN0240): how to answer a comment with @ is in the agent plugin's SKILL.md, and for `ceremony_daily`
# (PLN0127/PLN0283): the daily calls only the agents that declare it, the rest show "sem explicação do agente".
AGENT_ONLY = ('ceremony', 'ceremony_daily', 'task_comment')


def _opted_in(key):
    """True when the agent's own config says "planou": {<key>: true}. Read here (not in configure) so the three paths
    that send a heartbeat (the runner's CLI, work-watch's and job-scout's heavy tick) always agree: the server replaces
    the capability list on every heartbeat."""
    if not _S['root'] or not _S['agent']: return False
    pc = agent_settings(_S['root'], _S['agent'])
    return bool(pc) and pc.get(key) is True


def queue_enabled():
    return _opted_in(OPT_IN['task_queue'])


def capabilities():
    agent = str(_S['agent'] or '')
    legacy = any(agent == r or (r.endswith('-') and agent.startswith(r)) for r in DELIVERING_RUNNERS)
    agent_layout = config.is_agent_layout(_S['root'])
    if not legacy and not agent_layout: return []
    return [c for c in CAPABILITIES if (c not in OPT_IN or _opted_in(OPT_IN[c])) and (c not in AGENT_ONLY or agent_layout)]


def session_fields(session_id=None):
    """session_id and remote_control_url of the agent's session, as the runner last found them
    (cache/planou/session.json, written by watch_core.transcript). A different session_id passed in (the launcher
    closing a session) goes alone: the cached link belongs to another session."""
    info = _load('session.json', {})
    if session_id and session_id != info.get('session_id'):
        return {'session_id': session_id}
    return {k: info[k] for k in ('session_id', 'remote_control_url') if info.get(k)}


def heartbeat(phase, next_tick=None, broken_sources=(), session_id=None, now=None):
    if not active() or phase not in PHASES: return []
    now = now or datetime.now(timezone.utc)
    reaped = reap_workers(now) if phase == 'tick' else []
    body = {'phase': phase, 'broken_sources': list(broken_sources), 'host': socket.gethostname(), 'plugin_version': _S['plugin_version']}
    caps = capabilities()
    if caps: body['capabilities'] = caps       # absent = the server keeps what it had (the launcher's session_closed)
    if next_tick: body['next_tick_at'] = next_tick.isoformat() if hasattr(next_tick, 'isoformat') else str(next_tick)
    body.update(session_fields(session_id))
    due = _tools_due(now)
    if due: body['tools'] = due[1]            # absent = the server keeps the list (a `tick` also confirms it)
    links = _links_due(now)
    if links: body['links'] = links[1]        # absent = the server keeps the rules
    docs = _docs_due(now)
    if docs: body['docs'] = docs[1]           # absent = the server keeps the manifest
    try:
        _, res = _call('POST', '/agent/heartbeat', body)
    except PlanouError as e:
        if docs and e.status == 422 and e.code in DOCS_REFUSED and all(f.startswith('docs') for f in e.fields or ['docs']):
            # Planou answers the tools and the links refusals first: here both were taken.
            if due: _save('tools_sent.json', {'sig': due[0], 'at': now.isoformat()})
            if links: _save('links_sent.json', {'sig': links[0], 'at': now.isoformat()})
            return reaped + _docs_refused(e, docs[0], now)
        if due and e.status == 422 and e.code in ('secret_refused', 'invalid_tools'):
            return reaped + _tools_refused(e, due[0], now)       # the sign of life counted: Planou is fine, the list is not
        if links and e.status == 422 and e.code == LINKS_REFUSED:
            if due: _save('tools_sent.json', {'sig': due[0], 'at': now.isoformat()})   # checked first: it was taken
            return reaped + _links_refused(e, links[0], now)
        return reaped + [_failed(e.message if e.status else e)]
    _ok()
    if phase == 'session_closed': _workers_session_closed(session_id or _session_now(), now)
    if due: _save('tools_sent.json', {'sig': due[0], 'at': now.isoformat()})
    if links: _save('links_sent.json', {'sig': links[0], 'at': now.isoformat()})
    if docs: _docs_taken(docs[0], now)
    told = pause_from_heartbeat(res, phase, now)
    return reaped + told + (flush_attachments(now) if phase == 'tick' else [])


# What the agent does with each event type (one entry per type; a new type from Planou is one more entry):
#   wake        the light loop prints it in `== PLANOU (n)` and wakes the session (the person answered something, handed
#               a task over or wrote to the agent). The person's edits to the agent's tasks (completed, reopened,
#               changed, deleted) wait for the heavy tick, which applies them to the state anyway (spec 13.2: waking for
#               every edit would cost tokens).
#   deliver     only the light loop delivers it, and it is acknowledged only after the runner printed it (ack-delivered);
#               the heavy tick never acknowledges past one not delivered yet
#   capability  the capability declared in the heartbeat: without it Planou does not offer the feature (a runner that does
#               not know the event would acknowledge it unread); OPT_IN ones also need the agent's config
EVENTS = {
    'decision_answered': {'wake': True}, 'approved': {'wake': True}, 'rejected': {'wake': True},
    'draft_sent': {'wake': True}, 'draft_discarded': {'wake': True}, 'alert_acknowledged': {'wake': True},
    'task_assigned': {'wake': True}, 'notify_agent': {'wake': True}, 'comment': {'wake': True},
    'user_message': {'wake': True, 'deliver': True, 'capability': 'user_message'},
    'task_queued_for_agent': {'wake': True, 'deliver': True, 'capability': 'task_queue'},
    'task_released': {'wake': True, 'deliver': True, 'capability': 'task_release'},
    'task_dequeued': {'wake': True},
    # "Pedir ajuste" (Planou 0.38.0): wakes the session with the person's text; not `deliver`: the text is kept in
    # cache/planou/changes.json and printed again with the redelivery, so the heavy tick may apply and print it too
    'task_changes_requested': {'wake': True},
    # cost-cap pause (Planou 0.42.0): wakes the session with `== PAUSADO` / `== DESPAUSADO`; the state is kept in
    # cache/planou/pause.json (pause_note), so the heavy tick may print it too when the light loop did not
    'agent_paused': {'wake': True}, 'agent_resumed': {'wake': True},
    # project ceremonies (Planou PLN0126, the retro): the invited agent sends its contribution, the facilitator the
    # minutes; the payload is kept in cache/planou/ceremonies.json (the heavy tick acknowledges and drops the event);
    # ceremony_vote (PLN0195) opens the voting round of a retro that has one; the heartbeat declares `ceremony`
    # (PLN0208), without it Planou leaves the agent out of the meeting
    'ceremony_started': {'wake': True, 'capability': 'ceremony'}, 'ceremony_facilitate': {'wake': True, 'capability': 'ceremony'},
    'ceremony_vote': {'wake': True, 'capability': 'ceremony'},
    # the backlog refinement (PLN0210): ceremony_refine brings the project's backlog to every invited agent, which sends
    # its suggestions (estimate, split, agent_can_do); ceremony_refine_facilitate brings them all to the facilitator
    'ceremony_refine': {'wake': True, 'capability': 'ceremony'},
    'ceremony_refine_facilitate': {'wake': True, 'capability': 'ceremony'},
    # the project daily (Planou PLN0127): the minutes come from the data, the agent is called only to explain its tasks
    # in Impedida, one line each (`daily explicar`); its own capability, so an older runner is never called for it
    'ceremony_daily_explain': {'wake': True, 'capability': 'ceremony_daily'},
    # a person called this agent with @name in a task comment (Planou PLN0240): delivered like a message, acknowledged
    # only after the session saw it (Planou calls only agents with `task_comment`, so a mention is never dropped unread);
    # the session answers by comment (`comentario PID --text -`), which is not taking the task
    'task_comment_mentioned': {'wake': True, 'deliver': True, 'capability': 'task_comment'},
    # a proposal of this agent was applied, rejected, failed or reverted (Planou 0.53.0)
    'proposal_changed': {'wake': True},
    # an edit of the Papel tab (instructions, context, the behaviors on): not a wake type, the light loop never prints it
    # unapplied; the heavy tick hands it to the handler of set_docs_handler() (docs_ack answers Planou), which prints
    # `== PAPEL MUDOU` for the session. Without a handler (a plugin whose manifest is read only) it is acknowledged unread
    'agent_docs_changed': {},
    'task_completed': {}, 'task_reopened': {}, 'task_changed': {}, 'task_deleted': {}, 'task_restored': {},
}
WAKE_TYPES = tuple(k for k, v in EVENTS.items() if v.get('wake'))
DELIVER_TYPES = tuple(k for k, v in EVENTS.items() if v.get('deliver'))
CAPABILITIES = tuple(dict.fromkeys(v['capability'] for v in EVENTS.values() if v.get('capability')))
QUEUE_EVENTS = ('task_queued_for_agent', 'task_released', 'task_dequeued')
QUEUE_IN = ('task_queued_for_agent', 'task_released')
TASK_EVENTS = ('task_completed', 'task_reopened', 'task_changed', 'task_deleted', 'task_restored')
# the task fields a `changed` may carry ({field: {from, to}}, same format as the payload); the rest of the payload is the
# whole task as Planou has it
CHANGED_FIELDS = ('state', 'project_state', 'resolution', 'title', 'priority', 'due', 'deadline', 'estimate_h',
                  'remaining_h', 'completed_h', 'assignee')
SEEN_DAYS = 7
MESSAGE_PREFIX = '-- MENSAGEM do usuario pelo Planou'
QUEUE_PREFIX = '-- FILA'
CHANGES_EVENT = 'task_changes_requested'
CHANGES_PREFIX = '-- AJUSTE PEDIDO'
DAILY_EVENT = 'ceremony_daily_explain'
CEREMONY_EVENTS = ('ceremony_started', 'ceremony_facilitate', 'ceremony_vote', 'ceremony_refine', 'ceremony_refine_facilitate',
                   DAILY_EVENT)
CEREMONY_KEYS = {'ceremony_started': 'started', 'ceremony_facilitate': 'facilitate', 'ceremony_vote': 'vote',
                 'ceremony_refine': 'started', 'ceremony_refine_facilitate': 'facilitate', DAILY_EVENT: 'started'}
CEREMONY_PREFIX = '-- CERIMONIA'
COMMENT_EVENT = 'task_comment_mentioned'
COMMENT_PREFIX = '-- COMENTARIO'
COMMENT_TEXT_MAX = 10000
CEREMONY_GUIDE = 'behaviors/retro/BEHAVIOR.md do plugin agent'
REFINE_GUIDE = 'behaviors/refinement/BEHAVIOR.md do plugin agent'
DAILY_GUIDE = 'behaviors/daily/BEHAVIOR.md do plugin agent'
DAILY_TEXT_MAX = 300
DAILY_LINES = 12          # blocked tasks listed under the -- CERIMONIA daily line; the rest with `daily ver`
TOKENS_MAX = 100_000_000
CEREMONIES_KEPT = 20


def _inbox():
    ib = _load('inbox.json', {})
    ib.setdefault('events', {}); ib.setdefault('printed', {}); ib.setdefault('pending_print', [])
    return ib


def _messages():
    """cache/planou/messages.json: the person's messages already delivered to the session ({message_id: {seq, at}}, kept
    SEEN_DAYS) and the seqs still to acknowledge (to_ack: printed, ack not confirmed yet)."""
    m = _load('messages.json', {})
    if not isinstance(m.get('delivered'), dict): m['delivered'] = {}
    if not isinstance(m.get('to_ack'), list): m['to_ack'] = []
    return m


def _message_id(ev):
    mid = (ev.get('payload') or {}).get('message_id')
    return str(mid) if mid not in (None, '') else f'seq-{ev.get("seq")}'


def delivered(ev):
    """True for a `deliver` event (a person's message) whose message_id already reached the session."""
    return ev.get('type') in DELIVER_TYPES and _message_id(ev) in _messages()['delivered']


def fetch(next_tick=None, poll_s=None, wait=None, info=None):
    """Brings the events not acknowledged yet into the local inbox (cache/planou/inbox.json) and returns the inbox.
    Raises PlanouError. The server keeps handing out an event until the heavy tick acknowledges it after applying it;
    the inbox (seq -> event) makes the light loop and the heavy tick see the same thing.

    wait=N (long poll): the server holds the request until an event comes or N seconds pass. `info`, a dict, gets
    'waited': the seconds the server honoured, or None when it ignored ?wait= (a Planou without long polling)."""
    ib = _inbox()
    known = [int(k) for k in ib['events']] + [int(k) for k in ib['printed']]
    q = []
    if known: q.append(f'after={max(known)}')
    if poll_s: q.append(f'poll_s={int(poll_s)}')
    wait = max(0, min(int(wait), MAX_WAIT_S)) if wait else 0
    if wait: q.append(f'wait={wait}')
    if next_tick:
        nt = datetime.fromtimestamp(int(next_tick), timezone.utc) if str(next_tick).isdigit() else next_tick
        q.append('next_tick_at=' + urllib.parse.quote(nt.isoformat() if hasattr(nt, 'isoformat') else str(nt)))
    headers = {}
    status, res = _call('GET', '/agent/events' + ('?' + '&'.join(q) if q else ''), timeout=(wait + 15) if wait else None,
                        headers=headers)
    if info is not None:
        raw = headers.get(WAIT_HEADER.lower())
        info['waited'] = int(raw) if raw is not None and str(raw).isdigit() else None
    for ev in ([] if status == 204 or not res else res.get('events') or []):
        ib['events'].setdefault(str(ev['seq']), ev)
        if ev.get('type') in CEREMONY_EVENTS: remember_ceremony(ev)
    _save('inbox.json', ib)
    return ib


def poll():
    """Events waiting for this agent, oldest first ([] when there are none or on failure). Also the "poll" sign of life."""
    if not active(): return []
    try:
        ib = fetch()
    except PlanouError:
        return []
    return [ib['events'][k] for k in sorted(ib['events'], key=int)]


def _when(p):
    """HH:MM of the answer. .NET sends 7 fractional digits, which fromisoformat (3.10) does not take: cut to 6."""
    raw = str(p.get('at') or p.get('effective_at') or p.get('ts') or '').replace('Z', '+00:00')
    raw = re.sub(r'(\.\d{6})\d+', r'\1', raw)
    try:
        return datetime.fromisoformat(raw).astimezone().strftime('%H:%M')
    except ValueError:
        return '?'


_CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')


def message_text(text):
    """The person's text for the session: control characters out, the first line after the prefix and every other line
    indented, so no line of it starts like a runner token (`== PLANOU (`, `FONTE QUEBRADA`, `EXIT=`)."""
    lines = _CONTROL.sub('', str(text or '').replace('\r\n', '\n').replace('\r', '\n').replace('\t', '    ')).strip().split('\n')
    return '\n'.join([lines[0].strip()] + ['    ' + ln.rstrip() for ln in lines[1:]])


def one_line(text, limit=200):
    """Free text from the person (a task title) on a single line, control characters out."""
    return ' '.join(_CONTROL.sub(' ', str(text or '')).split())[:limit]


DEQUEUE_REASON = {'person': 'tirada da fila pelo usuario', 'closed': 'concluida', 'deleted': 'apagada',
                  'reassigned': 'passada a outro agente'}


QUEUED_BY = {'auto': 'vaga livre, projeto autonomo', 'agent': 'o agente se atribuiu', 'state': 'veio pela coluna do projeto'}


def queued_by(p):
    """Who put the task in the queue, for the queue line: the person (also when absent, an older Planou), the idle pull
    of an autonomous project (`auto`, with Planou's `reason`), the agent itself or a column owner (`state`)."""
    by = p.get('queued_by')
    if by == 'auto': return one_line(p.get('reason'), 80) or QUEUED_BY['auto']
    return QUEUED_BY.get(by, 'passada pelo usuario')


def _by_agent(who):
    return isinstance(who, dict) and who.get('kind') == 'agent'


def _requested_in_name(v):
    return one_line(v.get('name') if isinstance(v, dict) else v, 60) or None


def requested_by(who, kind=None, role=None, where=None):
    """Who asked for an adjustment, for the lines: `<role> <name>` when an agent sent the task back (Planou 0.43.0), the
    role being the event's `requested_by_role` (the agent's Funcao, Planou 0.49.0), else the column where it was asked
    (`requested_in.name`), else `revisor` (an older Planou); `pessoa` for the person's button (also when absent)."""
    if isinstance(who, dict): kind, name = who.get('kind'), one_line(who.get('name'), 80)
    else: name = one_line(who, 80)
    if kind == 'agent':
        label = one_line(role, 60) or _requested_in_name(where) or 'revisor'
        return f'{label} {name}' if name else label
    return 'pessoa'


def handoff_line(p, pr=None):
    """The tail of a released task that came by another agent's handoff (Planou PLN0106: `handed_off_by` {name, role,
    note} and `project_state` in the queue payload), or '' without it (the person or Planou queued it, or an older
    Planou). The part of this agent is the column's, not new work: a reviewer reviews (code-review), the owner of the
    release column after the review puts the branch in the release queue (batch-release). The note of the handoff is the
    branch when there is no PR (batch release)."""
    by = p.get('handed_off_by')
    if not isinstance(by, dict) or not one_line(by.get('name'), 80):
        return ''
    who = ' '.join(x for x in (one_line(by.get('role'), 60), one_line(by.get('name'), 80)) if x)
    column = one_line(p.get('project_state'), 60)
    note = one_line(by.get('note'), 300)
    return (f'passada por {who}' + (f' para a coluna {column}' if column else '') + (f', com a PR: {pr}' if pr else '')
            + (f'; nota: {note}' if note else '')
            + ' -> nao e tarefa nova e nao pede worker de codigo: `fila ver` e seguir o comportamento da coluna (revisao:'
            + ' code-review, sem procurar a PR pelo PID; release: batch-release, `--release-queue add <branch> <PID>` e'
            + ' `fila in_review`)')


# ---------------------------------------------------------------- one instance does the whole cycle (PLN0281)
# The same agent may own the dev column and the review or QA column after it. Planou then hands the task to itself: the
# entry is queued again with `queued_by: "state"` and the next column, but without `handed_off_by` and without the note
# (only the PR goes on), and request-changes answers 409 no_previous_owner (there is no other agent to send it back to).
# The plugin keeps its own record of that handoff (cache/planou/self_handoffs.json), so the queue line tells the column's
# part from new work and the review or QA goes to a NEW worker, never the one that delivered (no self-approval). The
# record has no expiry: it goes only when the task leaves the agent (done or handoff to someone else, dequeued), and it
# counts only while the task is in the column it was handed to, so a review that waits long is still a review.
INDEPENDENT_ROLES = {'code-review': 'revisao', 'qa': 'QA'}


def column_role(name):
    """The behavior a column calls for, by its name: 'qa' (QA, teste, homologacao, qualidade), 'code-review' (revisao,
    review), 'batch-release' (release, publicacao, deploy), or None when the name says none of them."""
    n = state_key(name)
    if not n: return None
    if re.search(r'\bqa\b|\btest|homolog|qualidade|quality', n): return 'qa'
    if 'revis' in n or 'review' in n: return 'code-review'
    if 'release' in n or 'publica' in n or 'deploy' in n: return 'batch-release'
    return None


def _self_handoffs():
    d = _load('self_handoffs.json', {})
    tasks = d.get('tasks') if isinstance(d, dict) and isinstance(d.get('tasks'), dict) else {}
    return {k: v for k, v in tasks.items() if isinstance(v, dict)}


def _handed_to_me(entry, res, now=None):
    """True when the handoff answer gives the task back to this same agent: the assignee is the owner marked `is_me` in
    the project's states, or the new column is one this agent owns."""
    who = res.get('assignee') if isinstance(res.get('assignee'), dict) else {}
    if who.get('kind') != 'agent': return False
    data = project_states((entry or {}).get('project') or _S['project'], now, ttl=HANDOFF_STATES_TTL)
    col = state_key(res.get('project_state'))
    for s in (data or {}).get('states') or []:
        o = (s.get('owner') or {}) if isinstance(s, dict) else {}
        if o.get('is_me') is not True: continue
        if (who.get('id') and o.get('id') == who.get('id')) or (col and state_key(s.get('name')) == col): return True
    return False


def _column_before(entry, res, now=None):
    """The column of this agent whose "Ao terminar, vai para" is the new one (where the handoff came from), or None."""
    data = project_states((entry or {}).get('project') or _S['project'], now, ttl=HANDOFF_STATES_TTL)
    col = state_key(res.get('project_state'))
    for s in (data or {}).get('states') or []:
        if isinstance(s, dict) and ((s.get('owner') or {}).get('is_me') is True) and col and state_key(s.get('next_state')) == col:
            return one_line(s.get('name'), 60) or None
    return None


def note_self_handoff(tid, res, note=None, entry=None, now=None):
    """Keeps the record of a handoff to this same agent (see above): column, PR, the note (the branch, without a PR) and
    the column it came from."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    before = one_line((entry or {}).get('project_state'), 60) or _column_before(entry, res, now)   # no lock over HTTP
    with _locked('self_handoffs.json'):
        tasks = _self_handoffs()
        prev = tasks.get(str(tid)) or {}
        tasks[str(tid)] = {'pid': res.get('pid') or (entry or {}).get('pid'), 'column': one_line(res.get('project_state'), 60) or None,
                           'from': prev.get('column') or before, 'pr_url': res.get('pr_url') or (entry or {}).get('pr_url'),
                           'note': one_line(note, 300) or prev.get('note'), 'at': stamp, 'asks': []}
        _save('self_handoffs.json', {'tasks': tasks})


def forget_self_handoff(tid, pid=None):
    with _locked('self_handoffs.json'):
        tasks = _self_handoffs()
        drop = [k for k, v in tasks.items() if k == str(tid) or (pid and v.get('pid') == pid)]
        if drop:
            for k in drop: tasks.pop(k)
            _save('self_handoffs.json', {'tasks': tasks})


def self_handoff(p, now=None):
    """The record of a handoff of this task to this same agent (by task_id, else by PID), or None. Only while the task
    is in the column it was handed to (`project_state` of the payload or of the queue entry): a task the person moved
    back to the dev column, or sent elsewhere, is not the column's part any more."""
    tasks = _self_handoffs()
    tid, pid = str(p.get('task_id') or ''), one_line(p.get('pid'), 20).upper()
    rec = tasks.get(tid) or next((v for v in tasks.values() if pid and str(v.get('pid') or '').upper() == pid), None)
    if not rec or state_key(p.get('project_state')) != state_key(rec.get('column')): return None
    return rec


def self_handoff_line(p, pr=None):
    """The tail of a released task that this same agent handed to itself (another column of its own), or ''."""
    rec = self_handoff(p)
    if not rec: return ''
    column = one_line(p.get('project_state') or rec.get('column'), 60)
    note = one_line(rec.get('note'), 300)
    role = column_role(column)
    head = ('passada por voce mesmo (mesma instancia que desenvolve)' + (f' para a coluna {column}' if column else '')
            + (f', com a PR: {pr}' if pr else '') + (f'; nota: {note}' if note else ''))
    if role in INDEPENDENT_ROLES:
        what = INDEPENDENT_ROLES[role]
        return (f'{head} -> nao e tarefa nova: `fila ver`, `fila started` e delegar a {what} a um worker NOVO ({role}, '
                f'`--brief --role {role}`; contexto limpo: so o pedido, a PR ou a branch e o criterio; nunca o worker que '
                f'entregou nem SendMessage a ele; a sessao nao faz a {what}); o parecer registra "{what} independente '
                '(worker novo)"; ajuste pedido: `fila ajuste` responde RETRABALHO PROPRIO')
    if role == 'batch-release':
        return (f'{head} -> nao e tarefa nova e nao pede worker de codigo: `fila ver` e seguir o batch-release '
                '(`--release-queue add <branch> <PID>` e `fila in_review`)')
    return ''          # any other own column (an analysis before A fazer, the dev's own): the usual lines, it is work


def note_self_rework(tid, rec, text, now=None):
    """Keeps the ask of an own review or QA with the record (the rework brief), the text cut like request-changes'."""
    with _locked('self_handoffs.json'):
        tasks = _self_handoffs()
        key = str(tid) if str(tid) in tasks else next((k for k, v in tasks.items() if v.get('pid') == rec.get('pid')), None)
        if key is None: return
        tasks[key].setdefault('asks', []).append({'text': clean_text(text or '', CHANGES_TEXT_MAX),
                                                  'at': (now or datetime.now(timezone.utc)).isoformat()})
        _save('self_handoffs.json', {'tasks': tasks})


def self_rework_comment(rec, text):
    """The comment that leaves the ask of an own review or QA on the task (PLN0281): without another agent there is no
    request-changes, and without a PR (batch release) nothing else shows it in Planou. Under "minimum" only a neutral
    line goes up (the text stays in the session and in self_handoffs.json)."""
    what = INDEPENDENT_ROLES.get(column_role(rec.get('column')), 'revisao')
    head = f'Ajuste pedido na {what} independente (worker novo, mesma instancia que desenvolve)'
    if _S['confidentiality'] == 'minimum':
        return f'{head}: o parecer esta ' + ('na PR.' if rec.get('pr_url') else 'com o agente.')
    return f'{head}; volta como retrabalho na mesma branch' + (' e PR' if rec.get('pr_url') else '') + f'.\n\n{str(text or "").strip()}'


def review_worker_after(ref, rec):
    """True when a worker of role other was opened for this task (`fila worker-start --role other`) after the handoff
    to the own review or QA column, or after its last ask for changes: the independent worker that `fila done` stands for."""
    since = max([rec.get('at') or ''] + [a.get('at') or '' for a in rec.get('asks') or []])
    refs = {_norm_ref(x) for x in ref if x}
    for v in _workers()['workers'].values():
        if (isinstance(v, dict) and v.get('role') == 'other' and (v.get('started_at') or '') >= since
                and refs & {_norm_ref(t) for t in v.get('tasks') or []}):
            return True
    return False


def self_rework_text(pid, rec, text):
    """What to do when a review or QA of this same agent asks for changes (request-changes has no one to send back to)."""
    column, came = one_line(rec.get('column'), 60), one_line(rec.get('from'), 60)
    role = column_role(column)
    what = INDEPENDENT_ROLES.get(role, 'revisao')
    pr = one_line(rec.get('pr_url'), 300)
    return (f'RETRABALHO PROPRIO: {pid} pediu ajuste na {what} independente desta mesma instancia (o Planou nao devolve '
            f'ao proprio agente; a tarefa fica na coluna {column or "atual"}, com o agente). Retrabalho normal: worker de dev '
            f'na mesma branch e na mesma PR{f" ({pr})" if pr else ""} atendendo o ajuste (`fila worker-start {pid} --role dev`), '
            f'sem `fila in_review`; quando ele voltar, um worker NOVO de {role or "code-review"} confere so o que mudou desde o '
            f'commit do parecer; aprovado: `fila done {pid}`. Ajuste: {one_line(text, 300)}'
            + (f' (veio de {came})' if came else ''))


def _author(a):
    """The name of a comment's author ({kind, id, name, display_name?}): the name given, else the canonical one."""
    a = a if isinstance(a, dict) else {}
    return one_line(a.get('display_name') or a.get('name'), 80) or ('agente' if a.get('kind') == 'agent' else 'pessoa')


def comment_line(p):
    """The lines of a comment with @ to this agent (task_comment_mentioned): the person's text and how to answer."""
    pid, cid = one_line(p.get('pid'), 20), one_line(p.get('comment_id'), 40)
    ps = p.get('project_state')
    col = one_line(ps.get('name') if isinstance(ps, dict) else ps, 60)
    title = one_line(p.get('title'))
    return (f'{COMMENT_PREFIX} {pid} de {_author(p.get("author"))} ({_when(p)}{", editado" if p.get("edited") else ""}): '
            f'{message_text(p.get("text"))}\n    -> chamado com @ na tarefa {pid} "{title}"' + (f' ({col})' if col else '')
            + f": responder pelo comentario: `cat <<'FIM' | $PL comentario {pid} --text -"
            + (f' --reply-to {cid}' if cid else '') + f'`, a resposta nas linhas seguintes e `FIM` sozinho no fim (a conversa:'
            f' `$PL comentario {pid} --ver`); o comentario sozinho nao comeca nem pega tarefa (a que ja esta na fila do'
            ' agente segue a fila normal); visivel a pessoas: sem dado de cliente nem atribuicao')


def line(ev):
    """One line of the `== PLANOU` block for an event (the same in the light loop and in the heavy tick)."""
    p, kind = ev.get('payload') or {}, ev.get('type')
    if kind == 'user_message':
        return f'{MESSAGE_PREFIX} ({_when(p)}): {message_text(p.get("text"))}'
    if kind in QUEUE_IN:
        extra = ', '.join(x for x in (queued_by(p),
                                      f'P{p.get("priority")}' if p.get('priority') else '',
                                      f'data {_date(p.get("due"))}' if _date(p.get('due')) else '',
                                      f'prazo {_date(p.get("deadline"))}' if _date(p.get('deadline')) else '') if x)
        head = f'{QUEUE_PREFIX}{" LIBERADA" if kind == "task_released" else ""} {one_line(p.get("pid"), 20)}: {one_line(p.get("title"))} ({extra})'
        chg = rework_for(p, ev.get('seq'))
        if chg:
            # the task comes back after "Pedir ajuste": the same branch and PR, with the person's text as the brief
            asks = ''.join(f'\n    AJUSTE{" do " + requested_by(a.get("by"), a.get("by_kind"), a.get("by_role"), a.get("in")) if a.get("by_kind") == "agent" else ""}: '
                           f'{message_text(a.get("text"))}' for a in chg.get('asks') or [])
            if not released(p):
                return f'{head} -> RETRABALHO NA FILA, nao comecar: espera a vaga (vem -- FILA LIBERADA){asks}'
            pr = one_line(chg.get('pr_url') or p.get('pr_url'), 300)
            return (f'{head} -> RETRABALHO (ajuste pedido), nao e tarefa nova: `fila started`, worker na mesma branch e na '
                    f'mesma PR{f" ({pr})" if pr else ""} atendendo o ajuste, depois `fila in_review` de novo{asks}')
        pr = one_line(p.get('pr_url'), 300)
        if not released(p):
            return f'{head} -> NA FILA, nao comecar: espera a vaga (vem -- FILA LIBERADA)' + (f'; PR: {pr}' if pr else '')
        handed = handoff_line(p, pr) or self_handoff_line(p, pr)
        if handed:
            return f'{head} -> {handed}'
        if pr:
            # a handoff brought the PR (Planou 0.43.0): the next owner (a reviewer) works on it, no search by the PID
            return (f'{head} -> tarefa da fila, com a PR: {pr}; `fila ver` para o detalhe e seguir o comportamento da '
                    f'coluna (revisao: code-review, sem procurar a PR pelo PID)')
        return (f'{head} -> tarefa da fila: `fila ver` para o detalhe; worker, PR em rascunho e parar ali (SKILL, Fila do agente)'
                + ('; o Planou tirou do backlog pela vaga livre: escopo sem clareza volta com `fila refinar`'
                   if p.get('queued_by') == 'auto' else ''))
    if kind == CHANGES_EVENT:
        pid, pr = one_line(p.get('pid'), 20), one_line(p.get('pr_url'), 300)
        by = requested_by(p.get('requested_by'), role=p.get('requested_by_role'), where=p.get('requested_in'))
        return (f'{CHANGES_PREFIX} {pid} ({by}): {message_text(p.get("text"))}\n    -> retrabalho de {pid}: nao mandar '
                f'progresso agora; quando vier -- FILA LIBERADA {pid}, retomar na mesma branch'
                + (f' e na mesma PR ({pr})' if pr else '') + ' atendendo este texto'
                + ('; o parecer completo esta na PR' if _by_agent(p.get('requested_by')) else ''))

    if kind == COMMENT_EVENT:
        return comment_line(p)
    if kind in CEREMONY_EVENTS:
        return ceremony_line(kind, p)
    if kind == PROPOSAL_EVENT:
        return proposal_line(p)
    if kind == 'agent_paused':
        return pause_lines(p, p.get('in_progress'))
    if kind == 'agent_resumed':
        return resume_lines(p, _pause()['held'])
    if kind == 'task_dequeued':
        why = DEQUEUE_REASON.get(p.get('reason'), one_line(p.get('reason'), 40) or 'saiu')
        return f'{QUEUE_PREFIX} SAIU {one_line(p.get("pid"), 20)}: {one_line(p.get("title"))} ({why}) -> parar o trabalho nela'
    ref = ' '.join(x for x in (p.get('code'), p.get('task_pid')) if x)
    who = f'(voce, {_when(p)}' + (', confirmado)' if p.get('confirmed') else ')')
    if kind == 'decision_answered':
        # a question the session asked (pergunta): not an action of the daily page, nothing to close with --acao
        if ask_ref(ev).startswith('q-'): who += ' (pergunta da sessao)'
        if p.get('value') == 'other':          # free answer, in the person's own words
            return f'-- DECISAO {ref}: resposta livre "{p.get("note") or ""}" {who}'
        opt = f'opcao {p.get("value")}' + (f' "{p.get("option_text")}"' if p.get('option_text') else '')
        return f'-- DECISAO {ref}: {opt} {who}' + (f' nota: {p.get("note")}' if p.get('note') else '')
    if kind in ('approved', 'rejected') and p.get('code') in _proposal_asks():
        # the ask of a proposal: Planou applies the change itself when the Desfazer window closes
        verb = 'APROVADO' if kind == 'approved' else 'RECUSADO'
        then = ('Planou aplica sozinho quando o Desfazer fecha; nada a fazer (vem -- PROPOSTA)' if kind == 'approved'
                else 'nada muda; nao aplicar a mao')
        return f'-- {verb} {ref}: {p.get("title")} {who} -> proposta: {then}'
    if kind == 'approved':
        # Phase 3 hard rule: the approval only records the OK; nothing is sent because of it.
        return (f'-- APROVADO {ref}: {p.get("title")} {who} -> OK registrado; nada foi enviado. '
                f'O envio segue pelo fluxo de hoje, com o usuario')
    if kind == 'rejected':
        return f'-- RECUSADO {ref}: {p.get("title")} {who} -> nao executar'
    if kind == 'draft_sent':
        final = p.get('final_text')
        return f'-- ENVIEI {ref}: rascunho enviado pelo usuario {who}' + (f'; texto final: {final}' if final else '')
    if kind == 'draft_discarded':
        return f'-- DESCARTADO {ref}: rascunho descartado pelo usuario {who}'
    if kind == 'alert_acknowledged':
        return f'-- ALERTA VISTO {ref}: {p.get("title")} {who}'
    if kind == 'notify_agent':
        return f'-- AVISAR AGENTE {p.get("about_agent")}: {p.get("title")} ({p.get("code")}) -> SendMessage para a sessao dele'
    if kind == 'task_assigned':
        return f'-- ATRIBUIDA {p.get("pid")}: {p.get("title")} (pelo usuario)'
    return f'-- {str(kind).upper()} {p.get("pid") or ref}'


def poll_block(next_tick=None, poll_s=None):
    """The light loop's block (see poll_wait); '' when there is nothing new or on failure."""
    return poll_wait(next_tick, poll_s)[0]


def poll_wait(next_tick=None, poll_s=None, wait=None):
    """The light loop (spec 6.2): events that deserve waking the session and were never printed, as a `== PLANOU (n)`
    block, or ''. Never applies anything to the agent state; a failure goes to health.json and returns '' (the heavy tick
    reports a failure that lasts, with the runner's 60-minute rule), so a Planou outage does not wake the session every
    90 s. The printed seqs become final with cursor_commit(), once the block is in tick.out.

    The only acknowledgement here is for the person's messages: a message_id already delivered that comes again (at least
    once delivery) is not printed, only acknowledged, and an ack that failed after a delivery is retried.

    Returns (block, how): POLL_LONG when the server honoured `wait` (the runner polls again right away), POLL_SHORT when
    it did not or no wait was asked (the runner sleeps its short interval), POLL_FAILED on failure (the runner backs off)."""
    if not active(): return '', POLL_SHORT
    info = {}
    try:
        ib = fetch(next_tick, poll_s, wait, info)
    except PlanouError as e:
        print(_failed(e.message if e.status else e), file=sys.stderr)
        return '', POLL_FAILED
    _ok()
    how = POLL_LONG if wait and info.get('waited') else POLL_SHORT
    msgs = _messages()
    new = []
    for k in sorted(ib['events'], key=int):
        # an adjustment is kept before any line is built: a redelivery in the same block is told apart as rework
        if ib['events'][k].get('type') == CHANGES_EVENT: changes_note(ib['events'][k])
    for k in sorted(ib['events'], key=int):
        ev = ib['events'][k]
        if k in ib['printed'] or ev.get('type') not in WAKE_TYPES: continue
        if ev.get('type') in DELIVER_TYPES and _message_id(ev) in msgs['delivered']:
            ib['printed'][k] = datetime.now(timezone.utc).isoformat()      # the repeated one: never twice in the session
            if int(k) not in msgs['to_ack']: msgs['to_ack'].append(int(k))
            continue
        new.append(ev)
    ib['pending_print'] = [str(e['seq']) for e in new]
    _save('inbox.json', ib)
    _save('messages.json', msgs)
    if msgs['to_ack'] and not new: ack_delivered()
    if not new: return '', how
    return '\n'.join([f'== PLANOU ({len(new)})'] + [line(e) for e in new]), how


def cursor_commit(now=None):
    """The block of the last poll_block() reached tick.out: those events never wake the session again. A person's message
    in it is delivered from now on (its message_id is never printed again) and waits for ack_delivered()."""
    ib = _inbox()
    msgs = _messages()
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    for k in ib['pending_print']:
        ib['printed'][k] = stamp
        ev = ib['events'].get(k) or {}
        queue_note(ev)
        changes_note(ev)
        pause_note(ev)
        proposal_note(ev)
        if ev.get('type') in DELIVER_TYPES:
            msgs['delivered'][_message_id(ev)] = {'seq': int(k), 'at': stamp}
            if int(k) not in msgs['to_ack']: msgs['to_ack'].append(int(k))
    ib['pending_print'] = []
    cut = ((now or datetime.now(timezone.utc)) - timedelta(days=SEEN_DAYS)).isoformat()
    msgs['delivered'] = {m: v for m, v in msgs['delivered'].items() if (v or {}).get('at', '') >= cut}
    _save('inbox.json', ib)
    _save('messages.json', msgs)


def ack_delivered():
    """The runner printed tick.out: acknowledges the person's messages delivered in it (the cursor is cumulative, so the
    highest seq covers them all). The local inbox is not pruned: task events before them still wait for the heavy tick,
    which reads them from there. False when the ack failed (retried on the next light loop)."""
    msgs = _messages()
    if not msgs['to_ack']: return True
    if not ack(max(msgs['to_ack'])): return False
    msgs['to_ack'] = []
    _save('messages.json', msgs)
    return True


def ack(seq):
    if not active(): return False
    try:
        _call('POST', '/agent/events/ack', {'seq': int(seq)})
        return True
    except PlanouError:
        return False


def _prune(ib, upto, now=None):
    cut = ((now or datetime.now(timezone.utc)) - timedelta(days=SEEN_DAYS)).isoformat()
    ib['events'] = {k: v for k, v in ib['events'].items() if int(k) > upto}
    # printed seqs stay 7 days (a fetch after an ack never brings them back, but a lost ack would)
    ib['printed'] = {k: t for k, t in ib['printed'].items() if t >= cut or int(k) > upto}


def _norm(field, v):
    """A task field in a comparable form: dates as YYYY-MM-DD, a project state by its name as Planou compares it, an
    assignee by (kind, id), numbers as float."""
    if field in ('due', 'deadline'): return _date(v)
    if field == 'project_state': return state_key(v.get('name') if isinstance(v, dict) else v) or None
    if field == 'assignee':
        return (v.get('kind'), v.get('id')) if isinstance(v, dict) else (v or None)
    if field in ('priority', 'estimate_h', 'remaining_h', 'completed_h') and v is not None:
        try: return float(v)
        except (TypeError, ValueError): return v
    return v


def _local_value(field, v):
    """The person's value as the agent keeps it (state.json, sent.json): dates as YYYY-MM-DD, a project state by name."""
    if field in ('due', 'deadline'): return _date(v)
    if field == 'project_state': return v.get('name') if isinstance(v, dict) else v
    return v


def local_values(entry, body):
    """The values the next sync will send for a task, the ones an event's `changed` is compared with (a field missing
    here always applies): the last body the agent tried to send (sent.json, conflicts included), else the known values
    of state.json that are not empty. A task Planou holds in backlog or in "Em refinamento" sends no state of its own."""
    if body is not None:
        local = {k: body[k] for k in CHANGED_FIELDS if k in body}
    else:
        local = {k: v for k, v in (entry or {}).items() if k in CHANGED_FIELDS and v is not None}
    if not local.get('project_state'): local.pop('project_state', None)
    if (entry or {}).get('backlog') or (entry or {}).get('refining'):
        for k in ('state', 'resolution', 'project_state'): local.pop(k, None)
    return local


def split_changed(p, local):
    """(applied, kept) of an event's `changed` ({field: {from, to}}): a field applies when the agent has no local value
    for it or its local value is the `from`; otherwise the local value did not go up yet and wins (the next sync resends
    it over the person's, with the event's version as base). A local value that already is the `to` agrees with the
    person (an adjustment asked, task_changes_requested, may have written it first). `applied` is {field: to}; `kept`
    lists the fields."""
    applied, kept = {}, []
    for field, ch in (p.get('changed') or {}).items():
        if field not in CHANGED_FIELDS or not isinstance(ch, dict): continue
        mine = _norm(field, local.get(field))
        if field in local and mine != _norm(field, ch.get('from')) and mine != _norm(field, ch.get('to')):
            kept.append(field)
        else:
            applied[field] = ch.get('to')
    return applied, kept


@_holding('state.json')
def apply_events(events, apply, on_ask=None, on_change=None, on_state=None):
    """Heavy tick: applies the person's changes to the agent state, prints what was not printed yet and acknowledges.

    Events are applied in `seq` order. `apply(code, status_label)` is the agent's "box ticked" handler (the same one the
    Notion page uses); it returns True when the agent state changed. Only then the event version is stored, so the next
    sync confirms the person's change; otherwise the server keeps what the person did (conflict) until the agent catches
    up. A reopened task comes as the label 'A fazer'.

    Fields changed (Planou `changed`, PLN0072): a task event that carries `changed` ({field: {from, to}}, `{}` for a new
    task or a change of section or tag only) applies only those fields; the rest of its payload is the whole task as
    Planou has it and would erase local values that did not go up yet (a local due becoming null). A field whose local
    value (what the agent last tried to send, sent.json, else state.json) is not the `from` keeps the local value: it
    is not applied, and the event version is stored so the next sync resends it. Without `changed` (older Planou) the
    whole payload applies, as before.

    `on_ask(event) -> bool` handles answers to asks (draft sent, approval, decision) in the agent state; optional.

    `on_change(code, payload, sent) -> bool` takes an edit (title, due date, state) into the agent state; `sent` is what
    the agent last sent for that task ({'title', 'due', 'state'}), so the handler can tell what the person changed. With
    `changed`, `payload` carries only the fields that apply (their `to`) besides pid, source_key, version, by and the
    like, and the handler is not called when none applies. Optional: without it the server keeps the person's values for
    good and the version is simply acknowledged.

    `on_state(code, name, sent) -> bool` takes a move to another state of the project (the event's `project_state` name,
    on a completed, reopened or changed task) when that name is not the one the agent last sent; optional. A deleted
    task is forgotten locally; the server never recreates it. A restored one (task_restored, from the Lixeira) is known
    again from its payload and otherwise handled as a changed task. Events already printed by the light loop are applied
    silently. A person's message (user_message) is never printed here: one already delivered by the light loop is only
    acknowledged; one not delivered yet stops the loop right before it (the ack goes up to the event before it, the rest
    waits for a later tick), since the cursor is cumulative and a message is acknowledged only after it reached the
    session. An `agent_docs_changed` goes to the handler of set_docs_handler() first; when it says its answer did not
    reach Planou, the loop stops right before it, as for a message not delivered yet. Returns lines for the tick output.
    """
    if not events: return []
    events = sorted(events, key=lambda e: int(e.get('seq') or 0))
    prefix = _S['agent'] + ':'
    st = _load('state.json', {})
    tasks = st.setdefault('tasks', {})
    kept_bodies = _load('sent.json', {})
    ib = _inbox()
    lines, last = [], None
    for ev in events:
        kind = ev.get('type')
        if kind in DELIVER_TYPES:
            if not delivered(ev): break       # the light loop delivers it; nothing from here on is applied or acknowledged
            last = ev.get('seq'); ib['printed'][str(last)] = datetime.now(timezone.utc).isoformat()
            queue_note(ev)
            changes_note(ev)
            continue
        if kind == DOCS_EVENT and _docs_handler[0] and not _docs_handler[0](ev):
            break                             # its answer did not reach Planou: it waits for a later tick (idempotent)
        last = ev.get('seq')
        queue_note(ev)
        changes_note(ev)
        printed = str(last) in ib['printed']
        p = ev.get('payload') or {}
        key = p.get('source_key') or ''
        if kind == CHANGES_EVENT:
            code = key[len(prefix):] if key.startswith(prefix) else None
            if code in tasks: _changes_to_state(code, p, tasks, kept_bodies, on_state)
            if not printed: lines.append(line(ev))
            ib['printed'][str(last)] = datetime.now(timezone.utc).isoformat()
            continue
        if kind not in EVENTS and key.startswith(prefix) and isinstance(p.get('changed'), dict):
            # a type this runner does not know yet, about one of its tasks: a task_changed (Planou's rule for older
            # runners), with only the fields of its `changed` (the rest of the payload would erase local values)
            kind = 'task_changed'
        if kind not in TASK_EVENTS:
            handled = bool(on_ask and on_ask(ev))
            if kind in ASK_ANSWERS:
                _answered(p.get('code'))
                # the answer to a refinement ask: Planou brings the task back to A fazer; the sync stops holding it
                for v in tasks.values():
                    if (v.get('refining') or {}).get('ask') == p.get('code'): v.pop('refining', None)
            if not printed:
                if kind == 'task_assigned' and not key.startswith(prefix):
                    lines.append(f'PLANOU: {p.get("pid")} atribuida a voce pelo usuario: {p.get("title")}')
                elif kind in WAKE_TYPES:
                    lines.append(line(ev) + (' -> aplicado' if handled else ''))
            pause_note(ev)              # after its line: the resume line reads the tasks held by the pause
            proposal_note(ev)
            ib['printed'][str(last)] = datetime.now(timezone.utc).isoformat()
            continue
        if not key.startswith(prefix): continue
        code = key[len(prefix):]
        restored = kind == 'task_restored'
        edit = kind in ('task_changed', 'task_restored')
        if restored:
            # back from the Lixeira: the agent knows the task again (the delete forgot it), with Planou's values as sent
            ps0 = p.get('project_state')
            tasks[code] = {**(tasks.get(code) or {}), 'pid': p.get('pid'), 'version': p.get('version'),
                           'title': p.get('title'), 'due': _date(p.get('due')), 'state': p.get('state'),
                           'project_state': ps0.get('name') if isinstance(ps0, dict) else ps0}
        sent = tasks.get(code) or {}
        partial = 'changed' in p and kind != 'task_deleted'
        if partial:
            local = local_values(sent, kept_bodies.get(code))
            applied, held = split_changed(p, local)
            if 'state' in held and 'project_state' in local and 'project_state' not in held:
                # Planou derives the category from the named project state: a state the agent never really sent
                held.remove('state'); applied['state'] = p['changed']['state'].get('to')
            view = {k: v for k, v in p.items() if k not in CHANGED_FIELDS and k != 'changed'}
            view.update(applied)
        else:
            applied, held, view = None, [], p
        label = STATUS_OF_RESOLUTION.get(p.get('resolution')) if kind == 'task_completed' else 'A fazer' if kind == 'task_reopened' else None
        if 'state' in held: label = None          # the agent moved it meanwhile: its state goes up again
        changed = bool(label) and bool(apply(code, label))
        if edit and on_change and (not partial or applied):
            changed = bool(on_change(code, view, sent))
        pv = view.get('project_state')
        ps = pv.get('name') if isinstance(pv, dict) else None
        if on_state and ps and kind != 'task_deleted' and state_key(ps) != state_key(sent.get('project_state')):
            changed = bool(on_state(code, ps, sent)) or changed
        if changed or edit or held:
            # from now on the person's values are what the agent "sent": the next edit is told apart from them. A field
            # the local value kept is not written: the stored version makes the next sync resend it over the person's
            tasks[code] = {**sent, 'pid': p.get('pid'), 'version': p.get('version')}
            if edit and not partial:
                tasks[code].update(title=p.get('title'), due=_date(p.get('due')), state=p.get('state'))
            elif partial:
                for f in ('title', 'due', 'state'):
                    if f in applied: tasks[code][f] = _local_value(f, applied[f])
            if ps and on_state: tasks[code]['project_state'] = ps
            if partial and code in kept_bodies:
                for f, v in applied.items():
                    if f in kept_bodies[code]: kept_bodies[code][f] = _local_value(f, v)
        if code in tasks and kind != 'task_deleted' and view.get('state'):
            # the person moved it: in or out of backlog, out of "Em refinamento"
            if view['state'] == 'backlog': tasks[code]['backlog'] = True
            else: tasks[code].pop('backlog', None)
            if view['state'] != 'waiting' or (ps and state_key(ps) != state_key(REFINEMENT)): tasks[code].pop('refining', None)
        elif kind == 'task_deleted':
            tasks.pop(code, None)
        what = {'task_completed': 'concluida', 'task_reopened': 'reaberta', 'task_changed': 'alterada', 'task_deleted': 'apagada',
                'task_restored': 'restaurada'}.get(kind, kind)
        if code in tasks and kind != 'task_deleted' and (p.get('assignee') or {}).get('self'):
            tasks[code]['self_sent'] = True     # already the agent's: a "pronta --self" mark sends no assignee again
        who = ('pelo Planou (saiu do backlog pela vaga livre)' if p.get('by') == 'auto' and view.get('state') not in (None, 'backlog')
               else 'pelo Planou' if p.get('by') == 'auto' else 'pelo usuario')
        if not printed:
            lines.append(f'PLANOU: {p.get("pid")} {what} {who}' + (' -> aplicado' if changed else '')
                         + (f' (mantido o valor local: {", ".join(held)})' if held else ''))
        ib['printed'][str(last)] = datetime.now(timezone.utc).isoformat()
    _save('state.json', st)
    if kept_bodies: _save('sent.json', kept_bodies)
    if last is not None and ack(last):
        _prune(ib, int(last))
    _save('inbox.json', ib)
    return lines


# ---------------------------------------------------------------- the agent's work queue ("Fila do agente")

def _queue():
    """cache/planou/queue.json: {"tasks": {task_id: payload + seq, received_at, state, pr_url}}. A task that left (dequeued,
    done, not_in_queue) stays as a tombstone ({"left": reason, "seq"}) so an older event applied later never brings it
    back."""
    q = _load('queue.json', {})
    if not isinstance(q.get('tasks'), dict): q['tasks'] = {}
    return q


def released(p):
    """A queue task the agent may start: released true, or absent (a Planou before task_release sends only those)."""
    return (p or {}).get('released') is not False


@_holding('queue.json')
def queue_note(ev, now=None):
    """Keeps queue.json in step with a queue event (idempotent: the light loop and the heavy tick may both see it)."""
    kind = ev.get('type')
    if kind not in QUEUE_EVENTS: return
    p = ev.get('payload') or {}
    tid = str(p.get('task_id') or p.get('pid') or '')
    if not tid: return
    seq = int(ev.get('seq') or 0)
    q = _queue()
    cur = q['tasks'].get(tid)
    if cur and int(cur.get('seq') or 0) >= seq and (cur.get('left') or kind in QUEUE_IN): return
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    if kind in QUEUE_IN:
        # task_released carries the whole payload too: it also stands in for an announcement the agent missed
        rel = kind == 'task_released' or released(p)
        q['tasks'][tid] = {**({} if cur is None or cur.get('left') else cur), **p, 'seq': seq, 'received_at': stamp,
                           'released': rel, 'state': 'sent' if rel else 'queued'}
    else:
        q['tasks'][tid] = {'pid': p.get('pid'), 'title': p.get('title'), 'left': p.get('reason') or 'dequeued', 'seq': seq, 'at': stamp}
        _stop_marker(p.get('pid'), now)          # the turns after this are no longer this task's
        forget_self_handoff(tid, p.get('pid'))   # out of the agent: an own review that was waiting is gone too
    cut = ((now or datetime.now(timezone.utc)) - timedelta(days=SEEN_DAYS)).isoformat()
    q['tasks'] = {k: v for k, v in q['tasks'].items() if not v.get('left') or (v.get('at') or '') >= cut}
    _save('queue.json', q)


def queue_tasks():
    """The queue tasks with this agent right now (oldest first), with the whole payload."""
    return sorted((v for v in _queue()['tasks'].values() if not v.get('left')), key=lambda v: int(v.get('seq') or 0))


# ---------------------------------------------------------------- cost-cap pause (agent_paused / agent_resumed, Planou 0.42.0)
#
# At 100% of its cost cap the agent is paused until the cycle turns, the person raises or removes the cap, or clicks
# Despausar. Planou hands out nothing new meanwhile; the runner prints `== PAUSADO (teto de custo)` and the session takes
# nothing new (no `fila started` on a new task, no pull from the backlog) and finishes what is in progress, or blocks it
# with a note (`fila blocked PID`, the task is then "held by the pause"). `agent_resumed` prints `== DESPAUSADO` with a
# `fila started` for each held task Planou still lists as blocked. A runner that starts while paused learns it from the
# heartbeat answer (`paused`) and prints the same banner on the next heavy tick.
#
# cache/planou/pause.json: {"paused": {reason, label, since, until, cap_usd, used_usd, percent, in_progress} or null,
# "seq": the last pause event noted, "held": [{task_id, pid, title}], "told": {since, session}: the pause the session
# was shown (the banner is printed again only for a new pause or a new session, never on every tick: each wake costs)}.

PAUSE_EVENTS = ('agent_paused', 'agent_resumed')
PAUSED_PREFIX = '== PAUSADO'
RESUMED_PREFIX = '== DESPAUSADO'
PAUSE_NOTE = 'parou no teto de custo'
PAUSED_EXIT = 5
RESUMED_HOW = {'cycle': 'o ciclo virou', 'cap': 'o usuario subiu ou tirou o teto', 'person': 'o usuario despausou'}


def _pause():
    st = _load('pause.json', {})
    if not isinstance(st, dict): st = {}
    if not isinstance(st.get('held'), list): st['held'] = []
    return st


def paused():
    """The pause in force ({reason, label, since, until, ...}) or None."""
    return _pause().get('paused') or None


def _refs(items):
    return [{'task_id': r.get('task_id'), 'pid': r.get('pid'), 'title': r.get('title')} for r in items or [] if isinstance(r, dict)]


def _instant(v):
    """A datetime for an ISO value (7 fractional digits and Z taken), or None."""
    try:
        return datetime.fromisoformat(re.sub(r'(\.\d{6})\d+', r'\1', str(v or '').replace('Z', '+00:00')))
    except ValueError:
        return None


def _same_instant(a, b):
    da, db = _instant(a), _instant(b)
    if da is None or db is None: return str(a or '') == str(b or '')
    if (da.tzinfo is None) != (db.tzinfo is None): return False
    return abs((da - db).total_seconds()) < 1


def _session_now():
    return _load('session.json', {}).get('session_id')


def _usd(v):
    try: return f'US$ {float(v):.2f}'
    except (TypeError, ValueError): return ''


def pause_lines(p, in_progress=None):
    """The `== PAUSADO (teto de custo)` banner with what to do, one line per task in progress."""
    label = one_line(p.get('label') or 'teto de custo', 40)
    cost = f'{_usd(p.get("used_usd"))} de {_usd(p.get("cap_usd"))}' if _usd(p.get('used_usd')) and _usd(p.get('cap_usd')) else ''
    if p.get('percent') is not None: cost = f'{cost} ({p.get("percent")}%)'.strip()
    until = when_label(p.get('until'))
    head = (f'{PAUSED_PREFIX} ({label})' + (f': {cost}' if cost else '') + (f', desde {when_label(p.get("since"))}' if when_label(p.get('since')) else '')
            + f'; volta sozinho {f"em {until} (fim do ciclo)" if until else "no fim do ciclo"}, antes se o usuario subir ou tirar o teto ou clicar Despausar')
    out = [head, '    -> nao pegar trabalho novo: nada de `fila started` em tarefa nova, `pronta --self` nem puxar do backlog '
                 '(as fontes seguem so registrando). Dizer ao usuario que o agente pausou no teto']
    refs = _refs(in_progress)
    for r in refs:
        pid = one_line(r.get('pid'), 20)
        out.append(f'    EM ANDAMENTO {pid}: {one_line(r.get("title"))} -> terminar (preferivel) e dizer que terminou mesmo '
                   f'pausado; se nao der para terminar, parar o worker e `fila blocked {pid} --note "{PAUSE_NOTE}"`')
    if not refs: out.append('    nenhuma tarefa da fila em andamento')
    return '\n'.join(out)


def _held_ids(items):
    return {str(x) for r in items or [] if isinstance(r, dict) for x in (r.get('task_id'), str(r.get('pid') or '').upper()) if x}


def resume_lines(p, held=()):
    """The `== DESPAUSADO` banner: a `fila started` for each task held by the pause that Planou still lists as blocked."""
    how = RESUMED_HOW.get(p.get('how')) or one_line(p.get('how'), 40) or 'fim da pausa'
    if p.get('how') == 'person' and when_label(p.get('until')): how += f', vale ate {when_label(p.get("until"))}'
    label = one_line(p.get('label') or 'teto de custo', 40) if p.get('reason') not in (None, 'cost_cap') else 'teto de custo'
    blocked = _held_ids(p.get('blocked'))
    retake = [h for h in held if isinstance(h, dict) and _held_ids([h]) & blocked]
    out = [f'{RESUMED_PREFIX} ({label}, {how}): pode pegar trabalho de novo; a fila volta a liberar']
    for h in retake:
        pid = one_line(h.get('pid'), 20)
        out.append(f'    RETOMAR {pid}: {one_line(h.get("title"))} -> travada pela pausa: `fila started {pid}` e o worker de '
                   f'volta na mesma branch')
    return '\n'.join(out)


@_holding('pause.json')
def pause_note(ev, now=None):
    """Keeps pause.json in step with agent_paused / agent_resumed (idempotent by seq). Called where the event reaches the
    session (cursor_commit, apply_events), so the pause it notes counts as told."""
    kind = ev.get('type')
    if kind not in PAUSE_EVENTS: return
    seq = int(ev.get('seq') or 0)
    st = _pause()
    if seq and seq <= int(st.get('seq') or 0): return
    p = ev.get('payload') or {}
    st['seq'] = seq
    if kind == 'agent_paused':
        st['paused'] = {**{k: p.get(k) for k in ('reason', 'label', 'since', 'until', 'cap_usd', 'used_usd', 'percent')},
                        'in_progress': _refs(p.get('in_progress'))}
        st['held'] = []
        st['told'] = {'since': p.get('since'), 'session': _session_now()}
    else:
        st['paused'], st['held'] = None, []
        st.pop('told', None)
        st['resumed'] = {'how': p.get('how'), 'at': (now or datetime.now(timezone.utc)).isoformat()}
    _save('pause.json', st)


@_holding('pause.json')
def pause_from_heartbeat(res, phase, now=None):
    """The heartbeat answer's `paused` (Planou 0.42.0): a pause not known here starts (a runner that starts while paused),
    none ends the local one silently (agent_resumed prints `== DESPAUSADO`; the held tasks wait for it). On the heavy
    tick (the only heartbeat whose lines reach the session) the banner is printed once per pause and session."""
    if not isinstance(res, dict): return []
    hb = res.get('paused') if isinstance(res.get('paused'), dict) else None
    st = _pause()
    cur = st.get('paused')
    changed = False
    if hb and not (cur and _same_instant(cur.get('since'), hb.get('since'))):
        mine = [v for v in queue_tasks() if released(v) and v.get('state') in ('sent', 'started')]
        st['paused'] = {**{k: hb.get(k) for k in ('reason', 'label', 'since', 'until')}, 'in_progress': _refs(mine)}
        changed = True
    elif not hb and cur:
        st['paused'] = None
        changed = True
    if phase == 'session_closed' and st.get('told'):
        # the session is gone (the runner stops or relaunches): the next one is told again, even before the transcript
        # pump rewrites session.json (the first heavy tick of a new runner runs right away)
        st.pop('told')
        changed = True
    out = []
    p, told, sid = st.get('paused'), st.get('told') or {}, _session_now()
    if phase == 'tick' and p and not (_same_instant(told.get('since'), p.get('since')) and (not sid or told.get('session') == sid)):
        out = [pause_lines(p, p.get('in_progress'))]
        st['told'] = {'since': p.get('since'), 'session': sid}
        changed = True
    if changed: _save('pause.json', st)
    return out


def _pause_gate(ref):
    """While paused: None when `fila started` may go (a task in progress when the pause came, not held by it, or one
    already started here), else the ESPERE line."""
    p = paused()
    if not p: return None
    try:
        tid, entry = _queue_entry(ref)
    except PlanouError:
        tid, entry = str(ref or ''), None
    ids = _held_ids([{'task_id': tid, 'pid': (entry or {}).get('pid') or ref}])
    held = ids & _held_ids(_pause()['held'])
    if not held and (ids & _held_ids(p.get('in_progress')) or (entry or {}).get('state') == 'started'): return None
    until = when_label(p.get('until'))
    return (f'ESPERE: agente pausado pelo teto de custo{f" ate {until}" if until else ""}. Nao comece tarefa nova'
            + (' nem retome a que travou pela pausa' if held else '') + '; ela segue na fila e volta quando vier == DESPAUSADO.')


@_holding('pause.json')
def pause_hold(tid, pid, title=None):
    """`fila blocked` while paused: the task is held by the pause and comes back with `fila started` on the resume."""
    st = _pause()
    if not st.get('paused'): return
    if _held_ids([{'task_id': tid, 'pid': pid}]) & _held_ids(st['held']): return
    st['held'].append({'task_id': tid, 'pid': pid, 'title': title})
    _save('pause.json', st)


# ---------------------------------------------------------------- "Pedir ajuste" (task_changes_requested, Planou 0.38.0)
#
# The person asks for an adjustment on a task in review that the agent did: the task goes back to the agent's queue
# (in front, within its priority) and waits for a slot like any other; meanwhile progress answers 409 not_in_queue. The
# text is kept in cache/planou/changes.json by task_id (the brief of the rework: same branch, same PR) through three
# phases: `pending` from the event until the queue releases the task again (task_queued_for_agent released or
# task_released with a higher seq), `rework` while the agent works on it, and a `cleared` tombstone (so a late copy of
# the event never brings it back) once the agent delivers again (in_review, done, handoff), the task leaves the queue
# (task_dequeued) or a 409 not_in_queue comes after the redelivery.

def _changes():
    c = _load('changes.json', {})
    if not isinstance(c.get('tasks'), dict): c['tasks'] = {}
    return c


def _project_state_name(v):
    return v.get('name') if isinstance(v, dict) else v


def _newer(version, known):
    """True when an event's version is not older than the known one (a version never goes back)."""
    try: return known is None or int(version) >= int(known)
    except (TypeError, ValueError): return False


def _queue_changes(tid, p, seq, now=None):
    """queue.json follows the adjustment: the task waits in the queue again (not released, local state
    `changes_requested`, so `fila ver` shows it and no progress is sent), with Planou's version and states."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with _locked('queue.json'):
        q = _queue()
        cur = q['tasks'].get(tid)
        if cur and int(cur.get('seq') or 0) >= seq: return
        keep = {} if cur is None or cur.get('left') else cur
        entry = {**keep, **{k: p.get(k) for k in ('task_id', 'pid', 'source_key', 'title', 'progress_url') if p.get(k)},
                 'seq': seq, 'received_at': stamp, 'released': False, 'state': 'changes_requested',
                 'planou_state': p.get('state'), 'project_state': _project_state_name(p.get('project_state'))}
        if p.get('version') is not None and _newer(p.get('version'), keep.get('version')): entry['version'] = p['version']
        if p.get('pr_url'): entry['pr_url'] = p['pr_url']
        q['tasks'][tid] = entry
        _save('queue.json', q)


def changes_note(ev, now=None):
    """Keeps changes.json in step with an adjustment asked and with the queue events that end its wait (idempotent: the
    light loop, the heavy tick and `fila` may all see the same event). queue.json first, then changes.json (lock order)."""
    kind = ev.get('type')
    if kind != CHANGES_EVENT and kind not in QUEUE_EVENTS: return
    p = ev.get('payload') or {}
    tid = str(p.get('task_id') or p.get('pid') or '')
    if not tid: return
    seq = int(ev.get('seq') or 0)
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    if kind == CHANGES_EVENT:
        c = _changes()['tasks'].get(tid)
        if c and c.get('phase') == 'cleared' and int(c.get('seq') or 0) >= seq: return
        _queue_changes(tid, p, seq, now)
    with _locked('changes.json'):
        c = _changes()
        cur = c['tasks'].get(tid)
        if kind == CHANGES_EVENT:
            if cur and cur.get('phase') == 'cleared' and int(cur.get('seq') or 0) >= seq: return
            if cur is None or cur.get('phase') == 'cleared': cur = {'asks': []}
            who = p.get('requested_by') if isinstance(p.get('requested_by'), dict) else {}
            # comment_id is null when an agent (a reviewer) asked: the text is no comment; dedup by seq then
            ask = {'text': str(p.get('text') or '')[:4000], 'comment_id': p.get('comment_id'), 'at': p.get('at'),
                   'by': one_line(who.get('name'), 80) or None, 'by_kind': who.get('kind') or 'person', 'seq': seq,
                   'by_role': one_line(p.get('requested_by_role'), 60) or None, 'in': _requested_in_name(p.get('requested_in'))}
            if not any(a.get('seq') == seq or (ask['comment_id'] and a.get('comment_id') == ask['comment_id'])
                       for a in cur['asks']):
                cur['asks'].append(ask)
            if cur.get('seq') is None or seq > int(cur['seq']):      # the same event again never undoes the rework
                cur.update(pid=p.get('pid'), title=p.get('title'), source_key=p.get('source_key'), seq=seq,
                           phase='pending', version=p.get('version'), state=p.get('state'),
                           project_state=_project_state_name(p.get('project_state')), noted_at=stamp)
                cur.pop('released_seq', None)
                if p.get('pr_url'): cur['pr_url'] = p['pr_url']
            c['tasks'][tid] = cur
        elif not cur or cur.get('phase') == 'cleared' or seq <= int(cur.get('seq') or 0):
            return
        elif kind == 'task_dequeued':
            c['tasks'][tid] = {'pid': cur.get('pid'), 'phase': 'cleared', 'why': 'dequeued', 'seq': seq, 'at': stamp}
        elif cur.get('phase') == 'pending' and (kind == 'task_released' or released(p)):
            cur.update(phase='rework', released_seq=seq)
        else:
            return
        cut = ((now or datetime.now(timezone.utc)) - timedelta(days=SEEN_DAYS)).isoformat()
        c['tasks'] = {k: v for k, v in c['tasks'].items() if v.get('phase') != 'cleared' or (v.get('at') or '') >= cut}
        _save('changes.json', c)


def _change_of(ref, entry=None):
    """(task_id, record) of the adjustment open (pending or rework) for a task_id, pid or the queue entry; (None, None)."""
    r = str(ref or '').strip().upper()
    pid = str((entry or {}).get('pid') or '').upper()
    for tid, v in _changes()['tasks'].items():
        if v.get('phase') not in ('pending', 'rework'): continue
        if r and r in (tid.upper(), str(v.get('pid') or '').upper()) or pid and pid == str(v.get('pid') or '').upper():
            return tid, v
    return None, None


def rework_for(p, seq):
    """The adjustment a queue event (re)delivers: an open one for the same task noted before the event. None for a new task."""
    tid, v = _change_of(p.get('task_id') or p.get('pid'))
    return v if v and int(v.get('seq') or 0) < int(seq or 0) else None


def changes_open():
    """The adjustments asked and not delivered again yet ({task_id: record}), for `fila ver`."""
    return {k: v for k, v in _changes()['tasks'].items() if v.get('phase') in ('pending', 'rework')}


def _clear_change(tid, why, now=None):
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with _locked('changes.json'):
        c = _changes()
        cur = c['tasks'].get(tid)
        if not cur or cur.get('phase') == 'cleared': return
        c['tasks'][tid] = {'pid': cur.get('pid'), 'phase': 'cleared', 'why': why, 'seq': cur.get('seq'), 'at': stamp}
        _save('changes.json', c)


def _changes_to_state(code, p, tasks, kept_bodies, on_state=None):
    """The agent's own task (state.json) takes the adjustment's version, state and project state, so the next sync
    neither conflicts nor puts the task back in review. A version older than the known one changes nothing."""
    sent = tasks.get(code) or {}
    if not _newer(p.get('version'), sent.get('version')): return
    ps = _project_state_name(p.get('project_state'))
    if on_state and ps and state_key(ps) != state_key(sent.get('project_state')): on_state(code, ps, sent)
    entry = {**sent, 'pid': p.get('pid') or sent.get('pid'), 'version': p.get('version')}
    if p.get('state'): entry['state'] = p['state']
    if ps: entry['project_state'] = ps
    if p.get('state') and p['state'] != 'backlog': entry.pop('backlog', None)
    entry.pop('refining', None)
    tasks[code] = entry
    if code in kept_bodies:
        if p.get('state'): kept_bodies[code]['state'] = p['state']
        if ps: kept_bodies[code]['project_state'] = ps


def _wait_changes(tid, entry, ref=None):
    """Raises PlanouError changes_pending while an adjustment waits for the queue: no progress before the redelivery."""
    _, chg = _change_of(ref or tid, entry)
    if chg and chg.get('phase') == 'pending':
        pid = chg.get('pid') or ref or tid
        raise PlanouError(409, 'changes_pending', f'ajuste pedido em {pid}: a tarefa voltou para a fila e nada vai ao '
                          f'Planou por ela ate a nova entrega (-- FILA LIBERADA {pid})')


def _changes_arrived(tid, entry, ref=None):
    """After a 409 not_in_queue: an adjustment the runner has not noted yet may be why. Reads the local inbox and asks
    Planou for the events after it (best effort), noting only the adjustments among them. Read only: inbox.json is the
    runner's (a save from here could undo what its light loop printed), and nothing is acknowledged. True when one
    waits for this task."""
    ib = _inbox()
    evs = list(ib['events'].values())
    known = [int(k) for k in ib['events']] + [int(k) for k in ib['printed']]
    try:
        _, res = _call('GET', '/agent/events' + (f'?after={max(known)}' if known else ''))
        evs += (res or {}).get('events') or []
    except PlanouError:
        pass
    for ev in sorted(evs, key=lambda e: int(e.get('seq') or 0)):
        if ev.get('type') == CHANGES_EVENT: changes_note(ev)
    _, chg = _change_of(ref or tid, entry)
    return bool(chg and chg.get('phase') == 'pending')


def _queue_entry(ref):
    """(task_id, entry) of the local queue for a pid, task_id or source_key; the only task when ref is empty."""
    tasks = queue_tasks()
    if not ref:
        free = [v for v in tasks if released(v)] if len(tasks) > 1 else tasks
        if len(free) == 1: return str(free[0].get('task_id') or free[0].get('pid')), free[0]
        raise PlanouError(0, 'which_task', 'nenhuma tarefa da fila com o agente' if not tasks else
                          'mais de uma tarefa da fila com o agente: diga qual (PID)')
    r = str(ref).strip()
    for v in tasks:
        if r.upper() == str(v.get('pid') or '').upper() or r in (str(v.get('task_id')), v.get('source_key')):
            return str(v.get('task_id') or v.get('pid')), v
    return r, None


PROGRESS_STATES = ('started', 'in_review', 'done', 'blocked', 'handoff')

# How old the project states may be when `fila done` decides between done and handoff: fresh, so an owner the person
# just set on a column is not missed (a missed one would close the task instead of passing it on).
HANDOFF_STATES_TTL = timedelta(minutes=2)


def owns_a_column(entry=None, now=None):
    """True when the task's project (the queue payload's `project`, else the configured one) has a state whose owner is
    this agent (`owner.is_me`) and that has a `next_state` (Planou 0.27.0, "Dono da coluna e esteira"). The queue payload
    does not say the task's column, so this only gates the try: Planou moves the task by the column it is in, and answers
    409 no_next_state when that column has no next. False when unknown (no project, an older Planou, the route failed)."""
    data = project_states((entry or {}).get('project') or _S['project'], now, ttl=HANDOFF_STATES_TTL)
    return any((s.get('owner') or {}).get('is_me') is True and s.get('next_state')
               for s in (data or {}).get('states') or [] if isinstance(s, dict))


MAX_HOURS = 100000


def _hours(name, value):
    if value is None or value == '': return None
    try: h = round(float(str(value).replace(',', '.')), 2)
    except ValueError: raise PlanouError(0, 'hours', f'{name}: numero de horas (ex.: 2.5)') from None
    if not 0 <= h <= MAX_HOURS: raise PlanouError(0, 'hours', f'{name}: de 0 a {MAX_HOURS} horas')
    return h


def progress(state, ref=None, pr_url=None, note=None, now=None, estimate_h=None, remaining_h=None):
    """POST /agent/tasks/{id}/progress for a task of the agent's queue (ref: pid, task_id or source_key; empty = the only
    one). started when the work begins; in_review with the draft PR (pr_url); done concludes the task; blocked with a
    note opens a decision in "Precisa de você" (the note never goes up under "minimum"). Returns the server answer.

    Handoff (Planou 0.27.0): when the agent owns a column with a next state in the task's project (owns_a_column), done
    is sent as `handoff`: Planou moves the task to the column's "Ao terminar, vai para" and to that column's owner (the
    answer has `state: "handoff"`, `project_state` and `assignee`), or concludes it when the next state is closed (the
    answer says `done`). A 409 no_next_state (the task sits in a column with no next) falls back to done. `handoff`
    asked explicitly never falls back. A 409 not_in_queue (the task left the queue)
    drops it locally and raises PlanouError with code not_in_queue: stop the work.

    Time (Planou 0.26.0, "Tempo no sync"): `estimate_h` (the agent's estimate, sent with started) and `remaining_h`
    (what is left, on every step); done without hours sends remaining_h 0. completed_h is never sent: Planou measures it
    from the cost turns tied to the task (watch_core.turns, through the current-task marker set here). An hour the
    person changed stays hers (`ignored_fields` in the answer)."""
    if state not in PROGRESS_STATES: raise PlanouError(0, 'state', f'estado {state}: use {", ".join(PROGRESS_STATES)}')
    estimate_h, remaining_h = _hours('estimate_h', estimate_h), _hours('remaining_h', remaining_h)
    if not active(): raise PlanouError(0, 'inactive', 'Planou desligado para este agente (config, chave ou modo teste)')
    tid, entry = _queue_entry(ref)
    _wait_changes(tid, entry, ref)
    # the note is free text the server shows as it came: under "minimum" it stays in the session (Planou then shows its
    # neutral "O agente parou e precisa de você para seguir."); the PR link goes up like any origin URL
    local_note = note                   # the branch of a handoff to itself stays here (self_handoffs.json)
    if _S['confidentiality'] == 'minimum': note = None
    files = []
    if note: note, files = without_paths(note)       # a file the note cites goes as an attachment (PLN0052)
    if state == 'started' and _S['confidentiality'] != 'minimum':
        files += [f for _, f in cited_files((entry or {}).get('description')) if f not in files]
    path = f'/agent/tasks/{urllib.parse.quote(tid, safe="")}/progress'

    def body(sent):
        # done sends remaining 0; a handoff does not: the task goes on with the next owner
        rem = 0 if sent == 'done' and remaining_h is None else remaining_h
        b = {k: v for k, v in {'state': sent, 'pr_url': pr_url, 'note': note}.items() if v}
        b.update({k: v for k, v in {'estimate_h': estimate_h, 'remaining_h': rem}.items() if v is not None})
        return b

    sent = 'handoff' if state == 'done' and owns_a_column(entry, now) else state
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    # queue.json is read under its lock only after the answer: the runner may note events meanwhile (no lock over HTTP)
    try:
        try:
            _, res = _call('POST', path, body(sent))
        except PlanouError as e:
            if not (state == 'done' and sent == 'handoff' and e.status == 409 and e.code == 'no_next_state'): raise
            sent = 'done'                                           # the task's column has no next: conclude it
            _, res = _call('POST', path, body(sent))
    except PlanouError as e:
        if e.status == 409 and e.code == 'not_in_queue':
            if (entry or {}).get('refining'):
                # in "Em refinamento" the task waits for the person's answer; it comes back through the queue
                raise PlanouError(409, 'refining', 'a tarefa esta em refinamento: espere a resposta do pedido') from None
            if _changes_arrived(tid, entry, ref):
                # an adjustment asked: the task waits in the queue again, it did not leave
                _wait_changes(tid, entry, ref)
            ctid, _ = _change_of(ref or tid, entry)
            if ctid: _clear_change(ctid, 'not_in_queue', now)
            with _locked('queue.json'):
                q = _queue()
                if entry is not None or tid in q['tasks']:
                    old = q['tasks'].get(tid) or {}
                    q['tasks'][tid] = {'pid': old.get('pid'), 'title': old.get('title'), 'left': 'not_in_queue',
                                       'seq': old.get('seq') or 0, 'at': stamp}
                    _save('queue.json', q)
            _stop_marker((entry or {}).get('pid'), now)
        elif e.status == 409 and e.code == 'not_released':
            with _locked('queue.json'):
                q = _queue()
                if tid in q['tasks']:
                    q['tasks'][tid].update(released=False, state='queued')
                    _save('queue.json', q)
        raise
    res = res or {}
    pid = res.get('pid') or (entry or {}).get('pid')
    if sent in ('in_review', 'done', 'handoff'):
        ctid, _ = _change_of(ref or tid, entry)          # delivered again: the adjustment is answered
        if ctid: _clear_change(ctid, sent, now)
    with _locked('queue.json'):
        q = _queue()
        if sent in ('done', 'handoff'):
            left = 'handoff' if res.get('state') == 'handoff' else 'done'
            if tid in q['tasks']: q['tasks'][tid] = {'pid': pid, 'title': q['tasks'][tid].get('title'), 'left': left,
                                                     'seq': q['tasks'][tid].get('seq') or 0, 'at': stamp}
        elif tid in q['tasks']:
            q['tasks'][tid].update(state=res.get('state') or state, pr_url=res.get('pr_url') or pr_url or q['tasks'][tid].get('pr_url'),
                                   progress_at=stamp)
            for k, v in (('estimate_h', estimate_h), ('remaining_h', remaining_h)):
                if v is not None: q['tasks'][tid][k] = v
        _save('queue.json', q)
    if sent in ('done', 'handoff'):
        if res.get('state') == 'handoff' and _handed_to_me(entry, res, now): note_self_handoff(tid, res, local_note, entry, now)
        else: forget_self_handoff(tid, pid)          # concluded, or passed to someone else: out of the own cycle
    if state == 'started' and pid: current_task(pid, now=now)
    elif state != 'started': _stop_marker(pid, now)
    if files:
        attached = {os.path.basename(f): attach_file(pid or tid, f, now=now)[0] for f in files}
        res = {**res, 'anexos': attached} if isinstance(res, dict) else res
    return res


CHANGES_TEXT_MAX = 2000
# under "minimum" the reviewer's findings stay on the PR and in the session: Planou gets only this neutral text
CHANGES_MINIMUM_TEXT = 'Ajuste pedido na revisao: o parecer esta na PR.'
# `to` of request-changes (Planou 0.49.0, PLN0164): author = whoever did the work (the first agent of the handoffs, the
# server's default), previous = whoever passed the task to the current column (the old behavior)
CHANGES_TO = ('author', 'previous')


def request_changes(text, ref=None, pr_url=None, now=None, to=None):
    """`fila ajuste` (Planou 0.43.0, PLN0139): the owner of a review column sends a task that came by another agent's
    handoff back to that agent (POST /agent/tasks/{id}/request-changes {"text", "pr_url"?}). The task goes back to the
    dev's column and to the front of the dev's queue as rework (same branch and PR); the reviewer's entry ends (done, no
    task_dequeued), so it is dropped here. pr_url absent keeps the task's. `to` (CHANGES_TO) goes only when given:
    absent, the server sends it to the author (Planou 0.49.0). Returns the server answer.
    Errors (PlanouError): 422 text empty or over 2000, pr_url not http(s) (checked here first); 409 not_in_queue (the
    task is not with the agent: dropped locally), not_released (wait), no_previous_owner (it did not come by a handoff)."""
    text = str(text or '').strip()
    if not text: raise PlanouError(0, 'text', 'diga o que ajustar (--text, ou --text - para ler do stdin)')
    if len(text) > CHANGES_TEXT_MAX: raise PlanouError(0, 'text', f'texto do ajuste acima de {CHANGES_TEXT_MAX} caracteres: resuma e deixe o parecer na PR')
    if pr_url and not re.match(r'https?://\S+$', str(pr_url).strip()): raise PlanouError(0, 'pr_url', '--pr-url: link http(s) da PR')
    if to is not None and to not in CHANGES_TO: raise PlanouError(0, 'to', f'--to: {" ou ".join(CHANGES_TO)}')
    if not active(): raise PlanouError(0, 'inactive', 'Planou desligado para este agente (config, chave ou modo teste)')
    if _S['confidentiality'] == 'minimum': text = CHANGES_MINIMUM_TEXT
    tid, entry = _queue_entry(ref)
    body = {'text': text, **({'pr_url': str(pr_url).strip()} if pr_url else {}), **({'to': to} if to else {})}
    stamp = (now or datetime.now(timezone.utc)).isoformat()

    def drop(left, pid=None):
        with _locked('queue.json'):
            q = _queue()
            if entry is not None or tid in q['tasks']:
                old = q['tasks'].get(tid) or {}
                q['tasks'][tid] = {'pid': pid or old.get('pid'), 'title': old.get('title'), 'left': left,
                                   'seq': old.get('seq') or 0, 'at': stamp}
                _save('queue.json', q)
        _stop_marker(pid or (entry or {}).get('pid'), now)

    try:
        _, res = _call('POST', f'/agent/tasks/{urllib.parse.quote(tid, safe="")}/request-changes', body)
    except PlanouError as e:
        if e.status == 409 and e.code == 'not_in_queue': drop('not_in_queue')
        raise
    res = res or {}
    drop('changes_requested', res.get('pid'))
    return res


TIME_STATES = ('started', 'in_review')


def update_time(ref=None, remaining_h=None, estimate_h=None, now=None):
    """`fila tempo`: sends the hours of a task without changing its state (the progress route needs one, so it resends
    the task's current state: started or in_review; a blocked task would rewrite its question, a task not started yet
    has no work to count). Returns the server answer."""
    if remaining_h is None and estimate_h is None: raise PlanouError(0, 'hours', 'diga --remaining-h e/ou --estimate-h')
    tid, entry = _queue_entry(ref)
    _wait_changes(tid, entry, ref)
    st = (entry or {}).get('state') or 'started'
    if st not in TIME_STATES:
        raise PlanouError(0, 'state', f'a tarefa esta em {st}: o tempo vai com o proximo passo (fila started|in_review|done)')
    return progress(st, ref, now=now, estimate_h=estimate_h, remaining_h=remaining_h)


DELIVERY_ROLES = {'dev': 'dev', 'integrator': 'integrator', 'integrador': 'integrator'}
DELIVERY_RESULTS = {'feito': 'done', 'parcial': 'partial', 'falhou': 'failed', 'done': 'done', 'partial': 'partial', 'failed': 'failed'}


def _count(name, value, limit):
    try: n = int(str(value).strip())
    except (TypeError, ValueError): raise PlanouError(0, name, f'--{name.replace("_", "-")}: numero inteiro') from None
    if not 0 <= n <= limit: raise PlanouError(0, name, f'--{name.replace("_", "-")}: de 0 a {limit}')
    return n


USAGE_KEYS = ('calls', 'input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'output')


def _delivery_usage(usage):
    """The usage as Planou takes it: up to 20 models (names up to 80 characters), each with the known counters as
    non-negative integers; models with no tokens are left out."""
    out = {}
    for model, c in (usage or {}).items():
        name = str(model or '').strip()[:80]
        if not name or not isinstance(c, dict): continue
        counters = {}
        for k in USAGE_KEYS:
            try: n = int(c.get(k) or 0)
            except (TypeError, ValueError): n = 0
            counters[k] = min(max(n, 0), 10 ** 12)
        if any(counters[k] for k in USAGE_KEYS if k != 'calls'): out[name] = counters
    return dict(sorted(out.items(), key=lambda x: -sum(v for k, v in x[1].items() if k != 'calls'))[:20])


def worker_delivery(ref, role, tokens, steps, duration_ms, result, model=None, now=None, key=None, phases=None, usage=None):
    """`fila worker`: POST /agent/tasks/{id}/deliveries, one worker (subagent) that came back for the task (ref: pid,
    task_id or source_key), with what the session got from it: tokens, steps (tool uses), duration in ms, the result
    (feito, parcial, falhou) and the role (dev or integrator). Returns the server answer, or None when this Planou has
    no such route yet (404 without an error body: an older Planou; nothing is stored).
    The key of the worker opened by `fila worker-start` (PLN0220) goes as the delivery's key, and Planou closes that
    worker with the delivery: `key` given, else the one open here for the task and role (cache/planou/workers.json),
    else the one a delivery with these same values already closed (a repeat stays `unchanged`), else a key derived from
    the values (a worker never registered).
    phases (PLN0261): seconds per phase ({'model': 540.2, ...}, watch_core.phases), sent as `phases` and scaled down to the
    duration when they add up to more; they stay out of the derived key (the same worker measured again is the same).
    usage (PLN0101): the worker's raw usage per model (watch_core.phases.usage), sent as `usage` for Planou to price;
    also out of the key. A Planou without the field ignores it (unknown JSON members are skipped)."""
    if not ref: raise PlanouError(0, 'which_task', 'diga a tarefa (PID)')
    r = DELIVERY_ROLES.get(str(role or '').strip().lower())
    if not r: raise PlanouError(0, 'role', '--role: dev ou integrator')
    res = DELIVERY_RESULTS.get(str(result or '').strip().lower())
    if not res: raise PlanouError(0, 'result', '--result: feito, parcial ou falhou')
    body = {'role': r, 'result': res, 'tokens': _count('tokens', tokens, 10 ** 12), 'steps': _count('steps', steps, 10 ** 6),
            'duration_ms': _count('duration_ms', duration_ms, 7 * 24 * 3600 * 1000)}
    m = clean_text(model or '', 80)
    if m: body['model'] = m
    if not active(): raise PlanouError(0, 'inactive', 'Planou desligado para este agente (config, chave ou modo teste)')
    tid, entry = _queue_entry(ref)
    pid = (entry or {}).get('pid') or str(ref).strip()
    # the same report sent twice (a retry, the session repeating itself) finds the same row
    sig = hashlib.sha256(json.dumps([pid.upper(), body], sort_keys=True).encode()).hexdigest()[:32]
    if key is not None:
        key = str(key).strip()
        if not WORKER_KEY.match(key): raise PlanouError(0, 'key', '--key: de 1 a 200 caracteres, so letras, numeros e . _ : -')
    else:
        key = _worker_key_for({str(ref).strip(), pid, tid}, r, sig)
    body['key'] = key or 'worker:' + sig
    if now is not None: body['finished_at'] = now.isoformat()
    if phases:
        from . import phases as _phases
        fitted = _phases.fit({k: float(v) for k, v in phases.items() if k in _phases.KEYS and float(v) > 0}, body['duration_ms'])
        if fitted: body['phases'] = fitted
    if usage:
        sent = _delivery_usage(usage)
        if sent: body['usage'] = sent
    try:
        _, out = _call('POST', f'/agent/tasks/{urllib.parse.quote(tid, safe="")}/deliveries', body)
    except PlanouError as e:
        if e.status == 404 and e.code == 'http': return None
        raise
    if key: _worker_closed(key, res, sig=sig, now=now)
    return out or {}


# ---------------------------------------------------------------- workers in progress (Planou "Workers agora", PLN0219)
#
# The session registers each worker (subagent) it starts and closes it when it comes back, so the Fila tab shows who is
# working on what. `fila worker-start` (or `worker start`) generates the key, keeps it in cache/planou/workers.json with
# the session it came from and opens it on Planou (POST /agent/workers, idempotent by the key); the delivery with the same
# key (`fila worker`) closes it, and `worker end` closes one that has no delivery (no task, or role `other`). `worker
# ping` is the PATCH (label, tasks, sign of life: without a sign for 2 h Planou shows "sem noticia"). A terminal that
# dies without session_closed leaves its workers open on Planou until a delivery or an /end: the heavy tick of the next
# session closes the ones this cache still has open from a session that is gone (reap_workers). Planou's /end takes only
# done, partial or failed ("interrupted" is set by session_closed alone), so they go as failed and are kept here as
# interrupted. Closed entries stay WORKERS_KEEP (a repeated delivery without --key finds its key again).

WORKER_ROLES = {'dev': 'dev', 'integrator': 'integrator', 'integrador': 'integrator', 'other': 'other', 'outro': 'other'}
WORKER_KEY = re.compile(r'^[A-Za-z0-9._:-]{1,200}$')
WORKER_TASKS_MAX = 20
WORKERS_KEEP = timedelta(days=7)
WORKER_LABELS = {'done': 'feito', 'partial': 'parcial', 'failed': 'falhou', 'interrupted': 'interrompido'}


def _workers():
    w = _load('workers.json', {})
    if not isinstance(w, dict) or not isinstance(w.get('workers'), dict): w = {'workers': {}}
    return w


def _workers_save(w, now=None):
    cut = ((now or datetime.now(timezone.utc)) - WORKERS_KEEP).isoformat()
    w['workers'] = {k: v for k, v in w['workers'].items()
                    if isinstance(v, dict) and (v.get('state') == 'running' or (v.get('ended_at') or '') >= cut)}
    _save('workers.json', w)


def _caller_session():
    """The Claude Code session running this command (its sessions/<pid>.json among the ancestors), else the one the
    runner last saw (cache/planou/session.json). None outside a session."""
    try:
        from . import transcript
        d = transcript.find_session()
        if d and d.get('sessionId'): return str(d['sessionId'])
    except Exception:
        pass
    return _session_now()


def _norm_ref(x):
    return str(x or '').strip().upper()


def _worker_key_for(refs, role, sig):
    """The key of the local worker a delivery without --key belongs to: the one open for the task and role; else the one
    a delivery with the same values (sig) already closed. Two or more open: PlanouError (say --key)."""
    refs = {_norm_ref(x) for x in refs if x}
    ws = _workers()['workers']
    mine = [(k, v) for k, v in ws.items() if v.get('role') == role and refs & {_norm_ref(t) for t in v.get('tasks') or []}]
    running = [k for k, v in mine if v.get('state') == 'running']
    if len(running) > 1:
        raise PlanouError(0, 'key', f'{len(running)} workers abertos nesta tarefa e papel ({", ".join(sorted(running))}): diga --key')
    if running: return running[0]
    again = [k for k, v in mine if v.get('delivery_sig') == sig]
    return again[0] if again else None


def _worker_closed(key, result, sig=None, now=None):
    now = now or datetime.now(timezone.utc)
    with _locked('workers.json'):
        w = _workers()
        v = w['workers'].get(key)
        if v is None: return
        if v.get('state') == 'running': v.update(state='ended', result=result, ended_at=now.isoformat())
        if sig: v['delivery_sig'] = sig
        _workers_save(w, now)


def _worker_body(v, with_key=True):
    body = {'role': v['role'], 'tasks': list(v.get('tasks') or []), 'label': v['label'], 'started_at': v['started_at']}
    if with_key: body['key'] = v['key']
    return body


def _new_worker_key(sid, tasks):
    head = re.sub(r'[^A-Za-z0-9]', '', str(sid or ''))[:8] or 'local'
    task = re.sub(r'[^A-Za-z0-9._-]', '-', str(tasks[0]))[:40] if tasks else 'w'
    return f'{head}:{task}:{secrets.token_hex(3)}'


def worker_start(tasks, role, label, now=None):
    """`fila worker-start` / `worker start`: registers a worker the session just started (tasks: pid, task_id or
    source_key, 0 to 20; role dev, integrator or other; label: what it is doing, one line). Returns (key, warning):
    the key is always kept here, so the delivery or `worker end` finds it even when Planou is off, old or down (the
    warning says so). A refusal by Planou (422: a task that is not the agent's, a bad label) raises PlanouError and
    keeps nothing."""
    r = WORKER_ROLES.get(str(role or '').strip().lower())
    if not r: raise PlanouError(0, 'role', '--role: dev, integrator ou other')
    lab = clean_text(label or '', 200)
    if not lab: raise PlanouError(0, 'label', '--label: o que o worker esta fazendo, numa linha')
    refs, seen = [], set()
    for t in tasks or []:
        t = str(t or '').strip()
        if t and _norm_ref(t) not in seen:
            seen.add(_norm_ref(t)); refs.append(t)
    if len(refs) > WORKER_TASKS_MAX: raise PlanouError(0, 'tasks', f'no maximo {WORKER_TASKS_MAX} tarefas por worker')
    now = now or datetime.now(timezone.utc)
    sid = _caller_session()
    key = _new_worker_key(sid, refs)
    v = {'key': key, 'role': r, 'tasks': refs, 'label': lab, 'session': sid, 'started_at': now.isoformat(),
         'state': 'running', 'sent': False}
    warning = None
    if not active():
        warning = 'Planou desligado para este agente: worker guardado so aqui'
    else:
        try:
            _call('POST', '/agent/workers', _worker_body(v))
            v['sent'] = True
        except PlanouError as e:
            if e.status == 422 or (e.status == 404 and e.code != 'http'): raise
            warning = ('este Planou ainda nao tem a rota de workers (versao antiga)' if e.status == 404 else
                       f'Planou nao respondeu ({e.message}): worker guardado aqui, o `worker ping` tenta de novo')
    with _locked('workers.json'):
        w = _workers()
        w['workers'][key] = v
        _workers_save(w, now)
    return key, warning


def _worker_local(key):
    key = str(key or '').strip()
    if not WORKER_KEY.match(key): raise PlanouError(0, 'key', 'key: de 1 a 200 caracteres, so letras, numeros e . _ : -')
    return key, _workers()['workers'].get(key)


def worker_ping(key, label=None, tasks=None, now=None):
    """`worker ping`: PATCH /agent/workers/{key} (label and tasks when given; the call is the worker's sign of life). A
    worker Planou never got (it was down at the start) is opened now instead. Returns the server answer (None: Planou off)."""
    key, v = _worker_local(key)
    lab = None
    if label is not None:
        lab = clean_text(label, 200)
        if not lab: raise PlanouError(0, 'label', '--label: o que o worker esta fazendo, numa linha')
    refs = [str(t).strip() for t in tasks or [] if str(t or '').strip()] if tasks else None
    if refs is not None and len(refs) > WORKER_TASKS_MAX: raise PlanouError(0, 'tasks', f'no maximo {WORKER_TASKS_MAX} tarefas por worker')
    if v and v.get('state') != 'running': raise PlanouError(0, 'ended', f'worker {key} ja fechado ({WORKER_LABELS.get(v.get("result"), v.get("result"))})')
    if v:
        with _locked('workers.json'):
            w = _workers()
            cur = w['workers'].get(key) or v
            if lab: cur['label'] = lab
            if refs is not None: cur['tasks'] = refs
            _workers_save(w, now)
            v = cur
    if not active(): return None
    if v and not v.get('sent'):
        _, out = _call('POST', '/agent/workers', _worker_body(v))
        with _locked('workers.json'):
            w = _workers()
            if key in w['workers']: w['workers'][key]['sent'] = True
            _workers_save(w, now)
        return out or {}
    body = {}
    if lab: body['label'] = lab
    if refs is not None: body['tasks'] = refs
    _, out = _call('PATCH', f'/agent/workers/{urllib.parse.quote(key, safe=":")}', body)
    return out or {}


def worker_end(key, result, now=None):
    """`worker end`: POST /agent/workers/{key}/end with done, partial or failed (feito, parcial, falhou): the way out
    of a worker without a delivery (no task, or role other). Returns the server answer (None: Planou off, or a worker
    Planou never got; closed here all the same)."""
    res = DELIVERY_RESULTS.get(str(result or '').strip().lower())
    if not res: raise PlanouError(0, 'result', '--result: feito, parcial ou falhou')
    key, v = _worker_local(key)
    out = None
    if active() and (v is None or v.get('sent')):
        _, out = _call('POST', f'/agent/workers/{urllib.parse.quote(key, safe=":")}/end', {'result': res})
        out = out or {}
    _worker_closed(key, res, now=now)
    return out


def workers_view():
    """`worker ver`: the workers kept here (open first), as the session registered them."""
    ws = sorted(_workers()['workers'].values(), key=lambda v: (v.get('state') != 'running', v.get('started_at') or ''))
    return [{k: v.get(k) for k in ('key', 'state', 'role', 'tasks', 'label', 'session', 'started_at', 'ended_at', 'result')
             if v.get(k) is not None} for v in ws]


def _session_gone(sid):
    try:
        from . import transcript
        return not transcript.session_alive(sid)
    except Exception:
        return False


def reap_workers(now=None):
    """The heavy tick of a new session: the workers this cache still has open from a session that is gone (its terminal
    died without session_closed) are closed on Planou (/end failed: the route has no interrupted) and kept here as
    interrupted. Only when the current session is known, differs from the worker's and the worker's is not alive.
    Returns lines for the tick output."""
    cur = _session_now()
    if not cur: return []
    stale = [v for v in _workers()['workers'].values()
             if v.get('state') == 'running' and v.get('session') and v['session'] != cur and _session_gone(v['session'])]
    out = []
    for v in stale:
        if v.get('sent') and active():
            try:
                _call('POST', f'/agent/workers/{urllib.parse.quote(v["key"], safe=":")}/end', {'result': 'failed'})
            except PlanouError as e:
                if not (e.status == 404 and e.code != 'http'):
                    continue                    # Planou down: next tick
        _worker_closed(v['key'], 'interrupted', now=now)
        tasks = ', '.join(v.get('tasks') or []) or 'sem tarefa'
        out.append(f'-- WORKER INTERROMPIDO {v["key"]} ({tasks}): {one_line(v.get("label"), 120)} -> a sessao anterior acabou '
                   'sem fechar este worker; fechado no Planou. Se a tarefa segue com o agente, delegar de novo (worker-start novo)')
    return out


def _workers_session_closed(session_id=None, now=None):
    """session_closed reached Planou, which ended the open workers as interrupted: the same here (those of that session,
    or all without one)."""
    now = now or datetime.now(timezone.utc)
    with _locked('workers.json'):
        w = _workers()
        changed = False
        for v in w['workers'].values():
            if v.get('state') == 'running' and (not session_id or not v.get('session') or v['session'] == session_id):
                v.update(state='ended', result='interrupted', ended_at=now.isoformat())
                changed = True
        if changed: _workers_save(w, now)


def _stop_marker(pid, now=None):
    """Closes the current-task marker (cost attribution) when it points at this task."""
    if pid and current_task(now=now) == str(pid).upper(): current_task('-', now=now)


def refine(question, ref=None, options=(), now=None):
    """Before starting a queue task whose scope is not clear (goal, deliverable, done, constraints, access): asks the
    person. The agent's own task (its source_key) goes to the project state "Em refinamento" (a one-task sync now; the
    heavy tick keeps it there until the answer) with the question as a decision tied to it: it frees the slot, and the
    answer brings the task back to A fazer and to the queue. A task the person created cannot take a project state from
    the agent: it goes `blocked` with the question as its note (the same "Precisa de você" ask). Returns
    {"mode": "refining"|"blocked", "pid", "ask"}."""
    if not active(): raise PlanouError(0, 'inactive', 'Planou desligado para este agente (config, chave ou modo teste)')
    q = ' '.join(str(question or '').split())
    if not q: raise PlanouError(0, 'question', 'diga o que precisa acertar (--note)')
    tid, entry = _queue_entry(ref)
    if entry is None: raise PlanouError(0, 'which_task', f'{ref}: nao esta na fila local (fila ver)')
    pid = entry.get('pid')
    prefix = _S['agent'] + ':'
    sk = entry.get('source_key') or ''
    st = _load('state.json', {})
    code = sk[len(prefix):] if sk.startswith(prefix) else None
    if not code or code not in (st.get('tasks') or {}):
        res = progress('blocked', tid, note=q, now=now)     # opens the decision (under "minimum" without the note)
        return {'mode': 'blocked', 'pid': pid, 'ask': res.get('ask') or res.get('code')}
    ask = ask_question(q, options, task=pid, now=now)
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with _locked('state.json'):
        st = _load('state.json', {})
        if code in (st.get('tasks') or {}):
            st['tasks'][code]['refining'] = {'ask': ask.get('code'), 'since': stamp}
            _save('state.json', st)
    with _locked('queue.json'):
        qq = _queue()
        if tid in qq['tasks']: qq['tasks'][tid].update(refining=True, state='refining', progress_at=stamp); _save('queue.json', qq)
    _stop_marker(pid, now)
    body = _load('sent.json', {}).get(code)
    if body:
        t = {k: v for k, v in body.items() if k not in ('state', 'resolution', 'completed_at', 'waiting', 'ready_reason')}
        t.update(project_state=REFINEMENT, base_version=((st.get('tasks') or {}).get(code) or {}).get('version'))
        try:
            _, res = _call('POST', '/agent/sync', {'agent': _S['agent'], 'generated_at': stamp, 'tasks': [t]})
            for r in (res or {}).get('tasks') or []:
                if r.get('pid') and r.get('result') in ('created', 'updated', 'unchanged'):
                    with _locked('state.json'):
                        st = _load('state.json', {})
                        if code in (st.get('tasks') or {}):
                            st['tasks'][code].update(version=r.get('version'), project_state=REFINEMENT)
                            _save('state.json', st)
                note_warnings(code, list(r.get('warnings') or []) + ([r['reason']] if r.get('reason') else []), now)
        except PlanouError as e:
            _log('warnings.log', f'{code}: refinamento fica para o proximo tick: {e.message}', now)
    return {'mode': 'refining', 'pid': pid, 'ask': ask.get('code')}


# ---------------------------------------------------------------- asks ("Precisa de você", spec 5.2)

def _idem(kind, ref, *parts):
    """Idempotency key: the agent's own reference when it has one, else a hash of the normalized input."""
    if ref: return f'{_S["agent"]}:{kind}:{ref}'[:128]
    norm = '|'.join(' '.join(str(x or '').split()).lower() for x in parts)
    return f'{_S["agent"]}:{kind}:' + hashlib.sha256(norm.encode()).hexdigest()[:32]


def _ask(kind, body, ref, *parts):
    """POST /asks; records the code locally (cache/planou/asks.json) so the answer can be matched later."""
    if not active(): raise PlanouError(0, 'inactive', 'Planou desligado para este agente (config, chave ou modo teste)')
    body = {k: v for k, v in {**body, 'kind': kind, 'idempotency_key': _idem(kind, ref, *parts)}.items() if v is not None}
    _, res = _call('POST', '/asks', body)
    asks = _load('asks.json', {})
    asks[res['code']] = {'kind': kind, 'ref': ref, 'task': body.get('task'), 'title': body.get('title'),
                         'created': datetime.now(timezone.utc).isoformat()}
    _save('asks.json', asks)
    return res


def request_decision(title, options, context=None, task=None, ref=None, free_text=False, due=None):
    """options: [(key, text, recommended), ...] or [{"key","text","recommended"}]."""
    opts = [o if isinstance(o, dict) else {'key': o[0], 'text': o[1], 'recommended': bool(o[2]) if len(o) > 2 else False} for o in options]
    return _ask('decision', {'title': title, 'context': context, 'task': task, 'options': opts,
                             'accepts_free_text': bool(free_text), 'due_at': due}, ref, title, task, *[o['text'] for o in opts])


def request_approval(action_type, verb, title, context=None, task=None, ref=None, description=None, irreversible=True):
    """Asks the person's OK for an action the agent would execute. In this phase the answer only records the OK:
    the agent sends nothing because of it."""
    return _ask('approval', {'title': title, 'context': context, 'task': task,
                             'action': {'type': action_type, 'verb': verb, 'description': description, 'irreversible': bool(irreversible)}},
                ref, title, task, action_type)


def submit_draft(channel, to, text, title, context=None, task=None, subject=None, ref=None):
    """A draft for the person to send. The agent never sends it. Without drafts_text (config "drafts_in_planou") the text
    stays on this machine: the ask says who and where, and the person reads the text in the session."""
    up = text if _S['drafts_text'] else None
    return _ask('draft', {'title': title, 'context': context, 'task': task,
                          'draft': {'channel': channel, 'to': to, 'subject': subject, 'text': up}}, ref, title, task, to, text)


YES_NO = [('S', 'Sim', False), ('N', 'Não', False)]


def ask_question(question, options=(), context=None, task=None, ref=None, now=None):
    """A direct question the session asks the person ("Quer que eu ...?", "Posso ...?", a choice) as a Planou decision:
    Sim/Não without options, the options otherwise, always with a free answer. The confidentiality applies: "minimum"
    sends a neutral title and "Opção A/B" (Sim/Não stay), "detail" also sends the context. The ref (idempotency key) is
    a hash of the real question, kept on this machine, so the same question is never opened twice and two questions
    never collide under the same neutral title. The local date is part of it: the same question asked on another day is
    a new ask (Planou answers an idempotency key it knows with the old ask, answered or not). `ref` overrides it. The
    answer comes back as `-- DECISAO <code> ... (pergunta da sessao)`."""
    q = ' '.join(str(question or '').split())
    opts = [(str(o['key']), str(o['text']), bool(o.get('recommended'))) if isinstance(o, dict)
            else (str(o[0]), str(o[1]), bool(o[2]) if len(o) > 2 else False) for o in options]
    opts = opts[:8] if len(opts) >= 2 else YES_NO
    day = (now or datetime.now(timezone.utc)).astimezone().date().isoformat()
    ref = 'q-' + (re.sub(r'[^A-Za-z0-9_.-]', '-', ref)[:60] if ref else
                  hashlib.sha256(json.dumps([q, [o[:2] for o in opts], task, day], ensure_ascii=False).encode()).hexdigest()[:16])
    conf = _S['confidentiality']
    if conf == 'minimum':
        title = f'Pergunta do agente ({hashlib.sha256(ref.encode()).hexdigest()[:6]})'
        if opts != YES_NO: opts = [(k, f'Opção {k}', r) for k, _t, r in opts]
    else:
        title = q[:300]
    ctx = (context or q)[:4000] if conf == 'detail' else None
    return request_decision(title, opts, ctx, task, ref, free_text=True)


def decide_question(question, options=(), choice=None, context=None, task=None, ref=None, now=None):
    """A question the agent decided on its own (PLN0225): the decision has a recommended option inside the autonomy, so
    the agent follows it instead of waiting. /v1 has no way for the agent to answer its own ask (the answer is the
    person's, on /api), so the decision already taken goes to Planou as a low severity alert "Decidi: <question> ->
    <option>" with the context, and no decision stays open in Precisa de você. `choice` is the option key (default: the
    recommended one); Sim/Não without options. The confidentiality applies as in ask_question: "minimum" sends a neutral
    question and "Opção A" (Sim/Não stay), "detail" also sends the context and the options. The ref (a hash of the real
    question, the options, the task and the local date, or `ref`) keeps the same decision from being recorded twice."""
    q = ' '.join(str(question or '').split())
    opts = [(str(o['key']), str(o['text']), bool(o.get('recommended'))) if isinstance(o, dict)
            else (str(o[0]), str(o[1]), bool(o[2]) if len(o) > 2 else False) for o in options]
    opts = opts[:8] if len(opts) >= 2 else YES_NO
    key = choice if choice is not None else next((k for k, _t, r in opts if r), None)
    hit = [o for o in opts if key is not None and o[0].upper() == str(key).strip().upper()]
    if not hit: raise ValueError(f'decisao sem opcao escolhida: {key!r} nao e uma das opcoes ({", ".join(o[0] for o in opts)})')
    k, text = hit[0][:2]
    day = (now or datetime.now(timezone.utc)).astimezone().date().isoformat()
    ref = 'q-auto-' + (re.sub(r'[^A-Za-z0-9_.-]', '-', ref)[:60] if ref else
                       hashlib.sha256(json.dumps([q, [o[:2] for o in opts], task, day], ensure_ascii=False).encode()).hexdigest()[:16])
    conf = _S['confidentiality']
    if conf == 'minimum':
        qt = f'pergunta do agente ({hashlib.sha256(ref.encode()).hexdigest()[:6]})'
        shown = [(o[0], o[1] if opts == YES_NO else f'Opção {o[0]}') for o in opts]
    else:
        qt = q
        shown = [(o[0], ' '.join(o[1].split())) for o in opts]
    chosen = dict(shown)[k][:120]
    tail = f' -> {chosen}'
    head = 'Decidi: '
    room = 300 - len(head) - len(tail)
    title = head + (qt if len(qt) <= room else qt[:room - 3].rstrip() + '...') + tail
    ctx = f'decidido pelo agente: {chosen}'
    if conf == 'detail':
        ctx += '\nOpções: ' + '; '.join(f'{ok}{" (escolhida)" if ok == k else ""}: {ot}' for ok, ot in shown)
        if context: ctx += '\n\n' + str(context)
    return raise_alert(title, 'agent_decided', 'low', ctx[:4000], task, ref)


def raise_alert(title, code='agent', severity='medium', context=None, task=None, ref=None):
    return _ask('alert', {'title': title, 'context': context, 'task': task, 'alert': {'code': code, 'severity': severity}},
                ref, title, task, code)


def cancel_ask(code, reason=None):
    if not active(): return False
    _call('POST', f'/asks/{urllib.parse.quote(code)}/cancel', {'reason': reason})
    return True


def report_result(code, result, reason=None):
    """Closes the cycle of an answered ask: result is 'executed' or 'failed'."""
    if not active(): return False
    _call('POST', f'/asks/{urllib.parse.quote(code)}/result', {'result': result, 'reason': reason})
    return True


# ---------------------------------------------------------------- proposals (Planou 0.53.0)

PROPOSAL_EVENT = 'proposal_changed'
PROPOSAL_CHANGES = ('param', 'role')
PROPOSAL_PARAMS = ('wip', 'release_min_branches', 'release_max_wait_min', 'release_e2e_every_h', 'priority')
PROPOSAL_DOCS = ('instructions', 'context', 'behavior_config', 'behaviors')
PROPOSAL_FIELDS = ('reason', 'param', 'value', 'agent', 'project', 'task', 'doc', 'name', 'content', 'enabled',
                   'base_sha256')
PROPOSAL_STATUS = {'open': 'ABERTA', 'applied': 'APLICADA', 'rejected': 'RECUSADA', 'failed': 'FALHOU',
                   'cancelled': 'RETIRADA', 'reverted': 'DESFEITA'}
PROPOSAL_THEN = {'applied': 'no ar; medir o efeito', 'rejected': 'nada mudou; nao aplicar a mao',
                 'failed': 'nada mudou (o alvo mudou desde a proposta)', 'reverted': 'voltou o valor de antes',
                 'cancelled': 'retirada'}


def _proposal_asks():
    return {v.get('ask_code') for v in _load('proposals.json', {}).values() if v.get('ask_code')}


def _keep_proposal(res, ref=None):
    props = _load('proposals.json', {})
    pid = str(res.get('proposal_id'))
    old = props.get(pid) or {}
    props[pid] = {**old, 'ask_code': res.get('ask_code'), 'change': res.get('change'), 'target': res.get('target'),
                  'status': res.get('status'), 'before': res.get('before'), 'after': res.get('after'),
                  'result': res.get('result'), 'ref': ref if ref is not None else old.get('ref'),
                  'at': datetime.now(timezone.utc).isoformat()}
    _save('proposals.json', props)


def propose(change, title, ref=None, **fields):
    """POST /agent/proposals: a parameter (`param`, `value`, and `agent`, `project` or `task`) or a Papel change (`doc`
    with `name`, `content` and `base_sha256`, or `enabled`). Returns Planou's answer (201 new, 200 `reused` for the same
    ref). Raises PlanouError: 422 `validation` or `unchanged` (already the current value), 404, 409 on a Papel change
    (`not_reported`, `stale`, `no_content`, ...), 0 `inactive` when this agent does not publish to Planou."""
    if not active(): raise PlanouError(0, 'inactive', 'Planou desligado para este agente (config, chave ou modo teste)')
    if change not in PROPOSAL_CHANGES: raise ValueError(f'change {change!r}')
    body = {'change': change, 'title': ' '.join(str(title or '').split())[:300]}
    body.update({k: v for k, v in fields.items() if k in PROPOSAL_FIELDS and v is not None})
    body['idempotency_key'] = _idem('proposal', ref, change, title, json.dumps(fields, sort_keys=True, default=str))
    _, res = _call('POST', '/agent/proposals', body)
    _keep_proposal(res, ref)
    return res


def proposal_state(proposal_id):
    """GET /agent/proposals/{id} (a read: works whenever there is a key). Raises PlanouError."""
    _, res = _call('GET', f'/agent/proposals/{urllib.parse.quote(str(proposal_id))}')
    if str(proposal_id) in _load('proposals.json', {}): _keep_proposal(res)
    return res


def cancel_proposal(proposal_id, reason=None):
    if not active(): return None
    _, res = _call('POST', f'/agent/proposals/{urllib.parse.quote(str(proposal_id))}/cancel',
                   {'reason': reason} if reason else {})
    if str(proposal_id) in _load('proposals.json', {}): _keep_proposal(res)
    return res


def proposal_note(ev):
    """A `proposal_changed` updates the proposal kept in cache/planou/proposals.json (status, before, after, result)."""
    if (ev or {}).get('type') != PROPOSAL_EVENT: return
    p = ev.get('payload') or {}
    if str(p.get('proposal_id')) in _load('proposals.json', {}): _keep_proposal(p)


def proposal_line(p):
    """The `proposal_changed` line: the ask code, what changed, before -> after (a parameter), the result and who."""
    status = str(p.get('status') or '')
    head = f'-- PROPOSTA {one_line(p.get("ask_code"), 20)} {PROPOSAL_STATUS.get(status, status.upper() or "?")}'
    what = f'{one_line(p.get("change"), 10)} {one_line(p.get("target"), 80)}'
    if p.get('before') is not None or p.get('after') is not None:
        what += f': {one_line(p.get("before"), 40)} -> {one_line(p.get("after"), 40)}'
    by = p.get('by') or {}
    who = one_line(by.get('display_name') or by.get('name'), 60) if isinstance(by, dict) else ''
    extra = f' ({one_line(p.get("result"), 200)})' if p.get('result') else ''
    return (f'{head}: {what}{extra}' + (f' por {who}' if who else '') + f' ({_when(p)})'
            + f' -> {PROPOSAL_THEN.get(status, "ver GET /v1/agent/proposals/{id}")}')


ASK_ANSWERS = ('decision_answered', 'approved', 'rejected', 'draft_sent', 'draft_discarded', 'alert_acknowledged')


def _answered(code):
    """An answer arrived: the ask it belongs to is never withdrawn nor opened again by reconcile_asks."""
    if not code: return
    st = _load('open_asks.json', {})
    for v in st.values():
        if v.get('code') == code: v['answered'] = True
    _save('open_asks.json', st)


def ask_ref(event):
    """The agent's ref of the ask an answer belongs to ('' when the ask had none): from the idempotency key
    '<agent>:<kind>:<ref>'."""
    k = str((event.get('payload') or {}).get('idempotency_key') or '')
    parts = k.split(':', 2)
    return parts[2] if len(parts) == 3 and parts[0] == _S['agent'] else ''


def reconcile_asks(desired):
    """Keeps the asks that come from the agent state in step with it (heavy tick, after sync).

    desired: {ref: {'kind': 'decision' | 'alert', 'title', 'options' [(key, text, recommended)] and 'free_text'
                    (decision: no options needs free_text), 'context', 'task', 'code' and 'severity' (alert)}}
    A question to the person is always a decision (an alert only has "Ciente"); alerts are for what needs no answer.
    A ref not open yet is opened (the ref is the idempotency key, so a lost local file never duplicates one); an open ref
    that left `desired` is withdrawn (cancel); an answered one is only forgotten. Returns lines for the tick output
    (AVISO for a refused ask, the usual failure lines for Planou down)."""
    if not active(): return []
    st = _load('open_asks.json', {})
    lines = []
    for ref, d in desired.items():
        if ref in st: continue
        try:
            if d.get('kind') == 'decision':
                res = request_decision(d['title'], d['options'], d.get('context'), d.get('task'), ref, d.get('free_text', False))
            else:
                res = raise_alert(d['title'], d.get('code') or 'agent', d.get('severity') or 'medium', d.get('context'), d.get('task'), ref)
        except PlanouError as e:
            lines.append(f'AVISO (planou): pedido {ref} recusado: {e.message}' if 400 <= e.status < 500 else _failed(e.message if e.status else e))
            continue
        _ok()
        st[ref] = {'code': res.get('code'), 'answered': res.get('state') not in (None, 'open', 'in_window'),
                   'at': datetime.now(timezone.utc).isoformat()}
    for ref in [r for r in st if r not in desired]:
        v = st[ref]
        if not v.get('answered'):
            try:
                cancel_ask(v['code'], 'resolvido fora do Planou')
            except PlanouError as e:
                if not 400 <= e.status < 500:       # 404 / 409: gone or already closed on the server
                    lines.append(_failed(e.message if e.status else e)); continue
        st.pop(ref)
    _save('open_asks.json', st)
    return lines


# ---------------------------------------------------------------- tools ("Ferramentas do agente", Planou 0.14)

# The agent's sources and tools, one entry each, shown on the agent's Ferramentas tab. The heavy tick builds the list
# (set_tools) from what it knows: the sources of its config, the broken ones (the `quebrado` state and its `desde`), how
# to fix each one (the `conserto` of watch_core.tasks) and a credential's expiry when it is known. The list stays in
# cache/planou/tools.json and any heartbeat carries it (the runner's CLI ones too) when it changed or an hour went by.
# Planou opens the tool_failing and tool_expiring alerts itself: the agent never raises an alert for that.
TOOLS_EVERY = timedelta(hours=1)           # resend the whole list at least this often (the server marks it stale at 3 h)
TOOLS_MAX_AGE = timedelta(hours=3)         # a list the heavy tick has not rebuilt for this long is not sent any more
MAX_TOOLS = 50
TOOL_KINDS = ('source', 'mcp', 'other')
TOOL_STATUSES = ('ok', 'failing', 'expiring', 'off')
CREDENTIAL_TYPES = ('token', 'session_cookie', 'oauth', 'api_key', 'none', 'other')
_TOOL_KEY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,79}$')
# The server's own detector (Planou.Api/Agents/AgentTools.cs): one hit refuses the whole list, so the text that leaves
# is checked with the same patterns first.
_SECRET_VALUE = re.compile(
    r'\bbearer\s+[A-Za-z0-9._~+/=-]{8,}'
    r'|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bglpat-[A-Za-z0-9_-]{16,}'
    r'|\bxox[abeprs]-[A-Za-z0-9-]{10,}|\bpl_ag_[A-Za-z0-9_]{8,}|\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b'
    r'|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'
    r'|[?&](access_token|token|api_?key|key|password|secret|sig|code)=[^&\s]{6,}'
    r'|\b(password|passwd|secret|api[_-]?key|token|cookie)\s*[:=]\s*(?=\S*[A-Za-z])(?=\S*[0-9])\S{16,}'
    r'|(?-i:\b[a-z2-7]{52}\b)', re.I)
_OPAQUE = re.compile(r'[A-Za-z0-9+_=-]{40,}')


def _mixed(s):
    return any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s)


def looks_secret(text):
    """True when Planou would take the text for a credential (and refuse the whole list)."""
    if not text: return False
    t = str(text)
    return bool(_SECRET_VALUE.search(t)) or any(_mixed(m.group(0)) for m in _OPAQUE.finditer(t))


def clean_text(text, limit=300):
    """One line, without query strings, tokens or opaque sequences, cut at `limit`; None when nothing safe is left."""
    if text is None: return None
    t = ' '.join(str(text).split()).replace(' — ', ': ').replace('—', '-')
    t = re.sub(r'(https?://[^\s?#]+)[?#]\S*', r'\1', t)
    t = _SECRET_VALUE.sub('[removido]', t)
    t = _OPAQUE.sub(lambda m: '[removido]' if _mixed(m.group(0)) else m.group(0), t)
    t = t[:limit].strip()
    return t if t and not looks_secret(t) else None


def tool_key(text):
    """A valid tool key from a name ('jira diario' -> 'jira-diario', accents dropped)."""
    import unicodedata
    t = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode()
    t = re.sub(r'[^A-Za-z0-9._:/@-]+', '-', t).strip('-._:/@')[:80]
    return t or None


def _iso(v):
    if not v: return None
    try:
        d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def tool(key, status, label=None, kind='source', credential=None, expires_at=None, error=None, failing_since=None,
         fix=None, last_ok_at=None):
    """One entry of the list, only with the fields of the contract. `credential` is the TYPE (token, session_cookie,
    oauth, api_key, none, other), never a value; `expires_at` only when the expiry is really known."""
    t = {'key': key, 'label': label or key, 'kind': kind if kind in TOOL_KINDS else 'other',
         'status': status if status in TOOL_STATUSES else 'failing'}
    if last_ok_at: t['last_ok_at'] = _iso(last_ok_at)
    if t['status'] == 'failing':
        if failing_since and _iso(failing_since): t['failing_since'] = _iso(failing_since)
        if error: t['last_error'] = clean_text(error) or 'erro omitido (o texto parecia ter uma credencial)'
    if credential:
        t['credential'] = {'type': credential if credential in CREDENTIAL_TYPES else 'other'}
        if expires_at and _iso(expires_at): t['credential']['expires_at'] = _iso(expires_at)
    if fix: t['fix'] = fix
    return t


def source_tools(sources, broken=None):
    """Tools from the agent's sources. `sources`: [{'name', 'label'?, 'kind'?, 'credential'?, 'expires_at'?, 'fix'?,
    'off'?}]; `broken`: the state's broken sources, {name: {'erro'|'error', 'desde'|'since'}} (a source missing from it
    ran clean). A broken source is `failing` since its `desde`, with its error cleaned of secrets."""
    broken = broken if isinstance(broken, dict) else {}
    out = []
    for s in sources:
        b = broken.get(s['name'])
        b = b if isinstance(b, dict) else ({} if b is None else {'erro': str(b)})
        status = 'off' if s.get('off') else 'failing' if s['name'] in broken else 'ok'
        out.append(tool(s.get('key') or s['name'], status, s.get('label'), s.get('kind', 'source'), s.get('credential'),
                        s.get('expires_at'), b.get('erro') or b.get('error'), b.get('desde') or b.get('since'), s.get('fix')))
    return out


def _tool_clean(t):
    """The entry as it leaves: valid key, contract fields only, texts cleaned. None when it cannot go."""
    key = t.get('key') if _TOOL_KEY.match(str(t.get('key') or '')) and not looks_secret(t.get('key')) else tool_key(t.get('key'))
    if not key or looks_secret(key): return None
    out = {'key': key, 'label': clean_text(t.get('label'), 80) or key,
           'kind': t.get('kind') if t.get('kind') in TOOL_KINDS else 'other',
           'status': t.get('status') if t.get('status') in TOOL_STATUSES else 'failing'}
    for f in ('last_ok_at', 'failing_since'):
        if _iso(t.get(f)): out[f] = _iso(t[f])
    if t.get('last_error'):
        err = clean_text(t['last_error'])
        out['last_error'] = err or 'erro omitido (o texto parecia ter uma credencial)'
    c = t.get('credential')
    if isinstance(c, dict) and c.get('type'):
        out['credential'] = {'type': c['type'] if c['type'] in CREDENTIAL_TYPES else 'other'}
        if _iso(c.get('expires_at')): out['credential']['expires_at'] = _iso(c['expires_at'])
    fix = clean_text(t.get('fix'))
    if fix: out['fix'] = fix
    return out


def set_tools(tools, now=None):
    """The heavy tick's list of tools: cleaned, unique keys, at most 50, `last_ok_at` kept per key (the last tick the
    tool was ok), saved for the next heartbeat (heartbeat() decides when it goes). Returns the list as it will leave."""
    if not _S['root']: return []
    now = now or datetime.now(timezone.utc)
    last_ok = _load('tools_ok.json', {})
    out, seen = [], set()
    for t in tools or []:
        c = _tool_clean(t or {})
        if not c or c['key'] in seen: continue
        seen.add(c['key'])
        if c['status'] == 'ok': last_ok[c['key']] = c.get('last_ok_at') or now.isoformat()
        if last_ok.get(c['key']): c['last_ok_at'] = last_ok[c['key']]
        out.append(c)
        if len(out) >= MAX_TOOLS: break
    _save('tools_ok.json', {k: v for k, v in last_ok.items() if k in seen})
    _save('tools.json', {'at': now.isoformat(), 'tools': out})
    return out


def _tools_sig(tools):
    """What makes the list 'changed': not the times the tools were last ok nor the error text of a failure that goes on."""
    keep = [{k: v for k, v in t.items() if k not in ('last_ok_at', 'last_error')} for t in tools]
    return hashlib.sha256(json.dumps(keep, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _tools_due(now):
    """(sig, list) when the list must go with this heartbeat: it changed since the last one Planou took, or an hour went
    by. None when there is no list, it is older than 3 h (the heavy tick stopped: Planou shows it stale) or it is the
    same list Planou refused last time (not repeated in a row)."""
    if not _S['root']: return None
    data = _load('tools.json', None)
    if not isinstance(data, dict) or not isinstance(data.get('tools'), list): return None
    at = _date_time(data.get('at'))
    if not at or now - at > TOOLS_MAX_AGE: return None
    sig = _tools_sig(data['tools'])
    sent = _load('tools_sent.json', {})
    if sig == sent.get('refused'): return None
    last = _date_time(sent.get('at'))
    if sig == sent.get('sig') and last and now - last < TOOLS_EVERY: return None
    return sig, data['tools']


def _date_time(v):
    try:
        d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _log(name, line, now=None):
    try:
        f = os.path.join(_cache_dir(), name)
        os.makedirs(_cache_dir(), exist_ok=True)
        old = open(f).read().splitlines()[-199:] if os.path.exists(f) else []
        with open(f, 'w') as fh:
            fh.write('\n'.join(old + [f'{(now or datetime.now(timezone.utc)).isoformat(timespec="seconds")}\t{line}']) + '\n')
    except OSError:
        pass


def _tools_refused(e, sig, now):
    """422 secret_refused / invalid_tools: the heartbeat counted, the list did not. Logged with the field paths only
    (never a value), and the same list is not sent again until it changes."""
    _ok()
    sent = _load('tools_sent.json', {})
    sent.update(refused=sig, refused_at=now.isoformat())
    _save('tools_sent.json', sent)
    line = f'AVISO (planou): lista de ferramentas recusada ({e.code}: {", ".join(e.fields) or "sem campo"})'
    _log('tools.log', line, now)
    return [line]


# ---------------------------------------------------------------- autolinks ("Links automáticos do agente", Planou with "links")

# The instance's link rules (the same ones its daily page uses: "ABC-123" to the tracker, "repo #12" to the pull request),
# so Planou links the ids the agent writes in its messages, and in the descriptions and comments of its tasks. The rule
# runs on Planou's server (.NET regex with a time limit); here it is checked with Python's re, which reads the simple
# patterns the same way.
MAX_LINKS = 20
MAX_LINK_GROUPS = 3
LINKS_EVERY = timedelta(days=1)            # resend the unchanged rules at least this often (a restored Planou gets them)
LINKS_REFUSED = 'invalid_links'
_PLACEHOLDER = re.compile(r'\{(\d+)\}')


def link_rule(pattern, url, label=None):
    """One rule as it leaves, or None when Planou would refuse it: a pattern of up to 200 characters with 1 to 3 groups,
    no named group, no backreference, that does not match the empty text; an http(s) url of up to 500 with {1} and only
    {1}..{groups}, outside the host; no credential anywhere."""
    if not isinstance(pattern, str) or not isinstance(url, str): return None
    pattern, url = pattern.strip(), url.strip()
    if not pattern or len(pattern) > 200 or not url or len(url) > 500: return None
    if re.search(r'(?<!\\)(?:\\\\)*\\(?:[1-9]|k)', pattern) or '(?P' in pattern or '(?<' in pattern.replace('(?<=', '').replace('(?<!', ''):
        return None
    try:
        rx = re.compile(pattern)
    except re.error:
        return None
    if not 1 <= rx.groups <= MAX_LINK_GROUPS or rx.search('') is not None: return None
    used = {int(n) for n in _PLACEHOLDER.findall(url)}
    if 1 not in used or any(n < 1 or n > rx.groups for n in used): return None
    import urllib.parse
    a, b = (urllib.parse.urlsplit(_PLACEHOLDER.sub(v, url)) for v in ('a1', 'zz9'))
    if a.scheme not in ('http', 'https') or (a.scheme, a.netloc.lower()) != (b.scheme, b.netloc.lower()) or not a.hostname \
            or '@' in a.netloc:
        return None
    label = clean_text(label, 60) if label else None
    if looks_secret(pattern) or looks_secret(url): return None
    out = {'pattern': pattern, 'url': url}
    if label: out['label'] = label
    return out


def set_links(rules, now=None):
    """The heavy tick's link rules: each one checked (link_rule), unique patterns, at most 20, saved for the next
    heartbeat (heartbeat() decides when they go). [] is a list too: it clears Planou's. Returns the rules as they leave."""
    if not _S['root']: return []
    now = now or datetime.now(timezone.utc)
    out, seen = [], set()
    for r in rules or []:
        c = link_rule((r or {}).get('pattern'), (r or {}).get('url'), (r or {}).get('label')) if isinstance(r, dict) else None
        if not c or c['pattern'] in seen: continue
        seen.add(c['pattern'])
        out.append(c)
        if len(out) >= MAX_LINKS: break
    _save('links.json', {'at': now.isoformat(), 'links': out})
    return out


def _links_due(now):
    """(sig, rules) when the rules must go with this heartbeat: they changed since the last ones Planou took, or a day
    went by. None when there are none saved or they are the ones Planou refused last time."""
    if not _S['root']: return None
    data = _load('links.json', None)
    if not isinstance(data, dict) or not isinstance(data.get('links'), list): return None
    sig = hashlib.sha256(json.dumps(data['links'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    sent = _load('links_sent.json', {})
    if sig == sent.get('refused'): return None
    last = _date_time(sent.get('at'))
    if sig == sent.get('sig') and last and now - last < LINKS_EVERY: return None
    return sig, data['links']


def _links_refused(e, sig, now):
    """422 invalid_links: the heartbeat counted, the rules did not (Planou keeps the previous ones). Logged with the field
    paths only, and the same rules are not sent again until they change."""
    _ok()
    sent = _load('links_sent.json', {})
    sent.update(refused=sig, refused_at=now.isoformat())
    _save('links_sent.json', sent)
    line = f'AVISO (planou): regras de link recusadas ({e.code}: {", ".join(e.fields) or "sem campo"})'
    _log('links.log', line, now)
    return [line]


# ---------------------------------------------------------------- role ("Papel do agente", Planou with "docs")

# The files the agent's session reads, in order, with size, hash and date, and the result of each behavior's reference
# cases ("evals", Planou 0.49 or later), for the Papel tab. The agent plugin builds the manifest on the heavy tick
# (agent.py docs_manifest): instructions.md and CONTEXT.md editable with their content, and the catalog of behaviors,
# because its runner applies agent_docs_changed (role_edit.py through set_docs_handler) and answers with docs_ack. A
# plugin without a handler sends no manifest, or a read-only one, so Planou offers no edit it would leave unapplied.
DOCS_EVERY = timedelta(days=1)             # resend the unchanged manifest at least this often (a restored Planou gets it)
DOCS_REFUSED = ('invalid_docs', 'secret_refused')
# Fields newer than some Planou, which refuses the whole manifest with a field it does not know, in levels: level 1 is
# the newest (PLN0257, Planou after 0.66.0: the autonomy of the Rules layer and the first lines of the handoff in the
# Memory layer); level 2 adds the name and sentence of each behavior (frontmatter of its BEHAVIOR.md: title and summary,
# and layer and kind in the catalog; Planou after 0.64.0). A refusal that names only fields of a level sends the
# manifest without them (and without the newer ones) for a day (DOCS_EVERY), then tries them again. "top" keys are keys
# of the manifest itself.
DOCS_NEW_LEVELS = (
    {'top': ('autonomy',), 'files': ('excerpt',)},
    {'catalog': ('title', 'summary', 'layer', 'kind'), 'files': ('title', 'summary')},
)
DOCS_NEW = DOCS_NEW_LEVELS[1]
_DOCS_NEW_RX = (re.compile(r'^docs\.(autonomy(\..*)?|files\[\d+\]\.excerpt)$'),
                re.compile(r'^docs\.(catalog\[\d+\]\.(title|summary|layer|kind)|files\[\d+\]\.(title|summary))$'))
DOCS_EVENT = 'agent_docs_changed'
_docs_handler = [None]


def set_docs_handler(fn):
    """fn(event) -> bool applies an agent_docs_changed and answers it with docs_ack(); False when the answer did not
    reach Planou (apply_events then stops before the event and a later tick hands it over again). None turns it off."""
    _docs_handler[0] = fn


def docs_ack(version_id, result, sha256=None, reason=None):
    """POST /v1/agent/docs/ack: `applied` with the sha256 of the version, or `refused` with the reason (up to 300).
    Returns (done, note): done True when Planou took it (note None) or refused it for good (4xx: note is the error code,
    logged in cache/planou/docs.log; answering again would not change it); done False on a network failure, a 5xx, a
    401 or a 429 (answer again later; Planou takes the same answer twice)."""
    if not active(): return False, None
    body = {'version_id': version_id, 'result': result}
    if sha256: body['sha256'] = sha256
    if result == 'refused': body['reason'] = _one(reason, 300) or 'recusado'
    try:
        _call('POST', '/agent/docs/ack', body)
    except PlanouError as e:
        if e.status and 400 <= e.status < 500 and e.status not in (401, 429):   # a 401 (key swapped): answer later
            _log('docs.log', f'AVISO (planou): resposta do papel {version_id} ({result}) recusada: {e.status} {e.code}',
                 datetime.now(timezone.utc))
            return True, e.code or str(e.status)
        return False, None
    return True, None


def set_docs(manifest, now=None):
    """Saves the manifest ({"files": [...]}) for the next heartbeat (heartbeat() decides when it goes). None or a manifest
    without files saves nothing (Planou keeps the one it has)."""
    if not _S['root'] or not isinstance(manifest, dict) or not manifest.get('files'): return None
    now = now or datetime.now(timezone.utc)
    _save('docs.json', {'at': now.isoformat(), 'docs': manifest})
    return manifest


def _docs_sig(manifest):
    """What makes the manifest 'changed': not when the reference cases ran (they run on every heavy tick)."""
    files = [dict(f, evals={k: v for k, v in f['evals'].items() if k != 'ran_at'}) if isinstance(f.get('evals'), dict) else f
             for f in manifest.get('files') or [] if isinstance(f, dict)]
    return hashlib.sha256(json.dumps(dict(manifest, files=files), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _docs_plain(manifest, level=2):
    """The manifest without the fields of DOCS_NEW_LEVELS up to `level` (for a Planou that does not know them yet)."""
    out = dict(manifest)
    for lv in DOCS_NEW_LEVELS[:level]:
        for k in lv.get('top', ()): out.pop(k, None)
        for part, keys in lv.items():
            if part != 'top' and isinstance(out.get(part), list):
                out[part] = [{k: v for k, v in x.items() if k not in keys} if isinstance(x, dict) else x for x in out[part]]
    return out


def _plain_level(sent, now):
    """How many levels of DOCS_NEW_LEVELS go out while `plain_until` holds (0: none). A `plain_until` of before the
    levels counts as 2 (it was about the title and summary)."""
    until = _date_time(sent.get('plain_until'))
    if not until or now >= until: return 0
    lv = sent.get('plain_level')
    return lv if lv in (1, 2) else 2


def _docs_due(now):
    """(sig, manifest) when the manifest must go with this heartbeat: it changed since the last one Planou took, or a day
    went by. None when there is none saved or it is the one Planou refused last time. While `plain_until` holds (a
    Planou that refused the title and summary of the behaviors) it goes without them."""
    if not _S['root']: return None
    data = _load('docs.json', None)
    if not isinstance(data, dict) or not isinstance(data.get('docs'), dict): return None
    sent = _load('docs_sent.json', {})
    level = _plain_level(sent, now)
    manifest = _docs_plain(data['docs'], level) if level else data['docs']
    data = {'docs': manifest}
    sig = _docs_sig(manifest)
    if sig == sent.get('refused'): return None
    last = _date_time(sent.get('at'))
    if sig == sent.get('sig') and last and now - last < DOCS_EVERY: return None
    return sig, data['docs']


def _docs_taken(sig, now):
    """Planou took the manifest: remember its sig and when, keeping a `plain_until` that still holds (else the next
    heartbeat would send the title and summary again to a Planou that refuses them)."""
    sent = {'sig': sig, 'at': now.isoformat()}
    old = _load('docs_sent.json', {})
    level = _plain_level(old, now)
    if level: sent.update(plain_until=old['plain_until'], plain_level=level)
    _save('docs_sent.json', sent)


def _docs_refused(e, sig, now):
    """422 invalid_docs / secret_refused on the manifest: the heartbeat counted, the manifest did not (Planou keeps the
    previous one). Logged with the field paths only, and the same manifest is not sent again until it changes. A refusal
    of only the title and summary of the behaviors (DOCS_NEW: a Planou that does not know them yet) is not a refusal of
    the manifest: the next heartbeat sends it without them, for a day."""
    _ok()
    sent = _load('docs_sent.json', {})
    fields = list(e.fields or [])
    level = _plain_level(sent, now)       # already without these: this is a refusal of the manifest
    new = next((i + 1 for i in range(len(_DOCS_NEW_RX)) if fields
                and all(any(rx.match(f) for rx in _DOCS_NEW_RX[:i + 1]) for f in fields)), 0)
    if new > level:
        sent.update(plain_until=(now + DOCS_EVERY).isoformat(), plain_level=new)
        _save('docs_sent.json', sent)
        what = 'a autonomia e o resumo de passagem' if new == 1 else 'o nome e a frase das habilidades'
        _log('docs.log', f'AVISO (planou): o Planou ainda não aceita {what} ({e.code}: '
                         f'{", ".join(fields)}); o papel vai sem eles por um dia', now)
        return []
    sent.update(refused=sig, refused_at=now.isoformat())
    _save('docs_sent.json', sent)
    line = f'AVISO (planou): arquivos do papel recusados ({e.code}: {", ".join(e.fields) or "sem campo"})'
    _log('docs.log', line, now)
    return [line]


# ---------------------------------------------------------------- attachments ("Anexos da tarefa", Planou 0.15)

ATTACHMENT_TYPES = {'.md': 'text/markdown', '.markdown': 'text/markdown', '.txt': 'text/plain', '.pdf': 'application/pdf',
                    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
                    '.webp': 'image/webp',
                    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
MAX_ATTACHMENT = 10 * 1024 * 1024
ATTACH_REFUSED = (403, 409, 413, 415, 422)       # the same file would be refused again: not retried until it changes


def _multipart(fields, filename, data, content_type):
    """multipart/form-data body with the text `fields` and one `file`. The name goes quoted (commas, parentheses and
    `&` are fine there) with an ASCII fallback, plus filename* in UTF-8 and the explicit `name` field."""
    import uuid
    b = uuid.uuid4().hex
    ascii_name = ''.join(c if 32 <= ord(c) < 127 and c not in '"\\' else '_' for c in filename)
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + str(v).encode() + b'\r\n')
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{ascii_name}"; '
                 f"filename*=UTF-8''{urllib.parse.quote(filename, safe='')}\r\n"
                 f'Content-Type: {content_type}\r\n\r\n'.encode() + data + b'\r\n')
    parts.append(f'--{b}--\r\n'.encode())
    return b''.join(parts), f'multipart/form-data; boundary={b}'


def _task_ref(ref):
    """(cache code, id for the URL) of a task: a code of the agent's own sync (its pid once created), a task of the queue
    (pid or task_id) or, passed through, anything shaped like a pid (CAR0012) or a task uuid (the server finds a task by
    pid, id or source_key). (code, None) when the task is not known yet: the sync has not created it."""
    r = str(ref or '').strip()
    known = _load('state.json', {}).get('tasks') or {}
    if r in known: return r, (known[r] or {}).get('pid')
    for c, v in known.items():
        if v.get('pid') and str(v['pid']).upper() == r.upper(): return c, v['pid']
    for v in _queue()['tasks'].values():
        if v.get('left'): continue
        if r.upper() == str(v.get('pid') or '').upper() or r == str(v.get('task_id') or ''):
            return str(v.get('pid') or v.get('task_id')), str(v.get('pid') or v.get('task_id'))
    if _PID.match(r) or _UUID.match(r): return r.upper() if _PID.match(r) else r, r
    return r, None


def _ignored(code, source_key, cache=None):
    """True when the person deleted this attachment of this task in Planou (a 202 `result: ignored` once): the key
    is never sent again. A cache written before PLN0175 has the same `result`, so it counts too."""
    cache = _load('attachments.json', {}) if cache is None else cache
    return (cache.get(f'{_task_ref(code)[0]}|{source_key}') or {}).get('result') == 'ignored'


def _send(code, source_key, path=None, data=None, name=None, now=None):
    """(outcome, lines) of one attachment: sent, unchanged, pending (no pid yet, 404 or network: a later tick sends it),
    inactive (Planou off or "minimum"), missing, refused, ignored (the person deleted it in Planou: never again)."""
    if not active(): return 'inactive', []
    if _S['confidentiality'] == 'minimum': return 'inactive', []
    now = now or datetime.now(timezone.utc)
    code, pid = _task_ref(code)
    cache = _load('attachments.json', {})
    k = f'{code}|{source_key}'
    if (cache.get(k) or {}).get('result') == 'ignored': return 'ignored', []    # before reading up to 10 MB
    if data is None:
        if not path or not os.path.isfile(path): return 'missing', []
        with open(path, 'rb') as f: data = f.read()
    name = name or os.path.basename(path or '') or f'{source_key}.md'
    sha = hashlib.sha256(data).hexdigest()
    if (cache.get(k) or {}).get('sha') == sha and cache[k].get('name') == name: return 'unchanged', []
    # the same content already went up under another key of this task (job-scout's `cv` and a `file:` of the same PDF)
    if any(kk.startswith(f'{code}|') and kk != k and v.get('sha') == sha and v.get('id') for kk, v in cache.items()):
        return 'unchanged', []
    if len(data) > MAX_ATTACHMENT:
        _cache_put(k, {'sha': sha, 'name': name, 'refused': 'too_large', 'at': now.isoformat()})
        return 'refused', [f'AVISO (planou): anexo {source_key} de {code} passa de 10 MB; não enviado']
    ctype = ATTACHMENT_TYPES.get(os.path.splitext(name)[1].lower(), 'application/octet-stream')
    if not pid: return 'pending', []      # the sync has not created the task (or refused it): a later tick sends it
    ref = urllib.parse.quote(pid, safe=':@-._~')
    body, mtype = _multipart({'source_key': source_key, 'name': name}, name, data, ctype)
    try:
        _, res = _call('POST', f'/agent/tasks/{ref}/attachments', raw=body, content_type=mtype, timeout=60)
    except PlanouError as e:
        if e.status == 404: return 'not_found', []          # not synced yet (the next tick tries again), or gone
        if e.status in ATTACH_REFUSED:
            _cache_put(k, {'sha': sha, 'name': name, 'refused': e.code, 'at': now.isoformat()})
            line = f'AVISO (planou): anexo {source_key} de {code} recusado ({e.status} {e.code})'
            _log('attachments.log', line, now)
            return 'refused', [line]
        return 'pending', [f'AVISO (planou): anexo {source_key} de {code}: {e.message if e.status else e}']
    res = res if isinstance(res, dict) else {}
    if res.get('result') == 'ignored':      # 202: the person deleted it; Planou ignores this key on this task from now on
        _cache_put(k, {'sha': sha, 'name': name, 'result': 'ignored', 'reason': res.get('reason'), 'at': now.isoformat()})
        _log('attachments.log', f'{code} {source_key} ignored ({res.get("reason") or "sem motivo"}) {name}', now)
        return 'ignored', []
    _cache_put(k, {'sha': sha, 'name': name, 'id': res.get('id'), 'result': res.get('result'), 'at': now.isoformat()})
    _log('attachments.log', f'{code} {source_key} {res.get("result") or "ok"} {name}', now)
    return ('unchanged' if res.get('result') == 'unchanged' else 'sent'), []


def _cache_put(k, entry):
    """One entry of cache/planou/attachments.json, under its lock (the runner's tick and the CLI may send at once)."""
    with _locked('attachments.json'):
        cache = _load('attachments.json', {})
        cache[k] = entry
        _save('attachments.json', cache)


def attach(code, source_key, path=None, data=None, name=None, now=None):
    """Sends a file to the agent's task `code` (the code of the synced item, its pid, or a task of the queue by pid)
    under the attachment's `source_key` (stable per kind: 'roteiro', 'cv', 'respostas'), only when the content or the
    name changed since the last send (sha256 in cache/planou/attachments.json). Missing file: nothing. 404 (the task
    does not exist yet) and network failures are retried on a later tick; a refusal (403, 409, 413, 415) is not retried
    until the file changes; an attachment the person deleted (202 ignored) is never sent again under that key, not even
    a new version (if the person restores it from the Lixeira, new versions stay local). Nothing goes under "minimum"
    confidentiality. Returns lines (AVISO on failure; an ignored attachment is not a failure: only attachments.log)."""
    return _send(code, source_key, path, data, name, now)[1]    # a 404 is retried on every call (job-scout's tick)


# Files the agent produces for a task, or that a description cites (PLN0052): they go as attachments of the task, and
# the text says "ver anexo" instead of a local path nobody else can open. Only what passes the local gate goes up.
_PID = re.compile(r'^[A-Za-z]{2,10}\d+$')
_UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
# a local path in text: in backticks, or bare, starting at / or ~/ (not inside a URL or a word) and running to the
# first extension Planou accepts, spaces included ("artifacts/2026-09-28 previsao-horas.md")
_CITED_TICKS = re.compile(r'`(~?/[^`\n]+)`')
_CITED_BARE = re.compile(r'(?<![\w:/.~`-])(~?/[^\n`\'"<>()\[\]{}|]*?\.(?:md|markdown|txt|pdf|png|jpe?g|gif|webp|docx))(?![\w-]|\.\w)',
                         re.I)
NOT_FOUND_WAIT = timedelta(days=1)  # a registered file whose task Planou does not find is tried again a day later
_DENY_DIRS = {'secrets', '.ssh', '.gnupg', '.aws', '.kube', '.docker', 'cache', '.cache', '.git'}
_DENY_NAME = re.compile(r'(?i)(^\.env|\.env$|token|cookie|credential|secret|passw|senha|\.key$|\.pem$|id_rsa)')
_TEXT_KINDS = ('.md', '.markdown', '.txt')
MAX_PER_TASK = 20                   # Planou keeps up to 20 attachments per task
SEEN_TEXT = 'ver anexo'


def attachable(path):
    """(real path, None) when the file may go up as an attachment, else (None, why): a regular file, a type Planou
    accepts, 1 byte to 10 MB, outside secrets/, .ssh, caches and git, no credential-looking name, and a text file without
    a credential in it."""
    try:
        p = os.path.realpath(os.path.expanduser(str(path or '').strip()))
    except (TypeError, ValueError):
        return None, 'caminho inválido'
    if not os.path.isfile(p): return None, 'não existe'
    ext = os.path.splitext(p)[1].lower()
    if ext not in ATTACHMENT_TYPES: return None, f'tipo {ext or "sem extensão"} não aceito pelo Planou'
    parts = [x.lower() for x in p.split(os.sep)]
    if any(x in _DENY_DIRS for x in parts[:-1]) or _DENY_NAME.search(parts[-1]):
        return None, 'caminho de credencial ou de cache: não vai como anexo'
    size = os.path.getsize(p)
    if size == 0: return None, 'arquivo vazio'
    if size > MAX_ATTACHMENT: return None, 'passa de 10 MB'
    if ext in _TEXT_KINDS:
        try:
            with open(p, 'rb') as f: text = f.read().decode('utf-8', 'replace')
        except OSError:
            return None, 'não deu para ler'
        if _SECRET_VALUE.search(text): return None, 'o texto parece ter uma credencial'
    return p, None


def file_key(path):
    """source_key of a file attached by path: `file:<name>` (the same file sent by the tick and by hand is one
    attachment; a new version replaces it)."""
    return ('file:' + os.path.basename(path))[:200]


def cited_files(text):
    """[(the text as cited, real path)] of the local files a text cites that pass attachable(), once each."""
    out, seen = [], set()
    for rx in (_CITED_TICKS, _CITED_BARE):
        for m in rx.finditer(str(text or '')):
            raw = m.group(1).strip()
            real, _why = attachable(raw)
            if real and real not in seen:
                seen.add(real)
                out.append((m.group(0) if rx is _CITED_TICKS else raw, real))
    return out


def without_paths(text, cited=None):
    """(text with each attachable local path replaced by "[ver anexo: <name>]", [real paths])."""
    cited = cited_files(text) if cited is None else cited
    t = str(text or '')
    for raw, real in sorted(cited, key=lambda c: -len(c[0])):
        t = t.replace(raw, f'[{SEEN_TEXT}: {os.path.basename(real)}]')
    return t, [real for _, real in cited]


def _registry():
    r = _load('attach_files.json', {})
    return r if isinstance(r, dict) else {}


def register_files(task, files, now=None):
    """Remembers files of a task (cache/planou/attach_files.json {task: {source_key: path}}): every tick sends them again
    when they change (sha256), or for the first time once the sync created the task."""
    if not files: return
    with _locked('attach_files.json'):
        reg = _registry()
        cur = reg.setdefault(str(task), {})
        changed = False
        for f in files:
            k = file_key(f)
            if cur.get(k) != f and (k in cur or len(cur) < MAX_PER_TASK):
                cur[k] = f; changed = True
        if changed: _save('attach_files.json', reg)


def attach_file(task, path, name=None, now=None):
    """Attaches a local file the agent produced for `task` (code, pid or queue task) and registers it, so a later tick
    retries it and sends each new version. Returns (outcome, lines): sent, unchanged, pending, inactive, blocked (the
    local gate said no: the reason is in the lines), missing."""
    real, why = attachable(path)
    if not real: return ('missing' if why == 'não existe' else 'blocked'), [f'anexo não enviado ({why}): {path}']
    if not active() or _S['confidentiality'] == 'minimum': return 'inactive', []
    register_files(task, [real], now)
    out = _send(task, file_key(real), path=real, name=name, now=now)
    if out[0] in ('not_found', 'ignored'): _forget(task, file_key(real))   # a mistyped task, or deleted by the person
    return out


def _forget(task, key):
    with _locked('attach_files.json'):
        reg = _registry()
        (reg.get(task) or {}).pop(key, None)
        if task in reg and not reg[task]: reg.pop(task)
        _save('attach_files.json', reg)


_FLUSHED = {'at': None}
ATTACH_SAID = {                     # what `attach` prints for each outcome
    'sent': 'anexo {name} enviado para {task}',
    'unchanged': 'anexo {name} igual ao que já está em {task}; nada enviado',
    'pending': 'anexo {name} guardado: vai para {task} no próximo tick (a tarefa ainda não existe no Planou, ou falhou a rede)',
    'not_found': 'a tarefa {task} não existe no Planou (ou não é deste agente); nada enviado',
    'inactive': 'nada enviado: Planou desligado para este agente (config, chave, modo teste) ou confidencialidade "minimum"',
    'blocked': 'nada enviado',
    'missing': 'nada enviado',
    'refused': 'o Planou recusou o anexo {name}; só vai de novo quando o arquivo mudar',
    'ignored': 'a pessoa apagou o anexo {name} de {task} no Planou; ele não vai de novo',
}


def flush_attachments(now=None):
    """Sends the registered files that changed or never went up (called by the sync and the tick heartbeat, at most
    once a minute per process). A file that is gone, no longer passes the gate, or that the person deleted in Planou
    (202 ignored) leaves the registry. Prints nothing:
    a failure goes to cache/planou/attachments.log (an AVISO line in the sync output would read as Planou down and hold
    the asks of the tick). Returns []."""
    if not active() or _S['confidentiality'] == 'minimum': return []
    now = now or datetime.now(timezone.utc)
    if _FLUSHED['at'] and timedelta(0) <= now - _FLUSHED['at'] < timedelta(minutes=1): return []
    _FLUSHED['at'] = now
    lines, gone = [], []
    missing = _load('attach_missing.json', {})
    cache = _load('attachments.json', {})
    waits = {}
    for task, files in list(_registry().items()):
        for k, f in list((files or {}).items()):
            real, _why = attachable(f) if not _ignored(task, k, cache) else (None, 'ignored')
            if not real:
                gone.append((task, k))      # gone, no longer passes the gate, or deleted by the person in Planou
                continue
            with open(real, 'rb') as fh: sha = hashlib.sha256(fh.read()).hexdigest()
            m = missing.get(f'{task}|{k}') or {}
            if m.get('sha') == sha and now - datetime.fromisoformat(m['at']) < NOT_FOUND_WAIT:
                waits[f'{task}|{k}'] = m
                continue                # Planou did not find the task a moment ago: no 10 MB upload on every tick
            outcome, out = _send(task, k, path=real, now=now)
            lines += out
            if outcome == 'ignored': gone.append((task, k))
            if outcome == 'not_found': waits[f'{task}|{k}'] = {'sha': sha, 'at': now.isoformat()}
    if waits != missing: _save('attach_missing.json', waits)
    if gone:
        with _locked('attach_files.json'):
            reg = _registry()
            for task, k in gone:
                (reg.get(task) or {}).pop(k, None)
                if task in reg and not reg[task]: reg.pop(task)
            _save('attach_files.json', reg)
    for ln in lines:
        if 'recusado' not in ln: _log('attachments.log', ln, now)      # a refusal was logged where it happened
    return []


# ---------------------------------------------------------------- current task (spec 8.3)

def current_task(pid=None, at=None, now=None):
    """Without pid: the task in progress at `at` (default now), or None. With pid: marks it ('-' closes the marker).
    The history (cache/planou/current_task.jsonl, pid@time) lets the cost hook attribute a turn to the task that was
    in progress when it started."""
    if not _S['root']: return None
    f = os.path.join(_cache_dir(), 'current_task.jsonl')
    if pid is not None:
        os.makedirs(_cache_dir(), exist_ok=True)
        with open(f, 'a') as out:
            out.write(json.dumps({'pid': None if pid == '-' else pid.strip().upper(),
                                  'at': (now or datetime.now(timezone.utc)).isoformat()}) + '\n')
        return None if pid == '-' else pid.strip().upper()
    at = at or datetime.now(timezone.utc)
    cur = None
    try:
        for ln in open(f):
            try: m = json.loads(ln)
            except ValueError: continue
            if datetime.fromisoformat(m['at']) <= at: cur = m.get('pid')
    except OSError:
        return None
    return cur


# ---------------------------------------------------------------- CLI

def _key_set():
    print('Cole a chave do Planou (pl_ag_...) e tecle Enter:', file=sys.stderr)
    key = sys.stdin.readline().strip()
    if not key.startswith('pl_ag_'):
        print('Isso não parece uma chave do Planou (começa com pl_ag_).', file=sys.stderr)
        return 2
    d = os.path.dirname(_key_file())
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(_key_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(f'PLANOU_AGENT_KEY={key}\n')
    os.chmod(_key_file(), stat.S_IRUSR | stat.S_IWUSR)
    print(f'Chave gravada em {_key_file()} (0600).')
    return 0


def agent_root(agent):
    """Folder of an agent (watch_core.config.agent_root): ~/.config/agent/<agent> when it exists, else the legacy folder,
    ~/.config/work-watch/<instance> for work-watch-<instance> and ~/.config/<agent> otherwise."""
    return config.agent_root(agent)


def _work_watch(agent):
    return str(agent or '').startswith('work-watch-')


def _strict_live(agent, root):
    """Agents that are live only with `"live": true`: work-watch instances and every agent of the agent plugin."""
    return _work_watch(agent) or config.is_agent_layout(root)


def agent_settings(root, agent=None):
    """The "planou" block of the agent's own config (config/config.json or config.json in its folder), or None.
    `"live": false`, `"test": true` or `"mode": "test"` in that config mean test mode: nothing is sent. A work-watch
    instance and an agent of the agent plugin are live only with `"live": true` (a new one starts in test mode)."""
    for rel in ('config/config.json', 'config.json'):
        try:
            with open(os.path.join(root, rel)) as f:
                c = json.load(f)
        except (OSError, ValueError):
            continue
        pc = c.get('planou') if isinstance(c, dict) else None
        if not isinstance(pc, dict): return None
        test = c.get('test') is True or c.get('mode') == 'test' or pc.get('live') is False
        if _strict_live(agent, root) and c.get('live') is not True: test = True
        return {**pc, 'live': not test}
    return None


def configure_agent(agent, root=None, plugin_version=None):
    """configure() from the agent's own config: what the runner, the CLI and the cost hook use. Without a "planou"
    block the module stays off. Draft text: "drafts_in_planou" in the block (default true, false for work-watch)."""
    root = os.path.expanduser(root) if root else agent_root(agent)
    pc = agent_settings(root, agent)
    default_conf = 'minimum' if _work_watch(agent) else 'title'
    configure(agent, root, project=(pc or {}).get('project'), confidentiality=(pc or {}).get('confidentiality') or default_conf,
              publish=bool(pc) and pc.get('publish', True) is not False, live=(pc or {}).get('live', True),
              plugin_version=plugin_version, drafts_text=(pc or {}).get('drafts_in_planou', not _work_watch(agent)) is True)
    return pc


def _text_arg(value):
    """'-' reads the text from stdin (no quoting trouble in the shell, and it never lands in the shell history)."""
    return sys.stdin.read().strip() if value == '-' else value


def queue_view():
    """fila ver: the local queue (whole payloads) merged with Planou's GET /agent/queue, which says what is released
    (within the limit "Ao mesmo tempo") and what waits. Each task gets `status`: "liberada" or "na fila". Planou
    unreachable, or older than the route: the local copy alone ("source": "cache")."""
    tasks = {str(v.get('task_id') or v.get('pid')): dict(v) for v in queue_tasks()}
    out = {'task_queue': queue_enabled(), 'source': 'cache'}
    if active():
        try:
            _, res = _call('GET', '/agent/queue')
            if not isinstance((res or {}).get('tasks'), list): raise PlanouError(0, 'no_queue', 'sem a lista da fila')
            remote = {}
            for r in res.get('tasks') or []:
                tid = str(r.get('task_id') or r.get('pid'))
                remote[tid] = {**tasks.get(tid, {}), **{k: v for k, v in r.items() if v is not None}}
            tasks = remote
            out.update(source='planou', wip=res.get('wip'), busy=res.get('busy'))
        except PlanouError:
            pass
    # an adjustment asked goes with its task (the brief of the rework), and a task that waits for one is listed even when
    # neither list has it
    for tid, chg in changes_open().items():
        row = tasks.get(tid) or next((v for v in tasks.values() if chg.get('pid') and v.get('pid') == chg.get('pid')), None)
        if row is None:
            row = tasks[tid] = {'task_id': tid, 'pid': chg.get('pid'), 'title': chg.get('title'), 'released': False,
                                'state': 'changes_requested', 'pr_url': chg.get('pr_url')}
        if chg.get('phase') == 'pending' and out['source'] == 'planou' and released(row) and row.get('state') in ('sent', 'started'):
            # Planou already handed it out again (the event is on its way): the rework may start
            with _locked('changes.json'):
                c = _changes()
                if (c['tasks'].get(tid) or {}).get('phase') == 'pending':
                    c['tasks'][tid]['phase'] = chg['phase'] = 'rework'
                    _save('changes.json', c)
        last = (chg.get('asks') or [{}])[-1]
        row['ajuste_pedido'] = {'fase': 'esperando a fila' if chg.get('phase') == 'pending' else 'retrabalho',
                                'textos': [a.get('text') for a in chg.get('asks') or []],
                                'pr_url': chg.get('pr_url') or row.get('pr_url'),
                                'pedido_em': last.get('at'),
                                'pedido_por': requested_by(last.get('by'), last.get('by_kind'), last.get('by_role'),
                                                           last.get('in'))}
    rows = list(tasks.values())
    for v in rows:
        v['status'] = 'liberada' if released(v) else 'na fila'
        own = self_handoff(v)
        if own:
            # handed to itself (PLN0281): the column's part goes to a new worker; the branch of the note stays here
            v['mesma_instancia'] = {'coluna': own.get('column'), 'veio_de': own.get('from'), 'papel': column_role(own.get('column')),
                                    'nota': own.get('note'), 'ajustes': [x.get('text') for x in own.get('asks') or []]}
    if out['source'] == 'cache': rows.sort(key=lambda v: (not released(v), int(v.get('seq') or 0)))
    out['tasks'] = rows
    pause = paused()
    if pause:
        # cost-cap pause: nothing new starts; the ones in progress finish (or block with a note)
        held = _pause()['held']
        out['pausado'] = {**{k: pause.get(k) for k in ('reason', 'label', 'since', 'until')},
                          'em_andamento': [r.get('pid') for r in pause.get('in_progress') or []],
                          'travadas_pela_pausa': [h.get('pid') for h in held]}
    return out


def declared():
    """What the next heartbeat declares: the capabilities (the server replaces its list with this one) and the optional
    lists it carries when due (tools, links), with how many items each one has saved locally (None = none saved)."""
    tools = _load('tools.json', None) if _S['root'] else None
    links = _load('links.json', None) if _S['root'] else None
    count = lambda d, k: len(d[k]) if isinstance(d, dict) and isinstance(d.get(k), list) else None
    return {'capabilities': capabilities(), 'tools': count(tools, 'tools'), 'links': count(links, 'links')}


def queue_limit():
    """The limit "Ao mesmo tempo" of the Fila tab as Planou has it now (GET /agent/queue): {wip, busy, source}. With the
    queue off: None. Planou unreachable or older than the route: wip and busy None, source "cache"."""
    if not queue_enabled(): return None
    out = {'wip': None, 'busy': None, 'source': 'cache'}
    if active():
        try:
            _, res = _call('GET', '/agent/queue')
            if isinstance(res, dict): out.update(wip=res.get('wip'), busy=res.get('busy'), source='planou')
        except PlanouError:
            pass
    return out


def _fila(a, ap):
    """fila ver: the queue tasks with the agent (JSON, whole payload, liberada or na fila). fila STATE [PID]: progress."""
    sub = (a.arg or ['ver'])[0]
    if sub == 'ver':
        print(json.dumps(queue_view(), ensure_ascii=False, indent=1))
        return 0
    if sub == 'refinar':
        if not a.note: ap.error('use: fila refinar [PID] --note "o que precisa acertar" (ou --note - para ler do stdin)')
        opts = [(o.split('=', 1)[0].strip(), o.split('=', 1)[1].strip(), False) for o in a.option if '=' in o]
        try:
            res = refine(_text_arg(a.note), a.arg[1] if len(a.arg) > 1 else None, opts)
        except PlanouError as e:
            print(f'AVISO (planou): {e.message}', file=sys.stderr)
            return 1
        print(json.dumps(res, ensure_ascii=False))
        print('ESPERE: a tarefa aguarda a resposta do usuario; ela volta pela fila (-- FILA) quando ele responder.')
        return 0
    if sub == 'worker-start':
        if len(a.arg) < 2 or not a.role or not a.label:
            ap.error("use: fila worker-start PID [--task PID2 ...] --role dev|integrator|other --label '<uma linha>'")
        return _worker_start_cli(a.arg[1:] + (a.task or []), a)
    if sub == 'worker':
        if len(a.arg) < 2 or None in (a.role, a.tokens, a.steps, a.duration_ms, a.result):
            ap.error('use: fila worker PID --role dev|integrator --tokens N --steps N --duration-ms N --result feito|parcial|falhou [--model M] [--key K] [--phases-from ARQ]')
        measured = used = None
        if a.phases_from:
            from . import phases as _phases
            measured, used, why = _phases.measure(a.phases_from)
            if why: print(f'AVISO (planou): tempo por fase e uso nao medidos ({why}); a entrega vai sem eles.', file=sys.stderr)
            else:
                if not measured: print('AVISO (planou): tempo por fase nao medido (registro do worker sem tempo medido); a entrega vai sem ele.', file=sys.stderr)
                if not used: print('AVISO (planou): uso do worker nao medido (registro sem respostas com uso); a entrega vai sem custo.', file=sys.stderr)
        try:
            res = worker_delivery(a.arg[1], a.role, a.tokens, a.steps, a.duration_ms, a.result, a.model, key=a.key, phases=measured,
                                  usage=used)
        except PlanouError as e:
            print(f'AVISO (planou): {e.message}', file=sys.stderr)
            return 1
        if res is None:
            print('AVISO (planou): este Planou ainda nao tem a rota de entregas (versao antiga); nada gravado, siga o trabalho.',
                  file=sys.stderr)
            return 0
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if sub == 'ajuste':
        if len(a.arg) < 2 or not a.text: ap.error('use: fila ajuste PID --text "o que ajustar" (ou --text -) [--pr-url URL] [--to author|previous]')
        if a.to is not None and a.to not in CHANGES_TO: ap.error(f'fila ajuste --to: {" ou ".join(CHANGES_TO)}')
        try:
            text = _text_arg(a.text)
            res = request_changes(text, a.arg[1], a.pr_url, to=a.to)
        except PlanouError as e:
            if e.code == 'not_in_queue':
                print(f'PARE: a tarefa nao esta mais com o agente ({e.message}). Nada foi devolvido.')
                return 3
            if e.code == 'not_released':
                print(f'ESPERE: a tarefa ainda nao foi liberada ({e.message}). Revise so quando vier -- FILA LIBERADA.')
                return 4
            if e.code == 'no_previous_owner':
                tid, entry = _queue_entry(a.arg[1])
                rec = self_handoff({'task_id': tid, 'pid': (entry or {}).get('pid') or a.arg[1],
                                    'project_state': (entry or {}).get('project_state')})
                if rec:
                    note_self_rework(tid, rec, text)
                    pid = (entry or {}).get('pid') or a.arg[1]
                    try:
                        post_comment(pid, self_rework_comment(rec, text))
                        kept = ' O pedido ficou registrado na tarefa (comentario).'
                    except PlanouError as ce:
                        kept = ''
                        print(f'AVISO (planou): o pedido de ajuste nao subiu como comentario da tarefa ({ce.message}); '
                              f'registre a mao: `comentario {pid} --text -`', file=sys.stderr)
                    print(self_rework_text(pid, rec, text) + kept)
                    return 0
                print(f'SEM DEV: a tarefa nao chegou pelo handoff de outro agente ({e.message}); nao ha a quem devolver. '
                      f'Peca a pessoa: printf \'%s\' "<ajuste>" | fila blocked {a.arg[1]} --note -')
                return 5
            print(f'AVISO (planou): {e.message}', file=sys.stderr)
            return 1
        print(json.dumps(res, ensure_ascii=False))
        who = (res.get('assignee') or {}).get('name') or 'o dev'
        ps = res.get('project_state')
        ps = ps.get('name') if isinstance(ps, dict) else ps
        print(f'DEVOLVIDA: {res.get("pid") or a.arg[1]} voltou para {ps or "a coluna do dev"} ({who}) como retrabalho, '
              f'na mesma PR{" (" + res["pr_url"] + ")" if res.get("pr_url") else ""}; nao esta mais com o agente.')
        return 0
    if sub not in PROGRESS_STATES + ('tempo',):
        ap.error('use: fila ver | fila started|in_review|done|blocked|handoff|refinar [PID] [--pr-url URL] [--note TEXTO] '
                 '[--estimate-h H] [--remaining-h H] | fila tempo [PID] --remaining-h H [--estimate-h H] | '
                 'fila worker PID --role dev|integrator --tokens N --steps N --duration-ms N --result feito|parcial|falhou [--key K] [--phases-from ARQ] | '
                 'fila worker-start PID [--task PID2 ...] --role dev|integrator|other --label TEXTO | '
                 'fila ajuste PID --text TEXTO [--pr-url URL] [--to author|previous]')
    ref = a.arg[1] if len(a.arg) > 1 else None
    if sub == 'started':
        wait = _pause_gate(ref)
        if wait:
            print(wait)
            return PAUSED_EXIT
    note = _text_arg(a.note) if a.note else None
    if sub == 'blocked' and not note and paused(): note = PAUSE_NOTE
    if sub == 'done':
        try: tid, entry = _queue_entry(ref)
        except PlanouError: tid, entry = None, None
        own = self_handoff({'task_id': tid, 'pid': (entry or {}).get('pid') or ref,
                            'project_state': (entry or {}).get('project_state')}) if tid else None
        role = column_role((own or {}).get('column'))
        if own and role in INDEPENDENT_ROLES and not review_worker_after((tid, (entry or {}).get('pid'), ref), own):
            what = INDEPENDENT_ROLES[role]
            print(f'AVISO (planou): {(entry or {}).get("pid") or ref} esta na {what} desta mesma instancia e nenhum worker '
                  f'NOVO de {what} foi aberto depois do handoff (`fila worker-start <PID> --role other`). A {what} e de um '
                  'worker novo, nunca da sessao nem do worker que entregou', file=sys.stderr)
    try:
        if sub == 'tempo':
            res = update_time(ref, a.remaining_h, a.estimate_h)
        else:
            res = progress(sub, ref, a.pr_url, note, estimate_h=a.estimate_h, remaining_h=a.remaining_h)
            if sub == 'blocked' and paused():
                tid, entry = _queue_entry(res.get('pid') or ref)
                pause_hold(res.get('task_id') or tid, res.get('pid') or (entry or {}).get('pid'), (entry or {}).get('title'))
    except PlanouError as e:
        if e.code == 'refining':
            print(f'ESPERE: {e.message}. Nao comece nem pare nada por ela; ela volta pela fila (-- FILA) com a resposta.')
            return 0
        if e.code == 'changes_pending':
            print(f'ESPERE: {e.message}. Nao pare o que ja existe (branch, PR); o texto do ajuste esta em `fila ver`.')
            return 0
        if e.code == 'not_in_queue':
            print(f'PARE: a tarefa nao esta mais com o agente ({e.message}). Pare o worker e nao abra nem atualize PR por ela.')
            return 3
        if e.code == 'not_released':
            print(f'ESPERE: a tarefa ainda nao foi liberada ({e.message}). Nao comece; ela chega como -- FILA LIBERADA.')
            return 4
        print(f'AVISO (planou): {e.message}', file=sys.stderr)
        return 1
    print(json.dumps(res, ensure_ascii=False))
    if res.get('state') == 'handoff':
        who = res.get('assignee') or {}
        who = who.get('name') or ('o usuario' if who.get('kind') == 'person' else 'sem responsavel')
        print(f'PASSOU: {res.get("pid") or ref or "a tarefa"} foi para {res.get("project_state") or "o proximo estado"} '
              f'({who}); nao esta mais com o agente. Diga ao usuario que passou adiante, nao que foi concluida.')
    if sub == 'started' and a.estimate_h is None and not (_queue_entry(ref)[1] or {}).get('estimate_h'):
        print('AVISO (planou): sem estimativa; mande --estimate-h H no started (ou `fila tempo <PID> --estimate-h H`)',
              file=sys.stderr)
    return 0


# ---------------------------------------------------------------- project ceremonies (the retro, Planou PLN0126)
# ceremony_started reaches every invited agent: it sends its contribution from its own data (rework, blocks, time in
# Precisa de você, cost, adjustments asked), each proposal citing real tasks. ceremony_facilitate reaches the
# facilitator with every contribution: it groups them and sends the minutes (at most 3 actions, each with cases). When
# the retro has voting (PLN0195), ceremony_vote reaches every invited agent between the two: it votes on the items
# (`retro votar`), and the facilitator gets the votes in each item. The payloads stay in cache/planou/ceremonies.json by meeting_id, so `retro ver` still has them after the heavy tick
# acknowledged the event. The guide is the agent plugin's behaviors/retro/BEHAVIOR.md.

def _ceremonies():
    c = _load('ceremonies.json', {})
    if not isinstance(c.get('meetings'), dict): c['meetings'] = {}
    return c


def remember_ceremony(ev, now=None):
    """Keeps the payload of a ceremony event by meeting_id (started, vote and facilitate apart), the newest CEREMONIES_KEPT."""
    p = ev.get('payload') or {}
    mid = str(p.get('meeting_id') or '')
    if not mid: return
    with _locked('ceremonies.json'):
        c = _ceremonies()
        m = c['meetings'].setdefault(mid, {})
        m[CEREMONY_KEYS.get(ev.get('type'), 'facilitate')] = p
        m['seen_at'] = m.get('seen_at') or (now or datetime.now(timezone.utc)).isoformat()
        for old in sorted(c['meetings'], key=lambda k: c['meetings'][k].get('seen_at') or '')[:-CEREMONIES_KEPT]:
            c['meetings'].pop(old, None)
        _save('ceremonies.json', c)


def ceremony(meeting_id):
    """What this agent knows of a meeting: {'started': payload, 'vote': payload, 'facilitate': payload} ({} when unknown)."""
    return _ceremonies()['meetings'].get(str(meeting_id)) or {}


def _who(v):
    return one_line((v or {}).get('display_name') or (v or {}).get('name'), 60) if isinstance(v, dict) else ''


def ceremony_line(kind, p):
    """The `-- CERIMONIA` line of a ceremony event (light loop and heavy tick)."""
    name = one_line(p.get('ceremony') or 'retro', 20)
    head = f'{name} #{p.get("number")} de {one_line((p.get("project") or {}).get("key"), 12)}'
    mid, due = one_line(p.get('meeting_id'), 40), _when({'at': p.get('due_at')})
    if kind == DAILY_EVENT:
        return daily_lines(head, mid, due, p)
    if kind == 'ceremony_refine':
        fac = _who(p.get('facilitator'))
        total = p.get('backlog_total')
        total = total if isinstance(total, int) else len(p.get('backlog') or [])
        return (f'{CEREMONY_PREFIX} {head}: sugestoes ate {due} ({total} tarefa{"" if total == 1 else "s"} no backlog'
                + (f', facilita {fac}' if fac else '') + f') -> `refino ver {mid}` para o backlog, estimativa, quebra ou '
                f'selo agent_can_do so onde houver motivo real, e `refino sugerir {mid} --text -` ([] = nada a sugerir); '
                f'guia: {REFINE_GUIDE}')
    if kind == 'ceremony_refine_facilitate':
        n = len(p.get('contributions') or [])
        missing = ', '.join(_who(x) for x in p.get('missing') or [] if _who(x))
        return (f'{CEREMONY_PREFIX} LISTA {head} (voce facilita; {n} contribuic{"ao" if n == 1 else "oes"}'
                + (f', faltou: {missing}' if missing else '') + f'): lista ate {due} -> `refino ver {mid}` para as '
                f'sugestoes de todos, uma estimativa por tarefa, e `refino lista {mid} --text -`; o usuario aprova cada '
                f'uma na tela; guia: {REFINE_GUIDE}')
    if kind == 'ceremony_started':
        fac = _who(p.get('facilitator'))
        return (f'{CEREMONY_PREFIX} {head}: contribuicao ate {due}' + (f' (facilita {fac})' if fac else '') +
                f' -> `retro dados {mid}`, escrever a contribuicao a partir dos proprios dados, toda proposta citando '
                f'PIDs reais, e `retro contribuir {mid} --text -`; guia: {CEREMONY_GUIDE}')
    if kind == 'ceremony_vote':
        n = len(p.get('items') or [])
        return (f'{CEREMONY_PREFIX} VOTO {head}: votos ate {due} (ate {p.get("votes_per_voter") or "?"} de {n} '
                f'ite{"m" if n == 1 else "ns"}) -> `retro ver {mid}` para os itens e `retro votar {mid} <item_id> ...` '
                f'(sem ids = voto em branco), nos itens de mais efeito real nos casos; guia: {CEREMONY_GUIDE}')
    n = len(p.get('contributions') or [])
    missing = ', '.join(_who(x) for x in p.get('missing') or [] if _who(x))
    voted = (p.get('rules') or {}).get('votes_per_voter') is not None or bool(p.get('vote_closed_by'))
    return (f'{CEREMONY_PREFIX} ATA {head} (voce facilita; {n} contribuic{"ao" if n == 1 else "oes"}'
            + (f', faltou: {missing}' if missing else '') + f'): ata ate {due} -> `retro ver {mid}` para as '
            f'contribuicoes' + (' e os votos (mais votados primeiro)' if voted else '') + f', agrupar, no maximo '
            f'{(p.get("rules") or {}).get("max_actions") or 3} acoes com casos, marcar repeticao, e '
            f'`retro ata {mid} --text -`; guia: {CEREMONY_GUIDE}')


def _blocked_hours(h):
    try: h = float(h)
    except (TypeError, ValueError): return '?'
    return f'{h:.0f} h' if h >= 10 else f'{h:.1f} h'.replace('.0 h', ' h')


def daily_lines(head, mid, due, p):
    """The `-- CERIMONIA daily` block: the header with the command, then one indented line per blocked task of this agent
    (pid, title, hours blocked, the blocking note, the open ask, the pids it waits on), so the session writes the
    explanations from the block alone. Under "minimum" the titles and notes stay out (the session sees them with `daily
    ver`) and the text asked for is neutral."""
    blocked = [b for b in p.get('blocked') or [] if isinstance(b, dict) and b.get('pid')]
    n, mx = len(blocked), p.get('text_max') or DAILY_TEXT_MAX
    minimum = _S['confidentiality'] == 'minimum'
    out = [f'{CEREMONY_PREFIX} {head}: explicar {n} tarefa{"" if n == 1 else "s"} Impedida{"" if n == 1 else "s"} ate {due} '
           f'-> uma linha por tarefa (ate {mx} caracteres) a partir da nota do bloqueio e do pedido aberto, sem worker, '
           f'e `daily explicar {mid} --text -` com {{"explanations": [{{"pid", "text"}}], "cost_usd", "tokens"}}'
           + ('; confidencialidade minimum: texto neutro, sem nome, assunto ou dado de cliente' if minimum else '')
           + f'; guia: {DAILY_GUIDE}']
    for b in blocked[:DAILY_LINES]:
        bits = [_blocked_hours(b.get('hours'))]
        if not minimum:
            if b.get('reason'): bits.append(f'nota: {one_line(b.get("reason"), 160)}')
        if b.get('ask_code'): bits.append(f'pedido {one_line(b.get("ask_code"), 20)}')
        waits = [one_line(x, 20) for x in b.get('blocked_by') or [] if x]
        if waits: bits.append('bloqueada por ' + ', '.join(waits))
        title = '' if minimum else f' {one_line(b.get("title"), 80)}'
        out.append(f'    {one_line(b.get("pid"), 20)}{title} ({"; ".join(bits)})')
    if n > DAILY_LINES: out.append(f'    ... mais {n - DAILY_LINES}: `daily ver {mid}`')
    return '\n'.join(out)


def _in_period(at, start, end):
    d = _date_time(at)
    return d is not None and (start is None or d >= start) and (end is None or d <= end)


def ceremony_facts(meeting_id):
    """The agent's own data for a meeting, from the caches it already keeps (nothing new is measured): adjustments asked
    in the period (changes.json), tasks blocked or waiting in its queue (queue.json), asks still open with the person
    (open_asks.json) and the earlier actions with their result. Each item carries the PID to cite."""
    m = ceremony(meeting_id)
    base = m.get('started') or m.get('facilitate') or {}
    start, end = _date_time(base.get('period_from')), _date_time(base.get('period_to'))
    adjustments = []
    for t in _changes()['tasks'].values():
        asks = [a for a in t.get('asks') or [] if _in_period(a.get('at'), start, end)]
        if asks and t.get('pid'):
            adjustments.append({'pid': t['pid'], 'title': one_line(t.get('title'), 120), 'adjustments': len(asks),
                                'by': sorted({a.get('by_kind') or 'person' for a in asks})})
    held = [{'pid': v.get('pid'), 'title': one_line(v.get('title'), 120), 'state': v.get('state'),
             'note': one_line(v.get('note'), 200) or None, 'since': v.get('received_at')}
            for v in _queue()['tasks'].values()
            if not v.get('left') and v.get('pid') and v.get('state') in ('blocked', 'waiting', 'changes_requested')]
    now = datetime.now(timezone.utc)
    waiting = []
    for ref, v in (_load('open_asks.json', {}) or {}).items():
        if not isinstance(v, dict) or v.get('answered'): continue
        since = _date_time(v.get('at') or v.get('created_at') or v.get('opened_at'))
        waiting.append({'code': v.get('code'), 'task': v.get('task') or v.get('pid'), 'ref': ref,
                        'hours_open': round((now - since).total_seconds() / 3600, 1) if since else None})
    return {'meeting_id': str(meeting_id), 'known': bool(m), 'period_from': base.get('period_from'),
            'period_to': base.get('period_to'), 'rules': base.get('rules'),
            'previous_actions': base.get('previous_actions') or [], 'adjustments_asked': adjustments,
            'held_in_queue': held, 'open_asks': waiting}


def retro_facts(meeting_id):
    """`retro dados`: the facts Planou measured in the meeting's period (GET /v1/agent/ceremonies/{id}/facts, PLN0207:
    rework, blocked and waiting time, hours in Precisa de voce and cost, per agent, each with the PID) in `planou`, beside
    the agent's own caches (ceremony_facts). An older Planou without the route, or Planou out of reach, leaves `planou`
    null with the reason in `planou_aviso`: the local data still comes."""
    out = ceremony_facts(meeting_id)
    try:
        _, res = _call('GET', f'/agent/ceremonies/{urllib.parse.quote(str(meeting_id))}/facts')
        out['planou'], out['source'] = res, 'planou+cache'
        if not out.get('period_from') and isinstance(res, dict):
            out['period_from'], out['period_to'] = res.get('period_from'), res.get('period_to')
    except PlanouError as e:
        out['planou'], out['source'] = None, 'cache'
        out['planou_aviso'] = f'sem os fatos do Planou ({e.status} {e.code}: {e.message}); so os dados locais'
    return out


CEREMONY_REFUSALS = {
    'cases_required': 'RECUSADA: toda proposta (e toda acao da ata) cita pelo menos um caso real: {"pid": "ABC0001", "note": "o que aconteceu"}.',
    'unknown_case': 'RECUSADA: {m} Cite so tarefas que existem no Planou.',
    'secret_refused': 'RECUSADA: o texto parece ter uma credencial; tire o valor e mande de novo.',
    'round_closed': 'FECHADA: a rodada de contribuicoes ja fechou; nada a mandar (o facilitador ja esta com a reuniao).',
    'not_invited': 'NAO CONVIDADO: {m} Nada a fazer.',
    'not_facilitator': 'NAO FACILITA: {m} Quem facilita manda a ata; a sua parte e a contribuicao.',
    'vote_not_open': 'AINDA NAO: a votacao abre quando a rodada de contribuicoes fechar; espere o -- CERIMONIA VOTO.',
    'vote_closed': 'FECHADA: a votacao ja fechou; nada a mandar (o facilitador ja esta com a reuniao).',
    'no_vote': 'SEM VOTACAO: esta reuniao nao tem rodada de votos; nada a mandar.',
    'unknown_item': 'RECUSADA: {m} Vote so nos ids de item que vieram no -- CERIMONIA VOTO (`retro ver`).',
    'unknown_task': 'RECUSADA: {m}',
}


def _json_arg(text, ap, use):
    try:
        body = json.loads(_text_arg(text) or '')
    except ValueError:
        ap.error(f'use: {use} (JSON invalido)')
    if not isinstance(body, dict): ap.error(f'use: {use} (um objeto JSON)')
    return body


def _retro(a, ap):
    """retro dados|ver|contribuir|votar|ata <meeting_id>: the agent's part in a project ceremony (Planou PLN0126, the vote
    PLN0195)."""
    sub, mid = (a.arg + ['', ''])[:2]
    if sub not in ('dados', 'ver', 'contribuir', 'votar', 'ata') or not mid:
        ap.error('use: retro dados|ver <meeting_id>; retro contribuir|ata <meeting_id> --text <json> (ou --text - do stdin); '
                 'retro votar <meeting_id> [item_id ...] [--cost-usd N] (sem ids = voto em branco)')
    if sub == 'dados':
        print(json.dumps(retro_facts(mid), ensure_ascii=False, indent=1))
        return 0
    try:
        if sub == 'ver':
            _, res = _call('GET', f'/agent/ceremonies/{urllib.parse.quote(mid)}')
            print(json.dumps({**res, 'cached': ceremony(mid)}, ensure_ascii=False, indent=1))
            return 0
        if sub == 'votar':
            ids = a.arg[2:]
            body = {'item_ids': ids, **({'cost_usd': a.cost_usd} if a.cost_usd is not None else {})}
            _, res = _call('POST', f'/agent/ceremonies/{urllib.parse.quote(mid)}/votes', body)
            print(json.dumps(res, ensure_ascii=False))
            print(f'VOTO ENVIADO: {", ".join(ids) if ids else "em branco"} '
                  f'({"atualizado" if res.get("result") == "updated" else "registrado"}; reuniao {res.get("status")}).')
            return 0
        if sub == 'contribuir':
            body = _json_arg(a.text, ap, 'retro contribuir <meeting_id> --text \'{"items": [...], "cost_usd": 0.1}\'')
            _, res = _call('POST', f'/agent/ceremonies/{urllib.parse.quote(mid)}/contributions',
                           {k: body[k] for k in ('items', 'cost_usd', 'tokens') if k in body})
            print(json.dumps(res, ensure_ascii=False))
            print(f'ENVIADA: contribuicao {"atualizada" if res.get("result") == "updated" else "registrada"} '
                  f'(reuniao {res.get("status")}).')
            return 0
        body = _json_arg(a.text, ap, 'retro ata <meeting_id> --text \'{"summary": "...", "actions": [...]}\'')
        _, res = _call('POST', f'/agent/ceremonies/{urllib.parse.quote(mid)}/minutes',
                       {k: body[k] for k in ('summary', 'decisions', 'actions', 'lessons', 'cost_usd') if k in body})
        print(json.dumps(res, ensure_ascii=False))
        acts = ', '.join(f'{x.get("pid")}' + (' (repete acao anterior)' if x.get('repeat_of') else '') for x in res.get('actions') or [])
        print(f'ATA ENVIADA: {acts or "sem acoes"}; as acoes estao no backlog do projeto, esperando a aprovacao do usuario.'
              if res.get('result') != 'unchanged' else 'ATA JA ESTAVA LA: nada mudou.')
        return 0
    except PlanouError as e:
        msg = CEREMONY_REFUSALS.get(e.code)
        if msg:
            print(msg.replace('{m}', e.message) + (f' Campos: {", ".join(e.fields)}.' if e.fields else ''))
            return 3
        print(f'AVISO (planou): {e.message}' + (f' Campos: {", ".join(e.fields)}.' if e.fields else ''), file=sys.stderr)
        return 1


def _refino(a, ap):
    """refino ver|sugerir|lista <meeting_id>: the agent's part in the backlog refinement (Planou PLN0210). `ver` shows the
    meeting and the cached event (the backlog, and for the facilitator every suggestion); `sugerir` sends this agent's
    suggestions (a resend replaces them); `lista` sends the facilitator's merged list, which the person approves."""
    sub, mid = (a.arg + ['', ''])[:2]
    if sub not in ('ver', 'sugerir', 'lista') or not mid:
        ap.error('use: refino ver <meeting_id>; refino sugerir|lista <meeting_id> --text <json> (ou --text - do stdin)')
    try:
        if sub == 'ver':
            _, res = _call('GET', f'/agent/ceremonies/{urllib.parse.quote(mid)}')
            print(json.dumps({**res, 'cached': ceremony(mid)}, ensure_ascii=False, indent=1))
            return 0
        if sub == 'sugerir':
            body = _json_arg(a.text, ap, 'refino sugerir <meeting_id> --text \'{"suggestions": [...], "cost_usd": 0.1}\'')
            if not isinstance(body.get('suggestions'), list): ap.error('use: refino sugerir: "suggestions" e uma lista ([] = nada a sugerir)')
            _, res = _call('POST', f'/agent/ceremonies/{urllib.parse.quote(mid)}/contributions',
                           {k: body[k] for k in ('suggestions', 'cost_usd', 'tokens') if k in body})
            print(json.dumps(res, ensure_ascii=False))
            print(f'ENVIADAS: {len(body["suggestions"])} sugest{"ao" if len(body["suggestions"]) == 1 else "oes"} '
                  f'({"atualizadas" if res.get("result") == "updated" else "registradas"}; reuniao {res.get("status")}).')
            return 0
        body = _json_arg(a.text, ap, 'refino lista <meeting_id> --text \'{"summary": "...", "suggestions": [...]}\'')
        _, res = _call('POST', f'/agent/ceremonies/{urllib.parse.quote(mid)}/minutes',
                       {k: body[k] for k in ('summary', 'decisions', 'suggestions', 'cost_usd') if k in body})
        print(json.dumps(res, ensure_ascii=False))
        print(f'LISTA ENVIADA: {res.get("suggestions") or 0} sugestoes esperando a aprovacao do usuario em Cerimonias.'
              if res.get('result') != 'unchanged' else 'LISTA JA ESTAVA LA: nada mudou.')
        return 0
    except PlanouError as e:
        # a field the Planou refused (estimate out of range, split with one part) is the agent's to fix, like a case
        msg = CEREMONY_REFUSALS.get(e.code) or ('RECUSADA: {m}' if e.code == 'validation' else None)
        if msg:
            print(msg.replace('{m}', e.message) + (f' Campos: {", ".join(e.fields)}.' if e.fields else ''))
            return 3
        print(f'AVISO (planou): {e.message}' + (f' Campos: {", ".join(e.fields)}.' if e.fields else ''), file=sys.stderr)
        return 1


DAILY_REFUSALS = {
    'unknown_case': 'RECUSADA: {m} Explique so os PIDs da linha -- CERIMONIA daily (`daily ver`).',
    'validation': 'RECUSADA: {m}',
}


def daily_explanations(meeting_id, body, cost_usd=None, tokens=None):
    """The body of `daily explicar`, checked here before anything goes up: `explanations` is a list of {pid, text}, one
    per pid, each text one line (line breaks become spaces) of 1 to text_max characters; with the event cached, only the
    pids it brought. --cost-usd and --tokens fill what the JSON left out. Returns (body, warnings); raises PlanouError
    (code 'validation') on what Planou would refuse anyway."""
    ev = ceremony(meeting_id).get('started') or {}
    mx = int(ev.get('text_max') or DAILY_TEXT_MAX)
    known = [str(b.get('pid')) for b in ev.get('blocked') or [] if isinstance(b, dict) and b.get('pid')]
    rows = body.get('explanations')
    if not isinstance(rows, list) or not rows:
        raise PlanouError(0, 'validation', '"explanations" e uma lista com um {"pid", "text"} por tarefa Impedida')
    out, seen = [], set()
    for i, r in enumerate(rows):
        pid = str((r or {}).get('pid') or '').strip() if isinstance(r, dict) else ''
        text = ' '.join(str((r or {}).get('text') or '').split()) if isinstance(r, dict) else ''
        if not pid: raise PlanouError(0, 'validation', f'explanations[{i}]: falta o "pid"')
        if pid in seen: raise PlanouError(0, 'validation', f'{pid} aparece duas vezes: uma linha por tarefa')
        if known and pid not in known:
            raise PlanouError(0, 'validation', f'{pid} nao veio neste evento (so {", ".join(known)})')
        if not 1 <= len(text) <= mx:
            raise PlanouError(0, 'validation', f'{pid}: a explicacao tem de 1 a {mx} caracteres ({len(text)})')
        seen.add(pid); out.append({'pid': pid, 'text': text})
    res = {'explanations': out}
    cost = body.get('cost_usd', cost_usd)
    if cost is not None:
        try: cost = float(cost)
        except (TypeError, ValueError): raise PlanouError(0, 'validation', '"cost_usd" e um numero em USD') from None
        if cost < 0: raise PlanouError(0, 'validation', '"cost_usd" nao pode ser negativo')
        res['cost_usd'] = cost
    tok = body.get('tokens', tokens)
    if tok is not None:
        try: res['tokens'] = _count('tokens', tok, TOKENS_MAX)
        except PlanouError as e: raise PlanouError(0, 'validation', e.message) from None
    missing = [x for x in known if x not in seen]
    warnings = [f'sem explicacao para {", ".join(missing)}: fica "sem explicacao do agente" na ata'] if missing else []
    return res, warnings


def _daily(a, ap):
    """daily ver|explicar <meeting_id>: the agent's part in the project daily (Planou PLN0127). `ver` shows the meeting
    and the cached event (the blocked tasks); `explicar` sends one line per blocked task in ONE call, with the cost and
    the tokens of the turn (a resend replaces; the last agent called closes the daily, so never one pid per call)."""
    sub, mid = (a.arg + ['', ''])[:2]
    if sub not in ('ver', 'explicar') or not mid:
        ap.error('use: daily ver <meeting_id>; daily explicar <meeting_id> --text <json> (ou --text - do stdin) '
                 '[--cost-usd N] [--tokens N]')
    try:
        if sub == 'ver':
            _, res = _call('GET', f'/agent/ceremonies/{urllib.parse.quote(mid)}')
            print(json.dumps({**res, 'cached': ceremony(mid)}, ensure_ascii=False, indent=1))
            return 0
        use = ('daily explicar <meeting_id> --text \'{"explanations": [{"pid": "ABC0001", "text": "..."}], '
               '"cost_usd": 0.01, "tokens": 1800}\'')
        try:
            body, warnings = daily_explanations(mid, _json_arg(a.text, ap, use), a.cost_usd, a.tokens)
        except PlanouError as e:
            ap.error(f'{e.message} (use: {use})')
        for w in warnings: print(f'AVISO (planou): {w}', file=sys.stderr)
        _, res = _call('POST', f'/agent/ceremonies/{urllib.parse.quote(mid)}/contributions', body)
        print(json.dumps(res, ensure_ascii=False))
        n = len(body['explanations'])
        print(f'ENVIADAS: {n} explicac{"ao" if n == 1 else "oes"} '
              f'({"atualizadas" if res.get("result") == "updated" else "registradas"}; reuniao {res.get("status")}).')
        return 0
    except PlanouError as e:
        msg = DAILY_REFUSALS.get(e.code) or CEREMONY_REFUSALS.get(e.code)
        if msg:
            print(msg.replace('{m}', e.message) + (f' Campos: {", ".join(e.fields)}.' if e.fields else ''))
            return 3
        print(f'AVISO (planou): {e.message}' + (f' Campos: {", ".join(e.fields)}.' if e.fields else ''), file=sys.stderr)
        return 1


def _worker_start_cli(tasks, a):
    try:
        key, warning = worker_start(tasks, a.role, a.label)
    except PlanouError as e:
        print(f'AVISO (planou): {e.message}', file=sys.stderr)
        return 1
    if warning: print(f'AVISO (planou): {warning}; siga o trabalho', file=sys.stderr)
    print(key)
    return 0


# ---------------------------------------------------------------- comments with @ (task_comment_mentioned, PLN0240)

def _comment_task(ref):
    """The id for /agent/tasks/{id}/comments: a pid (Planou finds a task by pid, id or source_key), a task uuid, or the
    agent's own sync code turned into its pid."""
    code, tid = _task_ref(ref)
    if not tid: raise PlanouError(0, 'task', f'tarefa {one_line(ref, 40) or "?"} desconhecida: use o PID (ex.: CAR0007)')
    return tid


def _comment_error(e, pid):
    if e.code == 'not_your_task':
        return PlanouError(e.status, e.code, f'{pid} nao e tarefa deste agente e ninguem o chamou nos comentarios dela')
    if e.status == 404 and e.code in ('not_found', 'http_404'):
        return PlanouError(e.status, e.code, f'{pid}: tarefa nao encontrada ({e.message})')
    return e


def task_comments(ref):
    """`comentario PID --ver`: the task's comments, oldest first (GET /agent/tasks/{id}/comments). Raises PlanouError
    (403 not_your_task: not the agent's task and nobody called it there)."""
    if not active(): raise PlanouError(0, 'inactive', 'Planou desligado para este agente (config, chave ou modo teste)')
    tid = _comment_task(ref)
    try:
        _, res = _call('GET', f'/agent/tasks/{urllib.parse.quote(tid, safe="")}/comments')
    except PlanouError as e:
        raise _comment_error(e, tid) from None
    return res or {}


def post_comment(ref, text, reply_to=None):
    """`comentario PID --text T [--reply-to ID]`: the agent's answer in the task's comments, as its author (POST
    /agent/tasks/{id}/comments {"text", "reply_to"?}). A comment is seen by people in Planou: the session writes it under
    the usual rules (no client data where it does not belong, no attribution). Returns the comment (201). Raises
    PlanouError: text empty or over COMMENT_TEXT_MAX, reply_to not a comment id (checked here first); 403 not_your_task;
    422 reply_to of another task."""
    text = str(text or '').strip()
    if not text: raise PlanouError(0, 'text', 'diga a resposta (--text, ou --text - para ler do stdin)')
    if len(text) > COMMENT_TEXT_MAX: raise PlanouError(0, 'text', f'comentario acima de {COMMENT_TEXT_MAX} caracteres: resuma')
    if reply_to and not _UUID.match(str(reply_to).strip()):
        raise PlanouError(0, 'reply_to', '--reply-to: o id do comentario (o comment_id da linha -- COMENTARIO)')
    if not active(): raise PlanouError(0, 'inactive', 'Planou desligado para este agente (config, chave ou modo teste)')
    tid = _comment_task(ref)
    body = {'text': text, **({'reply_to': str(reply_to).strip()} if reply_to else {})}
    try:
        _, res = _call('POST', f'/agent/tasks/{urllib.parse.quote(tid, safe="")}/comments', body)
    except PlanouError as e:
        raise _comment_error(e, tid) from None
    return res or {}


def _stamp(raw):
    """YYYY-MM-DD HH:MM (local) of a timestamp of Planou, or '?'."""
    raw = re.sub(r'(\.\d{6})\d+', r'\1', str(raw or '').replace('Z', '+00:00'))
    try:
        return datetime.fromisoformat(raw).astimezone().strftime('%Y-%m-%d %H:%M')
    except ValueError:
        return '?'


def _comentario(a, ap):
    """comentario PID --text TEXT|- [--reply-to ID] | comentario PID --ver."""
    if len(a.arg) != 1 or (not a.ver and a.text is None):
        ap.error('use: comentario PID --text - [--reply-to COMMENT_ID] | comentario PID --ver')
    try:
        if a.ver:
            res = task_comments(a.arg[0])
            rows = res.get('comments') or []
            print(f'{one_line(res.get("pid"), 20) or a.arg[0]}: {len(rows)} comentario(s)')
            for c in rows:
                who = _author(c.get('author')) + (' (agente)' if (c.get('author') or {}).get('kind') == 'agent' else '')
                print(f'- {c.get("id")} {who} ({_stamp(c.get("created_at"))}'
                      + (', editado' if c.get('edited_at') else '')
                      + (f', resposta a {c["reply_to"]}' if c.get('reply_to') else '') + f'): {message_text(c.get("text"))}')
            return 0
        res = post_comment(a.arg[0], _text_arg(a.text), a.reply_to)
    except PlanouError as e:
        print(f'AVISO (planou): {e.message}', file=sys.stderr)
        return 1
    print(f'comentario enviado em {one_line(res.get("pid"), 20) or a.arg[0]} ({res.get("id")})'
          + (f', resposta a {res["reply_to"]}' if res.get('reply_to') else ''))
    return 0


def _worker(a, ap):
    """worker start [PID ...] | worker ping KEY | worker end KEY | worker ver."""
    sub = (a.arg or ['ver'])[0]
    if sub == 'ver':
        print(json.dumps(workers_view(), ensure_ascii=False, indent=1))
        return 0
    if sub == 'start':
        if not a.role or not a.label: ap.error("use: worker start [PID ...] [--task PID ...] --role dev|integrator|other --label '<uma linha>'")
        return _worker_start_cli(a.arg[1:] + (a.task or []), a)
    if sub not in ('end', 'ping') or len(a.arg) != 2:
        ap.error("use: worker start [PID ...] --role R --label TEXTO | worker end KEY --result feito|parcial|falhou | "
                 "worker ping KEY [--label TEXTO] [--task PID ...] | worker ver")
    try:
        if sub == 'end':
            if not a.result: ap.error('use: worker end KEY --result feito|parcial|falhou')
            res = worker_end(a.arg[1], a.result)
        else:
            res = worker_ping(a.arg[1], a.label, a.task)
    except PlanouError as e:
        print(f'AVISO (planou): {e.message}', file=sys.stderr)
        return 1
    print(json.dumps(res, ensure_ascii=False) if res is not None else 'ok (so aqui: Planou desligado ou sem o worker)')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python3 -m watch_core.planou')
    ap.add_argument('--agent', required=True)
    ap.add_argument('--root', help='agent folder (default: ~/.config/agent/<agent> when it exists, else ~/.config/<agent> or '
                                   '~/.config/work-watch/<instance>)')
    ap.add_argument('cmd', choices=['key', 'status', 'active', 'heartbeat', 'poll', 'cursor-commit', 'ack-delivered', 'ack',
                                    'events', 'draft', 'approval', 'decision', 'pergunta', 'alert', 'cancel', 'result', 'tarefa',
                                    'fila', 'tools', 'anexo', 'attach', 'pronta', 'autonomia', 'sugestao',
                                    'retro', 'refino', 'daily', 'worker', 'comentario'])
    ap.add_argument('arg', nargs='*')
    ap.add_argument('--next-heavy', help='epoch of the next heavy tick (poll, heartbeat)')
    ap.add_argument('--poll-s', type=int, help='light loop interval, sent with the poll')
    ap.add_argument('--wait', type=int, help=f'poll: long poll, the server holds the request up to N s (<= {MAX_WAIT_S}); '
                                             f'exit {POLL_LONG} honoured, {POLL_SHORT} not supported by the server, '
                                             f'{POLL_FAILED} failure')
    ap.add_argument('--session-id', help='heartbeat: the Claude Code session it is about (the launcher, on close)')
    ap.add_argument('--title'); ap.add_argument('--context'); ap.add_argument('--ref')
    ap.add_argument('--task', action='append', help='the task (draft, approval, decision, pergunta, alert: the last one); '
                                                    'fila worker-start, worker start|ping: one more task (repeat)')
    ap.add_argument('--channel'); ap.add_argument('--subject')
    ap.add_argument('--to', help='draft: the recipient; fila ajuste: author (whoever did the work, Planou\'s default) or '
                                 'previous (whoever passed the task to the current column)')
    ap.add_argument('--text', help="draft text; fila ajuste: what to adjust; comentario: the answer; '-' reads stdin")
    ap.add_argument('--type', help='approval action type (send_application, send_message)')
    ap.add_argument('--verb'); ap.add_argument('--description')
    ap.add_argument('--reversible', action='store_true')
    ap.add_argument('--option', action='append', default=[], help='decision option "A=text" (repeat)')
    ap.add_argument('--recommended')
    ap.add_argument('--auto', action='store_true', help='pergunta: the agent already decided by --recommended; record it (low alert), open nothing')
    ap.add_argument('--free-text', action='store_true', help='(default) the person may answer in their own words')
    ap.add_argument('--no-free-text', action='store_true', help='options only, no free answer')
    ap.add_argument('--code'); ap.add_argument('--severity'); ap.add_argument('--reason')
    ap.add_argument('--pr-url', help='fila in_review: the draft PR; fila ajuste: the PR to send back (default: the task\'s)')
    ap.add_argument('--note', help="fila: note for the person (blocked: what the agent needs); '-' reads stdin")
    ap.add_argument('--estimate-h', help='fila: estimate in hours (with started, or fila tempo)')
    ap.add_argument('--remaining-h', help='fila: hours left (every step; done without it sends 0)')
    ap.add_argument('--role', help='fila worker: dev or integrator; fila worker-start: also other')
    ap.add_argument('--tokens', help='fila worker: the subagent tokens; daily explicar: the tokens of the turn')
    ap.add_argument('--steps', help='fila worker: tool uses of the worker')
    ap.add_argument('--duration-ms', help='fila worker: how long the worker ran, in ms')
    ap.add_argument('--result', help='fila worker, worker end: feito, parcial or falhou')
    ap.add_argument('--model', help='fila worker: the model of the worker (optional)')
    ap.add_argument('--key', help='fila worker: the key printed by fila worker-start (default: the one open here for the task and role)')
    ap.add_argument('--phases-from', help="fila worker: the worker's transcript (the Agent tool's output_file) or its agent id: sends the time per phase and the usage per model")
    ap.add_argument('--label', help='fila worker-start, worker start|ping: what the worker is doing, one line')
    ap.add_argument('--name', help='anexo: the file name shown in Planou (default: the file name)')
    ap.add_argument('--self', dest='take', action='store_true', help='pronta: the agent also takes the task (assignee self)')
    ap.add_argument('--what', help='sugestao: what happened'); ap.add_argument('--expected', help='sugestao: what was expected')
    ap.add_argument('--example', help='sugestao: an example without client data')
    ap.add_argument('--priority', help='sugestao: suggested priority, P1 (urgent) to P4 (low); default P3')
    ap.add_argument('--reply-to', help='comentario: the comment_id it answers (from the -- COMENTARIO line)')
    ap.add_argument('--ver', action='store_true', help='comentario: list the task comments instead of posting')
    ap.add_argument('--cost-usd', type=float, help='retro votar, daily explicar: the cost, in USD (optional)')
    a = ap.parse_args(argv)
    if a.cmd == 'sugestao':
        from . import suggestions
        return suggestions.cli(a.agent, a)
    if a.cmd == 'key':
        configure(a.agent, a.root or agent_root(a.agent))
        if a.arg != ['set']: ap.error('use: key set')
        return _key_set()
    configure_agent(a.agent, a.root)
    task1 = a.task[-1] if a.task else None
    if a.cmd == 'active':
        return 0 if active() else 1
    if a.cmd == 'status':
        print(json.dumps({'agent': _S['agent'], 'root': _S['root'], 'base_url': _S['base_url'], 'enabled': bool(config.get('planou.enabled')),
                          'key': bool(_key()), 'publish': _S['publish'], 'live': _S['live'], 'active': active(),
                          'project': _S['project'], 'confidentiality': _S['confidentiality'], 'drafts_text': _S['drafts_text'],
                          'open_asks': len(_load('open_asks.json', {})),
                          'known_tasks': len(_load('state.json', {}).get('tasks') or {}), 'inbox': len(_inbox()['events']),
                          'health': _load('health.json', {}), 'current_task': current_task(),
                          'heartbeat': declared(), 'queue': queue_limit(), 'paused': paused()}, ensure_ascii=False, indent=1))
        if active():
            try:
                print(json.dumps(_call('GET', '/agent/whoami')[1], ensure_ascii=False))
            except PlanouError as e:
                print(f'AVISO (planou): {e}')
        return 0
    if a.cmd == 'heartbeat':
        nt = datetime.fromtimestamp(int(a.next_heavy), timezone.utc) if a.next_heavy else None
        out = heartbeat((a.arg or ['tick'])[0], next_tick=nt, session_id=a.session_id)
        print('\n'.join(out) or 'ok')
        return 0
    if a.cmd == 'poll':
        block, how = poll_wait(a.next_heavy, a.poll_s, a.wait)
        if block: print(block)
        return how if a.wait else 0          # without --wait, as before: always 0 (runners up to 0.26 read the block only)
    if a.cmd == 'cursor-commit':
        cursor_commit()
        return 0
    if a.cmd == 'ack-delivered':
        return 0 if ack_delivered() else 1
    if a.cmd == 'ack':
        return 0 if a.arg and ack(a.arg[0]) else 1
    if a.cmd == 'events':
        print(json.dumps(poll(), ensure_ascii=False, indent=1))
        return 0
    if a.cmd == 'fila':
        return _fila(a, ap)
    if a.cmd == 'worker':
        return _worker(a, ap)
    if a.cmd == 'comentario':
        return _comentario(a, ap)
    if a.cmd == 'retro':
        return _retro(a, ap)
    if a.cmd == 'refino':
        return _refino(a, ap)
    if a.cmd == 'daily':
        return _daily(a, ap)
    if a.cmd == 'autonomia':
        print(autonomy() or 'desconhecida (sem projeto, Planou desligado ou antigo): vale semi_autonomous')
        return 0
    if a.cmd == 'pronta':
        if not a.arg: ap.error('use: pronta CODIGO|PID "motivo" [--self] | pronta CODIGO|PID -')
        try:
            code = mark_ready(a.arg[0], ' '.join(a.arg[1:]).strip() or None, a.take)
        except PlanouError as e:
            print(f'AVISO (planou): {e.message}', file=sys.stderr)
            return 1
        m = _load('ready.json', {}).get(code)
        head = f'{code}: ' + (f'pronta ({m["reason"]})' + (' e o agente pega' if m.get('self') else '') if m else 'sem marca')
        known = (_load('state.json', {}).get('tasks') or {}).get(code)
        if not m:
            print(head)
            return 0
        if not known:
            if _PID.match(code):
                # not a task this agent synced: the sync never touches a task the person created
                _save('ready.json', {c: v for c, v in _load('ready.json', {}).items() if c != code})
                print(f'{code}: nao e tarefa deste agente (o sync so mexe nas que ele criou): nada foi marcado. Tarefa da '
                      'pessoa sai do backlog pela pessoa ou, em projeto autonomo, pela vaga livre do Planou', file=sys.stderr)
                return 1
            print(head + '; vai quando a fonte trouxer o item (sync das fontes)')
            return 0
        try:
            r = ready_now(code)
        except PlanouError as e:
            print(head + f'; o envio agora falhou ({e.message}): o proximo sync das fontes tenta de novo, se houver')
            return 1
        if r is None:
            print(head + '; Planou desligado: nada foi enviado')
            return 1
        if r['result'] not in ('created', 'updated', 'unchanged'):
            print(head + f'; o Planou recusou ({r["result"]}: {"; ".join(r["notes"]) or "sem motivo"})')
            return 1
        print(head + f'; enviado agora ({r["pid"]})'
              + ('; o Planou manteve onde estava: ' + ('; '.join(r['notes']) or 'estado ignorado') if r['state_ignored'] else '')
              + ('; responsavel nao mudou (so em projeto autonomo, tarefa fora do backlog e ainda com uma pessoa)'
                 if r['assignee_ignored'] else ''))
        return 0
    if a.cmd == 'tools':
        data = _load('tools.json', {})
        sent = _load('tools_sent.json', {})
        print(f'lista de {data.get("at") or "-"}; enviada {sent.get("at") or "nunca"}'
              + (f'; recusada em {sent["refused_at"]}' if sent.get('refused_at') else ''))
        for t in data.get('tools') or []:
            exp = (t.get('credential') or {}).get('expires_at')
            print(f'  {t["key"]}: {t["status"]}' + (f' (vence {exp[:10]})' if exp else '')
                  + (f' desde {t["failing_since"][:16]}' if t.get('failing_since') else ''))
        return 0
    if a.cmd == 'attach' or (a.cmd == 'anexo' and len(a.arg) == 2):
        if len(a.arg) != 2: ap.error('use: attach TAREFA ARQUIVO [--name NOME]  (TAREFA: PID, codigo do agente ou tarefa da fila)')
        outcome, out = attach_file(a.arg[0], a.arg[1], name=a.name)
        print('\n'.join(out + [ATTACH_SAID[outcome].format(task=a.arg[0], name=a.name or os.path.basename(a.arg[1]))]))
        return 0 if outcome in ('sent', 'unchanged', 'pending', 'ignored') else 1
    if a.cmd == 'anexo':
        if len(a.arg) != 3: ap.error('use: anexo CODIGO SOURCE_KEY ARQUIVO [--name NOME] | anexo TAREFA ARQUIVO')
        out = attach(a.arg[0], a.arg[1], path=os.path.expanduser(a.arg[2]), name=a.name)
        print('\n'.join(out) or 'ok (enviado, ou igual ao que já estava lá)')
        return 1 if out else 0
    if a.cmd == 'tarefa':
        if a.arg: print(current_task(a.arg[0]) or '-')
        else: print(current_task() or '-')
        return 0
    try:
        if a.cmd == 'draft':
            if not (a.channel and a.to and a.title and a.text): ap.error('draft precisa de --channel, --to, --title e --text')
            res = submit_draft(a.channel, a.to, _text_arg(a.text), a.title, a.context, task1, a.subject, a.ref)
        elif a.cmd == 'approval':
            if not (a.type and a.verb and a.title): ap.error('approval precisa de --type, --verb e --title')
            res = request_approval(a.type, a.verb, a.title, a.context, task1, a.ref, a.description, not a.reversible)
        elif a.cmd == 'decision':
            opts = [(o.split('=', 1)[0].strip(), o.split('=', 1)[1].strip(), o.split('=', 1)[0].strip() == a.recommended)
                    for o in a.option if '=' in o]
            free = not a.no_free_text          # a question always offers "Responder" in the person's own words
            if not a.title or (len(opts) < 2 and not (free and not opts)):
                ap.error('decision precisa de --title e de 2 ou mais --option "A=texto" (ou nenhuma, com resposta livre)')
            res = request_decision(a.title, opts, a.context, task1, a.ref, free)
        elif a.cmd == 'pergunta':
            opts = [(o.split('=', 1)[0].strip(), o.split('=', 1)[1].strip(), o.split('=', 1)[0].strip() == a.recommended)
                    for o in a.option if '=' in o]
            if not a.title or len(opts) == 1: ap.error('pergunta precisa de --title (e nenhuma ou 2 ou mais --option "A=texto")')
            if a.recommended and opts and not any(o[2] for o in opts):
                ap.error(f'--recommended {a.recommended}: nenhuma --option com essa letra')
            if a.auto:
                if not a.recommended:
                    ap.error('pergunta --auto precisa de --recommended <letra> (a opcao que o agente seguiu; S ou N sem --option)')
                try:
                    res = decide_question(a.title, opts, a.recommended, a.context, task1, a.ref)
                except ValueError as e:
                    ap.error(f'--recommended {a.recommended}: {e}')
                print(f'decisao registrada no Planou ({res.get("code")}, alerta baixo); nenhum pedido esperando resposta',
                      file=sys.stderr)
                print(json.dumps(res, ensure_ascii=False))
                return 0
            res = ask_question(a.title, opts, a.context, task1, a.ref)
            if opts and not a.recommended:
                print('dica: marque a opcao recomendada com --recommended <letra> (o botao sai destacado no Planou)',
                      file=sys.stderr)
            print(f'pedido {res.get("code")} aberto no Planou; se o usuario responder no terminal antes: cancel {res.get("code")}',
                  file=sys.stderr)
        elif a.cmd == 'alert':
            if not a.title: ap.error('alert precisa de --title')
            res = raise_alert(a.title, a.code or 'agent', a.severity or 'medium', a.context, task1, a.ref)
        elif a.cmd == 'cancel':
            res = {'cancelled': cancel_ask(a.arg[0], a.reason)}
        else:
            if len(a.arg) < 2 or a.arg[1] not in ('executed', 'failed'): ap.error('use: result CODE executed|failed [--reason]')
            res = {'reported': report_result(a.arg[0], a.arg[1], a.reason)}
    except PlanouError as e:
        print(f'AVISO (planou): {e.message}', file=sys.stderr)
        return 1
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
