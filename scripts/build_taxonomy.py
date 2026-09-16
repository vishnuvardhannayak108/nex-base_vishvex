"""Build the packaged discovery taxonomy from the raw BLS/NAICS/O*NET data.

Reads:
  data/processed/soc_industry_mapping.csv   SOC code -> client industry
  data/raw/onet/occupation_data.csv         SOC code -> canonical title
  data/raw/onet/Alternate Titles.txt        SOC code -> real-world job titles
  data/raw/onet/sample_of_reported_titles.csv

Writes:
  nexbase/discovery/data/search_terms.csv
      tier,client_industry,soc_code,soc_major,soc_title,search_term,rank

  tier is CORE when the SOC maps to one of the client's ten industries, and
  EXPLORATION otherwise. SOC major group 15-0000 (Computer and Mathematical)
  is dropped entirely.

Run from the repository root:
    python scripts/build_taxonomy.py

The output is committed so the runtime package never depends on the multi-MB
raw O*NET extracts.
"""
from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "nexbase" / "discovery" / "data" / "search_terms.csv"

#: Titles above this many words make poor job-board queries.
MAX_WORDS = 5
MAX_PER_SOC = 18

#: SOC major groups never searched. 15-0000 is Computer and Mathematical
#: Occupations, excluded by explicit client instruction.
EXCLUDED_MAJOR_GROUPS = ("15-",)

#: Titles that are too generic to be useful as a standalone board query, or
#: that describe the person we want to *contact* rather than the role a company
#: is hiring for.
_STOP_TITLES = {
    "manager", "supervisor", "director", "engineer", "technician", "operator",
    "specialist", "coordinator", "analyst", "assistant", "associate", "clerk",
    "worker", "helper", "laborer", "owner", "president", "officer", "consultant",
}

_BAD_CHARS = re.compile(r"[^\w\s&/'\-.,()]")
_PARENS = re.compile(r"\s*\([^)]*\)")
_WS = re.compile(r"\s+")


def clean_title(raw: str) -> str | None:
    t = _PARENS.sub("", raw or "")
    t = _BAD_CHARS.sub(" ", t)
    t = _WS.sub(" ", t).strip(" ,.-/")
    if not t or len(t) < 4:
        return None
    words = t.split()
    if len(words) > MAX_WORDS:
        return None
    if t.lower() in _STOP_TITLES:
        return None
    if any(ch.isdigit() for ch in t):
        return None
    return t


def soc6(onet_code: str) -> str:
    """`11-1011.00` -> `11-1011`."""
    return (onet_code or "").split(".")[0].strip()


def main() -> int:
    mapping_path = ROOT / "data" / "processed" / "soc_industry_mapping.csv"
    occ_path = ROOT / "data" / "raw" / "onet" / "occupation_data.csv"
    alt_path = ROOT / "data" / "raw" / "onet" / "Alternate Titles.txt"
    rep_path = ROOT / "data" / "raw" / "onet" / "sample_of_reported_titles.csv"

    for p in (mapping_path, occ_path, alt_path, rep_path):
        if not p.exists():
            print(f"missing input: {p}", file=sys.stderr)
            return 1

    # SOC -> (tier, client industry). CORE rows map to a client industry;
    # EXPLORATION rows are the rest of the occupation universe, kept so the
    # planner can spend its exploration budget outside the core sectors.
    soc_industry: dict[str, str] = {}
    soc_tier: dict[str, str] = {}
    with mapping_path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            code = row["soc_code"].strip()
            if code.startswith(EXCLUDED_MAJOR_GROUPS):
                continue
            if row.get("status") == "MAPPED":
                soc_industry[code] = row["industry_or_reason"].strip()
                soc_tier[code] = "CORE"
            else:
                soc_industry[code] = ""
                soc_tier[code] = "EXPLORATION"

    # SOC -> canonical occupation title.
    soc_title: dict[str, str] = {}
    with occ_path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            code = soc6(row.get("O*NET-SOC Code", ""))
            if code in soc_industry and code not in soc_title:
                soc_title[code] = (row.get("Title") or "").strip()

    # SOC -> candidate search terms, canonical title ranked first.
    candidates: dict[str, list[str]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)

    def add(code: str, title: str | None) -> None:
        if not code or code not in soc_industry:
            return
        cleaned = clean_title(title or "")
        if not cleaned:
            return
        key = cleaned.lower()
        if key in seen[code]:
            return
        seen[code].add(key)
        candidates[code].append(cleaned)

    for code, title in soc_title.items():
        add(code, title)

    with rep_path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            add(soc6(row.get("O*NET-SOC Code", "")), row.get("Reported Job Title"))

    with alt_path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            add(soc6(row.get("O*NET-SOC Code", "")), row.get("Alternate Title"))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["tier", "client_industry", "soc_code", "soc_major",
                         "soc_title", "search_term", "rank"])
        for code in sorted(candidates):
            major = code.split("-")[0] + "-0000"
            for rank, term in enumerate(candidates[code][:MAX_PER_SOC]):
                writer.writerow([soc_tier[code], soc_industry[code], code, major,
                                 soc_title.get(code, ""), term, rank])
                written += 1

    industries = sorted({soc_industry[c] for c in candidates if soc_industry[c]})
    core = sum(1 for c in candidates if soc_tier[c] == "CORE")
    print(f"wrote {written} search terms across {len(candidates)} SOC codes -> {OUT}")
    print(f"  CORE SOCs: {core}  EXPLORATION SOCs: {len(candidates) - core}")
    print(f"  excluded major groups: {', '.join(EXCLUDED_MAJOR_GROUPS)}")
    print(f"  client industries: {len(industries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
