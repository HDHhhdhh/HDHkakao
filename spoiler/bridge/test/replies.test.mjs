import assert from 'node:assert/strict';
import test from 'node:test';
import Long from 'long';
import { positiveId, parseReplyCommand, validateReplyPacket, executeReplyCommand } from '../src/replies.mjs';

const credentials = { userId: '123', deviceUuid: 'synthetic-device' };
const command = { type: 'send_reply', request_id: 'request-1', user_id: '123', device_uuid: 'synthetic-device',
  chat_id: '9223372036854775807', text: '  원문\n그대로 😄  ' };
const packet = () => ({ method: 'WRITE', statusCode: 0,
  body: { logId: Long.fromString('9007199254740993'), sendAt: 1790733600, chatId: Long.fromString(command.chat_id) } });

async function run(value = command, response = packet(), settings = {}) {
  const events = [];
  let writes = 0;
  const client = {
    isConnected: () => true,
    acquireSession: async () => ({ sendMessage: async (chatId, text) => {
      writes++; assert.equal(String(chatId), command.chat_id); assert.equal(text, command.text);
      if (response instanceof Error) throw response;
      if (typeof response === 'function') return response();
      return response;
    } }),
    sendMessage: () => { throw Error('High-level replaying send must not be used'); },
    markRead: () => { throw Error('Read watermark must not change'); },
    getChatInfo: () => { throw Error('Must not open room'); },
  };
  const result = await executeReplyCommand({ value, credentials, client, signal: new AbortController().signal,
    emit: async (event) => { events.push(event); return true; },
    emitMessage: async (event) => { events.push(event); return true; }, seenRequests: new Set(), ...settings });
  return { result, events, writes };
}

test('canonical signed64 IDs preserve precision and reject malformed identities', () => {
  assert.equal(positiveId(Long.fromString(command.chat_id)), command.chat_id);
  assert.equal(positiveId('9007199254740993'), '9007199254740993');
  for (const id of [0, -1, '0', '01', '+1', ' 1', '1 ', '9223372036854775808', '1.0', 9007199254740992, null, {}])
    assert.equal(positiveId(id), null);
  assert.equal(parseReplyCommand(command, credentials).text, command.text);
  for (const change of [{ type: 'message' }, { request_id: 'bad\nrequest' }, { chat_id: 123 }, { text: ' ' },
    { text: 'a'.repeat(4001) }, { user_id: 'other' }, { device_uuid: 'other' }])
    assert.throws(() => parseReplyCommand({ ...command, ...change }, credentials));
});

test('valid server receipt publishes own body before success without read/open/high-level send', async () => {
  const { result, events, writes } = await run();
  assert.equal(writes, 1);
  assert.equal(result.status, 'sent');
  assert.equal(result.log_id, '9007199254740993');
  assert.equal(events.length, 2);
  assert.deepEqual(events[0], { type: 'message', chatId: command.chat_id, logId: '9007199254740993',
    authorId: '123', authorName: '나', message: command.text, messageType: 1, sentAt: 1790733600, isMine: true, replyAck: true });
  assert.equal(events[1].type, 'reply_result');
});

test('provider rejections are explicit failures; zero transport status alone is never success', async (t) => {
  for (const value of [{ method: 'WRITE', statusCode: 7, body: {} },
    { method: 'WRITE', statusCode: 0, body: { status: 4 } }]) {
    await t.test(JSON.stringify(value), async () => {
      const { result, events, writes } = await run(command, value);
      assert.equal(writes, 1); assert.equal(result.status, 'failed'); assert.equal(result.code, 'reply_rejected');
      assert.equal(events.some((event) => event.type === 'message'), false);
    });
  }
});

test('malformed or missing ACK stays unknown and never retries WRITE', async (t) => {
  const values = [null, {}, { ...packet(), method: 'NOTIREAD' }, { ...packet(), statusCode: -1 },
    { ...packet(), body: {} }, { ...packet(), body: { ...packet().body, status: -1 } },
    { ...packet(), body: { ...packet().body, status: '0' } },
    { ...packet(), body: { ...packet().body, error: 'provider-detail' } },
    { ...packet(), body: { ...packet().body, chatId: '999' } },
    { ...packet(), body: { ...packet().body, logId: '0' } },
    { ...packet(), body: { ...packet().body, sendAt: 0 } },
    { ...packet(), body: { ...packet().body, sendAt: '1790733600' } }, new Error('secret upstream text')];
  for (let index = 0; index < values.length; index++) await t.test(String(index), async () => {
    const { result, events, writes } = await run(command, values[index]);
    assert.equal(writes, 1); assert.equal(result.status, 'unknown');
    assert.equal(events.some((event) => event.type === 'message'), false);
    assert.equal(JSON.stringify(result).includes('secret upstream text'), false);
  });
});

test('timeout and cancellation after WRITE are unknown and execute exactly once', async () => {
  const { result, writes } = await run(command, () => new Promise(() => {}), { timeoutMs: 10 });
  assert.equal(result.status, 'unknown'); assert.equal(writes, 1);
  const aborted = new AbortController(); aborted.abort();
  const before = await run(command, packet(), { signal: aborted.signal });
  assert.equal(before.writes, 0); assert.equal(before.result.status, 'failed');
});

test('foreign identity, disconnected session and duplicate request IDs never write', async () => {
  const mismatch = await run({ ...command, user_id: '456' });
  assert.equal(mismatch.writes, 0); assert.equal(mismatch.result.code, 'reply_identity_changed');
  const disconnected = await run(command, packet(), { client: { isConnected: () => false } });
  assert.equal(disconnected.writes, 0); assert.equal(disconnected.result.code, 'reply_disconnected');
  const duplicate = await run(command, packet(), { seenRequests: new Set(['request-1']) });
  assert.equal(duplicate.writes, 0); assert.equal(duplicate.result.status, 'unknown');
});

test('raw receipt parser rejects body errors and signed64 overflows', () => {
  assert.equal(validateReplyPacket(packet(), command.chat_id).logId, '9007199254740993');
  assert.throws(() => validateReplyPacket({ ...packet(), body: { ...packet().body, logId: '9223372036854775808' } }, command.chat_id));
});
