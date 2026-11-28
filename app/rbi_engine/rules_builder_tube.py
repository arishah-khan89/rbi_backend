"""Reads the client's tube bundle workbook ("RBI Logics -Tube Bundles.xlsx") into rules_tube.json. Nothing of the client's is typed here.

Weights come from the "Weight (%)" column of RBI_Calc (the client said Factor_Master is not filled in for this sheet), every score table
from Score_Bands (found from the factor's own formula), the thresholds, matrix, policy and priority table from Config. As in
rules_builder.py, only the SHAPE of the sheet is known here, and every formula relied on is checked: a formula this reader does not know
stops the build with a message.

    python -m app.rbi_engine.rules_builder_tube --file "C:/client/RBI/RBI Logics -Tube Bundles.xlsx"           # shows what would change
    python -m app.rbi_engine.rules_builder_tube --file "C:/client/RBI/RBI Logics -Tube Bundles.xlsx" --apply   # writes rules_tube.json

The software does what the sheet's FORMULAS compute. Things that look wrong are not corrected: they are listed in "known_issues" so the
person can ask the client. The only exception is CORRECTIONS (a decision of the project owner, listed there too). Where a formula decides a behaviour (wall ratio, thickness override) it is read from the formula, so when the
client changes the cell, building again follows it.
"""
import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import openpyxl

from app.rbi_engine.rules_builder import (
    BANDS, CALC, CONFIG, CORROSION_RATE_KINDS, FLUIDS, LIBRARY, MASTER, RulesError, _core, _expect, _fluids_meta, _formula, _num,
    _read_config, _read_table, _text, describe_changes, dumps,
)

RULES_PATH = Path(__file__).with_name("rules_tube.json")
DM_SHEET = "DM -SHell"                                    # the sheet RBI_Calc takes P1.8 / P2.1 from
DM_UNUSED = "DM -TUBE side"

# id -> (kind, keys of the inputs it scores). A key that is not typed (derived) is listed in DERIVED_CELL below.
FACTORS: Dict[str, tuple] = {
    "C1.1": ("composite", ["shutdown_required", "downtime_days", "lost_production_category"]),
    "C1.2": ("financial", ["inventory_kg", "cost_per_kg", "repair_cost"]),
    "C2.1": ("toxic", ["toxic_fluid"]),
    "C2.2": ("band", ["toxic_percentage"]),
    "C2.3": ("map", ["initial_fluid_phase"]),
    "C2.4": ("band", ["detection_time"]),
    "C2.5": ("band", ["isolation_time"]),
    "C2.6": ("band", ["ignition_distance"]),
    "C2.7": ("flammability", ["representative_fluid"]),
    "C2.8": ("band", ["operating_temperature"]),
    "C2.9": ("band", ["operating_pressure"]),
    "C2.10": ("flash", ["representative_fluid", "operating_temperature"]),
    "C3.1": ("map", ["reputation"]),
    "C3.2": ("map", ["environmental"]),
    "C3.3": ("band", ["workforce_persons"]),
    "C4.1": ("derived_band", ["pressure_differential"]),
    "C4.2": ("derived_band", ["design_pressure_ratio"]),
    "C4.3": ("map", ["tube_leak_class"]),
    "C4.4": ("map", ["rupture_protection"]),
    "P1.7": ("map", ["coating_quality"]),
    "P1.8": ("dm", ["mechanisms"]),
    "P2.1": ("dm", ["mechanisms"]),
    "P2.2": ("map", ["corrosion_trend"]),
    "P2.3": ("map", ["internal_corrosion_type"]),
    "P2.5": ("derived_band", ["component_life_ratio"]),
    "P2.6": ("derived_band", ["wall_ratio"]),
    "P2.7": ("map", ["operation_data_status"]),
    "P2.8": ("band", ["internal_inspections"]),
    "P2.9": ("map", ["internal_inspection_confidence"]),
    "P2.10": ("derived_band", ["years_since_internal_inspection"]),
    "P2.11": ("map", ["service_status"]),
    "P3.1": ("derived_band", ["pressure_differential"]),
    "P3.2": ("derived_band", ["remaining_life_internal"]),
    "P3.3": ("derived_band", ["wall_loss_pct"]),
    "P3.4": ("derived_band", ["plugged_pct"]),
    "P3.5": ("map", ["fiv_fouling"]),
}
# the cell of RBI_Calc a worked-out value stands in (a derived_band factor's D formula must point at it)
DERIVED_CELL = {"pressure_differential": "D28", "design_pressure_ratio": "D32", "component_life_ratio": "D23",
                "wall_ratio": "D25", "years_since_internal_inspection": "D26", "remaining_life_internal": "D36", "wall_loss_pct": "D34",
                "plugged_pct": "D35"}
# inputs: (key, RBI_Calc row, kind, section)
INPUTS = [
    ("assessment_date", 8, "date", "equipment"), ("in_service_date", 9, "date", "equipment"), ("design_life_years", 10, "number", "equipment"),
    ("tube_operating_pressure", 11, "number", "equipment"), ("shell_operating_pressure", 12, "number", "equipment"),
    ("tube_design_pressure", 13, "number", "equipment"), ("shell_design_pressure", 14, "number", "equipment"),
    ("total_tubes", 15, "number", "equipment"), ("plugged_tubes", 16, "number", "equipment"),
    ("t_nominal", 17, "number", "equipment"), ("t_actual", 18, "number", "equipment"), ("t_min", 19, "number", "equipment"),
    ("internal_corrosion_rate", 20, "number", "equipment"), ("last_internal_inspection", 21, "date", "equipment"),
    ("shutdown_required", 39, "yesno", "C1"), ("downtime_days", 40, "number", "C1"), ("lost_production_category", 41, "select", "C1"),
    ("inventory_kg", 43, "number", "C1"), ("cost_per_kg", 44, "number", "C1"), ("repair_cost", 45, "number", "C1"),
    ("toxic_fluid", 49, "fluid", "C2"), ("toxic_percentage", 51, "number", "C2"), ("initial_fluid_phase", 52, "select", "C2"),
    ("detection_time", 53, "number", "C2"), ("isolation_time", 54, "number", "C2"), ("ignition_distance", 55, "number", "C2"),
    ("representative_fluid", 56, "fluid", "C2"), ("operating_temperature", 58, "number", "C2"), ("operating_pressure", 59, "number", "C2"),
    ("reputation", 63, "select", "C3"), ("environmental", 64, "select", "C3"), ("workforce_persons", 65, "number", "C3"),
    ("tube_leak_class", 70, "select", "C4"), ("rupture_protection", 71, "select", "C4"),
    ("coating_quality", 74, "select", "PT"), ("corrosion_trend", 77, "select", "PT"), ("internal_corrosion_type", 78, "select", "PT"),
    ("operation_data_status", 81, "select", "PT"), ("internal_inspections", 82, "number", "PT"),
    ("internal_inspection_confidence", 83, "select", "PT"), ("service_status", 85, "select", "PT"), ("fiv_fouling", 90, "select", "PT"),
]
# inputs the sheet links to another with a formula (RBI_Calc D59 is =D11): they take that value until the person types their own
LINKED = {"operating_pressure": "tube_operating_pressure"}
# derived values shown next to the inputs (key, row)
DERIVED_ROWS = [("age_years", 22), ("component_life_ratio", 23), ("remaining_allowance", 24), ("wall_ratio", 25),
                ("years_since_internal_inspection", 26), ("pressure_differential", 28), ("higher_pressure_side", 29),
                ("design_pressure_hp", 30), ("design_pressure_lp", 31), ("design_pressure_ratio", 32), ("rule_10_13", 33),
                ("wall_loss_pct", 34), ("plugged_pct", 35), ("remaining_life_internal", 36)]


def _need(wf, cell: str, pattern: str) -> None:
    _expect(_formula(wf[CALC][cell]), pattern, f"{CALC}!{cell}")


def _check_derived(wf) -> str:
    """The derived values are worked out the way engine_tube does them. Returns how the wall ratio is taken."""
    _need(wf, "D22", r'=IF\(D9="","",\(D8-D9\)/365\.25\)')
    _need(wf, "D23", r'=IF\(OR\(D22="",N\(D10\)<=0\),"",D22/D10\)')
    _need(wf, "D24", r"=D18-D19")
    _need(wf, "D26", r'=IF\(D21="","",\(D8-D21\)/365\.25\)')
    _need(wf, "D28", r'=IF\(OR\(D11="",D12=""\),"",ABS\(D11-D12\)\)')
    _need(wf, "D29", r'=IF\(OR\(D11="",D12=""\),"",IF\(D11>=D12,"Tube side","Shell side"\)\)')
    _need(wf, "D30", r'=IF\(D29="","",IF\(D29="Tube side",D13,D14\)\)')
    _need(wf, "D31", r'=IF\(D29="","",IF\(D29="Tube side",D14,D13\)\)')
    _need(wf, "D32", r'=IF\(D30="","",IF\(N\(D30\)<=0,"",D31/D30\)\)')
    _need(wf, "D34", r'=IF\(N\(D17\)<=0,"",MAX\(0,\(D17-D18\)/D17\*100\)\)')
    _need(wf, "D35", r'=IF\(N\(D15\)<=0,"",D16/D15\*100\)')
    _need(wf, "D36", r"=IF\(D18-D19<=0,0,IF\(D20<=0,Config!\$B\$9,MIN\(Config!\$B\$9,\(D18-D19\)/D20\)\)\)")
    _need(wf, "D59", r"=D11")
    _need(wf, "D50", r"=VLOOKUP\(D49,'list of fluids'!A:E,5,0\)")
    _need(wf, "D57", r"=VLOOKUP\(D56,'list of fluids'!B2:F116,5,0\)")
    _need(wf, "D60", r"=VLOOKUP\(D56,'list of fluids'!B:J,9,0\)")
    ratio = _formula(wf[CALC]["D25"]) or ""
    if ratio == "=D18/D19":
        return "actual_over_min"
    if ratio in ("=D19/D18", '=IF(N(D18)>0,D19/D18,"")'):
        return "min_over_actual"
    raise RulesError(f"{CALC}!D25: the wall ratio is not one this reader knows: {ratio}")


# group -> (cell of its sum, first row of the group, last row its factors can stand on)
GROUP_SUMS = {"C1": ("G47", 42, 46), "C2": ("G61", 49, 60), "C3": ("G66", 63, 65), "C4": ("G72", 68, 71), "PT": ("G92", 74, 90)}


# The ONLY places where the software does not follow the sheet's formula. Each is applied only while the workbook still holds the old
# formula (when the client fixes the cell the correction stops by itself) and is always listed in "known_issues".
# group -> (formula in the workbook, last row used instead, why)
CORRECTIONS = {
    "C2": ("=SUM(G49:G59)", 60, "the COF-2 sum stops one row before the flash point (row 60), although the flash point has a weight of 4, the "
                                "weights only total 100 with it and the shell sheet adds it. The software adds it. Decided by the project owner "
                                "(29 Nov 2026); to be confirmed by the client."),
}


def _group_rows(wf, issues: List[str]) -> Dict[str, range]:
    """The rows each group's own formula adds up (a factor outside them is not counted by the sheet), with CORRECTIONS applied."""
    out = {}
    for group, (cell, first, last) in GROUP_SUMS.items():
        m = re.fullmatch(r"=SUM\(G(\d+):G(\d+)\)", _formula(wf[CALC][cell]) or "")
        if not m or int(m.group(1)) != first or int(m.group(2)) > last + 1:
            raise RulesError(f"{CALC}!{cell}: the sum of group {group} should start on row {first} and end by row {last}.")
        end = int(m.group(2))
        fix = CORRECTIONS.get(group)
        if fix and _formula(wf[CALC][cell]) == fix[0]:
            end = fix[1]
            issues.append(f"{CALC}!{cell} holds {fix[0]}: {fix[2]}")
        out[group] = range(int(m.group(1)), end + 1)
    return out


def _check_results(wf) -> bool:
    """The results are put together the way engine_tube does them. Returns whether the POF formula applies the thickness override."""
    _need(wf, "D94", r"=MAX\(G47,G61,G66,G72\)")
    _need(wf, "D98", r"=G92")
    _need(wf, "D99", r'=IF\(AND\(Config!\$B\$7=1,D18<=D19\),"YES","No"\)')
    pof = _formula(wf[CALC]["D100"]) or ""
    if pof == "=G92":
        applied = False
    elif re.fullmatch(r'=IF\(D99="YES",5,(G92|D98)\)', pof):
        applied = True
    else:
        raise RulesError(f"{CALC}!D100: the POF formula is not one this reader knows: {pof}")
    _need(wf, "D103", r"=INDEX\(Config!\$B\$21:\$F\$25,6-D101,D96\)")
    _need(wf, "D105", r"=INDEX\(Config!\$B\$49:\$F\$53,6-D96,D101\)")
    _need(wf, "D110", r"=ROUND\(IF\(D36>=Config!\$B\$9,D108,MIN\(D108,Config!\$B\$8\*D36\)\),1\)")
    _need(wf, "D112", r'=IF\(ISNUMBER\(D111\),IF\(D111<D8,"OVERDUE",IF\(D111-D8<=183,"Due within 6 months","OK"\)\),IF\(D111="n/a","n/a","INSPECT NOW"\)\)')
    _need(wf, "D75", r"='DM -SHell'!\$F\$23")
    _need(wf, "D76", r"='DM -SHell'!\$F\$22")
    return applied


def _read_factors(wf, wb, issues: List[str], counted: Dict[str, range]) -> List[dict]:
    calc, master, bands = wb[CALC], wb[MASTER], wb[BANDS]
    rows = {_text(calc[f"A{r}"].value): r for r in range(1, calc.max_row + 1) if _text(calc[f"A{r}"].value) in FACTORS}
    missing = [f for f in FACTORS if f not in rows]
    extra = [t for r in range(37, 93) if (t := _text(calc[f"A{r}"].value)) and re.fullmatch(r"[CP]\d\.\d+", t) and t not in FACTORS and t != "C2.0"]
    if missing or extra:
        raise RulesError(f"{CALC}: the factor list changed (missing {missing}, new {extra}).")
    in_master = {_text(master[f"A{r}"].value): r for r in range(6, master.max_row + 1) if _text(master[f"A{r}"].value) in FACTORS}
    out, differ = [], []
    for fid, (kind, inputs) in FACTORS.items():
        cr, mr = rows[fid], in_master.get(fid)
        weight = calc[f"C{cr}"].value
        if not isinstance(weight, (int, float)):
            raise RulesError(f"{CALC}!C{cr}: {fid} has no weight (found '{weight}').")
        weight = _num(weight)
        if "[" in (_formula(wf[CALC][f"C{cr}"]) or "").split("Factor_Master")[0]:
            issues.append(f"{fid} '{_text(calc[f'B{cr}'].value)}': the weight in {CALC}!C{cr} is a link to another workbook file; the number Excel last "
                          f"saved there ({weight}) is used.")
        if mr is not None and isinstance(master[f"F{mr}"].value, (int, float)) and _num(master[f"F{mr}"].value) != weight:
            differ.append(fid)
        f: Dict[str, Any] = {"id": fid, "side": "COF" if fid.startswith("C") else "POF", "group": fid.split(".")[0] if fid.startswith("C") else "PT",
                             "label": _text(calc[f"B{cr}"].value), "weight": weight, "cui": False, "kind": kind, "inputs": inputs,
                             "scale": [_text(master[f"{c}{mr}"].value) for c in "KLMNO"] if mr else [None] * 5,
                             "note": _text(calc[f"H{cr}"].value), "calc_row": cr,
                             "location": "External" if fid == "P1.8" else "Internal" if fid == "P2.1" else None}
        f["counted"] = cr in counted[f["group"]]
        if not f["counted"]:
            cell = GROUP_SUMS[f["group"]][0]
            issues.append(f"{fid} '{f['label']}' is on {CALC} row {cr}, outside the rows its group sum adds up ({cell} = "
                          f"{_formula(wf[CALC][cell])}), so the sheet does not count it (weight {weight}). The software follows the formula.")
        where, formula, d = f"{CALC}!F{cr}", _formula(wf[CALC][f"F{cr}"]), f"D{cr}"
        if kind == "derived_band":
            _expect(_formula(wf[CALC][d]), rf"={DERIVED_CELL[inputs[0]]}", f"{CALC}!{d}")
        if kind in ("band", "derived_band", "map", "toxic"):
            blank = rf'=IF\({d}="",5,(?P<core>.+)\)' if fid == "P2.10" else rf'=IF\({d}="","",(?P<core>.+)\)'
            wm = re.fullmatch(blank, formula or "", re.S)
            if not wm:
                raise RulesError(f"{where}: the formula is not the one this reader knows for {fid}: {formula}")
            r1, r2, mtype, _ = _core(wm.group("core"), cr, where)
            if mtype != (0 if kind == "map" else 1):
                raise RulesError(f"{where}: expected a {'exact' if kind == 'map' else 'approximate'} match for {fid}: {formula}")
            f["table"] = _read_table(bands, "A", "B", r1, r2, kind != "map", issues, f"{fid} '{f['label']}'")
            f["table_rows"] = f"{BANDS}!A{r1}:B{r2}"
            if fid == "P2.10":
                f["blank_score"] = 5
            if kind == "toxic":
                f["zero_is_zero"] = False
        elif kind == "composite":
            _expect(formula, r'=IF\(OR\(D39="",D40="",D41=""\),"",IF\(D39="No",1,MAX\(F40,F41\)\)\)', where)
            r1, r2, t, _ = _core(_formula(wf[CALC]["F40"]), 40, f"{CALC}!F40")
            f["downtime_table"] = _read_table(bands, "A", "B", r1, r2, True, issues, f"{fid} downtime")
            f["downtime_rows"] = f"{BANDS}!A{r1}:B{r2}"
            wm = re.fullmatch(r'=IF\(D41="","",(?P<core>.+)\)', _formula(wf[CALC]["F41"]) or "", re.S)
            if t != 1 or not wm:
                raise RulesError(f"{CALC}!F40/F41: the production-loss formulas are not the ones this reader knows.")
            r1, r2, t, _ = _core(wm.group("core"), 41, f"{CALC}!F41")
            f["lost_production_table"] = _read_table(bands, "A", "B", r1, r2, False)
            f["lost_production_rows"] = f"{BANDS}!A{r1}:B{r2}"
        elif kind == "financial":
            _need(wf, "D46", r'=IF\(OR\(D43="",D44="",D45=""\),"",D43\*D44\+D45\)')
            wm = re.fullmatch(r'=IF\(D46="","",(?P<core>.+)\)', formula or "", re.S)
            if not wm:
                raise RulesError(f"{where}: the cost formula is not the one this reader knows.")
            r1, r2, _, _ = _core(wm.group("core"), 46, where)
            f["table"] = _read_table(bands, "A", "B", r1, r2, True, issues, f"{fid} cost")
            f["table_rows"] = f"{BANDS}!A{r1}:B{r2}"
        elif kind == "flammability":
            wm = re.fullmatch(r'=IF\(D57="","",(?P<core>.+)\)', formula or "", re.S)
            if not wm:
                raise RulesError(f"{where}: the formula is not the one this reader knows: {formula}")
            r1, r2, _, _ = _core(wm.group("core"), 57, where)
            table = [(_num(float(bands[f"A{r}"].value)), _num(float(bands[f"B{r}"].value))) for r in range(r1, r2 + 1)]
            if table != [(n, n + 1) for n in range(5)]:
                raise RulesError(f"{BANDS}!A{r1}:B{r2}: the flammability table is no longer 'rating + 1': {table}")
        elif kind == "flash":
            flat = re.sub(r"\s+", "", formula or "")
            _expect(flat, r'=IF\(UPPER\(D60\)="N/A",0,IF\(D60<=1\.05\*D58,5,IF\(D60<=1\.1\*D58,4,IF\(D60<=1\.15\*D58,3,IF\(D60<=1\.2\*D58,2,'
                          r'IF\(D60<=1\.25\*D58,1,0\)\)\)\)\)\)', where)
            f["steps"] = [{"up_to_times_temperature": _num(float(a)), "score": int(b)} for a, b in re.findall(r"IF\(D60<=([\d.]+)\*D58,(\d)", flat)]
        elif kind == "dm":
            _expect(formula, rf'=IF\({d}="","",{d}\)', where)
        out.append(f)
    if differ:
        issues.append(f"{MASTER} holds other weights than {CALC} for {len(differ)} factors ({', '.join(differ)}). The weights of {CALC} are used "
                      f"(the client said {MASTER} is not filled in for this sheet).")
    return out


def _sheet_issues(wf, wb, factors: List[dict], config: dict, wall_ratio: str, override: bool) -> List[str]:
    issues = []
    heading = _text(wb[BANDS]["A133"].value) or ""
    for r in range(1, wb[BANDS].max_row + 1):
        text = _text(wb[BANDS][f"A{r}"].value) or ""
        if text.startswith("B17"):
            heading = text
            break
    if wall_ratio == "actual_over_min" and re.search(r"t_min\s*÷\s*t_actual", heading):
        issues.append(f"P2.6 Wall ratio: {CALC}!D25 works out t_actual ÷ t_min, but the table it is looked up in is headed '{heading[:60]}' "
                      f"(t_min ÷ t_actual). With the formula as it is, a sound tube scores 5 (worst). The software follows the formula.")
    if not override and config["settings"]["hard_override"] == 1:
        issues.append(f"Thickness override: {CONFIG}!B7 is on and {CALC}!D99 reports 'YES' when the tube is at or below t_min, but the POF formula "
                      f"({CALC}!D100) does not use it, so the probability is not raised to 5. The software follows the formula and shows a warning.")
    refs = {name: sum(1 for row in wf[name].iter_rows() for c in row if isinstance(c.value, str) and "#REF!" in c.value) for name in wf.sheetnames}
    for name, n in refs.items():
        if n:
            issues.append(f"'{name}': {n} formulas contain #REF! (they point at a cell that no longer exists).")
    used = any(isinstance(c.value, str) and DM_UNUSED in c.value for row in wf[CALC].iter_rows() for c in row)
    if DM_UNUSED in wf.sheetnames and not used:
        issues.append(f"'{DM_UNUSED}' is not used by {CALC}: P1.8 and P2.1 are taken from '{DM_SHEET}'. In the software a mechanism's place "
                      f"(external = shell side, internal = tube side) comes from the damage-mechanism library.")
    for g in ("C1", "C2", "C3", "C4", "PT"):
        total = sum(Decimal(str(f["weight"])) for f in factors if f["group"] == g)
        if total != 100:
            issues.append(f"The weights of group {g} add up to {total}, not 100.")
    return issues


def _mechanism_names(wb) -> List[str]:
    """The damage mechanisms of this workbook's own NDE_Library: the only ones a tube bundle analysis offers."""
    ws = wb[LIBRARY]
    head = [_text(ws.cell(5, c).value) or "" for c in (2, 3)]
    if head != ["Damage mechanism", "Location"]:
        raise RulesError(f"{LIBRARY}!B5:C5 should be headed 'Damage mechanism', 'Location' but is {head}.")
    names = [n for r in range(6, ws.max_row + 1) if (n := _text(ws.cell(r, 2).value)) and _text(ws.cell(r, 3).value) in ("Internal", "External")]
    if not names:
        raise RulesError(f"{LIBRARY}: no damage mechanism found from row 6.")
    return names


# The column the tube sheet's inspection priority table lacks (POF category 5), by COF category. The table is the shell sheet's with its
# last column cut off: the first four columns are the same, and the numbers missing from 1..25 are exactly these.
PRIORITY_POF5 = {"5": 1, "4": 3, "3": 5, "2": 10, "1": 12}
DECIDED = "Decided by the project owner (29 Nov 2026); to be confirmed by the client."


def _apply_corrections(config: dict, wall_ratio: str, override: bool, issues: List[str]) -> tuple:
    """The corrections that are not a group sum (see CORRECTIONS). Returns how the wall ratio is taken and whether POF is 5 at t_min."""
    if wall_ratio == "actual_over_min":
        wall_ratio = "min_over_actual"
        issues.append(f"P2.6 Wall ratio: {CALC}!D25 works out t_actual ÷ t_min, but its score table is headed t_min ÷ t_actual and the shell "
                      f"sheet takes t_min ÷ t_actual (as written, a thinner tube scores better). The software takes t_min ÷ t_actual. {DECIDED}")
    if not override and config["settings"]["hard_override"] == 1:
        override = True
        issues.append(f"Thickness override: {CONFIG}!B7 is on and {CALC}!D99 / D100 say POF is 5 at or below t_min, but the D100 formula (=G92) "
                      f"leaves it out. The software sets POF to 5, as the shell sheet does. {DECIDED}")
    if all(row[4] is None for row in config["priority"].values()):
        for cof, value in PRIORITY_POF5.items():
            config["priority"][cof][4] = value
        issues[:] = [i for i in issues if "stops at category 4" not in i]
        issues.append(f"{CONFIG}: the inspection priority table has no column for POF category 5. The software uses the column of the shell sheet "
                      f"(COF 5 to 1: {', '.join(str(PRIORITY_POF5[k]) for k in '54321')}). {DECIDED}")
    return wall_ratio, override


def build_rules(path: str) -> Dict[str, Any]:
    wb = openpyxl.load_workbook(path, data_only=True)
    wf = openpyxl.load_workbook(path, data_only=False)
    try:
        for name in (CALC, MASTER, BANDS, CONFIG, FLUIDS, LIBRARY, DM_SHEET):
            if name not in wb.sheetnames:
                raise RulesError(f"There is no sheet called '{name}'.")
        issues: List[str] = []
        wall_ratio = _check_derived(wf)
        override = _check_results(wf)
        factors = _read_factors(wf, wb, issues, _group_rows(wf, issues))
        config = _read_config(wb, issues, priority_header=48, choices=False)
        wall_ratio, override = _apply_corrections(config, wall_ratio, override, issues)
        calc, bands = wb[CALC], wb[BANDS]
        inputs = []
        for key, row, kind, section in INPUTS:
            note = _text(calc[f"H{row}"].value)
            if note and re.match(r"(sample|example)\b", note, re.I):
                note = None
            label = _text(calc[f"B{row}"].value)
            if key == "toxic_fluid":
                label = "Toxic fluid"
            inputs.append({"key": key, "kind": kind, "section": section, "row": row, "label": label,
                           "unit": None if key in ("toxic_fluid", "representative_fluid") else _text(calc[f"E{row}"].value), "note": note,
                           "source": _text(calc[f"J{row}"].value) if row <= 36 else None, **({"linked_to": LINKED[key]} if key in LINKED else {})})
        sections = {"equipment": _text(calc["A5"].value), "C1": _text(calc["A38"].value), "C2": _text(calc["A48"].value),
                    "C3": _text(calc["A62"].value), "C4": _text(calc["A67"].value), "PT": "POF-3  Tube bundle"}
        for key, start in (("equipment", "0."), ("C1", "COF-1"), ("C2", "COF-2"), ("C3", "COF-3"), ("C4", "COF-4")):
            if not (sections[key] or "").startswith(start):
                raise RulesError(f"{CALC}: the heading of section {key} should start with '{start}' but is '{sections[key]}'.")
        derived = [{"key": key, "row": row, "label": _text(calc[f"B{row}"].value), "unit": _text(calc[f"E{row}"].value),
                    "note": _text(calc[f"H{row}"].value), "how": _text(calc[f"K{row}"].value)} for key, row in DERIVED_ROWS]
        if wall_ratio == "min_over_actual":
            next(d for d in derived if d["key"] == "wall_ratio")["unit"] = "t_min ÷ t_actual"
        settings = config["settings"]
        settings["override_applied"] = 1 if override else 0
        settings["design_pressure_ratio_limit"] = float(wb[CONFIG]["B45"].value)
        issues += _sheet_issues(wf, wb, factors, config, wall_ratio, override)
        status = [re.findall(r'"([^"]*)"', _formula(wf[CALC]["D33"]) or "")]
        rules = {"model": "tube", "source_file": Path(path).name, "factors": factors, "inputs": inputs, "sections": sections, "derived": derived,
                 "corrosion_rate_kinds": CORROSION_RATE_KINDS, **config, "shape": {"priority_rows": "cof", "due_window_days": 183},
                 "wall_ratio": wall_ratio, "rule_10_13_texts": [t for t in status[0] if t], "dm_life_factor": "P3.2", "linked": LINKED,
                 "fluids": _fluids_meta(wb), "mechanism_names": _mechanism_names(wb), "known_issues": issues}
        body = json.dumps(rules, sort_keys=True, ensure_ascii=False)
        rules["version"] = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        return rules
    finally:
        wb.close()
        wf.close()


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
    for issue in rules["known_issues"]:
        print("  ?", issue)
    if args.apply:
        RULES_PATH.write_text(dumps(rules), encoding="utf-8")
        print(f"Written: {RULES_PATH}")
    else:
        print("Nothing was written (add --apply to write).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
