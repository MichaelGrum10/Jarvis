/* The knowledge galaxy: every note a star, every reference a thread.
 *
 * Rendered with 3d-force-graph (WebGL), vendored into web/vendor/ rather than
 * pulled from a CDN. Three reasons, and the first is not negotiable: this app
 * is served from the user's own box behind their own password, and a CDN tag
 * would hand a third party a request every time the galaxy is opened. The
 * second is the service worker — an offline-capable PWA cannot depend on
 * unpkg being reachable. The third is that we have already lost an evening to
 * a CDN-shaped failure this month.
 *
 * The 1.3MB bundle is loaded lazily, on first open, for the same reason the
 * tool schemas are trimmed per turn: most sessions never open the galaxy, and
 * paying a megabyte at startup to make a rare action faster is the wrong
 * trade on a phone.
 *
 * Data comes live from /api/notes/graph, which is authenticated and always
 * current. That is the whole reason this lives in the app rather than beside
 * it: the standalone viewer renders a snapshot you have to rebuild by hand,
 * and is unreachable from a phone without an SSH tunnel.
 */

const LIB = '/static/vendor/3d-force-graph.min.js?v=15';

let graph = null;          // the ForceGraph3D instance
let deps = { api: null };
let loaded = false;        // graph data fetched
let libPromise = null;
let byId = new Map();
let neighbours = new Map();
let selected = null;
let highlight = new Set();
let pendingFly = null;     // ids to fly to once the layout has coordinates
let fitted = false;        // whether the opening zoom-to-fit has happened
let starTimer = null;
let generation = '';   // which graph the browser is holding
let session = '';      // per-tab, so follow-up questions have context

function $(id) { return document.getElementById(id); }

export function initGalaxy(options) { deps = options; }

/* ---------------- lazy library load ---------------- */

function loadLibrary() {
  if (window.ForceGraph3D) return Promise.resolve();
  if (libPromise) return libPromise;

  libPromise = new Promise((resolve, reject) => {
    const tag = document.createElement('script');
    tag.src = LIB;
    tag.onload = () => (window.ForceGraph3D ? resolve() : reject(new Error('library loaded but defined nothing')));
    tag.onerror = () => reject(new Error('could not load the 3D library from this server'));
    document.head.appendChild(tag);
  });
  return libPromise;
}

/* ---------------- starfield ---------------- */
/* A plain 2D canvas behind a transparent WebGL canvas. Not three.js points:
   doing it this way means a breaking change in the graph library cannot take
   the background with it, and it costs nothing to run. */

function startStars() {
  const canvas = $('galaxy-stars');
  const ctx = canvas.getContext('2d');
  let stars = [];

  function seed() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = window.innerWidth, h = window.innerHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    stars = [];
    // Three parallax layers. The slow one reads as distance; the fast one is
    // what makes the drift legible at all.
    for (const layer of [
      { n: 220, v: 0.006, r: 0.7, a: 0.40 },
      { n: 110, v: 0.017, r: 1.1, a: 0.60 },
      { n: 40,  v: 0.034, r: 1.6, a: 0.90 },
    ]) {
      for (let i = 0; i < layer.n; i++) {
        stars.push({
          x: Math.random() * w, y: Math.random() * h,
          r: layer.r * (0.6 + Math.random() * 0.8),
          a: layer.a * (0.5 + Math.random() * 0.5),
          v: layer.v, t: Math.random() * Math.PI * 2,
        });
      }
    }
  }

  function draw(now) {
    const w = window.innerWidth, h = window.innerHeight;
    ctx.clearRect(0, 0, w, h);
    for (const s of stars) {
      s.x -= s.v;
      if (s.x < -2) { s.x = w + 2; s.y = Math.random() * h; }
      const a = s.a * (0.75 + 0.25 * Math.sin(now * 0.001 + s.t));
      ctx.fillStyle = `rgba(200, 225, 255, ${a.toFixed(3)})`;
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fill();
    }
    starTimer = requestAnimationFrame(draw);
  }

  seed();
  window.addEventListener('resize', seed);
  starTimer = requestAnimationFrame(draw);
}

function stopStars() {
  if (starTimer) cancelAnimationFrame(starTimer);
  starTimer = null;
}

/* ---------------- helpers ---------------- */

function hueOf(text) {
  let h = 0;
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) >>> 0;
  return h % 360;
}

function ends(link) {
  return [
    typeof link.source === 'object' ? link.source.id : link.source,
    typeof link.target === 'object' ? link.target.id : link.target,
  ];
}

function status(text) {
  const box = $('galaxy-status');
  if (!box) return;
  box.textContent = text || '';
  box.classList.toggle('hidden', !text);
}

/* ---------------- building ---------------- */

function build(data) {
  const hues = {};
  for (const n of data.nodes) if (!(n.group in hues)) hues[n.group] = hueOf(n.group);

  // Adjacency from the raw numeric ids, before the library is handed the data:
  // 3d-force-graph rewrites link.source/target into node objects in place, so
  // reading them afterwards means handling both shapes forever.
  byId = new Map(data.nodes.map((n) => [n.id, n]));
  neighbours = new Map(data.nodes.map((n) => [n.id, new Set()]));
  for (const link of data.links) {
    const [a, b] = ends(link);
    if (neighbours.has(a)) neighbours.get(a).add(b);
    if (neighbours.has(b)) neighbours.get(b).add(a);
  }

  graph = window.ForceGraph3D()($('galaxy-graph'))
    .graphData(data)
    .backgroundColor('rgba(0,0,0,0)')   // starfield shows through from beneath
    .showNavInfo(false)
    .nodeLabel((n) => n.label)
    .nodeRelSize(5)
    // Longer notes are bigger stars — variety from something real rather than
    // from a random number.
    .nodeVal((n) => 1 + Math.min(6, (n.size || (n.excerpt || '').length) / 160))
    .nodeOpacity(0.95)
    .nodeResolution(12)
    .nodeColor((n) => {
      const h = hues[n.group];
      if (selected === null && !highlight.size) return `hsl(${h}, 78%, 66%)`;
      if (n.id === selected) return '#ffffff';
      if (highlight.has(n.id)) return `hsl(${h}, 95%, 74%)`;
      return `hsla(${h}, 30%, 40%, 0.28)`;
    })
    .linkColor((l) => {
      if (selected === null && !highlight.size) return 'rgba(140, 185, 235, 0.32)';
      const [a, b] = ends(l);
      const touching = highlight.has(a) || highlight.has(b) || a === selected || b === selected;
      return touching ? 'rgba(127, 230, 255, 0.85)' : 'rgba(120, 160, 210, 0.05)';
    })
    .linkWidth((l) => {
      const [a, b] = ends(l);
      return (a === selected || b === selected) ? 1.4 : 0.4;
    })
    // Light travelling the threads. Built in, needs no three.js of our own,
    // and does more for the feel of the thing than anything else here.
    .linkDirectionalParticles((l) => {
      const [a, b] = ends(l);
      return (a === selected || b === selected) ? 4 : 1;
    })
    .linkDirectionalParticleWidth(1.5)
    .linkDirectionalParticleSpeed(0.005)
    .onNodeClick((n) => select(n))
    .onBackgroundClick(() => clearSelection());

  graph.d3Force('charge').strength(-140);

  // The library's default cooldown is 15 seconds, and onEngineStop — which is
  // what triggers the opening framing — does not fire until then. Fifteen
  // seconds of stars sitting off the edge of the screen is indistinguishable
  // from a broken viewer. A personal vault settles in well under five.
  graph.cooldownTime(5000);

  // Nodes have no coordinates until the simulation has run, so a fly-to
  // requested during loading has to wait for them rather than aim at NaN.
  //
  // The same applies to framing. The default camera sits at a fixed distance
  // that has nothing to do with how big the layout turned out, so a small vault
  // opens with its stars scattered off the edges of the screen. Fitting once
  // the simulation settles is what makes it open looking composed.
  graph.onEngineStop(() => {
    if (pendingFly) {
      const ids = pendingFly;
      pendingFly = null;
      flyTo(ids);
      return;
    }
    // Once only: the engine restarts on interaction, and re-fitting every time
    // would yank the camera back while the user is looking at something.
    if (!fitted) {
      fitted = true;
      graph.zoomToFit(900, 110);
    }
  });

  // An early frame so it opens looking composed rather than waiting out the
  // whole cooldown. The authoritative fit still happens at engine stop, by
  // which time the layout has actually settled; 800ms is long enough for the
  // first few ticks to have spread the nodes into something worth framing.
  setTimeout(() => { if (graph && !fitted) graph.zoomToFit(500, 110); }, 800);

  const groups = Object.keys(hues).length;
  status(`${data.nodes.length} notes · ${groups} cluster${groups === 1 ? '' : 's'} · ${data.links.length} links`);
}

function repaint() {
  if (!graph) return;
  graph.nodeColor(graph.nodeColor())
       .linkColor(graph.linkColor())
       .linkWidth(graph.linkWidth())
       .linkDirectionalParticles(graph.linkDirectionalParticles());
}

/* ---------------- camera ---------------- */

/* `light` exists because the two callers want different things. A search wants
   its results lit up, so the camera target and the highlight are the same set.
   A click wants the node *and its neighbours* lit while flying to the node
   alone — and an earlier version let flyTo overwrite the highlight the click
   had just computed, so neighbours were dimmed the instant you selected
   anything. The whole point of clicking a star is seeing what it connects to. */
function flyTo(ids, light = true) {
  if (!graph) return false;
  const nodes = ids.map((i) => byId.get(i)).filter((n) => n && Number.isFinite(n.x));
  if (!nodes.length) { pendingFly = ids; return false; }

  if (light) highlight = new Set(ids);
  const mid = { x: 0, y: 0, z: 0 };
  for (const n of nodes) { mid.x += n.x / nodes.length; mid.y += n.y / nodes.length; mid.z += n.z / nodes.length; }

  // Far enough back that every highlighted star is in shot — a dive that frames
  // one of five answers is worse than no dive.
  let spread = 60;
  for (const n of nodes) spread = Math.max(spread, Math.hypot(n.x - mid.x, n.y - mid.y, n.z - mid.z));

  const back = Math.max(160, spread * 2.6);
  const span = Math.hypot(mid.x, mid.y, mid.z) || 1;
  const ratio = 1 + back / span;

  graph.cameraPosition(
    { x: mid.x * ratio, y: mid.y * ratio, z: mid.z * ratio },
    mid,
    1800
  );
  repaint();
  return true;
}

function select(node) {
  selected = node.id;
  highlight = new Set([node.id, ...(neighbours.get(node.id) || [])]);
  repaint();
  flyTo([node.id], false);   // keep the neighbour highlight set above
  showNote(node);
}

function clearSelection() {
  selected = null;
  highlight = new Set();
  repaint();
  $('galaxy-note').classList.add('hidden');
}

/* ---------------- the note panel ---------------- */

async function showNote(node) {
  const box = $('galaxy-note');
  box.classList.remove('hidden');
  box.innerHTML = '';

  const head = document.createElement('h4');
  head.textContent = node.label;
  const where = document.createElement('p');
  where.className = 'muted';
  where.textContent = `${node.group} · node ${node.id}`;
  const body = document.createElement('pre');
  body.textContent = node.excerpt || 'Loading…';
  const close = document.createElement('button');
  close.className = 'ghost-btn';
  close.textContent = 'Close';
  close.onclick = () => box.classList.add('hidden');
  box.append(head, where, body, close);

  try {
    const full = await deps.api(`/api/notes/note/${node.id}`);
    // textContent, never innerHTML: this is the user's own writing, it may
    // legitimately contain angle brackets, and none of it should ever run.
    body.textContent = full.text || '(empty note)';
  } catch (err) {
    body.textContent = `Could not read the note: ${err.message}`;
  }
}


/* ---------------- asking the vault ---------------- */

function renderAnswer(text, ids, warning, isError) {
  const box = $('galaxy-answer');
  box.classList.remove('hidden');
  box.innerHTML = '';

  const body = document.createElement('div');
  if (isError) body.className = 'err';
  // textContent, never innerHTML: this is the user's own writing coming back.
  body.textContent = text;
  box.appendChild(body);

  if (ids && ids.length) {
    const row = document.createElement('div');
    row.className = 'sources';
    for (const id of ids) {
      const node = byId.get(id);
      if (!node) continue;
      const chip = document.createElement('button');
      chip.className = 'src';
      chip.textContent = node.label;
      chip.onclick = () => select(node);
      row.appendChild(chip);
    }
    if (row.children.length) box.appendChild(row);
  }

  if (warning) {
    const warn = document.createElement('div');
    warn.className = 'warn';
    warn.textContent = warning;
    box.appendChild(warn);
  }
}

async function askVault() {
  const input = $('ask-input');
  const send = $('ask-send');
  const question = input.value.trim();
  if (!question) return;

  send.disabled = true;
  renderAnswer('Reading your notes…', null, null, false);

  try {
    const res = await deps.api('/api/notes/ask', {
      method: 'POST',
      body: JSON.stringify({ question, session, generation }),
    });
    const warning = res.stale
      ? 'Your notes changed since this opened — the highlighted stars may be wrong. Reopen the galaxy.'
      : null;
    renderAnswer(res.answer, res.nodes, warning, false);
    if (!res.stale && res.nodes && res.nodes.length) flyTo(res.nodes);
    input.value = '';
  } catch (err) {
    renderAnswer(err.message, null, null, true);
  } finally {
    send.disabled = false;
  }
}

/* ---------------- public API ---------------- */

export async function openGalaxy(ids) {
  const shell = $('galaxy');
  shell.classList.remove('hidden');

  if (!starTimer) startStars();

  if (!session) {
    session = Math.random().toString(36).slice(2) + Date.now().toString(36);
  }

  if (!graph) {
    $('ask-send').onclick = askVault;
    $('ask-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); askVault(); }
    });
    $('galaxy-close').onclick = closeGalaxy;
    $('galaxy-reset').onclick = () => {
      clearSelection();
      if (graph) graph.zoomToFit(1200, 110);
    };
    $('galaxy-search').addEventListener('keydown', async (e) => {
      if (e.key !== 'Enter') return;
      const q = e.target.value.trim();
      if (!q) return;
      try {
        const { notes } = await deps.api(`/api/notes/search?q=${encodeURIComponent(q)}`);
        if (!notes.length) return status(`Nothing matches “${q}”.`);
        flyTo(notes.map((n) => n.node));
        status(`${notes.length} match${notes.length === 1 ? '' : 'es'} · ${notes[0].title}`);
      } catch (err) {
        status(err.message);
      }
    });
  }

  if (!loaded) {
    status('Reading your notes…');
    try {
      await loadLibrary();
      const data = await deps.api('/api/notes/graph');
      if (!data.nodes.length) return status('No notes in your vault yet.');
      generation = data.generation || '';
      build(data);
      loaded = true;
    } catch (err) {
      // Far and away the likeliest cause is NOTES_DIR being unset, and the
      // endpoint says exactly that — so show what it said.
      status(err.message);
      return;
    }
  } else if (graph) {
    graph.resumeAnimation();
  }

  if (ids && ids.length) flyTo(ids);
}

export function closeGalaxy() {
  $('galaxy').classList.add('hidden');
  $('galaxy-note').classList.add('hidden');
  stopStars();
  // A WebGL scene left rendering behind a hidden div is pure battery drain.
  if (graph) graph.pauseAnimation();
}

export function galaxyIsOpen() {
  const shell = $('galaxy');
  return shell && !shell.classList.contains('hidden');
}

/** Dive to the notes an answer came from — but only if the user is looking. */
export function galaxyFlyTo(ids) {
  if (graph && galaxyIsOpen()) flyTo(ids);
}

/** A capture added a star; re-fetch the graph on next open. */
export function galaxyInvalidate() {
  loaded = false;
  fitted = false;
  generation = '';
  if (graph) { graph._destructor?.(); graph = null; }
  const host = $('galaxy-graph');
  if (host) host.innerHTML = '';
}
