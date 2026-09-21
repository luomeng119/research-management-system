from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_leadership_intro_assets_are_local_and_complete():
    template = (ROOT / "templates" / "leadership" / "system_intro.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "leadership-intro.css").read_text(encoding="utf-8")
    script = (ROOT / "static" / "js" / "leadership-intro.js").read_text(encoding="utf-8")

    for section in ("overview", "workflow", "features", "data-flow", "local-ai", "architecture", "delivery"):
        assert f'data-intro-target="{section}"' in template
        assert f'data-intro-section="{section}"' in template

    for diagram in ("intro-overview-diagram", "flow-diagram", "feature-map", "document-flow-diagram", "ai-boundary", "architecture-diagram", "delivery-diagram"):
        assert diagram in template

    combined = template + css + script
    assert "https://" not in combined
    assert "cdnjs" not in combined
    assert "unpkg" not in combined
    assert "background-image: url(" not in combined
    assert "辅助工具 → 敏感词库维护" in template
    assert "按版本维护" in template


def test_login_contract_and_intro_link_are_preserved():
    login = (ROOT / "templates" / "login.html").read_text(encoding="utf-8")

    assert 'id="login-username"' in login
    assert 'name="username"' in login
    assert 'id="login-password"' in login
    assert 'name="password"' in login
    assert 'name="_csrf_token"' in login
    assert 'href="/system-intro"' in login


def test_existing_ai_entries_have_consistent_visible_markers():
    templates = (
        ROOT / "templates" / "proposals" / "form.html",
        ROOT / "templates" / "proposals" / "_assistant.html",
        ROOT / "templates" / "research_reports" / "selection.html",
        ROOT / "templates" / "research_reports" / "wording.html",
        ROOT / "templates" / "research_reports" / "sources.html",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in templates)

    assert combined.count('class="ai-action-badge"') >= 6
    assert "提案助手" in combined
    assert "整合报告草稿" in combined
    assert "选区措辞建议" in combined
    assert "图片内容由本地模型提取" in combined
