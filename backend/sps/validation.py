"""Stage B validation designs, expressed exclusively in stable observation IDs.

No target is accepted by this module. Group relations are connected components,
including exact duplicate locations. Spatial distances and block sizes are metres;
geographic inputs require an explicit projected distance CRS. A projected CRS does
not establish that distortion is acceptable for a particular study area.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import math

import numpy as np
from pyproj import CRS, Transformer
from scipy.spatial import cKDTree

from .contract import JobError


METHODS = {"random", "repeated", "group", "spatial_block", "buffered", "custom", "knndm", "temporal"}


def _ids(values, label="Sample"):
    result = list(values)
    if any(not isinstance(value, str) or not value.strip() for value in result):
        raise JobError("SAMPLE_ID", label + " IDs must be non-empty text.")
    if len(result) != len(set(result)):
        raise JobError("DUPLICATE_ID", label + " IDs are duplicated.")
    return result


def indices_for(ids, subset_ids):
    """Reindex a subset in the caller's requested ID order, never its row order."""
    source = _ids(ids)
    wanted = _ids(subset_ids, "Subset")
    lookup = {value: index for index, value in enumerate(source)}
    unknown = set(wanted) - set(lookup)
    if unknown:
        raise JobError("UNKNOWN_ID", "Unknown ID in subset: " + sorted(unknown)[0])
    return np.asarray([lookup[value] for value in wanted], dtype=np.int64)


def _coordinates(values, n):
    try:
        array = np.asarray(values, dtype=float)
    except (ValueError, TypeError) as exc:
        raise JobError("COORDINATES", "Coordinates must be numeric.") from exc
    if array.shape != (n, 2) or not np.isfinite(array).all():
        raise JobError("COORDINATES", "Two finite coordinates are required for each sample.")
    return array


def _group_token(value, field):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise JobError("MISSING_GROUP", "Missing value in group field: " + field)
    if isinstance(value, (np.integer, np.floating)):
        value = value.item()
    if isinstance(value, (dict, list, tuple)):
        raise JobError("GROUP_VALUE", "Group field must hold a single value: " + field)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise JobError("MISSING_GROUP", "Nonfinite value in group field: " + field)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return ("number", value)
    if not isinstance(value, (str, bool)):
        raise JobError("GROUP_VALUE", "Group field must be text or a number: " + field)
    return (type(value).__name__, value)


def build_groups(ids, props, coordinates, group_fields, *, crs=None):
    """Union profile/parcel/farm links and duplicate locations transitively.

    Returned labels are the smallest member ID with a fixed prefix. They are
    independent of row order and group-field order, rather than ordinal labels.
    A missing value in an explicitly selected grouping field is an error.
    """
    ids = _ids(ids)
    xy = _coordinates(coordinates, len(ids))
    props = list(props)
    fields = list(group_fields)
    if len(props) != len(ids) or any(not isinstance(row, dict) for row in props):
        raise JobError("GROUP_PROPERTIES", "Sample properties must have the same length as the IDs.")
    if any(not isinstance(field, str) or not field.strip() for field in fields) or len(fields) != len(set(fields)):
        raise JobError("GROUP_FIELDS", "Group fields must be non-empty, unique names.")
    parent = list(range(len(ids)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left, right = root(left), root(right)
        if left != right:
            parent[max(left, right)] = min(left, right)

    for field in fields:
        first = {}
        for i, row in enumerate(props):
            token = _group_token(row.get(field), field)
            if token in first:
                union(i, first[token])
            else:
                first[token] = i
    first = {}
    for i, point in enumerate(xy):
        key = tuple(point)
        if key in first:
            union(i, first[key])
        else:
            first[key] = i
    if crs is not None and len(first)>1:
        # Numerical projection round trips can perturb identical locations.
        # Ten micrometres is a numerical tolerance, not sampling accuracy.
        source=CRS.from_user_input(crs)
        locations=np.asarray(list(first));members_at_location=list(first.values())
        tx=Transformer.from_crs(source.to_3d(),'EPSG:4978',always_xy=True)
        xyz=np.column_stack(tx.transform(locations[:,0],locations[:,1],np.zeros(len(locations)),errcheck=True))
        if not np.isfinite(xyz).all():raise JobError('COORDINATES','Locations could not be transformed to a common metric space.')
        for left,right in cKDTree(xyz).query_pairs(1e-5):union(members_at_location[left],members_at_location[right])
    members = defaultdict(list)
    for i, sample_id in enumerate(ids):
        members[root(i)].append(sample_id)
    labels = {key: "group:" + min(values) for key, values in members.items()}
    return np.asarray([labels[root(i)] for i in range(len(ids))], dtype=object)


def _inputs(ids, coordinates, groups):
    ids = _ids(ids)
    xy = _coordinates(coordinates, len(ids))
    groups = list(groups)
    if len(groups) != len(ids):
        raise JobError("GROUP_LENGTH", "The number of groups does not match the number of samples.")
    # JSON is used only as a deterministic typed key, never to relabel IDs.
    keys = [json.dumps(_group_token(group, "groups"), ensure_ascii=True) for group in groups]
    coordinate_groups = {}
    for point, group in zip(xy, keys):
        location = tuple(point)
        if location in coordinate_groups and coordinate_groups[location] != group:
            raise JobError("GROUP_COORDINATE", "Samples at the same location cannot be in different groups.")
        coordinate_groups[location] = group
    # Sorting here makes every downstream assignment independent of input order.
    order = sorted(range(len(ids)), key=lambda i: ids[i])
    return ([ids[i] for i in order], xy[order], np.asarray([keys[i] for i in order], dtype=object))


def _positive_int(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise JobError("VALIDATION_PARAMETER", name + " must be an integer of at least " + str(minimum) + ".")
    return value


def _settings(config, inner=False):
    if not isinstance(config, dict):
        raise JobError("VALIDATION_PARAMETER", "Validation settings must be an object.")
    method = config.get("method", "random")
    if method not in METHODS:
        raise JobError("VALIDATION_METHOD", "Unsupported validation method: " + str(method))
    if method == "custom":
        inner_method = config.get("inner_method")
        if inner_method not in METHODS - {"custom"}:
            raise JobError("INNER_METHOD", "User-defined outer folds require a supported inner_method.")
        if inner:
            method = inner_method
    n_splits = _positive_int(config.get("inner_splits" if inner else "n_splits", 3 if inner else 5), "Number of folds", 2)
    repeats = _positive_int(config.get("repeats", 1), "Number of repeats") if method == "repeated" else 1
    seed = _positive_int(config.get("seed", 42), "Seed", 0)
    minimum = _positive_int(config.get("min_train_groups", 2), "Minimum training groups", 2)
    for name, default, positive in [("block_size", 1000.0, True), ("buffer_distance", 0.0, False)]:
        value = config.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (positive and value == 0):
            raise JobError("VALIDATION_PARAMETER", name + " is invalid.")
    buffer = float(config.get("buffer_distance", 0.0))
    if method == "buffered" and buffer <= 0:
        raise JobError("VALIDATION_PARAMETER", "Buffered validation requires buffer_distance greater than zero.")
    return method, n_splits, repeats, seed, minimum, float(config.get("block_size", 1000.0)), buffer


def _metric_coordinates(xy, crs, config, needed):
    if not needed:
        return None, None
    try:
        source = CRS.from_user_input(crs)
    except Exception as exc:
        raise JobError("METRIC_CRS", "A valid source coordinate reference system is required.") from exc
    destination = config.get("metric_crs")
    if destination is None and not source.is_projected:
        raise JobError("METRIC_CRS_REQUIRED", "For distances with geographic coordinates, select an explicit projected coordinate reference system.")
    try:
        target = CRS.from_user_input(destination) if destination is not None else source
        if not target.is_projected or len(target.axis_info) < 2:
            raise ValueError("not projected")
        factors = np.asarray([axis.unit_conversion_factor for axis in target.axis_info[:2]], dtype=float)
        if not np.isfinite(factors).all() or np.any(factors <= 0):
            raise ValueError("invalid units")
        transformed = np.column_stack(Transformer.from_crs(source, target, always_xy=True).transform(
            xy[:, 0], xy[:, 1], errcheck=True))
        metric = transformed * factors
        if not np.isfinite(metric).all():
            raise ValueError("nonfinite projection")
    except Exception as exc:
        raise JobError("METRIC_CRS", "The distance coordinate reference system must be a valid projected system with linear units.") from exc
    return metric, target.to_string()


def _nearest(left, right):
    if not len(left) or not len(right):
        return np.asarray([], dtype=float)
    return cKDTree(right).query(left, k=1, workers=1)[0]


def _development(ids, groups, metric, config, buffer, inner=False):
    independent = set(_ids(config.get("independent_ids", []), "Independent test"))
    unknown = independent - set(ids)
    if unknown and not inner:
        raise JobError("INDEPENDENT_ID", "Independent test ID not found in the data: " + sorted(unknown)[0])
    held = np.asarray([i for i, value in enumerate(ids) if value in independent], dtype=int)
    dev = np.asarray([i for i, value in enumerate(ids) if value not in independent], dtype=int)
    if set(groups[held]) & set(groups[dev]):
        raise JobError("INDEPENDENT_GROUP_LEAKAGE", "A group is split between the independent test and the development data.")
    if buffer > 0 and len(held) and len(dev) and np.any(_nearest(metric[dev], metric[held]) < buffer):
        raise JobError("INDEPENDENT_BUFFER_VIOLATION", "The independent test and development data do not satisfy the buffer distance.")
    return dev


def _units(indices, groups, metric, method, block_size):
    if method not in {"spatial_block", "buffered"}:
        result = defaultdict(list)
        for i in indices:
            result[groups[i]].append(int(i))
        return dict(result)
    blocks = np.floor(metric / block_size).astype(np.int64)
    group_blocks = {}
    result = defaultdict(list)
    for i in indices:
        block = tuple(blocks[i])
        if groups[i] in group_blocks and group_blocks[groups[i]] != block:
            raise JobError("GROUP_BLOCK_CONFLICT", "A group that must not be split extends into more than one spatial block. Change the block size or the group design.")
        group_blocks[groups[i]] = block
        result[block].append(int(i))
    return dict(result)


def _assigned_units(units, n_splits, rng):
    keys = sorted(units)
    if len(keys) < n_splits:
        raise JobError("INSUFFICIENT_GROUPS", "There are not enough independent groups or spatial blocks for the number of folds.")
    # Shuffle before the stable size sort to break equal-size ties reproducibly.
    shuffled = [keys[i] for i in rng.permutation(len(keys))]
    shuffled.sort(key=lambda key: -len(units[key]))
    loads = np.zeros(n_splits, dtype=int)
    allocated = [[] for _ in range(n_splits)]
    for key in shuffled:
        tied = np.flatnonzero(loads == loads.min())
        chosen = int(tied[int(rng.integers(len(tied)))])
        allocated[chosen].extend(units[key])
        loads[chosen] += len(units[key])
    return [np.asarray(sorted(values), dtype=int) for values in allocated]


def _buffer_training(train, test, groups, metric, buffer):
    if buffer <= 0 or not len(train):
        return train
    rejected = set(groups[train[_nearest(metric[train], metric[test]) < buffer]])
    return np.asarray([i for i in train if groups[i] not in rejected], dtype=int)


def _fold(ids, groups, metric, metric_crs, dev, train, test, repeat, fold, seed, method, minimum, buffer):
    train_set, test_set = set(train), set(test)
    if not test_set or train_set & test_set or not (train_set | test_set) <= set(dev):
        raise JobError("FOLD_ROLES", "Fold IDs are empty, overlap or lie outside the development data.")
    train_groups, test_groups = set(groups[train]), set(groups[test])
    if train_groups & test_groups:
        raise JobError("FOLD_GROUP_LEAKAGE", "The same group is split between training and test folds.")
    # Whole groups are indivisible even when custom folds omit other groups.
    for subset, selected_groups in [(train_set, train_groups), (test_set, test_groups)]:
        if any(i not in subset and groups[i] in selected_groups for i in dev):
            raise JobError("FOLD_GROUP_LEAKAGE", "A fold does not keep all observations of a group in the same role.")
    if len(train_groups) < minimum:
        raise JobError("INSUFFICIENT_TRAIN_GROUPS", "At least %d training groups are required after buffering or splitting." % minimum)
    nearest = float(_nearest(metric[train], metric[test]).min()) if metric is not None else None
    if buffer > 0 and nearest < buffer:
        raise JobError("BUFFER_VIOLATION", "Training and test folds do not satisfy the buffer distance.")
    excluded = set(dev) - train_set - test_set
    return {"repeat": int(repeat), "fold": int(fold), "train_ids": sorted(ids[i] for i in train),
            "test_ids": sorted(ids[i] for i in test), "excluded_ids": sorted(ids[i] for i in excluded),
            "diagnostics": {"method": method, "seed": seed, "train_samples": len(train), "test_samples": len(test),
                            "train_groups": len(train_groups), "test_groups": len(test_groups),
                            "excluded_samples": len(excluded), "buffer_distance_m": buffer,
                            "minimum_train_test_distance_m": nearest, "metric_crs": metric_crs}}


def make_folds(ids, coordinates, groups, config, *, crs, inner=False, check_cancel=lambda:None):
    """Create ID folds; inner folds use inner_splits and custom inner_method.

    Independent test IDs are never returned. For outer splits, unknown locked
    IDs are an error. For inner subsets, absent locked IDs are expected. User
    folds retain their training membership: invalid buffers are rejected, not
    silently repaired. Each development ID must be tested once per repeat.
    """
    ids, xy, groups = _inputs(ids, coordinates, groups)
    method, n_splits, repeats, seed, minimum, block_size, buffer = _settings(config, inner)
    metric, metric_crs = _metric_coordinates(xy, crs, config, method in {"spatial_block", "buffered", "knndm"} or buffer > 0)
    dev = _development(ids, groups, metric, config, buffer, inner)
    folds = []
    if method == 'temporal':
        from .temporal_d import temporal_folds
        return temporal_folds(ids,groups,dev,metric,metric_crs,config,inner=inner,
                              n_splits=n_splits,seed=seed,minimum=minimum,buffer=buffer,check_cancel=check_cancel)
    if method == 'knndm':
        from .knndm import cast_knndm
        if len(set(groups[dev]))!=len(dev):
            raise JobError('KNNDM_GROUPS','In this version, CAST kNNDM is for unique, independent observations. For repeated profiles or parcels, select grouped validation.')
        if buffer>0:raise JobError('KNNDM_BUFFER','A buffer cannot be added after CAST distance matching; select the buffered method.')
        points=config.get('prediction_coordinates')
        if points is None:raise JobError('KNNDM_DOMAIN','Prediction locations must be sampled from the mapping area.')
        prediction,_=_metric_coordinates(np.asarray(points,dtype=float),config.get('prediction_crs',crs),config,True)
        match=cast_knndm(metric[dev],prediction,n_splits,config.get('maxp',.75),seed,check_cancel)
        labels=np.asarray(match['fold'])
        for fold in range(n_splits):
            test=dev[labels==fold];train=dev[labels!=fold]
            item=_fold(ids,groups,metric,metric_crs,dev,train,test,0,fold,seed,method,minimum,0)
            item['diagnostics']['distance_matching']=match
            item['diagnostics']['mapping_sample']=config.get('prediction_sample',{})
            folds.append(item)
        return folds
    if method == "custom":
        raw_folds = config.get("custom_folds")
        if not isinstance(raw_folds, list) or not raw_folds:
            raise JobError("CUSTOM_FOLDS", "User-defined folds cannot be empty.")
        seen = set()
        coverage = defaultdict(list)
        for raw in raw_folds:
            if not isinstance(raw, dict) or set(raw) != {"repeat", "fold", "train_ids", "test_ids"}:
                raise JobError("CUSTOM_FOLDS", "Each user-defined fold must contain repeat, fold, train_ids and test_ids.")
            repeat = _positive_int(raw["repeat"], "Repeat ID", 0)
            fold = _positive_int(raw["fold"], "Fold ID", 0)
            if (repeat, fold) in seen:
                raise JobError("CUSTOM_FOLDS", "Duplicate repeat and fold ID.")
            seen.add((repeat, fold))
            train = indices_for(ids, raw["train_ids"])
            test = indices_for(ids, raw["test_ids"])
            folds.append(_fold(ids, groups, metric, metric_crs, dev, train, test, repeat, fold, seed, method, minimum, buffer))
            coverage[repeat].extend(raw["test_ids"])
        expected = Counter(ids[i] for i in dev)
        if any(Counter(values) != expected for values in coverage.values()):
            raise JobError("CUSTOM_COVERAGE", "Each development ID must be in a test fold exactly once per repeat.")
        if sorted(coverage) != list(range(len(coverage))) or any(sorted(f for r, f in seen if r == repeat) != list(range(n_splits)) for repeat in coverage):
            raise JobError("CUSTOM_FOLDS", "User-defined fold IDs must start at zero and match n_splits.")
        return sorted(folds, key=lambda item: (item["repeat"], item["fold"]))
    units = _units(dev, groups, metric, method, block_size)
    for repeat in range(repeats):
        rng = np.random.default_rng(np.random.SeedSequence([seed, repeat, int(inner)]))
        for fold, test in enumerate(_assigned_units(units, n_splits, rng)):
            train = np.asarray(sorted(set(dev) - set(test)), dtype=int)
            train = _buffer_training(train, test, groups, metric, buffer)
            folds.append(_fold(ids, groups, metric, metric_crs, dev, train, test, repeat, fold, seed, method, minimum, buffer))
    return folds


def split_calibration(ids, coordinates, groups, config, fraction, *, crs, seed):
    """Reserve whole groups/blocks, then buffer model development from calibration.

    The fraction applies to the number of indivisible groups or blocks, rounded
    upwards. Actual sample fractions can differ for unequal group sizes. Buffer
    exclusions belong to neither returned list. No target influences the split.
    """
    if isinstance(fraction, bool) or not isinstance(fraction, (float, int)) or not math.isfinite(fraction) or not 0 < fraction < 1:
        raise JobError("CALIBRATION_FRACTION", "Calibration fraction must be between 0 and 1.")
    seed = _positive_int(seed, "Calibration seed", 0)
    ids, xy, groups = _inputs(ids, coordinates, groups)
    method, _, _, _, minimum, block_size, buffer = _settings(config, inner=True)
    metric, _ = _metric_coordinates(xy, crs, config, method in {"spatial_block", "buffered"} or buffer > 0)
    dev = _development(ids, groups, metric, config, buffer, inner=True)
    if method == 'temporal':
        from .temporal_d import temporal_calibration
        return temporal_calibration(ids,groups,dev,metric,config,fraction,minimum,buffer)
    units = _units(dev, groups, metric, method, block_size)
    keys = sorted(units)
    n_calibration = int(math.ceil(len(keys) * fraction))
    if not keys or n_calibration >= len(keys):
        raise JobError("CALIBRATION_GROUPS", "There are not enough groups or spatial blocks for the calibration split.")
    selected = np.random.default_rng(seed).permutation(len(keys))[:n_calibration]
    calibration = np.asarray(sorted(i for j in selected for i in units[keys[j]]), dtype=int)
    training = np.asarray(sorted(set(dev) - set(calibration)), dtype=int)
    training = _buffer_training(training, calibration, groups, metric, buffer)
    # Enough groups must survive to allow the requested inner design as well.
    minimum = max(minimum, _positive_int(config.get("inner_splits", 3), "Number of inner folds", 2))
    if len(set(groups[training])) < minimum:
        raise JobError("CALIBRATION_GROUPS", "After calibration and buffering, there are not enough development groups for inner evaluation.")
    if len(_units(training, groups, metric, method, block_size)) < config.get("inner_splits", 3):
        raise JobError("CALIBRATION_GROUPS", "After calibration, there are not enough blocks for inner evaluation.")
    return sorted(ids[i] for i in training), sorted(ids[i] for i in calibration)
