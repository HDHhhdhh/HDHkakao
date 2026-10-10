using System;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace Spoiler
{
    public sealed partial class LocoMessageSource
    {
        private string _processDeviceUuid;
        private bool _replyReady;
        private PendingReply _pendingReply;

        private sealed class PendingReply
        {
            public string RequestId;
            public string ChatId;
            public string Text;
            public string AccountId;
            public string DeviceUuid;
            public long Generation;
            public Process Process;
            public Timer Timeout;
            public bool WriteStarted;
            public TaskCompletionSource<ReplySendResult> Completion;
        }

        public bool CanReply
        {
            get
            {
                lock (_sync)
                    return !_disposed && !_stopping && _replyReady && _pendingReply == null
                        && _process != null && !HasExited(_process) && !_storageBlocked;
            }
        }

        public Task<ReplySendResult> SendReplyAsync(string chatId, string text)
        {
            PendingReply pending;
            lock (_sync)
            {
                if (!IsPositiveServerId(chatId) || string.IsNullOrWhiteSpace(text)
                    || text.Length > 4000 || Encoding.UTF8.GetByteCount(text) > 16384)
                    return CompletedReply(ReplySendStatus.Failed, "reply_invalid", chatId);
                if (!CanReply) return CompletedReply(ReplySendStatus.Failed, "reply_disconnected", chatId);
                pending = new PendingReply { RequestId = Guid.NewGuid().ToString("N"), ChatId = chatId, Text = text,
                    AccountId = _processAccountId, DeviceUuid = _processDeviceUuid,
                    Generation = _lifecycle.Generation, Process = _process,
                    Completion = new TaskCompletionSource<ReplySendResult>(TaskCreationOptions.RunContinuationsAsynchronously) };
                _pendingReply = pending;
                pending.Timeout = new Timer(ReplyTimedOut, pending, 25000, Timeout.Infinite);
            }
            ThreadPool.QueueUserWorkItem(delegate
            {
                // DPAPI and the cross-process writer lock can take seconds.
                // Keep both off the caller/UI thread and outside _sync so
                // Stop and availability checks never wait for this vault read.
                LocoCredentials latest = null;
                try { latest = _vault.Load(); } catch { }
                bool authorized;
                lock (_sync)
                {
                    authorized = ReferenceEquals(_pendingReply, pending) && ReferenceEquals(_process, pending.Process)
                        && _replyReady && _lifecycle.Allows(pending.Generation) && ReplyIdentityMatches(latest)
                        && _inbox != null && _inbox.LoadMessages().Any(item => item != null && item.ChatId == chatId);
                    if (!authorized && ReferenceEquals(_pendingReply, pending))
                        CompletePendingReplyLocked(ReplySendStatus.Failed, "reply_identity_changed");
                    if (authorized) pending.WriteStarted = true;
                }
                if (!authorized) return;
                try
                {
                    // Do not hold the lifecycle lock while a pipe write waits.
                    lock (pending.Process)
                        LocoBridgeLauncher.WriteUtf8Line(pending.Process, SerializeReplyRequest(pending, text));
                }
                catch
                {
                    lock (_sync)
                        if (ReferenceEquals(_pendingReply, pending))
                        {
                            _replyReady = false;
                            CompletePendingReplyLocked(ReplySendStatus.Unknown, "reply_outcome_unknown");
                        }
                }
            });
            return pending.Completion.Task;
        }

        private bool ReplyIdentityMatches(LocoCredentials credentials)
        {
            return credentials != null && credentials.IsUsable && credentials.UserId == _processAccountId
                && credentials.DeviceUuid == _processDeviceUuid && _processAccountId == _inboxAccountId;
        }

        private static bool IsPositiveServerId(string value)
        {
            long parsed;
            return value != null && long.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out parsed)
                && parsed > 0 && parsed.ToString(CultureInfo.InvariantCulture) == value;
        }

        private static Task<ReplySendResult> CompletedReply(ReplySendStatus status, string code, string chatId)
        {
            return Task.FromResult(new ReplySendResult(status, code, ReplyDetail(code), null, chatId, null));
        }

        private static string SerializeReplyRequest(PendingReply reply, string text)
        {
            return "{\"type\":\"send_reply\",\"request_id\":\"" + ReplyEscape(reply.RequestId)
                + "\",\"user_id\":\"" + ReplyEscape(reply.AccountId) + "\",\"device_uuid\":\""
                + ReplyEscape(reply.DeviceUuid) + "\",\"chat_id\":\"" + ReplyEscape(reply.ChatId)
                + "\",\"text\":\"" + ReplyEscape(text) + "\"}";
        }

        private static string ReplyEscape(string text)
        {
            StringBuilder value = new StringBuilder();
            foreach (char character in text ?? string.Empty)
            {
                if (character == '"' || character == '\\') value.Append('\\').Append(character);
                else if (character < 32) value.Append("\\u").Append(((int)character).ToString("x4", CultureInfo.InvariantCulture));
                else value.Append(character);
            }
            return value.ToString();
        }

        private void UpdateReplyConnectionLocked(LocoBridgeEvent frame)
        {
            string status = (frame.Status ?? frame.State ?? string.Empty).ToLowerInvariant();
            string type = (frame.Type ?? string.Empty).ToLowerInvariant();
            if (type == "status" && status == "ready") _replyReady = true;
            else if ((type == "status" && (status == "disconnected" || status == "error" || status == "stopped"))
                || type == "disconnected" || type == "kicked" || type == "error" || type == "auth-error")
            {
                _replyReady = false;
                CompletePendingReplyLocked(ReplySendStatus.Unknown, "reply_outcome_unknown");
            }
        }

        private void ConsumeReplyResultLocked(Process process, LocoBridgeEvent frame, LocoCredentials latest)
        {
            PendingReply pending = _pendingReply;
            if (pending == null || frame.RequestId != pending.RequestId || !ReferenceEquals(process, pending.Process)) return;
            if (!_lifecycle.Allows(pending.Generation) || frame.ChatId != pending.ChatId
                || frame.ReplyUserId != pending.AccountId || frame.DeviceUuid != pending.DeviceUuid
                || !ReplyIdentityMatches(latest))
            {
                _replyReady = false;
                CompletePendingReplyLocked(ReplySendStatus.Unknown, "reply_outcome_unknown");
                return;
            }
            // The message frame is consumed first through the normal durable
            // inbox path. An ACK without that durable body cannot clear a draft.
            long sentAt;
            if (frame.Status == "sent" && frame.Code == "reply_sent" && IsPositiveServerId(frame.LogId)
                && long.TryParse(frame.SentAt, NumberStyles.None, CultureInfo.InvariantCulture, out sentAt) && sentAt > 0
                && _inbox != null && _inbox.PersistenceAvailable
                && _inbox.LoadMessages().Any(item => item.Id == frame.LogId && item.ChatId == pending.ChatId
                    && item.IsMine && item.Message == pending.Text))
                CompletePendingReplyLocked(ReplySendStatus.Sent, "reply_sent", frame.LogId);
            else if (frame.Status == "failed" && (frame.Code == "reply_invalid" || frame.Code == "reply_identity_changed"
                || frame.Code == "reply_disconnected" || frame.Code == "reply_rejected"))
                CompletePendingReplyLocked(ReplySendStatus.Failed, frame.Code);
            else
            {
                _replyReady = false;
                CompletePendingReplyLocked(ReplySendStatus.Unknown, "reply_outcome_unknown");
            }
        }

        private void ReplyTimedOut(object state)
        {
            PendingReply pending = state as PendingReply;
            lock (_sync)
            {
                if (!ReferenceEquals(_pendingReply, pending)) return;
                _replyReady = false;
                CompletePendingReplyLocked(pending.WriteStarted ? ReplySendStatus.Unknown : ReplySendStatus.Failed,
                    pending.WriteStarted ? "reply_outcome_unknown" : "reply_disconnected");
            }
            SafeKill(pending.Process); // Stop a blocked pipe or unfinished WRITE; never replay it.
        }

        private void CompletePendingReplyLocked(ReplySendStatus status, string code, string logId = null)
        {
            PendingReply pending = _pendingReply;
            if (pending == null) return;
            _pendingReply = null;
            if (pending.Timeout != null) pending.Timeout.Dispose();
            pending.Completion.TrySetResult(new ReplySendResult(status, code, ReplyDetail(code),
                pending.RequestId, pending.ChatId, logId));
        }

        private static string ReplyDetail(string code)
        {
            switch (code)
            {
                case "reply_sent": return "서버가 전송을 확인했어요.";
                case "reply_invalid": return "대화방 또는 메시지 내용을 확인해 주세요. 최대 4,000자까지 보낼 수 있어요.";
                case "reply_identity_changed": return "계정 또는 대화방 정보를 확인해 주세요. 메시지는 보내지 않았어요.";
                case "reply_disconnected": return "연결 후 다시 시도해 주세요. 메시지는 보내지 않았어요.";
                case "reply_rejected": return "서버가 전송을 거절했어요. 입력한 내용은 유지됩니다.";
                default: return "전송 결과를 확인하지 못했어요. 중복 전송을 피하려면 카카오톡에서 확인해 주세요.";
            }
        }
    }
}
