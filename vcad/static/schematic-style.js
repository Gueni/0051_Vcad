// Schematic contour, net and context styling for the browser viewer.
const NS = 'http://www.w3.org/2000/svg';
const GREEN = '#008a3b', BACKGROUND = [255, 252, 248], CONTRAST = .45;
const paints = new WeakMap();
function fade(value) {
  const channels = value.match(/^rgba?\(([^)]+)\)$/)?.[1].split(',').map(Number);
  if (!channels || channels[3] === 0) return value;
  const rgb = channels.slice(0, 3).map((channel, i) => Math.round(BACKGROUND[i] + (channel - BACKGROUND[i]) * CONTRAST));
  return `rgba(${rgb.join(',')},${channels[3] ?? 1})`;
}
const painted = value => value !== 'none' && value !== 'transparent' && !/^rgba\([^)]*,\s*0\)$/.test(value);

export function styleSchematic(board) {
  const svg = board.svg, scene = svg.querySelector('#scene');
  svg.querySelector('#schematic-highlight')?.remove();
  const selected = board.selectedComponents.size || board.selectedNets.size;
  let rows = paints.get(svg);
  if (!rows && !selected) return;
  if (!rows) {
    rows = [...scene.querySelectorAll('path, rect, circle, ellipse, line, polyline, polygon, text, tspan, use')]
      .filter(el => !el.closest('defs, clipPath, mask, .sch-hit, .sch-component-hit'))
      .map(el => {
        const paint = getComputedStyle(el);
        return { el, style: el.getAttribute('style'), fill: paint.fill, stroke: paint.stroke, width: paint.strokeWidth };
      });
    paints.set(svg, rows);
  }
  const highlighted = new Set();
  for (const row of rows) {
    const { el, style, fill, stroke, width } = row;
    if (style === null) el.removeAttribute('style'); else el.setAttribute('style', style);
    if (!selected) continue;
    const component = Boolean(el.closest('.sch-component-match')), net = Boolean(el.closest('.sch-net-match'));
    const glyph = el.matches('text, tspan') || el.closest('text, [data-primitive="text"]');
    const contour = component && !glyph;
    for (const [property, value] of [['fill', fill], ['stroke', stroke]]) {
      el.style.setProperty(property, !painted(value) || contour && property === 'fill' ? value :
        component || net ? GREEN : fade(value), 'important');
    }
    if (contour && !painted(stroke) && painted(fill)) el.style.setProperty('stroke', GREEN, 'important');
    if (net && painted(stroke)) el.style.setProperty('stroke-width', `${parseFloat(width) * 3.5}px`, 'important');
    if (component || net) highlighted.add(el);
  }
  svg.classList.toggle('schematic-selection', Boolean(selected));
  if (!highlighted.size) return;
  const branches = new Set();
  for (const el of highlighted) for (let node = el; node && node !== svg; node = node.parentElement) branches.add(node);
  function copyBranch(node) {
    if (node.matches('.sch-hit, .sch-component-hit')) return null;
    const copy = node.cloneNode(highlighted.has(node));
    if (!highlighted.has(node)) for (const child of node.children) {
      if (!branches.has(child)) continue;
      const branch = copyBranch(child); if (branch) copy.append(branch);
    }
    return copy;
  }
  const overlay = document.createElementNS(NS, 'g'); overlay.id = 'schematic-highlight';
  overlay.style.pointerEvents = 'none'; overlay.setAttribute('aria-hidden', 'true');
  const copy = copyBranch(scene);
  for (const el of [copy, ...copy.querySelectorAll('*')]) {
    el.style.setProperty('pointer-events', 'none', 'important');
    el.removeAttribute('id'); el.classList.remove('sch-component-match', 'sch-net-match');
    for (const attr of [...el.attributes]) if (attr.name.startsWith('data-')) el.removeAttribute(attr.name);
  }
  overlay.append(copy); svg.append(overlay);
}
