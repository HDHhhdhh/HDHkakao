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
        private readonly IReplyMessageSource _replySource;
        private IList<ChatRoom> _rooms = new List<ChatRoom>();
        private string _selectedRoomKey;
        private readonly Dictionary<string, string> _roomDrafts = new Dictionary<string, string>(StringComparer.Ordinal);
        private readonly Dictionary<string, string> _roomReplyFeedback = new Dictionary<string, string>(StringComparer.Ordinal);
        private bool _updatingRoomList;
        private bool _replyPending;
        private ListBox _roomList;
        private TextBox _roomSearchInput;
        private TextBox _replyInput;
        private TextBlock _replyFeedback;
        private TextBlock _replyPlaceholder;
        private TextBlock _selectedRoomTitle;
        private TextBlock _roomStateText;
        private TextBlock _roomCount;
        private TactileButton _sendReplyButton;









        private void RenderRooms(bool animateNewest)
        {
            if (_roomList == null || _listHost == null) return;
            CancelPendingRoomSearch();
            _visualsDirty = false;
            string query = (_roomSearchInput.Text ?? string.Empty).Trim();
            _rooms = GetProjectedRooms(query);
            string selectedKey = ChatRoomGrouping.ResolveSelection(_rooms, _selectedRoomKey);
            ChatRoom selected = _rooms.FirstOrDefault(room => room.Key == selectedKey);
            _updatingRoomList = true;
            try
            {
                ReconcileRoomRows(_rooms);
                _roomList.SelectedItem = selected == null ? null : _roomRows[selected.Key].Row;
            }
            finally { _updatingRoomList = false; }
            _roomCount.Text = "대화방 · " + _rooms.Count(room => room.HasStableIdentity) + "개";
            // Drop drafts only when their underlying retained room has disappeared.
            HashSet<string> retainedKeys = _retainedRoomKeys;
            foreach (string key in _roomDrafts.Keys.Where(key => !retainedKeys.Contains(key)).ToList()) _roomDrafts.Remove(key);
            foreach (string key in _roomReplyFeedback.Keys.Where(key => !retainedKeys.Contains(key)).ToList()) _roomReplyFeedback.Remove(key);
            SelectConversation(selected);
        }



        private void SelectConversation(ChatRoom room)
        {
            bool changed = !string.Equals(_selectedRoomKey, room == null ? null : room.Key, StringComparison.Ordinal);
            double offset = _contentScroll.VerticalOffset;
            bool atBottom = _contentScroll.ScrollableHeight - offset < 24;
            if (changed)
            {
                if (_selectedRoomKey != null && _retainedRoomKeys.Contains(_selectedRoomKey)) _roomDrafts[_selectedRoomKey] = _replyInput.Text;
                _selectedRoomKey = room == null ? null : room.Key;
                string draft;
                _replyInput.Text = _selectedRoomKey != null && _roomDrafts.TryGetValue(_selectedRoomKey, out draft) ? draft : string.Empty;
            }
            _selectedRoomTitle.Text = room == null ? "대화방을 선택하세요" : room.Title;
            _roomStateText.Text = room == null ? "저장된 메시지를 읽어도 읽음 처리를 보내지 않습니다."
                : !room.HasStableIdentity ? "여러 방의 미확인 기록 · 실제 대화방으로 합치지 않았습니다."
                : "시간순 · " + (_hideMine ? "상대 메시지만" : "내·상대 메시지 함께") + " · 보관 " + room.Count + "개";
            bool timelineChanged = ReconcileConversation(room, changed);
            string feedback;
            _replyFeedback.Text = room != null && _roomReplyFeedback.TryGetValue(room.Key, out feedback) ? feedback : "Enter 줄바꿈 · Ctrl+Enter 전송";
            UpdateReplyAvailability();
            if (!timelineChanged && !changed) return;
            string currentKey = _selectedRoomKey;
            int generation = _uiGeneration;
            int renderVersion = ++_conversationRenderVersion;
            Dispatcher.BeginInvoke(new Action(delegate
            {
                if (_exitRequested || generation != _uiGeneration || renderVersion != _conversationRenderVersion || currentKey != _selectedRoomKey) return;
                if (changed || atBottom) _contentScroll.ScrollToEnd();
                else _contentScroll.ScrollToVerticalOffset(offset);
            }), System.Windows.Threading.DispatcherPriority.Loaded);
        }



        private void UpdateReplyAvailability()
        {
            if (_replyInput == null || _sendReplyButton == null) return;
            ChatRoom room = _rooms.FirstOrDefault(value => value.Key == _selectedRoomKey);
            bool stable = room != null && room.HasStableIdentity;
            bool sameAccount = !_scopeTransition && (_source == null || string.Equals(_displayAccountId, _source.CurrentAccountId, StringComparison.Ordinal));
            bool available = stable && sameAccount && _replySource != null && _replySource.CanReply;
            _replyInput.IsEnabled = stable && sameAccount;
            _sendReplyButton.IsEnabled = available && !_replyPending && !string.IsNullOrWhiteSpace(_replyInput.Text);
            _sendReplyButton.Content = _replyPending ? "전송 중" : "전송";
            _sendReplyButton.IsPending = _replyPending;
            _sendReplyButton.ToolTip = room == null ? "대화방을 선택하세요." : !stable ? "기존 기록은 대화방 ID가 없어 읽기만 가능합니다. 같은 방의 새 메시지를 수신한 뒤 답장할 수 있습니다." : !available ? "태블릿 수신 연결이 준비되면 직접 답장할 수 있습니다." : "선택한 대화방에 직접 전송 (Ctrl+Enter)";
            if (room != null && !stable) _replyFeedback.Text = "기존 기록은 읽기 전용입니다. 대화방 ID가 있는 새 메시지부터 답장할 수 있어요.";
        }

        private void SendSelectedReply()
        {
            UpdateReplyAvailability();
            if (!_sendReplyButton.IsEnabled) return;
            ChatRoom room = _rooms.FirstOrDefault(value => value.Key == _selectedRoomKey);
            if (room == null || !room.HasStableIdentity) return;
            string roomKey = room.Key;
            string chatId = room.ChatId;
            string text = _replyInput.Text;
            int generation = _uiGeneration;
            string accountId = _displayAccountId;
            _replyPending = true;
            SetRoomReplyFeedback(roomKey, "전송 중… 결과를 확인하기 전에는 다시 보내지 마세요.");
            UpdateReplyAvailability();
            Task<ReplySendResult> send;
            try { send = _replySource.SendReplyAsync(chatId, text); }
            catch (Exception) { CompleteRoomReply(roomKey, text, generation, accountId, null); return; }
            if (send == null) { CompleteRoomReply(roomKey, text, generation, accountId, null); return; }
            send.ContinueWith(task =>
            {
                if (task.IsFaulted) { AggregateException observed = task.Exception; }
                if (Dispatcher.HasShutdownStarted) return;
                Dispatcher.BeginInvoke(new Action(delegate
                {
                    CompleteRoomReply(roomKey, text, generation, accountId, task.Status == TaskStatus.RanToCompletion ? task.Result : null);
                }));
            }, System.Threading.CancellationToken.None, TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
        }

        private void CompleteRoomReply(string roomKey, string sentText, int generation, string accountId, ReplySendResult result)
        {
            if (_exitRequested || generation != _uiGeneration || !string.Equals(accountId, _displayAccountId, StringComparison.Ordinal)) return;
            _replyPending = false;
            bool validReceipt = result != null && result.Status == ReplySendStatus.Sent
                && ChatRoomGrouping.CreateServerRoomKey(result.ChatId) == roomKey
                && !string.IsNullOrWhiteSpace(result.LogId);
            if (validReceipt)
            {
                string draft;
                if (_roomDrafts.TryGetValue(roomKey, out draft) && draft == sentText) _roomDrafts[roomKey] = string.Empty;
                if (_selectedRoomKey == roomKey && _replyInput.Text == sentText) _replyInput.Text = string.Empty;
                SetRoomReplyFeedback(roomKey, "서버 접수 및 저장 확인 · 답장을 보냈습니다.");
            }
            else
            {
                SetRoomReplyFeedback(roomKey, result != null && result.Status == ReplySendStatus.Failed ? "전송 실패 · 입력한 답장을 유지했습니다. 연결 상태를 확인해 주세요." : "전송 결과 불명 · 입력한 답장을 유지했습니다. 실제 대화를 확인한 뒤 재전송을 결정해 주세요.");
            }
            // The receiver's durable message event is the only path to an own bubble.
            UpdateReplyAvailability();
        }

        private void SetRoomReplyFeedback(string key, string text)
        {
            _roomReplyFeedback[key] = text;
            if (_selectedRoomKey == key) _replyFeedback.Text = text;
        }

        private void ResetRoomUiState()
        {
            ResetRoomRenderingState();
            _selectedRoomKey = null;
            _roomDrafts.Clear();
            _roomReplyFeedback.Clear();
            _replyPending = false;
            if (_replyInput != null) _replyInput.Clear();
            if (_roomSearchInput != null) _roomSearchInput.Clear();
        }

        private static bool ContainsQuery(string value, string query) { return (value ?? string.Empty).IndexOf(query, StringComparison.CurrentCultureIgnoreCase) >= 0; }
        private static bool SameConversationMessage(NotificationPreview existing, NotificationPreview incoming)
        {
            if (existing == null || incoming == null || string.IsNullOrEmpty(existing.Id) || existing.Id != incoming.Id) return false;
            string previousRoom = ChatRoomGrouping.CreateServerRoomKey(existing.ChatId);
            string nextRoom = ChatRoomGrouping.CreateServerRoomKey(incoming.ChatId);
            if (previousRoom != null && nextRoom != null) return previousRoom == nextRoom;
            // Exact old room+author evidence allows a legacy row to be
            // enriched; a display name alone never joins records.
            return !string.IsNullOrEmpty(existing.GroupKey) && existing.GroupKey == incoming.GroupKey;
        }





    }
}
