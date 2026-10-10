import assert from 'node:assert/strict';
import test from 'node:test';
import { Long } from 'bson';
import { unreadAnchor, createUnreadRoomDiscovery } from '../src/room-discovery.mjs';
import { createConversationSyncState, runConversationSync } from '../src/conversation-sync.mjs';

const now = 1791276000000;
const packet = (id, changes = {}) => ({ method: 'CHATINFO', statusCode: 0,
  body: { status: 0, chatInfo: { chatId: Long.fromString(id), newMessageCount: 6,
    lastSeenLogId: Long.ZERO, lastLogId: Long.fromString('106'),
    lastChatLog: { sendAt: now / 1000 - 100 }, ...changes } } });

test('unseen unread room recovers all six messages without any live MSG or read/write call', async () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '500' }]);
  const calls = [], received = [], controller = new AbortController();
  const discovery = createUnreadRoomDiscovery({ getChatIds: () => ['101', '202'], now: () => now,
    getChannelInfo: async id => { calls.push(['CHATINFO', id]); return packet(id); } });
  const client = { getMessagePage: async (id, options) => {
    calls.push(['MCHATLOGS', id, options.from]);
    return { complete: true, messages: id === '202' ? Array.from({ length: 6 }, (_, i) => ({
      chat_id: id, log_id: String(101 + i), author_id: 222, message_type: 1,
      sent_at: now / 1000 - 100 + i, message: `unread-${i}`, author_name: 'fixture' })) : [] };
  }, markRead() { assert.fail('NOTIREAD'); }, getChatInfo() { assert.fail('CHATONROOM'); },
  sendMessage() { assert.fail('WRITE'); } };
  await runConversationSync({ state, client, discoverUnreadRooms: discovery, signal: controller.signal,
    currentUserId: '1', onMessage: async event => { received.push(event); return true; },
    emit: async event => { if (event.status === 'catchup_completed') controller.abort(); return true; } });
  assert.equal(received.length, 6);
  assert.deepEqual(received.map(item => item.chatId), Array(6).fill('202'));
  assert.ok(calls.some(call => call[0] === 'MCHATLOGS' && call[1] === '202' && call[2] === '0'));
  assert.equal(state.snapshot().find(row => row.chatId === '101').afterLogId, '500');
  assert.equal(state.snapshot().find(row => row.chatId === '202').afterLogId, '106');
});

test('accepts safe numeric chat IDs and exact Long cursors; rejects wrong room and lossy IDs', () => {
  assert.equal(unreadAnchor(packet('202', { chatId: 202 }), '202', now / 1000).afterLogId, '0');
  for (const changes of [{ chatId: 203 }, { chatId: Number.MAX_SAFE_INTEGER + 1 },
    { lastSeenLogId: '106' }, { lastSeenLogId: '107' }, { lastSeenLogId: Number.MAX_SAFE_INTEGER + 1 },
    { left: true }])
    assert.equal(unreadAnchor(packet('202', changes), '202', now / 1000), null);
});

test('old and future room previews cannot seed a new recovery window', () => {
  for (const sendAt of [now / 1000 - 8 * 86400, now / 1000 + 301, NaN])
    assert.equal(unreadAnchor(packet('202', { lastChatLog: { sendAt } }), '202', now / 1000), null);
});

test('synced login omits preview and unread count, but real head verifies recovery without changing read state', async () => {
  const state = createConversationSyncState([]), calls = [];
  const discovery = createUnreadRoomDiscovery({ getChatIds: () => ['202'], now: () => now,
    getChannelInfo: async id => packet(id, { newMessageCount: 0, invalidNewMessageCount: true, lastChatLog: null }),
    peekLatest: async (id, head) => { calls.push([id, head]); return { log_id: head, sent_at: now / 1000 - 100 }; } });
  assert.equal((await discovery(state, new AbortController().signal)).discovered, 1);
  assert.deepEqual(calls, [['202', '106']]);
  assert.equal(state.snapshot()[0].afterLogId, '0');
});

test('a missing or mismatched head body remains retryable instead of inventing a timestamp', async () => {
  const state = createConversationSyncState([]);
  const discovery = createUnreadRoomDiscovery({ getChatIds: () => ['202'], now: () => now,
    getChannelInfo: async id => packet(id, { lastChatLog: null }),
    peekLatest: async () => ({ log_id: '999', sent_at: now / 1000 }) });
  assert.equal((await discovery(state, new AbortController().signal)).partial, true);
  assert.equal(state.size, 0);
});

test('known and cleared-room recovery anchors cannot be overwritten by server unread counts', async () => {
  const state = createConversationSyncState([{ chatId: '202', afterLogId: '900' }]);
  const discovery = createUnreadRoomDiscovery({ getChatIds: () => ['202'], now: () => now,
    getChannelInfo: () => assert.fail('known room was probed') });
  assert.deepEqual(await discovery(state, new AbortController().signal), { discovered: 0, partial: false });
  assert.equal(state.snapshot()[0].afterLogId, '900');
});

test('recovery room capacity is independent of the 80-body display limit', () => {
  const state = createConversationSyncState(Array.from({ length: 80 }, (_, i) => ({
    chatId: String(i + 1), afterLogId: '100' })));
  assert.equal(state.seedUnread({ chatId: '202', afterLogId: '0' }), true);
  assert.equal(state.size, 81);
  assert.equal(state.seedUnread({ chatId: '202', afterLogId: '1' }), false);
  assert.equal(state.snapshot().find(row => row.chatId === '202').afterLogId, '0');
});

test('a live message arriving before discovery cannot skip earlier unread bodies', async () => {
  const state = createConversationSyncState([]), anchors = [];
  state.observe({ type: 'message', chatId: '202', logId: '500' });
  const discovery = createUnreadRoomDiscovery({ getChatIds: () => [], now: () => now,
    getChannelInfo: async id => packet(id), onAnchor: async anchor => { anchors.push(anchor); return true; } });
  assert.equal((await discovery(state, new AbortController().signal)).discovered, 1);
  assert.equal(state.snapshot()[0].afterLogId, '0');
  assert.equal(anchors[0].throughLogId, '106');
});

test('bounded probes rotate and successful non-unread metadata is cached', async () => {
  const state = createConversationSyncState([]), calls = [];
  const discovery = createUnreadRoomDiscovery({ getChatIds: () => ['1', '2', '3'], batchSize: 2,
    now: () => now, getChannelInfo: async id => { calls.push(id); return packet(id, { lastSeenLogId: '106' }); } });
  assert.equal((await discovery(state, new AbortController().signal)).partial, true);
  assert.equal((await discovery(state, new AbortController().signal)).partial, false);
  await discovery(state, new AbortController().signal);
  assert.deepEqual(calls.sort(), ['1', '2', '3']);
});

test('failed room metadata remains retryable and does not create a cursor', async () => {
  const state = createConversationSyncState([]); let attempt = 0;
  const discovery = createUnreadRoomDiscovery({ getChatIds: () => ['202'], now: () => now,
    getChannelInfo: async id => { if (++attempt === 1) throw Error(); return packet(id); } });
  assert.equal((await discovery(state, new AbortController().signal)).partial, true);
  assert.equal(state.size, 0);
  assert.equal((await discovery(state, new AbortController().signal)).discovered, 1);
});

test('cancellation stops discovery and a full state never evicts an existing room', async () => {
  const cancelled = new AbortController(); cancelled.abort();
  const state = createConversationSyncState([{ chatId: '1', afterLogId: '7' }], 1);
  const discovery = createUnreadRoomDiscovery({ getChatIds: () => ['202'], now: () => now,
    getChannelInfo: async id => packet(id) });
  assert.equal((await discovery(state, cancelled.signal)).discovered, 0);
  assert.equal((await discovery(state, new AbortController().signal)).partial, true);
  assert.equal(state.has('1'), true);
  assert.equal(state.has('202'), false);
});
