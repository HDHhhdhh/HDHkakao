import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { PassThrough, Writable } from 'node:stream';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { KakaoTalkClient } from 'agent-messenger/kakaotalk';
import { runAuth } from '../src/auth.mjs';
import { runBridge } from '../src/bridge.mjs';
import { fetchReadOnlyCatchUp } from '../src/catchup.mjs';
import { attachNormalizedEvents, normalizeListenerEvent } from '../src/events.mjs';
import { createNdjsonEmitter, safeError } from '../src/io.mjs';
import { installSessionAdoptionBuffer } from '../src/session-adapter.mjs';

const fixture = fileURLToPath(new URL('./fixtures/runtime-child.mjs', import.meta.url));
const start = { oauthToken: 'fixture-token', refreshToken: 'fixture-refresh', userId: '1', deviceUuid: 'fixture-device', deviceType: 'tablet' };
const auth = { email: 'fixture@example.test', password: 'fixture-password', deviceType: 'tablet' };
const history = (id, room = undefined) => ({ log_id: String(id), chat_id: room, type: 1, author_id: 2, author_name: 'fixture', message: `m${id}`, sent_at: id });
const liveSession = () => ({ connection: { socket: { destroyed: false, readable: true, writable: true, readableEnded: false, writableEnded: false, connecting: false } }, close() { this.connection.socket.destroyed = true; } });
function capture(onEvent) {
  const events = [];
  const output = new Writable({ write(chunk, _encoding, callback) { const event = JSON.parse(String(chunk)); events.push(event); onEvent?.(event); callback(); } });
  return { events, output };
}
async function childProbe(mode, request, onEvent, entryPoint = fixture) {
  const child = spawn(process.execPath, [entryPoint, mode], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  const events = [];
  let buffer = '';
  let stderr = '';
  child.stdout.on('data', (chunk) => {
    buffer += chunk;
    let split;
    while ((split = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, split); buffer = buffer.slice(split + 1);
      if (line) { const event = JSON.parse(line); events.push(event); onEvent?.(event, child); }
    }
  });
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  child.stdin.on('error', () => {});
  child.stdin.write(`${JSON.stringify(request)}\n`);
  let timer;
  const result = await Promise.race([
    new Promise((resolve, reject) => { child.once('exit', (code) => resolve(code)); child.once('error', reject); }),
    new Promise((_, reject) => { timer = setTimeout(() => { child.kill(); reject(new Error(`child ${mode} did not exit with parent stdin left open`)); }, 2500); }),
  ]).finally(() => clearTimeout(timer));
  assert.equal(stderr, '');
  return { code: result, events };
}

test('real child terminates on disconnect with parent stdin left open', async () => {
  const result = await childProbe('disconnect', start);
  assert.equal(result.code, 1);
  assert.equal(result.events.at(-1).status, 'stopped');
  assert.ok(result.events.some((event) => event.status === 'disconnected'));
});
test('real one-shot auth children exit needs_force and error with stdin open', async () => {
  assert.equal((await childProbe('auth-force', auth)).code, 2);
  const failed = await childProbe('auth-error', auth);
  assert.equal(failed.code, 1);
  assert.doesNotMatch(JSON.stringify(failed.events), /FIXTURE_SECRET|FIXTURE_TOKEN/);
});
test('real children enforce login, auth, and credential ACK deadlines', async () => {
  for (const [mode, request, code] of [
    ['login-timeout', start, 'session_timeout'], ['auth-timeout', auth, 'auth_timeout'], ['ack-timeout', start, 'credentials_ack_timeout'],
  ]) {
    const result = await childProbe(mode, request, (event, child) => {
      if (event.type === 'auth_lease_request') child.stdin.write(JSON.stringify({ type: 'auth_lease_granted',
        request_id: event.request_id, ok: true, auth_lease_id: 'fixture-lease', credentials: event.credentials }) + '\n');
      if (event.type === 'auth_lease_release') child.stdin.write(JSON.stringify({ type: 'auth_lease_released',
        request_id: event.request_id, ok: true }) + '\n');
      // Intentionally no credentials_saved ACK: keep the original deadline test.
    });
    assert.equal(result.code, 1, mode);
    assert.ok(result.events.some((event) => event.code === code), JSON.stringify(result.events));
  }
});
test('history timeout does not stop listener heartbeat and host EOF cancels promptly', async () => {
  const result = await childProbe('history-timeout', { ...start, catchUp: [{ chatId: 'room', afterLogId: '1' }] }, (event, child) => {
    if (event.status === 'catchup_failed') child.stdin.end();
  });
  assert.equal(result.code, 0);
  assert.ok(result.events.some((event) => event.type === 'heartbeat'));
});
test('allowlist removes JSON, bearer, token-shaped code and unknown upstream text', () => {
  for (const text of ['access_token=FIXTURE_SECRET', '{"password":"FIXTURE_SECRET","refresh_token":"FIXTURE_SECRET"}', 'Authorization: Bearer FIXTURE_SECRET']) {
    const result = safeError({ code: 'FIXTURE_TOKEN', message: text });
    assert.deepEqual(result, { code: 'bridge_error', message: '연결을 처리하지 못했습니다.' });
    for (const [code, message] of [
      ['auth_lease_busy', '다른 앱에서 인증 중이에요. 잠시 후 다시 시도해 주세요.'],
      ['auth_lease_invalid', '인증 갱신 조율을 확인하지 못했어요. 두 앱을 업데이트한 뒤 다시 연결해 주세요.'],
      ['auth_identity_changed', '다른 앱에서 계정 또는 기기 정보가 바뀌었어요. 최신 정보로 다시 연결해 주세요.'],
    ]) {
      const recognized = safeError({ code, message: text });
      assert.deepEqual(recognized, { code, message });
      assert.doesNotMatch(JSON.stringify(recognized), /FIXTURE_SECRET|FIXTURE_TOKEN/);
    }
  }
});
test('auth cancellation suppresses late passcode output', async () => {
  const input = new PassThrough(); input.end(`${JSON.stringify(auth)}\n`);
  const controller = new AbortController(); const captured = capture(); let callback;
  const running = runAuth({ input, output: captured.output, signal: controller.signal, sdk: {
    generateDeviceUuid: () => 'fixture-device', loginFlow: async (options) => { callback = options.onPasscodeDisplay; controller.abort(); return new Promise(() => {}); },
  } });
  assert.equal(await running, 1);
  callback('1234');
  assert.equal(captured.events.some((event) => event.type === 'passcode'), false);
});
test('slow output honors byte bound and flush timeout', async () => {
  const output = new Writable({ write() {} });
  const emit = createNdjsonEmitter(output, { timeoutMs: 25, maxQueuedBytes: 100 });
  emit({ message: 'a'.repeat(30) }); emit({ message: 'b'.repeat(70) });
  assert.equal((await emit.failed).code, 'output_backpressure');
  await assert.rejects(emit.flush()); emit.close(); output.destroy();
});
test('live event queue has a finite bound while metadata lookup is pending', async () => {
  const listener = new EventEmitter(); const events = [];
  const detach = attachNormalizedEvents(listener, (event) => events.push(event), () => new Promise(() => {}), 10, '1', 2);
  for (let id = 1; id <= 3; id += 1) listener.emit('message', { chat_id: 'room', log_id: String(id), author_id: 2, author_name: null, message: 'body', message_type: 1, sent_at: id });
  await detach();
  assert.equal(events.filter((event) => event.type === 'message').length, 2);
  assert.ok(events.some((event) => event.code === 'event_queue_full'));
});
test('continued pages rotate rooms, preserve older forward slice and emit body before cursor', async () => {
  const calls = []; const output = [];
  const result = await fetchReadOnlyCatchUp({
    client: { async getMessagePage(chatId, { from }) { calls.push([chatId, from]); return { messages: [history(103), history(101), history(102)].filter((message) => Number(message.log_id) > Number(from)), complete: true, next_cursor: '99999' }; } },
    cursors: [{ chatId: 'a', afterLogId: '100', limit: 2 }, { chatId: 'b', afterLogId: '100', limit: 2 }],
    maxPasses: 4, concurrency: 1, startOffset: 1,
    onMessage: async (event) => { await new Promise((resolve) => setTimeout(resolve, 1)); output.push(`m:${event.chatId}:${event.logId}`); },
    onProgress: async (event) => { output.push(`p:${event.chatId}:${event.logId}:${event.complete}`); },
  });
  assert.deepEqual(calls, [['b', '100'], ['a', '100'], ['b', '102'], ['a', '102']]);
  assert.deepEqual(output, ['m:b:101', 'm:b:102', 'p:b:102:false', 'm:a:101', 'm:a:102', 'p:a:102:false', 'm:b:103', 'p:b:103:true', 'm:a:103', 'p:a:103:true']);
  assert.equal(result.partial, false); assert.equal(result.messageCount, 6);
});
test('malformed or cross-room history never advances a recovery cursor', async () => {
  for (const messages of [[history(101), { log_id: '102' }], [history(101, 'other')]]) {
    const progress = [];
    await assert.rejects(fetchReadOnlyCatchUp({ client: { getMessagePage: async () => ({ messages, complete: true, next_cursor: '999' }) }, cursors: [{ chatId: 'room', afterLogId: '100' }], onProgress: (event) => progress.push(event) }), { code: 'catchup_failed' });
    assert.deepEqual(progress, []);
  }
});
test('history shutdown cancels an unresolved page without advancing progress', async () => {
  const controller = new AbortController(); const progress = [];
  const running = fetchReadOnlyCatchUp({ client: { getMessagePage: async () => { controller.abort(); return new Promise(() => {}); } }, cursors: [{ chatId: 'room', afterLogId: '1' }], signal: controller.signal, onProgress: (event) => progress.push(event) });
  await assert.rejects(running, { code: 'cancelled' }); assert.deepEqual(progress, []);
});
test('adapter flushes only adopted session pushes and continues rejecting stale identities', async () => {
  const client = new KakaoTalkClient();
  await client.login({ oauthToken: 'fixture', userId: '1', deviceUuid: 'fixture', deviceType: 'tablet' });
  const adopted = liveSession(); const stale = liveSession(); const received = [];
  client.connect = async function () { this.dispatchPush(stale, { method: 'MSG', body: { id: 'stale' } }); this.dispatchPush(adopted, { method: 'MSG', body: { id: 'buffered' } }); return { session: adopted, loginResult: {} }; };
  installSessionAdoptionBuffer(client); client.onPush((packet) => received.push(packet.body.id));
  await client.acquireSession();
  client.dispatchPush(stale, { method: 'MSG', body: { id: 'late-stale' } });
  client.dispatchPush(adopted, { method: 'MSG', body: { id: 'live' } });
  assert.deepEqual(received, ['buffered', 'live']); client.close();
});
test('failed adoption and adapter overflow discard buffered session packets', async () => {
  for (const fail of [true, false]) {
    const client = new KakaoTalkClient(); await client.login({ oauthToken: 'fixture', userId: '1', deviceUuid: 'fixture', deviceType: 'tablet' });
    const received = []; const session = liveSession();
    client.connect = async function () { for (let id = 0; id < 3; id++) this.dispatchPush(session, { method: 'MSG', body: { id } }); if (fail) throw new Error('fixture'); return { session, loginResult: {} }; };
    installSessionAdoptionBuffer(client, { maxMessages: 2 }); client.onPush((packet) => received.push(packet));
    await assert.rejects(client.acquireSession()); assert.deepEqual(received, []); client.close();
  }
});

test('early credential ACK remains buffered and recovered live overlap reaches host before progress', async () => {
  const input = new PassThrough();
  let attempt = 0;
  class Client {
    constructor() { this.attempt = ++attempt; }
    async login() { this.connected = this.attempt > 1; }
    isConnected() { return this.connected; }
    close() { this.connected = false; }
    acquireSession() { return Promise.resolve({}); }
    onPush() { return () => {}; }
    onSessionEvent() { return () => {}; }
    lookupAuthorName() { return 'fixture'; }
    async getMessagePage() { return { messages: [history(101)], next_cursor: '101', complete: true }; }
  }
  class Listener extends EventEmitter {
    constructor(client) { super(); this.client = client; }
    async start() {
      if (!this.client.isConnected()) { this.emit('error', Object.assign(new Error('expired'), { code: 'invalid_access_token' })); return; }
      this.emit('connected', { userId: '1' });
      this.emit('message', { chat_id: 'room', log_id: '101', author_id: 2, author_name: 'fixture', message: 'm101', message_type: 1, sent_at: 101 });
    }
    stop() {}
  }
  const captured = capture((event) => {
    if (event.type === 'auth_lease_release') input.write(JSON.stringify({ type: 'auth_lease_released',
      request_id: event.request_id, ok: true }) + '\n');
    if (event.status === 'catchup_completed') input.end();
  });
  // A single chunk contains the lease grant followed by a credential ACK,
  // both before runBridge begins waiting. The bounded reader must retain it.
  input.write(`${JSON.stringify({ ...start, catchUp: [{ chatId: 'room', afterLogId: '100' }] })}\n${JSON.stringify({
    type: 'auth_lease_granted', request_id: 'auth-1', ok: true, auth_lease_id: 'fixture-lease',
    credentials: start })}\n${JSON.stringify({ type: 'credentials_saved' })}\n`);
  const code = await runBridge({ input, output: captured.output, sdk: {
    KakaoTalkClient: Client, KakaoTalkListener: Listener,
    refreshKakaoOAuthToken: async () => ({ accessToken: 'fixture-new', refreshToken: 'fixture-refresh-new' }),
  } });
  assert.equal(code, 0); assert.equal(attempt, 2);
  const messages = captured.events.filter((event) => event.type === 'message');
  assert.deepEqual(messages.map((event) => [event.logId, Boolean(event.recovered)]), [['101', false], ['101', true]]);
  const recoveredIndex = captured.events.findIndex((event) => event.type === 'message' && event.recovered);
  const progressIndex = captured.events.findIndex((event) => event.type === 'catchup_progress');
  assert.ok(progressIndex > recoveredIndex);
  assert.deepEqual(captured.events[progressIndex], { type: 'catchup_progress', chatId: 'room', logId: '101', complete: true });
});

test('hung writable fails on an application deadline without an overflow trigger', async () => {
  const output = new Writable({ write() {} });
  const emit = createNdjsonEmitter(output, { timeoutMs: 20 });
  const writing = emit({ type: 'message', message: 'fixture' });
  await assert.rejects(writing, { code: 'output_backpressure' });
  assert.equal((await emit.failed).code, 'output_backpressure');
  emit.close(); output.destroy();
});

test('media without a text field still emits a safe body before its recovery progress', async () => {
  const event = normalizeListenerEvent('message', { chat_id: 'room', log_id: '101', author_id: 2, message_type: 3, sent_at: 101 });
  assert.equal(event.message, '[동영상]');
  const emitted = [];
  await fetchReadOnlyCatchUp({
    client: { getMessagePage: async () => ({ messages: [{ log_id: '101', type: 3, author_id: 2, sent_at: 101 }], complete: true }) },
    cursors: [{ chatId: 'room', afterLogId: '100' }],
    onMessage: (message) => emitted.push(message), onProgress: (progress) => emitted.push(progress),
  });
  assert.equal(emitted[0].message, '[동영상]');
  assert.deepEqual(emitted[1], { type: 'catchup_progress', chatId: 'room', logId: '101', complete: true });
});

test('actual SDK rejects null or destroyed socket closed before session adoption', async () => {
  const entryPoint = fileURLToPath(new URL('./fixtures/dead-session-child.mjs', import.meta.url));
  for (const mode of ['destroyed', 'null']) {
    const result = await childProbe(mode, start, undefined, entryPoint);
    assert.equal(result.code, 1, mode);
    assert.ok(result.events.some((event) => event.code === 'connection_failed'), JSON.stringify(result.events));
    assert.equal(result.events.some((event) => ['connected', 'ready'].includes(event.status) || ['heartbeat', 'message'].includes(event.type)), false, JSON.stringify(result.events));
  }
});

test('actual SDK keeps a live adopted socket and delivers its buffered startup message', async () => {
  const entryPoint = fileURLToPath(new URL('./fixtures/dead-session-child.mjs', import.meta.url));
  const result = await childProbe('live', start, (event, child) => { if (event.type === 'message') child.stdin.end(); }, entryPoint);
  assert.equal(result.code, 0);
  assert.ok(result.events.some((event) => event.message === 'fixture-buffered'));
});

test('a socket closing between connect return and adoption cannot flush its buffered message', async () => {
  const client = new KakaoTalkClient();
  await client.login({ oauthToken: 'fixture', userId: '1', deviceUuid: 'fixture', deviceType: 'tablet' });
  const session = liveSession(); const received = [];
  client.connect = async function () { this.dispatchPush(session, { method: 'MSG', body: { id: 'buffered' } }); return { session, loginResult: {} }; };
  const ensure = client.ensureSession;
  client.ensureSession = async function () { const state = await ensure.call(this); state.session.connection.socket.destroyed = true; return state; };
  installSessionAdoptionBuffer(client); client.onPush((packet) => received.push(packet));
  await assert.rejects(client.acquireSession(), { code: 'connection_failed' });
  assert.deepEqual(received, []); assert.equal(client.isConnected(), false); client.close();
});
