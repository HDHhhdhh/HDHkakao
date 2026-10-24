import assert from "node:assert/strict";
import test from "node:test";
import { createMetadataLookupAdapter, readDirectRoomMetadata, readDirectRoomMetadataForClient } from "../bridge/kakao_direct_metadata.mjs";
import { lookupAndVerifyChatsByIds } from "../bridge/kakao_direct_bridge.mjs";

const LongType = { fromString: (value) => value, isLong: () => false };
const ok = (body) => ({ statusCode: 0, body: { status: 0, ...body } });
const members = () => [{ userId: 10, nickName: "나" }, { userId: 20, nickName: "상대" }];
const room = () => ({ c: 101, t: "DirectChat", a: [10, 20], m: members() });
const missing = () => Object.assign(new Error("redacted"), {
  code: "get_chat_failed", serverStatus: -401, responseStatusSource: "body",
});
function sessionWith(transform = () => {}) {
  const calls = [];
  return {
    calls,
    async getChatInfo(id) {
      calls.push(["CHATONROOM", id]);
      const response = ok(room());
      transform(response, "room", calls.length);
      return response;
    },
    async getAllMembers(id) {
      calls.push(["GETMEM", id]);
      const response = ok({ members: members() });
      transform(response, "members", calls.length);
      return response;
    },
  };
}

test("verifies empty direct rooms with stable metadata only and strips private fields", async () => {
  const session = sessionWith((response) => { response.body.message = "PRIVATE"; });
  const result = await readDirectRoomMetadata(session, LongType, "101", "10");
  assert.equal(result.chat.type, "DirectChat");
  assert.equal(result.snapshot.complete, true);
  assert.deepEqual(result.snapshot.members.map((m) => m.user_id), ["10", "20"]);
  assert.deepEqual(session.calls, [["CHATONROOM", "101"], ["GETMEM", "101"], ["CHATONROOM", "101"], ["GETMEM", "101"]]);
  assert.equal(JSON.stringify(result).includes("PRIVATE"), false);
});

for (const [name, mutate] of [
  ["wrong room", (r, kind) => { if (kind === "room") r.body.c = 102; }],
  ["group room", (r, kind) => { if (kind === "room") r.body.t = "MultiChat"; }],
  ["absent owner", (r, kind) => { if (kind === "room") r.body.a = [30, 20]; }],
  ["duplicate active member", (r, kind) => { if (kind === "room") r.body.a = [10, 10]; }],
  ["left room", (r, kind) => { if (kind === "room") r.body.left = true; }],
  ["incomplete members", (r, kind) => { if (kind === "members") r.body.members.pop(); }],
  ["wrong GETMEM peer", (r, kind) => { if (kind === "members") r.body.members[1].userId = 30; }],
  ["changed peer", (r, kind, call) => { if (call > 2) {
    if (kind === "room") { r.body.a[1] = 30; r.body.m[1].userId = 30; }
    else r.body.members[1].userId = 30;
  } }],
  ["body rejection", (r) => { r.body.status = -401; }],
  ["missing status", (r) => { delete r.body.status; }],
  ["unsafe number ID", (r, kind) => { if (kind === "room") r.body.c = Number.MAX_SAFE_INTEGER + 1; }],
  ["mismatched response method", (r) => { r.method = "NOTIREAD"; }],
]) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(readDirectRoomMetadata(sessionWith(mutate), LongType, "101", "10"),
      { code: "direct_room_metadata_unverified" });
  });
}

test("fallback integrates with the existing peer verification boundary", async () => {
  const session = sessionWith();
  const adapter = createMetadataLookupAdapter({ LongType, ownUserId: "10", client: {
    async getChat() { throw missing(); },
    async acquireSession() { return session; },
    async getMemberSnapshot() { assert.fail("must reuse verified snapshot"); },
  } });
  const result = await lookupAndVerifyChatsByIds(["101"], { ...adapter, ownUserId: "10" });
  assert.equal(result.partial_failure, false);
  assert.equal(result.chats[0].peer_user_id, "20");
  assert.equal(session.calls.length, 4);
});

test("restarts the entire snapshot once after a confirmed disconnect", async () => {
  let connected = true;
  let acquisitions = 0;
  const old = sessionWith((response, kind) => {
    if (kind === "members") { connected = false; response.statusCode = -1; }
  });
  const replacement = sessionWith();
  const client = { isConnected: () => connected, async acquireSession() {
    acquisitions++;
    if (acquisitions === 1) return old;
    connected = true; return replacement;
  } };
  const result = await readDirectRoomMetadataForClient(() => client, LongType, "101", "10");
  assert.equal(acquisitions, 2);
  assert.equal(old.calls.length, 2);
  assert.equal(replacement.calls.length, 4);
  assert.equal(result.snapshot.complete, true);
});

test("never retries invalid identity even when the connection also died", async () => {
  let acquisitions = 0;
  const client = { isConnected: () => false, async acquireSession() {
    acquisitions++; return sessionWith((r, kind) => { if (kind === "room") r.body.t = "MultiChat"; });
  } };
  await assert.rejects(readDirectRoomMetadataForClient(() => client, LongType, "101", "10"), { code: "direct_room_metadata_unverified" });
  assert.equal(acquisitions, 1);
});

test("retry is bounded and requires a different session", async () => {
  for (const sameSession of [false, true]) {
    let acquisitions = 0;
    const disconnected = () => sessionWith(r => { r.statusCode = -1; });
    const old = disconnected();
    const client = { isConnected: () => false, async acquireSession() {
      acquisitions++; return acquisitions === 1 || sameSession ? old : disconnected();
    } };
    await assert.rejects(readDirectRoomMetadataForClient(() => client, LongType, "101", "10"), { code: "direct_room_metadata_disconnected" });
    assert.equal(acquisitions, 2);
  }
});

test("cancellation stops further metadata calls and publication", async () => {
  let active = true;
  const session = sessionWith(() => { active = false; });
  await assert.rejects(readDirectRoomMetadata(session, LongType, "101", "10", { isActive: () => active }), { code: "metadata_lookup_cancelled" });
  assert.equal(session.calls.length, 1);
});

test("member-only lookup supports the same exact missing CHATINFO response", async () => {
  const session = sessionWith();
  const adapter = createMetadataLookupAdapter({ LongType, ownUserId: "10", client: {
    async getMemberSnapshot() { throw Object.assign(missing(), { code: "get_member_snapshot_failed" }); },
    async acquireSession() { return session; },
  } });
  assert.equal((await adapter.getMemberSnapshot("101")).complete, true);
  assert.equal(session.calls.length, 4);
});

test("refresh uses the current client and a repeated lookup drops its earlier cached snapshot", async () => {
  const session = sessionWith();
  let current = { async getChat() { throw missing(); }, async acquireSession() { return session; } };
  const adapter = createMetadataLookupAdapter({ getClient: () => current, LongType, ownUserId: "10" });
  await adapter.getChat("101");
  const freshSnapshot = { chat_id: "101", complete: false };
  current = { async getChat() { return { chat_id: "101" }; }, async getMemberSnapshot() { return freshSnapshot; } };
  await adapter.getChat("101");
  assert.equal(await adapter.getMemberSnapshot("101"), freshSnapshot);
});

test("accepts omitted optional member IDs after the first read only with complete matching members", async () => {
  const session = sessionWith((response, kind, call) => {
    if (kind === "room" && call > 2) delete response.body.a;
  });
  const result = await readDirectRoomMetadata(session, LongType, "101", "10");
  assert.equal(result.snapshot.complete, true);
});

test("normal SDK lookup stays unchanged", async () => {
  const expected = { chat_id: "101" };
  const snapshot = { complete: true };
  const adapter = createMetadataLookupAdapter({ LongType, ownUserId: "10", client: {
    async getChat() { return expected; },
    async getMemberSnapshot() { return snapshot; },
    async acquireSession() { assert.fail("unexpected fallback"); },
  } });
  assert.equal(await adapter.getChat("101"), expected);
  assert.equal(await adapter.getMemberSnapshot("101"), snapshot);
});

test("does not fallback on authentication, transport, or other provider errors", async () => {
  for (const error of [new Error("timeout"), Object.assign(missing(), { code: "login_rejected" }),
    Object.assign(missing(), { serverStatus: -1 }), Object.assign(missing(), { responseStatusSource: "packet" })]) {
    const adapter = createMetadataLookupAdapter({ LongType, ownUserId: "10", client: {
      async getChat() { throw error; },
      async acquireSession() { assert.fail("unexpected fallback"); },
    } });
    await assert.rejects(adapter.getChat("101"), (received) => received === error);
  }
});
