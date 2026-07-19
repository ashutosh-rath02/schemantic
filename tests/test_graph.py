"""Graph-layer invariants, checked against the real reference schematic.
The MST contract matters most: for every net, segment count == pin count - 1
and every pin is reachable -- that's what makes the drawn wires an honest
topological rendering even though the specific paths are synthetic.
"""

from pathlib import Path

import pytest

from schemantic.graph import build_graph, component_box, is_power_net
from schemantic.parser.netlist import parse_schematic_pdf

REFERENCE_PDF = Path(__file__).parent.parent / "ROS_Driver_for_Robots.pdf"


@pytest.fixture(scope="module")
def graph():
    schematic = parse_schematic_pdf(str(REFERENCE_PDF))
    nets, pin_to_net = build_graph(schematic)
    return schematic, nets, pin_to_net


def test_every_net_is_a_spanning_tree(graph):
    _, nets, _ = graph
    for key, net in nets.items():
        assert len(net["segments"]) == max(len(net["pins"]) - 1, 0), key


def test_mst_connects_every_pin(graph):
    _, nets, _ = graph
    for key, net in nets.items():
        if len(net["pins"]) < 2:
            continue
        # union-find over segment endpoints proves reachability
        points = {(p["x"], p["y"]) for p in net["pins"]}
        parent = {pt: pt for pt in points}

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for x1, y1, x2, y2 in net["segments"]:
            parent[find((x1, y1))] = find((x2, y2))
        roots = {find(pt) for pt in points}
        assert len(roots) == 1, f"net {key} wires don't connect all pins"


def test_ground_is_power_and_i2c_is_not(graph):
    _, nets, _ = graph
    gnd = next(n for k, n in nets.items() if "GND" in n["names"])
    assert gnd["is_power"]
    i2c = next(n for k, n in nets.items() if "IIC0SDA" in n["names"])
    assert not i2c["is_power"]  # 8-pin signal bus must stay visible by default


def test_power_name_matching():
    assert is_power_net(["GND"], 2)
    assert is_power_net(["VDD3V3"], 2)
    assert is_power_net(["5V0Vout", "Vout"], 2)
    assert not is_power_net(["IIC0SDA"], 8)
    assert not is_power_net(["DATA"], 4)
    assert is_power_net(["SOME_BUS"], 20)  # fanout backstop


def test_component_box_encloses_all_pins(graph):
    schematic, _, _ = graph
    for c in schematic.components:
        box = component_box(c)
        for p in c.pins:
            assert box["x0"] <= p.x <= box["x1"]
            assert box["y0"] <= p.y <= box["y1"]
