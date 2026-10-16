"""SVG-графики панели: строятся на сервере, без JS-библиотек. Подписи по-русски.

Цвета берутся из CSS-переменных страницы (`--accent`, `--good`, `--bad`…), поэтому графики
совпадают со стилем каркаса. Размеры заданы через `viewBox`: график масштабируется по ширине.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from markupsafe import Markup

from callscope.stats import FunnelStep, WeekPoint

_STYLE = (
    "<style>"
    ".c-grid{stroke:var(--line);stroke-width:1}"
    ".c-good{stroke:var(--good);stroke-width:1;stroke-dasharray:5 4}"
    ".c-axis{fill:var(--muted);font-size:12px}"
    ".c-good-t{fill:var(--good);font-size:12px;font-weight:600}"
    ".c-line{fill:none;stroke:var(--accent);stroke-width:2.5;stroke-linejoin:round;"
    "stroke-linecap:round}"
    ".c-dot{fill:var(--card);stroke:var(--accent);stroke-width:2.5}"
    ".c-val{fill:var(--ink);font-size:12px;font-weight:700;text-anchor:middle}"
    ".c-empty{fill:none;stroke:#cbd5e1;stroke-width:1.5}"
    ".c-lab{fill:var(--ink);font-size:13px}"
    ".c-lab-top{fill:var(--bad);font-size:13px;font-weight:700}"
    ".c-track{fill:#f3f4f6}"
    ".c-failed{fill:#dc2626}"
    ".c-lost{fill:#f59e0b}"
    ".c-num{fill:var(--ink);font-size:12px}"
    ".c-top-bg{fill:var(--bad-bg)}"
    "</style>"
)


def _week_label(point: WeekPoint) -> str:
    return f"{point.week_start:%d.%m}"


def _fmt(value: float) -> str:
    return f"{value:.0f}"


def line_chart(points: Sequence[WeekPoint], good_threshold: float = 80) -> Markup:
    """Средний балл по неделям. Неделя без звонков — разрыв линии и серый кружок на нуле."""
    width, height = 760, 290
    left, right, top, bottom = 44, 84, 24, 48
    plot_w, plot_h = width - left - right, height - top - bottom

    def x_at(index: int) -> float:
        if len(points) == 1:
            return left + plot_w / 2
        return left + plot_w * index / (len(points) - 1)

    def y_at(value: float) -> float:
        return top + plot_h * (1 - max(0.0, min(100.0, value)) / 100)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" width="100%" '
        'aria-label="Средний балл по неделям" xmlns="http://www.w3.org/2000/svg">',
        _STYLE,
    ]
    for level in (0, 50, 100):
        y = y_at(level)
        parts.append(f'<line class="c-grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" '
                     f'y2="{y:.1f}"/>')
        parts.append(f'<text class="c-axis" x="{left - 8}" y="{y + 4:.1f}" '
                     f'text-anchor="end">{level}</text>')
    gy = y_at(good_threshold)
    parts.append(f'<line class="c-good" x1="{left}" x2="{width - right}" y1="{gy:.1f}" '
                 f'y2="{gy:.1f}"/>')
    parts.append(f'<text class="c-good-t" x="{width - right + 8}" y="{gy - 2:.1f}">хорошо</text>')
    parts.append(f'<text class="c-good-t" x="{width - right + 8}" y="{gy + 13:.1f}">'
                 f"от {_fmt(good_threshold)}</text>")

    step = max(1, -(-len(points) // 12))  # подписи недель — не чаще, чем помещается
    run: list[tuple[float, float]] = []

    def flush() -> None:
        if len(run) > 1:
            coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in run)
            parts.append(f'<polyline class="c-line" points="{coords}"/>')
        run.clear()

    for index, point in enumerate(points):
        x = x_at(index)
        if index % step == 0 or index == len(points) - 1:
            parts.append(f'<text class="c-axis" x="{x:.1f}" y="{height - 24}" '
                         f'text-anchor="middle">{_week_label(point)}</text>')
        if point.avg_score is None:
            flush()
            parts.append(
                f'<circle class="c-empty" cx="{x:.1f}" cy="{y_at(0):.1f}" r="4">'
                f"<title>Неделя с {point.week_start:%d.%m.%Y}: звонков нет</title></circle>"
            )
            continue
        run.append((x, y_at(point.avg_score)))
    flush()
    for index, point in enumerate(points):
        if point.avg_score is None:
            continue
        x, y = x_at(index), y_at(point.avg_score)
        tip = (f"Неделя с {point.week_start:%d.%m.%Y}: средний балл "
               f"{_fmt(point.avg_score)}, звонков {point.calls}")
        parts.append(f'<circle class="c-dot" cx="{x:.1f}" cy="{y:.1f}" r="5">'
                     f"<title>{escape(tip)}</title></circle>")
        if len(points) <= 14:
            parts.append(f'<text class="c-val" x="{x:.1f}" y="{y - 12:.1f}">'
                         f"{_fmt(point.avg_score)}</text>")
    parts.append(f'<text class="c-axis" x="{left}" y="{height - 4}">'
                 "Недели (дата — понедельник)</text>")
    parts.append("</svg>")
    return Markup("".join(parts))


def _wrap(text: str, width: int = 32, lines: int = 2) -> list[str]:
    words, out, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width or not current:
            current = candidate
        else:
            out.append(current)
            current = word
    out.append(current)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1][: width - 1].rstrip() + "…"
    return out


def funnel_chart(steps: Sequence[FunnelStep], total: int, top_item_id: str | None) -> Markup:
    """Воронка: у каждого пункта две полосы — «провалено» и «потеряно» (доля от всех звонков)."""
    row_h, label_w, bar_x_gap, num_w = 56, 270, 12, 84
    width = 760
    bar_x = label_w + bar_x_gap
    bar_w = width - bar_x - num_w
    height = row_h * len(steps) + 4
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" width="100%" '
        'aria-label="Воронка по пунктам чек-листа" xmlns="http://www.w3.org/2000/svg">',
        _STYLE,
    ]
    for index, step in enumerate(steps):
        y = index * row_h
        is_top = step.item_id == top_item_id
        if is_top:
            parts.append(f'<rect class="c-top-bg" x="0" y="{y + 2}" width="{width}" '
                         f'height="{row_h - 4}" rx="8"/>')
        lines = _wrap(f"{index + 1}. {step.title}")
        first = y + 33 - (len(lines) - 1) * 8
        for n, line in enumerate(lines):
            parts.append(
                f'<text class="{"c-lab-top" if is_top else "c-lab"}" x="10" '
                f'y="{first + n * 16:.1f}">{escape(line)}</text>'
            )
        for n, (cls, value, name) in enumerate(
            (("c-failed", step.failed, "провалено"), ("c-lost", step.lost, "потеряно"))
        ):
            by = y + 11 + n * 17
            share = value / total if total else 0.0
            parts.append(f'<rect class="c-track" x="{bar_x}" y="{by}" width="{bar_w}" '
                         'height="12" rx="3"/>')
            if value:
                parts.append(
                    f'<rect class="{cls}" x="{bar_x}" y="{by}" '
                    f'width="{max(3.0, bar_w * share):.1f}" height="12" rx="3">'
                    f"<title>{name.capitalize()}: {value} из {total} "
                    f"({share * 100:.0f}%)</title></rect>"
                )
            parts.append(f'<text class="c-num" x="{bar_x + bar_w + 8}" y="{by + 10}">'
                         f"{value} из {total}</text>")
    parts.append("</svg>")
    return Markup("".join(parts))
