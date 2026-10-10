using System;
using System.IO;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;

namespace Spoiler
{
    /// <summary>
    /// The non-password portion of a KakaoTalk tablet companion session.
    ///
    /// The names intentionally mirror agent-messenger's KakaoTalk account
    /// format.  There is no password member in this DTO, which makes it
    /// impossible for the vault serializer to persist the login password by
    /// accident.
    /// </summary>
    [DataContract]
    public sealed class LocoCredentials
    {
        [DataMember(Name = "oauth_token", Order = 1, EmitDefaultValue = false)]
        public string OAuthToken { get; set; }

        [DataMember(Name = "access_token", Order = 2, EmitDefaultValue = false)]
        public string AccessToken { get; set; }

        [DataMember(Name = "refresh_token", Order = 3, EmitDefaultValue = false)]
        public string RefreshToken { get; set; }

        [DataMember(Name = "user_id", Order = 4, EmitDefaultValue = false)]
        public string UserId { get; set; }

        [DataMember(Name = "device_uuid", Order = 5, EmitDefaultValue = false)]
        public string DeviceUuid { get; set; }

        [DataMember(Name = "device_type", Order = 6, EmitDefaultValue = false)]
        public string DeviceType { get; set; }

        [DataMember(Name = "account_id", Order = 7, EmitDefaultValue = false)]
        public string AccountId { get; set; }

        [DataMember(Name = "auth_method", Order = 8, EmitDefaultValue = false)]
        public string AuthMethod { get; set; }

        [DataMember(Name = "created_at", Order = 9, EmitDefaultValue = false)]
        public string CreatedAt { get; set; }

        [DataMember(Name = "updated_at", Order = 10, EmitDefaultValue = false)]
        public string UpdatedAt { get; set; }

        public bool HasSessionToken
        {
            get
            {
                return !string.IsNullOrWhiteSpace(OAuthToken)
                    || !string.IsNullOrWhiteSpace(AccessToken);
            }
        }

        public bool IsUsable
        {
            get
            {
                return HasSessionToken
                    && !string.IsNullOrWhiteSpace(UserId)
                    && string.Equals(DeviceType, CredentialVault.TabletDeviceType, StringComparison.OrdinalIgnoreCase);
            }
        }

        public LocoCredentials Clone()
        {
            return new LocoCredentials
            {
                OAuthToken = OAuthToken,
                AccessToken = AccessToken,
                RefreshToken = RefreshToken,
                UserId = UserId,
                DeviceUuid = DeviceUuid,
                DeviceType = DeviceType,
                AccountId = AccountId,
                AuthMethod = AuthMethod,
                CreatedAt = CreatedAt,
                UpdatedAt = UpdatedAt
            };
        }
    }

    public sealed class CredentialSnapshot
    {
        public LocoCredentials Credentials { get; private set; }
        public string Revision { get; private set; }

        internal CredentialSnapshot(LocoCredentials credentials, string revision)
        {
            Credentials = credentials;
            Revision = revision;
        }
    }

    public sealed class CredentialVaultConflictException : InvalidOperationException
    {
        public CredentialVaultConflictException()
            : base("The credential vault changed during authentication.") { }
    }

    /// <summary>
    /// Stores the tablet companion token bundle as CurrentUser DPAPI data.
    /// The default path is always below %LOCALAPPDATA%\Spoiler.  A directory
    /// constructor is provided solely so tests can use an isolated temporary
    /// directory without touching the user's real vault.
    /// </summary>
    public sealed class CredentialVault
    {
        internal const string TabletDeviceType = "tablet";

        private static readonly byte[] Entropy = Encoding.UTF8.GetBytes("Spoiler.LocoCredentials.v1");
        private static readonly byte[] DeviceEntropy = Encoding.UTF8.GetBytes("Spoiler.LocoDeviceIdentity.v1");
        private readonly string _dataDirectory;
        private readonly string _credentialPath;
        private readonly string _devicePath;
        private readonly int _lockTimeoutMilliseconds;
        private static readonly object LockRegistryGuard = new object();
        private static readonly Dictionary<string, object> LockRegistry =
            new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);

        public CredentialVault()
            : this(null)
        {
        }

        public CredentialVault(string dataDirectory)
            : this(dataDirectory, 10000) { }

        public CredentialVault(string dataDirectory, int lockTimeoutMilliseconds)
        {
            _dataDirectory = string.IsNullOrWhiteSpace(dataDirectory)
                ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Spoiler")
                : Path.GetFullPath(dataDirectory);
            _credentialPath = Path.Combine(_dataDirectory, "loco-credentials.dat");
            _devicePath = Path.Combine(_dataDirectory, "loco-device.dat");
            _lockTimeoutMilliseconds = Math.Max(0, lockTimeoutMilliseconds);
        }

        public string DataDirectory
        {
            get { return _dataDirectory; }
        }

        public string CredentialPath
        {
            get { return _credentialPath; }
        }

        public string DeviceIdentityPath
        {
            get { return _devicePath; }
        }

        public bool Exists
        {
            get { return File.Exists(_credentialPath); }
        }

        /// <summary>
        /// Saves only an authenticated session bundle.  A tablet device type
        /// is forced here even if a caller accidentally supplies another
        /// value; Spoiler never stores or starts a PC companion session.
        /// </summary>
        public void Save(LocoCredentials credentials)
        {
            SaveCore(credentials, null, false);
        }

        public CredentialSnapshot SaveIfUnchanged(LocoCredentials credentials, string expectedRevision)
        {
            return SaveCore(credentials, expectedRevision, true);
        }

        private CredentialSnapshot SaveCore(LocoCredentials credentials, string expectedRevision, bool compare)
        {
            if (credentials == null) throw new ArgumentNullException("credentials");
            LocoCredentials copy = credentials.Clone();
            copy.DeviceType = TabletDeviceType;
            if (!copy.HasSessionToken || string.IsNullOrWhiteSpace(copy.UserId))
                throw new InvalidOperationException("The credential bundle does not contain a usable session token.");

            using (AcquireWriterLock())
            {
                string currentRevision = File.Exists(_credentialPath)
                    ? RevisionOf(File.ReadAllBytes(_credentialPath)) : null;
                if (compare && !string.Equals(currentRevision, expectedRevision, StringComparison.Ordinal))
                    throw new CredentialVaultConflictException();
                if (string.IsNullOrWhiteSpace(copy.DeviceUuid)) copy.DeviceUuid = GetOrCreateDeviceUuid();
                else SaveDeviceUuid(copy.DeviceUuid);
                byte[] protectedBytes = ProtectedData.Protect(Serialize(copy), Entropy, DataProtectionScope.CurrentUser);
                string temporaryPath = _credentialPath + "." + Guid.NewGuid().ToString("N") + ".tmp";
                try
                {
                    using (FileStream stream = new FileStream(temporaryPath, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    {
                        stream.Write(protectedBytes, 0, protectedBytes.Length);
                        stream.Flush(true);
                    }
                    if (File.Exists(_credentialPath)) File.Replace(temporaryPath, _credentialPath, null);
                    else File.Move(temporaryPath, _credentialPath);
                }
                finally { TryDelete(temporaryPath); }
                return new CredentialSnapshot(copy, RevisionOf(protectedBytes));
            }
        }

        /// <summary>Load the token bundle and its revision from one encrypted buffer.</summary>
        public CredentialSnapshot LoadSnapshot()
        {
            using (AcquireWriterLock())
            {
                if (!File.Exists(_credentialPath)) return new CredentialSnapshot(null, null);
                byte[] protectedBytes = File.ReadAllBytes(_credentialPath);
                string revision = RevisionOf(protectedBytes);
                try
                {
                    byte[] json = ProtectedData.Unprotect(protectedBytes, Entropy, DataProtectionScope.CurrentUser);
                    LocoCredentials credentials = Deserialize(json);
                    if (credentials == null || !credentials.IsUsable) return new CredentialSnapshot(null, revision);
                    credentials.DeviceType = TabletDeviceType;
                    if (string.IsNullOrWhiteSpace(credentials.DeviceUuid)) credentials.DeviceUuid = GetOrCreateDeviceUuid();
                    else if (!string.Equals(LoadDeviceUuid(), credentials.DeviceUuid, StringComparison.Ordinal))
                        SaveDeviceUuid(credentials.DeviceUuid);
                    return new CredentialSnapshot(credentials, revision);
                }
                catch { return new CredentialSnapshot(null, revision); }
            }
        }

        public LocoCredentials Load()
        {
            // Legacy callers treated unavailable vaults as absent.
            try { return LoadSnapshot().Credentials; }
            catch { return null; }
        }

        private static string RevisionOf(byte[] payload)
        {
            using (SHA256 sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(payload)).Replace("-", string.Empty).ToLowerInvariant();
        }

        public IDisposable AcquireAuthLease(int timeoutMilliseconds, CancellationToken cancellation)
        {
            Directory.CreateDirectory(_dataDirectory);
            string path = Path.Combine(_dataDirectory, "." + Path.GetFileName(_credentialPath) + ".auth.lock");
            return new RemoteAuthLease(path, Math.Max(0, timeoutMilliseconds), cancellation);
        }

        private sealed class RemoteAuthLease : IDisposable
        {
            private static readonly Dictionary<string, SemaphoreSlim> Gates =
                new Dictionary<string, SemaphoreSlim>(StringComparer.OrdinalIgnoreCase);
            private static readonly object GatesGuard = new object();
            private readonly SemaphoreSlim _gate;
            private readonly object _disposeGuard = new object();
            private FileStream _stream;
            private bool _held;
            public RemoteAuthLease(string path, int timeoutMilliseconds, CancellationToken cancellation)
            {
                lock (GatesGuard)
                {
                    if (!Gates.TryGetValue(path, out _gate))
                    {
                        _gate = new SemaphoreSlim(1, 1);
                        Gates[path] = _gate;
                    }
                }
                Stopwatch elapsed = Stopwatch.StartNew();
                bool entered = false;
                try
                {
                    while (!_gate.Wait(0))
                    {
                        cancellation.ThrowIfCancellationRequested();
                        if (elapsed.ElapsedMilliseconds >= timeoutMilliseconds)
                            throw new TimeoutException("auth_lease_busy");
                        cancellation.WaitHandle.WaitOne(25);
                    }
                    entered = true;
                    _stream = new FileStream(path, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.ReadWrite);
                    while (true)
                    {
                        cancellation.ThrowIfCancellationRequested();
                        try { _stream.Lock(0, 1); _held = true; break; }
                        catch (IOException)
                        {
                            if (elapsed.ElapsedMilliseconds >= timeoutMilliseconds)
                                throw new TimeoutException("auth_lease_busy");
                            cancellation.WaitHandle.WaitOne(25);
                        }
                    }
                }
                catch
                {
                    if (_stream != null) _stream.Dispose();
                    _stream = null;
                    if (entered) _gate.Release();
                    throw;
                }
            }
            public void Dispose()
            {
                lock (_disposeGuard)
                {
                    if (!_held) return;
                    _held = false;
                    try { _stream.Unlock(0, 1); }
                    finally { _stream.Dispose(); _stream = null; _gate.Release(); }
                }
            }
        }

        private IDisposable AcquireWriterLock()
        {
            Directory.CreateDirectory(_dataDirectory);
            string path = Path.Combine(_dataDirectory, "." + Path.GetFileName(_credentialPath) + ".lock");
            object gate;
            lock (LockRegistryGuard)
            {
                if (!LockRegistry.TryGetValue(path, out gate))
                {
                    gate = new object();
                    LockRegistry[path] = gate;
                }
            }
            return new VaultWriterLock(path, gate, _lockTimeoutMilliseconds);
        }

        private sealed class VaultWriterLock : IDisposable
        {
            private readonly object _gate;
            private FileStream _stream;
            public VaultWriterLock(string path, object gate, int timeoutMilliseconds)
            {
                _gate = gate;
                Stopwatch elapsed = Stopwatch.StartNew();
                if (!Monitor.TryEnter(gate, timeoutMilliseconds))
                    throw new TimeoutException("The credential vault writer lock timed out.");
                try
                {
                    _stream = new FileStream(path, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.ReadWrite);
                    while (true)
                    {
                        try { _stream.Lock(0, 1); break; }
                        catch (IOException)
                        {
                            if (elapsed.ElapsedMilliseconds >= timeoutMilliseconds)
                                throw new TimeoutException("The credential vault writer lock timed out.");
                            Thread.Sleep((int)Math.Max(1, Math.Min(50, timeoutMilliseconds - elapsed.ElapsedMilliseconds)));
                        }
                    }
                }
                catch
                {
                    if (_stream != null) _stream.Dispose();
                    _stream = null;
                    Monitor.Exit(_gate);
                    throw;
                }
            }
            public void Dispose()
            {
                try
                {
                    if (_stream != null)
                    {
                        try { _stream.Unlock(0, 1); }
                        finally { _stream.Dispose(); _stream = null; }
                    }
                }
                finally { Monitor.Exit(_gate); }
            }
        }

        public bool Clear()
        {
            try
            {
                using (AcquireWriterLock())
                {
                    if (File.Exists(_credentialPath)) File.Delete(_credentialPath);
                    if (File.Exists(_devicePath)) File.Delete(_devicePath);
                    return !File.Exists(_credentialPath) && !File.Exists(_devicePath);
                }
            }
            catch
            {
                return false;
            }
        }

        /// <summary>
        /// Returns the stable tablet UUID used by the companion.  It is
        /// generated and protected before the registration request is sent,
        /// so a retry never silently becomes a second device.
        /// </summary>
        public string GetOrCreateDeviceUuid()
        {
            string existing = LoadDeviceUuid();
            if (!string.IsNullOrWhiteSpace(existing))
            {
                return existing;
            }

            string generated = Guid.NewGuid().ToString("N");
            SaveDeviceUuid(generated);
            return generated;
        }

        public string LoadDeviceUuid()
        {
            if (!File.Exists(_devicePath))
            {
                return null;
            }

            try
            {
                byte[] protectedBytes = File.ReadAllBytes(_devicePath);
                byte[] json = ProtectedData.Unprotect(protectedBytes, DeviceEntropy, DataProtectionScope.CurrentUser);
                LocoDeviceIdentity identity = DeserializeDevice(json);
                if (identity == null || string.IsNullOrWhiteSpace(identity.DeviceUuid))
                {
                    return null;
                }
                return identity.DeviceUuid.Trim();
            }
            catch
            {
                return null;
            }
        }

        public void SaveDeviceUuid(string deviceUuid)
        {
            string normalized = (deviceUuid ?? string.Empty).Trim();
            if (normalized.Length < 16 || normalized.Length > 128)
            {
                throw new ArgumentException("The device UUID is not valid.", "deviceUuid");
            }

            LocoDeviceIdentity identity = new LocoDeviceIdentity
            {
                DeviceUuid = normalized,
                DeviceType = TabletDeviceType
            };
            byte[] json = SerializeDevice(identity);
            byte[] protectedBytes = ProtectedData.Protect(json, DeviceEntropy, DataProtectionScope.CurrentUser);
            string temporaryPath = _devicePath + "." + Guid.NewGuid().ToString("N") + ".tmp";

            try
            {
                Directory.CreateDirectory(_dataDirectory);
                File.WriteAllBytes(temporaryPath, protectedBytes);
                if (File.Exists(_devicePath))
                {
                    File.Replace(temporaryPath, _devicePath, null);
                }
                else
                {
                    File.Move(temporaryPath, _devicePath);
                }
            }
            finally
            {
                TryDelete(temporaryPath);
            }
        }

        internal static byte[] Serialize(LocoCredentials credentials)
        {
            DataContractJsonSerializer serializer = new DataContractJsonSerializer(typeof(LocoCredentials));
            using (MemoryStream stream = new MemoryStream())
            {
                serializer.WriteObject(stream, credentials);
                return stream.ToArray();
            }
        }

        internal static LocoCredentials Deserialize(byte[] json)
        {
            if (json == null || json.Length == 0)
            {
                return null;
            }

            DataContractJsonSerializer serializer = new DataContractJsonSerializer(typeof(LocoCredentials));
            using (MemoryStream stream = new MemoryStream(json))
            {
                return serializer.ReadObject(stream) as LocoCredentials;
            }
        }

        [DataContract]
        private sealed class LocoDeviceIdentity
        {
            [DataMember(Name = "device_uuid", Order = 1)]
            public string DeviceUuid { get; set; }

            [DataMember(Name = "device_type", Order = 2)]
            public string DeviceType { get; set; }
        }

        private static byte[] SerializeDevice(LocoDeviceIdentity identity)
        {
            DataContractJsonSerializer serializer = new DataContractJsonSerializer(typeof(LocoDeviceIdentity));
            using (MemoryStream stream = new MemoryStream())
            {
                serializer.WriteObject(stream, identity);
                return stream.ToArray();
            }
        }

        private static LocoDeviceIdentity DeserializeDevice(byte[] json)
        {
            if (json == null || json.Length == 0)
            {
                return null;
            }

            DataContractJsonSerializer serializer = new DataContractJsonSerializer(typeof(LocoDeviceIdentity));
            using (MemoryStream stream = new MemoryStream(json))
            {
                LocoDeviceIdentity identity = serializer.ReadObject(stream) as LocoDeviceIdentity;
                if (identity == null || !string.Equals(identity.DeviceType, TabletDeviceType, StringComparison.OrdinalIgnoreCase))
                {
                    return null;
                }
                return identity;
            }
        }

        private static void TryDelete(string path)
        {
            try
            {
                if (!string.IsNullOrEmpty(path) && File.Exists(path))
                {
                    File.Delete(path);
                }
            }
            catch
            {
            }
        }
    }
}
