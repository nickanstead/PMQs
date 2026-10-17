#!/usr/bin/env python3
"""
resolve_names.py: settle ambiguous pre-1997 questioners using Historic Hansard.

Historic Hansard (api.parliament.uk/historic-hansard) links every speaker's
name to a page for that person. For each exchange that enrich_parties.py could
not resolve ("Mr. Hamilton" when two Hamiltons sat), this script:

  1. opens that day's sitting and the questions-to-the-PM sections,
  2. finds the speech whose name and opening words match the exchange,
  3. follows the speaker's link to their person page, and
  4. picks the sitting MP whose surname and constituency fit that page.

Results go to data/reference/historic_hansard_matches.csv, one row per
exchange, which enrich_parties.py then applies. Exchanges already in that file
are skipped, so the script can be run repeatedly and picks up where it left off.

Before resolving anything it runs a self-check on exchanges whose MP is
already known. If it cannot reproduce those, it stops rather than write
unreliable matches.

Usage:
  python scripts/resolve_names.py                 resolve, up to 300 minutes
  python scripts/resolve_names.py --minutes 60    shorter run
  python scripts/resolve_names.py --check 40      self-check only, on 40 exchanges
"""
import argparse
import csv
import html
import random
import re
import sys
import time
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from enrich_parties import CUTOFF, REF_DIR, Members, key  # noqa: E402
from extract_pmqs import HEADERS, OUT_DIR  # noqa: E402

BASE = "https://api.parliament.uk/historic-hansard"
MATCHES = REF_DIR / "historic_hansard_matches.csv"
FIELDS = ["exchange_id", "date", "name", "status", "hh_person", "hh_name", "person_id",
          "party", "constituency", "score"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
PM_SECTION = re.compile(r"prime-minister|engagement|questions-to-the-prime", re.I)

_page_cache = {}


def get(path, tries=4):
    """Fetch a Historic Hansard page; None if it does not exist."""
    if path in _page_cache:
        return _page_cache[path]
    url = path if path.startswith("http") else BASE + path
    text = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as err:
            if err.code == 404:
                break
            if attempt == tries - 1:
                raise
        except Exception:
            if attempt == tries - 1:
                raise
        time.sleep(20 * (attempt + 1))
    time.sleep(0.5)  # be polite
    _page_cache[path] = text
    return text


def plain(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def norm(text):
    return re.sub(r"[^a-z ]", "", text.lower().replace(".", " ")).split()


def sitting_sections(date):
    """Links to the day's Commons sections, PM-related ones first."""
    y, m, d = date.split("-")
    page = get(f"/sittings/{y}/{MONTHS[int(m) - 1]}/{int(d):02d}")
    if not page:
        return []
    prefix = f"/historic-hansard/commons/{y}/{MONTHS[int(m) - 1]}/{int(d):02d}/"
    links = []
    for href in re.findall(r'href="(' + re.escape(prefix) + r'[^"#]+)"', page):
        path = href.replace("/historic-hansard", "", 1)
        if path not in links:
            links.append(path)
    pm = [p for p in links if PM_SECTION.search(p)]
    return pm + [p for p in links if p not in pm]


def speeches(section_html):
    """(person slug, printed name, speech text) in page order, splitting the
    page at each link to a person page."""
    parts = re.split(r'(<a[^>]+href="/historic-hansard/people/[^"]+"[^>]*>.*?</a>)', section_html, flags=re.S)
    out = []
    for i in range(1, len(parts), 2):
        slug = re.search(r'/historic-hansard/people/([^"/#]+)', parts[i]).group(1)
        name = plain(parts[i])
        text = plain(parts[i + 1])[:400] if i + 1 < len(parts) else ""
        out.append((slug, name, text))
    return out


def find_speaker(date, name, question, max_sections=40):
    """Slug of the person who made this speech on this day, or None."""
    want_name = norm(name)
    want_text = " ".join(norm(question)[:15])
    for n, path in enumerate(sitting_sections(date)):
        if n >= max_sections:
            break
        page = get(path)
        if not page or "Prime Minister" not in page:
            continue
        best, best_score = None, 0.0
        for slug, printed, text in speeches(page):
            if norm(printed)[-1:] != want_name[-1:]:  # surname must agree
                continue
            score = SequenceMatcher(None, " ".join(norm(text)[:15]), want_text).ratio()
            if score > best_score:
                best, best_score = slug, score
        if best and best_score >= 0.6:
            return best
    return None


def person_details(slug):
    page = get(f"/people/{slug}")
    if not page:
        return "", ""
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    return (plain(title.group(1)).split("(")[0].replace("Historic Hansard", "").strip(" -|")
            if title else slug), plain(page).lower()


def choose_member(members, slug, date):
    """The MP sitting on this date who fits the person page best."""
    hh_name, page_text = person_details(slug)
    page_words = set(re.findall(r"[a-z]+", page_text))
    slug_key = key(slug)
    slug_words = slug.lower().split("-")
    best, best_score = None, 0.0
    for m in members.memberships:
        if not (m["start_date"] <= date <= m.get("end_date", "9999")):
            continue
        names = members.names(m["person_id"])
        if not any(key(f) and key(f) in slug_key for f, _ in names):
            continue
        cons = members.posts.get(m["post_id"], "")
        cons_words = set(re.findall(r"[a-z]+", cons.lower())) - {"and", "the", "of"}
        score = len(cons_words & page_words) / max(1, len(cons_words))
        if any(g and any(w[:3] == g.lower()[:3] for w in slug_words) for _, g in names):
            score += 0.5
        if score > best_score:
            best, best_score = m, score
    return best, best_score, hh_name


def resolve(members, row):
    slug = find_speaker(row["date"], row["q_name"], row["question"])
    if not slug:
        return {"status": "speech_not_found"}
    m, score, hh_name = choose_member(members, slug, row["date"])
    base = {"hh_person": slug, "hh_name": hh_name, "score": f"{score:.2f}"}
    if not m or score < 0.5:
        return {**base, "status": "member_not_matched"}
    return {**base, "status": "matched", "person_id": m["person_id"],
            "party": members.party.get(m["id"], ""),
            "constituency": members.posts.get(m["post_id"], "")}


def pre1997_rows():
    for path in sorted(OUT_DIR.glob("*.csv")):
        if int(path.stem) > 1997:
            continue
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["date"] < CUTOFF:
                    yield r


def self_check(members, n):
    """Try exchanges whose MP enrich_parties.py already identified uniquely."""
    known = [r for r in pre1997_rows() if r.get("q_match") == "exact" and r.get("q_pid")
             and r.get("is_formula") == "0"]
    random.seed(1)
    sample = random.sample(known, min(n, len(known)))
    right = wrong = missing = 0
    for r in sample:
        res = resolve(members, r)
        if res["status"] != "matched":
            missing += 1
        elif res["person_id"] == r["q_pid"]:
            right += 1
        else:
            wrong += 1
            print(f"  check mismatch: {r['exchange_id']} {r['q_name']}: got {res.get('hh_name')}")
    print(f"self-check on {len(sample)} known exchanges: {right} right, {wrong} wrong, {missing} not found")
    return right, wrong, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=300)
    ap.add_argument("--check", type=int, default=30)
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()
    members = Members()

    right, wrong, missing = self_check(members, args.check)
    tried = right + wrong
    if tried == 0 or wrong > 0.1 * tried or right < 0.5 * args.check:
        print("Self-check failed: the page structure is not what this script expects. "
              "Stopping without writing matches. Paste this log to Claude.")
        sys.exit(1)
    if args.check_only:
        return

    done = set()
    if MATCHES.exists():
        with open(MATCHES, encoding="utf-8") as f:
            done = {r["exchange_id"] for r in csv.DictReader(f)}
    else:
        REF_DIR.mkdir(parents=True, exist_ok=True)
        with open(MATCHES, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(FIELDS)

    todo = [r for r in pre1997_rows() if r.get("q_match") in ("ambiguous", "none", "fuzzy")
            and r["exchange_id"] not in done]
    print(f"{len(todo)} exchanges to resolve ({len(done)} already in {MATCHES})")
    stop_at = time.time() + args.minutes * 60
    counts = {}
    with open(MATCHES, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        for i, r in enumerate(todo, 1):
            if time.time() > stop_at:
                print(f"time limit reached after {i - 1}; run again to continue")
                break
            res = resolve(members, r)
            w.writerow({"exchange_id": r["exchange_id"], "date": r["date"], "name": r["q_name"], **res})
            f.flush()
            counts[res["status"]] = counts.get(res["status"], 0) + 1
            if i % 50 == 0:
                print(f"  {i}/{len(todo)}: {counts}")
    print(f"done: {counts}")


if __name__ == "__main__":
    main()
