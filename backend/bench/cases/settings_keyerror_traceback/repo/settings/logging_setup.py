"""Logging configuration driven by settings."""

import logging


def configure(cfg):
    level = logging.DEBUG if cfg.get("verbose") else logging.INFO
    logging.basicConfig(level=level)
    return level
