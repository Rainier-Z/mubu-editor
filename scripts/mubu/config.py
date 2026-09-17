"""mubu 包 — 配置、常量、日志、异常与路径安全基础设施。"""
import errno
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any, Iterator, Mapping, Optional, Tuple
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

# --------------------------------------------------------------------------- #
# 日志（P1 #16）：用 logging 取代散落的 print；warning/error 分级，
# --verbose 控制 debug；敏感内容（密码 / token）绝不进日志。
# --------------------------------------------------------------------------- #
logger = logging.getLogger("mubu_api")
logger.propagate = False  # 不外传至 root，避免重复输出
if not logger.handlers:
    # 默认 stderr 处理器（导入期或尚未经 main() 配置时也能输出 warning/error）
    _default_handler = logging.StreamHandler(sys.stderr)
    _default_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(_default_handler)
logger.setLevel(logging.WARNING)

# API 基础配置
# 默认 base URL；允许通过环境变量 MUBU_BASE_URL 覆盖，但仅限 mubu.com 家族域名，
# 防止指向恶意服务器造成 MITM / 凭据泄漏。
DEFAULT_BASE_URL = "https://api2.mubu.com/v3/api"
ALLOWED_BASE_HOSTS = ("api2.mubu.com", "api.mubu.com", "mubu.com")


def _resolve_base_url() -> str:
    """解析并校验 MUBU_BASE_URL；不安全覆盖会被忽略并告警。"""
    env_url = os.getenv("MUBU_BASE_URL")
    if not env_url:
        return DEFAULT_BASE_URL

    valid = False
    try:
        parsed = urlparse(env_url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
        valid = (
            parsed.scheme == "https"
            and host in ALLOWED_BASE_HOSTS
            and port in (None, 443)
            and not parsed.username
            and not parsed.password
            and "@" not in parsed.netloc
            and not parsed.netloc.endswith(":")
            and not parsed.query
            and not parsed.fragment
            and not any(ord(char) < 32 for char in env_url)
            and env_url == env_url.strip()
        )
    except ValueError:
        pass
    if valid:
        return env_url.rstrip("/")
    logger.warning(
        "MUBU_BASE_URL 未通过 HTTPS、主机名、端口和 URL 结构校验，"
        "已忽略并使用默认地址 %s",
        DEFAULT_BASE_URL,
    )
    return DEFAULT_BASE_URL


BASE_URL = _resolve_base_url()
# --------------------------------------------------------------------------- #
# 配置路径：先选择一个默认来源，随后才应用逐文件环境变量覆盖。
# 如果项目 config/ 中任一已知数据文件存在，全部默认路径都留在项目目录；
# 否则若发现旧文件则整体沿用旧布局，避免某个文件单独从另一代目录回退。
# MUBU_CONFIG_DIR 和各 MUBU_*_FILE 是显式选择，优先于任何自动迁移判断。
# --------------------------------------------------------------------------- #
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


def _resolve_config_paths(
    workspace_root: Path,
    home: Path,
    environ: Mapping[str, str],
) -> Tuple[Path, Path, Path, Path]:
    """返回 (config dir, token, trash, env) 并保持默认来源一致。"""
    explicit_config_dir = environ.get("MUBU_CONFIG_DIR")
    project_dir = Path(explicit_config_dir) if explicit_config_dir else workspace_root / "config"
    legacy_dir = home / ".workbuddy"

    if explicit_config_dir:
        config_dir = project_dir
        use_legacy = False
    else:
        project_files = (
            project_dir / ".env.mubu",
            project_dir / ".mubu_token",
            project_dir / ".mubu_trash.json",
        )
        legacy_files = (
            home / ".mubu_token",
            legacy_dir / ".env.mubu",
            legacy_dir / ".mubu_trash.json",
        )
        if any(path.exists() for path in project_files):
            config_dir = project_dir
            use_legacy = False
        elif any(path.exists() for path in legacy_files):
            config_dir = legacy_dir
            use_legacy = True
        else:
            config_dir = project_dir
            use_legacy = False

    token_default = home / ".mubu_token" if use_legacy else config_dir / ".mubu_token"
    trash_default = config_dir / ".mubu_trash.json"
    env_default = config_dir / ".env.mubu"
    token_file = Path(environ["MUBU_TOKEN_FILE"]) if environ.get("MUBU_TOKEN_FILE") else token_default
    trash_file = Path(environ["MUBU_TRASH_FILE"]) if environ.get("MUBU_TRASH_FILE") else trash_default
    env_file = Path(environ["MUBU_ENV_FILE"]) if environ.get("MUBU_ENV_FILE") else env_default
    return config_dir, token_file, trash_file, env_file


CONFIG_DIR, TOKEN_FILE, TRASH_FILE, ENV_FILE = _resolve_config_paths(
    WORKSPACE_ROOT,
    Path.home(),
    os.environ,
)
try:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

# 本地回收站（软删除）快照文件：仅记录元数据（id / type / name / parent_id /
# deleted_at），作为「云端仍在、可恢复」的安全网；永不作为重建来源。
# 软删除（delete）→ 仅写入此文件、零服务端调用；restore → 仅移除标记；
# purge → 调用真实删除 API 后移除标记（唯一不可逆操作）。
# .env 凭据文件路径：仅当环境变量未设置时用于补全 MUBU_PHONE / MUBU_PASSWORD

# 默认请求头
DEFAULT_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://mubu.com",
    "Referer": "https://mubu.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 接口路径常量：统一在此维护，便于后续集中修改。
# 每个端点为 (HTTP 方法, 路径) 二元组；调用处用 self._request(*ENDPOINTS["key"], ...) 解包。
ENDPOINTS = {
    "login": ("POST", "/user/phone_login"),
    "list": ("POST", "/list/get"),
    "create_folder": ("POST", "/list/create_folder"),
    "create_doc": ("POST", "/list/create_doc"),
    "get_doc": ("POST", "/document/edit/get"),
    # save_doc 从已废弃的 /doc/save（服务端签名校验
    # 一律返回 code:17 illegal request）切换至网页端真实写接口 /v3/api/colla/events。
    # 端点形态与请求体逆向自网页端 DocEditor chunk（抓包复核）。
    "save_doc": ("POST", "/colla/events"),
    # 文档重命名走独立端点 /list/rename_doc（与 /list/rename_folder
    # 同族）。注意：不可把 nameChanged 事件塞进 colla/events —— 该事件仅用于协同实时
    # 同步，显式重命名会被服务端拒绝 illegal request（已真机验证）。
    "rename_doc": ("POST", "/list/rename_doc"),
    # 真机验证（2026-07-15）：删除必须区分类型，且端点为 delete_folder / delete_doc，
    # 原推测的 /list/delete 实测返回 code 17 illegal request。
    "delete_folder": ("POST", "/list/delete_folder"),
    "delete_doc": ("POST", "/list/delete_doc"),
    # move 端点已抓包确认（2026-08-04）：真实为 /list/custom/drag，旧推测的
    # /list/move 实测返回 code 17 illegal request。body 见 client.move()。
    "move": ("POST", "/list/custom/drag"),
}

# 网络重试配置
# 单次请求超时（秒）
REQUEST_TIMEOUT = 15
# 网络层/5xx 最大重试次数（不含首次，即最多共发起 3 次请求）
MAX_NETWORK_RETRIES = 2
# 指数退避时间（秒）：第 1 次重试等 1s，第 2 次等 2s
NETWORK_BACKOFF = (1, 2)
# Token 文件权限：仅属主可读写
TOKEN_FILE_MODE = 0o600
# 异常 body 截断长度，避免超大响应体污染错误信息
BODY_TRUNCATE = 200


def _current_windows_user_sid() -> str:
    """Return the SID of the current process token without account-name lookup."""
    import ctypes
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("User", SidAndAttributes)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if not required.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token, 1, buffer, required.value, ctypes.byref(required)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(token_user.User.Sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return sid_text.value
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def _set_windows_file_acl(path: Path) -> None:
    """Replace a file DACL with protected Full Control ACEs for user and SYSTEM."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p

    user_sid = _current_windows_user_sid()
    sddl = f"D:P(A;;FA;;;SY)(A;;FA;;;{user_sid})"
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        dacl_and_protected = 0x00000004 | 0x80000000
        if not advapi32.SetFileSecurityW(str(path), dacl_and_protected, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.LocalFree(descriptor)


def _secure_file_permissions(path: Path) -> None:
    """Restrict a sensitive file to the current user and SYSTEM (or POSIX 0600)."""
    path = Path(path)
    if os.name == "nt":
        _set_windows_file_acl(path)
    else:
        os.chmod(path, TOKEN_FILE_MODE)

# 本地搜索（search）限制配置
# 根文件夹 depth=0，默认 3 即最多展开 4 层
MAX_SEARCH_DEPTH = 3
# 返回结果总数上限（单轮 search 命中条目硬上限，达到即静默截断）
MAX_SEARCH_LIMIT = 50
# 整个搜索的 HTTP 请求数硬上限（get_list 调用次数）
MAX_SEARCH_REQUESTS = 200


# 配置文件跨进程 advisory 锁：Unix 用 fcntl.flock，Windows 用 msvcrt byte-range lock。
# Windows 的 msvcrt 非阻塞锁没有内建超时；限制轮询时间，避免持锁进程异常时 CLI
# 永久挂起。该常量可由测试替换，生产环境使用固定的短等待上限。
WINDOWS_LOCK_TIMEOUT_SECONDS = 10.0
@contextmanager
def _token_file_lock(path: Optional[Path] = None) -> Iterator[None]:
    """对指定配置数据文件加跨进程排他锁。

    锁文件位于数据文件同目录，名称为 ``<name>.lock``。Windows 上始终锁定
    第 0 字节；若锁被其他进程持有，则短暂轮询，直至取得锁或遇到非锁冲突错误。
    如果运行平台没有可用的跨进程锁实现，则失败关闭，不静默降级为无锁写入。
    """
    target = Path(path) if path is not None else TOKEN_FILE
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+b")
    lock_kind = None
    try:
        # Persistent lock files can inherit access for unrelated local accounts.
        # Secure the lock before creating bytes or entering the critical section.
        _secure_file_permissions(lock_path)
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            lock_kind = "fcntl"
        elif msvcrt is not None:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(b"\0")
                f.flush()
            f.seek(0)
            retryable_lock_errors = {errno.EACCES, errno.EDEADLK}
            deadlock_code = getattr(errno, "EDEADLOCK", None)
            if deadlock_code is not None:
                retryable_lock_errors.add(deadlock_code)
            deadline = time.monotonic() + WINDOWS_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    lock_kind = "msvcrt"
                    break
                except OSError as e:
                    if e.errno not in retryable_lock_errors:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MubuError(
                            f"获取配置文件锁超时（{lock_path}，等待 "
                            f"{WINDOWS_LOCK_TIMEOUT_SECONDS:g} 秒）"
                        ) from e
                    time.sleep(min(0.05, remaining))
        else:
            raise RuntimeError("此平台没有可用的跨进程文件锁实现")
        yield
    finally:
        if lock_kind == "fcntl":
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        elif lock_kind == "msvcrt":
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        f.close()


class MubuError(Exception):
    """幕布 API 基础异常。

    基础结构包含 msg / status_code / body。
    body 自动截断前 BODY_TRUNCATE 字，
    避免超大响应体（如限流 HTML 页面）撑爆错误信息。
    请勿在此重复定义完整字段以外的内容。
    """

    def __init__(self, msg: str, status_code: Optional[int] = None, body: Any = None) -> None:
        super().__init__(msg)
        self.msg = msg
        self.status_code = status_code
        # body 若为字符串则截断，避免非 JSON / 错误页面撑爆异常信息
        if isinstance(body, str) and len(body) > BODY_TRUNCATE:
            body = body[:BODY_TRUNCATE]
        self.body = body


def _safe_local_path(path: str) -> Path:
    """校验并解析本地文件路径，仅允许当前工作目录或其子目录。

    拒绝绝对路径、``..`` 越界路径、以及跳出当前工作目录的路径，防止
    ``create --md`` / ``save --file`` 读取 ``/etc/passwd``、``~/.ssh/id_rsa``
    等任意文件并外发。校验失败抛 MubuError（清晰错误，而非原始栈）。
    """
    # 0) 展开 ~ 为用户目录（如 ~/.ssh/id_rsa → /Users/.../.ssh/id_rsa），
    #    展开后若为绝对路径将在下一步被明确拒绝，避免被静默解析为 cwd 下文件。
    path = os.path.expanduser(path)
    # 1) 拒绝越界片段（.. 跳出目录层级）
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise MubuError(f"拒绝越界路径（包含 '..'）: {path}")
    # 2) 在任意宿主系统上都拒绝绝对路径和 Windows 驱动器路径。CI 的
    # Linux runner 也必须识别 ``C:\\...`` / UNC，不能把它们当作 cwd 下的
    # 普通文件名。
    windows_path = PureWindowsPath(path)
    if os.path.isabs(path) or windows_path.is_absolute() or windows_path.drive:
        raise MubuError(f"拒绝读取绝对路径（可能越权访问系统文件）: {path}")
    # 3) 解析后的真实路径必须位于当前工作目录内（含其自身，symlink 已被 realpath 展开）
    resolved = os.path.realpath(path)
    cwd = os.path.realpath(os.getcwd())
    if resolved != cwd and not resolved.startswith(cwd + os.sep):
        raise MubuError(f"拒绝访问允许目录（当前工作目录）之外的文件: {path}")
    return Path(resolved)
