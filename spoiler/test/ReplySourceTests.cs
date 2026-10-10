using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;

namespace Spoiler.Tests
{
    internal static class ReplySourceTests
    {
        private static string _root;
        private static string _fixture;
        private static string _node;
        private static int _failed;

        public static int Main(string[] args)
        {
            _fixture = Path.GetFullPath(args[0]); _node = args[1];
            _root = Path.Combine(Path.GetTempPath(), "SpoilerReplyTests-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_root);
            try
            {
                Run("valid ACK follows durable body and duplicate push is deduped", Success);
                Run("provider rejection preserves failed state", Rejection);
                Run("unknown response is never converted into success", Unknown);
                Run("ACK without durable body is unknown", AckOnly);
                Run("foreign room ACK is rejected", Foreign);
                Run("ACK for a different durable body is unknown", WrongBody);
                Run("invalid receipt timestamp is unknown", BadTime);
                Run("unknown room, malformed ID and empty text do not send", Preflight);
                Run("account and device changes block queued reply", ChangedAccount);
                Run("identity change after write rejects late ACK", ChangedAfterWrite);
                Run("Stop ends pending reply without replay", StopPending);
                Run("one pending write blocks additional send", Busy);
                Run("busy credential writer cannot block reply call, CanReply or Stop", WriterResponsiveness);
                Run("own ACK does not skip earlier pending pushes or catch-up", CursorOrdering);
                Run("timeout ends pending reply and late ACK cannot upgrade it", TimeoutReply);
            }
            finally { try { Directory.Delete(_root, true); } catch { } }
            Console.WriteLine(_failed == 0 ? "PASS: reply source (15 scenarios)" : "FAIL: reply source=" + _failed);
            return _failed == 0 ? 0 : 1;
        }

        private static void Check(bool result) { if (!result) throw new InvalidOperationException("Assertion failed"); }
        private static void Run(string name, Action action)
        {
            try { action(); Console.WriteLine("PASS: " + name); }
            catch (Exception error) { _failed++; Console.WriteLine("FAIL: " + name + " (" + error.GetType().Name + ")"); }
        }
        private static CredentialVault Vault(string mode)
        {
            CredentialVault vault = new CredentialVault(Path.Combine(_root, mode + "-" + Guid.NewGuid().ToString("N")));
            vault.Save(new LocoCredentials { OAuthToken = "fixture:" + mode, RefreshToken = "fixture-refresh",
                UserId = "123", DeviceUuid = "synthetic-device-1234567890", DeviceType = "tablet" });
            return vault;
        }
        private static LocoMessageSource Source(CredentialVault vault)
        {
            LocoMessageSource source = new LocoMessageSource(vault, _node, _fixture);
            string lastState = null;
            source.StateChanged += delegate(object sender, CaptureStateChangedEventArgs state) { lastState = state.State + ":" + state.Detail; };
            source.Start();
            Stopwatch time = Stopwatch.StartNew();
            while (!source.CanReply) { if (time.ElapsedMilliseconds > 5000) { Console.WriteLine("Synthetic startup state=" + lastState + " running=" + source.IsRunning); source.Dispose(); throw new TimeoutException(); } Thread.Sleep(10); }
            return source;
        }
        private static ReplySendResult Await(Task<ReplySendResult> task)
        {
            Check(task.Wait(5000)); return task.Result;
        }
        private static void Success()
        {
            CredentialVault vault = Vault("success");
            using (LocoMessageSource source = Source(vault))
            {
                ReplySendResult sent = Await(source.SendReplyAsync("101", " 원문\n😄 그대로 "));
                Check(sent.Status == ReplySendStatus.Sent && sent.LogId == "5001");
                using (InboxStore inbox = new InboxStore(vault.DataDirectory, "123"))
                {
                    var own = inbox.LoadMessages().Where(item => item.Id == "5001").ToList();
                    Check(own.Count == 1 && own[0].ChatId == "101" && own[0].IsMine && own[0].Message == " 원문\n😄 그대로 ");
                    Check(own[0].RoomTitle == "테스트 대화방");
                }
            }
        }
        private static void Case(string mode, ReplySendStatus expected)
        {
            using (LocoMessageSource source = Source(Vault(mode)))
                Check(Await(source.SendReplyAsync("101", "합성 답장")).Status == expected);
        }
        private static void Rejection() { Case("reject", ReplySendStatus.Failed); }
        private static void Unknown() { Case("unknown", ReplySendStatus.Unknown); }
        private static void AckOnly() { Case("ack-only", ReplySendStatus.Unknown); }
        private static void Foreign() { Case("foreign", ReplySendStatus.Unknown); }
        private static void WrongBody() { Case("wrong-body", ReplySendStatus.Unknown); }
        private static void BadTime() { Case("bad-time", ReplySendStatus.Unknown); }
        private static void Preflight()
        {
            using (LocoMessageSource source = Source(Vault("success")))
            {
                foreach (string id in new[] { "999", "01", "9223372036854775808", "room", " 101" })
                    Check(Await(source.SendReplyAsync(id, "합성 답장")).Status == ReplySendStatus.Failed);
                Check(Await(source.SendReplyAsync("101", " ")).Status == ReplySendStatus.Failed);
                Check(Await(source.SendReplyAsync("101", new string('가', 4001))).Status == ReplySendStatus.Failed);
                Check(source.LoadStoredMessages().Count == 1);
            }
        }
        private static void ChangedAccount()
        {
            foreach (bool account in new[] { true, false })
            {
                CredentialVault vault = Vault("success");
                using (LocoMessageSource source = Source(vault))
                {
                    LocoCredentials changed = vault.Load();
                    if (account) changed.UserId = "456"; else changed.DeviceUuid = "different-device-1234567890";
                    vault.Save(changed);
                    Check(Await(source.SendReplyAsync("101", "합성 답장")).Status == ReplySendStatus.Failed);
                }
            }
        }
        private static void StopPending()
        {
            using (LocoMessageSource source = Source(Vault("pending")))
            {
                Task<ReplySendResult> pending = source.SendReplyAsync("101", "합성 답장");
                Thread.Sleep(50); source.Stop();
                Check(Await(pending).Status == ReplySendStatus.Unknown && !source.CanReply);
            }
        }
        private static void ChangedAfterWrite()
        {
            CredentialVault vault = Vault("pending");
            using (LocoMessageSource source = Source(vault))
            {
                Task<ReplySendResult> completion = source.SendReplyAsync("101", "합성 답장");
                Thread.Sleep(50);
                BindingFlags hidden = BindingFlags.NonPublic | BindingFlags.Instance;
                object pending = typeof(LocoMessageSource).GetField("_pendingReply", hidden).GetValue(source);
                string request = (string)pending.GetType().GetField("RequestId").GetValue(pending);
                Process process = (Process)typeof(LocoMessageSource).GetField("_process", hidden).GetValue(source);
                LocoCredentials credentials = vault.Load(); credentials.DeviceUuid = "changed-device-1234567890"; vault.Save(credentials);
                string ack = "{\"type\":\"reply_result\",\"request_id\":\"" + request
                    + "\",\"chat_id\":\"101\",\"user_id\":\"123\",\"device_uuid\":\"synthetic-device-1234567890\","
                    + "\"status\":\"sent\",\"code\":\"reply_sent\",\"log_id\":\"5001\",\"sent_at\":"
                    + ((long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds - 60)
                        .ToString(System.Globalization.CultureInfo.InvariantCulture) + "}";
                typeof(LocoMessageSource).GetMethod("ConsumeBridgeLine", hidden).Invoke(source, new object[] { process, ack });
                Check(Await(completion).Status == ReplySendStatus.Unknown);
                source.Stop();
            }
        }
        private static void Busy()
        {
            using (LocoMessageSource source = Source(Vault("pending")))
            {
                Task<ReplySendResult> pending = source.SendReplyAsync("101", "합성 답장");
                Check(!source.CanReply);
                Check(Await(source.SendReplyAsync("101", "두번째 합성 답장")).Status == ReplySendStatus.Failed);
                source.Stop(); Check(Await(pending).Status == ReplySendStatus.Unknown);
            }
        }
        private static void TimeoutReply()
        {
            using (LocoMessageSource source = Source(Vault("pending")))
            {
                Task<ReplySendResult> pending = source.SendReplyAsync("101", "합성 답장");
                Thread.Sleep(50);
                object request = typeof(LocoMessageSource).GetField("_pendingReply", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(source);
                typeof(LocoMessageSource).GetMethod("ReplyTimedOut", BindingFlags.NonPublic | BindingFlags.Instance).Invoke(source, new[] { request });
                Check(Await(pending).Status == ReplySendStatus.Unknown);
                Check(pending.Result.Status != ReplySendStatus.Sent);
                source.Stop();
            }
        }
        private static void WriterResponsiveness()
        {
            CredentialVault vault = Vault("success");
            using (LocoMessageSource source = Source(vault))
            {
                MethodInfo acquire = typeof(CredentialVault).GetMethod("AcquireWriterLock", BindingFlags.NonPublic | BindingFlags.Instance);
                using ((IDisposable)acquire.Invoke(vault, null))
                {
                    Stopwatch clock = Stopwatch.StartNew();
                    Task<ReplySendResult> reply = source.SendReplyAsync("101", "합성 답장");
                    Check(clock.ElapsedMilliseconds < 300 && !reply.IsCompleted);
                    Thread.Sleep(50);
                    clock.Restart(); Check(!source.CanReply); Check(clock.ElapsedMilliseconds < 300);
                    clock.Restart(); source.Stop(); Check(clock.ElapsedMilliseconds < 3000);
                    Check(Await(reply).Status != ReplySendStatus.Sent);
                }
                Thread.Sleep(50);
                using (InboxStore inbox = new InboxStore(vault.DataDirectory, "123"))
                    Check(inbox.LoadMessages().Count == 1); // Cancelled preflight cannot send later.
            }
        }
        private static string Cursor(CredentialVault vault)
        {
            using (InboxStore inbox = new InboxStore(vault.DataDirectory, "123"))
                return inbox.GetCatchUpCursors(512).Single(item => item.ChatId == "101").LogId;
        }
        private static void CursorOrdering()
        {
            CredentialVault vault = Vault("delayed-push");
            using (LocoMessageSource source = Source(vault))
            {
                Check(Cursor(vault) == "1000");
                Check(Await(source.SendReplyAsync("101", "합성 답장")).Status == ReplySendStatus.Sent);
                Check(Cursor(vault) == "1000"); // Reconnect includes the not-yet-published 5000 and own 5001.
                BindingFlags hidden = BindingFlags.NonPublic | BindingFlags.Instance;
                Process process = (Process)typeof(LocoMessageSource).GetField("_process", hidden).GetValue(source);
                typeof(LocoMessageSource).GetMethod("WriteJsonLine", BindingFlags.NonPublic | BindingFlags.Static)
                    .Invoke(null, new object[] { process, "{\"type\":\"release_previous_push\"}" });
                Stopwatch clock = Stopwatch.StartNew();
                while (Cursor(vault) != "5000") { if (clock.ElapsedMilliseconds > 5000) throw new TimeoutException(); Thread.Sleep(10); }
                using (InboxStore inbox = new InboxStore(vault.DataDirectory, "123"))
                {
                    Check(inbox.LoadMessages().Count(item => item.Id == "5000") == 1);
                    Check(inbox.LoadMessages().Count(item => item.Id == "5001") == 1);
                }
            }
        }
    }
}
