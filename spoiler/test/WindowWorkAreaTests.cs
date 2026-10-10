using System;
using System.Runtime.InteropServices;

namespace Spoiler.Tests
{
    // Pure geometry only: this suite never creates a window or calls a monitor API.
    internal static class WindowWorkAreaTests
    {
        private static int _count;
        private static int _failures;

        public static int Main()
        {
            Run("bottom taskbar", delegate { Bounds(Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1032), 0, 0, 1920, 1032); });
            Run("top taskbar", delegate { Bounds(Rect(0, 0, 1920, 1080), Rect(0, 48, 1920, 1080), 0, 48, 1920, 1032); });
            Run("left taskbar", delegate { Bounds(Rect(0, 0, 1920, 1080), Rect(72, 0, 1920, 1080), 72, 0, 1848, 1080); });
            Run("right taskbar", delegate { Bounds(Rect(0, 0, 1920, 1080), Rect(0, 0, 1848, 1080), 0, 0, 1848, 1080); });
            Run("secondary monitor at negative X", delegate { Bounds(Rect(-1920, 0, 0, 1080), Rect(-1920, 0, 0, 1032), 0, 0, 1920, 1032); });
            Run("secondary monitor at negative Y", delegate { Bounds(Rect(0, -1440, 2560, 0), Rect(0, -1368, 2560, 0), 0, 72, 2560, 1368); });
            Run("secondary monitor with positive origin", delegate { Bounds(Rect(1920, 240, 4480, 1680), Rect(2000, 240, 4480, 1680), 80, 0, 2480, 1440); });
            Run("1920 physical pixels at 100 percent DPI", delegate { Bounds(Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), 0, 0, 1920, 1040); });
            Run("2560 physical pixels at 150 percent DPI", delegate { Bounds(Rect(-2560, -300, 0, 1140), Rect(-2560, -300, 0, 1068), 0, 0, 2560, 1368); });
            Run("3840 physical pixels at 200 percent DPI", delegate { Bounds(Rect(1920, 0, 5760, 2160), Rect(1920, 0, 5760, 2064), 0, 0, 3840, 2064); });
            Run("auto-hidden taskbar uses reported full work area", delegate { Bounds(Rect(-3840, -2160, 0, 0), Rect(-3840, -2160, 0, 0), 0, 0, 3840, 2160); });
            Run("only max position and size change", PreservesOtherFields);
            Run("monitor transition replaces previous bounds", MonitorTransition);
            Run("native data layouts are POINT 8 RECT 16 MINMAXINFO 40", NativeLayouts);
            Run("zero monitor width rejected", delegate { Reject(Rect(0, 0, 0, 1080), Rect(0, 0, 1, 100)); });
            Run("zero monitor height rejected", delegate { Reject(Rect(0, 0, 1920, 0), Rect(0, 0, 100, 1)); });
            Run("inverted monitor width rejected", delegate { Reject(Rect(100, 0, 0, 1080), Rect(0, 0, 100, 100)); });
            Run("inverted monitor height rejected", delegate { Reject(Rect(0, 100, 1920, 0), Rect(0, 0, 100, 100)); });
            Run("zero work width rejected", delegate { Reject(Rect(0, 0, 1920, 1080), Rect(10, 10, 10, 100)); });
            Run("zero work height rejected", delegate { Reject(Rect(0, 0, 1920, 1080), Rect(10, 10, 100, 10)); });
            Run("inverted work width rejected", delegate { Reject(Rect(0, 0, 1920, 1080), Rect(100, 10, 10, 100)); });
            Run("inverted work height rejected", delegate { Reject(Rect(0, 0, 1920, 1080), Rect(10, 100, 100, 10)); });
            Run("work outside left edge rejected", delegate { Reject(Rect(-1920, 0, 0, 1080), Rect(-1921, 0, 0, 1000)); });
            Run("work outside top edge rejected", delegate { Reject(Rect(0, -1440, 2560, 0), Rect(0, -1441, 2560, 0)); });
            Run("work outside right edge rejected", delegate { Reject(Rect(1920, 0, 4480, 1440), Rect(1920, 0, 4481, 1400)); });
            Run("work outside bottom edge rejected", delegate { Reject(Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1081)); });
            Run("all reported work margins applied", delegate { Bounds(Rect(-2560, -1440, 0, 0), Rect(-2480, -1380, -20, -40), 80, 60, 2460, 1340); });
            Run("small positive work area accepted", delegate { Bounds(Rect(-10, -10, 10, 10), Rect(-1, -1, 0, 0), 9, 9, 1, 1); });
            Console.WriteLine((_failures == 0 ? "PASS: " : "FAIL: ") + _count + " window work-area scenarios; failures=" + _failures);
            return _failures == 0 ? 0 : 1;
        }

        private static void Run(string name, Action test)
        {
            _count++;
            try { test(); Console.WriteLine("PASS: " + name); }
            catch (Exception error) { _failures++; Console.WriteLine("FAIL: " + name + ": " + error.Message); }
        }

        private static void Check(bool value, string message)
        {
            if (!value) throw new InvalidOperationException(message);
        }

        private static WindowWorkArea.PixelRect Rect(int left, int top, int right, int bottom)
        {
            return new WindowWorkArea.PixelRect(left, top, right, bottom);
        }

        private static WindowWorkArea.PixelPoint Point(int x, int y)
        {
            return new WindowWorkArea.PixelPoint { X = x, Y = y };
        }

        private static WindowWorkArea.MinMaxInfo Initial()
        {
            return new WindowWorkArea.MinMaxInfo {
                Reserved = Point(11, 12), MaxSize = Point(21, 22), MaxPosition = Point(31, 32),
                MinTrackSize = Point(41, 42), MaxTrackSize = Point(51, 52)
            };
        }

        private static bool Same(WindowWorkArea.PixelPoint first, WindowWorkArea.PixelPoint second)
        {
            return first.X == second.X && first.Y == second.Y;
        }

        private static void Bounds(WindowWorkArea.PixelRect monitor, WindowWorkArea.PixelRect work, int x, int y, int width, int height)
        {
            WindowWorkArea.MinMaxInfo info = Initial();
            WindowWorkArea.MinMaxInfo original = info;
            Check(WindowWorkArea.TryApplyMaximizedBounds(ref info, monitor, work), "Valid rectangles must be accepted.");
            Check(Same(info.MaxPosition, Point(x, y)), "Max position must use monitor-relative physical pixels.");
            Check(Same(info.MaxSize, Point(width, height)), "Max size must equal the work rectangle in physical pixels.");
            Check(Same(info.Reserved, original.Reserved), "Reserved fields changed.");
            Check(Same(info.MinTrackSize, original.MinTrackSize), "Minimum tracking size changed.");
            Check(Same(info.MaxTrackSize, original.MaxTrackSize), "Maximum tracking size changed.");
        }

        private static void Reject(WindowWorkArea.PixelRect monitor, WindowWorkArea.PixelRect work)
        {
            WindowWorkArea.MinMaxInfo info = Initial();
            WindowWorkArea.MinMaxInfo original = info;
            Check(!WindowWorkArea.TryApplyMaximizedBounds(ref info, monitor, work), "Invalid rectangles must be rejected.");
            Check(Same(info.Reserved, original.Reserved) && Same(info.MaxSize, original.MaxSize) &&
                Same(info.MaxPosition, original.MaxPosition) && Same(info.MinTrackSize, original.MinTrackSize) &&
                Same(info.MaxTrackSize, original.MaxTrackSize), "Rejected input changed MINMAXINFO.");
        }

        private static void PreservesOtherFields()
        {
            Bounds(Rect(-1920, -1080, 0, 0), Rect(-1860, -1040, 0, -30), 60, 40, 1860, 1010);
        }

        private static void MonitorTransition()
        {
            WindowWorkArea.MinMaxInfo info = Initial();
            Check(WindowWorkArea.TryApplyMaximizedBounds(ref info, Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1032)), "First monitor rejected.");
            Check(WindowWorkArea.TryApplyMaximizedBounds(ref info, Rect(-3840, -500, 0, 1660), Rect(-3744, -500, 0, 1660)), "Second monitor rejected.");
            Check(Same(info.MaxPosition, Point(96, 0)) && Same(info.MaxSize, Point(3744, 2160)), "Previous monitor bounds were retained.");
            Check(Same(info.MinTrackSize, Point(41, 42)) && Same(info.MaxTrackSize, Point(51, 52)), "Tracking sizes changed during monitor transition.");
        }

        private static void NativeLayouts()
        {
            Check(Marshal.SizeOf(typeof(WindowWorkArea.PixelPoint)) == 8, "POINT size differs from Win32 layout.");
            Check(Marshal.SizeOf(typeof(WindowWorkArea.PixelRect)) == 16, "RECT size differs from Win32 layout.");
            Check(Marshal.SizeOf(typeof(WindowWorkArea.MinMaxInfo)) == 40, "MINMAXINFO size differs from Win32 layout.");
            Check(Marshal.OffsetOf(typeof(WindowWorkArea.MinMaxInfo), "Reserved").ToInt32() == 0, "Reserved offset differs.");
            Check(Marshal.OffsetOf(typeof(WindowWorkArea.MinMaxInfo), "MaxSize").ToInt32() == 8, "MaxSize offset differs.");
            Check(Marshal.OffsetOf(typeof(WindowWorkArea.MinMaxInfo), "MaxPosition").ToInt32() == 16, "MaxPosition offset differs.");
            Check(Marshal.OffsetOf(typeof(WindowWorkArea.MinMaxInfo), "MinTrackSize").ToInt32() == 24, "MinTrackSize offset differs.");
            Check(Marshal.OffsetOf(typeof(WindowWorkArea.MinMaxInfo), "MaxTrackSize").ToInt32() == 32, "MaxTrackSize offset differs.");
        }
    }
}
