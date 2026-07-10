// The Windy Talk face — canvas design ported from the prototype, driven purely by
// engine state via window.face. Crucially it NEVER invents mic state (§6): the mic
// on/off comes only from setMicUi(), called by the renderer with the REAL value.

(() => {
  const C = { listening: "--cyan", thinking: "--amber", speaking: "--green",
    paused: "--slate", idle: "--off", offline: "--off", waiting: "--wait", locked: "--lock" };
  const LABEL = { listening: "Listening", thinking: "Thinking…", speaking: "Speaking",
    paused: "Paused", idle: "Ready", offline: "Offline", waiting: 'Say "Hey Windy"', locked: "Locked" };
  const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const lerp = (a, b, k) => a + (b - a) * k;
  const hexToRgb = (h) => { const n = parseInt(h.slice(1), 16); return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 }; };

  let state = "offline", micOn = false, level = 0, connected = false;
  const dot = document.getElementById("dot"), stateEl = document.getElementById("state"),
    cap = document.getElementById("caption"), btn = document.getElementById("btn");

  function apply() {
    const col = cssv(state === "offline" ? "--off" : (micOn ? (C[state] || "--off") : "--slate"));
    dot.style.background = dot.style.color = col;
    stateEl.textContent = state === "offline" ? "Offline" : (micOn ? (LABEL[state] || state) : "Paused");
    btn.classList.toggle("on", micOn);
  }

  window.face = {
    setState(s) { state = s; apply(); },                 // does NOT touch micOn (§6)
    setLevel(v) { level = v; },
    setCaption(text, kind) { cap.textContent = text || ""; cap.style.color = cssv(kind === "say" ? "--cyan" : "--ink"); },
    setConnected(on) { connected = on; if (!on) state = "offline"; apply(); },
    setMicUi(on) { micOn = on; apply(); },               // the ONLY setter of mic state
  };

  // ---- canvas draw loop (design from the prototype) ----
  const cv = document.getElementById("stage"), g = cv.getContext("2d");
  const W = cv.width, H = cv.height, cx = W / 2, cy = H * 0.49;
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
    g.clearRect(0, 0, W, H);
    const aura = g.createRadialGradient(cx, cy, 16, cx, cy, 100);
    aura.addColorStop(0, `rgba(${hue.r | 0},${hue.g | 0},${hue.b | 0},${.16 * glowC + .03})`);
    aura.addColorStop(1, "rgba(0,0,0,0)"); g.fillStyle = aura; g.fillRect(0, 0, W, H);
    const breathe = Math.sin(t * .03) * 1.0, hw = 70, hh = 76 + breathe;
    g.save();
    const hg = g.createLinearGradient(cx, cy - hh, cx, cy + hh);
    hg.addColorStop(0, "#1a2b40"); hg.addColorStop(1, "#0f1c2c");
    g.fillStyle = hg; g.strokeStyle = col; g.lineWidth = 2.5;
    roundRect(cx - hw, cy - hh, hw * 2, hh * 2, 36); g.fill();
    g.globalAlpha = .55 + glowC * .45; g.stroke(); g.globalAlpha = 1; g.restore();
    const ey = cy - 12, ex = 32, ew = 17, eh = 22 * Math.max(eyeOpenC, .04);
    for (const s of [-1, 1]) {
      g.fillStyle = "#0a1420"; roundRect(cx + s * ex - ew / 2, ey - eh / 2, ew, eh, 8); g.fill();
      if (eyeOpenC > .25) {
        g.fillStyle = col; const px = cx + s * ex + pupilX, py = ey + pupilYC;
        g.beginPath(); g.arc(px, py, 4.4, 0, 7); g.fill();
        g.fillStyle = "rgba(255,255,255,.85)"; g.beginPath(); g.arc(px - 1.4, py - 1.5, 1.3, 0, 7); g.fill();
      }
      g.strokeStyle = col; g.lineWidth = 2.6; g.globalAlpha = .7;
      g.beginPath(); g.moveTo(cx + s * ex - 11, ey - 17 + browYC); g.lineTo(cx + s * ex + 11, ey - 17 + browYC); g.stroke(); g.globalAlpha = 1;
    }
    g.strokeStyle = `rgba(${hue.r | 0},${hue.g | 0},${hue.b | 0},.5)`; g.lineWidth = 2.2;
    g.beginPath(); g.moveTo(cx, cy + 2); g.lineTo(cx - 4, cy + 12); g.lineTo(cx + 3, cy + 12); g.stroke();
    const my = cy + 34; g.strokeStyle = col; g.lineWidth = 3.0; g.lineCap = "round";
    if (state === "offline" || state === "locked") { g.beginPath(); g.moveTo(cx - 17, my); g.lineTo(cx + 17, my); g.stroke(); }
    else if (mouthC > .04) { const mo = 5 + mouthC * 26; g.fillStyle = "#0a1420"; roundRect(cx - 20, my - mo / 2, 40, mo, mo / 2); g.fill(); g.stroke(); }
    else if (thinking) { g.beginPath(); g.moveTo(cx - 12, my + 2); g.lineTo(cx + 12, my + 2); g.stroke(); }
    else { g.beginPath(); g.moveTo(cx - 18, my - 2); g.quadraticCurveTo(cx, my + 10, cx + 18, my - 2); g.stroke(); }
    if (thinking) for (let i = 0; i < 3; i++) {
      const a = (Math.sin(t * .15 - i * .6) + 1) / 2;
      g.fillStyle = `rgba(${hue.r | 0},${hue.g | 0},${hue.b | 0},${.3 + a * .7})`;
      g.beginPath(); g.arc(cx - 13 + i * 13, cy + 60, 2.7, 0, 7); g.fill();
    }
    requestAnimationFrame(draw);
  }
  apply(); draw();
})();
