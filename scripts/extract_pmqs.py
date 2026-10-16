#!/usr/bin/env python3
"""
extract_pmqs.py: build a dataset of PMQs question/answer exchanges from
mySociety's ParlParse XML (TheyWorkForYou), 1979 onwards.
 
Output (one CSV per year):
  data/pmqs/<year>.csv          one row per question/answer exchange
  data/diagnostics/<year>.csv   one row per day checked, for sanity-checking
 
Usage:
  python scripts/extract_pmqs.py              current year only
  python scripts/extract_pmqs.py 1979 1989    inclusive range of years
  add --force to redo years that already have a CSV
 
Past years whose CSV already exists are skipped; delete the file to redo one.
Standard library only, so no installs are needed.
"""
import csv
import datetime as dt
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
 
BASE = "https://www.theyworkforyou.com/pwdata/scrapedxml/debates/"
PEOPLE_URL = "https://raw.githubusercontent.com/mysociety/parlparse/master/members/people.json"
OUT_DIR = Path("data/pmqs")
DIAG_DIR = Path("data/diagnostics")
CACHE = Path("data/cache")
HEADERS = {"User-Agent": "pmqs-honeymoon-clock (academic research, LSE)"}
 
# (surname, full name, start inclusive, end exclusive)
PRIME_MINISTERS = [
    ("Thatcher", "Margaret Thatcher", "1979-05-04", "1990-11-28"),
    ("Major", "John Major", "1990-11-28", "1997-05-02"),
    ("Blair", "Tony Blair", "1997-05-02", "2007-06-27"),
    ("Brown", "Gordon Brown", "2007-06-27", "2010-05-11"),
    ("Cameron", "David Cameron", "2010-05-11", "2016-07-13"),
    ("May", "Theresa May", "2016-07-13", "2019-07-24"),
    ("Johnson", "Boris Johnson", "2019-07-24", "2022-09-06"),
    ("Truss", "Liz Truss", "2022-09-06", "2022-10-25"),
    ("Sunak", "Rishi Sunak", "2022-10-25", "2024-07-05"),
    ("Starmer", "Keir Starmer", "2024-07-05", "9999-12-31"),
]
# Parties in government. Matched with startswith against ParlParse party
# names, so "Labour" covers "Labour/Co-operative" but not the SDLP.
GOVERNMENTS = [
    ("1979-05-04", "1997-05-02", ["Conservative"]),
    ("1997-05-02", "2010-05-11", ["Labour"]),
    ("2010-05-11", "2015-05-08", ["Conservative", "Liberal Democrat"]),
    ("2015-05-08", "2024-07-05", ["Conservative"]),
    ("2024-07-05", "9999-12-31", ["Labour"]),
]
MAIN_OPPOSITION = [
    ("1979-05-04", "1997-05-02", "Labour"),
    ("1997-05-02", "2010-05-11", "Conservative"),
    ("2010-05-11", "2024-07-05", "Labour"),
    ("2024-07-05", "9999-12-31", "Conservative"),
]
 
PM_HEADING = re.compile(r"prime minister|engagements", re.I)
NOT_PM_HEADING = re.compile(r"deputy prime minister", re.I)
SPEAKER_NAME = re.compile(r"\bspeaker\b", re.I)
FORMULA = re.compile(
    r"official engagements|engagements for (monday|tuesday|wednesday|thursday|friday)", re.I)
QUESTION_NUMBER = re.compile(r"^\s*(Q\d+\.?\s*)?(\[\d+\]\s*)?")
 
_index_html = None
 
 
def fetch(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(10 * (attempt + 1))
 
 
def lookup(table, date):
    for start, end, value in table:
        if start <= date < end:
            return value
    return None
 
 
def pm_for(date):
    for surname, full, start, end in PRIME_MINISTERS:
        if start <= date < end:
            return surname, full
    return None, None
 
 
def text_of(el):
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()
 
 
def load_people():
    path = CACHE / "people.json"
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fetch(PEOPLE_URL))
    data = json.loads(path.read_text(encoding="utf-8"))
    orgs = {o["id"]: o.get("name", o["id"]) for o in data.get("organizations", [])}
    member_to_person, by_person = {}, {}
    for m in data.get("memberships", []):
        pid = m.get("person_id")
        if not pid:
            continue
        member_to_person[m.get("id", "")] = pid
        org = m.get("on_behalf_of_id")
        party = orgs.get(org, org) or ""
        by_person.setdefault(pid, []).append(
            (m.get("start_date", "0000"), m.get("end_date", "9999-12-31"), party))
    return member_to_person, by_person
 
 
def party_on(by_person, pid, date):
    for start, end, party in by_person.get(pid, []):
        if party and start <= date <= end:
            return party
    return ""
 
 
def day_files(year):
    """Latest version of each Tuesday-Thursday debates file in a year.
    PMQs has been on Wednesdays since 1997 and Tuesdays/Thursdays before."""
    global _index_html
    if _index_html is None:
        _index_html = fetch(BASE).decode("utf-8", "replace")
    latest = {}
    pattern = rf'href="(debates({year}-\d\d-\d\d)([a-z])\.xml)"'
    for fname, date, letter in re.findall(pattern, _index_html):
        if dt.date.fromisoformat(date).weekday() not in (1, 2, 3):
            continue
        if date not in latest or letter > latest[date][1]:
            latest[date] = (fname, letter)
    return [(d, latest[d][0]) for d in sorted(latest)]
 
 
def pmqs_speeches(root):
    inside, out = False, []
    for el in root:
        if el.tag in ("major-heading", "minor-heading"):
            heading = text_of(el)
            if PM_HEADING.search(heading) and not NOT_PM_HEADING.search(heading):
                inside = True
            elif el.tag == "major-heading":
                inside = False
        elif el.tag == "speech" and inside and el.get("nospeaker") != "true":
            out.append(el)
    return out
 
 
def process_day(date, xml_bytes, people):
    member_to_person, by_person = people
    surname, pm_name = pm_for(date)
    root = ET.fromstring(xml_bytes)
 
    speeches = []
    for el in pmqs_speeches(root):
        name = el.get("speakername", "")
        pid = el.get("person_id") or member_to_person.get(el.get("speakerid", ""), "")
        # The Speaker's interventions are neither questions nor answers. Modern
        # files name the Speaker personally ("Lindsay Hoyle"), so check party too.
        if SPEAKER_NAME.search(name) or party_on(by_person, pid, date) == "Speaker":
            continue
        speeches.append((name, pid, text_of(el)))
    if not speeches:
        return [], {"pmqs_speeches": 0, "note": "no PMQs section found"}
 
    # The PM is the most frequent speaker whose name matches their surname.
    # Requiring 30% of speeches stops a namesake MP being picked on days
    # when a deputy stands in.
    surname_re = re.compile(rf"\b{re.escape(surname)}\b", re.I)
    candidates = Counter(pid for name, pid, _ in speeches if pid and surname_re.search(name))
    pm_pid = None
    if candidates:
        top, n = candidates.most_common(1)[0]
        if n >= 0.3 * len(speeches):
            pm_pid = top
    if pm_pid is None:
        return [], {"pmqs_speeches": len(speeches),
                    "note": "PM not found: possibly a stand-in, excluded"}
 
    def is_pm(name, pid):
        lowered = name.lower()
        return pid == pm_pid or ("prime minister" in lowered and "deputy" not in lowered)
 
    # Pair each MP's speech with the PM's next reply.
    exchanges, pending = [], None
    for name, pid, text in speeches:
        if is_pm(name, pid):
            if pending:
                exchanges.append({"q_name": pending[0], "q_pid": pending[1],
                                  "question": pending[2], "answer": text})
                pending = None
            elif exchanges:
                exchanges[-1]["answer"] += " " + text  # answer resumed after interruption
        else:
            text = QUESTION_NUMBER.sub("", text)
            if pending and pending[1] == pid:
                pending = (pending[0], pending[1], pending[2] + " " + text)
            else:
                pending = (name, pid, text)  # an unanswered earlier speech is dropped
 
    gov = lookup(GOVERNMENTS, date) or []
    main_opp = lookup(MAIN_OPPOSITION, date)
    for e in exchanges:
        party = party_on(by_person, e["q_pid"], date)
        e["q_party"] = party
        e["is_formula"] = bool(FORMULA.search(e["question"])) and len(e["question"]) < 400
        if not party:
            e["q_role"] = "unknown"
        elif any(party.startswith(g) for g in gov):
            e["q_role"] = "government"
        else:
            e["q_role"] = "opposition"
 
    # Leader of the Opposition: the main opposition party's most frequent
    # questioner that day, if they asked at least two questions.
    counts = Counter(e["q_pid"] for e in exchanges
                     if not e["is_formula"] and main_opp and e["q_party"].startswith(main_opp))
    if counts:
        top, n = counts.most_common(1)[0]
        if n >= 2:
            for e in exchanges:
                if e["q_pid"] == top:
                    e["q_role"] = "leader_of_opposition"
 
    rows = []
    for i, e in enumerate(exchanges, 1):
        rows.append({
            "exchange_id": f"{date}-{i:03d}", "date": date, "pm": pm_name,
            "sequence": i, "q_name": e["q_name"], "q_pid": e["q_pid"],
            "q_party": e["q_party"], "q_role": e["q_role"],
            "is_formula": int(e["is_formula"]),
            "q_words": len(e["question"].split()), "a_words": len(e["answer"].split()),
            "question": e["question"], "answer": e["answer"],
        })
    return rows, {"pmqs_speeches": len(speeches), "note": ""}
 
 
def process_year(year, people):
    rows, diag = [], []
    for date, fname in day_files(year):
        if pm_for(date)[0] is None:
            continue
        try:
            day_rows, info = process_day(date, fetch(BASE + fname), people)
        except ET.ParseError as err:
            day_rows, info = [], {"pmqs_speeches": "", "note": f"XML parse error: {err}"}
        rows.extend(day_rows)
        diag.append({"date": date, "file": fname, "exchanges": len(day_rows), **info})
        time.sleep(0.5)  # be polite to mySociety's server
    return rows, diag
 
 
def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
 
 
ROW_FIELDS = ["exchange_id", "date", "pm", "sequence", "q_name", "q_pid", "q_party",
              "q_role", "is_formula", "q_words", "a_words", "question", "answer"]
DIAG_FIELDS = ["date", "file", "exchanges", "pmqs_speeches", "note"]
 
 
def main():
    this_year = dt.date.today().year
    force = "--force" in sys.argv
    args = [int(a) for a in sys.argv[1:] if a.strip().isdigit()]
    start = args[0] if args else this_year
    end = args[1] if len(args) > 1 else (start if args else this_year)
    people = load_people()
    for year in range(start, end + 1):
        out = OUT_DIR / f"{year}.csv"
        if out.exists() and year < this_year and not force:
            print(f"{year}: already done, skipping")
            continue
        rows, diag = process_year(year, people)
        write_csv(out, rows, ROW_FIELDS)
        write_csv(DIAG_DIR / f"{year}.csv", diag, DIAG_FIELDS)
        sessions = sum(1 for d in diag if d["exchanges"])
        print(f"{year}: {len(rows)} exchanges from {sessions} sessions")
 
 
if __name__ == "__main__":
    main()
