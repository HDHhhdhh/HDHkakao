using System;
using System.Runtime.InteropServices;

namespace Spoiler
{
    internal static class WindowWorkArea
    {
        internal const int GetMinMaxInfoMessage = 0x0024;
        private const uint MonitorDefaultToNearest = 2;

        [StructLayout(LayoutKind.Sequential)]
        internal struct PixelPoint
        {
            public int X;
            public int Y;
        }

        [StructLayout(LayoutKind.Sequential)]
        internal struct PixelRect
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;

            public PixelRect(int left, int top, int right, int bottom)
            {
                Left = left; Top = top; Right = right; Bottom = bottom;
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        internal struct MinMaxInfo
        {
            public PixelPoint Reserved;
            public PixelPoint MaxSize;
            public PixelPoint MaxPosition;
            public PixelPoint MinTrackSize;
            public PixelPoint MaxTrackSize;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct MonitorInfo
        {
            public int Size;
            public PixelRect Monitor;
            public PixelRect Work;
            public uint Flags;
        }

        internal static bool TryApplyMaximizedBounds(ref MinMaxInfo info, PixelRect monitor, PixelRect work)
        {
            long width = (long)work.Right - work.Left;
            long height = (long)work.Bottom - work.Top;
            if (monitor.Right <= monitor.Left || monitor.Bottom <= monitor.Top
                || width <= 0 || height <= 0 || width > int.MaxValue || height > int.MaxValue
                || work.Left < monitor.Left || work.Top < monitor.Top
                || work.Right > monitor.Right || work.Bottom > monitor.Bottom)
            {
                return false;
            }

            // Both rectangles use physical pixels. Do not scale again for
            // WPF DIPs; negative virtual-desktop origins cancel here.
            info.MaxPosition.X = work.Left - monitor.Left;
            info.MaxPosition.Y = work.Top - monitor.Top;
            info.MaxSize.X = (int)width;
            info.MaxSize.Y = (int)height;
            // Leave normal resizing, WPF minimum sizes and restore bounds
            // alone. In particular, do not cap MaxTrackSize to this monitor.
            return true;
        }

        internal static void UpdateMaximizedBounds(IntPtr window, IntPtr minMaxInfoPointer)
        {
            if (window == IntPtr.Zero || minMaxInfoPointer == IntPtr.Zero) return;
            IntPtr monitor = MonitorFromWindow(window, MonitorDefaultToNearest);
            if (monitor == IntPtr.Zero) return;
            MonitorInfo monitorInfo = new MonitorInfo();
            monitorInfo.Size = Marshal.SizeOf(typeof(MonitorInfo));
            if (!GetMonitorInfo(monitor, ref monitorInfo)) return;
            MinMaxInfo info = (MinMaxInfo)Marshal.PtrToStructure(minMaxInfoPointer, typeof(MinMaxInfo));
            if (TryApplyMaximizedBounds(ref info, monitorInfo.Monitor, monitorInfo.Work))
            {
                Marshal.StructureToPtr(info, minMaxInfoPointer, false);
            }
        }

        [DllImport("user32.dll")]
        private static extern IntPtr MonitorFromWindow(IntPtr window, uint flags);

        [DllImport("user32.dll", EntryPoint = "GetMonitorInfoW")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetMonitorInfo(IntPtr monitor, ref MonitorInfo info);
    }
}
