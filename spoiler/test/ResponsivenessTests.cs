using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Threading;

namespace Spoiler.Tests
{
    // Offline tests of this application's own controls. There is no account
    // source, OS input injection, desktop capture, or real send operation.
    internal static class ResponsivenessTests
    {
        private static int _checks;
        private static int _failures;

        [STAThread]
        public static int Main(string[] args)
        {
            // Ignore optional QA capture paths inherited from another task.
            // The wrapper owns the only logs and render output locations.
            Environment.SetEnvironmentVariable("SPOILER_CAPTURE_LOG", null);
            Environment.SetEnvironmentVariable("SPOILER_SCREENSHOT_PATH", null);
            Application application = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
            SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext());
            bool benchmarkOnly = args.Any(value => value == "-benchmark");
            int processBudgetMs = benchmarkOnly ? 90000 : 180000;
            // A dispatcher callback can block the UI polling loop. This
            // watchdog terminates only this isolated synthetic test process.
            Timer deadline = new Timer(delegate
            {
                Console.Error.WriteLine("RESPONSIVENESS_PROCESS_TIMEOUT_MS=" + processBudgetMs + "; own synthetic QA process terminated");
                Environment.Exit(124);
            }, null, processBudgetMs, Timeout.Infinite);
            try
            {
                if (!benchmarkOnly)
                {
                    Run("incremental room messages and metadata", IncrementalMessages);
                    Run("draft search scroll and filter continuity", InteractionContinuity);
                    Run("queued account and closed-window boundaries", QueuedBoundaries);
                    Run("composer busy and focus controls", ComposerStates);
                }
                Run("1000-message callback and settled-layout measurements", Benchmark);
                Console.WriteLine("RESPONSIVENESS_CHECKS=" + _checks + " FAILURES=" + _failures + " BENCHMARK_ONLY=" + benchmarkOnly);
                return _failures == 0 ? 0 : 1;
            }
            finally { deadline.Dispose(); application.Shutdown(); }
        }

        private static void Run(string name, Action scenario)
        {
            int before = _failures;
            try { scenario(); }
            catch (Exception error) { _failures++; Console.WriteLine("FAIL " + name + ": " + error); }
            Console.WriteLine((before == _failures ? "PASS " : "FAIL ") + name);
        }

        private static void Check(bool condition, string message)
        {
            _checks++;
            if (!condition) { _failures++; Console.WriteLine("FAIL: " + message); }
        }

        private static MainWindow Window(FakeReplySource source)
        {
            MainWindow window = new MainWindow(false, true, source);
            window.Width = 960; window.Height = 800;
            window.Show(); RoomUiFixtures.Pump(window);
            Check(RoomUiFixtures.Get<object>(window, "_source") == null, "Offline QA created an account receiver.");
            return window;
        }

        private static IList<NotificationPreview> Messages(int count, bool dense)
        {
            DateTime start = DateTime.Now.AddHours(-1);
            List<NotificationPreview> result = new List<NotificationPreview>();
            int denseCount = dense ? Math.Min(240, count - 120) : 0;
            for (int index = 0; index < count; index++)
            {
                string chat = dense && index < denseCount ? "101" : (2000 + (dense ? (index - denseCount) % 120 : index % 120)).ToString(CultureInfo.InvariantCulture);
                string title = chat == "101" ? "Synthetic dense room" : "Synthetic room " + chat;
                result.Add(RoomUiFixtures.Message("synthetic-" + index, chat, title, "Writer " + (index % 3),
                    "Synthetic body " + index + "\n확인할 내용과 수업 시간이 이 메시지에 있습니다.", start.AddSeconds(index), index % 4 == 0));
            }
            return result;
        }

        private static StackPanel Timeline(MainWindow window) { return RoomUiFixtures.Get<StackPanel>(window, "_listHost"); }
        private static List<TextBox> Bodies(MainWindow window) { return RoomUiFixtures.Children<TextBox>(Timeline(window)).ToList(); }
        private static List<NotificationPreview> Items(MainWindow window) { return RoomUiFixtures.Get<List<NotificationPreview>>(window, "_items"); }
        private static int RetainedInRoom(MainWindow window, string chat) { return Items(window).Count(item => item.ChatId == chat); }

        private static void Wait(MainWindow window, Func<bool> ready)
        {
            Stopwatch watch = Stopwatch.StartNew();
            while (watch.ElapsedMilliseconds < 2000)
            {
                RoomUiFixtures.Pump(window);
                // A synchronous WPF layout can itself exceed the polling
                // budget. Accept the actual completed state after that frame;
                // do not turn an already-rendered baseline into a timeout.
                if (ready()) return;
                Thread.Sleep(2);
            }
            throw new InvalidOperationException("Synthetic UI did not settle within two seconds. Visible bodies=" + Bodies(window).Count + ", retained=" + Items(window).Count);
        }

        private static void SelectNoPump(MainWindow window, string chat)
        {
            ListBox list = RoomUiFixtures.Find<ListBox>(window, "RoomList");
            ListBoxItem item = list.Items.Cast<object>().OfType<ListBoxItem>().FirstOrDefault(value => RoomUiFixtures.RoomValue(value, "ChatId") == chat);
            if (item == null) throw new InvalidOperationException("Synthetic room missing: " + chat);
            list.SelectedItem = item;
        }

        private static void Select(MainWindow window, string chat)
        {
            SelectNoPump(window, chat);
            Wait(window, delegate { return RoomReady(window, chat); });
        }

        // Benchmark readiness uses the known own-app timeline structure, not
        // a traversal of hundreds of TextBox templates. Check only the count,
        // date dividers and final message. Full-tree contract checks are kept
        // outside measured samples above.
        private static bool RoomReady(MainWindow window, string chat)
        {
            ListBoxItem selected = RoomUiFixtures.Find<ListBox>(window, "RoomList").SelectedItem as ListBoxItem;
            if (RoomUiFixtures.RoomValue(selected, "ChatId") != chat) return false;
            List<NotificationPreview> messages = Items(window).Where(item => item.ChatId == chat).OrderBy(item => item.ReceivedAt).ToList();
            StackPanel host = Timeline(window);
            if (host.Children.Count != messages.Count + messages.Select(item => item.ReceivedAt.Date).Distinct().Count()) return false;
            return messages.Count == 0 || RoomUiFixtures.Texts(host.Children[host.Children.Count - 1]).Contains(messages[messages.Count - 1].Message);
        }

        private static void Seed(MainWindow window, IList<NotificationPreview> messages)
        {
            Items(window).Clear(); Items(window).AddRange(messages);
            RoomUiFixtures.Invoke(window, "RenderList", false);
            Wait(window, delegate { return RoomUiFixtures.Find<ListBox>(window, "RoomList").Items.Count > 0; });
        }

        private static void Capture(MainWindow window, NotificationPreview message)
        {
            RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, message);
            RoomUiFixtures.Invoke(window, "FlushPendingUi");
        }

        private static void IncrementalMessages()
        {
            FakeReplySource source = new FakeReplySource();
            MainWindow window = Window(source);
            try
            {
                Seed(window, Messages(980, true)); Select(window, "101");
                Dictionary<string, TextBox> original = Bodies(window).ToDictionary(body => RoomUiFixtures.OriginalText(body));
                Capture(window, RoomUiFixtures.Message("other-new", "2000", "Synthetic room 2000", "Writer", "Another room append", DateTime.Now, false));
                Wait(window, delegate { return Bodies(window).Count == original.Count; });
                Check(Bodies(window).All(body => original.ContainsKey(RoomUiFixtures.OriginalText(body)) && object.ReferenceEquals(original[RoomUiFixtures.OriginalText(body)], body)),
                    "Another-room arrival rebuilt the selected conversation's message controls.");
                Capture(window, RoomUiFixtures.Message("same-new", "101", "Synthetic dense room", "Writer", "Same room append", DateTime.Now.AddMilliseconds(1), false));
                Wait(window, delegate { return Bodies(window).Count == original.Count + 1 && Bodies(window).Any(body => RoomUiFixtures.OriginalText(body) == "Same room append"); });
                Check(Bodies(window).Where(body => original.ContainsKey(RoomUiFixtures.OriginalText(body))).All(body => object.ReferenceEquals(original[RoomUiFixtures.OriginalText(body)], body)),
                    "Appending one message rebuilt the unchanged message prefix.");
                NotificationPreview target = Items(window).First(item => item.ChatId == "101" && !item.IsMine);
                Dictionary<string, TextBox> beforeUpgrade = Bodies(window).ToDictionary(body => RoomUiFixtures.OriginalText(body));
                NotificationPreview replacement = RoomUiFixtures.Message(target.Id, target.ChatId, target.RoomTitle, "Updated author", target.Message, target.ReceivedAt, target.IsMine);
                replacement.GroupKey = target.GroupKey;
                Capture(window, replacement);
                Wait(window, delegate { return RoomUiFixtures.Texts(Timeline(window)).Contains("Updated author"); });
                Check(Bodies(window).Count == beforeUpgrade.Count, "An exact metadata update duplicated a message.");
                Check(Bodies(window).Where(body => RoomUiFixtures.OriginalText(body) != target.Message).All(body => object.ReferenceEquals(beforeUpgrade[RoomUiFixtures.OriginalText(body)], body)),
                    "One metadata update rebuilt unrelated message controls.");
                Check(source.Calls == 0, "Reading or receiving invoked the fake reply transport.");
            }
            finally { window.Close(); }
        }

        private static void InteractionContinuity()
        {
            FakeReplySource source = new FakeReplySource();
            MainWindow window = Window(source);
            try
            {
                Seed(window, Messages(980, true)); Select(window, "101");
                TextBox input = RoomUiFixtures.Find<TextBox>(window, "ReplyInput");
                TextBox search = RoomUiFixtures.Find<TextBox>(window, "RoomSearchInput");
                ScrollViewer scroll = RoomUiFixtures.Find<ScrollViewer>(window, "ConversationScroll");
                input.Text = "첫 방의 작성 중인 답장";
                Select(window, "2000"); input.Text = "두 번째 방의 초안";
                Select(window, "101");
                Check(input.Text == "첫 방의 작성 중인 답장", "Room switching discarded the first draft.");
                search.Text = "No matching synthetic text";
                Wait(window, delegate { return RoomUiFixtures.Find<ListBox>(window, "RoomList").Items.Count == 0; });
                search.Clear();
                Wait(window, delegate { return RoomUiFixtures.Find<ListBox>(window, "RoomList").Items.Count == 121; });
                Select(window, "101");
                Check(input.Text == "첫 방의 작성 중인 답장", "Search clear discarded the retained room's draft.");
                scroll.ScrollToVerticalOffset(240); RoomUiFixtures.Pump(window);
                double offset = scroll.VerticalOffset;
                Capture(window, RoomUiFixtures.Message("position-other", "2000", "Synthetic room 2000", "Writer", "Other-room position", DateTime.Now, false));
                Wait(window, delegate { return Bodies(window).Count == RetainedInRoom(window, "101"); });
                Check(Math.Abs(scroll.VerticalOffset - offset) <= 1, "Another-room arrival changed the reading offset.");
                Capture(window, RoomUiFixtures.Message("position-same", "101", "Synthetic dense room", "Writer", "Same-room position", DateTime.Now.AddMilliseconds(1), false));
                Wait(window, delegate { return Bodies(window).Any(body => RoomUiFixtures.OriginalText(body) == "Same-room position"); });
                Check(Math.Abs(scroll.VerticalOffset - offset) <= 1, "Same-room append interrupted older-message reading.");
                scroll.ScrollToEnd(); RoomUiFixtures.Pump(window);
                Capture(window, RoomUiFixtures.Message("position-bottom", "101", "Synthetic dense room", "Writer", "Follow bottom", DateTime.Now.AddMilliseconds(2), false));
                Wait(window, delegate { return Bodies(window).Any(body => RoomUiFixtures.OriginalText(body) == "Follow bottom") && Math.Abs(scroll.VerticalOffset - scroll.ScrollableHeight) <= 1; });
                Check(Math.Abs(scroll.VerticalOffset - scroll.ScrollableHeight) <= 1, "Bottom-follow stopped after an append.");
                CheckBox includeMine = RoomUiFixtures.Get<CheckBox>(window, "_hideMineCheckBox");
                includeMine.IsChecked = false;
                Wait(window, delegate { return Bodies(window).Count == Items(window).Count(item => item.ChatId == "101" && !item.IsMine); });
                HashSet<string> ownBodies = new HashSet<string>(Items(window).Where(item => item.ChatId == "101" && item.IsMine).Select(item => item.Message));
                Check(!Bodies(window).Any(body => ownBodies.Contains(RoomUiFixtures.OriginalText(body))), "The mine filter retained an excluded own message.");
                includeMine.IsChecked = true;
                Wait(window, delegate { return Bodies(window).Count == RetainedInRoom(window, "101"); });
                search.Text = "No matching synthetic text";
                search.Text = "Synthetic room 2001";
                search.Text = "Synthetic room 2000";
                Wait(window, delegate
                {
                    ListBox list = RoomUiFixtures.Find<ListBox>(window, "RoomList");
                    return list.Items.Count == 1 && RoomUiFixtures.RoomValue(list.Items[0] as ListBoxItem, "ChatId") == "2000";
                });
                Check(RoomUiFixtures.Find<TextBlock>(window, "SelectedRoomTitle").Text == "Synthetic room 2000", "Rapid search committed an older query result.");
                search.Clear();
                Wait(window, delegate { return RoomUiFixtures.Find<ListBox>(window, "RoomList").Items.Count == 121; });
                Select(window, "101");
                Check(input.Text == "첫 방의 작성 중인 답장" && source.Calls == 0, "A live update changed the draft or sent a reply.");
            }
            finally { window.Close(); }
        }

        private static void QueuedBoundaries()
        {
            FakeReplySource source = new FakeReplySource();
            MainWindow window = Window(source);
            try
            {
                Seed(window, Messages(1000, false));
                RoomUiFixtures.Invoke(window, "ResetDisplayedInbox", "old-account");
                NotificationPreview old = RoomUiFixtures.Message("old-queued", "101", "Old", "Writer", "Old queued body", DateTime.Now, false);
                old.AccountId = "old-account";
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, old);
                RoomUiFixtures.Invoke(window, "ResetDisplayedInbox", "new-account");
                RoomUiFixtures.Invoke(window, "FlushPendingUi"); RoomUiFixtures.Pump(window);
                Check(Items(window).Count == 0 && Bodies(window).Count == 0, "Queued UI work crossed an account reset.");
                Check(RoomUiFixtures.Get<string>(window, "_selectedRoomKey") == null, "Account reset retained a selected room.");
                NotificationPreview pending = RoomUiFixtures.Message("closing-queued", "101", "New", "Writer", "Closed queued body", DateTime.Now, false);
                pending.AccountId = "new-account";
                RoomUiFixtures.Invoke(window, "OnPreviewCaptured", null, pending);
                window.Close();
                RoomUiFixtures.Invoke(window, "FlushPendingUi");
                Check(Items(window).Count == 0 && RoomUiFixtures.Get<bool>(window, "_exitRequested"), "A closed window committed queued message work.");
                Check(source.Calls == 0, "Account transition or close invoked a reply transport.");
            }
            finally { if (!RoomUiFixtures.Get<bool>(window, "_exitRequested")) window.Close(); }
        }

        private static void ComposerStates()
        {
            FakeReplySource source = new FakeReplySource();
            MainWindow window = Window(source);
            try
            {
                Seed(window, RoomUiFixtures.Messages()); Select(window, "101");
                TextBox input = RoomUiFixtures.Find<TextBox>(window, "ReplyInput");
                ButtonBase send = RoomUiFixtures.Find<ButtonBase>(window, "SendReplyButton");
                Check(input.AcceptsReturn && send.Focusable, "Composer newline or keyboard-focus behavior was lost.");
                Check(!send.IsEnabled, "An empty draft enabled sending.");
                input.Text = "Explicit fake reply"; RoomUiFixtures.Pump(window);
                Check(send.IsEnabled, "A valid draft did not enable sending.");
                send.Focus(); RoomUiFixtures.Pump(window);
                Check(send.IsKeyboardFocused || send.IsFocused, "The own-app send control did not accept focus.");
                send.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); RoomUiFixtures.Pump(window);
                Check(source.Calls == 1 && !send.IsEnabled && send.Content.ToString().Contains("전송 중"), "Pending send lacks a disabled busy state.");
                input.Text += " edited while pending";
                source.Complete(ReplySendStatus.Sent); RoomUiFixtures.WaitForReply(window);
                Check(input.Text.Contains("edited while pending"), "An old send completion erased a newer draft.");
                Check(source.Calls == 1, "Pending completion caused an extra fake send.");
            }
            finally { window.Close(); }
        }

        private sealed class Timing
        {
            public double Callback;
            public double Settled;
        }

        private static Timing Measure(MainWindow window, Action action, Func<bool> ready)
        {
            Stopwatch watch = Stopwatch.StartNew();
            action();
            double callback = watch.Elapsed.TotalMilliseconds;
            Wait(window, ready);
            return new Timing { Callback = callback, Settled = watch.Elapsed.TotalMilliseconds };
        }

        private static double Median(IEnumerable<double> values)
        {
            double[] ordered = values.OrderBy(value => value).ToArray();
            return ordered.Length % 2 == 0 ? (ordered[ordered.Length / 2 - 1] + ordered[ordered.Length / 2]) / 2 : ordered[ordered.Length / 2];
        }

        private static void Report(string name, IList<Timing> timings)
        {
            Console.WriteLine("MEASURE scenario=" + name + " samples=" + timings.Count
                + " callback_ms_median=" + Median(timings.Select(value => value.Callback)).ToString("F3", CultureInfo.InvariantCulture)
                + " callback_ms_max=" + timings.Max(value => value.Callback).ToString("F3", CultureInfo.InvariantCulture)
                + " settled_ms_median=" + Median(timings.Select(value => value.Settled)).ToString("F3", CultureInfo.InvariantCulture)
                + " settled_ms_max=" + timings.Max(value => value.Settled).ToString("F3", CultureInfo.InvariantCulture));
        }

        private static void Benchmark()
        {
            Console.WriteLine("BENCHMARK_NOTE=synthetic_own_WPF_callbacks_and_layout;no_real_transport;settled_includes_dispatcher_pump_layout_and_last_message_readiness_checks;async_poll_budget_ms=2000;dense_fixture_total=1000;dense_room=240;other_rooms=120;dense_switch_is_warm_revisit=true;cold_samples_use_fresh_windows");
            List<Timing> emptyWindows = new List<Timing>();
            List<Timing> firstLists = new List<Timing>();
            List<Timing> coldOpens = new List<Timing>();
            for (int index = 0; index < 5; index++)
            {
                FakeReplySource coldSource = new FakeReplySource();
                Stopwatch creation = Stopwatch.StartNew();
                MainWindow coldWindow = Window(coldSource);
                creation.Stop();
                emptyWindows.Add(new Timing { Callback = creation.Elapsed.TotalMilliseconds, Settled = creation.Elapsed.TotalMilliseconds });
                try
                {
                    IList<NotificationPreview> messages = Messages(1000, true);
                    firstLists.Add(Measure(coldWindow, delegate
                    {
                        Items(coldWindow).Clear(); Items(coldWindow).AddRange(messages);
                        RoomUiFixtures.Invoke(coldWindow, "RenderList", false);
                    }, delegate { return RoomUiFixtures.Find<ListBox>(coldWindow, "RoomList").Items.Count == 121; }));
                    Check(RoomUiFixtures.RoomValue(RoomUiFixtures.Find<ListBox>(coldWindow, "RoomList").SelectedItem as ListBoxItem, "ChatId") != "101",
                        "The cold fixture had already opened the dense room.");
                    coldOpens.Add(Measure(coldWindow, delegate { SelectNoPump(coldWindow, "101"); }, delegate { return RoomReady(coldWindow, "101"); }));
                    Check(coldSource.Calls == 0, "Cold first viewing invoked a reply transport.");
                }
                finally { coldWindow.Close(); }
            }
            Report("empty-window-create-show", emptyWindows);
            Report("initial-1000-message-room-list", firstLists);
            Report("cold-room-open", coldOpens);
            FakeReplySource source = new FakeReplySource();
            MainWindow window = Window(source);
            try
            {
                Seed(window, Messages(1000, true)); Select(window, "101");
                // Warm each scenario before recording medians; include the
                // total settled time so scheduled render work is not hidden.
                Select(window, "2000"); Select(window, "101");
                List<Timing> switches = new List<Timing>();
                for (int index = 0; index < 5; index++)
                {
                    Select(window, "2000");
                    switches.Add(Measure(window, delegate { SelectNoPump(window, "101"); }, delegate { return RoomReady(window, "101"); }));
                }
                Report("dense-room-switch", switches);
                List<Timing> otherAppends = new List<Timing>();
                List<Timing> sameAppends = new List<Timing>();
                for (int index = 0; index < 5; index++)
                {
                    int sample = index;
                    otherAppends.Add(Measure(window, delegate { Capture(window, RoomUiFixtures.Message("bench-other-" + sample, "2000", "Synthetic room 2000", "Writer", "Bench other " + sample, DateTime.Now.AddMilliseconds(sample), false)); },
                        delegate { return RoomReady(window, "101"); }));
                    sameAppends.Add(Measure(window, delegate { Capture(window, RoomUiFixtures.Message("bench-same-" + sample, "101", "Synthetic dense room", "Writer", "Bench same " + sample, DateTime.Now.AddMilliseconds(10 + sample), false)); },
                        delegate { return RoomReady(window, "101"); }));
                }
                Report("other-room-append-at-capacity", otherAppends); Report("same-room-append-at-capacity", sameAppends);
                Seed(window, Messages(1000, false)); Select(window, "2000");
                TextBox search = RoomUiFixtures.Find<TextBox>(window, "RoomSearchInput");
                search.Text = "Synthetic room 2000"; Wait(window, delegate { return RoomUiFixtures.Find<ListBox>(window, "RoomList").Items.Count == 1; });
                search.Clear(); Wait(window, delegate { return RoomUiFixtures.Find<ListBox>(window, "RoomList").Items.Count == 120; });
                List<Timing> searches = new List<Timing>(); List<Timing> clears = new List<Timing>();
                for (int index = 0; index < 5; index++)
                {
                    searches.Add(Measure(window, delegate { search.Text = "Synthetic room 2000"; }, delegate { return RoomUiFixtures.Find<ListBox>(window, "RoomList").Items.Count == 1; }));
                    clears.Add(Measure(window, delegate { search.Clear(); }, delegate { return RoomUiFixtures.Find<ListBox>(window, "RoomList").Items.Count == 120; }));
                }
                Report("120-room-search", searches); Report("120-room-search-clear", clears);
                Check(Items(window).Count == 1000 && source.Calls == 0, "The synthetic benchmark changed retention capacity or sent a reply.");
            }
            finally { window.Close(); }
        }
    }
}
