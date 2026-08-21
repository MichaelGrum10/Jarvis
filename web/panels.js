/* Draggable HUD panels.
 *
 * The panels start where the layout puts them — a column each side on a wide
 * screen, stacked on a phone. Drag one by its heading and it detaches from that
 * layout and stays where you left it, on this device, until you reset it.
 *
 * Two things this deliberately does not do:
 *
 * - **It never drags from the panel body.** Headings only. The bodies scroll and
 *   contain links, and a drag that starts on content is a drag that eats every
 *   attempt to scroll the inbox.
 * - **It never lets a panel leave the screen.** Positions are clamped on drop
 *   *and* on resize, because a layout saved on a desktop and reopened on a phone
 *   would otherwise put half the panels somewhere unreachable, with no way back
 *   except clearing site data.
 */

const KEY = 'jarvis-hud-layout';
const EDGE = 8;             // px of panel that must stay on screen

let layout = load();

function load() {
  try {
    return JSON.parse(localStorage.getItem(KEY) || '{}') || {};
  } catch {
    return {};
  }
}

function save() {
  try {
    localStorage.setItem(KEY, JSON.stringify(layout));
  } catch { /* private mode: the layout just won't survive a reload */ }
}

function clamp(panel, left, top) {
  const width = panel.offsetWidth || 240;
  const height = panel.offsetHeight || 120;
  return {
    left: Math.min(Math.max(left, EDGE - width + 40), window.innerWidth - EDGE - 40),
    top: Math.min(Math.max(top, EDGE), window.innerHeight - EDGE - 28),
  };
}

function place(panel, left, top) {
  const safe = clamp(panel, left, top);
  panel.classList.add('moved');
  panel.style.left = `${safe.left}px`;
  panel.style.top = `${safe.top}px`;
  return safe;
}

/** Make one panel draggable by its heading, remembering where it lands. */
export function makeDraggable(panel) {
  const handle = panel.querySelector('h3');
  if (!handle || handle.dataset.drag) return;
  handle.dataset.drag = 'on';
  handle.classList.add('drag-handle');

  const saved = layout[panel.id];
  if (saved) place(panel, saved.left, saved.top);

  let originX = 0, originY = 0, startLeft = 0, startTop = 0, dragging = false;

  handle.addEventListener('pointerdown', (event) => {
    // Ignore anything but a primary press: a two-finger scroll or a right-click
    // is not a drag.
    if (event.button !== 0 && event.pointerType === 'mouse') return;
    const box = panel.getBoundingClientRect();
    originX = event.clientX;
    originY = event.clientY;
    startLeft = box.left;
    startTop = box.top;
    dragging = true;
    // Pin it where it already is before switching to absolute positioning, or
    // it jumps to the top-left corner on the first pointermove.
    place(panel, startLeft, startTop);
    panel.classList.add('dragging');
    handle.setPointerCapture(event.pointerId);
    event.preventDefault();
  });

  handle.addEventListener('pointermove', (event) => {
    if (!dragging) return;
    place(panel, startLeft + (event.clientX - originX), startTop + (event.clientY - originY));
  });

  const drop = (event) => {
    if (!dragging) return;
    dragging = false;
    panel.classList.remove('dragging');
    try { handle.releasePointerCapture(event.pointerId); } catch { /* already gone */ }
    const box = panel.getBoundingClientRect();
    layout[panel.id] = { left: Math.round(box.left), top: Math.round(box.top) };
    save();
  };
  handle.addEventListener('pointerup', drop);
  handle.addEventListener('pointercancel', drop);
}

/** Put every panel back where the stylesheet wanted it. */
export function resetLayout() {
  layout = {};
  save();
  for (const panel of document.querySelectorAll('.hud-panel.moved')) {
    panel.classList.remove('moved');
    panel.style.left = '';
    panel.style.top = '';
  }
}

export function layoutIsCustom() {
  return Object.keys(layout).length > 0;
}

/** Drag every panel that exists now, and keep them on screen afterwards. */
export function initPanels() {
  for (const panel of document.querySelectorAll('.hud-panel')) makeDraggable(panel);

  window.addEventListener('resize', () => {
    // A layout saved on a laptop and opened on a phone puts panels off the
    // right-hand edge. Re-clamping on resize is what makes that recoverable
    // without knowing the reset button exists.
    for (const panel of document.querySelectorAll('.hud-panel.moved')) {
      const box = panel.getBoundingClientRect();
      const safe = place(panel, box.left, box.top);
      layout[panel.id] = { left: Math.round(safe.left), top: Math.round(safe.top) };
    }
    save();
  });
}
