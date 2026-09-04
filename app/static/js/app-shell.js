(function () {
    'use strict';

    var appShell = document.getElementById('appShell');
    if (!appShell) return;

    var path = window.location.pathname;
    var pageMap = [
        { key: 'proposals', label: '科研提案', match: function (value) { return value.indexOf('/proposals') === 0; } },
        { key: 'experts', label: '专家库', match: function (value) { return value.indexOf('/experts') === 0; } },
        { key: 'resources', label: '科研资源', match: function (value) { return value.indexOf('/equipment') === 0 || value.indexOf('/standards') === 0 || value.indexOf('/templates') === 0 || value.indexOf('/host_devices') === 0 || value.indexOf('/research_units') === 0; } },
        { key: 'tools', label: '辅助工具', match: function (value) { return value.indexOf('/utils') === 0 || value.indexOf('/expense') === 0 || value.indexOf('/tables') === 0; } },
        { key: 'projects', label: '科研项目', match: function (value) { return value.indexOf('/projects') === 0 || value.indexOf('/security_projects') === 0 || value.indexOf('/crypto_projects') === 0 || value.indexOf('/argumentation') === 0; } },
        { key: 'dashboard', label: '工作台', match: function (value) { return value === '/'; } }
    ];

    var currentPage = pageMap.find(function (item) { return item.match(path); }) || pageMap[5];
    var activeNav = document.querySelector('[data-primary-nav="' + currentPage.key + '"]');
    if (activeNav) {
        activeNav.classList.add('active');
        activeNav.setAttribute('aria-current', 'page');
    }
    var crumb = document.getElementById('pageCrumb');
    if (crumb) crumb.textContent = currentPage.label;

    var collapsePrimary = document.getElementById('collapsePrimaryNav');
    var narrowViewport = window.matchMedia('(max-width: 1279px)');
    var storedPrimaryState = sessionStorage.getItem('primaryNavCollapsed');
    if (storedPrimaryState === 'true') appShell.classList.add('nav-collapsed');

    function syncPrimaryControl() {
        if (!collapsePrimary) return;
        var isAutoCollapsed = narrowViewport.matches;
        var isCollapsed = isAutoCollapsed || appShell.classList.contains('nav-collapsed');
        collapsePrimary.setAttribute('aria-expanded', String(!isCollapsed));
        collapsePrimary.setAttribute('aria-label', isAutoCollapsed ? '导航在当前宽度下已收起' : (isCollapsed ? '展开导航' : '收起导航'));
        collapsePrimary.disabled = isAutoCollapsed;
        var label = collapsePrimary.querySelector('span');
        if (label) label.textContent = isAutoCollapsed ? '导航已收起' : (isCollapsed ? '展开导航' : '收起导航');
    }
    syncPrimaryControl();
    if (collapsePrimary) {
        collapsePrimary.addEventListener('click', function () {
            appShell.classList.toggle('nav-collapsed');
            sessionStorage.setItem('primaryNavCollapsed', String(appShell.classList.contains('nav-collapsed')));
            syncPrimaryControl();
        });
    }
    if (narrowViewport.addEventListener) narrowViewport.addEventListener('change', syncPrimaryControl);

    var directoryToggle = document.getElementById('directoryToggle');
    var directorySidebar = document.getElementById('sidebar');
    var resizer = document.getElementById('resizer');
    if (sessionStorage.getItem('directoryCollapsed') === 'true') appShell.classList.add('directory-collapsed');

    function syncDirectoryControl() {
        if (!directoryToggle) return;
        var isCollapsed = appShell.classList.contains('directory-collapsed');
        directoryToggle.setAttribute('aria-expanded', String(!isCollapsed));
        directoryToggle.setAttribute('aria-label', isCollapsed ? '展开二级目录' : '收起二级目录');
    }
    syncDirectoryControl();
    if (directoryToggle) {
        directoryToggle.addEventListener('click', function () {
            appShell.classList.toggle('directory-collapsed');
            sessionStorage.setItem('directoryCollapsed', String(appShell.classList.contains('directory-collapsed')));
            syncDirectoryControl();
        });
    }

    if (directorySidebar && resizer) {
        var savedWidth = Number(sessionStorage.getItem('directoryWidth'));
        if (savedWidth >= 180 && savedWidth <= 380) directorySidebar.style.width = savedWidth + 'px';
        resizer.setAttribute('aria-valuenow', String(savedWidth >= 180 && savedWidth <= 380 ? savedWidth : 250));
        var dragging = false;
        var startX = 0;
        var startWidth = 0;
        resizer.addEventListener('mousedown', function (event) {
            dragging = true; startX = event.clientX; startWidth = directorySidebar.getBoundingClientRect().width;
            document.body.style.cursor = 'col-resize'; event.preventDefault();
        });
        document.addEventListener('mousemove', function (event) {
            if (!dragging) return;
            var width = Math.max(180, Math.min(380, startWidth + event.clientX - startX));
            directorySidebar.style.width = width + 'px'; resizer.setAttribute('aria-valuenow', String(Math.round(width)));
        });
        document.addEventListener('mouseup', function () {
            if (!dragging) return;
            dragging = false; document.body.style.cursor = '';
            sessionStorage.setItem('directoryWidth', String(Math.round(directorySidebar.getBoundingClientRect().width)));
        });
        resizer.addEventListener('keydown', function (event) {
            if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
            var delta = event.key === 'ArrowLeft' ? -10 : 10;
            var width = Math.max(180, Math.min(380, directorySidebar.getBoundingClientRect().width + delta));
            directorySidebar.style.width = width + 'px'; resizer.setAttribute('aria-valuenow', String(width)); sessionStorage.setItem('directoryWidth', String(width)); event.preventDefault();
        });
    }

    function escapeHtml(value) {
        var element = document.createElement('span');
        element.textContent = value == null ? '' : String(value);
        return element.innerHTML;
    }

    function buildNodeHtml(node) {
        var hasChildren = Array.isArray(node.children) && node.children.length > 0;
        var url = node.url || '';
        var icon = /^bi-[a-z0-9-]+$/.test(node.icon || '') ? node.icon : 'bi-folder';
        var html = '<li class="tree-node"><div class="tree-row">';
        if (hasChildren) {
            html += '<button class="tree-toggle" type="button" data-tree-toggle data-id="' + escapeHtml(node.id) + '" aria-label="展开' + escapeHtml(node.label) + '" aria-expanded="false"><i class="bi bi-chevron-right toggle-icon" aria-hidden="true"></i></button>';
        } else {
            html += '<span class="tree-toggle-spacer" aria-hidden="true"></span>';
        }
        html += '<a class="tree-link" data-id="' + escapeHtml(node.id) + '" href="' + escapeHtml(url || '#') + '" data-url="' + escapeHtml(url) + '"><i class="bi ' + icon + '" aria-hidden="true"></i><span class="node-text">' + escapeHtml(node.label) + '</span></a></div>';
        if (hasChildren) {
            html += '<ul class="tree-children">';
            node.children.forEach(function (child) { html += buildNodeHtml(child); });
            html += '</ul>';
        }
        return html + '</li>';
    }

    function saveTreeState() {
        var openIds = Array.prototype.map.call(document.querySelectorAll('.tree-children.show'), function (list) {
            var toggle = list.previousElementSibling && list.previousElementSibling.querySelector('[data-tree-toggle]');
            return toggle && toggle.dataset.id;
        }).filter(Boolean);
        sessionStorage.setItem('treeState', JSON.stringify(openIds));
    }

    function restoreTreeState() {
        var openIds;
        try { openIds = JSON.parse(sessionStorage.getItem('treeState') || '[]'); } catch (error) { openIds = []; }
        openIds.forEach(function (id) {
            var control = document.querySelector('[data-tree-toggle][data-id="' + CSS.escape(String(id)) + '"]');
            var childList = control && control.closest('.tree-row').nextElementSibling;
            if (childList) {
                childList.classList.add('show');
                control.setAttribute('aria-expanded', 'true');
                control.setAttribute('aria-label', control.getAttribute('aria-label').replace(/^展开/, '收起'));
            }
        });
    }

    function highlightTree() {
        document.querySelectorAll('.tree-link').forEach(function (control) {
            var url = control.dataset.url;
            if (url && (url === path || (url !== '/' && path.indexOf(url + '/') === 0))) control.classList.add('active');
        });
    }

    function showTreeError(message) {
        var loading = document.getElementById('treeLoading');
        var error = document.getElementById('treeError');
        if (loading) loading.hidden = true;
        if (error) { error.hidden = false; error.innerHTML = '<p>' + escapeHtml(message) + '</p><button type="button" class="btn btn-sm btn-outline-danger" data-tree-retry>重试</button>'; }
    }

    function loadTree() {
        var loading = document.getElementById('treeLoading');
        var error = document.getElementById('treeError');
        var container = document.getElementById('treeContainer');
        if (!container) return;
        if (loading) loading.hidden = false;
        if (error) error.hidden = true;
        fetch('/api/tree', { headers: { 'Accept': 'application/json' } })
            .then(function (response) { if (!response.ok) throw new Error('目录加载失败'); return response.json(); })
            .then(function (payload) {
                if (payload.code !== 0 || !Array.isArray(payload.data)) throw new Error(payload.msg || '目录加载失败');
                container.innerHTML = '<ul>' + payload.data.map(buildNodeHtml).join('') + '</ul>';
                restoreTreeState(); highlightTree();
                if (loading) loading.hidden = true;
            })
            .catch(function () { showTreeError('本地服务暂时无法读取目录，可重试'); });
    }

    document.addEventListener('click', function (event) {
        var retry = event.target.closest('[data-tree-retry]');
        if (retry) { loadTree(); return; }
        var control = event.target.closest('[data-tree-toggle]');
        if (!control) return;
        var childList = control.closest('.tree-row').nextElementSibling;
        if (childList && childList.classList.contains('tree-children')) {
            var isOpen = childList.classList.toggle('show');
            control.setAttribute('aria-expanded', String(isOpen));
            control.setAttribute('aria-label', control.getAttribute('aria-label').replace(isOpen ? /^展开/ : /^收起/, isOpen ? '收起' : '展开'));
            saveTreeState();
        }
    });

    window.loadTree = loadTree;
    window.toggleSidebar = function () { if (directoryToggle) directoryToggle.click(); };
    loadTree();
}());
