#!/usr/bin/env python3
"""
extract_pmqs.py: build a dataset of PMQs question/answer exchanges from
mySociety's ParlParse XML (TheyWorkForYou), 1961 onwards.

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
import urllib.error
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
    ("Macmillan", "Harold Macmillan", "1957-01-10", "1963-10-19"),
    ("Douglas-Home", "Alec Douglas-Home", "1963-10-19", "1964-10-16"),
    ("Wilson", "Harold Wilson", "1964-10-16", "1970-06-19"),
    ("Heath", "Edward Heath", "1970-06-19", "1974-03-04"),
    ("Wilson", "Harold Wilson", "1974-03-04", "1976-04-05"),
    ("Callaghan", "James Callaghan", "1976-04-05", "1979-05-04"),
    ("Thatcher", "Margaret Thatcher", "1979-05-04", "1990-11-28"),
    ("Major", "John Major", "1990-11-28", "1997-05-02"),
    ("Blair", "Tony Blair", "1997-05-02", "2007-06-27"),
    ("Brown", "Gordon Brown", "2007-06-27", "2010-05-11"),
    ("Cameron", "David Cameron", "2010-05-11", "2016-07-13"),
    ("May", "Theresa May", "2016-07-13", "2019-07-24"),
    ("Johnson", "Boris Johnson", "2019-07-24", "2022-09-06"),
    ("Truss", "Liz Truss", "2022-09-06", "2022-10-25"),
    ("Sunak", "Rishi Sunak", "2022-10-25", "2024-07-05"),
    ("Starmer", "Keir Starmer", "2024-07-05", "2026-07-20"),
    ("Burnham", "Andy Burnham", "2026-07-20", "9999-12-31"),
]
# Parties in government. Matched with startswith against ParlParse party
# names, so "Labour" covers "Labour/Co-operative" but not the SDLP.
GOVERNMENTS = [
    ("1957-01-10", "1964-10-16", ["Conservative"]),
    ("1964-10-16", "1970-06-19", ["Labour"]),
    ("1970-06-19", "1974-03-04", ["Conservative"]),
    ("1974-03-04", "1979-05-04", ["Labour"]),  # Lib-Lab pact 1977-78: Liberals not in government
    ("1979-05-04", "1997-05-02", ["Conservative"]),
    ("1997-05-02", "2010-05-11", ["Labour"]),
    ("2010-05-11", "2015-05-08", ["Conservative", "Liberal Democrat"]),
    ("2015-05-08", "2024-07-05", ["Conservative"]),
    ("2024-07-05", "9999-12-31", ["Labour"]),
]
MAIN_OPPOSITION = [
    ("1957-01-10", "1964-10-16", "Labour"),
    ("1964-10-16", "1970-06-19", "Conservative"),
    ("1970-06-19", "1974-03-04", "Labour"),
    ("1974-03-04", "1979-05-04", "Conservative"),
    ("1979-05-04", "1997-05-02", "Labour"),
    ("1997-05-02", "2010-05-11", "Conservative"),
    ("2010-05-11", "2024-07-05", "Labour"),
    ("2024-07-05", "9999-12-31", "Conservative"),
]

# Headings that open PMQs, matched against the whole heading once lower-cased
# with punctuation removed: "Prime Minister", "Engagements", "PRIME MINISTER
# (ENGAGEMENTS)". A heading that merely mentions the PM, such as "Prime
# Minister's Update" for a statement, does not count.
PM_HEADING = re.compile(
    r"(oral answers to questions )?(the )?prime minister( engagements)?"
    r"|engagements|prime ministers? questions|questions to the prime minister")
NOT_PM_HEADING = re.compile(r"deputy prime minister", re.I)
SPEAKER_NAME = re.compile(r"\bspeaker\b", re.I)
FORMULA = re.compile(
    r"official engagements|engagements for (monday|tuesday|wednesday|thursday|friday)", re.I)
QUESTION_NUMBER = re.compile(r"^\s*(Q\d+\.?\s*)?(\[\d+\]\s*)?")

_index_html = None


def fetch(url, tries=5):
    """Download with patient retries: mySociety's server sometimes returns
    503 (busy), so back off for 30s, 60s, 90s... before giving up."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(30 * (attempt + 1))


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


def exists(url, tries=4):
    """True if a file is on the server, False on 404; retries if busy."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS, method="HEAD")
            with urllib.request.urlopen(req, timeout=60):
                return True
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return False
            if err.code == 405:  # server refuses HEAD requests: ask for the file itself
                try:
                    fetch(url, tries=1)
                    return True
                except urllib.error.HTTPError as err2:
                    if err2.code == 404:
                        return False
            if attempt == tries - 1:
                raise
        except Exception:
            if attempt == tries - 1:
                raise
        time.sleep(30 * (attempt + 1))


def probe_files(year):
    """Find a year's files by asking for each one directly: for every
    Tuesday-Thursday, try version a, then b, c... and keep the last that exists."""
    found = []
    day = dt.date(year, 1, 1)
    while day.year == year:
        if day.weekday() in (1, 2, 3) and pm_for(day.isoformat())[0]:
            latest = None
            for letter in "abcdefgh":
                fname = f"debates{day.isoformat()}{letter}.xml"
                if not exists(BASE + fname):
                    break
                latest = fname
                time.sleep(0.2)
            if latest:
                found.append((day.isoformat(), latest))
            time.sleep(0.2)
        day += dt.timedelta(days=1)
    return found


def day_files(year):
    """Latest version of each Tuesday-Thursday debates file in a year.
    PMQs has been on Wednesdays since 1997 and Tuesdays/Thursdays before.
    Uses the server's file index; if that lists suspiciously few files for
    the year, checks for each day's file directly instead."""
    global _index_html
    if _index_html is None:
        _index_html = fetch(BASE).decode("utf-8", "replace")
    latest = {}
    pattern = rf"(debates({year}-\d\d-\d\d)([a-z]?)\.xml)"
    for fname, date, letter in re.findall(pattern, _index_html):
        if dt.date.fromisoformat(date).weekday() not in (1, 2, 3):
            continue
        if date not in latest or letter > latest[date][1]:
            latest[date] = (fname, letter)
    files = [(d, latest[d][0]) for d in sorted(latest)]
    if len(files) < 60:  # a normal year has 90+ Tuesday-Thursday sitting days
        print(f"{year}: index lists only {len(files)} files; checking each day directly")
        probed = probe_files(year)
        print(f"{year}: direct check found {len(probed)} files")
        if len(probed) > len(files):
            files = probed
    return files


# A question tabled to the PM, as printed in older Hansard: "asked the Prime
# Minister...", "1. Mr. X asked the Prime Minister..." or "Mr. X: To ask the
# Prime Minister...". Matched only at the start of a speech, so an MP saying
# "I asked the Prime Minister last week" during a statement does not count.
ASKED_PM = re.compile(
    r"\s*(q?\d+\.?\s*)?(\[\d+\]\s*)?"
    r"((mr|mrs|miss|ms|dr|sir|dame|lady|lord|rev|captain|major|colonel|commander|brigadier|lieut)"
    r"\b[^:?,]{0,60}?(:\s*|\s+))?(asked|to ask) the prime minister", re.I)


def normalise_heading(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", text.lower())).strip()


def pmqs_speeches(root):
    """Speeches inside questions to the PM. A section starts at a heading
    mentioning the PM or engagements, or at a tabled question that 'asked'
    (or is 'To ask') the Prime Minister, as in older Hansard where headings
    name the topic. It ends at the next heading that is neither, so that a
    following statement or debate is not swept in; a later question to the
    PM under a topic heading reopens it."""
    inside, out, opened = False, [], []
    for el in root:
        if el.tag in ("major-heading", "minor-heading"):
            heading = text_of(el)
            inside = bool(PM_HEADING.fullmatch(normalise_heading(heading)))
            if inside:
                opened.append(heading[:40])
        elif el.tag == "speech" and el.get("nospeaker") != "true":
            if not inside and ASKED_PM.match(text_of(el)):
                inside = True
                opened.append("tabled question")
            if inside:
                out.append(el)
    return out, opened


def process_day(date, xml_bytes, people):
    member_to_person, by_person = people
    surname, pm_name = pm_for(date)
    root = ET.fromstring(xml_bytes)

    speeches = []
    section, opened = pmqs_speeches(root)
    opened_by = "; ".join(sorted(set(opened)))[:120]
    for el in section:
        name = el.get("speakername", "").strip()
        if not name and not el.get("person_id") and not el.get("speakerid"):
            # Unnamed speech: in older Hansard, the same speaker continuing.
            if speeches:
                speeches[-1] = (speeches[-1][0], speeches[-1][1],
                                speeches[-1][2] + " " + text_of(el))
            continue
        pid = el.get("person_id") or member_to_person.get(el.get("speakerid", ""), "")
        # The Speaker's interventions are neither questions nor answers. Modern
        # files name the Speaker personally ("Lindsay Hoyle"), so check party too.
        if SPEAKER_NAME.search(name) or party_on(by_person, pid, date) == "Speaker":
            continue
        speeches.append((name, pid, text_of(el)))
    if not speeches:
        headings = [text_of(h)[:40] for h in root if h.tag == "major-heading"][:8]
        return [], {"pmqs_speeches": 0, "note": "no PMQs section found",
                    "detail": " | ".join(headings)}

    # The PM is the most frequent speaker whose name matches their surname.
    # Requiring 30% of speeches stops a namesake MP being picked on days
    # when a deputy stands in.
    surname_re = re.compile(rf"\b{re.escape(surname)}\b", re.I)
    # Older files may lack IDs, so identify speakers by ID or, failing that, by name.
    def key(name, pid):
        return pid or name.lower()

    def titled_pm(name):
        lowered = name.lower()
        return "prime minister" in lowered and "deputy" not in lowered

    candidates = Counter(pid for name, pid, _ in speeches if pid and surname_re.search(name))
    pm_pid = None
    if candidates:
        top, n = candidates.most_common(1)[0]
        if n >= 0.3 * len(speeches):
            pm_pid = top

    def is_pm(name, pid):
        # Surname alone is trusted only when there is no ID, as in older files;
        # otherwise a namesake (e.g. Diana Johnson under Boris Johnson) would match.
        return ((pm_pid is not None and pid == pm_pid) or titled_pm(name)
                or (not pid and bool(surname_re.search(name))))

    if sum(is_pm(name, pid) for name, pid, _ in speeches) < 0.3 * len(speeches):
        names = Counter(name for name, _, _ in speeches).most_common(4)
        return [], {"pmqs_speeches": len(speeches),
                    "note": "PM not found: possibly a stand-in, excluded",
                    "detail": " | ".join(f"{n} x{c}" for n, c in names)}

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
            if pending and key(pending[0], pending[1]) == key(name, pid):
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
    unknown = sum(1 for r in rows if r["q_role"] == "unknown")
    detail = f"opened by: {opened_by}"
    if unknown:
        detail += f"; {unknown} questioners unmatched"
    return rows, {"pmqs_speeches": len(speeches), "note": "", "detail": detail}


def process_year(year, people):
    rows, diag = [], []
    files = day_files(year)
    if not files:
        print(f"{year}: WARNING no files found in the TheyWorkForYou index")
    for date, fname in files:
        if pm_for(date)[0] is None:
            continue
        try:
            xml_bytes = fetch(BASE + fname)
        except Exception as err:
            # One failed download no longer stops the run; the year is
            # flagged and redone automatically next time.
            diag.append({"date": date, "file": fname, "exchanges": 0,
                         "pmqs_speeches": "", "note": f"download failed: {err}"})
            continue
        try:
            day_rows, info = process_day(date, xml_bytes, people)
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
DIAG_FIELDS = ["date", "file", "exchanges", "pmqs_speeches", "note", "detail"]


def year_complete(year):
    """A past year counts as done only if it had no failed downloads."""
    diag = DIAG_DIR / f"{year}.csv"
    if not (OUT_DIR / f"{year}.csv").exists() or not diag.exists():
        return False
    return "download failed" not in diag.read_text(encoding="utf-8")


def main():
    this_year = dt.date.today().year
    force = "--force" in sys.argv
    args = [int(a) for a in sys.argv[1:] if a.strip().isdigit()]
    start = args[0] if args else this_year
    end = args[1] if len(args) > 1 else (start if args else this_year)
    people = load_people()
    for year in range(start, end + 1):
        out = OUT_DIR / f"{year}.csv"
        if year < this_year and not force and year_complete(year):
            print(f"{year}: already done, skipping")
            continue
        rows, diag = process_year(year, people)
        write_csv(out, rows, ROW_FIELDS)
        write_csv(DIAG_DIR / f"{year}.csv", diag, DIAG_FIELDS)
        sessions = sum(1 for d in diag if d["exchanges"])
        print(f"{year}: {len(rows)} exchanges from {sessions} sessions")


if __name__ == "__main__":
    main()
