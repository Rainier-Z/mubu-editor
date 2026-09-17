"""系统级 CLI 命令处理与分发。"""

import getpass
import json
import re
import sys

from mubu.config import (
    _safe_local_path,
    logger,
)
from mubu.convert import (
    doc_to_freeplane,
    doc_to_opml,
    export_markdown,
    format_list,
    format_search,
    formula_html,
    highlight_html,
    markdown_to_doc,
    mask_html,
    mention_html,
    node_mention_html,
)
from mubu.methods.headings import append_headings, set_headings


def _login(client, _args):
    if not client.phone:
        try:
            client.phone = input("请输入幕布手机号: ").strip()
        except EOFError:
            pass
    if not client.password:
        try:
            client.password = getpass.getpass("请输入幕布密码: ")
        except EOFError:
            pass
    result = client.login()
    print(f"登录成功: {result['username']} (ID: {result['user_id']})")


def _list(client, args):
    data = client.get_list(args.folder, include_trashed=args.include_trash)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(format_list(data))


def _mkdir(client, args):
    folder_id = client.create_folder(args.name, args.parent)
    print(f"创建文件夹成功: {folder_id}")


def _create(client, args):
    if args.md:
        safe_path = _safe_local_path(args.md)
        md_doc = markdown_to_doc(safe_path.read_text(encoding="utf-8"))
        content = json.dumps({"nodes": md_doc["nodes"]}, ensure_ascii=False)
    else:
        content = args.content
    doc_id = client.create_doc(args.name, args.folder, content)
    print(f"创建文档成功: {doc_id}")


def _get(client, args):
    doc = client.get_doc(args.doc_id)
    if args.export == "json":
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    else:
        print(export_markdown(doc))


def _save(client, args):
    if args.md:
        safe_path = _safe_local_path(args.md)
        md_doc = markdown_to_doc(safe_path.read_text(encoding="utf-8"))
        new_definition = {"nodes": md_doc["nodes"]}
    elif args.file:
        safe_path = _safe_local_path(args.file)
        new_definition = json.loads(safe_path.read_text(encoding="utf-8"))
    elif args.content:
        new_definition = json.loads(args.content)
    else:
        new_definition = json.loads(sys.stdin.read())

    client.sync_doc_definition(args.doc_id, new_definition)
    print("保存成功")


def _append(client, args):
    ids = append_headings(client, args.doc_id, args.texts, level=args.level)
    print(f"已追加 {len(ids)} 个 {args.level} 级标题：{' / '.join(args.texts)}")
    print("节点ID：" + ", ".join(ids))


def _set_headings(client, args):
    result = set_headings(client, args.doc_id, args.texts, level=args.level)
    print(f"完成：改写 {result['updated']} 个，新建 {len(result['created'])} 个")
    print(f"{args.level} 级标题：" + " / ".join(args.texts))


def _mask(client, args):
    """用幕布原生挖空样式改写指定节点。"""
    path = _parse_node_path(args.path)
    html = mask_html(args.text)
    wrote = client.set_node_fields(args.doc_id, path, {"text": html})
    print(("已改写为挖空" if wrote else "内容相同，未写入") + f"：{args.text}")


def _highlight(client, args):
    """用幕布原生高亮样式改写指定节点。"""
    path = _parse_node_path(args.path)
    html = highlight_html(args.text, color=args.color)
    wrote = client.set_node_fields(args.doc_id, path, {"text": html})
    color_names = {"yellow": "黄色"}
    color_label = color_names.get(args.color.strip().lower(), args.color)
    print((f"已改写为{color_label}高亮" if wrote else "内容相同，未写入") + f"：{args.text}")


def _formula(client, args):
    """用幕布原生公式 HTML 改写指定节点。"""
    path = _parse_node_path(args.path)
    html = formula_html(args.latex, inline=not args.block)
    wrote = client.set_node_fields(args.doc_id, path, {"text": html})
    print(("已写入公式" if wrote else "内容相同，未写入") + f"：{args.latex}")


def _mention(client, args):
    """用幕布原生文档内链 HTML 改写指定节点。"""
    path = _parse_node_path(args.path)
    html = mention_html(args.target_doc, args.name)
    wrote = client.set_node_fields(args.doc_id, path, {"text": html})
    print(("已写入文档内链" if wrote else "内容相同，未写入") + f"：{args.name}")


def _node_mention(client, args):
    """用幕布原生节点引用 HTML 改写指定节点。"""
    path = _parse_node_path(args.path)
    html = node_mention_html(args.target_doc, args.target_node, args.text)
    wrote = client.set_node_fields(args.doc_id, path, {"text": html})
    print(("已写入节点引用" if wrote else "内容相同，未写入") + f"：{args.text}")


def _summary_create(client, args):
    """为多个成员节点创建同一概要。"""
    paths = [_parse_node_path(spec) for spec in args.members]
    summary_id = client.create_summary(
        args.doc_id, paths, text=getattr(args, "text", ""))
    print(f"已创建概要：{summary_id}（覆盖 {len(paths)} 个节点）")


def _summary_delete(client, args):
    """从多个成员节点删除指定概要。"""
    paths = [_parse_node_path(spec) for spec in args.members]
    count = client.delete_summary(args.doc_id, paths, args.summary_id)
    print(f"已删除概要：{args.summary_id}（影响 {count} 个节点）")


def _emoji(client, args):
    """设置指定节点的 Emoji 字段。"""
    path = _parse_node_path(args.path)
    wrote = client.set_node_fields(args.doc_id, path, {"emoji": args.value})
    print(("已设置 Emoji" if wrote else "内容相同，未写入") + f"：{args.value}")


def _task(client, args):
    """设置指定节点的 taskStatus 字段。"""
    path = _parse_node_path(args.path)
    wrote = client.set_node_fields(args.doc_id, path, {"taskStatus": args.status})
    print(("已设置待办状态" if wrote else "内容相同，未写入") + f"：{args.status}")


def _collapse(client, args):
    """折叠或展开指定节点。"""
    path = _parse_node_path(args.path)
    collapsed = not args.expand
    wrote = client.set_node_fields(args.doc_id, path, {"collapsed": collapsed})
    action = "展开" if args.expand else "折叠"
    print((f"已{action}" if wrote else "内容相同，未写入") + f"：{args.path}")


def _move_node(client, args):
    """按节点路径移动节点，并可跨父节点。"""
    path = _parse_node_path(args.path)
    parent_path = _parse_node_path(args.parent) if args.parent else None
    moved = client.move_node(args.doc_id, path, args.index, parent_path)
    print(("已移动节点" if moved else "节点位置未变化") + f"：{args.path} -> {args.index}")


def _image(client, args):
    """从本地图片文件上传并附加到指定节点。"""
    path = _parse_node_path(args.path)
    # Validate at the CLI boundary as well as in the client upload path.  Keep
    # the original spelling for the client so existing relative-path callers
    # and diagnostics remain stable.
    _safe_local_path(args.file)
    wrote = client.attach_image(
        args.doc_id, path, args.file, width=getattr(args, "width", None))
    print("已附加图片" if wrote else "图片内容相同，未写入")


def _link_nodes(client, args):
    """在两个节点之间创建思维导图连接线。"""
    from_path = _parse_node_path(args.from_path)
    to_path = _parse_node_path(args.to_path)
    wrote = client.link_nodes(args.doc_id, from_path, to_path, side=args.side)
    print("已创建节点连接线" if wrote else "连接线已存在，未写入")


def _delete(client, args):
    if not args.yes:
        logger.warning(
            "移入本地回收站（云端仍在，可用 restore 恢复；purge 可彻底删除）："
            "即将软删除幕布%s %s。确认请加 --yes 重新执行。",
            "文档" if args.type == "doc" else "文件夹", args.id,
        )
        sys.exit(1)
    client.trash_item(args.id, args.type)
    print(f"已移入本地回收站: {args.id}（restore 可恢复，purge 可彻底删除）")


def _restore(client, args):
    if client.restore_item(args.id):
        print(f"已恢复: {args.id}")
    else:
        print(f"未找到回收站项: {args.id}")


def _purge(client, args):
    if not args.yes:
        logger.warning(
            "彻底删除不可逆：将调用服务端真实删除 %s，且不可恢复。"
            "确认请加 --yes 重新执行。", args.id,
        )
        sys.exit(1)
    client.purge_item(args.id, item_type=args.type)
    print(f"已彻底删除: {args.id}（不可恢复）")


def _trash(client, _args):
    items = client.list_trash()
    if not items:
        print("回收站为空")
    else:
        for item in items:
            print(
                f"{item.get('id')} / {item.get('type')} / "
                f"{item.get('name')} / {item.get('deleted_at')}"
            )


def _move(client, args):
    client.move(args.item_id, args.target, args.type)
    print(f"移动成功: {args.item_id} -> {args.target}")


def _search(client, args):
    result = client.search(
        args.keyword,
        max_depth=args.max_depth,
        limit=args.limit,
        include_trashed=args.include_trash,
    )
    results = result["results"]
    if result.get("truncated"):
        logger.warning(
            "搜索因达到上限（limit=%s）而提前结束，结果可能不完整",
            result.get("limit"),
        )
    if result.get("partial"):
        logger.warning(
            "搜索过程中有 %s 个项目读取失败，结果可能不完整：%s",
            len(result.get("errors") or []),
            "; ".join(
                f"{item.get('type')}:{item.get('id')}" for item in result.get("errors") or []
            ),
        )
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(format_search(results))


def _export_tree(client, args):
    stats = client.export_tree(args.folder, args.output, max_depth=args.max_depth)
    summary = f"导出完成: {stats['docs']} 文档 / {stats['folders']} 文件夹"
    if stats["errors"]:
        summary += f"（{stats['errors']} 失败）"
    print(summary)


def _rename(client, args):
    if args.type == "doc":
        client.rename_doc(args.id, args.name)
    else:
        client.rename_folder(args.id, args.name)
    print(f"重命名成功: {args.id} -> {args.name}")


def _opml(client, args):
    doc = client.get_doc(args.doc_id)
    if args.format == "freeplane":
        print(doc_to_freeplane(doc))
    else:
        print(doc_to_opml(doc))


def _del_node(client, args):
    """删除文档内的顶层节点（级联删除子树）。破坏性操作，需 --yes 守卫。"""
    if not args.yes:
        print("删除文档内节点不可逆（会级联删除整棵子树），确认请加 --yes 重新执行。")
        raise SystemExit(1)
    n = client.delete_top_nodes(args.doc_id, args.index)
    print(f"已删除 {n} 个顶层节点（及其子树）")


def _table(client, args):
    """把 Markdown 表格 / 二维 JSON 生成为幕布原生表格。

    表格是幕布原生能力（右键添加表格），存储为节点 text 里的 HTML 表格，
    因此这里只做格式转换 + 复用 append_top_nodes / insert_child_nodes /
    set_node_fields。
    """
    import json as _json
    import sys as _sys

    from mubu.config import MubuError, _safe_local_path
    from mubu.convert import markdown_table_to_rows, rows_to_table_html

    parent_spec = getattr(args, "parent_path", None)
    if args.replace is not None and parent_spec is not None:
        raise MubuError("--replace 不能与 --parent-path 同时使用")
    parent_path = None
    if parent_spec is not None:
        try:
            parent_path = _parse_node_path(parent_spec)
        except ValueError as exc:
            raise MubuError(f"无效父节点路径：{exc}") from exc

    if args.md:
        rows = markdown_table_to_rows(_safe_local_path(args.md).read_text(encoding="utf-8"))
    elif args.json:
        rows = _json.loads(_safe_local_path(args.json).read_text(encoding="utf-8"))
    else:
        rows = _json.loads(_sys.stdin.read())
    if not rows:
        raise MubuError("未解析到任何表格行（检查输入格式）")
    html = rows_to_table_html(rows, header=not args.no_header)
    shape = f"{len(rows)} 行 x {max(len(r) for r in rows)} 列"
    if args.replace is not None:
        if args.replace < 0:
            raise MubuError("--replace 序号不能为负数")
        client.set_node_fields(args.doc_id, ["nodes", args.replace], {"text": html})
        print(f"已替换顶层节点 {args.replace}：原生表格 {shape}")
    elif parent_path is not None:
        ids = client.insert_child_nodes(args.doc_id, parent_path, [html])
        print(f"已在 {parent_spec} 下插入原生表格 {shape} node={(ids or [''])[0]}")
    else:
        ids = client.append_top_nodes(args.doc_id, [{"text": html}])
        print(f"已追加原生表格 {shape} node={(ids or [''])[0]}")


def _node_at_path(nodes, path):
    """Resolve an alternating nodes/index/children/index path within a document."""
    try:
        if path[0] != "nodes":
            raise IndexError
        node = nodes[path[1]]
        for offset in range(2, len(path), 2):
            if path[offset] != "children":
                raise IndexError
            node = node.get("children", [])[path[offset + 1]]
        return node
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise IndexError("节点路径超出范围或结构无效") from exc


def _export_table(client, args):
    """把文档里指定节点的原生表格导出为 Markdown / CSV。"""
    from mubu.config import MubuError
    from mubu.convert import rows_to_markdown_table, table_html_to_rows

    index = getattr(args, "index", None)
    path_spec = getattr(args, "path", None)
    if (index is None) == (path_spec is None):
        raise MubuError("必须且只能指定 --index 或 --path")

    doc = client.get_doc(args.doc_id)
    nodes = doc.get("nodes", [])
    if index is not None:
        if index < 0 or index >= len(nodes):
            raise MubuError(f"顶层序号 {index} 超出范围（当前 {len(nodes)} 个节点）")
        node = nodes[index]
        node_label = f"顶层节点 {index}"
    else:
        try:
            path = _parse_node_path(path_spec)
            node = _node_at_path(nodes, path)
        except ValueError as exc:
            raise MubuError(f"无效节点路径：{exc}") from exc
        except IndexError as exc:
            raise MubuError(f"节点路径 {path_spec} 超出范围") from exc
        node_label = f"节点路径 {path_spec}"

    rows = table_html_to_rows(node.get("text") or "")
    if not rows:
        raise MubuError(f"{node_label} 不是表格（未解析到表格行）")
    if args.format == "csv":
        for r in rows:
            cells = []
            for cell in r:
                value = str(cell)
                leading = len(value) - len(value.lstrip())
                if value[leading:leading + 1] in ("=", "+", "-", "@"):
                    value = value[:leading] + "'" + value[leading:]
                cells.append('"' + value.replace('"', '""') + '"')
            print(",".join(cells))
    else:
        print(rows_to_markdown_table(rows))


def _parse_node_path(spec):
    """解析严格节点路径：``0.1`` 或 ``nodes,0,children,1``。"""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("路径不能为空")
    spec = spec.strip()

    def parse_index(segment):
        segment = segment.strip()
        if not re.fullmatch(r"[0-9]+", segment):
            raise ValueError(f"路径序号必须为非负整数：{segment!r}")
        return int(segment)

    if "," in spec:
        parts = [part.strip() for part in spec.split(",")]
        if any(not part for part in parts):
            raise ValueError("路径不能包含空段")
        if parts[0] != "nodes" or len(parts) < 2 or len(parts) % 2:
            raise ValueError("结构化路径必须以 nodes,序号 开始并按 children,序号 延伸")
        path = ["nodes", parse_index(parts[1])]
        for offset in range(2, len(parts), 2):
            if parts[offset] != "children":
                raise ValueError("结构化路径的节点层级必须使用 children")
            path.extend(["children", parse_index(parts[offset + 1])])
        return path

    parts = spec.split(".")
    if any(not part.strip() for part in parts):
        raise ValueError("路径不能包含空段")
    indices = [parse_index(part) for part in parts]
    path = ["nodes", indices[0]]
    for index in indices[1:]:
        path.extend(["children", index])
    return path


def _insert_child(client, args):
    """在已有节点下插入子节点（幕布原生 create changeset，系统命令）。"""
    parent_path = _parse_node_path(args.parent)
    ids = client.insert_child_nodes(args.doc_id, parent_path, args.texts)
    print(f"已在 {args.parent} 下插入 {len(ids)} 个子节点：{' / '.join(args.texts)}")
    print("节点ID：" + ", ".join(ids))


def _link(client, args):
    """把文字变成幕布原生超链接：改写已有节点，或新建一条链接节点。"""
    from mubu.convert import link_html

    html = link_html(args.text or args.url, args.url, new_tab=args.new_tab)
    label = args.text or args.url
    if args.path:
        wrote = client.set_node_fields(args.doc_id, _parse_node_path(args.path), {"text": html})
        print(("已改写为链接" if wrote else "内容相同，未写入") + "：" + label)
    elif args.parent:
        ids = client.insert_child_nodes(args.doc_id, _parse_node_path(args.parent), [html])
        print("已在 " + args.parent + " 下新建链接节点：" + (ids[0] if ids else ""))
    else:
        ids = client.append_top_nodes(args.doc_id, [{"text": html}])
        print("已新建链接节点：" + (ids[0] if ids else ""))


def _view(client, args):
    """切换文档视图（大纲 / 思维导图 / 演示）。"""
    client.set_view_type(args.doc_id, args.view)
    print("已把文档切换到 %s 视图：%s" % (args.view.upper(), args.doc_id))


def _share(client, args):
    """开启 / 刷新 / 关闭文档分享链接。"""
    if args.close:
        client.close_share_link(args.doc_id)
        print("已关闭分享：" + args.doc_id)
        return
    link = (client.refresh_share_link(args.doc_id) if args.refresh
            else client.create_share_link(args.doc_id))
    print(("已刷新分享链接：" if args.refresh else "已开启分享：") + link)


def _refs(client, args):
    """列出「谁引用了我」——引用本文档的其他文档（双向链接）。"""
    import re

    items = client.list_backlinks(args.doc_id)
    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return
    if not items:
        print("没有任何文档引用本文档")
        return
    print("共 %d 处引用：" % len(items))
    for it in items:
        doc_name = it.get("docName") or it.get("docId") or "?"
        raw = ((it.get("node") or {}).get("text") or "")
        snippet = re.sub(r"<[^>]+>", "", raw)
        snippet = " ".join(snippet.split())[:60] or "(空)"
        print("  [%s] %s  <- %s" % (it.get("docId"), doc_name, snippet))


def _templates(client, args):
    """列出模板（推荐 / 我的 / 最近）。"""
    data = client.list_templates()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    for key, label in (("personal", "我的模板"), ("recent", "最近使用"),
                       ("dailyNote", "每日笔记"), ("recommend", "推荐（按分类）")):
        items = data.get(key) or []
        if not items:
            continue
        print("== %s ==" % label)
        if key == "recommend":
            for cat in items:
                names = ["%s(%s)" % (i.get("name"), i.get("uuid"))
                         for i in (cat.get("items") or [])]
                if names:
                    print("  [分类 %s] %s" % (cat.get("categoryId"), " / ".join(names)))
        else:
            for it in items:
                print("  %s(%s)  使用 %s 次" % (it.get("name"), it.get("uuid"), it.get("useCount")))


def _template_use(client, args):
    """用模板创建一篇新文档。"""
    doc_id = client.create_doc_from_template(args.uuid, args.name, args.folder)
    print("已用模板创建文档：%s" % doc_id)
    print("  https://mubu.com/app/edit/home/%s" % doc_id)


def _import_doc(client, args):
    """通过官方导入通道创建带内容的文档（一次请求）。"""
    from mubu.config import MubuError, _safe_local_path
    from mubu.convert import markdown_to_doc

    if args.md:
        md = _safe_local_path(args.md).read_text(encoding="utf-8")
        converted = markdown_to_doc(md)
        nodes = converted.get("nodes") if isinstance(converted, dict) else None
    elif args.json:
        payload = json.loads(_safe_local_path(args.json).read_text(encoding="utf-8"))
        nodes = payload.get("nodes") if isinstance(payload, dict) else payload
    else:
        nodes = []
    if not isinstance(nodes, list) or not nodes:
        raise MubuError("没有可导入的节点（用 --md 或 --json 指定内容）")
    doc_id = client.import_doc(args.name, args.folder, nodes)
    print("已导入文档：%s" % doc_id)
    print("  https://mubu.com/app/edit/home/%s" % doc_id)


def _tags(client, args):
    """列出账号用过的标签；或用 --search 查标签搜索建议。"""
    if args.search:
        items = client.search_tags(args.search, at=args.at)
        if args.json:
            print(json.dumps(items, indent=2, ensure_ascii=False))
            return
        if not items:
            print("没有匹配的标签")
            return
        for it in items:
            print("  #%s   使用 %s 次" % (it.get("tag"), it.get("count")))
        return

    data = client.list_tags()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    hash_tags = data.get("hashTags") or []
    at_tags = data.get("atTags") or []
    print("== # 标签（%d 个）==" % len(hash_tags))
    for it in hash_tags:
        print("  #%-24s 使用 %s 次" % (it.get("tag"), it.get("count")))
    if at_tags:
        print("== @ 提及（%d 个）==" % len(at_tags))
        for it in at_tags:
            print("  @%-24s 使用 %s 次" % (it.get("tag"), it.get("count")))


COMMANDS = {
    "login": _login,
    "list": _list,
    "mkdir": _mkdir,
    "create": _create,
    "get": _get,
    "save": _save,
    "append": _append,
    "set-headings": _set_headings,
    "mask": _mask,
    "highlight": _highlight,
    "formula": _formula,
    "mention": _mention,
    "node-mention": _node_mention,
    "summary-create": _summary_create,
    "summary-delete": _summary_delete,
    "emoji": _emoji,
    "task": _task,
    "collapse": _collapse,
    "move-node": _move_node,
    "image": _image,
    "link-nodes": _link_nodes,
    "delete": _delete,
    "restore": _restore,
    "purge": _purge,
    "trash": _trash,
    "move": _move,
    "search": _search,
    "export-tree": _export_tree,
    "rename": _rename,
    "opml": _opml,
    "del-node": _del_node,
    "table": _table,
    "export-table": _export_table,
    "insert-child": _insert_child,
    "link": _link,
    "view": _view,
    "share": _share,
    "refs": _refs,
    "templates": _templates,
    "template-use": _template_use,
    "import-doc": _import_doc,
    "tags": _tags,
}


def dispatch(client, args):
    """按解析后的系统命令执行对应处理器。"""
    handler = COMMANDS.get(args.command)
    if handler is None:
        raise ValueError(f"不支持的幕布命令: {args.command}")
    handler(client, args)
