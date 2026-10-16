import pytest

from callscope.checklist import Checklist, ChecklistItem, load_checklist
from callscope.config import DEFAULTS_DIR, Thresholds
from callscope.model import Dialogue, Utterance
from callscope.scoring import grade_for, score_call

# --- нормализация словоформ (callscope.textnorm) --------------------------------


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        ("Здравствуйте, Иван!", "здравствуй"),
        ("Здравствуй, Иван!", "здравствуйте"),
        ("Я вам предложу ламинат", "предлагаю"),
        ("Предлагаю ламинат", "предложу"),
        ("Всё понятно", "все понятно"),
        ("ЕЩЁ один вопрос", "еще"),
    ],
)
def test_word_forms_match(normalizer, text, phrase):
    assert normalizer.contains_phrase(text, phrase)


def test_phrase_lemmas_must_be_consecutive(normalizer):
    assert normalizer.contains_phrase("Меня зовут Анна", "меня зовут")
    assert not normalizer.contains_phrase("Зовут меня Анна", "меня зовут")
    assert not normalizer.contains_phrase("Добрый день", "до свидания")


def test_lemmas_lowercase_without_punctuation(normalizer):
    lemmas = normalizer.lemmas("Ёлки, ПАЛКИ!")
    assert all(lemma == lemma.lower() for lemma in lemmas)
    assert "," not in "".join(lemmas) and "!" not in "".join(lemmas)
    assert "ё" not in "".join(lemmas)


# --- оценка звонка по чек-листу -------------------------------------------------


THRESHOLDS = Thresholds(good=80, medium=50)


@pytest.fixture(scope="module")
def default_checklist() -> Checklist:
    return load_checklist(DEFAULTS_DIR / "checklist.yaml")


def items_by_id(score):
    return {item.item_id: item for item in score.items}


def make_dialogue(*lines: tuple[str, str]) -> Dialogue:
    return Dialogue(
        utterances=tuple(
            Utterance(index=i, start=float(i * 5), end=float(i * 5 + 4), text=text, role=role)
            for i, (role, text) in enumerate(lines)
        ),
        approximate=False,
        manager_channel="left",
        role_source="manual",
    )


def test_good_dialogue_scores_good(load_dialogue, default_checklist, normalizer):
    score = score_call(load_dialogue("good.json"), default_checklist, THRESHOLDS, normalizer)
    assert [item.item_id for item in score.items] == [
        "greeting",
        "need",
        "solution",
        "objection",
        "next_step",
        "farewell",
    ]
    assert all(item.status == "passed" for item in score.items)
    assert score.score == 100
    assert score.grade == "good"
    assert score.loss_item_id is None
    for item in score.items:
        assert item.reason and item.evidence
    objection = items_by_id(score)["objection"]
    assert {e.role for e in objection.evidence} == {"client", "manager"}


def test_no_objection_item_is_na_and_excluded(load_dialogue, default_checklist, normalizer):
    score = score_call(load_dialogue("no_objection.json"), default_checklist, THRESHOLDS)
    objection = items_by_id(score)["objection"]
    assert objection.status == "na"
    assert objection.reason
    assert score.score == 100
    assert score.grade == "good"


def test_no_open_questions_fails_need(load_dialogue, default_checklist, normalizer):
    score = score_call(load_dialogue("no_need.json"), default_checklist, THRESHOLDS, normalizer)
    by_id = items_by_id(score)
    need = by_id["need"]
    assert need.status == "failed"
    assert "открытых вопросов" in need.reason.lower()
    assert by_id["next_step"].status == "failed"
    assert by_id["objection"].status == "na"
    # (5 + 15 + 5) / (5 + 20 + 15 + 25 + 5) * 100
    assert score.score == pytest.approx(25 / 70 * 100, abs=0.1)
    assert score.grade == "bad"
    assert score.loss_item_id == "need"


def test_unhandled_objection_fails_with_client_quote_and_reply(
    load_dialogue, default_checklist, normalizer
):
    score = score_call(
        load_dialogue("failed_objection.json"), default_checklist, THRESHOLDS, normalizer
    )
    objection = items_by_id(score)["objection"]
    assert objection.status == "failed"
    assert objection.reason
    client_quotes = [e for e in objection.evidence if e.role == "client"]
    manager_quotes = [e for e in objection.evidence if e.role == "manager"]
    assert client_quotes[0].text.startswith("Ой, у конкурентов дешевле")
    assert client_quotes[0].utterance_index == 7
    assert manager_quotes[0].text == "Ну, у нас такие цены."
    assert score.loss_item_id == "objection"
    assert score.score == pytest.approx(45, abs=0.1)
    assert score.grade == "bad"


def test_loss_is_first_failed_in_checklist_order(normalizer):
    checklist = Checklist(
        items=(
            ChecklistItem(id="a", title="А", weight=1, phrases=("привет",)),
            ChecklistItem(id="b", title="Б", weight=1, phrases=("ламинат",)),
            ChecklistItem(id="c", title="В", weight=1, phrases=("пока",)),
        )
    )
    dialogue = make_dialogue(("manager", "Ламинат есть"), ("client", "Привет, пока"))
    score = score_call(dialogue, checklist, THRESHOLDS, normalizer)
    assert [i.status for i in score.items] == ["failed", "passed", "failed"]
    assert score.loss_item_id == "a"
    assert score.score == pytest.approx(100 / 3, abs=0.1)


def test_greeting_only_in_first_manager_replies(default_checklist, normalizer):
    late_greeting = make_dialogue(
        ("client", "Здравствуйте, это компания СтройДом?"),
        ("manager", "Да, слушаю."),
        ("client", "Мне нужен ламинат."),
        ("manager", "Хорошо."),
        ("client", "Какой есть?"),
        ("manager", "Здравствуйте, меня зовут Анна, забыла представиться."),
    )
    greeting = items_by_id(score_call(late_greeting, default_checklist, THRESHOLDS, normalizer))[
        "greeting"
    ]
    assert greeting.status == "failed"


def test_farewell_only_in_last_manager_replies(default_checklist, normalizer):
    early_bye = make_dialogue(
        ("manager", "До свидания, то есть здравствуйте!"),
        ("client", "Здравствуйте."),
        ("manager", "Слушаю вас."),
        ("client", "До свидания."),
        ("manager", "Ага."),
        ("manager", "Угу."),
    )
    farewell = items_by_id(score_call(early_bye, default_checklist, THRESHOLDS, normalizer))[
        "farewell"
    ]
    assert farewell.status == "failed"
    on_time = make_dialogue(("manager", "Слушаю."), ("manager", "Всего доброго!"))
    farewell = items_by_id(score_call(on_time, default_checklist, THRESHOLDS, normalizer))[
        "farewell"
    ]
    assert farewell.status == "passed"
    assert farewell.evidence[0].utterance_index == 1


def test_client_phrase_does_not_count_for_manager_item(default_checklist, normalizer):
    dialogue = make_dialogue(
        ("manager", "Алло."),
        ("client", "Здравствуйте, предлагаю записать меня на замер. До свидания."),
        ("manager", "Ага."),
    )
    by_id = items_by_id(score_call(dialogue, default_checklist, THRESHOLDS, normalizer))
    assert by_id["greeting"].status == "failed"
    assert by_id["solution"].status == "failed"
    assert by_id["next_step"].status == "failed"
    assert by_id["farewell"].status == "failed"


def test_min_matches_counts_distinct_replies(normalizer):
    checklist = Checklist(
        items=(ChecklistItem(id="q", title="Вопросы", weight=1, phrases=("цена",), min_matches=2),)
    )
    once = make_dialogue(("manager", "Цена хорошая, цена низкая"))
    twice = make_dialogue(("manager", "Цена хорошая"), ("manager", "Цена низкая"))
    assert score_call(once, checklist, THRESHOLDS, normalizer).items[0].status == "failed"
    assert score_call(twice, checklist, THRESHOLDS, normalizer).items[0].status == "passed"


def test_synonyms_extend_phrases(normalizer):
    item = ChecklistItem(
        id="price",
        title="Цена",
        weight=1,
        phrases=("назвать цену",),
        synonyms={"цена": ("стоимость",)},
    )
    dialogue = make_dialogue(("manager", "Могу назвать стоимость прямо сейчас"))
    assert (
        score_call(dialogue, Checklist(items=(item,)), THRESHOLDS, normalizer).items[0].status
        == "passed"
    )
    global_only = Checklist(
        items=(ChecklistItem(id="price", title="Цена", weight=1, phrases=("назвать цену",)),),
        synonyms={"цена": ("стоимость",)},
    )
    assert score_call(dialogue, global_only, THRESHOLDS, normalizer).items[0].status == "passed"


def test_closed_questions_are_not_open(normalizer):
    item = ChecklistItem(
        id="need",
        title="Потребность",
        weight=1,
        rule="open_questions",
        phrases=("какой", "что"),
    )
    closed = make_dialogue(("manager", "Вам нужен ламинат? Вы знаете, какой хотите?"))
    open_q = make_dialogue(("manager", "Скажите, какой ламинат вы хотите?"))
    statement = make_dialogue(("manager", "Какой хороший день."))
    checklist = Checklist(items=(item,))
    assert score_call(closed, checklist, THRESHOLDS, normalizer).items[0].status == "failed"
    assert score_call(open_q, checklist, THRESHOLDS, normalizer).items[0].status == "passed"
    assert score_call(statement, checklist, THRESHOLDS, normalizer).items[0].status == "failed"


def test_no_applicable_items_scores_zero(normalizer):
    item = ChecklistItem(
        id="o", title="Возражение", weight=1, phrases=("скидка",), trigger=("дорого",)
    )
    score = score_call(make_dialogue(("client", "Хорошо")), Checklist(items=(item,)), THRESHOLDS)
    assert score.items[0].status == "na"
    assert score.score == 0
    assert score.loss_item_id is None


@pytest.mark.parametrize(
    ("value", "grade"),
    [(100, "good"), (80, "good"), (79.9, "medium"), (50, "medium"), (49.9, "bad"), (0, "bad")],
)
def test_grade_for_thresholds(value, grade):
    assert grade_for(value, THRESHOLDS) == grade


def test_grade_for_custom_thresholds():
    assert grade_for(70, Thresholds(good=70, medium=30)) == "good"
    assert grade_for(29, Thresholds(good=70, medium=30)) == "bad"


# --- отрицания, следующий шаг, уточнения клиента ---------------------------------


def objection_status(default_checklist, normalizer, *lines):
    score = score_call(make_dialogue(*lines), default_checklist, THRESHOLDS, normalizer)
    return items_by_id(score)["objection"].status


@pytest.mark.parametrize("reply", ["Скидок у нас нет.", "Это не бесплатно.", "Гарантии нет."])
def test_negated_objection_reply_is_not_handling(default_checklist, normalizer, reply):
    status = objection_status(
        default_checklist, normalizer, ("client", "Это дорого."), ("manager", reply)
    )
    assert status == "failed"


def test_affirmative_objection_reply_still_counts(default_checklist, normalizer):
    status = objection_status(
        default_checklist,
        normalizer,
        ("client", "Это дорого."),
        ("manager", "Могу дать скидку десять процентов."),
    )
    assert status == "passed"


def next_step_status(default_checklist, normalizer, text):
    dialogue = make_dialogue(("client", "Понятно."), ("manager", text))
    return items_by_id(score_call(dialogue, default_checklist, THRESHOLDS, normalizer))[
        "next_step"
    ].status


@pytest.mark.parametrize(
    "text", ["Ну подумайте, перезвоните нам.", "Хорошо, договорились, до свидания."]
)
def test_vague_next_step_fails(default_checklist, normalizer, text):
    assert next_step_status(default_checklist, normalizer, text) == "failed"


@pytest.mark.parametrize(
    "text",
    [
        "Давайте я перезвоню вам в четверг в 11.",
        "Отправлю счёт сегодня до вечера.",
        "Встречаемся на объекте в субботу в 10.",
    ],
)
def test_concrete_next_step_passes(default_checklist, normalizer, text):
    assert next_step_status(default_checklist, normalizer, text) == "passed"


@pytest.mark.parametrize(
    "question", ["Есть что-то дешевле в другом цвете?", "Я не уверен, какая площадь."]
)
def test_clarifying_client_remark_is_not_objection(default_checklist, normalizer, question):
    status = objection_status(
        default_checklist, normalizer, ("client", question), ("manager", "Сейчас посмотрю.")
    )
    assert status == "na"


@pytest.mark.parametrize("objection", ["Это дорого.", "Я подумаю.", "У конкурентов дешевле."])
def test_real_objections_still_trigger(default_checklist, normalizer, objection):
    status = objection_status(
        default_checklist, normalizer, ("client", objection), ("manager", "Хорошо.")
    )
    assert status == "failed"


def test_every_objection_must_be_handled(default_checklist, normalizer):
    status = objection_status(
        default_checklist,
        normalizer,
        ("client", "Дорого."),
        ("manager", "Есть рассрочка без переплаты."),
        ("client", "Всё равно у конкурентов дешевле."),
        ("manager", "Ну, как знаете."),
    )
    assert status == "failed"


def test_negation_in_next_clause_does_not_cancel_handling(default_checklist, normalizer):
    status = objection_status(
        default_checklist,
        normalizer,
        ("client", "Дорого."),
        ("manager", "Есть рассрочка, переплат нет."),
    )
    assert status == "passed"


def test_negation_in_next_clause_does_not_cancel_trigger(default_checklist, normalizer):
    status = objection_status(
        default_checklist, normalizer, ("client", "Дорого, денег нет."), ("manager", "Ясно.")
    )
    assert status == "failed"


def test_negation_in_previous_clause_does_not_cancel_farewell(default_checklist, normalizer):
    dialogue = make_dialogue(
        ("client", "Спасибо."), ("manager", "Вопросов больше нет, до свидания.")
    )
    farewell = items_by_id(score_call(dialogue, default_checklist, THRESHOLDS, normalizer))[
        "farewell"
    ]
    assert farewell.status == "passed"
