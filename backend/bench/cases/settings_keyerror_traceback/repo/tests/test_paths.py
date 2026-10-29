from settings.paths import data_dir


def test_explicit_data_dir_wins():
    assert data_dir({"data_dir": "/srv/app"}) == "/srv/app"
