import os
import tempfile
import time


def atomic_write_text(path, text, encoding="utf-8"):
    """Write text without exposing readers to a partially written file."""
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or os.getcwd()
    os.makedirs(directory, exist_ok=True)

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            newline="",
            dir=directory,
            prefix=f".{os.path.basename(target)}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        # On Windows a reader that opened the destination without delete sharing
        # can make an otherwise atomic replacement fail briefly with WinError 5.
        # Dashboard polling is read-only, so retry the transient collision before
        # treating it as a runtime failure.
        for attempt in range(8):
            try:
                os.replace(temp_path, target)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(0.01 * (2**attempt), 0.25))
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
