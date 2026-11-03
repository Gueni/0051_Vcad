/* Local OpenCascade runtime: conversion never blocks the viewer's UI thread. */
importScripts('./occt-import-js.js');
let runtime;
onmessage = async ({ data }) => {
  try {
    let bytes = data.bytes ? new Uint8Array(data.bytes) :
      Uint8Array.from(atob(data.data), c => c.charCodeAt(0));
    if (bytes.length > 32 * 1024 * 1024) throw new Error('STEP file exceeds 32 MB');
    if (data.compressed) {
      const reader = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate')).getReader();
      const chunks = []; let length = 0;
      try {
        while (true) {
          const { value, done } = await reader.read(); if (done) break;
          length += value.length;
          if (length > 64 * 1024 * 1024) throw new Error('Decompressed STEP file exceeds 64 MB');
          chunks.push(value);
        }
      } finally { await reader.cancel(); }
      bytes = new Uint8Array(length); let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    }
    runtime ||= occtimportjs({ locateFile: path => new URL(path, self.location.href).href });
    const occt = await runtime;
    const result = occt.ReadStepFile(bytes, { linearUnit: 'millimeter', linearDeflectionType: 'absolute_value',
      linearDeflection: 0.025, angularDeflection: 0.35 });
    if (!result.success || !result.meshes?.length) throw new Error('STEP file contains no usable solid geometry');
    const triangles = result.meshes.reduce((sum, mesh) => sum + mesh.index.array.length / 3, 0);
    if (triangles > 250000) throw new Error('STEP model exceeds the 250,000 triangle limit');
    const transfers = [];
    const meshes = result.meshes.map(mesh => {
      const position = new Float32Array(mesh.attributes.position.array), index = new Uint32Array(mesh.index.array);
      const normal = mesh.attributes.normal ? new Float32Array(mesh.attributes.normal.array) : null;
      const colored = mesh.color || mesh.brep_faces.some(face => face.color);
      let color = null;
      if (colored) {
        color = new Float32Array(position.length);
        const base = mesh.color || [0.65, 0.65, 0.65];
        for (let v = 0; v < color.length; v += 3) color.set(base, v);
        for (const face of mesh.brep_faces) if (face.color)
          for (let i = face.first * 3; i < (face.last + 1) * 3; i++) color.set(face.color, index[i] * 3);
        // STEP appearance is sRGB; Three.js vertex attributes use linear RGB.
        for (let i = 0; i < color.length; i++) color[i] = color[i] <= 0.04045 ? color[i] / 12.92 : ((color[i] + 0.055) / 1.055) ** 2.4;
      }
      for (const array of [position, index, normal, color]) if (array) transfers.push(array.buffer);
      return { position, index, normal, color };
    });
    postMessage({ meshes }, transfers);
  } catch (error) { postMessage({ error: error.message || 'STEP conversion failed' }); }
};
