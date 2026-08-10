#!/usr/bin/env python3
"""Refresh the TTB COLA repository and publish its viewer assets."""
from __future__ import annotations

import logging
import sys
import webbrowser
from datetime import date, timedelta
from pathlib import Path

import requests
from bs4.element import Tag

from ttb_cola_data import (
    MAX_TTB_EXPORT_ROWS, QUERY_WINDOW_DAYS, QuerySummary, Settings,
    format_search_date, merge_export_into_csv, prune_repository,
    rebuild_repository_continuations, repository_date_bounds, rolling_start,
    split_date_window, quarantine_invalid_rows,
)
from ttb_cola_ttb import SEARCH_PAGE_URL, create_session, fetch_query, search_form, total_count_as_int
from ttb_cola_viewer import refresh_viewer

LOGGER = logging.getLogger("ttb_cola_search")

def configure_logging() -> None:
    """Send progress logging to stdout."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
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



def run() -> int:
    """Execute the fixed whiskey repository refresh."""
    configure_logging()
    settings = Settings()
    session = create_session()
    verify = session.verify
    try:
        page_response = session.get(SEARCH_PAGE_URL, timeout=settings.timeout_seconds, verify=verify)
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
