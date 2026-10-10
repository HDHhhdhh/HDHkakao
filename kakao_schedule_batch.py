"""CSV -> native KakaoTalk reservations with a durable no-resubmit journal.

Default is preparation only. --submit explicitly enables registration.
Run on an unlocked Windows desktop; Esc and PyAutoGUI's corner stop work.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path

from kakao_pc_lease import pc_kakao_ui_lease
from kakao_app_paths import SCHEDULE_JOURNAL_PATH
from kakao_schedule_session import NativeScheduleSession
from kakao_schedule_journal import ScheduleJournal, reservation_key
from kakao_sender import MessageRow, ScheduleRegistrationResult, load_messages, validate_schedule_row


def load_source(args):
    if args.csv:
        return load_messages(args.csv)
    raw = json.loads(args.source_json.read_text(encoding="utf-8-sig"))
    return [MessageRow(x["index"], x["chat_name"], x["text"], scheduled_at=datetime.fromisoformat(x["scheduled_at"])) for x in raw]


def write_status(journal, rows, output):
    records = {record.key: record for record in journal.records()}
    items = []
    for row in rows:
        key = reservation_key(row)
        record = records[key]
        items.append({"index": row.row_number, "chat_name": row.chat_name,
                      "scheduled_at": row.scheduled_at.isoformat(), "state": record.state,
                      "key": key, "evidence": record.events[-1]})
    status = {"at":datetime.now().isoformat(),"counts":dict(Counter(x['state'] for x in items)),"items":items}
    output.mkdir(parents=True,exist_ok=True)
    temp = output / "status.tmp"
    temp.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding="utf-8")
    temp.replace(output / "status.json")
    return status


def submit_batch(args, rows, journal):
    """Use the same local slot admission and registration receipts as the GUI."""
    records = {record.key: record for record in journal.records()}
    unresolved = [row for row in rows if records[reservation_key(row)].state in ('submitting', 'unknown')]
    for row in unresolved:
        print(json.dumps({'event':'previous_submission_unresolved', 'index':row.row_number,
                          'chat_name':row.chat_name, 'scheduled_at':row.scheduled_at.isoformat(),
                          'action':'held_without_resubmit'}, ensure_ascii=False), flush=True)
    selected = [row for row in rows if records[reservation_key(row)].safe_to_submit]
    chat_order = {name: index for index, name in enumerate(dict.fromkeys(row.chat_name for row in selected))}
    selected.sort(key=lambda row: (chat_order[row.chat_name], row.scheduled_at))
    if args.limit:
        selected = selected[:args.limit]
    if not selected:
        print(json.dumps({'event': 'no_pending_rows'}), flush=True)
        return 2 if unresolved else 0
    session = NativeScheduleSession(journal, args.output)
    try:
        with pc_kakao_ui_lease(cancel_event=session.cancel_event):
            session.prepare_batch(selected + unresolved)
            try:
                for row in selected:
                    result = session.register(row)
                    state = write_status(journal, rows, args.output)
                    print(json.dumps({'event': 'verified' if result.verified else 'acknowledged',
                                      'index': row.row_number, 'counts': state['counts']}, ensure_ascii=False), flush=True)
                    if type(result) is ScheduleRegistrationResult and result.cleanup_error:
                        print(json.dumps({'event':'chat_cleanup_failed', 'index':row.row_number,
                                          'error':result.cleanup_error,
                                          'registration_preserved':True}, ensure_ascii=False), flush=True)
                        return 2
            except KeyboardInterrupt:
                session.cancel_event.set()
                raise
    finally:
        try:
            write_status(journal, rows, args.output)
        finally:
            session.close()
    print(json.dumps({'event': 'batch_registered', 'count': len(selected), 'unresolved':len(unresolved)}, ensure_ascii=False), flush=True)
    return 2 if unresolved else 0


def run(args):
    if args.limit < 0:
        raise ValueError('--limit은 0 이상의 정수여야 합니다.')
    rows=load_source(args)
    journal=ScheduleJournal(args.journal)
    if args.import_run:
        journal.import_prior_run(args.import_run/'rows.json',args.import_run/'results.json')
    journal.prepare(rows)
    state=write_status(journal,rows,args.output)
    if args.status:
        print(json.dumps(state['counts'],ensure_ascii=False),flush=True)
        return 0
    if args.submit:
        return submit_batch(args, rows, journal)
    if args.verify_only:
        from kakao_native_schedule import NativeScheduleDriver
        records = {record.key: record for record in journal.records()}
        keys = {reservation_key(row) for row in rows
                if records[reservation_key(row)].state in ('submitting', 'acknowledged', 'verified', 'unknown')}
        if not keys:
            print(json.dumps({'event':'nothing_to_verify'}),flush=True)
            return 0
        driver=NativeScheduleDriver(args.output)
        def report_verified(row,count,pending_count):
            state=write_status(journal,rows,args.output)
            print(json.dumps({'event':'detail_verified','index':row.row_number,'observed_this_audit':count,'native_pending_count':pending_count,'counts':state['counts']},ensure_ascii=False),flush=True)
        try:
            with pc_kakao_ui_lease():
                found=driver.verify_list(rows,journal,only_keys=keys,on_progress=report_verified)
                result={'event':'list_verified','at':datetime.now().isoformat(),'count':len(found),'native_pending_count':driver.last_list_count,'keys':sorted(found)}
                (args.output/'list-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(result,ensure_ascii=False),flush=True)
        finally:
            try:
                write_status(journal,rows,args.output)
            finally:
                driver.close()
        return 0
    # Hold prior uncertain rows; preparation of unrelated rows can proceed.
    source_keys = {reservation_key(row) for row in rows}
    uncertain = [record for record in journal.records()
                 if record.state in ('submitting', 'unknown') and record.key in source_keys]
    for record in uncertain:
        print(json.dumps({'event':'previous_submission_unresolved', 'key':record.key,
                          'chat_name':record.chat_name, 'scheduled_at':record.scheduled_at,
                          'action':'held_without_resubmit'}, ensure_ascii=False), flush=True)
    pending_keys={r.key for r in journal.pending(rows)}
    pending=[row for row in rows if reservation_key(row) in pending_keys]
    # Preserve first occurrence order; finish each chat before changing target.
    chat_order = {name: index for index, name in enumerate(dict.fromkeys(row.chat_name for row in pending))}
    pending.sort(key=lambda row: (chat_order[row.chat_name], row.scheduled_at))
    for row in pending:
        validate_schedule_row(row,row.scheduled_at)
    if not pending:
        print(json.dumps({'event':'no_pending_rows','counts':state['counts']}),flush=True)
        return 2 if uncertain else 0
    from kakao_native_schedule import NativeScheduleDriver
    driver=NativeScheduleDriver(args.output)
    try:
        with pc_kakao_ui_lease():
            if args.resume_form:
                forms=[window for window in driver.windows() if window.title=='예약 메시지']
                if len(forms)>1:
                    raise RuntimeError('예약 양식이 여러 개여서 재개하지 않습니다.')
                if forms:
                    form=driver.activate(forms[0])
                    observed,_,_=driver.read_form(form,'resume-identify',expected_names={row.chat_name for row in pending})
                    body=driver.body(form).replace('\r\n','\n').replace('\r','\n')
                    matches=[row for row in pending if ''.join(row.chat_name.split())==''.join(observed.chat_name.split()) and row.text==body]
                    if not matches:
                        raise RuntimeError('열린 양식의 대상·본문과 일치하는 미등록 CSV 행이 없습니다.')
                    resumed=min(matches,key=lambda row:(row.scheduled_at!=observed.scheduled_at,row.scheduled_at))
                    pending.remove(resumed);pending.insert(0,resumed)
                    print(json.dumps({'event':'resuming_form','index':resumed.row_number,'chat_name':resumed.chat_name},ensure_ascii=False),flush=True)
            if args.limit:
                pending=pending[:args.limit]
            boards=[window for window in driver.windows() if window.title=="브리핑 보드"]
            if len(boards)==1:
                driver.close_window(driver.activate(boards[0]))
            for row in pending:
                journal.assert_safe_to_submit(row)
                print(json.dumps({"event":"preparing","index":row.row_number,"chat_name":row.chat_name,"scheduled_at":row.scheduled_at.isoformat()},ensure_ascii=False),flush=True)
                try:
                    chat,form,path=driver.prepare(row,resume_form=args.resume_form)
                    print(json.dumps({"event":"prepared_not_submitted","index":row.row_number,"screenshot":str(path)},ensure_ascii=False),flush=True)
                    break
                except BaseException as error:
                    if journal.safe_to_submit(row):
                        journal.mark_rejected_before_submit(row,evidence={"error":str(error)})
                    write_status(journal,rows,args.output)
                    driver.log('run_stopped',index=row.row_number,error=str(error))
                    raise
    finally:
        driver.close()
    return 2 if uncertain else 0


def parser():
    parser=argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--csv',type=Path)
    source.add_argument('--source-json',type=Path)
    parser.add_argument('--journal',type=Path,default=SCHEDULE_JOURNAL_PATH,
                        help='기본값은 Hub GUI와 공유하는 예약 기록입니다.')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--import-run',type=Path)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--submit',action='store_true')
    parser.add_argument('--resume-form',action='store_true')
    parser.add_argument('--limit',type=int,default=0)
    mode.add_argument('--status',action='store_true')
    mode.add_argument('--verify-only',action='store_true',help='저장된 예약 목록과 상세만 읽습니다. 새 등록은 하지 않습니다.')
    return parser


if __name__=='__main__':
    raise SystemExit(run(parser().parse_args()))
