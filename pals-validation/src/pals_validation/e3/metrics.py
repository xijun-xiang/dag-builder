"""E3 trajectory aggregates; W is within-trajectory population dispersion."""

import math
from statistics import fmean

from ..metrics import pair


def summarize_trace(rows: list[dict]) -> dict:
    if any(not math.isfinite(row["g"]) or not math.isfinite(row["v"]) or
           not math.isfinite(row["full_nll"]) or not math.isfinite(row["deleted_nll"])
           for row in rows):
        raise ValueError("Non-finite step score")
    count = len(rows)
    if not count:
        return {"K": 0, "G": None, "M": None, "W": None, "NLL": None}
    g = [row["g"] for row in rows]
    mean = fmean(g)
    return {"K": count, "G": mean,
            "M": fmean(row["v"] for row in rows),
            "W": math.sqrt(math.fsum((v - mean) ** 2 for v in g) / count)
            if count >= 2 else None,
            "NLL": fmean(row["full_nll"] for row in rows)}


__all__ = ["pair", "summarize_trace"]
