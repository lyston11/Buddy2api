"""「领取」按钮的可用性契约测试。

背景：账号列表每行的「领取」按钮原先只判断 `status==='active'`，所以今天已经领过的
账号、签到活动没开的账号依然可以点。点了服务端也只是返回 `already_claimed`——
用户看到的现象是「点了也没用，按钮还是亮的」，而且一键领取会把 `claimingAll`
卡在「领取中」（旧代码在 catch 之外复位，抛错时能复位，但 `load()` 抛错就跳过复位）。

这里钉死两件事：
1. `canClaim()` / `claimBlockReason()` 的判定与理由文案；
2. 模板里每行按钮与工具栏按钮都真的绑定了这两个函数（不是只定义了没用）。

纯静态断言不足以证明"点了真的没反应"，所以另有一条真实浏览器渲染测试，
见 tests/test_web_render_claim_button.py。
"""

import re
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"


@pytest.fixture(scope="module")
def html_source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def claim_logic(html_source) -> str:
    """截出 claimBlockReason / canClaim 两个函数的源码，供逐条断言。"""
    m = re.search(r"function claimBlockReason\(a\)\{(.*?)\n  \}", html_source, re.S)
    assert m, "web/index.html 里找不到 claimBlockReason 函数"
    return m.group(0)


@pytest.fixture(scope="module")
def can_claim(html_source) -> str:
    m = re.search(r"function canClaim\(a\)\{[^\n]*\}", html_source)
    assert m, "web/index.html 里找不到 canClaim 函数"
    return m.group(0)


def test_disabled_when_already_claimed(claim_logic):
    """今日已领（三种等价标记）都必须给出禁用理由。"""
    for flag in ("r.claimed", "r.already_claimed", "r.today_checked_in"):
        assert flag in claim_logic, f"claimBlockReason 没有处理 {flag}：今天领过的账号仍可点"
    assert "今日已领取" in claim_logic


def test_disabled_when_activity_unavailable(claim_logic):
    """活动未开启/过期必须禁用，否则点了必然无效。"""
    assert "r.unavailable" in claim_logic
    assert "r.active===false" in claim_logic
    assert "活动未开启" in claim_logic


def test_disabled_when_status_fetch_failed(claim_logic):
    """状态读取失败时不能假装可领，否则点下去只会再失败一次。"""
    assert "r.ok===false" in claim_logic


def test_disabled_when_account_not_active(claim_logic):
    assert "a.status!=='active'" in claim_logic
    assert "账号未启用" in claim_logic


def test_status_unknown_stays_clickable(claim_logic):
    """没有状态数据时必须保持可点：签到状态是异步/可缓存的，不该把按钮锁死。

    这条是防止"修过头"——把 `if(!r)return ''` 改成一律禁用会让首屏
    （状态还没加载回来）所有按钮都点不动。
    """
    assert re.search(r"if\(!r\)return ''", claim_logic), (
        "缺少状态时的放行分支：签到状态尚未加载时按钮会被永久禁用"
    )


def test_can_claim_is_negation_of_reason(can_claim):
    assert "claimBlockReason" in can_claim, "canClaim 应复用 claimBlockReason，避免两套判定漂移"


def test_row_button_uses_can_claim(html_source):
    """每行的「领取」按钮必须绑定 canClaim 并给出 title 说明原因。"""
    m = re.search(r'<button class="btn s" @click="claimOne\(a\)"([^>]*)>', html_source)
    assert m, "找不到每行的领取按钮"
    attrs = m.group(1)
    assert "canClaim(a)" in attrs, "行内领取按钮没有用 canClaim 判定，已领过的账号仍可点"
    assert "claimBlockReason(a)" in attrs, "行内领取按钮缺少 title，用户不知道为什么点不了"


def test_row_button_no_longer_only_checks_status(html_source):
    """回归防线：不能再退回只判断 status 的旧写法。"""
    m = re.search(r'<button class="btn s" @click="claimOne\(a\)"([^>]*)>', html_source)
    assert m
    assert ':disabled="busyKey(a.id,\'claim\')||a.status!==\'active\'"' not in m.group(1), (
        "行内领取按钮退回了只看 status 的旧判定"
    )


def test_bulk_button_uses_can_claim(html_source):
    """一键领取在没有"可领"账号时应禁用，而不是明知领不到还发起一轮请求。"""
    # 属性值里含 `=>`（!l.some(a=>canClaim(a))），所以不能按 `>` 截断标签，
    # 直接取该按钮到下一个 </button> 之间的片段。
    i = html_source.find('@click="claimAll"')
    assert i > 0, "找不到一键领取按钮"
    attrs = html_source[i : html_source.find("</button>", i)]
    assert ":disabled=" in attrs, "一键领取按钮缺少禁用条件"
    assert "canClaim(a)" in attrs, "一键领取按钮仍只按 status 判断"


def test_claim_all_resets_flag_in_finally(html_source):
    """claimingAll 必须在 finally 里复位，否则出错时按钮会永久卡在「领取中」。"""
    m = re.search(r"async function claimAll\(\)\{(.*?)\n  \}", html_source, re.S)
    assert m, "找不到 claimAll"
    body = m.group(0)
    assert "finally{claimingAll.value=false}" in body, (
        "claimAll 没有在 finally 中复位 claimingAll，异常时会卡住按钮"
    )


def test_exports_include_new_helpers(html_source):
    """两个新函数必须出现在 setup 的 return 里，否则模板引用会静默白屏。"""
    # 账号页 setup 的 return 在它自己的模板之前。文件里有 7 个组件（各自一个
    # `},template:`），所以不能取第一个——用模板里对 claimOne 的绑定定位（注意
    # `async function claimOne(a)` 定义在前，要取带 @click 的那处），再往前找 return{。
    tpl = html_source.find('@click="claimOne(a)"')
    assert tpl > 0, "找不到账号页模板（claimOne 绑定处）"
    rstart = html_source.rfind("return{", 0, tpl)
    guard = html_source.rfind("onMounted(", 0, tpl)
    assert rstart > guard > 0, "定位到的 return{ 不在 onMounted 之后，可能是别的 helper"
    # return{...} 在同一行内结束（行尾是 `}` 再换行），取到换行即可。
    # 用 find("}") 会被结尾 `}` 之前的内容干扰，手写括号配对又会被对象字面量带偏。
    rend = html_source.find("\n", rstart)
    assert rend > rstart, "return{...} 未在同一行内结束"
    body = html_source[rstart + len("return{"):rend].rstrip().rstrip("}")
    keys = {k.strip() for k in body.split(",") if k.strip()}
    for name in ("canClaim", "claimBlockReason"):
        assert name in keys, f"setup 没有返回 {name}，模板引用它会让账号页静默渲染为空"
