#!/usr/bin/env python3
"""push_outputs.py — write the engine's outputs back to this school's storage prefix.

  /home/claude/data_real.json -> {school}/data.json   (what the dashboard loads)
  /home/claude/flags.json     -> {school}/flags.json  (unmapped subjects/codes for the Admin panel)

Upsert (x-upsert: true) so a rebuild overwrites the previous data.json. Uses the
service-role key (bypasses RLS). Stdlib only.
"""
import argparse, json, os, sys, time, urllib.request, urllib.error

BUCKET = os.environ.get("DATA_BUCKET", "dashboard-data")


def _env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"push_outputs: missing env {name}")
    return v.rstrip("/") if name == "SUPABASE_URL" else v


def upload(base, src_path, object_path, tries=3):
    key = _env("SUPABASE_SERVICE_ROLE_KEY")
    with open(src_path, "rb") as f:
        blob = f.read()
    hdr = {"apikey": key, "Authorization": f"Bearer {key}",
           "Content-Type": "application/json", "x-upsert": "true"}
    url = f"{base}/storage/v1/object/{BUCKET}/{object_path}"
    last = None
    for attempt in range(tries):
        try:
            r = urllib.request.Request(url, data=blob, headers=hdr, method="POST")
            with urllib.request.urlopen(r, timeout=120) as resp:
                resp.read()
                print(f"  pushed {object_path} ({len(blob)} bytes)")
                return
        except urllib.error.HTTPError as e:
            last = f"{e.code} {e.reason}: {e.read().decode(errors='replace')[:300]}"
        except urllib.error.URLError as e:
            last = str(e)
        time.sleep(1.5 * (attempt + 1))
    sys.exit(f"push_outputs: upload {object_path} failed after {tries} tries -> {last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--school", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--flags", required=False)
    args = ap.parse_args()
    base = _env("SUPABASE_URL")

    if not os.path.exists(args.data):
        sys.exit(f"push_outputs: engine output missing: {args.data}")
    # sanity: must be valid JSON before we publish it as the live data file
    with open(args.data, "rb") as f:
        json.loads(f.read())

    upload(base, args.data, f"{args.school}/data.json")
    if args.flags and os.path.exists(args.flags):
        upload(base, args.flags, f"{args.school}/flags.json")
    print("push_outputs: done")


if __name__ == "__main__":
    main()
