from pathlib import Path

from jinja2 import DictLoader, Environment


def test_existing_approval_page_emits_its_list_loader():
    source = (Path(__file__).parents[1] / "templates/expense/approvals.html").read_text()
    template = Environment(loader=DictLoader({
        "base.html": "{% block content %}{% endblock %}{% block extra_js %}{% endblock %}",
        "components/preview_panel.html": "",
        "components/list_preview_helper.html": "",
        "expense/approvals.html": source,
    })).get_template("expense/approvals.html")
    html = template.render()
    assert "fetch('/expense/api/reimbursements'" in html
    assert "loadRecords();" in html
