# Static viewer asset generation.
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ttb_cola_data import TTB_ID_COLUMN, VIEWER_EXCLUDED_COLUMNS, clean_ttb_id

VIEWER_DATA_FILE = "ttb_cola_records.json"

def refresh_viewer(csv_path: Path, viewer_path: Path) -> None:
    """Write external viewer data and update the lightweight HTML shell."""
    columns, rows, ids, newest_completed = viewer_data(csv_path)
    payload = {
        "title": "TTB COLA Records Viewer",
        "sourceName": csv_path.name,
        "rowCount": len(rows),
        "columns": columns,
        "rows": rows,
        "ttbIds": ids,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "newestCompletedDate": newest_completed,
    }
    if not viewer_path.exists():
        raise RuntimeError(f"Viewer template not found: {viewer_path}.")
    data_path = viewer_path.with_name(VIEWER_DATA_FILE)
    data_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    viewer = viewer_path.read_text(encoding="utf-8")
    viewer, replacements = re.subn(
        r'<script id="payload" type="application/json">.*?</script>',
        '<script id="payload" type="application/json"></script>',
        viewer,
        flags=re.DOTALL,
    )
    if not replacements:
        raise RuntimeError(f"Could not find viewer payload in {viewer_path}.")
    viewer_path.write_text(viewer, encoding="utf-8")
    ids_path = viewer_path.with_name("ttb_cola_ttb_ids.js")
    ids_path.write_text(f"window.TTB_COLA_IDS = {json.dumps(ids, ensure_ascii=False, separators=(chr(44), chr(58)))};\\n", encoding="utf-8")

def viewer_data(csv_path: Path) -> tuple[list[str], list[list[str]], list[str], str | None]:
    """Read repository data in the shape expected by the records viewer."""
    with csv_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise RuntimeError("Cannot build viewer from a CSV without headers.")
        columns = [column for column in reader.fieldnames if column not in VIEWER_EXCLUDED_COLUMNS]
        rows: list[list[str]] = []
        ids: list[str] = []
        newest_completed: datetime | None = None
        for row in reader:
            rows.append([(row.get(column) or "").strip() for column in columns])
            ids.append(clean_ttb_id(row.get(TTB_ID_COLUMN) or ""))
            try:
                completed = datetime.strptime(row.get("Completed Date") or "", "%m/%d/%Y")
            except ValueError:
                continue
            newest_completed = max(newest_completed, completed) if newest_completed else completed
    return columns, rows, ids, newest_completed.strftime("%B %-d, %Y") if newest_completed else None
