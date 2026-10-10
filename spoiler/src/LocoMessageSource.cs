using System;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading;

namespace Spoiler
{
    /// <summary>
    /// Monotonic authorization token for listener lifecycle work. Reconnect
    /// callbacks may only act on the generation that originally scheduled
    /// them; Stop() invalidates every outstanding token atomically.
    /// </summary>
    internal sealed class LocoLifecycleGate
    {
        public long Generation { get; private set; }
        public bool DesiredRunning { get; private set; }

        public long RequestStart()
        {
            DesiredRunning = true;
            Generation++;
            return Generation;
        }

        public long RequestStop()
        {
            DesiredRunning = false;
            Generation++;
            return Generation;
        }

        public bool Allows(long generation)
        {
            return DesiredRunning && generation == Generation;
        }
    }

    /// <summary>
    /// Reads the read-only NDJSON stream produced by the bundled KakaoTalk
    /// tablet companion bridge.  The bridge owns the network protocol; this
    /// class only starts it, supplies an already-issued token bundle, and
    /// consumes events. Read paths never advance a read watermark. Explicit
    /// user-triggered replies use a separate, one-write capability.
    /// </summary>
    public sealed partial class LocoMessageSource : IMessageSource, IReplyMessageSource
    {
        private readonly object _sync = new object();
        private readonly CredentialVault _vault;
        private readonly string _nodeExecutable;
        private readonly string _bridgeScriptPath;
        private readonly LocoLifecycleGate _lifecycle = new LocoLifecycleGate();
        private readonly HashSet<string> _catchUpProtectedChats = new HashSet<string>(StringComparer.Ordinal);

        private InboxStore _inbox;
        private string _inboxAccountId;
        private Timer _maintenanceTimer;
        private DateTime _lastBridgeActivityUtc;
        private DateTime _lastPruneUtc;
        private bool _storageBlocked;
        private string _lastDetail;
        private Process _process;
        private string _processAccountId;
        private string _processCredentialRevision;
        private bool _credentialConflict;
        private IDisposable _authLease;
        private string _authLeaseId;
        private bool _authLeasePending;
        private CancellationTokenSource _authLeaseCancellation = new CancellationTokenSource();
        private Timer _authLeaseWatchdog;
        private Thread _stdoutThread;
        private Thread _stderrThread;
        private Timer _reconnectTimer;
        private int _reconnectAttempt;
        private bool _starting;
        private long _startingGeneration;
        private bool _stopping;
        private bool _disposed;
        private CaptureState _lastState = CaptureState.Starting;

        public event EventHandler<NotificationPreview> PreviewCaptured;
        public event EventHandler<CaptureStateChangedEventArgs> StateChanged;

        public LocoMessageSource()
            : this(new CredentialVault(), null, null)
        {
        }

        public LocoMessageSource(CredentialVault vault)
            : this(vault, null, null)
        {
        }

        /// <param name="vault">CurrentUser-protected tablet credentials.</param>
        /// <param name="nodeExecutable">Optional node.exe path; null uses PATH.</param>
        /// <param name="bridgeScriptPath">Optional bundled bridge entry point.</param>
        public LocoMessageSource(CredentialVault vault, string nodeExecutable, string bridgeScriptPath)
        {
            _vault = vault ?? new CredentialVault();
            _nodeExecutable = string.IsNullOrWhiteSpace(nodeExecutable)
                ? LocoBridgeLocator.FindNodeExecutable()
                : nodeExecutable;
            _bridgeScriptPath = string.IsNullOrWhiteSpace(bridgeScriptPath)
                ? LocoBridgeLocator.FindBridgeScript()
                : Path.GetFullPath(bridgeScriptPath);
        }

        public bool IsRunning
        {
            get
            {
                lock (_sync)
                {
                    return _process != null && !HasExited(_process);
                }
            }
        }

        public string NodeExecutable
        {
            get { return _nodeExecutable; }
        }

        public string BridgeScriptPath
        {
            get { return _bridgeScriptPath; }
        }

        public void Start()
        {
            long generation;
            lock (_sync)
            {
                ThrowIfDisposed();
                generation = _lifecycle.RequestStart();
                _stopping = false;
                if (_maintenanceTimer == null)
                    _maintenanceTimer = new Timer(Maintain, null, 15000, 15000);
                DisposeReconnectTimerLocked();
                if (_process != null)
                {
                    return;
                }

                if (_starting)
                {
                    return;
                }
                _starting = true;
                _startingGeneration = generation;
            }

            RunStartAttempt(generation);
        }

        private void RunStartAttempt(long generation)
        {
            Process process = null;
            try
            {
                RaiseState(CaptureState.Starting, "태블릿 보조기기 연결을 준비하고 있어요");

                CredentialSnapshot credentialSnapshot = _vault.LoadSnapshot();
                LocoCredentials credentials = credentialSnapshot.Credentials;
                if (credentials == null || !credentials.IsUsable)
                {
                    RaiseState(CaptureState.Error, "먼저 태블릿 보조기기를 등록해 주세요");
                    return;
                }

                InboxStore inbox = PrepareInbox(credentials.UserId);
                if (inbox == null || inbox.HasLoadError)
                {
                    RaiseState(CaptureState.Error, "저장 기록을 안전하게 열지 못했어요. 기존 파일은 보존됩니다.");
                    return;
                }

                if (string.IsNullOrWhiteSpace(_bridgeScriptPath) || !File.Exists(_bridgeScriptPath))
                {
                    RaiseState(CaptureState.Error, "Spoiler 브리지를 찾을 수 없어요");
                    return;
                }

                process = new Process();
                process.StartInfo = LocoBridgeLauncher.CreateStartInfo(
                    _nodeExecutable,
                    _bridgeScriptPath,
                    Path.Combine(_vault.DataDirectory, "bridge-state"));
                process.EnableRaisingEvents = true;
                process.Exited += OnBridgeExited;

                lock (_sync)
                {
                    // Authorization and Process.Start share the same lock as
                    // Stop(). A stale reconnect callback therefore cannot
                    // launch anything after a later stop generation wins.
                    if (_disposed || !_lifecycle.Allows(generation))
                    {
                        return;
                    }
                    CredentialSnapshot currentSnapshot = _vault.LoadSnapshot();
                    LocoCredentials currentCredentials = currentSnapshot.Credentials;
                    if (currentCredentials == null || currentCredentials.UserId != credentials.UserId
                        || currentCredentials.DeviceUuid != credentials.DeviceUuid)
                    {
                        RaiseState(CaptureState.Error, "등록 계정이 바뀌었어요. 다시 연결해 주세요.");
                        return;
                    }
                    // Start with the most recent tokens and retain their exact
                    // vault revision as the baseline for child refresh events.
                    credentials = currentCredentials;
                    credentialSnapshot = currentSnapshot;
                    if (!process.Start())
                    {
                        throw new InvalidOperationException("The bridge process did not start.");
                    }

                    _authLeaseCancellation = new CancellationTokenSource();
                    _authLeasePending = false;
                    _process = process;
                    _processAccountId = credentials.UserId.Trim();
                    _processDeviceUuid = credentials.DeviceUuid;
                    _replyReady = false;
                    _processCredentialRevision = credentialSnapshot.Revision;
                    _lastBridgeActivityUtc = DateTime.UtcNow;
                    _stdoutThread = new Thread(ReadStdoutLoop);
                    _stderrThread = new Thread(ReadStderrLoop);
                    _stdoutThread.IsBackground = true;
                    _stderrThread.IsBackground = true;
                    _stdoutThread.Start(process);
                    _stderrThread.Start(process);
                }

                // The listener startup command contains only tablet
                // credentials and read-only options. In
                // particular it never contains a password or a read marker.
                IList<LocoCatchUpCursor> catchUp = GetConversationSyncCursors(inbox);
                SetCatchUpProtection(catchUp);
                WriteStartRequest(process, credentials, catchUp);
                RaiseState(CaptureState.Starting, "카카오톡 보조기기에 연결하고 있어요");
            }
            catch (Exception exception)
            {
                if (process != null)
                {
                    SafeKill(process);
                    try { process.Dispose(); } catch { }
                }

                lock (_sync)
                {
                    if (ReferenceEquals(_process, process))
                    {
                        _process = null;
                    }
                }

                RaiseState(CaptureState.Error, SafeErrorDetail(exception));
                ScheduleReconnect(generation);
            }
            finally
            {
                long followupGeneration = -1;
                bool processIsCurrent;
                lock (_sync)
                {
                    processIsCurrent = ReferenceEquals(_process, process);
                    if (_starting && _startingGeneration == generation)
                    {
                        _starting = false;
                    }

                    // An explicit Start may supersede an in-flight attempt.
                    // Claim that newer generation without going back through
                    // public Start(), which would be unsafe after Stop().
                    if (!_disposed && _lifecycle.DesiredRunning && _process == null
                        && !_starting && _lifecycle.Generation != generation)
                    {
                        followupGeneration = _lifecycle.Generation;
                        _starting = true;
                        _startingGeneration = followupGeneration;
                    }
                }

                if (process != null && !processIsCurrent)
                {
                    try { process.Dispose(); } catch { }
                }

                if (followupGeneration >= 0)
                {
                    ThreadPool.QueueUserWorkItem(delegate
                    {
                        try { RunStartAttempt(followupGeneration); } catch { }
                    });
                }
            }
        }

        public void Stop()
        {
            Process process;
            lock (_sync)
            {
                _lifecycle.RequestStop();
                _stopping = true;
                _authLeaseCancellation.Cancel();
                _reconnectAttempt = 0;
                DisposeReconnectTimerLocked();
                if (_maintenanceTimer != null) { _maintenanceTimer.Dispose(); _maintenanceTimer = null; }
                process = _process;
                _process = null;
                _processAccountId = null;
                _processDeviceUuid = null;
                _replyReady = false;
                CompletePendingReplyLocked(ReplySendStatus.Unknown, "reply_outcome_unknown");
            }

            if (process == null)
            {
                return;
            }

            // Closing stdin is the bridge's graceful lifecycle signal.  It
            // gives the bridge a chance to call KakaoTalkClient.close() and
            // tear down its socket.  No command is sent to the KakaoTalk
            // protocol, so this cannot become a message or read receipt.
            try
            {
                try { process.StandardInput.Close(); } catch { }
                if (!process.WaitForExit(2500))
                {
                    SafeKill(process);
                }
            }
            catch
            {
                SafeKill(process);
            }
            finally
            {
                bool exited = HasExited(process);
                if (!exited)
                {
                    SafeKill(process);
                    try { process.WaitForExit(1000); } catch { }
                    exited = HasExited(process);
                }
                if (exited)
                {
                    lock (_sync) { ReleaseAuthLeaseLocked(); }
                    try { process.Dispose(); } catch { }
                }
            }
        }

        public void Dispose()
        {
            Stop();
            lock (_sync)
            {
                if (_disposed) return;
                _disposed = true;
                if (_inbox != null) _inbox.Dispose();
                _inbox = null;
                _inboxAccountId = null;
                _catchUpProtectedChats.Clear();
            }
        }

        private void WriteStartRequest(
            Process process,
            LocoCredentials credentials,
            IList<LocoCatchUpCursor> catchUp)
        {
            LocoStartRequest request = new LocoStartRequest
            {
                Type = "start",
                DeviceType = CredentialVault.TabletDeviceType,
                Force = true,
                Credentials = credentials.Clone(),
                CatchUp = catchUp
            };
            WriteJsonLine(process, SerializeStartRequest(request));
        }

        private static void WriteJsonLine(Process process, string json)
        {
            if (process == null || string.IsNullOrEmpty(json))
            {
                return;
            }

            lock (process)
            {
                if (HasExited(process) || process.StandardInput == null)
                {
                    return;
                }

                LocoBridgeLauncher.WriteUtf8Line(process, json);
            }
        }

        private void ReadStdoutLoop(object state)
        {
            Process process = state as Process;
            if (process == null)
            {
                return;
            }

            // Reading and saving have separate bounded stages. A locked
            // inbox fills this queue, then stops pipe reads rather than
            // building an unbounded history in memory.
            BlockingCollection<string> lines = new BlockingCollection<string>(InboxStore.MaxCommitBatch);
            int readerStopped = 0;
            Thread reader = new Thread(delegate()
            {
                try
                {
                    string line;
                    while (Volatile.Read(ref readerStopped) == 0 && IsCurrentProcess(process)
                        && (line = process.StandardOutput.ReadLine()) != null)
                    {
                        while (Volatile.Read(ref readerStopped) == 0 && IsCurrentProcess(process))
                            if (lines.TryAdd(line, 100)) break;
                    }
                }
                catch { /* Shutdown and a disappearing pipe are normal. */ }
                finally { try { lines.CompleteAdding(); } catch { } }
            });
            reader.IsBackground = true;
            reader.Start();
            try
            {
                LocoBridgeEvent pending = null;
                while (IsCurrentProcess(process))
                {
                    LocoBridgeEvent first = pending;
                    pending = null;
                    if (first == null && !TryTakeBridgeEvent(lines, 100, out first))
                    {
                        if (lines.IsCompleted) break;
                        continue;
                    }
                    if (!IsMessageEvent(first))
                    {
                        ConsumeBridgeEvent(process, first);
                        continue;
                    }
                    List<LocoBridgeEvent> batch = new List<LocoBridgeEvent> { first };
                    Stopwatch age = Stopwatch.StartNew();
                    while (batch.Count < InboxStore.MaxCommitBatch && age.ElapsedMilliseconds < 100 && IsCurrentProcess(process))
                    {
                        LocoBridgeEvent next;
                        if (!TryTakeBridgeEvent(lines, Math.Max(0, 100 - (int)age.ElapsedMilliseconds), out next)) break;
                        if (!IsMessageEvent(next)) { pending = next; break; }
                        batch.Add(next);
                    }
                    ConsumeMessageBatch(process, batch);
                }
            }
            catch { /* Stop invalidates buffered events before closing pipes. */ }
            finally
            {
                Interlocked.Exchange(ref readerStopped, 1);
                JoinReader(reader);
                if (!reader.IsAlive) lines.Dispose();
            }
        }

        private bool IsCurrentProcess(Process process)
        {
            lock (_sync)
                return !_disposed && !_stopping && ReferenceEquals(_process, process)
                    && _processAccountId == _inboxAccountId;
        }

        private static bool IsMessageEvent(LocoBridgeEvent bridgeEvent)
        {
            string type = (bridgeEvent.Type ?? string.Empty).Trim();
            return string.Equals(type, "message", StringComparison.OrdinalIgnoreCase)
                || string.Equals(type, "msg", StringComparison.OrdinalIgnoreCase);
        }

        private static bool TryTakeBridgeEvent(BlockingCollection<string> lines, int timeout, out LocoBridgeEvent bridgeEvent)
        {
            Stopwatch wait = Stopwatch.StartNew();
            string line;
            bridgeEvent = null;
            while (lines.TryTake(out line, Math.Max(0, timeout - (int)wait.ElapsedMilliseconds)))
            {
                if (TryParseBridgeLine(line, out bridgeEvent)) return true;
                if (wait.ElapsedMilliseconds >= timeout) break;
            }
            return false;
        }

        private void ReadStderrLoop(object state)
        {
            Process process = state as Process;
            if (process == null)
            {
                return;
            }

            // Drain stderr so a noisy node process cannot deadlock.  Do not
            // persist or surface it: bridge diagnostics may contain tokens or
            // login material supplied by a third-party dependency.
            try
            {
                while (!_disposed && !_stopping && process.StandardError.ReadLine() != null)
                {
                }
            }
            catch
            {
            }
        }

        private void ConsumeBridgeLine(Process process, string line)
        {
            LocoBridgeEvent bridgeEvent;
            if (!TryParseBridgeLine(line, out bridgeEvent)) return;
            ConsumeBridgeEvent(process, bridgeEvent);
        }

        private void ConsumeBridgeEvent(Process process, LocoBridgeEvent bridgeEvent)
        {
            if (IsMessageEvent(bridgeEvent))
            {
                ConsumeMessageBatch(process, new[] { bridgeEvent });
                return;
            }
            // An explicit reply ACK needs a fresh identity snapshot. Loading
            // the cross-process vault must not hold the listener lifecycle
            // lock, otherwise UI availability checks and Stop would block.
            LocoCredentials replyCredentials = null;
            if (string.Equals(bridgeEvent.Type, "reply_result", StringComparison.OrdinalIgnoreCase))
                try { replyCredentials = _vault.Load(); } catch { }
            // Retry the same event, not a copy in an unbounded memory queue.
            // Pipe backpressure preserves body-before-cursor ordering.
            while (true)
            {
                bool retry = false;
                lock (_sync)
                {
                    if (_disposed || _stopping || !ReferenceEquals(_process, process)
                        || _processAccountId != _inboxAccountId) return;
                    _lastBridgeActivityUtc = DateTime.UtcNow;
                    string type = (bridgeEvent.Type ?? string.Empty).Trim().ToLowerInvariant();
                    UpdateReplyConnectionLocked(bridgeEvent);
                    if (type == "room_metadata")
                    {
                        InboxRoomMetadataResult committed = _inbox == null ? null
                            : _inbox.CommitRoomMetadata(bridgeEvent.ChatId, bridgeEvent.ChatTitle, bridgeEvent.TitleSource, bridgeEvent.Authoritative);
                        bool saved = committed != null && committed.Saved;
                        retry = !saved && _inbox != null && !_inbox.PersistenceAvailable && !_inbox.HasLoadError;
                        if (saved)
                            foreach (NotificationPreview updated in committed.Updates ?? new NotificationPreview[0])
                            {
                                if (!IsCurrentProcess(process)) break;
                                PublishMetadataPreviewLocked(process, updated);
                            }
                    }
                    else if (type == "unread_recovery_anchor")
                    {
                        bool committed = _inbox != null && _inbox.BeginUnreadRecovery(
                            bridgeEvent.ChatId, bridgeEvent.LogId, bridgeEvent.ThroughLogId);
                        if (committed) _catchUpProtectedChats.Add(bridgeEvent.ChatId);
                        retry = !committed && _inbox != null && !_inbox.PersistenceAvailable && !_inbox.HasLoadError;
                        if (!committed) RaiseState(CaptureState.Error, "새 대화방의 복구 기준점을 저장하지 못했어요.");
                    }
                    else if (type == "catchup_progress")
                    {
                        bool committed = _inbox != null && _inbox.CommitProgress(bridgeEvent.ChatId, bridgeEvent.LogId);
                        if (committed && bridgeEvent.Complete)
                            _catchUpProtectedChats.Remove(bridgeEvent.ChatId);
                        // Invalid progress fails closed. A failed write is retried.
                        retry = !committed && _inbox != null && !_inbox.PersistenceAvailable && !_inbox.HasLoadError;
                        if (!committed) RaiseState(CaptureState.Error, "복구 기준점을 저장하지 못했어요. 이전 기준점은 유지됩니다.");
                    }
                    else if (type == "conversation_synced" && bridgeEvent.Complete)
                    {
                        // The stdout reader finishes its preceding message
                        // batch durably before dispatching this ordered local
                        // marker. Empty pages may have no stored anchor, so
                        // completion must not invent a durable checkpoint.
                        bool committed = _inbox != null && _inbox.CompleteUnreadRecovery(bridgeEvent.ChatId);
                        retry = !committed && _inbox != null && !_inbox.PersistenceAvailable && !_inbox.HasLoadError;
                        if (committed) _catchUpProtectedChats.Remove(bridgeEvent.ChatId ?? string.Empty);
                        else RaiseState(CaptureState.Error, "새 대화방의 메시지 복구를 확인하지 못했어요. 기준점을 유지합니다.");
                    }
                    else if (type == "reply_result")
                        ConsumeReplyResultLocked(process, bridgeEvent, replyCredentials);
                    else if (type == "status" || type == "connected" || type == "ready" || type == "authenticated")
                        HandleStatus(bridgeEvent);
                    else if (type == "auth_lease_request" || type == "auth_lease_release")
                        HandleAuthLeaseEvent(process, bridgeEvent);
                    else if (type == "credential-refresh" || type == "credentials")
                    {
                        bool saved = RefreshCredentials(bridgeEvent.Credentials ?? bridgeEvent.Credential, bridgeEvent.AuthLeaseId);
                        WriteJsonLine(process, LocoBridgeLauncher.CreateCredentialSaveAck(saved));
                        if (!saved) RaiseState(CaptureState.Error, _credentialConflict
                            ? "다른 앱에서 인증 정보가 갱신됐어요. 최신 정보로 다시 연결합니다."
                            : "보조기기 자격증명을 갱신하지 못했어요");
                    }
                    else if (type == "needs_force" || type == "needs-force")
                        RaiseState(CaptureState.Error, "태블릿 슬롯이 사용 중이에요. 등록 화면에서 교체를 확인해 주세요");
                    else if (type == "error" || type == "auth-error" || type == "disconnected" || type == "kicked")
                        RaiseState(CaptureState.Error, bridgeEvent.DetailOrError());
                    _storageBlocked = retry;
                    if (!retry) return;
                    RaiseState(CaptureState.Error, "메시지를 저장하지 못해 수신 처리를 잠시 멈췄어요. 저장 공간을 확인해 주세요.");
                }
                Thread.Sleep(1000);
            }
        }

        private void OnBridgeExited(object sender, EventArgs args)
        {
            Process process = sender as Process;
            Thread stdout;
            Thread stderr;
            lock (_sync)
            {
                if (!ReferenceEquals(_process, process)) return;
                stdout = _stdoutThread;
                stderr = _stderrThread;
            }
            // Keep the process identity current until its already-delivered
            // events are durably consumed. Stop can still invalidate it.
            if (stdout != null && stdout != Thread.CurrentThread)
            {
                while (stdout.IsAlive)
                {
                    try { if (stdout.Join(250)) break; } catch { break; }
                    lock (_sync) { if (!ReferenceEquals(_process, process) || _disposed || _stopping) return; }
                }
            }
            JoinReader(stderr);
            long generation;
            lock (_sync)
            {
                if (!ReferenceEquals(_process, process)) return;
                _authLeaseCancellation.Cancel();
                ReleaseAuthLeaseLocked();
                _process = null;
                _processAccountId = null;
                _processDeviceUuid = null;
                _replyReady = false;
                CompletePendingReplyLocked(ReplySendStatus.Unknown, "reply_outcome_unknown");
                _processCredentialRevision = null;
                _stdoutThread = null;
                _stderrThread = null;
                _storageBlocked = false;
                generation = _lifecycle.Generation;
            }
            try { process.Exited -= OnBridgeExited; process.Dispose(); } catch { }
            ScheduleReconnect(generation);
        }

        private void ScheduleReconnect(long generation)
        {
            int delayMilliseconds;
            lock (_sync)
            {
                if (_stopping || _disposed || _process != null || !_lifecycle.Allows(generation))
                {
                    return;
                }

                _reconnectAttempt = Math.Min(_reconnectAttempt + 1, 5);
                delayMilliseconds = Math.Min(30000, 2000 * (1 << (_reconnectAttempt - 1)));
                DisposeReconnectTimerLocked();
                _reconnectTimer = new Timer(delegate
                {
                    bool claimed = false;
                    lock (_sync)
                    {
                        if (_stopping || _disposed || !_lifecycle.Allows(generation))
                        {
                            return;
                        }
                        DisposeReconnectTimerLocked();
                        if (!_starting && _process == null)
                        {
                            _starting = true;
                            _startingGeneration = generation;
                            claimed = true;
                        }
                    }
                    if (claimed)
                    {
                        // Never call public Start() here: it represents new
                        // user intent and could otherwise override Stop().
                        try { RunStartAttempt(generation); } catch { }
                    }
                }, null, delayMilliseconds, Timeout.Infinite);
            }

            RaiseState(CaptureState.Starting,
                "태블릿 연결이 끊어져 " + Math.Max(1, delayMilliseconds / 1000).ToString(CultureInfo.InvariantCulture) + "초 후 다시 연결해요");
        }

        private void DisposeReconnectTimerLocked()
        {
            Timer timer = _reconnectTimer;
            _reconnectTimer = null;
            if (timer != null)
            {
                try { timer.Dispose(); } catch { }
            }
        }

        private static void JoinReader(Thread thread)
        {
            if (thread == null || ReferenceEquals(thread, Thread.CurrentThread))
            {
                return;
            }
            try { thread.Join(800); } catch { }
        }

        private void RaiseState(CaptureState state, string detail)
        {
            lock (_sync)
            {
                if (_lastState == state && _lastDetail == detail && state != CaptureState.Captured)
                {
                    return;
                }

                _lastState = state;
                _lastDetail = detail;
            }

            EventHandler<CaptureStateChangedEventArgs> handler = StateChanged;
            if (handler != null)
            {
                CaptureStateChangedEventArgs stateEvent = new CaptureStateChangedEventArgs(state, detail ?? string.Empty);
                foreach (EventHandler<CaptureStateChangedEventArgs> subscriber in handler.GetInvocationList())
                {
                    try { subscriber(this, stateEvent); }
                    catch { /* A UI subscriber cannot stop the durable reader. */ }
                }
            }
        }

        private void ThrowIfDisposed()
        {
            if (_disposed)
            {
                throw new ObjectDisposedException("LocoMessageSource");
            }
        }

        private static bool HasExited(Process process)
        {
            try { return process == null || process.HasExited; }
            catch { return true; }
        }

        private static void SafeKill(Process process)
        {
            try
            {
                if (process != null && !HasExited(process))
                {
                    process.Kill();
                }
            }
            catch
            {
            }
        }

        private static string SafeErrorDetail(Exception exception)
        {
            if (exception == null)
            {
                return "카카오톡 보조기기를 시작하지 못했어요";
            }

            // Keep process paths and raw node diagnostics out of the UI.  A
            // short type-only message is enough to guide the user to retry.
            return "카카오톡 보조기기를 시작하지 못했어요 (" + exception.GetType().Name + ")";
        }
    }
}
