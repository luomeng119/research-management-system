# T04 实施报告

- Base：`0f18fb894e8aa34091f8afd6548c322a5ec1cb7d`
- 结果：完成 PostgreSQL 受控文件、版本、对象关联、授权下载/预览/归档，以及旧预览和报销附件读取边界收口。

## TDD 与验证

- 初始 RED：`app/tests/test_file_service.py` 因 `app.repositories.files` 不存在而在收集阶段失败。
- 服务与 Web GREEN：本地文件测试得到 `29 passed, 3 skipped`；覆盖路径穿越、反斜线、控制字符、双重编码、伪扩展、空文件、超限、同名不覆盖、类型锁定、版本冲突、对象删除、归档、审计失败补偿、symlink、ZIP/OOXML 膨胀、角色边界及预览降级。
- 真实 PostgreSQL：隔离临时集群完成 Alembic 升降级、最小权限验证、串行版本契约、两个并发版本写者序列化，以及移动后审计失败的数据库回滚和物理文件补偿；T04 用例 `3 passed, 27 deselected`，临时集群自动清理。
- T04、认证审计、数据库契约和 runner safety 合并回归：`91 passed, 24 skipped`。
- `compileall` 与 `git diff --check` 通过。
- 独立代码复核：`APPROVED`，无 P0/P1。
- 独立安全复核：`APPROVED`，无 P0/P1；超预算 DOCX/XLSX 在第三方解析前降级，正常文件仍可解析。

## 关键实现

- 上传采用临时写入、流式大小与 SHA-256、扩展名和内容签名校验、UUID 存储名、同卷原子移动；数据库或审计失败会回滚并删除本次物理文件。
- `stored_files`、`stored_file_versions`、`object_files` 形成逻辑文件、不可覆盖版本和业务对象关联；PostgreSQL 使用对象 `FOR SHARE` 与文件 `FOR UPDATE` 控制并发。
- 仅 `BUSINESS_USER` 可访问业务附件；维护账号、未登录、对象不匹配、已删除对象和已归档文件均被拒绝。
- 下载使用数据库相对路径、逐级 no-follow 句柄和 SHA/大小复核；Windows 分支通过最终句柄路径验证。HTTP 不返回内部存储路径。
- 预览在第三方 Office 库前先流式限制 ZIP 条目/展开量、XML 字节/节点/字符数及实体，解析后再限制行列与返回字符预算；失败返回受控下载地址。旧 `path=` 预览入口被拒绝。
- 旧报销附件兼容 uploads 根内历史绝对路径，但拒绝越界、symlink 及非服务账号独占写入的目录链；PDF 附件增加页数和像素预算。
- 上传、版本、归档、下载、预览的成功和失败均写低敏审计事件。

## 范围与后续风险

- 未引入复杂 RBAC、对象存储、病毒扫描、异步队列、Office 在线转换、OCR 流水线或目录权限树。
- `os.replace` 后、数据库提交前进程被强杀仍存在极短孤儿窗口；这是已确认的 V1 边界，T12 必须执行数据库引用与磁盘文件对账，不在 T04 引入双阶段状态机。
- Windows 句柄分支已实现但当前 macOS 开发机不能提供真实 Windows 运行证据；须在后续离线部署验收中实机验证。
- T04 只证明新文件服务和 preview/documents 适配边界；其他旧模块逐步委托新服务属于后续已排任务，不能据此宣称全系统文件路径已全部迁移。
