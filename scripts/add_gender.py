#!/usr/bin/env python3
"""
add_gender.py: add the questioner's and the answerer's gender to every exchange.

Adds four columns to data/pmqs/*.csv and data/standins/*.csv:
  q_gender         the questioner: F or M, blank if unknown ("Hon. Members")
  q_gender_source  where it came from (below)
  a_gender         the person answering: the PM in data/pmqs (source "pm"),
                   the stand-in in data/standins (sources below)
  a_gender_source

Sources, in this order of preference:
                     manual       data/reference/manual_gender.csv
                     members_api  UK Parliament Members API, via the MP's
                                  datadotparl_id in mySociety's people.json
                     wikidata     Wikidata "sex or gender" (P21), via the
                                  MP's Wikidata ID in people.json
                     honorific    the MP's titles in people.json (Mr, Mrs,
                                  Miss, Ms, Sir, Dame, Lord, Lady, Baroness)
                     title        the title in the Hansard speaker name, for
                                  questioners with no member ID ("Mr. Hamilton")

Look-ups from the Members API and Wikidata are kept in
data/reference/member_gender.csv, so later runs only ask about new MPs.
If either service is unavailable the run carries on with the other sources
and tries again next time.

Usage:
  python scripts/add_gender.py
"""
import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from extract_pmqs import HEADERS, OUT_DIR, PRIME_MINISTERS, STANDIN_DIR, load_people_json  # noqa: E402

REF_DIR = Path("data/reference")
CACHE_FILE = REF_DIR / "member_gender.csv"
MANUAL_FILE = REF_DIR / "manual_gender.csv"
MEMBERS_API = "https://members-api.parliament.uk/api/Members/{}"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_SEX = {"Q6581072": "F", "Q6581097": "M", "Q1052281": "F", "Q2449503": "M"}
FIELDS = ["q_gender", "q_gender_source", "a_gender", "a_gender_source"]
# Every PM in extract_pmqs.PRIME_MINISTERS. A PM missing here gets a blank
# a_gender and a warning, rather than a guess.
PM_GENDER = {"Margaret Thatcher": "F", "Theresa May": "F", "Liz Truss": "F",
             "Harold Macmillan": "M", "Alec Douglas-Home": "M", "Harold Wilson": "M",
             "Edward Heath": "M", "James Callaghan": "M", "John Major": "M",
             "Tony Blair": "M", "Gordon Brown": "M", "David Cameron": "M",
             "Boris Johnson": "M", "Rishi Sunak": "M", "Keir Starmer": "M", "Andy Burnham": "M"}

FEMALE_TITLES = {"mrs", "miss", "ms", "dame", "lady", "baroness"}
MALE_TITLES = {"mr", "sir", "lord", "viscount", "earl"}
TITLE_F = re.compile(r"\b(Mrs|Miss|Ms|Dame|Lady|Baroness)\b")
TITLE_M = re.compile(r"\b(Mr|Sir)\b")


def get_json(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={**HEADERS, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            if attempt == tries - 1:
                raise
        except Exception:
            if attempt == tries - 1:
                raise
        time.sleep(5 * (attempt + 1))


def read_csv(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def honorific_gender(person):
    titles = {t.lower().strip(".") for n in person.get("other_names", [])
              for t in (n.get("honorific_prefix") or "").split()}
    f, m = bool(titles & FEMALE_TITLES), bool(titles & MALE_TITLES)
    return "F" if f and not m else "M" if m and not f else ""


def title_gender(name):
    if TITLE_F.search(name):
        return "F"
    if TITLE_M.search(name):
        return "M"
    return ""


def members_api(ids):
    """{person_id: gender} for {person_id: datadotparl_id}."""
    found = {}
    for i, (pid, parl_id) in enumerate(sorted(ids.items()), 1):
        try:
            data = get_json(MEMBERS_API.format(parl_id))
        except Exception as err:
            print(f"  Members API unavailable ({err}); will try again next run")
            break
        g = ((data or {}).get("value") or {}).get("gender", "")
        if g in ("F", "M"):
            found[pid] = g
        if i % 200 == 0:
            print(f"  Members API: {i}/{len(ids)}")
        time.sleep(0.2)
    return found


def wikidata(ids):
    """{person_id: gender} for {person_id: Wikidata Q-id}, in batches."""
    by_q = {q: pid for pid, q in ids.items()}
    found, qs = {}, sorted(by_q)
    for start in range(0, len(qs), 200):
        batch = " ".join(f"wd:{q}" for q in qs[start:start + 200])
        query = f"SELECT ?item ?sex WHERE {{ VALUES ?item {{ {batch} }} ?item wdt:P21 ?sex . }}"
        url = WIKIDATA_SPARQL + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
        try:
            data = get_json(url)
        except Exception as err:
            print(f"  Wikidata unavailable ({err}); will try again next run")
            break
        for b in (data or {}).get("results", {}).get("bindings", []):
            q = b["item"]["value"].rsplit("/", 1)[-1]
            g = WIKIDATA_SEX.get(b["sex"]["value"].rsplit("/", 1)[-1])
            if g and q in by_q:
                found[by_q[q]] = g
        time.sleep(1)
    return found


def data_files():
    return sorted(OUT_DIR.glob("*.csv")) + sorted(STANDIN_DIR.glob("*.csv"))


def main():
    persons = {p["id"]: p for p in load_people_json()["persons"]}
    manual = {r["person_id"]: r["gender"] for r in read_csv(MANUAL_FILE) if r.get("gender") in ("F", "M")}
    cache = {r["person_id"]: (r["gender"], r["source"]) for r in read_csv(CACHE_FILE)}

    missing = {full for _, full, _, _ in PRIME_MINISTERS} - set(PM_GENDER)
    if missing:
        print(f"  no gender recorded for PM(s) {', '.join(sorted(missing))}: add to PM_GENDER")

    pids = set()
    for path in data_files():
        for r in read_csv(path):
            pids |= {r.get("q_pid") or "", r.get("answered_by_pid") or ""}
    pids.discard("")

    # Ask the two services only about MPs not already in the cache.
    todo = [p for p in pids if p not in cache and p not in manual]
    ids = {p: {i["scheme"]: i["identifier"] for i in persons.get(p, {}).get("identifiers", [])}
           for p in todo}
    parl = {p: i["datadotparl_id"] for p, i in ids.items() if "datadotparl_id" in i}
    print(f"{len(pids)} questioners and stand-ins with member IDs; {len(todo)} not yet looked up "
          f"({len(parl)} in the Members API)")
    for pid, g in members_api(parl).items():
        cache[pid] = (g, "members_api")
    wd = {p: i["wikidata"] for p, i in ids.items() if "wikidata" in i and p not in cache}
    if wd:
        print(f"  asking Wikidata about {len(wd)}")
        for pid, g in wikidata(wd).items():
            cache[pid] = (g, "wikidata")

    REF_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["person_id", "gender", "source"])
        for pid in sorted(cache):
            w.writerow([pid, *cache[pid]])

    def gender_of(pid, name):
        if pid in manual:
            return manual[pid], "manual"
        if pid in cache:
            return cache[pid]
        if pid in persons:
            g = honorific_gender(persons[pid])
            if g:
                return g, "honorific"
        g = title_gender(name or "")
        return (g, "title") if g else ("", "")

    totals = Counter()
    for path in data_files():
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fields = list(reader.fieldnames or [])
        if not rows:
            continue
        standin = "answered_by" in fields
        for r in rows:
            r["q_gender"], r["q_gender_source"] = gender_of(r.get("q_pid", ""), r.get("q_name", ""))
            totals[r["q_gender_source"] or "unknown"] += 1
            totals[r["q_gender"] or "?"] += 1
            if standin:
                r["a_gender"], r["a_gender_source"] = gender_of(r.get("answered_by_pid", ""),
                                                                r.get("answered_by", ""))
            else:
                g = PM_GENDER.get(r.get("pm", ""), "")
                r["a_gender"], r["a_gender_source"] = (g, "pm") if g else ("", "")
            totals["a_" + (r["a_gender"] or "?")] += 1
        fields += [c for c in FIELDS if c not in fields]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    n = totals["F"] + totals["M"] + totals["?"]
    print(f"gender for {n - totals['?']} of {n} exchanges ({(n - totals['?']) / max(n, 1):.1%}): "
          f"{totals['F']} F, {totals['M']} M")
    print("  by source: " + ", ".join(f"{k}: {totals[k]}" for k in
          ("manual", "members_api", "wikidata", "honorific", "title", "unknown")))
    print(f"answerers: {totals['a_F']} F, {totals['a_M']} M, {totals['a_?']} unknown")


if __name__ == "__main__":
    main()
