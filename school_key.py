"""school_key.py — engine-side loader for a school's data key.

Counterpart to the browser's school_key_admin.js: same key, same schema. The engine loads the school's
key, sources every school-specific value from it (no literals in engine code), and at the end emits the
flags.json that the Admin panel turns into "N items need you" tasks.
"""
import json
import os

try:
    import jsonschema
except ImportError:  # validation is best-effort in the engine; the Admin panel already validated on save
    jsonschema = None

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "school_data_key.schema.json")


def load_key(path, schema_path=_SCHEMA_PATH):
    """Load a school key and (if jsonschema + schema present) validate it. Returns the raw dict."""
    with open(path) as f:
        key = json.load(f)
    if jsonschema is not None and os.path.exists(schema_path):
        with open(schema_path) as f:
            schema = json.load(f)
        jsonschema.validate(key, schema)  # raises on an invalid key — fail fast, never import against junk
    return key


# Maps the engine's internal "unknown" trackers to the flag types the Admin layer routes on.
# Each value is an iterable of the raw values the key didn't cover.
def dump_flags(path, mapping):
    """Write flags.json in the shape school_key_admin.js#pendingDecisions consumes:
       [{ "type": "<flag>", "values": [ ... ] }, ...]   (only non-empty flags)."""
    flags = []
    for ftype, values in mapping.items():
        vals = sorted({str(v) for v in values}) if values else []
        if vals:
            flags.append({"type": ftype, "values": vals})
    with open(path, "w") as f:
        json.dump(flags, f, indent=2)
    return flags


class FlagCollector:
    """Optional helper if the engine wants to record flags as it goes rather than from end-state sets."""
    def __init__(self):
        self._f = {}

    def add(self, ftype, value):
        self._f.setdefault(ftype, set()).add(str(value))

    def mapping(self):
        return {t: v for t, v in self._f.items()}


def check_scale_alignment(key):
    """Every attainment scale a school actually uses must sit on the reference axis, or longitudinal
    charts can't place it. Returns the list of scale ids that lack a transition to referenceScale
    (engine emits these as the 'unaligned_scale' flag)."""
    sc = key.get('scales', {})
    ref = sc.get('referenceScale')
    used = set(sc.get('attainmentByYearGroup', {}).values())
    trans = key.get('transitions', {})
    return sorted(s for s in used if s != ref and f"{s}:{ref}" not in trans)
