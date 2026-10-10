import assert from 'node:assert/strict';
import test from 'node:test';
import {
  DEFAULT_CATCHUP_LIMIT,
  MAX_CATCHUP_MESSAGES_PER_CHAT,
  MAX_CATCHUP_ROOMS,
  createMessageDeduper,
  dedupeAndOrderMessages,
  fetchReadOnlyCatchUp,
  normalizeCatchUpMessage,
  sanitizeCatchUpCursors,
} from '../src/catchup.mjs';

test('sanitizes, bounds, and merges reconnect cursors by chat', () => {
  const cursors = sanitizeCatchUpCursors([
    { chatId: 'room-1', afterLogId: '100', limit: 10, ignored: 'drop-me' },
    { chat_id: 'room-1', after_log_id: '105', count: 25 },
    { chatId: 'room-2' },
  ]);

  assert.deepEqual(cursors, [
    { chatId: 'room-1', afterLogId: '105', limit: 25 },
    { chatId: 'room-2', afterLogId: '0', limit: DEFAULT_CATCHUP_LIMIT },
  ]);
  assert.throws(() => sanitizeCatchUpCursors(new Array(MAX_CATCHUP_ROOMS + 1).fill({ chatId: 'room' })), {
    code: 'invalid_catchup_request',
  });
  assert.throws(() => sanitizeCatchUpCursors([{ chatId: 'room', afterLogId: '1', limit: MAX_CATCHUP_MESSAGES_PER_CHAT + 1 }]), {
    code: 'invalid_catchup_request',
  });
  assert.throws(() => sanitizeCatchUpCursors([{ chatId: 'room', afterLogId: 'not valid / cursor' }]), {
    code: 'invalid_catchup_request',
  });
});

test('normalizes a full SDK message while retaining long text and safe metadata', () => {
  const message = '긴 메시지 '.repeat(300) + '마지막 줄까지 보존';
  const event = normalizeCatchUpMessage({
    chatId: 'room-7',
    logId: '987654',
    authorId: '42',
    type: 1,
    message,
    sentAt: 1760000000000,
  }, '42', { chatTitle: '내가 지정한 방 이름' });

  assert.equal(event.type, 'message');
  assert.equal(event.chatId, 'room-7');
  assert.equal(event.logId, '987654');
  assert.equal(event.message, message);
  assert.equal(event.message.endsWith('마지막 줄까지 보존'), true);
  assert.equal(event.messageType, 1);
  assert.equal(event.chatTitle, '내가 지정한 방 이름');
  assert.equal(event.isMine, true);
  assert.equal(event.recovered, true);
});

test('deduplicates by chat/log identity and orders chronologically', () => {
  const messages = [
    { type: 'message', chatId: 'room', logId: '12', sentAt: 300, message: '세 번째' },
    { type: 'message', chatId: 'room', logId: '10', sentAt: 100, message: '첫 번째' },
    { type: 'message', chatId: 'room', logId: '10', sentAt: 100, message: '중복' },
    { type: 'message', chatId: 'room', logId: '11', sentAt: 200, message: '두 번째' },
  ];
  const deduper = createMessageDeduper();
  assert.deepEqual(dedupeAndOrderMessages(messages, deduper).map((item) => item.logId), ['10', '11', '12']);
  assert.equal(deduper.size, 3);
});

test('fetches one bounded forward page without exposing write methods', async () => {
  const calls = [];
  const forbiddenCalls = [];
  const client = {
    async getMessagePage(chatId, options) {
      calls.push({ chatId, options });
      return {
        messages: [
          { log_id: '203', type: 1, author_id: 7, message: '뒤', sent_at: 300 },
          { log_id: '201', type: 1, author_id: 7, message: '앞', sent_at: 100 },
          { log_id: '201', type: 1, author_id: 7, message: '중복', sent_at: 100 },
        ],
        next_cursor: '203',
        complete: false,
      };
    },
    async markRead() { forbiddenCalls.push('markRead'); },
    async sendMessage() { forbiddenCalls.push('sendMessage'); },
    async sendTyping() { forbiddenCalls.push('sendTyping'); },
    async getChatInfo() { forbiddenCalls.push('getChatInfo'); },
    async openChat() { forbiddenCalls.push('openChat'); },
  };

  const result = await fetchReadOnlyCatchUp({
    client,
    cursors: [{ chatId: 'room-2', afterLogId: '200', limit: 3 }],
    currentUserId: '42',
    resolveAuthorName: async () => '상대 이름',
    resolveChatTitle: async () => '테스트 방',
  });

  assert.deepEqual(calls, [{ chatId: 'room-2', options: { count: 3, from: '200' } }]);
  assert.deepEqual(result.messages.map((item) => item.logId), ['201', '203']);
  assert.deepEqual(result.messages.map((item) => item.authorName), ['상대 이름', '상대 이름']);
  assert.deepEqual(result.messages.map((item) => item.chatTitle), ['테스트 방', '테스트 방']);
  assert.equal(result.partial, true);
  assert.deepEqual(result.nextCursors, [{ chatId: 'room-2', afterLogId: '203' }]);
  assert.deepEqual(forbiddenCalls, []);
});

test('rejects newest-window getMessages fallback without invoking it', async () => {
  const pageCalls = [];
  const client = {
    async getMessages(chatId, options) {
      pageCalls.push({ chatId, options });
      return [
        { log_id: '301', type: 1, author_id: 1, message: '첫 페이지', sent_at: 1 },
        { log_id: '302', type: 1, author_id: 1, message: '둘째 페이지', sent_at: 2 },
      ];
    },
  };
  await assert.rejects(fetchReadOnlyCatchUp({
    client,
    cursors: [{ chatId: 'room-3', afterLogId: '300', limit: 2 }],
  }), { code: 'catchup_unavailable' });
  assert.deepEqual(pageCalls, []);
});

test('keeps successful rooms when one reconnect cursor fails', async () => {
  const calls = [];
  const client = {
    async getMessagePage(chatId, options) {
      calls.push(chatId);
      if (chatId === 'room-b') throw new Error('room unavailable');
      return {
        messages: [{
          log_id: chatId === 'room-a' ? '401' : '403',
          type: 1,
          author_id: 7,
          message: chatId,
          sent_at: chatId === 'room-a' ? 1 : 3,
        }],
        next_cursor: chatId === 'room-a' ? '401' : '403',
        complete: true,
      };
    },
  };

  const result = await fetchReadOnlyCatchUp({
    client,
    cursors: [
      { chatId: 'room-a', afterLogId: '400', limit: 5 },
      { chatId: 'room-b', afterLogId: '400', limit: 5 },
      { chatId: 'room-c', afterLogId: '400', limit: 5 },
    ],
  });

  assert.deepEqual(calls, ['room-a', 'room-b', 'room-c']);
  assert.deepEqual(result.messages.map((item) => item.chatId), ['room-a', 'room-c']);
  assert.equal(result.partial, true);
  assert.equal(result.failedRooms, 1);
  assert.deepEqual(result.nextCursors, [
    { chatId: 'room-a', afterLogId: '401' },
    { chatId: 'room-c', afterLogId: '403' },
  ]);
});

test('fails only when every requested room fails', async () => {
  await assert.rejects(
    fetchReadOnlyCatchUp({
      client: { getMessagePage: async () => { throw new Error('unavailable'); } },
      cursors: [{ chatId: 'room-a', afterLogId: '1' }],
    }),
    { code: 'catchup_failed' },
  );
});
