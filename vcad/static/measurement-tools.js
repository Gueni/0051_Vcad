// Distances are stored in document millimetres; snapping/picking use CSS pixels.
export const UNITS = { mm: { f: 1, d: 3 }, mil: { f: 1000 / 25.4, d: 1 }, in: { f: 1 / 25.4, d: 4 } };
export const distance = (value, unit) => (value * UNITS[unit].f).toFixed(UNITS[unit].d) + ' ' + unit;
const indices = new WeakMap();

export function snapIndex(board) {
  if (board.gpuScene?.snap_points) return board.gpuScene.snap_points;
  if (indices.has(board.svg)) return indices.get(board.svg);
  const points = new Map();
  for (const shape of board.svg.querySelectorAll('#scene [data-primitive="pad"], #scene [data-primitive="via"]')) {
    const layer = shape.closest('#scene > g');
    if (!layer || !(layer.dataset.layerRole === 'copper' || ['signal', 'internal_plane'].includes(layer.dataset.layerFamily) ||
      /^(TOP|BOTTOM|MID\d+|PLANE\d+|MULTILAYER)$/i.test(layer.dataset.layerToken || '') ||
      /^(layer-)?(top|bottom|multilayer)$/i.test(layer.id))) continue;
    // Source SVG transforms give document coordinates even for hidden layers,
    // and avoid cancelling the viewport/Retina transform numerically.
    let transform = new DOMMatrix();
    for (let node = shape; node && node !== board.svg; node = node.parentElement) {
      const list = node.transform?.baseVal;
      let own = new DOMMatrix();
      for (let i = 0; list && i < list.numberOfItems; i++) {
        const m = list.getItem(i).matrix;
        own = own.multiply(new DOMMatrix([m.a, m.b, m.c, m.d, m.e, m.f]));
      }
      transform = own.multiply(transform);
    }
    const bounds = shape.getBBox();
    const explicit = shape.hasAttribute('data-snap-x') && shape.hasAttribute('data-snap-y');
    const center = new DOMPoint(explicit ? Number(shape.dataset.snapX) : bounds.x + bounds.width / 2,
      explicit ? Number(shape.dataset.snapY) : bounds.y + bounds.height / 2).matrixTransform(transform);
    const key = `${center.x},${center.y}`;
    if (!points.has(key)) points.set(key, { x: center.x, y: center.y, layers: [] });
    const row = points.get(key); if (!row.layers.includes(layer.id)) row.layers.push(layer.id);
  }
  const rows = [...points.values()].sort((a, b) => a.x - b.x); indices.set(board.svg, rows); return rows;
}

export function snapPoint(point, board, enabled, radius = 12) {
  if (!enabled) return { ...point, snapped: false };
  const matrix = board.svg.getScreenCTM(), scale = Math.hypot(matrix.a, matrix.b), reach = radius / scale;
  const visible = new Set(board.groups.filter(g => g.style.display !== 'none').map(g => g.id));
  const rows = snapIndex(board);
  let lo = 0, hi = rows.length;
  while (lo < hi) { const mid = (lo + hi) >>> 1; if (rows[mid].x < point.x - reach) lo = mid + 1; else hi = mid; }
  let closest = point, best = reach * reach;
  for (let i = lo; i < rows.length && rows[i].x <= point.x + reach; i++) {
    const row = rows[i]; if (!row.layers.some(layer => visible.has(layer))) continue;
    const squared = (row.x - point.x) ** 2 + (row.y - point.y) ** 2;
    if (squared <= best) { closest = row; best = squared; }
  }
  return { x: closest.x, y: closest.y, snapped: closest !== point };
}

export function segmentAt(point, segments, screen, radius = 6) {
  let selected = null, best = radius * radius;
  for (let i = segments.length - 1; i >= 0; i--) {
    const [a, b] = segments[i].map(screen), dx = b.x - a.x, dy = b.y - a.y;
    const length = dx * dx + dy * dy;
    const t = length ? Math.max(0, Math.min(1, ((point.x - a.x) * dx + (point.y - a.y) * dy) / length)) : 0;
    const squared = (point.x - a.x - t * dx) ** 2 + (point.y - a.y - t * dy) ** 2;
    if (squared < best) { selected = i; best = squared; }
  }
  return selected;
}

export function labelPosition(a, b, size, type = 'total') {
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  if (type === 'x') return { x: mid.x, y: a.y + (b.y > a.y ? -1 : 1) * (size.h / 2 + 14) };
  if (type === 'y') return { x: b.x + (b.x > a.x ? 1 : -1) * (size.w / 2 + 16), y: mid.y };
  const dx = b.x - a.x, dy = b.y - a.y, length = Math.hypot(dx, dy);
  let nx = length ? dy / length : 0, ny = length ? -dx / length : -1;
  const side = (b.x - mid.x) * nx + (a.y - mid.y) * ny;
  if (side > 0 || side === 0 && (ny > 0 || ny === 0 && nx < 0)) { nx = -nx; ny = -ny; }
  const offset = (Math.abs(nx) * size.w + Math.abs(ny) * size.h) / 2 + 16;
  return { x: mid.x + nx * offset, y: mid.y + ny * offset };
}
