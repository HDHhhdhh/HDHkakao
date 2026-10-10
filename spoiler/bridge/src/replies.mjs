import Long from 'long';
import { withDeadline, bridgeError } from './io.mjs';

const ID = /^[1-9]\d{0,18}$/;
export function positiveId(value) {
  if (typeof value === 'number' && !Number.isSafeInteger(value)) return null;
  if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'bigint' && !Long.isLong(value)) return null;
  const text = String(value);
  return ID.test(text) && BigInt(text) <= 9223372036854775807n ? text : null;
}

export function parseReplyCommand(value, credentials) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || value.type !== 'send_reply' ||
      typeof value.request_id !== 'string' || !/^[a-zA-Z0-9-]{1,80}$/.test(value.request_id))
    throw bridgeError('reply_invalid');
  if (value.user_id !== credentials.userId || value.device_uuid !== credentials.deviceUuid)
    throw bridgeError('reply_identity_changed');
  const chatId = typeof value.chat_id === 'string' ? positiveId(value.chat_id) : null;
  if (!chatId || typeof value.text !== 'string' || !value.text.trim() || value.text.length > 4000 ||
      Buffer.byteLength(value.text, 'utf8') > 16384) throw bridgeError('reply_invalid');
  return { requestId: value.request_id, chatId, text: value.text };
}

export function validateReplyPacket(packet, chatId) {
  if (packet?.method !== 'WRITE' || !Number.isSafeInteger(packet.statusCode) || packet.statusCode === -1)
    throw bridgeError('reply_outcome_unknown');
  if (packet.statusCode !== 0) throw bridgeError('reply_rejected');
  const body = packet.body;
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw bridgeError('reply_outcome_unknown');
  if (Object.hasOwn(body, 'status')) {
    if (!Number.isSafeInteger(body.status) || body.status === -1) throw bridgeError('reply_outcome_unknown');
    if (body.status !== 0) throw bridgeError('reply_rejected');
  }
  const logId = positiveId(body.logId);
  if (Object.hasOwn(body, 'error') || !logId || !Number.isSafeInteger(body.sendAt) || body.sendAt <= 0 ||
      (Object.hasOwn(body, 'chatId') && positiveId(body.chatId) !== chatId)) throw bridgeError('reply_outcome_unknown');
  return { logId, sentAt: body.sendAt };
}

const MESSAGES = Object.freeze({
  reply_invalid: '대화방 또는 메시지 내용을 확인해 주세요. 최대 4,000자까지 보낼 수 있어요.',
  reply_identity_changed: '연결 계정이나 기기가 바뀌었어요. 다시 연결해 주세요.',
  reply_disconnected: '연결 후 다시 시도해 주세요. 메시지는 보내지 않았어요.',
  reply_rejected: '서버가 전송을 거절했어요. 입력한 내용은 유지됩니다.',
  reply_outcome_unknown: '전송 결과를 확인하지 못했어요. 중복 전송을 피하려면 카카오톡에서 확인해 주세요.',
  reply_duplicate: '이미 처리한 전송 요청이에요. 자동으로 다시 보내지 않습니다.',
});

/** Exactly one WRITE. The high-level client.sendMessage can replay after reconnect. */
export async function executeReplyCommand({ value, credentials, client, signal, emit, emitMessage,
  seenRequests, timeoutMs = 15000 }) {
  let request;
  let writeStarted = false;
  let result;
  try {
    request = parseReplyCommand(value, credentials);
    if (seenRequests.has(request.requestId)) throw bridgeError('reply_duplicate');
    // A bounded set lives for this process only; duplicates are never replayed.
    if (seenRequests.size >= 4096) throw bridgeError('reply_duplicate');
    seenRequests.add(request.requestId);
    if (signal.aborted || !client?.isConnected()) throw bridgeError('reply_disconnected');
    const session = await withDeadline(() => client.acquireSession(), timeoutMs, 'reply_disconnected', signal);
    if (signal.aborted || !client.isConnected() || typeof session?.sendMessage !== 'function')
      throw bridgeError('reply_disconnected');
    writeStarted = true;
    const packet = await withDeadline(() => session.sendMessage(Long.fromString(request.chatId), request.text),
      timeoutMs, 'reply_outcome_unknown', signal);
    const receipt = validateReplyPacket(packet, request.chatId);
    // Pipe order makes the existing host durable inbox commit precede the UI success ACK.
    await emitMessage({ type: 'message', chatId: request.chatId, logId: receipt.logId,
      authorId: credentials.userId, authorName: '나', message: request.text, messageType: 1,
      sentAt: receipt.sentAt, isMine: true, replyAck: true });
    result = { type: 'reply_result', request_id: request.requestId, chat_id: request.chatId,
      user_id: credentials.userId, device_uuid: credentials.deviceUuid,
      status: 'sent', code: 'reply_sent', log_id: receipt.logId, sent_at: receipt.sentAt };
  } catch (error) {
    const known = Object.hasOwn(MESSAGES, error?.code) ? error.code : 'reply_outcome_unknown';
    const code = writeStarted && known !== 'reply_rejected' ? 'reply_outcome_unknown' : known;
    result = { type: 'reply_result', request_id: request?.requestId ??
      (typeof value?.request_id === 'string' && /^[a-zA-Z0-9-]{1,80}$/.test(value.request_id) ? value.request_id : ''),
      chat_id: request?.chatId ?? (typeof value?.chat_id === 'string' ? positiveId(value.chat_id) ?? '' : ''), user_id: credentials.userId,
      device_uuid: credentials.deviceUuid, status: code === 'reply_outcome_unknown' || code === 'reply_duplicate' ? 'unknown' : 'failed',
      code, message: MESSAGES[code] };
  }
  await emit(result);
  return result;
}
