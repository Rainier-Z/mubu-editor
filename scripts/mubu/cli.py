"""mubu 包 — 命令行入口（argparse 子命令 + 日志配置）。"""

import argparse
import logging
import sys

from mubu.client import MubuClient
from mubu.commands import dispatch
from mubu.config import (
    MAX_SEARCH_DEPTH,
    MAX_SEARCH_LIMIT,
    logger,
)


def _configure_logging(verbose: bool) -> None:
    """配置 mubu_api 日志（P1 #16）。

    每次运行前清理旧 handler，避免跨进程 / 跨测试绑定到失效的 stderr
    （capsys 场景：每轮测试替换 sys.stderr，handler 必须重绑当前对象）。
    - 默认 WARNING：仅输出 warning / error
    - --verbose：DEBUG，输出请求级调试信息
    """
    for h in list(logger.handlers):
        logger.removeHandler(h)
    # 绑定当前 sys.stderr（capsys 生效时为捕获对象，确保测试可断言）
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="幕布 API 命令行工具")
    # P1 #16：--verbose 控制 debug 日志（默认仅 warning/error）
    parser.add_argument("--verbose", action="store_true",
                        help="输出调试日志（DEBUG 级别，含请求级信息）")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # 登录（凭据取自环境变量 / config/.env.mubu；缺失时交互式输入，
    # 不再提供 --phone/--password 明文参数，避免出现在 ps / shell 历史中）
    subparsers.add_parser(
        "login", help="登录幕布（凭据取自环境变量 / .env.mubu，缺失时交互式输入）"
    )

    # 列表
    list_parser = subparsers.add_parser("list", help="获取文档列表")
    list_parser.add_argument("--folder", default="0", help="文件夹ID")
    list_parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    list_parser.add_argument("--include-trash", action="store_true",
                             help="包含已软删除（本地回收站）中的项")

    # 创建文件夹
    folder_parser = subparsers.add_parser("mkdir", help="创建文件夹")
    folder_parser.add_argument("name", help="文件夹名称")
    folder_parser.add_argument("--parent", default="0", help="父文件夹ID")

    # 创建文档
    doc_parser = subparsers.add_parser("create", help="创建文档")
    doc_parser.add_argument("name", help="文档名称")
    doc_parser.add_argument("--folder", default="0", help="文件夹ID")
    doc_parser.add_argument("--content", default="", help="文档内容（大纲 JSON 字符串）")
    doc_parser.add_argument("--md", help="从 Markdown 文件导入内容")

    # 获取文档
    get_parser = subparsers.add_parser("get", help="获取文档内容")
    get_parser.add_argument("doc_id", help="文档ID")
    get_parser.add_argument("--export", choices=["markdown", "json"], default="json", help="导出格式")

    # 保存文档
    save_parser = subparsers.add_parser("save", help="保存文档")
    save_parser.add_argument("doc_id", help="文档ID")
    save_parser.add_argument("--file", help="从文件读取内容（原始大纲 JSON）")
    save_parser.add_argument("--md", help="从 Markdown 文件导入内容")
    save_parser.add_argument("--content", help="直接指定内容")

    # 顶层追加 / 设置用户标题样式（真实 create+update changeset）
    append_parser = subparsers.add_parser("append", help="在文档顶层追加指定级别的标题样式")
    append_parser.add_argument("doc_id", help="文档ID")
    append_parser.add_argument("texts", nargs="+", help="要追加的标题文本（可多个）")
    append_parser.add_argument("--level", type=int, choices=range(1, 5), default=1,
                               help="用户标题级别 1-4（默认 1）")

    headings_parser = subparsers.add_parser(
        "set-headings", help="把文档顶层节点统一设为指定级别的标题样式")
    headings_parser.add_argument("doc_id", help="文档ID")
    headings_parser.add_argument("texts", nargs="+", help="目标标题（按顺序）")
    headings_parser.add_argument("--level", type=int, choices=range(1, 5), default=1,
                                 help="用户标题级别 1-4（默认 1）")

    # 文本样式（幕布原生 HTML：挖空 / 高亮）
    mask_parser = subparsers.add_parser("mask", help="将指定节点文本改写为挖空样式")
    mask_parser.add_argument("doc_id", help="文档ID")
    mask_parser.add_argument("--path", required=True,
                             help="节点路径（如 0.1；也支持 nodes,0,children,1）")
    mask_parser.add_argument("--text", required=True, help="要写入的文本")

    highlight_parser = subparsers.add_parser(
        "highlight", help="将指定节点文本改写为高亮样式")
    highlight_parser.add_argument("doc_id", help="文档ID")
    highlight_parser.add_argument("--path", required=True,
                                  help="节点路径（如 0.1；也支持 nodes,0,children,1）")
    highlight_parser.add_argument("--text", required=True, help="要写入的文本")
    highlight_parser.add_argument(
        "--color", default="yellow",
        choices=["yellow"],
        help="高亮颜色（默认 yellow）")

    # 高级原生节点能力（均改写已有节点，协议细节由 client 层负责）
    formula_parser = subparsers.add_parser("formula", help="将指定节点文本改写为幕布原生公式")
    formula_parser.add_argument("doc_id", help="文档ID")
    formula_parser.add_argument("--path", required=True, help="节点路径")
    formula_parser.add_argument("--latex", required=True, help="LaTeX 公式源码")
    formula_parser.add_argument("--block", action="store_true", help="写入非行内公式")

    mention_parser = subparsers.add_parser("mention", help="将指定节点改写为文档内链")
    mention_parser.add_argument("doc_id", help="文档ID")
    mention_parser.add_argument("--path", required=True, help="节点路径")
    mention_parser.add_argument("--target-doc", required=True, help="被引用文档ID")
    mention_parser.add_argument("--name", required=True, help="内链显示名称")

    node_mention_parser = subparsers.add_parser("node-mention", help="将指定节点改写为节点引用")
    node_mention_parser.add_argument("doc_id", help="文档ID")
    node_mention_parser.add_argument("--path", required=True, help="节点路径")
    node_mention_parser.add_argument("--target-doc", required=True, help="被引用文档ID")
    node_mention_parser.add_argument("--target-node", required=True, help="被引用节点ID")
    node_mention_parser.add_argument("--text", required=True, help="引用显示文本")

    summary_create_parser = subparsers.add_parser("summary-create", help="为多个节点创建概要")
    summary_create_parser.add_argument("doc_id", help="文档ID")
    summary_create_parser.add_argument("--member", dest="members", action="append", required=True,
                                       help="概要成员节点路径；可重复传入")
    summary_create_parser.add_argument("--text", default="", help="概要文本（默认空）")

    summary_delete_parser = subparsers.add_parser("summary-delete", help="从多个节点删除概要")
    summary_delete_parser.add_argument("doc_id", help="文档ID")
    summary_delete_parser.add_argument("summary_id", help="概要ID")
    summary_delete_parser.add_argument("--member", dest="members", action="append", required=True,
                                       help="概要成员节点路径；可重复传入")

    emoji_parser = subparsers.add_parser("emoji", help="设置指定节点的 Emoji")
    emoji_parser.add_argument("doc_id", help="文档ID")
    emoji_parser.add_argument("--path", required=True, help="节点路径")
    emoji_parser.add_argument("--value", required=True, help="Emoji 字符；传空字符串可由 API 清除")

    task_parser = subparsers.add_parser("task", help="设置指定节点的待办状态")
    task_parser.add_argument("doc_id", help="文档ID")
    task_parser.add_argument("--path", required=True, help="节点路径")
    task_parser.add_argument("--status", required=True, type=int, choices=[0, 1, 2],
                             help="待办状态：0 普通 / 1 未完成 / 2 已完成")

    collapse_parser = subparsers.add_parser("collapse", help="折叠或展开指定节点")
    collapse_parser.add_argument("doc_id", help="文档ID")
    collapse_parser.add_argument("--path", required=True, help="节点路径")
    collapse_parser.add_argument("--expand", action="store_true", help="展开节点（默认折叠）")

    move_node_parser = subparsers.add_parser("move-node", help="移动文档内节点并调整顺序")
    move_node_parser.add_argument("doc_id", help="文档ID")
    move_node_parser.add_argument("--path", required=True, help="源节点路径")
    move_node_parser.add_argument("index", type=int, help="目标兄弟序号")
    move_node_parser.add_argument("--parent", help="目标父节点路径；缺省表示文档顶层")

    image_parser = subparsers.add_parser("image", help="从本地文件附加图片到指定节点")
    image_parser.add_argument("doc_id", help="文档ID")
    image_parser.add_argument("--path", required=True, help="节点路径")
    image_parser.add_argument("--file", required=True, help="本地 PNG/JPEG 文件路径")
    image_parser.add_argument("--width", type=int, help="显示宽度（默认 400）")

    link_nodes_parser = subparsers.add_parser("link-nodes", help="在两个节点之间创建连接线")
    link_nodes_parser.add_argument("doc_id", help="文档ID")
    link_nodes_parser.add_argument("--from-path", required=True, help="起点节点路径")
    link_nodes_parser.add_argument("--to-path", required=True, help="终点节点路径")
    link_nodes_parser.add_argument("--side", default="right", choices=["left", "right", "top", "bottom"],
                                   help="连接方位（默认 right）")

    # 删除
    delete_parser = subparsers.add_parser("delete", help="删除文档或文件夹")
    delete_parser.add_argument("id", help="文档或文件夹ID")
    delete_parser.add_argument("--type", choices=["doc", "folder"], default="folder",
                               help="对象类型：doc=文档 / folder=文件夹（默认 folder）")
    delete_parser.add_argument("--yes", action="store_true",
                               help="确认移入本地回收站（云端仍在；可用 restore 恢复）")

    # 软删除恢复：仅移除本地标记，不调用服务端；零风险。
    restore_parser = subparsers.add_parser(
        "restore", help="从本地回收站恢复（仅移除标记，不调用服务端）"
    )
    restore_parser.add_argument("id", help="回收站项ID")

    # 彻底删除：唯一不可逆操作，调用服务端 + 移除本地标记。
    purge_parser = subparsers.add_parser(
        "purge", help="彻底删除（不可逆，调用服务端 + 移除本地标记）"
    )
    purge_parser.add_argument("id", help="文档或文件夹ID")
    purge_parser.add_argument("--type", choices=["doc", "folder"], default=None,
                              help="对象类型（仅当回收站记录缺失时必填）："
                                   "doc=文档 / folder=文件夹")
    purge_parser.add_argument("--yes", action="store_true",
                              help="确认执行不可逆彻底删除（必须显式传参）")

    # 列出本地回收站
    subparsers.add_parser(
        "trash", help="列出本地回收站中已软删除的项"
    )

    # 移动
    move_parser = subparsers.add_parser("move", help="移动文档/文件夹到其他文件夹")
    move_parser.add_argument("item_id", help="文档/文件夹 ID")
    move_parser.add_argument("--target", required=True, help="目标文件夹ID")
    move_parser.add_argument("--type", default="doc", choices=["doc", "folder"],
                             help="移动项类型（默认 doc）")

    # 搜索（支持 --max-depth / --limit）
    search_parser = subparsers.add_parser("search", help="本地搜索文档/文件夹（按名称）")
    search_parser.add_argument("keyword", help="搜索关键字（大小写不敏感）")
    search_parser.add_argument("--max-depth", type=int, default=MAX_SEARCH_DEPTH,
                               help="递归深度上限（根 depth=0，默认 3 即最多展开 4 层）")
    search_parser.add_argument("--limit", type=int, default=MAX_SEARCH_LIMIT,
                               help="返回结果上限（默认 50）")
    search_parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    search_parser.add_argument("--include-trash", action="store_true",
                               help="包含已软删除（本地回收站）中的项")

    # 整树导出：递归导出整个文件夹树为嵌套 Markdown
    export_tree_parser = subparsers.add_parser(
        "export-tree", help="递归导出整个文件夹树为嵌套 Markdown 文件"
    )
    export_tree_parser.add_argument("--folder", default="0", help="根文件夹ID（默认根 0）")
    export_tree_parser.add_argument("--output", default=".", help="输出目录（默认当前目录）")
    export_tree_parser.add_argument("--max-depth", type=int, default=MAX_SEARCH_DEPTH,
                                    help="递归深度上限（默认 3）")

    # 重命名：文档走 save_doc name；文件夹走已验证端点 /list/rename_folder
    rename_parser = subparsers.add_parser("rename", help="重命名文档或文件夹")
    rename_parser.add_argument("id", help="文档或文件夹ID")
    rename_parser.add_argument("--name", required=True, help="新名称")
    rename_parser.add_argument("--type", choices=["doc", "folder"], default="doc",
                               help="对象类型：doc=走已验证端点 /list/rename_doc（内容保真）；"
                                    "folder=走已验证端点 /list/rename_folder（folderId 填自身 id）")

    # OPML / FreeMind 导出：兼容其它大纲工具
    opml_parser = subparsers.add_parser("opml", help="将文档导出为 OPML / FreeMind XML")
    opml_parser.add_argument("doc_id", help="文档ID")
    opml_parser.add_argument("--format", choices=["opml", "freeplane"], default="opml",
                             help="导出格式（opml / freeplane）")

    # 删除文档内节点（系统命令：幕布原生 delete changeset，级联删除子树）
    delnode_parser = subparsers.add_parser(
        "del-node", help="删除文档内的顶层节点（级联删除其整棵子树，不可逆）")
    delnode_parser.add_argument("doc_id", help="文档ID")
    delnode_parser.add_argument("--index", type=int, action="append", required=True,
                                help="顶层节点序号，0 起；可重复传 --index 删除多个")
    delnode_parser.add_argument("--yes", action="store_true",
                                help="确认执行删除（必须显式传参）")

    # 原生表格（系统命令：幕布表格 = 节点 text 里的 HTML 表格）
    table_parser = subparsers.add_parser(
        "table", help="把 Markdown 表格 / 二维 JSON 生成为幕布原生表格")
    table_parser.add_argument("doc_id", help="文档ID")
    table_parser.add_argument("--md", help="Markdown 表格文件路径（相对当前目录）")
    table_parser.add_argument("--json", help="二维数组 JSON 文件路径（相对当前目录）")
    table_location = table_parser.add_mutually_exclusive_group()
    table_location.add_argument("--replace", type=int, default=None,
                                 help="替换该顶层序号上的节点（缺省 = 追加到文档末尾）")
    table_location.add_argument("--parent-path",
                                help="父节点路径（如 0.1；缺省 = 追加到文档顶层）")
    table_parser.add_argument("--no-header", action="store_true",
                              help="不把首行渲染为表头（全部当数据行）")

    export_table_parser = subparsers.add_parser(
        "export-table", help="把文档里的原生表格导出为 Markdown / CSV")
    export_table_parser.add_argument("doc_id", help="文档ID")
    table_source = export_table_parser.add_mutually_exclusive_group(required=True)
    table_source.add_argument("--index", type=int,
                              help="表格所在的顶层序号（0 起）")
    table_source.add_argument("--path",
                              help="表格节点路径（如 0.1；也支持 nodes,0,children,1）")
    export_table_parser.add_argument("--format", choices=["md", "csv"], default="md",
                                     help="导出格式（默认 md）")

    # 在已有节点下插入子节点（系统命令）
    insert_child_parser = subparsers.add_parser(
        "insert-child", help="在已有节点下插入子节点（可多个）")
    insert_child_parser.add_argument("doc_id", help="文档ID")
    insert_child_parser.add_argument("--parent", required=True,
                                     help="父节点：顶层序号（如 5）或完整路径（如 nodes,0,children,1）")
    insert_child_parser.add_argument("texts", nargs="+", help="要插入的子节点文本（可多个）")

    # 超链接（系统命令：幕布原生能力，节点 text 里的 <a> HTML）
    link_parser = subparsers.add_parser(
        "link", help="把文字变成幕布原生超链接（改写已有节点，或新建链接节点）")
    link_parser.add_argument("doc_id", help="文档ID")
    link_parser.add_argument("--url", required=True, help="链接地址")
    link_parser.add_argument("--text", help="显示文字（缺省显示 URL 本身）")
    link_parser.add_argument("--path", help="改写该已有节点：顶层序号（如 5）或完整路径（如 nodes,0,children,1）")
    link_parser.add_argument("--parent", help="在该父节点下新建链接节点（写法同 --path）")
    link_parser.add_argument("--new-tab", action="store_true", help="加 target=_blank（新标签打开）")

    # 文档视图切换（真实 settingChanged changeset）
    view_parser = subparsers.add_parser(
        "view", help="切换文档视图：大纲 / 思维导图 / 演示")
    view_parser.add_argument("doc_id", help="文档ID")
    view_parser.add_argument("view", choices=["outline", "mindmap", "presentation"],
                             help="目标视图（outline=大纲 / mindmap=思维导图 / presentation=演示）")

    # 分享链接（真实端点）
    share_parser = subparsers.add_parser(
        "share", help="开启 / 刷新 / 关闭文档分享链接")
    share_parser.add_argument("doc_id", help="文档ID")
    share_parser.add_argument("--refresh", action="store_true",
                              help="刷新链接（旧链接失效）")
    share_parser.add_argument("--close", action="store_true", help="关闭分享")

    # 双向链接：谁引用了我（真实端点）
    refs_parser = subparsers.add_parser(
        "refs", help="列出「谁引用了我」——引用本文档的其他文档")
    refs_parser.add_argument("doc_id", help="文档ID")
    refs_parser.add_argument("--json", action="store_true", help="JSON 格式输出")

    # 模板（真机确认）
    tp_parser = subparsers.add_parser("templates", help="列出模板（推荐 / 我的 / 最近）")
    tp_parser.add_argument("--json", action="store_true", help="JSON 格式输出")

    tpu_parser = subparsers.add_parser("template-use", help="用模板创建一篇新文档")
    tpu_parser.add_argument("uuid", help="模板 uuid（用 templates 命令查看）")
    tpu_parser.add_argument("--name", help="新文档名（缺省用模板名）")
    tpu_parser.add_argument("--folder", default="0", help="目标文件夹ID（默认根）")

    # 官方导入通道（一次请求带内容建文档，真机确认）
    imp_parser = subparsers.add_parser(
        "import-doc", help="用官方导入通道创建带内容的文档（一次请求）")
    imp_parser.add_argument("name", help="文档标题")
    imp_parser.add_argument("--folder", default="0", help="目标文件夹ID（默认根）")
    imp_parser.add_argument("--md", help="从 Markdown 文件导入")
    imp_parser.add_argument("--json", help="从 definition JSON 文件导入（{\"nodes\":[...]}）")

    # 标签（真机确认；⚠️ 全部是 GET）
    tags_parser = subparsers.add_parser("tags", help="列出账号用过的标签 / 标签搜索建议")
    tags_parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    tags_parser.add_argument("--search", help="按关键词查搜索建议（不含 # / @）")
    tags_parser.add_argument("--at", action="store_true", help="配合 --search 查 @ 提及")

    args = parser.parse_args()
    _configure_logging(args.verbose)

    if not args.command:
        parser.print_help()
        return

    try:
        client = MubuClient()

        dispatch(client, args)

    except Exception as e:
        # 仅记录 msg（已脱敏：不含密码/token/原始 body），不泄露敏感信息
        logger.error("%s", e)
        sys.exit(1)
