"""Join verified historical outcome statistics without inventing paired trials.

Input records carry provenance and an explicit comparability decision. Numeric
verification is a prerequisite, not inferred from the fact a PDF table exists.
"""
import math
from statistics import correlation

from .schema import require


def join(summary: dict, history: dict) -> dict:
    require(history.get("schema_version") == "coevalchain_verified_outcomes_v1", "history schema")
    index = {}
    for row in history["records"]:
        key = (row["model"], row["benchmark"], row["method"])
        require(row["method"] in ("centralized", "decentralized") and key not in index, "duplicate/method ambiguity")
        require(row.get("verified_against_source") is True and row.get("source") and row.get("source_sha256"), "unverified historical value")
        require(all(isinstance(row[k], (int, float)) and math.isfinite(row[k]) for k in ("mean", "std"))
                and row["std"] >= 0 and type(row["n_runs"]) is int and row["n_runs"] >= 2, "invalid historical statistics")
        require(type(row["comparable_for_association"]) is bool and bool(row["scope_note"]), "comparison decision required")
        index[key] = row
    table = []
    for row in summary["primary_question_weighted"]:
        table.append({"model": row["model"], "benchmark": row["benchmark"], "AS": row["G"], "NS": row["M"],
            "scoreable": row["scoreable"], "total": row["total"],
            "historical": {method: index.get((row["model"], row["benchmark"], method))
                           for method in ("centralized", "decentralized")}})
    associations = []
    for benchmark in sorted({r["benchmark"] for r in table}):
        for method in ("centralized", "decentralized"):
            for metric in ("AS", "NS"):
                rows = [r for r in table if r["benchmark"] == benchmark and r[metric]["mean"] is not None
                        and r["historical"][method] and r["historical"][method]["comparable_for_association"]]
                x = [r[metric]["mean"] for r in rows]
                y = [r["historical"][method]["std"] for r in rows]
                value = correlation(x, y) if len(x) >= 3 and len(set(x)) > 1 and len(set(y)) > 1 else None
                associations.append({"benchmark": benchmark, "method": method, "metric": metric,
                    "n_model_benchmark_cells": len(rows), "pearson_with_historical_std": value,
                    "exploratory_only": True})
    return {"table": table, "within_benchmark_associations": associations,
            "note": "Independent B1 measurements, not historical centralized/decentralized trajectories. No pooled 30-cell test or causal effect."}
