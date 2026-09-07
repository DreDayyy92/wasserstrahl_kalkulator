import os
import random
import secrets
import shutil
import time
import uuid
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, redirect, url_for, flash, Response, session
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import generate_password_hash, check_password_hash

import dxf_analyzer as dxf
import mailer
import pdf_export
import storage

# Laedt SMTP-Zugangsdaten/Zieladresse aus einer lokalen .env-Datei (siehe
# .env.example) - die echten Werte stehen damit nie im Quellcode/Git-Repo.
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
RESULTS_DIR = os.path.join(INSTANCE_DIR, "results")
MATERIALS_PATH = os.path.join(INSTANCE_DIR, "materials.json")
SETTINGS_PATH = os.path.join(INSTANCE_DIR, "settings.json")
ADMIN_PATH = os.path.join(INSTANCE_DIR, "admin.json")
LEGAL_PATH = os.path.join(INSTANCE_DIR, "legal.json")
SECRET_KEY_PATH = os.path.join(INSTANCE_DIR, "secret_key.txt")

os.makedirs(INSTANCE_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)


def _load_or_create_secret_key() -> str:
    """Mehrere Gunicorn-Worker-Prozesse importieren app.py beim Start
    gleichzeitig - ohne atomares Anlegen könnten zwei Worker je einen
    eigenen Schlüssel erzeugen und sich gegenseitig die Datei überschreiben,
    wodurch Sessions je nach Worker ungültig würden."""
    for _ in range(20):
        try:
            fd = os.open(SECRET_KEY_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            key = secrets.token_hex(32)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(key)
            return key
        except FileExistsError:
            pass
        try:
            with open(SECRET_KEY_PATH, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise RuntimeError(f"Konnte Secret Key nicht aus {SECRET_KEY_PATH} lesen/erzeugen.")


app = Flask(__name__)
# Zufälliger, dauerhaft gespeicherter Schlüssel statt fest im Code - mehrere
# Leute nutzen die App jetzt gleichzeitig über eigene (Login-freie) Sessions,
# die Signatur muss deshalb pro Installation eindeutig sein.
app.secret_key = _load_or_create_secret_key()

# Werden nur vom Admin (Login) gesetzt - Kunden sehen/ändern diese nicht.
DEFAULT_SETTINGS = {
    "maschinenstundensatz_eur": 45.0,
    "ruestzeit_min": 10.0,
    "max_upload_mb": 20,
}

# Fallback fuer Materialien aus einer aelteren Version ohne eigene
# Einstechzeit je Staerke.
DEFAULT_EINSTECHZEIT_S = 15

# Schnittqualität: fester Anteil der Listen-Schnittgeschwindigkeit des
# gewählten Materials. Vom Kunden auswählbar (im Gegensatz zu Preis/
# Geschwindigkeit selbst, die aus der Materialliste kommen und nicht
# änderbar sind), da es eine reine Qualitäts-/Zeit-Abwägung für den Kunden
# ist, keine interne Preiskalkulation.
SCHNITTQUALITAET = {
    "fein": {"label": "Feinschnitt", "prozent": 50},
    "mittel": {"label": "Mittelschnitt", "prozent": 75},
    "trenn": {"label": "Trennschnitt", "prozent": 100},
}
SCHNITTQUALITAET_DEFAULT = "fein"

# Impressum/Datenschutz verlinken auf die Haupt-Domain (dort zentral
# gepflegt); nur die AGB werden als Freitext direkt in der App verwaltet.
# Rechtlich bindende Inhalte müssen vom Betreiber stammen (ggf. mit
# Generator/Anwalt), wir formulieren hier nichts vor.
DEFAULT_LEGAL = {
    "impressum_url": "https://www.baeckereitechnik-doerner.com/about/",
    "datenschutz_url": "https://www.baeckereitechnik-doerner.com/j/privacy",
    "agb": "",
}

# Wird überall dort angezeigt, wo Preise erscheinen (Formular, Ergebnis,
# PDF, Auftrags-Mail) - alle berechneten Preise sind Nettopreise.
NETTO_HINWEIS = "Alle Preise sind Nettopreise zzgl. der gesetzlichen Mehrwertsteuer."


@app.before_request
def _apply_upload_limit():
    # Admin-konfigurierbar (siehe /admin/settings) statt fest im Code - liest
    # settings.json bei jedem Request neu, damit eine Änderung ohne Neustart
    # wirkt. Werkzeug prüft MAX_CONTENT_LENGTH beim Einlesen des Request-Body,
    # danach greift automatisch RequestEntityTooLarge (siehe Errorhandler).
    settings = storage.load_json(SETTINGS_PATH, default=DEFAULT_SETTINGS)
    max_mb = settings.get("max_upload_mb", DEFAULT_SETTINGS["max_upload_mb"])
    app.config["MAX_CONTENT_LENGTH"] = int(max_mb * 1024 * 1024)


@app.errorhandler(RequestEntityTooLarge)
def handle_upload_too_large(e):
    settings = storage.load_json(SETTINGS_PATH, default=DEFAULT_SETTINGS)
    max_mb = settings.get("max_upload_mb", DEFAULT_SETTINGS["max_upload_mb"])
    message = f"Datei ist zu groß (maximal {max_mb:.0f} MB erlaubt)."
    if request.path == url_for("dxf_preview"):
        return jsonify({"error": message}), 413
    flash(message)
    return redirect(url_for("index"))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


def _session_uid() -> str:
    """Jeder Besucher bekommt über die (Login-freie) Session eine eigene ID,
    damit niemand die Berechnung/den PDF-Export/die DXF eines anderen sieht."""
    if "uid" not in session:
        session["uid"] = uuid.uuid4().hex
        session.permanent = True
    return session["uid"]


def _session_result_path() -> str:
    return os.path.join(RESULTS_DIR, f"{_session_uid()}.json")


def _session_dxf_path() -> str:
    """Kopie der zuletzt berechneten DXF pro Session - wird nur gebraucht,
    damit der Kunde sie beim Senden per Mail als Anhang mitschicken kann."""
    return os.path.join(RESULTS_DIR, f"{_session_uid()}.dxf")


def _new_captcha() -> tuple[int, int]:
    """Einfache, selbst gehostete Rechenaufgabe statt Google reCAPTCHA o.ae. -
    kein Drittanbieter, keine Zugangsdaten noetig; schuetzt den Mailversand
    vor simplen Spam-Bots (nicht vor gezielten Angriffen)."""
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return a, b


def _session_preview_path() -> str:
    """PNG-Vorschau des Bauteils pro Session - wird im PDF-Export eingebettet
    und beim Mailversand als zusätzlicher Bild-Anhang mitgeschickt."""
    return os.path.join(RESULTS_DIR, f"{_session_uid()}.png")


def _load_material_groups() -> list[dict]:
    """Lädt die Materialgruppen und überspringt defensiv Einträge in einem
    alten Format (z.B. Reste des früheren CSV-Imports vor der Gruppen-
    Verwaltung - flache Material/Stärke-Dicts ohne "id"/"staerken"). Ohne
    diesen Filter bleiben solche Reste unbemerkt liegen: die Kundenliste
    wirkt leer und Admin-Links auf die (fehlende) Gruppen-ID brechen mit
    404. Wird hier eine Gruppe gespeichert, fallen die alten Einträge dabei
    automatisch raus."""
    raw = storage.load_json(MATERIALS_PATH, default=[])
    return [g for g in raw if isinstance(g, dict) and "id" in g and "staerken" in g]


def _flatten_materials(groups: list[dict]) -> list[dict]:
    """Wandelt die im Admin-Bereich gepflegte Gruppen-Struktur (Hauptgruppe
    z.B. "VA" + mehrere Stärken je eigener Schnittgeschwindigkeit/
    Einstechzeit) in eine flache Liste um - ein Eintrag pro Material/Stärke-
    Kombination, wie sie das Kundenformular (Dropdown) und /berechnen per
    Index erwarten."""
    flat = []
    for gruppe in groups:
        for st in gruppe.get("staerken", []):
            flat.append({
                "name": gruppe.get("gruppe", ""),
                "staerke_mm": st.get("staerke_mm", 0),
                "schnittgeschwindigkeit_mm_min": st.get("schnittgeschwindigkeit_mm_min", 0),
                "einstechzeit_s": st.get("einstechzeit_s", DEFAULT_EINSTECHZEIT_S),
                "preis_pro_kg": gruppe.get("preis_pro_kg", 0.0),
                "dichte_g_cm3": gruppe.get("dichte_g_cm3", 7.85),
            })
    return flat


def _load_legal() -> dict:
    """Mergt gespeicherte Werte über die Defaults, damit einzelne noch nie
    gespeicherte Felder (z.B. nach diesem Update neu hinzugekommene URL-
    Felder) trotzdem ihren Default zeigen, statt leer zu sein."""
    return {**DEFAULT_LEGAL, **storage.load_json(LEGAL_PATH, default={})}


def _cleanup_old_results(max_age_seconds: int = 24 * 3600) -> None:
    try:
        cutoff = time.time() - max_age_seconds
        for name in os.listdir(RESULTS_DIR):
            path = os.path.join(RESULTS_DIR, name)
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Startseite / Hauptformular
# --------------------------------------------------------------------------
@app.route("/")
def index():
    materials = _flatten_materials(_load_material_groups())
    return render_template(
        "index.html",
        materials=materials,
        has_materials=len(materials) > 0,
        schnittqualitaet=SCHNITTQUALITAET,
        schnittqualitaet_default=SCHNITTQUALITAET_DEFAULT,
        legal_ack=session.get("legal_ack", False),
        netto_hinweis=NETTO_HINWEIS,
    )


# --------------------------------------------------------------------------
# Rechtliches: Impressum/Datenschutz verweisen auf die Haupt-Domain, AGB
# bleiben als Freitext in der App (vom Admin gepflegt). Zusätzlich die
# einmalige Bestätigung, dass alle drei vor Nutzung des Rechners gelesen und
# verstanden wurden (Session-Flag, kein Login nötig).
# --------------------------------------------------------------------------
@app.route("/impressum")
def impressum():
    return redirect(_load_legal()["impressum_url"])


@app.route("/datenschutz")
def datenschutz():
    return redirect(_load_legal()["datenschutz_url"])


@app.route("/agb")
def agb():
    return render_template("legal_page.html", title="AGB", text=_load_legal()["agb"])


@app.route("/rechtliches/bestaetigen", methods=["POST"])
def rechtliches_bestaetigen():
    session["legal_ack"] = True
    session.permanent = True
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# DXF-Vorschau (AJAX, vor der eigentlichen Berechnung): liefert SVG,
# Layer-Liste und erkannte Einstichzahl, damit im Formular Konturen (z.B.
# Beschriftungslayer) vor dem Schneiden abgewählt werden können.
# --------------------------------------------------------------------------
@app.route("/dxf/preview", methods=["POST"])
def dxf_preview():
    if not session.get("legal_ack"):
        return jsonify({"error": "Bitte zuerst Datenschutzerklärung/Impressum bestätigen."}), 403

    dxf_file = request.files.get("dxf_file")
    if not dxf_file or dxf_file.filename == "":
        return jsonify({"error": "Keine Datei angegeben."}), 400
    if not dxf_file.filename.lower().endswith(".dxf"):
        return jsonify({"error": "Bitte eine DXF-Datei (.dxf) hochladen."}), 400

    filename = f"preview_{uuid.uuid4().hex}.dxf"
    filepath = os.path.join(UPLOAD_DIR, filename)
    dxf_file.save(filepath)

    try:
        layers = dxf.list_layers(filepath)
        svg = dxf.render_svg_preview(filepath)
        einstiche = dxf.analyze_dxf(filepath)["einstiche"]
    except Exception as e:
        return jsonify({"error": f"DXF konnte nicht gelesen werden: {e}"}), 400
    finally:
        os.remove(filepath)

    return jsonify({"layers": layers, "svg": svg, "einstiche": einstiche})


# --------------------------------------------------------------------------
# Kostenberechnung
# --------------------------------------------------------------------------
@app.route("/berechnen", methods=["POST"])
def berechnen():
    if not session.get("legal_ack"):
        flash("Bitte zuerst Datenschutzerklärung und Impressum bestätigen.")
        return redirect(url_for("index"))

    admin_settings = storage.load_json(SETTINGS_PATH, default=DEFAULT_SETTINGS)

    # Parameter aus dem Formular
    def to_float(name, default=0.0):
        val = request.form.get(name, "").strip().replace(",", ".")
        try:
            return float(val)
        except ValueError:
            return default

    berechnungsart = request.form.get("berechnungsart", "dxf")

    if berechnungsart == "manuell":
        schnittlaenge_mm = to_float("manuelle_schnittlaenge_mm", 0.0)
        if schnittlaenge_mm <= 0:
            flash("Bitte eine Schnittlänge größer 0 mm eingeben.")
            return redirect(url_for("index"))

        flaeche_m2 = to_float("manuelle_flaeche_m2", 0.0)
        # Ohne DXF gibt es keine Geometrie, aus der sich Einstiche erkennen
        # ließen - hier zählt der Kunde selbst, wie viele Konturen/Löcher
        # geschnitten werden.
        einstiche = max(0, int(to_float("manuelle_einstiche", 1)))
        geo = {
            "width_mm": None,
            "height_mm": None,
            "area_mm2": flaeche_m2 * 1_000_000,
            "area_m2": flaeche_m2,
            "total_cut_length_mm": schnittlaenge_mm,
            "entity_count": None,
            "skipped_entity_count": None,
            "einstiche": einstiche,
        }
        svg_preview = ""
        dateiname = "Manuelle Eingabe"
        # Keine DXF diesmal - eine evtl. von einer frueheren Berechnung dieser
        # Session liegende Kopie darf beim Senden nicht mehr angehaengt werden.
        stale_dxf_copy = _session_dxf_path()
        if os.path.exists(stale_dxf_copy):
            os.remove(stale_dxf_copy)
        stale_preview = _session_preview_path()
        if os.path.exists(stale_preview):
            os.remove(stale_preview)
    else:
        dxf_file = request.files.get("dxf_file")
        if not dxf_file or dxf_file.filename == "":
            flash("Bitte eine DXF-Datei hochladen.")
            return redirect(url_for("index"))
        if not dxf_file.filename.lower().endswith(".dxf"):
            flash("Bitte eine DXF-Datei (.dxf) hochladen.")
            return redirect(url_for("index"))

        if request.form.get("rechte_bestaetigt") != "on":
            flash("Bitte bestätigen, dass die hochgeladene Datei keine Rechte Dritter verletzt.")
            return redirect(url_for("index"))

        filename = f"teil_{uuid.uuid4().hex}.dxf"
        filepath = os.path.join(UPLOAD_DIR, filename)
        dxf_file.save(filepath)

        # Layer-Auswahl aus der Vorschau: nur gesetzt, wenn das Formular die
        # Checkboxen tatsächlich gerendert hat (sonst None = keine Filterung).
        included_layers = None
        if request.form.get("layers_filter_active"):
            included_layers = request.form.getlist("included_layers")

        try:
            geo = dxf.analyze_dxf(filepath, included_layers=included_layers)
            svg_preview = dxf.render_svg_preview(filepath, included_layers=included_layers)
            # Kopie pro Session behalten, damit der Kunde sie später per Mail
            # mitschicken kann (siehe /auftrag/senden) - andere Besucher sehen
            # diese Datei nicht, da sie unter der eigenen Session-UID liegt.
            shutil.copyfile(filepath, _session_dxf_path())
            png_bytes = dxf.render_png_preview(filepath, included_layers=included_layers)
            if png_bytes:
                with open(_session_preview_path(), "wb") as f:
                    f.write(png_bytes)
        except Exception as e:
            flash(f"DXF konnte nicht gelesen werden: {e}")
            return redirect(url_for("index"))
        finally:
            # Nur lokal für die Analyse gebraucht - bei mehreren Nutzern auf
            # einem gemeinsamen Server sollen fremde DXF-Dateien nicht liegen
            # bleiben.
            if os.path.exists(filepath):
                os.remove(filepath)

        einstiche = geo["einstiche"]
        dateiname = dxf_file.filename

    material_berechnet = request.form.get("material_berechnen") == "on"

    # Material ausschließlich serverseitig aus der Admin-Liste nachschlagen -
    # Preis, Schnittgeschwindigkeit, Einstechzeit und Dichte kommen NIE aus
    # dem Formular, sonst könnte jeder per direktem POST eigene Preise
    # unterschieben.
    materials = _flatten_materials(_load_material_groups())
    material = None
    material_index_raw = request.form.get("material_index", "").strip()
    if material_index_raw != "":
        try:
            idx = int(material_index_raw)
            if 0 <= idx < len(materials):
                material = materials[idx]
        except ValueError:
            pass

    if material is None:
        flash("Bitte ein Material aus der Liste auswählen.")
        return redirect(url_for("index"))

    # Dicke darf der Kunde anpassen (reale Blechdicke kann leicht vom
    # Listenwert abweichen), Preis/Geschwindigkeit/Dichte nicht.
    dicke_mm = to_float("dicke_mm", material["staerke_mm"])
    dichte = material.get("dichte_g_cm3", 7.85)
    material_preis_pro_kg = material["preis_pro_kg"]
    schnittgeschwindigkeit_basis = material["schnittgeschwindigkeit_mm_min"]

    schnittqualitaet_key = request.form.get("schnittqualitaet", SCHNITTQUALITAET_DEFAULT)
    if schnittqualitaet_key not in SCHNITTQUALITAET:
        schnittqualitaet_key = SCHNITTQUALITAET_DEFAULT
    schnittqualitaet_prozent = SCHNITTQUALITAET[schnittqualitaet_key]["prozent"]
    schnittgeschw = schnittgeschwindigkeit_basis * (schnittqualitaet_prozent / 100)

    # Feste, vom Admin hinterlegte Kostenparameter - der Kunde sieht/ändert
    # diese nicht über das Formular.
    stundensatz = admin_settings.get("maschinenstundensatz_eur", DEFAULT_SETTINGS["maschinenstundensatz_eur"])
    ruestzeit_min = admin_settings.get("ruestzeit_min", DEFAULT_SETTINGS["ruestzeit_min"])
    # Einstechzeit haengt von der Materialstaerke ab (dickeres Blech braucht
    # laenger zum Einstechen) - kommt deshalb aus der gewaehlten Staerke,
    # nicht mehr aus einer globalen Einstellung.
    einstechzeit_s = material.get("einstechzeit_s", DEFAULT_EINSTECHZEIT_S)

    stueckzahl = max(1, int(to_float("stueckzahl", 1)))

    # --- Blechverbrauch ---
    # Herleitung: Volumen[cm3] = Fläche[cm2] * Dicke[cm]; Masse[g] = Volumen * Dichte[g/cm3]
    if material_berechnet:
        flaeche_cm2 = geo["area_mm2"] / 100
        dicke_cm = dicke_mm / 10
        volumen_cm3 = flaeche_cm2 * dicke_cm
        gewicht_kg = (volumen_cm3 * dichte) / 1000
        materialkosten = gewicht_kg * material_preis_pro_kg
    else:
        # Kunde bringt eigenes Material mit -> keine Materialkosten ansetzen
        gewicht_kg = 0.0
        materialkosten = 0.0

    # --- Schnittzeit (pro Teil) ---
    schnittzeit_min = (
        geo["total_cut_length_mm"] / schnittgeschw if schnittgeschw > 0 else 0
    )
    einstechzeit_min = (einstiche * einstechzeit_s) / 60
    maschinenzeit_min = schnittzeit_min + einstechzeit_min
    maschinenkosten = (maschinenzeit_min / 60) * stundensatz

    # Material- und Maschinenkosten fallen pro Teil an, die Rüstzeit nur einmal
    # für den gesamten Auftrag unabhängig von der Stückzahl.
    materialkosten_gesamt = materialkosten * stueckzahl
    maschinenkosten_gesamt = maschinenkosten * stueckzahl
    ruestkosten = (ruestzeit_min / 60) * stundensatz

    gesamtkosten = materialkosten_gesamt + maschinenkosten_gesamt + ruestkosten

    result = {
        "geo": geo,
        "svg_preview": svg_preview,
        "material_berechnet": material_berechnet,
        "material_name": material["name"],
        "dicke_mm": dicke_mm,
        "dichte": dichte,
        "gewicht_kg": round(gewicht_kg, 3),
        "material_preis_pro_kg": material_preis_pro_kg,
        "schnittqualitaet_label": SCHNITTQUALITAET[schnittqualitaet_key]["label"],
        "schnittgeschwindigkeit_effektiv": round(schnittgeschw, 1),
        "stueckzahl": stueckzahl,
        "materialkosten": round(materialkosten, 2),
        "materialkosten_gesamt": round(materialkosten_gesamt, 2),
        "einstiche": einstiche,
        "schnittzeit_min": round(schnittzeit_min, 2),
        "einstechzeit_min": round(einstechzeit_min, 2),
        "maschinenzeit_min": round(maschinenzeit_min, 2),
        "maschinenkosten": round(maschinenkosten, 2),
        "maschinenkosten_gesamt": round(maschinenkosten_gesamt, 2),
        "ruestkosten": round(ruestkosten, 2),
        "gesamtkosten": round(gesamtkosten, 2),
    }

    # Pro Besucher (Session) gespeichert, damit niemand die Berechnung eines
    # anderen sieht - siehe _session_result_path().
    _cleanup_old_results()
    storage.save_json({"result": result, "dateiname": dateiname}, _session_result_path())

    captcha_a, captcha_b = _new_captcha()
    return render_template(
        "result.html",
        r=result,
        dateiname=dateiname,
        mail_configured=mailer.is_configured(),
        captcha_a=captcha_a,
        captcha_b=captcha_b,
        netto_hinweis=NETTO_HINWEIS,
    )


@app.route("/export/pdf")
def export_pdf():
    data = storage.load_json(_session_result_path(), default=None)
    if not data:
        flash("Keine Berechnung zum Exportieren vorhanden.")
        return redirect(url_for("index"))

    preview_path = _session_preview_path()
    if not os.path.exists(preview_path):
        preview_path = None

    pdf_bytes = pdf_export.build_pdf(data["result"], data["dateiname"], preview_path)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=kalkulation.pdf"},
    )


# --------------------------------------------------------------------------
# Unverbindliche Angebotsanfrage per E-Mail an den Betreiber senden (PDF +
# ggf. Original-DXF als Anhang) - kein verbindlicher Auftrag, nur eine
# Anfrage. Zieladresse/SMTP-Zugangsdaten kommen aus der Umgebung (.env),
# stehen also nicht im Quellcode.
# --------------------------------------------------------------------------
@app.route("/auftrag/senden", methods=["POST"])
def auftrag_senden():
    data = storage.load_json(_session_result_path(), default=None)
    if not data:
        flash("Keine Berechnung zum Senden vorhanden.")
        return redirect(url_for("index"))

    result, dateiname = data["result"], data["dateiname"]

    if not mailer.is_configured():
        flash("E-Mail-Versand ist noch nicht eingerichtet. Bitte den Betreiber kontaktieren.")
        return render_template(
            "result.html", r=result, dateiname=dateiname, mail_configured=False, netto_hinweis=NETTO_HINWEIS
        )

    captcha_antwort = request.form.get("captcha_antwort", "").strip()
    try:
        captcha_ok = int(captcha_antwort) == session.get("captcha_answer")
    except ValueError:
        captcha_ok = False

    if not captcha_ok:
        flash("Sicherheitsfrage falsch beantwortet. Bitte erneut versuchen.")
        captcha_a, captcha_b = _new_captcha()
        return render_template(
            "result.html",
            r=result,
            dateiname=dateiname,
            mail_configured=True,
            captcha_a=captcha_a,
            captcha_b=captcha_b,
            netto_hinweis=NETTO_HINWEIS,
        )
    session.pop("captcha_answer", None)

    dxf_path = _session_dxf_path()
    if not os.path.exists(dxf_path):
        dxf_path = None

    preview_path = _session_preview_path()
    if not os.path.exists(preview_path):
        preview_path = None

    kunde_name = request.form.get("kunde_name", "").strip()
    kunde_email = request.form.get("kunde_email", "").strip()
    kunde_notiz = request.form.get("kunde_notiz", "").strip()

    try:
        mailer.send_offer_request_email(
            result, dateiname, dxf_path, preview_path, kunde_name, kunde_email, kunde_notiz
        )
    except Exception as e:
        flash(f"E-Mail konnte nicht gesendet werden: {e}")
        captcha_a, captcha_b = _new_captcha()
        return render_template(
            "result.html",
            r=result,
            dateiname=dateiname,
            mail_configured=True,
            captcha_a=captcha_a,
            captcha_b=captcha_b,
            netto_hinweis=NETTO_HINWEIS,
        )

    # Bestaetigung an den Kunden ist ein Best-effort-Extra - schlaegt sie
    # fehl, ist die eigentliche Anfrage an den Betreiber trotzdem schon raus,
    # das soll dem Kunden nicht als Fehler angezeigt werden.
    bestaetigung_gesendet = False
    if kunde_email:
        try:
            mailer.send_customer_confirmation_email(result, dateiname, preview_path, kunde_name, kunde_email)
            bestaetigung_gesendet = True
        except Exception:
            pass

    flash("Unverbindliche Anfrage wurde per E-Mail gesendet.")
    return render_template(
        "result.html",
        r=result,
        dateiname=dateiname,
        mail_configured=True,
        gesendet=True,
        bestaetigung_gesendet=bestaetigung_gesendet,
        netto_hinweis=NETTO_HINWEIS,
    )


# --------------------------------------------------------------------------
# Admin-Bereich: Materialgruppen (mit Stärken) und feste Kostenparameter,
# die Kunden nicht sehen/ändern sollen. Kein Login für normale Nutzung
# nötig - nur hier.
# --------------------------------------------------------------------------
@app.route("/admin/setup", methods=["GET", "POST"])
def admin_setup():
    if storage.load_json(ADMIN_PATH, default=None):
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if len(password) < 4:
            flash("Passwort muss mindestens 4 Zeichen haben.")
            return render_template("admin_setup.html")
        if password != password2:
            flash("Passwörter stimmen nicht überein.")
            return render_template("admin_setup.html")

        storage.save_json({"password_hash": generate_password_hash(password)}, ADMIN_PATH)
        session["is_admin"] = True
        flash("Admin-Passwort eingerichtet.")
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_setup.html")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    admin_data = storage.load_json(ADMIN_PATH, default=None)
    if not admin_data:
        return redirect(url_for("admin_setup"))

    if request.method == "POST":
        password = request.form.get("password", "")
        employee_hash = admin_data.get("employee_password_hash")
        if check_password_hash(admin_data["password_hash"], password) or (
            employee_hash and check_password_hash(employee_hash, password)
        ):
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Falsches Passwort.")

    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    groups = _load_material_groups()
    settings = storage.load_json(SETTINGS_PATH, default=DEFAULT_SETTINGS)
    legal = _load_legal()
    admin_data = storage.load_json(ADMIN_PATH, default={})
    return render_template(
        "admin.html",
        groups=groups,
        settings=settings,
        legal=legal,
        has_employee_password=bool(admin_data.get("employee_password_hash")),
    )


@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_settings_save():
    def to_float(name, default=0.0):
        val = request.form.get(name, "").strip().replace(",", ".")
        try:
            return float(val)
        except ValueError:
            return default

    settings = {
        "maschinenstundensatz_eur": to_float(
            "maschinenstundensatz_eur", DEFAULT_SETTINGS["maschinenstundensatz_eur"]
        ),
        "ruestzeit_min": to_float("ruestzeit_min", DEFAULT_SETTINGS["ruestzeit_min"]),
        "max_upload_mb": max(1, to_float("max_upload_mb", DEFAULT_SETTINGS["max_upload_mb"])),
    }
    storage.save_json(settings, SETTINGS_PATH)
    flash("Einstellungen gespeichert.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/rechtstexte", methods=["POST"])
@admin_required
def admin_rechtstexte_save():
    legal = {
        "impressum_url": request.form.get("impressum_url", "").strip(),
        "datenschutz_url": request.form.get("datenschutz_url", "").strip(),
        "agb": request.form.get("agb", "").strip(),
    }
    storage.save_json(legal, LEGAL_PATH)
    flash("Rechtliches gespeichert.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/mitarbeiterpasswort", methods=["POST"])
@admin_required
def admin_mitarbeiterpasswort_save():
    admin_data = storage.load_json(ADMIN_PATH, default={})

    if request.form.get("action") == "entfernen":
        admin_data.pop("employee_password_hash", None)
        storage.save_json(admin_data, ADMIN_PATH)
        flash("Mitarbeiterpasswort entfernt.")
        return redirect(url_for("admin_dashboard"))

    password = request.form.get("password", "")
    password2 = request.form.get("password2", "")
    if len(password) < 4:
        flash("Mitarbeiterpasswort muss mindestens 4 Zeichen haben.")
        return redirect(url_for("admin_dashboard"))
    if password != password2:
        flash("Mitarbeiterpasswörter stimmen nicht überein.")
        return redirect(url_for("admin_dashboard"))
    if check_password_hash(admin_data["password_hash"], password):
        flash("Mitarbeiterpasswort darf nicht mit deinem eigenen Passwort übereinstimmen.")
        return redirect(url_for("admin_dashboard"))

    admin_data["employee_password_hash"] = generate_password_hash(password)
    storage.save_json(admin_data, ADMIN_PATH)
    flash("Mitarbeiterpasswort gespeichert.")
    return redirect(url_for("admin_dashboard"))


def _to_float_form(name, default=0.0):
    val = request.form.get(name, "").strip().replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return default


@app.route("/admin/materialien/gruppe/neu", methods=["POST"])
@admin_required
def material_gruppe_neu():
    name = request.form.get("gruppe", "").strip()
    if not name:
        flash("Bitte einen Namen für die Materialgruppe angeben.")
        return redirect(url_for("admin_dashboard"))

    groups = _load_material_groups()
    groups.append({
        "id": uuid.uuid4().hex,
        "gruppe": name,
        "preis_pro_kg": _to_float_form("preis_pro_kg", 0.0),
        "dichte_g_cm3": _to_float_form("dichte_g_cm3", 7.85),
        "staerken": [],
    })
    storage.save_json(groups, MATERIALS_PATH)
    flash(f"Materialgruppe '{name}' angelegt.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/materialien/gruppe/<gruppe_id>/bearbeiten", methods=["POST"])
@admin_required
def material_gruppe_bearbeiten(gruppe_id):
    groups = _load_material_groups()
    for g in groups:
        if g.get("id") == gruppe_id:
            name = request.form.get("gruppe", "").strip()
            if name:
                g["gruppe"] = name
            g["preis_pro_kg"] = _to_float_form("preis_pro_kg", g.get("preis_pro_kg", 0.0))
            g["dichte_g_cm3"] = _to_float_form("dichte_g_cm3", g.get("dichte_g_cm3", 7.85))
            break
    storage.save_json(groups, MATERIALS_PATH)
    flash("Materialgruppe aktualisiert.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/materialien/gruppe/<gruppe_id>/loeschen", methods=["POST"])
@admin_required
def material_gruppe_loeschen(gruppe_id):
    groups = _load_material_groups()
    groups = [g for g in groups if g.get("id") != gruppe_id]
    storage.save_json(groups, MATERIALS_PATH)
    flash("Materialgruppe gelöscht.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/materialien/gruppe/<gruppe_id>/staerke/neu", methods=["POST"])
@admin_required
def material_staerke_neu(gruppe_id):
    groups = _load_material_groups()
    for g in groups:
        if g.get("id") == gruppe_id:
            g.setdefault("staerken", []).append({
                "staerke_mm": _to_float_form("staerke_mm", 0.0),
                "schnittgeschwindigkeit_mm_min": _to_float_form("schnittgeschwindigkeit_mm_min", 0.0),
                "einstechzeit_s": _to_float_form("einstechzeit_s", DEFAULT_EINSTECHZEIT_S),
            })
            break
    else:
        flash("Materialgruppe nicht gefunden.")
        return redirect(url_for("admin_dashboard"))

    storage.save_json(groups, MATERIALS_PATH)
    flash("Stärke hinzugefügt.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/materialien/gruppe/<gruppe_id>/staerke/<int:idx>/bearbeiten", methods=["POST"])
@admin_required
def material_staerke_bearbeiten(gruppe_id, idx):
    groups = _load_material_groups()
    for g in groups:
        if g.get("id") == gruppe_id:
            staerken = g.get("staerken", [])
            if 0 <= idx < len(staerken):
                staerken[idx] = {
                    "staerke_mm": _to_float_form("staerke_mm", staerken[idx].get("staerke_mm", 0.0)),
                    "schnittgeschwindigkeit_mm_min": _to_float_form(
                        "schnittgeschwindigkeit_mm_min", staerken[idx].get("schnittgeschwindigkeit_mm_min", 0.0)
                    ),
                    "einstechzeit_s": _to_float_form(
                        "einstechzeit_s", staerken[idx].get("einstechzeit_s", DEFAULT_EINSTECHZEIT_S)
                    ),
                }
            break
    storage.save_json(groups, MATERIALS_PATH)
    flash("Stärke aktualisiert.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/materialien/gruppe/<gruppe_id>/staerke/<int:idx>/loeschen", methods=["POST"])
@admin_required
def material_staerke_loeschen(gruppe_id, idx):
    groups = _load_material_groups()
    for g in groups:
        if g.get("id") == gruppe_id:
            staerken = g.get("staerken", [])
            if 0 <= idx < len(staerken):
                staerken.pop(idx)
            break
    storage.save_json(groups, MATERIALS_PATH)
    flash("Stärke entfernt.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/materialien/reset", methods=["POST"])
@admin_required
def materialien_reset():
    if os.path.exists(MATERIALS_PATH):
        os.remove(MATERIALS_PATH)
    flash("Alle Materialien gelöscht.")
    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
