# Wasserstrahl-Kalkulator

Lokale Flask-Web-App zur Kostenberechnung für Wasserstrahl-Zuschnitte:
Materialpreise (CSV-Import) + DXF-Bauteilgeometrie (Bounding Box + Schnittlänge)
oder manuelle Schnittlänge → Material- und Maschinenkosten, inkl. PDF-Export.

## Installation

```bash
cd wasserstrahl_kalkulator
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Starten

```bash
python app.py
```

Dann im Browser öffnen: http://localhost:5050

Läuft genauso auf einem Raspberry Pi im Netzwerk – dann von einem anderen
Rechner über `http://<pi-ip>:5050` erreichbar (Server bindet auf `0.0.0.0`).
Für einen dauerhaften Server-Betrieb (z.B. Proxmox/Docker) ist das die
vorgesehene Betriebsart - es gibt keine separate Desktop-Anwendung mehr,
nur diesen Flask-Server.

## Mit Docker betreiben

```bash
docker compose up -d --build
```

Läuft danach unter `http://<server-ip>:5050` (Gunicorn, 2 Worker). Die
Ordner `instance/` (Materialliste, Admin-Passwort, Settings, Session-
Ergebnisse) und `uploads/` werden als Volumes gemountet - bleiben also bei
`docker compose down` / Neu-Build erhalten. Updates einspielen: neue Dateien
in den Ordner kopieren (oder `git pull`), dann `docker compose up -d --build`
erneut ausführen.

Nur `docker build`/`docker run` ohne Compose:

```bash
docker build -t wasserstrahl-kalkulator .
docker run -d -p 5050:5050 \
    -v "$(pwd)/instance:/app/instance" -v "$(pwd)/uploads:/app/uploads" \
    --name wasserstrahl-kalkulator wasserstrahl-kalkulator
```

## In einem Proxmox-LXC betreiben

Docker läuft standardmäßig NICHT in einem LXC-Container (im Gegensatz zu
einer VM) - dafür muss im Container-Feature "Nesting" aktiviert werden.

1. **LXC-Container erstellen** (Proxmox-Weboberfläche, "CT erstellen"):
   - Template: Debian 12 oder Ubuntu 22.04/24.04.
   - 1-2 CPU-Kerne, 1-2 GB RAM reichen für dieses Tool.
   - **Wichtig**: nach dem Erstellen (oder direkt beim Erstellen unter
     "Erweitert") bei den Container-**Optionen → Features** `Nesting: 1`
     aktivieren (bei unprivilegierten Containern zusätzlich `keyctl: 1`).
     Ohne das startet der Docker-Daemon im Container nicht.
   - Danach den Container einmal neu starten, falls er schon lief.

2. **Docker im Container installieren** (Konsole öffnen, z.B. über
   `pct enter <VMID>` auf dem Proxmox-Host oder die Web-Konsole):
   ```bash
   apt update && apt install -y curl
   curl -fsSL https://get.docker.com | sh
   ```
   Das ist Docker's offizielles Installationsskript, installiert Docker
   Engine + das `docker compose`-Plugin.

3. **Projekt in den Container kopieren**, z.B. per SCP vom Windows-PC aus
   (im Container vorher `apt install -y openssh-server`, falls noch kein
   SSH läuft):
   ```powershell
   scp -r wasserstrahl_kalkulator root@<lxc-ip>:/opt/
   ```
   Alternativ über Proxmox' eigenen Datei-Upload/`pct push`, oder später
   per `git pull`, falls das Projekt in einem Git-Repo liegt.

4. **Bauen und starten**:
   ```bash
   cd /opt/wasserstrahl_kalkulator
   docker compose up -d --build
   ```

5. **Testen**: `http://<lxc-ip>:5050` im Browser öffnen.

**Nur für den eigenen Netzwerkzugriff** (z.B. Werkstatt-LAN) reicht das so.
Soll die App wirklich **öffentlich aus dem Internet** erreichbar sein, vorher
unbedingt einen Reverse Proxy mit HTTPS davorschalten (z.B. Caddy oder nginx
+ Let's Encrypt) statt Port 5050 direkt am Router freizugeben - das ist ein
eigenes Thema, sag Bescheid, wenn wir das als Nächstes angehen sollen.

## Ablauf (Kunde/Nutzer - kein Login nötig)

1. **Teil erfassen** – zwei Berechnungsarten:
   - **DXF-Datei hochladen**: Vorschau erscheint sofort im Formular, Konturen
     (Layer) können per Checkbox von der Berechnung ausgeschlossen werden
     (z.B. Beschriftungen). Die App berechnet automatisch Bounding Box
     (Breite × Höhe → Blechfläche), Gesamt-Schnittlänge und **Anzahl
     Einstiche** (jede unabhängige geschlossene Kontur - Außenkontur oder
     Loch - zählt als ein Einstich).
   - **Manuelle Schnittlänge**: Schnittlänge, Blechfläche und Anzahl Einstiche
     direkt eingeben, ohne DXF-Datei (Einstiche können hier naturgemäß nicht
     automatisch erkannt werden).
2. **Parameter eingeben**: Stückzahl, Material aus der Liste auswählen
   (Preis/kg, Dichte und Listen-Schnittgeschwindigkeit kommen dabei fest aus
   der Admin-Materialliste und sind nicht änderbar; nur die tatsächliche
   Blechdicke lässt sich leicht anpassen), sowie Schnittqualität
   (Feinschnitt 50 % / Mittelschnitt 75 % / Trennschnitt 100 % der Listen-
   Schnittgeschwindigkeit). Über eine Checkbox lässt sich die
   Materialberechnung ganz abschalten (Kunde bringt eigenes Blech mit).
3. **Berechnen** → Ergebnisseite mit Aufschlüsselung: Materialkosten,
   Maschinenkosten je Stückzahl, Rüstkosten (einmalig), Gesamtkosten – inkl.
   PDF-Export und "Auftrag per E-Mail senden" (siehe unten). Jeder Besucher
   sieht nur seine eigene letzte Berechnung (Session-Cookie, kein Login
   nötig) - Maschinenstundensatz und Rüstzeit sind fest hinterlegt (siehe
   Admin-Bereich) und werden dem Kunden nicht angezeigt.

## Admin-Bereich

Über den Link "Admin" im Footer erreichbar (`/admin`). Beim allerersten
Aufruf wird einmalig ein Passwort festgelegt (`instance/admin.json`, nur ein
gehashtes Passwort, kein Klartext); danach normaler Login. Dort:

- **Feste Kostenparameter**: Maschinenstundensatz, Rüstzeit, Einstechzeit je
  Einstich - diese Werte sieht/ändert der Kunde nicht, sie fließen aber in
  jede Berechnung ein.
- **Materialliste (CSV)**: `Material;Staerke_mm;Schnittgeschwindigkeit_mm_min;Preis_pro_kg;Dichte_g_cm3`
  (Dichte optional, Standard 7.85), eine Zeile pro Material/Stärke-
  Kombination. Vorlage zum Download in der App. Nur der Admin kann sie
  hochladen/löschen; Kunden können daraus nur auswählen, Preis/Geschwindigkeit/
  Dichte kommen ausschließlich aus dieser Liste und sind für den Kunden nicht
  änderbar.

## Auftrag per E-Mail senden

Auf der Ergebnisseite kann der Kunde das berechnete Teil direkt per E-Mail als
Auftrag an den Betreiber schicken (PDF-Kalkulation + Original-DXF als Anhang,
optional Name/E-Mail/Anmerkung des Kunden als `Reply-To`).

Damit das funktioniert, müssen die SMTP-Zugangsdaten des Postfachs, über das
gesendet werden soll (z.B. `schneiden@baeckereitechnik-doerner.de`), einmalig
in einer lokalen `.env`-Datei hinterlegt werden:

```bash
cp .env.example .env
# .env dann mit einem Editor öffnen und die echten Werte eintragen:
#   SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, MAIL_FROM, MAIL_TO
```

Die Zugangsdaten/SMTP-Host/Serveradressen (z.B. `smtp.ionos.de`,
`smtp.strato.de` - steht im Kundenportal des jeweiligen Mail-Hosters unter
"Postausgangsserver"/"SMTP") stehen **nur** in dieser `.env`-Datei. Sie ist in
`.gitignore` eingetragen und wird nie mit committet/auf GitHub hochgeladen -
im Repo liegt nur `.env.example` als Vorlage ohne echte Werte. Bei Docker
Compose wird `.env` automatisch als `env_file` in den Container geladen (in
`docker-compose.yml` hinterlegt), ohne Neu-Build bei geänderten Werten -
`docker compose up -d` reicht. Ist keine `.env` vorhanden bzw. sind
`SMTP_HOST`/`MAIL_TO` nicht gesetzt, zeigt die Ergebnisseite statt des
Sende-Formulars nur einen Hinweis, dass der Versand noch nicht eingerichtet
ist - die restliche App funktioniert unabhängig davon normal weiter.

## Wichtige Vereinfachungen (bewusst, für den Start)

- **Blechverbrauch = Bounding Box**, keine Schachtelung mehrerer Teile auf
  einer Tafel. Für Einzelteil-Kalkulation ausreichend; bei Bedarf später als
  Nesting-Feature nachrüstbar.
- **Schnittzeit** ist eine lineare Näherung (Länge / Geschwindigkeit) ohne
  Beschleunigungs-/Verzögerungsrampen an Ecken – für eine Kostenschätzung
  i. d. R. genau genug.
- Nur schneidbare DXF-Geometrie (Linien, Bögen, Kreise, Polylinien, Splines,
  Ellipsen) fließt in Maße/Fläche/Schnittlänge ein – Text/Beschriftungen
  werden automatisch ignoriert.
- Preise/Einstellungen liegen als einfache JSON-Dateien im `instance/`-
  Ordner (kein Datenbank-Server nötig, für Ein-Personen-/Einzelplatz-Nutzung
  gedacht).

## Dateistruktur

```
Dockerfile               Container-Image (Python + Gunicorn)
docker-compose.yml       Build + Start inkl. Volumes für instance/ und uploads/, lädt .env
.env.example             Vorlage für SMTP-Zugangsdaten (echte Werte in .env, nicht im Git-Repo)
app.py                  Flask-Routen
storage.py                JSON-Persistenz-Helfer
materials_import.py      Material-CSV-Einlesen (Material/Stärke/Geschw./Preis/Dichte)
dxf_analyzer.py           DXF-Bounding-Box, Schnittlängen- und SVG-Vorschau-Berechnung
pdf_export.py             PDF-Kalkulationsblatt (fpdf2)
mailer.py                 Versand des Auftrags (PDF + DXF) per E-Mail an den Betreiber
templates/                HTML-Seiten
static/style.css          Styling
instance/                 gespeicherte Materialien/Settings/Admin-Passwort (entsteht zur Laufzeit)
instance/results/         letzte Berechnung + DXF-Kopie je Besucher-Session (Auto-Cleanup nach 24h)
uploads/                  hochgeladene Dateien (temporär)
```
