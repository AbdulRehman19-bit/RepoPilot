import pytest
from scheduler.cron import next_run


def test_step_expression():
    assert next_run("*/15", 14) == 15


def test_zero_step_is_rejected_with_a_clear_error():
    with pytest.raises(ValueError):
        next_run("*/0", 10)
