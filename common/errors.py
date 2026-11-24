"""Ошибки загрузки и структуры источников. Python 3.8+."""

class SourceError(RuntimeError):
    """Источник недоступен или ответ не соответствует ожидаемой структуре."""


class HttpSourceError(SourceError):
    """HTTP/сетевая ошибка с кодом ответа для выбора резервного маршрута."""

    def __init__(self, message, status_code=None, retry_not_before=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_not_before = retry_not_before
