using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Spoiler;

internal static class InboxUiMergeTests
{
    private static readonly MethodInfo RoomKey = typeof(MainWindow).Assembly.GetType("Spoiler.ChatRoomGrouping").GetMethod("CreateServerRoomKey", BindingFlags.Static | BindingFlags.Public);
    private static bool Same(NotificationPreview a, NotificationPreview b)
    {
        if (a == null || b == null || string.IsNullOrEmpty(a.Id) || a.Id != b.Id) return false;
        string x = (string)RoomKey.Invoke(null, new object[] { a.ChatId });
        string y = (string)RoomKey.Invoke(null, new object[] { b.ChatId });
        if (x != null && y != null) return x == y;
        return !string.IsNullOrEmpty(a.GroupKey) && a.GroupKey == b.GroupKey;
    }

    public static int Main()
    {
        try
        {
            MethodInfo merge = typeof(MainWindow).Assembly.GetType("Spoiler.InboxUiMerge").GetMethod("Merge", BindingFlags.Static | BindingFlags.NonPublic);
            MethodInfo retain = typeof(MainWindow).Assembly.GetType("Spoiler.InboxStore").GetMethod("RetainMessages", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
            Random random = new Random(31415);
            List<NotificationPreview> pool = new List<NotificationPreview>();
            string[] rooms = { null, "123", "456", "00123", "bad" };
            string[] ids = { null, "", "one", "two", "three" };
            string[] groups = { null, "", "shared", "other" };
            for (int i = 0; i < 100; i++)
                pool.Add(new NotificationPreview { Id = ids[i % ids.Length], ChatId = rooms[(i / 5) % rooms.Length],
                    GroupKey = groups[(i / 25) % groups.Length], Message = "row " + i, ReceivedAt = DateTime.Now.AddMinutes(-i) });
            pool.Add(null);
            for (int trial = 0; trial < 400; trial++)
            {
                List<NotificationPreview> current = Enumerable.Range(0, 40).Select(i => pool[random.Next(pool.Count)]).ToList();
                List<NotificationPreview> incoming = Enumerable.Range(0, 25).Select(i => pool[random.Next(pool.Count)]).ToList();
                List<NotificationPreview> latest = new List<NotificationPreview>();
                foreach (NotificationPreview update in incoming)
                {
                    latest.RemoveAll(item => Same(item, update));
                    latest.Add(update);
                }
                object rows = current.Where(item => !latest.Any(update => Same(item, update))).Concat(latest);
                IEnumerable<NotificationPreview> expected = (IEnumerable<NotificationPreview>)retain.Invoke(null, new[] { rows });
                IEnumerable<NotificationPreview> actual = (IEnumerable<NotificationPreview>)merge.Invoke(null, new object[] { current, incoming });
                if (!expected.SequenceEqual(actual)) throw new Exception("Identity or order changed in trial " + trial);
            }
            Console.WriteLine("PASS: UI merge preserves identity, order and retention (400 mixed batches)");
            return 0;
        }
        catch (Exception error) { Console.WriteLine("FAIL: " + error); return 1; }
    }
}
