import assert from 'node:assert/strict';
import test from 'node:test';
import { coordinatedRefresh } from '../src/auth-lease.mjs';

const current = { oauth_token: 'old', refresh_token: 'old-refresh', user_id: '42', device_uuid: 'fixture' };
test('the losing host reuses the saved winning token without a remote refresh', async () => {
  const latest = { ...current, oauth_token: 'winning', refresh_token: 'winning-refresh' };
  let released = false;
  const result = await coordinatedRefresh({ current,
    acquire: async () => ({ ok: true, auth_lease_id: 'lease', credentials: latest }),
    refresh: async () => { throw new Error('Second remote refresh is forbidden'); },
    persist: async () => { throw new Error('Do not overwrite the winning vault'); },
    release: async () => { released = true; },
  });
  assert.equal(result, latest);
  assert.equal(released, true);
});
test('persistence completes before release and a rejected ACK propagates', async () => {
  const events = [];
  await assert.rejects(coordinatedRefresh({ current,
    acquire: async () => ({ ok: true, auth_lease_id: 'lease', credentials: current }),
    refresh: async () => { events.push('refresh'); return { accessToken: 'rotated' }; },
    persist: async () => { events.push('save'); throw Object.assign(new Error(), { code: 'credential_save_failed' }); },
    release: async () => { events.push('release'); },
  }), { code: 'credential_save_failed' });
  assert.deepEqual(events, ['refresh', 'save', 'release']);
});
test('an account or device change rejects the grant and releases its lease', async () => {
  let released = false;
  await assert.rejects(coordinatedRefresh({ current,
    acquire: async () => ({ ok: true, auth_lease_id: 'lease', credentials: { ...current, user_id: 'different' } }),
    refresh: async () => { throw new Error('No network permitted'); },
    persist: async () => {}, release: async () => { released = true; },
  }), { code: 'auth_identity_changed' });
  assert.equal(released, true);
});
test('an old host ACK is rejected before any remote operation', async () => {
  await assert.rejects(coordinatedRefresh({ current, acquire: async () => ({ type: 'credentials_saved' }),
    refresh: async () => { throw new Error('No network permitted'); }, persist: async () => {}, release: async () => {},
  }), { code: 'auth_lease_invalid' });
});
