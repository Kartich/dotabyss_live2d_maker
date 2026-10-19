# -*- coding: utf-8 -*-
"""Overwrite-in-place Catalog resource table persistence; no dated backups."""
from __future__ import annotations
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

TABLE_FIELDS = ("name", "kind", "scope", "character_ids", "story_ids", "source_path", "downloaded")
CURRENT_CSV = "资源表.csv"
CURRENT_JSON = "资源表.json"
ADDED_CSV = "新增资源表.csv"
ADDED_JSON = "新增资源表.json"
META_JSON = "资源表更新信息.json"


def _read_names(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            rows = payload.get("records", payload.get("resources", []))
        else:
            rows = payload
        if isinstance(rows, list):
            return {str(row.get("name", "")) for row in rows if isinstance(row, dict) and row.get("name")}
    except Exception:
        pass
    return set()


def _project(rows: Iterable[dict]) -> list[dict]:
    return [{field: str(row.get(field, "") or "") for field in TABLE_FIELDS} for row in rows]


def _write_csv(path: Path, rows: list[dict]) -> None:
    # Always overwrite the same file; never create backup copies.
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=TABLE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def save_resource_tables(records: list[dict], table_dir: Path, catalog_path: Path, *, update_delta: bool = True) -> dict:
    table_dir = Path(table_dir).expanduser().resolve()
    table_dir.mkdir(parents=True, exist_ok=True)
    current_json = table_dir / CURRENT_JSON
    prior_exists = current_json.is_file()
    old_names = _read_names(current_json) if prior_exists else set()
    rows = _project(records)
    current_names = {row["name"] for row in rows if row["name"]}
    added_count = 0
    initial = not prior_exists

    current_payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "catalog": str(Path(catalog_path).expanduser().resolve()),
        "count": len(rows),
        "fields": list(TABLE_FIELDS),
        "records": rows,
    }
    current_json.write_text(json.dumps(current_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(table_dir / CURRENT_CSV, rows)

    if update_delta:
        added = rows if initial else [row for row in rows if row["name"] not in old_names]
        added_count = len(added)
        delta_payload = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "catalog": str(Path(catalog_path).expanduser().resolve()),
            "comparison": "首次建表（初始资源清单）" if initial else "与上次资源表比较，仅包含新增 Bundle 名称",
            "count": len(added),
            "fields": list(TABLE_FIELDS),
            "records": added,
        }
        (table_dir / ADDED_JSON).write_text(json.dumps(delta_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_csv(table_dir / ADDED_CSV, added)

    meta = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "catalog": str(Path(catalog_path).expanduser().resolve()),
        "resource_count": len(rows),
        "delta_updated": bool(update_delta),
        "delta_count": added_count if update_delta else None,
        "first_build": initial,
        "backup_policy": "覆盖写入，不保留历史备份",
        "table_files": [CURRENT_CSV, CURRENT_JSON, ADDED_CSV, ADDED_JSON],
    }
    (table_dir / META_JSON).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"table_dir": str(table_dir), "count": len(rows), "added_count": added_count,
            "initial": initial, "delta_updated": bool(update_delta),
            "csv": str(table_dir / CURRENT_CSV), "added_csv": str(table_dir / ADDED_CSV)}
