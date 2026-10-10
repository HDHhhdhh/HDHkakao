import { pathToFileURL } from 'node:url';
import { Long } from 'bson';
import { coordinatedRefresh } from './auth-lease.mjs';
import { KakaoTalkClient, KakaoTalkListener, refreshKakaoOAuthToken } from 'agent-messenger/kakaotalk';
import {
  createNdjsonEmitter,
  parseJsonLine,
  readFirstJsonLine,
  safeError,
  releaseJsonInput,
  withDeadline,
  bridgeError,
} from './io.mjs';
import { installSessionAdoptionBuffer } from './session-adapter.mjs';
import { installReadOnlySyncCache, getReadOnlySyncChatIds } from './sync-cache.mjs';
import { createUnreadRoomDiscovery } from './room-discovery.mjs';
import { attachNormalizedEvents } from './events.mjs';
import { executeReplyCommand } from './replies.mjs';
import { createConversationSyncState, runConversationSync } from './conversation-sync.mjs';
import { createRoomNameResolver } from './room-names.mjs';
import {
  createMessageDeduper,
  sanitizeCatchUpCursors,
} from './catchup.mjs';

function nonEmptyString(value) {
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : null;
}

export function parseBridgeCredentials(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw Object.assign(new Error('Credential input must be a JSON object.'), { code: 'invalid_credentials' });
  }

  // Accept the camelCase DPAPI handoff contract and the SDK's snake_case
  // account shape, but never inspect desktop files or credential managers.
  const account = value.credentials && typeof value.credentials === 'object' && !Array.isArray(value.credentials)
    ? value.credentials
    : value;
  const oauthToken = nonEmptyString(account.oauthToken ?? account.oauth_token ?? account.accessToken ?? account.access_token);
  const userId = nonEmptyString(account.userId ?? account.user_id);
  const deviceUuid = nonEmptyString(account.deviceUuid ?? account.device_uuid ?? account.savedDeviceUuid);
  const refreshToken = nonEmptyString(account.refreshToken ?? account.refresh_token);
  const deviceType = account.deviceType ?? account.device_type ?? 'tablet';

  if (!oauthToken || !userId || !deviceUuid || deviceType !== 'tablet') {
    throw Object.assign(new Error('A tablet credential handoff requires oauthToken, userId, and deviceUuid.'), {
      code: 'invalid_credentials',
    });
  }

  return { oauthToken, refreshToken, userId, deviceUuid, deviceType: 'tablet' };
}

/**
 * Parse the start envelope without widening the credential parser's public
 * shape.  Reconnect state is an optional, bounded list of forward cursors;
 * credentials remain the only required input and are never persisted here.
 */
export function parseBridgeStartRequest(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw Object.assign(new Error('Start input must be a JSON object.'), { code: 'invalid_start_request' });
  }
  return {
    credentials: parseBridgeCredentials(value),
    catchUp: sanitizeCatchUpCursors(value.catchUp ?? value.catch_up ?? value.catchup),
  };
}

function readOnlyClientFacade(client, userId, roomNames, emit) {
  const discoverUnreadRooms = createUnreadRoomDiscovery({
    getChatIds: async () => {
      const cached = getReadOnlySyncChatIds(client);
      if (cached.length || typeof client.getChats !== 'function') return cached;
      return (await client.getChats({ all: true, resolveTitles: false })).map(chat => chat.chat_id);
    },
    getChannelInfo: async chatId => {
      const session = await client.acquireSession();
      return session.getChannelInfo(Long.fromString(chatId));
    },
    peekLatest: async (chatId, logId) => {
      const page = await client.getMessagePage(chatId, { from: String(BigInt(logId) - 1n), count: 1 });
      return page?.messages?.find(message => message.log_id === logId) ?? null;
    },
    onAnchor: anchor => emit({ type: 'unread_recovery_anchor', chatId: anchor.chatId,
      logId: anchor.afterLogId, throughLogId: anchor.throughLogId }),
  });
  return Object.freeze({
    discoverUnreadRooms,
    acquireSession: () => client.acquireSession(),
    onPush: (handler) => client.onPush(handler),
    onSessionEvent: (handler) => client.onSessionEvent(handler),
    isConnected: () => client.isConnected(),
    // These are the only history/metadata calls exposed to the bridge.  In
    // particular, the facade deliberately has no markRead, sendMessage,
    // sendTyping, or chat-opening method.
    getMessagePage: typeof client.getMessagePage === 'function'
      ? (chatId, options) => client.getMessagePage(chatId, options)
      : undefined,
    getChatTitle: roomNames ? (chatId) => roomNames.getChatTitle(chatId) : typeof client.getChatTitle === 'function'
      ? (chatId) => client.getChatTitle(chatId)
      : undefined,
    getCredentials: () => ({ userId }),
    lookupAuthorName: (chatId, authorId) => client.lookupAuthorName(chatId, authorId),
    resolveAuthorName: async (chatId, authorId) => {
      const cached = nonEmptyString(client.lookupAuthorName(chatId, authorId));
      if (cached) return cached;
      if (typeof client.getMembersByIds !== 'function') return null;
      const members = await client.getMembersByIds(chatId, [String(authorId)]);
      if (!Array.isArray(members)) return null;
      const exact = members.find((member) => String(member?.user_id ?? '') === String(authorId));
      return nonEmptyString(exact?.nickname) ?? null;
    },
  });
}

function credentialsHandoffEvent(credentials, authLeaseId) {
  return {
    type: 'credentials',
    auth_lease_id: authLeaseId,
    oauthToken: credentials.oauthToken,
    refreshToken: credentials.refreshToken ?? '',
    userId: credentials.userId,
    deviceUuid: credentials.deviceUuid,
    deviceType: credentials.deviceType,
    credentials: {
      oauth_token: credentials.oauthToken,
      access_token: credentials.oauthToken,
      refresh_token: credentials.refreshToken ?? '',
      user_id: credentials.userId,
      device_uuid: credentials.deviceUuid,
      device_type: credentials.deviceType,
    },
  };
}

async function waitForCredentialSaveAck(input, signal, timeoutMs) {
  let line;
  try { line = await readFirstJsonLine(input, { timeoutMs, signal }); }
  catch (error) {
    if (error?.code === 'input_timeout') throw bridgeError('credentials_ack_timeout');
    throw error;
  }
  const ack = parseJsonLine(line, 'invalid_credentials_ack');
  if (!ack || typeof ack !== 'object' || Array.isArray(ack) || ack.type !== 'credentials_saved') {
    throw bridgeError('credentials_not_saved');
  }
}

export async function runBridge({ input = process.stdin, output = process.stdout, sdk, limits = {} } = {}) {
  const emit = createNdjsonEmitter(output, { timeoutMs: limits.outputMs ?? 10000 });
  const lifetime = new AbortController();
  const onEnd = () => lifetime.abort();
  input.once('end', onEnd);
  input.once('error', onEnd);
  const signalHandlers = ['SIGINT', 'SIGTERM'].map((signal) => {
    const handler = () => lifetime.abort();
    process.once(signal, handler);
    return [signal, handler];
  });
  let outputFailure = null;
  emit.failed.then((error) => { outputFailure = error; lifetime.abort(); });
  let client = null;
  let listener = null;
  let detach = null;
  let heartbeat = null;
  let stopping = false;
  let recovery = null;
  let recoveryController = null;
  let roomNames = null;
  const Client = sdk?.KakaoTalkClient ?? KakaoTalkClient;
  const Listener = sdk?.KakaoTalkListener ?? KakaoTalkListener;
  const refreshToken = sdk?.refreshKakaoOAuthToken ?? refreshKakaoOAuthToken;

  const closeCurrentSession = async () => {
    clearInterval(heartbeat); heartbeat = null;
    roomNames?.close(); roomNames = null;
    recoveryController?.abort();
    try { await withDeadline(() => recovery, 2000, 'operation_timeout'); } catch {}
    recovery = null;
    const currentDetach = detach; detach = null;
    const currentListener = listener; listener = null;
    const currentClient = client; client = null;
    try { currentListener?.stop(); } catch {}
    try { await withDeadline(() => currentDetach?.(), limits.drainMs ?? 15000, 'operation_timeout'); } catch {}
    try { currentClient?.close(); } catch {}
  };

  try {
    emit({ type: 'status', status: 'starting' });
    const line = await readFirstJsonLine(input, { timeoutMs: limits.inputMs ?? 15000, signal: lifetime.signal });
    const request = parseBridgeStartRequest(parseJsonLine(line));
    const credentials = request.credentials;
    const catchUp = request.catchUp;
    const conversationState = createConversationSyncState(catchUp);
    const messageDeduper = createMessageDeduper();
    const emitMessage = (event) => {
      conversationState.observe(event);
      // A recovered duplicate carries body-before-progress semantics. The host
      // decides display dedupe after durability; never hide its recovered copy.
      if (event?.type === 'message' && !event.recovered && !messageDeduper.accept(event)) return Promise.resolve(true);
      return emit(event);
    };
    emit({ type: 'status', status: 'credentials_loaded', userId: credentials.userId, deviceType: credentials.deviceType });

    const startSession = async (suppressRecoverableError) => {
      client = new Client();
      if (Client === KakaoTalkClient || sdk?.useSessionAdapter) {
        installReadOnlySyncCache(client);
        installSessionAdoptionBuffer(client);
      }
      await withDeadline(() => client.login({
        oauthToken: credentials.oauthToken, userId: credentials.userId,
        deviceUuid: credentials.deviceUuid, deviceType: credentials.deviceType,
      }), limits.sessionMs ?? 45000, 'session_timeout', lifetime.signal);
      // The name resolver reads only CHATINFO and account-synced friend names.
      // Its lifetime is tied to this exact session, including late responses.
      roomNames = createRoomNameResolver({ client, credentials,
        ...(sdk?.fetchRoomNames ? { fetchImpl: sdk.fetchRoomNames } : {}),
        onMetadata: (event) => stopping || lifetime.signal.aborted
          ? Promise.resolve(false) : emit(event),
      });
      const facade = readOnlyClientFacade(client, credentials.userId, roomNames, emit);
      listener = new Listener(facade);
      let startupErrorCode = null;
      let finishSession;
      const ended = new Promise((resolve) => { finishSession = resolve; });
      detach = attachNormalizedEvents(listener, (event) => {
        if (stopping) return Promise.resolve(false);
        if (event.type === 'status' && event.status === 'error') {
          startupErrorCode = event.code ?? 'connection_error';
          const recoverable = event.code === 'invalid_access_token' || event.code === 'login_rejected';
          if (suppressRecoverableError && recoverable && credentials.refreshToken) return Promise.resolve(true);
        }
        const delivery = emitMessage(event);
        if (event.type === 'status' && ['disconnected', 'error'].includes(event.status)) {
          recoveryController?.abort();
          finishSession(event.status);
        }
        return delivery;
      }, facade.resolveAuthorName, 1200, credentials.userId, 1024, facade.getChatTitle);
      emit({ type: 'status', status: 'session_starting', userId: credentials.userId });
      await withDeadline(() => listener.start(), limits.sessionMs ?? 45000, 'session_timeout', lifetime.signal);
      return { errorCode: startupErrorCode, connected: !startupErrorCode && client.isConnected(), ended, facade };
    };

    let session = await startSession(true);
    if (!session.connected && ['invalid_access_token', 'login_rejected'].includes(session.errorCode) && credentials.refreshToken) {
      await closeCurrentSession();
      const canonical = (value) => ({ oauth_token: value.oauthToken, refresh_token: value.refreshToken,
        user_id: value.userId, device_uuid: value.deviceUuid, device_type: 'tablet' });
      let leaseSequence = 0;
      const exchange = async (type, fields) => {
        const requestId = `auth-${++leaseSequence}`;
        await emit({ type, request_id: requestId, ...fields });
        const reply = parseJsonLine(await readFirstJsonLine(input, { timeoutMs: limits.leaseMs ?? 15000,
          signal: lifetime.signal }), 'auth_lease_invalid');
        const expectedType = type === 'auth_lease_request' ? 'auth_lease_granted' : 'auth_lease_released';
        if (!reply || reply.type !== expectedType || reply.request_id !== requestId) throw bridgeError('auth_lease_invalid');
        return reply;
      };
      const next = await coordinatedRefresh({
        current: canonical(credentials),
        acquire: async (current) => {
          const reply = await exchange('auth_lease_request', { credentials: current });
          if (reply.ok === true) reply.credentials = canonical(parseBridgeCredentials(reply.credentials));
          return reply;
        },
        refresh: (current) => withDeadline(() => refreshToken({ accessToken: current.oauth_token,
          refreshToken: current.refresh_token, deviceUuid: current.device_uuid }),
          limits.refreshMs ?? 30000, 'token_refresh_timeout', lifetime.signal).catch((error) => {
            if (error?.code === 'token_refresh_timeout') lifetime.abort();
            throw error;
          }),
        persist: async (next, leaseId) => {
          await emit(credentialsHandoffEvent(parseBridgeCredentials(next), leaseId));
          await waitForCredentialSaveAck(input, lifetime.signal, limits.ackMs ?? 10000);
        },
        release: async (leaseId) => {
          // A deadline does not cancel the SDK fetch. On timeout the process
          // exits first; the host then releases the OS lease on child exit.
          if (lifetime.signal.aborted) return;
          const reply = await exchange('auth_lease_release', { auth_lease_id: leaseId });
          if (reply.ok !== true) throw bridgeError('auth_lease_invalid');
        },
      });
      Object.assign(credentials, parseBridgeCredentials(next));
      emit({ type: 'status', status: 'token_refreshed', userId: credentials.userId });
      session = await startSession(false);
    }
    if (!session.connected) {
      if (!session.errorCode) emit({ type: 'status', status: 'error', ...safeError({ code: 'connection_failed' }) });
      return 1;
    }
    emit({ type: 'status', status: 'ready', userId: credentials.userId });
    heartbeat = setInterval(() => emit({ type: 'heartbeat' }), limits.heartbeatMs ?? 15000);
    heartbeat.unref?.();

    // Reuse the same buffered reader only after lease/save ACK exchanges have
    // completed. No input is ever interpreted as a chat action implicitly.
    const seenReplyRequests = new Set();
    const controls = (async () => {
      while (!lifetime.signal.aborted) {
        let line;
        try { line = await readFirstJsonLine(input, { timeoutMs: 1000, signal: lifetime.signal }); }
        catch (error) {
          if (error?.code === 'input_timeout') continue;
          if (error?.code !== 'cancelled') lifetime.abort();
          break;
        }
        let value;
        try { value = parseJsonLine(line); }
        catch { lifetime.abort(); break; }
        const result = await executeReplyCommand({ value, credentials, client, signal: lifetime.signal,
          emit, emitMessage, seenRequests: seenReplyRequests, timeoutMs: limits.replyMs ?? 15000 });
        // A WRITE that lacks a receipt must not linger or be replayed. A fresh
        // connection permits only a new explicit user action.
        if (result.status === 'unknown') { lifetime.abort(); break; }
      }
    })().catch(() => lifetime.abort());

    recoveryController = new AbortController();
    const abortRecovery = () => recoveryController.abort();
    lifetime.signal.addEventListener('abort', abortRecovery, { once: true });
    recovery = runConversationSync({ state: conversationState, client: session.facade,
      discoverUnreadRooms: session.facade.discoverUnreadRooms,
      currentUserId: credentials.userId, resolveAuthorName: session.facade.resolveAuthorName,
      resolveChatTitle: session.facade.getChatTitle, signal: recoveryController.signal, emit, onMessage: emitMessage,
      refreshChatTitles: (chatIds, signal) => roomNames.refresh(chatIds, signal),
      intervalMs: limits.conversationIntervalMs ?? 30000, pageTimeoutMs: limits.historyPageMs ?? 10000,
      maxDurationMs: limits.historyMs ?? 45000, maxMessages: limits.conversationMaxMessages ?? 500,
    }).finally(() => lifetime.signal.removeEventListener('abort', abortRecovery));

    const shutdown = new Promise((resolve) => {
      if (lifetime.signal.aborted) resolve('shutdown');
      else lifetime.signal.addEventListener('abort', () => resolve('shutdown'), { once: true });
    });
    const reason = await Promise.race([shutdown, session.ended]);
    lifetime.abort();
    await controls;
    return reason === 'shutdown' && !outputFailure ? 0 : 1;
  } catch (error) {
    if (error?.code !== 'cancelled') emit({ type: 'status', status: 'error', ...safeError(error) });
    return error?.code === 'cancelled' && !outputFailure ? 0 : 1;
  } finally {
    lifetime.abort();
    await closeCurrentSession();
    stopping = true;
    emit({ type: 'status', status: 'stopped' });
    input.removeListener('end', onEnd);
    input.removeListener('error', onEnd);
    for (const [signal, handler] of signalHandlers) process.removeListener(signal, handler);
    releaseJsonInput(input);
    try { await emit.flush(); } catch {}
    emit.close();
  }
}

async function main() {
  const exitCode = await runBridge();
  // SDK fetch/login internals do not expose cancellation. Terminal process exit
  // follows bounded stdout flush, ensuring an abandoned operation cannot linger.
  process.exit(exitCode);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(async (error) => {
    const emit = createNdjsonEmitter(process.stdout);
    emit({ type: 'status', status: 'error', ...safeError(error) });
    try { await emit.flush(); } catch {}
    releaseJsonInput(process.stdin);
    process.exit(1);
  });
}
