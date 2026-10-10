using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace Spoiler.Tests
{
    // Separate synthetic renderer: no credentials, account history, native
    // capture, external UI automation, or real reply transports are used.
    internal static class RoomUiPreview
    {
        [STAThread]
        public static int Main(string[] args)
        {
            if (args.Length < 3) return 2;
            string output = Path.GetFullPath(args[0]);
            double width = double.Parse(args[1], CultureInfo.InvariantCulture);
            double height = double.Parse(args[2], CultureInfo.InvariantCulture);
            string state = args.Length > 3 ? args[3].ToLowerInvariant() : "conversation";
            Application application = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
            SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext());
            FakeReplySource source = new FakeReplySource();
            MainWindow window = new MainWindow(false, true, source);
            window.Width = width; window.Height = height;
            try
            {
                window.Show();
                IList<NotificationPreview> fixtures = RoomUiFixtures.Messages();
                if (state == "korean")
                {
                    fixtures = new List<NotificationPreview> {
                        RoomUiFixtures.Message("k1", "101", "한국어 줄바꿈 검증", "테스트", "우리 가족 맞춤형 미국여행 코스를 확인해 주세요. 예약 등록 전에 대상과 시간을 확인하면 실수를 줄일 수 있습니다.", RoomUiFixtures.Anchor.AddMinutes(-2), false),
                        RoomUiFixtures.Message("k2", "101", "한국어 줄바꿈 검증", "나", "사용자가 입력한 줄바꿈은 그대로 유지합니다.\n\n공백  두 개와 👩🏽‍🏫 이모지도 보존합니다.", RoomUiFixtures.Anchor.AddMinutes(-1), true),
                        RoomUiFixtures.Message("k3", "101", "한국어 줄바꿈 검증", "테스트", "긴 용어와 주소는 화면 안에서 줄을 넘깁니다.\n개인정보처리방침동의안내문개인정보처리방침동의안내문개인정보처리방침동의안내문\nhttps://example.test/" + new string('a', 90), RoomUiFixtures.Anchor, false)
                    };
                }
                if (state == "long")
                {
                    fixtures.Add(RoomUiFixtures.Message("9", "101", "초강스 🏫 (초심을 찾는 강사 스토리)", "EUNMIN 🌸",
                        "긴 메시지도 원문 그대로 표시됩니다.\n" + string.Join("\n", Enumerable.Repeat("이번 주 보강 일정은 월요일부터 금요일까지입니다. 준비물과 수업 시간을 확인해 주세요.", 8)),
                        RoomUiFixtures.Anchor.AddMinutes(1), false));
                }
                if (state == "untitled")
                {
                    foreach (NotificationPreview item in fixtures.Where(value => value.ChatId == "101")) item.RoomTitle = null;
                }
                if (state == "empty") fixtures = new List<NotificationPreview>();
                RoomUiFixtures.Seed(window, fixtures);
                if (state != "empty") RoomUiFixtures.SelectRoom(window, "101");
                TextBox input = RoomUiFixtures.Find<TextBox>(window, "ReplyInput");
                ButtonBase send = RoomUiFixtures.Find<ButtonBase>(window, "SendReplyButton");
                if (state == "failed" || state == "unknown" || state == "pending")
                {
                    input.Text = "오늘 보강 수업은 오후 5시에 시작합니다.";
                    send.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
                    RoomUiFixtures.Pump(window);
                    if (state != "pending")
                    {
                        source.Complete(state == "failed" ? ReplySendStatus.Failed : ReplySendStatus.Unknown);
                        RoomUiFixtures.WaitForReply(window);
                    }
                }
                if (state == "legacy")
                {
                    ListBox rooms = RoomUiFixtures.Find<ListBox>(window, "RoomList");
                    rooms.SelectedItem = rooms.Items.Cast<object>().OfType<ListBoxItem>().First(item => RoomUiFixtures.RoomValue(item, "ChatId") == null);
                }
                if (state == "search") RoomUiFixtures.Find<TextBox>(window, "RoomSearchInput").Text = "결석";
                RoomUiFixtures.Pump(window);
                FrameworkElement surface = window.Content as FrameworkElement;
                if (surface == null || surface.ActualWidth <= 0 || surface.ActualHeight <= 0)
                    throw new InvalidOperationException("Own synthetic app content did not render.");
                string directory = Path.GetDirectoryName(output);
                if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
                RenderTargetBitmap bitmap = new RenderTargetBitmap((int)Math.Ceiling(surface.ActualWidth), (int)Math.Ceiling(surface.ActualHeight), 96, 96, PixelFormats.Pbgra32);
                bitmap.Render(surface);
                PngBitmapEncoder encoder = new PngBitmapEncoder();
                encoder.Frames.Add(BitmapFrame.Create(bitmap));
                using (FileStream stream = File.Create(output)) encoder.Save(stream);
                Console.WriteLine("SYNTHETIC_ROOM_PREVIEW=" + output);
                Console.WriteLine("SIZE=" + surface.ActualWidth.ToString(CultureInfo.InvariantCulture) + "x" + surface.ActualHeight.ToString(CultureInfo.InvariantCulture)
                    + " STATE=" + state + " FAKE_SENDS=" + source.Calls);
                return 0;
            }
            finally { window.Close(); application.Shutdown(); }
        }
    }
}
