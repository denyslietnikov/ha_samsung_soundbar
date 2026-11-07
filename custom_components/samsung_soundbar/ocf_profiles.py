"""Model-scoped writes backed by authenticated Q800F read/write/readback probes."""

from dataclasses import dataclass

WOOFER_HREF = "/sec/networkaudio/woofer"
WOOFER_FIELD = "x.com.samsung.networkaudio.woofer"


def validate_woofer_level(value):
    """Keep the actual dB scale; never truncate fractions or use a slider index."""
    if type(value) not in (int, float) or not -12 <= value <= 6 or int(value) != value:
        raise ValueError("Woofer level must be an integer between -12 and 6")
    return int(value)


@dataclass(frozen=True)
class WooferProfile:
    model: str
    rpc_identifier: str
    minimum: int = -12
    maximum: int = 6
    step: int = 1


# 2026-11-07, HW-Q800F / 1103.1: -12, -7, -6 and +6 each read back;
# the earlier 0/+1 probe and the -7 probe establish a continuous integer scale.
# This is not an exhaustive step scan or a claim about other soundbar models.
Q800F_WOOFER = WooferProfile("HW-Q800F", "22_AV_HW-Q800F")
