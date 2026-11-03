// Usage: PLAYWRIGHT_CHANNEL=chrome node scripts/benchmark_browser_layout.cjs board.PcbDoc [output.json]
// Observes rAF cadence during the same 6 s timed wheel/pinch sequence in both
// renderers. rAF is browser scheduling cadence, not GPU execution/display FPS.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { resolve } = require('node:path');
const { writeFileSync } = require('node:fs');
const { once } = require('node:events');
async function main() {
  const path = resolve(process.argv[2]), output = process.argv[3];
  const server = spawn(resolve(__dirname, '../.venv/bin/python'), ['-B', '-u', resolve(__dirname, '../cadview.py'), '--no-open']);
  let browser;
  try {
    const origin = await new Promise((done, reject) => {
      let text = ''; server.stdout.on('data', chunk => { text += chunk; const match = text.match(/http:\/\/127\.0\.0\.1:\d+\//); if (match) done(match[0]); });
      server.on('error', reject); server.stderr.on('data', chunk => process.stderr.write(chunk));
    });
    browser = await chromium.launch({ headless: !process.env.PCB_BENCH_HEADED,
      ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
    const results = { document: path, browser: browser.version(), cases: [] };
    for (const gpu of [false, true]) {
      const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      if (!gpu) await page.addInitScript(() => {
        const original = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function (kind, ...args) { return kind === 'webgl2' ? null : original.call(this, kind, ...args); };
      });
      await page.goto(origin); const load = Date.now();
      await page.locator('#file').setInputFiles(path);
      await page.waitForFunction(() => document.querySelector('#view > svg'), undefined, { timeout: 180000 });
      const svg_setup_ms = Date.now() - load;
      await page.waitForFunction(() => document.querySelector('#busy').style.display === 'none', undefined, { timeout: 180000 });
      if (gpu) await page.waitForFunction(() => document.querySelector('#view.gpu-layout') || document.querySelector('#renderer-status').textContent.startsWith('SVG:'), undefined, { timeout: 180000 });
      const prepared_ms = Date.now() - load;
      const renderer = await page.locator('#renderer-status').textContent();
      if (gpu && renderer !== 'WebGL 2') throw new Error(renderer);
      await page.waitForTimeout(500);
      const cadence = await page.evaluate(async () => {
        const view = document.querySelector('#view'), rect = view.getBoundingClientRect();
        const begin = performance.now(), intervals = []; let last, frames = 0, events = 0;
        const longtasks = []; const observer = new PerformanceObserver(list => longtasks.push(...list.getEntries().map(e => e.duration)));
        observer.observe({ type: 'longtask', buffered: false });
        const timer = setInterval(() => {
          const elapsed = performance.now() - begin;
          // Zoom in, then pan, then zoom back out. Pass through the actual
          // listener and requestAnimationFrame coalescing, not a GL-only loop.
          const zoom = elapsed < 2000 || elapsed > 4000;
          view.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true,
            clientX: rect.x + rect.width * .55, clientY: rect.y + rect.height * .45,
            ctrlKey: zoom, deltaX: zoom ? 0 : 3, deltaY: zoom ? (elapsed < 2000 ? -1.5 : 1.5) : 1 })); events++;
        }, 16);
        await new Promise(done => requestAnimationFrame(function frame(now) {
          if (last !== undefined) intervals.push(now - last); last = now; frames++;
          if (performance.now() - begin < 6000) requestAnimationFrame(frame); else done();
        }));
        clearInterval(timer); longtasks.push(...observer.takeRecords().map(e => e.duration)); observer.disconnect(); intervals.sort((a, b) => a - b);
        const canvas = document.querySelector('.layout-webgl'), gl = canvas?.getContext('webgl2'), debug = gl?.getExtension('WEBGL_debug_renderer_info');
        return { elapsed_ms: performance.now() - begin, frames, input_events: events,
          raf_median_ms: intervals[Math.floor(intervals.length * .5)], raf_p95_ms: intervals[Math.floor(intervals.length * .95)],
          raf_max_ms: intervals.at(-1), longtasks_ms: longtasks,
          geometry: canvas ? { ...canvas.dataset } : null,
          gpu: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : null };
      });
      // Also stop at a close zoom and let captions finish; deferring them must
      // not merely hide a large synchronous pause at the end of a gesture.
      const settled = await page.evaluate(async () => {
        const view = document.querySelector('#view'), rect = view.getBoundingClientRect(), start = performance.now();
        view.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, ctrlKey: true,
          clientX: rect.x + rect.width / 2, clientY: rect.y + rect.height / 2, deltaY: -100 }));
        const intervals = []; let previous = start;
        await new Promise(done => requestAnimationFrame(function tick() {
          const time = performance.now();
          if (previous !== undefined) intervals.push(time - previous); previous = time;
          if (performance.now() - start < 1500 || document.querySelector('#net-labels').dataset.pending === '1') requestAnimationFrame(tick); else done();
        }));
        const query = document.querySelector('#net-query'), before = performance.now();
        query.value = 'GND'; query.dispatchEvent(new Event('input', { bubbles: true }));
        return { elapsed_ms: performance.now() - start, raf_max_ms: Math.max(...intervals),
          net_selection_handler_ms: performance.now() - before, captions: document.querySelectorAll('.copper-label').length };
      });
      const cdp = await page.context().newCDPSession(page);
      const heap = await cdp.send('Runtime.getHeapUsage'), dom = await cdp.send('Memory.getDOMCounters');
      await page.screenshot({ path: `/private/tmp/pcb-browser-${gpu ? 'webgl' : 'svg'}.png` });
      results.cases.push({ renderer, svg_setup_ms, prepared_ms, cadence, settled, heap, dom, errors });
      console.log(JSON.stringify(results.cases.at(-1), null, 2));
      await page.close();
    }
    if (output) writeFileSync(output, JSON.stringify(results, null, 2) + '\n');
  } finally {
    await browser?.close();
    if (server.exitCode === null) { const exited = once(server, 'exit'); server.kill(); await exited; }
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
