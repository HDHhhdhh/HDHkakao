"""Live layout QA: prepare and compare a source row, NEVER submit it."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kakao_native_schedule import NativeScheduleDriver
from kakao_pc_lease import pc_kakao_ui_lease
from kakao_sender import MessageRow

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-json',type=Path,required=True)
parser.add_argument('--index',type=int,default=1)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--count-only',action='store_true')
args=parser.parse_args()
raw=next(item for item in json.loads(args.source_json.read_text(encoding='utf-8-sig')) if item['index']==args.index)
row=MessageRow(raw['index'],raw['chat_name'],raw['text'],scheduled_at=datetime.fromisoformat(raw['scheduled_at']))
driver=NativeScheduleDriver(args.output)
try:
    with pc_kakao_ui_lease():
        initial = [{'rect': window.rect, 'minimized': bool(driver.gui.IsIconic(window.handle))}
                   for window in driver.windows() if window.title == row.chat_name]
        if args.count_only:
            result={'native_pending_count':driver.read_pending_count(chat_name=row.chat_name),'submitted':False}
        else:
            chat,form,path=driver.prepare(row)
            result={'initial_chat': initial,'chat_rect':chat.rect,'form_rect':form.rect,'readback':driver.last_verification,'submitted':False}
            driver.close_draft(form)
        (args.output/'probe-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
finally:
    try:
        driver.discard_owned_draft()
    except Exception:
        pass
    finally:
        driver.close()
