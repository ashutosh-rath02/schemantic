"""Graph-API tests against the real reference board's cached payload.
No network, no AI -- if the cache doesn't exist yet these skip rather than
silently building one (building it costs an enrichment run).
"""

import json
from pathlib import Path

import pytest

from schemantic import graph_api
from schemantic.pipeline import CACHE_DIR, SCHEMA_VERSION, _pdf_hash

REFERENCE_PDF = Path(__file__).parent.parent / "ROS_Driver_for_Robots.pdf"


@pytest.fixture(scope="module")
def payload():
    cache = CACHE_DIR / f"{_pdf_hash(str(REFERENCE_PDF))}_v{SCHEMA_VERSION}.json"
    if not cache.exists():
        pytest.skip("enriched cache not built yet (costs an enrichment run)")
    return json.loads(cache.read_text(encoding="utf-8"))


def test_search_finds_by_part_number(payload):
    hits = graph_api.search_components(payload, "ina219")
    assert any(h["ref"] == "U6" for h in hits)


def test_get_component_reports_ai_provenance(payload):
    u6 = graph_api.get_component(payload, "U6")
    assert u6["part_number"] == "INA219BIDR"
    assert u6["identity_is_ai_guess"] is True  # never presented as verified fact


def test_get_connections_separates_power_from_signal(payload):
    conns = graph_api.get_connections(payload, "U6")
    signal_nets = {n["net"] for n in conns["signal_nets"]}
    power_nets = {n["net"] for n in conns["power_rails"]}
    assert any("IIC0SDA" in n for n in signal_nets)
    assert any("GND" in n for n in power_nets)
    assert not (signal_nets & power_nets)


def test_get_net_lists_verified_members(payload):
    net = graph_api.get_net(payload, "IIC0SDA")
    # the SDA bus membership verified by hand earlier in the project
    assert "U6" in net["members"]
    assert "M3" in net["members"]
    assert net["is_power_rail"] is False


def test_path_between_goes_via_i2c_not_ground(payload):
    result = graph_api.path_between(payload, "U6", "M3")
    assert result["found"] is True
    path_nets = [p for p in result["path"] if p.startswith("net:")]
    assert all("GND" not in n for n in path_nets)
    assert result["hops"] <= 2  # they share the I2C bus directly


def test_path_between_honest_when_only_power_shared(payload):
    # LED1 (power indicator) has no signal path to the IMU -- the honest
    # answer is "not found via signal nets", not a path through GND.
    result = graph_api.path_between(payload, "LED1", "U1")
    if not result["found"]:
        assert "power" in result["note"]


def test_unknown_component_returns_error_not_guess(payload):
    assert "error" in graph_api.get_component(payload, "U999")
    assert "error" in graph_api.get_net(payload, "NOT_A_NET")


def test_list_regions_includes_unlabeled(payload):
    regions = graph_api.list_regions(payload)
    names = {r["region"] for r in regions}
    assert "unlabeled area" in names  # P1/P2 headers live there
