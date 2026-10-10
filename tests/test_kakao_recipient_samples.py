"""An inspected UI label cannot be reused for different or damaged pixels."""
import hashlib
import json
from unittest.mock import Mock

from PIL import Image,ImageDraw
import pytest

import kakao_native_schedule as native
from kakao_schedule_readback import ScheduleReadbackError


@pytest.fixture
def sample(tmp_path,monkeypatch):
    monkeypatch.setattr(native,'__file__',str(tmp_path/'driver.py'))
    image=Image.new('RGB',(180,60),'white')
    ImageDraw.Draw(image).rectangle((12,15,150,35),fill='black')
    source=tmp_path/'reference.png';image.save(source)
    current=tmp_path/'current.png';image.save(current)
    value={'version':1,'samples':[{'observed_label':'확인한 대화방','size':list(image.size),
            'pixel_sha256':hashlib.sha256(image.tobytes()).hexdigest(),'image':source.name}]}
    manifest=tmp_path/'native_schedule_recipient_samples.json'
    manifest.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    driver=native.NativeScheduleDriver.__new__(native.NativeScheduleDriver);driver.log=Mock()
    return driver,current,source,manifest,value


def test_only_the_complete_identical_region_reuses_the_inspected_label(sample):
    driver,current,_,_,_=sample
    result=driver.read_recipient_sample(current,(0,0,180,60),allowed={'확인한대화방'})
    assert result.text=='확인한 대화방'
    with Image.open(current) as image:
        image.putpixel((179,59),(0,0,0));image.save(current)
    assert driver.read_recipient_sample(current,(0,0,180,60),allowed={'확인한대화방'}) is None


def test_identical_pixels_do_not_authorize_a_different_csv_target(sample):
    driver,current,_,_,_=sample
    with pytest.raises(ScheduleReadbackError):
        driver.read_recipient_sample(current,(0,0,180,60),allowed={'다른대화방'})


def test_a_changed_reference_image_cannot_supply_a_label(sample):
    driver,current,source,_,_=sample
    Image.new('RGB',(180,60),'white').save(source)
    with pytest.raises(ScheduleReadbackError):
        driver.read_recipient_sample(current,(0,0,180,60),allowed={'확인한대화방'})


def test_conflicting_calibration_records_fail_closed(sample):
    driver,current,_,manifest,value=sample
    value['samples'].append({**value['samples'][0],'observed_label':'다른 대화방'})
    manifest.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    with pytest.raises(ScheduleReadbackError):
        driver.read_recipient_sample(current,(0,0,180,60),allowed={'확인한대화방'})
