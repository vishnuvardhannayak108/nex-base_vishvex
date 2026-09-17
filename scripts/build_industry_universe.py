"""Build the full eligible industry universe from BLS OEWS + NAICS.

The discovery taxonomy previously had an industry dimension only for the
client's ten core industries; everything else was grouped by SOC *major group*,
so exploration explored occupations rather than industries.

This script derives the real universe from data already in the repository:

  data/raw/bls_oews/nat3d_M2025_dl.xlsx   NAICS 3-digit x occupation x employment
  data/processed/naics_to_industry.csv    NAICS 3-digit -> client core industry
  data/raw/naics/2022_NAICS_Structure.xlsx every NAICS code and title, 2-6 digits

Writes:
  nexbase/discovery/data/industry_occupations.csv
      naics3,naics_title,tier,client_industry,soc_code,employment,rank
  nexbase/discovery/data/naics_titles.csv
      naics_code,naics_title,client_industry
      (classifies a source's industry label; client_industry is the NexBase
      sector of the code's 3-digit subsector per naics_to_industry.csv, or of a
      2-digit sector only when every subsector in it maps to that one sector)

``tier`` is CORE when the NAICS subsector maps to one of the client's usual
industries, NON_CORE otherwise. Every NAICS subsector BLS publishes is
included, so the exploration budget can reach the whole universe rather than a
hand-picked list. Occupations in SOC major group 15-0000 are dropped.

Run from the repository root:
    python scripts/build_industry_universe.py
"""
from __future__ import annotations

import csv
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "nexbase" / "discovery" / "data" / "industry_occupations.csv"
NAICS_TITLES_OUT = ROOT / "nexbase" / "discovery" / "data" / "naics_titles.csv"

#: Occupations per industry, ranked by employment in that industry.
TOP_OCCUPATIONS_PER_INDUSTRY = 40

#: SOC major groups never searched (client instruction).
EXCLUDED_SOC_PREFIXES = ("15-",)

#: OEWS aggregate rows that are not real subsectors.
_SKIP_TITLES = ("Cross-industry", "Total, all industries")


def main() -> int:
    import pandas as pd

    oews = ROOT / "data" / "raw" / "bls_oews" / "nat3d_M2025_dl.xlsx"
    mapping = ROOT / "data" / "processed" / "naics_to_industry.csv"
    for p in (oews, mapping):
        if not p.exists():
            print(f"missing input: {p}", file=sys.stderr)
            return 1

    core: dict[str, str] = {}
    with mapping.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            core[str(row["naics_3digit"]).strip()] = row["client_industry"].strip()

    df = pd.read_excel(oews)
    df["NAICS"] = df["NAICS"].astype(str).str.strip()
    df["OCC_CODE"] = df["OCC_CODE"].astype(str).str.strip()

    # Detailed occupations only, and never the excluded major groups.
    df = df[df["O_GROUP"].astype(str).str.lower() == "detailed"]
    df = df[~df["OCC_CODE"].str.startswith(EXCLUDED_SOC_PREFIXES)]
    df = df[~df["NAICS_TITLE"].astype(str).str.startswith(_SKIP_TITLES)]

    df["TOT_EMP"] = pd.to_numeric(df["TOT_EMP"], errors="coerce").fillna(0)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    industries: set[str] = set()
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["naics3", "naics_title", "tier", "client_industry",
                         "soc_code", "employment", "rank"])
        for naics, group in df.groupby("NAICS"):
            title = str(group["NAICS_TITLE"].iloc[0]).strip()
            tier = "CORE" if naics in core else "NON_CORE"
            client_industry = core.get(naics, "")
            industries.add(naics)
            top = group.sort_values("TOT_EMP", ascending=False).head(
                TOP_OCCUPATIONS_PER_INDUSTRY
            )
            for rank, (_, row) in enumerate(top.iterrows()):
                writer.writerow([naics, title, tier, client_industry,
                                 row["OCC_CODE"], int(row["TOT_EMP"]), rank])
                written += 1

    n_core = sum(1 for n in industries if n in core)
    print(f"wrote {written} rows across {len(industries)} NAICS subsectors -> {OUT}")
    print(f"  CORE subsectors     : {n_core}")
    print(f"  NON_CORE subsectors : {len(industries) - n_core}")
    mfg = sorted(n for n in industries if n[:2] in ("31", "32", "33"))
    print(f"  manufacturing subsectors (31-33): {len(mfg)}")
    return write_naics_titles(pd, core)


def write_naics_titles(pd, core: dict[str, str]) -> int:
    """Every 2022 NAICS code with its title and NexBase sector."""
    structure = ROOT / "data" / "raw" / "naics" / "2022_NAICS_Structure.xlsx"
    if not structure.exists():
        print(f"missing input: {structure}", file=sys.stderr)
        return 1
    df = pd.read_excel(structure, header=2, dtype=str)
    df = df.rename(columns=lambda c: str(c).strip())
    rows = []
    for code, title in zip(df["2022 NAICS Code"], df["2022 NAICS Title"]):
        if not isinstance(code, str) or not isinstance(title, str):
            continue
        title = title.strip()
        if title.endswith("T"):
            title = title[:-1].rstrip()
        rows.append((code.strip(), title))

    subsectors = [code for code, _ in rows if len(code) == 3]

    def sector_of(code: str) -> str:
        if code.isdigit() and len(code) >= 3:
            return core.get(code[:3] + "000", "")
        # A 2-digit sector ("23", or a range such as "31-33").
        first, _, last = code.partition("-")
        prefixes = {str(n) for n in range(int(first), int(last or first) + 1)}
        sectors = {core.get(s + "000", "") for s in subsectors if s[:2] in prefixes}
        return sectors.pop() if len(sectors) == 1 else ""

    with NAICS_TITLES_OUT.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["naics_code", "naics_title", "client_industry"])
        writer.writerows((code, title, sector_of(code)) for code, title in rows)
    print(f"wrote {len(rows)} NAICS codes -> {NAICS_TITLES_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
