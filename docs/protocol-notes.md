# 幕布 changeset 协议参考

> 传输：所有事件都 POST 到 `/v3/api/colla/events`，
> body = `{memberId, type:"CHANGE", version, documentId, events:[...]}`。
> 本文区分已观察协议与当前可用行为。

## 一、事件类型总览

| 事件名 | 载荷键 | 用途 | 状态 |
|---|---|---|---|
| `create` | `created` | 新建节点（节点自带 `children` 可一次建整棵子树） | 已实现 |
| `update` | `updated` | 改节点字段 / 改 `text` | 已实现 |
| `delete` | `deleted` | 删节点（级联删整棵子树） | 已实现 |
| **`structureChanged`** | **`changed`** | **移动 / 排序节点（含换父节点）** | **已实现 · 真机验证** |

**通用位置寻址**：`path` = `["nodes", i]` 或 `["nodes", i, "children", j]`；
`index` = 同级序号；`parentId` = 父节点 id（顶层为 `null`）。

`create` 和 `delete` 的 `path` 同样支持嵌套节点：例如 `["nodes", 0, "children", 1]` 指向第一个顶层节点的第二个子节点位置；`create` 在该位置插入，`delete` 删除该位置的现有节点，`parentId` 必须是直接父节点 ID。`save` 对嵌套增删改按节点路径逐步发送 changeset，并在每一步写入后读回、按最新树重算路径。

```json
{"name":"create","created":[{"index":i,"parentId":null,"node":{...},"path":["nodes",i]}]}
{"name":"update","updated":[{"updated":{...},"original":{...},"path":["nodes",i]}]}
{"name":"delete","deleted":[{"index":i,"parentId":null,"node":{...},"path":["nodes",i]}]}
{"name":"structureChanged","changed":[{"original":{"parentId":null,"index":1,"node":{...},"path":["nodes",1]},
                                        "changed": {"parentId":null,"index":7,"node":{...},"path":["nodes",7]}}]}
```

`update.updated` 是**部分字段补丁**，不是完整节点快照。真机示例：改 `collapsed` 时为 `{"id":"<node_id>","collapsed":true}`；改 `text` 时为 `{"id":"<node_id>","text":"<new_text>","modified":<timestamp>,"highlight":"<value>","color":"<value>"}`。不能从这些片段推断其它字段组合（详见「待办 / 折叠 / 高亮」）。

`structureChanged` 安全边界：每个写入步骤后都会重新读取文档并基于最新树重算源 / 目标路径；节点身份无法唯一确定、目标父节点位于待移动节点自身子树、或目标父节点是在当前同步中新建时**失败关闭**——不猜测身份、不形成环、不引用尚未稳定的路径。

## 二、写入行为与安全边界

- 文档保存按节点级 `create` / `update` / `delete` changeset 与 `path` 同步，不整体回写 definition；每个已发送 changeset 后都会重新读取并做语义核验。低层 `save_doc(events=...)` 对空列表、非对象事件、缺失事件名或结构不合格的事件失败关闭。
- 空 definition 对象 `{}` 和 `nodes: null` 读作空节点列表；`nodes` 是非数组的其他值属于无效文档结构。空定义是合法空文档，不应被当成读取错误。
- 重复节点 ID，或无法唯一消解的节点身份匹配会中止同步。文档内部同父重排和跨父级移动通过 `structureChanged` 实现；身份歧义、自子树目标及移动到当前同步中新建的父节点失败关闭。这里说的是大纲内部节点，不是 CLI `move` 移动文档 / 文件夹。
- 写入超时、连接断开和 HTTP 5xx **不自动重放**，因为服务端可能已经完成写操作；无法读回确认时，错误可能报告"结果未知"。先读远端当前状态，再由操作者决定是否重试。
- `create` 先校验 definition，再创建空文档并逐个写顶层节点。节点正文部分失败时错误包括 `doc_id`、已完成 / 总数及已确认节点 ID；先查询该 ID 的当前文档，不要盲目重复 `create`。
- 服务端读回可省略默认值 `children=[]`、`priority=0`、`highlight=""`、`taskStatus=0`、`note=""`、`collapsed=false`、`finish=false`、`color=""`、`deadline=0`、`remindAt=0`；核验将这些缺失视为相应默认值。非默认值仍严格核验，节点 `id` 与 `text` 也须匹配。
- 写入同步会保留目标内容没有声明的已有节点字段，包括 `highlight`、`color` 和未知 / 未来字段；目标明确提供字段值时则按目标同步。
- **发布门槛：** 任何新增或修改的写操作，在隔离测试文档上完成"新建空文档 → 写入 → 读回语义核验"的真机 E2E 前，不得宣称真机可用。自动测试通过不能替代此门槛。
- **双端视觉验收：** 写入成功 + 读回一致仍不够，同一份内容须在**网页版与桌面版**都看过——两端渲染引擎不一致，同一 HTML 表现可能完全不同。

## 三、节点级字段

| 字段 | 类型 | 含义 | 样例 |
|---|---|---|---|
| `emoji` | str | 节点 Emoji 图标 | `"😄"` |
| `images` | list | 节点内图片 | `[{"id","uri","ow","oh","w"}]` |
| `imageLayouts` | list | 图片布局 | `[{"count":1}]` |
| `highlight` | str | 高亮色（空字符串 = 无） | `""` |
| `color` | str | 文字色（空字符串 = 无） | `""` |

图片字段含义：`uri` = 服务端路径 `document_image/<userId>_<uuid>.<ext>`；`ow` / `oh` = 原图宽高；`w` = 显示宽度。

图片写入 = **先上传拿到 `uri`，再做节点 `update`**；上传通道见「图片上传通道对比」。

## 四、text 里的富文本 HTML 编码

| 功能 | 编码 |
|---|---|
| 加粗 / 红字 | `<span class="bold text-color-red">文字</span>` |
| 斜体 / 下划线 | `class="italic"` / `class="underline"` |
| **文字挖空** | `<span class="mask">文字</span>` |
| 普通超链接 | `<a class="content-link" ...>`（见「外链的真实 HTML 模板」） |
| **文档内链（双向链接）** | `<a class="mention mm-iconfont" ...>`（见下） |
| **节点引用** | `<span class="node-mention" ...>`（见下） |
| 表格 | `<div class="table-container"><table class="auto-table" ...>` |

### 文档内链（mention）

```html
<a class="mention mm-iconfont" target="_blank" rel="noreferrer" spellcheck="false"
   contenteditable="false" href="https://mubu.com/doc<docId>"
   id="mention-<random>" data-mention="<urlencode(JSON)>"
   data-type="1" data-token="<docId>">显示名</a>
```

`data-mention` 解码后：

```json
{"type":2,"id":"<random>","mentionType":1,"mentionNotify":false,
 "token":"<docId>","link":"https://mubu.com/doc<docId>",
 "textEn":"","text":"显示名","docId":"<docId>"}
```

> ⚠️ 内链 href **没有斜杠**：`https://mubu.com/doc` + docId（不是 `/doc/`）。

### 节点引用（node-mention）

```html
<span class="node-mention" id="<random>" spellcheck="false" contenteditable="false"
      data-doc="<docId>" data-node="<nodeId>" data-text="<urlencode(JSON)">
  <span>被引用节点的文本</span>
</span>
```

`data-text` 解码后：`[{"type":1,"text":"被引用节点的文本"}]`

## 五、公式 / 连接线 / 图片

### 数理化公式

```html
<span class=" formula" data-raw="x%5E2" contenteditable="false">&#8203;&#8203;&#8203;</span>
```

- **公式也是 text 里的 HTML**：`<span class=" formula" data-raw="<URL编码的 LaTeX>" contenteditable="false">`
- `data-raw` 是 **URL 编码**的公式源码：`x%5E2` 解码 = `x^2`
- span 内部是**零宽字符**（不可见），真正渲染靠 `data-raw`
- 行内公式在正文里被 `$ ... $` 包裹（前后各一个 `<span>$</span>`）
- ⚠️ `class=" formula"` **带一个前导空格**；HTML 里多余空格无害，但照抄最稳
- ⚠️ **写入时不应带 `$`** —— 只写 formula span 本身。带 `$` 会让网页端多显示两个 `$`，且两端处理路径不一致

### 思维导图连接线

```json
{"name":"update","updated":[{
  "updated":{"id":"<source_node_id>","linkLines":[{
      "id":"<link_line_id>","fromNodeId":"<source_node_id>","toNodeId":"<target_node_id>",
      "modified":1789375236654,
      "fromAttachment":{"side":"right","t":0.5},
      "toAttachment":{"side":"right","t":0.5},
      "controlPoints":[[110.70867067120469,0],[110.70867067120469,0]]}]},
  "original":{"id":"<source_node_id>"},"path":["nodes",0]}]}
```

- **连接线 = 节点字段 `linkLines`**（数组），挂在**起点节点**上
- 每条：`{id, fromNodeId, toNodeId, modified, fromAttachment:{side,t}, toAttachment:{side,t}, controlPoints:[[x,y],[x,y]]}`
- `side` = 连接方位（如 `right`）；`t` = 在该边上的相对位置（`0.5` = 居中）
- `controlPoints` = 贝塞尔控制点（仅用于画线）
- 创建 = 往 `linkLines` **追加**一项；**删除连接线的写法未采集**

### 图片

```json
{"id":"<target_node_id>",
 "images":[{"id":"<image_id>","uri":"document_image/<userId>_<uuid>.png",
            "ow":777,"oh":209,"w":400}],
 "imageLayouts":[{"count":1}]}
```

- **图片 = 节点字段 `images`（数组）+ `imageLayouts`**，不在 `text` 里
- `ow` / `oh` = 原图宽高，`w` = 显示宽度，`uri` = 服务端路径 `document_image/<userId>_<uuid>.<ext>`
- 上传必须走 **TOS 直传**，并在上传后调用注册接口（见「图片上传通道对比」「图片上传后必须注册」）

## 六、其它请求类型（了解即可，不要重放）

| 请求 | 作用 | 备注 |
|---|---|---|
| `POST /colla/events`，body `type:"CURSOR"` | 光标位置同步 | 结构：`{reqId, memberId, type:"CURSOR", documentId, cursor:{anchorId, anchorOffset, anchorNodeType, focusId, focusOffset, focusNodeType}}`；无需实现 |
| `GET /v4/api/user/personalStorageSpace/exceed` | 存储配额检查 | 无需实现 |
| `POST /v4/api/user/trace` | **埋点 / AB 实验上报** | 含 AB 分组列表与操作场景；**纯遥测，绝不重放** |

## 七、待办 / 折叠 / 高亮

### 待办（`taskStatus`）

```json
// 普通节点 -> 待办
{"name":"update","updated":[{
  "updated":{"id":"<node_id>","taskStatus":1,"finish":false,"deadline":0,"remindAt":0},
  "original":{"id":"<node_id>","taskStatus":0,"deadline":0,"remindAt":0},
  "path":["nodes",1]}]}

// 勾选完成
{"name":"update","updated":[{
  "updated":{"id":"<node_id>","taskStatus":2,"finish":false,"deadline":0,"remindAt":0},
  "original":{"id":"<node_id>","taskStatus":1,"finish":false,"deadline":0,"remindAt":0},
  "path":["nodes",1]}]}
```

| `taskStatus` | 含义 |
|---|---|
| `0` | 普通节点（无勾选框） |
| `1` | 待办 · 未完成，Markdown 显示 `[ ]` |
| `2` | 待办 · 已完成，Markdown 显示 `[x]` |

复选框只由 `taskStatus` 的 `0/1/2` 状态决定；`finish` 单独存在不会渲染复选框。只实现并确认 `0 → 1` 与 `1 → 2` 两种转换；其它状态转换及与其它字段合并仍失败关闭。

配套字段：`finish`（bool）、`deadline`（`0` = 无截止时间）、`remindAt`（`0` = 无提醒）。读回可能省略它们各自的默认值。

### 折叠（`collapsed`）

```json
{"name":"update","updated":[{
  "updated":{"id":"<node_id>","collapsed":true},
  "original":{"id":"<node_id>","collapsed":false},
  "path":["nodes",3]}]}
```

⚠️ **关键**：这里 `updated` **只带了 `{id, collapsed}`** —— `update` 只提交本次修改的字段。

### 高亮

```json
{"name":"update","updated":[{
  "updated":{"id":"<node_id>","text":"<span class=\"highlight-yellow\">文字</span>"},
  "original":{"id":"<node_id>","text":"<span>文字</span>"},
  "path":["nodes",4]}]}
```

**高亮是 `text` 里的 class**：`<span class="highlight-yellow">`，**不是节点字段**。

> 注意区分：节点上的 `highlight` 字段（读回常见 `highlight: ""`）是另一回事，用途未明。

### 已排除（不要实现）

| 功能 | 结论 | 依据 |
|---|---|---|
| **优先级** | **幕布没有此功能** | app.js 中「优先级」出现 0 次；`priority` 的命中全是 React / HTML 属性名。`normalize_node` 里的 `priority: 0` 是上游凭空的假设 |
| **附件** | **不是面向用户的功能** | app.js 中「附件」出现 0 次；只有 4 个 `/v4/api/document/attachment/*` 端点，疑为内部 / 导入导出用 |

### 概要（summary）

**数据模型：不是特殊节点，也不是文档级属性。**
它是**每个成员节点上的 `data.summaryData` 数组**——一个概要覆盖 N 个节点时，这 N 个节点**各存一份相同的记录**；读取时收集各副本、取 `modified` 最大的那份。

> ⚠️ `summaryData` 是**数组**：一个节点可以**同时属于多个概要**。

**网络请求：增 / 删 / 改全都走普通 `update`**，没有 summary 专属事件。

#### 创建概要（覆盖 1 个节点）

```json
{"memberId":"...","type":"CHANGE","version":131,"documentId":"...","events":[{
  "name":"update","updated":[{
    "updated":{"id":"Foh6Oko0Yg","summaryData":[{
        "id":"G75A7hspIy","createdAt":1789439258892,"modified":1789439258892,
        "text":"","memberIds":["Foh6Oko0Yg"],"children":[]}]},
    "original":{"id":"Foh6Oko0Yg"},
    "path":["nodes",1,"children",0,"children",2]}]}]}
```

#### 把概要扩展到 3 个节点

```json
{"version":132,"events":[{"name":"update","updated":[
  {"updated":{"id":"Foh6Oko0Yg","summaryData":[{ "modified":1789439261181,
      "memberIds":["0u7U0cLxct","ucxnU95hkV","Foh6Oko0Yg"] }]},
   "original":{"id":"Foh6Oko0Yg","summaryData":[{ "memberIds":["Foh6Oko0Yg"] }]},
   "path":["nodes",1,"children",0,"children",2]},

  {"updated":{"id":"0u7U0cLxct","summaryData":[{ "memberIds":["0u7U0cLxct","ucxnU95hkV","Foh6Oko0Yg"] }]},
   "original":{"id":"0u7U0cLxct"},
   "path":["nodes",1,"children",0,"children",0]},

  {"updated":{"id":"ucxnU95hkV","summaryData":[
      {"id":"IjJNXzMASV","memberIds":["ucxnU95hkV"]},
      {"id":"G75A7hspIy","memberIds":["0u7U0cLxct","ucxnU95hkV","Foh6Oko0Yg"]}]},
   "original":{"id":"ucxnU95hkV","summaryData":[{"id":"IjJNXzMASV"}]},
   "path":["nodes",1,"children",0,"children",1]}]}]}
```

#### 实现要点

| 要点 | 说明 |
|---|---|
| **一条 update 里放 N 条 entry** | 每个成员节点一条，`path` 是该节点**自己的位置** |
| **副本必须完全一致** | 所有副本的 `memberIds` 相同、`modified` 用**同一个时间戳** |
| `original` 的形态 | 该节点**原本没有**这个概要 -> `{"id": ...}`；**已有** -> `{"id": ..., "summaryData":[...]}` |
| **追加而非覆盖** | 要**保留**该节点原有的其它概要（上例 `ucxnU95hkV` 就有两条） |
| `text` | 初始为空字符串 `""`（**不是**对象数组） |
| `children` | `[]` |

#### Summary 记录形状

```ts
{
  id: string,          // 概要自己的 id，客户端生成
  createdAt: number,   // ms
  modified: number,    // ms —— 所有副本用同一个值
  text: string,        // 初始 ""
  memberIds: string[], // 被覆盖的节点 id（所有副本内容一致）
  children: []         // 初始 []
}
```

### 其它事件名枚举（来自 app.js）

```js
En = { CREATE:"create", UPDATE:"update", DELETE:"delete",
       STRUCTURE_CHANGE:"structureChanged", SETTING_CHANGE:"settingChanged",
       TITLE_CHANGE:"nameChanged" }
type(传输层) = { CHANGE:"CHANGE", CURSOR:"CURSOR", WATCH:"WATCH",
                UNWATCH:"UNWATCH", USER_HEARTBEAT:"USER_HEARTBEAT", ROOM_MEMBERS:"ROOM_MEMBERS" }
```

## 八、复选框渲染：字段对照

**问题**：普通节点在幕布里显示成「未勾选复选框 ☐」，到底是什么字段造成的？

同一文档受控变量对照结果：

| 行 | 发给服务端的字段 | 服务端存下来的 | 渲染结果 |
|---|---|---|---|
| A | `id/text/modified`（网页端最小载荷） | `id, modified, text` | 圆点 · 无框 |
| B | A + `finish=false` | `finish, id, modified, text` | **圆点 · 无框** |
| C | 工具当前全量载荷（16 字段） | `collapsed, color, finish, id, modified, note, text` | **圆点 · 无框** |
| D | A + `taskStatus=1` + `finish=false/deadline=0/remindAt=0` | `finish, id, modified, taskStatus, text` | **☐ 复选框** |
| E | A + `taskStatus=0` | `id, modified, text`（0 被服务端丢弃） | 圆点 · 无框 |

**结论**：

- **决定复选框的只有 `taskStatus`**：`0` = 无框；`1` / `2` = 有框
- **`finish` / `deadline` / `remindAt` 只是 `taskStatus` 的配套字段，它们本身不影响渲染**（B 行只加 `finish=false`，渲染仍是圆点）
- `taskStatus=0` 会被服务端**丢弃**（不回传），因此不能靠它判断
- ⚠️ 历史真凶：`checked` 字段（`markdown_to_doc` 为 `- [ ]` 列表产生）才会导致 ☐；现在客户端已把 `checked` 拦在 payload 之外，并在本地转成 `taskStatus`

### Emoji 完整请求载荷

```json
{"name":"update","updated":[{
  "updated": {"id":"qvHWBfYG10","emoji":"😄"},
  "original":{"id":"qvHWBfYG10"},
  "path":["nodes",6]}]}
```

要点：

- **只发两个字段**：`updated` = `{id, emoji}`，`original` = `{id}`（最小形态，不用回传整节点）
- `emoji` 直接放 emoji 字符；**传空字符串即可清除 Emoji**
- 写入后服务端返回 `emoji`（长度 1）

## 九、外链 / 图片注册 / 公式 `$` —— 前端 JS 实证

### 9.1 外链的真实 HTML 模板 ⚠️ 易错点

序列化函数 `tl`（app.js @3178549）：

```js
'<a class="' + 样式类('content-link') + '" '
  + (e.id ? 'data-id="'+e.id+'" ' : '')
  + 'target="_blank" spellcheck="false" rel="noreferrer" href="' + 转义URL
  + '"><span class="content-link-text">' + 文字 + '</span></a>'
```

**规范模板**：

```html
<a class="content-link" data-id="<随机id>" target="_blank" spellcheck="false" rel="noreferrer" href="URL"><span class="content-link-text">文字</span></a>
```

| 要点 | 说明 |
|---|---|
| class | **必须有 `content-link`**（另有 bold / italic / text-color-* 等样式类会拼上） |
| 文字 | 必须包在 `<span class="content-link-text">` 里 |
| **没有 `contenteditable`** | 与 mention 不同（mention 明确带 `contenteditable="false"`） |
| `data-id` | 有 id 才输出 |

> ❌ 错误写法是**裸 `<a href="...">文字</a>`**：没有 class、没有 `content-link-text`。
> 这解释了「桌面版能打开一次、点一次后就没了」—— 桌面端不认识裸 `<a>`，交互后被规范化剥离。

### 9.2 图片上传后必须「注册」

上传成功后编辑器会调用：

```js
async syncRecentImgs(e){
  await net({ url: SyncRecentImgs, method: "POST", data: { imageIdList: [e] } })
}
// SyncRecentImgs: "/v3/api/document/sync_recently_used_img"
```

**规范调用**：

```
POST /v3/api/document/sync_recently_used_img
body: {"imageIdList": ["document_image/<userId>_<uuid>.png"]}
```

- key 必须**原样**是 `document_image/...` 的 TOS object key（不是完整 URL）
- 触发条件：`key.startsWith("document_image/")`

> ❌ 上传后**未调用它** → 图片没进「用户图片库」→ **桌面端解析不到（破图）**，而网页端走另一条解析路径所以能显示。

### 9.3 公式的 `$`：编辑器**不会**帮你移除

| 路径 | 行为 |
|---|---|
| 序列化器 `ts` | **从不输出 `$`**；只输出 `<span class=" formula" data-raw="<URL编码>" contenteditable="false">零宽字符</span>` |
| **非 Markdown（默认，`markdown:!1`）** | DOM 里的 `<span>$</span>` 会被**读回成普通文本 token** → 再次序列化仍是 `<span>$</span>`，**不移除** |
| Markdown 重解析 | 词法器 `flat` 会**吃掉 `$`** |

> 抓包里那两个 `<span>$</span>` 是**真实持久化形态**（按 `$x^2$` 输入触发，非 Markdown 模式回读保留）。
> **写入时不应带 `$`** —— 只写 formula span 本身。带 `$` 会让网页端多显示两个 `$`。

### 9.4 验收标准

> **「写入成功 + 读回一致」不够，必须加「双端视觉验收」**
> —— 同一份内容，**网页版和桌面版都要看**。
> 两端渲染引擎不一致，同一个 HTML 表现可能完全不同。

## 十、桌面端渲染的硬性门槛

### 10.1 图片必须 >= 500 字节

桌面端 `assetsManger` 有启发式保护：下载到的图片若小于 **500 字节**，就判定为「服务端返回了错误响应」并拒绝渲染：

```
[WARN]  assetsManger - [Image Size Too Small] size: 341 bytes (expected >= 500), preview: <PNG...
[ERROR] assetsManger - [Image Fetch Failed] Invalid image size, contentType: image/png
```

⚠️ **程序生成的小图极易踩坑**：纯色块 / 小图标 / 1x1 像素 PNG 压缩后常常只有几百字节。
**网页端能正常显示，桌面端却报破图** —— 极易被误判成上传逻辑的 bug。

实测：600x300 的渐变 + 噪声 PNG（486 KB）在两种上传通道下都能正常显示。

### 10.2 桌面端走「本地缓存 + 缩略图处理」

| 项 | 值 |
|---|---|
| 缓存目录 | `%APPDATA%\Mubu\mubu_app_data\mubu_data\caches\<userId>\` |
| 缓存文件名 | `<key>.q_<base64(查询串)>`，例如 `...png.q_eC10b3MtcHJvY2Vzcz1pbWFnZS9yZXNpemUsd182MDA` |
| 实际请求 | `https://document-image.mubu.com/<key>?x-tos-process=image/resize,w_600&from=electron-proxy` |

把 `q_` 后面的 base64 解码即可得到查询串：`x-tos-process=image/resize,w_600`。

### 10.3 渲染问题排查入口

**桌面端日志**：`%APPDATA%\Mubu\mubu_app_data\mubu_data\log\app.log`

搜 `assetsManger` / `Image Fetch` 能直接看到失败原因、缓存 key 与请求 URL。

### 10.4 图片上传通道对比

| 通道 | key 形态 | 格式 | 备注 |
|---|---|---|---|
| `POST /document/upload_img_base64`（旧） | `document_image/<uuid>-<userId>.jpg` | 服务端**转成 jpg** | 一行搞定，但丢原格式 |
| **TOS 直传（网页端同款）** | `document_image/<userId>_<uuid>.<ext>` | **保留原格式** | 需 `GET /tos/sts` 拿临时凭证 + `TOS4-HMAC-SHA256` 签名 |

> 两条通道桌面端都能渲染；本项目采用 **TOS 直传**（更原生、保留原格式、key 命名与网页端一致）。
> ⚠️ TOS4 与 AWS SigV4 的**唯一区别**：密钥派生**不加 `"TOS4"` 前缀**
> （`kDate = HMAC(secretAccessKey, dateStamp)`），加了会 403 `SignatureDoesNotMatch`。

## 十一、文档级设置 `settingChanged`

**这是第 5 种事件类型**，改的是**文档级**（而非节点级）设置。

### 载荷形状

```json
{"name":"settingChanged","setting":{"viewType":"MINDMAP"}}
```

⚠️ 键名是 **`setting`**。网页端内部字段叫 `changed`，但传输层 `ts()` 会改名：

```js
e.name === En.SETTING_CHANGE && e.changed
  ? { name: En.SETTING_CHANGE, setting: e.changed }
  : e
```

写成 `{"name":"settingChanged","changed":{...}}` 会被服务端拒绝（**实测 illegal request**）。

### 可设字段（6 个）

| 字段 | 取值 |
|---|---|
| `viewType` | `OUTLINE`（大纲）/ `MINDMAP`（思维导图）/ `PRESENTATION`（演示） |
| `structure` | 文档结构，默认 `DEFAULT` |
| `theme` | 主题 |
| `structureSetting` | 结构设置 |
| `colorScheme` | 配色方案 |
| `customStyle` | 自定义样式（JSON 字符串） |

**最小载荷可用**：只传要改的字段即可（实测 `{"setting":{"viewType":"MINDMAP"}}` 通过）。

### ⚠️ 无法读回校验

这些设置**任何 API 都不返回**：

- `get_doc` 顶层键只有 `author / baseVersion / definition / directory / name / role`
- `list/get` 的文档条目里也没有 viewType 类字段

所以**只能写、不能读回**，验收必须靠人眼打开文档确认。（实测：写入 MINDMAP 后，文档确实以思维导图形式打开。）

## 十二、分享链接

| 操作 | 端点 | body | 返回 |
|---|---|---|---|
| 取分享域名 | `POST /common/share_domain` | `{}` | `{"domain": "share.mubu.com"}` |
| 开启分享 | `POST /document/create_link` | `{"docId": <doc>}` | `{"shareId": "...", "version": ...}` |
| 刷新链接 | `POST /document/refresh_link` | `{"docId": <doc>}` | **新的** `shareId`（旧链接失效） |
| 关闭分享 | `POST /document/close_link` | `{"docId": <doc>}` | `{"version": ...}` |

**链接格式**：`https://share.mubu.com/doc/<shareId>`

- ✅ 实测 `/doc/<id>` 返回 200；而 `https://share.mubu.com/<id>`（少 `/doc/`）会 302 跳回 `https://mubu.com/<id>` —— 用这个差异可以确认路径格式
- ⚠️ 分享页是**纯 SPA 壳**（HTML 里没有文档标题），**无法用状态码或 HTML 校验 shareId 是否有效**（真假 id 都返回 200）—— 验收必须人眼打开链接
- `POST /document/share/get` 传 `{"shareId": ...}` 返回 `{}`（不提供文档信息）

### 其它分享相关端点（未实现）

`GetShareId: /v3/doc/get/shareId`、`GetBasicShareDoc: /v3/doc`、`GetDetailShareDoc: /v3/api/document/view/get`、`VerifyPwd: /v3/doc/verify-password`、`SetSharePwd: /v3/api/document/save_doc_password`（分享密码）

## 十三、官方导出

端点在前端映射表里，但**载荷形状没挖到**（JS 里只出现在端点映射与 i18n 文案中，调用点很可能是**页面导航**而非 XHR）：

| 端点 | 试过的 body / 结果 |
|---|---|
| `ExportFile: /v3/api/export/file` | `{docId,type}` / `{id,fileType}` / `{id,exportType}` / `{id,format}` → 全部 `code:1 system error` |
| `Export: /convert/export`、`ExportV2: /convert/export/v2` | **不在 `/v3/api` 基址下**（实际应是 `https://mubu.com/convert/export` 页面路由） |
| `ExportPdf: /convert/print-pdf` | 同上 |
| `DownloadFile: /v3/download/` | 下载路由 |

> **结论**：官方导出走的是**页面路由**（不是 JSON API），需要浏览器上下文（cookie / 会话态），不适合在无头客户端中复刻。
> 已有的 `get --export markdown` + `opml` 已覆盖纯文本导出需求。

## 十四、双向链接「谁引用了我」

```
POST /v3/api/refer/doc/list     body {"targetDocId": <docId>}
-> {"list": [{
      "docId": "<引用方文档>", "docName": "...",
      "nodeId": "<含引用的节点>", "node": {"id": ..., "text": ...},
      "ancestors": [...], "mentionId": "mention-xxx",
      "targetDocId": "<本文档>", "targetDocName": "...",
      "createTime": ..., "userId": ...}]}
```

### ⚠️ 两个大坑

1. **参数名必须是 `targetDocId`**
   写成 `{"id": ...}` **不报错，但静默返回空列表** —— 极易被误判成「没有任何引用」，或误以为缺少绑定步骤。
2. **无需任何 bind 调用**
   只要节点 `text` 里有规范 mention HTML，服务端**自动索引**（实测插入后立刻可查）。
   前端确实存在 `/refer/bind`，但它的触发时机是**双向链接面板里点 Link/Unlink 按钮**，**不是插入 `@` 时、也不是保存时**（通过扫描全部 156 个 chunk 确认）。

### 其它 refer 端点

| 端点 | 参数 / 状态 |
|---|---|
| `refer/node/list` | `{"targetDocId": ..., "targetNodeId": ...}`；节点级反向链接，实测为空（可能只对节点级引用生效） |
| `refer/node/count` | `{"targetDocId": ...}`；实测 `{"countMap": {}}` |
| `refer/search_refers` | 形状未确认（试过的 body 均 `system error`） |
| `refer/bind` / `refer/unbind` | `{"docId": <对端doc>, "memberId": <当前doc的memberId>, "updated": [{"original": {"id","text","note"}, "updated": {"id","text","note"}}]}`（来自 JS，未真机验证） |

## 十五、模板

```
POST /v3/api/template/get_list   {}                    -> 模板集合
POST /v3/api/template/view       {"uuid": "<uuid>"}    -> 模板详情
```

### get_list 的返回结构

| 键 | 内容 |
|---|---|
| `personal` | **我的模板**（字段最全：`uuid` / `name` / `useCount` / `viewCount` / `imgPath` / `tag` …） |
| `recent` | 最近使用 |
| `dailyNote` | 每日笔记模板 |
| `recommend` | 推荐模板（按分类分组：`[{categoryId, items:[模板…]}]`） |
| `categoryList` | 分类元数据 |

### view 的返回结构

```json
{"name": "工作日报", "useCount": 283820, "viewCount": 749681, "source": 1,
 "mainDefinition": "{\"nodes\":[{\"children\":[...],\"id\":\"...\"}]}"}
```

⚠️ `mainDefinition` 是**JSON 字符串**，需二次解析（结构同 `{"nodes":[...]}`）。

### 「使用模板」的实现

模板本身**没有「应用」接口**，正确做法是：

1. `template/view` 取 `mainDefinition`
2. 解析出 `nodes`
3. 新建空文档（`create_doc`）+ `append_top_nodes(nodes)`

实测：用「工作日报」模板建出的文档有 18 个节点，结构完全一致。

## 十六、官方导入通道 `import_doc`

```
POST /v3/api/list/import_doc
body {"name": "<标题>", "folderId": "<文件夹>", "itemCount": <顶层节点数>,
      "define": "{\"nodes\":[...]}"}          <- define 是 JSON **字符串**
-> 返回完整文档对象（含 id）
```

要点：

- **一次请求即可建出带内容的文档** —— 是 `create_doc(content=...)` 的原生替代（后者实际是「建空文档 + 逐个 append」，因为 create 接口不认 content）
- `itemCount` = `define.nodes` 的长度
- ⚠️ 不能把字段包在 `{"data": {...}}` 里（会 `system error`）
- 实测：从 Markdown 导入后，顶层 1 / 总节点 5，结构与 `markdown_to_doc` 产物一致
- ⚠️ `markdown_to_doc()` 会**剥离 UTF-8 BOM**（`\ufeff`）：Windows 上用记事本 / PowerShell `Set-Content -Encoding utf8` 保存的 `.md` 常带 BOM，会导致**首行标题解析失败**（实测报「无法识别 Markdown 结构（第 1 行）」）。同类 BOM 问题也影响 `.env.mubu` 的解析。

## 十七、标签

### ⚠️ 全部是 GET，没有 JSON body

| 用途 | 请求 | 响应 |
|---|---|---|
| **全部标签** | `GET /v4/api/document/tag/list`（**v4 基址**，无参数） | `{"atTags": [...], "hashTags": [{"id","tag","count","visitTime"}]}` |
| **# 标签搜索建议** | `GET /v3/api/document/get_hash_tag?keyword=<词>` | `{"tags": [{"id","tag","score","count","visitTime","highlights"}]}` |
| **@ 提及搜索建议** | `GET /v3/api/document/get_at_tag?keyword=<词>` | 同上 |

要点：

- 两个 `get_*_tag` 是**编辑器里输入 `#` / `@` 时的候选搜索建议**，**不是**「文档里的标签」
- 参数走 **query string** 的 `keyword`（**不含** `#` / `@` 本身）
- 写成 POST / 用 JSON body → 一律 `code:17 illegal request`

### v4 基址

`MubuClient._request` 支持 v4 路径：以 `/v4/api` 开头的 endpoint 会走 `https://api2.mubu.com/v4/api/...`（默认基址是 `/v3/api`）。

## 十八、已知未采集 / 未实现

| 项 | 状态 |
|---|---|
| 连接线的**删除**写法 | 未采集（创建走 `linkLines` 追加） |
| `refer/search_refers` 请求形状 | 未确认（试过的 body 均 `system error`） |
| `refer/bind` / `refer/unbind` | 来自 JS，未真机验证 |
| `refer/node/list` 节点级反向链接 | 实测返回空，触发条件未确认 |
| 模板 `add` / `rename` / `delete` / `set_doc` | 未探测 |
| 分享密码等端点（`SetSharePwd` 等） | 未实现 |
| 官方导出载荷形状 | 页面路由，不计划复刻 |
| 节点 `highlight` 字段的用途 | 未明（注意与 text 里的 `highlight-*` class 区分） |
| `attachment` 4 个端点 | 疑为内部 / 导入导出用，不计划实现 |
| 文档级设置读回 | 服务端不返回：只能写、无法读回校验 |
