using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

namespace Spoiler.Tests
{
    internal static class ConversationSourceTests
    {
        private const string Account = "123";
        private static string _root;
        private static string _fixture;
        private static string _node;
        private static int _failures;

        public static int Main(string[] args)
        {
            _fixture = Path.GetFullPath(args.Length > 0 ? args[0] : "test/fixtures/conversation-source-fixture.mjs");
            _node = args.Length > 1 ? args[1] : "C:\\Program Files\\nodejs\\node.exe";
            _root = Path.Combine(Path.GetTempPath(), "SpoilerConversationSourceTests-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_root);
            try
            {
                Check(File.Exists(_fixture) && File.Exists(_node), "fixture runtime exists");
                Run("own history fills before a higher observed cursor and reconnect dedupes", RecoverAndReconnect);
                Run("clear history retains tombstones without replaying old rows", ClearHistory);
                Run("empty complete page releases only its room without a false anchor commit", EmptyComplete);
                Run("partial and unknown completion cannot release another room", PartialProtection);
                Run("completion waits behind durable message retry", DurableCompletion);
                Run("duplicate metadata publishes the saved enriched row once", MetadataPublication);
                Run("new exact member proof publishes a separate restored legacy row", LegacyMetadataPublication);
                Run("new unread range survives live arrival and restart", DiscoveredRoomReplay);
            }
            catch (Exception error) { _failures++; Console.WriteLine("FAIL: setup (" + error.GetType().Name + ") " + error.Message); }
            finally
            {
                // The target is an explicitly created unique fixture directory.
                try
                {
                    string expectedPrefix = Path.Combine(Path.GetFullPath(Path.GetTempPath()), "SpoilerConversationSourceTests-");
                    if (Path.GetFullPath(_root).StartsWith(expectedPrefix, StringComparison.OrdinalIgnoreCase)) Directory.Delete(_root, true);
                }
                catch { }
            }
            Console.WriteLine(_failures == 0 ? "PASS: conversation source tests (8 scenarios)" : "FAIL: conversation source scenarios=" + _failures);
            return _failures == 0 ? 0 : 1;
        }

        private static void Run(string name, Action action)
        {
            try { action(); Console.WriteLine("PASS: " + name); }
            catch (Exception error) { _failures++; Console.WriteLine("FAIL: " + name + " (" + error.GetType().Name + ") " + error.Message); }
        }
        private static void Check(bool value, string detail) { if (!value) throw new InvalidOperationException(detail); }
        private static void Wait(Func<bool> condition, int milliseconds = 6000)
        {
            Stopwatch time = Stopwatch.StartNew();
            while (!condition()) { if (time.ElapsedMilliseconds >= milliseconds) throw new TimeoutException(); Thread.Sleep(25); }
        }
        private static T Field<T>(object source, string name)
        {
            return (T)source.GetType().GetField(name, BindingFlags.Instance | BindingFlags.NonPublic).GetValue(source);
        }
        private static bool Protected(LocoMessageSource source, string room)
        {
            lock (Field<object>(source, "_sync")) return Field<HashSet<string>>(source, "_catchUpProtectedChats").Contains(room);
        }
        private static CredentialVault Vault(string name, string mode)
        {
            CredentialVault vault = new CredentialVault(Path.Combine(_root, name));
            vault.Save(new LocoCredentials { UserId = Account, OAuthToken = "fixture:" + mode,
                RefreshToken = "fixture-refresh", DeviceUuid = "fixture-device-1234567890", DeviceType = "tablet" });
            return vault;
        }
        private static NotificationPreview Row(string room, string id, string title = "fixture group")
        {
            return new NotificationPreview { Id = id, AccountId = Account, Sender = "fixture sender", AuthorName = "fixture sender",
                Message = "synthetic " + room + "/" + id, ReceivedAt = DateTime.UtcNow.AddMilliseconds(-3000 + int.Parse(id) * 10),
                GroupKey = NotificationGrouping.CreateServerGroupKey(room, "222"), ChatId = room, RoomTitle = title,
                RoomTitleSource = "server_title", MessageType = 1 };
        }
        private static void Seed(CredentialVault vault, string room, params string[] ids)
        {
            using (InboxStore inbox = new InboxStore(vault.DataDirectory, Account))
                foreach (string id in ids) Check(inbox.CommitMessage(Row(room, id), room, "222", true).Saved, "seed body");
        }
        private static LocoMessageSource Source(CredentialVault vault)
        {
            LocoMessageSource source = new LocoMessageSource(vault, _node, _fixture);
            source.StateChanged += delegate(object sender, CaptureStateChangedEventArgs args)
            { if (args.State == CaptureState.Error) Console.WriteLine("EVIDENCE: synthetic source error=" + args.Detail); };
            return source;
        }
        private static string Cursor(CredentialVault vault, string room)
        {
            using (InboxStore inbox = new InboxStore(vault.DataDirectory, Account))
            {
                MessageCatchUpCursor cursor = inbox.GetCatchUpCursors(80).FirstOrDefault(item => item.ChatId == room);
                return cursor == null ? null : cursor.LogId;
            }
        }
        private static string StartsPath(CredentialVault vault)
        {
            return Path.Combine(vault.DataDirectory, "bridge-state", "conversation-starts.jsonl");
        }
        private static string[] Starts(CredentialVault vault)
        {
            string path = StartsPath(vault);
            try { return File.Exists(path) ? File.ReadAllLines(path) : new string[0]; } catch { return new string[0]; }
        }
        private static void Emit(LocoMessageSource source, string events)
        {
            Process process = Field<Process>(source, "_process");
            lock (process) LocoBridgeLauncher.WriteUtf8Line(process, "{\"type\":\"fixture_emit\",\"events\":[" + events + "]}");
        }
        private static string Message(string room, string id, bool recovered = false, string title = "fixture group")
        {
            return "{\"type\":\"message\",\"chatId\":\"" + room + "\",\"logId\":\"" + id + "\",\"authorId\":\"222\","
                + "\"authorName\":\"fixture sender\",\"chatTitle\":\"" + title + "\",\"message\":\"synthetic " + room + "/" + id
                + "\",\"messageType\":1,\"sentAt\":\"" + DateTime.UtcNow.ToString("o") + "\",\"recovered\":" + (recovered ? "true" : "false") + "}";
        }
        private static string Complete(string room, bool complete = true)
        {
            return "{\"type\":\"conversation_synced\",\"chatId\":\"" + room + "\",\"complete\":" + (complete ? "true" : "false") + "}";
        }

        private static void DiscoveredRoomReplay()
        {
            CredentialVault vault = Vault("discovered", "hold");
            using (LocoMessageSource source = Source(vault))
            {
                source.Start(); Wait(() => Starts(vault).Length == 1);
                Emit(source, "{\"type\":\"unread_recovery_anchor\",\"chatId\":\"202\",\"logId\":\"0\",\"throughLogId\":\"106\"}");
                Wait(() => {
                    using (InboxStore disk = new InboxStore(vault.DataDirectory, Account))
                        return disk.GetConversationSyncCursors(512).Any(row => row.ChatId == "202" && row.LogId == "0");
                });
                Emit(source, Message("202", "500") + "," + Message("202", "101", true));
                Wait(() => source.LoadStoredMessages().Count == 2);
                source.Stop(); source.Start();
                Wait(() => Starts(vault).Length == 2);
                Check(Starts(vault)[1].Contains("\"afterLogId\":\"0\""), "unfinished range survives the newer live message");
                string events = string.Join(",", Enumerable.Range(102, 5).Select(id => Message("202", id.ToString(), true)));
                Emit(source, events + "," + Complete("202"));
                Wait(() => source.LoadStoredMessages().Count == 7 && !Protected(source, "202"));
                using (InboxStore disk = new InboxStore(vault.DataDirectory, Account))
                    Check(disk.GetConversationSyncCursors(512).Single().LogId == "100", "completed range uses real stored bodies");
            }
        }

        private static void RecoverAndReconnect()
        {
            CredentialVault vault = Vault("recover", "recover");
            Seed(vault, "101", "100", "102");
            Check(Cursor(vault, "101") == "102", "old observed maximum is higher than the missing own body");
            using (LocoMessageSource source = Source(vault))
            {
                int count = 0, failures = 0;
                source.PreviewCaptured += delegate(object sender, NotificationPreview preview)
                {
                    using (InboxStore disk = new InboxStore(vault.DataDirectory, Account))
                        if (preview.Id != "101" || !preview.IsMine || preview.ChatId != "101"
                            || !disk.LoadMessages().Any(item => item.Id == preview.Id && item.ChatId == preview.ChatId && item.IsMine))
                            Interlocked.Increment(ref failures);
                    Interlocked.Increment(ref count);
                };
                source.Start(); Wait(() => Volatile.Read(ref count) == 1 && !Protected(source, "101"));
                Check(Starts(vault).Single().Contains("\"afterLogId\":\"99\""), "restart scans before oldest retained body");
                Check(source.LoadStoredMessages().Count == 3 && Volatile.Read(ref failures) == 0, "own and peer bodies share room with durable publication");
                source.Stop(); source.Start();
                Wait(() => Starts(vault).Length == 2 && !Protected(source, "101"));
                Check(Volatile.Read(ref count) == 1 && source.LoadStoredMessages().Count == 3, "reconnect emits no duplicate bubbles");
                Check(Starts(vault)[1].Contains("\"afterLogId\":\"99\""), "reconnect anchor comes from retained window");
                source.Stop();
            }
        }
        private static void ClearHistory()
        {
            CredentialVault vault = Vault("clear", "recover"); Seed(vault, "101", "100", "102");
            using (LocoMessageSource source = Source(vault))
            {
                int count = 0; source.PreviewCaptured += delegate { Interlocked.Increment(ref count); };
                source.Start(); Wait(() => Volatile.Read(ref count) == 1 && !Protected(source, "101"));
                Check(source.ClearHistory(), "clear history succeeds");
                Wait(() => Starts(vault).Length == 2 && !Protected(source, "101"));
                Check(Starts(vault)[1].Contains("\"afterLogId\":\"102\""), "bodyless room starts after its deletion tombstone");
                Check(source.LoadStoredMessages().Count == 0 && Volatile.Read(ref count) == 1, "deleted rows stay deleted");
                Emit(source, Message("101", "103")); Wait(() => Volatile.Read(ref count) == 2);
                Check(source.LoadStoredMessages().Single().Id == "103" && Cursor(vault, "101") == "103", "new post-clear messages are accepted");
                source.Stop();
            }
        }
        private static void EmptyComplete()
        {
            CredentialVault vault = Vault("empty", "empty"); Seed(vault, "101", "100");
            using (LocoMessageSource source = Source(vault))
            {
                int errors = 0;
                source.StateChanged += delegate(object sender, CaptureStateChangedEventArgs args)
                { if (args.State == CaptureState.Error) Interlocked.Increment(ref errors); };
                source.Start(); Wait(() => Starts(vault).Length == 1 && !Protected(source, "101"));
                Check(Cursor(vault, "101") == "100" && Volatile.Read(ref errors) == 0, "nonexistent 99 anchor is never committed");
                Emit(source, Message("101", "102")); Wait(() => Cursor(vault, "101") == "102");
                Check(source.LoadStoredMessages().Count == 2, "subsequent live cursor is no longer protected");
                source.Stop();
            }
        }
        private static void PartialProtection()
        {
            CredentialVault vault = Vault("partial", "hold"); Seed(vault, "101", "100"); Seed(vault, "202", "100");
            using (LocoMessageSource source = Source(vault))
            {
                source.Start(); Wait(() => Starts(vault).Length == 1);
                Check(Protected(source, "101") && Protected(source, "202"), "both seeded rooms begin protected");
                Emit(source, Complete("101", false) + "," + Complete("999") + ","
                    + "{\"type\":\"status\",\"status\":\"catchup_completed\",\"partial\":true}," + Message("202", "101"));
                Wait(() => source.LoadStoredMessages().Any(item => item.ChatId == "202" && item.Id == "101"));
                Check(Protected(source, "101") && Protected(source, "202") && Cursor(vault, "202") == "100", "partial/global/unknown markers cannot release rooms");
                Emit(source, Complete("101") + "," + Message("101", "103") + "," + Message("202", "103"));
                Wait(() => source.LoadStoredMessages().Count == 5);
                Check(!Protected(source, "101") && Protected(source, "202"), "only completed room is released");
                Check(Cursor(vault, "101") == "103" && Cursor(vault, "202") == "100", "other room cursor remains protected");
                source.Stop();
            }
        }
        private static void DurableCompletion()
        {
            CredentialVault vault = Vault("blocked", "blocked"); Seed(vault, "101", "100");
            using (LocoMessageSource source = Source(vault))
            {
                source.LoadStoredMessages();
                InboxStore inbox = Field<InboxStore>(source, "_inbox");
                string blocker = inbox.StatePath + ".tmp";
                int count = 0; source.PreviewCaptured += delegate { Interlocked.Increment(ref count); };
                Directory.CreateDirectory(blocker);
                try
                {
                    source.Start(); Wait(() => Field<bool>(source, "_storageBlocked"));
                    Check(Protected(source, "101") && Volatile.Read(ref count) == 0 && Cursor(vault, "101") == "100", "marker cannot overtake failed durable body");
                }
                finally { Directory.Delete(blocker); }
                Wait(() => Volatile.Read(ref count) == 1 && !Protected(source, "101"));
                Check(source.LoadStoredMessages().Count == 2 && Cursor(vault, "101") == "101", "retry commits body before releasing room");
                source.Stop();
            }
        }
        private static void MetadataPublication()
        {
            CredentialVault vault = Vault("metadata", "hold");
            using (InboxStore inbox = new InboxStore(vault.DataDirectory, Account))
                Check(inbox.CommitMessage(Row("101", "100", null), "101", "222", true).Saved, "seed untitled row");
            using (LocoMessageSource source = Source(vault))
            {
                int count = 0; NotificationPreview saved = null;
                source.PreviewCaptured += delegate(object sender, NotificationPreview preview) { saved = preview; Interlocked.Increment(ref count); };
                source.Start(); Wait(() => Starts(vault).Length == 1);
                Emit(source, Message("101", "100", true, "actual group title")); Wait(() => Volatile.Read(ref count) == 1);
                Check(saved != null && saved.RoomTitle == "actual group title" && saved.ChatId == "101"
                    && source.LoadStoredMessages().Count == 1 && source.LoadStoredMessages().Single().RoomTitle == saved.RoomTitle,
                    "published metadata is the durable enriched row, not a duplicate body");
                Emit(source, Message("101", "100", true, "actual group title") + "," + Complete("101"));
                Wait(() => !Protected(source, "101")); Check(Volatile.Read(ref count) == 1, "unchanged metadata is not republished");
                source.Stop();
            }
        }
        private static void LegacyMetadataPublication()
        {
            CredentialVault vault = Vault("legacy-metadata", "hold"); Seed(vault, "101", "100");
            string statePath;
            using (InboxStore inbox = new InboxStore(vault.DataDirectory, Account)) { statePath = inbox.StatePath; }
            // Construct an old schema record only inside this synthetic inbox.
            // Leave the replay identity, but remove exact member/name proof.
            byte[] entropy = Encoding.UTF8.GetBytes("Spoiler.AccountInbox.v1");
            byte[] clear = ProtectedData.Unprotect(File.ReadAllBytes(statePath), entropy, DataProtectionScope.CurrentUser);
            try
            {
                Type documentType = typeof(InboxStore).GetNestedType("InboxDocument", BindingFlags.NonPublic);
                DataContractJsonSerializer serializer = new DataContractJsonSerializer(documentType);
                object document;
                using (MemoryStream stream = new MemoryStream(clear)) document = serializer.ReadObject(stream);
                NotificationPreview legacy = ((IList<NotificationPreview>)documentType.GetProperty("Messages").GetValue(document, null)).Single();
                legacy.ChatId = null; legacy.RoomTitle = null; legacy.AuthorName = null;
                ((System.Collections.IList)documentType.GetProperty("Names").GetValue(document, null)).Clear();
                using (MemoryStream stream = new MemoryStream())
                {
                    serializer.WriteObject(stream, document);
                    File.WriteAllBytes(statePath, ProtectedData.Protect(stream.ToArray(), entropy, DataProtectionScope.CurrentUser));
                }
            }
            finally { Array.Clear(clear, 0, clear.Length); }
            using (LocoMessageSource source = Source(vault))
            {
                Check(source.LoadStoredMessages().Single().ChatId == null, "legacy row has no recoverable member proof at startup");
                int count = 0, failures = 0;
                List<string> published = new List<string>();
                source.PreviewCaptured += delegate(object sender, NotificationPreview preview)
                {
                    using (InboxStore disk = new InboxStore(vault.DataDirectory, Account))
                        if (preview.ChatId != "101" || preview.AccountId != Account
                            || !disk.LoadMessages().Any(item => item.Id == preview.Id && item.ChatId == "101" && item.RoomTitle == "actual group title"))
                            Interlocked.Increment(ref failures);
                    lock (published) published.Add(preview.Id);
                    Interlocked.Increment(ref count);
                };
                source.Start(); Wait(() => Starts(vault).Length == 1);
                Emit(source, Message("101", "103", false, "actual group title")); Wait(() => Volatile.Read(ref count) == 2);
                lock (published) Check(published.SequenceEqual(new[] { "103", "100" }), "batch row is followed by its separately restored legacy metadata");
                Check(Volatile.Read(ref failures) == 0 && source.LoadStoredMessages().Count == 2,
                    "restored row is durably assigned to the real room without adding a body");
                Emit(source, Message("101", "103", true, "actual group title") + "," + Complete("101"));
                Wait(() => !Protected(source, "101")); Check(Volatile.Read(ref count) == 2, "unchanged legacy metadata is not republished");
                source.Stop();
            }
        }
    }
}
