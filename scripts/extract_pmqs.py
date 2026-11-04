#!/usr/bin/env python3
"""
extract_pmqs.py: build a dataset of PMQs question/answer exchanges from
mySociety's ParlParse XML (TheyWorkForYou), 1961 onwards.

Output (one CSV per year):
  data/pmqs/<year>.csv          one row per question/answer exchange
  data/standins/<year>.csv      the same, for sessions someone else answered
  data/diagnostics/<year>.csv   one row per day checked, for sanity-checking
  data/reports/missing_sessions.csv   PMQs days where questions were held
                                       but no PMQs section was found

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
STANDIN_DIR = Path("data/standins")
REPORT_DIR = Path("data/reports")
EXCLUSIONS = Path("data/reference/manual_exclusions.csv")
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
    r"(oral answers to questions )?(the )?prime minister( engagements?)?"
    r"|engagements?|prime ministers? questions|questions to the prime minister")
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


# Blair's first PMQs: from here, once a week on Wednesdays.
WEDNESDAY_FORMAT = "1997-05-21"


def pmqs_day(date):
    """True if PMQs would normally be held on this weekday: Tuesdays and
    Thursdays until May 1997, Wednesdays after."""
    wd = dt.date.fromisoformat(date).weekday()
    return wd == 2 if date >= WEDNESDAY_FORMAT else wd in (1, 3)


def pm_candidates(date):
    """The PM in office, plus the outgoing PM on a handover day. The
    outgoing PM answers PMQs that morning and leaves office that afternoon
    (Blair 27 Jun 2007, Cameron 13 Jul 2016, May 24 Jul 2019), but the
    table above gives the date to the successor."""
    cands = []
    for surname, full, start, end in PRIME_MINISTERS:
        if start <= date < end or end == date:
            cands.append((surname, full))
    return cands


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


ORAL_PREFIX = re.compile(r"^oral answers to questions\s*")


# Business that can follow PMQs under the same "Prime Minister" heading and
# is not a question to the PM: statements, points of order, urgent questions.
NOT_A_QUESTION = re.compile(
    r"^\s*(with permission|on a point of order|\(urgent question\)|to ask\b"
    r"|i beg to move|i (should|would) like to make a statement|order\b|before we come)", re.I)


def opens_question(text):
    """True if a speech reads as a question tabled to the PM, such as "If he
    will make an official visit to Dartington" or "When he next expects to
    visit Ayrshire": short, and not a statement, point of order or urgent
    question. Statements open with a long speech by the minister."""
    text = QUESTION_NUMBER.sub("", text)
    return not NOT_A_QUESTION.match(text) and len(text.split()) <= 80


def normalise_heading(text):
    """Lower-case, drop punctuation and drop a leading "Oral Answers to
    Questions", which 1997-2001 files combine with the heading itself
    ("Oral Answers to Questions — Engagements")."""
    h = re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", text.lower())).strip()
    return ORAL_PREFIX.sub("", h)


def pmqs_speeches(root, keep_topics=True):
    """Speeches inside questions to the PM. A section starts at a heading
    mentioning the PM or engagements, or at a tabled question that 'asked'
    (or is 'To ask') the Prime Minister, as in older Hansard where headings
    name the topic. It ends at the next heading that is neither, so that a
    following statement or debate is not swept in; a later question to the
    PM under a topic heading reopens it."""
    inside, pm_major, topic_pending, out, opened = False, False, False, [], []
    for el in root:
        if el.tag in ("major-heading", "minor-heading"):
            heading = text_of(el)
            is_pm = bool(PM_HEADING.fullmatch(normalise_heading(heading)))
            topic_pending = False
            if el.tag == "major-heading":
                pm_major = inside = is_pm
            elif is_pm:
                inside = True
            else:
                # Under a "Prime Minister" major heading, a topic minor heading
                # (a substantive question on, say, Iraq) stays inside if the
                # speech after it reads as a question to the PM. A statement
                # after PMQs ("With permission...") does not, so it is left
                # out. With keep_topics off, process_day's fallback, topic
                # headings close.
                inside = False
                topic_pending = pm_major and keep_topics
            if is_pm:
                opened.append(heading[:40])
        elif el.tag == "speech" and el.get("nospeaker") != "true":
            if topic_pending and not SPEAKER_NAME.search(el.get("speakername", "")):
                # The first speech after the heading, not counting the Speaker.
                topic_pending = False
                if opens_question(text_of(el)):
                    inside = True
            if not inside and ASKED_PM.match(text_of(el)):
                inside = True
                opened.append("tabled question")
            if inside:
                out.append(el)
    return out, opened


ORAL_HINT = re.compile(r"oral answers|\basked the\b|to ask the", re.I)


def all_headings(root, limit=500):
    heads = [text_of(h)[:50] for h in root if h.tag in ("major-heading", "minor-heading")]
    return " | ".join(heads)[:limit]


def titled_pm(name):
    lowered = name.lower()
    return "prime minister" in lowered and "deputy" not in lowered


def pm_matcher(speeches, surname):
    """A test for which speeches are the PM's, for one candidate PM, and how
    many speeches it picks out. The PM is the most frequent speaker whose
    name matches their surname; requiring 30% of speeches stops a namesake
    MP being picked on days when a deputy stands in."""
    surname_re = re.compile(rf"\b{re.escape(surname)}\b", re.I)
    candidates = Counter(pid for name, pid, _ in speeches if pid and surname_re.search(name))
    pm_pid = None
    if candidates:
        top, n = candidates.most_common(1)[0]
        if n >= 0.2 * len(speeches):
            pm_pid = top

    def is_pm(name, pid):
        # Surname alone is trusted only when there is no ID, as in older files;
        # otherwise a namesake (e.g. Diana Johnson under Boris Johnson) would match.
        return ((pm_pid is not None and pid == pm_pid) or titled_pm(name)
                or (not pid and bool(surname_re.search(name))))

    n_pm = sum(is_pm(name, pid) for name, pid, _ in speeches)
    others = Counter(speaker_key(name, pid) for name, pid, _ in speeches if not is_pm(name, pid))
    runner_up = others.most_common(1)[0][1] if others else 0
    # The PM answered if they gave 30% of speeches, or at least 20% and
    # clearly more than anyone else, as in long sessions full of interjections.
    answered = n_pm >= 0.3 * len(speeches) or (n_pm >= 0.2 * len(speeches)
                                                and n_pm >= 1.5 * runner_up)
    return is_pm, n_pm, answered


def speaker_key(name, pid):
    # Older files may lack IDs, so identify speakers by ID or, failing that, by name.
    return pid or name.lower()


def standin_matcher(speeches):
    """For a session the PM didn't answer: the most frequent speaker, if they
    gave at least 30% of the speeches, is taken to be the person answering."""
    counts = Counter(speaker_key(name, pid) for name, pid, _ in speeches)
    top, n = counts.most_common(1)[0]
    if n < 0.3 * len(speeches):
        return None, None, None
    name, pid = next((name, pid) for name, pid, _ in speeches if speaker_key(name, pid) == top)
    return (lambda nm, p: speaker_key(nm, p) == top), name, pid


def pair_exchanges(speeches, is_answerer):
    """Pair each MP's speech with the answerer's next reply."""
    exchanges, pending = [], None
    for name, pid, text in speeches:
        if is_answerer(name, pid):
            if pending:
                exchanges.append({"q_name": pending[0], "q_pid": pending[1],
                                  "question": pending[2], "answer": text})
                pending = None
            elif exchanges:
                exchanges[-1]["answer"] += " " + text  # answer resumed after interruption
        else:
            text = QUESTION_NUMBER.sub("", text)
            if pending and speaker_key(pending[0], pending[1]) == speaker_key(name, pid):
                pending = (pending[0], pending[1], pending[2] + " " + text)
            else:
                pending = (name, pid, text)  # an unanswered earlier speech is dropped
    return exchanges


def process_day(date, xml_bytes, people):
    """Returns (rows, standin_rows, diagnostics). Exactly one of the two row
    lists is filled on a day with PMQs."""
    member_to_person, by_person = people
    root = ET.fromstring(xml_bytes)

    def collect(keep_topics):
        section, opened = pmqs_speeches(root, keep_topics)
        found = []
        for el in section:
            name = el.get("speakername", "").strip()
            if not name and not el.get("person_id") and not el.get("speakerid"):
                # Unnamed speech: in older Hansard, the same speaker continuing.
                if found:
                    found[-1] = (found[-1][0], found[-1][1], found[-1][2] + " " + text_of(el))
                continue
            pid = el.get("person_id") or member_to_person.get(el.get("speakerid", ""), "")
            # The Speaker's interventions are neither questions nor answers. Modern
            # files name the Speaker personally ("Lindsay Hoyle"), so check party too.
            if SPEAKER_NAME.search(name) or party_on(by_person, pid, date) == "Speaker":
                continue
            found.append((name, pid, text_of(el)))
        return found, "; ".join(sorted(set(opened)))[:120]

    def which_pm(speeches):
        # Which PM answered: on a handover day, whichever candidate fits best.
        best = None
        for surname, full in pm_candidates(date):
            is_pm, n, answered = pm_matcher(speeches, surname)
            if best is None or (answered, n) > (best[3], best[2]):
                best = (is_pm, full, n, answered)
        return best

    speeches, opened_by = collect(True)
    if not speeches:
        if not pmqs_day(date):
            note = "not a PMQs day"
        elif any(ORAL_HINT.search(text_of(el)) for el in root
                 if el.tag in ("major-heading", "minor-heading", "speech")):
            note = "MISSING: questions held but no PMQs section found"
        else:
            note = "no PMQs section found (no questions held)"
        return [], [], {"pmqs_speeches": 0, "note": note, "detail": all_headings(root)}

    is_pm, pm_name, n_pm, answered = which_pm(speeches)
    if not answered:
        # Topic headings may have swept in other business (13 Mar 1990: Arts
        # questions after PMQs). Retry without them, and use that stricter
        # section for the stand-in check too.
        strict, strict_opened = collect(False)
        if strict and strict != speeches:
            speeches, opened_by = strict, strict_opened + "; strict headings"
            is_pm, pm_name, n_pm, answered = which_pm(speeches)
    in_office = pm_for(date)[1]

    standin = None
    if not answered:
        is_answerer, resp_name, resp_pid = standin_matcher(speeches)
        names = Counter(name for name, _, _ in speeches).most_common(4)
        detail = " | ".join(f"{nm} x{c}" for nm, c in names)
        if is_answerer is None:
            return [], [], {"pmqs_speeches": len(speeches),
                            "note": "PM not found and no clear stand-in, excluded",
                            "detail": detail}
        standin = (resp_name, resp_pid)
        exchanges = pair_exchanges(speeches, is_answerer)
        pm_name = in_office
    else:
        exchanges = pair_exchanges(speeches, is_pm)

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
        row = {
            "exchange_id": f"{date}-{i:03d}" + ("-s" if standin else ""), "date": date,
            "pm": pm_name, "sequence": i, "q_name": e["q_name"], "q_pid": e["q_pid"],
            "q_party": e["q_party"], "q_role": e["q_role"],
            "is_formula": int(e["is_formula"]),
            "q_words": len(e["question"].split()), "a_words": len(e["answer"].split()),
            "question": e["question"], "answer": e["answer"],
        }
        if standin:
            row["answered_by"], row["answered_by_pid"] = standin
        rows.append(row)
    unknown = sum(1 for r in rows if r["q_role"] == "unknown")
    detail = f"opened by: {opened_by}"
    if unknown:
        detail += f"; {unknown} questioners unmatched"
    if standin:
        return [], rows, {"pmqs_speeches": len(speeches),
                          "note": f"stand-in: {standin[0]}", "detail": detail}
    if pm_name != in_office:
        detail += "; handover day, answered by outgoing PM"
    return rows, [], {"pmqs_speeches": len(speeches), "note": "", "detail": detail}


def load_exclusions():
    """Manual corrections the rules can't make: for each date, drop that
    day's exchanges from from_exchange onwards (1 drops the whole session)."""
    if not EXCLUSIONS.exists():
        return {}
    with open(EXCLUSIONS, encoding="utf-8") as f:
        return {r["date"]: (int(r["from_exchange"]), r["reason"]) for r in csv.DictReader(f)}


def process_year(year, people):
    rows, standins, diag = [], [], []
    exclusions = load_exclusions()
    files = day_files(year)
    if not files:
        print(f"{year}: WARNING no files found in the TheyWorkForYou index")
    for date, fname in files:
        if not pm_candidates(date):
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
            day_rows, day_standins, info = process_day(date, xml_bytes, people)
        except ET.ParseError as err:
            day_rows, day_standins, info = [], [], {"pmqs_speeches": "",
                                                    "note": f"XML parse error: {err}"}
        if date in exclusions:
            cut, reason = exclusions[date]
            day_rows = [r for r in day_rows if r["sequence"] < cut]
            day_standins = [r for r in day_standins if r["sequence"] < cut]
            info["note"] = (info.get("note") or "") + f" [manual exclusion from exchange {cut}: {reason}]"
            info["note"] = info["note"].strip()
        rows.extend(day_rows)
        standins.extend(day_standins)
        diag.append({"date": date, "file": fname,
                     "exchanges": len(day_rows) + len(day_standins), **info})
        time.sleep(0.5)  # be polite to mySociety's server
    return rows, standins, diag


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


ROW_FIELDS = ["exchange_id", "date", "pm", "sequence", "q_name", "q_pid", "q_party",
              "q_role", "is_formula", "q_words", "a_words", "question", "answer"]
STANDIN_FIELDS = ROW_FIELDS + ["answered_by", "answered_by_pid"]
DIAG_FIELDS = ["date", "file", "exchanges", "pmqs_speeches", "note", "detail"]


def write_missing_report():
    """Collect every suspected missing session, across all years, in one file."""
    missing = []
    for path in sorted(DIAG_DIR.glob("*.csv")):
        with open(path, encoding="utf-8") as f:
            missing += [r for r in csv.DictReader(f) if r["note"].startswith("MISSING")]
    write_csv(REPORT_DIR / "missing_sessions.csv", missing, DIAG_FIELDS)
    print(f"{len(missing)} suspected missing sessions listed in {REPORT_DIR / 'missing_sessions.csv'}")


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
        rows, standins, diag = process_year(year, people)
        write_csv(out, rows, ROW_FIELDS)
        write_csv(STANDIN_DIR / f"{year}.csv", standins, STANDIN_FIELDS)
        write_csv(DIAG_DIR / f"{year}.csv", diag, DIAG_FIELDS)
        sessions = sum(1 for d in diag if d["exchanges"] and not d["note"].startswith("stand-in"))
        n_si = sum(1 for d in diag if d["exchanges"] and d["note"].startswith("stand-in"))
        print(f"{year}: {len(rows)} exchanges from {sessions} sessions; {n_si} stand-in sessions")
    write_missing_report()


if __name__ == "__main__":
    main()
