/*
 * Read-only Kakao friend-alias lookup.
 *
 * Kakao's friend-list response contains considerably more personal data than
 * KakaoSender needs.  This module keeps the HTTP request and the allowlist
 * boundary together: callers receive only a stable user ID, the account-local
 * friend name, the public profile nickname, and an optional direct-chat ID.
 * Phone numbers, UUIDs, profile URLs, status messages, and raw response bodies
 * never leave this module.
 */

const DEFAULT_TIMEOUT_MS = 15_000;
const MAX_NAME_LENGTH = 100;

export class KakaoFriendAliasError extends Error {
  constructor(code, { httpStatus, serverStatus } = {}) {
    super(`Kakao friend alias lookup failed (${code})`);
    this.name = "KakaoFriendAliasError";
    this.code = code;
    if (Number.isInteger(httpStatus)) this.httpStatus = httpStatus;
    if (Number.isInteger(serverStatus)) this.serverStatus = serverStatus;
  }
}

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function normalizedId(value) {
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value) || value <= 0) return "";
    return String(value);
  }
  if (typeof value !== "string") return "";
  const text = value.trim();
  return /^[1-9]\d*$/.test(text) ? text : "";
}

function safeName(value) {
  if (typeof value !== "string") return "";
  const text = value.normalize("NFKC").trim();
  if (!text || text.length > MAX_NAME_LENGTH || /[\u0000-\u001f\u007f]/.test(text)) return "";
  return text;
}

export function sanitizeFriendAliasRecord(value) {
  const input = record(value);
  if (!input) return null;
  const userId = normalizedId(input.userId ?? input.user_id);
  if (!userId) return null;
  return {
    user_id: userId,
    friend_name: safeName(input.friendNickName ?? input.friend_nickname ?? input.friend_name),
    profile_name: safeName(input.nickName ?? input.nickname ?? input.profile_name),
    direct_chat_id: normalizedId(input.directChatId ?? input.direct_chat_id),
  };
}

export function sanitizeFriendAliasResponse(value) {
  const input = record(value);
  if (!input) throw new KakaoFriendAliasError("friend_list_malformed_response");
  const serverStatus = Number.isInteger(input.status) ? input.status : undefined;
  if (serverStatus !== undefined && serverStatus !== 0) {
    throw new KakaoFriendAliasError("friend_list_rejected", { serverStatus });
  }
  if (!Array.isArray(input.friends)) throw new KakaoFriendAliasError("friend_list_malformed_response");
  return input.friends.map(sanitizeFriendAliasRecord).filter(Boolean);
}

function requestConfig(credentials, config = {}) {
  const deviceType = String(credentials.device_type ?? credentials.deviceType ?? "tablet").toLowerCase();
  const os = String(config.os ?? (deviceType === "tablet" ? "android" : "mac")).toLowerCase();
  const apiPrefix = os === "android" ? "android" : "mac";
  const version = String(config.appVersion ?? config.app_version ?? (apiPrefix === "android" ? "25.9.2" : "26.2.0"));
  const language = String(config.language ?? "ko");
  const osVersion = String(config.osVersion ?? config.os_version ?? (apiPrefix === "android" ? "13" : "Windows"));
  return {
    apiPrefix,
    agent: `${apiPrefix}/${version}/${language}`,
    userAgent: `KT/${version} ${apiPrefix === "android" ? "An" : "Md"}/${osVersion} ${language}`,
    language,
  };
}

/** Fetch a full friend list with the current Kakao OAuth credential. */
export async function fetchFriendAliasRecords(
  credentials,
  { fetchImpl = globalThis.fetch, timeoutMs = DEFAULT_TIMEOUT_MS, config = {} } = {},
) {
  const input = record(credentials);
  const oauthToken = input && String(input.oauth_token ?? input.oauthToken ?? "");
  const deviceUuid = input && String(input.device_uuid ?? input.deviceUuid ?? "");
  if (!oauthToken || !deviceUuid) throw new KakaoFriendAliasError("friend_list_credentials_missing");
  if (typeof fetchImpl !== "function") throw new KakaoFriendAliasError("friend_list_fetch_unavailable");

  const request = requestConfig(input, config);
  const query = new URLSearchParams({
    type: JSON.stringify(["plus", "normal"]),
    event_types: JSON.stringify(["create"]),
    token: "0",
  });
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), Math.max(1, Number(timeoutMs) || DEFAULT_TIMEOUT_MS));
  try {
    let response;
    try {
      response = await fetchImpl(`https://katalk.kakao.com/${request.apiPrefix}/friends/list.json?${query}`, {
        method: "GET",
        headers: {
          Authorization: `${oauthToken}-${deviceUuid}`,
          A: request.agent,
          "User-Agent": request.userAgent,
          Accept: "*/*",
          "Accept-Language": request.language,
        },
        signal: controller.signal,
      });
    } catch {
      throw new KakaoFriendAliasError(controller.signal.aborted ? "friend_list_timeout" : "friend_list_request_failed");
    }
    if (!response || !response.ok) {
      const httpStatus = Number.isInteger(response?.status) ? response.status : undefined;
      const code = httpStatus === 401 || httpStatus === 403 ? "friend_list_auth_failed" : "friend_list_http_error";
      throw new KakaoFriendAliasError(code, { httpStatus });
    }
    let body;
    try {
      body = await response.json();
    } catch {
      throw new KakaoFriendAliasError("friend_list_malformed_response", { httpStatus: response.status });
    }
    return sanitizeFriendAliasResponse(body);
  } finally {
    clearTimeout(timeout);
  }
}

/**
 * Attach one account-local friend name only after stable ID checks agree.
 * Duplicate friend records and direct-chat ID disagreements fail closed.
 */
export function enrichDirectChatFriendAliases(chats, aliases, { complete = true } = {}) {
  if (!Array.isArray(chats)) return chats;
  const output = chats.map((chat) => ({
    ...(record(chat) ?? {}),
    peer_local_names: [],
  }));
  if (!complete || !Array.isArray(aliases)) return output;

  const byUserId = new Map();
  for (const rawAlias of aliases) {
    const alias = sanitizeFriendAliasRecord(rawAlias);
    if (!alias) continue;
    const current = byUserId.get(alias.user_id) ?? [];
    current.push(alias);
    byUserId.set(alias.user_id, current);
  }

  return output.map((chat) => {
    if (
      String(chat.type ?? "").trim().toLowerCase() !== "directchat" ||
      chat.active_members !== 2
    ) {
      return chat;
    }
    const chatId = normalizedId(chat.chat_id);
    const peerUserId = normalizedId(chat.peer_user_id);
    const candidates = byUserId.get(peerUserId) ?? [];
    if (!chatId || !peerUserId || candidates.length !== 1) return chat;
    const alias = candidates[0];
    if (!alias.friend_name) return chat;
    if (alias.direct_chat_id && alias.direct_chat_id !== chatId) return chat;
    return { ...chat, peer_local_names: [alias.friend_name] };
  });
}

export { DEFAULT_TIMEOUT_MS as DEFAULT_FRIEND_ALIAS_TIMEOUT_MS };
