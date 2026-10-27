"""
Erzeugt ein PDF-Kalkulationsblatt aus dem zuletzt berechneten Ergebnis.
Nutzt fpdf2 (reines Python, keine Systemabhängigkeiten wie bei WeasyPrint).
"""
from __future__ import annotations

from fpdf import FPDF

LABEL_W = 95
NETTO_HINWEIS = "Alle Preise sind Nettopreise zzgl. der gesetzlichen Mehrwertsteuer."


def _row(pdf: FPDF, label: str, value: str, bold: bool = False) -> None:
    pdf.set_font("helvetica", "B" if bold else "", 10)
    pdf.cell(LABEL_W, 7, label, border=1)
    pdf.cell(0, 7, value, border=1, new_x="LMARGIN", new_y="NEXT")


def _section(pdf: FPDF, title: str) -> None:
    pdf.ln(3)
    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")


def build_pdf(result: dict, dateiname: str, preview_png_paths: list[str | None] | None = None) -> bytes:
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    teile = result["teile"]
    previews = preview_png_paths or []

    pdf.set_font("helvetica", "B", 16)
    pdf.cell(0, 10, "Wasserstrahl-Kalkulation (unverbindlich)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 11)
    if len(teile) == 1:
        pdf.cell(0, 7, f"Teil: {dateiname}", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(0, 7, f"{len(teile)} Teile", new_x="LMARGIN", new_y="NEXT")

    _section(pdf, "Material" + ("" if result["material_berechnet"] else " (Beistellmaterial des Kunden)"))
    _row(pdf, "Material", f"{result.get('material_name', '-')} ({result['dicke_mm']} mm)")
    if result["material_berechnet"]:
        _row(pdf, "Preis/kg", f"{result['material_preis_pro_kg']:.2f} EUR")
    if result.get("schnittqualitaet_label"):
        _row(pdf, "Schnittqualität", result["schnittqualitaet_label"])
    if result.get("schnittgeschwindigkeit_effektiv") is not None:
        _row(pdf, "Schnittgeschwindigkeit", f"{result['schnittgeschwindigkeit_effektiv']} mm/min")

    for i, t in enumerate(teile):
        geo = t.get("geo") or {}
        _section(pdf, f"Teil {i + 1}: {t['dateiname']} (Stückzahl {t['stueckzahl']})")
        if geo.get("width_mm") is not None:
            _row(pdf, "Breite x Höhe", f"{geo['width_mm']} x {geo['height_mm']} mm")
        _row(pdf, "Fläche", f"{geo.get('area_m2', 0):.4f} m2")
        _row(pdf, "Gesamt-Schnittlänge", f"{geo.get('total_cut_length_mm', 0)} mm")
        _row(pdf, "Einstiche", str(t.get("einstiche", "-")))

        preview = previews[i] if i < len(previews) else None
        if preview:
            pdf.ln(3)
            pdf.image(preview, x=10, w=80)

        if result["material_berechnet"]:
            _row(pdf, "Gewicht pro Teil", f"{t['gewicht_kg']} kg")
            _row(pdf, "Materialkosten pro Teil", f"{t['materialkosten']:.2f} EUR")
            _row(pdf, f"Materialkosten x {t['stueckzahl']}", f"{t['materialkosten_gesamt']:.2f} EUR", bold=True)
        _row(pdf, "Schnittzeit", f"{t['schnittzeit_min']} min")
        _row(pdf, "Einstechzeit gesamt", f"{t['einstechzeit_min']} min")
        _row(pdf, "Maschinenzeit pro Teil", f"{t['maschinenzeit_min']} min")
        _row(pdf, "Maschinenkosten pro Teil", f"{t['maschinenkosten']:.2f} EUR")
        _row(pdf, f"Maschinenkosten x {t['stueckzahl']}", f"{t['maschinenkosten_gesamt']:.2f} EUR", bold=True)
        _row(pdf, "Kosten dieses Teils", f"{t['teilkosten']:.2f} EUR", bold=True)

    _section(pdf, "Summe")
    if result["material_berechnet"]:
        _row(pdf, "Materialkosten gesamt", f"{result['materialkosten_gesamt']:.2f} EUR")
    else:
        _row(pdf, "Materialkosten", "0.00 EUR (vom Kunden gestellt)")
    _row(pdf, "Maschinenkosten gesamt", f"{result['maschinenkosten_gesamt']:.2f} EUR")
    _row(pdf, f"Rüstkosten ({result.get('ruestvorgaenge', 1)} Tafel(n))", f"{result['ruestkosten']:.2f} EUR", bold=True)

    pdf.ln(6)
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(
        0, 10,
        f"Gesamtkosten: {result['gesamtkosten']:.2f} EUR",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.set_font("helvetica", "", 9)
    pdf.cell(0, 6, NETTO_HINWEIS, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "Unverbindlicher Richtwert, kein verbindliches Angebot.", new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())
