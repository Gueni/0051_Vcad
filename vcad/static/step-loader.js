// Keep converted data across tabs while bounding retained CPU memory. GPU
// geometries are shared per model by Board3D and retained with completed views.
const cache = new Map(); let cacheBytes = 0;
const cacheLimit = 64 * 1024 * 1024;
// SolidWorks display meshes arrive already tessellated, with saved normals.
function decodeMesh(mesh) {
  const decode = (text, Type) => text ? new Type(Uint8Array.from(atob(text), c => c.charCodeAt(0)).buffer) : null;
  return { position: decode(mesh.position, Float32Array), index: decode(mesh.index, Uint32Array),
           normal: decode(mesh.normal, Float32Array), color: decode(mesh.color, Float32Array) };
}
export class StepLoader {
  async load(key, asset) {
    if (asset?.mesh) return asset.mesh.map(decodeMesh);  // already tessellated (SolidWorks)
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
