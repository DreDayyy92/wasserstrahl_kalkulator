"""
Erzeugt ein PDF-Kalkulationsblatt aus dem zuletzt berechneten Ergebnis.
Nutzt fpdf2 (reines Python, keine Systemabhängigkeiten wie bei WeasyPrint).
"""
from __future__ import annotations

from fpdf import FPDF

LABEL_W = 95


def _row(pdf: FPDF, label: str, value: str, bold: bool = False) -> None:
    pdf.set_font("helvetica", "B" if bold else "", 10)
    pdf.cell(LABEL_W, 7, label, border=1)
    pdf.cell(0, 7, value, border=1, new_x="LMARGIN", new_y="NEXT")


def _section(pdf: FPDF, title: str) -> None:
    pdf.ln(3)
    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")


def build_pdf(result: dict, dateiname: str, preview_png_path: str | None = None) -> bytes:
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("helvetica", "B", 16)
    pdf.cell(0, 10, "Wasserstrahl-Kalkulation", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 11)
    pdf.cell(0, 7, f"Teil: {dateiname}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Stueckzahl: {result['stueckzahl']}", new_x="LMARGIN", new_y="NEXT")

    geo = result.get("geo") or {}
    _section(pdf, "Geometrie")
    if geo.get("width_mm") is not None:
        _row(pdf, "Breite x Hoehe", f"{geo['width_mm']} x {geo['height_mm']} mm")
    _row(pdf, "Flaeche", f"{geo.get('area_m2', 0):.4f} m2")
    _row(pdf, "Gesamt-Schnittlaenge", f"{geo.get('total_cut_length_mm', 0)} mm")

    if preview_png_path:
        pdf.ln(3)
        pdf.image(preview_png_path, x=10, w=80)

    _section(pdf, "Material" + ("" if result["material_berechnet"] else " (Beistellmaterial des Kunden)"))
    _row(pdf, "Material", f"{result.get('material_name', '-')} ({result['dicke_mm']} mm)")
    if result["material_berechnet"]:
        _row(pdf, "Gewicht pro Teil", f"{result['gewicht_kg']} kg")
        _row(pdf, "Preis/kg", f"{result['material_preis_pro_kg']:.2f} EUR")
        _row(pdf, "Materialkosten pro Teil", f"{result['materialkosten']:.2f} EUR")
        _row(pdf, f"Materialkosten x {result['stueckzahl']}", f"{result['materialkosten_gesamt']:.2f} EUR", bold=True)
    else:
        _row(pdf, "Materialkosten", "0.00 EUR (vom Kunden gestellt)")

    _section(pdf, "Maschine")
    if result.get("schnittqualitaet_label"):
        _row(pdf, "Schnittqualitaet", result["schnittqualitaet_label"])
    if result.get("schnittgeschwindigkeit_effektiv") is not None:
        _row(pdf, "Schnittgeschwindigkeit", f"{result['schnittgeschwindigkeit_effektiv']} mm/min")
    _row(pdf, "Einstiche", str(result.get("einstiche", "-")))
    _row(pdf, "Schnittzeit", f"{result['schnittzeit_min']} min")
    _row(pdf, "Einstechzeit gesamt", f"{result['einstechzeit_min']} min")
    _row(pdf, "Maschinenzeit pro Teil", f"{result['maschinenzeit_min']} min")
    _row(pdf, "Maschinenkosten pro Teil", f"{result['maschinenkosten']:.2f} EUR")
    _row(pdf, f"Maschinenkosten x {result['stueckzahl']}", f"{result['maschinenkosten_gesamt']:.2f} EUR", bold=True)
    _row(pdf, "Ruestkosten (einmalig)", f"{result['ruestkosten']:.2f} EUR", bold=True)

    pdf.ln(6)
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(
        0, 10,
        f"Gesamtkosten fuer {result['stueckzahl']} Stueck: {result['gesamtkosten']:.2f} EUR",
        new_x="LMARGIN", new_y="NEXT",
    )

    return bytes(pdf.output())
