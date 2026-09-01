"""
Versendet eine unverbindliche Angebotsanfrage (PDF + ggf. Original-DXF) per
E-Mail an den Betreiber - kein verbindlicher Auftrag. Alle Zugangsdaten
(SMTP-Host, Benutzer, Passwort, Zieladresse) kommen ausschliesslich aus
Umgebungsvariablen (siehe .env.example) - so landen sie nie im Quellcode und
damit auch nie im Git-Repo/auf GitHub.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import pdf_export


def is_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("MAIL_TO"))


def send_offer_request_email(
    result: dict,
    dateiname: str,
    dxf_path: str | None,
    preview_png_path: str | None,
    kunde_name: str,
    kunde_email: str,
    kunde_notiz: str,
) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    mail_from = os.environ.get("MAIL_FROM") or username
    mail_to = os.environ["MAIL_TO"]

    msg = EmailMessage()
    msg["Subject"] = f"Unverbindliche Angebotsanfrage: {dateiname}"
    msg["From"] = mail_from
    msg["To"] = mail_to
    if kunde_email:
        msg["Reply-To"] = kunde_email

    lines = [
        "Unverbindliche Angebotsanfrage ueber den Wasserstrahl-Kalkulator.",
        "",
        f"Teil: {dateiname}",
        f"Stueckzahl: {result.get('stueckzahl')}",
        f"Material: {result.get('material_name')} ({result.get('dicke_mm')} mm)",
        f"Schnittqualitaet: {result.get('schnittqualitaet_label')}",
        f"Schnittgeschwindigkeit: {result.get('schnittgeschwindigkeit_effektiv')} mm/min",
        f"Gesamtkosten: {result.get('gesamtkosten'):.2f} EUR (netto, zzgl. gesetzlicher MwSt.)",
    ]
    if kunde_name or kunde_email:
        lines += ["", "Kunde:"]
        if kunde_name:
            lines.append(f"  Name: {kunde_name}")
        if kunde_email:
            lines.append(f"  E-Mail: {kunde_email}")
    if kunde_notiz:
        lines += ["", "Anmerkung des Kunden:", kunde_notiz]

    msg.set_content("\n".join(lines))

    pdf_bytes = pdf_export.build_pdf(result, dateiname, preview_png_path)
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="kalkulation.pdf")

    if dxf_path and os.path.exists(dxf_path):
        with open(dxf_path, "rb") as f:
            dxf_bytes = f.read()
        anhang_name = dateiname if dateiname.lower().endswith(".dxf") else f"{dateiname}.dxf"
        msg.add_attachment(dxf_bytes, maintype="application", subtype="dxf", filename=anhang_name)

    if preview_png_path and os.path.exists(preview_png_path):
        with open(preview_png_path, "rb") as f:
            png_bytes = f.read()
        msg.add_attachment(png_bytes, maintype="image", subtype="png", filename="vorschau.png")

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)
