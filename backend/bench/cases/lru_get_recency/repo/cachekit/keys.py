"""Build cache keys from function arguments."""


def make_key(*args, **kwargs):
    return (args, tuple(sorted(kwargs.items())))
