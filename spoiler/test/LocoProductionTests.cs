using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;

namespace Spoiler.Tests
{
    internal static class LocoProductionTests
    {
        private static string _root;
        private static string _fixture;
        private static string _node;
        private static int _failures;

        public static int Main(string[] args)
        {
            _fixture = args.Length > 0 ? Path.GetFullPath(args[0]) : Path.GetFullPath("test/fixtures/loco-production-fixture.mjs");
            _node = args.Length > 1 ? args[1] : "C:\\Program Files\\nodejs\\node.exe";
            _root = Path.Combine(Path.GetTempPath(), "SpoilerLocoProductionTests-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_root);
            try
            {
                Check(File.Exists(_fixture) && File.Exists(_node));
                Run("source account isolation and stale events", AccountIsolation);
                Run("temporary locked load retries after unlock", LoadRetry);
                Run("durable publish and per-room catch-up progress", CatchUp);
                Run("disk failure backpressure and retry", DiskRetry);
                Run("Stop cancels blocked storage retry", StopRetry);
                Run("real child exit reconnect and Stop cancellation", Reconnect);
                Run("burst batching preserves durable publication order and names", Burst);
                Run("sparse message flushes without another event", Sparse);
                Run("failed burst retries without advancing cursor or publishing", BurstDiskRetry);
                Run("Stop during burst publication cancels remaining UI events", StopPublication);
                Run("child EOF drains accepted burst before reconnect", BurstExit);
                Run("throwing UI subscribers cannot strand durable burst", SubscriberFailure);
                Run("Stop cancels authentication lease acquisition", StopAuthWait);
                Run("Stop closes granted authentication child before release", StopAuthGranted);
            }
            catch (Exception error) { _failures++; Console.WriteLine("FAIL: fixture setup (" + error.GetType().Name + ")"); }
            finally { try { Directory.Delete(_root, true); } catch { } }
            Console.WriteLine(_failures == 0 ? "PASS: LOCO production tests (14 scenarios)" : "FAIL: LOCO production scenarios=" + _failures);
            return _failures == 0 ? 0 : 1;
        }
        private static void Run(string name, Action action)
        {
            try { action(); Console.WriteLine("PASS: " + name); }
            catch (Exception error) { _failures++; Console.WriteLine("FAIL: " + name + " (" + error.GetType().Name + ")"); }
        }
        private static void Check(bool value) { if (!value) throw new InvalidOperationException("Assertion failed."); }
        private static void Wait(Func<bool> condition, int milliseconds = 6000)
        {
            Stopwatch time = Stopwatch.StartNew();
            while (!condition()) { if (time.ElapsedMilliseconds >= milliseconds) throw new TimeoutException(); Thread.Sleep(25); }
        }
        private static CredentialVault Vault(string name, string account, string mode)
        {
            CredentialVault vault = new CredentialVault(Path.Combine(_root, name));
            vault.Save(Credentials(account, mode));
            return vault;
        }
        private static LocoCredentials Credentials(string account, string mode)
        {
            return new LocoCredentials { UserId = account, OAuthToken = "fixture:" + mode, RefreshToken = "fixture-refresh",
                DeviceUuid = "fixture-device-1234567890", DeviceType = "tablet" };
        }
        private static LocoMessageSource Source(CredentialVault vault) { return new LocoMessageSource(vault, _node, _fixture); }
        private static NotificationPreview Sample(string room, string id)
        {
            return new NotificationPreview { Id = id, Sender = "seed", Message = "synthetic seed", ReceivedAt = DateTime.UtcNow,
                GroupKey = NotificationGrouping.CreateServerGroupKey(room, "synthetic-author") };
        }
        private static void Seed(CredentialVault vault, string account, params string[] rooms)
        {
            using (InboxStore inbox = new InboxStore(vault.DataDirectory, account))
                foreach (string room in rooms) Check(inbox.CommitMessage(Sample(room, "100"), room, "synthetic-author", true).Saved);
        }
        private static T Field<T>(object source, string name)
        {
            return (T)source.GetType().GetField(name, BindingFlags.NonPublic | BindingFlags.Instance).GetValue(source);
        }
        private static void Consume(LocoMessageSource source, Process process, string line)
        {
            source.GetType().GetMethod("ConsumeBridgeLine", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(source, new object[] { process, line });
        }
        private static string Cursor(CredentialVault vault, string account, string room)
        {
            using (InboxStore inbox = new InboxStore(vault.DataDirectory, account))
            {
                MessageCatchUpCursor cursor = inbox.GetCatchUpCursors(512).FirstOrDefault(item => item.ChatId == room);
                return cursor == null ? null : cursor.LogId;
            }
        }
        private static void StopAuthWait()
        {
            CredentialVault vault = Vault("auth-wait", "a", "auth-lease");
            using (vault.AcquireAuthLease(0, CancellationToken.None))
            using (LocoMessageSource source = Source(vault))
            {
                source.Start();
                Wait(() => Field<bool>(source, "_authLeasePending"));
                Stopwatch elapsed = Stopwatch.StartNew();
                source.Stop();
                Check(elapsed.ElapsedMilliseconds < 4000);
                Check(Field<IDisposable>(source, "_authLease") == null);
            }
            using (vault.AcquireAuthLease(0, CancellationToken.None)) { }
        }
        private static void StopAuthGranted()
        {
            CredentialVault vault = Vault("auth-granted", "a", "auth-lease");
            using (LocoMessageSource source = Source(vault))
            {
                source.Start();
                Wait(() => Field<IDisposable>(source, "_authLease") != null);
                source.Stop();
                Check(Field<IDisposable>(source, "_authLease") == null);
                using (vault.AcquireAuthLease(0, CancellationToken.None)) { }
            }
        }
        private static void AccountIsolation()
        {
            CredentialVault vault = Vault("accounts", "a", "hold");
            Seed(vault, "a", "room-a");
            using (LocoMessageSource source = Source(vault))
            {
                Check(source.LoadStoredMessages().Single().AccountId == "a");
                source.Start(); Wait(() => source.IsRunning);
                Process old = Field<Process>(source, "_process");
                // Credential replacement can happen while A still runs.
                // Loading B must retire A before switching the inbox owner.
                vault.Save(Credentials("b", "hold"));
                Check(source.LoadStoredMessages().Count == 0 && source.CurrentAccountId == "b" && !source.IsRunning);
                source.Stop();
                byte[] unchanged = File.ReadAllBytes(vault.CredentialPath);
                Consume(source, old, "{\"type\":\"credentials\",\"userId\":\"a\",\"oauthToken\":\"fixture:stale\",\"deviceType\":\"tablet\"}");
                Consume(source, old, "{\"type\":\"message\",\"chat_id\":\"room-a\",\"log_id\":\"999\",\"message\":\"synthetic stale\"}");
                Check(unchanged.SequenceEqual(File.ReadAllBytes(vault.CredentialPath)) && source.LoadStoredMessages().Count == 0);
                vault.Save(Credentials("a", "hold"));
                Check(source.LoadStoredMessages().Single().Id == "100" && source.CurrentAccountId == "a");
                File.Delete(vault.CredentialPath);
                Check(source.LoadStoredMessages().Count == 0 && source.CurrentAccountId == null);
            }
        }

        private static void CatchUp()
        {
            CredentialVault vault = Vault("catchup", "a", "catchup");
            Seed(vault, "a", "room-a", "room-b");
            using (LocoMessageSource source = Source(vault))
            {
                int count = 0; int publicationFailures = 0;
                source.PreviewCaptured += delegate(object sender, NotificationPreview preview)
                {
                    using (InboxStore disk = new InboxStore(vault.DataDirectory, "a"))
                        if (!disk.LoadMessages().Any(item => item.Id == preview.Id && item.GroupKey == preview.GroupKey)
                            || preview.AccountId != "a") Interlocked.Increment(ref publicationFailures);
                    Interlocked.Increment(ref count);
                };
                source.Start(); Wait(() => Volatile.Read(ref count) == 4);
                Wait(() => Cursor(vault, "a", "room-a") == "300");
                Check(Volatile.Read(ref publicationFailures) == 0 && Cursor(vault, "a", "room-b") == "100");
                Check(source.LoadStoredMessages().Count == 6);
                source.Stop(); Check(Volatile.Read(ref count) == 4);
            }
        }

        private static void LoadRetry()
        {
            CredentialVault vault = Vault("load-retry", "a", "hold"); Seed(vault, "a", "room-a");
            string path;
            using (InboxStore inbox = new InboxStore(vault.DataDirectory, "a")) { path = inbox.StatePath; }
            byte[] unchanged = File.ReadAllBytes(path);
            using (LocoMessageSource source = Source(vault))
            {
                using (FileStream locked = new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
                {
                    Check(source.LoadStoredMessages().Count == 0 && Field<InboxStore>(source, "_inbox").HasLoadError);
                    source.Start(); Check(!source.IsRunning);
                }
                Check(unchanged.SequenceEqual(File.ReadAllBytes(path)));
                Check(source.LoadStoredMessages().Single().Id == "100" && !Field<InboxStore>(source, "_inbox").HasLoadError);
                source.Start(); Wait(() => source.IsRunning); source.Stop();
            }
        }

        private static void DiskRetry()
        {
            CredentialVault vault = Vault("retry", "a", "message"); Seed(vault, "a", "room-a");
            using (LocoMessageSource source = Source(vault))
            {
                source.LoadStoredMessages();
                int count = 0; source.PreviewCaptured += delegate { Interlocked.Increment(ref count); };
                string path = Field<InboxStore>(source, "_inbox").StatePath;
                using (FileStream locked = new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
                {
                    source.Start(); Wait(() => Field<bool>(source, "_storageBlocked"));
                    Check(Volatile.Read(ref count) == 0 && source.LoadStoredMessages().Single().Id == "100");
                }
                Wait(() => Volatile.Read(ref count) == 1);
                Check(source.LoadStoredMessages().Count == 2 && Cursor(vault, "a", "room-a") == "100");
                // Existing room remains protected until a complete page.
                source.Stop();
            }
        }

        private static void StopRetry()
        {
            CredentialVault vault = Vault("stop-retry", "a", "message"); Seed(vault, "a", "room-a");
            using (LocoMessageSource source = Source(vault))
            {
                source.LoadStoredMessages();
                int count = 0; source.PreviewCaptured += delegate { Interlocked.Increment(ref count); };
                string path = Field<InboxStore>(source, "_inbox").StatePath;
                using (FileStream locked = new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
                {
                    source.Start(); Wait(() => Field<bool>(source, "_storageBlocked"));
                    Stopwatch stop = Stopwatch.StartNew(); source.Stop(); Check(stop.ElapsedMilliseconds < 4000);
                }
                Thread.Sleep(1200);
                Check(Volatile.Read(ref count) == 0 && !source.IsRunning && source.LoadStoredMessages().Single().Id == "100");
            }
        }

        private static void Reconnect()
        {
            CredentialVault vault = Vault("reconnect", "a", "reconnect");
            using (LocoMessageSource source = Source(vault))
            {
                int count = 0; source.PreviewCaptured += delegate { Interlocked.Increment(ref count); };
                source.Start(); Wait(() => Volatile.Read(ref count) >= 2, 8000);
                source.Stop(); int stoppedAt = Volatile.Read(ref count);
                Thread.Sleep(2300);
                Check(!source.IsRunning && Volatile.Read(ref count) == stoppedAt && Field<Timer>(source, "_reconnectTimer") == null);
            }
        }

        private static void Burst()
        {
            CredentialVault vault = Vault("burst", "a", "burst");
            using (LocoMessageSource source = Source(vault))
            {
                int count = 0; int failures = 0;
                source.PreviewCaptured += delegate(object sender, NotificationPreview preview)
                {
                    int ordinal = Volatile.Read(ref count);
                    using (InboxStore disk = new InboxStore(vault.DataDirectory, "a"))
                    {
                        if (preview.Id != (1000 + ordinal).ToString() || preview.Sender != "burst sender"
                            || !disk.LoadMessages().Any(item => item.Id == preview.Id)
                            || decimal.Parse(Cursor(vault, "a", "burst-room")) < decimal.Parse(preview.Id))
                            Interlocked.Increment(ref failures);
                    }
                    Interlocked.Increment(ref count);
                };
                source.Start(); Wait(() => Volatile.Read(ref count) == 120);
                InboxStore inbox = Field<InboxStore>(source, "_inbox");
                Console.WriteLine("EVIDENCE: burst=120, snapshot_writes=" + inbox.SnapshotWriteCount + ", publication_failures=" + failures);
                Check(Volatile.Read(ref failures) == 0 && inbox.SnapshotWriteCount == 3);
                Check(source.LoadStoredMessages().Count == 120 && Cursor(vault, "a", "burst-room") == "1119");
                source.Stop();
            }
        }

        private static void Sparse()
        {
            CredentialVault vault = Vault("sparse", "a", "sparse");
            using (LocoMessageSource source = Source(vault))
            {
                int count = 0; double latency = 0;
                source.PreviewCaptured += delegate(object sender, NotificationPreview preview)
                {
                    if (preview.Id == "1000") latency = (DateTime.UtcNow - preview.ReceivedAt.ToUniversalTime()).TotalMilliseconds;
                    Interlocked.Increment(ref count);
                };
                source.Start(); Wait(() => Volatile.Read(ref count) >= 1);
                Check(Volatile.Read(ref count) == 1 && latency >= 0 && latency < 400);
                Wait(() => Volatile.Read(ref count) == 2);
                Check(Field<InboxStore>(source, "_inbox").SnapshotWriteCount == 2);
                source.Stop();
                Console.WriteLine("EVIDENCE: sparse_first_latency_ms=" + Math.Round(latency));
            }
        }

        private static void BurstDiskRetry()
        {
            CredentialVault vault = Vault("burst-retry", "a", "burst");
            using (LocoMessageSource source = Source(vault))
            {
                source.LoadStoredMessages();
                InboxStore inbox = Field<InboxStore>(source, "_inbox");
                int count = 0; source.PreviewCaptured += delegate { Interlocked.Increment(ref count); };
                Directory.CreateDirectory(inbox.StatePath + ".tmp");
                source.Start(); Wait(() => Field<bool>(source, "_storageBlocked"));
                Check(Volatile.Read(ref count) == 0 && inbox.LoadMessages().Count == 0 && inbox.GetCatchUpCursors(512).Count == 0);
                Directory.Delete(inbox.StatePath + ".tmp");
                Wait(() => Volatile.Read(ref count) == 120);
                Check(inbox.SnapshotWriteCount == 3 && Cursor(vault, "a", "burst-room") == "1119");
                source.Stop();
            }
        }

        private static void StopPublication()
        {
            CredentialVault vault = Vault("stop-publication", "a", "burst");
            using (LocoMessageSource source = Source(vault))
            {
                int count = 0;
                source.PreviewCaptured += delegate { Interlocked.Increment(ref count); source.Stop(); };
                source.Start(); Wait(() => !source.IsRunning && Volatile.Read(ref count) == 1);
                Thread.Sleep(300);
                Check(Volatile.Read(ref count) == 1 && Field<InboxStore>(source, "_inbox").SnapshotWriteCount == 1);
            }
        }

        private static void BurstExit()
        {
            CredentialVault vault = Vault("burst-exit", "a", "burst-exit");
            using (LocoMessageSource source = Source(vault))
            {
                int count = 0; source.PreviewCaptured += delegate { Interlocked.Increment(ref count); };
                source.Start(); Wait(() => Volatile.Read(ref count) == 120);
                Wait(() => Field<Process>(source, "_process") == null);
                Check(Field<InboxStore>(source, "_inbox").SnapshotWriteCount == 3 && Cursor(vault, "a", "burst-room") == "1119");
                source.Stop(); Thread.Sleep(200);
                Check(Volatile.Read(ref count) == 120 && !source.IsRunning);
            }
        }

        private static void SubscriberFailure()
        {
            CredentialVault vault = Vault("subscriber-failure", "a", "burst");
            using (LocoMessageSource source = Source(vault))
            {
                int count = 0; int throwingCount = 0; int errors = 0;
                source.PreviewCaptured += delegate { Interlocked.Increment(ref throwingCount); throw new InvalidOperationException("synthetic subscriber failure"); };
                source.PreviewCaptured += delegate { Interlocked.Increment(ref count); };
                source.StateChanged += delegate { throw new InvalidOperationException("synthetic state subscriber failure"); };
                source.StateChanged += delegate(object sender, CaptureStateChangedEventArgs args)
                { if (args.State == CaptureState.Error) Interlocked.Increment(ref errors); };
                source.Start(); Wait(() => Volatile.Read(ref count) == 120);
                Check(source.IsRunning && Volatile.Read(ref throwingCount) == 120 && Volatile.Read(ref errors) > 0);
                Check(Field<InboxStore>(source, "_inbox").SnapshotWriteCount == 3 && Cursor(vault, "a", "burst-room") == "1119");
                source.Stop();
            }
        }
    }
}
