import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import test from 'node:test';
import { attachNormalizedEvents } from '../src/events.mjs';

const turn = () => new Promise((resolve) => setImmediate(resolve));
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const message = (id, author = 1, room = 'room', text = `body-${id}`) => ({
  chat_id: room, log_id: String(id), author_id: author,
  message: text, message_type: 1, sent_at: 1760000000000 + id,
});

test('a burst of 100 messages shares one in-flight room/author lookup', async () => {
  const listener = new EventEmitter();
  const events = [];
  let calls = 0;
  let finish;
  const detach = attachNormalizedEvents(listener, (event) => events.push(event),
    () => { calls += 1; return new Promise((resolve) => { finish = resolve; }); }, 1000);
  for (let i = 1; i <= 100; i += 1) listener.emit('message', message(i));
  await turn();
  assert.equal(calls, 1);
  finish('shared-name');
  await detach();
  assert.equal(events.length, 100);
  assert.deepEqual(events.map((event) => event.logId), Array.from({ length: 100 }, (_, i) => String(i + 1)));
  assert.ok(events.every((event) => event.authorName === 'shared-name'));
});

test('distinct author lookups run at most four at once and preserve message order', async () => {
  const listener = new EventEmitter();
  const events = [];
  const pending = new Map();
  let active = 0;
  let peak = 0;
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), (_room, author) => {
    active += 1;
    peak = Math.max(peak, active);
    return new Promise((resolve) => pending.set(author, (value) => { active -= 1; resolve(value); }));
  }, 1000);
  for (let i = 1; i <= 9; i += 1) listener.emit('message', message(i, i));
  await turn();
  assert.deepEqual([...pending.keys()], [1, 2, 3, 4]);
  pending.get(2)('name-2');
  await turn();
  assert.ok(pending.has(5));
  assert.equal(events.length, 0);
  for (const author of [1, 3, 4, 5]) pending.get(author)(`name-${author}`);
  await turn();
  assert.deepEqual([...pending.keys()], [1, 2, 3, 4, 5, 6, 7, 8, 9]);
  for (const author of [6, 7, 8, 9]) pending.get(author)(`name-${author}`);
  await detach();
  assert.equal(peak, 4);
  assert.equal(active, 0);
  assert.deepEqual(events.map((event) => event.authorId), Array.from({ length: 9 }, (_, i) => i + 1));
  assert.ok(events.every((event) => event.authorName === `name-${event.authorId}`));
});

test('same author in separate rooms is isolated and settled names are not cached', async () => {
  const listener = new EventEmitter();
  const events = [];
  const calls = [];
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), async (room) => {
    calls.push(room);
    return `${room}-${calls.length}`;
  }, 1000);
  listener.emit('message', message(1, 1, 'room-a'));
  listener.emit('message', message(2, 1, 'room-b'));
  await turn();
  listener.emit('message', message(3, 1, 'room-a'));
  await detach();
  assert.deepEqual(calls, ['room-a', 'room-b', 'room-a']);
  assert.deepEqual(events.map((event) => event.authorName), ['room-a-1', 'room-b-2', 'room-a-3']);
});

test('hung lookups retain all four SDK slots while queued bodies expire together', async () => {
  const listener = new EventEmitter();
  const events = [];
  const pending = new Map();
  const calls = [];
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), (_room, author) => {
    calls.push(author);
    return new Promise((resolve) => pending.set(author, resolve));
  }, 20);
  for (let i = 1; i <= 100; i += 1) listener.emit('message', message(i, i));
  await turn();
  assert.deepEqual(calls, [1, 2, 3, 4]);
  await delay(40);
  assert.equal(events.length, 100);
  assert.ok(events.every((event) => event.authorName === null));
  listener.emit('message', message(101, 1));
  listener.emit('message', message(102, 102));
  await delay(40);
  assert.equal(events.length, 102);
  assert.deepEqual(calls, [1, 2, 3, 4]);
  pending.get(1)('late-name');
  await turn();
  assert.deepEqual(calls, [1, 2, 3, 4]);
  assert.equal(events[0].authorName, null);
  listener.emit('message', message(103, 103));
  await turn();
  assert.deepEqual(calls, [1, 2, 3, 4, 103]);
  pending.get(103)('fresh-name');
  await detach();
  assert.equal(events.at(-1).authorName, 'fresh-name');
  assert.deepEqual(events.map((event) => event.logId), Array.from({ length: 103 }, (_, i) => String(i + 1)));
});

test('shared lookup rejection delivers every body then permits a fresh lookup', async () => {
  const listener = new EventEmitter();
  const events = [];
  let calls = 0;
  let fail;
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), () => {
    calls += 1;
    return calls === 1 ? new Promise((_resolve, reject) => { fail = reject; }) : Promise.resolve('recovered-name');
  }, 1000);
  listener.emit('message', message(1));
  listener.emit('message', message(2));
  await turn();
  fail(new Error('synthetic rejection'));
  await turn();
  listener.emit('message', message(3));
  await detach();
  assert.equal(calls, 2);
  assert.deepEqual(events.map((event) => event.authorName), [null, null, 'recovered-name']);
  assert.deepEqual(events.map((event) => event.message), ['body-1', 'body-2', 'body-3']);
});

test('detach drains accepted queued bodies and rejects events received after detach', async () => {
  const listener = new EventEmitter();
  const events = [];
  let calls = 0;
  const detach = attachNormalizedEvents(listener, (event) => events.push(event),
    () => { calls += 1; return new Promise(() => {}); }, 20);
  for (let i = 1; i <= 12; i += 1) listener.emit('message', message(i, i));
  const drained = detach();
  listener.emit('message', message(13, 13));
  await drained;
  assert.equal(calls, 4);
  assert.deepEqual(events.map((event) => event.logId), Array.from({ length: 12 }, (_, i) => String(i + 1)));
});

test('the 32 MiB message guard still rejects overflow and drains accepted bodies', async () => {
  const listener = new EventEmitter();
  const events = [];
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), () => new Promise(() => {}), 20);
  const body = 'x'.repeat(8 * 1024 * 1024);
  for (let i = 1; i <= 5; i += 1) listener.emit('message', message(i, i, 'room', body));
  await detach();
  assert.equal(events.filter((event) => event.code === 'event_queue_full').length, 1);
  assert.equal(events.filter((event) => event.type === 'message').length, 4);
});
