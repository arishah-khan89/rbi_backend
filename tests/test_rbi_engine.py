"""The RBI engine against the client's workbook (030-E-11- Shell.xlsx): the example of RBI_Calc and the six Test_Cases.

Parity first: with the weights and the one typed score that RBI_Calc itself used in its example, the engine must give the same
number as Excel stored, cell by cell. Then the rules the software really uses (weights from Factor_Master, flammability worked out)
are checked to differ from the example in exactly the documented ways.
"""
import unittest
from datetime import date
from decimal import Decimal

from app.rbi_engine.engine import approx, band, derived_values, evaluate, exact, load_rules
from app.rbi_engine.rules_builder import RulesError, build_rules
from tests.rbi_sheet_helpers import FACTOR_ROWS, SHEET_FILE, Workbook, needs_sheet

RULES = load_rules()
WORKBOOK_NORMALISE = 0          # Config!B6 of the client's workbook; the software switches it on (SETTING_CHOICES in rules_builder)


def close(a, b, places=6):
    return abs(float(a) - float(b)) < 10 ** -places


@needs_sheet
class ParityWithRbiCalc(unittest.TestCase):
    """The workbook's own example row, with the weights and typed score the example used."""

    @classmethod
    def setUpClass(cls):
        cls.wb = Workbook()
        cls.inputs = cls.wb.example_inputs()
        cls.fluids = cls.wb.fluids(cls.inputs)
        cls.library = cls.wb.library()
        # the example's own weights; RBI_Calc leaves row 89 (P2.12) out of its POF-internal sum (G91 = SUM(G78:G88))
        cls.out = evaluate(RULES, cls.inputs, cls.fluids, cls.library, weight_overrides={**cls.wb.sheet_weights(), "P2.12": 0},
                           score_overrides={"C2.7": cls.wb.cached("F45")})
        cls.by_id = {f["id"]: f for f in cls.out["factors"]}

    def test_the_fluids_and_mechanisms_were_found(self):
        self.assertIsNotNone(self.fluids["toxic"])
        self.assertIsNotNone(self.fluids["flammable"])
        self.assertEqual(len(self.out["mechanisms"]), 2)
        self.assertTrue(all(m["status"] == "scored" for m in self.out["mechanisms"]))

    def test_derived_values(self):
        d, wb = self.out["derived"], self.wb
        for key, row in (("age_years", 19), ("component_life_ratio", 20), ("remaining_allowance", 21), ("wall_ratio", 22),
                         ("remaining_life_external", 23), ("remaining_life_internal", 24), ("years_since_internal_inspection", 25)):
            self.assertTrue(close(d[key], wb.cached(f"D{row}"), 9), f"{key}: {d[key]} vs {wb.cached(f'D{row}')}")

    def test_every_score_is_the_one_excel_stored(self):
        for fid, row in FACTOR_ROWS.items():
            stored, got = self.wb.cached(f"F{row}"), self.by_id[fid]["score"]
            self.assertIsNotNone(got, fid)
            self.assertTrue(close(got, stored, 6), f"{fid}: engine {got} vs sheet {stored}")

    def test_every_contribution_is_the_one_excel_stored(self):
        for fid, row in FACTOR_ROWS.items():
            if fid in ("P1.0", "P2.12"):
                continue                        # P1.0 is a switch; P2.12 has a weight in RBI_Calc but sits outside every group sum
            stored, got = self.wb.cached(f"G{row}"), self.by_id[fid]["contribution"]
            self.assertTrue(close(got, stored, 6), f"{fid}: engine {got} vs sheet {stored}")

    def test_group_scores_cof_pof_and_categories(self):
        o, wb = self.out, self.wb
        self.assertTrue(close(o["groups"]["C1"], wb.cached("G36")))
        self.assertTrue(close(o["groups"]["C2"], wb.cached("G50")))
        self.assertTrue(close(o["groups"]["C3"], wb.cached("G56")))
        self.assertTrue(close(o["groups"]["PE"], wb.cached("G75")))
        self.assertTrue(close(o["groups"]["PI"], wb.cached("G91")))
        self.assertTrue(close(o["cof"]["score"], wb.cached("D93")))
        self.assertEqual(o["cof"]["driving_group"], wb.cached("D94"))
        self.assertEqual(o["cof"]["category"], wb.cached("D95"))
        self.assertEqual(o["cof"]["label"], wb.cached("D96"))
        self.assertTrue(close(o["pof"]["score"], wb.cached("D100")))
        self.assertEqual(o["pof"]["driving_group"], wb.cached("D101"))
        self.assertEqual(o["pof"]["category"], wb.cached("D102"))
        self.assertEqual(o["pof"]["label"], wb.cached("D103"))
        self.assertEqual(o["pof"]["override"], wb.cached("D99") == "YES")

    def test_risk_level_priority_and_planning(self):
        o, wb = self.out, self.wb
        self.assertEqual(o["risk"]["level"], wb.cached("D104"))
        self.assertEqual(o["risk"]["number"], wb.cached("D105"))
        self.assertEqual(o["risk"]["priority"], wb.cached("D106"))
        p = o["planning"]
        self.assertEqual(p["required_action"], wb.cached("D107"))
        self.assertEqual(p["minimum_effectiveness"], wb.cached("D108"))
        self.assertEqual(p["policy_interval_years"], wb.cached("D109"))
        self.assertEqual(p["internal_interval_years"], Decimal(str(wb.cached("D110"))))
        self.assertEqual(p["external_interval_years"], Decimal(str(wb.cached("D111"))))
        self.assertEqual(p["internal"]["due"], wb.cached("D112").date())
        self.assertEqual(p["internal"]["status"], wb.cached("D113"))
        self.assertEqual(p["external"]["due"], wb.cached("D114").date())
        self.assertEqual(p["external"]["status"], wb.cached("D115"))

    def test_damage_mechanism_summary(self):
        dm, wb = self.out["damage_mechanisms"], self.wb
        self.assertEqual(dm["entered"], wb.cached("F16", "DM_Register"))
        self.assertEqual(dm["credible"], wb.cached("F17", "DM_Register"))
        self.assertTrue(close(dm["internal_score"], wb.cached("F22", "DM_Register"), 6))
        self.assertTrue(close(dm["external_score"], wb.cached("F23", "DM_Register"), 6))
        self.assertEqual(dm["governing"], wb.cached("F24", "DM_Register"))
        self.assertEqual(dm["highest_score"], Decimal(str(wb.cached("F25", "DM_Register"))))

    def test_each_mechanism_row(self):
        wb = self.wb
        for i, m in enumerate(self.out["mechanisms"]):
            r = 6 + i
            self.assertTrue(close(m["remaining_life"], wb.cached(f"G{r}", "DM_Register"), 6), m["name"])
            self.assertTrue(close(m["damage_score"], wb.cached(f"I{r}", "DM_Register")))
            self.assertTrue(close(m["effectiveness_score"], wb.cached(f"K{r}", "DM_Register")))
            self.assertTrue(close(m["calculated_score"], wb.cached(f"L{r}", "DM_Register")))
            self.assertTrue(close(m["score_used"], wb.cached(f"O{r}", "DM_Register")))
            self.assertTrue(close(m["frequency"], wb.cached(f"P{r}", "DM_Register"), 12))

    def test_checks_of_the_sheet(self):
        self.assertFalse(self.out["checks"]["problems"])
        self.assertEqual(self.out["checks"]["missing_factors"], [])
        self.assertTrue(self.out["complete"])


@needs_sheet
class WhatTheSoftwareUsesInsteadOfTheExample(unittest.TestCase):
    """The software's own rules (Factor_Master weights, flammability = rating + 1) against the example."""

    @classmethod
    def setUpClass(cls):
        cls.wb = Workbook()
        cls.inputs = cls.wb.example_inputs()
        cls.fluids = cls.wb.fluids(cls.inputs)
        cls.library = cls.wb.library()
        cls.out = evaluate(RULES, cls.inputs, cls.fluids, cls.library)
        cls.by_id = {f["id"]: f for f in cls.out["factors"]}

    def test_every_group_totals_100_with_the_factor_master_weights(self):
        self.assertTrue(self.out["checks"]["weights_total_100"])
        self.assertEqual(sorted(self.out["checks"]["group_weights"].values()), [100] * 5)

    def test_flammability_is_the_fluids_rating_plus_one_not_the_typed_5(self):
        rating = Decimal(str(self.fluids["flammable"]["flammability"]))
        self.assertEqual(self.by_id["C2.7"]["score"], rating + 1)
        self.assertNotEqual(self.by_id["C2.7"]["score"], self.wb.cached("F45"))

    def test_scores_that_do_not_depend_on_a_weight_or_the_flammability_are_unchanged(self):
        for fid, row in FACTOR_ROWS.items():
            if fid == "C2.7":
                continue
            self.assertTrue(close(self.by_id[fid]["score"], self.wb.cached(f"F{row}"), 6), fid)

    def test_the_rules_say_what_they_changed(self):
        text = " ".join(RULES["known_issues"])
        for needle in ("typed by hand", "F45", "priority", "ascending", "Test_Cases"):
            self.assertIn(needle, text)

    def test_a_missing_input_is_reported_not_guessed(self):
        inputs = dict(self.inputs)
        inputs["detection_time"] = None
        inputs["coating_quality"] = "Mystery"
        out = evaluate(RULES, inputs, self.fluids, self.library)
        by_id = {f["id"]: f for f in out["factors"]}
        self.assertEqual(by_id["C2.4"]["status"], "missing")
        self.assertEqual(by_id["P1.7"]["status"], "invalid")
        self.assertFalse(out["complete"])
        self.assertIn("C2.4", out["checks"]["missing_factors"])
        self.assertIn("P1.7", out["checks"]["missing_factors"])


@needs_sheet
class TestCasesOfTheWorkbook(unittest.TestCase):
    """Test_Cases: scores typed in directly, aggregated with the same rules. Compared with the sheet's own calculation of each case."""

    COLS = "FGHIJK"

    @classmethod
    def setUpClass(cls):
        cls.wb = Workbook()
        cls.ws = cls.wb.values["Test_Cases"]

    def _aggregate(self, col):
        """The engine's aggregation (weights, groups, categories, matrix) fed with the case's typed scores."""
        scores = {self.ws[f"A{r}"].value: self.ws[f"{col}{r}"].value for r in range(6, 43)}
        weights = {self.ws[f"A{r}"].value: self.ws[f"E{r}"].value for r in range(6, 43)}
        # a direct-score engine run: every factor is given its typed score (the inputs are empty, so nothing else could score)
        # P2.12 / P1.12 are not rows of Test_Cases: they carry no weight
        sheet_config = {**RULES, "settings": {**RULES["settings"], "normalise_uninsulated": WORKBOOK_NORMALISE}}     # the sheet's own Config!B6
        return evaluate(sheet_config, {"assessment_date": date(2026, 1, 1), "t_actual": 10, "t_min": 5},
                        score_overrides={k: v for k, v in scores.items()},
                        weight_overrides={**{f["id"]: 0 for f in RULES["factors"]}, **weights})

    def test_the_six_cases(self):
        for col in self.COLS:
            o = self._aggregate(col)
            name = self.ws[f"{col}5"].value
            self.assertTrue(close(o["cof"]["score"], self.ws[f"{col}48"].value, 6), f"{name}: COF")
            self.assertTrue(close(o["pof"]["score"], self.ws[f"{col}55"].value, 6), f"{name}: POF")
            self.assertEqual(o["cof"]["category"], self.ws[f"{col}49"].value, name)
            self.assertEqual(o["pof"]["category"], self.ws[f"{col}56"].value, name)
            self.assertEqual(o["risk"]["level"], self.ws[f"{col}57"].value, name)


@needs_sheet
class Behaviour(unittest.TestCase):
    """Things the workbook's single example row does not show."""

    @classmethod
    def setUpClass(cls):
        cls.wb = Workbook()
        cls.inputs = cls.wb.example_inputs()
        cls.fluids = cls.wb.fluids(cls.inputs)
        cls.library = cls.wb.library()

    def run_with(self, **changes):
        return evaluate(RULES, {**self.inputs, **changes}, self.fluids, self.library)

    def test_wall_at_or_below_t_min_forces_pof_5(self):
        out = self.run_with(t_actual=10.0714)
        self.assertTrue(out["pof"]["override"])
        self.assertEqual((out["pof"]["score"], out["pof"]["category"]), (5, 5))
        self.assertEqual(out["pof"]["driving_group"], "Wall at or below minimum")
        self.assertEqual(out["derived"]["remaining_life_internal"], 0)

    def test_an_uninsulated_item_cannot_get_the_cui_block(self):
        insulated = self.run_with()
        off = {**RULES, "settings": {**RULES["settings"], "normalise_uninsulated": 0}}                  # the workbook's own value
        bare = evaluate(off, {**self.inputs, "insulated": "No"}, self.fluids, self.library)
        self.assertLess(bare["groups"]["PE"], insulated["groups"]["PE"])
        self.assertEqual(bare["groups"]["PE"], sum(f["contribution"] for f in bare["factors"] if f["group"] == "PE" and not f["cui"] and f["contribution"] is not None).quantize(Decimal("0.000000001")))

    def test_the_cui_answers_are_not_needed_when_the_item_is_not_insulated(self):
        cui_inputs = {k: None for f in RULES["factors"] if f["cui"] for k in f["inputs"]}
        cui_ids = {f["id"] for f in RULES["factors"] if f["cui"]}
        yes = self.run_with(insulated="Yes", **cui_inputs)
        no = self.run_with(insulated="No", **cui_inputs)
        self.assertTrue(cui_ids <= set(yes["checks"]["missing_factors"]))             # insulated: the four answers are needed
        self.assertFalse(cui_ids & set(no["checks"]["missing_factors"]))              # not insulated: they are multiplied by 0, so not needed
        self.assertTrue(no["complete"])                                               # and nothing else is missing
        self.assertEqual(no["groups"]["PE"], self.run_with(insulated="No")["groups"]["PE"])   # the score is the same with or without those answers

    # ---- the professional way: only the side of the damage mechanisms the person entered is asked and counted
    def mechanisms_of(self, location):
        return [m for m in self.inputs["mechanisms"] if self.library[str(m["name"]).strip().lower()]["location"] == location]

    def by_mechanism(self, mechanisms):
        return evaluate(RULES, {**self.inputs, "mechanisms": mechanisms}, self.fluids, self.library, sides_by_mechanism=True)

    def test_by_mechanism_with_both_sides_gives_the_workbook_result(self):
        self.assertTrue(self.mechanisms_of("External") and self.mechanisms_of("Internal"))
        both = self.by_mechanism(self.inputs["mechanisms"])
        plain = self.run_with()
        self.assertEqual(both["pof"]["score"], plain["pof"]["score"])
        self.assertEqual(both["risk"], plain["risk"])
        self.assertEqual(both["sides"], {"external": True, "internal": True, "by_mechanism": True})

    def test_by_mechanism_only_the_entered_side_counts(self):
        out = self.by_mechanism(self.mechanisms_of("External"))
        self.assertEqual(out["sides"], {"external": True, "internal": False, "by_mechanism": True})
        self.assertEqual(out["pof"]["score"], out["groups"]["PE"])                       # POF = the external side alone
        self.assertTrue(all(not f["applicable"] for f in out["factors"] if f["group"] == "PI"))
        inner = {f["id"] for f in RULES["factors"] if f["group"] == "PI"}
        self.assertFalse(inner & set(out["checks"]["missing_factors"]))
        out = self.by_mechanism(self.mechanisms_of("Internal"))
        self.assertEqual(out["pof"]["score"], out["groups"]["PI"])
        self.assertTrue(all(not f["applicable"] for f in out["factors"] if f["group"] == "PE"))

    def test_by_mechanism_without_any_mechanism_is_not_complete(self):
        out = self.by_mechanism([])
        self.assertFalse(out["complete"])
        self.assertTrue(any("damage mechanism" in p for p in out["checks"]["problems"]))
        self.assertEqual(out["sides"], {"external": False, "internal": False, "by_mechanism": True})

    def test_the_workbook_way_tells_the_person_to_answer_for_both_sides(self):
        only_external = evaluate(RULES, {**self.inputs, "mechanisms": self.mechanisms_of("External")}, self.fluids, self.library)
        notes = only_external["checks"]["side_notes"]
        self.assertEqual(len(notes), 1)
        self.assertIn("internal damage mechanism", notes[0])
        self.assertEqual(only_external["sides"], {"external": True, "internal": True, "by_mechanism": False})   # both sides still counted
        self.assertEqual(self.run_with()["checks"]["side_notes"], [])                                           # both sides have mechanisms
        self.assertEqual(len(evaluate(RULES, {**self.inputs, "mechanisms": []}, self.fluids, self.library)["checks"]["side_notes"]), 2)
        self.assertEqual(self.by_mechanism(self.mechanisms_of("External"))["checks"]["side_notes"], [])         # the professional way: nothing to add

    def test_the_software_works_the_professional_way_until_the_client_decides_otherwise(self):
        from app.crud import rbi_assessment
        self.assertTrue(rbi_assessment.SIDES_BY_MECHANISM)

    def test_the_workbook_way_still_counts_both_sides_without_mechanisms(self):
        out = evaluate(RULES, {**self.inputs, "mechanisms": []}, self.fluids, self.library)
        self.assertEqual(out["sides"], {"external": True, "internal": True, "by_mechanism": False})
        self.assertTrue(all(f["applicable"] for f in out["factors"]))

    def test_a_worked_out_answer_says_which_data_it_still_needs(self):
        """Component life is made from the in-service date and the design life: when it is missing the factor names what is blank."""
        out = self.run_with(design_life_years=None)
        life = next(f for f in out["factors"] if f["id"] == "P2.5")
        self.assertEqual(life["status"], "missing")
        self.assertEqual(life["message"], "needs Design life")
        out = self.run_with(design_life_years=None, in_service_date=None)
        self.assertEqual(next(f for f in out["factors"] if f["id"] == "P2.5")["message"], "needs In-service date, Design life")

    def test_an_in_service_date_after_the_assessment_date_is_named_as_the_problem(self):
        out = self.run_with(in_service_date=date(2030, 1, 1))
        life = next(f for f in out["factors"] if f["id"] == "P2.5")
        self.assertEqual(life["status"], "invalid")
        self.assertIn("a date is later than the assessment date", life["message"])
        self.assertTrue(life["from_data"])

    def test_dates_are_checked_against_the_assessment_date_and_the_in_service_date(self):
        out = self.run_with(in_service_date=date(2030, 1, 1))
        self.assertIn("In-service date is after the assessment date.", out["checks"]["problems"])
        out = self.run_with(last_internal_inspection=date(1990, 1, 1))
        self.assertTrue(any("last internal inspection is before the in-service date" in w for w in out["checks"]["warnings"]))
        self.assertFalse(any("in-service date" in p for p in out["checks"]["problems"]))

    def test_a_suggested_score_outside_1_to_5_is_not_silently_ignored(self):
        mechanisms = [dict(m) for m in self.inputs["mechanisms"]]
        mechanisms[0]["suggested_score"] = 56
        out = evaluate(RULES, {**self.inputs, "mechanisms": mechanisms}, self.fluids, self.library)
        self.assertFalse(out["complete"])
        self.assertTrue(any("from 1 to 5" in p for p in out["checks"]["problems"]))
        self.assertEqual(out["mechanisms"][0]["score_used"], out["mechanisms"][0]["calculated_score"])      # the calculated score is what stays in use
        mechanisms[0]["suggested_score"] = 4
        self.assertFalse(any("from 1 to 5" in p for p in evaluate(RULES, {**self.inputs, "mechanisms": mechanisms}, self.fluids, self.library)["checks"]["problems"]))

    def test_the_cui_range_takes_the_operating_temperature_until_the_person_types_their_own(self):
        from datetime import date
        from app.crud.rbi_assessment import Facts, effective_inputs, linked_inputs
        self.assertEqual(linked_inputs(RULES), {"cui_temperature": "operating_temperature"})       # read from the workbook's own "linked to C2.8"
        eff, _ = effective_inputs({"operating_temperature": 165}, Facts(), date(2026, 11, 13))
        self.assertEqual(eff["cui_temperature"], 165)                                              # nothing typed: the operating temperature
        eff, _ = effective_inputs({"operating_temperature": 165, "cui_temperature": 100}, Facts(), date(2026, 11, 13))
        self.assertEqual(eff["cui_temperature"], 100)                                              # the person's own value wins
        eff, _ = effective_inputs({"operating_temperature": 170}, Facts(), date(2026, 11, 13))
        self.assertEqual(eff["cui_temperature"], 170)                                              # it follows a change of the operating temperature
        eff, _ = effective_inputs({}, Facts(), date(2026, 11, 13))
        self.assertNotIn("cui_temperature", eff)                                                   # nothing to take

    def test_the_normalise_switch_rescales_the_non_cui_part(self):
        self.assertEqual(RULES["settings"]["normalise_uninsulated"], 1)                                  # the software's choice (the workbook holds 0)
        self.assertTrue(any("Config!B6" in i and "workbook value 0, software value 1" in i for i in RULES["known_issues"]))
        off = {**RULES, "settings": {**RULES["settings"], "normalise_uninsulated": 0}}
        out = self.run_with(insulated="No")
        plain = evaluate(off, {**self.inputs, "insulated": "No"}, self.fluids, self.library)
        non_cui_weights = sum(f["weight"] for f in RULES["factors"] if f["group"] == "PE" and not f["cui"]) / 100
        self.assertEqual(non_cui_weights, 0.6)
        self.assertAlmostEqual(float(out["groups"]["PE"]), float(plain["groups"]["PE"]) / non_cui_weights, places=6)

    def test_no_internal_inspection_date_means_never_inspected(self):
        out = self.run_with(last_internal_inspection=None)
        self.assertEqual(out["planning"]["internal"]["status"], "INSPECT NOW")
        self.assertEqual({f["id"]: f for f in out["factors"]}["P2.10"]["score"], 5)

    def test_an_overdue_inspection_is_flagged(self):
        out = self.run_with(last_internal_inspection=date(2000, 1, 1))
        self.assertEqual(out["planning"]["internal"]["status"], "OVERDUE")

    def test_the_priority_follows_the_sheets_formula(self):
        rows = RULES["shape"]["priority_rows"]
        out = self.run_with()
        pof, cof = out["pof"]["category"], out["cof"]["category"]
        row, col = (cof, pof) if rows == "cof" else (pof, cof)
        self.assertEqual(out["risk"]["priority"], RULES["priority"][str(row)][col - 1])

    def test_a_mechanism_with_a_weak_inspection_gets_an_upgrade_recommendation(self):
        weak = [{"name": "Internal Corrosion", "inspection_effectiveness": "E – Ineffective / none"}]
        out = self.run_with(mechanisms=weak)
        m = out["mechanisms"][0]
        self.assertTrue(m["upgrade"])
        self.assertTrue(m["recommendation"].startswith("UPGRADE"))
        good = [{"name": "Internal Corrosion", "inspection_effectiveness": "A – Highly effective"}]
        self.assertTrue(self.run_with(mechanisms=good)["mechanisms"][0]["recommendation"].startswith("MAINTAIN"))

    def test_a_suggested_score_below_the_calculated_one_needs_a_justification(self):
        rows = [{"name": "Internal Corrosion", "suggested_score": 1, "inspection_effectiveness": "E – Ineffective / none"}]
        out = self.run_with(mechanisms=rows)
        self.assertIn("justification MISSING", out["mechanisms"][0]["flag"])
        self.assertTrue(any("justification" in w for w in out["checks"]["warnings"]))

    def test_a_mechanism_that_is_not_in_the_library_is_refused(self):
        out = self.run_with(mechanisms=[{"name": "Moon dust"}])
        self.assertEqual(out["mechanisms"][0]["status"], "invalid")

    def test_two_mechanisms_score_higher_than_one(self):
        one = self.run_with(mechanisms=[{"name": "Internal Corrosion", "suggested_score": 3}])
        two = self.run_with(mechanisms=[{"name": "Internal Corrosion", "suggested_score": 3},
                                        {"name": "Localised / pitting corrosion", "suggested_score": 3}])
        self.assertGreater(two["damage_mechanisms"]["internal_score"], one["damage_mechanisms"]["internal_score"])

    def test_no_mechanism_scores_1_with_a_warning(self):
        out = self.run_with(mechanisms=[])
        self.assertEqual(out["damage_mechanisms"]["internal_score"], 1)
        self.assertTrue(any("No damage mechanism" in w for w in out["checks"]["warnings"]))

    def test_shutdown_not_required_scores_1_without_the_other_two_answers(self):
        out = self.run_with(shutdown_required="No", downtime_days=None, lost_production_category=None)
        self.assertEqual({f["id"]: f for f in out["factors"]}["C1.1"]["score"], 1)

    def test_a_flammability_that_is_not_an_nfpa_rating_is_not_scored(self):
        fluids = {**self.fluids, "flammable": {**self.fluids["flammable"], "flammability": 5}}       # the client's list gives water a 5
        out = evaluate(RULES, self.inputs, fluids, self.library)
        f = {x["id"]: x for x in out["factors"]}["C2.7"]
        self.assertEqual(f["status"], "invalid")
        self.assertIn("NFPA rating from 0 to 4", f["message"])
        self.assertIn("C2.7", out["checks"]["missing_factors"])

    def test_bad_inputs_are_reported(self):
        out = self.run_with(t_min=20, last_external_inspection=date(2100, 1, 1), internal_corrosion_rate=-1)
        text = " ".join(out["checks"]["problems"])
        self.assertIn("cannot exceed the nominal thickness", text)
        self.assertIn("after the assessment date", text)
        self.assertIn("cannot be negative", text)
        self.assertFalse(out["complete"])


class FlashPointBands(unittest.TestCase):
    """RBI_Calc F48: flash point against 1.05, 1.10, 1.15, 1.20, 1.25 times the operating temperature (score 5 down to 1, else 0)."""

    @classmethod
    def setUpClass(cls):
        cls.f = next(f for f in RULES["factors"] if f["id"] == "C2.10")

    def score(self, flash, temp):
        from app.rbi_engine.engine import _score_factor
        out = _score_factor(RULES, self.f, {"representative_fluid": "x", "operating_temperature": temp}, {}, {"flammable": {"flash_point": flash}}, {})
        return int(out["score"])

    def test_above_zero_it_is_the_sheets_multiplication(self):
        for flash, temp in ((83, 80), (84, 80), (85, 80), (88, 80), (92, 80), (96, 80), (100, 80), (100.1, 80), (200, 80), (1, 1), (1.3, 1)):
            t = Decimal(str(temp))
            expected = next((s for m, s in ((1.05, 5), (1.10, 4), (1.15, 3), (1.20, 2), (1.25, 1)) if Decimal(str(flash)) <= Decimal(str(m)) * t), 0)
            self.assertEqual(self.score(flash, temp), expected, (flash, temp))

    def test_at_or_below_zero_every_band_can_be_reached(self):
        # -20 C: the limits are -19, -18, -17, -16, -15 (a margin of 5 % of 20 degrees each step)
        self.assertEqual([self.score(f, -20) for f in (-30, -19, -18, -17, -16, -15, -14)], [5, 5, 4, 3, 2, 1, 0])
        self.assertEqual([self.score(f, 0) for f in (-1, 0, 1)], [5, 5, 0])


class KnownTypingSlip(unittest.TestCase):
    """The toxic table of the client's sheet (Score_Bands A143:A147) reads 0, 100, 200, 5000, 1000: 5000 is used as 500 until the client answers."""

    def test_the_toxic_table_is_ascending_and_the_correction_is_listed(self):
        table = next(f for f in RULES["factors"] if f["id"] == "C2.1")["table"]
        self.assertEqual([t["lower"] for t in table], [0, 100, 200, 500, 1000])
        self.assertEqual([t["score"] for t in table], [5, 4, 3, 2, 1])
        self.assertTrue(any("Score_Bands!A146 holds 5000; the software uses 500" in i for i in RULES["known_issues"]))
        self.assertEqual([band(table, Decimal(v)) for v in (99, 500, 900, 1000, 2500)], [5, 2, 2, 1, 1])

    def test_a_correction_stops_when_the_workbook_no_longer_has_the_old_number(self):
        from app.rbi_engine.rules_builder import _apply_corrections
        table, issues = [{"lower": 0}, {"lower": 100}, {"lower": 200}, {"lower": 500}, {"lower": 1000}], []
        _apply_corrections(table, 143, 147, "C2.1", issues)
        self.assertEqual([t["lower"] for t in table], [0, 100, 200, 500, 1000])
        self.assertEqual(issues, [])


class Lookups(unittest.TestCase):
    """The two ways the sheet looks a value up, done the way Excel does them."""

    def test_approximate_match_is_the_last_lower_bound_not_above_the_value(self):
        lowers = [0, 2, 5, 10, 20]
        for x, i in ((0, 0), (1.99, 0), (2, 1), (4.9, 1), (5, 2), (19.99, 3), (20, 4), (1000, 4)):
            self.assertEqual(approx(lowers, Decimal(str(x))), i, x)
        self.assertIsNone(approx(lowers, Decimal("-1")))

    def test_a_table_that_is_not_ascending_gives_what_excel_gives(self):
        # Score_Bands A143:A147 of the client's sheet: 0, 100, 200, 5000, 1000
        lowers = [0, 100, 200, 5000, 1000]
        self.assertEqual(approx(lowers, Decimal(100)), 1)         # the example of the sheet: 100 ppm scores like row 2
        self.assertEqual(approx(lowers, Decimal(50)), 0)

    def test_exact_match_ignores_capitals_and_spaces(self):
        table = [{"option": "A – Highly effective", "score": 1}, {"option": "Gas / vapour", "score": 4}]
        self.assertEqual(exact(table, "gas  / VAPOUR")["score"], 4)
        self.assertIsNone(exact(table, "plasma"))

    def test_band_is_none_below_the_table(self):
        self.assertIsNone(band([{"lower": 5, "score": 1}], Decimal(4)))


@needs_sheet
class Builder(unittest.TestCase):
    def test_the_saved_rules_are_what_the_sheet_gives_now(self):
        fresh = build_rules(SHEET_FILE)
        self.assertEqual(fresh["version"], RULES["version"], "rules.json is older than the workbook: run app.rbi_engine.rules_builder --apply")
        self.assertEqual(len(fresh["factors"]), 39)

    def test_a_formula_the_reader_does_not_know_stops_the_build(self):
        import os
        import tempfile
        import openpyxl
        wb = openpyxl.load_workbook(SHEET_FILE)
        wb["RBI_Calc"]["F42"] = "=VLOOKUP(D42,Score_Bands!A30:B34,2,1)"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "changed.xlsx")
            wb.save(path)
            with self.assertRaises(RulesError):
                build_rules(path)


if __name__ == "__main__":
    unittest.main()
