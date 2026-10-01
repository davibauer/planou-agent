'use strict';
// Pure logic of the team-terminals extension: which agents exist, which terminals to open, revive or close.
// No `vscode` import here, so node:test covers it with a temporary HOME.
const fs = require('fs');
const path = require('path');

const NAME_RE = /^[a-z0-9][a-z0-9-]*$/;

function teamDir(home) { return path.join(home, '.config', 'team'); }
function agentsFile(home) { return path.join(teamDir(home), 'agents.json'); }

// Validates the agents.json content. Throws Error with a pt-BR message when the file is not usable, so the caller keeps
// the previous list instead of acting on a half-written file.
function parseAgents(text) {
  let data;
  try { data = JSON.parse(text); } catch (e) { throw new Error(`agents.json não é JSON válido: ${e.message}`); }
  if (!Array.isArray(data)) throw new Error('agents.json precisa ser uma lista: [{"name": "...", "enabled": true, "order": 10}]');
  const seen = new Set();
  const out = data.map((item, i) => {
    if (!item || typeof item !== 'object') throw new Error(`agents.json, item ${i}: precisa ser um objeto`);
    const name = item.name;
    if (typeof name !== 'string' || !NAME_RE.test(name)) throw new Error(`agents.json, item ${i}: "name" inválido (${JSON.stringify(name)}); use letras minúsculas, dígitos e hífen`);
    if (seen.has(name)) throw new Error(`agents.json: "${name}" aparece duas vezes`);
    seen.add(name);
    if (item.enabled !== undefined && typeof item.enabled !== 'boolean') throw new Error(`agents.json, "${name}": "enabled" precisa ser true ou false`);
    if (item.order !== undefined && typeof item.order !== 'number') throw new Error(`agents.json, "${name}": "order" precisa ser um número`);
    // other fields (aliases, cwd, skill, rotate: read by the `team` launcher) are kept as they are
    const { name: _n, enabled: _e, order: _o, ...rest } = item;
    return { name, enabled: item.enabled !== false, order: item.order === undefined ? (i + 1) * 10 : item.order, ...rest };
  });
  return sortAgents(out);
}

function sortAgents(list) {
  return list.slice().sort((a, b) => (a.order - b.order) || a.name.localeCompare(b.name));
}

function safeReaddir(dir) { try { return fs.readdirSync(dir); } catch { return []; } }

// The list when agents.json does not exist: every ~/.config/team/<agent>.session plus every instance of the agent plugin
// (~/.config/agent/<name>/config/config.json) with "live": true. Reads only; writes nothing.
function discoverAgents(home) {
  const names = new Set();
  for (const f of safeReaddir(teamDir(home))) {
    if (f.endsWith('.session')) {
      const n = f.slice(0, -'.session'.length);
      if (NAME_RE.test(n)) names.add(n);
    }
  }
  const agentRoot = path.join(home, '.config', 'agent');
  for (const n of safeReaddir(agentRoot)) {
    if (!NAME_RE.test(n)) continue;
    try {
      const cfg = JSON.parse(fs.readFileSync(path.join(agentRoot, n, 'config', 'config.json'), 'utf8'));
      if (cfg && cfg.live === true) names.add(n);
    } catch { /* no config or unreadable: not an instance */ }
  }
  return [...names].sort().map((name, i) => ({ name, enabled: true, order: (i + 1) * 10 }));
}

// { source: 'file' | 'discovered', agents, error? }. A broken file returns error and no agents: never fall back to the
// discovered list there, or a typo would open every agent.
function readAgents(home) {
  const file = agentsFile(home);
  let text;
  try { text = fs.readFileSync(file, 'utf8'); } catch (e) {
    if (e.code === 'ENOENT') return { source: 'discovered', agents: discoverAgents(home) };
    return { source: 'file', agents: null, error: `não consegui ler ${file}: ${e.message}` };
  }
  try { return { source: 'file', agents: parseAgents(text) }; } catch (e) {
    return { source: 'file', agents: null, error: e.message };
  }
}

function renderAgents(list) {
  return JSON.stringify(sortAgents(list).map(({ name, enabled, order, ...rest }) => ({ name, enabled, order, ...rest })), null, 2) + '\n';
}

// The file after "Team: gerar agents.json" when it already exists: every entry kept as it is (with the fields the
// `team` launcher reads), plus the discovered agents it does not have, after the last order. { list, added: [names] }.
function mergeAgents(existing, discovered) {
  const have = new Set(existing.map(a => a.name));
  let last = existing.reduce((m, a) => Math.max(m, a.order), 0);
  const added = [];
  const list = existing.slice();
  for (const a of sortAgents(discovered)) {
    if (have.has(a.name)) continue;
    last += 10;
    list.push({ name: a.name, enabled: a.enabled, order: last });
    added.push(a.name);
  }
  return { list: sortAgents(list), added };
}

// What to do given the current list, the list of the previous reconcile (null when none) and the open terminals.
//   terminals: [{ name, idle }] where idle = the shell has no child process (a terminal restored after a reboot)
// Returns { open: [names], revive: [names], close: [names] }:
//   open   enabled agent without a terminal of that name;
//   revive enabled agent whose terminal exists but sits idle (reviveIdle on): run the command inside it;
//   close  agent enabled before (previous) and now disabled or gone, whose terminal is open. Terminals of names never
//          seen enabled are left alone: the user's own terminals are never touched.
function plan(current, previous, terminals, opts = {}) {
  const byName = new Map();
  for (const t of terminals) {
    const prev = byName.get(t.name);
    // two terminals with one name: busy wins (someone is running there)
    byName.set(t.name, prev ? { name: t.name, idle: prev.idle && t.idle } : t);
  }
  const open = [], revive = [], close = [];
  const enabledNow = new Set();
  for (const a of sortAgents(current)) {
    if (!a.enabled) continue;
    enabledNow.add(a.name);
    const t = byName.get(a.name);
    if (!t) open.push(a.name);
    else if (opts.reviveIdle && t.idle) revive.push(a.name);
  }
  const knownBefore = new Set((previous || []).filter(a => a.enabled).map(a => a.name));
  for (const name of knownBefore) {
    if (!enabledNow.has(name) && byName.has(name)) close.push(name);
  }
  // a disabled agent whose terminal is open is closed too, even without a previous list (disabled on purpose in the file)
  for (const a of current) {
    if (!a.enabled && byName.has(a.name) && !close.includes(a.name)) close.push(a.name);
  }
  return { open, revive, close };
}

// The stop of an agent-plugin instance: [runner.sh, name, 'stop'] when ~/.config/agent/<name>/config/config.json exists
// and the runner is found; null otherwise (then the terminal is just closed, with a warning).
function stopCommandFor(name, home, runnerPath) {
  if (!NAME_RE.test(name)) return null;
  if (!fs.existsSync(path.join(home, '.config', 'agent', name, 'config', 'config.json'))) return null;
  const runner = runnerPath || path.join(home, '.claude', 'skills', 'agent', 'scripts', 'runner.sh');
  if (!fs.existsSync(runner)) return null;
  return { file: 'bash', args: [runner, name, 'stop'] };
}

function launchText(template, name) {
  if (!NAME_RE.test(name)) throw new Error(`nome de agente inválido: ${name}`);
  return (template || 'team ${agent}').split('${agent}').join(name);
}

// true when the process has no child (a bare shell waiting at the prompt). Reads /proc; unknown -> false (busy).
function shellIsIdle(pid, procRoot = '/proc') {
  if (!pid) return false;
  const tasks = safeReaddir(path.join(procRoot, String(pid), 'task'));
  if (!tasks.length) return false;
  for (const tid of tasks) {
    try {
      if (fs.readFileSync(path.join(procRoot, String(pid), 'task', tid, 'children'), 'utf8').trim()) return false;
    } catch { return false; }
  }
  return true;
}

// Signature of the file for the periodic check: mtime, size and inode (an editor that saves by rename changes the inode).
function fileSig(file) {
  try { const s = fs.statSync(file); return `${s.mtimeMs}:${s.size}:${s.ino}`; } catch { return 'missing'; }
}

const FILE_REASON = 'arquivo';
const POLL_REASON = 'arquivo (verificação periódica)';

// Follows agents.json by two ways that share one debounce: the VS Code watcher (fromWatcher) and a periodic check of the
// file signature (check, every pollMs; 0 = off). The watcher misses events for a folder outside the workspace, so the
// check catches the change the watcher lost. The baseline is taken at creation and on every watcher event, so one
// change is reconciled once. enabled() is asked on every event (autoOpen can change while VS Code is open).
function fileWatch({ file, pollMs = 0, debounceMs = 700, enabled = () => true, onChange, sig = fileSig }) {
  let last = sig(file), timer = null, interval = null;
  const fire = reason => {
    clearTimeout(timer);
    timer = setTimeout(() => { timer = null; onChange(reason); }, debounceMs);
  };
  const w = {
    fromWatcher() {
      if (!enabled()) return;
      last = sig(file);
      fire(FILE_REASON);
    },
    check() {
      if (!enabled()) return false;
      const now = sig(file);
      if (now === last) return false;
      last = now;
      fire(POLL_REASON);
      return true;
    },
    poll(ms) {
      clearInterval(interval); interval = null;
      if (Number(ms) > 0) interval = setInterval(w.check, Number(ms));
      return w;
    },
    polling() { return interval !== null; },
    dispose() { clearTimeout(timer); clearInterval(interval); timer = interval = null; },
  };
  return w.poll(pollMs);
}

// A pass started by agents.json (watcher or periodic check) never revives idle terminals.
function fromFile(reason) { return String(reason || '').startsWith(FILE_REASON); }

module.exports = { NAME_RE, agentsFile, teamDir, parseAgents, discoverAgents, readAgents, renderAgents, mergeAgents, plan,
  stopCommandFor, launchText, shellIsIdle, sortAgents, fileSig, fileWatch, fromFile, FILE_REASON, POLL_REASON };
