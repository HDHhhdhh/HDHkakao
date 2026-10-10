using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace Spoiler.Tests
{
    internal static class InboxStoreTests
    {
        private static string _root;
        private static int _failures;

        public static int Main()
        {
            _root = Path.Combine(Path.GetTempPath(), "SpoilerInboxTests-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_root);
            try
            {
                Run("atomic restart and account isolation", AccountAndRestart);
                Run("locked writes rollback and recover", FailedCommit);
                Run("corruption and owner mismatch fail closed", Corruption);
                Run("server-time retention independent of arrival", Retention);
                Run("clear tombstones and scoped temporary cleanup", Clear);
                Run("expired records prune during runtime", Expiry);
                Run("recovered duplicate advances only committed cursor", DuplicateProgress);
                Run("legacy unowned history remains unbound", Legacy);
                Run("bounded burst is one durable transaction", BatchCommit);
                Run("failed burst rolls back all bodies and cursors", BatchRollback);
                Run("arbitrary decimal cursor ordering survives restart", DecimalCursorOrdering);
                Run("metadata and replay bounds remain encrypted", MetadataBounds);
                Run("account identity is mandatory and bounded", AccountIdentityRequired);
                Run("room metadata survives encrypted copy and restart", RoomMetadataRoundTrip);
                Run("exact member proof restores old room and own identity", LegacyRoomMigration);
                Run("seen logs alone cannot guess a room", LegacySeenProof);
                Run("failed migration preserves old document and retries", MigrationRollback);
                Run("metadata replay requires exact identity and body", MetadataReplay);
                Run("new member proof publishes separate old row upgrades", SupplementalMigration);
                Run("history anchors rewind retained bodies but respect deletion", ConversationAnchors);
                Run("unseen unread range survives restart without inventing body progress", UnreadRecoveryRestart);
                Run("clear suppresses pending unread restoration", ClearUnreadRecovery);
            }
            finally { try { Directory.Delete(_root, true); } catch { } }
            Console.WriteLine(_failures == 0 ? "PASS: inbox store tests (22 scenarios)" : "FAIL: inbox store scenarios=" + _failures);
            return _failures == 0 ? 0 : 1;
        }

        private static void Run(string name, Action test)
        {
            try { test(); Console.WriteLine("PASS: " + name); }
            catch (Exception error) { _failures++; Console.WriteLine("FAIL: " + name + " (" + error.GetType().Name + ")"); }
        }
        private static void Check(bool condition) { if (!condition) throw new InvalidOperationException("Assertion failed."); }
        private static string Area(string name) { return Path.Combine(_root, name); }
        private static NotificationPreview Message(string id, DateTime? at = null)
        {
            return new NotificationPreview { Id = id, Sender = "가상 발신자", Message = "synthetic body " + id,
                GroupKey = "test-room", ReceivedAt = at ?? DateTime.UtcNow, IsMine = true, MessageType = 26 };
        }
        private static string Cursor(InboxStore store, string chatId)
        {
            MessageCatchUpCursor cursor = store.GetCatchUpCursors(512).FirstOrDefault(item => item.ChatId == chatId);
            return cursor == null ? null : cursor.LogId;
        }

        private static void UnreadRecoveryRestart()
        {
            string directory = Area("unread-recovery");
            using (InboxStore store = new InboxStore(directory, "123"))
            {
                Check(store.BeginUnreadRecovery("202", "0", "106"));
                Check(store.LoadMessages().Count == 0 && store.GetCatchUpCursors(512).Count == 0);
                Check(store.GetConversationSyncCursors(512).Single().LogId == "0");
                Check(store.CommitMessage(RoomMessage("202", "500", "222"), "202", "222", true).Saved);
                Check(!store.CompleteUnreadRecovery("202"));
                Check(store.GetConversationSyncCursors(512).Single().LogId == "0");
                Check(store.CommitMessage(RoomMessage("202", "101", "222"), "202", "222", true).Saved);
            }
            using (InboxStore restored = new InboxStore(directory, "123"))
            {
                Check(restored.GetConversationSyncCursors(512).Single().LogId == "0");
                for (int i = 102; i <= 106; i++)
                    Check(restored.CommitMessage(RoomMessage("202", i.ToString(), "222"), "202", "222", true).Saved);
                Check(restored.CompleteUnreadRecovery("202"));
                Check(restored.GetConversationSyncCursors(512).Single().LogId == "100");
                Check(!restored.BeginUnreadRecovery("202", "107", "106"));
                Check(!restored.BeginUnreadRecovery("202", "0", "9223372036854775808"));
            }
        }

        private static void ClearUnreadRecovery()
        {
            string directory = Area("clear-unread-recovery");
            using (InboxStore store = new InboxStore(directory, "123"))
            {
                Check(store.BeginUnreadRecovery("202", "0", "106"));
                Check(store.ClearHistory());
                Check(store.CompleteUnreadRecovery("202"));
                Check(store.GetConversationSyncCursors(512).Single().LogId == "106");
            }
            using (InboxStore restored = new InboxStore(directory, "123"))
            {
                Check(restored.GetConversationSyncCursors(512).Single().LogId == "106");
                Check(!restored.BeginUnreadRecovery("202", "0", "106"));
                Check(restored.CommitMessage(RoomMessage("202", "107", "222"), "202", "222", true).Saved);
                Check(restored.CompleteUnreadRecovery("202"));
                Check(restored.GetConversationSyncCursors(512).Single().LogId == "106");
            }
        }

        private static IList<InboxMessageWrite> Burst(int first, int count)
        {
            return Enumerable.Range(first, count).Select(id => new InboxMessageWrite { Preview = Message(id.ToString()),
                ChatId = "room", AuthorId = "author", AdvanceCursor = true }).ToList();
        }

        private static void BatchCommit()
        {
            string directory = Area("batch");
            using (InboxStore store = new InboxStore(directory, "a"))
            {
                IList<InboxMessageWrite> burst = Burst(100, InboxStore.MaxCommitBatch);
                IList<InboxCommitResult> committed = store.CommitMessages(burst);
                Check(committed.Count == 50 && committed.All(item => item.Saved && item.IsNew));
                Check(store.SnapshotWriteCount == 1 && store.LoadMessages().Count == 50 && Cursor(store, "room") == "149");
                using (InboxStore disk = new InboxStore(directory, "a"))
                    Check(disk.LoadMessages().Count == 50 && Cursor(disk, "room") == "149");
                IList<InboxMessageWrite> duplicates = Burst(140, 3);
                duplicates.Add(Burst(150, 1)[0]);
                duplicates.Add(Burst(150, 1)[0]);
                committed = store.CommitMessages(duplicates);
                Check(committed.Select(item => item.IsNew).SequenceEqual(new[] { false, false, false, true, false }));
                Check(committed.All(item => item.Saved) && store.SnapshotWriteCount == 2 && Cursor(store, "room") == "150");
                bool rejected = false;
                try { store.CommitMessages(Burst(200, InboxStore.MaxCommitBatch + 1)); }
                catch (ArgumentOutOfRangeException) { rejected = true; }
                Check(rejected && store.SnapshotWriteCount == 2);
                IList<InboxMessageWrite> wrongOwner = Burst(200, 2);
                wrongOwner[1].Preview.AccountId = "b";
                Check(store.CommitMessages(wrongOwner).All(item => !item.Saved) && store.SnapshotWriteCount == 2);
                Check(store.LoadMessages().Count == 51 && Cursor(store, "room") == "150");
            }
        }

        private static void BatchRollback()
        {
            using (InboxStore store = new InboxStore(Area("batch-failure"), "a"))
            {
                Check(store.CommitMessage(Message("100"), "room", "author", true).Saved);
                byte[] before = File.ReadAllBytes(store.StatePath);
                IList<InboxMessageWrite> burst = Burst(200, 50);
                using (FileStream locked = new FileStream(store.StatePath, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
                {
                    Check(store.CommitMessages(burst).All(item => item.IsNew && !item.Saved));
                    Check(store.LoadMessages().Count == 1 && Cursor(store, "room") == "100" && store.SnapshotWriteCount == 1);
                }
                Check(before.SequenceEqual(File.ReadAllBytes(store.StatePath)));
                Check(store.CommitMessages(burst).All(item => item.IsNew && item.Saved));
                Check(store.LoadMessages().Count == 51 && Cursor(store, "room") == "249" && store.SnapshotWriteCount == 2);
            }
        }

        private static void RoomMetadataRoundTrip()
        {
            string directory = Area("room-metadata");
            const string roomId = "76543210987654321";
            const string title = "SYNTHETIC_ROOM_TITLE";
            const string author = "SYNTHETIC_AUTHOR";
            using (InboxStore store = new InboxStore(directory, "room-metadata-owner"))
            {
                NotificationPreview preview = Message("100");
                preview.ChatId = roomId; preview.RoomTitle = title; preview.AuthorName = author;
                Check(store.CommitMessage(preview, roomId, "member-a", true).Saved);
                // A second transaction clones the full document, so this also
                // guards against silently dropping metadata on later arrivals.
                Check(store.CommitMessage(Message("101"), "another-room", "member-b", true).Saved);
                NotificationPreview clone = store.LoadMessages().Single(item => item.Id == "100");
                Check(clone.ChatId == roomId && clone.RoomTitle == title && clone.AuthorName == author);
                clone.ChatId = "42"; clone.RoomTitle = "changed outside store"; clone.AuthorName = "changed";
                NotificationPreview unaffected = store.LoadMessages().Single(item => item.Id == "100");
                Check(unaffected.ChatId == roomId && unaffected.RoomTitle == title && unaffected.AuthorName == author);
                string encrypted = Encoding.UTF8.GetString(File.ReadAllBytes(store.StatePath));
                Check(!encrypted.Contains(roomId) && !encrypted.Contains(title) && !encrypted.Contains(author));
            }
            using (InboxStore restarted = new InboxStore(directory, "room-metadata-owner"))
            {
                NotificationPreview loaded = restarted.LoadMessages().Single(item => item.Id == "100");
                Check(loaded.ChatId == roomId && loaded.RoomTitle == title && loaded.AuthorName == author);
                Check(loaded.AccountId == "room-metadata-owner" && Cursor(restarted, roomId) == "100");
            }
        }

        private static NotificationPreview RoomMessage(string room, string id, string author, string title = null)
        {
            NotificationPreview row = Message(id);
            row.ChatId = room;
            row.GroupKey = NotificationGrouping.CreateServerGroupKey(room, author);
            row.Sender = "writer " + author;
            row.AuthorName = row.Sender;
            row.RoomTitle = title;
            row.IsMine = author == "123";
            return row;
        }

        // Rewrite only a unique synthetic DPAPI fixture into the old schema.
        // Reflection uses the production document type, preserving its dates
        // and replay keys rather than inventing a second serializer contract.
        private static void Rewrite(string path, Action<object> change)
        {
            byte[] entropy = Encoding.UTF8.GetBytes("Spoiler.AccountInbox.v1");
            byte[] clear = ProtectedData.Unprotect(File.ReadAllBytes(path), entropy, DataProtectionScope.CurrentUser);
            byte[] rewritten = null;
            try
            {
                Type type = typeof(InboxStore).GetNestedType("InboxDocument", BindingFlags.NonPublic);
                DataContractJsonSerializer serializer = new DataContractJsonSerializer(type);
                object document;
                using (MemoryStream input = new MemoryStream(clear)) document = serializer.ReadObject(input);
                change(document);
                using (MemoryStream output = new MemoryStream())
                {
                    serializer.WriteObject(output, document);
                    rewritten = output.ToArray();
                }
                File.WriteAllBytes(path, ProtectedData.Protect(rewritten, entropy, DataProtectionScope.CurrentUser));
            }
            finally
            {
                Array.Clear(clear, 0, clear.Length);
                if (rewritten != null) Array.Clear(rewritten, 0, rewritten.Length);
            }
        }
        private static IList Records(object document, string property)
        {
            return (IList)document.GetType().GetProperty(property).GetValue(document, null);
        }
        private static string Value(object record, string property)
        {
            return (string)record.GetType().GetProperty(property).GetValue(record, null);
        }
        private static void SetValue(object record, string property, string value)
        {
            record.GetType().GetProperty(property).SetValue(record, value, null);
        }
        private static string SeedLegacy(string directory, bool includeNames = true)
        {
            string path;
            using (InboxStore store = new InboxStore(directory, "123"))
            {
                path = store.StatePath;
                Check(store.CommitMessage(RoomMessage("101", "100", "222"), "101", "222", true).Saved);
                Check(store.CommitMessage(RoomMessage("101", "101", "123"), "101", "123", true).Saved);
                Check(store.CommitMessage(RoomMessage("101", "102", "333", "actual room title"), "101", "333", true).Saved);
                Check(store.CommitMessage(RoomMessage("202", "100", "222", "other room"), "202", "222", true).Saved);
            }
            Rewrite(path, document =>
            {
                foreach (NotificationPreview row in Records(document, "Messages"))
                    if (row.ChatId == "101" && row.Id != "102")
                    { row.ChatId = null; row.RoomTitle = null; row.AuthorName = null; }
                if (!includeNames) Records(document, "Names").Clear();
            });
            return path;
        }

        private static void LegacyRoomMigration()
        {
            string directory = Area("legacy-room-exact");
            string path = SeedLegacy(directory);
            Rewrite(path, document =>
            {
                Records(document, "Seen").Clear(); // Names alone prove the old room/author hash.
                foreach (NotificationPreview row in Records(document, "Messages"))
                    if (row.Id == "101") row.IsMine = false;
            });
            using (InboxStore store = new InboxStore(directory, "123"))
            {
                IList<NotificationPreview> rows = store.LoadMessages();
                Check(store.PersistenceAvailable && store.SnapshotWriteCount == 1 && rows.Count == 4);
                Check(rows.Count(row => row.ChatId == "101") == 3 && rows.Count(row => row.ChatId == "202") == 1);
                Check(rows.Single(row => row.Id == "101").IsMine);
                Check(rows.Where(row => row.ChatId == "101").All(row => row.RoomTitle == "actual room title"));
                Check(rows.Where(row => row.ChatId == "101").All(row => row.AuthorName != null));
                Check(rows.All(row => row.Message == "synthetic body " + row.Id));
                string cipher = Encoding.UTF8.GetString(File.ReadAllBytes(path));
                Check(!cipher.Contains("actual room title") && !cipher.Contains("writer 123"));
            }
            using (InboxStore restarted = new InboxStore(directory, "123"))
                Check(restarted.SnapshotWriteCount == 0 && restarted.LoadMessages().Count(row => row.ChatId == "101") == 3);
        }

        private static void LegacySeenProof()
        {
            string directory = Area("legacy-seen-proof");
            SeedLegacy(directory, false);
            using (InboxStore store = new InboxStore(directory, "123"))
            {
                IList<NotificationPreview> rows = store.LoadMessages();
                Check(rows.Single(row => row.Id == "101").ChatId == "101");
                NotificationPreview peer = rows.Single(row => row.Id == "100" && row.ChatId != "202");
                Check(peer.ChatId == null && !peer.IsMine); // Same log exists in two rooms; it proves neither writer.
            }
            string invalid = Area("legacy-invalid-own-seen");
            string path = SeedLegacy(invalid, false);
            Rewrite(path, document =>
            {
                foreach (object seen in Records(document, "Seen"))
                    if (Value(seen, "LogId") == "101") SetValue(seen, "Key", "invalid replay proof");
            });
            using (InboxStore store = new InboxStore(invalid, "123"))
                Check(store.LoadMessages().Single(row => row.Id == "101").ChatId == null);
        }

        private static void MigrationRollback()
        {
            string directory = Area("legacy-migration-rollback");
            string path = SeedLegacy(directory);
            byte[] before = File.ReadAllBytes(path);
            string blocker = path + ".tmp";
            Directory.CreateDirectory(blocker);
            using (InboxStore store = new InboxStore(directory, "123"))
            {
                Check(!store.HasLoadError && !store.PersistenceAvailable && store.SnapshotWriteCount == 0);
                Check(store.LoadMessages().Single(row => row.Id == "101").ChatId == null);
                Check(before.SequenceEqual(File.ReadAllBytes(path)));
                Directory.Delete(blocker);
                Check(store.Prune() && store.PersistenceAvailable && store.SnapshotWriteCount == 1);
                Check(store.LoadMessages().Single(row => row.Id == "101").ChatId == "101");
            }
        }

        private static void MetadataReplay()
        {
            using (InboxStore store = new InboxStore(Area("metadata-replay"), "123"))
            {
                NotificationPreview original = RoomMessage("101", "100", "222", "initial");
                Check(store.CommitMessage(original, "101", "222", true).Saved);
                NotificationPreview upgrade = RoomMessage("101", "100", "222", "enriched");
                upgrade.AuthorName = "resolved writer"; upgrade.IsMine = true;
                using (FileStream locked = new FileStream(store.StatePath, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
                {
                    InboxCommitResult failed = store.CommitMessage(upgrade, "101", "222", true);
                    Check(!failed.Saved && failed.SavedPreview == null && failed.MetadataUpdates == null);
                    Check(store.LoadMessages().Single().RoomTitle == "initial");
                }
                InboxCommitResult updated = store.CommitMessage(upgrade, "101", "222", true);
                Check(updated.Saved && !updated.IsNew && updated.MetadataChanged && updated.SavedPreview.RoomTitle == "enriched");
                Check(!updated.SavedPreview.IsMine && updated.SavedPreview.AuthorName == "resolved writer");
                Check(updated.SavedPreview.ReceivedAt == original.ReceivedAt && updated.MetadataUpdates.Count == 0);
                updated.SavedPreview.RoomTitle = "caller mutation";
                Check(store.LoadMessages().Single().RoomTitle == "enriched");
                upgrade.Message = "different body"; upgrade.RoomTitle = "unsafe overwrite";
                InboxCommitResult mismatch = store.CommitMessage(upgrade, "101", "222", true);
                Check(mismatch.Saved && !mismatch.IsNew && !mismatch.MetadataChanged && mismatch.SavedPreview == null);
                Check(store.LoadMessages().Single().RoomTitle == "enriched" && store.LoadMessages().Single().Message == original.Message);
                NotificationPreview wrongRoom = RoomMessage("202", "101", "222");
                Check(!store.CommitMessage(wrongRoom, "101", "222", true).Saved && store.LoadMessages().Count == 1);
                NotificationPreview collision = RoomMessage("202", "100", "222", "different actual room");
                Check(store.CommitMessage(collision, "202", "222", true).IsNew && store.LoadMessages().Count == 2);
            }
        }

        private static void SupplementalMigration()
        {
            string directory = Area("supplemental-migration");
            string path = SeedLegacy(directory, false);
            Rewrite(path, document => Records(document, "Seen").Clear());
            using (InboxStore store = new InboxStore(directory, "123"))
            {
                Check(store.LoadMessages().Count(row => row.ChatId == null) == 2);
                NotificationPreview newBody = RoomMessage("101", "103", "222", "actual room title");
                using (FileStream locked = new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
                {
                    InboxCommitResult failed = store.CommitMessage(newBody, "101", "222", true);
                    Check(!failed.Saved && failed.MetadataUpdates == null && store.LoadMessages().Count(row => row.ChatId == null) == 2);
                }
                InboxCommitResult saved = store.CommitMessage(newBody, "101", "222", true);
                Check(saved.Saved && saved.IsNew && saved.SavedPreview.Id == "103" && saved.MetadataUpdates.Count == 1);
                Check(saved.MetadataUpdates.Single().Id == "100" && saved.MetadataUpdates.Single().ChatId == "101");
                Check(saved.MetadataUpdates.Single().Message == "synthetic body 100" && store.LoadMessages().Count == 5);
                saved.MetadataUpdates.Single().ChatId = "caller mutation";
                Check(store.LoadMessages().Any(row => row.Id == "100" && row.ChatId == "101"));
                InboxCommitResult replay = store.CommitMessage(newBody, "101", "222", true);
                Check(replay.Saved && !replay.IsNew && !replay.MetadataChanged && replay.MetadataUpdates.Count == 0);
                Check(store.ClearHistory());
                InboxCommitResult clearedReplay = store.CommitMessage(newBody, "101", "222", true);
                Check(clearedReplay.Saved && !clearedReplay.IsNew && clearedReplay.SavedPreview == null
                    && clearedReplay.MetadataUpdates.Count == 0 && store.LoadMessages().Count == 0);
            }
        }

        private static void ConversationAnchors()
        {
            using (InboxStore store = new InboxStore(Area("conversation-anchors"), "123"))
            {
                Check(store.CommitMessage(RoomMessage("101", "100", "222"), "101", "222", true).Saved);
                Check(store.CommitMessage(RoomMessage("101", "102", "222"), "101", "222", true).Saved);
                Check(Cursor(store, "101") == "102" && store.GetConversationSyncCursors(80).Single().LogId == "99");
                Check(store.CommitMessage(RoomMessage("202", "1", "333"), "202", "333", true).Saved);
                Check(store.GetConversationSyncCursors(80).Single(row => row.ChatId == "202").LogId == "0");
                Check(store.GetConversationSyncCursors(0).Count == 0 && store.GetConversationSyncCursors(1).Count == 1);
                Check(store.ClearHistory());
                Check(store.GetConversationSyncCursors(80).Single(row => row.ChatId == "101").LogId == "102");
                InboxCommitResult deleted = store.CommitMessage(RoomMessage("101", "100", "222"), "101", "222", true);
                Check(deleted.Saved && !deleted.IsNew && deleted.SavedPreview == null && store.LoadMessages().Count == 0);
                Check(store.GetConversationSyncCursors(80).Single(row => row.ChatId == "101").LogId == "102");
            }
        }

        private static void AccountAndRestart()
        {
            string aPath;
            using (InboxStore a = new InboxStore(Area("accounts"), "synthetic-account-a"))
            {
                aPath = a.StatePath;
                InboxCommitResult result = a.CommitMessage(Message("100"), "room-a", "person-a", true);
                Check(result.IsNew && result.Saved);
                Check(a.LoadMessages().Single().AccountId == "synthetic-account-a");
                Check(a.ResolveDisplayName("room-a", "person-a") == "가상 발신자");
                NotificationPreview clone = a.LoadMessages().Single();
                clone.Message = "modified outside store";
                Check(a.LoadMessages().Single().Message != clone.Message);
                byte[] cipher = File.ReadAllBytes(a.StatePath);
                Check(Encoding.UTF8.GetString(cipher).IndexOf("synthetic", StringComparison.Ordinal) < 0);
                byte[] json = ProtectedData.Unprotect(cipher, Encoding.UTF8.GetBytes("Spoiler.AccountInbox.v1"), DataProtectionScope.CurrentUser);
                string serialized = Encoding.UTF8.GetString(json);
                Check(serialized.Contains("synthetic-account-a") && serialized.Contains("synthetic body 100"));
                Check(serialized.Split(new[] { "AccountId" }, StringSplitOptions.None).Length == 2);
                Array.Clear(json, 0, json.Length);
            }
            using (InboxStore b = new InboxStore(Area("accounts"), "synthetic-account-b"))
            {
                Check(b.StatePath != aPath && b.LoadMessages().Count == 0 && b.GetCatchUpCursors(512).Count == 0);
                NotificationPreview stale = Message("200"); stale.AccountId = "synthetic-account-a";
                Check(!b.CommitMessage(stale, "room-a", "person-a", true).Saved);
                Check(b.CommitMessage(Message("201"), "room-b", "person-b", true).Saved);
            }
            using (InboxStore a = new InboxStore(Area("accounts"), "synthetic-account-a"))
            {
                Check(a.LoadMessages().Single().Id == "100" && Cursor(a, "room-a") == "100");
                InboxCommitResult replay = a.CommitMessage(Message("100"), "room-a", "person-a", true);
                Check(replay.Saved && !replay.IsNew && a.LoadMessages().Count == 1);
            }
        }

        private static void FailedCommit()
        {
            using (InboxStore store = new InboxStore(Area("failure"), "a"))
            {
                Check(store.CommitMessage(Message("100"), "room", "author", true).Saved);
                byte[] before = File.ReadAllBytes(store.StatePath);
                using (FileStream locked = new FileStream(store.StatePath, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
                {
                    InboxCommitResult failed = store.CommitMessage(Message("200"), "room", "author", true);
                    Check(failed.IsNew && !failed.Saved && !store.PersistenceAvailable);
                    Check(store.LoadMessages().Count == 1 && Cursor(store, "room") == "100");
                    Check(!store.CommitProgress("room", "200"));
                    Check(!File.Exists(store.StatePath + ".tmp"));
                }
                Check(before.SequenceEqual(File.ReadAllBytes(store.StatePath)));
                using (InboxStore restarted = new InboxStore(Area("failure"), "a"))
                    Check(restarted.LoadMessages().Single().Id == "100" && Cursor(restarted, "room") == "100");
                InboxCommitResult retried = store.CommitMessage(Message("200"), "room", "author", true);
                Check(retried.IsNew && retried.Saved && store.PersistenceAvailable);
                Check(Cursor(store, "room") == "200" && store.LoadMessages().Count == 2);
            }
            // A failed first write must not manufacture a cursor either.
            using (InboxStore store = new InboxStore(Area("first-failure"), "a"))
            {
                Directory.CreateDirectory(store.StatePath + ".tmp");
                Check(!store.CommitMessage(Message("1"), "room", "author", true).Saved);
                Check(store.GetCatchUpCursors(512).Count == 0 && store.LoadMessages().Count == 0);
                Directory.Delete(store.StatePath + ".tmp");
                Check(store.CommitMessage(Message("1"), "room", "author", true).IsNew);
            }
        }

        private static void Corruption()
        {
            string aPath;
            string bPath;
            using (InboxStore a = new InboxStore(Area("corrupt"), "a"))
            { Check(a.CommitMessage(Message("1"), "room", "author", true).Saved); aPath = a.StatePath; }
            using (InboxStore b = new InboxStore(Area("corrupt"), "b")) { bPath = b.StatePath; }
            File.Copy(aPath, bPath);
            byte[] before = File.ReadAllBytes(bPath);
            using (InboxStore b = new InboxStore(Area("corrupt"), "b"))
            {
                Check(b.HasLoadError && !b.PersistenceAvailable && b.LoadMessages().Count == 0);
                Check(!b.CommitMessage(Message("2"), "room", "author", true).Saved && !b.ClearHistory());
            }
            Check(before.SequenceEqual(File.ReadAllBytes(bPath)));
            File.WriteAllBytes(bPath, new byte[] { 1, 2, 3, 4 });
            using (InboxStore b = new InboxStore(Area("corrupt"), "b"))
            { Check(b.HasLoadError && !b.CommitMessage(Message("3"), "room", "author", true).Saved); }
            Check(File.ReadAllBytes(bPath).SequenceEqual(new byte[] { 1, 2, 3, 4 }));
        }

        private static void Retention()
        {
            DateTime now = DateTime.UtcNow;
            List<NotificationPreview> messages = Enumerable.Range(0, InboxStore.MaxMessages + 10).Select(index => Message(index.ToString(), now.AddMinutes(-index))).ToList();
            string[] forward = InboxStore.RetainMessages(messages).Select(item => item.Id).ToArray();
            messages.Reverse();
            Check(forward.SequenceEqual(InboxStore.RetainMessages(messages).Select(item => item.Id)) && forward.Length == InboxStore.MaxMessages);
            messages = Enumerable.Range(0, InboxStore.MaxMessages + 10).Select(index => Message(index.ToString(), now)).ToList();
            forward = InboxStore.RetainMessages(messages).Select(item => item.Id).ToArray();
            messages.Reverse();
            Check(forward.SequenceEqual(InboxStore.RetainMessages(messages).Select(item => item.Id)));
            using (InboxStore store = new InboxStore(Area("retention"), "a"))
            {
                for (int first = 0; first < InboxStore.MaxMessages; first += InboxStore.MaxCommitBatch)
                {
                    int batchSize = Math.Min(InboxStore.MaxCommitBatch, InboxStore.MaxMessages - first);
                    var batch = Enumerable.Range(first, batchSize).Select(index => new InboxMessageWrite {
                        Preview = Message((1000 + index).ToString(), now.AddMinutes(-index)), ChatId = "room",
                        AuthorId = "author", AdvanceCursor = true }).ToList();
                    Check(store.CommitMessages(batch).All(result => result.Saved && result.IsNew));
                }
                Check(store.CommitMessage(Message("10", now.AddDays(-2)), "room", "author", true).Saved);
                Check(store.LoadMessages().Count == InboxStore.MaxMessages && store.LoadMessages().First().Id == "1000" && !store.LoadMessages().Any(item => item.Id == "10"));
                Check(Cursor(store, "room") == (1000 + InboxStore.MaxMessages - 1).ToString());
            }
            using (InboxStore restarted = new InboxStore(Area("retention"), "a"))
                Check(restarted.LoadMessages().Count == InboxStore.MaxMessages && !restarted.HasLoadError);
            // Another room's recovered morning message must survive the former
            // 80-row ceiling when a busy room has filled that old limit.
            using (InboxStore store = new InboxStore(Area("cross-room-retention"), "a"))
            {
                for (int first = 0; first < 80; first += 40)
                    Check(store.CommitMessages(Enumerable.Range(first, 40).Select(index => new InboxMessageWrite {
                        Preview = Message((1000 + index).ToString(), now.AddMinutes(-index)), ChatId = "busy-room",
                        AuthorId = "author", AdvanceCursor = true }).ToList()).All(result => result.Saved));
                Check(store.CommitMessage(Message("10", now.AddHours(-10)), "other-room", "other-author", true).Saved);
                Check(store.LoadMessages().Count == 81 && store.LoadMessages().Any(item => item.Id == "10"));
            }
        }

        private static void Clear()
        {
            using (InboxStore store = new InboxStore(Area("clear"), "a"))
            {
                Check(store.CommitMessage(Message("100"), "room", "author", true).Saved);
                File.Copy(store.StatePath, store.StatePath + ".tmp");
                string unrelated = Path.Combine(Path.GetDirectoryName(store.StatePath), "unrelated.tmp");
                File.WriteAllText(unrelated, "keep");
                Check(store.ClearHistory() && !File.Exists(store.StatePath + ".tmp") && File.Exists(unrelated));
                Check(store.LoadMessages().Count == 0 && Cursor(store, "room") == "100");
                Check(store.ResolveDisplayName("room", "author") == null);
                InboxCommitResult duplicate = store.CommitMessage(Message("100"), "room", "author", true);
                Check(duplicate.Saved && !duplicate.IsNew && store.LoadMessages().Count == 0);
                Check(store.CommitMessage(Message("101"), "room", "author", true).Saved && store.LoadMessages().Count == 1);
                using (FileStream locked = new FileStream(store.StatePath, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
                    Check(!store.ClearHistory() && store.LoadMessages().Count == 1);
            }
            using (InboxStore store = new InboxStore(Area("clear"), "a"))
                Check(store.LoadMessages().Single().Id == "101" && Cursor(store, "room") == "101");
        }

        private static void Expiry()
        {
            DateTime now = DateTime.UtcNow;
            using (InboxStore store = new InboxStore(Area("expiry"), "a", () => now))
            {
                Check(store.CommitMessage(Message("1", now), "room", "author", true).Saved);
                now = now.AddDays(8);
                Check(store.LoadMessages().Count == 0 && store.GetCatchUpCursors(512).Count == 0 && store.ResolveDisplayName("room", "author") == null);
                Check(store.Prune());
                Check(store.CommitMessage(Message("1", now), "room", "author", true).IsNew);
            }
        }

        private static void DuplicateProgress()
        {
            using (InboxStore store = new InboxStore(Area("progress"), "a"))
            {
                Check(store.CommitMessage(Message("100"), "room", "author", true).Saved);
                Check(store.CommitMessage(Message("200"), "room", "author", false).Saved && Cursor(store, "room") == "100");
                Check(!store.CommitProgress("room", "150") && Cursor(store, "room") == "100");
                InboxCommitResult duplicate = store.CommitMessage(Message("200"), "room", "author", true);
                Check(!duplicate.IsNew && duplicate.Saved && Cursor(store, "room") == "200");
                Check(store.CommitMessage(Message("300"), "room", "author", false).Saved);
                Check(store.CommitProgress("room", "300") && Cursor(store, "room") == "300");
                Check(store.CommitProgress("room", "100") && Cursor(store, "room") == "300");
                Check(store.CommitMessage(Message("184467440737095516160000"), "room", "author", true).Saved);
                Check(Cursor(store, "room") == "184467440737095516160000");
            }
        }


        private static void DecimalCursorOrdering()
        {
            string directory = Area("decimal-order");
            const string lower = "099999999999999999999999999999999999999";
            const string higher = "100000000000000000000000000000000000000";
            using (InboxStore store = new InboxStore(directory, "a"))
            {
                Check(store.CommitMessage(Message(lower), "room", "author", true).Saved);
                Check(store.CommitMessage(Message(higher), "room", "author", true).Saved);
                Check(store.CommitMessage(Message(lower.TrimStart('0')), "room", "author", true).Saved);
                Check(Cursor(store, "room") == higher);
                Check(store.CommitMessage(Message("000100"), "zero-room", "author", true).Saved);
                Check(store.CommitMessage(Message("99"), "zero-room", "author", true).Saved);
                Check(Cursor(store, "zero-room") == "000100");
                Check(!store.CommitProgress("zero-room", "١٠١"));
                Check(!store.CommitProgress("zero-room", "101e1"));
                Check(!store.CommitProgress("zero-room", new string('1', 257)));
            }
            using (InboxStore store = new InboxStore(directory, "a"))
                Check(Cursor(store, "room") == higher && Cursor(store, "zero-room") == "000100");
        }

        private static void MetadataBounds()
        {
            DateTime now = DateTime.UtcNow;
            using (InboxStore store = new InboxStore(Area("metadata-bounds"), "a", () => now))
            {
                for (int first = 0; first < 2100; first += InboxStore.MaxCommitBatch)
                {
                    now = now.AddSeconds(1);
                    var batch = Enumerable.Range(first, InboxStore.MaxCommitBatch).Select(index => {
                        NotificationPreview preview = Message(index.ToString(), now);
                        preview.Sender = "  이름\r\n";
                        return new InboxMessageWrite { Preview = preview, ChatId = "room-" + index,
                            AuthorId = "author-" + index, AdvanceCursor = true };
                    }).ToList();
                    Check(store.CommitMessages(batch).All(result => result.Saved && result.IsNew));
                }
                byte[] cipher = File.ReadAllBytes(store.StatePath);
                string visible = Encoding.UTF8.GetString(cipher);
                Check(!visible.Contains("room-2099") && !visible.Contains("author-2099") && !visible.Contains("synthetic body"));
                byte[] plain = ProtectedData.Unprotect(cipher, Encoding.UTF8.GetBytes("Spoiler.AccountInbox.v1"), DataProtectionScope.CurrentUser);
                string json = Encoding.UTF8.GetString(plain);
                Array.Clear(plain, 0, plain.Length);
                string seen = Regex.Match(json, "\"Seen\":\\[(.*?)\\],\"Cursors\":", RegexOptions.Singleline).Groups[1].Value;
                string cursors = Regex.Match(json, "\"Cursors\":\\[(.*?)\\],\"Names\":", RegexOptions.Singleline).Groups[1].Value;
                string names = Regex.Match(json, "\"Names\":\\[(.*?)\\]", RegexOptions.Singleline).Groups[1].Value;
                Check(seen.Split(new[] { "\"Key\":" }, StringSplitOptions.None).Length - 1 == 2048);
                Check(cursors.Split(new[] { "\"ChatId\":" }, StringSplitOptions.None).Length - 1 == 512);
                Check(names.Split(new[] { "\"AuthorId\":" }, StringSplitOptions.None).Length - 1 == 1024);
                Check(store.LoadMessages().Count == InboxStore.MaxMessages && store.GetCatchUpCursors(512).Count == 512);
                Check(store.ResolveDisplayName("room-0", "author-0") == null);
                Check(store.ResolveDisplayName("room-2099", "author-2099") == "이름");
                Check(!store.CommitMessage(Message("2099", now), "room-2099", "author-2099", true).IsNew);
                Check(store.CommitMessage(Message("0", now), "room-0", "author-0", true).IsNew);
            }
        }

        private static void AccountIdentityRequired()
        {
            string directory = Area("invalid-owner");
            foreach (string owner in new[] { null, "", " ", new string('a', 257) })
            {
                bool rejected = false;
                try { using (InboxStore unused = new InboxStore(directory, owner)) { } }
                catch (ArgumentException) { rejected = true; }
                Check(rejected);
            }
            Check(!Directory.Exists(directory));
            using (InboxStore store = new InboxStore(directory, " a "))
                Check(store.AccountId == "a" && store.CommitMessage(Message("1"), "room", "author", true).Saved);
            using (InboxStore store = new InboxStore(directory, "a"))
                Check(store.LoadMessages().Single().Id == "1");
        }

        private static void Legacy()
        {
            string directory = Area("legacy"); Directory.CreateDirectory(directory);
            string history = Path.Combine(directory, "history.dat");
            File.WriteAllText(history, "unowned history fixture");
            using (InboxStore store = new InboxStore(directory, "a"))
                Check(store.LoadMessages().Count == 0 && store.CommitMessage(Message("1"), "room", "author", true).Saved);
            Check(File.ReadAllText(history) == "unowned history fixture");
        }
    }
}
