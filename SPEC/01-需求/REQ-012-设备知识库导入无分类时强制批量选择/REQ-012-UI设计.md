# REQ-012 UI 设计

**状态**: 阶段三（UI 设计）— 待用户确认
**基于**: REQ-012-需求设计.md（决策1A+2A 已确认）

---

## 改动范围

只改 **2 个页面 / 1 个路由**，其他不动：

| 文件 | 改动 | 影响 |
|------|------|------|
| `app/templates/equipment/import_step1.html` | 大改（删字段+加单选+JS） | 步骤1 UI |
| `app/templates/equipment/import_step2.html` | 小改（分类列允许编辑）| 步骤2 表格 |
| `app/routes/equipment.py` | 小改（preview 读 default_category） | 后端 |

---

## 1. 步骤1 完整 UI（关键改动）

### 1.1 改动后完整页面布局

```
┌─────────────────────────────────────────────────────────────────────┐
│ ← 返回列表                                                          │
│ 设备知识库 - 导入（步骤1/3）                                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  📁 拖拽 xlsx 文件到此处，或点击选择文件                        │  │
│  │  支持 .xlsx 格式                                                │  │
│  │              [选择文件 - 隐藏 input]                              │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  文件名: 设备知识库_基础场景.xlsx        [开始解析]                   │
│                                                                      │
├─────────────────────────────────────────────────────────────────────┤
│ 步骤1：列映射                                                       │
│ 请为每列选择对应字段（关联宿主设备为可选项）                          │
│                                                                      │
│ ┌────┬─────────┬─────────┬─────────┬─────────┬─────────┬─────────┐│
│ │ #  │ 设备名称 │ 型号     │ 分类     │ 单价     │ 生产厂商 │ ...     ││
│ │ 1  │ name  ▼ │ model ▼│ (未映射)│ price ▼│  (未映射)│ ...     ││
│ │    │         │         │ ↓        │        │ ↓        │         ││
│ │    │         │         │(无选项)  │        │(无选项)  │         ││
│ └────┴─────────┴─────────┴─────────┴─────────┴─────────┴─────────┘│
│ ↑                                                                    │
│ ║  "分类"列的下拉框只有"-- 不导入 --"选项（没有"分类"字段选项）     ││
│ ║                                                                    ││
│ ⚠ 有 2 列未映射（将忽略：分类、生产厂商）                            │
│                                                                      │
├─────────────────────────────────────────────────────────────────────┤
│ 批量分类（必选）                                                    │
│ 所有本次导入的设备将统一归入以下分类：                                │
│                                                                      │
│ ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                   │
│ │  ○          │  │  ○          │  │  ○          │                   │
│ │  安全设备   │  │  密码设备   │  │  通用设备   │                   │
│ │             │  │             │  │             │                   │
│ └─────────────┘  └─────────────┘  └─────────────┘                   │
│                                                                      │
│  ※ 未选择分类前，下方"下一步"按钮为禁用状态                          │
│                                                                      │
│                                              [下一步：匹配预览 →]   │
│                                              ^^^^^^^^^^^^^^^^^^    │
│                                              未选分类时：disabled    │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.2 关键改动点

**改动 A — 列映射下拉框移除"分类"选项**

```diff
  const fieldOptions = `
      <option value="">-- 不导入 --</option>
      <option value="equipment_id">设备编号</option>
      <option value="name">设备名称</option>
      <option value="former_name">曾用名</option>
      <option value="model">型号</option>
-     <option value="category">分类</option>
      <option value="form">形态</option>
      ...
  `;
```

**改动 B — 列映射自动识别 patterns 移除 'category'**

```diff
  const patterns = {
      'equipment_id': /编号|设备编号|eq_id/i,
      'name': /名称|设备名称|name/i,
      'former_name': /曾用|别名|former/i,
      'model': /型号|model/i,
-     'category': /分类|category|类型/i,
      'form': /形态|form/i,
      ...
  };
```

**改动 C — 步骤1 末尾增加分类单选卡片 UI**

```html
<!-- 在 mappingSection 之后、底部按钮之前插入 -->
<div id="categorySection" class="card mb-3">
    <div class="card-header">
        <strong>批量分类（必选）</strong>
        <span class="text-secondary ms-3" style="font-size:13px;">
            所有本次导入的设备将统一归入以下分类
        </span>
    </div>
    <div class="card-body">
        <div class="d-flex gap-3" id="categoryRadios">
            <label class="category-radio flex-fill">
                <input type="radio" name="default_category_radio" value="安全设备" class="d-none">
                <div class="radio-card">
                    <i class="bi bi-shield-check"></i>
                    <div class="radio-title">安全设备</div>
                </div>
            </label>
            <label class="category-radio flex-fill">
                <input type="radio" name="default_category_radio" value="密码设备" class="d-none">
                <div class="radio-card">
                    <i class="bi bi-key"></i>
                    <div class="radio-title">密码设备</div>
                </div>
            </label>
            <label class="category-radio flex-fill">
                <input type="radio" name="default_category_radio" value="通用设备" class="d-none">
                <div class="radio-card">
                    <i class="bi bi-hdd"></i>
                    <div class="radio-title">通用设备</div>
                </div>
            </label>
        </div>
        <div class="text-secondary mt-2" style="font-size:12px;">
            ※ 未选择分类前，"下一步"按钮为禁用状态
        </div>
    </div>
</div>
```

**改动 D — CSS 样式（卡片式单选按钮）**

```css
.radio-card {
    border: 2px solid #e0e0e0;
    border-radius: 8px;
    padding: 20px;
    text-align: center;
    cursor: pointer;
    transition: all 0.2s;
    background: #fafafa;
}
.radio-card:hover {
    border-color: #4a9eff;
    background: #f0f7ff;
}
.category-radio input[type="radio"]:checked + .radio-card {
    border-color: #1677ff;
    background: #e6f4ff;
    box-shadow: 0 0 0 3px rgba(22, 119, 255, 0.1);
}
.radio-title {
    font-size: 16px;
    font-weight: 500;
    margin-top: 8px;
    color: #333;
}
```

**改动 E — JS 监听单选变化控制"下一步"按钮**

```javascript
// 监听分类单选
document.querySelectorAll('input[name="default_category_radio"]').forEach(radio => {
    radio.addEventListener('change', updateStartMatchBtn);
});

function updateStartMatchBtn() {
    const selectedCategory = document.querySelector('input[name="default_category_radio"]:checked');
    const startBtn = document.getElementById('startMatchBtn');
    if (selectedCategory) {
        startBtn.removeAttribute('disabled');
        startBtn.classList.remove('btn-secondary');
        startBtn.classList.add('btn-primary');
    } else {
        startBtn.setAttribute('disabled', 'disabled');
        startBtn.classList.add('btn-secondary');
        startBtn.classList.remove('btn-primary');
    }
}

// 初始化时按钮 disabled
updateStartMatchBtn();
```

**改动 F — startMatchBtn 提交时附加 default_category**

```javascript
// 在已有的 startMatchBtn click handler 里修改
document.getElementById('startMatchBtn').addEventListener('click', () => {
    // 已有：检查 fileInput.files.length
    if (!fileInput.files.length) { alert('请先上传文件'); return; }

    // 新增：检查分类必选
    const selectedCategory = document.querySelector('input[name="default_category_radio"]:checked');
    if (!selectedCategory) {
        alert('请选择批量分类');
        return;
    }

    // 收集列映射（已有代码）
    const columnMapping = {};
    document.querySelectorAll('.column-map').forEach(sel => {
        if (sel.value) {
            columnMapping[sel.value] = parseInt(sel.dataset.col);
        }
    });

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    formData.append('column_mapping_json', JSON.stringify(columnMapping));
    formData.append('default_category', selectedCategory.value);  // ← 新增

    // ... 后续 fetch 不变
});
```

---

## 2. 步骤2 UI（仅分类列变化）

### 2.1 改动前

```
分类列：每行有下拉框让用户选 4 个分类之一（安全设备/密码设备/通用设备/其他设备）
```

### 2.2 改动后

```
分类列：每行显示用户选定的分类（可编辑 input）
- 默认值 = 用户在步骤1选的值
- 用户可在步骤2 修改某行的分类
- 但下拉选项仍然是这 4 个值（保留灵活性，决策1A）
```

### 2.3 步骤2 表格行示例（分类列）

```html
<!-- 改动前的代码 -->
<td>
    <select name="category_{{ row.row_idx }}" class="form-select form-select-sm">
        <option value="安全设备">安全设备</option>
        <option value="密码设备">密码设备</option>
        <option value="通用设备">通用设备</option>
        <option value="其他设备">其他设备</option>
    </select>
</td>

<!-- 改动后的代码：input + 灰色背景提示 -->
<td>
    <input type="text" name="category_{{ row.row_idx }}"
           value="{{ row.category }}"
           class="form-control form-control-sm"
           list="cat-list-{{ row.row_idx }}"
           style="background:#f0f7ff; font-weight:500;">
    <datalist id="cat-list-{{ row.row_idx }}">
        <option value="安全设备">
        <option value="密码设备">
        <option value="通用设备">
        <option value="其他设备">
    </datalist>
    <div style="font-size:11px; color:#888; margin-top:2px;">
        ※ 步骤1 已选：批量应用
    </div>
</td>
```

### 2.4 视觉对比

**改动前**（下拉框）:
```
┌─────────────┐
│ 安全设备 ▼ │  ← 下拉选择
└─────────────┘
```

**改动后**（带 datalist 的 input）:
```
┌─────────────┐
│ 安全设备    │  ← 可直接修改，蓝色背景 + datalist 提示
└─────────────┘
 ※ 步骤1 已选：批量应用
```

---

## 3. 后端改动（preview 路由）

### 3.1 改动位置

`app/routes/equipment.py` 的 `import_preview` 路由（line ~430-630 区间）

### 3.2 改动内容

```python
@bp.route('/import/preview', methods=['POST'])
def import_preview():
    # ... 既有代码 ...

    # 既有：从 form 取 column_mapping
    column_mapping_json = request.form.get('column_mapping_json', '{}')
    column_mapping = json.loads(column_mapping_json) if column_mapping_json else {}

    # 新增：读 default_category
    default_category = request.form.get('default_category', '').strip()
    if default_category and default_category not in ('安全设备', '密码设备', '通用设备'):
        return jsonify({'success': False, 'message': f'无效的分类: {default_category}'}), 400

    # 既有：处理每行
    for row_idx, row in enumerate(rows):
        processed = { ... }  # 既有字段提取

        # 修改：category 字段不再从 xlsx 读，统一用 default_category
        if 'category' in column_mapping:
            # 兼容旧逻辑（如果前端漏改）
            category = _eq_get_row_value(row, column_mapping, 'category')
        else:
            category = default_category  # ← 新增：统一用单选值

        processed['category'] = category
        # ... 后续不变 ...
```

### 3.3 commit 路由无需改动

`equipment.py:496` 已经 `subclass = _eq_get_row_value(row, column_mapping, 'subclass')`，
`category` 也类似从 `processed[category]` 取，commit 时直接 `equipment_model.add(... category=category ...)` 已经按字符串处理，无需改动。

---

## 4. 验收 UI 流程（验收时我会跑的 Playwright 脚本）

### 4.1 场景 1：xlsx 有"分类"列

```
1. 登录 → /equipment/import
2. 上传「设备知识库_手动测试_正常场景.xlsx」（有"分类"列）
3. 点"开始解析"
4. 检查：
   ✓ 列映射表头"分类"列下拉框只有"-- 不导入 --"
   ✓ 列映射下方显示"⚠ 有 1 列未映射（将忽略：分类）"
5. 不选分类，点"下一步"
6. 检查：
   ✓ 按钮 disabled 或 alert "请选择批量分类"
7. 选"安全设备"
8. 检查：
   ✓ 按钮变可点（蓝色）
9. 点"下一步"
10. 步骤2 渲染：
    ✓ 所有行 category = "安全设备"（蓝色 input 背景）
    ✓ 顶部统计 "精确:3 模糊:0 ..." 显示
11. 改第 2 行 category 为"密码设备"
12. 点"确认导入"
13. 检查数据库：第 1 行 category=安全设备，第 2 行 category=密码设备
```

### 4.2 场景 2：xlsx 无"分类"列 + 流程同上

```
1. 上传「设备知识库_手动测试_边界场景.xlsx」（无分类列）
2. 列映射全部正常，无警告
3. 必须选分类才能下一步
```

### 4.3 场景 3：回归测试（原有功能不能坏）

```
1. 原有 4 个测试文件（基础场景/模糊匹配/重复检测/含关联宿主设备）
2. 全部跑通
3. 步骤2 子类列、设备列表、设备详情、宿主设备列表都正常
```

---

## 5. 视觉验收图（预期效果）

**步骤1 默认状态**：
- 列映射区下方显示 ⚠ 警告（如果有未映射的分类列）
- 分类单选卡片：3 个未选状态
- 底部"下一步"按钮：**灰色 disabled**

**步骤1 选中"安全设备"后**：
- "安全设备"卡片：**蓝色边框 + 浅蓝背景 + 阴影**
- 底部"下一步"按钮：**蓝色可点**

**步骤2 表格分类列**：
- 默认值：用户选定的分类（蓝底）
- 可修改：input + datalist 提示
- 提示文字："※ 步骤1 已选：批量应用"

---

## 6. 不影响的部分

- ✅ 设备新增/编辑页（`/equipment/add`、`/equipment/edit`）保持原样
- ✅ 宿主设备导入（`/equipment/hosts/import`）保持原样
- ✅ 设备列表筛选（`/equipment/?category=xxx`）保持原样
- ✅ 子类匹配（fuzzy_match.match_subclass）保持原样

---

## 7. 等你确认

请确认 UI 设计是否符合你的期望。**如果同意，我就进入阶段四（代码设计），列出每个文件的具体改动 + diff 预览**。

**回复**:
- "OK" / "可以" / "继续" —— 我开始写代码设计
- 任何调整 —— 你指出哪里要改