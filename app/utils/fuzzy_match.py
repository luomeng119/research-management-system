# -*- coding: utf-8 -*-
"""
模糊匹配引擎
给定任意设备名称/型号字符串，匹配到 host_devices 或 equipment 表中对应记录
"""
import re
from app.models import get_db

# 去噪声词列表
NOISE_PATTERNS = [
    r'[型号]', r'[设备]', r'[vV]\d+', r'[（简称）]', r'[()]', r'[（]同[）]',
    r'\s+', r'^\s+|\s+$'
]
NOISE_REGEX = re.compile('|'.join(NOISE_PATTERNS))


def _normalize(text: str) -> str:
    """去除噪声词"""
    if not text:
        return ''
    t = str(text).strip()
    t = re.sub(r'[、，,]+', '', t)  # 保留顿号用于拆分，本身去掉
    t = re.sub(NOISE_REGEX, '', t)
    return t.strip()


def split_device_names(text: str) -> list[str]:
    """
    将 '数据中心服务器、XX-200密码设备' 拆分为子串列表
    支持多种分隔符（避免遗漏导致关联设备匹配不全）：
    - 顿号「、」
    - 中英文逗号「，」「,」
    - 中英文分号「；」「;」
    - 斜杠「/」
    - 竖线「|」
    - 多空格 / Tab
    """
    if not text:
        return []
    text = str(text).strip()
    # [REQ-013-fix] 补充分号/斜杠/竖线/空白分隔符（回归 bug：测试 xlsx 用了「；」和「/」）
    parts = re.split(r'[、，,；;/|]|\s{2,}|\t+', text)
    return [p.strip() for p in parts if p.strip()]


def _levenshtein(s1: str, s2: str) -> int:
    """编辑距离"""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row
    return prev_row[-1]


def _match(text: str, table: str, id_col: str, name_col: str, model_col: str) -> dict:
    """
    通用匹配逻辑

    参数:
        table: 表名 ('equipment' 或 'host_devices')
        id_col: ID列名
        name_col: name列名
        model_col: model列名

    返回:
        dict: {
            'original': 原始字符串,
            'devices': [
                {
                    'name': 子串,
                    'exact': [命中的设备dict列表],
                    'fuzzy': [模糊候选dict列表],
                    'status': 'exact' | 'fuzzy' | 'unmatched',
                    'selected': 命中的dict或None
                }
            ]
        }
    """
    names = split_device_names(text)
    if not names:
        return {'original': text, 'devices': []}

    result = {'original': text, 'devices': []}

    for sub_name in names:
        sub_name = sub_name.strip()
        if not sub_name:
            continue

        exact = []
        fuzzy = []

        with get_db() as conn:
            c = conn.cursor()

            # 1. 精确匹配: name = sub_name or model = sub_name
            c.execute(
                f"SELECT * FROM {table} WHERE {name_col} = ? OR {model_col} = ?",
                (sub_name, sub_name)
            )
            exact = [dict(row) for row in c.fetchall()]

            if exact:
                result['devices'].append({
                    'name': sub_name,
                    'exact': exact,
                    'fuzzy': [],
                    'status': 'exact',
                    'selected': exact[0]
                })
                continue

            # 2. 去噪声匹配
            cleaned = _normalize(sub_name)
            if cleaned and cleaned != sub_name:
                c.execute(
                    f"SELECT * FROM {table} WHERE {name_col} LIKE ? OR {model_col} LIKE ?",
                    (f'%{cleaned}%', f'%{cleaned}%')
                )
                noise_matches = [dict(row) for row in c.fetchall()]
                if noise_matches:
                    exact = noise_matches
                    result['devices'].append({
                        'name': sub_name,
                        'exact': [],
                        'fuzzy': exact,
                        'status': 'fuzzy',
                        'selected': None
                    })
                    continue

            # 3. 模糊相似度匹配: 编辑距离 < 3 且长度 > 5，或包含匹配
            c.execute(f"SELECT * FROM {table}")
            all_rows = [dict(row) for row in c.fetchall()]

            for row in all_rows:
                row_name = row.get(name_col, '') or ''
                row_model = row.get(model_col, '') or ''
                # 包含匹配
                if (cleaned and (
                    cleaned in row_name or cleaned in row_model or
                    row_name in cleaned or row_model in cleaned
                )):
                    fuzzy.append(row)
                    continue
                # 编辑距离 < 3 且长度 > 5
                if len(sub_name) > 5 and len(cleaned) > 5:
                    if (_levenshtein(sub_name, row_name) < 3 or
                        _levenshtein(cleaned, row_name) < 3 or
                        _levenshtein(sub_name, row_model) < 3):
                        fuzzy.append(row)

            # 去重
            seen_ids = set()
            fuzzy_unique = []
            for f in fuzzy:
                fid = f.get(id_col)
                if fid not in seen_ids:
                    seen_ids.add(fid)
                    fuzzy_unique.append(f)

            if fuzzy_unique:
                result['devices'].append({
                    'name': sub_name,
                    'exact': [],
                    'fuzzy': fuzzy_unique,
                    'status': 'fuzzy',
                    'selected': None
                })
            else:
                result['devices'].append({
                    'name': sub_name,
                    'exact': [],
                    'fuzzy': [],
                    'status': 'unmatched',
                    'selected': None
                })

    return result


def match_equipment(text: str) -> dict:
    """
    在 equipment 表中模糊匹配
    返回: {'original': str, 'devices': [{'name', 'exact', 'fuzzy', 'status', 'selected'}]}
    """
    return _match(text, 'equipment', 'equipment_id', 'name', 'model')


def match_host_device(text: str) -> dict:
    """
    在 host_devices 表中模糊匹配
    返回: {'original': str, 'devices': [{'name', 'exact', 'fuzzy', 'status', 'selected'}]}
    """
    return _match(text, 'host_devices', 'host_id', 'name', 'model')


def fuzzy_search_equipment(keyword: str, limit: int = 10) -> list[dict]:
    """供 API 调用，返回简单列表"""
    if not keyword or len(keyword.strip()) < 1:
        return []
    keyword = keyword.strip()
    results = []
    with get_db() as conn:
        c = conn.cursor()
        # 精确优先，再模糊
        c.execute(
            "SELECT equipment_id, name, model FROM equipment "
            "WHERE name LIKE ? OR model LIKE ? "
            "LIMIT ?",
            (f'%{keyword}%', f'%{keyword}%', limit * 2)
        )
        seen = set()
        for row in c.fetchall():
            rid = row['equipment_id']
            if rid not in seen:
                seen.add(rid)
                results.append({
                    'id': rid,
                    'name': row['name'],
                    'model': row['model'] or ''
                })
                if len(results) >= limit:
                    break
    return results


def fuzzy_search_host_device(keyword: str, limit: int = 10) -> list[dict]:
    """供 API 调用，返回简单列表"""
    if not keyword or len(keyword.strip()) < 1:
        return []
    keyword = keyword.strip()
    results = []
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT host_id, name, model FROM host_devices "
            "WHERE name LIKE ? OR model LIKE ? "
            "LIMIT ?",
            (f'%{keyword}%', f'%{keyword}%', limit * 2)
        )
        seen = set()
        for row in c.fetchall():
            rid = row['host_id']
            if rid not in seen:
                seen.add(rid)
                results.append({
                    'id': rid,
                    'name': row['name'],
                    'model': row['model'] or ''
                })
                if len(results) >= limit:
                    break
    return results


def match_research_unit(unit_name: str, model) -> dict:
    """
    模糊匹配研制单位
    供 REQ-009 导入功能和研制单位管理调用
    
    参数:
        unit_name: 待匹配的研制单位名称
        model: ResearchUnitModel 实例
    
    返回:
        dict: {
            'matched': matched unit dict or None,
            'exact': bool,
            'method': 'exact' | 'alias' | 'normalized' | 'fuzzy' | 'none',
            'candidates': [候选unit dict列表] (fuzzy模式时)
        }
    """
    if not unit_name or not model:
        return {'matched': None, 'exact': False, 'method': 'none', 'candidates': []}
    
    unit_name = str(unit_name).strip()
    if not unit_name:
        return {'matched': None, 'exact': False, 'method': 'none', 'candidates': []}
    
    all_units = model.get_all()
    if not all_units:
        return {'matched': None, 'exact': False, 'method': 'none', 'candidates': []}
    
    # 1. 精确匹配 name
    for u in all_units:
        if u.get('name') == unit_name:
            return {'matched': u, 'exact': True, 'method': 'exact', 'candidates': []}
    
    # 2. 精确匹配 alias（alias 以 "/" 分隔多个名称）
    for u in all_units:
        alias = u.get('alias', '')
        if alias:
            aliases = [a.strip() for a in alias.split('/') if a.strip()]
            if unit_name in aliases:
                return {'matched': u, 'exact': True, 'method': 'alias', 'candidates': []}
    
    # 3. 规范化匹配（去除噪声后匹配）
    normalized_input = _normalize(unit_name)
    if normalized_input and normalized_input != unit_name:
        for u in all_units:
            n_name = _normalize(u.get('name', ''))
            if n_name and (normalized_input in n_name or n_name in normalized_input):
                return {'matched': u, 'exact': False, 'method': 'normalized', 'candidates': []}
    
    # 4. 模糊匹配（编辑距离 < 3）
    candidates = []
    for u in all_units:
        u_name = u.get('name', '')
        if len(unit_name) > 4 and len(u_name) > 4:
            dist = _levenshtein(unit_name, u_name)
            if dist <= 2:
                candidates.append({'unit': u, 'distance': dist})
    
    # 按编辑距离排序
    candidates.sort(key=lambda x: x['distance'])
    if candidates:
        return {
            'matched': candidates[0]['unit'],
            'exact': False,
            'method': 'fuzzy',
            'candidates': [c['unit'] for c in candidates[:3]]
        }

    return {'matched': None, 'exact': False, 'method': 'none', 'candidates': []}


def match_subclass(subclass_name: str, model, parent_category: str | None = None) -> dict:
    """
    模糊匹配设备子类

    返回:
        {
            'matched': str | None,           # 匹配到的子类名称（字符串，不是 dict）
            'exact': bool,                   # 是否精确匹配
            'method': str,                   # 'exact' | 'normalized' | 'fuzzy' | 'none'
            'candidates': list[str],         # 候选子类名（字符串列表，最多 3 个，按距离升序）
        }

    供 REQ-011 设备知识库导入功能和子类管理调用。

    [REQ-011-fix] matched/candidates 字段从 dict 改为字符串，
    避免 Jinja2 模板 `{{ sr.matched }}` 渲染时显示完整 dict 字符串（如
    "{'id': 1, 'subclass_name': '防火墙', 'created_at': ...}"）。
    """
    if not subclass_name or not model:
        return {'matched': None, 'exact': False, 'method': 'none', 'candidates': []}

    subclass_name = str(subclass_name).strip()
    if not subclass_name:
        return {'matched': None, 'exact': False, 'method': 'none', 'candidates': []}

    all_subclasses = model.get_all()
    if not all_subclasses:
        return {'matched': None, 'exact': False, 'method': 'none', 'candidates': []}

    if parent_category:
        same_category = [s for s in all_subclasses if s.get('parent_category') == parent_category]
        other_category = [s for s in all_subclasses if s.get('parent_category') != parent_category]
    else:
        same_category = all_subclasses
        other_category = []

    def try_match(subclasses):
        for s in subclasses:
            if s.get('subclass_name') == subclass_name:
                # [REQ-011-fix] 返回字符串而非 dict
                return {'matched': s.get('subclass_name', ''), 'exact': True, 'method': 'exact', 'candidates': []}

        normalized_input = _normalize(subclass_name)
        if normalized_input and normalized_input != subclass_name:
            for s in subclasses:
                n_name = _normalize(s.get('subclass_name', ''))
                if n_name and (normalized_input in n_name or n_name in normalized_input):
                    # [REQ-011-fix] 返回字符串而非 dict
                    return {'matched': s.get('subclass_name', ''), 'exact': False, 'method': 'normalized', 'candidates': []}

        candidates = []
        for s in subclasses:
            s_name = s.get('subclass_name', '')
            if len(subclass_name) > 2 and len(s_name) > 2:
                dist = _levenshtein(subclass_name, s_name)
                if dist <= 2:
                    # [REQ-011-fix] 存字符串名称而非 dict
                    candidates.append({'subclass': s_name, 'distance': dist})

        candidates.sort(key=lambda x: x['distance'])
        if candidates:
            return {
                # [REQ-011-fix] 返回字符串而非 dict
                'matched': candidates[0]['subclass'],
                'exact': False,
                'method': 'fuzzy',
                # [REQ-011-fix] 返回字符串列表而非 dict 列表
                'candidates': [c['subclass'] for c in candidates[:3]]
            }
        return None

    if same_category:
        result = try_match(same_category)
        if result:
            return result

    if other_category:
        result = try_match(other_category)
        if result:
            return result

    return {'matched': None, 'exact': False, 'method': 'none', 'candidates': []}
