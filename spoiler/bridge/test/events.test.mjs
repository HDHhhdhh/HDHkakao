import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import test from 'node:test';
import { attachNormalizedEvents, normalizeListenerEvent } from '../src/events.mjs';

const turn = () => new Promise((resolve) => setImmediate(resolve));
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const message = (id, room = '101', author = 1, extra = {}) => ({
  chat_id: room, log_id: String(id), author_id: author, message_type: 1,
  message: `original-${id}`, sent_at: 1760000000000 + id, ...extra,
});

test('keeps only an actual supplied SDK room title', () => {
  const event = normalizeListenerEvent('message', message(1, '101', 1, {
    chat_title: ' 실제 단체방 ', author_name: '작성자', oauthToken: 'never-include',
  }));
  assert.equal(event.chatTitle, '실제 단체방');
  assert.equal(event.authorName, '작성자');
  assert.equal(Object.hasOwn(event, 'oauthToken'), false);
  for (const chat_title of [null, '', '  ', 12, { title: 'untrusted shape' }]) {
    assert.equal(Object.hasOwn(normalizeListenerEvent('message', message(2, '101', 1, { chat_title })), 'chatTitle'), false);
  }
});

test('provided titles avoid lookups and update the room cache for later messages', async () => {
  const listener = new EventEmitter();
  const events = [];
  let authorCalls = 0;
  let titleCalls = 0;
  const detach = attachNormalizedEvents(listener, (event) => events.push(event),
    async () => { authorCalls++; return 'unexpected author'; }, 1000, null, 1024,
    async () => { titleCalls++; return 'unexpected title'; });
  listener.emit('message', message(1, '101', 1, { chat_title: '첫 제목', author_name: '학생 A' }));
  await turn();
  listener.emit('message', message(2, '101', 2, { author_name: '학생 B' }));
  listener.emit('message', message(3, '101', 2, { chat_title: '변경된 제목', author_name: '학생 B' }));
  listener.emit('message', message(4, '101', 3, { author_name: '학생 C' }));
  await detach();
  assert.equal(authorCalls, 0);
  assert.equal(titleCalls, 0);
  assert.deepEqual(events.map((event) => event.chatTitle), ['첫 제목', '첫 제목', '변경된 제목', '변경된 제목']);
});

test('author and room title resolve together and a room shares one title lookup', async () => {
  const listener = new EventEmitter();
  const events = [];
  const authors = new Map();
  let titleCalls = 0;
  let finishTitle;
  const detach = attachNormalizedEvents(listener, (event) => events.push(event),
    (_room, author) => new Promise((resolve) => authors.set(author, resolve)), 1000, null, 1024,
    (room) => { assert.equal(room, '101'); titleCalls++; return new Promise((resolve) => { finishTitle = resolve; }); });
  listener.emit('message', message(1, '101', 1));
  listener.emit('message', message(2, '101', 2));
  await turn();
  assert.deepEqual([...authors.keys()], [1, 2]);
  assert.equal(titleCalls, 1);
  authors.get(2)('학생 B'); finishTitle('수학 단체방');
  await turn();
  assert.equal(events.length, 0);
  authors.get(1)('학생 A');
  await detach();
  assert.deepEqual(events.map((event) => event.logId), ['1', '2']);
  assert.deepEqual(events.map((event) => event.authorName), ['학생 A', '학생 B']);
  assert.ok(events.every((event) => event.chatTitle === '수학 단체방'));
});

test('room titles expire after sixty seconds without extending on cache hits', async () => {
  const listener = new EventEmitter();
  const events = [];
  const originalNow = Date.now;
  let now = 1000;
  let calls = 0;
  Date.now = () => now;
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), undefined, 1000, null, 1024,
    async () => `title-${++calls}`);
  try {
    listener.emit('message', message(1, '101', 1, { author_name: '학생' }));
    await turn();
    now = 60_999;
    listener.emit('message', message(2, '101', 1, { author_name: '학생' }));
    await turn();
    assert.equal(calls, 1);
    now = 61_000;
    listener.emit('message', message(3, '101', 1, { author_name: '학생' }));
    await detach();
    assert.equal(calls, 2);
    assert.deepEqual(events.map((event) => event.chatTitle), ['title-1', 'title-1', 'title-2']);
  } finally { Date.now = originalNow; await detach(); }
});

test('the room-title cache is bounded to 256 recently used rooms', async () => {
  const listener = new EventEmitter();
  const events = [];
  const calls = new Map();
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), undefined, 1000, null, 1024,
    async (room) => { calls.set(room, (calls.get(room) ?? 0) + 1); return `title-${room}`; });
  for (let room = 0; room < 257; room++) listener.emit('message', message(room + 1, String(1000 + room), 1, { author_name: '학생' }));
  await turn();
  assert.equal(calls.size, 257);
  listener.emit('message', message(258, '1001', 1, { author_name: '학생' }));
  await turn();
  listener.emit('message', message(259, '1000', 1, { author_name: '학생' }));
  await turn();
  listener.emit('message', message(260, '1002', 1, { author_name: '학생' }));
  await detach();
  assert.equal(calls.get('1001'), 1);
  assert.equal(calls.get('1000'), 2);
  assert.equal(calls.get('1002'), 2);
  assert.equal(events.length, 260);
});

test('hung title operations hold SDK slots after message deadlines expire', async () => {
  const listener = new EventEmitter();
  const events = [];
  const pending = new Map();
  const calls = [];
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), undefined, 20, null, 1024,
    (room) => { calls.push(room); return new Promise((resolve) => pending.set(room, resolve)); });
  for (let id = 1; id <= 100; id++) listener.emit('message', message(id, String(1000 + id), 1, { author_name: '학생' }));
  await turn();
  assert.deepEqual(calls, ['1001', '1002', '1003', '1004']);
  await delay(50);
  assert.equal(events.length, 100);
  assert.ok(events.every((event) => !Object.hasOwn(event, 'chatTitle')));
  listener.emit('message', message(101, '2001', 1, { author_name: '학생' }));
  await delay(50);
  assert.equal(events.length, 101);
  assert.equal(calls.length, 4);
  pending.get('1001')('늦게 확인된 제목');
  await turn();
  listener.emit('message', message(102, '1001', 1, { author_name: '학생' }));
  listener.emit('message', message(103, '2003', 1, { author_name: '학생' }));
  await turn();
  assert.deepEqual(calls, ['1001', '1002', '1003', '1004', '2003']);
  pending.get('2003')('새 방');
  await detach();
  assert.equal(events[0].chatTitle, undefined);
  assert.equal(events[101].chatTitle, '늦게 확인된 제목');
  assert.equal(events[102].chatTitle, '새 방');
});

test('author and title operations share the same four-slot pool', async () => {
  const listener = new EventEmitter();
  const events = [];
  const calls = [];
  const pending = [];
  const resolver = (kind) => (room) => {
    calls.push(`${kind}:${room}`);
    return new Promise((resolve) => pending.push(resolve));
  };
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), resolver('author'), 20, null, 1024, resolver('title'));
  for (let id = 1; id <= 20; id++) listener.emit('message', message(id, String(id), id));
  await turn();
  assert.deepEqual(calls, ['author:1', 'title:1', 'author:2', 'title:2']);
  await delay(50);
  assert.equal(events.length, 20);
  assert.equal(calls.length, 4);
  pending[0]('늦은 작성자');
  await turn();
  assert.equal(calls.length, 4);
  listener.emit('message', message(21, '21', 21, { author_name: '이미 확인된 이름' }));
  await turn();
  assert.equal(calls.at(-1), 'title:21');
  pending.at(-1)('새 제목');
  await detach();
  assert.equal(events.at(-1).chatTitle, '새 제목');
});

test('title failure never suppresses the body or successful author metadata', async () => {
  const listener = new EventEmitter();
  const events = [];
  let calls = 0;
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), async () => '학생', 1000, null, 1024,
    async () => { if (++calls === 1) throw new Error('synthetic title lookup failure'); return '복구된 제목'; });
  listener.emit('message', message(1));
  await turn();
  assert.equal(events.length, 1);
  assert.equal(events[0].authorName, '학생');
  assert.equal(events[0].message, 'original-1');
  assert.equal(events[0].chatTitle, undefined);
  listener.emit('message', message(2));
  await detach();
  assert.equal(events[1].chatTitle, '복구된 제목');
  assert.equal(events[1].message, 'original-2');
});

test('a newer supplied SDK title wins over an older in-flight lookup', async () => {
  const listener = new EventEmitter();
  const events = [];
  let finishTitle;
  let calls = 0;
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), undefined, 1000, null, 1024,
    () => { calls++; return new Promise((resolve) => { finishTitle = resolve; }); });
  listener.emit('message', message(1, '101', 1, { author_name: '학생 A' }));
  await turn();
  listener.emit('message', message(2, '101', 2, { author_name: '학생 B', chat_title: '새 제목' }));
  finishTitle('오래된 제목');
  await turn();
  listener.emit('message', message(3, '101', 3, { author_name: '학생 C' }));
  await detach();
  assert.equal(calls, 1);
  assert.ok(events.every((event) => event.chatTitle === '새 제목'));
});

test('read-only title enrichment drains accepted messages without opening or marking a room', async () => {
  const listener = new EventEmitter();
  const events = [];
  const writes = [];
  for (const method of ['markRead', 'sendMessage', 'sendTyping', 'openChat']) {
    listener[method] = () => { writes.push(method); throw new Error('forbidden write/open call'); };
  }
  let finishTitle;
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), undefined, 1000, '9', 1024,
    () => new Promise((resolve) => { finishTitle = resolve; }));
  listener.emit('message', message(1, '101', 1, { author_name: '학생' }));
  await turn();
  const drained = detach();
  listener.emit('message', message(2, '101', 1, { author_name: '학생' }));
  finishTitle('수업 대화방');
  await drained;
  assert.deepEqual(writes, []);
  assert.equal(events.length, 1);
  assert.equal(events[0].chatTitle, '수업 대화방');
  assert.equal(events[0].message, 'original-1');
  assert.equal(events[0].isMine, false);
});
