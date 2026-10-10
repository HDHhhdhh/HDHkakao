import assert from 'node:assert/strict';
import { PassThrough, Writable } from 'node:stream';
import test from 'node:test';
import { parseBridgeCredentials, parseBridgeStartRequest, runBridge } from '../src/bridge.mjs';

test('a retained own-only room gets its exact saved name through targeted read-only metadata', async () => {
  const input = new PassThrough();
  const events = [], calls = [];
  class Client {
    async login() { this.connected = true; }
    isConnected() { return this.connected === true; }
    close() { this.connected = false; }
    lookupAuthorName() { return null; }
    onPush() { return () => {}; }
    onSessionEvent() { return () => {}; }
    async acquireSession() {
      return { getChannelInfo: async id => {
        calls.push('CHATINFO:' + id);
        return { method: 'CHATINFO', statusCode: 0, body: { chatInfo: {
          chatId: String(id), type: 'DirectChat', activeMembersCount: 2,
          displayMembers: [{ userId: '200', nickName: '프로필 이름' }],
        } } };
      } };
    }
    async getMessagePage() { calls.push('history'); return { messages: [], complete: true }; }
    getChatTitle() { throw Error('old TITLE-only lookup must not be used'); }
    sendMessage() { throw Error('forbidden WRITE'); }
    markRead() { throw Error('forbidden read receipt'); }
    sendTyping() { throw Error('forbidden typing'); }
  }
  class Listener {
    constructor(facade) {
      this.handlers = new Map();
      for (const forbidden of ['sendMessage', 'markRead', 'sendTyping', 'getChatInfo'])
        assert.equal(facade[forbidden], undefined);
    }
    on(type, handler) { this.handlers.set(type, handler); }
    off(type) { this.handlers.delete(type); }
    async start() { this.handlers.get('connected')?.({ userId: '123' }); }
    stop() {}
  }
  const output = new Writable({ write(chunk, _encoding, callback) {
    const event = JSON.parse(String(chunk)); events.push(event);
    if (event.status === 'catchup_completed') queueMicrotask(() => input.end());
    callback();
  } });
  input.write(JSON.stringify({ credentials: { oauth_token: 'synthetic-token', user_id: '123',
    device_uuid: 'synthetic-device', device_type: 'tablet' },
    catchUp: [{ chatId: '101', afterLogId: '99', limit: 50 }] }) + '\n');
  const exitCode = await runBridge({ input, output, sdk: {
    KakaoTalkClient: Client, KakaoTalkListener: Listener,
    fetchRoomNames: async (url, options) => {
      assert.equal(options.method, 'GET');
      const path = new URL(url).pathname; calls.push(path);
      if (path === '/android/friends/list.json') return { ok: true, json: async () => ({ status: 0, friends: [] }) };
      assert.equal(path, '/android/friends/200.json');
      return { ok: true, json: async () => ({ status: 0,
        friend: { userId: '200', directChatId: '101', friendNickName: '  Ａ 반 가 ✨  ' } }) };
    },
  } });
  assert.equal(exitCode, 0);
  assert.deepEqual(events.filter(event => event.type === 'room_metadata'), [{ type: 'room_metadata',
    chatId: '101', chatTitle: '  Ａ 반 가 ✨  ', titleSource: 'synced_friend_name', authoritative: true }]);
  assert.equal(events.some(event => event.type === 'message'), false);
  assert.equal(calls.filter(item => item.startsWith('CHATINFO:')).length, 1);
  assert.ok(calls.includes('/android/friends/200.json'));
});

test('accepts the C# nested DPAPI handoff and normalizes tablet credentials', () => {
  assert.deepEqual(parseBridgeCredentials({
    type: 'start',
    credentials: {
      oauth_token: 'oauth-value',
      refresh_token: 'refresh-value',
      user_id: '123',
      device_uuid: 'device-value',
      device_type: 'tablet',
    },
  }), {
    oauthToken: 'oauth-value',
    refreshToken: 'refresh-value',
    userId: '123',
    deviceUuid: 'device-value',
    deviceType: 'tablet',
  });
});

test('rejects PC credentials and incomplete handoffs', () => {
  assert.throws(() => parseBridgeCredentials({
    oauthToken: 'oauth-value',
    userId: '123',
    deviceUuid: 'device-value',
    deviceType: 'pc',
  }), { code: 'invalid_credentials' });
  assert.throws(() => parseBridgeCredentials({ credentials: {} }), { code: 'invalid_credentials' });
});

test('accepts an optional bounded catch-up cursor list without widening credentials', () => {
  assert.deepEqual(parseBridgeStartRequest({
    oauthToken: 'oauth-value',
    userId: '123',
    deviceUuid: 'device-value',
    deviceType: 'tablet',
    catchUp: [{ chatId: 'room-1', afterLogId: '77', limit: 4 }],
  }), {
    credentials: {
      oauthToken: 'oauth-value',
      refreshToken: null,
      userId: '123',
      deviceUuid: 'device-value',
      deviceType: 'tablet',
    },
    catchUp: [{ chatId: 'room-1', afterLogId: '77', limit: 4 }],
  });
});

test('runs the read-only lifecycle with a fake client/listener and closes cleanly', async () => {
  const input = new PassThrough();
  const events = [];
  const output = new Writable({
    write(chunk, _encoding, callback) {
      const event = JSON.parse(String(chunk));
      events.push(event);
      if (event.type === 'status' && event.status === 'ready') {
        queueMicrotask(() => input.end());
      }
      callback();
    },
  });

  const clients = [];
  class FakeClient {
    constructor() {
      this.connected = false;
      this.closed = false;
      clients.push(this);
    }

    async login(credentials) {
      this.credentials = credentials;
      this.connected = true;
      return this;
    }

    isConnected() {
      return this.connected && !this.closed;
    }

    close() {
      this.closed = true;
    }

    acquireSession() {
      return Promise.resolve({});
    }

    onPush() {
      return () => {};
    }

    onSessionEvent() {
      return () => {};
    }

    lookupAuthorName() {
      return 'fake-user';
    }
  }

  class FakeListener {
    constructor(client) {
      this.client = client;
      this.handlers = new Map();
      this.stopped = false;
    }

    on(kind, handler) {
      this.handlers.set(kind, handler);
      return this;
    }

    off(kind, handler) {
      if (this.handlers.get(kind) === handler) this.handlers.delete(kind);
      return this;
    }

    async start() {
      this.handlers.get('connected')?.({ userId: this.client.getCredentials().userId });
      this.handlers.get('message')?.({
        chat_id: 'room-1',
        log_id: 'log-1',
        author_id: 7,
        author_name: 'fake-user',
        message: 'popup-independent',
        message_type: 1,
        attachment: null,
        sent_at: 1760000000000,
      });
    }

    stop() {
      this.stopped = true;
    }
  }

  input.write('{"oauthToken":"oauth-value","userId":"123","deviceUuid":"device-value","deviceType":"tablet"}\n');
  const exitCode = await runBridge({
    input,
    output,
    sdk: { KakaoTalkClient: FakeClient, KakaoTalkListener: FakeListener },
  });

  assert.equal(exitCode, 0);
  assert.equal(clients.length, 1);
  assert.equal(clients[0].closed, true);
  assert.deepEqual(events.filter(event => event.type !== 'message').map(event => `${event.type}:${event.status}`), [
    'status:starting',
    'status:credentials_loaded',
    'status:session_starting',
    'status:connected',
    'status:ready',
    'status:stopped',
  ]);
  // Readiness reports connection state. Independent metadata resolution can
  // complete on either side of ready, but the buffered body must be drained.
  assert.deepEqual(events.filter(event => event.type === 'message').map(event => event.message), ['popup-independent']);
  const bodyIndex = events.findIndex(event => event.type === 'message');
  assert.ok(bodyIndex > events.findIndex(event => event.status === 'connected'));
  assert.ok(bodyIndex < events.findIndex(event => event.status === 'stopped'));
});

test('refreshes an expired session once without exposing the recoverable error', async () => {
  const input = new PassThrough();
  const events = [];
  const output = new Writable({
    write(chunk, _encoding, callback) {
      const event = JSON.parse(String(chunk));
      events.push(event);
      if (event.type === 'auth_lease_request') {
        queueMicrotask(() => input.write(JSON.stringify({ type: 'auth_lease_granted', request_id: event.request_id,
          ok: true, auth_lease_id: 'synthetic-lease', credentials: event.credentials }) + '\n'));
      }
      if (event.type === 'auth_lease_release') {
        queueMicrotask(() => input.write(JSON.stringify({ type: 'auth_lease_released', request_id: event.request_id,
          ok: true }) + '\n'));
      }
      if (event.type === 'credentials') {
        queueMicrotask(() => input.write('{"type":"credentials_saved"}\n'));
      }
      if (event.type === 'status' && event.status === 'ready') queueMicrotask(() => input.end());
      callback();
    },
  });
  const clients = [];
  class RefreshClient {
    constructor() {
      this.attempt = clients.length + 1;
      this.closed = false;
      clients.push(this);
    }

    async login(credentials) {
      this.credentials = credentials;
      this.connected = this.attempt > 1;
      return this;
    }

    isConnected() {
      return this.connected && !this.closed;
    }

    close() {
      this.closed = true;
    }

    getCredentials() {
      return { userId: this.credentials.userId };
    }

    acquireSession() {
      return Promise.resolve({});
    }

    onPush() {
      return () => {};
    }

    onSessionEvent() {
      return () => {};
    }

    lookupAuthorName() {
      return null;
    }
  }

  class RefreshListener {
    constructor(client) {
      this.client = client;
      this.handlers = new Map();
    }

    on(kind, handler) {
      this.handlers.set(kind, handler);
      return this;
    }

    off(kind, handler) {
      if (this.handlers.get(kind) === handler) this.handlers.delete(kind);
      return this;
    }

    async start() {
      if (!this.client.isConnected()) {
        const error = Object.assign(new Error('expired'), { code: 'invalid_access_token' });
        this.handlers.get('error')?.(error);
        return;
      }
      this.handlers.get('connected')?.({ userId: this.client.getCredentials().userId });
    }

    stop() {}
  }

  const refreshCalls = [];
  input.write('{"oauthToken":"expired-token","refreshToken":"refresh-value","userId":"123","deviceUuid":"device-value","deviceType":"tablet"}\n');
  const exitCode = await runBridge({
    input,
    output,
    sdk: {
      KakaoTalkClient: RefreshClient,
      KakaoTalkListener: RefreshListener,
      refreshKakaoOAuthToken: async (value) => {
        refreshCalls.push(value);
        return { accessToken: 'fresh-token', refreshToken: 'rotated-refresh' };
      },
    },
  });

  assert.equal(exitCode, 0);
  assert.equal(clients.length, 2);
  assert.equal(clients[0].closed, true);
  assert.equal(clients[1].closed, true);
  assert.deepEqual(refreshCalls, [{
    accessToken: 'expired-token',
    refreshToken: 'refresh-value',
    deviceUuid: 'device-value',
  }]);
  assert.equal(events.some((event) => event.type === 'status' && event.status === 'error'), false);
  const credentialEvent = events.find((event) => event.type === 'credentials');
  assert.equal(credentialEvent?.oauthToken, 'fresh-token');
  assert.equal(credentialEvent?.credentials?.refresh_token, 'rotated-refresh');
});

test('exits the bridge when the live listener disconnects so the host can reconnect', async () => {
  const input = new PassThrough();
  const events = [];
  let activeListener = null;
  const output = new Writable({
    write(chunk, _encoding, callback) {
      const event = JSON.parse(String(chunk));
      events.push(event);
      if (event.type === 'status' && event.status === 'ready') {
        queueMicrotask(() => activeListener?.disconnect());
      }
      callback();
    },
  });

  class DisconnectClient {
    constructor() {
      this.connected = false;
      this.closed = false;
    }

    async login(credentials) {
      this.credentials = credentials;
      this.connected = true;
    }

    isConnected() { return this.connected && !this.closed; }
    close() { this.closed = true; }
    acquireSession() { return Promise.resolve({}); }
    onPush() { return () => {}; }
    onSessionEvent() { return () => {}; }
    lookupAuthorName() { return null; }
  }

  class DisconnectListener {
    constructor(client) {
      this.client = client;
      this.handlers = new Map();
      activeListener = this;
    }

    on(kind, handler) { this.handlers.set(kind, handler); return this; }
    off(kind, handler) {
      if (this.handlers.get(kind) === handler) this.handlers.delete(kind);
      return this;
    }
    async start() {
      this.handlers.get('connected')?.({ userId: this.client.getCredentials().userId });
    }
    disconnect() { this.handlers.get('disconnected')?.(); }
    stop() {}
  }

  input.write('{"oauthToken":"oauth-value","userId":"123","deviceUuid":"device-value","deviceType":"tablet"}\n');
  const exitCode = await runBridge({
    input,
    output,
    sdk: { KakaoTalkClient: DisconnectClient, KakaoTalkListener: DisconnectListener },
  });

  assert.equal(exitCode, 1);
  assert.equal(
    events.some((event) => event.type === 'status' && event.status === 'disconnected'),
    true,
    JSON.stringify(events),
  );
  assert.equal(events.at(-1)?.status, 'stopped');
});

test('runs bounded read-only catch-up after listener start and never invokes write/open methods', async () => {
  const input = new PassThrough();
  const events = [];
  const forbiddenCalls = [];
  const output = new Writable({
    write(chunk, _encoding, callback) {
      const event = JSON.parse(String(chunk));
      events.push(event);
      if (event.type === 'status' && event.status === 'catchup_completed') queueMicrotask(() => input.end());
      callback();
    },
  });

  class CatchUpClient {
    constructor() {
      this.connected = false;
      this.closed = false;
      this.pageCalls = [];
    }

    async login(credentials) {
      this.credentials = credentials;
      this.connected = true;
      return this;
    }

    isConnected() { return this.connected && !this.closed; }
    close() { this.closed = true; }
    acquireSession() { return Promise.resolve({}); }
    onPush() { return () => {}; }
    onSessionEvent() { return () => {}; }
    lookupAuthorName() { return '상대'; }
    async getMessagePage(chatId, options) {
      this.pageCalls.push({ chatId, options });
      if (options.from === '103') return { messages: [], next_cursor: '103', complete: true };
      return {
        messages: [
          { log_id: '103', type: 1, author_id: 7, message: '세 번째', sent_at: 300 },
          { log_id: '101', type: 1, author_id: 7, message: '첫 번째', sent_at: 100 },
          { log_id: '101', type: 1, author_id: 7, message: '중복', sent_at: 100 },
        ],
        next_cursor: '103',
        complete: false,
      };
    }
    async markRead() { forbiddenCalls.push('markRead'); }
    async sendMessage() { forbiddenCalls.push('sendMessage'); }
    async sendTyping() { forbiddenCalls.push('sendTyping'); }
    async getChatInfo() { forbiddenCalls.push('getChatInfo'); }
    async openChat() { forbiddenCalls.push('openChat'); }
  }

  class CatchUpListener {
    constructor(client) {
      this.client = client;
      this.handlers = new Map();
    }

    on(kind, handler) { this.handlers.set(kind, handler); return this; }
    off(kind, handler) {
      if (this.handlers.get(kind) === handler) this.handlers.delete(kind);
      return this;
    }
    async start() {
      this.handlers.get('connected')?.({ userId: this.client.getCredentials().userId });
    }
    stop() {}
  }

  let client;
  class InspectableClient extends CatchUpClient {
    constructor() {
      super();
      client = this;
    }
  }

  input.write(JSON.stringify({
    oauthToken: 'oauth-value',
    userId: '123',
    deviceUuid: 'device-value',
    deviceType: 'tablet',
    catchUp: [{ chatId: 'room-1', afterLogId: '100', limit: 3 }],
  }) + '\n');
  const exitCode = await runBridge({
    input,
    output,
    sdk: { KakaoTalkClient: InspectableClient, KakaoTalkListener: CatchUpListener },
  });

  assert.equal(exitCode, 0);
  assert.deepEqual(client.pageCalls, [
    { chatId: 'room-1', options: { count: 3, from: '100' } },
    { chatId: 'room-1', options: { count: 3, from: '103' } },
  ]);
  assert.deepEqual(events.filter((event) => event.type === 'message').map((event) => [event.logId, event.message]), [
    ['101', '첫 번째'],
    ['103', '세 번째'],
  ]);
  const started = events.find((event) => event.status === 'catchup_started');
  const completed = events.find((event) => event.status === 'catchup_completed');
  assert.deepEqual(started, { type: 'status', status: 'catchup_started', rooms: 1 });
  assert.equal(completed?.messages, 2);
  assert.equal(completed?.partial, false);
  assert.equal(completed?.failedRooms, 0);
  assert.deepEqual(completed?.nextCursors, [{ chatId: 'room-1', afterLogId: '103' }]);
  assert.deepEqual(forbiddenCalls, []);
});
