"""幕布文本颜色的可扩展注册表与通用解析方法。

新增颜色时，只需在 ``COLORS`` 中登记幕布已验证的颜色名；本模块不依赖
客户端或富文本转换层，供标题等上层方法安全复用。

``black`` 是幕布的默认文本色，因此登记为 ``None``，不生成 ``text-color`` class。
"""

from types import MappingProxyType
from typing import Optional

# 值为幕布 class 后缀；None 表示默认文本色，无需添加 text-color class。
# 只登记已经从模板 DOM 核验过的名称。
COLORS = MappingProxyType(
    {
        "red": "red",
        "yellow": "yellow",
        "green": "green",
        "blue": "blue",
        "purple": "purple",
        "black": None,
    }
)


def normalize_color(color: Optional[str]) -> Optional[str]:
    """将颜色名去空格并转为小写；空值返回 ``None``，未知名称报错。"""
    if color is None:
        return None
    if not isinstance(color, str):
        raise TypeError("color 必须是字符串或 None")

    name = color.strip().lower()
    if not name:
        return None
    if name not in COLORS:
        supported = ", ".join(COLORS)
        raise ValueError(f"不支持的幕布文本颜色 {color!r}；已登记颜色：{supported}")
    return name


def color_class(color: Optional[str]) -> Optional[str]:
    """返回幕布文本颜色 class（例如 ``text-color-red``）；默认黑色返回 ``None``。"""
    name = normalize_color(color)
    if name is None:
        return None
    suffix = COLORS[name]
    return f"text-color-{suffix}" if suffix is not None else None


__all__ = ["COLORS", "normalize_color", "color_class"]
