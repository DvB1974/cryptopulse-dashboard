#!/usr/bin/env python3
"""Collect news that is directly about Dutch banks (competitive monitoring) and write data/nl-banks.json.

Source: Google News RSS (hl=nl, gl=NL), one query per bank. Runs in GitHub Actions; Google News
blocks browser-side fetching, so the dashboard reads the JSON this script produces.
Strict rule: an item is kept only if a bank name appears in the HEADLINE.
On failure the existing JSON is kept. Standard library only.
"""
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "nl-banks.json"
DAYS = 30
MAX_ITEMS = 80

# name shown in dashboard, Google News query, headline regex (strict entity match)
BANKS = [
    ("ABN AMRO", '"ABN AMRO"', re.compile(r"ABN\s?AMRO|MeesPierson", re.I)),
    ("ING", '"ING" bank', re.compile(r"\bING\b")),  # case-sensitive on purpose
    ("Rabobank", "Rabobank", re.compile(r"Rabobank", re.I)),
    ("de Volksbank", '"de Volksbank" OR "ASN Bank" OR "SNS Bank" OR RegioBank', re.compile(r"Volksbank|ASN\s?Bank|\bSNS\b|RegioBank", re.I)),
    ("Triodos", "Triodos", re.compile(r"Triodos", re.I)),
    ("Van Lanschot Kempen", '"Van Lanschot" OR "Kempen"', re.compile(r"Van\s?Lanschot|\bKempen\b", re.I)),
    ("bunq", "bunq", re.compile(r"\bbunq\b", re.I)),
    ("Knab", "Knab bank", re.compile(r"\bKnab\b", re.I)),
    ("NIBC", "NIBC", re.compile(r"\bNIBC\b", re.I)),
    ("InsingerGilissen", '"InsingerGilissen" OR "Quintet Private Bank"', re.compile(r"Insinger|Quintet", re.I)),
]

THEMES = [
    ("Crypto & digitaal", re.compile(r"crypto|bitcoin|blockchain|stablecoin|tokeni[sz]|digitale euro|\bMiCA\b|digital asset|digitale activa|cbdc", re.I)),
    ("Private banking", re.compile(r"private bank|vermogensbeheer|vermogend|wealth|beleggen|belegging|family office|vermogen", re.I)),
    ("Resultaten", re.compile(r"winst|resultaat|kwartaal|jaarcijfers|halfjaar|omzet|dividend|inkoop van aandelen|profit|results|quarter|rente-inkomsten", re.I)),
    ("Toezicht", re.compile(r"\bDNB\b|\bAFM\b|\bECB\b|boete|toezicht|witwas|sanctie|rechter|rechtszaak|claim|fine\b|regulator|Kifid", re.I)),
    ("Tarieven", re.compile(r"spaarrente|hypotheekrente|\brente\b|tarie[fv]|kosten|provisie", re.I)),
    ("Strategie & product", re.compile(r"lanceert|introduceert|nieuwe? |app\b|overname|neemt .* over|acquisition|partnership|samenwerking|banen|reorganisatie|ceo|topman|bestuursvoorzitter|benoem|vertrekt|kantoren", re.I)),
]


def gnews(query):
    q = f"{query} when:{DAYS}d"
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "nl", "gl": "NL", "ceid": "NL:nl"})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (CryptoPulse dashboard; NL bank monitor)"})
    xml = urllib.request.urlopen(req, timeout=30).read()
    root = ET.fromstring(xml)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        # Google News appends " - Source" to titles
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)].strip()
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:
            continue
        yield {"title": title, "link": it.findtext("link") or "", "source": source, "ts": int(dt.timestamp() * 1000)}


def norm(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()[:70]


def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)
    items, seen, errors = [], set(), 0
    for name, query, rx in BANKS:
        try:
            for it in gnews(query):
                if it["ts"] < cutoff.timestamp() * 1000 or not rx.search(it["title"]):
                    continue
                key = norm(it["title"])
                if key in seen:
                    continue
                seen.add(key)
                it["banks"] = [n for n, _, r in BANKS if r.search(it["title"])]
                it["themes"] = [t for t, r in THEMES if r.search(it["title"])]
                items.append(it)
        except Exception as exc:
            errors += 1
            print(f"WARN {name}: {exc}", file=sys.stderr)
    if errors == len(BANKS) or not items:
        print("no data fetched; keeping existing file", file=sys.stderr)
        return
    items.sort(key=lambda x: x["ts"], reverse=True)
    data = {
        "source": "Google Nieuws NL, per bank gezocht; alleen berichten met de bank in de kop",
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": DAYS,
        "banks": [n for n, _, _ in BANKS],
        "items": items[:MAX_ITEMS],
    }
    old = json.loads(OUT.read_text()) if OUT.exists() else {}
    if old.get("items") == data["items"]:
        print("no change")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    print("updated", OUT, len(data["items"]), "items")


if __name__ == "__main__":
    main()
