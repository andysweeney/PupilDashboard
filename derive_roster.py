#!/usr/bin/env python3
"""derive_roster.py — derive the authoritative pupil roster from the SEN-with-Gender export.

The roster is the FIRST thing the portal needs: it attaches every pupil to a cohort, so the
engine can stop guessing cohorts from hardcoded streams and inconsistent leaving tags. We read
each pupil's CURRENT year group from the file's `Year` column (not the leaving tag in the name),
then turn that into a permanent intake year using the one fact the file can't supply — the
current academic year, which the Admin confirms once:

    intake = current_academic_year - (current_year_group - 7)

No "Year 11 is the end of their career" assumption: only the universal one-year-per-grade rule.

Output: a roster keyed by pupil number, carrying name, intake, current year group, gender and SEN
status. Adding a new cohort (Y7, Y12, anything) needs nothing here — it's just more rows.
"""
import csv, os, re

YEAR_RX = re.compile(r'(\d{1,2})')


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
        for r in _read(path):
            r = {(k.strip() if k else k): v for k, v in r.items()}
            name = r.get('Name')
            pid = pid_of(name)
            yg = yg_of(r.get('Year'))
            if pid is None:
                anomalies.append(f"{os.path.basename(path)}: no pupil number in {name!r}")
                continue
            if yg is None:
                anomalies.append(f"{os.path.basename(path)}: no year group for pupil {pid}")
                continue
            intake = current_acad_year - (yg - 7)
            sen = (r.get('SEN Status Code') or r.get('SEN Status') or '').strip()
            gender = (r.get('Gender') or '').strip()
            if pid in roster and roster[pid]['intake'] != intake:
                anomalies.append(f"pupil {pid} appears in two year groups "
                                 f"({roster[pid]['current_yg']} and {yg})")
            roster[pid] = {'pid': pid, 'name': str(name).strip(), 'intake': intake,
                           'current_yg': yg, 'gender': gender, 'sen': sen}
    # summary
    by_intake, by_gender = {}, {}
    for p in roster.values():
        by_intake[p['intake']] = by_intake.get(p['intake'], 0) + 1
        by_gender[p['gender'] or '?'] = by_gender.get(p['gender'] or '?', 0) + 1
    summary = {'pupils': len(roster), 'by_intake': dict(sorted(by_intake.items())),
               'by_gender': by_gender,
               'year_groups': dict(sorted({p['current_yg']: by_intake.get(p['intake'])
                                           for p in roster.values()}.items()))}
    return roster, summary, anomalies


def write_roster_csv(roster, out_path):
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['pid', 'Name', 'Intake', 'CurrentYG', 'Gender', 'SEN Status'])
        for p in sorted(roster.values(), key=lambda x: x['pid']):
            w.writerow([p['pid'], p['name'], p['intake'], p['current_yg'], p['gender'], p['sen']])


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
