import assert from 'node:assert/strict';
import test from 'node:test';
import { Long } from 'bson';
import { createRoomNameResolver } from '../src/room-names.mjs';

const turn = () => new Promise(resolve => setImmediate(resolve));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const credentials = { userId: '7', oauthToken: 'fixture-token', deviceUuid: 'fixture-device', deviceType: 'tablet' };
const packet = (id = '101', changes = {}) => ({ method: 'CHATINFO', statusCode: 0,
  body: { chatInfo: { chatId: Long.fromString(id), type: 'DirectChat', activeMembersCount: 2,
    displayMembers: [{ userId: Long.fromNumber(7), nickName: '나' }, { userId: Long.fromNumber(8), nickName: '공개 이름' }],
    ...changes } } });
const response = friends => ({ ok: true, async json() { return { status: 0, friends }; } });
const alias = (changes = {}) => ({ userId: '8', friendNickName: '저장 이름', directChatId: '101', ...changes });

function fixture({ channel = id => packet(id), fetchImpl = async () => response([]), ...options } = {}) {
  const messages = [];
  const calls = [];
  const writes = [];
  const session = { async getChannelInfo(id) { assert.ok(Long.isLong(id)); calls.push(id.toString()); return channel(id.toString()); } };
  const client = { async acquireSession() { return session; } };
  for (const target of [session, client]) for (const name of ['markRead', 'sendMessage', 'sendTyping', 'getChatInfo', 'openChat', 'login'])
    target[name] = () => { writes.push(name); throw new Error('forbidden side effect'); };
  const resolver = createRoomNameResolver({ client, credentials, fetchImpl,
    onMetadata: event => { messages.push(event); return true; }, ...options });
  return { resolver, messages, calls, writes, client };
}

test('account room name wins and raw display Unicode and spaces are preserved', async () => {
  let requests = 0;
  const name = '  Ａ 저장 👨‍👩‍👧 이름  ';
  const f = fixture({ channel: id => packet(id, { meta: { name }, chatMetas: [{ type: 3, content: '공유 제목' }] }),
    fetchImpl: () => { requests++; throw new Error('not needed'); } });
  assert.equal(await f.resolver.getChatTitle('101'), name);
  assert.deepEqual(f.messages, [{ type: 'room_metadata', chatId: '101', chatTitle: name, titleSource: 'synced_room_name', authoritative: true }]);
  assert.equal(requests, 0); assert.deepEqual(f.writes, []); f.resolver.close();
});

test('verified friend alias wins over shared title and drops unrelated response data', async () => {
  const f = fixture({ channel: id => packet(id, { chatMetas: [{ type: 3, content: '공유 제목' }], lastChatLog: { message: 'PRIVATE BODY' } }),
    fetchImpl: async (url, options) => {
      assert.match(url, /^https:\/\/katalk\.kakao\.com\/android\/friends\/list\.json\?/);
      assert.equal(options.method, 'GET'); assert.equal(options.headers.Authorization, 'fixture-token-fixture-device');
      return response([alias({ friendNickName: ' Ｂ 내 이름 ', phoneNumber: 'PRIVATE PHONE', UUID: 'PRIVATE UUID' })]);
    } });
  assert.equal(await f.resolver.getChatTitle('101'), ' Ｂ 내 이름 ');
  assert.equal(f.messages[0].titleSource, 'synced_friend_name');
  assert.equal(JSON.stringify(f.messages).includes('PRIVATE'), false); assert.deepEqual(f.writes, []); f.resolver.close();
});

test('shared title then participant names are explicitly distinguished', async () => {
  const shared = fixture({ channel: id => packet(id, { type: 'MultiChat', chatMetas: [{ type: 3, content: '공유 제목' }] }) });
  assert.equal(await shared.resolver.getChatTitle('101'), '공유 제목'); assert.equal(shared.messages[0].titleSource, 'server_title'); shared.resolver.close();
  const fallback = fixture();
  assert.equal(await fallback.resolver.getChatTitle('101'), '공개 이름'); assert.equal(fallback.messages[0].titleSource, 'participant_names'); fallback.resolver.close();
});

test('CHATINFO IDs, status, method, body status and left rooms fail closed', async () => {
  const invalid = [
    { ...packet('102'), statusCode: 0 }, { ...packet(), statusCode: 1 },
    { ...packet(), statusCode: undefined }, { ...packet(), method: 'CHATONROOM' },
    { ...packet(), body: { ...packet().body, status: -1 } }, packet('101', { left: true }),
    packet('101', { chatId: '0101' }), packet('101', { chatId: '9223372036854775808' }),
  ];
  for (const raw of invalid) {
    const f = fixture({ channel: () => raw });
    assert.equal(await f.resolver.getChatTitle('101'), null); assert.deepEqual(f.messages, []); f.resolver.close();
  }
  const f = fixture();
  for (const id of ['', '0', '-1', '0101', ' 101 ', '9223372036854775808', Number.MAX_SAFE_INTEGER + 1])
    assert.equal(await f.resolver.getChatTitle(id), null);
  assert.deepEqual(f.calls, []); f.resolver.close();
});

test('friend alias requires exact DirectChat type, two active members and one proven peer', async () => {
  const cases = [
    { type: 11 }, { type: 'MultiChat' }, { activeMembersCount: 3 },
    { displayMembers: [{ userId: 7, nickName: '나' }] },
    { displayMembers: [{ userId: 8, nickName: '공개 이름' }, { userId: 9, nickName: '둘째' }] },
    { displayMembers: [{ userId: 8, nickName: '공개 이름' }, { userId: 8, nickName: '중복' }] },
    { displayMembers: [{ userId: Number.MAX_SAFE_INTEGER + 1, nickName: '부정확 ID' }] },
  ];
  for (const info of cases) {
    let requests = 0;
    const f = fixture({ channel: id => packet(id, info), fetchImpl: async () => { requests++; return response([alias()]); } });
    await f.resolver.getChatTitle('101'); assert.equal(requests, 0); assert.ok(f.messages.every(event => event.titleSource !== 'synced_friend_name')); f.resolver.close();
  }
});

test('one authoritative display peer plus own account and two active members is sufficient', async () => {
  const f = fixture({ channel: id => packet(id, { displayMembers: [{ userId: 8, nickName: '공개 이름' }] }), fetchImpl: async () => response([alias()]) });
  assert.equal(await f.resolver.getChatTitle('101'), '저장 이름'); f.resolver.close();
});

test('duplicate alias users, mismatched rooms and malformed IDs never supply aliases', async () => {
  for (const friends of [[alias(), alias()], [alias(), alias({ friendNickName: '' })], [alias({ directChatId: '999' })],
    [alias({ directChatId: '0101' })], [alias({ userId: '9' })], [alias({ directChatId: Number.MAX_SAFE_INTEGER + 1 })]]) {
    const f = fixture({ fetchImpl: async () => response(friends) });
    assert.equal(await f.resolver.getChatTitle('101'), '공개 이름'); assert.equal(f.messages[0].titleSource, 'participant_names'); f.resolver.close();
  }
});

test('invalid names are rejected without transforming valid fallback names', async () => {
  for (const name of ['', '   ', 'x\nname', 'x\u0085name', 'x\u202ename', 'x'.repeat(257), 3]) {
    const f = fixture({ channel: id => packet(id, { meta: { name }, chatMetas: [{ type: 3, content: ' fallback ' }] }), fetchImpl: async () => response([alias({ friendNickName: name })]) });
    assert.equal(await f.resolver.getChatTitle('101'), ' fallback '); assert.equal(f.messages[0].titleSource, 'server_title'); f.resolver.close();
  }
});

test('own-only room never uses own author as a room name', async () => {
  const f = fixture({ channel: id => packet(id, { type: 'MemoChat', activeMembersCount: 1, displayMembers: [{ userId: 7, nickName: '나' }] }) });
  assert.equal(await f.resolver.getChatTitle('101'), null); assert.deepEqual(f.messages, []); f.resolver.close();
});

test('caller timeout retains the SDK slot and publishes late metadata once', async () => {
  let finish;
  const f = fixture({ lookupTimeoutMs: 10, channel: id => new Promise(resolve => { finish = () => resolve(packet(id, { meta: { name: '늦은 저장 이름' } })); }) });
  assert.equal(await f.resolver.getChatTitle('101'), null); assert.deepEqual(f.messages, []);
  finish(); await turn(); await turn();
  assert.equal(f.messages.length, 1); assert.equal(await f.resolver.getChatTitle('101'), '늦은 저장 이름'); assert.deepEqual(f.calls, ['101']); f.resolver.close();
});

test('burst calls coalesce and active unresolved operations never exceed four after caller timeouts', async () => {
  const pending = new Map();
  let live = 0; let peak = 0;
  const f = fixture({ lookupTimeoutMs: 10, channel: id => {
    live++; peak = Math.max(peak, live);
    return new Promise(resolve => pending.set(id, () => { live--; resolve(packet(id, { meta: { name: `방 ${id}` } })); }));
  } });
  await Promise.all(Array.from({ length: 30 }, (_, index) => f.resolver.getChatTitle(String(index % 10 + 101))));
  assert.equal(f.calls.length, 4); assert.equal(peak, 4); assert.equal(f.messages.length, 0);
  for (let pass = 0; pass < 3; pass++) { for (const [id, finish] of [...pending]) { pending.delete(id); finish(); } await turn(); await turn(); }
  assert.equal(f.calls.length, 10); assert.equal(f.messages.length, 10); assert.equal(peak, 4); f.resolver.close();
});

test('aborted refresh suppresses late metadata while a coalesced live owner remains eligible', async () => {
  let finish;
  const f = fixture({ lookupTimeoutMs: 100, channel: id => new Promise(resolve => { finish = () => resolve(packet(id, { meta: { name: '저장 제목' } })); }) });
  const controller = new AbortController(); const refresh = f.resolver.refresh(['101'], controller.signal);
  await turn(); controller.abort(); assert.equal(await refresh, 0); finish(); await turn(); await turn(); assert.deepEqual(f.messages, []);
  const second = new AbortController(); const refresh2 = f.resolver.refresh(['101'], second.signal);
  const live = f.resolver.getChatTitle('101'); await turn(); second.abort(); finish();
  assert.equal(await refresh2, 0); assert.equal(await live, '저장 제목'); assert.equal(f.messages.length, 1); f.resolver.close();
});

test('close rejects queued work and never publishes a stale late completion', async () => {
  const pending = [];
  const f = fixture({ lookupTimeoutMs: 10, channel: id => new Promise(resolve => pending.push(() => resolve(packet(id, { meta: { name: '저장 제목' } })))) });
  const calls = Array.from({ length: 10 }, (_, index) => f.resolver.getChatTitle(String(index + 101)));
  await turn(); f.resolver.close(); pending.forEach(finish => finish()); await Promise.all(calls); await turn();
  assert.equal(f.calls.length, 4); assert.deepEqual(f.messages, []); assert.equal(await f.resolver.getChatTitle('101'), null);
});

test('refresh covers known rooms without history bodies, obeys TTL and renews unchanged metadata', async () => {
  let clock = 1; let name = '이전 저장 제목';
  const f = fixture({ now: () => clock, metadataTtlMs: 5, channel: id => packet(id, { meta: { name } }) });
  assert.equal(await f.resolver.refresh(['101']), 1); assert.equal(f.messages.length, 1);
  assert.equal(await f.resolver.refresh(['101']), 1); assert.equal(f.calls.length, 1);
  clock += 6; await f.resolver.refresh(['101']); assert.equal(f.calls.length, 2); assert.equal(f.messages.length, 2);
  name = '새 저장 제목'; clock += 6; await f.resolver.refresh(['101']); assert.equal(f.messages.length, 3); assert.equal(f.messages[2].chatTitle, name); f.resolver.close();
});

test('failed CHATINFO and failed alias refresh preserve previous good metadata and retry', async () => {
  let clock = 1; let channelFails = false; let aliasFails = false; let name = '저장 이름';
  const f = fixture({ now: () => clock, metadataTtlMs: 2, aliasTtlMs: 2, retryDelayMs: 1,
    channel: id => { if (channelFails) throw new Error('synthetic'); return packet(id); },
    fetchImpl: async () => { if (aliasFails) throw new Error('synthetic'); return response([alias({ friendNickName: name })]); } });
  assert.equal(await f.resolver.getChatTitle('101'), name);
  clock += 3; channelFails = true; assert.equal(await f.resolver.getChatTitle('101'), name);
  clock += 3; channelFails = false; aliasFails = true; assert.equal(await f.resolver.getChatTitle('101'), name); assert.equal(f.messages.length, 1);
  clock += 3; aliasFails = false; name = '바뀐 저장 이름'; assert.equal(await f.resolver.getChatTitle('101'), name); assert.equal(f.messages.length, 2); f.resolver.close();
});

test('room LRU cache, pending room queue and refresh lists are bounded', async () => {
  const f = fixture({ maxCachedRooms: 2, channel: id => packet(id, { meta: { name: `방 ${id}` } }) });
  await f.resolver.getChatTitle('101'); await turn(); await f.resolver.getChatTitle('102'); await turn();
  await f.resolver.getChatTitle('103'); await turn(); await f.resolver.getChatTitle('101'); assert.deepEqual(f.calls, ['101', '102', '103', '101']); f.resolver.close();
  const boundedQueue = fixture({ maxPendingRooms: 2, lookupTimeoutMs: 5, channel: () => new Promise(() => {}) });
  await Promise.all(['101', '102', '103'].map(id => boundedQueue.resolver.getChatTitle(id))); assert.equal(boundedQueue.calls.length, 2); boundedQueue.resolver.close();
  const rooms = fixture({ channel: id => packet(id, { meta: { name: `방 ${id}` } }) });
  await rooms.resolver.refresh(Array.from({ length: 100 }, (_, index) => String(index + 101))); assert.equal(rooms.calls.length, 80); rooms.resolver.close();
});

test('alias requests coalesce, expire and reject oversized or malformed responses', async () => {
  let clock = 1; let requests = 0;
  const f = fixture({ now: () => clock, metadataTtlMs: 2, aliasTtlMs: 10,
    fetchImpl: async () => { requests++; return response([alias({ directChatId: undefined })]); } });
  await Promise.all(['101', '102', '103', '104'].map(id => f.resolver.getChatTitle(id))); assert.equal(requests, 1);
  clock += 3; await f.resolver.getChatTitle('101'); assert.equal(requests, 1);
  clock += 11; await f.resolver.getChatTitle('101'); assert.equal(requests, 2); f.resolver.close();
  for (const fetchImpl of [async () => response([alias(), alias({ userId: '9' })]),
    async () => ({ ok: true, json: async () => ({ friends: [alias()] }) }), async () => ({ ok: false, status: 403 })]) {
    const limited = fixture({ maxAliases: 1, fetchImpl }); assert.equal(await limited.resolver.getChatTitle('101'), '공개 이름'); limited.resolver.close();
  }
});

test('hung alias HTTP retains bounded room slots until actual settlement, even after AbortSignal timeout', async () => {
  let requests = 0; let finish; let requestSignal;
  const f = fixture({ lookupTimeoutMs: 5, aliasTimeoutMs: 5, fetchImpl: async (url, options) => {
    requests++; if (!url.includes('list.json?')) return { ok: false };
    requestSignal = options.signal; return new Promise(resolve => { finish = () => resolve(response([alias()])); });
  } });
  await Promise.all(Array.from({ length: 20 }, (_, index) => f.resolver.getChatTitle(String(index + 101)))); await delay(10);
  assert.equal(requests, 1); assert.equal(f.calls.length, 4); assert.equal(requestSignal.aborted, true);
  finish(); await turn(); await turn(); assert.equal(requests, 2); assert.ok(f.messages.every(event => event.titleSource === 'participant_names')); f.resolver.close();
});

test('current client credentials drive alias lookup and a failed publication is retried', async () => {
  let clock = 1; const events = []; let publications = 0;
  const f = fixture({ now: () => clock, onMetadata: event => { events.push(event); return ++publications > 1; },
    fetchImpl: async (_url, options) => { assert.equal(options.headers.Authorization, 'rotated-token-fixture-device'); return response([alias()]); } });
  f.client.getCredentials = () => ({ ...credentials, oauthToken: 'rotated-token' });
  assert.equal(await f.resolver.getChatTitle('101'), '저장 이름'); await turn(); clock++;
  assert.equal(await f.resolver.getChatTitle('101'), '저장 이름'); assert.equal(events.length, 2); f.resolver.close();
});

test('empty full list falls back to exact proved peer endpoint and preserves raw friend name', async () => {
  const urls = [];
  const f = fixture({ channel: id => packet(id, { displayMembers: [{ userId: 8, nickName: '공개 이름' }] }),
    fetchImpl: async (url, options) => {
      urls.push(url); assert.equal(options.method, 'GET');
      if (url.includes('list.json?')) return response([]);
      assert.match(url, /\/friends\/8\.json$/);
      return { ok: true, json: async () => ({ status: 0, token: 'not-retained', friend: alias({
        friendNickName: '  Ｃ 내 지정 이름 ', phoneNumber: 'PRIVATE PHONE', nickName: '프로필명' }) }) };
    } });
  assert.equal(await f.resolver.getChatTitle('101'), '  Ｃ 내 지정 이름 ');
  assert.equal(urls.length, 2); assert.equal(f.messages[0].titleSource, 'synced_friend_name');
  assert.equal(f.messages[0].authoritative, true); assert.equal(JSON.stringify(f.messages).includes('PRIVATE'), false); f.resolver.close();
});

test('targeted aliases reject wrong peer, mismatching room, purged and guessed wrappers', async () => {
  for (const body of [
    { status: 0, friend: alias({ userId: '9' }) }, { status: 0, friend: alias({ directChatId: '999' }) },
    { status: 0, friend: alias({ purged: true }) }, { status: 0, member: alias() },
    { status: 0, ...alias() }, { status: -1, friend: alias() }, { friend: alias() },
    { status: 0, friend: alias({ friendNickName: undefined }) },
  ]) {
    const f = fixture({ fetchImpl: async url => url.includes('list.json?') ? response([]) : { ok: true, json: async () => body } });
    assert.equal(await f.resolver.getChatTitle('101'), '공개 이름');
    assert.equal(f.messages[0].authoritative, false); f.resolver.close();
  }
});

test('only complete fresh absence permits source downgrade and failed alias lookup preserves saved room name', async () => {
  let clock = 1; let name = '내 방 지정 이름'; let success = false;
  const f = fixture({ now: () => clock, metadataTtlMs: 2, aliasTtlMs: 2, retryDelayMs: 1,
    channel: id => packet(id, { ...(name ? { meta: { name } } : {}) }),
    fetchImpl: async url => {
      if (url.includes('list.json?')) return response([]);
      return success ? { ok: true, json: async () => ({ status: 0, friend: alias({ friendNickName: '' }) }) } : { ok: false };
    } });
  assert.equal(await f.resolver.getChatTitle('101'), name);
  name = null; clock += 3; await f.resolver.refresh(['101']);
  assert.equal(f.messages.at(-1).chatTitle, '내 방 지정 이름'); assert.equal(f.messages.at(-1).authoritative, false);
  success = true; clock += 3; await f.resolver.refresh(['101']);
  assert.equal(f.messages.at(-1).chatTitle, '공개 이름'); assert.equal(f.messages.at(-1).titleSource, 'participant_names');
  assert.equal(f.messages.at(-1).authoritative, true); f.resolver.close();
});

test('malformed higher-priority metadata never authorizes replacing a confirmed saved name', async () => {
  let clock = 1; let name = '저장 이름';
  const f = fixture({ now: () => clock, metadataTtlMs: 2, channel: id => packet(id, { meta: { name }, type: 'MultiChat' }) });
  await f.resolver.getChatTitle('101'); name = '\n'; clock += 3;
  await f.resolver.refresh(['101']);
  assert.equal(f.messages.at(-1).chatTitle, '저장 이름'); assert.equal(f.messages.at(-1).authoritative, false); f.resolver.close();
});

test('refresh waits for late empty-room metadata beyond message caller deadline', async () => {
  const f = fixture({ lookupTimeoutMs: 5, channel: async id => { await delay(20); return packet(id, { meta: { name: '천천히 확인된 방 이름' } }); } });
  assert.equal(await f.resolver.refresh(['101']), 1); assert.equal(f.messages.length, 1);
  assert.equal(f.messages[0].chatTitle, '천천히 확인된 방 이름'); f.resolver.close();
});

test('repeated aborted refresh cycles remove all owner listeners while a SDK operation remains unresolved', async () => {
  let finish;
  const f = fixture({ channel: id => new Promise(resolve => { finish = () => resolve(packet(id, { meta: { name: '오래 걸린 방' } })); }) });
  for (let cycle = 0; cycle < 30; cycle++) {
    const controller = new AbortController(); const listeners = new Set();
    const add = controller.signal.addEventListener.bind(controller.signal);
    const remove = controller.signal.removeEventListener.bind(controller.signal);
    controller.signal.addEventListener = (kind, handler, options) => { if (kind === 'abort') listeners.add(handler); add(kind, handler, options); };
    controller.signal.removeEventListener = (kind, handler, options) => { if (kind === 'abort') listeners.delete(handler); remove(kind, handler, options); };
    const refreshed = f.resolver.refresh(['101'], controller.signal); await turn(); controller.abort();
    assert.equal(await refreshed, 0); assert.equal(listeners.size, 0);
  }
  assert.equal(f.calls.length, 1); finish(); await turn(); await turn(); assert.deepEqual(f.messages, []); f.resolver.close();
});

test('target alias requests coalesce, expire, preserve last good metadata on failure and stay bounded', async () => {
  let clock = 1; let name = '저장 이름'; let requests = 0; let failure = false;
  const f = fixture({ now: () => clock, metadataTtlMs: 2, aliasTtlMs: 2, retryDelayMs: 1,
    fetchImpl: async url => {
      if (url.includes('list.json?')) return response([]);
      requests++; if (failure) return { ok: false };
      return { ok: true, json: async () => ({ status: 0, friend: alias({ directChatId: undefined, friendNickName: name }) }) };
    } });
  await Promise.all(['101', '102', '103', '104'].map(id => f.resolver.getChatTitle(id))); assert.equal(requests, 1);
  failure = true; clock += 3; await f.resolver.refresh(['101']);
  assert.equal(f.messages.at(-1).chatTitle, name); assert.equal(f.messages.at(-1).authoritative, false);
  failure = false; name = '새 저장 이름'; clock += 3; await f.resolver.refresh(['101']);
  assert.equal(f.messages.at(-1).chatTitle, name); assert.equal(f.messages.at(-1).authoritative, true); f.resolver.close();
  let targeted = 0;
  const small = fixture({ maxCachedRooms: 2, channel: id => packet(id, { displayMembers: [{ userId: id, nickName: '공개' }] }),
    fetchImpl: async url => {
      if (url.includes('list.json?')) return response([]);
      targeted++; const peerId = url.match(/\/friends\/(\d+)\.json$/)[1];
      return { ok: true, json: async () => ({ status: 0, friend: alias({ userId: peerId, directChatId: peerId }) }) };
    } });
  for (const id of ['101', '102', '103', '101']) await small.resolver.getChatTitle(id);
  assert.equal(targeted, 4); small.resolver.close();
});

test('close cancels alias HTTP and cross-account credentials cannot retrieve aliases', async () => {
  let finish; let signal;
  const f = fixture({ lookupTimeoutMs: 5, fetchImpl: async (_url, options) => {
    signal = options.signal; return new Promise(resolve => { finish = () => resolve(response([alias()])); });
  } });
  const pending = f.resolver.getChatTitle('101'); await turn(); f.resolver.close();
  assert.equal(signal.aborted, true); finish(); await pending; assert.deepEqual(f.messages, []);
  let requests = 0; const other = fixture({ fetchImpl: () => { requests++; throw new Error('wrong account'); } });
  other.client.getCredentials = () => ({ ...credentials, userId: '99' });
  assert.equal(await other.resolver.getChatTitle('101'), '공개 이름'); assert.equal(requests, 0);
  assert.equal(other.messages[0].authoritative, false); other.resolver.close();
});
