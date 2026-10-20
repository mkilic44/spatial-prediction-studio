"""Download only the explicitly approved, pinned TabPFN v2 regression weight.

HTTPS certificate verification remains enabled. This is an explicit setup tool;
the model adapter never invokes it or any automatic download.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import ssl
import urllib.request
import uuid

REVISION = "4972a65a1b30806315c6f92499959ffbfc69a673"
NAME = "tabpfn-v2-regressor.ckpt"
SHA256 = "2ab5a07d5c41dfe6db9aa7ae106fc6de898326c2765be66505a07e2868c10736"
SIZE = 44390977
BASE = "https://huggingface.co/Prior-Labs/TabPFN-v2-reg/resolve/" + REVISION + "/"


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def download(url, path, limit, *, expected_hash=None, expected_size=None):
    if path.exists():
        if expected_hash and (digest(path) != expected_hash or path.stat().st_size != expected_size):
            raise RuntimeError("Existing weight does not match the approved artifact; no overwrite: " + str(path))
        return
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    context = ssl.create_default_context()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "SpatialPredictionStudio-authorized-setup/0.4"})
        with urllib.request.urlopen(request, context=context, timeout=60) as response, temporary.open("xb") as out:
            if not response.url.startswith("https://"):
                raise RuntimeError("HTTPS downgrade refused")
            total = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise RuntimeError("Download exceeds expected size limit")
                out.write(block)
        if expected_size is not None and total != expected_size:
            raise RuntimeError("Unexpected artifact length")
        if expected_hash is not None and digest(temporary) != expected_hash:
            raise RuntimeError("Downloaded weight SHA-256 does not match the approved artifact")
        temporary.rename(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parents[1] / "models" / "tabpfn-v2")
    args = parser.parse_args()
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    weight = destination / NAME
    license_path = destination / "LICENSE.txt"
    download(BASE + NAME, weight, SIZE, expected_hash=SHA256, expected_size=SIZE)
    download(BASE + "LICENSE.txt", license_path, 100000)
    license_text = license_path.read_text(encoding="utf-8")
    if "Prior Labs" not in license_text or "1.1" not in license_text:
        raise RuntimeError("Unexpected license text; inspect the pinned source")
    manifest = {
        "schema_version": 1, "status": "downloaded_and_hash_verified",
        "package": "tabpfn", "package_version": "2.2.1", "model_family": "TabPFN-v2-reg",
        "repository": "Prior-Labs/TabPFN-v2-reg", "revision": REVISION,
        "filename": NAME, "bytes": SIZE, "sha256": SHA256, "source_url": BASE + NAME,
        "license": "Prior Labs License 1.1", "license_file": "LICENSE.txt",
        "license_source_url": BASE + "LICENSE.txt", "license_sha256": digest(license_path),
        "required_attribution": "Built with PriorLabs-TabPFN",
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(), "https_certificate_verification": True,
        "authorization": "User explicitly approved download, local testing and integration in this task.",
    }
    target = destination / "weights-manifest.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(target), "weight_bytes": weight.stat().st_size, "sha256": digest(weight)}))


if __name__ == "__main__":
    main()
