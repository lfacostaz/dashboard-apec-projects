#!/usr/bin/env python3
"""
Scrape the APEC Project Database approved projects list and export CSV/XLSX files.

This script is the production version of the original Jupyter notebook:
web-scraping-apec-pdb.ipynb

Main changes from the notebook:
- Uses pathlib and paths relative to the repository root.
- Adds command-line arguments for output directory, workers, retries, and delay.
- Writes temporary output files first, validates the dataset, then atomically replaces
  the final CSV/XLSX files.
- Keeps backups of the previous CSV/XLSX files before replacement.
- Avoids notebook-only calls such as display(), cell ordering, or implicit cwd assumptions.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APPROVED_URL = "https://pdb.apec.org/Lists/Projects/Approved%20Projects.aspx"
DETAIL_URL_TEMPLATE = "https://pdb.apec.org/Lists/Projects/DispForm.aspx?ID={id}"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) APEC-Projects-Scraper/1.0"

# Label -> canonical column mapping.
# IMPORTANT: "Project Cost (US$)" belongs only to project_cost_usd.
# Self-funded projects use that field; TILF/Operational projects use
# apec_funding + co_funding_amount + total_project_value instead.
LABEL_ALIASES = {
    "project_title": ["Project Title", "Title"],
    "project_number": ["Project No.", "Project Number"],
    "project_year": ["Project Year"],
    "project_status": ["Project Status"],
    "fund_account": ["Fund Account"],
    "sub_fund": ["Sub-fund"],
    "project_session": ["Project Session"],
    "apec_funding": ["APEC Funding"],
    "co_funding_amount": ["Co-funding Amount", "Co-Funding Amount"],
    "total_project_value": ["Total Project Value"],
    "project_cost_usd": ["Project Cost (US$)"],
    "sponsoring_forum": ["Sponsoring Forum"],
    "topics": ["Topics"],
    "committee": ["Committee"],
    "other_fora_involved": ["Other Fora Involved"],
    "other_non_apec_stakeholders_involved": ["Other Non-APEC Stakeholders Involved"],
    "proposing_economy": ["Proposing Economy(ies)", "Proposing Economy"],
    "co_sponsoring_economies": ["Co-Sponsoring Economies", "Co-sponsoring Economies"],
    "expected_start_date": ["Expected Start Date"],
    "expected_completion_date": ["Expected Completion Date"],
    "organization_1": ["Organization 1"],
    "organization_2": ["Organization 2"],
}

# Final output column order.
OUTPUT_COLUMNS = [
    "project_title",
    "project_number",
    "project_year",
    "sponsoring_forum",
    "proposing_economy",
    "project_status",
    "fund_account",
    "sub_fund",
    "project_session",
    "apec_funding",
    "co_funding_amount",
    "total_project_value",
    "project_cost_usd",
    "topics",
    "committee",
    "other_fora_involved",
    "co_sponsoring_economies",
    "organization_1",
    "organization_2",
    "link",
]

REQUIRED_COLUMNS = ["project_title", "project_number", "project_year", "link"]


@dataclass(frozen=True)
class ScraperConfig:
    """Runtime configuration for the scraper."""

    output_dir: Path
    output_base: str = "apec_approved_projects"
    max_workers: int = 20
    max_retries: int = 3
    retry_backoff: float = 2.0
    list_page_delay: float = 0.4
    timeout: int = 40
    user_agent: str = DEFAULT_USER_AGENT
    keep_backups: bool = True


# ---------------------------------------------------------------------------
# Text and parsing helpers
# ---------------------------------------------------------------------------

def soupify(html: str) -> BeautifulSoup:
    """Parse raw HTML into a BeautifulSoup tree."""
    return BeautifulSoup(html, "lxml")


def norm_text(s: Optional[str]) -> str:
    """NFKD-normalize Unicode, replace typographic chars, and collapse whitespace."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = s.replace("\u2019", "'").replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", s).strip()


def flatten_lookup_array(arr) -> str:
    """Flatten a SharePoint lookup array into a semicolon-separated string."""
    if not isinstance(arr, list):
        return ""
    return "; ".join(
        str(v)
        for obj in arr
        if isinstance(obj, dict)
        for v in [obj.get("lookupValue") or obj.get("LookupValue", "")]
        if v
    )


def clean_label(txt: str) -> str:
    """Strip trailing asterisks and colons from a label string."""
    return re.sub(r"(\s*\*|:)\s*$", "", norm_text(txt))


def element_text(el) -> str:
    """Return normalized inner text of a BeautifulSoup element, or an empty string."""
    return norm_text(el.get_text(" ", strip=True)) if el else ""


def build_alias_index() -> Dict[str, str]:
    """Build a normalized label-to-column reverse index."""
    return {
        re.sub(r"(\s*\*|:)\s*$", "", norm_text(alias)).lower(): col
        for col, aliases in LABEL_ALIASES.items()
        for alias in aliases
    }


ALIAS_INDEX = build_alias_index()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def make_session(user_agent: str) -> requests.Session:
    """Create a requests session with default headers."""
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    return session


def get_with_retries(
    session: requests.Session,
    url: str,
    *,
    max_retries: int,
    retry_backoff: float,
    timeout: int,
    headers: Optional[Dict[str, str]] = None,
) -> requests.Response:
    """GET a URL with retries and linear backoff."""
    last_err: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            response = session.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_err = exc
            if attempt < max_retries:
                wait = retry_backoff * attempt
                print(f"  [RETRY {attempt}/{max_retries}] {url} - {exc} (waiting {wait}s)")
                time.sleep(wait)

    raise RuntimeError(f"Failed to fetch {url}: {last_err}") from last_err


# ---------------------------------------------------------------------------
# List page extraction
# ---------------------------------------------------------------------------

def extract_wpq1_data(html: str) -> Tuple[List[Dict], Optional[str]]:
    """
    Extract the embedded WPQ1ListData JSON from the SharePoint list page.

    Returns:
        rows: List of SharePoint row dictionaries.
        next_href: Relative URL for the next list page, or None on the last page.
    """
    pattern = (
        r"var\s+WPQ1ListData\s*=\s*"
        r"(\{.*?\})"
        r"\s*;\s*var\s+WPQ1SchemaData"
    )

    match = re.search(pattern, html, re.DOTALL)
    if not match:
        print("WPQ1ListData not found on page.")
        return [], None

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        print(f"JSON decode error: {exc}")
        print(match.group(1)[:500])
        return [], None

    return data.get("Row", []), data.get("NextHref")


def fetch_all_approved_rows(config: ScraperConfig) -> List[Dict[str, str]]:
    """
    Paginate through the full Approved Projects list view using SharePoint's NextHref cursor.
    """
    session = make_session(config.user_agent)

    url = APPROVED_URL
    all_records: List[Dict[str, str]] = []
    seen_urls = set()
    page = 1

    while url and url not in seen_urls:
        print(f"[VIEW] Fetching page {page}: {url}")
        seen_urls.add(url)

        response = get_with_retries(
            session,
            url,
            max_retries=config.max_retries,
            retry_backoff=config.retry_backoff,
            timeout=config.timeout,
        )

        rows, next_href = extract_wpq1_data(response.text)
        print(f"  rows in this page: {len(rows)}")

        for row in rows:
            project_id = row.get("ID")
            file_ref = row.get("FileRef", "")

            link = (
                DETAIL_URL_TEMPLATE.format(id=project_id)
                if project_id
                else urljoin("https://pdb.apec.org/", file_ref) if file_ref else ""
            )

            all_records.append({"id": str(project_id or ""), "link": link})

        if not next_href:
            print("  No NextHref -> last page reached.")
            break

        url = urljoin(APPROVED_URL, next_href)
        page += 1
        time.sleep(config.list_page_delay)

    print(f"[VIEW] Total records found: {len(all_records)}")
    return all_records


# ---------------------------------------------------------------------------
# Detail page extraction
# ---------------------------------------------------------------------------

def extract_label_value_pairs(soup: BeautifulSoup) -> Dict[str, str]:
    """
    Extract all label/value pairs from a project detail page.

    Layout variants:
    1. Desktop: div.lfi-fields-wrapper > .input-label + .input-col
    2. Fallback: p.lfi-field-label, then nearby .input-col
    3. Mobile: .pdb-responsive-header paired with .pdb-responsive-content
    """
    pairs: Dict[str, str] = {}

    # 1) Desktop layout.
    for wrap in soup.select("div.lfi-fields-wrapper"):
        label_div = wrap.select_one(".input-label")
        value_div = wrap.select_one(".input-col")

        if not label_div or not value_div:
            continue

        label = clean_label(element_text(label_div))
        if label:
            pairs[label] = element_text(value_div)

    # 2) Fallback layout.
    for label_node in soup.select("p.lfi-field-label"):
        label = clean_label(element_text(label_node))
        if not label or label in pairs:
            continue

        value_div = None
        container = label_node.find_parent(class_=re.compile(r"input-label"))

        if container:
            sibling = container.find_next_sibling(class_=re.compile(r"input-col"))
            if sibling:
                value_div = sibling

        if not value_div:
            steps = 0
            next_node = label_node

            while next_node and steps < 25 and not value_div:
                next_node = next_node.next_element
                steps += 1

                if getattr(next_node, "name", "") and re.search(
                    r"\binput-col\b", " ".join(next_node.get("class", []))
                ):
                    value_div = next_node
                    break

        value = element_text(value_div) if value_div else ""
        if label and value:
            pairs[label] = value

    # 3) Mobile/responsive layout.
    headers = soup.select(".pdb-responsive-header")
    contents = soup.select(".pdb-responsive-content")

    if headers and contents and len(headers) == len(contents):
        for header, content in zip(headers, contents):
            label = clean_label(element_text(header))
            if label and label not in pairs:
                pairs[label] = element_text(content)

    return pairs


def parse_detail(url: str, config: ScraperConfig) -> Dict[str, str]:
    """
    Fetch and parse a project detail page.

    Missing fields default to empty strings. If all attempts fail, the project is
    returned as an empty row so the link is not lost.
    """
    if not url:
        return {col: "" for col in LABEL_ALIASES}

    session = make_session(config.user_agent)

    try:
        response = get_with_retries(
            session,
            url,
            max_retries=config.max_retries,
            retry_backoff=config.retry_backoff,
            timeout=config.timeout,
            headers={"Referer": APPROVED_URL},
        )
    except Exception as exc:
        print(f"  [FAIL] giving up on {url}: {exc}")
        return {col: "" for col in LABEL_ALIASES}

    raw_pairs = extract_label_value_pairs(soupify(response.text))

    output: Dict[str, str] = {}
    for label, value in raw_pairs.items():
        key_norm = clean_label(label).lower()
        if key_norm in ALIAS_INDEX:
            output[ALIAS_INDEX[key_norm]] = value

    for col in LABEL_ALIASES:
        output.setdefault(col, "")

    return output


def fetch_details_parallel(base_records: List[Dict[str, str]], config: ScraperConfig) -> List[Dict[str, str]]:
    """
    Fetch all detail pages concurrently using a thread pool.

    Results are collected in the same order as base_records.
    """
    total = len(base_records)
    results: List[Optional[Dict[str, str]]] = [None] * total
    done = 0

    with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        future_to_idx = {
            executor.submit(parse_detail, row["link"], config): (i, row["link"])
            for i, row in enumerate(base_records)
        }

        for future in as_completed(future_to_idx):
            idx, link = future_to_idx[future]

            try:
                detail = future.result()
            except Exception as exc:
                print(f"  [FAIL] uncaught detail parsing error for {link}: {exc}")
                detail = {col: "" for col in LABEL_ALIASES}

            detail["link"] = link
            results[idx] = detail

            done += 1
            if done % 10 == 0 or done == total:
                print(f"[DETAIL] {done}/{total} pages fetched...")

    return [row if row is not None else {col: "" for col in OUTPUT_COLUMNS} for row in results]


# ---------------------------------------------------------------------------
# Output handling
# ---------------------------------------------------------------------------

def build_database(config: ScraperConfig) -> pd.DataFrame:
    """Scrape the list page and detail pages, then return a normalized DataFrame."""
    base_records = fetch_all_approved_rows(config)

    if not base_records:
        raise RuntimeError("No base records found. The source page may have changed or the request failed.")

    records = fetch_details_parallel(base_records, config)
    df = pd.DataFrame.from_records(records)

    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = (
        df[OUTPUT_COLUMNS]
        .replace("", pd.NA)
        .drop_duplicates(keep="first")
        .reset_index(drop=True)
    )

    return df


def validate_database(df: pd.DataFrame) -> None:
    """Validate the final dataset before replacing production outputs."""
    if df.empty:
        raise ValueError("The scraped dataset is empty.")

    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    if df["link"].isna().all():
        raise ValueError("All project links are missing.")

    # project_number is the closest stable public identifier in the detail data.
    # Some pages may be missing it, so only duplicated non-null values are checked.
    if "project_number" in df.columns:
        duplicated_project_numbers = df["project_number"].dropna().duplicated().sum()
        if duplicated_project_numbers > 0:
            print(f"[WARN] Duplicated project_number values found: {duplicated_project_numbers}")


def write_outputs_safely(df: pd.DataFrame, config: ScraperConfig) -> Tuple[Path, Path]:
    """
    Write temporary CSV/XLSX files, then replace the final outputs after validation.
    """
    config.output_dir.mkdir(parents=True, exist_ok=True)

    final_csv = config.output_dir / f"{config.output_base}.csv"
    final_xlsx = config.output_dir / f"{config.output_base}.xlsx"

    temp_csv = config.output_dir / f"{config.output_base}__new.csv"
    temp_xlsx = config.output_dir / f"{config.output_base}__new.xlsx"

    backup_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_csv = config.output_dir / f"{config.output_base}__backup_{backup_timestamp}.csv"
    backup_xlsx = config.output_dir / f"{config.output_base}__backup_{backup_timestamp}.xlsx"

    df.to_csv(temp_csv, index=False, encoding="utf-8-sig")
    df.to_excel(temp_xlsx, index=False)

    if config.keep_backups:
        if final_csv.exists():
            shutil.copy2(final_csv, backup_csv)
        if final_xlsx.exists():
            shutil.copy2(final_xlsx, backup_xlsx)

    temp_csv.replace(final_csv)
    temp_xlsx.replace(final_xlsx)

    return final_csv, final_xlsx


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    repo_root = Path(__file__).resolve().parents[1] if "__file__" in globals() else Path.cwd()

    parser = argparse.ArgumentParser(
        description="Scrape approved APEC projects and export CSV/XLSX files."
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=repo_root,
        help="Directory where CSV/XLSX outputs will be written. Default: repository root.",
    )
    parser.add_argument(
        "--output-base",
        default="apec_approved_projects",
        help="Base filename for output files, without extension.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=20,
        help="Number of concurrent detail-page fetchers.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum retry attempts per request.",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=2.0,
        help="Retry backoff multiplier in seconds.",
    )
    parser.add_argument(
        "--list-page-delay",
        type=float,
        default=0.4,
        help="Delay between list-page requests in seconds.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=40,
        help="HTTP request timeout in seconds.",
    )
    parser.add_argument(
        "--no-backups",
        action="store_true",
        help="Do not keep timestamped backups of previous CSV/XLSX files.",
    )

    return parser.parse_args()


def main() -> None:
    """Run the full scraping and export pipeline."""
    args = parse_args()

    config = ScraperConfig(
        output_dir=args.output_dir,
        output_base=args.output_base,
        max_workers=args.max_workers,
        max_retries=args.max_retries,
        retry_backoff=args.retry_backoff,
        list_page_delay=args.list_page_delay,
        timeout=args.timeout,
        keep_backups=not args.no_backups,
    )

    print("[START] APEC PDB scraper")
    print(f"[CONFIG] output_dir={config.output_dir}")
    print(f"[CONFIG] output_base={config.output_base}")
    print(f"[CONFIG] max_workers={config.max_workers}")

    df = build_database(config)
    validate_database(df)

    print("Final shape:", df.shape)
    print(df.head(5))

    final_csv, final_xlsx = write_outputs_safely(df, config)

    print(f"[SAVED] {final_csv}")
    print(f"[SAVED] {final_xlsx}")
    print("[DONE] APEC PDB scraper finished successfully")


if __name__ == "__main__":
    main()
