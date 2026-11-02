// Settings page: everything works without this file; it only adds drag, add-row, chips and YAML highlighting.
(() => {
  const form = document.querySelector("form.settings");
  if (form) rowsAndChips(form);
  const ta = document.getElementById("yaml");
  if (ta) highlight(ta);

  function rowsAndChips(form) {
    // Add a row from the <template>.
    document.querySelectorAll("[data-add]").forEach((btn) => {
      btn.hidden = false;
      const grid = document.querySelector(`[data-rows="${btn.dataset.add}"]`);
      btn.addEventListener("click", () => {
        const html = grid.querySelector("[data-tpl]").innerHTML.replaceAll("__N__", String(Date.now()));
        grid.querySelector("[data-list]").insertAdjacentHTML("beforeend", html);
        grid.querySelectorAll("[data-list] > [data-row]").forEach(draggable);
      });
    });
    // Remove and move with the buttons.
    document.addEventListener("click", (e) => {
      const b = e.target.closest("[data-move], [data-remove]");
      const row = b && b.closest("[data-row]");
      if (!row) return;
      e.preventDefault();
      if (b.hasAttribute("data-remove")) return row.remove();
      const other = b.dataset.move < 0 ? row.previousElementSibling : row.nextElementSibling;
      if (other) b.dataset.move < 0 ? row.parentNode.insertBefore(row, other) : row.parentNode.insertBefore(other, row);
    });
    // Drag a row by its grip: the order on screen is the order of the indexes sent.
    let dragged = null;
    function draggable(row) {
      if (row.dataset.drag) return;
      row.dataset.drag = "1";
      row.addEventListener("mousedown", (e) => { row.draggable = !e.target.closest("input, textarea, select, button, label"); });
      row.addEventListener("dragstart", (e) => { dragged = row; e.dataTransfer.effectAllowed = "move"; row.classList.add("is-dragging"); });
      row.addEventListener("dragend", () => { dragged = null; row.classList.remove("is-dragging"); row.draggable = false; });
      row.addEventListener("dragover", (e) => {
        if (!dragged || dragged === row || dragged.parentNode !== row.parentNode) return;
        e.preventDefault();
        const after = e.clientY > row.getBoundingClientRect().top + row.offsetHeight / 2;
        row.parentNode.insertBefore(dragged, after ? row.nextSibling : row);
      });
    }
    document.querySelectorAll("[data-list] > [data-row]").forEach(draggable);
    // Renumber on submit so the indexes follow the screen.
    form.addEventListener("submit", () => {
      document.querySelectorAll("[data-rows]").forEach((grid) => {
        const prefix = grid.dataset.rows;
        grid.querySelectorAll("[data-list] > [data-row]").forEach((row, n) => {
          row.querySelectorAll("[name]").forEach((el) => {
            el.name = el.name.replace(new RegExp(`^${prefix}-\\d+-`), `${prefix}-${n}-`);
          });
        });
      });
    });
    // Chips mirror the text typed in the feature fields.
    document.querySelectorAll("[data-chips]").forEach((box) => {
      const input = document.getElementById(box.dataset.chips);
      const paint = () => {
        box.replaceChildren(...input.value.split(/[\n,]/).map((s) => s.trim()).filter(Boolean).map((s) => {
          const chip = document.createElement("span");
          chip.className = "chip";
          chip.textContent = s;
          return chip;
        }));
      };
      input.addEventListener("input", paint);
    });
  }

  // YAML highlighting: the textarea stays the form field, transparent over a <pre> repainted on input.
  function highlight(ta) {
    const pre = ta.previousElementSibling.previousElementSibling, nums = document.querySelector(".yaml__lines");
    let bad = Number(nums.dataset.errorLine) || 0;  // the line the server could not parse
    const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
    const value = (v) => /^\s*(true|false|null|~|-?\d+(\.\d+)?)\s*$/.test(v) ? `<span class="y-lit">${esc(v)}</span>` : esc(v);
    const line = (l) => {
      const c = l.search(/(^|\s)#/);
      const code = c < 0 ? l : l.slice(0, c), note = c < 0 ? "" : `<span class="y-com">${esc(l.slice(c))}</span>`;
      const m = code.match(/^(\s*)(- )?([^\s:#][^:#]*?)(:)(\s.*|$)/);
      if (m) return `${m[1]}${m[2] ? '<span class="y-dash">-</span> ' : ""}<span class="y-key">${esc(m[3])}</span>:${value(m[5])}${note}`;
      const d = code.match(/^(\s*)- (.*)$/);
      return (d ? `${d[1]}<span class="y-dash">-</span> ${value(d[2])}` : esc(code)) + note;
    };
    const paint = () => {
      const lines = ta.value.split("\n");
      pre.innerHTML = lines.map((l, i) => (i + 1 === bad ? `<span class="y-bad">${line(l) || " "}</span>` : line(l))).join("\n") + "\n";
      nums.innerHTML = lines.map((_, i) => (i + 1 === bad ? `<span class="y-bad-n">${i + 1}</span>` : i + 1)).join("\n");
      ta.style.height = "auto";
      ta.style.height = `${Math.max(420, ta.scrollHeight)}px`;
    };
    ta.addEventListener("input", () => { bad = 0; paint(); });
    ta.addEventListener("scroll", () => { pre.scrollLeft = ta.scrollLeft; });
    paint();
  }
})();
