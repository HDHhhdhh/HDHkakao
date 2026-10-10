/*
 * The direct-send mutation boundary lives in this small module so it can be
 * tested without starting the long-lived stdin/stdout bridge.
 */

function unknownOutcome() {
  const error = new Error("KakaoTalk direct send outcome is unknown");
  error.code = "send_outcome_unknown";
  error.responseFailureKind = "transient_or_unknown";
  return error;
}

function serverRejected(status) {
  const error = new Error("KakaoTalk direct send was rejected");
  error.code = "server_rejected";
  error.serverStatus = status;
  error.responseFailureKind = "provider_rejection";
  return error;
}

export function positiveLongString(value, LongType) {
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) return null;
  } else if (typeof value !== "string" && typeof value !== "bigint" &&
      !(typeof LongType?.isLong === "function" && LongType.isLong(value))) {
    return null;
  }
  const text = String(value);
  return /^[1-9]\d{0,18}$/.test(text) && BigInt(text) <= 9223372036854775807n ? text : null;
}

export function validateSendReceipt(result, chatId, LongType) {
  const logId = positiveLongString(result?.log_id, LongType);
  if (result?.success !== true || result?.status_code !== 0 || result?.chat_id !== chatId ||
      !logId || !Number.isSafeInteger(result?.sent_at) || result.sent_at <= 0) {
    throw unknownOutcome();
  }
  return { success: true, status_code: 0, chat_id: chatId, log_id: logId, sent_at: result.sent_at };
}

export async function sendWriteOnce(session, LongType, chatId, text) {
  let response;
  try {
    response = await session.sendMessage(LongType.fromString(chatId), text);
  } catch {
    throw unknownOutcome();
  }

  // LocoConnection correlates packet IDs; also require the matching method.
  // A zero packet status alone is not a message receipt: body.status can
  // reject the request, and an empty body must never become a green result.
  if (response?.method !== "WRITE" || !Number.isInteger(response.statusCode) || response.statusCode === -1) {
    throw unknownOutcome();
  }
  if (response.statusCode !== 0) throw serverRejected(response.statusCode);
  const body = response.body;
  if (!body || typeof body !== "object" || Array.isArray(body)) throw unknownOutcome();
  if (Object.hasOwn(body, "status")) {
    if (!Number.isSafeInteger(body.status) || body.status === -1) throw unknownOutcome();
    if (body.status !== 0) throw serverRejected(body.status);
  }
  if (Object.hasOwn(body, "error") ||
      (Object.hasOwn(body, "chatId") && positiveLongString(body.chatId, LongType) !== chatId)) {
    throw unknownOutcome();
  }
  // This confirms a well-formed server ACK, not recipient delivery or read.
  return validateSendReceipt({
    success: true,
    status_code: 0,
    chat_id: chatId,
    log_id: body.logId,
    sent_at: body.sendAt,
  }, chatId, LongType);
}
