using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;

namespace Spoiler
{
    // A commit owns the message body and its replay/catch-up state together.
    // No accepted message or cursor becomes visible until the atomic file
    // replacement succeeds. The old unowned history/checkpoint is not read.
    public sealed class InboxStore : IDisposable
    {
        public const int MaxMessages = 1000;
        internal const int MaxCommitBatch = 50;
        private const int MaxSeen = 2048;
        private const int MaxCursors = 512;
        private const int MaxNames = 1024;
        internal const int MaxRoomNames = 512;
        private static readonly byte[] Entropy = Encoding.UTF8.GetBytes("Spoiler.AccountInbox.v1");
        private readonly object _gate = new object();
        private readonly string _accountId;
        private readonly string _statePath;
        private readonly Func<DateTime> _utcNow;
        private InboxDocument _document;
        private bool _available;
        private bool _loadError;
        private bool _needsPrune;
        private bool _disposed;
        private int _snapshotWriteCount;

        public InboxStore(string dataDirectory, string accountId)
            : this(dataDirectory, accountId, () => DateTime.UtcNow) { }

        internal InboxStore(string dataDirectory, string accountId, Func<DateTime> utcNow)
        {
            _accountId = Identity(accountId);
            if (_accountId == null) throw new ArgumentException("An account ID is required.", "accountId");
            if (string.IsNullOrWhiteSpace(dataDirectory))
            {
                dataDirectory = Environment.GetEnvironmentVariable("SPOILER_DATA_DIR");
                if (string.IsNullOrWhiteSpace(dataDirectory))
                    dataDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Spoiler");
            }
            _statePath = Path.Combine(Path.GetFullPath(dataDirectory), "inbox-" + Digest("account-inbox:v1\u001f" + _accountId) + ".dat");
            _utcNow = utcNow ?? (() => DateTime.UtcNow);
            _document = EmptyDocument();
            _available = true;
            Load();
        }

        public string AccountId { get { return _accountId; } }
        public string StatePath { get { return _statePath; } }
        public bool PersistenceAvailable { get { lock (_gate) { return _available && !_loadError && !_disposed; } } }
        public bool HasLoadError { get { lock (_gate) { return _loadError; } } }
        internal int SnapshotWriteCount { get { lock (_gate) { return _snapshotWriteCount; } } }

        public IList<NotificationPreview> LoadMessages()
        {
            lock (_gate)
            {
                ThrowIfDisposed();
                return RetainMessagesAt(_document.Messages, Now()).Select(ClonePreview).ToList();
            }
        }

        public InboxCommitResult CommitMessage(NotificationPreview preview, string chatId, string authorId, bool advanceCursor)
        {
            return CommitMessages(new[] { new InboxMessageWrite { Preview = preview, ChatId = chatId,
                AuthorId = authorId, AdvanceCursor = advanceCursor } })[0];
        }

        // The body, replay identity, names, and cursor of a bounded burst are
        // one transaction. Failed persistence leaves every in-memory record
        // untouched, so retrying the same burst cannot skip or duplicate it.
        internal IList<InboxCommitResult> CommitMessages(IList<InboxMessageWrite> messages)
        {
            if (messages == null) throw new ArgumentNullException("messages");
            if (messages.Count == 0 || messages.Count > MaxCommitBatch) throw new ArgumentOutOfRangeException("messages");
            if (messages.Any(item => item == null || item.Preview == null)) throw new ArgumentNullException("preview");
            lock (_gate)
            {
                ThrowIfDisposed();
                if (_loadError || messages.Any(item => (!string.IsNullOrEmpty(item.Preview.AccountId) && item.Preview.AccountId != _accountId)
                    || !MatchesAuthoritativeRoom(item.Preview, item.ChatId)))
                    return messages.Select(item => new InboxCommitResult { IsNew = false, Saved = false }).ToList();

                DateTime now = Now();
                InboxDocument next = CopyDocument();
                Trim(next, now);
                HashSet<string> seen = new HashSet<string>(next.Seen.Select(item => item.Key), StringComparer.Ordinal);
                List<InboxCommitResult> results = new List<InboxCommitResult>(messages.Count);
                List<NotificationPreview> previousRows = new List<NotificationPreview>(messages.Count);
                List<NotificationPreview> savedRows = new List<NotificationPreview>(messages.Count);
                foreach (InboxMessageWrite item in messages)
                {
                    NotificationPreview preview = item.Preview;
                    string room = Identity(item.ChatId);
                    string logId = Identity(preview.Id);
                    string key = MessageKey(room, logId, preview);
                    bool isNew = seen.Add(key);
                    NotificationPreview stored = null;
                    NotificationPreview previous = null;
                    if (isNew)
                    {
                        next.Seen.Add(new SeenRecord { Key = key, ChatId = room, LogId = logId, AtUtc = now });
                        stored = ClonePreview(preview);
                        stored.ChatId = CanonicalServerRoom(room) ?? room;
                        ApplyRoomMetadata(next, stored);
                        next.Messages.Add(stored);
                    }
                    else
                    {
                        // Replay bodies never replace the original text. A
                        // metadata upgrade needs the same room/log/group and
                        // byte-identical body; cleared tombstones have no row.
                        stored = next.Messages.FirstOrDefault(existing => existing.Id == preview.Id
                            && !string.IsNullOrWhiteSpace(existing.GroupKey) && existing.GroupKey == preview.GroupKey
                            && string.Equals(existing.Message, preview.Message, StringComparison.Ordinal)
                            && (SameRoom(existing.ChatId, room) || (string.IsNullOrWhiteSpace(existing.ChatId)
                                && existing.GroupKey == NotificationGrouping.CreateServerGroupKey(room, item.AuthorId))));
                        if (stored != null)
                        {
                            previous = ClonePreview(stored);
                            stored.ChatId = CanonicalServerRoom(room) ?? room;
                            if (RoomMetadataContract.ValidTitle(preview.RoomTitle)
                                && TitlePriority(preview.RoomTitleSource) >= TitlePriority(stored.RoomTitleSource))
                            {
                                stored.RoomTitle = preview.RoomTitle;
                                stored.RoomTitleSource = preview.RoomTitleSource;
                            }
                            if (!string.IsNullOrWhiteSpace(preview.AuthorName)) stored.AuthorName = preview.AuthorName;
                            stored.IsMine = stored.GroupKey == NotificationGrouping.CreateServerGroupKey(room, item.AuthorId)
                                ? SameMember(item.AuthorId, _accountId) : preview.IsMine;
                            ApplyRoomMetadata(next, stored);
                        }
                    }

                    // Recovered duplicates can advance a protected cursor
                    // only with the body's durable replay identity.
                    if (item.AdvanceCursor && room != null && IsDecimal(logId)) Advance(next, room, logId, now);
                    string member = Identity(item.AuthorId);
                    string name = DisplayName(preview.Sender);
                    if (room != null && member != null && name != null)
                    {
                        NameRecord known = next.Names.FirstOrDefault(record => record.ChatId == room && record.AuthorId == member);
                        if (known == null)
                            next.Names.Add(new NameRecord { ChatId = room, AuthorId = member, Name = name, AtUtc = now });
                        else { known.Name = name; known.AtUtc = now; }
                    }
                    results.Add(new InboxCommitResult { IsNew = isNew });
                    previousRows.Add(previous);
                    savedRows.Add(stored);
                }
                Trim(next, now);
                RestoreLegacyRooms(next);
                foreach (NotificationPreview row in next.Messages) ApplyRoomMetadata(next, row);
                // New member evidence can also restore older rows outside
                // this input burst. Publish those upgrades after the same
                // durable transaction, without republishing its input rows.
                List<NotificationPreview> metadataUpdates = next.Messages.Where(row => !savedRows.Contains(row))
                    .Where(row => _document.Messages.Any(previous => previous.Id == row.Id
                        && previous.GroupKey == row.GroupKey && previous.Message == row.Message
                        && MetadataDiffers(previous, row))).Select(ClonePreview).ToList();
                bool saved = Save(next);
                for (int index = 0; index < results.Count; index++)
                {
                    results[index].Saved = saved;
                    NotificationPreview row = savedRows[index];
                    if (!saved || row == null || !next.Messages.Contains(row)) continue;
                    results[index].SavedPreview = ClonePreview(row);
                    results[index].MetadataChanged = previousRows[index] != null && MetadataDiffers(previousRows[index], row);
                }
                if (saved) results[0].MetadataUpdates = metadataUpdates.AsReadOnly();
                return results;
            }
        }

        public bool CommitProgress(string chatId, string logId)
        {
            lock (_gate)
            {
                ThrowIfDisposed();
                string room = Identity(chatId);
                string message = Identity(logId);
                if (_loadError || room == null || !IsDecimal(message)) return false;
                DateTime now = Now();
                InboxDocument next = CopyDocument();
                Trim(next, now);
                // A server page cursor alone is not evidence that the body
                // was saved; the host must commit every page event first.
                if (!next.Seen.Any(item => item.ChatId == room && item.LogId == message)
                    && !next.Cursors.Any(item => item.ChatId == room && CompareDecimal(item.LogId, message) == 0)) return false;
                Advance(next, room, message, now);
                Trim(next, now);
                return Save(next);
            }
        }

        public IList<MessageCatchUpCursor> GetCatchUpCursors(int maxRooms)
        {
            lock (_gate)
            {
                ThrowIfDisposed();
                DateTime cutoff = Now().AddDays(-7);
                return _document.Cursors.Where(item => item.AtUtc >= cutoff)
                    .OrderBy(item => item.AtUtc).ThenBy(item => item.ChatId, StringComparer.Ordinal)
                    .Take(Math.Max(0, Math.Min(MaxCursors, maxRooms)))
                    .Select(item => new MessageCatchUpCursor { ChatId = item.ChatId, LogId = item.LogId, UpdatedAtUtc = item.AtUtc }).ToList();
            }
        }

        // A connection-local scan starts before the oldest retained body,
        // independently of the durable highest log. Replaying that window can
        // restore missing participants without moving the durable cursor back.
        public IList<MessageCatchUpCursor> GetConversationSyncCursors(int maxRooms)
        {
            lock (_gate)
            {
                ThrowIfDisposed();
                DateTime now = Now();
                Dictionary<string, MessageCatchUpCursor> candidates = new Dictionary<string, MessageCatchUpCursor>(StringComparer.Ordinal);
                foreach (CursorRecord cursor in _document.Cursors.Where(item => item.AtUtc >= now.AddDays(-7)))
                {
                    string room = CanonicalServerRoom(cursor.ChatId) ?? cursor.ChatId;
                    MessageCatchUpCursor previous;
                    if (!candidates.TryGetValue(room, out previous) || CompareDecimal(cursor.LogId, previous.LogId) > 0)
                        candidates[room] = new MessageCatchUpCursor { ChatId = room, LogId = cursor.LogId, UpdatedAtUtc = cursor.AtUtc };
                }
                HashSet<string> bodyRooms = new HashSet<string>(StringComparer.Ordinal);
                foreach (IGrouping<string, NotificationPreview> room in RetainMessagesAt(_document.Messages, now)
                    .Where(item => Identity(item.ChatId) != null && IsDecimal(item.Id))
                    .GroupBy(item => CanonicalServerRoom(item.ChatId) ?? item.ChatId, StringComparer.Ordinal))
                {
                    NotificationPreview oldest = room.Aggregate((left, right) => CompareDecimal(left.Id, right.Id) <= 0 ? left : right);
                    DateTime activity = room.Max(item => Utc(item.ReceivedAt));
                    MessageCatchUpCursor durable;
                    if (candidates.TryGetValue(room.Key, out durable) && durable.UpdatedAtUtc > activity) activity = durable.UpdatedAtUtc;
                    candidates[room.Key] = new MessageCatchUpCursor { ChatId = room.Key, LogId = DecrementDecimal(oldest.Id), UpdatedAtUtc = activity };
                    bodyRooms.Add(room.Key);
                }
                foreach (RecoveryRecord recovery in _document.Recoveries.Where(item => item.AtUtc >= now.AddDays(-7)))
                {
                    string room = recovery.ChatId;
                    string anchor = recovery.Suppressed ? recovery.ThroughLogId : recovery.FromLogId;
                    MessageCatchUpCursor existing;
                    if (!candidates.TryGetValue(room, out existing))
                        candidates[room] = new MessageCatchUpCursor { ChatId = room, LogId = anchor, UpdatedAtUtc = recovery.AtUtc };
                    else
                    {
                        int order = CompareDecimal(existing.LogId, anchor);
                        if ((recovery.Suppressed && order < 0) || (!recovery.Suppressed && order > 0)) existing.LogId = anchor;
                        if (recovery.AtUtc > existing.UpdatedAtUtc) existing.UpdatedAtUtc = recovery.AtUtc;
                    }
                }
                return candidates.Values.OrderByDescending(item => bodyRooms.Contains(item.ChatId))
                    .ThenByDescending(item => item.UpdatedAtUtc).ThenBy(item => item.ChatId, StringComparer.Ordinal)
                    .Take(Math.Max(0, Math.Min(MaxCursors, maxRooms))).ToList();
            }
        }

        // Discovery metadata is not proof that message bodies were stored.
        public bool BeginUnreadRecovery(string chatId, string fromLogId, string throughLogId)
        {
            lock (_gate)
            {
                ThrowIfDisposed();
                string room = CanonicalServerRoom(chatId);
                if (_loadError || room == null || !ValidRecoveryRange(fromLogId, throughLogId)) return false;
                InboxDocument next = CopyDocument();
                RecoveryRecord pending = next.Recoveries.FirstOrDefault(item => item.ChatId == room);
                if (pending != null)
                {
                    if (pending.Suppressed) return false;
                    if (CompareDecimal(fromLogId, pending.FromLogId) < 0) pending.FromLogId = fromLogId;
                    if (CompareDecimal(throughLogId, pending.ThroughLogId) > 0) pending.ThroughLogId = throughLogId;
                    pending.AtUtc = Now();
                }
                else
                {
                    if (next.Recoveries.Count >= MaxCursors) return false;
                    next.Recoveries.Add(new RecoveryRecord { ChatId = room, FromLogId = fromLogId,
                        ThroughLogId = throughLogId, AtUtc = Now() });
                }
                return Save(next);
            }
        }

        public bool CompleteUnreadRecovery(string chatId)
        {
            lock (_gate)
            {
                ThrowIfDisposed();
                if (_loadError) return false;
                RecoveryRecord pending = _document.Recoveries.FirstOrDefault(item => item.ChatId == chatId);
                if (pending == null) return true;
                if (pending.Suppressed)
                {
                    CursorRecord cursor = _document.Cursors.FirstOrDefault(item => item.ChatId == chatId);
                    if (cursor == null || CompareDecimal(cursor.LogId, pending.ThroughLogId) < 0) return true;
                }
                else if (!_document.Seen.Any(item => item.ChatId == chatId && item.LogId == pending.ThroughLogId))
                    return false;
                InboxDocument next = CopyDocument();
                next.Recoveries.RemoveAll(item => item.ChatId == chatId);
                return Save(next);
            }
        }

        private static bool ValidRecoveryRange(string from, string through)
        {
            return IsDecimal(from) && IsDecimal(through) && from.Length <= 19 && through.Length <= 19
                && (from == "0" || from[0] != '0') && through[0] != '0'
                && CompareDecimal(through, "9223372036854775807") <= 0 && CompareDecimal(from, through) < 0;
        }

        public string ResolveDisplayName(string chatId, string authorId)
        {
            lock (_gate)
            {
                ThrowIfDisposed();
                string room = Identity(chatId);
                string member = Identity(authorId);
                DateTime cutoff = Now().AddDays(-7);
                NameRecord known = _document.Names.FirstOrDefault(item => item.ChatId == room && item.AuthorId == member && item.AtUtc >= cutoff);
                return known == null ? null : known.Name;
            }
        }

        internal InboxRoomMetadata ResolveRoomMetadata(string chatId)
        {
            lock (_gate)
            {
                ThrowIfDisposed();
                string room = CanonicalServerRoom(chatId);
                RoomNameRecord known = _document.RoomNames.FirstOrDefault(item => item.ChatId == room && item.AtUtc >= Now().AddDays(-7));
                return known == null ? null : new InboxRoomMetadata { ChatTitle = known.Title, TitleSource = known.Source };
            }
        }

        internal InboxRoomMetadataResult CommitRoomMetadata(string chatId, string title, string source, bool authoritative = false)
        {
            lock (_gate)
            {
                ThrowIfDisposed();
                if (_loadError || !RoomMetadataContract.ValidChatId(chatId)
                    || !RoomMetadataContract.ValidTitle(title) || RoomMetadataContract.Priority(source) == 0)
                    return new InboxRoomMetadataResult();
                DateTime now = Now();
                InboxDocument next = CopyDocument();
                Trim(next, now);
                RoomNameRecord known = next.RoomNames.FirstOrDefault(item => item.ChatId == chatId);
                if (known != null && !authoritative && RoomMetadataContract.Priority(source) < RoomMetadataContract.Priority(known.Source))
                    return new InboxRoomMetadataResult { Saved = true };
                // Only a verified current resolver snapshot can replace a
                // removed personal name with a weaker fallback. Unchanged
                // successful snapshots refresh their TTL at most daily.
                if (known != null && known.Title == title && known.Source == source && now - known.AtUtc < TimeSpan.FromDays(1))
                    return new InboxRoomMetadataResult { Saved = true };
                if (known == null)
                {
                    known = new RoomNameRecord { ChatId = chatId };
                    next.RoomNames.Add(known);
                }
                known.Title = title;
                known.Source = source;
                known.AtUtc = now;
                List<NotificationPreview> updates = new List<NotificationPreview>();
                foreach (NotificationPreview row in next.Messages.Where(item => SameRoom(item.ChatId, chatId)))
                {
                    if (row.RoomTitle == title && row.RoomTitleSource == source) continue;
                    row.RoomTitle = title;
                    row.RoomTitleSource = source;
                    updates.Add(ClonePreview(row));
                }
                Trim(next, now);
                bool saved = Save(next);
                return new InboxRoomMetadataResult { Saved = saved,
                    Updates = saved ? updates.AsReadOnly() : new List<NotificationPreview>().AsReadOnly() };
            }
        }

        public bool ClearHistory()
        {
            lock (_gate)
            {
                ThrowIfDisposed();
                if (_loadError) return false;
                InboxDocument next = CopyDocument();
                next.Messages.Clear();
                next.Names.Clear();
                next.RoomNames.Clear();
                foreach (RecoveryRecord recovery in next.Recoveries)
                {
                    recovery.Suppressed = true;
                    recovery.AtUtc = Now();
                }
                // Keep replay/cursor tombstones so reconnect does not restore
                // a history the user just cleared.
                Trim(next, Now());
                bool saved = Save(next);
                bool temporaryRemoved = RemoveTemporary();
                return saved && temporaryRemoved;
            }
        }

        public bool Prune()
        {
            lock (_gate)
            {
                ThrowIfDisposed();
                if (_loadError) return false;
                InboxDocument next = CopyDocument();
                int before = next.Messages.Count + next.Seen.Count + next.Cursors.Count + next.Names.Count + next.RoomNames.Count + next.Recoveries.Count;
                Trim(next, Now());
                bool migrated = RestoreLegacyRooms(next);
                int after = next.Messages.Count + next.Seen.Count + next.Cursors.Count + next.Names.Count + next.RoomNames.Count + next.Recoveries.Count;
                return (!_needsPrune && !migrated && before == after) || Save(next);
            }
        }

        public static IList<NotificationPreview> RetainMessages(IEnumerable<NotificationPreview> items)
        {
            return RetainMessagesAt(items, DateTime.UtcNow);
        }

        private static IList<NotificationPreview> RetainMessagesAt(IEnumerable<NotificationPreview> items, DateTime now)
        {
            DateTime cutoff = now.AddDays(-7);
            return (items ?? Enumerable.Empty<NotificationPreview>())
                .Where(item => item != null && Utc(item.ReceivedAt) >= cutoff)
                .OrderByDescending(item => Utc(item.ReceivedAt))
                .ThenBy(item => item.Id ?? string.Empty, StringComparer.Ordinal)
                .ThenBy(item => item.GroupKey ?? string.Empty, StringComparer.Ordinal)
                .ThenBy(item => item.Message ?? string.Empty, StringComparer.Ordinal)
                .GroupBy(item => (item.GroupKey ?? string.Empty) + "\u001f" + (item.Id ?? string.Empty), StringComparer.Ordinal)
                .Select(group => group.First()).Take(MaxMessages).ToList();
        }

        private void Load()
        {
            try
            {
                if (Directory.Exists(_statePath)) throw new IOException("Inbox path is a directory.");
                if (!File.Exists(_statePath)) return;
                FileInfo state = new FileInfo(_statePath);
                if (state.Length <= 0 || state.Length > 64 * 1024 * 1024) throw new SerializationException("Invalid inbox size.");
                byte[] clear = ProtectedData.Unprotect(File.ReadAllBytes(_statePath), Entropy, DataProtectionScope.CurrentUser);
                try
                {
                    InboxDocument loaded;
                    using (MemoryStream stream = new MemoryStream(clear))
                        loaded = (InboxDocument)new DataContractJsonSerializer(typeof(InboxDocument)).ReadObject(stream);
                    if (loaded == null || loaded.Version != 1 || loaded.AccountId != _accountId
                        || loaded.Messages == null || loaded.Seen == null || loaded.Cursors == null || loaded.Names == null)
                        throw new SerializationException("Invalid inbox owner or document.");
                    if (loaded.Seen.Any(item => item == null || string.IsNullOrEmpty(item.Key))
                        || loaded.Cursors.Any(item => item == null || Identity(item.ChatId) == null || !IsDecimal(item.LogId))
                        || loaded.Names.Any(item => item == null || Identity(item.ChatId) == null || Identity(item.AuthorId) == null))
                        throw new SerializationException("Invalid inbox record.");
                    // Version-1 inboxes predate independently stored room
                    // metadata. The absent optional list is a safe migration.
                    if (loaded.RoomNames == null) loaded.RoomNames = new List<RoomNameRecord>();
                    if (loaded.RoomNames.Any(item => item == null || !RoomMetadataContract.ValidChatId(item.ChatId)
                        || !RoomMetadataContract.ValidTitle(item.Title) || RoomMetadataContract.Priority(item.Source) == 0))
                        throw new SerializationException("Invalid room metadata.");
                    if (loaded.Recoveries == null) loaded.Recoveries = new List<RecoveryRecord>();
                    if (loaded.Recoveries.Any(item => item == null || CanonicalServerRoom(item.ChatId) == null
                        || !ValidRecoveryRange(item.FromLogId, item.ThroughLogId)))
                        throw new SerializationException("Invalid recovery metadata.");
                    _document = loaded;
                    int before = _document.Messages.Count + _document.Seen.Count + _document.Cursors.Count + _document.Names.Count + _document.RoomNames.Count + _document.Recoveries.Count;
                    Trim(_document, Now());
                    _needsPrune = before != _document.Messages.Count + _document.Seen.Count + _document.Cursors.Count + _document.Names.Count + _document.RoomNames.Count + _document.Recoveries.Count;
                    InboxDocument migrated = CopyDocument();
                    if (RestoreLegacyRooms(migrated))
                    {
                        _needsPrune = true;
                        Save(migrated); // Failure keeps the original loaded document; Prune/commit can retry.
                    }
                }
                finally { Array.Clear(clear, 0, clear.Length); }
            }
            catch
            {
                // Corruption, a locked file, and an owner mismatch all fail
                // closed. Do not replace unreadable committed history.
                _document = EmptyDocument();
                _available = false;
                _loadError = true;
            }
        }

        private bool Save(InboxDocument next)
        {
            if (_loadError) return false;
            string temporary = _statePath + ".tmp";
            byte[] json = null;
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(_statePath));
                using (MemoryStream stream = new MemoryStream())
                {
                    new DataContractJsonSerializer(typeof(InboxDocument)).WriteObject(stream, next);
                    json = stream.ToArray();
                }
                byte[] ciphertext = ProtectedData.Protect(json, Entropy, DataProtectionScope.CurrentUser);
                if (ciphertext.Length > 64 * 1024 * 1024) throw new IOException("Inbox size limit reached.");
                using (FileStream stream = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None))
                {
                    stream.Write(ciphertext, 0, ciphertext.Length);
                    stream.Flush(true);
                }
                if (File.Exists(_statePath)) File.Replace(temporary, _statePath, null);
                else File.Move(temporary, _statePath);
                _document = next;
                _needsPrune = false;
                _available = true;
                _snapshotWriteCount++;
                return true;
            }
            catch { _available = false; return false; }
            finally
            {
                if (json != null) Array.Clear(json, 0, json.Length);
                RemoveTemporary();
            }
        }

        private bool RemoveTemporary()
        {
            string temporary = _statePath + ".tmp";
            try
            {
                if (File.Exists(temporary)) File.Delete(temporary);
                return !File.Exists(temporary) && !Directory.Exists(temporary);
            }
            catch { return false; }
        }

        private InboxDocument EmptyDocument()
        {
            return new InboxDocument { Version = 1, AccountId = _accountId, Messages = new List<NotificationPreview>(),
                Seen = new List<SeenRecord>(), Cursors = new List<CursorRecord>(), Names = new List<NameRecord>(), RoomNames = new List<RoomNameRecord>(), Recoveries = new List<RecoveryRecord>() };
        }

        private InboxDocument CopyDocument()
        {
            return new InboxDocument { Version = 1, AccountId = _accountId,
                Messages = _document.Messages.Select(ClonePreview).ToList(),
                Seen = _document.Seen.Select(item => new SeenRecord { Key = item.Key, ChatId = item.ChatId, LogId = item.LogId, AtUtc = item.AtUtc }).ToList(),
                Cursors = _document.Cursors.Select(item => new CursorRecord { ChatId = item.ChatId, LogId = item.LogId, AtUtc = item.AtUtc }).ToList(),
                Names = _document.Names.Select(item => new NameRecord { ChatId = item.ChatId, AuthorId = item.AuthorId, Name = item.Name, AtUtc = item.AtUtc }).ToList(),
                RoomNames = _document.RoomNames.Select(item => new RoomNameRecord { ChatId = item.ChatId, Title = item.Title, Source = item.Source, AtUtc = item.AtUtc }).ToList(),
                Recoveries = _document.Recoveries.Select(item => new RecoveryRecord { ChatId = item.ChatId, FromLogId = item.FromLogId,
                    ThroughLogId = item.ThroughLogId, AtUtc = item.AtUtc, Suppressed = item.Suppressed }).ToList() };
        }

        private NotificationPreview ClonePreview(NotificationPreview item)
        {
            return new NotificationPreview { Id = item.Id, Sender = item.Sender, Message = item.Message, ReceivedAt = item.ReceivedAt,
                Source = item.Source, GroupKey = item.GroupKey, IsMine = item.IsMine, MessageType = item.MessageType,
                ChatId = item.ChatId, RoomTitle = item.RoomTitle, RoomTitleSource = item.RoomTitleSource, AuthorName = item.AuthorName,
                DemoAvatarResource = item.DemoAvatarResource, AccountId = _accountId };
        }

        private sealed class RoomMemberProof
        {
            public string ChatId;
            public string AuthorId;
            public string Name;
        }

        private bool RestoreLegacyRooms(InboxDocument document)
        {
            if (!document.Messages.Any(item => string.IsNullOrWhiteSpace(item.ChatId) && !string.IsNullOrWhiteSpace(item.GroupKey))) return false;
            Dictionary<string, RoomMemberProof> proofs = new Dictionary<string, RoomMemberProof>(StringComparer.Ordinal);
            foreach (NameRecord member in document.Names)
            {
                string room = CanonicalServerRoom(member.ChatId);
                if (room == null) continue;
                string key = NotificationGrouping.CreateServerGroupKey(room, member.AuthorId);
                if (key == null) continue;
                RoomMemberProof known;
                if (!proofs.TryGetValue(key, out known))
                    proofs[key] = new RoomMemberProof { ChatId = room, AuthorId = member.AuthorId, Name = member.Name };
                else if (known != null && (!SameRoom(known.ChatId, room) || !SameMember(known.AuthorId, member.AuthorId)))
                    proofs[key] = null; // Ambiguous evidence is never used to assign a room.
            }
            Dictionary<string, string> titles = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (NotificationPreview item in document.Messages.Where(item => !string.IsNullOrWhiteSpace(item.RoomTitle)).OrderByDescending(item => Utc(item.ReceivedAt)))
            {
                string room = CanonicalServerRoom(item.ChatId);
                if (room != null && !titles.ContainsKey(room)) titles[room] = item.RoomTitle;
            }
            bool changed = false;
            foreach (NotificationPreview item in document.Messages)
            {
                if (!string.IsNullOrWhiteSpace(item.ChatId) || string.IsNullOrWhiteSpace(item.GroupKey)) continue;
                RoomMemberProof proof;
                proofs.TryGetValue(item.GroupKey, out proof);
                if (proof == null && item.IsMine)
                {
                    string[] rooms = document.Seen.Where(seen => seen.LogId == item.Id && CanonicalServerRoom(seen.ChatId) != null
                            && seen.Key == MessageKey(seen.ChatId, seen.LogId, item)
                            && NotificationGrouping.CreateServerGroupKey(seen.ChatId, _accountId) == item.GroupKey)
                        .Select(seen => CanonicalServerRoom(seen.ChatId)).Distinct(StringComparer.Ordinal).ToArray();
                    if (rooms.Length == 1) proof = new RoomMemberProof { ChatId = rooms[0], AuthorId = _accountId, Name = item.Sender };
                }
                if (proof == null) continue;
                item.ChatId = proof.ChatId;
                if (string.IsNullOrWhiteSpace(item.AuthorName)) item.AuthorName = DisplayName(item.Sender) ?? proof.Name;
                item.IsMine = SameMember(proof.AuthorId, _accountId);
                string title;
                if (string.IsNullOrWhiteSpace(item.RoomTitle) && titles.TryGetValue(proof.ChatId, out title)) item.RoomTitle = title;
                changed = true;
            }
            return changed;
        }

        private static bool MetadataDiffers(NotificationPreview left, NotificationPreview right)
        {
            return left.ChatId != right.ChatId || left.RoomTitle != right.RoomTitle || left.RoomTitleSource != right.RoomTitleSource
                || left.AuthorName != right.AuthorName || left.IsMine != right.IsMine;
        }

        private static int TitlePriority(string source)
        {
            return RoomMetadataContract.Priority(source) == 0 ? 2 : RoomMetadataContract.Priority(source);
        }

        private static void ApplyRoomMetadata(InboxDocument document, NotificationPreview row)
        {
            string room = CanonicalServerRoom(row.ChatId);
            if (room == null) return;
            RoomNameRecord known = document.RoomNames.FirstOrDefault(item => item.ChatId == room);
            if (known == null) return;
            row.RoomTitle = known.Title;
            row.RoomTitleSource = known.Source;
        }

        private static bool MatchesAuthoritativeRoom(NotificationPreview preview, string chatId)
        {
            return string.IsNullOrWhiteSpace(preview.ChatId) || SameRoom(preview.ChatId, chatId);
        }

        private static bool SameRoom(string left, string right)
        {
            string a = CanonicalServerRoom(left) ?? Identity(left);
            string b = CanonicalServerRoom(right) ?? Identity(right);
            return a != null && b != null && string.Equals(a, b, StringComparison.Ordinal);
        }

        private static bool SameMember(string left, string right)
        {
            ulong a;
            ulong b;
            if (ulong.TryParse(left, NumberStyles.None, CultureInfo.InvariantCulture, out a)
                && ulong.TryParse(right, NumberStyles.None, CultureInfo.InvariantCulture, out b)) return a == b;
            return string.Equals(Identity(left), Identity(right), StringComparison.Ordinal);
        }

        private static string CanonicalServerRoom(string value)
        {
            string identity = Identity(value);
            long number;
            return IsDecimal(identity) && long.TryParse(identity, NumberStyles.None, CultureInfo.InvariantCulture, out number) && number > 0
                ? number.ToString(CultureInfo.InvariantCulture) : null;
        }

        private static string DecrementDecimal(string value)
        {
            string canonical = value.TrimStart('0');
            if (canonical.Length == 0) return "0";
            char[] digits = canonical.ToCharArray();
            for (int index = digits.Length - 1; index >= 0; index--)
            {
                if (digits[index] == '0') { digits[index] = '9'; continue; }
                digits[index]--;
                break;
            }
            string decremented = new string(digits).TrimStart('0');
            return decremented.Length == 0 ? "0" : decremented;
        }

        private static void Trim(InboxDocument document, DateTime now)
        {
            DateTime cutoff = now.AddDays(-7);
            document.Messages = RetainMessagesAt(document.Messages, now).ToList();
            Dictionary<string, DateTime> activeRooms = document.Messages.Where(item => CanonicalServerRoom(item.ChatId) != null)
                .GroupBy(item => CanonicalServerRoom(item.ChatId), StringComparer.Ordinal)
                .ToDictionary(group => group.Key, group => group.Max(item => Utc(item.ReceivedAt) > now ? now : Utc(item.ReceivedAt)), StringComparer.Ordinal);
            // An unchanged name need not rewrite the encrypted snapshot on
            // every resolver poll. Retained recent bodies keep their room's
            // independently stored name and its precedence alive instead.
            foreach (RoomNameRecord room in document.RoomNames)
            {
                DateTime activity;
                if (activeRooms.TryGetValue(room.ChatId, out activity) && activity > room.AtUtc) room.AtUtc = activity;
            }
            document.Seen = document.Seen.Where(item => item.AtUtc >= cutoff).OrderByDescending(item => item.AtUtc)
                .ThenBy(item => item.Key, StringComparer.Ordinal).Take(MaxSeen).ToList();
            document.Cursors = document.Cursors.Where(item => item.AtUtc >= cutoff).OrderByDescending(item => item.AtUtc)
                .ThenBy(item => item.ChatId, StringComparer.Ordinal).Take(MaxCursors).ToList();
            document.Recoveries = document.Recoveries.Where(item => item.AtUtc >= cutoff).OrderByDescending(item => item.AtUtc)
                .ThenBy(item => item.ChatId, StringComparer.Ordinal).Take(MaxCursors).ToList();
            document.Names = document.Names.Where(item => item.AtUtc >= cutoff).OrderByDescending(item => item.AtUtc)
                .ThenBy(item => item.ChatId, StringComparer.Ordinal).ThenBy(item => item.AuthorId, StringComparer.Ordinal).Take(MaxNames).ToList();
            document.RoomNames = document.RoomNames.Where(item => item.AtUtc >= cutoff)
                .OrderByDescending(item => activeRooms.ContainsKey(item.ChatId)).ThenByDescending(item => item.AtUtc)
                .ThenBy(item => item.ChatId, StringComparer.Ordinal).Take(MaxRoomNames).ToList();
        }

        private static void Advance(InboxDocument next, string chatId, string logId, DateTime now)
        {
            CursorRecord cursor = next.Cursors.FirstOrDefault(item => item.ChatId == chatId);
            if (cursor == null) next.Cursors.Add(new CursorRecord { ChatId = chatId, LogId = logId, AtUtc = now });
            else if (CompareDecimal(logId, cursor.LogId) > 0) { cursor.LogId = logId; cursor.AtUtc = now; }
        }

        private static int CompareDecimal(string left, string right)
        {
            string a = left.TrimStart('0');
            string b = right.TrimStart('0');
            return a.Length == b.Length ? string.CompareOrdinal(a, b) : a.Length.CompareTo(b.Length);
        }

        private static bool IsDecimal(string value) { return !string.IsNullOrEmpty(value) && value.Length <= 256 && value.All(ch => ch >= '0' && ch <= '9'); }
        private static string Identity(string value) { return string.IsNullOrWhiteSpace(value) || value.Trim().Length > 256 ? null : value.Trim(); }
        private static DateTime Utc(DateTime value) { return value.Kind == DateTimeKind.Utc ? value : value.ToUniversalTime(); }
        private DateTime Now() { return Utc(_utcNow()); }
        private static string DisplayName(string value)
        {
            if (string.IsNullOrWhiteSpace(value)) return null;
            string clean = new string(value.Where(ch => !char.IsControl(ch)).ToArray()).Trim();
            return clean.Length == 0 || clean.Length > 200 ? null : clean;
        }
        private static string MessageKey(string chatId, string logId, NotificationPreview preview)
        {
            return chatId != null && logId != null ? Digest("message\u001f" + chatId + "\u001f" + logId)
                : Digest("fallback\u001f" + (preview.GroupKey ?? "") + "\u001f" + (preview.Id ?? "") + "\u001f" + preview.ReceivedAt.ToUniversalTime().Ticks + "\u001f" + preview.Message);
        }
        private static string Digest(string value)
        {
            using (SHA256 hash = SHA256.Create()) return BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(value))).Replace("-", "").ToLowerInvariant();
        }
        private void ThrowIfDisposed() { if (_disposed) throw new ObjectDisposedException("InboxStore"); }
        public void Dispose() { lock (_gate) { _disposed = true; } }

        [DataContract]
        private sealed class InboxDocument
        {
            [DataMember(Order = 1)] public int Version { get; set; }
            [DataMember(Order = 2)] public string AccountId { get; set; }
            [DataMember(Order = 3)] public List<NotificationPreview> Messages { get; set; }
            [DataMember(Order = 4)] public List<SeenRecord> Seen { get; set; }
            [DataMember(Order = 5)] public List<CursorRecord> Cursors { get; set; }
            [DataMember(Order = 6)] public List<NameRecord> Names { get; set; }
            [DataMember(Order = 7, EmitDefaultValue = false)] public List<RoomNameRecord> RoomNames { get; set; }
            [DataMember(Order = 8, EmitDefaultValue = false)] public List<RecoveryRecord> Recoveries { get; set; }
        }
        [DataContract]
        private sealed class RecoveryRecord
        {
            [DataMember(Order = 1)] public string ChatId { get; set; }
            [DataMember(Order = 2)] public string FromLogId { get; set; }
            [DataMember(Order = 3)] public string ThroughLogId { get; set; }
            [DataMember(Order = 4)] public DateTime AtUtc { get; set; }
            [DataMember(Order = 5)] public bool Suppressed { get; set; }
        }
        [DataContract]
        private sealed class RoomNameRecord
        {
            [DataMember(Order = 1)] public string ChatId { get; set; }
            [DataMember(Order = 2)] public string Title { get; set; }
            [DataMember(Order = 3)] public string Source { get; set; }
            [DataMember(Order = 4)] public DateTime AtUtc { get; set; }
        }
        [DataContract]
        private sealed class SeenRecord
        {
            [DataMember(Order = 1)] public string Key { get; set; }
            [DataMember(Order = 2)] public string ChatId { get; set; }
            [DataMember(Order = 3)] public string LogId { get; set; }
            [DataMember(Order = 4)] public DateTime AtUtc { get; set; }
        }
        [DataContract]
        private sealed class CursorRecord
        {
            [DataMember(Order = 1)] public string ChatId { get; set; }
            [DataMember(Order = 2)] public string LogId { get; set; }
            [DataMember(Order = 3)] public DateTime AtUtc { get; set; }
        }
        [DataContract]
        private sealed class NameRecord
        {
            [DataMember(Order = 1)] public string ChatId { get; set; }
            [DataMember(Order = 2)] public string AuthorId { get; set; }
            [DataMember(Order = 3)] public string Name { get; set; }
            [DataMember(Order = 4)] public DateTime AtUtc { get; set; }
        }
    }

    public sealed class InboxCommitResult
    {
        public bool IsNew { get; internal set; }
        public bool Saved { get; internal set; }
        public bool MetadataChanged { get; internal set; }
        public IList<NotificationPreview> MetadataUpdates { get; internal set; }
        public NotificationPreview SavedPreview { get; internal set; }
    }

    internal sealed class InboxMessageWrite
    {
        public NotificationPreview Preview { get; set; }
        public string ChatId { get; set; }
        public string AuthorId { get; set; }
        public bool AdvanceCursor { get; set; }
    }

    internal sealed class InboxRoomMetadata
    {
        public string ChatTitle { get; set; }
        public string TitleSource { get; set; }
    }

    internal sealed class InboxRoomMetadataResult
    {
        public bool Saved { get; set; }
        public IList<NotificationPreview> Updates { get; set; }
    }
}
