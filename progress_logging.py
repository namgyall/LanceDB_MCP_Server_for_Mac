import re
import threading
from datetime import datetime
from pathlib import Path


_LOG_LOCK = threading.Lock()
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret|authorization)"
    r"(\s*[:=]\s*)([^\s,;&]+)"
)
_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|password|secret)=)[^&#\s]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+\S+")


def sanitize_log_message(message: str) -> str:
    sanitized = _SECRET_ASSIGNMENT.sub(r"\1\2[redacted]", str(message))
    sanitized = _SECRET_QUERY.sub(r"\1[redacted]", sanitized)
    sanitized = _BEARER_TOKEN.sub("Bearer [redacted]", sanitized)
    return sanitized.replace("\r", " ").replace("\n", " ")


def append_progress_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"{timestamp} {sanitize_log_message(message)}\n"
    with _LOG_LOCK:
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(line)
            log_file.flush()
