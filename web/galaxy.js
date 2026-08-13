/* The knowledge galaxy: every note a star, every link between them a thread.
 *
 * Drawn on a 2D canvas with a hand-rolled perspective projection rather than
 * WebGL or a graph library. Three reasons, in order of weight:
 *
 *  1. Nothing may be fetched from a CDN. This runs on the user's own server and
 *     has to work with the phone offline, so a library would have to be vendored
 *     and kept in step by hand — for a few hundred lines of maths.
 *  2. A few thousand points is nowhere near where canvas struggles. The cost
 *     here is the depth sort, not the fill.
 *  3. Sorted painting gives depth fade and haze for free, which is most of what
 *     makes a point cloud read as a galaxy rather than as confetti.
 *
 * The layout is not a full force simulation: n-body repulsion over a whole vault
 * is O(n²) per frame and would melt a phone. Folders become clusters placed on a
 * sphere, notes sit in a ball around their folder, and only the links pull. The
 * structure you see is therefore honest about folders and honest about links,
 * and does not pretend to be a global energy minimum.
 */

const TAU = Math.PI * 2;
const LINK_BUDGET = 1400;      // lines drawn per frame before the faintest are cut
const LABEL_BUDGET = 28;       // titles on screen at once, nearest first
const RELAX_STEPS = 220;

/* ---------------- layout ---------------- */

function fibonacciSphere(count, radius) {
  // Even spread without clumping at the poles, which a naive random spherical
  // pick gives you.
  const out = [];
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < count; i++) {
    const y = count === 1 ? 0 : 1 - (i / (count - 1)) * 2;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = golden * i;
    out.push({ x: Math.cos(theta) * r * radius, y: y * radius, z: Math.sin(theta) * r * radius });
  }
  return out;
}

function hashHue(text) {
  let h = 0;
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) >>> 0;
  return h % 360;
}

function layout(nodes, links) {
  const folders = [...new Set(nodes.map((n) => n.group))].sort();
  const centres = fibonacciSphere(folders.length, folders.length > 1 ? 520 : 0);
  const centreOf = new Map(folders.map((f, i) => [f, centres[i]]));

  const points = nodes.map((n) => {
    const c = centreOf.get(n.group);
    // A deterministic scatter, so the galaxy looks the same every time it is
    // opened. A random one would reshuffle on every reload and make the shape
    // impossible to learn.
    const seed = hashHue(n.label + n.group);
    const a = (seed % 360) / 360 * TAU;
    const b = ((seed >> 8) % 360) / 360 * TAU;
    const r = 40 + ((seed >> 16) % 100) * 1.4;
    return {
      x: c.x + Math.cos(a) * Math.sin(b) * r,
      y: c.y + Math.sin(a) * Math.sin(b) * r,
      z: c.z + Math.cos(b) * r,
      hue: hashHue(n.group),
      node: n,
    };
  });

  // Springs on the links, and a weak pull home so a note linked across the vault
  // does not get dragged out of its own cluster entirely.
  for (let step = 0; step < RELAX_STEPS; step++) {
    const rate = 0.06 * (1 - step / RELAX_STEPS);
    for (const link of links) {
      const a = points[link.source];
      const b = points[link.target];
      if (!a || !b) continue;
      const dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
      const dist = Math.hypot(dx, dy, dz) || 1;
      const pull = ((dist - 150) / dist) * rate;
      a.x += dx * pull; a.y += dy * pull; a.z += dz * pull;
      b.x -= dx * pull; b.y -= dy * pull; b.z -= dz * pull;
    }
    for (const p of points) {
      const c = centreOf.get(p.node.group);
      p.x += (c.x - p.x) * rate * 0.12;
      p.y += (c.y - p.y) * rate * 0.12;
      p.z += (c.z - p.z) * rate * 0.12;
    }
  }
  return points;
}

/* ---------------- the viewer ---------------- */

export class Galaxy {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.points = [];
    this.links = [];
    this.highlight = new Set();
    this.cam = { yaw: 0.4, pitch: -0.25, dist: 1500, tx: 0, ty: 0, tz: 0 };
    this.want = { ...this.cam };
    this.spin = 0.00035;
    this.running = false;
    this.onOpen = null;
    this._bind();
  }

  load(graph) {
    this.links = graph.links.map((l) => ({ source: l.source, target: l.target }));
    this.points = layout(graph.nodes, this.links);
    this.frame();
    return this.points.length;
  }

  /* --- camera --- */

  frame() {
    // Fit the whole cloud, then sit back far enough that it reads as a volume.
    let span = 400;
    for (const p of this.points) span = Math.max(span, Math.hypot(p.x, p.y, p.z));
    this.want = { yaw: 0.4, pitch: -0.25, dist: span * 2.6, tx: 0, ty: 0, tz: 0 };
    this.highlight = new Set();
  }

  /** Dive to a set of nodes — the move the whole thing exists for. */
  flyTo(ids) {
    const chosen = ids.map((i) => this.points[i]).filter(Boolean);
    if (!chosen.length) return false;
    this.highlight = new Set(ids);

    const mid = chosen.reduce(
      (acc, p) => ({ x: acc.x + p.x / chosen.length, y: acc.y + p.y / chosen.length, z: acc.z + p.z / chosen.length }),
      { x: 0, y: 0, z: 0 }
    );
    // Far enough back that every highlighted star is still in shot; a dive that
    // frames one of five answers is worse than no dive.
    let spread = 120;
    for (const p of chosen) spread = Math.max(spread, Math.hypot(p.x - mid.x, p.y - mid.y, p.z - mid.z));

    this.want = {
      yaw: Math.atan2(mid.x, mid.z) + 0.5,
      pitch: -0.2,
      dist: Math.max(320, spread * 3.2),
      tx: mid.x, ty: mid.y, tz: mid.z,
    };
    this.start();
    return true;
  }

  _project(p) {
    const c = this.cam;
    const x = p.x - c.tx, y = p.y - c.ty, z = p.z - c.tz;
    const cy = Math.cos(c.yaw), sy = Math.sin(c.yaw);
    const rx = x * cy - z * sy;
    const rz = x * sy + z * cy;
    const cp = Math.cos(c.pitch), sp = Math.sin(c.pitch);
    const ry = y * cp - rz * sp;
    const depth = y * sp + rz * cp + c.dist;
    if (depth < 40) return null;
    const scale = this.focal / depth;
    return { sx: this.w / 2 + rx * scale, sy: this.h / 2 + ry * scale, depth, scale };
  }

  /* --- drawing --- */

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = this.canvas.getBoundingClientRect();
    this.w = rect.width; this.h = rect.height;
    this.canvas.width = Math.round(this.w * dpr);
    this.canvas.height = Math.round(this.h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.focal = Math.max(this.w, this.h) * 0.9;
  }

  draw() {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.w, this.h);
    if (!this.points.length) return;

    const seen = new Array(this.points.length);
    let far = 1, near = Infinity;
    for (let i = 0; i < this.points.length; i++) {
      const p = (seen[i] = this._project(this.points[i]));
      if (!p) continue;
      if (p.depth > far) far = p.depth;
      if (p.depth < near) near = p.depth;
    }
    const range = Math.max(1, far - near);

    // Threads first, behind every star, and faint: the links are context, and a
    // dense vault turns into a ball of wool if they compete with the nodes.
    ctx.lineWidth = 1;
    let drawn = 0;
    for (const link of this.links) {
      if (drawn >= LINK_BUDGET) break;
      const a = seen[link.source], b = seen[link.target];
      if (!a || !b) continue;
      const lit = this.highlight.has(link.source) || this.highlight.has(link.target);
      const fade = 1 - (Math.min(a.depth, b.depth) - near) / range;
      ctx.strokeStyle = lit
        ? `rgba(120, 230, 255, ${0.30 + fade * 0.35})`
        : `rgba(110, 160, 210, ${0.04 + fade * 0.10})`;
      ctx.beginPath();
      ctx.moveTo(a.sx, a.sy);
      ctx.lineTo(b.sx, b.sy);
      ctx.stroke();
      drawn++;
    }

    // Back to front, so near stars paint over far ones and the haze reads.
    const order = [];
    for (let i = 0; i < seen.length; i++) if (seen[i]) order.push(i);
    order.sort((i, j) => seen[j].depth - seen[i].depth);

    const labels = [];
    for (const i of order) {
      const p = seen[i];
      const point = this.points[i];
      const lit = this.highlight.has(i);
      const fade = 1 - (p.depth - near) / range;
      const r = Math.max(1, (2.2 + Math.min(3, (point.node.size || 0) / 900)) * p.scale * 260);

      ctx.beginPath();
      ctx.arc(p.sx, p.sy, Math.min(r, 26), 0, TAU);
      ctx.fillStyle = lit
        ? '#9ff2ff'
        : `hsla(${point.hue}, 70%, ${52 + fade * 22}%, ${0.25 + fade * 0.7})`;
      ctx.fill();

      if (lit) {
        ctx.beginPath();
        ctx.arc(p.sx, p.sy, Math.min(r, 26) + 7, 0, TAU);
        ctx.strokeStyle = 'rgba(120, 230, 255, 0.85)';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
      if (lit || (labels.length < LABEL_BUDGET && p.scale * 260 > 1.6)) {
        labels.push({ p, label: point.node.label, lit });
      }
    }

    ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.textAlign = 'center';
    for (const { p, label, lit } of labels.slice(0, LABEL_BUDGET + this.highlight.size)) {
      ctx.fillStyle = lit ? 'rgba(200, 245, 255, 0.95)' : 'rgba(190, 210, 230, 0.45)';
      ctx.fillText(label.slice(0, 34), p.sx, p.sy - 12);
    }
  }

  step() {
    if (!this.running) return;
    const c = this.cam, w = this.want;
    // Eased chase rather than a scripted tween: a new flyTo mid-flight just
    // changes the target, and the camera never jumps.
    for (const k of ['yaw', 'pitch', 'dist', 'tx', 'ty', 'tz']) {
      c[k] += (w[k] - c[k]) * 0.075;
    }
    if (!this.dragging) { c.yaw += this.spin; w.yaw += this.spin; }
    this.draw();
    requestAnimationFrame(() => this.step());
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.resize();
    requestAnimationFrame(() => this.step());
  }

  stop() { this.running = false; }

  /* --- input --- */

  _bind() {
    const canvas = this.canvas;
    let last = null, moved = 0, pinch = 0;

    const pointAt = (cx, cy) => {
      const rect = canvas.getBoundingClientRect();
      const x = cx - rect.left, y = cy - rect.top;
      let best = null, bestDist = 22;
      for (let i = 0; i < this.points.length; i++) {
        const p = this._project(this.points[i]);
        if (!p) continue;
        const d = Math.hypot(p.sx - x, p.sy - y);
        if (d < bestDist) { bestDist = d; best = i; }
      }
      return best;
    };

    canvas.addEventListener('pointerdown', (e) => {
      last = { x: e.clientX, y: e.clientY };
      moved = 0;
      this.dragging = true;
      canvas.setPointerCapture(e.pointerId);
    });

    canvas.addEventListener('pointermove', (e) => {
      if (!last) return;
      const dx = e.clientX - last.x, dy = e.clientY - last.y;
      moved += Math.abs(dx) + Math.abs(dy);
      this.want.yaw = this.cam.yaw - dx * 0.006;
      this.want.pitch = Math.max(-1.4, Math.min(1.4, this.cam.pitch - dy * 0.006));
      last = { x: e.clientX, y: e.clientY };
    });

    const release = (e) => {
      this.dragging = false;
      // A tap is a click that did not travel. Without the threshold every orbit
      // gesture would also open whatever note it finished over.
      if (last && moved < 8) {
        const hit = pointAt(e.clientX, e.clientY);
        if (hit !== null && this.onOpen) {
          this.highlight = new Set([hit]);
          this.onOpen(this.points[hit].node, hit);
        }
      }
      last = null;
    };
    canvas.addEventListener('pointerup', release);
    canvas.addEventListener('pointercancel', () => { this.dragging = false; last = null; });

    canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      this.want.dist = Math.max(200, Math.min(9000, this.want.dist * (1 + e.deltaY * 0.0015)));
    }, { passive: false });

    canvas.addEventListener('touchmove', (e) => {
      if (e.touches.length !== 2) return;
      e.preventDefault();
      const gap = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      if (pinch) this.want.dist = Math.max(200, Math.min(9000, this.want.dist * (pinch / gap)));
      pinch = gap;
    }, { passive: false });
    canvas.addEventListener('touchend', () => { pinch = 0; });

    window.addEventListener('resize', () => { if (this.running) this.resize(); });
  }
}

/* ---------------- the overlay around it ---------------- */

let view = null;
let deps = { api: null };
let loaded = false;

export function initGalaxy(options) {
  deps = options;
}

function $(id) { return document.getElementById(id); }

function status(text) {
  const box = $('galaxy-status');
  if (box) { box.textContent = text || ''; box.classList.toggle('hidden', !text); }
}

async function showNote(node, index) {
  const box = $('galaxy-note');
  box.classList.remove('hidden');
  box.innerHTML = `<h4>${node.label}</h4><p class="muted">${node.group}</p><pre>Loading…</pre>`;
  try {
    const full = await deps.api(`/api/notes/note/${index}`);
    box.innerHTML = '';
    const head = document.createElement('h4');
    head.textContent = full.title;
    const where = document.createElement('p');
    where.className = 'muted';
    where.textContent = full.folder;
    const body = document.createElement('pre');
    // textContent, not innerHTML: a note is the user's own writing and may
    // legitimately contain angle brackets, and none of it should ever run.
    body.textContent = full.text;
    const close = document.createElement('button');
    close.className = 'ghost-btn';
    close.textContent = 'Close';
    close.onclick = () => box.classList.add('hidden');
    box.append(head, where, body, close);
  } catch (err) {
    box.querySelector('pre').textContent = `Could not read the note: ${err.message}`;
  }
}

/** Open the galaxy, loading the graph the first time. `ids` dives on arrival. */
export async function openGalaxy(ids) {
  const shell = $('galaxy');
  shell.classList.remove('hidden');

  if (!view) {
    view = new Galaxy($('galaxy-canvas'));
    view.onOpen = showNote;
    $('galaxy-close').onclick = closeGalaxy;
    $('galaxy-reset').onclick = () => { view.frame(); $('galaxy-note').classList.add('hidden'); };
    $('galaxy-search').addEventListener('keydown', async (e) => {
      if (e.key !== 'Enter') return;
      const q = e.target.value.trim();
      if (!q) return;
      try {
        const { notes } = await deps.api(`/api/notes/search?q=${encodeURIComponent(q)}`);
        if (!notes.length) return status(`Nothing in the notes matches “${q}”.`);
        view.flyTo(notes.map((n) => n.node));
        status(`${notes.length} note${notes.length === 1 ? '' : 's'} · ${notes[0].title}`);
      } catch (err) {
        status(err.message);
      }
    });
  }

  view.start();

  if (!loaded) {
    status('Reading your notes…');
    try {
      const graph = await deps.api('/api/notes/graph');
      const count = view.load(graph);
      loaded = true;
      status(count ? `${count} notes · ${graph.links.length} links` : 'No notes found in your vault yet.');
    } catch (err) {
      // The likely cause by far is NOTES_DIR being unset, and the endpoint says
      // exactly that — so show what it said rather than a generic failure.
      status(err.message);
      return;
    }
  }
  if (ids && ids.length) view.flyTo(ids);
}

export function closeGalaxy() {
  $('galaxy').classList.add('hidden');
  $('galaxy-note').classList.add('hidden');
  if (view) view.stop();
}

export function galaxyIsOpen() {
  const shell = $('galaxy');
  return shell && !shell.classList.contains('hidden');
}

/** Dive to notes an answer came from, but only if the user is already looking. */
export function galaxyFlyTo(ids) {
  if (view && galaxyIsOpen()) view.flyTo(ids);
}

/** Drop a freshly captured note in without re-reading the whole vault later. */
export function galaxyInvalidate() {
  loaded = false;
}
