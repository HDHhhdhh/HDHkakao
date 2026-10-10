using System;
using System.Linq;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Threading;

namespace Spoiler
{
    // Persistence and replay state stay together; publication follows a durable commit.
    public sealed partial class LocoMessageSource
    {
        public string CurrentAccountId { get { lock (_sync) { return _inboxAccountId; } } }
        public IList<NotificationPreview> LoadStoredMessages()
        {
            LocoCredentials credentials = _vault.Load();
            string owner = credentials != null && credentials.IsUsable ? credentials.UserId.Trim() : null;
            bool stopPrevious;
            lock (_sync) { stopPrevious = _process != null && _processAccountId != owner; }
            if (stopPrevious) Stop();
            lock (_sync)
            {
                ThrowIfDisposed();
                if (credentials == null || !credentials.IsUsable)
                {
                    if (_inbox != null) _inbox.Dispose();
                    _inbox = null;
                    _inboxAccountId = null;
                    return new List<NotificationPreview>();
                }
                return PrepareInbox(credentials.UserId).LoadMessages();
            }
        }
        public bool ClearHistory()
        {
            bool restart;
            lock (_sync) { restart = _lifecycle.DesiredRunning; }
            Stop(); // invalidates queued bridge events before erasing.
            bool cleared;
            lock (_sync)
            {
                cleared = _inbox == null || _inbox.ClearHistory();
                cleared = new NotificationStore(_vault.DataDirectory).Clear() && cleared;
            }
            if (restart && !_disposed) Start();
            return cleared;
        }
        private void Maintain(object ignored)
        {
            Process stalled = null;
            lock (_sync)
            {
                if (_disposed || !_lifecycle.DesiredRunning) return;
                if (_process != null && !_storageBlocked
                    && DateTime.UtcNow - _lastBridgeActivityUtc > TimeSpan.FromSeconds(120))
                    stalled = _process;
                if (_inbox != null && DateTime.UtcNow - _lastPruneUtc > TimeSpan.FromMinutes(5))
                {
                    _lastPruneUtc = DateTime.UtcNow;
                    if (!_inbox.Prune()) RaiseState(CaptureState.Error, "저장 공간을 확인해 주세요. 기록 정리를 완료하지 못했어요.");
                }
            }
            if (stalled != null)
            {
                RaiseState(CaptureState.Error, "보조기기 응답이 멈춰 연결을 다시 시작해요.");
                SafeKill(stalled);
            }
        }
        private InboxStore PrepareInbox(string accountId)
        {
            lock (_sync)
            {
                if (_disposed || string.IsNullOrWhiteSpace(accountId)) return null;
                string owner = accountId.Trim();
                if (_inbox != null && _inboxAccountId == owner && !_inbox.HasLoadError) return _inbox;
                if (_inbox != null) _inbox.Dispose();
                _inbox = new InboxStore(_vault.DataDirectory, owner);
                _inboxAccountId = owner;
                _catchUpProtectedChats.Clear();
                return _inbox;
            }
        }
        private static IList<LocoCatchUpCursor> GetConversationSyncCursors(InboxStore checkpoints)
        {
            List<LocoCatchUpCursor> result = new List<LocoCatchUpCursor>();
            if (checkpoints == null)
            {
                return result;
            }

            try
            {
                foreach (MessageCatchUpCursor cursor in checkpoints.GetConversationSyncCursors(512))
                {
                    if (cursor == null || string.IsNullOrWhiteSpace(cursor.ChatId)
                        || string.IsNullOrWhiteSpace(cursor.LogId))
                    {
                        continue;
                    }
                    result.Add(new LocoCatchUpCursor
                    {
                        ChatId = cursor.ChatId,
                        AfterLogId = cursor.LogId,
                        Limit = 50
                    });
                }
            }
            catch
            {
                result.Clear();
            }
            return result;
        }
        private void SetCatchUpProtection(IList<LocoCatchUpCursor> cursors)
        {
            lock (_sync)
            {
                _catchUpProtectedChats.Clear();
                foreach (LocoCatchUpCursor cursor in cursors ?? new LocoCatchUpCursor[0])
                {
                    if (cursor != null && !string.IsNullOrWhiteSpace(cursor.ChatId))
                    {
                        _catchUpProtectedChats.Add(cursor.ChatId.Trim());
                    }
                }
            }
        }
        private bool IsCatchUpProtected(string chatId)
        {
            if (string.IsNullOrWhiteSpace(chatId))
            {
                return false;
            }
            lock (_sync)
            {
                return _catchUpProtectedChats.Contains(chatId.Trim());
            }
        }
        private void HandleStatus(LocoBridgeEvent bridgeEvent)
        {
            string status = (bridgeEvent.Status ?? bridgeEvent.State ?? bridgeEvent.Detail ?? string.Empty).ToLowerInvariant();
            if (status.Contains("catchup_started"))
            {
                RaiseState(CaptureState.Starting, "기록된 대화방의 연결 공백을 읽음 신호 없이 복구하는 중이에요");
            }
            else if (status.Contains("catchup_completed"))
            {
                // Each room is released only by its own ordered completion.
                RaiseState(CaptureState.Watching,
                    bridgeEvent.Partial
                        ? "기록된 대화방을 일부 복구했어요 · 다음 확인 때 이어서 복구해요"
                        : "기록된 대화방의 누락 메시지 확인 완료 · 읽음 신호는 보내지 않았어요");
            }
            else if (status.Contains("catchup_failed"))
            {
                RaiseState(CaptureState.Watching, "실시간 수신은 계속돼요 · 기록된 대화방의 연결 공백은 복구하지 못했어요");
            }
            else if (status.Contains("credentials_required") || status.Contains("register_required") || status.Contains("auth_required"))
            {
                RaiseState(CaptureState.Error, "먼저 태블릿 보조기기를 등록해 주세요");
            }
            else if (status.Contains("error") || status.Contains("disconnect") || status.Contains("kick"))
            {
                RaiseState(CaptureState.Error, bridgeEvent.DetailOrError());
            }
            else if (status.Contains("connected") || status.Contains("ready") || status.Contains("watch") || status.Contains("token_refreshed"))
            {
                lock (_sync)
                {
                    _reconnectAttempt = 0;
                }
                RaiseState(CaptureState.Watching,
                    CheckpointPersistenceAvailable()
                        ? "카카오톡 메시지를 기다리는 중 · 읽음 신호는 보내지 않아요"
                        : "메시지 수신 중 · 재접속 복구 기록은 이번 실행에만 유지돼요");
            }
            else
            {
                RaiseState(CaptureState.Starting, "카카오톡 보조기기에 연결하고 있어요");
            }
        }
        private void ConsumeMessageBatch(Process process, IList<LocoBridgeEvent> events)
        {
            List<InboxMessageWrite> messages = null;
            while (true)
            {
                lock (_sync)
                {
                    if (!IsCurrentProcess(process)) return;
                    _lastBridgeActivityUtc = DateTime.UtcNow;
                    if (messages == null)
                    {
                        messages = new List<InboxMessageWrite>(events.Count);
                        Dictionary<string, string> names = new Dictionary<string, string>(StringComparer.Ordinal);
                        foreach (LocoBridgeEvent bridgeEvent in events)
                        {
                            NotificationPreview preview = CreatePreview(bridgeEvent, names);
                            messages.Add(new InboxMessageWrite { Preview = preview, ChatId = bridgeEvent.ChatId,
                                AuthorId = bridgeEvent.AuthorId,
                                // A WRITE receipt can overtake an older push
                                // still resolving metadata. Store its body,
                                // but never skip history based on that ACK.
                                AdvanceCursor = !bridgeEvent.ReplyAck && (bridgeEvent.Recovered || !IsCatchUpProtected(bridgeEvent.ChatId)) });
                        }
                    }
                    IList<InboxCommitResult> committed = _inbox == null || _inbox.HasLoadError
                        ? null : _inbox.CommitMessages(messages);
                    _storageBlocked = committed == null || !committed[0].Saved;
                    if (!_storageBlocked)
                    {
                        // Publication remains in pipe order, after the whole
                        // burst has a durable body/cursor/replay transaction.
                        for (int index = 0; index < messages.Count && IsCurrentProcess(process); index++)
                        {
                            if (!committed[index].IsNew && !committed[index].MetadataChanged) continue;
                            NotificationPreview savedPreview = committed[index].SavedPreview;
                            if (savedPreview == null) continue;
                            PublishSavedPreviewLocked(process, savedPreview);
                        }
                        // Newly learned exact room/member proof can restore
                        // retained legacy rows outside this incoming batch.
                        // Publish those durable metadata changes through the
                        // same account/process gate without resetting drafts.
                        foreach (NotificationPreview restored in committed[0].MetadataUpdates ?? new NotificationPreview[0])
                        {
                            if (!IsCurrentProcess(process)) break;
                            if (restored != null) PublishSavedPreviewLocked(process, restored);
                        }
                        return;
                    }
                    RaiseState(CaptureState.Error, "메시지를 저장하지 못해 수신 처리를 잠시 멈췄어요. 저장 공간을 확인해 주세요.");
                }
                Thread.Sleep(1000);
            }
        }
        private void PublishSavedPreviewLocked(Process process, NotificationPreview savedPreview)
        {
            if (!IsCurrentProcess(process) || savedPreview.AccountId != _inboxAccountId) return;
            EventHandler<NotificationPreview> handler = PreviewCaptured;
            bool publicationFailed = false;
            if (handler != null)
            {
                foreach (EventHandler<NotificationPreview> subscriber in handler.GetInvocationList())
                {
                    try { subscriber(this, savedPreview); }
                    catch { publicationFailed = true; }
                }
            }
            if (IsCurrentProcess(process))
                RaiseState(publicationFailed ? CaptureState.Error : CaptureState.Captured,
                    publicationFailed ? "메시지는 저장됐지만 화면 알림 일부를 전달하지 못했어요."
                        : "새 메시지를 받았어요 · 읽음 신호는 보내지 않았어요");
        }
        private void PublishMetadataPreviewLocked(Process process, NotificationPreview savedPreview)
        {
            if (!IsCurrentProcess(process) || savedPreview.AccountId != _inboxAccountId) return;
            EventHandler<NotificationPreview> handler = PreviewCaptured;
            if (handler == null) return;
            foreach (EventHandler<NotificationPreview> subscriber in handler.GetInvocationList())
            {
                if (!IsCurrentProcess(process)) return;
                try { subscriber(this, savedPreview); }
                catch { RaiseState(CaptureState.Error, "대화명은 저장됐지만 화면 일부를 갱신하지 못했어요."); }
            }
        }
        private NotificationPreview CreatePreview(LocoBridgeEvent bridgeEvent, IDictionary<string, string> names)
        {
            string nameKey = (bridgeEvent.ChatId ?? string.Empty).Trim() + "\u001f" + (bridgeEvent.AuthorId ?? string.Empty).Trim();
            string batchName;
            names.TryGetValue(nameKey, out batchName);
            string suppliedSender = FirstNonEmpty(bridgeEvent.Sender, bridgeEvent.AuthorName, null);
            string sender = FirstNonEmpty(suppliedSender,
                FirstNonEmpty(batchName, _inbox == null ? null : _inbox.ResolveDisplayName(bridgeEvent.ChatId, bridgeEvent.AuthorId), null),
                bridgeEvent.ChatTitle, "카카오톡");
            StringBuilder nameBuilder = new StringBuilder();
            foreach (char character in sender) if (!char.IsControl(character)) nameBuilder.Append(character);
            string cleanName = nameBuilder.ToString().Trim();
            if (!string.IsNullOrWhiteSpace(bridgeEvent.ChatId) && bridgeEvent.ChatId.Trim().Length <= 256
                && !string.IsNullOrWhiteSpace(bridgeEvent.AuthorId) && bridgeEvent.AuthorId.Trim().Length <= 256
                && cleanName.Length > 0 && cleanName.Length <= 200) names[nameKey] = cleanName;
            int messageType = ParseMessageType(bridgeEvent.MessageType);
            string message = MessageForDisplay(FirstNonEmpty(bridgeEvent.Message, bridgeEvent.Text, string.Empty), messageType);
            if (string.IsNullOrWhiteSpace(message)) message = "[메시지]";
            string id = FirstNonEmpty(bridgeEvent.LogId, bridgeEvent.MessageId, bridgeEvent.Id, null);
            // Explicit own-message receipts have no title. Carry forward only
            // an actual title already stored for this same server room.
            string roomTitle = RoomMetadataContract.ValidTitle(bridgeEvent.ChatTitle) ? bridgeEvent.ChatTitle : null;
            string titleSource = RoomMetadataContract.Priority(bridgeEvent.TitleSource) > 0 ? bridgeEvent.TitleSource : "server_title";
            InboxRoomMetadata roomMetadata = _inbox == null ? null : _inbox.ResolveRoomMetadata(bridgeEvent.ChatId);
            if (roomMetadata != null)
            {
                roomTitle = roomMetadata.ChatTitle;
                titleSource = roomMetadata.TitleSource;
            }
            if (roomTitle == null && _inbox != null)
            {
                NotificationPreview previousTitle = _inbox.LoadMessages().Where(item => item.ChatId == bridgeEvent.ChatId
                    && !string.IsNullOrWhiteSpace(item.RoomTitle))
                    .OrderByDescending(item => item.ReceivedAt).FirstOrDefault();
                if (previousTitle != null)
                {
                    roomTitle = previousTitle.RoomTitle;
                    titleSource = previousTitle.RoomTitleSource;
                }
            }
            return new NotificationPreview
            {
                Id = id ?? Guid.NewGuid().ToString("N"),
                AccountId = _inboxAccountId,
                Sender = sender,
                Message = message,
                ReceivedAt = ParseTimestamp(bridgeEvent.SentAt, bridgeEvent.Timestamp, bridgeEvent.ReceivedAt),
                Source = "KakaoTalk tablet companion",
                GroupKey = NotificationGrouping.CreateServerGroupKey(bridgeEvent.ChatId, bridgeEvent.AuthorId),
                ChatId = string.IsNullOrWhiteSpace(bridgeEvent.ChatId) ? null : bridgeEvent.ChatId.Trim(),
                RoomTitle = roomTitle,
                RoomTitleSource = titleSource,
                AuthorName = sender,
                IsMine = bridgeEvent.IsMine,
                MessageType = messageType
            };
        }
        private bool CheckpointPersistenceAvailable()
        {
            lock (_sync) { return _inbox != null && _inbox.PersistenceAvailable; }
        }
        internal static string MessageForDisplay(string message, int messageType)
        {
            if (!string.IsNullOrWhiteSpace(message))
            {
                return message;
            }

            switch (messageType)
            {
                case 2: return "[사진]";
                case 3: return "[동영상]";
                case 5: return "[음성 메시지]";
                case 18: return "[파일]";
                case 26: return "[답장]";
                case 27: return "[사진 여러 장]";
                default: return messageType > 0 ? "[첨부 메시지]" : string.Empty;
            }
        }
        private static int ParseMessageType(string value)
        {
            int parsed;
            return int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed)
                && parsed >= 0
                ? parsed
                : 0;
        }
        private static string FirstNonEmpty(string first, string second, string fallback)
        {
            return !string.IsNullOrWhiteSpace(first)
                ? first
                : (!string.IsNullOrWhiteSpace(second) ? second : fallback);
        }
        private static string FirstNonEmpty(string first, string second, string third, string fallback)
        {
            if (!string.IsNullOrWhiteSpace(first)) return first;
            if (!string.IsNullOrWhiteSpace(second)) return second;
            if (!string.IsNullOrWhiteSpace(third)) return third;
            return fallback;
        }
        private static DateTime ParseTimestamp(string sentAt, string timestamp, string receivedAt)
        {
            string value = FirstNonEmpty(sentAt, timestamp, receivedAt, null);
            if (string.IsNullOrWhiteSpace(value))
            {
                return DateTime.Now;
            }

            long integer;
            if (long.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out integer))
            {
                try
                {
                    if (integer < 100000000000L)
                    {
                        integer *= 1000L;
                    }
                    return new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)
                        .AddMilliseconds(integer)
                        .ToLocalTime();
                }
                catch
                {
                }
            }

            DateTime parsed;
            if (DateTime.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out parsed))
            {
                return parsed.ToLocalTime();
            }

            return DateTime.Now;
        }
    }
}
