/*
 * Read-only reconciliation for agent-messenger@2.37.1 getMessagePage.
 * That SDK method filters by the requested chat ID, paginates forward from
 * an exclusive log ID, and uses MCHATLOGS / CHATINFO / SYNCMSG, not CHATONROOM.
 * This module receives only that read function; it has no send capability.
 */
import { performance } from "node:perf_hooks";

const MAX_LONG = 9223372036854775807n;

function idString(value, { zero = false, LongType } = {}) {
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) return null;
  } else if (typeof value !== "string" && typeof value !== "bigint" &&
      !(typeof LongType?.isLong === "function" && LongType.isLong(value))) return null;
  const text = String(value);
  if (!(zero ? /^(0|[1-9][0-9]{0,18})$/ : /^[1-9][0-9]{0,18}$/).test(text)) return null;
  return BigInt(text) <= MAX_LONG ? text : null;
}

function timestamp(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function object(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

async function beforeDeadline(operation, remaining) {
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(operation),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("read deadline")), remaining); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

export async function reconcileDirectTextHistory({
  getMessagePage, chatId, ownUserId, text, sentAfter, sentBefore,
  sinceLogId = "0", throughLogId, observedAt, expectedLogId,
  LongType, maxPages = 20, pageSize = 100, maxDurationMs = 30_000,
}) {
  let pages = 0;
  let scannedCount = 0;
  let cursor = "0";
  let lowerBoundObserved = false;
  const result = (outcome, reason, matchingLogIds = [], coverageComplete = false) => ({
    outcome, reason, matchingLogIds, pages, scannedCount, lastCursor: cursor, coverageComplete,
  });
  // A nonzero lower cursor needs independent proof that it precedes the
  // incident. Until that contract exists, only a full-from-zero scan can
  // support absence. Also require a real message at/before the incident
  // start: an EOF may describe only retained history, not the incident range.
  // An empty page or stale login snapshot alone proves nothing.
  if (typeof getMessagePage !== "function" || idString(chatId) !== chatId ||
      idString(ownUserId, { LongType }) === null || typeof text !== "string" || !text.length ||
      !timestamp(sentAfter) || !timestamp(sentBefore) || sentAfter > sentBefore ||
      sinceLogId !== "0" || idString(throughLogId, { zero: true }) !== throughLogId ||
      !timestamp(observedAt) || observedAt < sentBefore ||
      (expectedLogId !== undefined && (idString(expectedLogId) !== expectedLogId ||
        BigInt(expectedLogId) > BigInt(throughLogId))) ||
      !Number.isInteger(maxPages) || maxPages < 1 || maxPages > 100 ||
      !Number.isInteger(pageSize) || pageSize < 1 || pageSize > 100 ||
      !Number.isInteger(maxDurationMs) || maxDurationMs < 1 || maxDurationMs > 120_000) {
    return result("unknown", "invalid_reconciliation_bounds");
  }
  const author = idString(ownUserId, { LongType });
  const upper = BigInt(throughLogId);
  const deadline = performance.now() + maxDurationMs;
  for (let pageIndex = 0; pageIndex < maxPages; pageIndex += 1) {
    const remaining = Math.ceil(deadline - performance.now());
    if (remaining <= 0) return result("unknown", "read_deadline");
    let page;
    try {
      // Omitting from selects the SDK's explicit zero-cursor default.
      const options = cursor === "0" ? { count: pageSize } : { count: pageSize, from: cursor };
      pages += 1;
      page = await beforeDeadline(() => getMessagePage(chatId, options), remaining);
    } catch {
      return result("unknown", "history_read_failed");
    }
    if (!object(page) || !Array.isArray(page.messages) || page.messages.length > pageSize ||
        typeof page.complete !== "boolean") return result("unknown", "invalid_history_page");
    let previous = BigInt(cursor);
    const matches = [];
    let clockBoundaryMessage = false;
    for (const message of page.messages) {
      if (!object(message)) return result("unknown", "invalid_history_message");
      const logId = idString(message.log_id);
      if (typeof message.log_id !== "string" || logId === null || BigInt(logId) <= previous || !Number.isInteger(message.type) ||
          !timestamp(message.sent_at) || message.sent_at - observedAt > 60 ||
          (Object.hasOwn(message, "chat_id") && idString(message.chat_id, { LongType }) !== chatId)) {
        return result("unknown", "invalid_history_message");
      }
      previous = BigInt(logId);
      if (message.sent_at <= sentAfter) lowerBoundObserved = true;
      if (message.type === 1) {
        const messageAuthor = idString(message.author_id, { LongType });
        if (messageAuthor === null || typeof message.message !== "string") {
          return result("unknown", "invalid_text_history_message");
        }
        if (messageAuthor === author && message.message === text &&
            sentAfter <= message.sent_at && message.sent_at <= sentBefore &&
            (expectedLogId === undefined || logId === expectedLogId)) matches.push(logId);
        if (messageAuthor === author && message.message === text &&
            (message.sent_at > sentBefore || (sentAfter - 60 < message.sent_at && message.sent_at < sentAfter))) {
          clockBoundaryMessage = true;
        }
      }
    }
    const nextCursor = page.messages.length ? String(previous) : null;
    if (page.next_cursor !== nextCursor) return result("unknown", "invalid_history_cursor");
    scannedCount += page.messages.length;
    if (nextCursor !== null) cursor = nextCursor;
    const reachedUpper = page.complete === true && BigInt(cursor) >= upper;
    const coverageComplete = reachedUpper && lowerBoundObserved;
    // A positive match is sufficient to prevent a duplicate even if a later
    // page cannot be read. Only full, watermark-reaching coverage can support
    // absence from the server history visible to this account.
    if (matches.length) return result("present", "exact_message_found", matches, coverageComplete);
    if (clockBoundaryMessage) return result("unknown", "clock_boundary_message");
    if (coverageComplete) return result("absent", "covered_history_without_match", [], true);
    if (reachedUpper) return result("unknown", "history_missing_incident_lower_bound");
    if (page.complete || !page.messages.length) return result("unknown", "history_did_not_reach_watermark");
  }
  return result("unknown", "history_page_limit");
}

export function reconcileLatestHistorySnapshot({ chatInfo, chatId, throughLogId, sentAfter, observedAt, LongType }) {
  const unknown = (reason = "invalid_latest_history_snapshot") => ({ outcome: "unknown", reason, coverageComplete: false });
  // The caller must obtain this from a fresh, status-checked CHATINFO response,
  // never a cached/login snapshot. Matching both IDs ties the last-message
  // timestamp to that response's current server watermark. No raw text is read
  // or returned. This establishes current server-history absence only.
  if (!object(chatInfo) || idString(chatId) !== chatId ||
      idString(throughLogId) !== throughLogId ||
      idString(chatInfo.chatId, { LongType }) !== chatId ||
      idString(chatInfo.lastLogId, { LongType }) !== throughLogId ||
      !timestamp(sentAfter) || !timestamp(observedAt) || sentAfter > observedAt) return unknown();
  const latest = chatInfo.lastChatLog;
  if (!object(latest) || idString(latest.logId, { LongType }) !== throughLogId ||
      !timestamp(latest.sendAt) || latest.sendAt > observedAt ||
      (Object.hasOwn(latest, "chatId") && idString(latest.chatId, { LongType }) !== chatId) ||
      (Object.hasOwn(latest, "chat_id") && idString(latest.chat_id, { LongType }) !== chatId)) return unknown();
  if (latest.sendAt >= sentAfter - 60) return unknown("latest_history_not_before_incident");
  return { outcome: "absent", reason: "latest_server_message_precedes_incident", coverageComplete: true };
}
