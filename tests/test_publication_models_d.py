"""Real producer metadata, run one family per clean dedicated D process."""
import importlib.util
import io
import json

import joblib
import numpy as np
import pytest

from test_science_b import scientific, fit

pytestmark = pytest.mark.skipif(importlib.util.find_spec("torch") is None,
                               reason="D modelleri için ayrılmış Python çalışma ortamı gerekir.")


@pytest.mark.parametrize("name", ["tabm", "realmlp", "tabpfn"])
def test_actual_producer_runtime_parameters_and_serialization(scientific, name):
    params = {"n_estimators": 1} if name == "tabpfn" else {"n_epochs": 1, "hidden_size": 8, "n_layers": 1}
    scientific[-1]["model"] = {"id": name, "parameters": params}
    fitted = fit(scientific)
    producer = fitted.record["experimental_model_diagnostics"]["producer_parameters"]
    if name == "tabm":
        assert producer["constructor"]["n_blocks"] == 1
        assert producer["optimizer_defaults"]["betas"] == [.9, .999]
        assert producer["optimizer_defaults"]["lr"] == .001
    elif name == "realmlp":
        assert producer["get_config"]["use_early_stopping"] is False
        assert producer["get_config"]["val_fraction"] == 0
        assert producer["get_params"]["n_epochs"] == 1
    else:
        assert producer["get_params"]["device"] == "cpu"
        assert producer["get_params"]["fit_mode"] == "fit_preprocessors"
        assert producer["get_params"]["inference_precision"] == {"type": "torch.dtype", "value": "torch.float32"}
        assert producer["inference_config_source"] == "interface_config_"
        assert producer["inference_config"]["fields"]["MAX_NUMBER_OF_SAMPLES"] > 0
    assert "type_only_not_exposed" not in json.dumps(producer, allow_nan=False, sort_keys=True)
    buffer = io.BytesIO()
    joblib.dump(fitted, buffer)
    buffer.seek(0)
    restored = joblib.load(buffer)
    assert restored.record == fitted.record
    np.testing.assert_array_equal(fitted.predict(scientific[0][:4]), restored.predict(scientific[0][:4]))
