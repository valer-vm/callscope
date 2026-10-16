"""Исключения CallScope. Текст сообщения — по-русски, его видит пользователь."""


class CallScopeError(Exception):
    """Базовая ошибка CallScope."""

    def __init__(self, message_ru: str) -> None:
        super().__init__(message_ru)
        self.message_ru = message_ru


class ConfigError(CallScopeError):
    """Ошибка в callscope.yaml или checklist.yaml."""


class AudioError(CallScopeError):
    """Файл записи не читается или не распознаётся."""
