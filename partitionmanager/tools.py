"""
Tools for working with iterators. Helpers.
"""

from itertools import tee


def pairwise(iterable):
    """iterable -> (s0,s1), (s1,s2), (s2, s3), ... (s_n-1, s_n)."""
    a, b = tee(iterable)
    next(b, None)
    return zip(a, b)


def iter_show_end(iterable):
    """iterable -> (s0, false), (s1, false), ... (s_n, true)."""
    it = iter(iterable)
    prev = next(it)
    for val in it:
        yield prev, False
        prev = val
    yield prev, True
