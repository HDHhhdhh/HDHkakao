"""Observed, screenshot-checked KakaoTalk native reservation controls.

This is PC UI automation, not a server reservation API. Coordinates use the
current window's physical pixels; saved screenshots and native body readback
are the evidence for each operation. No code path sends an immediate message.
"""
from __future__ import annotations

import calendar
import ctypes
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
from typing import Any

from kakao_sender import (
    FocusSafetyError, MessageRow, activate_window_handle, find_kakao_windows,
    is_kakao_process_id, set_clipboard, validate_schedule_row,
)
from kakao_schedule_readback import (
    ScheduleReadbackError, read_schedule_form, verify_schedule_form,
    read_calendar_month, find_dropdown_option_center, has_schedule_success_toast,
)
from kakao_windows_ocr import OcrResult, OcrLine, OcrWord, OcrRect, WindowsOcrError, recognize_image, use_ocr_session
from kakao_ocr_worker import WindowsOcrSession


@dataclass(frozen=True)
class NativeWindow:
    handle: int
    title: str
    class_name: str
    pid: int
    rect: tuple[int, int, int, int]

    @property
    def width(self):
        return self.rect[2] - self.rect[0]

    @property
    def height(self):
        return self.rect[3] - self.rect[1]


class NativeScheduleDriver:
    """One UI owner; an Esc press, lost focus, or failed readback stops input."""

    def __init__(self, output: Path, cancel_event=None):
        import win32gui
        import win32process
        import pyautogui
        self.gui, self.process, self.input = win32gui, win32process, pyautogui
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        self.previous_dpi = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not self.previous_dpi:
            raise FocusSafetyError("물리 픽셀 좌표계를 설정하지 못했습니다.")
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.cancel_event = cancel_event
        self.stopped = False
        self.seq = len(list(self.output.glob("*.png")))
        self.created_forms = set()
        self.previous_input_pause = self.input.PAUSE
        self.previous_input_failsafe = self.input.FAILSAFE
        self.input.PAUSE = .04
        self.input.FAILSAFE = True
        # Discard historical key state before this explicitly started run.
        self.user.GetAsyncKeyState(0x1B)
        self._ocr_session = WindowsOcrSession(cancel_event=cancel_event)
        self._ocr_scope = use_ocr_session(self._ocr_session)
        self._ocr_scope.__enter__()

    def check(self):
        if self.stopped or (self.cancel_event is not None and self.cancel_event.is_set()):
            raise InterruptedError("예약 자동화가 중단됐습니다.")
        if self.user.GetAsyncKeyState(0x1B) & 0x8001:
            self.stopped = True
            raise InterruptedError("Esc 입력으로 예약 자동화를 중단했습니다.")

    def log(self, kind: str, **data):
        with (self.output / "events.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps({"at": datetime.now().isoformat(), "kind": kind, **data}, ensure_ascii=False) + "\n")
            file.flush()

    def windows(self):
        self.check()
        return [self.window(item[0]) for item in find_kakao_windows()]

    def window(self, handle: int):
        if not self.gui.IsWindow(handle) or not self.gui.IsWindowVisible(handle):
            raise FocusSafetyError("대상 카카오톡 창이 사라졌습니다.")
        _, pid = self.process.GetWindowThreadProcessId(handle)
        if not is_kakao_process_id(pid):
            raise FocusSafetyError("카카오톡 프로세스가 아닌 창입니다.")
        return NativeWindow(handle, self.gui.GetWindowText(handle), self.gui.GetClassName(handle), pid, tuple(self.gui.GetWindowRect(handle)))

    def unique(self, title: str):
        matches = [w for w in self.windows() if w.title == title]
        if len(matches) != 1:
            raise FocusSafetyError(f"'{title}' 창이 정확히 한 개가 아닙니다: {len(matches)}")
        return matches[0]

    def guard(self, window: NativeWindow, *, foreground=True):
        self.check()
        current = self.window(window.handle)
        if (current.title, current.pid, current.rect) != (window.title, window.pid, window.rect):
            raise FocusSafetyError("관측 후 창의 대상 또는 위치가 변경됐습니다.")
        if foreground and self.gui.GetForegroundWindow() != window.handle:
            foreground_handle=self.gui.GetForegroundWindow()
            self.log("focus_changed",expected=window.handle,actual=foreground_handle,actual_class=self.gui.GetClassName(foreground_handle),actual_pid=self.process.GetWindowThreadProcessId(foreground_handle)[1])
            raise FocusSafetyError("예약 자동화 대상 밖으로 포커스가 변경됐습니다.")
        return current

    def activate(self, window: NativeWindow):
        self.guard(window, foreground=False)
        if self.gui.GetForegroundWindow()!=window.handle:
            activate_window_handle(window.handle)
            time.sleep(.12)
        if self.gui.GetForegroundWindow()!=window.handle:
            # Windows gives the foreground input thread permission to activate
            # another window. Join its queue temporarily; no keystrokes are
            # sent to the unrelated foreground application.
            current=ctypes.windll.kernel32.GetCurrentThreadId()
            foreground=self.gui.GetForegroundWindow()
            owner=self.process.GetWindowThreadProcessId(foreground)[0]
            attached=False
            try:
                if current!=owner:
                    attached=bool(self.user.AttachThreadInput(current,owner,True))
                self.gui.BringWindowToTop(window.handle)
                self.gui.SetForegroundWindow(window.handle)
            except Exception as error:
                self.log("activation_error",error=str(error),target=asdict(window))
            finally:
                if attached:
                    self.user.AttachThreadInput(current,owner,False)
            if self.gui.GetForegroundWindow()!=window.handle:
                raise FocusSafetyError("카카오톡 창을 활성화하지 못했습니다.")
        time.sleep(.05)
        window=self.window(window.handle)
        self.guard(window)
        self.capture(window,"activate")
        return window

    def capture(self, window: NativeWindow, label: str):
        from PIL import ImageGrab
        self.guard(window, foreground=False)
        if self.gui.IsIconic(window.handle):
            raise FocusSafetyError("최소화된 창은 검증 화면으로 사용할 수 없습니다.")
        self.seq += 1
        path = self.output / f"{self.seq:05d}-{label}.png"
        # Physical screen pixels, unlike the downsampled CUA logical captures.
        image = ImageGrab.grab(bbox=window.rect, all_screens=True)
        if image.size != (window.width, window.height):
            raise FocusSafetyError("화면 캡처와 창 좌표계가 일치하지 않습니다.")
        image.save(path)
        self.log("capture", window=asdict(window), path=str(path))
        return path

    def click(self, window: NativeWindow, x: float, y: float, label: str, *, count=1, allow_owned_popup=False, before_input=None):
        self.guard(window, foreground=not allow_owned_popup)
        px, py = window.rect[0] + round(x), window.rect[1] + round(y)
        if not (window.rect[0] <= px < window.rect[2] and window.rect[1] <= py < window.rect[3]):
            raise FocusSafetyError("클릭 좌표가 관측된 창 밖입니다.")
        hit = self.gui.WindowFromPoint((px, py))
        hit_root = self.gui.GetAncestor(hit, 2)
        if hit_root != window.handle:
            raise FocusSafetyError("클릭 위치가 다른 창에 가려져 있습니다.")
        self.log("click", window=asdict(window), x=x, y=y, label=label, count=count)
        if before_input is not None:
            before_input()
        self.input.click(px, py, clicks=count, interval=.07 if count == 2 else 0)
        time.sleep(.14)

    def click_form(self, form: NativeWindow, x: float, y: float, label: str, *, before_input=None):
        self.assert_form_geometry(form)
        self.click(form, x * form.width / 370, y * form.height / 600, label, before_input=before_input)

    def assert_form_geometry(self, form: NativeWindow):
        if form.title != "예약 메시지" or not .60 < form.width / form.height < .64:
            raise FocusSafetyError("관측한 예약 양식과 창 비율이 다릅니다.")

    def wait_new(self, before: set[int], *, title: str | None = None, timeout=3.):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check()
            matches = [w for w in self.windows() if w.handle not in before and (title is None or w.title == title)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise FocusSafetyError("새 카카오톡 창이 여러 개여서 대상을 결정할 수 없습니다.")
            time.sleep(.05)
        raise FocusSafetyError(f"예상한 새 창이 열리지 않았습니다: {title}")

    def children(self, window: NativeWindow, class_name: str):
        found = []
        def each(handle, _):
            if self.gui.IsWindowVisible(handle) and self.gui.GetClassName(handle) == class_name:
                found.append(handle)
        self.gui.EnumChildWindows(window.handle, each, None)
        return found

    def search_edit(self, main: NativeWindow):
        """Resolve the observed main search control, never just any focused Edit."""
        self.guard(main)
        matches=[]
        for handle in self.children(main,"Edit"):
            left,top,right,bottom=self.gui.GetWindowRect(handle)
            if (self.gui.GetDlgCtrlID(handle)==100
                and self.gui.GetAncestor(handle,2)==main.handle
                and main.rect[0]<=left<right<=main.rect[2]
                and main.rect[1]<top<bottom<main.rect[1]+main.height*.25):
                matches.append(handle)
        if len(matches)>1:
            raise FocusSafetyError("검색 입력란이 여러 개여서 검색하지 않습니다.")
        return matches[0] if matches else None

    def assert_search_focus(self, main: NativeWindow, edit: int):
        from ctypes import wintypes
        class GuiThreadInfo(ctypes.Structure):
            _fields_=[("cbSize",wintypes.DWORD),("flags",wintypes.DWORD),
                      ("hwndActive",wintypes.HWND),("hwndFocus",wintypes.HWND),
                      ("hwndCapture",wintypes.HWND),("hwndMenuOwner",wintypes.HWND),
                      ("hwndMoveSize",wintypes.HWND),("hwndCaret",wintypes.HWND),
                      ("rcCaret",wintypes.RECT)]
        self.guard(main)
        info=GuiThreadInfo();info.cbSize=ctypes.sizeof(info)
        thread=self.process.GetWindowThreadProcessId(main.handle)[0]
        if not self.user.GetGUIThreadInfo(thread,ctypes.byref(info)) or info.hwndFocus!=edit:
            raise FocusSafetyError("카카오톡 검색 입력란의 실제 키보드 포커스를 확인하지 못했습니다.")
        if self.search_edit(main)!=edit:
            raise FocusSafetyError("검색 입력란이 관측 이후 바뀌었습니다.")

    def control_text(self, handle: int):
        size=self.gui.SendMessage(handle,0x000E,0,0)
        buffer=ctypes.create_unicode_buffer(size+1)
        self.user.SendMessageW.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_size_t,ctypes.c_void_p]
        self.user.SendMessageW.restype=ctypes.c_ssize_t
        self.user.SendMessageW(handle,0x000D,size+1,buffer)
        return buffer.value

    def body(self, form: NativeWindow):
        self.guard(form)
        edits = self.children(form, "RICHEDIT50W")
        if len(edits) != 1:
            raise FocusSafetyError("예약 본문 입력란을 유일하게 찾지 못했습니다.")
        size = self.gui.SendMessage(edits[0], 0x000E, 0, 0)  # WM_GETTEXTLENGTH
        buffer = ctypes.create_unicode_buffer(size + 1)
        self.user.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
        self.user.SendMessageW.restype = ctypes.c_ssize_t
        self.user.SendMessageW(edits[0], 0x000D, size + 1, buffer)
        return buffer.value

    def open_chat(self, name: str):
        existing = [w for w in self.windows() if w.title == name]
        if len(existing) == 1:
            return self.activate(existing[0])
        if existing:
            raise FocusSafetyError("동일한 대상 대화창이 여러 개 있습니다.")
        main = self.unique("카카오톡")
        main=self.activate(main)
        edit=self.search_edit(main)
        if edit is None:
            self.guard(main)
            self.input.hotkey("ctrl","f")
            time.sleep(.15)
            self.capture(main,"search-open")
            edit=self.search_edit(main)
        if edit is None:
            raise FocusSafetyError("카카오톡 메인 검색 입력란을 찾지 못했습니다.")
        left,top,right,bottom=self.gui.GetWindowRect(edit)
        self.click(main,(left+right)/2-main.rect[0],(top+bottom)/2-main.rect[1],"focus-search-edit")
        self.assert_search_focus(main,edit)
        # WM_SETTEXT updates this exact native search control. Ctrl+A is an
        # application shortcut (Add Friend) when focus slips, so never use it.
        value=ctypes.create_unicode_buffer(name)
        self.user.SendMessageW.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_size_t,ctypes.c_void_p]
        self.user.SendMessageW.restype=ctypes.c_ssize_t
        self.log("set_search",handle=edit,chat_name=name)
        self.user.SendMessageW(edit,0x000C,0,value)
        if self.control_text(edit)!=name:
            raise FocusSafetyError("검색 입력값이 대상 대화명과 다릅니다.")
        time.sleep(.3)
        self.capture(main, "search-result")
        self.assert_search_focus(main,edit)
        self.input.press("enter")
        time.sleep(.35)
        chat = self.unique(name)
        self.guard(chat)
        self.capture(chat, "chat-open")
        return chat

    def open_form(self, chat: NativeWindow):
        self.guard(chat)
        if any(w.title == "예약 메시지" for w in self.windows()):
            raise FocusSafetyError("기존 예약 양식이 열려 있어 새 예약을 시작하지 않습니다.")
        menus = [w for w in self.windows() if w.class_name == "EVA_Menu"]
        if len(menus) > 1:
            raise FocusSafetyError("메뉴가 여러 개 열려 있습니다.")
        if menus:
            menu = menus[0]
            if self.gui.GetWindow(menu.handle, 4) != chat.handle:
                raise FocusSafetyError("기존 메뉴가 대상 대화창에 속하지 않습니다.")
        else:
            before = {w.handle for w in self.windows()}
            dpi = self.user.GetDpiForWindow(chat.handle) / 96
            self.click(chat, 22 * dpi, chat.height - 25 * dpi, "chat-plus")
            menu = self.wait_new(before)
        image = self.capture(menu, "plus-menu")
        ocr = recognize_image(image, scale=2)
        self.log("ocr", label="plus-menu", text=ocr.text)
        matches = [line for line in ocr.lines if "메시지예약" == "".join(
            word.text.replace(" ", "") for word in line.words if word.rect.x >= menu.width * .22
        )]
        if len(matches) != 1:
            raise FocusSafetyError("메시지 예약 메뉴를 화면에서 확인하지 못했습니다.")
        before = {w.handle for w in self.windows()}
        self.click(menu, *matches[0].rect.center, "open-form", allow_owned_popup=True)
        form = self.wait_new(before, title="예약 메시지")
        self.created_forms.add(form.handle)
        self.capture(form, "form-empty")
        self.assert_form_geometry(form)
        return form

    def fill_body(self, form: NativeWindow, text: str):
        current = self.body(form)
        if current not in ("", "예약 메시지를 입력해 주세요."):
            raise FocusSafetyError("새 예약창의 본문이 비어 있지 않습니다.")
        edit = self.children(form, "RICHEDIT50W")[0]
        rect = self.gui.GetWindowRect(edit)
        self.click(form, (rect[0] + rect[2]) / 2 - form.rect[0], (rect[1] + rect[3]) / 2 - form.rect[1], "body-focus")
        set_clipboard(text)
        self.guard(form)
        self.input.hotkey("ctrl", "v")
        time.sleep(.15)
        image = self.capture(form, "body-filled")
        actual = self.body(form).replace("\r\n", "\n").replace("\r", "\n")
        if actual != text:
            raise FocusSafetyError("입력된 본문이 CSV 원문과 다릅니다.")
        self.log("body_verified", sha256=hashlib.sha256(text.encode()).hexdigest(), path=str(image))
        return image

    def capture_form(self, form: NativeWindow, label: str):
        # A completion popup from another app may appear between filling and
        # reading. Raise the enabled form before observing its fields. An
        # already-open owned cancel prompt leaves its parent disabled;
        # dismiss_matching_draft restores the editable form before reading.
        if self.gui.IsWindowEnabled(form.handle):
            self.gui.BringWindowToTop(form.handle)
            self.guard(form)
        path = self.capture(form, label)
        self.assert_form_geometry(form)
        return path

    @staticmethod
    def form_field_box(form: NativeWindow, field: str):
        coordinates = {
            'recipient': (125,770,709,855),
            'date': (144,850,478,950),
            'time': (480,850,714,950),
        }[field]
        return tuple(round(n * (form.width/740 if i % 2 == 0 else form.height/1200))
                     for i,n in enumerate(coordinates))

    def read_form_recipient(self, form: NativeWindow, label='draft-identity', *, expected_names=None):
        """Identify a draft without requiring readable or valid date/time fields."""
        path = self.capture_form(form, label)
        result = self.read_recipient(path, self.form_field_box(form, 'recipient'), expected_names=expected_names)
        self.log('draft_recipient_readback', text=result.text, path=str(path))
        return result, path

    def read_form(self, form: NativeWindow, label="form-readback", *, expected_names=None):
        path = self.capture_form(form, label)
        lines = []
        for name in ('recipient', 'date', 'time'):
            box = self.form_field_box(form, name)
            if name=="recipient":
                crop=self.read_recipient(path,box,expected_names=expected_names)
            elif name=="date":
                crop=self.read_date(path,box)
            else:
                crop=self.crop_ocr(path,box,name)
            def shifted(rect):
                return OcrRect(rect.x+box[0], rect.y+box[1], rect.width, rect.height)
            lines.extend(OcrLine(line.text, tuple(OcrWord(word.text, shifted(word.rect)) for word in line.words), shifted(line.rect)) for line in crop.lines)
        result = OcrResult(form.width, form.height, "ko", tuple(lines))
        self.log("ocr", label=label, text=result.text, path=str(path))
        return read_schedule_form(result), result, path

    def read_date(self,path: Path,box):
        # Enlarging a small Korean weekday can turn 금 into '='. Retry the
        # original pixels, while retaining every date digit and the weekday.
        for scale in (2,1):
            result=self.crop_ocr(path,box,f'date-s{scale}',scale=scale)
            if re.fullmatch(r'[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}\.?\([월화수목금토일]\)', ''.join(result.text.split())):
                return result
        raise ScheduleReadbackError('예약 날짜의 전체 숫자와 요일을 읽지 못했습니다.')

    def read_recipient(self, path: Path, box, *, expected_names=None):
        # Compare full observed labels. Expected names only reject OCR results;
        # they never supply or replace characters in a recognized label.
        votes={}
        allowed={"".join(unicodedata.normalize("NFC",value).split()) for value in expected_names} if expected_names else None
        saved=self.read_recipient_sample(path,box,allowed=allowed)
        if saved is not None:
            return saved
        for scale in (1,2,3):
            result=self.crop_ocr(path,box,f"recipient-s{scale}",scale=scale)
            value="".join(unicodedata.normalize("NFC",result.text).split())
            if value:
                votes[value]=votes.get(value,0)+1
                if votes[value]>=2:
                    if allowed is None or value in allowed:
                        return result
                    break
        if allowed is not None:
            # Recover grey anti-aliasing failures using only the observed
            # glyph pixels. At least two different thresholds must return the
            # full exact label; no character replacement is used.
            ink_votes={}
            for threshold in (100,140,180):
                result=self.clean_recipient_ocr(path,box,threshold)
                name="".join(unicodedata.normalize("NFC",result.text).split())
                ink_votes[name]=ink_votes.get(name,0)+1
                if name in allowed and ink_votes[name]>=2:
                    return result
            # Korean name recognition can favour a dictionary word (희 -> 회)
            # even at every scale. Change only blank columns between observed
            # glyphs; retain their pixels and require two exact readbacks.
            spaced_votes={}
            for threshold,gap_ratio in ((100,.15),(140,.23)):
                result=self.spaced_recipient_ocr(path,box,threshold,gap_ratio=gap_ratio)
                name="".join(unicodedata.normalize("NFC",result.text).split())
                spaced_votes[name]=spaced_votes.get(name,0)+1
                if name in allowed and spaced_votes[name]>=2:
                    return result
        if allowed is not None:
            raise ScheduleReadbackError("대화명 OCR이 CSV 대상과 두 번 이상 정확히 일치하지 않습니다.")
        raise ScheduleReadbackError("대화명 OCR의 두 배율 이상 결과가 일치하지 않습니다.")

    def read_recipient_sample(self,path: Path,box,*,allowed):
        """Reuse an inspected label only when its complete pixels are identical.

        Samples are explicit visual calibration data, not fuzzy OCR aliases.
        Missing samples use OCR; damaged calibration never supplies a label.
        """
        from PIL import Image,ImageOps
        manifest=Path(__file__).with_name('native_schedule_recipient_samples.json')
        if not manifest.is_file() or not path.is_file():
            return None
        with Image.open(path) as image:
            crop=image.crop(box).convert('RGB')
        digest=hashlib.sha256(crop.tobytes()).hexdigest()
        try:
            samples=json.loads(manifest.read_text(encoding='utf-8'))
            if samples['version']!=1 or not isinstance(samples['samples'],list):
                raise ValueError
            matches=[sample for sample in samples['samples']
                     if sample['pixel_sha256']==digest and sample['size']==list(crop.size)]
            if not matches:
                return None
            if len(matches)!=1:
                raise ValueError
            sample=matches[0]
            label=sample['observed_label']
            if not isinstance(label,str) or not label.strip():
                raise ValueError
            source=manifest.parent/sample['image']
            with Image.open(source) as reference:
                reference=reference.convert('RGB')
                if reference.size!=crop.size or reference.tobytes()!=crop.tobytes():
                    raise ValueError
            compact=''.join(unicodedata.normalize('NFC',label).split())
            if allowed is not None and compact not in allowed:
                raise ScheduleReadbackError('검증된 이름 이미지가 CSV 대상과 다릅니다.')
        except (KeyError,TypeError,ValueError,OSError) as error:
            raise ScheduleReadbackError('대화명 이미지 검증 자료가 일치하지 않습니다.') from error
        mask=crop.convert('L').point(lambda value:0 if value<180 else 255)
        bounds=ImageOps.invert(mask).getbbox()
        if bounds is None:
            raise ScheduleReadbackError('검증된 대화명 이미지에 문자 픽셀이 없습니다.')
        rect=OcrRect(bounds[0],bounds[1],bounds[2]-bounds[0],bounds[3]-bounds[1])
        self.log('recipient_sample_match',text=label,pixel_sha256=digest,source=str(source),source_box=box)
        return OcrResult(crop.width,crop.height,'ko',(OcrLine(label,(OcrWord(label,rect),),rect),))

    def spaced_recipient_ocr(self,path: Path,box,threshold: int,*,gap_ratio=.15):
        from PIL import Image,ImageOps
        with Image.open(path) as image:
            original=image.crop(box).convert('L')
        mask=original.point(lambda value:0 if value<threshold else 255)
        bounds=ImageOps.invert(mask).getbbox()
        if bounds is None:
            raise ScheduleReadbackError("대화명 문자 픽셀을 찾지 못했습니다.")
        height=bounds[3]-bounds[1]
        columns=[x for x in range(bounds[0],bounds[2])
                 if any(mask.getpixel((x,y))==0 for y in range(bounds[1],bounds[3]))]
        groups=[]
        join_gap=max(2,round(height*.12))
        for x in columns:
            if not groups or x>groups[-1][-1]+join_gap+1:
                groups.append([x])
            else:
                groups[-1].append(x)
        padding=20
        gap=max(4,round(height*gap_ratio))
        width=sum(group[-1]-group[0]+1 for group in groups)+gap*(len(groups)-1)
        spaced=Image.new('L',(width+padding*2,height+padding*2),255)
        segments=[]
        x=padding
        for group in groups:
            left,right=group[0],group[-1]+1
            spaced.paste(original.crop((left,bounds[1],right,bounds[3])),(x,padding))
            segments.append((x,x+right-left,left,right))
            x+=right-left+gap
        saved=path.with_name(path.stem+f'-recipient-spaced-{threshold}.png')
        spaced.save(saved)
        result=recognize_image(saved,scale=1)
        self.log('recipient_spaced_ocr',threshold=threshold,text=result.text,path=str(saved),
                 source_box=box,ink_box=bounds,gap=gap,segments=segments)
        def restore(rect):
            hits=[(max(rect.x,left),min(rect.right,right),left,source)
                  for left,right,source,_ in segments if rect.right>left and rect.x<right]
            if not hits:
                raise ScheduleReadbackError("대화명 OCR 좌표가 문자 픽셀과 겹치지 않습니다.")
            left=min(start-offset+source for start,_,offset,source in hits)
            right=max(end-offset+source for _,end,offset,source in hits)
            return OcrRect(left,rect.y-padding+bounds[1],right-left,rect.height)
        return OcrResult(original.width,original.height,result.language,tuple(
            OcrLine(line.text,tuple(OcrWord(word.text,restore(word.rect)) for word in line.words),restore(line.rect))
            for line in result.lines))

    def clean_recipient_ocr(self,path: Path,box,threshold: int):
        from PIL import Image,ImageOps
        with Image.open(path) as image:
            original=image.crop(box).convert('L')
        binary=original.point(lambda value:0 if value<threshold else 255)
        bounds=ImageOps.invert(binary).getbbox()
        if bounds is None:
            raise ScheduleReadbackError("대화명 문자 픽셀을 찾지 못했습니다.")
        padded=ImageOps.expand(binary.crop(bounds),border=20,fill=255)
        saved=path.with_name(path.stem+f'-recipient-ink-{threshold}.png')
        padded.save(saved)
        result=recognize_image(saved,scale=1)
        self.log('recipient_ink_ocr',threshold=threshold,text=result.text,path=str(saved),source_box=box,ink_box=bounds)
        def restore(rect):
            return OcrRect(rect.x-20+bounds[0],rect.y-20+bounds[1],rect.width,rect.height)
        return OcrResult(original.width,original.height,result.language,tuple(
            OcrLine(line.text,tuple(OcrWord(word.text,restore(word.rect)) for word in line.words),restore(line.rect)) for line in result.lines))

    def crop_ocr(self, path: Path, box, label: str, *, scale=2):
        from PIL import Image
        cropped = path.with_name(path.stem + "-" + label + ".png")
        with Image.open(path) as image:
            image.crop(tuple(round(v) for v in box)).save(cropped)
        result = recognize_image(cropped, scale=scale)
        self.log("ocr_crop", label=label, box=box, text=result.text, path=str(cropped))
        return result

    def set_date(self, form: NativeWindow, target):
        before = {w.handle for w in self.windows()}
        self.click_form(form, 155, 450, "date-field")
        picker = self.wait_new(before)
        if not .84 < picker.width / picker.height < .94:
            raise FocusSafetyError("예상한 달력 창의 비율과 다릅니다.")
        for _ in range(13):
            path = self.capture(picker, "calendar")
            header = self.crop_ocr(path, (picker.width*.22, picker.height*.055, picker.width*.8, picker.height*.22), "header")
            year, month = read_calendar_month(header)
            delta = (target.year-year)*12 + target.month-month
            if delta == 0:
                break
            self.click(picker, (202 if delta > 0 else 33)*picker.width/235, 36*picker.height/265, "calendar-month", allow_owned_popup=True)
        else:
            raise FocusSafetyError("달력에서 목표 월을 확인하지 못했습니다.")
        weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(target.year, target.month)
        row = next(i for i, days in enumerate(weeks) if target.day in days)
        col = weeks[row].index(target.day)
        self.click(picker, (34+28*col)*picker.width/235, (99+28*row)*picker.height/265, "calendar-day", allow_owned_popup=True)
        self.capture(form, "date-selected")

    def dropdown(self, form: NativeWindow, field: str, target: str | int):
        xpos = {"ampm": 267, "hour": 289, "minute": 308}[field]
        before = {w.handle for w in self.windows()}
        self.click_form(form, xpos, 450, field + "-field")
        popup = self.wait_new(before)
        point = None
        for attempt in range(5):
            path = self.capture(popup, field + "-options")
            ocr = recognize_image(path, scale=2, language="en-US" if field == "hour" else "ko")
            self.log("ocr", label=field+"-options", text=ocr.text, path=str(path))
            try:
                if field == 'ampm':
                    point = find_dropdown_option_center(ocr, target)
                else:
                    from kakao_dropdown_ocr import find_complete_numeric_option_center
                    point = find_complete_numeric_option_center(path, ocr, target)
                break
            except ScheduleReadbackError:
                if field == "ampm":
                    # A narrow 오전/오후 menu can lose the lower label at 2x.
                    # Retry the same saved pixels at 3x; no inferred row index.
                    ocr=recognize_image(path,scale=3,language='ko')
                    self.log('ampm_menu_readback',text=ocr.text,path=str(path),scale=3)
                    point=find_dropdown_option_center(ocr,target)
                    break
                from kakao_dropdown_ocr import recognize_numeric_dropdown_image
                ocr=recognize_numeric_dropdown_image(path)
                self.log("numeric_menu_readback",label=field,text=ocr.text,path=str(path))
                try:
                    point=find_dropdown_option_center(ocr,target)
                    break
                except ScheduleReadbackError:
                    pass
                # Numeric direction derives from the actual menu labels.
                values = [int(line.text.strip()) for line in ocr.lines if line.text.strip().isascii() and line.text.strip().isdigit()]
                if field=='hour':
                    if any(not 1<=value<=12 for value in values):
                        raise FocusSafetyError("시각 메뉴에 올바르지 않은 숫자가 있습니다.")
                    # The observed native menu starts with 12, then 1..11.
                    # Comparing numeric min/max mistakes 11 for a visible
                    # item when the current viewport contains 12, 1..9.
                    ranks=[value%12 for value in values]
                    target_rank=int(target)%12
                else:
                    ranks=values
                    target_rank=int(target)
                if not ranks or min(ranks) <= target_rank <= max(ranks):
                    raise FocusSafetyError("목표 드롭다운 항목을 정확히 판독하지 못했습니다.")
                direction = 4 if target_rank < min(ranks) else -3
                self.guard(popup, foreground=False)
                cx, cy = popup.rect[0]+popup.width*.5, popup.rect[1]+popup.height*.4
                if self.gui.GetAncestor(self.gui.WindowFromPoint((round(cx),round(cy))), 2) != popup.handle:
                    raise FocusSafetyError("드롭다운이 다른 창에 가려져 있습니다.")
                self.log("scroll", window=asdict(popup), ticks=direction)
                self.input.moveTo(cx,cy)
                # Windows wheel data is in WHEEL_DELTA (120) units. The
                # installed PyAutoGUI Windows backend passes raw data through.
                self.user.mouse_event(0x0800, 0, 0, direction * 120, 0)
                time.sleep(.12)
        if point is None:
            raise FocusSafetyError("드롭다운 선택을 완료하지 못했습니다.")
        self.click(popup, *point, field + "-select", allow_owned_popup=True)
        self.capture(form, field + "-selected")

    def prepare(self, row: MessageRow, *, resume_form=False):
        validate_schedule_row(row, row.scheduled_at)
        forms = [w for w in self.windows() if w.title == "예약 메시지"]
        if resume_form and len(forms) == 1:
            form = forms[0]
            chat=self.unique(row.chat_name)
            if self.gui.IsIconic(chat.handle):
                self.gui.ShowWindow(chat.handle,9)
                time.sleep(.12)
                chat=self.window(chat.handle)
            form=self.activate(self.window(form.handle))
            menus=[w for w in self.windows() if w.class_name == "EVA_Menu"]
            if menus:
                if len(menus) != 1 or self.gui.GetWindow(menus[0].handle,4) != form.handle:
                    raise FocusSafetyError("열린 팝업이 예약 양식에 속하지 않습니다.")
                self.click_form(form,180,45,"dismiss-owned-dropdown")
                self.capture(form,"dropdown-dismissed")
            observed, _, _ = self.read_form(form, "resume-form",expected_names={row.chat_name})
            if "".join(observed.chat_name.split()) != "".join(row.chat_name.split()):
                raise FocusSafetyError("열린 예약창의 대상이 다음 CSV 행과 다릅니다.")
            if self.body(form).replace("\r\n", "\n").replace("\r", "\n") != row.text:
                raise FocusSafetyError("열린 예약창의 본문이 다음 CSV 행과 다릅니다.")
            chat = self.unique(row.chat_name)
        else:
            chat = self.open_chat(row.chat_name)
            form = self.open_form(chat)
            self.fill_body(form, row.text)
            observed, _, _ = self.read_form(form,expected_names={row.chat_name})
        if "".join(observed.chat_name.split()) != "".join(row.chat_name.split()):
            raise FocusSafetyError("예약 양식의 대상이 CSV와 다릅니다.")
        target = row.scheduled_at
        if observed.scheduled_at.date() != target.date():
            self.set_date(form, target.date())
        if (observed.scheduled_at.hour >= 12) != (target.hour >= 12):
            self.dropdown(form, "ampm", "오후" if target.hour >= 12 else "오전")
        if observed.scheduled_at.hour % 12 != target.hour % 12:
            self.dropdown(form, "hour", target.hour % 12 or 12)
        if observed.scheduled_at.minute != target.minute:
            self.dropdown(form, "minute", target.minute)
        _, ocr, path = self.read_form(form, "ready",expected_names={row.chat_name})
        verify_schedule_form(ocr, chat_name=row.chat_name, scheduled_at=target, expected_body=row.text, actual_body=self.body(form))
        self.assert_submit_button(path, form)
        self.log("form_verified", chat_name=row.chat_name, scheduled_at=target.isoformat(), body_sha256=hashlib.sha256(row.text.encode()).hexdigest(), path=str(path))
        self.last_verification={"observed_chat_name":read_schedule_form(ocr).chat_name,"observed_scheduled_at":read_schedule_form(ocr).scheduled_at.isoformat(),"expected_chat_name":row.chat_name,"expected_scheduled_at":target.isoformat(),"body_sha256":hashlib.sha256(row.text.encode()).hexdigest(),"form_screenshot":str(path),"form_rect":form.rect,"form_handle":form.handle}
        return chat, form, path

    @staticmethod
    def assert_submit_button(path: Path, form: NativeWindow):
        from PIL import Image
        with Image.open(path).convert("RGB") as image:
            area = image.crop((round(form.width*.38), round(form.height*.91), round(form.width*.40), round(form.height*.95)))
            pixels = list(area.getdata())
            if not pixels or sum(r>220 and g>170 and b<100 for r,g,b in pixels)/len(pixels) < .85:
                raise FocusSafetyError("예약 버튼이 활성화된 새 양식인지 확인하지 못했습니다.")

    def submit(self, row: MessageRow, chat: NativeWindow, form: NativeWindow, path: Path, journal):
        self.guard(form)
        # An unrelated Kakao popup may cover the button while this form still
        # has keyboard focus. Raise the validated form, never click through it.
        self.gui.BringWindowToTop(form.handle)
        self.guard(form)
        validate_schedule_row(row, row.scheduled_at)
        if getattr(self,"last_verification",{}).get("form_screenshot") != str(path):
            raise FocusSafetyError("이 실행에서 확인한 예약 양식이 아닙니다.")
        before_chat=self.capture(chat,"before-submit-chat")
        try:
            prior_toast=self.find_toast_image(before_chat)
        except FocusSafetyError:
            prior_toast=None
        if prior_toast and has_schedule_success_toast(self.read_submission_toast(prior_toast, timeout=3.)[0]):
            raise FocusSafetyError("이전 접수 토스트가 남아 있어 새 접수 여부를 구분할 수 없습니다.")
        # Recheck native body immediately at the persisted dispatch boundary.
        if self.body(form).replace("\r\n", "\n").replace("\r", "\n") != row.text:
            raise FocusSafetyError("검증 후 예약 본문이 변경됐습니다.")
        from PIL import Image, ImageChops
        final = self.capture(form, "dispatch-boundary")
        with Image.open(path).convert("RGB") as earlier, Image.open(final).convert("RGB") as current:
            box=(round(form.width*.17),round(form.height*.65),round(form.width*.96),round(form.height*.79))
            if ImageChops.difference(earlier.crop(box),current.crop(box)).getbbox():
                raise FocusSafetyError("판독 후 대상·일시 필드 화면이 변경됐습니다.")
        committed = False
        self.last_submission_observation = {}
        def commit_submit():
            nonlocal committed
            journal.mark_submitting(row, evidence={**self.last_verification,
                "before_chat_screenshot":str(before_chat),"body_exact":True,"date_time_exact":True,
                "click_guard_passed":True,"submission_boundary":"guarded_before_native_click"})
            committed = True
        try:
            self.click_form(form, 180, 562, "submit", before_input=commit_submit)
            close_started = time.monotonic()
            deadline = close_started+10
            while self.gui.IsWindow(form.handle) and self.gui.IsWindowVisible(form.handle) and time.monotonic()<deadline:
                self.check()
                time.sleep(.05)
            if self.gui.IsWindow(form.handle) and self.gui.IsWindowVisible(form.handle):
                self.last_submission_observation['form_close_wait_seconds'] = round(time.monotonic()-close_started,3)
                try:
                    held = self.capture(form,'submission-form-still-visible')
                    self.last_submission_observation['held_form_screenshot'] = str(held)
                except Exception as error:
                    self.last_submission_observation['held_form_capture_error'] = str(error)
                raise FocusSafetyError("제출 후 양식이 남아 있어 등록 결과가 불명확합니다.")
            close_wait = round(time.monotonic()-close_started,3)
            self.log('submission_form_closed',wait_seconds=close_wait,handle=form.handle)
            image, toast, ocr = self.wait_submission_toast(chat)
            self.last_submission_observation['form_close_wait_seconds'] = close_wait
            journal.mark_acknowledged(row, evidence={**self.last_submission_observation,
                "form_screenshot":str(path),"toast_screenshot":str(toast),"toast":ocr.text})
            self.log("acknowledged", chat_name=row.chat_name,scheduled_at=row.scheduled_at.isoformat(),path=str(toast))
            return image
        except BaseException as exc:
            if committed and journal.get(row).state not in ('acknowledged', 'verified'):
                journal.mark_unknown(row, evidence={**self.last_submission_observation,
                    "error":str(exc),"submission_boundary":"committed"})
            raise

    def wait_submission_toast(self, chat: NativeWindow, *, timeout=5.):
        """Observe delayed receipt animation without issuing another input.

        Closing the compose window precedes the server's toast on some runs.
        Keep saving fresh frames until an exact receipt appears or time runs
        out. A dialog closing or the passage of time never proves acceptance.
        """
        started = time.monotonic()
        deadline = started + timeout
        frames = 0
        read_crops = set()
        last_error = "접수 완료 알림이 아직 나타나지 않았습니다."
        self.last_submission_observation = {}
        while time.monotonic() < deadline:
            self.check()
            image = self.capture(chat, "submitted-chat")
            frames += 1
            self.last_submission_observation.update(
                submitted_chat_screenshot=str(image), receipt_wait_frames=frames,
                receipt_wait_seconds=round(time.monotonic()-started, 3))
            try:
                toast = self.find_toast_image(image)
            except FocusSafetyError as error:
                last_error = str(error)
            else:
                self.last_submission_observation['toast_screenshot'] = str(toast)
                digest = hashlib.sha256(toast.read_bytes()).hexdigest()
                remaining = deadline - time.monotonic()
                if digest not in read_crops and remaining > 0:
                    try:
                        result, readback_path = self.read_submission_toast(toast, timeout=remaining)
                    except WindowsOcrError as error:
                        last_error = str(error)
                        self.log("submission_toast_ocr_error", error=last_error, path=str(toast))
                    else:
                        read_crops.add(digest)
                        self.check()
                        elapsed = round(time.monotonic()-started, 3)
                        self.last_submission_observation['receipt_wait_seconds'] = elapsed
                        self.last_submission_observation['toast_readback_screenshot'] = str(readback_path)
                        self.log("submission_toast_ocr", text=result.text, path=str(readback_path),
                                 frames=frames, elapsed=elapsed)
                        if has_schedule_success_toast(result):
                            self.last_submission_observation.pop('last_receipt_error', None)
                            return image, toast, result
                        last_error = "접수 완료 토스트의 정확한 문구를 확인하지 못했습니다."
            self.last_submission_observation['last_receipt_error'] = last_error
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(.12, remaining))
        self.last_submission_observation['receipt_wait_seconds'] = round(time.monotonic()-started, 3)
        self.log("submission_toast_timeout", **self.last_submission_observation)
        raise FocusSafetyError(f"등록 입력 후 {timeout:g}초 동안 접수 완료 알림을 확인하지 못했습니다. ({last_error})")

    def read_submission_toast(self, path: Path, *, timeout=5.):
        """Read original pixels, then isolate observed light toast lettering.

        A translucent toast lets dark chat text show through. Preserve only
        its bright glyph pixels for the second reading; never supply
        an expected letter or accept approximate wording.
        """
        started = time.monotonic()
        result = recognize_image(path, scale=2, timeout=timeout)
        if has_schedule_success_toast(result):
            return result, path
        self.log('toast_original_readback', text=result.text, path=str(path))
        remaining = timeout - (time.monotonic()-started)
        if remaining <= 0:
            return result, path
        from PIL import Image
        from statistics import median
        with Image.open(path).convert('RGB') as source:
            # The old crop includes chat pixels beside the rounded toast.
            # Recover its actual horizontal edges from long black margins.
            segments = []
            for y in range(source.height):
                start = None
                for x in range(source.width+1):
                    dark = x < source.width and max(source.getpixel((x, y))) < 65
                    if dark and start is None:
                        start = x
                    if not dark and start is not None:
                        if x-start > min(source.width*.62,160):
                            segments.append((start, x))
                        start = None
            if not segments:
                return result, path
            left = int(median(start for start, end in segments))
            right = int(median(end for start, end in segments))
            source = source.crop((left, 0, right, source.height))
            cleaned = Image.new('RGB', source.size, 'white')
            pixels, ink = source.load(), cleaned.load()
            for y in range(source.height):
                for x in range(source.width):
                    rgb = pixels[x, y]
                    # Over a yellow bubble the same light letters are tinted
                    # yellow. Their red/green contrast still separates them
                    # from the dark toast background and underlying text.
                    if rgb[0] > 160 and rgb[1] > 160:
                        ink[x, y] = (0, 0, 0)
            clean_path = path.with_name(path.stem+'-light-ink.png')
            cleaned.save(clean_path)
        remaining = timeout - (time.monotonic()-started)
        if remaining <= 0:
            return result, path
        cleaned_result = recognize_image(clean_path, scale=2, timeout=remaining)
        self.log('toast_light_ink_readback', text=cleaned_result.text, path=str(clean_path), source=str(path), bounds=[left, right])
        return cleaned_result, clean_path

    @staticmethod
    def find_toast_image(path: Path):
        """Locate the black rounded toast near the chat center, not chat text."""
        from PIL import Image
        with Image.open(path).convert("RGB") as image:
            w,h=image.size
            # Native toast observed centered horizontally and vertically.
            box=(round(w*.20),round(h*.38),round(w*.80),round(h*.62))
            region=image.crop(box)
            # Toast lettering stays the same size when a chat is widened.
            # Require a continuous dark background, not summed black glyphs;
            # cap width/height thresholds so a wider/taller chat still works.
            runs=[]
            minimum_width=min(region.width*.62,160)
            for y in range(region.height):
                longest=active=0
                for x in range(region.width):
                    if max(region.getpixel((x,y)))<65:
                        active+=1
                        longest=max(longest,active)
                    else:
                        active=0
                if longest>minimum_width:
                    runs.append(y)
            if not runs or max(runs)-min(runs)<max(12,min(h*.035,30)):
                raise FocusSafetyError("새 접수 토스트의 검은 배경을 찾지 못했습니다.")
            top,bottom=min(runs),max(runs)+1
            if bottom-top>h*.15:
                raise FocusSafetyError("접수 토스트 영역이 예상 크기와 다릅니다.")
            toast=path.with_name(path.stem+"-toast.png")
            image.crop((box[0],box[1]+top,box[2],box[1]+bottom)).save(toast)
            return toast

    def close_window(self, window: NativeWindow):
        self.guard(window)
        dpi=self.user.GetDpiForWindow(window.handle)/96
        self.click(window,window.width-15*dpi,13*dpi,"close-window")

    def close_registered_chat(self, chat: NativeWindow, *, expected_chat_name: str):
        """Close only the completed row's chat with one guarded Esc press."""
        if chat.title != expected_chat_name or chat.title in ('카카오톡', '예약 메시지', '브리핑 보드'):
            raise FocusSafetyError('접수가 확인된 대상 대화창만 닫을 수 있습니다.')
        self.check()
        if not self.gui.IsWindow(chat.handle) or not self.gui.IsWindowVisible(chat.handle):
            self.log('registered_chat_already_closed', window=asdict(chat))
            return
        self.guard(chat, foreground=False)
        self.activate(chat)
        self.guard(chat)
        self.log('close_registered_chat_esc', window=asdict(chat))
        self.input.press('esc')
        # Consume this injected key's one-shot latch. A held physical Esc,
        # later physical presses and the cancel event still stop the run.
        if self.user.GetAsyncKeyState(0x1B) & 0x8000:
            self.stopped = True
            raise InterruptedError('Esc 키가 눌린 상태여서 예약 자동화를 중단했습니다.')
        deadline = time.monotonic() + 2
        while self.gui.IsWindow(chat.handle) and self.gui.IsWindowVisible(chat.handle):
            self.check()
            if time.monotonic() >= deadline:
                raise FocusSafetyError('예약 접수 후 Esc로 대상 대화창이 닫힌 것을 확인하지 못했습니다.')
            time.sleep(.05)
        self.check()
        self.log('registered_chat_closed', window=asdict(chat))

    def discard_owned_draft(self):
        """Close only a form opened by this driver and still in its foreground."""
        for window in self.windows():
            if window.title == '예약 메시지' and window.handle in self.created_forms:
                try:
                    self.close_draft(window)
                except FocusSafetyError as error:
                    self.log('draft_cleanup_skipped', error=str(error))

    def respond_to_draft_cancel_prompt(self, form, dialog, button_text):
        """Read the owned draft-cancel question before choosing either button."""
        if button_text not in ('확인', '취소'):
            raise ValueError('지원하지 않는 작성 취소 동작입니다.')
        if self.gui.GetWindow(dialog.handle, 4) != form.handle:
            raise FocusSafetyError('작성 취소 확인창이 예약 초안에 속하지 않습니다.')
        dialog = self.activate(dialog)
        path = self.capture(dialog, 'cancel-draft-prompt')
        observed = recognize_image(path, scale=2)
        self.log('cancel_draft_ocr', text=observed.text, path=str(path))
        if '작성을취소하겠습니까?' not in ''.join(observed.text.split()):
            raise FocusSafetyError('예약 초안의 작성 취소 안내를 읽지 못했습니다.')
        buttons = [word for line in observed.lines for word in line.words
                   if word.text.strip() == button_text and word.rect.y > dialog.height * .55]
        if len(buttons) != 1:
            raise FocusSafetyError(f'작성 취소 {button_text} 버튼을 유일하게 찾지 못했습니다.')
        action = 'confirm-cancel-draft' if button_text == '확인' else 'keep-draft-for-readback'
        self.click(dialog, *buttons[0].rect.center, action)

    def close_draft(self, form):
        """Dismiss an owned, unsubmitted compose form and its exact cancel prompt."""
        if form.handle not in self.created_forms:
            raise FocusSafetyError('이번 실행에서 확인한 초안만 닫을 수 있습니다.')
        windows=self.windows()
        existing=[window for window in windows if window.handle!=form.handle
                  and window.title=='' and window.class_name=='EVA_Window_Dblclk'
                  and self.gui.GetWindow(window.handle,4)==form.handle]
        if len(existing)>1:
            raise FocusSafetyError('예약 초안에 확인창이 여러 개 열려 있습니다.')
        dialog=existing[0] if existing else None
        if dialog is None:
            before = {window.handle for window in windows}
            self.close_window(form)
            if self.gui.IsWindow(form.handle) and self.gui.IsWindowVisible(form.handle):
                dialog = self.wait_new(before)
        if dialog is not None:
            self.respond_to_draft_cancel_prompt(form, dialog, '확인')
        deadline = time.monotonic() + 2
        while self.gui.IsWindow(form.handle) and self.gui.IsWindowVisible(form.handle):
            self.check()
            if time.monotonic() > deadline:
                raise FocusSafetyError('예약 초안이 닫힌 것을 확인하지 못했습니다.')
            time.sleep(.05)
        self.created_forms.discard(form.handle)
        self.log('draft_closed', handle=form.handle)

    def dismiss_matching_draft(self, rows):
        forms = [window for window in self.windows() if window.title == '예약 메시지']
        if not forms:
            return
        if len(forms) != 1:
            raise FocusSafetyError('예약창이 여러 개 열려 있어 시작하지 않습니다.')
        form=forms[0]
        owned_prompts=[window for window in self.windows() if window.handle!=form.handle
                       and window.title=='' and window.class_name=='EVA_Window_Dblclk'
                       and self.gui.GetWindow(window.handle,4)==form.handle]
        if len(owned_prompts)>1:
            raise FocusSafetyError('예약 초안에 확인창이 여러 개 열려 있습니다.')
        if owned_prompts:
            # Cancelling this question keeps the draft. The disabled parent
            # cannot be raised or read reliably while another app covers it.
            # Restore it first, then verify CSV identity before discarding.
            dialog=owned_prompts[0]
            self.respond_to_draft_cancel_prompt(form, dialog, '취소')
            deadline=time.monotonic()+2
            while ((self.gui.IsWindow(dialog.handle) and self.gui.IsWindowVisible(dialog.handle))
                   or not self.gui.IsWindowEnabled(form.handle)):
                self.check()
                if time.monotonic()>deadline:
                    raise FocusSafetyError('작성 취소 확인창에서 예약 초안으로 돌아오지 못했습니다.')
                time.sleep(.05)
            self.log('draft_restored_for_readback', handle=form.handle)
        form=self.activate(form)
        recipient, path = self.read_form_recipient(form, 'resume-draft', expected_names={row.chat_name for row in rows})
        body = self.body(form).replace('\r\n', '\n').replace('\r', '\n')
        matches = [row for row in rows if ''.join(row.chat_name.split()) == ''.join(recipient.text.split()) and row.text == body]
        if not matches:
            raise FocusSafetyError('열린 예약창의 대상·본문이 이번 CSV와 다릅니다. 해당 예약창을 닫은 뒤 다시 시작하세요.')
        self.log('matching_draft_closed', path=str(path), source_indexes=[row.row_number for row in matches])
        self.created_forms.add(form.handle)
        self.close_draft(form)

    def read_pending_count(self, *, chat_name=None):
        """Refresh the native pending list. No submission or deletion inputs."""
        if any(window.title == '예약 메시지' for window in self.windows()):
            raise FocusSafetyError('예약 입력창을 닫은 뒤 슬롯 목록을 확인하세요.')
        boards = [window for window in self.windows() if window.title == '브리핑 보드']
        if len(boards) > 1:
            raise FocusSafetyError('예약 목록 창이 여러 개 열려 있습니다.')
        if boards:
            board = self.activate(boards[0])
        elif chat_name:
            board = self.open_board(self.open_chat(chat_name))
        else:
            raise FocusSafetyError('목록을 열 대화방이 필요합니다. CSV를 불러오거나 대화방을 먼저 지정하세요.')
        if not .60 < board.width / board.height < .64:
            raise FocusSafetyError('예약 목록 창의 배치를 확인하지 못했습니다.')
        self.click(board, board.width*50/370, board.height*149/600, 'pending-tab')
        self.click(board, board.width*304/370, board.height*93/600, 'refresh-pending-list')
        time.sleep(.3)
        path = self.capture(board, 'capacity-list')
        result = self.crop_ocr(path, (board.width*.04, board.height*.29, board.width*.45, board.height*.332), 'capacity-count')
        match = re.fullmatch(r'대기([0-9]+)개', ''.join(result.text.split()))
        if match is None or not 0 <= int(match[1]) <= 100:
            raise ScheduleReadbackError('실제 예약 대기 수량을 읽지 못했습니다. 슬롯을 임의로 비우지 않습니다.')
        count = int(match[1])
        self.log('capacity_observed', count=count, path=str(path))
        self.close_window(board)
        return count

    def open_board(self, chat: NativeWindow):
        boards=[w for w in self.windows() if w.title=="브리핑 보드"]
        if len(boards)==1:
            return self.activate(boards[0])
        if boards:
            raise FocusSafetyError("예약 목록 창이 여러 개 있습니다.")
        chat=self.activate(chat)
        form=self.open_form(chat)
        initial_body = self.body(form)
        before={w.handle for w in self.windows()}
        self.click_form(form,290,562,"open-reservation-list")
        board=self.wait_new(before,title="브리핑 보드")
        self.capture(board,"reservation-list")
        # The list button leaves its empty compose window behind. Close only
        # that newly created, still-empty draft so it cannot block later rows.
        form = self.activate(form)
        if self.body(form) != initial_body:
            raise FocusSafetyError('예약 목록을 연 빈 양식에 본문이 생겨 정리하지 않습니다.')
        self.close_window(form)
        return self.activate(board)

    def verify_list(self, rows: list[MessageRow], journal, *, only_keys=None, on_progress=None):
        """Read saved details from the native list; never click Save or Delete."""
        from kakao_schedule_journal import reservation_key
        from PIL import Image
        expected={reservation_key(row):row for row in rows}
        allowed_names={"".join(row.chat_name.split()) for row in rows}
        needed=set(expected if only_keys is None else only_keys)
        observed=set()
        self.last_list_count=None
        boards=[w for w in self.windows() if w.title=="브리핑 보드"]
        if len(boards)==1:
            board=self.activate(boards[0])
        else:
            board=self.open_board(self.open_chat(rows[0].chat_name))
        self.click(board, board.width*50/370, board.height*149/600, 'audit-pending-tab')
        self.click(board, board.width*304/370, board.height*93/600, 'audit-refresh-list')
        # Start from top so this operation also works after a previous audit.
        self.guard(board)
        self.input.moveTo(board.rect[0]+board.width*.7,board.rect[1]+board.height*.5)
        self.user.mouse_event(0x0800,0,0,120*50,0)
        time.sleep(.15)
        last_page=None
        for page in range(100):
            path=self.capture(board,"list-page")
            if page==0:
                count_ocr=self.crop_ocr(path,(round(board.width*.04),round(board.height*.29),round(board.width*.45),round(board.height*.332)),"pending-count")
                count_match=re.fullmatch(r"대기([0-9]+)개","".join(count_ocr.text.split()))
                if count_match:
                    self.last_list_count=int(count_match[1])
                self.log('list_pending_count',count=self.last_list_count,text=count_ocr.text,path=str(path))
            with Image.open(path) as image:
                digest=hashlib.sha256(image.crop((60,round(board.height*.3),board.width-20,board.height-110)).tobytes()).hexdigest()
            if digest==last_page:
                break
            last_page=digest
            # List labels start at the left; avatars are on the right.
            left=round(board.width*.025)
            top=round(board.height*.30)
            ocr=self.crop_ocr(path,(left,top,round(board.width*.80),round(board.height*.90)),"list-items")
            candidates=[]
            for line in ocr.lines:
                compact="".join(line.text.split())
                candidate_name=re.sub(r"[·•.]?대기$","",compact)
                if candidate_name in allowed_names or compact.endswith('대기'):
                    candidates.append((line.rect.center[0]+left,line.rect.center[1]+top))
            for x,y in candidates:
                self.guard(board)
                before={w.handle for w in self.windows()}
                self.click(board,x,y,"open-saved-detail",count=2)
                detail=self.wait_new(before,title="예약 메시지")
                try:
                    actual, _, detail_path=self.read_form(detail,"saved-detail",expected_names={row.chat_name for row in rows})
                    body=self.body(detail).replace("\r\n","\n").replace("\r","\n")
                    matches=[(key,row) for key,row in expected.items() if "".join(actual.chat_name.split())=="".join(row.chat_name.split()) and actual.scheduled_at==row.scheduled_at and body==row.text]
                    if len(matches)==1:
                        key,row=matches[0]
                        observed.add(key)
                        if journal.get(key).state in ("submitting","acknowledged","unknown"):
                            journal.mark_verified(key,evidence={"list_screenshot":str(path),"detail_screenshot":str(detail_path),"observed_chat_name":actual.chat_name,"observed_scheduled_at":actual.scheduled_at.isoformat(),"body_sha256":hashlib.sha256(body.encode()).hexdigest()})
                        self.log("list_verified",key=key,chat_name=row.chat_name,scheduled_at=row.scheduled_at.isoformat(),path=str(detail_path))
                        if on_progress:
                            on_progress(row,len(observed),self.last_list_count)
                    else:
                        self.log('list_detail_unmatched',chat_name=actual.chat_name,scheduled_at=actual.scheduled_at.isoformat(),path=str(detail_path))
                except ScheduleReadbackError as error:
                    self.log('list_detail_unreadable',error=str(error),list_path=str(path))
                finally:
                    self.close_window(detail)
                self.guard(board)
                self.capture(board,"detail-closed")
                if needed <= observed:
                    return observed
            self.guard(board)
            self.input.moveTo(board.rect[0]+board.width*.7,board.rect[1]+board.height*.5)
            self.log("scroll_list",page=page,ticks=-4)
            self.user.mouse_event(0x0800,0,0,-120*4,0)
            time.sleep(.15)
        missing=needed-observed
        if missing:
            self.log('list_audit_missing',keys=sorted(missing),observed=len(observed),pending_count=self.last_list_count)
            raise FocusSafetyError(f"예약 목록에서 {len(missing)}건의 정확한 저장 내용을 확인하지 못했습니다.")
        return observed

    def close(self):
        session = getattr(self, '_ocr_session', None)
        try:
            if session is not None:
                try:
                    self.log('ocr_session_summary', launches=session.launches, requests=session.requests, cache_hits=session.cache_hits)
                finally:
                    try:
                        session.close()
                    finally:
                        try:
                            self._ocr_scope.__exit__(None, None, None)
                        finally:
                            self._ocr_session = None
        finally:
            if hasattr(self, 'previous_input_pause'):
                self.input.PAUSE = self.previous_input_pause
                self.input.FAILSAFE = self.previous_input_failsafe
            if self.previous_dpi:
                self.user.SetThreadDpiAwarenessContext(self.previous_dpi)
                self.previous_dpi = None
