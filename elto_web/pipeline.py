"""
ElTo - generation engine.

This is the original EngineThread / _process_image logic from the desktop
app, ported from Qt signals to plain threading + a shared state dict that
the Flask routes poll. The actual model-loading and mesh-generation code is
unchanged - only the communication layer (Qt signals -> dict + lock) differs.
"""

import os
import sys
import queue
import shutil
import tempfile
import threading
import traceback


class Engine:
    """Loads the TSR model once at startup, then processes generate jobs
    one at a time from a queue (matches the original single-worker design -
    the model/GPU is not thread-safe for concurrent inference)."""

    def __init__(self, model_dir: str):
        self.model_dir = model_dir
        self._model = None
        self._session = None
        self.device = "cpu"

        self.ready = False
        self.load_error = None
        self.load_progress = (0, "Starting...")

        self.jobs = {}          # job_id -> dict(progress=(pct,msg), result, error, preview)
        self._job_seq = 0
        self._lock = threading.Lock()
        self._queue = queue.Queue()

        threading.Thread(target=self._load_then_serve, daemon=True).start()

    # ── phase 1: load model (mirrors EngineThread.run, phase 1) ─────────
    def _load_then_serve(self):
        import warnings
        warnings.filterwarnings("ignore")

        try:
            import torch
            import rembg
            from elto.system import TSR

            self.load_progress = (10, "Initializing PyTorch...")
            self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
            if self.device == "cpu":
                self.load_progress = (
                    10,
                    "\u26a0\ufe0f No CUDA GPU detected \u2014 processing will be very slow on CPU",
                )

            if not os.path.isdir(self.model_dir):
                raise FileNotFoundError(
                    f"Model directory not found: {self.model_dir}"
                )

            self.load_progress = (35, "Loading 3D model weights...")
            self._model = TSR.from_pretrained(
                self.model_dir,
                config_name="config.yaml",
                weight_name="model.ckpt",
            )
            self._model.renderer.set_chunk_size(8192)

            self.load_progress = (70, f"Moving model to {self.device}...")
            self._model.to(self.device)

            self.load_progress = (88, "Loading rembg AI session...")
            self._session = rembg.new_session()

            self.load_progress = (100, "Ready")
            self.ready = True

        except Exception:
            self.load_error = traceback.format_exc()
            return

        # ── phase 2: serve queued generate jobs, one at a time ──────────
        while True:
            job_id, image_path, resolution, decimate_ratio, smooth_iter = self._queue.get()
            try:
                paths = self._process_image(
                    job_id, image_path, resolution, decimate_ratio, smooth_iter
                )
                self.jobs[job_id]["result"] = paths
                self.jobs[job_id]["progress"] = (100, "Done")
            except Exception:
                self.jobs[job_id]["error"] = traceback.format_exc()

    # ── public API (called from Flask routes) ───────────────────────────
    def submit_job(self, image_path, resolution, decimate_ratio, smooth_iter):
        with self._lock:
            self._job_seq += 1
            job_id = str(self._job_seq)
        self.jobs[job_id] = {
            "progress": (0, "Queued..."),
            "result": None,
            "error": None,
            "preview": None,
        }
        self._queue.put((job_id, image_path, resolution, decimate_ratio, smooth_iter))
        return job_id

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    # ── image processing (identical to the original _process_image) ────
    def _process_image(self, job_id, image_path, resolution,
                        decimate_ratio=1.0, smooth_iter=0):
        import torch
        import numpy as np
        import trimesh as _trimesh
        from PIL import Image
        from elto.utils import remove_background, resize_foreground

        def _progress(val, msg):
            self.jobs[job_id]["progress"] = (val, msg)

        _progress(8, "Loading image...")
        image = Image.open(image_path).convert("RGB")

        _progress(15, "Resizing image...")
        image = image.resize((512, 512), Image.LANCZOS)

        _progress(18, "Removing background...")
        image = remove_background(image, self._session)

        _progress(28, "Framing the subject...")
        image = resize_foreground(image, 0.85)
        arr = np.array(image).astype(np.float32) / 255.0
        arr = arr[:, :, :3] * arr[:, :, 3:4] + (1 - arr[:, :, 3:4]) * 0.5
        image = Image.fromarray((arr * 255.0).astype(np.uint8))

        _progress(40, "Generating 3D...")
        with torch.no_grad():
            scene_codes = self._model([image], device=self.device)

        _progress(55, "Extracting mesh...")
        meshes = self._model.extract_mesh(scene_codes, True, resolution=resolution)
        mesh = meshes[0]

        try:
            verts = mesh.vertices.numpy() if hasattr(mesh.vertices, "numpy") else mesh.vertices
            faces = mesh.faces.numpy() if hasattr(mesh.faces, "numpy") else mesh.faces
            self.jobs[job_id]["preview"] = {
                "vertices": verts.tolist() if hasattr(verts, "tolist") else verts,
                "faces": faces.tolist() if hasattr(faces, "tolist") else faces,
            }
        except Exception:
            pass

        _progress(75, "Exporting files...")
        tmpdir = tempfile.mkdtemp(prefix="elto_")

        tm = None
        try:
            if hasattr(mesh, "vertices"):
                tm = mesh
            else:
                tm = _trimesh.Trimesh(
                    vertices=(mesh.vertices.numpy() if hasattr(mesh.vertices, "numpy") else mesh.vertices),
                    faces=(mesh.faces.numpy() if hasattr(mesh.faces, "numpy") else mesh.faces),
                )
        except Exception:
            pass

        if tm is not None:
            if smooth_iter > 0:
                _progress(82, f"Smoothing ({smooth_iter} iter)...")
                try:
                    _trimesh.smoothing.filter_laplacian(
                        tm, lamb=0.5, iterations=smooth_iter,
                        implicit_time_integration=False,
                    )
                except Exception:
                    print("Smoothing failed:\n" + traceback.format_exc(), file=sys.stderr)

            if decimate_ratio < 0.99:
                target = max(500, int(len(tm.faces) * decimate_ratio))
                _progress(85, f"Decimating \u2192 {target:,} faces...")
                try:
                    tm = tm.simplify_quadric_decimation(target)
                except Exception:
                    try:
                        tm = tm.simplify_vertex_clustering(
                            voxel_size=tm.scale / (100 * decimate_ratio + 1)
                        )
                    except Exception:
                        print("Decimation failed:\n" + traceback.format_exc(), file=sys.stderr)
            mesh = tm

        paths = {
            "obj": os.path.join(tmpdir, "model.obj"),
            "glb": "",
            "gltf": "",
        }
        mesh.export(paths["obj"])

        glb = os.path.join(tmpdir, "model.glb")
        try:
            mesh.export(glb)
            paths["glb"] = glb
        except Exception:
            print("GLB export failed:\n" + traceback.format_exc(), file=sys.stderr)

        gltf = os.path.join(tmpdir, "model.gltf")
        try:
            mesh.export(gltf)
            paths["gltf"] = gltf
        except Exception:
            print("glTF export failed:\n" + traceback.format_exc(), file=sys.stderr)

        return paths
