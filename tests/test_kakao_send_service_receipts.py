"""Offline batch safety checks with a fake bridge and deterministic clock."""
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kakao_direct import DirectSendError, DirectSendUnknownError
from kakao_send_run import SendRunSupervisor
from kakao_send_service import SendPorts, SendRunRequest, SendService, direct_send_batch_policy
from kakao_sender import MessageRow


class VirtualClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []
        self.cancel_on_wait = False

    def monotonic(self):
        return self.now

    def wait(self, event, seconds):
        self.waits.append(seconds)
        if self.cancel_on_wait:
            event.set()
            return True
        self.now += seconds
        return event.is_set()


def receipt(chat_id, log_id):
    return {"success": True, "status_code": 0, "chat_id": chat_id,
            "log_id": str(log_id), "sent_at": 1725000000 + log_id}


def make_request(count=3, **overrides):
    rows = tuple((index, MessageRow(index + 2, f"fixture-{index}", "synthetic text"))
                 for index in range(count))
    request = SendRunRequest(
        selected=rows, config={}, countdown=0, delay_scale=0,
        common_schedule=None, continue_on_error=True, confirm_each=False,
        direct_enabled=True,
        chat_mappings=tuple((row.chat_name, str(1000 + index)) for index, row in rows),
    )
    return replace(request, **overrides)


def run_fake(request, *, response=None, duration=0.0, cancel_on_wait=False, factory_error=None, confirmation="send"):
    clock = VirtualClock()
    clock.cancel_on_wait = cancel_on_wait
    starts, attempts, events = [], [], []

    def send(*, chat_id, text, attachments=(), attachment_run_id=None):
        starts.append(clock.now)
        ordinal = len(starts)
        clock.now += duration
        if response:
            return response(ordinal, chat_id)
        if attachments:
            kinds = (["text"] if text.strip() else []) + ["file"] * len(attachments)
            receipts = [dict(receipt(chat_id, ordinal * 10 + index), kind=kind)
                        for index, kind in enumerate(kinds, start=1)]
            return dict(receipts[-1], results=receipts, attachment_count=len(attachments))
        return receipt(chat_id, ordinal)

    bridge = SimpleNamespace(send_message=Mock(side_effect=send), send_attachments=Mock(side_effect=send),
                             prepare_attachments=Mock(), release_attachments=Mock())
    bridge_factory = Mock(return_value=bridge, side_effect=factory_error)
    ports = SendPorts(
        bridge_factory=bridge_factory, bridge_close=Mock(),
        confirm_row=Mock(return_value=confirmation),
        require_pyautogui=Mock(return_value=SimpleNamespace()),
        focus_kakao_window=Mock(return_value="Synthetic Kakao test title"),
        send_one=Mock(), send_scheduled_one=Mock(), return_to_recent_chat_top=Mock(),
        monotonic=clock.monotonic, wait_for_cancel=clock.wait,
    )
    service = SendService(ports)
    supervisor = SendRunSupervisor(events.append)

    def execute(context):
        context.observe_attempts(lambda index, route: attempts.append((index, route)))
        service.execute(request, context)

    completed = supervisor.run(execute)
    return SimpleNamespace(completed=completed, records=completed.legacy_results(), clock=clock,
                           starts=starts, attempts=attempts, events=events, bridge=bridge, ports=ports)


def test_85_row_missing_receipt_incident_stops_after_first_unconfirmed_reply():
    def response(ordinal, chat_id):
        result = receipt(chat_id, ordinal)
        if ordinal > 30:
            result["log_id"] = ""
        return result

    run = run_fake(make_request(85), response=response)

    assert run.completed.failure is None
    assert len(run.starts) == 31
    assert len(run.attempts) == 31
    assert Counter(record["status"] for record in run.records) == {
        "direct_sent": 30, "unknown": 1, "stopped": 54,
    }
    assert [record["row_index"] for record in run.records] == list(range(85))
    assert run.records[30]["retry_transport"] == "none"
    assert all(record["failure_stage"] == "direct_batch_not_attempted"
               and record["retry_transport"] == "none" for record in run.records[31:])
    run.ports.bridge_close.assert_called_once_with(run.bridge)


@pytest.mark.parametrize("bad_reply", [None, {}, {"success": True},
    {"success": True, "status_code": 0, "chat_id": "9999", "log_id": "3", "sent_at": 1},
    {"success": True, "status_code": 0, "chat_id": "1000", "log_id": "0", "sent_at": 1},
])
def test_malformed_text_reply_is_unknown_and_cannot_turn_next_rows_green(bad_reply):
    run = run_fake(make_request(), response=lambda _ordinal, _chat_id: bad_reply)
    assert [record["status"] for record in run.records] == ["unknown", "stopped", "stopped"]
    assert len(run.starts) == 1
    assert run.attempts == [(0, "direct")]


@pytest.mark.parametrize("error,current_status", [
    (DirectSendError(code="server_rejected", status_code=-777), "error"),
    (DirectSendError(code="rate_limited", status_code=429), "error"),
    (DirectSendUnknownError(), "unknown"),
    (RuntimeError("synthetic adapter failure"), "unknown"),
])
def test_provider_rejection_or_uncertain_exception_stops_remaining_batch(error, current_status):
    def fail(_ordinal, _chat_id):
        raise error

    run = run_fake(make_request(), response=fail)
    assert [record["status"] for record in run.records] == [current_status, "stopped", "stopped"]
    assert len(run.starts) == 1
    assert run.records[1]["failure_stage"] == "direct_batch_not_attempted"


def test_unattempted_scheduled_row_is_recorded_without_entering_its_sender():
    request = make_request(2)
    later = replace(request.selected[1][1], scheduled_at=datetime.now() + timedelta(days=1))
    request = replace(request, selected=(request.selected[0], (1, later)))
    run = run_fake(request, response=lambda _ordinal, _chat_id: {})
    assert run.records[1]["status"] == "stopped"
    assert run.records[1]["transport"] == "scheduled"
    assert run.records[1]["scheduled_at"]
    assert run.attempts == [(0, "direct")]
    run.ports.send_scheduled_one.assert_not_called()
    run.ports.send_one.assert_not_called()
    run.ports.return_to_recent_chat_top.assert_not_called()


def test_pre_dispatch_mapping_failure_does_not_claim_attempt_or_block_valid_next_row():
    request = make_request(2)
    run = run_fake(replace(request, chat_mappings=request.chat_mappings[1:]))
    assert [record["status"] for record in run.records] == ["error", "direct_sent"]
    assert run.attempts == [(1, "direct")]
    assert run.starts == [0.0]


def test_bridge_initialization_failures_remain_pre_dispatch_errors():
    run = run_fake(make_request(), factory_error=RuntimeError("synthetic setup failure"))
    assert [record["status"] for record in run.records] == ["error"] * 3
    assert run.attempts == []
    run.bridge.send_message.assert_not_called()


def test_85_valid_receipts_keep_every_row_and_pause_only_between_30_row_batches():
    run = run_fake(make_request(85))
    assert run.completed.failure is None
    assert len(run.records) == len(run.attempts) == len(run.starts) == 85
    assert all(record["status"] == "direct_sent" for record in run.records)
    assert run.starts == [0.0] * 30 + [2.0] * 30 + [4.0] * 25
    assert run.clock.waits == [2.0, 2.0]
    assert len({record["chat_id"] for record in run.records}) == 85
    assert len({record["log_id"] for record in run.records}) == 85


@pytest.mark.parametrize("configured", [0, -1, 0.5, None, False, "bad", float("nan"), float("inf")])
def test_invalid_or_short_pause_never_bypasses_minimum(configured):
    assert direct_send_batch_policy({"direct_send_batch_pause_seconds": configured}) == (30, 2.0)


@pytest.mark.parametrize("configured", [0, -1, 31, 1000000, 1.5, None, False, True, "3", float("nan"), float("inf")])
def test_invalid_or_oversized_batch_uses_default(configured):
    assert direct_send_batch_policy({"direct_send_batch_size": configured}) == (30, 2.0)


@pytest.mark.parametrize("configured", [1, 5, 30])
def test_valid_smaller_batch_sizes_are_honored(configured):
    run = run_fake(make_request(config={"direct_send_batch_size": configured}))
    assert run.starts == [2.0 * (index // configured) for index in range(3)]


def test_configured_pause_is_added_after_final_call_in_batch():
    run = run_fake(make_request(5, config={"direct_send_batch_size": 2,
                                          "direct_send_batch_pause_seconds": 4.0}), duration=1.0)
    assert run.starts == [0.0, 1.0, 6.0, 7.0, 12.0]
    assert run.clock.waits == [4.0, 4.0]


@pytest.mark.parametrize("count,waits", [(1, []), (30, []), (31, [2.0]), (60, [2.0])])
def test_first_batch_has_no_forced_per_row_wait_or_final_trailing_pause(count, waits):
    run = run_fake(make_request(count))
    assert run.clock.waits == waits


def test_old_per_message_interval_setting_no_longer_delays_rows():
    run = run_fake(make_request(config={"direct_send_min_interval_seconds": 10.0}))
    assert run.starts == [0.0, 0.0, 0.0]
    assert run.clock.waits == []


def test_pre_dispatch_mapping_error_is_not_counted_as_a_batch_call():
    request = make_request(5, config={"direct_send_batch_size": 2})
    run = run_fake(replace(request, chat_mappings=request.chat_mappings[1:]))
    assert run.starts == [0.0, 0.0, 2.0, 2.0]
    assert [record["status"] for record in run.records] == ["error"] + ["direct_sent"] * 4


def test_message_and_multiple_attachments_count_as_one_direct_row(tmp_path):
    request = make_request(3, config={"direct_send_batch_size": 2})
    attachments = tuple(tmp_path / name for name in ("synthetic-a.pdf", "synthetic-b.pdf"))
    for attachment in attachments:
        attachment.write_bytes(b"%PDF- synthetic")
    first = replace(request.selected[0][1], attachments=attachments)
    run = run_fake(replace(request, selected=((0, first), *request.selected[1:])))
    assert run.starts == [0.0, 0.0, 2.0]
    assert run.records[0]["direct_receipt_count"] == 3
    assert [record["status"] for record in run.records] == ["direct_sent"] * 3
    run.bridge.send_attachments.assert_called_once()
    assert run.bridge.send_message.call_count == 2


def test_cancel_during_pacing_never_marks_or_dispatches_next_attempt():
    run = run_fake(make_request(85), cancel_on_wait=True)
    assert run.completed.cancelled is True
    assert run.attempts == [(index, "direct") for index in range(30)]
    assert run.starts == [0.0] * 30
    assert [record["status"] for record in run.records] == ["direct_sent"] * 30 + ["stopped"]


def test_stop_in_confirmation_is_a_cancelled_run_and_never_sends():
    run = run_fake(make_request(3), confirmation="stop")
    assert run.completed.cancelled is True
    assert run.completed.failure is None
    assert run.starts == []
    assert run.attempts == []
    assert [record["status"] for record in run.records] == ["stopped"]
    run.ports.bridge_close.assert_called_once_with(run.bridge)
