// Offline synthetic source contract only: no SDK, network, or real account data.
import readline from 'node:readline';
import fs from 'node:fs';
import path from 'node:path';

const input = readline.createInterface({ input: process.stdin });
const emit = value => process.stdout.write(`${JSON.stringify(value)}\n`);
const message = (room, id, recovered = true, extra = {}) => ({ type: 'message', chatId: room,
  logId: id, authorId: id === '101' ? '123' : '222', authorName: 'fixture sender',
  chatTitle: 'fixture group', message: `synthetic ${room}/${id}`, messageType: 1,
  sentAt: Date.now() - 3000 + Number(id) * 10, recovered, isMine: id === '101', ...extra });

input.on('line', line => {
  let request;
  try { request = JSON.parse(line.trim()); } catch { return; }
  if (request.type === 'fixture_emit') { for (const event of request.events ?? []) emit(event); return; }
  if (request.type !== 'start') return;
  const mode = request.oauthToken?.replace(/^fixture:/, '');
  const cursors = request.catchUp ?? [];
  fs.appendFileSync(path.join(process.env.AGENT_MESSENGER_CONFIG_DIR, 'conversation-starts.jsonl'),
    `${JSON.stringify({ mode, cursors })}\n`);
  emit({ type: 'status', status: 'ready' });
  if (mode === 'hold') return;
  emit({ type: 'status', status: 'catchup_started' });
  for (const cursor of cursors) {
    const rows = mode === 'recover' ? ['100', '101', '102'] : mode === 'blocked' ? ['101'] : [];
    const selected = rows.filter(id => BigInt(id) > BigInt(cursor.afterLogId));
    for (const id of selected) emit(message(cursor.chatId, id));
    if (selected.length) emit({ type: 'catchup_progress', chatId: cursor.chatId,
      logId: selected[selected.length - 1], complete: true });
    emit({ type: 'conversation_synced', chatId: cursor.chatId, complete: true });
  }
  emit({ type: 'status', status: 'catchup_completed', partial: false });
});
input.on('close', () => process.stdin.destroy());
