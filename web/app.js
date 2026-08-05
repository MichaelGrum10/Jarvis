/* Jarvis PWA client.
 *
 * Three jobs beyond chat:
 *  - hand the server this device's real coordinates, since the server's own IP is
 *    a datacentre and says nothing about where you are;
 *  - poll for queued device commands and execute them locally (open app / URL /
 *    Shortcut), which is the only way a remote server can act on your phone;
 *  - render tool results as cards instead of walls of JSON.
 */

import { Listener, Speaker, voiceSupport, defaultMode, saveMode, isMobile } from '/static/voice.js';

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
  $('login').classList.add('hidden');
  $('app').classList.remove('hidden');
  $('status-dot').classList.add('on');
  if (!voiceSupport.any && mode === 'voice') mode = 'text';
  applyMode();
  $('voice-hint').textContent = tapHint();
  loadConversations();
  requestLocation(true);
  commandTimer = setInterval(pollCommands, 4000);
  pollCommands();
}

/* ---------------- location ---------------- */

function requestLocation(quiet = false) {
  if (!navigator.geolocation) {
    if (!quiet) alert('This browser has no location support.');
    return;
  }
  navigator.geolocation.getCurrentPosition(
    async (pos) => {
      location_ = {
        lat: pos.coords.latitude,
        lon: pos.coords.longitude,
        accuracy_m: pos.coords.accuracy,
      };
      $('loc-btn').classList.add('active');
      try { await api('/api/device/location', { method: 'POST', body: JSON.stringify(location_) }); } catch {}
    },
    (err) => { if (!quiet) alert(`Location unavailable: ${err.message}`); },
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 120000 }
  );
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

function applyMode() {
  const voice = mode === 'voice';
  $('voice-panel').classList.toggle('hidden', !voice);
  $('composer').classList.toggle('hidden', voice);
  $('mode-btn').textContent = voice ? '⌨' : '🎙';
  $('mode-btn').title = voice ? 'Switch to typing' : 'Switch to voice';
  // Dictation is available in text mode too — it just doesn't take over the screen.
  $('mic-btn').classList.toggle('hidden', !voiceSupport.any);
  if (!voice) { stopListening(); speaker.cancel(); }
}

function setMode(next) {
  mode = next;
  saveMode(next);
  applyMode();
}

function speakReply(text) {
  $('voice-stop').classList.remove('hidden');
  speaker.speak(text, {
    onEnd: () => $('voice-stop').classList.add('hidden'),
  });
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

/* ---------------- drawer ---------------- */

const openDrawer = () => { $('drawer').classList.add('open'); $('scrim').classList.add('on'); };
const closeDrawer = () => { $('drawer').classList.remove('open'); $('scrim').classList.remove('on'); };

/* ---------------- wiring ---------------- */

$('login-btn').onclick = signIn;
$('password').onkeydown = (e) => { if (e.key === 'Enter') signIn(); };
$('label').onkeydown = (e) => { if (e.key === 'Enter') signIn(); };

$('mode-btn').onclick = () => setMode(mode === 'voice' ? 'text' : 'voice');
$('voice-orb').onclick = () => toggleListening();
$('voice-to-text').onclick = () => setMode('text');
$('voice-stop').onclick = () => { speaker.cancel(); $('voice-stop').classList.add('hidden'); };
$('mic-btn').onclick = () => toggleListening({ intoComposer: true });

$('menu-btn').onclick = openDrawer;
$('drawer-close').onclick = closeDrawer;
$('scrim').onclick = closeDrawer;
$('new-btn').onclick = newConversation;
$('loc-btn').onclick = () => requestLocation(false);
$('logout-btn').onclick = signOut;
$('status-btn').onclick = showStatus;
$('auto-btn').onclick = showAutonomy;
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

if (store.token) enterApp();
