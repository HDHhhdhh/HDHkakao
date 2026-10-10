import assert from 'node:assert/strict';
import test from 'node:test';
import { attachNormalizedEvents, normalizeListenerEvent } from '../src/events.mjs';

class FakeListener {
  #handlers = new Map();

  on(kind, handler) {
    const handlers = this.#handlers.get(kind) ?? new Set();
    handlers.add(handler);
    this.#handlers.set(kind, handlers);
    return this;
  }

  off(kind, handler) {
    this.#handlers.get(kind)?.delete(handler);
    return this;
  }

  emit(kind, payload) {
    for (const handler of this.#handlers.get(kind) ?? []) handler(payload);
  }
}

test('normalizes a fake SDK message into the stable NDJSON contract', async () => {
  const fake = new FakeListener();
  const events = [];
  const detach = attachNormalizedEvents(fake, (event) => events.push(event), undefined, 1200, '42');

  fake.emit('message', {
    type: 'MSG',
    chat_id: '123456',
    log_id: '987654',
    author_id: 42,
    author_name: '테스터',
    message: '팝업 없이 도착한 테스트 메시지',
    message_type: 1,
    attachment: { k: 'value', w: 100 },
    sent_at: 1760000000000,
  });

  await detach();

  assert.deepEqual(events, [{
    type: 'message',
    chatId: '123456',
    logId: '987654',
    authorId: 42,
    authorName: '테스터',
    message: '팝업 없이 도착한 테스트 메시지',
    messageType: 1,
    sentAt: 1760000000000,
    isMine: true,
  }]);

  fake.emit('message', {
    chat_id: '123456',
    log_id: '987655',
    author_id: 42,
    message: 'detached',
    message_type: 1,
    sent_at: 1760000000001,
  });
  assert.equal(events.length, 1);
});

test('normalizes lifecycle status and replaces upstream errors with fixed text', () => {
  assert.deepEqual(normalizeListenerEvent('connected', { userId: '42' }), {
    type: 'status',
    status: 'connected',
    userId: '42',
  });
  assert.deepEqual(normalizeListenerEvent('disconnected'), {
    type: 'status',
    status: 'disconnected',
  });
  assert.deepEqual(normalizeListenerEvent('error', {
    code: 'session_failed',
    message: 'oauthToken=secret-value password=do-not-print',
  }), {
    type: 'status',
    status: 'error',
    code: 'connection_error',
    message: '카카오톡 연결에 오류가 발생했습니다.',
  });
});

test('drops malformed and generic SDK packets instead of leaking raw protocol fields', () => {
  assert.equal(normalizeListenerEvent('message', {
    chat_id: 'room',
    log_id: 'log',
    author_id: 1,
    message_type: 1,
    message: 'missing timestamp',
  }), null);
  assert.equal(normalizeListenerEvent('kakaotalk_event', { type: 'MSG', oauthToken: 'secret' }), null);
});

test('marks only messages authored by the registered account as mine', () => {
  const mine = normalizeListenerEvent('message', {
    chat_id: 'room',
    log_id: 'mine',
    author_id: 42,
    message_type: 1,
    message: '내가 보낸 메시지',
    sent_at: 1760000000002,
  }, '42');
  const other = normalizeListenerEvent('message', {
    chat_id: 'room',
    log_id: 'other',
    author_id: 43,
    message_type: 1,
    message: '상대가 보낸 메시지',
    sent_at: 1760000000003,
  }, '42');

  assert.equal(mine?.isMine, true);
  assert.equal(other?.isMine, false);
});

test('fills a missing author name through the read-only member resolver', async () => {
  const fake = new FakeListener();
  const events = [];
  const calls = [];
  const detach = attachNormalizedEvents(
    fake,
    (event) => events.push(event),
    async (chatId, authorId) => {
      calls.push({ chatId, authorId });
      return '내 계정에 보이는 이름';
    },
  );

  fake.emit('message', {
    chat_id: 'room-7',
    log_id: 'log-7',
    author_id: 77,
    author_name: null,
    message: '이름 보완 테스트',
    message_type: 1,
    sent_at: 1760000000007,
  });
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(calls, [{ chatId: 'room-7', authorId: 77 }]);
  assert.equal(events.length, 1);
  assert.equal(events[0].authorName, '내 계정에 보이는 이름');
  detach();
});

test('keeps the original message when member lookup rejects', async () => {
  const fake = new FakeListener();
  const events = [];
  const detach = attachNormalizedEvents(
    fake,
    (event) => events.push(event),
    async () => { throw new Error('member lookup failed'); },
  );

  fake.emit('message', {
    chat_id: 'room-reject',
    log_id: 'log-reject',
    author_id: 81,
    message: '이 메시지는 없어지면 안 돼요',
    message_type: 1,
    sent_at: 1760000000081,
  });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(events.length, 1);
  assert.equal(events[0].message, '이 메시지는 없어지면 안 돼요');
  assert.equal(events[0].authorName, null);
  detach();
});

test('times out a hanging member lookup without suppressing the message', async () => {
  const fake = new FakeListener();
  const events = [];
  const detach = attachNormalizedEvents(
    fake,
    (event) => events.push(event),
    () => new Promise(() => {}),
    15,
  );

  fake.emit('message', {
    chat_id: 'room-timeout',
    log_id: 'log-timeout',
    author_id: 82,
    message: '조회 시간 초과 뒤에도 표시',
    message_type: 1,
    sent_at: 1760000000082,
  });
  await new Promise((resolve) => setTimeout(resolve, 30));

  assert.equal(events.length, 1);
  assert.equal(events[0].message, '조회 시간 초과 뒤에도 표시');
  detach();
});

test('resolves names concurrently while preserving receive order', async () => {
  const fake = new FakeListener();
  const events = [];
  const resolvers = new Map();
  const detach = attachNormalizedEvents(
    fake,
    (event) => events.push(event),
    (chatId, authorId) => new Promise((resolve) => resolvers.set(authorId, resolve)),
    100,
  );

  fake.emit('message', {
    chat_id: 'room-order', log_id: 'first', author_id: 1,
    message: '첫 번째', message_type: 1, sent_at: 1760000000101,
  });
  fake.emit('message', {
    chat_id: 'room-order', log_id: 'second', author_id: 2,
    message: '두 번째', message_type: 1, sent_at: 1760000000102,
  });
  fake.emit('message', {
    chat_id: 'room-order', log_id: 'third', author_id: 3,
    author_name: '이미 아는 이름', message: '세 번째', message_type: 1, sent_at: 1760000000103,
  });
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual([...resolvers.keys()], [1, 2]);
  resolvers.get(2)('둘');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(events.length, 0);
  resolvers.get(1)('하나');
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(events.map((event) => event.logId), ['first', 'second', 'third']);
  assert.deepEqual(events.map((event) => event.authorName), ['하나', '둘', '이미 아는 이름']);
  detach();
});

test('detach drains a message already accepted while its name is pending', async () => {
  const fake = new FakeListener();
  const events = [];
  let finishLookup;
  const detach = attachNormalizedEvents(
    fake,
    (event) => events.push(event),
    () => new Promise((resolve) => { finishLookup = resolve; }),
    100,
  );

  fake.emit('message', {
    chat_id: 'room-detach', log_id: 'before-detach', author_id: 91,
    message: '연결 종료 직전 수신', message_type: 1, sent_at: 1760000000191,
  });
  await new Promise((resolve) => setImmediate(resolve));
  const drained = detach();

  fake.emit('message', {
    chat_id: 'room-detach', log_id: 'after-detach', author_id: 92,
    message: '해제 뒤 이벤트', message_type: 1, sent_at: 1760000000192,
  });
  finishLookup('종료 전 이름');
  await drained;

  assert.equal(events.length, 1);
  assert.equal(events[0].logId, 'before-detach');
  assert.equal(events[0].authorName, '종료 전 이름');
});
