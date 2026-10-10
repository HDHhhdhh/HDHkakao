using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;

namespace Spoiler.Tests
{
    internal static class RuntimeTests
    {
        public static int Main(string[] args)
        {
            string testRoot = args.Length > 0
                ? Path.GetFullPath(args[0])
                : Path.Combine(Path.GetTempPath(), "Spoiler-runtime-contract-" + Guid.NewGuid().ToString("N"));
            string reportPath = args.Length > 1 ? Path.GetFullPath(args[1]) : null;
            List<string> report = new List<string>();
            int failures = 0;

            try
            {
                Directory.CreateDirectory(testRoot);
                string runtimeRoot = Path.Combine(testRoot, "runtime");
                string firstDirectory;
                string firstBridgeHash;

                using (EmbeddedRuntimeLease first = EmbeddedRuntime.EnsureReadyAtRoot(runtimeRoot))
                {
                    firstDirectory = first.RuntimeDirectory;
                    failures += Require(
                        string.Equals(Path.GetDirectoryName(firstDirectory), Path.GetFullPath(runtimeRoot), StringComparison.OrdinalIgnoreCase),
                        "Versioned runtime is not a direct child of the requested root.",
                        report);
                    failures += RequireExactPayload(firstDirectory, report);
                    failures += RequirePublishedManifest(runtimeRoot, report);
                    failures += RequireNodeRuns(first, report);
                    failures += RequireVerifiedHandleBlocksWrites(first.NodeExecutablePath, report);
                    string relativeConfigDirectory = Path.Combine(
                        ".",
                        "build",
                        "runtime-relative-state-" + Guid.NewGuid().ToString("N"));
                    failures += RequireSanitizedLauncher(first, relativeConfigDirectory, report);

                    firstBridgeHash = HashFile(first.BridgeScriptPath);
                    string resourceHash = HashResource(EmbeddedRuntime.BridgeResourceName);
                    failures += Require(
                        string.Equals(firstBridgeHash, resourceHash, StringComparison.Ordinal),
                        "Extracted listener hash does not match the embedded resource.",
                        report);
                    report.Add("listener_sha256=" + firstBridgeHash);
                }

                string corruptBridge = Path.Combine(firstDirectory, EmbeddedRuntime.BridgeFileName);
                File.SetAttributes(corruptBridge, FileAttributes.Normal);
                File.WriteAllText(corruptBridge, "corrupted-runtime-must-never-run", new UTF8Encoding(false));
                report.Add("corruption_injected=" + corruptBridge);

                using (EmbeddedRuntimeLease repaired = EmbeddedRuntime.EnsureReadyAtRoot(runtimeRoot))
                {
                    string repairedHash = HashFile(repaired.BridgeScriptPath);
                    failures += Require(
                        string.Equals(repairedHash, firstBridgeHash, StringComparison.Ordinal),
                        "Corrupted cache was not replaced with the verified embedded listener.",
                        report);
                    failures += RequireNodeRuns(repaired, report);
                    failures += RequireExactPayload(repaired.RuntimeDirectory, report);
                    failures += RequirePublishedManifest(runtimeRoot, report);
                }

                // Cache reuse must repair the publication as well; no cache
                // timestamp is an authority for another process.
                File.WriteAllText(Path.Combine(runtimeRoot, EmbeddedRuntime.ActiveManifestFileName), "stale-publication", new UTF8Encoding(false));
                using (EmbeddedRuntimeLease republished = EmbeddedRuntime.EnsureReadyAtRoot(runtimeRoot))
                {
                    failures += RequirePublishedManifest(runtimeRoot, report);
                }
                string blockedRoot = Path.Combine(testRoot, "blocked-publication");
                Directory.CreateDirectory(blockedRoot);
                File.WriteAllText(Path.Combine(blockedRoot, Path.GetFileName(firstDirectory)), "collision", new UTF8Encoding(false));
                try
                {
                    using (EmbeddedRuntimeLease unexpected = EmbeddedRuntime.EnsureReadyAtRoot(blockedRoot)) { }
                    failures += Require(false, "Runtime cache collision unexpectedly succeeded.", report);
                }
                catch (System.Security.SecurityException)
                {
                    failures += Require(!File.Exists(Path.Combine(blockedRoot, EmbeddedRuntime.ActiveManifestFileName)),
                        "Failed extraction published a runtime.", report);
                }

                string version = Path.GetFileName(firstDirectory);
                string[] quarantines = Directory.GetDirectories(runtimeRoot, ".corrupt-" + version + "-*", SearchOption.TopDirectoryOnly);
                failures += Require(quarantines.Length == 1, "Corrupted cache was not quarantined exactly once.", report);
                if (quarantines.Length == 1)
                {
                    report.Add("quarantined_cache=" + quarantines[0]);
                }
            }
            catch (Exception exception)
            {
                failures++;
                report.Add("UNEXPECTED: " + exception);
            }

            report.Insert(0, failures == 0
                ? "PASS: embedded runtime security contract tests"
                : "FAIL: " + failures.ToString(CultureInfo.InvariantCulture) + " embedded runtime security contract tests");
            report.Add("test_root=" + testRoot);
            string text = string.Join(Environment.NewLine, report.ToArray()) + Environment.NewLine;
            Console.Write(text);
            if (!string.IsNullOrWhiteSpace(reportPath))
            {
                string directory = Path.GetDirectoryName(reportPath);
                if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
                File.WriteAllText(reportPath, text, new UTF8Encoding(false));
            }
            return failures == 0 ? 0 : 1;
        }

        private static int RequirePublishedManifest(string root, List<string> report)
        {
            string expected;
            using (Stream source = typeof(EmbeddedRuntime).Assembly.GetManifestResourceStream(EmbeddedRuntime.ManifestResourceName))
            using (StreamReader reader = new StreamReader(source, new UTF8Encoding(false, true)))
                expected = reader.ReadToEnd();
            string actual = File.ReadAllText(Path.Combine(root, EmbeddedRuntime.ActiveManifestFileName), new UTF8Encoding(false, true));
            if (actual == expected)
            {
                report.Add("PASS canonical_runtime_publication=" + root);
                return 0;
            }
            return Require(false, "Published runtime differs from verified embedded manifest.", report);
        }

        private static int RequireExactPayload(string directory, List<string> report)
        {
            string[] files = Directory.GetFiles(directory, "*", SearchOption.TopDirectoryOnly);
            string[] directories = Directory.GetDirectories(directory, "*", SearchOption.TopDirectoryOnly);
            HashSet<string> names = new HashSet<string>(StringComparer.Ordinal);
            foreach (string file in files) names.Add(Path.GetFileName(file));
            bool exact = files.Length == 3
                && directories.Length == 0
                && names.Contains(EmbeddedRuntime.NodeFileName)
                && names.Contains(EmbeddedRuntime.BridgeFileName)
                && names.Contains(EmbeddedRuntime.AuthFileName);
            if (exact)
            {
                report.Add("PASS exact_allowlisted_payload=" + directory);
                return 0;
            }
            return Require(false, "Runtime directory contains a missing or non-allowlisted entry.", report);
        }

        private static int RequireNodeRuns(EmbeddedRuntimeLease lease, List<string> report)
        {
            string version = EmbeddedRuntime.RunNodeVersion(lease);
            bool valid = version.StartsWith("v", StringComparison.Ordinal) && version.Length > 1;
            if (valid)
            {
                report.Add("PASS extracted_node_version=" + version);
                return 0;
            }
            return Require(false, "Extracted Node did not return a version.", report);
        }

        private static int RequireVerifiedHandleBlocksWrites(string nodePath, List<string> report)
        {
            try
            {
                using (FileStream unexpected = new FileStream(nodePath, FileMode.Open, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete))
                {
                }
                return Require(false, "Verified Node file remained writable while its lease was active.", report);
            }
            catch (IOException)
            {
                report.Add("PASS verified_handle_denies_write=" + nodePath);
                return 0;
            }
            catch (UnauthorizedAccessException)
            {
                report.Add("PASS verified_handle_denies_write=" + nodePath);
                return 0;
            }
        }

        private static int RequireSanitizedLauncher(EmbeddedRuntimeLease lease, string configDirectory, List<string> report)
        {
            string[] injectionNames =
            {
                "NODE_OPTIONS",
                "NODE_PATH",
                "NODE_EXTRA_CA_CERTS",
                "OPENSSL_CONF",
                "OPENSSL_MODULES",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "NODE_ICU_DATA",
                "NODE_V8_COVERAGE"
            };
            Dictionary<string, string> previous = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (string name in injectionNames)
            {
                previous[name] = Environment.GetEnvironmentVariable(name);
                Environment.SetEnvironmentVariable(name, "C:\\malicious\\" + name.ToLowerInvariant());
            }

            try
            {
                ProcessStartInfo info = LocoBridgeLauncher.CreateStartInfo(
                    lease.NodeExecutablePath,
                    lease.BridgeScriptPath,
                    configDirectory);
                foreach (string name in injectionNames)
                {
                    if (info.Environment.ContainsKey(name))
                    {
                        return Require(false, "Launcher inherited injection variable " + name + ".", report);
                    }
                }

                foreach (KeyValuePair<string, string> pair in info.Environment)
                {
                    if (!EmbeddedRuntime.IsSafeNodeEnvironmentName(pair.Key))
                    {
                        return Require(false, "Launcher kept a non-allowlisted environment variable.", report);
                    }
                }

                string expectedConfig = Path.GetFullPath(configDirectory);
                bool configMatches = string.Equals(
                    info.Environment["AGENT_MESSENGER_CONFIG_DIR"],
                    expectedConfig,
                    StringComparison.OrdinalIgnoreCase);
                if (!configMatches)
                {
                    return Require(false, "Launcher config directory was not canonicalized and allowlisted.", report);
                }
                report.Add("PASS sanitized_launcher_environment");
                return 0;
            }
            finally
            {
                foreach (KeyValuePair<string, string> pair in previous)
                {
                    Environment.SetEnvironmentVariable(pair.Key, pair.Value);
                }
            }
        }

        private static string HashFile(string path)
        {
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            using (SHA256 sha256 = SHA256.Create())
            {
                return ToHex(sha256.ComputeHash(stream));
            }
        }

        private static string HashResource(string resourceName)
        {
            Assembly assembly = typeof(EmbeddedRuntime).Assembly;
            using (Stream stream = assembly.GetManifestResourceStream(resourceName))
            using (SHA256 sha256 = SHA256.Create())
            {
                if (stream == null) throw new InvalidDataException("Missing embedded test resource: " + resourceName);
                return ToHex(sha256.ComputeHash(stream));
            }
        }

        private static string ToHex(byte[] bytes)
        {
            StringBuilder result = new StringBuilder(bytes.Length * 2);
            foreach (byte value in bytes) result.Append(value.ToString("x2", CultureInfo.InvariantCulture));
            return result.ToString();
        }

        private static int Require(bool condition, string message, List<string> report)
        {
            if (condition)
            {
                return 0;
            }
            report.Add("FAIL " + message);
            return 1;
        }
    }
}
