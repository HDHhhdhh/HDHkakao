import { createInterface } from 'node:readline';

export function bridgeError(code) { return Object.assign(new Error(code), { code }); }

export async function withDeadline(task, timeoutMs, code = 'operation_timeout', signal) {
  if (signal?.aborted) throw bridgeError('cancelled');
  let timer;
  let onAbort;
  try {
    return await Promise.race([
      Promise.resolve().then(() => typeof task === 'function' ? task() : task),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(bridgeError(code)), timeoutMs);
        if (signal) {
          onAbort = () => reject(bridgeError('cancelled'));
          signal.addEventListener('abort', onAbort, { once: true });
        }
      }),
    ]);
  } finally {
    clearTimeout(timer);
    if (onAbort) signal.removeEventListener('abort', onAbort);
  }
}

const readers = new WeakMap();
function getReader(input) {
  if (readers.has(input)) return readers.get(input);
  const lines = [];
  let pending = null;
  let ended = false;
  let failure = null;
  let buffered = 0;
  const reader = createInterface({ input, crlfDelay: Infinity });
  const fail = (error) => { failure = error; pending?.reject(error); pending = null; };
  reader.on('line', (line) => {
    if (!line.trim()) return;
    if (line.length > 1024 * 1024 || lines.length >= 16 || buffered + line.length > 1024 * 1024) {
      fail(bridgeError('invalid_input')); reader.close(); return;
    }
    if (pending) { const waiter = pending; pending = null; waiter.resolve(line); }
    else { lines.push(line); buffered += line.length; }
  });
  reader.on('close', () => { ended = true; pending?.reject(bridgeError('input_closed')); pending = null; });
  const onError = () => fail(bridgeError('input_closed'));
  input.on('error', onError);
  const handle = {
    async next({ timeoutMs = 15000, signal } = {}) {
      if (failure) throw failure;
      if (lines.length) { const line = lines.shift(); buffered -= line.length; return line; }
      if (ended) throw bridgeError('input_closed');
      if (pending) throw bridgeError('invalid_input');
      let waiter;
      const waiting = new Promise((resolve, reject) => { waiter = { resolve, reject }; pending = waiter; });
      try { return await withDeadline(waiting, timeoutMs, 'input_timeout', signal); }
      finally { if (pending === waiter) pending = null; }
    },
    release() {
      reader.close(); input.removeListener('error', onError); input.pause?.(); input.destroy?.();
      lines.length = 0; buffered = 0; readers.delete(input);
    },
  };
  readers.set(input, handle);
  return handle;
}

// Keep a single reader for start + credential ACK; an early ACK cannot be lost.
export function readFirstJsonLine(input, options) { return getReader(input).next(options); }
export function releaseJsonInput(input) {
  const reader = readers.get(input);
  if (reader) reader.release(); else { input.pause?.(); input.destroy?.(); }
}
export function parseJsonLine(line, invalidCode = 'invalid_json') {
  // .NET Framework StandardInput can prepend a UTF-8 BOM when it constructs
  // its writer. Accept that transport preamble without changing JSON text.
  try { return JSON.parse(typeof line === 'string' && line.charCodeAt(0) === 0xfeff ? line.slice(1) : line); }
  catch { throw bridgeError(invalidCode); }
}

export function createNdjsonEmitter(output = process.stdout, { timeoutMs = 10000, maxQueuedBytes = 32 * 1024 * 1024 } = {}) {
  let queue = Promise.resolve();
  let queuedBytes = 0;
  let failure = null;
  let stopped = false;
  let resolveFailure;
  const failed = new Promise((resolve) => { resolveFailure = resolve; });
  const fail = (error) => {
    if (!failure) { failure = bridgeError(error?.code === 'output_backpressure' ? 'output_backpressure' : 'output_closed'); resolveFailure(failure); }
    return failure;
  };
  const onError = () => fail(bridgeError('output_closed'));
  output.on?.('error', onError);
  const emit = (event) => {
    if (stopped || failure) return Promise.resolve(false);
    const line = `${JSON.stringify(event)}\n`;
    const bytes = Buffer.byteLength(line);
    if (bytes + queuedBytes > maxQueuedBytes) { fail(bridgeError('output_backpressure')); return Promise.resolve(false); }
    queuedBytes += bytes;
    const writing = queue.then(async () => {
      if (failure) throw failure;
      await withDeadline(new Promise((resolve, reject) => {
        try { output.write(line, (error) => error ? reject(error) : resolve()); }
        catch (error) { reject(error); }
      }), timeoutMs, 'output_backpressure');
      return true;
    }).finally(() => { queuedBytes -= bytes; });
    queue = writing.catch((error) => { fail(error); });
    writing.catch(() => {});
    return writing;
  };
  emit.failed = failed;
  emit.flush = async () => { await withDeadline(queue, timeoutMs, 'output_backpressure'); if (failure) throw failure; };
  emit.close = () => { stopped = true; output.removeListener?.('error', onError); };
  return emit;
}
export function stopNdjsonEmitter(emitter) { emitter.close?.(); return emitter; }

const ERROR_MESSAGES = Object.freeze({
  bridge_error: '연결을 처리하지 못했습니다.', connection_error: '카카오톡 연결에 오류가 발생했습니다.',
  connection_failed: '카카오톡에 연결하지 못했습니다.', session_timeout: '카카오톡 연결 시간이 초과되었습니다.',
  login_failed: '카카오톡 로그인에 실패했습니다.', login_rejected: '카카오톡이 로그인을 거절했습니다.',
  invalid_access_token: '로그인 인증이 만료되었습니다.', not_authenticated: '다시 로그인해 주세요.',
  invalid_auth_request: '로그인 입력을 확인해 주세요.', invalid_credentials: '저장된 인증 정보를 확인해 주세요.',
  invalid_start_request: '연결 요청 형식이 올바르지 않습니다.', invalid_json: '요청 형식이 올바르지 않습니다.',
  invalid_input: '입력 크기 또는 형식이 올바르지 않습니다.', input_closed: '입력 연결이 종료되었습니다.',
  input_timeout: '입력 대기 시간이 초과되었습니다.', auth_failed: '기기 등록에 실패했습니다.',
  auth_timeout: '기기 등록 시간이 초과되었습니다. 다시 시도해 주세요.',
  cancelled: '작업이 취소되었습니다.', token_refresh_failed: '인증을 갱신하지 못했습니다.',
  token_refresh_timeout: '인증 갱신 시간이 초과되었습니다.',
  auth_lease_busy: '다른 앱에서 인증 중이에요. 잠시 후 다시 시도해 주세요.',
  auth_lease_invalid: '인증 갱신 조율을 확인하지 못했어요. 두 앱을 업데이트한 뒤 다시 연결해 주세요.',
  auth_identity_changed: '다른 앱에서 계정 또는 기기 정보가 바뀌었어요. 최신 정보로 다시 연결해 주세요.',
  credentials_not_saved: '갱신된 인증 정보 저장을 확인하지 못했습니다.',
  invalid_credentials_ack: '인증 정보 저장 응답이 올바르지 않습니다.',
  credentials_ack_timeout: '인증 정보 저장 확인 시간이 초과되었습니다.',
  catchup_failed: '일부 이전 메시지를 복구하지 못했습니다.', catchup_timeout: '이전 메시지 조회 시간이 초과되었습니다.',
  catchup_unavailable: '이 버전에서는 이전 메시지 복구를 사용할 수 없습니다.',
  invalid_catchup_request: '이전 메시지 복구 요청이 올바르지 않습니다.',
  catchup_invalid_page: '이전 메시지 페이지를 확인하지 못했습니다.',
  output_closed: '메시지 전달 연결이 종료되었습니다.', output_backpressure: '메시지 처리 대기 시간이 초과되었습니다.',
  event_queue_full: '메시지 처리 대기열이 가득 찼습니다.',
  adoption_buffer_full: '연결 중 메시지 대기열이 가득 찼습니다.', sdk_adapter_unavailable: '연결 호환 기능을 사용할 수 없습니다.',
  sync_cache_path_invalid: '연결 캐시 저장 위치를 확인하지 못했습니다.',
  operation_timeout: '작업 시간이 초과되었습니다.', client_closed: '연결이 종료되었습니다.',
});

// Codes and text are fixed application values, never arbitrary upstream text.
export function safeError(error, fallbackCode = 'bridge_error') {
  const candidate = error?.code;
  const fallback = Object.hasOwn(ERROR_MESSAGES, fallbackCode) ? fallbackCode : 'bridge_error';
  const code = typeof candidate === 'string' && Object.hasOwn(ERROR_MESSAGES, candidate) ? candidate : fallback;
  return { code, message: ERROR_MESSAGES[code] };
}
export function safeResultError(code, _message, fallbackCode = 'bridge_error') { return safeError({ code }, fallbackCode); }
