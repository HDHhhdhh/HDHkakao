import assert from "node:assert/strict";
import test from "node:test";
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { historyWatermark } from "../bridge/kakao_direct_bridge.mjs";
import { reconcileDirectTextHistory, reconcileLatestHistorySnapshot } from "../bridge/kakao_direct_reconcile.mjs";

const incident = { chatId: "777", ownUserId: "888", text: "한글 원본\nPRIVATE exact text",
  sentAfter: 1000, sentBefore: 1010, sinceLogId: "0", throughLogId: "99", observedAt: 1020 };
const message = (changes = {}) => ({ log_id: "99", type: 1, author_id: 888,
  message: incident.text, sent_at: 1005, ...changes });
const page = (messages, complete = true) => ({ messages, next_cursor: messages.at(-1)?.log_id ?? null, complete });
function fixture(pages, overrides = {}) {
  const calls = [];
  return { calls, options: { ...incident, ...overrides, getMessagePage: async (...args) => {
    calls.push(args);
    if (!pages.length) throw new Error("unexpected history read PRIVATE");
    const value = pages.shift();
    if (value instanceof Error) throw value;
    return value;
  } } };
}

test("exact own text and incident time is present without exposing message or author", async () => {
  const f = fixture([page([message()])]);
  const result = await reconcileDirectTextHistory(f.options);
  assert.equal(result.outcome, "present");
  assert.deepEqual(result.matchingLogIds, ["99"]);
  assert.equal(result.coverageComplete, false);
  assert.deepEqual(f.calls, [["777", { count: 100 }]]);
  assert.ok(!JSON.stringify(result).includes("PRIVATE"));
  assert.ok(!Object.hasOwn(result, "message"));
  assert.ok(!Object.hasOwn(result, "author_id"));
});

test("a valid positive control requires its exact known ACK log ID", async () => {
  const f = fixture([
    page([message({ log_id: "90" })], false),
    page([message()], true),
  ], { expectedLogId: "99" });
  const result = await reconcileDirectTextHistory(f.options);
  assert.equal(result.outcome, "present");
  assert.deepEqual(result.matchingLogIds, ["99"]);
  assert.equal(result.pages, 2);
  assert.deepEqual(f.calls[1], ["777", { count: 100, from: "90" }]);
});

test("a positive match stops read pagination even before complete coverage", async () => {
  const f = fixture([page([message({ log_id: "90" })], false)]);
  const result = await reconcileDirectTextHistory(f.options);
  assert.equal(result.outcome, "present");
  assert.equal(result.coverageComplete, false);
  assert.equal(f.calls.length, 1);
});

test("absence requires forward pages from zero through the fresh watermark and completion", async () => {
  const f = fixture([
    page([message({ log_id: "90", sent_at: 990, message: "anchor" })], false),
    page([message({ message: "different" })], true),
  ]);
  const result = await reconcileDirectTextHistory(f.options);
  assert.equal(result.outcome, "absent");
  assert.equal(result.coverageComplete, true);
  assert.equal(result.lastCursor, "99");
  assert.equal(result.scannedCount, 2);
});

test("other author, type, original text or incident time cannot match", async () => {
  for (const changes of [{ author_id: "999" }, { type: 2 }, { message: incident.text + " " },
    { message: incident.text.replace("\n", " ") }, { sent_at: 900 }]) {
    const f = fixture([page([message({ log_id: "90", sent_at: 990, message: "anchor" }), message(changes)])]);
    assert.equal((await reconcileDirectTextHistory(f.options)).outcome, "absent");
  }
  for (const sent_at of [1000, 1010]) {
    const f = fixture([page([message({ sent_at })])]);
    assert.equal((await reconcileDirectTextHistory(f.options)).outcome, "present");
  }
});

test("empty, prematurely complete, truncated and stalled history stay unknown", async () => {
  for (const pages of [
    [page([])], [page([], false)], [page([message({ log_id: "90", message: "other" })])],
    [page([message({ log_id: "99", message: "other" })], false), page([], false)],
  ]) {
    const f = fixture(pages);
    const result = await reconcileDirectTextHistory(f.options);
    assert.equal(result.outcome, "unknown");
    assert.equal(result.coverageComplete, false);
  }
  const f = fixture([page([message({ message: "other" })], false)], { maxPages: 1 });
  assert.equal((await reconcileDirectTextHistory(f.options)).reason, "history_page_limit");
});

test("empty page cannot establish the incident lower bound even with a fresh empty-room watermark", async () => {
  const f = fixture([page([])], { throughLogId: "0" });
  const result = await reconcileDirectTextHistory(f.options);
  assert.equal(result.outcome, "unknown");
  assert.equal(result.reason, "history_missing_incident_lower_bound");
  assert.equal(result.coverageComplete, false);
  const stale = fixture([page([])], { throughLogId: "0", observedAt: 999 });
  assert.equal((await reconcileDirectTextHistory(stale.options)).outcome, "unknown");
  assert.equal(stale.calls.length, 0);
});

test("invalid or unproven reconciliation bounds issue no read", async () => {
  for (const change of [{ sinceLogId: "90" }, { throughLogId: undefined }, { throughLogId: "-1" },
    { throughLogId: 99 }, { observedAt: undefined }, { observedAt: 1009 },
    { sentAfter: 1011 }, { sentBefore: Infinity }, { ownUserId: 0 }, { chatId: "0777" },
    { text: "" }, { expectedLogId: "0" }, { expectedLogId: "100" }, { maxPages: 0 }, { maxPages: 101 },
    { pageSize: 101 }, { pageSize: 0 }, { maxDurationMs: 0 }]) {
    const f = fixture([], change);
    assert.equal((await reconcileDirectTextHistory(f.options)).outcome, "unknown");
    assert.equal(f.calls.length, 0);
  }
});

test("malformed page metadata or incomplete message data cannot establish absence", async () => {
  for (const badPage of [null, [], {}, { ...page([]), complete: undefined },
    { ...page([message()]), next_cursor: "100" },
    { ...page([]), next_cursor: "99" },
    page([message({ log_id: "0" })]), page([message({ log_id: 99 })]),
    page([message({ log_id: "99", sent_at: NaN })]),
    page([message({ author_id: undefined })]), page([message({ message: undefined })]),
    page([message({ chat_id: "778" })]), page([message({ chat_id: undefined })]),
    page([message(), message()]), page([message({ log_id: "100" }), message()]),
  ]) {
    const f = fixture([badPage]);
    assert.equal((await reconcileDirectTextHistory(f.options)).outcome, "unknown");
  }
  const tooMany = fixture([page([message({ log_id: "90" }), message()])], { pageSize: 1 });
  assert.equal((await reconcileDirectTextHistory(tooMany.options)).outcome, "unknown");
});

test("a repeated earlier page cannot masquerade as forward coverage", async () => {
  const first = page([message({ log_id: "90", message: "other" })], false);
  const f = fixture([first, first]);
  assert.equal((await reconcileDirectTextHistory(f.options)).outcome, "unknown");
  assert.equal(f.calls.length, 2);
});

test("history read errors and deadlines remain unknown without another call", async () => {
  const f = fixture([new Error("PRIVATE server error")]);
  const result = await reconcileDirectTextHistory(f.options);
  assert.equal(result.outcome, "unknown");
  assert.ok(!JSON.stringify(result).includes("PRIVATE"));
  assert.equal(f.calls.length, 1);
  let reads = 0;
  const timeout = await reconcileDirectTextHistory({ ...incident, maxDurationMs: 5,
    getMessagePage: () => { reads++; return new Promise(() => {}); } });
  assert.equal(timeout.outcome, "unknown");
  assert.equal(reads, 1);
});

test("retained history after the incident is not proof of absence", async () => {
  const f = fixture([page([message({ sent_at: 1070, message: "later message" })])]);
  const result = await reconcileDirectTextHistory(f.options);
  assert.equal(result.outcome, "unknown");
  assert.equal(result.reason, "history_missing_incident_lower_bound");
});

test("lower bound evidence persists across multiple forward pages", async () => {
  const f = fixture([
    page([message({ log_id: "80", sent_at: 1000, message: "anchor" })], false),
    page([message({ log_id: "90", sent_at: 1005, message: "other" })], false),
    page([message({ log_id: "99", sent_at: 1070, message: "later" })]),
  ]);
  const result = await reconcileDirectTextHistory(f.options);
  assert.equal(result.outcome, "absent");
  assert.equal(result.coverageComplete, true);
  assert.equal(result.pages, 3);
});

test("exact own text after the PC clock boundary prevents an absent result", async () => {
  const f = fixture([page([
    message({ log_id: "90", sent_at: 990, message: "anchor" }),
    message({ sent_at: 1011 }),
  ])]);
  const result = await reconcileDirectTextHistory(f.options);
  assert.equal(result.outcome, "unknown");
  assert.equal(result.reason, "clock_boundary_message");
  assert.equal(result.coverageComplete, false);
});

test("exact own text shortly before the PC clock boundary also prevents absence", async () => {
  for (const sent_at of [incident.sentAfter - 1, incident.sentAfter - 59]) {
    const f = fixture([page([message({ sent_at })])]);
    const result = await reconcileDirectTextHistory(f.options);
    assert.equal(result.outcome, "unknown");
    assert.equal(result.reason, "clock_boundary_message");
    assert.equal(result.coverageComplete, false);
  }
  const oldTemplate = fixture([page([message({ sent_at: incident.sentAfter - 61 })])]);
  assert.equal((await reconcileDirectTextHistory(oldTemplate.options)).outcome, "absent");
});

test("implausible future or millisecond timestamps invalidate even unrelated history", async () => {
  for (const sent_at of [incident.observedAt + 61, 1005000]) {
    const f = fixture([page([
      message({ log_id: "90", sent_at: 990, message: "anchor" }),
      message({ sent_at, message: "unrelated" }),
    ])]);
    const result = await reconcileDirectTextHistory(f.options);
    assert.equal(result.outcome, "unknown");
    assert.equal(result.reason, "invalid_history_message");
  }
});

const watermarkResponse = (lastLogId = "99", bodyChanges = {}, packetChanges = {}) => ({
  method: "CHATINFO", statusCode: 0,
  body: { chatInfo: { chatId: "777", lastLogId }, ...bodyChanges }, ...packetChanges,
});
const expectBadWatermark = (response, LongType) => assert.throws(
  () => historyWatermark(response, "777", LongType), { code: "invalid_history_watermark" });

test("watermark requires a successful CHATINFO packet and optional zero body status", () => {
  assert.equal(historyWatermark(watermarkResponse(), "777"), "99");
  assert.equal(historyWatermark(watermarkResponse("99", { status: 0 }), "777"), "99");
  for (const statusCode of [undefined, null, false, "0", -1, 9, NaN, Infinity]) {
    expectBadWatermark(watermarkResponse("99", {}, { statusCode }));
  }
  for (const status of [undefined, null, false, "0", -1, 9, NaN, Infinity]) {
    expectBadWatermark(watermarkResponse("99", { status }));
  }
  for (const method of [undefined, "", "CHATONROOM", "SYNCMSG"]) {
    expectBadWatermark(watermarkResponse("99", {}, { method }));
  }
});

test("watermark rejects malformed bodies or a different channel", () => {
  for (const body of [undefined, null, [], {}, { chatInfo: null }, { chatInfo: [] },
    { chatInfo: { lastLogId: "99" } }, { chatInfo: { chatId: "778", lastLogId: "99" } },
    { chatInfo: { chatId: 777.5, lastLogId: "99" } }, { chatInfo: { chatId: "0777", lastLogId: "99" } }]) {
    expectBadWatermark(watermarkResponse("99", {}, { body }));
  }
});

test("watermark IDs are lossless signed 64-bit values including a real zero", () => {
  for (const value of [0, "0", 0n, 99, "99", 99n, "9223372036854775807"]) {
    assert.equal(historyWatermark(watermarkResponse(value), "777"), String(value));
  }
  for (const value of [undefined, null, false, true, "", "01", " 99", "１", "1e3", -1, 1.5,
    NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, "9223372036854775808", {}, { toString: () => "99" }]) {
    expectBadWatermark(watermarkResponse("99", { chatInfo: { chatId: "777", lastLogId: value } }));
  }
});

test("watermark accepts installed SDK BSON number and Long decoding without a session", (t) => {
  const sdkRoot = process.env.KAKAO_AGENT_MESSENGER_ROOT ||
    "C:/Users/gkseh/Documents/Codex/2026-08-28/li/work/Spoiler/bridge/node_modules/agent-messenger";
  const manifest = join(sdkRoot, "package.json");
  if (!existsSync(manifest)) { t.skip("installed SDK is unavailable"); return; }
  const { BSON, Long } = createRequire(manifest)("bson");
  for (const value of ["0", "99", "9223372036854775807"]) {
    const body = BSON.deserialize(BSON.serialize({ chatInfo: {
      chatId: Long.fromString("777"), lastLogId: Long.fromString(value),
    } }), { promoteValues: true });
    assert.equal(historyWatermark(watermarkResponse(undefined, {}, { body }), "777", Long), value);
  }
  expectBadWatermark(watermarkResponse(Long.fromString("-1")), Long);
  expectBadWatermark(watermarkResponse(Long.fromString("9223372036854775808", true)), Long);
});

const latestSnapshot = () => ({ chatId: "777", throughLogId: "99", sentAfter: 1000, observedAt: 1020,
  chatInfo: { chatId: "777", lastLogId: "99", lastChatLog: { logId: "99", sendAt: 939 } } });

test("fresh latest-message timestamp proves absence only when tied to the same positive watermark", () => {
  const input = latestSnapshot();
  input.chatInfo.lastChatLog.message = "PRIVATE payload must not cross the boundary";
  input.chatInfo.lastChatLog.chatId = 777;
  const result = reconcileLatestHistorySnapshot(input);
  assert.deepEqual(result, { outcome: "absent", reason: "latest_server_message_precedes_incident", coverageComplete: true });
  assert.ok(!JSON.stringify(result).includes("PRIVATE"));
});

test("missing, inconsistent, zero or unsafe latest-message IDs remain unknown", () => {
  const mutations = [
    (x) => { x.chatInfo = null; },
    (x) => { x.chatInfo.chatId = "778"; },
    (x) => { x.chatInfo.lastLogId = "98"; },
    (x) => { delete x.chatInfo.lastLogId; },
    (x) => { x.chatInfo.lastChatLog = {}; },
    (x) => { x.chatInfo.lastChatLog = []; },
    (x) => { x.chatInfo.lastChatLog.logId = "98"; },
    (x) => { x.chatInfo.lastChatLog.logId = Number.MAX_SAFE_INTEGER + 1; },
    (x) => { x.chatInfo.lastChatLog.chatId = "778"; },
    (x) => { x.chatInfo.lastChatLog.chatId = undefined; },
    (x) => { x.chatInfo.lastChatLog.chat_id = "778"; },
    (x) => { x.throughLogId = x.chatInfo.lastLogId = x.chatInfo.lastChatLog.logId = "0"; },
  ];
  for (const mutate of mutations) {
    const input = latestSnapshot();
    mutate(input);
    assert.equal(reconcileLatestHistorySnapshot(input).outcome, "unknown");
  }
});

test("latest-message proof keeps a strict 60-second lower guard and sane seconds", () => {
  for (const sendAt of [undefined, null, 0, -1, "939", false, 939.5, NaN, Infinity,
    940, 999, 1000, 1020, 1021, 939000]) {
    const input = latestSnapshot();
    input.chatInfo.lastChatLog.sendAt = sendAt;
    assert.equal(reconcileLatestHistorySnapshot(input).outcome, "unknown");
  }
  for (const changes of [{ observedAt: 999 }, { observedAt: Infinity }, { sentAfter: 1021 },
    { sentAfter: "1000" }, { throughLogId: undefined }]) {
    assert.equal(reconcileLatestHistorySnapshot({ ...latestSnapshot(), ...changes }).outcome, "unknown");
  }
});
