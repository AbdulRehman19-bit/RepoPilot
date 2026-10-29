from scheduler.runner import Runner


def test_runs_due_jobs():
    runner = Runner()
    runner.schedule_job("a", "*/15")
    assert runner.run_due(30) == ["a"]
