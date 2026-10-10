"""Regression checks for the real native automation failures (no live UI)."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import unittest
import hashlib
import json

from PIL import Image

from kakao_native_schedule import NativeScheduleDriver, NativeWindow
from kakao_schedule_readback import ScheduleReadbackError
from kakao_sender import FocusSafetyError, MessageRow
from kakao_windows_ocr import OcrResult, OcrLine, OcrRect, OcrWord


def ocr(text):
    rect=OcrRect(5,5,150,20)
    return OcrResult(600,90,"ko",(OcrLine(text,(OcrWord(text,rect),),rect),))


class NativeScheduleTests(unittest.TestCase):
    def driver(self):
        d=NativeScheduleDriver.__new__(NativeScheduleDriver)
        d.gui=Mock();d.process=Mock();d.user=Mock();d.input=Mock()
        d.guard=Mock();d.capture=Mock();d.log=Mock();d.check=Mock()
        d.activate=Mock(side_effect=lambda window:window)
        return d

    def test_count_selects_pending_not_completed_and_rejects_wrong_header(self):
        d=self.driver()
        board=NativeWindow(1,'브리핑 보드','EVA',2,(0,0,740,1200))
        d.windows=Mock(return_value=[board]);d.activate=Mock(return_value=board)
        d.click=Mock();d.close_window=Mock();d.crop_ocr=Mock(return_value=ocr('대기 54개'))
        with patch('kakao_native_schedule.time.sleep'):
            self.assertEqual(d.read_pending_count(),54)
        self.assertEqual(d.click.call_args_list[0].args[1:3],(100,298))
        d.close_window.assert_called_once_with(board)
        for text in ('완료 14개','대기 101개','대기 S4개',''):
            d.close_window.reset_mock();d.crop_ocr.return_value=ocr(text)
            with patch('kakao_native_schedule.time.sleep'),self.assertRaises(ScheduleReadbackError):
                d.read_pending_count()
            d.close_window.assert_not_called()

    def test_opening_list_closes_only_unchanged_new_draft(self):
        d=self.driver()
        chat=NativeWindow(1,'fixture','EVA',2,(0,0,760,960))
        form=NativeWindow(2,'예약 메시지','EVA',2,(0,0,740,1200))
        board=NativeWindow(3,'브리핑 보드','EVA',2,(740,0,1480,1200))
        d.windows=Mock(return_value=[chat]);d.activate=Mock(side_effect=lambda w:w)
        d.open_form=Mock(return_value=form);d.body=Mock(return_value='placeholder')
        d.click_form=Mock();d.wait_new=Mock(return_value=board);d.close_window=Mock()
        self.assertEqual(d.open_board(chat),board)
        d.close_window.assert_called_once_with(form)
        self.assertEqual([c.args[0] for c in d.activate.call_args_list],[chat,form,board])
        d.body.side_effect=['placeholder','new draft'];d.close_window.reset_mock()
        with self.assertRaises(FocusSafetyError):d.open_board(chat)
        d.close_window.assert_not_called()

    def test_draft_cleanup_refuses_unowned_form_or_unrelated_confirmation(self):
        d=self.driver();d.created_forms=set()
        form=NativeWindow(2,'예약 메시지','EVA',2,(0,0,740,1200))
        dialog=NativeWindow(3,'','EVA',2,(100,400,620,630))
        d.close_window=Mock();d.windows=Mock(return_value=[form])
        with self.assertRaises(FocusSafetyError):d.close_draft(form)
        d.close_window.assert_not_called()
        d.created_forms.add(form.handle)
        d.gui.IsWindow.return_value=True;d.gui.IsWindowVisible.return_value=True
        d.wait_new=Mock(return_value=dialog);d.gui.GetWindow.return_value=999
        d.click=Mock()
        with self.assertRaises(FocusSafetyError):d.close_draft(form)
        d.click.assert_not_called()

    def test_draft_cleanup_never_confirms_delete_or_other_prompts(self):
        d=self.driver();d.created_forms={2}
        form=NativeWindow(2,'예약 메시지','EVA',2,(0,0,740,1200))
        dialog=NativeWindow(3,'','EVA',2,(100,400,620,630))
        d.close_window=Mock();d.windows=Mock(return_value=[form])
        d.gui.IsWindow.return_value=True;d.gui.IsWindowVisible.return_value=True
        d.wait_new=Mock(return_value=dialog);d.gui.GetWindow.return_value=2;d.click=Mock()
        with patch('kakao_native_schedule.recognize_image',return_value=ocr('삭제하시겠습니까? 확인 취소')):
            with self.assertRaises(FocusSafetyError):d.close_draft(form)
        d.click.assert_not_called()

    def test_existing_owned_cancel_prompt_is_finished_without_closing_the_parent_again(self):
        d=self.driver();d.created_forms={2}
        form=NativeWindow(2,'예약 메시지','EVA',2,(0,0,740,1200))
        dialog=NativeWindow(3,'','EVA_Window_Dblclk',2,(100,400,622,629))
        d.windows=Mock(return_value=[form,dialog]);d.gui.GetWindow.return_value=form.handle
        d.close_window=Mock();d.wait_new=Mock()
        question=OcrRect(20,20,400,40);confirm=OcrRect(80,170,80,25);cancel=OcrRect(350,170,80,25)
        lines=tuple(OcrLine(text,(OcrWord(text,rect),),rect) for text,rect in
                    [('작성을 취소하겠습니까?',question),('확인',confirm),('취소',cancel)])
        d.gui.IsWindow.return_value=True;d.gui.IsWindowVisible.return_value=True
        d.click=Mock(side_effect=lambda *a,**k:setattr(d.gui.IsWindow,'return_value',False))
        with patch('kakao_native_schedule.recognize_image',return_value=OcrResult(522,229,'ko',lines)):
            d.close_draft(form)
        d.close_window.assert_not_called();d.wait_new.assert_not_called()
        d.click.assert_called_once_with(dialog,120.,182.5,'confirm-cancel-draft')
        self.assertNotIn(form.handle,d.created_forms)

    def test_resume_keeps_the_draft_before_reading_and_discards_only_a_csv_match(self):
        for body in ('원문', '다른 본문'):
            with self.subTest(body=body):
                d=self.driver();d.created_forms=set();sequence=[]
                form=NativeWindow(2,'예약 메시지','EVA_Window_Dblclk',2,(0,0,740,1200))
                dialog=NativeWindow(3,'','EVA_Window_Dblclk',2,(100,400,622,629))
                d.windows=Mock(return_value=[form,dialog]);d.gui.GetWindow.return_value=form.handle
                d.gui.IsWindow.return_value=True;d.gui.IsWindowVisible.return_value=True
                d.gui.IsWindowEnabled.return_value=False
                question=OcrRect(20,20,400,40);confirm=OcrRect(80,170,80,25);cancel=OcrRect(350,170,80,25)
                lines=tuple(OcrLine(text,(OcrWord(text,rect),),rect) for text,rect in
                            [('작성을 취소하겠습니까?',question),('확인',confirm),('취소',cancel)])
                def keep_draft(*args):
                    sequence.append('keep')
                    d.gui.IsWindow.return_value=False
                    d.gui.IsWindowEnabled.return_value=True
                d.click=Mock(side_effect=keep_draft)
                def read_restored(*args,**kwargs):
                    self.assertTrue(d.gui.IsWindowEnabled(form.handle))
                    sequence.append('read')
                    return ocr('학생'),Path('restored.png')
                d.read_form_recipient=Mock(side_effect=read_restored);d.body=Mock(return_value=body)
                d.close_draft=Mock(side_effect=lambda window:sequence.append('discard'))
                row=MessageRow(1,'학생','원문',scheduled_at=datetime(2099,1,1,10))
                with patch('kakao_native_schedule.recognize_image',return_value=OcrResult(522,229,'ko',lines)):
                    if body==row.text:
                        d.dismiss_matching_draft([row])
                        self.assertEqual(sequence,['keep','read','discard'])
                        d.close_draft.assert_called_once_with(form)
                        self.assertIn(form.handle,d.created_forms)
                    else:
                        with self.assertRaises(FocusSafetyError):d.dismiss_matching_draft([row])
                        self.assertEqual(sequence,['keep','read'])
                        d.close_draft.assert_not_called()
                        self.assertFalse(d.created_forms)
                d.click.assert_called_once_with(dialog,390.,182.5,'keep-draft-for-readback')
                self.assertEqual([call.args[0] for call in d.activate.call_args_list],[dialog,form])

    def test_draft_identity_cleanup_does_not_need_a_readable_default_date_or_time(self):
        d=self.driver();d.created_forms=set()
        form=NativeWindow(2,'예약 메시지','EVA_Window_Dblclk',2,(0,0,740,1200))
        d.windows=Mock(return_value=[form]);d.body=Mock(return_value='원문')
        d.read_form=Mock(side_effect=ScheduleReadbackError('default time unreadable'))
        d.read_form_recipient=Mock(return_value=(ocr('학생'),Path('recipient.png')))
        d.close_draft=Mock()
        row=MessageRow(1,'학생','원문',scheduled_at=datetime(2099,1,1,10))
        d.dismiss_matching_draft([row])
        d.read_form.assert_not_called()
        d.read_form_recipient.assert_called_once_with(form,'resume-draft',expected_names={'학생'})
        d.close_draft.assert_called_once_with(form)

    def test_reviewed_samples_require_exact_pixels_and_never_replace_csv_name(self):
        root=Path(__file__).resolve().parents[1]
        manifest=root/'native_schedule_recipient_samples.json'
        if not manifest.is_file():
            self.skipTest('Private reviewed production samples are not published.')
        samples=json.loads(manifest.read_text(encoding='utf-8'))['samples']
        for sample in samples:
            label=sample['observed_label']
            wrong='다른 대상 · '+label
            source=root/sample['image']
            with Image.open(source) as image:
                crop=image.convert('RGB')
            self.assertEqual(list(crop.size),sample['size'])
            self.assertEqual(hashlib.sha256(crop.tobytes()).hexdigest(),sample['pixel_sha256'])
            d=self.driver()
            d.crop_ocr=Mock(side_effect=AssertionError('Exact reviewed pixels should not require OCR'))
            box=(0,0,*crop.size)
            self.assertEqual(d.read_recipient(source,box,expected_names={label}).text,label)
            d.crop_ocr.assert_not_called()
            with self.assertRaises(ScheduleReadbackError):
                d.read_recipient(source,box,expected_names={wrong})
            with TemporaryDirectory() as temporary:
                changed=crop.copy()
                changed.putpixel((0,0),(254,255,255))
                path=Path(temporary)/'different.png'
                changed.save(path)
                self.assertIsNone(d.read_recipient_sample(path,box,allowed={''.join(label.split())}))

    def test_reviewed_catalog_contains_no_duplicate_or_unverifiable_samples(self):
        root=Path(__file__).resolve().parents[1]
        manifest=root/'native_schedule_recipient_samples.json'
        if not manifest.is_file():
            self.skipTest('Private reviewed production samples are not published.')
        samples=json.loads(manifest.read_text(encoding='utf-8'))['samples']
        self.assertEqual(len(samples),len({entry['pixel_sha256'] for entry in samples}))
        for sample in samples:
            with Image.open(root/sample['image']) as image:
                pixels=image.convert('RGB')
            self.assertEqual(hashlib.sha256(pixels.tobytes()).hexdigest(),sample['pixel_sha256'])
            self.assertEqual(list(pixels.size),sample['size'])

    def test_recipient_needs_two_independent_equal_readings(self):
        d=self.driver()
        d.crop_ocr=Mock(side_effect=[ocr("A 저현2 학생타"),ocr("A 저현2 학생카"),ocr("A 저현2 학생카")])
        result=d.read_recipient(Path('saved.png'),(1,2,3,4))
        self.assertEqual(result.text,"A 저현2 학생카")
        self.assertEqual(d.crop_ocr.call_count,3)

    def test_recipient_preserves_digit_and_rejects_no_consensus(self):
        d=self.driver()
        d.crop_ocr=Mock(side_effect=[ocr("A 안곡1 학생파"),ocr("A 안곡 학생파"),ocr("A 안곡1 학생파")])
        self.assertEqual(d.read_recipient(Path('saved.png'),(1,2,3,4)).text,"A 안곡1 학생파")
        d.crop_ocr=Mock(side_effect=[ocr("이름1"),ocr("이름2"),ocr("이름3")])
        with self.assertRaises(ScheduleReadbackError):d.read_recipient(Path('saved.png'),(1,2,3,4))

    def test_minimized_window_is_not_a_receipt_image(self):
        d=self.driver();d.gui.IsIconic.return_value=True
        window=NativeWindow(1,'chat','EVA',2,(-32000,-32000,-31686,-31950))
        with patch('PIL.ImageGrab.grab') as grab,self.assertRaises(FocusSafetyError):
            NativeScheduleDriver.capture(d,window,'receipt')
        grab.assert_not_called()

    def test_recipient_ink_recovery_needs_two_exact_observations(self):
        d=self.driver()
        d.crop_ocr=Mock(side_effect=[ocr('A 대진2 학생각'),ocr('A 대진2 학생각')])
        d.clean_recipient_ocr=Mock(side_effect=[ocr('A 대진2 학생각'),ocr('A 대진2 학생하'),ocr('A 대진2 학생하')])
        result=d.read_recipient(Path('saved.png'),(1,2,3,4),expected_names={'A 대진2 학생하'})
        self.assertEqual(result.text,'A 대진2 학생하')
        self.assertEqual([call.args[2] for call in d.clean_recipient_ocr.call_args_list],[100,140,180])

    def test_expected_name_cannot_replace_an_unread_character(self):
        d=self.driver()
        d.crop_ocr=Mock(side_effect=[ocr('A 대진2 학생각'),ocr('A 대진2 학생각')])
        d.clean_recipient_ocr=Mock(side_effect=[ocr('A 대진2 학생각'),ocr('A 대진2 학생하'),ocr('A 대진2 학생각')])
        d.spaced_recipient_ocr=Mock(side_effect=[ocr('A 대진2 학생하'),ocr('A 대진2 학생각')])
        with self.assertRaises(ScheduleReadbackError):
            d.read_recipient(Path('saved.png'),(1,2,3,4),expected_names={'A 대진2 학생하'})

    def test_recipient_spacing_requires_two_exact_full_labels(self):
        d=self.driver()
        d.crop_ocr=Mock(return_value=ocr('A 고국2 학생갈'))
        d.clean_recipient_ocr=Mock(return_value=ocr('A 고국2 학생갈'))
        d.spaced_recipient_ocr=Mock(side_effect=[ocr('A 고국2 학생간'),ocr('A 고국2 학생간')])
        result=d.read_recipient(Path('saved.png'),(1,2,3,4),expected_names={'A 고국2 학생간'})
        self.assertEqual(result.text,'A 고국2 학생간')
        self.assertEqual(d.spaced_recipient_ocr.call_count,2)
        d.spaced_recipient_ocr=Mock(side_effect=[ocr('A 고국2 학생간'),ocr('A 고국 학생간')])
        with self.assertRaises(ScheduleReadbackError):
            d.read_recipient(Path('saved.png'),(1,2,3,4),expected_names={'A 고국2 학생간'})

    def test_ampm_retry_reads_the_same_menu_before_selecting_the_observed_label(self):
        d=self.driver()
        form=NativeWindow(1,'예약 메시지','EVA',2,(0,0,740,1200))
        popup=NativeWindow(2,'','EVA_Menu',2,(500,850,700,990))
        d.windows=Mock(return_value=[]);d.wait_new=Mock(return_value=popup)
        d.click_form=Mock();d.click=Mock();d.capture=Mock(return_value=Path('menu.png'))
        with patch('kakao_native_schedule.recognize_image',side_effect=[ocr('오전'),ocr('오후')]) as read:
            d.dropdown(form,'ampm','오후')
        self.assertEqual([call.kwargs['scale'] for call in read.call_args_list],[2,3])
        self.assertEqual([call.args[0] for call in read.call_args_list],[Path('menu.png'),Path('menu.png')])
        d.click.assert_called_once_with(popup,80.,15.,'ampm-select',allow_owned_popup=True)

    def test_missing_ampm_label_after_retry_never_selects_an_assumed_row(self):
        d=self.driver()
        form=NativeWindow(1,'예약 메시지','EVA',2,(0,0,740,1200))
        d.windows=Mock(return_value=[]);d.wait_new=Mock(return_value=NativeWindow(2,'','EVA_Menu',2,(500,850,700,990)))
        d.click_form=Mock();d.click=Mock();d.capture=Mock(return_value=Path('menu.png'))
        with patch('kakao_native_schedule.recognize_image',side_effect=[ocr('오전'),ocr('오휴')]),self.assertRaises(ScheduleReadbackError):
            d.dropdown(form,'ampm','오후')
        d.click.assert_not_called()

    def test_hour_menu_starting_with_twelve_scrolls_down_to_observe_eleven(self):
        d=self.driver()
        form=NativeWindow(1,'예약 메시지','EVA',2,(0,0,740,1200))
        popup=NativeWindow(2,'','EVA_Menu',2,(500,800,600,1400))
        d.windows=Mock(return_value=[]);d.wait_new=Mock(return_value=popup)
        d.click_form=Mock();d.click=Mock();d.capture=Mock(return_value=Path('menu.png'))
        d.gui.GetAncestor.return_value=popup.handle
        def numeric(values):
            lines=[]
            for i,value in enumerate(values):
                rect=OcrRect(20,20+i*50,30,20)
                word=OcrWord(str(value),rect)
                lines.append(OcrLine(str(value),(word,),rect))
            return OcrResult(100,600,'en-US',tuple(lines))
        with patch('kakao_native_schedule.recognize_image',side_effect=[numeric([12]),numeric([10,11])]),patch('kakao_dropdown_ocr.recognize_numeric_dropdown_image',return_value=numeric([12,1,2,3,4,5,6,7,8,9])),patch('kakao_dropdown_ocr.find_complete_numeric_option_center',side_effect=[ScheduleReadbackError('target not visible'),(35.,80.)]),patch('kakao_native_schedule.time.sleep'):
            d.dropdown(form,'hour',11)
        d.user.mouse_event.assert_called_once_with(0x0800,0,0,-360,0)
        d.click.assert_called_once_with(popup,35.,80.,'hour-select',allow_owned_popup=True)

    def test_raw_ocr_of_clipped_hour_is_scrolled_before_the_complete_target_is_selected(self):
        from PIL import ImageDraw
        d=self.driver();form=NativeWindow(1,'예약 메시지','EVA',2,(0,0,740,1200))
        popup=NativeWindow(2,'','EVA_Menu',2,(500,800,568,1080))
        d.windows=Mock(return_value=[]);d.wait_new=Mock(return_value=popup)
        d.click_form=Mock();d.click=Mock();d.gui.GetAncestor.return_value=popup.handle
        def menu_result(values):
            lines=[]
            for value,y,h in values:
                rect=OcrRect(30,y,10,h)
                lines.append(OcrLine(str(value),(OcrWord(str(value),rect),),rect))
            return OcrResult(68,280,'en-US',tuple(lines))
        with TemporaryDirectory() as directory:
            clipped=Path(directory)/'clipped.png';complete=Path(directory)/'complete.png'
            for path in (clipped,complete):
                image=Image.new('RGB',(68,280),'white');draw=ImageDraw.Draw(image)
                for y in (29,89,149,209):draw.rectangle((30,y,39,y+16),fill='black')
                if path==clipped:draw.rectangle((30,269,39,272),fill='black')
                image.save(path)
            d.capture.side_effect=[clipped,complete,Path('selected.png')]
            raw=[menu_result([(11,269,4)]),menu_result([(11,209,17)])]
            full=menu_result([(7+i,29+60*i,17) for i in range(4)])
            with patch('kakao_native_schedule.recognize_image',side_effect=raw),patch('kakao_dropdown_ocr.recognize_numeric_dropdown_image',return_value=full) as read_full,patch('kakao_native_schedule.time.sleep'):
                d.dropdown(form,'hour',11)
            read_full.assert_called_once_with(clipped)
            d.user.mouse_event.assert_called_once_with(0x0800,0,0,-360,0)
            d.click.assert_called_once_with(popup,35.,217.5,'hour-select',allow_owned_popup=True)

    def test_date_retries_original_pixels_without_replacing_unread_characters(self):
        d=self.driver()
        d.crop_ocr=Mock(side_effect=[ocr('2026. 10. 2 (=)'),ocr('2026. 10.2 (금)')])
        self.assertEqual(d.read_date(Path('saved.png'),(1,2,3,4)).text,'2026. 10.2 (금)')
        d.crop_ocr=Mock(side_effect=[ocr('2026. 1O. 2 (금)'),ocr('2026. 10. 2 (=)')])
        with self.assertRaises(ScheduleReadbackError):d.read_date(Path('saved.png'),(1,2,3,4))

    def test_search_control_must_match_id_root_and_header_geometry(self):
        d=self.driver();main=NativeWindow(1,'카카오톡','EVA',2,(100,100,900,1700))
        d.children=Mock(return_value=[10,20,30])
        d.gui.GetDlgCtrlID.side_effect=lambda h:{10:100,20:100,30:200}[h]
        d.gui.GetAncestor.side_effect=lambda h,_:{10:1,20:999,30:1}[h]
        d.gui.GetWindowRect.return_value=(300,270,700,310)
        self.assertEqual(d.search_edit(main),10)
        d.gui.GetAncestor.return_value=1;d.gui.GetAncestor.side_effect=None
        with self.assertRaises(FocusSafetyError):d.search_edit(main)

    def test_keyboard_focus_must_be_the_exact_search_control(self):
        d=self.driver();main=NativeWindow(1,'카카오톡','EVA',2,(0,0,800,1600))
        d.process.GetWindowThreadProcessId.return_value=(99,2)
        d.search_edit=Mock(return_value=10)
        def info(_thread,pointer):
            pointer._obj.hwndFocus=20
            return True
        d.user.GetGUIThreadInfo.side_effect=info
        with self.assertRaises(FocusSafetyError):d.assert_search_focus(main,10)

    def test_search_does_not_send_global_select_all_and_requires_focus_before_enter(self):
        d=self.driver();main=NativeWindow(1,'카카오톡','EVA',2,(0,0,800,1600));chat=NativeWindow(2,'학생','EVA',2,(100,100,900,1700))
        d.windows=Mock(return_value=[main]);d.unique=Mock(side_effect=lambda title:main if title=='카카오톡' else chat)
        d.activate=Mock(side_effect=lambda w:w);d.search_edit=Mock(return_value=10)
        d.assert_search_focus=Mock();d.gui.GetWindowRect.return_value=(200,150,600,200)
        d.click=Mock();d.control_text=Mock(return_value='학생')
        with patch('kakao_native_schedule.time.sleep'):
            self.assertEqual(d.open_chat('학생'),chat)
        d.input.hotkey.assert_not_called()
        d.input.press.assert_called_once_with('enter')
        self.assertEqual(d.assert_search_focus.call_count,2)
        d.input.reset_mock();d.user.SendMessageW.reset_mock()
        d.assert_search_focus=Mock(side_effect=FocusSafetyError('wrong focused Edit'))
        with patch('kakao_native_schedule.time.sleep'),self.assertRaises(FocusSafetyError):d.open_chat('학생')
        d.input.press.assert_not_called();d.user.SendMessageW.assert_not_called()

    def test_submission_is_persisted_before_click_and_missing_toast_blocks_retry(self):
        d=self.driver()
        with TemporaryDirectory() as temp:
            path=Path(temp)/'form.png';Image.new('RGB',(740,1200),'white').save(path)
            row=MessageRow(1,'학생','본문',scheduled_at=datetime(2099,1,1,10))
            form=NativeWindow(1,'예약 메시지','EVA',2,(0,0,740,1200));chat=NativeWindow(2,'학생','EVA',2,(0,0,760,1280))
            d.body=Mock(return_value='본문');d.last_verification={'form_screenshot':str(path)}
            d.capture.return_value=path;d.find_toast_image=Mock(side_effect=FocusSafetyError('no toast'))
            d.wait_submission_toast=Mock(side_effect=FocusSafetyError('no toast after timeout'))
            d.gui.IsWindow.return_value=False;journal=Mock();sequence=[]
            journal.mark_submitting.side_effect=lambda *a,**k:sequence.append('persist')
            def click(*args, before_input=None):
                before_input()
                sequence.append('click')
            d.click_form=Mock(side_effect=click)
            with self.assertRaises(FocusSafetyError):d.submit(row,chat,form,path,journal)
            self.assertEqual(sequence,['persist','click'])
            journal.mark_unknown.assert_called_once();journal.mark_acknowledged.assert_not_called()

    def test_obscured_submit_point_does_not_commit_or_click(self):
        d=self.driver()
        with TemporaryDirectory() as temp:
            path=Path(temp)/'form.png';Image.new('RGB',(740,1200),'white').save(path)
            row=MessageRow(1,'fixture','body',scheduled_at=datetime(2099,1,1,10))
            form=NativeWindow(1,'예약 메시지','EVA',2,(0,0,740,1200))
            chat=NativeWindow(2,'fixture','EVA',2,(0,0,760,1280))
            d.body=Mock(return_value='body');d.last_verification={'form_screenshot':str(path)}
            d.capture.return_value=path;d.find_toast_image=Mock(side_effect=FocusSafetyError('no toast'))
            d.gui.GetAncestor.return_value=999
            journal=Mock()
            with self.assertRaises(FocusSafetyError):d.submit(row,chat,form,path,journal)
            d.gui.BringWindowToTop.assert_called_once_with(form.handle)
            d.input.click.assert_not_called()
            journal.mark_submitting.assert_not_called()
            journal.mark_unknown.assert_not_called()

    def test_dispatch_commit_runs_after_hit_test_and_before_native_input(self):
        d=self.driver()
        with TemporaryDirectory() as temp:
            path=Path(temp)/'form.png';Image.new('RGB',(740,1200),'white').save(path)
            row=MessageRow(1,'fixture','body',scheduled_at=datetime(2099,1,1,10))
            form=NativeWindow(1,'예약 메시지','EVA',2,(0,0,740,1200))
            chat=NativeWindow(2,'fixture','EVA',2,(0,0,760,1280))
            d.body=Mock(return_value='body');d.last_verification={'form_screenshot':str(path)}
            d.capture.return_value=path;d.find_toast_image=Mock(side_effect=FocusSafetyError('no toast'))
            d.wait_submission_toast=Mock(side_effect=FocusSafetyError('no toast after timeout'))
            d.gui.IsWindow.return_value=False;journal=Mock();sequence=[]
            d.gui.GetAncestor.side_effect=lambda *args:sequence.append('hit_test') or form.handle
            journal.mark_submitting.side_effect=lambda *args,**kwargs:sequence.append('persist')
            d.input.click.side_effect=lambda *args,**kwargs:sequence.append('input')
            with patch('kakao_native_schedule.time.sleep'),self.assertRaises(FocusSafetyError):
                d.submit(row,chat,form,path,journal)
            self.assertEqual(sequence,['hit_test','persist','input'])
            journal.mark_unknown.assert_called_once()
            self.assertTrue(journal.mark_submitting.call_args.kwargs['evidence']['click_guard_passed'])

    def test_failed_commit_cannot_reach_native_input(self):
        d=self.driver()
        form=NativeWindow(1,'예약 메시지','EVA',2,(0,0,740,1200))
        d.gui.GetAncestor.return_value=form.handle
        commit=Mock(side_effect=OSError('disk full'))
        with self.assertRaises(OSError):d.click_form(form,180,562,'submit',before_input=commit)
        d.input.click.assert_not_called()


if __name__=='__main__':unittest.main()
