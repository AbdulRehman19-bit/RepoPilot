"""Where the application keeps its files."""

import os


def data_dir(cfg):
    return cfg.get("data_dir") or os.path.join(os.path.expanduser("~"), ".app")
