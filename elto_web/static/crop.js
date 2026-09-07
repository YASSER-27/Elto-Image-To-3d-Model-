// Minimal drag-to-select crop tool. Exposes window.ElToCrop.open(file, onApply).
(function () {
  const modal = document.getElementById('cropModal');
  const canvas = document.getElementById('cropCanvas');
  const ctx = canvas.getContext('2d');
  const btnApply = document.getElementById('btnCropApply');
  const btnCancel = document.getElementById('btnCropCancel');

  let img = null;          // full-resolution source image
  let scale = 1;           // display scale factor (canvas px per source px)
  let sel = null;          // {x,y,w,h} in canvas/display coordinates
  let dragging = false;
  let dragStart = null;
  let onApplyCb = null;

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    if (sel) {
      ctx.fillStyle = 'rgba(0,0,0,0.5)';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.clearRect(sel.x, sel.y, sel.w, sel.h);
      ctx.drawImage(
        img,
        sel.x / scale, sel.y / scale, sel.w / scale, sel.h / scale,
        sel.x, sel.y, sel.w, sel.h
      );
      ctx.strokeStyle = '#38bdf8';
      ctx.lineWidth = 2;
      ctx.strokeRect(sel.x, sel.y, sel.w, sel.h);
    }
  }

  function normRect(a, b) {
    return {
      x: Math.min(a.x, b.x), y: Math.min(a.y, b.y),
      w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y),
    };
  }

  canvas.addEventListener('mousedown', (e) => {
    const r = canvas.getBoundingClientRect();
    dragStart = { x: e.clientX - r.left, y: e.clientY - r.top };
    dragging = true;
  });
  canvas.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const r = canvas.getBoundingClientRect();
    const cur = { x: e.clientX - r.left, y: e.clientY - r.top };
    sel = normRect(dragStart, cur);
    draw();
  });
  window.addEventListener('mouseup', () => { dragging = false; });

  btnCancel.addEventListener('click', () => { modal.style.display = 'none'; });

  btnApply.addEventListener('click', () => {
    if (!sel || sel.w < 4 || sel.h < 4) { modal.style.display = 'none'; return; }
    const sx = sel.x / scale, sy = sel.y / scale, sw = sel.w / scale, sh = sel.h / scale;
    const out = document.createElement('canvas');
    out.width = Math.max(1, Math.round(sw));
    out.height = Math.max(1, Math.round(sh));
    out.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, out.width, out.height);
    out.toBlob((blob) => {
      modal.style.display = 'none';
      if (onApplyCb) onApplyCb(blob);
    }, 'image/png');
  });

  window.ElToCrop = {
    open(fileOrBlob, onApply) {
      const url = URL.createObjectURL(fileOrBlob);
      const image = new Image();
      image.onload = () => {
        img = image;
        const maxW = Math.min(700, window.innerWidth * 0.75);
        const maxH = Math.min(520, window.innerHeight * 0.65);
        scale = Math.min(maxW / img.width, maxH / img.height, 1);
        canvas.width = Math.round(img.width * scale);
        canvas.height = Math.round(img.height * scale);
        sel = null;
        onApplyCb = onApply;
        draw();
        modal.style.display = 'flex';
        URL.revokeObjectURL(url);
      };
      image.src = url;
    },
  };
})();
