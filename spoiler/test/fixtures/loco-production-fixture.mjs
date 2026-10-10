// Offline-only synthetic bridge: no SDK, sockets, or real credentials.
import readline from 'node:readline';
const input = readline.createInterface({ input: process.stdin });
const timers = new Set();
const emit = value => process.stdout.write(`${JSON.stringify(value)}\n`);
const later = (callback, ms) => { const timer = setTimeout(callback, ms); timers.add(timer); };
const message = (room, id, recovered = false) => emit({ type: 'message', chat_id: room,
  log_id: id, author_id: 'synthetic-author', author_name: 'fixture sender',
  message: `synthetic ${room}/${id}`, sent_at: Date.now(), recovered });
input.on('line', line => {
  let start;
  try { start = JSON.parse(line.trim()); } catch { return; }
  if (start.type !== 'start') return;
  emit({ type: 'status', status: 'connected' });
  if (start.oauthToken === 'fixture:auth-lease') {
    emit({ type: 'auth_lease_request', request_id: 'fixture-auth', credentials: {
      oauth_token: start.oauthToken, refresh_token: start.refreshToken, user_id: start.userId,
      device_uuid: start.deviceUuid, device_type: 'tablet' } });
  } else if (start.oauthToken === 'fixture:catchup') {
    message('room-a', '200');
    message('room-a', '200', true);
    message('room-b', '200');
    emit({ type: 'catchup_progress', chat_id: 'room-a', log_id: '200', complete: true });
    emit({ type: 'catchup_progress', chat_id: 'room-b', log_id: '100', complete: false });
    message('room-a', '300');
    message('room-b', '300');
    emit({ type: 'status', status: 'catchup_completed', partial: true });
  } else if (start.oauthToken === 'fixture:message') {
    message('room-a', '200');
  } else if (start.oauthToken === 'fixture:burst' || start.oauthToken === 'fixture:burst-exit') {
    const burstAt = Date.now() - 120;
    for (let index = 0; index < 120; index++) emit({ type: 'message', chat_id: 'burst-room',
      log_id: String(1000 + index), author_id: 'synthetic-author',
      ...(index === 0 ? { author_name: 'burst sender' } : {}), chat_title: 'fallback room title',
      message: `synthetic burst/${index}`, sent_at: burstAt + index });
    if (start.oauthToken === 'fixture:burst-exit') {
      input.close(); process.stdin.destroy();
      process.stdout.write('', () => { process.exitCode = 1; });
    }
  } else if (start.oauthToken === 'fixture:sparse') {
    message('sparse-room', '1000');
    later(() => message('sparse-room', '1001'), 400);
  } else if (start.oauthToken === 'fixture:reconnect') {
    message('room-a', String(process.pid));
    later(() => {
      emit({ type: 'disconnected', code: 'fixture_disconnect' });
      input.close(); process.stdin.destroy();
      process.stdout.write('', () => { process.exitCode = 1; });
    }, 150);
  }
});
input.on('close', () => { for (const timer of timers) clearTimeout(timer); process.stdin.destroy(); });
