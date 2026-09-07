import base64
import html as html_lib
import os
import sys
import tempfile
import traceback
import uuid

from flask import Flask, jsonify, request, send_file, Response

import viewer_templates as vt
from pipeline import Engine

# ── paths (mirrors the desktop app's EXE_DIR / SCRIPT_DIR logic) ───────────
def _get_app_dir():
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _get_exe_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


SCRIPT_DIR = _get_app_dir()
EXE_DIR = _get_exe_dir()
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

MODEL_DIR = os.path.join(EXE_DIR, "model")   # same convention as before: next to the exe

app = Flask(__name__, static_folder="static", static_url_path="/static")
engine = Engine(MODEL_DIR)

RESOLUTIONS = [128, 256, 384, 512]           # Fast, Balanced, High, Ultra

# in-memory registries (this is a single-user localhost app, so plain dicts are fine)
IMAGES = {}          # image_id -> filesystem path
UPLOADED_MESHES = {} # mesh_id  -> (data_url, file_label)


# ── page ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    with open(os.path.join(app.static_folder, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


# ── engine status (polled while the model loads) ───────────────────────────
@app.route("/api/status")
def api_status():
    pct, msg = engine.load_progress
    return jsonify({
        "ready": engine.ready,
        "error": engine.load_error,
        "progress": pct,
        "message": msg,
        "device": engine.device if engine.ready else None,
    })


# ── import image (file picker / drag-drop / paste / crop result) ──────────
@app.route("/api/import", methods=["POST"])
def api_import():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "no file"}), 400
    ext = os.path.splitext(f.filename or "")[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
        ext = ".png"
    tmp = tempfile.NamedTemporaryFile(suffix=ext, prefix="elto_img_", delete=False)
    f.save(tmp.name)
    tmp.close()

    image_id = uuid.uuid4().hex
    IMAGES[image_id] = tmp.name
    return jsonify({"ok": True, "image_id": image_id})


# ── generate ────────────────────────────────────────────────────────────
@app.route("/api/generate", methods=["POST"])
def api_generate():
    if not engine.ready:
        return jsonify({"ok": False, "error": "engine not ready"}), 400

    data = request.get_json(force=True)
    image_id = data.get("image_id")
    quality_idx = int(data.get("quality_idx", 1))
    decimate_pct = float(data.get("decimate_pct", 100))   # 100 = Off
    smooth_iter = int(data.get("smooth_iter", 0))          # 0 = Off

    path = IMAGES.get(image_id)
    if not path or not os.path.exists(path):
        return jsonify({"ok": False, "error": "image not found"}), 404

    resolution = RESOLUTIONS[max(0, min(3, quality_idx))]
    decimate_ratio = decimate_pct / 100.0

    job_id = engine.submit_job(path, resolution, decimate_ratio, smooth_iter)
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    job = engine.get_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "unknown job"}), 404
    pct, msg = job["progress"]
    return jsonify({
        "ok": True,
        "progress": pct,
        "message": msg,
        "done": job["result"] is not None,
        "error": job["error"],
        "result": {
            "obj": bool(job["result"] and job["result"].get("obj")),
            "glb": bool(job["result"] and job["result"].get("glb")),
            "gltf": bool(job["result"] and job["result"].get("gltf")),
        } if job["result"] else None,
    })


# ── viewer pages (served into the <iframe>) ────────────────────────────────
@app.route("/api/viewer/placeholder")
def api_viewer_placeholder():
    return Response(vt.build_placeholder_html(), mimetype="text/html")


@app.route("/api/viewer/<job_id>")
def api_viewer(job_id):
    job = engine.get_job(job_id)
    if job is None or not job["result"]:
        return Response(vt.build_placeholder_html(), mimetype="text/html")

    paths = job["result"]
    glb_path, gltf_path, obj_path = paths.get("glb", ""), paths.get("gltf", ""), paths.get("obj", "")

    if glb_path and os.path.exists(glb_path):
        html = _viewer_html_for_binary(glb_path, "glb")
    elif gltf_path and os.path.exists(gltf_path):
        html = _viewer_html_for_binary(gltf_path, "gltf")
    elif obj_path and os.path.exists(obj_path):
        with open(obj_path, "r", encoding="utf-8") as fh:
            obj_text = fh.read()
        obj_b64 = base64.b64encode(obj_text.encode("utf-8")).decode("ascii")
        html = vt.build_viewer_html(obj_b64, "model")
    else:
        return Response(vt.build_placeholder_html(), mimetype="text/html")

    return Response(html, mimetype="text/html")


def _viewer_html_for_binary(file_path, kind):
    mime = "model/gltf-binary" if kind == "glb" else "model/gltf+json"
    with open(file_path, "rb") as fh:
        raw = fh.read()
    b64 = base64.b64encode(raw).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    label = html_lib.escape(os.path.basename(file_path))
    return vt.build_gltf_viewer_html(data_url, label)


# ── download generated mesh (Save OBJ / GLB / GLTF buttons) ───────────────
@app.route("/api/download/<job_id>/<kind>")
def api_download(job_id, kind):
    job = engine.get_job(job_id)
    if job is None or not job["result"]:
        return "not found", 404
    src = job["result"].get(kind, "")
    if not src or not os.path.exists(src):
        return "not available", 404
    return send_file(src, as_attachment=True, download_name=f"model.{kind}")


# ── open an arbitrary local .glb/.gltf file in the viewer ──────────────────
@app.route("/api/open_mesh", methods=["POST"])
def api_open_mesh():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "no file"}), 400
    kind = "glb" if f.filename.lower().endswith(".glb") else "gltf"
    tmp = tempfile.NamedTemporaryFile(suffix="." + kind, prefix="elto_open_", delete=False)
    f.save(tmp.name)
    tmp.close()

    html = _viewer_html_for_binary(tmp.name, kind)
    mesh_id = uuid.uuid4().hex
    UPLOADED_MESHES[mesh_id] = html
    return jsonify({"ok": True, "mesh_id": mesh_id, "label": f.filename})


@app.route("/api/view_uploaded/<mesh_id>")
def api_view_uploaded(mesh_id):
    html = UPLOADED_MESHES.get(mesh_id)
    if html is None:
        return Response(vt.build_placeholder_html(), mimetype="text/html")
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5732, debug=False, threaded=True)
