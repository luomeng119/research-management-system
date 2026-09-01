# -*- coding: utf-8 -*-
"""
match_subclass() 单元测试 — 防止返回值类型回归到 dict

[REQ-011-fix] 此文件保证：
  - 'matched' 字段必须是 str（不是 dict），否则 Jinja2 模板 `{{ sr.matched }}`
    会把整个 dict 渲染成 `{'id': 1, 'subclass_name': '防火墙', ...}` 字符串泄漏到 UI
  - 'candidates' 字段必须是 list[str]

运行方法：python test/test_match_subclass.py

背景：2026-05-25 设备知识库导入步骤2 出现 dict 字符串泄漏 bug
      （截图证据：screenshots/bug_reproduce/BUG_repro_step2.png）
      根因：match_subclass() 返回 matched=s（dict），import_step2.html 用
      `{{ sr.matched or sr.original }}` 把 dict 当字符串渲染。
      修复：match_subclass() 改为返回 matched=s.get('subclass_name')（字符串）。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils.fuzzy_match import match_subclass


class MockSubclassModel:
    """模拟 KnowledgeSubclassModel.get_all() 的返回值"""
    def __init__(self, data):
        self._data = data

    def get_all(self):
        return self._data


# ============================================================
# 测试用例
# ============================================================

def test_exact_match_returns_string():
    """精确匹配：matched 必须是 str '防火墙'，exact=True, method='exact'"""
    model = MockSubclassModel([
        {'id': 1, 'parent_category': '安全设备', 'subclass_name': '防火墙', 'created_at': '2026-01-01'},
        {'id': 2, 'parent_category': '安全设备', 'subclass_name': '入侵检测', 'created_at': '2026-01-01'},
    ])
    result = match_subclass('防火墙', model, parent_category='安全设备')

    assert isinstance(result['matched'], str), \
        f"[BUG] matched 必须是 str，但得到 {type(result['matched']).__name__}: {result['matched']!r}"
    assert result['matched'] == '防火墙', f"matched 应为 '防火墙'，得到 {result['matched']!r}"
    assert result['exact'] is True
    assert result['method'] == 'exact'
    assert result['candidates'] == []
    print("✓ test_exact_match_returns_string 通过")


def test_normalized_match_returns_string():
    """规范化匹配：matched 必须是字符串"""
    model = MockSubclassModel([
        {'id': 1, 'parent_category': '安全设备', 'subclass_name': '防火墙', 'created_at': '2026-01-01'},
    ])
    # '防火强' 是 '防火墙' 的编辑距离 1 变体（也可能 normalized 命中）
    result = match_subclass('防火强', model, parent_category='安全设备')

    assert isinstance(result['matched'], str), \
        f"[BUG] matched 必须是 str，但得到 {type(result['matched']).__name__}: {result['matched']!r}"
    assert result['matched'] == '防火墙', f"matched 应为 '防火墙'，得到 {result['matched']!r}"
    assert result['method'] in ('normalized', 'fuzzy'), \
        f"method 应为 normalized/fuzzy，得到 {result['method']!r}"
    print("✓ test_normalized_match_returns_string 通过")


def test_fuzzy_match_returns_string_with_candidates():
    """模糊匹配：matched 是字符串，candidates 是字符串列表"""
    model = MockSubclassModel([
        {'id': 1, 'parent_category': '安全设备', 'subclass_name': '防火墙', 'created_at': '2026-01-01'},
        {'id': 2, 'parent_category': '安全设备', 'subclass_name': '入侵检测', 'created_at': '2026-01-01'},
        {'id': 3, 'parent_category': '安全设备', 'subclass_name': '漏洞扫描', 'created_at': '2026-01-01'},
    ])
    # '防火强' 模糊匹配到 '防火墙'（编辑距离 1）
    result = match_subclass('防火强', model, parent_category='安全设备')

    assert isinstance(result['matched'], str), \
        f"[BUG] matched 必须是 str，但得到 {type(result['matched']).__name__}: {result['matched']!r}"
    # candidates 必须全是字符串
    for c in result['candidates']:
        assert isinstance(c, str), \
            f"[BUG] candidates 元素必须是 str，但得到 {type(c).__name__}: {c!r}"
    print(f"  (matched={result['matched']!r}, method={result['method']!r}, candidates={result['candidates']!r})")
    print("✓ test_fuzzy_match_returns_string_with_candidates 通过")


def test_no_match_returns_none():
    """无匹配：matched=None, candidates=[]"""
    model = MockSubclassModel([
        {'id': 1, 'parent_category': '安全设备', 'subclass_name': '防火墙', 'created_at': '2026-01-01'},
    ])
    result = match_subclass('完全不相关xyz12345', model, parent_category='安全设备')

    assert result['matched'] is None
    assert result['exact'] is False
    assert result['method'] == 'none'
    assert result['candidates'] == []
    print("✓ test_no_match_returns_none 通过")


def test_empty_input():
    """空输入：matched=None"""
    model = MockSubclassModel([
        {'id': 1, 'parent_category': '安全设备', 'subclass_name': '防火墙', 'created_at': '2026-01-01'},
    ])
    result = match_subclass('', model, parent_category='安全设备')
    assert result['matched'] is None
    assert result['method'] == 'none'

    result2 = match_subclass('   ', model, parent_category='安全设备')
    assert result2['matched'] is None
    assert result2['method'] == 'none'
    print("✓ test_empty_input 通过")


def test_no_model():
    """无 model：matched=None"""
    result = match_subclass('防火墙', None, parent_category='安全设备')
    assert result['matched'] is None
    assert result['method'] == 'none'

    result2 = match_subclass('防火墙', model=None, parent_category='安全设备')
    assert result2['matched'] is None
    assert result2['method'] == 'none'
    print("✓ test_no_model 通过")


def test_fallback_to_other_category():
    """fallback：parent_category 没匹配时，去其他分类找"""
    model = MockSubclassModel([
        {'id': 1, 'parent_category': '密码设备', 'subclass_name': '密码机', 'created_at': '2026-01-01'},
        {'id': 2, 'parent_category': '安全设备', 'subclass_name': '防火墙', 'created_at': '2026-01-01'},
    ])
    # 传入 '安全设备' 分类，找 '密码机'（不在同分类 → fallback 到 other_category）
    result = match_subclass('密码机', model, parent_category='安全设备')

    assert isinstance(result['matched'], str)
    assert result['matched'] == '密码机'
    assert result['method'] == 'exact'
    print("✓ test_fallback_to_other_category 通过")


def test_regression_not_dict():
    """【防回归核心测试】matched 不能是 dict 类型"""
    model = MockSubclassModel([
        {'id': 1, 'parent_category': '安全设备', 'subclass_name': '防火墙', 'created_at': '2026-01-01'},
        {'id': 2, 'parent_category': '安全设备', 'subclass_name': '入侵检测', 'created_at': '2026-01-01'},
        {'id': 3, 'parent_category': '安全设备', 'subclass_name': '漏洞扫描', 'created_at': '2026-01-01'},
    ])
    # 精确匹配
    r1 = match_subclass('防火墙', model, parent_category='安全设备')
    assert not isinstance(r1['matched'], dict), \
        f"[REGRESSION] exact matched 又变成 dict 了: {r1['matched']!r}"
    # 模糊匹配（防火墙→防火强）
    r2 = match_subclass('防火强', model, parent_category='安全设备')
    assert not isinstance(r2['matched'], dict), \
        f"[REGRESSION] fuzzy matched 又变成 dict 了: {r2['matched']!r}"
    # candidates 也不能是 dict 列表
    for c in r2['candidates']:
        assert not isinstance(c, dict), \
            f"[REGRESSION] candidates 又包含 dict 了: {c!r}"
    print("✓ test_regression_not_dict 通过（防回归核心）")


def test_no_parent_category_filter():
    """不传 parent_category：在所有子类中搜索"""
    model = MockSubclassModel([
        {'id': 1, 'parent_category': '密码设备', 'subclass_name': '密码机', 'created_at': '2026-01-01'},
        {'id': 2, 'parent_category': '安全设备', 'subclass_name': '防火墙', 'created_at': '2026-01-01'},
    ])
    result = match_subclass('防火墙', model, parent_category=None)
    assert isinstance(result['matched'], str)
    assert result['matched'] == '防火墙'
    print("✓ test_no_parent_category_filter 通过")


def test_candidates_are_strings_not_dicts():
    """candidates 元素必须是字符串而非 dict（防回归 2）"""
    model = MockSubclassModel([
        {'id': 1, 'parent_category': '安全设备', 'subclass_name': '防火墙', 'created_at': '2026-01-01'},
        {'id': 2, 'parent_category': '安全设备', 'subclass_name': '入侵检测', 'created_at': '2026-01-01'},
    ])
    # '防火强' 模糊匹配到 '防火墙'，candidates 应包含 '防火墙'（字符串）
    result = match_subclass('防火强', model, parent_category='安全设备')
    if result['candidates']:
        for c in result['candidates']:
            assert isinstance(c, str), \
                f"[BUG] candidate 应为 str，实际为 {type(c).__name__}: {c!r}"
            # 不能含 dict 特征字符
            assert '{' not in c, f"[BUG] candidate 含 dict 字符串: {c!r}"
    print(f"  candidates={result['candidates']!r}")
    print("✓ test_candidates_are_strings_not_dicts 通过")


if __name__ == '__main__':
    test_exact_match_returns_string()
    test_normalized_match_returns_string()
    test_fuzzy_match_returns_string_with_candidates()
    test_no_match_returns_none()
    test_empty_input()
    test_no_model()
    test_fallback_to_other_category()
    test_regression_not_dict()
    test_no_parent_category_filter()
    test_candidates_are_strings_not_dicts()
    print(f"\n✅ 所有 10 个测试通过 — match_subclass() 返回值类型契约保持正确")
