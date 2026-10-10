import assert from "node:assert/strict";
import test from "node:test";

import {
  enrichDirectChatPeerIdentities,
  enrichUniqueEmptyMemoChatLabels,
  peerIdentityFromSnapshot,
  SANITIZED_CHAT_FIELDS,
  sanitizeChat,
} from "../bridge/kakao_chat_enrichment.mjs";

function emptyMemoChat(id = "self") {
  return {
    chat_id: id,
    type: "MemoChat",
    title: null,
    display_name: "",
    active_members: 1,
    last_message: { message: "PRIVATE MESSAGE", author_name: "PRIVATE NAME" },
  };
}

test("a unique empty MemoChat is enriched from nickname only", async () => {
  const chats = [emptyMemoChat("self"), { chat_id: "group", type: "MultiChat", title: "수학방" }];
  let profileCalls = 0;
  const enriched = await enrichUniqueEmptyMemoChatLabels(
    chats,
    async () => {
      profileCalls += 1;
      return {
        nickname: "ㅎㄷㅎ",
        status_message: "PRIVATE STATUS",
        account_email: "PRIVATE EMAIL",
        profile_image_url: "PRIVATE IMAGE",
      };
    },
  );

  assert.equal(profileCalls, 1);
  assert.equal(enriched[0].title, "ㅎㄷㅎ");
  assert.equal(enriched[0].display_name, "ㅎㄷㅎ");
  const safe = sanitizeChat(enriched[0]);
  assert.deepEqual(Object.keys(safe), [...SANITIZED_CHAT_FIELDS]);
  assert.equal(safe.chat_id, "self");
  assert.equal(safe.title, "ㅎㄷㅎ");
  assert.equal(safe.display_name, "ㅎㄷㅎ");
  assert.equal("PRIVATE MESSAGE" in safe, false);
  assert.equal("PRIVATE STATUS" in safe, false);
  assert.equal("PRIVATE EMAIL" in safe, false);
  assert.equal("profile_image_url" in safe, false);
});

test("profile failure leaves the empty MemoChat unchanged and keeps list success possible", async () => {
  const chats = [emptyMemoChat()];
  const enriched = await enrichUniqueEmptyMemoChatLabels(chats, async () => {
    throw new Error("profile failure with secret text must not escape");
  });

  assert.strictEqual(enriched, chats);
  assert.equal(enriched[0].title, null);
  assert.equal(enriched[0].display_name, "");
});

test("a refresh-wrapped profile callback can retry an expired token and enrich", async () => {
  const chats = [emptyMemoChat()];
  let profileAttempts = 0;
  const refreshWrapped = async () => {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      profileAttempts += 1;
      try {
        if (profileAttempts === 1) {
          const error = new Error("SECRET TOKEN must not escape");
          error.code = "profile_request_failed";
          throw error;
        }
        return { nickname: "ㅎㄷㅎ", account_email: "PRIVATE EMAIL" };
      } catch (error) {
        if (attempt === 0 && error?.code === "profile_request_failed") continue;
        throw error;
      }
    }
  };
  const enriched = await enrichUniqueEmptyMemoChatLabels(chats, refreshWrapped);

  assert.equal(profileAttempts, 2);
  assert.equal(enriched[0].title, "ㅎㄷㅎ");
  assert.equal(enriched[0].display_name, "ㅎㄷㅎ");
});

test("multiple MemoChats never receive an arbitrary profile label", async () => {
  const chats = [emptyMemoChat("self-1"), emptyMemoChat("self-2")];
  let profileCalls = 0;
  const enriched = await enrichUniqueEmptyMemoChatLabels(chats, async () => {
    profileCalls += 1;
    return { nickname: "ㅎㄷㅎ" };
  });

  assert.equal(profileCalls, 0);
  assert.strictEqual(enriched, chats);
  assert.equal(enriched.every((chat) => chat.title === null && chat.display_name === ""), true);
});

test("partial or filtered lists do not use a coincidental MemoChat", async () => {
  const chats = [emptyMemoChat()];
  let profileCalls = 0;
  const enriched = await enrichUniqueEmptyMemoChatLabels(
    chats,
    async () => {
      profileCalls += 1;
      return { nickname: "ㅎㄷㅎ" };
    },
    { complete: false },
  );

  assert.equal(profileCalls, 0);
  assert.strictEqual(enriched, chats);
});

test("sanitized chat output has an explicit allowlist and no SDK secret fields", () => {
  const safe = sanitizeChat({
    chat_id: 7,
    type: "MemoChat",
    title: "ㅎㄷㅎ",
    display_name: "ㅎㄷㅎ",
    active_members: 1,
    unread_count: 9,
    last_message: { message: "PRIVATE MESSAGE" },
    oauth_token: "SECRET TOKEN",
    profile: { account_email: "PRIVATE EMAIL" },
  });

  assert.deepEqual(Object.keys(safe), [...SANITIZED_CHAT_FIELDS]);
  assert.deepEqual(safe, {
    chat_id: "7",
    type: "MemoChat",
    display_name: "ㅎㄷㅎ",
    title: "ㅎㄷㅎ",
    active_members: 1,
    peer_user_id: null,
    peer_names: [],
    peer_local_names: [],
  });
  assert.equal(JSON.stringify(safe).includes("SECRET"), false);
  assert.equal(JSON.stringify(safe).includes("PRIVATE"), false);
});

test("a complete DirectChat snapshot exposes only the single peer ID and nickname", () => {
  const identity = peerIdentityFromSnapshot(
    {
      chat_id: "700",
      active_members: 2,
      complete: true,
      members: [
        { user_id: "10", nickname: "내 이름", status_message: "PRIVATE SELF STATUS" },
        {
          user_id: "20",
          nickname: "상대 이름",
          profile_image_url: "PRIVATE IMAGE",
          status_message: "PRIVATE PEER STATUS",
        },
      ],
    },
    "10",
    "700",
  );

  assert.deepEqual(identity, { peer_user_id: "20", peer_names: ["상대 이름"] });
  assert.equal(JSON.stringify(identity).includes("PRIVATE"), false);
});

test("member snapshot mismatches and incomplete identities fail closed", () => {
  const base = {
    chat_id: "700",
    active_members: 2,
    complete: true,
    members: [
      { user_id: "10", nickname: "나" },
      { user_id: "20", nickname: "상대" },
    ],
  };

  assert.equal(peerIdentityFromSnapshot({ ...base, complete: false }, "10", "700"), null);
  assert.equal(peerIdentityFromSnapshot({ ...base, chat_id: "701" }, "10", "700"), null);
  assert.equal(peerIdentityFromSnapshot({ ...base, active_members: 3 }, "10", "700"), null);
  assert.equal(peerIdentityFromSnapshot({ ...base, members: [base.members[1], base.members[1]] }, "10", "700"), null);
  assert.equal(peerIdentityFromSnapshot({ ...base, members: [{ user_id: "10", nickname: "나" }] }, "10", "700"), null);
  assert.equal(
    peerIdentityFromSnapshot(
      { ...base, members: [{ user_id: "10", nickname: "나" }, { user_id: "20", nickname: "" }] },
      "10",
      "700",
    ),
    null,
  );
});

test("peer enrichment queries only full-list two-person DirectChats and isolates failures", async () => {
  const chats = [
    { chat_id: "1", type: "DirectChat", active_members: 2, title: "첫 방" },
    { chat_id: "2", type: "DirectChat", active_members: 2, title: "둘째 방" },
    { chat_id: "3", type: "MultiChat", active_members: 2, title: "그룹" },
    { chat_id: "4", type: "MemoChat", active_members: 1, title: "나" },
  ];
  const calls = [];
  const enriched = await enrichDirectChatPeerIdentities(chats, "10", async (chatId) => {
    calls.push(chatId);
    if (chatId === "2") throw new Error("PRIVATE FAILURE");
    return {
      chat_id: chatId,
      active_members: 2,
      complete: true,
      members: [
        { user_id: "10", nickname: "나" },
        { user_id: "20", nickname: "상대", profile_image_url: "PRIVATE IMAGE" },
      ],
    };
  });

  assert.deepEqual(calls.sort(), ["1", "2"]);
  assert.equal(enriched[0].peer_user_id, "20");
  assert.deepEqual(enriched[0].peer_names, ["상대"]);
  assert.equal(enriched[1].peer_user_id, null);
  assert.deepEqual(enriched[1].peer_names, []);
  assert.equal(enriched[2].peer_user_id, null);
  assert.equal(JSON.stringify(enriched).includes("PRIVATE"), false);

  let partialCalls = 0;
  const partial = await enrichDirectChatPeerIdentities(chats, "10", async () => {
    partialCalls += 1;
    return {};
  }, { complete: false });
  assert.equal(partialCalls, 0);
  assert.equal(partial.every((chat) => chat.peer_user_id === null), true);
});

test("sanitizer accepts verified peer identity only for a two-person DirectChat", () => {
  const safe = sanitizeChat({
    chat_id: "700",
    type: "DirectChat",
    active_members: 2,
    title: null,
    display_name: null,
    peer_user_id: "20",
    peer_names: ["상대 이름"],
    members: [{ status_message: "PRIVATE" }],
  });
  assert.equal(safe.peer_user_id, "20");
  assert.deepEqual(safe.peer_names, ["상대 이름"]);
  assert.deepEqual(safe.peer_local_names, []);
  assert.equal(JSON.stringify(safe).includes("PRIVATE"), false);

  const unsafeGroup = sanitizeChat({ ...safe, type: "MultiChat" });
  assert.equal(unsafeGroup.peer_user_id, null);
  assert.deepEqual(unsafeGroup.peer_names, []);
  assert.deepEqual(unsafeGroup.peer_local_names, []);
  const duplicateNames = sanitizeChat({ ...safe, peer_names: ["상대 이름", "다른 이름"] });
  assert.equal(duplicateNames.peer_user_id, null);
  assert.deepEqual(duplicateNames.peer_names, []);
  assert.deepEqual(duplicateNames.peer_local_names, []);
});

test("sanitizer keeps one local friend alias only behind a verified peer identity", () => {
  const safe = sanitizeChat({
    chat_id: "700",
    type: "DirectChat",
    active_members: 2,
    peer_user_id: "20",
    peer_names: ["프로필명"],
    peer_local_names: ["A 저동2 김가현"],
    friend: { phoneNumber: "PRIVATE PHONE" },
  });
  assert.deepEqual(safe.peer_local_names, ["A 저동2 김가현"]);
  assert.equal(JSON.stringify(safe).includes("PRIVATE"), false);

  const noPeer = sanitizeChat({ ...safe, peer_user_id: null, peer_names: [] });
  assert.deepEqual(noPeer.peer_local_names, []);
});
