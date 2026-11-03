// Usage: PLAYWRIGHT_CHANNEL=chrome node scripts/benchmark_browser_cross_probe.cjs [output.json]
// Generated 400-component/600-net workload. Measures synchronous real probe
// button handlers, including activation, selection and fitting, not display FPS
// or deferred captions. No external CAD fixtures are needed.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { writeFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { once } = require('node:events');
const assert = require('node:assert/strict');
const components = 400, nets = 600;

function layout() {
  const links = [], parts = [];
  for (let i = 0; i < components; i++) {
    links.push({ name: 'P' + i, source_name: 'R' + i, source_id: '' });
    for (let pin = 1; pin <= 2; pin++) parts.push(`<rect data-component="P${i}" data-pad-number="${pin}" data-net="PCB${pin === 1 ? i : (i + components - 1) % components}" x="${i % 20 * 40 + pin * 5}" y="${Math.floor(i / 20) * 40}" width="4" height="4" fill="red"/>`);
  }
  return { name: 'Layout.PcbDoc', kind: 'pcb', webgl: true, layout_components: links,
    stats: { components, pads: 2 * components, tracks: 0, vias: 0, polygons: 0, regions: 0 },
    svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800"><g id="scene"><g id="layer-top" data-layer-name="Top copper" data-layer-role="copper">${parts.join('')}</g></g></svg>` };
}

function schematic() {
  const symbols = [], wires = [], parts = [];
  for (let i = 0; i < components; i++) {
    symbols.push({ id: 'c' + i, name: 'R' + i, elements: ['symbol' + i], body_elements: ['symbol' + i] });
    parts.push(`<g id="symbol${i}"><rect x="${i % 20 * 40}" y="${Math.floor(i / 20) * 40}" width="10" height="10" fill="#ffffb0" stroke="brown"/></g>`);
  }
  for (let i = 0; i < nets; i++) {
    const r = i % components;
    wires.push({ id: 'n' + i, name: 'SCHEM' + i, elements: ['wire' + i],
      terminals: [{ component: 'R' + r, pin: '1' }, { component: 'R' + (r + 1) % components, pin: '2' }] });
    parts.push(`<g id="wire${i}"><path d="M${i % 20 * 40} ${Math.floor(i / 20) * 25 + 15}h20" fill="none" stroke="blue"/></g>`);
  }
  return { name: 'Sheet.SchDoc', kind: 'schematic', selection: { components: symbols, nets: wires },
    stats: { components, wires: nets, net_labels: nets },
    svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800"><g id="scene">${parts.join('')}</g></svg>` };
}

async function main() {
  const root = resolve(__dirname, '..');
  const server = spawn(process.env.PCB_TEST_PYTHON || resolve(root, '.venv/bin/python'),
    ['-B', '-u', resolve(root, 'vcad.py'), '--no-open'], { cwd: root });
  let browser;
  try {
    const origin = await new Promise((done, reject) => {
      let output = '';
      server.stdout.on('data', chunk => {
        output += chunk;
        const match = output.match(/http:\/\/127\.0\.0\.1:\d+\//);
        if (match) done(match[0]);
      });
      server.on('error', reject);
      server.once('exit', code => reject(new Error('Backend exited before startup: ' + code)));
      server.stderr.on('data', chunk => process.stderr.write(chunk));
    });
    browser = await chromium.launch({ headless: true,
      ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/initial', route => route.fulfill({ json: {} }));
    await page.route('**/render', route => route.fulfill({ json:
      route.request().headers()['x-filename'].endsWith('.SchDoc') ? schematic() : layout() }));
    await page.goto(origin);
    for (const name of ['Sheet.SchDoc', 'Layout.PcbDoc']) {
      await page.locator('#file').setInputFiles({ name, mimeType: 'application/octet-stream', buffer: Buffer.from('fixture') });
      await page.waitForFunction(name => document.querySelector('#filename').textContent === name &&
        document.querySelector('#busy').style.display === 'none', name);
    }
    assert.equal(await page.locator('#renderer-status').textContent(), 'WebGL 2');
    const timed = selector => page.evaluate(selector => {
      const start = performance.now(); document.querySelector(selector).click(); return performance.now() - start;
    }, selector);
    const result = { components, nets, browser: browser.version(),
      componentForwardMs: [], componentBackMs: [], netForwardMs: [], netBackMs: [] };
    await page.locator('#component-query').fill('P0');
    for (let i = 0; i < 3; i++) {
      result.componentForwardMs.push(await timed('#probe-schematic-btn'));
      assert.match(await page.locator('#schematic-selection').textContent(), /Components: R0/);
      result.componentBackMs.push(await timed('#probe-layout-btn'));
      assert.equal(await page.locator('#component-query').inputValue(), 'P0');
    }
    await page.locator('#component-clear').click(); await page.locator('#net-query').fill('PCB0');
    for (let i = 0; i < 3; i++) {
      result.netForwardMs.push(await timed('#probe-schematic-btn'));
      assert.match(await page.locator('#schematic-selection').textContent(), /Nets: SCHEM0, SCHEM400/);
      result.netBackMs.push(await timed('#probe-layout-btn'));
      assert.equal(await page.locator('#net-query').inputValue(), 'PCB0');
    }
    result.errors = errors; assert.deepEqual(errors, []);
    if (process.argv[2]) writeFileSync(process.argv[2], JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result, null, 2));
  } finally {
    await browser?.close();
    if (server.exitCode === null) { const exited = once(server, 'exit'); server.kill(); await exited; }
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
