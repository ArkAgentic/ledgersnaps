(function (global) {
  const DURATION_MS = 460;
  const EASE = 'cubic-bezier(0.22, 1, 0.36, 1)';
  const activeByParent = new WeakMap();

  function reducedMotion() {
    return !!(global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function nextFrame() {
    return new Promise((resolve) => requestAnimationFrame(() => resolve()));
  }

  function stopActive(parent) {
    const running = parent && activeByParent.get(parent);
    if (!running) return;
    try { running.outAnim?.cancel(); } catch (_) {}
    try { running.inAnim?.cancel(); } catch (_) {}
    activeByParent.delete(parent);
  }

  async function swap({ outEl, inEl, hideClass = 'hidden', onBeforeIn } = {}) {
    if (!outEl || !inEl || outEl === inEl) return;

    if (reducedMotion()) {
      if (onBeforeIn) onBeforeIn();
      outEl.classList.add(hideClass);
      inEl.classList.remove(hideClass);
      return;
    }

    const parent = outEl.parentElement;
    if (!parent) {
      if (onBeforeIn) onBeforeIn();
      outEl.classList.add(hideClass);
      inEl.classList.remove(hideClass);
      return;
    }

    stopActive(parent);

    outEl.classList.remove(hideClass);
    inEl.classList.remove(hideClass);

    // Overlay both panes in the same grid cell during transition
    const prevParentDisplay = parent.style.display;
    const prevParentAlignItems = parent.style.alignItems;
    const prevParentMinHeight = parent.style.minHeight;
    const prevOutGridArea = outEl.style.gridArea;
    const prevInGridArea = inEl.style.gridArea;

    parent.style.display = 'grid';
    parent.style.alignItems = 'start';
    outEl.style.gridArea = '1 / 1';
    inEl.style.gridArea = '1 / 1';

    const h = Math.max(outEl.offsetHeight || 0, inEl.offsetHeight || 0);
    if (h > 0) parent.style.minHeight = `${h}px`;

    if (onBeforeIn) onBeforeIn();

    outEl.style.willChange = 'opacity';
    inEl.style.willChange = 'opacity';
    inEl.style.opacity = '0';

    await nextFrame();

    const outAnim = outEl.animate(
      [{ opacity: 1 }, { opacity: 0 }],
      { duration: DURATION_MS, easing: EASE, fill: 'forwards' }
    );
    const inAnim = inEl.animate(
      [{ opacity: 0 }, { opacity: 1 }],
      { duration: DURATION_MS, easing: EASE, fill: 'forwards' }
    );

    activeByParent.set(parent, { outAnim, inAnim });

    await Promise.allSettled([outAnim.finished, inAnim.finished]);

    outEl.classList.add(hideClass);

    outEl.style.opacity = '';
    inEl.style.opacity = '';
    outEl.style.willChange = '';
    inEl.style.willChange = '';

    outEl.style.gridArea = prevOutGridArea;
    inEl.style.gridArea = prevInGridArea;
    parent.style.display = prevParentDisplay;
    parent.style.alignItems = prevParentAlignItems;
    parent.style.minHeight = prevParentMinHeight;

    activeByParent.delete(parent);
  }

  global.FadeThrough = { swap };
})(window);
