using System;
using System.Collections.Generic;
using System.Linq;

namespace Spoiler.Tests
{
    internal static class GroupingTests
    {
        public static int Main()
        {
            int failures = 0;
            failures += AssertServerGroupKeyContract();
            failures += AssertStableIdentityGroupingAndOrder();
            failures += AssertSameNameDifferentRoomsStaySeparate();
            failures += AssertLegacyRecordsRemainVisible();

            Console.WriteLine(failures == 0
                ? "PASS: notification grouping tests"
                : "FAIL: " + failures + " notification grouping tests");
            return failures == 0 ? 0 : 1;
        }

        private static int AssertServerGroupKeyContract()
        {
            string first = NotificationGrouping.CreateServerGroupKey("room-a", "author-a");
            string same = NotificationGrouping.CreateServerGroupKey(" room-a ", " author-a ");
            string otherRoom = NotificationGrouping.CreateServerGroupKey("room-b", "author-a");
            string otherAuthor = NotificationGrouping.CreateServerGroupKey("room-a", "author-b");
            string canonicalNumeric = NotificationGrouping.CreateServerGroupKey("00042", "0007");
            string plainNumeric = NotificationGrouping.CreateServerGroupKey("42", "7");

            if (string.IsNullOrWhiteSpace(first)
                || !first.StartsWith("group:v1:", StringComparison.Ordinal)
                || first != same || first == otherRoom || first == otherAuthor
                || canonicalNumeric != plainNumeric
                || NotificationGrouping.CreateServerGroupKey(null, "author-a") != null
                || NotificationGrouping.CreateServerGroupKey("room-a", " ") != null)
            {
                Console.WriteLine("Server group-key stability/separation/missing-ID contract failed.");
                return 1;
            }
            return 0;
        }

        private static int AssertStableIdentityGroupingAndOrder()
        {
            DateTime anchor = new DateTime(2026, 8, 28, 12, 0, 0, DateTimeKind.Local);
            string roomAAuthorA = NotificationGrouping.CreateServerGroupKey("room-a", "author-a");
            string roomBAuthorA = NotificationGrouping.CreateServerGroupKey("room-b", "author-a");
            string roomAAuthorB = NotificationGrouping.CreateServerGroupKey("room-a", "author-b");
            IList<NotificationGroup> groups = NotificationGrouping.Build(new[]
            {
                Message("older", "예전 표시명", roomAAuthorA, anchor.AddMinutes(1)),
                Message("other room newest", "민수", roomBAuthorA, anchor.AddMinutes(3)),
                Message("latest", "민수", roomAAuthorA, anchor.AddMinutes(2)),
                Message("other author", "민수", roomAAuthorB, anchor)
            });

            if (groups.Count != 3
                || groups[0].Key != roomBAuthorA
                || groups[1].Key != roomAAuthorA
                || groups[1].Count != 2
                || groups[1].Sender != "민수"
                || groups[1].Latest == null || groups[1].Latest.Message != "latest"
                || groups[1].Messages[1].Message != "older"
                || groups[2].Key != roomAAuthorB)
            {
                Console.WriteLine("Stable identity grouping/latest-first order failed.");
                return 1;
            }
            return 0;
        }

        private static int AssertSameNameDifferentRoomsStaySeparate()
        {
            DateTime now = DateTime.Now;
            IList<NotificationGroup> groups = NotificationGrouping.Build(new[]
            {
                Message("방 A", "같은 이름", NotificationGrouping.CreateServerGroupKey("room-a", "author-1"), now),
                Message("방 B", "같은 이름", NotificationGrouping.CreateServerGroupKey("room-b", "author-1"), now.AddSeconds(-1))
            });

            if (groups.Count != 2 || groups.Any(group => group.Count != 1))
            {
                Console.WriteLine("Same-name senders in different rooms were merged.");
                return 1;
            }
            return 0;
        }

        private static int AssertLegacyRecordsRemainVisible()
        {
            DateTime now = DateTime.Now;
            IList<NotificationGroup> groups = NotificationGrouping.Build(new NotificationPreview[]
            {
                Message("새 기록", " Legacy Name ", null, now),
                Message("이전 기록", "legacy name", null, now.AddMinutes(-1)),
                null
            });

            if (groups.Count != 1 || groups[0].Count != 2
                || groups[0].Key != "legacy:LEGACY NAME"
                || groups[0].Latest == null || groups[0].Latest.Message != "새 기록")
            {
                Console.WriteLine("Legacy null-identity records were not preserved/grouped.");
                return 1;
            }
            return 0;
        }

        private static NotificationPreview Message(
            string text,
            string sender,
            string groupKey,
            DateTime receivedAt)
        {
            return new NotificationPreview
            {
                Id = Guid.NewGuid().ToString("N"),
                Sender = sender,
                Message = text,
                ReceivedAt = receivedAt,
                GroupKey = groupKey
            };
        }
    }
}
