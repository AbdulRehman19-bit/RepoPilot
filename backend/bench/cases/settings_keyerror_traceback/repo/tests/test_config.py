from settings.config import get_setting, load_settings


def test_defaults_are_applied():
    cfg = load_settings({})
    assert cfg["timeout"] == 30


def test_overrides_win():
    assert load_settings({"retries": 9})["retries"] == 9


def test_existing_setting_is_returned():
    assert get_setting({"timeout": 5}, "timeout") == 5


def test_missing_setting_uses_default():
    assert get_setting({}, "proxy", default="direct") == "direct"


def test_missing_setting_defaults_to_none():
    assert get_setting({}, "proxy") is None
