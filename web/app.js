/* Jarvis PWA client.
 *
 * Three jobs beyond chat:
 *  - hand the server this device's real coordinates, since the server's own IP is
 *    a datacentre and says nothing about where you are;
 *  - poll for queued device commands and execute them locally (open app / URL /
 *    Shortcut), which is the only way a remote server can act on your phone;
 *  - render tool results as cards instead of walls of JSON.
 */

import { Listener, Speaker, voiceSupport, defaultMode, saveMode, isMobile, unlockSpeech, IS_WEBKIT } from '/static/voice.js?v=19';
import { WakeListener, captureUtterance, JarvisVoice, pickJarvisVoice, startHudPanels } from '/static/hud.js?v=19';
import { initGalaxy, openGalaxy, galaxyFlyTo, galaxyIsOpen, galaxyInvalidate } from '/static/galaxy.js?v=19';

const API = '';
const store = {
  get token() { return localStorage.getItem('jarvis_token'); },
  set token(v) { v ? localStorage.setItem('jarvis_token', v) : localStorage.removeItem('jarvis_token'); },
  get deviceId() { return localStorage.getItem('jarvis_device_id'); },
  set deviceId(v) { v ? localStorage.setItem('jarvis_device_id', v) : localStorage.removeItem('jarvis_device_id'); },
};

let conversationId = null;
let location_ = null;
let sending = false;
let commandTimer = null;

/* Phones start in text mode, desktops start in voice mode, and the mode button
 * switches either way — the choice persists per device. */
let mode = defaultMode();
let listener = null;
const speaker = new Speaker();
const jarvis = new JarvisVoice();

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (store.token) headers.Authorization = `Bearer ${store.token}`;
  const res = await fetch(API + path, { ...options, headers });

  // A 401 from the login endpoint means "wrong password" — the server's own
  // message for that, not an expired session (there was never a session to
  // expire). Only a 401 on an already-authenticated call means the device
  // token itself is invalid, which is the actual "session expired" case.
  const isLoginCall = path.startsWith('/api/auth/login');
  if (res.status === 401 && !isLoginCall) {
    signOut();
    throw new Error('Session expired — sign in again.');
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

/* ---------------- auth ---------------- */

async function signIn() {
  const password = $('password').value.trim();
  const label = $('label').value.trim() || guessDeviceName();
  if (!password) return;
  $('login-error').textContent = '';
  try {
    const data = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password, label, platform: navigator.platform || 'web', device_id: store.deviceId || '' }),
    });
    store.token = data.token;
    store.deviceId = data.device_id;
    enterApp();
  } catch (err) {
    $('login-error').textContent = err.message;
  }
}

function signOut() {
  store.token = null;
  clearInterval(commandTimer);
  $('app').classList.add('hidden');
  $('login').classList.remove('hidden');
}

function guessDeviceName() {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/iPad/.test(ua)) return 'iPad';
  if (/Macintosh/.test(ua)) return 'Mac';
  if (/Android/.test(ua)) return 'Android';
  return 'Browser';
}

async function enterApp() {
  checkSecureContext();
  hideDeadVoiceControls();
  showWakeGate();
  $('login').classList.add('hidden');
  $('app').classList.remove('hidden');
  $('status-dot').classList.add('on');
  if (!voiceSupport.any && mode === 'voice') mode = 'text';
  refreshIdentity().then(applyMode);
  $('voice-hint').textContent = tapHint();
  loadConversations();
  requestLocation();
  commandTimer = setInterval(pollCommands, 4000);
  pollCommands();
}

/* ---------------- toast ---------------- */

function toast(message, ms = 7000) {
  let box = $('toast');
  if (!box) {
    box = el('div', 'toast');
    box.id = 'toast';
    document.body.appendChild(box);
  }
  box.textContent = message;
  box.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => box.classList.remove('show'), ms);
}

/* ---------------- location ---------------- */

/* Location.
 *
 * iOS only shows the permission prompt in response to a user gesture. Asking on
 * page load — as this used to — gets denied without ever prompting, and iOS then
 * remembers that denial, so every later request fails instantly and the button
 * appears dead forever. The fix is to never ask automatically: on startup we
 * only *reuse* a permission already granted, and the actual prompt happens on
 * the tap, which is a gesture iOS accepts.
 */

function setLocationState(state, title) {
  const button = $('loc-btn');
  button.classList.toggle('active', state === 'granted');
  button.classList.toggle('pending', state === 'pending');
  button.classList.toggle('denied', state === 'denied');
  button.title = title || 'Share location';
}

async function locationPermission() {
  // Permissions API isn't available on older iOS; "unknown" simply means we ask.
  if (!navigator.permissions?.query) return 'unknown';
  try {
    return (await navigator.permissions.query({ name: 'geolocation' })).state;
  } catch {
    return 'unknown';
  }
}

function getPosition(options) {
  return new Promise((resolve, reject) =>
    navigator.geolocation.getCurrentPosition(resolve, reject, options));
}

async function requestLocation({ userInitiated = false } = {}) {
  if (!navigator.geolocation) {
    if (userInitiated) toast('This browser has no location support.');
    return;
  }

  // On startup, only proceed when permission is already granted. Asking here
  // burns the one prompt iOS will show, without a gesture to justify it.
  if (!userInitiated && (await locationPermission()) !== 'granted') {
    setLocationState('idle', 'Tap to share location');
    return;
  }

  setLocationState('pending', 'Getting location…');
  try {
    const position = await getPosition({
      enableHighAccuracy: true,
      timeout: 15000,
      maximumAge: 120000,
    });
    location_ = {
      lat: position.coords.latitude,
      lon: position.coords.longitude,
      accuracy_m: position.coords.accuracy,
    };
    setLocationState('granted', `Location shared (±${Math.round(location_.accuracy_m)}m)`);
    if (userInitiated) toast(`Location shared — accurate to about ${Math.round(location_.accuracy_m)}m.`);
    try {
      await api('/api/device/location', { method: 'POST', body: JSON.stringify(location_) });
    } catch { /* the coordinates still work for this session */ }
  } catch (err) {
    setLocationState('denied', 'Location unavailable');
    if (userInitiated) toast(locationErrorHelp(err));
  }
}

function locationErrorHelp(err) {
  // A raw "User denied Geolocation" is baffling when no prompt was ever shown,
  // and it doesn't say where the switch is. On iOS the fix is two settings deep.
  const onIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);
  switch (err?.code) {
    case 1:
      return onIOS
        ? 'Location is blocked. Settings → Privacy & Security → Location Services → '
          + 'Safari Websites → While Using the App. Then reload and tap again.'
        : 'Location is blocked. Allow it for this site in your browser settings, then tap again.';
    case 2:
      return 'Your device could not get a fix. Try again outdoors or near a window.';
    case 3:
      return 'Location timed out. Try again — the first fix can take a few seconds.';
    default:
      return `Location unavailable: ${err?.message || 'unknown error'}`;
  }
}

/* ---------------- device commands ---------------- */

async function pollCommands() {
  if (!store.token) return;
  try {
    const { commands } = await api('/api/device/commands');
    for (const cmd of commands) runCommand(cmd);
  } catch { /* offline is fine, we retry */ }
}

function runCommand(cmd) {
  if (cmd.kind === 'notify') {
    if (Notification?.permission === 'granted') new Notification('Jarvis', { body: cmd.text || '' });
    return;
  }
  if (!cmd.url) return;
  // Safari blocks window.open outside a user gesture, so offer a tap target that
  // definitely works instead of silently failing.
  const opened = window.open(cmd.url, '_blank');
  if (!opened) {
    const box = el('div', 'cards');
    const card = el('div', 'card');
    card.innerHTML = `<h4>Action ready</h4><a href="${esc(cmd.url)}" target="_blank" rel="noopener">Tap to open ${esc(cmd.app || cmd.name || 'link')}</a>`;
    box.appendChild(card);
    $('messages').appendChild(box);
    scrollDown();
  }
}

/* ---------------- chat ---------------- */

function clearWelcome() {
  const w = document.querySelector('.welcome');
  if (w) w.remove();
}

function addMessage(role, text) {
  clearWelcome();
  const wrap = el('div', `msg ${role}`);
  const bubble = el('div', 'bubble');
  bubble.innerHTML = role === 'assistant' ? renderMarkdown(text) : esc(text);
  wrap.appendChild(bubble);
  $('messages').appendChild(wrap);
  scrollDown();
  return bubble;
}

function renderMarkdown(text) {
  return esc(text)
    .replace(/```([\s\S]*?)```/g, (_, c) => `<pre><code>${c.trim()}</code></pre>`)
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(?<![\w"'>])\*([^*\n]+)\*(?![\w])/g, '<em>$1</em>')
    .replace(/(https?:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
    .replace(/\n/g, '<br>');
}

function scrollDown() {
  const m = $('messages');
  m.scrollTop = m.scrollHeight;
}

async function send(text) {
  if (sending || !text.trim()) return;
  sending = true;
  $('send').disabled = true;
  $('status-dot').className = 'dot busy';

  addMessage('user', text);
  $('input').value = '';
  $('input').style.height = 'auto';

  const activity = el('div', 'activity');
  $('messages').appendChild(activity);
  const typing = el('div', 'msg assistant');
  typing.innerHTML = '<div class="bubble"><span class="typing"><i></i><i></i><i></i></span></div>';
  $('messages').appendChild(typing);
  scrollDown();

  const displays = [];
  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${store.token}` },
      body: JSON.stringify({
        message: text,
        conversation_id: conversationId,
        lat: location_?.lat ?? null,
        lon: location_?.lon ?? null,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
      }),
    });
    if (res.status === 401) { signOut(); return; }
    if (!res.ok) throw new Error(`Server returned ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let finalText = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop() || '';

      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith('data:')) continue;
        let event;
        try { event = JSON.parse(line.slice(5).trim()); } catch { continue; }

        if (event.type === 'start') {
          conversationId = event.conversation_id;
        } else if (event.type === 'tool_start') {
          activity.appendChild(el('span', 'act', `${prettyTool(event.tool)}…`));
          scrollDown();
        } else if (event.type === 'tool_end') {
          const chips = activity.querySelectorAll('.act');
          const chip = chips[chips.length - 1];
          if (chip) {
            chip.className = `act ${event.ok ? 'ok' : 'err'}`;
            chip.textContent = event.ok ? prettyTool(event.tool) : `${prettyTool(event.tool)} failed`;
            // "Reading calendar failed" says nothing about why. The reason is
            // already in the event; showing it on tap turns a dead end into
            // something reportable without reading the server log.
            if (!event.ok && event.error) {
              chip.title = event.error;
              chip.onclick = () => { chip.textContent = event.error.slice(0, 160); };
            }
          }
          if (event.display) displays.push(event.display);
        } else if (event.type === 'final') {
          finalText = event.reply;
        } else if (event.type === 'error') {
          finalText = `⚠️ ${event.message}`;
        }
      }
    }

    typing.remove();
    if (displays.length) renderDisplays(displays);
  // Answered from the notes while the galaxy is up: fly there without being
  // asked, which is the whole experience the feature is for.
  for (const d of displays) {
    if (d.type === 'notes' && galaxyIsOpen()) galaxyFlyTo(d.notes.map((n) => n.node));
  }
    const reply = finalText || 'No response.';
    addMessage('assistant', reply);
    if (mode === 'voice') speakReply(reply);
    loadConversations();
  } catch (err) {
    typing.remove();
    addMessage('assistant', `⚠️ ${err.message}`);
  } finally {
    sending = false;
    $('send').disabled = false;
    $('status-dot').className = 'dot on';
  }
}

function prettyTool(name) {
  const map = {
    calendar_list: 'Reading calendar', calendar_create: 'Creating event',
    calendar_delete: 'Deleting event', calendar_find_free: 'Finding free time',
    mail_summary: 'Checking mail', mail_read: 'Opening email', mail_search: 'Searching mail',
    mail_send: 'Sending email', messages_recent: 'Reading messages',
    messages_send: 'Sending message', messages_threads: 'Reading threads',
    stock_quote: 'Fetching quotes', stock_history: 'Fetching history', stock_news: 'Stock news',
    wsj_headlines: 'Reading WSJ', news_headlines: 'Reading news',
    web_search: 'Searching web', web_read: 'Reading page',
    places_search: 'Finding places nearby', where_am_i: 'Locating you',
    device_open_app: 'Opening app', device_open_url: 'Opening link',
    device_run_shortcut: 'Running shortcut',
    memory_save: 'Remembering', memory_search: 'Recalling', memory_forget: 'Forgetting',
    current_time: 'Checking time', system_status: 'Checking status',
  };
  return map[name] || name.replace(/_/g, ' ');
}

/* ---------------- cards ---------------- */

function renderDisplays(displays) {
  const box = el('div', 'cards');
  for (const d of displays) {
    const card = renderCard(d);
    if (card) box.appendChild(card);
  }
  if (box.children.length) { clearWelcome(); $('messages').appendChild(box); scrollDown(); }
}

function renderCard(d) {
  const card = el('div', 'card');
  const rows = [];

  if (d.type === 'stocks') {
    card.appendChild(el('h4', null, 'Markets'));
    for (const q of d.quotes) {
      const dir = (q.change ?? 0) >= 0 ? 'up' : 'down';
      const sign = (q.change ?? 0) >= 0 ? '+' : '';
      rows.push(`<div class="row"><div class="r-main">
        <div class="r-title">${esc(q.symbol)}</div>
        <div class="r-sub">${esc(q.name || '')}</div></div>
        <div class="r-val">${q.price ?? '—'}<div class="r-sub ${dir}">${sign}${q.change ?? '—'} (${sign}${q.change_percent ?? '—'}%)</div></div></div>`);
    }
  } else if (d.type === 'stock_chart') {
    card.appendChild(el('h4', null, `${d.symbol} · ${d.period}`));
    const dir = (d.change_percent ?? 0) >= 0 ? 'up' : 'down';
    rows.push(`<div class="row"><div class="r-main"><div class="r-title">${d.start_price} → ${d.end_price}</div>
      <div class="r-sub">${esc(d.start_date)} to ${esc(d.end_date)}</div></div>
      <div class="r-val ${dir}">${(d.change_percent ?? 0) >= 0 ? '+' : ''}${d.change_percent}%</div></div>`);
    if (d.closes?.length) rows.push(sparkline(d.closes.map((c) => c.close)));
  } else if (d.type === 'calendar' || d.type === 'calendar_created') {
    const events = d.events || [d.event];
    card.appendChild(el('h4', null, d.type === 'calendar_created' ? 'Event created' : 'Calendar'));
    if (!events.length) rows.push('<div class="row"><div class="r-sub">Nothing scheduled.</div></div>');
    for (const e of events) {
      rows.push(`<div class="row"><div class="r-main">
        <div class="r-title">${esc(e.summary)}</div>
        <div class="r-sub">${esc(e.location || e.calendar || '')}</div></div>
        <div class="r-val"><div class="r-sub">${fmtWhen(e.start, e.all_day)}</div></div></div>`);
    }
  } else if (d.type === 'mail_list') {
    card.appendChild(el('h4', null, 'Inbox'));
    for (const m of d.messages.slice(0, 12)) {
      const hot = (m.priority ?? 0) >= 70 ? ' hot' : '';
      rows.push(`<div class="row"><div class="r-main">
        <div class="r-title">${m.unread ? '● ' : ''}${esc(m.subject)}</div>
        <div class="r-sub">${esc(m.sender)}</div></div>
        <div class="r-val">${m.priority !== undefined ? `<span class="pill${hot}">${m.priority}</span>` : ''}</div></div>`);
    }
  } else if (d.type === 'mail_full') {
    const m = d.message;
    card.appendChild(el('h4', null, 'Email'));
    rows.push(`<div class="row"><div class="r-main"><div class="r-title">${esc(m.subject)}</div>
      <div class="r-sub">${esc(m.sender)} · ${fmtWhen(m.date)}</div></div></div>`);
  } else if (d.type === 'messages') {
    card.appendChild(el('h4', null, 'Messages'));
    for (const m of d.messages.slice(0, 12)) {
      rows.push(`<div class="row"><div class="r-main">
        <div class="r-title">${esc(m.from)}${m.is_group ? ` · ${esc(m.chat)}` : ''}</div>
        <div class="r-sub">${esc((m.text || '').slice(0, 90))}</div></div>
        <div class="r-val"><div class="r-sub">${fmtWhen(m.at)}</div></div></div>`);
    }
  } else if (d.type === 'places') {
    card.appendChild(el('h4', null, 'Nearby'));
    for (const p of d.places) {
      rows.push(`<div class="row"><div class="r-main">
        <div class="r-title"><a href="${esc(p.maps_url)}" target="_blank" rel="noopener">${esc(p.name)}</a></div>
        <div class="r-sub">${esc(p.address || p.opening_hours || '')}${p.phone ? ` · ${esc(p.phone)}` : ''}</div></div>
        <div class="r-val"><div class="r-sub">${p.distance_km} km</div></div></div>`);
    }
  } else if (d.type === 'news' || d.type === 'search') {
    card.appendChild(el('h4', null, d.type === 'search' ? `Search · ${esc(d.query || '')}` : `${esc(d.source || 'News')}`));
    for (const a of (d.articles || d.results || []).slice(0, 10)) {
      rows.push(`<div class="row"><div class="r-main">
        <div class="r-title"><a href="${esc(a.link || a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a></div>
        <div class="r-sub">${esc((a.summary || a.snippet || '').slice(0, 120))}</div></div></div>`);
    }
  } else if (d.type === 'notes') {
    card.appendChild(el('h4', null, `Notes \u00b7 ${esc(d.query || '')}`));
    for (const n of d.notes.slice(0, 8)) {
      rows.push(`<div class="row"><div class="r-main">
        <div class="r-title">${esc(n.title)}</div>
        <div class="r-sub">${esc(n.folder)} \u00b7 ${esc((n.excerpt || '').slice(0, 110))}</div></div></div>`);
    }
    // The dive is the point of the galaxy, so the card that names the sources
    // is where it has to be offered.
    const open = el('button', 'note-open', '\u2727 Show these in the galaxy');
    open.onclick = () => openGalaxy(d.notes.map((n) => n.node));
    card.insertAdjacentHTML('beforeend', rows.join(''));
    card.appendChild(open);
    return card;
  } else if (d.type === 'note_created') {
    card.appendChild(el('h4', null, 'Noted'));
    rows.push(`<div class="row"><div class="r-main"><div class="r-title">${esc(d.title)}</div>
      <div class="r-sub">Written to your notes.</div></div></div>`);
    // A new star exists now; the next open should see it.
    galaxyInvalidate();
  } else if (d.type === 'device_action') {
    card.appendChild(el('h4', null, 'Device'));
    rows.push(`<div class="row"><div class="r-main"><div class="r-title">${esc(d.action)}</div></div></div>`);
  } else {
    return null;
  }

  card.insertAdjacentHTML('beforeend', rows.join(''));
  return card;
}

function sparkline(values) {
  if (values.length < 2) return '';
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const points = values.map((v, i) =>
    `${(i / (values.length - 1)) * 100},${30 - ((v - min) / span) * 28}`).join(' ');
  const rising = values[values.length - 1] >= values[0];
  return `<svg viewBox="0 0 100 30" preserveAspectRatio="none" style="width:100%;height:46px;margin-top:8px">
    <polyline points="${points}" fill="none" stroke="${rising ? '#3fd07f' : '#ff6b6b'}" stroke-width="1.4" vector-effect="non-scaling-stroke"/></svg>`;
}

function fmtWhen(iso, allDay = false) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  if (allDay) return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  if (sameDay) return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

/* ---------------- conversations ---------------- */

async function loadConversations() {
  try {
    const { conversations } = await api('/api/chat/conversations');
    const list = $('conv-list');
    list.innerHTML = '';
    for (const c of conversations) {
      const item = el('div', `conv-item${c.id === conversationId ? ' active' : ''}`, c.title);
      item.onclick = () => openConversation(c.id);
      list.appendChild(item);
    }
  } catch { /* not fatal */ }
}

async function openConversation(id) {
  const data = await api(`/api/chat/conversations/${id}`);
  conversationId = id;
  $('messages').innerHTML = '';
  for (const m of data.messages) {
    if (m.role !== 'user' && m.role !== 'assistant') continue;
    if (m.role === 'assistant' && m.displays?.length) renderDisplays(m.displays);
    addMessage(m.role, m.content);
  }
  closeDrawer();
  loadConversations();
}

function newConversation() {
  conversationId = null;
  $('messages').innerHTML = `<div class="welcome"><div class="logo big">J</div>
    <h2>What do you need?</h2></div>`;
  closeDrawer();
}

/* ---------------- modals ---------------- */

function showModal(title, html) {
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = html;
  $('modal').classList.remove('hidden');
}

async function showStatus() {
  closeDrawer();
  try {
    const health = await api('/api/health');
    const rows = Object.entries(health.features)
      .map(([k, v]) => `<div class="row"><div class="r-main"><div class="r-title">${k}</div></div>
        <div class="r-val ${v ? 'up' : 'down'}">${v ? 'ready' : 'not configured'}</div></div>`).join('');
    showModal('Status', `<div class="card">${rows}
      <div class="row"><div class="r-main"><div class="r-title">tools loaded</div></div>
      <div class="r-val">${health.tools}</div></div>
      <div class="row"><div class="r-main"><div class="r-title">self-improve</div></div>
      <div class="r-val ${health.autonomy_enabled ? 'up' : ''}">${health.autonomy_enabled ? 'enabled' : 'off'}</div></div></div>`);
  } catch (err) {
    showModal('Status', `<p class="error">${esc(err.message)}</p>`);
  }
}

function showAutonomy() {
  closeDrawer();
  showModal('Self-improve', `
    <p class="muted">Describe what Jarvis should build or fix in its own code. It works on a
    throwaway git branch, runs the tests, and hands you a diff to review. Nothing merges automatically.</p>
    <textarea id="auto-goal" placeholder="e.g. Add a weather tool using the free Open-Meteo API, with tests."></textarea>
    <button id="auto-run">Start run</button>
    <div id="auto-out"></div>`);
  $('auto-run').onclick = startAutonomy;
  loadRuns();
}

async function startAutonomy() {
  const goal = $('auto-goal').value.trim();
  if (goal.length < 8) return;
  const out = $('auto-out');
  out.innerHTML = '<p class="muted">Starting…</p>';
  try {
    const { run_id } = await api('/api/autonomy/run', { method: 'POST', body: JSON.stringify({ goal }) });
    pollRun(run_id);
  } catch (err) {
    out.innerHTML = `<p class="error">${esc(err.message)}</p>`;
  }
}

async function pollRun(id) {
  const out = $('auto-out');
  const tick = async () => {
    try {
      const run = await api(`/api/autonomy/runs/${id}`);
      out.innerHTML = `<div class="card"><h4>Run #${run.id} · ${esc(run.status)}</h4>
        ${run.branch ? `<div class="r-sub">branch: <code>${esc(run.branch)}</code></div>` : ''}
        <pre class="run-log">${esc(run.log || 'working…')}</pre>
        ${run.result ? `<pre class="run-log">${esc(run.result.slice(0, 4000))}</pre>` : ''}</div>`;
      if (run.status === 'queued' || run.status === 'running') setTimeout(tick, 2500);
    } catch (err) {
      out.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    }
  };
  tick();
}

async function loadRuns() {
  try {
    const { runs } = await api('/api/autonomy/runs');
    if (!runs.length) return;
    const rows = runs.slice(0, 6).map((r) =>
      `<div class="row"><div class="r-main"><div class="r-title">${esc(r.goal.slice(0, 60))}</div>
       <div class="r-sub">${esc(r.branch || '')}</div></div>
       <div class="r-val"><span class="pill">${esc(r.status)}</span></div></div>`).join('');
    $('auto-out').innerHTML = `<div class="card"><h4>Recent runs</h4>${rows}</div>`;
  } catch { /* autonomy may be off */ }
}

/* ---------------- voice ---------------- */

let stopHudPanels = null;

function applyMode() {
  const voice = mode === 'voice';
  // Panels poll on a timer, so they run only while the HUD is on screen. Left
  // running in text mode they would keep hitting iCloud and Yahoo for a display
  // nobody is looking at.
  if (voice && !stopHudPanels) {
    stopHudPanels = startHudPanels(api);
  } else if (!voice && stopHudPanels) {
    stopHudPanels();
    stopHudPanels = null;
  }
  // In voice mode the HUD takes the whole screen: no transcript, no composer,
  // no message list. You already know what you said, and reading a reply you are
  // simultaneously being told is just noise.
  $('hud').classList.toggle('hidden', !voice);
  $('messages').classList.toggle('hidden', voice);
  $('composer').classList.toggle('hidden', voice);
  $('voice-panel').classList.add('hidden');   // superseded by the HUD
  $('mode-btn').textContent = voice ? '⌨' : '🎙';
  $('mode-btn').title = voice ? 'Switch to typing' : 'Switch to voice';
  // Dictation stays available in text mode — it just doesn't take over the screen.
  // Gated on the secure context too: over plain HTTP getUserMedia never resolves,
  // so the button would look functional and do nothing at all.
  $('mic-btn').classList.toggle('hidden', !voiceUsable());

  if (voice) {
    startWakeWord();
    setHudState('idle', identityState.enrolled ? 'Say "Jarvis", or tap' : 'Tap to speak');
  } else {
    stopWakeWord();
    stopListening();
    jarvis.cancel();
    speaker.cancel();
  }
}

function setMode(next) {
  mode = next;
  saveMode(next);
  applyMode();
}

/* ---------------- HUD state ----------------
 * idle | listening | thinking | speaking | denied
 * The ring's colour and motion carry the state; there is deliberately no text
 * transcript in voice mode. */

function setHudState(state, status) {
  $('hud').dataset.state = state;
  if (status !== undefined) $('hud-status').textContent = status;
}

function hudAlert(message) {
  const box = $('hud-alert');
  box.textContent = message;
  box.classList.remove('hidden');
  clearTimeout(hudAlert._timer);
  hudAlert._timer = setTimeout(() => box.classList.add('hidden'), 6000);
}

function speakReply(text) {
  if (mode === 'voice') {
    jarvis.speak(text, {
      onStart: () => setHudState('speaking', 'Speaking'),
      onEnd: () => {
        setHudState('idle', identityState.enrolled ? 'Say "Jarvis", or tap' : 'Tap to speak');
        // Hand the mic straight back so a conversation can continue without
        // reaching for the phone between turns.
        if (mode === 'voice') startWakeWord();
      },
    });
    return;
  }
  $('voice-stop').classList.remove('hidden');
  speaker.speak(text, { onEnd: () => $('voice-stop').classList.add('hidden') });
}

function makeListener({ intoComposer }) {
  return new Listener({
    onStart: () => {
      $('voice-orb').classList.add('listening');
      $('voice-hint').textContent = 'Listening… tap to stop';
      $('mic-btn').classList.add('active');
    },
    onInterim: (text) => {
      if (intoComposer) {
        $('input').value = text;
      } else {
        $('voice-transcript').textContent = text;
      }
    },
    onFinal: (text) => {
      if (!text.trim()) return;
      if (intoComposer) {
        $('input').value = text;
        $('input').focus();
      } else {
        $('voice-transcript').textContent = '';
        send(text);
      }
    },
    onError: (message) => {
      $('voice-hint').textContent = message;
      setTimeout(() => { $('voice-hint').textContent = tapHint(); }, 4000);
    },
    onStop: () => {
      $('voice-orb').classList.remove('listening');
      $('mic-btn').classList.remove('active');
      $('voice-hint').textContent = tapHint();
    },
  });
}

function tapHint() {
  if (!voiceSupport.any) return 'Speech is not available in this browser.';
  return listener?.mode === 'whisper' ? 'Tap to record, tap again to send' : 'Tap to speak';
}

function startListening({ intoComposer = false } = {}) {
  // Barge-in: if Jarvis is mid-sentence, talking over it should cut it off.
  speaker.cancel();
  $('voice-stop').classList.add('hidden');
  listener = makeListener({ intoComposer });
  listener.start({ continuous: !intoComposer && !isMobile() });
}

function stopListening() {
  listener?.stop();
}

function toggleListening(options) {
  if (listener?.active) stopListening();
  else startListening(options);
}

/* ---------------- HUD voice turn ---------------- */

/** Draw the radial tick marks. Generated rather than hand-written so the count
 *  can change without editing 60 nearly-identical SVG lines. */
function buildTicks() {
  const group = $('hud-ticks');
  if (!group || group.childElementCount) return;
  const ns = 'http://www.w3.org/2000/svg';
  const count = 60;
  for (let i = 0; i < count; i += 1) {
    const angle = (i / count) * Math.PI * 2;
    const major = i % 5 === 0;
    const inner = major ? 66 : 70;
    const line = document.createElementNS(ns, 'line');
    line.setAttribute('x1', (100 + Math.cos(angle) * inner).toFixed(2));
    line.setAttribute('y1', (100 + Math.sin(angle) * inner).toFixed(2));
    line.setAttribute('x2', (100 + Math.cos(angle) * 76).toFixed(2));
    line.setAttribute('y2', (100 + Math.sin(angle) * 76).toFixed(2));
    line.setAttribute('opacity', major ? '0.85' : '0.35');
    group.appendChild(line);
  }
}

let wake = null;
let hudBusy = false;
const identityState = { enrolled: false, enforcing: false, ready: false };

async function refreshIdentity() {
  try {
    const status = await api('/api/identity/status');
    Object.assign(identityState, {
      enrolled: status.enrolled,
      enforcing: status.enforcing,
      ready: status.ready,
    });
    $('hud-enroll').textContent = status.enrolled
      ? `Re-enrol (${status.samples})`
      : 'Enrol voice';
    if (status.unacknowledged_alerts > 0) {
      hudAlert(
        `${status.unacknowledged_alerts} unrecognised voice attempt(s). ` +
        'Nothing was actioned.'
      );
      api('/api/identity/alerts/acknowledge', { method: 'POST' }).catch(() => {});
    }
  } catch { /* identity is optional; the HUD works without it */ }
}

function startWakeWord() {
  if (mode !== 'voice' || hudBusy) return;
  buildTicks();
  if (!wake) {
    wake = new WakeListener(
      'jarvis',
      () => { runVoiceTurn(); },
      // Only surface a mic problem once they've actually tried to talk. Firing
      // it the instant voice mode opens blames the user for a permission they
      // were never asked for yet.
      (message) => { if (hudBusy) hudAlert(message); },
    );
  }
  if (!wake.available) {
    setHudState('idle', 'Tap to speak');
    return;
  }
  wake.start();
}

function stopWakeWord() {
  wake?.stop();
}

async function runVoiceTurn() {
  if (hudBusy) return;
  hudBusy = true;
  stopWakeWord();
  jarvis.cancel();

  try {
    setHudState('listening', 'Listening');
    const { wav, spoke } = await captureUtterance();

    if (!spoke || !wav) {
      setHudState('idle', 'Didn\'t catch that');
      return;
    }

    setHudState('thinking', 'Working');
    const form = new FormData();
    form.append('audio', wav, 'speech.wav');
    const language = (navigator.language || '').slice(0, 2);
    if (language) form.append('language', language);

    const res = await fetch('/api/voice/transcribe', {
      method: 'POST',
      headers: { Authorization: `Bearer ${store.token}` },
      body: form,
    });

    if (res.status === 403) {
      // Voice didn't match the enrolled owner. The server has already refused
      // the request and raised the alert; the HUD just has to show it.
      const body = await res.json().catch(() => ({}));
      setHudState('denied', 'Voice not recognised');
      hudAlert(body.detail || 'That voice does not match. Nothing was actioned.');
      setTimeout(() => setHudState('idle', 'Say "Jarvis", or tap'), 3200);
      return;
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Transcription failed (${res.status})`);
    }

    const data = await res.json();
    if (data.ignored || !data.text) {
      setHudState('idle', data.reason || 'Nothing to do');
      return;
    }

    setHudState('thinking', 'Working');
    await send(data.text);   // send() drives speakReply() on completion
  } catch (err) {
    setHudState('idle', 'Something went wrong');
    hudAlert(err.message);
  } finally {
    hudBusy = false;
    if (mode === 'voice' && !jarvis.speaking) startWakeWord();
  }
}

/* ---------------- voice enrolment ---------------- */

const ENROL_PHRASES = [
  'The quick brown fox jumps over the lazy dog.',
  'Jarvis, what is on my calendar today?',
  'I would like to book a haircut this afternoon.',
];

async function enrolVoice() {
  stopWakeWord();
  hudBusy = true;

  showModal('Enrol your voice', `
    <p class="muted">Read each line aloud, normally, at your usual distance from
    the phone. Three separate recordings average out posture and background so
    the match is stable.</p>
    <p class="muted"><strong>This is a filter, not a lock.</strong> It stops other
    people in the room being answered and tells you when someone tried. A
    recording of your voice will pass it — your password is what actually
    protects this server.</p>
    <div id="enrol-body"></div>
  `);

  const body = () => $('enrol-body');

  try {
    for (let i = 0; i < ENROL_PHRASES.length; i += 1) {
      body().innerHTML = `
        <div class="card">
          <h4>Sample ${i + 1} of ${ENROL_PHRASES.length}</h4>
          <p style="font-size:17px">“${esc(ENROL_PHRASES[i])}”</p>
          <button id="enrol-go">Record</button>
        </div>`;

      await new Promise((resolve) => { $('enrol-go').onclick = resolve; });

      body().innerHTML = '<div class="card"><h4>Recording…</h4><p class="muted">Speak now.</p></div>';
      const { wav, spoke } = await captureUtterance();
      if (!spoke || !wav) {
        body().innerHTML = '<p class="error">Didn\'t hear anything. Close and try again.</p>';
        return;
      }

      const form = new FormData();
      form.append('audio', wav, 'enrol.wav');
      if (i === 0) form.append('reset', 'true');   // fresh profile on first sample

      const res = await fetch('/api/identity/enroll', {
        method: 'POST',
        headers: { Authorization: `Bearer ${store.token}` },
        body: form,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        body().innerHTML = `<p class="error">${esc(data.detail || 'Enrolment failed.')}</p>`;
        return;
      }

      if (data.ready) {
        body().innerHTML = `
          <div class="card">
            <h4>Done</h4>
            <p>Voice enrolled from ${data.samples} samples (consistency
            ${Math.round(data.cohesion * 100)}%).</p>
            ${data.warning ? `<p class="error">${esc(data.warning)}</p>` : ''}
            <p class="muted">To act on this, set <code>REQUIRE_VOICE_MATCH=true</code>
            in <code>.env</code> and restart. Until then it records matches without
            refusing anything.</p>
          </div>`;
      }
    }
    await refreshIdentity();
  } catch (err) {
    body().innerHTML = `<p class="error">${esc(err.message)}</p>`;
  } finally {
    hudBusy = false;
    if (mode === 'voice') startWakeWord();
  }
}

/* ---------------- skills ---------------- */

async function showSkills() {
  closeDrawer();
  showModal('Skills', '<p class="muted">Loading…</p>');
  try {
    const { skills } = await api('/api/skills');
    renderSkills(skills);
  } catch (err) {
    $('modal-body').innerHTML = `<p class="error">${esc(err.message)}</p>`;
  }
}

/* ---------------- browser session ----------------
 * Pasting the export straight into the app is the whole point of this screen.
 * The alternative is getting a JSON blob onto the server first, which from a
 * phone means an SSH client and a very long paste into a terminal. */

async function showBrowserSession() {
  closeDrawer();
  showModal('Browser session', '<p class="muted">Checking…</p>');
  let current = { present: false };
  try {
    current = await api('/api/browser/session');
  } catch (err) {
    $('modal-body').innerHTML = `<p class="error">${esc(err.message)}</p>`;
    return;
  }
  renderBrowserSession(current);
}

function renderBrowserSession(state) {
  const days = state.expires
    ? Math.round((state.expires * 1000 - Date.now()) / 86400000)
    : null;

  const summary = state.present
    ? `<div class="card">
         <div class="row"><div class="r-main"><div class="r-title">Signed in to</div>
           <div class="r-sub">${esc((state.domains || []).join(', '))}</div></div>
           <div class="r-val up">${state.cookies} cookies</div></div>
         ${days === null ? '' : `<div class="row"><div class="r-main">
           <div class="r-title">Expires</div></div>
           <div class="r-val ${days <= 0 ? 'down' : ''}">${
             days <= 0 ? 'expired' : `in ${days} days`}</div></div>`}
       </div>`
    : '<p class="muted">No session stored. Subscriber-only articles will show '
      + 'only a teaser until you import one.</p>';

  $('modal-body').innerHTML = `
    ${summary}
    <div class="card">
      <h4>Import a session</h4>
      <p class="r-sub">On the device where you're signed in: open the site, use a
      cookie-export extension, choose <b>Export as JSON</b>, then paste it here.
      Your password is never involved.</p>
      <textarea id="ck-text" rows="6" placeholder='Paste the JSON here — starts with [{"name":…'></textarea>
      <button id="ck-import">Import</button>
      ${state.present ? '<button id="ck-clear" class="hud-btn danger">Remove stored session</button>' : ''}
      <p id="ck-result"></p>
    </div>`;

  $('ck-import').onclick = async () => {
    const text = $('ck-text').value.trim();
    const result = $('ck-result');
    if (!text) { result.className = 'error'; result.textContent = 'Nothing pasted yet.'; return; }

    result.className = 'muted';
    result.textContent = 'Importing…';
    try {
      // Sent as form data rather than JSON: the export is itself JSON, and
      // wrapping JSON in JSON is one escaping mistake away from a confusing
      // parse error on a paste that was perfectly fine.
      const body = new FormData();
      body.append('cookies', text);
      const res = await fetch(`${API}/api/browser/session`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${store.token}` },
        body,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Import failed (${res.status})`);
      renderBrowserSession(data);
      $('ck-result').className = 'muted';
      $('ck-result').textContent = `Imported ${data.cookies} cookies for ${
        (data.domains || []).join(', ')}.`;
    } catch (err) {
      result.className = 'error';
      result.textContent = err.message;
    }
  };

  const clear = $('ck-clear');
  if (clear) {
    clear.onclick = async () => {
      await api('/api/browser/session', { method: 'DELETE' });
      renderBrowserSession({ present: false });
    };
  }
}

function renderSkills(skills) {
  const rows = skills.map((s) => `
    <div class="row">
      <div class="r-main">
        <div class="r-title">${esc(s.name)} ${s.builtin ? '<span class="pill">built-in</span>' : ''}</div>
        <div class="r-sub">${esc(s.triggers.join(', ') || 'no triggers')}</div>
        <div class="r-sub">used ${s.uses}x</div>
      </div>
      <div class="r-val">
        <button class="hud-btn" data-edit="${s.id}">edit</button>
      </div>
    </div>`).join('');

  $('modal-body').innerHTML = `
    <p class="muted">A skill is a standing instruction. Say one of its trigger
    phrases and Jarvis follows that procedure instead of improvising.</p>
    <div class="card">${rows || '<div class="r-sub">No skills yet.</div>'}</div>
    <button id="skill-new">New skill</button>
    <button id="skill-restore" class="hud-btn">Restore built-ins</button>
    <div id="skill-edit"></div>`;

  $('skill-new').onclick = () => editSkill(null);
  $('skill-restore').onclick = async () => {
    const { skills: fresh } = await api('/api/skills/restore-builtins', { method: 'POST' });
    renderSkills(fresh);
  };
  document.querySelectorAll('[data-edit]').forEach((btn) => {
    btn.onclick = () => editSkill(skills.find((s) => s.id === Number(btn.dataset.edit)));
  });
}

function editSkill(skill) {
  const box = $('skill-edit');
  box.innerHTML = `
    <div class="card">
      <h4>${skill ? 'Edit' : 'New'} skill</h4>
      <input id="sk-name" placeholder="Name" value="${esc(skill?.name || '')}">
      <input id="sk-trig" placeholder="Trigger phrases, comma separated"
             value="${esc(skill?.triggers?.join(', ') || '')}">
      <textarea id="sk-inst" placeholder="What should Jarvis do? Be specific — this replaces guesswork.">${esc(skill?.instruction || '')}</textarea>
      <button id="sk-save">Save</button>
      ${skill ? '<button id="sk-del" class="hud-btn danger">Delete</button>' : ''}
      <div id="sk-msg"></div>
    </div>`;

  $('sk-save').onclick = async () => {
    const body = JSON.stringify({
      name: $('sk-name').value.trim(),
      triggers: $('sk-trig').value.trim(),
      instruction: $('sk-inst').value.trim(),
      enabled: true,
    });
    try {
      await api(skill ? `/api/skills/${skill.id}` : '/api/skills',
                { method: skill ? 'PUT' : 'POST', body });
      showSkills();
    } catch (err) {
      $('sk-msg').innerHTML = `<p class="error">${esc(err.message)}</p>`;
    }
  };
  if (skill) {
    $('sk-del').onclick = async () => {
      await api(`/api/skills/${skill.id}`, { method: 'DELETE' });
      showSkills();
    };
  }
}

/* ---------------- voice picker ---------------- */

function showVoicePicker() {
  closeDrawer();
  const voices = ('speechSynthesis' in window) ? window.speechSynthesis.getVoices() : [];
  if (!voices.length) {
    showModal('Voice', '<p class="muted">No voices available in this browser yet. '
      + 'Close this, wait a moment, and try again — some browsers load them late.</p>');
    return;
  }

  const saved = localStorage.getItem('jarvis_voice') || '';
  const best = pickJarvisVoice(voices);
  // English first: the rest are rarely what anyone wants, but keep them
  // reachable rather than deciding for the user.
  const sorted = [...voices].sort((a, b) =>
    (b.lang.startsWith('en') - a.lang.startsWith('en')) || a.name.localeCompare(b.name));

  const options = sorted.map((v) => `
    <div class="row">
      <div class="r-main">
        <div class="r-title">${esc(v.name)} ${v.name === best?.name ? '<span class="pill">closest to JARVIS</span>' : ''}</div>
        <div class="r-sub">${esc(v.lang)}</div>
      </div>
      <div class="r-val">
        <button class="hud-btn" data-try="${esc(v.name)}">try</button>
        <button class="hud-btn" data-use="${esc(v.name)}">${saved === v.name ? '✓ using' : 'use'}</button>
      </div>
    </div>`).join('');

  showModal('Voice', `
    <p class="muted">JARVIS is British, male and unhurried. On Apple devices
    <strong>Daniel</strong> is the closest match. This is not the voice from the
    films — nothing free is — but a premium British voice installed via
    Settings → Accessibility → Spoken Content gets noticeably closer.</p>
    <div class="card">${options}</div>`);

  document.querySelectorAll('[data-try]').forEach((b) => {
    b.onclick = () => {
      const v = voices.find((x) => x.name === b.dataset.try);
      jarvis.voice = v;
      jarvis.speak('Good evening, sir. All systems are functioning within normal parameters.');
    };
  });
  document.querySelectorAll('[data-use]').forEach((b) => {
    b.onclick = () => {
      localStorage.setItem('jarvis_voice', b.dataset.use);
      jarvis.voice = voices.find((x) => x.name === b.dataset.use);
      showVoicePicker();
    };
  });
}

/* ---------------- drawer ---------------- */

const openDrawer = () => { $('drawer').classList.add('open'); $('scrim').classList.add('on'); };
const closeDrawer = () => { $('drawer').classList.remove('open'); $('scrim').classList.remove('on'); };

/* ---------------- wiring ---------------- */

$('login-btn').onclick = signIn;
$('password').onkeydown = (e) => { if (e.key === 'Enter') signIn(); };
$('label').onkeydown = (e) => { if (e.key === 'Enter') signIn(); };

$('mode-btn').onclick = () => setMode(mode === 'voice' ? 'text' : 'voice');
$('hud-stage').onclick = () => { if (jarvis.speaking) { jarvis.cancel(); } runVoiceTurn(); };
$('hud-stage').onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); runVoiceTurn(); } };
$('hud-enroll').onclick = enrolVoice;
$('hud-exit').onclick = () => setMode('text');
$('voice-orb').onclick = () => toggleListening();
$('voice-to-text').onclick = () => setMode('text');
$('voice-stop').onclick = () => { speaker.cancel(); $('voice-stop').classList.add('hidden'); };
$('mic-btn').onclick = () => toggleListening({ intoComposer: true });

$('menu-btn').onclick = openDrawer;
$('drawer-close').onclick = closeDrawer;
$('scrim').onclick = closeDrawer;
$('new-btn').onclick = newConversation;
$('loc-btn').onclick = () => requestLocation({ userInitiated: true });
$('logout-btn').onclick = signOut;
$('status-btn').onclick = showStatus;
$('auto-btn').onclick = showAutonomy;
$('skills-btn').onclick = showSkills;
$('cookies-btn').onclick = showBrowserSession;
$('voice-btn').onclick = showVoicePicker;
$('hud-btn').onclick = () => { closeDrawer(); setMode('voice'); };
initGalaxy({
  api,
  // The galaxy speaks its answers but never the note it opens: the note is on
  // screen to be read. jarvis.speak already respects the voice on/off setting
  // and the chosen voice, so routing through it keeps one place in charge.
  speak: (text) => { if (mode === 'voice' || jarvis.enabled) jarvis.speak(text); },
});
$('galaxy-btn').onclick = () => { closeDrawer(); openGalaxy(); };
$('hud-galaxy').onclick = () => openGalaxy();
$('modal-close').onclick = () => $('modal').classList.add('hidden');
$('modal').onclick = (e) => { if (e.target === $('modal')) $('modal').classList.add('hidden'); };

$('composer').onsubmit = (e) => { e.preventDefault(); send($('input').value); };
$('input').addEventListener('input', (e) => {
  e.target.style.height = 'auto';
  e.target.style.height = `${Math.min(e.target.scrollHeight, 160)}px`;
});
$('input').addEventListener('keydown', (e) => {
  // Enter sends on a physical keyboard; on touch we leave Enter as newline.
  if (e.key === 'Enter' && !e.shiftKey && !/Mobi|Android|iPhone|iPad/.test(navigator.userAgent)) {
    e.preventDefault();
    send($('input').value);
  }
});
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('chip')) send(e.target.textContent);
});

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// Tells the startup watchdog in index.html that the module ran.

/* ---------------- Safari audio unlock + first-run gate ---------------- */

/* Safari will not run speechSynthesis.speak() unless the call chain traces back
 * to a user gesture. Every answer here arrives from an async fetch, so by the
 * time there is something to say the gesture is gone and Safari stays silent —
 * no error, no console warning, nothing. The fix is to prime the engine during
 * the very first interaction of any kind, whatever that interaction was for.
 *
 * Capture phase and { once: true }: capture so it runs before any handler that
 * might stopPropagation, once because unlockSpeech is idempotent and there is
 * no reason to keep a listener alive for the rest of the session. */
/** Voice needs both an API and a secure context; either missing means dead controls. */
function voiceUsable() {
  return voiceSupport.any && voiceSupport.secure;
}

function armAudioUnlock() {
  const prime = () => unlockSpeech();
  for (const evt of ['pointerdown', 'touchstart', 'click', 'keydown']) {
    window.addEventListener(evt, prime, { capture: true, once: true, passive: true });
  }
}

/* Requirement 8. Served over plain HTTP from a remote host, getUserMedia and
 * speechSynthesis both fail silently in Safari and Chrome alike. Say why. */
function checkSecureContext() {
  if (voiceSupport.secure) return true;
  const bar = $('insecure');
  bar.classList.remove('hidden');
  bar.innerHTML = '<b>Voice is unavailable over plain HTTP.</b> '
    + 'Microphones and speech both require a secure context. '
    + 'Reach this page over <b>https://</b>, or tunnel it to localhost with '
    + '<code>ssh -N -L 8000:127.0.0.1:8000 jarvis</code>.';
  return false;
}

/* Requirement 9. A mic button that cannot work is worse than no mic button —
 * it looks broken rather than absent. */
function hideDeadVoiceControls() {
  if (voiceUsable()) return;
  // setMode first: applyMode owns the mic button's visibility, so hiding it
  // here and then switching modes would simply un-hide it again.
  setMode('text');
  for (const id of ['voice-orb', 'mode-btn']) $(id)?.classList.add('hidden');
}

/* Requirement 2. The greeting cannot play on load, because nothing has been
 * tapped yet. Ask for that tap once per session, with the app visible behind
 * it, then speak. */
/**
 * The boot greeting, timed by *this device's* clock.
 *
 * Deliberately not the server's: the VM sits in whatever region Oracle put it
 * in, and would cheerfully wish you good morning at midnight. `new Date()` here
 * is the phone in your hand.
 */
function greetingPrefix() {
  const hour = new Date().getHours();
  if (hour < 12) return 'Good morning, sir.';
  if (hour < 18) return 'Good afternoon, sir.';
  return 'Good evening, sir.';
}

async function bootGreeting() {
  const opening = greetingPrefix();
  try {
    const { notes, configured } = await api('/api/notes/summary');
    if (!configured || !notes) return opening;
    return `${opening} ${notes} note${notes === 1 ? '' : 's'} indexed, `
      + 'all present and accounted for.';
  } catch {
    // No vault, or the call failed. The greeting still works; it just has
    // nothing to count.
    return opening;
  }
}

function showWakeGate() {
  const gate = $('wake-gate');
  if (!voiceSupport.synthesis || !voiceSupport.secure) return;
  if (sessionStorage.getItem('jarvis_woke')) return;

  const line = $('wake-greeting');
  // Shown immediately with the time-based half, then completed when the count
  // arrives. Whatever is on screen when you tap is what gets spoken, so a fast
  // tap is never left speaking something different from what it displayed.
  line.textContent = greetingPrefix();
  gate.classList.remove('hidden');
  bootGreeting().then((full) => { line.textContent = full; });

  const wake = () => {
    sessionStorage.setItem('jarvis_woke', '1');
    // Order matters: unlock inside the gesture, and only then speak. Speaking
    // first is the mistake that leaves Safari mute for the whole session.
    unlockSpeech();
    gate.classList.add('hidden');
    jarvis.speak(line.textContent);
  };
  gate.addEventListener('click', wake, { once: true });
  gate.addEventListener('touchend', wake, { once: true });
}

armAudioUnlock();

window.__jarvisStarted = true;

if (store.token) enterApp();
