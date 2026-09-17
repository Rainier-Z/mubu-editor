"""mubu 包 — MubuClient：鉴权、请求、文档/文件夹/搜索/导出等操作。"""

import copy
import json
import os
import secrets
import string
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from mubu import tos
from mubu.config import (
    BASE_URL,
    DEFAULT_HEADERS,
    ENDPOINTS,
    ENV_FILE,
    MAX_NETWORK_RETRIES,
    MAX_SEARCH_DEPTH,
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_REQUESTS,
    NETWORK_BACKOFF,
    REQUEST_TIMEOUT,
    TOKEN_FILE,
    TRASH_FILE,
    MubuError,
    _safe_local_path,
    _secure_file_permissions,
    _token_file_lock,
    logger,
)
from mubu.convert import (
    _unique_filename,
    export_markdown,
    normalize_node,
)
from mubu.convert import (
    style_text as _style_text,
)
from mubu.media import (
    MediaError,
    read_image_details,
    validate_display_width,
)
from mubu.methods.headings import (
    HEADING_BOLD,
    HEADING_COLOR,
    HEADING_LEVEL,
)
from mubu.methods.headings import (
    append_headings as _append_headings,
)
from mubu.methods.headings import (
    set_headings as _set_headings,
)
from mubu.methods.headings import (
    style_heading as _style_heading,
)

_SAFE_READ_ENDPOINTS = frozenset((ENDPOINTS["list"][1], ENDPOINTS["get_doc"][1]))


class MubuOutcomeUnknownError(MubuError):
    """请求可能已被服务端处理，但客户端未收到确认。"""

    result_unknown = True


def _atomic_replace_text(path: Path, text: str) -> None:
    """将 UTF-8 文本写到同目录唯一临时文件，再原子替换目标。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    descriptor_open = True
    try:
        # mkstemp creates the file with safe POSIX mode, but Windows inherits
        # the parent directory DACL. Apply the explicit sensitive-file ACL
        # before writing any secret bytes.
        _secure_file_permissions(temp_path)
        temp_file = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        descriptor_open = False
        with temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        # The temp descriptor's ACL follows rename, and this final application
        # also protects callers/filesystems which adjust security on replace.
        _secure_file_permissions(path)
    finally:
        if descriptor_open:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


class MubuClient:
    """幕布 API 客户端"""

    def __init__(self, phone: Optional[str] = None, password: Optional[str] = None) -> None:
        # 在读取 phone/password 之前，先尝试从 .env 文件补全凭据
        self._load_env_file()
        self.phone = phone or os.getenv("MUBU_PHONE")
        self.password = password or os.getenv("MUBU_PASSWORD")
        self.token = None
        self.user_id = None
        self.username = None
        self.member_id = None  # colla 会话 id，由 _load_token / 环境变量补全
        self.expires_at = 0  # Token 过期时间戳（秒）
        # P2 #22：复用 requests.Session 连接池，search 多请求场景下避免每次新建连接
        self._session = requests.Session()
        self._load_token()  # 先还原 token 缓存中的 member_id（若有）
        # memberId 为幕布 colla（协同）命名空间下的每账号会话 id，
        # 既非登录 id 也非 JWT sub，无法经任何 API 反查；来源优先级：
        # 环境变量 MUBU_MEMBER_ID（config/.env.mubu）> token 缓存兜底。
        if not self.member_id:
            self.member_id = os.getenv("MUBU_MEMBER_ID")
        # 模拟浏览器 window.uniqueId / 会话的稳定标识（move 端点签名所需），
        # 整个客户端生命周期内不变，用于对齐网页端请求头以平抑 code:17（真机待验证）。
        self._client_unique_id = str(uuid.uuid4())
        self._session_id = str(uuid.uuid4())

    def _load_env_file(self, path: Optional[Path] = None) -> None:
        """从 .env 文件加载凭据（仅当环境变量未设置时补全）。

        默认读取 ENV_FILE（config/.env.mubu）；文件不存在则静默跳过。
        逐行解析 KEY=VALUE，忽略空行与 # 注释行。
        仅补全 MUBU_PHONE / MUBU_PASSWORD，且环境变量已设置时优先于文件。

        Args:
            path: 可选，指定 .env 文件路径（便于测试；默认用 ENV_FILE）
        """
        env_path = path or ENV_FILE
        if not env_path.exists():
            return
        # Keep an existing credential file private before reading credentials.
        # ACL failures are security failures and must not be swallowed below.
        _secure_file_permissions(env_path)
        try:
            for line in env_path.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # 仅在环境变量未设置时补全
                if key in ("MUBU_PHONE", "MUBU_PASSWORD", "MUBU_MEMBER_ID") and not os.getenv(key):
                    os.environ[key] = value
        except Exception:
            # 加载失败不影响主流程，后续 login 会提示设置环境变量
            pass

    def _load_token(self) -> bool:
        """从本地加载 Token（未过期才生效）"""
        if TOKEN_FILE.exists():
            # Do not read an inherited/over-broad Windows DACL token cache.
            _secure_file_permissions(TOKEN_FILE)
            try:
                data = json.loads(TOKEN_FILE.read_text())
                expires_at = data.get("expires_at", 0)
                if time.time() < expires_at:
                    self.token = data.get("token")
                    self.user_id = data.get("user_id")
                    self.username = data.get("username")
                    self.member_id = data.get("member_id")
                    self.expires_at = expires_at
                    return True
            except Exception:
                pass
        return False

    def _save_token(self) -> None:
        """原子写 Token 到本地：先写临时文件再 rename，避免中途崩溃留残缺文件。

        临时文件和最终文件都通过配置层设置当前用户/SYSTEM ACL（Unix 为 chmod 0600）。
        原子写整体用跨进程文件锁包裹，避免多进程并发写损坏 Token 文件。
        """
        self.expires_at = time.time() + 7200  # 2 小时过期
        data = {
            "token": self.token,
            "user_id": self.user_id,
            "username": self.username,
            "member_id": self.member_id,
            "expires_at": self.expires_at
        }
        with _token_file_lock(TOKEN_FILE):
            _atomic_replace_text(TOKEN_FILE, json.dumps(data, indent=2))

    def _get_headers(self) -> Dict[str, str]:
        """获取带认证的请求头。

        在 DEFAULT_HEADERS 基础上补 4 个「浏览器同款」头，对齐网页版 mubu 前端
        的请求出口（app.js 模块 15224 的 axios 封装，并经 2026-08-04 抓包复核）：
        data-unique-id / x-session-id / x-reg-entrance / x-request-id。
        - data-unique-id：客户端生命周期内稳定 uuid4（__init__ 生成）。
        - x-session-id：``{uuid}:{epoch秒}`` 格式（前缀稳定，后缀每次请求刷新）。
        - x-reg-entrance：固定 ``https://mubu.com/app``。
        - x-request-id：每次请求重新生成 uuid4。
        Jwt-Token 原有逻辑不变。
        """
        headers = DEFAULT_HEADERS.copy()
        if self.token:
            headers["Jwt-Token"] = self.token
        headers["data-unique-id"] = self._client_unique_id
        headers["x-session-id"] = f"{self._session_id}:{int(time.time())}"
        headers["x-reg-entrance"] = "https://mubu.com/app"
        headers["x-request-id"] = str(uuid.uuid4())
        return headers

    def ensure_valid_token(self) -> None:
        """确保 Token 有效，临近过期则重新登录。

        刷新策略：未持有 token，或距过期不足 (300 + leeway) 秒时重新登录。
        leeway=60 预留网络与处理余量。不依赖 refresh_token，凭据来自缓存的
        phone/password。
        """
        leeway = 60
        if not self.token or time.time() > self.expires_at - 300 - leeway:
            self.login()

    def _is_auth_error(self, result: Dict[str, Any], response: "requests.Response") -> bool:
        """判断是否为鉴权失效错误。

        仅当 HTTP 401，或响应 code 表示登录失效（含相关关键字）时返回 True。
        403 权限不足或其它非 0 code 不触发重登，避免误重试。
        """
        if response.status_code == 401:
            return True
        code = result.get("code")
        if code is not None and code != 0:
            msg = str(result.get("msg", "")).lower()
            # 收紧关键字：仅保留明确指向登录失效的短语，移除 "token"/"auth"/
            # "expire"/"login"/"过期" 等易出现在正常业务错误中的泛化词，
            # 避免误触发重登、掩盖真实错误
            auth_keywords = (
                "登录", "未登录", "重新登录", "登录失效", "unauthorized"
            )
            if any(k in msg for k in auth_keywords):
                return True
        return False

    def _http_request(self, method: str, url: str, headers: Dict[str, str],
                      retry_safe: bool = True, **kwargs) -> requests.Response:
        """执行 HTTP 请求；仅被显式分类为安全读取的业务操作可自动重试。

        此层只负责网络健壮性，**不触发重登**：
        - 安全读取：网络异常、HTTP 5xx、HTTP 429 可有限重试。
        - 写入及未知操作：只发一次；网络异常或 HTTP 5xx 以“结果未知”结束。
        - 所有请求关闭 requests 的自动重定向，防止自定义 JWT 头跨主机泄漏。
        重试上限 MAX_NETWORK_RETRIES（即最多共发起 3 次请求），退避见 NETWORK_BACKOFF。
        其余 4xx（含 401）响应会原样返回，交由上层 _request 处理鉴权重试。

        retry_safe 的缺省 True 仅保留既有私有低层调用的兼容行为；业务入口
        ``_request`` 会根据 endpoint 语义显式传入分类，未知 endpoint 按写操作处理。
        """
        last_err: Optional[Exception] = None
        max_attempts = MAX_NETWORK_RETRIES + 1 if retry_safe else 1
        kwargs.pop("allow_redirects", None)
        for attempt in range(max_attempts):
            try:
                logger.debug("HTTP %s %s (attempt=%s, timeout=%ss)",
                             method, url, attempt + 1, REQUEST_TIMEOUT)
                response = self._session.request(
                    method, url, headers=headers, timeout=REQUEST_TIMEOUT,
                    allow_redirects=False, **kwargs
                )
            except requests.exceptions.RequestException as e:
                last_err = e
                if retry_safe and attempt + 1 < max_attempts:
                    time.sleep(NETWORK_BACKOFF[min(attempt, len(NETWORK_BACKOFF) - 1)])
                    continue
                if not retry_safe:
                    raise MubuOutcomeUnknownError(
                        f"写请求未收到服务端确认，结果未知；请先查询服务端状态，确认后再决定是否重试: {e}"
                    ) from e
                raise MubuError(
                    f"网络连接失败，请检查网络（已重试 {MAX_NETWORK_RETRIES} 次）: {e}",
                    status_code=None,
                )

            # 429 仅对已知安全读取重试（不重登）。
            if response.status_code == 429:
                last_err = None
                if retry_safe and attempt + 1 < max_attempts:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and str(retry_after).isdigit():
                        time.sleep(min(int(retry_after), 30))
                    else:
                        time.sleep(NETWORK_BACKOFF[min(attempt, len(NETWORK_BACKOFF) - 1)])
                    continue
                raise MubuError(
                    "请求过于频繁（HTTP 429），请稍后重试"
                    + (f"（已重试 {MAX_NETWORK_RETRIES} 次）" if retry_safe else "；写操作未自动重试"),
                    status_code=429,
                    body=response.text,
                )

            # 5xx 仅对安全读取重试；写入响应可能丢失，按未知结果处理。
            if response.status_code >= 500:
                last_err = None
                if retry_safe and attempt + 1 < max_attempts:
                    time.sleep(NETWORK_BACKOFF[min(attempt, len(NETWORK_BACKOFF) - 1)])
                    continue
                if not retry_safe:
                    raise MubuOutcomeUnknownError(
                        f"写请求收到 HTTP {response.status_code}，服务端处理结果未知；"
                        "请先查询服务端状态，确认后再决定是否重试",
                        status_code=response.status_code,
                        body=response.text,
                    )
                raise MubuError(
                    f"幕布服务暂不可用，请稍后重试（HTTP {response.status_code}，"
                    f"已重试 {MAX_NETWORK_RETRIES} 次仍失败）",
                    status_code=response.status_code,
                    body=response.text,
                )

            # 非 5xx（含 401 等 4xx、2xx）原样返回，交由上层处理
            return response

        # 理论不可达：兜底抛错，避免漏掉 last_err
        raise MubuError(
            f"网络请求失败（非预期）: {last_err or '未知错误'}",
            status_code=None,
        )

    def _request(self, method: str, endpoint: str, max_retries: int = 1,
                 auth: bool = True, **kwargs: Any) -> Dict:
        """发送 HTTP 请求，统一处理鉴权与重试。

        分层说明：
        - 网络层/5xx 重试：由 _http_request 负责，最多 2 次，不重登。
        - 鉴权失效重试：本方法递归处理，max_retries 默认 1（仅重试 1 次，杜绝死循环）。

        两条链路互斥、互不干扰：网络重试只重发请求，鉴权重试才重登。

        Args:
            method: HTTP 方法
            endpoint: 接口路径（取自 ENDPOINTS）
            max_retries: 鉴权失败时的重试次数上限（默认 1，杜绝死循环）
            auth: 是否需要在发起前确保 Token 有效（login 自身应传 False）
        """
        if auth:
            self.ensure_valid_token()

        logger.debug("请求 %s %s (auth=%s)", method, endpoint, auth)
        # 支持 v4 端点：部分接口（如 /v4/api/document/tag/list）不在 v3 基址下。
        if endpoint.startswith("/v4/api"):
            url = BASE_URL.rsplit("/v3/api", 1)[0] + endpoint
        else:
            url = f"{BASE_URL}{endpoint}"
        headers = self._get_headers()
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))

        # 网络层/5xx 重试在 _http_request 内完成，这里拿到的是已确认非 5xx 的响应
        response = self._http_request(
            method, url, headers, retry_safe=endpoint in _SAFE_READ_ENDPOINTS, **kwargs)

        if 300 <= response.status_code < 400:
            raise MubuError(
                f"服务端返回 HTTP 重定向（status={response.status_code}）；"
                "出于凭据安全，客户端已禁用自动跟随，请检查 MUBU_BASE_URL 和服务端地址",
                status_code=response.status_code,
            )

        # 响应体非 JSON（限流 HTML / 502 错误页等）→ 抛友好异常，不再抛裸 JSONDecodeError
        try:
            result = response.json()
        except ValueError:
            raise MubuError(
                f"响应解析失败（status={response.status_code}, url={url}）："
                f"响应体不是预期的 JSON",
                status_code=response.status_code,
                body=response.text,
            )

        # 只有明确的安全读取才允许鉴权失效后重登并重试。写请求一旦已经
        # 到达服务端，401 可能只是响应阶段的鉴权失败；自动重放会制造
        # 重复创建/修改，因此必须把结果标记为未知并交给调用方核验。
        if self._is_auth_error(result, response):
            if endpoint in _SAFE_READ_ENDPOINTS and max_retries > 0:
                self.login()
                return self._request(method, endpoint, max_retries=max_retries - 1, auth=auth, **kwargs)
            # 登录本身没有业务写入副作用，失败应报告凭据错误而不是“业务结果未知”。
            if endpoint not in _SAFE_READ_ENDPOINTS and endpoint != ENDPOINTS["login"][1]:
                raise MubuOutcomeUnknownError(
                    "写请求鉴权失败，客户端未自动重放；服务端是否已处理结果未知，"
                    "请先读取并核验服务端状态后再决定是否重试",
                    status_code=response.status_code,
                    body=result,
                )
            # 401 或登录失效类错误，重试后仍失败 → 给出下一步操作指引
            raise MubuError(
                f"登录失效或密码错误，请检查凭据后重试"
                f"（{result.get('msg', '未知错误')}）",
                status_code=response.status_code,
                body=result,
            )

        if result.get("code") != 0:
            # 403 权限不足或其它业务错误，不触发重登
            if response.status_code == 403:
                raise MubuError(
                    f"权限不足，请确认账号权限"
                    f"（{result.get('msg', '未知错误')}）",
                    status_code=response.status_code,
                    body=result,
                )
            raise MubuError(
                f"API 错误: {result.get('msg', '未知错误')}",
                status_code=response.status_code,
                body=result,
            )

        return result.get("data", {})

    def login(self) -> Dict:
        """登录幕布（auth 引导，自身不走 ensure_valid_token）"""
        if not self.phone or not self.password:
            raise MubuError("请设置 MUBU_PHONE 和 MUBU_PASSWORD 环境变量，或传入参数")

        data = self._request(*ENDPOINTS["login"], auth=False, max_retries=0, json={
            "phone": self.phone,
            "password": self.password,
            "callbackType": 0
        })

        # 登录返回的是扁平结构，token 和用户信息都在 data 里
        self.token = data["token"]
        self.user_id = data["id"]
        self.username = data["name"]
        # 防御性尝试从登录响应读取 colla memberId。已知限制：幕布登录响应
        # 不含 memberId（任何 API 均不返回），此处仅作无害兜底；读不到则保持原值
        #（来自 config/.mubu_token 缓存或 MUBU_MEMBER_ID 环境变量，由 P0 校验兜底）。
        if not self.member_id:
            self.member_id = data.get("memberId") or data.get("member_id")
        self._save_token()

        return {
            "token": self.token,
            "user_id": self.user_id,
            "username": self.username
        }

    def ensure_login(self) -> None:
        """确保已登录（兼容旧调用；_request 内已统一处理）"""
        if not self.token:
            self.login()

    def get_list(self, folder_id: str = "0",
                 include_trashed: bool = False) -> List[Dict]:
        """获取文件夹下的文档和子文件夹列表。

        Args:
            folder_id: 文件夹 ID（默认 "0" 为根）
            include_trashed: 为 False 时过滤掉已软删除（回收站）中的项；
                为 True 时保留。软删除不影响云端，仅本地标记过滤。
        """
        data = self._request(*ENDPOINTS["list"], json={"folderId": folder_id})
        if not include_trashed:
            trash = self._load_trash()
            if trash:
                # 文件夹：直接过滤
                folders = data.get("folders", []) or []
                data["folders"] = [f for f in folders if f.get("id") not in trash]
                # 文档：保留接口实际返回的 key（真机返回 documents，旧兜底 docs）
                if "documents" in data:
                    docs = data.get("documents") or []
                    data["documents"] = [d for d in docs if d.get("id") not in trash]
                elif "docs" in data:
                    docs = data.get("docs") or []
                    data["docs"] = [d for d in docs if d.get("id") not in trash]
        return data

    def create_folder(self, name: str, parent_id: str = "0") -> str:
        """创建文件夹"""
        data = self._request(*ENDPOINTS["create_folder"], json={
            "folderId": parent_id,
            "name": name
        })
        folder = data.get("folder") if isinstance(data, dict) else None
        folder_id = folder.get("id") if isinstance(folder, dict) else None
        if not folder_id:
            raise MubuOutcomeUnknownError("创建文件夹接口未返回文件夹 ID，创建结果未知")
        return folder_id

    @staticmethod
    def _fill_node_defaults(node: Dict) -> Dict:
        """递归补全节点及其子树的最小契约字段（id / taskStatus / modified / children）。

        真机实测：children 里缺 ``id`` 的后代节点会被服务端静默丢弃，
        因此一次建子树前必须给每个后代都补上 id。
        """
        if not isinstance(node, dict):
            raise MubuError("节点必须是对象")
        if not node.get("id"):
            node["id"] = MubuClient._gen_node_id()
        checked = node.pop("checked", None)
        if checked is not None and "taskStatus" not in node:
            node["taskStatus"] = 2 if bool(checked) else 1
        node.setdefault("taskStatus", 0)
        status = node["taskStatus"]
        if type(status) is not int or status not in (0, 1, 2):
            raise MubuError("节点包含无效 taskStatus")
        for field, expected in (("finish", False), ("deadline", 0), ("remindAt", 0)):
            if field in node and not MubuClient._status_companion_matches(
                    field, node[field], expected):
                raise MubuError(
                    f"taskStatus={status} 的 {field} 必须为 {expected!r}")
            node[field] = expected
        node.setdefault("modified", int(time.time() * 1000))
        node.setdefault("children", [])
        if not isinstance(node["children"], list):
            raise MubuError("节点 children 必须是数组")
        for child in node["children"]:
            MubuClient._fill_node_defaults(child)
        return node

    def append_top_nodes(self, doc_id: str, nodes: List[Dict]) -> List[str]:
        """在顶层逐个追加任意节点（可带 children 子树），返回新建节点 id 列表。

        与 ``append_nodes``（只接受纯文本）不同，本方法接受完整节点 dict，
        用于 ``create_doc(content=...)`` 以及一次建整棵子树的场景。
        """
        created_ids: List[str] = []
        for node in nodes:
            if not isinstance(node, dict):
                raise MubuError(
                    "append_top_nodes 只接受节点 dict；收到 "
                    f"{type(node).__name__}：{str(node)[:60]!r}"
                    "（若要追加纯文本，请改用 append_nodes）")
            raw = self._get_doc_raw(doc_id)
            existing = self._raw_nodes(raw, doc_id)
            index = len(existing)
            item = self._fill_node_defaults(copy.deepcopy(node))
            normalize_node(item)
            event = self.build_node_create_event(item, ["nodes", index], index)
            expected = copy.deepcopy(existing)
            expected.insert(index, copy.deepcopy(item))
            self._write_events_verified(doc_id, [event], expected, raw.get("baseVersion"))
            created_ids.append(item["id"])
        return created_ids

    def insert_child_nodes(self, doc_id: str, parent_path: List, texts) -> List[str]:
        """在**已有节点**下插入子节点，返回新建节点 id 列表。

        parent_path 形如 ``["nodes", 0]`` 或 ``["nodes", 0, "children", 1]``。
        真机格式（真机验证）：
          {"name": "create", "created": [{"index": i, "parentId": <父节点 id>,
            "node": {...}, "path": parent_path + ["children", i]}]}
        与 append_top_nodes 的区别：那个只能加**顶层**节点，这个能往任意已有节点下加。
        """
        if isinstance(texts, str):
            texts = [texts]
        created_ids: List[str] = []
        for text in texts:
            raw = self._get_doc_raw(doc_id)
            nodes = self._raw_nodes(raw, doc_id)
            parent = self._resolve_path(nodes, parent_path)
            index = len(parent.get("children") or [])
            node = self._fill_node_defaults(
                {"id": self._gen_node_id(), "text": text, "children": []})
            child_path = list(parent_path) + ["children", index]
            event = self.build_node_create_event(node, child_path, index,
                                                 parent_id=parent.get("id"))
            expected = copy.deepcopy(nodes)
            self._resolve_path(expected, parent_path).setdefault("children", []).insert(
                index, copy.deepcopy(node))
            self._write_events_verified(doc_id, [event], expected, raw.get("baseVersion"))
            created_ids.append(node["id"])
        return created_ids

    def create_doc(self, name: str, folder_id: str = "0", content: str = "") -> str:
        """创建文档，返回新文档 id。

        真机响应为扁平 ``{"id": "..."}``；测试 mock 为
        ``{"doc": {"id": "..."}}``，此处兼容两种结构，避免 CLI 打印空 id。

        真机实测：create 接口的 ``content`` 参数不会生成任何节点
        （传 definition JSON 只会得到一篇空文档）。因此 content 非空时改为两步：
        先建空文档，再用 ``name:create`` changeset 逐个写顶层节点
        （节点自带 children 时可一次建成整棵子树，见 ``append_top_nodes``）。
        """
        nodes: List[Dict] = []
        if content not in (None, ""):
            try:
                parsed = json.loads(content) if isinstance(content, str) else content
            except (TypeError, ValueError) as e:
                raise MubuError(f"文档 content 不是有效 JSON：{e}") from e
            if not isinstance(parsed, dict) or not isinstance(parsed.get("nodes"), list):
                raise MubuError("文档 content 必须是包含 nodes 数组的对象")
            nodes = parsed["nodes"]
            seen_ids = set()

            def validate_node(node, path):
                if not isinstance(node, dict):
                    raise MubuError(f"文档 content 的节点必须是对象（path={path}）")
                node_id = node.get("id")
                if node_id:
                    if not isinstance(node_id, str) or node_id in seen_ids:
                        raise MubuError(f"文档 content 含无效或重复节点 ID（path={path}）")
                    seen_ids.add(node_id)
                if "text" in node and not isinstance(node["text"], str):
                    raise MubuError(f"文档 content 的 text 必须是字符串（path={path}）")
                children = node.get("children", [])
                if children is None:
                    children = []
                if not isinstance(children, list):
                    raise MubuError(f"文档 content 的 children 必须是数组（path={path}）")
                for index, child in enumerate(children):
                    validate_node(child, f"{path}.children[{index}]")

            for index, node in enumerate(nodes):
                validate_node(node, f"nodes[{index}]")
            try:
                json.dumps(parsed)
            except (TypeError, ValueError) as e:
                raise MubuError(f"文档 content 不能序列化为 JSON：{e}") from e

        data = self._request(*ENDPOINTS["create_doc"], json={
            "folderId": folder_id,
            "name": name,
            "content": ""
        })
        if not isinstance(data, dict):
            raise MubuError("创建文档接口没有返回有效对象")
        doc = data.get("doc")
        if isinstance(doc, dict) and doc.get("id"):
            doc_id = doc["id"]
        else:
            doc_id = data.get("id") or data.get("docId") or ""
        if not doc_id:
            raise MubuOutcomeUnknownError("创建文档接口没有返回文档 ID，创建结果未知")
        created_ids: List[str] = []
        for node in nodes:
            try:
                ids = self.append_top_nodes(doc_id, [node])
                if len(ids) != 1 or not ids[0]:
                    raise MubuError("节点写入没有返回已确认的节点 ID")
                created_ids.extend(ids)
            except Exception as e:
                raise MubuError(
                    f"文档已创建 doc_id={doc_id}；正文写入失败，进度 "
                    f"{len(created_ids)}/{len(nodes)}，已创建节点 IDs={created_ids}：{e}"
                ) from e
        return doc_id

    # ------------------------------------------------------------------ #
    # 文档级设置（真实 settingChanged changeset，真机验证）
    # 载荷形状：{"name":"settingChanged","setting":{...}}
    #   —— 网页端内部字段叫 changed，传输层会改名成 **setting**（写成 changed 会
    #      illegal request，已实测）。
    # ⚠️ 这些设置**任何 API 都不返回**（get_doc 顶层只有 author/baseVersion/
    #    definition/directory/name/role），所以无法写后读回校验，只能写。
    # ------------------------------------------------------------------ #
    VIEW_TYPES = ("OUTLINE", "MINDMAP", "PRESENTATION")

    # ------------------------------------------------------------------ #
    # 分享链接（端点与形状均为真机探测确认）
    #   创建 POST /document/create_link   {"docId": <doc>} -> {"shareId": ...}
    #   刷新 POST /document/refresh_link  {"docId": <doc>} -> 新 shareId（旧的失效）
    #   关闭 POST /document/close_link    {"docId": <doc>}
    #   域名 POST /common/share_domain    {}                -> {"domain": "share.mubu.com"}
    # 链接格式：https://<share_domain>/doc/<shareId>
    # ------------------------------------------------------------------ #
    SHARE_DOMAIN_FALLBACK = "share.mubu.com"

    def get_share_domain(self) -> str:
        """取分享域名（服务端返回，缺省 share.mubu.com）。"""
        data = self._request("POST", "/common/share_domain", json={})
        domain = data.get("domain") if isinstance(data, dict) else None
        return str(domain or self.SHARE_DOMAIN_FALLBACK)

    def _share_link(self, share_id: str) -> str:
        return f"https://{self.get_share_domain()}/doc/{share_id}"

    def create_share_link(self, doc_id: str) -> str:
        """开启文档分享并返回可访问链接（`https://share.mubu.com/doc/<shareId>`）。"""
        data = self._request("POST", "/document/create_link", json={"docId": doc_id})
        share_id = data.get("shareId") if isinstance(data, dict) else None
        if not share_id:
            raise MubuError("创建分享链接失败：响应缺少 shareId")
        return self._share_link(str(share_id))

    def refresh_share_link(self, doc_id: str) -> str:
        """刷新分享链接（旧链接失效），返回新链接。"""
        data = self._request("POST", "/document/refresh_link", json={"docId": doc_id})
        share_id = data.get("shareId") if isinstance(data, dict) else None
        if not share_id:
            raise MubuError("刷新分享链接失败：响应缺少 shareId")
        return self._share_link(str(share_id))

    def close_share_link(self, doc_id: str) -> None:
        """关闭分享（链接失效）。"""
        self._request("POST", "/document/close_link", json={"docId": doc_id})

    # ------------------------------------------------------------------ #
    # 双向链接：谁引用了我（真机探测确认）
    #   POST /v3/api/refer/doc/list   body {"targetDocId": <docId>}
    # ⚠️ 参数名必须是 **targetDocId**。写成 {"id": ...} 不报错但**静默返回空列表**
    #    （极易误判成「没有引用」）。
    # ⚠️ 也**无需任何 bind 调用**：只要节点 text 里有规范 mention HTML，
    #    服务端就会自动索引（实测插入后立刻可查）。
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # 模板（真机确认）
    #   POST /template/get_list  {}                 -> 推荐/个人/最近 模板
    #   POST /template/view      {"uuid": <uuid>}   -> 含 mainDefinition 的模板详情
    #   「使用模板」= 取 mainDefinition（形如 {"nodes":[...]} 的 JSON 字符串）
    #                  写进一篇新建的空文档
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # 官方导入通道（真机确认）
    #   POST /list/import_doc
    #   body {"name", "folderId", "itemCount", "define"}
    #        define = {"nodes":[...]} 的 **JSON 字符串**
    #   -> 返回完整文档对象（含 id）
    # 与 create_doc(content=...) 的区别：那个是「建空文档 + 逐个追加」，
    # 这是网页端「导入」用的原生通道，**一次请求即可带内容建好**。
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # 标签（真机确认）
    #   全部标签：GET /v4/api/document/tag/list（**v4 基址**，无参数）
    #             -> {"atTags": [...], "hashTags": [{"id","tag","count","visitTime"}]}
    #   搜索建议：GET /document/get_hash_tag?keyword=<词>   （at=True 用 get_at_tag）
    #             -> [{id, tag, score, count, visitTime, highlights}]
    #   ⚠️ 这两个是 **GET**，没有 JSON body —— 用 POST 一定 illegal request
    # ------------------------------------------------------------------ #
    def list_tags(self) -> Dict:
        """列出账号用过的标签（``POST/GET /v4/api/document/tag/list``）。

        返回 ``{"atTags": [...], "hashTags": [{"id", "tag", "count", "visitTime"}]}``。
        """
        data = self._request("GET", "/v4/api/document/tag/list")
        return data if isinstance(data, dict) else {}

    def search_tags(self, keyword: str, at: bool = False) -> List[Dict]:
        """标签搜索建议（编辑器里输入 ``#`` / ``@`` 时的候选）。

        Args:
            keyword: 搜索词（**不含** ``#`` / ``@`` 本身）
            at: True 用 ``get_at_tag``（@ 提及），False 用 ``get_hash_tag``（# 标签）
        """
        path = "/document/get_at_tag" if at else "/document/get_hash_tag"
        data = self._request("GET", path, params={"keyword": keyword})
        tags = data.get("tags") if isinstance(data, dict) else None
        return tags if isinstance(tags, list) else []

    def import_doc(self, name: str, folder_id: str = "0", nodes=None) -> str:
        """通过官方导入接口创建一篇**带内容**的文档（一次请求），返回新文档 id。

        Args:
            name: 文档标题
            folder_id: 目标文件夹（默认根 ``"0"``）
            nodes: 顶层节点列表（``{"nodes":[...]}`` 里的 ``nodes``）
        """
        payload_nodes = list(nodes or [])
        body = {
            "name": name,
            "folderId": folder_id,
            "itemCount": len(payload_nodes),
            "define": json.dumps({"nodes": payload_nodes}, ensure_ascii=False),
        }
        data = self._request("POST", "/list/import_doc", json=body)
        doc_id = data.get("id") if isinstance(data, dict) else None
        if not doc_id:
            raise MubuError("导入文档失败：响应缺少 id")
        return str(doc_id)

    def list_templates(self) -> Dict:
        """列出模板。

        返回键：``recommend``（按分类的推荐模板）/ ``personal``（我的模板）/
        ``recent``（最近使用）/ ``dailyNote`` / ``categoryList``（分类元数据）。
        每个模板项含 ``uuid`` / ``name`` / ``useCount`` / ``viewCount`` / ``imgPath`` 等。
        """
        data = self._request("POST", "/template/get_list", json={})
        return data if isinstance(data, dict) else {}

    def get_template(self, uuid: str) -> Dict:
        """取模板详情（含 ``mainDefinition``：形如 ``{"nodes":[...]}`` 的 JSON 字符串）。"""
        data = self._request("POST", "/template/view", json={"uuid": uuid})
        if not isinstance(data, dict) or not data.get("mainDefinition"):
            raise MubuError(f"模板详情缺少 mainDefinition（uuid={uuid}）")
        return data

    def create_doc_from_template(self, uuid: str, name: Optional[str] = None,
                                 folder_id: str = "0") -> str:
        """用模板创建一篇新文档，返回新文档 id。"""
        tpl = self.get_template(uuid)
        try:
            definition = json.loads(tpl["mainDefinition"])
        except (TypeError, ValueError) as exc:
            raise MubuError(f"模板定义解析失败：{exc}") from exc
        nodes = definition.get("nodes") if isinstance(definition, dict) else None
        if not isinstance(nodes, list) or not nodes:
            raise MubuError("模板定义里没有可写入的节点")
        doc_id = self.create_doc(name or tpl.get("name") or "未命名", folder_id, "")
        if doc_id:
            self.append_top_nodes(doc_id, nodes)
        return doc_id

    def list_backlinks(self, doc_id: str) -> List[Dict]:
        """列出「谁引用了我」——引用了本文档的其他文档。

        每条记录含 ``docId`` / ``docName``（引用方文档）、``nodeId`` / ``node``
        （含引用的那个节点）、``ancestors``（祖先路径）、``mentionId`` / ``createTime`` 等。
        """
        data = self._request("POST", "/refer/doc/list", json={"targetDocId": doc_id})
        items = data.get("list") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    def count_backlinks(self, doc_id: str) -> int:
        """引用本文档的条目数。"""
        return len(self.list_backlinks(doc_id))

    def set_document_settings(self, doc_id: str, settings: Dict) -> None:
        """修改文档级设置。

        可设字段（取自网页端文档模型与 updateSetting）：
          - ``viewType``:        ``OUTLINE`` / ``MINDMAP`` / ``PRESENTATION``
          - ``structure``:       文档结构，默认 ``DEFAULT``
          - ``theme``:           主题
          - ``structureSetting``:结构设置
          - ``colorScheme``:     配色方案
          - ``customStyle``:     自定义样式（JSON 字符串）

        只需传要改的字段即可（实测最小载荷 ``{"setting":{"viewType":"MINDMAP"}}`` 可用）。
        """
        if not isinstance(settings, dict) or not settings:
            raise MubuError("文档设置必须是非空对象")
        if not self.member_id:
            raise MubuError("修改文档设置需要 MUBU_MEMBER_ID（详见 SKILL.md 凭据章节）")
        raw = self._get_doc_raw(doc_id)
        payload = {
            "memberId": self.member_id,
            "type": "CHANGE",
            "version": raw.get("baseVersion"),
            "documentId": doc_id,
            "events": [{"name": "settingChanged", "setting": dict(settings)}],
        }
        self._request(*ENDPOINTS["save_doc"], json=payload,
                      headers={"x-reg-entrance":
                               f"https://mubu.com/app/edit/home/{doc_id}"})

    def set_view_type(self, doc_id: str, view: str) -> None:
        """切换文档视图：``OUTLINE``（大纲）/ ``MINDMAP``（思维导图）/ ``PRESENTATION``（演示）。"""
        target = (view or "").strip().upper()
        if target not in self.VIEW_TYPES:
            raise MubuError(
                f"viewType 必须是 {'/'.join(self.VIEW_TYPES)} 之一，收到 {view!r}")
        self.set_document_settings(doc_id, {"viewType": target})

    def get_doc(self, doc_id: str) -> Dict:
        """获取文档内容（正文大纲）。返回 {"name":..., "nodes":[...]}。"""
        self.ensure_login()
        data = self._request(*ENDPOINTS["get_doc"], json={
            "docId": doc_id,
            "password": "",
            "isFromDocDir": True,
        })
        # 真实响应：data.definition 是 JSON 字符串，需二次解析为 {"nodes":[...]}
        try:
            definition = json.loads(data["definition"])
        except (KeyError, TypeError, ValueError) as e:
            raise MubuError(f"解析文档定义失败（doc_id={doc_id}）：{e}") from e
        return {"name": data.get("name"), "nodes": definition.get("nodes", [])}

    def build_update_event(self, doc_definition: Dict, doc_id: str) -> Dict:
        """拒绝已弃用的 root ``children`` touch。"""
        raise MubuError(
            "build_update_event 生成的 root children touch 已弃用；"
            "请使用节点级 CRUD/structure changeset"
        )
    # ------------------------------------------------------------------ #
    # 节点级真实 changeset（2026-09 抓包核对）
    # 与 build_update_event 的「根节点幂等 touch」不同，网页端真正增/改节点用的是：
    #   update: {"name":"update","updated":[{"updated":node,"original":node,
    #            "path":["nodes",i]}]}          ← 按 path 定位并替换该节点
    #   create: {"name":"create","created":[{"index":i,"parentId":None,
    #            "node":node,"path":["nodes",i]}]}  ← 在 index 处插入新节点
    # path 形如 ["nodes", 0] 或 ["nodes", 0, "children", 1]。
    # 注意：把 updated/original 传成同一棵树会被服务端当作无变化而忽略（200 空转）。
    # ------------------------------------------------------------------ #
    @staticmethod
    def style_text(text: str, bold: bool = False, italic: bool = False,
                   underline: bool = False, color: Optional[str] = None) -> str:
        """包成幕布富文本 span（bold / italic / underline / text-color-<color>）。"""
        return _style_text(text, bold=bold, italic=italic,
                           underline=underline, color=color)

    @staticmethod
    def _gen_node_id() -> str:
        """生成幕布风格的 10 位 base62 节点 id。"""
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(10))

    @staticmethod
    def _resolve_path(nodes: List[Dict], path: List) -> Dict:
        """按 path（如 ["nodes", 0, "children", 1]）在顶层节点树中定位节点。"""
        if not path or path[0] != "nodes" or len(path) < 2:
            raise MubuError(f"不支持的 path（须以 ['nodes', i] 开头）: {path}")
        try:
            node = nodes[path[1]]
            idx = 2
            while idx < len(path):
                if path[idx] != "children":
                    raise KeyError(path[idx])
                node = node["children"][path[idx + 1]]
                idx += 2
        except (KeyError, IndexError, TypeError) as e:
            raise MubuError(f"path 定位失败: {path} ({e})") from e
        return node

    @staticmethod
    def _validate_node_path(path: List, label: str = "节点 path") -> None:
        """Validate a public node path before using it for a write."""
        if (not isinstance(path, list) or len(path) < 2 or len(path) % 2
                or path[0] != "nodes"):
            raise MubuError(f"{label} 无效：{path}")
        if any(type(index) is not int or index < 0 for index in path[1::2]):
            raise MubuError(f"{label} 索引无效：{path}")
        if any(path[offset] != "children" for offset in range(2, len(path), 2)):
            raise MubuError(f"{label} 格式无效：{path}")

    def _get_doc_raw(self, doc_id: str) -> Dict:
        """拉取文档原始响应（含 baseVersion 与 definition）。"""
        return self._request(*ENDPOINTS["get_doc"], json={
            "docId": doc_id, "password": "", "isFromDocDir": True})

    @staticmethod
    def _validate_task_status(value: Any, path: str) -> None:
        if type(value) is not int or value not in (0, 1, 2):
            raise MubuError(f"{path} 必须是整数 0、1 或 2")

    @staticmethod
    def _status_companion_matches(field: str, value: Any, expected: Any) -> bool:
        if field == "finish":
            return value is expected
        return type(value) is int and value == expected

    @classmethod
    def _reject_internal_checked(cls, value: Any, path: str) -> None:
        if isinstance(value, dict):
            if "checked" in value:
                raise MubuError(f"changeset 含内部字段 checked（path={path}.checked）")
            for key, nested in value.items():
                cls._reject_internal_checked(nested, f"{path}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                cls._reject_internal_checked(nested, f"{path}[{index}]")

    @classmethod
    def _validate_payload_node(cls, node: Dict, path: str,
                               *, require_status_companions: bool = False) -> None:
        if not isinstance(node, dict):
            raise MubuError(f"{path} 必须是对象")
        cls._reject_internal_checked(node, path)
        if "summaryData" in node:
            cls._validate_summary_data(node["summaryData"], path + ".summaryData")
        if "taskStatus" in node:
            status = node["taskStatus"]
            cls._validate_task_status(status, f"{path}.taskStatus")
            for field, expected in (("finish", False), ("deadline", 0), ("remindAt", 0)):
                if field not in node:
                    if require_status_companions:
                        raise MubuError(f"{path}.taskStatus 缺少配套字段 {field}")
                elif not cls._status_companion_matches(field, node[field], expected):
                    raise MubuError(
                        f"{path}.taskStatus={status} 的 {field} 必须为 {expected!r}")
        if "children" in node:
            children = node["children"]
            if not isinstance(children, list):
                raise MubuError(f"{path}.children 必须是数组")
            for index, child in enumerate(children):
                cls._validate_payload_node(
                    child, f"{path}.children[{index}]",
                    require_status_companions=require_status_companions)

    @classmethod
    def _complete_status_companions(cls, node: Dict, path: str) -> None:
        cls._validate_payload_node(node, path)
        if "taskStatus" in node:
            for field, expected in (("finish", False), ("deadline", 0), ("remindAt", 0)):
                node.setdefault(field, expected)
        for index, child in enumerate(node.get("children") or []):
            cls._complete_status_companions(child, f"{path}.children[{index}]")

    @classmethod
    def _validate_changeset_payload(cls, events: List[Dict]) -> None:
        for event_index, event in enumerate(events):
            name = event.get("name")
            prefix = f"events[{event_index}]"
            if name == "create":
                entries = event.get("created")
                if isinstance(entries, list):
                    for index, entry in enumerate(entries):
                        if not isinstance(entry, dict) or not isinstance(entry.get("node"), dict):
                            raise MubuError(f"{prefix}.created[{index}].node 必须是对象")
                        cls._validate_payload_node(
                            entry["node"], f"{prefix}.created[{index}].node",
                            require_status_companions=True)
            elif name == "update":
                entries = event.get("updated")
                if isinstance(entries, list):
                    for index, entry in enumerate(entries):
                        if not isinstance(entry, dict):
                            raise MubuError(f"{prefix}.updated[{index}] 必须是对象")
                        patch = entry.get("updated")
                        if not isinstance(patch, dict):
                            raise MubuError(f"{prefix}.updated[{index}].updated 必须是对象")
                        if "children" in patch:
                            raise MubuError(
                                f"{prefix}.updated[{index}].updated 不允许包含 children")
                        cls._validate_payload_node(
                            patch, f"{prefix}.updated[{index}].updated",
                            require_status_companions=True)
                        original = entry.get("original")
                        if original is not None:
                            cls._validate_payload_node(
                                original, f"{prefix}.updated[{index}].original")
            elif name == "structureChanged":
                entries = event.get("changed")
                if isinstance(entries, list):
                    for index, entry in enumerate(entries):
                        if not isinstance(entry, dict):
                            raise MubuError(f"{prefix}.changed[{index}] 必须是对象")
                        for side in ("original", "changed"):
                            position = entry.get(side)
                            if isinstance(position, dict) and position.get("node") is not None:
                                cls._validate_payload_node(
                                    position["node"],
                                    f"{prefix}.changed[{index}].{side}.node")

    def build_node_update_event(self, path: List, original: Dict,
                                updated: Dict) -> Dict:
        """构造节点级 update 事件；updated 是字段补丁，original 保留完整快照。"""
        self._validate_payload_node(original, "update.original")
        self._validate_payload_node(updated, "update.updated")
        patch = {"id": updated.get("id", original.get("id"))}
        for key, value in updated.items():
            if key in ("id", "children", "checked"):
                continue
            if key not in original or original[key] != value:
                patch[key] = copy.deepcopy(value)

        if updated.get("text", original.get("text")) != original.get("text"):
            modified = updated.get("modified")
            if modified is None or modified == original.get("modified"):
                modified = int(time.time() * 1000)
            patch["modified"] = modified
            patch["highlight"] = copy.deepcopy(
                updated.get("highlight", original.get("highlight", "")))
            patch["color"] = copy.deepcopy(
                updated.get("color", original.get("color", "")))
        if "taskStatus" in updated:
            status = updated["taskStatus"]
            self._validate_task_status(status, "update.updated.taskStatus")
            for field, expected in (("finish", False), ("deadline", 0), ("remindAt", 0)):
                if field in updated and not self._status_companion_matches(
                        field, updated[field], expected):
                    raise MubuError(
                        f"taskStatus={status} 的 {field} 必须为 {expected!r}")
                if status != original.get("taskStatus") or field in patch:
                    patch[field] = expected
        original_snapshot = self._sanitize_payload_node(original)
        return {"name": "update", "updated": [
            {"updated": patch, "original": original_snapshot, "path": list(path)}]}

    @staticmethod
    def _sanitize_payload_node(node: Dict) -> Dict:
        """删除仅供 Markdown 表达使用的 checked 字段，并递归复制节点。"""
        sanitized = copy.deepcopy(node)
        if isinstance(sanitized, dict):
            sanitized.pop("checked", None)
            for child in sanitized.get("children") or []:
                if isinstance(child, dict):
                    self_node = MubuClient._sanitize_payload_node(child)
                    child.clear()
                    child.update(self_node)
        return sanitized

    def build_node_create_event(self, node: Dict, path: List, index: int,
                                parent_id: Optional[str] = None) -> Dict:
        """构造节点级 create 事件（在 index 处插入，网页端真实格式）。"""
        payload_node = self._sanitize_payload_node(node)
        self._complete_status_companions(payload_node, "create.node")
        return {"name": "create", "created": [
            {"index": index, "parentId": parent_id,
             "node": payload_node,
             "path": list(path)}]}

    @staticmethod
    def _validate_structure_position(nodes: List[Dict], position: Dict,
                                     label: str, *, source: bool) -> Tuple[List[Dict], Optional[str]]:
        if not isinstance(position, dict) or not isinstance(position.get("node"), dict):
            raise MubuError(f"structureChanged 的 {label} 位置无效")
        path = position.get("path")
        index = position.get("index")
        if (not isinstance(path, list) or len(path) < 2 or len(path) % 2
                or path[0] != "nodes" or type(index) is not int or index < 0):
            raise MubuError(f"structureChanged 的 {label} path/index 无效")
        if any(type(value) is not int or value < 0 for value in path[1::2]):
            raise MubuError(f"structureChanged 的 {label} path 索引无效")
        if any(path[offset] != "children" for offset in range(2, len(path), 2)):
            raise MubuError(f"structureChanged 的 {label} path 格式无效")
        if path[-1] != index:
            raise MubuError(f"structureChanged 的 {label} path 末索引与 index 不一致")
        siblings = MubuClient._path_target_list(nodes, path)
        if source:
            if index >= len(siblings) or siblings[index] != position["node"]:
                raise MubuError("structureChanged 的源节点与快照不一致")
        elif index > len(siblings):
            raise MubuError("structureChanged 的目标 index 超出兄弟节点范围")

        parent_id = None
        if len(path) > 2:
            parent_id = MubuClient._resolve_path(nodes, path[:-2]).get("id")
        if position.get("parentId") != parent_id:
            raise MubuError(f"structureChanged 的 {label} parentId 不是直接父节点 ID")
        return siblings, parent_id

    def build_node_structure_event(self, original: Dict, changed: Dict,
                                   nodes: List[Dict]) -> Dict:
        """构造经快照校验的单节点结构移动事件。"""
        _, source_parent_id = self._validate_structure_position(
            nodes, original, "original", source=True)
        target_siblings, target_parent_id = self._validate_structure_position(
            nodes, changed, "changed", source=False)
        if changed.get("node") != original["node"]:
            raise MubuError("structureChanged 两侧必须携带同一完整节点快照")
        if (source_parent_id == target_parent_id
                and changed["index"] > len(target_siblings) - 1):
            raise MubuError("structureChanged 的同级目标 index 超出移除源节点后的范围")

        moving_id = original["node"].get("id")
        if not moving_id:
            raise MubuError("structureChanged 的源节点缺少稳定服务端 ID")

        def subtree_ids(node):
            return {node.get("id")} | set().union(
                *(subtree_ids(child) for child in node.get("children") or []))

        if target_parent_id in subtree_ids(original["node"]):
            raise MubuError("structureChanged 不能把节点移动到自身子树内")

        return {"name": "structureChanged", "changed": [{
            "original": {
                **{key: copy.deepcopy(value) for key, value in original.items()
                   if key != "node"},
                "node": self._sanitize_payload_node(original["node"]),
            },
            "changed": {
                **{key: copy.deepcopy(value) for key, value in changed.items()
                   if key != "node"},
                "node": self._sanitize_payload_node(changed["node"]),
            },
        }]}

    @staticmethod
    def _raw_nodes(raw: Dict, doc_id: str) -> List[Dict]:
        """解析并校验 get 响应中的节点数组。"""
        try:
            definition = json.loads(raw["definition"])
        except (KeyError, TypeError, ValueError) as e:
            raise MubuError(f"解析文档定义失败（doc_id={doc_id}）：{e}") from e
        if not isinstance(definition, dict):
            raise MubuError(f"文档定义不是对象（doc_id={doc_id}）")
        nodes = definition.get("nodes")
        if nodes is None:
            return []
        if not isinstance(nodes, list):
            raise MubuError(f"文档定义中的 nodes 非数组（doc_id={doc_id}）")
        if any(not isinstance(node, dict) for node in nodes):
            raise MubuError(f"文档定义包含无效节点（doc_id={doc_id}）")
        return nodes

    @staticmethod
    def _node_semantics(node: Dict) -> Dict:
        """用于写后读回的结构化比较，忽略服务端会更新的时间字段。"""
        volatile = {"modified", "createTime", "modifyTime", "timestamp", "checked"}
        omitted_defaults = {
            "children": [], "priority": 0, "highlight": "", "taskStatus": 0,
            "note": "", "collapsed": False, "finish": False, "color": "",
            "deadline": 0, "remindAt": 0, "summaryData": [],
        }
        result = {}
        for key, value in node.items():
            if key in volatile:
                continue
            if key in omitted_defaults and value == omitted_defaults[key]:
                continue
            if key == "children":
                result[key] = [MubuClient._node_semantics(child) for child in value or []]
            elif key == "summaryData" and isinstance(value, list):
                result[key] = [MubuClient._summary_record_semantics(record)
                               for record in value]
            else:
                result[key] = copy.deepcopy(value)
        return result

    @staticmethod
    def _summary_record_semantics(record: Any) -> Any:
        """概要记录仅将默认空 children 的省略视为等价。"""
        if not isinstance(record, dict):
            return copy.deepcopy(record)
        normalized = copy.deepcopy(record)
        if normalized.get("children") == []:
            normalized.pop("children", None)
        return normalized

    @classmethod
    def _nodes_semantically_equal(cls, actual: List[Dict], expected: List[Dict]) -> bool:
        return [cls._node_semantics(node) for node in actual] == [
            cls._node_semantics(node) for node in expected]

    def _write_events_verified(self, doc_id: str, events: List[Dict],
                               expected_nodes: List[Dict], version: Optional[int]) -> Dict:
        """提交一次 changeset，并在有限轮询内确认文档语义状态已落盘。"""
        if version is None:
            raise MubuError(f"写入文档缺少 baseVersion 前置条件（doc_id={doc_id}）")
        try:
            # 复合写入在这里统一做一次完整状态核验；不要让这个内部优化
            # 暴露成公共 save_doc(verify=False) 的绕过开关。
            self._save_doc_unverified(doc_id, events=events, version=version)
        except MubuError as e:
            if not getattr(e, "result_unknown", False):
                raise
            try:
                return self._verify_doc_nodes(doc_id, expected_nodes)
            except Exception as verify_error:
                raise MubuOutcomeUnknownError(
                    f"changeset 写入响应不确定，读回无法确认；结果未知（doc_id={doc_id}）："
                    f"{verify_error}"
                ) from e
        return self._verify_doc_nodes(doc_id, expected_nodes)

    def _verify_doc_nodes(self, doc_id: str, expected_nodes: List[Dict]) -> Dict:
        """有限轮询文档读回，直至结构、顺序与节点元数据符合预期。"""
        for attempt, delay in enumerate((0, 0.05, 0.1)):
            if delay:
                time.sleep(delay)
            raw = self._get_doc_raw(doc_id)
            actual = self._raw_nodes(raw, doc_id)
            if self._nodes_semantically_equal(actual, expected_nodes):
                return raw
            if attempt == 2:
                break
        raise MubuError(
            f"文档写入后读回验证失败（doc_id={doc_id}）；服务端内容与预期 changeset 不一致")

    def create_summary(self, doc_id: str, node_paths: List[List[Any]],
                       text: str = "") -> str:
        """为多个节点创建同一份概要，并返回概要 ID。

        概要是节点上的 ``summaryData`` 数组项。所有成员在一次 update
        changeset 中写入同一份概要记录；节点已有的其它概要会被保留。
        """
        if not isinstance(text, str):
            raise MubuError("概要文本必须是字符串")
        nodes, raw, paths = self._summary_snapshot(doc_id, node_paths)
        summary_id = self._gen_node_id()
        if not isinstance(summary_id, str) or not summary_id:
            raise MubuError("生成概要 ID 失败")
        if summary_id in self._summary_ids(nodes):
            raise MubuError(f"生成的概要 ID 已存在于文档快照中（summary_id={summary_id}）")
        member_ids = []
        resolved = []
        for path in paths:
            node = self._resolve_path(nodes, path)
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id:
                raise MubuError(f"概要成员节点缺少 ID（path={path}）")
            member_ids.append(node_id)
            existing = node.get("summaryData")
            if "summaryData" in node:
                self._validate_summary_data(existing, f"path={path}")
            resolved.append((path, node, existing))

        if len(member_ids) != len(set(member_ids)):
            raise MubuError("概要成员节点不能重复")
        now = int(time.time() * 1000)
        summary = {
            "id": summary_id,
            "createdAt": now,
            "modified": now,
            "text": text,
            "memberIds": list(member_ids),
            "children": [],
        }
        entries = []
        expected = copy.deepcopy(nodes)
        for path, node, existing in resolved:
            old_summary_data = copy.deepcopy(existing) if existing is not None else []
            updated_summary_data = old_summary_data + [copy.deepcopy(summary)]
            updated = {"id": node["id"], "summaryData": updated_summary_data}
            original = {"id": node["id"]}
            if existing:
                original["summaryData"] = copy.deepcopy(existing)
            entries.append({"updated": updated, "original": original, "path": list(path)})
            self._path_target_list(expected, path)[path[-1]]["summaryData"] = updated_summary_data

        event = {"name": "update", "updated": entries}
        self._write_events_verified(doc_id, [event], expected, raw.get("baseVersion"))
        return summary_id

    def delete_summary(self, doc_id: str, node_paths: List[List[Any]],
                       summary_id: str) -> int:
        """从每个指定节点移除概要 ID，并返回受影响的节点数。"""
        if not isinstance(summary_id, str) or not summary_id:
            raise MubuError("概要 ID 必须是非空字符串")
        nodes, raw, paths = self._summary_snapshot(doc_id, node_paths)
        resolved = []
        member_ids = set()
        for path in paths:
            node = self._resolve_path(nodes, path)
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id:
                raise MubuError(f"概要成员节点缺少 ID（path={path}）")
            if node_id in member_ids:
                raise MubuError("概要成员节点不能重复")
            member_ids.add(node_id)
            existing = node.get("summaryData")
            self._validate_summary_data(existing, f"path={path}")
            if not isinstance(existing, list):
                raise MubuError(f"节点 summaryData 必须是概要对象数组（path={path}）")
            matches = [item for item in existing if item.get("id") == summary_id]
            if len(matches) != 1:
                raise MubuError(
                    f"节点不包含唯一的概要 ID（doc_id={doc_id}, path={path}, summary_id={summary_id}）"
                )
            resolved.append((path, node, existing))

        entries = []
        expected = copy.deepcopy(nodes)
        for path, node, existing in resolved:
            updated_summary_data = [
                copy.deepcopy(item) for item in existing
                if item.get("id") != summary_id
            ]
            entries.append({
                "updated": {"id": node["id"], "summaryData": updated_summary_data},
                "original": {"id": node["id"], "summaryData": copy.deepcopy(existing)},
                "path": list(path),
            })
            self._path_target_list(expected, path)[path[-1]][
                "summaryData"
            ] = updated_summary_data

        event = {"name": "update", "updated": entries}
        self._write_events_verified(doc_id, [event], expected, raw.get("baseVersion"))
        return len(resolved)

    @classmethod
    def _validate_summary_data(cls, value: Any, path: str) -> None:
        """校验显式 summaryData；缺失字段表示节点没有概要。"""
        if value is None:
            raise MubuError(f"{path} 必须是概要对象数组")
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise MubuError(f"{path} 必须是概要对象数组")

    @classmethod
    def _summary_ids(cls, nodes: List[Dict]) -> set:
        """收集快照中所有概要 ID，同时拒绝无法安全保留的结构。"""
        found = set()

        def walk(items, path):
            if not isinstance(items, list):
                raise MubuError(f"{path} 必须是节点数组")
            for index, node in enumerate(items):
                node_path = f"{path}[{index}]"
                if not isinstance(node, dict):
                    raise MubuError(f"{node_path} 必须是节点对象")
                if "summaryData" in node:
                    cls._validate_summary_data(node["summaryData"], node_path + ".summaryData")
                    found.update(item.get("id") for item in node["summaryData"]
                                 if item.get("id") is not None)
                children = node.get("children") or []
                walk(children, node_path + ".children")

        walk(nodes, "nodes")
        return found

    def _summary_snapshot(self, doc_id: str, node_paths: List[List[Any]]):
        """读取一次新鲜快照并校验概要成员路径。"""
        if not isinstance(node_paths, list) or not node_paths:
            raise MubuError("概要至少需要一个节点路径")
        raw = self._get_doc_raw(doc_id)
        nodes = self._raw_nodes(raw, doc_id)
        paths = []
        for path in node_paths:
            if (not isinstance(path, list) or len(path) < 2 or len(path) % 2
                    or path[0] != "nodes" or
                    any(type(index) is not int or index < 0 for index in path[1::2]) or
                    any(path[offset] != "children" for offset in range(2, len(path), 2))):
                raise MubuError(f"概要节点路径无效：{path}")
            self._resolve_path(nodes, path)
            normalized = list(path)
            if normalized in paths:
                raise MubuError("概要成员节点路径不能重复")
            paths.append(normalized)
        if raw.get("baseVersion") is None:
            raise MubuError(f"概要写入缺少 baseVersion（doc_id={doc_id}）")
        return nodes, raw, paths

    @staticmethod
    def _path_target_list(nodes: List[Dict], path: List) -> List[Dict]:
        """返回节点 path 所在的兄弟数组；path 必须按 nodes/index/children/index 组成。"""
        if (not isinstance(path, list) or len(path) < 2 or len(path) % 2
                or path[0] != "nodes"):
            raise MubuError(f"不支持的 path（须以 ['nodes', i] 开头）: {path}")
        target = nodes
        for offset in range(2, len(path), 2):
            if path[offset] != "children":
                raise MubuError(f"不支持的 path: {path}")
            try:
                target = target[path[offset - 1]].get("children") or []
            except (IndexError, TypeError, AttributeError) as e:
                raise MubuError(f"path 定位失败: {path} ({e})") from e
        return target

    @classmethod
    def _apply_node_events(cls, nodes: List[Dict], events: List[Dict]) -> List[Dict]:
        """在本地快照上应用已知 create/update/delete 事件，用于语义核验。"""
        result = copy.deepcopy(nodes)
        for event in events:
            name = event.get("name")
            key = {
                "update": "updated", "create": "created", "delete": "deleted",
                "structureChanged": "changed",
            }.get(name)
            entries = event.get(key) if key else None
            if not isinstance(entries, list) or not entries:
                raise MubuError(f"无法验证无效的节点 changeset：{event!r}")
            if name == "structureChanged" and len(entries) != 1:
                raise MubuError("structureChanged 每个事件只能移动一个节点")
            base = copy.deepcopy(result)
            changed = copy.deepcopy(result)
            for entry in entries:
                if name == "structureChanged":
                    if not isinstance(entry, dict):
                        raise MubuError("structureChanged entry 必须是对象")
                    original = entry.get("original")
                    destination = entry.get("changed")
                    _, source_parent_id = MubuClient._validate_structure_position(
                        base, original, "original", source=True)
                    target_siblings, target_parent_id = MubuClient._validate_structure_position(
                        base, destination, "changed", source=False)
                    if destination.get("node") != original.get("node"):
                        raise MubuError("structureChanged 两侧必须携带同一完整节点快照")
                    if (source_parent_id == target_parent_id
                            and destination["index"] > len(target_siblings) - 1):
                        raise MubuError("structureChanged 的同级目标 index 超出移除源节点后的范围")
                    moving_node = original["node"]
                    moving_id = moving_node.get("id")
                    if not moving_id:
                        raise MubuError("structureChanged 源节点缺少 ID")

                    def subtree_ids(node):
                        return {node.get("id")} | set().union(
                            *(subtree_ids(child) for child in node.get("children") or []))

                    if target_parent_id in subtree_ids(moving_node):
                        raise MubuError("structureChanged 不能把节点移动到自身子树内")

                    source_list = cls._path_target_list(changed, original["path"])
                    moved = source_list.pop(original["index"])
                    if target_parent_id is None:
                        target_list = changed
                    else:
                        parents = []
                        def find_parent(node_list):
                            for node in node_list:
                                if node.get("id") == target_parent_id:
                                    parents.append(node)
                                find_parent(node.get("children") or [])
                        find_parent(changed)
                        if len(parents) != 1:
                            raise MubuError(
                                f"structureChanged 目标父节点不存在或 ID 不唯一：{target_parent_id}"
                            )
                        target_list = parents[0].setdefault("children", [])
                    if destination["index"] > len(target_list):
                        raise MubuError("structureChanged 目标 index 在移除源节点后超出范围")
                    target_list.insert(destination["index"], copy.deepcopy(moved))
                    continue

                if not isinstance(entry, dict) or not isinstance(entry.get("path"), list):
                    raise MubuError(f"无法验证缺少 path 的节点 changeset：{event!r}")
                path = entry["path"]
                if name == "create":
                    if len(path) == 2:
                        target = changed
                    else:
                        parent_path = path[:-2]
                        parent_list = cls._path_target_list(base, parent_path)
                        parent = parent_list[parent_path[-1]]
                        parent_list_changed = cls._path_target_list(changed, parent_path)
                        parent_changed = parent_list_changed[parent_path[-1]]
                        target = parent_changed.setdefault("children", [])
                        if parent.get("id") != parent_changed.get("id"):
                            raise MubuError(f"create path 的父节点在写入期间发生变化：{path}")
                    index = entry.get("index")
                    if not isinstance(index, int) or index < 0 or index > len(target):
                        raise MubuError(f"create index 超出当前兄弟节点范围：{path}")
                    target.insert(index, copy.deepcopy(entry["node"]))
                else:
                    base_list = cls._path_target_list(base, path)
                    changed_list = cls._path_target_list(changed, path)
                    index = path[-1]
                    if index < 0 or index >= len(base_list):
                        raise MubuError(f"changeset path 定位失败：{path}")
                    original = entry.get("original") if name == "update" else entry.get("node")
                    if isinstance(original, dict) and original.get("id") != base_list[index].get("id"):
                        raise MubuError(f"changeset 目标节点已变化：{path}")
                    if name == "update":
                        patch = entry.get("updated")
                        if not isinstance(patch, dict):
                            raise MubuError(f"update changeset 缺少字段补丁：{path}")
                        updated = copy.deepcopy(base_list[index])
                        updated.update(copy.deepcopy(patch))
                        changed_list[index] = updated
                    else:
                        changed_list.pop(index)
            result = changed
        return result

    def sync_doc_definition(self, doc_id: str, doc_definition: Dict) -> Dict:
        """按完整目标树同步 Markdown/definition，保留原节点未表达的元数据。"""
        if not isinstance(doc_definition, dict) or not isinstance(doc_definition.get("nodes"), list):
            raise MubuError("保存正文需要包含 nodes 数组的文档定义")
        desired_nodes = doc_definition["nodes"]

        def flatten(nodes):
            found = []
            for node in nodes:
                if not isinstance(node, dict):
                    raise MubuError("保存正文时每个节点都必须是对象")
                children = node.get("children", [])
                if children is None:
                    children = []
                if not isinstance(children, list):
                    raise MubuError("保存正文时节点 children 必须是数组")
                found.append(node)
                found.extend(flatten(children))
            return found

        def node_paths(nodes, prefix=()):
            found = {}
            for index, node in enumerate(nodes):
                path = prefix + (index,)
                found[id(node)] = path
                found.update(node_paths(node.get("children") or [], path))
            return found

        current_raw = self._get_doc_raw(doc_id)
        current_nodes = self._raw_nodes(current_raw, doc_id)
        version = current_raw.get("baseVersion")
        if version is None:
            raise MubuError(f"读取文档时缺少 baseVersion（doc_id={doc_id}）")
        old_flat = flatten(current_nodes)
        new_flat = flatten(desired_nodes)
        old_ids = [node.get("id") for node in old_flat if node.get("id")]
        new_ids = [node.get("id") for node in new_flat if node.get("id")]
        if len(old_ids) != len(set(old_ids)):
            raise MubuError(f"文档包含重复节点 ID，无法安全同步（doc_id={doc_id}）")
        if len(new_ids) != len(set(new_ids)):
            raise MubuError("目标正文包含重复节点 ID，无法安全同步")
        if len(old_ids) != len(old_flat):
            raise MubuError(f"文档节点缺少 ID，无法安全同步（doc_id={doc_id}）")

        matches = {}
        used_old = set()
        old_by_id = {node["id"]: node for node in old_flat if node.get("id")}
        for wanted in new_flat:
            wanted_id = wanted.get("id")
            if wanted_id in old_by_id:
                matches[id(wanted)] = old_by_id[wanted_id]
                used_old.add(id(old_by_id[wanted_id]))

        def expressed_signature(node):
            return (
                node.get("text", ""),
                node.get("note", "") or "",
                bool(node.get("checked", node.get("finish", False))),
            )

        old_by_signature = {}
        new_by_signature = {}
        for node in old_flat:
            if id(node) not in used_old:
                old_by_signature.setdefault(expressed_signature(node), []).append(node)
        for node in new_flat:
            if id(node) not in matches:
                new_by_signature.setdefault(expressed_signature(node), []).append(node)
        for signature, wanted_nodes in new_by_signature.items():
            candidates = old_by_signature.get(signature, [])
            if len(candidates) > 1 or (candidates and len(wanted_nodes) > 1):
                raise MubuError(
                    f"Markdown 无法区分语义相同的重复节点，已中止同步（doc_id={doc_id}）"
                )
            if len(candidates) == 1 and len(wanted_nodes) == 1:
                matches[id(wanted_nodes[0])] = candidates[0]
                used_old.add(id(candidates[0]))

        def counts(nodes, key):
            result = {}
            for node in nodes:
                value = node.get(key)
                if isinstance(value, str) and value:
                    result.setdefault(value, []).append(node)
            return result

        old_by_text = counts(old_flat, "text")
        new_by_text = counts(new_flat, "text")
        for wanted in new_flat:
            if id(wanted) in matches:
                continue
            text = wanted.get("text")
            old_candidates = [node for node in old_by_text.get(text, []) if id(node) not in used_old]
            if len(old_candidates) > 1:
                raise MubuError(
                    f"重复节点无法凭 Markdown 内容唯一匹配，已中止同步（doc_id={doc_id}）"
                )
        for wanted in new_flat:
            if id(wanted) in matches:
                continue
            text = wanted.get("text")
            old_candidates = [node for node in old_by_text.get(text, []) if id(node) not in used_old]
            if text and len(old_candidates) == 1 and len(new_by_text.get(text, [])) == 1:
                matches[id(wanted)] = old_candidates[0]
                used_old.add(id(old_candidates[0]))

        old_paths = node_paths(current_nodes)
        new_paths = node_paths(desired_nodes)

        for wanted in new_flat:
            if id(wanted) in matches:
                continue
            if len(new_paths[id(wanted)]) != 1:
                continue
            path = new_paths[id(wanted)]
            same_path = [node for node in old_flat
                         if id(node) not in used_old and old_paths[id(node)] == path]
            if len(same_path) == 1:
                matches[id(wanted)] = same_path[0]
                used_old.add(id(same_path[0]))

        def merge_node(wanted):
            original = matches.get(id(wanted))
            if original is None:
                merged = copy.deepcopy(wanted)
                if merged.get("id") == "root" or (isinstance(merged.get("id"), str)
                        and merged["id"].startswith("node_") and merged["id"][5:].isdigit()):
                    merged.pop("id", None)
                self._fill_node_defaults(merged)
                normalize_node(merged)
                return merged
            merged = copy.deepcopy(original)
            original_id = original.get("id")
            for key, value in wanted.items():
                if key not in ("id", "children", "checked", "taskStatus"):
                    merged[key] = copy.deepcopy(value)
            merged["id"] = original_id
            if "note" not in wanted and "note" in original:
                merged["note"] = ""
            checked_specified = "checked" in wanted
            status_specified = "taskStatus" in wanted
            if checked_specified or status_specified:
                if checked_specified:
                    status = 2 if bool(wanted["checked"]) else 1
                else:
                    status = wanted["taskStatus"]
                if type(status) is not int or status not in (0, 1, 2):
                    raise MubuError("目标正文包含无效 taskStatus")
                for field, expected in (("finish", False), ("deadline", 0), ("remindAt", 0)):
                    if field in wanted and not self._status_companion_matches(
                            field, wanted[field], expected):
                        raise MubuError(
                            f"taskStatus={status} 的 {field} 必须为 {expected!r}")
                merged["taskStatus"] = status
                merged["finish"] = False
                merged["deadline"] = 0
                merged["remindAt"] = 0
            elif "finish" in original:
                merged["finish"] = False if "checked" not in wanted else bool(wanted["checked"])
            merged.pop("checked", None)
            desired_children = wanted.get("children") or []
            merged["children"] = [merge_node(child) for child in desired_children]
            return merged

        target_nodes = [merge_node(node) for node in desired_nodes]
        target_nodes_flat = flatten(target_nodes)
        if self._nodes_semantically_equal(current_nodes, target_nodes):
            raise MubuError(f"文档内容没有可写 changeset（doc_id={doc_id}）")

        old_node_ids = {node.get("id") for node in old_flat}
        target_groups = []
        def collect_target_groups(nodes, parent_id=None):
            target_groups.append((
                parent_id,
                [node["id"] for node in nodes if node.get("id") in old_node_ids],
            ))
            for node in nodes:
                collect_target_groups(node.get("children") or [], node.get("id"))
        collect_target_groups(target_nodes)
        for parent_id, child_ids in target_groups:
            if parent_id is not None and parent_id not in old_node_ids and child_ids:
                raise MubuError(
                    f"既有节点不能移动到本次新建的父节点内（doc_id={doc_id}, "
                    f"parent_id={parent_id}）；请先单独创建目标父节点"
                )

        working = copy.deepcopy(current_nodes)

        def locate_node(nodes, node_id, prefix=None, parent_id=None):
            for index, node in enumerate(nodes):
                path = (["nodes", index] if prefix is None
                        else prefix + ["children", index])
                if node.get("id") == node_id:
                    return {
                        "parentId": parent_id, "index": index, "path": path,
                        "node": node,
                    }
                found = locate_node(node.get("children") or [], node_id, path, node.get("id"))
                if found is not None:
                    return found
            return None

        for parent_id, child_ids in target_groups:
            for target_index, node_id in enumerate(child_ids):
                source = locate_node(working, node_id)
                if source is None:
                    raise MubuError(
                        f"结构同步期间目标节点已不存在（doc_id={doc_id}, node_id={node_id}）"
                    )
                if source["parentId"] == parent_id and source["index"] == target_index:
                    continue
                if parent_id is None:
                    target_path = ["nodes", target_index]
                else:
                    target_parent = locate_node(working, parent_id)
                    if target_parent is None:
                        raise MubuError(
                            f"结构同步期间目标父节点已不存在（doc_id={doc_id}, "
                            f"parent_id={parent_id}）"
                        )
                    target_path = target_parent["path"] + ["children", target_index]
                moved_node = copy.deepcopy(source["node"])
                original_position = {
                    "parentId": source["parentId"], "index": source["index"],
                    "node": moved_node, "path": source["path"],
                }
                changed_position = {
                    "parentId": parent_id, "index": target_index,
                    "node": copy.deepcopy(moved_node), "path": target_path,
                }
                event = self.build_node_structure_event(
                    original_position, changed_position, working)
                expected = self._apply_node_events(working, [event])
                current_raw = self._write_events_verified(
                    doc_id, [event], expected, version)
                version = current_raw.get("baseVersion")
                if version is None:
                    raise MubuError(f"结构变化后读回缺少 baseVersion（doc_id={doc_id}）")
                working = self._raw_nodes(current_raw, doc_id)

        target_ids = {node.get("id") for node in target_nodes_flat}

        def find_deletable(nodes, prefix=None, parent_id=None):
            for index, node in enumerate(nodes):
                path = (["nodes", index] if prefix is None
                        else prefix + ["children", index])
                if node.get("id") not in target_ids:
                    return {"parentId": parent_id, "index": index,
                            "path": path, "node": node}
                found = find_deletable(node.get("children") or [], path, node.get("id"))
                if found is not None:
                    return found
            return None

        removed_count = 0
        while True:
            position = find_deletable(working)
            if position is None:
                break
            event = {"name": "delete", "deleted": [{
                **{key: copy.deepcopy(value) for key, value in position.items()
                   if key != "node"},
                "node": self._sanitize_payload_node(position["node"]),
            }]}
            expected = self._apply_node_events(working, [event])
            current_raw = self._write_events_verified(doc_id, [event], expected, version)
            version = current_raw.get("baseVersion")
            if version is None:
                raise MubuError(f"删除后读回缺少 baseVersion（doc_id={doc_id}）")
            working = self._raw_nodes(current_raw, doc_id)
            removed_count += 1

        def create_missing(nodes, parent_id=None):
            nonlocal working, version
            for target_index, node in enumerate(nodes):
                node_id = node.get("id")
                if node_id in old_node_ids:
                    create_missing(node.get("children") or [], node_id)
                    continue
                if parent_id is None:
                    path = ["nodes", target_index]
                else:
                    parent = locate_node(working, parent_id)
                    if parent is None:
                        raise MubuError(
                            f"创建期间目标父节点已不存在（doc_id={doc_id}, parent_id={parent_id}）"
                        )
                    path = parent["path"] + ["children", target_index]
                create_node = copy.deepcopy(node)
                create_node["children"] = []
                event = self.build_node_create_event(
                    create_node, path, target_index, parent_id=parent_id)
                expected = self._apply_node_events(working, [event])
                current_raw = self._write_events_verified(doc_id, [event], expected, version)
                version = current_raw.get("baseVersion")
                if version is None:
                    raise MubuError(f"创建后读回缺少 baseVersion（doc_id={doc_id}）")
                working = self._raw_nodes(current_raw, doc_id)
                create_missing(node.get("children") or [], node_id)

        create_missing(target_nodes)

        update_count = 0
        for updated in target_nodes_flat:
            node_id = updated.get("id")
            if node_id not in old_node_ids:
                continue
            position = locate_node(working, node_id)
            if position is None:
                raise MubuError(
                    f"字段同步期间目标节点已不存在（doc_id={doc_id}, node_id={node_id}）")
            original = position["node"]
            original_fields = copy.deepcopy(original)
            updated_fields = copy.deepcopy(updated)
            original_fields.pop("children", None)
            updated_fields.pop("children", None)
            if self._node_semantics(original_fields) == self._node_semantics(updated_fields):
                continue
            event = self.build_node_update_event(position["path"], original, updated)
            if ("taskStatus" in updated and
                    updated.get("taskStatus") in (0, 1, 2) and
                    updated.get("taskStatus") != original.get("taskStatus")):
                event["updated"][0]["updated"].update({
                    "taskStatus": updated["taskStatus"],
                    "finish": False, "deadline": 0, "remindAt": 0,
                })
            expected = self._apply_node_events(working, [event])
            current_raw = self._write_events_verified(doc_id, [event], expected, version)
            version = current_raw.get("baseVersion")
            if version is None:
                raise MubuError(f"更新后读回缺少 baseVersion（doc_id={doc_id}）")
            working = self._raw_nodes(current_raw, doc_id)
            update_count += 1

        return {"updated": update_count,
                 "created": sum(node.get("id") not in old_node_ids
                                 for node in target_nodes_flat),
                 "deleted": removed_count}

    def delete_node(self, doc_id: str, path: List) -> bool:
        """删除一个节点及其整棵子树（真实 delete changeset，真机实测）。

        path 形如 ``["nodes", i]`` 或 ``["nodes", i, "children", j]``。
        真机验证：删顶层节点会**级联删除整棵子树**（临时文档 3 顶层/5 总节点 -> 2 顶层/2 总节点）。
        这是幕布原生能力，属于系统命令层，不放进 methods/。
        """
        raw = self._get_doc_raw(doc_id)
        nodes = self._raw_nodes(raw, doc_id)
        node = self._resolve_path(nodes, path)
        index = path[-1]
        parent_id = None
        if len(path) > 2:
            parent_id = self._resolve_path(nodes, path[:-2]).get("id")
        event = {"name": "delete", "deleted": [
            {"index": index, "parentId": parent_id,
             "node": self._sanitize_payload_node(node),
              "path": list(path)}]}
        expected = copy.deepcopy(nodes)
        self._path_target_list(expected, path).pop(index)
        self._write_events_verified(doc_id, [event], expected, raw.get("baseVersion"))
        return True

    def delete_top_nodes(self, doc_id: str, indices) -> int:
        """删除多个顶层节点（及其子树）；返回成功删除的个数。

        indices 为顶层序号集合；内部按序号**从大到小**删除，避免前序删除导致序号错位。
        """
        raw = self._get_doc_raw(doc_id)
        nodes = self._raw_nodes(raw, doc_id)
        if type(indices) is int:
            index_values = [indices]
        else:
            try:
                index_values = list(indices)
            except TypeError as e:
                raise MubuError("删除顶层节点的 indices 必须是整数索引集合") from e
        if any(type(index) is not int for index in index_values):
            raise MubuError("删除顶层节点的每个 index 都必须是整数")
        if len(index_values) != len(set(index_values)):
            raise MubuError("删除顶层节点的 indices 不能重复")
        if any(index < 0 or index >= len(nodes) for index in index_values):
            raise MubuError(f"删除顶层节点的 index 超出范围（当前节点数 {len(nodes)}）")
        selected = []
        for index in sorted(index_values, reverse=True):
            node_id = nodes[index].get("id")
            if not node_id:
                raise MubuError(f"顶层节点 {index} 缺少 ID，无法安全删除")
            selected.append(node_id)
        if len(selected) != len(set(selected)):
            raise MubuError("初始快照中的目标节点 ID 不唯一，已中止删除")
        version = raw.get("baseVersion")
        if selected and version is None:
            raise MubuError("删除节点需要服务端 baseVersion 前置条件")

        done = 0
        for node_id in selected:
            current_raw = self._get_doc_raw(doc_id)
            if current_raw.get("baseVersion") != version:
                raise MubuError(
                    f"删除期间文档版本发生变化，已中止（doc_id={doc_id}, "
                    f"expected={version}, actual={current_raw.get('baseVersion')}）")
            current_nodes = self._raw_nodes(current_raw, doc_id)
            matches = [index for index, node in enumerate(current_nodes)
                       if node.get("id") == node_id]
            if len(matches) != 1:
                raise MubuError(f"初始删除目标已变化或不存在（doc_id={doc_id}, node_id={node_id}）")
            index = matches[0]
            node = copy.deepcopy(current_nodes[index])
            event = {"name": "delete", "deleted": [{
                "index": index, "parentId": None,
                "node": self._sanitize_payload_node(node),
                "path": ["nodes", index],
            }]}
            expected = copy.deepcopy(current_nodes)
            expected.pop(index)
            after = self._write_events_verified(doc_id, [event], expected, version)
            version = after.get("baseVersion")
            if version is None:
                raise MubuError(f"删除后读回缺少 baseVersion（doc_id={doc_id}）")
            done += 1
        return done

    def set_node_fields(self, doc_id: str, path: List, fields: Dict) -> bool:
        """按 path 修改节点的若干字段（真实 update changeset）；字段全同则不写。

        返回 True=已写入 / False=无变化未发请求。
        """
        if not isinstance(fields, dict):
            raise MubuError("fields 必须是对象")
        protected_fields = {"id", "children", "parentId", "path", "index"}
        forbidden = sorted(protected_fields.intersection(fields))
        if forbidden:
            raise MubuError(
                "set_node_fields 不允许修改节点身份或结构字段："
                + ", ".join(forbidden)
            )
        raw = self._get_doc_raw(doc_id)
        nodes = self._raw_nodes(raw, doc_id)
        node = self._resolve_path(nodes, path)
        if "taskStatus" in fields:
            if len(fields) != 1:
                raise MubuError("taskStatus 更新暂不支持与其他字段合并")
            self._validate_task_status(fields["taskStatus"], "fields.taskStatus")
            self._validate_task_status(node.get("taskStatus", 0), "node.taskStatus")
        if all(node.get(k) == v for k, v in fields.items()):
            return False
        if "taskStatus" in fields:
            old_status = node.get("taskStatus", 0)
            new_status = fields["taskStatus"]
            if (type(old_status) is not int or type(new_status) is not int
                    or (old_status, new_status) not in (
                        (0, 1), (1, 2), (2, 1), (1, 0))):
                raise MubuError(
                    f"没有已确认的 taskStatus 转换协议（{old_status} -> {new_status}）"
                )
            original = {
                "id": node.get("id"), "taskStatus": old_status,
                "deadline": node.get("deadline", 0),
                "remindAt": node.get("remindAt", 0),
            }
            if old_status == 1:
                original["finish"] = bool(node.get("finish", False))
            patch = {
                "id": node.get("id"), "taskStatus": new_status,
                "finish": False, "deadline": 0, "remindAt": 0,
            }
            event = {"name": "update", "updated": [{
                "updated": patch, "original": original, "path": list(path),
            }]}
            updated = copy.deepcopy(node)
            updated.update(patch)
        else:
            updated = copy.deepcopy(node)
            updated.update(fields)
            event = self.build_node_update_event(path, node, updated)
        expected = copy.deepcopy(nodes)
        self._path_target_list(expected, path)[path[-1]] = copy.deepcopy(updated)
        self._write_events_verified(doc_id, [event], expected, raw.get("baseVersion"))
        return True

    def move_node(self, doc_id: str, path: List, to_index: int,
                  to_parent_path: Optional[List] = None) -> bool:
        """Move one node by stable identity using a structureChanged event.

        ``to_index`` is the node's final index among the destination siblings.
        Omitting ``to_parent_path`` moves the node to the document top level;
        to keep a nested node under its current parent, pass that parent path
        explicitly.  A successful write is read back and the node ID is checked
        at its requested final path.
        """
        self._validate_node_path(path, "源节点 path")
        if type(to_index) is not int or to_index < 0:
            raise MubuError("目标 index 必须是非负整数")

        raw = self._get_doc_raw(doc_id)
        nodes = self._raw_nodes(raw, doc_id)
        source_list = self._path_target_list(nodes, path)
        source_index = path[-1]
        if source_index >= len(source_list):
            raise MubuError(f"源节点 path 不存在：{path}")
        source_node = source_list[source_index]
        source_id = source_node.get("id")
        if not isinstance(source_id, str) or not source_id:
            raise MubuError(f"源节点缺少稳定 ID：{path}")

        source_parent_path = list(path[:-2]) if len(path) > 2 else None
        if to_parent_path is None:
            target_parent_path = None
        else:
            self._validate_node_path(to_parent_path, "目标父节点 path")
            target_parent_path = list(to_parent_path)

        if target_parent_path is None:
            target_siblings = nodes
            target_parent_id = None
        else:
            target_parent = self._resolve_path(nodes, target_parent_path)
            target_parent_id = target_parent.get("id")
            if not isinstance(target_parent_id, str) or not target_parent_id:
                raise MubuError(f"目标父节点缺少稳定 ID：{target_parent_path}")
            children = target_parent.get("children")
            if children is None:
                target_siblings = []
            elif isinstance(children, list):
                target_siblings = children
            else:
                raise MubuError(f"目标父节点 children 必须是数组：{target_parent_path}")

        same_parent = source_parent_path == target_parent_path
        if same_parent:
            max_index = len(target_siblings) - 1
        else:
            max_index = len(target_siblings)
        if to_index > max_index:
            raise MubuError(
                f"目标 index 超出范围（当前目标同级节点数 {len(target_siblings)}）")
        if same_parent and to_index == source_index:
            return False

        if target_parent_path is None:
            target_path = ["nodes", to_index]
        else:
            target_path = target_parent_path + ["children", to_index]
        original_position = {
            "parentId": (self._resolve_path(nodes, source_parent_path).get("id")
                          if source_parent_path is not None else None),
            "index": source_index,
            "node": copy.deepcopy(source_node),
            "path": list(path),
        }
        changed_position = {
            "parentId": target_parent_id,
            "index": to_index,
            "node": copy.deepcopy(source_node),
            "path": target_path,
        }
        event = self.build_node_structure_event(
            original_position, changed_position, nodes)
        expected = self._apply_node_events(nodes, [event])
        after = self._write_events_verified(
            doc_id, [event], expected, raw.get("baseVersion"))
        final_nodes = self._raw_nodes(after, doc_id)
        final_node = self._resolve_path(final_nodes, target_path)
        if final_node.get("id") != source_id:
            raise MubuError(
                f"移动后节点身份与目标 path 不一致（doc_id={doc_id}, "
                f"node_id={source_id}, path={target_path}）")
        return True

    def _tos_credentials(self) -> Dict:
        """取 TOS 临时凭证（缓存在内存，STS 有效期约 1 小时，保守缓存 10 分钟）。"""
        cached = getattr(self, "_tos_cred_cache", None)
        if isinstance(cached, dict) and time.time() < cached.get("_expires_at", 0):
            return cached
        data = self._request("GET", "/tos/sts")
        cred = data.get("credentials") if isinstance(data, dict) else None
        if not isinstance(cred, dict) or not cred.get("accessKeyId") \
                or not cred.get("secretAccessKey") or not cred.get("sessionToken"):
            raise MubuError("获取 TOS 临时凭证失败（/tos/sts 响应缺少字段）")
        cred["_expires_at"] = time.time() + 600
        self._tos_cred_cache = cred
        return cred

    def attach_image(self, doc_id: str, path: List, source,
                     width: Optional[int] = None) -> bool:
        """Upload a local PNG/JPEG and attach it to one node.

        The upload endpoint accepts JSON containing only a pure Base64 string.
        The returned ``fileId`` is then stored in the node's ``images`` field;
        ``imageLayouts`` records the resulting image count.
        """
        self._validate_node_path(path, "图片目标节点 path")
        try:
            # CLI 的文件边界必须在 Python API 层再次执行；否则调用方可直接
            # 传入工作目录外的字符串或 Path，绕过 commands.py 的校验。
            if isinstance(source, (str, os.PathLike)):
                source = _safe_local_path(os.fspath(source))
            raw_bytes, original_width, original_height, ext, content_type = \
                read_image_details(source)
            display_width = validate_display_width(width)
        except (MediaError, TypeError, ValueError) as exc:
            raise MubuError(f"图片无效：{exc}") from exc

        raw = self._get_doc_raw(doc_id)
        nodes = self._raw_nodes(raw, doc_id)
        node = self._resolve_path(nodes, path)
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise MubuError(f"图片目标节点缺少稳定 ID：{path}")
        existing_images = node.get("images")
        if existing_images is None:
            existing_images = []
        if not isinstance(existing_images, list) or any(
                not isinstance(image, dict) for image in existing_images):
            raise MubuError(f"节点 images 必须是对象数组：{path}")

        # 走网页端同款 TOS 直传。老通道 /document/upload_img_base64 产出的 key
        # （uuid-userId.jpg）桌面端解析不了，只有 TOS 的 userId_uuid.ext 两端都能渲染。
        credentials = self._tos_credentials()
        file_id = tos.build_object_key(self.user_id, ext)
        status, etag, detail = tos.put_object(file_id, raw_bytes, credentials, content_type)
        if status != 200:
            raise MubuError(f"图片上传失败（TOS HTTP {status}）：{detail}")

        # 注册到「用户图片库」：网页端上传成功后同样会调这一步
        # （TosService.syncRecentImgs -> POST /document/sync_recently_used_img，
        #   body {"imageIdList":[<TOS key>]}）。缺这一步时网页端能显示、
        #   但桌面端解析不到图片（显示为破图）。
        try:
            self._request("POST", "/document/sync_recently_used_img",
                          json={"imageIdList": [file_id]})
        except MubuError as exc:
            raise MubuError(
                "图片已上传，但用户图片库注册失败；为避免写入不可用图片，"
                "节点未修改，请稍后确认注册状态后再重试"
            ) from exc

        image = {
            "id": self._gen_node_id(),
            "uri": file_id,
            "ow": original_width,
            "oh": original_height,
            "w": display_width,
        }
        images = copy.deepcopy(existing_images)
        images.append(image)
        updated = {"id": node_id, "images": images,
                   "imageLayouts": [{"count": len(images)}]}
        updated_node = copy.deepcopy(node)
        updated_node.update(updated)
        event = self.build_node_update_event(path, node, updated_node)
        expected = copy.deepcopy(nodes)
        self._path_target_list(expected, path)[path[-1]].update(
            copy.deepcopy(updated))
        self._write_events_verified(doc_id, [event], expected, raw.get("baseVersion"))
        return True

    def link_nodes(self, doc_id: str, from_path: List, to_path: List,
                   side: str = "right") -> bool:
        """Connect two nodes by appending a linkLines entry to the source."""
        valid_sides = frozenset(("left", "right", "top", "bottom"))
        if type(side) is not str or side not in valid_sides:
            raise MubuError("连接线 side 必须是 left、right、top 或 bottom")
        self._validate_node_path(from_path, "连接线起点 path")
        self._validate_node_path(to_path, "连接线终点 path")
        if from_path == to_path:
            raise MubuError("连接线起点和终点不能是同一节点")

        raw = self._get_doc_raw(doc_id)
        nodes = self._raw_nodes(raw, doc_id)
        source = self._resolve_path(nodes, from_path)
        target = self._resolve_path(nodes, to_path)
        source_id = source.get("id")
        target_id = target.get("id")
        if not isinstance(source_id, str) or not source_id:
            raise MubuError(f"连接线起点缺少稳定 ID：{from_path}")
        if not isinstance(target_id, str) or not target_id:
            raise MubuError(f"连接线终点缺少稳定 ID：{to_path}")
        lines = source.get("linkLines")
        if lines is None:
            lines = []
        if not isinstance(lines, list) or any(not isinstance(line, dict) for line in lines):
            raise MubuError(f"节点 linkLines 必须是对象数组：{from_path}")
        if any(line.get("fromNodeId") == source_id and
               line.get("toNodeId") == target_id for line in lines):
            return False

        link_line = {
            "id": self._gen_node_id(),
            "fromNodeId": source_id,
            "toNodeId": target_id,
            "modified": int(time.time() * 1000),
            "fromAttachment": {"side": side, "t": 0.5},
            "toAttachment": {"side": side, "t": 0.5},
            "controlPoints": [[0, 0], [0, 0]],
        }
        updated = {"id": source_id, "linkLines": copy.deepcopy(lines) + [link_line]}
        updated_node = copy.deepcopy(source)
        updated_node.update(updated)
        event = self.build_node_update_event(from_path, source, updated_node)
        expected = copy.deepcopy(nodes)
        self._path_target_list(expected, from_path)[from_path[-1]].update(
            copy.deepcopy(updated))
        self._write_events_verified(
            doc_id, [event], expected, raw.get("baseVersion"))
        return True

    def update_node_text(self, doc_id: str, path: List, text: str) -> bool:
        """按 path 修改节点文本；返回 True=已写入 / False=文本相同未发请求。"""
        return self.set_node_fields(doc_id, path, {"text": text})

    def append_nodes(self, doc_id: str, texts, index: Optional[int] = None) -> List[str]:
        """在文档顶层追加节点（即一级标题），返回新建节点 id 列表。

        index 缺省 = 追加到末尾；逐个创建，避免尚未验证的批量语义。
        """
        if isinstance(texts, str):
            texts = [texts]
        created_ids: List[str] = []
        for offset, text in enumerate(texts):
            raw = self._get_doc_raw(doc_id)
            nodes = self._raw_nodes(raw, doc_id)
            pos = len(nodes) if index is None else index + offset
            if pos > len(nodes):
                raise MubuError(f"index={pos} 超出范围（当前顶层节点数 {len(nodes)}）")
            node = {"id": self._gen_node_id(), "taskStatus": 0, "text": text,
                    "modified": int(time.time() * 1000), "children": []}
            event = self.build_node_create_event(node, ["nodes", pos], pos)
            expected = copy.deepcopy(nodes)
            expected.insert(pos, copy.deepcopy(node))
            self._write_events_verified(doc_id, [event], expected, raw.get("baseVersion"))
            created_ids.append(node["id"])
        return created_ids

    def set_top_level_texts(self, doc_id: str, texts) -> Dict:
        """把顶层节点（一级标题）文本设为 texts：复用已有节点改写，不足则新建。

        返回 {"updated": n, "created": [ids]}；顶层节点多于 texts 时抛错
        （删除 changeset 尚未逆向，避免静默留下垃圾节点）。
        """
        if isinstance(texts, str):
            texts = [texts]
        raw = self._get_doc_raw(doc_id)
        nodes = json.loads(raw["definition"]).get("nodes", [])
        if len(nodes) > len(texts):
            raise MubuError(
                f"顶层节点数 {len(nodes)} 多于目标 {len(texts)}；删除 changeset 尚未实现，"
                "请先手动删除多余节点或把它们纳入 texts")
        updated = 0
        for i, t in enumerate(texts[:len(nodes)]):
            if self.update_node_text(doc_id, ["nodes", i], t):
                updated += 1
        created = self.append_nodes(doc_id, texts[len(nodes):], index=len(nodes))
        return {"updated": updated, "created": created}

    # 一级标题的业务规则定义在 methods.headings；保留类常量兼容旧调用方。
    HEADING_LEVEL = HEADING_LEVEL
    HEADING_BOLD = HEADING_BOLD
    HEADING_COLOR = HEADING_COLOR

    def style_heading(self, text: str, *, level: int = 1) -> str:
        """按用户标题样式格式化文本；默认仍为一级标题。"""
        return _style_heading(self, text, level=level)

    def append_headings(self, doc_id: str, texts, *, level: int = 1) -> List[str]:
        """追加指定语义层级的标题；业务实现位于 methods.headings。"""
        return _append_headings(self, doc_id, texts, level=level)

    def set_headings(self, doc_id: str, texts, *, level: int = 1) -> Dict:
        """设置指定语义层级的标题；业务实现位于 methods.headings。"""
        return _set_headings(self, doc_id, texts, level=level)

    def save_doc(self, doc_id: str, events: Optional[List[Dict]] = None,
                 version: Optional[int] = None, name: Optional[str] = None,
                 verify: Optional[bool] = None) -> None:
        """公开保存入口；禁止关闭写后读回核验。"""
        if verify is False:
            raise MubuError(
                "save_doc 不允许关闭写后读回核验；内部复合写入使用私有通道"
            )
        return self._save_doc_impl(
            doc_id, events=events, version=version, name=name, verify=verify
        )

    def _save_doc_unverified(self, doc_id: str,
                             events: Optional[List[Dict]] = None,
                             version: Optional[int] = None,
                             name: Optional[str] = None) -> None:
        """内部复合写入通道；调用方必须紧接着执行完整状态核验。"""
        return self._save_doc_impl(
            doc_id, events=events, version=version, name=name,
            verify=False,
        )

    def _save_doc_impl(self, doc_id: str, events: Optional[List[Dict]] = None,
                       version: Optional[int] = None, name: Optional[str] = None,
                       verify: Optional[bool] = None) -> None:
        """通过 colla/events 持久化文档变更（端点已 2026-08-04 真机验证）。

        Args:
            doc_id: 文档 ID
            events: 预构建的 changeset 事件列表；每个元素是形如
                ``{"name": "update", "updated": [{"updated": node, "original": node}]}``
                的字典，或由 ``build_update_event`` 生成。也可为
                ``{"name": "nameChanged", "changed": "新标题"}`` 等。
                None 或空列表会失败关闭，不会伪装成成功写入。
            version: 文档版本号（= get_doc 返回的 ``baseVersion``）。
                不传则自动拉取当前文档以取得版本。
            name: 可选，随本次保存一并改文档名（追加一个 ``nameChanged`` 事件）。
            verify: 对可识别的节点级事件默认自动做写后读回核验；内部复合写入可传
                False 并通过 ``_write_events_verified`` 统一核验期望的完整文档状态。

        真机契约（逆向自网页端 DocEditor chunk ``ts()`` 构建器 + 抓包复核）：
            POST /v3/api/colla/events
            body = {
              "memberId": <colla 会话 id>,
              "type": "CHANGE",
              "version": <baseVersion>,
              "documentId": <doc_id>,
              "events": [ ... ]
            }
        每文档 ``x-reg-entrance`` 必须为 ``https://mubu.com/app/edit/home/<doc_id>``，
        与网页端编辑页一致（经 2026-08-04 抓包复核）。

        ``name`` 参数：显式重命名走独立端点 ``/list/rename_doc``（见 ``rename_doc``），
        不要把它塞进 colla/events 的 ``nameChanged`` 事件——该事件仅用于协同实时同步，
        显式改名会被服务端拒绝 illegal request（已真机验证 2026-08-04）。
        """
        if not isinstance(events, list) or not events or any(
                not isinstance(event, dict) or not event.get("name") for event in events):
            raise MubuError("保存正文需要非空、有效的 changeset 事件")
        # Validate the complete write boundary before fetching a version or
        # making any other network request. This also applies when verify=False.
        self._validate_changeset_payload(events)
        event_keys = {
            "create": "created", "update": "updated", "delete": "deleted",
            "structureChanged": "changed",
        }
        def is_verifiable_event(event):
            name = event.get("name")
            key = event_keys.get(name)
            entries = event.get(key) if key else None
            if not isinstance(entries, list) or not entries:
                return False
            if name == "structureChanged":
                return all(
                    isinstance(entry, dict)
                    and isinstance(entry.get("original"), dict)
                    and isinstance(entry.get("changed"), dict)
                    for entry in entries
                )
            return all(
                isinstance(entry, dict) and isinstance(entry.get("path"), list)
                for entry in entries
            )
        verifiable = all(is_verifiable_event(event) for event in events)
        should_verify = verify is True or (verify is None and verifiable)
        if verify is True and not verifiable:
            raise MubuError("无法对未知事件类型执行语义读回验证")
        raw = None
        if version is None or should_verify:
            raw = self._get_doc_raw(doc_id)
            if version is None:
                version = raw.get("baseVersion")
            elif should_verify and raw.get("baseVersion") != version:
                raise MubuError(
                    f"写入前文档版本已变化（doc_id={doc_id}, expected={version}, "
                    f"actual={raw.get('baseVersion')}）")
        expected_nodes = None
        if should_verify:
            expected_nodes = self._apply_node_events(self._raw_nodes(raw, doc_id), events)
        # save_doc 必须有 member_id（colla 会话 id）。
        # 该值任何 API 都不返回（KNOWN LIMITATION），无法自动获取；缺失时绝不再静默
        # 发空串（空串会被服务端以 code:17 illegal request 拒绝），改为明确报错引导配置。
        if not self.member_id:
            raise MubuError(
                "保存正文需要幕布 colla 成员 ID（member_id）。该值无法经任何 API 自动获取，"
                "请设置环境变量 MUBU_MEMBER_ID 后重试（其值可在浏览器登录 mubu.com 后，"
                "从发往 /colla/events 或 /v3/api 的请求 payload / 网络请求中查到 memberId 字段）。",
                status_code=None,
            )
        payload = {
            "memberId": self.member_id,
            "type": "CHANGE",
            "version": version,
            "documentId": doc_id,
            "events": events,
        }
        # 每文档独立设置 x-reg-entrance（覆盖 _get_headers 的固定值）
        headers = {"x-reg-entrance": f"https://mubu.com/app/edit/home/{doc_id}"}
        self._request(*ENDPOINTS["save_doc"], json=payload, headers=headers)
        if should_verify:
            self._verify_doc_nodes(doc_id, expected_nodes)
        # 改名走独立端点（content 保存与改名是两个正交操作）
        if name:
            self.rename_doc(doc_id, name)

    def delete_folder(self, folder_id: str) -> None:
        """删除文件夹（已真机验证：POST /list/delete_folder，body {"id": ...}）。"""
        self._request(*ENDPOINTS["delete_folder"], json={"id": folder_id})

    def delete_doc(self, doc_id: str) -> None:
        """删除文档（已真机验证：POST /list/delete_doc，body {"id": ...}）。"""
        self._request(*ENDPOINTS["delete_doc"], json={"id": doc_id})

    def delete(self, item_id: str, item_type: str = "folder") -> None:
        """本地软删除（与 CLI 语义一致）：仅将项标记入本地回收站，零网络调用。

        与 CLI `delete` 子命令及下方设计注释一致——仅本地标记，
        云端副本仍在。真实硬删仅保留给 `purge_item`（由 CLI `purge --yes`
        触发）。兼容旧调用；新代码建议直接用 `trash_item` / `purge_item`。
        """
        self.trash_item(item_id, item_type)

    # --------------------------------------------------------------------- #
    # 软删除 / 本地回收站
    # 设计：delete = 软删除（仅本地标记，云端仍在）；restore = 移除标记；
    # purge = 唯一不可逆操作，调用真实删除 API 后移除标记。
    # 回收站仅存元数据快照作为安全网，不作为重建来源。
    # --------------------------------------------------------------------- #
    def _read_trash_unlocked(self) -> Dict[str, Any]:
        """在调用方已经持有回收站锁时读取快照。"""
        if TRASH_FILE.exists():
            _secure_file_permissions(TRASH_FILE)
            try:
                value = json.loads(TRASH_FILE.read_text(encoding="utf-8")) or {}
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise MubuError(
                    f"本地回收站文件损坏，无法安全读取：{TRASH_FILE}；"
                    "请先备份并修复该文件后再继续"
                ) from exc
            if not isinstance(value, dict):
                raise MubuError(
                    f"本地回收站文件格式无效（应为 JSON 对象）：{TRASH_FILE}"
                )
            return value
        return {}

    def _load_trash(self) -> Dict[str, Any]:
        """在回收站文件锁内读取快照，避免与其他进程的原子替换竞争。"""
        with _token_file_lock(TRASH_FILE):
            return self._read_trash_unlocked()

    def _write_trash_unlocked(self, trash: Dict) -> None:
        """在调用方已经持有回收站锁时原子写入快照。"""
        _atomic_replace_text(
            TRASH_FILE, json.dumps(trash, ensure_ascii=False, indent=2))

    def _save_trash(self, trash: Dict) -> None:
        """原子写回收站快照：tmp 与最终文件受保护，整体置于文件锁内。"""
        with _token_file_lock(TRASH_FILE):
            self._write_trash_unlocked(trash)

    def trash_item(self, item_id: str, item_type: str,
                   name: str = "", parent_id: str = "0") -> None:
        """软删除：把项标记进本地回收站，零服务端调用（云端仍在）。"""
        with _token_file_lock(TRASH_FILE):
            trash = self._read_trash_unlocked()
            trash[item_id] = {
                "id": item_id,
                "type": item_type,
                "name": name,
                "parent_id": parent_id,
                "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._write_trash_unlocked(trash)

    def restore_item(self, item_id: str) -> bool:
        """恢复：仅移除本地标记，零服务端调用。成功返回 True，未找到返回 False。"""
        with _token_file_lock(TRASH_FILE):
            trash = self._read_trash_unlocked()
            if item_id in trash:
                trash.pop(item_id)
                self._write_trash_unlocked(trash)
                return True
            return False

    def purge_item(self, item_id: str, item_type: Optional[str] = None) -> None:
        """彻底删除：唯一不可逆操作。

        优先从回收站快照读取 item_type；若回收站记录缺失且调用方未显式
        指定 item_type，**不默认 folder**，而是抛出明确错误要求显式指定，
        杜绝把 doc 当 folder 误删（``/list/delete_folder`` 端点与文档 id 不匹配）。
        调用方须已通过 CLI --yes 守卫确认。
        """
        with _token_file_lock(TRASH_FILE):
            trash = self._read_trash_unlocked()
            item = trash.get(item_id)
            # 优先回收站记录；缺失时回退到调用方显式传入的 item_type
            resolved_type = (item or {}).get("type") if item else item_type
            if resolved_type not in ("doc", "folder"):
                raise MubuError(
                    f"无法确定 {item_id} 的类型以执行彻底删除：回收站记录缺失且未显式"
                    f"指定 --type（doc/folder）。请使用 purge <id> --type <doc|folder> --yes "
                    f"显式指定后再执行，避免误删。"
                )
            if resolved_type == "doc":
                self.delete_doc(item_id)
            else:
                self.delete_folder(item_id)
            if item_id in trash:
                trash.pop(item_id)
                self._write_trash_unlocked(trash)

    def list_trash(self) -> List[Dict]:
        """列出回收站中所有已软删除的项（元数据快照）。"""
        return list(self._load_trash().values())

    def is_trashed(self, item_id: str) -> bool:
        """判断项是否已在本地回收站中。"""
        return item_id in self._load_trash()

    def move(self, item_id: str, target_folder_id: str, item_type: str = "doc") -> None:
        """移动文档/文件夹到其他文件夹。

        真实端点已抓包确认（2026-08-04）：``POST /list/custom/drag``（旧推测的
        ``/list/move`` 真机返回 ``code:17 / illegal request``）。请求体形状：
        ``{"dst": null, "src": [{"type": "doc"|"folder", "id": ...}],
        "folderId": <目标文件夹ID>}``。
        - ``dst``=null 表示追加到目标文件夹末尾（保留原顺序）。
        - ``src`` 为待移动项数组，每项 ``{"type", "id"}``；``type`` 支持 ``"doc"``
          / ``"folder"``。
        - ``folderId`` 为目标文件夹 ID（根目录用 ``"0"``）。
        """
        self._request(*ENDPOINTS["move"], json={
            "dst": None,
            "src": [{"type": item_type, "id": item_id}],
            "folderId": target_folder_id,
        })

    def rename_doc(self, doc_id: str, new_name: str) -> None:
        """重命名文档（真实端点 POST /list/rename_doc，2026-08-04 抓包复核）。

        网页端重命名走独立 rename API，而非 colla/events 的 ``nameChanged`` 事件——
        后者仅用于协同实时同步，显式重命名会被服务端拒绝 ``illegal request``
        （已真机验证）。请求体形状：``{"documentId": <doc_id>, "name": <新名>}``
        （注意字段是 ``documentId`` 不是 ``id``；``id`` 会返回 code 5 参数错误）。
        成功响应为 ``{"version": <时间戳>}``。
        """
        self._request(*ENDPOINTS["rename_doc"], json={"documentId": doc_id, "name": new_name})

    def rename_folder(self, folder_id: str, new_name: str) -> None:
        """重命名文件夹（已真机验证：POST /list/rename_folder）。

        实测幕布要求同时携带 ``id`` 与 ``folderId``，且 ``folderId`` 必须填文件夹
        **自身真实 id**（不能填根目录魔法值 ``"0"``，否则返回 code 5）。``name`` 为新名称。
        """
        self._request("POST", "/list/rename_folder", json={
            "id": folder_id,
            "name": new_name,
            "folderId": folder_id,
        })

    def export_tree(self, root_folder_id: str = "0", output_dir: str = ".",
                    max_depth: int = MAX_SEARCH_DEPTH) -> Dict[str, int]:
        """递归导出整个文件夹树为嵌套 Markdown 文件。

        文档写为 ``<name>.md``，子文件夹创建为同级子目录并继续递归。
        单个文件夹/文档拉取失败不阻断整体遍历（记入 errors 统计）。

        Returns:
            {"docs": 导出文档数, "folders": 创建文件夹数, "errors": 失败数}
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        output_root = output_path.resolve()
        stats: Dict[str, int] = {"docs": 0, "folders": 0, "errors": 0}

        def require_contained(path: Path) -> Path:
            # Names are normally sanitized before this point.  Check both
            # separator styles anyway: a faulty extension or a future caller
            # must not turn ``..\\escape`` into a literal safe filename on a
            # POSIX host and bypass the containment check.
            path_parts = [part for part in str(path).replace("\\", "/").split("/")
                          if part not in ("", ".")]
            if ".." in path_parts:
                raise MubuError(f"拒绝导出到输出目录之外的路径: {path}")
            resolved = path.resolve()
            try:
                common = Path(os.path.commonpath((str(output_root), str(resolved))))
            except ValueError as e:
                raise MubuError(f"拒绝导出到输出目录之外的路径: {path}") from e
            if os.path.normcase(str(common)) != os.path.normcase(str(output_root)):
                raise MubuError(f"拒绝导出到输出目录之外的路径: {path}")
            return resolved

        def allocate_path(name: str, parent: Path, used_names: set,
                          is_document: bool) -> Tuple[str, Path]:
            while True:
                safe_name = _unique_filename(name, used_names=used_names)
                component = f"{safe_name}.md" if is_document else safe_name
                final_path = require_contained(parent / component)
                used_names.add(safe_name)
                if not final_path.exists():
                    return safe_name, final_path

        def walk(folder_id: str, current_dir: Path, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                current_dir = require_contained(current_dir)
            except MubuError as e:
                logger.warning("拒绝遍历输出目录之外的路径: %s", e)
                stats["errors"] += 1
                return
            try:
                data = self.get_list(folder_id)
            except MubuError as e:
                logger.warning("导出遍历文件夹 %s 失败: %s", folder_id, e)
                stats["errors"] += 1
                return
            folders = data.get("folders", []) or []
            docs = data.get("documents") or data.get("docs") or []
            try:
                entries = list(current_dir.iterdir())
            except OSError as e:
                logger.warning("读取导出目录 %s 失败: %s", current_dir, e)
                stats["errors"] += 1
                return
            # Files and directories occupy different namespaces (``note`` and
            # ``note.md`` can coexist), but each namespace is compared with
            # Windows case-insensitive semantics so exports are portable.
            used_doc_names = {
                entry.stem for entry in entries
                if entry.is_file() and entry.suffix.casefold() == ".md"
            }
            used_folder_names = {entry.name for entry in entries if entry.is_dir()}
            for d in docs:
                doc_id = d.get("id")
                name = (d.get("name") or "untitled").strip()
                try:
                    doc = self.get_doc(doc_id)
                    md = export_markdown(doc)
                    while True:
                        _, final_path = allocate_path(
                            name, current_dir, used_doc_names, True)
                        try:
                            with final_path.open("x", encoding="utf-8") as output:
                                output.write(md)
                            break
                        except FileExistsError:
                            used_doc_names.add(final_path.stem)
                    stats["docs"] += 1
                except (MubuError, OSError) as e:
                    logger.warning("导出文档 %s 失败: %s", doc_id, e)
                    stats["errors"] += 1
            for f in folders:
                fid = f.get("id")
                fname = (f.get("name") or "untitled").strip()
                try:
                    while True:
                        _, child_dir = allocate_path(
                            fname, current_dir, used_folder_names, False)
                        try:
                            child_dir.mkdir(exist_ok=False)
                            break
                        except FileExistsError:
                            used_folder_names.add(child_dir.name)
                    require_contained(child_dir)
                    stats["folders"] += 1
                    walk(fid, child_dir, depth + 1)
                except (MubuError, OSError) as e:
                    logger.warning("导出文件夹 %s 失败: %s", fid, e)
                    stats["errors"] += 1

        walk(root_folder_id, output_path, 0)
        return stats

    def search(self, keyword: str, root_folder_id: str = "0",
               max_depth: int = MAX_SEARCH_DEPTH,
               limit: int = MAX_SEARCH_LIMIT,
               max_requests: int = MAX_SEARCH_REQUESTS,
               include_trashed: bool = False,
               include_content: bool = False) -> Dict[str, Any]:
        """本地递归搜索：名称包含关键字的文档与文件夹。

        mubu 无公开 /search 端点，从根文件夹开始递归遍历所有子文件夹，
        收集 name 包含 keyword（大小写不敏感）的条目。

        当 ``include_content=True`` 时，对名称未命中的文档额外拉取其正文
        （get_doc）并递归搜索节点 text/note 是否包含 keyword；命中内容的条目
        带 ``matched_in: "content"`` 字段，名称命中的为 ``matched_in: "name"``。
        该选项会额外发起 get_doc 请求，默认关闭以保留性能。

        为保护调用方，到达以下任一上限即停止遍历并标记 truncated=True
        （不再静默丢失信息，调用方据此知晓结果可能不完整）：
        - max_depth: 递归深度上限（根 depth=0，默认 3 即最多展开 4 层）
        - limit: 返回结果总数上限
        - max_requests: 整个搜索的 get_list 请求数硬上限

        环检测：已访问的 folder_id 进入 visited 集合，遇到重复引用直接跳过，
        防止幕布返回环引用时无限递归（max_requests 之上的第二道防线）。

        Args:
            keyword: 搜索关键字（大小写不敏感）
            root_folder_id: 遍历起点文件夹 ID，默认 "0"（根）
            max_depth: 递归深度上限
            limit: 返回结果总数上限
            max_requests: 搜索总请求数硬上限

        Returns:
            字典 {"results": [...], "truncated": bool, "limit": int, "max_depth": int}
            - results: 匹配项列表，每项含 id / name / type（"doc" | "folder"）
              / path（从根起的路径）
            - truncated: 是否因达到上限而提前结束（结果可能不完整）
        """
        keyword_lower = (keyword or "").lower()
        results: List[Dict[str, Any]] = []
        req_count = 0
        truncated = False
        failures: List[Dict[str, str]] = []
        visited: set = set()  # 已访问 folder_id，防环引用无限递归
        # 软删除过滤集：含 include_trashed 时不加载（即不过滤）
        trash = self._load_trash() if not include_trashed else {}

        def walk(folder_id: str, path: str, depth: int) -> None:
            nonlocal req_count, truncated
            if folder_id in visited:
                return  # 已访问，去重（防环）
            visited.add(folder_id)
            if truncated or depth > max_depth or req_count >= max_requests:
                if depth > max_depth or req_count >= max_requests:
                    truncated = True
                return
            try:
                data = self.get_list(folder_id, include_trashed=include_trashed)
            except MubuError as e:
                # 单个文件夹拉取失败不阻断整体遍历，但必须把结果标记为部分结果。
                logger.warning("遍历文件夹 %s 失败: %s", folder_id, e)
                failures.append({"type": "folder", "id": str(folder_id),
                                 "error": str(e)})
                return
            req_count += 1
            if len(results) >= limit:
                truncated = True
                return
            folders = data.get("folders", []) or []
            docs = data.get("documents") or data.get("docs") or []
            for d in docs:
                doc_id = d.get("id")
                # 软删除项：除非显式 include_trashed，否则跳过
                if not include_trashed and doc_id in trash:
                    continue
                name = d.get("name") or ""
                name_matched = bool(keyword_lower and keyword_lower in name.lower())
                if name_matched:
                    results.append({"id": doc_id, "name": name, "type": "doc",
                                    "path": path, "matched_in": "name"})
                    if len(results) >= limit:
                        truncated = True
                        return
                    continue
                # include_content：名称未命中时，拉取正文递归搜索节点 text/note
                if include_content and keyword_lower:
                    try:
                        doc = self.get_doc(doc_id)
                    except MubuError as exc:
                        failures.append({"type": "document", "id": str(doc_id),
                                         "error": str(exc)})
                        continue
                    if self._keyword_in_nodes(doc.get("nodes", []), keyword_lower):
                        results.append({"id": doc_id, "name": name, "type": "doc",
                                        "path": path, "matched_in": "content"})
                        if len(results) >= limit:
                            truncated = True
                            return
            for f in folders:
                fid = f.get("id")
                # 软删除项：除非显式 include_trashed，否则跳过
                if not include_trashed and fid in trash:
                    continue
                name = f.get("name") or ""
                if keyword_lower and keyword_lower in name.lower():
                    results.append({"id": fid, "name": name, "type": "folder", "path": path})
                    if len(results) >= limit:
                        truncated = True
                        return
                child_path = f"{path}/{name}" if path else name
                walk(fid, child_path, depth + 1)

        walk(root_folder_id, "", 0)
        return {
            "results": results,
            "truncated": truncated,
            "limit": limit,
            "max_depth": max_depth,
            "partial": bool(failures),
            "errors": failures,
        }

    def _keyword_in_nodes(self, nodes: Any, keyword_lower: str) -> bool:
        """递归检查节点 text/note 是否包含关键字（大小写不敏感）。

        用于 search(include_content=True) 对文档正文做内容级匹配。
        """
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            text = (node.get("text") or "")
            note = (node.get("note") or "")
            if keyword_lower in (text + " " + note).lower():
                return True
            if self._keyword_in_nodes(node.get("children", []), keyword_lower):
                return True
        return False
