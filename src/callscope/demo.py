"""Демо-база для показа панели: 5 вымышленных менеджеров «СтройДом», ~150 звонков за 8 недель.

Расшифровки собираются из вариативных реплик (темы: ламинат, плитка, ванная, потолки, окна,
двери, тёплый пол). Закономерности получаются не из подкрученных чисел, а из того, какие
этапы разговора менеджер «проводит»: оценки считает настоящий `score_call` по встроенному
чек-листу. 5 звонков — с настоящим аудио из examples/calls (их расшифровки лежат
в examples/demo/*.json, модель не нужна). Остальные получают короткую беззвучную дорожку.

Закономерности:
- petrova почти никогда не выявляет потребность → это самый частый провал на обзоре;
- ivanov за 8 недель вырастает с «слабого» до «отличного» результата;
- sidorov — отстающий в рейтинге;
- kuznetsova — стабильно сильная, morozov — середняк.
"""

from __future__ import annotations

import io
import json
import random
import shutil
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from callscope.checklist import load_checklist
from callscope.config import load_config
from callscope.model import ItemResult, RawTranscript, Segment
from callscope.pipeline import analyze_raw
from callscope.storage import DB_FILE_NAME, Storage
from callscope.textnorm import Normalizer

DEFAULT_SEED = 2026
WEEKS = 8
GENERATED_CALLS = 145
# examples/ лежит рядом с пакетом только в клоне репозитория (в установленном пакете аудио нет).
EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"

STAGES = ("greeting", "need", "solution", "objection", "next_step", "farewell")


@dataclass(frozen=True)
class DemoSummary:
    calls: int
    audio_calls: int
    corrected: int
    managers: tuple[str, ...]


@dataclass(frozen=True)
class _Manager:
    login: str  # так менеджер попадает в имя файла и в панель
    name: str  # так он представляется в разговоре
    female: bool
    # прогресс 0..1 (начало периода → сегодня) → вероятность провести этап
    odds: Callable[[float], dict[str, float]]


def _flat(**odds: float) -> Callable[[float], dict[str, float]]:
    return lambda _t: dict(odds)


def _growing(start: dict[str, float], end: dict[str, float]) -> Callable[[float], dict[str, float]]:
    # Первые недели результат ещё низкий, рост идёт заметно и в окне «4 недели».
    def odds(t: float) -> dict[str, float]:
        grown = min(max((t - 0.2) / 0.8, 0.0), 1.0)
        return {k: start[k] + (end[k] - start[k]) * grown for k in start}

    return odds


MANAGERS = (
    _Manager(
        "ivanov", "Алексей", False,
        _growing(
            dict(greeting=0.8, need=0.35, solution=0.45, objection=0.1, next_step=0.3,
                 farewell=0.8),
            dict(greeting=1.0, need=0.97, solution=0.97, objection=0.9, next_step=0.95,
                 farewell=1.0),
        ),
    ),
    _Manager(
        "petrova", "Ольга", True,
        _flat(greeting=0.95, need=0.02, solution=0.9, objection=0.85, next_step=0.9,
              farewell=0.95),
    ),
    _Manager(
        "sidorov", "Сергей", False,
        _flat(greeting=0.7, need=0.6, solution=0.4, objection=0.1, next_step=0.2,
              farewell=0.55),
    ),
    _Manager(
        "kuznetsova", "Марина", True,
        _flat(greeting=0.98, need=0.92, solution=0.95, objection=0.9, next_step=0.93,
              farewell=0.97),
    ),
    _Manager(
        "morozov", "Дмитрий", False,
        _flat(greeting=0.9, need=0.75, solution=0.8, objection=0.6, next_step=0.7,
              farewell=0.85),
    ),
)

# Какие записи из examples/calls какому менеджеру принадлежат (имя файла — как в examples).
AUDIO_STEMS = (
    "ivanov_2026-10-12_1530",
    "ivanov_2026-10-15_1145",
    "petrova_2026-10-13_1015",
    "petrova_2026-10-16_0930",
    "sidorov_2026-10-14_1400",
)
_AUDIO_REFERENCE_DAY = date(2026, 10, 16)


# --- тексты ---------------------------------------------------------------------------
# Осторожно с формулировками: слабые реплики не должны случайно содержать фраз чек-листа
# («предлагаю», «замер» в утверждении, «бесплатн…», «до свидания»…), а вопросы с «как/что/
# какой/сколько/где/когда…» в начале — только там, где менеджер действительно выявляет потребность.

_HELLO = ("Здравствуйте", "Добрый день", "Добрый день", "Здравствуйте")
_HELLO_MORNING = ("Доброе утро", "Здравствуйте")
_HELLO_EVENING = ("Добрый вечер", "Здравствуйте")
_INTRO = (
    "Меня зовут {name}, компания СтройДом. Слушаю вас.",
    "Компания СтройДом, меня зовут {name}. Слушаю вас.",
    "Вы позвонили в компанию СтройДом, меня зовут {name}. Слушаю вас.",
    "Это компания СтройДом, на связи {name}. Слушаю вас внимательно.",
)
_NO_GREETING = (
    "Да, слушаю вас.",
    "Алло, слушаю.",
    "Да, отдел продаж, слушаю вас.",
    "Слушаю вас.",
    "Да, говорите.",
)
_CLIENT_HELLO = ("Здравствуйте.", "Добрый день.", "Алло, здравствуйте.", "Добрый день!")
_ACK = ("Понятно.", "Хорошо.", "Отлично.", "Ясно.", "Так, понятно.", "Принято.")

_OBJECTION_CLIENT = (
    "Ой, дороговато получается.",
    "Дороговато, если честно.",
    "Надо подумать, сумма немаленькая.",
    "У конкурентов дешевле, я смотрел.",
    "Слишком дорого для меня.",
    "Мне нужно посоветоваться с женой.",
    "Я пока не уверен, что готов на такую сумму.",
)
_OBJECTION_HANDLED = (
    "Понимаю вас. Давайте посчитаем вариант попроще, а на работы мы даём гарантию три года.",
    "Могу сделать рассрочку на шесть месяцев без переплаты, платёж в месяц получится небольшим.",
    "Сейчас на материал действует скидка десять процентов, так что итог выйдет ниже.",
    "Понимаю. Сравните сами: дешёвый вариант придётся менять, а наш прослужит много лет, "
    "это окупится.",
    "Есть бюджетный вариант, он дешевле примерно на пятую часть, и гарантия на него та же.",
    "Понимаю. У нас сертификат качества на весь материал, и вы сэкономите "
    "на монтаже, если взять комплектом.",
)
_OBJECTION_DROPPED = (
    "Ну, цена у нас такая.",
    "Материалы дорожают, цена такая.",
    "Дешевле у нас не будет.",
    "Это актуальная цена, другой нет.",
    "Ну, это наш прайс.",
)
_CLIENT_AFTER_HANDLED = (
    "Так, это уже интереснее.",
    "Ну хорошо, звучит разумнее.",
    "Ладно, с этим уже можно разговаривать.",
    "Понятно, это меняет дело.",
)
_CLIENT_AFTER_DROPPED = ("Понятно.", "Ясно.", "Ну ладно.", "Понятно, спасибо.")
_CLIENT_NO_OBJECTION = (
    "Нормально, меня устраивает.",
    "Хорошо, по деньгам понятно.",
    "Да, это в моём бюджете.",
    "Ну, терпимо.",
)

_EXTRAS = (
    ("А доставка у вас есть?", "Да, привезём на следующий день после оплаты, по городу от "
     "пятисот рублей."),
    ("Вы сами всё установите?", "Да, работают наши мастера, обычно это занимает один-два дня."),
    ("А по срокам что получается?", "Материал на складе, мастер сможет начать уже на этой "
     "неделе."),
    ("Можно ли оплатить частями?", "Да, у нас есть рассрочка на шесть месяцев."),
    ("А мусор после работ вывозите?", "Да, вывозим за собой, это входит в работу."),
    ("Фотографии работ у вас есть?", "Конечно, на сайте есть раздел с готовыми объектами."),
)

_NEXT_STEP = (
    ("Давайте запишу вас на замер. Мастер может приехать {day} в {hour}, вам удобно?",
     "Да, {day} подойдёт."),
    ("Не будем откладывать: запишу вас на замер {day}, мастер всё посмотрит на месте.",
     "Давайте, {day} я дома."),
    ("Я отправлю вам смету на почту сегодня до конца дня, а завтра я вам перезвоню.",
     "Хорошо, жду."),
    ("Оформим заказ прямо сейчас? Мне нужны только ваш адрес и телефон.",
     "Да, оформляйте."),
    ("Жду вас в нашем салоне {day}, оформим договор и зафиксируем цену.",
     "Хорошо, приеду."),
    ("Я вышлю вам смету в мессенджер, посмотрите и скажете.",
     "Давайте, пришлите."),
)
_NEXT_STEP_DAYS = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу",
                   "в субботу")
_NEXT_STEP_HOURS = ("десять утра", "одиннадцать", "двенадцать", "два часа дня", "четыре часа")
_NO_NEXT_STEP = (
    "Хорошо, если надумаете, звоните.",
    "Смотрите сами, номер у вас есть.",
    "Ладно, будем на связи.",
    "Хорошо, можете зайти на наш сайт, там всё есть.",
    "Решать вам, номер у вас есть.",
)
_CLIENT_AFTER_NO_NEXT = ("Хорошо, спасибо.", "Ага, понял.", "Ладно, ясно.")
_FAREWELL = (
    "Спасибо за звонок, всего доброго!",
    "Всего хорошего, до свидания!",
    "Хорошего дня, до свидания!",
    "Спасибо за обращение, хорошего вечера!",
    "Всего доброго, будем рады помочь!",
)
_CLIENT_BYE = ("До свидания.", "Спасибо, всего доброго.", "Всего хорошего.", "Спасибо, пока.")

# Тема: opens — запросы клиента; need_open — открытые вопросы менеджера и ответы;
# need_closed — «да/нет»-вопросы; offers/catalog — сильное и слабое предложение; prices.
_TOPICS = (
    {
        "opens": (
            "Хочу сделать ремонт на кухне, нужен ламинат.",
            "Подскажите по ламинату, нужно положить пол в гостиной.",
            "Мне нужен ламинат в детскую, хочу узнать цены.",
        ),
        "need_open": (
            ("Какая у вас площадь комнаты и что сейчас лежит на полу?",
             "Около четырнадцати метров, старый линолеум."),
            ("Как вы пользуетесь комнатой? Есть ли дети или животные?",
             "Да, у нас кошка и маленький ребёнок."),
            ("Какой цвет вам нравится, светлый или тёмный?", "Светлый, под дуб."),
            ("Когда вы хотите закончить ремонт?", "Лучше до конца месяца."),
            ("Что для вас важнее всего в покрытии?", "Чтобы не царапалось и легко мылось."),
        ),
        "need_closed": (
            ("Вы уже выбирали ламинат?", "Нет, пока только смотрю."),
            ("Вам нужен светлый ламинат?", "Наверное, да."),
            ("Вы живёте в квартире?", "Да, в квартире."),
        ),
        "offers": (
            "Тогда я предлагаю влагостойкий ламинат тридцать третьего класса. Он хорошо "
            "переносит воду и подходит под вашу задачу.",
            "Рекомендую ламинат с плотным замком и фаской, он выдержит и кошку, и ребёнка.",
            "Для вашего случая оптимальный вариант — износостойкий ламинат под дуб, "
            "лучше всего с подложкой.",
        ),
        "catalog": (
            "У нас есть ламинат тридцать второго и тридцать третьего класса, всё в каталоге.",
            "Ламината много, разные цвета, смотрите на сайте в каталоге.",
        ),
        "prices": (
            "Материал с укладкой выйдет примерно в тридцать восемь тысяч рублей.",
            "За квадратный метр с работой получится около двух с половиной тысяч.",
            "Весь комплект с подложкой и плинтусом — около сорока тысяч рублей.",
        ),
    },
    {
        "opens": (
            "Мне нужна плитка в ванную.",
            "Здравствуйте, хочу положить плитку в ванной комнате.",
            "Подскажите по плитке для санузла.",
        ),
        "need_open": (
            ("Какой размер у вашей ванной комнаты?", "Примерно четыре квадратных метра."),
            ("Что вы хотите: глянцевую плитку или матовую?", "Лучше матовую, не скользкую."),
            ("Где будет плитка, только на стенах или и на полу?", "И на стенах, и на полу."),
            ("Какой бюджет вы планируете?", "Тысяч шестьдесят на всё."),
            ("Когда вам нужно, чтобы всё было готово?", "Желательно через месяц."),
        ),
        "need_closed": (
            ("Вы уже выбирали плитку?", "Нет, пока не определился."),
            ("Вам нужна глянцевая?", "Наверное, ещё не знаю."),
            ("Вы делаете ремонт в новой квартире?", "Нет, в старой."),
        ),
        "offers": (
            "Тогда я предлагаю керамогранит с матовой поверхностью. Он не скользит и "
            "подойдёт под вашу задачу.",
            "Рекомендую плитку тридцать на шестьдесят, на малой площади она выглядит "
            "аккуратно.",
            "Советую взять плитку с запасом десять процентов, а на пол — "
            "нескользящую, так лучше всего.",
        ),
        "catalog": (
            "Плитка у нас разная, есть в каталоге, там все размеры.",
            "Выбор большой, смотрите каталог на сайте.",
        ),
        "prices": (
            "Плитка с укладкой на вашу площадь выйдет около пятидесяти пяти тысяч рублей.",
            "Материал стоит от девятисот рублей за метр, работа отдельно.",
            "Итого с затиркой и клеем получится порядка шестидесяти тысяч.",
        ),
    },
    {
        "opens": (
            "Хочу сделать ремонт ванной под ключ.",
            "Нужен ремонт ванной комнаты полностью, с сантехникой.",
            "Подскажите, вы делаете ванные под ключ?",
        ),
        "need_open": (
            ("Какая у вас сейчас ванная и что вы хотите изменить?",
             "Старая, хочу всё поменять, и трубы тоже."),
            ("Сколько человек пользуются ванной?", "Нас четверо."),
            ("Что вы хотите поставить: ванну или душевую кабину?", "Скорее душевую."),
            ("Какие у вас сроки и бюджет?", "Бюджет около двухсот тысяч, сроки гибкие."),
            ("Почему решили менять именно сейчас?", "Протекают трубы, дальше тянуть нельзя."),
        ),
        "need_closed": (
            ("Вы живёте в квартире?", "Да, на пятом этаже."),
            ("Вам нужна душевая кабина?", "Пока не знаю."),
            ("Вы уже обращались в другие фирмы?", "Нет, только в вашу."),
        ),
        "offers": (
            "Тогда я предлагаю ремонт под ключ: демонтаж, трубы, плитка и установка "
            "сантехники одной бригадой.",
            "Рекомендую душевую зону вместо ванны, она освободит место и вам подойдёт.",
            "Для вашего случая оптимальный вариант — скрытый монтаж труб и "
            "влагозащита пола, так лучше всего.",
        ),
        "catalog": (
            "Мы делаем ванные под ключ, примеры работ есть на сайте.",
            "Работы у нас разные, цены в прайсе на сайте.",
        ),
        "prices": (
            "Ремонт под ключ на вашу площадь обойдётся примерно в сто восемьдесят тысяч рублей.",
            "Работа без материалов выйдет около девяноста тысяч, материал отдельно.",
            "Всё вместе получится порядка двухсот тысяч рублей.",
        ),
    },
    {
        "opens": (
            "Интересуют натяжные потолки в квартиру.",
            "Хочу узнать про натяжные потолки в гостиную.",
            "Подскажите по натяжному потолку на кухню.",
        ),
        "need_open": (
            ("В какой комнате вы хотите потолок и какая там высота?",
             "В гостиной, высота обычная."),
            ("Какой цвет и фактуру вы хотите?", "Белый, матовый."),
            ("Сколько светильников вам нужно?", "Штук восемь, точечные."),
            ("Когда вы хотите сделать монтаж?", "Ближе к концу месяца."),
            ("Что сейчас на потолке?", "Побелка, местами трещины."),
        ),
        "need_closed": (
            ("Вам нужен белый потолок?", "Да, скорее всего."),
            ("Светильники будут?", "Возможно."),
            ("Вы уже смотрели потолки?", "Нет, пока не смотрел."),
        ),
        "offers": (
            "Тогда я рекомендую матовое полотно со встроенными светильниками. "
            "Оно смотрится аккуратно и подойдёт вам.",
            "Предлагаю тканевый потолок, он без швов и хорошо держит форму, лучше всего "
            "для гостиной.",
            "Советую двухуровневый вариант, он скроет трещины и подойдёт под вашу задачу.",
        ),
        "catalog": (
            "Потолки у нас разные, есть матовые и глянцевые, фото в каталоге.",
            "Всё есть, посмотрите каталог на сайте.",
        ),
        "prices": (
            "Полотно с монтажом выйдет примерно в двадцать шесть тысяч рублей.",
            "Метр потолка с работой стоит около семисот рублей, светильники отдельно.",
            "Вместе со светильниками получится тысяч тридцать пять.",
        ),
    },
    {
        "opens": (
            "Хочу заказать окна в квартиру.",
            "Нужно заменить окна в спальне и детской.",
            "Добрый день, интересуют пластиковые окна.",
        ),
        "need_open": (
            ("Сколько окон вам нужно и какие у них размеры?", "Три окна, обычные."),
            ("Что для вас главное, тепло или тишина?", "Тишина, у нас рядом дорога."),
            ("Какой профиль и цвет вы хотите?", "Белый, обычный."),
            ("Когда нужно закончить установку?", "До холодов."),
            ("Где находится квартира, на каком этаже?", "На третьем этаже."),
        ),
        "need_closed": (
            ("Вам нужны пластиковые окна?", "Да, пластиковые."),
            ("Окна нужны в спальню?", "Да, и в детскую."),
            ("Вы уже делали замеры?", "Нет, ещё не делали."),
        ),
        "offers": (
            "Тогда я рекомендую двухкамерный профиль со стеклопакетом. Он хорошо глушит "
            "шум и подойдёт для вашего дома.",
            "Предлагаю профиль с тремя камерами и шумоизоляцией, лучше всего под вашу "
            "задачу.",
            "Советую энергосберегающий стеклопакет, он держит тепло зимой.",
        ),
        "catalog": (
            "Окна у нас разные, профили есть в каталоге на сайте.",
            "Профилей несколько, смотрите страницу с окнами.",
        ),
        "prices": (
            "Три окна выйдут примерно в сорок пять тысяч рублей с установкой.",
            "Одно окно стандартного размера стоит около четырнадцати тысяч.",
            "С откосами и подоконниками получится около пятидесяти тысяч рублей.",
        ),
    },
    {
        "opens": (
            "Мне нужны межкомнатные двери.",
            "Хочу поменять двери в квартире, три штуки.",
            "Подскажите по дверям в спальню и кабинет.",
        ),
        "need_open": (
            ("Сколько дверей вам нужно и какой у них размер?", "Три двери, стандартные."),
            ("Какой стиль вам нравится?", "Современный, светлый."),
            ("Для чего вам двери, спальня или кабинет?", "Спальня и кабинет."),
            ("Когда вы планируете установку?", "В течение месяца."),
            ("Что сейчас стоит в проёмах?", "Старые советские двери."),
        ),
        "need_closed": (
            ("Вы выбирали двери?", "Только смотрел."),
            ("Вам нужны белые двери?", "Не знаю пока."),
            ("Двери нужны с установкой?", "Да, с установкой."),
        ),
        "offers": (
            "Тогда я предлагаю двери из экошпона с тихим замком. Они вам подойдут и "
            "долго прослужат.",
            "Рекомендую светлые двери с матовым стеклом для кабинета, лучше всего "
            "смотрятся.",
            "Советую брать комплектом с наличниками, под вашу задачу это самое удобное.",
        ),
        "catalog": (
            "Двери у нас разные, фото в каталоге на сайте.",
            "Выбор большой, смотрите раздел «Двери».",
        ),
        "prices": (
            "Три двери с установкой выйдут около тридцати тысяч рублей.",
            "Одна дверь с монтажом стоит от девяти тысяч.",
            "С фурнитурой и наличниками получится тысяч тридцать пять.",
        ),
    },
    {
        "opens": (
            "Хочу тёплый пол в спальню.",
            "Нужна консультация по тёплому полу под ламинат.",
            "Скажите, можно ли сделать тёплый пол в ванной?",
        ),
        "need_open": (
            ("Какая у вас комната по размеру и какое там покрытие?",
             "Двенадцать метров, будет ламинат."),
            ("Для чего вам тёплый пол, основное отопление или для комфорта?",
             "Для комфорта, чтобы ноги не мёрзли."),
            ("Как у вас сейчас устроено отопление?", "Обычные батареи."),
            ("Когда вы планируете делать ремонт?", "В ближайшие две недели."),
            ("Что уже сделано в комнате?", "Стяжка есть, больше ничего."),
        ),
        "need_closed": (
            ("Вам нужен электрический пол?", "Не знаю, наверное."),
            ("Вы уже делали стяжку?", "Да, давно."),
            ("Тёплый пол нужен во всей комнате?", "Вроде бы да."),
        ),
        "offers": (
            "Тогда я предлагаю электрический нагревательный мат под ламинат. Он вам "
            "подойдёт и ставится быстро.",
            "Рекомендую плёночный тёплый пол, под вашу задачу это лучший вариант.",
            "Советую кабельный пол с терморегулятором, так лучше всего экономить "
            "электричество.",
        ),
        "catalog": (
            "Тёплые полы бывают разные, электрические и водяные, всё есть на сайте.",
            "Варианты и цены есть в каталоге.",
        ),
        "prices": (
            "Мат с монтажом и терморегулятором выйдет примерно в двадцать две тысячи рублей.",
            "Квадратный метр пола с установкой стоит около полутора тысяч.",
            "На вашу комнату получится порядка восемнадцати тысяч рублей.",
        ),
    },
)


# --- сборка диалога -------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    greeting: bool
    need: str  # full | partial | none
    solution: bool
    objection_raised: bool
    objection_handled: bool
    next_step: bool
    farewell: bool


def _make_plan(rng: random.Random, odds: dict[str, float]) -> _Plan:
    need_full = rng.random() < odds["need"]
    return _Plan(
        greeting=rng.random() < odds["greeting"],
        need="full" if need_full else ("partial" if rng.random() < 0.4 else "none"),
        solution=rng.random() < odds["solution"],
        objection_raised=rng.random() < 0.5,
        objection_handled=rng.random() < odds["objection"],
        next_step=rng.random() < odds["next_step"],
        farewell=rng.random() < odds["farewell"],
    )


def _compose(
    rng: random.Random, manager: _Manager, plan: _Plan, hour: int
) -> list[tuple[str, str]]:
    """Реплики звонка: пары (manager|client, текст)."""
    topic = rng.choice(_TOPICS)
    lines: list[tuple[str, str]] = []

    def say(role: str, text: str) -> None:
        lines.append((role, text))

    if plan.greeting:
        hello = rng.choice(
            _HELLO_MORNING if hour < 11 else _HELLO_EVENING if hour >= 17 else _HELLO
        )
        say("manager", f"{hello}! {rng.choice(_INTRO).format(name=manager.name)}")
    else:
        say("manager", rng.choice(_NO_GREETING))
    say("client", f"{rng.choice(_CLIENT_HELLO)} {rng.choice(topic['opens'])}")

    open_pairs = list(topic["need_open"])
    closed_pairs = list(topic["need_closed"])
    rng.shuffle(open_pairs)
    rng.shuffle(closed_pairs)
    if plan.need == "full":
        asked = [*open_pairs[: rng.choice((2, 3))]]
    elif plan.need == "partial":
        asked = [open_pairs[0], closed_pairs[0]]
    else:
        asked = closed_pairs[: rng.choice((1, 2))]
    for question, answer in asked:
        lead = rng.choice(("", "", rng.choice(_ACK) + " "))
        say("manager", lead + question)
        say("client", answer)

    if plan.solution:
        say("manager", rng.choice(_ACK) + " " + rng.choice(topic["offers"]))
        say("client", rng.choice(("Понятно.", "Звучит неплохо.", "Интересно.", "Хорошо.")))
    else:
        say("manager", rng.choice(topic["catalog"]))
        say("client", rng.choice(("Понятно.", "Ага.", "Ну ладно.")))

    for question, answer in rng.sample(_EXTRAS, rng.choice((0, 1, 1, 2))):
        say("client", question)
        say("manager", answer)

    say("client", rng.choice(("А сколько это будет стоить?", "А по цене что получится?",
                              "Сколько это выйдет в итоге?")))
    say("manager", rng.choice(topic["prices"]))
    if plan.objection_raised:
        say("client", rng.choice(_OBJECTION_CLIENT))
        if plan.objection_handled:
            say("manager", rng.choice(_OBJECTION_HANDLED))
            say("client", rng.choice(_CLIENT_AFTER_HANDLED))
        else:
            say("manager", rng.choice(_OBJECTION_DROPPED))
            say("client", rng.choice(_CLIENT_AFTER_DROPPED))
    else:
        say("client", rng.choice(_CLIENT_NO_OBJECTION))

    if plan.next_step:
        text, reply = rng.choice(_NEXT_STEP)
        day = rng.choice(_NEXT_STEP_DAYS)
        say("manager", text.format(day=day, hour=rng.choice(_NEXT_STEP_HOURS)))
        say("client", reply.format(day=day))
    else:
        say("manager", rng.choice(_NO_NEXT_STEP))
        say("client", rng.choice(_CLIENT_AFTER_NO_NEXT))
    if plan.farewell:
        say("manager", rng.choice(_FAREWELL))
        say("client", rng.choice(_CLIENT_BYE))
    return lines


def _to_raw(lines: list[tuple[str, str]], rng: random.Random) -> RawTranscript:
    """Реплики → стерео-расшифровка: менеджер слева, клиент справа, время по числу слов."""
    clock = 0.4
    segments: list[Segment] = []
    for role, text in lines:
        length = max(1.4, len(text.split()) / 2.7)
        segments.append(
            Segment(
                round(clock, 2),
                round(clock + length, 2),
                text,
                "left" if role == "manager" else "right",
            )
        )
        clock += length + rng.uniform(0.3, 1.1)
    return RawTranscript(tuple(segments), "stereo", round(clock, 2))


def _silent_wav(index: int) -> bytes:
    """Короткая беззвучная дорожка (плеер не ломается); первые отсчёты уникальны."""
    buffer = io.BytesIO()
    samples = bytearray(b"\x80" * 8000)
    samples[0:4] = index.to_bytes(4, "big")
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(8000)
        handle.writeframes(bytes(samples))
    return buffer.getvalue()


# --- расписание -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Slot:
    manager: _Manager
    moment: datetime
    progress: float
    stem: str | None = None  # запись из examples/calls (иначе — сгенерированный звонок)


def _weekday_near(day: date, earliest: date) -> date:
    """Выходной → пятница перед ним (не раньше `earliest`, иначе понедельник после)."""
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day if day >= earliest else day + timedelta(days=3)


def _generated_slots(rng: random.Random, today: date) -> list[_Slot]:
    per_manager = [GENERATED_CALLS // len(MANAGERS)] * len(MANAGERS)
    for i in range(GENERATED_CALLS - sum(per_manager)):
        per_manager[i] += 1
    span = WEEKS * 7 - 1
    earliest = today - timedelta(days=span)
    taken: set[tuple[str, datetime]] = set()
    slots: list[_Slot] = []
    for manager, count in zip(MANAGERS, per_manager, strict=True):
        for i in range(count):
            back = round(span * (1 - (i + rng.random() * 0.9) / count))
            day = _weekday_near(today - timedelta(days=min(max(back, 0), span)), earliest)
            while True:
                moment = datetime.combine(
                    day, datetime.min.time()
                ) + timedelta(hours=rng.randint(9, 18), minutes=5 * rng.randint(0, 11))
                if (manager.login, moment) not in taken:
                    taken.add((manager.login, moment))
                    break
            progress = 1 - (today - day).days / span
            slots.append(_Slot(manager, moment, progress))
    return slots


def _audio_slots(today: date) -> list[_Slot]:
    """Записи из examples/calls переносятся на недели вперёд, чтобы попасть в «последние»."""
    shift = timedelta(days=((today - _AUDIO_REFERENCE_DAY).days // 7) * 7)
    by_login = {m.login: m for m in MANAGERS}
    slots = []
    for stem in AUDIO_STEMS:
        login, day_text, hour_text = stem.split("_")
        moment = datetime.strptime(f"{day_text} {hour_text}", "%Y-%m-%d %H%M") + shift
        slots.append(_Slot(by_login[login], moment, 1.0, stem))
    return slots


# --- публичный интерфейс ----------------------------------------------------------------

_CORRECTIONS = (
    # (менеджер, пункт, статус, комментарий)
    ("petrova", "need", "passed",
     "Проверено вручную: вопросы о задаче клиент получил, хоть и в закрытой форме, "
     "засчитываю."),
    ("ivanov", "objection", None,
     "Хороший пример отработки возражения — разобрать на планёрке."),
)


def demo_exists(data_dir: Path) -> bool:
    return (Path(data_dir) / DB_FILE_NAME).is_file()


def clear_demo(data_dir: Path) -> None:
    """Удаляет только то, что создаёт `build_demo` (папка может быть общей)."""
    folder = Path(data_dir)
    for name in (DB_FILE_NAME, f"{DB_FILE_NAME}-wal", f"{DB_FILE_NAME}-shm", ".secret_key",
                 "checklist.yaml", "callscope.yaml"):
        (folder / name).unlink(missing_ok=True)
    for name in ("audio", "cache"):
        shutil.rmtree(folder / name, ignore_errors=True)


def build_demo(
    data_dir: Path,
    *,
    today: date | None = None,
    seed: int = DEFAULT_SEED,
    examples_dir: Path | None = None,
) -> DemoSummary:
    """Создаёт демо-базу в `data_dir` (рабочая папка данных не затрагивается)."""
    today = today or date.today()
    examples = Path(examples_dir) if examples_dir is not None else EXAMPLES_DIR
    rng = random.Random(seed)

    storage = Storage(Path(data_dir))
    config = load_config(storage.config_path)
    checklist = load_checklist(storage.checklist_path)
    normalizer = Normalizer()

    slots = _generated_slots(rng, today)
    audio_ready = all(
        (examples / "calls" / f"{s}.m4a").is_file() and (examples / "demo" / f"{s}.json").is_file()
        for s in AUDIO_STEMS
    )
    if audio_ready:
        slots += _audio_slots(today)
    slots.sort(key=lambda s: (s.moment, s.manager.login))

    audio_calls = 0
    placed: dict[tuple[str, str, str], int] = {}  # (менеджер, пункт, статус) → id звонка
    for number, slot in enumerate(slots):
        file_name = f"{slot.manager.login}_{slot.moment:%Y-%m-%d_%H%M}"
        if slot.stem is not None:
            audio = examples / "calls" / f"{slot.stem}.m4a"
            raw = RawTranscript.from_json(
                json.loads((examples / "demo" / f"{slot.stem}.json").read_text("utf-8"))
            )
            record, _new = storage.add_upload(f"{file_name}.m4a", audio)
            audio_calls += 1
        else:
            plan = _make_plan(rng, slot.manager.odds(slot.progress))
            lines = _compose(rng, slot.manager, plan, slot.moment.hour)
            raw = _to_raw(lines, rng)
            record, _new = storage.add_upload(f"{file_name}.wav", _silent_wav(number))
        result = analyze_raw(
            record.file_name, raw, config, checklist, normalizer, manager_channel="left"
        )
        storage.save_result(record.id, raw, result, checklist, config.thresholds)
        _remember(placed, slot, record.id, result.score.items)

    corrected = 0
    for login, item_id, status, comment in _CORRECTIONS:
        call_id = _correction_target(placed, login, item_id, status)
        if call_id is not None:
            storage.set_correction(call_id, item_id, status, comment)  # type: ignore[arg-type]
            corrected += 1
    return DemoSummary(
        calls=len(slots),
        audio_calls=audio_calls,
        corrected=corrected,
        managers=tuple(m.login for m in MANAGERS),
    )


def _remember(
    placed: dict[tuple[str, str, str], int],
    slot: _Slot,
    call_id: int,
    items: tuple[ItemResult, ...],
) -> None:
    """Запоминает звонки из середины периода, подходящие для ручной правки."""
    if slot.stem is not None or not 0.3 < slot.progress < 0.8:
        return
    for item in items:
        placed.setdefault((slot.manager.login, item.item_id, item.status), call_id)


def _correction_target(
    placed: dict[tuple[str, str, str], int], login: str, item_id: str, status: str | None
) -> int | None:
    # Правка «failed → passed» — берём звонок, где пункт провален; комментарий — где выполнен.
    wanted = "failed" if status == "passed" else "passed"
    return placed.get((login, item_id, wanted))
