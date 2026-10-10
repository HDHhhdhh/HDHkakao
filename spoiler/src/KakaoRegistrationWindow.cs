using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using Tokens = Spoiler.DesignTokens;

namespace Spoiler
{
    /// <summary>
    /// Small, deliberately single-purpose tablet companion registration
    /// window.  It sends the login secret to the hidden bridge over stdin for
    /// the duration of registration only; the secret is never placed in the
    /// credential DTO or written to disk.
    /// </summary>
    public sealed class KakaoRegistrationWindow : Window
    {
        private readonly CredentialVault _vault;
        private readonly string _nodeExecutable;
        private readonly string _bridgeScriptPath;

        private TextBox _emailBox;
        private PasswordBox _secretBox;
        private TextBlock _statusText;
        private TextBlock _passcodeText;
        private StackPanel _passcodePanel;
        private Button _registerButton;
        private Button _forceButton;
        private Process _process;
        private Thread _stdoutThread;
        private Thread _stderrThread;
        private bool _stopping;
        private bool _disposed;
        private string _deviceUuid;
        private string _registrationCredentialRevision;
        private IDisposable _registrationAuthLease;
        private bool _registrationCompleted;
        private bool _needsForce;
        private bool _forceRequested;
        private System.Windows.Threading.DispatcherTimer _registrationWatchdog;

        public event EventHandler RegistrationCompleted;

        public KakaoRegistrationWindow()
            : this(new CredentialVault())
        {
        }

        public KakaoRegistrationWindow(CredentialVault vault)
            : this(vault, null, null)
        {
        }

        public KakaoRegistrationWindow(CredentialVault vault, string nodeExecutable, string bridgeScriptPath)
        {
            _vault = vault ?? new CredentialVault();
            _nodeExecutable = string.IsNullOrWhiteSpace(nodeExecutable)
                ? LocoBridgeLocator.FindNodeExecutable()
                : nodeExecutable;
            _bridgeScriptPath = string.IsNullOrWhiteSpace(bridgeScriptPath)
                ? LocoBridgeLocator.FindAuthScript()
                : Path.GetFullPath(bridgeScriptPath);

            Title = "Spoiler · 태블릿 보조기기 등록";
            Width = Tokens.Size.RegistrationWidth;
            Height = Tokens.Size.RegistrationHeight;
            MinWidth = Tokens.Size.RegistrationMinWidth;
            MinHeight = Tokens.Size.RegistrationMinHeight;
            WindowStartupLocation = WindowStartupLocation.CenterOwner;
            ResizeMode = ResizeMode.NoResize;
            Background = Tokens.Brush(Tokens.Palette.Canvas);
            FontFamily = new FontFamily("Malgun Gothic");
            ShowInTaskbar = false;
            System.Windows.Media.Imaging.BitmapSource icon = AssetLoader.LoadIconBitmap();
            if (icon != null)
            {
                Icon = icon;
            }
            Content = BuildContent();
            Closed += OnClosed;
        }

        public bool WasRegistered
        {
            get { return _registrationCompleted; }
        }

        private UIElement BuildContent()
        {
            Grid root = new Grid
            {
                Margin = new Thickness(Tokens.Space.RegistrationInset),
                Background = Tokens.Brush(Tokens.Palette.Canvas)
            };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Auto) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            TextBlock title = new TextBlock
            {
                Text = "태블릿 보조기기 연결",
                FontSize = Tokens.TypeScale.RegistrationTitle,
                FontWeight = FontWeights.SemiBold,
                Foreground = Tokens.Brush(Tokens.Palette.InkStrong),
                Margin = new Thickness(0, 0, 0, 6)
            };
            Grid.SetRow(title, 0);
            root.Children.Add(title);

            TextBlock intro = new KoreanGuideTextBlock
            {
                Text = "카카오톡 휴대폰에서 기기 등록을 확인하면\n팝업 없이 새 메시지를 받을 수 있어요.\nPC 카카오톡 슬롯과 화면은 건드리지 않아요.",
                TextWrapping = TextWrapping.Wrap,
                FontSize = Tokens.TypeScale.RegistrationIntro,
                LineHeight = 19,
                Foreground = Tokens.Brush(Tokens.Palette.InkSecondary),
                Margin = new Thickness(0, 0, 0, Tokens.Space.Xl)
            };
            Grid.SetRow(intro, 1);
            root.Children.Add(intro);

            StackPanel fields = new StackPanel();
            Grid.SetRow(fields, 2);
            root.Children.Add(fields);

            fields.Children.Add(new TextBlock
            {
                Text = "카카오톡 이메일",
                FontSize = Tokens.TypeScale.FieldLabel,
                FontWeight = FontWeights.SemiBold,
                Foreground = Tokens.Brush(Tokens.Palette.InkBody),
                Margin = new Thickness(0, 0, 0, Tokens.Space.RegistrationLabelGap)
            });
            _emailBox = new TextBox
            {
                FontSize = Tokens.TypeScale.FieldText,
                Padding = new Thickness(Tokens.Space.RegistrationFieldPaddingHorizontal, 0, Tokens.Space.RegistrationFieldPaddingHorizontal, 0),
                VerticalContentAlignment = VerticalAlignment.Center,
                Background = Brushes.Transparent,
                Foreground = Tokens.Brush(Tokens.Palette.Ink),
                BorderThickness = new Thickness(0)
            };
            fields.Children.Add(BuildFieldShell(_emailBox, Tokens.Space.RegistrationFieldGap));

            fields.Children.Add(new TextBlock
            {
                Text = "카카오톡 비밀번호",
                FontSize = Tokens.TypeScale.FieldLabel,
                FontWeight = FontWeights.SemiBold,
                Foreground = Tokens.Brush(Tokens.Palette.InkBody),
                Margin = new Thickness(0, 0, 0, Tokens.Space.RegistrationLabelGap)
            });
            _secretBox = new PasswordBox
            {
                FontSize = Tokens.TypeScale.FieldText,
                Padding = new Thickness(Tokens.Space.RegistrationFieldPaddingHorizontal, 0, Tokens.Space.RegistrationFieldPaddingHorizontal, 0),
                VerticalContentAlignment = VerticalAlignment.Center,
                Background = Brushes.Transparent,
                Foreground = Tokens.Brush(Tokens.Palette.Ink),
                BorderThickness = new Thickness(0)
            };
            fields.Children.Add(BuildFieldShell(_secretBox, Tokens.Space.Md));

            _passcodePanel = new StackPanel
            {
                Margin = new Thickness(0, Tokens.Space.Sm, 0, 0),
                Visibility = Visibility.Collapsed
            };
            Grid.SetRow(_passcodePanel, 3);
            root.Children.Add(_passcodePanel);
            _passcodeText = new TextBlock
            {
                Text = string.Empty,
                FontSize = Tokens.TypeScale.Passcode,
                FontWeight = FontWeights.SemiBold,
                Foreground = Tokens.Brush(Tokens.Palette.InkStrong),
                Margin = new Thickness(0, 0, 0, 8)
            };
            _passcodePanel.Children.Add(_passcodeText);

            _statusText = new KoreanGuideTextBlock
            {
                Text = "비밀번호는 저장하지 않으며 태블릿 슬롯만 사용해요.",
                FontSize = Tokens.TypeScale.RegistrationStatus,
                LineHeight = 20,
                TextWrapping = TextWrapping.Wrap,
                Foreground = Tokens.Brush(Tokens.Palette.InkSecondary),
                VerticalAlignment = VerticalAlignment.Top
            };
            Grid.SetRow(_statusText, 4);
            root.Children.Add(_statusText);

            StackPanel buttons = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, Tokens.Space.RegistrationButtonsTop, 0, 0)
            };
            Grid.SetRow(buttons, 5);
            root.Children.Add(buttons);

            _forceButton = new TactileButton
            {
                Content = "기존 태블릿 연결 바꾸기",
                Height = Tokens.Size.MinimumHit,
                Padding = new Thickness(Tokens.Space.RegistrationSecondaryButtonPaddingHorizontal, 4, Tokens.Space.RegistrationSecondaryButtonPaddingHorizontal, 4),
                Margin = new Thickness(0, 0, Tokens.Space.RegistrationButtonGap, 0),
                Background = Tokens.Brush(Tokens.Palette.SurfaceControl),
                Foreground = Tokens.Brush(Tokens.Palette.Ink),
                BorderBrush = Tokens.Brush(Tokens.Palette.DividerStrong),
                BorderThickness = new Thickness(1),
                Template = TactileButton.CreateRoundedTemplate(Tokens.Radius.Action, Tokens.Brush(Tokens.Palette.SurfaceControlHover)),
                Visibility = Visibility.Collapsed
            };
            _forceButton.Click += OnForceRegistrationClick;
            buttons.Children.Add(_forceButton);

            _registerButton = new TactileButton
            {
                Content = "등록 시작",
                Height = Tokens.Size.MinimumHit,
                MinWidth = Tokens.Size.RegistrationPrimaryButtonMinWidth,
                Padding = new Thickness(Tokens.Space.RegistrationButtonPaddingHorizontal, 4, Tokens.Space.RegistrationButtonPaddingHorizontal, 4),
                FontWeight = FontWeights.SemiBold,
                Background = Tokens.Brush(Tokens.Palette.Accent),
                Foreground = Tokens.Brush(Tokens.Palette.AccentInk),
                BorderBrush = Tokens.Brush(Tokens.Palette.Focus),
                BorderThickness = new Thickness(0),
                Template = TactileButton.CreateRoundedTemplate(Tokens.Radius.Action, Tokens.Brush(Tokens.Palette.AccentHover)),
                Effect = Tokens.ActionElevation()
            };
            _registerButton.Click += OnRegisterClick;
            buttons.Children.Add(_registerButton);

            return root;
        }

        private static Border BuildFieldShell(Control field, double bottomMargin)
        {
            Border shell = new Border
            {
                Height = Tokens.Size.FieldHeight,
                Background = Tokens.Brush(Tokens.Palette.Surface),
                BorderBrush = Tokens.Brush(Tokens.Palette.DividerStrong),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(Tokens.Radius.Field),
                ClipToBounds = true,
                Margin = new Thickness(0, 0, 0, bottomMargin),
                Child = field
            };
            field.GotKeyboardFocus += delegate
            {
                shell.BorderBrush = Tokens.Brush(Tokens.Palette.Focus);
            };
            field.LostKeyboardFocus += delegate
            {
                shell.BorderBrush = Tokens.Brush(Tokens.Palette.DividerStrong);
            };
            return shell;
        }

        private void OnRegisterClick(object sender, RoutedEventArgs args)
        {
            BeginRegistration(false);
        }

        private void OnForceRegistrationClick(object sender, RoutedEventArgs args)
        {
            // Force is never inferred from a generic failure.  This button is
            // shown only after the bridge explicitly reports needs_force, and
            // the user must click it before a force:true request is sent.
            if (IsProcessRunning())
            {
                _forceRequested = true;
                _forceButton.IsEnabled = false;
                SetStatus("안전한 첫 연결이 끝나는 즉시 기존 태블릿 연결을 바꿀게요.", false);
                return;
            }

            // The safe probe exits after needs_force (exit code 2).  Dispose
            // that probe and launch a new invocation only because this button
            // was explicitly clicked by the user.
            ReleaseExitedProcess();
            _forceRequested = false;
            BeginRegistration(true);
        }

        private void BeginRegistration(bool force)
        {
            if (_disposed || IsProcessRunning())
            {
                return;
            }

            string email = _emailBox == null ? string.Empty : (_emailBox.Text ?? string.Empty).Trim();
            string secret = _secretBox == null ? string.Empty : _secretBox.Password;
            if (email.Length == 0 || secret.Length == 0)
            {
                SetStatus("이메일과 비밀번호를 입력해 주세요.", false);
                return;
            }

            try
            {
                _registrationAuthLease = _vault.AcquireAuthLease(0, CancellationToken.None);
                _forceRequested = false;
                _needsForce = false;
                _passcodePanel.Visibility = Visibility.Collapsed;
                _passcodeText.Text = string.Empty;
                _forceButton.Visibility = Visibility.Collapsed;
                SetRegistrationInputsEnabled(false);
                // Persist the identity before any bridge process can make a
                // network request.  It remains stable across retries.
                _deviceUuid = _vault.GetOrCreateDeviceUuid();
                _registrationCredentialRevision = _vault.LoadSnapshot().Revision;
                StartBridge(email, secret, force);
                StartRegistrationWatchdog();
                SetStatus(force
                    ? "기존 태블릿 연결을 바꾸는 중이에요. 잠시만 기다려 주세요."
                    : "카카오톡 휴대폰에서 기기 등록을 확인해 주세요.", false);
            }
            catch (TimeoutException)
            {
                StopBridge();
                SetRegistrationInputsEnabled(true);
                SetStatus("다른 앱에서 인증 중이에요. 잠시 후 다시 등록해 주세요.", true);
            }
            catch (Exception exception)
            {
                StopBridge();
                SetRegistrationInputsEnabled(true);
                SetStatus("등록을 시작하지 못했어요 (" + exception.GetType().Name + ")", true);
            }
            finally
            {
                // The PasswordBox is cleared on successful registration,
                // terminal failure, or window close.  Keeping it only in the
                // UI between the safe probe and an explicit force decision
                // lets the second invocation reuse the secret without ever
                // persisting it.
            }
        }

        private void StartBridge(string email, string secret, bool force)
        {
            if (string.IsNullOrWhiteSpace(_bridgeScriptPath) || !File.Exists(_bridgeScriptPath))
            {
                throw new FileNotFoundException("Spoiler bridge was not found.", _bridgeScriptPath);
            }

            Process process = new Process
            {
                StartInfo = LocoBridgeLauncher.CreateStartInfo(
                    _nodeExecutable,
                    _bridgeScriptPath,
                    Path.Combine(_vault.DataDirectory, "bridge-state")),
                EnableRaisingEvents = false
            };
            process.Exited += OnBridgeExited;
            if (!process.Start())
            {
                process.Dispose();
                throw new InvalidOperationException("The bridge process did not start.");
            }

            _process = process;
            _stopping = false;
            try
            {
                lock (process)
                {
                    LocoBridgeLauncher.WriteUtf8Line(
                        process,
                        LocoBridgeLauncher.CreateRegistrationRequest(email, secret, _deviceUuid, force));
                    // Authentication is a one-shot protocol, not an ACK stream.
                    process.StandardInput.Close();
                }
                _stdoutThread = new Thread(ReadStdoutLoop) { IsBackground = true };
                _stderrThread = new Thread(ReadStderrLoop) { IsBackground = true };
                _stdoutThread.Start(process);
                _stderrThread.Start(process);
                process.EnableRaisingEvents = true;
            }
            catch
            {
                _process = null;
                Kill(process);
                try { process.Dispose(); } catch { }
                throw;
            }
        }

        private void ReadStdoutLoop(object state)
        {
            Process process = state as Process;
            if (process == null)
            {
                return;
            }

            try
            {
                string line;
                while (!_disposed && !_stopping && (line = process.StandardOutput.ReadLine()) != null)
                {
                    LocoBridgeEvent bridgeEvent;
                    if (LocoMessageSource.TryParseBridgeLine(line, out bridgeEvent))
                    {
                        Dispatcher.BeginInvoke(new Action(delegate
                        {
                            if (!_disposed && !_stopping && ReferenceEquals(_process, process))
                                ConsumeBridgeEvent(bridgeEvent);
                        }));
                    }
                }
            }
            catch
            {
            }
        }

        private void ReadStderrLoop(object state)
        {
            Process process = state as Process;
            if (process == null)
            {
                return;
            }

            // Drain but never display/log dependency diagnostics.  They can
            // contain account identifiers or transient authentication data.
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

        private void ConsumeBridgeEvent(LocoBridgeEvent bridgeEvent)
        {
            if (bridgeEvent == null)
            {
                return;
            }

            if (!string.IsNullOrWhiteSpace(bridgeEvent.DeviceUuid))
            {
                try
                {
                    _deviceUuid = bridgeEvent.DeviceUuid.Trim();
                    _vault.SaveDeviceUuid(_deviceUuid);
                }
                catch
                {
                    SetStatus("태블릿 기기 ID를 저장하지 못했어요. 다시 시도해 주세요.", true);
                    return;
                }
            }

            LocoCredentials credentials = bridgeEvent.Credentials ?? bridgeEvent.Credential;
            if (credentials != null && credentials.HasSessionToken && !string.IsNullOrWhiteSpace(credentials.UserId))
            {
                try
                {
                    credentials.DeviceUuid = string.IsNullOrWhiteSpace(credentials.DeviceUuid) ? _deviceUuid : credentials.DeviceUuid;
                    credentials.DeviceType = CredentialVault.TabletDeviceType;
                    CredentialSnapshot persisted = _vault.SaveIfUnchanged(credentials, _registrationCredentialRevision);
                    _registrationCredentialRevision = persisted.Revision;
                    ReleaseRegistrationLease();
                    _registrationCompleted = true;
                    StopRegistrationWatchdog();
                    _needsForce = false;
                    _passcodePanel.Visibility = Visibility.Collapsed;
                    _passcodeText.Text = string.Empty;
                    _forceButton.Visibility = Visibility.Collapsed;
                    _registerButton.IsEnabled = false;
                    SetStatus("등록이 완료됐어요. 이제 Spoiler가 메시지를 기다립니다.", false);
                    if (_secretBox != null)
                    {
                        _secretBox.Clear();
                    }
                    EventHandler handler = RegistrationCompleted;
                    if (handler != null)
                    {
                        handler(this, EventArgs.Empty);
                    }
                }
                catch
                {
                    SetStatus("자격증명을 저장하지 못했어요. 다시 시도해 주세요.", true);
                }
                return;
            }

            string type = (bridgeEvent.Type ?? string.Empty).ToLowerInvariant();
            string status = (bridgeEvent.Status ?? bridgeEvent.State ?? string.Empty).ToLowerInvariant();
            if (type == "passcode")
            {
                string code = !string.IsNullOrWhiteSpace(bridgeEvent.Passcode) ? bridgeEvent.Passcode : bridgeEvent.Code;
                if (string.IsNullOrWhiteSpace(code) || !System.Text.RegularExpressions.Regex.IsMatch(code, @"^\d{4,8}$"))
                {
                    return;
                }
                _passcodePanel.Visibility = Visibility.Visible;
                _passcodeText.Text = "휴대폰 확인 코드  " + code;
                SetStatus("카카오톡 휴대폰에서 이 기기 등록을 확인해 주세요.", false);
                return;
            }

            if (type == "needs_force" || type == "needs-force"
                || (type == "status" && (status.Contains("needs_force") || status.Contains("choose_device") || status.Contains("slot"))))
            {
                _needsForce = true;
                _passcodePanel.Visibility = Visibility.Collapsed;
                _passcodeText.Text = string.Empty;
                _forceButton.Visibility = Visibility.Visible;
                _forceButton.IsEnabled = true;
                SetRegistrationInputsEnabled(true);
                SetStatus("기존 태블릿 연결이 사용 중이에요. 바꾸려면 버튼을 눌러 다시 시도하세요.", false);
                return;
            }

            if (type == "error" || type == "auth-error" || status.Contains("error"))
            {
                _passcodePanel.Visibility = Visibility.Collapsed;
                _passcodeText.Text = string.Empty;
                if (!_needsForce && _secretBox != null)
                {
                    _secretBox.Clear();
                }
                SetRegistrationInputsEnabled(true);
                SetStatus(FriendlyAuthError(bridgeEvent), true);
            }
        }

        private static string FriendlyAuthError(LocoBridgeEvent bridgeEvent)
        {
            string code = (bridgeEvent == null ? string.Empty : bridgeEvent.Code ?? string.Empty).ToLowerInvariant();
            switch (code)
            {
                case "bad_credentials":
                    return "카카오계정 이메일 또는 비밀번호를 확인해 주세요.";
                case "passcode_request_failed":
                    return "휴대폰 확인 코드를 받지 못했어요. 잠시 후 다시 시도해 주세요.";
                case "registration_timeout":
                    return "휴대폰 승인 시간이 지났어요. 등록을 다시 시작해 주세요.";
                case "registration_failed":
                    return "휴대폰에서 기기 등록을 완료하지 못했어요.";
                case "login_http_error":
                    return "카카오 로그인 서버에 연결하지 못했어요. 네트워크를 확인해 주세요.";
                case "invalid_auth_request":
                    return "태블릿 등록 요청을 만들지 못했어요. Spoiler를 다시 실행해 주세요.";
                default:
                    return bridgeEvent == null ? "태블릿 등록을 완료하지 못했어요." : bridgeEvent.DetailOrError();
            }
        }

        private void OnBridgeExited(object sender, EventArgs args)
        {
            Process process = sender as Process;
            if (!ReferenceEquals(_process, process)) return;
            int exitCode = -1;
            try { if (process != null) exitCode = process.ExitCode; } catch { }

            // Drain every final NDJSON line (especially the credential bundle)
            // before queueing exit handling on the UI dispatcher.
            Thread stdout = _stdoutThread;
            if (stdout != null && !ReferenceEquals(stdout, Thread.CurrentThread))
            {
                try { stdout.Join(5000); } catch { }
            }

            if (_stopping || _disposed || _registrationCompleted)
            {
                return;
            }

            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (_stopping || _disposed || _registrationCompleted || !ReferenceEquals(_process, process)) return;
                StopRegistrationWatchdog();
                // Once the registration process exits, any previously shown
                // handset code is stale and must not remain on screen.
                _passcodePanel.Visibility = Visibility.Collapsed;
                _passcodeText.Text = string.Empty;
                SetRegistrationInputsEnabled(true);
                if (exitCode == 2 || _needsForce)
                {
                    _needsForce = true;
                    if (_forceRequested)
                    {
                        _forceRequested = false;
                        ReleaseExitedProcess();
                        BeginRegistration(true);
                        return;
                    }
                    _forceButton.Visibility = Visibility.Visible;
                    _forceButton.IsEnabled = true;
                    SetStatus("기존 태블릿 연결이 사용 중이에요. 바꾸려면 버튼을 눌러 다시 시도하세요.", false);
                }
                else if (!_registrationCompleted)
                {
                    if (_secretBox != null)
                    {
                        _secretBox.Clear();
                    }
                    SetStatus("등록 과정이 종료됐어요. 다시 시도해 주세요.", true);
                }
                ReleaseExitedProcess();
            }));
        }

        private void StartRegistrationWatchdog()
        {
            StopRegistrationWatchdog();
            Process expected = _process;
            _registrationWatchdog = new System.Windows.Threading.DispatcherTimer
            {
                Interval = TimeSpan.FromMinutes(4)
            };
            _registrationWatchdog.Tick += delegate
            {
                StopRegistrationWatchdog();
                if (_disposed || _registrationCompleted || !ReferenceEquals(_process, expected)) return;
                StopBridge();
                _passcodePanel.Visibility = Visibility.Collapsed;
                _passcodeText.Text = string.Empty;
                _secretBox.Clear();
                SetRegistrationInputsEnabled(true);
                SetStatus("등록 응답 시간이 지났어요. 등록을 다시 시작해 주세요.", true);
            };
            _registrationWatchdog.Start();
        }

        private void StopRegistrationWatchdog()
        {
            if (_registrationWatchdog == null) return;
            _registrationWatchdog.Stop();
            _registrationWatchdog = null;
        }

        private bool IsProcessRunning()
        {
            try { return _process != null && !_process.HasExited; }
            catch { return false; }
        }

        private void SetStatus(string message, bool isError)
        {
            if (_statusText == null)
            {
                return;
            }

            _statusText.Text = message ?? string.Empty;
            _statusText.Foreground = isError
                ? Tokens.Brush(Tokens.Palette.DangerInk)
                : Tokens.Brush(Tokens.Palette.InkSecondary);
        }

        private void SetRegistrationInputsEnabled(bool enabled)
        {
            if (_emailBox != null) _emailBox.IsEnabled = enabled;
            if (_secretBox != null) _secretBox.IsEnabled = enabled;
            if (_registerButton != null) _registerButton.IsEnabled = enabled && !_registrationCompleted;
        }

        private void OnClosed(object sender, EventArgs args)
        {
            _disposed = true;
            StopBridge();
            if (_secretBox != null)
            {
                _secretBox.Clear();
            }
        }

        private void StopBridge()
        {
            StopRegistrationWatchdog();
            Process process = _process;
            _process = null;
            _stopping = true;
            if (process == null)
            {
                ReleaseRegistrationLease();
                return;
            }

            try
            {
                lock (process)
                {
                    if (!process.HasExited)
                    {
                        // EOF is the bridge's graceful shutdown signal; the
                        // bridge then stops its listener and closes the LOCO
                        // client before returning.
                        process.StandardInput.Close();
                    }
                }
                if (!process.WaitForExit(2500))
                {
                    Kill(process);
                }
            }
            catch
            {
                Kill(process);
            }
            finally
            {
                bool exited = !IsProcessRunning(process);
                if (!exited) { Kill(process); try { process.WaitForExit(1000); } catch { } exited = !IsProcessRunning(process); }
                if (exited) { ReleaseRegistrationLease(); try { process.Dispose(); } catch { } }
                else _process = process; // Retain ownership until the remote auth child actually exits.
            }
        }

        private void ReleaseRegistrationLease()
        {
            IDisposable lease = _registrationAuthLease;
            _registrationAuthLease = null;
            if (lease != null) lease.Dispose();
        }

        private void ReleaseExitedProcess()
        {
            Process process = _process;
            if (process == null || IsProcessRunning(process))
            {
                return;
            }

            _process = null;
            try { process.Dispose(); } catch { }
            ReleaseRegistrationLease();
        }

        private static bool IsProcessRunning(Process process)
        {
            try { return process != null && !process.HasExited; }
            catch { return false; }
        }

        private static void Kill(Process process)
        {
            try
            {
                if (process != null && !process.HasExited)
                {
                    process.Kill();
                }
            }
            catch
            {
            }
        }
    }
}
