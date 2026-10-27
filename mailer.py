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


def is_configured(empfaenger_emails: list[str] | None = None) -> bool:
    has_target = bool(empfaenger_emails) or bool(os.environ.get("MAIL_TO"))
    return bool(os.environ.get("SMTP_HOST") and has_target)


def _resolve_recipients(empfaenger_emails: list[str] | None) -> list[str]:
    if empfaenger_emails:
        return empfaenger_emails
    mail_to = os.environ.get("MAIL_TO")
    return [mail_to] if mail_to else []


def _send(msg: EmailMessage) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)


def _teile_lines(result: dict) -> list[str]:
    teile = result["teile"]
    if len(teile) == 1:
        return [f"Teil: {teile[0]['dateiname']}", f"Stückzahl: {teile[0]['stueckzahl']}"]
    return ["Teile:"] + [
        f"  {i}. {t['dateiname']} - Stückzahl {t['stueckzahl']}" for i, t in enumerate(teile, start=1)
    ]


def send_offer_request_email(
    result: dict,
    dateiname: str,
    teil_files: list[dict],
    kunde_name: str,
    kunde_email: str,
    kunde_notiz: str,
    empfaenger_emails: list[str] | None = None,
) -> None:
    username = os.environ.get("SMTP_USERNAME", "")
    mail_from = os.environ.get("MAIL_FROM") or username
    mail_to = _resolve_recipients(empfaenger_emails)
    if not mail_to:
        raise RuntimeError("Keine Zieladresse fuer die Angebotsanfrage konfiguriert.")

    msg = EmailMessage()
    msg["Subject"] = f"Unverbindliche Angebotsanfrage: {dateiname}"
    msg["From"] = mail_from
    msg["To"] = ", ".join(mail_to)
    if kunde_email:
        msg["Reply-To"] = kunde_email

    lines = [
        "Unverbindliche Angebotsanfrage über den Wasserstrahl-Kalkulator.",
        "",
        *_teile_lines(result),
        f"Material: {result.get('material_name')} ({result.get('dicke_mm')} mm)",
        f"Schnittqualität: {result.get('schnittqualitaet_label')}",
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

    pdf_bytes = pdf_export.build_pdf(result, dateiname, [f["png_path"] for f in teil_files])
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="kalkulation.pdf")

    for n, f in enumerate(teil_files, start=1):
        if f["dxf_path"] and os.path.exists(f["dxf_path"]):
            with open(f["dxf_path"], "rb") as fh:
                dxf_bytes = fh.read()
            name = f["dateiname"]
            anhang_name = name if name.lower().endswith(".dxf") else f"{name}.dxf"
            msg.add_attachment(dxf_bytes, maintype="application", subtype="dxf", filename=anhang_name)

        if f["png_path"] and os.path.exists(f["png_path"]):
            with open(f["png_path"], "rb") as fh:
                png_bytes = fh.read()
            msg.add_attachment(png_bytes, maintype="image", subtype="png", filename=f"vorschau_{n}.png")

    _send(msg)


def send_customer_confirmation_email(
    result: dict,
    dateiname: str,
    teil_files: list[dict],
    kunde_name: str,
    kunde_email: str,
    empfaenger_emails: list[str] | None = None,
) -> None:
    """Bestaetigung an den Kunden selbst (nur wenn er eine E-Mail-Adresse
    angegeben hat) - eigene Kopie der Kalkulation, kein verbindliches
    Angebot. Best-effort: wird vom Aufrufer separat von der eigentlichen
    Anfrage an den Betreiber behandelt, ein Fehler hier soll die bereits
    erfolgreich verschickte Anfrage nicht ungeschehen machen."""
    username = os.environ.get("SMTP_USERNAME", "")
    mail_from = os.environ.get("MAIL_FROM") or username
    shop_recipients = _resolve_recipients(empfaenger_emails)
    mail_to_shop = shop_recipients[0] if shop_recipients else mail_from

    msg = EmailMessage()
    msg["Subject"] = f"Ihre Anfrage: {dateiname}"
    msg["From"] = mail_from
    msg["To"] = kunde_email
    msg["Reply-To"] = mail_to_shop

    anrede = f"Hallo {kunde_name}," if kunde_name else "Hallo,"
    lines = [
        anrede,
        "",
        "vielen Dank für Ihre Anfrage über unseren Kalkulator. "
        "Wir haben ihre Daten erhalten und prüfen diese umgehend. "
        "Sie erhalten in Kürze ein verbindliches Angebot von uns. ",
        "",
        *_teile_lines(result),
        f"Material: {result.get('material_name')} ({result.get('dicke_mm')} mm)",
        f"Geschätzte Gesamtkosten: {result.get('gesamtkosten'):.2f} EUR "
        "(netto, unverbindlich - kein verbindliches Angebot)",
        "",
        "Im Anhang finden Sie eine Kopie der Kalkulation als PDF.",
        "",
        "Mit freundlichen Grüßen",
    ]
    msg.set_content("\n".join(lines))

    pdf_bytes = pdf_export.build_pdf(result, dateiname, [f["png_path"] for f in teil_files])
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="kalkulation.pdf")

    _send(msg)
