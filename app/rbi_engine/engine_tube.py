"""Scores one tube bundle analysis with the rules read from the client's tube bundle workbook (rules_tube.json).

It differs from the shell analysis (engine.py), as the two workbooks do:
  * consequence = the largest of four groups (the fourth is the tube bundle: pressure difference, 10/13 rule, fluid pair, protection);
  * probability = ONE score (damage mechanisms of both sides, tube data, inspection history), not an external and an internal path;
  * the plan is for the internal inspection only, from the remaining life of the tube bundle.
How each factor is scored, the damage-mechanism register, the categories, matrix and NDE advice are the same code as the shell analysis.
The software computes what the sheet's formulas compute; what looks wrong in the sheet is listed in the rules' known_issues.
"""
from decimal import Decimal
from typing import Any, Dict, Optional

from app.rbi_engine.engine import (
    Q9, _combine, _mechanism_rows, _res, _score_factor, blank, category_of, dec, due_info, mechanism_summary, nde_advice, norm,
    recommended_interval, to_date,
)

GROUPS = ("C1", "C2", "C3", "C4", "PT")
COF_NAMES = {"C1": "COF-1 Production & financial", "C2": "COF-2 Process-fluid hazard", "C3": "COF-3 Reputation / environment / personnel",
             "C4": "COF-4 Tube bundle ΔP & cross-leak"}


def derived_values(rules: dict, inputs: dict) -> Dict[str, Any]:
    """RBI_Calc rows 22 to 36 of the tube bundle sheet."""
    unlimited = Decimal(str(rules["settings"]["unlimited_life_years"]))
    n = lambda key: dec(inputs.get(key))
    assess, start = to_date(inputs.get("assessment_date")), to_date(inputs.get("in_service_date"))
    life = n("design_life_years")
    t_nom, t_act, t_min = n("t_nominal"), n("t_actual"), n("t_min")
    tube_p, shell_p = n("tube_operating_pressure"), n("shell_operating_pressure")
    tube_d, shell_d = n("tube_design_pressure"), n("shell_design_pressure")
    out: Dict[str, Any] = {}
    out["age_years"] = age = (Decimal((assess - start).days) / Decimal("365.25")) if assess and start else None
    out["component_life_ratio"] = (age / life) if age is not None and life is not None and life > 0 else None
    out["remaining_allowance"] = allowance = (t_act - t_min) if t_act is not None and t_min is not None else None
    top, bottom = (t_act, t_min) if rules["wall_ratio"] == "actual_over_min" else (t_min, t_act)          # D25, or its correction
    out["wall_ratio"] = (top / bottom) if top is not None and bottom is not None and top > 0 and bottom > 0 else None
    last = to_date(inputs.get("last_internal_inspection"))
    out["years_since_internal_inspection"] = (Decimal((assess - last).days) / Decimal("365.25")) if assess and last else None

    both = tube_p is not None and shell_p is not None
    out["pressure_differential"] = abs(tube_p - shell_p) if both else None
    high = ("Tube side" if tube_p >= shell_p else "Shell side") if both else None
    out["higher_pressure_side"] = high
    hp = (tube_d if high == "Tube side" else shell_d) if high else None
    lp = (shell_d if high == "Tube side" else tube_d) if high else None
    out["design_pressure_hp"], out["design_pressure_lp"] = hp, lp
    ratio = (lp / hp) if hp is not None and lp is not None and hp > 0 else None
    out["design_pressure_ratio"] = ratio
    texts = rules.get("rule_10_13_texts") or []
    limit = Decimal(str(rules["settings"]["design_pressure_ratio_limit"]))
    out["rule_10_13"] = None if ratio is None or len(texts) < 2 else texts[0] if ratio >= limit - Q9 else texts[1]

    out["wall_loss_pct"] = max(Decimal(0), (t_nom - t_act) / t_nom * 100) if t_nom is not None and t_nom > 0 and t_act is not None else None
    total, plugged = n("total_tubes"), n("plugged_tubes")
    out["plugged_pct"] = (plugged / total * 100) if total is not None and total > 0 and plugged is not None else None

    rate = n("internal_corrosion_rate")
    remaining = None
    if allowance is not None and allowance <= 0:
        remaining = Decimal(0)
    elif allowance is not None and rate is not None:
        remaining = unlimited if rate <= 0 else min(unlimited, allowance / rate)
    out["remaining_life_internal"] = remaining
    out["remaining_life_external"] = None                          # the tube bundle sheet has no external rate: an external mechanism has its own
    return out


def evaluate(rules: Dict[str, Any], inputs: Dict[str, Any], fluids: Optional[Dict[str, Any]] = None,
             library: Optional[Dict[str, dict]] = None, score_overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The same arguments and the same shape of answer as engine.evaluate. `score_overrides` is for the tests only."""
    fluids, library = fluids or {}, {norm(k): v for k, v in (library or {}).items()}
    score_overrides = score_overrides or {}
    s = rules["settings"]
    for key, other in (rules.get("linked") or {}).items():      # RBI_Calc D59 = D11, unless the person gave a value of their own
        if blank(inputs.get(key)) and not blank(inputs.get(other)):
            inputs = {**inputs, key: inputs[other]}
    derived = derived_values(rules, inputs)
    mech = _mechanism_rows(rules, inputs, derived, library)
    dm = {"internal": _combine(mech, "Internal", s), "external": _combine(mech, "External", s)}
    entered = {m["location"] for m in mech if m.get("location") in ("Internal", "External")}

    factors = []
    for f in rules["factors"]:
        r = dict(_score_factor(rules, f, inputs, derived, fluids, dm))
        if f["id"] in score_overrides:
            r = _res(Decimal(str(score_overrides[f["id"]])), "scored", "Score typed in the workbook.")
        weight = Decimal(str(f["weight"]))
        counted = f.get("counted", True)                           # a factor its group's own sum leaves out adds nothing (see known_issues)
        factors.append({"id": f["id"], "label": f["label"], "group": f["group"], "weight": weight, "cui": False, "score": r["score"],
                        "status": r["status"], "message": r["message"], "applicable": True, "counted": counted,
                        "contribution": (weight * r["score"] / 100 if counted else Decimal(0)) if r["score"] is not None else None,
                        "from_data": f["kind"] == "derived_band"})

    groups = {g: sum((x["contribution"] for x in factors if x["group"] == g and x["contribution"] is not None), Decimal(0)).quantize(Q9)
              for g in GROUPS}
    cof = max(groups[g] for g in COF_NAMES)
    cof_group = next(COF_NAMES[g] for g in COF_NAMES if groups[g] == cof)                  # RBI_Calc D95: the first group that holds the largest
    t_act, t_min, t_nom = dec(inputs.get("t_actual")), dec(inputs.get("t_min")), dec(inputs.get("t_nominal"))
    at_minimum = t_act is not None and t_min is not None and t_act <= t_min
    override = bool(s["hard_override"] == 1 and s["override_applied"] == 1 and at_minimum)
    pof = Decimal(5) if override else groups["PT"]
    pof_group = "Wall at or below minimum" if override else "POF-3 Tube bundle"
    cof_cat, pof_cat = category_of(cof, rules["categories"]), category_of(pof, rules["categories"])
    cat_row = {c["category"]: c for c in rules["categories"]}
    level_label = rules["matrix"][str(pof_cat)][cof_cat - 1]
    policy = next(l for l in rules["levels"] if l["label"] == level_label)
    priority = rules["priority"][str(cof_cat)][pof_cat - 1]                                # RBI_Calc D105; None where the table has no column

    policy_years = Decimal(str(policy["max_interval_years"]))
    assess = to_date(inputs.get("assessment_date"))
    years = recommended_interval(s, policy_years, derived["remaining_life_internal"])
    planning = {"policy_interval_years": policy_years, "internal_interval_years": years, "external_interval_years": None,
                "internal": due_info(rules, assess, inputs.get("last_internal_inspection"), years),
                "external": {"last": None, "due": None, "status": None, "label": None},
                "required_action": policy["action"], "minimum_effectiveness": policy["min_effectiveness"]}
    nde_advice(rules, mech, policy)

    problems = []
    if t_act is None or t_act <= 0:
        problems.append("Latest measured thickness must be more than 0.")
    if t_min is None or t_min <= 0:
        problems.append("Required minimum thickness must be more than 0.")
    if t_min is not None and t_nom is not None and t_min > t_nom:
        problems.append("Required minimum thickness cannot exceed the nominal thickness.")
    rate = dec(inputs.get("internal_corrosion_rate"))
    if rate is None:
        problems.append("Internal corrosion rate is required.")
    elif rate < 0:
        problems.append("Internal corrosion rate cannot be negative.")
    total, plugged = dec(inputs.get("total_tubes")), dec(inputs.get("plugged_tubes"))
    if total is not None and total <= 0:
        problems.append("Total number of tubes must be more than 0.")
    if total is not None and plugged is not None and total > 0 and plugged > total:
        problems.append("Plugged tubes cannot exceed the total number of tubes.")
    in_service = to_date(inputs.get("in_service_date"))
    if in_service is not None and assess is not None and in_service > assess:
        problems.append("In-service date is after the assessment date.")
    warnings = []
    last = to_date(inputs.get("last_internal_inspection"))
    if last is not None and assess is not None and last > assess:
        problems.append("Last internal inspection date is after the assessment date.")
    elif last is not None and in_service is not None and last < in_service:
        warnings.append("Last internal inspection date is before the in-service date: check it.")
    for m in mech:
        if m.get("suggested_invalid"):
            problems.append(f"{m['name']}: suggested score must be from 1 to 5.")

    notes = []                                                       # what the person should see next to the result
    if at_minimum and not override:
        notes.append(f"Wall thickness at or below minimum. Measured {t_act.normalize():f} mm, required {t_min.normalize():f} mm. "
                     "POF is not adjusted for this: engineering review required.")
    if priority is None:
        notes.append(f"Inspection priority not defined. No priority is available for POF category {pof_cat}.")
    side_notes = [f"No {side.lower()} damage mechanism. {side} factor scored 1 (lowest)." for side in ("External", "Internal") if side not in entered]
    if not mech:
        side_notes = ["No damage mechanism added. Damage mechanism factors scored 1 (lowest)."]
    warnings += side_notes                                     # shown with the mechanisms ...
    notes += side_notes                                        # ... and next to the result, so it is not missed
    for m in mech:
        if m.get("flag"):
            warnings.append(f"{m['name']}: {m['flag']}")
    group_weights = {g: sum((x["weight"] for x in factors if x["group"] == g), Decimal(0)) for g in GROUPS}
    missing = [x["id"] for x in factors if x["status"] in ("missing", "invalid") and x["weight"] > 0 and x["counted"]]
    return {
        "model": "tube",
        "derived": derived, "factors": factors, "groups": groups,
        "pof_groups": {"tube": groups["PT"]},
        "sides": {"external": False, "internal": True, "by_mechanism": False},             # which inspection plans there are
        "cof": {"score": cof, "category": cof_cat, "label": cat_row[cof_cat]["cof_label"], "driving_group": cof_group},
        "pof": {"score": pof, "category": pof_cat, "label": cat_row[pof_cat]["pof_label"], "driving_group": pof_group, "override": override},
        "risk": {"level": level_label, "rank": policy["rank"], "number": pof_cat * cof_cat, "priority": priority},
        "planning": planning, "mechanisms": mech, "damage_mechanisms": mechanism_summary(s, mech, dm),
        "checks": {"weights_total_100": all(w == 100 for w in group_weights.values()), "group_weights": group_weights, "problems": problems,
                   "warnings": warnings, "side_notes": side_notes, "missing_factors": missing},
        "notes": notes,
        "complete": not missing and not problems,
        "rules_version": rules["version"],
    }
