"""火山引擎 TOS 直传 —— 幕布图片上传的**真实通道**（逆向自网页端 app.js）。

网页端上传图片的完整流程：

1. ``GET /v3/api/tos/sts``  取临时凭证（accessKeyId / secretAccessKey / sessionToken）
2. ``PUT https://mubu-img.tos-cn-shanghai.volces.com/document_image/<userId>_<uuid>.<ext>``
   —— TOS4-HMAC-SHA256 签名
3. ``POST /v3/api/document/sync_recently_used_img`` 注册到「用户图片库」

⚠️ 为什么要走这条路：老通道 ``POST /document/upload_img_base64`` 产出的 key 形如
``document_image/<uuid>-<userId>.jpg``（uuid 在前、且被服务端转成 jpg），
**桌面端解析不了这张图（显示破图）**；只有 TOS 直传的
``document_image/<userId>_<uuid>.<ext>`` 网页端与桌面端都能正常渲染。

⚠️ TOS4 与 AWS SigV4 的**唯一区别**：密钥派生**不加 "TOS4" 前缀**
    kDate = HMAC(secretAccessKey, dateStamp)        ← AWS 是 HMAC("AWS4"+secret, ...)
"""
import datetime
import hashlib
import hmac
import uuid
from typing import Dict, Optional, Tuple
from urllib.parse import quote

import requests

TOS_ENDPOINT = "tos-cn-shanghai.volces.com"
TOS_REGION = "cn-shanghai"
TOS_SERVICE = "tos"
TOS_BUCKET = "mubu-img"
TOS_PREFIX = "document_image"
TOS_ALGORITHM = "TOS4-HMAC-SHA256"


def _hmac(key: bytes, msg) -> bytes:
    if isinstance(msg, str):
        msg = msg.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).digest()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_object_key(user_id, ext: str, prefix: str = TOS_PREFIX) -> str:
    """复刻网页端 generateKey()：``<prefix>/<userId>_<uuid>.<ext>``。"""
    clean = (ext or "").lstrip(".").lower() or "png"
    return f"{prefix}/{user_id}_{uuid.uuid4()}.{clean}"


def build_put_headers(key: str, body: bytes, credentials: Dict,
                      content_type: Optional[str] = None,
                      now: Optional[datetime.datetime] = None) -> Dict[str, str]:
    """构造 TOS PUT 请求头（含 TOS4-HMAC-SHA256 签名）。"""
    dt = now or datetime.datetime.now(datetime.timezone.utc)
    x_tos_date = dt.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = dt.strftime("%Y%m%d")
    host = f"{TOS_BUCKET}.{TOS_ENDPOINT}"
    payload_hash = _sha256_hex(body)

    signed_map = {
        "host": host,
        "x-tos-date": x_tos_date,
        "x-tos-content-sha256": payload_hash,
        "x-tos-security-token": credentials["sessionToken"],
    }
    if content_type:
        signed_map["content-type"] = content_type

    signed_headers = ";".join(sorted(signed_map))
    canonical_headers = "".join(f"{k}:{signed_map[k]}\n" for k in sorted(signed_map))
    canonical_request = "\n".join([
        "PUT",
        "/" + quote(key, safe="/~"),
        "",
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    scope = f"{date_stamp}/{TOS_REGION}/{TOS_SERVICE}/request"
    string_to_sign = "\n".join([
        TOS_ALGORITHM,
        x_tos_date,
        scope,
        _sha256_hex(canonical_request.encode("utf-8")),
    ])

    signing_key = _hmac(str(credentials["secretAccessKey"]).encode("utf-8"), date_stamp)
    for part in (TOS_REGION, TOS_SERVICE, "request"):
        signing_key = _hmac(signing_key, part)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    headers = {
        "Host": host,
        "x-tos-date": x_tos_date,
        "x-tos-content-sha256": payload_hash,
        "x-tos-security-token": credentials["sessionToken"],
        "Authorization": (
            f"{TOS_ALGORITHM} Credential={credentials['accessKeyId']}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"),
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def put_object(key: str, body: bytes, credentials: Dict,
               content_type: Optional[str] = None,
               timeout: int = 60) -> Tuple[int, Optional[str], str]:
    """直传一个对象到 TOS；返回 ``(状态码, ETag, 响应文本片段)``。"""
    url = f"https://{TOS_BUCKET}.{TOS_ENDPOINT}/{key}"
    resp = requests.put(
        url, data=body,
        headers=build_put_headers(key, body, credentials, content_type),
        timeout=timeout, allow_redirects=False)
    return resp.status_code, resp.headers.get("ETag"), resp.text[:200]
