import { EventDispatcher, MathUtils, Quaternion, Vector2, Vector3 } from './three.module.min.js';

// Free trackball rotation in the camera's own frame. There is no fixed world-up
// axis or polar clamp: top, bottom and rolled views use the same drag behavior.
// No inertia or animation loop; navigation stops exactly when input stops.
export class CadControls extends EventDispatcher {
  constructor(camera, element) {
    super();
    this.camera = camera; this.element = element;
    this.target = new Vector3(); this.minDistance = 0.01; this.maxDistance = Infinity;
    this.pointers = new Map(); this.drag = null; this.gestureScale = null; this._enabled = true;
    this.listeners = new AbortController();
    const listen = (host, name, listener, options = {}) => host.addEventListener(name, listener,
      { ...options, signal: this.listeners.signal });
    listen(element, 'contextmenu', event => { if (this.enabled) event.preventDefault(); });
    listen(element, 'pointerdown', event => this.pointerDown(event));
    listen(element, 'pointermove', event => this.pointerMove(event));
    listen(element, 'pointerup', event => this.pointerUp(event));
    listen(element, 'pointercancel', () => this.cancel());
    listen(element, 'lostpointercapture', event => { if (this.pointers.has(event.pointerId)) this.cancel(); });
    listen(element, 'wheel', event => this.wheel(event), { passive: false });
    listen(element, 'gesturestart', event => {
      if (!this.enabled) return;
      event.preventDefault(); this.cancel(); this.gestureScale = event.scale > 0 ? event.scale : 1;
    }, { passive: false });
    listen(element, 'gesturechange', event => {
      if (!this.enabled || this.gestureScale === null || !Number.isFinite(event.scale) || event.scale <= 0) return;
      event.preventDefault(); this.dolly(this.gestureScale / event.scale, this.point(event)); this.gestureScale = event.scale;
    }, { passive: false });
    listen(element, 'gestureend', event => {
      if (this.gestureScale !== null) event.preventDefault(); this.gestureScale = null; this.rebase();
    }, { passive: false });
    listen(window, 'blur', () => this.cancel());
  }

  get enabled() { return this._enabled; }
  set enabled(value) { this._enabled = Boolean(value); if (!this._enabled) this.cancel(); }

  point(event) {
    const rect = this.element.getBoundingClientRect();
    return new Vector2(Number.isFinite(event.clientX) ? event.clientX : rect.x + rect.width / 2,
      Number.isFinite(event.clientY) ? event.clientY : rect.y + rect.height / 2);
  }

  cancel() {
    const ids = [...this.pointers.keys()]; this.pointers.clear();
    for (const id of ids) if (this.element.hasPointerCapture(id)) this.element.releasePointerCapture(id);
    this.drag = null; this.gestureScale = null; this.element.style.cursor = '';
  }

  dispose() { this.cancel(); this.listeners.abort(); }

  rebase() {
    const points = [...this.pointers.values()];
    this.drag = points.length === 1 ? { start: points[0].point.clone(), last: points[0].point.clone(), moved: false } : null;
    if (points.length >= 2) this.touch = { center: points[0].point.clone().add(points[1].point).multiplyScalar(0.5),
      distance: points[0].point.distanceTo(points[1].point) };
  }

  pointerDown(event) {
    if (!this.enabled || ![0, 1, 2].includes(event.button)) return;
    event.preventDefault(); this.element.focus({ preventScroll: true });
    this.pointers.set(event.pointerId, { point: this.point(event), button: event.button });
    this.element.setPointerCapture(event.pointerId); this.rebase();
  }

  pointerMove(event) {
    const pointer = this.pointers.get(event.pointerId);
    if (!this.enabled || !pointer) return;
    event.preventDefault(); pointer.point.copy(this.point(event));
    if (this.gestureScale !== null) return;
    const points = [...this.pointers.values()];
    if (points.length >= 2) {
      const center = points[0].point.clone().add(points[1].point).multiplyScalar(0.5);
      const distance = points[0].point.distanceTo(points[1].point);
      this.pan(center.x - this.touch.center.x, center.y - this.touch.center.y);
      if (distance > 0 && this.touch.distance > 0) this.dolly(this.touch.distance / distance, center);
      this.touch = { center, distance }; this.element.style.cursor = 'move';
    } else if (this.drag) {
      if (!this.drag.moved && pointer.point.distanceTo(this.drag.start) <= 4) return;
      this.drag.moved = true;
      if (pointer.button === 2 || event.shiftKey || event.ctrlKey || event.metaKey) {
        this.pan(pointer.point.x - this.drag.last.x, pointer.point.y - this.drag.last.y);
        this.element.style.cursor = 'move';
      } else {
        this.rotate(this.drag.last, pointer.point); this.element.style.cursor = 'grabbing';
      }
      this.drag.last.copy(pointer.point);
    }
  }

  pointerUp(event) {
    if (!this.pointers.delete(event.pointerId)) return;
    if (this.element.hasPointerCapture(event.pointerId)) this.element.releasePointerCapture(event.pointerId);
    this.rebase(); if (!this.pointers.size) this.element.style.cursor = '';
  }

  ball(point) {
    const rect = this.element.getBoundingClientRect(), radius = Math.max(1, Math.min(rect.width, rect.height) / 2);
    const x = (point.x - rect.x - rect.width / 2) / radius, y = (rect.y + rect.height / 2 - point.y) / radius;
    const squared = x * x + y * y;
    // A hyperbolic skirt joins the sphere smoothly and keeps edge drags finite.
    return new Vector3(x, y, squared <= 0.5 ? Math.sqrt(1 - squared) : 0.5 / Math.sqrt(squared)).normalize();
  }

  rotate(from, to) {
    const turn = new Quaternion().setFromUnitVectors(this.ball(to), this.ball(from));
    this.applyRotation(turn);
  }

  rotateBy(horizontal, vertical) {
    const turn = new Quaternion().setFromAxisAngle(new Vector3(0, 1, 0), -horizontal);
    turn.multiply(new Quaternion().setFromAxisAngle(new Vector3(1, 0, 0), -vertical));
    this.applyRotation(turn);
  }

  applyRotation(turn) {
    const frame = this.camera.quaternion.clone();
    turn.premultiply(frame).multiply(frame.clone().invert());
    this.camera.position.sub(this.target).applyQuaternion(turn).add(this.target);
    this.camera.up.applyQuaternion(turn).normalize(); this.update();
  }

  pan(dx, dy) {
    const height = this.element.getBoundingClientRect().height;
    if (!height) return;
    const scale = 2 * this.camera.position.distanceTo(this.target) * Math.tan(MathUtils.degToRad(this.camera.fov / 2)) / height;
    const right = new Vector3(1, 0, 0).applyQuaternion(this.camera.quaternion);
    const up = new Vector3(0, 1, 0).applyQuaternion(this.camera.quaternion);
    const delta = right.multiplyScalar(-dx * scale).addScaledVector(up, dy * scale);
    this.camera.position.add(delta); this.target.add(delta); this.update();
  }

  worldAt(point) {
    const rect = this.element.getBoundingClientRect();
    this.camera.updateMatrixWorld();
    const direction = new Vector3((point.x - rect.x) / rect.width * 2 - 1,
      -(point.y - rect.y) / rect.height * 2 + 1, 0.5).unproject(this.camera).sub(this.camera.position).normalize();
    const normal = this.camera.getWorldDirection(new Vector3());
    const depth = this.target.clone().sub(this.camera.position).dot(normal) / direction.dot(normal);
    return this.camera.position.clone().addScaledVector(direction, depth);
  }

  dolly(factor, point) {
    if (!Number.isFinite(factor) || factor <= 0) return;
    const distance = this.camera.position.distanceTo(this.target);
    if (!distance) return;
    factor = MathUtils.clamp(distance * factor, this.minDistance, this.maxDistance) / distance;
    const anchor = point ? this.worldAt(point) : this.target.clone();
    this.camera.position.sub(anchor).multiplyScalar(factor).add(anchor);
    this.target.sub(anchor).multiplyScalar(factor).add(anchor); this.update();
  }

  wheel(event) {
    if (!this.enabled) return;
    event.preventDefault();
    if (this.gestureScale !== null) return;
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? this.element.clientHeight : 1;
    this.dolly(Math.exp(MathUtils.clamp(event.deltaY * unit * (event.ctrlKey ? 0.01 : 0.0015), -1, 1)), this.point(event));
  }

  update() { this.camera.lookAt(this.target); this.camera.updateMatrixWorld(); this.dispatchEvent({ type: 'change' }); }
}
