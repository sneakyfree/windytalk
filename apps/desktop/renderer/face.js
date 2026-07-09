// The Windy Talk face — ported from the prototype's proven canvas design
// (reference/desktop/index.html), rewired to a small setState/setLevel/setCaption
// API driven by the renderer's VoiceClient callbacks. Pure canvas; no deps.

(() => {
  const C = { listening: "--cyan", thinking: "--amber", speaking: "--green",
    paused: "--slate", idle: "--off", offline: "--off", waiting: "--wait", locked: "--lock" };
  const LABEL = { listening: "Listening", thinking: "Thinking…", speaking: "Speaking",
    paused: "Paused", idle: "Ready", offline: "Offline", waiting: 'Say "Hey Windy"', locked: "Locked" };
  const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const lerp = (a, b, k) => a + (b - a) * k;
  const hexToRgb = (h) => { const n = parseInt(h.slice(1), 16); return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 }; };

  let state = "offline", micOn = true, level = 0, connected = false;
  const dot = document.getElementById("dot"), stateEl = document.getElementById("state"),
    cap = document.getElementById("caption"), edot = document.getElementById("edot"),
    etext = document.getElementById("etext"), btn = document.getElementById("btn"),
    btnLabel = document.getElementById("btnLabel");

  function apply() {
    const col = cssv(state === "offline" ? "--off" : (micOn ? (C[state] || "--off") : "--slate"));
    dot.style.background = dot.style.color = col;
    stateEl.textContent = micOn ? (LABEL[state] || state) : "Paused";
    edot.style.background = edot.style.color = cssv(connected ? "--green" : "--off");
    etext.textContent = connected ? "connected" : "offline";
    btn.classList.toggle("on", micOn);
    btnLabel.textContent = micOn ? "On — tap to mute" : "Off — tap to talk";
  }

  window.face = {
    setState(s) { state = s; if (s !== "paused") micOn = true; apply(); },
    setLevel(v) { level = v; },
    setCaption(text, kind) { cap.textContent = text || ""; cap.style.color = cssv(kind === "say" ? "--cyan" : "--ink"); },
    setConnected(on) { connected = on; if (!on) state = "offline"; apply(); },
    setMic(on) { micOn = on; apply(); },
  };
  window.__toggleMic = () => { micOn = !micOn; apply(); };

  // ---- canvas draw loop (verbatim design from the prototype) ----
  const cv = document.getElementById("stage"), g = cv.getContext("2d");
  let t = 0, blink = 1, nextBlink = 60, eyeOpenC = 1, pupilYC = 0, browYC = 0, mouthC = 0, glowC = 0;
  let hue = { r: 90, g: 139, b: 163 };
  function roundRect(x, y, w, h, r) { g.beginPath(); g.moveTo(x + r, y);
    g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
    g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath(); }
  function draw() {
    t++;
    const active = state !== "offline", speaking = state === "speaking", thinking = state === "thinking";
    const target = hexToRgb(cssv(micOn ? (C[state] || "--off") : "--slate"));
    hue.r = lerp(hue.r, target.r, .08); hue.g = lerp(hue.g, target.g, .08); hue.b = lerp(hue.b, target.b, .08);
    const col = `rgb(${hue.r | 0},${hue.g | 0},${hue.b | 0})`;
    if (t > nextBlink) { blink = 0; if (t > nextBlink + 7) { blink = 1; nextBlink = t + 120 + Math.random() * 160; } }
    const eyeOpenT = (state === "offline" || state === "locked") ? .06 : state === "waiting" ? .5 : (!micOn ? .4 : blink);
    eyeOpenC = lerp(eyeOpenC, eyeOpenT, .4);
    pupilYC = lerp(pupilYC, thinking ? -3.2 : 0, .1);
    const pupilX = thinking ? Math.sin(t * .05) * 2.4 : 0;
    browYC = lerp(browYC, thinking ? 2.5 : (speaking ? -2 : 0), .12);
    mouthC = lerp(mouthC, speaking ? Math.min(.15 + level * .9, 1) : 0, .5);
    glowC = lerp(glowC, active ? (speaking ? 1 : .6) : .12, .08);
    g.clearRect(0, 0, 240, 240);
    const cx = 120, cy = 118;
    const aura = g.createRadialGradient(cx, cy, 20, cx, cy, 120);
    aura.addColorStop(0, `rgba(${hue.r | 0},${hue.g | 0},${hue.b | 0},${.16 * glowC + .03})`);
    aura.addColorStop(1, "rgba(0,0,0,0)"); g.fillStyle = aura; g.fillRect(0, 0, 240, 240);
    const breathe = Math.sin(t * .03) * 1.2, hw = 84, hh = 90 + breathe;
    g.save();
    const hg = g.createLinearGradient(cx, cy - hh, cx, cy + hh);
    hg.addColorStop(0, "#1a2b40"); hg.addColorStop(1, "#0f1c2c");
    g.fillStyle = hg; g.strokeStyle = col; g.lineWidth = 2.5;
    roundRect(cx - hw, cy - hh, hw * 2, hh * 2, 42); g.fill();
    g.globalAlpha = .55 + glowC * .45; g.stroke(); g.globalAlpha = 1; g.restore();
    const ey = cy - 14, ex = 38, ew = 20, eh = 26 * Math.max(eyeOpenC, .04);
    for (const s of [-1, 1]) {
      g.fillStyle = "#0a1420"; roundRect(cx + s * ex - ew / 2, ey - eh / 2, ew, eh, 9); g.fill();
      if (eyeOpenC > .25) {
        g.fillStyle = col; const px = cx + s * ex + pupilX, py = ey + pupilYC;
        g.beginPath(); g.arc(px, py, 5.2, 0, 7); g.fill();
        g.fillStyle = "rgba(255,255,255,.85)"; g.beginPath(); g.arc(px - 1.6, py - 1.8, 1.5, 0, 7); g.fill();
      }
      g.strokeStyle = col; g.lineWidth = 3; g.globalAlpha = .7;
      g.beginPath(); g.moveTo(cx + s * ex - 13, ey - 20 + browYC); g.lineTo(cx + s * ex + 13, ey - 20 + browYC); g.stroke(); g.globalAlpha = 1;
    }
    g.strokeStyle = `rgba(${hue.r | 0},${hue.g | 0},${hue.b | 0},.5)`; g.lineWidth = 2.5;
    g.beginPath(); g.moveTo(cx, cy + 2); g.lineTo(cx - 5, cy + 14); g.lineTo(cx + 3, cy + 14); g.stroke();
    const my = cy + 40; g.strokeStyle = col; g.lineWidth = 3.4; g.lineCap = "round";
    if (state === "offline" || state === "locked") { g.beginPath(); g.moveTo(cx - 20, my); g.lineTo(cx + 20, my); g.stroke(); }
    else if (mouthC > .04) { const mo = 6 + mouthC * 30; g.fillStyle = "#0a1420"; roundRect(cx - 24, my - mo / 2, 48, mo, mo / 2); g.fill(); g.stroke(); }
    else if (thinking) { g.beginPath(); g.moveTo(cx - 14, my + 2); g.lineTo(cx + 14, my + 2); g.stroke(); }
    else { g.beginPath(); g.moveTo(cx - 22, my - 2); g.quadraticCurveTo(cx, my + 12, cx + 22, my - 2); g.stroke(); }
    if (thinking) for (let i = 0; i < 3; i++) {
      const a = (Math.sin(t * .15 - i * .6) + 1) / 2;
      g.fillStyle = `rgba(${hue.r | 0},${hue.g | 0},${hue.b | 0},${.3 + a * .7})`;
      g.beginPath(); g.arc(cx - 16 + i * 16, cy + 72, 3.2, 0, 7); g.fill();
    }
    requestAnimationFrame(draw);
  }
  apply(); draw();
})();
