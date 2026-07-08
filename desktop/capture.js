// Render the face in a given state and save a PNG — verifies the UI without a live
// screen. Usage: electron capture.js <state> <outfile>
const { app, BrowserWindow } = require('electron');
const fs = require('fs');
const path = require('path');
if (process.env.WJ_NO_SANDBOX) app.commandLine.appendSwitch('no-sandbox');
app.disableHardwareAcceleration();

const state = process.argv[2] || 'listening';
const out = process.argv[3] || `/tmp/wj_face_${state}.png`;

app.whenReady().then(async () => {
  const win = new BrowserWindow({ width: 340, height: 500, show: false,
    frame: false, transparent: true, backgroundColor: '#00000000',
    webPreferences: { offscreen: false } });
  await win.loadFile(path.join(__dirname, 'index.html'),
                     { search: `demo=${state}` });
  await new Promise(r => setTimeout(r, 2600));
  const img = await win.webContents.capturePage();
  fs.writeFileSync(out, img.toPNG());
  console.log('captured', state, '->', out);
  app.quit();
});
