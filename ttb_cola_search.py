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


def collect_form_defaults(form: Tag) -> dict[str, str | list[str]]:
    """Collect default values from the TTB advanced-search form."""
    payload: dict[str, str | list[str]] = {}
    for field in form.find_all(["input", "select", "textarea"]):
        if not isinstance(field, Tag):
            continue
        name = field.get("name")
        if not isinstance(name, str) or not name:
            continue

        if field.name == "input":
            field_type = str(field.get("type", "text")).lower()
            if field_type in {"button", "submit", "image", "file"}:
                continue
            if field_type in {"checkbox", "radio"} and not field.has_attr("checked"):
                continue
            payload[name] = str(field.get("value", ""))
            continue

        if field.name == "select":
            if field.has_attr("multiple"):
                selected = [
                    str(option.get("value", ""))
                    for option in field.find_all("option", selected=True)
                    if option.get("value", "")
                ]
                if selected:
                    payload[name] = selected
                continue

            selected_option = field.find("option", selected=True) or field.find("option")
            if isinstance(selected_option, Tag):
                payload[name] = str(selected_option.get("value", ""))
            continue

        payload[name] = field.get_text()
    return payload


def search_form(response: requests.Response) -> Tag:
    """Return the advanced-search form from the TTB search page."""
    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.find("form", attrs={"name": "searchCriteriaForm"})
    if not isinstance(form, Tag):
        raise RuntimeError("Could not find searchCriteriaForm on the TTB page.")
    return form


def whiskey_class_type_codes(form: Tag) -> list[str]:
    """Return the class/type codes from the live form that match our whitelist."""
    field = form.find(attrs={"name": "searchCriteria.classTypeCodeArrayByCode"})
    if not isinstance(field, Tag):
        raise RuntimeError("Could not find advanced class/type code field.")

    codes = [
        str(option.get("value", ""))
        for option in field.find_all("option")
        if option.get("value", "") in WHISKEY_CLASS_TYPE_CODES
    ]
    if not codes:
        raise RuntimeError("No matching advanced class/type codes found on the TTB form.")
    return codes


def build_payload(
    form: Tag, start: date, end: date
) -> dict[str, str | list[str]]:
    """Build the fixed whiskey search payload for a date range."""
    payload = collect_form_defaults(form)
    payload.update(
        {
            "searchCriteria.dateCompletedFrom": format_search_date(start),
            "searchCriteria.dateCompletedTo": format_search_date(end),
            "searchCriteria.productNameSearchType": "E",
            "searchCriteria.classTypeDesired": "code",
            "searchCriteria.classTypeCodeArrayByCode": whiskey_class_type_codes(form),
        }
    )
    return payload


def result_count(html: str) -> str | None:
    """Extract TTB's reported total matching-record count from search HTML."""
    match = re.search(r"Total Matching Records:\s*([0-9,]+)", html)
    return match.group(1) if match else None


def total_count_as_int(total: str | None) -> int | None:
    """Convert a TTB count string to an integer."""
    if total is None:
        return None
    return int(total.replace(",", ""))


def csv_row_count(csv_text: str) -> int:
    """Count data rows in a CSV export."""
    return max(0, sum(1 for row in csv.reader(io.StringIO(csv_text)) if row) - 1)


def fetch_query(
    settings: Settings,
    session: requests.Session,
    form: Tag,
    page_url: str,
    verify: bool,
    start: date,
    end: date,
) -> tuple[str, int, str | None]:
    """Run one TTB search and return export text, exported rows, and reported rows."""
    action = urljoin(page_url, str(form.get("action", "")))
    search_response = session.post(
        action,
        data=build_payload(form, start, end),
        timeout=settings.timeout_seconds,
        verify=verify,
    )
    search_response.raise_for_status()

    export_response = session.get(EXPORT_URL, timeout=settings.timeout_seconds, verify=verify)
    export_response.raise_for_status()

    return (
        export_response.text,
        csv_row_count(export_response.text),
        result_count(search_response.text),
    )


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


def run_query_with_splitting(
    settings: Settings,
    session: requests.Session,
    form: Tag,
    page_url: str,
    verify: bool,
    start: date,
    end: date,
    depth: int = 0,
) -> QuerySummary:
    """Run a query, recursively splitting windows that hit TTB's 1,000-row cap."""
    export_text, exported_rows, total = fetch_query(
        settings,
        session,
        form,
        page_url,
        verify,
        start,
        end,
    )
    reported_total = total_count_as_int(total)
    capped = (
        exported_rows >= MAX_TTB_EXPORT_ROWS
        or (reported_total is not None and reported_total >= MAX_TTB_EXPORT_ROWS)
    )

    if capped:
        if start == end:
            raise RuntimeError(
                f"TTB returned at least 1,000 records for {format_search_date(start)}; "
                "cannot split below one day."
            )
        log_split(start, end, reported_total, depth)
        left, right = split_date_window(start, end)
        left_summary = run_query_with_splitting(
            settings,
            session,
            form,
            page_url,
            verify,
            left[0],
            left[1],
            depth + 1,
        )
        right_summary = run_query_with_splitting(
            settings,
            session,
            form,
            page_url,
            verify,
            right[0],
            right[1],
            depth + 1,
        )
        return QuerySummary(
            exported_rows=left_summary.exported_rows + right_summary.exported_rows,
            mergeable_rows=left_summary.mergeable_rows + right_summary.mergeable_rows,
            added_rows=left_summary.added_rows + right_summary.added_rows,
            final_rows=right_summary.final_rows,
            reported_rows=left_summary.reported_rows + right_summary.reported_rows,
        )

    incoming_count, added_count, final_count = merge_export_into_csv(
        settings.output_path,
        export_text,
    )
    reported_rows = reported_total if reported_total is not None else incoming_count
    log_query_result(start, end, reported_rows, exported_rows, added_count, depth)
    return QuerySummary(
        exported_rows=exported_rows,
        mergeable_rows=incoming_count,
        added_rows=added_count,
        final_rows=final_count,
        reported_rows=reported_rows,
    )


def run_query_in_windows(
    settings: Settings,
    session: requests.Session,
    form: Tag,
    page_url: str,
    verify: bool,
    start: date,
    end: date,
) -> QuerySummary:
    """Query an inclusive date range in fixed windows, splitting only at the cap."""
    summaries: list[QuerySummary] = []
    window_start = start
    while window_start <= end:
        window_end = min(
            end, window_start + timedelta(days=QUERY_WINDOW_DAYS - 1)
        )
        summaries.append(
            run_query_with_splitting(
                settings, session, form, page_url, verify, window_start, window_end
            )
        )
        window_start = window_end + timedelta(days=1)

    if not summaries:
        raise ValueError("Query end date cannot precede its start date.")
    return QuerySummary(
        exported_rows=sum(summary.exported_rows for summary in summaries),
        mergeable_rows=sum(summary.mergeable_rows for summary in summaries),
        added_rows=sum(summary.added_rows for summary in summaries),
        final_rows=summaries[-1].final_rows,
        reported_rows=sum(summary.reported_rows for summary in summaries),
    )


def log_split(start: date, end: date, reported_total: int | None, depth: int) -> None:
    """Log why a date window is being split."""
    reported = (
        f"TTB total {reported_total:,}"
        if reported_total is not None
        else "export hit 1,000"
    )
    LOGGER.info(
        "%s%s to %s: %s; splitting.",
        "  " * depth,
        format_search_date(start),
        format_search_date(end),
        reported,
    )


def log_query_result(
    start: date,
    end: date,
    reported_rows: int,
    exported_rows: int,
    added_rows: int,
    depth: int,
) -> None:
    """Log the result of a date-window query."""
    LOGGER.info(
        "%s%s to %s: reported %s, exported %s, added %s.",
        "  " * depth,
        format_search_date(start),
        format_search_date(end),
        f"{reported_rows:,}",
        exported_rows,
        added_rows,
    )


def refresh_viewer(csv_path: Path, viewer_path: Path) -> None:
    """Refresh the existing viewer payload while preserving UI customizations."""
    columns, rows, ids = viewer_data(csv_path)
    payload = {
        "title": "TTB COLA Records Viewer",
        "sourceName": csv_path.name,
        "rowCount": len(rows),
        "columns": columns,
        "rows": rows,
    }

    if not viewer_path.exists():
        raise RuntimeError(
            f"Viewer template not found: {viewer_path}. "
            "Keep ttb_cola_records_viewer.html alongside this script."
        )

    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    viewer = viewer_path.read_text(encoding="utf-8")
    viewer, replacements = re.subn(
        r'(<script id="payload" type="application/json">).*?(</script>)',
        lambda match: match.group(1) + payload_json + match.group(2),
        viewer,
        flags=re.DOTALL,
    )
    if not replacements:
        raise RuntimeError(f"Could not find viewer payload in {viewer_path}.")
    viewer_path.write_text(viewer, encoding="utf-8")

    ids_json = json.dumps(ids, ensure_ascii=False, separators=(",", ":"))
    ids_path = viewer_path.with_name("ttb_cola_ttb_ids.js")
    ids_path.write_text(f"window.TTB_COLA_IDS = {ids_json};\n", encoding="utf-8")


def viewer_data(csv_path: Path) -> tuple[list[str], list[list[str]], list[str]]:
    """Read repository data in the shape expected by the records viewer."""
    with csv_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise RuntimeError("Cannot build viewer from a CSV without headers.")

        columns = [
            column
            for column in reader.fieldnames
            if column not in VIEWER_EXCLUDED_COLUMNS
        ]
        rows: list[list[str]] = []
        ids: list[str] = []
        for row in reader:
            rows.append([(row.get(column) or "").strip() for column in columns])
            ids.append(clean_ttb_id(row.get(TTB_ID_COLUMN) or ""))
    return columns, rows, ids


def finalize_repository(settings: Settings) -> None:
    """Rebuild, validate, prune, and refresh repository outputs."""
    invalid_path = settings.output_path.with_name(
        f"{settings.output_path.stem}_invalid.csv"
    )
    rebuild_repository_continuations(settings.output_path)
    valid_rows, invalid_rows = quarantine_invalid_rows(
        settings.output_path, invalid_path
    )

    pruned_rows = prune_repository(settings.output_path, rolling_start(date.today()))
    if pruned_rows:
        LOGGER.info(
            "Removed %s rows older than the rolling cutoff after validation.",
            pruned_rows,
        )
    LOGGER.info(
        "CSV validation: %s valid rows, %s invalid rows.",
        f"{valid_rows:,}",
        f"{invalid_rows:,}",
    )
    if invalid_rows:
        LOGGER.info("Invalid rows saved: %s", invalid_path.resolve())

    refresh_viewer(settings.output_path, settings.viewer_path)
    LOGGER.info("Viewer refreshed: %s", settings.viewer_path.resolve())
    webbrowser.open(settings.viewer_path.resolve().as_uri())


def report_query_summary(summary: QuerySummary, output_path: Path) -> None:
    """Log a standard summary after query completion."""
    LOGGER.info("Reported rows across accepted windows: %s", summary.reported_rows)
    LOGGER.info("Exported rows from accepted windows: %s", summary.exported_rows)
    LOGGER.info("Mergeable rows from accepted windows: %s", summary.mergeable_rows)
    LOGGER.info("New rows added: %s", summary.added_rows)
    LOGGER.info("Total unique rows in CSV: %s", summary.final_rows)
    LOGGER.info("Saved CSV: %s", output_path.resolve())


def update_repository(
    settings: Settings,
    session: requests.Session,
    form: Tag,
    page_url: str,
    verify: bool,
) -> QuerySummary:
    """Maintain the rolling 15-year repository and refresh it through today."""
    today = date.today()
    cutoff = rolling_start(today)
    removed_rows = prune_repository(settings.output_path, cutoff)
    if removed_rows:
        LOGGER.info("Removed %s rows older than %s.", removed_rows, format_search_date(cutoff))

    oldest_date, newest_date = repository_date_bounds(settings.output_path)
    if newest_date is None:
        LOGGER.info(
            "Building rolling coverage from %s through %s",
            format_search_date(cutoff), format_search_date(today)
        )
        return run_query_in_windows(
            settings, session, form, page_url, verify, cutoff, today
        )

    if oldest_date is None:
        raise RuntimeError("CSV has no valid Completed Date values.")
    if oldest_date > cutoff:
        backfill_end = oldest_date - timedelta(days=1)
        LOGGER.info(
            "Backfilling rolling coverage from %s through %s",
            format_search_date(cutoff), format_search_date(backfill_end)
        )
        run_query_in_windows(
            settings, session, form, page_url, verify, cutoff, backfill_end
        )
        _, newest_date = repository_date_bounds(settings.output_path)
        if newest_date is None:
            raise RuntimeError("Rolling backfill did not produce a valid Completed Date.")

    update_start = max(cutoff, min(today, newest_date - timedelta(days=1)))
    LOGGER.info(
        "Refreshing repository from %s through %s",
        format_search_date(update_start), format_search_date(today)
    )
    return run_query_in_windows(
        settings, session, form, page_url, verify, update_start, today
    )


def create_session() -> requests.Session:
    """Create a TTB session with the normal public roots plus its missing issuer."""
    if not TTB_INTERMEDIATE_CERT.is_file():
        raise RuntimeError(f"Missing TTB issuer certificate: {TTB_INTERMEDIATE_CERT}")

    # TTB currently omits this intermediate certificate from its TLS handshake.
    # Preserve Requests' full public trust store and append only that issuer.
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix="ttb-cola-ca-", suffix=".pem", delete=False
    ) as bundle:
        bundle.write(Path(requests.certs.where()).read_bytes())
        bundle.write(b"\n")
        bundle.write(TTB_INTERMEDIATE_CERT.read_bytes())

    session = requests.Session()
    session.headers.update(HEADERS)
    session.verify = bundle.name
    return session


def run() -> int:
    """Execute the fixed whiskey repository refresh."""
    configure_logging()
    settings = Settings()

    # Use the per-session bundle: Requests' normal roots plus TTB's missing issuer.
    session = create_session()
    verify = session.verify
    try:
        page_response = session.get(
            SEARCH_PAGE_URL,
            timeout=settings.timeout_seconds,
            verify=verify,
        )
        page_response.raise_for_status()
        form = search_form(page_response)

        summary = update_repository(settings, session, form, page_response.url, verify)
        report_query_summary(summary, settings.output_path)
        finalize_repository(settings)
        return 0
    except requests.RequestException as exc:
        LOGGER.error("HTTP request failed: %s", exc)
        return 1
    except (RuntimeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1


def main() -> int:
    """Run the fixed refresh without command-line settings."""
    if len(sys.argv) > 1:
        print("This script takes no command-line settings.", file=sys.stderr)
        return 2
    return run()

if __name__ == "__main__":
    raise SystemExit(main())
