from scheduler.cron import next_run


class Runner:
    def __init__(self):
        self.jobs = {}

    def schedule_job(self, name, expr):
        self.jobs[name] = expr

    def run_due(self, now):
        due = []
        for name, expr in self.jobs.items():
            if next_run(expr, now) <= now:
                due.append(name)
        return due
