"""Scores one RBI analysis with the rules read from the client's workbook (rules.json). No number of the client's is written here.

The steps are the ones of the workbook's Algorithm sheet:
  1 inputs  2 derived values  3 raw value -> score 1..5  4 weight x score / 100  5 group scores  6 POF external (CUI switch)
  7 COF = largest of 3 groups, POF = larger of external / internal, hard override (wall at or below t_min -> POF 5)
  8 score -> category  9 risk level from the 5x5 matrix, inspection priority  10 inspection planning  11 validation
and the damage-mechanism register (DM_Register): up to 8 mechanisms, each with its own probability, combined into one score for the
internal side and one for the external side.

Nothing is guessed: an input that is empty or outside a table gets no score and is reported (status "missing" / "invalid"); the
analysis is then "not complete" and says what is still needed.
"""
import json
import math
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

RULES_PATH = Path(__file__).with_name("rules.json")
_cache: Dict[str, Any] = {}
Q9 = Decimal("0.000000001")


def load_rules(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path or RULES_PATH)
    stamp = p.stat().st_mtime_ns
    hit = _cache.get(str(p))
    if hit and hit[0] == stamp:
        return hit[1]
    rules = json.loads(p.read_text(encoding="utf-8"))
    _cache[str(p)] = (stamp, rules)
    return rules


# ------------------------------------------------------------------------------------------------ small helpers
def dec(v: Any) -> Optional[Decimal]:
    """A number from what was typed (blank or text that is not a number gives None)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
    try:
        s = str(v).strip().replace(",", "")
        return Decimal(s) if s else None
    except InvalidOperation:
        return None


def to_date(v: Any) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip()[:10]) if v not in (None, "") else None
    except ValueError:
        return None


def norm(v: Any) -> str:
    return " ".join(str(v).split()).lower() if v is not None else ""


def blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def exact(options: List[dict], value: Any) -> Optional[dict]:
    """MATCH(value, options, 0): text compared without caring about capitals or extra spaces; a number matches the same number."""
    key = norm(value)
    for row in options:
        if norm(row["option"]) == key:
            return row
    return None


def approx(lowers: List[Any], x: Decimal) -> Optional[int]:
    """MATCH(x, lowers, 1) the way Excel does it (a binary search for the last value that is not above x). For a table in ascending
    order that is the last lower bound <= x; None when x is below the first. A table that is not ascending gives what Excel gives."""
    lo, hi, found = 0, len(lowers) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if Decimal(str(lowers[mid])) <= x:
            found, lo = mid, mid + 1
        else:
            hi = mid - 1
    return found


def band(table: List[dict], x: Decimal) -> Optional[Decimal]:
    i = approx([t["lower"] for t in table], x)
    return None if i is None else Decimal(str(table[i]["score"]))


def q9(x: float) -> Decimal:
    return Decimal(repr(round(x, 9))).quantize(Q9)


def round_half_up(x: Decimal, places: int) -> Decimal:
    return x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def category_of(score: Decimal, categories: List[dict]) -> int:
    i = approx([c["lower"] for c in categories], score)
    return int(categories[0 if i is None else i]["category"])


def _res(score, status, message=None) -> dict:
    return {"score": score, "status": status, "message": message}


# ------------------------------------------------------------------------------------------------ derived values (step 2)
def derived_values(rules: dict, inputs: dict) -> Dict[str, Any]:
    unlimited = Decimal(str(rules["settings"]["unlimited_life_years"]))
    assess, start = to_date(inputs.get("assessment_date")), to_date(inputs.get("in_service_date"))
    life = dec(inputs.get("design_life_years"))
    t_act, t_min = dec(inputs.get("t_actual")), dec(inputs.get("t_min"))
    out: Dict[str, Any] = {}
    age = (Decimal((assess - start).days) / Decimal("365.25")) if assess and start else None
    out["age_years"] = age
    out["component_life_ratio"] = (age / life) if age is not None and life is not None and life > 0 else None
    allowance = (t_act - t_min) if t_act is not None and t_min is not None else None
    out["remaining_allowance"] = allowance
    out["wall_ratio"] = (t_min / t_act) if t_act is not None and t_min is not None and t_act > 0 else None

    def remaining_life(rate_key: str):
        rate = dec(inputs.get(rate_key))
        if allowance is None or rate is None:
            return None
        if allowance <= 0:
            return Decimal(0)
        if rate <= 0:
            return unlimited
        return allowance / rate

    out["remaining_life_external"] = remaining_life("external_corrosion_rate")
    out["remaining_life_internal"] = remaining_life("internal_corrosion_rate")
    last = to_date(inputs.get("last_internal_inspection"))
    out["years_since_internal_inspection"] = (Decimal((assess - last).days) / Decimal("365.25")) if assess and last else None
    return out


# ------------------------------------------------------------------------------------------------ damage mechanisms
def _mechanism_rows(rules: dict, inputs: dict, derived: dict, library: Dict[str, dict]) -> List[dict]:
    s = rules["settings"]
    unlimited = Decimal(str(s["unlimited_life_years"]))
    f_by_id = {f["id"]: f for f in rules["factors"]}
    life_table, eff_table = f_by_id["P1.11"]["table"], f_by_id["P2.9"]["table"]
    allowance = derived.get("remaining_allowance")
    anchor, step = float(s["dm_anchor_frequency"]), float(s["dm_decades_per_step"])
    wd, wi = Decimal(str(s["dm_weight_damage"])), Decimal(str(s["dm_weight_inspection"]))
    rows = []
    for raw in (inputs.get("mechanisms") or [])[:8]:
        name = raw.get("name")
        if blank(name):
            continue
        row: Dict[str, Any] = {"name": name, "messages": []}
        entry = library.get(norm(name))
        if entry is None:
            row.update(status="invalid", location=None, klass=None, rate_based=None, messages=[f"'{name}' is not in the damage-mechanism library."])
            rows.append(row)
            continue
        loc = entry.get("location")
        row.update(location=loc, klass=entry.get("damage_class"), rate_based=entry.get("rate_based"),
                   primary_nde=entry.get("primary_nde"), secondary_nde=entry.get("secondary_nde"), status="scored")
        rate_based = norm(entry.get("rate_based")) == "yes"
        # the rate of a rate-based mechanism: its own, else the component's external / internal rate (as the sheet links its first two rows)
        rate = dec(raw.get("rate"))
        if rate_based and rate is None:
            rate = dec(inputs.get("external_corrosion_rate" if loc == "External" else "internal_corrosion_rate"))
        row["rate"] = rate
        remaining = None
        if rate_based and rate is not None and allowance is not None:
            remaining = Decimal(0) if allowance <= 0 else (unlimited if rate <= 0 else min(unlimited, allowance / rate))
        row["remaining_life"] = remaining
        if rate_based:
            damage = band(life_table, remaining) if remaining is not None else None
            if damage is None:
                row["messages"].append("Needs a corrosion rate and the thicknesses to work out the remaining life.")
        else:
            damage = dec(raw.get("susceptibility"))
            if damage is None or not (1 <= damage <= 5):
                damage = None
                row["messages"].append("Needs a susceptibility from 1 to 5.")
        row["damage_score"] = damage
        eff_text = raw.get("inspection_effectiveness")
        hit = exact(eff_table, eff_text) if not blank(eff_text) else None
        row["effectiveness"] = eff_text if hit else None
        row["effectiveness_score"] = Decimal(str(hit["score"])) if hit else Decimal(5)       # no inspection = ineffective (the sheet's rule)
        if blank(eff_text):
            row["messages"].append("No inspection effectiveness chosen: counted as E (none).")
        calc = ((wd * damage + wi * row["effectiveness_score"]) / (wd + wi)) if damage is not None else None
        row["calculated_score"] = calc
        suggested = dec(raw.get("suggested_score"))
        if suggested is not None and not (1 <= suggested <= 5):
            suggested = None
            row["suggested_invalid"] = True
            row["messages"].append("A suggested score must be between 1 and 5.")
        row["suggested_score"] = suggested
        row["justification"] = raw.get("justification")
        used = suggested if suggested is not None else calc
        row["score_used"] = used
        row["frequency"] = (anchor * 10 ** ((float(used) - 1) * step)) if used is not None else 0.0
        flag = ""
        if suggested is not None and calc is not None and suggested < calc:
            flag = ("Override below calculated: justification MISSING" if blank(raw.get("justification"))
                    else "Override below calculated: justification must be approved")
        elif suggested is not None and blank(raw.get("justification")):
            flag = "Justification missing"
        row["flag"] = flag
        if used is None:
            row["status"] = "missing"
        rows.append(row)
    return rows


def _combine(rows: List[dict], location: str, settings: dict) -> Decimal:
    """DM_Register F22 / F23: the chance that at least one mechanism of that side acts, turned back into a score 1..5."""
    anchor, step = float(settings["dm_anchor_frequency"]), float(settings["dm_decades_per_step"])
    product = 1.0
    for r in rows:
        if r.get("score_used") is not None and r.get("location") == location:
            product *= 1 - r["frequency"]
    f = 1 - product
    if f <= 0:
        return Decimal(1)
    return q9(min(5.0, max(1.0, 1 + math.log10(f / anchor) / step)))


# ------------------------------------------------------------------------------------------------ the scoring of one factor
# the answers a worked-out value is made from: when it is blank, one of these is missing (they are given under Analysis details)
DERIVED_FROM = {
    "component_life_ratio": ["in_service_date", "design_life_years", "assessment_date"],
    "wall_ratio": ["t_actual", "t_min"],
    "remaining_life_external": ["t_actual", "t_min", "external_corrosion_rate"],
    "remaining_life_internal": ["t_actual", "t_min", "internal_corrosion_rate"],
    "years_since_internal_inspection": ["assessment_date", "last_internal_inspection"],
}


def _needs(rules: dict, inputs: dict, key: str) -> str:
    """"needs In-service date, Design life": the blank answers a worked-out value is made from."""
    labels = {i["key"]: i["label"] for i in rules["inputs"]}
    blanks = [labels.get(k, k) for k in DERIVED_FROM[key] if blank(inputs.get(k))]
    return "needs " + ", ".join(blanks) if blanks else "cannot be worked out from the data given"


def _score_factor(rules: dict, f: dict, inputs: dict, derived: dict, fluids: dict, dm: dict) -> dict:
    kind, fid = f["kind"], f["id"]

    def numeric(key: str) -> tuple:
        """(number, None) or (None, the result to give back): a worked-out value comes from `derived`, anything else from the inputs."""
        raw = derived.get(key) if key in derived else inputs.get(key)
        v = dec(raw)
        if v is None:
            if blank(raw) and key in DERIVED_FROM:
                return None, _res(None, "missing", _needs(rules, inputs, key))
            return None, _res(None, "missing" if blank(raw) else "invalid", None if blank(raw) else f"'{raw}' is not a number.")
        return v, None

    if kind in ("band", "derived_band"):
        key = f["inputs"][0]
        value, problem = numeric(key)
        if problem:
            if "blank_score" in f and problem["status"] == "missing":
                return _res(Decimal(str(f["blank_score"])), "scored", "No date: counted as never inspected.")
            return problem
        s = band(f["table"], value)
        if s is not None:
            return _res(s, "scored")
        if value < 0 and key in ("component_life_ratio", "years_since_internal_inspection"):
            return _res(None, "invalid", "negative, because a date is later than the assessment date")
        return _res(None, "invalid", f"{value.normalize():f} is below the first row of the table.")
    if kind == "map":
        raw = inputs.get(f["inputs"][0])
        if blank(raw):
            return _res(None, "missing")
        hit = exact(f["table"], raw)
        return _res(Decimal(str(hit["score"])), "scored") if hit else _res(None, "invalid", f"'{raw}' is not one of the choices.")
    if kind == "switch":
        raw = inputs.get("insulated")
        if blank(raw):
            return _res(None, "missing")
        return _res(Decimal(1 if norm(raw) == "yes" else 0), "scored")
    if kind == "composite":
        shut = inputs.get("shutdown_required")
        if blank(shut):
            return _res(None, "missing")
        if norm(shut) == "no":                                    # "if no shutdown, the production-loss score is 1" (Factor_Master C1.1)
            return _res(Decimal(1), "scored")
        days, problem = numeric("downtime_days")
        if problem:
            return problem
        down = band(f["downtime_table"], days)
        cat_raw = inputs.get("lost_production_category")
        if blank(cat_raw):
            return _res(None, "missing")
        hit = exact(f["lost_production_table"], cat_raw)
        if down is None or hit is None:
            return _res(None, "invalid", "The downtime or the lost-production category is not in the table.")
        return _res(max(down, Decimal(str(hit["score"]))), "scored")
    if kind == "financial":
        parts = [dec(inputs.get(k)) for k in ("inventory_kg", "cost_per_kg", "repair_cost")]
        if any(blank(inputs.get(k)) for k in ("inventory_kg", "cost_per_kg", "repair_cost")):
            return _res(None, "missing")
        if any(p is None for p in parts):
            return _res(None, "invalid", "Inventory, cost per kg and repair cost must be numbers.")
        cost = parts[0] * parts[1] + parts[2]
        s = band(f["table"], cost)
        return _res(s, "scored") if s is not None else _res(None, "invalid", "The cost is below the first row of the table.")
    if kind == "toxic":
        if blank(inputs.get("toxic_fluid")):
            return _res(None, "missing")
        fl = fluids.get("toxic")
        if fl is None:
            return _res(None, "invalid", "This fluid is not in the fluid list.")
        conc = dec(fl.get("toxicity_concentration"))
        if conc is None:
            return _res(None, "invalid", "The fluid list has no toxicity concentration for this fluid.")
        if conc == 0:
            return _res(Decimal(0), "scored")
        s = band(f["table"], conc)
        return _res(s, "scored") if s is not None else _res(None, "invalid", "The concentration is below the first row of the table.")
    if kind == "flammability":
        if blank(inputs.get("representative_fluid")):
            return _res(None, "missing")
        fl = fluids.get("flammable")
        if fl is None:
            return _res(None, "invalid", "This fluid is not in the fluid list.")
        rating = dec(fl.get("flammability"))
        if rating is None:
            return _res(None, "invalid", "The fluid list has no flammability rating for this fluid.")
        if not 0 <= rating <= 4:                                   # the rule is "NFPA rating (0 to 4) + 1", a score from 1 to 5
            return _res(None, "invalid", f"The fluid list gives this fluid a flammability of {rating.normalize():f}: the rule needs an NFPA rating from 0 to 4.")
        return _res(rating + 1, "scored")
    if kind == "flash":
        if blank(inputs.get("representative_fluid")):
            return _res(None, "missing")
        fl = fluids.get("flammable")
        if fl is None:
            return _res(None, "invalid", "This fluid is not in the fluid list.")
        flash_raw = fl.get("flash_point")
        if isinstance(flash_raw, str) and flash_raw.strip().upper() in ("N/A", "NA"):
            return _res(Decimal(0), "scored", "No flash point for this fluid.")
        flash, temp = dec(flash_raw), dec(inputs.get("operating_temperature"))
        if flash is None:
            return _res(None, "invalid", "The fluid list has no flash point for this fluid.")
        if temp is None:
            return _res(None, "missing", "Needs the operating temperature.")
        # The sheet (RBI_Calc F48) tests "flash point <= 1.05 x temperature" and so on. For a temperature above 0 that is "the flash point is
        # at most 5 % (10 %, ...) of the temperature above it", and the line below gives exactly the same limit. A plain multiplication
        # would turn the other way for 0 or below (-20 x 1.25 is lower than -20 x 1.05), so the margin is taken from the size of the
        # temperature: the limit is temperature + (factor - 1) x |temperature|.
        for step in f["steps"]:
            if flash <= temp + (Decimal(str(step["up_to_times_temperature"])) - 1) * abs(temp):
                return _res(Decimal(step["score"]), "scored")
        return _res(Decimal(0), "scored")
    if kind == "dm":
        return _res(dm["external" if f["location"] == "External" else "internal"], "scored")
    raise ValueError(f"unknown factor kind {kind}")


# ------------------------------------------------------------------------------------------------ the whole analysis
def evaluate(rules: Dict[str, Any], inputs: Dict[str, Any], fluids: Optional[Dict[str, Any]] = None,
             library: Optional[Dict[str, dict]] = None, weight_overrides: Optional[Dict[str, Any]] = None,
             score_overrides: Optional[Dict[str, Any]] = None, sides_by_mechanism: bool = False) -> Dict[str, Any]:
    """`inputs`: key -> what the person wrote or the software filled in. `fluids`: {"toxic": row, "flammable": row} of the fluid list
    (concentration / flammability rating / flash point) or None. `library`: damage-mechanism library by lower-case name.
    The two override arguments exist to prove the engine against the workbook's own typed values (tests); the software never uses them.
    `sides_by_mechanism`: the way the professional RBI software works. Only the side (external / internal) of a damage mechanism the person has entered is asked
    and counted in the probability; the other side is left out of POF = MAX(external, internal). The workbook itself always counts both (False, the default)."""
    fluids, library = fluids or {}, {norm(k): v for k, v in (library or {}).items()}
    weight_overrides, score_overrides = weight_overrides or {}, score_overrides or {}
    s = rules["settings"]
    derived = derived_values(rules, inputs)
    mech = _mechanism_rows(rules, inputs, derived, library)
    dm = {"internal": _combine(mech, "Internal", s), "external": _combine(mech, "External", s)}
    entered = {m["location"] for m in mech if m.get("location") in ("Internal", "External")}
    active = {"PE": "External" in entered or not sides_by_mechanism, "PI": "Internal" in entered or not sides_by_mechanism}

    factors, by_id = [], {}
    for f in rules["factors"]:
        r = dict(_score_factor(rules, f, inputs, derived, fluids, dm))
        if f["id"] in score_overrides:
            r = _res(Decimal(str(score_overrides[f["id"]])), "scored", "Score typed in the workbook.")
        weight = Decimal(str(weight_overrides.get(f["id"], f["weight"])))
        contribution = (weight * r["score"] / 100) if r["score"] is not None and f["id"] != "P1.0" else None
        item = {"id": f["id"], "label": f["label"], "group": f["group"], "weight": weight, "cui": f["cui"], "score": r["score"],
                "status": r["status"], "message": r["message"], "contribution": contribution, "applicable": active.get(f["group"], True),
                "from_data": f["kind"] == "derived_band"}
        factors.append(item)
        by_id[f["id"]] = item

    def total(group: str, *, cui: Optional[bool] = None) -> Decimal:
        return sum((x["contribution"] for x in factors if x["group"] == group and x["contribution"] is not None
                    and (cui is None or x["cui"] == cui)), Decimal(0))

    groups = {"C1": total("C1"), "C2": total("C2"), "C3": total("C3"), "PI": total("PI")}
    flag = by_id["P1.0"]["score"]
    cui_sum, non_cui_sum = total("PE", cui=True), total("PE", cui=False)
    non_cui_weights = sum((x["weight"] for x in factors if x["group"] == "PE" and not x["cui"]), Decimal(0)) / 100
    if s["normalise_uninsulated"] == 1 and (flag or 0) == 0:
        groups["PE"] = (non_cui_sum / non_cui_weights) if non_cui_weights > 0 else Decimal(0)
    else:
        groups["PE"] = (flag or Decimal(0)) * cui_sum + non_cui_sum
    groups = {k: v.quantize(Q9) for k, v in groups.items()}

    cof = max(groups["C1"], groups["C2"], groups["C3"])
    cof_group = "COF-1 Production & financial" if cof == groups["C1"] else "COF-2 Process-fluid hazard" if cof == groups["C2"] \
        else "COF-3 Reputation / environment / personnel"
    t_act, t_min = dec(inputs.get("t_actual")), dec(inputs.get("t_min"))
    override = bool(s["hard_override"] == 1 and t_act is not None and t_min is not None and t_act <= t_min)
    live = [g for g in ("PE", "PI") if active[g]]
    pof = Decimal(5) if override else max((groups[g] for g in live), default=Decimal(0))
    if override:
        pof_group = "Override: wall at / below t_min"
    elif not live:
        pof_group = "—"
    else:
        pof_group = "POF-1 External" if "PE" in live and ("PI" not in live or groups["PE"] >= groups["PI"]) else "POF-2 Internal"
    cof_cat, pof_cat = category_of(cof, rules["categories"]), category_of(pof, rules["categories"])
    cat_row = {c["category"]: c for c in rules["categories"]}
    level_label = rules["matrix"][str(pof_cat)][cof_cat - 1]
    policy = next(l for l in rules["levels"] if l["label"] == level_label)
    prio_row, prio_col = (cof_cat, pof_cat) if rules["shape"]["priority_rows"] == "cof" else (pof_cat, cof_cat)
    priority = rules["priority"][str(prio_row)][prio_col - 1]

    # inspection planning (step 10)
    unlimited, fraction = Decimal(str(s["unlimited_life_years"])), Decimal(str(s["interval_fraction"]))
    policy_years = Decimal(str(policy["max_interval_years"]))

    def interval(life: Optional[Decimal]) -> Optional[Decimal]:
        if life is None:
            return None
        return round_half_up(policy_years if life >= unlimited else min(policy_years, fraction * life), 1)

    assess = to_date(inputs.get("assessment_date"))

    def due(last_key: str, years: Optional[Decimal]) -> dict:
        last = to_date(inputs.get(last_key))
        if last is None:
            return {"last": None, "due": None, "status": "INSPECT NOW", "label": "Not inspected – inspect now"}
        if years is None or assess is None:
            return {"last": last, "due": None, "status": None, "label": None}
        days = int(round_half_up(years * Decimal("365.25"), 0))
        d = last + timedelta(days=days)
        status = "OVERDUE" if d < assess else "Due within 6 months" if (d - assess).days <= rules["shape"]["due_window_days"] else "OK"
        return {"last": last, "due": d, "status": status, "label": None}

    int_years, ext_years = interval(derived["remaining_life_internal"]), interval(derived["remaining_life_external"])
    planning = {"policy_interval_years": policy_years, "internal_interval_years": int_years, "external_interval_years": ext_years,
                "internal": due("last_internal_inspection", int_years), "external": due("last_external_inspection", ext_years),
                "required_action": policy["action"], "minimum_effectiveness": policy["min_effectiveness"]}

    # NDE recommendation per mechanism (DM_Register column S)
    eff_table = next(f for f in rules["factors"] if f["id"] == "P2.9")["table"]
    need = exact(eff_table, policy["min_effectiveness"])
    need_score = Decimal(str(need["score"])) if need else None
    need_letter = (policy["min_effectiveness"] or "")[:1]
    for m in mech:
        if m.get("score_used") is None and m["status"] != "invalid":
            m["recommendation"] = None
            continue
        if m["status"] == "invalid":
            m["recommendation"] = None
            continue
        letter = (m["effectiveness"] or "none")[:1] if m["effectiveness"] else "none"
        upgrade = need_score is not None and m["effectiveness_score"] > need_score
        m["upgrade"] = upgrade
        m["recommendation"] = (f"UPGRADE – use {m.get('primary_nde')} (current effectiveness {letter} is below required {need_letter}); "
                               f"add {m.get('secondary_nde')} as complementary method") if upgrade else \
            f"MAINTAIN – {m.get('primary_nde')} at the policy interval; complementary: {m.get('secondary_nde')}"
    scored_rows = [m for m in mech if m.get("score_used") is not None]
    dm_summary = {
        "entered": len(mech), "credible": sum(1 for m in scored_rows if m["score_used"] >= Decimal(str(s["dm_credible_from"]))),
        "internal_score": dm["internal"], "external_score": dm["external"],
        "governing": (max(scored_rows, key=lambda m: m["score_used"])["name"] if scored_rows else None),
        "highest_score": max((m["score_used"] for m in scored_rows), default=Decimal(1)),
        "needing_upgrade": sum(1 for m in mech if m.get("upgrade")),
        "overrides_below_calculated": sum(1 for m in mech if (m.get("flag") or "").startswith("Override")),
        "rows_with_missing_inputs": sum(1 for m in mech if m["status"] == "missing"),
    }

    # validation (step 11)
    problems = []
    if t_act is None or t_act <= 0:
        problems.append("The latest measured thickness (t_actual) must be more than 0.")
    if t_min is None or t_min <= 0:
        problems.append("The required minimum thickness (t_min) must be more than 0.")
    t_nom = dec(inputs.get("t_nominal"))
    if t_min is not None and t_nom is not None and t_min > t_nom:
        problems.append("t_min cannot be more than the nominal thickness.")
    for key, label in (("external_corrosion_rate", "external corrosion rate"), ("internal_corrosion_rate", "internal corrosion rate")):
        v = dec(inputs.get(key))
        if v is not None and v < 0:
            problems.append(f"The {label} cannot be negative.")
    in_service = to_date(inputs.get("in_service_date"))
    if in_service is not None and assess is not None and in_service > assess:
        problems.append("The in-service date is after the assessment date.")                      # Algorithm step 11: dates <= assessment date
    inspection_before_service = []
    for key, label in (("last_internal_inspection", "last internal inspection"), ("last_external_inspection", "last external inspection")):
        d = to_date(inputs.get(key))
        if d is not None and assess is not None and d > assess:
            problems.append(f"The date of the {label} is after the assessment date.")
        elif d is not None and in_service is not None and d < in_service:
            inspection_before_service.append(label)
    for m in mech:
        if m.get("suggested_invalid"):                                   # it is not silently ignored: the analysis is not complete until it is corrected or removed
            problems.append(f"{m['name']}: the score you give must be a number from 1 to 5.")
    group_weights = {g: sum((x["weight"] for x in factors if x["group"] == g), Decimal(0)) for g in groups}
    weights_ok = all(w == 100 for w in group_weights.values())
    # With "Insulated? = No" the corrosion-under-insulation sub-group is multiplied by 0 (RBI_Calc D59): its answers are not needed.
    cui_off = by_id["P1.0"]["score"] is not None and by_id["P1.0"]["score"] == 0
    missing = [x["id"] for x in factors if x["status"] in ("missing", "invalid") and (x["weight"] > 0 or x["id"] == "P1.0") and not (cui_off and x["cui"])
               and x["applicable"]]
    warnings = [f"The date of the {label} is before the in-service date: please check it." for label in inspection_before_service]
    if sides_by_mechanism and not live:
        problems.append("Add at least one damage mechanism (internal or external): only the questions of the damage mechanisms you add are asked.")
    elif not mech:
        warnings.append("No damage mechanism has been entered: the damage-mechanism factors are scored 1 (the lowest).")
    for m in mech:
        if m.get("flag"):
            warnings.append(f"{m['name']}: {m['flag']}")
    # The workbook counts both sides in POF = MAX(external, internal): a side without a damage mechanism is scored 1, so the person is told to answer for both.
    side_notes = [] if sides_by_mechanism else [
        f"No {side.lower()} damage mechanism has been entered. The workbook counts the external and the internal side together: add the {side.lower()} "
        f"mechanisms that apply, or its damage-mechanism factor is scored 1 (the lowest)."
        for side in ("External", "Internal") if side not in entered]

    return {
        "derived": derived, "factors": factors, "groups": groups,
        "pof_groups": {"external": groups["PE"], "internal": groups["PI"]},
        "sides": {"external": active["PE"], "internal": active["PI"], "by_mechanism": sides_by_mechanism},
        "cof": {"score": cof, "category": cof_cat, "label": cat_row[cof_cat]["cof_label"], "driving_group": cof_group},
        "pof": {"score": pof, "category": pof_cat, "label": cat_row[pof_cat]["pof_label"], "driving_group": pof_group, "override": override},
        "risk": {"level": level_label, "rank": policy["rank"], "number": pof_cat * cof_cat, "priority": priority},
        "planning": planning, "mechanisms": mech, "damage_mechanisms": dm_summary,
        "checks": {"weights_total_100": weights_ok, "group_weights": group_weights, "problems": problems, "warnings": warnings,
                   "side_notes": side_notes, "missing_factors": missing},
        "complete": not missing and not problems,
        "rules_version": rules["version"],
    }


# ------------------------------------------------------------------------------------------------ for the API / the database
def jsonable(value: Any) -> Any:
    """Decimals become numbers (6 decimals), dates become text: the result can be stored as JSON and sent as it is."""
    if isinstance(value, Decimal):
        return float(round(value, 6))
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value
