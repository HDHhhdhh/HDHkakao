using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Tokens = Spoiler.DesignTokens;

namespace Spoiler
{
    public sealed partial class MainWindow
    {
        private ControlTemplate _roomRowTemplate;
        private ControlTemplate _roomSecondaryButtonTemplate;
        private ControlTemplate _roomPrimaryButtonTemplate;
        private static readonly BubbleWidthConverter RoomBubbleWidthConverter = new BubbleWidthConverter();

        private UIElement BuildRoomWindow()
        {
            Grid root = new Grid { Background = Tokens.Brush(Tokens.Palette.Surface) };
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(Tokens.Size.TitleBarHeight) });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.Children.Add(BuildTitleBar());
            Grid body = new Grid();
            body.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(Tokens.Room.Sidebar), MinWidth = Tokens.Room.SidebarMin, MaxWidth = Tokens.Room.SidebarMax });
            body.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            SizeChanged += delegate { body.ColumnDefinitions[0].Width = new GridLength(Math.Max(Tokens.Room.SidebarMin, Math.Min(Tokens.Room.SidebarMax, ActualWidth * .28))); };
            Border sidebar = new Border { BorderBrush = Tokens.Brush(Tokens.Palette.Divider), BorderThickness = new Thickness(0, 0, 1, 0), Child = BuildRoomSidebar() };
            body.Children.Add(sidebar);
            Grid conversation = BuildConversationPane();
            Grid.SetColumn(conversation, 1);
            body.Children.Add(conversation);
            Grid.SetRow(body, 1);
            root.Children.Add(body);
            RenderRooms(false);
            return root;
        }

        private FrameworkElement BuildRoomSidebar()
        {
            Grid sidebar = new Grid();
            sidebar.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            sidebar.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            sidebar.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            sidebar.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            Grid header = new Grid { Margin = new Thickness(Tokens.Room.Inset) };
            header.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            header.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            StackPanel heading = new StackPanel();
            TextBlock title = RoomText("채팅", Tokens.TypeScale.Section, Tokens.Palette.InkStrong);
            title.FontWeight = FontWeights.Bold;
            heading.Children.Add(title);
            heading.Children.Add(RoomText("읽음 신호 없이 모아보기", Tokens.Room.Metadata, Tokens.Palette.InkMuted));
            header.Children.Add(heading);
            _refreshButton = RoomButton("새로고침", false);
            _refreshButton.Content = MakeIconText("\uE72C", Tokens.TypeScale.WindowTitle, Tokens.Palette.Icon);
            _refreshButton.Width = Tokens.Room.Action;
            _refreshButton.ToolTip = "수신 연결 새로고침 (F5)";
            AutomationProperties.SetName(_refreshButton, "수신 연결 새로고침");
            _refreshButton.Click += delegate { ConnectOrRegister(); };
            Grid.SetColumn(_refreshButton, 1);
            header.Children.Add(_refreshButton);
            _headerBlock = header;
            sidebar.Children.Add(header);

            StackPanel searchArea = new StackPanel { Margin = new Thickness(Tokens.Room.Inset, 0, Tokens.Room.Inset, Tokens.Room.Gap) };
            Grid search = new Grid();
            Border searchShell = new Border { Background = Tokens.Brush(Tokens.Palette.Surface), BorderBrush = Tokens.Brush(Tokens.Palette.DividerStrong), BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(Tokens.Radius.Field) };
            _roomSearchInput = new TextBox { Name = "RoomSearchInput", Height = Tokens.Room.SearchHeight, Padding = new Thickness(32, 9, 10, 7), BorderThickness = new Thickness(0), Background = Brushes.Transparent, FontFamily = UiFont, FontSize = Tokens.Room.Body, Foreground = Tokens.Brush(Tokens.Palette.Ink) };
            TextBlock placeholder = RoomText("대화방 · 내용 검색", Tokens.Room.Body, Tokens.Palette.InkMuted);
            placeholder.Margin = new Thickness(33, 0, 0, 0);
            placeholder.VerticalAlignment = VerticalAlignment.Center;
            placeholder.IsHitTestVisible = false;
            searchShell.Child = _roomSearchInput;
            search.Children.Add(searchShell);
            TextBlock searchIcon = MakeIconText("\uE721", Tokens.Room.Metadata, Tokens.Palette.IconMuted);
            searchIcon.Margin = new Thickness(11, 0, 0, 0);
            searchIcon.HorizontalAlignment = HorizontalAlignment.Left;
            searchIcon.IsHitTestVisible = false;
            search.Children.Add(searchIcon);
            search.Children.Add(placeholder);
            _roomSearchInput.TextChanged += delegate { placeholder.Visibility = _roomSearchInput.Text.Length == 0 ? Visibility.Visible : Visibility.Collapsed; ScheduleRoomSearch(); };
            ApplyFieldFeedback(_roomSearchInput, searchShell);
            AutomationProperties.SetName(_roomSearchInput, "저장된 대화방과 메시지 검색");
            searchArea.Children.Add(search);
            _roomCount = RoomText("대화방", Tokens.Room.Metadata, Tokens.Palette.InkMuted);
            _roomCount.Margin = new Thickness(0, 10, 0, 0);
            searchArea.Children.Add(_roomCount);
            Grid.SetRow(searchArea, 1);
            sidebar.Children.Add(searchArea);

            _roomList = new ListBox { Name = "RoomList", Background = Brushes.Transparent, BorderThickness = new Thickness(0), Padding = new Thickness(Tokens.Room.Gap, 0, Tokens.Room.Gap, 0), HorizontalContentAlignment = HorizontalAlignment.Stretch };
            ScrollViewer.SetHorizontalScrollBarVisibility(_roomList, ScrollBarVisibility.Disabled);
            ScrollViewer.SetVerticalScrollBarVisibility(_roomList, ScrollBarVisibility.Auto);
            _roomList.Resources[typeof(ScrollBar)] = BuildSlimScrollBarStyle();
            _roomList.SelectionChanged += delegate
            {
                if (_updatingRoomList) return;
                ListBoxItem item = _roomList.SelectedItem as ListBoxItem;
                SelectConversation(item == null ? null : item.Tag as ChatRoom);
            };
            Grid.SetRow(_roomList, 2);
            sidebar.Children.Add(_roomList);
            StackPanel footer = new StackPanel { Margin = new Thickness(Tokens.Room.Inset, Tokens.Room.Gap, Tokens.Room.Inset, Tokens.Room.Inset) };
            _deviceButton = RoomButton("태블릿 설정", false);
            _deviceButton.Click += delegate { OpenRegistration(); };
            footer.Children.Add(_deviceButton);
            TextBlock retention = RoomText("최대 7일 · 전체 " + InboxStore.MaxMessages.ToString("N0") + "개 보관", Tokens.Room.Metadata, Tokens.Palette.InkMuted);
            retention.Margin = new Thickness(0, Tokens.Room.Gap, 0, 0);
            retention.ToolTip = "방의 숫자는 로컬 보관 수입니다. 카카오톡의 안읽음 수가 아닙니다. 기록 지우기: Ctrl+Shift+Delete";
            footer.Children.Add(retention);
            Grid.SetRow(footer, 3);
            sidebar.Children.Add(footer);
            return sidebar;
        }

        private Grid BuildConversationPane()
        {
            Grid pane = new Grid { Background = Tokens.Brush(Tokens.Room.Canvas) };
            pane.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            pane.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            pane.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            pane.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            Border header = new Border { Padding = new Thickness(Tokens.Room.Inset, 12, Tokens.Room.Inset, 10), Background = Tokens.Brush(Tokens.Palette.Surface), BorderBrush = Tokens.Brush(Tokens.Palette.Divider), BorderThickness = new Thickness(0, 0, 0, 1) };
            StackPanel info = new StackPanel();
            _selectedRoomTitle = RoomText("대화방을 선택하세요", Tokens.Room.Heading, Tokens.Palette.InkStrong);
            _selectedRoomTitle.Name = "SelectedRoomTitle";
            _selectedRoomTitle.FontWeight = FontWeights.SemiBold;
            _selectedRoomTitle.TextTrimming = TextTrimming.CharacterEllipsis;
            info.Children.Add(_selectedRoomTitle);
            _roomStateText = RoomText("저장된 메시지를 읽어도 읽음 처리를 보내지 않습니다.", Tokens.Room.Metadata, Tokens.Palette.InkMuted);
            _roomStateText.Name = "RoomStateText";
            _roomStateText.TextWrapping = TextWrapping.Wrap;
            _roomStateText.Margin = new Thickness(0, 4, 0, 6);
            info.Children.Add(_roomStateText);
            Grid toolbar = new Grid();
            toolbar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            toolbar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            _statusText = RoomText(_demoMode || _qaMode ? "미리보기 · 실제 전송 없음" : "태블릿 연결 확인 중", Tokens.Room.Metadata, Tokens.Palette.InkMuted);
            _statusText.VerticalAlignment = VerticalAlignment.Center;
            _statusText.TextTrimming = TextTrimming.CharacterEllipsis;
            _statusText.SetBinding(FrameworkElement.ToolTipProperty, new Binding("Text") { Source = _statusText });
            toolbar.Children.Add(_statusText);
            StackPanel controls = new StackPanel { Orientation = Orientation.Horizontal };
            _hideMineCheckBox = new CheckBox { Content = "내 메시지 포함", IsChecked = true, FontSize = Tokens.Room.Metadata, Foreground = Tokens.Brush(Tokens.Palette.InkMuted), VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(8, 0, 8, 0) };
            _hideMineCheckBox.Checked += delegate { ApplyMineFilter(false); };
            _hideMineCheckBox.Unchecked += delegate { ApplyMineFilter(true); };
            controls.Children.Add(_hideMineCheckBox);
            _findToggleButton = RoomButton("시각 찾기", false);
            _findToggleButton.Click += delegate { ToggleFinderPanel(); };
            controls.Children.Add(_findToggleButton);
            Grid.SetColumn(controls, 1);
            toolbar.Children.Add(controls);
            info.Children.Add(toolbar);
            header.Child = info;
            _sectionHeading = header;
            pane.Children.Add(header);
            _finderPanel = BuildFinderPanel();
            _finderPanel.Margin = new Thickness(Tokens.Room.Inset, 0, Tokens.Room.Inset, 0);
            Grid.SetRow(_finderPanel, 1);
            pane.Children.Add(_finderPanel);
            _contentScroll = new ScrollViewer { Name = "ConversationScroll", VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, PanningMode = PanningMode.VerticalOnly, Focusable = false };
            _contentScroll.Resources[typeof(ScrollBar)] = BuildSlimScrollBarStyle();
            _listHost = new StackPanel { Name = "MessageTimeline", Margin = new Thickness(Tokens.Room.Inset) };
            _contentScroll.Content = _listHost;
            Grid.SetRow(_contentScroll, 2);
            pane.Children.Add(_contentScroll);
            Border composer = BuildReplyComposer();
            Grid.SetRow(composer, 3);
            pane.Children.Add(composer);
            return pane;
        }

        private Border BuildReplyComposer()
        {
            Border shell = new Border { Padding = new Thickness(Tokens.Room.Inset, 10, Tokens.Room.Inset, 10), Background = Tokens.Brush(Tokens.Palette.Surface), BorderBrush = Tokens.Brush(Tokens.Palette.Divider), BorderThickness = new Thickness(0, 1, 0, 0) };
            Grid composer = new Grid();
            composer.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            composer.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            composer.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            composer.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            Grid editor = new Grid();
            Border editorShell = new Border { CornerRadius = new CornerRadius(Tokens.Radius.Field), BorderBrush = Tokens.Brush(Tokens.Palette.DividerStrong), BorderThickness = new Thickness(1), Background = Tokens.Brush(Tokens.Palette.Surface) };
            _replyInput = new TextBox { Name = "ReplyInput", Height = Tokens.Room.ComposerHeight, TextWrapping = TextWrapping.Wrap, AcceptsReturn = true, MaxLength = 4000, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Padding = new Thickness(Tokens.Space.Md), BorderThickness = new Thickness(0), Background = Brushes.Transparent, FontFamily = UiFont, FontSize = Tokens.Room.Body, Foreground = Tokens.Brush(Tokens.Palette.Ink) };
            ApplyFieldFeedback(_replyInput, editorShell);
            _replyInput.TextChanged += delegate
            {
                if (_selectedRoomKey != null) _roomDrafts[_selectedRoomKey] = _replyInput.Text;
                _replyPlaceholder.Visibility = _replyInput.Text.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
                UpdateReplyAvailability();
            };
            _replyInput.PreviewKeyDown += delegate(object sender, KeyEventArgs args)
            {
                if (args.Key == Key.Enter && Keyboard.Modifiers == ModifierKeys.Control) { SendSelectedReply(); args.Handled = true; }
            };
            AutomationProperties.SetName(_replyInput, "선택한 대화방에 직접 답장 입력");
            editorShell.Child = _replyInput;
            editor.Children.Add(editorShell);
            _replyPlaceholder = RoomText("메시지를 입력하세요", Tokens.Room.Body, Tokens.Palette.InkMuted);
            _replyPlaceholder.Margin = new Thickness(11);
            _replyPlaceholder.IsHitTestVisible = false;
            editor.Children.Add(_replyPlaceholder);
            composer.Children.Add(editor);
            _sendReplyButton = RoomButton("전송", true);
            _sendReplyButton.Name = "SendReplyButton";
            _sendReplyButton.MinWidth = 88;
            _sendReplyButton.Margin = new Thickness(Tokens.Room.Gap, 0, 0, 0);
            _sendReplyButton.VerticalAlignment = VerticalAlignment.Bottom;
            _sendReplyButton.Click += delegate { SendSelectedReply(); };
            Grid.SetColumn(_sendReplyButton, 1);
            composer.Children.Add(_sendReplyButton);
            _replyFeedback = RoomText("Enter 줄바꿈 · Ctrl+Enter 전송", Tokens.Room.Metadata, Tokens.Palette.InkMuted);
            _replyFeedback.Name = "ReplyFeedback";
            _replyFeedback.TextWrapping = TextWrapping.Wrap;
            _replyFeedback.Margin = new Thickness(0, Tokens.Room.Gap, 0, 0);
            AutomationProperties.SetLiveSetting(_replyFeedback, AutomationLiveSetting.Polite);
            Grid.SetRow(_replyFeedback, 1);
            Grid.SetColumnSpan(_replyFeedback, 2);
            composer.Children.Add(_replyFeedback);
            shell.Child = composer;
            return shell;
        }

        private ListBoxItem BuildRoomRow(ChatRoom room)
        {
            ListBoxItem row = new ListBoxItem { Tag = room, Padding = new Thickness(0), Margin = new Thickness(0, 2, 0, 2), HorizontalContentAlignment = HorizontalAlignment.Stretch, BorderThickness = new Thickness(0) };
            row.Template = RoomRowTemplate();
            Grid grid = new Grid { Height = Tokens.Room.RowHeight, Margin = new Thickness(Tokens.Space.Md, 0, Tokens.Space.Md, 0) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            FrameworkElement avatar = RoomAvatar(room.Latest, room.Title);
            avatar.Margin = new Thickness(0, 0, Tokens.Space.Md, 0);
            grid.Children.Add(avatar);
            StackPanel summary = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            TextBlock title = RoomText(room.Title, Tokens.Room.Body, Tokens.Palette.InkStrong);
            title.FontWeight = FontWeights.SemiBold;
            title.TextTrimming = TextTrimming.CharacterEllipsis;
            title.ToolTip = room.Title;
            summary.Children.Add(title);
            TextBlock preview = RoomText(room.HasStableIdentity
                ? ChatRoomGrouping.MessageForDisplay(room.Latest).Replace("\r", " ").Replace("\n", " ")
                : "방을 확인할 수 없는 기록 · 읽기 전용", Tokens.Room.Metadata, Tokens.Palette.InkMuted);
            preview.TextTrimming = TextTrimming.CharacterEllipsis;
            preview.Margin = new Thickness(0, Tokens.Space.Xs + 1, 0, 0);
            summary.Children.Add(preview);
            Grid.SetColumn(summary, 1);
            grid.Children.Add(summary);
            StackPanel metadata = new StackPanel { VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(Tokens.Room.Gap, 0, 0, 0), HorizontalAlignment = HorizontalAlignment.Right };
            metadata.Children.Add(RoomText(RoomTime(room.Latest.ReceivedAt), Tokens.Room.Metadata, Tokens.Palette.InkMuted));
            TextBlock count = RoomText(room.Count + "개", Tokens.Room.Metadata, Tokens.Palette.InkMuted);
            count.Margin = new Thickness(0, Tokens.Space.Xs + 1, 0, 0);
            count.HorizontalAlignment = HorizontalAlignment.Right;
            count.ToolTip = "이 대화방에 로컬로 보관된 메시지 수 · 서버 안읽음 수가 아닙니다.";
            metadata.Children.Add(count);
            Grid.SetColumn(metadata, 2);
            grid.Children.Add(metadata);
            row.Content = grid;
            AutomationProperties.SetName(row, room.Title + ", 보관 메시지 " + room.Count + "개");
            return row;
        }

        private ControlTemplate RoomRowTemplate()
        {
            if (_roomRowTemplate != null) return _roomRowTemplate;
            FrameworkElementFactory frame = new FrameworkElementFactory(typeof(Border));
            frame.Name = "RoomSelectionFrame";
            frame.SetValue(Border.CornerRadiusProperty, new CornerRadius(Tokens.Radius.Field));
            frame.SetValue(Border.BackgroundProperty, Brushes.Transparent);
            frame.SetValue(Border.BorderBrushProperty, Brushes.Transparent);
            frame.SetValue(Border.BorderThicknessProperty, new Thickness(1));
            FrameworkElementFactory content = new FrameworkElementFactory(typeof(ContentPresenter));
            content.SetValue(ContentPresenter.ContentProperty, new TemplateBindingExtension(ContentControl.ContentProperty));
            frame.AppendChild(content);
            ControlTemplate template = new ControlTemplate(typeof(ListBoxItem)) { VisualTree = frame };
            Trigger hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
            hover.Setters.Add(new Setter(Border.BackgroundProperty, Tokens.Brush(Tokens.Palette.SurfaceHover), "RoomSelectionFrame"));
            template.Triggers.Add(hover);
            Trigger selected = new Trigger { Property = ListBoxItem.IsSelectedProperty, Value = true };
            selected.Setters.Add(new Setter(Border.BackgroundProperty, Tokens.Brush(Tokens.Room.Selected), "RoomSelectionFrame"));
            selected.Setters.Add(new Setter(Border.BorderBrushProperty, Tokens.Brush(Tokens.Room.SelectedBorder), "RoomSelectionFrame"));
            template.Triggers.Add(selected);
            Trigger focus = new Trigger { Property = UIElement.IsKeyboardFocusWithinProperty, Value = true };
            focus.Setters.Add(new Setter(Border.BorderBrushProperty, Tokens.Brush(Tokens.Palette.Focus), "RoomSelectionFrame"));
            template.Triggers.Add(focus);
            template.Seal();
            _roomRowTemplate = template;
            return template;
        }

        private FrameworkElement BuildMessageBubble(NotificationPreview item)
        {
            Grid line = new Grid { Margin = new Thickness(0, 0, 0, Tokens.Space.Xl) };
            line.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            line.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            if (!item.IsMine)
            {
                FrameworkElement avatar = RoomAvatar(item, ChatRoomGrouping.DisplayAuthor(item));
                avatar.VerticalAlignment = VerticalAlignment.Top;
                avatar.Margin = new Thickness(0, 0, 8, 0);
                line.Children.Add(avatar);
            }
            StackPanel message = new StackPanel { HorizontalAlignment = item.IsMine ? HorizontalAlignment.Right : HorizontalAlignment.Left };
            message.SetBinding(FrameworkElement.MaxWidthProperty, new Binding("ActualWidth") { Source = _contentScroll, Converter = RoomBubbleWidthConverter });
            if (!item.IsMine)
            {
                TextBlock author = RoomText(ChatRoomGrouping.DisplayAuthor(item), Tokens.Room.Metadata, Tokens.Palette.InkMuted);
                author.Margin = new Thickness(0, 0, 0, 5);
                message.Children.Add(author);
            }
            TextBox body = new KoreanMessageTextBox(ChatRoomGrouping.MessageForDisplay(item)) { IsReadOnly = true, IsReadOnlyCaretVisible = false, IsTabStop = false, TextWrapping = TextWrapping.Wrap, AcceptsReturn = true, BorderThickness = new Thickness(0), Background = Brushes.Transparent, Foreground = Tokens.Brush(Tokens.Palette.Ink), FontFamily = UiFont, FontSize = Tokens.Room.Body, Padding = new Thickness(0), VerticalScrollBarVisibility = ScrollBarVisibility.Disabled, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
            AutomationProperties.SetName(body, (item.IsMine ? "내 메시지" : ChatRoomGrouping.DisplayAuthor(item)) + " 메시지 본문");
            Border bubble = new Border { Background = Tokens.Brush(item.IsMine ? Tokens.Room.Outgoing : Tokens.Palette.Surface), BorderBrush = Tokens.Brush(item.IsMine ? Tokens.Room.Outgoing : Tokens.Palette.Divider), BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(Tokens.Room.BubbleRadius), Padding = new Thickness(Tokens.Room.BubbleInset, Tokens.Space.Md, Tokens.Room.BubbleInset, Tokens.Space.Md), Child = body };
            message.Children.Add(bubble);
            TextBlock time = RoomText(item.ReceivedAt.ToString("tt h:mm", KoreanCulture), Tokens.Room.Metadata, Tokens.Palette.InkMuted);
            time.HorizontalAlignment = item.IsMine ? HorizontalAlignment.Right : HorizontalAlignment.Left;
            time.Margin = new Thickness(0, 4, 0, 0);
            message.Children.Add(time);
            Grid.SetColumn(message, 1);
            line.Children.Add(message);
            return line;
        }

        private static string RoomTime(DateTime at) { return at.Date == DateTime.Today ? at.ToString("tt h:mm", KoreanCulture) : at.ToString("M/d", KoreanCulture); }

        private static TextBlock RoomText(string text, double size, string color) { return new KoreanGuideTextBlock { Text = text, FontSize = size, FontFamily = UiFont, Foreground = Tokens.Brush(color) }; }

        private TactileButton RoomButton(string label, bool primary)
        {
            TactileButton button = new TactileButton { Content = label, Height = Tokens.Room.Action, MinHeight = 0, Padding = new Thickness(12, 0, 12, 0), FontSize = Tokens.Room.Metadata, FontWeight = FontWeights.SemiBold, BorderThickness = new Thickness(primary ? 0 : 1), BorderBrush = Tokens.Brush(Tokens.Palette.DividerStrong), Background = Tokens.Brush(primary ? Tokens.Room.Outgoing : Tokens.Palette.Surface), Foreground = Tokens.Brush(Tokens.Palette.InkStrong) };
            if (primary)
            {
                if (_roomPrimaryButtonTemplate == null) _roomPrimaryButtonTemplate = TactileButton.CreateRoundedTemplate(Tokens.Radius.Field, Tokens.Brush(Tokens.Palette.AccentHover));
                button.Template = _roomPrimaryButtonTemplate;
            }
            else
            {
                if (_roomSecondaryButtonTemplate == null) _roomSecondaryButtonTemplate = TactileButton.CreateRoundedTemplate(Tokens.Radius.Field, Tokens.Brush(Tokens.Palette.SurfaceHover));
                button.Template = _roomSecondaryButtonTemplate;
            }
            AutomationProperties.SetName(button, label);
            return button;
        }

        private static void ApplyFieldFeedback(TextBox input, Border shell)
        {
            Action update = delegate
            {
                shell.BorderBrush = Tokens.Brush(input.IsKeyboardFocusWithin ? Tokens.Palette.Focus : Tokens.Palette.DividerStrong);
                shell.Background = Tokens.Brush(input.IsEnabled ? Tokens.Palette.Surface : Tokens.Palette.SurfaceSubtle);
            };
            input.GotKeyboardFocus += delegate { update(); };
            input.LostKeyboardFocus += delegate { update(); };
            input.IsEnabledChanged += delegate { update(); };
        }

        private FrameworkElement RoomAvatar(NotificationPreview item, string title)
        {
            Border avatar = new Border { Width = Tokens.Room.Avatar, Height = Tokens.Room.Avatar, CornerRadius = new CornerRadius(Tokens.Room.Avatar / 2), VerticalAlignment = VerticalAlignment.Center };
            BitmapSource image = item == null || string.IsNullOrEmpty(item.DemoAvatarResource) ? null : AssetLoader.LoadBitmap(item.DemoAvatarResource);
            if (image != null) { avatar.Background = new ImageBrush(image) { Stretch = Stretch.UniformToFill }; }
            else
            {
                string name = string.IsNullOrWhiteSpace(title) ? "?" : title.Trim();
                int hash = name.GetHashCode() & int.MaxValue;
                avatar.Background = Tokens.Brush(Tokens.Palette.AvatarFallbacks[hash % Tokens.Palette.AvatarFallbacks.Length]);
                TextBlock initial = RoomText(StringInfo.GetNextTextElement(name), Tokens.Room.Body, Tokens.Palette.InkBody);
                initial.VerticalAlignment = VerticalAlignment.Center;
                initial.HorizontalAlignment = HorizontalAlignment.Center;
                avatar.Child = initial;
            }
            return avatar;
        }

        private sealed class BubbleWidthConverter : IValueConverter
        {
            public object Convert(object value, Type targetType, object parameter, CultureInfo culture) { return Math.Max(120, Math.Min(Tokens.Room.BubbleMax, System.Convert.ToDouble(value) - Tokens.Room.BubbleReserve)); }
            public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture) { return Binding.DoNothing; }
        }
    }
}
