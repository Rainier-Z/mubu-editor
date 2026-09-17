"""用户自定义、可复用的幕布业务方法。"""

from .colors import COLORS, color_class, normalize_color
from .headings import (
    HEADING_BOLD,
    HEADING_COLOR,
    HEADING_LEVEL,
    HEADING_STYLES,
    HeadingStyle,
    append_headings,
    set_headings,
    style_heading,
)

METHODS = {
    "append_headings": append_headings,
    "set_headings": set_headings,
    "style_heading": style_heading,
}

__all__ = [
    "COLORS",
    "HEADING_BOLD",
    "HEADING_COLOR",
    "HEADING_LEVEL",
    "HEADING_STYLES",
    "HeadingStyle",
    "METHODS",
    "append_headings",
    "color_class",
    "normalize_color",
    "set_headings",
    "style_heading",
]
