(() => {
  const STATUS = {
    done: { label: '已完成', stroke: '#5c8e75', fill: '#edf6f1', text: '#2f654f', dash: null },
    active: { label: '进行中', stroke: '#238b7b', fill: '#e7f5f2', text: '#176c61', dash: [7, 5] },
    pending: { label: '未开始', stroke: '#aab4b1', fill: '#f4f6f5', text: '#5f6b68', dash: [7, 5] },
    risk: { label: '有风险', stroke: '#d28a2f', fill: '#fff5e5', text: '#875a19', dash: [7, 5] },
    blocked: { label: '受阻', stroke: '#c9514a', fill: '#fdeeed', text: '#963b36', dash: [2, 5] },
    paused: { label: '已暂停', stroke: '#8a6b2f', fill: '#faf3e4', text: '#6f5423', dash: [10, 5] },
    terminated: { label: '已终止', stroke: '#765d5b', fill: '#f3eeee', text: '#604846', dash: [2, 5] },
  };

  let graph = null;
  let tree = null;
  let selectedId = null;
  let mounted = false;
  let motionEnabled = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function container() { return document.querySelector('#researchPathCanvas'); }
  function nodeRecord(model) { return model && (model.data || model) || {}; }
  function* flatten(node) {
    if (!node) return;
    yield node;
    for (const child of node.children || []) yield* flatten(child);
  }
  function escapeText(value, fallback = '未记录') {
    return typeof value === 'string' && value.trim() ? value : fallback;
  }

  function showError(message) {
    const target = container();
    if (!target) return;
    target.replaceChildren();
    const wrap = document.createElement('div');
    wrap.className = 'path-error';
    const inner = document.createElement('div');
    const text = document.createElement('p');
    text.textContent = message;
    const retry = document.createElement('button');
    retry.className = 'btn btn-sm btn-outline-secondary';
    retry.type = 'button';
    retry.textContent = '重新加载研究路径';
    retry.addEventListener('click', () => { mounted = false; mount(); });
    inner.append(text, retry);
    wrap.append(inner);
    target.append(wrap);
  }

  function buildData() {
    const data = window.G6.treeToGraphData(tree);
    const statusById = new Map(data.nodes.map(node => [node.id, nodeRecord(node).status]));
    data.edges = data.edges.map(edge => ({...edge, data: {status: statusById.get(edge.target) || 'pending'}}));
    return data;
  }

  function registerFlowEdge() {
    if (window.__researchPathEdgeRegistered) return;
    class ResearchFlowEdge extends window.G6.CubicHorizontal {
      onCreate() {
        if (!this.shapeMap || !this.shapeMap.key) return;
        this.shapeMap.key.animate([{lineDashOffset: 18}, {lineDashOffset: 0}], {duration: 900, iterations: Infinity});
      }
    }
    window.G6.register(window.G6.ExtensionCategory.EDGE, 'research-flow', ResearchFlowEdge);
    window.__researchPathEdgeRegistered = true;
  }

  function updateInspector(id) {
    const item = [...flatten(tree)].find(node => node.id === id);
    if (!item) return;
    const record = item.data || {};
    const inspector = document.querySelector('#pathInspector');
    if (!inspector) return;
    const palette = STATUS[record.status] || STATUS.pending;
    const badge = inspector.querySelector('.path-inspector-status');
    badge.className = `path-inspector-status ${record.status || 'pending'}`;
    badge.textContent = palette.label;
    inspector.querySelector('h3').textContent = escapeText(record.title);
    inspector.querySelector(':scope > p').textContent = escapeText(record.summary);
    const values = inspector.querySelectorAll('dd');
    values[0].textContent = escapeText(record.owner);
    values[1].textContent = escapeText(record.period);
    values[2].textContent = escapeText(record.source);
    values[3].textContent = escapeText(record.issues, '无');
    inspector.querySelector('footer p').textContent = escapeText(record.next);
  }

  async function selectNode(id) {
    if (!graph || !tree || ![...flatten(tree)].some(node => node.id === id)) return false;
    selectedId = id;
    const states = Object.fromEntries([...flatten(tree)].map(node => [node.id, node.id === id ? ['selected'] : []]));
    await graph.setElementState(states);
    updateInspector(id);
    return true;
  }

  function updateMotionButton() {
    const button = document.querySelector('#togglePathMotion');
    if (!button) return;
    button.setAttribute('aria-pressed', String(!motionEnabled));
    const icon = button.querySelector('i');
    icon.className = `bi ${motionEnabled ? 'bi-pause' : 'bi-play'}`;
    button.querySelector('span').textContent = motionEnabled ? '暂停动态线' : '播放动态线';
  }

  async function render() {
    const target = container();
    if (!target || target.clientWidth === 0) return false;
    if (window.__G6_LOAD_FAILED__ || !window.G6) throw new Error('本地图形组件加载失败');
    registerFlowEdge();
    if (graph) graph.destroy();
    target.replaceChildren();
    graph = new window.G6.Graph({
      container: target,
      width: target.clientWidth,
      height: target.clientHeight,
      autoFit: {type: 'view', options: {padding: [34, 42, 54, 42]}},
      data: buildData(),
      node: {
        type: 'rect',
        style: model => {
          const record = nodeRecord(model);
          const palette = STATUS[record.status] || STATUS.pending;
          const root = model.id === tree.id;
          return {
            size: root ? [210, 58] : [164, 46], radius: root ? 10 : 8,
            fill: root ? '#174f48' : palette.fill, stroke: root ? '#174f48' : palette.stroke,
            lineWidth: record.status === 'active' ? 2.4 : 1.5,
            shadowColor: root ? 'rgba(16,63,55,.18)' : 'rgba(23,79,72,.08)', shadowBlur: root ? 12 : 5,
            labelText: record.title, labelPlacement: 'center', labelFill: root ? '#fff' : palette.text,
            labelFontSize: root ? 13 : 11, labelFontWeight: root ? 700 : 650,
            labelMaxWidth: root ? 184 : 140, labelWordWrap: true,
            ports: [{placement: 'right'}, {placement: 'left'}], cursor: 'pointer',
          };
        },
        state: {selected: {stroke: '#238b7b', lineWidth: 3, halo: true, haloLineWidth: 7, haloStroke: '#75b8ad'}},
        animation: false,
      },
      edge: {
        type: model => motionEnabled && nodeRecord(model).status === 'active' ? 'research-flow' : 'cubic-horizontal',
        style: model => {
          const palette = STATUS[nodeRecord(model).status] || STATUS.pending;
          return {stroke: palette.stroke, lineWidth: nodeRecord(model).status === 'active' ? 2.4 : 1.4, lineDash: palette.dash, opacity: nodeRecord(model).status === 'pending' ? .62 : .92};
        },
        animation: false,
      },
      layout: {type: 'mindmap', direction: 'LR', getHeight: () => 46, getWidth: model => model.id === tree.id ? 210 : 164, getVGap: () => 10, getHGap: () => 46},
      behaviors: ['drag-canvas', 'zoom-canvas'],
    });
    await graph.render();
    selectedId = selectedId && [...flatten(tree)].some(node => node.id === selectedId) ? selectedId : tree.id;
    await graph.setElementState({[selectedId]: ['selected']});
    updateInspector(selectedId);
    graph.on('node:click', event => selectNode(event.target.id));
    updateMotionButton();
    return true;
  }

  async function loadData() {
    const page = document.querySelector('.lifecycle-page');
    const response = await fetch(`/api/projects/${page.dataset.projectId}/research-path`, {headers: {'Accept': 'application/json'}});
    if (!response.ok) throw new Error('研究路径数据暂时无法读取');
    const data = await response.json();
    if (!data.readOnly || !data.tree) throw new Error('研究路径数据格式无效');
    tree = data.tree;
  }

  async function mount() {
    try {
      if (!tree) await loadData();
      if (mounted && graph) { graph.resize(); await graph.fitView({padding: [34, 42, 54, 42]}); return true; }
      mounted = await render();
      return mounted;
    } catch (error) {
      mounted = false;
      showError(error.message || '研究路径加载失败，不影响其他项目功能');
      return false;
    }
  }

  async function toggleMotion() { motionEnabled = !motionEnabled; mounted = false; await mount(); }
  async function fit() { if (graph) await graph.fitView({padding: [34, 42, 54, 42]}); }

  document.addEventListener('DOMContentLoaded', () => {
    const motion = document.querySelector('#togglePathMotion');
    const fitButton = document.querySelector('#fitResearchPath');
    if (motion) motion.addEventListener('click', toggleMotion);
    if (fitButton) fitButton.addEventListener('click', fit);
    updateMotionButton();
  });
  window.ResearchPath = {
    mount, toggleMotion, fit, selectNode,
    getGraph: () => graph,
    getTree: () => tree,
    getMotionEnabled: () => motionEnabled,
  };
})();
