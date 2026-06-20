#!/usr/bin/env python3
"""Import real school data (attendance, codes, detentions, FSM, SEN) into dashboard data.json format.

Enrichment steps (added May 2026):
- Y-codes (school closure) excluded from absence counting → not_counted category
- suppressedAbsences: mixed present+absent slots → present wins
- slotDenominators: per day×period counts, <10 non-Y marks → exclude slot
- slotTeachers: teacher session counts per pupil per slot
- schoolDayCounts: actual school days per day of week
- attendanceMarks: mark codes stored per date per pupil
- attAbsSubj includes mark codes for each absence record
- SEN/incident code configs embedded in data.json
- Duplicate registration detection and reporting
"""

import pandas as pd
import json, re, os, glob, csv
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from functools import lru_cache

# ── CONFIGURATION ──

# Attendance code classification
PRESENT_CODES = {'/', 'L', 'K', 'V', 'P', 'W', 'B', 'D'}
AUTH_ABSENT_CODES = {'C', 'C1', 'C2', 'E', 'I', 'J', 'J1', 'M', 'Q', 'R', 'S', 'T', 'X'}
UNAUTH_ABSENT_CODES = {'O', 'G', 'U', 'N', '-', '?'}
NOT_COUNTED_CODES = {'Y', 'Y1', 'Y2', 'Y3', 'Y4', 'Y5', 'Y6', 'Y7'}
ALL_ABSENT_CODES = AUTH_ABSENT_CODES | UNAUTH_ABSENT_CODES

# SEN code definitions
SEN_CODES = {
    'K': 'SEN Support',
    'E': 'EHCP',
    'Add': 'Additional Help',
    'add': 'Additional Help',
    'Strat': 'Strategies Applied',
    'strat': 'Strategies Applied',
    'N': 'SEN Monitoring',
}

# Incident category mapping
INCIDENT_CATEGORIES = {
    'Home learning/prep not completed': 'Behaviour',
    'Behaviour poor at break, lunch, or lesson changeover': 'Behaviour',
    'Behaviour poor in lesson': 'Behaviour',
    'Mobile phone confiscated (collect at 3.05pm from annexe)': 'Behaviour',
    'Effort poor in lesson': 'Behaviour',
    'Chewing gum': 'Behaviour',
    'Refusal to follow the on call/senior teacher instructions': 'Behaviour',
    'Behaviour disruptive in lesson (Senior Leader on-call used)': 'Behaviour',
    'Behaviour disruptive in lesson (Department on-call used)': 'Behaviour',
    'Protective measure': 'Behaviour',
    'Detention not attended': 'Behaviour',
    'Late to lesson': 'Attendance',
    'Toilet visited during lesson': 'Attendance',
    'Truancy from lesson (disruption to the normal working day of the school)': 'Attendance',
    'Medical room visited': 'Attendance',
    'One to one tutor review not attended': 'Attendance',
    'Attendance': 'Attendance',
    'Socks contrary to school policy': 'Uniform',
    'Earrings contrary to school policy': 'Uniform',
    'Blazer not worn': 'Uniform',
    'Nail varnish worn': 'Uniform',
    'Skirt contrary to school policy (cannot see waist band)': 'Uniform',
    'Shirt untucked': 'Uniform',
    'Bracelets worn contrary to school policy': 'Uniform',
    'Shoes contrary to school policy': 'Uniform',
    'Makeup excessive': 'Uniform',
    'Nose piercing worn': 'Uniform',
    'Jumper not worn': 'Uniform',
    'Tie not worn or worn properly': 'Uniform',
    'Uniform other': 'Uniform',
    'Exercise book missing': 'Equipment',
    'PE kit items missing': 'Equipment',
    'Reading book/material missing': 'Equipment',
    'Handbook missing': 'Equipment',
    'Calculator missing': 'Equipment',
    'Pen missing': 'Equipment',
    'Subject specific equipment': 'Equipment',
    'Ingredients missing': 'Equipment',
}

# ── REPORTS / GRADES (progress scores) ──
# The dashboard stores, per pupil × term × subject:  sc[subject] = [abilityLetter, effort, OTE]
#   abilityLetter : 7-point B/D/W/M/C/S/E   (Below -> Excellent)
#   effort        : 1..4                     (1 = Excellent .. 4 = Low; INVERTED vs ability)
#   OTE           : optional GCSE target 1..9 (from "OTA Grade")
#
# !!! CONFIRM AGAINST THE REAL Reports EXPORT BEFORE TRUSTING THESE !!!
# The maps below default to the dashboard's NATIVE scale (letters pass straight through,
# effort 1..4 passes straight through) plus the obvious word forms. If the SIMS export uses
# a school-specific scale (e.g. a 1-7 ability number, words like 'Secure'/'Emerging', or an
# effort scale where 1 = Low instead of 1 = Excellent), add those raw values here. Any raw
# value NOT found is reported on the console (⚠ unmapped ability/effort values) and skipped —
# nothing is silently coerced. Look at the flagged values, then extend these two dicts.
ABILITY_VALUE_MAP = {
    'B': 'B', 'D': 'D', 'W': 'W', 'M': 'M', 'C': 'C', 'S': 'S', 'E': 'E',
    'Below': 'B', 'Developing': 'D', 'Working': 'W', 'Meeting': 'M',
    'Confident': 'C', 'Skilful': 'S', 'Skillful': 'S', 'Excellent': 'E',
}
# effort raw value -> 1..4 (1 = best/Excellent ... 4 = Low). CONFIRM the direction!
EFFORT_VALUE_MAP = {
    '1': 1, '2': 2, '3': 3, '4': 4,
    'Excellent': 1, 'Good': 2, 'Developing': 3, 'Low': 4,
}

# Current academic year
CAY = 2025

# Years from Y7 start to the end of Y11 — used to derive intake from the leaving cohort.
YEARS_TO_GCSE = 5

# Subject-name normalisation (raw spelling -> canonical). Populated from Admin export.
SUBJECT_MAP = {}

# House Points (positive behaviour). Only these achievement types are kept; index == the
# stored type code. Each carries a weight (Admin-editable, like the sanction weights).
HP_TYPES = ['House Point', 'Positive on call (SLT)']
HOUSE_POINT_WEIGHTS = {'House Point': 1, 'Positive on call (SLT)': 5}

# ── SCHOOL KEY: source ALL school-specific config from the school's own key ──
# Phase 1 of the multi-school refactor. The literal constants above are now only a fallback
# default template; when a key is present it is the single source of truth (see
# school_data_key.schema.json). The dashboard Admin panel edits this key and saves it back per
# school; this engine just consumes whatever key it is handed.
from school_key import load_key, dump_flags
KEY_PATH = os.environ.get('SCHOOL_KEY_PATH', '/home/claude/school_001_key.json')
_KEY_ABILITY_MAP = None
if os.path.exists(KEY_PATH):
    print(f"Loading school key from {KEY_PATH}...")
    KEY = load_key(KEY_PATH)
    _ac = KEY['attendanceCodes']
    PRESENT_CODES        = set(_ac['present'])
    AUTH_ABSENT_CODES    = set(_ac['authorisedAbsent'])
    UNAUTH_ABSENT_CODES  = set(_ac['unauthorisedAbsent'])
    NOT_COUNTED_CODES    = set(_ac['notCounted'])
    ALL_ABSENT_CODES     = AUTH_ABSENT_CODES | UNAUTH_ABSENT_CODES
    SEN_CODES            = dict(KEY['senCodes'])
    INCIDENT_CATEGORIES  = dict(KEY['incidentCategories'])
    SUBJECT_MAP          = dict(KEY['subjects']['aliases'])
    ABILITY_VALUE_MAP    = dict(KEY['scales']['abilityValueMap'])
    EFFORT_VALUE_MAP     = {str(k): v for k, v in KEY['scales']['effortValueMap'].items()}
    HP_TYPES             = list(KEY['housePoints']['types'])
    HOUSE_POINT_WEIGHTS  = dict(KEY['housePoints']['weights'])
    CAY                  = int(KEY['meta']['academicYearStart'])
    YEARS_TO_GCSE        = int(KEY['meta']['yearsToGCSE'])
    _KEY_ABILITY_MAP     = dict(KEY['scales']['attainment']['labels'])
    print("  School key loaded.")
else:
    KEY = None
    print("No school key found, using built-in defaults.")

# ── HELPERS ──

@lru_cache(maxsize=None)
def pid(name):
    """Numeric pupil ID from a name string, WITHOUT leading zeros.
    The dashboard keys the registry by p.index (an int) and looks pupils up via
    parseInt(...) / String(intId), so admission numbers like '017961' must be
    normalised to '17961' or every per-pupil lookup misses."""
    m = re.match(r'(\d+)', str(name))
    return str(int(m.group(1))) if m else str(name)

def cohort_of(name):
    """Leaving cohort from '... (2027 cohort)' or '... (2027 leaver)' -> 2027, or None."""
    m = re.search(r'\((\d{4})\s*(?:cohort|leaver)\)', str(name))
    return int(m.group(1)) if m else None

def intake_from_name(name, default_intake):
    """Y7 intake year derived from the leaving cohort (cohort - 5), else the default."""
    c = cohort_of(name)
    return (c - YEARS_TO_GCSE) if c is not None else default_intake

def strip_tg(reg):
    """Tutor group without the tutor-initials suffix: '10W2-ELe' -> '10W2'."""
    return str(reg).split('-')[0].strip()

def norm_subject(s):
    """Canonicalise a subject spelling via SUBJECT_MAP (trimmed identity if unmapped)."""
    if pd.isna(s):
        return s
    s = str(s).strip()
    return SUBJECT_MAP.get(s, s)

@lru_cache(maxsize=None)
def parse_date_flex(date_str):
    """Parse dates in various formats (incl. dd-Mon-yy like '04-Sep-25')."""
    if pd.isna(date_str):
        return None
    s = str(date_str).strip()
    for fmt in ['%d-%b-%y', '%d-%b-%Y', '%d/%m/%Y', '%d/%m/%y',
                '%d %B %Y', '%Y-%m-%d', '%d-%m-%Y', '%d-%m-%y']:
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None

def get_monday(date_str):
    d = datetime.strptime(date_str, '%Y-%m-%d')
    return (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')

def _norm_raw(v):
    """Normalise a raw report cell to a lookup key: strip, drop a trailing '.0'
    (so a float-read '1.0' matches '1'), and Title-case bare words."""
    if pd.isna(v):
        return None
    s = str(v).strip()
    if not s or s.lower() == 'nan':
        return None
    if re.fullmatch(r'\d+\.0', s):      # 1.0 -> 1 (pandas reads numeric cols as float)
        s = s[:-2]
    return s

def map_ability(v, flagset):
    """Raw 'Ability Value' -> dashboard letter, or None (flagging the unknown raw value)."""
    s = _norm_raw(v)
    if s is None:
        return None
    if s in ABILITY_VALUE_MAP:
        return ABILITY_VALUE_MAP[s]
    if s.title() in ABILITY_VALUE_MAP:
        return ABILITY_VALUE_MAP[s.title()]
    if s.upper() in ABILITY_VALUE_MAP:
        return ABILITY_VALUE_MAP[s.upper()]
    flagset.add(s)
    return None

def map_effort(v, flagset):
    """Raw 'Effort Value' -> 1..4, or None (flagging the unknown raw value)."""
    s = _norm_raw(v)
    if s is None:
        return None
    if s in EFFORT_VALUE_MAP:
        return EFFORT_VALUE_MAP[s]
    if s.title() in EFFORT_VALUE_MAP:
        return EFFORT_VALUE_MAP[s.title()]
    flagset.add(s)
    return None

def map_ote(v):
    """Raw 'OTA Grade' -> GCSE int 1..9, or None. Tolerates '7', '7.0', '7a', 'Grade 7'."""
    s = _norm_raw(v)
    if s is None:
        return None
    m = re.search(r'\d+', s)
    if not m:
        return None
    n = int(m.group())
    return n if 1 <= n <= 9 else None

DAY_MAP = {'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 'Fri': 4}
DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']

# ── TERM / PERIOD RESOLUTION (year-aware) ──
# An academic year is labelled by its START calendar year: AY 2025-26 -> "2025".
# Term split (month-based default): T1 = Sep-Dec, T2 = Jan-Mar, T3 = Apr-Aug.
# NOTE: the Apr (Easter) boundary floats year-to-year; month-rule is the default,
# refine to attendance-gap detection or an explicit term-date table if needed.

@lru_cache(maxsize=None)
def acad_year(date_str):
    """Academic year (start calendar year) for an ISO (YYYY-MM-DD) date."""
    y, m = int(date_str[:4]), int(date_str[5:7])
    return y if m >= 9 else y - 1

@lru_cache(maxsize=None)
def get_term(date_str):
    """Period label 'T{n} {AYstart}' for an ISO date, or None. Year-aware."""
    if not date_str:
        return None
    m = int(date_str[5:7])
    ay = acad_year(date_str)
    if m >= 9:      t = 1   # Sep-Dec  -> T1
    elif m <= 3:    t = 2   # Jan-Mar  -> T2
    else:           t = 3   # Apr-Aug  -> T3
    return f"T{t} {ay}"

def get_periods(intake, cay):
    """Period labels for a cohort: intake (Y7 start year) .. cay, capped at Y11."""
    out = []
    for ay in range(intake, cay + 1):
        if (ay - intake + 7) > 11:   # past Year 11
            break
        for t in (1, 2, 3):
            out.append(f"T{t} {ay}")
    return out

def term_sort_key(label):
    """Chronological sort key for a 'T{n} {ay}' label."""
    t, ay = label.split(' ')
    return (int(ay), int(t[1:]))

def period_label(p):
    """Display label: 'T1 2025' -> 'T1 2025-2026'."""
    t, ay = p.split(' ')
    return f"{t} {ay}-{int(ay) + 1}"

# ── LOAD DATA ──
print("Loading data files...")
UP = '/home/claude/import_input'   # staged inputs (concatenated / renamed as needed)

def _req_csv(name, **kw):
    df = pd.read_csv(f'{UP}/{name}', encoding='utf-8-sig', **kw)
    df.columns = df.columns.str.strip()
    return df

def _opt_csv(name, cols):
    """Read a CSV if present, else return an empty frame with the expected columns.
    Lets a partial (e.g. Y10-only) import run before every file has been uploaded."""
    p = f'{UP}/{name}'
    if os.path.exists(p):
        df = pd.read_csv(p, encoding='utf-8-sig')
        df.columns = df.columns.str.strip()
        return df
    print(f"  (optional) {name} not found — using empty frame")
    return pd.DataFrame(columns=cols)

# ── COHORT-GENERIC INPUT READS ──
# Attendance, behaviour and detentions are read by glob, so ANY number of cohorts works — each
# cohort just contributes more files (the staging contract names them *attend_updated.csv /
# Behave*.csv / Detention*.csv). FSM is one combined file; SEN keeps its two sources. A pupil's
# cohort/intake comes from the roster, never from these files.
def _read_glob(pattern, rename=None):
    frames = []
    for p in sorted(glob.glob(f'{UP}/{pattern}')):
        df = pd.read_csv(p, encoding='utf-8-sig'); df.columns = df.columns.str.strip()
        if rename: df = df.rename(columns=rename)
        frames.append(df)
    return frames

att_frames = _read_glob('*attend_updated.csv')
if not att_frames:
    raise SystemExit("No attendance files (*attend_updated.csv) found in staging.")
codes_list = _read_glob('Behave*.csv',
                        rename={'Teacher Name': 'Teacher', 'Lesson - Period': 'Period', 'Pupil name': 'Name'})
dets_list = _read_glob('Detention*.csv')
fsm_y10 = _req_csv('FSM.csv'); fsm_y11 = fsm_y10
sen_y10 = (pd.read_excel(f'{UP}/SEN.xlsx') if os.path.exists(f'{UP}/SEN.xlsx')
           else pd.DataFrame(columns=['Name', 'SEN Status Code']))
sen_y11 = _opt_csv('SEN.csv', ['Name', 'SEN Status'])

# House Points (positive behaviour) — OPTIONAL. Filenames TBC; skipped if not present.
# Any CSV matching these is read; the processing is filename-agnostic.
HP_PATHS = [f'{UP}/House_Points_Y10.csv',
            f'{UP}/House_Points_Y11.csv',
            f'{UP}/House_Points.csv',
            f'{UP}/HousePoints.csv']
hp_dfs = []
for _p in HP_PATHS:
    if os.path.exists(_p):
        _df = pd.read_csv(_p, encoding='utf-8-sig')
        _df.columns = _df.columns.str.strip()
        hp_dfs.append(_df)
print(f"House-point files found: {len(hp_dfs)}")

# Reports / grades (progress scores) — OPTIONAL, multi-file per cohort supported.
# Stage as Reports.csv (the importer also picks up Reports_Y10/Y11 and Report_data and
# concatenates). Matching is by leading admission number, so cohort tags don't matter.
REPORT_PATHS = [f'{UP}/Reports.csv', f'{UP}/Reports_Y10.csv', f'{UP}/Reports_Y11.csv',
                f'{UP}/Report_data.csv']
_report_dfs = []
for _p in REPORT_PATHS:
    if os.path.exists(_p):
        _df = pd.read_csv(_p, encoding='utf-8-sig', dtype=str)
        _df.columns = _df.columns.str.strip()
        _report_dfs.append(_df)
reports = (pd.concat(_report_dfs, ignore_index=True) if _report_dfs
           else pd.DataFrame(columns=['Name', 'Date', 'Subject',
                                      'Ability Value', 'Effort Value', 'OTA Grade']))
print(f"Report files found: {len(_report_dfs)} ({len(reports)} grade rows)")

# ── COMBINE & PARSE ATTENDANCE ──
print("\nParsing attendance data...")
att_all = pd.concat(att_frames, ignore_index=True)
att_all = att_all.dropna(subset=['Period Description'])
att_all['pid'] = att_all['Name'].apply(pid)
att_all['Day'] = att_all['Period Description'].str.split(':').str[0]
att_all['Per'] = att_all['Period Description'].apply(
    lambda x: int(str(x).split(':')[1]) if ':' in str(x) and str(x).split(':')[1].isdigit() else 0)
att_all['DateISO'] = att_all['Date'].apply(parse_date_flex)
att_all['Teacher'] = att_all['Teacher'].apply(lambda x: int(x) if pd.notna(x) else None)
att_all = att_all.dropna(subset=['DateISO'])
# Academic year (start calendar year) per row — used to build per-year snapshots.
att_all['AY'] = att_all['DateISO'].apply(acad_year)

# Subject normalisation + flag case/spelling variants (e.g. 'Pe' vs 'PE') for mapping.
_raw_subjects = set(att_all['Subject'].dropna().astype(str).str.strip())
_variant_groups = {}
for _s in _raw_subjects:
    _variant_groups.setdefault(_s.lower(), set()).add(_s)
_subject_variants = {k: v for k, v in _variant_groups.items() if len(v) > 1}
if _subject_variants:
    print("⚠ SUBJECT SPELLING VARIANTS (add a canonical form to subject_map):")
    for _k, _v in sorted(_subject_variants.items()):
        print(f"  {sorted(_v)}")
att_all['Subject'] = att_all['Subject'].apply(norm_subject)

# Flag unknown attendance codes
all_marks = set(att_all['Mark'].dropna().unique())
known_marks = PRESENT_CODES | ALL_ABSENT_CODES | NOT_COUNTED_CODES
unknown_marks = all_marks - known_marks
if unknown_marks:
    print(f"⚠ UNKNOWN ATTENDANCE CODES: {unknown_marks}")
    for m in unknown_marks:
        count = len(att_all[att_all['Mark'] == m])
        print(f"  '{m}': {count} occurrences — defaulting to unauthorised absent")
    UNAUTH_ABSENT_CODES |= unknown_marks
    ALL_ABSENT_CODES = AUTH_ABSENT_CODES | UNAUTH_ABSENT_CODES

# ── BUILD REGISTRY (roster-driven) ──
# The roster is authoritative for each pupil's cohort: intake comes from the pupil's CURRENT
# year group (in the roster) anchored to the academic year — no "Y11 is the end" assumption, and
# any number of cohorts works. Where no roster row exists we fall back to the leaving-cohort tag
# in the Name. Year-group is derived from intake; TG has its year prefix stripped.
print("Building pupil registry...")

# ── PUPIL LEDGER ── persistent last-known characteristics for every pupil ever seen
# on a roster, so leavers keep their name/gender/cohort/SEN/FSM after they drop off.
# The current roster always wins; the ledger only fills pupils it no longer contains,
# so it can never alter a current pupil.
LEDGER_PATH = os.environ.get('LEDGER_PATH', '/home/claude/pupil_ledger.csv')

def _load_ledger(path):
    out = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, newline='', encoding='utf-8-sig') as _f:
            for _row in csv.DictReader(_f):
                _p = (_row.get('Pupil') or '').strip()
                if not _p:
                    continue
                _iv = (_row.get('Intake') or '').strip()
                try: _ik = int(_iv) if _iv else None
                except (ValueError, TypeError): _ik = None
                _g = (_row.get('Gender') or 'U').strip().upper()[:1]
                out[_p] = {'name': (_row.get('Name') or '').strip(),
                           'gender': _g if _g in ('M', 'F') else 'U',
                           'intake': _ik,
                           'sen': (_row.get('SEN') or '').strip(),
                           'fsm': (_row.get('FSM') or 'N').strip().upper()[:1]}
    except Exception as _e:
        print(f"Ledger: could not read {path} ({_e}); treating as empty")
    return out

def _write_ledger(path, ledger):
    try:
        with open(path, 'w', newline='', encoding='utf-8') as _f:
            _w = csv.writer(_f)
            _w.writerow(['Pupil', 'Name', 'Gender', 'Intake', 'SEN', 'FSM'])
            for _p in sorted(ledger):
                _L = ledger[_p]
                _w.writerow([_p, _L.get('name', ''), _L.get('gender', 'U'),
                             ('' if _L.get('intake') is None else int(_L['intake'])),
                             _L.get('sen', ''), _L.get('fsm', 'N')])
        print(f"Ledger: wrote {len(ledger)} pupils -> {path}")
    except Exception as _e:
        print(f"Ledger: could not write {path} ({_e})")

# Roster: pid -> {intake, gender, name}. Produced by the staging step (derive_roster) as Roster.csv.
roster_map = {}
_roster_path = f'{UP}/Roster.csv'
if os.path.exists(_roster_path):
    _rdf = pd.read_csv(_roster_path, encoding='utf-8-sig'); _rdf.columns = _rdf.columns.str.strip()
    for _, _r in _rdf.iterrows():
        _p = pid(_r['Name']) if pd.notna(_r.get('Name')) else None
        if _p is None: continue
        try: _ik = int(_r['Intake'])
        except (ValueError, TypeError, KeyError): _ik = None
        _g = str(_r.get('Gender', '')).strip().upper()[:1]
        roster_map[_p] = {'intake': _ik, 'gender': _g if _g in ('M', 'F') else 'U',
                          'name': str(_r.get('Name', '')).strip()}
    print(f"Roster: {len(roster_map)} pupils (authoritative cohort source)")
else:
    print("Roster: none found — falling back to cohort tags in pupil names")

# Merge the ledger: current roster wins; the ledger supplies only pupils no longer on it.
_ledger = _load_ledger(LEDGER_PATH)
_CURRENT_ROSTER = set(roster_map.keys())
# Pupils with actual data still in the store (attendance is the universal existence seed).
# A ledger pupil whose data has been deleted drops out of this set, which gates both the
# leaver-restore below and the ledger prune at the end — so wiping a cohort forgets them
# instead of re-injecting them as empty 'ghost' records.
_att_pids = set(att_all['pid'].dropna().astype(str).unique())
_merged = 0
for _p, _L in _ledger.items():
    if _p not in roster_map and _L.get('intake') is not None and _p in _att_pids:
        roster_map[_p] = {'intake': _L['intake'], 'gender': _L.get('gender', 'U'),
                          'name': _L.get('name') or str(_p)}
        _merged += 1
print(f"Ledger: {len(_ledger)} known pupils, {_merged} leavers restored to roster")

# Gender — authoritative from the roster, with any SEN 'Gender' column as a fallback.
gender_map = {p: r['gender'] for p, r in roster_map.items() if r.get('gender') in ('M', 'F')}
for _df in (sen_y10, sen_y11):
    if 'Gender' in _df.columns:
        for _, _r in _df.iterrows():
            g = str(_r.get('Gender', '')).strip().upper()[:1]
            if g in ('M', 'F'): gender_map.setdefault(pid(_r['Name']), g)
print(f"Gender: {sum(1 for g in gender_map.values() if g=='M')} M, "
      f"{sum(1 for g in gender_map.values() if g=='F')} F")

registry = {}
DEFAULT_INTAKE = min((r['intake'] for r in roster_map.values() if r.get('intake') is not None),
                     default=CAY - YEARS_TO_GCSE + 1)
for _, row in att_all.drop_duplicates('Name').iterrows():
    p = pid(row['Name'])
    r = roster_map.get(p)
    intake = (r['intake'] if r and r.get('intake') is not None
              else intake_from_name(row['Name'], DEFAULT_INTAKE))
    yg = CAY - intake + 7
    registry[p] = {'index': int(p), 'id': (r['name'] if r and r.get('name') else row['Name']), 'intake': intake, 'left': False,
                   'gender': gender_map.get(p, 'U'), 'year': f'Year {yg}', 'reg': strip_tg(row['Reg'])}
# Roster pupils with no attendance yet still belong in the registry.
for p, r in roster_map.items():
    if p not in registry and r.get('intake') is not None:
        yg = CAY - r['intake'] + 7
        registry[p] = {'index': int(p), 'id': r.get('name') or str(p), 'intake': r['intake'],
                       'left': False, 'gender': gender_map.get(p, 'U'), 'year': f'Year {yg}', 'reg': ''}
print(f"Registry: {len(registry)} pupils "
      f"({sum(1 for v in registry.values() if v['gender']=='U')} with unknown gender)")

# The roster is authoritative for who is currently on roll. If a roster has been provided,
# anyone it no longer lists — but who is still known to us (has data, or was on roll before)
# — is marked as having Left. Re-uploading a cohort's roster therefore flips departed pupils
# to Left automatically, while they keep their history.
if _CURRENT_ROSTER:
    _left_n = 0
    for _p in registry:
        _is_left = _p not in _CURRENT_ROSTER
        registry[_p]['left'] = _is_left
        if _is_left:
            _left_n += 1
    print(f"On roll: {len(registry) - _left_n}; left (kept with history): {_left_n}")

# ── FSM & SEN FLAGS ──
print("Processing FSM and SEN flags...")
fsm_set = set()
for _, row in fsm_y10.iterrows():
    p = pid(row['Name'])
    if p in registry and row.get('Eligible for free meals') == 'T':
        fsm_set.add(p)

# Leaver FSM: keep last-known FSM for pupils no longer on the roster. Current pupils
# already reflect the latest list above (including coming OFF free meals), so this
# only ever adds leavers — it cannot change a current pupil's status.
for _p, _L in _ledger.items():
    if _p in registry and _p not in _CURRENT_ROSTER and _L.get('fsm') == 'Y':
        fsm_set.add(_p)

sen_map = {}
for df_sen, col_pref in [(sen_y10, 'SEN Status Code'), (sen_y11, 'SEN Status')]:
    col = col_pref if col_pref in df_sen.columns else ('SEN Status Code' if 'SEN Status Code' in df_sen.columns else 'SEN Status')
    for _, row in df_sen.iterrows():
        p = pid(row['Name'])
        status = row.get(col)
        if pd.notna(status) and str(status).strip():
            sen_map[p] = str(status).strip()

# Leaver SEN: keep last-known SEN status for pupils no longer on the roster.
for _p, _L in _ledger.items():
    if _p not in _CURRENT_ROSTER and _p in _att_pids and _L.get('sen') and _p not in sen_map:
        sen_map[_p] = _L['sen']

ehcp_set = {p for p, s in sen_map.items() if s == 'E'}
send_set = set(sen_map.keys())
print(f"FSM: {len(fsm_set)}, SEND: {len(send_set)}, EHCP: {len(ehcp_set)}")

# Refresh the ledger from this run's current pupils (leaver rows are left untouched),
# then persist it so the next rebuild can restore anyone who has since left.
for _p in _CURRENT_ROSTER:
    _r = roster_map.get(_p)
    if not _r:
        continue
    _ledger[_p] = {'name': _r.get('name') or str(_p), 'gender': _r.get('gender', 'U'),
                   'intake': _r.get('intake'), 'sen': sen_map.get(_p, ''),
                   'fsm': 'Y' if _p in fsm_set else 'N'}
# Prune anyone the store no longer holds data for and who isn't on the current roll, so a
# deleted cohort's identity rows don't linger (and can't be re-injected next rebuild).
_pruned = [_p for _p in list(_ledger) if _p not in _CURRENT_ROSTER and _p not in _att_pids]
for _p in _pruned:
    del _ledger[_p]
if _pruned:
    print(f"Ledger: pruned {len(_pruned)} pupils with no remaining data in the store")
_write_ledger(LEDGER_PATH, _ledger)

# ── BUILD TIMETABLES (per academic year) ──
# A pupil's timetable differs each year, so timetables are nested by AY (start year):
#   tt_out[ay_str][px] = 5x5 grid. Split cells ([primary,per,secondary,changeover])
#   capture a mid-year subject change within that one year.
print("Building per-year timetables...")
# Vectorised build (equivalent to the former per-slot groupby loop, validated cell-for-cell
# incl. split-subject cells on the real data). One grouped pass computes the earliest date per
# (AY, pupil, day, period, subject); cells are then assembled from that. Within a slot, subjects
# are ordered by (earliest-date, subject-name) — the same stable/alphabetical tie-break the old
# code had — so primary = earliest-starting subject, secondary = latest, changeover = its start.
tt_out = {}                       # ay_str -> px -> 5x5 grid
_all_subject_set = set()
tt_src = att_all[att_all['Per'].between(1, 5)]
# Create an entry for every (AY, pupil) that has any period-1..5 row (matches old pid iteration).
for ay, ay_grp in tt_src.groupby('AY', sort=False):
    tt_out[str(int(ay))] = {p: [[None]*5 for _ in range(5)] for p in ay_grp['pid'].unique()}
# Earliest DateISO per (AY, pupil, valid-day, period, subject), then ordered for tie-breaking.
_tt_valid = tt_src[tt_src['Day'].isin(DAY_MAP)]
_tt_min = (_tt_valid.groupby(['AY', 'pid', 'Day', 'Per', 'Subject'], sort=False)['DateISO']
                    .min().reset_index())
_all_subject_set.update(_tt_min['Subject'].unique().tolist())
_tt_min = _tt_min.sort_values(['AY', 'pid', 'Day', 'Per', 'DateISO', 'Subject'], kind='stable')
for (ay, p, day, per), slot in _tt_min.groupby(['AY', 'pid', 'Day', 'Per'], sort=False):
    if per < 1 or per > 5:
        continue
    grid = tt_out[str(int(ay))][p]
    di = DAY_MAP[day]
    subs = slot['Subject'].tolist()      # already ordered by (earliest date, subject name)
    if len(subs) == 1:
        grid[di][per-1] = [subs[0], per]
    else:
        primary, secondary = subs[0], subs[-1]
        changeover = slot['DateISO'].iloc[-1]
        if primary != secondary:
            grid[di][per-1] = [primary, per, secondary, changeover]
        else:
            grid[di][per-1] = [primary, per]
print(f"Timetables: {sum(len(v) for v in tt_out.values())} pupil-years across {len(tt_out)} academic year(s)")

# ── ATTAINMENT SCALE RESOLUTION (year-group aware; Phase-1 scale migration) ──
# A pupil's attainment is read through the scale their year group uses (KS3 letters, GCSE 1-9, ...),
# declared in the key, and stored AS THE RAW TOKEN. The dashboard derives the rank (1..n, 1=best) and
# the axis labels from that scale, so a GCSE grade shows as a GCSE grade and a KS3 letter as a letter.
# Cross-scale longitudinal lines use the key's transitions at chart time, not a storage-time rewrite.
_SCALE_DEFS   = (KEY or {}).get('scales', {}).get('definitions', {})
_ATTAIN_BY_YG = {int(k): v for k, v in (KEY or {}).get('scales', {}).get('attainmentByYearGroup', {}).items()}
_REF_SCALE    = (KEY or {}).get('scales', {}).get('referenceScale')
_TRANSITIONS  = (KEY or {}).get('transitions', {})
def _scale_token_map(scale_id):
    out = {}
    for lvl in _SCALE_DEFS.get(scale_id, {}).get('levels', []):
        for t in lvl.get('raw', []):
            out[str(t)] = str(t); out[str(t).upper()] = str(t)
    return out
_SCALE_TOKENS = {sid: _scale_token_map(sid) for sid in _SCALE_DEFS}

def map_attainment_scaled(raw, year_group, flagset):
    """Validated raw attainment token, read through the scale the year group uses. Stored AS-IS so the
    dashboard derives rank (1..n, 1=best) and labels from that scale — a GCSE grade stays a GCSE grade,
    a KS3 letter stays a letter. A token not valid for the year group's scale is flagged."""
    s = _norm_raw(raw)
    if s is None:
        return None
    scale_id = _ATTAIN_BY_YG.get(year_group)
    if scale_id is None:
        flagset.add(s); return None
    canon = _SCALE_TOKENS.get(scale_id, {}).get(s) or _SCALE_TOKENS.get(scale_id, {}).get(s.upper())
    if canon is None:
        flagset.add(s); return None              # token not valid for this year group's scale
    return canon

# ── PARSE REPORTS (grades) -> per pupil × term × subject score lookup ──
# report_scores[pid][period_label][subject] = [abilityLetter, effort, OTE]
print("Parsing reports (grades)...")
report_scores = defaultdict(lambda: defaultdict(dict))
_rep_subject_set = set()
_unmapped_ability, _unmapped_effort = set(), set()
_rep_rows_used = _rep_no_subject = _rep_no_term = _rep_no_pupil = _rep_empty = 0

if len(reports):
    _has_subject = 'Subject' in reports.columns
    if not _has_subject:
        print("⚠ REPORTS: no 'Subject' column found — scores are per-subject, so nothing "
              "can be attached. Add/Confirm the subject column name and re-run.")
    _has_date = 'Date' in reports.columns
    _has_term = 'Term' in reports.columns
    for _, row in reports.iterrows():
        p = pid(row['Name']) if 'Name' in reports.columns and pd.notna(row.get('Name')) else None
        if not p or p not in registry:
            _rep_no_pupil += 1
            continue
        # Subject (required to key the score)
        subj = norm_subject(row['Subject']) if _has_subject else None
        if not subj or pd.isna(subj):
            _rep_no_subject += 1
            continue
        # Term: prefer a parseable Date; else an explicit 'T{n} {yyyy}' Term value.
        term = None
        if _has_date:
            term = get_term(parse_date_flex(row.get('Date')))
        if term is None and _has_term:
            tv = _norm_raw(row.get('Term'))
            if tv and re.fullmatch(r'T[123]\s+\d{4}', tv):
                term = tv
        if term is None:
            _rep_no_term += 1
            continue
        if _SCALE_DEFS:
            _ay = int(term.split()[1])
            _yg = _ay - registry[p]['intake'] + 7
            ability = map_attainment_scaled(row.get('Ability Value'), _yg, _unmapped_ability)
        else:
            ability = map_ability(row.get('Ability Value'), _unmapped_ability)
        effort = map_effort(row.get('Effort Value'), _unmapped_effort)
        ote = map_ote(row.get('OTA Grade')) if 'OTA Grade' in reports.columns else None
        if ability is None and effort is None and ote is None:
            _rep_empty += 1
            continue
        report_scores[p][term][subj] = [ability, effort, ote]
        _rep_subject_set.add(subj)
        _rep_rows_used += 1

# Fold report subjects into the global subject set so they get a compact subject key.
_all_subject_set.update(_rep_subject_set)
print(f"Reports: {_rep_rows_used} scores attached "
      f"({len(report_scores)} pupils, {len(_rep_subject_set)} subjects)")
if _rep_no_pupil or _rep_no_subject or _rep_no_term or _rep_empty:
    print(f"  skipped — no/unknown pupil: {_rep_no_pupil}, no subject: {_rep_no_subject}, "
          f"undated/no term: {_rep_no_term}, all-blank: {_rep_empty}")
if _unmapped_ability:
    print(f"⚠ UNMAPPED ABILITY VALUES ({len(_unmapped_ability)}) — add to ABILITY_VALUE_MAP: "
          f"{sorted(_unmapped_ability)}")
if _unmapped_effort:
    print(f"⚠ UNMAPPED EFFORT VALUES ({len(_unmapped_effort)}) — add to EFFORT_VALUE_MAP: "
          f"{sorted(_unmapped_effort)}")

# ── COLLECT ALL SUBJECTS ──
all_subjects = sorted(_all_subject_set)
print(f"Subjects: {len(all_subjects)}")

# ── BUILD ATTENDANCE DATA ──
print("Processing attendance...")

# Filter to real marks (exclude Y-codes for absence counting)
real_att = att_all[~att_all['Mark'].isin(NOT_COUNTED_CODES)]

# School weeks (Monday dates) — count actual lessons per week
week_lessons = {}
for date_str in sorted(real_att['DateISO'].unique()):
    mon = get_monday(date_str)
    # Count unique periods on this date with real marks (>=10 marks means slot existed)
    day_marks = real_att[real_att['DateISO'] == date_str]
    real_periods = 0
    for per in range(1, 6):
        per_marks = day_marks[day_marks['Per'] == per]
        if len(per_marks) >= 10:  # <10 marks = slot didn't really exist
            real_periods += 1
    week_lessons[mon] = week_lessons.get(mon, 0) + real_periods

# Per-pupil absence dates
attendance = {}
attendance_marks = {}
for pupil, group in real_att.groupby('pid'):
    absent_rows = group[group['Mark'].isin(ALL_ABSENT_CODES)]
    attendance[pupil] = sorted(set(absent_rows['DateISO'].tolist()))
    # Store marks per date for auth/unauth classification
    marks_by_date = {}
    for _, row in absent_rows.iterrows():
        d = row['DateISO']
        if d not in marks_by_date:
            marks_by_date[d] = []
        marks_by_date[d].append(row['Mark'])
    attendance_marks[pupil] = marks_by_date

# Per-pupil per-subject absences WITH MARK CODES
att_abs_subj = {}
for pupil, group in real_att.groupby('pid'):
    absent_rows = group[group['Mark'].isin(ALL_ABSENT_CODES)]
    records = []
    for _, row in absent_rows.iterrows():
        records.append([row['DateISO'], row['Subject'], int(row['Per']), row['Mark']])
    att_abs_subj[pupil] = records

# Per-period attendance by subject (year-aware terms via get_term above)

att_by_period_subj = {}
for pupil, group in real_att.groupby('pid'):
    periods = {}
    group_c = group.copy()
    group_c['Term'] = group_c['DateISO'].apply(get_term)
    for term, tgroup in group_c.groupby('Term'):
        if not term: continue
        subj_att = {}
        for subj, sgroup in tgroup.groupby('Subject'):
            total = len(sgroup)
            present = len(sgroup[sgroup['Mark'].isin(PRESENT_CODES)])
            subj_att[subj] = round(present / total * 100) if total > 0 else 100
        periods[term] = subj_att
    att_by_period_subj[pupil] = periods

att_by_period = {}
for pupil, group in real_att.groupby('pid'):
    group_c = group.copy()
    group_c['Term'] = group_c['DateISO'].apply(get_term)
    periods = {}
    for term, tgroup in group_c.groupby('Term'):
        if not term: continue
        total = len(tgroup)
        present = len(tgroup[tgroup['Mark'].isin(PRESENT_CODES)])
        periods[term] = round(present / total * 100) if total > 0 else 100
    att_by_period[pupil] = periods

print(f"Attendance records: {len(attendance)} pupils")

# ── ENRICHMENT: slotDenominators ──
print("Computing per-year slot denominators...")
slot_denominators = {}            # ay_str -> 5x5 grid
valid_slot_dates = {}             # ay_str -> {(di, per): [DateISO, ...]} scheduled dates (>=10 marks)
for ay, ay_grp in real_att.groupby('AY'):
    ay_str = str(int(ay))
    grid = [[0]*5 for _ in range(5)]
    vmap = {}
    for dow_name in DAY_NAMES:
        di = DAY_MAP[dow_name]
        day_data = ay_grp[ay_grp['Day'] == dow_name]
        for per in range(1, 6):
            per_data = day_data[day_data['Per'] == per]
            dates_with_slot = per_data.groupby('DateISO').size()
            valid = dates_with_slot[dates_with_slot >= 10].index.tolist()
            grid[di][per-1] = len(valid)
            vmap[(di, per)] = sorted(valid)
    slot_denominators[ay_str] = grid
    valid_slot_dates[ay_str] = vmap
print(f"Slot denominators: {len(slot_denominators)} year(s)")

# ── ENRICHMENT: schoolDayCounts (per AY) ──
school_day_counts = {}            # ay_str -> {Mon: n, ...}
for ay, ay_grp in real_att.groupby('AY'):
    ay_str = str(int(ay))
    school_day_counts[ay_str] = {
        dow_name: ay_grp[ay_grp['Day'] == dow_name]['DateISO'].nunique()
        for dow_name in DAY_NAMES
    }
print(f"School day counts: {len(school_day_counts)} year(s)")

# ── ENRICHMENT: slotTeachers (per AY) ──
print("Computing per-year slot teachers...")
slot_teachers = {}                # ay_str -> px -> 5x5 grid of [[teacher, count], ...]
st_src = att_all[att_all['Per'].between(1, 5) & att_all['Teacher'].notna()]
for ay, ay_grp in st_src.groupby('AY'):
    ay_str = str(int(ay))
    slot_teachers[ay_str] = {}
    for px, grp in ay_grp.groupby('pid'):
        days = [[[] for _ in range(5)] for _ in range(5)]
        for (dow_name, per), subgrp in grp.groupby(['Day', 'Per']):
            di = DAY_MAP.get(dow_name)
            if di is None or per < 1 or per > 5:
                continue
            tc = subgrp.groupby('Teacher').size().to_dict()
            days[di][per-1] = sorted([[int(t), c] for t, c in tc.items()], key=lambda x: -x[1])
        slot_teachers[ay_str][px] = days
print(f"Slot teachers: {sum(len(v) for v in slot_teachers.values())} pupil-years")

# ── ENRICHMENT: splitSlotMeta (exact per-era teacher + denominator for changed slots) ──
# A split cell ([primary, per, secondary, changeover]) is a slot whose subject changed
# mid-year. These pupils/slots are exactly the edge cases most likely to be examined in
# depth, so we bake the prior/current teacher and prior/current scheduled-lesson count from
# the dated attendance itself — the dashboard then renders them from fact, not inference.
#   splitSlotMeta[ay_str][px]["di,pi"] = [priorTeacherId, curTeacherId, priorDenom, curDenom]
# Denominators partition the SAME cohort valid-date set used by slotDenominators, so
# priorDenom + curDenom always reconciles to slotDenominators[di][per-1].
print("Computing split-slot per-era metadata...")
INV_DAY = {di: name for name, di in DAY_MAP.items()}
_split_src = att_all[att_all['Per'].between(1, 5)
                     & att_all['Day'].isin(DAY_MAP)
                     & att_all['Teacher'].notna()].copy()
_split_src['Teacher'] = _split_src['Teacher'].astype(int)
_split_gb = {}
for (ay_v, pid_v, day_v, per_v), grp in _split_src.groupby(['AY', 'pid', 'Day', 'Per']):
    _split_gb[(int(ay_v), pid_v, day_v, int(per_v))] = grp
split_slot_meta = {}
_split_count = 0
for ay_str, pupils in tt_out.items():
    ay_int = int(ay_str)
    vmap = valid_slot_dates.get(ay_str, {})
    for px, grid in pupils.items():
        cell_meta = {}
        for di in range(5):
            for p in range(5):
                c = grid[di][p]
                if not c or len(c) != 4:
                    continue
                per = p + 1
                changeover = c[3]
                vdates = vmap.get((di, per), [])
                prior_den = sum(1 for dt in vdates if dt < changeover)
                cur_den = sum(1 for dt in vdates if dt >= changeover)
                pt = ct = None
                sub = _split_gb.get((ay_int, px, INV_DAY[di], per))
                if sub is not None:
                    prior_rows = sub[sub['DateISO'] < changeover]
                    cur_rows = sub[sub['DateISO'] >= changeover]
                    if len(prior_rows):
                        pt = int(prior_rows.groupby('Teacher').size().idxmax())
                    if len(cur_rows):
                        ct = int(cur_rows.groupby('Teacher').size().idxmax())
                cell_meta[f"{di},{p}"] = [pt, ct, prior_den, cur_den]
                _split_count += 1
        if cell_meta:
            split_slot_meta.setdefault(ay_str, {})[px] = cell_meta
print(f"Split-slot metadata: {_split_count} changed slots across "
      f"{sum(len(v) for v in split_slot_meta.values())} pupil-years")

# ── ENRICHMENT: suppressedAbsences ──
print("Detecting mixed present+absent slots...")
suppressed_absences = {}
for px, grp in att_all[att_all['Per'].between(1,5)].groupby('pid'):
    slots = grp.groupby(['DateISO', 'Per']).agg(marks=('Mark', list)).reset_index()
    px_suppressed = []
    for _, row in slots.iterrows():
        has_present = any(m in PRESENT_CODES for m in row['marks'])
        has_absent = any(m in ALL_ABSENT_CODES for m in row['marks'])
        if has_present and has_absent:
            px_suppressed.append(f"{row['DateISO']}|{row['Per']}")
    if px_suppressed:
        suppressed_absences[px] = px_suppressed

supp_count = sum(len(v) for v in suppressed_absences.values())
print(f"Suppressed absences: {len(suppressed_absences)} pupils, {supp_count} slots")

# ── ENRICHMENT: Duplicate detection ──
print("Detecting duplicate registrations...")
dup_pupils = 0
dup_slots = 0
for px, grp in att_all[att_all['Per'].between(1,5)].groupby('pid'):
    slots = grp.groupby(['DateISO', 'Per']).size()
    multi = slots[slots > 1]
    if len(multi):
        dup_pupils += 1
        dup_slots += len(multi)
print(f"Duplicate registrations: {dup_pupils} pupils, {dup_slots} duplicate slots")

# ── BUILD SANCTIONS ──
print("Processing sanctions...")
sanctions = []
for df_codes in codes_list:
    df_codes['pid'] = df_codes['Name'].apply(pid)
    df_codes['DateISO'] = df_codes['Date'].apply(parse_date_flex)
    per_col = 'Period' if 'Period' in df_codes.columns else 'Lesson - Period'
    inc_col = 'Incident' if 'Incident' in df_codes.columns else 'Incident'
    for _, row in df_codes.iterrows():
        p = row['pid']
        if p not in registry: continue
        date_iso = row.get('DateISO')
        if not date_iso: continue
        subj = row.get('Subject')
        subj = None if pd.isna(subj) else norm_subject(subj)
        period_num = None
        period_desc = row.get(per_col)
        if pd.notna(period_desc):
            parts = str(period_desc).split(':')
            if len(parts) == 2:
                try: period_num = int(parts[1])
                except ValueError: pass
        incident = row.get(inc_col, '')
        if pd.isna(incident): incident = ''
        category = INCIDENT_CATEGORIES.get(incident, 'Other')
        sanctions.append([int(p), 0, date_iso, subj, period_num, incident, category])

# Process detentions
for df_dets in dets_list:
    df_dets['pid'] = df_dets['Name'].apply(pid)
    date_col = 'Detention Date' if 'Detention Date' in df_dets.columns else 'Date'
    for _, row in df_dets.iterrows():
        p = row['pid']
        if p not in registry: continue
        date_iso = parse_date_flex(row.get(date_col))
        if not date_iso: continue
        det_type = row.get('Detention Type', '')
        if pd.isna(det_type): det_type = 'Detention'
        sanctions.append([int(p), 2, date_iso, None, None, str(det_type), 'Detention'])

sanctions.sort(key=lambda x: (x[2], x[0]))
print(f"Sanctions: {len(sanctions)} ({sum(1 for s in sanctions if s[1]==0)} codes, {sum(1 for s in sanctions if s[1]==2)} detentions)")

# Flag unmapped incidents
unmapped = set(s[5] for s in sanctions if s[1] == 0) - set(INCIDENT_CATEGORIES.keys())
unmapped.discard('')
if unmapped:
    print(f"⚠ UNMAPPED INCIDENT TYPES ({len(unmapped)}):")
    for i in sorted(unmapped): print(f"  - {i}")

# ── BUILD HOUSE POINTS (positive behaviour) ──
# Keep only HP_TYPES. Day+period come from 'Lesson - Period'; the SUBJECT is derived from
# the per-year timetable for that slot (the file's 'Lesson - Subject'/'Lesson - Class' are
# ignored on purpose — they were found inconsistent with the derived timetable).
print("Processing house points...")

def _hp_day_period(raw):
    """'Mon:3' -> ('Mon',3); 'Monday AM' -> ('Mon',None); blanks -> (None,None)."""
    s = str(raw).strip()
    if not s or s.lower() == 'nan':
        return None, None
    if ':' in s:
        d, _, p = s.partition(':')
        day3 = d.strip()[:3].title()
        try:
            return day3, int(p.strip())
        except ValueError:
            return day3, None
    return s[:3].title(), None

house_points = []
_hp_unknown_types = set()
for df_hp in hp_dfs:
    for _, row in df_hp.iterrows():
        atype = str(row.get('Achievement Type', '')).strip()
        if atype not in HP_TYPES:
            if atype:
                _hp_unknown_types.add(atype)
            continue
        p = pid(row['Name'])
        if p not in registry:
            continue
        date_iso = parse_date_flex(row.get('Event/Date') or row.get('Event Date') or row.get('Date'))
        if not date_iso:
            continue
        day3, period = _hp_day_period(row.get('Lesson - Period'))
        # Subject derived from the pupil's timetable for that AY/slot (None if unavailable).
        subj = None
        if period and day3 in DAY_MAP:
            grid = tt_out.get(str(acad_year(date_iso)), {}).get(p)
            if grid:
                cell = grid[DAY_MAP[day3]][period - 1]
                if cell:
                    subj = cell[0]
        house_points.append([int(p), HP_TYPES.index(atype), date_iso, subj, period])

house_points.sort(key=lambda x: (x[2], x[0]))
print(f"House points: {len(house_points)} "
      f"({sum(1 for h in house_points if h[1]==0)} HP, {sum(1 for h in house_points if h[1]==1)} +on-call)")
if _hp_unknown_types:
    print(f"  (ignored achievement types: {sorted(_hp_unknown_types)})")

# ── BUILD PROGRESS ──
print("Building progress structure...")
INTAKES = sorted({info['intake'] for info in registry.values()})
print(f"Active intakes (from registry): {INTAKES}")

# Dynamic period list: union of each cohort's full grid (intake .. CAY, capped Y11)
# plus any term actually produced from real attendance dates (safety net).
period_set = set()
for intake in INTAKES:
    period_set.update(get_periods(intake, CAY))
for _pid, _periods in att_by_period.items():
    period_set.update(k for k in _periods.keys() if k)
periods_list = sorted(period_set, key=term_sort_key)
period_labels = [period_label(p) for p in periods_list]
print(f"Periods ({len(periods_list)}): {periods_list}")

progress = {}
for intake in INTAKES:
    ik = f"I{intake}"
    progress[ik] = {}
    intake_pupils = [p for p, info in registry.items() if info['intake'] == intake]
    for period in get_periods(intake, CAY):
        rows = []
        for p in intake_pupils:
            rows.append([int(p), registry[p]['id'], registry[p]['reg'],
                         dict(report_scores.get(p, {}).get(period, {})),
                         p in send_set, p in ehcp_set, p in fsm_set])
        progress[ik][period] = rows

# ── BUILD ENROLMENTS ──
print("Building enrolments...")
# Teacher roster + index (built here; the compress section reuses it). cls.tc stores the index.
_teacher_ids = sorted({int(t) for t in att_all['Teacher'].dropna().unique()})
teacher_index = ['T' + str(t) for t in _teacher_ids] or ['Unknown']
_teacher_pos = {tid: i for i, tid in enumerate(_teacher_ids)}

# A CLASS is a real teaching group: pupils sharing SUBJECT + TEACHER + timetable SLOTS in the
# CURRENT year. Derived from CAY attendance: for each (subject, teacher, day, period) take the
# on-roll roster, then merge a class's repeated weekly periods (same teacher, overlapping roster)
# into one class. Each pupil is assigned to the class where they have the most sessions (robust to
# cover lessons / stray marks). Class numbers run per (cohort, subject). Team-taught or blocked
# subjects (PE, etc.) legitimately yield large rosters — expected, not an error.
CAY_ROSTER_MIN = 5        # ignore tiny slot rosters (<5 pupils) — stray marks, not a class
MERGE_JACCARD  = 0.5      # same-teacher slot rosters merge into one class at this roster overlap

_cay = att_all[(att_all['AY'] == CAY) & att_all['Per'].between(1, 5) & att_all['Teacher'].notna()].copy()
_cay['Teacher'] = _cay['Teacher'].astype(int)
_sess = _cay.groupby(['Subject', 'pid', 'Teacher', 'Day', 'Per']).size().reset_index(name='n')

_slot_roster = defaultdict(set)        # (subject, teacher, day, per) -> {pids on roll}
_sess_by = {}                          # (subject, pid, teacher, day, per) -> session count
for r in _sess.itertuples(index=False):
    _slot_roster[(r.Subject, r.Teacher, r.Day, r.Per)].add(r.pid)
    _sess_by[(r.Subject, r.pid, r.Teacher, r.Day, r.Per)] = r.n

def _jacc(a, b):
    u = len(a | b)
    return (len(a & b) / u) if u else 0.0

# Seed clusters from the largest slots first, then a consolidation pass for order-independence.
subj_classes = defaultdict(list)       # subject -> [ {teacher, roster:set, slots:set(day,per)} ]
for (subj, tch, day, per), roster in sorted(_slot_roster.items(), key=lambda kv: -len(kv[1])):
    if len(roster) < CAY_ROSTER_MIN:
        continue
    for cl in subj_classes[subj]:
        if cl['teacher'] == tch and _jacc(cl['roster'], roster) >= MERGE_JACCARD:
            cl['roster'] |= roster
            cl['slots'].add((day, per))
            break
    else:
        subj_classes[subj].append({'teacher': tch, 'roster': set(roster), 'slots': {(day, per)}})
for clusters in subj_classes.values():
    again = True
    while again:
        again = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if clusters[i]['teacher'] == clusters[j]['teacher'] and \
                   _jacc(clusters[i]['roster'], clusters[j]['roster']) >= MERGE_JACCARD:
                    clusters[i]['roster'] |= clusters[j]['roster']
                    clusters[i]['slots']  |= clusters[j]['slots']
                    del clusters[j]; again = True; break
            if again:
                break

# Assign each (pupil, subject) to the class where they have the most sessions.
class_assign = defaultdict(dict)       # pid -> {subject: (cluster_index, teacher_id)}
for subj, clusters in subj_classes.items():
    pid_best = {}
    for ci, cl in enumerate(clusters):
        tch = cl['teacher']
        for q in cl['roster']:
            s = sum(_sess_by.get((subj, q, tch, d, p), 0) for (d, p) in cl['slots'])
            prev = pid_best.get(q)
            if prev is None or s > prev[0] or (s == prev[0] and ci < prev[1]):
                pid_best[q] = (s, ci, tch)
    for q, (s, ci, tch) in pid_best.items():
        class_assign[q][subj] = (ci, tch)

# Number classes per (intake, subject): clusters are cohort-pure (one teacher can't teach two
# cohorts in one slot). Order by descending size then teacher id for a stable 1..N numbering.
class_number = {}                      # (subject, cluster_index) -> class_num
_by_is = defaultdict(list)
for subj, clusters in subj_classes.items():
    for ci, cl in enumerate(clusters):
        any_pid = next(iter(cl['roster']))
        intk = registry[any_pid]['intake'] if any_pid in registry else None
        if intk is not None:
            _by_is[(intk, subj)].append((ci, len(cl['roster']), cl['teacher']))
for (intk, subj), lst in _by_is.items():
    lst.sort(key=lambda t: (-t[1], t[2]))
    for num, (ci, _sz, _tch) in enumerate(lst, start=1):
        class_number[(subj, ci)] = num

enrolments = {}
for intake in INTAKES:
    ik = f"I{intake}"
    recs = []
    intake_pupils = [p for p, info in registry.items() if info['intake'] == intake]
    for p in intake_pupils:
        recs.append({'t': 'tg', 'x': int(p), 'v': registry[p]['reg'],
                     'f': f'{CAY}-09-01', 'u': None})
        for subj in sorted(class_assign.get(p, {})):
            ci, tch = class_assign[p][subj]
            cnum = class_number.get((subj, ci))
            if cnum is None:
                continue
            recs.append({'t': 'subj', 'x': int(p), 's': subj,
                         'f': f'{CAY}-09-01', 'u': None})
            recs.append({'t': 'cls', 'x': int(p), 's': subj, 'c': cnum,
                         'tc': _teacher_pos[tch],      # index into teacherIndex
                         'f': f'{CAY}-09-01', 'u': None})
    enrolments[ik] = recs
print(f"Enrolments: {sum(len(v) for v in enrolments.values())} records")
print(f"Classes (subject+teacher+slot): {len(class_number)} across {len(_by_is)} cohort-subjects")

# ── COMPRESS & OUTPUT ──
print("Compressing and writing data.json...")

sk = {s: f"s{i}" for i, s in enumerate(all_subjects)}
pk = {p: f"p{i}" for i, p in enumerate(periods_list)}
ABILITY_MAP = _KEY_ABILITY_MAP if _KEY_ABILITY_MAP else {'B': 'Below', 'D': 'Developing', 'W': 'Working', 'M': 'Meeting',
               'C': 'Confident', 'S': 'Skilful', 'E': 'Excellent'}

# Date index
all_date_set = set()
for s in sanctions: all_date_set.add(s[2])
for h in house_points: all_date_set.add(h[2])
for p_id, dates in attendance.items():
    for d in dates: all_date_set.add(d)
for p_id, recs in att_abs_subj.items():
    for r in recs: all_date_set.add(r[0])
all_date_set.add(f'{CAY}-09-01')
all_dates_sorted = sorted(all_date_set)
date_idx = {d: i for i, d in enumerate(all_dates_sorted)}

# Compress progress
c_prog = {}
for ik, periods in progress.items():
    c_prog[ik] = {}
    for per, rows in periods.items():
        c_prog[ik][pk.get(per, per)] = [
            [r[0], r[2], {sk.get(s, s): v for s, v in r[3].items()}, r[4], r[5], r[6]]
            for r in rows
        ]

# Compress enrolments
c_enr = {}
for ik, recs in enrolments.items():
    c_enr[ik] = []
    for r in recs:
        if r['t'] == 'cls':
            c_enr[ik].append([0, r['x'], sk.get(r['s'], r['s']), r['c'], r['tc'],
                              date_idx.get(r['f'], 0),
                              None if r['u'] is None else date_idx.get(r['u'], 0)])
        elif r['t'] == 'subj':
            c_enr[ik].append([1, r['x'], sk.get(r['s'], r['s']), date_idx.get(r['f'], 0)])
        elif r['t'] == 'tg':
            c_enr[ik].append([2, r['x'], r['v'], date_idx.get(r['f'], 0),
                              None if r['u'] is None else date_idx.get(r['u'], 0)])

# Teacher roster (anon IDs) — built once in the enrolments section above; ready to merge a
# real teacher-name mapping file later (cls.tc stores the index into this list).
print(f"Teachers: {len(_teacher_ids)} anon IDs")

# Compress sanctions
c_sanc = []
sanction_details = []
for s in sanctions:
    subj_key = sk.get(s[3], s[3]) if s[3] else None
    c_sanc.append([s[0], s[1], date_idx.get(s[2], 0), subj_key, s[4]])
    sanction_details.append([s[5], s[6]])

# Compress house points (mirror of sanctions: [px, type, dateIdx, subjKey, period])
c_hp = []
hp_details = []
for h in house_points:
    subj_key = sk.get(h[3], h[3]) if h[3] else None
    c_hp.append([h[0], h[1], date_idx.get(h[2], 0), subj_key, h[4]])
    hp_details.append([HP_TYPES[h[1]]])

# Compress attendance
c_att = {}
for p_id, dates in attendance.items():
    c_att[p_id] = [date_idx[d] for d in dates if d in date_idx]

# Compress attendanceMarks
c_att_marks = {}
for p_id, marks_by_date in attendance_marks.items():
    c_att_marks[p_id] = {d: marks for d, marks in marks_by_date.items()}

# Compress attByPeriod / attByPeriodSubj
c_abp = {}
for p_id, periods in att_by_period.items():
    c_abp[p_id] = {pk.get(per, per): v for per, v in periods.items()}

c_abps = {}
for p_id, periods in att_by_period_subj.items():
    c_abps[p_id] = {}
    for per, subjs in periods.items():
        c_abps[p_id][pk.get(per, per)] = {sk.get(s, s): v for s, v in subjs.items()}

# Compress attAbsSubj (includes mark codes)
c_aas = {}
for p_id, recs in att_abs_subj.items():
    c_aas[p_id] = {}
    for r in recs:
        di = date_idx.get(r[0])
        if di is not None:
            if di not in c_aas[p_id]:
                c_aas[p_id][di] = []
            c_aas[p_id][di].append([sk.get(r[1], r[1]), r[2], r[3]])  # [subjKey, period, mark]

# Compress timetables (nested per AY: ay_str -> px -> grid)
def _compress_grid(grid):
    c_grid = []
    for day in grid:
        c_day = []
        for cell in day:
            if cell is None:
                c_day.append(None)
            elif len(cell) == 2:
                c_day.append([sk.get(cell[0], cell[0]), cell[1]])
            elif len(cell) == 4:
                c_day.append([sk.get(cell[0], cell[0]), cell[1],
                              sk.get(cell[2], cell[2]), cell[3]])
            else:
                c_day.append(None)
        c_grid.append(c_day)
    return c_grid

c_tt = {}
for ay_str, pupils in tt_out.items():
    c_tt[ay_str] = {p_id: _compress_grid(grid) for p_id, grid in pupils.items()}

# Attendance code config
att_code_config = {
    'present': sorted(PRESENT_CODES),
    'authorised_absent': sorted(AUTH_ABSENT_CODES),
    'unauthorised_absent': sorted(UNAUTH_ABSENT_CODES),
    'not_counted': sorted(NOT_COUNTED_CODES),
}

# Incident code config
incident_config = {}
for s in sanctions:
    if s[1] == 0 and s[5]:
        incident_config[s[5]] = s[6]

output = {
    "config": {
        "subjects": all_subjects,
        "ability_map": ABILITY_MAP,
        "periods": periods_list,
        "period_labels": period_labels,
        "intakes": sorted(INTAKES, reverse=True),
        "current_acad_year": CAY,
        "real_data": True,
        "att_codes": att_code_config,
        "sen_codes": SEN_CODES,
        "incident_codes": incident_config,
        "house_point_weights": HOUSE_POINT_WEIGHTS,
        "house_point_types": HP_TYPES,
    },
    "subjectKeys": sk,
    "periodKeys": pk,
    "dateIndex": all_dates_sorted,
    "teacherIndex": teacher_index,
    "registry": registry,
    "enrolments": c_enr,
    "progress": c_prog,
    "attendance": c_att,
    "attendanceMarks": c_att_marks,
    "attByPeriod": c_abp,
    "attByPeriodSubj": c_abps,
    "attAbsSubj": c_aas,
    "sanctions": c_sanc,
    "sanctionDetails": sanction_details,
    "housePoints": c_hp,
    "housePointDetails": hp_details,
    "senStatus": {p: s for p, s in sen_map.items()},
    "weekLessons": week_lessons,
    "timetables": c_tt,
    "slotDenominators": slot_denominators,
    "schoolDayCounts": school_day_counts,
    "slotTeachers": slot_teachers,
    "splitSlotMeta": split_slot_meta,
    "suppressedAbsences": suppressed_absences,
}

out_path = '/home/claude/data_real.json'
with open(out_path, 'w') as f:
    json.dump(output, f, separators=(',', ':'))

size_mb = os.path.getsize(out_path) / 1024 / 1024
print(f"\n{'='*50}")
print(f"data_real.json: {size_mb:.1f} MB")
print(f"Pupils: {len(registry)}")
print(f"Sanctions: {len(c_sanc)} ({sum(1 for s in c_sanc if s[1]==0)} codes, {sum(1 for s in c_sanc if s[1]==2)} detentions)")
print(f"Attendance: {sum(len(v) for v in c_att.values())} absence date entries")
print(f"AttAbsSubj: {sum(sum(len(v2) for v2 in v.values()) for v in c_aas.values())} absence period records")
print(f"Timetables: {sum(len(v) for v in c_tt.values())} pupil-years across {len(c_tt)} AY(s): {sorted(c_tt.keys())}")
print(f"Report scores: {_rep_rows_used} attached across {len(report_scores)} pupils")
print(f"Suppressed absences: {supp_count} slots across {len(suppressed_absences)} pupils")
print(f"Duplicate registrations: {dup_slots} slots across {dup_pupils} pupils")
if unknown_marks:
    print(f"⚠ Unknown marks defaulted to unauth absent: {unknown_marks}")
if unmapped:
    print(f"⚠ Unmapped incident types: {len(unmapped)}")
print(f"{'='*50}")
print("Done!")

# ── PHASE 1: emit flags.json for the Admin panel (reuses the trackers above) ──
def _flat(x):
    out = set()
    if isinstance(x, dict):
        for v in x.values():
            out |= set(v) if isinstance(v, (set, list, tuple)) else {v}
    elif isinstance(x, (set, list, tuple)):
        out |= set(x)
    elif x is not None:
        out.add(x)
    return out
_g = globals()
_flag_sources = {
    "unknown_subject":         _flat(_g.get("_subject_variants")),
    "unknown_attendance_code": _flat(_g.get("unknown_marks")),
    "unmapped_incident":       _flat(_g.get("unmapped")),
    "unmapped_ability_value":  _flat(_g.get("_unmapped_ability")),
    "unmapped_effort_value":   _flat(_g.get("_unmapped_effort")),
}
_flags_path = os.environ.get("FLAGS_PATH", "/home/claude/flags.json")
_emitted = dump_flags(_flags_path, _flag_sources)
print("\nFlags for Admin panel -> " + _flags_path + ": "
      + (", ".join(f"{f['type']}={len(f['values'])}" for f in _emitted) if _emitted else "none"))
