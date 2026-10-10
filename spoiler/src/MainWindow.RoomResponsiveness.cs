using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using Tokens = Spoiler.DesignTokens;

namespace Spoiler
{
    public sealed partial class MainWindow
    {
        // These are UI projections only. No transport identity or write decision
        // uses this cache. Scalar snapshots also detect in-place metadata edits.
        private List<RoomRecordSnapshot> _roomRecordSnapshots;
        private IList<ChatRoom> _retainedRooms = new List<ChatRoom>();
        private HashSet<string> _retainedRoomKeys = new HashSet<string>(StringComparer.Ordinal);
        private IList<ChatRoom> _filteredRooms;
        private IList<ChatRoom> _queriedRooms;
        private int _roomProjectionGeneration = -1;
        private bool _projectionHideMine;
        private bool _projectionSearchActive;
        private int _projectionHour;
        private int _projectionMinute;
        private bool _projectionAfternoon;
        private string _projectionQuery;
        private string _projectionCulture;
        private readonly Dictionary<string, RoomRowCache> _roomRows = new Dictionary<string, RoomRowCache>(StringComparer.Ordinal);
        private List<TimelineVisual> _timelineVisuals = new List<TimelineVisual>();
        private IList<MessageVisualSnapshot> _displayedMessages = new List<MessageVisualSnapshot>();
        private readonly Dictionary<string, ConversationProjection> _visitedConversations = new Dictionary<string, ConversationProjection>(StringComparer.Ordinal);
        private int _roomRecordsVersion;
        private string _renderedConversationKey;
        private int _renderedRecordsVersion = -1;
        private int _conversationRenderVersion;
        private bool? _emptyTimeline;
        private DispatcherOperation _roomSearchOperation;

        private void ScheduleRoomSearch()
        {
            if (_exitRequested || Dispatcher.HasShutdownStarted || _roomSearchOperation != null) return;
            int generation = _uiGeneration;
            _roomSearchOperation = Dispatcher.BeginInvoke(new Action(delegate
            {
                _roomSearchOperation = null;
                if (_exitRequested || generation != _uiGeneration) return;
                RenderRooms(false);
            }), DispatcherPriority.Background);
        }

        private void CancelPendingRoomSearch()
        {
            if (_roomSearchOperation == null) return;
            if (_roomSearchOperation.Status == DispatcherOperationStatus.Pending) _roomSearchOperation.Abort();
            _roomSearchOperation = null;
        }

        private IList<ChatRoom> GetProjectedRooms(string query)
        {
            bool recordsChanged = _roomProjectionGeneration != _uiGeneration
                || _roomRecordSnapshots == null || _roomRecordSnapshots.Count != _items.Count;
            if (!recordsChanged)
            {
                for (int index = 0; index < _items.Count; index++)
                    if (!_roomRecordSnapshots[index].Matches(_items[index])) { recordsChanged = true; break; }
            }
            if (recordsChanged)
            {
                _roomRecordsVersion++;
                _visitedConversations.Clear();
                _roomRecordSnapshots = _items.Select(item => new RoomRecordSnapshot(item)).ToList();
                _roomProjectionGeneration = _uiGeneration;
                _retainedRooms = ChatRoomGrouping.Build(_items);
                _retainedRoomKeys = new HashSet<string>(_retainedRooms.Select(room => room.Key), StringComparer.Ordinal);
                foreach (string key in _roomRows.Keys.Where(key => !_retainedRoomKeys.Contains(key)).ToList()) _roomRows.Remove(key);
                _filteredRooms = null;
            }
            bool filterChanged = _filteredRooms == null || _projectionHideMine != _hideMine
                || _projectionSearchActive != _searchActive || _projectionHour != _searchHour
                || _projectionMinute != _searchMinute || _projectionAfternoon != _committedSearchAfternoon;
            if (filterChanged)
            {
                // Titles must still come from the visible messages, as before.
                _filteredRooms = !_hideMine && !_searchActive ? _retainedRooms : ChatRoomGrouping.Build(GetVisibleItems());
                _projectionHideMine = _hideMine;
                _projectionSearchActive = _searchActive;
                _projectionHour = _searchHour;
                _projectionMinute = _searchMinute;
                _projectionAfternoon = _committedSearchAfternoon;
                _queriedRooms = null;
            }
            string culture = CultureInfo.CurrentCulture.CompareInfo.Name;
            if (_queriedRooms == null || !string.Equals(query, _projectionQuery, StringComparison.Ordinal)
                || !string.Equals(culture, _projectionCulture, StringComparison.Ordinal))
            {
                _queriedRooms = query.Length == 0 ? _filteredRooms : _filteredRooms.Where(room => ContainsQuery(room.Title, query)
                    || room.Messages.Any(item => ContainsQuery(item.Message, query) || ContainsQuery(ChatRoomGrouping.DisplayAuthor(item), query))).ToList();
                _projectionQuery = query;
                _projectionCulture = culture;
            }
            return _queriedRooms;
        }

        private void ReconcileRoomRows(IList<ChatRoom> rooms)
        {
            List<ListBoxItem> desired = new List<ListBoxItem>(rooms.Count);
            foreach (ChatRoom room in rooms)
            {
                RoomRowCache cached;
                if (!_roomRows.TryGetValue(room.Key, out cached) || !cached.Matches(room))
                {
                    cached = new RoomRowCache(room, BuildRoomRow(room));
                    _roomRows[room.Key] = cached;
                }
                cached.Row.Tag = room;
                desired.Add(cached.Row);
            }
            HashSet<ListBoxItem> keep = new HashSet<ListBoxItem>(desired);
            int removed = _roomList.Items.Cast<object>().Count(item => !keep.Contains(item as ListBoxItem));
            if (removed > Math.Max(8, _roomList.Items.Count / 3))
            {
                // Search can remove most rows. Keep the cached controls, but
                // change membership in one bulk operation rather than one by one.
                _roomList.Items.Clear();
                foreach (ListBoxItem row in desired) _roomList.Items.Add(row);
                return;
            }
            for (int index = _roomList.Items.Count - 1; index >= 0; index--)
                if (!keep.Contains(_roomList.Items[index] as ListBoxItem)) _roomList.Items.RemoveAt(index);
            for (int index = 0; index < desired.Count; index++)
            {
                ListBoxItem row = desired[index];
                if (index < _roomList.Items.Count && ReferenceEquals(_roomList.Items[index], row)) continue;
                int previous = _roomList.Items.IndexOf(row);
                if (previous >= 0) _roomList.Items.RemoveAt(previous);
                _roomList.Items.Insert(index, row);
            }
        }

        private bool ReconcileConversation(ChatRoom room, bool changed)
        {
            if (changed)
            {
                if (_renderedConversationKey != null && _renderedRecordsVersion == _roomRecordsVersion
                    && _retainedRoomKeys.Contains(_renderedConversationKey) && _displayedMessages.Count <= InboxStore.MaxMessages)
                {
                    // One projection per visited room. A record change discards
                    // every detached projection, including expired messages.
                    if (_visitedConversations.Values.Sum(value => value.Messages.Count) + _displayedMessages.Count > InboxStore.MaxMessages)
                        _visitedConversations.Clear();
                    _visitedConversations[_renderedConversationKey] = new ConversationProjection
                    {
                        Visuals = new List<TimelineVisual>(_timelineVisuals),
                        Messages = _displayedMessages.ToList()
                    };
                }
                // Detach the previous WPF tree before constructing new controls.
                _listHost.Children.Clear();
                _timelineVisuals = new List<TimelineVisual>();
                _displayedMessages = new List<MessageVisualSnapshot>();
                _emptyTimeline = null;
                ConversationProjection visited;
                if (room != null && _visitedConversations.TryGetValue(room.Key, out visited))
                {
                    _visitedConversations.Remove(room.Key);
                    _timelineVisuals = visited.Visuals;
                    _displayedMessages = visited.Messages;
                }
            }
            _renderedConversationKey = room == null ? null : room.Key;
            _renderedRecordsVersion = _roomRecordsVersion;
            if (room == null)
            {
                bool inboxEmpty = _items.Count == 0;
                if (!changed && _emptyTimeline == inboxEmpty) return false;
                _listHost.Children.Clear();
                _timelineVisuals.Clear();
                _displayedMessages = new List<MessageVisualSnapshot>();
                _emptyTimeline = inboxEmpty;
                StackPanel empty = new StackPanel { Margin = new Thickness(16, 70, 16, 16) };
                TextBlock title = RoomText(inboxEmpty ? "아직 보관된 대화가 없어요" : "검색 결과가 없어요", Tokens.Room.Heading, Tokens.Palette.InkStrong);
                title.HorizontalAlignment = HorizontalAlignment.Center;
                empty.Children.Add(title);
                TextBlock detail = RoomText(inboxEmpty ? "태블릿 연결 후 받은 메시지가 대화방별로 나타납니다." : "검색어나 메시지 필터를 바꿔 주세요.", Tokens.Room.Body, Tokens.Palette.InkMuted);
                detail.TextWrapping = TextWrapping.Wrap;
                detail.Margin = new Thickness(0, 10, 0, 0);
                detail.TextAlignment = TextAlignment.Center;
                empty.Children.Add(detail);
                _listHost.Children.Add(empty);
                return true;
            }
            _emptyTimeline = null;
            bool sameMessages = _displayedMessages.Count == room.Messages.Count;
            if (sameMessages)
            {
                for (int index = 0; index < room.Messages.Count; index++)
                    if (!_displayedMessages[index].Matches(room.Messages[index], room.HasStableIdentity)) { sameMessages = false; break; }
                if (sameMessages)
                {
                    if (changed) foreach (TimelineVisual visual in _timelineVisuals) _listHost.Children.Add(visual.Element);
                    return changed;
                }
            }
            Dictionary<Tuple<string, string>, Queue<TimelineVisual>> messages = new Dictionary<Tuple<string, string>, Queue<TimelineVisual>>();
            Dictionary<DateTime, TimelineVisual> dividers = new Dictionary<DateTime, TimelineVisual>();
            foreach (TimelineVisual visual in _timelineVisuals)
            {
                if (visual.Snapshot == null) { dividers[visual.Date] = visual; continue; }
                Tuple<string, string> identity = visual.Snapshot.Identity;
                Queue<TimelineVisual> candidates;
                if (!messages.TryGetValue(identity, out candidates)) messages[identity] = candidates = new Queue<TimelineVisual>();
                candidates.Enqueue(visual);
            }
            List<TimelineVisual> desired = new List<TimelineVisual>();
            List<MessageVisualSnapshot> displayed = new List<MessageVisualSnapshot>(room.Messages.Count);
            DateTime? date = null;
            foreach (NotificationPreview item in room.Messages)
            {
                if (date != item.ReceivedAt.Date)
                {
                    TimelineVisual divider;
                    if (!dividers.TryGetValue(item.ReceivedAt.Date, out divider))
                    {
                        TextBlock text = RoomText(item.ReceivedAt.ToString("yyyy년 M월 d일 dddd", KoreanCulture), Tokens.Room.Metadata, Tokens.Palette.InkMuted);
                        text.HorizontalAlignment = HorizontalAlignment.Center;
                        text.Margin = new Thickness(0, 8, 0, 16);
                        divider = new TimelineVisual { Date = item.ReceivedAt.Date, Element = text };
                    }
                    desired.Add(divider);
                    date = item.ReceivedAt.Date;
                }
                MessageVisualSnapshot snapshot = new MessageVisualSnapshot(item, room.HasStableIdentity);
                TimelineVisual visual = null;
                Queue<TimelineVisual> candidates;
                if (messages.TryGetValue(snapshot.Identity, out candidates) && candidates.Count != 0)
                {
                    TimelineVisual candidate = candidates.Dequeue();
                    if (candidate.Snapshot.Matches(snapshot)) visual = candidate;
                }
                if (visual == null) visual = new TimelineVisual { Snapshot = snapshot, Element = BuildMessageBubble(item) };
                desired.Add(visual);
                displayed.Add(visual.Snapshot);
            }
            bool visualsChanged = changed || desired.Count != _timelineVisuals.Count;
            if (!visualsChanged)
                for (int index = 0; index < desired.Count; index++)
                    if (!ReferenceEquals(desired[index].Element, _timelineVisuals[index].Element)) { visualsChanged = true; break; }
            if (!visualsChanged) return false;
            if (changed)
            {
                foreach (TimelineVisual visual in desired) _listHost.Children.Add(visual.Element);
                _timelineVisuals = desired;
                _displayedMessages = displayed;
                return true;
            }
            HashSet<UIElement> keep = new HashSet<UIElement>(desired.Select(visual => visual.Element));
            for (int index = _listHost.Children.Count - 1; index >= 0; index--)
                if (!keep.Contains(_listHost.Children[index])) _listHost.Children.RemoveAt(index);
            for (int index = 0; index < desired.Count; index++)
            {
                UIElement element = desired[index].Element;
                if (index < _listHost.Children.Count && ReferenceEquals(_listHost.Children[index], element)) continue;
                int previous = _listHost.Children.IndexOf(element);
                if (previous >= 0) _listHost.Children.RemoveAt(previous);
                _listHost.Children.Insert(index, element);
            }
            _timelineVisuals = desired;
            _displayedMessages = displayed;
            return true;
        }

        private void ResetRoomRenderingState()
        {
            CancelPendingRoomSearch();
            _conversationRenderVersion++;
            _roomRecordSnapshots = null;
            _filteredRooms = null;
            _queriedRooms = null;
            _retainedRooms = new List<ChatRoom>();
            _retainedRoomKeys.Clear();
            _roomRows.Clear();
            _timelineVisuals.Clear();
            _displayedMessages = new List<MessageVisualSnapshot>();
            _visitedConversations.Clear();
            _roomRecordsVersion++;
            _renderedConversationKey = null;
            _renderedRecordsVersion = -1;
            _emptyTimeline = null;
        }

        private sealed class RoomRecordSnapshot
        {
            private readonly bool _null;
            private readonly string _id, _chatId, _roomTitle, _author, _sender, _message, _groupKey, _avatar, _account, _titleSource;
            private readonly DateTime _at;
            private readonly bool _mine;
            private readonly int _type;
            public RoomRecordSnapshot(NotificationPreview item)
            {
                _null = item == null;
                if (_null) return;
                _id = item.Id; _chatId = item.ChatId; _roomTitle = item.RoomTitle; _author = item.AuthorName; _sender = item.Sender;
                _message = item.Message; _groupKey = item.GroupKey; _avatar = item.DemoAvatarResource; _account = item.AccountId;
                _titleSource = item.RoomTitleSource; _at = item.ReceivedAt; _mine = item.IsMine; _type = item.MessageType;
            }
            public bool Matches(NotificationPreview item)
            {
                if (_null || item == null) return _null && item == null;
                return _id == item.Id && _chatId == item.ChatId && _roomTitle == item.RoomTitle && _author == item.AuthorName
                    && _sender == item.Sender && _message == item.Message && _groupKey == item.GroupKey && _avatar == item.DemoAvatarResource
                    && _account == item.AccountId && _titleSource == item.RoomTitleSource && _at == item.ReceivedAt
                    && _mine == item.IsMine && _type == item.MessageType;
            }
        }

        private sealed class RoomRowCache
        {
            public readonly ListBoxItem Row;
            private readonly string _title, _preview, _avatar;
            private readonly int _count;
            private readonly DateTime _at, _day;
            private readonly bool _stable;
            public RoomRowCache(ChatRoom room, ListBoxItem row)
            {
                Row = row; _title = room.Title; _count = room.Count; _stable = room.HasStableIdentity;
                _preview = ChatRoomGrouping.MessageForDisplay(room.Latest); _avatar = room.Latest.DemoAvatarResource;
                _at = room.Latest.ReceivedAt; _day = DateTime.Today;
            }
            public bool Matches(ChatRoom room)
            {
                return _title == room.Title && _count == room.Count && _stable == room.HasStableIdentity
                    && _preview == ChatRoomGrouping.MessageForDisplay(room.Latest) && _avatar == room.Latest.DemoAvatarResource
                    && _at == room.Latest.ReceivedAt && _day == DateTime.Today;
            }
        }

        private sealed class TimelineVisual
        {
            public MessageVisualSnapshot Snapshot;
            public DateTime Date;
            public UIElement Element;
        }

        private sealed class ConversationProjection
        {
            public List<TimelineVisual> Visuals;
            public IList<MessageVisualSnapshot> Messages;
        }

        private sealed class MessageVisualSnapshot
        {
            public readonly Tuple<string, string> Identity;
            private readonly string _body, _author, _avatar;
            private readonly DateTime _at;
            private readonly bool _mine;
            public MessageVisualSnapshot(NotificationPreview item, bool stableRoom)
            {
                Identity = Tuple.Create(item.Id ?? string.Empty, stableRoom ? string.Empty : item.GroupKey ?? string.Empty);
                _body = ChatRoomGrouping.MessageForDisplay(item); _author = ChatRoomGrouping.DisplayAuthor(item);
                _avatar = item.DemoAvatarResource; _at = item.ReceivedAt; _mine = item.IsMine;
            }
            public bool Matches(MessageVisualSnapshot other)
            {
                return Identity.Equals(other.Identity) && _body == other._body && _author == other._author
                    && _avatar == other._avatar && _at == other._at && _mine == other._mine;
            }
            public bool Matches(NotificationPreview item, bool stableRoom)
            {
                return Identity.Item1 == (item.Id ?? string.Empty)
                    && Identity.Item2 == (stableRoom ? string.Empty : item.GroupKey ?? string.Empty)
                    && _body == ChatRoomGrouping.MessageForDisplay(item) && _author == ChatRoomGrouping.DisplayAuthor(item)
                    && _avatar == item.DemoAvatarResource && _at == item.ReceivedAt && _mine == item.IsMine;
            }
        }
    }
}
