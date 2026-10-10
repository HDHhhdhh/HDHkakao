using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Windows;
using System.Windows.Media.Imaging;

namespace Spoiler
{
    internal static class AssetLoader
    {
        private static readonly object BitmapSync = new object();
        private static readonly Dictionary<string, BitmapSource> Bitmaps = new Dictionary<string, BitmapSource>(StringComparer.Ordinal);
        private static BitmapSource _iconBitmap;

        public static BitmapSource LoadBitmap(string resourceName)
        {
            if (string.IsNullOrEmpty(resourceName)) return null;
            lock (BitmapSync)
            {
                BitmapSource cached;
                if (Bitmaps.TryGetValue(resourceName, out cached)) return cached;
                BitmapSource loaded = DecodeBitmap(resourceName);
                if (loaded != null) Bitmaps.Add(resourceName, loaded);
                return loaded;
            }
        }

        private static BitmapSource DecodeBitmap(string resourceName)
        {
            try
            {
                Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(resourceName);
                if (stream == null)
                {
                    return null;
                }

                using (stream)
                {
                    BitmapImage bitmap = new BitmapImage();
                    bitmap.BeginInit();
                    bitmap.CacheOption = BitmapCacheOption.OnLoad;
                    bitmap.StreamSource = stream;
                    bitmap.EndInit();
                    bitmap.Freeze();
                    return bitmap;
                }
            }
            catch
            {
                return null;
            }
        }

        public static BitmapSource LoadIconBitmap()
        {
            lock (BitmapSync)
            {
                if (_iconBitmap != null) return _iconBitmap;
                BitmapSource source = LoadBitmap("Spoiler.Assets.Icon");
                if (source == null || source.PixelWidth < 860 || source.PixelHeight < 885)
                {
                    _iconBitmap = source;
                    return source;
                }

                CroppedBitmap crop = new CroppedBitmap(source, new Int32Rect(164, 154, 696, 731));
                crop.Freeze();
                _iconBitmap = crop;
                return crop;
            }
        }

        public static Icon LoadTrayIcon()
        {
            try
            {
                Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("Spoiler.Assets.TrayIcon");
                if (stream == null)
                {
                    return null;
                }

                using (stream)
                using (MemoryStream copy = new MemoryStream())
                {
                    stream.CopyTo(copy);
                    copy.Position = 0;
                    using (Icon loaded = new Icon(copy))
                    {
                        return (Icon)loaded.Clone();
                    }
                }
            }
            catch
            {
                return null;
            }
        }
    }
}
