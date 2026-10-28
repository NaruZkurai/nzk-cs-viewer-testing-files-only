#!/usr/bin/env node
/* domdump.mjs — read the ORIGINAL viewer's live DOM + geometry over CDP.
 *
 * The reference the css-shell crate has to reproduce is not index.html: it is
 * the document after the 32 scripts have run. This dumps that document's
 * skeleton and the bounding box of every element that matters, so the Rust DOM
 * builder (css-shell/src/dom.rs) can be checked against the real thing instead
 * of against a reading of the scripts.
 *
 * Read-only against ../../rust-only-csviewer. Boot the engine first:
 *   rust-only-csviewer/tools/with_serve.sh --repo /tmp/fid-corpus --root . \
 *     -- node css-shell/tools/domdump.mjs --out-json /tmp/dom.json --out-html /tmp/dom.html
 * `with_serve.sh` appends --url and --cdp-port.
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
const OUT_JSON = arg('--out-json', '/tmp/csviewer-dom.json');
const OUT_HTML = arg('--out-html', '/tmp/csviewer-dom.html');
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

// Elements whose geometry the CSS shell must reproduce.
const SELECTORS = [
  '.topbar', '.brand', '.brand-mark', '.rootpick', '#rootInput', '#browseBtn', '#indexBtn',
  '.searchwrap', '.mode-switch', '#modeGraph', '#modeFind', '#searchInput', '#statusLine',
  '#exportBtn', '#editBtn', '#blocksBtn', '#previewBtn', '#shortcutsBtn', '#newWinBtn',
  '.layout', '#canvasWrap', '#graph', '.canvas-tools', '#backBtn', '#upBtn', '#zoomInBtn',
  '#zoomOutBtn', '#fitBtn', '#wholeGraphBtn', '#resettleBtn', '#layoutState', '#notice',
  '.side', '.pane-grip', '.pane-grip-title', '.side .pane-float-btn', '.side .pane-resize',
  '#filtersSection', '#filtersHead', '#filtersCount', '#projectFilterBlock',
  '#projectFilters', '#nsSelect', '.filter-label', '#kindFilters', '.facet',
  '.facet .swatch', '.facet .facet-name', '.facet .facet-count',
  '#edgeFilters', '.edgekey', '.edgekey .bars', '.check',
  '#resetFilters', '#pruneHidden', '.filter-group', '.range', '.range-name', '.range-val',
  '.hint', '#pubSection', '#strSection', '#chainSection', '#problemsSection',
  '#problemsHead', '#problemsCount', '.cs-problems-note', '#problemsSearch', '#problemsList',
];

const SKELETON = `(() => {
  function skel(n, depth) {
    if (n.nodeType === 3) {
      const t = n.textContent;
      return t.trim() ? { t: t.trim().slice(0, 80) } : null;
    }
    if (n.nodeType !== 1) return null;
    const o = { tag: n.tagName.toLowerCase() };
    if (n.id) o.id = n.id;
    if (n.className && typeof n.className === 'string') o.cls = n.className;
    for (const a of n.attributes || []) {
      if (a.name === 'class' || a.name === 'id') continue;
      if (a.name === 'value' || a.name === 'placeholder' || a.name === 'style' ||
          a.name === 'data-kind' || a.name === 'data-edge' || a.name === 'checked' ||
          a.name === 'type') o[a.name] = String(a.value).slice(0, 120);
    }
    if (n.tagName === 'INPUT') o.value = n.value;
    if (n.tagName === 'SELECT') o.value = n.value;
    const kids = [];
    for (const c of n.childNodes) { const k = skel(c, depth + 1); if (k) kids.push(k); }
    if (kids.length) o.kids = kids;
    if (depth > 12) return o;
    return o;
  }
  return skel(document.querySelector('.side'), 0);
})()`;

async function main() {
  const profile = mkdtempSync(join(tmpdir(), 'csviewer-domdump-'));
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
        if (window.state && state.indexed && document.querySelector('.side') &&
            document.querySelector('#graph')) return true;
        if (Date.now() - t0 > ${BOOT}) throw new Error('timeout waiting for boot');
        await new Promise(r => setTimeout(r, 80));
      }
    })()`);
    await cdp.eval('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))');
    await sleep(SETTLE);

    const report = await cdp.eval(`(() => {
      const sels = ${JSON.stringify(SELECTORS)};
      const boxes = {};
      for (const s of sels) {
        const n = document.querySelector(s);
        if (!n) { boxes[s] = null; continue; }
        const b = n.getBoundingClientRect();
        const cs = getComputedStyle(n);
        boxes[s] = {
          x: +b.left.toFixed(2), y: +b.top.toFixed(2),
          w: +b.width.toFixed(2), h: +b.height.toFixed(2),
          color: cs.color, bg: cs.backgroundColor, font: cs.font,
          display: cs.display, pad: cs.padding, mar: cs.margin,
        };
      }
      const all = {};
      for (const s of ['.side .section', '.side .section-head', '.side .filter-label',
                        '.side .facet', '.side .edgekey', '.side .check', '.side .range',
                        '.canvas-tools .btn', '.topbar .btn']) {
        all[s] = [...document.querySelectorAll(s)].map(n => {
          const b = n.getBoundingClientRect();
          return { x: +b.left.toFixed(2), y: +b.top.toFixed(2),
                   w: +b.width.toFixed(2), h: +b.height.toFixed(2),
                   text: (n.textContent || '').trim().slice(0, 60) };
        });
      }
      return {
        viewport: [innerWidth, innerHeight],
        htmlAttrs: [...document.documentElement.attributes].map(a => a.name + '=' + a.value),
        rootVars: {
          bg: getComputedStyle(document.documentElement).getPropertyValue('--bg'),
          topbarH: getComputedStyle(document.documentElement).getPropertyValue('--topbar-h'),
          sideW: getComputedStyle(document.documentElement).getPropertyValue('--side-w'),
        },
        boxes, all,
        sideSkeleton: ${SKELETON},
        statusText: (document.getElementById('statusLine') || {}).textContent,
        rootValue: (document.getElementById('rootInput') || {}).value,
      };
    })()`);

    const html = await cdp.eval('document.documentElement.outerHTML');
    writeFileSync(OUT_JSON, JSON.stringify(report, null, 2));
    if (OUT_HTML) writeFileSync(OUT_HTML, html);
    console.log('domdump ' + OUT_JSON + ' (' + html.length + ' bytes of html)');
  } finally {
    try { if (ws) ws.close(); } catch {}
    try { chrome.kill('SIGKILL'); } catch {}
    if (chromeErr && process.env.CSVIEWER_DOMDUMP_DEBUG) process.stderr.write(chromeErr);
  }
}

main().catch((e) => { console.error('domdump: ' + (e && e.stack || e)); process.exit(1); });
