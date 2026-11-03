import * as THREE from './three.module.min.js';
import { CadControls } from './cad-controls.js';
import { StepLoader } from './step-loader.js';
import { boardSurfaces } from './board-surface.js';
import { studioEnvironment, stepFinish, bodyMaterial, stepMaterial, boardPhysicalTexture, boardSurfaceMaterial } from './studio-3d.js';

const sceneFields = ['scene', 'pickMeshes', 'bodyMeshes', 'boardMeshes', 'studioLights', 'bounds', 'center', 'radius',
  'shadowDirty', 'shadowKey', 'selectionKey', 'bodyState', 'modelProgress', 'surfaceError', 'model', 'sourceSVG', 'ready', 'sceneBytes'];

function resources(scene) {
  const geometries = new Set(), materials = new Set(), textures = new Set(), shadows = new Set(), instances = new Set();
  scene?.traverse(object => {
    if (object.isInstancedMesh) instances.add(object);
    if (object.shadow) shadows.add(object.shadow);
    if (object.geometry) geometries.add(object.geometry);
    for (const material of Array.isArray(object.material) ? object.material : [object.material]) if (material) {
      materials.add(material);
      for (const value of Object.values(material)) if (value?.isTexture) textures.add(value);
    }
  });
  return { geometries, materials, textures, shadows, instances };
}

function disposeScene(scene) {
  const all = resources(scene);
  for (const items of Object.values(all)) for (const item of items) item.dispose();
}

function sceneBytes(scene) {
  const all = resources(scene), arrays = new Set(), attributes = new Set();
  let bytes = 0;
  for (const geometry of all.geometries) {
    for (const attribute of [...Object.values(geometry.attributes), geometry.index]) if (attribute) {
      arrays.add(attribute.array); attributes.add(attribute);
    }
  }
  for (const array of arrays) bytes += array.byteLength;
  for (const attribute of attributes) bytes += attribute.array.byteLength; // GPU buffers can have separate owners.
  for (const mesh of all.instances) bytes += mesh.instanceMatrix.array.byteLength * 2;
  for (const texture of all.textures) {
    const channels = texture.format === THREE.RGFormat ? 2 : 4;
    bytes += (texture.image?.width || 0) * (texture.image?.height || 0) * channels * 8 / 3;
  }
  for (const shadow of all.shadows) if (shadow.map) bytes += shadow.map.width * shadow.map.height * 8;
  return bytes;
}

// One WebGL context, with a bounded LRU of completed scenes across all tabs.
export class Board3D {
  constructor(host, onError, onPick, onModels, { maxBytes = 512 * 1024 * 1024, maxScenes = 3 } = {}) {
    this.host = host;
    this.onModels = onModels;
    this.cache = new Map(); this.maxBytes = maxBytes; this.maxScenes = maxScenes;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0x1b1d21);
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 0.95;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.shadowMap.autoUpdate = false;
    this.renderer.domElement.setAttribute('aria-label', '3D model view');
    this.renderer.domElement.setAttribute('role', 'region');
    this.renderer.domElement.tabIndex = 0;
    this.renderer.domElement.addEventListener('webglcontextlost', event => {
      event.preventDefault(); this.clear(); this.environment?.dispose(); this.environment = null;
      onError('The 3D graphics context was lost. Switch to 2D or reload to try again.');
    });
    host.append(this.renderer.domElement);
    this.camera = new THREE.PerspectiveCamera(40, 1, 0.01, 10000);
    this.camera.up.set(0, 0, 1);
    this.controls = new CadControls(this.camera, this.renderer.domElement);
    this.controls.addEventListener('change', () => this.queueRender());
    this.raycaster = new THREE.Raycaster();
    const canvas = this.renderer.domElement;
    const pointers = new Set();
    let click = null;
    this.cancelPick = () => { pointers.clear(); click = null; };
    window.addEventListener('blur', this.cancelPick);
    canvas.addEventListener('pointerdown', event => {
      pointers.add(event.pointerId);
      click = pointers.size === 1 && event.button === 0 ?
        { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false } : null;
    });
    canvas.addEventListener('pointermove', event => {
      if (click?.id === event.pointerId && Math.hypot(event.clientX - click.x, event.clientY - click.y) > 4) click.moved = true;
    });
    canvas.addEventListener('pointerup', event => {
      const selected = click; click = null; pointers.delete(event.pointerId);
      if (!this.scene || selected?.id !== event.pointerId || selected.moved || event.ctrlKey || event.metaKey) return;
      canvas.focus({ preventScroll: true });
      const rect = canvas.getBoundingClientRect();
      const point = new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1,
        -(event.clientY - rect.top) / rect.height * 2 + 1);
      this.scene.updateMatrixWorld(true);
      this.raycaster.setFromCamera(point, this.camera);
      // Include board surfaces so an occluded body cannot be picked through it.
      const hit = this.raycaster.intersectObjects(this.pickMeshes.filter(mesh => mesh.visible), false)[0];
      onPick?.(hit?.object.userData.bodyId ?? null, event.shiftKey);
    });
    canvas.addEventListener('pointercancel', event => { pointers.delete(event.pointerId); click = null; });
    canvas.addEventListener('lostpointercapture', event => { if (pointers.has(event.pointerId)) this.cancelPick(); });
    canvas.addEventListener('dblclick', event => {
      if (!this.scene || event.button !== 0) return;
      const rect = canvas.getBoundingClientRect();
      this.scene.updateMatrixWorld(true);
      this.raycaster.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1,
        -(event.clientY - rect.top) / rect.height * 2 + 1), this.camera);
      const hit = this.raycaster.intersectObjects(this.pickMeshes.filter(mesh => mesh.visible), false)[0];
      if (!hit) return; // Empty-space double-click still fits the whole board.
      event.preventDefault(); event.stopPropagation(); this.controls.cancel(); this.cancelPick();
      this.camera.position.add(hit.point.clone().sub(this.controls.target));
      this.controls.target.copy(hit.point); this.controls.update();
    });
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(host);
  }

  detach() {
    cancelAnimationFrame(this.renderFrame); this.renderFrame = 0;
    this.generation = (this.generation || 0) + 1;
    this.stepLoader?.close(); this.stepLoader = null;
    this.cancelPick();
    this.controls.enabled = false;
    for (const field of sceneFields) this[field] = null;
    this.ready = false; this.loading = null;
    this.pickMeshes = [];
    this.bodyMeshes = [];
    this.boardMeshes = [];
    this.renderer.clear();
  }

  hide() {
    if (this.scene && this.ready) {
      this.cache.set(this.model, Object.fromEntries(sceneFields.map(field => [field, this[field]])));
    } else disposeScene(this.scene);
    this.detach(); this.trimCache();
  }

  trimCache(activeBytes = 0, activeCount = 0) {
    let bytes = activeBytes + [...this.cache.values()].reduce((sum, entry) => sum + entry.sceneBytes, 0);
    while (this.cache.size && (bytes > this.maxBytes || this.cache.size + activeCount > this.maxScenes)) {
      const key = this.cache.keys().next().value, entry = this.cache.get(key);
      bytes -= entry.sceneBytes; this.cache.delete(key); disposeScene(entry.scene);
    }
  }

  release(model) {
    if (this.model === model) { disposeScene(this.scene); this.detach(); }
    const entry = this.cache.get(model);
    if (entry) { this.cache.delete(model); disposeScene(entry.scene); }
  }

  clear() {
    disposeScene(this.scene); this.detach();
    for (const entry of this.cache.values()) disposeScene(entry.scene);
    this.cache.clear();
  }

  show(model, state, bodies, sourceSVG) {
    this.hide();
    const saved = this.cache.get(model);
    if (saved) {
      this.cache.delete(model);
      if (saved.sourceSVG === sourceSVG) {
        Object.assign(this, saved);
        this.loading = Promise.resolve();
        this.renderer.shadowMap.needsUpdate = false;
        this.controls.minDistance = this.radius * 0.01; this.controls.maxDistance = this.radius * 100;
        this.controls.enabled = true;
        this.restoreCamera(state); this.resize(); this.updateBodies(bodies);
        this.onModels?.(this.modelProgress);
        if (this.surfaceError) this.onModels?.({ surfaceError: this.surfaceError });
        return;
      }
      disposeScene(saved.scene);
    }
    this.model = model; this.sourceSVG = sourceSVG;
    this.shadowDirty = true; this.shadowKey = null; this.selectionKey = null;
    this.modelProgress = { loaded: 0, total: 0, failed: [] }; this.surfaceError = null;
    this.scene = new THREE.Scene();
    // The worker can fetch WASM and convert models while the main thread builds
    // the board's often large drilled mesh. Its results resume after show returns.
    const generation = this.generation, steps = this.loadSteps(model, generation);
    this.environment ||= studioEnvironment(this.renderer);
    this.scene.environment = this.environment.texture; this.scene.environmentIntensity = 0.5;
    this.scene.background = new THREE.Color(0x242a32);
    const ambient = new THREE.HemisphereLight(0xe8f1ff, 0x8b8173, 0.35);
    ambient.up.set(0, 0, 1); ambient.position.set(0, 0, 1); this.scene.add(ambient);
    this.studioLights = [new THREE.DirectionalLight(0xfff4e5, 2), new THREE.DirectionalLight(0xc8ddff, 1.6)];
    for (const light of this.studioLights) {
      light.castShadow = true; light.shadow.mapSize.set(2048, 2048);
      light.shadow.bias = -0.00015; light.shadow.normalBias = 0.025;
      this.scene.add(light, light.target);
    }
    // Pre-tessellated multi-part models announce their extent so the view can
    // be framed before every part has loaded.
    this.modelBounds = model.bounds ? new THREE.Box3(new THREE.Vector3(...model.bounds[0]),
      new THREE.Vector3(...model.bounds[1])) : null;
    const surfaceBounds = new THREE.Box2().setFromPoints(model.board.flatMap(part =>
      part.outline.map(point => new THREE.Vector2(...point))));
    const surfaceSize = surfaceBounds.getSize(new THREE.Vector2());
    const addSolid = (data, z, height, color, opacity = 1, edgeColor = 0x182323, bodyId = null) => {
      if (!data.outline?.length) return;
      const shape = new THREE.Shape(data.outline.map(p => new THREE.Vector2(...p)));
      shape.holes = (data.holes || []).map(points => new THREE.Path(points.map(p => new THREE.Vector2(...p))));
      const geometry = new THREE.ExtrudeGeometry(shape, { depth: height, bevelEnabled: false, steps: 1 });
      // Rendering and picking use the finished buffers. ExtrudeGeometry otherwise
      // retains every construction curve (including thousands of drill contours).
      geometry.parameters = {};
      if (bodyId !== null) geometry.deleteAttribute('uv');
      if (bodyId === null) {
        // Initialize UVs before any shadow or color pass uploads the geometry.
        // Replacing the attribute after the asynchronous finish load can leave
        // a cached VAO with no UV buffer when shadow passes skip that upload.
        const positions = geometry.attributes.position, uv = geometry.attributes.uv;
        for (let i = 0; i < positions.count; i++) uv.setXY(i,
          (positions.getX(i) - surfaceBounds.min.x) / (surfaceSize.x || 1),
          (positions.getY(i) - surfaceBounds.min.y) / (surfaceSize.y || 1));
        // Give the existing top, bottom and edge their own material. A second
        // almost-coplanar finish mesh causes depth fighting at grazing angles.
        geometry.clearGroups();
        const normals = geometry.attributes.normal;
        let first = 0, previous = null;
        for (let i = 0; i < normals.count; i += 3) {
          const z = normals.getZ(i), current = z > 0.5 ? 0 : z < -0.5 ? 1 : 2;
          if (current !== previous && previous !== null) { geometry.addGroup(first, i - first, previous); first = i; }
          previous = current;
        }
        if (previous !== null) geometry.addGroup(first, normals.count - first, previous);
      }
      const material = bodyId === null ? [
        new THREE.MeshPhysicalMaterial({ color: 0x0e452c, roughness: 0.48, metalness: 0, clearcoat: 0.3, clearcoatRoughness: 0.34 }),
        new THREE.MeshPhysicalMaterial({ color: 0x0e452c, roughness: 0.48, metalness: 0, clearcoat: 0.3, clearcoatRoughness: 0.34 }),
        new THREE.MeshStandardMaterial({ color: 0x867552, roughness: 0.85, metalness: 0 }),
      ] : bodyMaterial({ color, roughness: 0.6, metalness: 0,
        opacity, transparent: opacity < 1 });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.visible = bodyId === null || !bodies?.deleted.has(bodyId);
      mesh.position.z = z; mesh.name = data.name || 'Board'; this.scene.add(mesh);
      mesh.userData.bodyId = bodyId;
      if (bodyId === null) this.boardMeshes.push(mesh);
      mesh.castShadow = true; mesh.receiveShadow = true;
      if (opacity > 0) this.pickMeshes.push(mesh);
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometry, 25),
        new THREE.LineBasicMaterial({ color: edgeColor, transparent: true, opacity: (bodyId === null ? 0.07 : 0.14) * opacity }));
      edges.position.z = z; this.scene.add(edges);
      edges.visible = mesh.visible;
      if (bodyId !== null) this.bodyMeshes.push({ id: bodyId, mesh, edges, edgeColor, edgeOpacity: 0.14 * opacity });
    };
    model.board.forEach(part => addSolid(part, -model.thickness / 2, model.thickness, 0x16764f, 1, 0xbcc28a));
    model.bodies.forEach((body, index) => addSolid(body, body.z, body.height, body.color, body.opacity,
      0x182323, body.id ?? `body-${index}`));
    this.updateBounds();
    this.controls.enabled = true;
    this.resize();
    this.restoreCamera(state);
    if (this.modelBounds && !state) this.fit('iso');
    this.updateBodies(bodies);
    this.loading = Promise.all([steps, this.loadSurfaces(sourceSVG, model, generation)]).then(() => {
      if (generation !== this.generation) return;
      this.stepLoader?.close(); this.stepLoader = null;
      if (model.standalone) this.fit('iso');
      this.ready = true; this.sceneBytes = sceneBytes(this.scene);
      this.trimCache(this.sceneBytes, 1);
    });
  }

  restoreCamera(state) {
    if (state) {
      this.camera.position.fromArray(state.position); this.camera.up.fromArray(state.up);
      this.controls.target.fromArray(state.target); this.controls.update();
    } else this.fit('iso');
  }

  async loadSurfaces(source, model, generation) {
    try {
      const surfaces = await boardSurfaces(source, model, Math.min(2048, this.renderer.capabilities.maxTextureSize));
      if (generation !== this.generation || !surfaces.length) return;
      for (const surface of surfaces) {
        const map = new THREE.CanvasTexture(surface.color); map.colorSpace = THREE.SRGBColorSpace;
        const physical = boardPhysicalTexture(surface.physical);
        for (const texture of [map, physical]) texture.anisotropy = Math.min(8, this.renderer.capabilities.getMaxAnisotropy());
        const material = boardSurfaceMaterial(map, physical);
        const cap = surface.side === 'top' ? 0 : 1;
        for (const mesh of this.boardMeshes) {
          mesh.material[cap].dispose(); mesh.material[cap] = material;
        }
      }
      this.render();
    } catch (error) {
      if (generation === this.generation) {
        this.surfaceError = 'Board finish unavailable: ' + error.message;
        this.onModels?.({ surfaceError: this.surfaceError });
      }
    }
  }

  async loadSteps(model, generation) {
    const assets = model.step_models || {};
    const requests = new Map();
    for (const body of model.bodies) if (body.model) {
      if (!requests.has(body.model)) requests.set(body.model, new Map());
      requests.get(body.model).set(body.id, body);
    }
    let loaded = 0; const failed = [];
    const progress = () => {
      this.modelProgress = { loaded, total: requests.size, failed: [...failed] };
      this.onModels?.(this.modelProgress);
    };
    progress();
    if (!requests.size) return;
    // Bound WASM memory/CPU use while allowing two independent assets to convert
    // concurrently on multicore machines. Repeated bodies still convert once.
    const count = requests.size > 1 && (navigator.hardwareConcurrency || 2) >= 4 ? 2 : 1;
    const loaders = Array.from({ length: count }, () => new StepLoader()), pending = requests.entries();
    this.stepLoader = { close: () => loaders.forEach(loader => loader.close()) };
    await Promise.all(loaders.map(async loader => {
      for (const [key, bodies] of pending) {
        try {
          const parts = await loader.load(key, assets[key]);
          if (generation !== this.generation) return;
          this.addStepBodies([...bodies.values()], parts);
          loaded++;
        } catch (error) {
          if (generation !== this.generation) return;
          failed.push(`${assets[key]?.name || 'Model'}: ${error.message}`);
        }
        progress();
        // Give orbit/pan and tab controls a turn between converted assets.
        await new Promise(resolve => setTimeout(resolve, 0));
        if (generation !== this.generation) return;
      }
    }));
    if (generation !== this.generation) return;
    this.renderer.shadowMap.needsUpdate = true; this.render();
  }

  addStepBodies(bodies, parts) {
    const ids = new Set(bodies.map(body => body.id));
    const replaced = this.bodyMeshes.filter(entry => ids.has(entry.id));
    const removed = new Set(replaced.map(entry => entry.mesh));
    const remaining = this.bodyMeshes.filter(entry => !ids.has(entry.id)), retiredBatches = new Set();
    const retainedGeometry = new Set(remaining.map(entry => entry.mesh.geometry));
    const retainedBatches = new Set(remaining.map(entry => entry.batch));
    for (const { mesh, edges, batch, instance } of replaced) {
      if (batch) {
        retiredBatches.add(batch);
        batch.setMatrixAt(instance, new THREE.Matrix4().makeScale(0, 0, 0)); batch.instanceMatrix.needsUpdate = true;
      } else {
        this.scene.remove(mesh);
        if (!retainedGeometry.has(mesh.geometry)) { mesh.geometry.dispose(); retainedGeometry.add(mesh.geometry); }
        mesh.material.dispose();
      }
      if (edges) this.scene.remove(edges);
      edges?.geometry.dispose(); edges?.material.dispose();
    }
    for (const batch of retiredBatches) if (!retainedBatches.has(batch)) {
      this.scene.remove(batch); batch.dispose(); batch.geometry.dispose(); batch.material.dispose();
    }
    this.pickMeshes = this.pickMeshes.filter(mesh => !removed.has(mesh));
    this.bodyMeshes = remaining;
    const geometries = parts.map(part => {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.BufferAttribute(part.position, 3));
      geometry.setIndex(new THREE.BufferAttribute(part.index, 1));
      if (part.normal) geometry.setAttribute('normal', new THREE.BufferAttribute(part.normal, 3));
      else geometry.computeVertexNormals();
      if (part.color) geometry.setAttribute('color', new THREE.BufferAttribute(part.color, 3));
      stepFinish(geometry, part, bodies[0].color);
      geometry.computeBoundingBox(); geometry.computeBoundingSphere();
      return geometry;
    });
    const groups = new Map(), matrix = new THREE.Matrix4();
    for (const body of bodies) {
      // Mirrored transforms and transparent sorting need individual draws.
      const batchable = body.opacity === 1 && matrix.fromArray(body.matrix).determinant() > 0;
      const key = batchable ? `${body.color}` : body;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(body);
    }
    for (const group of groups.values()) for (const [index, source] of geometries.entries()) {
      const instanced = group.length > 1;
      let geometry = source, batch = null;
      const body = group[0], material = stepMaterial({ color: parts[index].color ? 0xffffff : body.color,
        vertexColors: Boolean(parts[index].color), opacity: body.opacity,
        transparent: body.opacity < 1, side: THREE.DoubleSide }, instanced);
      if (instanced) {
        // Share immutable vertex/index arrays, but keep per-batch highlights.
        geometry = new THREE.BufferGeometry();
        // Separate attribute owners allow one batch to be disposed independently.
        geometry.setIndex(new THREE.BufferAttribute(source.index.array, 1));
        for (const [name, attribute] of Object.entries(source.attributes))
          geometry.setAttribute(name, new THREE.BufferAttribute(attribute.array, attribute.itemSize, attribute.normalized));
        geometry.boundingBox = source.boundingBox; geometry.boundingSphere = source.boundingSphere;
        geometry.setAttribute('instanceHighlight', new THREE.InstancedBufferAttribute(new Float32Array(group.length), 1));
        batch = new THREE.InstancedMesh(geometry, material, group.length);
        batch.castShadow = batch.receiveShadow = true;
        this.scene.add(batch);
      }
      group.forEach((body, instance) => {
        // Individual proxies preserve exact body picking, bounds and CAD edits.
        const mesh = new THREE.Mesh(geometry, material);
        mesh.name = body.name; mesh.userData.bodyId = body.id; mesh.userData.step = true;
        mesh.matrixAutoUpdate = false; mesh.matrix.fromArray(body.matrix); mesh.updateMatrixWorld(true);
        mesh.castShadow = mesh.receiveShadow = true;
        if (batch) batch.setMatrixAt(instance, mesh.matrix); else this.scene.add(mesh);
        if (body.opacity > 0) this.pickMeshes.push(mesh);
        this.bodyMeshes.push({ id: body.id, mesh, batch, instance });
      });
      if (batch) { batch.computeBoundingBox(); batch.computeBoundingSphere(); }
    }
    this.shadowDirty = true; this.updateBodies(this.bodyState);
  }

  updateBounds() {
    const bounds = new THREE.Box3();
    this.pickMeshes.filter(mesh => mesh.visible).forEach(mesh => bounds.expandByObject(mesh));
    if (this.modelBounds && !this.ready) bounds.union(this.modelBounds);
    this.bounds = bounds;
    this.center = bounds.getCenter(new THREE.Vector3());
    this.radius = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 0.1);
    this.controls.minDistance = this.radius * 0.01;
    this.controls.maxDistance = this.radius * 100;
    for (const [index, light] of (this.studioLights || []).entries()) {
      const r = this.radius, side = index ? -1 : 1;
      light.position.copy(this.center).add(new THREE.Vector3(-r * side, -r * 1.2, r * 2.5 * side));
      light.target.position.copy(this.center);
      const camera = light.shadow.camera;
      camera.left = camera.bottom = -r * 1.1; camera.right = camera.top = r * 1.1;
      camera.near = Math.max(0.01, r / 100); camera.far = r * 6; camera.updateProjectionMatrix();
    }
  }

  updateBodies(state) {
    this.bodyState = state;
    const key = [...(state?.deleted || [])].sort().join(','), selectionKey = [...(state?.selected || [])].sort().join(',');
    if (!this.shadowDirty && key === this.shadowKey && selectionKey === this.selectionKey) { this.render(); return; }
    for (const { id, mesh, edges, edgeColor, edgeOpacity, batch, instance } of this.bodyMeshes) {
      const visible = mesh.visible;
      mesh.visible = !state?.deleted.has(id);
      if (edges) edges.visible = mesh.visible;
      const selected = Boolean(state?.selected.has(id));
      if (batch) {
        const highlight = batch.geometry.attributes.instanceHighlight;
        if (highlight.getX(instance) !== +selected) { highlight.setX(instance, +selected); highlight.needsUpdate = true; }
        if (visible !== mesh.visible) {
          batch.setMatrixAt(instance, mesh.visible ? mesh.matrix : new THREE.Matrix4().makeScale(0, 0, 0));
          batch.instanceMatrix.needsUpdate = true;
        }
      } else mesh.material.userData.highlight.value = selected ? 1 : 0;
      if (edges) {
        edges.material.color.setHex(selected ? 0x35c2d8 : edgeColor);
        edges.material.opacity = selected ? 1 : edgeOpacity;
      }
    }
    if (this.shadowDirty || key !== this.shadowKey) this.updateBounds();
    if (this.shadowDirty || key !== this.shadowKey) this.renderer.shadowMap.needsUpdate = true;
    this.shadowDirty = false; this.shadowKey = key; this.selectionKey = selectionKey;
    this.render();
  }

  state() {
    return this.scene ? { position: this.camera.position.toArray(), up: this.camera.up.toArray(),
      target: this.controls.target.toArray() } : null;
  }

  fit(preset = null) {
    if (!this.scene) return;
    this.controls.cancel(); this.cancelPick();
    const angle = THREE.MathUtils.degToRad(this.camera.fov / 2);
    const distance = this.radius / Math.sin(Math.min(angle, Math.atan(Math.tan(angle) * this.camera.aspect))) * 1.12;
    const direction = !preset ? this.camera.getWorldDirection(new THREE.Vector3()).negate() :
      preset === 'top' ? new THREE.Vector3(0, 0, 1) : preset === 'bottom' ? new THREE.Vector3(0, 0, -1) :
        new THREE.Vector3(1, -1.3, 1.4).normalize();
    // Framing preserves viewing direction and roll; only explicit presets rotate.
    if (preset) this.camera.up.set(0, preset === 'bottom' ? -1 : 1, 0);
    this.camera.position.copy(this.center).addScaledVector(direction, distance);
    this.controls.target.copy(this.center); this.controls.update(); this.render();
  }

  rotateStep(horizontal, vertical, degrees = 5) {
    if (!this.scene) return;
    this.controls.cancel(); this.cancelPick();
    const angle = THREE.MathUtils.degToRad(degrees);
    this.controls.rotateBy(horizontal * angle, vertical * angle); this.render();
  }

  zoom(factor) {
    if (!this.scene) return;
    this.controls.dolly(factor); this.render();
  }

  resize() {
    const { clientWidth: width, clientHeight: height } = this.host;
    if (!width || !height) return;
    this.renderer.setSize(width, height);
    this.camera.aspect = width / height; this.camera.updateProjectionMatrix(); this.render();
  }

  queueRender() {
    if (!this.renderFrame) this.renderFrame = requestAnimationFrame(() => { this.renderFrame = 0; this.render(); });
  }

  render() {
    cancelAnimationFrame(this.renderFrame); this.renderFrame = 0;
    if (!this.scene || this.host.hidden) return;
    // Fit the depth interval to the scene along the current viewing direction,
    // including after zoom/pan/orbit. A huge far/near ratio quantizes nearby
    // STEP faces to the same depth and produces stripes across flat surfaces.
    if (this.bounds && !this.bounds.isEmpty()) {
      const direction = this.camera.getWorldDirection(new THREE.Vector3()), corner = new THREE.Vector3();
      let nearest = Infinity, farthest = -Infinity;
      for (const x of [this.bounds.min.x, this.bounds.max.x])
        for (const y of [this.bounds.min.y, this.bounds.max.y])
          for (const z of [this.bounds.min.z, this.bounds.max.z]) {
            const depth = corner.set(x, y, z).sub(this.camera.position).dot(direction);
            nearest = Math.min(nearest, depth); farthest = Math.max(farthest, depth);
          }
      const margin = this.radius * 0.01;
      const near = Math.max(this.radius / 1000, nearest - margin, 0.0001);
      const far = Math.max(near + margin, farthest + margin);
      if (near !== this.camera.near || far !== this.camera.far) {
        this.camera.near = near; this.camera.far = far; this.camera.updateProjectionMatrix();
      }
    }
    this.renderer.render(this.scene, this.camera);
  }
}
