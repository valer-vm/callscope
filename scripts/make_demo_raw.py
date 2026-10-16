"""Сырые расшифровки пяти тестовых записей для демо-базы (нужна модель whisper).

Результат — examples/demo/<имя записи>.json (формат RawTranscript.to_json); его коммитят,
поэтому `callscope demo` работает без модели. Запуск (один раз или после смены записей):

    uv run python scripts/make_demo_raw.py [--calls examples/calls] [--out examples/demo]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from callscope.asr import Transcriber
from callscope.cache import TranscriptCache
from callscope.config import load_config
from callscope.errors import CallScopeError
from callscope.pipeline import transcript_for

HERE = Path(__file__).resolve().parent
AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".m4a")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calls", type=Path, default=HERE.parent / "examples" / "calls")
    parser.add_argument("--out", type=Path, default=HERE.parent / "examples" / "demo")
    args = parser.parse_args()

    recordings = sorted(p for p in args.calls.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS)
    if not recordings:
        print(f"В папке {args.calls} нет записей.", file=sys.stderr)
        return 1
    config = load_config(None)
    transcriber = Transcriber(config.model_size, config.model_path)
    cache = TranscriptCache(args.calls / ".callscope-cache")
    args.out.mkdir(parents=True, exist_ok=True)
    for audio in recordings:
        try:
            raw, from_cache = transcript_for(audio, transcriber, cache, config)
        except CallScopeError as exc:
            print(f"{audio.name}: {exc.message_ru}", file=sys.stderr)
            return 2
        target = args.out / f"{audio.stem}.json"
        target.write_text(
            json.dumps(raw.to_json(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"{audio.name} -> {target} ({'кэш' if from_cache else 'расшифровано'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
