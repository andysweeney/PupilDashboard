#!/usr/bin/env python3
"""
Aspect dump - the school's assessment aspects, ready to confirm
===============================================================

WHAT THIS IS FOR
    Nothing in an assessment result says "effort" or "Maths". The aspect
    is the only thing carrying that, and aspects are defined per school.
    So every school needs a mapping:  aspect mis_id -> subject + role.

    This produces that mapping as a spreadsheet. It reads the school's
    aspect catalogue, works out which aspects actually carry results,
    proposes a subject and role for each using aspect_map.py, and writes a
    CSV for a human to check.

WHAT YOU DO WITH IT
    1. run this
    2. open aspects_to_confirm.csv in Excel
    3. fix anything wrong in the "subject" and "role" columns, delete the
       rows you do not want, save
    4. run this again with --build to turn the corrected CSV into the
       "aspects" block for key.json

    Aspects with results are listed first - those are the ones that matter.
    A catalogue of several thousand usually has a few dozen in real use.

REQUIRES
    aspect_map.py alongside this script, and the school's key.json for its
    canonical subject list.

USAGE
    python aspect_dump.py                     fetch and propose
    python aspect_dump.py --build             turn the confirmed CSV into JSON
    python aspect_dump.py --no-results        skip the results scan (faster)

Every request is a GET. Nothing is written to the MIS. No pupil data is
written to the output - only aspect definitions and counts.
"""

import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import aspect_map
except ImportError:
    print("aspect_map.py must sit alongside this script.")
    sys.exit(1)

# ============================================================================
TOKEN = "PUT_YOUR_WONDE_TOKEN_HERE"
SCHOOL_ID = ""                    # blank = first school the token can see
KEY_PATH = "key.json"             # the school's key, for canonical subjects

CSV_OUT = "aspects_to_confirm.csv"
JSON_OUT = "aspects_block.json"
MAX_RESULTS_SCAN = 40000          # how many results to scan for "in use"
# ============================================================================

BASE = "https://api.wonde.com"
TIMEOUT = 120
_calls = 0


def get(url, retries=3):
    global _calls
    _calls += 1
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept-Encoding", "identity")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT,
                                        context=ssl.create_default_context()) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace")), None
        except urllib.error.HTTPError as e:
            b = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            try:
                return e.code, json.loads(b), None
            except json.JSONDecodeError:
                return e.code, None, b[:200]
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return None, None, "%s: %s" % (type(e).__name__, e)
    return None, None, "gave up"


def rows(b):
    if isinstance(b, dict):
        d = b.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return [d]
    return []


def u(path, params=None):
    return BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")


def page(path, params, cap, label=""):
    q = dict(params or {})
    q["cursor"] = "true"
    q.setdefault("per_page", "200")
    url = u(path, q)
    out, n = [], 0
    while url and len(out) < cap and n < 400:
        st, body, err = get(url)
        n += 1
        if st != 200 or not isinstance(body, dict):
            break
        batch = rows(body)
        out.extend(batch)
        if label and n % 25 == 0:
            print("    %s ... %d rows" % (label, len(out)))
        nxt = ((body.get("meta") or {}).get("pagination") or {}).get("next")
        if not nxt or not batch:
            break
        url = nxt
    return out[:cap]


def unwrap(v):
    if isinstance(v, dict) and "data" in v:
        return v["data"]
    return v


def idof(v):
    v = unwrap(v)
    return v.get("id") if isinstance(v, dict) else v


# ---------------------------------------------------------------------------

def fetch(scan_results=True):
    global BASE, SCHOOL_ID
    st, body, err = get(u("/v1.0/schools", {"per_page": "50"}))
    schools = rows(body)
    if not schools:
        print("No schools visible (HTTP %s) %s" % (st, err or ""))
        sys.exit(1)
    sc = (next((s for s in schools if s.get("id") == SCHOOL_ID), None)
          if SCHOOL_ID else None) or schools[0]
    SCHOOL_ID = sc["id"]
    dom = (sc.get("region") or {}).get("domain")
    if dom:
        BASE = "https://" + dom
    sp = "/v1.0/schools/" + SCHOOL_ID
    print("School %s (%s)" % (SCHOOL_ID, sc.get("name")))

    print("  fetching aspects...")
    aspects = page(sp + "/assessment/aspects", {}, 20000, "aspects")
    print("  %d aspects" % len(aspects))

    used = None
    counts = Counter()
    if scan_results:
        print("  scanning results to find which aspects are in use")
        print("  (this is the slow part; --no-results skips it)")
        res = page(sp + "/assessment/results",
                   {"include": "aspect"}, MAX_RESULTS_SCAN, "results")
        for r in res:
            a = idof(r.get("aspect"))
            if a is not None:
                counts[str(a)] += 1
        used = set(counts)
        print("  %d results scanned, %d distinct aspects carry results"
              % (len(res), len(used)))
        if len(res) >= MAX_RESULTS_SCAN:
            print("  NOTE: hit the %d row cap, so 'in use' may be incomplete"
                  % MAX_RESULTS_SCAN)

    return aspects, counts, used


def write_review_csv(aspects, counts, used, key, path):
    """One row per aspect, proposals filled in, most useful first."""
    # Wonde ids are the provider's; mis_id is the SIMS id a school quotes.
    norm = [{"mis_id": a.get("mis_id"), "provider_id": a.get("id"),
             "name": a.get("name")} for a in aspects if isinstance(a, dict)]

    prop = aspect_map.propose_mappings(
        [{"mis_id": n["mis_id"], "name": n["name"]} for n in norm], key)
    mapped = prop["mapped"]
    unresolved = {u["id"]: u for u in prop["unresolved"]}

    out = []
    for n in norm:
        mid = str(n["mis_id"]) if n["mis_id"] is not None else ""
        pid = str(n["provider_id"] or "")
        nres = counts.get(pid, 0) or counts.get(mid, 0)
        m = mapped.get(mid)
        unr = unresolved.get(mid)
        out.append({
            "confirm": "y" if m else "",
            "mis_id": mid,
            "aspect_name": n["name"] or "",
            "subject": (m or {}).get("subject", ""),
            "role": (m or {}).get("role", "") or (unr or {}).get("role", ""),
            "results": nres,
            "note": ("" if m else
                     ("subject not recognised: %s" % unr["subjectToken"]
                      if unr else "no pattern matched")),
            "provider_id": pid,
        })

    # in use first, then proposed, then the rest
    out.sort(key=lambda r: (-(r["results"] > 0), -bool(r["subject"]),
                            -r["results"], r["aspect_name"]))

    cols = ["confirm", "mis_id", "aspect_name", "subject", "role", "results",
            "note", "provider_id"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in out:
            w.writerow(r)

    inuse = [r for r in out if r["results"] > 0]
    ready = [r for r in out if r["confirm"] == "y"]
    print()
    print("  wrote %s" % path)
    print("    %d aspects total" % len(out))
    print("    %d carry results" % len(inuse))
    print("    %d proposed and marked confirm=y" % len(ready))
    print("    %d need a subject alias" % len(prop["unresolved"]))
    print("    %d matched no pattern" % len(prop["unmatched"]))
    if inuse:
        print()
        print("  aspects in use, most results first:")
        for r in inuse[:15]:
            print("      %-8s %-34s %-14s %-9s %5d"
                  % (r["mis_id"], r["aspect_name"][:34], r["subject"],
                     r["role"], r["results"]))
    return out


def build_from_csv(path, json_path):
    """Turn the confirmed CSV back into the aspects block for key.json."""
    if not os.path.exists(path):
        print("%s not found - run without --build first." % path)
        sys.exit(1)
    block, skipped, bad = {}, 0, []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("confirm") or "").strip().lower() not in ("y", "yes", "1"):
                skipped += 1
                continue
            mid = (r.get("mis_id") or "").strip()
            subj = (r.get("subject") or "").strip()
            role = (r.get("role") or "").strip().lower()
            if not mid or not subj or role not in ("ability", "effort",
                                                   "target", "behaviour"):
                bad.append(r.get("aspect_name") or mid)
                continue
            block[mid] = {"subject": subj, "role": role,
                          "aspectName": (r.get("aspect_name") or "").strip()}
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"aspects": block}, f, indent=2, sort_keys=True)
    print("wrote %s" % json_path)
    print("  %d aspects confirmed, %d rows skipped" % (len(block), skipped))
    if bad:
        print("  %d rows had a missing subject or an invalid role:" % len(bad))
        for b in bad[:10]:
            print("      %s" % b)
        print("  role must be one of: ability, effort, target, behaviour")
    by_role = Counter(v["role"] for v in block.values())
    by_subj = Counter(v["subject"] for v in block.values())
    print("  by role: %s" % dict(by_role))
    print("  subjects covered: %d" % len(by_subj))
    print()
    print("Paste the contents of %s into key.json as a top-level" % json_path)
    print("\"aspects\" block, alongside \"subjects\" and \"scales\".")


def main():
    build = "--build" in sys.argv
    scan = "--no-results" not in sys.argv

    key = {}
    if os.path.exists(KEY_PATH):
        try:
            key = json.load(open(KEY_PATH, encoding="utf-8"))
            print("loaded %s (%d canonical subjects)"
                  % (KEY_PATH, len((key.get("subjects") or {}).get("canonical") or {})))
        except Exception as e:
            print("could not read %s: %s" % (KEY_PATH, e))
    else:
        print("no %s found - proposals will have no canonical subjects to"
              " match against, so most will need a subject typing in." % KEY_PATH)

    if build:
        build_from_csv(CSV_OUT, JSON_OUT)
        return

    if "PUT_YOUR" in TOKEN:
        print("Put your Wonde token in the CONFIG block first.")
        sys.exit(1)

    print("=" * 70)
    print("ASPECT DUMP   %s" % date.today().isoformat())
    print("=" * 70)
    aspects, counts, used = fetch(scan_results=scan)
    write_review_csv(aspects, counts, used, key, CSV_OUT)
    print()
    print("  %d API calls" % _calls)
    print()
    print("NEXT")
    print("  1. open %s" % CSV_OUT)
    print("  2. check the rows with results; fix subject and role; set")
    print("     confirm to y on the ones you want, blank on the rest")
    print("  3. python aspect_dump.py --build")


if __name__ == "__main__":
    main()
