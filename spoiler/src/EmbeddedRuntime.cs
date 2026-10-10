using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Security;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

[assembly: InternalsVisibleTo("Spoiler.RuntimeTests")]

namespace Spoiler
{
    /// <summary>
    /// A verified view of the three files required by the bundled read-only
    /// bridge.  The open handles deliberately deny write/delete sharing.  A
    /// cache file therefore cannot be replaced between verification and
    /// Process.Start while Spoiler is running.
    /// </summary>
    internal sealed class EmbeddedRuntimeLease : IDisposable
    {
        private FileStream[] _verifiedHandles;

        internal EmbeddedRuntimeLease(
            string runtimeDirectory,
            string nodeExecutablePath,
            string bridgeScriptPath,
            string authScriptPath,
            FileStream[] verifiedHandles)
        {
            RuntimeDirectory = runtimeDirectory;
            NodeExecutablePath = nodeExecutablePath;
            BridgeScriptPath = bridgeScriptPath;
            AuthScriptPath = authScriptPath;
            _verifiedHandles = verifiedHandles;
        }

        public string RuntimeDirectory { get; private set; }
        public string NodeExecutablePath { get; private set; }
        public string BridgeScriptPath { get; private set; }
        public string AuthScriptPath { get; private set; }

        public void Dispose()
        {
            FileStream[] handles = Interlocked.Exchange(ref _verifiedHandles, null);
            if (handles == null)
            {
                return;
            }

            foreach (FileStream handle in handles)
            {
                if (handle == null)
                {
                    continue;
                }
                try { handle.Dispose(); } catch { }
            }
        }
    }

    /// <summary>
    /// Extracts the embedded Node/bridge payload into a versioned LocalAppData
    /// cache.  Only a strict, canonical manifest and three fixed file names
    /// are accepted.  Files are fully hashed before the atomic directory move
    /// and again when opened for execution.
    /// </summary>
    internal static class EmbeddedRuntime
    {
        internal const string ManifestResourceName = "Spoiler.Runtime.Manifest";
        internal const string ActiveManifestFileName = "active-runtime.manifest";
        internal const string NodeResourceName = "Spoiler.Runtime.Node";
        internal const string BridgeResourceName = "Spoiler.Runtime.Bridge.Listener";
        internal const string AuthResourceName = "Spoiler.Runtime.Bridge.Auth";

        internal const string NodeFileName = "node.exe";
        internal const string BridgeFileName = "spoiler-bridge.mjs";
        internal const string AuthFileName = "spoiler-auth.mjs";

        private static readonly object SharedSync = new object();
        private static readonly object EnvironmentSync = new object();
        private static EmbeddedRuntimeLease _sharedLease;

        private static readonly string[] SafeEnvironmentNames =
        {
            "SystemRoot",
            "WINDIR",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "LOCALAPPDATA",
            "APPDATA",
            "ProgramData",
            "ALLUSERSPROFILE",
            "COMSPEC",
            "PROCESSOR_ARCHITECTURE",
            "PROCESSOR_IDENTIFIER",
            "NUMBER_OF_PROCESSORS"
        };

        public static EmbeddedRuntimeLease EnsureReady()
        {
            lock (SharedSync)
            {
                if (_sharedLease == null)
                {
                    _sharedLease = EnsureReadyAtRoot(GetDefaultRuntimeRoot());
                }
                return _sharedLease;
            }
        }

        internal static EmbeddedRuntimeLease EnsureReadyAtRoot(string requestedRuntimeRoot)
        {
            RuntimeManifest manifest = RuntimeManifest.Load();
            string runtimeRoot = Canonicalize(requestedRuntimeRoot);
            EnsureSecureDirectory(runtimeRoot);

            string finalDirectory = DirectChild(runtimeRoot, manifest.Version);
            string mutexName = "Local\\Spoiler.EmbeddedRuntime." + manifest.Version;
            using (Mutex runtimeMutex = new Mutex(false, mutexName))
            {
                bool ownsMutex = false;
                try
                {
                    try
                    {
                        ownsMutex = runtimeMutex.WaitOne(TimeSpan.FromSeconds(45));
                    }
                    catch (AbandonedMutexException)
                    {
                        ownsMutex = true;
                    }

                    if (!ownsMutex)
                    {
                        throw new IOException("내장 실행 구성 요소를 준비하는 다른 Spoiler 작업이 끝나지 않았어요. 잠시 후 다시 실행해 주세요.");
                    }

                    if (Directory.Exists(finalDirectory))
                    {
                        try
                        {
                            return PublishVerifiedLease(runtimeRoot, finalDirectory, manifest);
                        }
                        catch (Exception exception)
                        {
                            if (!IsValidationFailure(exception))
                            {
                                throw;
                            }
                            QuarantineCorruptedDirectory(runtimeRoot, finalDirectory, manifest.Version, exception);
                        }
                    }
                    else if (File.Exists(finalDirectory))
                    {
                        throw new SecurityException("Spoiler의 내장 실행 캐시 위치가 안전한 폴더가 아니어서 중단했어요.");
                    }

                    string stageName = ".stage-" + manifest.Version + "-" + Guid.NewGuid().ToString("N");
                    string stagingDirectory = DirectChild(runtimeRoot, stageName);
                    bool stagingMoved = false;
                    try
                    {
                        Directory.CreateDirectory(stagingDirectory);
                        RejectReparsePoint(stagingDirectory, "임시 실행 캐시");

                        foreach (RuntimeManifestEntry entry in manifest.Entries)
                        {
                            ExtractOne(stagingDirectory, entry);
                        }

                        // This first complete validation is intentionally
                        // closed before Directory.Move because Windows may not
                        // rename a directory containing our deny-write/delete
                        // handles.
                        using (EmbeddedRuntimeLease staged = OpenVerifiedLease(stagingDirectory, manifest))
                        {
                        }

                        try
                        {
                            Directory.Move(stagingDirectory, finalDirectory);
                            stagingMoved = true;
                        }
                        catch (Exception exception)
                        {
                            throw new IOException("Spoiler의 내장 실행 캐시를 안전하게 확정하지 못했어요. 다시 실행해 주세요.", exception);
                        }

                        return PublishVerifiedLease(runtimeRoot, finalDirectory, manifest);
                    }
                    finally
                    {
                        if (!stagingMoved)
                        {
                            TryCleanupStagingDirectory(runtimeRoot, stagingDirectory);
                        }
                    }
                }
                finally
                {
                    if (ownsMutex)
                    {
                        try { runtimeMutex.ReleaseMutex(); } catch { }
                    }
                }
            }
        }

        internal static string CreateSelfTestReport()
        {
            return CreateSelfTestReport(EnsureReady());
        }

        internal static string CreateSelfTestReport(string requestedRuntimeRoot)
        {
            using (EmbeddedRuntimeLease lease = EnsureReadyAtRoot(requestedRuntimeRoot))
            {
                return CreateSelfTestReport(lease);
            }
        }

        private static string CreateSelfTestReport(EmbeddedRuntimeLease lease)
        {
            RuntimeManifest manifest = RuntimeManifest.Load();
            List<string> report = new List<string>();
            report.Add("PASS: embedded runtime self-test");
            report.Add("manifest_version=" + manifest.Version);
            report.Add("runtime_directory=" + lease.RuntimeDirectory);

            foreach (RuntimeManifestEntry entry in manifest.Entries)
            {
                VerifyEmbeddedResource(entry);
                report.Add(string.Format(
                    CultureInfo.InvariantCulture,
                    "resource={0} file={1} size={2} sha256={3}",
                    entry.ResourceName,
                    entry.FileName,
                    entry.Size,
                    entry.Sha256));
            }

            string nodeVersion = RunNodeVersion(lease);
            report.Add("node_version=" + nodeVersion);
            report.Add("verified_open_handles=3");
            return string.Join(Environment.NewLine, report.ToArray()) + Environment.NewLine;
        }

        internal static string RunNodeVersion(EmbeddedRuntimeLease lease)
        {
            if (lease == null)
            {
                throw new ArgumentNullException("lease");
            }

            ProcessStartInfo startInfo = new ProcessStartInfo
            {
                FileName = lease.NodeExecutablePath,
                Arguments = "--version",
                WorkingDirectory = lease.RuntimeDirectory,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            SanitizeNodeEnvironment(startInfo);

            using (Process process = new Process { StartInfo = startInfo })
            {
                if (!process.Start())
                {
                    throw new IOException("내장 Node 실행 확인을 시작하지 못했어요.");
                }

                var outputTask = process.StandardOutput.ReadToEndAsync();
                var errorTask = process.StandardError.ReadToEndAsync();
                if (!process.WaitForExit(15000))
                {
                    try { process.Kill(); } catch { }
                    throw new TimeoutException("내장 Node 실행 확인 시간이 초과됐어요.");
                }
                if (process.ExitCode != 0)
                {
                    throw new IOException("내장 Node 실행 확인에 실패했어요.");
                }

                if (!System.Threading.Tasks.Task.WaitAll(new System.Threading.Tasks.Task[] { outputTask, errorTask }, 1000))
                    throw new TimeoutException("내장 Node 출력 확인 시간이 초과됐어요.");
                string version = (outputTask.Result ?? string.Empty).Trim();
                if (version.Length < 2 || version[0] != 'v')
                {
                    throw new InvalidDataException("내장 Node가 올바른 버전 응답을 보내지 않았어요.");
                }
                return version;
            }
        }

        /// <summary>
        /// Starts from a small OS-only allowlist instead of trying to predict
        /// every present/future Node injection variable.  In particular this
        /// removes NODE_OPTIONS, NODE_PATH, NODE_EXTRA_CA_CERTS, OPENSSL_CONF,
        /// OPENSSL_MODULES, SSL_CERT_FILE, SSL_CERT_DIR, and related variables.
        /// </summary>
        internal static void SanitizeNodeEnvironment(ProcessStartInfo startInfo)
        {
            if (startInfo == null)
            {
                throw new ArgumentNullException("startInfo");
            }

            // Some Windows launchers hand this process an invalid block with
            // duplicate case variants such as Path and PATH. .NET Framework
            // materializes either ProcessStartInfo environment property
            // through a case-insensitive dictionary, so normalize the native
            // process block before touching the lazy property.
            NormalizeDuplicateProcessEnvironmentKeys();
            IDictionary<string, string> environment = startInfo.Environment;
            Dictionary<string, string> safeValues = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (string name in SafeEnvironmentNames)
            {
                string value;
                environment.TryGetValue(name, out value);
                if (!string.IsNullOrEmpty(value))
                {
                    safeValues[name] = value;
                }
            }

            environment.Clear();
            foreach (KeyValuePair<string, string> pair in safeValues)
            {
                environment[pair.Key] = pair.Value;
            }
        }

        private static void NormalizeDuplicateProcessEnvironmentKeys()
        {
            lock (EnvironmentSync)
            {
                IDictionary snapshot = Environment.GetEnvironmentVariables(EnvironmentVariableTarget.Process);
                Dictionary<string, List<string>> namesByFoldedKey =
                    new Dictionary<string, List<string>>(StringComparer.Ordinal);
                foreach (object keyObject in snapshot.Keys)
                {
                    string name = Convert.ToString(keyObject, CultureInfo.InvariantCulture);
                    if (string.IsNullOrEmpty(name))
                    {
                        continue;
                    }

                    string folded = name.ToUpperInvariant();
                    List<string> names;
                    if (!namesByFoldedKey.TryGetValue(folded, out names))
                    {
                        names = new List<string>();
                        namesByFoldedKey.Add(folded, names);
                    }
                    names.Add(name);
                }

                foreach (KeyValuePair<string, List<string>> group in namesByFoldedKey)
                {
                    if (group.Value.Count < 2)
                    {
                        continue;
                    }

                    string preservedValue = Convert.ToString(snapshot[group.Value[0]], CultureInfo.InvariantCulture);
                    foreach (string originalName in group.Value)
                    {
                        Environment.SetEnvironmentVariable(originalName, null, EnvironmentVariableTarget.Process);
                    }
                    Environment.SetEnvironmentVariable(group.Key, preservedValue, EnvironmentVariableTarget.Process);
                }
            }
        }

        internal static bool IsSafeNodeEnvironmentName(string name)
        {
            if (string.IsNullOrEmpty(name))
            {
                return false;
            }
            foreach (string safeName in SafeEnvironmentNames)
            {
                if (string.Equals(name, safeName, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            return string.Equals(name, "AGENT_MESSENGER_CONFIG_DIR", StringComparison.OrdinalIgnoreCase);
        }

        internal static string PreparePrivateDataDirectory(string requestedDirectory)
        {
            string canonical = Canonicalize(requestedDirectory);
            EnsureSecureDirectory(canonical);
            return canonical;
        }

        private static string GetDefaultRuntimeRoot()
        {
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (string.IsNullOrWhiteSpace(localAppData))
            {
                throw new IOException("Windows의 로컬 앱 데이터 폴더를 찾지 못했어요.");
            }
            return Path.Combine(localAppData, "Spoiler", "runtime");
        }

        private static void ExtractOne(string stagingDirectory, RuntimeManifestEntry entry)
        {
            string targetPath = DirectChild(stagingDirectory, entry.FileName);
            Assembly assembly = Assembly.GetExecutingAssembly();
            using (Stream source = assembly.GetManifestResourceStream(entry.ResourceName))
            {
                if (source == null)
                {
                    throw new InvalidDataException("Spoiler.exe에 필요한 내장 파일이 없어요: " + entry.FileName);
                }

                long total = 0;
                string actualHash;
                using (SHA256 sha256 = SHA256.Create())
                using (FileStream target = new FileStream(targetPath, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1024 * 128, FileOptions.SequentialScan))
                {
                    byte[] buffer = new byte[1024 * 128];
                    int read;
                    while ((read = source.Read(buffer, 0, buffer.Length)) > 0)
                    {
                        target.Write(buffer, 0, read);
                        sha256.TransformBlock(buffer, 0, read, null, 0);
                        total += read;
                    }
                    sha256.TransformFinalBlock(new byte[0], 0, 0);
                    target.Flush(true);
                    actualHash = ToHex(sha256.Hash);
                }

                if (total != entry.Size || !HashesEqual(actualHash, entry.Sha256))
                {
                    throw new InvalidDataException("Spoiler.exe의 내장 파일 무결성 확인에 실패했어요: " + entry.FileName);
                }
            }

            FileAttributes attributes = File.GetAttributes(targetPath);
            File.SetAttributes(targetPath, attributes | FileAttributes.ReadOnly);
        }

        private static EmbeddedRuntimeLease OpenVerifiedLease(string directory, RuntimeManifest manifest)
        {
            ValidateDirectoryShape(directory, manifest);
            FileStream[] handles = new FileStream[manifest.Entries.Length];
            try
            {
                for (int index = 0; index < manifest.Entries.Length; index++)
                {
                    RuntimeManifestEntry entry = manifest.Entries[index];
                    string path = DirectChild(directory, entry.FileName);
                    RejectReparsePoint(path, entry.FileName);

                    FileStream handle = new FileStream(
                        path,
                        FileMode.Open,
                        FileAccess.Read,
                        FileShare.Read,
                        1024 * 128,
                        FileOptions.SequentialScan);
                    handles[index] = handle;
                    if (handle.Length != entry.Size)
                    {
                        throw new InvalidDataException("내장 실행 캐시 파일 크기가 바뀌었어요: " + entry.FileName);
                    }

                    string actualHash = ComputeHash(handle);
                    if (!HashesEqual(actualHash, entry.Sha256))
                    {
                        throw new InvalidDataException("내장 실행 캐시 파일이 바뀌었어요: " + entry.FileName);
                    }
                    handle.Position = 0;
                }

                string canonicalDirectory = Canonicalize(directory);
                return new EmbeddedRuntimeLease(
                    canonicalDirectory,
                    DirectChild(canonicalDirectory, NodeFileName),
                    DirectChild(canonicalDirectory, BridgeFileName),
                    DirectChild(canonicalDirectory, AuthFileName),
                    handles);
            }
            catch
            {
                foreach (FileStream handle in handles)
                {
                    if (handle == null) continue;
                    try { handle.Dispose(); } catch { }
                }
                throw;
            }
        }

        private static EmbeddedRuntimeLease PublishVerifiedLease(string runtimeRoot, string directory, RuntimeManifest manifest)
        {
            EmbeddedRuntimeLease lease = OpenVerifiedLease(directory, manifest);
            try
            {
                // Publication is authoritative. Hub never selects a version by
                // cache modification times or partially extracted contents.
                string publication = DirectChild(runtimeRoot, ActiveManifestFileName);
                string temporary = DirectChild(runtimeRoot, ".publish-" + Guid.NewGuid().ToString("N"));
                try
                {
                    byte[] data = new UTF8Encoding(false).GetBytes(manifest.ToCanonicalString());
                    using (FileStream output = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    {
                        output.Write(data, 0, data.Length);
                        output.Flush(true);
                    }
                    // Multiple versions may start concurrently. Serialization
                    // covers only this atomic publication, not extraction.
                    using (Mutex publicationMutex = new Mutex(false, "Local\\Spoiler.RuntimePublication"))
                    {
                        bool owns = false;
                        try
                        {
                            try { owns = publicationMutex.WaitOne(TimeSpan.FromSeconds(15)); }
                            catch (AbandonedMutexException) { owns = true; }
                            if (!owns) throw new IOException("실행 구성 요소 버전 확정 시간이 초과됐어요.");
                            if (File.Exists(publication))
                            {
                                RejectReparsePoint(publication, "실행 버전 목록");
                                File.Replace(temporary, publication, null);
                            }
                            else
                            {
                                File.Move(temporary, publication);
                            }
                        }
                        finally { if (owns) publicationMutex.ReleaseMutex(); }
                    }
                }
                finally { if (File.Exists(temporary)) File.Delete(temporary); }
                return lease;
            }
            catch
            {
                lease.Dispose();
                throw;
            }
        }

        private static void ValidateDirectoryShape(string directory, RuntimeManifest manifest)
        {
            string canonicalDirectory = Canonicalize(directory);
            if (!Directory.Exists(canonicalDirectory))
            {
                throw new DirectoryNotFoundException("내장 실행 캐시 폴더가 없어요.");
            }
            RejectReparsePoint(canonicalDirectory, "실행 캐시");

            string[] childDirectories = Directory.GetDirectories(canonicalDirectory, "*", SearchOption.TopDirectoryOnly);
            if (childDirectories.Length != 0)
            {
                throw new InvalidDataException("내장 실행 캐시에 예상하지 않은 폴더가 있어요.");
            }

            string[] files = Directory.GetFiles(canonicalDirectory, "*", SearchOption.TopDirectoryOnly);
            if (files.Length != manifest.Entries.Length)
            {
                throw new InvalidDataException("내장 실행 캐시의 파일 구성이 올바르지 않아요.");
            }

            HashSet<string> actualNames = new HashSet<string>(StringComparer.Ordinal);
            foreach (string file in files)
            {
                string canonicalFile = Canonicalize(file);
                EnsureContained(canonicalDirectory, canonicalFile, true);
                RejectReparsePoint(canonicalFile, Path.GetFileName(canonicalFile));
                actualNames.Add(Path.GetFileName(canonicalFile));
            }

            foreach (RuntimeManifestEntry entry in manifest.Entries)
            {
                if (!actualNames.Contains(entry.FileName))
                {
                    throw new InvalidDataException("내장 실행 캐시에 필요한 파일이 없어요: " + entry.FileName);
                }
            }
        }

        private static void VerifyEmbeddedResource(RuntimeManifestEntry entry)
        {
            using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(entry.ResourceName))
            {
                if (stream == null)
                {
                    throw new InvalidDataException("Spoiler.exe의 내장 리소스가 없어요: " + entry.ResourceName);
                }
                if (stream.Length != entry.Size || !HashesEqual(ComputeHash(stream), entry.Sha256))
                {
                    throw new InvalidDataException("Spoiler.exe의 내장 리소스 해시가 일치하지 않아요: " + entry.ResourceName);
                }
            }
        }

        private static void QuarantineCorruptedDirectory(string runtimeRoot, string finalDirectory, string version, Exception validationError)
        {
            string canonicalFinal = Canonicalize(finalDirectory);
            EnsureContained(runtimeRoot, canonicalFinal, true);
            if (IsReparsePoint(canonicalFinal))
            {
                throw new SecurityException(
                    "Spoiler 실행 캐시가 다른 위치를 가리키고 있어 안전을 위해 실행하지 않았어요. " +
                    "Windows 로컬 앱 데이터의 Spoiler\\runtime 폴더를 확인해 주세요.",
                    validationError);
            }

            string quarantineName = ".corrupt-" + version + "-" + DateTime.UtcNow.ToString("yyyyMMddHHmmss", CultureInfo.InvariantCulture)
                + "-" + Guid.NewGuid().ToString("N");
            string quarantineDirectory = DirectChild(runtimeRoot, quarantineName);
            try
            {
                Directory.Move(canonicalFinal, quarantineDirectory);
            }
            catch (Exception exception)
            {
                throw new IOException(
                    "손상된 Spoiler 실행 캐시를 격리하지 못해 안전을 위해 실행을 중단했어요. " +
                    "Spoiler를 모두 종료한 뒤 다시 실행해 주세요.",
                    exception);
            }
        }

        private static bool IsValidationFailure(Exception exception)
        {
            return exception is IOException
                || exception is UnauthorizedAccessException
                || exception is SecurityException
                || exception is InvalidDataException;
        }

        private static void EnsureSecureDirectory(string directory)
        {
            string canonical = Canonicalize(directory);
            Directory.CreateDirectory(canonical);

            DirectoryInfo cursor = new DirectoryInfo(canonical);
            while (cursor != null)
            {
                if ((cursor.Attributes & FileAttributes.ReparsePoint) != 0)
                {
                    throw new SecurityException("내장 실행 캐시 경로에 연결 지점이 있어 안전을 위해 중단했어요: " + cursor.FullName);
                }
                cursor = cursor.Parent;
            }
        }

        private static void TryCleanupStagingDirectory(string runtimeRoot, string stagingDirectory)
        {
            try
            {
                string canonicalRoot = Canonicalize(runtimeRoot);
                string canonicalStage = Canonicalize(stagingDirectory);
                EnsureContained(canonicalRoot, canonicalStage, true);
                if (!Path.GetFileName(canonicalStage).StartsWith(".stage-v1-", StringComparison.Ordinal))
                {
                    return;
                }
                if (!Directory.Exists(canonicalStage))
                {
                    return;
                }

                SafeDeleteTree(canonicalStage, canonicalStage);
            }
            catch
            {
                // Cleanup is best-effort.  A suspicious staging tree is left
                // in place rather than risking traversal outside our direct
                // child; it is never selected for execution.
            }
        }

        private static void SafeDeleteTree(string stagingRoot, string currentDirectory)
        {
            string canonicalRoot = Canonicalize(stagingRoot);
            string canonicalCurrent = Canonicalize(currentDirectory);
            if (!string.Equals(canonicalRoot, canonicalCurrent, StringComparison.OrdinalIgnoreCase))
            {
                EnsureContained(canonicalRoot, canonicalCurrent, false);
            }

            if (IsReparsePoint(canonicalCurrent))
            {
                // Directory.Delete(false) removes the link itself and never
                // recurses into its target.
                Directory.Delete(canonicalCurrent, false);
                return;
            }

            foreach (string file in Directory.GetFiles(canonicalCurrent, "*", SearchOption.TopDirectoryOnly))
            {
                string canonicalFile = Canonicalize(file);
                EnsureContained(canonicalRoot, canonicalFile, false);
                FileAttributes attributes = File.GetAttributes(canonicalFile);
                if ((attributes & FileAttributes.ReparsePoint) == 0
                    && (attributes & FileAttributes.ReadOnly) != 0)
                {
                    File.SetAttributes(canonicalFile, attributes & ~FileAttributes.ReadOnly);
                }
                // File.Delete removes a file reparse point itself.
                File.Delete(canonicalFile);
            }

            foreach (string child in Directory.GetDirectories(canonicalCurrent, "*", SearchOption.TopDirectoryOnly))
            {
                SafeDeleteTree(canonicalRoot, child);
            }
            Directory.Delete(canonicalCurrent, false);
        }

        private static string DirectChild(string parent, string childName)
        {
            if (string.IsNullOrWhiteSpace(childName)
                || childName.IndexOfAny(new[] { Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar }) >= 0
                || childName.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0
                || !string.Equals(Path.GetFileName(childName), childName, StringComparison.Ordinal)
                || childName == "."
                || childName == "..")
            {
                throw new SecurityException("내장 실행 파일 이름이 안전하지 않아요.");
            }

            string canonicalParent = Canonicalize(parent);
            string candidate = Canonicalize(Path.Combine(canonicalParent, childName));
            EnsureContained(canonicalParent, candidate, true);
            return candidate;
        }

        private static void EnsureContained(string parent, string candidate, bool directChild)
        {
            string canonicalParent = Canonicalize(parent).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string canonicalCandidate = Canonicalize(candidate);
            string prefix = canonicalParent + Path.DirectorySeparatorChar;
            if (!canonicalCandidate.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            {
                throw new SecurityException("내장 실행 캐시 경로가 허용된 폴더를 벗어났어요.");
            }
            if (directChild && !string.Equals(Path.GetDirectoryName(canonicalCandidate), canonicalParent, StringComparison.OrdinalIgnoreCase))
            {
                throw new SecurityException("내장 실행 캐시 항목은 지정 폴더의 바로 아래에 있어야 해요.");
            }
        }

        private static string Canonicalize(string path)
        {
            if (string.IsNullOrWhiteSpace(path))
            {
                throw new ArgumentException("A runtime path is required.", "path");
            }
            return Path.GetFullPath(path);
        }

        private static void RejectReparsePoint(string path, string label)
        {
            if (IsReparsePoint(path))
            {
                throw new SecurityException(label + " 항목이 다른 위치를 가리켜 안전을 위해 중단했어요.");
            }
        }

        private static bool IsReparsePoint(string path)
        {
            return (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0;
        }

        private static string ComputeHash(Stream stream)
        {
            if (stream.CanSeek)
            {
                stream.Position = 0;
            }
            using (SHA256 sha256 = SHA256.Create())
            {
                return ToHex(sha256.ComputeHash(stream));
            }
        }

        private static bool HashesEqual(string first, string second)
        {
            if (first == null || second == null || first.Length != second.Length)
            {
                return false;
            }
            int difference = 0;
            for (int index = 0; index < first.Length; index++)
            {
                difference |= char.ToLowerInvariant(first[index]) ^ char.ToLowerInvariant(second[index]);
            }
            return difference == 0;
        }

        private static string ToHex(byte[] bytes)
        {
            StringBuilder builder = new StringBuilder(bytes.Length * 2);
            foreach (byte value in bytes)
            {
                builder.Append(value.ToString("x2", CultureInfo.InvariantCulture));
            }
            return builder.ToString();
        }

        private static string SafeSingleLine(string value)
        {
            string normalized = (value ?? string.Empty).Replace('\r', ' ').Replace('\n', ' ').Trim();
            return normalized.Length <= 240 ? normalized : normalized.Substring(0, 240);
        }

        private sealed class RuntimeManifest
        {
            private const string Header = "SpoilerRuntimeManifest/1";

            public string Version { get; private set; }
            public RuntimeManifestEntry[] Entries { get; private set; }

            public static RuntimeManifest Load()
            {
                string manifestText;
                using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(ManifestResourceName))
                {
                    if (stream == null)
                    {
                        throw new InvalidDataException("Spoiler.exe에 내장 실행 목록이 없어요.");
                    }
                    using (StreamReader reader = new StreamReader(stream, new UTF8Encoding(false, true), false))
                    {
                        manifestText = reader.ReadToEnd();
                    }
                }

                if (manifestText.IndexOf('\r') >= 0 || !manifestText.EndsWith("\n", StringComparison.Ordinal))
                {
                    throw new InvalidDataException("내장 실행 목록의 줄 형식이 올바르지 않아요.");
                }

                string[] lines = manifestText.Substring(0, manifestText.Length - 1).Split('\n');
                if (lines.Length != 5 || lines[0] != Header || !lines[1].StartsWith("version=", StringComparison.Ordinal))
                {
                    throw new InvalidDataException("내장 실행 목록의 형식이 올바르지 않아요.");
                }

                string version = lines[1].Substring("version=".Length);
                RuntimeManifestEntry[] entries =
                {
                    ParseEntry(lines[2], NodeFileName, NodeResourceName),
                    ParseEntry(lines[3], BridgeFileName, BridgeResourceName),
                    ParseEntry(lines[4], AuthFileName, AuthResourceName)
                };

                string expectedVersion = ComputeVersion(entries);
                if (!string.Equals(version, expectedVersion, StringComparison.Ordinal))
                {
                    throw new InvalidDataException("내장 실행 목록 버전이 콘텐츠와 일치하지 않아요.");
                }

                RuntimeManifest result = new RuntimeManifest { Version = version, Entries = entries };
                if (!string.Equals(manifestText, result.ToCanonicalString(), StringComparison.Ordinal))
                {
                    throw new InvalidDataException("내장 실행 목록이 표준 형식이 아니에요.");
                }
                return result;
            }

            public string ToCanonicalString()
            {
                StringBuilder builder = new StringBuilder();
                builder.Append(Header).Append('\n');
                builder.Append("version=").Append(Version).Append('\n');
                foreach (RuntimeManifestEntry entry in Entries)
                {
                    builder.Append(entry.ToCanonicalLine()).Append('\n');
                }
                return builder.ToString();
            }

            private static RuntimeManifestEntry ParseEntry(string line, string expectedFile, string expectedResource)
            {
                string[] fields = line.Split('|');
                long size;
                if (fields.Length != 4
                    || fields[0] != expectedFile
                    || fields[1] != expectedResource
                    || !long.TryParse(fields[2], NumberStyles.None, CultureInfo.InvariantCulture, out size)
                    || size <= 0
                    || !IsLowerHex(fields[3], 64))
                {
                    throw new InvalidDataException("내장 실행 목록 항목이 올바르지 않아요: " + expectedFile);
                }
                return new RuntimeManifestEntry(expectedFile, expectedResource, size, fields[3]);
            }

            private static string ComputeVersion(RuntimeManifestEntry[] entries)
            {
                StringBuilder seed = new StringBuilder();
                foreach (RuntimeManifestEntry entry in entries)
                {
                    seed.Append(entry.ToCanonicalLine()).Append('\n');
                }
                byte[] bytes = new UTF8Encoding(false).GetBytes(seed.ToString());
                using (SHA256 sha256 = SHA256.Create())
                {
                    return "v1-" + ToHex(sha256.ComputeHash(bytes)).Substring(0, 16);
                }
            }

            private static bool IsLowerHex(string value, int length)
            {
                if (value == null || value.Length != length) return false;
                foreach (char character in value)
                {
                    if (!((character >= '0' && character <= '9') || (character >= 'a' && character <= 'f')))
                    {
                        return false;
                    }
                }
                return true;
            }
        }

        private sealed class RuntimeManifestEntry
        {
            public RuntimeManifestEntry(string fileName, string resourceName, long size, string sha256)
            {
                FileName = fileName;
                ResourceName = resourceName;
                Size = size;
                Sha256 = sha256;
            }

            public string FileName { get; private set; }
            public string ResourceName { get; private set; }
            public long Size { get; private set; }
            public string Sha256 { get; private set; }

            public string ToCanonicalLine()
            {
                return FileName + "|" + ResourceName + "|" + Size.ToString(CultureInfo.InvariantCulture) + "|" + Sha256;
            }
        }
    }
}
