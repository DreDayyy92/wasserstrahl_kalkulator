FROM python:3.12-slim

WORKDIR /app

# build-essential sorgt dafuer, dass pip Pakete notfalls aus dem Quellcode
# bauen kann, falls fuer eine Abhaengigkeit (z.B. ezdxf) kein passendes
# vorgebautes Wheel fuer dieses Image existiert. Kostet etwas Image-Groesse,
# dafuer baut es zuverlaessig durch.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py storage.py materials_import.py dxf_analyzer.py pdf_export.py ./
COPY templates/ templates/
COPY static/ static/

# instance/ (Materialliste, Admin-Passwort, Settings, Session-Ergebnisse)
# und uploads/ werden zur Laufzeit von app.py angelegt - im
# docker-compose.yml als Volumes gemountet, sonst sind sie nach jedem
# Container-Neustart weg.
ENV PYTHONUNBUFFERED=1

EXPOSE 5050

CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "app:app"]
