// 科研管理系统 - UI 增强 JS
(function() {
    'use strict';
    
    // ===== Toast 通知 =====
    window.showToast = function(message, type = 'info') {
        const container = document.querySelector('.toast-container') || createToastContainer();
        const icons = {
            success: 'bi-check-circle-fill',
            error: 'bi-exclamation-circle-fill',
            warning: 'bi-exclamation-triangle-fill',
            info: 'bi-info-circle-fill'
        };
        
        const toast = document.createElement('div');
        toast.className = `toast align-items-center text-white bg-${type === 'error' ? 'danger' : type} show`;
        toast.setAttribute('role', 'alert');
        toast.setAttribute('aria-live', 'assertive');
        toast.innerHTML = `
            <i class="bi ${icons[type]} me-2"></i>
            <div class="toast-body flex-grow-1">${message}</div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
        `;
        container.appendChild(toast);
        
        const bsToast = new bootstrap.Toast(toast);
        bsToast.show();
        
        toast.addEventListener('hidden.bs.toast', () => toast.remove());
        return bsToast;
    };
    
    function createToastContainer() {
        const container = document.createElement('div');
        container.className = 'toast-container';
        document.body.appendChild(container);
        return container;
    }
    
    // ===== 响应式边栏 =====
    const sidebar = document.getElementById('sidebar');
    const resizer = document.getElementById('resizer');
    
    const usesAppShell = Boolean(document.getElementById('appShell'));

    if (sidebar && !usesAppShell && window.innerWidth <= 768) {
        // 移动端：边栏默认隐藏
        sidebar.classList.remove('show');
    }
    
    // 汉堡菜单切换
    window.toggleSidebar = function() {
        const sidebar = document.getElementById('sidebar');
        const overlay = document.getElementById('sidebarOverlay');
        
        if (window.innerWidth <= 768) {
            if (!overlay) {
                const overlay = document.createElement('div');
                overlay.id = 'sidebarOverlay';
                overlay.className = 'sidebar-overlay';
                overlay.onclick = window.toggleSidebar;
                document.body.appendChild(overlay);
            }
            sidebar.classList.toggle('show');
            document.getElementById('sidebarOverlay').classList.toggle('show');
        }
    };
    
    // 边栏拖拽调整宽度
    if (resizer && sidebar && !usesAppShell) {
        let isResizing = false;
        
        resizer.addEventListener('mousedown', (e) => {
            isResizing = true;
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
        });
        
        document.addEventListener('mousemove', (e) => {
            if (!isResizing) return;
            const width = e.clientX - sidebar.getBoundingClientRect().left;
            sidebar.style.width = Math.max(150, Math.min(400, width)) + 'px';
        });
        
        document.addEventListener('mouseup', () => {
            isResizing = false;
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        });
    }
    
    // ===== 空状态 =====
    window.renderEmptyState = function(container, options = {}) {
        const {
            icon = 'bi-inbox',
            title = '暂无数据',
            description = '没有找到相关内容',
            action = null
        } = options;
        
        const html = `
            <div class="empty-state">
                <i class="bi ${icon}"></i>
                <h5>${title}</h5>
                <p class="text-muted">${description}</p>
                ${action ? `<button class="btn btn-primary mt-3" onclick="${action}">${action}</button>` : ''}
            </div>
        `;
        
        if (typeof container === 'string') {
            container = document.querySelector(container);
        }
        container.innerHTML = html;
    };
    
    // ===== 骨架屏 =====
    window.renderSkeleton = function(container, rows = 5) {
        const html = Array(rows).fill(0).map(() => `
            <div class="skeleton mb-3" style="height: 50px;"></div>
        `).join('');
        
        if (typeof container === 'string') {
            container = document.querySelector(container);
        }
        container.innerHTML = html;
    };
    
    // ===== 表单实时校验 =====
    document.querySelectorAll('.form-control, .form-select').forEach(input => {
        input.addEventListener('blur', function() {
            if (this.checkValidity()) {
                this.classList.remove('is-invalid');
                this.classList.add('is-valid');
            } else {
                this.classList.add('is-invalid');
            }
        });
        
        input.addEventListener('input', function() {
            if (this.classList.contains('is-invalid') && this.checkValidity()) {
                this.classList.remove('is-invalid');
                this.classList.add('is-valid');
            }
        });
    });
    
    // ===== 确认对话框 =====
    window.confirmAction = function(message, onConfirm) {
        if (confirm(message)) {
            onConfirm();
        }
    };
    
    // ===== 页面加载完成 =====
    document.addEventListener('DOMContentLoaded', () => {
        // 初始化工具提示
        const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
        tooltipTriggerList.map(tooltipTriggerEl => new bootstrap.Tooltip(tooltipTriggerEl));
        
        // 初始化弹出框
        const popoverTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="popover"]'));
        popoverTriggerList.map(popoverTriggerEl => new bootstrap.Popover(popoverTriggerEl));
    });
    
})();
