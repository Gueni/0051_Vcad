// Test each graphic separately: a net's disconnected branches must not turn
// all the empty space between them into a selection target.
const GRAPHICS = 'path, rect, circle, ellipse, line, polyline, polygon, text, use, image';
const ignored = el => el.closest('defs, clipPath, mask, .sch-hit, .sch-component-hit, [data-hole-owner], [data-primitive$="-hole"]');
const descendants = new WeakMap();
function graphics(elements) {
  const result = new Set();
  for (const el of elements) {
    let items = descendants.get(el);
    if (!items) {
      items = [el, ...el.querySelectorAll(GRAPHICS)].filter(child => child.matches(GRAPHICS) && !ignored(child));
      descendants.set(el, items);
    }
    for (const child of items) result.add(child);
  }
  return result;
}
function overlaps(a, b) {
  return a.left <= b.right && a.right >= b.left && a.top <= b.bottom && a.bottom >= b.top;
}
function lineTouches(el, box) {
  const transform = el.getScreenCTM();
  const a = new DOMPoint(el.x1.baseVal.value, el.y1.baseVal.value).matrixTransform(transform);
  const b = new DOMPoint(el.x2.baseVal.value, el.y2.baseVal.value).matrixTransform(transform);
  // Clip the centreline against the rectangle, allowing for its drawn width.
  const width = parseFloat(getComputedStyle(el).strokeWidth) || 0;
  const pad = width * Math.max(Math.hypot(transform.a, transform.b), Math.hypot(transform.c, transform.d)) / 2;
  let start = 0, end = 1;
  for (const [origin, delta, low, high] of [[a.x, b.x - a.x, box.left - pad, box.right + pad],
    [a.y, b.y - a.y, box.top - pad, box.bottom + pad]]) {
    if (!delta) { if (origin < low || origin > high) return false; continue; }
    const first = (low - origin) / delta, last = (high - origin) / delta;
    start = Math.max(start, Math.min(first, last)); end = Math.min(end, Math.max(first, last));
    if (start > end) return false;
  }
  return true;
}
export function rectangleMatches(board, componentIndex, netIndex, box) {
  const result = { components: new Set(), nets: new Set() }, hits = new Map();
  const matrix = board.svg.getScreenCTM(), worldBounds = board.gpuScene?.caption_bounds;
  function touches(el) {
    if (hits.has(el)) return hits.get(el);
    const world = worldBounds?.[el.id];
    // The compiler already provides conservative world bounds for copper.
    // Reject these in JS before requesting any DOM layout/style information.
    if (world) {
      const xs = [], ys = [];
      for (const x of [world[0], world[0] + world[2]]) for (const y of [world[1], world[1] + world[3]]) {
        xs.push(matrix.a * x + matrix.c * y + matrix.e); ys.push(matrix.b * x + matrix.d * y + matrix.f);
      }
      if (!overlaps({ left: Math.min(...xs), right: Math.max(...xs), top: Math.min(...ys), bottom: Math.max(...ys) }, box)) {
        hits.set(el, false); return false;
      }
    }
    const r = el.getBoundingClientRect();
    let hit = false;
    if ((r.width || r.height) && overlaps(r, box) && !(board.polygonsHidden && el.closest('.polygon-copper'))) {
      const paint = getComputedStyle(el);
      const visible = board.svg.closest('.gpu-layout') ? !board.gpuHidden?.has(el) : paint.visibility === 'visible';
      hit = visible && (el.localName === 'image' || paint.fill !== 'none' || paint.stroke !== 'none') &&
        (el.localName !== 'line' || lineTouches(el, box));
    }
    hits.set(el, hit); return hit;
  }
  for (const [index, selected, component] of [[componentIndex, result.components, true], [netIndex, result.nets, false]]) {
    for (const [id, value] of index) {
      const elements = board.kind === 'schematic' ? component && value.bodyElements.length ? value.bodyElements : value.elements : value;
      if ([...graphics(elements)].some(touches)) selected.add(id);
    }
  }
  return result;
}
