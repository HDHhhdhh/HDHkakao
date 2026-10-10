import { pathToFileURL } from 'node:url';
import { generateDeviceUuid, loginFlow } from 'agent-messenger/kakaotalk';
import { createNdjsonEmitter, parseJsonLine, readFirstJsonLine, releaseJsonInput, withDeadline, safeError, safeResultError } from './io.mjs';

function requiredText(value, field) {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw Object.assign(new Error(`${field} is required.`), { code: 'invalid_auth_request' });
  }
  return value;
}

export function parseAuthRequest(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw Object.assign(new Error('Authentication input must be a JSON object.'), { code: 'invalid_auth_request' });
  }
  const email = requiredText(value.email, 'email');
  const password = requiredText(value.password, 'password');
  if (value.deviceType !== 'tablet') {
    throw Object.assign(new Error('Only tablet registration is accepted by this entry point.'), {
      code: 'invalid_auth_request',
    });
  }
  const deviceUuid = value.deviceUuid === undefined ? null : requiredText(value.deviceUuid, 'deviceUuid');
  // `force` is an intent hint from the host, never authorization for the first
  // request. The first network attempt below is always non-forced. A forced
  // attempt is allowed only on an explicitly requested second invocation.
  return { email, password, deviceType: 'tablet', force: value.force === true, deviceUuid };
}

function credentialsEvent(result) {
  const credentials = result?.credentials;
  if (!result?.authenticated || !credentials) return null;
  const oauthToken = typeof credentials.access_token === 'string' ? credentials.access_token : '';
  const refreshToken = typeof credentials.refresh_token === 'string' ? credentials.refresh_token : '';
  const userId = typeof credentials.user_id === 'string' ? credentials.user_id : '';
  const deviceUuid = typeof credentials.device_uuid === 'string' ? credentials.device_uuid : '';
  const deviceType = credentials.device_type;
  if (!oauthToken || !userId || !deviceUuid || deviceType !== 'tablet') return null;
  // Keep a nested snake_case copy for the C# host's DPAPI vault while also
  // retaining the documented flat handoff fields for other hosts.
  return {
    type: 'credentials',
    oauthToken,
    refreshToken,
    userId,
    deviceUuid,
    deviceType,
    credentials: {
      oauth_token: oauthToken,
      access_token: oauthToken,
      refresh_token: refreshToken,
      user_id: userId,
      device_uuid: deviceUuid,
      device_type: deviceType,
    },
  };
}

export async function runAuth({ input = process.stdin, output = process.stdout, sdk = {}, limits = {}, signal } = {}) {
  const emit = createNdjsonEmitter(output, { timeoutMs: limits.outputMs ?? 10000 });
  const cancellation = new AbortController();
  const cancel = () => cancellation.abort();
  signal?.addEventListener('abort', cancel, { once: true });
  if (signal?.aborted) cancel();
  const handlers = ['SIGINT', 'SIGTERM'].map((name) => { process.once(name, cancel); return name; });
  emit.failed.then(cancel);
  const deadline = Date.now() + (limits.authMs ?? 180000);
  let finished = false;
  let request = null;
  let deviceUuid = null;
  try {
    const line = await readFirstJsonLine(input, { timeoutMs: limits.inputMs ?? 15000, signal: cancellation.signal });
    request = parseAuthRequest(parseJsonLine(line));
    // Registration is a one-shot request, unlike the listener's ACK channel.
    // Release our pipe immediately; EOF from the host is not auth cancellation.
    releaseJsonInput(input);
    // Generate the identity before the first request so a retry never creates
    // a second device. The UUID is returned to the host on an approval pause.
    deviceUuid = request.deviceUuid ?? sdk.generateDeviceUuid?.() ?? generateDeviceUuid();
    emit({ type: 'device', deviceUuid });
    const login = sdk.loginFlow ?? loginFlow;
    const flow = (options) => withDeadline(() => login(options), Math.max(1, deadline - Date.now()), 'auth_timeout', cancellation.signal);
    const firstResult = await flow({
      email: request.email,
      password: request.password,
      deviceType: request.deviceType,
      force: false,
      savedDeviceUuid: deviceUuid,
      onPasscodeDisplay: (code) => {
        if (!finished && !cancellation.signal.aborted && typeof code === 'string' && /^\d{4,8}$/.test(code)) emit({ type: 'passcode', code });
      },
    });

    const firstEvent = credentialsEvent(firstResult);
    if (firstEvent) {
      await emit(firstEvent);
      await emit.flush();
      return 0;
    }

    const requiresForce = firstResult?.next_action === 'choose_device' || firstResult?.error === 'login_failed';
    if (requiresForce) {
      if (!request.force) {
        emit({
          type: 'status',
          status: 'needs_force',
          deviceType: 'tablet',
          deviceUuid,
          message: 'The tablet slot may be occupied. Start a second registration with force:true to replace it.',
        });
        // Exit after the safe probe. The host must launch a second invocation
        // with force:true after the user confirms replacement.
        return 2;
      }

      emit({ type: 'status', status: 'force_starting', deviceType: 'tablet' });
      const forcedResult = await flow({
        email: request.email,
        password: request.password,
        deviceType: request.deviceType,
        force: true,
        savedDeviceUuid: deviceUuid,
        onPasscodeDisplay: (code) => {
          if (!finished && !cancellation.signal.aborted && typeof code === 'string' && /^\d{4,8}$/.test(code)) emit({ type: 'passcode', code });
        },
      });
      const forcedEvent = credentialsEvent(forcedResult);
      if (forcedEvent) {
        await emit(forcedEvent);
        await emit.flush();
        return 0;
      }
      const forcedCode = typeof forcedResult?.error === 'string'
        ? forcedResult.error
        : (forcedResult?.next_action ?? 'login_failed');
      const forcedMessage = typeof forcedResult?.message === 'string'
        ? forcedResult.message
        : 'Tablet registration did not complete.';
      emit({ type: 'error', ...safeResultError(forcedCode, forcedMessage, 'login_failed'), deviceUuid });
      return 1;
    }

    const code = typeof firstResult?.error === 'string' ? firstResult.error : (firstResult?.next_action ?? 'login_failed');
    const message = typeof firstResult?.message === 'string' ? firstResult.message : 'Tablet registration did not complete.';
    emit({ type: 'error', ...safeResultError(code, message, 'login_failed'), deviceUuid });
    return 1;
  } catch (error) {
    emit({ type: 'error', ...safeError(error, 'auth_failed') });
    return 1;
  } finally {
    finished = true;
    cancellation.abort();
    signal?.removeEventListener('abort', cancel);
    for (const name of handlers) process.removeListener(name, cancel);
    releaseJsonInput(input);
    try { await emit.flush(); } catch {}
    emit.close();
    // Best-effort release of the request object. The SDK keeps only the
    // resulting token set after login; no password is persisted by this entry.
    if (request) request.password = '';
  }
}

async function main() {
  const exitCode = await runAuth();
  process.exit(exitCode);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(async (error) => {
    const emit = createNdjsonEmitter(process.stdout);
    emit({ type: 'error', ...safeError(error, 'auth_failed') });
    try { await emit.flush(); } catch {}
    releaseJsonInput(process.stdin);
    process.exit(1);
  });
}
