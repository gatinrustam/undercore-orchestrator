"""Private file credentials; never render their contents in diagnostics."""

import os


def read_secret(path):
    import stat

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError("Secret must be a private regular file")
        return os.read(fd, 4097).strip()
    finally:
        os.close(fd)
