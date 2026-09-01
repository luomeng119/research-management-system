// REQ-015: 设备知识库列表页 - 实时搜索 + 自定义行 tooltip
(function () {
    'use strict';

    // ============================================================
    // 第一部分: 实时搜索 (debounce + AJAX)
    // ============================================================
    const keywordInput = document.getElementById('keywordSearch');
    const tableWrap = document.getElementById('equipmentTableWrap');
    const loadingMask = document.getElementById('tableLoadingMask');
    const loadingIcon = document.getElementById('searchLoading');
    const successIcon = document.getElementById('searchSuccess');
    const errorBar = document.getElementById('searchError');
    const errorMsg = document.getElementById('searchErrorMsg');
    const retryBtn = document.getElementById('searchRetryBtn');
    const errorClose = document.getElementById('searchErrorClose');

    if (!keywordInput || !tableWrap) {
        // 页面没有搜索框/表格（如非设备列表页）就跳过
    } else {
        let debounceTimer = null;
        let lastController = null;
        let errorHideTimer = null;

        function getQueryString() {
            const form = keywordInput.closest('form');
            const params = new URLSearchParams(new FormData(form));
            params.set('fragment', '1');
            // 移除空值
            for (const [k, v] of [...params.entries()]) {
                if (!v) params.delete(k);
            }
            return params.toString();
        }

        function doFetch() {
            const qs = getQueryString();
            if (lastController) lastController.abort();
            const controller = new AbortController();
            lastController = controller;

            // 加载状态
            loadingIcon.style.display = 'inline';
            successIcon.style.display = 'none';
            errorBar.style.display = 'none';
            if (errorHideTimer) clearTimeout(errorHideTimer);
            loadingMask.style.display = 'flex';

            fetch('/equipment?' + qs, { signal: controller.signal, headers: { 'X-Requested-With': 'fetch' } })
                .then(r => {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.text();
                })
                .then(html => {
                    if (controller.signal.aborted) return;
                    // 解析 HTML 提取 #equipmentTableWrap
                    const parser = new DOMParser();
                    const doc = parser.parseFromString(html, 'text/html');
                    const newWrap = doc.getElementById('equipmentTableWrap');
                    if (newWrap) {
                        tableWrap.innerHTML = newWrap.innerHTML;
                    } else {
                        throw new Error('响应中找不到 #equipmentTableWrap');
                    }
                    // URL 同步
                    const url = new URL(window.location);
                    const params = new URLSearchParams(qs);
                    for (const [k, v] of params.entries()) {
                        if (k === 'fragment') continue;
                        url.searchParams.set(k, v);
                    }
                    history.replaceState(null, '', url);

                    // 反馈
                    loadingMask.style.display = 'none';
                    loadingIcon.style.display = 'none';
                    successIcon.style.display = 'inline';
                    setTimeout(() => { successIcon.style.display = 'none'; }, 1500);
                })
                .catch(err => {
                    if (err.name === 'AbortError') return;
                    loadingMask.style.display = 'none';
                    loadingIcon.style.display = 'none';
                    errorMsg.textContent = '⚠️ 检索失败：' + err.message + '。点击重试';
                    errorBar.style.display = 'flex';
                    if (errorHideTimer) clearTimeout(errorHideTimer);
                    errorHideTimer = setTimeout(() => { errorBar.style.display = 'none'; }, 5000);
                });
        }

        function onInput() {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(doFetch, 300);
        }

        // input 事件: 防抖 300ms
        keywordInput.addEventListener('input', onInput);

        // Enter 键: 立即提交，绕过 debounce
        keywordInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                clearTimeout(debounceTimer);
                doFetch();
            }
        });

        // 「重置」按钮: 跳到无参数 URL
        const resetLink = document.querySelector('a.btn-outline-secondary[href*="/equipment"]');
        if (resetLink) {
            resetLink.addEventListener('click', function (e) {
                e.preventDefault();
                keywordInput.value = '';
                // 清空其他 select
                const form = keywordInput.closest('form');
                if (form) {
                    form.querySelectorAll('select').forEach(sel => { sel.value = ''; });
                }
                // 先清空 URL query（doFetch 内部只 set 不 delete，保留旧 keyword）
                window.history.replaceState(null, '', window.location.pathname);
                doFetch();
            });
        }

        // 重试按钮
        retryBtn.addEventListener('click', doFetch);
        // 关闭错误条
        errorClose.addEventListener('click', function () {
            errorBar.style.display = 'none';
            if (errorHideTimer) clearTimeout(errorHideTimer);
        });
    }

    // ============================================================
    // 第二部分: 自定义行 tooltip
    // ============================================================
    const tooltip = document.getElementById('rowTooltip');
    if (!tooltip) return;

    let showTimer = null;
    let currentRow = null;
    let currentMouseX = 0;
    let currentMouseY = 0;

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function buildTooltipHtml(raw) {
        if (!raw) return '';
        const lines = raw.split('\n').map(l => l.trim()).filter(l => l);
        return lines.map(line => {
            const idx = line.indexOf(':');
            if (idx === -1) {
                return '<div class="tt-row"><span class="tt-value">' + escapeHtml(line) + '</span></div>';
            }
            const label = line.slice(0, idx).trim();
            const value = line.slice(idx + 1).trim();
            return '<div class="tt-row"><span class="tt-label">' + escapeHtml(label) + ':</span> <span class="tt-value">' + escapeHtml(value) + '</span></div>';
        }).join('');
    }

    function positionTooltip() {
        const offset = 15;
        let x = currentMouseX + offset;
        let y = currentMouseY + offset;
        // tooltip 必须先 show 才能取 width/height（CSS opacity=0 时也算）
        const rect = tooltip.getBoundingClientRect();
        if (x + rect.width > window.innerWidth - 8) {
            x = currentMouseX - rect.width - offset;
            if (x < 8) x = 8;
        }
        if (y + rect.height > window.innerHeight - 8) {
            y = currentMouseY - rect.height - offset;
            if (y < 8) y = 8;
        }
        tooltip.style.left = x + 'px';
        tooltip.style.top = y + 'px';
    }

    function show(row) {
        clearTimeout(showTimer);
        showTimer = setTimeout(() => {
            const raw = row.getAttribute('data-tooltip-html');
            if (!raw) return;
            tooltip.innerHTML = buildTooltipHtml(raw);
            tooltip.style.display = 'block';
            positionTooltip();
            // 强制 reflow 后加 show class 触发 fade-in
            void tooltip.offsetWidth;
            tooltip.classList.add('show');
            currentRow = row;
        }, 200);
    }

    function hide() {
        clearTimeout(showTimer);
        tooltip.classList.remove('show');
        // 等待 fade-out 完成
        setTimeout(() => {
            if (!tooltip.classList.contains('show')) {
                tooltip.style.display = 'none';
            }
        }, 120);
        currentRow = null;
    }

    // scroll 触发的特殊 hide：不取消 mouseover 设置的 pending show（200ms）
    // 原因：Playwright hover() 内部先 scrollIntoView 再 mouse.move，
    //       如果 mouseover 200ms show 还未到就被 hide 取消，tooltip 永远不显示
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

    // 事件委托: 绑到 tableWrap（容器）保证 AJAX 替换 innerHTML 后仍生效
    tableWrap.addEventListener('mouseover', function (e) {
        const tr = e.target.closest('tr');
        if (!tr || !tr.hasAttribute('data-tooltip-html')) return;
        if (tr === currentRow) return;
        // 切换到新行
        if (currentRow) {
            clearTimeout(showTimer);
        }
        show(tr);
    });

    tableWrap.addEventListener('mousemove', function (e) {
        currentMouseX = e.clientX;
        currentMouseY = e.clientY;
        if (currentRow && tooltip.classList.contains('show')) {
            positionTooltip();
        }
    });

    tableWrap.addEventListener('mouseout', function (e) {
        const tr = e.target.closest('tr');
        if (!tr) return;
        // 检查鼠标是否真的离开了 tr（移入子元素不算离开）
        const related = e.relatedTarget;
        if (related && tr.contains(related)) return;
        hide();
    });

    // 滚动 / resize 隐藏
    // scroll 触发时不能简单 hide()，否则会取消 Playwright hover() 内部
    // scrollIntoView 之后 mouseover 设置的 200ms pending show。
    // 用 hideForScroll：只 hide 真正显示中的，不清 pending showTimer。
    window.addEventListener('scroll', hideForScroll, true);
    window.addEventListener('resize', hide);
})();
