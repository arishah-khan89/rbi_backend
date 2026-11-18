"""Risk assessment (RBI) of the components of an equipment, after the client's workbook (030-E-11- Shell.xlsx).

  Equipment -> Component (Shell, Head, Tube bundle ...) -> Assessment (revision 1, 2 ...)
  An assessment goes  draft -> finalized (risk completed, waits for an admin) -> approved (locked); an admin can send a finalized one
  back (with a reason). A newer revision supersedes the older one when it is approved. Nobody can approve what they finalized themselves.

What is stored is what the person chose or wrote. The scores, categories, risk level, priority and inspection plan are worked out by
app/rbi_engine from those inputs and the workbook's rules; when an assessment is finalized the result is kept with the version of the
rules, so an approved assessment always shows what was approved. Values the software knows (thicknesses and corrosion rates from the
inspection data, t_min from the equipment register) are taken from there when they exist: the person types them only when it has none.
Errors: ValueError -> 400, PermissionError -> 403, LookupError -> 404 (mapped by the endpoints).
"""
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core import audit
from app.core.config import settings
from app.crud import notification as notifications
from app.crud.approval import check_reviewer
from app.crud.cml import min_t_for_type
from app.models.cml import InspectionReading, InspectionTML
from app.models.equipment import Equipment
from app.models.rbi_assessment import RbiAssessment, RbiComponent, RbiFluidEntry, RbiMechanismEntry
from app.rbi_engine.engine import blank, dec, evaluate, exact, jsonable, load_rules, norm, to_date

COMPONENT_TYPES = ["Shell", "Head / Dish", "Tube bundle", "Roof", "Bottom", "Other"]
_GROUP = {"Shell": "shell", "Head / Dish": "head", "Tube bundle": "tube", "Roof": "roof", "Bottom": "bottom"}
_HEAD = ("dish", "head", "channel head", "dish / head")
_TUBE = ("tube", "tubes", "tube bundle")
_ROOF = ("roof", "fixed roof", "floating roof", "tank roof")
_BOTTOM = ("bottom", "tank bottom", "bottom plate")
_NOZZLE_LIKE = ("nozzle", "other")
STATES = ("draft", "finalized", "approved", "superseded")
RATE_KINDS_FROM_INSPECTION = ("controlling", "short term", "long term")

# True = the way the professional RBI software works: only the side (external / internal) of the damage mechanisms the person entered is asked and
# counted in POF = MAX(external, internal); to answer for both sides the person adds a mechanism of each side.
# False = the workbook's own rule: both sides always asked and counted, a side without a mechanism scored 1, and the person is told to answer for both.
# The workbook does not say which; the client has been asked (RBI_CLIENT_QUESTIONS.md, A1): change this one line with the answer.
SIDES_BY_MECHANISM = True

# inputs that cannot be negative / must be whole numbers (the workbook's own data validation says "decimal >= 0" for these)
_NEGATIVE_OK = {"operating_temperature", "operating_pressure", "cui_temperature"}
_WHOLE = {"external_inspections", "internal_inspections"}
_NOT_FROM_THE_PERSON = {"assessment_date"}                       # handled on its own


def _group_of(tml_type: Optional[str]) -> Optional[str]:
    t = (tml_type or "").strip().lower()
    if t in _NOZZLE_LIKE:
        return None
    return "head" if t in _HEAD else "tube" if t in _TUBE else "roof" if t in _ROOF else "bottom" if t in _BOTTOM else "shell"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    return v


# ------------------------------------------------------------------------------------------------ what the software knows
@dataclass
class Facts:
    tml_count: int = 0
    t_actual: Optional[Decimal] = None
    t_min: Optional[Decimal] = None
    governing_tml: Optional[str] = None
    last_reading_date: Optional[date] = None
    controlling_cr: Optional[Decimal] = None
    short_cr: Optional[Decimal] = None
    long_cr: Optional[Decimal] = None
    notes: List[str] = field(default_factory=list)


def _max(*values):
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _max_csv(text: Optional[str]) -> Optional[Decimal]:
    out = []
    for part in (text or "").split(","):
        try:
            out.append(Decimal(part.strip()))
        except InvalidOperation:
            continue
    return max(out) if out else None


def load_facts(db: Session, eq: Equipment, component_type: str) -> Facts:
    """The thickness locations of this kind of component: the governing one (the least wall left above t_min) gives t_actual and
    t_min; the corrosion rates are the highest of the latest readings."""
    facts = Facts()
    group = _GROUP.get(component_type)
    if group is None:
        return facts
    tmls = [t for t in db.query(InspectionTML).filter(InspectionTML.equipment_id == eq.id).all() if _group_of(t.tml_type) == group]
    facts.tml_count = len(tmls)
    if not tmls:
        return facts
    newest = (db.query(InspectionReading.tml_id.label("tml_id"), func.max(InspectionReading.inspection_date).label("d"))
              .filter(InspectionReading.tml_id.in_([t.id for t in tmls])).group_by(InspectionReading.tml_id).subquery())
    latest = {r.tml_id: r for r in db.query(InspectionReading).join(
        newest, and_(InspectionReading.tml_id == newest.c.tml_id, InspectionReading.inspection_date == newest.c.d)).all()}
    best = None                                                    # (allowance, thinnest, t_min, code)
    thinnest = None
    for t in tmls:
        r = latest.get(t.id)
        if r is None:
            continue
        values = [v for v in (r.reading_1, r.reading_2, r.reading_3, r.reading_4, r.reading_5, r.reading_6) if v is not None]
        if not values:
            continue
        low = min(values)
        t_min = min_t_for_type(eq, t.tml_type, t.min_required_thickness)
        if t_min is not None and t_min > 0:
            allowance = low - t_min
            if best is None or allowance < best[0]:
                best = (allowance, low, t_min, t.tml_code)
        if thinnest is None or low < thinnest:
            thinnest = low
        d = r.inspection_date.date() if isinstance(r.inspection_date, datetime) else r.inspection_date
        if d and (facts.last_reading_date is None or d > facts.last_reading_date):
            facts.last_reading_date = d
        facts.controlling_cr = _max(facts.controlling_cr, r.controlling_cr)
        facts.short_cr = _max(facts.short_cr, _max_csv(r.short_term_cr_csv))
        facts.long_cr = _max(facts.long_cr, _max_csv(r.long_term_cr_csv))
    if best is not None:
        facts.t_actual, facts.t_min, facts.governing_tml = best[1], best[2], best[3]
    elif thinnest is not None:
        facts.t_actual = thinnest
        facts.notes.append("The equipment register has no minimum required thickness for this kind of component.")
    return facts


def _nominal(eq: Equipment, component_type: str) -> Optional[Decimal]:
    return {"Shell": eq.nominal_thickness, "Head / Dish": eq.dish_nominal_thickness, "Tube bundle": eq.tube_thickness,
            "Roof": eq.roof_nominal_thickness, "Bottom": eq.bottom_nominal_thickness}.get(component_type)


def _fluid_names(db: Session, text: Optional[str]) -> Dict[str, Optional[str]]:
    """The fluid list entries that match the equipment register's fluid (same name, any capitals)."""
    out = {"toxic_fluid": None, "representative_fluid": None}
    key = (text or "").strip().lower()
    if not key:
        return out
    toxic = db.query(RbiFluidEntry).filter(func.lower(RbiFluidEntry.toxic_name) == key).first()
    flam = db.query(RbiFluidEntry).filter(func.lower(RbiFluidEntry.flammable_name) == key).first()
    out["toxic_fluid"] = toxic.toxic_name if toxic else None
    out["representative_fluid"] = flam.flammable_name if flam else None
    return out


def suggested_inputs(db: Session, eq: Equipment, comp: RbiComponent) -> Dict[str, Any]:
    """What a new assessment starts with: values the equipment register already has. They are suggestions the person can change."""
    out: Dict[str, Any] = {}
    start = comp.in_service_date or (eq.commissioning_date or eq.installation_date)
    if start:
        out["in_service_date"] = (start.date() if isinstance(start, datetime) else start).isoformat()
    if eq.expected_service_life:
        out["design_life_years"] = eq.expected_service_life
    nominal = _nominal(eq, comp.component_type)
    if nominal is not None:
        out["t_nominal"] = float(nominal)
    tube = comp.component_type == "Tube bundle"
    temperature = eq.tube_operating_temperature if tube else eq.operating_temperature
    pressure = eq.tube_operating_pressure if tube else eq.operating_pressure
    if temperature is not None:
        out["operating_temperature"] = float(temperature)
    if pressure is not None:
        out["operating_pressure"] = float(pressure)
    ins = (eq.has_insulation or "").strip().lower()
    if ins in ("yes", "no"):
        out["insulated"] = ins.capitalize()
    for k, v in _fluid_names(db, eq.fluid).items():
        if v:
            out[k] = v
    return out


# ------------------------------------------------------------------------------------------------ what the person writes
def _options(rules: dict) -> Dict[str, List[str]]:
    """The choices of every dropdown, taken from the score tables the rules were read from."""
    out: Dict[str, List[str]] = {}
    for f in rules["factors"]:
        if f["kind"] == "map":
            out[f["inputs"][0]] = [t["option"] for t in f["table"]]
        elif f["kind"] == "composite":
            out["lost_production_category"] = [t["option"] for t in f["lost_production_table"]]
    out["shutdown_required"] = out["insulated"] = ["Yes", "No"]
    out["internal_corrosion_rate_kind"] = list(rules["corrosion_rate_kinds"])
    return out


def _unique(options: List[str]) -> List[str]:
    return list(dict.fromkeys(options))


def clean_inputs(db: Session, rules: dict, raw: Dict[str, Any]) -> Dict[str, Any]:
    """What the person wrote, checked and tidied: only the workbook's inputs are kept; a wrong value is refused with a message that
    says what to correct (so everything stored is valid)."""
    options = _options(rules)
    meta = {i["key"]: i for i in rules["inputs"]}
    out: Dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key in ("mechanisms",) or key in _NOT_FROM_THE_PERSON:
            continue
        if key == "internal_corrosion_rate_kind":
            if blank(value):
                continue
            hit = next((o for o in options[key] if norm(o) == norm(value)), None)
            if hit is None:
                raise ValueError(f"'{value}' is not a choice for the internal corrosion rate (use {', '.join(options[key])}).")
            out[key] = hit
            continue
        item = meta.get(key)
        if item is None:
            continue
        label = item["label"] or key
        if blank(value):
            continue
        kind = item["kind"]
        if kind == "date":
            d = to_date(value)
            if d is None:
                raise ValueError(f"{label}: '{value}' is not a date (use the form year-month-day).")
            out[key] = d.isoformat()
        elif kind == "number":
            n = dec(value)
            if n is None:
                raise ValueError(f"{label}: '{value}' is not a number.")
            if n < 0 and key not in _NEGATIVE_OK:
                raise ValueError(f"{label}: cannot be negative.")
            if key in _WHOLE and n != n.to_integral_value():
                raise ValueError(f"{label}: must be a whole number.")
            out[key] = int(n) if key in _WHOLE else float(n)
        elif kind in ("yesno", "select"):
            hit = next((o for o in options.get(key, []) if norm(o) == norm(value)), None)
            if hit is None:
                raise ValueError(f"{label}: '{value}' is not one of the choices.")
            out[key] = hit
        elif kind == "fluid":
            col = RbiFluidEntry.toxic_name if key == "toxic_fluid" else RbiFluidEntry.flammable_name
            row = db.query(RbiFluidEntry).filter(func.lower(col) == str(value).strip().lower()).first()
            if row is None:
                raise ValueError(f"{label}: '{value}' is not in the fluid list.")
            out[key] = row.toxic_name if key == "toxic_fluid" else row.flammable_name
    out["mechanisms"] = _clean_mechanisms(db, rules, (raw or {}).get("mechanisms"))
    return out


def _clean_mechanisms(db: Session, rules: dict, rows: Any) -> List[dict]:
    if rows in (None, ""):
        return []
    if not isinstance(rows, list):
        raise ValueError("The damage mechanisms must be a list.")
    if len(rows) > 8:
        raise ValueError("At most 8 damage mechanisms can be entered.")
    eff = next(f for f in rules["factors"] if f["id"] == "P2.9")["table"]
    out, seen = [], set()
    for i, r in enumerate(rows, 1):
        name = (r or {}).get("name")
        if blank(name):
            continue
        entry = db.query(RbiMechanismEntry).filter(func.lower(RbiMechanismEntry.name) == str(name).strip().lower()).first()
        if entry is None:
            raise ValueError(f"Damage mechanism {i}: '{name}' is not in the damage-mechanism library.")
        if entry.name.lower() in seen:
            raise ValueError(f"The damage mechanism '{entry.name}' is entered twice.")
        seen.add(entry.name.lower())
        row: Dict[str, Any] = {"name": entry.name}
        rate = r.get("rate")
        if not blank(rate):
            n = dec(rate)
            if n is None or n < 0:
                raise ValueError(f"{entry.name}: the rate must be a number of 0 or more (mm/year).")
            row["rate"] = float(n)
        for key, label in (("susceptibility", "susceptibility"), ("suggested_score", "suggested score")):
            v = r.get(key)
            if not blank(v):
                n = dec(v)
                if n is None or not 1 <= n <= 5:
                    raise ValueError(f"{entry.name}: the {label} must be a number from 1 to 5.")
                row[key] = float(n)
        e = r.get("inspection_effectiveness")
        if not blank(e):
            hit = exact(eff, e)
            if hit is None:
                raise ValueError(f"{entry.name}: '{e}' is not one of the inspection effectiveness choices.")
            row["inspection_effectiveness"] = hit["option"]
        j = r.get("justification")
        if not blank(j):
            row["justification"] = str(j).strip()[:1000]
        out.append(row)
    return out


# ------------------------------------------------------------------------------------------------ working a result out
def _library(db: Session, names: List[str]) -> Dict[str, dict]:
    if not names:
        return {}
    rows = db.query(RbiMechanismEntry).filter(func.lower(RbiMechanismEntry.name).in_([n.lower() for n in names])).all()
    return {r.name.lower(): {"name": r.name, "location": r.location, "damage_class": r.damage_class, "rate_based": r.rate_based,
                             "primary_nde": r.primary_nde, "secondary_nde": r.secondary_nde} for r in rows}


def _fluid_row(db: Session, key: str, value: Any) -> Optional[dict]:
    if blank(value):
        return None
    col = RbiFluidEntry.toxic_name if key == "toxic_fluid" else RbiFluidEntry.flammable_name
    r = db.query(RbiFluidEntry).filter(func.lower(col) == str(value).strip().lower()).first()
    if r is None:
        return None
    return {"toxicity_concentration": r.toxicity_concentration, "flammability": r.flammability, "flash_point": r.flash_point,
            "unit": r.toxicity_unit}


def linked_inputs(rules: dict) -> Dict[str, str]:
    """{input: the input whose value it takes}: the workbook writes the link in the unit of an input ("°C (linked to C2.8)": C2.8 is the factor of the operating temperature)."""
    first_input = {f["id"]: f["inputs"][0] for f in rules["factors"] if f.get("inputs")}
    out: Dict[str, str] = {}
    for i in rules["inputs"]:
        m = re.search(r"linked to ([CP]\d+\.\d+)", i.get("unit") or "")
        if m and m.group(1) in first_input:
            out[i["key"]] = first_input[m.group(1)]
    return out


def effective_inputs(inputs: Dict[str, Any], facts: Facts, assessment_date: date) -> tuple:
    """(inputs used for the score, where each number came from). The software's own value wins for the thicknesses (inspection data /
    equipment register) and for an internal corrosion rate chosen as controlling, short term or long term."""
    eff = dict(inputs)
    eff["assessment_date"] = assessment_date.isoformat()
    source = {k: "person" for k in inputs}
    if facts.t_actual is not None:
        eff["t_actual"], source["t_actual"] = float(facts.t_actual), "inspection data"
    if facts.t_min is not None:
        eff["t_min"], source["t_min"] = float(facts.t_min), "equipment register"
    kind = norm(inputs.get("internal_corrosion_rate_kind"))
    rate = {"controlling": facts.controlling_cr, "short term": facts.short_cr, "long term": facts.long_cr}.get(kind)
    if rate is not None:
        eff["internal_corrosion_rate"], source["internal_corrosion_rate"] = float(rate), "inspection data"
    source["assessment_date"] = "person"
    # an input the workbook links to another ("°C (linked to C2.8)", Factor_Master "same value as C2.8") takes that value until the person types their own
    for key, other in linked_inputs(load_rules()).items():
        if blank(inputs.get(key)) and not blank(inputs.get(other)):
            eff[key] = inputs[other]
    return eff, source


def with_component_defaults(comp: RbiComponent, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """The component's date in service is the source of the in-service date: an assessment that has none of its own uses it (so a date
    saved on the component after the draft was started is not lost)."""
    if blank(inputs.get("in_service_date")) and comp.in_service_date:
        return {**inputs, "in_service_date": comp.in_service_date.isoformat()}
    return inputs


def analyse(db: Session, eq: Equipment, comp: RbiComponent, inputs: Dict[str, Any], assessment_date: date) -> dict:
    rules = load_rules()
    inputs = with_component_defaults(comp, inputs)
    facts = load_facts(db, eq, comp.component_type)
    eff, source = effective_inputs(inputs, facts, assessment_date)
    fluids = {"toxic": _fluid_row(db, "toxic_fluid", eff.get("toxic_fluid")), "flammable": _fluid_row(db, "representative_fluid", eff.get("representative_fluid"))}
    names = [m["name"] for m in (eff.get("mechanisms") or []) if m.get("name")]
    result = evaluate(rules, eff, fluids, _library(db, names), sides_by_mechanism=SIDES_BY_MECHANISM)
    return {"result": jsonable(result), "effective_inputs": eff, "sources": source, "facts": _facts_view(facts)}


def _facts_view(f: Facts) -> dict:
    return {"tml_count": f.tml_count, "t_actual": _f(f.t_actual), "t_min": _f(f.t_min), "governing_location": f.governing_tml,
            "last_reading_date": f.last_reading_date.isoformat() if f.last_reading_date else None,
            "controlling_cr": _f(f.controlling_cr), "short_cr": _f(f.short_cr), "long_cr": _f(f.long_cr), "notes": f.notes}


# ------------------------------------------------------------------------------------------------ lookups
def _equipment(db: Session, equipment_id) -> Equipment:
    eq = db.query(Equipment).filter(Equipment.id == equipment_id, Equipment.status != "DELETED").first()
    if eq is None:
        raise LookupError("Equipment not found")
    return eq


def _component(db: Session, component_id) -> RbiComponent:
    c = db.query(RbiComponent).filter(RbiComponent.id == component_id).first()
    if c is None:
        raise LookupError("Component not found")
    return c


def _assessment(db: Session, assessment_id) -> RbiAssessment:
    a = db.query(RbiAssessment).filter(RbiAssessment.id == assessment_id).first()
    if a is None:
        raise LookupError("Assessment not found")
    return a


def _brief(a: RbiAssessment) -> dict:
    """The line of an assessment in a list: its state and, once finalized, its risk."""
    res = (a.results or {}).get("result") or {}
    return {"id": str(a.id), "revision": a.revision, "state": a.state, "assessment_date": a.assessment_date.isoformat(),
            "risk_level": (res.get("risk") or {}).get("level"), "risk_rank": (res.get("risk") or {}).get("rank"),
            "priority": (res.get("risk") or {}).get("priority"), "pof_category": (res.get("pof") or {}).get("category"),
            "cof_category": (res.get("cof") or {}).get("category"), "created_by": a.created_by,
            "finalized_by": a.finalized_by, "approved_by": a.approved_by,
            "finalized_at": a.finalized_at.isoformat() if a.finalized_at else None, "approved_at": a.approved_at.isoformat() if a.approved_at else None}


def _effective(assessments: List[RbiAssessment]) -> Optional[RbiAssessment]:
    """The assessment that counts for the component's risk: the newest finalized or approved one."""
    ranked = [a for a in assessments if a.state in ("finalized", "approved")]
    return max(ranked, key=lambda a: a.revision) if ranked else None


def _by_component(db: Session, component_ids: List[Any]) -> Dict[Any, List[RbiAssessment]]:
    out: Dict[Any, List[RbiAssessment]] = {i: [] for i in component_ids}
    if component_ids:
        for a in db.query(RbiAssessment).filter(RbiAssessment.component_id.in_(component_ids)).order_by(RbiAssessment.revision).all():
            out[a.component_id].append(a)
    return out


def _highest(items: List[dict]) -> Optional[dict]:
    """The worst of the components: the highest risk level, then the most urgent inspection priority (1 is first)."""
    ranked = [i for i in items if i.get("risk_rank") is not None]
    return max(ranked, key=lambda i: (i["risk_rank"], -(i["priority"] or 99))) if ranked else None


def _component_line(c: RbiComponent, assessments: List[RbiAssessment]) -> dict:
    cur = _effective(assessments)
    latest = assessments[-1] if assessments else None
    line = {"id": str(c.id), "name": c.name, "component_type": c.component_type, "status": c.status,
            "assessments": len(assessments), "latest_state": latest.state if latest else None,
            "latest_revision": latest.revision if latest else None, "has_draft": any(a.state == "draft" for a in assessments),
            "awaiting_approval": any(a.state == "finalized" for a in assessments)}
    line.update({k: v for k, v in (_brief(cur) if cur else {"risk_level": None, "risk_rank": None, "priority": None,
                                                           "pof_category": None, "cof_category": None, "assessment_date": None}).items()
                 if k in ("risk_level", "risk_rank", "priority", "pof_category", "cof_category", "assessment_date")})
    line["counted_revision"] = cur.revision if cur else None
    line["counted_state"] = cur.state if cur else None
    return line


# ------------------------------------------------------------------------------------------------ equipment
def _equipment_head(eq: Equipment) -> dict:
    return {"id": str(eq.id), "tag": eq.equipment_id, "name": eq.equipment_name, "type": eq.equipment_type, "plant": eq.plant, "unit": eq.unit,
            "service": eq.service, "fluid": eq.fluid}


def _equipment_items(db: Session, rows: List[Equipment]) -> List[dict]:
    comps = db.query(RbiComponent).filter(RbiComponent.equipment_id.in_([e.id for e in rows])).all() if rows else []
    by_eq: Dict[Any, List[RbiComponent]] = {e.id: [] for e in rows}
    for c in comps:
        by_eq[c.equipment_id].append(c)
    assess = _by_component(db, [c.id for c in comps])
    items = []
    for e in rows:
        lines = [_component_line(c, assess[c.id]) for c in by_eq[e.id]]
        items.append({**_equipment_head(e), "components": len(lines), "assessed": sum(1 for l in lines if l["counted_revision"]),
                      "awaiting_approval": sum(1 for l in lines if l["awaiting_approval"]), "highest": _highest(lines)})
    return items


def _urgency(item: dict) -> tuple:
    """Most urgent first: the higher risk level, then the lower priority number (1 is first), then the higher consequence, then the older
    assessment; equipment with no counted assessment comes last. The order of ties is the project's own choice (the client's sheet has none)."""
    h = item.get("highest")
    if not h:
        return (1, 0, 0, 0, "", item["tag"] or "")
    return (0, -(h.get("risk_rank") or 0), h.get("priority") or 99, -(h.get("cof_category") or 0), str(h.get("assessment_date") or ""), item["tag"] or "")


def list_equipment(db: Session, q: Optional[str], equipment_type: Optional[str], page: int, page_size: int,
                   sort: str = "tag", risk_level: Optional[str] = None) -> dict:
    query = db.query(Equipment).filter(Equipment.status != "DELETED")
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(Equipment.equipment_id.ilike(like), Equipment.equipment_name.ilike(like)))
    if equipment_type:
        query = query.filter(Equipment.equipment_type == equipment_type)
    types = [t for (t,) in db.query(Equipment.equipment_type).filter(Equipment.status != "DELETED").distinct().order_by(Equipment.equipment_type).all() if t]
    if sort == "priority" or risk_level:
        # the risk of an equipment is worked out from its components, so the whole (filtered) list is worked out and then cut into pages
        items = _equipment_items(db, query.order_by(Equipment.equipment_id).all())
        if risk_level:
            wanted = "none" if risk_level == "none" else risk_level
            items = [i for i in items if (i["highest"]["risk_level"] if i["highest"] else "none") == wanted]
        if sort == "priority":
            items.sort(key=_urgency)
        total = len(items)
        items = items[(page - 1) * page_size: page * page_size]
    else:
        total = query.count()
        rows = query.order_by(Equipment.equipment_id).offset((page - 1) * page_size).limit(page_size).all()
        items = _equipment_items(db, rows)
    return {"items": items, "total": total, "page": page, "page_size": page_size, "equipment_types": types}


def get_equipment(db: Session, equipment_id) -> dict:
    eq = _equipment(db, equipment_id)
    comps = db.query(RbiComponent).filter(RbiComponent.equipment_id == eq.id).order_by(RbiComponent.name).all()
    assess = _by_component(db, [c.id for c in comps])
    lines = [_component_line(c, assess[c.id]) for c in comps]
    return {"equipment": _equipment_head(eq), "components": lines, "highest": _highest(lines), "component_types": COMPONENT_TYPES,
            "suggested_components": _suggested_components(db, eq, comps)}


def _suggested_components(db: Session, eq: Equipment, existing: List[RbiComponent]) -> List[dict]:
    """The parts the inspection data already knows (a Shell with thickness locations, a Dish ...) that have no component yet."""
    have = {c.component_type for c in existing}
    seen = {}
    for t in db.query(InspectionTML).filter(InspectionTML.equipment_id == eq.id).all():
        g = _group_of(t.tml_type)
        label = {"shell": "Shell", "head": "Head / Dish", "tube": "Tube bundle", "roof": "Roof", "bottom": "Bottom"}.get(g)
        if label and label not in have:
            seen[label] = seen.get(label, 0) + 1
    return [{"component_type": k, "name": f"{eq.equipment_id}-{_abbr(k)}", "thickness_locations": v} for k, v in seen.items()]


def _abbr(component_type: str) -> str:
    return {"Shell": "SH", "Head / Dish": "HD", "Tube bundle": "TB", "Roof": "RF", "Bottom": "BT", "Other": "OT"}.get(component_type, "CP")


# ------------------------------------------------------------------------------------------------ components
def _check_component_fields(db: Session, eq: Equipment, name: Any, component_type: Any, in_service: Any, exclude=None) -> tuple:
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("The component needs a name.")
    if len(name) > 120:
        raise ValueError("The component name is too long (120 characters at most).")
    if component_type not in COMPONENT_TYPES:
        raise ValueError(f"The component type must be one of: {', '.join(COMPONENT_TYPES)}.")
    q = db.query(RbiComponent).filter(RbiComponent.equipment_id == eq.id, func.lower(RbiComponent.name) == name.lower())
    if exclude is not None:
        q = q.filter(RbiComponent.id != exclude)
    if q.first() is not None:
        raise ValueError(f"This equipment already has a component called '{name}'.")
    d = None
    if not blank(in_service):
        d = to_date(in_service)
        if d is None:
            raise ValueError("The component's date in service is not a date.")
    return name, d


def create_component(db: Session, equipment_id, body: Dict[str, Any], actor: str) -> dict:
    eq = _equipment(db, equipment_id)
    name, d = _check_component_fields(db, eq, body.get("name"), body.get("component_type"), body.get("in_service_date"))
    comp = RbiComponent(equipment_id=eq.id, name=name, component_type=body["component_type"], in_service_date=d,
                        status="Active", description=(body.get("description") or None), comments=(body.get("comments") or None),
                        created_by=actor, updated_by=actor)
    db.add(comp)
    db.flush()
    audit.record(db, actor, "rbi.component.create", str(comp.id), f"{eq.equipment_id} | {comp.name} ({comp.component_type})")
    db.commit()
    return get_component(db, comp.id)


def update_component(db: Session, component_id, body: Dict[str, Any], actor: str) -> dict:
    comp = _component(db, component_id)
    eq = _equipment(db, comp.equipment_id)
    name, d = _check_component_fields(db, eq, body.get("name", comp.name), body.get("component_type", comp.component_type),
                                      body.get("in_service_date", comp.in_service_date), exclude=comp.id)
    if comp.component_type != body.get("component_type", comp.component_type):
        if db.query(RbiAssessment).filter(RbiAssessment.component_id == comp.id).count():
            raise ValueError("The type of a component with assessments cannot be changed: its inspection data would no longer match.")
    status = body.get("status", comp.status)
    if status not in ("Active", "Inactive"):
        raise ValueError("The status must be Active or Inactive.")
    comp.name, comp.component_type, comp.in_service_date, comp.status = name, body.get("component_type", comp.component_type), d, status
    comp.description, comp.comments, comp.updated_by = body.get("description", comp.description), body.get("comments", comp.comments), actor
    audit.record(db, actor, "rbi.component.edit", str(comp.id), f"{eq.equipment_id} | {comp.name}")
    db.commit()
    return get_component(db, comp.id)


def delete_component(db: Session, component_id, actor: str) -> None:
    comp = _component(db, component_id)
    if db.query(RbiAssessment).filter(RbiAssessment.component_id == comp.id, RbiAssessment.state.in_(("finalized", "approved", "superseded"))).count():
        raise ValueError("A component with a finalized or approved assessment cannot be deleted (the assessments are the record of the decision). Set it to Inactive instead.")
    eq = _equipment(db, comp.equipment_id)
    audit.record(db, actor, "rbi.component.delete", str(comp.id), f"{eq.equipment_id} | {comp.name}")
    db.delete(comp)
    db.commit()


def get_component(db: Session, component_id) -> dict:
    comp = _component(db, component_id)
    eq = _equipment(db, comp.equipment_id)
    rows = _by_component(db, [comp.id])[comp.id]
    facts = load_facts(db, eq, comp.component_type)
    return {"equipment": _equipment_head(eq),
            "component": {"id": str(comp.id), "name": comp.name, "component_type": comp.component_type, "status": comp.status,
                          "in_service_date": comp.in_service_date.isoformat() if comp.in_service_date else None,
                          "description": comp.description, "comments": comp.comments,
                          "functional_location": " / ".join(x for x in (eq.plant, eq.unit, eq.area) if x)},
            "summary": _component_line(comp, rows), "assessments": [_brief(a) for a in reversed(rows)],
            "facts": _facts_view(facts), "component_types": COMPONENT_TYPES}


# ------------------------------------------------------------------------------------------------ assessments
def _view(db: Session, a: RbiAssessment, live_inputs: Optional[Dict[str, Any]] = None, live_date: Optional[date] = None) -> dict:
    comp = _component(db, a.component_id)
    eq = _equipment(db, comp.equipment_id)
    rules = load_rules()
    locked = a.state != "draft"
    if locked and a.results:
        snap = a.results
        analysis = {"result": snap["result"], "effective_inputs": snap["effective_inputs"], "sources": snap["sources"], "facts": snap["facts"]}
    else:
        analysis = analyse(db, eq, comp, live_inputs if live_inputs is not None else (a.inputs or {}), live_date or a.assessment_date)
    inputs = live_inputs if live_inputs is not None else (a.inputs or {})
    if not locked:
        inputs = with_component_defaults(comp, inputs)
    return {"id": str(a.id), "revision": a.revision, "state": a.state, "assessment_date": (live_date or a.assessment_date).isoformat(),
            "comments": a.comments, "return_reason": a.return_reason, "inputs": inputs, **analysis,
            "equipment": _equipment_head(eq),
            "component": {"id": str(comp.id), "name": comp.name, "component_type": comp.component_type},
            "rules_version": a.rules_version or rules["version"], "rules_changed": bool(a.rules_version and a.rules_version != rules["version"]),
            "created_by": a.created_by, "updated_by": a.updated_by,
            "finalized_by": a.finalized_by, "finalized_at": a.finalized_at.isoformat() if a.finalized_at else None,
            "assigned_reviewer": a.assigned_reviewer, "submit_comment": a.submit_comment,
            "approved_by": a.approved_by, "approved_at": a.approved_at.isoformat() if a.approved_at else None,
            "history": [_brief(x) for x in reversed(_by_component(db, [comp.id])[comp.id])]}


def new_assessment(db: Session, component_id, actor: str) -> dict:
    comp = _component(db, component_id)
    eq = _equipment(db, comp.equipment_id)
    rows = _by_component(db, [comp.id])[comp.id]
    if any(a.state == "draft" for a in rows):
        raise ValueError("This component already has a draft assessment: continue it, or delete it first.")
    if any(a.state == "finalized" for a in rows):
        raise ValueError("This component has an assessment waiting for approval: an admin approves or returns it first.")
    inputs = dict(rows[-1].inputs) if rows else suggested_inputs(db, eq, comp)
    inputs.pop("assessment_date", None)
    revision = (rows[-1].revision + 1) if rows else 1
    a = RbiAssessment(component_id=comp.id, revision=revision, state="draft", assessment_date=date.today(), inputs=clean_inputs(db, load_rules(), inputs),
                      created_by=actor, updated_by=actor)
    db.add(a)
    db.flush()
    audit.record(db, actor, "rbi.assessment.create", str(a.id), f"{eq.equipment_id} | {comp.name} | revision {revision}")
    db.commit()
    return _view(db, a)


def get_assessment(db: Session, assessment_id) -> dict:
    return _view(db, _assessment(db, assessment_id))


def _draft(db: Session, assessment_id) -> RbiAssessment:
    a = _assessment(db, assessment_id)
    if a.state != "draft":
        raise ValueError(f"This assessment is {a.state} and cannot be changed. Start a new revision to change it.")
    return a


def _when(body: Dict[str, Any], a: RbiAssessment) -> date:
    raw = body.get("assessment_date")
    if blank(raw):
        return a.assessment_date
    d = to_date(raw)
    if d is None:
        raise ValueError("The assessment date is not a date.")
    return d


def preview(db: Session, assessment_id, body: Dict[str, Any]) -> dict:
    """The result of what is typed so far; nothing is saved."""
    a = _draft(db, assessment_id)
    return _view(db, a, clean_inputs(db, load_rules(), body.get("inputs") or {}), _when(body, a))


def save_assessment(db: Session, assessment_id, body: Dict[str, Any], actor: str) -> dict:
    a = _draft(db, assessment_id)
    a.inputs = clean_inputs(db, load_rules(), body.get("inputs") or {})
    a.assessment_date = _when(body, a)
    if "comments" in body:
        a.comments = (body.get("comments") or "").strip() or None
    a.updated_by = actor
    db.commit()
    return _view(db, a)


def delete_assessment(db: Session, assessment_id, actor: str) -> None:
    a = _draft(db, assessment_id)
    comp = _component(db, a.component_id)
    eq = _equipment(db, comp.equipment_id)
    audit.record(db, actor, "rbi.assessment.delete", str(a.id), f"{eq.equipment_id} | {comp.name} | revision {a.revision}")
    db.delete(a)
    db.commit()


def finalize(db: Session, assessment_id, actor: str, approver: Optional[str] = None, comment: Optional[str] = None) -> dict:
    """Risk completed: the result is worked out once more, must be complete, and is kept with the version of the rules.
    It is sent to ONE admin (as an inspection report is): only that admin approves or returns it."""
    a = _draft(db, assessment_id)
    comp = _component(db, a.component_id)
    eq = _equipment(db, comp.equipment_id)
    analysis = analyse(db, eq, comp, a.inputs or {}, a.assessment_date)
    check = analysis["result"]["checks"]
    problems = list(check["problems"])
    rules = load_rules()
    labels = {f["id"]: f["label"] for f in rules["factors"]}
    if check["missing_factors"]:
        problems.append("Still to fill in: " + ", ".join(labels[i] for i in check["missing_factors"]) + ".")
    if not check["weights_total_100"]:
        problems.append("The weights of the rules do not total 100 in every group: ask for the rules to be checked.")
    if any(m["status"] in ("invalid", "missing") for m in analysis["result"]["mechanisms"]):
        problems.append("A damage mechanism is incomplete (see its message).")
    unjustified = [m["name"] for m in analysis["result"]["mechanisms"] if (m.get("flag") or "").startswith(("Override below calculated: justification MISSING", "Justification missing"))]
    if unjustified:
        problems.append("A justification is needed for the suggested score of: " + ", ".join(unjustified) + ".")
    if problems:
        raise ValueError("This assessment is not complete. " + " ".join(problems))
    reviewer = check_reviewer(db, approver, actor) if settings.AUTH_REQUIRED else None       # without a login there is nobody to choose
    note = str(comment or "").strip()[:1000] or None
    a.results = {"result": analysis["result"], "effective_inputs": analysis["effective_inputs"], "sources": analysis["sources"],
                 "facts": analysis["facts"]}
    a.rules_version, a.state, a.return_reason = rules["version"], "finalized", None
    a.finalized_by, a.finalized_at, a.updated_by = actor, _now(), actor
    a.assigned_reviewer, a.submit_comment = (reviewer.username if reviewer else None), note
    res = analysis["result"]
    audit.record(db, actor, "rbi.assessment.finalize", str(a.id),
                 f"{eq.equipment_id} | {comp.name} | revision {a.revision} | {res['risk']['level']} (POF {res['pof']['category']}, COF {res['cof']['category']})"
                 + (f" -> {reviewer.username}" if reviewer else "") + (f" | {note}" if note else ""))
    if reviewer:
        notifications.notify(db, reviewer.username, "risk_assigned", f"{actor} sent you {_what(eq, comp, a)} for approval", note, _Ref(a, eq), actor)
    db.commit()
    return _view(db, a)


class _Ref:
    """What a notification needs to point at: the assessment (the link goes there) and its equipment."""

    def __init__(self, a: RbiAssessment, eq):
        self.id, self.equipment_id = a.id, eq.id


def _what(eq, comp: RbiComponent, a: RbiAssessment) -> str:
    return f"the risk assessment of {eq.equipment_id} ({comp.name}, revision {a.revision})"


def _decide_guard(a: RbiAssessment, actor: str, what: str) -> None:
    """Only the admin it was sent to decides (and never the person who finalized it)."""
    if settings.AUTH_REQUIRED and a.assigned_reviewer and actor.lower() != a.assigned_reviewer.lower():
        raise PermissionError(f"Only {a.assigned_reviewer} can {what} this assessment: it was sent to that admin.")
    _different_person(actor, a.finalized_by, what)


def _different_person(actor: str, other: Optional[str], what: str) -> None:
    if settings.AUTH_REQUIRED and other and actor.lower() == other.lower():
        raise PermissionError(f"You cannot {what} an assessment that you finalized yourself. Another admin does this.")


def approve(db: Session, assessment_id, actor: str) -> dict:
    a = _assessment(db, assessment_id)
    if a.state != "finalized":
        raise ValueError(f"Only a finalized assessment can be approved (this one is {a.state}).")
    _decide_guard(a, actor, "approve")
    comp = _component(db, a.component_id)
    eq = _equipment(db, comp.equipment_id)
    for older in db.query(RbiAssessment).filter(RbiAssessment.component_id == comp.id, RbiAssessment.state == "approved",
                                                RbiAssessment.id != a.id).all():
        older.state = "superseded"
    a.state, a.approved_by, a.approved_at, a.updated_by = "approved", actor, _now(), actor
    audit.record(db, actor, "rbi.assessment.approve", str(a.id), f"{eq.equipment_id} | {comp.name} | revision {a.revision}")
    notifications.notify(db, a.finalized_by, "risk_approved", f"{actor} approved {_what(eq, comp, a)}", None, _Ref(a, eq), actor)
    db.commit()
    return _view(db, a)


def return_assessment(db: Session, assessment_id, reason: Optional[str], actor: str) -> dict:
    a = _assessment(db, assessment_id)
    if a.state != "finalized":
        raise ValueError(f"Only a finalized assessment can be returned (this one is {a.state}).")
    if blank(reason):
        raise ValueError("Say why the assessment is returned: the person who finalized it will read it.")
    _decide_guard(a, actor, "return")
    comp = _component(db, a.component_id)
    eq = _equipment(db, comp.equipment_id)
    finalizer = a.finalized_by
    a.state, a.return_reason, a.results, a.updated_by = "draft", str(reason).strip()[:1000], None, actor
    a.finalized_at = a.finalized_by = a.rules_version = a.assigned_reviewer = a.submit_comment = None
    audit.record(db, actor, "rbi.assessment.return", str(a.id), f"{eq.equipment_id} | {comp.name} | revision {a.revision} | {a.return_reason}")
    notifications.notify(db, finalizer, "risk_returned", f"{actor} returned {_what(eq, comp, a)} to you", a.return_reason, _Ref(a, eq), actor)
    db.commit()
    return _view(db, a)


def withdraw(db: Session, assessment_id, actor: str, role: str) -> dict:
    """The person who finalized it takes it back while it waits (the admin it was sent to is not available, or it was sent by mistake)."""
    a = _assessment(db, assessment_id)
    if a.state != "finalized":
        raise ValueError("Only an assessment that is waiting for approval can be withdrawn.")
    if settings.AUTH_REQUIRED and not ((a.finalized_by or "").lower() == actor.lower() or role == "super_admin"):
        raise PermissionError("Only the person who sent it (or a super admin) can withdraw it.")
    comp = _component(db, a.component_id)
    eq = _equipment(db, comp.equipment_id)
    reviewer = a.assigned_reviewer
    a.state, a.results, a.updated_by = "draft", None, actor
    a.finalized_at = a.finalized_by = a.rules_version = a.assigned_reviewer = a.submit_comment = None
    audit.record(db, actor, "rbi.assessment.withdraw", str(a.id), f"{eq.equipment_id} | {comp.name} | revision {a.revision}")
    notifications.notify(db, reviewer, "risk_withdrawn", f"{actor} withdrew {_what(eq, comp, a)}: it no longer needs your decision", None, _Ref(a, eq), actor)
    db.commit()
    return _view(db, a)


# ------------------------------------------------------------------------------------------------ the whole plant
def _counted(db: Session) -> List[dict]:
    """The assessment that counts for each component (newest finalized or approved), with where it is."""
    comps = db.query(RbiComponent).all()
    assess = _by_component(db, [c.id for c in comps])
    eqs = {e.id: e for e in db.query(Equipment).filter(Equipment.id.in_({c.equipment_id for c in comps}), Equipment.status != "DELETED").all()} if comps else {}
    out = []
    for c in comps:
        cur = _effective(assess[c.id])
        eq = eqs.get(c.equipment_id)
        if cur is None or eq is None:                              # nothing finalized yet, or the equipment was deleted
            continue
        brief = _brief(cur)
        out.append({**brief, "component_id": str(c.id), "component": c.name, "component_type": c.component_type,
                    "equipment_id": str(c.equipment_id), "equipment_tag": eq.equipment_id if eq else None,
                    "equipment_name": eq.equipment_name if eq else None})
    return out


def risk_matrix(db: Session) -> dict:
    rules = load_rules()
    items = _counted(db)
    cells: Dict[str, dict] = {}
    for pof in range(1, 6):
        for cof in range(1, 6):
            cells[f"{pof}-{cof}"] = {"pof": pof, "cof": cof, "level": rules["matrix"][str(pof)][cof - 1], "count": 0, "components": []}
    for i in items:
        if i["pof_category"] and i["cof_category"]:
            cell = cells[f"{i['pof_category']}-{i['cof_category']}"]
            cell["count"] += 1
            cell["components"].append({k: i[k] for k in ("component_id", "component", "equipment_id", "equipment_tag", "priority", "state", "revision")})
    return {"cells": list(cells.values()), "levels": rules["levels"], "total": len(items)}


def summary(db: Session) -> dict:
    rules = load_rules()
    items = _counted(db)
    by_level = {l["label"]: 0 for l in rules["levels"]}
    for i in items:
        if i["risk_level"] in by_level:
            by_level[i["risk_level"]] += 1
    top = sorted(items, key=lambda i: (i["priority"] or 99, -(i["risk_rank"] or 0)))[:5]
    totals = {"components": db.query(RbiComponent).count(),
              "drafts": db.query(RbiAssessment).filter(RbiAssessment.state == "draft").count(),
              "awaiting_approval": db.query(RbiAssessment).filter(RbiAssessment.state == "finalized").count(),
              "approved": db.query(RbiAssessment).filter(RbiAssessment.state == "approved").count()}
    return {"totals": totals, "counted": len(items), "by_level": [{"level": l["label"], "colour": l["colour"], "count": by_level[l["label"]]} for l in rules["levels"]],
            "most_urgent": top, "rules_version": rules["version"]}


# ------------------------------------------------------------------------------------------------ for the screens
def form_description() -> dict:
    rules = load_rules()
    options = _options(rules)
    inputs = []
    for i in rules["inputs"]:
        item = dict(i)
        if i["key"] in options:
            item["options"] = _unique(options[i["key"]])
        inputs.append(item)
    inputs.insert(next(n for n, i in enumerate(inputs) if i["key"] == "internal_corrosion_rate"), {
        "key": "internal_corrosion_rate_kind", "kind": "select", "section": "equipment", "row": 15, "label": "Internal corrosion rate: which one",
        "unit": None, "note": "Controlling, short term and long term come from the inspection data; for Expected you type the rate.",
        "options": options["internal_corrosion_rate_kind"], "source": None})
    factors = [{k: f[k] for k in ("id", "side", "group", "label", "weight", "cui", "kind", "inputs", "scale", "note")} for f in rules["factors"]]
    return {"version": rules["version"], "source_file": rules["source_file"], "sections": rules["sections"], "inputs": inputs, "factors": factors,
            "derived": rules["derived"], "levels": rules["levels"], "matrix": rules["matrix"], "categories": rules["categories"],
            "priority": rules["priority"], "shape": rules["shape"], "settings": rules["settings"], "known_issues": rules["known_issues"],
            "effectiveness_options": [t["option"] for t in next(f for f in rules["factors"] if f["id"] == "P2.9")["table"]],
            "max_mechanisms": 8, "component_types": COMPONENT_TYPES}


def list_fluids(db: Session, q: Optional[str], kind: str, limit: int) -> List[dict]:
    col = RbiFluidEntry.toxic_name if kind == "toxic" else RbiFluidEntry.flammable_name
    query = db.query(RbiFluidEntry).filter(col.isnot(None))
    if q:
        query = query.filter(or_(col.ilike(f"%{q.strip()}%"), RbiFluidEntry.cas_number.ilike(f"%{q.strip()}%")))
    out = []
    for r in query.order_by(col).limit(limit).all():
        out.append({"name": r.toxic_name if kind == "toxic" else r.flammable_name, "cas_number": r.cas_number,
                    "toxicity_concentration": _f(r.toxicity_concentration), "toxicity_unit": r.toxicity_unit,
                    "flammability": r.flammability, "flash_point": r.flash_point})
    return out


def list_mechanisms(db: Session, location: Optional[str], q: Optional[str]) -> List[dict]:
    query = db.query(RbiMechanismEntry)
    if location:
        query = query.filter(RbiMechanismEntry.location == location)
    if q:
        query = query.filter(or_(RbiMechanismEntry.name.ilike(f"%{q.strip()}%"), RbiMechanismEntry.damage_class.ilike(f"%{q.strip()}%")))
    return [{"code": r.code, "name": r.name, "location": r.location, "damage_class": r.damage_class, "rate_based": r.rate_based,
             "typical_drivers": r.typical_drivers, "primary_nde": r.primary_nde, "secondary_nde": r.secondary_nde,
             "mitigation": r.mitigation, "definition": r.definition, "applicable_materials": r.applicable_materials,
             "min_temperature_c": _f(r.min_temperature_c), "max_temperature_c": _f(r.max_temperature_c),
             "process_streams": r.process_streams} for r in query.order_by(RbiMechanismEntry.sort_order).all()]
