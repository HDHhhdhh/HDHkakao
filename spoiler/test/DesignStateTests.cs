using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;

namespace Spoiler.Tests
{
    // This helper renders only its own synthetic WPF controls. It does not
    // read credentials, production messages, or another application's UI.
    internal static class DesignStateTests
    {
        private static int _checks;
        private static int _failures;

        [STAThread]
        public static int Main()
        {
            Application application = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
            SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext());
            Run("frozen brush reuse and text contrast", TokensAndBrushes);
            Run("native button state feedback", ButtonStates);
            Run("readable room rows and composer bounds", RoomLayout);
            Console.WriteLine("DESIGN_STATE_CHECKS=" + _checks + " FAILURES=" + _failures);
            application.Shutdown();
            return _failures == 0 ? 0 : 1;
        }

        private static void Run(string name, Action action)
        {
            int before = _failures;
            try { action(); }
            catch (Exception exception) { _failures++; Console.WriteLine("FAIL " + name + ": " + exception); }
            Console.WriteLine((before == _failures ? "PASS " : "FAIL ") + name);
        }

        private static void Check(bool condition, string message)
        {
            _checks++;
            if (!condition) { _failures++; Console.WriteLine("FAIL: " + message); }
        }

        private static Type TokenType(string name)
        {
            Type tokens = typeof(MainWindow).Assembly.GetType("Spoiler.DesignTokens", true);
            return name == null ? tokens : tokens.GetNestedType(name, BindingFlags.NonPublic | BindingFlags.Public);
        }

        private static string ColorToken(string group, string name)
        {
            return (string)TokenType(group).GetField(name, BindingFlags.Static | BindingFlags.Public).GetRawConstantValue();
        }

        private static SolidColorBrush Brush(string color)
        {
            return (SolidColorBrush)TokenType(null).GetMethod("Brush", BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, new object[] { color });
        }

        private static double Component(double value)
        {
            return value <= .04045 ? value / 12.92 : Math.Pow((value + .055) / 1.055, 2.4);
        }

        private static double Luminance(Color color)
        {
            return .2126 * Component(color.R / 255.0) + .7152 * Component(color.G / 255.0) + .0722 * Component(color.B / 255.0);
        }

        private static double Contrast(string foreground, string background)
        {
            double first = Luminance(Brush(foreground).Color);
            double second = Luminance(Brush(background).Color);
            return (Math.Max(first, second) + .05) / (Math.Min(first, second) + .05);
        }

        private static void TokensAndBrushes()
        {
            string surface = ColorToken("Palette", "Surface");
            SolidColorBrush first = Brush(surface);
            Check(first.IsFrozen, "Reusable brushes must be frozen.");
            Check(object.ReferenceEquals(first, Brush(surface.ToLowerInvariant())), "Equal color requests must reuse the frozen brush.");
            Task<SolidColorBrush> otherThread = Task.Run(delegate { return Brush(surface); });
            Check(otherThread.Wait(2000) && object.ReferenceEquals(first, otherThread.Result), "Brush reuse must be safe on another thread.");
            foreach (string background in new[] { surface, ColorToken("Room", "Canvas"), ColorToken("Room", "Outgoing"), ColorToken("Room", "Selected") })
            {
                Check(Contrast(ColorToken("Palette", "Ink"), background) >= 4.5, "Body text must have 4.5:1 contrast on each conversation surface.");
                Check(Contrast(ColorToken("Palette", "InkMuted"), background) >= 4.5, "Metadata must have 4.5:1 contrast on each conversation surface.");
            }
            Check((double)TokenType("Room").GetField("Body").GetRawConstantValue() >= 14, "Conversation body text must be at least 14 DIP.");
            Check((double)TokenType("Room").GetField("Metadata").GetRawConstantValue() >= 12, "Room times and metadata must be at least 12 DIP.");
        }

        private static void ButtonStates()
        {
            Type type = typeof(MainWindow).Assembly.GetType("Spoiler.TactileButton", true);
            Button button = (Button)Activator.CreateInstance(type, true);
            Brush accent = Brush(ColorToken("Palette", "Accent"));
            Brush hover = Brush(ColorToken("Palette", "AccentHover"));
            button.Background = accent;
            button.Foreground = Brush(ColorToken("Palette", "InkStrong"));
            button.Content = "작업 실행";
            button.Padding = new Thickness(12, 0, 12, 0);
            button.BorderThickness = new Thickness(1);
            button.BorderBrush = Brush(ColorToken("Palette", "DividerStrong"));
            button.Width = 140; button.Height = 44;
            button.Template = (ControlTemplate)type.GetMethod("CreateRoundedTemplate", new[] { typeof(double), typeof(Brush) }).Invoke(null, new object[] { 10.0, hover });
            Window host = new Window { Width = 320, Height = 180, Content = new Grid { Children = { button } }, ShowInTaskbar = false };
            try
            {
                host.Show(); host.UpdateLayout(); button.ApplyTemplate();
                Border chrome = (Border)button.Template.FindName("Chrome", button);
                Border ring = (Border)button.Template.FindName("FocusRing", button);
                Check(chrome != null && ring != null, "Button must expose stable chrome and a separate focus ring.");
                Check(button.RenderTransform == Transform.Identity || button.RenderTransform.Value.IsIdentity, "The button must not use a scale animation.");
                int clicks = 0;
                button.Click += delegate { clicks++; };
                button.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
                Check(clicks == 1, "Native click semantics must remain available.");
                Trigger[] triggers = button.Template.Triggers.OfType<Trigger>().ToArray();
                foreach (DependencyProperty property in new[] { UIElement.IsMouseOverProperty, ButtonBase.IsPressedProperty, UIElement.IsEnabledProperty, UIElement.IsKeyboardFocusedProperty })
                    Check(triggers.Any(value => value.Property == property), "Missing explicit button state trigger: " + property.Name);
                Trigger hoverTrigger = triggers.First(value => value.Property == UIElement.IsMouseOverProperty);
                Check(hoverTrigger.Setters.OfType<Setter>().Any(value => value.TargetName == "Chrome" && value.Property == Border.BackgroundProperty),
                    "Hover must target template chrome so a locally assigned button background cannot hide it.");
                button.IsEnabled = false; host.UpdateLayout();
                Check(((SolidColorBrush)chrome.Background).Color == Brush(ColorToken("Palette", "SurfaceDisabled")).Color,
                    "Disabled color must be visibly distinct from enabled accent.");
                type.GetProperty("IsPending").SetValue(button, true, null); host.UpdateLayout();
                Check(((SolidColorBrush)chrome.Background).Color == Brush(ColorToken("Palette", "SurfacePending")).Color,
                    "A pending disabled action must show pending feedback instead of ordinary disabled feedback.");
                type.GetProperty("IsPending").SetValue(button, false, null);
                button.IsEnabled = true; host.Activate(); Keyboard.Focus(button); host.UpdateLayout();
                Check(button.IsKeyboardFocused && ring.Visibility == Visibility.Visible, "Keyboard focus must show its own visible ring.");
                Check(chrome.BorderThickness == new Thickness(1), "Focus must not change content measurement or shift the button.");
                Check(!button.RenderTransform.HasAnimatedProperties, "Button states must not run decorative animation clocks.");
            }
            finally { host.Close(); }
        }

        private static void RoomLayout()
        {
            foreach (Size size in new[] { new Size(760, 560), new Size(960, 800), new Size(1440, 900) })
            {
                FakeReplySource source = new FakeReplySource();
                MainWindow window = new MainWindow(false, true, source) { Width = size.Width, Height = size.Height };
                try
                {
                    window.Show();
                    RoomUiFixtures.Seed(window, RoomUiFixtures.Messages());
                    RoomUiFixtures.SelectRoom(window, "101");
                    window.UpdateLayout(); RoomUiFixtures.Pump(window);
                    ListBox rooms = RoomUiFixtures.Find<ListBox>(window, "RoomList");
                    ListBoxItem[] items = rooms.Items.OfType<ListBoxItem>().ToArray();
                    Check(items.Length >= 2 && object.ReferenceEquals(items[0].Template, items[1].Template), "Room rows must reuse a sealed selection template.");
                    Check(items[0].Template.IsSealed, "Room selection template must be immutable after construction.");
                    Check(items.SelectMany(value => Descendants(value).OfType<TextBlock>()).All(value => value.FontSize >= 12), "A room row must not use tiny time labels.");
                    TextBox input = RoomUiFixtures.Find<TextBox>(window, "ReplyInput");
                    Button send = RoomUiFixtures.Find<Button>(window, "SendReplyButton");
                    FrameworkElement surface = (FrameworkElement)window.Content;
                    Rect inputBounds = input.TransformToAncestor(surface).TransformBounds(new Rect(input.RenderSize));
                    Rect sendBounds = send.TransformToAncestor(surface).TransformBounds(new Rect(send.RenderSize));
                    Check(inputBounds.Bottom <= surface.ActualHeight + 1 && sendBounds.Bottom <= surface.ActualHeight + 1,
                        "Composer input and action must remain inside the window at " + size);
                    Check(inputBounds.Right <= sendBounds.Left + 1 && inputBounds.Width >= 240,
                        "Composer input and action must not overlap or collapse at " + size);
                    Check(send.ActualWidth >= 80 && send.ActualHeight >= 34, "Send action must retain a comfortable target size.");
                    Check(input.FontSize >= 14, "Composer must use readable conversation text.");
                }
                finally { window.Close(); }
            }
        }

        private static IEnumerable<DependencyObject> Descendants(DependencyObject parent)
        {
            int children = VisualTreeHelper.GetChildrenCount(parent);
            for (int index = 0; index < children; index++)
            {
                DependencyObject child = VisualTreeHelper.GetChild(parent, index);
                yield return child;
                foreach (DependencyObject descendant in Descendants(child)) yield return descendant;
            }
        }
    }
}
