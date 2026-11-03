// Immutable document metadata indexes. Selections and visibility never change
// connectivity; new document/SVG instances get fresh indexes and pair caches.
export const identity = value => String(value || '').trim().toLowerCase();
const padKey = (component, pin) => JSON.stringify([identity(component), identity(pin)]);
const add = (map, key, value) => { if (!map.has(key)) map.set(key, []); map.get(key).push(value); };

export function indexLayout(elements, links) {
  const names = new Set(), netNames = new Set(), padNets = new Map();
  for (const { dataset } of elements) {
    const { component, padNumber, padDesignator, net } = dataset;
    if (component) names.add(component);
    if (net) netNames.add(net);
    if (!component || !(padNumber || padDesignator) || !net) continue;
    const key = padKey(component, padNumber || padDesignator);
    if (!padNets.has(key)) padNets.set(key, new Set());
    padNets.get(key).add(net);
  }
  const byName = new Map(), sourceLinks = new Map(), sourcesByName = new Map();
  for (const link of links) {
    const name = identity(link.name);
    // Keep the original first-row semantics for duplicate metadata names.
    if (!byName.has(name)) byName.set(name, link);
    if (link.source_id) {
      const source = identity(link.source_id);
      if (!sourceLinks.has(source)) sourceLinks.set(source, []);
      if (!sourcesByName.has(name)) sourcesByName.set(name, new Set());
      sourcesByName.get(name).add(source);
    }
  }
  const fallback = new Map(), unlinked = new Map();
  for (const name of names) {
    const link = byName.get(identity(name)), source = identity(link?.source_name || name);
    add(fallback, source, name);
    if (!link?.source_id) add(unlinked, source, name);
    for (const id of sourcesByName.get(identity(name)) || []) sourceLinks.get(id).push(name);
  }
  const netsByName = new Map(), netRank = new Map();
  for (const name of netNames) { netRank.set(name, netRank.size); add(netsByName, identity(name), name); }
  return { byName, sourceLinks, fallback, unlinked, netsByName, netRank, padNets };
}

export function indexSchematic(components, nets) {
  const byName = new Map(), byId = new Map();
  for (const [id, row] of components) { add(byName, identity(row.name), [id, row]); add(byId, identity(id), id); }
  return { byName, byId, nets, pairs: new WeakMap() };
}

export function matchingComponents(sourceId, row, layout) {
  const key = identity(sourceId);
  // A saved link with no geometry is still authoritative: never fall back
  // to a different designator when an explicit source-ID match exists.
  if (sourceId && layout.sourceLinks.has(key)) return layout.sourceLinks.get(key);
  return (sourceId ? layout.unlinked : layout.fallback).get(identity(row.name)) || [];
}

export function schematicComponents(name, schematic, layout) {
  const link = layout.byName.get(identity(name));
  return link?.source_id ? schematic.byId.get(identity(link.source_id)) || [] :
    (schematic.byName.get(identity(link?.source_name || name)) || []).map(([id]) => id);
}

function pair(schematic, layout) {
  if (!schematic.pairs.has(layout)) schematic.pairs.set(layout, { forward: new Map(), reverse: null, ambiguous: new Set() });
  return schematic.pairs.get(layout);
}

export function matchingNets(id, row, schematic, layout) {
  const cached = pair(schematic, layout);
  if (cached.forward.has(id)) return cached.forward.get(id);
  const byPins = new Set();
  for (const terminal of row.terminals) {
    if (!terminal.component || !terminal.pin) continue;
    const component = schematic.byName.get(identity(terminal.component))?.[0];
    const matches = matchingComponents(component?.[0] || '', component?.[1] || { name: terminal.component }, layout);
    for (const name of matches) for (const net of layout.padNets.get(padKey(name, terminal.pin)) || []) byPins.add(net);
  }
  const matches = byPins.size ? [...byPins] : [...new Set([row.name, ...row.aliases].flatMap(alias =>
    layout.netsByName.get(identity(alias)) || []))].sort((a,b) => layout.netRank.get(a) - layout.netRank.get(b));
  cached.forward.set(id, matches);
  return matches;
}

export function reverseNets(schematic, layout) {
  const cached = pair(schematic, layout);
  if (!cached.reverse) {
    cached.reverse = new Map();
    for (const [id, row] of schematic.nets) {
      const names = matchingNets(id, row, schematic, layout);
      if (names.length === 1) add(cached.reverse, names[0], id);
      else for (const name of names) cached.ambiguous.add(name);
    }
  }
  return cached;
}
