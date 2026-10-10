import assert from "node:assert/strict";
import test from "node:test";

import {
  createLocoMethodCapture,
  findProbeMessageMatches,
  installLocoMethodCapture,
  lookupAndVerifyChatsByIds,
  normalizeChatLookupIds,
  normalizeProbeMarker,
  sanitizeLocoMethod,
  startLocoMethodCapture,
  stopLocoMethodCapture,
} from "../bridge/kakao_direct_bridge.mjs";

test("chat lookup IDs are bounded, numeric, and deduplicated without reordering", () => {
  assert.deepEqual(normalizeChatLookupIds([]), []);
  assert.deepEqual(normalizeChatLookupIds([" 101 ", 101, "202", "101"]), ["101", "202"]);

  for (const invalid of [null, {}, "101", [""], ["0"], ["-1"], ["01"], [1.5], [true]]) {
    assert.throws(() => normalizeChatLookupIds(invalid), { code: "invalid_chat_ids" });
  }

  const maximumBatch = Array.from({ length: 128 }, (_value, index) => String(index + 1));
  assert.deepEqual(normalizeChatLookupIds(maximumBatch), maximumBatch);
  assert.throws(
    () => normalizeChatLookupIds([...maximumBatch, "129"]),
    { code: "invalid_chat_ids" },
  );
});

test("chat lookup returns a sanitized room only after stable peer snapshot verification", async () => {
  const getChatCalls = [];
  const memberCalls = [];
  const result = await lookupAndVerifyChatsByIds(["101"], {
    ownUserId: "10",
    async getChat(chatId) {
      getChatCalls.push(chatId);
      return {
        chat_id: chatId,
        type: "DirectChat",
        active_members: 2,
        title: "상대",
        display_name: "상대",
        last_message: { message: "PRIVATE MESSAGE" },
        oauth_token: "SECRET TOKEN",
      };
    },
    async getMemberSnapshot(chatId) {
      memberCalls.push(chatId);
      return {
        chat_id: chatId,
        active_members: 2,
        complete: true,
        members: [
          { user_id: "10", nickname: "나", status_message: "PRIVATE SELF" },
          { user_id: "20", nickname: "상대", profile_image_url: "PRIVATE IMAGE" },
        ],
      };
    },
  });

  assert.deepEqual(getChatCalls, ["101"]);
  assert.deepEqual(memberCalls, ["101"]);
  assert.equal(result.partial_failure, false);
  assert.deepEqual(result.chats, [
    {
      chat_id: "101",
      type: "DirectChat",
      display_name: "상대",
      title: "상대",
      active_members: 2,
      peer_user_id: "20",
      peer_names: ["상대"],
      peer_local_names: [],
    },
  ]);
  assert.equal(JSON.stringify(result).includes("PRIVATE"), false);
  assert.equal(JSON.stringify(result).includes("SECRET"), false);
});

test("chat lookup omits rejected and mismatched getChat rows and marks the batch partial", async () => {
  const memberCalls = [];
  const result = await lookupAndVerifyChatsByIds(["201", "202"], {
    ownUserId: "10",
    async getChat(chatId) {
      if (chatId === "201") throw new Error("PRIVATE LOOKUP FAILURE");
      return { chat_id: "different-id", type: "DirectChat", active_members: 2, title: "잘못된 방" };
    },
    async getMemberSnapshot(chatId) {
      memberCalls.push(chatId);
      return {};
    },
  });

  assert.deepEqual(result, { chats: [], partial_failure: true });
  assert.deepEqual(memberCalls, []);
});

test("chat lookup keeps a room but clears peer identity when member verification fails", async () => {
  const result = await lookupAndVerifyChatsByIds(["301"], {
    ownUserId: "10",
    async getChat(chatId) {
      return {
        chat_id: chatId,
        type: "DirectChat",
        active_members: 2,
        title: "검증 대기 방",
        display_name: "검증 대기 방",
        peer_user_id: "UNTRUSTED",
        peer_names: ["UNTRUSTED NAME"],
      };
    },
    async getMemberSnapshot() {
      throw new Error("PRIVATE MEMBER FAILURE");
    },
  });

  assert.equal(result.partial_failure, true);
  assert.equal(result.chats.length, 1);
  assert.equal(result.chats[0].chat_id, "301");
  assert.equal(result.chats[0].peer_user_id, null);
  assert.deepEqual(result.chats[0].peer_names, []);
  assert.deepEqual(result.chats[0].peer_local_names, []);
  assert.equal(JSON.stringify(result).includes("UNTRUSTED"), false);
  assert.equal(JSON.stringify(result).includes("PRIVATE"), false);
});

test("method sanitizer accepts only bounded LOCO method tokens", () => {
  assert.equal(sanitizeLocoMethod("WRITE"), "WRITE");
  assert.equal(sanitizeLocoMethod("PUSH_1"), "PUSH_1");
  assert.equal(sanitizeLocoMethod("A".repeat(11)), "A".repeat(11));

  for (const value of ["write", "PUSH-MESSAGE", "", "A".repeat(12), null, 7, {}, undefined]) {
    assert.equal(sanitizeLocoMethod(value), "<invalid-method>");
  }
});

test("probe marker is optional but bounded and never serialized", () => {
  assert.equal(normalizeProbeMarker(undefined), null);
  assert.equal(normalizeProbeMarker(null), null);
  assert.equal(normalizeProbeMarker("exact-probe"), "exact-probe");
  assert.throws(() => normalizeProbeMarker(""), { code: "invalid_probe_marker" });
  assert.throws(() => normalizeProbeMarker("x".repeat(4097)), { code: "invalid_probe_marker" });
});

test("capture start requires a connected client and stop returns redacted counts with monotonic offsets", () => {
  const times = [1000, 1002.25, 1004, 1007.75, 1008.5];
  const capture = createLocoMethodCapture({ now: () => times.shift() });
  const disconnected = { isConnected: () => false };

  assert.throws(() => startLocoMethodCapture(capture, disconnected), { code: "capture_not_connected" });
  assert.equal(capture.isActive(), false);

  const connected = { isConnected: () => true };
  assert.deepEqual(startLocoMethodCapture(capture, connected), { capturing: true });
  assert.equal(capture.isActive(), true);

  capture.record("WRITE");
  capture.record("PUSH");
  capture.record("WRITE");
  capture.record("not-a-method");

  const stopped = stopLocoMethodCapture(capture);
  assert.equal(stopped.capturing, false);
  assert.equal(capture.isActive(), false);
  assert.deepEqual(stopped.entries, [
    { method: "<invalid-method>", count: 1, first_offset_ms: 8.5, last_offset_ms: 8.5, field_paths: [] },
    { method: "PUSH", count: 1, first_offset_ms: 4, last_offset_ms: 4, field_paths: [] },
    { method: "WRITE", count: 2, first_offset_ms: 2.25, last_offset_ms: 7.75, field_paths: [] },
  ]);

  // A stopped capture cannot retain events from the previous run.
  capture.record("WRITE");
  assert.deepEqual(stopLocoMethodCapture(capture), { capturing: false, entries: [] });
});

test("a new capture run clears prior entries and non-monotonic clocks never move offsets backwards", () => {
  const times = [500, 502, 501, 503, 506];
  const capture = createLocoMethodCapture({ now: () => times.shift() });

  capture.start();
  capture.record("PUSH");
  capture.record("PUSH");
  assert.deepEqual(stopLocoMethodCapture(capture).entries, [
    { method: "PUSH", count: 2, first_offset_ms: 2, last_offset_ms: 2, field_paths: [] },
  ]);

  capture.start();
  capture.record("PUSH");
  assert.deepEqual(stopLocoMethodCapture(capture).entries, [
    { method: "PUSH", count: 1, first_offset_ms: 3, last_offset_ms: 3, field_paths: [] },
  ]);
});

test("dispatch wrapper observes only the method, preserves receiver/arguments/return value, and installs once", () => {
  const capture = createLocoMethodCapture({ now: () => 900 });
  const receiver = { name: "receiver" };
  const calls = [];
  const sentinel = { ok: true };
  const original = function (...args) {
    calls.push({ receiver: this, args });
    return sentinel;
  };
  const instance = { dispatchPush: original };

  assert.equal(installLocoMethodCapture(instance, capture), true);
  assert.equal(installLocoMethodCapture(instance, capture), false);

  capture.start();
  const result = instance.dispatchPush.call(receiver, { session: "opaque" }, { method: "PUSH", body: "opaque" });
  assert.strictEqual(result, sentinel);
  assert.equal(calls.length, 1);
  assert.strictEqual(calls[0].receiver, receiver);
  assert.deepEqual(calls[0].args, [
    { session: "opaque" },
    { method: "PUSH", body: "opaque" },
  ]);
  assert.deepEqual(stopLocoMethodCapture(capture).entries, [
    { method: "PUSH", count: 1, first_offset_ms: 0, last_offset_ms: 0, field_paths: [
      { path: "body", types: [{ type: "string", count: 1 }] },
    ] },
  ]);

  // Non-object packets are safe and are intentionally collapsed to the redacted marker.
  capture.start();
  instance.dispatchPush(null, null);
  assert.deepEqual(stopLocoMethodCapture(capture).entries, [
    { method: "<invalid-method>", count: 1, first_offset_ms: 0, last_offset_ms: 0, field_paths: [] },
  ]);
});

test("dispatch capture records bounded field paths and types but never packet values", () => {
  const capture = createLocoMethodCapture({ now: () => 100 });
  const instance = {
    dispatchPush() {
      return "original-result";
    },
  };
  assert.equal(installLocoMethodCapture(instance, capture), true);
  capture.start();

  const privateChatId = "private-chat-id-123456";
  const privateMessage = "PRIVATE MESSAGE MUST NOT ESCAPE";
  const privateUserId = "private-user-id-456789";
  instance.dispatchPush({}, {
    method: "PUSH",
    body: {
      chat_id: privateChatId,
      message: privateMessage,
      nested: {
        user_id: privateUserId,
        deeper: { count: 3, too_deep: { hidden: "PRIVATE HIDDEN" } },
      },
      list: [{ id: 9, text: "PRIVATE ARRAY VALUE" }],
      "bad\u0001key": "PRIVATE CONTROL KEY VALUE",
      ["k".repeat(65)]: "PRIVATE LONG KEY VALUE",
    },
    data: {
      status: true,
      optional: null,
    },
  });
  instance.dispatchPush({}, {
    method: "PUSH",
    data: { chat_id: "another-private-chat-id", status: false },
  });

  const entry = stopLocoMethodCapture(capture).entries[0];
  assert.equal(entry.method, "PUSH");
  assert.equal(entry.count, 2);
  assert.equal(entry.field_paths.length <= 64, true);
  assert.deepEqual(
    entry.field_paths.find(({ path }) => path === "body.chat_id"),
    { path: "body.chat_id", types: [{ type: "string", count: 1 }] },
  );
  assert.deepEqual(
    entry.field_paths.find(({ path }) => path === "body.nested.deeper.count"),
    { path: "body.nested.deeper.count", types: [{ type: "number", count: 1 }] },
  );
  assert.deepEqual(
    entry.field_paths.find(({ path }) => path === "body.list[0].id"),
    { path: "body.list[0].id", types: [{ type: "number", count: 1 }] },
  );
  assert.deepEqual(
    entry.field_paths.find(({ path }) => path === "data.status"),
    { path: "data.status", types: [
      { type: "boolean", count: 2 },
    ] },
  );
  assert.equal(entry.field_paths.some(({ path }) => path.includes("hidden")), false);
  assert.equal(entry.field_paths.some(({ path }) => path.includes("bad")), false);
  assert.equal(entry.field_paths.some(({ path }) => path.length > 256), false);

  const serialized = JSON.stringify(entry);
  for (const privateValue of [privateChatId, privateMessage, privateUserId, "PRIVATE HIDDEN", "PRIVATE ARRAY VALUE"]) {
    assert.equal(serialized.includes(privateValue), false, `private value leaked: ${privateValue}`);
  }
});

test("field-path collection remains bounded for large payloads", () => {
  const capture = createLocoMethodCapture({ now: () => 10 });
  capture.start();
  const body = Object.fromEntries(Array.from({ length: 100 }, (_, index) => [`key_${index}`, index]));
  capture.record("PUSH", { body });
  capture.record("PUSH", { body });
  const entry = stopLocoMethodCapture(capture).entries[0];

  assert.equal(entry.count, 2);
  assert.equal(entry.field_paths.length, 64);
  assert.equal(entry.field_paths.every(({ path, types }) => path.startsWith("body.key_") && types.length === 1), true);
  assert.deepEqual(entry.field_paths.find(({ path }) => path === "body.key_0"), {
    path: "body.key_0",
    types: [{ type: "number", count: 2 }],
  });
  assert.equal(JSON.stringify(entry).includes('"0"'), false);
  assert.equal(JSON.stringify(entry).includes('"99"'), false);
});

test("probe message matching is exact and returns no personal values", () => {
  const marker = "PROBE-20260830-EXACT";
  const privateMessage = "PRIVATE MESSAGE MUST NOT ESCAPE";
  const privateChatId = "private-chat-id-123456";
  const privateUserId = "private-user-id-456789";
  const privateLogId = "private-log-id-987654";
  const privateToken = "PRIVATE TOKEN MUST NOT ESCAPE";
  const messages = [
    {
      message: `${marker}-suffix`,
      type: 1,
      sent_at: 111,
      chat_id: privateChatId,
      author_id: privateUserId,
      log_id: privateLogId,
      attachment: { secret: privateMessage, token: privateToken },
    },
    {
      message: privateMessage,
      type: 2,
      sent_at: 222,
      chat_id: "unmatched-chat-id",
      attachment: { private: privateMessage },
    },
    {
      message: marker,
      type: 1,
      sent_at: 333,
      chat_id: privateChatId,
      author_id: privateUserId,
      log_id: privateLogId,
      attachment: {
        k: "opaque-key-value",
        token: privateToken,
        name: "private-name",
        nested: { width: 640, message: privateMessage },
      },
      supplement: JSON.stringify({
        version: 2,
        message: privateMessage,
        nested: { enabled: true },
      }),
    },
  ];

  const matches = findProbeMessageMatches(messages, marker);
  assert.equal(matches.length, 1);
  assert.deepEqual(matches[0].marker_matched, true);
  assert.equal(matches[0].type, 1);
  assert.equal(matches[0].sent_at, 333);
  assert.equal(matches[0].attachment.some(({ path }) => path === "attachment.k"), true);
  assert.equal(matches[0].attachment.some(({ path }) => path.includes("token")), false);
  assert.equal(matches[0].attachment.some(({ path }) => path.includes("name")), false);
  assert.equal(matches[0].supplement.some(({ path }) => path === "supplement.nested.enabled"), true);

  const serialized = JSON.stringify(matches);
  for (const privateValue of [
    marker,
    privateMessage,
    privateChatId,
    privateUserId,
    privateLogId,
    privateToken,
    "private-name",
  ]) {
    assert.equal(serialized.includes(privateValue), false, `probe value leaked: ${privateValue}`);
  }
  for (const forbiddenKey of ["message", "chat_id", "author_id", "log_id", "token"]) {
    assert.equal(serialized.includes(`"${forbiddenKey}"`), false, `forbidden key leaked: ${forbiddenKey}`);
  }
});

test("capture records only exact inbound chatLog marker matches and caps details at eight offsets", () => {
  const marker = "CAPTURE-PROBE-EXACT";
  const privateMessage = "PRIVATE CAPTURE MESSAGE MUST NOT ESCAPE";
  const privateChatId = "capture-private-chat-id";
  const privateUserId = "capture-private-user-id";
  const privateLogId = "capture-private-log-id";
  const privateToken = "capture-private-token";
  const capture = createLocoMethodCapture({ now: () => 1000 });
  const connected = { isConnected: () => true };

  assert.deepEqual(startLocoMethodCapture(capture, connected, marker), {
    capturing: true,
    probe_marker_enabled: true,
  });
  for (let index = 0; index < 10; index += 1) {
    capture.record("PUSH", {
      method: "PUSH",
      body: {
        chatLog: {
          message: marker,
          type: 1,
          scope: "private",
          sendAt: 1700000000 + index,
          chatId: privateChatId,
          userId: privateUserId,
          logId: privateLogId,
          attachment: { key: "opaque", token: privateToken },
          supplement: { enabled: true, message: privateMessage },
        },
      },
    });
  }
  capture.record("PUSH", {
    method: "PUSH",
    body: { chatLog: { message: privateMessage, type: 2, chatId: privateChatId } },
  });

  const result = stopLocoMethodCapture(capture);
  assert.equal(result.matched_events.length, 8);
  assert.equal(result.matched_events.every(({ marker_matched }) => marker_matched === true), true);
  assert.equal(result.matched_events.every(({ method }) => method === "PUSH"), true);
  assert.equal(result.matched_events.every(({ offset_ms }) => offset_ms === 0), true);
  assert.equal(result.matched_events[0].chat_log_scalars.some(({ path }) => path === "chatLog.scope"), true);
  assert.equal(result.matched_events[0].chat_log_scalars.some(({ path }) => path === "chatLog.sendAt"), true);
  assert.equal(result.matched_events[0].attachment.some(({ path }) => path === "chatLog.attachment.key"), true);
  assert.equal(result.matched_events[0].supplement.some(({ path }) => path === "chatLog.supplement.enabled"), true);

  const serialized = JSON.stringify(result);
  for (const privateValue of [
    marker,
    privateMessage,
    privateChatId,
    privateUserId,
    privateLogId,
    privateToken,
  ]) {
    assert.equal(serialized.includes(privateValue), false, `capture value leaked: ${privateValue}`);
  }
  const matchedSerialized = JSON.stringify(result.matched_events);
  for (const forbiddenKey of ["message", "chatId", "userId", "logId", "token"]) {
    assert.equal(matchedSerialized.includes(forbiddenKey), false, `capture key leaked: ${forbiddenKey}`);
  }
});
