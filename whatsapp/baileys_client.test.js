'use strict';
// Offline tests for whatsapp/baileys_client.js (no WhatsApp connection, bridge stubbed).
// Run: node --test whatsapp/baileys_client.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const client = require('./baileys_client');

const PHONE = '919000000013@s.whatsapp.net';
const LID = '123456789012345@lid';
const msg = (message, key = {}) => ({ key: { remoteJid: PHONE, fromMe: false, id: 'X', ...key }, message });
const READY = { should_reply: true, reply: 'Saapten da, nee?', status: 'ready', relationship: 'friends', reason: 'passed all gates' };
const LIVE = { dry_run: false, min_delay_seconds: 3, max_delay_seconds: 12 };

/** Message-flow deps. Defaults: dry run on, kill switch off, allowlist passes. */
function harness(result = { should_reply: false, reply: null, status: 'ignored', relationship: 'friends', reason: 'own message' }, overrides = {}) {
  const calls = { bridge: [], send: [], waits: [], allowlist: [] };
  const deps = {
    bridge: async (payload) => {
      calls.bridge.push(payload);
      if (result instanceof Error) throw result;
      return result;
    },
    send: async (jid, content) => calls.send.push({ jid, content }),
    wait: async (ms) => calls.waits.push(ms),
    readSettings: () => ({ ...client.DEFAULT_SETTINGS }),
    killSwitch: () => false,
    allowlist: (jid) => { calls.allowlist.push(jid); return true; },
    random: () => 0.5,
    recent: client.createRecentBuffer(),
    ...overrides,
  };
  return { calls, deps };
}

const logs = [];
let tmp;
test.beforeEach(() => {
  logs.length = 0;
  test.mock.method(console, 'log', (...args) => logs.push(args.join(' ')));
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'baileys-client-test-'));
});
test.afterEach(() => {
  test.mock.restoreAll();
  fs.rmSync(tmp, { recursive: true, force: true });
});
const tmpFile = (name, content) => {
  const file = path.join(tmp, name);
  fs.writeFileSync(file, typeof content === 'string' ? content : JSON.stringify(content));
  return file;
};

// ---------- loadSettings ----------

test('settings: missing file gives the safe defaults', () => {
  assert.deepEqual(client.loadSettings(path.join(tmp, 'missing.json')), { dry_run: true, min_delay_seconds: 3, max_delay_seconds: 12 });
});

test('settings: unreadable, unparseable, or wrong-shaped files give the defaults', () => {
  for (const content of ['{not json', '', '[1, 2]', '"dry_run"', 'null']) {
    assert.deepEqual(client.loadSettings(tmpFile('s.json', content)), client.DEFAULT_SETTINGS, content);
  }
  assert.deepEqual(client.loadSettings(tmp), client.DEFAULT_SETTINGS); // a directory, not a file
});

test('settings: a valid file is used', () => {
  assert.deepEqual(client.loadSettings(tmpFile('s.json', { dry_run: false, min_delay_seconds: 5, max_delay_seconds: 9 })),
    { dry_run: false, min_delay_seconds: 5, max_delay_seconds: 9 });
});

test('settings: dry_run only turns off for a literal false', () => {
  for (const value of ['false', 0, null, 'no', [], {}]) {
    assert.equal(client.loadSettings(tmpFile('s.json', { dry_run: value })).dry_run, true, JSON.stringify(value));
  }
});

test('settings: bad delay values fall back to the default pair', () => {
  for (const [low, high] of [[10, 2], [-1, 5], ['3', 12], [3, null], [true, 12]]) {
    const loaded = client.loadSettings(tmpFile('s.json', { min_delay_seconds: low, max_delay_seconds: high }));
    assert.deepEqual([loaded.min_delay_seconds, loaded.max_delay_seconds], [3, 12], `${low}-${high}`);
  }
});

// ---------- kill switch ----------

test('kill switch follows the flag file', () => {
  const flag = path.join(tmp, 'kill_switch.flag');
  assert.equal(client.isKillSwitchActive(flag), false);
  fs.writeFileSync(flag, '');
  assert.equal(client.isKillSwitchActive(flag), true);
});

test('kill switch active: nothing reaches the bridge and nothing is sent', async () => {
  const { calls, deps } = harness(READY, { killSwitch: () => true, readSettings: () => LIVE });
  await client.handleUpsert({ type: 'notify', messages: [msg({ conversation: 'Saaptiya?' })] }, deps);
  assert.equal(calls.bridge.length, 0);
  assert.equal(calls.send.length, 0);
  assert.deepEqual(logs, ['[KILL SWITCH] active, skipping all processing']);
});

test('kill switch flipped on during the pre-send delay still stops the send', async () => {
  let active = false;
  const { calls, deps } = harness(READY, {
    readSettings: () => LIVE,
    killSwitch: () => active,
    wait: async () => { active = true; },
  });
  await client.handleMessage(msg({ conversation: 'Saaptiya?' }), deps);
  assert.equal(calls.bridge.length, 1);
  assert.equal(calls.send.length, 0);
  assert.equal(logs.at(-1), '[KILL SWITCH] active, skipping all processing');
});

// ---------- allowlist ----------

const MAP = { _default: 'unknown', 919000000013: 'friends', 919000000003: 'unknown', 919000000004: 'REPLACE_ME' };

test('allowlist: mapped person JIDs pass, everything else is blocked', () => {
  const mapPath = tmpFile('map.json', MAP);
  const cases = {
    [PHONE]: true,
    '919000000013:12@s.whatsapp.net': true, // linked device, same rule as agent/router.py
    '919000000013@c.us': true,
    '919000000003@s.whatsapp.net': false, // mapped as unknown
    '919000000004@s.whatsapp.net': false, // not classified yet
    '15550000000@s.whatsapp.net': false, // missing from the map
    '_default@s.whatsapp.net': false,
    '919000000013@g.us': false, // group whose id collides with a contact's number
    'status@broadcast': false,
    '919000000013@lid': false, // privacy id, not a phone number
    '': false,
    'not-a-jid': false,
  };
  for (const [jid, allowed] of Object.entries(cases)) assert.equal(client.enforceAllowlist(jid, mapPath), allowed, jid);
  assert.equal(client.enforceAllowlist(undefined, mapPath), false);
});

test('allowlist: a missing or corrupt map blocks everyone', () => {
  assert.equal(client.enforceAllowlist(PHONE, path.join(tmp, 'missing.json')), false);
  assert.equal(client.enforceAllowlist(PHONE, tmpFile('map.json', '{broken')), false);
  assert.equal(client.enforceAllowlist(PHONE, tmpFile('map.json', '[1]')), false);
});

test('allowlist failure blocks the send even when the bridge said should_reply', async () => {
  const { calls, deps } = harness(READY, { readSettings: () => LIVE, allowlist: () => false });
  await client.handleMessage(msg({ conversation: 'Saaptiya?' }), deps);
  assert.equal(calls.send.length, 0);
  assert.ok(logs.some((line) => line.startsWith('[BLOCKED] failed independent allowlist check')));
});

test('allowlist is checked by phone even for an @lid chat, and the reply goes to the chat', async () => {
  const { calls, deps } = harness(READY, { readSettings: () => LIVE });
  await client.handleMessage(msg({ conversation: 'Saaptiya?' }, { remoteJid: LID, remoteJidAlt: PHONE }), deps);
  assert.deepEqual(calls.allowlist, [PHONE]);
  assert.deepEqual(calls.send, [{ jid: LID, content: { text: 'Saapten da, nee?' } }]);
});

// ---------- delay ----------

test('delay comes from the settings range', () => {
  assert.equal(client.randomDelayMs(LIVE, () => 0), 3000);
  assert.equal(client.randomDelayMs(LIVE, () => 1), 12000);
  assert.equal(client.randomDelayMs({ min_delay_seconds: 5, max_delay_seconds: 5 }, () => 0.7), 5000);
  for (let i = 0; i < 2000; i += 1) {
    const delay = client.randomDelayMs(LIVE);
    assert.ok(delay >= 3000 && delay <= 12000, `delay ${delay}`);
  }
});

test('live send waits the settings-based delay, then sends', async () => {
  const { calls, deps } = harness(READY, { readSettings: () => ({ dry_run: false, min_delay_seconds: 4, max_delay_seconds: 6 }) });
  await client.handleMessage(msg({ conversation: 'Saaptiya?' }), deps);
  assert.deepEqual(calls.waits, [5000]); // random() = 0.5 -> halfway between 4 s and 6 s
  assert.equal(calls.send.length, 1);
});

test('settings are re-read for every message', async () => {
  let reads = 0;
  const { deps } = harness(READY, { readSettings: () => { reads += 1; return client.DEFAULT_SETTINGS; } });
  await client.handleUpsert({ type: 'notify', messages: [msg({ conversation: 'a' }), msg({ conversation: 'b' })] }, deps);
  assert.equal(reads, 2);
});

// ---------- message flow (unchanged behaviour) ----------

test('history sync (type !== "notify") never reaches the bridge', async () => {
  const { calls, deps } = harness();
  for (const type of ['append', 'history', undefined]) {
    await client.handleUpsert({ type, messages: [msg({ conversation: 'old message' })] }, deps);
  }
  assert.equal(calls.bridge.length, 0);
  assert.ok(logs.every((line) => line === '[SKIP] history sync message, ignoring'));
});

test('own messages are skipped before the bridge', async () => {
  const { calls, deps } = harness();
  await client.handleUpsert({ type: 'notify', messages: [msg({ conversation: 'hi' }, { fromMe: true })] }, deps);
  assert.equal(calls.bridge.length, 0);
  assert.ok(logs.includes('[SKIP] own message'));
});

test('payload extraction for every message shape', () => {
  assert.deepEqual(client.extractPayload(msg({ conversation: 'Machan saaptiya?' })), {
    jid: PHONE, text: 'Machan saaptiya?', message_type: 'text', is_forwarded: false, from_me: false, chat_jid: PHONE,
  });
  assert.deepEqual(
    client.extractPayload(msg({ extendedTextMessage: { text: 'check this', contextInfo: { isForwarded: true } } })),
    { jid: PHONE, text: 'check this', message_type: 'text', is_forwarded: true, from_me: false, chat_jid: PHONE },
  );
  assert.equal(client.extractPayload(msg({ imageMessage: { caption: 'view!' } })).text, 'view!');
  assert.equal(client.extractPayload(msg({ imageMessage: { caption: 'view!' } })).message_type, 'image');
  assert.deepEqual(client.extractPayload(msg({ videoMessage: { contextInfo: { isForwarded: true } } })), {
    jid: PHONE, text: '', message_type: 'video', is_forwarded: true, from_me: false, chat_jid: PHONE,
  });
  assert.equal(client.extractPayload(msg({ audioMessage: { seconds: 4 } })).message_type, 'audio');
  assert.equal(client.extractPayload(msg({ audioMessage: { seconds: 4 } })).text, '');
  assert.equal(client.extractPayload(msg({ reactionMessage: { text: '👍' } })).message_type, 'other');
  assert.equal(client.extractPayload(msg({ ephemeralMessage: { message: { conversation: 'disappearing' } } })).text, 'disappearing');
  assert.equal(client.extractPayload(msg({ stickerMessage: { contextInfo: { isForwarded: true } } })).is_forwarded, true);
  assert.equal(client.extractPayload({ key: {} }).message_type, 'other'); // missing everything: no crash
});

test('@lid chats are routed by the phone JID in remoteJidAlt; groups are not', () => {
  const lid = msg({ conversation: 'hi' }, { remoteJid: LID, remoteJidAlt: PHONE });
  assert.equal(client.extractPayload(lid).jid, PHONE);
  assert.equal(client.routingJid({ remoteJid: '120363041234567890@g.us', remoteJidAlt: PHONE }), '120363041234567890@g.us');
  assert.equal(client.routingJid({ remoteJid: LID }), LID);
});

test('dry run logs what it would send and never sends', async () => {
  const { calls, deps } = harness(READY);
  await client.handleUpsert({ type: 'notify', messages: [msg({ conversation: 'Saaptiya?' })] }, deps);
  assert.equal(calls.send.length, 0);
  assert.equal(calls.waits.length, 0);
  assert.ok(logs.includes(`[DRY_RUN] would reply to ${PHONE}: Saapten da, nee?`));
});

test('held drafts, ignored messages, and empty replies are never sent', async () => {
  for (const result of [
    { should_reply: false, reply: 'Paakalam da', status: 'needs_approval', relationship: 'friends', reason: 'needs approval' },
    { should_reply: false, reply: null, status: 'ignored', relationship: 'friends', reason: 'money' },
    { should_reply: true, reply: '   ', status: 'ready', relationship: 'friends', reason: 'x' },
  ]) {
    const { calls, deps } = harness(result, { readSettings: () => LIVE });
    await client.handleMessage(msg({ conversation: 'hi' }), deps);
    assert.equal(calls.send.length, 0);
  }
});

test('never sends to groups, even if the bridge says yes', async () => {
  const { calls, deps } = harness(READY, { readSettings: () => LIVE });
  await client.handleMessage(msg({ conversation: 'hi all' }, { remoteJid: '120363041234567890@g.us' }), deps);
  assert.equal(calls.send.length, 0);
});

test('bridge errors are logged and never crash or send', async () => {
  const { calls, deps } = harness(new Error('connect ECONNREFUSED 127.0.0.1:5001'), { readSettings: () => LIVE });
  await client.handleUpsert({ type: 'notify', messages: [msg({ conversation: 'hi' })] }, deps);
  assert.equal(calls.send.length, 0);
  assert.ok(logs.some((line) => line.startsWith('[DECISION] bridge error, not replying')));
});

// ---------- approved replies (outbox) ----------

function outboxHarness(items, overrides = {}) {
  const calls = { send: [], report: [], allowlist: [] };
  const deps = {
    outbox: async () => items,
    report: async (id, dryRun) => calls.report.push({ id, dryRun }),
    send: async (jid, content) => calls.send.push({ jid, content }),
    readSettings: () => ({ ...client.DEFAULT_SETTINGS }),
    killSwitch: () => false,
    allowlist: (jid) => { calls.allowlist.push(jid); return true; },
    recent: client.createRecentBuffer(),
    ...overrides,
  };
  return { calls, deps };
}

test('approved replies: dry run logs and reports dry_run, never sends', async () => {
  const { calls, deps } = outboxHarness([{ id: 7, chat_jid: LID, jid: PHONE, text: 'Paakalam da' }]);
  await client.processOutbox(deps);
  assert.equal(calls.send.length, 0);
  assert.deepEqual(calls.report, [{ id: 7, dryRun: true }]);
  assert.ok(logs.includes(`[DRY_RUN] would send approved reply to ${LID}: Paakalam da`));
});

test('approved replies: live, allowlist checked by phone, sent to the chat, reported sent', async () => {
  const { calls, deps } = outboxHarness([{ id: 8, chat_jid: LID, jid: PHONE, text: 'Paakalam da' }], { readSettings: () => LIVE });
  await client.processOutbox(deps);
  assert.deepEqual(calls.allowlist, [PHONE]);
  assert.deepEqual(calls.send, [{ jid: LID, content: { text: 'Paakalam da' } }]);
  assert.deepEqual(calls.report, [{ id: 8, dryRun: false }]);
});

test('approved replies: allowlist failure blocks, is not reported sent, and is logged once', async () => {
  const { calls, deps } = outboxHarness([{ id: 31, chat_jid: PHONE, jid: PHONE, text: 'hello' }], { readSettings: () => LIVE, allowlist: () => false });
  await client.processOutbox(deps);
  await client.processOutbox(deps);
  assert.equal(calls.send.length, 0);
  assert.equal(calls.report.length, 0);
  assert.equal(logs.filter((line) => line.startsWith('[BLOCKED] failed independent allowlist check')).length, 1);
});

test('approved replies: kill switch stops the whole outbox', async () => {
  const { calls, deps } = outboxHarness([{ id: 9, chat_jid: PHONE, jid: PHONE, text: 'hello' }], { readSettings: () => LIVE, killSwitch: () => true });
  await client.processOutbox(deps);
  assert.equal(calls.send.length, 0);
  assert.equal(calls.report.length, 0);
});

test('approved replies to groups or with empty text are never sent', async () => {
  const { calls, deps } = outboxHarness([
    { id: 1, chat_jid: '120363041234567890@g.us', text: 'hi' },
    { id: 2, chat_jid: PHONE, text: '   ' },
  ], { readSettings: () => LIVE });
  await client.processOutbox(deps);
  assert.equal(calls.send.length, 0);
  assert.equal(calls.report.length, 0);
});

test('a failed send is not reported as sent', async () => {
  const { calls, deps } = outboxHarness([{ id: 10, chat_jid: PHONE, jid: PHONE, text: 'hello' }], {
    readSettings: () => LIVE,
    send: async () => { throw new Error('socket closed'); },
  });
  await client.processOutbox(deps);
  assert.equal(calls.report.length, 0);
});

test('outbox unavailable is logged, not thrown', async () => {
  const { deps } = outboxHarness([], { outbox: async () => { throw new Error('ECONNREFUSED'); } });
  await client.processOutbox(deps);
  assert.ok(logs.some((line) => line.startsWith('[SKIP] outbox unavailable')));
});

// ---------- recent conversation ----------

const HOUR = 60 * 60 * 1000;

test('recent buffer keeps the last 8 messages, clipped, and drops anything older than 12 hours', () => {
  let clock = 0;
  const recent = client.createRecentBuffer(() => clock);
  recent.record(PHONE, { fromMe: false, text: 'old one' });
  clock = 13 * HOUR;
  for (let i = 1; i <= 10; i += 1) recent.record(PHONE, { fromMe: i % 2 === 0, text: `msg ${i}` });
  const kept = recent.recent(PHONE);
  assert.equal(kept.length, client.RECENT_MAX_MESSAGES);
  assert.deepEqual(kept[0], { from_me: false, text: 'msg 3' });
  assert.deepEqual(kept.at(-1), { from_me: true, text: 'msg 10' });
  recent.record(PHONE, { fromMe: false, text: 'x'.repeat(1000) });
  assert.equal(recent.recent(PHONE).at(-1).text.length, client.RECENT_MAX_CHARS);
  clock += 13 * HOUR;
  assert.deepEqual(recent.recent(PHONE), []);
});

test('recent buffer never records groups, broadcasts, channels, empty text, or the same id twice', () => {
  const recent = client.createRecentBuffer();
  for (const chat of ['120363041234567890@g.us', 'status@broadcast', '120363111111111111@newsletter']) {
    recent.record(chat, { fromMe: false, text: 'hi all' });
    assert.deepEqual(recent.recent(chat), []);
  }
  recent.record(PHONE, { fromMe: false, text: '   ' });
  recent.record(PHONE, { fromMe: true, text: 'Saapten da', id: 'SENT1' });
  recent.record(PHONE, { fromMe: true, text: 'Saapten da', id: 'SENT1' });
  assert.deepEqual(recent.recent(PHONE), [{ from_me: true, text: 'Saapten da' }]);
});

test('the bridge gets earlier messages (theirs and yours from the phone) but not the current one', async () => {
  const { calls, deps } = harness();
  await client.handleUpsert({ type: 'notify', messages: [msg({ conversation: 'Machan saaptiya?' }, { id: 'A' })] }, deps);
  await client.handleUpsert({ type: 'notify', messages: [msg({ conversation: 'Saapten da' }, { fromMe: true, id: 'B' })] }, deps);
  await client.handleUpsert({ type: 'notify', messages: [msg({ imageMessage: {} }, { id: 'C' })] }, deps);
  await client.handleUpsert({ type: 'notify', messages: [msg({ conversation: 'Nee?' }, { id: 'D' })] }, deps);
  assert.equal(calls.bridge.length, 3); // own message never reaches the bridge
  assert.deepEqual(calls.bridge[0].recent_messages, []);
  assert.deepEqual(calls.bridge[2].recent_messages, [
    { from_me: false, text: 'Machan saaptiya?' },
    { from_me: true, text: 'Saapten da' },
    { from_me: false, text: '[photo]' },
  ]);
  assert.equal(calls.bridge[2].text, 'Nee?');
});

test('a live auto-reply is remembered once, even when Baileys echoes it back', async () => {
  const { deps } = harness(READY, {
    readSettings: () => LIVE,
    send: async () => ({ key: { id: 'SENT1' } }),
  });
  await client.handleMessage(msg({ conversation: 'Saaptiya?' }, { id: 'A' }), deps);
  await client.handleMessage(msg({ conversation: 'Saapten da, nee?' }, { fromMe: true, id: 'SENT1' }), deps);
  assert.deepEqual(deps.recent.recent(PHONE), [
    { from_me: false, text: 'Saaptiya?' },
    { from_me: true, text: 'Saapten da, nee?' },
  ]);
});

test('dry run does not remember a reply that was never sent', async () => {
  const { deps } = harness(READY);
  await client.handleMessage(msg({ conversation: 'Saaptiya?' }), deps);
  assert.deepEqual(deps.recent.recent(PHONE), [{ from_me: false, text: 'Saaptiya?' }]);
});

test('nothing is remembered while the kill switch is active, or from history sync', async () => {
  const { deps } = harness(READY, { killSwitch: () => true });
  await client.handleMessage(msg({ conversation: 'Saaptiya?' }), deps);
  await client.handleMessage(msg({ conversation: 'mine' }, { fromMe: true }), deps);
  const sync = harness();
  await client.handleUpsert({ type: 'append', messages: [msg({ conversation: 'old message' })] }, sync.deps);
  assert.deepEqual(deps.recent.recent(PHONE), []);
  assert.deepEqual(sync.deps.recent.recent(PHONE), []);
});

test('an approved reply sent live is remembered under the phone JID', async () => {
  const { deps } = outboxHarness([{ id: 9, chat_jid: LID, jid: PHONE, text: 'Paakalam da' }], {
    readSettings: () => LIVE,
    send: async () => ({ key: { id: 'OUT9' } }),
  });
  await client.processOutbox(deps);
  assert.deepEqual(deps.recent.recent(PHONE), [{ from_me: true, text: 'Paakalam da' }]);
});
