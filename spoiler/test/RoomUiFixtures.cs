using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;
using System.Linq;
using System.Reflection;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Threading;

namespace Spoiler.Tests
{
    internal static class RoomUiFixtures
    {
        public static readonly DateTime Anchor = DateTime.Today.AddHours(15).AddMinutes(56);

        public static IList<NotificationPreview> Messages()
        {
            return new List<NotificationPreview>
            {
                Message("1", "101", "초강스 🏫 (초심을 찾는 강사 스토리)", "산", "아 이거 터졌다 ㅋㅋㅋ", Anchor.AddMinutes(-4), false),
                Message("2", "101", "초강스 🏫 (초심을 찾는 강사 스토리)", "나", "아 진짜! 그 사진이 왜 터졌어? 😂", Anchor.AddMinutes(-3), true),
                Message("3", "101", "초강스 🏫 (초심을 찾는 강사 스토리)", "류재권", "ㅋㅋㅋㅋ 재욱쌤 또 실험중이다", Anchor.AddMinutes(-2), false),
                Message("4", "101", "초강스 🏫 (초심을 찾는 강사 스토리)", "park Joy", "ㅋㅋㅋ 미친 ㅋㅋㅋㅋㅋㅋ", Anchor.AddMinutes(-1), false),
                Message("5", "101", "초강스 🏫 (초심을 찾는 강사 스토리)", "나", "재욱쌤이 또 무슨 실험을 하시는 건가요? 😂", Anchor, true),
                Message("6", "102", "중간고사 보강 문의", "학생가", "오늘 결석해서 보강 시간 확인 부탁드려요.", Anchor.AddMinutes(-12), false),
                Message("7", "103", "중간고사 보강 문의", "김민수", "수업 자료 잘 받았습니다.", Anchor.AddMinutes(-18), false),
                new NotificationPreview { Id = "legacy", Sender = "이전 저장 기록", Message = "대화방 ID가 없는 기록은 읽기 전용입니다.", ReceivedAt = Anchor.AddMinutes(-24), GroupKey = "group:v1:legacy-preview" },
                new NotificationPreview { Id = "legacy-same-author", Sender = "산", AuthorName = "산", RoomTitle = "초강스 🏫 (초심을 찾는 강사 스토리)", Message = "UNKNOWN_ROOM_SAME_AUTHOR: 이 기록의 방은 확인되지 않았습니다.", ReceivedAt = Anchor.AddMinutes(-23), GroupKey = "group:v1:other-author-history" },
                new NotificationPreview { Id = "legacy-invalid-id", ChatId = "invalid-102", Sender = "학생가", RoomTitle = "중간고사 보강 문의", Message = "UNKNOWN_ROOM_INVALID_ID: 검증되지 않은 방 ID입니다.", ReceivedAt = Anchor.AddMinutes(-22) }
            };
        }

        public static NotificationPreview Message(string id, string chatId, string roomTitle, string author, string body, DateTime at, bool mine)
        {
            return new NotificationPreview
            {
                Id = id, ChatId = chatId, RoomTitle = roomTitle, Sender = author,
                AuthorName = author, Message = body, ReceivedAt = at, IsMine = mine,
                GroupKey = "group:v1:fixture:" + chatId + ":" + author
            };
        }

        public static void Seed(MainWindow window, IEnumerable<NotificationPreview> messages)
        {
            List<NotificationPreview> items = Get<List<NotificationPreview>>(window, "_items");
            items.Clear(); items.AddRange(messages);
            Invoke(window, "RenderList", false);
            Pump(window);
        }

        public static T Find<T>(DependencyObject root, string name) where T : FrameworkElement
        {
            return Children<T>(root).FirstOrDefault(item => item.Name == name);
        }

        public static IEnumerable<T> Children<T>(DependencyObject root) where T : DependencyObject
        {
            if (root == null) yield break;
            int count = VisualTreeHelper.GetChildrenCount(root);
            for (int index = 0; index < count; index++)
            {
                DependencyObject child = VisualTreeHelper.GetChild(root, index);
                T match = child as T;
                if (match != null) yield return match;
                foreach (T descendant in Children<T>(child)) yield return descendant;
            }
        }

        public static IList<string> Texts(DependencyObject root)
        {
            return Children<TextBlock>(root).Select(item => OriginalText(item))
                .Concat(Children<TextBox>(root).Select(item => OriginalText(item))).ToList();
        }

        public static string OriginalText(DependencyObject value)
        {
            PropertyInfo original = value.GetType().GetProperty("OriginalText");
            if (original != null) return (string)original.GetValue(value, null);
            TextBox box = value as TextBox;
            if (box != null) return box.Text;
            TextBlock block = value as TextBlock;
            return block == null ? string.Empty : block.Text;
        }

        public static void Pump(Window window)
        {
            window.UpdateLayout();
            window.Dispatcher.Invoke(DispatcherPriority.Background, new Action(delegate { }));
            window.UpdateLayout();
        }

        public static void WaitForReply(MainWindow window)
        {
            Stopwatch timeout = Stopwatch.StartNew();
            while (Get<bool>(window, "_replyPending") && timeout.ElapsedMilliseconds < 2500)
            {
                Pump(window);
                Thread.Sleep(2);
            }
            Pump(window);
            if (Get<bool>(window, "_replyPending")) throw new InvalidOperationException("The synthetic reply UI did not complete.");
        }

        public static object Invoke(object target, string name, params object[] args)
        {
            MethodInfo method = target.GetType().GetMethod(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (method == null) throw new InvalidOperationException("Missing UI test hook: " + name);
            return method.Invoke(target, args);
        }

        public static T Get<T>(object target, string name)
        {
            FieldInfo field = target.GetType().GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (field == null) throw new InvalidOperationException("Missing UI test field: " + name);
            return (T)field.GetValue(target);
        }

        public static void Set(object target, string name, object value)
        {
            target.GetType().GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic).SetValue(target, value);
        }

        public static string RoomValue(ListBoxItem item, string property)
        {
            object room = item == null ? null : item.Tag;
            if (room == null) return null;
            PropertyInfo member = room.GetType().GetProperty(property);
            return member == null ? null : member.GetValue(room, null) as string;
        }

        public static ListBoxItem SelectRoom(MainWindow window, string chatId)
        {
            ListBox list = Find<ListBox>(window, "RoomList");
            ListBoxItem item = list.Items.Cast<object>().OfType<ListBoxItem>()
                .FirstOrDefault(value => RoomValue(value, "ChatId") == chatId);
            if (item == null) throw new InvalidOperationException("Synthetic room missing: " + chatId);
            list.SelectedItem = item;
            Pump(window);
            return item;
        }
    }

    internal sealed class FakeReplySource : IReplyMessageSource
    {
        public bool CanReply { get; set; }
        public int Calls { get; private set; }
        public string LastChatId { get; private set; }
        public string LastText { get; private set; }
        private TaskCompletionSource<ReplySendResult> _pending;

        public FakeReplySource() { CanReply = true; }
        public Task<ReplySendResult> SendReplyAsync(string chatId, string text)
        {
            Calls++; LastChatId = chatId; LastText = text;
            _pending = new TaskCompletionSource<ReplySendResult>();
            return _pending.Task;
        }
        public void Complete(ReplySendStatus status)
        {
            if (_pending == null) throw new InvalidOperationException("No fake send pending.");
            TaskCompletionSource<ReplySendResult> current = _pending; _pending = null;
            current.SetResult(new ReplySendResult(status,
                status == ReplySendStatus.Sent ? "sent" : status == ReplySendStatus.Unknown ? "unknown" : "failed",
                status == ReplySendStatus.Sent ? "서버가 답장을 접수했습니다." : status == ReplySendStatus.Unknown ? "전송 결과를 확인하지 못했습니다." : "전송에 실패했습니다.",
                "fixture-request", LastChatId, status == ReplySendStatus.Sent ? "9001" : null));
        }
    }
}
