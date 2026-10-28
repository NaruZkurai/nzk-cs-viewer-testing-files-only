#!/usr/bin/env node
/* wrapprobe.mjs — exact line boxes and wrap points for the reference's
 * wrapping text surfaces, measured on the live original over CDP.
 *
 * For each selector it walks the element's text nodes, asks the browser for the
 * per-line client rects of a Range over each node, and then recovers the line's
 * exact substring by growing the range one character at a time and watching the
 * line-top change. That gives the wrap TEXT, not an estimate from height /
 * line-height.
 *
 * Read-only. Boot with with_serve.sh:
 *   rust-only-csviewer/tools/with_serve.sh --repo /tmp/tp-corpus --root . -- \
 *     node native-csviewer/tools/wrapprobe.mjs --out /tmp/tp/wrapprobe.json
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
const OUT = arg('--out', '/tmp/tp/wrapprobe.json');
const W = Number(arg('--width', '1600'));
const H = Number(arg('--height', '1000'));
const BOOT = Number(arg('--boot-ms', '90000'));
const SETTLE = Number(arg('--settle-ms', '1200'));

const SELECTORS = [
  ['p.hint', 'hint paragraph (sans 11.5/18.4)'],
  ['div.cs-problems-note', 'parser-warnings note (the one nzk_ui wraps 3 vs 2)'],
  ['div.notice', 'view notice'],
  ['div.status', 'status readout (single line)'],
  ['span.facet-name', 'sidebar facet name'],
  ['div.list-empty', 'sidebar empty list'],
];

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

const WALK = `(() => {
  const out = [];
  const walk = (n) => {
    if (n.nodeType === 3) { if (n.nodeValue && n.nodeValue.trim()) out.push(n); return; }
    if (n.nodeType === 1) for (const c of n.childNodes) walk(c);
  };
  return { nodes: out };
})()`;

async function measure(cdp, selector) {
  return cdp.eval(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return { selector: ${JSON.stringify(selector)}, found: false };
    const c = getComputedStyle(el);
    const b = el.getBoundingClientRect();
    const out = {
      selector: ${JSON.stringify(selector)},
      found: true,
      rect: { x: Math.round(b.left), y: Math.round(b.top), w: Math.round(b.width), h: Math.round(b.height) },
      style: { fontSize: c.fontSize, lineHeight: c.lineHeight, whiteSpace: c.whiteSpace,
               letterSpacing: c.letterSpacing, fontFamily: c.fontFamily, overflowWrap: c.overflowWrap },
      lines: [],
    };
    const nodes = [];
    (function walk(n) {
      if (n.nodeType === 3) { if (n.nodeValue && n.nodeValue.trim()) nodes.push(n); return; }
      if (n.nodeType === 1) for (const ch of n.childNodes) walk(ch);
    })(el);
    for (const node of nodes) {
      const len = node.nodeValue.length;
      const r = document.createRange();
      // Per-line client rects first: line count and box geometry.
      r.selectNodeContents(node);
      const rects = Array.from(r.getClientRects()).filter(x => x.height > 0);
      // Exact per-line text: grow the range until a second line box appears;
      // the character that opens line 2 ends line 1.
      let start = 0;
      const lineTexts = [];
      while (start < len) {
        r.setStart(node, start);
        r.setEnd(node, start + 1);
        if (!Array.from(r.getClientRects()).filter(x => x.height > 0).length) break;
        let end = start + 1;
        while (end < len) {
          r.setStart(node, start);
          r.setEnd(node, end + 1);
          const rs = Array.from(r.getClientRects()).filter(x => x.height > 0);
          if (rs.length > 1) break;
          end++;
        }
        lineTexts.push(node.nodeValue.slice(start, end));
        start = end;
        while (start < len && /\\s/.test(node.nodeValue[start])) start++;
      }
      out.lines.push({
        text_node: node.nodeValue.replace(/\\s+/g, ' ').trim().slice(0, 200),
        client_rects: rects.map(x => ({ x: Math.round(x.left), y: Math.round(x.top),
                                        w: Math.round(x.width), h: Math.round(x.height) })),
        line_count: rects.length,
        line_texts: lineTexts.map(t => t.replace(/\\s+/g, ' ').trim()),
      });
    }
    out.line_count = out.lines.reduce((m, l) => Math.max(m, l.line_count), 0);
    return out;
  })()`);
}

async function main() {
  const profile = mkdtempSync(join(tmpdir(), 'csviewer-wp-'));
  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    '--force-device-scale-factor=1', '--hide-scrollbars',
    `--user-data-dir=${profile}`, `--remote-debugging-port=${PORT}`, 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });
  let ws;
  try {
    await waitForDevtools(PORT, Date.now() + BOOT);
    const create = await fetch(
      `http://127.0.0.1:${PORT}/json/new?${encodeURIComponent('about:blank')}`,
      { method: 'PUT' });
    const target = await create.json();
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', () => rej(new Error('cdp websocket error')), { once: true });
    });
    const cdp = new CDP(ws);
    await cdp.send('Runtime.enable');
    await cdp.send('Page.enable');
    await cdp.send('Emulation.setDeviceMetricsOverride',
      { width: W, height: H, deviceScaleFactor: 1, mobile: false });
    await cdp.send('Page.navigate', { url: URL_ });
    await sleep(1500);
    await cdp.eval(`(async () => {
      const t0 = Date.now();
      for (;;) {
        if (window.state && state.indexed && document.querySelector('#graph')) return true;
        if (Date.now() - t0 > ${BOOT}) throw new Error('timeout waiting for boot');
        await new Promise(r => setTimeout(r, 80));
      }
    })()`);
    await sleep(SETTLE);

    const surfaces = [];
    for (const [sel, label] of SELECTORS) {
      const m = await measure(cdp, sel);
      m.label = label;
      surfaces.push(m);
    }
    const report = { url: URL_, width: W, height: H, surfaces };
    writeFileSync(OUT, JSON.stringify(report, null, 2));
    for (const s of surfaces) {
      if (!s.found) { console.log(`  ${s.selector}: NOT FOUND`); continue; }
      console.log(`  ${s.selector} (${s.style.fontSize}/${s.style.lineHeight}, ` +
                  `ls=${s.style.letterSpacing}) lines=${s.line_count} rect=${JSON.stringify(s.rect)}`);
      for (const l of s.lines) {
        console.log(`      line_count=${l.line_count} boxes=${JSON.stringify(l.client_rects)}`);
        for (const t of l.line_texts) console.log(`        | ${t}`);
      }
    }
    console.log(`wrapprobe: ${OUT}`);
  } finally {
    try { if (ws) ws.close(); } catch {}
    try { chrome.kill('SIGKILL'); } catch {}
  }
}

main().catch((e) => { console.error('wrapprobe: ' + (e && e.stack || e)); process.exit(1); });
