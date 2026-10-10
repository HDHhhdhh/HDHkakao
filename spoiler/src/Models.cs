using System;
using System.Runtime.Serialization;

namespace Spoiler
{
    [DataContract]
    public sealed class NotificationPreview : EventArgs
    {
        [DataMember(Order = 1)]
        public string Id { get; set; }

        [DataMember(Order = 2)]
        public string Sender { get; set; }

        [DataMember(Order = 3)]
        public string Message { get; set; }

        [DataMember(Order = 4)]
        public DateTime ReceivedAt { get; set; }

        [DataMember(Order = 5)]
        public string Source { get; set; }

        // A versioned one-way key groups messages without persisting Kakao's
        // raw room/member identifiers. Older history files deserialize this
        // field as null and remain visible through the sender-name fallback.
        [DataMember(Order = 6, EmitDefaultValue = false)]
        public string GroupKey { get; set; }

        // True only when the bridge matched the message author id to the
        // registered tablet account user id.  Omit false so old five-field
        // history remains byte-compatible while missing values deserialize
        // safely as false.
        [DataMember(Order = 7, EmitDefaultValue = false)]
        public bool IsMine { get; set; }

        // Numeric Kakao message kind retained from the read-only bridge.
        // Zero means unknown/legacy.  Keeping the value lets the UI show a
        // safe placeholder for attachment-only events without downloading
        // any media or opening the chat room.
        [DataMember(Order = 8, EmitDefaultValue = false)]
        public int MessageType { get; set; }

        // Room identity is retained inside the existing encrypted,
        // account-scoped inbox. Old author-scoped GroupKey hashes cannot be
        // reversed into a chat id; those records stay read-only.
        [DataMember(Order = 9, EmitDefaultValue = false)]
        public string ChatId { get; set; }

        [DataMember(Order = 10, EmitDefaultValue = false)]
        public string RoomTitle { get; set; }

        [DataMember(Order = 11, EmitDefaultValue = false)]
        public string AuthorName { get; set; }

        [DataMember(Order = 12, EmitDefaultValue = false)]
        public string RoomTitleSource { get; set; }

        [IgnoreDataMember]
        public string DemoAvatarResource { get; set; }

        // Set only by the account-scoped inbox. Queued UI events use this
        // value to reject a previous account after a registration change.
        [IgnoreDataMember]
        public string AccountId { get; set; }

        public NotificationPreview()
        {
            Id = Guid.NewGuid().ToString("N");
            Sender = "알 수 없음";
            Message = string.Empty;
            ReceivedAt = DateTime.Now;
            Source = "KakaoTalk notification";
        }
    }

    // The room name is metadata, never a guessed sender label. Preserve the
    // original Unicode string while rejecting ambiguous IDs and unsafe text.
    internal static class RoomMetadataContract
    {
        internal static bool ValidChatId(string value)
        {
            long number;
            return !string.IsNullOrEmpty(value) && value.Length <= 19 && value[0] != '0'
                && System.Array.TrueForAll(value.ToCharArray(), ch => ch >= '0' && ch <= '9')
                && long.TryParse(value, System.Globalization.NumberStyles.None,
                    System.Globalization.CultureInfo.InvariantCulture, out number) && number > 0
                && number.ToString(System.Globalization.CultureInfo.InvariantCulture) == value;
        }

        internal static bool ValidTitle(string value)
        {
            if (string.IsNullOrWhiteSpace(value) || value.Length > 256) return false;
            for (int index = 0; index < value.Length; index++)
            {
                char character = value[index];
                if (char.IsControl(character) || char.IsLowSurrogate(character)) return false;
                if (char.IsHighSurrogate(character))
                {
                    if (index + 1 >= value.Length || !char.IsLowSurrogate(value[index + 1])) return false;
                    index++;
                }
            }
            return true;
        }

        internal static int Priority(string source)
        {
            switch (source)
            {
                case "synced_room_name": return 4;
                case "synced_friend_name": return 3;
                case "server_title": return 2;
                case "participant_names": return 1;
                default: return 0;
            }
        }
    }

    public enum CaptureState
    {
        Starting,
        KakaoTalkNotRunning,
        NotificationsDisabled,
        Watching,
        Captured,
        Error
    }

    public sealed class CaptureStateChangedEventArgs : EventArgs
    {
        public CaptureState State { get; private set; }
        public string Detail { get; private set; }

        public CaptureStateChangedEventArgs(CaptureState state, string detail)
        {
            State = state;
            Detail = detail ?? string.Empty;
        }
    }
}
