// Screen-space net and component pin captions leave SVG hit targets unchanged.
function captionBounds(shape, paint, primitive) {
  const bounds = shape.getBBox();
  if (shape.localName !== 'circle' || !['pad', 'via'].includes(primitive) || paint.stroke === 'none') return bounds;
  // SVG getBBox omits stroke width; annular pads/vias use a circular stroke.
  const radius = (parseFloat(paint.strokeWidth) || 0) / 2;
  return { x: bounds.x - radius, y: bounds.y - radius, width: bounds.width + 2 * radius, height: bounds.height + 2 * radius };
}

export class NetLabels {
  constructor(container) {
    this.container = container;
    this.candidates = new WeakMap();
    this.bounds = new WeakMap();
    this.hidden = new WeakMap();
    this.widths = new Map();
    this.context = document.createElement('canvas').getContext('2d');
    const font = getComputedStyle(container);
    this.context.font = `${font.fontWeight} ${font.fontSize} ${font.fontFamily}`;
  }

  setBounds(svg, bounds, hidden = new WeakSet()) { this.bounds.set(svg, bounds); this.hidden.set(svg, hidden); this.candidates.delete(svg); }

  *index(svg) {
    if (this.candidates.has(svg)) return this.candidates.get(svg);
    const candidates = [];
    let deadline = performance.now() + 4;
    for (const shape of svg.querySelectorAll('#scene path, #scene rect, #scene circle, #scene ellipse, #scene line, #scene polyline, #scene polygon, #scene use')) {
      if (performance.now() > deadline) { yield; deadline = performance.now() + 4; }
      const net = shape.closest('[data-net]');
      const pad = shape.closest('[data-pad-number], [data-pad-designator]');
      const number = pad?.dataset.padNumber || pad?.dataset.padDesignator || '';
      const pin = shape.closest('[data-component]')?.dataset.component &&
        !['via', 'track', 'arc'].includes(pad?.dataset.primitive) ? number : '';
      const source = net || pad;
      if ((!net?.dataset.net && !pin.trim()) || shape.closest('defs, clipPath, mask, [data-hole-owner], [data-primitive$="-hole"], [data-overlay-kind], #component-highlight, #net-highlight')) continue;
      const layer = source.closest('[data-layer-role]');
      if (layer && layer.dataset.layerRole !== 'copper') continue;
      const worldBounds = this.bounds.get(svg)?.[shape.id];
      if (worldBounds) {
        candidates.push({ shape, name: net?.dataset.net || '', pin: pin.trim() ? pin : '', worldBounds,
          via: source.dataset.primitive === 'via', primitive: source.dataset.primitive });
        continue;
      }
      const paint = getComputedStyle(shape), bounds = captionBounds(shape, paint, source.dataset.primitive);
      if (!bounds.width && !bounds.height || paint.fill === 'none' && paint.stroke === 'none') continue;
      const route = !['pad', 'via'].includes(source.dataset.primitive) && paint.stroke !== 'none' && typeof shape.getTotalLength === 'function' &&
        (paint.fill === 'none' || shape.matches('line, polyline') || ['track', 'arc'].includes(source.dataset.primitive));
      candidates.push({ shape, name: net?.dataset.net || '', pin: pin.trim() ? pin : '', bounds, route, via: source.dataset.primitive === 'via',
        length: route ? shape.getTotalLength() : 0, stroke: parseFloat(paint.strokeWidth) || 0,
        fixedStroke: paint.vectorEffect === 'non-scaling-stroke' });
    }
    this.candidates.set(svg, candidates);
    return candidates;
  }

  cancel() {
    clearTimeout(this.timer); this.generation = (this.generation || 0) + 1;
    this.container.dataset.pending = '0';
  }

  update(board, deferred = false) {
    this.cancel();
    this.container.replaceChildren();
    const generation = this.generation, work = this.layout(board, deferred);
    if (!deferred) { while (!work.next().done) { /* SVG compatibility path */ } return; }
    this.container.dataset.pending = '1';
    const step = () => {
      if (generation !== this.generation) return;
      if (work.next().done) { this.container.dataset.pending = '0'; return; }
      this.timer = setTimeout(step, 0);
    };
    this.timer = setTimeout(step, 0);
  }

  *layout(board, gpu = false) {
    const svg = board?.svg;
    // At fit-to-board scale the labels would obscure the layout overview.
    if (board?.kind !== 'pcb' || board.mode === '3d' || !svg?.isConnected || board.home.w / svg.viewBox.baseVal.width < 2) return;
    const view = this.container.getBoundingClientRect(), occupied = [], fragment = document.createDocumentFragment();
    const candidates = yield* this.index(svg);
    const rootMatrix = svg.getScreenCTM();
    let deadline = performance.now() + 4;
    for (const item of candidates) {
      if (performance.now() > deadline) { yield; deadline = performance.now() + 4; }
      if (occupied.length >= 150) break;
      const { shape, name, pin } = item;
      if (board.polygonsHidden && shape.closest('.polygon-copper') ||
          svg.classList.contains('layout-selection') && !shape.closest('.net-match, .component-match')) continue;
      const world = item.worldBounds;
      const rect = world ? { left: world[0] * rootMatrix.a + rootMatrix.e, top: world[1] * rootMatrix.d + rootMatrix.f,
        right: (world[0] + world[2]) * rootMatrix.a + rootMatrix.e, bottom: (world[1] + world[3]) * rootMatrix.d + rootMatrix.f,
        width: world[2] * rootMatrix.a, height: world[3] * rootMatrix.d } : shape.getBoundingClientRect();
      if (rect.right < view.left || rect.left > view.right || rect.bottom < view.top || rect.top > view.bottom) continue;
      if (world) {
        const minimumWidth = Math.min(...[pin, name].filter(Boolean).map(text => {
          if (!this.widths.has(text)) this.widths.set(text, this.context.measureText(text).width + 4);
          return this.widths.get(text);
        }));
        if (rect.width < minimumWidth || rect.height < 18) continue;
      }
      const paint = getComputedStyle(shape);
      if (paint.display === 'none' || (gpu ? this.hidden.get(svg)?.has(shape) : paint.visibility !== 'visible') || !shape.getClientRects().length) continue;
      const matrix = shape.getScreenCTM();
      if (!matrix) continue;
      if (world && !item.bounds) {
        item.bounds = captionBounds(shape, paint, item.primitive); item.stroke = parseFloat(paint.strokeWidth) || 0;
        item.fixedStroke = paint.vectorEffect === 'non-scaling-stroke';
        item.route = !['pad', 'via'].includes(item.primitive) && paint.stroke !== 'none' && typeof shape.getTotalLength === 'function' &&
          (paint.fill === 'none' || shape.matches('line, polyline') || ['track', 'arc'].includes(item.primitive));
        item.length = item.route ? shape.getTotalLength() : 0;
      }
      const { bounds, route } = item;
      const scale = Math.min(Math.hypot(matrix.a, matrix.b), Math.hypot(matrix.c, matrix.d));
      const pinRow = { text: pin, kind: 'pin-label' }, netRow = { text: name, kind: 'net-label' };
      // Prefer both rows, then preserve the pin number when the pad is cramped.
      const variants = pin && name ? [[pinRow, netRow], [pinRow], [netRow]] : [[pin ? pinRow : netRow]];
      let placed = false;
      for (const rows of variants) {
        const width = Math.max(...rows.map(row => {
          if (!this.widths.has(row.text)) this.widths.set(row.text, this.context.measureText(row.text).width + 4);
          return this.widths.get(row.text);
        }));
        const height = rows.length * 16 + 2;
        if (item.via && (rect.width < width || rect.height < height)) continue;
        if (route && ((item.fixedStroke ? item.stroke : item.stroke * scale) < height + 2 || item.length * scale < width + 8)) continue;
        const anchors = [];
        if (route) {
          const start = shape.getPointAtLength(0).matrixTransform(matrix);
          const end = shape.getPointAtLength(item.length).matrixTransform(matrix);
          const dx = end.x - start.x, dy = end.y - start.y;
          const fraction = ((view.left + view.width / 2 - start.x) * dx + (view.top + view.height / 2 - start.y) * dy) / (dx * dx + dy * dy);
          for (const f of [Number.isFinite(fraction) ? Math.max(0, Math.min(1, fraction)) : 0.5, 0.5, 0.25, 0.75]) {
            const distance = item.length * f;
            const point = shape.getPointAtLength(distance).matrixTransform(matrix);
            const a = shape.getPointAtLength(Math.max(0, distance - item.length * 0.01)).matrixTransform(matrix);
            const b = shape.getPointAtLength(Math.min(item.length, distance + item.length * 0.01)).matrixTransform(matrix);
            anchors.push({ x: point.x, y: point.y, angle: Math.atan2(b.y - a.y, b.x - a.x) });
          }
        } else {
          // Additional anchors let labels avoid through-hole cutouts and concave fills.
          for (const [x, y] of [[0.5, 0.5], [0.5, 0.25], [0.5, 0.75], [0.25, 0.5], [0.75, 0.5]]) {
            const point = new DOMPoint(bounds.x + bounds.width * x, bounds.y + bounds.height * y).matrixTransform(matrix);
            anchors.push({ x: point.x, y: point.y, angle: 0 });
          }
          if (item.via) {
            // Place the caption as far out on the annulus as its corners allow.
            // Screen coordinates keep these candidates upright on mirrored vias.
            const rx = rect.width / 2, ry = rect.height / 2;
            const dx = rx * Math.sqrt(Math.max(0, 1 - (height / 2 / ry) ** 2)) - width / 2 - 0.5;
            const dy = ry * Math.sqrt(Math.max(0, 1 - (width / 2 / rx) ** 2)) - height / 2 - 0.5;
            for (const [x, y] of [[0, -dy], [0, dy], [-dx, 0], [dx, 0]]) {
              if (dx > 0 && dy > 0) anchors.push({ x: rect.left + rx + x, y: rect.top + ry + y, angle: 0 });
            }
          }
        }
        for (const anchor of anchors) {
          let { angle } = anchor;
          if (angle > Math.PI / 2) angle -= Math.PI;
          if (angle < -Math.PI / 2) angle += Math.PI;
          const cos = Math.cos(angle), sin = Math.sin(angle);
          const w = Math.abs(cos) * width + Math.abs(sin) * height;
          const h = Math.abs(sin) * width + Math.abs(cos) * height;
          const box = { left: anchor.x - w / 2, right: anchor.x + w / 2, top: anchor.y - h / 2, bottom: anchor.y + h / 2 };
          if (box.left < view.left + 2 || box.right > view.right - 2 || box.top < view.top + 2 || box.bottom > view.bottom - 2 ||
              occupied.some(other => box.left < other.right + 6 && box.right > other.left - 6 && box.top < other.bottom + 6 && box.bottom > other.top - 6)) continue;
          // Hit testing also respects clipping, hidden ancestors, holes and paint order.
          const probes = [-1, 0, 1].flatMap(x => [-1, 0, 1].map(y => [x * width / 2, y * height / 2]));
          if (!probes.every(([x, y]) => {
            const hit = document.elementFromPoint(anchor.x + x * cos - y * sin, anchor.y + x * sin + y * cos);
            return hit === shape || shape.contains(hit);
          })) continue;
          const caption = document.createElement('span'); caption.className = 'copper-label';
          if (rows.length === 1) {
            caption.classList.add(rows[0].kind); caption.textContent = rows[0].text;
          } else for (const row of rows) {
            const line = document.createElement('span'); line.className = row.kind; line.textContent = row.text;
            caption.append(line);
          }
          caption.style.left = anchor.x - view.left + 'px'; caption.style.top = anchor.y - view.top + 'px';
          caption.style.transform = `translate(-50%, -50%) rotate(${angle}rad)`;
          fragment.append(caption); occupied.push(box); placed = true;
          break;
        }
        if (placed) break;
      }
    }
    this.container.append(fragment);
  }
}
