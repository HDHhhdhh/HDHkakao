#!/usr/bin/env node

/*
 * Long-lived, newline-delimited JSON bridge for agent-messenger's KakaoTalk
 * client.  stdout is reserved for protocol frames only.  Diagnostics go to
 * stderr and contain stable, redacted codes rather than exception text.
 *
 * The parent process supplies credentials over stdin after decrypting its
 * Windows DPAPI vault.  This process does not read a plaintext credential
 * file.  Direct sending is opt-in: KAKAO_DIRECT_ENABLED must be "1" and the
 * parent may further change the switch with set_enabled.
 */

import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, extname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

import {
  enrichDirectChatPeerIdentities,
  enrichUniqueEmptyMemoChatLabels,
  sanitizeChat,
} from "./kakao_chat_enrichment.mjs";
import {
  enrichDirectChatFriendAliases,
  fetchFriendAliasRecords,
} from "./kakao_friend_aliases.mjs";
import { coordinatedRefresh } from "./kakao_auth_refresh.mjs";
import { sendWriteOnce } from "./kakao_direct_send_once.mjs";
import { sessionAcquireError } from "./kakao_direct_session.mjs";
import { reconcileDirectTextHistory } from "./kakao_direct_reconcile.mjs";
import { prepareAttachments, PreparedAttachmentCache, sendAttachmentsOnce } from "./kakao_direct_media.mjs";

const PROTOCOL_VERSION = 1;
const DEFAULT_SDK_ROOT =
  "C:\\Users\\gkseh\\Documents\\Codex\\2026-08-28\\li\\work\\Spoiler\\bridge\\node_modules\\agent-messenger";
const LOCO_METHOD_PATTERN = /^[A-Z0-9_]{1,11}$/;
const INVALID_LOCO_METHOD = "<invalid-method>";
const LOCO_CAPTURE_WRAPPER = Symbol("kakaoSenderLocoMethodCapture");
const MAX_LOCO_FIELD_DEPTH = 3;
const MAX_LOCO_FIELD_PATHS = 64;
const MAX_LOCO_FIELD_KEY_LENGTH = 64;
const MAX_LOCO_FIELD_PATH_LENGTH = 256;
const MAX_LOCO_ARRAY_ITEMS = 64;
const MAX_LOCO_FIELD_VISITS = 256;
const LOCO_FIELD_CONTROL_PATTERN = /[\u0000-\u001F\u007F-\u009F]/u;
const MAX_LOCO_PROBE_EVENTS = 8;
const MAX_LOCO_PROBE_MARKER_LENGTH = 4096;
const MAX_LOCO_PROBE_JSON_LENGTH = 64 * 1024;
const MAX_LOCO_PROBE_SCALAR_STRING_LENGTH = 64;
const MAX_CHAT_LOOKUP_IDS = 128;
const CHAT_LOOKUP_CONCURRENCY = 6;
const POSITIVE_CHAT_ID_PATTERN = /^[1-9]\d{0,31}$/u;
const PROBE_SAFE_SCALAR_KEYS = new Set([
  "type",
  "scope",
  "sendat",
  "sentat",
  "messagetype",
  "status",
  "isread",
  "isunread",
  "ismine",
  "version",
  "revision",
  "kind",
  "contenttype",
]);
const PROBE_SENSITIVE_KEYS = new Set([
  "message",
  "text",
  "content",
  "chatid",
  "userid",
  "authorid",
  "logid",
  "token",
  "oauthtoken",
  "accesstoken",
  "refreshtoken",
  "name",
  "nickname",
]);
const PROBE_SAFE_ENUM_PATTERN = /^[A-Za-z0-9_.:-]{1,64}$/u;
const PROBE_SAFE_KEY_PATTERN = /^[A-Za-z0-9_:-]{1,64}$/u;

let sdk;
let BsonLong;
let sdkPath;
let mediaTools;
const attachmentCache = new PreparedAttachmentCache();
let client = null;
let directEnabled = process.env.KAKAO_DIRECT_ENABLED === "1";
let credentialState = null;
let shuttingDown = false;
let sequence = 0;
let commandChain = Promise.resolve();
const credentialAckWaiters = new Map();
const authLeaseWaiters = new Map();

function monotonicMilliseconds() {
  return Number(process.hrtime.bigint()) / 1_000_000;
}

function sanitizeLocoMethod(method) {
  return typeof method === "string" && LOCO_METHOD_PATTERN.test(method) ? method : INVALID_LOCO_METHOD;
}

function normalizeProbeKey(key) {
  return typeof key === "string" ? key.toLowerCase().replace(/[-_]/g, "") : "";
}

function isProbeSensitiveKey(key) {
  return PROBE_SENSITIVE_KEYS.has(normalizeProbeKey(key));
}

function normalizeProbeMarker(marker) {
  if (marker === undefined || marker === null) return null;
  if (typeof marker !== "string" || marker.length === 0 || marker.length > MAX_LOCO_PROBE_MARKER_LENGTH) {
    const error = new Error();
    error.code = "invalid_probe_marker";
    throw error;
  }
  return marker;
}

function probeStringContainsMarker(value, marker) {
  return typeof value === "string" && typeof marker === "string" && marker.length > 0 && value.includes(marker);
}

function sanitizeLocoFieldKey(key) {
  if (typeof key !== "string" || key.length === 0 || key.length > MAX_LOCO_FIELD_KEY_LENGTH) return null;
  if (LOCO_FIELD_CONTROL_PATTERN.test(key)) return null;
  return key;
}

function locoValueType(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  const type = typeof value;
  return type;
}

function addLocoFieldPath(fieldPaths, path, value) {
  if (path.length > MAX_LOCO_FIELD_PATH_LENGTH) return false;
  const type = locoValueType(value);
  let entry = fieldPaths.get(path);
  if (!entry && fieldPaths.size >= MAX_LOCO_FIELD_PATHS) return false;
  if (!entry) {
    entry = { path, types: new Map() };
    fieldPaths.set(path, entry);
  }
  entry.types.set(type, (entry.types.get(type) || 0) + 1);
  return true;
}

function readOwnKeys(value) {
  try {
    return value && typeof value === "object" ? Object.keys(value) : [];
  } catch {
    return [];
  }
}

function readProperty(value, key) {
  try {
    return value[key];
  } catch {
    return undefined;
  }
}

function collectLocoFieldPaths(value, prefix, fieldPaths, depth = 1, budget = { remaining: MAX_LOCO_FIELD_VISITS }) {
  if (budget.remaining <= 0 || prefix.length > MAX_LOCO_FIELD_PATH_LENGTH) return;
  if (value === null || typeof value !== "object") {
    addLocoFieldPath(fieldPaths, prefix, value);
    return;
  }
  if (depth > MAX_LOCO_FIELD_DEPTH) return;

  if (Array.isArray(value)) {
    addLocoFieldPath(fieldPaths, prefix, value);
    const itemLimit = Math.min(value.length, MAX_LOCO_ARRAY_ITEMS);
    for (let index = 0; index < itemLimit && budget.remaining > 0; index += 1) {
      budget.remaining -= 1;
      const itemPath = `${prefix}[${index}]`;
      const item = readProperty(value, index);
      addLocoFieldPath(fieldPaths, itemPath, item);
      if (item !== null && typeof item === "object" && depth < MAX_LOCO_FIELD_DEPTH) {
        collectLocoFieldPaths(item, itemPath, fieldPaths, depth + 1, budget);
      }
    }
    return;
  }

  for (const rawKey of readOwnKeys(value)) {
    if (budget.remaining <= 0) break;
    budget.remaining -= 1;
    const key = sanitizeLocoFieldKey(rawKey);
    if (!key) continue;
    const path = `${prefix}.${key}`;
    if (path.length > MAX_LOCO_FIELD_PATH_LENGTH) continue;
    const child = readProperty(value, key);
    addLocoFieldPath(fieldPaths, path, child);
    if (child !== null && typeof child === "object" && depth < MAX_LOCO_FIELD_DEPTH) {
      collectLocoFieldPaths(child, path, fieldPaths, depth + 1, budget);
    }
  }
}

function collectLocoPacketFieldPaths(packet, fieldPaths) {
  if (!packet || typeof packet !== "object") return;
  const budget = { remaining: MAX_LOCO_FIELD_VISITS };
  for (const source of ["body", "data"]) {
    if (budget.remaining <= 0) break;
    const value = readProperty(packet, source);
    if (value !== undefined) collectLocoFieldPaths(value, source, fieldPaths, 1, budget);
  }
}

function serializeLocoFieldPaths(fieldPaths) {
  return Array.from(fieldPaths.values())
    .map((entry) => ({
      path: entry.path,
      types: Array.from(entry.types.entries())
        .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
        .map(([type, count]) => ({ type, count })),
    }))
    .sort((left, right) => (left.path < right.path ? -1 : left.path > right.path ? 1 : 0));
}

function safeProbeScalarValue(value, marker) {
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : undefined;
  if (typeof value !== "string") return undefined;
  if (value.length > MAX_LOCO_PROBE_SCALAR_STRING_LENGTH || LOCO_FIELD_CONTROL_PATTERN.test(value)) return undefined;
  if (!PROBE_SAFE_ENUM_PATTERN.test(value) || probeStringContainsMarker(value, marker)) return undefined;
  return value;
}

function collectProbeChatLogScalars(chatLog, marker) {
  const scalars = [];
  for (const rawKey of readOwnKeys(chatLog)) {
    const key = sanitizeLocoFieldKey(rawKey);
    if (!key || isProbeSensitiveKey(key) || !PROBE_SAFE_SCALAR_KEYS.has(normalizeProbeKey(key))) continue;
    const value = readProperty(chatLog, key);
    if (value === undefined) continue;
    const entry = {
      path: `chatLog.${key}`,
      type: locoValueType(value),
    };
    const safeValue = safeProbeScalarValue(value, marker);
    if (safeValue !== undefined) entry.value = safeValue;
    scalars.push(entry);
  }
  scalars.sort((left, right) => (left.path < right.path ? -1 : left.path > right.path ? 1 : 0));
  return scalars;
}

function collectProbeStructurePaths(value, prefix, fieldPaths, marker, depth = 1, budget = { remaining: MAX_LOCO_FIELD_VISITS }) {
  if (budget.remaining <= 0 || prefix.length > MAX_LOCO_FIELD_PATH_LENGTH) return;
  if (value === null || typeof value !== "object") {
    addLocoFieldPath(fieldPaths, prefix, value);
    return;
  }
  addLocoFieldPath(fieldPaths, prefix, value);
  if (depth > MAX_LOCO_FIELD_DEPTH) return;

  if (Array.isArray(value)) {
    const itemLimit = Math.min(value.length, MAX_LOCO_ARRAY_ITEMS);
    for (let index = 0; index < itemLimit && budget.remaining > 0; index += 1) {
      budget.remaining -= 1;
      const itemPath = `${prefix}[${index}]`;
      const item = readProperty(value, index);
      addLocoFieldPath(fieldPaths, itemPath, item);
      if (item !== null && typeof item === "object" && depth < MAX_LOCO_FIELD_DEPTH) {
        collectProbeStructurePaths(item, itemPath, fieldPaths, marker, depth + 1, budget);
      }
    }
    return;
  }

  for (const rawKey of readOwnKeys(value)) {
    if (budget.remaining <= 0) break;
    budget.remaining -= 1;
    const key = sanitizeLocoFieldKey(rawKey);
    if (!key || !PROBE_SAFE_KEY_PATTERN.test(key) || isProbeSensitiveKey(key) || probeStringContainsMarker(key, marker)) continue;
    const path = `${prefix}.${key}`;
    if (path.length > MAX_LOCO_FIELD_PATH_LENGTH) continue;
    const child = readProperty(value, key);
    addLocoFieldPath(fieldPaths, path, child);
    if (child !== null && typeof child === "object" && depth < MAX_LOCO_FIELD_DEPTH) {
      collectProbeStructurePaths(child, path, fieldPaths, marker, depth + 1, budget);
    }
  }
}

function parseProbeJsonStructure(value) {
  if (typeof value !== "string") return value;
  if (value.length > MAX_LOCO_PROBE_JSON_LENGTH) return undefined;
  try {
    return JSON.parse(value);
  } catch {
    return undefined;
  }
}

function serializeProbeStructure(value, prefix, marker) {
  const parsed = parseProbeJsonStructure(value);
  if (parsed === undefined) return [];
  const fieldPaths = new Map();
  collectProbeStructurePaths(parsed, prefix, fieldPaths, marker);
  return serializeLocoFieldPaths(fieldPaths);
}

function readChatLogForProbe(packet) {
  if (!packet || typeof packet !== "object") return null;
  const body = readProperty(packet, "body");
  if (!body || typeof body !== "object" || Array.isArray(body)) return null;
  const chatLog = readProperty(body, "chatLog");
  if (!chatLog || typeof chatLog !== "object" || Array.isArray(chatLog)) return null;
  return chatLog;
}

function createLocoProbeMatchedEvent(method, packet, offset, marker) {
  const chatLog = readChatLogForProbe(packet);
  if (!chatLog || readProperty(chatLog, "message") !== marker) return null;

  // Keep this shape intentionally value-minimal. The exact marker is used
  // only for comparison and is never copied into a diagnostic result.
  const safeMethod = sanitizeLocoMethod(method) === marker ? "<matched-method>" : sanitizeLocoMethod(method);
  const event = {
    marker_matched: true,
    method: safeMethod,
    offset_ms: offset,
    chat_log_scalars: collectProbeChatLogScalars(chatLog, marker),
  };
  for (const key of ["attachment", "supplement"]) {
    const value = readProperty(chatLog, key);
    if (value === undefined) continue;
    const structure = serializeProbeStructure(value, `chatLog.${key}`, marker);
    if (structure.length > 0) event[key] = structure;
  }
  return event;
}

function sanitizeProbeMessageMatch(message, marker) {
  const result = { marker_matched: true };
  const type = readProperty(message, "type");
  const safeType = safeProbeScalarValue(type, marker);
  if (safeType !== undefined) result.type = safeType;

  const sentAt = readProperty(message, "sent_at");
  if (typeof sentAt === "number" && Number.isFinite(sentAt)) result.sent_at = sentAt;

  for (const key of ["attachment", "supplement"]) {
    const value = readProperty(message, key);
    if (value === undefined) continue;
    const structure = serializeProbeStructure(value, key, marker);
    if (structure.length > 0) result[key] = structure;
  }
  return result;
}

function findProbeMessageMatches(messages, marker) {
  const normalizedMarker = normalizeProbeMarker(marker);
  if (normalizedMarker === null) return [];
  if (!Array.isArray(messages)) return [];
  const matches = [];
  for (const message of messages) {
    if (!message || typeof message !== "object" || Array.isArray(message)) continue;
    // The message is read for exact comparison only. It is never returned,
    // logged, or included in a structure walk.
    if (readProperty(message, "message") !== normalizedMarker) continue;
    matches.push(sanitizeProbeMessageMatch(message, normalizedMarker));
  }
  return matches;
}

function createLocoMethodCapture({ now = monotonicMilliseconds } = {}) {
  const entries = new Map();
  const matchedEvents = [];
  let active = false;
  let startedAt = 0;
  let lastOffset = 0;
  let probeMarker = null;

  function readNow(fallback) {
    try {
      const value = Number(now());
      return Number.isFinite(value) ? value : fallback;
    } catch {
      return fallback;
    }
  }

  function offsetNow() {
    const offset = Math.max(0, readNow(startedAt) - startedAt);
    lastOffset = Math.max(lastOffset, offset);
    return lastOffset;
  }

  return {
    start(marker = null) {
      entries.clear();
      matchedEvents.length = 0;
      probeMarker = normalizeProbeMarker(marker);
      startedAt = readNow(0);
      lastOffset = 0;
      active = true;
    },
    record(method, packet) {
      if (!active) return;
      const sanitizedMethod = sanitizeLocoMethod(method);
      const offset = offsetNow();
      const current = entries.get(sanitizedMethod);
      if (current) {
        current.count += 1;
        current.last_offset_ms = Math.max(current.last_offset_ms, offset);
        collectLocoPacketFieldPaths(packet, current.field_paths);
      } else {
        const fieldPaths = new Map();
        collectLocoPacketFieldPaths(packet, fieldPaths);
        entries.set(sanitizedMethod, {
          method: sanitizedMethod,
          count: 1,
          first_offset_ms: offset,
          last_offset_ms: offset,
          field_paths: fieldPaths,
        });
      }
      if (probeMarker !== null && matchedEvents.length < MAX_LOCO_PROBE_EVENTS) {
        const matched = createLocoProbeMatchedEvent(sanitizedMethod, packet, offset, probeMarker);
        if (matched) matchedEvents.push(matched);
      }
    },
    stop() {
      active = false;
      const hasProbe = probeMarker !== null;
      const safeMatchedEvents = hasProbe ? matchedEvents.slice(0, MAX_LOCO_PROBE_EVENTS) : null;
      const result = Array.from(entries.values(), (entry) => ({
        method: entry.method,
        count: entry.count,
        first_offset_ms: entry.first_offset_ms,
        last_offset_ms: entry.last_offset_ms,
        field_paths: serializeLocoFieldPaths(entry.field_paths),
      }));
      entries.clear();
      matchedEvents.length = 0;
      probeMarker = null;
      startedAt = 0;
      lastOffset = 0;
      result.sort((left, right) => (left.method < right.method ? -1 : left.method > right.method ? 1 : 0));
      return hasProbe ? { entries: result, matched_events: safeMatchedEvents } : { entries: result };
    },
    isActive() {
      return active;
    },
  };
}

function installLocoMethodCapture(instance, capture) {
  if (!instance || !capture || typeof capture.record !== "function") return false;
  const original = instance.dispatchPush;
  if (typeof original !== "function") return false;
  if (original[LOCO_CAPTURE_WRAPPER]) return false;

  function dispatchPushWithCapture(...args) {
    try {
      // dispatchPush(session, packet): inspect the method plus bounded field
      // names/types only. Never copy packet values, message text, or IDs.
      const packet = args[1];
      capture.record(packet && typeof packet === "object" ? packet.method : undefined, packet);
    } catch {
      // Diagnostics must never alter the SDK's push path.
    }
    return original.apply(this, args);
  }

  Object.defineProperty(dispatchPushWithCapture, LOCO_CAPTURE_WRAPPER, {
    configurable: false,
    enumerable: false,
    value: true,
    writable: false,
  });
  instance.dispatchPush = dispatchPushWithCapture;
  return true;
}

function startLocoMethodCapture(capture, candidateClient, marker = null) {
  let connected = false;
  try {
    connected =
      Boolean(candidateClient) &&
      typeof candidateClient.isConnected === "function" &&
      candidateClient.isConnected() === true;
  } catch {
    connected = false;
  }
  if (!connected) {
    const error = new Error();
    error.code = "capture_not_connected";
    throw error;
  }
  const normalizedMarker = normalizeProbeMarker(marker);
  capture.start(normalizedMarker);
  return normalizedMarker === null ? { capturing: true } : { capturing: true, probe_marker_enabled: true };
}

function stopLocoMethodCapture(capture) {
  const stopped = capture.stop();
  return {
    capturing: false,
    entries: stopped.entries,
    ...(stopped.matched_events ? { matched_events: stopped.matched_events } : {}),
  };
}

const locoMethodCapture = createLocoMethodCapture();

function safeCode(value, fallback = "operation_failed") {
  const candidate = typeof value === "string" && value.length > 0 ? value : fallback;
  const sanitized = candidate.replace(/[^A-Za-z0-9_.-]+/g, "_").slice(0, 64);
  return sanitized || fallback;
}

function diagnostic(code) {
  try {
    process.stderr.write(`[kakao-direct:${safeCode(code)}]\n`);
  } catch {
    // stderr is diagnostic-only; a closed pipe must not break protocol work.
  }
}

function frame(payload) {
  try {
    process.stdout.write(`${JSON.stringify(payload)}\n`);
  } catch {
    // The parent will classify a closed stdout pipe as a disconnect.
  }
}

function nextId(prefix = "evt") {
  sequence += 1;
  return `${prefix}-${String(sequence).padStart(8, "0")}`;
}

function sdkEntry(rootValue) {
  const root = resolve(rootValue || DEFAULT_SDK_ROOT);
  if (extname(root).toLowerCase() === ".js" || extname(root).toLowerCase() === ".mjs") {
    return root;
  }
  const candidates = [
    // agent-messenger@2.37.1 package root
    join(root, "dist", "src", "platforms", "kakaotalk", "index.js"),
    // A bundled bridge may expose a flattened dist/index.js.
    join(root, "dist", "index.js"),
    join(root, "index.js"),
    join(root, "dist", "src", "platforms", "kakaotalk", "index.mjs"),
    join(root, "src", "platforms", "kakaotalk", "index.js"),
  ];
  return candidates.find((candidate) => existsSync(candidate)) || candidates[0];
}

function responseError(error, fallbackCode = "operation_failed") {
  const code = safeCode(error?.code, fallbackCode);
  const status = Number.isInteger(error?.serverStatus)
    ? error.serverStatus
    : Number.isInteger(error?.status_code)
      ? error.status_code
      : undefined;
  const providerKind = typeof error?.responseFailureKind === "string" ? error.responseFailureKind : "";
  const beforeWrite = error?.sendPhase === "prewrite";
  const ambiguous =
    !beforeWrite &&
    (providerKind === "transient_or_unknown" ||
      error?.getChatFailureReason === "synthetic_connection_close" ||
      error?.getChatFailureReason === "transport_or_unknown" ||
      code === "process_disconnected" ||
      code === "network_error" ||
      status === -1);
  const body = {
    code,
    // Never forward exception.message: protocol/library failures can include
    // request details or user-provided text.
    message: "KakaoTalk direct operation failed",
    // Errors raised before the one-WRITE boundary are safe to retry.  Only
    // sendWriteOnce/transport ambiguity is marked unknown.
    kind: ambiguous ? "unknown" : "explicit",
    ambiguous,
  };
  if (status !== undefined) body.status_code = status;
  if (beforeWrite && code === "session_acquire_failed") {
    body.send_phase = "prewrite";
    body.retryable_prewrite = error.retryablePrewrite === true;
    body.cause_code = error.acquisitionCauseCode;
    if (Number.isSafeInteger(error.acquisitionCauseStatus)) body.cause_status_code = error.acquisitionCauseStatus;
  }
  return body;
}

function requireObject(value, code = "invalid_request") {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    const error = new Error();
    error.code = code;
    throw error;
  }
  return value;
}

function normalizeChatLookupIds(value) {
  if (!Array.isArray(value) || value.length > MAX_CHAT_LOOKUP_IDS) {
    const error = new Error();
    error.code = "invalid_chat_ids";
    throw error;
  }
  const normalized = [];
  const seen = new Set();
  for (const rawChatId of value) {
    const chatId =
      typeof rawChatId === "string"
        ? rawChatId.trim()
        : Number.isSafeInteger(rawChatId)
          ? String(rawChatId)
          : "";
    if (!POSITIVE_CHAT_ID_PATTERN.test(chatId)) {
      const error = new Error();
      error.code = "invalid_chat_ids";
      throw error;
    }
    if (!seen.has(chatId)) {
      seen.add(chatId);
      normalized.push(chatId);
    }
  }
  return normalized;
}

function stringField(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function normalizeCredentials(raw) {
  const input = requireObject(raw, "invalid_credentials");
  const oauthToken = input.oauth_token ?? input.oauthToken ?? input.access_token ?? input.accessToken;
  const userId = input.user_id ?? input.userId;
  if (typeof oauthToken !== "string" || oauthToken.length === 0) {
    const error = new Error();
    error.code = "missing_token";
    throw error;
  }
  if (typeof userId !== "string" && typeof userId !== "number") {
    const error = new Error();
    error.code = "missing_user_id";
    throw error;
  }
  const normalized = {
    oauth_token: String(oauthToken),
    user_id: String(userId),
    device_uuid: String(input.device_uuid ?? input.deviceUuid ?? `kakao-direct-${userId}`),
    // This companion must never reuse a PC identity: Kakao can evict an
    // existing desktop session when the same device identity logs in.
    device_type: "tablet",
  };
  const refreshToken = input.refresh_token ?? input.refreshToken;
  if (typeof refreshToken === "string" && refreshToken.length > 0) normalized.refresh_token = refreshToken;
  const accountId = input.account_id ?? input.accountId;
  if (typeof accountId === "string" && accountId.length > 0) normalized.account_id = accountId;
  return normalized;
}

function clientCredentials() {
  if (!client || typeof client.getCredentials !== "function") return null;
  try {
    const current = client.getCredentials();
    if (!current || typeof current !== "object") return null;
    const next = {
      ...(credentialState || {}),
      oauth_token: stringField(current.oauthToken, credentialState?.oauth_token || ""),
      user_id: String(current.userId ?? credentialState?.user_id ?? ""),
      device_uuid: String(current.deviceUuid ?? credentialState?.device_uuid ?? ""),
      device_type: current.deviceType ?? credentialState?.device_type ?? "tablet",
    };
    if (credentialState?.refresh_token) next.refresh_token = credentialState.refresh_token;
    if (!next.oauth_token || !next.user_id) return null;
    return next;
  } catch {
    return null;
  }
}

function attachSessionEvents(instance) {
  if (!instance || typeof instance.onSessionEvent !== "function") return;
  instance.onSessionEvent((event) => {
    if (!event || typeof event !== "object") return;
    const state = safeCode(event.type, "session_event");
    frame({ event: "session", state });
  });
}

function createClient() {
  const instance = new sdk.KakaoTalkClient();
  // dispatchPush is a compiled private method at runtime. Install exactly one
  // redacted observer per client before the SDK can dispatch any push.
  installLocoMethodCapture(instance, locoMethodCapture);
  attachSessionEvents(instance);
  return instance;
}

async function closeClient() {
  if (!client) return;
  try {
    if (typeof client.close === "function") client.close();
  } catch {
    diagnostic("client_close_failed");
  }
  client = null;
}

function waitForCredentialAck(eventId, timeoutMs = 5000) {
  return new Promise((resolveAck) => {
    const timer = setTimeout(() => {
      credentialAckWaiters.delete(eventId);
      resolveAck({ saved: false, errorCode: "credential_save_failed" });
    }, timeoutMs);
    credentialAckWaiters.set(eventId, (saved, errorCode) => {
      clearTimeout(timer);
      credentialAckWaiters.delete(eventId);
      resolveAck({ saved: saved === true, errorCode: errorCode === "credential_vault_conflict"
        ? "credential_vault_conflict" : "credential_save_failed" });
    });
  });
}

async function publishCredentials(credentials = clientCredentials(), authLeaseId = null) {
  if (!credentials) return { saved: false, errorCode: "credential_save_failed" };
  credentialState = credentials;
  const eventId = nextId("credentials");
  const acknowledgement = waitForCredentialAck(eventId);
  // Credential values are protocol payload, never diagnostics.  Python saves
  // this frame under DPAPI and replies with credentials_saved.
  frame({ event: "credentials", type: "credentials", event_id: eventId, credentials, ...(authLeaseId ? { auth_lease_id: authLeaseId } : {}) });
  return acknowledgement;
}

async function persistClientCredentials(credentials, authLeaseId = null) {
  const acknowledgement = await publishCredentials(credentials, authLeaseId);
  if (acknowledgement.saved) return;
  // OAuth refresh may rotate the refresh token. Do not use a session whose
  // credentials the parent failed to persist, or restart will lose that token.
  await closeClient();
  const error = new Error();
  error.code = acknowledgement.errorCode;
  throw error;
}

function authFailure(error) {
  // SDK 2.37.1 reports an expired LOGINLIST token as invalid_access_token.
  // Its generic login_failed also wraps network errors; reconnect those
  // without consuming/rotating otherwise valid OAuth credentials.
  let current = error;
  for (let depth = 0; depth < 4 && current; depth += 1) {
    const code = safeCode(current?.code, "");
    // SDK profile wrappers also cover HTTP 5xx and fetch/network errors.
    // Their names alone cannot prove token expiry. Prefer structured auth
    // codes or an explicit HTTP auth rejection; never parse error messages.
    if (
      code === "invalid_access_token" ||
      code === "token_expired" ||
      code === "oauth_expired" ||
      code === "friend_list_auth_failed"
    ) {
      return true;
    }
    if ((code === "profile_request_failed" || code === "get_profile_failed") &&
        (current.httpStatus === 401 || current.httpStatus === 403)) return true;
    current = current?.cause;
  }
  return false;
}

function authLeaseExchange(event, fields) {
  const requestId = nextId("auth-lease");
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      authLeaseWaiters.delete(requestId);
      // No remote operation starts without a grant. After a remote operation,
      // an unacknowledged release makes this session unusable.
      reject(Object.assign(new Error(), { code: "auth_lease_timeout" }));
    }, 15000);
    authLeaseWaiters.set(requestId, (response) => { clearTimeout(timer); authLeaseWaiters.delete(requestId); resolve(response); });
    frame({ event, type: event, request_id: requestId, ...fields });
  });
}

async function refreshAndLogin() {
  if (!credentialState || typeof sdk?.refreshKakaoOAuthToken !== "function") {
    throw Object.assign(new Error(), { code: "refresh_unavailable" });
  }
  try {
    credentialState = normalizeCredentials(await coordinatedRefresh({
      current: credentialState,
      acquire: (credentials) => authLeaseExchange("auth_lease_request", { credentials }),
      refresh: (credentials) => sdk.refreshKakaoOAuthToken({ accessToken: credentials.oauth_token,
        refreshToken: credentials.refresh_token, deviceUuid: credentials.device_uuid }),
      persist: (credentials, leaseId) => persistClientCredentials(credentials, leaseId),
      release: async (leaseId) => {
        const response = await authLeaseExchange("auth_lease_release", { auth_lease_id: leaseId });
        if (response.ok !== true) throw Object.assign(new Error(), { code: "auth_lease_invalid" });
      },
    }));
    await closeClient();
    client = createClient();
    await client.login({ oauthToken: credentialState.oauth_token, userId: credentialState.user_id,
      deviceUuid: credentialState.device_uuid, deviceType: credentialState.device_type });
  } catch (error) {
    await closeClient();
    throw error;
  }
}

async function withRefresh(operation) {
  try {
    return await operation();
  } catch (error) {
    if (!authFailure(error)) throw error;
    await refreshAndLogin();
    return operation();
  }
}

function ensureClient() {
  if (!client) {
    const error = new Error();
    error.code = "not_authenticated";
    throw error;
  }
  return client;
}

async function commandLogin(request) {
  const nextCredentials = normalizeCredentials(request.credentials);
  await closeClient();
  client = createClient();
  await client.login({
    oauthToken: nextCredentials.oauth_token,
    userId: nextCredentials.user_id,
    deviceUuid: nextCredentials.device_uuid,
    deviceType: nextCredentials.device_type,
  });
  credentialState = nextCredentials;
  await persistClientCredentials();
  return { authenticated: true, user_id: nextCredentials.user_id };
}

async function commandListChats(request) {
  const chats = await withRefresh(() =>
    ensureClient().getChats({
      all: request.all !== false,
      search: typeof request.search === "string" && request.search.length > 0 ? request.search : undefined,
      resolveTitles: request.resolve_titles !== false,
    }),
  );
  const completeList = request.all !== false && !(typeof request.search === "string" && request.search.length > 0);
  let enrichedChats = await enrichUniqueEmptyMemoChatLabels(
    chats,
    () => withRefresh(() => ensureClient().getProfile()),
    { complete: completeList },
  );
  if (request.resolve_peer_identities !== false) {
    enrichedChats = await enrichDirectChatPeerIdentities(
      enrichedChats,
      credentialState?.user_id,
      (chatId) => ensureClient().getMemberSnapshot(chatId),
      { complete: completeList },
    );
  }
  // The companion/mobile credential can legitimately expose an empty friend
  // list even though LOCO chats are available. Keep this read-only enrichment
  // as an explicit diagnostic capability, but do not add a slow HTTP request
  // to every normal chat-list refresh.
  if (completeList && request.resolve_friend_aliases === true) {
    try {
      const friendAliases = await withRefresh(() => fetchFriendAliasRecords(credentialState));
      enrichedChats = enrichDirectChatFriendAliases(enrichedChats, friendAliases, { complete: true });
    } catch {
      // Friend aliases are optional enrichment.  A retired endpoint, an old
      // server response, or a transient read must not poison the chat list.
    }
  }
  return { chats: Array.isArray(enrichedChats) ? enrichedChats.map(sanitizeChat) : [] };
}

async function lookupAndVerifyChatsByIds(
  rawChatIds,
  {
    getChat,
    getMemberSnapshot,
    ownUserId,
    concurrency = CHAT_LOOKUP_CONCURRENCY,
  } = {},
) {
  const chatIds = normalizeChatLookupIds(rawChatIds);
  if (chatIds.length === 0) return { chats: [], partial_failure: false };
  if (typeof getChat !== "function" || typeof getMemberSnapshot !== "function") {
    const error = new Error();
    error.code = "invalid_chat_lookup_adapter";
    throw error;
  }
  const records = new Array(chatIds.length);
  let nextIndex = 0;
  let partialFailure = false;
  const worker = async () => {
    while (nextIndex < chatIds.length) {
      const index = nextIndex;
      nextIndex += 1;
      const requestedChatId = chatIds[index];
      try {
        const chat = await getChat(requestedChatId);
        if (
          chat &&
          typeof chat === "object" &&
          String(chat.chat_id ?? "").trim() === requestedChatId
        ) {
          records[index] = chat;
        } else {
          partialFailure = true;
        }
      } catch {
        // One stale/local-only ID must not poison independently verified rooms.
        partialFailure = true;
      }
    }
  };
  await Promise.all(
    Array.from(
      {
        length: Math.min(
          Math.max(1, Math.min(CHAT_LOOKUP_CONCURRENCY, Number.isInteger(concurrency) ? concurrency : 1)),
          chatIds.length,
        ),
      },
      () => worker(),
    ),
  );

  const chats = records.filter((record) => record && typeof record === "object");
  const enrichedChats = await enrichDirectChatPeerIdentities(
    chats,
    ownUserId,
    getMemberSnapshot,
    { complete: true },
  );
  const sanitizedChats = Array.isArray(enrichedChats) ? enrichedChats.map(sanitizeChat) : [];
  const identityIncomplete = sanitizedChats.some(
    (chat) =>
      String(chat.type ?? "").trim().toLowerCase() === "directchat" &&
      chat.active_members === 2 &&
      (!chat.peer_user_id || !Array.isArray(chat.peer_names) || chat.peer_names.length !== 1),
  );
  return {
    chats: sanitizedChats,
    partial_failure: partialFailure || identityIncomplete,
  };
}

async function commandGetChatsByIds(request) {
  const result = await lookupAndVerifyChatsByIds(request.chat_ids, {
    getChat: (chatId) => withRefresh(() => ensureClient().getChat(chatId)),
    getMemberSnapshot: (chatId) => ensureClient().getMemberSnapshot(chatId),
    ownUserId: credentialState?.user_id,
  });
  if (result.partial_failure) diagnostic("chat_lookup_partial");
  return { chats: result.chats };
}

function normalizeProbeChatId(value) {
  if (typeof value !== "string" && typeof value !== "number") {
    const error = new Error();
    error.code = "invalid_chat_id";
    throw error;
  }
  const chatId = String(value);
  if (!/^\d+$/.test(chatId)) {
    const error = new Error();
    error.code = "invalid_chat_id";
    throw error;
  }
  return chatId;
}

function normalizeProbeCount(value) {
  const count = value === undefined || value === null ? 20 : Number(value);
  if (!Number.isInteger(count) || count < 1 || count > 100) {
    const error = new Error();
    error.code = "invalid_probe_count";
    throw error;
  }
  return count;
}

async function commandProbeMessage(request) {
  const chatId = normalizeProbeChatId(request.chat_id);
  const marker = normalizeProbeMarker(request.probe_marker ?? request.marker);
  if (marker === null) {
    const error = new Error();
    error.code = "invalid_probe_marker";
    throw error;
  }
  const count = normalizeProbeCount(request.count);
  const messages = await withRefresh(() => ensureClient().getMessages(chatId, { count }));
  const matches = findProbeMessageMatches(messages, marker);
  return { match_count: matches.length, matches };
}

function historyWatermark(response, chatId, LongType) {
  const fail = () => { const error = new Error(); error.code = "invalid_history_watermark"; throw error; };
  const id = (value) => {
    if (typeof value === "number" && !Number.isSafeInteger(value)) return null;
    if (!["string", "number", "bigint"].includes(typeof value) && !LongType?.isLong(value)) return null;
    const result = String(value);
    return /^(0|[1-9][0-9]{0,18})$/.test(result) && BigInt(result) <= 9223372036854775807n ? result : null;
  };
  const body = response?.body;
  if (response?.method !== "CHATINFO" || response.statusCode !== 0 ||
      !body || typeof body !== "object" || Array.isArray(body) ||
      (Object.hasOwn(body, "status") && body.status !== 0)) fail();
  const info = body.chatInfo;
  if (!info || typeof info !== "object" || Array.isArray(info) || id(info.chatId) !== chatId) fail();
  const watermark = id(info.lastLogId);
  if (watermark === null) fail();
  return watermark;
}

async function commandReconcileMessage(request) {
  // Read-only recovery inspection. A fresh CHATINFO watermark and lossless
  // forward pages are required before absence can authorize a follow-up.
  const chatId = request.chat_id;
  if (typeof chatId !== "string" || !/^[1-9][0-9]{0,18}$/.test(chatId) ||
      BigInt(chatId) > 9223372036854775807n || typeof request.text !== "string" || !request.text.length ||
      !Number.isSafeInteger(request.sent_after) || request.sent_after <= 0 ||
      !Number.isSafeInteger(request.sent_before) || request.sent_before < request.sent_after ||
      request.sent_before > Math.floor(Date.now() / 1000)) {
    const error = new Error(); error.code = "invalid_reconciliation_bounds"; throw error;
  }
  const response = await withRefresh(async () => {
    const session = await ensureClient().acquireSession();
    return session.getChannelInfo(BsonLong.fromString(chatId));
  });
  const throughLogId = historyWatermark(response, chatId, BsonLong);
  const observedAt = Math.floor(Date.now() / 1000);
  const result = await reconcileDirectTextHistory({
    getMessagePage: (id, options) => withRefresh(() => ensureClient().getMessagePage(id, options)),
    chatId, ownUserId: credentialState?.user_id, text: request.text,
    sentAfter: request.sent_after, sentBefore: request.sent_before,
    expectedLogId: request.expected_log_id, throughLogId, observedAt, LongType: BsonLong,
  });
  return { ...result, throughLogId, observedAt };
}

async function commandSendMessage(request) {
  if (!directEnabled) {
    const error = new Error();
    error.code = "direct_disabled";
    throw error;
  }
  if (typeof request.chat_id !== "string" &&
      !(typeof request.chat_id === "number" && Number.isSafeInteger(request.chat_id))) {
    const error = new Error();
    error.code = "invalid_chat_id";
    throw error;
  }
  if (typeof request.text !== "string" || request.text.length === 0) {
    const error = new Error();
    error.code = "message_empty";
    throw error;
  }
  const chatId = String(request.chat_id);
  if (!/^[1-9]\d{0,18}$/.test(chatId) || BigInt(chatId) > 9223372036854775807n || !BsonLong) {
    const error = new Error();
    error.code = "invalid_chat_id";
    throw error;
  }
  // KakaoTalkClient.sendMessage may replay a mutation after replacing a dead
  // session.  Acquire first, then issue exactly one WRITE.  An exception after
  // this boundary is conservatively unknown and must never be auto-retried.
  // Token refresh is allowed only while acquiring a session, before WRITE.
  let session;
  try {
    session = await withRefresh(() => ensureClient().acquireSession());
  } catch (error) {
    throw sessionAcquireError(error);
  }
  return sendWriteOnce(session, BsonLong, chatId, request.text);
}

async function loadMediaTools() {
  if (mediaTools) return mediaTools;
  try {
    const base = dirname(sdkPath);
    const [connection, config] = await Promise.all([
      import(pathToFileURL(join(base, "protocol", "connection.js")).href),
      import(pathToFileURL(join(base, "protocol", "config.js")).href),
    ]);
    if (typeof connection.LocoConnection !== "function" || typeof config.getLocoDeviceConfig !== "function" ||
        typeof sdk.detectImageDimensions !== "function") throw new Error();
    mediaTools = { connectionFactory: () => new connection.LocoConnection(),
      deviceConfig: config.getLocoDeviceConfig("tablet"), mccmnc: config.MCCMNC };
    return mediaTools;
  } catch {
    const error = new Error();
    error.code = "media_sdk_unavailable";
    error.sendPhase = "prewrite";
    throw error;
  }
}

async function commandSendAttachments(request) {
  if (!directEnabled) { const error = new Error(); error.code = "direct_disabled"; throw error; }
  if (typeof request.chat_id !== "string" || !/^[1-9]\d{0,18}$/.test(request.chat_id) ||
      BigInt(request.chat_id) > 9223372036854775807n || typeof request.text !== "string") {
    const error = new Error(); error.code = "invalid_media_request"; throw error;
  }
  const media = await loadMediaTools();
  // Read and validate ALL files before acquiring the sending session or
  // emitting the optional text. Nothing from a rejected batch is delivered.
  const attachments = request.attachment_run_id === undefined
    ? await prepareAttachments(request.attachments, sdk.detectImageDimensions)
    : await attachmentCache.prepare(request.attachment_run_id, request.attachments, sdk.detectImageDimensions);
  let session;
  try { session = await withRefresh(() => ensureClient().acquireSession()); }
  catch (error) { throw sessionAcquireError(error); }
  const userId = clientCredentials()?.user_id;
  return sendAttachmentsOnce({ session, LongType: BsonLong, chatId: request.chat_id, text: request.text,
    attachments, userId, ...media });
}

async function commandPrepareAttachments(request) {
  if (!directEnabled) { const error = new Error(); error.code = "direct_disabled"; throw error; }
  // Local preflight only. No login/session acquisition or network mutation.
  const attachments = await attachmentCache.prepare(request.attachment_run_id, request.attachments,
    sdk.detectImageDimensions);
  return { attachment_count: attachments.length, total_bytes: attachments.reduce((total, item) => total + item.data.length, 0) };
}

function commandReleaseAttachments(request) {
  attachmentCache.release(request.attachment_run_id);
  return { released: true };
}

async function commandSetEnabled(request) {
  directEnabled = request.enabled === true || request.enabled === 1 || request.enabled === "1";
  return { direct_enabled: directEnabled };
}

function commandStartLocoMethodCapture(request) {
  // This command is deliberately synchronous and only calls isConnected().
  // It must never acquire a session or cause a new network connection.
  return startLocoMethodCapture(locoMethodCapture, client, request?.probe_marker);
}

function commandStopLocoMethodCapture() {
  return stopLocoMethodCapture(locoMethodCapture);
}

async function commandShutdown() {
  if (shuttingDown) return { closed: true };
  shuttingDown = true;
  attachmentCache.clear();
  await closeClient();
  // The response frame is written by processRequest immediately after this
  // return.  Exit shortly afterward so a standalone bridge cannot linger on
  // an open stdin handle.
  setTimeout(() => process.exit(0), 50);
  return { closed: true };
}

async function handleCredentialAck(request) {
  const eventId = String(request.ack_for ?? request.event_id ?? "");
  const waiter = credentialAckWaiters.get(eventId);
  if (waiter) waiter(request.ok === true, request.error_code);
  // A direct response lets the Python reader verify that the ACK was accepted
  // in tests and diagnostics without returning credential values.
  return { ack_for: eventId, saved: request.ok === true };
}

async function handleRequest(request) {
  const command = request.command ?? request.cmd ?? (request.type === "credentials_saved" ? "credentials_saved" : undefined);
  switch (command) {
    case "ready":
      return { protocol: PROTOCOL_VERSION, direct_enabled: directEnabled, authenticated: Boolean(client) };
    case "login":
      return commandLogin(request);
    case "list_chats":
      return commandListChats(request);
    case "get_chats_by_ids":
      return commandGetChatsByIds(request);
    case "probe_message":
      return commandProbeMessage(request);
    case "reconcile_message":
      return commandReconcileMessage(request);
    case "send_message":
      return commandSendMessage(request);
    case "send_attachments":
      return commandSendAttachments(request);
    case "prepare_attachments":
      return commandPrepareAttachments(request);
    case "release_attachments":
      return commandReleaseAttachments(request);
    case "set_enabled":
      return commandSetEnabled(request);
    case "start_loco_method_capture":
      return commandStartLocoMethodCapture(request);
    case "stop_loco_method_capture":
      return commandStopLocoMethodCapture();
    case "auth_lease_granted":
    case "auth_lease_released": {
      const waiter = authLeaseWaiters.get(request.request_id);
      if (waiter) waiter(request);
      return { acknowledged: Boolean(waiter) };
    }
    case "credentials_saved":
      return handleCredentialAck(request);
    case "shutdown":
      return commandShutdown();
    default: {
      const error = new Error();
      error.code = "unknown_command";
      throw error;
    }
  }
}

async function processRequest(request) {
  const requestId = request && request.id !== undefined ? String(request.id) : "";
  if (!requestId) return;
  try {
    const result = await handleRequest(requireObject(request));
    frame({ id: requestId, ok: true, result });
  } catch (error) {
    frame({ id: requestId, ok: false, error: responseError(error, "request_failed") });
  }
}

function enqueueRequest(request) {
  const command = request && (request.command ?? request.cmd);
  // credentials_saved must be handled while another request is waiting for
  // its ACK; placing it behind commandChain would deadlock refresh/login.
  if (["credentials_saved", "auth_lease_granted", "auth_lease_released"].includes(command)) {
    processRequest(request).catch(() => diagnostic("ack_failed"));
    return;
  }
  commandChain = commandChain.then(() => processRequest(request));
}

async function startBridge() {
  try {
    const entry = sdkEntry(process.env.KAKAO_AGENT_MESSENGER_ROOT || DEFAULT_SDK_ROOT);
    sdkPath = entry;
    sdk = await import(pathToFileURL(entry).href);
    const requireFromSdk = createRequire(pathToFileURL(entry));
    BsonLong = requireFromSdk("bson").Long;
    if (typeof sdk.KakaoTalkClient !== "function") throw new Error();
  } catch {
    diagnostic("sdk_import_failed");
    process.exitCode = 1;
    return;
  }

  frame({ event: "ready", protocol: PROTOCOL_VERSION, direct_enabled: directEnabled, authenticated: false });
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  input.on("line", (line) => {
    if (!line || shuttingDown) return;
    let request;
    try {
      request = JSON.parse(line);
    } catch {
      diagnostic("invalid_json");
      return;
    }
    enqueueRequest(request);
  });
  input.on("close", async () => {
    if (!shuttingDown) {
      shuttingDown = true;
      attachmentCache.clear();
      await closeClient();
    }
  });
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  await startBridge();
}

export {
  authFailure,
  responseError,
  historyWatermark,
  createLocoMethodCapture,
  findProbeMessageMatches,
  installLocoMethodCapture,
  lookupAndVerifyChatsByIds,
  normalizeChatLookupIds,
  normalizeProbeMarker,
  sanitizeLocoMethod,
  startLocoMethodCapture,
  stopLocoMethodCapture,
};
