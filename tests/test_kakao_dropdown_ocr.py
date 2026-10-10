from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from kakao_dropdown_ocr import NumericDropdownOcrError, _Cell, _build_strip, _restore_coordinates, recognize_numeric_dropdown_image
from kakao_windows_ocr import OcrLine, OcrRect, OcrResult, OcrWord


def recognition(*items):
    lines = []
    for text, x, y, width in items:
        rect = OcrRect(x, y, width, 12)
        lines.append(OcrLine(text, (OcrWord(text, rect),), rect))
    return OcrResult(120, 70, "en-US", tuple(lines))


class NumericDropdownOcrTests(unittest.TestCase):
    def setUp(self):
        self.cells = (_Cell((20, 10, 48, 50), (20, 20)), _Cell((20, 70, 48, 110), (60, 20)))

    def test_words_map_to_original_coordinates_without_known_values(self):
        result = _restore_coordinates(recognition(("07", 23, 30, 20), ("55", 64, 30, 20)), self.cells, (68, 120))
        self.assertEqual(result.text, "07\n55")
        self.assertEqual(result.lines[0].rect, OcrRect(23, 20, 20, 12))
        self.assertEqual(result.lines[1].rect, OcrRect(24, 80, 20, 12))
        self.assertEqual((result.width, result.height), (68, 120))

    def test_missing_duplicate_merged_and_nonnumeric_words_fail_closed(self):
        cases = (
            recognition(("7", 23, 30, 10)),
            recognition(("07", 23, 30, 20), ("7", 64, 30, 10)),
            recognition(("75", 23, 30, 57)),
            recognition(("O7", 23, 30, 20), ("55", 64, 30, 20)),
            recognition(("７", 23, 30, 10), ("55", 64, 30, 20)),
            recognition(("7", 23, 30, 0), ("55", 64, 30, 20)),
            recognition(("7", 23, 30, 10), ("8", 35, 30, 10), ("55", 64, 30, 20)),
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(NumericDropdownOcrError):
                _restore_coordinates(case, self.cells, (68, 120))

    def test_segmentation_rejects_blank_clipped_and_irregular_rows(self):
        def sample(boxes):
            image = Image.new("RGB", (68, 190), "white")
            draw = ImageDraw.Draw(image)
            for box in boxes:
                draw.rectangle(box, fill="black")
            return image
        for boxes in ([], [(30, 4, 40, 20), (30, 64, 40, 80)],
                      [(30, 20, 40, 36), (30, 80, 40, 96), (30, 155, 40, 171)],
                      [(30, 20, 40, 36), (30, 80, 40, 88)]):
            with self.subTest(boxes=boxes), self.assertRaises(NumericDropdownOcrError):
                _build_strip(sample(boxes))

    def test_segmentation_preserves_source_pixels_and_saves_artifacts(self):
        image = Image.new("RGB", (68, 160), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 67, 159), outline="gray")
        draw.rectangle((2, 62, 65, 118), fill=(235, 235, 235))
        draw.rectangle((30, 29, 39, 45), fill="black")
        draw.rectangle((30, 89, 39, 105), fill="black")
        strip, cells = _build_strip(image)
        for cell in cells:
            x, y = cell.destination
            width, height = cell.source[2] - cell.source[0], cell.source[3] - cell.source[1]
            self.assertEqual(strip.crop((x, y, x + width, y + height)).tobytes(), image.crop(cell.source).tobytes())
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "menu.png"
            image.save(source)
            original = source.read_bytes()
            words = []
            for number, cell in enumerate(cells):
                x, y = cell.destination
                words.append((str(number), x + 3, y + 4, 10))
            with patch("kakao_dropdown_ocr.recognize_image", return_value=recognition(*words)) as read:
                result = recognize_numeric_dropdown_image(source)
            self.assertEqual(result.text, "0\n1")
            self.assertEqual(read.call_args.kwargs, {"language": "en-US", "scale": 3})
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue((source.parent / "menu-numeric-strip.png").is_file())
            self.assertTrue((source.parent / "menu-numeric-map.json").is_file())

    def test_partly_visible_boundary_row_is_omitted_without_guessing_its_value(self):
        image=Image.new('RGB',(68,280),'white');draw=ImageDraw.Draw(image)
        for y in (29,89,149,209):draw.rectangle((30,y,39,y+16),fill='black')
        draw.rectangle((30,269,39,272),fill='black')
        strip,cells=_build_strip(image)
        self.assertEqual(len(cells),4)
        self.assertTrue(all(cell.source[3]<269 for cell in cells))
        for cell in cells:
            x,y=cell.destination;left,top,right,bottom=cell.source
            self.assertEqual(strip.crop((x,y,x+right-left,y+bottom-top)).tobytes(),
                             image.crop(cell.source).tobytes())

    def test_partial_interior_row_or_off_grid_edge_ink_is_not_ignored(self):
        for partial_y in (149,253):
            with self.subTest(partial_y=partial_y):
                image=Image.new('RGB',(68,280),'white');draw=ImageDraw.Draw(image)
                for y in (29,89,149,209):
                    if y!=partial_y:draw.rectangle((30,y,39,y+16),fill='black')
                draw.rectangle((30,partial_y,39,partial_y+3),fill='black')
                with self.assertRaises(NumericDropdownOcrError):_build_strip(image)

    def test_raw_ocr_cannot_select_a_partial_numeric_row_or_wrong_image_coordinates(self):
        from kakao_dropdown_ocr import find_complete_numeric_option_center
        from kakao_schedule_readback import ScheduleReadbackError
        image=Image.new('RGB',(68,280),'white');draw=ImageDraw.Draw(image)
        for y in (29,89,149,209):draw.rectangle((30,y,39,y+16),fill='black')
        draw.rectangle((30,269,39,272),fill='black')
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'menu.png';image.save(source)
            def read(text,y,width=68):
                rect=OcrRect(30,y,10,4 if y==269 else 17)
                return OcrResult(width,280,'en-US',(OcrLine(text,(OcrWord(text,rect),),rect),))
            self.assertEqual(find_complete_numeric_option_center(source,read('10',209),10),(35.,217.5))
            with self.assertRaises(ScheduleReadbackError):
                find_complete_numeric_option_center(source,read('11',269),11)
            with self.assertRaises(NumericDropdownOcrError):
                find_complete_numeric_option_center(source,read('10',209,width=136),10)

    def test_language_fallback_requires_complete_numeric_evidence(self):
        image = Image.new("RGB", (68, 160), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((30, 29, 39, 45), fill="black")
        draw.rectangle((30, 89, 39, 105), fill="black")
        _, cells = _build_strip(image)
        first_x, first_y = cells[0].destination
        second_x, second_y = cells[1].destination
        english = recognition(("oo", first_x + 3, first_y + 4, 10), ("05", second_x + 3, second_y + 4, 10))
        korean = recognition(("00", first_x + 3, first_y + 4, 10), ("05", second_x + 3, second_y + 4, 10))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "menu.png"
            image.save(source)
            with patch("kakao_dropdown_ocr.recognize_image", side_effect=[english, korean]) as read:
                result = recognize_numeric_dropdown_image(source)
            self.assertEqual(result.text, "00\n05")
            self.assertEqual([call.kwargs["language"] for call in read.call_args_list], ["en-US", "ko"])
            with patch("kakao_dropdown_ocr.recognize_image", return_value=english):
                with self.assertRaises(NumericDropdownOcrError):
                    recognize_numeric_dropdown_image(source)


if __name__ == "__main__":
    unittest.main()
