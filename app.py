import os
import secrets
import time
import uuid
from functools import wraps

from flask import Flask, jsonify, render_template, request, redirect, url_for, flash, Response, session
from werkzeug.security import generate_password_hash, check_password_hash

import dxf_analyzer as dxf
import materials_import
import pdf_export
import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
RESULTS_DIR = os.path.join(INSTANCE_DIR, "results")
MATERIALS_PATH = os.path.join(INSTANCE_DIR, "materials.json")
SETTINGS_PATH = os.path.join(INSTANCE_DIR, "settings.json")
ADMIN_PATH = os.path.join(INSTANCE_DIR, "admin.json")
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
    "schnittgeschwindigkeit_prozent": 50,  # in der Praxis wird meist mit ~50% des Listenwerts geschnitten
    "maschinenstundensatz_eur": 45.0,
    "ruestzeit_min": 10.0,
    "einstechzeit_s": 15,
}


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


def _session_result_path() -> str:
    """Jeder Besucher bekommt über die (Login-freie) Session eine eigene ID,
    damit niemand die Berechnung/den PDF-Export eines anderen sieht."""
    if "uid" not in session:
        session["uid"] = uuid.uuid4().hex
        session.permanent = True
    return os.path.join(RESULTS_DIR, f"{session['uid']}.json")


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
    materials = storage.load_json(MATERIALS_PATH, default=[])
    settings = storage.load_json(SETTINGS_PATH, default=DEFAULT_SETTINGS)
    return render_template(
        "index.html",
        materials=materials,
        settings=settings,
        has_materials=len(materials) > 0,
    )


# --------------------------------------------------------------------------
# DXF-Vorschau (AJAX, vor der eigentlichen Berechnung): liefert SVG,
# Layer-Liste und erkannte Einstichzahl, damit im Formular Konturen (z.B.
# Beschriftungslayer) vor dem Schneiden abgewählt werden können.
# --------------------------------------------------------------------------
@app.route("/dxf/preview", methods=["POST"])
def dxf_preview():
    dxf_file = request.files.get("dxf_file")
    if not dxf_file or dxf_file.filename == "":
        return jsonify({"error": "Keine Datei angegeben."}), 400

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
    else:
        dxf_file = request.files.get("dxf_file")
        if not dxf_file or dxf_file.filename == "":
            flash("Bitte eine DXF-Datei hochladen.")
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

    dicke_mm = to_float("dicke_mm", 0.0)
    dichte = to_float("dichte_g_cm3", admin_settings.get("dichte_g_cm3", 7.85))
    # Schnittgeschwindigkeit aus Material/Liste ist ein Nennwert (100%). In der
    # Praxis wird nur ein Teil davon gefahren - der Anteil ist ausschließlich
    # vom Admin einstellbar, nicht vom Kunden.
    schnittgeschwindigkeit_basis = to_float(
        "schnittgeschwindigkeit_mm_min",
        DEFAULT_SETTINGS.get("schnittgeschwindigkeit_mm_min", 500),
    )
    schnittgeschwindigkeit_prozent = admin_settings.get(
        "schnittgeschwindigkeit_prozent", DEFAULT_SETTINGS["schnittgeschwindigkeit_prozent"]
    )
    schnittgeschw = schnittgeschwindigkeit_basis * (schnittgeschwindigkeit_prozent / 100)

    # Feste, vom Admin hinterlegte Kostenparameter - der Kunde sieht/ändert
    # diese nicht über das Formular.
    stundensatz = admin_settings.get("maschinenstundensatz_eur", DEFAULT_SETTINGS["maschinenstundensatz_eur"])
    ruestzeit_min = admin_settings.get("ruestzeit_min", DEFAULT_SETTINGS["ruestzeit_min"])
    einstechzeit_s = admin_settings.get("einstechzeit_s", DEFAULT_SETTINGS["einstechzeit_s"])

    material_preis_pro_kg = to_float("material_preis_pro_kg", 0.0)
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
        "dicke_mm": dicke_mm,
        "dichte": dichte,
        "gewicht_kg": round(gewicht_kg, 3),
        "material_preis_pro_kg": material_preis_pro_kg,
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

    return render_template("result.html", r=result, dateiname=dateiname)


@app.route("/export/pdf")
def export_pdf():
    data = storage.load_json(_session_result_path(), default=None)
    if not data:
        flash("Keine Berechnung zum Exportieren vorhanden.")
        return redirect(url_for("index"))

    pdf_bytes = pdf_export.build_pdf(data["result"], data["dateiname"])
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=kalkulation.pdf"},
    )


# --------------------------------------------------------------------------
# Admin-Bereich: Materialliste (CSV) und feste Kostenparameter, die Kunden
# nicht sehen/ändern sollen. Kein Login für normale Nutzung nötig - nur hier.
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
        if check_password_hash(admin_data["password_hash"], password):
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
    materials = storage.load_json(MATERIALS_PATH, default=[])
    settings = storage.load_json(SETTINGS_PATH, default=DEFAULT_SETTINGS)
    return render_template(
        "admin.html",
        materials=materials,
        has_materials=len(materials) > 0,
        settings=settings,
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
        "schnittgeschwindigkeit_prozent": to_float(
            "schnittgeschwindigkeit_prozent", DEFAULT_SETTINGS["schnittgeschwindigkeit_prozent"]
        ),
        "maschinenstundensatz_eur": to_float(
            "maschinenstundensatz_eur", DEFAULT_SETTINGS["maschinenstundensatz_eur"]
        ),
        "ruestzeit_min": to_float("ruestzeit_min", DEFAULT_SETTINGS["ruestzeit_min"]),
        "einstechzeit_s": to_float("einstechzeit_s", DEFAULT_SETTINGS["einstechzeit_s"]),
    }
    storage.save_json(settings, SETTINGS_PATH)
    flash("Einstellungen gespeichert.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/materialien/upload", methods=["POST"])
@admin_required
def materialien_upload():
    file = request.files.get("materialien_file")
    if not file or file.filename == "":
        flash("Bitte eine Material-CSV-Datei auswählen.")
        return redirect(url_for("admin_dashboard"))

    filename = f"materialien_{uuid.uuid4().hex}.csv"
    filepath = os.path.join(UPLOAD_DIR, filename)
    file.save(filepath)

    try:
        materials = materials_import.parse_csv(filepath)
    except Exception as e:
        flash(f"CSV konnte nicht gelesen werden: {e}")
        return redirect(url_for("admin_dashboard"))
    finally:
        os.remove(filepath)

    if not materials:
        flash("Keine Materialien in der Datei gefunden.")
        return redirect(url_for("admin_dashboard"))

    storage.save_json(materials, MATERIALS_PATH)
    flash(f"{len(materials)} Materialien importiert.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/materialien/vorlage")
@admin_required
def materialien_vorlage():
    csv_text = materials_import.build_template_csv()
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=materialien_vorlage.csv"},
    )


@app.route("/admin/materialien/reset", methods=["POST"])
@admin_required
def materialien_reset():
    if os.path.exists(MATERIALS_PATH):
        os.remove(MATERIALS_PATH)
    flash("Materialliste gelöscht.")
    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
