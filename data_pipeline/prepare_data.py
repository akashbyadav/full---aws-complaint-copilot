"""Download official public data and prepare a small, reproducible teaching cohort.

The 2025 archive is historical. We replay it in two batches to demonstrate daily
arrivals. Batch 3 contains separately labelled teaching fixtures; it never alters
the original downloaded archive. No AWS account is required for this script.
"""

import argparse
import copy
import csv
import hashlib
import io
import json
import random
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

# The source file lives in data_pipeline/; all data/config paths are rooted
# at the parent project directory rather than the shell's working directory.
ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_URL = (
    "https://files.consumerfinance.gov/f/documents/"
    "CCDB_Export_9_January_2025_through_February_2025.zip"
)
ARCHIVE_PAGE = (
    "https://www.consumerfinance.gov/foia-requests/foia-electronic-reading-room/"
    "cfpb-consumer-complaint-database-narratives-archive/"
)
GUIDANCE_HOME = "https://www.consumerfinance.gov/consumer-tools/credit-cards/"


def download(url, path):
    """Download a public source once and preserve the cached original.

    Args:
        url: Official public archive or guidance-page URL.
        path: Local destination underneath data/source/.

    A .part file prevents an interrupted download from looking complete. Rename
    it only after the whole response succeeds. Existing originals are reused,
    making later preparation runs possible without internet access.
    """
    if path.exists():
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "curl/8.7.1",
            "Accept": "text/html,application/zip,*/*",
        },
    )
    temporary = path.with_suffix(path.suffix + ".part")

    try:
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            temporary.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise
        # The public website sometimes rejects Python's HTTP client while serving
        # the same public page to curl. No login or protected endpoint is involved.
        subprocess.run(
            [
                "curl",
                "--location",
                "--fail",
                "--retry",
                "2",
                "--max-time",
                "120",
                url,
                "--output",
                str(temporary),
            ],
            check=True,
        )
    # Only a complete download becomes a reusable cached source.
    temporary.replace(path)


def save_json(path, value):
    """Write an indented UTF-8 JSON artifact, creating its parent folder.

    Prepared batches and guidance records are deliberately human-readable.
    Keeping Unicode avoids replacing the source text with escaped characters.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def article_text(html):
    """Extract readable article text from downloaded HTML.

    Prefer the main/article region over the full page. Remove navigation, forms,
    scripts and styles before collecting nonempty lines. This prevents repeated
    menu text from dominating the passages later embedded for retrieval.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("main") or soup.select_one("article") or soup.body

    for element in body.select("nav, footer, header, script, style, form"):
        element.decompose()

    lines = [line.strip() for line in body.get_text("\n").splitlines() if line.strip()]
    return "\n".join(lines)


def prepare_guidance():
    """Build the saved articles used by ingestion and the live-guidance tool.

    Discover real article links from the cached official credit-card page,
    rather than inventing URLs. Stable URL hashes identify the same document
    across reruns. Save each title, URL and article body together in
    data/guidance/articles.json. Ingestion reads the saved text, while the live
    tool uses the same record's URL to fetch the current page when asked.

    Returns:
        A list of extracted article dictionaries, including source URLs.
    """
    homepage = ROOT / "data/source/credit-cards.html"
    download(GUIDANCE_HOME, homepage)
    soup = BeautifulSoup(homepage.read_text(), "html.parser")
    links = {}

    # Accept actual article URLs only; a general navigation index is not an
    # article and would create a low-value retrieval document.
    for anchor in soup.select('a[href*="/ask-cfpb/"]'):
        url = urllib.request.urljoin(GUIDANCE_HOME, anchor["href"]).split("#")[0]
        is_official_site = (
            urllib.parse.urlparse(url).hostname == "www.consumerfinance.gov"
        )
        is_article = re.search(r"-en-\d+/$", url)

        if is_official_site and is_article:
            links.setdefault(url, anchor.get_text(" ", strip=True))
    records = []

    def article_priority(item):
        """Prefer dispute/refund/cancellation topics, then sort by source URL.

        Sorting keeps the selected guidance set stable across offline rebuilds.
        The title does not participate in the key, so a label change alone does
        not unexpectedly select a different article.
        """
        url, title = item
        preferred_topic = any(word in url for word in ("dispute", "refund", "cancel"))
        priority = 0 if preferred_topic else 1
        return priority, url

    ordered = sorted(links.items(), key=article_priority)

    for url, title in ordered[:12]:
        document_id = "guidance-" + hashlib.sha256(url.encode()).hexdigest()[:12]
        source = ROOT / "data/source" / (document_id + ".html")
        download(url, source)
        text = article_text(source.read_text())
        if len(text) < 150:
            raise ValueError("Article extraction was unexpectedly short: " + url)

        records.append(
            {
                "document_id": document_id,
                "title": title,
                "url": url,
                "text": text,
            }
        )

    if not records:
        raise ValueError("No guidance articles found; inspect the saved CFPB page.")
    save_json(ROOT / "data/guidance/articles.json", records)

    return records


def normalize(row):
    """Map one original CSV row into the smaller incoming-record model.

    Preserve the source's complaint ID, company labels and narrative text.
    Missing state is labelled UNKNOWN. Dates are normalized to their ISO date
    component, and records start as real, active source rows.

    record_version is added later when the record is assigned to a replay batch.
    Classroom correction/deletion flags are applied only to explicit copies.
    """
    return {
        "complaint_id": row["Complaint ID"].strip(),
        "date_received": row["Date received"].strip()[:10],
        "company": row["Company"].strip(),
        "product": row["Product"].strip(),
        "issue": row["Issue"].strip(),
        "state": row["State"].strip() or "UNKNOWN",
        "company_response": row["Company response to consumer"].strip(),
        "timely_response": row["Timely response?"].strip(),
        "narrative": row["Consumer complaint narrative"].strip(),
        "source_url": ARCHIVE_PAGE,
        "is_demo_fixture": False,
        "is_deleted": False,
    }


def main():
    """Prepare the source cohort, three replay batches and saved guidance.

    Steps:
        1. Read the official CSV directly from its cached ZIP.
        2. Filter real credit-card complaints and sample with seed 42.
        3. Split original records into two arrival batches.
        4. Create clearly labelled correction/deletion fixtures in batch 3.
        5. Extract official guidance and record source/count provenance.

    This command runs locally and makes no AWS requests. It downloads missing
    source files into data/source/ and reuses them on subsequent runs. Guidance
    pages may have changed since the supplied article records were prepared.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--size",
        type=int,
        default=1200,
        help="Small reproducible cohort; default 1,200.",
    )
    args = parser.parse_args()

    if args.size < 100:
        raise ValueError("Use at least 100 records for the three-batch demonstration.")

    # 1. Load the original archive. Reading CSV directly from ZIP avoids
    # extracting its much larger uncompressed contents onto the laptop.
    archive = ROOT / "data/source/cfpb-2025-01-02.zip"
    download(ARCHIVE_URL, archive)
    csv.field_size_limit(10_000_000)

    # Key by stable complaint ID so the cohort contains distinct records.
    # Product totals are retained in the manifest to explain our filtering.
    rows = {}
    products = Counter()

    with zipfile.ZipFile(archive) as zipped:
        name = next(n for n in zipped.namelist() if n.lower().endswith(".csv"))
        with zipped.open(name) as stream:
            for row in csv.DictReader(
                io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")
            ):
                products[row["Product"]] += 1
                if "credit card" in row["Product"].lower():
                    cleaned = normalize(row)
                    rows[cleaned["complaint_id"]] = cleaned

    # 2. Sort before sampling: a fixed seed alone would not make the sample
    # reproducible if the input iteration order changed.
    population = sorted(rows.values(), key=lambda r: r["complaint_id"])
    if len(population) < args.size:
        raise ValueError("Requested cohort exceeds available credit-card records.")
    selected = random.Random(42).sample(population, args.size)
    selected.sort(key=lambda r: (r["date_received"], r["complaint_id"]))

    # 3. Replay 80% of records on day one and the remaining 20% on day two.
    # Source dates remain historical dates; batch sequence models arrival.
    cut = int(args.size * 0.8)
    batch1 = [dict(r, record_version=1) for r in selected[:cut]]
    batch2 = [dict(r, record_version=2) for r in selected[cut:]]

    # 4. Make teaching changes on deep copies of real records. The downloaded
    # original and first two batches remain unchanged.
    # Correction examples use clearly labelled text, so no teaching modification
    # can be mistaken for a statement submitted by an actual consumer.
    fixture_source = [r for r in batch1 if r["narrative"]][:3]
    corrections = []

    for row in fixture_source:
        changed = copy.deepcopy(row)
        changed.update(record_version=3, is_demo_fixture=True)
        correction_notice = (
            "[CLASSROOM CORRECTION FIXTURE: NOT AN ORIGINAL CONSUMER STATEMENT]\n"
            "The classroom demonstration record now contains the phrase "
            "ORCHID-CORRECTION.\n\n"
        )
        changed["narrative"] = correction_notice + row["narrative"]
        corrections.append(changed)

    # A tombstone is still a complete record. The current-view SQL ranks
    # its newer version first and only then filters out deleted records.
    deleted = copy.deepcopy(batch1[-1])
    deleted.update(record_version=3, is_demo_fixture=True, is_deleted=True)
    for number, records in ((1, batch1), (2, batch2), (3, corrections + [deleted])):
        save_json(
            ROOT / "data/batches" / ("batch-%03d.json" % number),
            {
                "batch_id": "batch-%03d" % number,
                "sequence": number,
                "description": (
                    "Archived real records"
                    if number < 3
                    else "Labelled classroom correction/deletion fixtures"
                ),
                "records": records,
            },
        )

    # 5. Save source provenance and expected active counts so you can reconcile
    # each ingestion run against the records in its input batch.
    articles = prepare_guidance()
    manifest = {
        "archive_url": ARCHIVE_URL,
        "archive_page": ARCHIVE_PAGE,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_csv": name,
        "archive_product_counts": dict(products),
        "matching_credit_card_records": len(population),
        "cohort_size": len(selected),
        "sampling": (
            "Seed 42 sample without replacement, then sort by source " "date and ID"
        ),
        "source_date_min": selected[0]["date_received"],
        "source_date_max": selected[-1]["date_received"],
        "narratives_in_cohort": sum(bool(r["narrative"]) for r in selected),
        "expected_active_counts": {
            "batch-001": len(batch1),
            "batch-002": len(selected),
            "batch-003": len(selected) - 1,
        },
        "fixture_correction_ids": [r["complaint_id"] for r in corrections],
        "fixture_deleted_id": deleted["complaint_id"],
        "guidance_count": len(articles),
        "scope": (
            "Historical sample, not a current complete population. "
            "Daily arrivals are replayed."
        ),
    }
    save_json(ROOT / "data/manifest.json", manifest)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
