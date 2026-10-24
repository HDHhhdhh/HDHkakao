import assert from 'node:assert/strict';
import test from 'node:test';
import { createRoomNameResolver } from '../src/room-names.mjs';

const credentials = { userId: '7', oauthToken: 'fixture-token', deviceUuid: 'fixture-device' };
const ok = (method, body) => ({ method, statusCode: 0, body: { status: 0, ...body } });
const members = () => [{ userId: 7, nickName: '나' }, { userId: 8, nickName: '공개 이름' }];
function fixture({ mutate = () => {}, channel, ...options } = {}) {
  const calls = [], events = [], urls = [];
  const session = {
    async getChannelInfo(id) { calls.push('CHATINFO'); return channel ? channel(String(id)) : ok('CHATINFO', { status: -401 }); },
    async getChatInfo(id) {
      calls.push('CHATONROOM');
      const response = ok('CHATONROOM', { c: id, t: 'DirectChat', m: members() });
      mutate(response, 'room'); return response;
    },
    async getAllMembers() {
      calls.push('GETMEM'); const response = ok('GETMEM', { members: members() });
      mutate(response, 'members'); return response;
    },
  };
  const client = { async acquireSession() { return session; } };
  const resolver = createRoomNameResolver({ client, credentials, lookupTimeoutMs: 1000,
    fetchImpl: async (url, request) => {
      urls.push(url); assert.equal(request.method, 'GET');
      return { ok: true, async json() { return url.includes('list.json?') ? { status: 0, friends: [] }
        : { status: 0, friend: { userId: 8, friendNickName: ' Ｂ 저장 이름 ', directChatId: '0' } }; } };
    }, onMetadata: event => { events.push(event); return true; }, ...options });
  return { resolver, calls, events, urls };
}

test('empty direct room resolves its verified saved friend name without message or read calls', async () => {
  const f = fixture();
  assert.equal(await f.resolver.getChatTitle('101'), ' Ｂ 저장 이름 ');
  assert.deepEqual(f.calls, ['CHATINFO', 'CHATONROOM', 'GETMEM', 'CHATONROOM', 'GETMEM']);
  assert.deepEqual(f.events, [{ type: 'room_metadata', chatId: '101', chatTitle: ' Ｂ 저장 이름 ', titleSource: 'synced_friend_name', authoritative: false }]);
  assert.equal(f.urls.length, 2); f.resolver.close();
});

for (const [name, mutate] of [
  ['group', (r, kind) => { if (kind === 'room') r.body.t = 'MultiChat'; }],
  ['wrong room', (r, kind) => { if (kind === 'room') r.body.c = '102'; }],
  ['mismatched members', (r, kind) => { if (kind === 'members') r.body.members[1].userId = 9; }],
  ['missing own account', (r, kind) => { (kind === 'members' ? r.body.members : r.body.m)[0].userId = 6; }],
]) {
  test(`empty room fallback rejects ${name} before reading aliases`, async () => {
    const f = fixture({ mutate });
    assert.equal(await f.resolver.getChatTitle('101'), null);
    assert.deepEqual(f.events, []); assert.deepEqual(f.urls, []); f.resolver.close();
  });
}

test('does not substitute empty-room lookup for other CHATINFO failures', async () => {
  for (const channel of [() => ok('CHATINFO', { status: -1 }), () => ok('OTHER', { status: -401 }),
    () => ({ method: 'CHATINFO', statusCode: -1, body: { status: -401 } })]) {
    const f = fixture({ channel });
    assert.equal(await f.resolver.getChatTitle('101'), null);
    assert.deepEqual(f.calls, ['CHATINFO']); assert.deepEqual(f.events, []); f.resolver.close();
  }
});

test('fallback cannot erase a previously verified account room name', async () => {
  let clock = 1, full = true;
  const f = fixture({ now: () => clock, metadataTtlMs: 1, channel: id => full
    ? ok('CHATINFO', { chatInfo: { chatId: id, type: 'DirectChat', activeMembersCount: 2, displayMembers: members(), meta: { name: '내 대화방 이름' } } })
    : ok('CHATINFO', { status: -401 }) });
  assert.equal(await f.resolver.getChatTitle('101'), '내 대화방 이름');
  full = false; clock += 2;
  assert.equal(await f.resolver.getChatTitle('101'), '내 대화방 이름');
  assert.equal(f.events.length, 1); f.resolver.close();
});

test('abort during fallback stops the remaining reads and does not publish', async () => {
  const controller = new AbortController();
  const f = fixture({ mutate: () => controller.abort() });
  assert.equal(await f.resolver.refresh(['101'], controller.signal), 0);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(f.calls, ['CHATINFO', 'CHATONROOM']);
  assert.deepEqual(f.events, []); assert.deepEqual(f.urls, []); f.resolver.close();
});
