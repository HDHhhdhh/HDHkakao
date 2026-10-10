import assert from 'node:assert/strict';
import test from 'node:test';
import { getEventListeners } from 'node:events';
import { createConversationSyncState, runConversationSync } from '../src/conversation-sync.mjs';

const body = (chatId, logId, author = 222) => ({ chat_id: chatId, log_id: String(logId), author_id: author,
  message: 'synthetic-' + logId, message_type: 1, sent_at: 1790733600 + Number(logId), author_name: 'synthetic' });

async function cycles({ state, pages, stopAfter = 1, maxMessages = 500, intervalMs = 1 }) {
  const events = [], messages = [], calls = [], forbidden = [];
  const controller = new AbortController();
  let completed = 0;
  const client = {
    getMessagePage: async (chatId, options) => {
      calls.push({ chatId, ...options });
      return pages(chatId, options, completed);
    },
    markRead() { forbidden.push('markRead'); throw Error(); },
    getChatInfo() { forbidden.push('CHATONROOM'); throw Error(); },
    sendMessage() { forbidden.push('WRITE'); throw Error(); },
  };
  await runConversationSync({ state, client, currentUserId: '123', signal: controller.signal,
    maxMessages, intervalMs, onMessage: async event => { messages.push(event); return true; },
    emit: async event => {
      events.push(event);
      if (event.status === 'catchup_completed' && ++completed >= stopAfter) controller.abort();
      if (event.status === 'catchup_failed') controller.abort();
      return true;
    } });
  return { events, messages, calls, forbidden, controller };
}

test('live maximum and own ACK cannot advance an existing verified history cursor', () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '99', limit: 50 }]);
  state.observe({ type: 'message', chatId: '101', logId: '102' });
  state.observe({ type: 'message', chatId: '101', logId: '500', replyAck: true });
  assert.equal(state.snapshot()[0].afterLogId, '99');
  state.advance(state.snapshot()[0], '102');
  state.observe({ type: 'message', chatId: '101', logId: '1000' });
  assert.equal(state.snapshot()[0].afterLogId, '102');
});

test('history fills interleaved own and peer messages despite a newer peer push', async () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '99', limit: 50 }]);
  state.observe({ type: 'message', chatId: '101', logId: '102' });
  const result = await cycles({ state, pages: async (_chat, options) => ({
    messages: [body('101', 102), body('101', 101, 123), body('101', 100)], complete: true,
  }) });
  assert.equal(result.calls[0].from, '99');
  assert.deepEqual(result.messages.map(event => [event.logId, event.isMine]), [['100', false], ['101', true], ['102', false]]);
  assert.equal(state.snapshot()[0].afterLogId, '102');
  assert.deepEqual(result.forbidden, []);
});

test('a later own message without a MSG push is collected on the next bounded cycle', async () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '99', limit: 50 }]);
  const result = await cycles({ state, stopAfter: 2, pages: async (_chat, options, cycle) => ({
    messages: (cycle ? [body('101', 100), body('101', 101, 123)] : [body('101', 100)])
      .filter(row => BigInt(row.log_id) > BigInt(options.from)), complete: true,
  }) });
  assert.deepEqual(result.calls.map(call => call.from), ['99', '100']);
  assert.deepEqual(result.messages.map(row => [row.logId, row.isMine]), [['100', false], ['101', true]]);
  assert.deepEqual(result.forbidden, []);
});

test('new room starts before its first observed MSG and never follows later live maxima', () => {
  const state = createConversationSyncState([]);
  state.observe({ type: 'message', chatId: '202', logId: '500', replyAck: true });
  assert.equal(state.size, 0);
  state.observe({ type: 'message', chatId: '202', logId: '50' });
  state.observe({ type: 'message', chatId: '202', logId: '100' });
  assert.equal(state.snapshot()[0].afterLogId, '49');
  for (const bad of [{ chatId: '00', logId: '1' }, { chatId: '3', logId: '0' },
    { chatId: '3', logId: '9223372036854775808' }, { chatId: '9223372036854775808', logId: '1' }])
    state.observe({ type: 'message', ...bad });
  assert.equal(state.size, 1);
});

test('80 active rooms get a first-pass chance within the 500-message cycle budget', async () => {
  const state = createConversationSyncState(Array.from({ length: 80 }, (_, index) => ({
    chatId: String(index + 1), afterLogId: '0', limit: 50,
  })));
  const result = await cycles({ state, pages: async (chatId, options) => {
    const remaining = Array.from({ length: 12 }, (_, index) => body(chatId, index + 1))
      .filter(row => BigInt(row.log_id) > BigInt(options.from));
    return { messages: remaining, complete: remaining.length <= options.count };
  } });
  assert.equal(new Set(result.calls.filter(call => call.from === '0').map(call => call.chatId)).size, 80);
  assert.equal(result.messages.length, 500);
  assert.equal(result.events.find(event => event.status === 'catchup_completed').partial, true);
});

test('clear-history tombstone cursor does not resurrect old rows or get reset by a stale push', async () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '1002', limit: 50 }]);
  state.observe({ type: 'message', chatId: '101', logId: '1000' });
  const result = await cycles({ state, pages: async (_chat, options) => ({
    messages: [body('101', 1000), body('101', 1001, 123), body('101', 1003, 123)]
      .filter(row => BigInt(row.log_id) > BigInt(options.from)), complete: true,
  }) });
  assert.deepEqual(result.messages.map(row => row.logId), ['1003']);
  assert.equal(result.calls[0].from, '1002');
});

test('an empty unchanged oldest-minus-one anchor needs no false durability progress frame', async () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '99', limit: 50 }]);
  const result = await cycles({ state, pages: async () => ({ messages: [], complete: true }) });
  assert.equal(result.events.some(event => event.type === 'catchup_progress'), false);
  assert.deepEqual(result.events.filter(event => event.type === 'conversation_synced'),
    [{ type: 'conversation_synced', chatId: '101', complete: true }]);
  assert.equal(state.snapshot()[0].afterLogId, '99');
});

test('an empty room still refreshes its name without adding a message or changing the cursor', async () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '99', limit: 50 }]);
  const controller = new AbortController();
  const calls = [], events = [], messages = [];
  await runConversationSync({ state, currentUserId: '123', signal: controller.signal,
    client: { getMessagePage: async () => ({ messages: [], complete: true }) },
    refreshChatTitles: async (ids, signal) => {
      calls.push(ids);
      await Promise.resolve();
      assert.equal(signal.aborted, false);
      events.push({ type: 'room_metadata', chatId: '101', chatTitle: '내 저장 이름', titleSource: 'synced_room_name' });
    },
    onMessage: async event => { messages.push(event); return true; },
    emit: async event => {
      events.push(event);
      if (event.status === 'catchup_completed') controller.abort();
      return true;
    },
  });
  assert.deepEqual(calls, [['101']]);
  assert.equal(messages.length, 0);
  assert.equal(state.snapshot()[0].afterLogId, '99');
  assert.ok(events.findIndex(event => event.type === 'room_metadata')
    < events.findIndex(event => event.status === 'catchup_completed'));
});

test('a failed name refresh cannot discard an otherwise valid history page', async () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '99', limit: 50 }]);
  const controller = new AbortController();
  const messages = [];
  await runConversationSync({ state, currentUserId: '123', signal: controller.signal,
    client: { getMessagePage: async () => ({ messages: [body('101', 100)], complete: true }) },
    refreshChatTitles: async () => { throw Error('metadata unavailable'); },
    onMessage: async event => { messages.push(event); return true; },
    emit: async event => { if (event.status === 'catchup_completed') controller.abort(); return true; },
  });
  assert.deepEqual(messages.map(event => event.logId), ['100']);
  assert.equal(state.snapshot()[0].afterLogId, '100');
});

test('an unchanged partial empty page neither commits a nonexistent anchor nor releases protection', async () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '99', limit: 50 }]);
  const result = await cycles({ state, pages: async () => ({ messages: [], complete: false }) });
  assert.equal(result.events.some(event => event.type === 'catchup_progress'), false);
  assert.equal(result.events.some(event => event.type === 'conversation_synced'), false);
  assert.equal(result.events.find(event => event.status === 'catchup_completed').partial, true);
  assert.equal(state.snapshot()[0].afterLogId, '99');
});

test('complete markers follow page bodies and progress, and never release partial rooms', async () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '99', limit: 50 },
    { chatId: '202', afterLogId: '99', limit: 50 }]);
  const ordered = [];
  const controller = new AbortController();
  await runConversationSync({ state, currentUserId: '123', signal: controller.signal, maxMessages: 2,
    client: { getMessagePage: async (chatId) => ({ messages: [body(chatId, 100)], complete: chatId === '101' }) },
    onMessage: async event => { ordered.push('body:' + event.chatId); return true; },
    emit: async event => {
      ordered.push(event.type + ':' + (event.chatId ?? ''));
      if (event.status === 'catchup_completed') controller.abort();
      return true;
    } });
  assert.ok(ordered.indexOf('body:101') < ordered.indexOf('catchup_progress:101'));
  assert.ok(ordered.indexOf('catchup_progress:101') < ordered.indexOf('conversation_synced:101'));
  assert.equal(ordered.includes('conversation_synced:202'), false);
});

test('full first-pass completion rotates the first room on the next cycle', async () => {
  const state = createConversationSyncState(['101', '202', '303'].map(chatId => ({ chatId, afterLogId: '99', limit: 50 })));
  const result = await cycles({ state, stopAfter: 2, pages: async () => ({ messages: [], complete: true }) });
  assert.deepEqual(result.calls.slice(0, 3).map(call => call.chatId), ['101', '202', '303']);
  assert.deepEqual(result.calls.slice(3, 6).map(call => call.chatId), ['202', '303', '101']);
});

test('eviction during a history call does not emit completion for a replaced room', async () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '99', limit: 50 }], 1);
  const result = await cycles({ state, pages: async () => {
    state.observe({ type: 'message', chatId: '202', logId: '10' });
    state.observe({ type: 'message', chatId: '101', logId: '102' });
    return { messages: [], complete: true };
  } });
  assert.equal(result.events.some(event => event.type === 'conversation_synced'), false);
  assert.equal(state.snapshot()[0].afterLogId, '101');
});

test('evicted room responses cannot advance a newly re-added room identity', () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '99', limit: 50 }], 1);
  const old = state.snapshot()[0];
  state.observe({ type: 'message', chatId: '202', logId: '50' });
  state.observe({ type: 'message', chatId: '101', logId: '102' });
  state.advance(old, '99999');
  assert.equal(state.snapshot()[0].afterLogId, '101');
});

test('malformed pages do not advance process-local cursor and expose a safe partial status', async () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '99', limit: 50 }]);
  const result = await cycles({ state, pages: async () => ({ messages: [{ chat_id: '999', log_id: '100' }], complete: true }) });
  assert.equal(state.snapshot()[0].afterLogId, '99');
  assert.equal(result.events.some(event => event.status === 'catchup_failed'), true);
  assert.equal(result.messages.length, 0);
});

test('abort interrupts a pending history call and removes all abort listeners', async () => {
  const state = createConversationSyncState([{ chatId: '101', afterLogId: '99', limit: 50 }]);
  const controller = new AbortController();
  let calls = 0;
  const client = { getMessagePage: () => { calls++; queueMicrotask(() => controller.abort()); return new Promise(() => {}); } };
  await runConversationSync({ state, client, signal: controller.signal, emit: async () => true, onMessage: async () => true });
  assert.equal(calls, 1);
  assert.equal(getEventListeners(controller.signal, 'abort').length, 0);
  assert.equal(state.snapshot()[0].afterLogId, '99');
});
