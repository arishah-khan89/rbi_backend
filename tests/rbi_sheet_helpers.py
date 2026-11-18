"""Reads the client's workbook for the RBI tests: the example row of RBI_Calc as engine inputs, and the numbers Excel stored next to it."""
import os
import unittest
from datetime import date, datetime
from typing import Any, Dict, List

import openpyxl

SHEET_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "030-E-11- Shell.xlsx"))
HAVE_SHEET = os.path.exists(SHEET_FILE)
needs_sheet = unittest.skipUnless(HAVE_SHEET, "the client's workbook is not next to the project")

# input key -> RBI_Calc row of its D cell
INPUT_ROWS = {
    "assessment_date": 8, "in_service_date": 9, "design_life_years": 10, "t_nominal": 11, "t_actual": 12, "t_min": 13,
    "external_corrosion_rate": 14, "internal_corrosion_rate": 15, "last_internal_inspection": 16, "last_external_inspection": 17,
    "shutdown_required": 28, "downtime_days": 29, "lost_production_category": 30, "inventory_kg": 32, "cost_per_kg": 33,
    "repair_cost": 34, "toxic_fluid": 38, "toxic_percentage": 40, "initial_fluid_phase": 41, "detection_time": 42,
    "isolation_time": 43, "ignition_distance": 44, "operating_temperature": 46, "operating_pressure": 47,
    "representative_fluid": 49, "reputation": 52, "environmental": 53, "workforce_persons": 54, "insulated": 59,
    "cui_temperature": 60, "insulation_type": 61, "insulation_condition": 62, "chloride_ppm": 63, "area_humidity": 64,
    "cooling_tower_distance": 65, "coating_quality": 66, "external_inspections": 68, "external_inspection_confidence": 69,
    "corrosion_trend": 79, "internal_corrosion_type": 80, "operation_data_status": 84, "internal_inspections": 85,
    "internal_inspection_confidence": 86, "service_status": 88,
}
# factor id -> RBI_Calc row (the row of its weight in column C, its score in F and its contribution in G)
FACTOR_ROWS = {
    "C1.1": 31, "C1.2": 35, "C2.1": 39, "C2.2": 40, "C2.3": 41, "C2.4": 42, "C2.5": 43, "C2.6": 44, "C2.7": 45, "C2.8": 46,
    "C2.9": 47, "C2.10": 48, "C3.1": 52, "C3.2": 53, "C3.3": 54, "P1.0": 59, "P1.1": 60, "P1.2": 61, "P1.3": 62, "P1.4": 63,
    "P1.5": 64, "P1.6": 65, "P1.7": 66, "P1.8": 67, "P1.9": 68, "P1.10": 69, "P1.11": 70, "P2.1": 78, "P2.2": 79, "P2.3": 80,
    "P2.4": 81, "P2.5": 82, "P2.6": 83, "P2.7": 84, "P2.8": 85, "P2.9": 86, "P2.10": 87, "P2.11": 88, "P2.12": 89,
}


class Workbook:
    def __init__(self):
        self.values = openpyxl.load_workbook(SHEET_FILE, data_only=True)
        self.calc = self.values["RBI_Calc"]

    def example_inputs(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, row in INPUT_ROWS.items():
            v = self.calc[f"D{row}"].value
            out[key] = v.date() if isinstance(v, datetime) else v
        dm = self.values["DM_Register"]
        mechanisms = []
        for r in range(6, 14):
            name = dm[f"B{r}"].value
            if not name:
                continue
            mechanisms.append({"name": name, "susceptibility": dm[f"H{r}"].value, "inspection_effectiveness": dm[f"J{r}"].value,
                               "suggested_score": dm[f"M{r}"].value, "justification": dm[f"N{r}"].value})
        out["mechanisms"] = mechanisms
        return out

    def fluids(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        ws = self.values["list of fluids"]
        rows = [{"toxic_name": ws.cell(r, 1).value, "flammable_name": ws.cell(r, 2).value, "toxicity_concentration": ws.cell(r, 5).value,
                 "flammability": ws.cell(r, 6).value, "flash_point": ws.cell(r, 10).value} for r in range(2, ws.max_row + 1)]
        toxic = next((x for x in rows if x["toxic_name"] and str(x["toxic_name"]).strip().lower() == str(inputs.get("toxic_fluid")).strip().lower()), None)
        flam = next((x for x in rows if x["flammable_name"] and str(x["flammable_name"]).strip().lower() == str(inputs.get("representative_fluid")).strip().lower()), None)
        return {"toxic": toxic, "flammable": flam}

    def library(self) -> Dict[str, dict]:
        ws = self.values["NDE_Library"]
        out = {}
        for r in range(6, ws.max_row + 1):
            name = ws.cell(r, 2).value
            if name:
                out[str(name).strip().lower()] = {"name": name, "location": ws.cell(r, 3).value, "damage_class": ws.cell(r, 4).value,
                                                  "rate_based": ws.cell(r, 5).value, "primary_nde": ws.cell(r, 7).value,
                                                  "secondary_nde": ws.cell(r, 8).value}
        return out

    def cached(self, coord: str, sheet: str = "RBI_Calc") -> Any:
        return self.values[sheet][coord].value

    def sheet_weights(self) -> Dict[str, Any]:
        """The weights RBI_Calc actually used in its example (column C, some typed by hand)."""
        return {fid: self.calc[f"C{row}"].value for fid, row in FACTOR_ROWS.items() if isinstance(self.calc[f"C{row}"].value, (int, float))}
