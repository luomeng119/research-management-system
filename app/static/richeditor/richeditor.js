/**
 * 离线富文本编辑器 - 完全独立，无任何外部依赖
 * 支持：加粗、斜体、下划线、删除线、标题、有序/无序列表、链接、图片、表格、水平线、撤销/重做
 */
(function() {
    'use strict';

    window.RichEditor = RichEditor;

    function RichEditor(textareaId, options) {
        this.textarea = document.getElementById(textareaId);
        if (!this.textarea) return;

        this.id = textareaId;
        this.options = Object.assign({
            height: 400,
            placeholder: '请输入内容...'
        }, options || {});

        this._build();
        this._bind();
    }

    RichEditor.prototype._build = function() {
        var wrapper = document.createElement('div');
        wrapper.className = 'rich-editor-wrapper';
        wrapper.style.border = '1px solid #dee2e6';
        wrapper.style.borderRadius = '4px';
        wrapper.style.overflow = 'hidden';

        // 工具栏
        var toolbar = document.createElement('div');
        toolbar.className = 'rich-editor-toolbar';
        toolbar.style.cssText = 'background:#f8f9fa;border-bottom:1px solid #dee2e6;padding:6px 8px;display:flex;flex-wrap:wrap;gap:2px;align-items:center;';

        var buttons = this._getButtons();
        buttons.forEach(function(group) {
            group.forEach(function(btn) {
                if (btn === '|') {
                    var sep = document.createElement('span');
                    sep.style.cssText = 'width:1px;height:20px;background:#ccc;margin:0 4px;display:inline-block;';
                    toolbar.appendChild(sep);
                } else {
                    var b = document.createElement('button');
                    b.type = 'button';
                    b.title = btn.title;
                    b.dataset.cmd = btn.cmd;
                    b.dataset.value = btn.value || '';
                    b.innerHTML = btn.icon;
                    b.style.cssText = 'background:none;border:1px solid transparent;border-radius:3px;padding:4px 6px;cursor:pointer;font-size:14px;line-height:1;min-width:28px;color:#333;';
                    b.onmouseover = function() { this.style.background = '#e9ecef'; this.style.borderColor = '#ced4da'; };
                    b.onmouseout = function() { this.style.background = 'none'; this.style.borderColor = 'transparent'; };
                    toolbar.appendChild(b);
                }
            });
        });

        // 编辑区
        var editor = document.createElement('div');
        editor.className = 'rich-editor-content';
        editor.contentEditable = true;
        editor.style.cssText = 'min-height:' + this.options.height + 'px;padding:12px;outline:none;overflow-y:auto;background:#fff;font-family:inherit;font-size:14px;line-height:1.6;';
        editor.dataset.placeholder = this.options.placeholder;

        // 隐藏原textarea，editor内容同步到textarea
        this.textarea.style.display = 'none';
        this.textarea.parentNode.insertBefore(wrapper, this.textarea);
        wrapper.appendChild(toolbar);
        wrapper.appendChild(editor);

        this.toolbar = toolbar;
        this.editor = editor;

        // 同步内容到textarea
        this._syncToTextarea();
    };

    RichEditor.prototype._getButtons = function() {
        var self = this;
        return [
            [
                {cmd: 'bold', title: '加粗 (Ctrl+B)', icon: '<b>B</b>'},
                {cmd: 'italic', title: '斜体 (Ctrl+I)', icon: '<i>I</i>'},
                {cmd: 'underline', title: '下划线 (Ctrl+U)', icon: '<u>U</u>'},
                {cmd: 'strikeThrough', title: '删除线', icon: '<s>S</s>'},
            ],
            ['|'],
            [
                {cmd: 'fontName', title: '字体', icon: '字体', isSelect: true,
                 options: [
                     {label: '宋体', value: 'SimSun'},
                     {label: '黑体', value: 'SimHei'},
                     {label: '微软雅黑', value: 'Microsoft YaHei'},
                     {label: '楷体', value: 'KaiTi'},
                     {label: '仿宋', value: 'FangSong'},
                     {label: 'Arial', value: 'Arial'},
                     {label: 'Times New Roman', value: 'Times New Roman'},
                     {label: 'Courier New', value: 'Courier New'},
                 ]},
                {cmd: 'fontSize', title: '字号', icon: '字号', isSelect: true,
                 options: [
                     {label: '小', value: '1'},
                     {label: '标准', value: '3'},
                     {label: '大', value: '4'},
                     {label: '特大', value: '5'},
                     {label: '超大', value: '6'},
                     {label: '巨大', value: '7'},
                 ]},
            ],
            ['|'],
            [
                {cmd: 'insertUnorderedList', title: '无序列表', icon: '•≡'},
                {cmd: 'insertOrderedList', title: '有序列表', icon: '1.≡'},
            ],
            ['|'],
            [
                {cmd: 'formatBlock', value: 'h2', title: '标题2', icon: 'H2'},
                {cmd: 'formatBlock', value: 'h3', title: '标题3', icon: 'H3'},
                {cmd: 'formatBlock', value: 'p', title: '正文', icon: 'P'},
            ],
            ['|'],
            [
                {cmd: 'createLink', title: '插入链接', icon: '🔗'},
                {cmd: 'insertImage', title: '插入图片', icon: '🖼'},
                {cmd: 'insertTable', title: '插入表格', icon: '▦'},
                {cmd: 'insertHorizontalRule', title: '水平线', icon: '—'},
            ],
            ['|'],
            [
                {cmd: 'undo', title: '撤销', icon: '↩'},
                {cmd: 'redo', title: '重做', icon: '↪'},
            ],
            ['|'],
            [
                {cmd: 'removeFormat', title: '清除格式', icon: '⊗'},
            ]
        ];
    };

    RichEditor.prototype._bind = function() {
        var self = this;

        // 工具栏按钮点击（普通按钮）
        this.toolbar.addEventListener('click', function(e) {
            var btn = e.target.closest('button[data-cmd]');
            if (!btn) return;
            e.preventDefault();
            var cmd = btn.dataset.cmd;
            var value = btn.dataset.value;

            if (cmd === 'insertTable') {
                self._insertTable();
            } else if (cmd === 'createLink') {
                var url = prompt('请输入链接地址：', 'http://');
                if (url) document.execCommand(cmd, false, url);
            } else if (cmd === 'insertImage') {
                var src = prompt('请输入图片地址：', 'http://');
                if (src) document.execCommand(cmd, false, src);
            } else {
                document.execCommand(cmd, false, value || null);
            }
            self.editor.focus();
            self._syncToTextarea();
        });

        // 下拉选择器（字体、字号）
        this.toolbar.addEventListener('change', function(e) {
            var sel = e.target.closest('select[data-cmd]');
            if (!sel) return;
            e.preventDefault();
            var cmd = sel.dataset.cmd;
            var value = sel.value;
            if (cmd && value) {
                document.execCommand(cmd, false, value);
                self.editor.focus();
                self._syncToTextarea();
            }
        });

        // 键盘快捷键
        this.editor.addEventListener('keydown', function(e) {
            if ((e.ctrlKey || e.metaKey) && e.key === 'b') { e.preventDefault(); document.execCommand('bold', false, null); }
            if ((e.ctrlKey || e.metaKey) && e.key === 'i') { e.preventDefault(); document.execCommand('italic', false, null); }
            if ((e.ctrlKey || e.metaKey) && e.key === 'u') { e.preventDefault(); document.execCommand('underline', false, null); }
        });

        // 内容变化时同步到textarea
        this.editor.addEventListener('input', function() {
            self._syncToTextarea();
        });

        // 粘贴时同步
        this.editor.addEventListener('paste', function() {
            setTimeout(function() { self._syncToTextarea(); }, 10);
        });
    };

    RichEditor.prototype._insertTable = function() {
        var rows = prompt('行数：', '3');
        var cols = prompt('列数：', '3');
        if (!rows || !cols) return;
        rows = parseInt(rows);
        cols = parseInt(cols);
        if (rows < 1 || cols < 1) return;

        var table = '<table style="width:100%;border-collapse:collapse;font-size:13px;" border="1" cellpadding="4" cellspacing="0">';
        for (var r = 0; r < rows; r++) {
            table += '<tr>';
            for (var c = 0; c < cols; c++) {
                table += '<td style="border:1px solid #ccc;min-width:60px;">' + (r === 0 ? '表头' : '') + '</td>';
            }
            table += '</tr>';
        }
        table += '</table><p></p>';
        document.execCommand('insertHTML', false, table);
    };

    RichEditor.prototype._syncToTextarea = function() {
        this.textarea.value = this.editor.innerHTML;
    };

    RichEditor.prototype.getContent = function() {
        return this.editor.innerHTML;
    };

    RichEditor.prototype.setContent = function(html) {
        this.editor.innerHTML = html || '';
        this._syncToTextarea();
    };

    // 初始化函数：给所有 textarea.rich-editor 自动替换
    window.initRichEditors = function() {
        document.querySelectorAll('textarea.rich-editor').forEach(function(ta) {
            if (!ta.dataset.richInit) {
                ta.dataset.richInit = '1';
                new RichEditor(ta.id, { height: parseInt(ta.dataset.height) || 400 });
            }
        });
    };

    // DOMReady 初始化
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', window.initRichEditors);
    } else {
        window.initRichEditors();
    }
})();
