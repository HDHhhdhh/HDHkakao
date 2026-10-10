import assert from 'node:assert/strict';
import { PassThrough, Writable } from 'node:stream';
import test from 'node:test';
import { runAuth } from '../src/auth.mjs';

function captureOutput(onEvent) {
  const events = [];
  const output = new Writable({
    write(chunk, _encoding, callback) {
      const event = JSON.parse(String(chunk));
      events.push(event);
      onEvent?.(event);
      callback();
    },
  });
  return { events, output };
}

test('auth performs a safe first attempt and requires explicit force confirmation', async () => {
  const calls = [];
  const sdk = {
    generateDeviceUuid: () => 'stable-device-uuid',
    loginFlow: async (options) => {
      calls.push(options);
      if (!options.force) return { authenticated: false, error: 'login_failed', next_action: 'choose_device' };
      options.onPasscodeDisplay?.('2468');
      return {
        authenticated: true,
        credentials: {
          access_token: 'oauth-value',
          refresh_token: 'refresh-value',
          user_id: '123',
          device_uuid: 'stable-device-uuid',
          device_type: 'tablet',
        },
      };
    },
  };

  const firstInput = new PassThrough();
  firstInput.end('{"email":"owner@example.test","password":"in-memory","deviceType":"tablet","force":false}\n');
  const firstCapture = captureOutput();
  const firstExitCode = await runAuth({ input: firstInput, output: firstCapture.output, sdk });

  assert.equal(firstExitCode, 2);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].force, false);
  assert.deepEqual(firstCapture.events, [
    { type: 'device', deviceUuid: 'stable-device-uuid' },
    {
      type: 'status',
      status: 'needs_force',
      deviceType: 'tablet',
      deviceUuid: 'stable-device-uuid',
      message: 'The tablet slot may be occupied. Start a second registration with force:true to replace it.',
    },
  ]);

  const secondInput = new PassThrough();
  secondInput.end('{"email":"owner@example.test","password":"in-memory","deviceType":"tablet","force":true,"deviceUuid":"stable-device-uuid"}\n');
  const secondCapture = captureOutput();
  const exitCode = await runAuth({ input: secondInput, output: secondCapture.output, sdk });

  assert.equal(exitCode, 0);
  assert.equal(calls.length, 3);
  assert.equal(calls[1].force, false);
  assert.equal(calls[2].force, true);
  assert.equal(calls[0].savedDeviceUuid, 'stable-device-uuid');
  assert.equal(calls[1].savedDeviceUuid, 'stable-device-uuid');
  assert.equal(calls[2].savedDeviceUuid, 'stable-device-uuid');
  assert.deepEqual(secondCapture.events, [
    { type: 'device', deviceUuid: 'stable-device-uuid' },
    {
      type: 'status',
      status: 'force_starting',
      deviceType: 'tablet',
    },
    { type: 'passcode', code: '2468' },
    {
      type: 'credentials',
      oauthToken: 'oauth-value',
      refreshToken: 'refresh-value',
      userId: '123',
      deviceUuid: 'stable-device-uuid',
      deviceType: 'tablet',
      credentials: {
        oauth_token: 'oauth-value',
        access_token: 'oauth-value',
        refresh_token: 'refresh-value',
        user_id: '123',
        device_uuid: 'stable-device-uuid',
        device_type: 'tablet',
      },
    },
  ]);
});

test('auth rejects a non-tablet request before invoking the SDK', async () => {
  const input = new PassThrough();
  input.end('{"email":"owner@example.test","password":"in-memory","deviceType":"pc","force":true}\n');
  let invoked = false;
  const { events, output } = captureOutput();
  const exitCode = await runAuth({
    input,
    output,
    sdk: { loginFlow: async () => { invoked = true; } },
  });
  assert.equal(exitCode, 1);
  assert.equal(invoked, false);
  assert.equal(events[0].type, 'error');
  assert.equal(events[0].code, 'invalid_auth_request');
});
