"""Per-channel supplier model catalogs.

Fetch+parse of each source's list is separate from persist and from chat I/O.
Live WorkBuddy lists are also copied to the legacy `models` setting so older
readers stay in sync. Manual extras work the same way on every channel.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Awaitable, Callable

import buddy2api.database as db
import buddy2api.sites as sites
from buddy2api import auth_manager
from buddy2api.model_capacity import capacity_fields
from buddy2api.model_reasoning import reasoning_fields

CATALOG_SETTING = "channel_catalogs"
REFRESH_SETTING = "channel_catalog_refresh"
EXTRAS_SETTING = "channel_catalog_extras"
# 每个模型的官方扣费基准与可用站点，按通道持久化：重启后目录页仍能显示倍率、
# 站点筛选也不会失效（否则重启后两者都变成「未知」直到下次刷新）。
MODEL_SITES_SETTING = "channel_model_sites"
# 用实测计费反推「免费」所需的最少成功样本数：太少可能只是还没被计费，不能下结论。
FREE_RATE_MIN_REQUESTS = 5

Fetcher = Callable[[dict], Awaitable[list[dict]]]


class CatalogError(ValueError):
    """Invalid channel or model id for a catalog write."""


def _load_map(key: str) -> dict:
    try:
        value = db.get_setting(key, {}) or {}
    except sqlite3.OperationalError:
        return {}
    return value if isinstance(value, dict) else {}


def stored_catalog(channel: str) -> list[dict] | None:
    items = _load_map(CATALOG_SETTING).get(channel)
    if isinstance(items, list) and items:
        return [item for item in items if isinstance(item, dict) and item.get("id")]
    return None


def save_catalog(channel: str, models: list[dict]) -> None:
    catalogs = _load_map(CATALOG_SETTING)
    catalogs[channel] = models
    db.set_setting(CATALOG_SETTING, catalogs)


# 上游说明字段名不统一：WorkBuddy 给的是 descriptionZh / descriptionEn（官方客户端的
# 「已更新至 V4.1-Flash-1017最新版」就在前者里），别的通道用 description。
_DESCRIPTION_KEYS = ("description", "descriptionZh", "descriptionEn")


def description_text(row: Any) -> str:
    """取官方说明文本（中文优先、英文兜底）；都没有就返回空串。

    只做展示，不参与路由：版本号（如 1017）是上游写在说明里的，唯一拿得到的地方
    就是模型目录，丢了用户在页面上就看不到「当前是哪一版」。
    """
    if not isinstance(row, dict):
        return ""
    for key in _DESCRIPTION_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def normalize_models(rows: Any) -> list[dict]:
    if not isinstance(rows, list):
        return []
    models: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            mid = row.strip()
            name = mid
            description = ""
        elif isinstance(row, dict):
            mid = str(
                row.get("id")
                or row.get("model_id")
                or row.get("model_name")
                or row.get("name")
                or ""
            ).strip()
            name = str(row.get("name") or row.get("display_id") or row.get("display_name") or mid)
            description = description_text(row)
        else:
            continue
        if not mid or mid in seen:
            continue
        seen.add(mid)
        item = {"id": mid, "name": name or mid}
        item.update(capacity_fields(row))
        item.update(reasoning_fields(row))
        if description:
            item["description"] = description
        # 官方扣费基准（"x0.79 credits" → 0.79）：目录页要显示，不能在这里丢掉。
        rate = row.get("credit_rate") if isinstance(row, dict) else None
        if isinstance(rate, (int, float)):
            item["credit_rate"] = float(rate)
        models.append(item)
    return models


def _raw_extras(channel: str) -> list:
    items = _load_map(EXTRAS_SETTING).get(channel)
    return list(items) if isinstance(items, list) else []


def _removal_markers(items: list) -> list[dict]:
    """从原始 extras 里取出「已删除」标记项（官方模型被用户删掉时留下的墓碑）。"""
    return [
        {"id": str(item.get("id")), "removed": True}
        for item in items
        if isinstance(item, dict) and item.get("removed") and item.get("id")
    ]


def extras_for(channel: str) -> list[dict]:
    """用户手动添加的模型；「已删除」标记项不算模型，单独处理。"""
    items = [
        item
        for item in _raw_extras(channel)
        if isinstance(item, dict) and not item.get("removed")
    ]
    return normalize_models(items)


def removed_ids(channel: str) -> set[str]:
    return {item["id"] for item in _removal_markers(_raw_extras(channel))}


def save_extras(channel: str, items: list) -> None:
    extras = _load_map(EXTRAS_SETTING)
    extras[channel] = list(items)
    db.set_setting(EXTRAS_SETTING, extras)


def _merge_models(base: list[dict], extra: list[dict]) -> list[dict]:
    return normalize_models(list(base) + list(extra))


def _visible_models(channel: str, base: list[dict]) -> list[dict]:
    """官方/回退目录 + 手动项，并剔除用户删掉的（带墓碑的）模型。"""
    hidden = removed_ids(channel)
    if hidden:
        base = [
            item
            for item in base
            if str((item.get("id") if isinstance(item, dict) else item) or "") not in hidden
        ]
    return _merge_models(base, extras_for(channel))


def models_for(channel: str, fallback: list[dict]) -> list[dict]:
    stored = stored_catalog(channel)
    base = stored if stored else list(fallback)
    return _visible_models(channel, base)


def _with_manual(channel: str, models: list[dict]) -> list[dict]:
    extra_ids = {str(item.get("id")) for item in extras_for(channel)}
    annotated = []
    for item in models:
        row = dict(item)
        row["manual"] = str(row.get("id") or "") in extra_ids
        annotated.append(row)
    return annotated


def _hidden_count(channel: str) -> int:
    """被用户删除（打上墓碑）的官方模型数量，UI 用它显示「已删除 N」。"""
    return len(removed_ids(channel))


def _normalize_model_id(channel: str, model_id: str) -> str:
    mid = str(model_id or "").strip()
    prefix = f"{channel}/"
    if mid.startswith(prefix):
        mid = mid[len(prefix) :].strip()
    return mid


def _require_enabled_channel(channel: str) -> str:
    import buddy2api.providers as providers

    value = str(channel or "").strip()
    if not value or not providers.is_channel_enabled(value):
        raise CatalogError("unknown or disabled channel")
    return value


def current_models(channel: str) -> list[dict]:
    import buddy2api.providers as providers

    provider = providers.get_provider(channel)
    if provider is not None:
        return list(provider.list_models())
    if channel == "workbuddy":
        return workbuddy_fallback_models()
    return extras_for(channel)


def upsert_model(channel: str, model_id: str, name: str = "") -> dict:
    channel = _require_enabled_channel(channel)
    mid = _normalize_model_id(channel, model_id)
    if not mid:
        raise CatalogError("model id is required")
    label = str(name or "").strip() or mid
    raw = _raw_extras(channel)
    markers = _removal_markers(raw)
    manual = extras_for(channel)

    # 之前被删除的官方模型：重新添加即恢复（去掉墓碑），而不是变成手动项。
    if any(marker["id"] == mid for marker in markers):
        markers = [marker for marker in markers if marker["id"] != mid]
        save_extras(channel, markers + manual)
        visible = {
            str(item.get("id"))
            for item in current_models(channel)
            if isinstance(item, dict)
        }
        if mid not in visible:
            # 上游目录已经不提供它了：作为手动项保底，否则这次「恢复」等于什么都没做。
            manual = [item for item in manual if item.get("id") != mid]
            manual.append({"id": mid, "name": label})
            save_extras(channel, markers + manual)
        models = current_models(channel)
        return {
            "channel": channel,
            "id": mid,
            "name": label,
            "count": len(models),
            "models": _with_manual(channel, models),
            "updated": True,
            "restored": True,
        }

    extra_ids = {str(item.get("id")) for item in manual}
    current_ids = {
        str(item.get("id")) for item in current_models(channel) if isinstance(item, dict)
    }
    if mid in current_ids and mid not in extra_ids:
        raise CatalogError("model already exists in this channel")
    found = False
    for item in manual:
        if item.get("id") == mid:
            item["name"] = label
            found = True
            break
    if not found:
        manual.append({"id": mid, "name": label})
    save_extras(channel, markers + manual)
    models = current_models(channel)
    return {
        "channel": channel,
        "id": mid,
        "name": label,
        "count": len(models),
        "models": _with_manual(channel, models),
        "updated": found,
    }


def remove_model(channel: str, model_id: str) -> dict:
    """删除模型：手动项直接移除，官方项留「墓碑」——一键读取也不会把它带回来。"""
    channel = _require_enabled_channel(channel)
    mid = _normalize_model_id(channel, model_id)
    if not mid:
        raise CatalogError("model id is required")
    raw = _raw_extras(channel)
    markers = _removal_markers(raw)
    manual = extras_for(channel)
    manual_ids = {str(item.get("id")) for item in manual}
    visible_ids = {
        str(item.get("id")) for item in current_models(channel) if isinstance(item, dict)
    }
    official_ids = visible_ids - manual_ids

    if mid in manual_ids:
        manual = [item for item in manual if item.get("id") != mid]
    if mid in official_ids or any(marker["id"] == mid for marker in markers):
        markers.append({"id": mid, "removed": True})
    elif mid not in manual_ids:
        raise CatalogError("model not found")

    deduped: list[dict] = []
    seen: set[str] = set()
    for marker in markers:
        if marker["id"] in seen:
            continue
        seen.add(marker["id"])
        deduped.append(marker)
    save_extras(channel, deduped + manual)
    models = current_models(channel)
    return {
        "channel": channel,
        "id": mid,
        "count": len(models),
        "models": _with_manual(channel, models),
    }


def workbuddy_fallback_models() -> list[dict]:
    import buddy2api.proxy as proxy

    try:
        models = db.get_setting("models", proxy.DEFAULT_MODELS)
    except sqlite3.OperationalError:
        return list(proxy.DEFAULT_MODELS)
    if isinstance(models, list) and models:
        return models
    return list(proxy.DEFAULT_MODELS)


def _fallback_models(channel: str, provider) -> list[dict]:
    if provider is not None:
        try:
            return list(provider.list_models())
        except Exception:
            pass
    if channel == "workbuddy":
        return workbuddy_fallback_models()
    return extras_for(channel)


def _model_annotations() -> tuple[dict[str, dict], dict[str, dict[str, list[str]]]]:
    """目录页要显示的附加信息：历史日志累计消耗 + 每模型的站点/扣费基准。

    日志累计每次都现算（日志一直在写，缓存住会让「刷新」按钮点了等于没点）；
    站点与扣费基准来自持久化的 channel_model_sites（刷新目录时写入）。
    """
    try:
        credits = db.model_credit_totals()
    except sqlite3.OperationalError:
        credits = {}
    stored = _load_map(MODEL_SITES_SETTING).get("workbuddy")
    site_info: dict[str, dict[str, list[str]]] = {}
    if isinstance(stored, dict):
        for mid, info in stored.items():
            if isinstance(info, dict) and info.get("sites"):
                site_info[str(mid)] = {
                    "sites": [str(g) for g in info["sites"] if g in sites.SITE_GROUPS],
                    "rate": {str(k): v for k, v in (info.get("rate") or {}).items()
                             if isinstance(v, (int, float))},
                }
    # 目录接口只是官方推荐清单，不等于可服务范围（国际站目录里没有 deepseek-v4.1-flash
    # 但打它全部成功免费）——所以再用成功请求日志反证，与目录证据取并集。
    try:
        usage = db.model_site_usage()
    except sqlite3.OperationalError:
        usage = {}
    for mid, groups in usage.items():
        info = site_info.setdefault(mid, {"sites": [], "rate": {}})
        for group in groups:
            if group not in info["sites"]:
                info["sites"].append(group)
        info["sites"].sort()
    # 目录缺这个站点的倍率时，用实测计费反推：样本足够且一次都没扣费就是免费（x0）。
    # 典型例子 deepseek-v4.1-flash：国际站目录里没有它，但 5026 次成功请求 0 扣费，
    # 显示国内目录的 x0.03 会让人以为国际站也扣费。
    try:
        observed = auth_manager.cost_profile()
    except Exception:
        observed = {}
    for mid, info in site_info.items():
        rates = info["rate"]
        for group in info["sites"]:
            if group in rates:
                continue
            stats = (observed.get(mid) or {}).get(group) or {}
            if int(stats.get("requests") or 0) >= FREE_RATE_MIN_REQUESTS and not stats.get("paid"):
                rates[group] = 0.0
    return credits, site_info


def _annotate_models(
    channel: str,
    models: list[dict],
    credits: dict[str, dict],
    site_info: dict[str, dict],
) -> list[dict]:
    """给目录项补上展示字段：累计消耗、可用站点与官方扣费基准。

    - credit/credit_requests：历史日志按模型的实际累计（带前缀与裸 ID 都算）。
    - sites/rate：来自上游模型列表的 credits 字段（官方客户端的 x0.79/Free now 就
      是它），按站点分组记录 —— 同一个模型两边的倍率可以不同（hy4-preview 国际
      x0.00、国内 x0.29）。

    只有 WorkBuddy 的账号带 domain（国内/国际站），其余通道的模型不参与站点筛选；
    否则同名的 qclaw 模型会被贴上 WorkBuddy 的站点标签。
    """
    annotated: list[dict] = []
    for item in models:
        row = dict(item)
        mid = str(row.get("id") or "")
        credit = 0.0
        requests = 0
        for key in (mid, f"{channel}/{mid}"):
            stats = credits.get(key) or {}
            credit += float(stats.get("credit") or 0)
            requests += int(stats.get("requests") or 0)
        row["credit"] = round(credit, 4)
        row["credit_requests"] = requests
        info = site_info.get(mid) if channel == "workbuddy" else None
        if info is not None:
            # 上游模型项里烤进去的 credit_rate 只对「采样它的那个站点」有效（目录是并集，
            # 看不出是谁给的），所以展示值一律由下面的站点倍率决定。
            row.pop("credit_rate", None)
        row["sites"] = list(info.get("sites") or ()) if info else []
        if info and info.get("rate"):
            rates = info["rate"]
            known = [g for g in row["sites"] if g in rates]
            # 已知站点倍率一致时才给单一值（如 glm-5.3 两边都 x0.79）；两边不同就留给
            # 前端按站点分别展示 —— 把国内 x0.03 当成国际的会误导（国际实测免费）。
            if known and len(known) == len(row["sites"]) \
                    and len({rates[g] for g in known}) == 1:
                row["credit_rate"] = rates[known[0]]
            row["site_rates"] = rates
        annotated.append(row)
    return annotated


def _status_row(
    channel: str,
    *,
    mode: str,
    models: list[dict],
    message: str = "",
    display_name: str = "",
) -> dict:
    return {
        "channel": channel,
        "display_name": display_name or channel,
        "mode": mode,
        "message": message,
        "count": len(models),
        "hidden_count": _hidden_count(channel),
        "models": _with_manual(channel, models),
        "updated_at": int(time.time()),
    }


async def _pick_account(provider) -> dict | None:
    if provider is None:
        return None
    picker = getattr(provider, "pick_account_with_fallback", None)
    if picker is None:
        return None
    return await picker()


async def _catalog_accounts(provider) -> list[dict]:
    """刷新目录时采样哪些账号。

    默认只取一个（与历史行为一致）。通道可以覆盖成「全部活跃账号」—— 国内站与国际站
    的模型集几乎不重叠，只采样一个账号会让另一个站的模型整体从 /v1/models 里消失，
    客户端也就再也请求不到它们了。
    """
    if provider is None:
        return []
    hook = getattr(provider, "catalog_accounts", None)
    if hook is not None:
        accounts = [a for a in await hook() if isinstance(a, dict)]
        if accounts:
            return accounts
    account = await _pick_account(provider)
    return [account] if account else []


async def _fetch_qclaw(account: dict) -> list[dict]:
    from buddy2api.providers.qclaw.jprx import fetch_supplier_models

    return await fetch_supplier_models(account)


async def _fetch_traework(account: dict) -> list[dict]:
    from buddy2api.providers.traework.models import fetch_supplier_models

    return await fetch_supplier_models(account)


async def _fetch_qwenwork(account: dict) -> list[dict]:
    from buddy2api.providers.qwenwork.models import fetch_supplier_models

    return await fetch_supplier_models(account)


async def _fetch_workbuddy(account: dict) -> list[dict]:
    from buddy2api.providers.workbuddy.models import fetch_supplier_models

    return await fetch_supplier_models(account)


LIVE_FETCHERS: dict[str, Fetcher] = {
    "qclaw": _fetch_qclaw,
    "traework": _fetch_traework,
    "qwenwork": _fetch_qwenwork,
    "workbuddy": _fetch_workbuddy,
}


async def refresh_one(channel: str) -> dict:
    import buddy2api.providers as providers

    provider = providers.get_provider(channel)
    display_name = getattr(provider, "display_name", channel) if provider else channel
    fallback = _fallback_models(channel, provider)
    fetcher = LIVE_FETCHERS.get(channel)
    if fetcher is None:
        return _status_row(
            channel,
            mode="fallback",
            models=fallback,
            message="no supplier-list API",
            display_name=display_name,
        )
    accounts = await _catalog_accounts(provider)
    if not accounts:
        return _status_row(
            channel,
            mode="fallback",
            models=fallback,
            message="no usable account",
            display_name=display_name,
        )

    fetched: list[dict] = []
    failures: list[str] = []
    # 模型 → 站点 → 官方扣费基准。同一模型两边倍率不同（hy4-preview 国际 x0.00、
    # 国内 x0.29），所以必须按站点分开记；同站多个账号取首个非空值（同站倍率一致）。
    site_rates: dict[str, dict[str, float]] = {}
    for account in accounts:
        try:
            account_models = normalize_models(await fetcher(account))
        except Exception as exc:
            failures.append(str(exc)[:120])
            continue
        if not account_models:
            continue
        # 把「这个账号能服务哪些模型」写回路由层：混装国内/国际账号时，这是把请求
        # 发给正确账号的唯一依据（模型目录本身是并集，看不出账号差异）。
        record = getattr(provider, "record_account_models", None)
        if record is not None:
            record(account, [item["id"] for item in account_models])
        if channel == "workbuddy" and account.get("domain"):
            group = sites.site_group(account["domain"])
            for item in account_models:
                rate = item.get("credit_rate")
                if rate is not None:
                    site_rates.setdefault(str(item["id"]), {}).setdefault(group, rate)
        fetched = _merge_models(fetched, account_models)

    if not fetched:
        return _status_row(
            channel,
            mode="fallback",
            models=fallback,
            message="; ".join(failures)[:240] or "empty supplier list",
            display_name=display_name,
        )
    save_catalog(channel, fetched)
    if site_rates:
        stored_map = _load_map(MODEL_SITES_SETTING)
        stored_map[channel] = {
            mid: {"sites": sorted(groups), "rate": groups}
            for mid, groups in site_rates.items()
        }
        db.set_setting(MODEL_SITES_SETTING, stored_map)
    if channel == "workbuddy":
        db.set_setting("models", fetched)
    return _status_row(
        channel,
        mode="live",
        models=_visible_models(channel, fetched),
        message="",
        display_name=display_name,
    )


async def refresh_supplier_catalogs() -> dict:
    import buddy2api.providers as providers

    sources = []
    for channel in providers.enabled_provider_ids():
        sources.append(await refresh_one(channel))
    credits, site_availability = _model_annotations()
    for item in sources:
        item["models"] = _sorted_models(_annotate_models(
            item["channel"], item["models"], credits, site_availability
        ))
    db.set_setting(
        REFRESH_SETTING,
        {
            item["channel"]: {
                "mode": item["mode"],
                "message": item["message"],
                "count": item["count"],
                "updated_at": item["updated_at"],
            }
            for item in sources
        },
    )
    return {"sources": sources}


def _sorted_models(models: list[dict]) -> list[dict]:
    """目录页展示用：按 id 排序。

    目录是并集、顺序取決于采样账号先后，切站点筛选时行序会跳来跳去；
    排序后国际/国内筛选下的相对位置保持一致，也方便对照查找。
    """
    return sorted(models, key=lambda item: str(item.get("id") or ""))


def catalog_snapshot() -> dict:
    import buddy2api.providers as providers

    refresh = _load_map(REFRESH_SETTING)
    credits, site_availability = _model_annotations()
    sources = []
    for channel in providers.enabled_provider_ids():
        provider = providers.get_provider(channel)
        if provider is not None:
            models = list(provider.list_models())
        elif channel == "workbuddy":
            models = workbuddy_fallback_models()
        else:
            models = []
        meta = refresh.get(channel) if isinstance(refresh.get(channel), dict) else {}
        sources.append(
            {
                "channel": channel,
                "display_name": getattr(provider, "display_name", channel) if provider else channel,
                "mode": meta.get("mode") or ("fallback" if channel not in LIVE_FETCHERS else "static"),
                "message": meta.get("message") or "",
                "count": len(models),
                "hidden_count": _hidden_count(channel),
                "models": _sorted_models(_annotate_models(
                    channel, _with_manual(channel, models), credits, site_availability
                )),
                "updated_at": meta.get("updated_at"),
            }
        )
    return {"sources": sources}
