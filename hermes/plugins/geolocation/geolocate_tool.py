"""Open-data, inference-assisted text geolocation for Hermes."""

from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Optional


NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSM_MAP = "https://api.openstreetmap.org/api/0.6/map"
CDOT_MILEPOINTS = "https://dtdapps.codot.gov/server/rest/services/Webapps/open_data_sde/FeatureServer/19/query"
DEFAULT_CROSSROADS = Path.home() / "cdot_crossroads/final_all_intersection_points_highway_validated_prod.geojson"
DEFAULT_MILEPOINTS = Path.home() / ".hermes/data/geolocation/colorado_milepoints_labeled.geojson"
UA = "Hermes-Geolocation/1.0 (personal assistant; OpenStreetMap Nominatim)"

# Common local road names that may be absent from signed-route crossroads data.
# Aliases normalize to route labels before the usual matching safeguards run.
COLORADO_ROUTE_ALIASES = {
    "mulberry": "CO-14",
    "mulberry street": "CO-14",
}


def _tokens(text: str) -> set[str]:
    text = text.lower().replace("&", " at ")
    replacements = {"interstate": "i", "highway": "", "avenue": "ave", "street": "st", "road": "rd", "boulevard": "blvd"}
    for old, new in replacements.items():
        text = re.sub(rf"\b{old}\b", new, text)
    return {x for x in re.findall(r"[a-z]+|\d+(?:\.\d+)?", text) if x not in {"at", "and", "near", "the", "on", "sb", "nb", "eb", "wb"}}


def _normalize_route_alias(query: str) -> tuple[str, Optional[str]]:
    """Replace one known common road name with its signed Colorado route."""
    for alias in sorted(COLORADO_ROUTE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", query, re.I):
            route = COLORADO_ROUTE_ALIASES[alias]
            normalized = re.sub(rf"\b{re.escape(alias)}\b", route, query, flags=re.I)
            return normalized, alias.title()
    return query, None


def _route_refs(text: str) -> set[str]:
    """Return normalized signed-route references without confusing county roads."""
    refs = set()
    for family, number in re.findall(r"\b(I|INTERSTATE|US|U\.?S\.?|CO|SH)\s*-?\s*(\d{1,3})\b", text, re.I):
        normalized_family = family.upper().replace(".", "")
        normalized_family = "I" if normalized_family in {"I", "INTERSTATE"} else "US" if normalized_family == "US" else "CO"
        refs.add(f"{normalized_family}-{int(number)}")
    return refs


def parse_colorado_milepoint(text: str):
    route = re.search(r"\b(I|INTERSTATE|US|U\.?S\.?|CO|SH)\s*-?\s*(\d{1,3})\b", text, re.I)
    marker = re.search(r"\b(?:MM|MP|MI|MILE\s*(?:MARKER|POST|POINT)?)\s*#?\s*(\d+(?:\.\d+)?)\b", text, re.I)
    if not route or not marker:
        return None
    family = route.group(1).upper().replace(".", "")
    family = "I" if family in {"I", "INTERSTATE"} else "US" if family == "US" else "CO"
    return f"{family}-{int(route.group(2))}", float(marker.group(1))


def _route_code(label: str) -> str:
    family, number = label.split("-")
    return f"{int(number):03d}%"  # CDOT suffix variants, e.g. 070A


def _get_json(url: str, params: dict):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_text(url: str, params: Optional[dict] = None) -> str:
    full_url = url + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(full_url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8")


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in miles."""
    R = 3956.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _extract_route_public(label: str) -> Optional[str]:
    """Extract a signed route label (e.g. 'CO-83') from a candidate label."""
    # Direct format: CO-83, US-36, I-25
    m = re.search(r"\b(I|US|CO)-(\d{1,3})\b", label, re.I)
    if m:
        family = m.group(1).upper()
        return f"{family}-{int(m.group(2))}"
    # Nominatim format: "State Highway 83" / "North State Highway 83"
    m = re.search(r"\bState\s+Highway\s+(\d{1,3})\b", label, re.I)
    if m:
        return f"CO-{int(m.group(1))}"
    # "Interstate N"
    m = re.search(r"\bInterstate\s+(\d{1,3})\b", label, re.I)
    if m:
        return f"I-{int(m.group(1))}"
    # "US Highway N"
    m = re.search(r"\bUS\s+Highway\s+(\d{1,3})\b", label, re.I)
    if m:
        return f"US-{int(m.group(1))}"
    return None


def _estimate_milepoint(lat: float, lon: float, route_public: str, milepoints_path: Path = DEFAULT_MILEPOINTS) -> Optional[dict]:
    """Estimate the milepoint for a GPS coordinate along a Colorado route.

    Loads the labeled CDOT milepoints GeoJSON, filters to the matching route,
    finds the two nearest bracketing milepoints by REFPT, and linearly
    interpolates to 0.1-mile precision.

    Returns a dict with 'milepoint' (float), 'label' (str), 'bracket_low' and
    'bracket_high' (the flanking MM labels), or None if the route has no data.

    IMPORTANT: CDOT routes can have multiple segments (e.g., 060A and 060B for
    CO-60). Each segment has its own milepoint numbering and alignment. When the
    two bracketing milepoints are on different route segments, interpolation
    across the boundary produces incorrect results (the segments may be on
    different alignments separated by km). In this case, we:
    1. Prefer intra-segment brackets (both on the same segment code).
    2. If the crossing falls in a segment gap, return the nearest segment
       endpoint with a 'segment_gap' flag instead of a misleading interpolation.
    """
    if not milepoints_path.exists():
        return None
    try:
        data = json.loads(milepoints_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    route_mps = [
        f for f in data.get("features", [])
        if (f.get("properties") or {}).get("ROUTE_PUBLIC") == route_public
    ]
    if len(route_mps) < 2:
        return None

    # Sort by REFPT
    route_mps.sort(key=lambda f: float(f["properties"].get("REFPT", 0)))

    # Compute distance from target to each milepoint
    scored = []
    for f in route_mps:
        coords = (f.get("geometry") or {}).get("coordinates", [])
        if len(coords) < 2:
            continue
        mp_lon, mp_lat = coords[0], coords[1]
        d = _haversine_miles(lat, lon, mp_lat, mp_lon)
        refpt = float(f["properties"].get("REFPT", 0))
        route_code = (f.get("properties") or {}).get("ROUTE", "")
        scored.append((d, refpt, mp_lat, mp_lon, f, route_code))
    if len(scored) < 2:
        return None

    scored.sort(key=lambda x: x[0])

    # The closest milepoint
    _, closest_refpt, _, _, closest_feat, closest_route_code = scored[0]

    # Find the two bracketing milepoints by REFPT (the ones immediately
    # before and after the closest one in the sorted route sequence).
    route_mps_by_refpt = [
        (float(f["properties"].get("REFPT", 0)), f, f["properties"].get("ROUTE", ""))
        for f in route_mps
    ]
    bracket_low = None
    bracket_high = None
    for i, (refpt, f, rc) in enumerate(route_mps_by_refpt):
        if refpt == closest_refpt:
            if i > 0:
                bracket_low = route_mps_by_refpt[i - 1]
            if i < len(route_mps_by_refpt) - 1:
                bracket_high = route_mps_by_refpt[i + 1]
            # Also use the closest itself as one bracket
            if bracket_low is None:
                bracket_low = (refpt, f, rc)
            if bracket_high is None:
                bracket_high = (refpt, f, rc)
            break

    if bracket_low is None or bracket_high is None:
        return None

    low_refpt, low_feat, low_route_code = bracket_low
    high_refpt, high_feat, high_route_code = bracket_high

    if low_refpt == high_refpt:
        # Exactly on a milepoint
        return {
            "milepoint": low_refpt,
            "label": f"{route_public} MM {low_refpt:.1f}",
            "bracket_low": low_feat["properties"].get("MM_LABEL", f"{route_public} MM {low_refpt:.1f}"),
            "bracket_high": high_feat["properties"].get("MM_LABEL", f"{route_public} MM {high_refpt:.1f}"),
        }

    # Detect route-segment boundary: when the two bracketing milepoints are on
    # different CDOT route segments (e.g., 060A and 060B), the segments may
    # have different alignments. Interpolating across the boundary is wrong.
    segment_boundary = low_route_code != high_route_code and low_route_code and high_route_code

    if segment_boundary:
        # Find which segment the target point is closest to, and use only
        # milepoints from that segment.
        low_coords = (low_feat.get("geometry") or {}).get("coordinates", [])
        high_coords = (high_feat.get("geometry") or {}).get("coordinates", [])
        if len(low_coords) >= 2 and len(high_coords) >= 2:
            d_to_low = _haversine_miles(lat, lon, low_coords[1], low_coords[0])
            d_to_high = _haversine_miles(lat, lon, high_coords[1], high_coords[0])
        else:
            d_to_low, d_to_high = float("inf"), float("inf")

        # Pick the closer segment endpoint and try to find intra-segment brackets
        nearest_refpt = low_refpt if d_to_low <= d_to_high else high_refpt
        nearest_route_code = low_route_code if d_to_low <= d_to_high else high_route_code
        nearest_feat = low_feat if d_to_low <= d_to_high else high_feat

        # Look for an intra-segment bracket pair around the target
        same_seg = [
            (refpt, f, rc) for refpt, f, rc in route_mps_by_refpt
            if rc == nearest_route_code
        ]
        if len(same_seg) >= 2:
            # Find the two points in this segment that bracket the target's
            # position along the route (by longitude for E-W routes, latitude for N-S)
            seg_scored = []
            for refpt, f, rc in same_seg:
                coords = (f.get("geometry") or {}).get("coordinates", [])
                if len(coords) >= 2:
                    d = _haversine_miles(lat, lon, coords[1], coords[0])
                    seg_scored.append((d, refpt, f))
            seg_scored.sort()
            if len(seg_scored) >= 2:
                _, s_refpt, s_feat = seg_scored[0]
                # Find the adjacent point in this segment
                same_seg_sorted = sorted(same_seg, key=lambda x: x[0])
                for i, (refpt, f, rc) in enumerate(same_seg_sorted):
                    if refpt == s_refpt:
                        intra_low = same_seg_sorted[i - 1] if i > 0 else (refpt, f, rc)
                        intra_high = same_seg_sorted[i + 1] if i < len(same_seg_sorted) - 1 else (refpt, f, rc)
                        low_refpt2, low_feat2, _ = intra_low
                        high_refpt2, high_feat2, _ = intra_high
                        if low_refpt2 != high_refpt2:
                            low_c = (low_feat2.get("geometry") or {}).get("coordinates", [])
                            high_c = (high_feat2.get("geometry") or {}).get("coordinates", [])
                            if len(low_c) >= 2 and len(high_c) >= 2:
                                seg_len = _haversine_miles(low_c[1], low_c[0], high_c[1], high_c[0])
                                d_to_low2 = _haversine_miles(lat, lon, low_c[1], low_c[0])
                                frac = max(0.0, min(1.0, d_to_low2 / seg_len)) if seg_len >= 0.001 else 0.0
                                est = low_refpt2 + frac * (high_refpt2 - low_refpt2)
                                return {
                                    "milepoint": round(est, 1),
                                    "label": f"{route_public} MM {est:.1f}",
                                    "bracket_low": low_feat2["properties"].get("MM_LABEL", f"{route_public} MM {low_refpt2:.1f}"),
                                    "bracket_high": high_feat2["properties"].get("MM_LABEL", f"{route_public} MM {high_refpt2:.1f}"),
                                    "segment_code": nearest_route_code,
                                }
                        # Fallback: exactly on an intra-segment point
                        return {
                            "milepoint": s_refpt,
                            "label": f"{route_public} MM {s_refpt:.1f}",
                            "bracket_low": s_feat["properties"].get("MM_LABEL", f"{route_public} MM {s_refpt:.1f}"),
                            "bracket_high": s_feat["properties"].get("MM_LABEL", f"{route_public} MM {s_refpt:.1f}"),
                            "segment_code": nearest_route_code,
                        }
                        break

        # No intra-segment interpolation possible — return nearest endpoint
        # with a gap flag so callers know this is approximate.
        nearest_mp = nearest_refpt
        return {
            "milepoint": nearest_mp,
            "label": f"{route_public} MM {nearest_mp:.1f} (nearest, segment gap nearby)",
            "bracket_low": nearest_feat["properties"].get("MM_LABEL", f"{route_public} MM {nearest_mp:.1f}"),
            "bracket_high": nearest_feat["properties"].get("MM_LABEL", f"{route_public} MM {nearest_mp:.1f}"),
            "segment_gap": True,
            "segment_code": nearest_route_code,
            "segment_boundary": f"{low_route_code}/{high_route_code}",
        }

    # Normal case: both brackets on the same segment, interpolate.
    low_coords = (low_feat.get("geometry") or {}).get("coordinates", [])
    high_coords = (high_feat.get("geometry") or {}).get("coordinates", [])
    if len(low_coords) < 2 or len(high_coords) < 2:
        return None

    low_lat, low_lon = low_coords[1], low_coords[0]
    high_lat, high_lon = high_coords[1], high_coords[0]

    seg_len = _haversine_miles(low_lat, low_lon, high_lat, high_lon)
    d_to_low = _haversine_miles(lat, lon, low_lat, low_lon)

    if seg_len < 0.001:
        frac = 0.0
    else:
        frac = max(0.0, min(1.0, d_to_low / seg_len))

    est_refpt = low_refpt + frac * (high_refpt - low_refpt)

    return {
        "milepoint": round(est_refpt, 1),
        "label": f"{route_public} MM {est_refpt:.1f}",
        "bracket_low": low_feat["properties"].get("MM_LABEL", f"{route_public} MM {low_refpt:.1f}"),
        "bracket_high": high_feat["properties"].get("MM_LABEL", f"{route_public} MM {high_refpt:.1f}"),
    }


def _find_interstate_crossing_candidates(query: str, milepoints_path: Path = DEFAULT_MILEPOINTS) -> list[dict]:
    """Detect parallel roads that share an interstate overpass crossing.

    When two surface roads run parallel (both east-west or both north-south)
    and the user names them together, they may mean the interstate overpass area
    where both roads cross, not a surface intersection. This is common in
    Colorado's grid-based counties (Weld, Larimer, Adams).

    Returns candidates for the interstate crossing point if detected.
    """
    # Parse two road names from the query. Look for "A & B", "A and B",
    # "A at B" patterns.
    road_match = re.match(
        r'^(.+?)\s+(?:&|and|at|@)\s+(.+?)(?:\s*,?\s*(?:in|weld|larimer|adams|broomfield|jefferson|denver|arapahoe)\b|$)',
        query, re.I
    )
    if not road_match:
        return []

    road_a_raw = road_match.group(1).strip().rstrip(',').strip()
    road_b_raw = road_match.group(2).strip().rstrip(',').strip()

    # Look up both roads via Nominatim
    try:
        rows_a = _get_json(NOMINATIM, {"q": f"{road_a_raw}, Colorado", "format": "jsonv2", "limit": 5})
        rows_b = _get_json(NOMINATIM, {"q": f"{road_b_raw}, Colorado", "format": "jsonv2", "limit": 5})
    except Exception:
        return []

    if not rows_a or not rows_b:
        return []

    # Get approximate lat/lon ranges for each road
    def _road_range(rows):
        lats = [float(r["lat"]) for r in rows]
        lons = [float(r["lon"]) for r in rows]
        return min(lats), max(lats), min(lons), max(lons)

    a_lat_min, a_lat_max, a_lon_min, a_lon_max = _road_range(rows_a)
    b_lat_min, b_lat_max, b_lon_min, b_lon_max = _road_range(rows_b)

    # Check if both roads run parallel (both E-W or both N-S)
    a_lat_span = a_lat_max - a_lat_min
    a_lon_span = a_lon_max - a_lon_min
    b_lat_span = b_lat_max - b_lat_min
    b_lon_span = b_lon_max - b_lon_min

    # E-W roads: longitude span >> latitude span, and similar latitude
    ew_parallel = (
        a_lon_span > a_lat_span * 3 and b_lon_span > b_lat_span * 3
        and abs((a_lat_min + a_lat_max) / 2 - (b_lat_min + b_lat_max) / 2) < 0.02
    )
    # N-S roads: latitude span >> longitude span, and similar longitude
    ns_parallel = (
        a_lat_span > a_lon_span * 3 and b_lat_span > b_lon_span * 3
        and abs((a_lon_min + a_lon_max) / 2 - (b_lon_min + b_lon_max) / 2) < 0.02
    )

    if not ew_parallel and not ns_parallel:
        return []

    # Find perpendicular interstate. For E-W roads, look for N-S interstates.
    # For N-S roads, look for E-W interstates.
    # Colorado interstates: I-25 (N-S), I-70 (E-W), I-76 (NE-SW), I-225 (N-S),
    # I-270 (E-W), I-470 (loop)
    center_lat = (a_lat_min + a_lat_max + b_lat_min + b_lat_max) / 4
    center_lon = (a_lon_min + a_lon_max + b_lon_min + b_lon_max) / 4

    # Scan along the roads' latitude/longitude for interstate markers
    # by checking Nominatim reverse geocoding
    interstate_lon = None
    interstate_lat = None
    interstate_name = None

    if ew_parallel:
        # Roads run E-W, interstate runs N-S. Scan along longitude.
        scan_lat = center_lat
        for scan_lon in [center_lon + d * 0.005 for d in range(-10, 11)]:
            try:
                import time; time.sleep(0.3)
                rev = _get_json(
                    "https://nominatim.openstreetmap.org/reverse",
                    {"lat": scan_lat, "lon": scan_lon, "format": "jsonv2", "zoom": 16}
                )
                road_name = (rev.get("address") or {}).get("road", "")
                if any(kw in road_name.lower() for kw in ["interstate", "i-25", "i-70", "i-76", "eisenhower"]):
                    interstate_lon = scan_lon
                    interstate_lat = scan_lat
                    interstate_name = road_name
                    break
            except Exception:
                continue
    else:
        # Roads run N-S, interstate runs E-W. Scan along latitude.
        scan_lon = center_lon
        for scan_lat in [center_lat + d * 0.005 for d in range(-10, 11)]:
            try:
                import time; time.sleep(0.3)
                rev = _get_json(
                    "https://nominatim.openstreetmap.org/reverse",
                    {"lat": scan_lat, "lon": scan_lon, "format": "jsonv2", "zoom": 16}
                )
                road_name = (rev.get("address") or {}).get("road", "")
                if any(kw in road_name.lower() for kw in ["interstate", "i-25", "i-70", "i-76", "eisenhower"]):
                    interstate_lon = scan_lon
                    interstate_lat = scan_lat
                    interstate_name = road_name
                    break
            except Exception:
                continue

    if interstate_lon is None:
        return []

    return [{
        "latitude": interstate_lat,
        "longitude": interstate_lon,
        "label": f"{road_a_raw} & {road_b_raw} at {interstate_name or 'interstate'} overpass",
        "source": "interstate_crossing_detection",
        "score": 0.85,
        "evidence": (
            f"Both roads are parallel ({('E-W' if ew_parallel else 'N-S')}) and cross "
            f"{interstate_name or 'an interstate'} at this overpass point"
        ),
    }]


def _ramp_intersection_candidates(query: str) -> list[dict]:
    """Resolve cardinal ramp-terminal wording from shared OSM road/ramp nodes."""
    direction_match = re.search(r"\b(west|east|north|south)\s+(?:side\s+)?ramp\s+intersection\b", query, re.I)
    road_match = re.search(r"^(.+?)\s+(?:at|@|and|&)\s+(?:I|INTERSTATE)\s*-?\s*\d+", query, re.I)
    if not direction_match or not road_match:
        return []
    direction = direction_match.group(1).lower()
    road_wording = road_match.group(1).strip(" ,")
    road_search = re.sub(r"\bAve\b", "Avenue", road_wording, flags=re.I)
    rows = _get_json(NOMINATIM, {"q": f"{road_search}, Colorado, USA", "format": "jsonv2", "addressdetails": 1, "limit": 10})
    wanted = _tokens(road_wording)
    found = []
    for row in rows:
        lat, lon = float(row["lat"]), float(row["lon"])
        delta = .008
        try:
            xml = _get_text(OSM_MAP, {"bbox": f"{lon-delta},{lat-delta},{lon+delta},{lat+delta}"})
            root = ET.fromstring(xml)
        except Exception:
            continue
        nodes = {node.attrib["id"]: (float(node.attrib["lat"]), float(node.attrib["lon"])) for node in root.findall("node")}
        road_nodes, ramp_nodes = set(), set()
        for way in root.findall("way"):
            tags = {tag.attrib["k"]: tag.attrib["v"] for tag in way.findall("tag")}
            node_ids = {nd.attrib["ref"] for nd in way.findall("nd")}
            if wanted <= _tokens(tags.get("name", "")):
                road_nodes |= node_ids
            if tags.get("highway") == "motorway_link":
                ramp_nodes |= node_ids
        shared = [node_id for node_id in road_nodes & ramp_nodes if node_id in nodes]
        if not shared:
            continue
        axis = (lambda node_id: nodes[node_id][1]) if direction in {"west", "east"} else (lambda node_id: nodes[node_id][0])
        selected = min(shared, key=axis) if direction in {"west", "south"} else max(shared, key=axis)
        point_lat, point_lon = nodes[selected]
        found.append({"latitude": point_lat, "longitude": point_lon, "label": f"{road_search} at {direction} ramp intersection", "source": "OpenStreetMap_ramp_geometry", "score": .96, "evidence": f"Shared OSM node {selected} between the named road and a motorway ramp on the {direction} side of the interchange"})
    return found


def _milepoint_candidate(text: str) -> list[dict]:
    parsed = parse_colorado_milepoint(text)
    if not parsed:
        return []
    route, marker = parsed
    where = f"ROUTE LIKE '{_route_code(route)}' AND REFPT >= {marker - .06} AND REFPT <= {marker + .06}"
    data = _get_json(CDOT_MILEPOINTS, {"f": "json", "where": where, "outFields": "ROUTE,REFPT", "returnGeometry": "true", "outSR": 4326})
    candidates = []
    for feature in data.get("features", []):
        geom, attrs = feature.get("geometry", {}), feature.get("attributes", {})
        if "x" in geom and "y" in geom:
            candidates.append({"latitude": geom["y"], "longitude": geom["x"], "label": f"{route} MM {attrs.get('REFPT')}", "source": "CDOT_milepoints", "score": .98, "evidence": "Authoritative CDOT route/reference-point match"})
    return candidates


def _local_candidates(query: str, sources: Iterable[Path]) -> list[dict]:
    normalized_query, matched_alias = _normalize_route_alias(query)
    wanted = _tokens(normalized_query)
    milepoint = parse_colorado_milepoint(query)
    found = []
    for source in sources:
        source = Path(source)
        if not source.exists():
            continue
        data = json.loads(source.read_text(encoding="utf-8"))
        for feature in data.get("features", []):
            props = feature.get("properties") or {}
            geom = feature.get("geometry") or {}
            coords = geom.get("coordinates", [])
            if milepoint and geom.get("type") == "Point" and len(coords) >= 2:
                route, marker = milepoint
                if props.get("ROUTE_PUBLIC") == route and abs(float(props.get("REFPT", -9999)) - marker) <= .06:
                    found.append({"latitude": coords[1], "longitude": coords[0], "label": props.get("MM_LABEL", f"{route} MM {marker:g}"), "source": "local_CDOT_milepoints", "score": .99, "evidence": "Exact route and reference-point match in the offline labeled CDOT milepoint layer"})
                    continue
            label = " | ".join(str(v) for k in ("search_label", "primary_label", "alt_label_1", "MM_LABEL") if (v := props.get(k)) not in (None, "", "None"))
            have = _tokens(label)
            if not wanted or not have:
                continue
            score = len(wanted & have) / len(wanted)
            numeric_wanted = {t for t in wanted if t[0].isdigit()}
            alpha_wanted = wanted - numeric_wanted
            numeric_ok = numeric_wanted <= have
            requested_routes = _route_refs(normalized_query)
            route_refs_ok = requested_routes <= _route_refs(label)
            alpha_score = len(alpha_wanted & have) / len(alpha_wanted) if alpha_wanted else 1.0
            if score >= .5 and numeric_ok and route_refs_ok and alpha_score >= .75 and geom.get("type") == "Point" and len(coords) >= 2:
                clean_label = " | ".join(part for part in label.split(" | ") if part and part != "None")
                if "frontage" in clean_label.lower() and "frontage" not in normalized_query.lower():
                    score = max(0.0, score - 0.12)
                # When query says "highway N", the number N is the intended route.
                # Penalize candidates where N appears as a county road instead.
                highway_nums = set(re.findall(r'\bhighway\s+(\d{1,3})\b', query, re.I))
                if highway_nums:
                    county_road_nums = set(re.findall(r'\b(?:CR|County\s+Road)\s*(\d{1,3})\b', clean_label, re.I))
                    has_highway_prefix = bool(re.search(r'\b(?:CO|US|I)\s*-\s*\d', clean_label, re.I))
                    if highway_nums & county_road_nums:
                        score = max(0.0, score - 0.15)
                    elif has_highway_prefix:
                        score = min(1.0, score + 0.05)
                alias_evidence = (
                    f" after mapping common road name {matched_alias} to its signed route"
                    if matched_alias else ""
                )
                found.append({"latitude": coords[1], "longitude": coords[0], "label": clean_label, "source": "local_crossroads", "score": round(score, 3), "evidence": f"Matched {len(wanted & have)}/{len(wanted)} normalized query terms in local merged CDOT/OSM data{alias_evidence}"})
    return found


def _nominatim_candidates(query: str, context: Optional[str], limit: int) -> list[dict]:
    state_names = (
        "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming"
    )
    state_abbr = r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY"
    explicit_region = bool(re.search(rf"\b(?:{state_names})\b|,\s*(?:{state_abbr})\b", query, re.I))
    if context:
        phrases = [query if context.lower() in query.lower() else f"{query}, {context}"]
    elif explicit_region:
        phrases = [query]
    else:
        phrases = [f"{query}, Colorado, USA"]
    output = []
    seen = set()
    for phrase in phrases[:2]:
        rows = _get_json(NOMINATIM, {"q": phrase, "format": "jsonv2", "addressdetails": 1, "limit": limit})
        contextual = bool(context and phrase != query)
        for row in rows:
            key = (round(float(row["lat"]), 6), round(float(row["lon"]), 6))
            if key in seen:
                continue
            seen.add(key)
            importance = float(row.get("importance", .3))
            score = min(1.0, importance + (.2 if contextual else 0))
            evidence = f"Nominatim match for: {phrase}"
            address = row.get("address") or {}
            query_tokens = _tokens(query)
            query_numbers = {token for token in query_tokens if token[0].isdigit()}
            road_tokens = _tokens(str(address.get("road", "")))
            house_number = str(address.get("house_number", ""))
            street_words = {token for token in query_tokens if not token[0].isdigit()}
            exact_house = (
                row.get("type") in {"house", "building"}
                and bool(query_numbers)
                and house_number in query_numbers
                and bool(street_words)
                and street_words <= road_tokens
            )
            if exact_house:
                score = max(score, .9)
                evidence = f"Exact house number and street match from Nominatim for: {phrase}"
            output.append({"latitude": float(row["lat"]), "longitude": float(row["lon"]), "label": row.get("display_name", phrase), "source": "OpenStreetMap/Nominatim", "score": round(score, 3), "evidence": evidence, "osm_type": row.get("type")})
    return output


def geolocate(location: str, context: Optional[str] = None, limit: int = 5, local_sources=None, allow_network: bool = True) -> str:
    """Resolve free-form location wording to ranked GPS candidates."""
    sources = [DEFAULT_CROSSROADS, DEFAULT_MILEPOINTS] if local_sources is None else local_sources
    candidates = _local_candidates(location, sources)
    errors = []
    if allow_network:
        try:
            candidates.extend(_ramp_intersection_candidates(location))
        except Exception as exc:
            errors.append(f"OSM ramp-geometry lookup failed: {exc}")
        if not any(candidate["source"] == "local_CDOT_milepoints" for candidate in candidates):
            try:
                candidates.extend(_milepoint_candidate(location))
            except Exception as exc:
                errors.append(f"CDOT lookup failed: {exc}")
        # When initial candidates are weak (county centroid, low score),
        # try the interstate crossing detector for parallel-road queries.
        top_score = max((c["score"] for c in candidates), default=0)
        if top_score < 0.7:
            try:
                candidates.extend(_find_interstate_crossing_candidates(location))
            except Exception as exc:
                errors.append(f"Interstate crossing detection failed: {exc}")
        try:
            candidates.extend(_nominatim_candidates(location, context, limit))
        except Exception as exc:
            errors.append(f"Nominatim lookup failed: {exc}")
    candidates.sort(key=lambda x: x["score"], reverse=True)
    candidates = candidates[: max(1, min(limit, 10))]
    success = bool(candidates)
    top = candidates[0]["score"] if candidates else 0
    confidence = "high" if top >= .9 else "medium" if top >= .6 else "low"
    close_second = False
    if len(candidates) > 1 and (top - candidates[1]["score"]) < .1:
        first, second = candidates[0], candidates[1]
        lat1, lat2 = math.radians(float(first["latitude"])), math.radians(float(second["latitude"]))
        dlat = lat2 - lat1
        dlon = math.radians(float(second["longitude"]) - float(first["longitude"]))
        haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        close_second = 12_742_000 * math.asin(math.sqrt(haversine)) > 100
    needs_clarification = not success or confidence == "low" or close_second
    best_match = None
    if success:
        best = candidates[0]
        latitude = float(best["latitude"])
        longitude = float(best["longitude"])
        gps = f"{latitude:.6f}, {longitude:.6f}"
        best_match = {
            "name": best["label"],
            "gps": gps,
            "latitude": latitude,
            "longitude": longitude,
            "confidence": confidence,
            "why": best["evidence"],
            "source": best["source"],
            "map_url": f"https://www.openstreetmap.org/?mlat={latitude:.6f}&mlon={longitude:.6f}#map=16/{latitude:.6f}/{longitude:.6f}",
        }
        # Estimate milepoint if the best match is on a known Colorado route
        route_public = _extract_route_public(best["label"])
        if route_public:
            try:
                mp_info = _estimate_milepoint(latitude, longitude, route_public)
                if mp_info:
                    best_match["milepoint"] = mp_info
            except Exception:
                pass  # milepoint estimation is advisory, never blocks the result
    clarifying_question = (
        "Which city, county, state, cross street, route, mile marker, or nearby landmark should I use to narrow this down?"
        if needs_clarification else None
    )
    result = {
        "success": success,
        "query": location,
        "context": context,
        "best_match": best_match,
        "confidence": confidence,
        "needs_clarification": needs_clarification,
        "clarifying_question": clarifying_question,
        "agent_instruction": (
            "Ask clarifying_question; do not guess a coordinate."
            if needs_clarification else
            "Return best_match name, GPS, confidence, why, and map_url. Mention alternatives only when materially plausible."
        ),
        "candidates": candidates,
        "explanation": "Top candidate is ranked from local CDOT/OSM evidence and open geocoding; verify low-confidence or ambiguous wording." if success else "No sufficiently strong candidate was found. Add a city/county, route, cross street, landmark, or mile marker.",
        "errors": errors,
        "google_places_used": False,
    }
    return json.dumps(result, ensure_ascii=False)
