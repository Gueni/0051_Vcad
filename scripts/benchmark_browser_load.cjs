// Usage: PLAYWRIGHT_CHANNEL=chrome node scripts/benchmark_browser_load.cjs board.PcbDoc [output.json]
// Render timings measure CPU submission, not completed GPU work or display FPS.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { resolve } = require('node:path');
const { writeFileSync } = require('node:fs');

async function main() {
  if (!process.argv[2]) throw new Error('Provide a PCB document path');
  const path = resolve(process.argv[2]), root = process.env.PCB_BENCH_ROOT || resolve(__dirname, '..');
  const server = spawn(resolve(root, '.venv/bin/python'), ['-B', '-u', resolve(root, 'cadview.py'), '--no-open'], { cwd: root });
  let browser;
  try {
    const origin = await new Promise((done, reject) => {
      let output = '';
      server.stdout.on('data', chunk => {
        output += chunk; const match = output.match(/http:\/\/127\.0\.0\.1:\d+\//);
        if (match) done(match[0]);
      });
      server.on('error', reject); server.stderr.on('data', chunk => process.stderr.write(chunk));
      server.on('exit', code => reject(new Error('Viewer exited: ' + code)));
    });
    browser = await chromium.launch({ headless: true,
      ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } }), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {
      if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) errors.push(message.text());
    });
    page.on('response', response => {
      if (response.status() >= 400 && new URL(response.url()).pathname !== '/favicon.ico')
        errors.push(response.status() + ' ' + response.url());
    });
    await page.goto(origin);
    await page.evaluate(async () => {
      window.loadProfile = {};
      const { Board3D } = await import('/static/viewer-3d.js');
      for (const name of ['show', 'loadSurfaces', 'loadSteps', 'render', 'updateBounds']) {
        const original = Board3D.prototype[name];
        Board3D.prototype[name] = function (...args) {
          if (name === 'show') window.benchmarkViewer = this;
          const start = performance.now(), result = original.apply(this, args);
          const end = () => {
            const row = window.loadProfile[name] ||= { calls: 0, ms: 0 };
            row.calls++; row.ms += performance.now() - start;
          };
          if (result?.then) return result.finally(end);
          end(); return result;
        };
      }
    });
    const start = Date.now();
    await page.locator('#file').setInputFiles(path);
    await page.waitForFunction(() => document.querySelector('#view > svg') &&
      document.querySelector('#busy').style.display === 'none', undefined, { timeout: 180000 });
    const load = Date.now() - start;
    const layout = await page.evaluate(async () => {
      const view = document.querySelector('#view'), rect = view.getBoundingClientRect(), gaps = [], longtasks = [];
      const observer = new PerformanceObserver(list => longtasks.push(...list.getEntries().map(e => e.duration)));
      observer.observe({ type: 'longtask' });
      const begin = performance.now(); let previous, events = 0;
      const timer = setInterval(() => {
        const elapsed = performance.now() - begin, zoom = elapsed < 2000 || elapsed > 4000;
        view.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, ctrlKey: zoom,
          clientX: rect.x + rect.width * .55, clientY: rect.y + rect.height * .45,
          deltaX: zoom ? 0 : 3, deltaY: zoom ? (elapsed < 2000 ? -1.5 : 1.5) : 1 })); events++;
      }, 16);
      await new Promise(done => requestAnimationFrame(function frame(now) {
        if (previous !== undefined) gaps.push(now - previous); previous = now;
        if (performance.now() - begin < 6000) requestAnimationFrame(frame); else done();
      }));
      clearInterval(timer); longtasks.push(...observer.takeRecords().map(e => e.duration)); observer.disconnect();
      gaps.sort((a, b) => a - b);
      return { renderer: document.querySelector('#renderer-status').textContent, events,
        netCount: document.querySelectorAll('#net-options option').length,
        savedRegionCount: document.querySelectorAll('#scene [data-primitive="region"]').length,
        rafMedianMs: gaps[Math.floor(gaps.length * .5)], rafP95Ms: gaps[Math.floor(gaps.length * .95)],
        rafMaxMs: gaps.at(-1), longtasks, geometry: { ...document.querySelector('.layout-webgl')?.dataset } };
    });
    const three = Date.now();
    await page.keyboard.press('3');
    await page.waitForFunction(() => window.benchmarkViewer?.ready, undefined, { timeout: 180000 });
    const first3D = Date.now() - three;
    if (process.env.PCB_BENCH_SCREENSHOT) await page.screenshot({ path: resolve(process.env.PCB_BENCH_SCREENSHOT) });
    const frames = await page.evaluate(() => {
      const viewer = window.benchmarkViewer, times = [];
      for (let i = 0; i < 20; i++) {
        const start = performance.now(); viewer.rotateStep(1, 0); times.push(performance.now() - start);
      }
      return { times, renderInfo: { ...viewer.renderer.info.render }, objects: viewer.scene.children.length };
    });
    await page.keyboard.press('2'); const warm = Date.now(); await page.keyboard.press('3');
    await page.waitForFunction(() => window.benchmarkViewer.ready);
    const warm3D = Date.now() - warm;
    const data = await page.evaluate(() => {
      const gl = window.benchmarkViewer.renderer.getContext(), debug = gl.getExtension('WEBGL_debug_renderer_info');
      const viewer = window.benchmarkViewer;
      return { profile: window.loadProfile, cores: navigator.hardwareConcurrency,
        models: { ...viewer.modelProgress,
          availableAssets: Object.values(viewer.model.step_models || {}).filter(asset => asset.data).length,
          stepBodies: new Set(viewer.bodyMeshes.filter(entry => entry.mesh.userData.step).map(entry => entry.id)).size },
        gpu: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : null,
        resources: performance.getEntriesByType('resource')
          .filter(r => ['/render', '/geometry'].includes(new URL(r.name).pathname))
          .map(r => ({ path: new URL(r.name).pathname, ms: r.duration, size: r.decodedBodySize })),
        heap: performance.memory?.usedJSHeapSize };
    });
    const result = { document: path, browser: browser.version(), load, layout, first3D, warm3D, frames, ...data, errors };
    if (process.argv[3]) writeFileSync(process.argv[3], JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result, null, 2));
    if (errors.length) throw new Error('Browser reported errors');
  } finally {
    await browser?.close();
    if (server.exitCode === null) { const exited = once(server, 'exit'); server.kill(); await exited; }
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
