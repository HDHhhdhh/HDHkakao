using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;

namespace Spoiler.Tests
{
    internal static class StoreTests
    {
        public static int Main()
        {
            string originalDirectory = Environment.GetEnvironmentVariable("SPOILER_DATA_DIR");
            string testDirectory = Path.Combine(Path.GetTempPath(), "SpoilerStoreTests-" + Guid.NewGuid().ToString("N"));
            int failures = 0;

            try
            {
                Directory.CreateDirectory(testDirectory);
                Environment.SetEnvironmentVariable("SPOILER_DATA_DIR", testDirectory);
                NotificationStore store = new NotificationStore();

                List<NotificationPreview> samples = new List<NotificationPreview>();
                for (int index = 0; index < 85; index++)
                {
                    string rawChatId = "raw-room-secret-" + index;
                    string rawAuthorId = "raw-author-secret-" + index;
                    samples.Add(new NotificationPreview
                    {
                        Id = "message-" + index,
                        Sender = "보낸 사람 " + index,
                        Message = "암호화 확인 메시지 " + index,
                        ReceivedAt = DateTime.Now.AddMinutes(-index),
                        GroupKey = NotificationGrouping.CreateServerGroupKey(rawChatId, rawAuthorId),
                        IsMine = index == 0
                    });
                }

                store.Save(samples);
                IList<NotificationPreview> loaded = store.Load();
                if (loaded.Count != 80 || loaded[0].Sender != "보낸 사람 0"
                    || !loaded[0].IsMine
                    || loaded[0].GroupKey != NotificationGrouping.CreateServerGroupKey(
                        "raw-room-secret-0", "raw-author-secret-0")
                    || loaded.Any(item => item.Id == "message-80"))
                {
                    Console.WriteLine("Store global limit/order/identity round-trip test failed.");
                    failures++;
                }

                byte[] ciphertext = File.ReadAllBytes(Path.Combine(testDirectory, "history.dat"));
                string serialized = System.Text.Encoding.UTF8.GetString(ciphertext);
                if (serialized.IndexOf("암호화 확인 메시지", StringComparison.Ordinal) >= 0)
                {
                    Console.WriteLine("Stored history contained plaintext.");
                    failures++;
                }

                byte[] clearHistory = ProtectedData.Unprotect(
                    ciphertext,
                    Encoding.UTF8.GetBytes("Spoiler.LocalHistory.v1"),
                    DataProtectionScope.CurrentUser);
                string historyJson = Encoding.UTF8.GetString(clearHistory);
                if (historyJson.IndexOf("raw-room-secret-", StringComparison.Ordinal) >= 0
                    || historyJson.IndexOf("raw-author-secret-", StringComparison.Ordinal) >= 0
                    || historyJson.IndexOf("ChatId", StringComparison.Ordinal) >= 0
                    || historyJson.IndexOf("AuthorId", StringComparison.Ordinal) >= 0)
                {
                    Console.WriteLine("Serialized history retained raw server identity.");
                    failures++;
                }

                store.Save(new[]
                {
                    new NotificationPreview { Sender = "최근", Message = "유지", ReceivedAt = DateTime.Now },
                    new NotificationPreview { Sender = "오래됨", Message = "제거", ReceivedAt = DateTime.Now.AddDays(-8) }
                });
                if (store.Load().Count != 1 || store.Load().First().Sender != "최근")
                {
                    Console.WriteLine("Store expiration test failed.");
                    failures++;
                }

                string historyPath = Path.Combine(testDirectory, "history.dat");
                using (FileStream locked = new FileStream(historyPath, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
                {
                    if (store.Save(new[] { new NotificationPreview { Message = "must fail" } })
                        || File.Exists(historyPath + ".tmp"))
                    {
                        Console.WriteLine("Locked history save did not fail cleanly.");
                        failures++;
                    }
                    if (store.Clear())
                    {
                        Console.WriteLine("Locked history unexpectedly reported a successful clear.");
                        failures++;
                    }
                }

                File.Copy(historyPath, historyPath + ".tmp");
                if (!store.Clear() || File.Exists(historyPath) || File.Exists(historyPath + ".tmp"))
                {
                    Console.WriteLine("History clear test failed.");
                    failures++;
                }

                failures += AssertLegacyPayloadCompatibility();
            }
            catch (Exception exception)
            {
                Console.WriteLine("Store test exception: " + exception.GetType().Name + " " + exception.Message);
                failures++;
            }
            finally
            {
                Environment.SetEnvironmentVariable("SPOILER_DATA_DIR", originalDirectory);
                try { Directory.Delete(testDirectory, true); } catch { }
            }

            Console.WriteLine(failures == 0 ? "PASS: store tests" : "FAIL: " + failures + " store tests");
            return failures == 0 ? 0 : 1;
        }

        private static int AssertLegacyPayloadCompatibility()
        {
            NotificationPreview legacyShape = new NotificationPreview
            {
                Id = "legacy-message",
                Sender = "기존 이름",
                Message = "기존 기록",
                ReceivedAt = DateTime.Now,
                Source = "legacy"
            };

            DataContractJsonSerializer serializer = new DataContractJsonSerializer(typeof(List<NotificationPreview>));
            byte[] payload;
            using (MemoryStream stream = new MemoryStream())
            {
                serializer.WriteObject(stream, new List<NotificationPreview> { legacyShape });
                payload = stream.ToArray();
            }

            string json = Encoding.UTF8.GetString(payload);
            if (json.IndexOf("GroupKey", StringComparison.Ordinal) >= 0
                || json.IndexOf("IsMine", StringComparison.Ordinal) >= 0)
            {
                Console.WriteLine("Optional group key leaked into a legacy five-field payload.");
                return 1;
            }

            using (MemoryStream stream = new MemoryStream(payload))
            {
                List<NotificationPreview> loaded = serializer.ReadObject(stream) as List<NotificationPreview>;
                NotificationPreview item = loaded == null ? null : loaded.FirstOrDefault();
                if (item == null || item.Sender != "기존 이름" || item.Message != "기존 기록"
                    || item.GroupKey != null || item.IsMine)
                {
                    Console.WriteLine("Legacy history payload compatibility failed.");
                    return 1;
                }
            }

            return 0;
        }
    }
}
