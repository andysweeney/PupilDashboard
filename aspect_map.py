"""aspect_map.py — turn a provider's assessment aspects into subject + role.

WHY THIS EXISTS
---------------
Both Wonde and Xporter return assessment results as one row per aspect:

    {aspect: 705, student: 14262, value: "2", resultset: "Year 8 Autumn Term"}

Nothing in that row says "effort" or "Maths". The aspect is the only thing
carrying it. An aspect is one measurable thing - one column heading on a
marksheet - and it is defined per school, so there is no universal list.

This module holds the mapping and the machinery around it:

    aspect mis_id  ->  { subject, role, scale }

ROLES
    ability   an attainment / working-at / current grade
    effort    effort, ATL, attitude to learning
    target    a target or predicted grade
    behaviour a per-subject behaviour grade (distinct from conduct incidents)

WHY mis_id
    Wonde issues its own ids (A1857757900); Xporter uses SIMS ids. Both
    expose the SIMS id, and 705 was "English Effort" at both test schools.
    Keying on mis_id means the map survives a provider change and matches
    what a school tells you when they say "we use 705".

HOW A SCHOOL IS ONBOARDED
    1. propose_mappings() runs the patterns over the school's aspect
       catalogue and proposes what it can
    2. a human confirms or corrects - most of the work is already done
    3. the confirmed map is written into key.json under "aspects"
    4. anything unmatched becomes an unmapped_aspect flag in the Admin panel

Nothing here talks to an API. It takes a catalogue and returns a mapping,
so it can be developed and tested against saved fixtures.
"""

import re
from collections import Counter, OrderedDict

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
# Each entry is (role, compiled pattern, subject group).
# Ordered: the first match wins, so put the specific before the general.
#
# These come from two real schools' catalogues. "Act:" and "Tgt:" are a SIMS
# convention seen at both, so they are built in rather than per-school. The
# app's original patterns (On track for X / Predicted X / Target X) are kept
# because they match the CSV exports.
_PATTERNS = [
    # --- effort -----------------------------------------------------------
    ("effort", r"^(?P<subj>.+?)\s+Effort(?:\s+GCSE)?$"),
    ("effort", r"^(?P<subj>.+?)\s+ATL$"),
    ("effort", r"^ATL\s+(?P<subj>.+)$"),
    ("effort", r"^(?P<subj>.+?)\s+Attitude(?:\s+to\s+Learning)?$"),

    # --- per-subject behaviour grade --------------------------------------
    ("behaviour", r"^(?P<subj>.+?)\s+Behaviour$"),
    ("behaviour", r"^(?P<subj>.+?)\s+Conduct$"),

    # --- target / prediction ----------------------------------------------
    ("target", r"^Tgt:\s*(?P<subj>.+)$"),
    ("target", r"^(?:Target|Predicted)\s+(?P<subj>.+)$"),
    ("target", r"^(?P<subj>.+?)\s+Target$"),
    ("target", r"^(?P<subj>.+?)\s+Predicted(?:\s+Grade)?$"),

    # --- attainment -------------------------------------------------------
    ("ability", r"^Act:\s*(?P<subj>.+)$"),
    ("ability", r"^On track for\s+(?P<subj>.+)$"),
    ("ability", r"^(?P<subj>.+?)\s+Attainment(?:\s+Level)?$"),
    ("ability", r"^AGS\s*-\s*(?P<subj>.+?)\s+Working At$"),
    ("ability", r"^(?P<subj>.+?)\s+Working At(?:\s+\d+)?$"),
    ("ability", r"^(?P<subj>.+?)\s+Current(?:\s+Grade)?$"),
]

_COMPILED = [(role, re.compile(rx, re.I)) for role, rx in _PATTERNS]

# Qualification and tier noise to strip out of a subject token before matching
# it to a canonical subject. Seen in real data: "GCSE Rel Stud (9-1)",
# "GCE-AS Rel Stud", "WJEC L1/2 Vocational Award in Retail".
_QUAL_NOISE = re.compile(
    r"\b(GCSE|GCE|GCE-AS|AS|A2|A-?Level|BTEC|WJEC|OCR|AQA|Edexcel|RSL|NCFE|"
    r"Cambridge|Nationals?|Vocational|Award|Certificate|Diploma|Tech|"
    r"L\d[a-z]?C?|Level\s*\d)\b", re.I)
# Tier and level notation: "(9-1)", "L1/2", and the "in" of "Award in Retail".
_TIER_NOISE = re.compile(r"\(\s*[\d\-\u2013/ ]+\s*\)|\bL?\d+\s*/\s*\d+\b|\bin\s+", re.I)
# Any digits left over once qualification noise has gone are tier junk.
_STRAY_DIGITS = re.compile(r"(?:(?<=\s)|^)\d+(?:(?=\s)|$)")


def normalise_subject(raw):
    """Reduce a subject token from an aspect name to something matchable.

    "GCSE Rel Stud (9-1)" -> "rel stud"
    "Maths"               -> "maths"
    """
    s = str(raw or "")
    s = _TIER_NOISE.sub(" ", s)
    s = _QUAL_NOISE.sub(" ", s)
    s = re.sub(r"[^\w&' ]+", " ", s)
    s = _STRAY_DIGITS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def classify(name):
    """(role, raw_subject_token) for an aspect name, or (None, None)."""
    n = str(name or "").strip()
    for role, rx in _COMPILED:
        m = rx.match(n)
        if m:
            return role, m.group("subj").strip()
    return None, None


# ---------------------------------------------------------------------------
# Subject resolution against the school's canonical list
# ---------------------------------------------------------------------------

def build_subject_index(key):
    """Index the school's canonical subjects and aliases for lookup.

    Returns {normalised form: canonical name}. Aliases in key.json win over
    the canonical names themselves, so a school can override anything.
    """
    idx = {}
    subs = (key or {}).get("subjects") or {}
    for canon in (subs.get("canonical") or {}):
        idx[normalise_subject(canon)] = canon
        # a few obvious contractions so the first pass catches more
        idx.setdefault(normalise_subject(canon.replace(" ", "")), canon)
    for alias, canon in (subs.get("aliases") or {}).items():
        idx[normalise_subject(alias)] = canon
    return idx


def resolve_subject(raw, subject_index):
    """Map a raw subject token to a canonical subject, or None."""
    n = normalise_subject(raw)
    if not n:
        return None
    if n in subject_index:
        return subject_index[n]
    # try progressively shorter prefixes: "rel stud 9 1" -> "rel stud"
    parts = n.split()
    for cut in range(len(parts) - 1, 0, -1):
        cand = " ".join(parts[:cut])
        if cand in subject_index:
            return subject_index[cand]
    return None


# ---------------------------------------------------------------------------
# Proposing a map for a school
# ---------------------------------------------------------------------------

def propose_mappings(aspects, key, id_field="mis_id", name_field="name",
                     used_ids=None):
    """Propose an aspect map from a provider's catalogue.

    aspects   list of dicts as returned by the provider
    key       the school's key.json (for canonical subjects and aliases)
    id_field  which field holds the SIMS id - "mis_id" for Wonde,
              "ExternalId" or "Id" for Xporter depending on the endpoint
    used_ids  optional set of aspect ids that actually carry results; when
              given, aspects outside it are ignored, which cuts a catalogue
              of thousands down to the few dozen a school really uses

    Returns a dict with:
        mapped     {mis_id: {subject, role, aspectName, confidence}}
        unmatched  aspects whose NAME did not classify at all
        unresolved aspects that classified but whose subject is not canonical
        summary    counts by role
    """
    subject_index = build_subject_index(key)
    mapped, unmatched, unresolved = OrderedDict(), [], []

    for a in aspects or []:
        if not isinstance(a, dict):
            continue
        aid = a.get(id_field) or a.get("mis_id") or a.get("Id") or a.get("id")
        if aid is None:
            continue
        aid = str(aid)
        if used_ids is not None and aid not in used_ids:
            continue
        name = a.get(name_field) or a.get("Name") or ""
        role, raw_subj = classify(name)
        if role is None:
            unmatched.append({"id": aid, "name": name})
            continue
        canon = resolve_subject(raw_subj, subject_index)
        if canon is None:
            unresolved.append({"id": aid, "name": name, "role": role,
                               "subjectToken": raw_subj})
            continue
        mapped[aid] = {"subject": canon, "role": role, "aspectName": name,
                       "confidence": "pattern"}

    summary = Counter(v["role"] for v in mapped.values())
    return {"mapped": mapped, "unmatched": unmatched,
            "unresolved": unresolved, "summary": dict(summary)}


# ---------------------------------------------------------------------------
# Using the map at import time
# ---------------------------------------------------------------------------

def load_map(key):
    """Read the confirmed aspect map out of key.json. Keys are strings."""
    return {str(k): v for k, v in ((key or {}).get("aspects") or {}).items()}


def resolve_result(row, aspect_map, id_field="aspect_mis_id"):
    """Attach subject and role to one result row.

    Returns (subject, role) or (None, None) if the aspect is unmapped -
    the caller should record an unmapped_aspect flag in that case.
    """
    aid = row.get(id_field)
    if aid is None:
        return None, None
    m = aspect_map.get(str(aid))
    if not m:
        return None, None
    return m.get("subject"), m.get("role")


def flag_values(proposal):
    """Values for the unmapped_aspect and unmapped_subject flags, in the shape
    school_key.dump_flags expects."""
    return {
        "unmapped_aspect": ["%s (%s)" % (a["name"], a["id"])
                            for a in proposal.get("unmatched", [])],
        "unmapped_subject": sorted({a["subjectToken"]
                                    for a in proposal.get("unresolved", [])}),
    }
