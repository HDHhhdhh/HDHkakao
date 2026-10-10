// Only classify session acquisition, before any WRITE/SHIP/POST is issued.
// Retry decisions use structured codes, never exception messages or payloads.
const TRANSIENT = new Set([
  "login_failed", "network_error", "connection_closed", "transport_closed",
  "process_disconnected", "timeout", "request_timeout", "client_closed",
  "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "EPIPE", "EHOSTUNREACH",
  "ENETUNREACH", "EAI_AGAIN", "UND_ERR_CONNECT_TIMEOUT", "UND_ERR_SOCKET",
]);
const TERMINAL = new Set([
  "invalid_access_token", "token_expired", "oauth_expired", "login_rejected",
  "refresh_rejected", "refresh_unavailable", "refresh_http_error",
  "not_authenticated", "missing_token", "missing_user_id", "credentials_missing",
  "credentials_invalid", "credential_save_failed", "rate_limited", "server_rejected",
  "EACCES", "EPERM", "ENOENT", "ENOSPC",
]);

export function sessionAcquireError(cause) {
  const error = new Error("Direct session acquisition failed before sending");
  error.code = "session_acquire_failed";
  error.sendPhase = "prewrite";
  let transient = false;
  let terminal = false;
  let causeCode = "unclassified_session_error";
  const visited = new Set();
  for (let item = cause, depth = 0; item && depth < 6 && !visited.has(item); item = item.cause, depth += 1) {
    visited.add(item);
    const code = typeof item.code === "string" ? item.code : "";
    if (TRANSIENT.has(code)) {
      transient = true;
      if (!terminal) causeCode = code;
    }
    if (TERMINAL.has(code)) { terminal = true; causeCode = code; }
    const status = Number.isSafeInteger(item.serverStatus) ? item.serverStatus : item.status_code;
    if (Number.isSafeInteger(status) && status !== 0) {
      terminal = true;
      error.acquisitionCauseStatus = status;
    }
  }
  error.acquisitionCauseCode = causeCode;
  error.retryablePrewrite = transient && !terminal;
  return error;
}
