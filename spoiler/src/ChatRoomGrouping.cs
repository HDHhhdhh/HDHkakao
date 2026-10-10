using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Security.Cryptography;
using System.Text;

namespace Spoiler
{
    // A room is a projection of locally retained messages. Count is not the
    // Kakao unread count, and selecting a room has no transport side effects.
    internal sealed class ChatRoom
    {
        public string Key { get; private set; }
        public string ChatId { get; private set; }
        public string Title { get; private set; }
        public IList<NotificationPreview> Messages { get; private set; }
        public bool HasStableIdentity { get; private set; }
        public NotificationPreview Latest { get { return Messages.Count == 0 ? null : Messages[Messages.Count - 1]; } }
        public int Count { get { return Messages.Count; } }

        internal ChatRoom(string key, string title, string chatId, IList<NotificationPreview> messages)
        {
            Key = key;
            Title = title;
            ChatId = chatId;
            Messages = new List<NotificationPreview>(messages).AsReadOnly();
            HasStableIdentity = !string.IsNullOrWhiteSpace(chatId);
        }
    }

    internal static class ChatRoomGrouping
    {
        public static IList<ChatRoom> Build(IEnumerable<NotificationPreview> messages)
        {
            IEnumerable<NotificationPreview> source = messages ?? Enumerable.Empty<NotificationPreview>();
            List<ChatRoom> rooms = new List<ChatRoom>();
            List<NotificationPreview> records = source.Where(item => item != null).ToList();
            foreach (IGrouping<string, NotificationPreview> group in records
                .Where(item => CreateServerRoomKey(item.ChatId) != null).GroupBy(StableKey, StringComparer.Ordinal))
            {
                List<NotificationPreview> ordered = group.OrderBy(item => item.ReceivedAt)
                    .ThenBy(item => item.Id ?? string.Empty, MessageIdComparer.Instance).ToList();
                NotificationPreview titled = ordered.LastOrDefault(item => !string.IsNullOrWhiteSpace(item.RoomTitle));
                string chatId = ordered.Select(item => CanonicalizeChatId(item.ChatId)).FirstOrDefault(value => value != null);
                // When the server has no custom title, use the known writers
                // in this room, never whichever person happened to speak last.
                string title = titled == null ? UntitledRoomName(ordered, chatId) : titled.RoomTitle;
                rooms.Add(new ChatRoom(group.Key, title, chatId, ordered));
            }
            List<ChatRoom> orderedRooms = rooms.OrderByDescending(room => room.Latest.ReceivedAt)
                .ThenBy(room => room.Key, StringComparer.Ordinal).ToList();
            List<NotificationPreview> unidentified = records.Where(item => CreateServerRoomKey(item.ChatId) == null)
                .OrderBy(item => item.ReceivedAt).ThenBy(item => item.Id ?? string.Empty, MessageIdComparer.Instance).ToList();
            if (unidentified.Count != 0)
                orderedRooms.Add(new ChatRoom("archive:unassigned", "방 미확인 이전 기록", null, unidentified));
            return orderedRooms;
        }

        private static string UntitledRoomName(IList<NotificationPreview> messages, string chatId)
        {
            string[] writers = messages.Where(item => !item.IsMine).Select(DisplayAuthor)
                .Where(name => name != "알 수 없음" && name != "카카오톡")
                .Distinct(StringComparer.Ordinal).OrderBy(name => name, StringComparer.Ordinal).ToArray();
            if (writers.Length == 0) return "대화방 #" + chatId;
            string label = string.Join(", ", writers.Take(3).ToArray());
            return writers.Length > 3 ? label + " …" : label;
        }

        public static string StableKey(NotificationPreview item)
        {
            if (item == null) throw new ArgumentNullException("item");
            string roomKey = CreateServerRoomKey(item.ChatId);
            if (roomKey != null) return roomKey;

            // Previous group:v1 keys encoded both room and author, so they
            // cannot be reversed into room identity. Preserve those groups
            // rather than merging unrelated rooms with the same display name.
            if (!string.IsNullOrWhiteSpace(item.GroupKey)) return "legacy-group:" + item.GroupKey.Trim();
            if (string.IsNullOrWhiteSpace(item.ChatId) && !string.IsNullOrWhiteSpace(item.Sender))
                return "legacy-name:" + item.Sender.Trim().ToUpperInvariant();

            // No room or sender evidence exists for this old record. Keep it
            // visible without presenting all unidentified records as one room.
            string identity = !string.IsNullOrWhiteSpace(item.Id) ? item.Id.Trim()
                : item.ReceivedAt.Ticks.ToString(CultureInfo.InvariantCulture) + "\u001f" + (item.Message ?? string.Empty);
            return "legacy-message:" + Digest((item.ChatId ?? string.Empty) + "\u001f" + identity);
        }

        public static string CreateServerRoomKey(string chatId)
        {
            string identity = CanonicalizeChatId(chatId);
            return identity == null ? null : "room:v1:" + Digest(identity);
        }

        public static string DisplayTitle(NotificationPreview item)
        {
            if (item == null) return "대화방 정보 없음";
            if (!string.IsNullOrWhiteSpace(item.RoomTitle)) return item.RoomTitle;
            string value = !string.IsNullOrWhiteSpace(item.AuthorName) ? item.AuthorName : item.Sender;
            return string.IsNullOrWhiteSpace(value) ? "대화방 정보 없음" : value.Trim();
        }

        public static string DisplayAuthor(NotificationPreview item)
        {
            if (item == null) return "알 수 없음";
            string value = !string.IsNullOrWhiteSpace(item.AuthorName) ? item.AuthorName : item.Sender;
            return string.IsNullOrWhiteSpace(value) ? "알 수 없음" : value.Trim();
        }

        public static IList<NotificationPreview> FilterMessages(IEnumerable<NotificationPreview> messages,
            string searchText, bool includeMine, DateTime? fromInclusive, DateTime? toExclusive)
        {
            string query = (searchText ?? string.Empty).Trim();
            IEnumerable<NotificationPreview> selected = (messages ?? Enumerable.Empty<NotificationPreview>())
                .Where(item => item != null && (includeMine || !item.IsMine));
            if (fromInclusive.HasValue) selected = selected.Where(item => item.ReceivedAt >= fromInclusive.Value);
            if (toExclusive.HasValue) selected = selected.Where(item => item.ReceivedAt < toExclusive.Value);
            if (query.Length != 0) selected = selected.Where(item => Matches(item.RoomTitle, query)
                || Matches(item.AuthorName, query) || Matches(item.Sender, query) || Matches(item.Message, query));
            return selected.OrderBy(item => item.ReceivedAt)
                .ThenBy(item => item.Id ?? string.Empty, MessageIdComparer.Instance).ToList();
        }

        public static string ResolveSelection(IList<ChatRoom> rooms, string selectedKey)
        {
            if (rooms == null || rooms.Count == 0) return null;
            ChatRoom current = rooms.FirstOrDefault(room => string.Equals(room.Key, selectedKey, StringComparison.Ordinal));
            return current == null ? rooms[0].Key : current.Key;
        }

        public static string MessageForDisplay(NotificationPreview item)
        {
            if (item == null) return string.Empty;
            if (!string.IsNullOrWhiteSpace(item.Message)) return item.Message;
            switch (item.MessageType)
            {
                case 2: return "[사진 · 본문 없음]";
                case 3: return "[동영상 · 본문 없음]";
                case 5: return "[음성 메시지 · 본문 없음]";
                case 18: return "[파일 · 본문 없음]";
                case 26: return "[답장 · 본문 없음]";
                case 27: return "[사진 여러 장 · 본문 없음]";
                default: return item.MessageType > 0 ? "[첨부 메시지 · 본문 없음]" : "[저장된 본문 없음]";
            }
        }

        private static bool Matches(string text, string query)
        {
            return !string.IsNullOrEmpty(text) && text.IndexOf(query, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private static string CanonicalizeChatId(string value)
        {
            if (string.IsNullOrWhiteSpace(value)) return null;
            string identity = value.Trim();
            for (int index = 0; index < identity.Length; index++)
                if (identity[index] < '0' || identity[index] > '9') return null;
            long number;
            return long.TryParse(identity, NumberStyles.None, CultureInfo.InvariantCulture, out number) && number > 0
                ? number.ToString(CultureInfo.InvariantCulture) : null;
        }

        private static string Digest(string value)
        {
            byte[] hash;
            using (SHA256 algorithm = SHA256.Create()) hash = algorithm.ComputeHash(Encoding.UTF8.GetBytes(value));
            return Convert.ToBase64String(hash).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        }

        private sealed class MessageIdComparer : IComparer<string>
        {
            public static readonly MessageIdComparer Instance = new MessageIdComparer();
            public int Compare(string left, string right)
            {
                string first = DecimalIdentity(left);
                string second = DecimalIdentity(right);
                if (first != null && second != null)
                {
                    int length = first.Length.CompareTo(second.Length);
                    if (length != 0) return length;
                    int value = StringComparer.Ordinal.Compare(first, second);
                    if (value != 0) return value;
                }
                return StringComparer.Ordinal.Compare(left, right);
            }
            private static string DecimalIdentity(string value)
            {
                if (string.IsNullOrEmpty(value)) return null;
                for (int index = 0; index < value.Length; index++)
                    if (value[index] < '0' || value[index] > '9') return null;
                string canonical = value.TrimStart('0');
                return canonical.Length == 0 ? "0" : canonical;
            }
        }
    }
}
