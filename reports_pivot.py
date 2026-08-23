"""reports_pivot.py — turn provider assessment results into Reports.csv.

THE PROBLEM
-----------
Both Wonde and Xporter return assessment results as one row per aspect:

    pupil 14262, aspect 705 (English Effort), value "2", "Year 8 Autumn Term"
    pupil 14262, aspect 702 (English Attainment), value "6", "Year 8 Autumn Term"

The engine wants one row per pupil per subject per collection:

    Name, Date, Subject, Ability Value, Effort Value, OTA Grade

So two or three provider rows fold into one engine row. This module does
that fold, using the aspect map to decide which column each value lands in.

WHAT THE ENGINE NEEDS (from import_engine.py)
    Name           must start with the admission number - identity.
                   admissionNumberPattern is "^(\\d+)", and pid() reads the
                   leading digits. "14262 Smith, J" works; "Smith, J" does not.
    Date           the engine derives the term from this. A "Term" column of
                   the form "T1 2025" is accepted instead.
    Subject        must match the school's canonical subject list.
    Ability Value  raw attainment grade, mapped later by the school's scales
    Effort Value   raw effort value
    OTA Grade      optional GCSE target, 1..9

WHAT THIS MODULE DOES NOT DO
    No network calls, no provider specifics. It takes rows that a fetch
    adapter has already normalised, so it can be developed and tested
    against saved fixtures.
"""

import csv
import re
from collections import Counter, OrderedDict, defaultdict

# The engine's column order for Reports.csv.
COLUMNS = ["Name", "Date", "Subject", "Ability Value", "Effort Value", "OTA Grade"]

# Which role feeds which column.
ROLE_COLUMN = {
    "ability": "Ability Value",
    "effort": "Effort Value",
    "target": "OTA Grade",
    # behaviour is a per-subject grade but has no Reports column; it is
    # carried separately and ignored here.
}

_TERM_RX = re.compile(r"^T[123]\s+\d{4}$")


def pick_value(row):
    """The value a teacher entered.

    Wonde populates grade_value and leaves result null on the school we
    tested, so result is preferred and grade_value is the fallback. A
    trailing ".00" is stripped: the engine matches "2" against the scale,
    not "2.00".
    """
    for field in ("result", "Result", "grade_value", "GradeValue",
                  "NumericValue", "value"):
        v = row.get(field)
        if v not in (None, "", []):
            s = str(v).strip()
            if re.fullmatch(r"-?\d+\.0+", s):
                s = s.split(".")[0]
            return s
    return None


def pupil_label(row, name_field="pupil_name", id_field="pupil_mis_id"):
    """"14262 Smith, J" - the admission number must lead, or the engine
    cannot identify the pupil."""
    pid = row.get(id_field)
    name = str(row.get(name_field) or "").strip()
    if pid is None:
        return name or None
    pid = str(pid).strip()
    if not pid:
        return name or None
    if name.startswith(pid):
        return name
    return ("%s %s" % (pid, name)).strip()


def pivot(rows, aspect_map, name_field="pupil_name", id_field="pupil_mis_id",
          aspect_field="aspect_mis_id", date_fields=("collection_date", "result_date"),
          collection_field="resultset_name"):
    """Fold per-aspect rows into per-pupil-per-subject-per-report rows.

    rows        normalised result rows from a fetch adapter
    aspect_map  {mis_id: {"subject": ..., "role": ...}} from key.json
    date_fields which dates identify the report, in order of preference.
                The default is collection_date then result_date: the
                collection date is when a grade was gathered into a report,
                so a grade awarded in November and collected in December
                belongs to the December report. result_date is the fallback
                for providers or schools that do not set a collection date.
                A school running several reports a term produces several
                dated rows per subject, and the engine allocates each to a
                reporting period from that date.

    Returns (out_rows, stats) where out_rows are dicts keyed by COLUMNS.
    """
    grouped = OrderedDict()
    stats = Counter()
    unmapped_aspects = Counter()
    collisions = []

    for r in rows or []:
        stats["rows_in"] += 1

        aid = r.get(aspect_field)
        m = aspect_map.get(str(aid)) if aid is not None else None
        if not m:
            stats["no_aspect_map"] += 1
            if aid is not None:
                unmapped_aspects[str(aid)] += 1
            continue

        role = m.get("role")
        col = ROLE_COLUMN.get(role)
        if col is None:
            # behaviour, or a role with no Reports column
            stats["role_not_in_reports"] += 1
            continue

        subject = m.get("subject")
        if not subject:
            stats["no_subject"] += 1
            continue

        who = pupil_label(r, name_field, id_field)
        if not who or not re.match(r"\s*\d", who):
            stats["no_pupil_number"] += 1
            continue

        value = pick_value(r)
        if value is None:
            stats["no_value"] += 1
            continue

        # A report is dated, and the engine allocates it to a reporting
        # period from that date. A school can run several reports in a term,
        # so the DATE is the grouping key - not the collection name, which
        # may be shared across them.
        date, date_src = "", None
        for _f in date_fields:
            v = r.get(_f)
            if v not in (None, "", []):
                date, date_src = str(v)[:10], _f
                break
        if not date:
            stats["no_date"] += 1
            continue
        stats["date_from_" + date_src] += 1
        coll = str(r.get(collection_field) or "")

        gkey = (who, subject, date)
        row_out = grouped.get(gkey)
        if row_out is None:
            row_out = {c: "" for c in COLUMNS}
            row_out["Name"] = who
            row_out["Subject"] = subject
            row_out["Date"] = date
            row_out["_collection"] = coll      # diagnostics only, not exported
            grouped[gkey] = row_out
            stats["groups"] += 1

        if row_out[col]:
            # Two results for the same pupil, subject, collection and role.
            # Keep the first and record it - usually a duplicate collection
            # rather than an error, but worth surfacing.
            if row_out[col] != value:
                collisions.append({"pupil": who, "subject": subject,
                                   "column": col, "kept": row_out[col],
                                   "discarded": value, "collection": coll})
                stats["collisions"] += 1
        else:
            row_out[col] = value
            stats["values_placed"] += 1

    out = list(grouped.values())
    stats["rows_out"] = len(out)

    # How complete is each output row?
    per_pair = defaultdict(set)
    for r in out:
        per_pair[(r["Name"], r["Subject"])].add(r["Date"])
    multi = sum(1 for v in per_pair.values() if len(v) > 1)
    stats["pupil_subject_pairs"] = len(per_pair)
    stats["pairs_with_multiple_reports"] = multi
    stats["distinct_dates"] = len({r["Date"] for r in out})

    both = sum(1 for r in out if r["Ability Value"] and r["Effort Value"])
    stats["rows_with_both"] = both
    stats["rows_ability_only"] = sum(1 for r in out
                                     if r["Ability Value"] and not r["Effort Value"])
    stats["rows_effort_only"] = sum(1 for r in out
                                    if r["Effort Value"] and not r["Ability Value"])

    return out, {"counts": dict(stats),
                 "unmapped_aspects": dict(unmapped_aspects),
                 "collisions": collisions[:50]}


def write_csv(out_rows, path):
    """Write Reports.csv in the engine's expected shape."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in out_rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})   # _collection dropped
    return path


def summarise(stats):
    """A few lines a human can read after a run."""
    c = stats["counts"]
    lines = [
        "rows in                : %d" % c.get("rows_in", 0),
        "rows out               : %d" % c.get("rows_out", 0),
        "  with ability + effort: %d" % c.get("rows_with_both", 0),
        "  ability only         : %d" % c.get("rows_ability_only", 0),
        "  effort only          : %d" % c.get("rows_effort_only", 0),
        "values placed          : %d" % c.get("values_placed", 0),
        "skipped, unmapped aspect: %d" % c.get("no_aspect_map", 0),
        "skipped, role not in reports: %d" % c.get("role_not_in_reports", 0),
        "skipped, no pupil number: %d" % c.get("no_pupil_number", 0),
        "skipped, no value      : %d" % c.get("no_value", 0),
        "distinct report dates  : %d" % c.get("distinct_dates", 0),
        "  dated by collection  : %d" % c.get("date_from_collection_date", 0),
        "  dated by result      : %d" % c.get("date_from_result_date", 0),
        "pupil/subject pairs    : %d" % c.get("pupil_subject_pairs", 0),
        "  with >1 report       : %d" % c.get("pairs_with_multiple_reports", 0),
        "skipped, no date       : %d" % c.get("no_date", 0),
        "collisions (same date) : %d" % c.get("collisions", 0),
    ]
    if stats.get("unmapped_aspects"):
        top = sorted(stats["unmapped_aspects"].items(),
                     key=lambda x: -x[1])[:10]
        lines.append("unmapped aspect ids    : "
                     + ", ".join("%s x%d" % (a, n) for a, n in top))
    return "\n".join(lines)
