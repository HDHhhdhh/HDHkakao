using System;
using System.Collections.Generic;
using System.Linq;

namespace Spoiler
{
    // Identity is captured for one batch only: previews can later be enriched.
    internal static class InboxUiMerge
    {
        private sealed class Entry
        {
            public NotificationPreview Preview;
            public string Id;
            public string Room;
            public string ChatId;
            public bool RoomCaptured;
            public string Group;
            public bool Removed;
        }

        internal static List<NotificationPreview> Merge(
            IEnumerable<NotificationPreview> current, IEnumerable<NotificationPreview> incoming)
        {
            Dictionary<NotificationPreview, Entry> identities = new Dictionary<NotificationPreview, Entry>();
            Dictionary<string, List<Entry>> updatesById = new Dictionary<string, List<Entry>>(StringComparer.Ordinal);
            List<Entry> orderedUpdates = new List<Entry>();
            foreach (NotificationPreview preview in incoming)
            {
                Entry update = Capture(preview, identities);
                if (!string.IsNullOrEmpty(update.Id))
                {
                    List<Entry> candidates;
                    if (!updatesById.TryGetValue(update.Id, out candidates))
                    {
                        candidates = new List<Entry>();
                        updatesById.Add(update.Id, candidates);
                    }
                    // Keep the original sequential replacement rule. A legacy
                    // group can match two rooms that do not match each other.
                    for (int i = candidates.Count - 1; i >= 0; i--)
                    {
                        if (!Matches(candidates[i], update)) continue;
                        candidates[i].Removed = true;
                        candidates.RemoveAt(i);
                    }
                    // A fresh entry preserves the last occurrence's position,
                    // including when the same preview instance occurs twice.
                    update = Copy(update);
                    candidates.Add(update);
                }
                orderedUpdates.Add(update);
            }
            List<NotificationPreview> merged = new List<NotificationPreview>();
            foreach (NotificationPreview preview in current)
            {
                List<Entry> candidates;
                if (preview != null && !string.IsNullOrEmpty(preview.Id) && updatesById.TryGetValue(preview.Id, out candidates))
                {
                    Entry item = Capture(preview, identities);
                    if (candidates.Any(update => Matches(item, update))) continue;
                }
                merged.Add(preview);
            }
            merged.AddRange(orderedUpdates.Where(entry => !entry.Removed).Select(entry => entry.Preview));
            return InboxStore.RetainMessages(merged).ToList();
        }

        private static Entry Capture(NotificationPreview preview, Dictionary<NotificationPreview, Entry> cache)
        {
            if (preview == null) return new Entry();
            Entry entry;
            if (cache.TryGetValue(preview, out entry)) return entry;
            entry = new Entry
            {
                Preview = preview, Id = preview.Id, Group = preview.GroupKey,
                ChatId = preview.ChatId
            };
            cache.Add(preview, entry);
            return entry;
        }

        private static Entry Copy(Entry entry)
        {
            return new Entry { Preview = entry.Preview, Id = entry.Id, Room = entry.Room, ChatId = entry.ChatId,
                RoomCaptured = entry.RoomCaptured, Group = entry.Group };
        }

        private static bool Matches(Entry existing, Entry incoming)
        {
            if (string.IsNullOrEmpty(existing.Id) || existing.Id != incoming.Id) return false;
            if (!existing.RoomCaptured) { existing.Room = ChatRoomGrouping.CreateServerRoomKey(existing.ChatId); existing.RoomCaptured = true; }
            if (!incoming.RoomCaptured) { incoming.Room = ChatRoomGrouping.CreateServerRoomKey(incoming.ChatId); incoming.RoomCaptured = true; }
            if (existing.Room != null && incoming.Room != null) return existing.Room == incoming.Room;
            return !string.IsNullOrEmpty(existing.Group) && existing.Group == incoming.Group;
        }
    }
}
