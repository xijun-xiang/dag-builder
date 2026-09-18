"""Pure, dependency-free definitions. Missing values are never replaced by zero."""
import math
import random
from statistics import fmean, stdev


def pair(full, deleted):
    if not full or len(full) != len(deleted):
        raise ValueError("Same nonempty target required")
    if any(not math.isfinite(x) or x > 1e-6 for x in [*full, *deleted]):
        raise ValueError("Invalid target logprob")
    a, b = -fmean(full), -fmean(deleted)
    g = math.fsum(x - y for x, y in zip(full, deleted)) / len(full)
    return {"g": g, "v": max(-g, 0.0), "full_nll": a,
            "deleted_nll": b, "target_tokens": len(full)}


def trajectory(rows):
    return {"G": fmean(r["g"] for r in rows) if rows else None,
            "M": fmean(r["v"] for r in rows) if rows else None,
            "NLL": fmean(r["full_nll"] for r in rows) if rows else None,
            "targets": len(rows)}


def repeats(rows, expected):
    valid = [r["score"] for r in rows if r["status"] == "ok"]
    values = [r["g"] for r in valid]
    return {"planned": expected, "attempted": len(rows), "valid": len(valid),
            "complete": len(rows) == len(valid) == expected,
            "N": fmean(r["v"] for r in valid) if valid else None,
            "D": stdev(values) if len(values) >= 2 else None,
            "mean_g": fmean(values) if values else None,
            "std_full_nll": stdev(r["full_nll"] for r in valid) if len(valid) >= 2 else None,
            "std_deleted_nll": stdev(r["deleted_nll"] for r in valid) if len(valid) >= 2 else None}


def bootstrap(values, seed=2026091703, draws=5000):
    if not values:
        return {"n": 0, "mean": None, "ci95": None}
    rng = random.Random(seed)
    samples = sorted(fmean(rng.choices(values, k=len(values))) for _ in range(draws))
    return {"n": len(values), "mean": fmean(values), "mean_absolute": fmean(abs(x) for x in values),
            "positive": sum(v > 1e-10 for v in values),
            "negative": sum(v < -1e-10 for v in values),
            "ci95": [samples[int(.025 * (draws - 1))], samples[int(.975 * (draws - 1))]]}
