"""Protocol checks for the licensed, out-of-domain Elliptic benchmark."""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.eval.elliptic_benchmark import choose_threshold, graph_features, split_masks


def test_temporal_masks_are_disjoint_and_unknown_is_not_negative() -> None:
    steps = np.array([1, 1, 34, 35, 41, 42, 49, 49], dtype=np.int8)
    labels = np.array([0, 1, -1, 0, 1, 0, 1, -1], dtype=np.int8)
    masks = split_masks(steps, labels)
    assert [int(mask.sum()) for mask in masks.values()] == [2, 2, 2]
    assert not any(mask[2] or mask[7] for mask in masks.values())
    assert not (masks["train"] & masks["validation"]).any()
    assert not (masks["validation"] & masks["test"]).any()


def test_topology_features_do_not_use_class_labels() -> None:
    ids = np.array([10, 20, 30], dtype=np.int64)
    edges = pd.DataFrame({"txId1": [10, 20], "txId2": [20, 30]})
    values = graph_features(ids, edges)
    assert values.shape == (3, 5)
    assert values[1, 4] == 1  # Only the middle transaction has both directions.
    assert values[0, 4] == 0


def test_threshold_uses_given_validation_rows() -> None:
    labels = np.array([0, 1, 0, 1], dtype=np.int8)
    probabilities = np.array([0.1, 0.9, 0.2, 0.8])
    assert 0.2 < choose_threshold(labels, probabilities) <= 0.8
