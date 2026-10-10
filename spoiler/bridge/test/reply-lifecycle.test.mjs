import assert from 'node:assert/strict';
import test from 'node:test';
import { PassThrough, Writable } from 'node:stream';
import { runBridge } from '../src/bridge.mjs';
import { parseJsonLine } from '../src/io.mjs';

test('the C# UTF8 BOM preamble is accepted without changing message text', () => {
  const line = '\ufeff{"type":"send_reply","text":"\ufeff 원문 "}';
  assert.equal(parseJsonLine(line).text, '\ufeff 원문 ');
  assert.throws(() => parseJsonLine('\ufeff\ufeff{}'), { code: 'invalid_json' });
});

async function lifecycle({ timeout = false, refresh = false, delayedPush = false } = {}) {
  const input = new PassThrough();
  const events = [];
  let writes = 0;
  let attempts = 0;
  let resolveMembers;
  class Client {
    async login(credentials) { this.credentials = credentials; this.connected = ++attempts > (refresh ? 1 : 0); }
    isConnected() { return this.connected; }
    acquireSession() { return Promise.resolve({ sendMessage: async (chatId, text) => {
      writes++; assert.equal(String(chatId), '101'); assert.equal(text, ' 직접 답장\n ');
      if (timeout) return new Promise(() => {});
      return { method: 'WRITE', statusCode: 0, body: { logId: '9007199254740993', sendAt: 1790733600, chatId } };
    } }); }
    close() { this.connected = false; }
    lookupAuthorName() { return delayedPush ? null : '나'; }
    getMembersByIds() { return new Promise(resolve => { resolveMembers = resolve; }); }
    onPush() { return () => {}; }
    onSessionEvent() { return () => {}; }
  }
  class Listener {
    constructor(client) { this.client = client; this.handlers = new Map(); }
    on(type, handler) { this.handlers.set(type, handler); }
    off(type) { this.handlers.delete(type); }
    async start() {
      assert.equal(this.client.sendMessage, undefined);
      assert.equal(this.client.markRead, undefined);
      assert.equal(this.client.getChatInfo, undefined);
      if (!this.client.isConnected()) this.handlers.get('error')({ code: 'invalid_access_token' });
      else this.handlers.get('connected')({ userId: '123' });
      if (delayedPush) this.handlers.get('message')({ chat_id: '101', log_id: '9007199254740992', author_id: 222,
        message: '이전 메시지', message_type: 1, sent_at: 1790733600 });
    }
    stop() {}
  }
  const output = new Writable({ write(chunk, encoding, done) {
    const frame = JSON.parse(String(chunk)); events.push(frame);
    if (frame.type === 'auth_lease_request') queueMicrotask(() => input.write(JSON.stringify({
      type: 'auth_lease_granted', request_id: frame.request_id, ok: true, auth_lease_id: 'lease-1', credentials: frame.credentials }) + '\n'));
    if (frame.type === 'credentials') queueMicrotask(() => input.write('{"type":"credentials_saved"}\n'));
    if (frame.type === 'auth_lease_release') queueMicrotask(() => input.write(JSON.stringify({
      type: 'auth_lease_released', request_id: frame.request_id, ok: true }) + '\n'));
    if (frame.type === 'status' && frame.status === 'ready') queueMicrotask(() => input.write(JSON.stringify({
      type: 'send_reply', request_id: 'explicit-1', chat_id: '101', user_id: '123',
      device_uuid: 'synthetic-device', text: ' 직접 답장\n ' }) + '\n'));
    if (frame.type === 'reply_result' && !timeout) queueMicrotask(() => {
      resolveMembers?.([{ user_id: '222', nickname: '테스트' }]); input.end();
    });
    done();
  } });
  input.write('\ufeff' + JSON.stringify({ oauthToken: 'synthetic-token', refreshToken: 'synthetic-refresh', userId: '123',
    deviceUuid: 'synthetic-device', deviceType: 'tablet' }) + '\n');
  const exitCode = await runBridge({ input, output, limits: { replyMs: 10 }, sdk: {
    KakaoTalkClient: Client, KakaoTalkListener: Listener,
    refreshKakaoOAuthToken: async () => ({ accessToken: 'synthetic-new', refreshToken: 'synthetic-next' }),
  } });
  return { events, writes, exitCode, attempts };
}

test('explicit control uses only one WRITE and publishes durable frame before ACK', async () => {
  const { events, writes, exitCode } = await lifecycle();
  assert.equal(writes, 1); assert.equal(exitCode, 0);
  const bodyIndex = events.findIndex(frame => frame.type === 'message');
  const ackIndex = events.findIndex(frame => frame.type === 'reply_result');
  assert.ok(bodyIndex > 0 && ackIndex > bodyIndex);
  assert.equal(events[ackIndex].status, 'sent');
});

test('command input waits for credential save ACK and auth lease release before reading', async () => {
  const { events, writes, attempts } = await lifecycle({ refresh: true });
  assert.equal(writes, 1); assert.equal(attempts, 2);
  assert.ok(events.findIndex(frame => frame.type === 'auth_lease_release') < events.findIndex(frame => frame.type === 'reply_result'));
});

test('a timed out WRITE closes the bridge without a replay or success', async () => {
  const { events, writes } = await lifecycle({ timeout: true });
  assert.equal(writes, 1);
  assert.equal(events.find(frame => frame.type === 'reply_result').status, 'unknown');
  assert.equal(events.some(frame => frame.type === 'message'), false);
  assert.equal(events.at(-1).status, 'stopped');
});

test('ACK that overtakes a previous push carries a no-cursor-advance marker', async () => {
  const { events } = await lifecycle({ delayedPush: true });
  const own = events.findIndex(frame => frame.type === 'message' && frame.isMine);
  const previous = events.findIndex(frame => frame.type === 'message' && !frame.isMine);
  assert.ok(own >= 0 && previous > own);
  assert.equal(events[own].replyAck, true);
  assert.equal(events[previous].replyAck, undefined);
});
