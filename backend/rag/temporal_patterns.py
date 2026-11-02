"""Bounded causal cycles and rolling fan-in; consumes only transaction features."""
from collections import defaultdict
from datetime import datetime
from math import isfinite


def detect_temporal_patterns(graph, min_amount=200, window_hours=168, min_sources=3, tolerance=.2, max_cycle_length=6):
    outgoing, incoming = defaultdict(list), defaultdict(list)
    for edge in graph.get("edges", []):
        if edge.get("type") != "TRANSFER" or edge["source"] == edge["target"]:
            continue
        props = edge.get("properties", {})
        try:
            amount = float(props["amount"])
            timestamp = datetime.fromisoformat(str(props["date"]).replace("Z", "+00:00")).timestamp()
            if not isfinite(amount) or amount < min_amount: continue
        except (ValueError, KeyError, TypeError):
            continue
        row = (edge["source"], edge["target"], amount, timestamp, edge["id"])
        outgoing[row[0]].append(row); incoming[row[1]].append(row)
    seconds = window_hours * 3600
    findings, seen = [], set()
    # All participants and actual source edges are retained. No outgoing leg is
    # required for fan-in: money can remain at the collection account.
    for hub, rows in incoming.items():
        rows.sort(key=lambda row: row[3])
        end = 0
        for start, row in enumerate(rows):
            end = max(end, start)
            while end < len(rows) and rows[end][3] - row[3] <= seconds: end += 1
            window = rows[start:end]
            sources = {r[0] for r in window}
            if len(sources) >= min_sources:
                key = (hub, tuple(sorted(sources)))
                if key not in seen:
                    seen.add(key)
                    findings.append({"code": "TEMPORAL_FAN_IN", "node_ids": sorted(sources | {hub}), "source_ids": sorted({r[4] for r in window}), "hub": hub})
    # Causal traversal, 3–6 unique accounts, comparable amounts, at most a bounded
    # number of expansions. Exhaustion is explicit, never presented as full coverage.
    expansions, cap = 0, 2_000_000
    def walk(first, path, used, first_time, last_time, lo, hi):
        nonlocal expansions
        for row in outgoing.get(path[-1], []):
            expansions += 1
            if expansions > cap: return
            source, target, value, when, identifier = row
            if when < last_time or when - first_time > seconds: continue
            low, high = min(lo, value), max(hi, value)
            if (high - low) / low > tolerance: continue
            if target == first and len(path) >= 3:
                ids = tuple(sorted(used + [identifier]))
                if ids not in seen:
                    seen.add(ids)
                    findings.append({"code": "TEMPORAL_CYCLE", "node_ids": sorted(path), "source_ids": list(ids), "span_hours": (when - first_time) / 3600})
            elif target not in path and len(path) < max_cycle_length:
                walk(first, path + [target], used + [identifier], first_time, when, low, high)
    for source, rows in outgoing.items():
        if expansions > cap: break
        for row in rows:
            if expansions > cap: break
            walk(source, [source, row[1]], [row[4]], row[3], row[3], row[2], row[2])
    return {"findings": findings, "complete": expansions <= cap, "expansions": expansions, "expansion_limit": cap}
