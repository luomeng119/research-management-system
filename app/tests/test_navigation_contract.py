from hashlib import sha256
import re
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "app" / "templates"
STATIC = ROOT / "app" / "static"
PROTOTYPE_ASSETS = ROOT.parent.parent.parent / "prototype" / "assets"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_global_shell_uses_confirmed_brand_and_six_primary_destinations():
    source = _read(TEMPLATES / "base.html")
    expected = {
        "dashboard": ("工作台", "/"),
        "proposals": ("科研提案", "/proposals"),
        "projects": ("科研项目", "/projects"),
        "experts": ("专家库", "/experts"),
        "resources": ("科研资源", "/equipment"),
        "tools": ("辅助工具", "/utils"),
    }

    assert "科研创新管理" in source
    assert "logo-reference.png" in source
    for key, (label, href) in expected.items():
        assert f'data-primary-nav="{key}"' in source
        assert label in source
        assert f'href="{href}"' in source


def test_shell_keeps_existing_content_tree_and_accessible_controls():
    source = _read(TEMPLATES / "base.html")
    shell_script = _read(STATIC / "js" / "app-shell.js")

    assert "{% block content %}" in source
    assert "/api/tree" in source + shell_script
    assert 'aria-label="一级导航"' in source
    assert 'aria-label="收起导航"' in source
    assert 'aria-label="全局搜索"' in source
    assert "app-shell.js" in source
    assert "本地服务暂时无法读取目录，可重试" in shell_script
    assert "data-tree-toggle" in shell_script
    assert "aria-expanded" in shell_script


def test_shell_assets_are_local_and_match_the_approved_prototype():
    base = _read(TEMPLATES / "base.html")
    index = _read(TEMPLATES / "index.html")
    shell_css = _read(STATIC / "css" / "app-shell.css")

    assert "http://" not in base + index + shell_css
    assert "https://" not in base + index + shell_css
    assert "/static/icons/lucide-icons.svg#" in base + index
    assert sha256((STATIC / "brand" / "logo-reference.png").read_bytes()).hexdigest() == (
        sha256((PROTOTYPE_ASSETS / "logo-reference.png").read_bytes()).hexdigest()
    )
    def definition(element):
        # Ignore XML indentation only; geometry, attributes and child order stay exact.
        return (element.tag, tuple(sorted(element.attrib.items())),
                (element.text or '').strip(), tuple(definition(child) for child in element))

    namespace = {'svg': 'http://www.w3.org/2000/svg'}
    approved = ET.parse(PROTOTYPE_ASSETS / "lucide-icons.svg").getroot()
    current = ET.parse(STATIC / "icons" / "lucide-icons.svg").getroot()
    assert current.tag == approved.tag
    assert current.attrib == approved.attrib, 'root styles/attributes must not alter approved icons'
    symbol_tag = '{http://www.w3.org/2000/svg}symbol'
    assert [definition(child) for child in current if child.tag != symbol_tag] == [
        definition(child) for child in approved if child.tag != symbol_tag
    ], 'only new symbols may be appended; global style/other nodes must be preserved'
    original_symbols = approved.findall('svg:symbol', namespace)
    symbols = current.findall('svg:symbol', namespace)
    by_id = {symbol.attrib['id']: symbol for symbol in symbols}
    assert original_symbols, 'approved sprite must contain symbols'
    assert len(by_id) == len(symbols), 'duplicate symbol IDs shadow approved icons'
    for symbol in original_symbols:
        identifier = symbol.attrib['id']
        assert identifier in by_id, f'missing approved icon: {identifier}'
        assert definition(by_id[identifier]) == definition(symbol), identifier
    for element in current.iter():
        assert element.tag.rsplit('}', 1)[-1] not in {'script', 'foreignObject', 'style'}
        for attribute, value in element.attrib.items():
            if attribute.rsplit('}', 1)[-1] in {'href', 'src'}:
                assert value.startswith('#') or value.startswith('/static/'), value
            assert 'http://' not in value and 'https://' not in value and not value.lstrip().startswith('//')
            for target in re.findall(r"url\(\s*['\"]?([^'\"\s)]+)", value, flags=re.IGNORECASE):
                assert target.startswith('#') or target.startswith('/static/'), target



def test_design_tokens_and_responsive_accessibility_contract_are_present():
    tokens = _read(STATIC / "css" / "design-tokens.css")
    shell = _read(STATIC / "css" / "app-shell.css")

    for value in ("#0B5D59", "#073F3C", "#E7F1EF", "#F4F7F6", "#17201F"):
        assert value.lower() in tokens.lower()
    assert "224px" in tokens
    assert "58px" in tokens
    assert "@media (max-width: 1279px)" in shell
    assert "64px" in shell
    assert "focus-visible" in shell
    assert "prefers-reduced-motion" in shell
    assert "overflow-x: hidden" in shell
    assert ".empty-state" in tokens
    assert ".skeleton" in tokens
    assert ".toast-container" in tokens


def test_navigation_grouping_and_narrow_state_are_not_misleading():
    source = _read(TEMPLATES / "base.html")
    shell_script = _read(STATIC / "js" / "app-shell.js")

    resources_rule = next(
        line for line in shell_script.splitlines()
        if "key: 'resources'" in line
    )
    tools_rule = next(
        line for line in shell_script.splitlines()
        if "key: 'tools'" in line
    )
    assert "'/templates'" in resources_rule
    assert "'/templates'" not in tools_rule
    assert "matchMedia('(max-width: 1279px)')" in shell_script
    assert "collapsePrimary.disabled = isAutoCollapsed" in shell_script
    assert 'type="search"' not in source
    assert "请在各模块内搜索" in source


def test_dashboard_is_operational_workspace_not_marketing_hero():
    source = _read(TEMPLATES / "index.html")

    assert "新建科研提案" in source
    assert "常用资源" in source
    assert "待我推进" in source
    assert "最近更新" in source
    assert "dashboard.summary" in source
    assert "hero-bg" not in source
    assert "animation: gridMove" not in source
    for href in ("/proposals/new", "/projects", "/experts", "/equipment", "/utils"):
        assert f'href="{href}"' in source
    assert 'href="/projects?status=执行中"' in source
    assert 'href="/projects?status=结题上报"' in source


def test_proposal_ui_keeps_confirmed_interaction_safeguards():
    form = _read(TEMPLATES / "proposals" / "form.html")
    detail = _read(TEMPLATES / "proposals" / "detail.html")
    assistant = _read(TEMPLATES / "proposals" / "_assistant.html")
    styles = _read(STATIC / "css" / "proposal-workflow.css")

    assert "request.method == 'GET' and not proposal and not form" in form
    assert "form.dataset.submitting" in form
    assert "form.dataset.submitting" in detail
    assert "assistant-current" in assistant
    assert "assistant-proposed" in assistant
    assert "length > 3" in assistant
    assert 'href="#decision-records"' in detail
    assert '@media (max-width: 1399px)' in styles
