using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Windows;
using System.Windows.Automation.Peers;
using System.Windows.Automation.Provider;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;

namespace Spoiler.Tests
{
    // Synthetic controls and fake reply source only. No account or clipboard writes.
    internal static class KoreanWrappingTests
    {
        private static int _checks;
        private static void Check(bool value, string why)
        {
            _checks++;
            if (!value) throw new InvalidOperationException(why);
        }

        private static TextBox MessageBox(string raw)
        {
            Type type = typeof(MainWindow).Assembly.GetType("Spoiler.KoreanMessageTextBox", true);
            return (TextBox)Activator.CreateInstance(type, new object[] { raw });
        }

        private static string SelectedRaw(TextBox box)
        {
            return (string)box.GetType().GetMethod("OriginalSelectionText").Invoke(box, null);
        }

        private static void CheckCopy(TextBox box, string expected)
        {
            DataObject payload = new DataObject(DataFormats.UnicodeText, box.SelectedText);
            DataObjectCopyingEventArgs copying = new DataObjectCopyingEventArgs(payload, false);
            box.RaiseEvent(copying);
            Check((string)payload.GetData(DataFormats.UnicodeText) == expected, "Unicode copy differs from the raw selection.");
            Check((string)payload.GetData(DataFormats.Text) == expected, "Plain-text copy differs from the raw selection.");
            Check((string)payload.GetData(DataFormats.StringFormat) == expected, "String-format copy differs from the raw selection.");
        }

        [STAThread]
        public static int Main()
        {
            Application application = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
            try
            {
                StandaloneWrapping();
                RawCopyAndAccessibility();
                GuideUpdates();
                ConversationResizeAndDraft();
                Console.WriteLine("KOREAN_WRAPPING_CHECKS=" + _checks + " FAILURES=0");
                return 0;
            }
            catch (Exception exception) { Console.WriteLine("KOREAN_WRAPPING_FAILURE=" + exception); return 1; }
            finally { application.Shutdown(); }
        }

        private static void StandaloneWrapping()
        {
            TextBox box = MessageBox("우리 가족 맞춤형 미국여행 코스");
            box.FontFamily = new FontFamily("Malgun Gothic"); box.FontSize = 16;
            box.Width = 150; box.Padding = new Thickness(0); box.BorderThickness = new Thickness(0);
            Window host = new Window { Width = 210, Height = 250, Content = box, ShowInTaskbar = false };
            try
            {
                host.Show(); RoomUiFixtures.Pump(host);
                string[] lines = Enumerable.Range(0, box.LineCount).Select(i => box.GetLineText(i).Replace("\u2060", "").TrimEnd()).ToArray();
                Check(lines.Length == 2, "Fitting Korean words gained an unnecessary line.");
                Check(lines[0] == "우리 가족 맞춤형" && lines[1] == "미국여행 코스", "Korean syllables broke inside a fitting word.");
                string oversized = string.Concat(Enumerable.Repeat("개인정보처리방침", 12));
                TextBox longBox = MessageBox(oversized);
                longBox.FontFamily = box.FontFamily; longBox.FontSize = box.FontSize;
                longBox.Width = 150; longBox.Padding = new Thickness(0); longBox.BorderThickness = new Thickness(0);
                host.Content = longBox; RoomUiFixtures.Pump(host);
                Check(longBox.LineCount > 1, "An oversized Korean token did not break.");
                Check(longBox.ExtentWidth <= longBox.ViewportWidth + 1, "An oversized Korean token overflowed horizontally.");
                Check(RoomUiFixtures.OriginalText(longBox) == oversized, "Emergency wrapping changed the raw term.");
                TextBox url = MessageBox("https://example.test/" + new string('a', 240));
                url.Width = 150; url.Padding = new Thickness(0); url.BorderThickness = new Thickness(0);
                host.Content = url; RoomUiFixtures.Pump(host);
                Check(url.LineCount > 1 && url.ExtentWidth <= url.ViewportWidth + 1, "A long URL expanded horizontally.");
            }
            finally { host.Close(); }
        }

        private static void RawCopyAndAccessibility()
        {
            string[] samples = {
                "첫 문단  공백\r\n\r\n둘째 문단\n셋째 문단\r마지막  ",
                "가\u0301 👩🏽‍🏫 🇰🇷 e\u0301 가족여행",
                "기존\u2060원문과 TAB\t도 유지합니다.",
                "English without Korean formatting."
            };
            foreach (string raw in samples)
            {
                TextBox box = MessageBox(raw);
                Check(RoomUiFixtures.OriginalText(box) == raw, "The display changed the stored original.");
                box.SelectAll();
                Check(SelectedRaw(box) == raw, "Select-all changed spaces or hard breaks.");
                CheckCopy(box, raw);
                AutomationPeer peer = UIElementAutomationPeer.CreatePeerForElement(box);
                IValueProvider provider = (IValueProvider)peer.GetPattern(PatternInterface.Value);
                Check(provider.IsReadOnly && provider.Value == raw, "Accessibility value is not the read-only original.");
            }
            TextBox partial = MessageBox("첫 문장 가족여행 끝");
            string target = "가\u2060족\u2060여\u2060행";
            int start = partial.Text.IndexOf(target, StringComparison.Ordinal);
            Check(start >= 0, "The word protection is missing.");
            partial.Select(start, target.Length);
            Check(SelectedRaw(partial) == "가족여행", "Partial selection changed the source boundaries.");
            CheckCopy(partial, "가족여행");
        }

        private static void ConversationResizeAndDraft()
        {
            FakeReplySource endpoint = new FakeReplySource();
            MainWindow window = new MainWindow(false, true, endpoint);
            string raw = "우리 가족 맞춤형 미국여행 코스를 확인해 주세요.\n\n공백  두 개와 👩🏽‍🏫 이모지도 유지합니다.";
            NotificationPreview record = RoomUiFixtures.Message("korean", "101", "한국어 줄바꿈 검증", "테스트", raw, RoomUiFixtures.Anchor, false);
            try
            {
                window.Show(); RoomUiFixtures.Seed(window, new[] { record }); RoomUiFixtures.SelectRoom(window, "101");
                TextBox input = RoomUiFixtures.Find<TextBox>(window, "ReplyInput");
                string draft = "보강  안내\r\n다음 줄도 그대로 보내요. 👩🏽‍🏫";
                input.Text = draft;
                foreach (int width in new[] { 760, 960, 1440 })
                {
                    window.Width = width; window.Height = 800; RoomUiFixtures.Pump(window);
                    TextBox body = RoomUiFixtures.Children<TextBox>(RoomUiFixtures.Find<StackPanel>(window, "MessageTimeline")).Single();
                    Check(RoomUiFixtures.OriginalText(body) == raw && record.Message == raw, "Resize changed a received record.");
                    body.SelectAll(); CheckCopy(body, raw);
                    Check(body.ActualWidth <= body.ParentAsFrameworkElementWidth() + 1, "The message body exceeded its parent.");
                    Check(input.Text == draft && !input.Text.Contains("\u2060"), "Presentation formatting entered the editable draft.");
                    Check(RoomUiFixtures.Children<Button>(window).All(button => !(button.Content is string) || !((string)button.Content).Contains("\n")), "A button label gained a line break.");
                    Check(endpoint.Calls == 0, "Rendering unexpectedly sent a reply.");
                }
                ButtonBase send = RoomUiFixtures.Find<ButtonBase>(window, "SendReplyButton");
                send.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); RoomUiFixtures.Pump(window);
                Check(endpoint.Calls == 1 && endpoint.LastText == draft, "The fake reply received modified text.");
                endpoint.Complete(ReplySendStatus.Sent); RoomUiFixtures.WaitForReply(window);
            }
            finally { window.Close(); }
        }

        private static void GuideUpdates()
        {
            Type type = typeof(MainWindow).Assembly.GetType("Spoiler.KoreanGuideTextBlock", true);
            TextBlock guide = (TextBlock)Activator.CreateInstance(type, true);
            guide.Text = "예약 등록 전에 대상과 시간을 확인하세요.";
            string raw = guide.Text;
            guide.TextWrapping = TextWrapping.Wrap;
            Check(guide.Text.Contains("\u2060") && RoomUiFixtures.OriginalText(guide) == raw, "Guide wrapping changed the original.");
            guide.Text = "등록이 완료되었습니다. 다음 대상을 확인하세요.";
            Check(RoomUiFixtures.OriginalText(guide) == "등록이 완료되었습니다. 다음 대상을 확인하세요.", "Guide updates retained stale text.");
            guide.TextWrapping = TextWrapping.NoWrap;
            Check(guide.Text == RoomUiFixtures.OriginalText(guide) && !guide.Text.Contains("\u2060"), "Single-line guide text retained display joins.");
        }

        private static double ParentAsFrameworkElementWidth(this FrameworkElement element)
        {
            FrameworkElement parent = VisualTreeHelper.GetParent(element) as FrameworkElement;
            return parent == null ? element.ActualWidth : parent.ActualWidth;
        }
    }
}
