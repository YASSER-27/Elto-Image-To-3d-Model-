(function () {
  const $ = (id) => document.getElementById(id);

  const dropzone   = $('dropzone');
  const fileInput  = $('fileInput');
  const btnImport  = $('btnImport');
  const btnEdit    = $('btnEdit');
  const btnGenerate = $('btnGenerate');
  const spinner    = $('spinner');
  const progressText = $('progressText');
  const statusLabel  = $('statusLabel');
  const deviceLabel  = $('deviceLabel');
  const viewerFrame  = $('viewerFrame');
  const btnSaveObj  = $('btnSaveObj');
  const btnSaveGlb  = $('btnSaveGlb');
  const btnSaveGltf = $('btnSaveGltf');
  const btnOpenGlb  = $('btnOpenGlb');
  const btnOpenGltf = $('btnOpenGltf');
  const openMeshInput = $('openMeshInput');

  let engineReady = false;
  let currentImageId = null;
  let currentFile = null;      // last raw File/Blob, used for the crop tool
  let currentJobId = null;
  let generating = false;

  let qualityIdx = 1;          // Balanced by default
  let decimatePct = 100;       // Off
  let smoothVal = 0;           // Off

  function updateGenerateEnabled() {
    btnGenerate.disabled = !(engineReady && currentImageId && !generating);
  }

  // ── engine status polling ────────────────────────────────────────────
  function pollStatus() {
    fetch('/api/status').then(r => r.json()).then(s => {
      if (s.error) {
        const firstLine = (s.error.trim().split('\n').pop() || 'unknown error');
        progressText.textContent = 'Engine failed to load';
        statusLabel.textContent = 'Error: ' + firstLine;
        return; // stop polling, nothing more will change
      }
      if (!s.ready) {
        progressText.textContent = s.message + '  (' + s.progress + '%)';
        setTimeout(pollStatus, 500);
      } else {
        engineReady = true;
        progressText.textContent = 'Ready';
        statusLabel.textContent = currentImageId ? 'Image ready - click Generate' : 'Import an image to begin';
        deviceLabel.textContent = 'Device: ' + s.device;
        updateGenerateEnabled();
      }
    }).catch(() => setTimeout(pollStatus, 1000));
  }
  pollStatus();

  // ── import (file picker / drag / paste) ──────────────────────────────
  function importBlob(blob, filename) {
    currentFile = blob;
    const fd = new FormData();
    fd.append('file', blob, filename || 'image.png');
    fetch('/api/import', { method: 'POST', body: fd })
      .then(r => r.json())
      .then(res => {
        if (!res.ok) { statusLabel.textContent = 'Import failed'; return; }
        currentImageId = res.image_id;
        const url = URL.createObjectURL(blob);
        dropzone.style.backgroundImage = `url(${url})`;
        dropzone.textContent = '';
        btnEdit.style.display = 'inline-block';
        btnSaveObj.style.pointerEvents = 'none'; btnSaveObj.style.opacity = .5;
        btnSaveGlb.style.pointerEvents = 'none'; btnSaveGlb.style.opacity = .5;
        btnSaveGltf.style.pointerEvents = 'none'; btnSaveGltf.style.opacity = .5;
        viewerFrame.src = '/api/viewer/placeholder';
        statusLabel.textContent = 'Image ready - click Generate';
        updateGenerateEnabled();
      })
      .catch(() => { statusLabel.textContent = 'Import failed'; });
  }

  dropzone.addEventListener('click', () => fileInput.click());
  btnImport.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) importBlob(fileInput.files[0], fileInput.files[0].name);
  });

  ['dragenter', 'dragover'].forEach(ev => dropzone.addEventListener(ev, (e) => {
    e.preventDefault(); dropzone.classList.add('drag');
  }));
  ['dragleave', 'drop'].forEach(ev => dropzone.addEventListener(ev, (e) => {
    e.preventDefault(); dropzone.classList.remove('drag');
  }));
  dropzone.addEventListener('drop', (e) => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) importBlob(f, f.name);
  });

  document.addEventListener('paste', (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.indexOf('image') !== -1) {
        importBlob(item.getAsFile(), 'pasted.png');
        break;
      }
    }
  });

  // ── crop ──────────────────────────────────────────────────────────────
  btnEdit.addEventListener('click', () => {
    if (!currentFile) return;
    window.ElToCrop.open(currentFile, (croppedBlob) => {
      importBlob(croppedBlob, 'cropped.png');
    });
  });

  // ── quality / decimate / smooth radio rows ────────────────────────────
  function wireRadioRow(rowId, onSelect) {
    const row = document.getElementById(rowId);
    row.querySelectorAll('.radio-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        row.querySelectorAll('.radio-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        onSelect(btn);
      });
    });
  }
  wireRadioRow('qualityRow', (btn) => { qualityIdx = parseInt(btn.dataset.idx, 10); });
  wireRadioRow('decimateRow', (btn) => { decimatePct = parseFloat(btn.dataset.val); });
  wireRadioRow('smoothRow', (btn) => { smoothVal = parseFloat(btn.dataset.val); });

  // ── generate ─────────────────────────────────────────────────────────
  function setBusy(isBusy) {
    generating = isBusy;
    btnImport.disabled = isBusy;
    btnEdit.disabled = isBusy;
    spinner.style.display = isBusy ? 'inline-block' : 'none';
    updateGenerateEnabled();
  }

  btnGenerate.addEventListener('click', () => {
    if (!currentImageId || !engineReady || generating) return;
    setBusy(true);
    statusLabel.textContent = 'Generating...';
    progressText.textContent = 'Starting...';
    [btnSaveObj, btnSaveGlb, btnSaveGltf].forEach(b => { b.style.pointerEvents = 'none'; b.style.opacity = .5; });

    fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        image_id: currentImageId,
        quality_idx: qualityIdx,
        decimate_pct: decimatePct,
        smooth_iter: smoothVal,
      }),
    }).then(r => r.json()).then(res => {
      if (!res.ok) {
        setBusy(false);
        statusLabel.textContent = 'Error: ' + (res.error || 'could not start');
        return;
      }
      currentJobId = res.job_id;
      pollProgress(currentJobId);
    }).catch(() => { setBusy(false); statusLabel.textContent = 'Error: request failed'; });
  });

  function pollProgress(jobId) {
    fetch('/api/progress/' + jobId).then(r => r.json()).then(res => {
      if (!res.ok) { setBusy(false); statusLabel.textContent = 'Error: lost job'; return; }
      if (res.error) {
        setBusy(false);
        const firstLine = res.error.trim().split('\n').pop() || 'unknown error';
        progressText.textContent = 'Generation failed';
        statusLabel.textContent = 'Error: ' + firstLine;
        return;
      }
      progressText.textContent = res.message + '  (' + res.progress + '%)';
      if (res.done) {
        setBusy(false);
        progressText.textContent = 'Done \u2713';
        statusLabel.textContent = 'Done - drag to orbit, scroll to zoom';
        viewerFrame.src = '/api/viewer/' + jobId;
        if (res.result.obj)  { btnSaveObj.href  = '/api/download/' + jobId + '/obj';  btnSaveObj.style.pointerEvents = 'auto';  btnSaveObj.style.opacity = 1; }
        if (res.result.glb)  { btnSaveGlb.href  = '/api/download/' + jobId + '/glb';  btnSaveGlb.style.pointerEvents = 'auto';  btnSaveGlb.style.opacity = 1; }
        if (res.result.gltf) { btnSaveGltf.href = '/api/download/' + jobId + '/gltf'; btnSaveGltf.style.pointerEvents = 'auto'; btnSaveGltf.style.opacity = 1; }
      } else {
        setTimeout(() => pollProgress(jobId), 500);
      }
    }).catch(() => setTimeout(() => pollProgress(jobId), 1000));
  }

  // ── open .glb / .gltf directly in the viewer ──────────────────────────
  function openMesh(accept) {
    openMeshInput.accept = accept;
    openMeshInput.onchange = () => {
      const f = openMeshInput.files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append('file', f, f.name);
      statusLabel.textContent = 'Opening ' + f.name + '...';
      fetch('/api/open_mesh', { method: 'POST', body: fd })
        .then(r => r.json())
        .then(res => {
          if (!res.ok) { statusLabel.textContent = 'Could not open file'; return; }
          viewerFrame.src = '/api/view_uploaded/' + res.mesh_id;
          statusLabel.textContent = 'Viewing: ' + res.label;
        })
        .catch(() => { statusLabel.textContent = 'Could not open file'; });
    };
    openMeshInput.click();
  }
  btnOpenGlb.addEventListener('click', () => openMesh('.glb'));
  btnOpenGltf.addEventListener('click', () => openMesh('.gltf'));
})();
