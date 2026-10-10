using System;
using System.Diagnostics;
using System.Threading;

namespace Spoiler
{
    // The host arbitrates remote auth ownership and persists refreshed credentials before ACK.
    public sealed partial class LocoMessageSource
    {
        private void ReleaseAuthLeaseLocked()
        {
            if (_authLeaseWatchdog != null) { _authLeaseWatchdog.Dispose(); _authLeaseWatchdog = null; }
            IDisposable lease = _authLease;
            _authLease = null;
            _authLeaseId = null;
            if (lease != null) lease.Dispose();
        }
        private void HandleAuthLeaseEvent(Process process, LocoBridgeEvent bridgeEvent)
        {
            if (bridgeEvent.Type == "auth_lease_release")
            {
                bool valid = _authLease != null && bridgeEvent.AuthLeaseId == _authLeaseId;
                if (valid) ReleaseAuthLeaseLocked();
                WriteJsonLine(process, LocoBridgeLauncher.CreateAuthLeaseReply("auth_lease_released",
                    bridgeEvent.RequestId, valid, null, null, valid ? null : "auth_lease_invalid"));
                return;
            }
            if (_authLeasePending || _authLease != null || string.IsNullOrEmpty(bridgeEvent.RequestId)) return;
            _authLeasePending = true;
            CancellationToken cancellation = _authLeaseCancellation.Token;
            ThreadPool.QueueUserWorkItem(delegate
            {
                IDisposable lease = null;
                string errorCode = "auth_lease_invalid";
                try
                {
                    LocoCredentials expected = bridgeEvent.Credentials;
                    if (expected == null) throw new InvalidOperationException();
                    // Never hold _sync while waiting for the other app.
                    lease = _vault.AcquireAuthLease(10000, cancellation);
                    CredentialSnapshot snapshot = _vault.LoadSnapshot();
                    LocoCredentials latest = snapshot.Credentials;
                    if (latest == null || latest.UserId != expected.UserId || latest.DeviceUuid != expected.DeviceUuid)
                        throw new InvalidOperationException("auth_identity_changed");
                    lock (_sync)
                    {
                        if (cancellation.IsCancellationRequested || _disposed || _stopping || !ReferenceEquals(_process, process)) return;
                        _authLease = lease; lease = null;
                        _authLeaseId = Guid.NewGuid().ToString("N");
                        _processCredentialRevision = snapshot.Revision;
                        string watchedLeaseId = _authLeaseId;
                        _authLeaseWatchdog = new Timer(delegate
                        {
                            lock (_sync)
                            {
                                if (_authLease != null && _authLeaseId == watchedLeaseId)
                                {
                                    SafeKill(process);
                                    try { process.WaitForExit(1000); } catch { }
                                    if (HasExited(process)) ReleaseAuthLeaseLocked();
                                }
                            }
                        }, null, 65000, Timeout.Infinite);
                        WriteJsonLine(process, LocoBridgeLauncher.CreateAuthLeaseReply("auth_lease_granted",
                            bridgeEvent.RequestId, true, _authLeaseId, latest, null));
                        return;
                    }
                }
                catch (TimeoutException) { errorCode = "auth_lease_busy"; }
                catch (OperationCanceledException) { errorCode = "auth_lease_cancelled"; }
                catch (InvalidOperationException) { errorCode = "auth_identity_changed"; }
                catch { }
                finally
                {
                    if (lease != null) lease.Dispose();
                    lock (_sync) { if (ReferenceEquals(_process, process)) _authLeasePending = false; }
                }
                lock (_sync)
                {
                    if (!cancellation.IsCancellationRequested && ReferenceEquals(_process, process))
                        try { WriteJsonLine(process, LocoBridgeLauncher.CreateAuthLeaseReply("auth_lease_granted",
                            bridgeEvent.RequestId, false, null, null, errorCode)); } catch { }
                }
            });
        }
        private bool RefreshCredentials(LocoCredentials refreshed, string authLeaseId)
        {
            _credentialConflict = false;
            if (refreshed == null || _authLease == null || string.IsNullOrEmpty(authLeaseId)
                || !string.Equals(authLeaseId, _authLeaseId, StringComparison.Ordinal)) return false;

            try
            {
                LocoCredentials current = _vault.Load();
                if (current == null || current.UserId != _inboxAccountId
                    || (!string.IsNullOrEmpty(refreshed.UserId) && refreshed.UserId != current.UserId)
                    || (!string.IsNullOrEmpty(refreshed.DeviceUuid) && refreshed.DeviceUuid != current.DeviceUuid)) return false;
                LocoCredentials merged = MergeCredentials(current, refreshed);
                if (merged != null && merged.IsUsable)
                {
                    CredentialSnapshot persisted = _vault.SaveIfUnchanged(merged, _processCredentialRevision);
                    _processCredentialRevision = persisted.Revision;
                    return true;
                }
            }
            catch (CredentialVaultConflictException)
            {
                _credentialConflict = true;
                RaiseState(CaptureState.Error, "다른 앱에서 인증 정보가 갱신됐어요. 최신 정보로 다시 연결합니다.");
            }
            catch
            {
                RaiseState(CaptureState.Error, "보조기기 자격증명을 저장하지 못했어요");
            }
            return false;
        }
        internal static LocoCredentials MergeCredentials(LocoCredentials current, LocoCredentials refreshed)
        {
            if (refreshed == null && current == null)
            {
                return null;
            }

            LocoCredentials result = current == null
                ? new LocoCredentials()
                : current.Clone();

            if (refreshed != null)
            {
                if (!string.IsNullOrWhiteSpace(refreshed.OAuthToken)) result.OAuthToken = refreshed.OAuthToken;
                if (!string.IsNullOrWhiteSpace(refreshed.AccessToken)) result.AccessToken = refreshed.AccessToken;
                if (!string.IsNullOrWhiteSpace(refreshed.RefreshToken)) result.RefreshToken = refreshed.RefreshToken;
                if (!string.IsNullOrWhiteSpace(refreshed.UserId)) result.UserId = refreshed.UserId;
                if (!string.IsNullOrWhiteSpace(refreshed.DeviceUuid)) result.DeviceUuid = refreshed.DeviceUuid;
                if (!string.IsNullOrWhiteSpace(refreshed.AccountId)) result.AccountId = refreshed.AccountId;
                if (!string.IsNullOrWhiteSpace(refreshed.AuthMethod)) result.AuthMethod = refreshed.AuthMethod;
                if (!string.IsNullOrWhiteSpace(refreshed.CreatedAt)) result.CreatedAt = refreshed.CreatedAt;
                if (!string.IsNullOrWhiteSpace(refreshed.UpdatedAt)) result.UpdatedAt = refreshed.UpdatedAt;
            }

            result.DeviceType = CredentialVault.TabletDeviceType;
            return result;
        }
    }
}
