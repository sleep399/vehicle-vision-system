"""中国大陆普通与新能源车牌号码格式校验。"""

from __future__ import annotations

import re


PROVINCE_CHARS = "京沪津渝冀晋蒙辽吉黑苏浙皖闽赣鲁豫鄂湘粤桂琼川贵云藏陕甘青宁新"
ORDINARY_PLATE_RE = re.compile(
    rf"^[{PROVINCE_CHARS}][A-HJ-NP-Z][A-HJ-NP-Z0-9]{{5}}$"
)
NEW_ENERGY_PLATE_RE = re.compile(
    rf"^[{PROVINCE_CHARS}][A-HJ-NP-Z]"
    rf"(?:[DF][A-HJ-NP-Z0-9][0-9]{{4}}|[0-9]{{5}}[DF])$"
)


def is_valid_plate_number(value: object) -> bool:
    """Return whether *value* has a supported 7/8-character plate format."""
    text = str(value or "").strip()
    return bool(
        ORDINARY_PLATE_RE.fullmatch(text)
        or NEW_ENERGY_PLATE_RE.fullmatch(text)
    )
