(() => {
    const app = document.querySelector('[data-intro-app]');
    if (!app) return;

    const tabs = [...app.querySelectorAll('[data-intro-target]')];
    const panels = [...app.querySelectorAll('[data-intro-section]')];
    const progress = app.querySelector('[data-intro-progress]');
    const previous = app.querySelector('[data-intro-prev]');
    const next = app.querySelector('[data-intro-next]');

    const activate = (id, updateHash = true) => {
        const index = tabs.findIndex((tab) => tab.dataset.introTarget === id);
        if (index < 0) return;
        tabs.forEach((tab, itemIndex) => {
            const active = itemIndex === index;
            tab.classList.toggle('is-active', active);
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
        });
        panels.forEach((panel) => {
            const active = panel.dataset.introSection === id;
            panel.hidden = !active;
            panel.classList.toggle('is-active', active);
        });
        progress.textContent = `${String(index + 1).padStart(2, '0')} / ${String(tabs.length).padStart(2, '0')}`;
        previous.disabled = index === 0;
        next.disabled = index === tabs.length - 1;
        if (updateHash) history.replaceState(null, '', `#${id}`);
    };

    tabs.forEach((tab) => tab.addEventListener('click', () => activate(tab.dataset.introTarget)));
    previous.addEventListener('click', () => {
        const index = tabs.findIndex((tab) => tab.classList.contains('is-active'));
        if (index > 0) activate(tabs[index - 1].dataset.introTarget);
    });
    next.addEventListener('click', () => {
        const index = tabs.findIndex((tab) => tab.classList.contains('is-active'));
        if (index < tabs.length - 1) activate(tabs[index + 1].dataset.introTarget);
    });
    app.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        const index = tabs.findIndex((tab) => tab.classList.contains('is-active'));
        const target = event.key === 'ArrowRight' ? tabs[index + 1] : tabs[index - 1];
        if (target) { activate(target.dataset.introTarget); target.focus(); }
    });

    const initial = location.hash.slice(1);
    activate(tabs.some((tab) => tab.dataset.introTarget === initial) ? initial : 'overview', false);
})();
