// Synthetic NDJSON child only. No SDK, network, account, or desktop access.
import { createInterface } from 'node:readline';
const reader = createInterface({ input: process.stdin });
let start;
// Keep synthetic bodies inside the real seven-day retention window.
const sentAt = Math.floor(Date.now() / 1000) - 60;
const emit = (event) => process.stdout.write(`${JSON.stringify(event)}\n`);
reader.on('line', (line) => {
  const frame = JSON.parse(line.trim());
  if (!start) {
    start = frame;
    emit({ type: 'message', chatId: '101', logId: '1000', authorId: '222', authorName: '테스트',
      chatTitle: '테스트 대화방', message: '합성 메시지', messageType: 1, sentAt, isMine: false });
    emit({ type: 'status', status: 'ready' });
    return;
  }
  if (frame.type === 'release_previous_push') {
    emit({ type: 'message', chatId: '101', logId: '5000', authorId: '222', authorName: '테스트',
      chatTitle: '테스트 대화방', message: 'ACK보다 늦게 표시된 이전 메시지', messageType: 1, sentAt: sentAt + 1, isMine: false });
    return;
  }
  if (frame.type !== 'send_reply') return;
  const mode = start.oauthToken.split(':')[1];
  if (mode === 'pending') return;
  const common = { type: 'reply_result', request_id: frame.request_id, chat_id: frame.chat_id,
    user_id: frame.user_id, device_uuid: frame.device_uuid };
  if (mode === 'reject') { emit({ ...common, status: 'failed', code: 'reply_rejected' }); return; }
  if (mode === 'unknown') { emit({ ...common, status: 'unknown', code: 'reply_outcome_unknown' }); return; }
  if (mode !== 'ack-only') {
    const body = { type: 'message', chatId: frame.chat_id, logId: '5001', authorId: frame.user_id,
      authorName: '나', message: mode === 'wrong-body' ? '다른 원문' : frame.text, messageType: 1, sentAt: sentAt + 1, isMine: true, replyAck: true };
    emit(body); emit(body); // Duplicate server push must not add a second bubble.
  }
  emit({ ...common, status: 'sent', code: 'reply_sent', log_id: '5001', sent_at: mode === 'bad-time' ? 'invalid' : sentAt + 1,
    ...(mode === 'foreign' ? { chat_id: '999' } : {}) });
});
reader.on('close', () => process.exit(0));
