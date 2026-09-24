"""工具栏刷新按钮合并的契约测试。

背景：账号页原先并排三个刷新按钮 ——「刷新列表」「刷新官方额度」「刷新领取状态」。
其中后两个是第一个的真子集：`load(true)` 内部就是
    取列表 → loadCheckins() → refreshAllResources()
而且「刷新官方额度」调 `refreshAllResources()`（force=false），与 `load(true)` 的
第三步发的是**完全相同的请求**，点哪个都一样。

合并成一个「刷新」后，唯一会丢失的能力是「绕过签到状态的 5 分钟服务端缓存」
（fetch_checkin_status 的 max_age_seconds=300）。这是真需求，所以保留成第二个
「强制刷新」按钮，而不是直接删掉。

这里钉死：三个按钮没了、两个新按钮在、以及 force 真的透传到了下游。
"""

import re
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"


@pytest.fixture(scope="module")
def html_source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def account_toolbar(html_source) -> str:
    """账号页工具栏那一行。"""
    for line in html_source.split("\n"):
        if "claimAll" in line and "tbar" in line:
            return line
    pytest.fail("找不到账号页工具栏")


def test_redundant_buttons_removed(account_toolbar):
    """三个旧按钮的文字都不应再出现在工具栏里。"""
    for label in ("刷新列表", "刷新官方额度", "刷新领取状态"):
        assert label not in account_toolbar, f"工具栏里仍有冗余按钮「{label}」"


def test_single_refresh_button_does_everything(account_toolbar):
    """保留的「刷新」必须还是 load(true)（列表 + 领取状态 + 官方额度）。"""
    assert '@click="load(true)"' in account_toolbar, "「刷新」不再是全量刷新"
    assert ">刷新<" in account_toolbar.replace("'刷新中':'刷新'", ">刷新<") or "'刷新'" in account_toolbar


def test_force_refresh_button_preserved(account_toolbar):
    """必须保留一个能绕过签到缓存的入口，否则最多会看到 5 分钟前的旧状态。"""
    assert "强制刷新" in account_toolbar, "删掉了「强制刷新」，失去了绕过签到缓存的能力"
    assert '@click="load(true,true)"' in account_toolbar, "「强制刷新」没有传 force=true"


def test_load_accepts_and_forwards_force(html_source):
    """load(withOfficial, force) 必须把 force 透传给 loadCheckins 和 refreshAllResources。"""
    m = re.search(r"async function load\(withOfficial=false(.*?)\n", html_source)
    assert m, "load 的签名变了"
    sig = m.group(0)
    assert "force=false" in sig, "load 没有接收 force 参数"
    assert "loadCheckins(force)" in sig, "load 没有把 force 传给 loadCheckins"
    assert "refreshAllResources(true,force)" in sig, "load 没有把 force 传给 refreshAllResources"


def test_refreshAllResources_forwards_force(html_source):
    m = re.search(r"async function refreshAllResources\(silent=false,force=false\)\{(.*?)\n", html_source)
    assert m, "refreshAllResources 的签名变了"
    assert "refreshResource(a,true,force)" in m.group(0), (
        "refreshAllResources 没有把 force 透传给 refreshResource"
    )


def test_buttons_have_titles_explaining_the_difference(account_toolbar):
    """两个刷新按钮的区别（是否忽略缓存）必须写出来，否则用户不知道点哪个。"""
    assert "title=" in account_toolbar
    assert account_toolbar.count("title=") >= 2, "两个刷新按钮都应给出 title 说明"
    assert "缓存" in account_toolbar, "title 里应说明缓存差异"


def test_claim_and_seamless_buttons_untouched(account_toolbar):
    """这次只删刷新类按钮，别把无关功能删掉。"""
    for label in ("一键领取今日积分", "无感登录", "高级手动添加", "重置请求计数"):
        assert label in account_toolbar, f"误删了「{label}」"
