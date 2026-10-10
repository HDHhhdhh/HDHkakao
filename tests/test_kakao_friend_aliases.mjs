import assert from "node:assert/strict";
import test from "node:test";

import {
  enrichDirectChatFriendAliases,
  fetchFriendAliasRecords,
  sanitizeFriendAliasRecord,
  sanitizeFriendAliasResponse,
} from "../bridge/kakao_friend_aliases.mjs";

test("friend records cross the boundary with only stable IDs and names", () => {
  const safe = sanitizeFriendAliasRecord({
    userId: 20,
    friendNickName: " A 저동2 김가현 ",
    nickName: "김가현",
    directChatId: 700,
    phoneNumber: "PRIVATE PHONE",
    UUID: "PRIVATE UUID",
    statusMessage: "PRIVATE STATUS",
    profileImageUrl: "PRIVATE IMAGE",
  });
  assert.deepEqual(safe, {
    user_id: "20",
    friend_name: "A 저동2 김가현",
    profile_name: "김가현",
    direct_chat_id: "700",
  });
  assert.equal(JSON.stringify(safe).includes("PRIVATE"), false);
  assert.equal(sanitizeFriendAliasRecord({ userId: Number.MAX_SAFE_INTEGER + 1 }), null);
});

test("friend-list fetch is GET-only and returns a sanitized response", async () => {
  let requestUrl = "";
  let requestOptions;
  const records = await fetchFriendAliasRecords(
    { oauth_token: "SECRET TOKEN", device_uuid: "DEVICE", device_type: "tablet" },
    {
      fetchImpl: async (url, options) => {
        requestUrl = String(url);
        requestOptions = options;
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              status: 0,
              token: 100,
              friends: [
                {
                  userId: "20",
                  friendNickName: "내 지정명",
                  nickName: "프로필명",
                  directChatId: "700",
                  phoneNumber: "PRIVATE PHONE",
                },
              ],
            };
          },
        };
      },
    },
  );
  assert.equal(requestOptions.method, "GET");
  assert.equal(requestOptions.headers.Authorization, "SECRET TOKEN-DEVICE");
  assert.match(requestOptions.headers.A, /^android\//);
  assert.match(requestUrl, /^https:\/\/katalk\.kakao\.com\/android\/friends\/list\.json\?/);
  assert.match(requestUrl, /event_types=/);
  assert.deepEqual(records, [
    { user_id: "20", friend_name: "내 지정명", profile_name: "프로필명", direct_chat_id: "700" },
  ]);
  assert.equal(JSON.stringify(records).includes("PRIVATE"), false);
});

test("malformed and rejected friend-list responses fail with stable codes", () => {
  assert.throws(() => sanitizeFriendAliasResponse({ status: -1, message: "SECRET" }), {
    code: "friend_list_rejected",
  });
  assert.throws(() => sanitizeFriendAliasResponse({ status: 0, friends: null }), {
    code: "friend_list_malformed_response",
  });
});

test("local aliases enrich only the same verified one-to-one peer and room", () => {
  const chats = [
    {
      chat_id: "700",
      type: "DirectChat",
      active_members: 2,
      peer_user_id: "20",
      peer_names: ["프로필명"],
    },
    { chat_id: "701", type: "MultiChat", active_members: 2, peer_user_id: "21" },
  ];
  const enriched = enrichDirectChatFriendAliases(chats, [
    { user_id: "20", friend_name: "A 저동2 김가현", profile_name: "프로필명", direct_chat_id: "700" },
    { user_id: "21", friend_name: "그룹 별칭", profile_name: "상대", direct_chat_id: "701" },
  ]);
  assert.deepEqual(enriched[0].peer_local_names, ["A 저동2 김가현"]);
  assert.deepEqual(enriched[1].peer_local_names, []);

  const wrongRoom = enrichDirectChatFriendAliases(chats, [
    { user_id: "20", friend_name: "잘못된 방", profile_name: "프로필명", direct_chat_id: "999" },
  ]);
  assert.deepEqual(wrongRoom[0].peer_local_names, []);

  const duplicates = enrichDirectChatFriendAliases(chats, [
    { user_id: "20", friend_name: "첫 이름", direct_chat_id: "700" },
    { user_id: "20", friend_name: "둘째 이름", direct_chat_id: "700" },
  ]);
  assert.deepEqual(duplicates[0].peer_local_names, []);
});

test("partial chat lists never receive friend aliases", () => {
  const chats = [{ chat_id: "700", type: "DirectChat", active_members: 2, peer_user_id: "20" }];
  const enriched = enrichDirectChatFriendAliases(
    chats,
    [{ user_id: "20", friend_name: "내 지정명", direct_chat_id: "700" }],
    { complete: false },
  );
  assert.deepEqual(enriched[0].peer_local_names, []);
});
