# TTB COLA HTTP and search-form handling.
from __future__ import annotations

import csv
import io
import re
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

from ttb_cola_data import Settings, WHISKEY_CLASS_TYPE_CODES, format_search_date

BASE_URL = "https://ttbonline.gov/colasonline/"
WORKSPACE_DIR = Path(__file__).resolve().parent
TTB_INTERMEDIATE_CERT = WORKSPACE_DIR / "certs" / "entrust-ov-tls-issuing-rsa-ca-2.pem"
SEARCH_PAGE_URL = urljoin(BASE_URL, "publicSearchColasAdvanced.do")
EXPORT_URL = urljoin(BASE_URL, "publicSaveSearchResultsToFile.do?path=/publicSearchColasAdvancedProcess")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
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


