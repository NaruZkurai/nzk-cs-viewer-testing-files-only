#!/usr/bin/env node
/* shoot.mjs — capture the ORIGINAL viewer's page at an exact viewport size.
 *
 * Read-only against ../rust-only-csviewer (never writes there). Boot the real
 * engine first, e.g.:
 *   rust-only-csviewer/tools/with_serve.sh --repo /tmp/fid-corpus --root . \
 *     -- node native-csviewer/tools/shoot.mjs --out /tmp/ref.png --width 1600 --height 1000
 * `with_serve.sh` appends --url and --cdp-port for us.
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
const OUT = arg('--out', '/tmp/csviewer-shot.png');
const W = Number(arg('--width', '1600'));
const H = Number(arg('--height', '1000'));
const BOOT = Number(arg('--boot-ms', '90000'));
const SETTLE = Number(arg('--settle-ms', '1500'));
// The original's default camera depends on the frame the layout went idle
// (updateAutoFit snaps while easePositions is still moving nodes). Clicking Fit
// calls fitView() on the settled positions, which IS deterministic. On by
// default; --click-fit 0 captures the raw default view instead.
const CLICK_FIT = arg('--click-fit', '1') !== '0';

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

async function main() {
  const profile = mkdtempSync(join(tmpdir(), 'csviewer-shoot-'));
  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    '--force-device-scale-factor=1', '--hide-scrollbars',
    `--user-data-dir=${profile}`, `--remote-debugging-port=${PORT}`, 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });
  let chromeErr = '';
  chrome.stderr.on('data', (d) => { chromeErr += d.toString(); });

  const deadline = Date.now() + BOOT;
  let ws, cdp;
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
    if (CLICK_FIT) {
      // #fitBtn -> layout.js fitView() -> applyFit(computeFit()) on settled pos.
      await cdp.eval(`(() => {
        const b = document.getElementById('fitBtn');
        if (b) { b.click(); return true; }
        if (typeof fitView === 'function') { fitView(); return true; }
        return false;
      })()`);
      await cdp.eval('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))');
      await sleep(400);
    }

    const info = await cdp.eval(`(() => {
      const p = (n) => { const b = n.getBoundingClientRect();
        return { x: Math.round(b.left), y: Math.round(b.top),
                 w: Math.round(b.width), h: Math.round(b.height) }; };
      const g = window.state || {};
      const pos = [];
      try { (g.pos || new Map()).forEach((q, id) => pos.push([id, q.x, q.y, q.tx, q.ty])); } catch (e) {}
      return {
        viewport: [innerWidth, innerHeight],
        topbar: p(document.querySelector('.topbar')),
        layout: p(document.querySelector('.layout')),
        side: p(document.querySelector('.side')),
        canvas: p(document.querySelector('#canvasWrap')),
        nodes: (g.nodes || []).length, edges: (g.edges || []).length,
        status: (document.getElementById('statusLine') || {}).textContent,
        dpr: devicePixelRatio,
        topbarH: getComputedStyle(document.documentElement).getPropertyValue('--topbar-h'),
        sideW: getComputedStyle(document.documentElement).getPropertyValue('--side-w'),
        camera: g.camera, autoFit: !!g.autoFit,
        layoutMode: g.layout && g.layout.mode,
        viewNodes: (g.view && g.view.nodes ? g.view.nodes.length : null),
        pos,
      };
    })()`);
    console.log('meta ' + JSON.stringify(info));

    const r = await cdp.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(OUT, Buffer.from(r.data, 'base64'));
    console.log(`shot ${OUT} ${W}x${H}`);
  } finally {
    try { if (ws) ws.close(); } catch {}
    try { chrome.kill('SIGKILL'); } catch {}
    if (chromeErr && process.env.CSVIEWER_SHOOT_DEBUG) process.stderr.write(chromeErr);
  }
}

main().catch((e) => { console.error('shoot: ' + (e && e.stack || e)); process.exit(1); });
