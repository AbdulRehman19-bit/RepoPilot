"""Command line entry point."""

import sys

from settings.config import get_setting, load_settings
from settings.paths import data_dir


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = load_settings({"timeout": 10} if "--fast" in argv else {})
    timeout = get_setting(cfg, "timeout")
    retries = get_setting(cfg, "retries")
    root = data_dir(cfg)
    proxy = get_setting(cfg, "proxy", default=None)
    return {"timeout": timeout, "retries": retries, "root": root, "proxy": proxy}
