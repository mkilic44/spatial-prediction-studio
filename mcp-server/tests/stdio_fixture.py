"""Synthetic bridge for protocol-only acceptance. Never represents ArcMap."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import create_server

class Registry:
    def discover(self):
        return {"sessions": [{"session_id": "synthetic", "pid": 0}], "ignored_records": 0}

class FixtureBridge:
    registry = Registry()
    def call(self, session_id, method, params=None):
        if session_id != "synthetic":
            raise AssertionError("Unexpected fixture session")
        if method == "status":
            return {"busy": False, "state": "synthetic_protocol_fixture", "preview_ready": False}
        if method == "layers":
            return {"points": [{"name": "Layer " + str(i), "path": "fixture.shp", "fields": []} for i in range(123)],
                    "raster_bands": [], "scope": "synthetic_current_map"}
        raise AssertionError("Not exercised by protocol fixture")

if __name__ == "__main__":
    create_server(FixtureBridge()).run(transport="stdio")

