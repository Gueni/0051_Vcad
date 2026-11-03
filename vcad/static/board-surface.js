// Rasterize the real outer PCB layers once per prepared 3D scene. Interior copper,
// selection overlays, measurements and 2D visibility never enter the finish.
const NS = 'http://www.w3.org/2000/svg';
const MASK = '#0e452c', COPPER_UNDER_MASK = '#104c30', METAL = '#b8aa7e', INK = '#eeeade';

function token(group) { return (group.dataset.layerToken || '').toUpperCase(); }
function layer(group, side, role) {
  const front = side === 'top', name = token(group);
  if (role === 'copper') return name === (front ? 'TOP' : 'BOTTOM') || name === (front ? 'F.CU' : 'B.CU');
  if (role === 'mask') return name === (front ? 'TOPSOLDER' : 'BOTTOMSOLDER') || name === (front ? 'F.MASK' : 'B.MASK');
  return name === (front ? 'TOPOVERLAY' : 'BOTTOMOVERLAY') || name === (front ? 'F.SILKS' : 'B.SILKS');
}

function drawing(source, groups, window, size) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('xmlns', NS); svg.setAttribute('width', size[0]); svg.setAttribute('height', size[1]);
  svg.setAttribute('viewBox', window.join(' '));
  for (const defs of source.querySelectorAll('defs')) svg.append(defs.cloneNode(true));
  for (const group of groups) {
    const copy = group.cloneNode(true);
    for (const node of [copy, ...copy.querySelectorAll('*')]) {
      node.removeAttribute('style'); node.removeAttribute('class');
      node.removeAttribute('display'); node.removeAttribute('visibility'); node.removeAttribute('opacity');
      // Preserve geometry, fill rules and explicit "none" paints, including
      // stroke-font silkscreen. Every visible paint becomes opaque white.
      for (const key of ['fill', 'stroke']) if (node.hasAttribute(key) && node.getAttribute(key) !== 'none' && !node.getAttribute(key).startsWith('url('))
        node.setAttribute(key, '#fff');
    }
    copy.setAttribute('fill', group.getAttribute('fill') === 'none' ? 'none' : '#fff');
    svg.append(copy);
  }
  return new XMLSerializer().serializeToString(svg);
}

async function raster(source, groups, window, size) {
  const canvas = document.createElement('canvas'); [canvas.width, canvas.height] = size;
  if (!groups.length) return canvas;
  const url = URL.createObjectURL(new Blob([drawing(source, groups, window, size)], { type: 'image/svg+xml' }));
  try {
    const image = new Image(); image.src = url; await image.decode();
    canvas.getContext('2d').drawImage(image, 0, 0);
  } finally { URL.revokeObjectURL(url); }
  return canvas;
}

function tinted(context, image, color) {
  const copy = document.createElement('canvas'); copy.width = image.width; copy.height = image.height;
  const paint = copy.getContext('2d'); paint.drawImage(image, 0, 0);
  paint.globalCompositeOperation = 'source-in'; paint.fillStyle = color; paint.fillRect(0, 0, copy.width, copy.height);
  context.drawImage(copy, 0, 0);
}

export async function boardSurfaces(source, model, maximum = 2048) {
  if (!source || !model.surface_origin) return [];
  const points = model.board.flatMap(part => part.outline), xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  const bounds = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  const width = bounds[2] - bounds[0], height = bounds[3] - bounds[1];
  if (width <= 0 || height <= 0) return [];
  const [x, y] = model.surface_origin, window = [x + bounds[0], y - bounds[3], width, height];
  const size = [Math.max(1, Math.round(maximum * width / Math.max(width, height))),
    Math.max(1, Math.round(maximum * height / Math.max(width, height)))];
  const groups = [...source.querySelectorAll('#scene > g[data-layer-token]')];
  const results = [];
  for (const side of ['top', 'bottom']) {
    const [copper, openings, silk] = await Promise.all(['copper', 'mask', 'silk'].map(role =>
      raster(source, groups.filter(group => layer(group, side, role)), window, size)));
    const exposed = openings.getContext('2d');
    exposed.globalCompositeOperation = 'destination-in'; exposed.drawImage(copper, 0, 0);
    // Where a document has copper but no mask layer, retain the copper relief
    // without pretending that every route is bare metal.
    const color = document.createElement('canvas'); [color.width, color.height] = size;
    const albedo = color.getContext('2d'); albedo.fillStyle = MASK; albedo.fillRect(0, 0, ...size);
    tinted(albedo, copper, COPPER_UNDER_MASK); tinted(albedo, openings, METAL); tinted(albedo, silk, INK);
    const physical = document.createElement('canvas'); [physical.width, physical.height] = size;
    const finish = physical.getContext('2d'); finish.fillStyle = 'rgb(0,145,0)'; finish.fillRect(0, 0, ...size);
    // Three.js roughness uses green, metalness uses blue in the same map.
    tinted(finish, openings, 'rgb(0,72,235)'); tinted(finish, silk, 'rgb(0,205,0)');
    results.push({ side, color, physical, bounds });
  }
  return results;
}
