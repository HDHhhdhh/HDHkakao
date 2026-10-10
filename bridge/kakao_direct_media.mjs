import { createHash } from "node:crypto";
import { open } from "node:fs/promises";
import { basename, extname, isAbsolute, resolve } from "node:path";
import { positiveLongString, sendWriteOnce, validateSendReceipt } from "./kakao_direct_send_once.mjs";

// Product limits, deliberately separate from the provider's changing limits.
export const MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024;
export const MAX_ATTACHMENT_TOTAL_BYTES = MAX_ATTACHMENT_BYTES;
export const MAX_ATTACHMENTS = 10;
export const ATTACHMENT_EXTENSIONS = new Set(
  "jpg jpeg png gif webp pdf doc docx xls xlsx ppt pptx hwp hwpx txt csv json zip 7z tar gz mp4 mov webm mkv m4v mp3 m4a wav ogg".split(" "),
);
const IMAGE_MIME = { jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", gif: "image/gif", webp: "image/webp" };
function hasDirectText(text) {
  // Match Python 3.12 str.strip(), which the bridge and service use for
  // receipt counts. JS trim() differs for U+0085/U+001C..001F and U+FEFF.
  // Only decide whether to WRITE; preserve the original text when sending.
  return /[^\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u.test(text);
}

function failure(code, unknown = false, status) {
  const error = new Error("KakaoTalk direct attachment operation failed");
  error.code = unknown ? "send_outcome_unknown" : code;
  error.responseFailureKind = unknown ? "transient_or_unknown" : "provider_rejection";
  if (Number.isInteger(status)) error.serverStatus = status;
  return error;
}

function validId(value) {
  return positiveLongString(value) !== null;
}

export const strictReceipt = validateSendReceipt;

function validatePaths(paths) {
  if (!Array.isArray(paths) || paths.length < 1 || paths.length > MAX_ATTACHMENTS) {
    throw failure("invalid_attachment_count");
  }
  return paths.map((path) => {
    if (typeof path !== "string" || !isAbsolute(path)) throw failure("invalid_attachment_path");
    const filename = basename(path);
    const extension = extname(filename).slice(1).toLowerCase();
    if (!ATTACHMENT_EXTENSIONS.has(extension) || /[\x00-\x1f\x7f]/u.test(filename) || filename.length > 255) {
      throw failure("unsupported_attachment_type");
    }
    return { path, filename, extension };
  });
}

async function readAttachment(path, remainingBytes, beforeAllocate = () => {}) {
  let handle;
  let data;
  try {
    handle = await open(path, "r");
    const before = await handle.stat();
    if (!before.isFile() || before.size < 1 || before.size > MAX_ATTACHMENT_BYTES || before.size > remainingBytes) {
      throw failure("attachment_size_invalid");
    }
    beforeAllocate(before.size);
    // Snapshot every file before any WRITE/SHIP. Bound allocation even if a
    // producer grows a file while it is being read.
    data = Buffer.alloc(before.size);
    let offset = 0;
    while (offset < data.length) {
      const { bytesRead } = await handle.read(data, offset, data.length - offset, offset);
      if (bytesRead === 0) throw failure("attachment_changed");
      offset += bytesRead;
    }
    const after = await handle.stat();
    if (after.size !== before.size || after.mtimeMs !== before.mtimeMs || after.ctimeMs !== before.ctimeMs) {
      throw failure("attachment_changed");
    }
  } catch (error) {
    if (error?.responseFailureKind) throw error;
    throw failure("attachment_read_failed");
  } finally {
    if (handle) await handle.close();
  }
  return data;
}

async function prepareAttachment({ path, filename, extension }, detectImageDimensions, remainingBytes, beforeAllocate) {
  const data = await readAttachment(path, remainingBytes, beforeAllocate);
  let dimensions = {};
  const photo = Object.hasOwn(IMAGE_MIME, extension);
  if (photo) {
    try { dimensions = detectImageDimensions(data); } catch { throw failure("invalid_image"); }
    if (dimensions.mimeType !== IMAGE_MIME[extension] || !Number.isInteger(dimensions.width) ||
        !Number.isInteger(dimensions.height) || dimensions.width < 1 || dimensions.height < 1 ||
        dimensions.width > 65535 || dimensions.height > 65535) throw failure("invalid_image");
  } else if (extension === "pdf" && data.subarray(0, 5).toString("ascii") !== "%PDF-") {
    throw failure("invalid_pdf");
  }
  return { filename, extension, data, msgType: photo ? 2 : 18, kind: photo ? "photo" : "file",
    checksum: createHash("sha1").update(data).digest("hex").toUpperCase(),
    ...(photo ? { width: dimensions.width, height: dimensions.height } : {}) };
}

export async function prepareAttachments(paths, detectImageDimensions) {
  const prepared = [];
  let total = 0;
  for (const spec of validatePaths(paths)) {
    const attachment = await prepareAttachment(spec, detectImageDimensions, MAX_ATTACHMENT_TOTAL_BYTES - total);
    total += attachment.data.length;
    prepared.push(attachment);
  }
  return prepared;
}

function validRunId(runId) {
  if (typeof runId !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(runId)) throw failure("invalid_attachment_run_id");
  return runId;
}

// The host freezes source files into a private run directory before sending.
// Cache only requests carrying that run identity: legacy path-only requests
// must continue reading fresh contents. Buffers share one 100 MB LRU budget;
// evicted snapshots retain metadata and are verified before being reused.
export class PreparedAttachmentCache {
  constructor(maxBytes = MAX_ATTACHMENT_TOTAL_BYTES) {
    if (!Number.isSafeInteger(maxBytes) || maxBytes < 1 || maxBytes > MAX_ATTACHMENT_TOTAL_BYTES) {
      throw new RangeError("Invalid attachment cache budget");
    }
    this.maxBytes = maxBytes;
    this.runId = null;
    this.entries = new Map();
    this.buffers = new Map();
    this.bytes = 0;
  }

  clear() {
    this.entries.clear();
    this.buffers.clear();
    this.bytes = 0;
    this.runId = null;
  }

  release(runId) {
    validRunId(runId);
    if (this.runId === runId) this.clear();
  }

  reserve(size, protectedKeys) {
    for (const [key, entry] of this.buffers) {
      if (this.bytes + size <= this.maxBytes) break;
      if (protectedKeys.has(key)) continue;
      this.buffers.delete(key);
      this.bytes -= entry.size;
      entry.data = null;
    }
    return this.bytes + size <= this.maxBytes;
  }

  async prepare(runId, paths, detectImageDimensions) {
    validRunId(runId);
    const specs = validatePaths(paths);
    if (this.runId !== runId) {
      this.clear();
      this.runId = runId;
    }
    const prepared = [];
    const selected = new Map();
    let total = 0;
    for (const spec of specs) {
      const resolved = resolve(spec.path);
      const key = (process.platform === "win32" ? resolved.toLowerCase() : resolved) + "\0" + spec.filename;
      let entry = this.entries.get(key);
      let data = selected.get(key) ?? entry?.data;
      if (entry && entry.size > MAX_ATTACHMENT_TOTAL_BYTES - total) throw failure("attachment_size_invalid");
      if (!data) {
        const reserve = (size) => this.reserve(size, new Set(selected.keys()));
        if (entry) {
          data = await readAttachment(spec.path, MAX_ATTACHMENT_TOTAL_BYTES - total, reserve);
          if (data.length !== entry.size || createHash("sha1").update(data).digest("hex").toUpperCase() !== entry.metadata.checksum) {
            throw failure("attachment_changed");
          }
        } else {
          const item = await prepareAttachment(spec, detectImageDimensions, MAX_ATTACHMENT_TOTAL_BYTES - total, reserve);
          data = item.data;
          const { data: ignored, ...metadata } = item;
          entry = { metadata, size: data.length, data: null };
          this.entries.set(key, entry);
        }
        if (this.reserve(data.length, new Set(selected.keys()))) {
          entry.data = data;
          this.bytes += data.length;
          this.buffers.set(key, entry);
        }
      } else if (entry.data) {
        // Refresh access order without changing ownership or the byte count.
        this.buffers.delete(key);
        this.buffers.set(key, entry);
      }
      total += data.length;
      if (total > MAX_ATTACHMENT_TOTAL_BYTES) throw failure("attachment_size_invalid");
      selected.set(key, data);
      prepared.push({ ...entry.metadata, data });
    }
    return prepared;
  }
}

// Protocol follows agent-messenger@2.37.1 protocol/media-uploader.ts:
// SHIP -> secure media connection -> POST -> encrypted bytes -> COMPLETE.
// The high-level SDK upload wrapper retries after reconnect; this path never
// replays POST and validates POST before writing any file bytes.
export async function uploadMediaOnce({ session, LongType, connectionFactory, deviceConfig, mccmnc,
  userId, chatId, attachment, timeoutMs = 120_000 }) {
  let mutation = false;
  let conn;
  let timer;
  let expired = false;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => { expired = true; reject(failure("media_timeout", mutation)); }, timeoutMs);
  });
  const bounded = (operation) => Promise.race([operation, deadline]);
  try {
    const ship = await bounded(session.shipMedia(LongType.fromString(chatId), attachment.msgType,
      attachment.data.length, attachment.checksum, attachment.extension));
    if (ship?.statusCode !== 0) throw failure("ship_rejected", !Number.isInteger(ship?.statusCode) || ship.statusCode === -1, ship?.statusCode);
    const ticket = ship.body;
    if (typeof ticket?.k !== "string" || !ticket.k || typeof ticket?.vh !== "string" || !ticket.vh ||
        !Number.isInteger(ticket.p) || ticket.p < 1 || ticket.p > 65535) throw failure("invalid_ship_receipt");
    conn = connectionFactory();
    let completeResolve;
    const complete = new Promise((resolve) => { completeResolve = resolve; });
    conn.onPush((packet) => { if (packet?.method === "COMPLETE") completeResolve(packet); });
    conn.onClose(() => completeResolve(null));
    await bounded(conn.connectSecure(ticket.vh, ticket.p));
    if (expired) throw failure("media_timeout");
    const postBody = { k: ticket.k, s: attachment.data.length, t: attachment.msgType,
      u: LongType.fromString(userId), os: deviceConfig.os, av: deviceConfig.appVersion,
      nt: 0, mm: mccmnc, f: attachment.filename, c: LongType.fromString(chatId), mid: LongType.ONE, ns: true };
    if (attachment.kind === "photo") Object.assign(postBody, { w: attachment.width, h: attachment.height });
    mutation = true;
    const post = await bounded(conn.sendPacket("POST", postBody));
    if (post?.statusCode !== 0) throw failure("post_rejected", true, post?.statusCode);
    const offset = post?.body?.o;
    if (!Number.isInteger(offset) || offset < 0 || offset > attachment.data.length) throw failure("invalid_post_offset", true);
    if (offset < attachment.data.length) await bounded(conn.writeRaw(attachment.data.subarray(offset)));
    const packet = await bounded(complete);
    const log = packet?.body?.chatLog;
    if (packet?.method !== "COMPLETE" || packet?.statusCode !== 0 || !log ||
        (log.chatId != null && String(log.chatId) !== chatId) ||
        (log.type != null && log.type !== attachment.msgType)) throw failure("invalid_complete_receipt", true);
    return { ...strictReceipt({ success: true, status_code: 0, chat_id: chatId,
      log_id: log.logId, sent_at: log.sendAt }, chatId, LongType), kind: attachment.kind };
  } catch (error) {
    if (mutation || error?.responseFailureKind === "transient_or_unknown") throw failure("media_failed", true);
    if (error?.responseFailureKind) throw error;
    throw failure("media_prepare_failed");
  } finally {
    clearTimeout(timer);
    if (conn) { try { conn.close(); } catch {} }
  }
}

export async function sendAttachmentsOnce({ session, LongType, chatId, text, attachments, ...mediaOptions }) {
  if (!validId(chatId) || !validId(mediaOptions.userId) || typeof text !== "string" || !attachments?.length) {
    throw failure("invalid_media_request");
  }
  const results = [];
  try {
    if (hasDirectText(text)) {
      const result = await sendWriteOnce(session, LongType, chatId, text);
      results.push({ ...result, kind: "text" });
    }
    for (const attachment of attachments) {
      const receipt = await uploadMediaOnce({ session, LongType, chatId, attachment, ...mediaOptions });
      if (results.some((previous) => previous.log_id === receipt.log_id)) throw failure("duplicate_media_receipt", true);
      results.push(receipt);
    }
  } catch (error) {
    if (results.length || error?.responseFailureKind === "transient_or_unknown") {
      const unknown = failure("send_outcome_unknown", true);
      unknown.completedCount = results.length;
      throw unknown;
    }
    throw error;
  }
  return { ...results.at(-1), attachment_count: attachments.length, results };
}
