from __future__ import annotations

import os
import pwd
from pathlib import Path

from danmaku_backend.settings import (
    DOWNLOAD_DIR,
    REPORT_ARCHIVE_DIR,
    REPORT_DIR,
    STATE_DIR,
    STATIC_DIR,
    SUBTITLE_DIR,
    TEMPLATE_DIR,
)


def ensure_directories() -> None:
    for directory in (DOWNLOAD_DIR, SUBTITLE_DIR, REPORT_DIR, REPORT_ARCHIVE_DIR, STATIC_DIR, TEMPLATE_DIR, STATE_DIR):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        try:
            if os.geteuid() == 0:
                user = pwd.getpwnam("www")
                os.chown(path, user.pw_uid, user.pw_gid)
            os.chmod(path, 0o755)
        except Exception:
            # Directory creation should not prevent local imports/tests. Runtime logs catch route failures.
            pass
