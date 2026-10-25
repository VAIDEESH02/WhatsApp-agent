'use strict';
// Reply Desk: polls the console API every 2 s and updates the page in place, keyed by record,
// so open traces stay open and drafts being edited are never overwritten. All message text is
// inserted with textContent (never parsed as HTML).

const POLL_MS = 2000;
const WRITE_HEADERS = { 'Content-Type': 'application/json', 'X-Reply-Desk': '1' };
const THEME_KEY = 'reply-desk-theme';
const CONTROLS_KEY = 'reply-desk-controls'; // "open" | "closed", remembered per browser
const WIDE = window.matchMedia('(min-width: 981px)');

const REL_LABEL = {
  family: 'family', friends: 'friends', professional: 'professional', girlfriend: 'girlfriend',
  business_freelance: 'freelance', unknown: 'unknown', group: 'group', broadcast: 'status', newsletter: 'channel',
};
const REL_CLASS = new Set(['family', 'friends', 'professional', 'girlfriend', 'business_freelance']);
const DECISION = {
  reply: ['Auto-replied', 'state-ok'],
  needs_approval: ['Waiting for you', 'state-hold'],
  ignore: ['Ignored', 'state-idle'],
  error: ['Error', 'state-stop'],
};
const APPROVAL = {
  pending: ['Waiting for you', 'state-hold'],
  approved: ['Queued to send', 'state-ok'],
  sent: ['Sent', 'state-ok'],
  dry_run: ['Logged (dry run)', 'state-hold'],
  dismissed: ['Dismissed', 'state-idle'],
};

const $ = (id) => document.getElementById(id);
const state = {
  firstLoad: true, lastOk: 0, autoEnabled: false, mode: 'unknown',
  feedNodes: new Map(), openTraces: new Set(), tickets: new Map(), busy: new Set(),
  delaysDirty: false,
};

// ---------- helpers ----------
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function relChip(relationship) {
  const rel = relationship || 'unknown';
  return el('span', `chip rel-${REL_CLASS.has(rel) ? rel : 'other'}`, REL_LABEL[rel] || rel);
}

function stateChip([label, cls]) {
  return el('span', `chip ${cls}`, label);
}

function clock(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const sameDay = date.toDateString() === new Date().toDateString();
  return date.toLocaleString([], sameDay
    ? { hour: '2-digit', minute: '2-digit', second: '2-digit' }
    : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function toast(message, isError = false) {
  const node = el('div', `toast${isError ? ' is-error' : ''}`, message);
  $('toasts').append(node);
  setTimeout(() => node.remove(), isError ? 7000 : 4500);
}

async function api(path, body) {
  const response = await fetch(path, body === undefined
    ? { cache: 'no-store' }
    : { method: 'POST', headers: WRITE_HEADERS, body: JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

function whoRow(name, relationship, iso) {
  const row = el('div', 'who');
  row.append(el('span', 'who__name', name || 'Unknown'), relChip(relationship));
  const time = el('time', 'time', clock(iso));
  if (iso) time.dateTime = iso;
  row.append(time);
  return row;
}

// ---------- header: live indicator, switches, mode ----------
function setLive(ok) {
  const live = $('live');
  live.classList.toggle('is-ok', ok);
  live.classList.toggle('is-down', !ok);
  if (ok) {
    live.classList.remove('pulse');
    void live.offsetWidth; // restart the pulse animation
    live.classList.add('pulse');
  }
  tickLive();
}

function tickLive() {
  const text = $('live-text');
  if (!state.lastOk) { text.textContent = 'Connecting…'; return; }
  const seconds = Math.round((Date.now() - state.lastOk) / 1000);
  if ($('live').classList.contains('is-down')) {
    text.textContent = `Offline · retrying (last update ${seconds}s ago)`;
  } else {
    text.textContent = seconds <= 1 ? 'Connected · updated just now' : `Connected · updated ${seconds}s ago`;
  }
}

function renderHeader(data) {
  state.autoEnabled = data.auto_reply_enabled === true;
  const sw = $('auto-switch');
  sw.disabled = false;
  sw.setAttribute('aria-checked', String(state.autoEnabled));
  $('auto-state').textContent = state.autoEnabled ? 'On' : 'Off';
  $('auto-banner').hidden = !state.autoEnabled;
  const autoPill = $('auto-pill');
  autoPill.textContent = state.autoEnabled ? 'Auto-reply on' : 'Auto-reply off';
  autoPill.classList.toggle('pill--on', state.autoEnabled);

  state.mode = (data.mode || 'unknown').toUpperCase();
  if (data.controls) renderControls(data.controls);
}

// ---------- client controls: sending mode, reply timing, kill switch ----------
function renderControls(controls) {
  const live = controls.dry_run === false;
  const liveSwitch = $('live-switch');
  liveSwitch.disabled = false;
  liveSwitch.setAttribute('aria-checked', String(live));
  $('live-state').textContent = live ? 'Live' : 'Dry run';
  const modePill = $('mode-pill');
  modePill.textContent = live ? 'Live sending' : 'Dry run';
  modePill.classList.toggle('pill--live', live);
  modePill.classList.toggle('pill--dry', !live);

  const killOn = controls.kill_switch_active === true;
  $('kill-banner').hidden = !killOn;
  $('kill-switch').disabled = killOn;
  $('kill-switch').textContent = killOn ? '🛑 Kill switch on' : '🛑 Kill switch';

  const editing = [$('min-delay'), $('max-delay')].includes(document.activeElement);
  if (!state.delaysDirty && !editing) {
    $('min-delay').value = controls.min_delay_seconds;
    $('max-delay').value = controls.max_delay_seconds;
  }
}

async function setDryRun(dryRun) {
  const liveSwitch = $('live-switch');
  liveSwitch.disabled = true;
  $('live-confirm').hidden = true;
  try {
    renderControls(await api('/api/settings', { dry_run: dryRun }));
    state.mode = dryRun ? 'DRY_RUN' : 'LIVE';
    toast(dryRun ? 'Dry run on. The client only logs what it would send.' : 'Live sending on. The client now sends real messages.');
  } catch (error) {
    toast(`Couldn't change sending mode: ${error.message}`, true);
  } finally {
    liveSwitch.disabled = false;
  }
}

async function setKillSwitch(active) {
  try {
    renderControls(await api('/api/kill-switch', { active }));
    toast(active ? 'Kill switch on. The WhatsApp client is stopped.' : 'Kill switch cleared. The client is running again.');
  } catch (error) {
    toast(`Couldn't ${active ? 'turn on' : 'clear'} the kill switch: ${error.message}`, true);
  }
}

// The Controls panel: open by default on wide screens, closed on phones, then as you leave it.
function initControlsPanel() {
  const toggle = $('controls-toggle');
  const panel = $('controls-panel');
  const show = (open) => {
    panel.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  };
  let saved = null;
  try { saved = localStorage.getItem(CONTROLS_KEY); } catch (error) { /* storage unavailable */ }
  show(saved ? saved === 'open' : WIDE.matches);
  toggle.addEventListener('click', () => {
    const open = panel.hidden;
    show(open);
    try { localStorage.setItem(CONTROLS_KEY, open ? 'open' : 'closed'); } catch (error) { /* still toggles */ }
  });
}

function initControls() {
  const liveSwitch = $('live-switch');
  const confirmBox = $('live-confirm');
  liveSwitch.addEventListener('click', () => {
    if (liveSwitch.getAttribute('aria-checked') === 'true') { setDryRun(true); return; }
    confirmBox.hidden = !confirmBox.hidden;
    if (!confirmBox.hidden) $('live-confirm-yes').focus();
  });
  $('live-confirm-yes').addEventListener('click', () => setDryRun(false));
  $('live-confirm-no').addEventListener('click', () => { confirmBox.hidden = true; liveSwitch.focus(); });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !confirmBox.hidden) { confirmBox.hidden = true; liveSwitch.focus(); }
  });
  document.addEventListener('click', (event) => {
    if (!confirmBox.hidden && !confirmBox.contains(event.target) && !liveSwitch.contains(event.target)) confirmBox.hidden = true;
  });

  $('kill-switch').addEventListener('click', () => setKillSwitch(true));
  $('clear-kill').addEventListener('click', () => setKillSwitch(false));

  for (const id of ['min-delay', 'max-delay']) {
    $(id).addEventListener('input', () => { state.delaysDirty = true; $('delay-error').textContent = ''; });
  }
  $('timing').addEventListener('submit', async (event) => {
    event.preventDefault();
    const low = Number($('min-delay').value);
    const high = Number($('max-delay').value);
    const error = $('delay-error');
    if (!Number.isFinite(low) || !Number.isFinite(high) || low <= 0 || high <= 0) {
      error.textContent = 'Both delays must be positive numbers of seconds.';
      return;
    }
    if (low >= high) {
      error.textContent = 'The minimum must be less than the maximum.';
      return;
    }
    try {
      const controls = await api('/api/settings', { min_delay_seconds: low, max_delay_seconds: high });
      state.delaysDirty = false;
      renderControls(controls);
      toast(`Reply timing saved: ${controls.min_delay_seconds}–${controls.max_delay_seconds} seconds.`);
    } catch (failure) {
      error.textContent = failure.message;
    }
  });
}

async function setAutoReply(enabled) {
  const sw = $('auto-switch');
  sw.disabled = true;
  $('auto-confirm').hidden = true;
  try {
    const result = await api('/api/auto-reply', { enabled });
    renderHeader({ auto_reply_enabled: result.auto_reply_enabled, mode: state.mode });
    toast(result.auto_reply_enabled
      ? 'Auto-replies turned on. Clearly safe messages are answered on their own.'
      : 'Auto-replies turned off. Every draft waits for you.');
  } catch (error) {
    toast(`Couldn't change auto-reply: ${error.message}`, true);
  } finally {
    sw.disabled = false;
  }
}

function initSwitches() {
  const sw = $('auto-switch');
  const confirmBox = $('auto-confirm');
  sw.addEventListener('click', () => {
    if (state.autoEnabled) { setAutoReply(false); return; }
    confirmBox.hidden = !confirmBox.hidden;
    if (!confirmBox.hidden) $('auto-confirm-yes').focus();
  });
  $('auto-confirm-yes').addEventListener('click', () => setAutoReply(true));
  $('auto-confirm-no').addEventListener('click', () => { confirmBox.hidden = true; sw.focus(); });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !confirmBox.hidden) { confirmBox.hidden = true; sw.focus(); }
  });
  document.addEventListener('click', (event) => {
    if (!confirmBox.hidden && !confirmBox.contains(event.target) && !sw.contains(event.target)) confirmBox.hidden = true;
  });

  // Dark mode: follows the system until switched; the choice is remembered on this browser.
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  const themeSwitch = $('theme-switch');
  const isDark = () => {
    const chosen = document.documentElement.dataset.theme;
    return chosen ? chosen === 'dark' : media.matches;
  };
  const syncTheme = () => themeSwitch.setAttribute('aria-checked', String(isDark()));
  themeSwitch.addEventListener('click', () => {
    const next = isDark() ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(THEME_KEY, next); } catch (error) { /* storage unavailable: still switches */ }
    syncTheme();
  });
  media.addEventListener('change', syncTheme);
  syncTheme();
}

// ---------- stats ----------
function renderStats(data) {
  $('stat-processed').textContent = data.processed ?? 0;
  $('stat-auto').textContent = data.replies_sent ?? 0;
  $('stat-auto-label').textContent = state.mode === 'LIVE' ? 'Auto-replied' : 'Auto-replied (dry run)';
  $('stat-waiting').textContent = data.pending_approvals ?? 0;
  $('stat-approved').textContent = data.approved_sent ?? 0;
}

// ---------- approval queue ----------
function buildTicket(row) {
  const ticket = el('article', 'ticket');
  ticket.dataset.id = row.id;
  ticket.append(whoRow(row.contact_name, row.relationship, row.created_at));
  ticket.append(el('p', 'bubble', row.incoming_text || '(no text)'));
  ticket.append(el('p', 'reason', `Held because: ${row.reason}`));

  const fieldId = `draft-${row.id}`;
  const label = el('label', null, 'Reply to send');
  label.htmlFor = fieldId;
  const field = el('textarea');
  field.id = fieldId;
  field.value = row.draft || '';
  field.rows = 2;

  const actions = el('div', 'ticket__actions');
  const approve = el('button', 'btn btn--primary', 'Approve and send');
  approve.type = 'button';
  const dismiss = el('button', 'btn', 'Dismiss');
  dismiss.type = 'button';
  approve.addEventListener('click', () => decide(row, 'approve', ticket, field));
  dismiss.addEventListener('click', () => decide(row, 'dismiss', ticket, field));
  actions.append(approve, dismiss);

  ticket.append(label, field, actions);
  return ticket;
}

async function decide(row, action, ticket, field) {
  const text = field.value.trim();
  if (action === 'approve' && !text) { toast('The reply is empty. Write something or dismiss it.', true); field.focus(); return; }
  state.busy.add(row.id);
  ticket.classList.add('is-busy');
  ticket.querySelectorAll('button, textarea').forEach((control) => { control.disabled = true; });
  try {
    await api(`/api/approvals/${row.id}/${action}`, action === 'approve' ? { text } : {});
    ticket.remove();
    state.tickets.delete(row.id);
    toast(action === 'approve'
      ? `Approved for ${row.contact_name}. ${state.mode === 'LIVE' ? 'Sending within a few seconds.' : 'The client is in dry run, so it is logged, not sent.'}`
      : `Dismissed the draft for ${row.contact_name}.`);
    poll();
  } catch (error) {
    ticket.classList.remove('is-busy');
    ticket.querySelectorAll('button, textarea').forEach((control) => { control.disabled = false; });
    toast(`Couldn't ${action} this draft: ${error.message}`, true);
  } finally {
    state.busy.delete(row.id);
  }
}

function renderQueue(rows) {
  const pending = rows.filter((row) => row.status === 'pending');
  const queue = $('queue');
  const keep = new Set(pending.map((row) => row.id));
  for (const [id, node] of state.tickets) {
    if (!keep.has(id) && !state.busy.has(id)) { node.remove(); state.tickets.delete(id); }
  }
  const ordered = pending.map((row) => {
    if (!state.tickets.has(row.id)) state.tickets.set(row.id, buildTicket(row));
    return state.tickets.get(row.id);
  });
  queue.replaceChildren(...(ordered.length
    ? ordered
    : [el('div', 'empty', 'Nothing waiting for you. Held drafts appear here with a reply you can edit.')]));

  const count = $('queue-count');
  count.textContent = pending.length;
  count.classList.toggle('has-items', pending.length > 0);

  const decided = rows.filter((row) => row.status !== 'pending').slice(0, 6);
  $('recent').replaceChildren(...(decided.length ? decided.map((row) => {
    const item = el('li');
    item.append(el('strong', null, row.contact_name), relChip(row.relationship), stateChip(APPROVAL[row.status] || [row.status, 'state-idle']));
    const time = el('time', 'time', clock(row.sent_at || row.decided_at || row.created_at));
    item.append(time, el('span', 'recent__text', row.final_text || row.draft || ''));
    return item;
  }) : [el('li', 'empty empty--small', 'No decisions yet.')]));
}

// ---------- live feed ----------
const feedKey = (record) => `${record.timestamp || ''}|${record.jid || ''}`;
const feedSignature = (record) => `${record.decision}|${record.approval_status || ''}`;

function buildEntry(record, key) {
  const entry = el('li', 'entry');
  entry.append(whoRow(record.contact_name, record.relationship, record.timestamp));

  // One chip: the message's current state. Once a held draft is decided, its approval status
  // replaces "Waiting for you".
  const states = el('div', 'who');
  states.style.marginTop = '8px';
  states.append(stateChip(record.approval_status
    ? APPROVAL[record.approval_status] || [record.approval_status, 'state-idle']
    : DECISION[record.decision] || [record.decision || '?', 'state-idle']));
  entry.append(states);

  if (record.text) entry.append(el('p', 'bubble', record.text));
  if (record.reason) entry.append(el('p', 'reason', record.reason));

  if (record.reply) {
    const reply = el('div', 'entry__reply');
    reply.append(
      el('span', 'entry__reply-label', record.decision === 'reply' ? 'Agent reply' : 'Draft'),
      el('span', 'entry__reply-text', record.reply),
    );
    entry.append(reply);
  }

  const trace = Array.isArray(record.retrieval_trace) ? record.retrieval_trace : [];
  if (trace.length) {
    const details = el('details', 'trace');
    details.open = state.openTraces.has(key);
    details.addEventListener('toggle', () => (details.open ? state.openTraces.add(key) : state.openTraces.delete(key)));
    details.append(el('summary', null, `Why this reply · ${trace.length} similar past messages`));
    for (const pair of trace) {
      const item = el('div', 'trace__item');
      const them = el('div', 'trace__line');
      them.append(el('b', null, 'They: '), document.createTextNode(pair.their_message || ''));
      const you = el('div', 'trace__line');
      you.append(el('b', null, 'You: '), document.createTextNode(pair.my_reply || ''));
      item.append(them, you);
      if (typeof pair.distance === 'number') {
        const similarity = Math.max(0, Math.min(1, 1 - pair.distance));
        const match = el('div', 'match');
        const bar = el('span', 'match__bar');
        const fill = el('span', 'match__fill');
        fill.style.width = `${Math.round(similarity * 100)}%`;
        bar.append(fill);
        match.append(bar, el('span', null, `match ${similarity.toFixed(2)} · distance ${pair.distance.toFixed(3)}`));
        item.append(match);
      }
      details.append(item);
    }
    entry.append(details);
  }
  entry.dataset.sig = feedSignature(record);
  return entry;
}

function renderFeed(data) {
  const feed = $('feed');
  if (data.waiting || !data.records.length) {
    state.feedNodes.clear();
    const empty = el('li', 'empty', 'waiting for first message...');
    empty.append(el('br'), el('span', 'muted', 'Messages appear here as soon as the WhatsApp client passes them to the agent.'));
    feed.replaceChildren(empty);
    return;
  }
  const seen = new Set();
  const nodes = data.records.map((record) => {
    const key = feedKey(record);
    seen.add(key);
    let node = state.feedNodes.get(key);
    if (!node || node.dataset.sig !== feedSignature(record)) {
      const fresh = !node && !state.firstLoad;
      node = buildEntry(record, key);
      if (fresh) node.classList.add('is-fresh');
      state.feedNodes.set(key, node);
    }
    return node;
  });
  for (const key of state.feedNodes.keys()) if (!seen.has(key)) state.feedNodes.delete(key);
  feed.replaceChildren(...nodes);
}

// ---------- polling ----------
let pollTimer = null;
async function poll() {
  clearTimeout(pollTimer);
  try {
    const [feed, rows] = await Promise.all([api('/api/feed'), api('/api/approvals?limit=40')]);
    renderHeader(feed);
    renderStats(feed);
    renderQueue(rows);
    renderFeed(feed);
    state.firstLoad = false;
    state.lastOk = Date.now();
    setLive(true);
  } catch (error) {
    setLive(false);
  } finally {
    pollTimer = setTimeout(poll, POLL_MS);
  }
}

initControlsPanel();
initSwitches();
initControls();
setInterval(tickLive, 1000);
poll();
