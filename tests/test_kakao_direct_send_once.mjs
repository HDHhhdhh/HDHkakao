import assert from "node:assert/strict";
import test from "node:test";

import { sendWriteOnce } from "../bridge/kakao_direct_send_once.mjs";

const FakeLong = {
  fromString(value) {
    return { value };
  },
  isLong(value) {
    return value instanceof FakeLongValue;
  },
};

class FakeLongValue {
  constructor(value) { this.value = value; }
  toString() { return this.value; }
}

function receipt(body = { logId: 99, sendAt: 1234 }, packet = {}) {
  return { method: "WRITE", statusCode: 0, body, ...packet };
}

async function expectUnknown(response) {
  let calls = 0;
  await assert.rejects(
    sendWriteOnce({ async sendMessage() { calls += 1; return response; } }, FakeLong, "777", "PRIVATE MESSAGE"),
    (error) => error?.code === "send_outcome_unknown" &&
      error?.responseFailureKind === "transient_or_unknown" && !error.message.includes("PRIVATE"),
  );
  assert.equal(calls, 1, "an uncertain ACK must never replay WRITE");
}

test("one WRITE returns a sanitized success result", async () => {
  let calls = 0;
  const session = {
    async sendMessage(chatId, text) {
      calls += 1;
      assert.deepEqual(chatId, { value: "777" });
      assert.equal(text, "hello");
      return receipt();
    },
  };

  const result = await sendWriteOnce(session, FakeLong, "777", "hello");

  assert.equal(calls, 1);
  assert.deepEqual(result, {
    success: true,
    status_code: 0,
    chat_id: "777",
    log_id: "99",
    sent_at: 1234,
  });
});

test("a failed WRITE is attempted once and becomes unknown", async () => {
  let calls = 0;
  const session = {
    async sendMessage() {
      calls += 1;
      throw new Error("transport closed");
    },
  };

  await assert.rejects(
    sendWriteOnce(session, FakeLong, "777", "hello"),
    (error) =>
      error?.code === "send_outcome_unknown" &&
      error?.responseFailureKind === "transient_or_unknown",
  );
  assert.equal(calls, 1);
});

test("zero packet status with an empty receipt is unknown", async () => {
  for (const body of [{}, { logId: "" }, { sendAt: 1234 }, null, [], undefined]) {
    await expectUnknown({ method: "WRITE", statusCode: 0, body });
  }
});

test("explicit packet and body rejections remain single-attempt provider failures", async () => {
  for (const [response, status] of [
    [receipt({}, { statusCode: -500 }), -500],
    [receipt({ status: -500 }), -500],
    [receipt({ status: 9, logId: "99", sendAt: 1234 }), 9],
  ]) {
    let calls = 0;
    await assert.rejects(
      sendWriteOnce({ async sendMessage() { calls += 1; return response; } }, FakeLong, "777", "PRIVATE MESSAGE"),
      (error) => error?.code === "server_rejected" && error?.serverStatus === status &&
        error?.responseFailureKind === "provider_rejection" && !error.message.includes("PRIVATE"),
    );
    assert.equal(calls, 1);
  }
});

test("malformed packet or body status and synthetic close are unknown", async () => {
  for (const status of [undefined, null, "0", false, NaN, Infinity, 0.5, -1]) {
    await expectUnknown(receipt(undefined, { statusCode: status }));
    await expectUnknown(receipt({ logId: "99", sendAt: 1234, status }));
  }
  await expectUnknown({ packetId: 0, statusCode: -1, method: "", body: { error: "connection closed" } });
  await expectUnknown(receipt({ logId: "99", sendAt: 1234, error: "PRIVATE ERROR" }));
});

test("ACK method and any echoed chat ID must match the WRITE", async () => {
  for (const method of [undefined, "", "PING", "WRITE "]) {
    await expectUnknown(receipt(undefined, { method }));
  }
  for (const chatId of [undefined, null, "778", 0, "0", "0777", false, {}, Number.MAX_SAFE_INTEGER + 1]) {
    await expectUnknown(receipt({ logId: "99", sendAt: 1234, chatId }));
  }
});

test("receipt log IDs require lossless positive signed 64-bit values", async () => {
  for (const logId of [undefined, null, "", "0", 0, -1, 1.5, "01", " 99", "1e3", false, {},
    NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, "9223372036854775808", -1n,
    { toString: () => "99" }]) {
    await expectUnknown(receipt({ logId, sendAt: 1234 }));
  }
  for (const logId of [99, "99", 99n, new FakeLongValue("9223372036854775807")]) {
    const result = await sendWriteOnce({ async sendMessage() {
      return receipt({ logId, sendAt: 1234, status: 0, chatId: new FakeLongValue("777") });
    } }, FakeLong, "777", "hello");
    assert.equal(result.log_id, String(logId));
    assert.equal(result.success, true);
  }
});

test("ACK timestamp must be a positive finite integer", async () => {
  for (const sendAt of [undefined, null, 0, -1, "1234", false, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    await expectUnknown(receipt({ logId: "99", sendAt }));
  }
});
