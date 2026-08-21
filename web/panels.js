/* Draggable HUD panels.
 *
 * The panels start where the layout puts them — a column each side on a wide
 * screen, stacked on a phone. Drag one by its heading and it detaches from that
 * layout and stays where you left it for the rest of the session.
 *
 * **Deliberately not remembered across a reload.** Positions live in memory
 * only: reloading puts everything back where the stylesheet wanted it. A
 * dragged panel is a temporary rearrangement — moved aside to see something —
 * not a preference, and a layout that persists turns one careless drag into a
 * page that stays wrong until you find the reset button.
 *
 * **Only where the panels are docked.** On a narrow screen they are stacked in
 * normal flow under the ring and the HUD scrolls; dragging one out of that
 * stack leaves a hole and puts it over the thing you were reading. Worse, the
 * `touch-action: none` that dragging needs would eat the scroll gesture on the
 * one layout that depends on scrolling. So on a phone they are furniture, and
 * the drag handles are not attached at all.
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

const EDGE = 8;             // px of panel that must stay on screen

// The same breakpoint as the stylesheet's docking rule. Below it the panels are
// in normal flow and must stay there.
const DOCKED = window.matchMedia('(min-width: 980px)');

// This session only. Nothing is written to storage — see the note above.
let layout = {};

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

  let originX = 0, originY = 0, startLeft = 0, startTop = 0, dragging = false;

  handle.addEventListener('pointerdown', (event) => {
    // Checked here as well as at setup: a window resized across the breakpoint
    // keeps its listeners, and the layout underneath has changed.
    if (!DOCKED.matches) return;
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
  };
  handle.addEventListener('pointerup', drop);
  handle.addEventListener('pointercancel', drop);
}

/** Put every panel back where the stylesheet wanted it. */
export function resetLayout() {
  layout = {};
  for (const panel of document.querySelectorAll('.hud-panel.moved')) {
    panel.classList.remove('moved');
    panel.style.left = '';
    panel.style.top = '';
  }
}

export function layoutIsCustom() {
  return Object.keys(layout).length > 0;
}

/** Put the panels where this screen wants them, draggable or not.
 *
 * Called on load and whenever the viewport crosses the breakpoint. Rotating a
 * phone, or dragging a window narrow, has to return the panels to the stack —
 * and turning back has to give them their positions again, so going narrow
 * clears the layout from the page while keeping it in memory.
 */
function applyLayout() {
  const docked = DOCKED.matches;
  for (const panel of document.querySelectorAll('.hud-panel')) {
    const handle = panel.querySelector('h3');
    // The class carries the grab cursor and, more importantly, touch-action:
    // none. Leaving it on a phone would eat the scroll gesture the stacked
    // layout depends on.
    handle?.classList.toggle('drag-handle', docked);

    const saved = docked ? layout[panel.id] : null;
    if (saved) {
      place(panel, saved.left, saved.top);
    } else {
      panel.classList.remove('moved');
      panel.style.left = '';
      panel.style.top = '';
    }
  }
}

/** Set the panels up for this screen, and keep them on it. */
export function initPanels() {
  // An earlier version persisted layouts. Nothing reads that key now, so a
  // browser that used it would carry a dead entry forever; clear it once.
  try { localStorage.removeItem('jarvis-hud-layout'); } catch { /* private mode */ }

  for (const panel of document.querySelectorAll('.hud-panel')) makeDraggable(panel);
  applyLayout();

  // Safari before 14 has no addEventListener on a MediaQueryList.
  if (DOCKED.addEventListener) DOCKED.addEventListener('change', applyLayout);
  else DOCKED.addListener(applyLayout);

  window.addEventListener('resize', () => {
    if (!DOCKED.matches) return;
    // Dragging a window smaller can leave a panel off the right-hand edge.
    // Re-clamping on resize is what makes that recoverable without knowing the
    // reset button exists.
    for (const panel of document.querySelectorAll('.hud-panel.moved')) {
      const box = panel.getBoundingClientRect();
      const safe = place(panel, box.left, box.top);
      layout[panel.id] = { left: Math.round(safe.left), top: Math.round(safe.top) };
    }
  });
}
