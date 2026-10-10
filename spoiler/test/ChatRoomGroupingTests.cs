using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.Serialization.Json;
using System.Text;

namespace Spoiler.Tests
{
    internal static class ChatRoomGroupingTests
    {
        private static readonly DateTime Anchor = new DateTime(2026, 9, 30, 12, 0, 0, DateTimeKind.Local);
        private static int _failures;

        public static int Main()
        {
            Run("room identity canonicalization", Identity);
            Run("invalid room ids stay in unidentified archive", InvalidIdentity);
            Run("group room combines different authors", RoomAndAuthor);
            Run("same title never joins different server rooms", SameTitle);
            Run("untitled room title summarizes observed other authors", UntitledTitle);
            Run("own-only untitled room gets neutral room identity", OwnOnlyTitle);
            Run("unidentified authors and titles cannot contaminate known rooms", ArchiveIsolation);
            Run("old author hashes stay in explicitly unidentified archive", Legacy);
            Run("unidentified legacy records remain visible", Unidentified);
            Run("chronology uses time then numeric log id", Chronology);
            Run("selection survives new room arrival", Selection);
            Run("filters preserve source and use exclusive end", Filters);
            Run("empty body and attachment placeholders", EmptyBody);
            Run("room projection is read-only", ReadOnlyProjection);
            Run("optional metadata preserves legacy JSON", LegacySerialization);
            Run("room metadata round-trips", MetadataSerialization);
            Run("saved room title keeps exact display characters", ExactSavedTitle);
            Run("renaming retains selection and chronology", RenameSelection);
            Console.WriteLine(_failures == 0 ? "PASS: 18 chat room model scenarios" : "FAIL: " + _failures + " chat room model scenarios");
            return _failures == 0 ? 0 : 1;
        }

        private static void Run(string name, Action test)
        {
            try { test(); Console.WriteLine("PASS: " + name); }
            catch (Exception error) { _failures++; Console.WriteLine("FAIL: " + name + ": " + error.Message); }
        }
        private static void Check(bool value) { if (!value) throw new InvalidOperationException("Contract assertion failed."); }
        private static NotificationPreview Item(string id, string chatId, string author, string text, int minutes)
        {
            return new NotificationPreview { Id = id, ChatId = chatId, AuthorName = author, Sender = author,
                RoomTitle = "수학 수업", Message = text, ReceivedAt = Anchor.AddMinutes(minutes) };
        }

        private static void Identity()
        {
            string key = ChatRoomGrouping.CreateServerRoomKey("00042");
            Check(key == ChatRoomGrouping.CreateServerRoomKey(" 42 "));
            Check(key != ChatRoomGrouping.CreateServerRoomKey("43"));
            Check(key.StartsWith("room:v1:", StringComparison.Ordinal));
            Check(ChatRoomGrouping.CreateServerRoomKey("9223372036854775807")
                .IndexOf("9223372036854775807", StringComparison.Ordinal) < 0);
            Check(ChatRoomGrouping.CreateServerRoomKey(null) == null && ChatRoomGrouping.CreateServerRoomKey(" ") == null);
        }

        private static void InvalidIdentity()
        {
            foreach (string id in new[] { "0", "-1", "+42", "1.2", "room-a", "４２", "9223372036854775808" })
                Check(ChatRoomGrouping.CreateServerRoomKey(id) == null);
            NotificationPreview first = Item("1", "invalid-a", "같은 이름", "A", 0);
            NotificationPreview second = Item("2", "invalid-b", "같은 이름", "B", 1);
            first.GroupKey = "group:v1:legacy-a"; second.GroupKey = "group:v1:legacy-b";
            NotificationPreview third = Item("same-log-id", "invalid-a", "같은 이름", "C", 2);
            NotificationPreview fourth = Item("same-log-id", "invalid-b", "같은 이름", "D", 3);
            NotificationPreview fifth = Item("5", null, "같은 이름", "E", 4);
            IList<ChatRoom> rooms = ChatRoomGrouping.Build(new[] { first, second, third, fourth, fifth });
            Check(rooms.Count == 1 && !rooms[0].HasStableIdentity && rooms[0].ChatId == null && rooms[0].Count == 5);
            Check(rooms[0].Title == "방 미확인 이전 기록" && !string.IsNullOrWhiteSpace(rooms[0].Key));
            Check(rooms[0].Key != ChatRoomGrouping.CreateServerRoomKey("42"));
        }

        private static void RoomAndAuthor()
        {
            NotificationPreview oldest = Item("1", "42", "학생 A", "질문", 0);
            oldest.GroupKey = "old-author-a";
            NotificationPreview latest = Item("2", "00042", "학생 B", "답변", 1);
            latest.GroupKey = "old-author-b";
            latest.RoomTitle = null;
            NotificationPreview mine = Item("3", "42", "나", "내 답장", 2); mine.IsMine = true;
            mine.RoomTitle = null;
            IList<ChatRoom> rooms = ChatRoomGrouping.Build(new[] { latest, mine, oldest });
            Check(rooms.Count == 1 && rooms[0].Count == 3 && rooms[0].ChatId == "42" && rooms[0].HasStableIdentity);
            Check(rooms[0].Title == "수학 수업" && rooms[0].Latest == mine);
            Check(rooms[0].Messages[0] == oldest && rooms[0].Messages[1] == latest && rooms[0].Messages[2] == mine);
            Check(ChatRoomGrouping.DisplayAuthor(latest) == "학생 B");
        }

        private static void SameTitle()
        {
            IList<ChatRoom> rooms = ChatRoomGrouping.Build(new[] {
                Item("1", "42", "학생", "방 A", 0), Item("2", "43", "학생", "방 B", 1) });
            Check(rooms.Count == 2 && rooms.All(room => room.Count == 1));
            Check(rooms[0].ChatId == "43" && rooms[1].ChatId == "42");
        }

        private static void UntitledTitle()
        {
            NotificationPreview first = Item("1", "42", "학생 A", "첫 질문", 0); first.RoomTitle = null;
            NotificationPreview second = Item("2", "42", "학생 B", "다른 질문", 1); second.RoomTitle = null;
            NotificationPreview mine = Item("3", "42", "나", "내 답변", 2); mine.RoomTitle = null; mine.IsMine = true;
            ChatRoom initial = ChatRoomGrouping.Build(new[] { first, second, mine }).Single();
            Check(initial.Title.Contains("학생 A") && initial.Title.Contains("학생 B") && initial.Title != "나");
            NotificationPreview latest = Item("4", "42", "학생 A", "마지막 질문", 3); latest.RoomTitle = null;
            ChatRoom changed = ChatRoomGrouping.Build(new[] { latest, mine, second, first }).Single();
            Check(changed.Key == initial.Key && changed.Title == initial.Title && changed.Count == 4);
        }

        private static void OwnOnlyTitle()
        {
            NotificationPreview mine = Item("1", "42", "나", "내 답장만 보관됨", 0); mine.RoomTitle = null; mine.IsMine = true;
            ChatRoom room = ChatRoomGrouping.Build(new[] { mine }).Single();
            Check(room.HasStableIdentity && room.Title.Contains("대화방") && room.Title.Contains("42") && room.Title != "나");
        }

        private static void ExactSavedTitle()
        {
            NotificationPreview mine = Item("1", "42", "나", "내 메시지", 0);
            mine.IsMine = true;
            mine.RoomTitle = "  Ａ 반  가 ✨  ";
            ChatRoom room = ChatRoomGrouping.Build(new[] { mine }).Single();
            Check(room.Title == mine.RoomTitle && ChatRoomGrouping.DisplayTitle(mine) == mine.RoomTitle);
        }

        private static void RenameSelection()
        {
            NotificationPreview first = Item("1", "42", "학생", "질문", 0);
            NotificationPreview mine = Item("2", "42", "나", "답장", 1); mine.IsMine = true;
            ChatRoom before = ChatRoomGrouping.Build(new[] { first, mine }).Single();
            first.RoomTitle = mine.RoomTitle = "저장한 새 이름";
            IList<ChatRoom> after = ChatRoomGrouping.Build(new[] { mine, first });
            Check(ChatRoomGrouping.ResolveSelection(after, before.Key) == before.Key);
            Check(after[0].Title == "저장한 새 이름" && after[0].Messages[0] == first && after[0].Latest == mine);
        }

        private static void ArchiveIsolation()
        {
            NotificationPreview known = Item("1", "42", "동명이인", "확인된 실제방 내용", 0);
            known.GroupKey = "group:v1:shared-display";
            NotificationPreview legacy = Item("2", null, "동명이인", "미확인 이전 내용", 1);
            legacy.GroupKey = known.GroupKey;
            NotificationPreview invalid = Item("3", "invalid-42", "동명이인", "잘못된 방 ID", 2);
            IList<ChatRoom> rooms = ChatRoomGrouping.Build(new[] { known, legacy, invalid });
            ChatRoom actual = rooms.Single(room => room.HasStableIdentity);
            ChatRoom archive = rooms.Single(room => !room.HasStableIdentity);
            Check(rooms.Count == 2 && actual.ChatId == "42" && actual.Count == 1 && actual.Messages[0] == known);
            Check(archive.Title == "방 미확인 이전 기록" && archive.ChatId == null && archive.Count == 2);
            Check(archive.Messages.Contains(legacy) && archive.Messages.Contains(invalid));
            Check(actual.Key != archive.Key);
        }

        private static void Legacy()
        {
            NotificationPreview first = Item("1", null, "동명이인", "예전 A", 0);
            NotificationPreview second = Item("2", null, "동명이인", "예전 B", 1);
            first.GroupKey = "group:v1:author-a"; second.GroupKey = "group:v1:author-b";
            NotificationPreview renamed = Item("3", null, "바뀐 이름", "예전 A 최근", 2);
            renamed.GroupKey = first.GroupKey;
            IList<ChatRoom> rooms = ChatRoomGrouping.Build(new[] { first, second, renamed });
            Check(rooms.Count == 1 && rooms[0].Count == 3 && rooms[0].Latest == renamed);
            Check(rooms[0].Title == "방 미확인 이전 기록" && !rooms[0].HasStableIdentity && rooms[0].ChatId == null);
            first.GroupKey = null; second.GroupKey = null; first.Sender = " Legacy "; second.Sender = "legacy";
            Check(ChatRoomGrouping.Build(new[] { first, second }).Count == 1);
        }

        private static void Unidentified()
        {
            NotificationPreview first = Item("1", null, null, "", 0);
            NotificationPreview second = Item("2", null, null, "", 0);
            first.RoomTitle = null; second.RoomTitle = null;
            IList<ChatRoom> rooms = ChatRoomGrouping.Build(new[] { first, null, second });
            Check(rooms.Count == 1 && rooms[0].Count == 2 && rooms[0].Title == "방 미확인 이전 기록" && !rooms[0].HasStableIdentity);
            Check(ChatRoomGrouping.Build(null).Count == 0);
        }

        private static void Chronology()
        {
            NotificationPreview older = Item("999", "42", "A", "이전", -1);
            NotificationPreview two = Item("2", "42", "A", "두 번째", 0);
            NotificationPreview ten = Item("10", "42", "A", "열 번째", 0);
            NotificationPreview huge = Item("184467440737095516160000", "42", "A", "큰 log id", 0);
            ChatRoom room = ChatRoomGrouping.Build(new[] { huge, ten, older, two }).Single();
            Check(room.Messages.Select(item => item.Id).SequenceEqual(new[] { "999", "2", "10", "184467440737095516160000" }));
            Check(room.Latest == huge);
        }

        private static void Selection()
        {
            NotificationPreview selected = Item("1", "42", "A", "선택", 0);
            IList<ChatRoom> initial = ChatRoomGrouping.Build(new[] { selected });
            string selection = ChatRoomGrouping.ResolveSelection(initial, null);
            IList<ChatRoom> arrived = ChatRoomGrouping.Build(new[] { selected, Item("2", "43", "B", "새 방", 1) });
            Check(arrived[0].ChatId == "43" && ChatRoomGrouping.ResolveSelection(arrived, selection) == selection);
            Check(ChatRoomGrouping.ResolveSelection(arrived, "missing") == arrived[0].Key);
            Check(ChatRoomGrouping.ResolveSelection(new List<ChatRoom>(), selection) == null);
            Check(ChatRoomGrouping.ResolveSelection(null, selection) == null);
        }

        private static void Filters()
        {
            NotificationPreview first = Item("1", "42", "Student A", "Homework question", 0);
            NotificationPreview mine = Item("2", "42", "나", "Homework answer", 1); mine.IsMine = true;
            NotificationPreview end = Item("3", "42", "Student B", "다음 시간", 2);
            List<NotificationPreview> source = new List<NotificationPreview> { end, mine, first, null };
            IList<NotificationPreview> result = ChatRoomGrouping.FilterMessages(source, " HOMEWORK ", false, Anchor, Anchor.AddMinutes(2));
            Check(result.Count == 1 && result[0] == first && source.Count == 4 && source[0] == end);
            Check(ChatRoomGrouping.FilterMessages(source, "student b", true, null, null).Single() == end);
            Check(ChatRoomGrouping.FilterMessages(source, "수학", true, null, null).Count == 3);
            Check(ChatRoomGrouping.FilterMessages(source, "", true, Anchor, Anchor.AddMinutes(2)).Count == 2);
            Check(ChatRoomGrouping.FilterMessages(source, "", true, Anchor.AddMinutes(2), Anchor).Count == 0);
            Check(ChatRoomGrouping.FilterMessages(null, null, true, null, null).Count == 0);
        }

        private static void EmptyBody()
        {
            NotificationPreview empty = Item("1", "42", "A", null, 0);
            Check(ChatRoomGrouping.MessageForDisplay(empty) == "[저장된 본문 없음]");
            empty.MessageType = 2; Check(ChatRoomGrouping.MessageForDisplay(empty) == "[사진 · 본문 없음]");
            empty.MessageType = 999; Check(ChatRoomGrouping.MessageForDisplay(empty) == "[첨부 메시지 · 본문 없음]");
            empty.Message = "원문\r\n보존"; Check(ChatRoomGrouping.MessageForDisplay(empty) == empty.Message);
            Check(ChatRoomGrouping.MessageForDisplay(null) == string.Empty);
        }

        private static void ReadOnlyProjection()
        {
            NotificationPreview item = Item("1", "42", "A", "원문", 0);
            List<NotificationPreview> source = new List<NotificationPreview> { item };
            ChatRoom room = ChatRoomGrouping.Build(source).Single();
            bool rejected = false;
            try { room.Messages.Add(Item("2", "42", "B", "추가", 1)); }
            catch (NotSupportedException) { rejected = true; }
            ChatRoomGrouping.ResolveSelection(new List<ChatRoom> { room }, room.Key);
            Check(rejected && source.Count == 1 && item.Message == "원문" && item.Id == "1");
        }

        private static void LegacySerialization()
        {
            NotificationPreview legacy = new NotificationPreview { Id = "old", Sender = "기존 이름", Message = "기존 기록", ReceivedAt = Anchor };
            string json = Serialize(legacy);
            Check(!json.Contains("ChatId") && !json.Contains("RoomTitle") && !json.Contains("AuthorName"));
            NotificationPreview loaded = Deserialize(json);
            Check(loaded.Message == "기존 기록" && loaded.ChatId == null && loaded.RoomTitle == null && loaded.AuthorName == null);
            Check(!ChatRoomGrouping.Build(new[] { loaded }).Single().HasStableIdentity);
        }

        private static void MetadataSerialization()
        {
            NotificationPreview item = Item("1", "42", "실제 작성자", "원문", 0);
            NotificationPreview loaded = Deserialize(Serialize(item));
            Check(loaded.ChatId == "42" && loaded.RoomTitle == "수학 수업" && loaded.AuthorName == "실제 작성자");
            ChatRoom room = ChatRoomGrouping.Build(new[] { loaded }).Single();
            Check(room.HasStableIdentity && room.ChatId == "42" && room.Title == "수학 수업");
        }

        private static string Serialize(NotificationPreview item)
        {
            using (MemoryStream stream = new MemoryStream())
            {
                new DataContractJsonSerializer(typeof(NotificationPreview)).WriteObject(stream, item);
                return Encoding.UTF8.GetString(stream.ToArray());
            }
        }
        private static NotificationPreview Deserialize(string json)
        {
            using (MemoryStream stream = new MemoryStream(Encoding.UTF8.GetBytes(json)))
                return (NotificationPreview)new DataContractJsonSerializer(typeof(NotificationPreview)).ReadObject(stream);
        }
    }
}
