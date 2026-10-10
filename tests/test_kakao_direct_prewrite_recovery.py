"""Connection-drop regression tests; synthetic credentials, zero network sends."""
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from kakao_direct import (CredentialVaultError, DirectBridgeError, DirectKakaoConfig, DirectSendError, DirectSendNotStartedError,
                          DirectSendUnknownError, KakaoDirectBridge, redact_error)
from kakao_run_journal import RunJournal
from kakao_send_run import SendRunSupervisor
from kakao_send_service import SendPorts, SendService
from test_kakao_direct import FakeProcess, NoCredentials
from test_kakao_send_service_receipts import VirtualClock, make_request, receipt


def execute(request, factory, *, cancel_on_wait=False, journal=None, close=None):
    clock = VirtualClock()
    clock.cancel_on_wait = cancel_on_wait
    events, attempts = [], []
    ports = SendPorts(bridge_factory=factory, bridge_close=close or Mock(side_effect=lambda bridge:bridge.close()),
        confirm_row=Mock(return_value="send"),require_pyautogui=Mock(side_effect=AssertionError("no macro")),
        focus_kakao_window=Mock(),send_one=Mock(side_effect=AssertionError("no macro")),
        send_scheduled_one=Mock(),return_to_recent_chat_top=Mock(),
        monotonic=clock.monotonic,wait_for_cancel=clock.wait)
    supervisor = SendRunSupervisor(events.append)
    def work(context):
        saved = journal.begin_run(str(context.run_id),request.selected) if journal else None
        def attempt(index,route):
            attempts.append(index)
            if saved: saved.mark_attempt(index,route)
        context.observe_attempts(attempt)
        if saved: context.observe_results(saved.add_result)
        try:
            SendService(ports).execute(request,context)
        finally:
            if saved: saved.finish("failed")
    completed = supervisor.run(work)
    return SimpleNamespace(completed=completed,records=completed.legacy_results(),attempts=attempts,
                           ports=ports,events=events,clock=clock)


def prewrite(**changes):
    return {"code":"session_acquire_failed","kind":"explicit","ambiguous":False,
            "send_phase":"prewrite","retryable_prewrite":True,"cause_code":"ECONNRESET",**changes}


@pytest.mark.parametrize("changes,expected", [
    ({},DirectSendNotStartedError),
    ({"send_phase":None},DirectSendError),
    ({"kind":"unknown","ambiguous":True},DirectSendUnknownError),
    ({"status_code":-1},DirectSendUnknownError),
    ({"code":"server_rejected"},DirectSendError),
])
def test_only_explicit_prewrite_proof_can_request_connection_recovery(changes,expected):
    process=FakeProcess(send_error=prewrite(**changes))
    with KakaoDirectBridge(DirectKakaoConfig(enabled=True),credential_vault=NoCredentials(),
                          popen_factory=lambda *_a,**_k:process) as bridge:
        with pytest.raises(expected) as caught:
            bridge.send_message(chat_id="123",text="synthetic")
        assert type(caught.value) is expected
        if expected is DirectSendNotStartedError:
            assert caught.value.retryable_prewrite is True
            assert "ECONNRESET" in redact_error(caught.value)


def test_persistent_prewrite_failure_is_bounded_and_every_unsent_row_is_resumable(tmp_path):
    request=make_request(80)
    created=[]
    wire=[]
    def factory(_config):
        bridge=Mock()
        def send(*,chat_id,text):
            if int(chat_id)>=1030:
                raise DirectSendNotStartedError(retryable=True,cause_code="login_failed")
            wire.append(chat_id)
            return receipt(chat_id,len(wire))
        bridge.send_message.side_effect=send
        created.append(bridge)
        return bridge
    journal=RunJournal(tmp_path/"runs.sqlite3")
    run=execute(request,factory,journal=journal)
    assert len(wire)==30 and len(created)==3
    assert Counter(r["status"] for r in run.records)=={"direct_sent":30,"stopped":50}
    assert run.records[30]["failure_stage"]=="direct_prewrite"
    assert run.records[30]["retry_transport"]=="direct"
    assert run.attempts==list(range(31))  # retries do not create another row attempt
    assert run.clock.waits==[2.0,2.0,2.0]  # batch pause + two bounded recovery waits
    assert journal.load_recovery(str(run.completed.run_id)).resumable_row_indexes==tuple(range(30,80))
    for bridge in created: bridge.close.assert_called_once()


@pytest.mark.parametrize("error", [DirectSendUnknownError(),DirectSendError(code="session_acquire_failed"),
                                   DirectSendNotStartedError(cause_code="login_rejected")])
def test_ambiguous_legacy_or_terminal_errors_never_create_another_connection(error):
    bridge=Mock()
    bridge.send_message.side_effect=error
    factory=Mock(return_value=bridge)
    run=execute(make_request(),factory)
    assert factory.call_count==1
    bridge.send_message.assert_called_once()
    assert run.clock.waits==[]


def test_cancel_during_reconnection_never_opens_replacement_or_sends_current_row():
    bridge=Mock()
    bridge.send_message.side_effect=DirectSendNotStartedError(retryable=True,cause_code="ECONNRESET")
    factory=Mock(return_value=bridge)
    run=execute(make_request(),factory,cancel_on_wait=True)
    assert run.completed.cancelled is True
    assert factory.call_count==1 and bridge.send_message.call_count==1
    bridge.close.assert_called_once()
    assert run.records[0]["status"]=="stopped"


@pytest.mark.parametrize("failure_stage",["close","start"])
def test_replacement_lifecycle_failure_keeps_every_unsent_row_resumable(tmp_path,failure_stage):
    first=Mock()
    first.send_message.side_effect=DirectSendNotStartedError(retryable=True,cause_code="ECONNRESET")
    if failure_stage=="close":
        first.close.side_effect=RuntimeError("PRIVATE")
    factory=Mock(side_effect=[first,RuntimeError("PRIVATE")])
    journal=RunJournal(tmp_path/"runs.sqlite3")
    run=execute(make_request(3),factory,journal=journal)
    assert factory.call_count==(1 if failure_stage=="close" else 2)
    first.send_message.assert_called_once()
    first.close.assert_called_once()
    assert Counter(row["status"] for row in run.records)=={"stopped":3}
    assert run.records[0]["failure_stage"]=="direct_prewrite"
    assert f"bridge_{failure_stage}_failed" in run.records[0]["detail"]
    assert "PRIVATE" not in run.records[0]["detail"]
    assert journal.load_recovery(str(run.completed.run_id)).resumable_row_indexes==(0,1,2)


def test_gui_completion_counts_current_prewrite_row_and_following_rows_as_unsent():
    from kakao_send_run import SendRunCompleted, SendRunId, SendRunResult
    from kakao_sender_gui import KakaoSenderApp

    request=make_request(80)
    records=[dict(row_index=i,row=i+2,chat_name=row.chat_name,
                  status="direct_sent" if i<30 else "stopped",
                  failure_stage="" if i<30 else ("direct_prewrite" if i==30 else "direct_batch_not_attempted"),
                  retry_transport="direct" if i==30 else "none")
             for i,row in request.selected]
    completed=SendRunCompleted(SendRunId.new(),tuple(SendRunResult(row) for row in records))
    app=KakaoSenderApp.__new__(KakaoSenderApp)
    app.active_run=SimpleNamespace(run_id=completed.run_id)
    app.rows=[row for _,row in request.selected]
    app._close_requested=False
    app.advance_run_origins=Mock()
    app.log=Mock()
    app.status_var=Mock()
    app.restore_send_controls=Mock()
    app.update_summary=Mock()
    with patch("kakao_gui_feedback.write_log",return_value=Path("synthetic.csv")):
        app.finish_send_run(completed)
    text=app.status_var.set.call_args.args[0]
    assert "연결 복구 중단" in text and "성공 30건" in text and "미시도 50건" in text
    assert "완료" not in text and "2차 전송" not in text
    assert "미시도 50건" in app.log.call_args.args[1]
    assert app.active_run is None


def test_attachment_batch_is_retried_only_when_session_failed_before_all_uploads(tmp_path):
    first,second=Mock(),Mock()
    first.send_attachments.side_effect=DirectSendNotStartedError(retryable=True,cause_code="ECONNRESET")
    outputs=[dict(receipt("1000",1),kind="text"),dict(receipt("1000",2),kind="file")]
    second.send_attachments.return_value=dict(outputs[-1],results=outputs,attachment_count=1)
    request=make_request(1)
    attachment = tmp_path / "fixture.pdf"
    attachment.write_bytes(b"%PDF-1.4 synthetic")
    request=replace(request,selected=((0,replace(request.selected[0][1],attachments=(attachment,))),))
    def disconnect_before_upload(**_arguments):
        attachment.unlink()
        raise DirectSendNotStartedError(retryable=True, cause_code="ECONNRESET")
    first.send_attachments.side_effect = disconnect_before_upload
    reconnected_contents = []
    def send_frozen(**arguments):
        reconnected_contents.append(arguments["attachments"][0].read_bytes())
        return dict(outputs[-1], results=outputs, attachment_count=1)
    second.send_attachments.side_effect = send_frozen
    run=execute(request,Mock(side_effect=[first,second]))
    assert run.records[0]["status"]=="direct_sent"
    assert run.attempts==[0]
    first.send_message.assert_not_called()
    second.send_message.assert_not_called()
    first.close.assert_called_once()
    second.close.assert_called_once()
    assert first.send_attachments.call_args.kwargs == second.send_attachments.call_args.kwargs
    assert reconnected_contents == [b"%PDF-1.4 synthetic"]
    second.prepare_attachments.assert_called_once_with(
        attachments=second.send_attachments.call_args.kwargs["attachments"],
        attachment_run_id=second.send_attachments.call_args.kwargs["attachment_run_id"],
    )


def test_reconnected_attachment_preparation_failure_remains_unattempted_and_resumable(tmp_path):
    first, second = Mock(), Mock()
    first.send_attachments.side_effect = DirectSendNotStartedError(retryable=True, cause_code="ECONNRESET")
    second.prepare_attachments.side_effect = DirectBridgeError(code="timeout")
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"%PDF- synthetic")
    request = make_request(2)
    request = replace(request, selected=tuple((index, replace(row, attachments=(source,))) for index, row in request.selected))
    run = execute(request, Mock(side_effect=[first, second]))
    assert [row["status"] for row in run.records] == ["stopped", "stopped"]
    assert run.records[0]["failure_stage"] == "direct_prewrite"
    assert run.records[0]["retry_transport"] == "direct"
    assert run.attempts == [0]
    second.send_attachments.assert_not_called()
    assert not first.send_attachments.call_args.kwargs["attachments"][0].exists()


class SyntheticVault:
    @property
    def path(self):
        if not hasattr(self, "_directory"):
            self._directory = tempfile.TemporaryDirectory(prefix="synthetic-auth-lease-")
        return Path(self._directory.name) / "loco-credentials.dat"
    def load_snapshot(self):
        from kakao_direct import CredentialSnapshot
        return CredentialSnapshot(self.load(), str(getattr(self, "revision", 0)))
    def save_if_unchanged(self, credentials, expected_revision):
        from kakao_direct import CredentialSnapshot, CredentialVaultConflictError
        if expected_revision != str(getattr(self, "revision", 0)):
            raise CredentialVaultConflictError("Synthetic conflict")
        self.save(credentials)
        self.revision = getattr(self, "revision", 0) + 1
        return CredentialSnapshot(dict(credentials), str(self.revision))
    def load(self):
        return {"oauth_token":"synthetic-only-token","user_id":"1","device_uuid":"synthetic-test","device_type":"tablet"}
    def save(self,_credentials):
        pass


def fake_sdk(tmp_path,fail_after):
    package=tmp_path/"sdk"
    package.mkdir()
    (package/"package.json").write_text(json.dumps({"name":"agent-messenger","version":"2.37.1","type":"module"}),encoding="utf-8")
    bson=package/"node_modules"/"bson"
    bson.mkdir(parents=True)
    (bson/"index.js").write_text("class Long { static fromString(v) { return new Long(v); } static isLong(v) { return v instanceof Long; } constructor(v) { this.v=v; } toString() { return this.v; } }; module.exports={Long};",encoding="utf-8")
    trace=tmp_path/"wire.jsonl"
    source='''
import {appendFileSync} from 'node:fs';
import net from 'node:net';
net.Socket.prototype.connect=function(){throw new Error('NETWORK IS FORBIDDEN IN TEST');};
globalThis.fetch=()=>{throw new Error('NETWORK IS FORBIDDEN IN TEST');};
const record=value=>appendFileSync(TRACE,JSON.stringify(value)+'\\n');
export class KakaoTalkClient {
  constructor(){this.sent=0;record({event:'client'});}
  async login(value){this.credentials=value;}
  getCredentials(){return this.credentials;}
  onSessionEvent(){}
  close(){record({event:'closed'});}
  async acquireSession(){
    if(this.sent>=LIMIT){record({event:'acquire_failed'});throw Object.assign(new Error('PRIVATE'),{code:'login_failed',cause:Object.assign(new Error('PRIVATE'),{code:'ECONNRESET'})});}
    return {sendMessage:async(chatId,text)=>{
      this.sent++;const id=String(chatId);record({event:'WRITE',chat_id:id});
      return {method:'WRITE',statusCode:0,body:{logId:String(900000+Number(id)),sendAt:1800000000}};
    }};
  }
}
export async function refreshKakaoOAuthToken(){record({event:'forbidden_refresh'});throw new Error('network errors must not refresh OAuth');}
'''.replace('TRACE',json.dumps(str(trace))).replace('LIMIT',str(fail_after))
    entry=package/"index.mjs"
    entry.write_text(source,encoding="utf-8")
    return entry,trace


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_unsaved_login_credentials_close_session_before_any_write(tmp_path):
    sdk, trace = fake_sdk(tmp_path, 999)
    class FailedSaveVault(SyntheticVault):
        def save(self, _credentials):
            raise CredentialVaultError("synthetic private error")
    config = DirectKakaoConfig(enabled=True, node_path=shutil.which("node"), sdk_root=sdk, request_timeout=3.0)
    with KakaoDirectBridge(config, credential_vault=FailedSaveVault()) as bridge:
        with pytest.raises(DirectBridgeError) as raised:
            bridge.send_message(chat_id="1000", text="synthetic")
    assert raised.value.code == "credential_save_failed"
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert not any(row["event"] == "WRITE" for row in records)
    assert any(row["event"] == "closed" for row in records)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_concurrent_rotation_reports_conflict_and_closes_child_before_write(tmp_path):
    sdk, trace = fake_sdk(tmp_path, 999)

    class ConcurrentRotationVault(SyntheticVault):
        def __init__(self):
            self.value = super().load()

        def load(self):
            return dict(self.value)

        def save_if_unchanged(self, credentials, expected_revision):
            self.value = {**self.value, "oauth_token": "synthetic-other-app"}
            self.revision = getattr(self, "revision", 0) + 1
            return super().save_if_unchanged(credentials, expected_revision)

    vault = ConcurrentRotationVault()
    config = DirectKakaoConfig(enabled=True, node_path=shutil.which("node"), sdk_root=sdk, request_timeout=3.0)
    with KakaoDirectBridge(config, credential_vault=vault) as bridge:
        with pytest.raises(DirectBridgeError) as raised:
            bridge.send_message(chat_id="1000", text="synthetic")
    assert raised.value.code == "credential_vault_conflict"
    assert vault.load()["oauth_token"] == "synthetic-other-app"
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert not any(row["event"] == "WRITE" for row in records)
    assert any(row["event"] == "closed" for row in records)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_rotated_oauth_credentials_must_be_saved_before_retrying_session_acquisition(tmp_path):
    sdk, trace = fake_sdk(tmp_path, 999)
    source = sdk.read_text(encoding="utf-8")
    source = source.replace("async acquireSession(){", "async acquireSession(){if(this.credentials.oauthToken!=='rotated-token') throw Object.assign(new Error('PRIVATE'),{code:'invalid_access_token'});")
    source = source.replace("record({event:'forbidden_refresh'});throw new Error('network errors must not refresh OAuth');",
                            "record({event:'refresh'});return {accessToken:'rotated-token',refreshToken:'rotated-refresh'};")
    sdk.write_text(source, encoding="utf-8")
    class FailedRotationSaveVault(SyntheticVault):
        def load(self):
            return {**super().load(), "refresh_token":"synthetic-refresh"}
        def save(self, credentials):
            if credentials["oauth_token"] == "rotated-token":
                raise CredentialVaultError("synthetic private error")
    config = DirectKakaoConfig(enabled=True, node_path=shutil.which("node"), sdk_root=sdk, request_timeout=3.0)
    with KakaoDirectBridge(config, credential_vault=FailedRotationSaveVault()) as bridge:
        with pytest.raises(DirectSendNotStartedError) as raised:
            bridge.send_message(chat_id="1000", text="synthetic")
    assert raised.value.cause_code == "credential_save_failed"
    assert raised.value.retryable_prewrite is False
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in records].count("refresh") == 1
    assert [row["event"] for row in records].count("closed") == 1
    assert [row["event"] for row in records].count("client") == 1
    assert not any(row["event"] == "WRITE" for row in records)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required")
def test_rotated_credentials_survive_network_failure_during_fresh_login(tmp_path):
    sdk, trace = fake_sdk(tmp_path, 999)
    source = sdk.read_text(encoding="utf-8")
    source = source.replace("async acquireSession(){", "async acquireSession(){throw Object.assign(new Error('PRIVATE'),{code:'invalid_access_token'});")
    source = source.replace("async login(value){this.credentials=value;}",
                            "async login(value){record({event:'login',oauth_token:value.oauthToken});this.credentials=value;if(value.oauthToken==='rotated-token') throw Object.assign(new Error('PRIVATE'),{code:'ECONNRESET'});}")
    source = source.replace("record({event:'forbidden_refresh'});throw new Error('network errors must not refresh OAuth');",
                            "record({event:'refresh'});return {accessToken:'rotated-token',refreshToken:'rotated-refresh'};")
    sdk.write_text(source, encoding="utf-8")
    class RecordingVault(SyntheticVault):
        def __init__(self):
            self.value = {**super().load(), "refresh_token":"synthetic-refresh"}
        def load(self):
            return dict(self.value)
        def save(self, credentials):
            self.value = dict(credentials)
            with trace.open("a", encoding="utf-8") as output:
                output.write(json.dumps({"event":"saved", "oauth_token":credentials["oauth_token"]}) + "\n")
    vault = RecordingVault()
    config = DirectKakaoConfig(enabled=True, node_path=shutil.which("node"), sdk_root=sdk, request_timeout=3.0)
    with KakaoDirectBridge(config, credential_vault=vault) as bridge:
        with pytest.raises(DirectSendNotStartedError) as raised:
            bridge.send_message(chat_id="1000", text="synthetic")
    assert raised.value.cause_code == "ECONNRESET"
    assert vault.value["oauth_token"] == "rotated-token"
    assert vault.value["refresh_token"] == "rotated-refresh"
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    saved = records.index({"event":"saved", "oauth_token":"rotated-token"})
    fresh_login = records.index({"event":"login", "oauth_token":"rotated-token"})
    assert saved < fresh_login
    assert not any(row["event"] == "WRITE" for row in records)


@pytest.mark.skipif(shutil.which("node") is None,reason="Node required")
@pytest.mark.parametrize("fail_after,expected_clients,expected_waits",[(999,1,[2.0,2.0]),(30,3,[2.0,2.0,2.0,2.0])])
def test_real_python_node_bridge_finishes_80_rows_once_after_boundary_connection_drops(tmp_path,fail_after,expected_clients,expected_waits):
    sdk,trace=fake_sdk(tmp_path,fail_after)
    config=DirectKakaoConfig(enabled=True,node_path=shutil.which("node"),sdk_root=sdk,request_timeout=3.0)
    created=[]
    def factory(_config):
        bridge=KakaoDirectBridge(config,credential_vault=SyntheticVault())
        created.append(bridge)
        return bridge
    run=execute(make_request(80),factory)
    records=[json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    writes=[row['chat_id'] for row in records if row['event']=='WRITE']
    assert run.completed.failure is None
    assert Counter(row['status'] for row in run.records)=={'direct_sent':80}
    assert writes==[str(1000+i) for i in range(80)]
    assert len(set(writes))==80
    assert run.attempts==list(range(80))
    assert len(created)==expected_clients
    assert run.clock.waits==expected_waits
    assert not any(row['event']=='forbidden_refresh' for row in records)
    assert all(not bridge.is_running for bridge in created)
