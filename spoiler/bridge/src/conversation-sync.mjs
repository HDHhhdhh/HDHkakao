import { fetchReadOnlyCatchUp, sanitizeCatchUpCursors } from './catchup.mjs';
import { safeError, withDeadline } from './io.mjs';

const MAX_ROOMS = 512;
const MAX_ID = 9223372036854775807n;

function canonicalDecimal(value, allowZero = false) {
  if (typeof value !== 'string' || !/^(0|[1-9]\d{0,18})$/.test(value)) return null;
  const parsed = BigInt(value);
  return parsed <= MAX_ID && (allowZero || parsed > 0n) ? value : null;
}

/** Highest live logs are observations, not proof that intervening bodies exist. */
export function createConversationSyncState(cursors, maxRooms = MAX_ROOMS) {
  const capacity = Math.max(1, Math.min(MAX_ROOMS, Math.trunc(maxRooms) || MAX_ROOMS));
  const rooms = new Map();
  const initial = sanitizeCatchUpCursors(cursors).slice(0, capacity);
  let activity = initial.length;
  // The host snapshot is most recently active first.
  for (let index = 0; index < initial.length; index++) {
    const cursor = initial[index];
    rooms.set(cursor.chatId, { ...cursor, activity: initial.length - index, hostKnown: true });
  }
  return {
    has(chatId) { return rooms.has(chatId); },
    needsDiscovery(chatId) {
      const room = rooms.get(chatId);
      return !room || (!room.hostKnown && !room.discovered);
    },
    pendingDiscoveryIds() {
      return [...rooms.values()].filter(room => !room.hostKnown && !room.discovered).map(room => room.chatId);
    },
    seedUnread(cursor) {
      const chatId = canonicalDecimal(cursor?.chatId);
      const afterLogId = canonicalDecimal(cursor?.afterLogId, true);
      if (!chatId || !afterLogId) return false;
      const existing = rooms.get(chatId);
      if (existing) {
        if (existing.hostKnown || existing.discovered) return false;
        if (BigInt(afterLogId) < BigInt(existing.afterLogId)) existing.afterLogId = afterLogId;
        existing.discovered = true; existing.activity = ++activity;
        return true;
      }
      if (rooms.size >= capacity) return false;
      rooms.set(chatId, { chatId, afterLogId, limit: 50, activity: ++activity, discovered: true });
      return true;
    },
    observe(event) {
      if (event?.type !== 'message' || event.recovered || event.replyAck) return;
      const chatId = canonicalDecimal(event.chatId);
      const logId = canonicalDecimal(event.logId);
      if (!chatId || !logId) return;
      const current = rooms.get(chatId);
      if (current) { current.activity = ++activity; return; }
      // New rooms start at the first actually observed body. No heuristic
      // tries to turn an author/display-name key into a server room identity.
      if (rooms.size >= capacity) {
        const oldest = [...rooms.values()].sort((a, b) => a.activity - b.activity)[0];
        rooms.delete(oldest.chatId);
      }
      rooms.set(chatId, { chatId, afterLogId: String(BigInt(logId) - 1n), limit: 50, activity: ++activity });
    },
    snapshot(maxMessages = 500) {
      const available = [...rooms.values()].sort((a, b) => b.activity - a.activity);
      // Every room gets a page opportunity in the first pass, even at 80 rooms.
      const perRoom = Math.max(1, Math.min(50, Math.floor(maxMessages / Math.max(1, available.length))));
      return available.map(room => ({ chatId: room.chatId, afterLogId: room.afterLogId,
        limit: Math.min(room.limit, perRoom), identity: room }));
    },
    advance(snapshot, logId) {
      const current = rooms.get(snapshot.chatId);
      if (!current || current !== snapshot.identity) return false;
      const next = canonicalDecimal(logId, true);
      const previous = canonicalDecimal(current.afterLogId, true);
      if (next && previous && BigInt(next) >= BigInt(previous)) current.afterLogId = next;
      // Test/legacy cursors can be nonnumeric. They remain readable but must
      // never use lexicographic guesses to advance a production watermark.
      return true;
    },
    isCurrent(snapshot) { return rooms.get(snapshot.chatId) === snapshot.identity; },
    get size() { return rooms.size; },
  };
}

function waitForCycle(milliseconds, signal) {
  if (signal.aborted) return Promise.resolve();
  return new Promise(resolve => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
    const timer = setTimeout(finish, milliseconds);
    signal.addEventListener('abort', finish, { once: true });
  });
}

/** One session, sequential bounded cycles. No login, read watermark, room-open or send call. */
export async function runConversationSync({ state, client, currentUserId, signal, emit, onMessage,
  intervalMs = 30000, pageTimeoutMs = 10000, maxDurationMs = 45000, maxMessages = 500,
  resolveAuthorName, resolveChatTitle, refreshChatTitles, discoverUnreadRooms } = {}) {
  if (!client || typeof client.getMessagePage !== 'function') return;
  let nextOffset = 0;
  while (!signal.aborted) {
    const started = Date.now();
    let discovery = null;
    if (typeof discoverUnreadRooms === 'function') {
      try { discovery = await discoverUnreadRooms(state, signal); }
      catch { discovery = { partial: true }; }
      if (signal.aborted) break;
    }
    const snapshots = state.snapshot(maxMessages);
    const byChat = new Map(snapshots.map(snapshot => [snapshot.chatId, snapshot]));
    if (snapshots.length) {
      const cycleController = new AbortController();
      const abortCycle = () => cycleController.abort();
      signal.addEventListener('abort', abortCycle, { once: true });
      const visited = new Set();
      let delivered = 0;
      // Room names are independent of new bodies. An empty history page must
      // still be able to repair an own-only room or pick up a renamed room.
      const metadata = typeof refreshChatTitles === 'function'
        ? withDeadline(() => refreshChatTitles(snapshots.map(item => item.chatId), cycleController.signal),
          Math.min(maxDurationMs, 15000), 'room_metadata_timeout', cycleController.signal).catch(() => {})
        : Promise.resolve();
      await emit({ type: 'status', status: 'catchup_started', rooms: snapshots.length });
      try {
        const result = await withDeadline(() => fetchReadOnlyCatchUp({
          client: { getMessagePage: (chatId, options) => { visited.add(chatId); return client.getMessagePage(chatId, options); } },
          cursors: snapshots, currentUserId, resolveAuthorName, resolveChatTitle,
          signal: cycleController.signal,
          onMessage: async event => { const accepted = await onMessage(event); if (accepted !== false) delivered++; return accepted; },
          onProgress: async progress => {
            const snapshot = byChat.get(progress.chatId);
            if (!snapshot || !state.isCurrent(snapshot) || cycleController.signal.aborted) return false;
            // An empty initial anchor may never have been a stored body.
            // It needs no durable progress update or failure-looking host ACK.
            if (progress.logId !== snapshot.afterLogId) {
              if (await emit(progress) === false) return false;
            }
            if (!state.advance(snapshot, progress.logId)) return false;
            // This is a local completion marker, not a server read watermark.
            // All page bodies and any durable progress frame precede it in
            // the same stdout queue. An unchanged empty anchor is not a body.
            if (progress.complete && await emit({ type: 'conversation_synced',
              chatId: progress.chatId, complete: true }) === false) return false;
            return true;
          },
          maxPasses: 128, maxMessages, maxDurationMs, pageTimeoutMs, concurrency: 4,
          collectMessages: false, startOffset: nextOffset,
        }), maxDurationMs, 'catchup_timeout', cycleController.signal);
        await metadata;
        if (!signal.aborted) await emit({ type: 'status', status: 'catchup_completed', rooms: result.requestedRooms,
          messages: result.messageCount, partial: result.partial || discovery?.partial === true, failedRooms: result.failedRooms,
          nextCursors: result.nextCursors });
      } catch (error) {
        if (signal.aborted || error?.code === 'cancelled') break;
        if (error?.code === 'catchup_timeout') await emit({ type: 'status', status: 'catchup_completed',
          rooms: snapshots.length, messages: delivered, partial: true, failedRooms: 0 });
        else await emit({ type: 'status', status: 'catchup_failed', ...safeError(error, 'catchup_failed') });
      } finally {
        cycleController.abort();
        signal.removeEventListener('abort', abortCycle);
        // Slow rooms or a deadline cannot repeatedly starve the tail.
        nextOffset = (nextOffset + (visited.size === snapshots.length ? 1 : Math.max(1, visited.size))) % snapshots.length;
      }
    }
    // Long cycles are already bounded; leave a small gap rather than spinning.
    await waitForCycle(Math.max(1000, intervalMs - (Date.now() - started)), signal);
  }
}
