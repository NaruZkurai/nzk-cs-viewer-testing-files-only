#!/usr/bin/env node
/* textprobe.mjs — interrogate the LIVE original (Chromium+CDP) for the FACTS
 * about its text: the ACTUAL resolved platform font per text element (not the
 * requested CSS stack), computed font metrics, every text node's box, and a
 * screenshot with the glyph ink removed (colour:transparent) so the text share
 * of a pixel diff can be measured rather than guessed.
 *
 * Read-only against the original. Boot it with with_serve.sh:
 *   rust-only-csviewer/tools/with_serve.sh --repo /tmp/fid-corpus --root . -- \
 *     node native-csviewer/tools/textprobe.mjs --out /tmp/tp.json --width 1600 --height 1000
 * (with_serve appends --url/--cdp-port.)
 *
 * Writes: <out> (JSON report), <out>.notext.png (reference with text ink hidden).
 */
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import process from 'node:process';

function arg(name, dflt) {
  const i = process.argv.indexOf(name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}
const URL_ = arg('--url', 'http://127.0.0.1:8795/');
const PORT = Number(arg('--cdp-port', '9341'));
const CHROME = arg('--chrome', 'chromium');
const OUT = arg('--out', '/tmp/textprobe.json');
const W = Number(arg('--width', '1600'));
const H = Number(arg('--height', '1000'));
const BOOT = Number(arg('--boot-ms', '90000'));
const SETTLE = Number(arg('--settle-ms', '1500'));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForDevtools(port, deadline) {
  let last;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (r.ok) return await r.json();
      last = `HTTP ${r.status}`;
    } catch (e) { last = e.message; }
    await sleep(120);
  }
  throw new Error(`devtools never came up on ${port}: ${last}`);
}

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = new Map();
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      } else if (msg.method) {
        for (const h of this.handlers.get(msg.method) || []) h(msg.params);
      }
    });
  }
  on(method, fn) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(fn);
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true,
      allowUnsafeEvalBlockedByCSP: true,
    });
    if (r.exceptionDetails) {
      throw new Error('eval threw: ' + (r.exceptionDetails.exception?.description ||
        r.exceptionDetails.text));
    }
    return r.result.value;
  }
}

// Installed before any page script: records every canvas text draw and
// measureText call (the graph labels are painted by render.js, not DOM nodes,
// so neither getComputedStyle nor CSS color:transparent can see them), and
// provides a switch that suppresses canvas glyph ink on demand.
const HOOK = `
(function(){
  if (window.__tp_installed) return;
  window.__tp_installed = true;
  window.__tp_hide_text = false;
  window.__tp_ft = [];
  window.__tp_mt = [];
  try {
    var P = CanvasRenderingContext2D.prototype;
    var of = P.fillText;
    P.fillText = function(text, x, y){
      try {
        if (this.globalAlpha > 0.01) {
          window.__tp_ft.push({t:String(text), x:x, y:y, font:this.font,
            style:String(this.fillStyle), align:this.textAlign, baseline:this.textBaseline});
        }
      } catch(e){}
      if (window.__tp_hide_text) return;
      return of.apply(this, arguments);
    };
    var os = P.strokeText;
    P.strokeText = function(){ if (window.__tp_hide_text) return; return os.apply(this, arguments); };
    var om = P.measureText;
    P.measureText = function(text){
      var r;
      try { r = om.apply(this, arguments);
        window.__tp_mt.push({t:String(text), font:this.font, width:r.width});
      } catch(e){ r = om.apply(this, arguments); }
      return r;
    };
  } catch(e) { window.__tp_hookerr = String(e); }
})();
`;

// Walk the DOM tree from DOM.getDocument; return one entry per element that has
// a non-empty text child, with its nodeId (for CSS.getPlatformFontsForNode).
function textElements(root) {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.nodeType === 1) {
      const kids = n.children || [];
      const texts = kids
        .filter((c) => c.nodeType === 3 && (c.nodeValue || '').trim())
        .map((c) => c.nodeValue.trim())
        .join(' ');
      if (texts) {
        out.push({ nodeId: n.nodeId, tag: n.nodeName, text: texts.slice(0, 120) });
      }
    }
    for (const c of n.children || []) walk(c);
  };
  walk(root);
  return out;
}

async function main() {
  const profile = mkdtempSync(join(tmpdir(), 'csviewer-tp-'));
  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    '--force-device-scale-factor=1', '--hide-scrollbars',
    `--user-data-dir=${profile}`, `--remote-debugging-port=${PORT}`, 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });
  let chromeErr = '';
  chrome.stderr.on('data', (d) => { chromeErr += d.toString(); });

  const deadline = Date.now() + BOOT;
  let ws, cdp, report;
  try {
    await waitForDevtools(PORT, deadline);
    const create = await fetch(
      `http://127.0.0.1:${PORT}/json/new?${encodeURIComponent('about:blank')}`,
      { method: 'PUT' });
    if (!create.ok) throw new Error('could not open a target: HTTP ' + create.status);
    const target = await create.json();
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', () => rej(new Error('cdp websocket error')), { once: true });
    });
    cdp = new CDP(ws);
    await cdp.send('Runtime.enable');
    await cdp.send('Page.enable');
    await cdp.send('DOM.enable');
    await cdp.send('CSS.enable');
    await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: HOOK });
    await cdp.send('Emulation.setDeviceMetricsOverride',
      { width: W, height: H, deviceScaleFactor: 1, mobile: false });

    const loaded = new Promise((res) => cdp.on('Page.loadEventFired', res));
    await cdp.send('Page.navigate', { url: URL_ });
    await Promise.race([loaded, sleep(30000)]);
    await cdp.eval(`(async () => {
      const t0 = Date.now();
      for (;;) {
        if (window.state && state.indexed && state.nodes && state.nodes.length &&
            document.querySelector('.side') && document.querySelector('#graph')) return true;
        if (Date.now() - t0 > ${BOOT}) throw new Error('timeout waiting for boot');
        await new Promise(r => setTimeout(r, 80));
      }
    })()`);
    await cdp.eval('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))');
    await sleep(SETTLE);

    // ---- regions + computed styles of the body + document.fonts -------------
    const meta = await cdp.eval(`(() => {
      const p = (n) => { if (!n) return null; const b = n.getBoundingClientRect();
        return { x: Math.round(b.left), y: Math.round(b.top),
                 w: Math.round(b.width), h: Math.round(b.height) }; };
      const cs = getComputedStyle(document.documentElement);
      const bodyCs = getComputedStyle(document.body);
      const fontsReady = document.fonts ? document.fonts.status : 'n/a';
      const fams = [];
      try { document.fonts.forEach(f => fams.push({family:f.family, weight:f.weight, style:f.style, status:f.status})); } catch {}
      return {
        viewport: [innerWidth, innerHeight], dpr: devicePixelRatio,
        topbar: p(document.querySelector('.topbar')),
        layout: p(document.querySelector('.layout')),
        side: p(document.querySelector('.side')),
        canvas: p(document.querySelector('#canvasWrap')),
        htmlSans: cs.getPropertyValue('--sans').trim(),
        htmlMono: cs.getPropertyValue('--mono').trim(),
        bodyFont: bodyCs.fontFamily, bodySize: bodyCs.fontSize,
        bodyWeight: bodyCs.fontWeight, bodyLine: bodyCs.lineHeight,
        bodyColor: bodyCs.color, bodyBg: bodyCs.backgroundColor,
        fontsStatus: fontsReady, documentFonts: fams,
        nodes: (window.state && window.state.nodes || []).length,
        edges: (window.state && window.state.edges || []).length,
        status: (document.getElementById('statusLine') || {}).textContent,
      };
    })()`);

    // ---- every text node's box + the style that produced it -----------------
    const textRects = await cdp.eval(`(() => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      const out = [];
      let n;
      while ((n = walker.nextNode())) {
        const t = (n.nodeValue || '').replace(/\\s+/g, ' ').trim();
        if (!t) continue;
        const r = document.createRange();
        r.selectNodeContents(n);
        const b = r.getBoundingClientRect();
        if (b.width === 0 || b.height === 0) continue;
        const el = n.parentElement;
        const c = getComputedStyle(el);
        out.push({
          text: t.slice(0, 120),
          parent: el ? (el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? '.' + el.className.split(/\\s+/).slice(0,3).join('.') : '')) : '',
          x: Math.round(b.left), y: Math.round(b.top),
          w: Math.round(b.width), h: Math.round(b.height),
          fontFamily: c.fontFamily, fontSize: c.fontSize, fontWeight: c.fontWeight,
          lineHeight: c.lineHeight, color: c.color,
        });
      }
      return out;
    })()`);

    // ---- canvas text: recorded draw calls + measureText widths --------------
    // Re-render one clean frame so the recording is not polluted by the boot
    // animation, then read it back.
    await cdp.eval('window.__tp_ft = []; window.__tp_mt = []; true');
    await cdp.eval('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))');
    await sleep(SETTLE);
    const canvasRaw = await cdp.eval('({ft: window.__tp_ft || [], mt: window.__tp_mt || [], err: window.__tp_hookerr || null})');
    const dedup = (rows, key) => {
      const seen = new Map();
      for (const r of rows) {
        const k = key(r);
        if (!seen.has(k)) seen.set(k, r);
      }
      return [...seen.values()];
    };
    const canvasText = dedup(canvasRaw.ft,
      (r) => `${r.t}|${r.font}|${Math.round(r.x * 100)}|${Math.round(r.y * 100)}|${r.style}`);
    const canvasMetrics = dedup(canvasRaw.mt, (r) => `${r.t}|${r.font}`);
    const canvasFontHist = {};
    for (const r of canvasText) {
      canvasFontHist[r.font] = (canvasFontHist[r.font] || 0) + 1;
    }

    // ---- resolved platform font per text-bearing element --------------------
    const { root } = await cdp.send('DOM.getDocument', { depth: -1, pierce: true });
    const els = textElements(root);
    const resolved = [];
    for (const e of els) {
      let fonts = [];
      let style = null;
      try {
        const r = await cdp.send('CSS.getPlatformFontsForNode', { nodeId: e.nodeId });
        fonts = r.fonts || [];
      } catch (err) { fonts = [{ error: String(err.message || err) }]; }
      try {
        const r = await cdp.send('CSS.getComputedStyleForNode', { nodeId: e.nodeId });
        const pick = {};
        for (const p of r.computedStyle || []) {
          if (['font-family', 'font-size', 'font-weight', 'line-height', 'color'].includes(p.name)) {
            pick[p.name] = p.value;
          }
        }
        style = pick;
      } catch {}
      resolved.push({ tag: e.tag, text: e.text, fonts, style });
    }

    // ---- histogram of actually-resolved faces -------------------------------
    const hist = {};
    for (const e of resolved) {
      for (const f of e.fonts) {
        if (!f.familyName) continue;
        const key = `${f.familyName} | ${f.postScriptName || '?'}`;
        hist[key] = hist[key] || { familyName: f.familyName, postScriptName: f.postScriptName,
                                   glyphCount: 0, elements: 0, custom: !!f.isCustomFont };
        hist[key].glyphCount += f.glyphCount || 0;
        hist[key].elements += 1;
      }
    }

    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(OUT.replace(/\.json$/, '') + '.full.png', Buffer.from(shot.data, 'base64'));

    // Remove ALL glyph ink: DOM text via colour:transparent (keeps every
    // background/border), canvas text via the fillText/strokeText switch.
    // This is the reference with its text erased, so |ref - refNoText| isolates
    // the text pixels (DOM and canvas) without changing layout.
    await cdp.eval(`(() => {
      window.__tp_hide_text = true;
      const s = document.createElement('style'); s.id = '__tp_notext';
      s.textContent = '*{color:transparent !important;text-shadow:none !important;' +
        '-webkit-text-fill-color:transparent !important;caret-color:transparent !important}' +
        'input,textarea,select{color:transparent !important}' +
        '::placeholder{color:transparent !important}';
      document.head.appendChild(s);
      return true;
    })()`);
    await cdp.eval('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))');
    await sleep(300);
    const shot2 = await cdp.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(OUT.replace(/\.json$/, '') + '.notext.png', Buffer.from(shot2.data, 'base64'));

    report = {
      url: URL_, width: W, height: H,
      meta,
      fontHistogram: Object.values(hist).sort((a, b) => b.glyphCount - a.glyphCount),
      textElementCount: resolved.length,
      resolved,
      textRects,
      canvasHookError: canvasRaw.err,
      canvasFontHistogram: canvasFontHist,
      canvasText,
      canvasMetrics,
    };
    writeFileSync(OUT, JSON.stringify(report, null, 2));
    console.log(`textprobe: ${OUT}`);
    console.log('meta ' + JSON.stringify(meta));
    console.log('fontHistogram ' + JSON.stringify(report.fontHistogram));
    console.log('canvasFontHistogram ' + JSON.stringify(canvasFontHist));
    console.log('canvasText ' + JSON.stringify(canvasText.slice(0, 12)));
  } finally {
    try { if (ws) ws.close(); } catch {}
    try { chrome.kill('SIGKILL'); } catch {}
    if (chromeErr && process.env.CSVIEWER_SHOOT_DEBUG) process.stderr.write(chromeErr);
  }
}

main().catch((e) => { console.error('textprobe: ' + (e && e.stack || e)); process.exit(1); });
