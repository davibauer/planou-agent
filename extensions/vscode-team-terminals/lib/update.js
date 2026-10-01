'use strict';
// Self-update from a local copy of the claude-plugins repository (no marketplace, no account): the copy in use gets a
// `git pull --ff-only` after each merge, and it carries the packaged extension at a fixed path. Free of the vscode API
// so node --test can drive it.
const fs = require('fs');
const path = require('path');

const VSIX_REL = path.join('extensions', 'vscode-team-terminals', 'dist', 'team-terminals.vsix');
const PKG_REL = path.join('extensions', 'vscode-team-terminals', 'package.json');

function parseVersion(v) {
  const m = /^(\d+)\.(\d+)\.(\d+)$/.exec(String(v || '').trim());
  return m ? m.slice(1).map(Number) : null;
}

// 1 when a > b, -1 when a < b, 0 when equal; null when either is not a plain x.y.z (never update on doubt).
function compareVersions(a, b) {
  const x = parseVersion(a), y = parseVersion(b);
  if (!x || !y) return null;
  for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] > y[i] ? 1 : -1;
  return 0;
}

function hasVsix(dir) { try { return fs.statSync(path.join(dir, VSIX_REL)).isFile(); } catch { return false; } }

// Default source: the repository the agent plugin's skill links point into (~/.claude/skills/agent is a link into the
// copy in use), walking up until a folder has the packaged extension. '' when there is none.
function findSource(home, links = ['.claude/skills/agent']) {
  for (const rel of links) {
    let dir;
    try { dir = fs.realpathSync(path.join(home, rel)); } catch { continue; }
    for (;;) {
      if (hasVsix(dir)) return dir;
      const up = path.dirname(dir);
      if (up === dir) break;
      dir = up;
    }
  }
  return '';
}

// What the source offers: { vsix, version } or { error } (missing folder, no package, unreadable version).
function available(source) {
  if (!source) return { error: 'nenhuma cópia do repositório achada (teamTerminals.updateSource vazio e sem link em ~/.claude/skills/agent)' };
  const vsix = path.join(source, VSIX_REL);
  if (!hasVsix(source)) return { error: `sem ${vsix}` };
  let version;
  try { version = JSON.parse(fs.readFileSync(path.join(source, PKG_REL), 'utf8')).version; } catch (e) { return { error: `não li ${path.join(source, PKG_REL)}: ${e.message}` }; }
  if (!parseVersion(version)) return { error: `versão inválida em ${path.join(source, PKG_REL)}: ${version}` };
  return { vsix, version };
}

// One check. `state.installed` remembers the version installed in this window: until the reload, the running extension
// still reports the old version, and without it every tick would install and warn again.
async function checkForUpdate({ enabled, current, source, install, notify, log = () => {}, state = {} }) {
  if (!enabled) return { action: 'disabled' };
  const a = available(source);
  if (a.error) { log(`atualização: ${a.error}`); return { action: 'no-source', error: a.error }; }
  const cmp = compareVersions(a.version, current);
  if (cmp === null || cmp <= 0) return { action: 'up-to-date', version: a.version };
  if (state.installed && compareVersions(a.version, state.installed) <= 0) return { action: 'pending-reload', version: state.installed };
  log(`atualização: ${current} -> ${a.version} (${a.vsix})`);
  try { await install(a.vsix); } catch (e) { log(`atualização: falhou ao instalar ${a.vsix}: ${e && e.message || e}`); return { action: 'failed', version: a.version }; }
  state.installed = a.version;
  await notify(a.version);
  return { action: 'installed', version: a.version };
}

module.exports = { VSIX_REL, PKG_REL, parseVersion, compareVersions, findSource, available, checkForUpdate };
