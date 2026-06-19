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
import os, re, sys
import pandas as pd

ATT_COLS = ['Name', 'Reg', 'Mark', 'Date', 'Subject', 'Teacher', 'Period Description']
BEH_COLS = ['Name', 'Date', 'Subject', 'Lesson - Period', 'Incident', 'Teacher']

# Grade resultset -> engine Term label (AY labelled by its START year). Both current cohorts sit
# in AY 2025-26, so a "Year 10/11 Summer" resultset is its T3. Configurable for future imports.
DEFAULT_GRADE_TERM = 'T3 2025'


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
    d = df.rename(columns={'Pupil name': 'Name', 'Teacher Name': 'Teacher'})
    if 'Teacher' not in d.columns:
        d['Teacher'] = ''
    for c in BEH_COLS:
        if c not in d.columns:
            d[c] = ''
    return d[BEH_COLS]


def _parse_grades(attain_paths, effort_paths, term):
    def pidnum(n):
        m = re.match(r'(\d+)', str(n));  return str(int(m.group(1))) if m else None
    def parse(paths, rx, valcol):
        rows = []
        for p in paths:
            for _, r in _read(p).iterrows():
                bd = str(r.get('Basic details', '')).strip()
                m = re.match(rx, bd)
                if not m:
                    continue
                rows.append({'pid': pidnum(r.get('Name')), 'Name': r.get('Name'),
                             'Subject': m.group(1).strip(), valcol: r.get('Result')})
        return pd.DataFrame(rows, columns=['pid', 'Name', 'Subject', valcol])
    ot = parse(attain_paths, r'(?:On track for|Predicted|Target)\s+(.*)$', 'Ability Value')
    ef = parse(effort_paths, r'(.*)\s+Effort$', 'Effort Value')
    if ot.empty and ef.empty:
        return pd.DataFrame(columns=['Name', 'Subject', 'Term', 'Ability Value', 'Effort Value'])
    m = pd.merge(ot[['pid', 'Name', 'Subject', 'Ability Value']],
                 ef[['pid', 'Subject', 'Effort Value']], on=['pid', 'Subject'], how='outer')
    nm = dict(zip(ef['pid'], ef['Name'])) if not ef.empty else {}
    m['Name'] = m.apply(lambda r: r['Name'] if isinstance(r['Name'], str) else nm.get(r['pid'], r['pid']), axis=1)
    m['Term'] = term
    return m[['Name', 'Subject', 'Term', 'Ability Value', 'Effort Value']]


def stage(upload_dir, out_dir, grade_term=DEFAULT_GRADE_TERM, current_acad_year=None, verbose=True):
    os.makedirs(out_dir, exist_ok=True)
    buckets = {}
    for fn in sorted(os.listdir(upload_dir)):
        if not fn.lower().endswith(('.csv', '.xlsx')):
            continue
        role, yg = detect(fn)
        if role:
            buckets.setdefault((role, yg), []).append(os.path.join(upload_dir, fn))
    def files(role, yg):
        return buckets.get((role, yg), [])
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    summary = {}

    for yg, att_name in [(10, 'Year_10_attend_updated.csv'), (11, 'Year_11_attend_updated.csv')]:
        paths = files('attendance', yg)
        if not paths:
            continue
        att = pd.concat([_att_norm(_read(p)) for p in paths], ignore_index=True)
        att.to_csv(os.path.join(out_dir, att_name), index=False)
        summary[f'attendance_y{yg}'] = len(att)

    for yg, name in [(10, 'Behave_codes.csv'), (11, 'Behave_data.csv')]:
        parts = [_beh_norm(_read(p), False) for p in files('behave_current', yg)] + \
                [_beh_norm(_read(p), True) for p in files('behave_historic', yg)]
        if parts:
            beh = pd.concat(parts, ignore_index=True)
            beh.to_csv(os.path.join(out_dir, name), index=False)
            summary[f'behaviour_y{yg}'] = len(beh)

    for yg, name in [(10, 'Detentions.csv'), (11, 'Detention_data.csv')]:
        paths = files('detention', yg)
        if paths:
            det = pd.concat([_read(p) for p in paths], ignore_index=True)[['Name', 'Detention Date', 'Detention Type']]
            det.to_csv(os.path.join(out_dir, name), index=False)
            summary[f'detentions_y{yg}'] = len(det)

    # FSM — one combined file covers every cohort (engine reads FSM.csv once against the full registry)
    fsm_paths = [p for (role, _), ps in buckets.items() if role == 'fsm' for p in ps]
    if fsm_paths:
        fsm = pd.concat([_read(p) for p in fsm_paths], ignore_index=True)
        fsm.to_csv(os.path.join(out_dir, 'FSM.csv'), index=False)
        summary['fsm'] = len(fsm)

    # SEN — Y10 as .xlsx, Y11 as .csv (the engine's two SEN inputs)
    for p in files('sen', 10):
        sen = _read(p)
        sen['SEN Status'] = sen.get('SEN Status Code', sen.get('SEN Status'))
        keep = [c for c in ['Name', 'SEN Status Code', 'SEN Status'] if c in sen.columns]
        sen[keep].to_excel(os.path.join(out_dir, 'SEN.xlsx'), index=False)
        summary['sen_y10'] = len(sen)
    for p in files('sen', 11):
        sen = _read(p).rename(columns={'SEN Status Code': 'SEN Status'})
        sen[['Name', 'SEN Status']].to_csv(os.path.join(out_dir, 'SEN.csv'), index=False)
        summary['sen_y11'] = len(sen)

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

    for yg, name in [(10, 'House_Points_Y10.csv'), (11, 'House_Points_Y11.csv')]:
        paths = files('housepoints', yg)
        if paths:
            hp = pd.concat([_read(p) for p in paths], ignore_index=True).rename(columns={'Forename': 'Name'})
            if 'Event Date' in hp.columns:
                hp['Date'] = hp['Event Date']; hp['Event/Date'] = hp['Event Date']
            hp.to_csv(os.path.join(out_dir, name), index=False)
            summary[f'housepoints_y{yg}'] = len(hp)

    # GRADES — combine every attainment + effort file into one Reports.csv (engine keys by pupil number)
    attain = [p for (role, _), ps in buckets.items() if role == 'grade_attain' for p in ps]
    effort = [p for (role, _), ps in buckets.items() if role == 'grade_effort' for p in ps]
    if attain or effort:
        rep = _parse_grades(attain, effort, grade_term)
        rep.to_csv(os.path.join(out_dir, 'Reports.csv'), index=False)
        summary['report_rows'] = len(rep)

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
