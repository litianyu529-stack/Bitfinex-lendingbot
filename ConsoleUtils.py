import shutil


def get_terminal_size():
    size = shutil.get_terminal_size(fallback=(80, 25))
    return size.columns, size.lines
