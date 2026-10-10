using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Threading;

namespace Spoiler.Tests
{
    internal static class UiContractTests
    {
        private static int _failures;
        private static int _checks;

        [STAThread]
        public static int Main()
        {
            Application application = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
            SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext());
            Run("room selection and local reading", RoomSelection);
            Run("search and incoming selection", SearchAndIncoming);
            Run("confirmed room titles and unidentified archive", ConfirmedTitlesAndArchive);
            Run("confirmed room metadata upgrades replace exact messages", RoomMetadataUpgrade);
            Run("reading position during live updates", LiveReadingPosition);
            Run("composer safety and results", Composer);
            Run("room and account changes during reply", ReplyBoundaries);
            Run("retention account queue and trace privacy", ProductionRegressions);
            Run("reference compact and wide layout", LayoutSizes);
            Console.WriteLine("ROOM_UI_CHECKS=" + _checks + " FAILURES=" + _failures);
            application.Shutdown();
            return _failures == 0 ? 0 : 1;
        }

        private static void Run(string name, Action scenario)
        {
            int before = _failures;
            try { scenario(); }
            catch (Exception exception) { _failures++; Console.WriteLine("FAIL " + name + ": " + exception); }
            Console.WriteLine((before == _failures ? "PASS " : "FAIL ") + name);
        }

        private static MainWindow Window(FakeReplySource reply)
        {
            MainWindow window = new MainWindow(false, true, reply);
            window.Width = 860; window.Height = 800;
            window.Show();
            RoomUiFixtures.Seed(window, RoomUiFixtures.Messages());
            return window;
        }

        private static void Check(bool condition, string message)
        {
            _checks++;
            if (!condition) { _failures++; Console.WriteLine("FAIL: " + message); }
        }

        private static void RoomSelection()
        {
            FakeReplySource source = new FakeReplySource();
            MainWindow window = Window(source);
            try
            {
                ListBox rooms = RoomUiFixtures.Find<ListBox>(window, "RoomList");
                StackPanel timeline = RoomUiFixtures.Find<StackPanel>(window, "MessageTimeline");
                TextBox input = RoomUiFixtures.Find<TextBox>(window, "ReplyInput");
                ButtonBase send = RoomUiFixtures.Find<ButtonBase>(window, "SendReplyButton");
                Check(RoomUiFixtures.Get<object>(window, "_source") == null, "QA must not create the production account source.");
                Check(rooms != null && timeline != null && input != null && send != null, "Named room timeline and composer controls are missing.");
                if (rooms == null || timeline == null || input == null || send == null) return;
                Check(rooms.Items.Count == 4, "Authors in a single room were split or same-title rooms were merged.");
                Check(rooms.Items.Cast<object>().OfType<ListBoxItem>().Count(item => RoomUiFixtures.RoomValue(item, "Title") == "중간고사 보강 문의") == 2,
                    "Two independent rooms with identical titles must remain independently selectable.");
                RoomUiFixtures.SelectRoom(window, "101");
                IList<string> text = RoomUiFixtures.Texts(timeline);
                Check(text.Any(value => value == "산") && text.Any(value => value == "류재권") && text.Any(value => value == "park Joy"),
                    "A group room must show the different message authors together.");
                Check(text.IndexOf("아 이거 터졌다 ㅋㅋㅋ") < text.IndexOf("ㅋㅋㅋㅋ 재욱쌤 또 실험중이다")
                    && text.IndexOf("ㅋㅋㅋㅋ 재욱쌤 또 실험중이다") < text.IndexOf("재욱쌤이 또 무슨 실험을 하시는 건가요? 😂"),
                    "Room messages must run in chronological order.");
                Check(!text.Any(value => value.Contains("오늘 결석해서")), "Selecting one room exposed another room's messages.");
                Check(!text.Any(value => value.Contains("UNKNOWN_ROOM")), "Unconfirmed old records were guessed into a confirmed room by author or title.");
                Check(RoomUiFixtures.Get<CheckBox>(window, "_hideMineCheckBox").IsChecked == true,
                    "Own messages must be included by default with a checked inclusion control.");
                string state = RoomUiFixtures.OriginalText(RoomUiFixtures.Find<TextBlock>(window, "RoomStateText"));
                Check(state.Contains("시간순") && state.Contains("내") && state.Contains("상대"),
                    "The conversation header must explain chronological own-and-other message context.");
                Check(source.Calls == 0, "Selecting and reading rooms must not call the explicit reply sender.");
                IList<TextBox> bubbles = RoomUiFixtures.Children<TextBox>(timeline).ToList();
                Check(bubbles.Count == 5 && bubbles.All(bubble => bubble.IsReadOnly),
                    "Message bubbles must expose selectable read-only original text.");
                string longBody = "긴 원문\n" + new string('가', 1500) + "\n마지막 줄 😀";
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null,
                    RoomUiFixtures.Message("long-original", "101", "초강스 🏫 (초심을 찾는 강사 스토리)", "산", longBody, DateTime.Now, false));
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(RoomUiFixtures.Children<TextBox>(timeline).Any(bubble => bubble.IsReadOnly && RoomUiFixtures.OriginalText(bubble) == longBody),
                    "Long room text was truncated or changed instead of remaining selectable in full.");
                Check(!RoomUiFixtures.Texts(window).Any(value => value.Contains("AI 답장")), "Unimplemented AI reply controls must not appear.");
                Check(AutomationProperties.GetName(input).Length > 0 && AutomationProperties.GetName(send).Length > 0,
                    "Composer and explicit send need accessible labels.");
                ListBoxAutomationPeer peer = new ListBoxAutomationPeer(rooms);
                Check(peer.GetPattern(PatternInterface.Selection) != null, "The room list must expose native selection accessibility.");
                foreach (string name in new[] { "최소화", "최대화", "닫기" })
                    Check(RoomUiFixtures.Children<ButtonBase>(window).Any(button => AutomationProperties.GetName(button) == name),
                        "Window action is not accessible: " + name);

                ListBoxItem legacy = rooms.Items.Cast<object>().OfType<ListBoxItem>().First(item => RoomUiFixtures.RoomValue(item, "ChatId") == null);
                Check(RoomUiFixtures.RoomValue(legacy, "Title") == "방 미확인 이전 기록",
                    "Unconfirmed history must be explicitly identified as an archive, not a sender-based room.");
                rooms.SelectedItem = legacy; RoomUiFixtures.Pump(window);
                Check(RoomUiFixtures.Texts(timeline).Any(value => value.Contains("UNKNOWN_ROOM_SAME_AUTHOR"))
                    && RoomUiFixtures.Texts(timeline).Any(value => value.Contains("UNKNOWN_ROOM_INVALID_ID")),
                    "The separate archive lost unidentified old text instead of keeping it readable.");
                input.Text = "이 기록에는 보내면 안 됩니다.";
                Check(!send.IsEnabled, "Legacy history with no stable chat id must be read-only.");
                send.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); RoomUiFixtures.Pump(window);
                Check(source.Calls == 0, "A disabled legacy room send handler must not write.");
            }
            finally { window.Close(); }
        }

        private static void SearchAndIncoming()
        {
            FakeReplySource source = new FakeReplySource();
            MainWindow window = Window(source);
            try
            {
                ListBox rooms = RoomUiFixtures.Find<ListBox>(window, "RoomList");
                TextBox search = RoomUiFixtures.Find<TextBox>(window, "RoomSearchInput");
                TextBox input = RoomUiFixtures.Find<TextBox>(window, "ReplyInput");
                RoomUiFixtures.SelectRoom(window, "102");
                string selected = RoomUiFixtures.Get<string>(window, "_selectedRoomKey");
                input.Text = "102번 방에 남긴 초안";
                RoomUiFixtures.SelectRoom(window, "101");
                input.Text = "101번 방에 남긴 초안";
                RoomUiFixtures.SelectRoom(window, "102");
                Check(input.Text == "102번 방에 남긴 초안", "Switching rooms must preserve each room's draft.");
                NotificationPreview latest = RoomUiFixtures.Message("8", "103", "중간고사 보강 문의", "김민수", "새 메시지", DateTime.Now.AddMinutes(1), false);
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, latest);
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(RoomUiFixtures.Get<string>(window, "_selectedRoomKey") == selected,
                    "A newer message in another room stole the selected conversation.");
                Check(input.Text == "102번 방에 남긴 초안", "Incoming messages erased an unsent draft.");
                search.Text = "결석"; RoomUiFixtures.Pump(window);
                Check(rooms.Items.Count == 1 && RoomUiFixtures.RoomValue(rooms.Items.Cast<object>().OfType<ListBoxItem>().FirstOrDefault(), "ChatId") == "102",
                    "Room search must find message text without selecting unrelated rooms.");
                search.Text = "없는 검색어"; RoomUiFixtures.Pump(window);
                Check(rooms.Items.Count == 0, "An unmatched query did not show the empty search state.");
                search.Text = ""; RoomUiFixtures.Pump(window);
                Check(rooms.Items.Count == 4, "Clearing search did not restore all rooms.");
                Check(source.Calls == 0, "Search and incoming rendering must remain free of explicit writes.");

                RoomUiFixtures.SelectRoom(window, "101");
                CheckBox hideMine = RoomUiFixtures.Get<CheckBox>(window, "_hideMineCheckBox");
                hideMine.IsChecked = false; RoomUiFixtures.Pump(window);
                Check(!RoomUiFixtures.Texts(RoomUiFixtures.Find<StackPanel>(window, "MessageTimeline")).Contains("재욱쌤이 또 무슨 실험을 하시는 건가요? 😂"),
                    "Turning off own-message inclusion did not hide own bubbles.");
                hideMine.IsChecked = true; RoomUiFixtures.Pump(window);
                Check(RoomUiFixtures.Texts(RoomUiFixtures.Find<StackPanel>(window, "MessageTimeline")).Contains("재욱쌤이 또 무슨 실험을 하시는 건가요? 😂"),
                    "Turning on own-message inclusion did not restore own bubbles.");
                TextBox hour = RoomUiFixtures.Get<TextBox>(window, "_hourInput");
                TextBox minute = RoomUiFixtures.Get<TextBox>(window, "_minuteInput");
                hour.Text = "3"; minute.Text = "56";
                RoomUiFixtures.Invoke(window, "SetSearchPeriod", true);
                RoomUiFixtures.Invoke(window, "RunTimeSearch"); RoomUiFixtures.Pump(window);
                IList<NotificationPreview> exact = (IList<NotificationPreview>)RoomUiFixtures.Invoke(window, "GetVisibleItems");
                Check(exact.Count == 1 && exact[0].ReceivedAt.Hour == 15 && exact[0].ReceivedAt.Minute == 56,
                    "The existing time finder no longer applies hour AND minute together.");
                RoomUiFixtures.Invoke(window, "SetSearchPeriod", false);
                Check(((IList<NotificationPreview>)RoomUiFixtures.Invoke(window, "GetVisibleItems")).Count == 1,
                    "Editing AM/PM changed the previously submitted query without submission.");
                RoomUiFixtures.Invoke(window, "RunTimeSearch"); RoomUiFixtures.Pump(window);
                Check(((IList<NotificationPreview>)RoomUiFixtures.Invoke(window, "GetVisibleItems")).Count == 0,
                    "Submitting the changed AM period did not update the room filter.");
                RoomUiFixtures.Invoke(window, "ClearTimeSearch"); RoomUiFixtures.Pump(window);
                Check(rooms.Items.Count == 4, "Clearing the time search did not restore all room records.");
            }
            finally { window.Close(); }
        }

        private static void ConfirmedTitlesAndArchive()
        {
            MainWindow window = Window(new FakeReplySource());
            try
            {
                DateTime now = DateTime.Now;
                NotificationPreview first = RoomUiFixtures.Message("title-1", "501", null, "학생 A", "질문", now.AddMinutes(-3), false);
                NotificationPreview second = RoomUiFixtures.Message("title-2", "501", null, "학생 B", "보충 질문", now.AddMinutes(-2), false);
                NotificationPreview own = RoomUiFixtures.Message("title-3", "501", null, "나", "답변", now.AddMinutes(-1), true);
                NotificationPreview onlyOwn = RoomUiFixtures.Message("title-own", "502", null, "나", "다른 방 내 메시지", now, true);
                NotificationPreview unknown = new NotificationPreview { Id = "title-unknown", Sender = "학생 A", AuthorName = "학생 A",
                    RoomTitle = "학생 A", Message = "방 미확인 기록", ReceivedAt = now.AddMinutes(1) };
                RoomUiFixtures.Seed(window, new[] { first, second, own, onlyOwn, unknown });
                ListBox rooms = RoomUiFixtures.Find<ListBox>(window, "RoomList");
                RoomUiFixtures.SelectRoom(window, "501");
                TextBlock title = RoomUiFixtures.Find<TextBlock>(window, "SelectedRoomTitle");
                string before = title.Text;
                Check(before.Contains("학생 A") && before.Contains("학생 B") && !before.Contains("나"),
                    "A room with no verified title must describe observed other authors, not just the latest sender.");
                Check(RoomUiFixtures.Children<TextBox>(RoomUiFixtures.Find<StackPanel>(window, "MessageTimeline")).Count() == 3,
                    "Own and other messages in a verified room were split into different conversations.");
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null,
                    RoomUiFixtures.Message("title-4", "501", null, "학생 A", "새 질문", now.AddMinutes(2), false));
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(title.Text == before && RoomUiFixtures.RoomValue((ListBoxItem)rooms.SelectedItem, "ChatId") == "501",
                    "A changed latest author renamed or split an existing conversation.");
                RoomUiFixtures.SelectRoom(window, "502");
                Check(title.Text.Contains("대화방") && title.Text.Contains("502") && title.Text != "나",
                    "An own-only room without a verified title needs a neutral room-id fallback.");
                Check(rooms.Items.Cast<object>().OfType<ListBoxItem>().Count(item => RoomUiFixtures.RoomValue(item, "ChatId") == null) == 1,
                    "Unidentified history must have one clearly separate archive entry.");
            }
            finally { window.Close(); }
        }

        private static void RoomMetadataUpgrade()
        {
            MainWindow window = Window(new FakeReplySource());
            try
            {
                DateTime now = DateTime.Now;
                NotificationPreview known = RoomUiFixtures.Message("100", "601", null, "학생 A", "첫 메시지", now.AddMinutes(-3), false);
                NotificationPreview legacy = new NotificationPreview { Id = "101", Sender = "학생 A", AuthorName = "학생 A",
                    Message = "미확인 질문", GroupKey = "group:v1:metadata-exact-author", ReceivedAt = now.AddMinutes(-2) };
                NotificationPreview other = RoomUiFixtures.Message("101", "602", "별도 방", "학생 A", "다른 방 같은 로그 ID", now.AddMinutes(-1), false);
                RoomUiFixtures.Seed(window, new[] { known, legacy, other });
                RoomUiFixtures.SelectRoom(window, "601");
                TextBox input = RoomUiFixtures.Find<TextBox>(window, "ReplyInput");
                input.Text = "메타데이터 갱신 중 유지할 초안";
                string key = RoomUiFixtures.Get<string>(window, "_selectedRoomKey");
                NotificationPreview upgraded = RoomUiFixtures.Message("101", "601", "확인된 그룹 대화", "학생 A", "미확인 질문", legacy.ReceivedAt, false);
                upgraded.GroupKey = legacy.GroupKey;
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, upgraded);
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                List<NotificationPreview> items = RoomUiFixtures.Get<List<NotificationPreview>>(window, "_items");
                StackPanel timeline = RoomUiFixtures.Find<StackPanel>(window, "MessageTimeline");
                Check(items.Count == 3 && items.Count(item => item.ChatId == "601") == 2 && items.Count(item => item.ChatId == "602") == 1,
                    "An exact legacy metadata upgrade duplicated its original row or replaced a different room's log ID.");
                Check(RoomUiFixtures.Texts(timeline).Count(value => value == "미확인 질문") == 1
                    && !RoomUiFixtures.Texts(timeline).Contains("다른 방 같은 로그 ID"),
                    "Metadata upgrade duplicated its bubble or mixed another room into the selected timeline.");
                Check(RoomUiFixtures.Find<TextBlock>(window, "SelectedRoomTitle").Text == "확인된 그룹 대화",
                    "The confirmed room title was not reflected after exact metadata upgrade.");
                Check(RoomUiFixtures.Get<string>(window, "_selectedRoomKey") == key && input.Text == "메타데이터 갱신 중 유지할 초안",
                    "Room metadata upgrade moved the selected conversation or erased its draft.");
                NotificationPreview revised = RoomUiFixtures.Message("100", "601", "확인된 그룹 대화", "학생 A", "첫 메시지 최신 메타데이터", known.ReceivedAt, false);
                revised.GroupKey = "group:v1:changed-author-metadata";
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, revised);
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(items.Count == 3 && items.Count(item => item.ChatId == "601" && item.Id == "100") == 1
                    && RoomUiFixtures.Texts(timeline).Contains("첫 메시지 최신 메타데이터"),
                    "Verified room-and-log metadata changes must replace the row even after author metadata changes.");
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null,
                    RoomUiFixtures.Message("100", "602", "별도 방", "학생 A", "다른 방의 별도 메시지", now, false));
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(items.Count == 4 && items.Count(item => item.Id == "100") == 2
                    && RoomUiFixtures.Texts(timeline).Contains("첫 메시지 최신 메타데이터")
                    && !RoomUiFixtures.Texts(timeline).Contains("다른 방의 별도 메시지"),
                    "Identical log IDs in different verified rooms must remain separate during metadata updates.");
                NotificationPreview firstBatch = new NotificationPreview { Id = "104", Sender = "학생 A", AuthorName = "학생 A",
                    Message = "같은 배치의 정확한 메타데이터 갱신", GroupKey = "group:v1:batch-exact-author", ReceivedAt = now.AddMinutes(1) };
                NotificationPreview confirmedBatch = RoomUiFixtures.Message("104", "601", "확인된 그룹 대화", "학생 A",
                    firstBatch.Message, firstBatch.ReceivedAt, false);
                confirmedBatch.GroupKey = firstBatch.GroupKey;
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, firstBatch);
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, confirmedBatch);
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(items.Count == 5 && items.Count(item => item.Id == "104") == 1
                    && items.Single(item => item.Id == "104").ChatId == "601"
                    && RoomUiFixtures.Texts(timeline).Count(value => value == firstBatch.Message) == 1,
                    "A first capture and its exact metadata upgrade in one UI batch must become one confirmed-room bubble.");
            }
            finally { window.Close(); }
        }

        private static void LiveReadingPosition()
        {
            MainWindow window = Window(new FakeReplySource());
            try
            {
                DateTime now = DateTime.Now;
                List<NotificationPreview> messages = new List<NotificationPreview>();
                for (int index = 0; index < 40; index++)
                    messages.Add(RoomUiFixtures.Message("scroll-" + index, "101", "읽던 위치 확인", "학생",
                        "읽기용 메시지 " + index + "\n앞의 내용을 읽는 도중 새 메시지가 도착합니다.", now.AddSeconds(index - 60), false));
                RoomUiFixtures.Seed(window, messages);
                RoomUiFixtures.SelectRoom(window, "101");
                ScrollViewer scroll = RoomUiFixtures.Find<ScrollViewer>(window, "ConversationScroll");
                Check(scroll.ScrollableHeight > 250, "The live-reading fixture does not overflow the conversation viewport.");
                scroll.ScrollToVerticalOffset(200); RoomUiFixtures.Pump(window);
                double at = scroll.VerticalOffset;
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null,
                    RoomUiFixtures.Message("another-room", "102", "다른 방", "학생", "다른 방의 새 내용", now.AddSeconds(1), false));
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(Math.Abs(scroll.VerticalOffset - at) <= 1,
                    "A message in another room jumped the current reading position.");
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null,
                    RoomUiFixtures.Message("same-room-1", "101", "읽던 위치 확인", "학생", "같은 방의 새 내용", now.AddSeconds(2), false));
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(Math.Abs(scroll.VerticalOffset - at) <= 1,
                    "Reading older messages was interrupted by scrolling to the newest arrival.");
                scroll.ScrollToEnd(); RoomUiFixtures.Pump(window);
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null,
                    RoomUiFixtures.Message("same-room-2", "101", "읽던 위치 확인", "학생", "맨 아래에서 새 내용 받기", now.AddSeconds(3), false));
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(Math.Abs(scroll.VerticalOffset - scroll.ScrollableHeight) <= 1,
                    "The timeline stopped following new messages while already at the bottom.");
            }
            finally { window.Close(); }
        }

        private static void Composer()
        {
            FakeReplySource source = new FakeReplySource();
            MainWindow window = Window(source);
            try
            {
                RoomUiFixtures.SelectRoom(window, "101");
                TextBox input = RoomUiFixtures.Find<TextBox>(window, "ReplyInput");
                ButtonBase send = RoomUiFixtures.Find<ButtonBase>(window, "SendReplyButton");
                TextBlock feedback = RoomUiFixtures.Find<TextBlock>(window, "ReplyFeedback");
                int originalCount = RoomUiFixtures.Get<List<NotificationPreview>>(window, "_items").Count;
                input.Text = "   \n"; RoomUiFixtures.Pump(window);
                Check(!send.IsEnabled, "Whitespace-only replies must be disabled.");
                input.Text = "테스트 답장\n둘째 줄"; RoomUiFixtures.Pump(window);
                send.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); RoomUiFixtures.Pump(window);
                Check(source.Calls == 1 && source.LastChatId == "101" && source.LastText == "테스트 답장\n둘째 줄",
                    "Explicit send did not use the stable selected room and exact multiline body.");
                Check(!send.IsEnabled, "The pending send must prevent duplicate clicks.");
                send.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); RoomUiFixtures.Pump(window);
                Check(source.Calls == 1, "Repeated clicks issued a duplicate reply.");
                source.Complete(ReplySendStatus.Failed); RoomUiFixtures.WaitForReply(window);
                Check(input.Text == "테스트 답장\n둘째 줄" && feedback.Text.Length > 0, "A failed send erased its draft or omitted feedback.");
                Check(RoomUiFixtures.Get<List<NotificationPreview>>(window, "_items").Count == originalCount,
                    "A failed send was displayed as a delivered local message.");
                send.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); RoomUiFixtures.Pump(window);
                source.Complete(ReplySendStatus.Unknown); RoomUiFixtures.WaitForReply(window);
                Check(input.Text == "테스트 답장\n둘째 줄" && source.Calls == 2,
                    "An uncertain result must preserve the draft and never retry automatically.");
                send.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); RoomUiFixtures.Pump(window);
                source.Complete(ReplySendStatus.Sent); RoomUiFixtures.WaitForReply(window);
                Check(input.Text == string.Empty, "A confirmed send did not clear the unchanged submitted draft.");
                Check(RoomUiFixtures.Get<List<NotificationPreview>>(window, "_items").Count == originalCount,
                    "Reply result feedback must not append an optimistic duplicate; durable source events own the timeline.");
                NotificationPreview confirmed = RoomUiFixtures.Message("9001", "101", "초강스 🏫 (초심을 찾는 강사 스토리)", "나",
                    "테스트 답장\n둘째 줄", DateTime.Now, true);
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, confirmed);
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(RoomUiFixtures.Get<List<NotificationPreview>>(window, "_items").Count == originalCount + 1
                    && RoomUiFixtures.Texts(RoomUiFixtures.Find<StackPanel>(window, "MessageTimeline")).Contains("테스트 답장\n둘째 줄"),
                    "A durable confirmed own-message event did not appear in the selected conversation.");
                source.CanReply = false;
                input.Text = "연결이 없으면 보내지 않음"; RoomUiFixtures.Pump(window);
                Check(!send.IsEnabled, "A disconnected reply source must disable sending.");
                send.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); RoomUiFixtures.Pump(window);
                Check(source.Calls == 3, "A disconnected click invoked the sender.");
            }
            finally { window.Close(); }
        }

        private static void ReplyBoundaries()
        {
            FakeReplySource source = new FakeReplySource();
            MainWindow window = Window(source);
            try
            {
                TextBox input = RoomUiFixtures.Find<TextBox>(window, "ReplyInput");
                ButtonBase send = RoomUiFixtures.Find<ButtonBase>(window, "SendReplyButton");
                RoomUiFixtures.SelectRoom(window, "101"); input.Text = "첫 방에서 보내기";
                send.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); RoomUiFixtures.Pump(window);
                RoomUiFixtures.SelectRoom(window, "102"); input.Text = "다른 방의 초안";
                source.Complete(ReplySendStatus.Sent); RoomUiFixtures.WaitForReply(window);
                Check(input.Text == "다른 방의 초안", "A completed reply cleared another room's draft.");
                Check(RoomUiFixtures.RoomValue((ListBoxItem)RoomUiFixtures.Find<ListBox>(window, "RoomList").SelectedItem, "ChatId") == "102",
                    "A completed reply moved the selected room.");
                RoomUiFixtures.SelectRoom(window, "101");
                Check(input.Text == string.Empty, "The confirmed sent draft remained in its original room.");
                input.Text = "송신 중 이전 계정 초안";
                send.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); RoomUiFixtures.Pump(window);
                RoomUiFixtures.Invoke(window, "ResetDisplayedInbox", "account-new"); RoomUiFixtures.Pump(window);
                source.Complete(ReplySendStatus.Failed); RoomUiFixtures.WaitForReply(window);
                Check(input.Text == string.Empty && RoomUiFixtures.Get<List<NotificationPreview>>(window, "_items").Count == 0,
                    "An old account's reply completion repopulated the new account UI.");
                Check(RoomUiFixtures.Get<string>(window, "_selectedRoomKey") == null,
                    "Account changes must discard the previous room selection.");
                Check(!send.IsEnabled, "An empty new-account room must not be sendable.");
            }
            finally { window.Close(); }
        }

        private static void ProductionRegressions()
        {
            MainWindow window = new MainWindow(false, true);
            string tracePath = Path.Combine(Path.GetTempPath(), "spoiler-room-ui-redaction-" + Guid.NewGuid().ToString("N") + ".log");
            string oldTrace = Environment.GetEnvironmentVariable("SPOILER_CAPTURE_LOG");
            try
            {
                List<NotificationPreview> items = RoomUiFixtures.Get<List<NotificationPreview>>(window, "_items");
                Check(items.Count == 0 && RoomUiFixtures.Get<object>(window, "_source") == null,
                    "QA without demo data must not load actual credentials or stored history.");
                DateTime now = DateTime.Now;
                for (int index = 0; index < InboxStore.MaxMessages; index++)
                    items.Add(RoomUiFixtures.Message("new-" + index, "101", "Synthetic", "A", "Recent", now.AddSeconds(-index), false));
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null,
                    RoomUiFixtures.Message("old-recovery", "101", "Synthetic", "A", "Old", now.AddDays(-2), false));
                Check(items.Count == InboxStore.MaxMessages && items.All(item => item.Id != "old-recovery"), "Capture callback updated the UI collection before the batch flush.");
                RoomUiFixtures.Invoke(window, "FlushPendingUi");
                Check(items.Count == InboxStore.MaxMessages && items.Any(item => item.Id == "new-" + (InboxStore.MaxMessages - 1)) && items.All(item => item.Id != "old-recovery"),
                    "Recovered old messages evicted newer retained room messages.");
                RoomUiFixtures.Invoke(window, "ResetDisplayedInbox", "account-A");
                NotificationPreview first = RoomUiFixtures.Message("a-queued", "101", "A", "A", "A", now, false); first.AccountId = "account-A";
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, first);
                RoomUiFixtures.Invoke(window, "ResetDisplayedInbox", "account-B");
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, first);
                NotificationPreview second = RoomUiFixtures.Message("b-current", "101", "B", "B", "B", now, false); second.AccountId = "account-B";
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, second);
                RoomUiFixtures.Invoke(window, "FlushPendingUi");
                Check(items.Count == 1 && items[0].Id == "b-current", "Queued or late messages crossed account boundaries.");
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, second);
                Check((bool)RoomUiFixtures.Invoke(window, "ClearDisplayedHistory"), "Local clear failed.");
                RoomUiFixtures.Invoke(window, "FlushPendingUi");
                Check(items.Count == 0, "Pending messages repopulated the cleared history.");
                NotificationPreview expired = RoomUiFixtures.Message("expired", "101", "B", "B", "Expired", now.AddDays(-8), false); expired.AccountId = "account-B";
                items.Add(expired); RoomUiFixtures.Set(window, "_lastRetentionCheckUtc", DateTime.MinValue);
                RoomUiFixtures.Invoke(window, "FlushPendingUi");
                Check(items.Count == 0, "Long-running seven-day retention regressed.");
                Environment.SetEnvironmentVariable("SPOILER_CAPTURE_LOG", tracePath);
                Stopwatch watch = Stopwatch.StartNew();
                for (int index = 0; index < 80; index++)
                {
                    NotificationPreview message = RoomUiFixtures.Message("burst-" + index, "101", "SYNTHETIC_ROOM_SECRET", "SYNTHETIC_SENDER_SECRET",
                        "SYNTHETIC_BODY_SECRET_" + new string('x', 10000), now.AddMilliseconds(index), false);
                    message.AccountId = "account-B";
                    RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, message);
                }
                watch.Stop();
                Check(items.Count == 0, "Burst captures performed per-message collection updates.");
                RoomUiFixtures.Invoke(window, "FlushPendingUi");
                Check(items.Count == 80 && RoomUiFixtures.Get<bool>(window, "_visualsDirty"), "Hidden burst rendering was not deferred after retaining the batch.");
                string trace = File.Exists(tracePath) ? File.ReadAllText(tracePath) : string.Empty;
                Check(!trace.Contains("SYNTHETIC_BODY_SECRET") && !trace.Contains("SYNTHETIC_SENDER_SECRET") && !trace.Contains("SYNTHETIC_ROOM_SECRET"),
                    "QA traces disclosed message text, authors, or room titles.");
                Console.WriteLine("ROOM_UI_SYNTHETIC_BURST_ENQUEUE_MS=" + watch.ElapsedMilliseconds);
            }
            finally
            {
                Environment.SetEnvironmentVariable("SPOILER_CAPTURE_LOG", oldTrace);
                window.Close();
                if (File.Exists(tracePath)) File.Delete(tracePath);
            }
        }

        private static void LayoutSizes()
        {
            MainWindow window = Window(new FakeReplySource());
            try
            {
                foreach (Size size in new[] { new Size(860, 800), new Size(760, 560), new Size(1440, 900) })
                {
                    window.Width = size.Width; window.Height = size.Height; RoomUiFixtures.Pump(window);
                    ListBox rooms = RoomUiFixtures.Find<ListBox>(window, "RoomList");
                    TextBox input = RoomUiFixtures.Find<TextBox>(window, "ReplyInput");
                    ButtonBase send = RoomUiFixtures.Find<ButtonBase>(window, "SendReplyButton");
                    ScrollViewer conversation = RoomUiFixtures.Find<ScrollViewer>(window, "ConversationScroll");
                    Point at = input.TranslatePoint(new Point(0, 0), window);
                    Point sendAt = send.TranslatePoint(new Point(0, 0), window);
                    Check(Math.Abs(window.ActualWidth - size.Width) < 1 && Math.Abs(window.ActualHeight - size.Height) < 1,
                        "Window minimum exceeded the promised viewport: " + size);
                    Check(rooms.ActualWidth >= 200 && conversation.ActualWidth >= 350 && conversation.ActualHeight > 100,
                        "Room list or conversation became unusable at " + size);
                    Check(at.X >= 0 && at.Y >= 0 && at.X + input.ActualWidth <= window.ActualWidth + 1 && at.Y + input.ActualHeight <= window.ActualHeight + 1,
                        "Reply input escaped the visible window at " + size);
                    Check(sendAt.X >= 0 && sendAt.Y >= 0 && sendAt.X + send.ActualWidth <= window.ActualWidth + 1 && sendAt.Y + send.ActualHeight <= window.ActualHeight + 1,
                        "Explicit send escaped the visible window at " + size);
                }
            }
            finally { window.Close(); }
        }
    }
}
