// Empty CREATE rooms can reject CHATINFO with -401 while CHATONROOM is valid.
// Use only metadata reads, and require a stable, complete two-person identity.
function invalidMetadata() {
  const error = new Error("direct_room_metadata_unverified");
  error.code = "direct_room_metadata_unverified";
  return error;
}

function identity(value, LongType) {
  if (typeof value === "number" && !Number.isSafeInteger(value)) throw invalidMetadata();
  if (typeof value !== "number" && typeof value !== "string" && !LongType.isLong(value)) throw invalidMetadata();
  const text = String(value);
  if (!/^[1-9]\d{0,18}$/.test(text) || BigInt(text) > 9223372036854775807n) throw invalidMetadata();
  return text;
}

function bodyOf(response, method) {
  if (response?.statusCode === -1) {
    const error = new Error("direct_room_metadata_disconnected");
    error.code = "direct_room_metadata_disconnected";
    throw error;
  }
  if (response?.statusCode !== 0 || response.body?.status !== 0
      || (response.method !== undefined && response.method !== method)) throw invalidMetadata();
  return response.body;
}

function membersOf(records, LongType) {
  if (!Array.isArray(records) || records.length !== 2) throw invalidMetadata();
  const members = records.map((record) => {
    const user_id = identity(record?.userId, LongType);
    const nickname = typeof record?.nickName === "string" ? record.nickName.trim() : "";
    if (!nickname || nickname.length > 100 || /[\u0000-\u001f\u007f]/u.test(nickname)) throw invalidMetadata();
    return { user_id, nickname };
  }).sort((a, b) => a.user_id.localeCompare(b.user_id));
  if (members[0].user_id === members[1].user_id) throw invalidMetadata();
  return members;
}

function memberKey(members) {
  return members.map((member) => member.user_id).join(",");
}

export async function readDirectRoomMetadata(session, LongType, chatId, ownUserId, { isActive = () => true } = {}) {
  const expectedId = identity(chatId, LongType);
  const ownId = identity(ownUserId, LongType);
  const parsedId = LongType.fromString(expectedId);
  const checkActive = () => {
    if (!isActive()) throw Object.assign(new Error("metadata_lookup_cancelled"), { code: "metadata_lookup_cancelled" });
  };
  const read = async () => {
    checkActive();
    const room = bodyOf(await session.getChatInfo(parsedId), "CHATONROOM"); // no NOTIREAD
    if (identity(room.c, LongType) !== expectedId || room.t !== "DirectChat" || room.left === true) throw invalidMetadata();
    const displayed = membersOf(room.m, LongType);
    checkActive();
    const members = membersOf(bodyOf(await session.getAllMembers(parsedId), "GETMEM").members, LongType);
    if (!members.some((member) => member.user_id === ownId) || memberKey(displayed) !== memberKey(members)) {
      throw invalidMetadata();
    }
    // The provider omits a/w on subsequent CHATONROOM calls in one session.
    // Complete m + GETMEM membership is still required on both reads.
    if (room.a !== undefined && (!Array.isArray(room.a) || room.a.length !== 2
        || room.a.map((id) => identity(id, LongType)).sort().join(",") !== memberKey(members))) throw invalidMetadata();
    return members;
  };
  const first = await read();
  const second = await read();
  checkActive();
  if (memberKey(first) !== memberKey(second)) throw invalidMetadata();
  const peer = second.find((member) => member.user_id !== ownId);
  return {
    chat: { chat_id: expectedId, type: "DirectChat", active_members: 2,
      display_name: peer.nickname, title: null },
    snapshot: { chat_id: expectedId, active_members: 2, complete: true, members: second,
      consistency_basis: "stable_double_read_chatonroom_getmem" },
  };
}

// Retry metadata once only when the SDK confirms that the previous connection
// died. Restart the complete snapshot; never combine reads from two sessions.
export async function readDirectRoomMetadataForClient(getClient, LongType, chatId, ownUserId, options = {}) {
  const current = getClient();
  const session = await current.acquireSession();
  try {
    return await readDirectRoomMetadata(session, LongType, chatId, ownUserId, options);
  } catch (error) {
    if (error?.code === "direct_room_metadata_unverified" || error?.code === "metadata_lookup_cancelled"
        || typeof current.isConnected !== "function" || current.isConnected() !== false
        || (options.isActive && !options.isActive())) throw error;
    const replacement = await getClient().acquireSession();
    if (replacement === session) throw error;
    return readDirectRoomMetadata(replacement, LongType, chatId, ownUserId, options);
  }
}

export function isMissingChannelInfo(response) {
  return response?.statusCode === 0 && response.body?.status === -401
    && (response.method === undefined || response.method === "CHATINFO");
}

function missingChannelError(error, code) {
  return error?.code === code && error.serverStatus === -401 && error.responseStatusSource === "body";
}

// A lookup-scoped cache shares the same verified snapshot with the identity
// enrichment pass. It is discarded after the command, never persisted.
export function createMetadataLookupAdapter({ client, getClient = () => client, LongType, ownUserId, run = (operation) => operation() }) {
  const snapshots = new Map();
  const lookup = (chatId) => run(() => readDirectRoomMetadataForClient(getClient, LongType, chatId, ownUserId));
  return {
    async getChat(chatId) {
      snapshots.delete(chatId);
      try {
        return await run(() => getClient().getChat(chatId));
      } catch (error) {
        if (!missingChannelError(error, "get_chat_failed")) throw error;
        const result = await lookup(chatId);
        snapshots.set(chatId, result.snapshot);
        return result.chat;
      }
    },
    async getMemberSnapshot(chatId) {
      if (snapshots.has(chatId)) {
        const snapshot = snapshots.get(chatId);
        snapshots.delete(chatId);
        return snapshot;
      }
      try {
        return await run(() => getClient().getMemberSnapshot(chatId));
      } catch (error) {
        if (!missingChannelError(error, "get_member_snapshot_failed")) throw error;
        return (await lookup(chatId)).snapshot;
      }
    },
  };
}
