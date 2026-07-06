// Shared front-end helpers.

function rfToast(message, href) {
  const wrap = document.getElementById('toastWrap');
  if (!wrap) return;
  const el = document.createElement('div');
  el.className = 'rf-toast';
  el.innerHTML = message + (href ? ` <a href="${href}">view impact &rsaquo;</a>` : '');
  wrap.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 400); }, 6000);
}

function fmtMoney(v) {
  return (Math.round(v)).toLocaleString('en-US');
}

// Sidebar toggle (mobile)
document.addEventListener('click', (e) => {
  if (e.target.closest('#sidebarToggle')) {
    document.querySelector('.sidebar').classList.toggle('open');
  }
});

// Inline price editing on the ingredients table.
document.addEventListener('dblclick', (e) => {
  const cell = e.target.closest('.editable-price');
  if (!cell || cell.querySelector('input')) return;
  startInlineEdit(cell);
});
document.addEventListener('click', (e) => {
  const cell = e.target.closest('.editable-price');
  if (cell && !cell.querySelector('input')) startInlineEdit(cell);
});

function startInlineEdit(cell) {
  const id = cell.dataset.id;
  const current = parseFloat(cell.dataset.value || '0');
  const input = document.createElement('input');
  input.type = 'number';
  input.step = '0.01';
  input.value = current;
  input.className = 'form-control form-control-sm';
  input.style.width = '120px';
  cell.textContent = '';
  cell.appendChild(input);
  input.focus();
  input.select();

  const commit = () => {
    const val = parseFloat(input.value);
    if (isNaN(val) || val === current) { cell.textContent = fmtMoney(current); return; }
    fetch(`/costing/ingredients/${id}/price`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_cost: val })
    }).then(r => r.json()).then(d => {
      if (d.ok) {
        cell.dataset.value = d.total_cost;
        cell.textContent = fmtMoney(d.total_cost);
        const totalCell = document.querySelector(`.total-cost[data-id="${id}"]`);
        if (totalCell) totalCell.textContent = fmtMoney(d.total_cost);
        rfToast(`<strong>${d.affected}</strong> product(s) affected.`,
                `/costing/what-if/?ingredient=${id}`);
      } else {
        cell.textContent = fmtMoney(current);
        alert(d.error || 'Update failed');
      }
    });
  };
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
    if (ev.key === 'Escape') { cell.textContent = fmtMoney(current); }
  });
  input.addEventListener('blur', commit);
}
