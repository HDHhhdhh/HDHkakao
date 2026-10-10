/*
 * Small, side-effect-free helpers for the sanitized chat-list boundary.
 *
 * The Kakao SDK returns a rich profile object.  Chat-list callers only need
 * the current nickname for the one self-chat whose server labels are empty,
 * so this module deliberately reads and returns no other profile fields.
 */

const SANITIZED_CHAT_FIELDS = Object.freeze([
  "chat_id",
  "type",
  "display_name",
  "title",
  "active_members",
  "peer_user_id",
  "peer_names",
  "peer_local_names",
]);

const DEFAULT_PEER_IDENTITY_CONCURRENCY = 6;
const MAX_PEER_IDENTITY_CONCURRENCY = 8;
const MAX_PEER_NAME_LENGTH = 100;

function isBlankLabel(value) {
  return value === null || value === undefined || (typeof value === "string" && value.trim().length === 0);
}

function isMemoChat(chat) {
  return Boolean(chat && typeof chat === "object" && chat.type === "MemoChat");
}

function profileNickname(profile) {
  if (!profile || typeof profile !== "object") return "";
  const nickname = profile.nickname;
  return typeof nickname === "string" ? nickname.trim() : "";
}

function normalizedUserId(value) {
  const text = typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
  return /^[1-9]\d*$/.test(text) ? text : "";
}

function safePeerName(value) {
  if (typeof value !== "string") return "";
  const text = value.normalize("NFKC").trim();
  if (!text || text.length > MAX_PEER_NAME_LENGTH || /[\u0000-\u001f\u007f]/.test(text)) return "";
  return text;
}

function isTwoPersonDirectChat(chat) {
  return Boolean(
    chat &&
      typeof chat === "object" &&
      String(chat.type ?? "").trim().toLowerCase() === "directchat" &&
      chat.active_members === 2,
  );
}

/**
 * Reduce a consistency-checked SDK member snapshot to one safe peer identity.
 * Full member/profile records never cross this boundary.
 */
export function peerIdentityFromSnapshot(snapshot, ownUserId, expectedChatId) {
  if (!snapshot || typeof snapshot !== "object" || snapshot.complete !== true) return null;
  if (String(snapshot.chat_id ?? "") !== String(expectedChatId ?? "")) return null;
  if (snapshot.active_members !== 2 || !Array.isArray(snapshot.members) || snapshot.members.length !== 2) return null;

  const ownId = normalizedUserId(ownUserId);
  if (!ownId) return null;
  const members = [];
  const seenIds = new Set();
  for (const rawMember of snapshot.members) {
    if (!rawMember || typeof rawMember !== "object") return null;
    const userId = normalizedUserId(rawMember.user_id);
    if (!userId || seenIds.has(userId)) return null;
    seenIds.add(userId);
    members.push({ userId, nickname: safePeerName(rawMember.nickname) });
  }

  if (members.filter((member) => member.userId === ownId).length !== 1) return null;
  const peers = members.filter((member) => member.userId !== ownId);
  if (peers.length !== 1 || !peers[0].nickname) return null;
  return { peer_user_id: peers[0].userId, peer_names: [peers[0].nickname] };
}

/**
 * Enrich only complete two-person DirectChats.  Snapshot failures are isolated
 * per room so a transient read cannot poison the full chat list.  Concurrency
 * is bounded because each SDK snapshot performs a stable double read.
 */
export async function enrichDirectChatPeerIdentities(
  chats,
  ownUserId,
  getMemberSnapshot,
  { complete = true, concurrency = DEFAULT_PEER_IDENTITY_CONCURRENCY } = {},
) {
  if (!Array.isArray(chats)) return chats;
  const output = chats.map((chat) => ({
    ...(chat && typeof chat === "object" ? chat : {}),
    peer_user_id: null,
    peer_names: [],
    peer_local_names: [],
  }));
  if (!complete || !normalizedUserId(ownUserId) || typeof getMemberSnapshot !== "function") return output;

  const eligibleIndexes = output.flatMap((chat, index) => (isTwoPersonDirectChat(chat) ? [index] : []));
  let nextIndex = 0;
  const requestedConcurrency = Number.isInteger(concurrency) ? concurrency : DEFAULT_PEER_IDENTITY_CONCURRENCY;
  const workerCount = Math.min(
    eligibleIndexes.length,
    Math.max(1, Math.min(MAX_PEER_IDENTITY_CONCURRENCY, requestedConcurrency)),
  );

  const worker = async () => {
    while (nextIndex < eligibleIndexes.length) {
      const chatIndex = eligibleIndexes[nextIndex];
      nextIndex += 1;
      const chat = output[chatIndex];
      try {
        const identity = peerIdentityFromSnapshot(
          await getMemberSnapshot(String(chat.chat_id ?? "")),
          ownUserId,
          chat.chat_id,
        );
        if (identity) output[chatIndex] = { ...chat, ...identity };
      } catch {
        // Fail closed for this room; no exception text or profile field is exposed.
      }
    }
  };
  await Promise.all(Array.from({ length: workerCount }, () => worker()));
  return output;
}

/**
 * Add the tablet account nickname to exactly one unlabeled MemoChat.
 *
 * A complete list must be supplied by the caller.  Requiring one MemoChat in
 * that list (not merely one empty candidate) prevents a filtered/partial list
 * from causing an arbitrary self-chat choice.  Profile failures intentionally
 * return the original list unchanged.
 */
export async function enrichUniqueEmptyMemoChatLabels(chats, getProfile, { complete = true } = {}) {
  if (!complete || !Array.isArray(chats)) return chats;

  const memoChats = chats.filter(isMemoChat);
  if (memoChats.length !== 1) return chats;

  const target = memoChats[0];
  if (!isBlankLabel(target.title) || !isBlankLabel(target.display_name)) return chats;
  if (typeof getProfile !== "function") return chats;

  let nickname = "";
  try {
    nickname = profileNickname(await getProfile());
  } catch {
    return chats;
  }
  if (!nickname) return chats;

  return chats.map((chat) =>
    chat === target
      ? {
          ...chat,
          // Keep both existing label slots aligned for exact name matching.
          title: nickname,
          display_name: nickname,
        }
      : chat,
  );
}

/**
 * Return only the fields the Python mapping UI is allowed to receive.
 * In particular, never forward last_message, members, profile data, or any
 * SDK-specific fields that may contain message content or PII.
 */
export function sanitizeChat(chat) {
  const input = chat && typeof chat === "object" ? chat : {};
  const type =
    typeof input.type === "string" || (typeof input.type === "number" && Number.isFinite(input.type))
      ? input.type
      : "unknown";
  const sanitized = {
    chat_id: String(input.chat_id ?? ""),
    type,
    display_name: typeof input.display_name === "string" || input.display_name === null ? input.display_name : null,
    title: typeof input.title === "string" || input.title === null ? input.title : null,
    active_members: Number.isInteger(input.active_members) ? input.active_members : 0,
    peer_user_id: null,
    peer_names: [],
    peer_local_names: [],
  };
  if (isTwoPersonDirectChat(sanitized)) {
    const peerUserId = normalizedUserId(input.peer_user_id);
    const peerNames = Array.isArray(input.peer_names)
      ? [...new Set(input.peer_names.map(safePeerName).filter(Boolean))]
      : [];
    if (peerUserId && peerNames.length === 1) {
      sanitized.peer_user_id = peerUserId;
      sanitized.peer_names = peerNames;
      const peerLocalNames = Array.isArray(input.peer_local_names)
        ? [...new Set(input.peer_local_names.map(safePeerName).filter(Boolean))]
        : [];
      if (peerLocalNames.length === 1) sanitized.peer_local_names = peerLocalNames;
    }
  }
  return Object.fromEntries(SANITIZED_CHAT_FIELDS.map((field) => [field, sanitized[field]]));
}

export { DEFAULT_PEER_IDENTITY_CONCURRENCY, SANITIZED_CHAT_FIELDS };
