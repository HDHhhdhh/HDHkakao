using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace Spoiler.Tests
{
    internal static class LocoCoreTests
    {
        public static int Main()
        {
            int failures = 0;
            failures += AssertMessageEvent();
            failures += AssertNestedMessageIdentityEvent();
            failures += AssertLongMessageEvent();
            failures += AssertAttachmentPlaceholder();
            failures += AssertCredentialEvent();
            failures += AssertMalformedLineRejected();
            failures += AssertFlatStartEnvelope();
            failures += AssertCatchUpStartEnvelope();
            failures += AssertRegistrationForceIsExplicit();
            failures += AssertVaultProtectsAndPersistsIdentity();
            failures += AssertCredentialRefreshMerge();
            failures += AssertCredentialSaveAcknowledgement();
            failures += AssertLifecycleGenerationRejectsStaleReconnect();

            Console.WriteLine(failures == 0
                ? "PASS: LOCO core tests"
                : "FAIL: " + failures + " LOCO core tests");
            return failures == 0 ? 0 : 1;
        }

        private static int AssertMessageEvent()
        {
            LocoBridgeEvent bridgeEvent;
            bool parsed = LocoMessageSource.TryParseBridgeLine(
                "{\"type\":\"message\",\"chat_id\":42,\"log_id\":\"987\",\"author_id\":7,\"author_name\":\"민수\",\"chatTitle\":\"업무방\",\"message\":\"팝업 없이 도착\",\"messageType\":1,\"sent_at\":1700000000000,\"isMine\":false,\"recovered\":true}",
                out bridgeEvent);

            if (!parsed || bridgeEvent.Type != "message" || bridgeEvent.Sender != "민수"
                || bridgeEvent.Message != "팝업 없이 도착" || bridgeEvent.LogId != "987"
                || bridgeEvent.SentAt != "1700000000000" || bridgeEvent.ChatId != "42"
                || bridgeEvent.AuthorId != "7" || bridgeEvent.MessageType != "1"
                || bridgeEvent.ChatTitle != "업무방"
                || bridgeEvent.IsMine || !bridgeEvent.Recovered
                || NotificationGrouping.CreateServerGroupKey(bridgeEvent.ChatId, bridgeEvent.AuthorId)
                    != NotificationGrouping.CreateServerGroupKey("42", "7"))
            {
                Console.WriteLine("Message NDJSON identity/canonical numeric parsing failed.");
                return 1;
            }
            return 0;
        }

        private static int AssertNestedMessageIdentityEvent()
        {
            LocoBridgeEvent bridgeEvent;
            bool parsed = LocoMessageSource.TryParseBridgeLine(
                "{\"type\":\"message\",\"message\":{\"chatId\":\"nested-room\",\"authorId\":\"nested-author\",\"authorName\":\"중첩 이름\",\"text\":\"중첩 본문\",\"logId\":\"nested-log\",\"sentAt\":1700000000002,\"isMine\":true}}",
                out bridgeEvent);

            if (!parsed || bridgeEvent == null || bridgeEvent.ChatId != "nested-room"
                || bridgeEvent.AuthorId != "nested-author" || bridgeEvent.AuthorName != "중첩 이름"
                || bridgeEvent.Message != "중첩 본문" || bridgeEvent.LogId != "nested-log"
                || bridgeEvent.SentAt != "1700000000002" || !bridgeEvent.IsMine)
            {
                Console.WriteLine("Nested message identity parsing failed.");
                return 1;
            }
            return 0;
        }

        private static int AssertLongMessageEvent()
        {
            string message = new string('가', 8192) + "\n마지막 줄까지 보존";
            LocoBridgeEvent bridgeEvent;
            bool parsed = LocoMessageSource.TryParseBridgeLine(
                "{\"type\":\"message\",\"chat_id\":\"long-room\",\"log_id\":\"long-log\",\"author_name\":\"장문 테스트\",\"message\":\""
                + message.Replace("\n", "\\n")
                + "\",\"sent_at\":1700000000001}",
                out bridgeEvent);

            if (!parsed || bridgeEvent == null || bridgeEvent.Message != message
                || !bridgeEvent.Message.EndsWith("마지막 줄까지 보존", StringComparison.Ordinal))
            {
                Console.WriteLine("Long message NDJSON preservation failed.");
                return 1;
            }
            return 0;
        }

        private static int AssertAttachmentPlaceholder()
        {
            if (LocoMessageSource.MessageForDisplay(string.Empty, 2) != "[사진]"
                || LocoMessageSource.MessageForDisplay(string.Empty, 18) != "[파일]"
                || LocoMessageSource.MessageForDisplay(string.Empty, 27) != "[사진 여러 장]"
                || LocoMessageSource.MessageForDisplay("전문 보존", 2) != "전문 보존"
                || LocoMessageSource.MessageForDisplay(string.Empty, 0) != string.Empty)
            {
                Console.WriteLine("Attachment-only message placeholder contract failed.");
                return 1;
            }
            return 0;
        }

        private static int AssertCredentialEvent()
        {
            LocoBridgeEvent bridgeEvent;
            bool parsed = LocoMessageSource.TryParseBridgeLine(
                "{\"type\":\"credentials\",\"oauthToken\":\"token-a\",\"refreshToken\":\"refresh-a\",\"userId\":\"user-a\",\"deviceUuid\":\"device-a-1234567890\",\"deviceType\":\"tablet\"}",
                out bridgeEvent);

            LocoCredentials credentials = bridgeEvent == null ? null : bridgeEvent.Credentials;
            if (!parsed || credentials == null || credentials.OAuthToken != "token-a"
                || credentials.RefreshToken != "refresh-a" || credentials.UserId != "user-a"
                || credentials.DeviceUuid != "device-a-1234567890"
                || credentials.DeviceType != "tablet")
            {
                Console.WriteLine("Credential refresh NDJSON parsing failed.");
                return 1;
            }
            return 0;
        }

        private static int AssertMalformedLineRejected()
        {
            LocoBridgeEvent bridgeEvent;
            if (LocoMessageSource.TryParseBridgeLine("not-json", out bridgeEvent)
                || LocoMessageSource.TryParseBridgeLine("{} trailing", out bridgeEvent)
                || LocoMessageSource.TryParseBridgeLine("[]", out bridgeEvent))
            {
                Console.WriteLine("Malformed NDJSON was accepted.");
                return 1;
            }
            return 0;
        }

        private static int AssertFlatStartEnvelope()
        {
            string json = LocoBridgeLauncher.Serialize(new LocoStartRequest
            {
                Type = "start",
                DeviceType = "pc",
                Force = true,
                Credentials = new LocoCredentials
                {
                    OAuthToken = "oauth-a",
                    RefreshToken = "refresh-a",
                    UserId = "user-a",
                    DeviceUuid = "device-a-1234567890",
                    DeviceType = "pc"
                }
            });

            if (json.IndexOf("\"oauthToken\":\"oauth-a\"", StringComparison.Ordinal) < 0
                || json.IndexOf("\"userId\":\"user-a\"", StringComparison.Ordinal) < 0
                || json.IndexOf("\"deviceUuid\":\"device-a-1234567890\"", StringComparison.Ordinal) < 0
                || json.IndexOf("\"deviceType\":\"tablet\"", StringComparison.Ordinal) < 0
                || json.IndexOf("\"credentials\"", StringComparison.Ordinal) >= 0
                || json.IndexOf("\"force\"", StringComparison.Ordinal) >= 0)
            {
                Console.WriteLine("Flat read-only start envelope contract failed.");
                return 1;
            }

            LocoBridgeEvent parsedEvent;
            if (!LocoMessageSource.TryParseBridgeLine(json, out parsedEvent)
                || parsedEvent == null || parsedEvent.Type != "start"
                || parsedEvent.Credentials == null
                || parsedEvent.Credentials.OAuthToken != "oauth-a")
            {
                Console.WriteLine("Flat start envelope was not valid NDJSON.");
                return 1;
            }
            return 0;
        }

        private static int AssertCatchUpStartEnvelope()
        {
            List<LocoCatchUpCursor> requested = new List<LocoCatchUpCursor>();
            for (int index = 0; index < 40; index++)
            {
                requested.Add(new LocoCatchUpCursor
                {
                    ChatId = (123 + index).ToString(),
                    AfterLogId = (456 + index).ToString(),
                    Limit = 50
                });
            }
            string json = LocoMessageSource.SerializeStartRequest(new LocoStartRequest
            {
                Type = "start",
                DeviceType = "tablet",
                Credentials = new LocoCredentials
                {
                    OAuthToken = "oauth-cursor",
                    UserId = "user-cursor",
                    DeviceUuid = "device-cursor-1234567890",
                    DeviceType = "tablet"
                },
                CatchUp = requested
            });

            Dictionary<string, object> root = null;
            try { root = LocoJson.Parse(json) as Dictionary<string, object>; } catch { }
            object catchUpValue;
            List<object> cursors = root != null && root.TryGetValue("catchUp", out catchUpValue)
                ? catchUpValue as List<object>
                : null;
            Dictionary<string, object> cursor = cursors != null && cursors.Count == 40
                ? cursors[0] as Dictionary<string, object>
                : null;
            if (cursor == null
                || Convert.ToString(cursor["chatId"]) != "123"
                || Convert.ToString(cursor["afterLogId"]) != "456"
                || Convert.ToString(cursor["limit"]) != "50"
                || json.IndexOf("markRead", StringComparison.OrdinalIgnoreCase) >= 0
                || json.IndexOf("sendMessage", StringComparison.OrdinalIgnoreCase) >= 0
                || json.IndexOf("openChat", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                Console.WriteLine("Read-only catch-up start envelope contract failed.");
                return 1;
            }
            return 0;
        }

        private static int AssertRegistrationForceIsExplicit()
        {
            string safe = LocoBridgeLauncher.CreateRegistrationRequest(
                "a+\"quote@example.invalid", "transient\\secret\nline", "device-a-1234567890", false);
            string forced = LocoBridgeLauncher.CreateRegistrationRequest(
                "a@example.invalid", "transient-secret", "device-a-1234567890", true);

            Dictionary<string, object> parsed = null;
            try
            {
                parsed = LocoJson.Parse(safe) as Dictionary<string, object>;
            }
            catch
            {
            }

            if (parsed == null
                || !parsed.ContainsKey("email")
                || !parsed.ContainsKey("password")
                || !parsed.ContainsKey("deviceUuid")
                || Convert.ToString(parsed["email"]) != "a+\"quote@example.invalid"
                || Convert.ToString(parsed["password"]) != "transient\\secret\nline"
                || Convert.ToString(parsed["deviceUuid"]) != "device-a-1234567890"
                || safe.IndexOf("\"deviceType\":\"tablet\"", StringComparison.Ordinal) < 0
                || safe.IndexOf("\"force\":false", StringComparison.Ordinal) < 0
                || forced.IndexOf("\"deviceType\":\"tablet\"", StringComparison.Ordinal) < 0
                || forced.IndexOf("\"force\":true", StringComparison.Ordinal) < 0)
            {
                Console.WriteLine("Registration device/force contract failed.");
                return 1;
            }
            return 0;
        }

        private static int AssertVaultProtectsAndPersistsIdentity()
        {
            string directory = Path.Combine(Path.GetTempPath(), "SpoilerLocoTests-" + Guid.NewGuid().ToString("N"));
            try
            {
                CredentialVault vault = new CredentialVault(directory);
                string firstUuid = vault.GetOrCreateDeviceUuid();
                string secondUuid = new CredentialVault(directory).GetOrCreateDeviceUuid();
                if (string.IsNullOrWhiteSpace(firstUuid) || firstUuid != secondUuid || !File.Exists(vault.DeviceIdentityPath))
                {
                    Console.WriteLine("Stable device UUID persistence failed.");
                    return 1;
                }

                byte[] identityBytes = File.ReadAllBytes(vault.DeviceIdentityPath);
                if (Encoding.UTF8.GetString(identityBytes).IndexOf(firstUuid, StringComparison.Ordinal) >= 0)
                {
                    Console.WriteLine("Device UUID was stored in plaintext.");
                    return 1;
                }

                vault.Save(new LocoCredentials
                {
                    AccessToken = "access-token-a",
                    RefreshToken = "refresh-token-a",
                    UserId = "user-a",
                    DeviceType = "pc"
                });
                LocoCredentials loaded = vault.Load();
                byte[] credentialBytes = File.ReadAllBytes(vault.CredentialPath);
                if (loaded == null || !loaded.IsUsable || loaded.DeviceType != "tablet"
                    || loaded.DeviceUuid != firstUuid
                    || Encoding.UTF8.GetString(credentialBytes).IndexOf("access-token-a", StringComparison.Ordinal) >= 0
                    || Encoding.UTF8.GetString(CredentialVault.Serialize(loaded)).IndexOf("password", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    Console.WriteLine("DPAPI credential vault test failed.");
                    return 1;
                }

                if (!vault.Clear() || File.Exists(vault.CredentialPath) || File.Exists(vault.DeviceIdentityPath))
                {
                    Console.WriteLine("Credential vault clear test failed.");
                    return 1;
                }
            }
            catch (Exception exception)
            {
                Console.WriteLine("Credential vault exception: " + exception.GetType().Name + " " + exception.Message);
                return 1;
            }
            finally
            {
                try { if (Directory.Exists(directory)) Directory.Delete(directory, true); } catch { }
            }
            return 0;
        }

        private static int AssertCredentialRefreshMerge()
        {
            LocoCredentials merged = LocoMessageSource.MergeCredentials(
                new LocoCredentials
                {
                    OAuthToken = "old-token",
                    RefreshToken = "old-refresh",
                    UserId = "user-a",
                    DeviceUuid = "device-a-1234567890",
                    DeviceType = "tablet"
                },
                new LocoCredentials
                {
                    OAuthToken = "new-token",
                    UserId = "user-a",
                    DeviceType = "tablet"
                });

            if (merged == null || merged.OAuthToken != "new-token" || merged.RefreshToken != "old-refresh"
                || merged.DeviceUuid != "device-a-1234567890" || merged.DeviceType != "tablet")
            {
                Console.WriteLine("Credential refresh merge failed.");
                return 1;
            }
            return 0;
        }

        private static int AssertCredentialSaveAcknowledgement()
        {
            if (LocoBridgeLauncher.CreateCredentialSaveAck(true) != "{\"type\":\"credentials_saved\"}"
                || LocoBridgeLauncher.CreateCredentialSaveAck(false) != "{\"type\":\"credentials_save_failed\"}")
            {
                Console.WriteLine("Credential save acknowledgement contract failed.");
                return 1;
            }
            return 0;
        }

        private static int AssertLifecycleGenerationRejectsStaleReconnect()
        {
            LocoLifecycleGate gate = new LocoLifecycleGate();
            long timerGeneration = gate.RequestStart();
            if (!gate.Allows(timerGeneration))
            {
                Console.WriteLine("Lifecycle start generation was not authorized.");
                return 1;
            }

            // Models Stop()/Dispose() winning after a timer captured its
            // generation but before that callback attempts Process.Start().
            gate.RequestStop();
            if (gate.Allows(timerGeneration))
            {
                Console.WriteLine("A stale reconnect generation survived Stop/Dispose.");
                return 1;
            }

            long laterStart = gate.RequestStart();
            if (gate.Allows(timerGeneration) || !gate.Allows(laterStart))
            {
                Console.WriteLine("Lifecycle generations did not isolate a later explicit start.");
                return 1;
            }

            gate.RequestStop();
            if (gate.Allows(laterStart))
            {
                Console.WriteLine("The later reconnect generation survived the final stop.");
                return 1;
            }
            return 0;
        }
    }
}
