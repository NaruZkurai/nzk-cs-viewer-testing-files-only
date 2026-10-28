#!/usr/bin/env node
/* fontprobe.mjs — FACE-level facts about the live original's text, independent
 * of textprobe.mjs.
 *
 * The question this answers is "which face did Chromium actually resolve", not
 * "what font-family did the CSS request". For every key surface it reports the
 * computed font-* properties (including letter-spacing / word-spacing /
 * font-feature-settings, which textprobe does not collect) and the ACTUAL
 * platform font chosen (`CSS.getPlatformFontsForNode`).
 *
 * Canvas labels are drawn by JS (`ctx.font = '<px> ui-monospace, Menlo,
 * Consolas, monospace'` in render.js) and are invisible to
 * `getPlatformFontsForNode`. So it also probes a hidden DOM element whose
 * `font` shorthand is byte-identical to the canvas one: font matching for the
 * 2D context and for the DOM element consult the same fontconfig, so the probe
 * element's resolved face is the honest proxy, and that is stated, not hidden.
 *
 * Read-only against the original. Boot it with with_serve.sh:
 *   rust-only-csviewer/tools/with_serve.sh --repo /tmp/tp-corpus --root . -- \
 *     node native-csviewer/tools/fontprobe.mjs --out /tmp/tp/fontprobe.json \
 *     --width 1600 --height 1000
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
const OUT = arg('--out', '/tmp/tp/fontprobe.json');
const W = Number(arg('--width', '1600'));
const H = Number(arg('--height', '1000'));
const BOOT = Number(arg('--boot-ms', '90000'));
const SETTLE = Number(arg('--settle-ms', '1200'));

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
    this.ws = ws; this.id = 0; this.pending = new Map();
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      }
    });
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

const SURFACES = [
  ['.brand', 'brand'],
  ['.brand-mark', 'brand-mark'],
  ['.btn', 'button'],
  ['.btn-primary', 'button-primary'],
  ['.linkbtn', 'link-button'],
  ['.mode', 'mode-toggle'],
  ['.status', 'status-readout'],
  ['.mini-input', 'text-input'],
  ['.section-head', 'sidebar-section-head'],
  ['.filter-label', 'sidebar-filter-label'],
  ['.facet-name', 'sidebar-facet-name'],
  ['.facet-count', 'sidebar-facet-count'],
  ['.check', 'sidebar-check'],
  ['.range-name', 'sidebar-range-name'],
  ['.range-val', 'sidebar-range-value'],
  ['.pane-grip-title', 'pane-grip-title'],
  ['.hint', 'hint-paragraph'],
  ['.notice', 'notice'],
  ['.edgekey', 'edge-key'],
  ['.scount', 'count-badge'],
  ['.list-empty', 'list-empty'],
  ['.cs-problems-note', 'problems-note'],
  ['.drawer-title', 'drawer-title'],
  ['.find-hint', 'find-hint'],
  ['.loading-text', 'loading-text'],
];

// The exact canvas font shorthand strings from frontend/static/render.js.
const CANVAS_FONTS = [
  ['chip-label-11px', '11px ui-monospace, Menlo, Consolas, monospace'],
  ['edge-label-10px', '10px ui-monospace, Menlo, Consolas, monospace'],
];

const STYLE_PROPS = [
  'font-family', 'font-size', 'font-weight', 'font-style', 'font-stretch',
  'line-height', 'letter-spacing', 'word-spacing', 'text-rendering',
  'font-feature-settings', 'font-variation-settings', 'white-space',
  'text-transform', 'color',
];

async function main() {
  const profile = mkdtempSync(join(tmpdir(), 'csviewer-fp-'));
  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    '--force-device-scale-factor=1', '--hide-scrollbars',
    `--user-data-dir=${profile}`, `--remote-debugging-port=${PORT}`, 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });
  let chromeErr = '';
  chrome.stderr.on('data', (d) => { chromeErr += d.toString(); });

  const deadline = Date.now() + BOOT;
  let ws, report;
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
    const cdp = new CDP(ws);
    await cdp.send('Runtime.enable');
    await cdp.send('Page.enable');
    await cdp.send('DOM.enable');
    await cdp.send('CSS.enable');
    await cdp.send('Emulation.setDeviceMetricsOverride',
      { width: W, height: H, deviceScaleFactor: 1, mobile: false });

    const loaded = new Promise((res) => {
      cdp.send('Page.enable');
      const h = (msg) => { if (msg.method === 'Page.loadEventFired') res(); };
      // the CDP wrapper drops events; poll instead
      setTimeout(res, 1500);
    });
    await cdp.send('Page.navigate', { url: URL_ });
    await Promise.race([loaded, sleep(1500)]);
    await cdp.eval(`(async () => {
      const t0 = Date.now();
      for (;;) {
        if (window.state && state.indexed && document.querySelector('#graph')) return true;
        if (Date.now() - t0 > ${BOOT}) throw new Error('timeout waiting for boot');
        await new Promise(r => setTimeout(r, 80));
      }
    })()`);
    await cdp.eval('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))');
    await sleep(SETTLE);

    const { root } = await cdp.send('DOM.getDocument', { depth: -1, pierce: true });
    const surfaces = [];
    for (const [selector, label] of SURFACES) {
      const style = await cdp.eval(`(() => {
        const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) return null;
        const c = getComputedStyle(el);
        const b = el.getBoundingClientRect();
        const out = { rect: { x: Math.round(b.left), y: Math.round(b.top),
                              w: Math.round(b.width), h: Math.round(b.height) } };
        for (const p of ${JSON.stringify(STYLE_PROPS)}) out[p] = c.getPropertyValue(p);
        return out;
      })()`);
      let fonts = [];
      let errors = [];
      if (style) {
        const q = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector });
        if (q.nodeId) {
          const r = await cdp.send('CSS.getPlatformFontsForNode', { nodeId: q.nodeId });
          fonts = r.fonts || [];
        } else {
          errors.push('no nodeId for selector');
        }
      }
      surfaces.push({ selector, label, style, fonts, errors });
    }

    // Canvas font shorthand proxy: hidden spans, byte-identical font strings.
    // Append BOTH probes first, then take one fresh DOM snapshot, so the CDP
    // node lookups cannot race the DOM mutation.
    await cdp.eval(`(() => {
      for (const [i, font] of ${JSON.stringify(CANVAS_FONTS)}.entries()) {
        const s = document.createElement('span');
        s.id = '__fp_canvas_' + i;
        s.style.font = font[1];
        s.style.position = 'absolute';
        s.style.left = '-9999px';
        s.textContent = 'NodeName M0 0';
        document.body.appendChild(s);
      }
      return true;
    })()`);
    const fresh = await cdp.send('DOM.getDocument', { depth: -1, pierce: true });
    const canvasProbes = [];
    for (const [i, [label, font]] of CANVAS_FONTS.entries()) {
      const info = await cdp.eval(`(() => {
        const s = document.getElementById('__fp_canvas_${i}');
        const c = getComputedStyle(s);
        return { fontFamily: c.fontFamily, fontSize: c.fontSize,
                 fontWeight: c.fontWeight, lineHeight: c.lineHeight,
                 letterSpacing: c.letterSpacing };
      })()`);
      let fonts = [];
      const q = await cdp.send('DOM.querySelector',
        { nodeId: fresh.root.nodeId, selector: '#__fp_canvas_' + i });
      if (q.nodeId) {
        const r = await cdp.send('CSS.getPlatformFontsForNode', { nodeId: q.nodeId });
        fonts = r.fonts || [];
      } else {
        fonts = [{ error: 'node not in DOM snapshot' }];
      }
      canvasProbes.push({ label, font_shorthand: font, computed: info, fonts });
    }
    await cdp.eval(`(() => { for (let i = 0; i < 2; i++)
      document.getElementById('__fp_canvas_' + i)?.remove(); return true; })()`);

    // Face histogram of the whole page, for completeness.
    const hist = {};
    for (const s of surfaces) {
      for (const f of s.fonts) {
        if (!f.familyName) continue;
        const key = `${f.familyName} | ${f.postScriptName || '?'}`;
        hist[key] = hist[key] || { familyName: f.familyName, postScriptName: f.postScriptName,
                                   custom: !!f.isCustomFont, glyphCount: 0, surfaces: 0 };
        hist[key].glyphCount += f.glyphCount || 0;
        hist[key].surfaces += 1;
      }
    }

    const fontStatus = await cdp.eval(`(() => {
      const list = [];
      try { document.fonts.forEach(f => list.push({ family: f.family, weight: f.weight,
        style: f.style, status: f.status })); } catch {}
      return { status: document.fonts ? document.fonts.status : 'n/a',
               ready: document.fonts ? document.fonts.ready.then(() => true) : false,
               list };
    })()`);

    report = {
      url: URL_, width: W, height: H,
      fontStatus,
      faceHistogram: Object.values(hist).sort((a, b) => b.glyphCount - a.glyphCount),
      surfaces,
      canvasProbes,
    };
    writeFileSync(OUT, JSON.stringify(report, null, 2));
    console.log(`fontprobe: ${OUT}`);
    for (const s of surfaces) {
      const st = s.style || {};
      const fams = s.fonts.map((f) => `${f.familyName}/${f.postScriptName}`).join(', ') || '(none)';
      console.log(`  ${s.label.padEnd(24)} ${String(st['font-size'] || '-').padStart(8)} ` +
                  `w=${String(st['font-weight'] || '-').padStart(3)} ` +
                  `lh=${String(st['line-height'] || '-').padStart(8)} ` +
                  `ls=${String(st['letter-spacing'] || '-').padStart(7)} -> ${fams}`);
    }
    for (const p of canvasProbes) {
      const fams = p.fonts.map((f) => `${f.familyName}/${f.postScriptName}`).join(', ') || '(none)';
      console.log(`  canvas:${p.label.padEnd(18)} -> ${fams}`);
    }
    console.log('faceHistogram ' + JSON.stringify(report.faceHistogram));
  } finally {
    try { if (ws) ws.close(); } catch {}
    try { chrome.kill('SIGKILL'); } catch {}
    if (chromeErr && process.env.CSVIEWER_SHOOT_DEBUG) process.stderr.write(chromeErr);
  }
}

main().catch((e) => { console.error('fontprobe: ' + (e && e.stack || e)); process.exit(1); });
