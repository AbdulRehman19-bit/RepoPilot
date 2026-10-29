def next_run(expr, now):
    """Minute (since midnight) at or after `now` matching a fixed minute or '*/N'."""
    if expr.startswith("*/"):
        step = int(expr[2:])
        return now + (step - now % step) % step
    return int(expr)
