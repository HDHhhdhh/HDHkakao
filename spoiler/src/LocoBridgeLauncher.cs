using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;

namespace Spoiler
{
    internal static class LocoBridgeLauncher
    {
        public static ProcessStartInfo CreateStartInfo(string nodeExecutable, string bridgeScriptPath, string configDirectory)
        {
            string safeConfigDirectory = null;
            if (!string.IsNullOrWhiteSpace(configDirectory))
            {
                safeConfigDirectory = EmbeddedRuntime.PreparePrivateDataDirectory(configDirectory);
            }

            ProcessStartInfo info = new ProcessStartInfo
            {
                FileName = nodeExecutable,
                Arguments = Quote(bridgeScriptPath),
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                WorkingDirectory = Path.GetDirectoryName(bridgeScriptPath) ?? AppDomain.CurrentDomain.BaseDirectory
            };

            // Do not inherit Node/OpenSSL module and option injection knobs
            // from the desktop environment.  EmbeddedRuntime starts from a
            // small OS-only allowlist so future Node variables are denied by
            // default as well.
            EmbeddedRuntime.SanitizeNodeEnvironment(info);

            if (!string.IsNullOrWhiteSpace(safeConfigDirectory))
            {
                info.Environment["AGENT_MESSENGER_CONFIG_DIR"] = safeConfigDirectory;
            }
            return info;
        }

        public static ProcessStartInfo CreateStartInfo(string nodeExecutable, string bridgeScriptPath)
        {
            return CreateStartInfo(nodeExecutable, bridgeScriptPath, null);
        }

        public static string CreateRegistrationRequest(string email, string secret, string deviceUuid, bool force)
        {
            StringBuilder json = new StringBuilder();
            json.Append("{\"type\":\"register\",\"action\":\"register\",\"deviceType\":\"tablet\",\"force\":");
            json.Append(force ? "true" : "false");
            json.Append(",\"email\":\"").Append(Escape(email ?? string.Empty));
            json.Append("\",\"password\":\"").Append(Escape(secret ?? string.Empty));
            json.Append("\",\"deviceUuid\":\"").Append(Escape(deviceUuid ?? string.Empty));
            json.Append("\"}");
            return json.ToString();
        }

        public static string CreateCredentialSaveAck(bool saved)
        {
            return saved
                ? "{\"type\":\"credentials_saved\"}"
                : "{\"type\":\"credentials_save_failed\"}";
        }

        public static string CreateAuthLeaseReply(string type, string requestId, bool ok,
            string leaseId, LocoCredentials credentials, string errorCode)
        {
            string result = "{\"type\":\"" + Escape(type) + "\",\"request_id\":\"" + Escape(requestId ?? "")
                + "\",\"ok\":" + (ok ? "true" : "false") + ",\"auth_lease_id\":\"" + Escape(leaseId ?? "") + "\"";
            if (credentials != null)
                result += ",\"credentials\":" + Serialize(new LocoStartRequest { Credentials = credentials });
            if (!string.IsNullOrEmpty(errorCode)) result += ",\"error_code\":\"" + Escape(errorCode) + "\"";
            return result + "}";
        }

        public static void WriteUtf8Line(Process process, string json)
        {
            if (process == null || process.StandardInput == null || json == null)
            {
                return;
            }

            byte[] bytes = Encoding.UTF8.GetBytes(json + "\n");
            Stream stream = process.StandardInput.BaseStream;
            stream.Write(bytes, 0, bytes.Length);
            stream.Flush();
        }

        public static string Serialize(LocoStartRequest request)
        {
            if (request == null || request.Credentials == null)
            {
                return "{\"type\":\"start\",\"deviceType\":\"tablet\"}";
            }

            // The bridge accepts this small stable envelope.  Escaping is
            // implemented here so the C# code does not need a third-party
            // JSON dependency merely to write a credential line to stdin.
            LocoCredentials credentials = request.Credentials;
            StringBuilder json = new StringBuilder();
            json.Append("{\"type\":\"start\",\"deviceType\":\"tablet\"");
            json.Append(',');
            bool comma = false;
            AppendJsonString(json, "oauthToken", FirstNonEmpty(credentials.OAuthToken, credentials.AccessToken), ref comma);
            AppendJsonString(json, "refreshToken", credentials.RefreshToken, ref comma);
            AppendJsonString(json, "userId", credentials.UserId, ref comma);
            AppendJsonString(json, "deviceUuid", credentials.DeviceUuid, ref comma);
            AppendJsonString(json, "deviceType", CredentialVault.TabletDeviceType, ref comma);
            AppendCatchUp(json, request.CatchUp, ref comma);
            json.Append('}');
            return json.ToString();
        }

        private static void AppendCatchUp(
            StringBuilder json,
            IList<LocoCatchUpCursor> cursors,
            ref bool comma)
        {
            if (cursors == null || cursors.Count == 0)
            {
                return;
            }

            if (comma) json.Append(',');
            json.Append("\"catchUp\":[");
            int written = 0;
            for (int index = 0; index < cursors.Count && written < 512; index++)
            {
                LocoCatchUpCursor cursor = cursors[index];
                if (cursor == null || string.IsNullOrWhiteSpace(cursor.ChatId)
                    || string.IsNullOrWhiteSpace(cursor.AfterLogId))
                {
                    continue;
                }
                if (written > 0) json.Append(',');
                json.Append("{\"chatId\":\"").Append(Escape(cursor.ChatId.Trim()));
                json.Append("\",\"afterLogId\":\"").Append(Escape(cursor.AfterLogId.Trim()));
                json.Append("\",\"limit\":");
                int limit = cursor.Limit < 1 ? 50 : Math.Min(100, cursor.Limit);
                json.Append(limit.ToString(CultureInfo.InvariantCulture));
                json.Append('}');
                written++;
            }
            json.Append(']');
            comma = true;
        }

        private static string FirstNonEmpty(string first, string second)
        {
            return !string.IsNullOrWhiteSpace(first) ? first : second;
        }

        private static void AppendJsonString(StringBuilder json, string name, string value, ref bool comma)
        {
            if (string.IsNullOrWhiteSpace(value))
            {
                return;
            }

            if (comma) json.Append(',');
            json.Append('"').Append(Escape(name)).Append("\":\"").Append(Escape(value)).Append('"');
            comma = true;
        }

        private static string Escape(string value)
        {
            StringBuilder escaped = new StringBuilder(value == null ? 0 : value.Length + 8);
            if (value == null) return string.Empty;
            foreach (char character in value)
            {
                switch (character)
                {
                    case '"': escaped.Append("\\\""); break;
                    case '\\': escaped.Append("\\\\"); break;
                    case '\b': escaped.Append("\\b"); break;
                    case '\f': escaped.Append("\\f"); break;
                    case '\n': escaped.Append("\\n"); break;
                    case '\r': escaped.Append("\\r"); break;
                    case '\t': escaped.Append("\\t"); break;
                    default:
                        if (character < 0x20)
                        {
                            escaped.Append("\\u").Append(((int)character).ToString("x4", CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            escaped.Append(character);
                        }
                        break;
                }
            }
            return escaped.ToString();
        }

        private static string Quote(string value)
        {
            return "\"" + (value ?? string.Empty).Replace("\"", "\\\"") + "\"";
        }
    }

    internal static class LocoBridgeLocator
    {
        public static string FindNodeExecutable()
        {
            return EmbeddedRuntime.EnsureReady().NodeExecutablePath;
        }

        public static string FindBridgeScript()
        {
            return EmbeddedRuntime.EnsureReady().BridgeScriptPath;
        }

        public static string FindAuthScript()
        {
            return EmbeddedRuntime.EnsureReady().AuthScriptPath;
        }
    }

}
