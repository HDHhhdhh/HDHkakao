import { lstat, open, realpath, rename, unlink } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { LocoSession } from '../node_modules/agent-messenger/dist/src/platforms/kakaotalk/protocol/session.js';
import { KakaoTalkError } from '../node_modules/agent-messenger/dist/src/platforms/kakaotalk/client.js';
import { bridgeError } from './io.mjs';

const MAX_BYTES = 8 * 1024 * 1024;
const MAX_ROOMS = 10000;
const installed = new WeakSet();
const knownChatIds = new WeakMap();

// Login deltas omit unchanged rooms. Their validated derivative IDs are
// discovery hints only; CHATINFO must still verify each room and read cursor.
export function getReadOnlySyncChatIds(client) {
  return (knownChatIds.get(client) ?? []).slice();
}
const samePath = (a, b) => process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b;

async function fileInfo(path) {
  try { return await lstat(path); }
  catch (error) { if (error?.code === 'ENOENT') return null; throw error; }
}

async function ownedPaths(deviceUuid, configuredDirectory) {
  if (typeof deviceUuid !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(deviceUuid)
      || typeof configuredDirectory !== 'string' || !isAbsolute(configuredDirectory)) {
    throw bridgeError('sync_cache_path_invalid');
  }
  const directory = resolve(configuredDirectory);
  if (basename(directory).toLowerCase() !== 'bridge-state') throw bridgeError('sync_cache_path_invalid');
  const stat = await fileInfo(directory);
  if (!stat?.isDirectory() || stat.isSymbolicLink() || !samePath(await realpath(directory), directory)) {
    throw bridgeError('sync_cache_path_invalid');
  }
  const path = join(directory, `kakaotalk-sync-state-${deviceUuid}.json`);
  if (!samePath(dirname(path), directory)) throw bridgeError('sync_cache_path_invalid');
  return { directory, path, temporary: `${path}.tmp` };
}

function assertRegular(stat) {
  if (stat && (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1)) throw bridgeError('sync_cache_path_invalid');
}

async function quarantine(path, kind) {
  const stat = await fileInfo(path);
  if (!stat) return;
  assertRegular(stat);
  // Only the exact client-owned derivative is moved. Preserve its bytes for
  // recovery/diagnosis; never scan/delete sibling files, credentials or inbox.
  await rename(path, `${path}.${kind}-${randomUUID()}.bak`);
}

function longValue(value) {
  if (value && typeof value === 'object' && Number.isInteger(value.low) && Number.isInteger(value.high)
      && value.low >= -2147483648 && value.low <= 4294967295
      && value.high >= -2147483648 && value.high <= 4294967295) {
    return { low: value.low | 0, high: value.high | 0 };
  }
  if (typeof value === 'bigint' || (typeof value === 'number' && Number.isSafeInteger(value))
      || (typeof value === 'string' && /^\d{1,20}$/.test(value))) {
    const number = BigInt(value);
    return { low: Number(BigInt.asIntN(32, number)), high: Number(BigInt.asIntN(32, number >> 32n)) };
  }
  return null;
}
const longKey = value => {
  const item = longValue(value);
  return item ? `${item.high}:${item.low}` : null;
};

function normalizeState(value) {
  if (!value || value.version !== 2 || !Number.isSafeInteger(value.revision) || !Number.isSafeInteger(value.lbk)
      || !Array.isArray(value.chatIds) || !Array.isArray(value.maxIds)
      || value.chatIds.length !== value.maxIds.length || value.chatIds.length > MAX_ROOMS) return null;
  const chatIds = value.chatIds.map(longValue);
  const maxIds = value.maxIds.map(longValue);
  const lastTokenId = longValue(value.lastTokenId);
  if (!lastTokenId || chatIds.some(item => !item) || maxIds.some(item => !item)) return null;
  return { version: 2, revision: value.revision, chatIds, maxIds, lastTokenId, lbk: value.lbk };
}

async function loadState(paths) {
  // A previous process may have died before rename. Such a temp is never
  // promoted; the previously committed cache remains authoritative.
  await quarantine(paths.temporary, 'interrupted');
  const stat = await fileInfo(paths.path);
  if (!stat) return undefined;
  assertRegular(stat);
  if (stat.size <= 0 || stat.size > MAX_BYTES) { await quarantine(paths.path, 'invalid'); return undefined; }
  let state;
  const handle = await open(paths.path, 'r');
  try {
    const opened = await handle.stat();
    assertRegular(opened);
    if (opened.size > MAX_BYTES) throw bridgeError('sync_cache_path_invalid');
    const bytes = Buffer.alloc(opened.size + 1);
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
    if (bytesRead !== opened.size) throw bridgeError('sync_cache_path_invalid');
    try { state = normalizeState(JSON.parse(bytes.subarray(0, bytesRead).toString('utf8'))); }
    catch { state = null; }
  } finally { await handle.close(); }
  if (!state) await quarantine(paths.path, 'invalid');
  return state ?? undefined;
}

function mergeState(previous, result) {
  const rooms = new Map();
  if (previous) previous.chatIds.forEach((chat, i) => rooms.set(longKey(chat), [chat, previous.maxIds[i]]));
  for (const id of Array.isArray(result.delChatIds) ? result.delChatIds : []) rooms.delete(longKey(id));
  for (const chat of Array.isArray(result.chatDatas) ? result.chatDatas : []) {
    const id = longValue(chat.c);
    const max = longValue(chat.ll);
    if (id && max) rooms.set(longKey(id), [id, max]);
  }
  const values = [...rooms.values()];
  return normalizeState({ version: 2,
    revision: Number.isSafeInteger(result.revision) ? result.revision : previous?.revision ?? 0,
    chatIds: values.map(([id]) => id), maxIds: values.map(([, max]) => max),
    lastTokenId: longValue(result.lastTokenId) ?? { low: 0, high: 0 },
    lbk: Number.isSafeInteger(result.lbk) ? result.lbk : previous?.lbk ?? 0 });
}

async function saveState(paths, state) {
  if (!state) return false;
  const bytes = Buffer.from(JSON.stringify(state));
  if (bytes.length > MAX_BYTES) return false;
  let handle;
  let created = false;
  try {
    assertRegular(await fileInfo(paths.path));
    handle = await open(paths.temporary, 'wx', 0o600);
    created = true;
    await handle.writeFile(bytes);
    await handle.sync();
    await handle.close(); handle = null;
    // Recheck before replacing; a sibling symlink/hardlink is never followed.
    assertRegular(await fileInfo(paths.path));
    await rename(paths.temporary, paths.path);
    created = false;
    // POSIX directory fsync makes rename durable; Windows may reject opening
    // directories. File fsync above is required on both platforms.
    let directoryHandle;
    try { directoryHandle = await open(paths.directory, 'r'); await directoryHandle.sync(); }
    catch { /* Directory fsync is not available on every supported OS. */ }
    finally { await directoryHandle?.close().catch(() => {}); }
    return true;
  } catch (error) {
    if (error?.code === 'sync_cache_path_invalid') throw error;
    // This is derivative login acceleration only. Durable inbox state is
    // owned by the host; a failed cache write must not discard a live login.
    return false;
  } finally {
    await handle?.close().catch(() => {});
    if (created) await unlink(paths.temporary).catch(() => {});
  }
}

/** Pinned SDK 2.37.1 per-client connect adapter. The upstream module-private
 * cache functions cannot be decorated, so this preserves its session flow
 * while replacing only derivative cache I/O. Install before adoption buffer.
 */
export function installReadOnlySyncCache(client) {
  if (!client || !['connect', 'dispatchPush', 'emitSessionEvent'].every(key => typeof client[key] === 'function')
      || typeof client.nameCache?.ingest !== 'function') throw bridgeError('sdk_adapter_unavailable');
  if (installed.has(client)) return client;
  const configuredDirectory = process.env.AGENT_MESSENGER_CONFIG_DIR;
  client.connect = async function () {
    const paths = await ownedPaths(this.deviceUuid, configuredDirectory);
    const previous = await loadState(paths);
    if (this.closed) throw bridgeError('client_closed');
    const session = new LocoSession();
    session.onPush(packet => this.dispatchPush(session, packet));
    session.onClose(() => {
      if (this.state?.session === session) {
        this.state = null; this.initPromise = null;
        this.emitSessionEvent({ type: 'disconnected' });
      }
    });
    try {
      const loginResult = await session.login(this.oauthToken, this.userId, this.deviceUuid, previous, this.deviceType);
      const merged = mergeState(previous, loginResult);
      if (merged) {
        const decimal = value => (BigInt(value.high) << 32n) + BigInt(value.low >>> 0);
        const hints = merged.chatIds.map((id, index) => ({ id: decimal(id), log: decimal(merged.maxIds[index]) }))
          .filter(item => item.id > 0n && item.log >= 0n)
          .sort((a, b) => a.log === b.log ? 0 : a.log > b.log ? -1 : 1);
        knownChatIds.set(this, [...new Set(hints.map(item => String(item.id)))]);
      }
      await saveState(paths, merged);
      this.nameCache.ingest(loginResult.chatDatas ?? []);
      return { session, loginResult };
    } catch (error) {
      session.close();
      // Preserve the pinned client's public error class/code used by the
      // bridge token-refresh branch, without forwarding upstream secret text.
      const loginResponse = error instanceof Error
        && ['invalid_access_token', 'login_rejected'].includes(error.code)
        && typeof error.serverStatus === 'number';
      const code = loginResponse ? error.code : 'login_failed';
      throw new KakaoTalkError(code, code, { serverStatus: loginResponse ? error.serverStatus : undefined });
    }
  };
  installed.add(client);
  return client;
}
