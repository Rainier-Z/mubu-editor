"""用户约定的、可扩展的幕布标题样式与写入方法。"""

import json
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from mubu.config import MubuError
from mubu.convert import strip_span
from mubu.methods.colors import normalize_color


@dataclass(frozen=True)
class HeadingStyle:
    """一个标题级别的样式配置。

    映射键是模板中的语义级别。``heading`` 是幕布节点的标题格式字段，
    与节点在树中的深度无关。模板四级没有幕布 heading4，对应普通节点，
    因此配置为 ``heading=0`` 并单独保留加粗样式。
    """

    heading: int
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: Optional[str] = None


# 样式已根据“多层分级模板”中的 DOM 核验结果登记。
# 四级是模板中的语义层级；幕布底层没有 heading4，实际为普通节点加粗。
HEADING_STYLES: Mapping[int, HeadingStyle] = MappingProxyType(
    {
        1: HeadingStyle(heading=1, bold=True, color="red"),
        2: HeadingStyle(heading=2, bold=True),
        3: HeadingStyle(heading=3, bold=True),
        4: HeadingStyle(heading=0, bold=True),
    }
)

# 兼容此前直接导入这些常量的调用方。
_DEFAULT_HEADING_STYLE = HEADING_STYLES[1]
HEADING_LEVEL = _DEFAULT_HEADING_STYLE.heading
HEADING_BOLD = _DEFAULT_HEADING_STYLE.bold
HEADING_COLOR = _DEFAULT_HEADING_STYLE.color


def _get_heading_style(level: int) -> HeadingStyle:
    if isinstance(level, bool) or not isinstance(level, int):
        raise MubuError(f"标题级别必须是整数，收到 {level!r}")
    try:
        return HEADING_STYLES[level]
    except KeyError:
        available = ", ".join(str(value) for value in HEADING_STYLES)
        raise MubuError(
            f"未配置标题级别 {level}；当前可用级别：{available}"
        ) from None


def style_heading(client, text, *, level=1):
    """格式化指定标题级别的文本；重复调用不会叠加 span。"""
    style = _get_heading_style(level)
    color = normalize_color(style.color)
    return client.style_text(
        strip_span(text or ""),
        bold=style.bold,
        italic=style.italic,
        underline=style.underline,
        color=color,
    )


def append_headings(client, doc_id, texts, *, level=1):
    """在文档顶层追加指定标题样式的节点，返回新节点 ID。

    ``level`` 选择节点 ``heading`` 样式配置；追加位置仍是文档顶层，
    不会据此推导节点的树深度。
    """
    style = _get_heading_style(level)
    if isinstance(texts, str):
        texts = [texts]

    created_ids = []
    for text in texts:
        raw = client._get_doc_raw(doc_id)
        nodes = json.loads(raw["definition"]).get("nodes", [])
        position = len(nodes)
        node = {
            "id": client._gen_node_id(),
            "taskStatus": 0,
            "text": style_heading(client, text, level=level),
            "heading": style.heading,
            "modified": int(time.time() * 1000),
            "children": [],
        }
        event = client.build_node_create_event(
            node, ["nodes", position], position
        )
        client.save_doc(doc_id, events=[event], version=raw.get("baseVersion"))
        created_ids.append(node["id"])
    return created_ids


def set_headings(client, doc_id, texts, *, level=1):
    """按顺序设置顶层标题；复用现有节点并补齐缺少节点，保持幂等。"""
    style = _get_heading_style(level)
    if isinstance(texts, str):
        texts = [texts]

    raw = client._get_doc_raw(doc_id)
    nodes = json.loads(raw["definition"]).get("nodes", [])
    if len(nodes) > len(texts):
        raise MubuError(
            f"顶层节点数 {len(nodes)} 多于目标 {len(texts)}；删除 changeset 尚未实现，"
            "请先手动删除多余节点或把它们纳入 texts"
        )

    updated = 0
    for index, text in enumerate(texts[:len(nodes)]):
        fields = {
            "text": style_heading(client, text, level=level),
            "heading": style.heading,
        }
        if client.set_node_fields(doc_id, ["nodes", index], fields):
            updated += 1

    created = append_headings(client, doc_id, texts[len(nodes):], level=level)
    return {"updated": updated, "created": created}
