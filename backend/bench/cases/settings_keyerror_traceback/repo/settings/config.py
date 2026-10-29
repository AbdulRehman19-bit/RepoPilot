"""Application settings loaded from a dict (usually parsed from a TOML file)."""

DEFAULTS = {"timeout": 30, "retries": 3, "verbose": False}


def load_settings(raw):
    merged = dict(DEFAULTS)
    merged.update(raw)
    return merged


def get_setting(settings, name, default=None):
    """Look up a setting, falling back to `default` when it is not configured."""
    return settings[name]
