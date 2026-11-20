# This file was modified on 11/18/2026 with the assistance of Claude Code in VS Code
# (Claude Opus 5.5, Anthropic, 2026). It corrects the docstring's list of runtime
# dependencies: the pipeline uses numpy only since the camera was removed.
"""PELT: the optimal split of a series into blocks of constant mean.

Killick, Fearnhead & Eckley (2012), "Optimal detection of changepoints with a
linear computational cost", JASA 107(500). Minimises

    Σ over blocks of Σ (y − the block's mean)²  +  penalty × (number of changes)

exactly, pruning split points that can never be optimal again. Written here in
numpy — the pipeline's only runtime dependency — and checked against the
`ruptures` package (Pelt, model "l2") in
tests/pipeline/test_classify.py, which agrees breakpoint for breakpoint.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def pelt(y: NDArray[np.float64], penalty: float, min_size: int) -> list[int]:
    """The end of every block (exclusive), the last being len(y) — the same
    convention as ruptures' predict(). Every block has at least `min_size`
    values; a series shorter than 2 × min_size is one block."""
    n = len(y)
    if n == 0:
        return []
    if n < 2 * min_size:
        return [n]
    s1 = np.concatenate(([0.0], np.cumsum(y)))
    s2 = np.concatenate(([0.0], np.cumsum(y * y)))

    def cost(starts: NDArray[np.intp], end: int) -> NDArray[np.float64]:
        length = end - starts
        total = s1[end] - s1[starts]
        return np.asarray((s2[end] - s2[starts]) - total * total / length, dtype=np.float64)

    best = np.full(n + 1, np.inf)
    best[0] = -penalty
    previous = np.zeros(n + 1, dtype=np.intp)
    candidates = np.array([0], dtype=np.intp)
    for end in range(min_size, n + 1):
        usable = candidates[end - candidates >= min_size]
        values = best[usable] + cost(usable, end) + penalty
        pick = int(np.argmin(values))
        best[end] = values[pick]
        previous[end] = usable[pick]
        # Prune: a start that is already worse than the best by more than the
        # penalty can never be optimal later (the L2 cost is additive, K = 0).
        keep = np.concatenate((usable[values - penalty <= best[end]],
                               candidates[end - candidates < min_size]))
        new = end - min_size + 1
        if new >= min_size:
            keep = np.append(keep, new)
        candidates = np.unique(keep)
    ends: list[int] = []
    at = n
    while at > 0:
        ends.append(at)
        at = int(previous[at])
    return sorted(ends)
