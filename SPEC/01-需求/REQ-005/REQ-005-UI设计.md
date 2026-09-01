# REQ-005：报销单据自动填充增强 — UI设计

## 1. 设计目标

- 统一四个模板的页面布局风格，以 index.html 为准
- 自动填充表单以 **Word 模板行列样式**呈现（非简单网格）
- 动态行支持（科研物资采购申请单）
- OCR 进度可见、数据来源可追溯

---

## 2. 页面结构

### 2.1 新建单据模态框（newDocModal）

```
┌─────────────────────────────────────────────────────┐
│ [模态框标题: "新建单据 — {模板名}"]                    │
├─────────────────────────────────────────────────────┤
│ 选择模板 ──────────────────────────────────────── │
│ [模板卡片1] [模板卡片2] [模板卡片3] [模板卡片4]       │
├─────────────────────────────────────────────────────┤
│ 已选: {模板名}                                     │
│ ─────────────────────────────────────────────────  │
│ [🔄 自动填充] ← 按钮在字段区上方                    │
│ ┌─ OCR状态栏 (识别中 N/M) ──────────────────────┐  │
│ │ 🔍 正在识别: 发票编号 xxx (2/3)                │  │
│ └────────────────────────────────────────────────┘  │
│                                                     │
│ ┌─ Word样式表格 ────────────────────────────────┐  │
│ │ 字段布局: 按 template.json field_id 渲染     │  │
│ │ 如 T0[3,1] → 第0表第3行第1列                  │  │
│ └────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────┤
│            [取消]              [保存单据]           │
└─────────────────────────────────────────────────────┘
```

---

## 3. Word 样式表格布局规则

### 3.1 field_id 解析

`field_id = "T{table_index}[{row},{col}]"` → 映射到 HTML `<table>`

| 模板 | table_index |
|------|-------------|
| 因公出差审批单 | T2 |
| 差旅费报销凭证 | T0 |
| 伙食补助费申报表 | T1 |
| 科研物资采购申请单 | T0 |

### 3.2 渲染逻辑

1. 按 `table_index` 分组，同一表格的字段归为一组
2. 按 `row` 从小到大排序，`row` 相同按 `col` 排序
3. 同一行相邻的 label 格（`source: user_input` 且 `col < 3`）合并为一个 `<th>`
4. 同行连续输入格识别 `colspan`
5. 输出 `<table class="table table-bordered table-sm">`，深色主题兼容

### 3.3 单元格类型

| 类型 | 说明 | 渲染 |
|------|------|------|
| `label` | 字段标签 | `<th>` 左对齐，`text-white` |
| `input` | 用户输入 | `<td><input>` 蓝紫边框，`bg-dark` |
| `computed` | OCR来源 | `<td><input readonly badge>` |

---

## 4. OCR 进度与数据来源

### 4.1 自动填充按钮

- 位置：字段区最上方，`[自动填充]` 按钮
- 点击后：
  1. 检查缓存（有 invoice_id 的 key → 跳过）
  2. 显示进度条：`🔍 正在识别: {票据类型} {n}/{total}`
  3. 逐个调用 OCR recognizer
  4. 完成后自动填充字段

### 4.2 数据来源 Badge

OCR 填充的字段显示来源标识（右上角小标签）：

```html
<!-- 示例: 到达地点字段 -->
<div class="position-relative">
    <input id="field_T2[1,1]" class="form-control" value="上海" readonly>
    <span class="position-absolute top-0 end-0 badge bg-info small"
          style="font-size:0.6em;"
          title="来源: 火车票到达站 (2条)">
      invoice:火车票.到达站 (2条)
    </span>
</div>
```

- Badge 颜色：`bg-info`（蓝色）
- hover 显示完整来源

### 4.3 OCR 缓存

- 会话级 `Map<invoice_id, OCR结果>`
- 第二次点击同一报销项的单据：不弹进度条，直接用缓存数据填充

---

## 5. 动态行（科研物资采购申请单）

### 5.1 规则

电子发票有 N 条商品明细 → 在模板固定行之后追加 N 行物品填写行。

### 5.2 模板固定行

| field_id | 字段名 |
|----------|--------|
| T0[3,1] | 采购物品名称（固定） |
| T0[4,1] | 规格型号（固定） |
| T0[4,3] | 数量（固定） |
| T0[4,5] | 单价（固定） |
| T0[4,7] | 金额（固定） |

### 5.3 动态行渲染

```html
<!-- 固定行 (row=3, row=4) -->
<tr class="fixed-row">
  <th>采购物品名称</th>
  <td><input id="field_T0[3,1]"></td>
  ...
</tr>

<!-- 动态行: 商品明细[0], [1], ... -->
<tr class="item-row" data-item-index="0">
  <th>采购物品名称 <span class="badge bg-secondary">商品1</span></th>
  <td><input id="field_T0[3,1]_item_0" data-source="invoice:商品名称[0]"></td>
  ...
</tr>
```

### 5.4 追加逻辑

```javascript
// api_auto_fill 返回 items 数组后
items.forEach((item, i) => {
    const $row = createItemRow(item, i);  // 动态构建
    $('#itemsTable tbody').append($row);
});
```

---

## 6. 各模板字段映射（UI 绑定）

### 因公出差审批单（T2）

| field_id | 字段名 | UI 类型 | 数据源 Badge |
|----------|--------|--------|-------------|
| T2[1,1] | 到达单位及地点 | readonly+badge | `invoice:火车票.城市列表` |
| T2[2,1] | 起止时间 | readonly+badge | `invoice:日期范围` |
| T2[3,1] | 出行方式 | readonly+badge | `invoice:出行方式` |

### 差旅费报销凭证（T0）

| field_id | 字段名 | UI 类型 | 数据源 Badge |
|----------|--------|--------|-------------|
| T0[3,2] | 到达地点 | readonly+badge | `invoice:火车票.到达站` |
| T0[3,9] | 出差开始日期 | readonly+badge | `invoice:火车票.出发日期` |
| T0[3,12] | 出差结束日期 | readonly+badge | `invoice:火车票.到达日期` |
| T0[5,5] | 城市间交通费 | readonly+badge | `invoice:火车票.amount_sum` |

### 伙食补助费申报表（T1）

| field_id | 字段名 | UI 类型 | 数据源 Badge |
|----------|--------|--------|-------------|
| T1[3,2] | 开始日期 | readonly+badge | `invoice:日期范围` |
| T1[3,4] | 结束日期 | readonly+badge | `invoice:日期范围` |

### 科研物资采购申请单（T0，动态行）

| field_id | 字段名 | UI 类型 | 数据源 Badge |
|----------|--------|--------|-------------|
| T0[3,1]+item | 采购物品名称 | readonly+badge | `invoice:商品名称[i]` |
| T0[4,1]+item | 规格型号 | readonly+badge | `invoice:规格型号[i]` |
| T0[4,3]+item | 数量 | readonly+badge | `invoice:数量[i]` |
| T0[4,5]+item | 单价 | readonly+badge | `invoice:单价[i]` |
| T0[4,7]+item | 金额 | readonly+badge | `invoice:金额[i]` |

---

## 7. 配色规范

### 7.1 深色主题兼容

所有新增 UI 元素遵循现有深色主题：

| 元素 | 颜色 |
|------|------|
| 模态框背景 | `#1e3a5f` |
| 表格边框 | `rgba(0,255,255,0.2)` |
| label 文字 | `text-white` |
| 输入框背景 | `bg-dark` |
| 输入框边框 | `border-secondary` |
| 只读输入框背景 | `bg-darker`（#0d1117） |
| badge 背景 | `bg-info` |
| OCR 进度条 | `bg-warning` |

---

## 8. 交互流程

```
用户点击"自动填充"
    ↓
检查 OCR 缓存（Map<invoice_id, result>）
    ↓
[无缓存] → 显示进度条 🔍 正在识别: N/M
    ↓
逐个调用 /expense/records/api/ocr/{invoice_id}
    ↓
[有缓存] → 直接使用缓存，跳过进度条
    ↓
组装 auto_fill_data {字段: {value, source, badge}}
    ↓
renderWordStyleTable(template, auto_fill_data)
    ↓
追加动态行（科研物资采购申请单）
    ↓
字段显示值 + badge 显示来源
```

---

## 9. 验收检查点

- [ ] 自动填充按钮在字段区顶部可见
- [ ] OCR 进度条显示 N/M 计数
- [ ] 字段只读但有 badge 显示来源
- [ ] badge hover 显示完整来源文本
- [ ] 科研物资采购申请单有 N 行动态物品行
- [ ] 表格布局与 Word 模板行列对应
- [ ] 第二次点击同一报销项不重复 OCR
