"""Identity, dependence and physical-distance acceptance tests for Stage B."""
from pathlib import Path
import sys

import numpy as np
import pytest
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sps.contract import JobError
from sps.validation import build_groups, indices_for, make_folds, split_calibration


def samples(n=24):
    ids = ["S%03d" % i for i in range(n)]
    coordinates = np.column_stack([np.arange(n) * 100.0, np.zeros(n)])
    return ids, coordinates, np.array(ids, dtype=object)


def config(method="random", **kwargs):
    return {"method": method, "n_splits": 3, "inner_splits": 3, "seed": 42,
            "repeats": 1, "block_size": 200.0, "buffer_distance": 0.0, **kwargs}


def test_connected_components_crossed_relations_and_duplicate_locations():
    ids = ["a", "b", "c", "d", "e", "f"]
    props = [{"profile": "p1", "parcel": "x", "farm": "A"},
             {"profile": "p1", "parcel": "y", "farm": "B"},
             {"profile": "p2", "parcel": "y", "farm": "C"},
             {"profile": "p3", "parcel": "z", "farm": "D"},
             {"profile": "p4", "parcel": "v", "farm": "E"},
             {"profile": "p5", "parcel": "w", "farm": "C"}]
    xy = np.array([[0, 0], [1, 1], [2, 2], [3, 3], [3, 3], [5, 5]])
    groups = build_groups(ids, props, xy, ["profile", "parcel", "farm"])
    assert groups[0] == groups[1] == groups[2] == groups[5]
    assert groups[3] == groups[4] != groups[0]
    order = np.array([5, 2, 3, 0, 4, 1])
    permuted = build_groups([ids[i] for i in order], [props[i] for i in order],
                            xy[order], ["farm", "parcel", "profile"])
    assert dict(zip(ids, groups)) == dict(zip([ids[i] for i in order], permuted))


def test_missing_group_is_rejected_and_field_names_are_distinct():
    with pytest.raises(JobError, match="profil"):
        build_groups(["a", "b"], [{"profil": "x"}, {"profil": None}], [[0, 0], [1, 1]], ["profil"])
    groups = build_groups(["a", "b"], [{"p": "x", "q": "y"}, {"p": "z", "q": "x"}],
                          [[0, 0], [1, 1]], ["p", "q"])
    assert groups[0] != groups[1]


def test_numeric_group_labels_do_not_split_integer_and_float_equivalents():
    ids, xy, _ = samples(24)
    groups = [value for i in range(12) for value in (i, float(i))]
    folds = make_folds(ids, xy, groups, config(), crs="EPSG:32637")
    for fold in folds:
        for i in range(0, 24, 2):
            assert (ids[i] in fold["test_ids"]) == (ids[i + 1] in fold["test_ids"])


def test_indices_preserve_subset_order_and_reject_ambiguous_ids():
    assert indices_for(["c", "a", "b"], ["b", "c"]).tolist() == [2, 0]
    for ids, subset in [(["a", "a"], ["a"]), (["a"], ["b"]), (["a"], ["a", "a"])]:
        with pytest.raises(JobError):
            indices_for(ids, subset)


@pytest.mark.parametrize("method", ["random", "group", "repeated", "spatial_block", "buffered"])
def test_folds_are_order_invariant_cover_each_repeat_and_preserve_groups(method):
    ids, xy, _ = samples(48)
    xy[:, 0] = np.arange(48) // 2 * 100.0
    groups = np.array(["g%d" % (i // 2) for i in range(48)], dtype=object)
    cfg = config(method, repeats=3 if method == "repeated" else 1,
                 buffer_distance=20 if method == "buffered" else 0)
    folds = make_folds(ids, xy, groups, cfg, crs="EPSG:32637")
    order = np.random.default_rng(19).permutation(len(ids))
    again = make_folds([ids[i] for i in order], xy[order], groups[order], cfg, crs="EPSG:32637")
    assert folds == again
    by_id = dict(zip(ids, groups))
    for repeat in range(cfg["repeats"]):
        held_out = [i for f in folds if f["repeat"] == repeat for i in f["test_ids"]]
        assert sorted(held_out) == sorted(ids)
    for fold in folds:
        assert not ({by_id[i] for i in fold["train_ids"]} & {by_id[i] for i in fold["test_ids"]})
        assert len({by_id[i] for i in fold["train_ids"]}) >= 2


def test_repeated_splits_actually_change_and_seed_is_recorded():
    ids, xy, groups = samples()
    folds = make_folds(ids, xy, groups, config("repeated", repeats=3), crs="EPSG:32637")
    partitions = {tuple(tuple(f["test_ids"]) for f in folds if f["repeat"] == r) for r in range(3)}
    assert len(partitions) > 1
    assert all(f["diagnostics"]["seed"] == 42 for f in folds)


def test_independent_ids_stay_locked_and_group_cut_or_unknown_id_fails():
    ids, xy, groups = samples()
    cfg = config(independent_ids=ids[-3:])
    folds = make_folds(ids, xy, groups, cfg, crs="EPSG:32637")
    assert all(not (set(ids[-3:]) & set(f[role])) for f in folds for role in ["train_ids", "test_ids"])
    groups[-1] = groups[0]
    with pytest.raises(JobError) as exc:
        make_folds(ids, xy, groups, cfg, crs="EPSG:32637")
    assert exc.value.code == "INDEPENDENT_GROUP_LEAKAGE"
    with pytest.raises(JobError):
        make_folds(ids, xy, groups, config(independent_ids=["absent"]), crs="EPSG:32637")


def test_inner_folds_use_inner_count_and_subset_remapping():
    ids, xy, groups = samples()
    cfg = config(n_splits=5, inner_splits=3, independent_ids=ids[-2:])
    outer = make_folds(ids, xy, groups, cfg, crs="EPSG:32637")
    idx = indices_for(ids, outer[0]["train_ids"])[::-1]
    inner = make_folds([ids[i] for i in idx], xy[idx], groups[idx], cfg, crs="EPSG:32637", inner=True)
    assert len(inner) == 3
    assert set(i for f in inner for i in f["test_ids"]) == set(outer[0]["train_ids"])


def test_spatial_group_crossing_blocks_is_rejected():
    ids, xy, groups = samples()
    groups[3] = groups[0]
    with pytest.raises(JobError) as exc:
        make_folds(ids, xy, groups, config("spatial_block", block_size=200), crs="EPSG:32637")
    assert exc.value.code == "GROUP_BLOCK_CONFLICT"


def test_buffer_drops_whole_group_and_verifies_all_distances():
    ids, xy, groups = samples(60)
    groups[1] = groups[0]
    cfg = config("buffered", block_size=500, buffer_distance=150)
    folds = make_folds(ids, xy, groups, cfg, crs="EPSG:32637")
    assert any(f["excluded_ids"] for f in folds)
    for fold in folds:
        train = xy[indices_for(ids, fold["train_ids"])]
        test = xy[indices_for(ids, fold["test_ids"])]
        actual = np.sqrt(((train[:, None] - test[None, :]) ** 2).sum(axis=2)).min()
        assert actual >= 150
        assert actual == pytest.approx(fold["diagnostics"]["minimum_train_test_distance_m"])
        assert (ids[0] in fold["train_ids"]) == (ids[1] in fold["train_ids"])


def test_geographic_requires_explicit_projection_and_uses_metre_distance():
    ids, metres, groups = samples(60)
    metres += [500000, 4200000]
    transform = Transformer.from_crs(32637, 4326, always_xy=True)
    geographic = np.column_stack(transform.transform(metres[:, 0], metres[:, 1]))
    cfg = config("buffered", block_size=500, buffer_distance=150)
    with pytest.raises(JobError) as exc:
        make_folds(ids, geographic, groups, cfg, crs="EPSG:4326")
    assert exc.value.code == "METRIC_CRS_REQUIRED"
    cfg["metric_crs"] = "EPSG:32637"
    folds = make_folds(ids, geographic, groups, cfg, crs="EPSG:4326")
    for fold in folds:
        train = metres[indices_for(ids, fold["train_ids"])]
        test = metres[indices_for(ids, fold["test_ids"])]
        nearest = np.sqrt(((train[:, None] - test[None, :]) ** 2).sum(axis=2)).min()
        assert nearest >= 150
        assert nearest == pytest.approx(fold["diagnostics"]["minimum_train_test_distance_m"], abs=1e-6)
    cfg["metric_crs"] = "EPSG:4326"
    with pytest.raises(JobError):
        make_folds(ids, geographic, groups, cfg, crs="EPSG:4326")


def test_projected_feet_are_converted_to_metres():
    ids, xy, groups = samples(60)
    xy += [3000000, 10000000]
    cfg = config("buffered", block_size=150, buffer_distance=45)
    folds = make_folds(ids, xy, groups, cfg, crs="EPSG:2277")
    for fold in folds:
        train = xy[indices_for(ids, fold["train_ids"])]
        test = xy[indices_for(ids, fold["test_ids"])]
        feet = np.sqrt(((train[:, None] - test[None, :]) ** 2).sum(axis=2)).min()
        assert fold["diagnostics"]["minimum_train_test_distance_m"] == pytest.approx(feet * 1200 / 3937)


def test_insufficient_groups_and_buffer_survivors_fail_explicitly():
    ids, xy, groups = samples(6)
    with pytest.raises(JobError) as exc:
        make_folds(ids, xy, np.array(["a"] * 3 + ["b"] * 3), config(), crs="EPSG:32637")
    assert exc.value.code == "INSUFFICIENT_GROUPS"
    with pytest.raises(JobError) as exc:
        make_folds(ids, xy, groups, config("buffered", block_size=100, buffer_distance=10000), crs="EPSG:32637")
    assert exc.value.code == "INSUFFICIENT_TRAIN_GROUPS"


def custom_config(ids):
    folds = []
    for fold in range(3):
        test = ids[fold * 4:(fold + 1) * 4]
        folds.append({"repeat": 0, "fold": fold, "train_ids": sorted(set(ids) - set(test)), "test_ids": test})
    return config("custom", custom_folds=folds, inner_method="group")


def test_custom_folds_require_exact_coverage_unique_roles_and_groups():
    from copy import deepcopy
    ids, xy, groups = samples(12)
    cfg = custom_config(ids)
    folds = make_folds(ids, xy, groups, cfg, crs="EPSG:32637")
    assert len(folds) == 3
    assert len(make_folds(ids, xy, groups, cfg, crs="EPSG:32637", inner=True)) == 3
    broken = deepcopy(cfg)
    broken["custom_folds"][0]["test_ids"].append(ids[4])
    with pytest.raises(JobError):
        make_folds(ids, xy, groups, broken, crs="EPSG:32637")
    broken = deepcopy(cfg)
    broken["custom_folds"][0]["test_ids"].pop()
    with pytest.raises(JobError) as exc:
        make_folds(ids, xy, groups, broken, crs="EPSG:32637")
    assert exc.value.code == "CUSTOM_COVERAGE"
    groups[5] = groups[0]
    with pytest.raises(JobError) as exc:
        make_folds(ids, xy, groups, cfg, crs="EPSG:32637")
    assert exc.value.code == "FOLD_GROUP_LEAKAGE"


def test_custom_buffer_is_checked_without_silently_editing_train_ids():
    ids, xy, groups = samples(12)
    cfg = custom_config(ids)
    cfg["buffer_distance"] = 150
    with pytest.raises(JobError) as exc:
        make_folds(ids, xy, groups, cfg, crs="EPSG:32637")
    assert exc.value.code == "BUFFER_VIOLATION"
    for fold in cfg["custom_folds"]:
        train = indices_for(ids, fold["train_ids"])
        test = indices_for(ids, fold["test_ids"])
        distance = np.sqrt(((xy[train, None] - xy[test][None, :]) ** 2).sum(axis=2)).min(axis=1)
        fold["train_ids"] = [ids[i] for i, d in zip(train, distance) if d >= 150]
    folds = make_folds(ids, xy, groups, cfg, crs="EPSG:32637")
    assert all(f["diagnostics"]["minimum_train_test_distance_m"] >= 150 for f in folds)


def test_custom_requires_inner_design_and_rejects_partial_excluded_groups():
    ids, xy, groups = samples(12)
    cfg = custom_config(ids)
    del cfg["inner_method"]
    with pytest.raises(JobError) as exc:
        make_folds(ids, xy, groups, cfg, crs="EPSG:32637")
    assert exc.value.code == "INNER_METHOD"
    cfg = custom_config(ids)
    groups[5] = groups[4]
    cfg["custom_folds"][0]["train_ids"].remove(ids[5])
    with pytest.raises(JobError) as exc:
        make_folds(ids, xy, groups, cfg, crs="EPSG:32637")
    assert exc.value.code == "FOLD_GROUP_LEAKAGE"


def test_requested_minimum_training_groups_is_enforced():
    ids, xy, groups = samples(12)
    with pytest.raises(JobError) as exc:
        make_folds(ids, xy, groups, config(min_train_groups=10), crs="EPSG:32637")
    assert exc.value.code == "INSUFFICIENT_TRAIN_GROUPS"


def test_independent_buffer_violation_is_rejected():
    ids, xy, groups = samples(24)
    with pytest.raises(JobError) as exc:
        make_folds(ids, xy, groups, config(independent_ids=[ids[-1]], buffer_distance=150), crs="EPSG:32637")
    assert exc.value.code == "INDEPENDENT_BUFFER_VIOLATION"


@pytest.mark.parametrize("method", ["random", "group", "repeated", "spatial_block", "buffered", "custom"])
def test_calibration_is_order_invariant_group_safe_and_buffer_safe(method):
    ids, xy, groups = samples(100)
    groups = np.array(["g%d" % (i // 2) for i in range(100)])
    cfg = config(method, block_size=1000, buffer_distance=150 if method == "buffered" else 0,
                 inner_method="group")
    dev, cal = split_calibration(ids, xy, groups, cfg, 0.2, crs="EPSG:32637", seed=71)
    order = np.arange(100)[::-1]
    again = split_calibration([ids[i] for i in order], xy[order], groups[order], cfg, 0.2, crs="EPSG:32637", seed=71)
    assert (dev, cal) == again
    assert dev and cal and not set(dev) & set(cal)
    assert not set(groups[indices_for(ids, dev)]) & set(groups[indices_for(ids, cal)])
    if method == "buffered":
        distance = np.sqrt(((xy[indices_for(ids, dev), None] - xy[indices_for(ids, cal)][None, :]) ** 2).sum(axis=2)).min()
        assert distance >= 150


def test_calibration_excludes_independent_and_rejects_too_few_groups():
    ids, xy, groups = samples(24)
    dev, cal = split_calibration(ids, xy, groups, config(independent_ids=ids[-2:]), 0.25,
                                 crs="EPSG:32637", seed=2)
    assert not (set(ids[-2:]) & set(dev + cal))
    with pytest.raises(JobError):
        split_calibration(ids[:3], xy[:3], groups[:3], config(), 0.5, crs="EPSG:32637", seed=2)
    with pytest.raises(JobError):
        split_calibration(ids, xy, groups, config(), 1, crs="EPSG:32637", seed=2)


@pytest.mark.parametrize("changes", [{"n_splits": True}, {"seed": -1}, {"method": "knndm"},
                                     {"buffer_distance": -1}, {"block_size": 0}])
def test_invalid_settings_fail_closed(changes):
    ids, xy, groups = samples()
    with pytest.raises(JobError):
        make_folds(ids, xy, groups, config(**changes), crs="EPSG:32637")
