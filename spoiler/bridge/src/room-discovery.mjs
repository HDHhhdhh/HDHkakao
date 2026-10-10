import { Long } from 'bson';
import { withDeadline } from './io.mjs';

const MAX_ID = 9223372036854775807n;
const MAX_HINTS = 10000;
const WEEK_SECONDS = 7 * 24 * 60 * 60;

function decimal(value, allowZero = false) {
  if (Long.isLong(value) || typeof value === 'bigint') value = String(value);
  else if (typeof value === 'number' && Number.isSafeInteger(value)) value = String(value);
  if (typeof value !== 'string' || !/^(0|[1-9]\d{0,18})$/.test(value)) return null;
  const id = BigInt(value);
  return id <= MAX_ID && (allowZero || id > 0n) ? value : null;
}

export function unreadAnchor(packet, chatId, nowSeconds, verifiedSentAt = null) {
  const info = packet?.body?.chatInfo;
  if (packet?.method !== 'CHATINFO' || packet.statusCode !== 0
      || (packet.body?.status !== undefined && packet.body.status !== 0)
      || !info || info.left === true || decimal(info.chatId) !== chatId) return null;
  const latest = decimal(info.lastLogId);
  const seen = decimal(info.lastSeenLogId, true);
  const sentAt = Number.isFinite(info.lastChatLog?.sendAt) ? info.lastChatLog.sendAt : verifiedSentAt;
  if (!latest || seen === null || BigInt(seen) >= BigInt(latest)
      || !Number.isFinite(sentAt) || sentAt < nowSeconds - WEEK_SECONDS || sentAt > nowSeconds + 300) return null;
  return { chatId, afterLogId: seen, throughLogId: latest, limit: 50 };
}

// Metadata discovers a previously unseen room. It never fabricates a body,
// advances a durable cursor, enters a room, or changes its read watermark.
export function createUnreadRoomDiscovery({ getChatIds, getChannelInfo, peekLatest, onAnchor, now = Date.now,
  batchSize = 64, concurrency = 4, timeoutMs = 8000, pageTimeoutMs = 2000,
  retryAfterMs = 120000 } = {}) {
  let offset = 0;
  const checked = new Map();
  return async (state, signal) => {
    const result = { discovered: 0, partial: false };
    if (signal?.aborted) return result;
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    try {
      const rawIds = await withDeadline(() => getChatIds(), timeoutMs, 'room_discovery_timeout', controller.signal);
      if (!Array.isArray(rawIds) || rawIds.length > MAX_HINTS) return { ...result, partial: true };
      const moment = now();
      const ids = [...new Set([...state.pendingDiscoveryIds(), ...rawIds].map(id => decimal(id)).filter(Boolean))]
        .filter(id => state.needsDiscovery(id) && (!checked.has(id) || moment - checked.get(id) >= retryAfterMs));
      if (!ids.length || signal?.aborted) return result;
      const count = Math.min(Math.max(1, batchSize), ids.length);
      const start = offset % ids.length;
      const batch = Array.from({ length: count }, (_, index) => ids[(start + index) % ids.length]);
      offset = (start + count) % ids.length;
      result.partial = count < ids.length;
      let next = 0;
      const worker = async () => {
        while (next < batch.length && !controller.signal.aborted) {
          const id = batch[next++];
          if (!state.needsDiscovery(id)) continue;
          try {
            const packet = await withDeadline(() => getChannelInfo(id), pageTimeoutMs,
              'room_discovery_timeout', controller.signal);
            if (controller.signal.aborted) return;
            if (packet?.method !== 'CHATINFO' || packet.statusCode !== 0
                || (packet.body?.status !== undefined && packet.body.status !== 0)
                || decimal(packet.body?.chatInfo?.chatId) !== id) {
              result.partial = true; continue;
            }
            const info = packet.body.chatInfo;
            let verifiedAt = null;
            const latest = decimal(info.lastLogId), seen = decimal(info.lastSeenLogId, true);
            if (latest && seen !== null && BigInt(seen) < BigInt(latest)
                && !Number.isFinite(info.lastChatLog?.sendAt) && typeof peekLatest === 'function') {
              // A synced login may omit lastChatLog and report invalid/zero
              // newMessageCount. A real head body verifies the time instead.
              const head = await withDeadline(() => peekLatest(id, latest), pageTimeoutMs,
                'room_discovery_timeout', controller.signal);
              if (controller.signal.aborted) return;
              if (!head || decimal(head.log_id) !== latest || !Number.isFinite(head.sent_at)) {
                result.partial = true; continue;
              }
              verifiedAt = head.sent_at;
            }
            const anchor = unreadAnchor(packet, id, Math.floor(moment / 1000), verifiedAt);
            if (anchor && state.needsDiscovery(id)) {
              if (state.seedUnread(anchor)) {
                if (typeof onAnchor === 'function' && await onAnchor(anchor) === false) {
                  result.partial = true; controller.abort(); return;
                }
                result.discovered++;
              }
              else { result.partial = true; continue; }
            }
            checked.set(id, moment);
            while (checked.size > MAX_HINTS) checked.delete(checked.keys().next().value);
          } catch { if (!signal?.aborted) result.partial = true; }
        }
      };
      await withDeadline(() => Promise.all(Array.from({ length: Math.max(1, Math.min(4, concurrency)) }, worker)),
        timeoutMs, 'room_discovery_timeout', controller.signal);
    } catch { if (!signal?.aborted) result.partial = true; }
    finally { controller.abort(); signal?.removeEventListener('abort', abort); }
    return result;
  };
}
