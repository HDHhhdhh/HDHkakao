using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows;
using System.Windows.Automation;
using Accessibility;

namespace Spoiler
{
    internal enum KakaoNotificationSettingState
    {
        Unknown,
        Enabled,
        Disabled
    }

    public sealed class NotificationCaptureService : IDisposable
    {
        private const uint EventObjectShow = 0x8002;
        private const uint EventObjectNameChange = 0x800C;
        private const uint WineventOutOfContext = 0x0000;
        private const uint WineventSkipOwnProcess = 0x0002;
        private const uint GetAncestorRoot = 2;

        private readonly bool _qaMode;
        private readonly object _sync = new object();
        private readonly Dictionary<string, DateTime> _recent = new Dictionary<string, DateTime>();
        private readonly Dictionary<int, ProcessDecision> _processCache = new Dictionary<int, ProcessDecision>();
        private readonly Dictionary<IntPtr, List<AccessibleFragment>> _accessibleFragments = new Dictionary<IntPtr, List<AccessibleFragment>>();
        private readonly object _qaTraceSync = new object();
        private readonly string _qaTracePath;
        private readonly WinEventDelegate _winEventDelegate;
        private AutomationEventHandler _automationHandler;
        private Timer _scanTimer;
        private IntPtr _winEventHook;
        private bool _disposed;
        private volatile bool _captureAllowedBySettings;
        private CaptureState _lastState = CaptureState.Starting;

        public event EventHandler<NotificationPreview> PreviewCaptured;
        public event EventHandler<CaptureStateChangedEventArgs> StateChanged;

        public NotificationCaptureService(bool qaMode)
        {
            _qaMode = qaMode;
            _qaTracePath = Environment.GetEnvironmentVariable("SPOILER_CAPTURE_LOG");
            _captureAllowedBySettings = qaMode || GetKakaoNotificationSettingState() != KakaoNotificationSettingState.Disabled;
            _winEventDelegate = OnWinEvent;
        }

        public void Start()
        {
            if (_disposed)
            {
                throw new ObjectDisposedException("NotificationCaptureService");
            }

            RaiseState(CaptureState.Starting, "알림 감시를 준비하고 있어요");

            try
            {
                _winEventHook = SetWinEventHook(
                    EventObjectShow,
                    EventObjectNameChange,
                    IntPtr.Zero,
                    _winEventDelegate,
                    0,
                    0,
                    WineventOutOfContext | WineventSkipOwnProcess);

                _automationHandler = OnAutomationWindowOpened;
                Automation.AddAutomationEventHandler(
                    WindowPattern.WindowOpenedEvent,
                    AutomationElement.RootElement,
                    TreeScope.Subtree,
                    _automationHandler);
            }
            catch
            {
                // The periodic scan remains available if an event hook is unavailable.
            }

            _scanTimer = new Timer(delegate { ScanNow(); }, null, 120, 1100);
            QaTrace("watcher-started hook=" + (_winEventHook != IntPtr.Zero));
        }

        public void ScanNow()
        {
            if (_disposed)
            {
                return;
            }

            PruneAccessibleFragments(DateTime.Now);

            bool kakaoRunning = IsKakaoTalkRunning();
            if (!kakaoRunning && !_qaMode)
            {
                RaiseState(CaptureState.KakaoTalkNotRunning, "카카오톡을 실행하면 자동으로 감시해요");
                return;
            }

            KakaoNotificationSettingState notificationSetting = _qaMode
                ? KakaoNotificationSettingState.Enabled
                : GetKakaoNotificationSettingState();
            _captureAllowedBySettings = notificationSetting != KakaoNotificationSettingState.Disabled;
            if (!_captureAllowedBySettings)
            {
                QaTrace("capture-disabled notification-setting=no");
                RaiseState(CaptureState.NotificationsDisabled, "카카오톡 메시지 알림을 켜야 미리보기를 읽을 수 있어요");
                return;
            }

            RaiseState(CaptureState.Watching, "알림 팝업을 조용히 기다리는 중");

            try
            {
                AutomationElementCollection windows = AutomationElement.RootElement.FindAll(
                    TreeScope.Children,
                    System.Windows.Automation.Condition.TrueCondition);
                QaTrace("scan-window-count=" + windows.Count);

                for (int index = 0; index < windows.Count; index++)
                {
                    TryCaptureElement(windows[index], IntPtr.Zero, null);
                }
            }
            catch (ElementNotAvailableException)
            {
                // A toast disappeared between enumeration and inspection.
            }
            catch
            {
                RaiseState(CaptureState.Error, "알림 접근을 잠시 다시 시도하고 있어요");
            }
        }

        private void OnWinEvent(
            IntPtr hook,
            uint eventType,
            IntPtr windowHandle,
            int objectId,
            int childId,
            uint eventThread,
            uint eventTime)
        {
            if (_disposed || windowHandle == IntPtr.Zero || !_captureAllowedBySettings)
            {
                return;
            }

            IntPtr rootWindowHandle = NormalizeToRootWindow(windowHandle);
            if (rootWindowHandle == IntPtr.Zero)
            {
                return;
            }

            int processId;
            GetWindowThreadProcessId(rootWindowHandle, out processId);
            if (!IsAllowedProcess(processId, rootWindowHandle))
            {
                return;
            }

            CacheAccessibleFragments(rootWindowHandle, CollectAccessibleEventText(windowHandle, objectId, childId));

            ThreadPool.QueueUserWorkItem(delegate
            {
                Thread.Sleep(90);
                try
                {
                    AutomationElement element = AutomationElement.FromHandle(rootWindowHandle);
                    TryCaptureElement(element, rootWindowHandle, GetAccessibleFragments(rootWindowHandle));
                }
                catch
                {
                    // The toast can legitimately disappear during the debounce window.
                }
            });
        }

        private void OnAutomationWindowOpened(object sender, AutomationEventArgs args)
        {
            AutomationElement element = sender as AutomationElement;
            if (element == null || _disposed || !_captureAllowedBySettings)
            {
                return;
            }

            ThreadPool.QueueUserWorkItem(delegate
            {
                Thread.Sleep(90);
                TryCaptureElement(element, IntPtr.Zero, null);
            });
        }

        private void TryCaptureElement(AutomationElement element, IntPtr hintedHandle, IEnumerable<string> eventParts)
        {
            if (element == null || _disposed)
            {
                return;
            }

            try
            {
                IntPtr handle = hintedHandle;
                if (handle == IntPtr.Zero)
                {
                    handle = new IntPtr(element.Current.NativeWindowHandle);
                }

                handle = NormalizeToRootWindow(handle);
                if (handle == IntPtr.Zero)
                {
                    return;
                }

                if (new IntPtr(element.Current.NativeWindowHandle) != handle)
                {
                    element = AutomationElement.FromHandle(handle);
                }

                int processId = element.Current.ProcessId;

                if (!IsAllowedProcess(processId, handle))
                {
                    return;
                }

                QaTrace("candidate-allowed pid=" + processId + " handle=" + handle + " class=" + GetWindowClass(handle));

                Rect rectangle = element.Current.BoundingRectangle;
                if (!IsPopupSized(rectangle, handle))
                {
                    QaTrace("candidate-size-rejected " + rectangle.Width + "x" + rectangle.Height);
                    return;
                }

                IList<string> parts = CollectReadableText(element, eventParts);
                QaTrace("candidate-parts=" + string.Join(" || ", parts.ToArray()));
                string sender;
                string message;
                if (!TryParseParts(parts, out sender, out message))
                {
                    QaTrace("candidate-parse-rejected");
                    return;
                }

                string signature = ComputeSignature(sender + "\n" + message);
                lock (_sync)
                {
                    DateTime seenAt;
                    if (_recent.TryGetValue(signature, out seenAt) && DateTime.Now - seenAt < TimeSpan.FromSeconds(30))
                    {
                        return;
                    }

                    _recent[signature] = DateTime.Now;
                    DateTime cutoff = DateTime.Now.AddMinutes(-5);
                    foreach (string key in _recent.Where(pair => pair.Value < cutoff).Select(pair => pair.Key).ToList())
                    {
                        _recent.Remove(key);
                    }
                }

                NotificationPreview preview = new NotificationPreview
                {
                    Sender = sender,
                    Message = message,
                    ReceivedAt = DateTime.Now,
                    Source = "KakaoTalk popup"
                };

                EventHandler<NotificationPreview> handler = PreviewCaptured;
                if (handler != null)
                {
                    handler(this, preview);
                }

                QaTrace("captured=" + sender + "|" + message);

                RaiseState(CaptureState.Captured, "새 알림을 읽었어요 · 읽음 신호는 보내지 않았어요");
            }
            catch (ElementNotAvailableException)
            {
                // Toasts are short-lived by design.
            }
            catch
            {
                // UI Automation providers vary between KakaoTalk releases; keep the watcher alive.
            }
        }

        private bool IsAllowedProcess(int processId, IntPtr windowHandle)
        {
            if (processId <= 0)
            {
                return false;
            }

            if (_qaMode && windowHandle != IntPtr.Zero)
            {
                StringBuilder title = new StringBuilder(256);
                GetWindowText(windowHandle, title, title.Capacity);
                if (title.ToString().IndexOf("Spoiler Kakao QA Toast", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    return true;
                }
            }

            lock (_sync)
            {
                ProcessDecision cached;
                if (_processCache.TryGetValue(processId, out cached) && cached.ExpiresAt > DateTime.Now)
                {
                    return cached.Allowed;
                }

                _processCache.Remove(processId);
            }

            bool allowed = false;
            try
            {
                using (Process process = Process.GetProcessById(processId))
                {
                    if (_qaMode && process.ProcessName.StartsWith("KakaoToastSim", StringComparison.OrdinalIgnoreCase))
                    {
                        QaTrace("qa-process-allowed=" + process.ProcessName);
                        return true;
                    }

                    if (string.Equals(process.ProcessName, "KakaoTalk", StringComparison.OrdinalIgnoreCase))
                    {
                        string path = process.MainModule.FileName;
                        allowed = IsTrustedKakaoExecutablePath(path);
                    }
                }
            }
            catch
            {
                allowed = false;
            }

            lock (_sync)
            {
                _processCache[processId] = new ProcessDecision
                {
                    Allowed = allowed,
                    ExpiresAt = DateTime.Now.AddSeconds(allowed ? 5 : 1)
                };
            }

            return allowed;
        }

        internal static bool IsTrustedKakaoExecutablePath(string path)
        {
            if (string.IsNullOrWhiteSpace(path))
            {
                return false;
            }

            try
            {
                string programFilesX86 = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86);
                if (string.IsNullOrWhiteSpace(programFilesX86))
                {
                    return false;
                }

                string expectedPath = Path.Combine(programFilesX86, "Kakao", "KakaoTalk", "KakaoTalk.exe");
                return string.Equals(Path.GetFullPath(path), Path.GetFullPath(expectedPath), StringComparison.OrdinalIgnoreCase);
            }
            catch
            {
                return false;
            }
        }

        internal static IntPtr NormalizeToRootWindow(IntPtr windowHandle)
        {
            if (windowHandle == IntPtr.Zero || !IsWindow(windowHandle))
            {
                return IntPtr.Zero;
            }

            return GetAncestor(windowHandle, GetAncestorRoot);
        }

        internal static KakaoNotificationSettingState ParseKakaoNotificationSetting(IEnumerable<string> lines)
        {
            if (lines == null)
            {
                return KakaoNotificationSettingState.Unknown;
            }

            foreach (string line in lines)
            {
                if (line == null)
                {
                    continue;
                }

                Match match = Regex.Match(
                    line,
                    "^\\s*use_msg_recv_alarm\\s*=\\s*(yes|no)\\s*$",
                    RegexOptions.IgnoreCase);
                if (match.Success)
                {
                    return string.Equals(match.Groups[1].Value, "yes", StringComparison.OrdinalIgnoreCase)
                        ? KakaoNotificationSettingState.Enabled
                        : KakaoNotificationSettingState.Disabled;
                }
            }

            return KakaoNotificationSettingState.Unknown;
        }

        private static KakaoNotificationSettingState GetKakaoNotificationSettingState()
        {
            try
            {
                string usersDirectory = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "Kakao",
                    "KakaoTalk",
                    "users");
                if (!Directory.Exists(usersDirectory))
                {
                    return KakaoNotificationSettingState.Unknown;
                }

                IEnumerable<string> preferences = Directory.GetDirectories(usersDirectory)
                    .Select(directory => Path.Combine(directory, "user_pref.ini"))
                    .Where(File.Exists)
                    .OrderByDescending(File.GetLastWriteTimeUtc);

                foreach (string preferencePath in preferences)
                {
                    KakaoNotificationSettingState state = ParseKakaoNotificationSetting(File.ReadLines(preferencePath));
                    if (state != KakaoNotificationSettingState.Unknown)
                    {
                        return state;
                    }
                }
            }
            catch
            {
            }

            return KakaoNotificationSettingState.Unknown;
        }

        private bool IsKakaoTalkRunning()
        {
            try
            {
                Process[] processes = Process.GetProcessesByName("KakaoTalk");
                foreach (Process process in processes)
                {
                    try
                    {
                        if (IsAllowedProcess(process.Id, IntPtr.Zero))
                        {
                            return true;
                        }
                    }
                    finally
                    {
                        process.Dispose();
                    }
                }
            }
            catch
            {
                return false;
            }

            return false;
        }

        private void QaTrace(string message)
        {
            if (string.IsNullOrWhiteSpace(_qaTracePath))
            {
                return;
            }

            try
            {
                lock (_qaTraceSync)
                {
                    string directory = Path.GetDirectoryName(_qaTracePath);
                    if (!string.IsNullOrEmpty(directory))
                    {
                        Directory.CreateDirectory(directory);
                    }
                    File.AppendAllText(_qaTracePath, DateTime.Now.ToString("O") + "  " + message + Environment.NewLine);
                }
            }
            catch
            {
            }
        }

        private bool IsPopupSized(Rect rectangle, IntPtr windowHandle)
        {
            if (rectangle.IsEmpty || rectangle.Width <= 0 || rectangle.Height <= 0)
            {
                return false;
            }

            double scale = 1;
            if (windowHandle != IntPtr.Zero)
            {
                try
                {
                    uint dpi = GetDpiForWindow(windowHandle);
                    if (dpi <= 96)
                    {
                        uint systemDpi = GetDpiForSystem();
                        if (systemDpi > dpi)
                        {
                            dpi = systemDpi;
                        }
                    }
                    if (dpi >= 96 && dpi <= 768)
                    {
                        scale = dpi / 96.0;
                    }
                }
                catch
                {
                }
            }

            double logicalWidth = rectangle.Width / scale;
            double logicalHeight = rectangle.Height / scale;
            return logicalWidth >= 180
                && logicalWidth <= 680
                && logicalHeight >= 45
                && logicalHeight <= 360;
        }

        private static IList<string> CollectReadableText(AutomationElement root, IEnumerable<string> eventParts)
        {
            List<string> parts = new List<string>();
            try
            {
                AutomationElementCollection descendants = root.FindAll(TreeScope.Descendants, System.Windows.Automation.Condition.TrueCondition);
                int limit = Math.Min(descendants.Count, 120);
                for (int index = 0; index < limit; index++)
                {
                        AutomationElement child = descendants[index];
                        try
                        {
                            ControlType type = child.Current.ControlType;
                            if (type == ControlType.Text || type == ControlType.Document || type == ControlType.ListItem)
                            {
                                AddTextParts(parts, child.Current.Name);
                            }
                        }
                    catch (ElementNotAvailableException)
                    {
                    }
                }
            }
            catch
            {
            }

            if (parts.Count < 2)
            {
                try
                {
                    AddTextParts(parts, root.Current.Name);
                }
                catch
                {
                }
            }

            if (parts.Count < 2 && eventParts != null)
            {
                foreach (string part in eventParts)
                {
                    AddTextParts(parts, part);
                }
            }

            return parts.Distinct(StringComparer.Ordinal).ToList();
        }

        private static IList<string> CollectAccessibleEventText(IntPtr windowHandle, int objectId, int childId)
        {
            List<string> values = new List<string>();
            IAccessible accessible = null;
            object child = null;
            try
            {
                int result = AccessibleObjectFromEvent(windowHandle, objectId, childId, out accessible, out child);
                if (result < 0 || accessible == null)
                {
                    return values;
                }

                try
                {
                    object role = accessible.get_accRole(child);
                    int roleCode = role == null ? 0 : Convert.ToInt32(role);
                    if (IsInteractiveAccessibleRole(roleCode))
                    {
                        return values;
                    }
                }
                catch
                {
                }

                try { AddTextParts(values, accessible.get_accName(child)); } catch { }
                try { AddTextParts(values, accessible.get_accValue(child)); } catch { }
                try { AddTextParts(values, accessible.get_accDescription(child)); } catch { }
            }
            catch
            {
            }
            finally
            {
                if (accessible != null && Marshal.IsComObject(accessible))
                {
                    try { Marshal.FinalReleaseComObject(accessible); } catch { }
                }
            }

            return values;
        }

        private void CacheAccessibleFragments(IntPtr windowHandle, IEnumerable<string> values)
        {
            if (windowHandle == IntPtr.Zero || values == null)
            {
                return;
            }

            DateTime now = DateTime.Now;
            PruneAccessibleFragments(now);
            lock (_sync)
            {
                List<AccessibleFragment> fragments;
                if (!_accessibleFragments.TryGetValue(windowHandle, out fragments))
                {
                    fragments = new List<AccessibleFragment>();
                    _accessibleFragments[windowHandle] = fragments;
                }

                foreach (string value in values)
                {
                    if (!string.IsNullOrWhiteSpace(value))
                    {
                        fragments.Add(new AccessibleFragment { Value = value, SeenAt = now });
                    }
                }
            }
        }

        private IList<string> GetAccessibleFragments(IntPtr windowHandle)
        {
            if (windowHandle == IntPtr.Zero)
            {
                return new List<string>();
            }

            DateTime cutoff = DateTime.Now.AddSeconds(-3);
            lock (_sync)
            {
                List<AccessibleFragment> fragments;
                if (!_accessibleFragments.TryGetValue(windowHandle, out fragments))
                {
                    return new List<string>();
                }

                fragments.RemoveAll(fragment => fragment.SeenAt < cutoff);
                if (fragments.Count == 0)
                {
                    _accessibleFragments.Remove(windowHandle);
                    return new List<string>();
                }

                return fragments.Select(fragment => fragment.Value).Distinct(StringComparer.Ordinal).ToList();
            }
        }

        private void PruneAccessibleFragments(DateTime now)
        {
            DateTime cutoff = now.AddSeconds(-3);
            lock (_sync)
            {
                foreach (IntPtr handle in _accessibleFragments.Keys.ToList())
                {
                    List<AccessibleFragment> fragments = _accessibleFragments[handle];
                    fragments.RemoveAll(fragment => fragment.SeenAt < cutoff);
                    if (fragments.Count == 0)
                    {
                        _accessibleFragments.Remove(handle);
                    }
                }
            }
        }

        internal static bool IsInteractiveAccessibleRole(int roleCode)
        {
            return roleCode == 0x02  // menu bar
                || roleCode == 0x0B // menu popup
                || roleCode == 0x0C // menu item
                || roleCode == 0x1E // link
                || roleCode == 0x2B // push button
                || roleCode == 0x2C // check button
                || roleCode == 0x2D // radio button
                || roleCode == 0x2E // combo box
                || roleCode == 0x2F // drop list
                || roleCode == 0x34; // spin button
        }

        private static void AddTextParts(ICollection<string> parts, string raw)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return;
            }

            string[] lines = raw.Replace("\r", "\n").Split(new[] { '\n' }, StringSplitOptions.RemoveEmptyEntries);
            foreach (string line in lines)
            {
                string value = Regex.Replace(line, "\\s+", " ").Trim();
                if (value.Length > 0 && value.Length <= 400)
                {
                    parts.Add(value);
                }
            }
        }

        public static bool TryParseParts(IEnumerable<string> rawParts, out string sender, out string message)
        {
            sender = null;
            message = null;

            List<string> normalizedParts = rawParts
                .Where(value => !string.IsNullOrWhiteSpace(value))
                .Select(value => Regex.Replace(value, "\\s+", " ").Trim())
                .Distinct(StringComparer.Ordinal)
                .ToList();

            int kakaoHeaderIndex = normalizedParts.FindIndex(IsKakaoHeader);
            if (kakaoHeaderIndex == normalizedParts.Count - 1 && normalizedParts.Count > 2)
            {
                normalizedParts.Reverse();
            }

            List<string> parts = normalizedParts
                .Where(value => !IsBoilerplate(value))
                .ToList();

            if (parts.Count == 1)
            {
                return TrySplitCombinedPart(parts[0], out sender, out message);
            }

            for (int index = parts.Count - 2; index >= 0; index--)
            {
                string adjacentSender = parts[index];
                string adjacentMessage = parts[index + 1];
                if (!LooksLikeTime(adjacentSender) && !LooksLikeTime(adjacentMessage) && IsPlausible(adjacentSender, adjacentMessage))
                {
                    sender = adjacentSender;
                    message = adjacentMessage;
                    return true;
                }
            }

            for (int index = 0; index < parts.Count - 1; index++)
            {
                string candidateSender = parts[index];
                if (candidateSender.Length > 40 || LooksLikeTime(candidateSender))
                {
                    continue;
                }

                for (int bodyIndex = index + 1; bodyIndex < parts.Count; bodyIndex++)
                {
                    string candidateMessage = parts[bodyIndex];
                    if (LooksLikeTime(candidateMessage) || candidateMessage == candidateSender)
                    {
                        continue;
                    }

                    if (IsPlausible(candidateSender, candidateMessage))
                    {
                        sender = candidateSender;
                        message = candidateMessage;
                        return true;
                    }
                }
            }

            return false;
        }

        private static bool IsKakaoHeader(string value)
        {
            string normalized = (value ?? string.Empty).Trim().ToLowerInvariant();
            return normalized == "카카오톡" || normalized == "kakaotalk" || normalized == "kakao talk";
        }

        private static bool TrySplitCombinedPart(string value, out string sender, out string message)
        {
            sender = null;
            message = null;

            Match split = Regex.Match(value, "^(.{1,40}?)[\\s]*[:：][\\s]*(.{1,})$");
            if (!split.Success)
            {
                split = Regex.Match(value, "^(.{1,40}?)[\\s]+[-–][\\s]+(.{1,})$");
            }

            if (!split.Success)
            {
                return false;
            }

            sender = split.Groups[1].Value.Trim();
            message = split.Groups[2].Value.Trim();
            return IsPlausible(sender, message);
        }

        private static bool IsPlausible(string sender, string message)
        {
            return !string.IsNullOrWhiteSpace(sender)
                && sender.Length <= 40
                && !string.IsNullOrWhiteSpace(message)
                && message.Length <= 400
                && !IsBoilerplate(sender)
                && !IsBoilerplate(message)
                && !IsKnownNonMessagePair(sender, message);
        }

        internal static bool IsKnownNonMessagePair(string sender, string message)
        {
            string normalizedSender = Regex.Replace(sender ?? string.Empty, "\\s+", " ").Trim().ToLowerInvariant();
            string normalizedMessage = Regex.Replace(message ?? string.Empty, "\\s+", " ").Trim().ToLowerInvariant();

            return (normalizedSender == "편집" && normalizedMessage == "메시지 입력")
                || (normalizedSender == "moment 광고" && normalizedMessage == "광고 링크");
        }

        private static bool IsBoilerplate(string value)
        {
            string normalized = value.Trim().ToLowerInvariant();
            string[] exact =
            {
                "kakaotalk", "카카오톡", "kakao talk", "새 알림", "new notification",
                "닫기", "close", "답장", "reply", "더 보기", "알림 설정", "notification settings",
                "읽음으로 표시", "mark as read", "열기", "open", "메시지 열기", "open message",
                "링크 열기", "open link", "사진 보기", "view photo", "동영상 보기", "view video",
                "최소화", "최대화", "복원", "maximize", "minimize", "restore", "spoiler kakao qa toast",
                "시스템", "시스템 메뉴 모음", "system", "system menu bar"
            };

            if (exact.Contains(normalized))
            {
                return true;
            }

            return LooksLikeTime(normalized)
                || normalized.StartsWith("알림 끄기", StringComparison.Ordinal)
                || normalized.StartsWith("notification from", StringComparison.Ordinal);
        }

        private static bool LooksLikeTime(string value)
        {
            return Regex.IsMatch(value.Trim(), "^(오전|오후|am|pm)?[\\s]*\\d{1,2}:\\d{2}$", RegexOptions.IgnoreCase);
        }

        private static string ComputeSignature(string value)
        {
            using (SHA256 sha = SHA256.Create())
            {
                return Convert.ToBase64String(sha.ComputeHash(Encoding.UTF8.GetBytes(value)));
            }
        }

        private static string GetWindowClass(IntPtr windowHandle)
        {
            StringBuilder value = new StringBuilder(256);
            return GetClassName(windowHandle, value, value.Capacity) > 0 ? value.ToString() : string.Empty;
        }

        private void RaiseState(CaptureState state, string detail)
        {
            lock (_sync)
            {
                if (_lastState == state && state != CaptureState.Captured)
                {
                    return;
                }

                _lastState = state;
            }

            EventHandler<CaptureStateChangedEventArgs> handler = StateChanged;
            if (handler != null)
            {
                handler(this, new CaptureStateChangedEventArgs(state, detail));
            }
        }

        public void Dispose()
        {
            if (_disposed)
            {
                return;
            }

            _disposed = true;
            if (_scanTimer != null)
            {
                _scanTimer.Dispose();
                _scanTimer = null;
            }

            if (_automationHandler != null)
            {
                try
                {
                    Automation.RemoveAutomationEventHandler(
                        WindowPattern.WindowOpenedEvent,
                        AutomationElement.RootElement,
                        _automationHandler);
                }
                catch
                {
                }
            }

            if (_winEventHook != IntPtr.Zero)
            {
                UnhookWinEvent(_winEventHook);
                _winEventHook = IntPtr.Zero;
            }
        }

        private delegate void WinEventDelegate(
            IntPtr hook,
            uint eventType,
            IntPtr windowHandle,
            int objectId,
            int childId,
            uint eventThread,
            uint eventTime);

        private sealed class AccessibleFragment
        {
            public string Value { get; set; }
            public DateTime SeenAt { get; set; }
        }

        private sealed class ProcessDecision
        {
            public bool Allowed { get; set; }
            public DateTime ExpiresAt { get; set; }
        }

        [DllImport("user32.dll")]
        private static extern IntPtr SetWinEventHook(
            uint eventMin,
            uint eventMax,
            IntPtr eventHookModule,
            WinEventDelegate winEventProc,
            uint processId,
            uint threadId,
            uint flags);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool UnhookWinEvent(IntPtr hook);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr windowHandle, out int processId);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool IsWindow(IntPtr windowHandle);

        [DllImport("user32.dll")]
        private static extern IntPtr GetAncestor(IntPtr windowHandle, uint flags);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetWindowText(IntPtr windowHandle, StringBuilder value, int maximumCount);

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetClassName(IntPtr windowHandle, StringBuilder value, int maximumCount);

        [DllImport("user32.dll")]
        private static extern uint GetDpiForWindow(IntPtr windowHandle);

        [DllImport("user32.dll")]
        private static extern uint GetDpiForSystem();

        [DllImport("oleacc.dll")]
        private static extern int AccessibleObjectFromEvent(
            IntPtr windowHandle,
            int objectId,
            int childId,
            [MarshalAs(UnmanagedType.Interface)] out IAccessible accessible,
            [MarshalAs(UnmanagedType.Struct)] out object child);
    }
}
