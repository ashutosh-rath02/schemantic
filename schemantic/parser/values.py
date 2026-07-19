"""Recognizes component value labels (e.g. "100nF", "3.3R", "104") in
visible schematic text. Shared between the parser (which needs pure
nearest-distance matching to avoid picking up a neighboring component's
value) and the part-identity agent (which just needs to know if a token is
a value, for describing generic passives without a model call).
"""

from __future__ import annotations

import re

# Must START with digits and END with a recognized unit -- a loose
# "unit substring anywhere in the string" check matched "10-DOF-IMU-Sensor-D"
# as a capacitor value (the "M" in "IMU"), a real bug caught by inspection.
_VALUE_PATTERN = re.compile(
    r"^\d+(\.\d+)?\s*(u|n|p|m)?(F|H|R|K|M|ohm|Ω)$|^\d{3,4}$", re.IGNORECASE
)


def looks_like_value(text: str) -> bool:
    return bool(_VALUE_PATTERN.match(text.strip()))
