'use strict';
// team-terminals: one VS Code terminal per team agent, each running `team <agent>`.
// Reads ~/.config/team/agents.json (or, without it, the agents found in ~/.config/team/*.session and the live instances
// of the agent plugin), opens the missing terminals on startup and when the file changes, and closes the terminal of an
// agent that was disabled or removed (stopping its runner first when it is an agent-plugin instance).
const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFile } = require('child_process');
const core = require('./lib/plan');
const update = require('./lib/update');

const PREV_KEY = 'teamTerminals.previousAgents';
let out;
let chain = Promise.resolve();
let lastError = '';

function cfg() { return vscode.workspace.getConfiguration('teamTerminals'); }
function home() { return os.homedir(); }
function log(msg) { out.appendLine(`[${new Date().toLocaleTimeString()}] ${msg}`); }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function termNames(t) {
  const names = new Set([t.name]);
  if (t.creationOptions && t.creationOptions.name) names.add(t.creationOptions.name);
  return [...names];
}
function terminalsNamed(name) { return vscode.window.terminals.filter(t => termNames(t).includes(name)); }

async function snapshot() {
  const list = [];
  for (const t of vscode.window.terminals) {
    let idle = false;
    try { idle = core.shellIsIdle(await t.processId); } catch { idle = false; }
    for (const name of termNames(t)) list.push({ name, idle });
  }
  return list;
}

function runStop(cmd) {
  return new Promise(resolve => {
    execFile(cmd.file, cmd.args, { timeout: 90000 }, (err, stdout, stderr) => {
      resolve({ ok: !err, text: `${stdout || ''}${stderr || ''}`.trim(), err });
    });
  });
}

async function closeAgent(name) {
  const cmd = core.stopCommandFor(name, home(), cfg().get('agentRunner') || '');
  if (cmd) {
    log(`${name}: parando o runner (${cmd.args.join(' ')})`);
    const r = await runStop(cmd);
    log(`${name}: stop ${r.ok ? 'ok' : 'falhou'}${r.text ? `: ${r.text}` : ''}`);
    if (!r.ok) vscode.window.showWarningMessage(`Team: o stop do runner de ${name} falhou; fechando o terminal mesmo assim. Veja a saída "Team Terminals".`);
  } else {
    vscode.window.showInformationMessage(`Team: ${name} não é instância do plugin agent; fechei só o terminal. Se ele tem runner, pare pela sessão dele.`);
  }
  for (const t of terminalsNamed(name)) t.dispose();
  log(`${name}: terminal fechado`);
}

function openAgent(name, show) {
  const text = core.launchText(cfg().get('command'), name);
  const t = vscode.window.createTerminal({ name, iconPath: new vscode.ThemeIcon('robot') });
  t.sendText(text, true);
  if (show) t.show(false);
  log(`${name}: terminal aberto (${text})`);
  return t;
}

// Serialized: a file change during the delayed openings waits for the running pass.
function reconcile(reason) {
  chain = chain.then(() => doReconcile(reason)).catch(e => log(`erro: ${e && e.stack || e}`));
  return chain;
}

async function doReconcile(reason) {
  const r = core.readAgents(home());
  if (r.error) {
    if (r.error !== lastError) vscode.window.showWarningMessage(`Team: ${r.error}. Nada foi aberto nem fechado.`);
    lastError = r.error; log(`${reason}: ${r.error}`);
    return;
  }
  lastError = '';
  const state = extCtx.globalState;
  const previous = r.source === 'file' ? (state.get(PREV_KEY) || null) : null;   // agents.json deleted: close nothing
  const reviveIdle = cfg().get('reviveIdle') !== false && !core.fromFile(reason);
  const p = core.plan(r.agents, previous, await snapshot(), { reviveIdle });
  log(`${reason} (lista: ${r.source === 'file' ? 'agents.json' : 'gerada das sessões'}): abrir [${p.open}] reaproveitar [${p.revive}] fechar [${p.close}]`);
  for (const name of p.close) await closeAgent(name);
  const delay = Math.max(0, Number(cfg().get('delaySeconds')) || 0) * 1000;
  const todo = [...p.open.map(n => ['open', n]), ...p.revive.map(n => ['revive', n])];
  const order = new Map(r.agents.map(a => [a.name, a.order]));
  todo.sort((a, b) => order.get(a[1]) - order.get(b[1]));
  let first = true;
  for (const [kind, name] of todo) {
    if (!first && delay) await sleep(delay);
    first = false;
    const now = await snapshot();   // terminals restored late by the persistent sessions show up here
    const same = now.filter(t => t.name === name);
    if (kind === 'open' && same.length) { log(`${name}: já tem terminal, não dupliquei`); continue; }
    if (kind === 'revive') {
      const t = terminalsNamed(name)[0];
      if (!t || !same.every(x => x.idle)) continue;
      t.sendText(core.launchText(cfg().get('command'), name), true);
      log(`${name}: terminal restaurado estava ocioso; rodei o comando nele`);
      continue;
    }
    openAgent(name, false);
  }
  await state.update(PREV_KEY, r.source === 'file' ? r.agents : null);
}

async function cmdOpenOne() {
  const r = core.readAgents(home());
  if (r.error) { vscode.window.showWarningMessage(`Team: ${r.error}`); return; }
  const items = r.agents.map(a => ({ label: a.name, description: a.enabled ? '' : 'desabilitado',
    detail: terminalsNamed(a.name).length ? 'terminal aberto: só mostra' : undefined }));
  const pick = await vscode.window.showQuickPick(items, { placeHolder: 'Agente' });
  if (!pick) return;
  const existing = terminalsNamed(pick.label)[0];
  if (existing) existing.show(false); else openAgent(pick.label, true);
}

async function cmdGenerate() {
  const file = core.agentsFile(home());
  const list = core.discoverAgents(home());
  if (!list.length) { vscode.window.showWarningMessage('Team: não achei nenhum agente (~/.config/team/*.session nem instância live do plugin agent).'); return; }
  const r = core.readAgents(home());
  if (r.error) { vscode.window.showWarningMessage(`Team: ${r.error}. Corrija o arquivo antes de gerar.`); return; }
  let out = list;
  if (r.source === 'file') {
    // never rewrite what is there: the `team` launcher reads aliases, cwd and skill from the same entries
    const m = core.mergeAgents(r.agents, list);
    if (!m.added.length) { vscode.window.showInformationMessage('Team: o agents.json já tem todos os agentes achados.'); await vscode.window.showTextDocument(vscode.Uri.file(file)); return; }
    const ok = await vscode.window.showWarningMessage(`Acrescentar ao ${file}: ${m.added.join(', ')}? Os agentes que já estão lá ficam como estão.`, { modal: true }, 'Acrescentar');
    if (ok !== 'Acrescentar') return;
    out = m.list;
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, core.renderAgents(out));
  log(`agents.json gravado com ${out.length} agentes`);
  await vscode.window.showTextDocument(vscode.Uri.file(file));
}

// Self-update from the local copy of the repository (lib/update.js). The workbench command installs on the side the
// extension runs (WSL); if it fails, the server's own CLI does the same.
const UPDATE_EVERY_MS = 30 * 60 * 1000;
const updateState = {};
function updateSource() { return (cfg().get('updateSource') || '').trim() || update.findSource(home()); }
async function installVsix(vsix) {
  try {
    await vscode.commands.executeCommand('workbench.extensions.installExtension', vscode.Uri.file(vsix));
    return;
  } catch (e) { log(`atualização: o comando de instalar falhou (${e && e.message || e}); tentando pelo code-server`); }
  const cli = path.join(vscode.env.appRoot, 'bin', 'code-server');
  await new Promise((resolve, reject) => execFile(cli, ['--install-extension', vsix, '--force'], { timeout: 120000 },
    (err, stdout, stderr) => err ? reject(new Error(`${err.message} ${stderr || ''}`.trim())) : resolve(stdout)));
}
async function notifyUpdate(version) {
  log(`atualização: Team Terminals ${version} instalada; falta recarregar a janela`);
  const pick = await vscode.window.showInformationMessage(`Team Terminals ${version} instalada: recarregar a janela`, 'Recarregar');
  if (pick === 'Recarregar') await vscode.commands.executeCommand('workbench.action.reloadWindow');
}
function checkUpdate() {
  return update.checkForUpdate({ enabled: cfg().get('autoUpdate') !== false, current: extCtx.extension.packageJSON.version,
    source: updateSource(), install: installVsix, notify: v => { notifyUpdate(v).catch(e => log(`aviso: ${e}`)); },
    log, state: updateState }).catch(e => log(`atualização: erro ${e && e.stack || e}`));
}

let extCtx;
function activate(context) {
  extCtx = context;
  out = vscode.window.createOutputChannel('Team Terminals');
  context.subscriptions.push(out,
    vscode.commands.registerCommand('teamTerminals.openAll', () => reconcile('comando')),
    vscode.commands.registerCommand('teamTerminals.openOne', cmdOpenOne),
    vscode.commands.registerCommand('teamTerminals.generate', cmdGenerate));

  // watch the folder, not the file: editors that save by rename break a watch on the file itself. The watcher of a
  // folder outside the workspace loses events, so a periodic check of mtime/size backs it up (pollSeconds, 0 = off).
  const dir = core.teamDir(home());
  const pollMs = () => Math.max(0, Number(cfg().get('pollSeconds')) || 0) * 1000;
  const fw = core.fileWatch({ file: core.agentsFile(home()), pollMs: pollMs(), debounceMs: 700,
    enabled: () => !!cfg().get('autoOpen'), onChange: reason => reconcile(reason) });
  context.subscriptions.push(fw,
    vscode.workspace.onDidChangeConfiguration(e => { if (e.affectsConfiguration('teamTerminals.pollSeconds')) fw.poll(pollMs()); }));
  try {
    const w = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(vscode.Uri.file(dir), 'agents.json'));
    w.onDidChange(fw.fromWatcher); w.onDidCreate(fw.fromWatcher); w.onDidDelete(fw.fromWatcher);
    context.subscriptions.push(w);
  } catch (e) { log(`sem watcher em ${dir}: ${e.message}`); }
  log(`acompanhando ${core.agentsFile(home())}: watcher${fw.polling() ? ` e verificação a cada ${pollMs() / 1000} s` : ' (verificação periódica desligada)'}`);

  if (cfg().get('autoOpen')) {
    // give the persistent terminal sessions a moment to be restored before counting them
    const t = setTimeout(() => reconcile('início'), Math.max(0, Number(cfg().get('startupDelaySeconds')) || 0) * 1000);
    context.subscriptions.push({ dispose: () => clearTimeout(t) });
  }
  checkUpdate();
  const ut = setInterval(checkUpdate, UPDATE_EVERY_MS);
  context.subscriptions.push({ dispose: () => clearInterval(ut) });
  return { reconcile, _state: () => chain, _fileWatch: fw };   // used by the integration test
}

function deactivate() {}

module.exports = { activate, deactivate };
