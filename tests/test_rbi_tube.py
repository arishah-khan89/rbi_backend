"""The tube bundle analysis (rules_tube.json / engine_tube): against the numbers Excel stored in the client's tube bundle workbook, and
the cases the example row does not reach. No database. Run from backend/:  python -m unittest tests.test_rbi_tube -v
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.rbi_engine import engine_tube  # noqa: E402
from app.rbi_engine.engine import TUBE_RULES_PATH, jsonable, load_rules  # noqa: E402

SHEET = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "RBI Logics -Tube Bundles.xlsx"))
needs_sheet = unittest.skipUnless(os.path.exists(SHEET), "the client's tube bundle workbook is not next to the project")
RULES = load_rules(TUBE_RULES_PATH)
LIBRARY = {n: {"name": n, "location": loc, "damage_class": "Thinning", "rate_based": rb, "primary_nde": "ECT", "secondary_nde": "IRIS"}
           for n, loc, rb in (("Inside", "Internal", "Yes"), ("Outside", "External", "No"))}
FLUIDS = {"toxic": {"toxicity_concentration": 2}, "flammable": {"flammability": 2, "flash_point": "39"}}


def inputs(**change):
    """Every answer given: the example of the workbook's RBI_Calc, with two simple mechanisms."""
    base = {"assessment_date": "2026-11-03", "in_service_date": "2020-05-01", "design_life_years": 25, "tube_operating_pressure": 35,
            "shell_operating_pressure": 6, "tube_design_pressure": 45, "shell_design_pressure": 10, "total_tubes": 400, "plugged_tubes": 12,
            "t_nominal": 2.11, "t_actual": 1.88, "t_min": 1, "internal_corrosion_rate": 0.2, "last_internal_inspection": "2026-06-15",
            "shutdown_required": "Yes", "downtime_days": 10, "lost_production_category": "3", "inventory_kg": 20000, "cost_per_kg": 90,
            "repair_cost": 300000, "toxic_fluid": "A", "toxic_percentage": 10, "initial_fluid_phase": "Non-volatile liquid / solid",
            "detection_time": 45, "isolation_time": 45, "ignition_distance": 15, "representative_fluid": "B", "operating_temperature": 40,
            "reputation": "Negligible Impact", "environmental": "High Impact", "workforce_persons": 7,
            "tube_leak_class": "Both sides non-hazardous (water / air / steam)", "coating_quality": "Damaged", "corrosion_trend": "Stable",
            "internal_corrosion_type": "Mixed uniform + localised", "operation_data_status": "Partial records", "internal_inspections": 2,
            "internal_inspection_confidence": "B – Usually effective", "service_status": "In service", "fiv_fouling": "Negligible Impact",
            "mechanisms": [{"name": "Inside", "inspection_effectiveness": "B – Usually effective"},
                           {"name": "Outside", "susceptibility": 3, "inspection_effectiveness": "C – Fairly effective"}]}
    base["rupture_protection"] = next(f for f in RULES["factors"] if f["id"] == "C4.4")["table"][0]["option"]
    base.update(change)
    return base


def run(**change):
    return jsonable(engine_tube.evaluate(RULES, inputs(**change), FLUIDS, LIBRARY))


def factor(result, fid):
    return next(f for f in result["factors"] if f["id"] == fid)


class TheRules(unittest.TestCase):
    def test_four_consequence_groups_and_one_probability_group_each_with_100_percent(self):
        for group in ("C1", "C2", "C3", "C4", "PT"):
            self.assertEqual(sum(f["weight"] for f in RULES["factors"] if f["group"] == group), 100, group)
        self.assertEqual(len(RULES["factors"]), 36)

    def test_the_sheets_own_list_of_mechanisms_is_kept(self):
        self.assertEqual(len(RULES["mechanism_names"]), 33)
        self.assertIn("CO2 corrosion", RULES["mechanism_names"])

    def test_what_looks_wrong_in_the_sheet_is_listed_for_the_client(self):
        text = " ".join(RULES["known_issues"])
        for part in ("C2.10", "t_actual ÷ t_min", "Thickness override", "category 5", "#REF!", "DM -TUBE side", "Factor_Master"):
            self.assertIn(part, text)


@needs_sheet
class AgainstTheWorkbook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import openpyxl
        cls.wb = openpyxl.load_workbook(SHEET, data_only=True)

    def test_the_rules_file_is_what_the_workbook_gives(self):
        from app.rbi_engine.rules_builder_tube import build_rules
        self.assertEqual(build_rules(SHEET)["version"], RULES["version"], "run rules_builder_tube --apply")

    def test_the_example_row_gives_the_numbers_excel_stored(self):
        c, dm, fl = self.wb["RBI_Calc"], self.wb["DM -SHell"], self.wb["list of fluids"]
        given = {}
        for i in RULES["inputs"]:
            v = c.cell(i["row"], 4).value
            if v is not None:
                given[i["key"]] = v.date().isoformat() if hasattr(v, "date") else str(v) if i["kind"] in ("select", "yesno") else v
        library, rows = {}, []
        for r in range(6, 14):
            name = dm.cell(r, 2).value
            if not name:
                continue
            library[name] = {"name": name, "location": dm.cell(r, 3).value, "damage_class": dm.cell(r, 4).value, "rate_based": dm.cell(r, 5).value,
                             "primary_nde": "x", "secondary_nde": "y"}
            row = {"name": name, "rate": dm.cell(r, 6).value, "susceptibility": dm.cell(r, 8).value, "inspection_effectiveness": dm.cell(r, 10).value,
                   "suggested_score": dm.cell(r, 13).value, "justification": dm.cell(r, 14).value}
            rows.append({k: v for k, v in row.items() if v not in (None, "")})
        given["mechanisms"] = rows
        given.pop("operating_pressure", None)               # RBI_Calc D59 is the formula =D11: the engine takes the tube-side value
        find = lambda col, name: next(r for r in range(2, fl.max_row + 1) if str(fl.cell(r, col).value).strip() == str(name).strip())
        t, f = find(1, given["toxic_fluid"]), find(2, given["representative_fluid"])
        fluids = {"toxic": {"toxicity_concentration": fl.cell(t, 5).value},
                  "flammable": {"flammability": fl.cell(f, 6).value, "flash_point": fl.cell(f, 10).value}}
        res = jsonable(engine_tube.evaluate(RULES, given, fluids, library))

        self.assertTrue(res["complete"], res["checks"])
        for x in res["factors"]:
            row = next(r for r in range(1, c.max_row + 1) if c.cell(r, 1).value == x["id"])
            if x["id"] == "P2.6":                                  # corrected: t_min ÷ t_actual (1 / 1.88 = 0.53 scores 2; the sheet 5)
                self.assertEqual((x["score"], c.cell(row, 6).value), (2, 5))
                continue
            self.assertAlmostEqual(x["score"], float(c.cell(row, 6).value), places=5, msg=x["id"])
            if x["counted"]:
                self.assertAlmostEqual(x["contribution"], float(c.cell(row, 7).value), places=5, msg=x["id"])
        # deliberate differences: the flash point (row 60) is added to COF-2, and the wall ratio P2.6 is the right way up (weight 8)
        expected = {g: c.cell(row, 7).value for g, row in (("C1", 47), ("C2", 61), ("C3", 66), ("C4", 72), ("PT", 92))}
        expected["C2"] += c["G60"].value
        expected["PT"] += c["C80"].value * (2 - c["F80"].value) / 100
        for group, value in expected.items():
            self.assertAlmostEqual(res["groups"][group], value, places=5, msg=group)
        self.assertAlmostEqual(res["cof"]["score"], max(expected[g] for g in ("C1", "C2", "C3", "C4")), places=5)
        self.assertAlmostEqual(res["pof"]["score"], expected["PT"], places=5)
        self.assertEqual((res["cof"]["category"], res["pof"]["category"], res["risk"]["level"], res["risk"]["number"], res["risk"]["priority"]),
                         (c["D96"].value, c["D101"].value, c["D103"].value, c["D104"].value, c["D105"].value))
        self.assertAlmostEqual(res["planning"]["internal_interval_years"], c["D110"].value, places=5)
        self.assertEqual(res["planning"]["internal"]["due"], c["D111"].value.date().isoformat())
        self.assertEqual(res["planning"]["internal"]["status"], c["D112"].value)
        for d in RULES["derived"]:
            sheet = c.cell(d["row"], 4).value
            if d["key"] == "wall_ratio":
                sheet = 1 / sheet
            if isinstance(sheet, (int, float)):
                self.assertAlmostEqual(res["derived"][d["key"]], sheet, places=5, msg=d["key"])
            else:
                self.assertEqual(res["derived"][d["key"]], sheet, d["key"])


class TheEngine(unittest.TestCase):
    def test_a_side_without_a_mechanism_is_said_with_the_result(self):
        self.assertFalse([n for n in run()["notes"] if "damage mechanism" in n.lower()])
        only_inside = run(mechanisms=[{"name": "Inside", "inspection_effectiveness": "B – Usually effective"}])
        self.assertEqual(factor(only_inside, "P1.8")["score"], 1)
        for place in (only_inside["notes"], only_inside["checks"]["warnings"]):
            self.assertTrue(any(n.startswith("No external damage mechanism") for n in place), place)
        none = run(mechanisms=[])
        self.assertEqual([n for n in none["notes"] if "damage mechanism" in n.lower()],
                         ["No damage mechanism added. Damage mechanism factors scored 1 (lowest)."])

    def test_operating_pressure_is_the_tube_side_one_until_another_is_given(self):
        self.assertEqual(factor(run(), "C2.9")["score"], 3)                        # tube side 35 barg
        self.assertEqual(factor(run(operating_pressure=""), "C2.9")["score"], 3)
        self.assertEqual(factor(run(operating_pressure=6), "C2.9")["score"], 2)    # the person's own value
        self.assertEqual(factor(run(operating_pressure=6), "C4.1")["score"], 3)    # the pressure difference is not touched by it

    def test_a_complete_answer_gives_a_result_and_only_an_internal_plan(self):
        r = run()
        self.assertTrue(r["complete"], r["checks"])
        self.assertEqual(r["model"], "tube")
        self.assertEqual(r["sides"], {"external": False, "internal": True, "by_mechanism": False})
        self.assertIsNone(r["planning"]["external_interval_years"])
        self.assertEqual(r["pof"]["score"], r["groups"]["PT"])
        self.assertEqual(r["cof"]["score"], max(r["groups"][g] for g in ("C1", "C2", "C3", "C4")))

    def test_the_flash_point_is_added_to_the_fluid_hazard_group_as_in_the_shell(self):
        r = run()
        flash = factor(r, "C2.10")
        self.assertEqual((flash["counted"], flash["contribution"]), (True, 0.2))
        self.assertTrue(any("G61" in issue and "flash point" in issue for issue in RULES["known_issues"]))
        self.assertAlmostEqual(r["groups"]["C2"], sum(f["contribution"] for f in r["factors"] if f["group"] == "C2"), places=5)

    def test_derived_tube_values(self):
        d = run()["derived"]
        self.assertEqual((d["pressure_differential"], d["higher_pressure_side"], d["design_pressure_hp"], d["design_pressure_lp"]),
                         (29, "Tube side", 45, 10))
        self.assertTrue(d["rule_10_13"].startswith("NOT satisfied"))
        self.assertAlmostEqual(d["plugged_pct"], 3)
        self.assertAlmostEqual(d["remaining_life_internal"], 4.4, places=5)
        swapped = run(tube_operating_pressure=6, shell_operating_pressure=35)["derived"]
        self.assertEqual((swapped["higher_pressure_side"], swapped["design_pressure_hp"], swapped["design_pressure_lp"]), ("Shell side", 10, 45))
        self.assertTrue(swapped["rule_10_13"].startswith("Satisfied"))

    def test_wall_at_the_minimum_makes_the_probability_5(self):
        r = run(t_actual=1.0)
        self.assertEqual((r["pof"]["override"], r["pof"]["score"], r["pof"]["category"], r["pof"]["driving_group"]), (True, 5, 5, "Wall at or below minimum"))
        self.assertIsNotNone(r["risk"]["priority"])
        self.assertFalse([n for n in r["notes"] if n.startswith(("Wall thickness", "Inspection priority"))], r["notes"])
        self.assertEqual(r["derived"]["remaining_life_internal"], 0)
        self.assertEqual(r["planning"]["internal_interval_years"], 0)

    def test_missing_and_wrong_answers_are_said(self):
        r = run(tube_design_pressure=None, plugged_tubes=500)
        self.assertFalse(r["complete"])
        self.assertIn("C4.2", r["checks"]["missing_factors"])
        self.assertTrue(any("Plugged tubes cannot exceed" in p for p in r["checks"]["problems"]))
        self.assertEqual(factor(r, "C4.2")["message"], "needs Tube-side design pressure")
        r = run(internal_corrosion_rate=None)
        self.assertTrue(any("Internal corrosion rate is required" in p for p in r["checks"]["problems"]))

    def test_a_thinner_wall_never_scores_better(self):
        scores = [factor(run(t_actual=t), "P2.6")["score"] for t in (2.11, 1.9, 1.5, 1.2, 1.1, 1.01)]
        self.assertEqual(scores, sorted(scores))
        self.assertLess(scores[0], scores[-1])

    def test_probability_category_5_has_the_priority_of_the_shell_sheet(self):
        r = jsonable(engine_tube.evaluate(RULES, inputs(), FLUIDS, LIBRARY,
                                          score_overrides={f["id"]: 5 for f in RULES["factors"] if f["group"] == "PT"}))
        self.assertEqual(r["pof"]["category"], 5)
        self.assertEqual(r["risk"]["priority"], {5: 1, 4: 3, 3: 5, 2: 10, 1: 12}[r["cof"]["category"]])
        self.assertEqual([RULES["priority"][k][4] for k in "54321"], [1, 3, 5, 10, 12])


if __name__ == "__main__":
    unittest.main()
