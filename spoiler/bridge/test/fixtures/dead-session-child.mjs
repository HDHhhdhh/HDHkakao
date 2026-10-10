// Exercise actual pinned client.connect/ensureSession and listener. Only the
// network login method is replaced; derivative SDK state is isolated in temp.
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, sep } from 'node:path';
const fixtureRoot = mkdtempSync(join(tmpdir(), 'SpoilerDeadSession-'));
process.env.AGENT_MESSENGER_CONFIG_DIR = join(fixtureRoot, 'bridge-state');
mkdirSync(process.env.AGENT_MESSENGER_CONFIG_DIR);
const { LocoSession } = await import('../../node_modules/agent-messenger/dist/src/platforms/kakaotalk/protocol/session.js');
const { runBridge } = await import('../../src/bridge.mjs');
const mode = process.argv[2];
LocoSession.prototype.login = async function () {
  const socket = { destroyed: false, readable: true, writable: true, readableEnded: false, writableEnded: false, connecting: false };
  this.connection = { socket, close() { if (this.socket) this.socket.destroyed = true; this.socket = null; } };
  this.pushHandler?.({ method: 'MSG', body: { chatId: '7', chatLog: { logId: '101', authorId: 2, type: 1, message: 'fixture-buffered', sendAt: 101 } } });
  if (mode === 'destroyed') { socket.destroyed = true; this.closeHandler?.(); }
  if (mode === 'null') { this.connection.socket = null; this.closeHandler?.(); }
  return { chatDatas: [] };
};
try { process.exitCode = await runBridge({ limits: { heartbeatMs: 20 } }); }
finally {
  const validated = resolve(fixtureRoot);
  const allowedParent = resolve(tmpdir()) + sep;
  if (validated.startsWith(allowedParent) && validated.slice(allowedParent.length).startsWith('SpoilerDeadSession-')) rmSync(validated, { recursive: true, force: true });
}
