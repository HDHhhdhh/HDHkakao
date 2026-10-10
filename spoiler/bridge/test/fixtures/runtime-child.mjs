// Offline process boundary fixture. Every SDK method is injected in memory.
import { EventEmitter } from 'node:events';
import { runAuth } from '../../src/auth.mjs';
import { runBridge } from '../../src/bridge.mjs';

const mode = process.argv[2];
class Client {
  async login() { this.connected = mode !== 'ack-timeout'; if (mode === 'login-timeout') await new Promise(() => {}); }
  isConnected() { return this.connected; }
  close() { this.connected = false; }
  acquireSession() { return Promise.resolve({}); }
  onPush() { return () => {}; }
  onSessionEvent() { return () => {}; }
  lookupAuthorName() { return 'fixture'; }
  getMessagePage() { return new Promise(() => {}); }
}
class Listener extends EventEmitter {
  constructor(client) { super(); this.client = client; }
  async start() {
    if (mode === 'ack-timeout') { this.emit('error', Object.assign(new Error('FIXTURE_TOKEN'), { code: 'invalid_access_token' })); return; }
    this.emit('connected', { userId: '1' });
    if (mode === 'disconnect') this.timer = setTimeout(() => this.emit('disconnected'), 20);
  }
  stop() { clearTimeout(this.timer); }
}

if (mode.startsWith('auth-')) {
  process.exitCode = await runAuth({ sdk: {
    generateDeviceUuid: () => 'fixture-device',
    loginFlow: async () => {
      if (mode === 'auth-timeout') return new Promise(() => {});
      if (mode === 'auth-force') return { authenticated: false, next_action: 'choose_device' };
      throw Object.assign(new Error('Authorization: Bearer FIXTURE_SECRET'), { code: 'FIXTURE_TOKEN' });
    },
  }, limits: { authMs: 60 } });
} else {
  process.exitCode = await runBridge({ sdk: {
    KakaoTalkClient: Client, KakaoTalkListener: Listener,
    refreshKakaoOAuthToken: async () => ({ accessToken: 'fixture-new', refreshToken: 'fixture-refresh-new' }),
  }, limits: { sessionMs: 60, ackMs: 60, historyPageMs: 30, historyMs: 60, heartbeatMs: 20 } });
}
// Deliberately do not process.exit(): tests prove owned input handles close.
