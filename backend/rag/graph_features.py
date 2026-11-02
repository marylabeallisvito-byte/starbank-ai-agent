"""Label-free topology/flow features. Account identifiers are alignment keys only."""
from collections import defaultdict
from math import log1p

import numpy as np

NAMES = ["in_count", "out_count", "unique_sources", "unique_targets", "in_sum", "out_sum", "in_max", "out_max", "in_mean", "out_mean", "in_std", "out_std", "flow_balance", "in_repeat_ratio", "out_repeat_ratio", "neighbor_degree_mean", "neighbor_degree_max"]


def extract(graph):
    incoming, outgoing = defaultdict(list), defaultdict(list)
    neighbors = defaultdict(set)
    ids = sorted(n["id"] for n in graph["nodes"])
    for e in graph["edges"]:
        if e["type"] != "TRANSFER": continue
        amount = float(e["properties"]["amount"])
        incoming[e["target"]].append((e["source"], amount)); outgoing[e["source"]].append((e["target"], amount))
        neighbors[e["target"]].add(e["source"]); neighbors[e["source"]].add(e["target"])
    degree = {n: len(incoming[n]) + len(outgoing[n]) for n in ids}
    rows = []
    for n in ids:
        ins, outs = incoming[n], outgoing[n]
        iv, ov = [v for _, v in ins], [v for _, v in outs]
        unique_in, unique_out = len({p for p, _ in ins}), len({p for p, _ in outs})
        ns = [degree.get(p, 0) for p in neighbors[n]]
        row = [len(iv), len(ov), unique_in, unique_out, sum(iv), sum(ov), max(iv, default=0), max(ov, default=0), np.mean(iv) if iv else 0, np.mean(ov) if ov else 0, np.std(iv) if iv else 0, np.std(ov) if ov else 0,
               min(sum(iv), sum(ov)) / max(sum(iv), sum(ov), 1), 1 - unique_in / max(len(iv), 1), 1 - unique_out / max(len(ov), 1), np.mean(ns) if ns else 0, max(ns, default=0)]
        rows.append([log1p(max(0, float(v))) if i < 12 or i >= 15 else float(v) for i, v in enumerate(row)])
    return ids, np.asarray(rows, dtype=float).reshape((-1, len(NAMES)))
