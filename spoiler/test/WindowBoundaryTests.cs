using System;
using System.Windows.Forms;

namespace Spoiler.Tests
{
    internal static class WindowBoundaryTests
    {
        [STAThread]
        public static int Main()
        {
            int failures = 0;
            using (Form root = new Form())
            using (Panel panel = new Panel())
            using (TextBox child = new TextBox())
            {
                root.ShowInTaskbar = false;
                root.Opacity = 0;
                root.StartPosition = FormStartPosition.Manual;
                root.Left = -32000;
                root.Top = -32000;
                root.Controls.Add(panel);
                panel.Controls.Add(child);
                root.Show();
                Application.DoEvents();

                IntPtr rootHandle = root.Handle;
                IntPtr childHandle = child.Handle;
                IntPtr normalizedChild = NotificationCaptureService.NormalizeToRootWindow(childHandle);
                IntPtr normalizedRoot = NotificationCaptureService.NormalizeToRootWindow(rootHandle);

                if (normalizedChild != rootHandle)
                {
                    Console.WriteLine(
                        "Child HWND did not normalize to its top-level root. root={0} child={1} normalized={2}",
                        rootHandle,
                        childHandle,
                        normalizedChild);
                    failures++;
                }

                if (normalizedRoot != rootHandle)
                {
                    Console.WriteLine("Top-level HWND did not remain unchanged.");
                    failures++;
                }
            }

            if (NotificationCaptureService.NormalizeToRootWindow(IntPtr.Zero) != IntPtr.Zero)
            {
                Console.WriteLine("Zero HWND was not rejected.");
                failures++;
            }

            Console.WriteLine(failures == 0 ? "PASS: window boundary tests" : "FAIL: " + failures + " window boundary tests");
            return failures == 0 ? 0 : 1;
        }
    }
}
