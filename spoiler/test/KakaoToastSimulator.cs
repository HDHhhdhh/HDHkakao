using System;
using System.Drawing;
using System.Linq;
using System.Windows.Forms;

namespace Spoiler.Tests
{
    internal static class KakaoToastSimulator
    {
        [STAThread]
        public static void Main(string[] args)
        {
            Application.EnableVisualStyles();
            bool mainUiMode = args.Any(value => string.Equals(value, "--main-ui", StringComparison.OrdinalIgnoreCase));
            Form form = mainUiMode ? BuildMainUiFalsePositive() : BuildNotificationToast();

            Timer timer = new Timer();
            timer.Interval = 5000;
            timer.Tick += delegate { timer.Stop(); form.Close(); };
            form.Shown += delegate { timer.Start(); };
            Application.Run(form);
        }

        private static Form BuildNotificationToast()
        {
            Form form = new Form();
            form.Text = "Spoiler Kakao QA Toast";
            form.FormBorderStyle = FormBorderStyle.FixedToolWindow;
            form.StartPosition = FormStartPosition.Manual;
            form.TopMost = true;
            form.Width = 420;
            form.Height = 156;
            form.BackColor = Color.White;
            Rectangle area = Screen.PrimaryScreen.WorkingArea;
            form.Left = area.Right - form.Width - 24;
            form.Top = area.Bottom - form.Height - 24;

            Label app = MakeLabel("카카오톡", 16, FontStyle.Bold, 18);
            app.SetBounds(24, 15, 180, 26);
            Label sender = MakeLabel("민수", 15, FontStyle.Bold, 18);
            sender.SetBounds(24, 48, 180, 25);
            Label message = MakeLabel("오늘 회의 자료 보냈어요!", 14, FontStyle.Regular, 18);
            message.SetBounds(24, 78, 350, 30);
            form.Controls.Add(app);
            form.Controls.Add(sender);
            form.Controls.Add(message);

            return form;
        }

        private static Form BuildMainUiFalsePositive()
        {
            Form form = new Form();
            form.Text = "Spoiler Kakao QA Main UI";
            form.FormBorderStyle = FormBorderStyle.FixedToolWindow;
            form.StartPosition = FormStartPosition.Manual;
            form.Width = 400;
            form.Height = 436;
            form.Left = 120;
            form.Top = 120;

            Label editor = MakeLabel("편집", 14, FontStyle.Bold, 1);
            editor.SetBounds(20, 20, 120, 28);
            TextBox input = new TextBox();
            input.AccessibleName = "메시지 입력";
            input.Text = string.Empty;
            input.SetBounds(20, 56, 250, 32);

            Label ad = MakeLabel("MOMENT 광고", 14, FontStyle.Bold, 2);
            ad.SetBounds(20, 160, 180, 28);
            LinkLabel link = new LinkLabel();
            link.Text = "광고 링크";
            link.AccessibleName = "광고 링크";
            link.SetBounds(20, 196, 180, 28);

            form.Controls.Add(editor);
            form.Controls.Add(input);
            form.Controls.Add(ad);
            form.Controls.Add(link);
            return form;
        }

        private static Label MakeLabel(string text, float size, FontStyle style, int tabIndex)
        {
            Label label = new Label();
            label.Text = text;
            label.Font = new Font("Segoe UI", size, style, GraphicsUnit.Pixel);
            label.AutoSize = false;
            label.TabIndex = tabIndex;
            label.AccessibleName = text;
            return label;
        }
    }
}
