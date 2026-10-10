using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Controls;

namespace Spoiler.Tests
{
    // Offline synthetic records only. The source is null in QA mode, and the
    // injected reply endpoint records calls rather than performing any write.
    internal static class RoomRenderingTests
    {
        private static int _checks;
        [STAThread]
        public static int Main()
        {
            Application application = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
            try
            {
                ReuseAndMutation();
                FilterAndGeneration();
                VisitedRoomReuse();
                Console.WriteLine("ROOM_RENDERING_CHECKS=" + _checks + " FAILURES=0");
                return 0;
            }
            catch (Exception exception)
            {
                Console.WriteLine("ROOM_RENDERING_FAILURE=" + exception);
                return 1;
            }
            finally { application.Shutdown(); }
        }

        private static void Check(bool condition, string message)
        {
            _checks++;
            if (!condition) throw new InvalidOperationException(message);
        }

        private static TextBox Bubble(MainWindow window, string text)
        {
            return RoomUiFixtures.Children<TextBox>(RoomUiFixtures.Find<StackPanel>(window, "MessageTimeline"))
                .SingleOrDefault(box => RoomUiFixtures.OriginalText(box) == text);
        }

        private static MainWindow Window(FakeReplySource endpoint)
        {
            MainWindow window = new MainWindow(false, true, endpoint);
            window.Width = 960; window.Height = 800; window.Show();
            RoomUiFixtures.Seed(window, RoomUiFixtures.Messages());
            RoomUiFixtures.SelectRoom(window, "101");
            return window;
        }

        private static void ReuseAndMutation()
        {
            FakeReplySource endpoint = new FakeReplySource();
            MainWindow window = Window(endpoint);
            try
            {
                ListBoxItem selected = RoomUiFixtures.SelectRoom(window, "101");
                string firstBody = "아 이거 터졌다 ㅋㅋㅋ";
                string changedBody = "ㅋㅋㅋㅋ 재욱쌤 또 실험중이다";
                TextBox first = Bubble(window, firstBody);
                TextBox replace = Bubble(window, changedBody);
                object retained = RoomUiFixtures.Get<object>(window, "_retainedRooms");
                RoomUiFixtures.Invoke(window, "RenderList", false); RoomUiFixtures.Pump(window);
                Check(ReferenceEquals(first, Bubble(window, firstBody)), "A no-op refresh rebuilt the selected bubble.");
                Check(ReferenceEquals(selected, RoomUiFixtures.SelectRoom(window, "101")), "A no-op refresh rebuilt the room row.");
                Check(ReferenceEquals(retained, RoomUiFixtures.Get<object>(window, "_retainedRooms")), "A no-op refresh rebuilt retained room grouping.");
                TextBox search = RoomUiFixtures.Find<TextBox>(window, "RoomSearchInput");
                search.Text = "초"; search.Text = "초강"; search.Text = "초강스";
                RoomUiFixtures.Pump(window);
                Check(ReferenceEquals(first, Bubble(window, firstBody)), "Search rebuilt an unchanged selected timeline.");
                Check(ReferenceEquals(retained, RoomUiFixtures.Get<object>(window, "_retainedRooms")), "Text-only search regrouped unchanged records.");
                search.Clear(); RoomUiFixtures.Pump(window);
                Check(ReferenceEquals(selected, RoomUiFixtures.SelectRoom(window, "101")), "Clearing search discarded the cached room row.");

                List<NotificationPreview> items = RoomUiFixtures.Get<List<NotificationPreview>>(window, "_items");
                NotificationPreview mutable = items.Single(item => item.Id == "3");
                mutable.Message = "수정된 전체 원문\n다음 줄도 보존합니다.";
                mutable.AuthorName = "수정된 작성자";
                RoomUiFixtures.Invoke(window, "RenderList", false); RoomUiFixtures.Pump(window);
                Check(Bubble(window, changedBody) == null && Bubble(window, mutable.Message) != null,
                    "In-place body metadata did not invalidate the rendered snapshot.");
                Check(!ReferenceEquals(replace, Bubble(window, mutable.Message)), "A changed bubble incorrectly retained stale content.");
                Check(ReferenceEquals(first, Bubble(window, firstBody)), "Replacing one message rebuilt another unchanged message.");
                Check(RoomUiFixtures.Texts(RoomUiFixtures.Find<StackPanel>(window, "MessageTimeline")).Contains("수정된 작성자"),
                    "In-place author metadata did not update.");

                TextBox own = Bubble(window, "재욱쌤이 또 무슨 실험을 하시는 건가요? 😂");
                items.Single(item => item.Id == "5").RoomTitle = "PC에 저장된 새 대화명";
                RoomUiFixtures.Invoke(window, "RenderList", false); RoomUiFixtures.Pump(window);
                Check(RoomUiFixtures.Find<TextBlock>(window, "SelectedRoomTitle").Text == "PC에 저장된 새 대화명", "In-place title changes were hidden by grouping cache.");
                Check(ReferenceEquals(own, Bubble(window, RoomUiFixtures.OriginalText(own))), "Room title metadata rebuilt unchanged message content.");

                NotificationPreview incoming = RoomUiFixtures.Message("append", "101", "PC에 저장된 새 대화명", "다른 작성자", "추가 수신", RoomUiFixtures.Anchor.AddMinutes(1), false);
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, incoming);
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(Bubble(window, incoming.Message) != null && ReferenceEquals(first, Bubble(window, firstBody)), "Appending rebuilt the retained message prefix.");
                ListBoxItem beforeOtherRoom = RoomUiFixtures.SelectRoom(window, "101");
                NotificationPreview other = RoomUiFixtures.Message("other-latest", "103", "중간고사 보강 문의", "김민수", "다른 방의 새 수신", RoomUiFixtures.Anchor.AddMinutes(2), false);
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, other);
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(ReferenceEquals(beforeOtherRoom, RoomUiFixtures.SelectRoom(window, "101")), "Another room's reorder rebuilt the unaffected selected row.");
                Check(ReferenceEquals(first, Bubble(window, firstBody)), "Another room's arrival rebuilt the selected timeline.");
                items.Remove(items.Single(item => item.Id == "1"));
                RoomUiFixtures.Invoke(window, "RenderList", false); RoomUiFixtures.Pump(window);
                Check(Bubble(window, firstBody) == null && ReferenceEquals(own, Bubble(window, RoomUiFixtures.OriginalText(own))), "Pruning did not remove only the vanished message.");
                Check(endpoint.Calls == 0, "Rendering/search unexpectedly called the explicit reply endpoint.");
            }
            finally { window.Close(); }
        }

        private static void FilterAndGeneration()
        {
            FakeReplySource endpoint = new FakeReplySource();
            MainWindow window = Window(endpoint);
            try
            {
                TextBox draft = RoomUiFixtures.Find<TextBox>(window, "ReplyInput");
                draft.Text = "현재 방 초안";
                TextBox first = Bubble(window, "아 이거 터졌다 ㅋㅋㅋ");
                CheckBox mine = RoomUiFixtures.Get<CheckBox>(window, "_hideMineCheckBox");
                mine.IsChecked = false; RoomUiFixtures.Pump(window);
                Check(Bubble(window, "아 진짜! 그 사진이 왜 터졌어? 😂") == null, "Own-message filter retained a hidden own bubble.");
                Check(ReferenceEquals(first, Bubble(window, RoomUiFixtures.OriginalText(first))), "Filtering own messages rebuilt unchanged other-author content.");
                mine.IsChecked = true; RoomUiFixtures.Pump(window);
                Check(Bubble(window, "아 진짜! 그 사진이 왜 터졌어? 😂") != null && draft.Text == "현재 방 초안", "Restoring own messages lost content or draft.");
                TextBox search = RoomUiFixtures.Find<TextBox>(window, "RoomSearchInput");
                search.Text = "예약된 이전 계정 검색";
                RoomUiFixtures.Invoke(window, "ResetDisplayedInbox", "next-account");
                RoomUiFixtures.Pump(window);
                Check(RoomUiFixtures.Find<ListBox>(window, "RoomList").Items.Count == 0 && draft.Text.Length == 0,
                    "An old search or cached visual repopulated reset account state.");
                NotificationPreview next = RoomUiFixtures.Message("next", "901", "다음 계정 방", "다음 작성자", "다음 계정 원문", DateTime.Now, false);
                next.AccountId = "next-account";
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, next);
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(Bubble(window, next.Message) != null && Bubble(window, RoomUiFixtures.OriginalText(first)) == null, "The new generation reused another account's timeline.");
                Check(endpoint.Calls == 0, "Account/filter rendering unexpectedly sent a reply.");
            }
            finally { window.Close(); }
        }

        private static void VisitedRoomReuse()
        {
            FakeReplySource endpoint = new FakeReplySource();
            MainWindow window = Window(endpoint);
            try
            {
                string body = "아 이거 터졌다 ㅋㅋㅋ";
                TextBox first = Bubble(window, body);
                RoomUiFixtures.SelectRoom(window, "102");
                RoomUiFixtures.SelectRoom(window, "101");
                Check(ReferenceEquals(first, Bubble(window, body)), "Returning to an unchanged visited room rebuilt its message control.");
                RoomUiFixtures.SelectRoom(window, "102");
                List<NotificationPreview> items = RoomUiFixtures.Get<List<NotificationPreview>>(window, "_items");
                NotificationPreview mutable = items.Single(item => item.Id == "1");
                mutable.Message = "보이지 않는 방의 수정 원문";
                RoomUiFixtures.Invoke(window, "RenderList", false); RoomUiFixtures.Pump(window);
                RoomUiFixtures.SelectRoom(window, "101");
                Check(Bubble(window, body) == null && Bubble(window, mutable.Message) != null && !ReferenceEquals(first, Bubble(window, mutable.Message)),
                    "A detached room projection hid updated retained data.");
                RoomUiFixtures.SelectRoom(window, "102");
                items.Remove(mutable);
                RoomUiFixtures.Invoke(window, "RenderList", false); RoomUiFixtures.Pump(window);
                RoomUiFixtures.SelectRoom(window, "101");
                Check(Bubble(window, mutable.Message) == null, "A detached room projection restored a removed retained message.");
                RoomUiFixtures.SelectRoom(window, "102");
                RoomUiFixtures.Invoke(window, "ResetDisplayedInbox", "next-account"); RoomUiFixtures.Pump(window);
                Check(((System.Collections.IDictionary)RoomUiFixtures.Get<object>(window, "_visitedConversations")).Count == 0,
                    "Account reset retained a detached room projection.");
                Check(endpoint.Calls == 0, "Visited room rendering unexpectedly sent a reply.");
            }
            finally { window.Close(); }
        }
    }
}
