// Keep converted data across tabs while bounding retained CPU memory. GPU
// geometries are shared per model by Board3D and retained with completed views.
const cache = new Map(); let cacheBytes = 0;
const cacheLimit = 64 * 1024 * 1024;
// Pre-tessellated meshes (3D PDF) arrive indexed without normals. Corners are
// smooth across shallow edges and flat across creases, then welded again.
const creaseCos = Math.cos(30 * Math.PI / 180);
function decodeMesh(mesh) {
  const decode = (text, Type) => text ? new Type(Uint8Array.from(atob(text), c => c.charCodeAt(0)).buffer) : null;
  const source = decode(mesh.position, Float32Array);
  const index = mesh.index16 ? decode(mesh.index16, Uint16Array) : decode(mesh.index, Uint32Array);
  const faceColor = decode(mesh.face_color, Float32Array);
  const faces = index.length / 3, vertices = source.length / 3;
  const faceNormal = new Float32Array(faces * 3), smooth = new Float32Array(source.length);
  for (let f = 0; f < faces; f++) {
    const a = index[f * 3] * 3, b = index[f * 3 + 1] * 3, c = index[f * 3 + 2] * 3;
    const ux = source[b] - source[a], uy = source[b + 1] - source[a + 1], uz = source[b + 2] - source[a + 2];
    const vx = source[c] - source[a], vy = source[c + 1] - source[a + 1], vz = source[c + 2] - source[a + 2];
    const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;  // area weighted
    for (const v of [a, b, c]) { smooth[v] += nx; smooth[v + 1] += ny; smooth[v + 2] += nz; }
    const length = Math.hypot(nx, ny, nz) || 1;
    faceNormal.set([nx / length, ny / length, nz / length], f * 3);
  }
  for (let v = 0; v < source.length; v += 3) {
    const length = Math.hypot(smooth[v], smooth[v + 1], smooth[v + 2]) || 1;
    smooth[v] /= length; smooth[v + 1] /= length; smooth[v + 2] /= length;
  }
  const position = new Float32Array(index.length * 3), normal = new Float32Array(index.length * 3);
  const color = faceColor && new Float32Array(index.length * 3), out = new Uint32Array(index.length);
  const head = new Int32Array(vertices).fill(-1), next = new Int32Array(index.length);
  let count = 0;
  for (let i = 0; i < index.length; i++) {
    const v = index[i], f = (i / 3) | 0, s = v * 3, n = f * 3;
    const flat = smooth[s] * faceNormal[n] + smooth[s + 1] * faceNormal[n + 1] + smooth[s + 2] * faceNormal[n + 2] < creaseCos;
    const normals = flat ? faceNormal : smooth, at = flat ? n : s;
    let o = head[v];
    for (; o >= 0; o = next[o]) {
      if (Math.abs(normal[o * 3] - normals[at]) + Math.abs(normal[o * 3 + 1] - normals[at + 1]) +
          Math.abs(normal[o * 3 + 2] - normals[at + 2]) > 1e-4) continue;
      if (color && (color[o * 3] !== faceColor[n] || color[o * 3 + 1] !== faceColor[n + 1] ||
                    color[o * 3 + 2] !== faceColor[n + 2])) continue;
      break;
    }
    if (o < 0) {
      o = count++; next[o] = head[v]; head[v] = o;
      position.set(source.subarray(s, s + 3), o * 3);
      normal.set(normals.subarray(at, at + 3), o * 3);
      if (color) color.set(faceColor.subarray(n, n + 3), o * 3);
    }
    out[i] = o;
  }
  return { position: position.slice(0, count * 3), normal: normal.slice(0, count * 3), index: out,
           color: color ? color.slice(0, count * 3) : null };
}
export class StepLoader {
  async load(key, asset) {
    if (asset?.mesh) return asset.mesh.map(decodeMesh);  // already tessellated (3D PDF)
    if (!asset?.data && !asset?.bytes) throw new Error('Missing STEP file; use Add STEP models');
    if (cache.has(key)) {
      const entry = cache.get(key); cache.delete(key); cache.set(key, entry); return entry.meshes;
    }
    if (this.closed) throw new Error('STEP loading cancelled');
    this.worker ||= new Worker(new URL('./step-worker.js', import.meta.url));
    const meshes = await new Promise((resolve, reject) => {
      const finish = (error, meshes) => {
        clearTimeout(timer); this.pending = null;
        if (error) { this.worker?.terminate(); this.worker = null; reject(error); }
        else resolve(meshes);
      };
      const timer = setTimeout(() => finish(new Error('STEP conversion timed out')), 45000);
      this.pending = () => finish(new Error('STEP loading cancelled'));
      this.worker.onmessage = ({ data }) => finish(data.error ? new Error(data.error) : null, data.meshes);
      this.worker.onerror = event => { event.preventDefault(); finish(new Error('STEP converter could not be loaded')); };
      if (asset.bytes) this.worker.postMessage({ bytes: asset.bytes }, [asset.bytes]);
      else this.worker.postMessage(asset);
    });
    const bytes = meshes.reduce((sum, mesh) => sum + [mesh.position, mesh.index, mesh.normal, mesh.color]
      .reduce((n, array) => n + (array?.byteLength || 0), 0), 0);
    if (bytes <= cacheLimit) {
      while (cacheBytes + bytes > cacheLimit && cache.size) {
        const oldest = cache.keys().next().value; cacheBytes -= cache.get(oldest).bytes; cache.delete(oldest);
      }
      cache.set(key, { bytes, meshes }); cacheBytes += bytes;
    }
    return meshes;
  }
  close() { this.closed = true; this.pending?.(); this.worker?.terminate(); this.worker = null; }
}
