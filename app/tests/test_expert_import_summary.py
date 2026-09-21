from pathlib import Path

from jinja2 import DictLoader, Environment


def test_duplicate_preview_summary_does_not_claim_missing_name():
    source = (Path(__file__).parents[1] / "templates/experts/import_step2.html").read_text()
    template = Environment(loader=DictLoader({
        "base.html": "{% block content %}{% endblock %}",
        "experts/import_step2.html": source,
    })).get_template("experts/import_step2.html")
    html = template.render(
        processed=[{"name": "王老师", "_row_idx": 0, "_error": "专家已存在，跳过重复记录"}],
        statistics={"valid": 0, "skip": 1, "total": 1},
        filename="演练专家导入.xlsx",
    )
    assert "跳过（缺姓名）" not in html
    assert 'id="skipCount">1</span>' in html
    assert "原因见各行状态" in html
    assert "专家已存在，跳过重复记录" in html


def test_completed_summary_does_not_assign_a_false_skip_reason():
    source = (Path(__file__).parents[1] / "templates/experts/import_done.html").read_text()
    template = Environment(loader=DictLoader({
        "base.html": "{% block content %}{% endblock %}",
        "experts/import_done.html": source,
    })).get_template("experts/import_done.html")
    html = template.render(results={"success": [], "fail": [], "skip": 1})
    assert "跳过（缺姓名）" not in html
    assert '<div class="label">跳过</div>' in html
