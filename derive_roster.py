#!/usr/bin/env python3
"""derive_roster.py — derive the authoritative pupil roster from the SEN-with-Gender export.

The roster is the FIRST thing the portal needs: it attaches every pupil to a cohort, so the
engine can stop guessing cohorts from hardcoded streams and inconsistent leaving tags. Each
per-cohort roster file is named sen_<intake>.csv, where <intake> is the cohort's permanent
intake year — the Admin confirms it once when adding the cohort, and it is authoritative for
every pupil in that file. The file therefore needs no Year column at all.

A pupil's current year group is then a pure function of that intake and the one school-wide
fact the Admin confirms once — the current academic year:

    current_year_group = current_academic_year - intake + 7

(A legacy file whose name carries no intake falls back to reading a Year column.) Everything
downstream — attendance, behaviour, grades — joins to the roster on the pupil number and
inherits its cohort from there; no other file derives or carries a year group.

Output: a roster keyed by pupil number, carrying name, intake, current year group, gender and SEN
status. Adding a new cohort (Y7, Y12, anything) needs nothing here — it's just another file.
"""
import csv, os, re

YEAR_RX = re.compile(r'(\d{1,2})')
INTAKE_RX = re.compile(r'sen_(\d{4})\.csv$', re.I)


def intake_from_path(path):
    """A per-cohort roster file is named sen_<intake>.csv. That intake is the Admin's
    confirmed assertion for the whole file and is authoritative — the file needs no Year
    column. Returns the intake year, or None if the name doesn't carry one."""
    m = INTAKE_RX.search(os.path.basename(str(path or '')))
    return int(m.group(1)) if m else None


def pid_of(name):
    m = re.match(r'\s*(\d+)', str(name or ''))
    return int(m.group(1)) if m else None


def yg_of(year_field):
    """'Year 10' / 'Yr10' / '10' -> 10."""
    m = YEAR_RX.search(str(year_field or ''))
    return int(m.group(1)) if m else None


def _read(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def derive_roster(sen_paths, current_acad_year):
    """current_acad_year = the START year of the current AY (2025 for 2025-26).
    Returns (roster_dict, summary, anomalies)."""
    roster = {}
    anomalies = []
    for path in sen_paths:
        file_intake = intake_from_path(path)        # the Admin's confirmed cohort, if named so
        for r in _read(path):
            r = {(k.strip() if k else k): v for k, v in r.items()}
            name = r.get('Name')
            pid = pid_of(name)
            if pid is None:
                anomalies.append(f"{os.path.basename(path)}: no pupil number in {name!r}")
                continue
            if file_intake is not None:
                # intake asserted by the Admin — the whole file is this cohort. No Year needed.
                intake = file_intake
                yg = current_acad_year - intake + 7          # current year group, for display only
            else:
                # legacy file: fall back to reading the current year group from the Year column
                yg = yg_of(r.get('Year'))
                if yg is None:
                    anomalies.append(f"{os.path.basename(path)}: no intake in filename and "
                                     f"no year group for pupil {pid}")
                    continue
                intake = current_acad_year - (yg - 7)
            sen = (r.get('SEN Status Code') or r.get('SEN Status') or '').strip()
            gender = (r.get('Gender') or '').strip()
            # FSM is a pupil attribute that rides in on the roster alongside SEN and gender.
            # Optional: a roster with no FSM column simply yields no FSM flags (never an error).
            _fsm_raw = (r.get('FSM') or r.get('FSM Status') or r.get('Free School Meals') or '').strip()
            fsm = 'Y' if _fsm_raw and _fsm_raw[0].upper() in ('Y', 'E', '1', 'T') else ''
            if pid in roster and roster[pid]['intake'] != intake:
                anomalies.append(f"pupil {pid} listed in two cohorts "
                                 f"(intake {roster[pid]['intake']} and {intake})")
            roster[pid] = {'pid': pid, 'name': str(name).strip(), 'intake': intake,
                           'current_yg': yg, 'gender': gender, 'sen': sen, 'fsm': fsm}
    # summary
    by_intake, by_gender = {}, {}
    fsm_count = 0
    for p in roster.values():
        by_intake[p['intake']] = by_intake.get(p['intake'], 0) + 1
        by_gender[p['gender'] or '?'] = by_gender.get(p['gender'] or '?', 0) + 1
        if p.get('fsm') == 'Y':
            fsm_count += 1
    summary = {'pupils': len(roster), 'by_intake': dict(sorted(by_intake.items())),
               'by_gender': by_gender, 'fsm': fsm_count,
               'year_groups': dict(sorted({p['current_yg']: by_intake.get(p['intake'])
                                           for p in roster.values()}.items()))}
    return roster, summary, anomalies


def write_roster_csv(roster, out_path):
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['pid', 'Name', 'Intake', 'CurrentYG', 'Gender', 'SEN Status', 'FSM'])
        for p in sorted(roster.values(), key=lambda x: x['pid']):
            w.writerow([p['pid'], p['name'], p['intake'], p['current_yg'],
                        p['gender'], p['sen'], p.get('fsm', '')])


if __name__ == '__main__':
    import sys, glob
    up = sys.argv[1] if len(sys.argv) > 1 else '/mnt/user-data/uploads'
    cay = int(sys.argv[2]) if len(sys.argv) > 2 else 2025
    sen = sorted(p for p in glob.glob(os.path.join(up, '*.csv')) if 'sen' in os.path.basename(p).lower())
    print(f"SEN files: {[os.path.basename(p) for p in sen]}  | current AY start: {cay}")
    roster, summary, anomalies = derive_roster(sen, cay)
    print("summary:", summary)
    if anomalies:
        print("anomalies:", anomalies[:10])
    write_roster_csv(roster, os.path.join(os.path.dirname(__file__), 'Roster.csv'))
    print("wrote Roster.csv")
