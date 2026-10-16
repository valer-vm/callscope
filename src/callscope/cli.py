"""Заглушка CLI: настоящую команду analyze пишет таск 05."""

import typer

app = typer.Typer(help="CallScope — анализ звонков по чек-листу скрипта продаж.")


@app.command()
def analyze(path: str) -> None:
    """Проанализировать папку с записями звонков."""
    typer.echo("Команда analyze ещё не реализована.")
    raise typer.Exit(code=1)
