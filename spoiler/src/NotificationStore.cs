using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;

namespace Spoiler
{
    public sealed class NotificationStore
    {
        private static readonly byte[] Entropy = Encoding.UTF8.GetBytes("Spoiler.LocalHistory.v1");
        private readonly string _historyPath;
        private readonly object _gate = new object();

        public NotificationStore()
            : this(null)
        {
        }

        public NotificationStore(string dataDirectory)
        {
            string overrideDirectory = Environment.GetEnvironmentVariable("SPOILER_DATA_DIR");
            if (string.IsNullOrWhiteSpace(dataDirectory)) dataDirectory = string.IsNullOrWhiteSpace(overrideDirectory)
                ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Spoiler")
                : overrideDirectory;

            _historyPath = Path.Combine(dataDirectory, "history.dat");
        }

        public IList<NotificationPreview> Load()
        {
            lock (_gate)
            {
            try
            {
                if (!File.Exists(_historyPath))
                {
                    return new List<NotificationPreview>();
                }

                byte[] protectedBytes = File.ReadAllBytes(_historyPath);
                byte[] json = ProtectedData.Unprotect(protectedBytes, Entropy, DataProtectionScope.CurrentUser);
                DataContractJsonSerializer serializer = new DataContractJsonSerializer(typeof(List<NotificationPreview>));
                using (MemoryStream stream = new MemoryStream(json))
                {
                    List<NotificationPreview> items = serializer.ReadObject(stream) as List<NotificationPreview>;
                    DateTime cutoff = DateTime.Now.AddDays(-7);
                    return (items ?? new List<NotificationPreview>())
                        .Where(item => item != null && item.ReceivedAt >= cutoff)
                        .OrderByDescending(item => item.ReceivedAt)
                        .Take(80)
                        .ToList();
                }
            }
            catch
            {
                return new List<NotificationPreview>();
            }
            }
        }

        public bool Save(IEnumerable<NotificationPreview> notifications)
        {
            lock (_gate)
            {
            string temporaryPath = _historyPath + ".tmp";
            byte[] json = null;
            try
            {
                string directory = Path.GetDirectoryName(_historyPath);
                if (!Directory.Exists(directory))
                {
                    Directory.CreateDirectory(directory);
                }

                DateTime cutoff = DateTime.Now.AddDays(-7);
                List<NotificationPreview> items = (notifications ?? Enumerable.Empty<NotificationPreview>())
                    .Where(item => item != null && item.ReceivedAt >= cutoff)
                    .OrderByDescending(item => item.ReceivedAt)
                    .ThenBy(item => item.Id ?? string.Empty, StringComparer.Ordinal)
                    .Take(80)
                    .ToList();

                DataContractJsonSerializer serializer = new DataContractJsonSerializer(typeof(List<NotificationPreview>));
                using (MemoryStream stream = new MemoryStream())
                {
                    serializer.WriteObject(stream, items);
                    json = stream.ToArray();
                }

                byte[] protectedBytes = ProtectedData.Protect(json, Entropy, DataProtectionScope.CurrentUser);
                using (FileStream stream = new FileStream(temporaryPath, FileMode.Create, FileAccess.Write, FileShare.None))
                {
                    stream.Write(protectedBytes, 0, protectedBytes.Length);
                    stream.Flush(true);
                }

                if (File.Exists(_historyPath))
                {
                    File.Replace(temporaryPath, _historyPath, null);
                }
                else
                {
                    File.Move(temporaryPath, _historyPath);
                }
                return true;
            }
            catch
            {
                return false;
            }
            finally
            {
                if (json != null) Array.Clear(json, 0, json.Length);
                try { if (File.Exists(temporaryPath)) File.Delete(temporaryPath); } catch { }
            }
            }
        }

        public bool Clear()
        {
            lock (_gate)
            {
            bool cleared = true;
            foreach (string path in new[] { _historyPath, _historyPath + ".tmp" })
            {
            try
            {
                if (File.Exists(path)) File.Delete(path);
                cleared = !File.Exists(path) && !Directory.Exists(path) && cleared;
            }
            catch
            {
                cleared = false;
            }
            }
            return cleared;
            }
        }
    }
}
