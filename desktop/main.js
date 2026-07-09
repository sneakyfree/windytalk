// Windy Talk desktop shell — a thin Electron window over the Python voice agent.
// It spawns the agent (run.sh --ui), shows the face/button/status, and cleans up on quit.
const { app, BrowserWindow } = require('electron');
const { spawn } = require('child_process');
const path = require('path');

// This box lives in a sandboxed dev environment on some machines; harmless in normal use.
if (process.env.WJ_NO_SANDBOX) app.commandLine.appendSwitch('no-sandbox');

const REPO = path.resolve(__dirname, '..');
let agent = null;

function startAgent() {
  // run.sh sets up ydotool + AT-SPI + the Veron tunnel, then runs jarvis.py --ui
  agent = spawn('bash', [path.join(REPO, 'run.sh'), '--ui'], {
    cwd: REPO, env: process.env,
  });
  agent.stdout.on('data', d => process.stdout.write('[agent] ' + d));
  agent.stderr.on('data', d => process.stderr.write('[agent] ' + d));
  agent.on('exit', c => console.log('[agent] exited', c));
}

function stopAgent() {
  if (agent && !agent.killed) { try { agent.kill('SIGTERM'); } catch (e) {} }
}

function createWindow() {
  const win = new BrowserWindow({
    width: 340, height: 500, frame: false, transparent: true, resizable: false,
    hasShadow: true, backgroundColor: '#00000000', title: 'Windy Talk',
    webPreferences: { contextIsolation: true },
  });
  win.loadFile(path.join(__dirname, 'index.html'));
  return win;
}

app.whenReady().then(() => {
  startAgent();
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('before-quit', stopAgent);
app.on('window-all-closed', () => { stopAgent(); app.quit(); });
