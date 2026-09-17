---
name: mubu-editor
description: mubu-editor —— 用命令行与 AI Agent 直接编辑幕布（mubu）文档：读写大纲、改写节点文本与样式（标题／挖空／高亮／公式）、增删与移动节点、插入文档内链与节点引用、创建概要／表格／图片／连接线、切换视图、管理分享链接与模板。触发词：幕布、mubu、幕布文档、幕布笔记、幕布大纲、编辑幕布、同步到幕布、导出幕布
---

# mubu-editor —— 幕布编辑器

幕布（mubu.com）是一款大纲工具，支持一键转为思维导图。mubu-editor 把幕布文档变成**可被命令行与 AI Agent 直接编辑的对象**——不只读写整篇文档，还能改到**节点级**：文本、标题样式、公式、内链、概要、表格、图片与连接线。

## 权限与安全边界
本 Skill 以你的幕布账号身份操作**远程真实内容**，使用前请知悉其权限边界：
- **读取**：仅读取环境变量 `MUBU_PHONE` / `MUBU_PASSWORD` / `MUBU_MEMBER_ID`（环境变量未设置时，才由仓库外的 `config/.env.mubu` 补全；`MUBU_MEMBER_ID` 即幕布 colla 成员 ID，仅 `save` 写回需要。**该值任何 API 都不返回，无法自动获取，必须手动设置 `MUBU_MEMBER_ID` 或提前写入 token 缓存的 `member_id`**）。
- **写入**：远端写操作会修改你账号中的文档、文件夹或回收站状态；本地写入包括 Token 缓存及 `export-tree` 指定目录中的 Markdown 文件。Token 缓存使用权限 `0o600` 与跨进程文件锁。
- **网络**：默认访问 `https://api2.mubu.com`。`MUBU_BASE_URL` 只接受 HTTPS、无用户名密码/查询/片段、默认端口且主机为 `api2.mubu.com`、`api.mubu.com` 或 `mubu.com` 的地址；不合规时忽略并使用默认值。
- **写操作需确认**：执行 `create`、`mkdir`、`save`、`rename`、`move`、节点编辑、`purge` 等会改变远端内容的动作前，先说明目标和影响并取得用户确认。`delete` 只是**本地软删除**（云端副本仍在）；`purge` 才会调用服务端删除且不可逆。仅个别 CLI 命令要求 `--yes`，不可把它误解为所有写命令的通用确认机制。
- **信任边界**：Skill 不读取你的其它本地文件、不执行与幕布无关的 shell 命令；它只做「登录 → 读写你的幕布文档」这一件事。

## 功能概览

mubu-editor 把幕布当作一个**可编程的编辑器**：既能管理文档与文件夹，也能直接改写文档**内部**的节点——文本、样式、结构、图片、表格与链接。

> 除下表单独列出端点的少量操作外，**绝大多数编辑共用同一个 changeset 端点** `POST /colla/events`：`events` 里承载 `create` / `update` / `delete` / `structureChanged` / `settingChanged` 事件，每次写入后立即读回核验。协议细节见 `docs/protocol-notes.md`。

### 账号与连接

| 能力 | 命令 | 接口 |
|---|---|---|
| 手机号密码登录 | `login` | `POST /user/phone_login` |
| Token 自动刷新 | — | access_token 约 2 小时过期，临近自动重登（仅重试 1 次，杜绝死循环） |
| v3 / v4 双基址 | — | 默认 `https://api2.mubu.com/v3/api`；`/v4/api` 路径自动改走 v4 基址 |

### 文档与文件夹管理

| 能力 | 命令 | 接口 |
|---|---|---|
| 列出文档 / 文件夹 | `list` | `POST /list/get` |
| 新建文件夹 | `mkdir` | `POST /list/create_folder` |
| 新建文档 | `create` | `POST /list/create_doc` |
| 读取文档内容 | `get` | `POST /document/edit/get` |
| 保存文档 | `save` | `POST /colla/events`（changeset） |
| 重命名文档 / 文件夹 | `rename` | `POST /list/rename_doc` / `POST /list/rename_folder` |
| 移动文档 / 文件夹 | `move` | `POST /list/custom/drag`（须满足真机发布门槛） |
| 删除（本地软删除，云端副本仍在） | `delete` | `POST /list/delete_doc` / `POST /list/delete_folder` |
| 回收站：列出 / 恢复 / 彻底删除 | `trash` / `restore` / `purge` | `purge` 调服务端且**不可逆** |
| 本地搜索（可选搜正文） | `search` | 递归遍历，大小写不敏感 |
| 整树导出 | `export-tree` | 递归导出为嵌套 Markdown 文件 |

### 节点级编辑（编辑器的核心）

| 能力 | 命令 | 说明 |
|---|---|---|
| 追加顶层节点 | `append` | 按指定级别追加标题样式节点 |
| 插入子节点 | `insert-child` | 在**已有节点**下插入，支持完整路径 |
| 移动 / 重排节点 | `move-node` | 含跨父级移动；每步写后读回、按最新树重算路径 |
| 删除节点 | `del-node` | 级联删除整棵子树，**不可逆** |
| 折叠 / 展开 | `collapse` | 更新 `collapsed` |
| 待办状态 | `task` | `taskStatus` 0（普通）/ 1（未完成）/ 2（已完成） |
| Emoji | `emoji` | 传空字符串即可清除 |
| 节点连接线 | `link-nodes` | 写入起点节点的 `linkLines` |

### 文本与样式

| 能力 | 命令 |
|---|---|
| 标题级别：统一设置 / 追加 | `set-headings` / `append` |
| 文字挖空 | `mask` |
| 高亮 | `highlight` |
| 幕布原生公式 | `formula` |
| 加粗 / 颜色 / 斜体 / 下划线 | Python API 的样式方法（`style_text` 等） |

### 结构化元素

| 能力 | 命令 | 说明 |
|---|---|---|
| 原生表格：生成 / 导出 | `table` / `export-table` | 导出为 Markdown / CSV |
| 图片：附加本地文件 | `image` | 走 TOS 直传并注册到用户图片库（否则桌面端破图） |
| 概要（多节点折叠） | `summary-create` / `summary-delete` | 写入各成员节点的 `summaryData` |
| 文档内链 | `mention` | |
| 节点引用 | `node-mention` | |
| 原生超链接 | `link` | 改写已有节点，或新建链接节点 |

### 资产与协作

| 能力 | 命令 | 接口 |
|---|---|---|
| 双向链接「谁引用了我」 | `refs` | `POST /refer/doc/list` |
| 分享链接：开启 / 刷新 / 关闭 | `share` | `POST /document/create_link` / `refresh_link` / `close_link` |
| 模板：列出 / 用模板建文档 | `templates` / `template-use` | `POST /template/get_list` / `POST /template/view` |
| 官方导入通道（一次请求建带内容的文档） | `import-doc` | `POST /list/import_doc` |
| 标签：全部 / 搜索建议 | `tags` | `GET /v4/api/document/tag/list` 等 |
| 视图切换：大纲 / 思维导图 / 演示 | `view` | `POST /colla/events`（`settingChanged`） |

### 导出

| 能力 | 命令 |
|---|---|
| Markdown 语义往返（导入 → 编辑 → 导出） | `create --md` / `save --md` / `get --export markdown` |
| OPML / FreeMind | `opml` |
| 整树导出 | `export-tree` |
| 表格导出（Markdown / CSV） | `export-table` |

## API 基础信息

- **Base URL**: `https://api2.mubu.com/v3/api`
- **认证方式**: JWT Token，通过请求头 `Jwt-Token` 传递
- **Content-Type**: `application/json;charset=UTF-8`

## 环境变量配置

在使用前，需要配置以下环境变量：

```bash
export MUBU_PHONE="your_phone_number"    # 幕布账号手机号
export MUBU_PASSWORD="your_password"      # 幕布账号密码
# 可选：幕布 colla 成员 ID（仅 save 写回需要；任何 API 都不暴露，必须手动设置，缺失时 save 会明确报错）
export MUBU_MEMBER_ID="your_collab_member_id"
```

> 切勿在脚本或代码中硬编码明文密码；凭据仅通过环境变量或仓库外的
> `config/.env.mubu` 提供。

---

## 使用说明

### 1. 使用 MubuClient

所有操作都通过 `scripts/mubu/client.py` 中的 `MubuClient` 类完成（`scripts/mubu_api.py` 仅为向后兼容的重新导出 shim，不再建议直接使用；**不再有**独立的
`login()` / `create_folder()` / `create_doc()` / `get_list()` / `get_doc()` / `save_doc()` /
`delete_item()` 模块级函数）。实例化时自动读取 `MUBU_PHONE` / `MUBU_PASSWORD`
环境变量（或 `config/.env.mubu`）并加载本地缓存 Token：

```python
from mubu.client import MubuClient

# 登录：凭据来自环境变量；返回扁平 data（token / id / name）
client = MubuClient()
info = client.login()
print(info["user_id"], info["username"])   # 注意是扁平 data["id"]，非 data["user"]["id"]

# 按名称本地搜索文档/文件夹（递归遍历，大小写不敏感）
results = client.search("项目", max_depth=3, limit=50)["results"]
for r in results:
    print(r["type"], r["name"], r["path"])
```

> 登录返回结构为**扁平** `data`：`data["id"]`=用户 ID，`data["name"]`=用户名，
> `data["token"]`=令牌。这与旧版嵌套 `result["data"]["user"]["id"]` 不同。

---

## 大纲内容格式

幕布文档内容使用特定的 JSON 格式表示大纲结构：

```json
{
  "name": "文档标题",
  "nodes": [
    {
      "id": "node_1",
      "text": "一级标题",
      "taskStatus": 0,
      "children": [
        {
          "id": "node_1_1",
          "text": "二级标题",
          "children": []
        }
      ],
      "collapsed": false,
      "finish": false,
      "modified": 0
    },
    {
      "id": "node_2",
      "text": "另一个一级标题",
      "taskStatus": 0,
      "children": [],
      "collapsed": false,
      "finish": false,
      "modified": 0
    }
  ]
}
```

---

## Token 管理建议

由于幕布的 access_token 仅约 2 小时有效（无 refresh_token 机制，代码通过缓存凭据重新登录获取新 Token），建议：

1. **本地缓存**: 将 Token 保存到本地文件（如 `config/.mubu_token`）
2. **自动刷新**: 在 Token 快过期时自动刷新
3. **错误重试**: 遇到 401 错误时重新登录

```python
import os
import time
import json
import tempfile

TOKEN_FILE = os.path.expanduser("config/.mubu_token")

def save_token(token_data):
    """原子写 + 仅属主可读写：避免中途崩溃留下残缺文件，并防止其它用户读取。"""
    token_data = dict(token_data)
    token_data["expires_at"] = time.time() + 7200  # 2小时后过期
    # 注：真实 scripts/mubu_api.py 的 _save_token 还会用跨进程 fcntl.flock
    # advisory 锁包裹整段写（已做成跨平台安全：无 fcntl 平台降级为无锁）；
    # 此处省略锁，聚焦写盘逻辑。
    dir_name = os.path.dirname(TOKEN_FILE) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_name, prefix=".mubu_token.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(token_data, f)
        os.chmod(tmp, 0o600)        # 仅属主可读写
        os.replace(tmp, TOKEN_FILE) # 原子重命名，避免残缺文件
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

def load_token():
    """从本地加载未过期的 Token；已过期或损坏则返回 None。"""
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
    except Exception:
        return None
    if time.time() >= data.get("expires_at", 0):  # 已过期视为无效
        return None
    return data
```

说明：原示例中朴素的 `is_token_valid` 已移除——其职责（"是否过期"）已并入 `load_token`，仅返回未过期的 token。真实实现 `scripts/mubu_api.py` 的 `_save_token` 还包含跨进程 `fcntl.flock` 锁与统一的 `TOKEN_FILE_MODE` 权限管理，此处不再重复。

---

## 导出 / 导入 Markdown

Markdown 导入/导出按受支持的大纲语义往返；并非幕布全部元数据的字节级备份。核心纯函数位于 `scripts/mubu/convert.py`：

```python
def doc_to_markdown(node, level=0):
    """将节点（及子树）渲染为 Markdown 列表片段。
    '- ' 列表项，缩进 = 2 * level；`taskStatus` 为 1/2 时渲染为 '- [ ]'/'- [x]'；`finish` 单独存在不渲染 checkbox；
    含 note → 子树后追加引用块；根 note 不缩进，列表节点 note 比列表项多两个空格。根标题由 export_markdown 负责。"""
    ...

def export_markdown(doc):
    """doc 通常为 get_doc() 返回的 {"name": ..., "nodes": [...]}。
    每个顶层节点输出一个 '# 标题'；其子树递归为 '- ' 列表。结构无效时抛 MubuError。"""
    ...

def markdown_to_doc(md):
    """Markdown 文本 → {"nodes": [...]}（单根文档另带 node 兼容别名）。
    每个一级标题独立成为顶层节点，后续列表项属于该标题；列表项用栈按缩进
    深度维护层级；'- [ ]'/'- [x]' 和标题 checkbox 映射为 `taskStatus` 1/2；`finish` 单独存在不渲染 checkbox；note 引用按
    缩进归属节点，根 note 不缩进，列表节点 note 比列表项多两个空格。"""
    ...
```

导出示例（幕布 → Markdown）：

```
# 读书笔记
- 第一章
  - [x] 读完
  - [ ] 写笔记
> 第一章的备注
```

> 说明：根节点的 `text` 渲染为 `# 标题`，其直接子节点从缩进 0 的 `- ` 列表开始；
> note 出现在其所属节点（含子树）之后，并按缩进深度归属到对应节点。

往返规则：每个 `# 标题` 是独立顶层节点，其后的列表属于该标题；标题和列表项均支持 `[x]` / `[ ]` checkbox。根 note 用不缩进的 `> `，列表节点 note 比该节点的列表项多缩进两个空格；连续引用行组成同一多行 note。若节点文本本身以 `[x] ` / `[ ] ` 开头，导出会加一个转义反斜线以区别普通文本和 checkbox，重新导入会去掉这一层转义。只支持一级标题、无序列表及有效的两空格缩进；遇到不支持或有歧义的结构会报错，不会静默丢弃内容。

---

## 命令参考（CLI）

| 命令 | 说明 |
|------|------|
| `login` | 手机号密码登录，Token 本地缓存 |
| `list --folder <id> [--include-trash]` | 获取文件夹下的文档/子文件夹列表（`--json` 输出原始 JSON；`--include-trash` 包含已软删除项） |
| `mkdir <name> --parent <id>` | 创建文件夹 |
| `create <name> --folder <id> [--content <json>] [--md <file>]` | 创建文档；`--md` 从 Markdown 文件导入（输入先验证；正文部分失败报告 `doc_id` 与进度）|
| `get <doc_id> [--export markdown\|json]` | 获取文档；`--export markdown` 输出真实 Markdown |
| `save <doc_id> [--file <f>] [--md <file>] [--content <c>]` | 节点级同步正文并写后读回语义核验；`--md` 从 Markdown 文件导入 |
| `delete <id> [--type doc\|folder] --yes` | **软删除**：移入本地回收站（云端仍在，`restore` 可恢复，`purge` 可彻底删除）；`--type` 默认 folder，必须显式 `--yes` 才执行 |
| `restore <id>` | 从本地回收站恢复（仅移除标记，零服务端调用） |
| `purge <id> [--type doc\|folder] --yes` | **彻底删除（不可逆）**：调用服务端真实删除 API 后移除本地标记；必须显式 `--yes` 才执行 |
| `trash` | 列出本地回收站中已软删除的项 |
| `move <item_id> --target <folder_id> [--type doc\|folder]` | 移动文档/文件夹到其他位置（端点 `/list/custom/drag`；真机发布前须满足本手册的 E2E 门槛）|
| `search <关键字> [--max-depth N] [--limit N] [--include-trash]` | 按名称本地搜索文档/文件夹（递归遍历，大小写不敏感；`--include-trash` 包含已软删除项） |
| `export-tree --folder <id> [--output <dir>] [--max-depth N]` | 递归导出整个文件夹树为嵌套 Markdown 文件；不覆盖已有文件，清理文件名冲突时追加序号 |
| `rename <id> --name <新名> [--type doc\|folder]` | 重命名文档（`/list/rename_doc` 端点）或文件夹（端点 `/list/rename_folder`，`folderId` 填自身 id；真机发布前须满足本手册的 E2E 门槛）|
| `opml <doc_id> [--format opml\|freeplane]` | 导出为 OPML 2.0 / FreeMind XML（兼容 XMind 等其它大纲工具）|
| `mask <doc_id> --path <node-path> --text <text>` | 将节点文本写为原生挖空 HTML |
| `highlight <doc_id> --path <node-path> --text <text> [--color yellow]` | 将节点文本写为原生高亮 HTML；颜色目前仅支持 `yellow` |
| `formula <doc_id> --path <node-path> --latex <latex> [--block]` | 将节点文本写为原生公式（默认行内；`--block` 为非行内）|
| `mention <doc_id> --path <node-path> --target-doc <target-doc-id> --name <name>` | 将节点文本写为文档内链 |
| `node-mention <doc_id> --path <node-path> --target-doc <target-doc-id> --target-node <target-node-id> --text <text>` | 将节点文本写为节点引用 |
| `summary-create <doc_id> --member <node-path> [--member <node-path> ...] [--text <text>]` | 为多个成员节点创建概要 |
| `summary-delete <doc_id> <summary-id> --member <node-path> [--member <node-path> ...]` | 从多个成员节点删除概要 |
| `emoji <doc_id> --path <node-path> --value <emoji>` | 设置或清除节点 Emoji |
| `task <doc_id> --path <node-path> --status <0\|1\|2>` | 设置待办状态：0 普通、1 未完成、2 已完成 |
| `collapse <doc_id> --path <node-path> [--expand]` | 折叠节点；传 `--expand` 则展开 |
| `move-node <doc_id> --path <source-path> <index> [--parent <parent-path>]` | 移动文档内节点并调整顺序 |
| `image <doc_id> --path <node-path> --file <png-or-jpeg> [--width <pixels>]` | 从本地 PNG/JPEG 文件附加图片 |
| `link-nodes <doc_id> --from-path <from-path> --to-path <to-path> [--side left\|right\|top\|bottom]` | 在两个节点之间创建连接线 |

### 当前写入行为与真机发布门槛

- `create` 会在发起远端创建前验证输入，再先创建空文档并逐个写入顶层节点。正文部分失败时会报告 `doc_id`、已完成/总数和已确认节点 ID；先查询文档状态，再决定如何续作，避免盲目创建副本。`definition: {}` / 空 `nodes` 数组是合法空文档定义。
- `save` 使用带 `path` 的节点级 `create` / `update` / `delete` changeset；每次写入后重新读取并核验语义结果。嵌套节点的增删改使用包含 `children` 段的 `path`，例如 `["nodes", 0, "children", 1]`；空或无效 `events` 失败关闭；重复节点 ID 或无法唯一消解的身份匹配会中止。未被目标内容表达的现有 `highlight`、`color` 和未知字段会保留。
- `update.updated` 是部分字段补丁而非完整节点快照。已抓到的 `collapsed` 更新载荷为 `{id, collapsed}`，`text` 更新载荷为 `{id, text, modified, highlight, color}`；未抓包的字段组合不能从这两个例子推断。
- 真机观察表明读回可省略默认 `children=[]`、`priority=0`、`highlight=""`、`taskStatus=0`、`note=""`、`collapsed=false`、`finish=false`、`color=""`、`deadline=0`、`remindAt=0`；核验会将这些缺失视为默认值。非默认值仍须匹配，节点 `id` 与 `text` 也须匹配。
- Markdown checkbox 由 `taskStatus` 决定：`0` 为普通节点，`1` 为未完成待办，`2` 为已完成待办；`finish` 单独存在时不显示 checkbox。
- 同父重排和跨父级移动通过 `structureChanged` 已实现。已真机验证；每一步写入后重新读回并按最新树重算路径。身份有歧义、目标父节点位于待移动节点自身子树、或目标父节点是在同一同步中新建时会失败关闭。CLI `move` 指移动文档/文件夹，仍须满足其自身真机发布门槛。
- 写请求超时、连接中断或 HTTP 5xx 不自动重放，结果可能未知；先读取远端状态并确认后再决定是否重试。
- Markdown 往返规则：每个一级标题独立成为顶层节点，其后的列表属于该标题；标题和列表项支持 `[x]` / `[ ]`。根 note 用不缩进的 `> `，列表节点 note 比列表项多缩进两个空格，多行引用行属于同一个 note。节点文本若字面上以 checkbox 样式开头，导出会转义前缀并在重新导入时还原；不支持或跳级的结构会报错，不会静默压平。
- **真机发布门槛：** 新增或修改的写操作须在隔离文档完成“新建空文档 → 写入 → 读回语义核验”真机 E2E 后才能宣称真机可用。永久验收脚本为 `python scripts/validation/live_write_e2e.py --yes [--folder <folder-id>]`；它会创建唯一临时文档并最终 purge 该文档。运行前须取得用户明确授权；`--yes` 不能替代用户授权。

### 软删除 / 回收站

`delete` 现在不再是真正的服务端删除，而是**软删除**：

- `delete <id> [--type doc\|folder] --yes` —— **移入本地回收站**：仅把项的元数据标记进本地回收站文件 `config/.mubu_trash.json`，**云端副本保持不变**，不调用任何删除 API。缺省（无 `--yes`）仅打印提示并退出，绝不软删除。
- `restore <id>` —— 从本地回收站恢复：仅移除本地标记，**零服务端调用**（即使云端项已不存在也安全）。未找到该项时提示「未找到回收站项」。
- `purge <id> --yes` —— **彻底删除（不可逆）**：唯一真正调用服务端删除 API（`delete_doc` / `delete_folder`）的操作，成功后移除本地标记。必须显式 `--yes`，否则中止。
- `trash` —— 列出本地回收站中已软删除的项（id / type / name / deleted_at）。

`list --include-trash` 与 `search <关键字> --include-trash` 可在列表中**包含**已软删除项（`get_list` / `search` 默认过滤回收站项）。

> 回收站仅存元数据快照（id / type / name / parent_id / deleted_at），作为「云端仍在、可恢复」的安全网，**不作为重建来源**。

Markdown 往返示例：

```bash
# 导出为 Markdown
python3 scripts/mubu_api.py get <doc_id> --export markdown

# 从 Markdown 创建文档
python3 scripts/mubu_api.py create "我的文档" --folder <folder_id> --md ./outline.md

# 从 Markdown 更新文档
python3 scripts/mubu_api.py save <doc_id> --md ./outline.md

# 移动文档
python3 scripts/mubu_api.py move <doc_id> --target <folder_id>

# 按名称本地搜索文档/文件夹（递归遍历所有子文件夹，大小写不敏感）
python3 scripts/mubu_api.py search "项目"
python3 scripts/mubu_api.py search "项目" --json

# 递归导出整个文件夹树为嵌套 Markdown
python3 scripts/mubu_api.py export-tree --folder <root_folder_id> --output ./backup

# 重命名文档 / 文件夹
python3 scripts/mubu_api.py rename <doc_id> --name "新标题" --type doc
python3 scripts/mubu_api.py rename <folder_id> --name "新文件夹名" --type folder

# 导出为 OPML / FreeMind
python3 scripts/mubu_api.py opml <doc_id> --format opml
python3 scripts/mubu_api.py opml <doc_id> --format freeplane
```

---

## Token 刷新策略

- access_token 有效期约 2 小时，本地以 `expires_at` 缓存于 `config/.mubu_token`。
- 每次请求发起前调用 `ensure_valid_token()`：若未持有 token，或距过期不足
  `300 + 60`（leeway）秒，则使用缓存的 `phone`/`password` **重新登录**获取新 token。
- **刷新不依赖 refresh_token**（使用本地凭据重新登录）。
- **鉴权失败仅重试 1 次**：`_request` 捕获 401 / 登录失效类错误后重新登录并重试最多一次；
  第二次仍失败则抛出 `MubuError`，**不再重登**，避免密码错误/账号封禁场景下的死循环。
- 403（权限不足）或其它非 0 业务 code **不触发重登**。
- Token 写入采用原子写（先写 `.tmp` 再 `os.rename`），写完追加 `os.chmod(TOKEN_FILE, 0o600)`，
  确保 Token 文件仅属主可读写。

---

## 配置说明

脚本通过环境变量读取凭据（优先级：环境变量 > `config/.env.mubu` 文件；
两者皆无时，`login` 子命令会交互式提示输入，绝不接受明文命令行参数）：

```bash
export MUBU_PHONE="你的手机号"
export MUBU_PASSWORD="你的密码"
# 可选：幕布 colla 成员 ID（仅 save 写回需要；任何 API 都不暴露，必须手动设置，缺失时 save 会明确报错）
export MUBU_MEMBER_ID="你的幕布 colla 成员 ID"
```

也可在 `config/.env.mubu` 中配置（由 Skill 宿主加载为环境变量，且仅属主可读写）：

```
MUBU_PHONE=你的手机号
MUBU_PASSWORD=你的密码
# 可选：幕布 colla 成员 ID（仅 save 写回需要；任何 API 都不暴露，必须手动设置，缺失时 save 会明确报错）
MUBU_MEMBER_ID=你的幕布 colla 成员 ID
```

配置路径选择规则：显式设置的 `MUBU_CONFIG_DIR` 和各 `MUBU_TOKEN_FILE` / `MUBU_TRASH_FILE` / `MUBU_ENV_FILE` 优先。未设置显式路径时，若项目 `config/` 下存在任一已知配置文件，所有默认配置文件都从该目录读取；否则若发现旧布局配置文件，则整体采用旧来源（Token 在 `~/.mubu_token`，其余在 `~/.workbuddy/`），不按文件各自回退混用。环境变量中的凭据值优先于 `.env.mubu`。

---

## 已知限制

- `expand`（幕布大纲的折叠/展开状态）不在本期往返范围内，导入后节点默认展开。
- 有序列表 `1.` 不被解析，仅支持无序列表 `- `。
- 图片 / 附件类型节点不在本期 Markdown 往返范围内（会丢失媒体内容）。
- 每个 Markdown 一级标题导入后独立成为一个顶层节点，后续列表属于对应标题；标题之外的普通文本、跳级列表缩进、不受支持的结构会报错，不会静默压平。
- checkbox 支持标题和列表项；以 `[ ]` / `[x]` 开头但实际属于文本的内容会在导出时加转义斜线，导入时还原。根节点 note 使用不缩进的引用块；列表项 note 比对应列表项多缩进两个空格，多行 note 会按原归属还原。
- `save`（文档保存）需要幕布 colla 成员 ID：私人文档的 `memberId` **任何 API 都不暴露**，工具无法自动获取，必须由 `MUBU_MEMBER_ID` 环境变量提供（或提前写入 token 缓存的 `member_id`）。**缺失时 `save` 会明确报错提示配置，不影响 `get` / `create` / `move` / `rename` 等其它操作**。
- Markdown 不承载所有幕布元数据；`expand`、有序列表 `1.`、图片 / 附件仍不在支持范围内。通过完整 definition 保存时，未在目标中声明的现有 `highlight`、`color` 及未知字段会保留。读回可省略默认 `children=[]`、`priority=0`、`highlight=""`、`taskStatus=0`、`note=""`、`collapsed=false`、`finish=false`、`color=""`、`deadline=0`、`remindAt=0`；非默认值及 `id` / `text` 仍须匹配。

---

## 注意事项

1. **非官方 API**: 幕布未提供官方开放平台，此 Skill 基于幕布 Web 客户端自身调用的接口实现
2. **稳定性**: API 可能随版本更新而变化，如遇问题请反馈
3. **频率限制**: 请勿频繁调用，避免触发限流
4. **数据安全**: Token 存储在本地，请勿泄露

---

## 免责声明

- 本项目与幕布（Mubu）官方**无隶属、无合作关系**，属非官方集成。
- 仅用于操作**使用者本人账号**下的数据；凭据由使用者自行提供、自行保管。
- 使用前请确认符合幕布用户协议及当地法律法规；因使用本工具产生的**账号与数据风险由使用者自负**。
- 本工具**不提供**绕过付费、批量抓取、多账号轮换、非授权访问等能力，也不应用于上述用途。

---

## Agent 使用指引

当用户提到幕布、mubu 相关操作（如将幕布大纲导入 Obsidian、把 Markdown 同步到幕布、查询/导出幕布笔记）时，使用本 Skill 的脚本完成操作。

### 前置检查

1. 确认系统已安装 Python 3 和 requests 库：
   ```bash
   python3 -c "import requests; print('OK')"
   ```
   如果缺少 requests：`pip3 install requests`

2. 确认环境变量已配置：
   - `MUBU_PHONE` — 幕布手机号
   - `MUBU_PASSWORD` — 幕布密码
   - 如未配置，需提示用户先设置

### 脚本路径

```
scripts/mubu_api.py
```

### 常用命令速查

| 用户意图 | 执行命令 |
|---------|---------|
| 登录幕布 | `python3 scripts/mubu_api.py login` |
| 查看文档列表 | `python3 scripts/mubu_api.py list [--folder <folder_id>] [--include-trash]` |
| 查看某文件夹 | `python3 scripts/mubu_api.py list --folder <folder_id>` |
| 创建文件夹 | `python3 scripts/mubu_api.py mkdir "文件夹名"` |
| 创建文档 | `python3 scripts/mubu_api.py create "文档名" --folder <folder_id>` |
| 从 Markdown 创建文档 | `python3 scripts/mubu_api.py create "文档名" --folder <folder_id> --md outline.md` |
| 获取文档内容 | `python3 scripts/mubu_api.py get <doc_id>` |
| 导出为 Markdown | `python3 scripts/mubu_api.py get <doc_id> --export markdown` |
| 从 Markdown 保存文档 | `python3 scripts/mubu_api.py save <doc_id> --md outline.md` |
| 从文件保存文档 | `python3 scripts/mubu_api.py save <doc_id> --file content.json` |
| 移动文档 | `python3 scripts/mubu_api.py move <doc_id> --target <folder_id> [--type doc\|folder]` |
| 软删除（移入回收站） | `python3 scripts/mubu_api.py delete <id> --type doc\|folder --yes`（云端仍在，`restore` 可恢复）|
| 从回收站恢复 | `python3 scripts/mubu_api.py restore <id>` |
| 彻底删除（不可逆） | `python3 scripts/mubu_api.py purge <id> [--type doc\|folder] --yes`（必须显式 `--yes`，调用服务端真实删除）|
| 查看回收站 | `python3 scripts/mubu_api.py trash` |
| 按名称搜索 | `python3 scripts/mubu_api.py search <关键字> [--max-depth N] [--limit N]` |
| 按名称搜索（含回收站） | `python3 scripts/mubu_api.py search <关键字> [--max-depth N] [--limit N] --include-trash` |
| 按名称搜索（JSON） | `python3 scripts/mubu_api.py search <关键字> [--max-depth N] [--limit N] --json` |
| 追加一级标题（红+粗） | `python3 scripts/mubu_api.py append <doc_id> 标题1 标题2` |
| 设置一级标题（复用已有/不足新建） | `python3 scripts/mubu_api.py set-headings <doc_id> 标题1 标题2` |
| 在已有节点下插入子节点 | `python3 scripts/mubu_api.py insert-child <doc_id> --parent 5 "子节点A" "子节点B"` |
| 删除文档内节点（级联删子树） | `python3 scripts/mubu_api.py del-node <doc_id> --index 5 --yes` |
| 写入原生表格（Markdown） | `python3 scripts/mubu_api.py table <doc_id> --md table.md` |
| 写入原生表格（父节点下） | `python3 scripts/mubu_api.py table <doc_id> --parent-path 0.1 --md table.md` |
| 写入原生表格（二维 JSON） | `python3 scripts/mubu_api.py table <doc_id> --json rows.json` |
| 导出原生表格 | `python3 scripts/mubu_api.py export-table <doc_id> --index 3 --format md` 或 `--path 0.1 --format md` |
| 插入超链接（改写已有节点） | `python3 scripts/mubu_api.py link <doc_id> --path 0 --url https://example.com --text 点我` |
| 插入超链接（新建节点） | `python3 scripts/mubu_api.py link <doc_id> --url https://example.com --text 点我` |
| 切换文档视图 | `python3 scripts/mubu_api.py view <doc_id> outline\|mindmap\|presentation` |
| 开启分享链接 | `python3 scripts/mubu_api.py share <doc_id>` |
| 查看谁引用了我 | `python3 scripts/mubu_api.py refs <doc_id> [--json]` |
| 列出模板 | `python3 scripts/mubu_api.py templates [--json]` |
| 用模板建文档 | `python3 scripts/mubu_api.py template-use <uuid> [--name 新名]` |
| 官方导入建文档 | `python3 scripts/mubu_api.py import-doc 标题 --md file.md` |
| 列出标签 | `python3 scripts/mubu_api.py tags [--json]` |
| 标签搜索建议 | `python3 scripts/mubu_api.py tags --search 关键词 [--at]` |
| 刷新 / 关闭分享 | `python3 scripts/mubu_api.py share <doc_id> --refresh \| --close` |
| 文字挖空（原生 HTML） | `python3 scripts/mubu_api.py mask <doc_id> --path <node-path> --text <text>` |
| 文字高亮（原生 HTML） | `python3 scripts/mubu_api.py highlight <doc_id> --path <node-path> --text <text> [--color yellow]` |
| 原生公式 | `python3 scripts/mubu_api.py formula <doc_id> --path <node-path> --latex <latex> [--block]` |
| 文档内链 | `python3 scripts/mubu_api.py mention <doc_id> --path <node-path> --target-doc <target-doc-id> --name <name>` |
| 节点引用 | `python3 scripts/mubu_api.py node-mention <doc_id> --path <node-path> --target-doc <target-doc-id> --target-node <target-node-id> --text <text>` |
| 创建概要 | `python3 scripts/mubu_api.py summary-create <doc_id> --member <node-path> [--member <node-path> ...] [--text <text>]` |
| 删除概要 | `python3 scripts/mubu_api.py summary-delete <doc_id> <summary-id> --member <node-path> [--member <node-path> ...]` |
| 设置 Emoji | `python3 scripts/mubu_api.py emoji <doc_id> --path <node-path> --value <emoji>` |
| 设置待办状态 | `python3 scripts/mubu_api.py task <doc_id> --path <node-path> --status <0\|1\|2>` |
| 折叠或展开节点 | `python3 scripts/mubu_api.py collapse <doc_id> --path <node-path> [--expand]` |
| 移动文档内节点 | `python3 scripts/mubu_api.py move-node <doc_id> --path <source-path> <index> [--parent <parent-path>]` |
| 附加图片 | `python3 scripts/mubu_api.py image <doc_id> --path <node-path> --file <png-or-jpeg> [--width <pixels>]` |
| 创建节点连接线 | `python3 scripts/mubu_api.py link-nodes <doc_id> --from-path <from-path> --to-path <to-path> [--side left\|right\|top\|bottom]` |

> 上表列出的节点级与富内容命令（包括 `mask` / `highlight` / `formula` / `mention` / `node-mention` /
> `summary-create` / `summary-delete` / `emoji` / `task` / `collapse` / `move-node` / `image` / `link-nodes`）
> 属于系统命令层，协议依据见 `docs/protocol-notes.md`。

### 原生文本格式（第一批）

`mask` 和 `highlight` 改写指定节点的 `text`，分别写入幕布原生 HTML：
`<span class="mask">文字</span>` 与 `<span class="highlight-yellow">文字</span>`。
对应 Python 转换器为 `mask_html(text)` 和 `highlight_html(text, color="yellow")`。
每个命令都必须在隔离文档完成“新建空文档 → 写入 → 读回核验”的真机 E2E 后，才可宣称可用；本说明不代表已经完成真机验证。

本批新增的公式、内链、节点引用、概要、Emoji、待办、折叠、移动排序、图片和节点连接线命令，以及 `mask` / `highlight`，当前均等待同一轮最终隔离真机 E2E 验收；高亮颜色仅支持 `yellow`。

### 典型工作流

**场景 1：用户说"把这份大纲同步到幕布"**
1. 确认内容来源（文件或对话中直接提供）
2. 如果是 Markdown，直接用脚本创建文档并导入
3. 返回新文档 ID 和链接

**场景 2：用户说"导出我的幕布笔记"**
1. 先列出文档列表让用户选择，或按名称搜索
2. 获取文档内容
3. 转换为 Markdown 格式返回

**场景 3：用户说"在幕布建一个项目文件夹"**
1. 确认文件夹名称和层级结构
2. 批量创建文件夹
3. 返回创建结果

---

## 工作流示例

### 示例 1: 从 Markdown 创建幕布文档

```
用户: 把这份 Markdown 大纲同步到幕布
```

执行步骤：
1. 解析 Markdown 结构
2. 转换为幕布 JSON 格式
3. 登录获取 Token
4. 创建文档并保存内容

### 示例 2: 导出幕布文档为 Markdown

```
用户: 导出我的"读书笔记"文档
```

执行步骤：
1. 登录获取 Token
2. 本地搜索匹配文档：`python3 scripts/mubu_api.py search "读书笔记"`
3. 获取文档内容
4. 转换为 Markdown 并返回

### 示例 3: 批量创建文件夹结构

```
用户: 在幕布创建项目文档结构：需求分析、设计文档、开发日志、测试报告
```

执行步骤：
1. 登录获取 Token
2. 创建项目文件夹
3. 批量创建子文件夹
4. 返回创建结果
