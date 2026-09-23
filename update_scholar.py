#!/usr/bin/env python3
"""Fetch this public Scholar profile once; write JSON only after validation.

Uses Python's standard library. If SERPAPI_KEY is configured as an Actions
secret, use SerpApi instead of requesting the public profile directly.
Never retries blocked requests or attempts to solve CAPTCHA challenges.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROFILE_ID = "vbTD81EAAAAJ"
PROFILE_URL = "https://scholar.google.com/citations?user=" + PROFILE_ID + "&hl=en"
OUTPUT = Path("scholar-metrics.json")


class MetricsTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.table_depth = 0
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table":
            if self.in_table:
                self.table_depth += 1
            elif attributes.get("id") == "gsc_rsb_st":
                self.in_table = True
                self.table_depth = 1
        if not self.in_table:
            return
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.in_table and self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag in ("td", "th") and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth == 0:
                self.in_table = False


def count(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)", value.strip()):
        return int(value.strip().replace(",", ""))
    raise ValueError("Missing or invalid citation metric; existing data was not changed.")


def validate(citations, h_index):
    citations, h_index = count(citations), count(h_index)
    if h_index * h_index > citations:
        raise ValueError("Inconsistent metrics; existing data was not changed.")
    return citations, h_index


def parse_profile(html):
    table = MetricsTable()
    table.feed(html)
    # Explicitly require the all-time column. Never substitute recent metrics.
    if not any(len(row) >= 3 and row[1].lower() == "all" for row in table.rows):
        raise ValueError("Scholar metrics table unavailable. The request may be blocked or the profile may be private.")
    metrics = {}
    for row in table.rows:
        if len(row) < 3:
            continue
        label = row[0].lower().replace("\u2011", "-").replace("\u2010", "-")
        if label == "citations":
            metrics["citations"] = row[1]
        elif label == "h-index":
            metrics["h_index"] = row[1]
    return validate(metrics.get("citations"), metrics.get("h_index"))


def parse_api(payload):
    if payload.get("error"):
        raise ValueError("SerpApi returned an error. Check your API quota and account.")
    if payload.get("search_parameters", {}).get("author_id") != PROFILE_ID:
        raise ValueError("Unexpected Scholar profile in API response.")
    metrics = {}
    for row in payload.get("cited_by", {}).get("table", []):
        for key in ("citations", "h_index"):
            if key in row:
                metrics[key] = row[key].get("all")
    return validate(metrics.get("citations"), metrics.get("h_index"))


def fetch_text(url):
    request = Request(url, headers={
        "User-Agent": "ResearchHomepageMetrics/1.0",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urlopen(request, timeout=45) as response:
        return response.read(2_000_000).decode("utf-8")


def main():
    key = os.environ.get("SERPAPI_KEY", "").strip()
    now = datetime.now(timezone.utc)
    updated_at = now.isoformat().replace("+00:00", "Z")
    if key:
        query = urlencode({
            "engine": "google_scholar_author",
            "author_id": PROFILE_ID,
            "hl": "en",
            "api_key": key,
        })
        payload = json.loads(fetch_text("https://serpapi.com/search.json?" + query))
        citations, h_index = parse_api(payload)
        # Use the API result's creation time when a cached response is returned.
        created_at = payload.get("search_metadata", {}).get("created_at")
        if created_at:
            timestamp = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
            updated_at = timestamp.isoformat().replace("+00:00", "Z")
        provider = "SerpApi"
    else:
        citations, h_index = parse_profile(fetch_text(PROFILE_URL))
        provider = "Google Scholar public profile"

    data = {
        "profile_id": PROFILE_ID,
        "source": "Google Scholar",
        "source_url": PROFILE_URL,
        "provider": provider,
        "citations": citations,
        "h_index": h_index,
        "updated_at": updated_at,
    }
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT)
    print(f"Verified all-time metrics: citations={citations}, h-index={h_index}.")


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        # Do not log request URLs: an API key can be embedded in the query string.
        print(f"Metrics request failed (HTTP {error.code}); previous data was preserved.", file=sys.stderr)
        sys.exit(1)
    except URLError:
        print("Metrics request failed due to a network error; previous data was preserved.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, KeyError, TypeError, TimeoutError, OSError):
        print("Metrics could not be verified; previous data was preserved. Check profile visibility, service availability, and optional SERPAPI_KEY.", file=sys.stderr)
        sys.exit(1)
