"""Geolocation plugin — registers the ``geolocate`` tool.

Moved out of the Hermes source tree (``tools/geolocation_tool.py`` + the ``geolocation`` toolset
rows in ``toolsets.py`` / ``hermes_cli/tools_config.py``) so ``hermes update`` cannot revert it.
Registration now goes through ``ctx.register_tool`` with toolset ``geolocation``; the toolset is
discovered from plugin registrations and toggled per platform like any other.

Reference data lives OUTSIDE the repo and is unchanged by updates:
  ~/.hermes/data/geolocation/colorado_milepoints_labeled.geojson
  ~/.hermes/data/geolocation/drcog_roofprints_2024/
  ~/cdot_crossroads/final_all_intersection_points_highway_validated_prod.geojson
"""

from __future__ import annotations

from .geolocate_tool import geolocate

_TOOLSET = "geolocation"

_SCHEMA = {
    "name": "geolocate",
    "description": (
        "Find GPS coordinates from ordinary location wording. Call once with the user's wording "
        "unchanged. Colorado is assumed unless another region is stated. The result includes "
        "best_match and an agent_instruction telling you whether to answer or ask for "
        "clarification. Never uses Google Places."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "The user's location wording, unchanged.",
            },
            "context": {
                "type": "string",
                "description": (
                    "Only geographic context the user supplied or clearly implied, such as city, "
                    "county, state, country, route, or corridor."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 5,
                "description": "Maximum candidates. Usually leave at 5.",
            },
        },
        "required": ["location"],
    },
}


def register(ctx) -> None:
    """Register the geolocate tool with the plugin context."""
    ctx.register_tool(
        name="geolocate",
        toolset=_TOOLSET,
        schema=_SCHEMA,
        handler=lambda args, **kwargs: geolocate(
            args["location"], args.get("context"), args.get("limit", 5)
        ),
        check_fn=lambda: True,
        requires_env=[],
        description="Inference-assisted open-data geolocation",
        emoji="📍",
    )
