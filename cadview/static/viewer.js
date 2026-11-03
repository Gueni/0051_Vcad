import { NetLabels } from './net-labels.js';
import { LayoutWebGL } from './layout-webgl.js';
import { styleSchematic } from './schematic-style.js';
import { UNITS, distance, snapPoint, segmentAt, labelPosition } from './measurement-tools.js';
import { rectangleMatches } from './rectangle-selection.js';
import { trackViewerSession } from './viewer-session.js';
import { indexLayout, indexSchematic, matchingComponents, matchingNets,
  schematicComponents as findSchematicComponents, reverseNets } from './cross-probe.js';

(function () {
  trackViewerSession();
  const $ = id => document.getElementById(id);
  const view = $('view'), layersEl = $('layers'), readout = $('readout'), label = $('measure-label');
  const NS = 'http://www.w3.org/2000/svg';
  const netLabels = new NetLabels($('net-labels'));
  let svg = null, groups = [], vb = null, home = null;
  let twoDBoard = null;
  const boards = [];
  let activeBoard = null, nextBoardId = 1;
  let layoutGPU = null, gpuUnavailable = false, labelTimer = 0, paintBatch = 0;
  let geometryQueue = Promise.resolve();
  function updateNetLabels() { if (!paintBatch) netLabels.update(activeBoard, gpuActive()); }
  function viewLoading() { return Boolean(activeBoard?.loading || activeBoard?.gpuLoading); }
  function updateLoading() {
    const wasLoading = view.getAttribute('aria-busy') === 'true', loading = viewLoading();
    $('busy').textContent = activeBoard?.gpuLoading ? 'Starting WebGL…' : 'Rendering…';
    $('busy').style.display = loading ? 'flex' : 'none';
    view.setAttribute('aria-busy', String(loading));
    for (const id of ['zoom-in', 'zoom-out', 'view-fit', 'measure-btn']) $(id).disabled = !svg || loading;
    const modeSwitch = $('view-mode-switch');
    const standaloneModel = Boolean(activeBoard?.scene3d?.standalone);
    modeSwitch.hidden = standaloneModel;
    modeSwitch.disabled = !svg || loading || activeBoard?.kind !== 'pcb' || !activeBoard?.scene3d?.board?.length;
    modeSwitch.setAttribute('aria-checked', String(is3D()));
    modeSwitch.title = loading ? 'Loading document…' : !svg ? 'Open a PCB layout to switch views' :
      modeSwitch.disabled ? '3D view requires a PCB with a board outline' : is3D() ? 'Switch to 2D view (2)' : 'Switch to 3D view (3)';
    $('three-navigation').hidden = !is3D();
    const ready3D = is3D() && !loading && threeBoard === activeBoard && Boolean(threeViewer?.scene);
    for (const button of $('three-navigation').querySelectorAll('button')) button.disabled = !ready3D;
    if (wasLoading && !loading && !is3D()) updateNetLabels();
  }
  // The SVG remains the hit-test surface, but navigating a large unaccelerated
  // layout during preparation can overwhelm the browser. Keep input off it
  // until the first GPU draw (or the explicit SVG fallback).
  for (const type of ['wheel', 'gesturestart', 'gesturechange', 'gestureend', 'pointerdown', 'pointermove', 'pointerup',
    'mousedown', 'mousemove', 'mouseup', 'click', 'dblclick', 'touchstart', 'touchmove', 'touchend']) {
    view.addEventListener(type, event => {
      if (!viewLoading()) return;
      event.preventDefault(); event.stopImmediatePropagation();
    }, { capture: true, passive: false });
  }
  function gpuActive() { return Boolean(layoutGPU && !layoutGPU.lost && activeBoard?.gpuScene && layoutGPU.scene === activeBoard.gpuScene); }
  function paintGPU() {
    if (paintBatch || !gpuActive() || is3D()) return;
    layoutGPU.paint(vb, [...activeBoard.sceneGroup.children].filter(g => groups.includes(g)), !activeBoard.polygonsHidden,
      activeBoard.gpuComponents || [], activeBoard.gpuNets || []);
  }
  function syncGPU() {
    view.classList.remove('gpu-layout');
    if (layoutGPU) layoutGPU.canvas.hidden = true;
    const board = activeBoard;
    if (board?.gpuScene && !layoutGPU?.lost && !is3D()) {
      try {
        layoutGPU.setScene(board.gpuScene);
        view.classList.toggle('gpu-layout', !is3D());
        layoutGPU.canvas.hidden = is3D();
        board.gpuStatus = 'WebGL 2';
        paintGPU();
      } catch (error) { board.gpuScene = null; board.gpuStatus = 'SVG: ' + error.message; layoutGPU.release(); }
    }
    const status = $('renderer-status');
    status.hidden = board?.kind !== 'pcb' || is3D();
    status.textContent = layoutGPU?.lost && board?.gpuScene ? 'SVG: graphics context lost; waiting for recovery.' : board?.gpuStatus || 'SVG';
  }
  async function prepareGPU(board, data) {
    if ((data.kind || 'pcb') !== 'pcb' || !data.webgl || gpuUnavailable) return;
    const controller = new AbortController(); board.gpuController = controller;
    const generation = board.request;
    board.gpuStatus = 'Starting WebGL…';
    if (board === activeBoard) syncGPU();
    // Avoid repeatedly sending several large SVG bodies while the single
    // isolated compiler is busy. Closed/reloaded tabs skip their queued turn.
    const previous = geometryQueue;
    let finished;
    geometryQueue = new Promise(resolve => { finished = resolve; });
    try {
      await previous;
      if (controller.signal.aborted) return;
      // Let the loading message paint before synchronous shader compilation.
      if (board === activeBoard && !document.hidden)
        await new Promise(resolve => {
          const done = () => { clearTimeout(timer); cancelAnimationFrame(frame); resolve(); };
          // Occluded windows may stop animation callbacks without becoming
          // hidden. They must not hold the geometry queue indefinitely.
          const timer = setTimeout(done, 100);
          const frame = requestAnimationFrame(() => setTimeout(done, 0));
        });
      if (controller.signal.aborted) return;
      if (gpuUnavailable) { board.gpuStatus = 'SVG: WebGL 2 is unavailable.'; return; }
      try {
        layoutGPU ||= new LayoutWebGL(view, message => {
          if (message && activeBoard) activeBoard.gpuStatus = 'SVG: ' + message;
          syncGPU();
          if (activeBoard?.svg && !is3D()) { updateComponentStatus(); updateNetStatus(); }
        });
      } catch (error) { gpuUnavailable = true; throw error; }
      const body = JSON.stringify({ svg: data.svg, kind: 'pcb', polygon_elements: data.polygon_elements || [] });
      let response;
      for (;;) {
        response = await fetch('/geometry', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, signal: controller.signal });
        if (response.status !== 503) break;
        await new Promise(resolve => setTimeout(resolve, 750));
        if (controller.signal.aborted) return;
      }
      if (!response.ok) throw new Error((await response.json()).error || 'Geometry preparation failed');
      const scene = LayoutWebGL.decode(await response.arrayBuffer());
      if (!boards.includes(board) || board.request !== generation || controller.signal.aborted) return;
      board.gpuScene = scene; board.gpuStatus = 'WebGL 2';
      board.gpuHidden = LayoutWebGL.preparePicking(board.svg);
      netLabels.setBounds(board.svg, scene.caption_bounds || {}, board.gpuHidden);
    } catch (error) {
      if (error.name !== 'AbortError' && boards.includes(board) && board.request === generation) {
        board.gpuStatus = 'SVG: ' + error.message;
      }
    } finally {
      try {
        if (board.gpuController === controller) {
          board.gpuLoading = false; board.gpuController = null;
          if (board === activeBoard) {
            // Upload once, refresh flags/status without intermediate full-board
            // draws, then expose the first complete frame to navigation.
            paintBatch++;
            try { syncGPU(); if (!is3D()) { updateComponentStatus(); updateNetStatus(); } }
            finally { paintBatch--; }
            paintGPU(); updateLoading();
          }
        }
      } finally { finished(); }
    }
  }

  // ------------------------------------------------------------ board tabs / loading
  function saveBoard() {
    if (!activeBoard) return;
    Object.assign(activeBoard, { svg, groups, vb, home, unit, imperialUnit, measuring, pts, measurements, selectedMeasurement, hover, mg });
  }
  function createBoard(file, name = file.name) {
    const board = { id: nextBoardId++, file, name, stats: null, warnings: [],
                    svg: null, groups: [], vb: null, home: null, unit: 'mm', imperialUnit: 'mil', selectedMeasurement: null, measuring: false,
                    pts: [], measurements: [], hover: null, mg: null, loading: false, request: 0 };
    boards.push(board);
    return board;
  }
  function updateTabs() {
    $('tabs').replaceChildren();
    for (const board of boards) {
      const tab = document.createElement('div');
      tab.className = 'tab' + (board === activeBoard ? ' active' : '');
      tab.title = 'Drag to reorder; Alt+Shift+Left/Right moves this tab';
      tab.draggable = true;
      tab.ondragstart = e => { e.dataTransfer.setData('application/x-pcb-tab', String(board.id)); e.dataTransfer.effectAllowed = 'move'; };
      tab.ondragover = e => { if ([...e.dataTransfer.types].includes('application/x-pcb-tab')) e.preventDefault(); };
      tab.ondrop = e => {
        if (![...e.dataTransfer.types].includes('application/x-pcb-tab')) return;
        e.preventDefault();
        const source = boards.find(item => item.id === Number(e.dataTransfer.getData('application/x-pcb-tab')));
        if (source && source !== board) moveTab(source, boards.indexOf(board));
      };
      const button = document.createElement('button');
      button.className = 'tab-name'; button.id = 'board-tab-' + board.id;
      button.textContent = board.name + (board.loading ? ' …' : ''); button.title = board.name;
      button.setAttribute('role', 'tab'); button.setAttribute('aria-controls', 'view');
      button.setAttribute('aria-selected', String(board === activeBoard));
      button.tabIndex = board === activeBoard ? 0 : -1;
      button.onclick = () => activateBoard(board);
      button.onkeydown = e => {
        const index = boards.indexOf(board);
        if (e.altKey && e.shiftKey && ['ArrowLeft', 'ArrowRight'].includes(e.key)) {
          e.preventDefault(); moveTab(board, Math.max(0, Math.min(boards.length - 1, index + (e.key === 'ArrowLeft' ? -1 : 1)))); return;
        }
        let target;
        if (e.key === 'ArrowRight') target = boards[(index + 1) % boards.length];
        if (e.key === 'ArrowLeft') target = boards[(index + boards.length - 1) % boards.length];
        if (e.key === 'Home') target = boards[0];
        if (e.key === 'End') target = boards[boards.length - 1];
        if (e.key === 'Delete') { e.preventDefault(); closeBoard(board); }
        if (target) { e.preventDefault(); activateBoard(target); $('board-tab-' + target.id).focus(); }
      };
      const close = document.createElement('button');
      close.className = 'tab-close'; close.textContent = '×';
      close.setAttribute('aria-label', 'Close ' + board.name);
      close.onclick = () => closeBoard(board);
      tab.append(button, close); $('tabs').append(tab);
    }
    updateProbeTargets();
    updateProbeResult();
  }
  function moveTab(board, destination) {
    boards.splice(boards.indexOf(board), 1); boards.splice(destination, 0, board); updateTabs();
    $('board-tab-' + board.id).focus();
  }
  function activateBoard(board) {
    saveBoard();
    mountBoard(board);
  }
  function mountBoard(board) {
    cancelDrag();
    cancelViewUpdate();
    clearTimeout(labelTimer);
    netLabels.cancel();
    view.classList.remove('gpu-layout');
    if (layoutGPU) {
      layoutGPU.canvas.hidden = true;
      // A schematic needs no GPU scene; keep the last PCB resident for probe-back.
      if (!board || board.kind === 'pcb' && board.gpuScene !== layoutGPU.scene) layoutGPU.release();
    }
    unmount3D();
    if (svg) svg.remove();
    activeBoard = board; twoDBoard = null; drag = null; gestureScale = null;
    ({ svg, groups, vb, home, unit, imperialUnit, measuring, pts, measurements, selectedMeasurement, hover, mg } = board || {
      svg: null, groups: [], vb: null, home: null, unit: 'mm', imperialUnit: 'mil', selectedMeasurement: null, measuring: false, pts: [], measurements: [], hover: null, mg: null,
    });
    $('filename').textContent = board ? board.name : 'No document loaded';
    const schematic = board?.kind === 'schematic';
    document.querySelectorAll('[data-pcb-only]').forEach(el => { el.hidden = schematic; });
    $('schematic-selection').hidden = !schematic;
    $('schematic-hint').hidden = !schematic;
    $('warnings').textContent = board ? board.warnings.join('\n') : '';
    $('warnings').hidden = !$('warnings').textContent;
    const s = board && board.stats;
    $('stats').textContent = !s ? '' : schematic ?
      `${s.components} components, ${s.wires} wires, ${s.net_labels} net labels` :
      `${s.components} components, ${s.pads} pads, ${s.tracks} tracks, ${s.vias} vias\n${s.polygons} polygons, ${s.regions} poured regions`;
    $('empty').style.display = board ? 'none' : '';
    updateLoading();
    label.replaceChildren(); readout.textContent = '';
    $('measure-btn').classList.toggle('on', measuring); view.classList.toggle('measuring', measuring);
    document.querySelectorAll('.unit').forEach(b => b.classList.toggle('on', b.dataset.unit === unit));
    updateUnits();
    layersEl.replaceChildren();
    if (svg) {
      svg.style.display = is3D() ? 'none' : '';
      view.prepend(svg);
    }
    if (!is3D()) { mount2DControls(); apply(); }
    updateTabs();
    mountViewMode();
    if (board) $('view').setAttribute('aria-labelledby', 'board-tab-' + board.id);
    else $('view').removeAttribute('aria-labelledby');
  }
  function mount2DControls() {
    if (twoDBoard === activeBoard && activeBoard) return;
    componentIndex = new Map(); componentHits = []; netIndex = new Map(); netHits = [];
    if (svg) {
      if (activeBoard.kind === 'schematic') buildSchematicComponentTargets(activeBoard);
      else buildLayers();
      // Upload 2D buffers and build search/layer controls only when that view
      // is actually displayed. A 3D tab return should just restore its scene.
      syncGPU();
    }
    updatePolygonControl(); mountNetSearch(); mountComponentSearch(); updateSchematicSelection();
    twoDBoard = activeBoard;
  }
  function closeBoard(board) {
    const index = boards.indexOf(board);
    if (index < 0) return;
    board.controller?.abort();
    invalidate3D(board);
    if (board.gpuScene && layoutGPU?.scene === board.gpuScene) layoutGPU.release();
    board.gpuController?.abort(); board.gpuScene = null;
    boards.splice(index, 1);
    if (board === activeBoard) mountBoard(boards[Math.min(index, boards.length - 1)] || null);
    else updateTabs();
    if (activeBoard) $('board-tab-' + activeBoard.id).focus();
  }
  function openFiles(files) {
    for (const file of files) {
      const extension = file.name.split('.').at(-1).toLowerCase();
      if (extension === 'step' || extension === 'stp') {
        const board = createBoard(file);
        board.mode = '3d';
        activateBoard(board);
        renderStepBoard(board);
        continue;
      }
      const board = createBoard(file);
      if (['sldprt', 'sldasm', 'pdf'].includes(extension)) board.mode = '3d';
      activateBoard(board);
      renderBoard(board);
    }
  }
  async function renderStepBoard(board) {
    const request = ++board.request;
    board.loading = true;
    updateTabs();
    if (board === activeBoard) updateLoading();
    try {
      if (board.file.size > 32 * 1024 * 1024) throw new Error('STEP file exceeds the 32 MB limit.');
      const key = `standalone-${board.id}`;
      const model = {
        standalone: true, thickness: 0, board: [],
        bodies: [{
          id: key, name: board.name, model: key, z: 0, height: 0, color: 0xb0b9c9, opacity: 1,
          matrix: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        }],
        step_models: { [key]: { name: board.name, bytes: await board.file.arrayBuffer() } },
      };
      if (!boards.includes(board) || request !== board.request) return;
      show(board, {
        kind: 'model', name: board.name, svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><g id="scene"/></svg>',
        scene3d: model,
      });
    } catch (error) {
      if (boards.includes(board) && request === board.request) {
        toast(`Could not load ${board.name}: ${error.message}`);
        if (!board.svg) closeBoard(board);
      }
    } finally {
      if (boards.includes(board) && request === board.request) {
        board.loading = false;
        updateTabs();
        if (board === activeBoard) updateLoading();
      }
    }
  }
  async function renderBoard(board) {
    if (board === activeBoard) saveBoard();
    board.controller?.abort();
    board.gpuController?.abort();
    board.gpuController = null; board.gpuLoading = false;
    const controller = new AbortController(); board.controller = controller;
    const request = ++board.request;
    board.loading = true;
    updateTabs();
    if (board === activeBoard) updateLoading();
    try {
      const r = await fetch('/render', { method: 'POST', headers: { 'X-Filename': encodeURIComponent(board.file.name), 'X-WebGL': '1' }, body: board.file, signal: controller.signal });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || r.statusText);
      if (boards.includes(board) && request === board.request) show(board, data);
    } catch (e) {
      if (e.name !== 'AbortError' && boards.includes(board) && request === board.request) {
        toast('Could not render ' + board.name + ': ' + e.message);
        if (!board.svg) closeBoard(board);
      }
    } finally {
      if (boards.includes(board) && request === board.request) {
        board.loading = false; updateTabs();
        if (board === activeBoard) updateLoading();
      }
    }
  }

  function show(board, data) {
    const tpl = document.createElement('div'); tpl.innerHTML = data.svg;
    const rendered = tpl.querySelector('svg');
    if (!rendered) throw new Error('The renderer did not return an SVG.');
    invalidate3D(board);
    if (board.gpuScene && layoutGPU?.scene === board.gpuScene) layoutGPU.release();
    if (board === activeBoard) saveBoard();
    const b = rendered.viewBox.baseVal;
    const origin = { x: b.x, y: b.y, w: b.width, h: b.height };
    const previousLayers = new Map(board.groups.map(g => [g.id, g.style.display]));
    // Schematic groups are drawing containers, not PCB layers.
    const kind = data.kind || 'pcb';
    const polygonIds = new Set(data.polygon_elements || []);
    if (kind === 'pcb') for (const el of rendered.querySelectorAll('#scene [id], #scene [data-primitive]')) {
      if (polygonIds.has(el.id) || ['polygon-outline', 'polygon-cutout'].includes(el.dataset.primitive)) el.classList.add('polygon-copper');
    }
    for (const g of kind === 'pcb' ? rendered.querySelectorAll('#scene > g') : []) {
      g.style.display = previousLayers.has(g.id) ? previousLayers.get(g.id) : defaultLayerVisible(g) ? '' : 'none';
    }
    const measure = document.createElementNS(NS, 'g'); measure.id = 'measure'; rendered.append(measure);
    Object.assign(board, { svg: rendered, name: data.name, kind, stats: data.stats, warnings: data.warnings || [], groups: [],
                           layoutComponents: data.layout_components || [], probeConnections: null, probeSchematic: null,
                           componentIndex: null, netIndex: null, schematicStyleKey: null, schematicTargetsBuilt: false,
                           scene3d: data.scene3d, camera3d: null, bodyEdits: null,
                           home: origin, vb: { ...origin }, pts: [], measurements: [], hover: null, mg: measure, sceneGroup: rendered.querySelector('#scene'),
                           gpuScene: null, gpuLoading: kind === 'pcb' && Boolean(data.webgl) && !gpuUnavailable,
                           gpuStatus: 'SVG', gpuComponents: [], gpuNets: [], copperLayerOrder: data.copper_layer_order || [],
                           selectedMeasurement: null, activeLayer: null,
                           holeElements: [...rendered.querySelectorAll('#scene [data-hole-owner], #scene [data-primitive$="-hole"]')] });
    if (kind === 'model') board.mode = '3d';
    if (kind === 'schematic') prepareSchematicSelection(board, data.selection);
    if (board === activeBoard) mountBoard(board);
    prepareGPU(board, data);
  }

  $('open').onclick = () => $('file').click();
  $('file').onchange = e => { openFiles(e.target.files); e.target.value = ''; };
  document.addEventListener('dragover', e => {
    if (![...e.dataTransfer.types].includes('Files')) return;
    e.preventDefault(); view.classList.add('drop');
  });
  view.addEventListener('dragleave', () => view.classList.remove('drop'));
  document.addEventListener('drop', e => {
    if (![...e.dataTransfer.types].includes('Files')) return;
    e.preventDefault(); view.classList.remove('drop'); openFiles(e.dataTransfer.files);
  });
  document.addEventListener('dragleave', e => { if (!e.relatedTarget) view.classList.remove('drop'); });

  function toast(msg) { const t = $('toast'); t.textContent = msg; t.style.display = 'block'; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', 5000); }

  // ------------------------------------------------------------ 3D mode
  let threeViewer = null, threeModule = null, threeMount = 0, threeBoard = null;
  let rotationHold = null, rotationFrame = 0;
  function is3D() { return activeBoard?.mode === '3d'; }
  function invalidate3D(board) {
    if (threeBoard === board) { board.camera3d = threeViewer?.state(); threeBoard = null; }
    threeViewer?.release(board.scene3d);
  }
  function unmount3D() {
    stopRotation();
    threeMount++;
    if (threeBoard) threeBoard.camera3d = threeViewer?.state();
    threeBoard = null;
    threeViewer?.hide();
    $('three-view').hidden = true;
  }
  function mountViewMode() {
    const standaloneModel = Boolean(activeBoard?.scene3d?.standalone);
    const available = standaloneModel || activeBoard?.kind !== 'schematic' && Boolean(activeBoard?.scene3d?.board?.length);
    if (!available && activeBoard) activeBoard.mode = '2d';
    const showing = is3D();
    updateLoading();
    document.querySelectorAll('[data-2d-only]').forEach(el => { el.hidden = showing || el.hasAttribute('data-pcb-only') && activeBoard?.kind === 'schematic'; });
    $('three-controls').hidden = !showing;
    $('step-add').hidden = standaloneModel;
    $('three-status').textContent = (activeBoard?.scene3d?.notes || []).join('\n');
    $('three-status').hidden = standaloneModel || !showing && available;
    $('view-mode').hidden = activeBoard?.kind === 'schematic' || !showing && (available || !$('three-status').textContent);
    $('net-labels').hidden = showing; label.hidden = showing;
    if (showing) netLabels.cancel();
    view.classList.toggle('measuring', !showing && measuring);
    if (svg) svg.style.display = showing ? 'none' : '';
    syncGPU();
    updateUnits();
    updateBodyControls();
    if (showing) show3D();
  }
  async function show3D() {
    const board = activeBoard, request = ++threeMount;
    $('three-view').hidden = false;
    $('three-summary').textContent = board.scene3d.standalone ? `Loading ${board.name}…` : 'Loading 3D view…';
    try {
      threeModule ||= import('./viewer-3d.js');
      const { Board3D } = await threeModule;
      if (request !== threeMount || board !== activeBoard || !is3D()) return;
      threeViewer ||= new Board3D($('three-view'), message => {
        $('three-status').hidden = false; $('three-status').textContent = message;
        updateLoading();
      }, pickBody, ({ loaded, total, failed, surfaceError }) => {
        if (threeBoard && threeBoard !== activeBoard) return;
        if (surfaceError) { $('three-status').hidden = false; $('three-status').textContent += '\n' + surfaceError; return; }
        const failures = failed.length ? board.scene3d.standalone ?
          ` · ${failed.length} failed.\n${failed.join('\n')}` :
          ` · ${failed.length} unavailable; using body outlines where available.\n${failed.join('\n')}` : '';
        $('step-status').textContent = total ? `STEP models: ${loaded}/${total} loaded${failures}` : '';
      });
      threeViewer.show(board.scene3d, board.camera3d, bodyEdits(board), board.svg);
      threeBoard = board;
      updateLoading();
      updateBodyControls();
    } catch (error) {
      if (request !== threeMount || board !== activeBoard) return;
      threeViewer?.clear();
      threeBoard = null;
      board.mode = '2d'; mountViewMode();
      $('three-view').hidden = true;
      $('three-status').hidden = false;
      $('three-status').textContent = 'Could not open 3D. Check WebGL support in your browser. ' + error.message;
      $('view-mode').hidden = false;
    }
  }
  function setViewMode(mode) {
    if (!activeBoard || activeBoard.kind === 'schematic') return;
    cancelDrag();
    unmount3D(); activeBoard.mode = mode; drag = null; gestureScale = null;
    if (mode === '2d' && svg) { svg.style.display = ''; mount2DControls(); }
    mountViewMode();
    if (mode === '2d') apply();
  }
  $('view-mode-switch').onclick = () => setViewMode(is3D() ? '2d' : '3d');
  $('step-add').onclick = () => $('step-files').click();
  $('step-files').onchange = event => {
    const board = activeBoard, files = [...event.target.files]; event.target.value = '';
    addStepFiles(board, files).catch(error => toast('Could not load STEP file: ' + error.message));
  };
  async function addStepFiles(board, files) {
    if (!board?.scene3d) return;
    const assets = board.scene3d.step_models || {}, unmatched = [];
    const basename = name => name.replaceAll('\\', '/').split('/').at(-1).toLowerCase();
    for (const file of files) {
      const matches = Object.entries(assets).filter(([, asset]) => basename(asset.name) === basename(file.name));
      if (!matches.length) { unmatched.push(file.name); continue; }
      if (file.size > 32 * 1024 * 1024) { toast(`${file.name} exceeds the 32 MB STEP limit.`); continue; }
      // Two different reference paths with the same filename are ambiguous.
      const names = new Set(matches.map(([, asset]) => asset.name));
      if (names.size > 1) { toast(`Several models are named ${file.name}; use distinct filenames in the board.`); continue; }
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]);
        reader.onerror = () => reject(reader.error); reader.readAsDataURL(file);
      });
      const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
      const key = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
      invalidate3D(board);
      for (const [oldKey, asset] of matches) {
        assets[key] = { name: asset.name, data, compressed: false };
        for (const body of board.scene3d.bodies) if (body.model === oldKey) body.model = key;
        if (oldKey !== key) delete assets[oldKey];
      }
    }
    if (board === activeBoard && is3D()) { unmount3D(); mountViewMode(); }
    if (unmatched.length) toast('No model reference matches: ' + unmatched.join(', '));
  }
  for (const preset of ['top', 'bottom']) $('three-' + preset).onclick = () => threeViewer?.fit(preset);
  function stopRotation() {
    const held = rotationHold; rotationHold = null;
    cancelAnimationFrame(rotationFrame); rotationFrame = 0;
    if (held?.pointerId !== undefined && held.button.hasPointerCapture(held.pointerId)) held.button.releasePointerCapture(held.pointerId);
  }
  function startRotation(button, x, y, input) {
    if (button.disabled) return;
    stopRotation();
    const held = rotationHold = { button, board: activeBoard, ...input };
    let last = performance.now() + 250;
    threeViewer?.rotateStep(x, y);
    const repeat = now => {
      if (rotationHold !== held) return;
      if (button.disabled || !is3D() || activeBoard !== held.board || document.hidden) { stopRotation(); return; }
      // Keep speed independent of refresh rate; discard long suspended frames.
      if (now > last) { threeViewer?.rotateStep(x, y, Math.min(now - last, 50) * 0.06); last = now; }
      rotationFrame = requestAnimationFrame(repeat);
    };
    rotationFrame = requestAnimationFrame(repeat);
  }
  for (const [direction, x, y] of [['up', 0, -1], ['down', 0, 1], ['left', -1, 0], ['right', 1, 0]]) {
    const button = $('three-rotate-' + direction);
    button.onpointerdown = event => {
      if (event.button !== 0 || !event.isPrimary || button.disabled) return;
      event.preventDefault(); button.focus({ preventScroll: true });
      startRotation(button, x, y, { pointerId: event.pointerId }); button.setPointerCapture(event.pointerId);
    };
    button.onpointerup = button.onpointercancel = event => {
      if (rotationHold?.pointerId === event.pointerId) stopRotation();
    };
    button.onlostpointercapture = () => { if (rotationHold?.button === button) stopRotation(); };
    button.onkeydown = event => {
      if (![' ', 'Enter'].includes(event.key) || event.altKey || event.ctrlKey || event.metaKey) return;
      event.preventDefault();
      if (!event.repeat) startRotation(button, x, y, { key: event.key });
    };
    button.onkeyup = event => {
      if (![' ', 'Enter'].includes(event.key)) return;
      event.preventDefault(); if (rotationHold?.key === event.key) stopRotation();
    };
    button.onblur = () => { if (rotationHold?.button === button) stopRotation(); };
    // Pointer and keyboard presses already rotate; retain assistive/programmatic clicks.
    button.onclick = event => { if (event.detail === 0) threeViewer?.rotateStep(x, y); };
  }
  window.addEventListener('blur', stopRotation);
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopRotation(); });

  function bodyEdits(board) {
    return board.bodyEdits ||= { selected: new Set(), deleted: new Set(), undo: [] };
  }
  function bodyNames(board) {
    return new Map(board.scene3d.bodies.map((body, index) => [body.id ?? `body-${index}`, body.name || `Body ${index + 1}`]));
  }
  function updateBodyControls() {
    const ready = is3D() && threeBoard === activeBoard;
    const state = ready ? bodyEdits(activeBoard) : null;
    const list = $('three-body-list'), scroll = list.scrollTop;
    list.replaceChildren(); list.disabled = !ready;
    if (ready) {
      const names = bodyNames(activeBoard);
      for (const [id, name] of names) {
        if (state.deleted.has(id)) continue;
        const option = document.createElement('option'); option.value = id; option.textContent = name;
        option.selected = state.selected.has(id); list.append(option);
      }
      list.scrollTop = scroll;
      const count = list.options.length;
      $('three-summary').textContent = activeBoard.scene3d.standalone ?
        `${count} ${count === 1 ? 'part' : 'parts'}` :
        `${activeBoard.scene3d.thickness.toFixed(2)} mm board · ${count} ${count === 1 ? 'body' : 'bodies'}`;
      $('three-selection').textContent = state.selected.size ?
        `${state.selected.size} selected: ${[...state.selected].map(id => names.get(id)).join(', ')}` : 'No body selected.';
    } else $('three-selection').textContent = 'No body selected.';
    $('three-clear').disabled = $('three-delete').disabled = !state?.selected.size;
    $('three-undo').disabled = !state?.undo.length;
  }
  function updateBodies() {
    if (!is3D() || threeBoard !== activeBoard) return;
    threeViewer.updateBodies(bodyEdits(activeBoard));
    updateBodyControls();
  }
  function pickBody(id, additive) {
    if (!is3D() || threeBoard !== activeBoard) return;
    const state = bodyEdits(activeBoard), already = state.selected.has(id);
    if (!additive) state.selected.clear();
    if (id !== null) {
      if (already) state.selected.delete(id);
      else state.selected.add(id);
    }
    updateBodies();
  }
  function clearBodies() {
    if (!is3D() || threeBoard !== activeBoard) return;
    bodyEdits(activeBoard).selected.clear(); updateBodies();
  }
  function deleteBodies() {
    if (!is3D() || threeBoard !== activeBoard) return;
    const state = bodyEdits(activeBoard);
    if (!state.selected.size) return;
    const ids = [...state.selected];
    ids.forEach(id => state.deleted.add(id)); state.undo.push(ids); state.selected.clear();
    updateBodies();
  }
  function undoBodies() {
    if (!is3D() || threeBoard !== activeBoard) return;
    const state = bodyEdits(activeBoard), ids = state.undo.pop();
    if (!ids) return;
    ids.forEach(id => state.deleted.delete(id)); state.selected = new Set(ids);
    updateBodies();
  }
  $('three-body-list').onchange = () => {
    if (!is3D() || threeBoard !== activeBoard) return;
    bodyEdits(activeBoard).selected = new Set([...$('three-body-list').selectedOptions].map(option => option.value));
    updateBodies();
  };
  $('three-clear').onclick = clearBodies;
  $('three-delete').onclick = deleteBodies;
  $('three-undo').onclick = undoBodies;

  // ------------------------------------------------------------ schematic selection
  function prepareSchematicSelection(board, selection = {}) {
    // Resolve IDs within this SVG only. Source IDs and names need no CSS escaping.
    const elements = new Map([...board.svg.querySelectorAll('#scene [id]')].map(el => [el.id, el]));
    function index(rows, attribute) {
      const result = new Map();
      for (const row of rows || []) {
        const targets = row.elements.map(id => elements.get(id)).filter(Boolean);
        if (!targets.length) continue;
        result.set(row.id, { name: row.name, elements: targets,
          aliases: row.aliases || [], terminals: row.terminals || [],
          bodyElements: (row.body_elements || []).map(id => elements.get(id)).filter(Boolean) });
        for (const el of targets) {
          const ids = JSON.parse(el.getAttribute(attribute) || '[]');
          el.setAttribute(attribute, JSON.stringify([...new Set([...ids, row.id])]));
        }
      }
      return result;
    }
    board.schematicComponents = index(selection.components, 'data-sch-components');
    board.schematicNets = index(selection.nets, 'data-sch-nets');
    board.selectedComponents = new Set(); board.selectedNets = new Set();
    // Give thin wires and pins a usable screen-space click target without changing their drawing.
    const conductors = new Set();
    for (const target of board.svg.querySelectorAll('[data-sch-nets]')) {
      if (target.matches('line, path, polyline')) conductors.add(target);
      for (const el of target.querySelectorAll('line, path, polyline')) conductors.add(el);
    }
    for (const el of conductors) {
      const hit = el.cloneNode(false);
      for (const attr of [...hit.attributes]) if (attr.name === 'id' || attr.name.startsWith('data-')) hit.removeAttribute(attr.name);
      hit.removeAttribute('style'); hit.setAttribute('class', 'sch-hit');
      hit.setAttribute('data-sch-nets', el.closest('[data-sch-nets]').getAttribute('data-sch-nets'));
      hit.setAttribute('aria-hidden', 'true'); el.before(hit);
    }
  }
  function buildSchematicComponentTargets(board) {
    if (board.schematicTargetsBuilt) return;
    // Keep the body's own coordinate frame: a screen-aligned bounding box would
    // also capture empty corners around rotated/mirrored symbols.
    for (const row of board.schematicComponents.values()) for (const component of row.elements) {
      if (component.querySelector('.sch-component-hit')) continue;
      const bodies = row.bodyElements.filter(graphic => component.contains(graphic));
      if (!bodies.length) continue;
      let frame = bodies[0];
      while (frame !== component && (!frame.matches('g') || !bodies.every(graphic => frame.contains(graphic)))) frame = frame.parentElement;
      const points = [];
      const inverse = frame.getScreenCTM().inverse();
      for (const graphic of bodies) {
        const b = graphic.getBBox(), transform = inverse.multiply(graphic.getScreenCTM());
        if (!b.width && !b.height) continue;
        for (const [x, y] of [[b.x, b.y], [b.x + b.width, b.y], [b.x, b.y + b.height], [b.x + b.width, b.y + b.height]])
          points.push(new DOMPoint(x, y).matrixTransform(transform));
      }
      if (!points.length) continue;
      const xs = points.map(p => p.x), ys = points.map(p => p.y);
      const x = Math.min(...xs), y = Math.min(...ys);
      const hit = document.createElementNS(NS, 'rect');
      hit.setAttribute('x', x); hit.setAttribute('y', y);
      hit.setAttribute('width', Math.max(Math.max(...xs) - x, 1));
      hit.setAttribute('height', Math.max(Math.max(...ys) - y, 1));
      hit.setAttribute('class', 'sch-component-hit'); hit.setAttribute('aria-hidden', 'true');
      // Restrict the target to the body so external pins still select nets.
      frame.append(hit);
    }
    board.schematicTargetsBuilt = true;
  }
  function updateSchematicSelection() {
    if (activeBoard?.kind !== 'schematic') return;
    const board = activeBoard;
    const key = JSON.stringify([[...board.selectedComponents].sort(), [...board.selectedNets].sort()]);
    if (key !== board.schematicStyleKey) {
      for (const [index, selected, cls] of [
        [board.schematicComponents, board.selectedComponents, 'sch-component-match'],
        [board.schematicNets, board.selectedNets, 'sch-net-match'],
      ]) {
        const matched = new Set([...selected].flatMap(id => index.get(id).elements));
        for (const row of index.values()) for (const el of row.elements) el.classList.toggle(cls, matched.has(el));
      }
      styleSchematic(board); board.schematicStyleKey = key;
    }
    const names = (selected, index) => [...selected].map(id => index.get(id).name).join(', ');
    const selected = [];
    if (board.selectedComponents.size) selected.push('Components: ' + names(board.selectedComponents, board.schematicComponents));
    if (board.selectedNets.size) selected.push('Nets: ' + names(board.selectedNets, board.schematicNets));
    $('schematic-selection-status').textContent = selected.join('\n') || 'Nothing selected.';
    $('schematic-clear').disabled = !selected.length;
    componentHits = [...board.selectedComponents].flatMap(id => board.schematicComponents.get(id).elements);
    netHits = [...board.selectedNets].flatMap(id => board.schematicNets.get(id).elements);
    for (const [kind, index, selection, hits] of [['component', board.schematicComponents, board.selectedComponents, componentHits],
      ['net', board.schematicNets, board.selectedNets, netHits]]) {
      const value = names(selection, index);
      if (document.activeElement !== $(kind + '-query')) { $(kind + '-query').value = value; board[kind + 'Query'] = value; }
      $(kind + '-fit').disabled = !hits.length;
      $(kind + '-reveal').hidden = true;
      $(kind + '-status').textContent = selection.size ? `${selection.size} ${kind}${selection.size === 1 ? '' : 's'} highlighted.` : '';
    }
    updateProbeTargets();
  }
  function clearSchematicSelection() {
    if (activeBoard?.kind !== 'schematic') return;
    activeBoard.selectedComponents.clear(); activeBoard.selectedNets.clear();
    activeBoard.probeMessage = ''; activeBoard.probeResult = null; updateProbeResult();
    updateSchematicSelection();
  }
  function selectSchematic(e) {
    const net = e.target.matches('.sch-component-hit') ? null : e.target.closest('#scene [data-sch-nets]');
    const component = e.target.closest('#scene [data-sch-components]');
    activeBoard.probeMessage = ''; activeBoard.probeResult = null; updateProbeResult();
    // A pin selects its net; clicking the symbol body selects the component.
    const target = net || component;
    if (target && svg.contains(target)) {
      const selected = net ? activeBoard.selectedNets : activeBoard.selectedComponents;
      const ids = JSON.parse(target.getAttribute(net ? 'data-sch-nets' : 'data-sch-components'));
      if (ids.every(id => selected.has(id))) ids.forEach(id => selected.delete(id));
      else {
        if (!e.shiftKey) clearSchematicSelection();
        ids.forEach(id => selected.add(id));
      }
    } else if (!e.shiftKey) clearSchematicSelection();
    updateSchematicSelection();
  }
  $('schematic-clear').onclick = clearSchematicSelection;

  // ------------------------------------------------------------ cross-probing
  function updateProbeTargets() {
    const source = activeBoard;
    const fromSchematic = source?.kind === 'schematic';
    const kind = fromSchematic ? 'pcb' : 'schematic', noun = fromSchematic ? 'layout' : 'schematic';
    const select = $(fromSchematic ? 'probe-layout' : 'probe-schematic');
    const button = $(fromSchematic ? 'probe-layout-btn' : 'probe-schematic-btn');
    const status = $(fromSchematic ? 'probe-status' : 'probe-schematic-status');
    const targets = boards.filter(board => board.kind === kind && board.svg && !board.loading);
    if (source && !targets.some(board => board.id === source.probeTargetId))
      source.probeTargetId = targets.length === 1 ? targets[0].id : null;
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = targets.length ? 'Choose a ' + noun + '…' : 'No ' + noun + ' open';
    select.replaceChildren(empty, ...targets.map(board => {
      const option = document.createElement('option'); option.value = board.id; option.textContent = board.name; return option;
    }));
    select.value = source?.probeTargetId || '';
    select.disabled = !targets.length || !source?.svg;
    const selected = fromSchematic ? source.selectedComponents.size + source.selectedNets.size : componentHits.length + netHits.length;
    button.disabled = !source?.svg || !selected || !source.probeTargetId;
    status.textContent = !source?.svg ? '' : source.probeMessage || (!targets.length ?
      'Open a ' + (fromSchematic ? 'PCB layout (.PcbDoc or .kicad_pcb)' : 'schematic (.SchDoc, .kicad_sch, or .sch)') + ' to cross-probe.' :
      !selected ? 'Select ' + (fromSchematic ? 'schematic' : 'layout') + ' components or nets.' : !source.probeTargetId ? 'Choose the ' + noun + ' to probe.' : '');
  }
  function updateProbeResult() {
    const result = activeBoard?.probeResult;
    $('probe-result').hidden = !result;
    $('probe-result-status').textContent = result?.message || '';
    $('probe-back').disabled = !result || !boards.some(board => board.id === result.sourceId);
    $('probe-back').textContent = activeBoard?.kind === 'schematic' ? 'Back to layout' : 'Back to schematic';
  }
  $('probe-layout').onchange = () => {
    activeBoard.probeTargetId = Number($('probe-layout').value) || null;
    activeBoard.probeMessage = ''; updateProbeTargets();
  };
  $('probe-schematic').onchange = () => {
    activeBoard.probeTargetId = Number($('probe-schematic').value) || null;
    activeBoard.probeMessage = ''; updateProbeTargets();
  };
  $('probe-back').onclick = () => {
    const source = boards.find(board => board.id === activeBoard?.probeResult?.sourceId);
    if (source) activateBoard(source);
  };
  function layoutConnections(layout) {
    if (layout.probeConnections?.svg !== layout.svg) {
      layout.probeConnections = { svg: layout.svg, index: indexLayout(
        layout.svg.querySelectorAll('#scene [data-component], #scene [data-net]'), layout.layoutComponents) };
    }
    return layout.probeConnections.index;
  }
  function schematicConnections(board) {
    if (board.probeSchematic?.svg !== board.svg) {
      board.probeSchematic = { svg: board.svg, index: indexSchematic(board.schematicComponents, board.schematicNets) };
    }
    return board.probeSchematic.index;
  }
  function probeResult(source, components, nets, unmatched) {
    return { sourceId: source.id,
      message: 'From ' + source.name + ': ' + components.size + ' component' + (components.size === 1 ? '' : 's') +
        ', ' + nets.size + ' net' + (nets.size === 1 ? '' : 's') + ' highlighted.' + unmatched };
  }
  function probeTransition(update) {
    paintBatch++;
    try { update(); }
    finally { paintBatch--; if (!paintBatch) { paintGPU(); updateNetLabels(); } }
  }
  $('probe-layout-btn').onclick = () => {
    const source = activeBoard;
    const layout = boards.find(board => board.id === source?.probeTargetId && board.kind === 'pcb' && board.svg && !board.loading);
    if (source?.kind !== 'schematic' || !layout) return;
    const connections = layoutConnections(layout), schematic = schematicConnections(source);
    const components = new Set(), nets = new Set(), missing = [];
    const resolveComponent = (id, row) => matchingComponents(id, row, connections);
    for (const id of source.selectedComponents) {
      const row = source.schematicComponents.get(id), matches = resolveComponent(id, row);
      if (matches.length) matches.forEach(name => components.add(name));
      else missing.push('component ' + row.name);
    }
    for (const id of source.selectedNets) {
      const row = source.schematicNets.get(id);
      const matches = matchingNets(id, row, schematic, connections);
      if (matches.length === 1) nets.add(matches[0]);
      else missing.push('net ' + row.name + (matches.length > 1 ? ' (ambiguous)' : ''));
    }
    const unmatched = missing.length ? '\nNot found: ' + missing.join(', ') : '';
    if (!components.size && !nets.size) {
      source.probeMessage = 'No matches in ' + layout.name + '.' + unmatched; updateProbeTargets(); return;
    }
    layout.probeComponents = [...components]; layout.probeNets = [...nets];
    layout.componentQuery = [...components].join(', '); layout.netQuery = [...nets].join(', ');
    layout.probeResult = probeResult(source, components, nets, unmatched);
    probeTransition(() => {
      activateBoard(layout); if (is3D()) setViewMode('2d'); setMeasuring(false);
      const hits = [...componentHits, ...netHits], layers = new Set(hits.map(el => el.closest('#scene > g')));
      let revealed = false;
      for (const group of groups) if (group.style.display === 'none' && layers.has(group)) {
        group._cb.checked = true; group.style.display = ''; raiseLayer(group); revealed = true;
      }
      if (revealed) { updateComponentStatus(); updateNetStatus(); }
      fitMatches(hits);
    });
  };
  $('probe-schematic-btn').onclick = () => {
    const source = activeBoard;
    const target = boards.find(board => board.id === source?.probeTargetId && board.kind === 'schematic' && board.svg && !board.loading);
    if (source?.kind !== 'pcb' || !target) return;
    const connections = layoutConnections(source), schematic = schematicConnections(target);
    const components = new Set(), nets = new Set(), missing = [];
    for (const name of new Set(componentHits.map(el => el.dataset.component))) {
      const matches = findSchematicComponents(name, schematic, connections);
      if (matches.length) matches.forEach(id => components.add(id));
      else missing.push('component ' + name);
    }
    // Component-only probes never need to resolve the schematic's nets.
    const netMatches = netHits.length ? reverseNets(schematic, connections) : null;
    for (const name of new Set(netHits.map(el => el.dataset.net))) {
      const matches = netMatches.reverse.get(name) || [];
      if (matches.length) matches.forEach(id => nets.add(id));
      else missing.push('net ' + name + (netMatches.ambiguous.has(name) ? ' (ambiguous)' : ''));
    }
    const unmatched = missing.length ? '\nNot found: ' + missing.join(', ') : '';
    if (!components.size && !nets.size) {
      source.probeMessage = 'No matches in ' + target.name + '.' + unmatched; updateProbeTargets(); return;
    }
    target.selectedComponents = components; target.selectedNets = nets; target.probeMessage = '';
    target.probeResult = probeResult(source, components, nets, unmatched);
    probeTransition(() => {
      activateBoard(target);
      const hits = [...components].flatMap(id => target.schematicComponents.get(id).elements)
        .concat([...nets].flatMap(id => target.schematicNets.get(id).elements));
      fitMatches(hits);
    });
  };
  function resetProbeSelection(kind) {
    if (!activeBoard) return;
    activeBoard[kind] = null; activeBoard.probeResult = null; activeBoard.probeMessage = ''; updateProbeResult();
  }

  // ------------------------------------------------------------ layout highlight drawing
  function buildLayoutHighlight(id, hits, color) {
    svg?.querySelector('#' + id)?.remove();
    const overlay = document.createElementNS(NS, 'g'); overlay.id = id;
    overlay.style.pointerEvents = 'none';
    const hiddenPolygon = el => activeBoard?.polygonsHidden && el.closest('.polygon-copper');
    const visibleHits = hits.filter(el => !hiddenPolygon(el));
    const matched = new Set(visibleHits), branches = new Set();
    for (const el of visibleHits) for (let node = el; node && node.id !== 'scene'; node = node.parentElement) branches.add(node);
    if (activeBoard?.gpuLoading && !gpuActive()) {
      // The SVG is hidden behind the startup gate. Avoid cloning large nets
      // while the compiler works; completion applies the current selections
      // directly to WebGL, or rebuilds the overlay for SVG fallback.
      return { overlay: { childElementCount: groups.filter(g => g.style.display !== 'none' && branches.has(g)).length },
        hidden: groups.filter(g => g.style.display === 'none' && branches.has(g)).length, hiddenPolygons: hits.some(hiddenPolygon) };
    }
    if (gpuActive()) {
      const visibleLayers = new Set(groups.filter(g => g.style.display !== 'none'));
      const visible = el => !hiddenPolygon(el) && visibleLayers.has(el.closest('#scene > g'));
      activeBoard.gpuComponents = [...new Set(componentHits.filter(visible).map(el => el.dataset.component))];
      activeBoard.gpuNets = [...new Set(netHits.filter(visible).map(el => el.dataset.net))];
      // IDs in a lookup texture tint the original GPU geometry. Keep the DOM
      // solely for exact hit testing, search, bounds and cross-probing.
      svg.querySelectorAll('#component-highlight, #net-highlight, #layout-holes').forEach(el => el.remove());
      svg.classList.toggle('layout-selection', Boolean(activeBoard.gpuComponents.length || activeBoard.gpuNets.length));
      paintGPU();
      return { overlay: { childElementCount: groups.filter(g => g.style.display !== 'none' && branches.has(g)).length },
        hidden: groups.filter(g => g.style.display === 'none' && branches.has(g)).length, hiddenPolygons: hits.some(hiddenPolygon) };
    }
    function tintedCopy(node) {
      const copy = node.cloneNode(true);
      const originals = [node, ...node.querySelectorAll('*')], copies = [copy, ...copy.querySelectorAll('*')];
      for (let i = 0; i < originals.length; i++) {
        const original = originals[i], clone = copies[i];
        // Drill cutouts keep their paint. Tinting them would fill pad/via holes.
        if (!original.matches('path, rect, circle, ellipse, line, polyline, polygon, text, tspan, use') ||
            original.closest('[data-hole-owner], [data-primitive$="-hole"]')) continue;
        const paint = getComputedStyle(original);
        for (const property of ['fill', 'stroke']) {
          const value = paint[property];
          clone.style.setProperty(property, ['none', 'transparent', 'rgba(0, 0, 0, 0)'].includes(value) ? value : color, 'important');
        }
        clone.style.setProperty('stroke-width', paint.strokeWidth, 'important');
      }
      return copy;
    }
    function branch(node) {
      if (matched.has(node)) return tintedCopy(node);
      const copy = node.cloneNode(false);
      for (const child of node.children) if (branches.has(child)) copy.append(branch(child));
      return copy;
    }
    // Keep the renderer's transforms, clipping, widths, and shape geometry.
    // Only the original layer groups are faded; these copies stay at full opacity.
    for (const group of groups) if (group.style.display !== 'none' && branches.has(group)) overlay.append(branch(group));
    for (const el of overlay.querySelectorAll('*')) {
      el.removeAttribute('id'); el.classList.remove('component-match', 'net-match');
      el.style.setProperty('pointer-events', 'none', 'important');
      for (const attr of [...el.attributes]) if (attr.name.startsWith('data-')) el.removeAttribute(attr.name);
    }
    if (overlay.childElementCount) svg.querySelector('#scene').append(overlay);
    svg?.classList.toggle('layout-selection', Boolean(svg.querySelector('#component-highlight, #net-highlight')));
    return { overlay, hidden: groups.filter(group => group.style.display === 'none' && branches.has(group)).length,
      hiddenPolygons: hits.some(hiddenPolygon) };
  }
  function updateHoleOverlay() {
    if (!svg || activeBoard?.kind !== 'pcb' || gpuActive()) return;
    const component = svg.querySelector('#component-highlight'), net = svg.querySelector('#net-highlight');
    // A component's own geometry keeps its component color when its net is
    // selected too, matching the native renderer's object selection priority.
    if (component) component.parentNode.append(component);
    if (component || net) {
      // Like the native hole pass, keep every visible drill cutout above
      // highlighted copper, including holes belonging to another net.
      const key = JSON.stringify([activeBoard.polygonsHidden,
        [...activeBoard.sceneGroup.children].filter(g => groups.includes(g)).map(g => [g.id, g.style.display])]);
      const previous = svg.querySelector('#layout-holes');
      if (previous && activeBoard.holeOverlayStyle === key) previous.parentNode.append(previous);
      else {
        buildLayoutHighlight('layout-holes', activeBoard.holeElements || [], '');
        activeBoard.holeOverlayStyle = key;
      }
    } else svg.querySelector('#layout-holes')?.remove();
  }

  // ------------------------------------------------------------ component search
  let componentIndex = new Map(), componentHits = [];
  function mountComponentSearch() {
    componentIndex = activeBoard?.componentIndex || new Map(); componentHits = [];
    if (!activeBoard?.componentIndex && activeBoard?.kind === 'schematic') {
      for (const row of activeBoard.schematicComponents.values()) componentIndex.set(row.name, row.elements);
    }
    for (const el of activeBoard?.componentIndex ? [] : svg?.querySelectorAll('#scene [data-component]') || []) {
      const name = el.dataset.component;
      if (!name) continue;
      if (!componentIndex.has(name)) componentIndex.set(name, []);
      componentIndex.get(name).push(el);
    }
    if (activeBoard) activeBoard.componentIndex = componentIndex;
    $('component-options').replaceChildren(...[...componentIndex.keys()].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).map(name => {
      const option = document.createElement('option'); option.value = name; return option;
    }));
    $('component-query').disabled = !svg;
    $('component-query').value = activeBoard?.componentQuery || '';
    if (activeBoard?.kind !== 'schematic') searchComponents();
  }
  function searchComponents() {
    if (activeBoard?.kind === 'schematic') { searchSchematic('component'); return; }
    for (const el of componentHits) el.classList.remove('component-match');
    componentHits = [];
    const query = $('component-query').value.trim().toLowerCase();
    if (activeBoard) activeBoard.componentQuery = $('component-query').value;
    const names = [...componentIndex.keys()];
    const exact = names.find(name => name.toLowerCase() === query);
    const matches = activeBoard?.probeComponents || (query ? names.filter(name => exact ? name === exact : name.toLowerCase().includes(query)) : []);
    componentHits = matches.flatMap(name => componentIndex.get(name));
    for (const el of componentHits) el.classList.add('component-match');
    updateComponentStatus(matches.length);
    updateProbeTargets();
  }
  function updateComponentStatus(count = new Set(componentHits.map(el => el.dataset.component)).size) {
    if (activeBoard?.kind === 'schematic') { updateSchematicSelection(); return; }
    const { overlay, hidden, hiddenPolygons } = buildLayoutHighlight('component-highlight', componentHits, '#ffe066');
    updateHoleOverlay();
    $('component-fit').disabled = !overlay.childElementCount;
    $('component-reveal').hidden = !hidden;
    $('component-status').textContent = !$('component-query').value.trim() ? '' : !count ? 'No matching components.' :
      `${count} component${count === 1 ? '' : 's'} ${hiddenPolygons && !overlay.childElementCount ? 'matched' : 'highlighted'}` +
      (hidden ? `; ${hidden} hidden layer${hidden === 1 ? '' : 's'}` : '') + (hiddenPolygons ? '; polygons hidden.' : '.');
    updateNetLabels();
  }
  function clearComponent() { resetProbeSelection('probeComponents'); $('component-query').value = ''; searchComponents(); }
  $('component-query').oninput = () => { resetProbeSelection('probeComponents'); searchComponents(); };
  $('component-clear').onclick = clearComponent;
  $('component-fit').onclick = () => fitMatches(componentHits);
  function fitMatches(hits) {
    if (viewLoading()) return;
    const rects = hits.map(el => el.getBoundingClientRect()).filter(r => r.width || r.height);
    if (!rects.length) return;
    const bounds = rects.reduce((b, r) => ({ left: Math.min(b.left, r.left), top: Math.min(b.top, r.top),
      right: Math.max(b.right, r.right), bottom: Math.max(b.bottom, r.bottom) }), rects[0]);
    const a = toSvg({ clientX: bounds.left, clientY: bounds.top });
    const b = toSvg({ clientX: bounds.right, clientY: bounds.bottom });
    const pad = Math.max(b.x - a.x, b.y - a.y, 2) * 0.25;
    vb = { x: a.x - pad, y: a.y - pad, w: b.x - a.x + 2 * pad, h: b.y - a.y + 2 * pad }; apply();
  }
  $('component-reveal').onclick = () => {
    for (const g of groups) if (g.style.display === 'none' && componentHits.some(el => g.contains(el))) g._cb.click();
    updateComponentStatus();
  };

  // ------------------------------------------------------------ layers
  function updatePolygonControl() {
    const hidden = Boolean(activeBoard?.polygonsHidden);
    svg?.classList.toggle('hide-polygons', hidden);
    $('polygons-toggle').textContent = hidden ? 'Show polygons' : 'Hide polygons';
    $('polygons-toggle').setAttribute('aria-pressed', String(hidden));
    $('polygons-toggle').disabled = activeBoard?.kind !== 'pcb' || !svg?.querySelector('.polygon-copper');
  }
  $('polygons-toggle').onclick = () => {
    if (activeBoard?.kind !== 'pcb' || !svg) return;
    activeBoard.polygonsHidden = !activeBoard.polygonsHidden;
    updatePolygonControl(); updateComponentStatus(); updateNetStatus();
  };
  function defaultLayerVisible(g) {
    const { layerRole: role, layerFamily: family, layerToken: token } = g.dataset;
    if (g.id === 'board-outline' || role === 'copper' || role === 'drill' ||
        family === 'signal' || family === 'internal_plane') return true;
    // Tokens stay stable even when the board gives layers custom display names.
    if (token) return /^(TOP|BOTTOM|MID\d+|PLANE\d+|TOPOVERLAY|DRILLS|DRILLDRAWING|DRILLGUIDE|MULTILAYER)$/i.test(token);
    return /^(layer-)?(top|bottom|mid\d+|plane\d+|topoverlay|drills|drilldrawing|drillguide|multilayer)$/i.test(g.id) ||
      /^(top copper|bottom copper|top overlay)$/i.test(g.dataset.layerName || '');
  }
  function buildLayers() {
    layersEl.innerHTML = '';
    // Keep the sidebar in renderer order even after changing SVG paint order.
    if (!groups.length) groups = [...svg.querySelectorAll('#scene > g')];
    const ranks = new Map((activeBoard.copperLayerOrder || []).map((token, i) => [token, i]));
    const copper = groups.filter(g => ranks.has(g.dataset.layerToken)).sort((a, b) => ranks.get(a.dataset.layerToken) - ranks.get(b.dataset.layerToken));
    let index = 0;
    groups = groups.map(g => ranks.has(g.dataset.layerToken) ? copper[index++] : g);
    activeBoard.groups = groups;
    for (const g of groups) {
      const name = g.dataset.layerDisplayName || g.dataset.layerName || g.id.replace(/^layer-/, '');
      const color = g.dataset.color || (g.id === 'board-outline' ? '#C0A000' : '#888');
      const lab = document.createElement('label');
      const cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = g.style.display !== 'none';
      cb.onchange = () => {
        g.style.display = cb.checked ? '' : 'none';
        // SVG paints later siblings on top; retain the scene's transforms and clipping.
        if (cb.checked) raiseLayer(g);
        updateComponentStatus();
        if (measuring) { refreshHover(); draw(); }
      };
      const sw = document.createElement('span'); sw.className = 'swatch'; sw.style.background = color;
      const title = document.createElement('button'); title.type = 'button'; title.className = 'layer-name';
      title.textContent = name; title.setAttribute('aria-pressed', String(activeBoard.activeLayer === g.id));
      title.onclick = e => { e.preventDefault(); raiseLayer(g); updateComponentStatus(); updateNetStatus(); };
      lab.classList.toggle('active-layer', activeBoard.activeLayer === g.id);
      lab.append(cb, sw, title);
      if (g.dataset.primitiveCount !== undefined) { const c = document.createElement('span'); c.className = 'count'; c.textContent = g.dataset.primitiveCount; lab.append(c); }
      layersEl.append(lab); g._cb = cb;
    }
  }
  function raiseLayer(group) {
    activeBoard.activeLayer = group.id;
    group.parentNode.append(group);
    // Preserve native cutouts when a copper layer is moved above other copper.
    for (const drill of groups.filter(g => g.dataset.layerRole === 'drill' || g.dataset.layerToken === 'DRILLS' || g.id === 'layer-drills')) drill.parentNode.append(drill);
    for (const g of groups) {
      const row = g._cb?.closest('label');
      row?.classList.toggle('active-layer', g === group);
      row?.querySelector('.layer-name')?.setAttribute('aria-pressed', String(g === group));
    }
  }
  const setAll = v => {
    groups.forEach(g => { g._cb.checked = v; g.style.display = v ? '' : 'none'; }); updateComponentStatus();
    if (measuring) { refreshHover(); draw(); }
  };
  $('all').onclick = () => setAll(true);
  $('none').onclick = () => setAll(false);

  // ------------------------------------------------------------ net search
  let netIndex = new Map(), netHits = [];
  function mountNetSearch() {
    netIndex = activeBoard?.netIndex || new Map(); netHits = [];
    if (!activeBoard?.netIndex && activeBoard?.kind === 'schematic') {
      for (const row of activeBoard.schematicNets.values()) netIndex.set(row.name, row.elements);
    }
    for (const el of activeBoard?.netIndex ? [] : svg?.querySelectorAll('#scene [data-net]') || []) {
      const name = el.dataset.net;
      if (!name) continue;
      if (!netIndex.has(name)) netIndex.set(name, []);
      netIndex.get(name).push(el);
    }
    if (activeBoard) activeBoard.netIndex = netIndex;
    $('net-options').replaceChildren(...[...netIndex.keys()].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).map(name => {
      const option = document.createElement('option'); option.value = name; return option;
    }));
    $('net-query').disabled = !svg;
    $('net-query').value = activeBoard?.netQuery || '';
    if (activeBoard?.kind !== 'schematic') searchNets();
  }
  function searchNets() {
    if (activeBoard?.kind === 'schematic') { searchSchematic('net'); return; }
    for (const el of netHits) el.classList.remove('net-match');
    netHits = [];
    const query = $('net-query').value.trim().toLowerCase();
    if (activeBoard) activeBoard.netQuery = $('net-query').value;
    const names = [...netIndex.keys()];
    const exact = names.find(name => name.toLowerCase() === query);
    const matches = activeBoard?.probeNets || (query ? names.filter(name => exact ? name === exact : name.toLowerCase().includes(query)) : []);
    netHits = matches.flatMap(name => netIndex.get(name));
    for (const el of netHits) el.classList.add('net-match');
    updateNetStatus(matches.length);
    updateProbeTargets();
  }
  function updateNetStatus(count = new Set(netHits.map(el => el.dataset.net)).size) {
    if (activeBoard?.kind === 'schematic') { updateSchematicSelection(); return; }
    const { overlay, hidden, hiddenPolygons } = buildLayoutHighlight('net-highlight', netHits, '#68e0ff');
    updateHoleOverlay();
    $('net-fit').disabled = !overlay.childElementCount;
    $('net-reveal').hidden = !hidden;
    $('net-status').textContent = !$('net-query').value.trim() ? '' : !count ? 'No matching nets.' :
      `${count} net${count === 1 ? '' : 's'} ${hiddenPolygons && !overlay.childElementCount ? 'matched' : 'highlighted'}` +
      (hidden ? `; ${hidden} hidden layer${hidden === 1 ? '' : 's'}` : '') + (hiddenPolygons ? '; polygons hidden.' : '.');
    updateNetLabels();
  }
  function clearNet() { resetProbeSelection('probeNets'); $('net-query').value = ''; searchNets(); }
  function searchSchematic(kind) {
    const board = activeBoard, query = $(kind + '-query').value.trim().toLowerCase();
    const index = kind === 'component' ? board.schematicComponents : board.schematicNets;
    const exact = [...index].filter(([, row]) => row.name.toLowerCase() === query);
    const matches = exact.length ? exact : [...index].filter(([, row]) => query && row.name.toLowerCase().includes(query));
    board[kind === 'component' ? 'selectedComponents' : 'selectedNets'] = new Set(matches.map(([id]) => id));
    board[kind + 'Query'] = $(kind + '-query').value;
    updateSchematicSelection();
    if (query && !matches.length) $(kind + '-status').textContent = `No matching ${kind}s.`;
  }
  $('net-query').oninput = () => { resetProbeSelection('probeNets'); searchNets(); };
  $('net-clear').onclick = clearNet;
  function toggleLayoutSelection(name, attribute, hits, selectionKey, queryId, search, additive = false) {
    const selected = new Set(hits.map(el => el.dataset[attribute]));
    const remove = selected.delete(name);
    resetProbeSelection(selectionKey);
    // Retain the other exact matches from a search or cross-probe. The query
    // remains a readable list; the array prevents substring or comma matching.
    if (additive && !remove) selected.add(name);
    if ((remove || additive) && selected.size) activeBoard[selectionKey] = [...selected];
    $(queryId).value = remove || additive ? [...selected].join(', ') : name;
    search();
  }
  view.addEventListener('click', e => {
    if (is3D()) return;
    if (!svg || measuring || panned || e.button !== 0) return;
    if (activeBoard.kind === 'schematic') { selectSchematic(e); return; }
    const primitive = e.target.closest('#scene [data-net]');
    const name = primitive?.dataset.net;
    if (name && svg.contains(primitive) && netIndex.has(name)) {
      toggleLayoutSelection(name, 'net', netHits, 'probeNets', 'net-query', searchNets, e.shiftKey);
    }
    const component = e.target.closest('#scene [data-component]');
    const designator = component?.dataset.component;
    if (designator && svg.contains(component) && componentIndex.has(designator)) {
      toggleLayoutSelection(designator, 'component', componentHits, 'probeComponents', 'component-query', searchComponents, e.shiftKey);
    }
  });
  $('net-fit').onclick = () => fitMatches(netHits);
  $('net-reveal').onclick = () => {
    for (const g of groups) if (g.style.display === 'none' && netHits.some(el => g.contains(el))) g._cb.click();
    updateNetStatus();
  };

  layersEl.addEventListener('change', () => updateNetStatus());
  $('all').addEventListener('click', () => updateNetStatus());
  $('none').addEventListener('click', () => updateNetStatus());

  // ------------------------------------------------------------ pan / zoom
  let viewFrame = 0, drag = null, panned = false, gestureScale = null, spaceHeld = false;
  function cancelDrag(suppressClick = true) {
    if (!drag) return;
    if (drag && suppressClick) panned = true;
    drag = null;
    $('selection-rectangle').hidden = true;
    view.classList.remove('selecting', 'panning');
  }
  function dragBox(e) {
    const bounds = view.getBoundingClientRect();
    const x = Math.max(bounds.left, Math.min(bounds.right, e.clientX));
    const y = Math.max(bounds.top, Math.min(bounds.bottom, e.clientY));
    return { left: Math.min(drag.x, x), right: Math.max(drag.x, x), top: Math.min(drag.y, y), bottom: Math.max(drag.y, y) };
  }
  function selectRectangle(box, additive) {
    const schematic = activeBoard.kind === 'schematic';
    const matches = rectangleMatches(activeBoard, schematic ? activeBoard.schematicComponents : componentIndex,
      schematic ? activeBoard.schematicNets : netIndex, box);
    activeBoard.probeMessage = ''; activeBoard.probeResult = null; updateProbeResult();
    if (schematic) {
      if (!additive) { activeBoard.selectedComponents.clear(); activeBoard.selectedNets.clear(); }
      matches.components.forEach(id => activeBoard.selectedComponents.add(id));
      matches.nets.forEach(id => activeBoard.selectedNets.add(id));
      updateSchematicSelection();
    } else {
      const components = new Set(additive ? componentHits.map(el => el.dataset.component) : []);
      const nets = new Set(additive ? netHits.map(el => el.dataset.net) : []);
      matches.components.forEach(name => components.add(name)); matches.nets.forEach(name => nets.add(name));
      activeBoard.probeComponents = [...components]; activeBoard.probeNets = [...nets];
      $('component-query').value = [...components].join(', '); $('net-query').value = [...nets].join(', ');
      searchComponents(); searchNets();
    }
  }
  function cancelViewUpdate() { cancelAnimationFrame(viewFrame); viewFrame = 0; }
  function apply(redrawMeasurements = true) {
    cancelViewUpdate();
    if (!svg) return;
    svg.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
    if (measuring) refreshHover();
    if (redrawMeasurements) draw(); else positionMeasurementLabels();
    paintGPU();
    clearTimeout(labelTimer);
    if (gpuActive() && !redrawMeasurements) {
      // Caption placement performs exact DOM hit probes. Keep it off the
      // navigation frame; refresh once the gesture has settled.
      $('net-labels').replaceChildren();
      netLabels.cancel();
      labelTimer = setTimeout(updateNetLabels, 140);
    } else updateNetLabels();
  }
  function queueViewUpdate() {
    if (!viewFrame) viewFrame = requestAnimationFrame(() => { viewFrame = 0; apply(false); });
  }
  function viewPoint(e) {
    const point = new DOMPoint(e.clientX, e.clientY).matrixTransform(svg.getScreenCTM().inverse());
    const shown = svg.viewBox.baseVal;
    // Input can arrive several times before the next animation frame. Account
    // for the pending viewBox while using the actual SVG letterbox transform.
    return { x: vb.x + (point.x - shown.x) * vb.w / shown.width,
             y: vb.y + (point.y - shown.y) * vb.h / shown.height };
  }
  function screenUnits() {
    const inverse = svg.getScreenCTM().inverse(), ratio = vb.w / svg.viewBox.baseVal.width;
    return { a: inverse.a * ratio, b: inverse.b * ratio, c: inverse.c * ratio, d: inverse.d * ratio };
  }
  function centerPoint() {
    const r = view.getBoundingClientRect(); return { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
  }
  function zoomAt(point, factor) {
    if (!svg || viewLoading() || !Number.isFinite(factor) || factor <= 0) return;
    cancelDrag();
    const span = Math.max(vb.w, vb.h), base = Math.max(home.w, home.h);
    const k = Math.max(base * 0.0001, Math.min(base * 100, span * factor)) / span;
    const anchor = viewPoint(point);
    vb = { x: anchor.x - (anchor.x - vb.x) * k, y: anchor.y - (anchor.y - vb.y) * k,
           w: vb.w * k, h: vb.h * k };
    queueViewUpdate();
  }
  function resetView() {
    if (viewLoading()) return;
    if (is3D()) { threeViewer?.fit(); return; }
    if (svg) { cancelDrag(); vb = { ...home }; apply(); }
  }
  $('zoom-in').onclick = () => is3D() ? threeViewer?.zoom(1 / 1.25) : zoomAt(centerPoint(), 1 / 1.25);
  $('zoom-out').onclick = () => is3D() ? threeViewer?.zoom(1.25) : zoomAt(centerPoint(), 1.25);
  $('view-fit').onclick = resetView;
  view.addEventListener('wheel', e => {
    if (!svg || is3D()) return;
    cancelDrag();
    e.preventDefault();
    if (gestureScale !== null || !Number.isFinite(e.deltaX) || !Number.isFinite(e.deltaY)) return;
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? view.clientHeight : 1;
    if (e.ctrlKey || e.metaKey) {
      zoomAt(e, Math.exp(Math.max(-100, Math.min(100, e.deltaY * unit)) * 0.01));
    } else {
      const scale = screenUnits(), dx = e.deltaX * unit, dy = e.deltaY * unit;
      vb.x += scale.a * dx + scale.c * dy; vb.y += scale.b * dx + scale.d * dy;
      queueViewUpdate();
    }
  }, { passive: false });
  // Safari exposes native pinch gestures separately from wheel events.
  view.addEventListener('gesturestart', e => {
    if (!svg || is3D()) return; cancelDrag(); e.preventDefault(); gestureScale = e.scale > 0 ? e.scale : 1;
  }, { passive: false });
  view.addEventListener('gesturechange', e => {
    if (!svg || is3D() || gestureScale === null || !Number.isFinite(e.scale) || e.scale <= 0) return;
    e.preventDefault();
    const point = Number.isFinite(e.clientX) && Number.isFinite(e.clientY) ? e : centerPoint();
    zoomAt(point, gestureScale / e.scale); gestureScale = e.scale;
  }, { passive: false });
  view.addEventListener('gestureend', e => { if (gestureScale !== null) e.preventDefault(); gestureScale = null; }, { passive: false });
  view.addEventListener('mousedown', e => {
    panned = false;
    if (!is3D() && svg && !activeBoard.loading && (e.button === 1 || e.button === 0)) {
      e.preventDefault();
      view.focus({ preventScroll: true });
      drag = { x: e.clientX, y: e.clientY, vx: vb.x, vy: vb.y, scale: screenUnits(), button: e.button,
        selection: e.button === 0 && !measuring && !spaceHeld, additive: e.shiftKey };
    }
  });
  window.addEventListener('mousemove', e => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y, scale = drag.scale;
    if (Math.hypot(dx, dy) > 3) panned = true;
    if (!panned) return;
    if (drag.selection) {
      const box = dragBox(e), bounds = view.getBoundingClientRect(), rectangle = $('selection-rectangle');
      rectangle.hidden = false;
      Object.assign(rectangle.style, { left: box.left - bounds.left + 'px', top: box.top - bounds.top + 'px',
        width: box.right - box.left + 'px', height: box.bottom - box.top + 'px' });
      view.classList.add('selecting');
      return;
    }
    view.classList.add('panning');
    vb.x = drag.vx - scale.a * dx - scale.c * dy; vb.y = drag.vy - scale.b * dx - scale.d * dy;
    queueViewUpdate();
  });
  window.addEventListener('mouseup', e => {
    if (!drag || e.button !== drag.button) return;
    if (drag.selection && panned) selectRectangle(dragBox(e), drag.additive);
    cancelDrag(false);
  });
  window.addEventListener('blur', () => { cancelDrag(); spaceHeld = false; view.classList.remove('pan-ready'); gestureScale = null; });
  view.addEventListener('dblclick', e => { if (e.button === 0 && !measuring && !panned) resetView(); });

  // ------------------------------------------------------------ measuring (SVG units are mm)
  let unit = 'mm', imperialUnit = 'mil', measuring = false, pts = [], measurements = [], selectedMeasurement = null, hover = null, mg = null;
  let lastPointer = null, snapping = false;
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  const snapModifier = e => mac ? e.metaKey : e.ctrlKey;
  const fmt = v => distance(v, unit);
  const toSvg = e => viewPoint(e);
  const toScreen = p => { const q = new DOMPoint(p.x, p.y).matrixTransform(svg.getScreenCTM()); const r = view.getBoundingClientRect(); return { x: q.x - r.left, y: q.y - r.top }; };
  function marker(p, pointer = false) {
    const g = document.createElementNS(NS, 'g'); g.classList.add('measurement-marker');
    g.dataset.x = p.x; g.dataset.y = p.y;
    if (pointer) { g.id = 'measure-pointer'; g.dataset.snapped = String(Boolean(p.snapped)); }
    g.append(document.createElementNS(NS, 'circle'), document.createElementNS(NS, 'path'));
    return g;
  }
  function refreshHover(e) {
    if (e) { lastPointer = { clientX: e.clientX, clientY: e.clientY }; snapping = Boolean(snapModifier(e)); }
    if (measuring && svg && !is3D()) hover = snapPoint(toSvg(lastPointer || centerPoint()), { ...activeBoard, groups }, snapping);
  }
  function positionMeasurementLabels() {
    if (!svg || is3D() || !mg) return;
    if (!mg.childElementCount && !label.childElementCount) { updateMeasurementReadout(); return; }
    const scale = Math.hypot(svg.getScreenCTM().a, svg.getScreenCTM().b);
    for (const g of mg.querySelectorAll('.measurement-marker')) {
      const p = g.id === 'measure-pointer' && hover ? hover : { x: g.dataset.x, y: g.dataset.y };
      g.setAttribute('transform', `translate(${p.x} ${p.y})`);
      g.querySelector('circle').setAttribute('r', 6 / scale);
      const r = 10 / scale; g.querySelector('path').setAttribute('d', `M${-r} 0H${r}M0 ${-r}V${r}`);
      if (g.id === 'measure-pointer') g.dataset.snapped = String(Boolean(p.snapped));
    }
    const preview = mg.querySelector('.measurement-preview');
    if (preview && hover) { preview.setAttribute('x2', hover.x); preview.setAttribute('y2', hover.y); }
    for (const caption of label.children) {
      const pair = caption.dataset.index === 'preview' ? pts.length && hover && [pts[0], hover] : measurements[Number(caption.dataset.index)];
      if (!pair) continue;
      if (caption.dataset.index === 'preview') caption.textContent = fmt(Math.hypot(pair[1].x - pair[0].x, pair[1].y - pair[0].y));
      const [a, b] = pair.map(toScreen);
      const point = labelPosition(a, b, { w: caption.offsetWidth, h: caption.offsetHeight }, caption.dataset.kind);
      caption.style.left = point.x + 'px'; caption.style.top = point.y + 'px';
    }
    updateMeasurementReadout();
  }
  function measurementLine(a, b, cls) {
    const line = document.createElementNS(NS, 'line'); line.setAttribute('class', cls);
    for (const [key, value] of Object.entries({ x1: a.x, y1: a.y, x2: b.x, y2: b.y })) line.setAttribute(key, value);
    return line;
  }
  function measurementLabel(text, index, kind = 'total') {
    const caption = document.createElement('div'); caption.className = 'measure-label';
    caption.dataset.index = index; caption.dataset.kind = kind; caption.textContent = text; label.append(caption);
  }
  function draw() {
    if (!mg || is3D()) return;
    mg.replaceChildren(); label.replaceChildren();
    for (let index = 0; index < measurements.length; index++) {
      const [a, b] = measurements[index];
      const chosen = index === selectedMeasurement;
      mg.append(measurementLine(a, b, 'measurement-segment' + (chosen ? ' selected' : '')), marker(a), marker(b));
      measurementLabel(fmt(Math.hypot(b.x - a.x, b.y - a.y)), index);
      if (chosen) {
        const corner = { x: b.x, y: a.y };
        mg.append(measurementLine(a, corner, 'measurement-axis'), measurementLine(corner, b, 'measurement-axis'));
        measurementLabel('X: ' + fmt(Math.abs(b.x - a.x)), index, 'x');
        measurementLabel('Y: ' + fmt(Math.abs(b.y - a.y)), index, 'y');
      }
    }
    if (measuring && hover) {
      if (pts.length) {
        mg.append(measurementLine(pts[0], hover, 'measurement-preview'), marker(pts[0]));
        measurementLabel(fmt(Math.hypot(hover.x - pts[0].x, hover.y - pts[0].y)), 'preview');
      }
      mg.append(marker(hover, true));
    }
    positionMeasurementLabels();
  }
  function updateMeasurementReadout() {
    const latest = pts.length && hover ? [pts[0], hover] : measurements[selectedMeasurement ?? measurements.length - 1];
    if (!latest) {
      const p = pts[0] || (measuring && hover);
      readout.textContent = p ? `cursor  ${fmt(p.x)}, ${fmt(-p.y)}` : ''; return;
    }
    const [a, b] = latest, dx = b.x - a.x, dy = b.y - a.y;
    readout.textContent = `dist ${fmt(Math.hypot(dx, dy))}\ndx   ${fmt(Math.abs(dx))}\ndy   ${fmt(Math.abs(dy))}\nang  ${(Math.atan2(-dy, dx) * 180 / Math.PI).toFixed(1)}°`;
  }
  function setMeasuring(v) {
    if (v && viewLoading()) return;
    cancelDrag();
    v = Boolean(v && svg && !is3D() && activeBoard?.kind !== 'schematic');
    measuring = v; $('measure-btn').classList.toggle('on', v); view.classList.toggle('measuring', v);
    pts = []; hover = null;
    if (v) refreshHover();
    draw();
  }
  function updateUnits() {
    document.querySelectorAll('.unit').forEach(b => b.classList.toggle('on', b.dataset.unit === unit));
    $('units-toggle').textContent = unit === 'mm' ? 'Metric (mm)' : `Imperial (${unit})`;
    $('units-toggle').disabled = !svg || is3D() || activeBoard?.kind !== 'pcb';
    $('units-toggle').setAttribute('aria-pressed', String(unit !== 'mm'));
  }
  function setUnit(next) {
    if (!UNITS[next]) return;
    unit = next; if (unit !== 'mm') imperialUnit = unit;
    updateUnits(); draw();
  }
  function toggleUnits() { if (svg && !is3D() && activeBoard?.kind === 'pcb') setUnit(unit === 'mm' ? imperialUnit : 'mm'); }
  $('measure-btn').onclick = () => setMeasuring(!measuring);
  $('units-toggle').onclick = toggleUnits;
  window.addEventListener('keydown', e => {
    if (measuring && (e.key === 'Meta' || e.key === 'Control')) { snapping = Boolean(snapModifier(e)); refreshHover(); draw(); }
    const editing = e.target.closest('textarea, select, [contenteditable], input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="file"])');
    if (!editing && !is3D() && e.code === 'Space' && !e.ctrlKey && !e.metaKey && !e.altKey &&
        (e.target === document.body || view.contains(e.target))) {
      e.preventDefault(); spaceHeld = true; view.classList.add('pan-ready'); return;
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'o') { e.preventDefault(); $('file').click(); return; }
    if (!editing && !e.altKey && !e.metaKey && !e.ctrlKey) {
      if ((e.key === '2' || e.key === '3') && !e.shiftKey && !e.isComposing && !$('view-mode-switch').disabled) {
        e.preventDefault();
        const mode = e.key === '2' ? '2d' : '3d';
        if (activeBoard.mode !== mode) setViewMode(mode);
        return;
      }
      if (e.key.toLowerCase() === 'f') { e.preventDefault(); resetView(); return; }
      if (e.key === '+' || e.key === '=') { e.preventDefault(); $('zoom-in').click(); return; }
      if (e.key === '-') { e.preventDefault(); $('zoom-out').click(); return; }
    }
    if (is3D()) {
      if (e.target.closest('input, textarea, [contenteditable]') || e.target.closest('select') && e.target !== $('three-body-list')) return;
      if (e.key === 'Escape') { e.preventDefault(); clearBodies(); }
      if (e.key === 'Delete' || e.key === 'Backspace') {
        // A tab's Delete key belongs to the existing tab-close shortcut.
        if (!e.target.closest('[role="tab"]')) { e.preventDefault(); deleteBodies(); }
      }
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'z') { e.preventDefault(); undoBodies(); }
      return;
    }
    if (e.key === 'Escape') {
      cancelDrag();
      selectedMeasurement = null;
      clearSchematicSelection();
      clearComponent();
      e.preventDefault();
      clearNet(); setMeasuring(false);
      return;
    }
    if (editing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'm' || e.key === 'M') setMeasuring(!measuring);
    if (e.key.toLowerCase() === 'q') { e.preventDefault(); toggleUnits(); }
    if (e.shiftKey && e.key.toLowerCase() === 'c') { measurements = []; pts = []; selectedMeasurement = null; refreshHover(); draw(); }
  });
  window.addEventListener('keyup', e => {
    if (e.code === 'Space') { spaceHeld = false; view.classList.remove('pan-ready'); }
    if (measuring && (e.key === 'Meta' || e.key === 'Control')) { snapping = Boolean(snapModifier(e)); refreshHover(); draw(); }
  });
  window.addEventListener('blur', () => { snapping = false; if (measuring) { refreshHover(); draw(); } });
  document.querySelectorAll('.unit').forEach(b => b.onclick = () => setUnit(b.dataset.unit));
  view.addEventListener('click', e => {
    if (!svg || is3D() || activeBoard.kind !== 'pcb' || panned || e.button !== 0 || measuring && (pts.length || snapModifier(e))) return;
    const rect = view.getBoundingClientRect();
    const hit = segmentAt({ x: e.clientX - rect.left, y: e.clientY - rect.top }, measurements, toScreen);
    if (hit !== null) {
      e.preventDefault(); e.stopImmediatePropagation();
      selectedMeasurement = selectedMeasurement === hit ? null : hit;
      if (measuring) refreshHover(e);
      draw();
    } else if (selectedMeasurement !== null) { selectedMeasurement = null; draw(); }
  }, true);
  view.addEventListener('click', e => {
    if (is3D()) return;
    if (!svg || !measuring || panned || e.button !== 0) return;
    refreshHover(e); selectedMeasurement = null;
    pts.push({ x: hover.x, y: hover.y });
    if (pts.length === 2) { measurements.push(pts); pts = []; }
    draw();
  });
  view.addEventListener('mousemove', e => {
    lastPointer = { clientX: e.clientX, clientY: e.clientY };
    if (svg && !is3D() && measuring) { refreshHover(e); draw(); }
  });

  new ResizeObserver(() => {
    positionMeasurementLabels();
    paintGPU();
    updateNetLabels();
  }).observe(view);

  // ------------------------------------------------------------ board passed on the command line
  fetch('/initial').then(r => r.ok ? r.json() : null).then(d => {
    const documents = d?.documents || (d?.svg ? [d] : []), activate = !activeBoard, opened = [];
    for (const data of documents) {
      if (!data.svg) continue;
      const file = new File([Uint8Array.from(atob(data.source || ''), c => c.charCodeAt(0))], data.name);
      const board = createBoard(file); opened.push(board); show(board, data);
    }
    if (activate && opened.length) activateBoard(opened.at(-1)); else updateTabs();
  }).catch(e => toast('Could not load the initial document: ' + e.message));
})();
