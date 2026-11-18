"""Loads the reference lists of the client's workbook ("030-E-11- Shell.xlsx") into the database: the fluid list, and the damage-mechanism
library (sheet "NDE_Library") with the extra text of the "damages list" sheet.

    python -m app.seed.import_rbi_lists --file "C:/client/RBI/030-E-11- Shell.xlsx"            # shows what would change
    python -m app.seed.import_rbi_lists --file "C:/client/RBI/030-E-11- Shell.xlsx" --apply    # does it

Safe to run again when the client sends a newer workbook: a name that exists is updated, a new name is added, nothing is deleted.
Columns are read by position after checking the headings look right.
"""
import argparse
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional

import openpyxl
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.rbi_assessment import RbiFluidEntry, RbiMechanismEntry

FLUIDS, LIBRARY, DAMAGES = "list of fluids", "NDE_Library", "damages list"


class SheetError(ValueError):
    """The workbook does not look like the one the client gave: a message the person can read."""


def _text(v: Any) -> Optional[str]:
    s = " ".join(str(v).split()) if v is not None else ""
    return s or None


def _number(v: Any) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return Decimal(str(v).strip().replace(",", ""))
    except InvalidOperation:
        return None


def _heading(ws, col: int, word: str) -> None:
    head = str(ws.cell(1, col).value or "").lower()
    if word not in head:
        raise SheetError(f"Sheet '{ws.title}': column {col} should be headed with '{word}' but is '{ws.cell(1, col).value}'.")


def read_fluids(wb) -> List[dict]:
    if FLUIDS not in wb.sheetnames:
        raise SheetError(f"There is no sheet called '{FLUIDS}'.")
    ws = wb[FLUIDS]
    for col, word in ((1, "toxic"), (2, "flamable"), (4, "cs"), (5, "toxicity"), (6, "flamability"), (7, "unit")):
        _heading(ws, col, word)
    rows, toxic_seen, flam_seen = [], set(), set()
    for r in range(2, ws.max_row + 1):
        toxic, flam = _text(ws.cell(r, 1).value), _text(ws.cell(r, 2).value)
        if not toxic and not flam:
            continue
        for name, seen, what in ((toxic, toxic_seen, "toxic"), (flam, flam_seen, "flammable")):
            if name:
                if name.lower() in seen:
                    raise SheetError(f"Sheet '{FLUIDS}', row {r}: the {what} fluid '{name}' is written twice.")
                seen.add(name.lower())
        conc = ws.cell(r, 5).value
        rating = ws.cell(r, 6).value
        if rating is not None and not isinstance(rating, (int, float)):
            raise SheetError(f"Sheet '{FLUIDS}', row {r}: the flammability '{rating}' is not a number.")
        flash = ws.cell(r, 10).value
        rows.append({"toxic_name": toxic, "flammable_name": flam, "cas_number": _text(ws.cell(r, 4).value),
                     "toxicity_concentration": _number(conc), "toxicity_unit": _text(ws.cell(r, 7).value),
                     "flammability": int(rating) if rating is not None else None,
                     "flash_point": _text(flash) if flash is not None else None, "note": _text(ws.cell(r, 3).value)})
    return rows


def _temperature(v: Any):
    if isinstance(v, (int, float)):
        return Decimal(str(v)), None
    s = _text(v)
    if s is None or s.upper() in ("N/A", "NA", "NONE"):
        return None, None
    n = _number(s)
    return (n, None) if n is not None else (None, s[:200])


def read_mechanisms(wb) -> List[dict]:
    if LIBRARY not in wb.sheetnames:
        raise SheetError(f"There is no sheet called '{LIBRARY}'.")
    ws = wb[LIBRARY]
    for col, word in ((1, "id"), (2, "damage mechanism"), (3, "location"), (4, "class"), (5, "rate"), (7, "primary"), (8, "secondary")):
        if word not in str(ws.cell(5, col).value or "").lower():
            raise SheetError(f"Sheet '{LIBRARY}': column {col} should be headed with '{word}' but is '{ws.cell(5, col).value}'.")
    details: Dict[str, dict] = {}
    if DAMAGES in wb.sheetnames:
        dl = wb[DAMAGES]
        for col, word in ((1, "damage"), (4, "minimum"), (5, "maximum"), (8, "nde")):
            _heading(dl, col, word)
        for r in range(2, dl.max_row + 1):
            name = _text(dl.cell(r, 1).value)
            if name:
                lo, lo_note = _temperature(dl.cell(r, 4).value)
                hi, hi_note = _temperature(dl.cell(r, 5).value)
                notes = [f"minimum: {lo_note}" if lo_note else None, f"maximum: {hi_note}" if hi_note else None]
                details[name.lower()] = {"definition": _text(dl.cell(r, 2).value), "applicable_materials": _text(dl.cell(r, 3).value),
                                         "min_temperature_c": lo, "max_temperature_c": hi,
                                         "temperature_note": "; ".join(n for n in notes if n) or None,
                                         "process_streams": _text(dl.cell(r, 6).value), "damage_type": _text(dl.cell(r, 7).value),
                                         "recommended_nde": _text(dl.cell(r, 8).value)}
    rows, seen, codes = [], set(), set()
    for r in range(6, ws.max_row + 1):
        name = _text(ws.cell(r, 2).value)
        if not name:
            continue
        code = _text(ws.cell(r, 1).value)
        if not code:
            raise SheetError(f"Sheet '{LIBRARY}', row {r}: the mechanism '{name}' has no ID.")
        if name.lower() in seen:
            raise SheetError(f"Sheet '{LIBRARY}', row {r}: the mechanism '{name}' is written twice.")
        seen.add(name.lower())
        codes.add(code)
        location = _text(ws.cell(r, 3).value)
        if location not in (None, "Internal", "External"):
            raise SheetError(f"Sheet '{LIBRARY}', row {r}: the location '{location}' must be Internal or External.")
        row = {"code": code, "name": name[:200], "location": location, "damage_class": _text(ws.cell(r, 4).value),
               "rate_based": _text(ws.cell(r, 5).value), "typical_drivers": _text(ws.cell(r, 6).value),
               "primary_nde": _text(ws.cell(r, 7).value), "secondary_nde": _text(ws.cell(r, 8).value),
               "coverage_timing": _text(ws.cell(r, 9).value), "mitigation": _text(ws.cell(r, 10).value), "sort_order": len(rows) + 1}
        row.update(details.get(name.lower(), {}))
        rows.append(row)
    return rows


def _differs(a: Any, b: Any) -> bool:
    return (Decimal(str(a)) != Decimal(str(b))) if isinstance(a, (Decimal, int, float)) and isinstance(b, (Decimal, int, float)) else a != b


def duplicate_codes(rows: List[dict]) -> List[str]:
    count: Dict[str, List[str]] = {}
    for m in rows:
        count.setdefault(m["code"], []).append(m["name"])
    return [f"{c}: {', '.join(names)}" for c, names in count.items() if len(names) > 1]


def _upsert(db: Session, model, rows: List[dict], key: Callable) -> Dict[str, int]:
    existing = {key(o): o for o in db.query(model).all()}
    added = changed = same = 0
    for row in rows:
        obj = existing.get(key(row))
        if obj is None:
            db.add(model(**row))
            added += 1
            continue
        diff = {k: v for k, v in row.items() if _differs(getattr(obj, k), v)}
        if diff:
            for k, v in diff.items():
                setattr(obj, k, v)
            changed += 1
        else:
            same += 1
    return {"added": added, "changed": changed, "unchanged": same}


def import_lists(db: Session, path: str, apply: bool = False) -> Dict[str, Any]:
    """Reads the workbook and, when `apply` is true, saves it. Returns what was (or would be) added / changed for each list."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    try:
        fluids, mechanisms = read_fluids(wb), read_mechanisms(wb)
    finally:
        wb.close()
    fluid_key = lambda o: ((o["toxic_name"] if isinstance(o, dict) else o.toxic_name) or "").lower() + "|" + \
        ((o["flammable_name"] if isinstance(o, dict) else o.flammable_name) or "").lower()          # noqa: E731
    result = {"fluids": _upsert(db, RbiFluidEntry, fluids, fluid_key),
              "mechanisms": _upsert(db, RbiMechanismEntry, mechanisms, lambda o: (o["name"] if isinstance(o, dict) else o.name).lower()),
              "duplicate_codes": duplicate_codes(mechanisms),
              "without_location": [m["name"] for m in mechanisms if not m["location"]],
              "flammability_outside_0_to_4": [f["flammable_name"] for f in fluids if f["flammability"] is not None and not 0 <= f["flammability"] <= 4],
              "damages_list_not_in_library": _unmatched(wb_path=path, mechanisms=mechanisms)}
    if apply:
        db.commit()
    else:
        db.rollback()
    return result


def _unmatched(wb_path: str, mechanisms: List[dict]) -> List[str]:
    wb = openpyxl.load_workbook(wb_path, data_only=True)
    try:
        if DAMAGES not in wb.sheetnames:
            return []
        have = {m["name"].lower() for m in mechanisms}
        ws = wb[DAMAGES]
        return [_text(ws.cell(r, 1).value) for r in range(2, ws.max_row + 1) if _text(ws.cell(r, 1).value) and _text(ws.cell(r, 1).value).lower() not in have]
    finally:
        wb.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True, help="the client's RBI workbook (.xlsx)")
    ap.add_argument("--apply", action="store_true", help="save the changes (without it nothing is saved)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from app.core.database import SessionLocal
    db = SessionLocal()
    try:
        result = import_lists(db, args.file, apply=args.apply)
    except SheetError as e:
        print(f"Not imported: {e}")
        return 1
    finally:
        db.close()
    for part in ("fluids", "mechanisms"):
        c = result[part]
        print(f"{part}: {c['added']} to add, {c['changed']} to change, {c['unchanged']} already the same")
    if result["without_location"]:
        print("Mechanisms with no location (cannot be used until the client gives Internal / External):", ", ".join(result["without_location"]))
    if result["duplicate_codes"]:
        print("IDs used for more than one mechanism (the client should correct them):", "; ".join(result["duplicate_codes"]))
    if result["flammability_outside_0_to_4"]:
        print("Fluids whose flammability is not an NFPA rating from 0 to 4 (they cannot be scored until the client corrects them):",
              ", ".join(result["flammability_outside_0_to_4"]))
    if result["damages_list_not_in_library"]:
        print("In 'damages list' but not in 'NDE_Library' (their details are not loaded):", ", ".join(result["damages_list_not_in_library"]))
    print("Saved." if args.apply else "Nothing was saved (add --apply to save).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
