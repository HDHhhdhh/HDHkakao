using System;
using System.Collections.Generic;
using System.Globalization;

namespace Spoiler
{
    public sealed partial class LocoMessageSource
    {
        /// <summary>
        /// Parses one bridge NDJSON line.  This is public so the bridge
        /// contract can be tested without starting node.exe or contacting
        /// KakaoTalk.
        /// </summary>
        public static bool TryParseBridgeLine(string line, out LocoBridgeEvent bridgeEvent)
        {
            bridgeEvent = null;
            if (string.IsNullOrWhiteSpace(line))
            {
                return false;
            }

            try
            {
                object parsed = LocoJson.Parse(line);
                Dictionary<string, object> root = parsed as Dictionary<string, object>;
                if (root == null)
                {
                    return false;
                }

                bridgeEvent = LocoBridgeEvent.FromJson(root);
                return bridgeEvent != null && !string.IsNullOrWhiteSpace(bridgeEvent.Type);
            }
            catch
            {
                return false;
            }
        }
        internal static string SerializeStartRequest(LocoStartRequest request)
        {
            return LocoBridgeLauncher.Serialize(request);
        }
    }

    [Serializable]
    public sealed class LocoBridgeEvent
    {
        public string Type { get; private set; }
        public string AuthLeaseId { get; private set; }
        public string RequestId { get; private set; }
        public string ReplyUserId { get; private set; }
        public string Status { get; private set; }
        public string State { get; private set; }
        public string Detail { get; private set; }
        public string Error { get; private set; }
        public string Sender { get; private set; }
        public string AuthorName { get; private set; }
        public string ChatTitle { get; private set; }
        public string TitleSource { get; private set; }
        public bool Authoritative { get; private set; }
        public string ChatId { get; private set; }
        public string AuthorId { get; private set; }
        public string Message { get; private set; }
        public string Text { get; private set; }
        public string MessageType { get; private set; }
        public string Id { get; private set; }
        public string LogId { get; private set; }
        public string ThroughLogId { get; private set; }
        public string MessageId { get; private set; }
        public string SentAt { get; private set; }
        public string Timestamp { get; private set; }
        public string ReceivedAt { get; private set; }
        public bool IsMine { get; private set; }
        public bool ReplyAck { get; private set; }
        public bool Recovered { get; private set; }
        public bool Partial { get; private set; }
        public bool Complete { get; private set; }
        public string Passcode { get; private set; }
        public string Code { get; private set; }
        public string DeviceUuid { get; private set; }
        public string CodeValue { get { return Code; } }
        public LocoCredentials Credentials { get; private set; }
        public LocoCredentials Credential { get; private set; }

        public string DetailOrError()
        {
            // Never display dependency exception text, even from an old bridge.
            switch ((Code ?? string.Empty).ToLowerInvariant())
            {
                case "bad_credentials": return "카카오계정 인증 정보를 확인해 주세요.";
                case "credential_save_failed": return "자격증명을 안전하게 저장하지 못했어요.";
                case "auth_lease_busy": return "다른 앱에서 인증 중이에요. 잠시 후 다시 연결합니다.";
                case "auth_lease_invalid": return "인증 조율 응답을 확인하지 못했어요. 두 앱을 다시 시작해 주세요.";
                case "auth_identity_changed": return "인증 계정이나 기기가 바뀌었어요. 등록 상태를 확인해 주세요.";
                case "registration_timeout": case "auth_timeout": return "인증 시간이 지났어요. 다시 시도해 주세요.";
                case "login_timeout": case "history_timeout": case "ack_timeout":
                    return "서버 응답 시간이 지났어요. 연결을 다시 확인합니다.";
                case "event_queue_full": case "adoption_buffer_full":
                    return "수신 처리가 지연되어 기록된 기준점부터 다시 연결합니다.";
                case "sync_cache_path_invalid":
                    return "보조 캐시 경로를 안전하게 열지 못했어요. 저장 폴더의 연결·권한을 확인해 주세요.";
                default: return "보조기기 연결에 문제가 생겼어요. 네트워크와 등록 상태를 확인해 주세요.";
            }
        }

        internal static LocoBridgeEvent FromJson(Dictionary<string, object> root)
        {
            if (root == null)
            {
                return null;
            }

            Dictionary<string, object> messageObject = GetObject(root, "message");
            Dictionary<string, object> credentialsObject = GetObject(root, "credentials");
            Dictionary<string, object> credentialObject = GetObject(root, "credential");

            LocoBridgeEvent result = new LocoBridgeEvent
            {
                Type = GetString(root, "type"),
                AuthLeaseId = GetString(root, "auth_lease_id"),
                RequestId = GetString(root, "request_id"),
                ReplyUserId = GetString(root, "user_id"),
                Status = FirstNonEmpty(GetString(root, "status"), GetString(root, "state"), null),
                State = GetString(root, "state"),
                Detail = FirstNonEmpty(GetString(root, "detail"), GetString(root, "statusMessage"), null),
                Error = GetString(root, "error"),
                Sender = FirstNonEmpty(GetString(root, "sender"), GetString(root, "author_name"), GetString(root, "authorName"), null),
                AuthorName = FirstNonEmpty(GetString(root, "author_name"), GetString(root, "authorName"), GetString(root, "nickname"), null),
                ChatTitle = FirstNonEmpty(GetString(root, "chat_title"), GetString(root, "chatTitle"), null),
                TitleSource = FirstNonEmpty(GetString(root, "title_source"), GetString(root, "titleSource"), null),
                ChatId = FirstNonEmpty(GetString(root, "chat_id"), GetString(root, "chatId"), null),
                AuthorId = FirstNonEmpty(GetString(root, "author_id"), GetString(root, "authorId"), null),
                Message = FirstNonEmpty(GetString(root, "message"), GetString(root, "messageText"), null),
                Text = GetString(root, "text"),
                MessageType = FirstNonEmpty(GetString(root, "message_type"), GetString(root, "messageType"), null),
                Id = GetString(root, "id"),
                LogId = FirstNonEmpty(GetString(root, "log_id"), GetString(root, "logId"), null),
                ThroughLogId = GetString(root, "throughLogId"),
                MessageId = FirstNonEmpty(GetString(root, "messageId"), GetString(root, "message_id"), null),
                SentAt = FirstNonEmpty(GetString(root, "sent_at"), GetString(root, "sentAt"), null),
                Timestamp = GetString(root, "timestamp"),
                ReceivedAt = GetString(root, "receivedAt"),
                IsMine = GetBoolean(root, "isMine", GetBoolean(root, "is_mine", false)),
                ReplyAck = GetBoolean(root, "replyAck", false),
                Recovered = GetBoolean(root, "recovered", false),
                Partial = GetBoolean(root, "partial", false),
                Complete = GetBoolean(root, "complete", false),
                Passcode = FirstNonEmpty(GetString(root, "passcode"), GetString(root, "deviceCode"), null),
                Code = GetString(root, "code"),
                DeviceUuid = FirstNonEmpty(GetString(root, "deviceUuid"), GetString(root, "device_uuid"), null),
                Credentials = ReadCredentials(credentialsObject),
                Credential = ReadCredentials(credentialObject)
            };

            if (result.Credentials == null && result.Credential == null)
            {
                // The bridge's credentials event is intentionally flat so
                // the host can stream it without a nested secret object.
                result.Credentials = ReadCredentials(root);
            }

            if (messageObject != null)
            {
                result.Message = FirstNonEmpty(
                    GetString(messageObject, "message"),
                    GetString(messageObject, "text"),
                    result.Message);
                result.Text = FirstNonEmpty(result.Text, GetString(messageObject, "text"), null);
                result.Sender = FirstNonEmpty(result.Sender, GetString(messageObject, "sender"), GetString(messageObject, "author_name"), result.Sender);
                result.AuthorName = FirstNonEmpty(result.AuthorName, GetString(messageObject, "author_name"), GetString(messageObject, "authorName"), result.AuthorName);
                result.ChatTitle = FirstNonEmpty(result.ChatTitle, GetString(messageObject, "chat_title"), GetString(messageObject, "chatTitle"), result.ChatTitle);
                result.ChatId = FirstNonEmpty(result.ChatId, GetString(messageObject, "chat_id"), GetString(messageObject, "chatId"), result.ChatId);
                result.AuthorId = FirstNonEmpty(result.AuthorId, GetString(messageObject, "author_id"), GetString(messageObject, "authorId"), result.AuthorId);
                result.MessageType = FirstNonEmpty(result.MessageType, GetString(messageObject, "message_type"), GetString(messageObject, "messageType"), result.MessageType);
                result.LogId = FirstNonEmpty(result.LogId, GetString(messageObject, "log_id"), GetString(messageObject, "logId"), result.LogId);
                result.SentAt = FirstNonEmpty(result.SentAt, GetString(messageObject, "sent_at"), GetString(messageObject, "sentAt"), result.SentAt);
                result.IsMine = GetBoolean(messageObject, "isMine", GetBoolean(messageObject, "is_mine", result.IsMine));
                result.Recovered = GetBoolean(messageObject, "recovered", result.Recovered);
            }

            if (string.IsNullOrWhiteSpace(result.Detail) && string.Equals(result.Type, "status", StringComparison.OrdinalIgnoreCase))
            {
                result.Detail = GetString(root, "message");
            }

            if (string.Equals(result.Type, "room_metadata", StringComparison.OrdinalIgnoreCase))
            {
                // Numeric JSON values can already have lost precision before
                // parsing. Room metadata requires the exact string contract.
                object rawId;
                object rawTitle;
                object rawSource;
                if (!root.TryGetValue("chatId", out rawId) || !(rawId is string)
                    || !root.TryGetValue("chatTitle", out rawTitle) || !(rawTitle is string)
                    || !root.TryGetValue("titleSource", out rawSource) || !(rawSource is string)
                    || !RoomMetadataContract.ValidChatId((string)rawId)
                    || !RoomMetadataContract.ValidTitle((string)rawTitle)
                    || RoomMetadataContract.Priority((string)rawSource) == 0) return null;
                result.ChatId = (string)rawId;
                result.ChatTitle = (string)rawTitle;
                result.TitleSource = (string)rawSource;
                object rawAuthoritative;
                if (root.TryGetValue("authoritative", out rawAuthoritative))
                {
                    if (!(rawAuthoritative is bool)) return null;
                    result.Authoritative = (bool)rawAuthoritative;
                }
            }

            return result;
        }

        private static LocoCredentials ReadCredentials(Dictionary<string, object> values)
        {
            if (values == null)
            {
                return null;
            }

            LocoCredentials credentials = new LocoCredentials
            {
                OAuthToken = FirstNonEmpty(GetString(values, "oauth_token"), GetString(values, "oauthToken"), null),
                AccessToken = FirstNonEmpty(GetString(values, "access_token"), GetString(values, "accessToken"), null),
                RefreshToken = FirstNonEmpty(GetString(values, "refresh_token"), GetString(values, "refreshToken"), null),
                UserId = FirstNonEmpty(GetString(values, "user_id"), GetString(values, "userId"), null),
                DeviceUuid = FirstNonEmpty(GetString(values, "device_uuid"), GetString(values, "deviceUuid"), null),
                DeviceType = FirstNonEmpty(GetString(values, "device_type"), GetString(values, "deviceType"), null),
                AccountId = FirstNonEmpty(GetString(values, "account_id"), GetString(values, "accountId"), null),
                AuthMethod = FirstNonEmpty(GetString(values, "auth_method"), GetString(values, "authMethod"), null),
                CreatedAt = GetString(values, "created_at"),
                UpdatedAt = GetString(values, "updated_at")
            };

            // agent-messenger calls the login token access_token in its auth
            // result and oauthToken in its client login API.  Normalize that
            // harmless naming difference at the host boundary.
            if (string.IsNullOrWhiteSpace(credentials.OAuthToken))
            {
                credentials.OAuthToken = credentials.AccessToken;
            }

            if (!credentials.HasSessionToken
                && string.IsNullOrWhiteSpace(credentials.UserId)
                && string.IsNullOrWhiteSpace(credentials.DeviceUuid))
            {
                return null;
            }
            return credentials;
        }

        private static Dictionary<string, object> GetObject(Dictionary<string, object> root, string key)
        {
            object value;
            return root != null && root.TryGetValue(key, out value) ? value as Dictionary<string, object> : null;
        }

        private static string GetString(Dictionary<string, object> root, string key)
        {
            if (root == null || string.IsNullOrEmpty(key))
            {
                return null;
            }

            object value;
            if (!root.TryGetValue(key, out value) || value == null)
            {
                return null;
            }

            string text = value as string;
            if (text != null)
            {
                return text;
            }

            if (value is bool)
            {
                return ((bool)value) ? "true" : "false";
            }

            if (value is IFormattable)
            {
                return ((IFormattable)value).ToString(null, CultureInfo.InvariantCulture);
            }

            return null;
        }

        private static bool GetBoolean(Dictionary<string, object> root, string key, bool fallback)
        {
            if (root == null || string.IsNullOrEmpty(key))
            {
                return fallback;
            }

            object value;
            if (!root.TryGetValue(key, out value) || value == null)
            {
                return fallback;
            }

            if (value is bool)
            {
                return (bool)value;
            }

            bool parsed;
            return bool.TryParse(value as string, out parsed) ? parsed : fallback;
        }

        private static string FirstNonEmpty(string first, string second, string fallback)
        {
            return !string.IsNullOrWhiteSpace(first) ? first : (!string.IsNullOrWhiteSpace(second) ? second : fallback);
        }

        private static string FirstNonEmpty(string first, string second, string third, string fallback)
        {
            if (!string.IsNullOrWhiteSpace(first)) return first;
            if (!string.IsNullOrWhiteSpace(second)) return second;
            if (!string.IsNullOrWhiteSpace(third)) return third;
            return fallback;
        }
    }

    [Serializable]
    internal sealed class LocoStartRequest
    {
        public string Type { get; set; }
        public string DeviceType { get; set; }
        public bool Force { get; set; }
        public LocoCredentials Credentials { get; set; }
        public IList<LocoCatchUpCursor> CatchUp { get; set; }
    }

    [Serializable]
    internal sealed class LocoCatchUpCursor
    {
        public string ChatId { get; set; }
        public string AfterLogId { get; set; }
        public int Limit { get; set; }
    }

}
