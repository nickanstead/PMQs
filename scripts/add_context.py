#!/usr/bin/env python3
"""
add_context.py: add session context and questioner characteristics to every
exchange in data/pmqs/*.csv and data/standins/*.csv.

Session context
  parliament            number of the Parliament (42nd elected 1959, 59th 2024)
  days_since_election   days since the last general election
  days_in_office        days the Prime Minister in office had served
  gov_seats, gov_majority
                        the government's seats and overall majority at the last
                        general election (2 x government seats - all seats, so
                        the Speaker counts as a non-government seat; 2010-15
                        counts both coalition parties)
  loto, loto_status     Leader of the Opposition, and whether "leader" or
                        "acting" (a deputy acting after the leader left:
                        George Brown 1963, Margaret Beckett 1994, Harriet
                        Harman 2010 and 2015)
Questioner
  q_is_loto             1 if the questioner is the Leader of the Opposition
  q_constituency        seat held on the day, from ParlParse
  q_nation              England, Scotland, Wales or Northern Ireland: from the
                        seat's ONS code where it has one, otherwise its name
  q_years_in_commons    years since first elected (counting from the first
                        election, including any gap out of the House)
  q_age                 age in whole years, from Wikidata birth dates
  q_majority_pct        winning margin in percentage points at the general
                        election before the question, where the MP won the
                        seat at that election (blank after a by-election)
  q_bench, q_post       from 1997: "government", "opposition_frontbench" or
                        "backbench" on the day, with the post held, from the
                        Parliament data published by ParlParse. Party leaders
                        count as opposition frontbench; PPSs as backbench.
                        Blank before 1997.

Usage:
  python scripts/add_context.py
"""
import csv
import io
import json
import math
import re
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date as Date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from extract_pmqs import (CACHE, OUT_DIR, PRIME_MINISTERS, STANDIN_DIR,  # noqa: E402
                          fetch, load_people_json)
import add_gender  # noqa: E402  (for its JSON fetcher)

REF_DIR = Path("data/reference")
BIRTH_FILE = REF_DIR / "member_birth.csv"
POSTS_URL = "https://raw.githubusercontent.com/mysociety/parlparse/master/members/ministers-2010.json"
ELECTIONS_URL = ("https://raw.githubusercontent.com/ukparliament/"
                 "uk-general-elections/main/db/data/download.csv")
GE2024_URL = "https://electionresults.parliament.uk/general-elections/6/candidacies.csv"
BENCH_FROM = "1997-05-01"
LEADER_ROLE = re.compile(r"^(Acting |Parliamentary |Co-)?Leader(,| of)\b")

FIELDS = ["parliament", "days_since_election", "days_in_office", "gov_seats", "gov_majority",
          "loto", "loto_status", "q_is_loto", "q_constituency", "q_nation",
          "q_years_in_commons", "q_age", "q_majority_pct", "q_bench", "q_post"]

# General elections: date, Parliament number, government seats, all seats.
# Seats as published by the House of Commons Library (2010: Conservative 306
# plus Liberal Democrat 57).
ELECTIONS = [
    ("1959-10-08", 42, 365, 630), ("1964-10-15", 43, 317, 630), ("1966-03-31", 44, 364, 630),
    ("1970-06-18", 45, 330, 630), ("1974-02-28", 46, 301, 635), ("1974-10-10", 47, 319, 635),
    ("1979-05-03", 48, 339, 635), ("1983-06-09", 49, 397, 650), ("1987-06-11", 50, 376, 650),
    ("1992-04-09", 51, 336, 651), ("1997-05-01", 52, 418, 659), ("2001-06-07", 53, 412, 659),
    ("2005-05-05", 54, 355, 646), ("2010-05-06", 55, 363, 650), ("2015-05-07", 56, 330, 650),
    ("2017-06-08", 57, 317, 650), ("2019-12-12", 58, 365, 650), ("2024-07-04", 59, 411, 650),
]
ELECTION_DATES = [e[0] for e in ELECTIONS]

# Leaders of the Opposition: name, member ID, from, to (exclusive), status.
LOTO = [
    ("Hugh Gaitskell", "17474", "1955-12-14", "1963-01-18", "leader"),
    ("George Brown", "21818", "1963-01-18", "1963-02-14", "acting"),
    ("Harold Wilson", "21924", "1963-02-14", "1964-10-16", "leader"),
    ("Alec Douglas-Home", "17210", "1964-10-16", "1965-07-28", "leader"),
    ("Edward Heath", "10270", "1965-07-28", "1970-06-19", "leader"),
    ("Harold Wilson", "21924", "1970-06-19", "1974-03-04", "leader"),
    ("Edward Heath", "10270", "1974-03-04", "1975-02-11", "leader"),
    ("Margaret Thatcher", "19284", "1975-02-11", "1979-05-04", "leader"),
    ("James Callaghan", "13677", "1979-05-04", "1980-11-10", "leader"),
    ("Michael Foot", "17433", "1980-11-10", "1983-10-02", "leader"),
    ("Neil Kinnock", "13171", "1983-10-02", "1992-07-18", "leader"),
    ("John Smith", "14202", "1992-07-18", "1994-05-12", "leader"),
    ("Margaret Beckett", "10031", "1994-05-12", "1994-07-21", "acting"),
    ("Tony Blair", "10047", "1994-07-21", "1997-05-02", "leader"),
    ("John Major", "10408", "1997-05-02", "1997-06-19", "leader"),
    ("William Hague", "10251", "1997-06-19", "2001-09-13", "leader"),
    ("Iain Duncan Smith", "10180", "2001-09-13", "2003-11-06", "leader"),
    ("Michael Howard", "10290", "2003-11-06", "2005-12-06", "leader"),
    ("David Cameron", "10777", "2005-12-06", "2010-05-11", "leader"),
    ("Harriet Harman", "10260", "2010-05-11", "2010-09-25", "acting"),
    ("Ed Miliband", "11545", "2010-09-25", "2015-05-08", "leader"),
    ("Harriet Harman", "10260", "2015-05-08", "2015-09-12", "acting"),
    ("Jeremy Corbyn", "10133", "2015-09-12", "2020-04-04", "leader"),
    ("Keir Starmer", "25353", "2020-04-04", "2024-07-05", "leader"),
    ("Rishi Sunak", "25428", "2024-07-05", "2024-11-02", "leader"),
    ("Kemi Badenoch", "25693", "2024-11-02", "9999-12-31", "leader"),
]
PID = "uk.org.publicwhip/person/"

# Nation from a constituency's name, for seats without an ONS code.
_SCOT = ("Aberdeen|Aberdeenshire|Airdrie|Alloa|Angus|Arbroath|Argyll|Ayr|Ayrshire|Banff|Banffshire|"
         "Bearsden|Bellshill|Berwickshire|Bothwell|Buchan|Bute|Buteshire|Caithness|Carrick|Cathcart|"
         "Chryston|Clackmannan|Clydebank|Clydesdale|Coatbridge|Cowdenbeath|Cromarty|Cumbernauld|"
         "Cunninghame|Dumbarton|Dunbartonshire|Dumbartonshire|Dumfries|Dumfriesshire|Dundee|"
         "Dunfermline|East Kilbride|Eastwood|Edinburgh|Ettrick|Falkirk|Fife|Galloway|Glasgow|"
         "Glenrothes|Gordon|Govan|Grangemouth|Greenock|Hamilton|Inverclyde|Inverness|Kilmarnock|"
         "Kilsyth|Kincardine|Kincardineshire|Kinross|Kinross-shire|Kirkcaldy|Kirkcudbright|"
         "Kirkintilloch|Lanark|Lanarkshire|Lauderdale|Leith|Linlithgow|Livingston|Lochaber|Lothian|"
         "Mearns|Midlothian|Monklands|Moray|Morayshire|Motherwell|Na h-Eileanan|Nairn|Ochil|Orkney|"
         "Paisley|Peebles|Peeblesshire|Perth|Perthshire|Renfrew|Renfrewshire|Ross|Ross-shire|"
         "Roxburgh|Roxburghshire|Rutherglen|Selkirk|Selkirkshire|Shetland|Skye|Stirling|"
         "Stirlingshire|Strathkelvin|Sutherland|Tayside|Tweeddale|Western Isles|West Lothian|"
         "Wigtown|Wishaw|Zetland")
_WALES = ("Aberavon|Aberafan|Aberconwy|Aberdare|Abertillery|Alyn|Anglesey|Ynys M[oô]n|Arfon|Bangor|"
          "Barry|Blaenau Gwent|Bedwellty|Brecon|Breconshire|Brecknock|Bridgend|Caerfyrddin|Caernarfon|"
          "Caernarvon|Caerphilly|Cardiff|Cardigan|Cardiganshire|Ceredigion|Carmarthen|Clwyd|Conwy|"
          "Conway|Cynon|Delyn|Denbigh|Dwyfor|Ebbw Vale|Flint|Flintshire|Glamorgan|Gower|Gwynedd|"
          "Islwyn|Llanelli|Llanelly|Meirionnydd|Merioneth|Merionethshire|Merthyr|Monmouth|"
          "Monmouthshire|Montgomery|Montgomeryshire|Neath|Newport|Ogmore|Pembroke|Pembrokeshire|"
          "Penarth|Pontypool|Pontypridd|Preseli|Radnor|Radnorshire|Rhondda|Swansea|Torfaen|Wrexham")
_NI = ("Antrim|Armagh|Belfast|Down|Fermanagh|Foyle|Londonderry|Lagan Valley|Mid Ulster|Mid-Ulster|"
       "Newry|Strangford|Tyrone|Upper Bann")
NATION_PATTERNS = [("Northern Ireland", re.compile(rf"\b({_NI})\b")),
                   ("Scotland", re.compile(rf"\b({_SCOT})\b")),
                   ("Wales", re.compile(rf"\b({_WALES})\b"))]
ENGLAND_EXCEPTIONS = {"Berwick-upon-Tweed", "Newport (Isle of Wight)"}
SEAT_ALIASES = {"Reigate and Banstead": "Reigate", "Linsey East": "East Lindsey",
                "Oxon Mid": "Mid Oxon", "Llanelli": "Llanelly",
                "Newport (Monmouthshire/Gwent)": "Newport", "Booth Ferry": "Boothferry"}
GSS_NATION = {"E": "England", "S": "Scotland", "W": "Wales", "N": "Northern Ireland"}


def nation_of(name, gss=""):
    if gss and gss[0] in GSS_NATION:
        return GSS_NATION[gss[0]]
    if name in ENGLAND_EXCEPTIONS:
        return "England"
    for nation, pattern in NATION_PATTERNS:
        if pattern.search(name):
            return nation
    return "England"


def norm(name):
    """Constituency names as matching keys: "Glasgow, Govan" = "Glasgow Govan"."""
    name = name.lower().replace("&", "and").replace("ô", "o").replace("ŵ", "w")
    return re.sub(r"[^a-z]+", " ", name).strip()


WORD_FIX = {"western": "west", "eastern": "east", "northern": "north", "southern": "south",
            "borough": "", "burghs": "", "the": "", "of": "", "district": ""}


def variants(key):
    """Looser forms of a constituency key, most exact first: word order
    ignored ("Fife West" = "West Fife"), small words and "-shire" dropped, and
    a leading London borough or city prefix dropped ("Lambeth Norwood")."""
    words = [WORD_FIX.get(w, w) for w in key.split()]
    words = [w for w in words if w]
    out = [key, " ".join(sorted(words)),
           " ".join(sorted(w[:-5] if w.endswith("shire") and len(w) > 7 else w for w in words))]
    for drop in (1, 2, 3):
        if len(words) > drop:
            out.append(" ".join(sorted(words[drop:])))
    return out


def cached(url, name, tries=5):
    path = CACHE / name
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fetch(url, tries=tries))
    return path


def days(a, b):
    return (Date.fromisoformat(a) - Date.fromisoformat(b)).days


def read_csv(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def data_files():
    return sorted(OUT_DIR.glob("*.csv")) + sorted(STANDIN_DIR.glob("*.csv"))


class Context:
    def __init__(self):
        people = load_people_json()
        self.persons = {p["id"]: p for p in people["persons"]}
        posts = {p["id"]: p for p in people["posts"] if p.get("organization_id") == "house-of-commons"}
        self.post_name = {pid: p["area"]["name"] for pid, p in posts.items()}
        self.post_gss = {pid: next((i["identifier"] for i in p.get("identifiers", [])
                                    if i["scheme"] == "gss"), "") for pid, p in posts.items()}
        self.post_keys = {pid: {norm(p["area"]["name"])} | {norm(n) for n in p["area"].get("other_names", [])}
                          for pid, p in posts.items()}
        for pid, p in posts.items():  # names the two sources spell differently
            extra = SEAT_ALIASES.get(p["area"]["name"])
            if extra:
                self.post_keys[pid].add(norm(extra))
        self.seats = defaultdict(list)  # person -> Commons memberships
        for m in people["memberships"]:
            if m.get("post_id") in posts:
                self.seats[m["person_id"]].append(m)
        self.first_elected = {p: min(m.get("start_date", "9999") for m in ms)
                              for p, ms in self.seats.items()}
        self.load_results()
        self.load_posts()

    # --- seats and elections ---------------------------------------------
    def seat_on(self, pid, date):
        for m in self.seats.get(pid, []):
            if m.get("start_date", "0000") <= date <= (m.get("end_date") or "9999"):
                return m
        return None

    def load_results(self):
        """{(election date, constituency key): margin in points} from the
        general election results, 1959-2019, plus 2024 by ONS code."""
        self.margin, self.margin_gss, self.loose = {}, {}, {}
        try:
            rows = csv.reader(io.StringIO(cached(ELECTIONS_URL, "general_elections.csv")
                                          .read_text(encoding="utf-8")))
            votes = defaultdict(list)
            for r in rows:
                if len(r) < 9 or r[2] < "1959" or r[2] not in ELECTION_DATES:
                    continue
                unopposed = r[7] == "1"
                v = float(r[8]) if r[8] else 0.0
                votes[(r[2], norm(r[4]))].append((v, unopposed))
            for key, vs in votes.items():
                self.margin[key] = self.margin_pct([v for v, _ in vs], any(u for _, u in vs))
            # Looser keys, kept only where they point to a single seat.
            loose = defaultdict(set)
            for ge, key in self.margin:
                for v in variants(key)[1:]:
                    loose[(ge, v)].add(key)
            self.loose = {k: next(iter(v)) for k, v in loose.items() if len(v) == 1}
        except Exception as err:
            print(f"  election results unavailable ({err})")
        try:
            text = cached(GE2024_URL, "general_election_2024.csv", tries=2).read_text(encoding="utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(text)))
            cols = rows[0].keys() if rows else []
            code = next(c for c in cols if "geographic code" in c.lower() and "constituency" in c.lower())
            count = next(c for c in cols if "vote count" in c.lower())
            votes = defaultdict(list)
            for r in rows:
                votes[r[code]].append(float(r[count] or 0))
            for gss, vs in votes.items():
                self.margin_gss[gss] = self.margin_pct(vs, False)
            print(f"  2024 results: {len(self.margin_gss)} constituencies")
        except Exception as err:
            print(f"  2024 results unavailable ({err}); q_majority_pct blank after July 2024")

    @staticmethod
    def margin_pct(votes, unopposed):
        votes = sorted(votes, reverse=True)
        if unopposed or len(votes) < 2 or sum(votes) == 0:
            return None
        return round(100 * (votes[0] - votes[1]) / sum(votes), 1)

    def majority_pct(self, pid, seat, date):
        i = bisect_right(ELECTION_DATES, date) - 1
        if i < 0 or not seat:
            return None
        ge = ELECTION_DATES[i]
        # Only if the MP won this seat at that election, not at a by-election.
        # (Memberships start on polling day or within a few days of it.)
        won = any(m.get("post_id") == seat["post_id"] and m.get("start_date")
                  and 0 <= days(m["start_date"][:10], ge) <= 7
                  for m in self.seats.get(pid, []))
        if not won:
            return None
        if ge == "2024-07-04":
            return self.margin_gss.get(self.post_gss.get(seat["post_id"], ""))
        keys = self.post_keys.get(seat["post_id"], ())
        for key in keys:
            if (ge, key) in self.margin:
                return self.margin[(ge, key)]
        for key in keys:
            for v in variants(key):
                hit = self.loose.get((ge, v))
                if hit:
                    return self.margin[(ge, hit)]
        return None

    # --- frontbench posts --------------------------------------------------
    def load_posts(self):
        self.posts = defaultdict(list)
        try:
            data = json.loads(cached(POSTS_URL, "ministers-2010.json").read_text(encoding="utf-8"))
        except Exception as err:
            print(f"  frontbench posts unavailable ({err}); q_bench left blank")
            self.posts = None
            return
        for m in data["memberships"]:
            source = m.get("source", "")
            role = m.get("role", "")
            # Party leaders (Liberal Democrat, SNP and others) are recorded
            # as party posts, not opposition posts, but lead from the front.
            if source == "datadotparl/parliamentarypost" and LEADER_ROLE.search(role) \
                    and not re.search(r"Lords|Peers|Deputy", role):
                source = "datadotparl/oppositionpost"
            if source not in ("datadotparl/governmentpost", "datadotparl/oppositionpost"):
                continue
            if re.search(r"Parliamentary Private Secretary|\bPPS\b", role):
                continue
            kind = "government" if source.endswith("governmentpost") else "opposition_frontbench"
            self.posts[m["person_id"]].append((m.get("start_date", "0000"),
                                               m.get("end_date") or "9999", kind, role))

    def bench(self, pid, date):
        if self.posts is None or date < BENCH_FROM or not pid:
            return "", ""
        held = [(kind, role) for s, e, kind, role in self.posts.get(pid, []) if s <= date <= e]
        if not held:
            return "backbench", ""
        # A government post outranks an opposition one held in the same week
        # around a change of government.
        held.sort(key=lambda h: h[0] != "government")
        return held[0][0], "; ".join(sorted({r for _, r in held}))[:150]


def wikidata_births(qids):
    """{Q-id: birth date} from Wikidata."""
    found = {}

    def query(batch):
        values = " ".join(f"wd:{q}" for q in batch)
        return f"SELECT ?item ?born WHERE {{ VALUES ?item {{ {values} }} ?item wdt:P569 ?born . }}"

    for bindings in add_gender.run_batches(sorted(qids), query):
        for b in bindings:
            q = b["item"]["value"].rsplit("/", 1)[-1]
            born = b["born"]["value"][:10]
            if re.match(r"\d{4}-\d\d-\d\d", born) and q not in found:
                found[q] = born
    print(f"  Wikidata birth dates found: {len(found)}")
    return found


# --- Speaker interventions, counted from their text ---------------------------
# The Speaker calling the next Member ("I call the Leader of the Opposition",
# "Finally, I call Tim Farron", or a bare name in older Hansard) is not an
# intervention; Hansard records such calls far more often since 2019.
CALL = re.compile(r"(^|[.!?]\s+)(and\s+)?(finally,?\s+)?(I\s+(now\s+)?call|we\s+(now\s+)?come\s+to|"
                  r"I\s+will\s+call|let\s+us\s+(go|hear))\b[^.!?]*[.!?]?\s*$", re.I)
NAME_ONLY = re.compile(r"^\s*((Mr|Mrs|Miss|Ms|Dr|Sir|Dame)\.?\s+[A-Z][\w'’-]+(\s+[A-Z][\w'’-]+){0,3}"
                       r"|The Prime Minister|The right hon\.? [\w ]+)\.?\s*$")
ORDER_CALL = re.compile(r"(^|[.!?]\s+)order\b\s*([.!,—–-]|$)", re.I)
WITHDRAW_OR_NAME = re.compile(r"\bwithdraw|\bI (must )?name\b", re.I)
SPEAKER_FIELDS = ["speaker_interventions", "speaker_calls", "speaker_order", "speaker_withdraw",
                  "session_speaker_interventions"]


def is_call(text):
    """A short call to the next Member, with no ruling in it."""
    return ((bool(CALL.search(text)) and len(text.split()) <= 15 and not ORDER_CALL.search(text))
            or bool(NAME_ONLY.match(text)))


def recount_speaker(rows):
    """Recount each exchange's Speaker interventions from speaker_text, with
    calls to the next Member counted separately; the session total is the sum
    over the session's exchanges."""
    if not rows or "speaker_text" not in rows[0]:
        return
    per_session = Counter()
    for r in rows:
        texts = [t for t in (r.get("speaker_text") or "").split(" || ") if t.strip()]
        calls = [t for t in texts if is_call(t)]
        rest = [t for t in texts if not is_call(t)]
        r["speaker_interventions"] = len(rest)
        r["speaker_calls"] = len(calls)
        r["speaker_order"] = sum(bool(ORDER_CALL.search(t)) for t in rest)
        r["speaker_withdraw"] = sum(bool(WITHDRAW_OR_NAME.search(t)) for t in rest)
        per_session[r["date"]] += len(rest)
    for r in rows:
        r["session_speaker_interventions"] = per_session[r["date"]]


# --- Suspected missing sessions ------------------------------------------------
DIAG_DIR = Path("data/diagnostics")
MISSING_REPORT = Path("data/reports/missing_sessions.csv")
ORAL_HEADING = re.compile(r"oral answers|\bwas asked\b", re.I)


def refresh_missing_report():
    """Re-check days noted as missing: only those whose headings show question
    time stay in the report; the rest are relabelled as days without questions.
    (Older runs counted "I asked the Minister" in a debate as question time.)"""
    if not DIAG_DIR.exists():
        return
    missing = []
    for path in sorted(DIAG_DIR.glob("*.csv")):
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows, fields = list(reader), reader.fieldnames
        changed = False
        for r in rows:
            if r.get("note", "").startswith("MISSING"):
                if ORAL_HEADING.search(r.get("detail", "")):
                    missing.append(r)
                else:
                    r["note"] = "no PMQs section found (no questions held)"
                    changed = True
        if changed:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
    if missing:
        MISSING_REPORT.parent.mkdir(parents=True, exist_ok=True)
        with open(MISSING_REPORT, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(missing[0].keys()))
            w.writeheader()
            w.writerows(missing)
    print(f"  {len(missing)} suspected missing sessions in {MISSING_REPORT}")


def loto_on(date):
    for name, pid, start, end, status in LOTO:
        if start <= date < end:
            return name, PID + pid, status
    return "", "", ""


def pm_term_start(pm, date):
    for _, full, start, end in PRIME_MINISTERS:
        if full == pm and start <= date <= end:
            return start
    return None


def main():
    ctx = Context()

    # Birth dates: from the cache, plus Wikidata for questioners not yet looked up.
    births = {r["person_id"]: r["birth_date"] for r in read_csv(BIRTH_FILE)}
    pids = set()
    for path in data_files():
        pids |= {r["q_pid"] for r in read_csv(path) if r.get("q_pid")}
    todo = {}
    for p in pids - set(births):
        q = next((i["identifier"] for i in ctx.persons.get(p, {}).get("identifiers", [])
                  if i["scheme"] == "wikidata"), None)
        if q:
            todo[q] = p
    if todo:
        print(f"  asking Wikidata for {len(todo)} birth dates")
        for q, born in wikidata_births(todo).items():
            births[todo[q]] = born
    with open(BIRTH_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["person_id", "birth_date"])
        for p in sorted(births):
            w.writerow([p, births[p]])

    filled = Counter()
    n = 0
    for path in data_files():
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fields = list(reader.fieldnames or [])
        if not rows:
            continue
        recount_speaker(rows)
        for r in rows:
            d, pid = r["date"], r.get("q_pid", "")
            i = bisect_right(ELECTION_DATES, d) - 1
            ge, parl, gov_seats, total = ELECTIONS[i] if i >= 0 else (None, "", "", "")
            start = pm_term_start(r.get("pm", ""), d)
            loto, loto_pid, status = loto_on(d)
            seat = ctx.seat_on(pid, d) if pid else None
            cons = ctx.post_name.get(seat["post_id"], "") if seat else ""
            first = ctx.first_elected.get(pid)
            born = births.get(pid)
            margin = ctx.majority_pct(pid, seat, d) if seat else None
            bench, post = ctx.bench(pid, d)
            r.update({
                "parliament": parl,
                "days_since_election": days(d, ge) if ge else "",
                "days_in_office": days(d, start) if start else "",
                "gov_seats": gov_seats,
                "gov_majority": 2 * gov_seats - total if ge else "",
                "loto": loto, "loto_status": status,
                "q_is_loto": int(bool(pid) and pid == loto_pid),
                "q_constituency": cons,
                "q_nation": nation_of(cons, ctx.post_gss.get(seat["post_id"], "")) if seat else "",
                "q_years_in_commons": round(days(d, first) / 365.25, 1) if first and first <= d else "",
                "q_age": math.floor(days(d, born) / 365.25) if born else "",
                "q_majority_pct": "" if margin is None else margin,
                "q_bench": bench, "q_post": post,
            })
            n += 1
            for c in ("q_constituency", "q_years_in_commons", "q_age", "q_majority_pct", "q_bench"):
                filled[c] += r[c] != ""
        fields += [c for c in SPEAKER_FIELDS + FIELDS if c not in fields and (c in rows[0])]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    refresh_missing_report()
    print(f"context added to {n} exchanges; filled: " +
          ", ".join(f"{c} {filled[c] / max(n, 1):.1%}" for c in filled))


if __name__ == "__main__":
    main()
