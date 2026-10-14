(() => {
  const key = 'ecourse.appearance';
  const allowed = ['system', 'light', 'dark'];
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = 'system';
  try { const saved = localStorage.getItem(key); if (allowed.includes(saved)) preference = saved; } catch {}
  function apply() {
    document.documentElement.dataset.theme = preference === 'system' ? (system.matches ? 'dark' : 'light') : preference;
    document.querySelectorAll('[name="appearance"]').forEach(input => { input.checked = input.value === preference; });
  }
  apply();
  system.addEventListener('change', apply);
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) { preference = allowed.includes(event.newValue) ? event.newValue : 'system'; apply(); }
  });
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.querySelectorAll('[name="appearance"]').forEach(input => input.addEventListener('change', () => {
      if (!allowed.includes(input.value)) return;
      preference = input.value;
      apply();
      const status = document.getElementById('appearance-status');
      const english = document.documentElement.lang === 'en';
      try { localStorage.setItem(key, preference); if (status) status.textContent = english ? 'Saved' : '已儲存'; }
      catch { if (status) status.textContent = english ? 'Applied, but could not be saved.' : '已套用，但無法儲存。'; }
    }));
  });
})();
