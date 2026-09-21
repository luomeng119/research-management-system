from html.parser import HTMLParser
from pathlib import Path

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def test_local_model_settings_has_only_controlled_choices_and_accessible_actions():
    templates = Path(__file__).parents[1] / 'templates'
    env = Environment(loader=ChoiceLoader([
        DictLoader({'base.html': '{% block content %}{% endblock %}'}),
        FileSystemLoader(templates),
    ]), autoescape=True)
    html = env.get_template('utils/local_model_status.html').render()
    parsed = Elements()
    parsed.feed(html)
    inputs = [attrs for tag, attrs in parsed.elements if tag == 'input']
    assert {item['value'] for item in inputs} == {'qwen3.5-9b-q8'}
    assert all(item['type'] == 'radio' and 'disabled' in item for item in inputs)
    assert all(any(tag == 'label' and attrs.get('for') == item['id'] for tag, attrs in parsed.elements) for item in inputs)
    buttons = {attrs.get('id'): attrs for tag, attrs in parsed.elements if tag == 'button'}
    assert set(buttons) == {'loadModel', 'unloadModel', 'refreshModelStatus'}
    assert all(attrs.get('type') == 'button' for attrs in buttons.values())
    assert '当前系统只允许加载Qwen3.5-9B' in html
    assert 'DeepSeek API 已关闭' in html
    assert '未采样' in html and '选择不会自动加载' in html
    assert 'role="status"' in html and '<fieldset' in html
    assert 'type="file"' not in html and 'name="file_path"' not in html
