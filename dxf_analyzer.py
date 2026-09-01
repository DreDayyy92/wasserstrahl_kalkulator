"""
Analysiert eine DXF-Datei: Bounding Box (für Blechverbrauch) und
Gesamt-Schnittlänge (für Schnittzeit-Schätzung).

Bounding Box = kleinstes Rechteck um das Bauteil. Das ist eine bewusste
Vereinfachung (kein Schachteln mehrerer Teile), reicht aber für die
Kalkulation eines Einzelteils auf einer Blechtafel.

Nur tatsächlich schneidbare Geometrie (CUT_ENTITY_TYPES) fließt in Bounding
Box, Fläche und Schnittlänge ein. TEXT/MTEXT & Co. (z.B. Beschriftungen wie
"Nur für Lehrzwecke") würden sonst die Bounding Box verfälschen, da sie nicht
mitgeschnitten werden.
"""
from __future__ import annotations

from html import escape as _escape

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf import path as ezpath

CUT_ENTITY_TYPES = frozenset({
    "LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "SPLINE", "ELLIPSE",
})


def _entity_length(entity, flatten_distance: float = 0.05) -> float:
    """Approximiert die Länge eines beliebigen DXF-Entities über den
    Pfad-Flattening-Mechanismus von ezdxf (funktioniert für Linien,
    Bögen, Kreise, Polylinien, Splines, Ellipsen)."""
    try:
        p = ezpath.make_path(entity)
    except Exception:
        return 0.0
    points = list(p.flattening(flatten_distance))
    length = 0.0
    for a, b in zip(points, points[1:]):
        dx = b.x - a.x
        dy = b.y - a.y
        length += (dx * dx + dy * dy) ** 0.5
    return length


def _cut_entities(entities, included_layers=None):
    included = set(included_layers) if included_layers is not None else None
    result = []
    for e in entities:
        if e.dxftype() not in CUT_ENTITY_TYPES:
            continue
        if included is not None and e.dxf.layer not in included:
            continue
        result.append(e)
    return result


def list_layers(filepath: str) -> list[dict]:
    """Liste aller Layer mit schneidbarer Geometrie (für die Kontur-Auswahl
    in der Vorschau), inkl. Anzahl der Entities je Layer."""
    doc = ezdxf.readfile(filepath)
    msp = doc.modelspace()
    counts: dict[str, int] = {}
    for e in msp:
        if e.dxftype() not in CUT_ENTITY_TYPES:
            continue
        layer = e.dxf.layer
        counts[layer] = counts.get(layer, 0) + 1
    return [
        {"name": name, "entity_count": count}
        for name, count in sorted(counts.items())
    ]


def _endpoint_key(point, tol: float = 1e-3) -> tuple:
    return (round(point.x / tol), round(point.y / tol))


def count_piercings(entities) -> int:
    """Zählt unabhängige Konturen: pro Kontur (Außenkontur oder Loch) wird
    einmal eingestochen und dann einmal komplett entlang geschnitten. Nutzt
    Union-Find über die Endpunkte aller Entities (per Koordinate verbunden,
    mit Toleranz gegen Rundungsfehler); jede zusammenhängende Gruppe = 1
    Einstich - funktioniert unabhängig davon, ob eine Kontur aus einem Kreis,
    einer geschlossenen Polylinie oder mehreren einzelnen Linien/Bögen
    besteht, die Ende-an-Ende aneinanderhängen."""
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    nodes = set()
    for e in entities:
        try:
            p = ezpath.make_path(e)
        except Exception:
            continue
        start = _endpoint_key(p.start)
        end = _endpoint_key(p.end)
        union(start, end)
        nodes.add(start)
        nodes.add(end)

    return len({find(n) for n in nodes})


def analyze_dxf(filepath: str, included_layers=None) -> dict:
    doc = ezdxf.readfile(filepath)
    msp = doc.modelspace()
    all_entities = list(msp)
    cut_entities = _cut_entities(all_entities, included_layers)

    if not cut_entities:
        raise ValueError(
            "Keine schneidbare Geometrie gefunden (Kontur-Auswahl prüfen)."
        )

    extents = ezbbox.extents(cut_entities, fast=True)
    if not extents.has_data:
        raise ValueError("Konnte keine Geometrie in der DXF-Datei finden.")

    width_mm = extents.extmax.x - extents.extmin.x
    height_mm = extents.extmax.y - extents.extmin.y

    total_length_mm = 0.0
    for e in cut_entities:
        total_length_mm += _entity_length(e)

    area_mm2 = width_mm * height_mm

    return {
        "width_mm": round(width_mm, 2),
        "height_mm": round(height_mm, 2),
        "area_mm2": round(area_mm2, 2),
        "area_m2": round(area_mm2 / 1_000_000, 6),
        "total_cut_length_mm": round(total_length_mm, 2),
        "entity_count": len(cut_entities),
        "skipped_entity_count": len(all_entities) - len(cut_entities),
        "einstiche": count_piercings(cut_entities),
    }


def render_svg_preview(filepath: str, included_layers=None, max_size: int = 400) -> str:
    """Rendert eine einfache SVG-Vorschau der Schnittkontur (Linien, Bögen,
    Kreise, Polylinien, Splines, Ellipsen). Nutzt dieselbe Flattening-Technik
    wie _entity_length, damit keine schwergewichtigen Zusatzabhängigkeiten
    (PIL/matplotlib für ezdxf.addons.drawing) benötigt werden.

    Jede Polylinie trägt ein data-layer-Attribut, damit das Frontend
    einzelne Layer ein-/ausblenden kann (Kontur-Auswahl)."""
    doc = ezdxf.readfile(filepath)
    msp = doc.modelspace()
    entities = _cut_entities(msp, included_layers)

    if not entities:
        return ""

    extents = ezbbox.extents(entities, fast=True)
    if not extents.has_data:
        return ""

    min_x, min_y = extents.extmin.x, extents.extmin.y
    max_x, max_y = extents.extmax.x, extents.extmax.y
    width = (max_x - min_x) or 1.0
    height = (max_y - min_y) or 1.0

    padding = 10
    scale = min((max_size - 2 * padding) / width, (max_size - 2 * padding) / height)
    svg_width = width * scale + 2 * padding
    svg_height = height * scale + 2 * padding

    def to_svg(x, y):
        sx = (x - min_x) * scale + padding
        sy = svg_height - ((y - min_y) * scale + padding)  # DXF Y-oben -> SVG Y-unten
        return sx, sy

    polylines = []
    for e in entities:
        try:
            p = ezpath.make_path(e)
        except Exception:
            continue
        points = list(p.flattening(0.1))
        if len(points) < 2:
            continue
        coords = " ".join(f"{sx:.2f},{sy:.2f}" for sx, sy in (to_svg(pt.x, pt.y) for pt in points))
        layer = _escape(e.dxf.layer, quote=True)
        polylines.append(
            f'<polyline data-layer="{layer}" points="{coords}" fill="none" '
            f'stroke="#1a7f37" stroke-width="1.2"/>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {svg_width:.2f} {svg_height:.2f}" '
        f'width="100%" style="max-width:{max_size}px; height:auto; background:#fff; border:1px solid #ccc;">'
        + "".join(polylines) +
        "</svg>"
    )


def render_png_preview(filepath: str, included_layers=None, max_size: int = 800) -> bytes:
    """Rasterisiert dieselbe Kontur wie render_svg_preview als PNG (Pillow),
    damit sie als Bild in PDF-Export/E-Mail-Anhang eingebettet werden kann -
    ein Browser stellt SVG dar, ein PDF-Betrachter/Mail-Client nicht
    zuverlässig. Zeichnet mit 3x Supersampling und verkleinert danach, für
    glattere Linien als direktes 1:1-Zeichnen."""
    from io import BytesIO

    from PIL import Image, ImageDraw

    doc = ezdxf.readfile(filepath)
    msp = doc.modelspace()
    entities = _cut_entities(msp, included_layers)

    if not entities:
        return b""

    extents = ezbbox.extents(entities, fast=True)
    if not extents.has_data:
        return b""

    min_x, min_y = extents.extmin.x, extents.extmin.y
    max_x, max_y = extents.extmax.x, extents.extmax.y
    width = (max_x - min_x) or 1.0
    height = (max_y - min_y) or 1.0

    supersample = 3
    padding = 10 * supersample
    target = max_size * supersample
    scale = min((target - 2 * padding) / width, (target - 2 * padding) / height)
    img_width = max(1, int(width * scale + 2 * padding))
    img_height = max(1, int(height * scale + 2 * padding))

    def to_px(x, y):
        px = (x - min_x) * scale + padding
        py = img_height - ((y - min_y) * scale + padding)  # DXF Y-oben -> Bild Y-unten
        return px, py

    img = Image.new("RGB", (img_width, img_height), "white")
    draw = ImageDraw.Draw(img)

    for e in entities:
        try:
            p = ezpath.make_path(e)
        except Exception:
            continue
        points = list(p.flattening(0.1))
        if len(points) < 2:
            continue
        pixels = [to_px(pt.x, pt.y) for pt in points]
        draw.line(pixels, fill=(26, 127, 55), width=supersample)

    final_size = (max(1, img_width // supersample), max(1, img_height // supersample))
    img = img.resize(final_size, Image.LANCZOS)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
