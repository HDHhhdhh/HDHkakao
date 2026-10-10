import { safeError } from './io.mjs';
const ID_PATTERN = /^[0-9A-Za-z._:-]{1,200}$/;

function requiredId(value) {
  if (typeof value !== 'string') return null;
  const normalized = value.trim();
  return ID_PATTERN.test(normalized) ? normalized : null;
}

function nullableName(value) {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function nullableTitle(value) {
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : null;
}

function comparableUserId(value) {
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  const normalized = String(value).trim();
  return normalized.length > 0 ? normalized : null;
}

function isMessageMine(authorId, currentUserId) {
  const author = comparableUserId(authorId);
  const currentUser = comparableUserId(currentUserId);
  return author !== null && currentUser !== null && author === currentUser;
}

function normalizeMessage(event, currentUserId) {
  if (!event || typeof event !== 'object') return null;
  const chatId = requiredId(event.chat_id);
  const logId = requiredId(event.log_id);
  if (!chatId || !logId) return null;
  if (!Number.isSafeInteger(event.author_id) || event.author_id < 0) return null;
  if (!Number.isInteger(event.message_type) || event.message_type < 0) return null;
  if (typeof event.message !== 'string' && event.message_type === 1) return null;
  if (typeof event.sent_at !== 'number' || !Number.isFinite(event.sent_at)) return null;

  return {
    type: 'message',
    chatId,
    logId,
    authorId: event.author_id,
    authorName: nullableName(event.author_name),
    ...(nullableTitle(event.chat_title) ? { chatTitle: nullableTitle(event.chat_title) } : {}),
    message: typeof event.message === 'string' && event.message.length ? event.message : ({ 2: '[사진]', 3: '[동영상]', 5: '[음성 메시지]', 18: '[파일]', 26: '[답장]', 27: '[사진 여러 장]' }[event.message_type] ?? '[메시지]'),
    messageType: event.message_type,
    sentAt: event.sent_at,
    isMine: isMessageMine(event.author_id, currentUserId),
  };
}

function normalizeStatus(status, payload = {}) {
  const result = { type: 'status', status };
  if (status === 'connected' && payload && typeof payload.userId === 'string') {
    result.userId = payload.userId;
  }
  return result;
}

function normalizeError(error) {
  return { type: 'status', status: 'error', ...safeError(error, 'connection_error') };
}

/**
 * Pure boundary between the SDK's event names and Spoiler's NDJSON schema.
 * It deliberately ignores generic SDK packets so raw protocol fields cannot
 * leak through stdout.
 */
export function normalizeListenerEvent(kind, payload, currentUserId) {
  switch (kind) {
    case 'message':
      return normalizeMessage(payload, currentUserId);
    case 'connected':
      return normalizeStatus('connected', payload);
    case 'disconnected':
      return normalizeStatus('disconnected');
    case 'error':
      return normalizeError(payload);
    default:
      return null;
  }
}

/**
 * Attach only read-side listener events. The listener instance is injected so
 * this wiring can be tested with a fake SDK without any network access.
 */
export function attachNormalizedEvents(
  listener,
  emit,
  resolveAuthorName,
  nameResolutionTimeoutMs = 1200,
  currentUserId = null,
  maxPendingMessages = 1024,
  resolveChatTitle = null,
) {
  let accepting = true;
  let messageQueue = Promise.resolve();
  let pendingMessages = 0;
  let pendingBytes = 0;
  const metadataLookups = new Map();
  const titleCache = new Map();
  const waitingLookups = [];
  let activeLookups = 0;

  const cachedTitle = (chatId) => {
    const cached = titleCache.get(chatId);
    if (!cached) return null;
    titleCache.delete(chatId);
    if (cached.expiresAt <= Date.now()) return null;
    titleCache.set(chatId, cached);
    return cached.title;
  };

  const rememberTitle = (chatId, title) => {
    titleCache.delete(chatId);
    titleCache.set(chatId, { title, expiresAt: Date.now() + 60_000 });
    while (titleCache.size > 256) titleCache.delete(titleCache.keys().next().value);
  };

  const startMetadataLookups = () => {
    while (activeLookups < 4 && waitingLookups.length) {
      const lookup = waitingLookups.shift();
      if (!lookup.waiters.size) continue;
      lookup.started = true;
      activeLookups += 1;
      const finish = (value) => {
        let name = lookup.field === 'chatTitle' ? nullableTitle(value) : nullableName(value);
        if (lookup.field === 'chatTitle') {
          // A real title supplied by a newer SDK event wins over a lookup
          // that started before that event arrived.
          name = cachedTitle(lookup.chatId) ?? name;
          if (name) rememberTitle(lookup.chatId, name);
        }
        activeLookups -= 1;
        metadataLookups.delete(lookup.key);
        for (const waiter of [...lookup.waiters]) waiter.finish(name);
        startMetadataLookups();
      };
      // A message deadline does not cancel the SDK. Keep this slot until the
      // real operation settles, including when every message has timed out.
      Promise.resolve().then(() => lookup.resolver(lookup.chatId, lookup.authorId))
        .then(finish, () => finish(null));
    }
  };

  const resolveMetadata = (event, field, resolver) => {
    if (event[field]) return Promise.resolve(event[field]);
    if (typeof resolver !== 'function') return Promise.resolve(null);
    const key = field === 'chatTitle' ? `title\u001f${event.chatId}` : `author\u001f${event.chatId}\u001f${event.authorId}`;
    let lookup = metadataLookups.get(key);
    if (!lookup) {
      lookup = { key, field, resolver, chatId: event.chatId, authorId: event.authorId, waiters: new Set(), started: false };
      metadataLookups.set(key, lookup);
      waitingLookups.push(lookup);
    }
    return new Promise((resolve) => {
      const waiter = {
        finish(name) {
          if (!lookup.waiters.delete(waiter)) return;
          clearTimeout(timeoutId);
          if (!lookup.started && !lookup.waiters.size) {
            metadataLookups.delete(key);
            const index = waitingLookups.indexOf(lookup);
            if (index !== -1) waitingLookups.splice(index, 1);
          }
          resolve(name);
        },
      };
      // Waiters can be removed on timeout without attaching another callback
      // to a permanently hung SDK promise. Queued work expires with its body.
      const timeoutId = setTimeout(() => waiter.finish(null), nameResolutionTimeoutMs);
      lookup.waiters.add(waiter);
      startMetadataLookups();
    });
  };

  const resolveMessage = (event) => {
    if (event.chatTitle) rememberTitle(event.chatId, event.chatTitle);
    const knownTitle = event.chatTitle ?? cachedTitle(event.chatId);
    const author = resolveMetadata(event, 'authorName', resolveAuthorName);
    const title = knownTitle ? Promise.resolve(knownTitle) : resolveMetadata(event, 'chatTitle', resolveChatTitle);
    return Promise.all([author, title]).then(([authorName, chatTitle]) => ({
      ...event,
      ...(authorName ? { authorName } : {}),
      ...(chatTitle ? { chatTitle } : {}),
    }));
  };

  const subscriptions = ['message', 'connected', 'disconnected', 'error'].map((kind) => {
    const handler = (payload) => {
      const event = normalizeListenerEvent(kind, payload, currentUserId);
      if (!event || !accepting) return;
      if (event.type === 'message') {
        const bytes = Buffer.byteLength(event.message);
        if (pendingMessages >= maxPendingMessages || pendingBytes + bytes > 32 * 1024 * 1024) {
          accepting = false;
          emit(normalizeError({ code: 'event_queue_full' }));
          return;
        }
        pendingMessages += 1;
        pendingBytes += bytes;
        // Metadata deadlines run concurrently while SDK lookups are bounded and
        // shared. Delivery still preserves the SDK's receive order.
        const resolvedEvent = resolveMessage(event);
        const deliver = async () => {
          const ready = await resolvedEvent;
          // The event was accepted before detach. Always finish delivering it
          // so a disconnect cannot erase a message while its name is loading.
          try { await emit(ready); }
          finally { pendingMessages -= 1; pendingBytes -= bytes; }
        };
        messageQueue = messageQueue.then(deliver, deliver).catch(() => {});
        return;
      }
      emit(event);
    };
    listener.on(kind, handler);
    return { kind, handler };
  });

  return () => {
    accepting = false;
    for (const { kind, handler } of subscriptions) {
      if (typeof listener.off === 'function') listener.off(kind, handler);
    }
    return messageQueue.catch(() => {});
  };
}
