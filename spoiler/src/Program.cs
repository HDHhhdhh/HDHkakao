using System;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Threading;

namespace Spoiler
{
    internal static class Program
    {
        private const string MutexName = "Local\\Spoiler.MessagePreview.8A5F08FC";
        private static bool _diagnosticsEnabled;

        [STAThread]
        public static void Main(string[] args)
        {
            _diagnosticsEnabled = args.Any(value => value == "--qa" || value == "--demo" || value == "--runtime-self-test");
            WriteTrace("entry");
            if (args.Any(value => string.Equals(value, "--runtime-self-test", StringComparison.OrdinalIgnoreCase)))
            {
                RunRuntimeSelfTest(args);
                return;
            }

            bool demoMode = args.Any(value => string.Equals(value, "--demo", StringComparison.OrdinalIgnoreCase));
            bool qaMode = args.Any(value => string.Equals(value, "--qa", StringComparison.OrdinalIgnoreCase));
            bool qaAutoExit = args.Any(value => string.Equals(value, "--qa-auto-exit", StringComparison.OrdinalIgnoreCase));

            bool createdNew;
            using (Mutex singleInstance = new Mutex(true, MutexName + (demoMode ? ".Demo" : string.Empty), out createdNew))
            {
                WriteTrace("mutex=" + createdNew);
                if (!createdNew)
                {
                    IntPtr existingWindow = FindWindow(null, "Spoiler");
                    if (existingWindow != IntPtr.Zero)
                    {
                        ShowWindow(existingWindow, 9);
                        SetForegroundWindow(existingWindow);
                    }
                    return;
                }

                SetProcessDpiAwarenessContext(new IntPtr(-4));
                WriteTrace("dpi-ready");

                Application application = new Application();
                // Closing the window hides Spoiler to the notification area.
                // Only an explicit tray exit (or a system session ending) may
                // terminate the WPF dispatcher.
                application.ShutdownMode = ShutdownMode.OnExplicitShutdown;
                application.DispatcherUnhandledException += OnUnhandledException;
                WriteTrace("application-ready");

                try
                {
                    if (!demoMode && !qaMode)
                    {
                        WriteTrace("embedded-runtime-preparing");
                        EmbeddedRuntime.EnsureReady();
                        WriteTrace("embedded-runtime-ready");
                    }

                    WriteTrace("window-constructing");
                    MainWindow window = new MainWindow(demoMode, qaMode);
                    window.SourceInitialized += delegate { WriteTrace("event-source-initialized"); };
                    window.Loaded += delegate { WriteTrace("event-loaded"); };
                    window.ContentRendered += delegate { WriteTrace("event-content-rendered"); };
                    window.IsVisibleChanged += delegate { WriteTrace("event-visible=" + window.IsVisible); };
                    window.Closing += delegate { WriteTrace("event-closing"); };
                    window.Closed += delegate { WriteTrace("event-closed"); };
                    WriteTrace("window-constructed");
                    application.MainWindow = window;
                    window.Show();
                    IntPtr shownHandle = new WindowInteropHelper(window).Handle;
                    WriteTrace("window-shown handle=" + shownHandle + " visible=" + window.IsVisible + " state=" + window.WindowState);

                    DispatcherTimer qaExitTimer = null;
                    if (qaMode && qaAutoExit)
                    {
                        qaExitTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(7) };
                        qaExitTimer.Tick += delegate
                        {
                            qaExitTimer.Stop();
                            // QA uses a demo window without a tray host.  Keep
                            // the explicit-shutdown contract while still
                            // allowing the harness to terminate deterministically.
                            window.RequestExit();
                            application.Shutdown();
                        };
                        qaExitTimer.Start();
                    }

                    WriteTrace("run-enter");
                    application.Run();
                    WriteTrace("run-exit");
                    GC.KeepAlive(qaExitTimer);
                }
                catch (Exception exception)
                {
                    WriteDiagnostic(exception);
                    MessageBox.Show(
                        "Spoiler를 시작하지 못했습니다.\n\n" +
                        "단일 실행 파일 안의 구성 요소를 안전하게 준비하지 못했어요. " +
                        "별도 설치나 보조 폴더는 필요하지 않습니다. Spoiler를 모두 종료한 뒤 다시 실행해 주세요.\n\n" +
                        "오류 종류: " + exception.GetType().Name,
                        "Spoiler",
                        MessageBoxButton.OK,
                        MessageBoxImage.Information);
                }

                GC.KeepAlive(singleInstance);
            }
        }

        private static void RunRuntimeSelfTest(string[] args)
        {
            string reportPath = GetArgumentValue(args, "--runtime-self-test-report=");
            string runtimeRoot = GetArgumentValue(args, "--runtime-self-test-root=");
            try
            {
                string report = string.IsNullOrWhiteSpace(runtimeRoot)
                    ? EmbeddedRuntime.CreateSelfTestReport()
                    : EmbeddedRuntime.CreateSelfTestReport(Path.GetFullPath(runtimeRoot));
                WriteSelfTestReport(reportPath, report);
                Environment.ExitCode = 0;
            }
            catch (Exception exception)
            {
                WriteDiagnostic(exception);
                WriteSelfTestReport(
                    reportPath,
                    "FAIL: embedded runtime self-test" + Environment.NewLine + exception + Environment.NewLine);
                Environment.ExitCode = 2;
            }
        }

        private static string GetArgumentValue(string[] args, string prefix)
        {
            foreach (string argument in args ?? new string[0])
            {
                if (argument != null && argument.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                {
                    return argument.Substring(prefix.Length).Trim('"');
                }
            }
            return null;
        }

        private static void WriteSelfTestReport(string reportPath, string report)
        {
            if (string.IsNullOrWhiteSpace(reportPath))
            {
                return;
            }

            string fullPath = Path.GetFullPath(reportPath);
            string directory = Path.GetDirectoryName(fullPath);
            if (!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }
            File.WriteAllText(fullPath, report ?? string.Empty, new System.Text.UTF8Encoding(false));
        }

        private static void OnUnhandledException(object sender, DispatcherUnhandledExceptionEventArgs args)
        {
            WriteDiagnostic(args.Exception);
            MessageBox.Show(
                "Spoiler가 잠시 멈췄습니다. 다시 실행하면 메시지 수신을 이어갈 수 있어요.\n\n오류 종류: " + args.Exception.GetType().Name,
                "Spoiler",
                MessageBoxButton.OK,
                MessageBoxImage.Information);
            args.Handled = true;
        }

        private static void WriteDiagnostic(Exception exception)
        {
            try
            {
                string requestedPath = _diagnosticsEnabled ? Environment.GetEnvironmentVariable("SPOILER_DIAGNOSTIC_LOG") : null;
                string logPath = string.IsNullOrWhiteSpace(requestedPath)
                    ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Spoiler", "error.log")
                    : requestedPath;
                string directory = Path.GetDirectoryName(logPath);
                if (!string.IsNullOrEmpty(directory))
                {
                    Directory.CreateDirectory(directory);
                }
                File.AppendAllText(logPath, DateTime.Now.ToString("O") + " " + exception.GetType().Name + Environment.NewLine);
            }
            catch
            {
                // Diagnostics must never replace the original error.
            }
        }

        private static void WriteTrace(string message)
        {
            if (!_diagnosticsEnabled) return;
            try
            {
                string logPath = Environment.GetEnvironmentVariable("SPOILER_DIAGNOSTIC_LOG");
                if (string.IsNullOrWhiteSpace(logPath))
                {
                    return;
                }
                string directory = Path.GetDirectoryName(logPath);
                if (!string.IsNullOrEmpty(directory))
                {
                    Directory.CreateDirectory(directory);
                }
                File.AppendAllText(logPath, DateTime.Now.ToString("O") + "  " + message + Environment.NewLine);
            }
            catch
            {
            }
        }

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetProcessDpiAwarenessContext(IntPtr value);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr FindWindow(string className, string windowName);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool ShowWindow(IntPtr windowHandle, int command);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetForegroundWindow(IntPtr windowHandle);
    }
}
