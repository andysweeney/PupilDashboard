#!/usr/bin/env python3
"""stage_inputs.py — turn a folder of RAW school exports into the import engine's staged inputs.

The dashboard upload portal drops whatever the admin exports into UPLOAD_DIR. This module
detects each file's role from its name (and falls back to column signatures), then writes the
exact filenames import_engine.py expects into OUT_DIR. It folds in every real-world quirk found
in the genuine SIMS exports:

  - four per-year attendance files per cohort, combined into one feed (engine derives year from date)
  - the SIMS duplicate-header glitch where a trailing 'Name' column is really the teacher
  - extra 'Year' column / reordered columns in the current-year attendance file
  - 'Teacher Name' vs 'Teacher' in behaviour exports
  - the house-points file naming the pupil column 'Forename' instead of 'Name'
  - one combined FSM file covering all cohorts
  - SEN delivered as .xlsx for Y10 and .csv for Y11 (matching the engine's two SEN sources)
  - split grade exports ("On track for X" + "X Effort") parsed and joined by (pupil, raw subject)

Subjects are passed through RAW — no mapping happens here. That is the Admin panel's job.
"""
import os, re, sys, shutil, json
import pandas as pd

# ── Term-label recognition (for grade Resultsets) ─────────────────────────────
# Built-in season words → T1/T2/T3. Deliberately CONSERVATIVE: only clearly-standard,
# unambiguous wordings live here. Anything ambiguous (e.g. "Winter") or bespoke is left
# unresolved and surfaced in Admin for the school to map, so a term is never silently
# filed in the wrong place. Order matters only in that the first contained word wins.
_SEASON_WORDS = [
    ('michaelmas', 'T1'), ('mich', 'T1'), ('autumn', 'T1'), ('aut', 'T1'), ('fall', 'T1'),
    ('lent', 'T2'), ('hilary', 'T2'), ('spring', 'T2'), ('spr', 'T2'),
    ('trinity', 'T3'), ('summer', 'T3'), ('sum', 'T3'),
]
_MONTHS_RS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
              'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}


def _norm_term(s):
    """Canonical key for a Resultset label — must match the dashboard's normaliser exactly
    (lowercase, runs of non-alphanumerics collapsed to one space, trimmed)."""
    return re.sub(r'[^a-z0-9]+', ' ', str(s or '').lower()).strip()


def _parse_term_date(s):
    """If the label is (or contains) a date, return (month, year); else None. Only consulted
    when no season word matched, so it never overrides an explicit season."""
    s = str(s or '')
    m = re.search(r'(20\d{2})[-/](\d{1,2})(?:[-/]\d{1,2})?', s)            # 2025-12, 2025/12/05
    if m:
        mo = int(m.group(2))
        if 1 <= mo <= 12:
            return (mo, int(m.group(1)))
    m = re.search(r'\b([a-z]{3,})\s+(20\d{2})', s.lower())                  # December 2025 / Dec 2025
    if m and m.group(1)[:3] in _MONTHS_RS:
        return (_MONTHS_RS[m.group(1)[:3]], int(m.group(2)))
    m = re.search(r'\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2})\b', s)            # 05/12/2025 (d/m/y)
    if m:
        mo = int(m.group(2))
        if 1 <= mo <= 12:
            return (mo, int(m.group(3)))
    return None


def _load_term_map(upload_dir):
    """Per-school overrides for term labels the built-ins don't know: {normalised label: 'T1'|'T2'|'T3'}.
    Rides along like the other admin-editable engine inputs (_calibration_ks*.json, _ability_scale.json)."""
    out = {}
    try:
        p = os.path.join(upload_dir, '_term_map.json')
        if os.path.exists(p):
            with open(p, encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    vs = str(v).strip().upper()
                    if vs in ('T1', 'T2', 'T3'):
                        out[_norm_term(k)] = vs
    except Exception:
        pass
    return out

ATT_COLS = ['Name', 'Reg', 'Mark', 'Date', 'Subject', 'Teacher', 'Period Description']
BEH_COLS = ['Name', 'Date', 'Subject', 'Lesson - Period', 'Incident', 'Teacher']

# Grade term resolution: the report's "Resultset" (e.g. "*Year 10 Summer") carries the SEASON
# (Autumn/Spring/Summer -> T1/T2/T3) and the YEAR GROUP it was collected in, but NOT a calendar
# year. The academic year is recovered per pupil from their intake: AY = intake + (resultYG - 7).
# So "Year 10 Summer" for an intake-2022 pupil -> AY 2025 -> "T3 2025", and the same label for an
# intake-2021 (now Year 11) pupil -> AY 2024. No hard-coded default term: if a row can't be
# resolved it is left blank for the engine to flag, never silently stamped with a guessed term.


def _read(path):
    df = pd.read_csv(path, encoding='utf-8-sig', dtype=str)
    df.columns = df.columns.str.strip()
    return df


def detect(fname):
    """(role, year_group) from a filename; year_group is 10/11/None."""
    f = fname.lower()
    yg = 11 if re.search(r'(year[_ ]?11|y11|ys?7-10|_in_y(?:s)?7)', f) and '11' in f else None
    if yg is None:
        yg = 11 if re.search(r'year[_ ]?11|y11', f) else (10 if re.search(r'year[_ ]?10|y10', f) else None)
    if 'in_year' in f:                                   return ('attendance', yg)
    if 'on_track_for' in f:                              return ('grade_attain', yg)
    if 'effort' in f:                                    return ('grade_effort', yg)
    if 'behaviour_data' in f:                            return ('behave_current', yg)
    if re.search(r'behave.*in.*y', f):                   return ('behave_historic', yg)
    if 'detention' in f:                                 return ('detention', yg)
    if 'fsm' in f:                                       return ('fsm', yg)
    if 'housepoint' in f:                                return ('housepoints', yg)
    if 'sen' in f:                                       return ('sen', yg)
    return (None, None)


def _att_norm(df):
    """Map any attendance export to ATT_COLS, recovering the mislabelled teacher column."""
    if 'Teacher' not in df.columns and 'Name.1' in df.columns:
        df = df.rename(columns={'Name.1': 'Teacher'})
    for c in ATT_COLS:
        if c not in df.columns:
            df[c] = ''
    return df[ATT_COLS]


def _beh_norm(df, historic):
    d = df.copy()
    # Rename source->target ONLY when the target isn't already present; if a file carries both
    # (e.g. 'Name' AND 'Pupil name'), renaming would create two identically-named columns, which
    # makes the later pd.concat fail with "Reindexing only valid with uniquely valued Index objects".
    # In that case keep the existing target column and drop the redundant source.
    for src, tgt in (('Pupil name', 'Name'), ('Teacher Name', 'Teacher')):
        if src in d.columns:
            if tgt in d.columns:
                d = d.drop(columns=[src])
            else:
                d = d.rename(columns={src: tgt})
    # Belt-and-braces: collapse any remaining duplicate labels (keep first) so columns stay unique.
    d = d.loc[:, ~d.columns.duplicated()]
    if 'Teacher' not in d.columns:
        d['Teacher'] = ''
    for c in BEH_COLS:
        if c not in d.columns:
            d[c] = ''
    return d[BEH_COLS]


def _parse_grades(attain_paths, effort_paths, pid_intake=None, fallback_term=None, term_map=None):
    pid_intake = pid_intake or {}
    term_map = term_map or {}
    stats = {'rows': 0, 'unresolved_term': 0, 'unresolved_labels': set()}
    def pidnum(n):
        m = re.match(r'(\d+)', str(n));  return str(int(m.group(1))) if m else None
    def term_of(resultset, pidn):
        # Resolve the SEASON (T1/T2/T3), then the academic YEAR, from the Resultset label.
        s = str(resultset or '').strip(); low = s.lower(); norm = _norm_term(s)
        season = None; ay = None
        # 1) Per-school override wins (covers bespoke wordings the school has mapped in Admin).
        if norm and norm in term_map:
            season = term_map[norm]
        # 2) Built-in season words (Autumn/Spring/Summer + Michaelmas/Lent/Hilary/Trinity/Fall).
        if season is None:
            for w, t in _SEASON_WORDS:
                if w in low:
                    season = t; break
        # 3) Explicit "T1".."T3" / "Term 1".."Term 3".
        if season is None:
            tm = re.search(r'\bt\s*([1-3])\b', low) or re.search(r'term\s*([1-3])', low)
            if tm:
                season = 'T' + tm.group(1)
        # 4) A date label: month → season, year → AY (UK Sept–Aug). Only if nothing above matched.
        if season is None:
            d = _parse_term_date(s)
            if d:
                mo, yr = d
                if mo >= 9:   season, ay = 'T1', yr
                elif mo <= 4: season, ay = 'T2', yr - 1
                else:         season, ay = 'T3', yr - 1
        if season is None:
            if s:
                stats['unresolved_labels'].add(s)
            return fallback_term
        # AY (unless a date already fixed it): prefer year-group + pupil intake, else a calendar year.
        if ay is None:
            ygm = re.search(r'year\s*(\d{1,2})\b', low) or re.search(r'\byr?\s*(\d{1,2})\b', low)
            yg = int(ygm.group(1)) if ygm else None
            intake = pid_intake.get(pidn) if pidn else None
            if yg is not None and 7 <= yg <= 13 and intake is not None:
                ay = intake + (yg - 7)
            else:
                ym = re.search(r'(20\d{2})', s)
                if ym:
                    cal = int(ym.group(1)); ay = cal if season == 'T1' else cal - 1
        if ay is None:
            if s:
                stats['unresolved_labels'].add(s)
            return fallback_term
        return f"{season} {ay}"
    def parse(paths, rx, valcol):
        rows = []
        for p in paths:
            for _, r in _read(p).iterrows():
                bd = str(r.get('Basic details', '')).strip()
                m = re.match(rx, bd)
                if not m:
                    continue
                pidn = pidnum(r.get('Name'))
                tm = term_of(r.get('Resultset'), pidn)
                if tm is None:
                    stats['unresolved_term'] += 1
                rows.append({'pid': pidn, 'Name': r.get('Name'),
                             'Subject': m.group(1).strip(), valcol: r.get('Result'),
                             'Term': tm})
        return pd.DataFrame(rows, columns=['pid', 'Name', 'Subject', valcol, 'Term'])
    # Both metrics can live in ONE file (a single grades export) or in separate attainment/
    # effort files — so parse each metric from the union of all grade files.
    all_paths = list(dict.fromkeys(list(attain_paths) + list(effort_paths)))
    ot = parse(all_paths, r'(?:On track for|Predicted|Target)\s+(.*)$', 'Ability Value')
    ef = parse(all_paths, r'(.*)\s+Effort$', 'Effort Value')
    if ot.empty and ef.empty:
        return pd.DataFrame(columns=['Name', 'Subject', 'Term', 'Ability Value', 'Effort Value']), stats
    m = pd.merge(ot[['pid', 'Name', 'Subject', 'Term', 'Ability Value']],
                 ef[['pid', 'Subject', 'Term', 'Effort Value']],
                 on=['pid', 'Subject', 'Term'], how='outer')
    nm = dict(zip(ef['pid'], ef['Name'])) if not ef.empty else {}
    m['Name'] = m.apply(lambda r: r['Name'] if isinstance(r['Name'], str) else nm.get(r['pid'], r['pid']), axis=1)
    stats['rows'] = len(m)
    return m[['Name', 'Subject', 'Term', 'Ability Value', 'Effort Value']], stats


def stage(upload_dir, out_dir, grade_term=None, current_acad_year=None, verbose=True):
    os.makedirs(out_dir, exist_ok=True)
    buckets = {}
    for fn in sorted(os.listdir(upload_dir)):
        if not fn.lower().endswith(('.csv', '.xlsx')):
            # Admin grade-mapping calibration + custom ability ladder (JSON) ride along verbatim — the
            # engine folds them in. The CSV/XLSX-only path below would otherwise drop them.
            if re.match(r'_calibration_ks[45]\.json$', fn) or fn == '_ability_scale.json':
                shutil.copyfile(os.path.join(upload_dir, fn), os.path.join(out_dir, fn))
            continue
        role, yg = detect(fn)
        if role:
            buckets.setdefault((role, yg), []).append(os.path.join(upload_dir, fn))
    def files(role, yg):
        return buckets.get((role, yg), [])
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    summary = {}

    # Category-generic staging: every file of a category is pooled regardless of which cohort
    # or year group it belongs to. The engine reads these by glob and assigns each pupil's
    # cohort from the roster (by admission number), so NO year group is hardcoded here — a
    # Year 7 cohort's files flow through exactly like a Year 11 cohort's.
    def of_role(*roles):
        return [p for (r, _yg), ps in buckets.items() if r in roles for p in ps]

    att_paths = of_role('attendance')
    if att_paths:
        att = pd.concat([_att_norm(_read(p)) for p in att_paths], ignore_index=True)
        att.to_csv(os.path.join(out_dir, 'attend_updated.csv'), index=False)   # engine globs *attend_updated.csv
        summary['attendance'] = len(att)

    beh_parts = [_beh_norm(_read(p), False) for p in of_role('behave_current')] + \
                [_beh_norm(_read(p), True)  for p in of_role('behave_historic')]
    if beh_parts:
        beh = pd.concat(beh_parts, ignore_index=True)
        beh.to_csv(os.path.join(out_dir, 'Behave_all.csv'), index=False)        # engine globs Behave*.csv
        summary['behaviour'] = len(beh)

    det_paths = of_role('detention')
    if det_paths:
        det = pd.concat([_read(p) for p in det_paths], ignore_index=True)[['Name', 'Detention Date', 'Detention Type']]
        det.to_csv(os.path.join(out_dir, 'Detention_all.csv'), index=False)      # engine globs Detention*.csv
        summary['detentions'] = len(det)

    # FSM — one combined file covers every cohort (engine reads FSM.csv once against the full registry)
    fsm_paths = [p for (role, _), ps in buckets.items() if role == 'fsm' for p in ps]
    if fsm_paths:
        fsm = pd.concat([_read(p) for p in fsm_paths], ignore_index=True)
        fsm.to_csv(os.path.join(out_dir, 'FSM.csv'), index=False)
        summary['fsm'] = len(fsm)

    # SEN status — one whole-school snapshot. Pool every SEN file, normalise the status column
    # (exports use either 'SEN Status Code' or 'SEN Status'), and write the engine's SEN.csv.
    sen_all = of_role('sen')
    if sen_all:
        parts = []
        for p in sen_all:
            s = _read(p)
            s['SEN Status'] = s.get('SEN Status Code', s.get('SEN Status'))
            parts.append(s[[c for c in ['Name', 'SEN Status'] if c in s.columns]])
        sen = pd.concat(parts, ignore_index=True).dropna(subset=['Name'])
        sen.to_csv(os.path.join(out_dir, 'SEN.csv'), index=False)
        summary['sen'] = len(sen)

    # ROSTER — the authoritative pupil->cohort map. Derived from the SEN-with-Gender exports
    # (they carry the current year group + gender for the whole cohort), anchored to the
    # confirmed current academic year. This is what makes the engine cohort-generic: add a
    # cohort's SEN export and its pupils appear in the roster, no code change.
    sen_paths = [p for (role, _), ps in buckets.items() if role == 'sen' for p in ps]
    if sen_paths and current_acad_year:
        try:
            from derive_roster import derive_roster, write_roster_csv
            roster, rsum, anomalies = derive_roster(sen_paths, int(current_acad_year))
            write_roster_csv(roster, os.path.join(out_dir, 'Roster.csv'))
            summary['roster'] = rsum.get('pupils', len(roster))
            if anomalies:
                log(f"  roster anomalies (for Admin review): {len(anomalies)}")
        except Exception as e:
            log(f"  roster: skipped ({e})")
    elif sen_paths:
        log("  roster: skipped — current academic year not supplied")

    hp_paths = of_role('housepoints')
    if hp_paths:
        hp = pd.concat([_read(p) for p in hp_paths], ignore_index=True).rename(columns={'Forename': 'Name'})
        if 'Event Date' in hp.columns:
            hp['Date'] = hp['Event Date']; hp['Event/Date'] = hp['Event Date']
        hp.to_csv(os.path.join(out_dir, 'House_Points.csv'), index=False)        # engine reads House_Points.csv
        summary['housepoints'] = len(hp)

    # GRADES — combine every attainment + effort file into one Reports.csv (engine keys by pupil number)
    attain = [p for (role, _), ps in buckets.items() if role == 'grade_attain' for p in ps]
    effort = [p for (role, _), ps in buckets.items() if role == 'grade_effort' for p in ps]
    if attain or effort:
        # Pupil -> intake, read from the roster we just derived, so each grade's term can be
        # resolved from its Resultset year group (see _parse_grades). No roster -> no map; rows
        # whose term can't be resolved are left blank for the engine to flag.
        pid_intake = {}
        _rpath = os.path.join(out_dir, 'Roster.csv')
        if os.path.exists(_rpath):
            try:
                _rdf = pd.read_csv(_rpath, dtype=str)
                for _, _rr in _rdf.iterrows():
                    _m = re.match(r'(\d+)', str(_rr.get('pid', '')))
                    if _m and pd.notna(_rr.get('Intake')) and str(_rr.get('Intake')).strip():
                        pid_intake[str(int(_m.group(1)))] = int(float(_rr['Intake']))
            except Exception as e:
                log(f"  grades: roster intake map unavailable ({e})")
        # Per-school term-label overrides (Admin-mapped). Built-ins handle the standard wordings;
        # this catches anything bespoke a school uses.
        term_map = _load_term_map(upload_dir)
        rep, gsum = _parse_grades(attain, effort, pid_intake, fallback_term=grade_term, term_map=term_map)
        rep.to_csv(os.path.join(out_dir, 'Reports.csv'), index=False)
        summary['report_rows'] = len(rep)
        # Surface any term labels we couldn't place, so the engine can pass them to the Admin
        # "unknown term" mapper. Distinct, sorted; an empty list clears a previously-flagged set.
        _unres = sorted(gsum.get('unresolved_labels') or [])
        try:
            with open(os.path.join(out_dir, 'unresolved_terms.json'), 'w', encoding='utf-8') as _uf:
                json.dump(_unres, _uf)
        except Exception as e:
            log(f"  grades: could not write unresolved-terms list ({e})")
        if gsum.get('unresolved_term'):
            log(f"  grades: {gsum['unresolved_term']} rows with unresolvable term "
                f"({len(_unres)} distinct label(s) for Admin to map)")

    for k, v in summary.items():
        log(f"  {k}: {v}")
    return summary


if __name__ == '__main__':
    up = sys.argv[1] if len(sys.argv) > 1 else '/mnt/user-data/uploads'
    out = sys.argv[2] if len(sys.argv) > 2 else '/home/claude/import_input'
    cay = int(sys.argv[3]) if len(sys.argv) > 3 else None
    print(f"Staging raw uploads from {up} -> {out}" + (f" (academic year start {cay})" if cay else ""))
    stage(up, out, current_acad_year=cay)
    print("Done.")
