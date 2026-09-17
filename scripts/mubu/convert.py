"""mubu 包 — 文档结构 ↔ Markdown / OPML / FreeMind 转换与展示格式化。

核心能力：
- Markdown 导出：doc_to_markdown / export_markdown
- Markdown 导入：markdown_to_doc

扩展能力：
- OPML / FreeMind 导出：doc_to_opml / doc_to_freeplane
- 文件名安全化处理：_safe_filename
- 列表 / 搜索结果展示格式化：format_list / format_search
"""

import json
import re
import secrets
import string
import time
from html import escape as _html_escape
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

from mubu.config import MubuError

_PLAIN_TEXT_MARKER = "<!-- mubu-plain-text -->"


def _escape_checkbox_text(text: str) -> str:
    """Escape a literal checkbox-looking prefix in a node's text."""
    if re.match(r"^\\*\[([ xX])\] ", text):
        return "\\" + text
    return text


def _unescape_checkbox_text(text: str) -> str:
    """Undo one escape slash before a literal checkbox-looking prefix."""
    if re.match(r"^\\+\[([ xX])\] ", text):
        return text[1:]
    return text


def _task_checkbox_state(node: Dict[str, Any]) -> Optional[bool]:
    """Return a checkbox mark from protocol taskStatus or a legacy checked field."""
    if "taskStatus" in node:
        status = node.get("taskStatus")
        if isinstance(status, int) and not isinstance(status, bool):
            if status == 1:
                return False
            if status == 2:
                return True
        return None

    checked = node.get("checked")
    return bool(checked) if checked is not None else None


def doc_to_markdown(node: Dict[str, Any], level: int = 0) -> str:
    """将节点（及其子树）递归渲染为 Markdown 列表片段。

    子节点使用 '- ' 列表项，缩进 = 2 * level；taskStatus 1/2 渲染为 '- [ ]'/'- [x]'；
    缺少 taskStatus 时兼容旧 checked 字段；
    含 note 在其子树之后追加 '> {note}'。根标题（'# '）由 export_markdown 负责。

    Args:
        node: 节点字典，可包含 text / checked / note / children
        level: 当前节点深度（根节点的直接子节点为 0）

    Returns:
        Markdown 列表片段（不含根标题行）
    """
    lines: List[str] = []
    text = (node.get("text") or "").replace("\n", " ")
    indent = " " * (2 * level)

    # taskStatus 是真实任务语义；仅在缺少协议字段时兼容旧 checked。
    checked = _task_checkbox_state(node)
    if checked is not None:
        mark = "x" if checked else " "
        lines.append(f"{indent}- [{mark}] {text}")
    else:
        lines.append(f"{indent}- {_escape_checkbox_text(text)}")

    # 递归子节点（位于 note 之前）
    for child in node.get("children") or []:
        lines.append(doc_to_markdown(child, level + 1))

    # note 比对应列表项多缩进一级，以便与根节点 note 明确区分。
    note = node.get("note")
    if note:
        note_indent = " " * (2 * (level + 1))
        lines.extend(f"{note_indent}> {line}" for line in str(note).split("\n"))

    return "\n".join(lines)


def export_markdown(doc: Dict[str, Any]) -> str:
    """将文档结构转换为 Markdown 文本。

    兼容两种输入形状（get_doc 修复后引入真实 API 形状，需向后兼容旧往返形状）：
    - 真实 API（get_doc 修复后返回）：{"name":..., "nodes":[顶层节点...]}
      mubu 文档通常只有一个顶层节点，其 text 即文档标题，children 为大纲正文。
    - 本地往返（markdown_to_doc 返回）：{"node":{...}} 或裸 {"text":..., "children":[...]}

    Args:
        doc: 文档结构（见上两种形状）

    Returns:
        Markdown 文本（首行为 '# 标题'）

    Raises:
        MubuError: 文档结构无效（既无有效 text 也无 children）时
    """
    if doc.get("_plain_text"):
        nodes = doc.get("nodes")
        if (not isinstance(nodes, list) or len(nodes) != 1
                or nodes[0].get("children") or nodes[0].get("note")):
            raise MubuError("无效的纯文本文档结构")
        return f"{_PLAIN_TEXT_MARKER}\n{nodes[0].get('text') or ''}"

    # 新形状：真实 API 或 Markdown 解析器生成的 nodes 顶层数组（优先）。
    if "nodes" in doc:
        nodes = doc["nodes"]
        if not isinstance(nodes, list):
            raise MubuError("无效的文档结构：nodes 必须是数组")
        lines: List[str] = []
        for node in nodes:
            if not isinstance(node, dict):
                raise MubuError("无效的文档结构：顶层节点必须是对象")
            title = str(node.get("text") or "").replace("\n", " ")
            title = _escape_checkbox_text(title)
            checked = _task_checkbox_state(node)
            if checked is not None:
                mark = "x" if checked else " "
                heading = f"# [{mark}] {title}"
            else:
                heading = f"# {title}"
            lines.append(heading)
            for child in node.get("children") or []:
                lines.append(doc_to_markdown(child, level=0))
            note = node.get("note")
            if note:
                lines.extend(f"> {line}" for line in str(note).split("\n"))
        return "\n".join(lines)

    # 旧形状：markdown_to_doc 的单一 node（或裸 node）
    root = doc.get("node") or doc
    if not isinstance(root, dict) or (not root.get("text") and not root.get("children")):
        raise MubuError("无效的文档结构")

    title = str(root.get("text") or "").replace("\n", " ")
    checked = _task_checkbox_state(root)
    title = _escape_checkbox_text(title)
    if checked is not None:
        mark = "x" if checked else " "
        lines = [f"# [{mark}] {title}"]
    else:
        lines = [f"# {title}"]
    for child in root.get("children") or []:
        lines.append(doc_to_markdown(child, level=0))
    # 根节点的 note（备注）在 children 之后输出
    note = root.get("note")
    if note:
        lines.extend(f"> {line}" for line in str(note).split("\n"))
    return "\n".join(lines)


def _safe_filename(name: str) -> str:
    """将文档/文件夹名称转为安全的文件名（去除路径非法字符）。"""
    bad = {'/', ':', '*', '?', '"', '<', '>', '|', chr(92)}
    cleaned = ''.join(
        '_' if ch in bad or ord(ch) < 32 else ch for ch in (name or '')
    ).strip().rstrip(' .')
    if not cleaned:
        return 'untitled'

    device = cleaned.split('.', 1)[0].rstrip(' .').upper()
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
    if device in reserved or re.fullmatch(r'(?:COM|LPT)(?:[1-9]|[¹²³])', device):
        cleaned = '_' + cleaned
    return cleaned


def _unique_filename(name: str, *, stable_id: Optional[str] = None,
                     used_names: Optional[set] = None) -> str:
    """安全化并消解名称冲突。

    传入 ``stable_id`` 时总是把其安全化后的值作为后缀，因而同名条目可按
    稳定 ID 得到与遍历顺序无关的名称。仅传 ``used_names`` 时，首个名称保留，
    冲突项依次添加 ``_2``、``_3``；集合按 Windows 不区分大小写的规则更新。
    """
    base = _safe_filename(name)
    if stable_id is not None:
        candidate = f"{base}--{_safe_filename(str(stable_id))}"
    else:
        candidate = base

    if used_names is None:
        return candidate

    existing = {value.casefold() for value in used_names}
    if candidate.casefold() in existing:
        suffix = 2
        while f"{candidate}_{suffix}".casefold() in existing:
            suffix += 1
        candidate = f"{candidate}_{suffix}"
    used_names.add(candidate)
    return candidate


def doc_to_opml(doc: Dict[str, Any]) -> str:
    """将幕布文档转为 OPML 2.0 XML（兼容 FreeMind / XMind 等大纲工具导入）。

    兼容两种输入形状（与 export_markdown 一致的双形状设计）：
    - 真实 API（get_doc 返回）：{"name":..., "nodes":[顶层节点...]}
      每个顶层 node 成为 <body> 下的一个 <outline>。
    - 本地往返（markdown_to_doc 返回）：{"node":{...}} 或裸 {"text":..., "children":[...]}
    """
    import xml.etree.ElementTree as ET

    def build(node: Dict[str, Any], parent: ET.Element) -> None:
        text = (node.get("text") or "").replace("\n", " ")
        outline = ET.SubElement(parent, "outline", text=text)
        note = node.get("note")
        if note:
            outline.set("_note", note)
        for child in node.get("children") or []:
            build(child, outline)

    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")

    nodes = doc.get("nodes")
    if nodes:
        # 真实 API 形状：每个顶层 node 成为 <body> 下的一个 <outline>
        title = (nodes[0].get("text") or doc.get("name") or "mubu-export").replace("\n", " ")
        ET.SubElement(head, "title").text = title
        body = ET.SubElement(opml, "body")
        for node in nodes:
            build(node, body)
    else:
        # 向后兼容：markdown_to_doc 形状的单一 node（或裸 node）
        root = doc.get("node") or doc
        title = (root.get("text") or "mubu-export").replace("\n", " ")
        ET.SubElement(head, "title").text = title
        body = ET.SubElement(opml, "body")
        build(root, body)

    ET.indent(opml, space="  ")
    return ET.tostring(opml, encoding="utf-8", xml_declaration=True).decode("utf-8")


def doc_to_freeplane(doc: Dict[str, Any]) -> str:
    """将幕布文档转为 FreeMind (Freeplane) XML。

    兼容两种输入形状（与 export_markdown 一致的双形状设计）：
    - 真实 API（get_doc 返回）：{"name":..., "nodes":[顶层节点...]}
      用第一个顶层 node 作为根 <node>（title=其 text），对其 children 递归 build；
      其余顶层 node 作为根 node 的 children 追加（保证不丢内容）。
    - 本地往返（markdown_to_doc 返回）：{"node":{...}} 或裸 {"text":..., "children":[...]}
    """
    import xml.etree.ElementTree as ET

    def build(node: Dict[str, Any], parent: ET.Element) -> None:
        for child in node.get("children") or []:
            text = (child.get("text") or "").replace("\n", " ")
            node_el = ET.SubElement(parent, "node", text=text)
            note = child.get("note")
            if note:
                note_el = ET.SubElement(node_el, "richcontent", type="note")
                ET.SubElement(note_el, "html").text = note
            build(child, node_el)

    mindmap = ET.Element("map", version="1.0.1")

    nodes = doc.get("nodes")
    if nodes:
        # 真实 API 形状：用第一个顶层 node 作为根 <node>
        title = (nodes[0].get("text") or doc.get("name") or "mubu-export").replace("\n", " ")
        root_node = ET.SubElement(mindmap, "node", text=title)
        build(nodes[0], root_node)
        # 其余顶层 node 作为根 node 的 children 追加（不丢内容）
        for extra in nodes[1:]:
            text = (extra.get("text") or "mubu-export").replace("\n", " ")
            extra_el = ET.SubElement(root_node, "node", text=text)
            note = extra.get("note")
            if note:
                note_el = ET.SubElement(extra_el, "richcontent", type="note")
                ET.SubElement(note_el, "html").text = note
            build(extra, extra_el)
    else:
        root = doc.get("node") or doc
        title = (root.get("text") or "mubu-export").replace("\n", " ")
        root_node = ET.SubElement(mindmap, "node", text=title)
        build(root, root_node)

    ET.indent(mindmap, space="  ")
    return ET.tostring(mindmap, encoding="utf-8", xml_declaration=True).decode("utf-8")


def markdown_to_doc(md: str) -> Dict[str, Any]:
    r"""将 Markdown 大纲解析为 ``{"nodes": [...]}``。

    一级标题各自对应一个顶层节点；其后的无序列表是该节点的子树。每一级
    列表缩进使用两个空格。根 note 使用未缩进引用，列表节点的 note 引用行
    比该节点的列表缩进多两个空格。因此 ``# Doc\n- A\n> root note`` 中的
    引用明确属于文档根节点。无标题的普通文本和列表仍按单根旧格式兼容。

    多根文档和空文档使用 ``nodes`` 数组；单根文档另外提供 ``node`` 兼容别名。
    不支持的标题层级、跳级缩进或无法识别的结构会抛出 ``MubuError``，不静默
    丢弃或重排内容。
    """
    # Windows 上用记事本/PowerShell 保存的 .md 常带 UTF-8 BOM（\ufeff），
    # 会导致首行标题解析失败；这里统一剥掉。
    if md and md[0] == "\ufeff":
        md = md[1:]

    if md.startswith(_PLAIN_TEXT_MARKER + "\n"):
        text = md[len(_PLAIN_TEXT_MARKER) + 1:]
        root = {"id": "root", "text": text, "children": []}
        return {"nodes": [root], "node": root, "_plain_text": True}

    lines = md.splitlines()
    if not any(line.strip() for line in lines):
        return {"nodes": []}

    heading_positions = [
        index for index, line in enumerate(lines)
        if re.match(r"^#+[ \t]+", line)
    ]
    if heading_positions:
        first_heading = heading_positions[0]
        if any(line.strip() for line in lines[:first_heading]):
            raise MubuError("一级标题前不支持文档内容")

    has_list = any(re.match(r"^[ ]*- ", line) for line in lines)
    has_note = any(re.match(r"^[ ]*> ?", line) for line in lines)
    if not heading_positions and not has_list and not has_note:
        text = md.strip()
        root = {"id": "root", "text": text, "children": []}
        doc: Dict[str, Any] = {"nodes": [root], "node": root}
        if "\n" in text:
            doc["_plain_text"] = True
        return doc

    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"node_{counter}"

    roots: List[Dict[str, Any]] = []
    current_root: Optional[Dict[str, Any]] = None
    stack: List[Any] = []

    def ensure_root() -> Dict[str, Any]:
        nonlocal current_root
        if current_root is None:
            current_root = {
                "id": "root" if not roots else next_id(),
                "text": "",
                "children": [],
            }
            roots.append(current_root)
            stack.clear()
        return current_root

    def result() -> Dict[str, Any]:
        doc: Dict[str, Any] = {"nodes": roots}
        if len(roots) == 1:
            doc["node"] = roots[0]
        return doc

    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip():
            continue

        heading = re.match(r"^(#+) (.*)$", line)
        if heading:
            if len(heading.group(1)) != 1:
                raise MubuError("Markdown 大纲只支持一级标题作为顶层节点")
            title = heading.group(2)
            checked: Optional[bool] = None
            checkbox = re.match(r"^\[([ xX])\] (.*)$", title)
            if checkbox:
                checked = checkbox.group(1).lower() == "x"
                title = checkbox.group(2)
            title = _unescape_checkbox_text(title)

            root: Dict[str, Any] = {
                "id": "root" if not roots else next_id(),
                "text": title,
                "children": [],
            }
            if checked is not None:
                root["checked"] = checked
                root["taskStatus"] = 2 if checked else 1
            roots.append(root)
            current_root = root
            stack = []
            continue

        if line.startswith("#"):
            raise MubuError(f"无法识别标题行：{line}")

        list_item = re.match(r"^([ ]*)- (.*)$", line)
        if list_item:
            root = ensure_root()
            indent = len(list_item.group(1))
            if indent % 2:
                raise MubuError("列表缩进必须是两个空格的倍数")
            depth = indent // 2
            if depth > 0 and (not stack or stack[-1][0] < depth - 1):
                raise MubuError("列表节点不能跳过父级")
            while stack and stack[-1][0] >= depth:
                stack.pop()
            if depth == 0:
                parent = root
            elif stack and stack[-1][0] == depth - 1:
                parent = stack[-1][1]
            else:
                raise MubuError("列表节点不能跳过父级")

            text = list_item.group(2)
            checked = None
            checkbox = re.match(r"^\[([ xX])\] (.*)$", text)
            if checkbox:
                checked = checkbox.group(1).lower() == "x"
                text = checkbox.group(2)
            text = _unescape_checkbox_text(text)

            node: Dict[str, Any] = {
                "id": next_id(),
                "text": text,
                "children": [],
            }
            if checked is not None:
                node["checked"] = checked
                node["taskStatus"] = 2 if checked else 1
            parent["children"].append(node)
            stack.append((depth, node))
            continue

        note_line = re.match(r"^([ ]*)> ?(.*)$", line)
        if note_line:
            root = ensure_root()
            indent = len(note_line.group(1))
            if indent % 2:
                raise MubuError("备注缩进必须是两个空格的倍数")
            if indent == 0:
                target = root
            else:
                depth = indent // 2 - 1
                target = next(
                    (node for node_depth, node in reversed(stack)
                     if node_depth == depth),
                    None,
                )
                if target is None:
                    raise MubuError("备注引用必须对应一个已有列表节点")

            note_lines = [note_line.group(2)]
            while i < len(lines):
                continuation = re.match(r"^([ ]*)> ?(.*)$", lines[i])
                if continuation is None or len(continuation.group(1)) != indent:
                    break
                note_lines.append(continuation.group(2))
                i += 1
            if target.get("note") is not None:
                raise MubuError("同一节点不能包含多个独立备注块")
            target["note"] = "\n".join(note_lines)
            continue

        raise MubuError(f"无法识别 Markdown 结构（第 {i} 行）：{line}")

    if not heading_positions and not any(re.match(r"^[ ]*- ", line) for line in lines):
        # 无标题、无列表的普通文本继续按旧格式作为单根节点正文。
        text = md.strip()
        root = {"id": "root", "text": text, "children": []}
        return {"nodes": [root], "node": root}

    return result()


def normalize_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """递归补全 changeset 节点缺失的契约字段（对齐网页端 ``tr()`` 序列化器产出）。

    仅对**缺失**字段补保守默认值，已存在的字段原样保留（不动 ``id``/``text``/
    ``children``/``checked`` 等既有内容）：

    - ``note``: ``""``
    - ``collapsed``: ``False``
    - ``finish``: ``False``
    - ``priority``: ``0``
    - ``highlight`` / ``color``: ``""``
    - ``createTime`` / ``modifyTime`` / ``timestamp``: 节点已有则沿用，否则
      ``int(time.time() * 1000)``

    用途：加固 ``build_update_event`` —— 当 ``nodes`` 由 ``markdown_to_doc`` 构造
    （缺上述字段）直喂 ``save`` 时，避免残缺 payload 触发服务端 ``code:17 illegal
    request``（hypothesis 2 闭环）。``get_doc`` 返回的完整 ``nodes`` 经此函数无副作用
    （字段本就齐全，仅对极端缺失项补默认值），不破坏标准 save 路径。
    """
    if not isinstance(node, dict):
        return node
    now = int(time.time() * 1000)
    if node.get("note") is None:
        node["note"] = ""
    if node.get("collapsed") is None:
        node["collapsed"] = False
    if node.get("finish") is None:
        node["finish"] = False
    if node.get("priority") is None:
        node["priority"] = 0
    if node.get("highlight") is None:
        node["highlight"] = ""
    if node.get("color") is None:
        node["color"] = ""
    if node.get("createTime") is None:
        node["createTime"] = node.get("timestamp") or now
    if node.get("modifyTime") is None:
        node["modifyTime"] = node.get("timestamp") or now
    if node.get("timestamp") is None:
        node["timestamp"] = now
    # 递归归一化子树
    for child in node.get("children") or []:
        normalize_node(child)
    return node


def format_list(data: Dict) -> str:
    """格式化文档列表为可读文本。

    真机 get_list 返回文档列表字段为 ``documents``；旧版/个别环境可能用 ``docs``，
    此处优先读 ``documents`` 并兜底 ``docs``，避免真机上读不到文档。
    """
    lines = []
    folders = data.get("folders", []) or []
    docs = data.get("documents") or data.get("docs") or []

    if folders:
        lines.append("📁 文件夹:")
        for f in folders:
            name = f.get("name", "未命名")
            fid = f.get("id", "")
            lines.append(f"  [{fid}] {name}")

    if docs:
        lines.append("\n📄 文档:")
        for d in docs:
            name = d.get("name", "未命名")
            did = d.get("id", "")
            lines.append(f"  [{did}] {name}")

    if not folders and not docs:
        lines.append("（空）")

    return "\n".join(lines)


def format_search(results: List[Dict]) -> str:
    """格式化搜索结果为可读文本，复用 format_list 的分区展示风格。

    将匹配项分为 📁 文件夹 / 📄 文档 两区，命中项附带路径（path）便于定位。
    """
    folders = [r for r in results if r.get("type") == "folder"]
    docs = [r for r in results if r.get("type") == "doc"]
    lines = []

    if folders:
        lines.append("📁 文件夹:")
        for f in folders:
            path = f.get("path", "")
            suffix = f"  ({path})" if path else ""
            lines.append(f"  [{f.get('id')}] {f.get('name')}{suffix}")

    if docs:
        lines.append("\n📄 文档:")
        for d in docs:
            path = d.get("path", "")
            suffix = f"  ({path})" if path else ""
            lines.append(f"  [{d.get('id')}] {d.get('name')}{suffix}")

    if not folders and not docs:
        lines.append("（无匹配结果）")

    return "\n".join(lines)


_SPAN_RE = re.compile(r"^<span[^>]*>(.*)</span>$", re.S)


def strip_span(text: str) -> str:
    """剥掉最外层 ``<span ...>...</span>``，返回内层文本（无标签则原样返回）。"""
    if not text:
        return text
    m = _SPAN_RE.match(text)
    return m.group(1) if m else text


def style_text(text: str, bold: bool = False, italic: bool = False,
               underline: bool = False, color: Optional[str] = None) -> str:
    """把文本包成幕布富文本 ``<span class="...">``（class 组合，2026-09 抓包核对）。

    - 加粗 ``bold`` / 斜体 ``italic`` / 下划线 ``underline``
    - 颜色由 ``methods.colors`` 注册表解析；black 表示默认文本色
    例：``style_text("Use", bold=True, color="red")``
        → ``<span class="bold text-color-red">Use</span>``
    """
    escaped_text = _html_escape("" if text is None else str(text), quote=True)
    classes = []
    if bold:
        classes.append("bold")
    if italic:
        classes.append("italic")
    if underline:
        classes.append("underline")
    if color is not None:
        # 延迟导入可避免 convert 与 methods.headings 的导入环。
        from mubu.methods.colors import color_class

        resolved_color_class = color_class(color)
        if resolved_color_class:
            classes.append(resolved_color_class)
    if not classes:
        return escaped_text
    return f'<span class="{" ".join(classes)}">{escaped_text}</span>'


def mask_html(text: str) -> str:
    """将文本转为幕布原生挖空 HTML。

    文本会先进行 HTML 转义，避免用户内容被当作标签解析。
    """
    return f'<span class="mask">{_html_escape("" if text is None else str(text), quote=True)}</span>'


def highlight_html(text: str, color: str = "yellow") -> str:
    """将文本转为幕布原生高亮 HTML。

    当前只有 ``yellow`` 有真实抓包依据；名称不区分大小写并允许首尾空格。
    """
    supported = {"yellow"}
    if not isinstance(color, str):
        raise TypeError("高亮颜色必须是字符串")
    normalized = color.strip().lower()
    if normalized not in supported:
        names = ", ".join(sorted(supported))
        raise ValueError(f"不支持的幕布高亮颜色 {color!r}；已支持颜色：{names}")
    escaped = _html_escape("" if text is None else str(text), quote=True)
    return f'<span class="highlight-{normalized}">{escaped}</span>'


def _rich_text_element_id() -> str:
    """生成网页端编辑器风格的 10 位 base62 元素 id（mention / 外链 / 节点引用共用）。

    抓包实证：编辑器 ``generateId()`` 产出 10 位 base62
    （如 mention 的 ``gX7RlC1Kh9``、链接占位 ``Hk9aIIFiVQ``）。
    早期使用 ``token_hex(16)``（32 位十六进制）时，网页端解析出的 link token
    与 id 不匹配，交互一次后会被规范化剥离（实测「网页版点一次后链接消失」）。
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))


def formula_html(latex: str, inline: bool = True) -> str:
    """Encode a captured Mubu formula element.

    Mubu stores the LaTeX source URL-encoded in ``data-raw`` and renders the
    formula from three zero-width characters inside a non-editable span.  An
    inline formula is surrounded by literal-dollar spans, matching the
    captured editor output.
    """
    raw = quote("" if latex is None else str(latex), safe="")
    # 注意：class 不能带前导空格！抓包形态 `class=" formula"` 是网页端
    # ["", "formula"].join(" ") 的中间态产物，渲染器按空格切分后认不出，会退化成源码。
    # 双端实测：class="formula"（无前导空格）网页版与桌面版都能正常渲染。
    formula = (
        f'<span class="formula" data-raw="{raw}" '
        'contenteditable="false">\u200b\u200b\u200b</span>'
    )
    # 不要包 <span>$</span>：序列化器 ts 从不输出 $；$ 只是 Markdown 源标记/编辑装饰。
    # 网页端默认非 Markdown 模式会把 $ 读回成普通文本 → 页面多显示两个 $。
    return formula


def mention_html(doc_id: str, name: str) -> str:
    """Encode a captured Mubu document mention anchor."""
    document_id = "" if doc_id is None else str(doc_id)
    display_name = "" if name is None else str(name)
    element_id = _rich_text_element_id()
    link = f"https://mubu.com/doc{document_id}"
    payload = {
        "type": 2,
        "id": element_id,
        "mentionType": 1,
        "mentionNotify": False,
        "token": document_id,
        "link": link,
        "textEn": "",
        "text": display_name,
        "docId": document_id,
    }
    encoded_payload = quote(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        safe="",
    )
    return (
        '<a class="mention mm-iconfont" target="_blank" rel="noreferrer" '
        'spellcheck="false" contenteditable="false" '
        f'href="{_html_escape(link, quote=True)}" '
        f'id="mention-{element_id}" data-mention="{encoded_payload}" '
        f'data-type="1" data-token="{_html_escape(document_id, quote=True)}">'
        f'{_html_escape(display_name, quote=True)}</a>'
    )


def node_mention_html(doc_id: str, node_id: str, text: str) -> str:
    """Encode a captured Mubu node-mention element."""
    document_id = "" if doc_id is None else str(doc_id)
    referenced_node_id = "" if node_id is None else str(node_id)
    display_text = "" if text is None else str(text)
    element_id = _rich_text_element_id()
    encoded_text = quote(
        json.dumps([{"type": 1, "text": display_text}],
                   ensure_ascii=False, separators=(",", ":")),
        safe="",
    )
    return (
        f'<span class="node-mention" id="{element_id}" '
        'spellcheck="false" contenteditable="false" '
        f'data-doc="{_html_escape(document_id, quote=True)}" '
        f'data-node="{_html_escape(referenced_node_id, quote=True)}" '
        f'data-text="{encoded_text}"><span>{_html_escape(display_text, quote=True)}'
        '</span></span>'
    )

# --------------------------------------------------------------------------- #
# 幕布原生表格：二维数组 <-> 表格 HTML（幕布能力，系统命令层，非 methods）
# 真机模板（从真实表格逐字段抄录）：
#   <div class="table-container"><table class="auto-table" border="1"
#     cellspacing="0" style="border-collapse: collapse;"><thead><tr><th
#     tabindex="0" contenteditable="false"><span>..</span></th></tr></thead>
#     <tbody><tr><td tabindex="0" contenteditable="false"><span>..</span></td></tr></tbody></table></div>
# 编辑器还会往单元格里注入 column-select-btn / row-select-btn 两个 UI 辅助 div，
# 那是编辑器交互用的、不属于数据，生成时可以省略，解析时要跳过。
# --------------------------------------------------------------------------- #
TABLE_CONTAINER_OPEN = '<div class="table-container">'
TABLE_OPEN = ('<table class="auto-table" border="1" cellspacing="0" '
              'style="border-collapse: collapse;">')
_CELL_ATTR = 'tabindex="0" contenteditable="false"'

_SELECT_BTN_RE = re.compile(
    r'<div class="(?:column-select-btn|row-select-btn)"[^>]*>.*?</div>', re.S | re.I)


def _escape_cell(value: Any) -> str:
    """单元格值 -> HTML 转义（& < >），换行转 <br>。"""
    s = "" if value is None else str(value)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace("\n", "<br>"))


def _unescape_cell(text: str) -> str:
    """HTML 实体还原（与 _escape_cell 对称）。"""
    return (text.replace("<br>", "\n").replace("&nbsp;", " ")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


def rows_to_table_html(rows, header: bool = True) -> str:
    """二维数组 -> 幕布原生表格 HTML（与编辑器同构，可直接写进节点 text）。

    Args:
        rows: 形如 [[表头...], [数据...], ...]；None 当空串处理。
        header: 为 True 时首行渲染成 <thead><th>，否则全部当数据行。
    """
    data = [list(r) for r in rows or []]
    if not data:
        return ""
    width = max((len(r) for r in data), default=0)

    def cell(tag: str, value: Any) -> str:
        return f'<{tag} {_CELL_ATTR}><span>{_escape_cell(value)}</span></{tag}>'

    parts = [TABLE_CONTAINER_OPEN, TABLE_OPEN]
    body_start = 0
    if header:
        parts.append("<thead><tr>")
        parts.extend(cell("th", v) for v in data[0])
        parts.append("</tr></thead>")
        body_start = 1
    parts.append("<tbody>")
    for row in data[body_start:]:
        padded = list(row) + [""] * (width - len(row))
        parts.append("<tr>")
        parts.extend(cell("td", v) for v in padded)
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def table_html_to_rows(html: str):
    """幕布原生表格 HTML -> 二维数组（表头行在最前，顺序与文档一致）。"""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html or "", flags=re.S | re.I):
        cells = []
        for m in re.finditer(r"<(t[hd])[^>]*>(.*?)</\1>", tr, flags=re.S | re.I):
            inner = _SELECT_BTN_RE.sub("", m.group(2))
            text = re.sub(r"<br\s*/?>", "\u0001", inner, flags=re.I)
            text = re.sub(r"<[^>]+>", "", text)
            text = _unescape_cell(text).replace("\u0001", "\n")
            cells.append(text.strip())
        if cells:
            rows.append(cells)
    return rows


def markdown_table_to_rows(md: str):
    r"""Markdown 表格文本 -> 二维数组（自动跳过 ``|---|`` 分隔行）。

    本函数对应 :func:`rows_to_markdown_table` 的可逆转义：``\\`` / ``\|`` /
    ``\n`` / ``\r`` 分别表示反斜杠、竖线、换行和回车。表格分隔符两侧各
    一个空格是格式填充，除此之外保留单元格首尾空白。列数不一致时显式报错，
    避免不完整数据被静默拆分或合并。
    """
    rows = []
    width = None

    def split_row(line: str) -> List[str]:
        cells: List[str] = []
        current: List[str] = []
        i = 0
        while i < len(line):
            char = line[i]
            if char == "\\" and i + 1 < len(line):
                next_char = line[i + 1]
                if next_char == "|":
                    current.extend((char, next_char))
                    i += 2
                    continue
                if next_char == "\\":
                    current.extend((char, next_char))
                    i += 2
                    continue
                if next_char in "nr":
                    current.extend((char, next_char))
                    i += 2
                    continue
            if char == "|":
                cells.append("".join(current))
                current = []
            else:
                current.append(char)
            i += 1
        cells.append("".join(current))

        # 规范形式有首尾竖线；兼容省略首尾竖线的普通 Markdown 表格。
        if line.lstrip().startswith("|") and cells and not cells[0].strip():
            cells.pop(0)
        if line.rstrip().endswith("|") and cells and not cells[-1].strip():
            cells.pop()

        def decode(cell: str) -> str:
            # 去掉分隔符附近的一格格式空格，不吞掉单元格自身的空白。
            if cell.startswith(" "):
                cell = cell[1:]
            if cell.endswith(" "):
                cell = cell[:-1]
            out: List[str] = []
            i = 0
            escapes = {"\\": "\\", "|": "|", "n": "\n", "r": "\r"}
            while i < len(cell):
                if cell[i] == "\\" and i + 1 < len(cell) and cell[i + 1] in escapes:
                    out.append(escapes[cell[i + 1]])
                    i += 2
                else:
                    out.append(cell[i])
                    i += 1
            return "".join(out)

        return [decode(cell) for cell in cells]

    separator_cell = re.compile(r":?-{3,}:?")
    for line in (md or "").splitlines():
        if not line.strip():
            continue
        cells = split_row(line)
        has_outer_pipe = line.lstrip().startswith("|") or line.rstrip().endswith("|")
        if len(cells) < 2 and not has_outer_pipe:
            continue
        is_separator = all(separator_cell.fullmatch(cell.strip()) for cell in cells)
        if width is None:
            width = len(cells)
        elif len(cells) != width:
            raise ValueError(
                f"Markdown 表格列数不一致：预期 {width} 列，实际 {len(cells)} 列"
            )
        if not is_separator:
            rows.append(cells)
    return rows


def rows_to_markdown_table(rows) -> str:
    """二维数组 -> Markdown 表格文本（首行当表头）。"""
    data = [list(r) for r in rows or []]
    if not data:
        return ""
    width = max(len(r) for r in data)
    pad = lambda r: list(r) + [""] * (width - len(r))  # noqa: E731
    def escape(cell: Any) -> str:
        value = "" if cell is None else str(cell)
        return (value.replace("\\", "\\\\").replace("|", "\\|")
                .replace("\r", "\\r").replace("\n", "\\n"))

    head, body = pad(data[0]), [pad(r) for r in data[1:]]
    out = ["| " + " | ".join(escape(c) for c in head) + " |",
           "| " + " | ".join("---" for _ in head) + " |"]
    out.extend("| " + " | ".join(escape(c) for c in r) + " |" for r in body)
    return "\n".join(out)

def link_html(text: str, url: str, new_tab: bool = False) -> str:
    """把纯文本标签包成幕布原生超链接（就是节点 text 里的 ``<a>`` HTML）。

    ``text`` 始终按纯文本处理并进行 HTML 转义，不接受调用方传入原始富文本。
    ``url`` 只允许 http/https/mailto scheme 或相对链接；其他 scheme 和控制字符拒绝。

    模板取自网页端序列化器 ``tl``（app.js @3178549）。常用变体虽然"网页端能点"，
    但桌面端会在交互后剥离裸 ``<a>``，因此必须用带 ``content-link`` 的规范模板。

    Args:
        text: 纯文本显示文字；为空时退化为显示 URL 本身
        url: 链接地址（其中的 HTML 特殊字符会被正确转义）
        new_tab: 是否加 ``target="_blank"``

    Raises:
        ValueError: URL scheme 不安全、Web URL 缺少主机名或 URL 含控制字符时。
    """
    safe_url = (url or "").strip()
    if not safe_url or any(ord(char) < 32 or ord(char) == 127 for char in safe_url):
        raise ValueError("链接地址为空或包含控制字符")
    try:
        parsed = urlsplit(safe_url)
    except ValueError as exc:
        raise ValueError("无效的链接地址") from exc

    scheme = parsed.scheme.lower()
    if scheme and scheme not in {"http", "https", "mailto"}:
        raise ValueError(f"不支持的链接 scheme: {scheme}")
    if scheme in {"http", "https"}:
        try:
            has_host = bool(parsed.hostname)
        except ValueError as exc:
            raise ValueError("无效的 Web 链接主机名") from exc
        if not parsed.netloc or not has_host:
            raise ValueError("Web 链接必须包含主机名")
    elif scheme == "mailto" and not parsed.path:
        raise ValueError("mailto 链接必须包含收件地址")

    label = _escape_cell(text or safe_url)
    href = _html_escape(safe_url, quote=True)
    # 网页端真实模板（序列化器 tl，app.js @3178549）——不要写成裸 <a href>：
    # 桌面端不认识裸 <a>，交互一次后会被规范化剥离（实测"点击一次就没了"）。
    # 注意：原生模板没有 contenteditable（这点与 mention 不同），target 恒为 _blank。
    element_id = _rich_text_element_id()
    return (
        f'<a class="content-link" data-id="{element_id}" target="_blank" '
        f'spellcheck="false" rel="noreferrer" href="{href}">'
        f'<span class="content-link-text">{label}</span></a>'
    )
