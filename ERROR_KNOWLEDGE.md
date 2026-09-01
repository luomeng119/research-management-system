# 科研管理系统 — 错误知识库

> 记录已确认的错误模式、根因和修复方案，防止同类问题重复出现。

## Bug: 删除报销项导致关联发票/支付记录被物理删除

**日期**: 2026-05-03
**影响**: Bug #2（用户报告）

### 根因
`expense_db.py::delete_reimbursement()` 在删除报销项时，使用 `DELETE FROM expense_invoice WHERE reimbursement_id=?` 和 `DELETE FROM expense_payment WHERE reimbursement_id=?` **物理删除**了关联的发票和支付记录。

### 正确行为
删除报销项时，应将关联的发票/支付记录**撤回到待整理区**：
- `reimbursement_id` → `NULL`
- `status` → `'未匹配'`

### 修复
```python
def delete_reimbursement(rid):
    conn = get_db()
    conn.execute('BEGIN IMMEDIATE')
    # 改为 UPDATE（撤回到待整理区），不再是 DELETE
    c.execute(
        "UPDATE expense_invoice SET reimbursement_id=NULL, status='未匹配' WHERE reimbursement_id=?",
        (rid,)
    )
    c.execute(
        "UPDATE expense_payment SET reimbursement_id=NULL, status='未匹配' WHERE reimbursement_id=?",
        (rid,)
    )
    c.execute('DELETE FROM expense_reimbursement WHERE id=?', (rid,))
    conn.commit()
```

### 验证
- 报销项状态=草稿时可删除
- 删除后发票/支付记录 status='未匹配', reimbursement_id=NULL ✓
- 记录物理删除（不再残留）✓

---

## Bug: 去重逻辑以 amount+date 为键，忽略 invoice_no

**日期**: 2026-05-03
**用户报告**: `26319166100004590755.pdf` 无法上传

### 根因
`expense.py::api_upload()` 的去重逻辑：
```sql
SELECT id FROM expense_invoice WHERE amount=? AND date=? LIMIT 1
```
只比较 `金额 + 日期`，未比较 `invoice_no`。

两张同天同价（662元）但不同发票号的高铁票互认为"重复"，第二张被拦截：
- `26119110010003200111.pdf`: invoice_no=26119110010003200111, amount=662, date=2026-04-16 ✓
- `26319166100004590755.pdf`: invoice_no=26319166100004590755, amount=662, date=2026-04-16 → 去重拦截 ✗

### 修复
去重条件改为 `amount + date + invoice_no`（三键去重）：
```sql
SELECT id FROM expense_invoice WHERE amount=? AND date=? AND invoice_no=? LIMIT 1
```
若 invoice_no 为空则降级为 amount+date 去重（保持对无号票据的保护）。

### 验证
- 同金额同日期不同 invoice_no → 应允许上传
- 同金额同日期同 invoice_no → 拦截（真正重复）
- 同金额同日期 invoice_no 为空 → 拦截（防止无号票据重复）

---

## Bug: 支付记录被误判为发票（自动分类不可靠）

**日期**: 2026-05-03
**用户报告**: 上传后未能自动区分发票和支付记录

### 根因
`api_upload()` 并行运行 `recognize_file` 和 `recognize_payment` 两个 OCR 器，分数高者胜出。评分算法只看字段数量，不验证实际类型。

测试中微信/支付宝截图被误判为 invoice：
- `Iamge_2026_04_16_101855.jpg`（微信支付42元）→ invoice_no 被误识别 → invoice得分2 vs payment得分2 → 平局选invoice
- `Iamge_2026_04_16_101937.jpg`（支付宝1324元）→ invoice_type=火车票（误识别消费场所）→ invoice得分1 < payment得分2 → 本应选payment但invoice识别器写入了 invoice_type

### 修复
两条路线：
1. **短期**：在评分中引入"类型惩罚"——recognize_payment 专有关键词（微信、支付宝、财付通）命中时大幅降低 invoice 得分
2. **长期**：OCR 文本中若含"交易金额/支付日期/财付通/支付宝"等强支付关键词，直接判定为 payment（不依赖评分）

### 涉及文件
- `app/routes/expense.py` — `api_upload()` 评分逻辑
- `app/ocr/recognizer.py` — `recognize_file()` / `recognize_payment()` 字段提取

---

## [2026-05-02] fetch 缺少 .catch() 导致 UI 永久挂死在"加载中"

### 错误摘要

- **项目**：科研管理系统（04-科研管理系统）
- **错误模式**：前端 `fetch()` 调用缺少 `.catch()` 错误处理
- **症状**：页面显示"加载中…"永不消失，浏览器 console 无报错（Promise 异常被吞掉）
- **根因**：`fetch(...).then(r => r.json()).then(...)` 链中，当 API 返回非 JSON 响应（如 405/302/404 返回的 HTML）时，`r.json()` 抛出异常，整个 Promise 链进入 `reject` 分支但没有任何 `.catch()` 处理器，异常被 JavaScript 引擎吞掉，UI 永远不更新
- **修复方案**：所有 fetch 链末尾必须加 `.catch(err => { console.error(...); ... })`
- **预防**：编码规范要求：所有 fetch 必须配对 .catch()
- **涉及文件**：`records.html`, `pending.html`, `index_new.html`, `approvals.html`, `payments.html`, `upload.html`

### 错误代码示例

```javascript
// 错误写法 — 无 .catch()
fetch('/api/data', { credentials: 'include' })
    .then(r => r.json())
    .then(data => { render(data); });

// 正确写法
fetch('/api/data', { credentials: 'include' })
    .then(r => r.json())
    .then(data => { render(data); })
    .catch(err => { console.error('加载失败:', err); /* 显示错误提示 */ });
```

### 关联错误

- `405 Method Not Allowed`：后端路由方法缺失（如 GET 端点未定义）返回 HTML 而非 JSON，导致 r.json() 抛异常
- `401/302` 重定向：未登录时 Flask 重定向到登录页，返回 HTML，r.json() 失败

### 预防规范

1. **所有 fetch 必须有 .catch()**：无论是 GET/POST/PUT/DELETE，任何 fetch 链末尾必须有 .catch()
2. **.catch 必须有用户反馈**：不能只有 `console.error`，必须更新 UI 告知用户（如显示"加载失败"）
3. **API 优先验证 HTTP 状态码**：在 r.json() 前检查 `if (!r.ok)` 先处理非 200 状态

```javascript
// 推荐写法：先检查 r.ok
fetch(url, { credentials: 'include' })
    .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    })
    .then(data => { /* 成功 */ })
    .catch(err => { console.error(err); /* 显示错误 UI */ });
```

---

## [2026-05-02] 后端 API 缺少 GET 方法返回 405

### 错误摘要

- **项目**：科研管理系统
- **错误模式**：前端调用 `GET /api/invoices/<id>` 预览发票详情，后端只有 PUT/DELETE 未定义 GET
- **症状**：`405 Method Not Allowed`
- **根因**：新增功能时只为更新/删除定义了路由，未定义查询路由
- **修复方案**：为 `/api/invoices/<int:iid>` 和 `/api/payments/<int:pid>` 补充 `GET` 方法
- **涉及文件**：`app/routes/expense.py`

---

## Bug: 报销单据页面调用了错误的API路径

**日期**: 2026-05-03
**影响**: BUG-001（用户报告）/ BUG-002（用户报告）

### 根因

`app/templates/expense/documents.html` 和 `document_templates.html` 中的 fetch API 路径使用了 `/expense/api/*`，但这些 API 属于 `documents` blueprint（url_prefix=`/expense/documents`），正确路径应为 `/expense/documents/api/*`。

此外，`documents` blueprint 缺少 `GET /api/reimbursements` 和 `GET /api/reimbursements/<rid>` 两个报销项查询路由，导致 `documents.html` 无法加载报销项列表和详情。

### 修复方案

1. `documents.html` 和 `document_templates.html`：将所有 `/expense/api/` 替换为 `/expense/documents/api/`
2. `app/routes/documents.py`：新增两个路由函数 `api_reimbursements()` 和 `api_reimbursement_get(rid)`

### 预防

- 不同 blueprint 有不同的 url_prefix，前端 fetch 路径必须与后端路由的 url_prefix 一致
- 新增 API 路由时，前端模板同步修改，并确保前端先做语法检查验证路径正确
- 涉及多个 blueprint 的页面，建议在各自 blueprint 补齐所有需要的路由，避免跨 blueprint 调用

---

<!-- 格式说明：每条错误占一个 ## 区块，包含：错误摘要（项目/模式/症状/根因/修复/预防/文件）+ 代码示例 + 关联错误 -->

---

## Bug: 设备知识库导入步骤1 column-mapping 缺少"设备子类"选项

**日期**: 2026-05-25
**影响**: REQ-009（用户报告）

### 根因

`app/templates/equipment/import_step1.html` 的 `fieldOptions` 和 `patterns` 变量中缺少 `subclass`（设备子类）字段定义，导致用户在步骤1下拉映射中无法选择"设备子类"列。虽然后端 `routes/equipment.py` 的 `_eq_get_row_value` 和 `import_commit` 都已支持 `subclass` 字段，但前端缺少 UI 入口。

### 修复方案

`import_step1.html` 中 `fieldOptions` 添加：
```html
<option value="subclass">设备子类</option>
```
`patterns` 中添加：
```javascript
'subclass': /子类|设备子类|subclass/i,
```

### 涉及文件

- `app/templates/equipment/import_step1.html`

### 预防

- 新增字段时，前端模板的 `fieldOptions` 下拉选项、patterns 自动映射、步骤2的显示和提交 form 三个地方必须同步添加。

---

## Bug: 设备知识库导入"关联宿主设备"被误标为必填，且点击"匹配预览"提示"请先上传文件"

**日期**: 2026-05-25
**影响**: REQ-009（用户报告）

### 根因

1. `import_step1.html` 的步骤1表头描述文字写的是"关联宿主设备列必须指定"，文字误导用户；
2. JS patch 过程中，第260-261行的 `parseError.textContent` 和 `parseError.style.color` 两行缩进丢失（变成与外层 `if` 同级），导致 `return` 执行时已经修改了 `parseError`，之后 `fetch` 失败后 `.catch` 再次设置相同消息。浏览器缓存该错误后，即使重新上传文件也仍然显示已缓存的错误提示。

### 修复方案

1. 描述文字改为"（关联宿主设备为可选项）"；
2. 补正两行缩进（8空格缩进回到箭头函数内）；
3. 在 `fetch` 之前增加二次检查 `fileInput.files.length`。

### 涉及文件

- `app/templates/equipment/import_step1.html`

### 预防

- patch 操作后必须检查缩进是否正确（特别是嵌套在 `{}` 内的代码），用 `python3 -c "for i,l in enumerate(open('file').read().splitlines()...)"` 逐行验证缩进。


---

## Bug: 设备知识库无法打开（run.py 启动阻塞 30 秒）

**日期**: 2026-06-22
**影响**: 整个系统（设备知识库 / 项目 / 报销 等所有路由都不可达）

### 症状

用户访问 `/equipment/` 无响应（连接超时）。实际是 Flask 服务根本没启动。

### 根因

`run.py` 的 `ensure_inference_server()` 函数是**同步阻塞**流程：
1. 检查端口 18789 是否有服务在跑（裸 `except:` 吞掉 ConnectionError，正常）
2. 检查 `node` 是否可用（FileNotFoundError 处理，正常）
3. `subprocess.Popen` 启动 `inference_server.js`
4. **进入 30 秒循环等待服务器就绪**

当 `models/chinese-text-correction-1.5b.Q4_K_M.gguf` 不存在时，Node 进程启动后立即退出（找不到模型），但循环要等满 30 秒才放弃。期间 Flask 永远到不了 `app.run()`，端口 5001 也没有监听。

### 修复方案

把 `ensure_inference_server()` 改为**后台线程异步启动**：
- 启动前先检查 `node` 可用、`models/*.gguf` 存在、`inference_server.js` 存在，缺一直接跳过
- 启动逻辑放到 `threading.Thread(daemon=True)` 里，Flask 不再等待
- 所有 try 块改用 `except Exception:` 替代裸 `except:`
- 启动时 stdout/stderr 重定向到 DEVNULL（避免污染日志）

### 涉及文件

- `run.py`（重写 `ensure_inference_server`）

### 验证

```
GET /equipment/    → 302（重定向到 /auth/login，正常）
GET /auth/login   → 200
GET /             → 302
启动耗时: < 3 秒（之前 30 秒+）
```

### 预防

- 任何「可选依赖」的启动逻辑（如推理服务器、缓存服务）必须**异步后台启动**，不能阻塞主进程。
- 启动前做完整的依赖检查（node / 模型文件 / 启动脚本），缺一即跳过。
- 主进程（Flask）启动时不应等待任何可选服务。

---

## Bug: EquipmentModel.search() 缺少 subclass 参数（REQ-011 半成品代码）

**日期**: 2026-06-22
**影响**: REQ-011（设备知识库子类字典表）
**触发条件**: 访问 `/equipment/` 任意 URL（甚至 `?category=安全设备`）即崩

### 症状

```
TypeError: EquipmentModel.search() got an unexpected keyword argument 'subclass'
  File "Z:\代码牛生产区\04-科研管理系统\app\routes\equipment.py", line 49, in index
    all_equipment = equipment_model.search(
        ...
        subclass=subclass_filter if subclass_filter else None
    )
```

### 根因

REQ-011 在 `app/models.py` 改动 `EquipmentModel.search()` 时**只写了 SQL 过滤逻辑，忘了加方法签名参数**：

```python
def search(self, category=None, form=None, tech_status=None, keyword=None):  # ← 缺 subclass
    """综合搜索设备"""
    ...
    if subclass:                          # ← 但方法体已经引用 subclass
        conditions.append('subclass = ?')
        params.append(subclass)
```

这是典型的「半成品代码」：SQL 过滤、SELECT 子句、返回字典都已经包含 `subclass` 字段，唯独方法签名没声明。任何调用方传 `subclass=...` 都会触发 `TypeError`。

### 修复方案

```diff
-def search(self, category=None, form=None, tech_status=None, keyword=None):
-    """综合搜索设备"""
+def search(self, category=None, form=None, tech_status=None, keyword=None, subclass=None):
+    """综合搜索设备（支持子类筛选）"""
```

单行签名补全，业务逻辑无需改动。

### 涉及文件

- `app/models.py`（EquipmentModel.search 签名）

### 验证

- 底层 4/4 通过：`search(keyword)` / `search(subclass=None)` / `search(subclass='防火墙')` / `search(category+subclass)`
- 端到端 5/5 通过：`/equipment/` 基线、`?category=安全设备`、`?subclass=防火墙`、`?category&subclass`、`?keyword`
- HTTP 全 200，响应中无 `TypeError`/`Traceback`/`unexpected keyword`

### 预防

- 改方法签名时，**先看调用方传的所有参数**，再确认签名是否都声明了。
- 用 `git grep "Model.*\.search("` 或 IDE 的 "Find Usages" 反向核对。
- REQ 半成品代码合并前必须做端到端冒烟测试（登录→访问主页面→触发所有筛选路径）。

---

## Bug: 设备知识库导入步骤2子类列显示 Python dict 字符串（之前修过又复现）

**日期**: 2026-06-23
**影响**: REQ-011（设备知识库子类字典表）导入功能
**触发条件**: 设备知识库导入步骤1 选完列映射 → 点"下一步：匹配预览" → 步骤2 表格中所有匹配的子类单元格

### 症状

设备知识库导入 → 步骤2（匹配预览）→ 设备子类列显示完整的 Python dict 字符串：

```
{'created_at': '2026-01-01 00:00:00', 'id': 4, 'parent_category': '安全设备', 'subclass_name': '防火墙'}
{'created_at': '2026-01-01 00:00:00', 'id': 5, 'parent_category': '安全设备', 'subclass_name': '入侵检测'}
{'created_at': '2026-01-01 00:00:00', 'id': 1, 'parent_category': '密码设备', 'subclass_name': '密码机'}
```

期望显示：`防火墙` / `入侵检测` / `密码机`（彩色标注精确/模糊/未匹配）

截图证据：`screenshots/bug_reproduce/BUG_repro_step2.png`

### 根因（3 处代码返回值类型不一致）

```python
# 1. app/utils/fuzzy_match.py:357（根源）
def match_subclass(...):
    def try_match(subclasses):
        for s in subclasses:
            if s.get('subclass_name') == subclass_name:
                return {'matched': s, ...}                  # ← s 是整个 dict 对象！
        ...
        if candidates:
            return {
                'matched': candidates[0]['subclass'],        # ← 还是 dict
                'candidates': [c['subclass'] for c in ...]   # ← list of dict
            }
```

```python
# 2. app/routes/equipment.py:572（中间层，未做转换）
subclass_result = {
    'original': subclass,
    'matched': ms['matched'],    # ← 把 dict 原封不动塞进模板上下文
    ...
}
```

```jinja2
{# 3. app/templates/equipment/import_step2.html (HEAD) #}
<span style="color:#22c55e;">{{ sr.matched or sr.original }}</span>
{## ← 把 dict 当字符串渲染！Jinja2 会把 dict 调 __str__ 输出 {'key': value, ...} #}
```

**根本原因**：`match_subclass()` 返回的 `matched`/`candidates` 字段是数据库行的完整 dict（含 id/created_at/parent_category/subclass_name），但调用方把它当成字符串使用。这是**返回值类型契约不一致**的典型 bug —— 函数没承诺类型，调用方按字符串假设。

### 修复方案（彻底改底层 + 防回归测试）

**核心**：让 `match_subclass()` 的返回值类型契约明确 —— `matched: str | None`，`candidates: list[str]`。

```diff
# app/utils/fuzzy_match.py — match_subclass() 内部 4 处改动

 def try_match(subclasses):
     for s in subclasses:
         if s.get('subclass_name') == subclass_name:
-            return {'matched': s, 'exact': True, ...}                        # dict
+            return {'matched': s.get('subclass_name', ''), ...}             # str ✓

     normalized_input = _normalize(subclass_name)
     if normalized_input and normalized_input != subclass_name:
         for s in subclasses:
             n_name = _normalize(s.get('subclass_name', ''))
             if n_name and (normalized_input in n_name or n_name in normalized_input):
-                return {'matched': s, 'exact': False, ...}                  # dict
+                return {'matched': s.get('subclass_name', ''), ...}         # str ✓

     candidates = []
     for s in subclasses:
         s_name = s.get('subclass_name', '')
         if len(subclass_name) > 2 and len(s_name) > 2:
             dist = _levenshtein(subclass_name, s_name)
             if dist <= 2:
-                candidates.append({'subclass': s, 'distance': dist})        # dict
+                candidates.append({'subclass': s_name, 'distance': dist})   # str ✓

     candidates.sort(key=lambda x: x['distance'])
     if candidates:
         return {
-            'matched': candidates[0]['subclass'],                           # dict
+            'matched': candidates[0]['subclass'],                           # str ✓
             'exact': False,
             'method': 'fuzzy',
-            'candidates': [c['subclass'] for c in candidates[:3]]           # list of dict
+            'candidates': [c['subclass'] for c in candidates[:3]]           # list of str ✓
         }
     return None
```

```diff
# app/templates/equipment/import_step2.html — 模板简化（因为底层已返回字符串）
-                                            <span style="color:#22c55e;">{{ sr.matched.subclass_name if sr.matched else sr.original }}</span>
+                                            <span style="color:#22c55e;">{{ sr.matched or sr.original }}</span>
```
（3 处 span + 1 处 hidden input value 全部简化回 `sr.matched or ...` 形式）

### 涉及文件

- `app/utils/fuzzy_match.py`（match_subclass 内部 4 处 + 函数 docstring 文档化类型契约 + 类型注解 `parent_category: str | None`）
- `app/templates/equipment/import_step2.html`（5 处简化）
- `test/test_match_subclass.py`（新增，10 个测试用例）

### 验证

**底层 10/10 通过**（`python3 test/test_match_subclass.py`）：
- exact/normal/fuzzy/none 4 种 method 路径全覆盖
- matched 必为 str 类型断言
- candidates 必为 list[str] 断言
- **防回归核心测试**：`test_regression_not_dict` — 任何场景下 matched 都不能是 dict

**端到端 4/4 场景通过**（Playwright + Chrome，Flask 5001 端口）：
| 场景 | subclass 渲染 | new | relations | 结果 |
|------|------|------|------|------|
| 设备知识库_基础场景.xlsx | 防火墙/入侵检测/密码机（绿色 ✓） | 4 | 0 | ✅ |
| 设备知识库_模糊匹配.xlsx | 同上 | 4 | 0 | ✅ |
| 设备知识库_重复检测.xlsx | 防火墙/防火墙/防火墙 | 3 | 0 | ✅ |
| 设备知识库_含关联宿主设备.xlsx | 防火墙/入侵检测/密码机 | 3 | 2 | ✅ |

截图证据：`screenshots/import_test_v2/{01-04}_*_05_step2_full.png`（修复后干净渲染）

### 「之前修过又复现」的根因

工作区在 5/25 ~ 6/22 期间有 **未提交的修复尝试**（`import_step2.html` 用 `{{ sr.matched.subclass_name if sr.matched else sr.original }}` 绕过 dict 渲染）。但：
- 未 commit
- 没有 commit message 标注 `[REQ-011-fix]`
- 任何 `git checkout` / `git reset` / `git pull` 都会还原模板 → bug 复现
- 这就是用户报告"之前修过又复现"的根因

**本次彻底解决**：改底层 `match_subclass()` 返回字符串契约 + 单元测试锁定 + 模板简化，三层防护。

### 预防

- **返回值类型契约必须在 docstring 中明文写清**（这次已加 `{'matched': str | None, 'candidates': list[str], ...}` 文档）
- **任何模糊匹配函数返回 dict/list 时**，调用方使用前必须打印 `type()` 确认类型 —— `print(type(ms['matched']))` 应该看到 `<class 'str'>` 而不是 `<class 'dict'>`
- **修复尝试如果未在 24 小时内 commit + push，必须告知用户**，避免"半截修复"留在工作区被误还原
- **新增 `test/test_match_subclass.py`**：每次 PR 必须跑 `python3 test/test_match_subclass.py`，任何类型回归立即失败
- 类似函数 `match_research_unit()`（REQ-009）也需审计 —— 当前返回 dict 包裹的 `{original, matched, exact, method, candidates}`，需检查是否有相同隐患（本次未涉及）

---

## 工程改进: 内网环境无法访问 CDN（在线 JS/CSS 全部本地化）

**日期**: 2026-06-22
**影响**: 全站（doc_preview 组件被 base.html 全站 include，所有页面受影响）
**触发条件**: 内网环境访问 /equipment/import 等任何含 PDF/Word 文档预览的页面

### 根因

4 处 CDN 资源未本地化：

| 文件 | 行 | 在线 URL |
|---|---|---|
| `templates/components/doc_preview.html` | 37 | cdnjs.cloudflare.com/.../pdf.min.js |
| `templates/components/doc_preview.html` | 39 | cdnjs.cloudflare.com/.../pdf.worker.min.js |
| `templates/components/doc_preview.html` | 43 | cdnjs.cloudflare.com/.../mammoth.browser.min.js |
| `routes/templates.py` | 128 | cdn.jsdelivr.net/.../bootstrap.min.css |

注意：`base.html` 早已全部离线化（bootstrap/jquery/ui-enhance 都用本地路径）。
但 `base.html` 第 95 行 `{% include 'components/doc_preview.html' %}` 把 doc_preview 注入到所有页面，所以**任何时候用户预览 PDF/Word 附件，就会去外网拉 CDN**。

### 修复方案

直接替换为本地静态资源路径：

```diff
- <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
+ <script src="/static/js/pdf.js"></script>

- pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/.../pdf.worker.min.js';
+ pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/js/pdf.worker.js';

- <script src="https://cdnjs.cloudflare.com/.../mammoth.browser.min.js"></script>
+ <script src="/static/js/mammoth.min.js"></script>

- <link href="https://cdn.jsdelivr.net/.../bootstrap.min.css" rel="stylesheet">
+ <link href="/static/css/bootstrap.min.css" rel="stylesheet">
```

### 涉及文件

- `app/templates/components/doc_preview.html`（3 处）
- `app/routes/templates.py`（1 处）

### 验证

- CDN URL 扫描：项目内仅剩 bootstrap.bundle.min.js 源码内 webpack 字符串（含 https 但非 HTML 资源）
- 端到端：/equipment/import HTTP 200，响应中不含 cdnjs/jsdelivr/unpkg
- 静态资源：4 个本地 JS/CSS 全部 HTTP 200，文件大小匹配
- 调试页 /templates/test_tree 也已切换

### 预防

- **任何 CDN 资源在提交前必须本地化**（外网测试通过 → 内网测试通过 → 才算完成）。

---

## Bug: `flask_session` 依赖未声明，Windows 启动报 ModuleNotFoundError

**日期**: 2026-06-24
**影响**: 任何 Windows 全新部署（按 requirements.txt 装完所有包后跑 `python run.py` 必崩）

### 根因

REQ-013 commit `73e42da` 修复 1200+ 条数据步骤2 超时时，引入 `from flask_session import Session` 做 filesystem session（替代 cookie session），但**漏改 `requirements.txt`**——导致依赖列表里没 `Flask-Session`，新机器按 requirements.txt 装完后启动 Flask 立即报错：

```
Traceback (most recent call last):
  File "F:\04-科研管理系统\run.py", line 70, in <module>
  app = create_app()
  File "F:\04-科研管理系统\app\__init__.py", line 21, in create_app
  from flask_session import Session
ModuleNotFoundError: No module named 'flask_session'
```

而 `offline_packages/packages/` 目录也漏放 `Flask-Session` / `cachelib` / `msgspec` 三个 wheel，offline install.bat 同样装不上。

### Flask-Session 0.8.0 的完整依赖链（不可漏！）

| 包 | 用途 |
|---|---|
| `Flask-Session==0.8.0` | 服务端 session 存储 |
| `cachelib==0.14.0` | 缓存后端抽象（FileSystemCache / RedisCache 等） |
| `msgspec==0.21.1` | 高性能序列化（Flask-Session 0.8 默认 serializer） |

注意：msgspec 是 **cp313 专用 wheel**（非 abi3），不要尝试找通用版，PyPI 也没有。

### 修复

1. `requirements.txt` 加三行（按字母序，紧跟 `Flask`）：
   ```
   Flask-Session==0.8.0
   cachelib==0.14.0
   msgspec==0.21.1
   ```
2. `offline_packages/packages/` 复制 3 个 whl：
   - `flask_session-0.8.0-py3-none-any.whl`
   - `cachelib-0.14.0-py3-none-any.whl`
   - `msgspec-0.21.1-cp313-cp313-win_amd64.whl`
3. `offline_packages/install.bat` 在 [1/6] core dependencies 步骤加这 3 个文件
4. `offline_packages/download.bat` 在 [1/3] core dependencies 步骤加这 3 个包名
5. `offline_packages/README.md` 更新「包含包」清单（37→41 个 whl）

### 验证

- 临时 venv 跑 `pip install --no-index --find-links=offline_packages/packages -r requirements.txt`：所有包都装得上 ✅
- `python -c "import flask_session; print(flask_session.__version__)"`：输出 0.8.0 ✅
- 启动 run.py 不再报 ModuleNotFoundError ✅

### 预防

- **任何新增 `import xxx`（非 stdlib）必须同步检查/更新 requirements.txt**。把这条写进 commit message 模板：
  > 新增 import → 检查 requirements.txt / offline_packages 是否齐全
- commit 前跑一遍 `pip install --dry-run -r requirements.txt`，看是否有冲突或缺包
- 重大依赖变更（如 Flask-Session 0.7 → 0.8）必须做完整端到端验证，不能只跑 import 测试
- 工程扫描脚本：用 `grep -rE "https?://[^\"'<> ]+\.(js|css)" app/templates app/routes app/static` 定期巡检。
- base.html 已经做对了（全部本地），新模板都应继承这种"零 CDN"模式。

---

## Bug: REQ-014 E2E 验证 JS 函数体"看起来没跑"——Flask 进程没重启

**现象**：
- 改了 `app/templates/equipment/import_step2.html`（startImportFlow 函数体加 `window._impStart = 'called'` 调试标记）
- Playwright 真实点击 `#confirmImportBtn` 之后：
  - 按钮变 disabled ✅（说明 JS 跑了至少 1 行）
  - 但 `window._impStart` 是 `null` ❌
  - 没有 `console.log` 输出
  - 没有 `fetch` 网络请求
- 同样的 Python 测试脚本、相同的 Playwright session 注入、相同 cookie、相同 chrome-headless-shell

**根因**：
- Flask 进程是 **10:09 启动** 的，但 `import_step2.html` 改动发生在 10:30 之后
- 项目的 `run.py` **没有 `app.run(debug=True)`**，Flask **不会自动重载模板/源码**
- 浏览器拿到的 HTML 来自 **10:09 那个老 Flask 进程内存中缓存的旧模板**
- 用 `String(startImportFlow).slice(0, 200)` 拿到的是 **旧函数体**（没有 `_impStart` 那行）——这是关键证据

**为什么之前所有单测都过**：
- `test_client` 不经过 HTTP，直接调用路由函数 → 走的是最新代码
- 4/4 单测全部基于 test_client，**完全不会暴露这种"Flask 内存缓存"问题**
- 这是典型的"单测绿、上线红"陷阱

**修复**：
```bash
# 杀旧进程（多次启动会留多个）
ps aux | grep "python3 run.py" | grep -v grep | awk '{print $2}' | xargs -r kill -9

# 启动新进程
cd /opt && PYTHONPATH="/opt/代码牛生产区/04-科研管理系统" \
  python "/opt/代码牛生产区/04-科研管理系统/run.py" &
```

**验证（修复后）**：
- `_impStart: "called"` ✅
- `console.log` 出现 ✅
- `POST /equipment/import/commit` 网络请求出现 ✅
- SSE 进度推送正常 ✅
- DB 实际入库与预期一致 ✅

**预防**：
- **Playwright E2E 必须配"先重启 Flask"**作为前置条件，不能假设 Flask 在跑的就是最新代码
- 任何对 `app/templates/*.html` / `app/routes/*.py` / `app/utils/*.py` 的修改 → **强制重启 Flask** → 再跑 E2E
- 在 `run.py` 加 `debug=True` 可以让模板和源码自动 reload（开发期可接受）
- 用 `String(window.someFn).slice(0, 200)` 是验证"前端拿到的代码 vs 当前磁盘代码"是否一致的最快方法
- 单测绿不等于 E2E 绿。**单测（test_client）能发现后端逻辑 bug；只有 E2E（Playwright 真实点击）能发现"代码没加载"类问题**

---

## Bug: REQ-015 Playwright `page.url` 不稳定反映 `history.replaceState`

**日期**: 2026-06-25
**影响**: E2E 测试误判 URL 同步状态

### 现象

```python
# Playwright 测试
page.fill('#keywordSearch', '密码')
time.sleep(2.0)
print('page.url:', page.url)                       # ❌ 不含 keyword
print('window.location.href:', page.evaluate('window.location.href'))  # ✅ 含 keyword
```

**`page.url` 在 Playwright 中不稳定反映 `history.replaceState` 后的 URL 变化**——只在 navigation（pushState、popstate、浏览器前进后退）时更新。

### 根因

Playwright 的 `page.url` 跟踪的是 **frame URL**，**只在 navigation 事件时更新**。`history.replaceState` 不会触发 navigation，所以 `page.url` 可能仍是旧值。

但 `window.location.href` 是浏览器实时状态，**总是反映当前 URL**。

### 修复方案

E2E 测试中**用 `page.evaluate('window.location.href')` 替代 `page.url`**：

```python
# ❌ 不稳定
url = page.url

# ✅ 可靠
url = page.evaluate('window.location.href')
```

### 预防

所有 E2E 测试验证 URL 同步时，统一用 `page.evaluate('window.location.href')`。

---

## Bug: REQ-015 scroll 事件触发 hide 取消 Playwright hover 的 pending show

**日期**: 2026-06-25
**影响**: tooltip 在视口外行 hover 时不显示

### 现象

Playwright `element.hover()` 在元素位于视口外时会**先** `scrollIntoView` 再 `mouse.move`。`scrollIntoView` 触发 `window.scroll` 事件 → 我的 hide() 被调用 → **`clearTimeout(showTimer)` 取消 mouseover 已设置的 200ms pending show** → tooltip 永远不显示。

### 时间线

```
T0     Playwright scrollIntoView() → scroll event → hide() → clearTimeout(undefined)
T0+1ms Playwright mouse.move()     → mouseover    → show(tr) → setTimeout(200ms)
T0+2ms scroll 异步触发（被 Playwright 延后处理） → hide() → clearTimeout(showTimer) ❌
                                                          currentRow = null
       show 永远不会触发，tooltip 一直 display:none
```

**关键**：Playwright 的 `scrollIntoView` 用 `requestAnimationFrame` 异步滚动，scroll 事件在 mouseover **之后**才派发。hide() 看到 currentRow 不是 null 就调 clearTimeout。

### 修复

新增 `hideForScroll` 函数：scroll 触发的 hide **不清 pending showTimer**：

```js
function hideForScroll() {
    if (tooltip.classList.contains('show')) {
        // 真正显示中：正常 fade-out
        tooltip.classList.remove('show');
        setTimeout(() => {
            if (!tooltip.classList.contains('show')) {
                tooltip.style.display = 'none';
            }
        }, 120);
        currentRow = null;
    } else {
        // 还没显示（pending show）：只清 currentRow 让 mouseover 重新触发
        currentRow = null;
    }
    // 不 clearTimeout(showTimer) — 让 mouseover 的 200ms show 仍能执行
}

window.addEventListener('scroll', hideForScroll, true);
```

### 预防

- 任何依赖 mouseover + 延迟显示的 UI，scroll listener **必须区分"正在显示"和"pending 显示"**
- 不要在 scroll listener 里 `clearTimeout` mouseover 设置的 timer
- E2E 测试中视口外元素 hover 需特殊处理（auto-scroll + 异步事件时序）

---

## Bug: REQ-015 reset 后 URL 仍含旧 keyword

**日期**: 2026-06-25
**影响**: 重置按钮不能清空 URL

### 现象

点「重置」按钮：input 清空、fetch 发了无 keyword 请求，但 URL 仍含旧 keyword。

### 根因

```js
const url = new URL(window.location);  // window.location 还含 keyword
const params = new URLSearchParams(qs);  // qs 只有 fragment=1
for (const [k, v] of params.entries()) {
    if (k === 'fragment') continue;
    url.searchParams.set(k, v);  // ❌ 只 set 不 delete
}
history.replaceState(null, '', url);
```

reset 之后 fetch 的 query string 只有 `fragment=1`，**不包含** keyword，所以 `url.searchParams` 不会被设置为空，旧 keyword 保留。

### 修复

reset 时**先清空 URL 再 fetch**：

```js
window.history.replaceState(null, '', window.location.pathname);
doFetch();
```

### 预防

- 任何"清空筛选"按钮，必须**先清 URL 再 fetch**，否则旧 query 残留
- `url.searchParams.set(k, '')` **不会删除** key——要 `url.searchParams.delete(k)` 或 `new URL(pathname)` 重建

---

## Bug: REQ-016 设备导入"匹配失败，请重试"无错误原因

**日期**: 2026-06-25
**影响**: 用户上传文件失败时无法知道具体原因

### 现象

用户上传设备知识库 xlsx 后报错"匹配失败，请重试"或"解析失败"——不告知是网络断、文件格式错、HTTP 413、500 服务器错误哪种。

### 根因

`app/templates/equipment/import_step1.html` 两个 fetch handler 的 `.catch()` 块都**不接收 err 参数**，只显示固定文字：

```js
.catch(() => {                              // ❌ 不接 err
    parseError.textContent = '匹配失败，请重试';
});
```

而且 `r.json()` 会**先**解析响应体，**非 2xx 状态码 + HTML 错误页**时 JSON.parse 抛错走 catch 块，但 catch 也不显示原因。

### 修复

1. fetch 内 `r.json()` 改为：
```js
.then(r => {
    if (!r.ok) {
        return r.text().then(text => {
            let msg = 'HTTP ' + r.status;
            try {
                const j = JSON.parse(text);
                if (j && j.message) msg = j.message;
            } catch (e) {
                if (text && text.length < 200) msg += ': ' + text;
            }
            throw new Error(msg);
        });
    }
    return r.json();
})
```

2. catch 接 err：
```js
.catch((err) => {
    parseError.textContent = '匹配失败：' + (err && err.message ? err.message : '网络错误') + '。请重试';
    console.error('[import_preview]', err);
});
```

### 预防

- 任何 fetch 的 catch 块必须接 err 参数 + 显示 err.message
- HTTP 错误（4xx/5xx）要单独处理，不能直接 r.json()
- 测试时同时验证：成功 / 后端 message / 网络中断 / HTTP 500 四种错误路径

---

## Bug: REQ-016 设备导入"加装要求"字段完全缺失导入链路（6 处遗漏）

**日期**: 2026-06-25
**影响**: 用户上传的 xlsx 即使有"加装要求"列也无法导入

### 现象

设备知识库导入流程完全不支持 `installation_requirements` 字段。前端下拉无选项，后端 SQL 写死空字符串，DB 表里这个字段**永远为空**。

### 根因

DB 表 `equipment.installation_requirements` 字段已存在（创建表时就有），但**整个 import 流程完全没读这个字段**。**6 处遗漏**：

1. `templates/equipment/import_step1.html` fieldOptions 下拉无 `installation_requirements` 选项
2. `templates/equipment/import_step1.html` autoMap patterns 无 `加装要求` 关键字
3. `routes/equipment.py::import_preview` 字段提取不读 `installation_requirements`
4. `routes/equipment.py::import_save_row` allowed_fields 不含 `installation_requirements`
5. `routes/equipment.py::import_commit` 字段提取不取 `installation_requirements_{row_idx}`
6. `routes/equipment.py::import_commit` UPDATE/INSERT SQL 写死 `''`
7. `routes/equipment.py::_insert_equipment` 函数签名无 `installation_requirements` 参数
8. `templates/equipment/import_step2.html` 隐藏字段无 `installation_requirements_{row_idx}`

### 修复

6 处全部加 `installation_requirements`：
- 前端下拉 + autoMap + 隐藏字段
- 后端字段提取 + allowed_fields + SQL
- 函数签名 + INSERT VALUES

### 预防

- 任何"加字段"必须**先列全链路检查**（前端展示/前端提交/后端接收/后端处理/SQL/DB）
- DB schema 文档化时同步检查所有 import 路径
- 设备知识库 12 个字段都应该走 import 路径，可用 grep 一次性检查

## Bug: 通用表格 Flask 启动报"ModuleNotFoundError: flask"

**日期**: 2026-06-25
**影响**: REQ-016 编码后重启 Flask 失败，整个系统不可用
**触发**: `python run.py`（默认 `python` 指向 `/usr/bin/python`，未装 flask）

### 现象

`python run.py` 启动失败，报 `ModuleNotFoundError: No module named 'flask'`

### 根因

科研管理系统**没有自带 venv**——它依赖**用户级 site-packages**（`/opt/data/home/.local/lib/python3.13/site-packages`）安装的 flask 3.1.3。但 PATH 里 `python` 指向 `/usr/bin/python`（系统 Python，不识别用户级包）。

### 修复

用 `/usr/bin/python3.13` 启动（能识别用户级 site-packages）：
```
cd /opt/代码牛生产区/04-科研管理系统
pkill -f "python.*run.py"
/usr/bin/python3.13 run.py
```

### 预防

- **生产环境重启命令统一用 `/usr/bin/python3.13`**，不要用 `python` 或 `python3`
- 建议在项目根目录加 `start.sh` 脚本统一封装，避免每次手敲

## Bug: 通用表格使用专用 DB `data/generic_tables.db`（不是 `data/research.db`）

**日期**: 2026-06-25
**影响**: REQ-016 调试时找不到通用表数据
**触发**: 改通用表格后用 sqlite 查 `/opt/代码牛生产区/04-科研管理系统/data/research.db`

### 现象

查 `data/research.db` 没看到 `generic_tables`/`generic_table_data` 等表，以为迁移没跑。其实表在 **`data/generic_tables.db`**（独立数据库）。

### 根因

**通用表格模块用了独立 DB**——`models_generic_tables.py` 里 `DB_PATH` 指向 `data/generic_tables.db`，跟主系统的 `data/research.db` 分离。可能是早期为了模块解耦。

### 预防

- 调试任何模块前先看模型文件的 `DB_PATH` 常量
- 科研管理系统下所有 DB：
  - `data/research.db` — 设备/项目/用户/标准等主业务
  - `data/generic_tables.db` — 通用表格管理
  - `data/equipment.db` — 设备知识库（独立，可能是测试用）
  - `data/expense.db` — 报销
  - `app/users.db` — 早期用户

## Bug: 通用表格"激活态占位色块"被点击不响应

**日期**: 2026-06-25
**影响**: 行染色无法取消（再点已激活色块无效）
**触发**: REQ-016 编码时设计"激活态用额外占位色块显示"

### 现象

行已染成红色，DOM 上出现"激活态占位红色块"（只有 `data-color` 没有 `data-row-key`）。用户点这个色块，**没反应**——JS 处理器 `chip.dataset.rowKey` 为空直接 `return`。

### 根因

`renderTable` 渲染激活态占位色块时只给了 `data-color`，没给 `data-row-key`：
```js
const activeChip = rowColor ? `<span class="color-chip color-${rowColor} active" data-color="${rowColor}"></span>` : '';
```
JS 处理器对"激活态色块"和"普通色块"**共用同一条事件**——共用就要共用 data 属性。

### 修复

激活态占位色块也加 `data-row-key`：
```js
const activeChip = rowColor ? `<span class="color-chip color-${rowColor} active" data-color="${rowColor}" data-row-key="${row.row_key}"></span>` : '';
```

### 预防

- 任何"状态指示器"元素，**都应响应交互**（点击/悬停/键盘）
- 同一类交互的所有元素，**数据属性必须一致**——别让"显示态"少一两个
- E2E 测试要覆盖"再点同一项取消"这种场景，不能只测"切到新值"

## Bug: Pyright/LSP 报"方法重复声明"——`reorder_columns` 早已存在

**日期**: 2026-06-25
**影响**: REQ-016 编码差点引入覆盖型 bug
**触发**: 直接读 model 类末尾追加新函数，没检查整个类方法列表

### 现象

Pyright 报 `ERROR [340:9] Method declaration "reorder_columns" is obscured by a declaration of the same name`

### 根因

`GenericTableModel.reorder_columns(version_id, col_keys)` **已存在**（line 340），签名是 `col_keys` 不是 `order`。我按"白纸"写代码设计，没核查到这一点。

### 修复

- 删除我新加的 `reorder_columns`（重复）
- 改用现有 `reorder_columns(version_id, col_keys)`
- 前端 fetch 时键名用 `col_keys` 而非 `order`

### 预防

- 编码前先 grep 整个 model**：`grep "def " models_xxx.py` 看现有方法
- 代码设计阶段先看现有 model**，不能"按理想设计"写白纸
- 接到 LSP/IDE 警告要**优先看**，不要忽略

## Bug: 通用表格 `upsert_column` 用 `or -1` 短路导致 col_index 全部撞 0

**日期**: 2026-06-25
**影响**: REQ-016 拖拽改列顺序后，列顺序"看起来错乱"且多次刷新可能返回不同顺序

### 根因
`app/models_generic_tables.py::upsert_column` 在新增列时计算 `col_index`：

```python
c.execute('SELECT MAX(col_index) as max_i FROM generic_table_columns WHERE version_id = ?', (version_id,))
row = c.fetchone()
col_index = (row['max_i'] or -1) + 1   # ❌ BUG
```

`row['max_i']` 当 MAX(col_index) = 0 时是 `0`（不是 None）。**Python 的 `0 or -1` 因为 0 是 falsy，**实际返回 -1**——**所以 col_index 永远从 0 重复分配**：

- 加第 1 列：MAX=NULL → -1+1=0 ✓
- 加第 2 列：MAX=0 → `0 or -1` = -1 → -1+1=0 ❌（应该 1）
- 加第 3 列：MAX=0 → `0 or -1` = -1 → -1+1=0 ❌（应该 2）

结果：同一 version 的所有列 col_index 全部 = 0。

**`get_columns()` 用 `ORDER BY col_index` 排序**——3 个相同值时 SQLite 返回顺序**不稳定**（按主键 id 升序）——导致页面渲染时**列顺序看似错乱**，**多次刷新顺序可能变化**。

同样的 bug 在 3 处：
1. line 290-291（`upsert_column` 带 `_conn` 路径）
2. line 312-315（`upsert_column` 无 `_conn` 路径）
3. line 467-468（`import_rows_from_excel` 算 `row_index`）

### 修复
把 `or -1` 改成 `is not None` 判断：

```python
# 修复
max_i = row['max_i'] if row['max_i'] is not None else -1
col_index = max_i + 1
```

### 触发场景
- 用户用 UI 添加多列（每次新加）
- 用户用 Excel 导入多列

### 数据迁移
需要把已撞车的 version 重新按 id 顺序分配 col_index：

```sql
UPDATE generic_table_columns SET col_index = (
    SELECT COUNT(*) FROM generic_table_columns c2
    WHERE c2.version_id = generic_table_columns.version_id
    AND c2.id <= generic_table_columns.id
) - 1
WHERE version_id IN (SELECT version_id FROM ...);
```

### 预防
- **不要用 `or default` 处理 0/空字符串/空集合**——这些是 falsy 值，**`is None` 才是判断 None 的正确方法**
- 涉及 MAX/MIN 计数时，**显式判断 None**，不用 falsy 短路
- 加列后用 `python3 -c "import sqlite3; ..."` 查 DB 验证 col_index 是不是递增的
- Playwright 验证：建新表 + 加 3 列 → DB 查 col_index 应该是 [0, 1, 2]

## Bug: 通用表格列宽拖拽"能拖大不能拖小"

**日期**: 2026-06-25
**影响**: REQ-016 列宽调整功能

### 现象
用户拖列宽往小时，列宽不变化（或卡在 800px 最大值）。

### 根因
`app/templates/generic_tables/detail.html` 列宽拖拽实现有 2 个问题：

1. **CSS 缺 `table-layout: fixed`**
   浏览器默认 `table-layout: auto`，th 的 `style.width` 只起 `min-width` 作用。
   **实际宽度由内容撑开**——长内容会把列撑得比 style.width 大很多。
   拖完第一次后：用户设 style.width=554，但浏览器渲染 903。

2. **startW 用 `getBoundingClientRect()` 而非 style.width**
   ```js
   startW: th.getBoundingClientRect().width  // ❌ 返回内容撑开宽度 903
   ```
   第二次拖小 100px：newW = 903 - 100 = 803 → **被 max 800 钳住** → 用户感觉"拖不动"。

### 修复
1. CSS 加 `table-layout: fixed` —— style.width 严格生效
2. `td.cell-edit` 加 `white-space: nowrap; overflow: hidden; text-overflow: ellipsis` —— 长内容省略号，不撑列
3. JS 改用 `parseFloat(th.style.width)` 作为 startW —— 防御性修复，即使 table-layout 失效也能正确计算

### 触发场景
- 任何含长内容的列（值长度 > style.width 设定值）
- 拖完一次后再调整

### 预防
- **拖拽宽度类功能必须用 `table-layout: fixed`**，否则 style.width 不可靠
- **startW 优先用 style.width 解析值**，不要直接用 getBoundingClientRect
- 长内容必须加 `text-overflow: ellipsis` 防止撑列
- Playwright 验证：连续拖大 200 + 拖小 100，style.width 应该正确递减


## REQ-018: 通用表格快照未固化 + 默认 label 无日期

**症状**（用户实报 2026-06-27）：
- Bug 1：建立快照后，后续表格更改会对前序快照状态产生影响
- Bug 2：建立快照时没有日期，多次快照名称（label）都是 v1/v2/v3，无法区分

**根因**（SQL UPDATE 缺 version_id 过滤）：
- `app/models_generic_tables.py:400` `upsert_row` UPDATE 行数据时 `WHERE row_key=?` 缺 `version_id`
- `app/models_generic_tables.py:357` `delete_column` 删列时 `WHERE row_key=?` 缺 `version_id`
- 表 schema：`(version_id, row_key)` 是 UNIQUE，但 SQL 不带 version_id 时会**同时改所有版本的同 row_key 数据**
- `create_version` 第 259 行默认 label 是 `f'v{new_version_number}'`，无日期

**复现路径**：
```
1. v1 创建：r1 = {name: 张三, age: 30}
2. v2 快照：r1 = {name: 张三, age: 30}  (复制)
3. 改 v2 r1 = {name: 李四, age: 25}
4. 查 v1 r1: 应该是 张三/30
   修复前: 李四/25  ← 错误！v1 被改了
   修复后: 张三/30  ← 正确
```

**修复方案**（3 处 SQL 改动）：

代码块（python）：

```python
# 1. upsert_row 第 400-401 行
# 错的：
c.execute('UPDATE generic_table_data SET row_data=?, updated_at=? WHERE row_key=?',
          (json.dumps(row_data_dict, ensure_ascii=False), now, row_key))
# 对的：
c.execute('UPDATE generic_table_data SET row_data=?, updated_at=? WHERE row_key=? AND version_id=?',
          (json.dumps(row_data_dict, ensure_ascii=False), now, row_key, version_id))

# 2. delete_column 第 357-358 行
# 错的：
c.execute('UPDATE generic_table_data SET row_data=? WHERE row_key=?',
          (json.dumps(data, ensure_ascii=False), row['row_key']))
# 对的：
c.execute('UPDATE generic_table_data SET row_data=? WHERE row_key=? AND version_id=?',
          (json.dumps(data, ensure_ascii=False), row['row_key'], version_id))

# 3. create_version 第 257-261 行 默认 label
# 错的：
version_label=label or f'v{new_version_number}'
# 对的：
version_label=label or f'v{new_version_number}_{datetime.now().strftime("%Y%m%d_%H%M")}'
```

**验证**（临时 SQLite + Python 脚本 `/tmp/req018_verify.py`）：
- 创建临时数据库目录
- Mock config.DATA_DIR 指向临时目录
- 跑 init_db 建表
- 用 GenericTableModel 实例跑完整复现 + 验证
- 3 个 assert 全过：label 带日期、v1 r1 未受影响、delete_column 不跨版本

**涉及文件**：
- `app/models_generic_tables.py`（3 处 SQL 修复，commit 标识：REQ-018-fix）
- `ERROR_KNOWLEDGE.md`（新增 REQ-018 条目）
- `/tmp/req018_verify.py`（验证脚本，不入 git）

**预防规范**（写入开发规范）：
- **多版本表（generic_table_data, generic_table_columns）所有 UPDATE/DELETE 必须带 version_id**
- **任何表（version_id 字段）修改数据前要明确"操作哪个版本"**
- 通用表 schema：(table_id, row_key) + version_id 复合唯一，SQL 必须带 version_id
- Code Review 检查项：grep `UPDATE.*generic_table_data\|UPDATE.*generic_table_columns` 找所有缺 version_id 的地方
- 默认 label 命名约定：`v{N}_YYYYMMDD_HHMM`（版本号+日期+时间，ASCII 格式，跨平台兼容）

**类似潜在 bug 排查**（已扫过 models_generic_tables.py）：
- 第 300, 326, 351, 368, 422, 477, 620, 631 行 UPDATE/DELETE 都正确带 version_id
- 只有 357 和 400 这两处遗漏
