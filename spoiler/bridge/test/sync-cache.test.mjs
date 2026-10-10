import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, readdirSync, rmSync, existsSync, linkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PassThrough, Writable } from 'node:stream';
import { KakaoTalkClient, KakaoTalkListener, KakaoTalkError } from 'agent-messenger/kakaotalk';
import { LocoSession } from '../node_modules/agent-messenger/dist/src/platforms/kakaotalk/protocol/session.js';
import { installReadOnlySyncCache, getReadOnlySyncChatIds } from '../src/sync-cache.mjs';
import { runBridge } from '../src/bridge.mjs';

const uuid = 'fixture-cache-123';
const cacheName = `kakaotalk-sync-state-${uuid}.json`;
const valid = () => ({ version: 2, revision: 7, chatIds: [{ low: 1, high: 0 }],
  maxIds: [{ low: 100, high: 0 }], lastTokenId: { low: 3, high: 0 }, lbk: 1 });
const result = () => ({ revision: 8, chatDatas: [{ c: { low: 1, high: 0 }, ll: { low: 101, high: 0 } }],
  lastTokenId: { low: 4, high: 0 }, lbk: 2 });

function setup(t, onLogin = () => result()) {
  const root = mkdtempSync(join(tmpdir(), 'SpoilerSyncCache-'));
  const directory = join(root, 'bridge-state'); mkdirSync(directory);
  const originalEnv = process.env.AGENT_MESSENGER_CONFIG_DIR;
  const originalLogin = LocoSession.prototype.login;
  process.env.AGENT_MESSENGER_CONFIG_DIR = directory;
  const calls = [];
  LocoSession.prototype.login = async function (...args) { calls.push(args); return onLogin.apply(this, args); };
  t.after(() => {
    LocoSession.prototype.login = originalLogin;
    if (originalEnv === undefined) delete process.env.AGENT_MESSENGER_CONFIG_DIR;
    else process.env.AGENT_MESSENGER_CONFIG_DIR = originalEnv;
    rmSync(root, { recursive: true, force: true });
  });
  return { root, directory, path: join(directory, cacheName), calls };
}
async function client(deviceUuid = uuid) {
  const value = installReadOnlySyncCache(new KakaoTalkClient());
  await value.login({ oauthToken: 'fixture-only', userId: '1', deviceUuid, deviceType: 'tablet' });
  return value;
}

test('unchanged room IDs remain discovery hints when login returns an empty delta', async t => {
  const area = setup(t, () => ({ ...result(), chatDatas: [] }));
  writeFileSync(area.path, JSON.stringify(valid()));
  const value = await client(); await value.acquireSession();
  assert.deepEqual(getReadOnlySyncChatIds(value), ['1']);
  const copy = getReadOnlySyncChatIds(value); copy.push('999');
  assert.deepEqual(getReadOnlySyncChatIds(value), ['1']);
  value.close();
});

test('pinned SDK corrupt derivative is quarantined then login and future reuse succeed', async t => {
  const area = setup(t);
  writeFileSync(area.path, '{');
  const first = await client(); await first.acquireSession(); first.close();
  assert.equal(area.calls.length, 1);
  assert.equal(area.calls[0][3], undefined);
  const preserved = readdirSync(area.directory).filter(name => name.startsWith(`${cacheName}.invalid-`));
  assert.equal(preserved.length, 1);
  assert.equal(readFileSync(join(area.directory, preserved[0]), 'utf8'), '{');
  assert.equal(JSON.parse(readFileSync(area.path, 'utf8')).revision, 8);
  const second = await client(); await second.acquireSession(); second.close();
  assert.equal(area.calls.length, 2);
  assert.equal(area.calls[1][3].revision, 8);
  assert.equal(readdirSync(area.directory).filter(name => name.endsWith('.tmp')).length, 0);
});

test('valid sync cache is reused and login deltas merge with exact bounded schema', async t => {
  const area = setup(t, () => ({ ...result(), delChatIds: [{ low: 1, high: 0 }],
    chatDatas: [{ c: { low: 2, high: 0 }, ll: { low: 200, high: 0 } }] }));
  writeFileSync(area.path, JSON.stringify(valid()));
  const value = await client(); await value.acquireSession(); value.close();
  assert.deepEqual(area.calls[0][3], valid());
  const saved = JSON.parse(readFileSync(area.path, 'utf8'));
  assert.deepEqual(saved.chatIds, [{ low: 2, high: 0 }]);
  assert.deepEqual(saved.maxIds, [{ low: 200, high: 0 }]);
  assert.deepEqual(Object.keys(saved).sort(), ['chatIds', 'lastTokenId', 'lbk', 'maxIds', 'revision', 'version']);
  assert.equal(readdirSync(area.directory).filter(name => name.includes('.invalid-')).length, 0);
});

test('interrupted temp is preserved without promotion; committed cache remains input', async t => {
  const area = setup(t);
  writeFileSync(area.path, JSON.stringify(valid()));
  writeFileSync(`${area.path}.tmp`, '{unfinished derivative');
  writeFileSync(join(area.directory, 'loco-credentials.dat'), 'synthetic unrelated vault sentinel');
  const value = await client(); await value.acquireSession(); value.close();
  assert.equal(area.calls[0][3].revision, 7);
  const interrupted = readdirSync(area.directory).filter(name => name.startsWith(`${cacheName}.tmp.interrupted-`));
  assert.equal(interrupted.length, 1);
  assert.equal(readFileSync(join(area.directory, interrupted[0]), 'utf8'), '{unfinished derivative');
  assert.equal(readFileSync(join(area.directory, 'loco-credentials.dat'), 'utf8'), 'synthetic unrelated vault sentinel');
  assert.equal(existsSync(`${area.path}.tmp`), false);
});

test('failed atomic temp creation preserves previous valid cache and allows live login', async t => {
  let area;
  area = setup(t, () => { mkdirSync(`${area.path}.tmp`); return result(); });
  const before = JSON.stringify(valid()); writeFileSync(area.path, before);
  const value = await client(); await value.acquireSession(); value.close();
  assert.equal(area.calls.length, 1);
  assert.equal(readFileSync(area.path, 'utf8'), before);
  assert.equal(existsSync(`${area.path}.tmp`), true); // not our created file, never removed
});

test('unsafe device filename and non-private/missing configured paths are refused before login', async t => {
  const area = setup(t);
  for (const id of ['../outside', '..\\outside', 'a/b', 'a:b', '.', 'a'.repeat(129)]) {
    const value = await client(id);
    await assert.rejects(value.acquireSession(), error => error.code === 'sync_cache_path_invalid'); value.close();
  }
  process.env.AGENT_MESSENGER_CONFIG_DIR = area.root;
  let value = await client();
  await assert.rejects(value.acquireSession(), error => error.code === 'sync_cache_path_invalid'); value.close();
  delete process.env.AGENT_MESSENGER_CONFIG_DIR;
  value = await client();
  await assert.rejects(value.acquireSession(), error => error.code === 'sync_cache_path_invalid'); value.close();
  assert.equal(area.calls.length, 0);
  assert.deepEqual(readdirSync(area.directory), []);
});

test('cache hardlinks cannot redirect replacement or quarantine outside owned path', async t => {
  const area = setup(t);
  const outside = join(area.root, 'outside.json'); writeFileSync(outside, '{'); linkSync(outside, area.path);
  const value = await client();
  await assert.rejects(value.acquireSession(), error => error.code === 'sync_cache_path_invalid'); value.close();
  assert.equal(readFileSync(outside, 'utf8'), '{');
  assert.equal(area.calls.length, 0);
  assert.deepEqual(readdirSync(area.directory), [cacheName]);
});

test('invalid schema and over-limit derivative are quarantined without parsing unbounded input', async t => {
  const area = setup(t);
  writeFileSync(area.path, JSON.stringify({ ...valid(), maxIds: [] }));
  let value = await client(); await value.acquireSession(); value.close();
  assert.equal(area.calls[0][3], undefined);
  writeFileSync(area.path, Buffer.alloc(8 * 1024 * 1024 + 1, 0x20));
  value = await client(); await value.acquireSession(); value.close();
  assert.equal(area.calls[1][3], undefined);
  assert.equal(readdirSync(area.directory).filter(name => name.includes('.invalid-')).length, 2);
});

test('pinned login error class and recoverable code survive sanitized cache adapter', async t => {
  setup(t, () => { throw Object.assign(new Error('synthetic-secret-error-text'), { code: 'invalid_access_token', serverStatus: -950 }); });
  const value = await client();
  await assert.rejects(value.acquireSession(), error => error instanceof KakaoTalkError
    && error.code === 'invalid_access_token' && error.serverStatus === -950 && error.message === 'invalid_access_token');
  value.close();
});

test('actual cache plus adoption adapters retain expired-token refresh and durable ACK flow', async t => {
  const area = setup(t, function (token) {
    if (token === 'fixture-expired') throw Object.assign(new Error('synthetic-secret-error-text'), { code: 'invalid_access_token', serverStatus: -950 });
    const socket = { destroyed: false, readable: true, writable: true, readableEnded: false, writableEnded: false, connecting: false };
    this.connection = { socket, close() { socket.destroyed = true; } };
    return result();
  });
  const input = new PassThrough();
  const events = [];
  const output = new Writable({ write(chunk, _encoding, callback) {
    const event = JSON.parse(String(chunk)); events.push(event);
    if (event.type === 'auth_lease_request') queueMicrotask(() => input.write(JSON.stringify({
      type: 'auth_lease_granted', request_id: event.request_id, ok: true,
      auth_lease_id: 'fixture-lease', credentials: event.credentials }) + '\n'));
    if (event.type === 'auth_lease_release') queueMicrotask(() => input.write(JSON.stringify({
      type: 'auth_lease_released', request_id: event.request_id, ok: true }) + '\n'));
    if (event.type === 'credentials') queueMicrotask(() => input.write('{"type":"credentials_saved"}\n'));
    if (event.type === 'status' && event.status === 'ready') queueMicrotask(() => input.end());
    callback();
  } });
  let refreshCalls = 0;
  input.write(JSON.stringify({ type: 'start', oauthToken: 'fixture-expired', refreshToken: 'fixture-refresh',
    userId: '1', deviceUuid: uuid, deviceType: 'tablet' }) + '\n');
  const code = await runBridge({ input, output, sdk: { KakaoTalkClient, KakaoTalkListener, useSessionAdapter: true,
    refreshKakaoOAuthToken: async () => { refreshCalls++; return { accessToken: 'fixture-fresh', refreshToken: 'fixture-rotated' }; } },
    limits: { inputMs: 1000, sessionMs: 1000, refreshMs: 1000, ackMs: 1000, drainMs: 1000 } });
  assert.equal(code, 0);
  assert.equal(refreshCalls, 1);
  assert.equal(area.calls.length, 2);
  assert.equal(area.calls[1][0], 'fixture-fresh');
  assert.equal(events.some(event => event.status === 'ready'), true);
  assert.equal(JSON.stringify(events).includes('synthetic-secret-error-text'), false);
});
