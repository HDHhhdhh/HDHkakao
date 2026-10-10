"""Replay receipt timing with fixture pixels and mock input; no live GUI."""
from datetime import datetime
from unittest.mock import Mock

from PIL import Image, ImageDraw
import pytest

import kakao_native_schedule as native
from kakao_native_schedule import NativeScheduleDriver, NativeWindow
from kakao_schedule_journal import ScheduleJournal, ScheduleJournalError
from kakao_sender import FocusSafetyError, MessageRow
from kakao_windows_ocr import OcrLine, OcrRect, OcrResult, OcrWord, WindowsOcrError


def observed(text):
    rect = OcrRect(0, 0, 150, 20)
    return OcrResult(200, 40, 'ko', (OcrLine(text, (OcrWord(text, rect),), rect),))


@pytest.mark.parametrize('size',[(320,600),(760,1280),(1032,1430),(1600,1280),(1040,2800)])
def test_toast_detection_handles_chat_size_without_scaling_the_toast(tmp_path,size):
    width,height=size
    image=Image.new('RGB',size,(83,83,83))
    toast_width=min(366,width//2);toast_height=40 if width<500 else 80
    left=(width-toast_width)//2;top=(height-toast_height)//2
    ImageDraw.Draw(image).rounded_rectangle((left,top,left+toast_width,top+toast_height),radius=4,fill=(12,12,12))
    path=tmp_path/'chat.png';image.save(path)
    saved=NativeScheduleDriver.find_toast_image(path)
    with Image.open(saved) as crop:
        assert toast_height-10<=crop.height<=toast_height+2


def test_black_glyph_pixels_without_a_continuous_background_are_not_a_toast(tmp_path):
    image=Image.new('RGB',(1032,1430),'white')
    draw=ImageDraw.Draw(image)
    for x in range(250,780,4):draw.rectangle((x,675,x+1,755),fill='black')
    path=tmp_path/'chat-text.png';image.save(path)
    with pytest.raises(FocusSafetyError,match='검은 배경'):
        NativeScheduleDriver.find_toast_image(path)


class Clock:
    def __init__(self):
        self.now = 0.

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(native.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(native.time, 'sleep', clock.sleep)
    paths = {}
    for label, color in [('empty', None), ('fading', (90, 90, 90)), ('toast', (12, 12, 12)), ('other', (22, 22, 22))]:
        image = Image.new('RGB', (320, 600), (210, 220, 225))
        if color:
            ImageDraw.Draw(image).rectangle((80, 280, 240, 320), fill=color)
        paths[label] = tmp_path / (label+'.png')
        image.save(paths[label])
    paths['form'] = tmp_path/'form.png'
    Image.new('RGB', (740, 1200), 'white').save(paths['form'])
    row = MessageRow(1, 'fixture room', 'fixture body', scheduled_at=datetime(2099, 1, 1, 10))
    chat = NativeWindow(2, row.chat_name, 'EVA', 9, (0, 0, 320, 600))
    form = NativeWindow(1, '예약 메시지', 'EVA', 9, (320, 0, 1060, 1200))
    driver = NativeScheduleDriver.__new__(NativeScheduleDriver)
    driver.gui = Mock(); driver.input = Mock()
    driver.guard = Mock(); driver.check = Mock(); driver.log = Mock()
    driver.gui.GetAncestor.return_value = form.handle
    driver.gui.IsWindow.return_value = False
    driver.body = Mock(return_value=row.text)
    driver.last_verification = {'form_screenshot': str(paths['form'])}
    journal = ScheduleJournal(tmp_path/'journal.sqlite3')
    journal.prepare([row])

    def frames(sequence, before='empty'):
        index = 0
        def capture(window, label):
            nonlocal index
            if label == 'before-submit-chat':
                return paths[before]
            if label in ('dispatch-boundary','submission-form-still-visible'):
                return paths['form']
            assert label == 'submitted-chat'
            path = paths[sequence[min(index, len(sequence)-1)]]
            index += 1
            return path
        driver.capture = Mock(side_effect=capture)

    def submit():
        return driver.submit(row, chat, form, paths['form'], journal)

    return driver, journal, row, chat, paths, clock, frames, submit


def test_delayed_fading_toast_is_acknowledged_without_another_click(scenario, monkeypatch):
    driver, journal, row, _, paths, clock, frames, submit = scenario
    frames(['empty', 'fading', 'toast'])
    read = Mock(return_value=observed('메시지를 예약했습니다.'))
    monkeypatch.setattr(native, 'recognize_image', read)
    assert submit() == paths['toast']
    driver.input.click.assert_called_once()
    record = journal.get(row)
    assert record.state == 'acknowledged'
    assert record.events[-1]['evidence']['receipt_wait_frames'] == 3
    assert record.events[-1]['evidence']['receipt_wait_seconds'] == pytest.approx(.24)
    assert clock.now < 1  # No unconditional five-second sleep.
    assert 0 < read.call_args.kwargs['timeout'] <= 5
    with pytest.raises(ScheduleJournalError):
        journal.mark_submitting(row)


def test_server_delay_over_three_seconds_does_not_stop_or_reclick(scenario,monkeypatch):
    driver,journal,row,_,paths,clock,frames,submit=scenario
    frames(['toast'])
    driver.gui.IsWindow.side_effect=lambda handle:clock.now<4
    driver.gui.IsWindowVisible.return_value=True
    monkeypatch.setattr(native,'recognize_image',Mock(return_value=observed('메시지를 예약했습니다.')))
    assert submit()==paths['toast']
    driver.input.click.assert_called_once()
    record=journal.get(row)
    assert record.state=='acknowledged'
    assert 3<record.events[-1]['evidence']['form_close_wait_seconds']<4.1
    assert 4<=clock.now<4.2


def test_form_that_never_closes_stays_uncertain_with_saved_evidence(scenario,monkeypatch):
    driver,journal,row,_,paths,clock,frames,submit=scenario
    frames(['empty'])
    driver.gui.IsWindow.return_value=True
    driver.gui.IsWindowVisible.return_value=True
    monkeypatch.setattr(native,'recognize_image',Mock())
    with pytest.raises(FocusSafetyError,match='양식이 남아'):
        submit()
    driver.input.click.assert_called_once()
    record=journal.get(row)
    assert record.state=='unknown' and not record.safe_to_submit
    assert 10<=record.events[-1]['evidence']['form_close_wait_seconds']<10.1
    assert record.events[-1]['evidence']['held_form_screenshot']==str(paths['form'])


def test_missing_toast_has_bounded_wait_and_keeps_dispatch_uncertain(scenario, monkeypatch):
    driver, journal, row, _, paths, clock, frames, submit = scenario
    frames(['empty'])
    read = Mock()
    monkeypatch.setattr(native, 'recognize_image', read)
    with pytest.raises(FocusSafetyError, match='5초'):
        submit()
    driver.input.click.assert_called_once()
    read.assert_not_called()
    record = journal.get(row)
    assert record.state == 'unknown' and not record.safe_to_submit
    assert record.events[-1]['evidence']['submitted_chat_screenshot'] == str(paths['empty'])
    assert record.events[-1]['evidence']['receipt_wait_frames'] > 1
    assert record.events[-1]['evidence']['receipt_wait_seconds'] == pytest.approx(5)
    assert clock.now == pytest.approx(5.14)


def test_other_notification_is_not_a_receipt_and_identical_crops_are_read_once(scenario, monkeypatch):
    driver, journal, row, _, _, _, frames, submit = scenario
    frames(['other'])
    read = Mock(return_value=observed('예약 메시지는 최대 100개입니다.'))
    monkeypatch.setattr(native, 'recognize_image', read)
    with pytest.raises(FocusSafetyError, match='정확한 문구'):
        submit()
    assert read.call_count == 2  # Original and light lettering, once per distinct crop.
    assert journal.get(row).state == 'unknown'
    driver.input.click.assert_called_once()


def test_new_pixels_retry_incomplete_animation_text(scenario, monkeypatch):
    _, journal, row, _, _, _, frames, submit = scenario
    frames(['other', 'other', 'toast'])
    read = Mock(side_effect=[observed('메시지를'), observed('메시지를'), observed('메시지를 예약했습니다.')])
    monkeypatch.setattr(native, 'recognize_image', read)
    submit()
    assert read.call_count == 3
    assert journal.get(row).state == 'acknowledged'


def test_transient_ocr_failure_retries_without_reclick(scenario, monkeypatch):
    driver, journal, row, _, _, _, frames, submit = scenario
    frames(['toast'])
    read = Mock(side_effect=[WindowsOcrError('temporary OCR failure'), observed('메시지를 예약했습니다.')])
    monkeypatch.setattr(native, 'recognize_image', read)
    submit()
    assert journal.get(row).state == 'acknowledged'
    driver.input.click.assert_called_once()
    assert read.call_count == 2


def test_previous_toast_cannot_be_used_for_new_registration(scenario, monkeypatch):
    driver, journal, row, _, _, _, frames, submit = scenario
    frames(['toast'], before='toast')
    monkeypatch.setattr(native, 'recognize_image', Mock(return_value=observed('메시지를 예약했습니다.')))
    with pytest.raises(FocusSafetyError, match='이전 접수'):
        submit()
    driver.input.click.assert_not_called()
    assert journal.get(row).state == 'prepared'


def test_cancel_during_wait_keeps_unknown_and_stops_observing(scenario, monkeypatch):
    driver, journal, row, _, _, clock, frames, submit = scenario
    frames(['empty'])
    monkeypatch.setattr(native, 'recognize_image', Mock())
    def check():
        if clock.now >= .26:
            raise InterruptedError('Esc')
    driver.check.side_effect = check
    with pytest.raises(InterruptedError):
        submit()
    driver.input.click.assert_called_once()
    assert journal.get(row).state == 'unknown'
    assert clock.now < 1


def test_window_change_during_wait_does_not_acknowledge_or_reclick(scenario, monkeypatch):
    driver, journal, row, _, _, _, frames, submit = scenario
    frames(['empty'])
    original = driver.capture.side_effect
    def capture(window, label):
        if label == 'submitted-chat':
            raise FocusSafetyError('window moved')
        return original(window, label)
    driver.capture.side_effect = capture
    monkeypatch.setattr(native, 'recognize_image', Mock())
    with pytest.raises(FocusSafetyError, match='window moved'):
        submit()
    assert journal.get(row).state == 'unknown'
    driver.input.click.assert_called_once()
