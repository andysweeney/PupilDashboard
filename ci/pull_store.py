#!/usr/bin/env python3
"""pull_store.py — download one school's raw store + key from Supabase Storage.

Runs inside the GitHub Actions rebuild job with the service-role key, which bypasses
RLS, so it can read any tenant's prefix. It pulls:

  {school}/uploads/raw/*   -> --raw-dest   (the accumulated raw exports staging reads)
  {school}/key.json        -> --key-dest   (the school's engine key, saved by the Admin panel)

Stdlib only (urllib) so the job needs no extra pip installs to fetch.
"""
import argparse, json, os, sys, time, urllib.request, urllib.error

BUCKET = os.environ.get("DATA_BUCKET", "dashboard-data")


def _env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"pull_store: missing env {name}")
    return v.rstrip("/") if name == "SUPABASE_URL" else v


def _req(url, method="GET", body=None, headers=None, expect_json=False, tries=3):
    key = _env("SUPABASE_SERVICE_ROLE_KEY")
    hdr = {"apikey": key, "Authorization": f"Bearer {key}"}
    if headers:
        hdr.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        hdr.setdefault("Content-Type", "application/json")
    last = None
    for attempt in range(tries):
        try:
            r = urllib.request.Request(url, data=data, headers=hdr, method=method)
            with urllib.request.urlopen(r, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if expect_json else raw
        except urllib.error.HTTPError as e:
            last = f"{e.code} {e.reason}: {e.read().decode(errors='replace')[:300]}"
        except urllib.error.URLError as e:
            last = str(e)
        time.sleep(1.5 * (attempt + 1))
    sys.exit(f"pull_store: {method} {url} failed after {tries} tries -> {last}")


def list_prefix(base, prefix):
    """List objects directly under a storage prefix (paginated)."""
    out, offset, page = [], 0, 100
    while True:
        body = {"prefix": prefix, "limit": page, "offset": offset,
                "sortBy": {"column": "name", "order": "asc"}}
        rows = _req(f"{base}/storage/v1/object/list/{BUCKET}",
                    method="POST", body=body, expect_json=True)
        if not rows:
            break
        for it in rows:
            # folders come back with id == None; skip them, keep real files
            if it.get("id") and it.get("name"):
                out.append(prefix + it["name"])
        if len(rows) < page:
            break
        offset += page
    return out


def download(base, object_path, dest_path):
    blob = _req(f"{base}/storage/v1/object/{BUCKET}/{object_path}")
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(blob)
    return len(blob)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--school", required=True)
    ap.add_argument("--raw-dest", required=True)
    ap.add_argument("--key-dest", required=True)
    args = ap.parse_args()
    base = _env("SUPABASE_URL")

    raw_prefix = f"{args.school}/uploads/raw/"
    paths = list_prefix(base, raw_prefix)
    if not paths:
        sys.exit(f"pull_store: no raw files under {raw_prefix} — nothing to build")
    os.makedirs(args.raw_dest, exist_ok=True)
    total = 0
    for p in paths:
        fn = os.path.basename(p)
        n = download(base, p, os.path.join(args.raw_dest, fn))
        total += n
        print(f"  pulled {fn} ({n} bytes)")
    print(f"pull_store: {len(paths)} raw files, {total} bytes -> {args.raw_dest}")

    download(base, f"{args.school}/key.json", args.key_dest)
    print(f"pull_store: key -> {args.key_dest}")


if __name__ == "__main__":
    main()
