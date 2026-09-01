"""
Import einer einfachen Materialliste als CSV: Material, Stärke (mm),
Schnittgeschwindigkeit (mm/min), Preis (€/kg), optional Dichte (g/cm³) -
eine Zeile pro Material/Stärke-Kombination. Ersetzt den früheren
DATANORM-Import: keine Spaltenzuordnung nötig, die Spalten werden anhand der
Kopfzeile erkannt (Groß-/Kleinschreibung und übliche Schreibvarianten egal).

Preis, Schnittgeschwindigkeit und Dichte werden serverseitig ausschließlich
aus dieser Liste gelesen (siehe app.py: berechnen()) - Kunden wählen nur ein
Material aus und können diese Werte nicht über das Formular überschreiben,
nur der Admin über diese CSV.

CSV statt Excel, damit keine zusätzliche Bibliothek (openpyxl) nötig ist -
Excel kann CSV-Dateien direkt öffnen, bearbeiten und speichern.
"""
from __future__ import annotations

import csv
import io

NAME_ALIASES = {"material", "materialname", "name", "bezeichnung"}
STAERKE_ALIASES = {
    "staerke_mm", "staerke", "stärke", "stärke_mm", "dicke_mm", "dicke",
}
SCHNITTGESCHW_ALIASES = {
    "schnittgeschwindigkeit_mm_min", "schnittgeschwindigkeit", "schnittgeschw",
    "geschwindigkeit_mm_min", "geschwindigkeit", "vmax",
}
PREIS_ALIASES = {"preis_pro_kg", "preis_kg", "preis", "price", "preis_eur_kg"}
DICHTE_ALIASES = {"dichte_g_cm3", "dichte", "dichte_g_cm", "density"}
DICHTE_DEFAULT = 7.85  # Baustahl, falls keine Dichte-Spalte vorhanden ist


def _normalize_header(name: str) -> str:
    return (
        name.strip().lower()
        .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
        .replace(" ", "_").replace("/", "_").replace("€", "eur")
    )


def _to_float(value: str, default: float = 0.0) -> float:
    value = value.strip().replace(",", ".")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _read_text(filepath: str) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(filepath, "r", encoding=enc, newline="") as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(filepath, "r", encoding="latin-1", errors="replace", newline="") as f:
        return f.read()


def parse_csv(filepath: str) -> list[dict]:
    """Liest eine Material-CSV ein. Erwartete Spalten (Reihenfolge egal):
    Material, Staerke_mm, Schnittgeschwindigkeit_mm_min, Preis_pro_kg."""
    raw = _read_text(filepath)
    lines = raw.splitlines()
    first_line = lines[0] if lines else ""
    # Deutsches Excel exportiert CSV meist mit ';' statt ',' als Trennzeichen
    delimiter = ";" if first_line.count(";") >= first_line.count(",") else ","

    rows = list(csv.reader(io.StringIO(raw), delimiter=delimiter))
    if not rows:
        return []

    header = [_normalize_header(h) for h in rows[0]]

    def find_col(aliases):
        for i, h in enumerate(header):
            if h in aliases:
                return i
        return -1

    name_idx = find_col(NAME_ALIASES)
    staerke_idx = find_col(STAERKE_ALIASES)
    geschw_idx = find_col(SCHNITTGESCHW_ALIASES)
    preis_idx = find_col(PREIS_ALIASES)
    dichte_idx = find_col(DICHTE_ALIASES)

    if name_idx < 0:
        raise ValueError(
            "Keine 'Material'-Spalte gefunden. Erwartete Spalten: "
            "Material, Staerke_mm, Schnittgeschwindigkeit_mm_min, Preis_pro_kg, "
            "optional Dichte_g_cm3."
        )

    materials = []
    for row in rows[1:]:
        if not row or all(not cell.strip() for cell in row):
            continue

        def get(idx):
            return row[idx].strip() if 0 <= idx < len(row) else ""

        name = get(name_idx)
        if not name:
            continue

        materials.append(
            {
                "name": name,
                "staerke_mm": _to_float(get(staerke_idx)) if staerke_idx >= 0 else 0.0,
                "schnittgeschwindigkeit_mm_min": _to_float(get(geschw_idx)) if geschw_idx >= 0 else 0.0,
                "preis_pro_kg": _to_float(get(preis_idx)) if preis_idx >= 0 else 0.0,
                "dichte_g_cm3": _to_float(get(dichte_idx), DICHTE_DEFAULT) if dichte_idx >= 0 else DICHTE_DEFAULT,
            }
        )
    return materials


def build_template_csv() -> str:
    """Beispiel-CSV zum Download, damit das erwartete Format klar ist."""
    lines = [
        "Material;Staerke_mm;Schnittgeschwindigkeit_mm_min;Preis_pro_kg;Dichte_g_cm3",
        "Baustahl S235;5;500;1.20;7.85",
        "Edelstahl 1.4301;3;350;4.50;7.90",
        "Alu AlMg3;4;800;3.10;2.70",
    ]
    return "\r\n".join(lines) + "\r\n"
