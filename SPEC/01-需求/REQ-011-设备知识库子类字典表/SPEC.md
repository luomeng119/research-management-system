# SPEC REQ-011: 设备知识库子类字典表

## 1. 概述

为设备知识库的三大类（密码设备、安全设备、通用设备）增加二级子类分类，属性的值来自独立字典表。导入时出现新子类由用户确认后新增；编辑/新增页面子类为下拉选择。

---

## 2. 新增数据表

**表名**: `knowledge_subclasses`

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT | 主键 |
| parent_category | VARCHAR(50) | NOT NULL | 父分类 |
| subclass_name | VARCHAR(100) | NOT NULL | 子类名称 |
| created_at | DATETIME | DEFAULT CURRENT_TIMESTAMP | 创建时间 |

**唯一约束**: `UNIQUE(parent_category, subclass_name)`

---

## 3. 子需求

| 编号 | 子需求 | 说明 |
|------|--------|------|
| REQ-NEW-1 | 数据模型 | KnowledgeSubclassModel 及字典表创建 |
| REQ-NEW-2 | 模糊匹配函数 | fuzzy_match.py 新增 match_subclass() |
| REQ-NEW-3 | 导入预览增强 | 步骤2 processed 添加 subclass_result |
| REQ-NEW-4 | 导入新子类确认 | 步骤2映射表显示确认/跳过按钮 |
| REQ-NEW-5 | 导入提交写入 | 步骤3提交时写入新子类到字典表 |
| REQ-NEW-6 | equipment 表字段 | 新增 subclass 字段 |
| REQ-NEW-7 | 详情页显示 | 设备知识库详情页显示设备子类 |
| REQ-NEW-8 | 编辑页下拉 | 设备知识库编辑页子类下拉选择 |
| REQ-NEW-9 | 新增页下拉 | 设备知识库新增页子类下拉选择 |
| REQ-NEW-10 | 导出扩展 | 导出 Excel 包含设备子类列 |
| REQ-NEW-11 | 列表页筛选 | 列表页增加装备子类筛选项，支持按子类过滤设备 |

---

## 4. 模糊匹配逻辑

`match_subclass()` 与 `match_research_unit()` 逻辑完全一致：

1. **精确匹配**：字典中 name == 输入
2. **规范化匹配**：去噪声后 substring 匹配
3. **模糊匹配**：编辑距离 <= 2
4. **均无**：未匹配

---

## 5. 列表页筛选器

分类与子类联动：切换分类后子类下拉自动刷新为该分类下的子类选项。

```
全部分类  装备子类  全部形态  全部状态  [综合搜索...] [筛选] [重置]
   ↓          ↓
<select>   <select>（根据分类动态加载选项）
```

---

## 6. 验收标准

1. 导入 Excel 包含设备子类列时，步骤2预览正确显示匹配状态
2. 字典中已有的子类名，匹配状态显示精确/模糊匹配
3. 字典中无此子类名，映射表显示"待确认"及确认/跳过按钮
4. 用户点确认后，步骤3提交时写入字典表
5. 用户点跳过后，设备 subclass 为空值
6. 子类为空时正常导入，不报错误
7. 新增/编辑页子类为下拉选择，按当前分类过滤
8. 切换分类后子类下拉选项自动刷新
9. 导出 Excel 包含设备子类列
10. 详情页正确显示设备子类

---

## 6. 文件改动清单

| 文件 | 改动 |
|------|------|
| app/models.py | 新增 KnowledgeSubclassModel |
| app/utils/fuzzy_match.py | 新增 match_subclass() |
| app/routes/equipment.py | 导入预览/提交加子类匹配和写入 |
| app/templates/equipment/import_step2.html | 子类列加匹配状态和确认UI |
| app/templates/equipment/detail.html | 子类字段显示 |
| app/templates/equipment/edit.html | 子类下拉选择 |
| app/templates/equipment/add.html | 子类下拉选择 |
| app/templates/equipment/index.html | 子类筛选项（分类联动下拉） |
| app/routes/equipment.py export 部分 | 导出加子类列 |

---

## 7. 状态

- [x] 需求设计确认
- [x] UI设计（6个页面）
- [x] 编码实现
- [ ] 自测验证
- [ ] 用户验收