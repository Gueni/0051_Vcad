import { vertexShader, fragmentShader } from './layout-shaders.js';

// One context for every tab; at most one PCB is resident, including behind sheets.
// CPU buffers are retained per tab for switching/context restoration. Navigation
// changes uniforms only. The SVG owns picking, annotations and measurements.
export class LayoutWebGL {
  constructor(host, changed) {
    this.canvas = document.createElement('canvas');
    this.canvas.className = 'layout-webgl'; this.canvas.hidden = true;
    this.canvas.setAttribute('aria-hidden', 'true'); host.prepend(this.canvas);
    this.changed = changed; this.scene = null; this.lost = false;
    this.stats = { geometryUploads: 0, selectionUploads: 0, frames: 0, drawCalls: 0 };
    this.canvas.addEventListener('webglcontextlost', event => {
      event.preventDefault(); this.lost = true; this.resources = null;
      this.canvas.hidden = true; this.changed('Graphics context lost; using SVG.');
    });
    this.canvas.addEventListener('webglcontextrestored', () => {
      try {
        this.lost = false; this.initialize();
        const scene = this.scene; this.scene = null;
        if (scene) this.setScene(scene);
        this.changed();
      } catch (error) { this.changed(error.message); }
    });
    this.gl = this.canvas.getContext('webgl2', { alpha: false, antialias: true, depth: false, stencil: false });
    if (!this.gl) { this.canvas.remove(); throw new Error('WebGL 2 is unavailable; using SVG.'); }
    try { this.initialize(); }
    catch (error) { this.canvas.remove(); throw error; }
  }
  initialize() {
    const gl = this.gl, shaders = [];
    const program = gl.createProgram();
    try {
      for (const [type, source] of [[gl.VERTEX_SHADER, vertexShader], [gl.FRAGMENT_SHADER, fragmentShader]]) {
        const shader = gl.createShader(type); shaders.push(shader);
        gl.shaderSource(shader, source); gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
        gl.attachShader(program, shader);
      }
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
    } catch (error) { gl.deleteProgram(program); throw error; }
    finally { shaders.forEach(shader => gl.deleteShader(shader)); }
    this.program = program; this.uniforms = {};
    for (const name of ['camera', 'viewport', 'pixelMetrics', 'objectFlags', 'flagsWidth', 'renderPass', 'showPolygons', 'selectionActive']) {
      this.uniforms[name] = gl.getUniformLocation(program, name);
    }
    gl.disable(gl.DEPTH_TEST); gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    this.selectionKey = null;
  }
  static decode(buffer) {
    if (buffer.byteLength < 4) throw new Error('Incomplete WebGL scene');
    const size = new DataView(buffer).getUint32(0, true), start = 4 + size;
    if (size % 4 || start > buffer.byteLength) throw new Error('Invalid WebGL scene header');
    const scene = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, size)));
    if (!scene.supported) throw new Error(scene.reason || 'This layout requires SVG.');
    if (scene.version !== 2 || scene.stride !== 72 || scene.vertex_bytes % 72 || scene.index_bytes % 4 ||
        start + scene.vertex_bytes + scene.index_bytes !== buffer.byteLength || buffer.byteLength > 160 * 1024 * 1024) {
      throw new Error('Invalid WebGL geometry buffers');
    }
    scene.vertices = new Uint8Array(buffer, start, scene.vertex_bytes);
    scene.indices = new Uint32Array(buffer, start + scene.vertex_bytes, scene.index_bytes / 4);
    return scene;
  }
  static preparePicking(svg) {
    // Hidden SVG avoids repainting the interaction tree on every camera move.
    // SVG's painted/fill/stroke hit modes ignore visibility. Preserve the
    // source visibility and pointer rules before applying that GPU-only mode.
    const hidden = new WeakSet();
    const modes = { auto: 'painted', visiblepainted: 'painted', visiblefill: 'fill', visiblestroke: 'stroke', visible: 'all' };
    function visit(node, visibility = 'visible', pointer = 'visiblepainted') {
      const ownVisibility = node.style.visibility || node.getAttribute('visibility');
      const ownPointer = node.style.pointerEvents || node.getAttribute('pointer-events');
      if (ownVisibility && ownVisibility !== 'inherit') visibility = ownVisibility;
      if (ownPointer && ownPointer !== 'inherit') pointer = ownPointer.toLowerCase();
      if (visibility !== 'visible') hidden.add(node);
      // Non-visibility-sensitive source modes can intentionally hit hidden paint.
      const requiresVisible = pointer === 'auto' || pointer.startsWith('visible');
      node.style.setProperty('--layout-hit-test', requiresVisible && visibility !== 'visible' ? 'none' : modes[pointer] || pointer);
      for (const child of node.children) visit(child, visibility, pointer);
    }
    visit(svg);
    return hidden;
  }
  release() {
    if (this.resources && !this.lost) {
      const gl = this.gl, r = this.resources;
      gl.deleteBuffer(r.vertices); gl.deleteBuffer(r.indices); gl.deleteTexture(r.flags); gl.deleteVertexArray(r.vao);
    }
    this.resources = null; this.scene = null; this.selectionKey = null;
    this.orderedScene = null; this.entries = []; this.order = null;
  }
  setScene(scene) {
    if (this.lost) return false;
    if (scene === this.scene && this.resources) return true;
    this.release();
    const gl = this.gl;
    this.resources = { vao: gl.createVertexArray(), vertices: gl.createBuffer(), indices: gl.createBuffer(), flags: gl.createTexture() };
    const r = this.resources;
    try {
      gl.bindVertexArray(r.vao); gl.bindBuffer(gl.ARRAY_BUFFER, r.vertices);
      gl.bufferData(gl.ARRAY_BUFFER, scene.vertices, gl.STATIC_DRAW);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, r.indices); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, scene.indices, gl.STATIC_DRAW);
      for (const [name, size, offset] of [['worldPosition', 2, 0], ['localPosition', 2, 8], ['shapeParameters', 4, 16],
        ['sourceColor', 4, 32], ['worldExpansion', 2, 56], ['localExpansion', 2, 64]]) {
        const location = gl.getAttribLocation(this.program, name);
        gl.enableVertexAttribArray(location); gl.vertexAttribPointer(location, size, gl.FLOAT, false, 72, offset);
      }
      const metadata = gl.getAttribLocation(this.program, 'metadata');
      gl.enableVertexAttribArray(metadata); gl.vertexAttribIPointer(metadata, 2, gl.UNSIGNED_INT, 72, 48);
      const count = Math.max(1, scene.object_count), maximum = gl.getParameter(gl.MAX_TEXTURE_SIZE);
      this.flagsWidth = Math.min(1024, maximum, count); this.flagsHeight = Math.ceil(count / this.flagsWidth);
      if (this.flagsHeight > maximum) throw new Error('Layout exceeds the graphics selection limit');
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, r.flags);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, this.flagsWidth, this.flagsHeight, 0, gl.RED, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      if (gl.getError() !== gl.NO_ERROR) throw new Error('Graphics memory allocation failed; using SVG.');
      this.scene = scene; this.stats.geometryUploads++;
      return true;
    } catch (error) { this.release(); throw error; }
    finally { gl.bindVertexArray(null); }
  }
  paint(vb, groups, polygons, components, nets) {
    if (this.lost || !this.resources) return;
    const gl = this.gl, u = this.uniforms, scene = this.scene;
    const width = this.canvas.clientWidth, height = this.canvas.clientHeight;
    if (!width || !height) return;
    const dpr = window.devicePixelRatio || 1, scale = Math.min(width / vb.w, height / vb.h);
    const pw = Math.round(width * dpr), ph = Math.round(height * dpr);
    if (this.canvas.width !== pw || this.canvas.height !== ph) { this.canvas.width = pw; this.canvas.height = ph; }
    gl.viewport(0, 0, pw, ph); gl.clearColor(27 / 255, 29 / 255, 33 / 255, 1); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(this.program); gl.bindVertexArray(this.resources.vao);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this.resources.flags);
    const key = JSON.stringify([components, nets]);
    if (key !== this.selectionKey) {
      const flags = new Uint8Array(this.flagsWidth * this.flagsHeight);
      for (const [names, mapping, value] of [[nets, scene.object_nets, 1], [components, scene.object_components, 2]]) {
        for (const name of names) for (const id of mapping?.[name] || []) if (id > 0 && id < flags.length) flags[id] = value;
      }
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, this.flagsWidth, this.flagsHeight, gl.RED, gl.UNSIGNED_BYTE, flags);
      this.selectionKey = key; this.stats.selectionUploads++;
    }
    gl.uniform2f(u.camera, vb.x + vb.w / 2 - scene.origin[0], vb.y + vb.h / 2 - scene.origin[1]);
    gl.uniform2f(u.viewport, width, height); gl.uniform2f(u.pixelMetrics, scale, 1 / (scale * dpr));
    gl.uniform1i(u.objectFlags, 0); gl.uniform1i(u.flagsWidth, this.flagsWidth); gl.uniform1i(u.showPolygons, +polygons);
    const selected = Boolean(components.length || nets.length); gl.uniform1i(u.selectionActive, +selected);
    const visible = new Set(groups.filter(g => g.style.display !== 'none').map(g => g.id));
    const order = groups.map(g => g.id).join('|');
    if (order !== this.order || scene !== this.orderedScene) {
      const rank = new Map(groups.map((g, i) => [g.id, i]));
      // Keep layerless slots in source order; visible layers follow SVG order.
      const layers = scene.entries.filter(e => e.layer).sort((a, b) => (rank.get(a.layer) ?? -1) - (rank.get(b.layer) ?? -1));
      let i = 0; this.entries = scene.entries.map(e => e.layer ? layers[i++] : e);
      this.order = order; this.orderedScene = scene;
    }
    let calls = 0;
    for (let pass = 0; pass < (selected ? 3 : 1); pass++) {
      gl.uniform1i(u.renderPass, pass);
      for (const entry of this.entries) if ((!entry.layer || visible.has(entry.layer)) && entry.count) {
        gl.drawElements(gl.TRIANGLES, entry.count, gl.UNSIGNED_INT, entry.first * 4); calls++;
      }
    }
    gl.bindVertexArray(null);
    this.stats.frames++; this.stats.drawCalls = calls;
    // Readable diagnostics without synchronous GL queries on the frame path.
    Object.assign(this.canvas.dataset, { geometryUploads: this.stats.geometryUploads, selectionUploads: this.stats.selectionUploads,
      frames: this.stats.frames, drawCalls: calls, geometryBytes: scene.vertex_bytes + scene.index_bytes });
  }
}
