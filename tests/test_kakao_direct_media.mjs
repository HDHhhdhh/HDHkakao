import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { prepareAttachments, PreparedAttachmentCache, sendAttachmentsOnce, strictReceipt, uploadMediaOnce } from "../bridge/kakao_direct_media.mjs";

const LongType = { fromString: (value) => value, ONE: "1" };
const attachment = { filename: "sample.pdf", extension: "pdf", data: Buffer.from("%PDF-synthetic"),
  kind: "file", msgType: 18, checksum: "A".repeat(40) };

test("text and media receipts share the same lossless ID and integer timestamp contract", () => {
  const receipt = { success: true, status_code: 0, chat_id: "777", log_id: "901", sent_at: 1788800000 };
  for (const change of [{ sent_at: 1.5 }, { sent_at: Number.MAX_SAFE_INTEGER + 1 },
    { log_id: { toString: () => "901" } }, { chat_id: { toString: () => "777" } }]) {
    assert.throws(() => strictReceipt({ ...receipt, ...change }, "777"), { code: "send_outcome_unknown" });
  }
  const long = { toString: () => "9223372036854775807" };
  assert.equal(strictReceipt({ ...receipt, log_id: long }, "777", { isLong: value => value === long }).log_id,
    "9223372036854775807");
});
function fixture(options = {}) {
  const calls = [];
  let uploadIndex = 0;
  const conn = {
    onPush(fn) { this.push = fn; }, onClose(fn) { this.closed = fn; },
    async connectSecure(host, port) { calls.push(["connect", host, port]); if (options.connectError) throw new Error("PRIVATE"); },
    async sendPacket(method, body) {
      calls.push([method, body]);
      if (options.postError) throw new Error("PRIVATE");
      if (options.completeEarly) this.push(options.completeEarly);
      return options.post ?? { statusCode: 0, body: { o: 0 } };
    },
    async writeRaw(data) {
      calls.push(["bytes", data.length]);
      if (options.writeError) throw new Error("PRIVATE");
      if (!options.noComplete) this.push(options.complete ?? { method: "COMPLETE", statusCode: 0,
        body: { chatLog: { logId: String(901 + uploadIndex++), sendAt: 1788800000, chatId: "777", type: 18 } } });
    },
    close() { calls.push(["close"]); this.closed?.(); },
  };
  const session = {
    async sendMessage(chatId, text) { calls.push(["WRITE", chatId, text]); return options.write ?? { method: "WRITE", statusCode: 0, body: { logId: "900", sendAt: 1788800000 } }; },
    async shipMedia(...args) { calls.push(["SHIP", ...args]); return options.ship ?? { statusCode: 0, body: { k: "PRIVATE_TICKET", vh: "media.example.test", p: 443 } }; },
  };
  return { calls, options: { session, LongType, connectionFactory: () => conn, deviceConfig: { os: "android", appVersion: "test" },
    mccmnc: "99999", userId: "888", chatId: "777", attachment, timeoutMs: 50 } };
}

test("SHIP, POST, bytes, COMPLETE happen once and return only a verified receipt", async () => {
  const f = fixture();
  const result = await uploadMediaOnce(f.options);
  assert.deepEqual(f.calls.map((call) => call[0]), ["SHIP", "connect", "POST", "bytes", "close"]);
  const post = f.calls.find(([method]) => method === "POST")[1];
  assert.deepEqual(post, { k: "PRIVATE_TICKET", s: attachment.data.length, t: 18, u: "888", os: "android", av: "test",
    nt: 0, mm: "99999", f: "sample.pdf", c: "777", mid: "1", ns: true });
  assert.equal(result.log_id, "901");
  assert.equal(result.kind, "file");
  assert.ok(!JSON.stringify(result).includes("PRIVATE"));
});

test("POST rejection and invalid offsets never stream bytes or retry", async () => {
  for (const post of [{ statusCode: 9, body: { o: 0 } }, { statusCode: 0, body: {} },
    { statusCode: 0, body: { o: -1 } }, { statusCode: 0, body: { o: 0.5 } },
    { statusCode: 0, body: { o: attachment.data.length + 1 } }]) {
    const f = fixture({ post });
    await assert.rejects(uploadMediaOnce(f.options), { code: "send_outcome_unknown" });
    assert.equal(f.calls.filter(([kind]) => kind === "POST").length, 1);
    assert.equal(f.calls.filter(([kind]) => kind === "bytes").length, 0);
    assert.equal(f.calls.at(-1)[0], "close");
  }
});

test("photos use type 2 and include measured dimensions in POST", async () => {
  const f = fixture({ complete: { method: "COMPLETE", statusCode: 0,
    body: { chatLog: { logId: "902", sendAt: 1, chatId: "777", type: 2 } } } });
  const result = await uploadMediaOnce({ ...f.options, attachment: {
    ...attachment, filename: "image.png", extension: "png", msgType: 2, kind: "photo", width: 2, height: 3 } });
  assert.equal(f.calls[0][2], 2);
  const post = f.calls.find(([kind]) => kind === "POST")[1];
  assert.equal(post.t, 2);
  assert.equal(post.w, 2);
  assert.equal(post.h, 3);
  assert.equal(result.kind, "photo");
});

test("partial upload offset streams only the remaining bytes", async () => {
  const f = fixture({ post: { statusCode: 0, body: { o: 5 } } });
  await uploadMediaOnce(f.options);
  assert.equal(f.calls.find(([kind]) => kind === "bytes")[1], attachment.data.length - 5);
});

test("an already complete upload still needs a valid COMPLETE packet", async () => {
  const complete = { method: "COMPLETE", statusCode: 0, body: { chatLog: { logId: "901", sendAt: 1 } } };
  const f = fixture({ post: { statusCode: 0, body: { o: attachment.data.length } }, completeEarly: complete });
  await uploadMediaOnce(f.options);
  assert.equal(f.calls.filter(([kind]) => kind === "bytes").length, 0);
});

test("transport errors and missing or mismatched COMPLETE are unknown", async () => {
  for (const options of [{ postError: true }, { writeError: true }, { noComplete: true },
    { complete: { method: "COMPLETE", statusCode: 0, body: {} } },
    { complete: { method: "COMPLETE", statusCode: 4, body: { chatLog: { logId: "1", sendAt: 1 } } } },
    { complete: { method: "COMPLETE", statusCode: 0, body: { chatLog: { logId: "1", sendAt: 1, chatId: "999" } } } },
    { complete: { method: "COMPLETE", statusCode: 0, body: { chatLog: { logId: "", sendAt: 1 } } } },
    { complete: { method: "COMPLETE", statusCode: 0, body: { chatLog: { logId: "1", sendAt: 0 } } } }]) {
    const f = fixture(options);
    await assert.rejects(uploadMediaOnce(f.options), { code: "send_outcome_unknown" });
    assert.equal(f.calls.filter(([kind]) => kind === "POST").length, 1);
    assert.equal(f.calls.at(-1)[0], "close");
  }
});

test("SHIP rejection is pre-mutation; synthetic status remains unknown", async () => {
  for (const [ship, code] of [[{ statusCode: 9 }, "ship_rejected"], [{ statusCode: -1 }, "send_outcome_unknown"],
    [{ statusCode: 0, body: {} }, "invalid_ship_receipt"]]) {
    const f = fixture({ ship });
    await assert.rejects(uploadMediaOnce(f.options), { code });
    assert.deepEqual(f.calls.map(([kind]) => kind), ["SHIP"]);
  }
});

test("batch sends text then files with independent receipts, without high-level retries", async () => {
  const f = fixture();
  const result = await sendAttachmentsOnce({ ...f.options, text: "hello", attachments: [attachment, attachment] });
  assert.equal(result.attachment_count, 2);
  assert.deepEqual(result.results.map((r) => r.kind), ["text", "file", "file"]);
  assert.equal(f.calls.filter(([kind]) => kind === "WRITE").length, 1);
  assert.equal(f.calls.filter(([kind]) => kind === "POST").length, 2);
});

test("file-only and whitespace-text batches have no WRITE", async () => {
  const f = fixture();
  const result = await sendAttachmentsOnce({ ...f.options, text: " \n", attachments: [attachment] });
  assert.equal(result.results.length, 1);
  assert.ok(!f.calls.some(([kind]) => kind === "WRITE"));
});

test("Unicode blank detection matches Python while preserving the original text", async () => {
  for (const [text, expectedWrites] of [["\u0085", 0], ["\u001c\u001d\u001e\u001f", 0], ["\ufeff", 1]]) {
    const f = fixture();
    const result = await sendAttachmentsOnce({ ...f.options, text, attachments: [attachment] });
    const writes = f.calls.filter(([kind]) => kind === "WRITE");
    assert.equal(writes.length, expectedWrites);
    assert.equal(result.results.length, expectedWrites + 1);
    if (expectedWrites) assert.equal(writes[0][2], text);
  }
});

test("a repeated receipt log ID makes the batch unknown and stops later uploads", async () => {
  const f = fixture({ complete: { method: "COMPLETE", statusCode: 0,
    body: { chatLog: { logId: "901", sendAt: 1, chatId: "777", type: 18 } } } });
  await assert.rejects(sendAttachmentsOnce({ ...f.options, text: "", attachments: [attachment, attachment, attachment] }),
    { code: "send_outcome_unknown" });
  assert.equal(f.calls.filter(([kind]) => kind === "POST").length, 2);
});

test("a rejection after confirmed text is an incomplete unknown batch", async () => {
  const f = fixture({ ship: { statusCode: 9 } });
  await assert.rejects(sendAttachmentsOnce({ ...f.options, text: "hello", attachments: [attachment, attachment] }),
    (error) => error.code === "send_outcome_unknown" && error.completedCount === 1);
  assert.equal(f.calls.filter(([kind]) => kind === "SHIP").length, 1);
});

test("malformed text receipt stops the batch before uploading", async () => {
  const f = fixture({ write: { method: "WRITE", statusCode: 0, body: {} } });
  await assert.rejects(sendAttachmentsOnce({ ...f.options, text: "hello", attachments: [attachment] }), { code: "send_outcome_unknown" });
  assert.deepEqual(f.calls.map(([kind]) => kind), ["WRITE"]);
});

test("all files are snapshotted and PDF/image headers validated before any send", async () => {
  const root = await mkdtemp(join(tmpdir(), "kakao-media-test-"));
  try {
    const pdf = join(root, "sample.pdf");
    const png = join(root, "sample.png");
    await writeFile(pdf, "%PDF-1.4 synthetic");
    await writeFile(png, "synthetic image");
    const detect = () => ({ width: 2, height: 3, mimeType: "image/png" });
    const prepared = await prepareAttachments([pdf, png], detect);
    assert.deepEqual(prepared.map((item) => item.kind), ["file", "photo"]);
    assert.equal(prepared[1].width, 2);
    assert.match(prepared[0].checksum, /^[A-F0-9]{40}$/);
    await writeFile(pdf, "changed after snapshot");
    assert.equal(prepared[0].data.toString(), "%PDF-1.4 synthetic");
    await assert.rejects(prepareAttachments([pdf], detect), { code: "invalid_pdf" });
    let sends = 0;
    await assert.rejects((async () => {
      await prepareAttachments([png, pdf], detect);
      sends += 1;
    })(), { code: "invalid_pdf" });
    assert.equal(sends, 0, "a bad later file blocks even the optional text");
    await assert.rejects(prepareAttachments([png], () => { throw new Error("PRIVATE"); }), { code: "invalid_image" });
    await assert.rejects(prepareAttachments([png], () => ({ width: 0, height: 1, mimeType: "image/png" })), { code: "invalid_image" });
    await assert.rejects(prepareAttachments([join(root, "missing.txt")], detect), { code: "attachment_read_failed" });
    await assert.rejects(prepareAttachments([join(root, "run.exe")], detect), { code: "unsupported_attachment_type" });
    await assert.rejects(prepareAttachments([], detect), { code: "invalid_attachment_count" });
    await assert.rejects(prepareAttachments(Array(11).fill(png), detect), { code: "invalid_attachment_count" });
    await writeFile(join(root, "empty.txt"), "");
    await assert.rejects(prepareAttachments([join(root, "empty.txt")], detect), { code: "attachment_size_invalid" });
  } finally {
    assert.ok(root.startsWith(join(tmpdir(), "kakao-media-test-")));
    await rm(root, { recursive: true, force: true });
  }
});

test("one run reuses a shared file across recipients and distinct attachment bundles", async () => {
  const root = await mkdtemp(join(tmpdir(), "kakao-media-cache-"));
  try {
    const photo = join(root, "shared.png");
    const pdf = join(root, "extra.pdf");
    await writeFile(photo, "frozen image");
    await writeFile(pdf, "%PDF-frozen");
    let detections = 0;
    const detect = () => { detections += 1; return { width: 2, height: 3, mimeType: "image/png" }; };
    const cache = new PreparedAttachmentCache();
    const first = await cache.prepare("run-1", [photo], detect);
    const next = await cache.prepare("run-1", [photo, pdf, photo], detect);
    assert.equal(detections, 1);
    assert.equal(first[0].data, next[0].data);
    assert.equal(next[0].data, next[2].data);
    assert.equal(cache.entries.size, 2);
    assert.equal(cache.bytes, Buffer.byteLength("frozen image") + Buffer.byteLength("%PDF-frozen"));
    assert.equal(first[0].filename, "shared.png");
    await writeFile(photo, "modified image");
    assert.equal((await cache.prepare("run-1", [photo], detect))[0].data.toString(), "frozen image");
    cache.release("another-run");
    assert.equal(cache.entries.size, 2);
    cache.release("run-1");
    assert.equal(cache.bytes, 0);
    assert.equal(cache.entries.size, 0);
    const fresh = await cache.prepare("run-2", [photo], detect);
    assert.equal(fresh[0].data.toString(), "modified image");
    assert.equal(detections, 2);
  } finally {
    assert.ok(root.startsWith(join(tmpdir(), "kakao-media-cache-")));
    await rm(root, { recursive: true, force: true });
  }
});

test("evicted buffers reload with checksum verification while reusing image metadata", async () => {
  const root = await mkdtemp(join(tmpdir(), "kakao-media-cache-"));
  try {
    const first = join(root, "first.png");
    const second = join(root, "second.png");
    await writeFile(first, "image-a");
    await writeFile(second, "image-b");
    let detections = 0;
    const detect = () => { detections += 1; return { width: 2, height: 3, mimeType: "image/png" }; };
    const cache = new PreparedAttachmentCache(8);
    await cache.prepare("run", [first], detect);
    await cache.prepare("run", [second], detect);
    assert.ok(cache.bytes <= 8);
    const reloaded = await cache.prepare("run", [first], detect);
    assert.equal(reloaded[0].data.toString(), "image-a");
    assert.equal(detections, 2, "rehydration does not repeat image detection");
    await cache.prepare("run", [second], detect);
    await writeFile(first, "image-c");
    await assert.rejects(cache.prepare("run", [first], detect), { code: "attachment_changed" });
    assert.ok(cache.bytes <= 8);
    cache.clear();
    assert.equal(cache.bytes, 0);
    assert.equal(cache.entries.size, 0);
  } finally {
    assert.ok(root.startsWith(join(tmpdir(), "kakao-media-cache-")));
    await rm(root, { recursive: true, force: true });
  }
});

test("a row remains valid when its buffers exceed a smaller cache budget", async () => {
  const root = await mkdtemp(join(tmpdir(), "kakao-media-cache-"));
  try {
    const first = join(root, "first.pdf");
    const second = join(root, "second.pdf");
    await writeFile(first, "%PDF-a");
    await writeFile(second, "%PDF-b");
    const cache = new PreparedAttachmentCache(8);
    const prepared = await cache.prepare("run", [first, second, first], () => {});
    assert.deepEqual(prepared.map(item => item.data.toString()), ["%PDF-a", "%PDF-b", "%PDF-a"]);
    assert.equal(prepared[0].data, prepared[2].data);
    assert.ok(cache.bytes <= 8);
    const otherRun = await cache.prepare("new-run", [first], () => {});
    assert.equal(otherRun[0].data.toString(), "%PDF-a");
    assert.equal(cache.entries.size, 1, "switching runs releases previous metadata too");
    await assert.rejects(cache.prepare("", [first], () => {}), { code: "invalid_attachment_run_id" });
    assert.throws(() => cache.release("../run"), { code: "invalid_attachment_run_id" });
  } finally {
    assert.ok(root.startsWith(join(tmpdir(), "kakao-media-cache-")));
    await rm(root, { recursive: true, force: true });
  }
});
