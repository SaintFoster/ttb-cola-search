#!/usr/bin/env python3
"""
Build and maintain a whiskey COLA repository, then refresh its records viewer.

Examples:
  python3 ttb_cola_search.py

Run without a specific date range to keep a rolling 15-year CSV, pruning older
records before backfilling any gaps and refreshing through today.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import sys
import tempfile
import webbrowser
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

LOGGER = logging.getLogger("ttb_cola_search")

BASE_URL = "https://ttbonline.gov/colasonline/"
WORKSPACE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = WORKSPACE_DIR / "ttb_cola_results.csv"
DEFAULT_VIEWER = WORKSPACE_DIR / "ttb_cola_records_viewer.html"
TTB_INTERMEDIATE_CERT = WORKSPACE_DIR / "certs" / "entrust-ov-tls-issuing-rsa-ca-2.pem"

SEARCH_PAGE_URL = urljoin(BASE_URL, "publicSearchColasAdvanced.do")
EXPORT_URL = urljoin(
    BASE_URL,
    "publicSaveSearchResultsToFile.do?path=/publicSearchColasAdvancedProcess",
)
DATE_FORMAT = "%m/%d/%Y"
ROLLING_YEARS = 15
MAX_TTB_EXPORT_ROWS = 1000
QUERY_WINDOW_DAYS = 42

TTB_ID_COLUMN = "TTB ID"
COMPLETED_DATE_COLUMN = "Completed Date"
INVALID_REASON_COLUMN = "Invalid Reason"
CSV_REQUIRED_COLUMNS = {TTB_ID_COLUMN, COMPLETED_DATE_COLUMN}
MULTILINE_TEXT_COLUMNS = ("Fanciful Name", "Brand Name")
VIEWER_EXCLUDED_COLUMNS = {
    TTB_ID_COLUMN,
    "Serial Number",
    "Origin",
    "Origin Desc",
    "Class/Type",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

WHISKEY_CLASS_TYPE_CODES = {
    "100", "101", "102", "103", "109", "110", "111", "112", "113",
    "117", "118", "119", "120", "121", "122", "123", "129", "130",
    "131", "132", "133", "134", "137", "138", "139", "140", "141",
    "142", "143", "144", "146", "147", "148", "149", "150", "151",
    "152", "153", "154", "157", "158", "160", "161", "162", "165",
    "166", "167", "168", "170", "171", "172", "177", "178", "181",
    "182", "183", "184", "185", "186", "187", "188", "189", "190",
    "191", "192", "197", "198", "199", "641", "645", "691", "695",
    "701", "702", "703", "721", "722", "723", "751", "752",
    "753", "771", "772", "773",
}


@dataclass(frozen=True)
class Settings:
    """Fixed runtime settings for the repository refresh."""

    output_path: Path = DEFAULT_OUTPUT
    viewer_path: Path = DEFAULT_VIEWER
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class QuerySummary:
    """Aggregated results for one accepted query window or split window tree."""

    exported_rows: int
    mergeable_rows: int
    added_rows: int
    final_rows: int
    reported_rows: int


@dataclass(frozen=True)
class CsvRepository:
    """Serialized repository rows keyed by cleaned TTB ID."""

    header: str | None
    rows_by_ttb_id: dict[str, str]


def configure_logging() -> None:
    """Send progress logging to stdout."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)


def parse_search_date(value: str) -> date:
    """Parse a TTB search date in MM/DD/YYYY format."""
    return datetime.strptime(value, DATE_FORMAT).date()


def format_search_date(value: date) -> str:
    """Format a date for TTB search parameters and CSV comparisons."""
    return value.strftime(DATE_FORMAT)


def is_search_date(value: str) -> bool:
    """Return True when a string is a valid TTB search date."""
    try:
        parse_search_date(value.strip())
    except ValueError:
        return False
    return True


def split_date_window(start: date, end: date) -> tuple[tuple[date, date], tuple[date, date]]:
    """Split an inclusive date window into two inclusive windows."""
    midpoint = start + (end - start) // 2
    return (start, midpoint), (midpoint + timedelta(days=1), end)


def clean_ttb_id(raw_value: str) -> str:
    """Normalize TTB IDs from CSV exports and generated JavaScript data."""
    return raw_value.strip().strip("'").strip('"')


def normalize_multiline_text(value: str) -> str:
    """Replace embedded line breaks in names with ordinary spaces."""
    if "\n" not in value and "\r" not in value:
        return value
    return " ".join(part.strip() for part in value.splitlines() if part.strip())


def normalize_export_text_fields(row: list[str], header: list[str]) -> list[str]:
    """Normalize multiline fanciful and brand names in one parsed export row."""
    normalized = list(row)
    for column in MULTILINE_TEXT_COLUMNS:
        if column in header:
            index = header.index(column)
            if index < len(normalized):
                normalized[index] = normalize_multiline_text(normalized[index])
    return normalized


def serialize_csv_row(row: list[str]) -> str:
    """Serialize a CSV row to one line without a trailing newline."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(row)
    return buffer.getvalue()


def parse_csv_line(line: str) -> list[str]:
    """Parse one serialized CSV line with proper quote handling."""
    return next(csv.reader([line]))


def ttb_id_from_line(line: str) -> str:
    """Extract a cleaned TTB ID from a serialized CSV row."""
    row = parse_csv_line(line)
    return clean_ttb_id(row[0]) if row else ""


def completed_date_from_line(line: str) -> date:
    """Extract the Completed Date from a serialized repository row."""
    columns = parse_csv_line(line)
    if len(columns) < 4:
        return date.min
    try:
        return parse_search_date(columns[3].strip())
    except ValueError:
        return date.min


def read_repository(path: Path) -> CsvRepository:
    """Read the repository CSV into serialized rows keyed by cleaned TTB ID."""
    if not path.exists():
        return CsvRepository(header=None, rows_by_ttb_id={})

    with path.open(newline="", encoding="utf-8-sig", errors="replace") as source:
        reader = csv.reader(source)
        try:
            header_row = next(reader)
        except StopIteration:
            return CsvRepository(header=None, rows_by_ttb_id={})

        rows_by_ttb_id: dict[str, str] = {}
        for row in reader:
            if not row:
                continue
            ttb_id = clean_ttb_id(row[0])
            if ttb_id:
                rows_by_ttb_id[ttb_id] = serialize_csv_row(row)

    return CsvRepository(
        header=serialize_csv_row(header_row),
        rows_by_ttb_id=rows_by_ttb_id,
    )


def repository_date_bounds(path: Path) -> tuple[date | None, date | None]:
    """Return the oldest and newest valid repository dates."""
    dates = [
        completed
        for line in read_repository(path).rows_by_ttb_id.values()
        if (completed := completed_date_from_line(line)) != date.min
    ]
    return (min(dates), max(dates)) if dates else (None, None)


def rolling_start(today: date) -> date:
    """Return the inclusive first date in the 15-year TTB query window."""
    try:
        return today.replace(year=today.year - ROLLING_YEARS) + timedelta(days=1)
    except ValueError:
        return date(today.year - ROLLING_YEARS, 3, 1)


def prune_repository(path: Path, cutoff: date) -> int:
    """Remove repository rows older than the rolling query window."""
    repository = read_repository(path)
    if not repository.header:
        return 0
    retained: dict[str, str] = {}
    for ttb_id, line in repository.rows_by_ttb_id.items():
        completed = completed_date_from_line(line)
        if completed == date.min or completed >= cutoff:
            retained[ttb_id] = line
    removed = len(repository.rows_by_ttb_id) - len(retained)
    if removed:
        write_repository(path, parse_csv_line(repository.header), retained)
    return removed


def write_dict_rows(
    path: Path,
    fieldnames: list[str],
    rows: Iterable[dict[str, str]],
) -> None:
    """Write dictionaries to CSV using the supplied field order."""
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def validate_repository_headers(fieldnames: list[str] | None) -> list[str]:
    """Validate that a CSV has the columns needed to function as a repository."""
    if not fieldnames or not CSV_REQUIRED_COLUMNS.issubset(fieldnames):
        raise RuntimeError("CSV must include TTB ID and Completed Date columns.")
    return fieldnames


def quarantine_invalid_rows(path: Path, invalid_path: Path) -> tuple[int, int]:
    """Move malformed repository rows to the invalid CSV."""
    valid_rows: list[dict[str, str]] = []
    invalid_rows: list[dict[str, str]] = []

    with path.open(newline="", encoding="utf-8-sig", errors="replace") as source:
        reader = csv.DictReader(source)
        fieldnames = validate_repository_headers(reader.fieldnames)
        for row in reader:
            normalized = {
                name: normalize_multiline_text(row.get(name) or "")
                if name in MULTILINE_TEXT_COLUMNS
                else row.get(name) or ""
                for name in fieldnames
            }
            reason = invalid_row_reason(row, normalized)
            if reason:
                invalid_rows.append({**normalized, INVALID_REASON_COLUMN: reason})
            else:
                valid_rows.append(normalized)

    existing_invalid = read_existing_invalid_rows(invalid_path, fieldnames)
    combined_invalid = merge_invalid_rows(existing_invalid, invalid_rows)

    write_dict_rows(path, fieldnames, valid_rows)
    write_dict_rows(
        invalid_path,
        [*fieldnames, INVALID_REASON_COLUMN],
        combined_invalid.values(),
    )
    return len(valid_rows), len(combined_invalid)


def invalid_row_reason(
    raw_row: dict[str, str | list[str] | None],
    normalized_row: dict[str, str],
) -> str:
    """Return a reason when a CSV row should be quarantined."""
    ttb_id = clean_ttb_id(normalized_row[TTB_ID_COLUMN])
    completed = normalized_row[COMPLETED_DATE_COLUMN].strip()
    class_type = normalized_row["Class/Type"].strip()
    if raw_row.get(None):
        return "unexpected number of CSV columns"
    if not ttb_id:
        return "continuation fragment (missing TTB ID)"
    if not class_type.isdigit():
        return f"invalid Class/Type: {class_type!r}"
    if class_type not in WHISKEY_CLASS_TYPE_CODES:
        return f"unexpected Class/Type: {class_type!r}"
    if not is_search_date(completed):
        return f"invalid Completed Date: {completed!r}"
    return ""


def read_existing_invalid_rows(
    invalid_path: Path,
    fieldnames: list[str],
) -> list[dict[str, str]]:
    """Read the invalid-row queue if it already exists."""
    if not invalid_path.exists():
        return []

    rows: list[dict[str, str]] = []
    with invalid_path.open(newline="", encoding="utf-8-sig", errors="replace") as source:
        reader = csv.DictReader(source)
        for row in reader:
            if not clean_ttb_id(row.get(TTB_ID_COLUMN) or "").isdigit():
                continue
            rows.append(
                {
                    **{name: row.get(name) or "" for name in fieldnames},
                    INVALID_REASON_COLUMN: (
                        row.get(INVALID_REASON_COLUMN)
                        or "previously quarantined row"
                    ),
                }
            )
    return rows


def merge_invalid_rows(
    existing_rows: Iterable[dict[str, str]],
    new_rows: Iterable[dict[str, str]],
) -> dict[tuple[str, str], dict[str, str]]:
    """Merge invalid rows by TTB ID and reason so reruns preserve prior failures."""
    merged: dict[tuple[str, str], dict[str, str]] = {}
    for row in [*existing_rows, *new_rows]:
        key = (
            clean_ttb_id(row.get(TTB_ID_COLUMN) or ""),
            row[INVALID_REASON_COLUMN],
        )
        merged[key] = row
    return merged


def has_valid_export_identity(row: list[str], header: list[str]) -> bool:
    """Validate a complete row by TTB ID and configured class/type only."""
    try:
        ttb_id_index = header.index(TTB_ID_COLUMN)
        class_type_index = header.index("Class/Type")
    except ValueError as exc:
        raise RuntimeError("TTB export is missing a required column.") from exc
    return (
        len(row) == len(header)
        and clean_ttb_id(row[ttb_id_index]).isdigit()
        and row[class_type_index].strip() in WHISKEY_CLASS_TYPE_CODES
    )


def has_terminal_class_type(row: list[str]) -> bool:
    """Return whether a physical row ends with a queried class and description."""
    return len(row) >= 2 and row[-2].strip() in WHISKEY_CLASS_TYPE_CODES


def join_comma_fragments(parts: list[str]) -> str:
    """Join cells created by unquoted commas inside a name."""
    return ", ".join(part.strip() for part in parts)


def rebuild_export_row(
    row: list[str], header: list[str]
) -> list[str] | None:
    """Rebuild flexible name cells between a valid ID and class/type."""
    if len(row) < len(header) or not has_terminal_class_type(row):
        return None

    middle = row[4:-4]
    if len(middle) < 2:
        return None
    if len(middle) == 2:
        fanciful_name, brand_name = middle
    elif not middle[0].strip():
        fanciful_name = ""
        brand_name = join_comma_fragments(middle[1:])
    else:
        fanciful_name = join_comma_fragments(middle[:-1])
        brand_name = middle[-1].strip()

    rebuilt = [*row[:4], fanciful_name, brand_name, *row[-4:]]
    return rebuilt if has_valid_export_identity(rebuilt, header) else None


def rejoin_split_export_rows(
    rows: list[list[str]], header: list[str]
) -> tuple[list[list[str]], int]:
    """Append fragments until a configured class/type completes the record."""
    joined_rows: list[list[str]] = []
    joined_count = 0
    index = 0
    while index < len(rows):
        row = rows[index]
        rebuilt = rebuild_export_row(row, header)
        if rebuilt is not None:
            joined_rows.append(rebuilt)
            index += 1
            continue
        if has_terminal_class_type(row) or index + 1 >= len(rows):
            joined_rows.append(row)
            index += 1
            continue

        candidate = row
        continuation_index = index + 1
        repaired = False
        while continuation_index < len(rows):
            continuation = rows[continuation_index]
            if rebuild_export_row(continuation, header) is not None:
                break

            boundary_value = " ".join(
                part.strip()
                for part in (candidate[-1], continuation[0])
                if part.strip()
            )
            candidate = [*candidate[:-1], boundary_value, *continuation[1:]]
            rebuilt = rebuild_export_row(candidate, header)
            if rebuilt is not None:
                joined_rows.append(rebuilt)
                joined_count += 1
                index = continuation_index + 1
                repaired = True
                break
            if has_terminal_class_type(candidate):
                break
            continuation_index += 1

        if repaired:
            continue

        joined_rows.append(row)
        index += 1

    return joined_rows, joined_count


def rebuild_repository_continuations(path: Path) -> int:
    """Rebuild split repository records before any other validation or repair."""
    if not path.exists():
        return 0

    with path.open(newline="", encoding="utf-8-sig", errors="replace") as source:
        rows = [row for row in csv.reader(source) if row]
    if not rows:
        return 0

    header = rows[0]
    normalized_rows = [
        normalize_export_text_fields(row, header) for row in rows[1:]
    ]
    rebuilt_rows, rebuilt_count = rejoin_split_export_rows(
        normalized_rows, header
    )

    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as output:
        csv.writer(output).writerows([header, *rebuilt_rows])
    temp_path.replace(path)
    return rebuilt_count


def merge_export_into_csv(path: Path, export_text: str) -> tuple[int, int, int]:
    """Merge a TTB export into the repository, keyed by TTB ID."""
    export_rows = [row for row in csv.reader(io.StringIO(export_text)) if row]
    if not export_rows:
        return 0, 0, 0

    export_header = export_rows[0]
    normalized_rows = [
        normalize_export_text_fields(row, export_header) for row in export_rows[1:]
    ]
    data_rows, _ = rejoin_split_export_rows(
        normalized_rows, export_header
    )
    repository = read_repository(path)
    existing_count = len(repository.rows_by_ttb_id)

    if repository.header and parse_csv_line(repository.header) != export_header:
        raise RuntimeError(
            f"Existing CSV header does not match TTB export header: {path}"
        )

    incoming_count = 0
    added_count = 0
    rows_by_ttb_id = dict(repository.rows_by_ttb_id)
    for row in data_rows:
        # Preserve malformed rows with an identifier so finalization can
        # quarantine and repair them instead of silently dropping them.
        ttb_id = clean_ttb_id(row[0])
        if not ttb_id:
            continue

        incoming_count += 1
        if ttb_id not in rows_by_ttb_id:
            rows_by_ttb_id[ttb_id] = serialize_csv_row(row)
            added_count += 1

    write_repository(path, export_header, rows_by_ttb_id)
    return incoming_count, added_count, existing_count + added_count


def write_repository(
    path: Path,
    header: list[str],
    rows_by_ttb_id: dict[str, str],
) -> None:
    """Write the repository atomically, newest Completed Date first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as output:
        csv.writer(output).writerow(header)
        for line in sorted(
            rows_by_ttb_id.values(),
            key=lambda value: (
                completed_date_from_line(value),
                ttb_id_from_line(value),
            ),
            reverse=True,
        ):
            output.write(line + "\n")
    temp_path.replace(path)


