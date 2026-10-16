from datetime import datetime

from callscope.filenames import parse_filename

DEFAULT = ("{manager}_{date}_{time}", "%Y-%m-%d", "%H%M")


def test_default_template_extracts_manager_and_datetime():
    meta = parse_filename("ivanov_2026-10-12_1530.mp3", *DEFAULT)
    assert meta.manager == "ivanov"
    assert meta.call_datetime == datetime(2026, 10, 12, 15, 30)
    assert meta.matched_template is True
    assert meta.file_name == "ivanov_2026-10-12_1530.mp3"


def test_hyphenated_manager_and_uppercase_extension():
    meta = parse_filename("petrov-a_2026-01-05_0905.WAV", *DEFAULT)
    assert meta.manager == "petrov-a"
    assert meta.call_datetime == datetime(2026, 1, 5, 9, 5)


def test_mismatch_gives_undefined_manager():
    meta = parse_filename("запись звонка.mp3", *DEFAULT)
    assert meta.manager == "Не определён"
    assert meta.matched_template is False
    assert meta.call_datetime is None


def test_invalid_date_is_mismatch():
    meta = parse_filename("ivanov_2026-13-45_1530.mp3", *DEFAULT)
    assert meta.matched_template is False
    assert meta.manager == "Не определён"


def test_other_template_from_config():
    meta = parse_filename("12.10.2026_sidorov.ogg", "{date}_{manager}", "%d.%m.%Y", "%H%M")
    assert meta.manager == "sidorov"
    assert meta.call_datetime == datetime(2026, 10, 12)
    assert meta.matched_template is True


def test_any_placeholder():
    meta = parse_filename(
        "call-778_ivanov_2026-10-12_1530.m4a", "{any}_{manager}_{date}_{time}", "%Y-%m-%d", "%H%M"
    )
    assert meta.manager == "ivanov"
    assert meta.call_datetime == datetime(2026, 10, 12, 15, 30)
