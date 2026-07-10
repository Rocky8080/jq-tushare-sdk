import re
import traceback
from datetime import datetime
from pathlib import Path


_SENSITIVE_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<prefix>['\"]?(?:password|passwd|token|secret|api[_-]?key|authorization)['\"]?\s*[:=]\s*)
    (?:
        (?P<quote>['\"])(?P<quoted>(?:\\.|(?!(?P=quote)).)*)(?P=quote)
        | (?P<bare>[^,'\"}\]\s]+)
    )
    """
)
_CREDENTIAL_URL = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:/\s]*:)([^@/\s]+)(@)")


def _redact_sensitive_text(value) -> str:
    text = str(value)
    text = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote') or ''}******{match.group('quote') or ''}",
        text,
    )
    return _CREDENTIAL_URL.sub(r"\1******\3", text)


class RuntimeLogger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def info(self, message, *args, **kwargs):
        self._write("INFO", message, *args, exc_info=kwargs.get("exc_info", False))

    def warning(self, message, *args, **kwargs):
        self._write("WARNING", message, *args, exc_info=kwargs.get("exc_info", False))

    def error(self, message, *args, **kwargs):
        self._write("ERROR", message, *args, exc_info=kwargs.get("exc_info", False))

    def _write(self, level: str, message, *args, exc_info=False):
        text = str(message)
        if args:
            text = text % args
        text = _redact_sensitive_text(text)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {level} {text}\n")
            if exc_info:
                if exc_info is True:
                    trace_text = traceback.format_exc()
                else:
                    trace_text = "".join(traceback.format_exception(*exc_info))
                if trace_text and trace_text != "NoneType: None\n":
                    handle.write(_redact_sensitive_text(trace_text))
