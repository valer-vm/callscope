"""Самодостаточный HTML-отчёт."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from callscope.aggregate import Summary
from callscope.report_common import (
    GRADE_LABEL,
    ROLE_LABEL,
    STATUS_LABEL,
    fmt_datetime,
    fmt_time,
    titles,
)
from callscope.scoring import grade_for

TEMPLATES_DIR = Path(__file__).parent / "templates"


def render_html(summary: Summary) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(default=True),
    )
    env.filters["clock"] = fmt_time
    env.filters["when"] = fmt_datetime
    template = env.get_template("report.html.j2")
    return template.render(
        s=summary,
        titles=titles(summary),
        grade_label=GRADE_LABEL,
        status_label=STATUS_LABEL,
        role_label=ROLE_LABEL,
        grade_of=lambda score: _grade(summary, score),
        max_loss=max((n for _, n in summary.loss_distribution), default=0),
        max_fail=max((n for _, n in summary.failure_ranking), default=0),
    )


def _grade(summary: Summary, score: float) -> str:
    return grade_for(score, summary.thresholds)


def write_html(summary: Summary, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(summary), encoding="utf-8")
