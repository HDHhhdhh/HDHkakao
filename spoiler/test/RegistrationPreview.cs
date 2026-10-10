using System;
using System.IO;
using System.Reflection;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace Spoiler.Tests
{
    /// <summary>
    /// QA-only renderer for deidentified registration states. It is compiled
    /// separately and is never included in Spoiler.exe or the release folder.
    /// </summary>
    internal static class RegistrationPreview
    {
        [STAThread]
        public static int Main(string[] args)
        {
            if (args.Length < 2)
            {
                return 2;
            }

            string outputPath = Path.GetFullPath(args[0]);
            string state = (args[1] ?? string.Empty).Trim().ToLowerInvariant();
            string isolatedVault = Path.Combine(Path.GetTempPath(), "SpoilerRegistrationPreview", Guid.NewGuid().ToString("N"));

            Application app = new Application
            {
                ShutdownMode = ShutdownMode.OnMainWindowClose
            };
            KakaoRegistrationWindow window = new KakaoRegistrationWindow(
                new CredentialVault(isolatedVault),
                "node.exe",
                Path.Combine(isolatedVault, "not-started.mjs"));

            // Render the production margin as wrapper padding. This is layout-
            // equivalent in the window, while avoiding WPF's Visual offset
            // clipping when a margined root is rendered off-screen.
            FrameworkElement original = window.Content as FrameworkElement;
            if (original != null)
            {
                Thickness inset = original.Margin;
                original.Margin = new Thickness(0);
                window.Content = null;
                window.Content = new Border
                {
                    Padding = inset,
                    Background = new SolidColorBrush(Color.FromRgb(251, 251, 249)),
                    Child = original
                };
            }

            window.Loaded += delegate
            {
                LocoBridgeEvent bridgeEvent = PreviewEvent(state);
                if (bridgeEvent == null)
                {
                    return;
                }
                MethodInfo consume = typeof(KakaoRegistrationWindow).GetMethod(
                    "ConsumeBridgeEvent",
                    BindingFlags.Instance | BindingFlags.NonPublic);
                if (consume != null)
                {
                    if (state == "passcode")
                    {
                        MethodInfo setInputs = typeof(KakaoRegistrationWindow).GetMethod(
                            "SetRegistrationInputsEnabled",
                            BindingFlags.Instance | BindingFlags.NonPublic);
                        if (setInputs != null) setInputs.Invoke(window, new object[] { false });
                    }
                    consume.Invoke(window, new object[] { bridgeEvent });
                }
            };

            bool captureQueued = false;
            EventHandler captureHandler = null;
            captureHandler = delegate
            {
                if (captureQueued) return;
                captureQueued = true;
                window.ContentRendered -= captureHandler;
                window.Dispatcher.BeginInvoke(new Action(delegate
                {
                    try
                    {
                        string directory = Path.GetDirectoryName(outputPath);
                        if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
                        window.UpdateLayout();
                        FrameworkElement surface = window.Content as FrameworkElement;
                        if (surface == null) throw new InvalidOperationException("Registration content was not rendered.");
                        int width = Math.Max(1, (int)Math.Ceiling(surface.ActualWidth));
                        int height = Math.Max(1, (int)Math.Ceiling(surface.ActualHeight));
                        RenderTargetBitmap bitmap = new RenderTargetBitmap(width, height, 96, 96, PixelFormats.Pbgra32);
                        bitmap.Render(surface);
                        PngBitmapEncoder encoder = new PngBitmapEncoder();
                        encoder.Frames.Add(BitmapFrame.Create(bitmap));
                        using (FileStream stream = File.Create(outputPath))
                        {
                            encoder.Save(stream);
                        }
                    }
                    finally
                    {
                        window.Close();
                    }
                }), DispatcherPriority.ApplicationIdle);
            };
            window.ContentRendered += captureHandler;

            app.Run(window);
            return File.Exists(outputPath) ? 0 : 1;
        }

        private static LocoBridgeEvent PreviewEvent(string state)
        {
            string json;
            switch (state)
            {
                case "passcode":
                    json = "{\"type\":\"passcode\",\"passcode\":\"4821\"}";
                    break;
                case "slot":
                    json = "{\"type\":\"needs_force\"}";
                    break;
                case "error":
                    json = "{\"type\":\"error\",\"code\":\"bad_credentials\"}";
                    break;
                default:
                    return null;
            }

            LocoBridgeEvent bridgeEvent;
            return LocoMessageSource.TryParseBridgeLine(json, out bridgeEvent) ? bridgeEvent : null;
        }
    }
}
