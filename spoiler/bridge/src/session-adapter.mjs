import { bridgeError } from './io.mjs';

// Pinned 2.37.1 does not publish session liveness: its client.state only proves
// adoption, while a pre-adoption onClose callback deliberately leaves it null.
// Validate the exact underlying Node socket at both adoption boundaries.
function sessionIsLive(session) {
  const socket = session?.connection?.socket;
  return socket != null && socket.destroyed === false
    && socket.readable === true && socket.writable === true
    && socket.readableEnded !== true && socket.writableEnded !== true
    && socket.connecting !== true;
}

/** Adapter for pinned agent-messenger 2.37.1. No node_modules modifications.
 * connect() creates the session internally, so its identity is only proven by
 * the returned SessionState. Temporarily collect MSG by identity, then flush
 * only the exact returned session after ensureSession() has adopted it.
 */
export function installSessionAdoptionBuffer(client, { maxMessages = 512, maxBytes = 8 * 1024 * 1024 } = {}) {
  if (!client || !['connect', 'ensureSession', 'dispatchPush', 'close'].every((key) => typeof client[key] === 'function')) {
    throw bridgeError('sdk_adapter_unavailable');
  }
  const original = Object.fromEntries(['connect', 'ensureSession', 'dispatchPush', 'close'].map((key) => [key, client[key]]));
  const pending = new Set();
  const ready = new Map();
  let closed = false;

  client.connect = async function (...args) {
    const attempt = { sessions: new Map(), count: 0, bytes: 0, overflow: false };
    pending.add(attempt);
    try {
      const state = await original.connect.apply(this, args);
      if (closed || attempt.overflow || !sessionIsLive(state?.session)) {
        state?.session?.close();
        throw bridgeError(closed ? 'client_closed' : attempt.overflow ? 'adoption_buffer_full' : 'connection_failed');
      }
      ready.set(state.session, { packets: attempt.sessions.get(state.session) ?? [], bytes: attempt.bytes });
      return state;
    } finally { pending.delete(attempt); }
  };

  client.dispatchPush = function (session, packet) {
    if (closed) return;
    if (this.state?.session === session) return original.dispatchPush.call(this, session, packet);
    if (packet?.method !== 'MSG') return;
    for (const attempt of pending) {
      let bytes;
      try { bytes = Buffer.byteLength(JSON.stringify(packet, (_key, value) => typeof value === 'bigint' ? value.toString() : value)); }
      catch { attempt.overflow = true; continue; }
      if (++attempt.count > maxMessages || (attempt.bytes += bytes) > maxBytes) { attempt.overflow = true; continue; }
      if (!attempt.sessions.has(session)) attempt.sessions.set(session, []);
      attempt.sessions.get(session).push(packet);
    }
    // Between connect resolution and ensureSession adoption, only the exact
    // returned identity is eligible; late traffic from old sessions is ignored.
    if (ready.has(session)) {
      const buffered = ready.get(session);
      let bytes;
      try { bytes = Buffer.byteLength(JSON.stringify(packet, (_key, value) => typeof value === 'bigint' ? value.toString() : value)); }
      catch { bytes = maxBytes + 1; }
      if (buffered.packets.length >= maxMessages || (buffered.bytes += bytes) > maxBytes) {
        ready.delete(session); session.close(); throw bridgeError('adoption_buffer_full');
      }
      buffered.packets.push(packet);
    }
  };

  client.ensureSession = async function (...args) {
    const state = await original.ensureSession.apply(this, args);
    if (!sessionIsLive(state?.session)) {
      ready.delete(state?.session);
      if (this.state?.session === state?.session) { this.state = null; this.initPromise = null; }
      state?.session?.close();
      throw bridgeError('connection_failed');
    }
    if (!closed && this.state?.session === state.session && ready.has(state.session)) {
      const { packets } = ready.get(state.session);
      ready.delete(state.session);
      for (const packet of packets) {
        if (closed || this.state?.session !== state.session) break;
        original.dispatchPush.call(this, state.session, packet);
      }
    }
    return state;
  };
  client.close = function (...args) {
    closed = true; ready.clear();
    for (const attempt of pending) attempt.sessions.clear();
    return original.close.apply(this, args);
  };
  return client;
}
