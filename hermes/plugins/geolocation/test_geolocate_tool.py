import json
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from geolocate_tool import geolocate, parse_colorado_milepoint  # noqa: E402


def test_parse_colorado_milepoint_accepts_common_wording():
    assert parse_colorado_milepoint("crash at I-70 mile marker 259 westbound") == ("I-70", 259.0)
    assert parse_colorado_milepoint("US 36 MM 44.5") == ("US-36", 44.5)
    assert parse_colorado_milepoint("CO 141 near milepost 77") == ("CO-141", 77.0)


def test_geolocate_finds_local_crossroads_without_network(tmp_path: Path):
    data = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-104.999, 39.739]},
            "properties": {"search_label": "I-25 & Colfax Avenue", "alt_label_1": "US-40 & I-25"},
        }],
    }
    source = tmp_path / "crossroads.geojson"
    source.write_text(json.dumps(data), encoding="utf-8")

    result = json.loads(geolocate("I-25 at Colfax", local_sources=[source], allow_network=False))

    assert result["success"] is True
    assert result["candidates"][0]["latitude"] == 39.739
    assert result["candidates"][0]["longitude"] == -104.999
    assert result["candidates"][0]["source"] == "local_crossroads"
    assert result["confidence"] in {"high", "medium"}


def test_geolocate_matches_common_road_name_to_signed_route(tmp_path: Path):
    data = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-105.005654, 40.580837]},
                "properties": {"search_label": "CO-14 & I-25 SW Frontage Road"},
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-105.001024, 40.581109]},
                "properties": {
                    "search_label": "CO-14 & I-25",
                    "search_cross_hwy": "I-25 & CO-14",
                },
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-104.980992, 40.349174]},
                "properties": {
                    "search_label": "I-25 & CO-60",
                    "alt_label_1": "CO-60 & Larimer County Road 14",
                },
            },
        ],
    }
    source = tmp_path / "crossroads.geojson"
    source.write_text(json.dumps(data), encoding="utf-8")

    result = json.loads(geolocate("I-25 at Mulberry", local_sources=[source], allow_network=False))

    assert result["success"] is True
    assert result["best_match"]["gps"] == "40.581109, -105.001024"
    assert result["needs_clarification"] is False
    assert "Mulberry" in result["best_match"]["why"]
    assert all("CO-60" not in candidate["label"] for candidate in result["candidates"])


def test_geolocate_rejects_partial_numeric_crossroad_matches(tmp_path: Path):
    data = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-105.5, 39.7]},
        "properties": {"search_label": "I-70 & 25th Avenue"},
    }]}
    source = tmp_path / "crossroads.geojson"
    source.write_text(json.dumps(data), encoding="utf-8")
    result = json.loads(geolocate("I-25 at Colfax Avenue", local_sources=[source], allow_network=False))
    assert result["success"] is False


def test_highway_wording_prefers_numbered_highway_over_same_number_county_road(tmp_path: Path):
    data = {"type": "FeatureCollection", "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-104.881859, 39.885246]},
            "properties": {"search_label": "CO-44 & US-85"},
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-104.748709, 40.307463]},
            "properties": {"search_label": "US-85 & Weld County Road 44"},
        },
    ]}
    source = tmp_path / "crossroads.geojson"
    source.write_text(json.dumps(data), encoding="utf-8")

    result = json.loads(geolocate(
        "highway 85 at highway 44",
        local_sources=[source],
        allow_network=False,
    ))

    assert result["best_match"]["name"] == "CO-44 & US-85"
    assert result["needs_clarification"] is False
    assert result["candidates"][0]["score"] > result["candidates"][1]["score"]


def test_geolocate_uses_offline_labeled_milepoints(tmp_path: Path):
    data = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-105.123, 39.456]},
        "properties": {"ROUTE_PUBLIC": "I-70", "REFPT": 259.0, "MM_LABEL": "I-70 MM 259"},
    }]}
    source = tmp_path / "milepoints.geojson"
    source.write_text(json.dumps(data), encoding="utf-8")
    result = json.loads(geolocate("I-70 mile marker 259", local_sources=[source], allow_network=False))
    assert result["success"] is True
    assert result["candidates"][0]["label"] == "I-70 MM 259"
    assert result["candidates"][0]["source"] == "local_CDOT_milepoints"


def test_estimate_milepoint_does_not_cross_segment_boundaries(tmp_path: Path):
    """_estimate_milepoint should not interpolate across route-segment boundaries.

    CO-60 has two segments: 060A (MM 1-4, lat ~40.349) and 060B (MM 6-20,
    lat ~40.335). These are on different alignments ~1.5 km apart.
    Interpolating between 060A MM 4 and 060B MM 6 produces coordinates that
    aren't on either road. The function should detect the boundary and use
    intra-segment brackets instead.
    """
    from geolocate_tool import _estimate_milepoint

    data = {"type": "FeatureCollection", "features": [
        # 060A segment (west of I-25, lat ~40.349)
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-105.055, 40.349]},
         "properties": {"ROUTE": "060A", "ROUTE_PUBLIC": "CO-60", "REFPT": 1.0, "MM_LABEL": "CO-60 MM 1"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-104.999, 40.349]},
         "properties": {"ROUTE": "060A", "ROUTE_PUBLIC": "CO-60", "REFPT": 4.0, "MM_LABEL": "CO-60 MM 4"}},
        # 060B segment (east of I-25, lat ~40.335)
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-104.978, 40.336]},
         "properties": {"ROUTE": "060B", "ROUTE_PUBLIC": "CO-60", "REFPT": 6.0, "MM_LABEL": "CO-60 MM 6"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-104.959, 40.336]},
         "properties": {"ROUTE": "060B", "ROUTE_PUBLIC": "CO-60", "REFPT": 7.0, "MM_LABEL": "CO-60 MM 7"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-104.940, 40.336]},
         "properties": {"ROUTE": "060B", "ROUTE_PUBLIC": "CO-60", "REFPT": 8.0, "MM_LABEL": "CO-60 MM 8"}},
    ]}
    mp_path = tmp_path / "milepoints.geojson"
    mp_path.write_text(json.dumps(data), encoding="utf-8")

    # Test at I-25 crossing (between 060A and 060B segments)
    result = _estimate_milepoint(40.3355, -104.980, "CO-60", mp_path)
    assert result is not None
    # Should use 060B segment, NOT interpolate across 060A/060B boundary
    assert result.get("segment_code", "060B") == "060B"
    # Milepoint should be near 6.x (within 060B range), not 5.x (cross-segment)
    assert 6.0 <= result["milepoint"] <= 7.0


def test_estimate_milepoint_segment_gap_flag(tmp_path: Path):
    """When a point falls outside all segment ranges, segment_gap should be set."""
    from geolocate_tool import _estimate_milepoint

    data = {"type": "FeatureCollection", "features": [
        # Two segments with a gap
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-105.0, 40.349]},
         "properties": {"ROUTE": "060A", "ROUTE_PUBLIC": "CO-60", "REFPT": 4.0, "MM_LABEL": "CO-60 MM 4"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-104.94, 40.336]},
         "properties": {"ROUTE": "060B", "ROUTE_PUBLIC": "CO-60", "REFPT": 7.0, "MM_LABEL": "CO-60 MM 7"}},
    ]}
    mp_path = tmp_path / "milepoints.geojson"
    mp_path.write_text(json.dumps(data), encoding="utf-8")

    # Point between the two segments
    result = _estimate_milepoint(40.340, -104.97, "CO-60", mp_path)
    assert result is not None
    assert result.get("segment_gap") is True or result.get("segment_code") is not None


def test_nominatim_defaults_unqualified_addresses_to_colorado(monkeypatch):
    import geolocate_tool as geolocation_tool
    calls = []

    def fake_get(url, params):
        calls.append(params["q"])
        return []

    monkeypatch.setattr(geolocation_tool, "_get_json", fake_get)
    geolocation_tool._nominatim_candidates("712 Horizon Ave", None, 5)
    assert calls == ["712 Horizon Ave, Colorado, USA"]


def test_exact_nominatim_house_match_is_not_low_confidence(monkeypatch):
    import geolocate_tool as geolocation_tool

    def fake_get(url, params):
        return [{
            "lat": "40.5782607",
            "lon": "-105.0182091",
            "display_name": "708, Dawn Avenue, Fort Collins, Colorado, 80524, United States",
            "importance": 0.0,
            "type": "house",
            "address": {
                "house_number": "708",
                "road": "Dawn Avenue",
                "city": "Fort Collins",
                "state": "Colorado",
            },
        }]

    monkeypatch.setattr(geolocation_tool, "_get_json", fake_get)
    result = json.loads(geolocate("708 Dawn Ave", local_sources=[]))

    assert result["confidence"] in {"medium", "high"}
    assert result["needs_clarification"] is False


def test_contextual_nominatim_result_outranks_context_free_result(monkeypatch):
    import geolocate_tool as geolocation_tool

    def fake_get(url, params):
        if "Paris" in params["q"]:
            return [{"lat": "48.8584", "lon": "2.2945", "display_name": "Eiffel Tower, Paris", "importance": 0.8}]
        return [{"lat": "37.84", "lon": "-77.44", "display_name": "Eiffel Tower, Virginia", "importance": 0.9}]

    monkeypatch.setattr(geolocation_tool, "_get_json", fake_get)
    result = json.loads(geolocate("Eiffel Tower", context="Paris, France", local_sources=[]))
    assert result["candidates"][0]["latitude"] == 48.8584


def test_geolocate_returns_explanation_when_no_candidate(tmp_path: Path):
    result = json.loads(geolocate("the old barn past nowhere", local_sources=[], allow_network=False))
    assert result["success"] is False
    assert result["candidates"] == []
    assert "No sufficiently strong candidate" in result["explanation"]


def test_geolocate_returns_answer_ready_summary_for_small_agents(tmp_path: Path):
    data = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-104.999, 39.739]},
        "properties": {"search_label": "I-25 & Colfax Avenue"},
    }]}
    source = tmp_path / "crossroads.geojson"
    source.write_text(json.dumps(data), encoding="utf-8")

    result = json.loads(geolocate("I-25 at Colfax", local_sources=[source], allow_network=False))

    assert result["best_match"]["name"] == "I-25 & Colfax Avenue"
    assert result["best_match"]["gps"] == "39.739000, -104.999000"
    assert result["best_match"]["map_url"].endswith("#map=16/39.739000/-104.999000")
    assert result["needs_clarification"] is False
    assert result["agent_instruction"].startswith("Return best_match")


def test_geolocate_no_match_tells_small_agent_what_to_ask():
    result = json.loads(geolocate("the old barn past nowhere", local_sources=[], allow_network=False))

    assert result["best_match"] is None
    assert result["needs_clarification"] is True
    assert "city" in result["clarifying_question"].lower()
    assert result["agent_instruction"].startswith("Ask clarifying_question")


def test_duplicate_sources_for_same_point_do_not_trigger_clarification(monkeypatch, tmp_path: Path):
    import geolocate_tool as geolocation_tool

    data = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-104.999, 39.739]},
        "properties": {"search_label": "I-25 & Colfax Avenue"},
    }]}
    source = tmp_path / "crossroads.geojson"
    source.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(geolocation_tool, "_milepoint_candidate", lambda _query: [])
    monkeypatch.setattr(geolocation_tool, "_nominatim_candidates", lambda *_args: [{
        "latitude": 39.739001,
        "longitude": -104.999001,
        "label": "Colfax Avenue and I-25, Denver",
        "source": "OpenStreetMap/Nominatim",
        "score": .95,
        "evidence": "Nominatim match",
    }])

    result = json.loads(geolocate("I-25 at Colfax", local_sources=[source]))

    assert result["needs_clarification"] is False


def test_resolves_cardinal_ramp_intersection_from_osm_geometry(monkeypatch):
    import geolocate_tool as geolocation_tool

    monkeypatch.setattr(geolocation_tool, "_get_json", lambda _url, _params: [{
        "lat": "40.7039473", "lon": "-105.0045457",
        "display_name": "Cleveland Avenue, Wellington, Colorado",
    }])
    osm = """<osm>
      <node id="1" lat="40.7039423" lon="-104.9985677"/>
      <node id="2" lat="40.7039394" lon="-105.0003407"/>
      <node id="3" lat="40.7041287" lon="-104.9985139"/>
      <way id="10"><nd ref="2"/><nd ref="1"/><tag k="name" v="Cleveland Avenue"/><tag k="ref" v="CO 1"/><tag k="highway" v="secondary"/></way>
      <way id="20"><nd ref="3"/><nd ref="1"/><tag k="highway" v="motorway_link"/><tag k="oneway" v="yes"/></way>
    </osm>"""
    monkeypatch.setattr(geolocation_tool, "_get_text", lambda _url, _params=None: osm)

    candidates = geolocation_tool._ramp_intersection_candidates(
        "Cleveland Ave at I-25, west ramp intersection"
    )

    assert candidates[0]["latitude"] == 40.7039423
    assert candidates[0]["longitude"] == -104.9985677
    assert candidates[0]["score"] >= .9
    assert "west" in candidates[0]["evidence"].lower()


def test_ramp_geometry_skips_unusable_road_candidate(monkeypatch):
    import geolocate_tool as geolocation_tool

    monkeypatch.setattr(geolocation_tool, "_get_json", lambda _url, _params: [
        {"lat": "40.0", "lon": "-105.3", "display_name": "Wrong Cleveland Avenue"},
        {"lat": "40.7039", "lon": "-105.0045", "display_name": "Cleveland Avenue, Wellington"},
    ])
    osm = """<osm><node id="1" lat="40.7039423" lon="-104.9985677"/><node id="2" lat="40.704" lon="-104.9985"/>
    <way id="10"><nd ref="1"/><tag k="name" v="Cleveland Avenue"/><tag k="highway" v="secondary"/></way>
    <way id="20"><nd ref="1"/><nd ref="2"/><tag k="highway" v="motorway_link"/></way></osm>"""
    calls = 0

    def fake_text(_url, _params=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("map area too dense")
        return osm

    monkeypatch.setattr(geolocation_tool, "_get_text", fake_text)
    candidates = geolocation_tool._ramp_intersection_candidates("Cleveland Ave at I-25, west ramp intersection")
    assert candidates[0]["longitude"] == -104.9985677


def test_geolocate_uses_ramp_geometry_fallback(monkeypatch):
    import geolocate_tool as geolocation_tool

    monkeypatch.setattr(geolocation_tool, "_milepoint_candidate", lambda _query: [])
    monkeypatch.setattr(geolocation_tool, "_nominatim_candidates", lambda *_args: [])
    monkeypatch.setattr(geolocation_tool, "_ramp_intersection_candidates", lambda _query: [{
        "latitude": 40.7039423, "longitude": -104.9985677,
        "label": "Cleveland Avenue at I-25 west ramp intersection",
        "source": "OpenStreetMap_ramp_geometry", "score": .96,
        "evidence": "Shared road/ramp node on west side of interchange",
    }])

    result = json.loads(geolocate("Cleveland Ave at I-25, west ramp intersection", local_sources=[]))

    assert result["success"] is True
    assert result["best_match"]["gps"] == "40.703942, -104.998568"
    assert result["needs_clarification"] is False


def test_direction_suffixes_do_not_block_crossroads_match(tmp_path: Path):
    """Direction qualifiers like SB/NB/EB/WB must not poison alpha_score."""
    data = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-104.992, 40.378]},
            "properties": {"search_label": "I-25 & CO-402"},
        }],
    }
    source = tmp_path / "crossroads.geojson"
    source.write_text(json.dumps(data), encoding="utf-8")

    for direction in ("SB", "NB", "EB", "WB"):
        result = json.loads(geolocate(f"I-25 {direction} at CO 402", local_sources=[source], allow_network=False))
        assert result["success"] is True, f"Failed for direction suffix {direction}"
        assert result["needs_clarification"] is False, f"Unnecessarily asked for clarification with {direction}"
        assert result["candidates"][0]["latitude"] == 40.378


def test_none_property_values_do_not_add_noise_tokens(tmp_path: Path):
    """Properties with None values must not inject 'None' as a matchable token."""
    data = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-104.992, 40.378]},
            "properties": {
                "search_label": "I-25 & CO-402",
                "primary_label": None,
                "alt_label_1": None,
                "MM_LABEL": None,
            },
        }],
    }
    source = tmp_path / "crossroads.geojson"
    source.write_text(json.dumps(data), encoding="utf-8")

    result = json.loads(geolocate("I-25 SB at CO 402", local_sources=[source], allow_network=False))
    assert result["success"] is True
    assert "None" not in result["candidates"][0].get("label", ""), "'None' leaked into label"



def test_geolocate_remains_directly_exposed_with_progressive_tool_search():
    import model_tools

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=["geolocation"], quiet_mode=True
    )
    names = {item["function"]["name"] for item in definitions}
    assert "geolocate" in names
