"""Read only native allow-listed completed artifacts; verify the bytes returned."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

from .bridge import BridgeError, reject_reparse, strict_json

MAX_READ = 32 * 1024 * 1024
MAX_RESULT = 64 * 1024
COORDINATE_NAMES = {
    "x", "y", "z", "lon", "lat", "longitude", "latitude", "easting", "northing",
    "geometry", "coordinates", "coordinate", "wkt", "geojson", "shape", "shape_x",
    "shape_y", "x_coordinate", "y_coordinate", "sample_x", "sample_y",
}
REDACTED = "[koordinat veya ham kaynak alanı gizlendi]"


def is_coordinate(name):
    value = str(name).strip().lower().replace(" ", "_")
    return (value in COORDINATE_NAMES or value.startswith("geometry.")
            or value.split(".")[-1] in COORDINATE_NAMES
            or re.search(r"(^|[_.])coordinates?([_.]|$)", value) is not None
            or value.endswith(("_longitude", "_latitude", "_easting", "_northing")))


def is_raw_source(name):
    value = str(name).strip().lower()
    return value in {"raw_record", "properties"} or value.startswith(("property.", "properties."))


def sensitive_reference(value):
    return any(is_coordinate(token) or is_raw_source(token)
               for token in re.findall(r"[\w.]+", str(value)))


def coordinate_parameter_row(value):
    return any(key in value and sensitive_reference(value[key]) for key in ("parameter", "field", "key"))


def redact_coordinates(value):
    if isinstance(value, dict):
        contextual = coordinate_parameter_row(value)
        return {k: (REDACTED if is_coordinate(k) or is_raw_source(k) or (contextual and k == "value") else redact_coordinates(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_coordinates(v) for v in value]
    return value


def redact_json_cell(value):
    # Preserve ordinary scalar CSV text exactly; only parse structured JSON cells.
    if not isinstance(value, str) or not value.lstrip().startswith(("{", "[")):
        return value
    try:
        parsed = strict_json(value)
    except ValueError:
        return "[ayrıştırılamayan yapılandırılmış hücre gizlendi]"
    return json.dumps(redact_coordinates(parsed), ensure_ascii=False, allow_nan=False)


class PlainHtml(HTMLParser):
    def __init__(self, include_coordinates=False):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0
        self.include_coordinates = include_coordinates
        self.row, self.row_header, self.tables = None, False, []

    def append(self, text):
        (self.row if self.row is not None else self.parts).append(text)

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"} or (tag == "pre" and not self.include_coordinates):
            if tag == "pre" and not self.hidden:
                self.append("\n[ham önbiçimli içerik gizlendi]\n")
            self.hidden += 1
        if self.hidden:
            return
        if tag == "table":
            self.tables.append(False)
        if tag == "tr":
            self.row, self.row_header = [], False
        if tag == "th":
            self.row_header = True
        if tag in {"p", "div", "tr", "h1", "h2", "h3", "li", "br"}:
            self.append("\n")

    def handle_endtag(self, tag):
        if (tag in {"script", "style"} or (tag == "pre" and not self.include_coordinates)) and self.hidden:
            self.hidden -= 1
            return
        if self.hidden:
            return
        if not self.hidden and tag in {"td", "th"}:
            self.append(" | ")
        if tag == "tr" and self.row is not None:
            content = "".join(self.row)
            sensitive = not self.include_coordinates and sensitive_reference(content)
            if sensitive and self.row_header and self.tables:
                self.tables[-1] = True
            self.parts.append("\n" + REDACTED if sensitive or (self.tables and self.tables[-1]) else content)
            self.row = None
        if tag == "table" and self.tables:
            self.tables.pop()

    def handle_data(self, data):
        if not self.hidden:
            self.append(data)


def bounded_json(value):
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_RESULT:
        raise BridgeError("RESULT_LIMIT", "Sonuç büyük. Daha küçük sayfa isteyin.")
    return value


def validated_artifacts(listing):
    if not isinstance(listing, dict) or not isinstance(listing.get("artifacts"), list):
        raise BridgeError("ARTIFACT_LIST", "Doğrulanmış çıktı listesi alınamadı.")
    manifest = Path(str(listing.get("manifest_path", "")))
    if not manifest.is_absolute():
        raise BridgeError("ARTIFACT_PATH", "Çıktı bildiriminin yolu mutlak değil.")
    reject_reparse(manifest)
    root = manifest.resolve().parent
    if len(listing["artifacts"]) > 10000:
        raise BridgeError("ARTIFACT_LIMIT", "Çıktı listesi sınırı aşıldı.")
    names, results = set(), []
    for item in listing["artifacts"]:
        if not isinstance(item, dict):
            raise BridgeError("ARTIFACT_LIST", "Çıktı kaydı geçersiz.")
        name = item.get("name")
        if not isinstance(name, str) or not name or "\\" in name or ":" in name:
            raise BridgeError("ARTIFACT_PATH", "Çıktı adı geçersiz.")
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or name in names:
            raise BridgeError("ARTIFACT_PATH", "Çıktı adı veya kapsamı geçersiz.")
        names.add(name)
        source = Path(str(item.get("path", "")))
        if not source.is_absolute():
            raise BridgeError("ARTIFACT_PATH", "Çıktı yolu mutlak değil.")
        reject_reparse(source)
        resolved = source.resolve()
        if resolved != (root / Path(*relative.parts)).resolve() or not resolved.is_relative_to(root):
            raise BridgeError("ARTIFACT_PATH", "Çıktı bildirim dizininin dışında.")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-fA-F]{64}", digest):
            raise BridgeError("ARTIFACT_HASH", "Çıktı özeti geçersiz.")
        results.append({**item, "path": str(resolved), "sha256": digest.lower()})
    return results


def read_verified(listing, name):
    matches = [item for item in validated_artifacts(listing) if item["name"] == name]
    if len(matches) != 1:
        raise BridgeError("ARTIFACT_UNKNOWN", "İstenen dosya tamamlanmış sonucun listesinde yok.")
    item = matches[0]
    path = Path(item["path"])
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_READ + 1)
        if len(data) > MAX_READ:
            raise BridgeError("ARTIFACT_READ_LIMIT", "Dosya metin okuma sınırını aşıyor; dışa aktarmayı kullanın.")
    except OSError:
        raise BridgeError("ARTIFACT_READ", "Çıktı dosyası okunamadı.") from None
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise BridgeError("ARTIFACT_CHANGED", "Çıktı dosyasının özeti değişmiş; içerik döndürülmedi.")
    if type(item.get("bytes")) is int and item["bytes"] != len(data):
        raise BridgeError("ARTIFACT_CHANGED", "Çıktı dosyasının boyutu değişmiş.")
    return item, data


def read_text(listing, name, offset=0, limit=12000, expected_sha256=None, include_coordinates=False):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
        raise BridgeError("PAGE_BOUNDS", "Metin sayfası sınırları geçersiz.")
    item, data = read_verified(listing, name)
    if expected_sha256 is not None and expected_sha256 != item["sha256"]:
        raise BridgeError("PAGE_STALE", "Sonuç değişti; ilk sayfadan başlayın.")
    suffix = Path(name).suffix.lower()
    if suffix not in {".html", ".htm", ".txt", ".md", ".json"}:
        raise BridgeError("ARTIFACT_TYPE", "Bu dosya metin aracıyla okunmaz. CSV için tablo aracını kullanın.")
    try:
        text = data.decode("utf-8-sig")
        if suffix in {".html", ".htm"}:
            parser = PlainHtml(include_coordinates=include_coordinates)
            parser.feed(text)
            text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
            text = re.sub(r"\n[ \t]*\n+", "\n\n", text).strip()
        if suffix == ".json":
            obj = strict_json(text)
            if not include_coordinates:
                obj = redact_coordinates(obj)
            text = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)
    except (ValueError, UnicodeError):
        raise BridgeError("ARTIFACT_TEXT", "Metin UTF-8 veya JSON olarak okunamadı.") from None
    end = min(offset + limit, len(text))
    return bounded_json({"name": name, "sha256": item["sha256"], "offset": offset,
                         "next_offset": end if end < len(text) else None, "total_characters": len(text),
                         "text": text[offset:end], "coordinate_fields_included": include_coordinates})


def read_table(listing, name, offset=0, limit=50, columns=None,
               expected_sha256=None, include_coordinates=False):
    if type(offset) is not int or not 0 <= offset <= 1000000 or type(limit) is not int or not 1 <= limit <= 200:
        raise BridgeError("PAGE_BOUNDS", "Tablo sayfası sınırları geçersiz.")
    if Path(name).suffix.lower() != ".csv":
        raise BridgeError("ARTIFACT_TYPE", "Tablo aracı yalnız CSV okur.")
    item, data = read_verified(listing, name)
    if expected_sha256 is not None and expected_sha256 != item["sha256"]:
        raise BridgeError("PAGE_STALE", "Sonuç değişti; ilk sayfadan başlayın.")
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
        fields = reader.fieldnames
        if not fields or len(fields) > 1000 or len(fields) != len(set(fields)):
            raise ValueError("Invalid header")
        available = [field for field in fields if include_coordinates or not (is_coordinate(field) or is_raw_source(field))]
        selected = available if columns is None else columns
        if not isinstance(selected, list) or len(selected) > 1000 or any(c not in available for c in selected):
            raise BridgeError("TABLE_COLUMNS", "Sütun yok veya koordinat alanı açıkça etkinleştirilmedi.")
        rows, total = [], 0
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("Malformed row")
            if offset <= total < offset + limit:
                sensitive_row = coordinate_parameter_row(row)
                rows.append({key: row[key] if include_coordinates else (
                    REDACTED if sensitive_row and key == "value" else redact_json_cell(row[key])) for key in selected})
            total += 1
        end = min(offset + len(rows), total)
        return bounded_json({"name": name, "sha256": item["sha256"], "columns": selected,
                             "rows": rows, "offset": offset, "total_rows": total,
                             "next_offset": end if end < total else None,
                             "omitted_coordinate_columns": [f for f in fields if f not in available]})
    except BridgeError:
        raise
    except (ValueError, UnicodeError, csv.Error):
        raise BridgeError("ARTIFACT_CSV", "CSV yapısı veya kodlaması geçersiz.") from None

