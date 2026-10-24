import { Long } from 'bson';
import { isMissingChannelInfo, readDirectRoomMetadataForClient } from '../../../bridge/kakao_direct_metadata.mjs';

const MAX_ID = 9223372036854775807n;
const SOURCES = Object.freeze({ account: 'synced_room_name', friend: 'synced_friend_name',
  shared: 'server_title', members: 'participant_names' });

function record(value) { return value && typeof value === 'object' && !Array.isArray(value) ? value : null; }
function positiveId(value) {
  if (Long.isLong(value)) value = value.toString();
  else if (typeof value === 'bigint') value = value.toString();
  else if (typeof value === 'number' && Number.isSafeInteger(value)) value = String(value);
  if (typeof value !== 'string' || !/^[1-9]\d{0,18}$/.test(value)) return null;
  return BigInt(value) <= MAX_ID ? value : null;
}
// Display text is never normalized. Normalization belongs to a separate search
// key, otherwise a saved full-width name or intentional spacing is lost.
function displayName(value) {
  return typeof value === 'string' && value.length <= 256 && value.trim().length
    && !/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/u.test(value) ? value : null;
}
function emptyName(value) {
  return typeof value === 'string' && value.length <= 256 && !value.trim().length
    && !/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/u.test(value);
}
function bounded(value, fallback, minimum, maximum) {
  return Number.isInteger(value) ? Math.max(minimum, Math.min(maximum, value)) : fallback;
}

function checkedInfo(response, chatId) {
  if (!record(response) || response.statusCode !== 0
    || (response.method !== undefined && response.method !== 'CHATINFO')) return null;
  const body = record(response.body);
  if (!body || (body.status !== undefined && body.status !== 0)) return null;
  const info = record(body.chatInfo);
  return info && info.left !== true && positiveId(info.chatId) === chatId ? info : null;
}

function memberView(info, ownId) {
  if (!Array.isArray(info.displayMembers) || info.displayMembers.length > 100) return null;
  const members = [];
  const ids = new Set();
  for (const raw of info.displayMembers) {
    const member = record(raw);
    const id = member && positiveId(member.userId);
    if (!id || ids.has(id)) return null;
    ids.add(id);
    members.push({ id, name: displayName(member.nickName) });
  }
  const others = members.filter(member => member.id !== ownId);
  // Numeric chat type guesses must not turn a two-person group into a DM.
  const peer = ownId && info.type === 'DirectChat' && info.activeMembersCount === 2
    && members.length <= 2 && others.length === 1 ? others[0].id : null;
  return { others, peer };
}

function sharedName(info) {
  if (!Array.isArray(info.chatMetas) || info.chatMetas.length > 100) return null;
  const titles = info.chatMetas.filter(meta => record(meta)?.type === 3);
  return titles.length === 1 ? displayName(titles[0].content) : null;
}
function participantsName(view) {
  if (!view) return null;
  const names = [...new Set(view.others.map(member => member.name).filter(Boolean))];
  return displayName(names.slice(0, 3).join(', ') + (names.length > 3 ? ' …' : ''));
}

function friendAlias(friend, expectedId = null) {
  const input = record(friend);
  const id = input && positiveId(input.userId);
  if (!id || (expectedId && id !== expectedId) || input.purged === true) return null;
  const rawChatId = input.directChatId;
  const absentChatId = rawChatId === undefined || rawChatId === null || rawChatId === ''
    || rawChatId === 0 || rawChatId === '0';
  const chatId = absentChatId ? null : positiveId(rawChatId);
  if (!absentChatId && !chatId) return null;
  const name = displayName(input.friendNickName);
  // An explicit empty string means no synchronized alias. A missing/malformed
  // field is an incomplete response, not permission to erase a previous name.
  if (!name && !emptyName(input.friendNickName)) return null;
  return { name, chatId };
}

function aliasMap(body, maxAliases) {
  if (!record(body) || body.status !== 0 || !Array.isArray(body.friends)
    || body.friends.length > maxAliases) return null;
  const aliases = new Map();
  for (const raw of body.friends) {
    const friend = record(raw);
    const id = friend && positiveId(friend.userId);
    if (!id) continue;
    if (aliases.has(id)) { aliases.set(id, null); continue; }
    aliases.set(id, friendAlias(friend));
  }
  return aliases;
}

/**
 * Read-only metadata owner for one authenticated Spoiler session. It never
 * logs raw responses, persists credentials, creates rooms, or marks messages read.
 * meta.name is the server's account setting; unsynchronized PC-only names are
 * not inferred from public nicknames or claimed to be identical.
 */
export function createRoomNameResolver({ client, credentials, onMetadata = () => true,
  fetchImpl = globalThis.fetch, lookupTimeoutMs = 1200, metadataTtlMs = 60000,
  aliasTtlMs = 300000, aliasTimeoutMs = 15000, retryDelayMs = 5000,
  maxCachedRooms = 256, maxPendingRooms = 256, maxAliases = 10000,
  concurrency = 4, now = Date.now, aliasConfig = {} } = {}) {
  const capacity = bounded(maxCachedRooms, 256, 1, 256);
  const pendingCapacity = bounded(maxPendingRooms, 256, 1, 256);
  const parallel = bounded(concurrency, 4, 1, 4);
  const aliasLimit = bounded(maxAliases, 10000, 1, 10000);
  const callerDeadline = bounded(lookupTimeoutMs, 1200, 1, 30000);
  const metadataTtl = bounded(metadataTtlMs, 60000, 1, 300000);
  const aliasTtl = bounded(aliasTtlMs, 300000, 1, 1800000);
  const aliasDeadline = bounded(aliasTimeoutMs, 15000, 1, 30000);
  const retryDelay = bounded(retryDelayMs, 5000, 1, 60000);
  const ownId = positiveId(credentials?.userId ?? credentials?.user_id);
  const cache = new Map();
  const jobs = new Map();
  const queue = [];
  const controllers = new Set();
  const targetCache = new Map();
  const targetFlights = new Map();
  let active = 0;
  let closed = false;
  let aliasCache = null;
  let aliasFlight = null;
  let retryAliasesAfter = 0;

  const cached = (chatId) => {
    const value = cache.get(chatId);
    if (!value) return null;
    cache.delete(chatId); cache.set(chatId, value);
    return value;
  };
  const remember = (chatId, value) => {
    cache.delete(chatId); cache.set(chatId, value);
    while (cache.size > capacity) cache.delete(cache.keys().next().value);
  };
  const owned = (job) => !closed && (job.liveOwner || [...job.signals].some(signal => !signal.aborted));

  const requestJson = async (path) => {
    if (closed) return null;
    // Obtain current client credentials if available, because refresh may have
    // rotated OAuth while this metadata resolver remains in the same session.
    let current = credentials;
    try { if (typeof client?.getCredentials === 'function') current = client.getCredentials(); }
    catch { return null; }
    const token = current?.oauthToken ?? current?.oauth_token;
    const uuid = current?.deviceUuid ?? current?.device_uuid;
    if (positiveId(current?.userId ?? current?.user_id) !== ownId
      || typeof token !== 'string' || !token || typeof uuid !== 'string' || !uuid
      || typeof fetchImpl !== 'function') return null;
    const platform = (current?.deviceType ?? current?.device_type ?? 'tablet') === 'pc' ? 'mac' : 'android';
    const version = String(aliasConfig.appVersion ?? (platform === 'android' ? '25.9.2' : '26.2.0'));
    const language = String(aliasConfig.language ?? 'ko');
    const osVersion = String(aliasConfig.osVersion ?? (platform === 'android' ? '13' : 'Windows'));
    const controller = new AbortController();
    controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), aliasDeadline);
    try {
        const response = await fetchImpl(`https://katalk.kakao.com/${platform}/friends/${path}`, {
          method: 'GET', signal: controller.signal,
          headers: { Authorization: `${token}-${uuid}`, A: `${platform}/${version}/${language}`,
            'User-Agent': `KT/${version} ${platform === 'android' ? 'An' : 'Md'}/${osVersion} ${language}`,
            Accept: '*/*', 'Accept-Language': language },
        });
        if (!response?.ok || controller.signal.aborted || closed) return null;
        const body = await response.json();
        return controller.signal.aborted || closed ? null : body;
      } catch { return null; }
      finally { clearTimeout(timeout); controllers.delete(controller); }
  };
  const fetchAliases = async () => {
    if (closed) return null;
    if (aliasCache && aliasCache.expiresAt > now()) return aliasCache.map;
    if (aliasFlight) return aliasFlight;
    if (retryAliasesAfter > now()) return null;
    const task = (async () => {
      const query = new URLSearchParams({ type: '["plus","normal"]', event_types: '["create"]', token: '0' });
      const map = aliasMap(await requestJson(`list.json?${query}`), aliasLimit);
      if (map && !closed) { aliasCache = { map, expiresAt: now() + aliasTtl }; retryAliasesAfter = 0; }
      return map;
    })();
    aliasFlight = task;
    try {
      const result = await task;
      if (!result && !closed) retryAliasesAfter = now() + retryDelay;
      return result;
    } finally { if (aliasFlight === task) aliasFlight = null; }
  };

  const targetedAlias = async (peerId) => {
    if (closed) return null;
    const previous = targetCache.get(peerId);
    if (previous) {
      targetCache.delete(peerId); targetCache.set(peerId, previous);
      if (previous.retryAfter > now()) return null;
      if (previous.expiresAt > now()) return previous.alias;
    }
    if (targetFlights.has(peerId)) return targetFlights.get(peerId);
    const task = (async () => {
      // Primary FriendFindIdStruct is { token, friend: FriendStruct }.
      // Do not interpret profile3/member wrappers or unnamed flat responses.
      const body = await requestJson(`${peerId}.json`);
      const alias = record(body)?.status === 0 ? friendAlias(body.friend, peerId) : null;
      if (closed) return null;
      const entry = alias ? { alias, expiresAt: now() + aliasTtl, retryAfter: 0 }
        : { alias: previous?.alias ?? null, expiresAt: 0, retryAfter: now() + retryDelay };
      targetCache.delete(peerId); targetCache.set(peerId, entry);
      while (targetCache.size > capacity) targetCache.delete(targetCache.keys().next().value);
      return alias;
    })();
    targetFlights.set(peerId, task);
    try { return await task; }
    finally { if (targetFlights.get(peerId) === task) targetFlights.delete(peerId); }
  };

  const peerAlias = async (peerId, chatId) => {
    const aliases = await fetchAliases();
    if (closed) return { alias: null, checked: false };
    if (aliases?.has(peerId)) {
      const alias = aliases.get(peerId);
      // Duplicate/malformed or contradictory records fail closed rather than
      // being silently bypassed through a second endpoint.
      if (!alias || (alias.chatId && alias.chatId !== chatId)) return { alias: null, checked: false };
      if (alias.name) return { alias, checked: true };
    }
    const alias = await targetedAlias(peerId);
    return { alias: alias && (!alias.chatId || alias.chatId === chatId) ? alias : null,
      checked: !!alias && (!alias.chatId || alias.chatId === chatId) };
  };

  const resolveRoom = async (job) => {
    const previous = cached(job.chatId);
    try {
      if (!owned(job) || typeof client?.acquireSession !== 'function') return null;
      const session = await client.acquireSession();
      if (!owned(job) || typeof session?.getChannelInfo !== 'function') return null;
      // Pinned SDK acquireSession() returns LocoSession itself, not SessionState.
      const response = await session.getChannelInfo(Long.fromString(job.chatId));
      let info = checkedInfo(response, job.chatId);
      let fallbackView = null;
      if (!info && owned(job) && ownId && isMissingChannelInfo(response)) {
        const verified = await readDirectRoomMetadataForClient(() => client, Long, job.chatId, ownId,
          { isActive: () => owned(job) });
        const peer = verified.snapshot.members.find(member => member.user_id !== ownId);
        fallbackView = { peer: peer.user_id, others: [{ id: peer.user_id, name: displayName(peer.nickname) }] };
      }
      if ((!info && !fallbackView) || !owned(job)) return owned(job) ? previous?.chatTitle ?? null : null;
      const completeChannelInfo = !!info;
      info = info ?? {};
      let chatTitle = displayName(record(info.meta)?.name);
      let titleSource = chatTitle ? SOURCES.account : null;
      const rawMeta = info.meta;
      const accountChecked = rawMeta === undefined || rawMeta === null
        || (record(rawMeta) && (rawMeta.name === undefined || rawMeta.name === null
          || emptyName(rawMeta.name) || chatTitle !== null));
      // CHATONROOM proves the peer, but does not prove that an account-specific
      // room title was removed. It must not erase a stronger saved room name.
      let authoritative = completeChannelInfo && !!accountChecked;
      const view = fallbackView ?? memberView(info, ownId);
      if (!chatTitle && !['DirectChat', 'MultiChat', 'MemoChat', 'PlusChat', 'OM', 'OD'].includes(info.type)) authoritative = false;
      if (!chatTitle && info.type === 'DirectChat' && !view?.peer) authoritative = false;
      if (!chatTitle && view?.peer) {
        const { alias, checked } = await peerAlias(view.peer, job.chatId);
        if (!owned(job)) return null;
        authoritative = authoritative && checked;
        if (alias?.name) {
          chatTitle = alias.name; titleSource = SOURCES.friend;
        } else if (!checked && previous?.titleSource === SOURCES.friend) {
          // A failed HTTP refresh is not evidence that a good saved alias was
          // removed. Keep it until a successful snapshot says otherwise.
          chatTitle = previous.chatTitle; titleSource = previous.titleSource;
        }
      }
      if (!chatTitle) {
        chatTitle = sharedName(info); titleSource = chatTitle ? SOURCES.shared : null;
        if (info.chatMetas !== undefined && info.chatMetas !== null) {
          authoritative = authoritative && Array.isArray(info.chatMetas) && info.chatMetas.length <= 100
            && info.chatMetas.filter(meta => record(meta)?.type === 3).every(meta =>
              emptyName(meta.content) || displayName(meta.content) !== null)
            && info.chatMetas.filter(meta => record(meta)?.type === 3).length <= 1;
        }
      }
      if (!chatTitle) { chatTitle = participantsName(view); titleSource = chatTitle ? SOURCES.members : null; }
      if (!chatTitle || !owned(job)) return owned(job) ? previous?.chatTitle ?? null : null;
      const priority = source => [SOURCES.account, SOURCES.friend, SOURCES.shared, SOURCES.members].indexOf(source);
      if (!authoritative && previous && priority(previous.titleSource) <= priority(titleSource)) {
        chatTitle = previous.chatTitle; titleSource = previous.titleSource;
      }
      const value = { chatTitle, titleSource, expiresAt: now() + metadataTtl };
      const changed = !previous || previous.chatTitle !== chatTitle || previous.titleSource !== titleSource;
      if (changed || previous?.publicationPending || job.refreshOwner) {
        const accepted = await onMetadata({ type: 'room_metadata', chatId: job.chatId, chatTitle, titleSource, authoritative });
        if (!owned(job)) return null;
        if (accepted === false) { value.publicationPending = true; value.expiresAt = 0; }
      }
      remember(job.chatId, value);
      return chatTitle;
    } catch { return owned(job) ? previous?.chatTitle ?? null : null; }
  };

  const clearOwners = job => {
    for (const [signal, abort] of job.abortHandlers) signal.removeEventListener('abort', abort);
    job.abortHandlers.clear(); job.signals.clear();
  };

  const pump = () => {
    while (!closed && active < parallel && queue.length) {
      const job = queue.shift();
      if (!owned(job)) { jobs.delete(job.chatId); clearOwners(job); job.finish(null); continue; }
      active++;
      // Caller deadlines never release this slot. The underlying SDK operation
      // must actually settle before another room can begin.
      const finish = value => { active--; jobs.delete(job.chatId); clearOwners(job); job.finish(value); pump(); };
      Promise.resolve().then(() => resolveRoom(job)).then(finish, () => finish(null));
    }
  };
  const request = (chatId, signal) => {
    if (closed || signal?.aborted) return null;
    let job = jobs.get(chatId);
    if (!job) {
      if (jobs.size >= pendingCapacity) return null;
      let finish;
      const promise = new Promise(resolve => { finish = resolve; });
      job = { chatId, promise, finish, liveOwner: false, refreshOwner: false, signals: new Set(), abortHandlers: new Map() };
      jobs.set(chatId, job); queue.push(job);
    }
    if (signal && !job.signals.has(signal)) {
      const abort = () => { job.signals.delete(signal); job.abortHandlers.delete(signal); signal.removeEventListener('abort', abort); };
      job.signals.add(signal); job.abortHandlers.set(signal, abort); signal.addEventListener('abort', abort, { once: true });
    } else if (!signal) job.liveOwner = true;
    pump();
    return job;
  };
  const wait = (job, signal, fallback, deadline = callerDeadline) => {
    if (!job || closed || signal?.aborted) return Promise.resolve(closed || signal?.aborted ? null : fallback);
    return new Promise(resolve => {
      let settled = false;
      const finish = value => { if (settled) return; settled = true; clearTimeout(timer); signal?.removeEventListener('abort', abort); resolve(value); };
      const abort = () => finish(null);
      const timer = deadline === null ? null : setTimeout(() => finish(closed || signal?.aborted ? null : fallback), deadline);
      signal?.addEventListener('abort', abort, { once: true });
      job.promise.then(finish, () => finish(fallback));
    });
  };

  return Object.freeze({
    async getChatTitle(chatId) {
      const id = positiveId(chatId);
      if (!id || closed) return null;
      const value = cached(id);
      if (value?.expiresAt > now()) return value.chatTitle;
      return wait(request(id), null, value?.chatTitle ?? null);
    },
    async refresh(chatIds, signal) {
      if (closed || signal?.aborted || !Array.isArray(chatIds)) return 0;
      const ids = new Set();
      for (const raw of chatIds) {
        const id = positiveId(raw);
        if (id) ids.add(id);
        if (ids.size === 80) break;
      }
      const results = await Promise.all([...ids].map(id => {
        const value = cached(id);
        if (value?.expiresAt > now()) return value.chatTitle;
        const job = request(id, signal);
        if (job) job.refreshOwner = true;
        return wait(job, signal, value?.chatTitle ?? null, null);
      }));
      return closed || signal?.aborted ? 0 : results.filter(Boolean).length;
    },
    close() {
      if (closed) return;
      closed = true;
      for (const controller of controllers) controller.abort();
      for (const job of jobs.values()) clearOwners(job);
      for (const job of queue.splice(0)) { jobs.delete(job.chatId); job.finish(null); }
      cache.clear(); aliasCache = null; targetCache.clear();
    },
  });
}
