import { bridgeError, withDeadline } from './io.mjs';
const ID_PATTERN = /^[0-9A-Za-z._:-]{1,200}$/;

// The bridge only performs one bounded forward page per requested room.  A
// reconnect must never turn into an unbounded history export or a flood of
// member/title lookups.
export const MAX_CATCHUP_ROOMS = 512;
export const MAX_CATCHUP_MESSAGES_PER_CHAT = 100;
export const MAX_CATCHUP_MESSAGES_TOTAL = 500;
export const DEFAULT_CATCHUP_LIMIT = 50;
export const MAX_DEDUPE_ENTRIES = 4096;

function toStringId(value) {
  if (typeof value === 'string') {
    const normalized = value.trim();
    return ID_PATTERN.test(normalized) ? normalized : null;
  }
  if (typeof value === 'number' && Number.isSafeInteger(value)) {
    const normalized = String(value);
    return ID_PATTERN.test(normalized) ? normalized : null;
  }
  if (typeof value === 'bigint') {
    const normalized = String(value);
    return ID_PATTERN.test(normalized) ? normalized : null;
  }
  return null;
}

function nonEmptyText(value, maxLength = 2000) {
  if (typeof value !== 'string') return null;
  const normalized = value.trim();
  return normalized.length > 0 ? normalized.slice(0, maxLength) : null;
}

function comparableId(value) {
  if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'bigint') return null;
  const normalized = String(value).trim();
  return normalized.length > 0 ? normalized : null;
}

function isMine(authorId, currentUserId) {
  const author = comparableId(authorId);
  const current = comparableId(currentUserId);
  return author !== null && current !== null && author === current;
}

function safeAuthorId(value) {
  if (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0) return value;
  if (typeof value === 'bigint' && value >= 0n && value <= BigInt(Number.MAX_SAFE_INTEGER)) return Number(value);
  if (typeof value === 'string' && /^\d+$/.test(value.trim())) {
    const parsed = Number(value.trim());
    if (Number.isSafeInteger(parsed) && parsed >= 0) return parsed;
  }
  return null;
}

function safeMessageType(value) {
  if (typeof value === 'number' && Number.isInteger(value) && value >= 0) return value;
  if (typeof value === 'string' && /^\d+$/.test(value.trim())) {
    const parsed = Number(value.trim());
    if (Number.isSafeInteger(parsed) && parsed >= 0) return parsed;
  }
  return null;
}

function safeTimestamp(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim().length > 0) {
    const parsed = Number(value.trim());
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function compareIds(left, right) {
  try {
    const a = BigInt(left);
    const b = BigInt(right);
    return a < b ? -1 : a > b ? 1 : 0;
  } catch {
    return String(left).localeCompare(String(right));
  }
}

function compareMessages(left, right) {
  const leftTime = Number.isFinite(left?.sentAt) ? left.sentAt : 0;
  const rightTime = Number.isFinite(right?.sentAt) ? right.sentAt : 0;
  if (leftTime !== rightTime) return leftTime - rightTime;
  const chatOrder = String(left?.chatId ?? '').localeCompare(String(right?.chatId ?? ''));
  if (chatOrder !== 0) return chatOrder;
  return compareIds(left?.logId ?? '', right?.logId ?? '');
}

function messageKey(event) {
  if (!event || event.type !== 'message') return null;
  const chatId = toStringId(event.chatId);
  const logId = toStringId(event.logId);
  return chatId && logId ? `${chatId}\u001F${logId}` : null;
}

function invalidCatchupRequest(message) {
  return Object.assign(new Error(message), { code: 'invalid_catchup_request' });
}

function latestCursor(left, right) {
  return compareIds(left, right) >= 0 ? left : right;
}

/**
 * Validate the reconnect cursors before any SDK method is reached.  The
 * returned objects contain only the small allowlist used by the read-only
 * history call; arbitrary protocol fields never cross this boundary.
 */
export function sanitizeCatchUpCursors(value) {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || value.length > MAX_CATCHUP_ROOMS) {
    throw invalidCatchupRequest(`catchUp must contain at most ${MAX_CATCHUP_ROOMS} rooms.`);
  }

  const byChat = new Map();
  for (const raw of value) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw invalidCatchupRequest('Each catchUp cursor must be an object.');
    }
    const chatId = toStringId(raw.chatId ?? raw.chat_id);
    const rawAfterLogId = raw.afterLogId ?? raw.after_log_id ?? raw.from;
    const afterLogId = rawAfterLogId === undefined || rawAfterLogId === null
      ? '0'
      : toStringId(rawAfterLogId);
    const requestedLimit = raw.limit ?? raw.count;
    const limit = requestedLimit === undefined ? DEFAULT_CATCHUP_LIMIT : requestedLimit;
    if (!chatId || !toStringId(afterLogId) || !Number.isInteger(limit)
      || limit < 1 || limit > MAX_CATCHUP_MESSAGES_PER_CHAT) {
      throw invalidCatchupRequest('Each catchUp cursor needs a valid chatId, afterLogId, and limit.');
    }

    const existing = byChat.get(chatId);
    if (!existing) {
      byChat.set(chatId, { chatId, afterLogId, limit });
      continue;
    }
    // Duplicate room entries can be produced when a reconnect snapshot is
    // merged with a live cursor.  Keep the newest watermark and the larger
    // requested page, never issuing two history calls for one room.
    existing.afterLogId = latestCursor(existing.afterLogId, afterLogId);
    existing.limit = Math.max(existing.limit, limit);
  }
  return [...byChat.values()];
}

/**
 * Convert either agent-messenger's snake_case response or a camelCase test/
 * host object into the same message envelope emitted by the live listener.
 */
export function normalizeCatchUpMessage(raw, currentUserId, metadata = {}) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const chatId = toStringId(raw.chat_id ?? raw.chatId ?? metadata.chatId);
  const logId = toStringId(raw.log_id ?? raw.logId);
  const authorId = safeAuthorId(raw.author_id ?? raw.authorId);
  const messageType = safeMessageType(raw.message_type ?? raw.messageType ?? raw.type);
  const message = typeof (raw.message ?? raw.text) === 'string' ? (raw.message ?? raw.text) : (messageType !== null && messageType !== 1 ? '' : null);
  const sentAt = safeTimestamp(raw.sent_at ?? raw.sentAt);
  if (!chatId || !logId || authorId === null || messageType === null || message === null || sentAt === null) return null;

  const event = {
    type: 'message',
    chatId,
    logId,
    authorId,
    authorName: nonEmptyText(raw.author_name ?? raw.authorName, 200),
    message: message.length ? message : ({ 2: '[사진]', 3: '[동영상]', 5: '[음성 메시지]', 18: '[파일]', 26: '[답장]', 27: '[사진 여러 장]' }[messageType] ?? '[메시지]'),
    messageType,
    sentAt,
    isMine: isMine(authorId, currentUserId),
    recovered: true,
  };
  const chatTitle = nonEmptyText(metadata.chatTitle, 200);
  if (chatTitle) event.chatTitle = chatTitle;
  return event;
}

/**
 * A bounded process-lifetime identity set.  It is intentionally keyed only
 * by chatId/logId, so duplicate push/history deliveries cannot duplicate a
 * preview while raw message contents are never retained here.
 */
export function createMessageDeduper(maxEntries = MAX_DEDUPE_ENTRIES) {
  const boundedMax = Number.isInteger(maxEntries) && maxEntries > 0 ? maxEntries : MAX_DEDUPE_ENTRIES;
  const seen = new Set();
  const order = [];
  return {
    accept(event) {
      const key = messageKey(event);
      if (!key) return true;
      if (seen.has(key)) return false;
      seen.add(key);
      order.push(key);
      while (order.length > boundedMax) {
        seen.delete(order.shift());
      }
      return true;
    },
    filter(events) {
      return Array.isArray(events) ? events.filter((event) => this.accept(event)) : [];
    },
    has(event) {
      const key = messageKey(event);
      return key ? seen.has(key) : false;
    },
    get size() {
      return seen.size;
    },
  };
}

/** Deduplicate by room/log identity and return chronological output. */
export function dedupeAndOrderMessages(messages, deduper = createMessageDeduper()) {
  const accepted = deduper.filter(messages);
  return accepted.sort(compareMessages);
}

async function resolveWithTimeout(resolver, args, timeoutMs) {
  if (typeof resolver !== 'function') return null;
  const timeout = Number.isInteger(timeoutMs) && timeoutMs > 0 ? timeoutMs : 1200;
  let timer = null;
  try {
    const task = Promise.resolve().then(() => resolver(...args)).catch(() => null);
    const expiry = new Promise((resolve) => {
      timer = setTimeout(() => resolve(null), timeout);
    });
    return await Promise.race([task, expiry]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

/** One verified forward page. A malformed event makes its whole page retryable. */
async function fetchPage({ client, cursor, currentUserId, resolveAuthorName, resolveChatTitle, timeoutMs, nameResolutionTimeoutMs, signal }) {
  const response = await withDeadline(() => client.getMessagePage(cursor.chatId, {
    count: cursor.limit, from: cursor.afterLogId,
  }), timeoutMs, 'catchup_timeout', signal);
  if (!response || !Array.isArray(response.messages)) throw bridgeError('catchup_invalid_page');
  const raw = response.messages;
  const normalized = raw.map((message) => normalizeCatchUpMessage(message, currentUserId, { chatId: cursor.chatId }));
  if (normalized.some((message) => !message || message.chatId !== cursor.chatId)) throw bridgeError('catchup_invalid_page');
  // The forward API is required; sort oldest-first before slicing even when a
  // server returns a batch larger than count. Never trust next_cursor to skip.
  const forward = dedupeAndOrderMessages(normalized).filter((event) => compareIds(event.logId, cursor.afterLogId) > 0)
    .sort((a, b) => compareIds(a.logId, b.logId));
  const page = forward.slice(0, cursor.limit);
  const title = page.length ? await resolveWithTimeout(resolveChatTitle, [cursor.chatId], nameResolutionTimeoutMs) : null;
  const names = new Map();
  const messages = await Promise.all(page.map(async (event) => {
    let authorName = event.authorName;
    if (!authorName) {
      const key = String(event.authorId);
      if (!names.has(key)) names.set(key, resolveWithTimeout(resolveAuthorName, [cursor.chatId, event.authorId], nameResolutionTimeoutMs));
      authorName = nonEmptyText(await names.get(key), 200);
    }
    return { ...event, ...(title ? { chatTitle: nonEmptyText(title, 200) } : {}), ...(authorName ? { authorName } : {}) };
  }));
  const logId = messages.at(-1)?.logId ?? cursor.afterLogId;
  const complete = response.complete === true && forward.length <= cursor.limit;
  return { messages, progress: { type: 'catchup_progress', chatId: cursor.chatId, logId, complete } };
}

/**
 * Finite round-robin recovery. Default is one pass for standalone consumers;
 * production enables continued pages, streamed callbacks, and an overall bound.
 * Page callbacks are serialized per worker and progress is emitted only after
 * every body callback has settled. Host commits body before progress.
 */
export async function fetchReadOnlyCatchUp({
  client, cursors, currentUserId = null, resolveAuthorName, resolveChatTitle,
  nameResolutionTimeoutMs = 1200, pageTimeoutMs = 10000, signal,
  onMessage, onProgress, maxPasses = 1, maxMessages = MAX_CATCHUP_MESSAGES_TOTAL,
  maxDurationMs = 120000, concurrency = 4, collectMessages = true,
  startOffset = 0,
} = {}) {
  const safeCursors = sanitizeCatchUpCursors(cursors);
  if (!client || typeof client.getMessagePage !== 'function') throw bridgeError('catchup_unavailable');
  const states = safeCursors.map((cursor) => ({ ...cursor, complete: false, failed: false, passes: 0 }));
  const offset = states.length ? Math.abs(Math.trunc(startOffset)) % states.length : 0;
  const visitOrder = [...states.slice(offset), ...states.slice(0, offset)];
  const collected = [];
  const deadline = Date.now() + maxDurationMs;
  let count = 0;
  let pages = 0;
  let stoppedByBound = false;
  let interrupted = null;
  // A pass visits every room before any room receives another page. The next
  // pass continues the remaining watermarks without reconnect or login churn.
  for (let pass = 0; pass < Math.min(Math.max(maxPasses, 1), 128); pass += 1) {
    const active = visitOrder.filter((state) => !state.complete && !state.failed);
    if (!active.length) break;
    let index = 0;
    const workers = Array.from({ length: Math.min(Math.max(concurrency, 1), 8, active.length) }, async () => {
      while (index < active.length) {
        if (signal?.aborted) { interrupted = bridgeError('cancelled'); return; }
        if (Date.now() >= deadline || count >= maxMessages) { stoppedByBound = true; return; }
        const state = active[index++];
        // Reserve count before awaiting so concurrent pages obey the total bound.
        const limit = Math.min(state.limit, maxMessages - count);
        count += limit;
        let actual = 0;
        try {
          const page = await fetchPage({
            client, cursor: { ...state, limit }, currentUserId, resolveAuthorName, resolveChatTitle,
            timeoutMs: Math.min(pageTimeoutMs, Math.max(1, deadline - Date.now())),
            nameResolutionTimeoutMs, signal,
          });
          if (signal?.aborted) throw bridgeError('cancelled');
          for (const event of page.messages) {
            if (signal?.aborted) throw bridgeError('cancelled');
            if (onMessage && await onMessage(event) === false) throw bridgeError('output_closed');
            if (collectMessages) collected.push(event);
          }
          if (onProgress && await onProgress(page.progress) === false) throw bridgeError('output_closed');
          actual = page.messages.length;
          const advanced = compareIds(page.progress.logId, state.afterLogId) > 0;
          state.afterLogId = page.progress.logId;
          state.complete = page.progress.complete;
          state.passes += 1;
          pages += 1;
          if (!advanced && !state.complete) state.failed = true;
        } catch (error) {
          if (['cancelled', 'output_closed', 'output_backpressure'].includes(error?.code)) { interrupted = error; return; }
          state.failed = true;
        } finally { count -= limit - actual; }
        // Explicitly yield between pages, including synchronous fake SDK results.
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    });
    await Promise.all(workers);
    if (interrupted) throw interrupted;
    if (stoppedByBound) break;
  }
  const failedRooms = states.filter((state) => state.failed).length;
  if (states.length && failedRooms === states.length && pages === 0) throw bridgeError('catchup_failed');
  return {
    messages: dedupeAndOrderMessages(collected), messageCount: count,
    partial: states.some((state) => !state.complete), failedRooms,
    nextCursors: states.filter((state, index) => compareIds(state.afterLogId, safeCursors[index].afterLogId) > 0)
      .map(({ chatId, afterLogId }) => ({ chatId, afterLogId })),
    requestedRooms: states.length, pages,
  };
}
