// Usage: PLAYWRIGHT_CHANNEL=chrome node scripts/benchmark_browser_memory.cjs board.PcbDoc [output.json]
// PCB_BENCH_ROOT can select a baseline checkout. Compare identical boards/browsers.
const { chromium } = require('playwright');
const { spawn, execFileSync } = require('node:child_process');
const { once } = require('node:events');
const { resolve } = require('node:path');
const { writeFileSync } = require('node:fs');
async function main() {
  if (!process.argv[2]) throw new Error('Provide a PCB document path');
  const path = resolve(process.argv[2]), root = process.env.PCB_BENCH_ROOT || resolve(__dirname, '..');
  const server = spawn(resolve(root, '.venv/bin/python'), ['-B', '-u', resolve(root, 'vcad.py'), '--no-open'], { cwd: root });
  let browser;
  try {
    const origin = await new Promise((done, reject) => {
      let text = ''; server.stdout.on('data', chunk => {
        text += chunk; const match = text.match(/http:\/\/127\.0\.0\.1:\d+\//); if (match) done(match[0]);
      });
      server.on('error', reject); server.on('exit', code => reject(new Error('Viewer exited: ' + code)));
      server.stderr.on('data', chunk => process.stderr.write(chunk));
    });
    browser = await chromium.launch({ headless: true,
      ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } }), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {
      if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) errors.push(message.text());
    });
    page.on('response', response => {
      if (response.status() >= 400 && new URL(response.url()).pathname !== '/favicon.ico') errors.push(response.status() + ' ' + response.url());
    });
    await page.goto(origin);
    await page.evaluate(async () => {
      const { Board3D } = await import('/static/viewer-3d.js'), show = Board3D.prototype.show;
      Board3D.prototype.show = function (...args) { window.benchmarkViewer = this; return show.apply(this, args); };
    });
    const cdp = await page.context().newCDPSession(page), browserCDP = await browser.newBrowserCDPSession();
    async function memory() {
      await cdp.send('HeapProfiler.collectGarbage');
      const heap = await cdp.send('Runtime.getHeapUsage'), dom = await cdp.send('Memory.getDOMCounters');
      let processes = null;
      // RSS includes shared pages and is an observation, not an additive RAM
      // accounting model. Keep each process category separate from V8 metrics.
      try {
        const { processInfo } = await browserCDP.send('SystemInfo.getProcessInfo');
        const rss = execFileSync('ps', ['-o', 'pid=,rss=', '-p', processInfo.map(p => p.id).join(',')], { encoding: 'utf8' });
        const rows = new Map(rss.trim().split('\n').map(row => row.trim().split(/\s+/).map(Number)));
        processes = processInfo.filter(p => rows.has(p.id)).map(p => ({ type: p.type, rssBytes: rows.get(p.id) * 1024 }));
      } catch { /* Process RSS is optional on platforms without ps/CDP access. */ }
      return { heap, dom, processes };
    }
    const begin = Date.now(); await page.locator('#file').setInputFiles(path);
    await page.waitForFunction(() => document.querySelector('#view.gpu-layout') && document.querySelector('#busy').style.display === 'none',
      undefined, { timeout: 180000 });
    const load2Dms = Date.now() - begin, memory2D = await memory();
    async function navigation(three) {
      return page.evaluate(async three => {
        const view = document.querySelector('#view'), rect = view.getBoundingClientRect(), intervals = [], cpu = [];
        const start = performance.now(); let last = start, events = 0, timer;
        if (!three) timer = setInterval(() => {
          const elapsed = performance.now() - start, zoom = elapsed < 2000 || elapsed > 4000;
          view.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, ctrlKey: zoom,
            clientX: rect.x + rect.width * .55, clientY: rect.y + rect.height * .45,
            deltaX: zoom ? 0 : 3, deltaY: zoom ? (elapsed < 2000 ? -1.5 : 1.5) : 1 })); events++;
        }, 16);
        await new Promise(done => requestAnimationFrame(function frame(now) {
          const delta = now - last; intervals.push(delta); last = now;
          if (three) {
            const before = performance.now(); window.benchmarkViewer.rotateStep(1, 0, Math.min(delta, 50) * .06);
            cpu.push(performance.now() - before); events++;
          }
          if (performance.now() - start < 6000) requestAnimationFrame(frame); else done();
        }));
        clearInterval(timer); intervals.sort((a,b)=>a-b); cpu.sort((a,b)=>a-b);
        return { events, rafMedianMs: intervals[Math.floor(intervals.length*.5)], rafP95Ms: intervals[Math.floor(intervals.length*.95)],
          rafMaxMs: intervals.at(-1), renderCpuMedianMs: cpu.length ? cpu[Math.floor(cpu.length*.5)] : null };
      }, three);
    }
    const navigation2D = await navigation(false);
    await page.waitForTimeout(300);
    const start3D = Date.now(); await page.keyboard.press('3');
    await page.waitForFunction(() => window.benchmarkViewer?.ready, undefined, { timeout: 180000 });
    const load3Dms = Date.now() - start3D, memory3D = await memory(), navigation3D = await navigation(true);
    const render = await page.evaluate(() => ({ ...window.benchmarkViewer.renderer.info.render }));
    await page.keyboard.press('2'); const startWarm = Date.now(); await page.keyboard.press('3');
    await page.waitForFunction(() => window.benchmarkViewer.ready); const warm3Dms = Date.now() - startWarm;
    const gpu = await page.evaluate(() => {
      const gl = window.benchmarkViewer.renderer.getContext(), debug = gl.getExtension('WEBGL_debug_renderer_info');
      return debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : null;
    });
    const cached = await memory();
    await page.getByRole('button', { name: /^Close / }).click(); const closed = await memory();
    const result = { document: path, browser: browser.version(), gpu, load2Dms, load3Dms, warm3Dms,
      memory2D, memory3D, cached, closed, navigation2D, navigation3D, render, errors };
    if (process.argv[3]) writeFileSync(process.argv[3], JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result, null, 2));
    if (errors.length) throw new Error('Browser reported errors');
  } finally {
    await browser?.close();
    if (server.exitCode === null) { const exited = once(server, 'exit'); server.kill(); await exited; }
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
