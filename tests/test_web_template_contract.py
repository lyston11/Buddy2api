"""管理页模板契约测试。

背景：`web/index.html` 是一个单文件 Vue 应用，每个组件的 setup 以 `return{...}`
显式暴露模板用到的变量/函数。如果模板里引用了一个名字，而它既不是 setup 返回的键、
也不是 props / v-for 局部变量 / Vue 内置对象，Vue 在渲染期求值时就抛错——该区域会
静默渲染为空，页面看起来"什么都没有"，且除了浏览器控制台外没有任何提示。

这类改动静态检查不报错、后端接口也完全正常，极易被误判成后端故障。所以这里把契约
钉死：模板引用的根标识符必须能在这几个来源里找到。

模板用反引号包裹，且闭合的反引号后面是换行再跟 `})`／`}).component(...)`，
因此不能简单用 `` `}) `` 去切——必须按"反引号之后是 } 或 ) 或 ,"来判定闭合，
否则会一个组件都解析不出来、测试空转通过。

覆盖范围：{{ 插值 }}、v-* 指令、:bind 指令、@event 处理器。
"""

import asyncio
import re
from pathlib import Path

import httpx
import pytest

import buddy2api.server as server

INDEX_HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"

# 模板里合法但不来自 setup/props 的名字
JS_KEYWORDS = {
    "true", "false", "null", "undefined", "in", "of", "typeof", "new", "return",
    "void", "delete", "instanceof", "if", "else", "for", "while", "do", "try",
    "catch", "switch", "case", "default", "break", "continue", "this",
    "Math", "Number", "String", "Boolean", "Array", "Object", "JSON", "Date",
    "Set", "Map", "Promise", "RegExp", "Symbol", "parseInt", "parseFloat",
    "isNaN", "encodeURIComponent", "decodeURIComponent",
    # Vue 模板内置
    "$event", "$refs", "$slots", "$attrs", "$emit", "$props",
}


def _template_end(src: str, start: int) -> int:
    """定位模板字面量的闭合反引号。

    闭合反引号后面（跳过空白）应当是 `}`／`)`／`,`——即 `})` / `}).component(` 这类结尾。
    """
    i = start
    while True:
        i = src.find("`", i)
        if i < 0:
            return -1
        rest = src[i + 1:].lstrip()
        if rest[:1] in ("}", ")", ","):
            return i
        i += 1


def _strip_literals(expr: str) -> str:
    """去掉字符串/模板字面量，避免把其中的文字当成标识符。"""
    expr = re.sub(r"'(?:[^'\\]|\\.)*'", "''", expr)
    expr = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr)
    expr = re.sub(r"`(?:[^`\\]|\\.)*`", "``", expr)
    return expr


def _root_identifiers(expr: str) -> set:
    """取出表达式里的"根标识符"，剔除 `.prop` 属性访问与 `{key: value}` 的字面量键。"""
    expr = _strip_literals(expr)
    expr = re.sub(r"\.\s*[A-Za-z_$][\w$]*", "", expr)              # 去掉 .prop
    expr = re.sub(r"(?<![\w$])[A-Za-z_$][\w$]*\s*:", "", expr)     # 去掉对象字面量 key
    return set(re.findall(r"(?<![\w$.])[A-Za-z_$][\w$]*", expr))


def _components(src: str):
    """产出 (组件名, 模板源码, setup 返回键集合, props 集合)。

    注意用 `template:` 而不是 `,template:` 匹配：根组件写成 `},\\n  template:`，
    逗号在上一行，只认 `,template:` 会漏掉根组件。
    """
    for m in re.finditer(r"template:`", src):
        tstart = m.end()
        tend = _template_end(src, tstart)
        if tend < 0:
            continue
        head = src[: m.start()]

        name = "(root app)"
        props = set()
        for am in re.finditer(r"\.component\('([\w-]+)',\{props:\[([^\]]*)\]", head):
            name = am.group(1)
            props = {p.strip().strip("'\"") for p in am.group(2).split(",") if p.strip()}

        rstart = head.rfind("return{")
        assert rstart >= 0, f"组件 {name} 的 setup 未找到 return{{...}}"
        rend = head.find("}", rstart)
        keys = {k.strip() for k in head[rstart + len("return{"):rend].split(",") if k.strip()}

        yield name, src[tstart:tend], keys, props


def _vfor_locals(tpl: str) -> set:
    names = set(re.findall(r'v-for="\(?\s*([A-Za-z_$][\w$]*)', tpl))
    names |= set(re.findall(r'v-for="\(?\s*[A-Za-z_$][\w$]*\s*,\s*([A-Za-z_$][\w$]*)', tpl))
    return names


@pytest.fixture(scope="module")
def html_source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_component_templates_only_use_known_bindings(html_source):
    """模板引用的根标识符必须来自 setup 返回、props、v-for 局部变量或 Vue 内置。"""
    components = list(_components(html_source))

    # 防止解析失效导致"零断言通过"——这个文件当前是 1 个根组件 + 6 个命名组件
    assert len(components) >= 7, f"只解析出 {len(components)} 个组件，解析逻辑可能已失效"

    problems = []
    for name, tpl, keys, props in components:
        assert len(tpl) > 500, f"组件 {name} 的模板只有 {len(tpl)} 字符，切分可能出错"
        allowed = keys | props | JS_KEYWORDS | _vfor_locals(tpl)

        render_time = set()
        for expr in re.findall(r"\{\{(.*?)\}\}", tpl, re.S):
            render_time |= _root_identifiers(expr)
        for expr in re.findall(r'\sv-[\w:.-]+="([^"]*)"', tpl):
            render_time |= _root_identifiers(expr)
        for expr in re.findall(r'(?<![@\w:]):[\w.-]+="([^"]*)"', tpl):
            render_time |= _root_identifiers(expr)

        event_time = set()
        for expr in re.findall(r'@[\w.-]+="([^"]*)"', tpl):
            event_time |= _root_identifiers(expr)

        missing_render = sorted(render_time - allowed)
        missing_event = sorted(event_time - allowed)

        if missing_render:
            problems.append(
                f"组件 [{name}] 渲染期引用了未定义的名字 {missing_render}，"
                f"会导致该区域静默渲染为空（白屏）"
            )
        if missing_event:
            problems.append(
                f"组件 [{name}] 事件处理器引用了未定义的名字 {missing_event}，点击时会抛错"
            )

    assert not problems, "\n".join(problems)


def test_accounts_component_exposes_site_label(html_source):
    """账号页用它自己的 siteLabel 渲染站点标签；漏返回会让整页白屏，单独钉一条。"""
    assert "{{siteLabel(a)}}" in html_source, "账号行应渲染站点标签"
    for name, tpl, keys, _props in _components(html_source):
        if "{{siteLabel(a)}}" in tpl:
            assert "siteLabel" in keys, f"组件 [{name}] 模板用了 siteLabel，但 setup 没有返回它"
            break
    else:
        pytest.fail("没有找到渲染 siteLabel 的组件")


def test_vue_is_served_locally_not_from_cdn(html_source):
    """Vue 必须由本机 /vendor 提供，不能再依赖外网 CDN。

    回归（2026-09-24）：index.html 一直外链 jsdelivr 上的 Vue。当代理到该 CDN 不通时
    浏览器拿不到脚本 → `Uncaught ReferenceError: Vue is not defined` → Vue 从不 mount
    → 整页白屏，而 HTTP 响应仍是 200、字节数也正常，看响应完全查不出来。

    这里钉死整条链路：模板的外链 URL 有对应的本地文件、渲染后换成 /vendor 路径、
    静态资源真的能取到、未知文件仍是 404。
    """
    # 1. 模板里每个外链脚本 URL 都必须在服务端的本地映射表里（否则渲染后仍是外链）
    linked = re.findall(r'<script[^>]*\bsrc="(https?://[^"]+)"', html_source)
    assert linked, "index.html 里没有外链脚本，测试已失效"
    for url in linked:
        assert url in server.VENDOR_ASSETS.values(), f"外链脚本 {url} 没有本地副本，CDN 不可达时仍会白屏"

    # 2. 渲染后的 HTML 只引用本地 /vendor 路径
    rendered = server._render_index_html()
    for local, remote in server.VENDOR_ASSETS.items():
        assert f'src="/vendor/{local}"' in rendered, f"渲染后应引用 /vendor/{local}"
        assert remote not in rendered, f"渲染后不应再出现外链 {remote}"

    # 3. 本地副本存在，且真的能通过 HTTP 取到
    def get(path):
        async def run():
            transport = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 12345))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as client:
                return await client.get(path)
        return asyncio.run(run())

    for local in server.VENDOR_ASSETS:
        assert (server.VENDOR_DIR / local).is_file(), f"缺少本地脚本 web/vendor/{local}"
        response = get(f"/vendor/{local}")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        # Vue 的全局构建会把 Vue 挂到 window 上，用这个当"确实是 Vue"的判据
        assert "Vue" in response.text

    # 4. 白名单之外的文件不能被当成静态资源取走
    assert get("/vendor/../server.py").status_code in (404, 400)
    assert get("/vendor/nope.js").status_code == 404


def test_ui_has_global_error_handler(html_source):
    """必须注册全局 errorHandler，否则渲染异常只会静默白屏、无任何提示。"""
    assert "app.config.errorHandler" in html_source, (
        "缺少 app.config.errorHandler：Vue 渲染异常会静默失败，页面直接白屏且无提示"
    )
    assert "window.__cbFatal" in html_source, "缺少统一的前端异常展示入口 window.__cbFatal"
