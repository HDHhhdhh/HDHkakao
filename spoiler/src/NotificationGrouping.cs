using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Security.Cryptography;
using System.Text;

namespace Spoiler
{
    internal sealed class NotificationGroup
    {
        public string Key { get; private set; }
        public string Sender { get; private set; }
        public IList<NotificationPreview> Messages { get; private set; }

        public NotificationPreview Latest
        {
            get { return Messages.Count == 0 ? null : Messages[0]; }
        }

        public int Count
        {
            get { return Messages.Count; }
        }

        public NotificationGroup(string key, string sender)
        {
            Key = key;
            Sender = sender;
            Messages = new List<NotificationPreview>();
        }

        public void Add(NotificationPreview item)
        {
            if (item == null)
            {
                return;
            }
            if (Messages.Count == 0 && !string.IsNullOrWhiteSpace(item.Sender))
            {
                Sender = item.Sender;
            }
            Messages.Add(item);
        }
    }

    internal static class NotificationGrouping
    {
        public static IList<NotificationGroup> Build(IEnumerable<NotificationPreview> notifications)
        {
            List<NotificationPreview> ordered = (notifications ?? Enumerable.Empty<NotificationPreview>())
                .Select((item, index) => new KeyValuePair<int, NotificationPreview>(index, item))
                .Where(pair => pair.Value != null)
                .OrderByDescending(pair => pair.Value.ReceivedAt)
                .ThenBy(pair => pair.Key)
                .ThenBy(pair => pair.Value.Id ?? string.Empty, StringComparer.Ordinal)
                .Select(pair => pair.Value)
                .ToList();

            Dictionary<string, NotificationGroup> byKey = new Dictionary<string, NotificationGroup>(StringComparer.Ordinal);
            List<NotificationGroup> groups = new List<NotificationGroup>();
            foreach (NotificationPreview item in ordered)
            {
                string key = StableKey(item);
                NotificationGroup group;
                if (!byKey.TryGetValue(key, out group))
                {
                    group = new NotificationGroup(key, DisplaySender(item));
                    byKey.Add(key, group);
                    groups.Add(group);
                }
                group.Add(item);
            }
            return groups;
        }

        public static string StableKey(NotificationPreview item)
        {
            if (item != null && !string.IsNullOrWhiteSpace(item.GroupKey))
            {
                return item.GroupKey.Trim();
            }

            // Legacy records predate server identity persistence. Group them
            // by their visible sender instead of dropping or rewriting them.
            string sender = DisplaySender(item).Trim().ToUpperInvariant();
            return "legacy:" + sender;
        }

        public static string CreateServerGroupKey(string chatId, string authorId)
        {
            string canonicalChatId = CanonicalizeServerId(chatId);
            string canonicalAuthorId = CanonicalizeServerId(authorId);
            if (canonicalChatId == null || canonicalAuthorId == null)
            {
                return null;
            }

            byte[] value = Encoding.UTF8.GetBytes(canonicalChatId + "\u001F" + canonicalAuthorId);
            byte[] hash;
            using (SHA256 algorithm = SHA256.Create())
            {
                hash = algorithm.ComputeHash(value);
            }

            string base64Url = Convert.ToBase64String(hash)
                .TrimEnd('=')
                .Replace('+', '-')
                .Replace('/', '_');
            return "group:v1:" + base64Url;
        }

        private static string CanonicalizeServerId(string value)
        {
            if (string.IsNullOrWhiteSpace(value))
            {
                return null;
            }

            string trimmed = value.Trim();
            ulong numeric;
            if (ulong.TryParse(trimmed, NumberStyles.None, CultureInfo.InvariantCulture, out numeric))
            {
                return numeric.ToString(CultureInfo.InvariantCulture);
            }
            return trimmed;
        }

        private static string DisplaySender(NotificationPreview item)
        {
            return item == null || string.IsNullOrWhiteSpace(item.Sender)
                ? "카카오톡"
                : item.Sender.Trim();
        }
    }
}
