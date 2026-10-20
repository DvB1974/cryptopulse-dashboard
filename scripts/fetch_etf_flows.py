#!/usr/bin/env python3
"""Fetch daily US spot BTC/ETH ETF net flows from Farside and write data/etf-flows.json.

Runs in GitHub Actions (see .github/workflows/etf-flows.yml). Standard library only.
On any failure the existing JSON is left untouched, so the dashboard keeps the last good data.
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "etf-flows.json"
PAGES = {"btc": "https://farside.co.uk/btc/", "eth": "https://farside.co.uk/eth/"}
DATE_RE = re.compile(r"^\d{2} [A-Z][a-z]{2} \d{4}$")


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell, self._in_cell = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._in_cell, self._cell = True, []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._in_cell = False
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in_cell:
            self._cell.append(data)


def parse_num(txt):
    t = txt.replace(",", "").strip()
    if t in ("", "-"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def fetch_rows(url, keep=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (CryptoPulse dashboard; daily ETF flow refresh)"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    p = TableParser()
    p.feed(html)
    out = []
    for row in p.rows:
        if len(row) >= 3 and DATE_RE.match(row[0]):
            total = parse_num(row[-1])
            if total is not None:
                out.append({"date": row[0], "total": total})
    if not out:
        raise RuntimeError(f"no flow rows parsed from {url}")
    return out[-keep:]


def main():
    data = json.loads(OUT.read_text()) if OUT.exists() else {}
    changed = False
    for key, url in PAGES.items():
        try:
            rows = fetch_rows(url)
            if rows != data.get(key):
                data[key] = rows
                changed = True
        except Exception as exc:  # keep last good data
            print(f"WARN {key}: {exc}", file=sys.stderr)
    if changed:
        data["source"] = "Farside Investors (farside.co.uk), US$ mln, netto dagflow per handelsdag"
        data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data.pop("note", None)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(data, indent=2) + "\n")
        print("updated", OUT)
    else:
        print("no change")


if __name__ == "__main__":
    main()
