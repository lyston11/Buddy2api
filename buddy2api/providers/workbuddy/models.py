"""WorkBuddy supplier-list fetch.

Official desktop CloudAgentService.listAvailableModels:
GET /v2/enterprises/personal/models, unwrap json.data ?? json, then data.models.
Skip rows whose tags include text-to-image.
"""

from __future__ import annotations

import re

import httpx

import buddy2api.auth_manager as auth_manager
from buddy2api.catalog import description_text
from buddy2api.upstream_transport import client_scope
from buddy2api.model_capacity import capacity_fields
from buddy2api.model_reasoning import reasoning_fields

MODELS_PATH = "/v2/enterprises/personal/models"
_NON_CHAT_TAGS = frozenset({"text-to-image"})


class WorkBuddyModelsError(ValueError):
    """Supplier model list request failed."""


def parse_supplier_models(payload) -> list[dict]:
    models: list[dict] = []
    seen: set[str] = set()
    for row in _model_rows(payload):
        if not isinstance(row, dict):
            continue
        mid = row.get("id")
        if not isinstance(mid, str):
            continue
        mid = mid.strip()
        if not mid or mid in seen:
            continue
        if _has_non_chat_tag(row):
            continue
        seen.add(mid)
        name = str(row.get("name") or row.get("display_name") or mid)
        item = {"id": mid, "name": name or mid}
        item.update(capacity_fields(row))
        item.update(reasoning_fields(row))
        # 官方说明在 descriptionZh / descriptionEn 里（如「已更新至 V4.1-Flash-1017
        # 最新版」），只认 description 会把版本信息整个丢掉。
        description = description_text(row)
        if description:
            item["description"] = description
        # 官方客户端模型选择器里的「x0.79 / Free now」就来自这个字段：上游给出的是
        # "x0.79 credits" / "x0.00" 这样的字符串，不解析的话目录页就只剩模型名了。
        rate = _parse_credits(row.get("credits"))
        if rate is not None:
            item["credit_rate"] = rate
        models.append(item)
    return models


def _parse_credits(value) -> float | None:
    """"x0.79 credits" / "x0.00" → 0.79；解析不了返回 None。

    用正则而不是 strip("x")：容忉空格、大小写与未来可能出现的其它后缀。
    """
    match = re.search(r"x\s*([0-9]+(?:\.[0-9]+)?)", str(value or ""), re.IGNORECASE)
    if not match:
        return None
    try:
        return round(float(match.group(1)), 4)
    except ValueError:
        return None


def _model_rows(payload) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), (dict, list)) else payload
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        rows = data.get("models")
        if isinstance(rows, list):
            return rows
    rows = payload.get("models")
    return rows if isinstance(rows, list) else []


def _has_non_chat_tag(row: dict) -> bool:
    tags = row.get("tags")
    if not isinstance(tags, list):
        return False
    return any(str(tag) in _NON_CHAT_TAGS for tag in tags)


async def fetch_supplier_models(account: dict) -> list[dict]:
    headers = await auth_manager.get_billing_headers(account)
    if not headers:
        raise WorkBuddyModelsError("no usable WorkBuddy token")
    headers = dict(headers)
    # 头名按官方客户端形态写小写（见 fingerprint.py），这里也用小写键
    headers.pop("content-type", None)
    headers["accept"] = "application/json"
    url = f"{auth_manager.backend_url_for(account)}{MODELS_PATH}"
    async with client_scope(account, 30.0) as client:
        response = await client.get(url, headers=headers)
    if response.status_code >= 400:
        raise WorkBuddyModelsError(f"models HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise WorkBuddyModelsError("models response is not JSON") from exc
    if isinstance(payload, dict):
        code = payload.get("code")
        if code not in (None, 0):
            raise WorkBuddyModelsError(str(payload.get("msg") or payload.get("message") or code))
    return parse_supplier_models(payload)
