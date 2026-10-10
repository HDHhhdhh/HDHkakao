from copy import deepcopy
import unittest

from kakao_delivery import progress_summary
from kakao_sender import MessageRow
from kakao_sender_gui import KakaoSenderApp


class ResultTree:
    def __init__(self):
        self.rows = {str(index): {"values": [index, "fixture", "text", "", "", "직접 전송", "직접"],
                                 "tags": ("success",)} for index in range(3)}

    def get_children(self):
        return tuple(self.rows)

    def item(self, item_id, option=None, **changes):
        if changes:
            self.rows[item_id].update(deepcopy(changes))
        return self.rows[item_id].get(option) if option else self.rows[item_id]


class SendStatusResetTests(unittest.TestCase):
    def test_new_run_clears_old_green_even_for_rows_not_selected_this_time(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.tree = ResultTree()
        app.reset_run_preview_statuses([(1, MessageRow(3, "fixture", "text"))])
        self.assertEqual([item["values"][5] for item in app.tree.rows.values()], ["미대상", "대기", "미대상"])
        self.assertTrue(all(item["tags"] == ("ready",) for item in app.tree.rows.values()))

    def test_batch_halt_counts_attempted_unknown_and_unattempted_rows_separately(self):
        records = [{"row_index": i, "status": "direct_sent", "transport": "direct"} for i in range(30)]
        records += [{"row_index": 30, "status": "unknown", "transport": "direct"}]
        records += [{"row_index": i, "status": "stopped", "transport": "direct"} for i in range(31, 85)]
        summary = progress_summary(85, records)
        self.assertIn("성공: 30", summary)
        self.assertIn("확인: 1", summary)
        self.assertIn("중단: 54", summary)
        self.assertIn("재시도: 0", summary)


if __name__ == "__main__":
    unittest.main()
