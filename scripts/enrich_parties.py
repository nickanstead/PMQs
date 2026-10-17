#!/usr/bin/env python3
"""
enrich_parties.py: identify pre-1997 PMQs questioners and attach their party.

ParlParse has no party data and, for older Hansard, no speaker IDs before the
1997 election. This script fills the gap for every exchange dated before
2 May 1997:

  1. Match the printed name ("Mr. Heath", "Mr. H. Wilson") to the MPs sitting
     on that date, using the ParlParse members list.
  2. Give each MP the party they were elected under, from the UK Parliament's
     general election results (github.com/ukparliament/uk-general-elections).
     MPs first elected at a by-election take their party from their nearest
     general election.
  3. Apply known mid-term defections and any hand resolutions in
     data/reference/manual_names.csv.
  4. Recalculate q_role (government / opposition / leader_of_opposition).

It rewrites q_pid, q_party and q_role in data/pmqs/<year>.csv for 1961-1997,
adds a q_match column saying how each name was resolved, and writes
data/reference/ambiguous_names.csv listing names it could not resolve, with the
candidate MPs, so they can be settled by hand.

Usage: python scripts/enrich_parties.py
"""
import collections
import csv
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from extract_pmqs import CACHE, OUT_DIR, PEOPLE_URL, ROW_FIELDS, fetch, lookup  # noqa: E402

ELECTIONS_URL = ("https://raw.githubusercontent.com/ukparliament/"
                 "uk-general-elections/main/db/data/download.csv")
REF_DIR = Path("data/reference")
MANUAL = REF_DIR / "manual_names.csv"
AMBIGUOUS_REPORT = REF_DIR / "ambiguous_names.csv"
CUTOFF = "1997-05-02"  # ParlParse has party data from the 1997 election on

# Party families for deciding government or opposition before 1997.
# Ulster Unionists took the Conservative whip until the February 1974 election.
CONSERVATIVE = {"Conservative", "Scottish Unionist", "National Liberal and Conservative",
                "Conservative and National Liberal", "National Liberal"}
LABOUR = {"Labour", "Labour Co-op", "Labour Co-Op"}


def family(party, date):
    if party in CONSERVATIVE or (party == "UUP" and date < "1974-02-28"):
        return "Conservative"
    if party in LABOUR:
        return "Labour"
    return party or ""


GOVERNMENT = [("1957-01-10", "1964-10-16", "Conservative"),
              ("1964-10-16", "1970-06-19", "Labour"),
              ("1970-06-19", "1974-03-04", "Conservative"),
              ("1974-03-04", "1979-05-04", "Labour"),
              ("1979-05-04", "1997-05-02", "Conservative")]
MAIN_OPPOSITION = {"Conservative": "Labour", "Labour": "Conservative"}

# Leaders of the Opposition: (surname key, given-name start, from, to, party).
# Used to tag them directly, since a bare "Mr. Wilson" is otherwise ambiguous.
LEADERS = [
    ("gaitskell", "hugh", "1955-12-14", "1963-01-18", "Labour"),
    ("brown", "george", "1963-01-18", "1963-02-14", "Labour"),
    ("wilson", "harold", "1963-02-14", "1964-10-16", "Labour"),
    ("douglashome", "alec", "1964-10-16", "1965-07-28", "Conservative"),
    ("heath", "edward", "1965-07-28", "1970-06-19", "Conservative"),
    ("wilson", "harold", "1970-06-19", "1974-03-04", "Labour"),
    ("heath", "edward", "1974-03-04", "1975-02-11", "Conservative"),
    ("thatcher", "margaret", "1975-02-11", "1979-05-04", "Conservative"),
    ("callaghan", "james", "1979-05-04", "1980-11-10", "Labour"),
    ("foot", "michael", "1980-11-10", "1983-10-02", "Labour"),
    ("kinnock", "neil", "1983-10-02", "1992-07-18", "Labour"),
    ("smith", "john", "1992-07-18", "1994-05-12", "Labour"),
    ("beckett", "margaret", "1994-05-12", "1994-07-21", "Labour"),
    ("blair", "tony", "1994-07-21", "1997-05-02", "Labour"),
]


def leader_on(date):
    for row in LEADERS:
        if row[2] <= date < row[3]:
            return row
    return None

# Mid-term moves that change an MP's side of the House (government to
# opposition or back). Moves between opposition parties, such as Labour to
# the SDP in 1981 while Labour was in opposition, do not change the role and
# are not needed here. Verify before publication.
DEFECTIONS = [
    # (surname key, given-name start, from date, new party)
    ("sillars", "james", "1976-01-26", "Scottish Labour"),
    ("robertson", "john", "1976-01-26", "Scottish Labour"),
    ("prentice", "reg", "1977-10-08", "Conservative"),
    ("brocklebankfowler", "christopher", "1981-03-15", "Social Democratic"),
    ("howarth", "alan", "1995-10-07", "Labour"),
    ("nicholson", "emma", "1995-12-29", "Liberal Democrats"),
    ("thurnham", "peter", "1996-10-12", "Liberal Democrats"),
]

TITLES = re.compile(
    r"(mr|mrs|miss|ms|dr|sir|dame|lady|lord|rev|reverend|captain|major|colonel|commander|"
    r"brigadier|lieut|lieutenant|general|viscount|marquess|earl|hon|rt|right|the|professor|prof)",
    re.I)
COLLECTIVE = re.compile(r"\bhon\.? members?\b|several|an hon", re.I)


def key(text):
    return re.sub(r"[^a-z]", "", text.lower())


def cached(url, name):
    path = CACHE / name
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fetch(url))
    return path


class Members:
    def __init__(self):
        data = json.loads(cached(PEOPLE_URL, "people.json").read_text(encoding="utf-8"))
        self.persons = {p["id"]: p for p in data["persons"]}
        self.posts = {p["id"]: p.get("area", {}).get("name", "") for p in data["posts"]}
        self.memberships = [m for m in data["memberships"]
                            if m.get("post_id") and m.get("end_date", "9999") >= "1955"
                            and m.get("start_date", "") < CUTOFF]
        self.index = collections.defaultdict(list)
        for m in self.memberships:
            for n in self.persons[m["person_id"]].get("other_names", []):
                fam = n.get("family_name") or ""
                if not fam:
                    continue
                given = (n.get("given_name") or "").lower()
                for k in {key(fam), key(fam.split()[-1]), key(re.split(r"[- ]", fam)[-1])}:
                    self.index[k].append((m, given))
        self.party = self._parties()

    def names(self, pid):
        return [((n.get("family_name") or ""), (n.get("given_name") or ""))
                for n in self.persons[pid].get("other_names", []) if n.get("family_name")]

    def label(self, m):
        fam, given = (self.names(m["person_id"]) or [("", "")])[0]
        return f"{given} {fam} ({self.posts.get(m['post_id'], '?')})".strip()

    def _parties(self):
        """Party per membership, from the general election that began it."""
        winners = collections.defaultdict(list)  # polling date -> [(surname, constituency, party)]
        rows = csv.reader(cached(ELECTIONS_URL, "general_elections.csv")
                          .read_text(encoding="utf-8").splitlines())
        best = {}
        for row in rows:
            if len(row) < 9 or not row[1].isdigit() or not 1955 <= int(row[1]) <= 1997:
                continue
            election, date, cons, party, cand, unopposed, votes = (
                row[0], row[2], row[4], row[5], row[6], row[7], row[8])
            v = 10**9 if unopposed == "1" else float(votes or -1)
            k = (election, cons)
            if k not in best or v > best[k][0]:
                best[k] = (v, date, cons, party, cand)
        for v, date, cons, party, cand in best.values():
            forenames = cand.split(",")[1].lower().split() if "," in cand else []
            winners[date].append((key(cand.split(",")[0]), cons, party, forenames))

        def overlap(a, b):
            A = set(re.findall(r"[a-z]+", a.lower())) - {"and", "the", "of"}
            B = set(re.findall(r"[a-z]+", b.lower())) - {"and", "the", "of"}
            return len(A & B) / max(1, len(A | B))

        party = {}
        for m in self.memberships:
            if m.get("start_reason") != "general_election":
                continue
            names = self.names(m["person_id"])
            fams = {key(f) for f, _ in names}
            givens = {g.lower().split()[0] for _, g in names if g.split()}
            hits = [w for w in winners.get(m["start_date"], []) if w[0] in fams]
            if len(hits) > 1:
                # Several winners share the surname: choose by constituency name
                # and forename, and give up rather than guess if neither helps.
                cons = self.posts.get(m["post_id"], "")
                scored = sorted(((overlap(w[1], cons) + (0.5 if givens & set(w[3]) else 0), w)
                                 for w in hits), key=lambda t: t[0])
                if scored[-1][0] > 0 and (len(scored) < 2 or scored[-1][0] > scored[-2][0]):
                    hits = [scored[-1][1]]
                else:
                    hits = []
            if len(hits) == 1:
                party[m["id"]] = hits[0][2]
        by_person = collections.defaultdict(list)
        for m in self.memberships:
            by_person[m["person_id"]].append(m)
        for m in self.memberships:  # by-elections: borrow the nearest general election
            if m["id"] in party:
                continue
            known = [o for o in by_person[m["person_id"]] if o["id"] in party]
            if known:
                nearest = min(known, key=lambda o: abs(int(o["start_date"][:4]) - int(m["start_date"][:4])))
                party[m["id"]] = party[nearest["id"]]
        return party

    def sitting(self, k, date):
        return {m["person_id"]: (m, g) for m, g in self.index.get(k, [])
                if m["start_date"] <= date <= m.get("end_date", "9999")}

    def resolve(self, name, date):
        """Returns (how, membership or None, party or None, candidates)."""
        if COLLECTIVE.search(name):
            return "collective", None, None, {}
        cleaned = re.sub(r"\(.*?\)", "", name).replace(".", " ").replace(",", " ")
        toks = [t for t in cleaned.split() if t]
        while toks and TITLES.fullmatch(toks[0]):
            toks = toks[1:]
        if not toks:
            return "unparsed", None, None, {}
        given = [t.lower() for t in toks[:-1]]
        cands, how = {}, "exact"
        if len(toks) >= 2:  # double-barrelled name printed with a space
            cands = self.sitting(key(toks[-2] + toks[-1]), date)
            if cands:
                given = given[:-1]
        if not cands:
            cands = self.sitting(key(toks[-1]), date)
        if not cands:  # misprint in the old scans: nearest sitting surname
            sitting_keys = [k for k in self.index if self.sitting(k, date)]
            close = difflib.get_close_matches(key(toks[-1]), sitting_keys, n=2, cutoff=0.8)
            if len(close) == 1:
                cands, how = self.sitting(close[0], date), "fuzzy"
        if len(cands) > 1 and given:
            full = [g for g in given if len(g) > 1]
            pick = [pid for pid, (m, g) in cands.items() if full and set(full) & set(g.split())]
            if not pick:
                pick = [pid for pid, (m, g) in cands.items()
                        if g.split() and g.split()[0][:1] == given[0][:1]]
            if pick:
                cands = {pid: cands[pid] for pid in pick}
        if not cands:
            return "none", None, None, {}
        if len(cands) == 1:
            m = next(iter(cands.values()))[0]
            return how, m, self.party.get(m["id"]), cands
        parties = {self.party.get(m["id"]) for m, _ in cands.values()}
        if len(parties) == 1 and None not in parties:
            return "ambiguous_same_party", None, parties.pop(), cands
        return "ambiguous", None, None, cands


def load_manual():
    """Hand resolutions: name, from, to, person_id, party (person_id optional)."""
    if not MANUAL.exists():
        REF_DIR.mkdir(parents=True, exist_ok=True)
        MANUAL.write_text("name,from,to,person_id,party,note\n", encoding="utf-8")
        return []
    with open(MANUAL, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("name")]


def defected(members, pid, date, party):
    for k, given, since, new in DEFECTIONS:
        if date >= since and any(key(f) == k and g.lower().startswith(given)
                                 for f, g in members.names(pid)):
            return new
    return party


def main():
    members = Members()
    manual = load_manual()
    report = collections.defaultdict(lambda: {"count": 0, "first": "9999", "last": "", "cands": {}})
    totals = collections.Counter()
    for path in sorted(OUT_DIR.glob("*.csv")):
        if int(path.stem) > 1997:
            continue
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        changed = False
        for r in rows:
            if r["date"] >= CUTOFF:
                continue
            date, name = r["date"], r["q_name"]
            how, m, party, cands = members.resolve(name, date)
            pid = m["person_id"] if m else ""
            for man in manual:
                if man["name"] == name and man["from"] <= date <= (man["to"] or "9999"):
                    how, pid, party = "manual", man.get("person_id") or "", man["party"]
                    break
            if pid and party:
                party = defected(members, pid, date, party)
            is_leader = False
            lead = leader_on(date)
            if lead and how not in ("collective", "manual"):
                cleaned = [t for t in re.sub(r"\(.*?\)", "", name).replace(".", " ").split()
                           if not TITLES.fullmatch(t)]
                surname_ok = cleaned and (key(cleaned[-1]) == lead[0]
                                          or key("".join(cleaned[-2:])) == lead[0])
                given = [t.lower() for t in cleaned[:-1]]
                given_ok = not given or given[0][:1] == lead[1][:1] or given[-1][:1] == lead[1][:1]
                if surname_ok and given_ok:
                    is_leader, party = True, lead[4]
                    if how in ("ambiguous", "ambiguous_same_party", "none"):
                        how = "leader"
                    leaders = [m2 for m2, g in members.sitting(lead[0], date).values()
                               if g.startswith(lead[1][:3])]
                    if leaders:
                        pid = leaders[0]["person_id"]
            if how == "ambiguous" and not is_leader:
                rep = report[(name, tuple(sorted(cands)))]
                rep["count"] += 1
                rep["first"], rep["last"] = min(rep["first"], date), max(rep["last"], date)
                for m2, _ in cands.values():
                    rep["cands"][m2["person_id"]] = f"{members.label(m2)}: {members.party.get(m2['id'], '?')}"
            gov = lookup(GOVERNMENT, date)
            fam = family(party, date) if party else ""
            if how == "collective":
                role = "collective"
            elif is_leader:
                role = "leader_of_opposition"
            elif not fam:
                role = "unknown"
            elif fam == gov:
                role = "government"
            else:
                role = "opposition"
            r.update({"q_pid": pid, "q_party": party or "", "q_role": role, "q_match": how})
            totals[how] += 1
            totals["with_party"] += bool(party)
            changed = True
        if changed:
            fields = ROW_FIELDS + (["q_match"] if "q_match" not in ROW_FIELDS else [])
            for r in rows:
                r.setdefault("q_match", "")
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
    REF_DIR.mkdir(parents=True, exist_ok=True)
    with open(AMBIGUOUS_REPORT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "exchanges", "first_date", "last_date", "candidate_mps"])
        for (name, _), rep in sorted(report.items(), key=lambda kv: -kv[1]["count"]):
            w.writerow([name, rep["count"], rep["first"], rep["last"], " | ".join(sorted(rep["cands"].values()))])
    n = sum(v for k, v in totals.items() if k != "with_party")
    if n:
        print(f"pre-1997 exchanges: {n}; party found for {totals['with_party']} ({totals['with_party'] / n:.1%})")
        print("  " + ", ".join(f"{k}: {v}" for k, v in totals.most_common() if k != "with_party"))
        print(f"  {len(report)} ambiguous names listed in {AMBIGUOUS_REPORT}")


if __name__ == "__main__":
    main()
