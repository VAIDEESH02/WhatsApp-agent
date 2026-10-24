'use strict';
/**
 * WhatsApp <-> Python agent client (Baileys, CommonJS).
 *
 * Receives real-time messages, asks the local FastAPI bridge (agent/bridge.py) what to do,
 * and replies only when the bridge says should_reply. Every few seconds it also sends replies
 * Vaideesh approved in the console (GET /outbox).
 *
 * Read fresh for every message (and every outbox check), so changes apply without a restart:
 *   config/settings.json       dry_run and the min/max pre-send delay (safe defaults if unreadable)
 *   kill_switch.flag           if this file exists in the repo root, nothing is processed or sent
 *   config/relationship_map.json   independent allowlist check right before every send
 *
 * Each 1:1 chat's last few messages (theirs, yours from the phone, and the bot's own sends) are
 * kept in memory only and passed to the bridge as recent_messages, so drafts see the conversation.
 *
 * Run from the project root, with the bridge already running:  node whatsapp/baileys_client.js
 */

const fs = require('fs');
const path = require('path');
const axios = require('axios');
const pino = require('pino');
const qrcode = require('qrcode-terminal');
const baileys = require('@whiskeysockets/baileys');

const makeWASocket = baileys.default ?? baileys.makeWASocket;
const { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = baileys;

const PROJECT_ROOT = path.resolve(__dirname, '..');
const SETTINGS_PATH = path.join(PROJECT_ROOT, 'config', 'settings.json');
const KILL_SWITCH_PATH = path.join(PROJECT_ROOT, 'kill_switch.flag');
const RELATIONSHIP_MAP_PATH = path.join(PROJECT_ROOT, 'config', 'relationship_map.json');
// ./auth_info_baileys at the project root. Holds the WhatsApp session: never commit it.
const AUTH_DIR = path.join(PROJECT_ROOT, 'auth_info_baileys');

// 127.0.0.1, not "localhost": the bridge listens on IPv4 only, and Node may resolve localhost to ::1.
const BRIDGE_BASE = 'http://127.0.0.1:5001';
const BRIDGE_URL = `${BRIDGE_BASE}/process`;
const OUTBOX_POLL_MS = 3000;
const BRIDGE_TIMEOUT_MS = 120000;

const DEFAULT_SETTINGS = Object.freeze({ dry_run: true, min_delay_seconds: 3, max_delay_seconds: 12 });
// Relationships that never pass the allowlist: not a contact, or not classified yet.
const NOT_ALLOWED = new Set(['unknown', 'REPLACE_ME']);
// Same JID rules as ingestion/whatsapp_ids.py (used by agent/router.py).
const JID_PATTERN = /^([^@:]*)(?::(\d+))?@([\w.]+)$/;
const PERSON_SERVERS = new Set(['s.whatsapp.net', 'c.us']);
const E164_DIGITS = /^[1-9]\d{7,14}$/;

const MEDIA_TYPES = { imageMessage: 'image', videoMessage: 'video', audioMessage: 'audio' };
// Containers WhatsApp wraps real content in (disappearing, view-once, edited, captioned documents).
const WRAPPERS = [
  'ephemeralMessage',
  'viewOnceMessage',
  'viewOnceMessageV2',
  'viewOnceMessageV2Extension',
  'documentWithCaptionMessage',
  'editedMessage',
];
// Chats the agent must never send to, even if the bridge were ever to say yes.
const NEVER_SEND_SUFFIXES = ['@g.us', '@broadcast', '@newsletter'];
// Recent conversation passed to the bridge: in memory only, cleared on restart.
const RECENT_MAX_MESSAGES = 8;
const RECENT_MAX_AGE_MS = 12 * 60 * 60 * 1000;
const RECENT_MAX_CHARS = 300;

const log = (...args) => console.log(...args);

// ---------- settings, kill switch, allowlist (all read fresh, never throw) ----------

/** config/settings.json over safe defaults. Any read/parse problem or bad value falls back. */
function loadSettings(filePath = SETTINGS_PATH) {
  const settings = { ...DEFAULT_SETTINGS };
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return settings;
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return settings;
  // dry_run only turns off for a literal false; "false", 0, or null keep it on.
  if (typeof raw.dry_run === 'boolean') settings.dry_run = raw.dry_run;
  const low = raw.min_delay_seconds ?? settings.min_delay_seconds;
  const high = raw.max_delay_seconds ?? settings.max_delay_seconds;
  if (Number.isFinite(low) && Number.isFinite(high) && low >= 0 && low <= high) {
    settings.min_delay_seconds = low;
    settings.max_delay_seconds = high;
  }
  return settings;
}

function isKillSwitchActive(flagPath = KILL_SWITCH_PATH) {
  return fs.existsSync(flagPath);
}

/** Phone number of a person JID, the way agent/router.py reads it (device suffix dropped). */
function phoneFromJid(jid) {
  const match = JID_PATTERN.exec(String(jid ?? '').trim());
  if (!match || !PERSON_SERVERS.has(match[3].toLowerCase())) return null; // groups, broadcasts, channels, @lid
  return E164_DIGITS.test(match[1]) ? match[1] : null;
}

/**
 * Independent allowlist check, read straight from config/relationship_map.json: true only if the
 * JID is a person whose number is mapped to a real relationship. A missing, unreadable, or
 * corrupt map blocks every send.
 */
function enforceAllowlist(jid, mapPath = RELATIONSHIP_MAP_PATH) {
  const phone = phoneFromJid(jid);
  if (!phone) return false;
  let relationshipMap;
  try {
    relationshipMap = JSON.parse(fs.readFileSync(mapPath, 'utf8'));
  } catch {
    return false;
  }
  if (!relationshipMap || typeof relationshipMap !== 'object' || Array.isArray(relationshipMap)) return false;
  const relationship = relationshipMap[phone];
  return typeof relationship === 'string' && !NOT_ALLOWED.has(relationship);
}

/** Random pre-send delay in ms, between the settings' min and max seconds. */
function randomDelayMs(settings, random = Math.random) {
  const { min_delay_seconds: low, max_delay_seconds: high } = settings;
  return Math.round((low + random() * (high - low)) * 1000);
}

// ---------- recent conversation (in memory only) ----------

/**
 * Per-chat buffer of the last RECENT_MAX_MESSAGES messages from the last RECENT_MAX_AGE_MS.
 * Groups, broadcasts, and channels are never recorded. A message id seen before (a bot send
 * echoed back by Baileys) is not recorded twice.
 */
function createRecentBuffer(now = Date.now) {
  const chats = new Map();

  const fresh = (chat) => {
    const cutoff = now() - RECENT_MAX_AGE_MS;
    const entries = (chats.get(chat) ?? []).filter((entry) => entry.at >= cutoff);
    if (entries.length) chats.set(chat, entries);
    else chats.delete(chat);
    return entries;
  };

  return {
    record(chat, { fromMe, text, id }) {
      const clean = typeof text === 'string' ? text.trim().slice(0, RECENT_MAX_CHARS) : '';
      if (!chat || !clean || NEVER_SEND_SUFFIXES.some((suffix) => chat.endsWith(suffix))) return;
      const entries = fresh(chat);
      if (id && entries.some((entry) => entry.id === id)) return;
      entries.push({ from_me: fromMe === true, text: clean, id: id ?? null, at: now() });
      chats.set(chat, entries.slice(-RECENT_MAX_MESSAGES));
    },
    recent(chat) {
      return fresh(chat).map(({ from_me, text }) => ({ from_me, text }));
    },
  };
}

const recentMessages = createRecentBuffer();

/** Text to remember for a payload: the text itself, with a [photo]-style tag for media. */
function recentText(payload) {
  const tag = { image: '[photo]', video: '[video]', audio: '[voice note]' }[payload.message_type];
  if (!tag) return payload.text;
  return payload.text ? `${tag} ${payload.text}` : tag;
}

// ---------- message handling ----------

function unwrap(content) {
  let current = content;
  for (let depth = 0; depth < 5 && current; depth += 1) {
    const wrapper = WRAPPERS.find((key) => current[key]?.message);
    if (!wrapper) break;
    current = current[wrapper].message;
  }
  return current || {};
}

/**
 * The JID used for routing. Baileys 7 may address a 1:1 chat by a privacy id (@lid) and put the
 * phone JID in key.remoteJidAlt; the relationship map is keyed by phone, so route by that.
 * Groups never use the alt (it is only set for 1:1 chats anyway).
 */
function routingJid(key = {}) {
  const remote = key.remoteJid ?? '';
  if (remote.endsWith('@lid') && typeof key.remoteJidAlt === 'string' && key.remoteJidAlt.endsWith('@s.whatsapp.net')) {
    return key.remoteJidAlt;
  }
  return remote;
}

/** Build the bridge payload from a Baileys message. Never throws on missing fields. */
function extractPayload(msg) {
  const content = unwrap(msg?.message);
  let text = '';
  let messageType = 'other';
  let contextInfo;

  if (typeof content.conversation === 'string') {
    text = content.conversation;
    messageType = 'text';
  } else if (content.extendedTextMessage) {
    text = content.extendedTextMessage.text ?? '';
    messageType = 'text';
    contextInfo = content.extendedTextMessage.contextInfo;
  } else {
    const mediaKey = Object.keys(MEDIA_TYPES).find((key) => content[key]);
    if (mediaKey) {
      messageType = MEDIA_TYPES[mediaKey];
      text = content[mediaKey].caption ?? '';
      contextInfo = content[mediaKey].contextInfo;
    }
  }
  // Fall back to whichever message type carries a contextInfo.
  if (!contextInfo) {
    contextInfo = Object.values(content).find((value) => value && typeof value === 'object' && value.contextInfo)?.contextInfo;
  }

  return {
    jid: routingJid(msg?.key),
    text: typeof text === 'string' ? text : '',
    message_type: messageType,
    is_forwarded: contextInfo?.isForwarded === true,
    from_me: msg?.key?.fromMe === true,
    chat_jid: msg?.key?.remoteJid ?? '',
  };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function postToBridge(payload) {
  const response = await axios.post(BRIDGE_URL, payload, { timeout: BRIDGE_TIMEOUT_MS });
  return response.data;
}

async function handleMessage(msg, deps) {
  const {
    bridge = postToBridge,
    send,
    wait = sleep,
    readSettings = loadSettings,
    killSwitch = isKillSwitchActive,
    allowlist = enforceAllowlist,
    random = Math.random,
    recent = recentMessages,
  } = deps;

  if (killSwitch()) {
    log('[KILL SWITCH] active, skipping all processing');
    return;
  }
  const settings = readSettings();
  const chat = msg?.key?.remoteJid ?? '';

  if (msg?.key?.fromMe) {
    // A reply typed on the phone: never answered, but remembered as conversation context.
    if (msg?.message) {
      const own = extractPayload(msg);
      recent.record(own.jid, { fromMe: true, text: recentText(own), id: msg.key.id });
    }
    log('[SKIP] own message');
    return;
  }
  if (!msg?.message) {
    log(`[SKIP] no message content (receipt/protocol) from ${chat}`);
    return;
  }

  const extracted = extractPayload(msg);
  // Earlier messages only: read the buffer before the current message is added to it.
  const payload = { ...extracted, recent_messages: recent.recent(extracted.jid) };
  recent.record(extracted.jid, { fromMe: false, text: recentText(extracted), id: msg.key?.id });
  log(`[ROUTE] ${payload.jid}${payload.jid !== chat ? ` (chat ${chat})` : ''} | type=${payload.message_type} | forwarded=${payload.is_forwarded} | text=${JSON.stringify(payload.text)}`);

  let result;
  try {
    result = await bridge(payload);
  } catch (error) {
    const detail = error.response ? `HTTP ${error.response.status}: ${JSON.stringify(error.response.data)}` : error.message;
    log(`[DECISION] bridge error, not replying: ${detail}`);
    return;
  }
  log(`[DECISION] ${result.status ?? (result.should_reply ? 'ready' : 'no reply')} | relationship=${result.relationship} | ${result.reason}`);

  const reply = typeof result.reply === 'string' ? result.reply.trim() : '';
  if (result.should_reply !== true || !reply) {
    if (reply) log(`[REPLY] held for approval, not sending: ${reply}`);
    else log('[SKIP] no reply');
    return;
  }
  if (NEVER_SEND_SUFFIXES.some((suffix) => chat.endsWith(suffix))) {
    log(`[SKIP] refusing to send to ${chat}: groups/broadcasts/channels are never auto-replied`);
    return;
  }

  log(`[REPLY] ${reply}`);
  if (settings.dry_run) {
    log(`[DRY_RUN] would reply to ${chat}: ${reply}`);
    return;
  }
  const delay = randomDelayMs(settings, random);
  log(`[SEND] waiting ${delay} ms before replying to ${chat}`);
  await wait(delay);
  if (killSwitch()) {
    log('[KILL SWITCH] active, skipping all processing');
    return;
  }
  // The allowlist is checked by phone (payload.jid): for an @lid chat the chat id isn't a number.
  if (!allowlist(payload.jid)) {
    log(`[BLOCKED] failed independent allowlist check: ${payload.jid}`);
    return;
  }
  const sent = await send(chat, { text: reply });
  recent.record(payload.jid, { fromMe: true, text: reply, id: sent?.key?.id });
  log(`[SEND] sent to ${chat}`);
}

// ---------- approved replies (outbox) ----------

async function fetchOutbox() {
  return (await axios.get(`${BRIDGE_BASE}/outbox`, { timeout: 10000 })).data;
}

async function reportSent(id, dryRun) {
  await axios.post(`${BRIDGE_BASE}/outbox/${id}/sent`, { dry_run: dryRun }, { timeout: 10000 });
}

const reportedBlocks = new Set(); // log each blocked approval once, not every 3 s
let killSwitchLogged = false;

/** Send replies Vaideesh approved in the console, under the same kill switch, dry run, and allowlist rules. */
async function processOutbox(deps) {
  const {
    outbox = fetchOutbox,
    report = reportSent,
    send,
    readSettings = loadSettings,
    killSwitch = isKillSwitchActive,
    allowlist = enforceAllowlist,
    recent = recentMessages,
  } = deps;

  if (killSwitch()) {
    if (!killSwitchLogged) log('[KILL SWITCH] active, skipping all processing');
    killSwitchLogged = true;
    return;
  }
  killSwitchLogged = false;
  const settings = readSettings();

  let items;
  try {
    items = await outbox();
  } catch (error) {
    log(`[SKIP] outbox unavailable: ${error.message}`);
    return;
  }
  for (const item of items ?? []) {
    const chat = item.chat_jid || item.jid || '';
    const text = typeof item.text === 'string' ? item.text.trim() : '';
    if (!text || NEVER_SEND_SUFFIXES.some((suffix) => chat.endsWith(suffix))) {
      log(`[SKIP] approved reply ${item.id} not sendable to ${chat}`);
      continue;
    }
    try {
      if (settings.dry_run) {
        log(`[DRY_RUN] would send approved reply to ${chat}: ${text}`);
      } else {
        if (!allowlist(item.jid || chat)) {
          if (!reportedBlocks.has(item.id)) log(`[BLOCKED] failed independent allowlist check: approved reply ${item.id} to ${item.jid || chat}`);
          reportedBlocks.add(item.id);
          continue;
        }
        const sent = await send(chat, { text });
        recent.record(item.jid || chat, { fromMe: true, text, id: sent?.key?.id });
        log(`[SEND] approved reply sent to ${chat}`);
      }
      await report(item.id, settings.dry_run);
    } catch (error) {
      log(`[SKIP] approved reply ${item.id} failed: ${error.message}`);
    }
  }
}

/** messages.upsert handler: only real-time ("notify") messages ever reach the bridge. */
async function handleUpsert(event, deps) {
  if (event?.type !== 'notify') {
    log('[SKIP] history sync message, ignoring');
    return;
  }
  for (const msg of event.messages ?? []) {
    try {
      await handleMessage(msg, deps);
    } catch (error) {
      log(`[SKIP] error handling message from ${msg?.key?.remoteJid}: ${error.message}`);
    }
  }
}

// ---------- connection ----------

async function start() {
  const settings = loadSettings();
  log(
    `[ROUTE] starting WhatsApp client (dry_run=${settings.dry_run}, delay=${settings.min_delay_seconds}-${settings.max_delay_seconds}s, ` +
      `kill switch=${isKillSwitchActive() ? 'ACTIVE' : 'off'}, bridge=${BRIDGE_URL})`,
  );
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: 'warn' }),
    markOnlineOnConnect: false,
    syncFullHistory: false,
  });

  sock.ev.on('creds.update', saveCreds);

  let outboxTimer = null;
  let outboxBusy = false;
  const stopOutbox = () => {
    if (outboxTimer) clearInterval(outboxTimer);
    outboxTimer = null;
  };

  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      log('[ROUTE] scan this QR code with WhatsApp > Linked devices > Link a device:');
      qrcode.generate(qr, { small: true });
    }
    if (connection === 'open') {
      log('[ROUTE] connected to WhatsApp');
      stopOutbox();
      outboxTimer = setInterval(async () => {
        if (outboxBusy) return;
        outboxBusy = true;
        try {
          await processOutbox({ send: (jid, content) => sock.sendMessage(jid, content) });
        } finally {
          outboxBusy = false;
        }
      }, OUTBOX_POLL_MS);
    } else if (connection === 'close') {
      stopOutbox();
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      if (statusCode === DisconnectReason.loggedOut) {
        log(`[SKIP] logged out: delete ${AUTH_DIR} and run again to re-pair`);
      } else {
        log(`[ROUTE] connection closed (${statusCode ?? 'unknown'}), reconnecting...`);
        start().catch((error) => log(`[SKIP] reconnect failed: ${error.message}`));
      }
    }
  });

  sock.ev.on('messages.upsert', (event) =>
    handleUpsert(event, { send: (jid, content) => sock.sendMessage(jid, content) }).catch((error) =>
      log(`[SKIP] upsert handler error: ${error.message}`),
    ),
  );
}

module.exports = {
  BRIDGE_URL,
  DEFAULT_SETTINGS,
  SETTINGS_PATH,
  KILL_SWITCH_PATH,
  RELATIONSHIP_MAP_PATH,
  loadSettings,
  isKillSwitchActive,
  phoneFromJid,
  enforceAllowlist,
  randomDelayMs,
  createRecentBuffer,
  RECENT_MAX_MESSAGES,
  RECENT_MAX_CHARS,
  extractPayload,
  routingJid,
  handleMessage,
  handleUpsert,
  processOutbox,
};

if (require.main === module) {
  start().catch((error) => {
    log(`[SKIP] failed to start: ${error.message}`);
    process.exit(1);
  });
}
