"""Small text files are replaced atomically; readers see an old or new whole file."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time

data_file_lock = threading.RLock()


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with data_file_lock:
        fd, temporary = tempfile.mkstemp(prefix='.atomic-', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            # Windows may briefly deny replacement while another reader has the file open.
            for attempt in range(20):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.01)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def atomic_write_json(path, value):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))
