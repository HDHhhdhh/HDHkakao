from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
import threading

from kakao_sender import MessageRow
from kakao_sender_gui import KakaoSenderApp
from kakao_schedule_capacity import ScheduleCapacity, CapacityError


def test_native_reservation_preflight_needs_no_legacy_coordinate_or_mapping():
    app=KakaoSenderApp.__new__(KakaoSenderApp)
    due=(datetime.now()+timedelta(days=1)).replace(hour=10,minute=0,second=0,microsecond=0)
    row=MessageRow(2,'fixture','fixture body',scheduled_at=due)
    app.rows=[row];app.common_schedule_datetime=Mock(return_value=None)
    app.common_schedule_var=SimpleNamespace(get=lambda:False)
    app.direct_enabled_var=SimpleNamespace(get=lambda:True)
    app.coord_x_var=Mock();app.coord_x_var.get.side_effect=AssertionError('coordinates unnecessary')
    app.coord_y_var=app.coord_x_var
    app.direct_mappings={};app.direct_mapping_labels={};app._latest_chat_snapshot=()
    app.status_var=Mock();app.log=Mock();app.log_check_progress=Mock()
    app.preflight_problem=Mock(return_value=False)
    assert app.preflight([(0,row)]) is True
    app.coord_x_var.get.assert_not_called()
    app.preflight_problem.assert_not_called()


def test_counter_renders_clock_expiry_as_slot_release_not_delivery(tmp_path):
    app=KakaoSenderApp.__new__(KakaoSenderApp)
    app.schedule_counter_var=Mock();app.schedule_counter_detail_var=Mock()
    now=datetime(2026,9,28,16)
    counter=ScheduleCapacity(tmp_path/'counter.sqlite3')
    item=MessageRow(2,'fixture','fixture',scheduled_at=now+timedelta(seconds=30))
    counter.journal.prepare([item]);counter.journal.mark_submitting(item);counter.journal.mark_acknowledged(item)
    app.render_schedule_capacity(counter.view(now+timedelta(seconds=35)))
    label=app.schedule_counter_var.set.call_args.args[0]
    assert '추정 0/100' in label and '여유 100개' in label
    detail=app.schedule_counter_detail_var.set.call_args.args[0]
    assert '예약 시각 경과 1건' in detail and '발송 완료 확인 아님' in detail


def test_counter_refresh_needs_no_csv_screen_guard_or_native_session(tmp_path):
    app=KakaoSenderApp.__new__(KakaoSenderApp)
    app.rows=[]
    app.schedule_counter_var=Mock();app.schedule_counter_detail_var=Mock();app.status_var=Mock();app.post=Mock()
    with patch('kakao_sender_gui.SCHEDULE_JOURNAL_PATH',tmp_path/'counter.sqlite3'), \
         patch('kakao_sender_gui.NativeScheduleSession') as session, \
         patch('kakao_sender_gui.pc_kakao_ui_lease') as lease:
        app.refresh_schedule_capacity()
    assert '추정 0/100' in app.schedule_counter_var.set.call_args.args[0]
    session.assert_not_called();lease.assert_not_called();app.post.assert_not_called()


def test_counter_refresh_does_not_hide_a_damaged_local_record():
    app=KakaoSenderApp.__new__(KakaoSenderApp)
    app._schedule_capacity_store=Mock()
    app._schedule_capacity_store.view.side_effect=CapacityError('fixture corrupt ledger')
    app.schedule_counter_var=Mock();app.schedule_counter_detail_var=Mock();app.status_var=Mock()
    app.refresh_schedule_capacity()
    assert '자동 등록 차단' in app.schedule_counter_var.set.call_args.args[0]
    assert 'corrupt ledger' in app.schedule_counter_detail_var.set.call_args.args[0]
