using System;
using System.Collections;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

namespace Spoiler.Tests
{
    // These tests construct only synthetic stores and an unstarted Process
    // identity. They neither start a bridge nor inspect/control desktop UI.
    internal static class RoomMetadataTests
    {
        private const string Account = "123";
        private static string _root;
        private static int _failures;
        private static int _checks;
        private const BindingFlags Hidden = BindingFlags.Instance | BindingFlags.NonPublic;

        public static int Main()
        {
            _root = Path.Combine(Path.GetTempPath(), "SpoilerRoomMetadataTests-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_root);
            try
            {
                Run("strict room metadata contract preserves original Unicode", Protocol);
                Run("metadata updates every retained row and future own-only bodies", ExistingAndFuture);
                Run("same-priority rename succeeds and weaker names cannot clobber", NamePriority);
                Run("names survive encrypted restart and remain account scoped", AccountPersistence);
                Run("old inbox documents without room names migrate safely", LegacyDocument);
                Run("failed name commit rolls back metadata and replacement rows", FailedCommit);
                Run("bounded names expire without inventing message bodies", BoundsAndExpiry);
                Run("cleared history cannot be restored by late names or replay", ClearTombstones);
                Run("source publishes replacements only after durable name commit", SourcePublication);
                Run("source retries failed persistence without early publication", SourceFailedPublication);
                Run("clear and account changes reject stale process metadata", SourceGeneration);
            }
            catch (Exception error) { _failures++; Console.WriteLine("FAIL: setup " + error.GetType().Name + " " + error.Message); }
            finally
            {
                string allowed = Path.Combine(Path.GetFullPath(Path.GetTempPath()), "SpoilerRoomMetadataTests-");
                if (Path.GetFullPath(_root).StartsWith(allowed, StringComparison.OrdinalIgnoreCase))
                    try { Directory.Delete(_root, true); } catch { }
            }
            Console.WriteLine((_failures == 0 ? "PASS" : "FAIL") + ": room metadata tests (11 scenarios, " + _checks + " checks)");
            return _failures == 0 ? 0 : 1;
        }

        private static void Run(string label, Action test)
        {
            try { test(); Console.WriteLine("PASS: " + label); }
            catch (Exception error) { _failures++; Console.WriteLine("FAIL: " + label + " " + error.GetType().Name + " " + error.Message); }
        }
        private static void Check(bool condition, string label)
        {
            _checks++;
            if (!condition) throw new InvalidOperationException(label);
        }
        private static string Area(string name) { return Path.Combine(_root, name); }
        private static string Frame(string room, string title, string source)
        {
            return "{\"type\":\"room_metadata\",\"chatId\":\"" + room + "\",\"chatTitle\":\"" + title
                + "\",\"titleSource\":\"" + source + "\"}";
        }
        private static NotificationPreview Row(string room, string id, string body = null, bool mine = true)
        {
            return new NotificationPreview { Id = id, ChatId = room, AccountId = Account, Sender = mine ? "내 이름" : "상대 프로필",
                AuthorName = mine ? "내 이름" : "상대 프로필", Message = body ?? "synthetic body " + id, ReceivedAt = DateTime.UtcNow,
                IsMine = mine, MessageType = 1, GroupKey = NotificationGrouping.CreateServerGroupKey(room, mine ? Account : "222") };
        }
        private static void Seed(InboxStore store, string room, string id, bool mine = true)
        {
            Check(store.CommitMessage(Row(room, id, null, mine), room, mine ? Account : "222", true).Saved, "seed commits");
        }

        private static void Protocol()
        {
            LocoBridgeEvent parsed;
            const string raw = "  Ａ 가 👩‍🏫 모임  ";
            Check(LocoMessageSource.TryParseBridgeLine(Frame("9223372036854775807", raw, "synced_room_name"), out parsed), "largest positive signed ID accepted");
            Check(parsed.ChatId == "9223372036854775807" && parsed.ChatTitle == raw && parsed.TitleSource == "synced_room_name", "original string survives");
            foreach (string room in new[] { "0", "01", " 1", "1 ", "+1", "-1", "1.0", "1e3", "9223372036854775808", "１２３" })
                Check(!LocoMessageSource.TryParseBridgeLine(Frame(room, "valid", "server_title"), out parsed), "noncanonical ID rejected");
            foreach (string title in new[] { "", " ", "bad\\nname", new string('a', 257), "bad\\uD800" })
                Check(!LocoMessageSource.TryParseBridgeLine(Frame("101", title, "server_title"), out parsed), "unsafe name rejected");
            Check(!LocoMessageSource.TryParseBridgeLine(Frame("101", "valid", "profile_name"), out parsed), "unknown provenance rejected");
            Check(!LocoMessageSource.TryParseBridgeLine("{\"type\":\"room_metadata\",\"chatId\":101,\"chatTitle\":\"valid\",\"titleSource\":\"server_title\"}", out parsed), "numeric JSON ID rejected");
            foreach (string source in new[] { "synced_friend_name", "server_title", "participant_names" })
                Check(LocoMessageSource.TryParseBridgeLine(Frame("101", "valid", source), out parsed), "allowed provenance accepted");
            string validFrame = Frame("101", "valid", "server_title");
            Check(LocoMessageSource.TryParseBridgeLine(validFrame.TrimEnd('}') + ",\"authoritative\":true}", out parsed) && parsed.Authoritative, "fresh snapshot requires boolean true");
            foreach (string wrong in new[] { "\"true\"", "1", "null", "{}" })
                Check(!LocoMessageSource.TryParseBridgeLine(validFrame.TrimEnd('}') + ",\"authoritative\":" + wrong + "}", out parsed), "nonboolean authority rejected");
        }

        private static void ExistingAndFuture()
        {
            using (InboxStore store = new InboxStore(Area("existing"), Account))
            {
                Seed(store, "101", "10"); Seed(store, "101", "11", false); Seed(store, "202", "20");
                NotificationPreview[] before = store.LoadMessages().ToArray();
                InboxRoomMetadataResult named = store.CommitRoomMetadata("101", "  Ａ반 가 👩‍🏫  ", "synced_room_name");
                Check(named.Saved && named.Updates.Count == 2, "all exact room rows updated");
                Check(named.Updates.All(row => row.ChatId == "101" && row.RoomTitle == "  Ａ반 가 👩‍🏫  " && row.RoomTitleSource == "synced_room_name"), "same title and source on each row");
                Check(before.All(old => store.LoadMessages().Any(row => row.Id == old.Id && row.Message == old.Message && row.ReceivedAt == old.ReceivedAt
                    && row.GroupKey == old.GroupKey && row.IsMine == old.IsMine)), "no body ordering identity changes");
                Check(store.LoadMessages().Single(row => row.ChatId == "202").RoomTitle == null, "another room untouched");
                NotificationPreview future = Row("101", "12"); future.RoomTitle = "상대 프로필"; future.RoomTitleSource = "server_title";
                Check(store.CommitMessage(future, "101", Account, true).SavedPreview.RoomTitle == "  Ａ반 가 👩‍🏫  ", "future own-only body inherits verified name");
                NotificationPreview duplicate = Row("101", "10"); duplicate.RoomTitle = "다른 제목";
                Check(store.CommitMessage(duplicate, "101", Account, true).SavedPreview.RoomTitle == "  Ａ반 가 👩‍🏫  ", "replay title cannot clobber personal name");
            }
        }

        private static void NamePriority()
        {
            using (InboxStore store = new InboxStore(Area("priority"), Account))
            {
                Seed(store, "101", "10");
                foreach (string source in new[] { "participant_names", "server_title", "synced_friend_name", "synced_room_name" })
                {
                    Check(store.CommitRoomMetadata("101", source, source).Saved, "priority upgrade persists");
                    Check(store.ResolveRoomMetadata("101").TitleSource == source, "stronger source chosen");
                }
                Check(store.CommitRoomMetadata("101", "새 사용자 지정 이름", "synced_room_name").Updates.Count == 1, "same-priority rename updates");
                int writes = store.SnapshotWriteCount;
                Check(store.CommitRoomMetadata("101", "낮은 이름", "synced_friend_name").Saved, "weaker source ignored safely");
                Check(store.CommitRoomMetadata("101", "새 사용자 지정 이름", "synced_room_name").Saved, "duplicate accepted as no-op");
                Check(store.SnapshotWriteCount == writes && store.ResolveRoomMetadata("101").ChatTitle == "새 사용자 지정 이름", "weaker and duplicate snapshots do not write");
                Check(!store.CommitRoomMetadata("101", "", "synced_room_name").Saved && store.LoadMessages().Single().RoomTitle == "새 사용자 지정 이름", "empty result does not erase known title");
                Check(store.CommitRoomMetadata("101", "현재 서버 제목", "server_title", true).Updates.Count == 1,
                    "fresh complete snapshot can reflect explicit personal-name removal");
                Check(store.ResolveRoomMetadata("101").ChatTitle == "현재 서버 제목" && store.LoadMessages().Single().RoomTitleSource == "server_title", "verified downgrade persists");
            }
        }

        private static void AccountPersistence()
        {
            string directory = Area("account");
            using (InboxStore store = new InboxStore(directory, Account))
            {
                Check(store.CommitRoomMetadata("101", "  사용자 지정 이름  ", "synced_friend_name").Saved, "metadata saves without body");
                Check(store.LoadMessages().Count == 0, "metadata is not a message");
                string disk = Encoding.UTF8.GetString(File.ReadAllBytes(store.StatePath));
                Check(!disk.Contains("사용자 지정 이름") && !disk.Contains("synced_friend_name"), "names encrypted at rest");
            }
            using (InboxStore own = new InboxStore(directory, Account))
            using (InboxStore another = new InboxStore(directory, "456"))
            {
                Check(own.ResolveRoomMetadata("101").ChatTitle == "  사용자 지정 이름  ", "restart retains original text");
                Check(another.ResolveRoomMetadata("101") == null && another.LoadMessages().Count == 0, "different account receives no names");
            }
        }

        private static void LegacyDocument()
        {
            string path;
            using (InboxStore store = new InboxStore(Area("legacy"), Account)) { Seed(store, "101", "10"); path = store.StatePath; }
            byte[] entropy = Encoding.UTF8.GetBytes("Spoiler.AccountInbox.v1");
            byte[] clear = ProtectedData.Unprotect(File.ReadAllBytes(path), entropy, DataProtectionScope.CurrentUser);
            string json = Regex.Replace(Encoding.UTF8.GetString(clear), ",\"RoomNames\":\\[\\]", "");
            Check(!json.Contains("RoomNames"), "fixture models old optional-member absence");
            File.WriteAllBytes(path, ProtectedData.Protect(Encoding.UTF8.GetBytes(json), entropy, DataProtectionScope.CurrentUser));
            Array.Clear(clear, 0, clear.Length);
            using (InboxStore loaded = new InboxStore(Area("legacy"), Account))
            {
                Check(!loaded.HasLoadError && loaded.LoadMessages().Count == 1 && loaded.ResolveRoomMetadata("101") == null, "old document loads");
                Check(loaded.CommitRoomMetadata("101", "첫 이름", "server_title").Saved, "old document accepts room name upgrade");
            }
        }

        private static void FailedCommit()
        {
            using (InboxStore store = new InboxStore(Area("rollback"), Account))
            {
                Seed(store, "101", "10"); Check(store.CommitRoomMetadata("101", "기존 이름", "synced_room_name").Saved, "seed name");
                string blocker = store.StatePath + ".tmp";
                Directory.CreateDirectory(blocker);
                try
                {
                    InboxRoomMetadataResult failed = store.CommitRoomMetadata("101", "바뀐 이름", "synced_room_name");
                    Check(!failed.Saved && failed.Updates.Count == 0, "failure returns no publishable replacements");
                    Check(store.ResolveRoomMetadata("101").ChatTitle == "기존 이름" && store.LoadMessages().Single().RoomTitle == "기존 이름", "in-memory rollback");
                    using (InboxStore disk = new InboxStore(Area("rollback"), Account)) Check(disk.ResolveRoomMetadata("101").ChatTitle == "기존 이름", "disk unchanged");
                }
                finally { Directory.Delete(blocker); }
                Check(store.CommitRoomMetadata("101", "바뀐 이름", "synced_room_name").Saved, "retry succeeds");
            }
        }

        private static void BoundsAndExpiry()
        {
            DateTime clock = DateTime.UtcNow;
            using (InboxStore store = new InboxStore(Area("bounds"), Account, () => clock))
            {
                for (int index = 1; index <= InboxStore.MaxRoomNames + 8; index++)
                {
                    clock = clock.AddSeconds(1);
                    if (!store.CommitRoomMetadata(index.ToString(), "synthetic", "server_title").Saved) throw new InvalidOperationException("bound seed failed");
                }
                object document = typeof(InboxStore).GetField("_document", Hidden).GetValue(store);
                IList names = (IList)document.GetType().GetProperty("RoomNames").GetValue(document, null);
                Check(names.Count == InboxStore.MaxRoomNames && store.ResolveRoomMetadata("1") == null, "oldest room names bounded");
                Check(store.LoadMessages().Count == 0 && store.GetCatchUpCursors(512).Count == 0, "names add no body or cursor");
                clock = clock.AddDays(8);
                Check(store.Prune() && store.ResolveRoomMetadata("520") == null, "expired names removed");
            }
            DateTime activeClock = DateTime.UtcNow.AddDays(-6);
            using (InboxStore active = new InboxStore(Area("active-name"), Account, () => activeClock))
            {
                Check(active.CommitRoomMetadata("101", "저장 이름", "synced_room_name").Saved, "seed older active name");
                activeClock = DateTime.UtcNow;
                Seed(active, "101", "10");
                activeClock = activeClock.AddDays(2);
                Check(active.Prune() && active.ResolveRoomMetadata("101").ChatTitle == "저장 이름", "recent retained bodies keep an older name alive");
                Check(active.CommitRoomMetadata("101", "프로필", "participant_names").Saved
                    && active.LoadMessages().Single().RoomTitle == "저장 이름", "active name retains precedence beyond initial name age");
            }
            DateTime refreshClock = DateTime.UtcNow;
            using (InboxStore refresh = new InboxStore(Area("refresh-name"), Account, () => refreshClock))
            {
                Check(refresh.CommitRoomMetadata("101", "변함없는 이름", "synced_friend_name", true).Saved, "seed metadata before first message");
                int writes = refresh.SnapshotWriteCount;
                refreshClock = refreshClock.AddHours(12);
                Check(refresh.CommitRoomMetadata("101", "변함없는 이름", "synced_friend_name", true).Saved && refresh.SnapshotWriteCount == writes, "same-day refresh avoids snapshot write");
                for (int day = 0; day < 8; day++)
                {
                    refreshClock = refreshClock.AddDays(1);
                    InboxRoomMetadataResult refreshed = refresh.CommitRoomMetadata("101", "변함없는 이름", "synced_friend_name", true);
                    Check(refreshed.Saved && refreshed.Updates.Count == 0, "unchanged daily snapshot refreshes without UI event");
                }
                Check(refresh.ResolveRoomMetadata("101").ChatTitle == "변함없는 이름" && refresh.LoadMessages().Count == 0,
                    "authoritative refresh keeps name before first body without inventing body");
            }
        }

        private static void ClearTombstones()
        {
            using (InboxStore store = new InboxStore(Area("clear"), Account))
            {
                Seed(store, "101", "10"); store.CommitRoomMetadata("101", "기존 이름", "synced_room_name");
                Check(store.ClearHistory() && store.ResolveRoomMetadata("101") == null, "clear erases room names");
                InboxRoomMetadataResult late = store.CommitRoomMetadata("101", "뒤늦은 이름", "server_title");
                Check(late.Saved && late.Updates.Count == 0 && store.LoadMessages().Count == 0, "late metadata cannot recreate content");
                InboxCommitResult replay = store.CommitMessage(Row("101", "10"), "101", Account, true);
                Check(replay.Saved && !replay.IsNew && replay.SavedPreview == null && store.LoadMessages().Count == 0, "deleted replay remains a tombstone");
            }
        }

        private static CredentialVault Vault(string name)
        {
            CredentialVault vault = new CredentialVault(Area(name));
            vault.Save(new LocoCredentials { UserId = Account, OAuthToken = "synthetic-token", RefreshToken = "synthetic-refresh",
                DeviceUuid = "synthetic-device-1234567890", DeviceType = "tablet" });
            return vault;
        }
        private static object Field(object target, string name) { return target.GetType().GetField(name, Hidden).GetValue(target); }
        private static LocoMessageSource SyntheticSource(CredentialVault vault)
        {
            return new LocoMessageSource(vault, "synthetic-unused-node.exe", Path.Combine(_root, "synthetic-unused-bridge.mjs"));
        }
        private static void Set(object target, string name, object value) { target.GetType().GetField(name, Hidden).SetValue(target, value); }
        private static Process Attach(LocoMessageSource source)
        {
            source.LoadStoredMessages();
            Process identity = new Process();
            Set(source, "_process", identity); Set(source, "_processAccountId", source.CurrentAccountId); Set(source, "_stopping", false);
            return identity;
        }
        private static void Consume(LocoMessageSource source, Process process, string frame)
        {
            typeof(LocoMessageSource).GetMethod("ConsumeBridgeLine", Hidden).Invoke(source, new object[] { process, frame });
        }
        private static void Wait(Func<bool> ready)
        {
            Stopwatch watch = Stopwatch.StartNew();
            while (!ready()) { if (watch.ElapsedMilliseconds > 5000) throw new TimeoutException(); Thread.Sleep(20); }
        }

        private static void SourcePublication()
        {
            CredentialVault vault = Vault("publication");
            using (InboxStore seed = new InboxStore(vault.DataDirectory, Account)) { Seed(seed, "101", "10"); Seed(seed, "101", "11", false); }
            using (LocoMessageSource source = SyntheticSource(vault))
            {
                Process process = Attach(source);
                int count = 0; int captured = 0;
                source.PreviewCaptured += delegate(object sender, NotificationPreview row)
                {
                    using (InboxStore disk = new InboxStore(vault.DataDirectory, Account))
                        Check(disk.LoadMessages().Any(item => item.Id == row.Id && item.RoomTitle == row.RoomTitle && item.RoomTitleSource == row.RoomTitleSource), "replacement already durable");
                    count++;
                };
                source.StateChanged += delegate(object sender, CaptureStateChangedEventArgs args) { if (args.State == CaptureState.Captured) captured++; };
                Consume(source, process, Frame("101", "사용자 이름", "synced_friend_name"));
                Check(count == 2 && captured == 0 && source.LoadStoredMessages().Count == 2, "metadata produces only replacements");
                Consume(source, process, Frame("101", "사용자 이름", "synced_friend_name"));
                Check(count == 2, "duplicate frame does not republish");
                Set(source, "_process", null); process.Dispose();
            }
        }

        private static void SourceFailedPublication()
        {
            CredentialVault vault = Vault("source-failure");
            using (InboxStore seed = new InboxStore(vault.DataDirectory, Account)) Seed(seed, "101", "10");
            using (LocoMessageSource source = SyntheticSource(vault))
            {
                Process process = Attach(source);
                InboxStore inbox = (InboxStore)Field(source, "_inbox");
                string blocker = inbox.StatePath + ".tmp";
                int count = 0; Exception background = null;
                source.PreviewCaptured += delegate { Interlocked.Increment(ref count); };
                Directory.CreateDirectory(blocker);
                Thread thread = new Thread(delegate() { try { Consume(source, process, Frame("101", "새 이름", "synced_room_name")); } catch (Exception error) { background = error; } });
                thread.Start();
                try
                {
                    Wait(() => (bool)Field(source, "_storageBlocked"));
                    Check(Volatile.Read(ref count) == 0 && source.LoadStoredMessages().Single().RoomTitle == null, "failed persistence has no UI replacement");
                }
                finally { Directory.Delete(blocker); }
                Check(thread.Join(5000) && background == null && Volatile.Read(ref count) == 1, "ordered frame retries and publishes once");
                using (InboxStore disk = new InboxStore(vault.DataDirectory, Account)) Check(disk.LoadMessages().Single().RoomTitle == "새 이름", "retry persisted");
                Set(source, "_process", null); process.Dispose();
            }
        }

        private static void SourceGeneration()
        {
            CredentialVault vault = Vault("generation");
            using (InboxStore seed = new InboxStore(vault.DataDirectory, Account)) Seed(seed, "101", "10");
            using (LocoMessageSource source = SyntheticSource(vault))
            {
                Process old = Attach(source);
                int count = 0; source.PreviewCaptured += delegate { count++; };
                Check(source.ClearHistory(), "clear invalidates queued process");
                Consume(source, old, Frame("101", "늦은 이름", "synced_room_name"));
                Check(source.LoadStoredMessages().Count == 0 && ((InboxStore)Field(source, "_inbox")).ResolveRoomMetadata("101") == null && count == 0, "old generation cannot reinstall room name");
                Process previous = Attach(source);
                LocoCredentials changed = vault.Load(); changed.UserId = "456"; changed.DeviceUuid = "synthetic-device-4567890123"; vault.Save(changed);
                source.LoadStoredMessages();
                Consume(source, previous, Frame("101", "다른 계정 이름", "synced_room_name"));
                Check(source.CurrentAccountId == "456" && ((InboxStore)Field(source, "_inbox")).ResolveRoomMetadata("101") == null && count == 0, "old account frame rejected");
            }
        }
    }
}
