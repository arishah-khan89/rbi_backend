"""Reads the client's RBI workbook ("030-E-11- Shell.xlsx") into rules.json. Nothing of the client's is typed here.

Weights come from Factor_Master, every score table from Score_Bands (the table a factor uses is found from the factor's own formula
in RBI_Calc, so a table the client moves is followed), and the thresholds, risk matrix, risk policy and inspection-priority table from
Config. What this file knows is only the SHAPE of the sheet: which row of RBI_Calc is which input. Every formula it relies on is
checked against the shape it expects; a formula it does not understand stops the build with a message (it never guesses).

    python -m app.rbi_engine.rules_builder --file "C:/client/RBI/030-E-11- Shell.xlsx"           # shows what would change
    python -m app.rbi_engine.rules_builder --file "C:/client/RBI/030-E-11- Shell.xlsx" --apply   # writes rules.json

Things in the sheet that look wrong are not corrected: they are listed in "known_issues" so the person can ask the client.
The only exceptions are CORRECTIONS (a plain typing slip) and SETTING_CHOICES (one Config setting chosen on purpose), both listed too.
"""
import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter

RULES_PATH = Path(__file__).with_name("rules.json")
CALC, MASTER, BANDS, CONFIG, FLUIDS, LIBRARY = "RBI_Calc", "Factor_Master", "Score_Bands", "Config", "list of fluids", "NDE_Library"


# The ONLY places where the software does not take the client's number as it is: a number that is plainly a typing slip, corrected on
# purpose until the client answers. Each entry is applied only while the workbook still holds the old value (when the client fixes the
# cell the correction stops by itself) and is always listed in "known_issues".
# (cell of Score_Bands, number in the workbook, number used, why)
CORRECTIONS = [
    ("A146", 5000, 500, "the lower bounds of the toxic table are 0, 100, 200, 5000, 1000 (not ascending; the sheet's own text reads "
                        "'5000 - <1000 ppm'): 5000 is taken as 500 (0, 100, 200, 500, 1000), a typing slip. To be confirmed by the client."),
]


# A setting of the client's Config sheet that the software does not take as it is, on purpose. Applied only while the workbook holds a
# different value, and always listed in "known_issues". (setting, the value used, cell, why)
SETTING_CHOICES = [
    ("normalise_uninsulated", 1, "Config!B6",
     "the workbook holds 0 (the original logic: an uninsulated item can never score above 3.0 externally); its own note and the README recommend 1 "
     "(the other 60 % of the weights are rescaled to 100 %) and ask the client to decide. Switched on by the project owner; to be confirmed by the client."),
]


def _apply_setting_choices(cfg: dict, issues: List[str]) -> None:
    for key, value, cell, why in SETTING_CHOICES:
        if cfg.get(key) != value:
            issues.append(f"{cell}: {why} (workbook value {cfg.get(key)}, software value {value}).")
            cfg[key] = value


def _apply_corrections(table: List[dict], r1: int, r2: int, name: str, issues: List[str]) -> None:
    for cell, was, now, why in CORRECTIONS:
        row = int(cell[1:])
        if r1 <= row <= r2 and Decimal(str(table[row - r1]["lower"])) == Decimal(str(was)):
            table[row - r1]["lower"] = now
            issues.append(f"{name}: Score_Bands!{cell} holds {was}; the software uses {now}: {why}")


class RulesError(ValueError):
    """The workbook is not the one this reader knows: the message says what to look at."""


def _num(v: Any) -> Any:
    """A number as the sheet shows it (no float noise): int when whole, else a float with at most 12 significant digits."""
    if isinstance(v, bool) or v is None:
        return v
    d = Decimal(repr(float(v))) if isinstance(v, float) else Decimal(str(v))
    d = Decimal(format(d, ".12g"))
    return int(d) if d == d.to_integral_value() else float(d)


def _text(v: Any) -> Optional[str]:
    s = " ".join(str(v).split()) if v is not None else ""
    return s or None


def _formula(cell) -> Optional[str]:
    """The formula of a cell of the workbook opened WITH formulas (None when the cell holds a number or text)."""
    v = cell.value
    v = getattr(v, "text", v)
    return v if isinstance(v, str) and v.startswith("=") else None


# --------------------------------------------------------------------------------------------- what each factor is
# id -> (key of the input it scores, kind, calc row of its score formula, input kind)
#   kinds: band (INDEX/MATCH 1), map (INDEX/MATCH 0), switch, composite, financial, toxic, flammability, flash, dm, derived_band
# The numbers (weights, tables) are never here.
FACTORS: Dict[str, Dict[str, Any]] = {
    "C1.1": dict(kind="composite", row=31, inputs=["shutdown_required", "downtime_days", "lost_production_category"]),
    "C1.2": dict(kind="financial", row=35, inputs=["inventory_kg", "cost_per_kg", "repair_cost"]),
    "C2.1": dict(kind="toxic", row=39, inputs=["toxic_fluid"]),
    "C2.2": dict(kind="band", row=40, inputs=["toxic_percentage"]),
    "C2.3": dict(kind="map", row=41, inputs=["initial_fluid_phase"]),
    "C2.4": dict(kind="band", row=42, inputs=["detection_time"]),
    "C2.5": dict(kind="band", row=43, inputs=["isolation_time"]),
    "C2.6": dict(kind="band", row=44, inputs=["ignition_distance"]),
    "C2.7": dict(kind="flammability", row=45, inputs=["representative_fluid"]),
    "C2.8": dict(kind="band", row=46, inputs=["operating_temperature"]),
    "C2.9": dict(kind="band", row=47, inputs=["operating_pressure"]),
    "C2.10": dict(kind="flash", row=48, inputs=["representative_fluid", "operating_temperature"]),
    "C3.1": dict(kind="map", row=52, inputs=["reputation"]),
    "C3.2": dict(kind="map", row=53, inputs=["environmental"]),
    "C3.3": dict(kind="band", row=54, inputs=["workforce_persons"]),
    "P1.0": dict(kind="switch", row=59, inputs=["insulated"]),
    "P1.1": dict(kind="band", row=60, inputs=["cui_temperature"]),
    "P1.2": dict(kind="map", row=61, inputs=["insulation_type"]),
    "P1.3": dict(kind="map", row=62, inputs=["insulation_condition"]),
    "P1.4": dict(kind="band", row=63, inputs=["chloride_ppm"]),
    "P1.5": dict(kind="band", row=64, inputs=["area_humidity"]),
    "P1.6": dict(kind="band", row=65, inputs=["cooling_tower_distance"]),
    "P1.7": dict(kind="map", row=66, inputs=["coating_quality"]),
    "P1.8": dict(kind="dm", row=67, inputs=["mechanisms"], location="External"),
    "P1.9": dict(kind="band", row=68, inputs=["external_inspections"]),
    "P1.10": dict(kind="map", row=69, inputs=["external_inspection_confidence"]),
    "P1.11": dict(kind="derived_band", row=70, inputs=["remaining_life_external"]),
    "P2.1": dict(kind="dm", row=78, inputs=["mechanisms"], location="Internal"),
    "P2.2": dict(kind="map", row=79, inputs=["corrosion_trend"]),
    "P2.3": dict(kind="map", row=80, inputs=["internal_corrosion_type"]),
    "P2.4": dict(kind="band", row=81, inputs=["internal_corrosion_rate"]),
    "P2.5": dict(kind="derived_band", row=82, inputs=["component_life_ratio"]),
    "P2.6": dict(kind="derived_band", row=83, inputs=["wall_ratio"]),
    "P2.7": dict(kind="map", row=84, inputs=["operation_data_status"]),
    "P2.8": dict(kind="band", row=85, inputs=["internal_inspections"]),
    "P2.9": dict(kind="map", row=86, inputs=["internal_inspection_confidence"]),
    "P2.10": dict(kind="derived_band", row=87, inputs=["years_since_internal_inspection"], blank_score=5),
    "P2.11": dict(kind="map", row=88, inputs=["service_status"]),
    "P2.12": dict(kind="derived_band", row=89, inputs=["remaining_life_internal"]),
}

# the inputs of the form, by RBI_Calc row (label, unit, notes and the client's own "where does it come from" are read from the sheet)
# key -> (row, kind)   kind: date | number | yesno | select | fluid | text
INPUTS: List[Tuple[str, int, str, str]] = [
    # (key, calc row, kind, section)
    ("assessment_date", 8, "date", "equipment"), ("in_service_date", 9, "date", "equipment"),
    ("design_life_years", 10, "number", "equipment"), ("t_nominal", 11, "number", "equipment"),
    ("t_actual", 12, "number", "equipment"), ("t_min", 13, "number", "equipment"),
    ("external_corrosion_rate", 14, "number", "equipment"), ("internal_corrosion_rate", 15, "number", "equipment"),
    ("last_internal_inspection", 16, "date", "equipment"), ("last_external_inspection", 17, "date", "equipment"),
    ("shutdown_required", 28, "yesno", "C1"), ("downtime_days", 29, "number", "C1"),
    ("lost_production_category", 30, "select", "C1"),
    ("inventory_kg", 32, "number", "C1"), ("cost_per_kg", 33, "number", "C1"), ("repair_cost", 34, "number", "C1"),
    ("toxic_fluid", 38, "fluid", "C2"), ("toxic_percentage", 40, "number", "C2"), ("initial_fluid_phase", 41, "select", "C2"),
    ("detection_time", 42, "number", "C2"), ("isolation_time", 43, "number", "C2"), ("ignition_distance", 44, "number", "C2"),
    ("operating_temperature", 46, "number", "C2"), ("operating_pressure", 47, "number", "C2"), ("representative_fluid", 49, "fluid", "C2"),
    ("reputation", 52, "select", "C3"), ("environmental", 53, "select", "C3"), ("workforce_persons", 54, "number", "C3"),
    ("insulated", 59, "yesno", "PE"), ("cui_temperature", 60, "number", "PE"), ("insulation_type", 61, "select", "PE"),
    ("insulation_condition", 62, "select", "PE"), ("chloride_ppm", 63, "number", "PE"), ("area_humidity", 64, "number", "PE"),
    ("cooling_tower_distance", 65, "number", "PE"), ("coating_quality", 66, "select", "PE"),
    ("external_inspections", 68, "number", "PE"), ("external_inspection_confidence", 69, "select", "PE"),
    ("corrosion_trend", 79, "select", "PI"), ("internal_corrosion_type", 80, "select", "PI"),
    ("operation_data_status", 84, "select", "PI"), ("internal_inspections", 85, "number", "PI"),
    ("internal_inspection_confidence", 86, "select", "PI"), ("service_status", 88, "select", "PI"),
]
# derived values shown next to the inputs (RBI_Calc "Derived values", rows 19 to 25)
DERIVED_ROWS = {"age_years": 19, "component_life_ratio": 20, "remaining_allowance": 21, "wall_ratio": 22,
                "remaining_life_external": 23, "remaining_life_internal": 24, "years_since_internal_inspection": 25}
# the row of RBI_Calc that holds the heading of each section of the form (its text is read from the sheet)
SECTION_ROWS = {"equipment": 5, "C1": 27, "C2": 37, "C3": 51, "PE": 58, "PI": 77}
# the client's note (RBI_Calc J15): the internal corrosion rate is chosen by the person: long term / short term / controlling / expected
CORROSION_RATE_KINDS = ["Controlling", "Short term", "Long term", "Expected"]

_RNG = r"Score_Bands!\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)"
_CORE = re.compile(r"INDEX\(" + _RNG + r",\s*MATCH\(\s*([A-Z]+)(\d+)\s*," + _RNG + r",\s*([01])\s*\)\)")


def _read_table(ws, col_key: str, col_val: str, r1: int, r2: int, numeric: bool, issues: Optional[List[str]] = None, what: str = "") -> List[dict]:
    out = []
    for r in range(r1, r2 + 1):
        key, score = ws[f"{col_key}{r}"].value, ws[f"{col_val}{r}"].value
        if key is None and score is None:
            continue
        if numeric:
            if not isinstance(key, (int, float)) or not isinstance(score, (int, float)):
                if not out and isinstance(key, str):
                    # a range that starts one row too high takes in the table's heading: Excel skips text when it looks a number up
                    if issues is not None:
                        issues.append(f"{what}: the table range {BANDS}!A{r1}:B{r2} starts on the heading row ({r}, '{key}'). Excel skips it, "
                                      f"so the software skips it too.")
                    continue
                raise RulesError(f"{BANDS}!{col_key}{r}: a band table needs a number and a score, found '{key}' and '{score}'.")
            out.append({"lower": _num(key), "score": _num(score)})
        else:
            out.append({"option": _text(key), "score": _num(score), "description": _text(ws[f"C{r}"].value)})
    return out


def _core(formula: str, calc_row: int, where: str) -> Tuple[int, int, int, str]:
    """(first row, last row, match type) of the Score_Bands table in `formula`, after checking it looks up this row's input."""
    m = _CORE.search(formula)
    if not m:
        raise RulesError(f"{where}: the formula is not an INDEX / MATCH on Score_Bands: {formula}")
    a1, r1, a2, r2, mcol, mrow, b1, s1, b2, s2, kind = m.groups()
    if (a1, a2) != ("B", "B") or (b1, b2) != ("A", "A") or (int(r1), int(r2)) != (int(s1), int(s2)):
        raise RulesError(f"{where}: the table must be scores in column B and keys in column A on the same rows: {formula}")
    if int(mrow) != calc_row or mcol != "D":
        raise RulesError(f"{where}: the formula should look up its own row's input (D{calc_row}) but looks up {mcol}{mrow}: {formula}")
    return int(r1), int(r2), int(kind), formula


def _expect(formula: Optional[str], pattern: str, where: str) -> None:
    if formula is None or not re.fullmatch(pattern, formula, re.S):
        raise RulesError(f"{where}: the formula is not the one this reader knows ({formula}).")


def _section_of_row(row: int) -> str:
    return "equipment" if row < 26 else "C1" if row < 37 else "C2" if row < 51 else "C3" if row < 57 else "PE" if row < 76 else "PI"


def _read_factors(wf, wb) -> Tuple[List[dict], Dict[str, int], List[str]]:
    """`wf`: the workbook opened with formulas, `wb`: the same one with the numbers Excel calculated."""
    master, calc, bands = wb[MASTER], wb[CALC], wb[BANDS]
    mf, cf = wf[MASTER], wf[CALC]
    issues: List[str] = []
    # the rows the sheet's own "weight check" adds up (Factor_Master F48: SUMIFS($F$6:$F$43, ...))
    check = _formula(mf["F48"]) or ""
    m = re.search(r"SUMIFS\(\$F\$(\d+):\$F\$(\d+),\$C\$(\d+):\$C\$(\d+),", check)
    if not m:
        raise RulesError(f"{MASTER}!F48: the weight check is not a SUMIFS over the factor rows: {check}")
    check_first, check_last = int(m.group(1)), int(m.group(2))
    rows: Dict[str, int] = {}
    for r in range(6, master.max_row + 1):
        fid = _text(master[f"A{r}"].value)
        if fid and re.fullmatch(r"[CP]\d\.\d+", fid):
            rows[fid] = r
    missing = [f for f in FACTORS if f not in rows]
    unknown = [f for f in rows if f not in FACTORS]
    if missing or unknown:
        raise RulesError(f"{MASTER}: the factor list changed (missing {missing}, new {unknown}).")
    # RBI_Calc row of each factor, by the ID in its column A
    calc_rows = {_text(calc[f"A{r}"].value): r for r in range(1, calc.max_row + 1) if _text(calc[f"A{r}"].value) in FACTORS}
    out: List[dict] = []
    tables: Dict[str, dict] = {}
    for fid, spec in FACTORS.items():
        mr, cr = rows[fid], spec["row"]
        if calc_rows.get(fid) != cr:
            raise RulesError(f"{CALC}: factor {fid} should be on row {cr} but is on row {calc_rows.get(fid)}: the layout changed.")
        weight = master[f"F{mr}"].value
        weight = _num(weight) if isinstance(weight, (int, float)) else 0
        counted = check_first <= mr <= check_last
        if not counted and weight:
            issues.append(f"{fid} '{_text(master[f'E{mr}'].value)}' has a weight of {weight} in {MASTER} but is outside the rows the sheet's "
                          f"weight check adds up (rows {check_first}-{check_last}), so it carries no weight here (as the README says of "
                          f"internal remaining life: it drives the interval and the override, not the score).")
            weight = 0
        f = {"id": fid, "side": _text(master[f"B{mr}"].value), "group": _text(master[f"C{mr}"].value), "label": _text(master[f"E{mr}"].value),
             "weight": weight, "cui": bool(master[f"G{mr}"].value == 1), "kind": spec["kind"], "inputs": spec["inputs"],
             "scale": [_text(master[f"{c}{mr}"].value) for c in "KLMNO"], "note": _text(master[f"P{mr}"].value),
             "calc_row": cr, "location": spec.get("location")}
        where = f"{CALC}!F{cr}"
        formula = _formula(cf[f"F{cr}"])
        d = f"D{cr}"
        kind = spec["kind"]
        if kind in ("band", "derived_band", "map", "toxic"):
            wrap = {
                "band": rf'=IF\({d}="","",(?P<core>.+)\)', "map": rf'=IF\({d}="","",(?P<core>.+)\)',
                "derived_band": (rf'=IF\({d}="",5,(?P<core>.+)\)' if spec.get("blank_score") else rf'=IF\({d}="","",(?P<core>.+)\)') if fid != "P2.12" else r"=(?P<core>.+)",
                "toxic": rf'=IF\({d}="","",IF\({d}=0,0,(?P<core>.+)\)\)',
            }[kind]
            wm = re.fullmatch(wrap, formula or "", re.S)
            if not wm:
                raise RulesError(f"{where}: the formula is not the one this reader knows for {fid}: {formula}")
            r1, r2, mtype, _ = _core(wm.group("core"), cr, where)
            want = 0 if kind == "map" else 1
            if mtype != want:
                raise RulesError(f"{where}: expected a {'exact' if want == 0 else 'approximate'} match for {fid}: {formula}")
            numeric = kind != "map"
            f["table"] = _read_table(bands, "A", "B", r1, r2, numeric, issues, f"{fid} '{f['label']}'")
            f["table_rows"] = f"{BANDS}!A{r1}:B{r2}"
            if numeric:
                _apply_corrections(f["table"], r1, r2, f"{fid} '{f['label']}'", issues)
                lowers = [t["lower"] for t in f["table"]]
                if lowers != sorted(lowers) or len(set(lowers)) != len(lowers):
                    issues.append(f"{fid} '{f['label']}': the lower bounds in {f['table_rows']} are not in ascending order ({', '.join(str(x) for x in lowers)}), "
                                  f"so a lookup in it gives results that depend on how Excel searches. The software searches the same way Excel does.")
            if "blank_score" in spec:
                f["blank_score"] = spec["blank_score"]
            if kind == "toxic":
                _expect(_formula(cf[f"D{cr}"]), r"=VLOOKUP\(D38,'list of fluids'!\$A\$1:\$G\$116,5,0\)", f"{CALC}!D{cr}")
                f["fluid_column"] = "toxicity concentration (column E of the fluid list)"
        elif kind == "switch":
            _expect(formula, rf'=IF\({d}="","",IF\({d}="Yes",1,0\)\)', where)
        elif kind == "composite":
            _expect(formula, r'=IF\(OR\(D28="",D29="",D30=""\),"",IF\(D28="No",1,MAX\(F29,F30\)\)\)', where)
            r1, r2, t, _ = _core(_formula(cf["F29"]), 29, f"{CALC}!F29")
            if t != 1:
                raise RulesError(f"{CALC}!F29: the downtime table must be an approximate match.")
            f["downtime_table"] = _read_table(bands, "A", "B", r1, r2, True, issues, f"{fid} downtime")
            f["downtime_rows"] = f"{BANDS}!A{r1}:B{r2}"
            wm = re.fullmatch(r'=IF\(D30="","",(?P<core>.+)\)', _formula(cf["F30"]) or "", re.S)
            if not wm:
                raise RulesError(f"{CALC}!F30: the lost-production formula is not the one this reader knows.")
            r1, r2, t, _ = _core(wm.group("core"), 30, f"{CALC}!F30")
            f["lost_production_table"] = _read_table(bands, "A", "B", r1, r2, False)
            f["lost_production_rows"] = f"{BANDS}!A{r1}:B{r2}"
        elif kind == "financial":
            _expect(_formula(cf["D35"]), r'=IF\(OR\(D32="",D33="",D34=""\),"",D32\*D33\+D34\)', f"{CALC}!D35")
            wm = re.fullmatch(r'=IF\(D35="","",(?P<core>.+)\)', formula or "", re.S)
            if not wm:
                raise RulesError(f"{where}: the cost formula is not the one this reader knows.")
            r1, r2, t, _ = _core(wm.group("core"), 35, where)
            f["table"] = _read_table(bands, "A", "B", r1, r2, True, issues, f"{fid} cost")
            f["table_rows"] = f"{BANDS}!A{r1}:B{r2}"
        elif kind == "flammability":
            _expect(_formula(cf["D45"]), r"=VLOOKUP\(D49,'list of fluids'!B:J,5,0\)", f"{CALC}!D45")
            typed = calc[f"F{cr}"].value
            f["typed_score_in_sheet"] = _num(typed) if isinstance(typed, (int, float)) else None
            issues.append(f"{fid} 'Flammability': the sheet's score cell F{cr} holds the number {f['typed_score_in_sheet']} typed by hand, not a formula. "
                          f"{MASTER} says the score is the fluid's flammability rating + 1, so the software works it out that way.")
        elif kind == "flash":
            _expect(_formula(cf["D48"]), r"=VLOOKUP\(D49,'list of fluids'!B2:J117,9,0\)", f"{CALC}!D48")
            nested = re.sub(r"\s+", "", formula or "")
            steps = re.findall(r"IF\(D48<=([\d.]+)\*D46,(\d)", nested)
            _expect(re.sub(r"\s+", "", formula or ""),
                    r'=IF\(UPPER\(D48\)="N/A",0,IF\(D48<=1\.05\*D46,5,IF\(D48<=1\.1\*D46,4,IF\(D48<=1\.15\*D46,3,IF\(D48<=1\.2\*D46,2,IF\(D48<=1\.25\*D46,1,0\)\)\)\)\)\)', where)
            f["steps"] = [{"up_to_times_temperature": _num(a), "score": int(b)} for a, b in steps]
        elif kind == "dm":
            _expect(formula, rf"=IF\({d}=\"\",\"\",{d}\)", where)
        out.append(f)
        # a weight typed by hand in RBI_Calc that differs from the master (the sheet means to take it from the master)
        cw = calc[f"C{cr}"].value
        if isinstance(cw, (int, float)) and _formula(cf[f"C{cr}"]) is None and counted and _num(cw) != weight:
            issues.append(f"{fid} '{f['label']}': RBI_Calc C{cr} holds the weight {_num(cw)} typed by hand, but {MASTER} says {weight}. "
                          f"The software uses {MASTER} (the README says weights are changed there).")
    return out, {k: v for k, v in rows.items()}, issues


def _read_config(wb, issues: List[str]) -> dict:
    ws = wb[CONFIG]
    for cell, text in (("A6", "normalise"), ("A7", "hard override"), ("A8", "fraction"), ("A9", "unlimited"),
                       ("A38", "failure frequency"), ("A39", "decades"), ("A40", "damage score"), ("A41", "inspection-effectiveness"), ("A42", "credible")):
        if text not in str(ws[cell].value or "").lower():
            raise RulesError(f"{CONFIG}!{cell} should be the setting '{text}' but is '{ws[cell].value}': the layout changed.")
    cfg = {"normalise_uninsulated": _num(ws["B6"].value), "hard_override": _num(ws["B7"].value),
           "interval_fraction": _num(ws["B8"].value), "unlimited_life_years": _num(ws["B9"].value),
           "dm_anchor_frequency": _num(ws["B38"].value), "dm_decades_per_step": _num(ws["B39"].value),
           "dm_weight_damage": _num(ws["B40"].value), "dm_weight_inspection": _num(ws["B41"].value),
           "dm_credible_from": _num(ws["B42"].value)}
    _apply_setting_choices(cfg, issues)
    if cfg["dm_weight_damage"] + cfg["dm_weight_inspection"] != 100:
        raise RulesError(f"{CONFIG}: the weights of damage score and inspection effectiveness must total 100.")
    categories = [{"lower": _num(ws[f"A{r}"].value), "category": _num(ws[f"B{r}"].value),
                   "pof_label": _text(ws[f"C{r}"].value), "cof_label": _text(ws[f"D{r}"].value)} for r in range(13, 18)]
    if [c["category"] for c in categories] != [1, 2, 3, 4, 5]:
        raise RulesError(f"{CONFIG}: the score to category table (A13:B17) must give categories 1 to 5.")
    if [ws.cell(20, c).value for c in range(2, 7)] != [1, 2, 3, 4, 5] or [ws[f"A{r}"].value for r in range(21, 26)] != [5, 4, 3, 2, 1]:
        raise RulesError(f"{CONFIG}: the risk matrix must have POF 5 to 1 down column A and COF 1 to 5 along row 20.")
    matrix = {str(ws[f"A{r}"].value): [_text(ws.cell(r, c).value) for c in range(2, 7)] for r in range(21, 26)}
    levels = []
    for r in range(29, 33):
        fill = ws[f"A{r}"].fill
        colour = f"#{fill.fgColor.rgb[2:]}" if fill.fill_type and fill.fgColor.type == "rgb" and isinstance(fill.fgColor.rgb, str) else None
        levels.append({"label": _text(ws[f"A{r}"].value), "rank": _num(ws[f"B{r}"].value), "max_interval_years": _num(ws[f"C{r}"].value),
                       "min_effectiveness": _text(ws[f"D{r}"].value), "action": _text(ws[f"E{r}"].value), "colour": colour})
    used = {x for row in matrix.values() for x in row}
    if used - {l["label"] for l in levels}:
        raise RulesError(f"{CONFIG}: the matrix uses risk levels the policy table does not list: {sorted(used - {l['label'] for l in levels})}.")
    if [ws.cell(45, c).value for c in range(2, 7)] != [1, 2, 3, 4, 5] or [ws[f"A{r}"].value for r in range(46, 51)] != [5, 4, 3, 2, 1]:
        raise RulesError(f"{CONFIG}: the inspection priority table must have 5 to 1 down column A and 1 to 5 along row 45.")
    priority = {str(ws[f"A{r}"].value): [_num(ws.cell(r, c).value) for c in range(2, 7)] for r in range(46, 51)}
    return {"settings": cfg, "categories": categories, "matrix": matrix, "levels": levels, "priority": priority}


def _result_shape(wb, config: dict, issues: List[str]) -> dict:
    """How RBI_Calc reads the matrix and the priority table (which index is the row, which the column), taken from its formulas."""
    calc = wb[CALC]
    _expect(_formula(calc["D104"]), r"=INDEX\(Config!\$B\$21:\$F\$25,6-D102,D95\)", f"{CALC}!D104")
    prio = _formula(calc["D106"])
    shape = {}
    if re.fullmatch(r"=INDEX\(Config!\$B\$46:\$F\$50,6-D102,D95\)", prio or ""):
        shape["priority_rows"] = "pof"
    elif re.fullmatch(r"=INDEX\(Config!\$B\$46:\$F\$50,6-D95,D102\)", prio or ""):
        shape["priority_rows"] = "cof"
        issues.append("Inspection priority: Config's table is headed 'POF category rows x COF category columns' (like the risk matrix), but the "
                      f"formula in {CALC}!D106 uses the COF category as the row and the POF category as the column. The software follows the "
                      "formula (what the sheet computes); where POF and COF categories differ the two readings give different priorities.")
    else:
        raise RulesError(f"{CALC}!D106: the priority formula is not one this reader knows: {prio}")
    _expect(_formula(calc["D110"]), r"=ROUND\(IF\(D24>=Config!\$B\$9,D109,MIN\(D109,Config!\$B\$8\*D24\)\),1\)", f"{CALC}!D110")
    _expect(_formula(calc["D111"]), r"=ROUND\(IF\(D23>=Config!\$B\$9,D109,MIN\(D109,Config!\$B\$8\*D23\)\),1\)", f"{CALC}!D111")
    _expect(_formula(calc["D113"]),
            r'=IF\(ISNUMBER\(D112\),IF\(D112<D8,"OVERDUE",IF\(D112-D8<=183,"Due within 6 months","OK"\)\),"INSPECT NOW"\)', f"{CALC}!D113")
    _expect(_formula(calc["D100"]), r'=IF\(D99="YES",5,MAX\(D97,D98\)\)', f"{CALC}!D100")
    _expect(_formula(calc["D93"]), r"=MAX\(G36,G50,G56\)", f"{CALC}!D93")
    shape["due_window_days"] = 183
    return shape


def _form_inputs(wb) -> List[dict]:
    calc, bands = wb[CALC], wb[BANDS]
    out = []
    for key, row, kind, section in INPUTS:
        label = _text(calc[f"B{row}"].value)
        if key == "internal_corrosion_rate" and label:
            label = re.sub(r"\s*\((long|short)[- ]term\)", "", label, flags=re.I)      # which rate it is, is chosen in the field above
        note = _text(calc[f"H{row}"].value)
        if note and re.match(r"(sample|example)\b", note, re.I):
            note = None                                                              # a remark about the workbook's example row, not about the field
        item = {"key": key, "kind": kind, "section": section, "row": row, "label": label,
                "unit": _text(calc[f"E{row}"].value), "note": note, "source": _text(calc[f"J{row}"].value)}
        out.append(item)
    return out


def _section_titles(wb) -> Dict[str, str]:
    calc = wb[CALC]
    out = {}
    for key, row in SECTION_ROWS.items():
        title = _text(calc[f"A{row}"].value)
        want = {"equipment": "0.", "C1": "COF-1", "C2": "COF-2", "C3": "COF-3", "PE": "POF-1", "PI": "POF-2"}[key]
        if not title or not title.startswith(want):
            raise RulesError(f"{CALC}!A{row} should be the heading that starts with '{want}' but is '{title}': the layout changed.")
        out[key] = title
    return out


def _derived_labels(wb) -> List[dict]:
    calc = wb[CALC]
    return [{"key": key, "row": row, "label": _text(calc[f"B{row}"].value), "unit": _text(calc[f"E{row}"].value),
             "note": _text(calc[f"H{row}"].value), "how": _text(calc[f"J{row}"].value)} for key, row in DERIVED_ROWS.items()]


def _fluids_meta(wb) -> dict:
    ws = wb[FLUIDS]
    head = [_text(ws.cell(1, c).value) for c in range(1, 11)]
    expect = {1: "toxic", 2: "flamable", 4: "cs", 5: "toxicity", 6: "flamability", 7: "unit"}
    for c, word in expect.items():
        if word not in (head[c - 1] or "").lower():
            raise RulesError(f"'{FLUIDS}': column {get_column_letter(c)} should be headed '{word}' but is '{head[c - 1]}'.")
    return {"columns": {get_column_letter(c): head[c - 1] for c in range(1, 11)}}


def build_rules(path: str) -> Dict[str, Any]:
    wb = openpyxl.load_workbook(path, data_only=True)
    wf = openpyxl.load_workbook(path, data_only=False)
    try:
        for name in (CALC, MASTER, BANDS, CONFIG, FLUIDS, LIBRARY, "DM_Register", "Test_Cases"):
            if name not in wb.sheetnames:
                raise RulesError(f"There is no sheet called '{name}'.")
        # formulas are read from wf, numbers from wb: each reader below receives the book it needs
        factors, rows, issues = _read_factors(wf, wb)
        config = _read_config(wb, issues)
        shape = _result_shape(wf, config, issues)
        issues += _check_groups(factors)
        issues += _test_case_issues(wb)
        rules = {"source_file": Path(path).name, "factors": factors, "inputs": _form_inputs(wb), "sections": _section_titles(wb),
                 "derived": _derived_labels(wb), "corrosion_rate_kinds": CORROSION_RATE_KINDS, **config, "shape": shape,
                 "fluids": _fluids_meta(wb), "known_issues": issues}
        body = json.dumps(rules, sort_keys=True, ensure_ascii=False)
        rules["version"] = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        return rules
    finally:
        wb.close()
        wf.close()


def _check_groups(factors: List[dict]) -> List[str]:
    issues = []
    for g, label in (("C1", "COF-1"), ("C2", "COF-2"), ("C3", "COF-3"), ("PE", "POF-1 external"), ("PI", "POF-2 internal")):
        total = sum(Decimal(str(f["weight"])) for f in factors if f["group"] == g)
        if total != 100:
            issues.append(f"The weights of {label} add up to {total}, not 100.")
    return issues


def _test_case_issues(wb) -> List[str]:
    ws = wb["Test_Cases"]
    out = []
    for col in "FGHIJK":
        if ws[f"{col}63"].value == "FAIL":
            out.append(f"Test_Cases, {ws[f'{col}4'].value or col}: the sheet itself says FAIL (its 'expected' result is older than the current Config), "
                       f"so that case is compared with the sheet's own calculation, not with its expected values.")
    return out


def dumps(rules: Dict[str, Any]) -> str:
    return json.dumps(rules, indent=1, ensure_ascii=False, sort_keys=False) + "\n"


def describe_changes(old: Optional[dict], new: dict) -> List[str]:
    if old is None:
        return ["No rules were saved before: all of them are new."]
    out = []
    for f in new["factors"]:
        before = next((x for x in old.get("factors", []) if x["id"] == f["id"]), None)
        if before != f:
            out.append(f"{f['id']} {f['label']}: changed")
    for key in ("settings", "categories", "matrix", "levels", "priority", "shape", "inputs", "derived", "sections"):
        if old.get(key) != new.get(key):
            out.append(f"{key}: changed")
    for issue in new["known_issues"]:
        if issue not in old.get("known_issues", []):
            out.append(f"New issue in the sheet: {issue}")
    for issue in old.get("known_issues", []):
        if issue not in new["known_issues"]:
            out.append(f"Issue no longer in the sheet: {issue}")
    return out or ["Nothing is different from the rules now in use."]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        rules = build_rules(args.file)
    except RulesError as e:
        print(f"Not built: {e}")
        return 1
    old = json.loads(RULES_PATH.read_text(encoding="utf-8")) if RULES_PATH.exists() else None
    print(f"{len(rules['factors'])} factors read. Rules version {rules['version']}.")
    for line in describe_changes(old, rules):
        print("  -", line)
    if args.apply:
        RULES_PATH.write_text(dumps(rules), encoding="utf-8")
        print(f"Written: {RULES_PATH}")
    else:
        print("Nothing was written (add --apply to write).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
