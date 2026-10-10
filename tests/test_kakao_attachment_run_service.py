from types import SimpleNamespace
from unittest.mock import Mock, patch

from kakao_attachment_run import AttachmentRunSnapshot
from kakao_direct import DirectSendError
from kakao_sender import MessageRow
from kakao_send_run import SendRunSupervisor
from kakao_send_service import SendPorts, SendRunRequest, SendService


def execute(rows, bridge, *, confirm=None):
    ports = SendPorts(
        bridge_factory=Mock(return_value=bridge), bridge_close=lambda value: value.close(),
        confirm_row=confirm or Mock(return_value="send"), require_pyautogui=Mock(),
        focus_kakao_window=Mock(), send_one=Mock(), send_scheduled_one=Mock(),
        return_to_recent_chat_top=Mock(),
    )
    request = SendRunRequest(tuple(enumerate(rows)), {}, 0, 0, None, True, False,
                             direct_enabled=True, chat_mappings=tuple((row.chat_name, str(i+100)) for i, row in enumerate(rows)))
    attempts = []
    supervisor = SendRunSupervisor(lambda _: None)
    def work(context):
        context.observe_attempts(lambda index, route: attempts.append(index))
        SendService(ports).execute(request, context)
    return supervisor.run(work), attempts


def test_all_recipients_share_snapshot_even_if_original_is_edited_between_sends(tmp_path):
    source = tmp_path / "exam.pdf"
    source.write_bytes(b"%PDF- initial")
    rows = [MessageRow(i+2, str(i), "", (source,)) for i in range(3)]
    read = []
    bridge = SimpleNamespace(prepare_attachments=Mock(), release_attachments=Mock(), close=Mock())
    def send(*, attachments, chat_id, text, attachment_run_id):
        read.append((attachments, attachment_run_id, attachments[0].read_bytes()))
        source.write_bytes(b"%PDF- modified")
        receipt = dict(success=True, status_code=0, chat_id=chat_id, log_id=str(200+len(read)), sent_at=1800000000, kind="file")
        return dict(receipt, attachment_count=1, results=[receipt])
    bridge.send_attachments = Mock(side_effect=send)
    with patch.object(AttachmentRunSnapshot, "_copy", wraps=AttachmentRunSnapshot._copy) as copying:
        completed, attempts = execute(rows, bridge)
    assert completed.succeeded
    assert attempts == [0, 1, 2]
    assert copying.call_count == bridge.prepare_attachments.call_count == 1
    assert len({(paths, run_id) for paths, run_id, _ in read}) == 1
    assert [data for _, _, data in read] == [b"%PDF- initial"] * 3
    assert not read[0][0][0].exists()
    bridge.release_attachments.assert_called_once_with(attachment_run_id=read[0][1])
    bridge.close.assert_called_once()


def test_invalid_later_file_stops_before_any_recipient_dispatch(tmp_path):
    sources = [tmp_path / "good.pdf", tmp_path / "bad.pdf"]
    sources[0].write_bytes(b"%PDF- good")
    sources[1].write_bytes(b"bad format")
    rows = [MessageRow(i+2, str(i), "", (source,)) for i, source in enumerate(sources)]
    copied = []
    def prepare(*, attachments, attachment_run_id):
        copied.extend(attachments)
        if not attachments[0].read_bytes().startswith(b"%PDF-"):
            raise DirectSendError(code="invalid_pdf")
    bridge = SimpleNamespace(prepare_attachments=Mock(side_effect=prepare), release_attachments=Mock(),
                             close=Mock(), send_attachments=Mock())
    completed, attempts = execute(rows, bridge)
    assert completed.failure.exception_type == "DirectSendError"
    assert attempts == []
    bridge.send_attachments.assert_not_called()
    assert all(not path.exists() for path in copied)
    bridge.release_attachments.assert_called_once()
    bridge.close.assert_called_once()


def test_stop_at_confirmation_releases_prepared_files_without_sending(tmp_path):
    source = tmp_path / "exam.pdf"
    source.write_bytes(b"%PDF- initial")
    bridge = SimpleNamespace(prepare_attachments=Mock(), release_attachments=Mock(),
                             close=Mock(), send_attachments=Mock())
    completed, attempts = execute([MessageRow(2, "0", "", (source,))], bridge, confirm=Mock(return_value="stop"))
    assert completed.cancelled
    assert attempts == []
    bridge.send_attachments.assert_not_called()
    paths = bridge.prepare_attachments.call_args.kwargs["attachments"]
    assert not paths[0].exists()
    bridge.release_attachments.assert_called_once()
