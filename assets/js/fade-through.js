(function (global) {
  const ENTER_DURATION_S = 0.72;
  const EXIT_DURATION_S = 0.42;
  const ENTER_EASE = 'cubic-bezier(0.2, 0, 0, 1)';
  const EXIT_EASE = 'cubic-bezier(0.4, 0, 1, 1)';

  function reducedMotion() {
    return !!(global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function apply(el, cfg) {
    if (!el) return;
    Object.assign(el.style, cfg);
  }

  async function swap({ outEl, inEl, hideClass = 'hidden', onBeforeIn } = {}) {
    if (!outEl || !inEl) return;

    if (reducedMotion()) {
      outEl.classList.add(hideClass);
      inEl.classList.remove(hideClass);
      if (onBeforeIn) onBeforeIn();
      return;
    }

    outEl.classList.remove(hideClass);
    inEl.classList.remove(hideClass);

    // Prepare in element initial state
    apply(inEl, {
      opacity: '0',
      transform: 'translateY(6px) scale(0.99)',
      filter: 'blur(2px)',
      transition: 'none',
    });

    // Animate out element
    apply(outEl, {
      transition: `opacity ${EXIT_DURATION_S}s ${EXIT_EASE}, transform ${EXIT_DURATION_S}s ${EXIT_EASE}, filter ${EXIT_DURATION_S}s ${EXIT_EASE}`,
      opacity: '0',
      transform: 'translateY(-4px) scale(1)',
      filter: 'blur(0px)',
    });

    await new Promise((r) => setTimeout(r, EXIT_DURATION_S * 1000));

    outEl.classList.add(hideClass);
    if (onBeforeIn) onBeforeIn();

    // Animate in
    apply(inEl, {
      transition: `opacity ${ENTER_DURATION_S}s ${ENTER_EASE}, transform ${ENTER_DURATION_S}s ${ENTER_EASE}, filter ${ENTER_DURATION_S}s ${ENTER_EASE}`,
      opacity: '1',
      transform: 'translateY(0) scale(1)',
      filter: 'blur(0px)',
    });

    await new Promise((r) => setTimeout(r, ENTER_DURATION_S * 1000));

    // cleanup inline style so layout remains controllable by classes
    apply(inEl, { transition: '', opacity: '', transform: '', filter: '' });
    apply(outEl, { transition: '', opacity: '', transform: '', filter: '' });
  }

  global.FadeThrough = { swap };
})(window);
