#!/usr/bin/env python3
"""
compare_rebuild.py: compare a rebuilt data/pmqs against a saved copy of the
previous version, so that a full rebuild can be checked before it's trusted.

Usage:
  python scripts/compare_rebuild.py /tmp/old_pmqs 1961-1980

Writes data/reports/rebuild_changes_<label>.csv, where the optional label
names the years run, so a rebuild done in chunks keeps every report. One row
per date whose exchanges changed: a session gained or lost, a different number of exchanges, or the
same number with different questioners. Unchanged dates are left out.
"""
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

NEW_DIR = Path("data/pmqs")
REPORT_DIR = Path("data/reports")


def by_date(folder):
    days = defaultdict(list)
    for path in sorted(Path(folder).glob("*.csv")):
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                days[r["date"]].append(r["q_name"])
    return days


def main():
    if len(sys.argv) < 2 or not Path(sys.argv[1]).is_dir():
        print("No previous copy given, nothing to compare")
        return
    label = sys.argv[2].strip("-") if len(sys.argv) > 2 else ""
    report = REPORT_DIR / f"rebuild_changes_{label or 'latest'}.csv"
    old, new = by_date(sys.argv[1]), by_date(NEW_DIR)
    changes = []
    for date in sorted(set(old) | set(new)):
        o, n = old.get(date, []), new.get(date, [])
        if o == n:
            continue
        if not o:
            kind = "session added"
        elif not n:
            kind = "session lost"
        elif len(o) != len(n):
            kind = "exchange count changed"
        else:
            kind = "questioners changed"
        changes.append({"date": date, "change": kind,
                        "old_exchanges": len(o), "new_exchanges": len(n)})
    report.parent.mkdir(parents=True, exist_ok=True)
    with open(report, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "change", "old_exchanges", "new_exchanges"])
        w.writeheader()
        w.writerows(changes)
    totals = Counter(c["change"] for c in changes)
    print(f"Sessions before: {len(old)}, after: {len(new)}")
    print("Changes: " + (", ".join(f"{k}: {v}" for k, v in totals.most_common()) or "none"))
    print(f"Details in {report}")


if __name__ == "__main__":
    main()
