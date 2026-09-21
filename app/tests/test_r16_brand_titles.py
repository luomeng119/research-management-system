from pathlib import Path
import re

import pytest
from jinja2 import Template


TEMPLATES = Path(__file__).parents[1] / "templates"
PREFIXES = {
    "experts/index.html": "专家库",
    "experts/add.html": "添加专家",
    "experts/edit.html": "编辑专家",
    "expense/documents.html": "报销单据",
    "expense/document_new.html": "新建单据 - 报销单据",
    "expense/document_templates.html": "模板管理 - 报销单据",
    "expense/document_print.html": "汇总打印 - 报销单据",
    "expense/approvals.html": "审批单台账 - 报销助手",
    "expense/records.html": "报销项列表 - 报销助手",
    "utils/index.html": "辅助工具",
    "utils/monitor.html": "状态监控 - 辅助工具",
    "utils/model_config.html": "模型配置 - 辅助工具",
    "utils/document_correction.html": "文档校对 - 辅助工具",
}


@pytest.mark.parametrize("path,prefix", PREFIXES.items())
def test_changed_brand_title_preserves_function_prefix(path, prefix):
    source = (TEMPLATES / path).read_text()
    title = re.search(r"{% block title %}(.*?){% endblock %}", source, re.S).group(1)
    assert title == prefix + " - 科研创新管理"


def test_no_legacy_product_name_in_template_titles():
    for path in TEMPLATES.rglob("*.html"):
        source = path.read_text()
        titles = re.findall(r"{% block title %}(.*?){% endblock %}|<title[^>]*>(.*?)</title>", source, re.S)
        assert all("科研管理系统" not in part for match in titles for part in match), str(path)


@pytest.mark.parametrize("context,expected", [
    ({"project_type": "projects"}, "科研项目"),
    ({"project_type": "security_projects"}, "安全保密项目"),
    ({"project_type": "crypto_projects"}, "密码应用项目"),
    ({"project_type": "unknown"}, "unknown"),
    ({}, "科研项目"),
])
def test_project_title_renders_existing_category_names(context, expected):
    source = (TEMPLATES / "projects/index.html").read_text()
    title = re.search(r"{% block title %}(.*?){% endblock %}", source, re.S).group(1)
    assert Template(title).render(**context) == expected + " - 科研创新管理"
