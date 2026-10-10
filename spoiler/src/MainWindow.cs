using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Data;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Effects;
using System.Windows.Media.Imaging;
using System.Windows.Shapes;
using System.Windows.Shell;
using Tokens = Spoiler.DesignTokens;

namespace Spoiler
{
    public sealed partial class MainWindow : Window
    {
        private static readonly object QaTraceSync = new object();
        private static readonly FontFamily UiFont = new FontFamily("Malgun Gothic");
        private static readonly FontFamily IconFont = new FontFamily("Segoe Fluent Icons");
        private static readonly CultureInfo KoreanCulture = CultureInfo.GetCultureInfo("ko-KR");

        private readonly bool _demoMode;
        private readonly bool _qaMode;
        private readonly CredentialVault _vault;
        private readonly List<NotificationPreview> _items;
        private readonly LocoMessageSource _source;
        private readonly object _pendingSync = new object();
        private readonly List<PendingPreview> _pendingPreviews = new List<PendingPreview>();
        private readonly System.Windows.Threading.DispatcherTimer _uiBatchTimer;
        private CaptureStateChangedEventArgs _pendingState;
        private int _pendingStateGeneration;
        private string _pendingStateAccountId;
        private int _uiGeneration;
        private string _displayAccountId;
        private bool _visualsDirty;
        private bool _scopeTransition;
        private DateTime _lastRetentionCheckUtc = DateTime.UtcNow;

        private sealed class PendingPreview
        {
            public NotificationPreview Preview;
            public int Generation;
        }

        private bool _hideMine;
        private bool _finderExpanded;
        private bool _searchActive;
        private bool _searchAfternoon = true;
        private bool _committedSearchAfternoon = true;
        private int _searchHour;
        private int _searchMinute;
        private bool _exitRequested;

        private StackPanel _listHost;
        private ScrollViewer _contentScroll;
        private TextBlock _statusText;
        private FrameworkElement _headerBlock;
        private FrameworkElement _sectionHeading;
        private TextBlock _maximizeGlyph;
        private TactileButton _maximizeButton;
        private TactileButton _refreshButton;
        private TactileButton _deviceButton;
        private TactileButton _findToggleButton;
        private TactileButton _amButton;
        private TactileButton _pmButton;
        private TactileButton _clearSearchButton;
        private CheckBox _hideMineCheckBox;
        private Border _finderPanel;
        private TextBox _hourInput;
        private TextBox _minuteInput;
        private TextBlock _finderFeedback;
        private TrayIconHost _trayIcon;
        private System.Windows.Threading.DispatcherTimer _screenshotTimer;
        private HwndSource _windowHwndSource;

        public MainWindow(bool demoMode, bool qaMode) : this(demoMode, qaMode, null) { }

        public MainWindow(bool demoMode, bool qaMode, IReplyMessageSource replySource)
        {
            _demoMode = demoMode;
            _qaMode = qaMode;
            _vault = new CredentialVault();
            if (demoMode)
            {
                _items = CreateDemoItems();
            }
            else
            {
                _items = new List<NotificationPreview>();
            }
            _source = demoMode || qaMode ? null : new LocoMessageSource(_vault);
            _replySource = replySource ?? _source;
            // The receiver owns the durable inbox. No unscoped history is
            // shown before the registered account has been established.
            if (_source != null)
            {
                _items.AddRange(_source.LoadStoredMessages());
                _displayAccountId = _source.CurrentAccountId;
            }
            _uiBatchTimer = new System.Windows.Threading.DispatcherTimer(
                System.Windows.Threading.DispatcherPriority.Background, Dispatcher);
            _uiBatchTimer.Interval = TimeSpan.FromMilliseconds(100);
            _uiBatchTimer.Tick += delegate { FlushPendingUi(); };

            Title = "Spoiler";
            Width = Tokens.Size.WindowWidth;
            Height = Tokens.Size.WindowHeight;
            MinWidth = Tokens.Size.WindowMinWidth;
            MinHeight = Tokens.Size.WindowMinHeight;
            WindowStartupLocation = WindowStartupLocation.CenterScreen;
            WindowStyle = WindowStyle.None;
            ResizeMode = ResizeMode.CanResize;
            Background = MakeBrush(Tokens.Palette.Canvas);
            FontFamily = UiFont;
            UseLayoutRounding = true;
            SnapsToDevicePixels = true;

            BitmapSource icon = AssetLoader.LoadIconBitmap();
            if (icon != null)
            {
                Icon = icon;
            }

            WindowChrome chrome = new WindowChrome();
            chrome.CaptionHeight = Tokens.Size.TitleBarHeight;
            chrome.ResizeBorderThickness = new Thickness(7);
            chrome.CornerRadius = new CornerRadius(Tokens.Radius.Window);
            chrome.GlassFrameThickness = new Thickness(0);
            chrome.UseAeroCaptionButtons = false;
            WindowChrome.SetWindowChrome(this, chrome);

            Content = BuildWindow();

            if (!demoMode && !qaMode)
            {
                _trayIcon = new TrayIconHost(
                    delegate { Dispatcher.BeginInvoke(new Action(ShowFromTray)); },
                    delegate { Dispatcher.BeginInvoke(new Action(RequestExit)); });
            }

            SourceInitialized += OnSourceInitialized;
            Loaded += OnLoaded;
            ContentRendered += OnContentRendered;
            Closing += OnClosing;
            Closed += OnClosed;
            StateChanged += OnWindowStateChanged;
            IsVisibleChanged += delegate
            {
                if (IsVisible && _visualsDirty)
                {
                    FlushPendingUi();
                    RenderList(false);
                }
            };
            PreviewKeyDown += OnPreviewKeyDown;

            if (_source != null)
            {
                _source.PreviewCaptured += OnPreviewCaptured;
                _source.StateChanged += OnCaptureStateChanged;
            }
        }

        private UIElement BuildWindow() { return BuildRoomWindow(); }

        private Border BuildTitleBar()
        {
            Border bar = new Border();
            bar.Background = MakeBrush(Tokens.Palette.Canvas);
            bar.BorderBrush = MakeBrush(Tokens.Palette.Divider);
            bar.BorderThickness = new Thickness(0, 0, 0, 1);
            bar.MouseLeftButtonDown += OnTitleBarMouseDown;

            Grid grid = new Grid();
            grid.Margin = new Thickness(Tokens.Space.TitleBarLeft, 0, Tokens.Space.TitleBarRight, 0);
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

            StackPanel brand = new StackPanel();
            brand.Orientation = Orientation.Horizontal;
            brand.VerticalAlignment = VerticalAlignment.Center;

            BitmapSource icon = AssetLoader.LoadIconBitmap();
            if (icon != null)
            {
                Image image = new Image();
                image.Source = icon;
                image.Width = Tokens.Size.TitleIcon;
                image.Height = Tokens.Size.TitleIcon;
                image.Stretch = Stretch.Uniform;
                RenderOptions.SetBitmapScalingMode(image, BitmapScalingMode.HighQuality);
                brand.Children.Add(image);
            }

            TextBlock title = new TextBlock();
            title.Text = "Spoiler";
            title.FontFamily = UiFont;
            title.FontWeight = FontWeights.SemiBold;
            title.FontSize = Tokens.TypeScale.WindowTitle;
            title.Foreground = MakeBrush(Tokens.Palette.InkStrong);
            title.VerticalAlignment = VerticalAlignment.Center;
            title.Margin = new Thickness(9, 0, 0, 1);
            brand.Children.Add(title);

            Grid.SetColumn(brand, 0);
            grid.Children.Add(brand);

            StackPanel controls = new StackPanel();
            controls.Orientation = Orientation.Horizontal;
            controls.HorizontalAlignment = HorizontalAlignment.Right;

            TactileButton minimize = MakeTitleButton("\uE921", "최소화");
            minimize.Click += delegate { SystemCommands.MinimizeWindow(this); };
            controls.Children.Add(minimize);

            _maximizeButton = MakeTitleButton("\uE922", "최대화");
            _maximizeGlyph = _maximizeButton.Content as TextBlock;
            _maximizeButton.Click += delegate
            {
                if (WindowState == WindowState.Maximized)
                {
                    SystemCommands.RestoreWindow(this);
                }
                else
                {
                    SystemCommands.MaximizeWindow(this);
                }
            };
            controls.Children.Add(_maximizeButton);

            TactileButton close = MakeTitleButton("\uE8BB", "닫기");
            close.Template = TactileButton.CreateRoundedTemplate(Tokens.Radius.TitleAction, MakeBrush(Tokens.Palette.DangerSurface));
            close.ToolTip = "닫기 · 백그라운드에서 계속 받기";
            AutomationProperties.SetHelpText(close, "창을 숨긴 뒤 알림 영역에서 메시지를 계속 받습니다.");
            close.Click += delegate { Close(); };
            close.MouseEnter += delegate { close.Background = MakeBrush(Tokens.Palette.DangerSurface); close.Foreground = MakeBrush(Tokens.Palette.DangerInk); };
            close.MouseLeave += delegate { close.Background = Brushes.Transparent; close.Foreground = MakeBrush(Tokens.Palette.InkBody); };
            controls.Children.Add(close);

            Grid.SetColumn(controls, 1);
            grid.Children.Add(controls);
            bar.Child = grid;
            return bar;
        }

        private Border BuildFinderPanel()
        {
            Border panel = new Border();
            panel.Name = "TimeFinderPanel";
            panel.Visibility = Visibility.Collapsed;
            panel.Margin = new Thickness(0, Tokens.Space.Sm, 0, Tokens.Space.Sm);
            panel.Padding = new Thickness(Tokens.Space.Xl);
            panel.Background = MakeBrush(Tokens.Palette.SurfaceSubtle);
            panel.BorderBrush = MakeBrush(Tokens.Palette.DividerStrong);
            panel.BorderThickness = new Thickness(1);
            panel.CornerRadius = new CornerRadius(Tokens.Radius.Notice);

            StackPanel content = new StackPanel();

            StackPanel titleLine = new StackPanel();
            titleLine.Orientation = Orientation.Horizontal;
            titleLine.VerticalAlignment = VerticalAlignment.Center;
            TextBlock searchGlyph = MakeIconText("\uE721", 17, Tokens.Palette.Icon);
            searchGlyph.Margin = new Thickness(0, 0, 9, 0);
            titleLine.Children.Add(searchGlyph);
            TextBlock title = new TextBlock();
            title.Text = "받은 시각으로 찾기";
            title.FontFamily = UiFont;
            title.FontSize = Tokens.TypeScale.Metadata;
            title.FontWeight = FontWeights.SemiBold;
            title.Foreground = MakeBrush(Tokens.Palette.Ink);
            titleLine.Children.Add(title);
            content.Children.Add(titleLine);

            TextBlock helper = new KoreanGuideTextBlock();
            helper.Text = "오전·오후와 시·분이 모두 같은 메시지만 보여드려요.";
            helper.FontFamily = UiFont;
            helper.FontSize = Tokens.TypeScale.Small;
            helper.Foreground = MakeBrush(Tokens.Palette.InkSecondary);
            helper.Margin = new Thickness(26, 3, 0, 0);
            helper.TextWrapping = TextWrapping.Wrap;
            content.Children.Add(helper);

            WrapPanel controls = new WrapPanel();
            controls.Margin = new Thickness(0, Tokens.Space.Lg, 0, 0);
            controls.VerticalAlignment = VerticalAlignment.Bottom;

            StackPanel periodField = new StackPanel();
            periodField.Margin = new Thickness(0, 0, Tokens.Space.Md, 0);
            periodField.Children.Add(MakeFieldLabel("구분"));
            Border periodShell = new Border();
            periodShell.Height = Tokens.Size.FieldHeight;
            periodShell.Padding = new Thickness(3);
            periodShell.Background = MakeBrush(Tokens.Palette.SurfaceControl);
            periodShell.BorderBrush = MakeBrush(Tokens.Palette.DividerStrong);
            periodShell.BorderThickness = new Thickness(1);
            periodShell.CornerRadius = new CornerRadius(Tokens.Radius.Field);
            StackPanel periodButtons = new StackPanel();
            periodButtons.Orientation = Orientation.Horizontal;
            _amButton = MakePeriodButton("오전");
            _amButton.Click += delegate { SetSearchPeriod(false); };
            _pmButton = MakePeriodButton("오후");
            _pmButton.Click += delegate { SetSearchPeriod(true); };
            periodButtons.Children.Add(_amButton);
            periodButtons.Children.Add(_pmButton);
            periodShell.Child = periodButtons;
            periodField.Children.Add(periodShell);
            controls.Children.Add(periodField);

            FrameworkElement hourField = BuildTimeInput("시간", "시간 입력, 1부터 12", out _hourInput);
            hourField.Margin = new Thickness(0, 0, Tokens.Space.Md, 0);
            controls.Children.Add(hourField);

            FrameworkElement minuteField = BuildTimeInput("분", "분 입력, 0부터 59", out _minuteInput);
            minuteField.Margin = new Thickness(0, 0, Tokens.Space.Lg, 0);
            controls.Children.Add(minuteField);

            TactileButton runSearch = MakeToolbarButton("\uE721", "메시지 찾기");
            runSearch.Height = Tokens.Size.FieldHeight;
            runSearch.MinWidth = 126;
            runSearch.Margin = new Thickness(0, 21, Tokens.Space.Sm, 0);
            runSearch.Background = MakeBrush(Tokens.Palette.Accent);
            runSearch.Foreground = MakeBrush(Tokens.Palette.AccentInk);
            runSearch.Template = TactileButton.CreateRoundedTemplate(Tokens.Radius.Action, MakeBrush(Tokens.Palette.AccentHover));
            runSearch.Click += delegate { RunTimeSearch(); };
            AutomationProperties.SetName(runSearch, "입력한 시각으로 메시지 찾기");
            controls.Children.Add(runSearch);

            _clearSearchButton = MakeToolbarButton("\uE711", "전체 보기");
            _clearSearchButton.Height = Tokens.Size.FieldHeight;
            _clearSearchButton.MinWidth = 104;
            _clearSearchButton.Margin = new Thickness(0, 21, 0, 0);
            _clearSearchButton.Visibility = Visibility.Collapsed;
            _clearSearchButton.Click += delegate { ClearTimeSearch(); };
            AutomationProperties.SetName(_clearSearchButton, "시간 찾기 해제하고 전체 보기");
            controls.Children.Add(_clearSearchButton);
            content.Children.Add(controls);

            _finderFeedback = new KoreanGuideTextBlock();
            _finderFeedback.Text = "오후가 기본값이에요. 시간은 1–12, 분은 0–59로 입력해 주세요.";
            _finderFeedback.FontFamily = UiFont;
            _finderFeedback.FontSize = Tokens.TypeScale.Small;
            _finderFeedback.Foreground = MakeBrush(Tokens.Palette.InkMuted);
            _finderFeedback.Margin = new Thickness(0, Tokens.Space.Md, 0, 0);
            _finderFeedback.TextWrapping = TextWrapping.Wrap;
            AutomationProperties.SetLiveSetting(_finderFeedback, AutomationLiveSetting.Polite);
            content.Children.Add(_finderFeedback);

            panel.Child = content;
            AutomationProperties.SetName(panel, "받은 시각으로 메시지 찾기 패널");
            SetSearchPeriod(true);
            return panel;
        }

        private void ToggleFinderPanel()
        {
            if (_finderPanel == null)
            {
                return;
            }

            _finderExpanded = !_finderExpanded;
            _finderPanel.Visibility = _finderExpanded ? Visibility.Visible : Visibility.Collapsed;
            AutomationProperties.SetHelpText(
                _findToggleButton,
                _finderExpanded ? "시간 찾기 패널이 열려 있습니다." : "시간 찾기 패널이 닫혀 있습니다.");

            if (_finderExpanded)
            {
                FadeIn(_finderPanel, 0, 180, 6);
                Dispatcher.BeginInvoke(new Action(delegate
                {
                    if (_hourInput != null)
                    {
                        _hourInput.Focus();
                        _hourInput.SelectAll();
                    }
                }), System.Windows.Threading.DispatcherPriority.Input);
            }
        }

        private void SetSearchPeriod(bool afternoon)
        {
            _searchAfternoon = afternoon;
            UpdatePeriodButton(_amButton, !afternoon, "오전");
            UpdatePeriodButton(_pmButton, afternoon, "오후");
        }

        private static void UpdatePeriodButton(TactileButton button, bool selected, string label)
        {
            if (button == null)
            {
                return;
            }

            string background = selected ? Tokens.Palette.Accent : Tokens.Palette.SurfaceControl;
            string foreground = selected ? Tokens.Palette.AccentInk : Tokens.Palette.InkBody;
            button.Background = MakeBrush(background);
            button.Foreground = MakeBrush(foreground);
            button.Template = TactileButton.CreateRoundedTemplate(
                Tokens.Radius.Field - 3,
                MakeBrush(selected ? Tokens.Palette.AccentHover : Tokens.Palette.SurfaceControlHover));
            TextBlock text = button.Content as TextBlock;
            if (text != null)
            {
                text.Foreground = MakeBrush(foreground);
            }
            AutomationProperties.SetName(button, label + (selected ? " 선택됨" : " 선택"));
            AutomationProperties.SetHelpText(button, selected ? "현재 선택" : "선택하려면 누르세요");
        }

        private void OnFinderInputKeyDown(object sender, KeyEventArgs args)
        {
            if (args.Key == Key.Enter)
            {
                RunTimeSearch();
                args.Handled = true;
            }
        }

        private void RunTimeSearch()
        {
            int hour;
            if (_hourInput == null || !int.TryParse(_hourInput.Text.Trim(), out hour) || hour < 1 || hour > 12)
            {
                SetFinderFeedback("시간은 1부터 12 사이로 입력해 주세요.", true);
                if (_hourInput != null)
                {
                    _hourInput.Focus();
                    _hourInput.SelectAll();
                }
                return;
            }

            int minute;
            if (_minuteInput == null || !int.TryParse(_minuteInput.Text.Trim(), out minute) || minute < 0 || minute > 59)
            {
                SetFinderFeedback("분은 0부터 59 사이로 입력해 주세요.", true);
                if (_minuteInput != null)
                {
                    _minuteInput.Focus();
                    _minuteInput.SelectAll();
                }
                return;
            }

            _searchHour = hour;
            _searchMinute = minute;
            _committedSearchAfternoon = _searchAfternoon;
            _searchActive = true;
            if (_clearSearchButton != null)
            {
                _clearSearchButton.Visibility = Visibility.Visible;
            }

            IList<NotificationPreview> matches = GetVisibleItems();
            string query = SearchTimeLabel();
            SetFinderFeedback(
                matches.Count == 0
                    ? query + "에 저장된 메시지가 없어요. 오전·오후와 분을 다시 확인해 주세요."
                    : query + "에 받은 메시지 " + matches.Count + "개를 찾았어요.",
                matches.Count == 0);
            RenderList(false);
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (_listHost != null)
                {
                    _listHost.BringIntoView();
                }
            }), System.Windows.Threading.DispatcherPriority.Loaded);
        }

        private void ApplyMineFilter(bool hide)
        {
            _hideMine = hide;
            if (_searchActive)
            {
                IList<NotificationPreview> matches = GetVisibleItems();
                SetFinderFeedback(
                    matches.Count == 0
                        ? SearchTimeLabel() + "에 표시할 받은 메시지가 없어요. 숨김 조건도 확인해 주세요."
                        : SearchTimeLabel() + "에 받은 메시지 " + matches.Count + "개를 찾았어요.",
                    matches.Count == 0);
            }
            RenderList(false);
        }

        private void ClearTimeSearch()
        {
            _searchActive = false;
            if (_clearSearchButton != null)
            {
                _clearSearchButton.Visibility = Visibility.Collapsed;
            }
            SetFinderFeedback("오후가 기본값이에요. 시간은 1–12, 분은 0–59로 입력해 주세요.", false);
            RenderList(false);
        }

        private void SetFinderFeedback(string message, bool isError)
        {
            if (_finderFeedback == null)
            {
                return;
            }
            _finderFeedback.Text = message ?? string.Empty;
            _finderFeedback.Foreground = MakeBrush(isError ? Tokens.Palette.DangerInk : Tokens.Palette.InkMuted);
        }

        private string SearchTimeLabel()
        {
            return (_committedSearchAfternoon ? "오후 " : "오전 ")
                + _searchHour.ToString(CultureInfo.InvariantCulture)
                + ":"
                + _searchMinute.ToString("00", CultureInfo.InvariantCulture);
        }

        private IList<NotificationPreview> GetVisibleItems()
        {
            IEnumerable<NotificationPreview> visible = _items.Where(item => item != null);
            if (_hideMine)
            {
                visible = visible.Where(item => !item.IsMine);
            }
            if (_searchActive)
            {
                int targetHour = (_searchHour % 12) + (_committedSearchAfternoon ? 12 : 0);
                visible = visible.Where(item => item.ReceivedAt.Hour == targetHour && item.ReceivedAt.Minute == _searchMinute);
            }
            return visible.OrderByDescending(item => item.ReceivedAt).ToList();
        }

        private static Style BuildSlimScrollBarStyle()
        {
            string thumbColor = SystemParameters.HighContrast
                ? SystemColors.ControlTextColor.ToString()
                : Tokens.Palette.ScrollThumb;
            string thumbHoverColor = SystemParameters.HighContrast
                ? SystemColors.HighlightColor.ToString()
                : Tokens.Palette.ScrollThumbHover;
            string xaml =
                "<Style xmlns=\"http://schemas.microsoft.com/winfx/2006/xaml/presentation\" " +
                "xmlns:x=\"http://schemas.microsoft.com/winfx/2006/xaml\" TargetType=\"{x:Type ScrollBar}\">" +
                "<Setter Property=\"Width\" Value=\"12\"/>" +
                "<Setter Property=\"MinWidth\" Value=\"12\"/>" +
                "<Setter Property=\"Background\" Value=\"Transparent\"/>" +
                "<Setter Property=\"Template\"><Setter.Value>" +
                "<ControlTemplate TargetType=\"{x:Type ScrollBar}\">" +
                "<Grid Width=\"12\" Background=\"Transparent\">" +
                "<Track x:Name=\"PART_Track\" IsDirectionReversed=\"True\" Focusable=\"False\">" +
                "<Track.DecreaseRepeatButton><RepeatButton Command=\"{x:Static ScrollBar.PageUpCommand}\" Opacity=\"0\" Focusable=\"False\"/></Track.DecreaseRepeatButton>" +
                "<Track.Thumb><Thumb MinHeight=\"36\" Focusable=\"False\"><Thumb.Template>" +
                "<ControlTemplate TargetType=\"{x:Type Thumb}\"><Grid Background=\"Transparent\"><Border x:Name=\"ThumbChrome\" Width=\"5\" Margin=\"3,1\" CornerRadius=\"2.5\" Background=\"" + thumbColor + "\"/></Grid>" +
                "<ControlTemplate.Triggers><Trigger Property=\"IsMouseOver\" Value=\"True\"><Setter TargetName=\"ThumbChrome\" Property=\"Background\" Value=\"" + thumbHoverColor + "\"/></Trigger></ControlTemplate.Triggers>" +
                "</ControlTemplate></Thumb.Template></Thumb></Track.Thumb>" +
                "<Track.IncreaseRepeatButton><RepeatButton Command=\"{x:Static ScrollBar.PageDownCommand}\" Opacity=\"0\" Focusable=\"False\"/></Track.IncreaseRepeatButton>" +
                "</Track></Grid>" +
                "<ControlTemplate.Triggers><Trigger Property=\"IsEnabled\" Value=\"False\"><Setter Property=\"Opacity\" Value=\"0\"/></Trigger></ControlTemplate.Triggers>" +
                "</ControlTemplate></Setter.Value></Setter></Style>";
            return (Style)XamlReader.Parse(xaml);
        }

        private void RenderList(bool animateNewest) { RenderRooms(animateNewest); }

        private TactileButton MakeToolbarButton(string glyph, string label)
        {
            TactileButton button = new TactileButton();
            button.Height = Tokens.Size.MinimumHit;
            button.MinHeight = Tokens.Size.MinimumHit;
            button.MinWidth = 94;
            button.Padding = new Thickness(14, 0, 15, 0);
            button.Background = MakeBrush(Tokens.Palette.SurfaceControl);
            button.Foreground = MakeBrush(Tokens.Palette.Ink);
            button.BorderBrush = MakeBrush(Tokens.Palette.DividerStrong);
            button.BorderThickness = new Thickness(1);
            button.Template = TactileButton.CreateRoundedTemplate(Tokens.Radius.Action, MakeBrush(Tokens.Palette.SurfaceControlHover));

            StackPanel content = new StackPanel();
            content.Orientation = Orientation.Horizontal;
            content.HorizontalAlignment = HorizontalAlignment.Center;
            content.VerticalAlignment = VerticalAlignment.Center;
            TextBlock icon = MakeIconText(glyph, 16, Tokens.Palette.Icon);
            icon.Margin = new Thickness(0, 0, 8, 0);
            content.Children.Add(icon);
            TextBlock text = new TextBlock();
            text.Text = label;
            text.FontFamily = UiFont;
            text.FontSize = Tokens.TypeScale.Small;
            text.FontWeight = FontWeights.SemiBold;
            text.VerticalAlignment = VerticalAlignment.Center;
            content.Children.Add(text);
            button.Content = content;
            AutomationProperties.SetName(button, label);
            return button;
        }

        private TactileButton MakePeriodButton(string label)
        {
            TactileButton button = new TactileButton();
            button.Width = 64;
            button.Height = Tokens.Size.FieldHeight - 6;
            button.Padding = new Thickness(8, 0, 8, 0);
            button.BorderThickness = new Thickness(0);
            TextBlock text = new TextBlock();
            text.Text = label;
            text.FontFamily = UiFont;
            text.FontSize = Tokens.TypeScale.Small;
            text.FontWeight = FontWeights.SemiBold;
            text.VerticalAlignment = VerticalAlignment.Center;
            button.Content = text;
            return button;
        }

        private static TextBlock MakeFieldLabel(string label)
        {
            TextBlock text = new TextBlock();
            text.Text = label;
            text.FontFamily = UiFont;
            text.FontSize = Tokens.TypeScale.FieldLabel;
            text.FontWeight = FontWeights.SemiBold;
            text.Foreground = MakeBrush(Tokens.Palette.InkBody);
            text.Margin = new Thickness(2, 0, 0, 5);
            return text;
        }

        private FrameworkElement BuildTimeInput(string label, string automationName, out TextBox input)
        {
            StackPanel field = new StackPanel();
            field.Width = 82;
            field.Children.Add(MakeFieldLabel(label));

            input = new TextBox();
            input.Height = Tokens.Size.FieldHeight - 2;
            input.MaxLength = 2;
            input.Padding = new Thickness(10, 0, 10, 0);
            input.VerticalContentAlignment = VerticalAlignment.Center;
            input.HorizontalContentAlignment = HorizontalAlignment.Center;
            input.TextAlignment = TextAlignment.Center;
            input.FontFamily = UiFont;
            input.FontSize = Tokens.TypeScale.Status;
            input.Foreground = MakeBrush(Tokens.Palette.Ink);
            input.Background = Brushes.Transparent;
            input.BorderThickness = new Thickness(0);
            input.KeyDown += OnFinderInputKeyDown;
            AutomationProperties.SetName(input, automationName);
            AutomationProperties.SetHelpText(input, automationName);

            Border shell = new Border();
            shell.Height = Tokens.Size.FieldHeight;
            shell.Background = MakeBrush(Tokens.Palette.Surface);
            shell.BorderBrush = MakeBrush(Tokens.Palette.DividerStrong);
            shell.BorderThickness = new Thickness(1);
            shell.CornerRadius = new CornerRadius(Tokens.Radius.Field);
            shell.Child = input;
            input.GotKeyboardFocus += delegate
            {
                shell.BorderBrush = MakeBrush(Tokens.Palette.Focus);
                shell.BorderThickness = new Thickness(2);
            };
            input.LostKeyboardFocus += delegate
            {
                shell.BorderBrush = MakeBrush(Tokens.Palette.DividerStrong);
                shell.BorderThickness = new Thickness(1);
            };
            field.Children.Add(shell);
            return field;
        }

        private TactileButton MakeIconButton(string glyph, string tooltip)
        {
            TactileButton button = new TactileButton();
            button.Width = Tokens.Size.Action;
            button.Height = Tokens.Size.Action;
            button.Background = MakeBrush(Tokens.Palette.SurfaceControl);
            button.Foreground = MakeBrush(Tokens.Palette.IconMuted);
            button.BorderThickness = new Thickness(0);
            button.Template = TactileButton.CreateRoundedTemplate(Tokens.Radius.Action, MakeBrush(Tokens.Palette.SurfaceControlHover));
            button.Content = MakeIconText(glyph, Tokens.TypeScale.WindowTitle, Tokens.Palette.IconMuted);
            button.ToolTip = tooltip;
            AutomationProperties.SetName(button, tooltip);
            return button;
        }

        private TactileButton MakeTitleButton(string glyph, string tooltip)
        {
            TactileButton button = new TactileButton();
            button.Width = Tokens.Room.TitleAction;
            button.Height = Tokens.Room.TitleAction;
            button.MinHeight = 0;
            button.Background = Brushes.Transparent;
            button.Foreground = MakeBrush(Tokens.Palette.InkBody);
            button.BorderThickness = new Thickness(0);
            button.Template = TactileButton.CreateRoundedTemplate(Tokens.Radius.TitleAction, MakeBrush(Tokens.Palette.SurfaceMuted));
            TextBlock icon = MakeIconText(glyph, Tokens.TypeScale.Small, Tokens.Palette.InkBody);
            button.Content = icon;
            button.ToolTip = tooltip;
            AutomationProperties.SetName(button, tooltip);
            WindowChrome.SetIsHitTestVisibleInChrome(button, true);
            return button;
        }

        private static TextBlock MakeIconText(string glyph, double size, string color)
        {
            TextBlock text = new TextBlock();
            text.Text = glyph;
            text.FontFamily = IconFont;
            text.FontSize = size;
            text.Foreground = MakeBrush(color);
            text.HorizontalAlignment = HorizontalAlignment.Center;
            text.VerticalAlignment = VerticalAlignment.Center;
            text.TextAlignment = TextAlignment.Center;
            return text;
        }

        private void OnLoaded(object sender, RoutedEventArgs args)
        {
            _uiBatchTimer.Start();
            AnimateInitialLayout();
            if (_source == null)
            {
                return;
            }

            if (_vault.Load() == null)
            {
                _statusText.Text = "태블릿 보조기기를 등록해 주세요";
                Dispatcher.BeginInvoke(new Action(OpenRegistration));
                return;
            }

            _source.Start();
        }

        private void ConnectOrRegister()
        {
            if (_source == null)
            {
                return;
            }

            if (_vault.Load() == null)
            {
                OpenRegistration();
                return;
            }

            _statusText.Text = "태블릿 보조기기에 다시 연결하는 중";
            _source.Stop();
            RefreshAccountScope();
            _source.Start();
        }

        private void OpenRegistration()
        {
            if (_source == null || !IsLoaded)
            {
                return;
            }

            _scopeTransition = true;
            _source.Stop();
            ResetDisplayedInbox(null);
            KakaoRegistrationWindow registration = new KakaoRegistrationWindow(_vault);
            registration.Owner = this;
            registration.RegistrationCompleted += delegate
            {
                registration.Dispatcher.BeginInvoke(new Action(delegate
                {
                    if (registration.IsVisible)
                    {
                        registration.Close();
                    }
                }));
            };

            try
            {
                registration.ShowDialog();
            }
            finally
            {
                RefreshAccountScope();
                _scopeTransition = false;
            }
            if (registration.WasRegistered)
            {
                _statusText.Text = "태블릿 등록 완료 · 서버에 연결하는 중";
                _source.Start();
            }
            else if (_vault.Load() == null)
            {
                _statusText.Text = "태블릿 보조기기를 등록해 주세요";
            }
            else
            {
                _source.Start();
            }
        }

        private void OnContentRendered(object sender, EventArgs args)
        {
            if (!_demoMode && !_qaMode)
            {
                return;
            }
            string screenshotPath = Environment.GetEnvironmentVariable("SPOILER_SCREENSHOT_PATH");
            if (string.IsNullOrWhiteSpace(screenshotPath))
            {
                return;
            }

            ContentRendered -= OnContentRendered;
            _screenshotTimer = new System.Windows.Threading.DispatcherTimer
            {
                Interval = TimeSpan.FromMilliseconds(950)
            };
            _screenshotTimer.Tick += delegate
            {
                _screenshotTimer.Stop();
                try
                {
                    if (string.Equals(Environment.GetEnvironmentVariable("SPOILER_SCREENSHOT_FOCUS"), "refresh", StringComparison.OrdinalIgnoreCase)
                        && _refreshButton != null)
                    {
                        _refreshButton.Focus();
                        Keyboard.Focus(_refreshButton);
                    }
                    if (string.Equals(Environment.GetEnvironmentVariable("SPOILER_SCREENSHOT_SCROLL"), "bottom", StringComparison.OrdinalIgnoreCase)
                        && _contentScroll != null)
                    {
                        _contentScroll.ScrollToEnd();
                    }
                    UpdateLayout();
                    int width = Math.Max(1, (int)Math.Ceiling(ActualWidth));
                    int height = Math.Max(1, (int)Math.Ceiling(ActualHeight));
                    RenderTargetBitmap bitmap = new RenderTargetBitmap(width, height, 96, 96, PixelFormats.Pbgra32);
                    bitmap.Render(this);

                    string directory = System.IO.Path.GetDirectoryName(screenshotPath);
                    if (!string.IsNullOrEmpty(directory))
                    {
                        Directory.CreateDirectory(directory);
                    }

                    PngBitmapEncoder encoder = new PngBitmapEncoder();
                    encoder.Frames.Add(BitmapFrame.Create(bitmap));
                    using (FileStream stream = File.Create(screenshotPath))
                    {
                        encoder.Save(stream);
                    }
                    TraceQa("ui-screenshot-saved=" + width + "x" + height);
                }
                catch (Exception exception)
                {
                    TraceQa("ui-screenshot-failed=" + exception.GetType().Name);
                }
            };
            _screenshotTimer.Start();
        }

        private void AnimateInitialLayout()
        {
            FadeIn(_headerBlock, 0, 180, 0);
            FadeIn(_sectionHeading, 0, 180, 0);
        }

        private static void FadeIn(FrameworkElement element, int delayMs, int durationMs, double offset)
        {
            if (element == null)
            {
                return;
            }

            // Show controls immediately; opening a panel needs no animation.
            element.BeginAnimation(OpacityProperty, null);
            element.Opacity = 1;
            element.RenderTransform = Transform.Identity;
        }

        private void OnPreviewCaptured(object sender, NotificationPreview preview)
        {
            if (preview == null)
            {
                return;
            }
            string sourceAccount = _source == null ? _displayAccountId : _source.CurrentAccountId;
            lock (_pendingSync)
            {
                if (_exitRequested || !IsPreviewForAccount(preview, sourceAccount))
                {
                    return;
                }
                // Coalesce replacements before the cap chooses retained rows.
                // Otherwise a large rename batch can keep the older title.
                _pendingPreviews.RemoveAll(entry => entry.Generation == _uiGeneration
                    && SameConversationMessage(entry.Preview, preview));
                _pendingPreviews.Add(new PendingPreview { Preview = preview, Generation = _uiGeneration });
                if (_pendingPreviews.Count > InboxStore.MaxMessages)
                {
                    HashSet<NotificationPreview> keep = new HashSet<NotificationPreview>(
                        InboxStore.RetainMessages(_pendingPreviews.Select(entry => entry.Preview)));
                    _pendingPreviews.RemoveAll(entry => !keep.Contains(entry.Preview));
                }
            }
            TraceQa("ui-preview-enqueued");
        }

        private bool IsCurrentPreview(NotificationPreview preview)
        {
            string sourceAccount = _source == null ? _displayAccountId : _source.CurrentAccountId;
            return IsPreviewForAccount(preview, sourceAccount);
        }

        private bool IsPreviewForAccount(NotificationPreview preview, string sourceAccount)
        {
            return string.Equals(preview.AccountId, _displayAccountId, StringComparison.Ordinal)
                && string.Equals(preview.AccountId, sourceAccount, StringComparison.Ordinal);
        }

        private void RefreshAccountScope()
        {
            if (_source == null)
            {
                return;
            }
            ResetDisplayedInbox(_source.CurrentAccountId);
            IList<NotificationPreview> stored = _source.LoadStoredMessages();
            if (!string.Equals(_displayAccountId, _source.CurrentAccountId, StringComparison.Ordinal))
            {
                ResetDisplayedInbox(_source.CurrentAccountId);
            }
            _items.AddRange(stored.Where(IsCurrentPreview));
            RenderList(false);
        }

        private void ResetDisplayedInbox(string accountId)
        {
            lock (_pendingSync)
            {
                _uiGeneration++;
                _displayAccountId = accountId;
                _pendingPreviews.Clear();
                _pendingState = null;
            }
            _items.Clear();
            ResetRoomUiState();
            _searchActive = false;
            if (_clearSearchButton != null)
            {
                _clearSearchButton.Visibility = Visibility.Collapsed;
            }
            SetFinderFeedback("오후가 기본값이에요. 시간은 1–12, 분은 0–59로 입력해 주세요.", false);
            RenderList(false);
        }

        // One dispatcher update per burst. All durable writes have already
        // succeeded in the receiver before an item reaches this queue.
        internal void FlushPendingUi()
        {
            if (_scopeTransition || _exitRequested)
            {
                return;
            }
            // Never acquire the receiver's lock while holding _pendingSync:
            // source callbacks may run while its account snapshot is locked.
            string sourceAccount = _source == null ? _displayAccountId : _source.CurrentAccountId;
            if (_source != null && !string.Equals(sourceAccount, _displayAccountId, StringComparison.Ordinal))
            {
                RefreshAccountScope();
                sourceAccount = _source.CurrentAccountId;
            }
            List<NotificationPreview> incoming;
            CaptureStateChangedEventArgs state;
            int generation;
            lock (_pendingSync)
            {
                generation = _uiGeneration;
                incoming = _pendingPreviews.Where(entry => entry.Generation == generation && IsPreviewForAccount(entry.Preview, sourceAccount))
                    .Select(entry => entry.Preview).ToList();
                _pendingPreviews.Clear();
                state = _pendingStateGeneration == generation
                    && string.Equals(_pendingStateAccountId, _displayAccountId, StringComparison.Ordinal)
                    && string.Equals(sourceAccount, _displayAccountId, StringComparison.Ordinal)
                    ? _pendingState : null;
                _pendingState = null;
            }
            if (state != null)
            {
                ApplyCaptureState(state);
            }
            bool pruneDue = DateTime.UtcNow - _lastRetentionCheckUtc >= TimeSpan.FromMinutes(1);
            if (incoming.Count == 0 && !pruneDue)
            {
                return;
            }
            // A recovered duplicate can supply verified room metadata for a
            // previously unclassified row. Replace that row in place rather
            // than keeping a second bubble or resetting drafts and selection.
            List<NotificationPreview> retained = InboxUiMerge.Merge(_items, incoming);
            bool changed = !_items.SequenceEqual(retained);
            _lastRetentionCheckUtc = DateTime.UtcNow;
            if (!changed)
            {
                return;
            }
            string focusedGroupKey = IsVisible ? GetFocusedGroupKey() : null;
            _items.Clear();
            _items.AddRange(retained);
            _visualsDirty = true;
            UpdateSearchFeedback();
            if (!IsVisible || WindowState == WindowState.Minimized)
            {
                return;
            }
            RenderList(incoming.Count > 0);
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (generation != _uiGeneration || _exitRequested)
                {
                    return;
                }
                RestoreGroupFocus(focusedGroupKey);
            }), System.Windows.Threading.DispatcherPriority.Loaded);
            TraceQa("ui-batch-count=" + incoming.Count);
        }

        private void UpdateSearchFeedback()
        {
            if (!_searchActive)
            {
                return;
            }
            int count = GetVisibleItems().Count;
            SetFinderFeedback(count == 0
                ? SearchTimeLabel() + "에 표시할 받은 메시지가 없어요. 숨김 조건도 확인해 주세요."
                : SearchTimeLabel() + "에 받은 메시지 " + count + "개를 찾았어요.", count == 0);
        }

        private string GetFocusedGroupKey()
        {
            ListBoxItem item = Keyboard.FocusedElement as ListBoxItem;
            ChatRoom room = item == null ? null : item.Tag as ChatRoom;
            return room == null ? null : room.Key;
        }

        private void RestoreGroupFocus(string groupKey)
        {
            if (groupKey == null || _roomList == null) return;
            ListBoxItem item = _roomList.Items.Cast<ListBoxItem>().FirstOrDefault(value => ((ChatRoom)value.Tag).Key == groupKey);
            if (item != null) item.Focus();
        }

        private void OnCaptureStateChanged(object sender, CaptureStateChangedEventArgs args)
        {
            if (args == null)
            {
                return;
            }
            TraceQa("ui-state=" + args.State);
            string sourceAccount = _source == null ? _displayAccountId : _source.CurrentAccountId;
            lock (_pendingSync)
            {
                if (_exitRequested)
                {
                    return;
                }
                _pendingState = args;
                _pendingStateGeneration = _uiGeneration;
                _pendingStateAccountId = sourceAccount;
            }
        }

        private void ApplyCaptureState(CaptureStateChangedEventArgs args)
        {
                switch (args.State)
                {
                    case CaptureState.Watching:
                        _statusText.Text = string.IsNullOrWhiteSpace(args.Detail)
                            ? "카카오톡 서버에 연결됨 · 읽음 신호 없음"
                            : args.Detail;
                        break;
                    case CaptureState.Captured:
                        _statusText.Text = string.IsNullOrWhiteSpace(args.Detail)
                            ? "새 메시지를 받았어요 · 읽음 신호 없음"
                            : args.Detail;
                        break;
                    case CaptureState.Starting:
                        _statusText.Text = string.IsNullOrWhiteSpace(args.Detail)
                            ? "태블릿 보조기기에 연결하는 중"
                            : args.Detail;
                        break;
                    case CaptureState.Error:
                        _statusText.Text = string.IsNullOrWhiteSpace(args.Detail)
                            ? "태블릿 연결을 확인해 주세요"
                            : args.Detail;
                        break;
                    default:
                        _statusText.Text = "태블릿 연결 확인 중";
                        break;
                }
                UpdateReplyAvailability();
        }

        private void OnClearHistory(object sender, RoutedEventArgs args)
        {
            MessageBoxResult answer = MessageBox.Show(
                this,
                "Spoiler에 암호화해 둔 메시지 기록을 모두 지울까요?\n카카오톡의 원본 대화에는 영향을 주지 않습니다.",
                "기록 지우기",
                MessageBoxButton.YesNo,
                MessageBoxImage.Question);

            if (answer != MessageBoxResult.Yes)
            {
                return;
            }

            if (!ClearDisplayedHistory())
            {
                MessageBox.Show(
                    this,
                    "암호화된 기록 파일을 지우지 못했습니다.\nSpoiler를 종료한 뒤 다시 시도해 주세요.",
                    "기록을 지우지 못했어요",
                    MessageBoxButton.OK,
                    MessageBoxImage.Warning);
                return;
            }

        }

        internal bool ClearDisplayedHistory()
        {
            // Invalidate queued updates before the receiver stops and clears
            // its inbox; callbacks from the previous generation cannot replay.
            lock (_pendingSync)
            {
                _uiGeneration++;
                _pendingPreviews.Clear();
                _pendingState = null;
            }
            if (_source != null && !_source.ClearHistory())
            {
                return false;
            }
            if (_source != null)
            {
                RefreshAccountScope();
            }
            else
            {
                ResetDisplayedInbox(_displayAccountId);
            }
            return true;
        }

        private static List<NotificationPreview> CreateDemoItems()
        {
            DateTime anchor = DateTime.Today.AddHours(16).AddMinutes(53);
            bool showLongMessage = string.Equals(
                Environment.GetEnvironmentVariable("SPOILER_QA_LONG_MESSAGE"),
                "1",
                StringComparison.Ordinal);
            string firstMessage = showLongMessage
                ? "장문 표시 확인용 메시지입니다. 한두 줄만 보여 주고 자르는 대신, 받은 내용을 끝까지 그대로 펼쳐서 보여 줍니다. 문장이 길어지면 카드의 높이가 자연스럽게 늘어나고 창 전체 스크롤로 나머지 대화도 계속 확인할 수 있어요.\n\n두 번째 문단과 줄바꿈도 유지됩니다. 이 문장이 보이면 마지막 줄까지 정상 표시된 것입니다."
                : "오늘 회의 자료 보냈어요!";
            List<NotificationPreview> items = new List<NotificationPreview>
            {
                new NotificationPreview { ChatId = "101", RoomTitle = "수업 자료 이야기", AuthorName = "민수", Sender = "민수", Message = firstMessage, ReceivedAt = anchor, DemoAvatarResource = "Spoiler.Assets.Avatar.Dog", GroupKey = NotificationGrouping.CreateServerGroupKey("101", "2001") },
                new NotificationPreview { ChatId = "101", RoomTitle = "수업 자료 이야기", AuthorName = "나", Sender = "내 계정", Message = "내가 보낸 메시지 숨김 확인용 항목입니다.", ReceivedAt = anchor.AddMinutes(-1), IsMine = true, GroupKey = NotificationGrouping.CreateServerGroupKey("105", "9999") },
                new NotificationPreview { ChatId = "101", RoomTitle = "수업 자료 이야기", AuthorName = "민수", Sender = "민수", Message = "회의실은 3층 B룸으로 잡아뒀어요.", ReceivedAt = anchor.AddMinutes(-2), DemoAvatarResource = "Spoiler.Assets.Avatar.Dog", GroupKey = NotificationGrouping.CreateServerGroupKey("101", "2001") },
                new NotificationPreview { ChatId = "101", RoomTitle = "수업 자료 이야기", AuthorName = "민수", Sender = "민수", Message = "자료 마지막 장에 일정표도 추가했습니다.", ReceivedAt = anchor.AddMinutes(-4), DemoAvatarResource = "Spoiler.Assets.Avatar.Dog", GroupKey = NotificationGrouping.CreateServerGroupKey("101", "2001") },
                new NotificationPreview { ChatId = "102", RoomTitle = "지원", AuthorName = "지원", Sender = "지원", Message = "점심 메뉴 뭐 먹을까? 저는 샐러드 생각 중인데 다른 의견 있으면 말해줘요!", ReceivedAt = anchor.AddMinutes(-9), DemoAvatarResource = "Spoiler.Assets.Avatar.Bear", GroupKey = NotificationGrouping.CreateServerGroupKey("102", "2002") },
                new NotificationPreview { ChatId = "103", RoomTitle = "하늘", AuthorName = "하늘", Sender = "하늘", Message = "주말 등산 가는 거 날씨 확인해봤는데 토요일은 괜찮을 것 같아!", ReceivedAt = anchor.AddMinutes(-13), DemoAvatarResource = "Spoiler.Assets.Avatar.Mountain", GroupKey = NotificationGrouping.CreateServerGroupKey("103", "2003") },
                new NotificationPreview { ChatId = "104", RoomTitle = "영준", AuthorName = "영준", Sender = "영준", Message = "방금 그 영화 봤어. 마지막 장면 너무 인상 깊더라. 기회 되면 이야기해줘!", ReceivedAt = anchor.AddMinutes(-17), DemoAvatarResource = "Spoiler.Assets.Avatar.Coffee", GroupKey = NotificationGrouping.CreateServerGroupKey("104", "2004") }
            };

            if (string.Equals(Environment.GetEnvironmentVariable("SPOILER_QA_MANY_MESSAGES"), "1", StringComparison.Ordinal))
            {
                for (int index = 0; index < 18; index++)
                {
                    items.Add(new NotificationPreview
                    {
                        ChatId = (500 + index).ToString(),
                        RoomTitle = "스크롤 확인 " + (index + 1),
                        Sender = "스크롤 확인 " + (index + 1),
                        Message = "메시지가 쌓여도 아래까지 부드럽게 스크롤되는지 확인하는 항목입니다.",
                        ReceivedAt = anchor.AddMinutes(-25 - index),
                        GroupKey = NotificationGrouping.CreateServerGroupKey((500 + index).ToString(), (9000 + index).ToString())
                    });
                }
            }
            return items;
        }

        private void OnTitleBarMouseDown(object sender, MouseButtonEventArgs args)
        {
            if (args.ChangedButton != MouseButton.Left)
            {
                return;
            }

            if (args.ClickCount == 2)
            {
                if (WindowState == WindowState.Maximized)
                {
                    SystemCommands.RestoreWindow(this);
                }
                else
                {
                    SystemCommands.MaximizeWindow(this);
                }
                return;
            }

            DragMove();
        }

        private void OnPreviewKeyDown(object sender, KeyEventArgs args)
        {
            if (args.Key == Key.F5)
            {
                ConnectOrRegister();
                args.Handled = true;
                return;
            }

            if (args.Key == Key.Delete && Keyboard.Modifiers == (ModifierKeys.Control | ModifierKeys.Shift))
            {
                OnClearHistory(this, new RoutedEventArgs());
                args.Handled = true;
                return;
            }

            if (args.Key == Key.F && Keyboard.Modifiers == ModifierKeys.Control)
            {
                if (!_finderExpanded)
                {
                    ToggleFinderPanel();
                }
                else if (_hourInput != null)
                {
                    _hourInput.Focus();
                    _hourInput.SelectAll();
                }
                args.Handled = true;
                return;
            }

            if (args.Key == Key.Escape && Keyboard.Modifiers == ModifierKeys.None && _finderExpanded)
            {
                ToggleFinderPanel();
                args.Handled = true;
                return;
            }

            if (Keyboard.FocusedElement is TextBoxBase) return;

            if (_contentScroll != null && Keyboard.Modifiers == ModifierKeys.None)
            {
                if (args.Key == Key.PageDown)
                {
                    _contentScroll.PageDown();
                    args.Handled = true;
                }
                else if (args.Key == Key.PageUp)
                {
                    _contentScroll.PageUp();
                    args.Handled = true;
                }
                else if (args.Key == Key.Home)
                {
                    _contentScroll.ScrollToHome();
                    args.Handled = true;
                }
                else if (args.Key == Key.End)
                {
                    _contentScroll.ScrollToEnd();
                    args.Handled = true;
                }
            }
        }

        private void OnWindowStateChanged(object sender, EventArgs args)
        {
            if (IsVisible && WindowState != WindowState.Minimized && _visualsDirty)
            {
                FlushPendingUi();
                RenderList(false);
            }
            if (_maximizeGlyph != null)
            {
                _maximizeGlyph.Text = WindowState == WindowState.Maximized ? "\uE923" : "\uE922";
            }
            if (_maximizeButton != null)
            {
                string actionName = WindowState == WindowState.Maximized ? "복원" : "최대화";
                _maximizeButton.ToolTip = actionName;
                AutomationProperties.SetName(_maximizeButton, actionName);
            }
        }

        private void OnClosing(object sender, CancelEventArgs args)
        {
            if (_exitRequested || _trayIcon == null)
            {
                return;
            }

            args.Cancel = true;
            Hide();
            _trayIcon.ShowBackgroundHint();
            TraceQa("ui-window-hidden-to-tray count=" + _items.Count);
        }

        private void ShowFromTray()
        {
            if (_exitRequested)
            {
                return;
            }

            if (!IsVisible)
            {
                Show();
            }
            if (WindowState == WindowState.Minimized)
            {
                WindowState = WindowState.Normal;
            }
            Activate();
            Topmost = true;
            Topmost = false;
            Focus();
            TraceQa("ui-window-restored-from-tray");
        }

        internal void RequestExit()
        {
            if (_exitRequested)
            {
                return;
            }
            _exitRequested = true;
            Close();
        }

        private void OnClosed(object sender, EventArgs args)
        {
            if (_windowHwndSource != null)
            {
                if (!_windowHwndSource.IsDisposed) _windowHwndSource.RemoveHook(OnNativeWindowMessage);
                _windowHwndSource = null;
            }
            _exitRequested = true;
            _uiBatchTimer.Stop();
            if (_screenshotTimer != null)
            {
                _screenshotTimer.Stop();
            }
            lock (_pendingSync)
            {
                _uiGeneration++;
                _pendingPreviews.Clear();
                _pendingState = null;
            }
            TraceQa("ui-window-closed count=" + _items.Count);
            if (_trayIcon != null)
            {
                _trayIcon.Dispose();
                _trayIcon = null;
            }
            if (_source != null)
            {
                _source.PreviewCaptured -= OnPreviewCaptured;
                _source.StateChanged -= OnCaptureStateChanged;
                _source.Dispose();
            }
        }

        private void TraceQa(string message)
        {
            if (!_demoMode && !_qaMode)
            {
                return;
            }
            string path = Environment.GetEnvironmentVariable("SPOILER_CAPTURE_LOG");
            if (string.IsNullOrWhiteSpace(path))
            {
                return;
            }

            try
            {
                lock (QaTraceSync)
                {
                    File.AppendAllText(path, DateTime.Now.ToString("O") + "  " + message + Environment.NewLine);
                }
            }
            catch
            {
            }
        }

        private void OnSourceInitialized(object sender, EventArgs args)
        {
            IntPtr handle = new WindowInteropHelper(this).Handle;
            _windowHwndSource = HwndSource.FromHwnd(handle);
            if (_windowHwndSource != null) _windowHwndSource.AddHook(OnNativeWindowMessage);
            try
            {
                int preference = 2;
                DwmSetWindowAttribute(handle, 33, ref preference, sizeof(int));
            }
            catch
            {
            }
        }

        private IntPtr OnNativeWindowMessage(IntPtr window, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
        {
            if (message == WindowWorkArea.GetMinMaxInfoMessage)
            {
                WindowWorkArea.UpdateMaximizedBounds(window, lParam);
                // WPF must still consume the adjusted maximum bounds and
                // apply its own minimum/maximum resize constraints.
            }
            return IntPtr.Zero;
        }

        private static SolidColorBrush MakeBrush(string hex)
        {
            return Tokens.Brush(hex);
        }

        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(IntPtr windowHandle, int attribute, ref int value, int valueSize);
    }
}
