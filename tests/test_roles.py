from dataclasses import replace

import pytest

from callscope.config import load_config
from callscope.model import RawTranscript
from callscope.roles import assign_roles


@pytest.fixture
def config():
    from callscope.config import DEFAULTS_DIR

    return load_config(DEFAULTS_DIR / "callscope.yaml")


def raw(segments, mode="stereo"):
    return RawTranscript.from_json(
        {
            "mode": mode,
            "duration": 60.0,
            "segments": [
                {"start": s, "end": e, "text": t, "channel": c} for s, e, t, c in segments
            ],
        }
    )


# Клиент заговорил первым («Алло»), менеджер (правый канал) представился от компании.
INTRO_RIGHT = [
    (0.0, 1.0, "Алло?", "left"),
    (1.5, 4.0, "Здравствуйте, меня зовут Анна, компания СтройДом.", "right"),
    (4.2, 6.0, "Звоню по поводу вашей заявки.", "right"),
    (6.5, 8.0, "Да, слушаю вас.", "left"),
]


def test_manager_is_side_that_introduces_from_company(config):
    dialogue, warnings = assign_roles(raw(INTRO_RIGHT), config, "a.wav")
    assert dialogue.manager_channel == "right"
    assert dialogue.role_source == "intro"
    assert [u.role for u in dialogue.utterances] == ["client", "manager", "client"]
    assert warnings == []


def test_adjacent_segments_of_one_side_are_merged(config):
    dialogue, _ = assign_roles(raw(INTRO_RIGHT), config, "a.wav")
    merged = dialogue.utterances[1]
    assert merged.text == (
        "Здравствуйте, меня зовут Анна, компания СтройДом. Звоню по поводу вашей заявки."
    )
    assert (merged.start, merged.end) == (1.5, 6.0)
    assert [u.index for u in dialogue.utterances] == [0, 1, 2]


def test_company_name_alone_identifies_manager(config):
    segments = [
        (0.0, 1.0, "Слушаю.", "left"),
        (1.2, 3.0, "Добрый день, СтройДом, по вашей заявке.", "right"),
    ]
    dialogue, _ = assign_roles(raw(segments), config, "a.wav")
    assert dialogue.manager_channel == "right"


def test_nobody_introduced_first_speaker_is_manager_with_warning(config):
    segments = [(0.0, 1.0, "Алло, добрый день.", "right"), (1.5, 3.0, "Да, слушаю.", "left")]
    dialogue, warnings = assign_roles(raw(segments), config, "call.wav")
    assert dialogue.manager_channel == "right"
    assert dialogue.role_source == "first_speaker"
    assert len(warnings) == 1 and "call.wav" in warnings[0]


def test_config_channel_beats_intro(config):
    cfg = replace(config, manager_channel="left")
    dialogue, _ = assign_roles(raw(INTRO_RIGHT), cfg, "a.wav")
    assert dialogue.manager_channel == "left"
    assert dialogue.role_source == "manual"


def test_file_override_beats_config(config):
    cfg = replace(config, manager_channel="right", manager_channel_overrides={"a.wav": "left"})
    dialogue, _ = assign_roles(raw(INTRO_RIGHT), cfg, "a.wav")
    assert dialogue.manager_channel == "left"
    other, _ = assign_roles(raw(INTRO_RIGHT), cfg, "b.wav")
    assert other.manager_channel == "right"


def test_cli_channel_beats_file_override(config):
    cfg = replace(config, manager_channel_overrides={"a.wav": "right"})
    dialogue, _ = assign_roles(raw(INTRO_RIGHT), cfg, "a.wav", cli_channel="left")
    assert dialogue.manager_channel == "left"
    assert dialogue.role_source == "manual"


def test_cli_auto_falls_through_to_detection(config):
    dialogue, _ = assign_roles(raw(INTRO_RIGHT), config, "a.wav", cli_channel="auto")
    assert dialogue.role_source == "intro"


def test_mono_is_approximate_stereo_is_not(config):
    mono, _ = assign_roles(raw(INTRO_RIGHT, mode="mono"), config, "a.wav")
    stereo, _ = assign_roles(raw(INTRO_RIGHT), config, "a.wav")
    assert mono.approximate is True
    assert stereo.approximate is False


def test_empty_transcript_gives_empty_dialogue_with_warning(config):
    dialogue, warnings = assign_roles(raw([]), config, "silent.wav")
    assert dialogue.utterances == ()
    assert warnings and "silent.wav" in warnings[0]
