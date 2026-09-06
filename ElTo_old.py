#!/usr/bin/env python3
"""ElTo - Image to 3D Model Converter"""

import sys
import os
import base64
import shutil
import tempfile
import traceback
import html as html_lib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# numpy, torch, rembg imported lazily inside functions — faster startup
from PIL import Image

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QGroupBox,
    QSpacerItem, QSizePolicy, QFrame, QSplitter,
    QDialog, QScrollArea, QSlider,
)
from PySide6.QtCore import (
    Qt, QUrl, QThread, Signal, QTimer, QRect, QPoint, QSize,
)
from PySide6.QtGui import (
    QFont, QPixmap, QIcon, QPainter, QPen, QColor, QBrush,
    QConicalGradient, QPainterPath,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtCore import Slot as QtSlot

# TSR and utils imported lazily inside ModelLoader/MeshGenerator — faster startup

APP_NAME    = "ElTo"
APP_TAGLINE = "Image -> 3D model"
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR  = os.path.join(SCRIPT_DIR, "vendor")

BG           = "#111111"
BG_PANEL     = "#161616"
BG_RAISED    = "#1c1c1c"
BORDER       = "#2a2a2a"
BORDER_SOFT  = "#202020"
TEXT         = "#e8e8e8"
TEXT_DIM     = "#8a8a8a"
TEXT_FAINT   = "#5a5a5a"
ACCENT       = "#38bdf8"
ACCENT_HOVER = "#5cc9fa"
ACCENT_SOFT  = "rgba(56,189,248,40)"
WARN         = "#f6ad55"
WARN_HOVER   = "#f8bd77"
WARN_SOFT    = "rgba(246,173,85,40)"

RESOLUTIONS = [
    ("Fast",    128),
    ("Balanced", 256),
    ("High",     384),
    ("Ultra",    512),
]

SIDEBAR_MIN_WIDTH     = 180
SIDEBAR_DEFAULT_WIDTH = 220


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _fill_background(image):
    import numpy as np
    arr = np.array(image).astype(np.float32) / 255.0
    arr = arr[:, :, :3] * arr[:, :, 3:4] + (1 - arr[:, :, 3:4]) * 0.5
    return Image.fromarray((arr * 255.0).astype(np.uint8))


def _read_vendor_file(name):
    path = os.path.join(VENDOR_DIR, name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return None


def _build_webchannel_script():
    """Return inline <script> for qwebchannel.js from vendor."""
    src = _read_vendor_file("qwebchannel.js")
    if src:
        return "<script>" + src + "</script>"
    return ""   # Qt injects it automatically if page.setWebChannel() is called


def _build_three_scripts_block():
    three_js = _read_vendor_file("three.min.js")
    orbit_js = _read_vendor_file("OrbitControls.js")
    obj_js   = _read_vendor_file("OBJLoader.js")
    if three_js and orbit_js and obj_js:
        return (
            "<script>" + three_js  + "</script>\n"
            "<script>" + orbit_js  + "</script>\n"
            "<script>" + obj_js    + "</script>\n"
        )
    base = "https://unpkg.com/three@0.128.0"
    return (
        f'<script src="{base}/build/three.min.js"></script>\n'
        f'<script src="{base}/examples/js/controls/OrbitControls.js"></script>\n'
        f'<script src="{base}/examples/js/loaders/OBJLoader.js"></script>\n'
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Circular Spinner Widget
# ══════════════════════════════════════════════════════════════════════════════

class CircularSpinner(QWidget):
    """A small animated arc spinner.  Call start() / stop() to animate."""

    def __init__(self, size=28, parent=None):
        super().__init__(parent)
        self._sz    = size
        self._angle = 0
        self._active = False
        self._timer = QTimer(self)
        self._timer.setInterval(20)          # ~50 fps
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def start(self):
        self._active = True
        self._timer.start()
        self.show()

    def stop(self):
        self._active = False
        self._timer.stop()
        self.update()
        self.hide()

    def _tick(self):
        self._angle = (self._angle + 6) % 360
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        sz   = self._sz
        half = sz / 2
        r    = half - 3

        # background circle
        p.setPen(QPen(QColor(BORDER), 2.5, Qt.SolidLine, Qt.RoundCap))
        p.drawEllipse(QPoint(int(half), int(half)), int(r), int(r))

        if not self._active:
            p.end()
            return

        # spinning arc
        pen = QPen(QColor(ACCENT), 2.5, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        rect = QRect(int(half - r), int(half - r), int(r * 2), int(r * 2))
        start_angle = (90 - self._angle) * 16   # Qt uses 1/16th degree
        span_angle  = -270 * 16
        p.drawArc(rect, start_angle, span_angle)
        p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  Free-Crop Dialog
# ══════════════════════════════════════════════════════════════════════════════

class _CropCanvas(QWidget):
    """Displays the image and lets the user draw AND resize a crop rectangle."""

    HANDLE_R = 7        # hit-test radius for corner/edge handles (px)

    # handle identifiers
    _H_NONE  = 0
    _H_TL    = 1   # top-left
    _H_TR    = 2
    _H_BL    = 3
    _H_BR    = 4
    _H_T     = 5   # top-edge
    _H_B     = 6
    _H_L     = 7
    _H_R     = 8
    _H_BODY  = 9   # drag whole rect

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self._orig_pixmap = pixmap
        self._scale  = 1.0
        self._offset = QPoint(0, 0)
        # rect stored as top-left + bottom-right in widget coords
        self._tl : QPoint | None = None
        self._br : QPoint | None = None
        # interaction state
        self._drag_handle = self._H_NONE
        self._drag_origin = QPoint()
        self._drag_tl_snap = QPoint()
        self._drag_br_snap = QPoint()
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

    # ── layout ───────────────────────────────────────────────────
    def sizeHint(self):
        return QSize(700, 500)

    def resizeEvent(self, e):
        self._fit()
        super().resizeEvent(e)

    def _fit(self):
        if self._orig_pixmap.isNull():
            return
        sw, sh = self.width(), self.height()
        iw, ih = self._orig_pixmap.width(), self._orig_pixmap.height()
        self._scale  = min(sw / iw, sh / ih, 1.0)
        dw = int(iw * self._scale)
        dh = int(ih * self._scale)
        self._offset = QPoint((sw - dw) // 2, (sh - dh) // 2)

    # ── handle detection ─────────────────────────────────────────
    def _handles(self):
        """Return dict {handle_id: center_QPoint} when rect exists."""
        if self._tl is None:
            return {}
        r = self._rect_norm()
        cx = (r.left() + r.right()) // 2
        cy = (r.top()  + r.bottom()) // 2
        return {
            self._H_TL: QPoint(r.left(),  r.top()),
            self._H_TR: QPoint(r.right(), r.top()),
            self._H_BL: QPoint(r.left(),  r.bottom()),
            self._H_BR: QPoint(r.right(), r.bottom()),
            self._H_T:  QPoint(cx,        r.top()),
            self._H_B:  QPoint(cx,        r.bottom()),
            self._H_L:  QPoint(r.left(),  cy),
            self._H_R:  QPoint(r.right(), cy),
        }

    def _hit_handle(self, pos: QPoint):
        for hid, center in self._handles().items():
            if (pos - center).manhattanLength() <= self.HANDLE_R + 3:
                return hid
        if self._tl is not None:
            if self._rect_norm().contains(pos):
                return self._H_BODY
        return self._H_NONE

    def _cursor_for(self, h):
        m = {
            self._H_TL: Qt.SizeFDiagCursor, self._H_BR: Qt.SizeFDiagCursor,
            self._H_TR: Qt.SizeBDiagCursor, self._H_BL: Qt.SizeBDiagCursor,
            self._H_T:  Qt.SizeVerCursor,   self._H_B:  Qt.SizeVerCursor,
            self._H_L:  Qt.SizeHorCursor,   self._H_R:  Qt.SizeHorCursor,
            self._H_BODY: Qt.SizeAllCursor,
        }
        return m.get(h, Qt.CrossCursor)

    # ── mouse ─────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        pos = e.position().toPoint()
        if e.button() == Qt.LeftButton:
            h = self._hit_handle(pos)
            if h == self._H_NONE:
                # start a brand-new selection
                self._tl = pos
                self._br = pos
                self._drag_handle = self._H_BR   # grow from bottom-right
            else:
                self._drag_handle = h
            self._drag_origin   = pos
            r = self._rect_norm()
            self._drag_tl_snap  = QPoint(r.topLeft())
            self._drag_br_snap  = QPoint(r.bottomRight())
            self.update()

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        if e.buttons() & Qt.LeftButton and self._drag_handle != self._H_NONE:
            dx = pos.x() - self._drag_origin.x()
            dy = pos.y() - self._drag_origin.y()
            tl = QPoint(self._drag_tl_snap)
            br = QPoint(self._drag_br_snap)
            h = self._drag_handle
            if   h == self._H_TL:   tl.setX(tl.x()+dx); tl.setY(tl.y()+dy)
            elif h == self._H_TR:   br.setX(br.x()+dx); tl.setY(tl.y()+dy)
            elif h == self._H_BL:   tl.setX(tl.x()+dx); br.setY(br.y()+dy)
            elif h == self._H_BR:   br.setX(br.x()+dx); br.setY(br.y()+dy)
            elif h == self._H_T:    tl.setY(tl.y()+dy)
            elif h == self._H_B:    br.setY(br.y()+dy)
            elif h == self._H_L:    tl.setX(tl.x()+dx)
            elif h == self._H_R:    br.setX(br.x()+dx)
            elif h == self._H_BODY: tl.setX(tl.x()+dx); tl.setY(tl.y()+dy); br.setX(br.x()+dx); br.setY(br.y()+dy)
            self._tl = tl
            self._br = br
            self.update()
        else:
            # update cursor only
            self.setCursor(self._cursor_for(self._hit_handle(pos)))

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_handle = self._H_NONE
            self.update()

    # ── paint ─────────────────────────────────────────────────────
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.Antialiasing)

        iw = int(self._orig_pixmap.width()  * self._scale)
        ih = int(self._orig_pixmap.height() * self._scale)
        p.drawPixmap(self._offset.x(), self._offset.y(), iw, ih, self._orig_pixmap)

        if self._tl is not None and self._br is not None:
            rect = self._rect_norm()

            # dim outside
            p.fillRect(self.rect(), QColor(0, 0, 0, 110))

            # bright inside
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.fillRect(rect, Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.drawPixmap(rect, self._orig_pixmap,
                         self._widget_rect_to_image_rect(rect))

            # border
            p.setPen(QPen(QColor(ACCENT), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect)

            # dashed rule-of-thirds grid
            pen3 = QPen(QColor(255, 255, 255, 50), 0.5, Qt.DashLine)
            p.setPen(pen3)
            for i in (1, 2):
                x = rect.left() + rect.width()  * i // 3
                y = rect.top()  + rect.height() * i // 3
                p.drawLine(x, rect.top(),  x, rect.bottom())
                p.drawLine(rect.left(), y, rect.right(), y)

            # draw handles
            handles = self._handles()
            for hid, center in handles.items():
                is_corner = hid in (self._H_TL, self._H_TR, self._H_BL, self._H_BR)
                r = self.HANDLE_R if is_corner else self.HANDLE_R - 2
                p.setBrush(QColor(ACCENT))
                p.setPen(QPen(QColor("#ffffff"), 1.5))
                p.drawEllipse(center, r, r)

        p.end()

    # ── helpers ───────────────────────────────────────────────────
    def _rect_norm(self) -> QRect:
        if self._tl is None:
            return QRect()
        return QRect(self._tl, self._br).normalized()

    def _selection_rect_widget(self) -> QRect:
        return self._rect_norm()

    def _widget_rect_to_image_rect(self, wrect: QRect) -> QRect:
        """Convert widget-space rect to original-image-space rect."""
        ox, oy = self._offset.x(), self._offset.y()
        s = self._scale
        x1 = max(0, int((wrect.left()   - ox) / s))
        y1 = max(0, int((wrect.top()    - oy) / s))
        x2 = min(self._orig_pixmap.width(),  int((wrect.right()  - ox) / s))
        y2 = min(self._orig_pixmap.height(), int((wrect.bottom() - oy) / s))
        return QRect(QPoint(x1, y1), QPoint(x2, y2))

    def get_cropped_pixmap(self):
        """Return a QPixmap of the selected area, or None if no selection."""
        if self._tl is None or self._br is None:
            return None
        wrect  = self._rect_norm()
        irect  = self._widget_rect_to_image_rect(wrect)
        if irect.width() < 4 or irect.height() < 4:
            return None
        return self._orig_pixmap.copy(irect)


class CropDialog(QDialog):
    """Full-screen-like crop dialog with a free-rect selection tool."""

    cropped = Signal(QPixmap)   # emitted when user confirms crop

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crop Image")
        self.setMinimumSize(600, 480)
        self.resize(800, 580)
        self.setStyleSheet(f"""
            QDialog {{ background:{BG}; color:{TEXT}; }}
            QLabel  {{ color:{TEXT_DIM}; font-size:10px; }}
            QPushButton {{
                background:{BG_RAISED}; color:{TEXT};
                border:1px solid {BORDER}; border-radius:5px;
                padding:6px 16px; font-size:11px; font-weight:600;
                min-height:26px;
            }}
            QPushButton:hover  {{ border-color:{ACCENT}; background:{BORDER_SOFT}; }}
            QPushButton:pressed{{ background:{BORDER}; }}
            QPushButton#btnCrop {{
                background:{BG_RAISED}; color:{ACCENT};
                border:1.5px solid {ACCENT};
            }}
            QPushButton#btnCrop:hover {{
                background:{ACCENT_SOFT}; border-color:{ACCENT_HOVER};
                color:{ACCENT_HOVER};
            }}
            QPushButton#btnCrop:pressed {{
                background:{ACCENT}; color:#052330;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        hint = QLabel("Drag to select an area, then click  Crop.")
        hint.setAlignment(Qt.AlignCenter)
        root.addWidget(hint)

        self._canvas = _CropCanvas(pixmap)
        root.addWidget(self._canvas, 1)

        # bottom bar
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self._btn_reset = QPushButton("Reset")
        self._btn_reset.clicked.connect(self._reset)
        bar.addWidget(self._btn_reset)

        bar.addStretch(1)

        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.clicked.connect(self.reject)
        bar.addWidget(self._btn_cancel)

        self._btn_crop = QPushButton("✂  Crop")
        self._btn_crop.setObjectName("btnCrop")
        self._btn_crop.clicked.connect(self._do_crop)
        bar.addWidget(self._btn_crop)

        root.addLayout(bar)

    def _reset(self):
        self._canvas._tl = None
        self._canvas._br = None
        self._canvas._drag_handle = self._canvas._H_NONE
        self._canvas.update()

    def _do_crop(self):
        pix = self._canvas.get_cropped_pixmap()
        if pix is None:
            return   # no selection yet — do nothing
        self.cropped.emit(pix)
        self.accept()


# ══════════════════════════════════════════════════════════════════════════════
#  Worker threads
# ══════════════════════════════════════════════════════════════════════════════

PLACEHOLDER_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  html,body{margin:0;height:100%;background:__BG__;overflow:hidden;}
</style></head>
<body>
__THREE_SCRIPTS__
<script>
(function(){
  if(!window.THREE||!THREE.OrbitControls){
    document.body.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#5a5a5a;font-family:Segoe UI,sans-serif;font-size:13px">Loading 3D engine...</div>';
    return;
  }
  var scene=new THREE.Scene();
  scene.background=new THREE.Color(0x111111);
  var camera=new THREE.PerspectiveCamera(50,innerWidth/innerHeight,0.01,2000);
  camera.position.set(2,1.5,2);
  var renderer=new THREE.WebGLRenderer({antialias:true});
  renderer.setSize(innerWidth,innerHeight);
  renderer.setPixelRatio(Math.min(devicePixelRatio,2));
  document.body.appendChild(renderer.domElement);
  var controls=new THREE.OrbitControls(camera,renderer.domElement);
  controls.enableDamping=true;controls.dampingFactor=0.08;
  scene.add(new THREE.HemisphereLight(0xffffff,0x2a2a2a,1.15));
  var key=new THREE.DirectionalLight(0xffffff,1.4);key.position.set(3,4,2);scene.add(key);
  var fill=new THREE.DirectionalLight(0xffffff,0.55);fill.position.set(-3,-1,-2);scene.add(fill);
  var grid=new THREE.GridHelper(4,20,0x2a2a2a,0x1a1a1a);grid.position.y=-0.001;scene.add(grid);
  var axes=new THREE.AxesHelper(0.6);axes.position.set(-1.8,0.01,-1.8);scene.add(axes);
  (function animate(){requestAnimationFrame(animate);controls.update();renderer.render(scene,camera)})();
  addEventListener('resize',function(){
    camera.aspect=innerWidth/innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(innerWidth,innerHeight);
  });
})();
</script></body></html>"""


class ModelLoader(QThread):
    loaded = Signal(object, object, str)
    failed = Signal(str)

    def run(self):
        try:
            import torch
            import rembg
            from elto.system import TSR
            device     = "cuda:0" if torch.cuda.is_available() else "cpu"
            model_path = os.path.join(SCRIPT_DIR, "model")
            model      = TSR.from_pretrained(
                model_path, config_name="config.yaml", weight_name="model.ckpt"
            )
            model.renderer.set_chunk_size(8192)
            model.to(device)
            session = rembg.new_session()
            self.loaded.emit(model, session, device)
        except Exception:
            self.failed.emit(traceback.format_exc())


class MeshGenerator(QThread):
    progress    = Signal(int, str)
    finished_ok = Signal(dict)
    failed      = Signal(str)

    def __init__(self, model, rembg_session, device, image_path, resolution,
                 decimate_ratio=1.0, smooth_iter=0):
        super().__init__()
        self.model          = model
        self.rembg_session  = rembg_session
        self.device         = device
        self.image_path     = image_path
        self.resolution     = resolution
        self.decimate_ratio = decimate_ratio   # 0.0-1.0, 1.0 = no decimation
        self.smooth_iter    = smooth_iter      # 0 = no smoothing

    def run(self):
        try:
            import torch
            from elto.utils import remove_background, resize_foreground
            self.progress.emit(8,  "Loading image...")
            image = Image.open(self.image_path).convert("RGB")

            self.progress.emit(18, "Removing background...")
            image = remove_background(image, self.rembg_session)

            self.progress.emit(28, "Framing the subject...")
            image = resize_foreground(image, 0.85)
            image = _fill_background(image)

            self.progress.emit(40, "Generating 3D...")
            with torch.no_grad():
                scene_codes = self.model([image], device=self.device)

            self.progress.emit(72, "Extracting mesh...")
            meshes = self.model.extract_mesh(scene_codes, True, resolution=self.resolution)
            mesh   = meshes[0]

            self.progress.emit(88, "Exporting files...")
            tmpdir = tempfile.mkdtemp(prefix="elto_")

            # ── post-processing ───────────────────────────────────
            import trimesh as _trimesh
            # convert to trimesh for optional smoothing / decimation
            tm = None
            try:
                # mesh from extract_mesh is already a trimesh-compatible object
                if hasattr(mesh, 'vertices'):
                    tm = mesh
                else:
                    tm = _trimesh.Trimesh(
                        vertices=mesh.vertices.numpy() if hasattr(mesh.vertices,'numpy') else mesh.vertices,
                        faces=mesh.faces.numpy()    if hasattr(mesh.faces,   'numpy') else mesh.faces,
                    )
            except Exception:
                pass

            if tm is not None:
                # smooth (Laplacian)
                if self.smooth_iter > 0:
                    self.progress.emit(89, f"Smoothing ({self.smooth_iter} iter)...")
                    try:
                        _trimesh.smoothing.filter_laplacian(
                            tm, lamb=0.5, iterations=self.smooth_iter,
                            implicit_time_integration=False,
                        )
                    except Exception:
                        print("Smoothing failed:\n" + traceback.format_exc())

                # decimate
                if self.decimate_ratio < 0.99:
                    target_faces = max(500, int(len(tm.faces) * self.decimate_ratio))
                    self.progress.emit(90, f"Decimating → {target_faces:,} faces...")
                    try:
                        tm = tm.simplify_quadric_decimation(target_faces)
                    except Exception:
                        try:
                            tm = tm.simplify_vertex_clustering(
                                voxel_size=tm.scale / (100 * self.decimate_ratio + 1)
                            )
                        except Exception:
                            print("Decimation failed:\n" + traceback.format_exc())
                mesh = tm
            # ─────────────────────────────────────────────────────
            paths  = {"obj": os.path.join(tmpdir, "model.obj"), "glb": "", "gltf": ""}
            mesh.export(paths["obj"])

            glb_path = os.path.join(tmpdir, "model.glb")
            try:
                mesh.export(glb_path)
                paths["glb"] = glb_path
            except Exception:
                print("GLB export unavailable:\n" + traceback.format_exc())

            gltf_path = os.path.join(tmpdir, "model.gltf")
            try:
                mesh.export(gltf_path)
                paths["gltf"] = gltf_path
            except Exception:
                print("glTF export unavailable:\n" + traceback.format_exc())

            self.progress.emit(100, "Done")
            self.finished_ok.emit(paths)
        except Exception:
            tb = traceback.format_exc()
            print(tb)
            self.failed.emit(tb)


class NoPrintWebPage(QWebEnginePage):
    def print(self, *args, **kwargs):
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  Main Window
# ══════════════════════════════════════════════════════════════════════════════
#  WebChannel Bridge  (JS → Python file-save)
# ══════════════════════════════════════════════════════════════════════════════

from PySide6.QtCore import QObject

class ViewerBridge(QObject):
    """Exposed to JavaScript via QWebChannel as 'bridge'.
    JS calls bridge.saveFile(kind, data, dims) to trigger Python save dialogs.
    """

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._win = window          # reference to ElToWindow for dialogs + status

    # kind = "png" | "obj" | "glb" | "gltf"
    # data = base64 data-URL string for png/glb/gltf,  plain text for obj
    # dims = "1920x1080" for png,  "" otherwise
    @QtSlot(str, str, str)
    def saveFile(self, kind: str, data: str, dims: str):
        try:
            if kind == "png":
                self._save_png(data, dims)
            elif kind == "obj":
                self._save_text(data, "model.obj",
                                "OBJ Files (*.obj);;All files (*.*)")
            elif kind in ("glb", "gltf"):
                self._save_binary_dataurl(data, f"model.{kind}",
                    "GLB Files (*.glb);;All files (*.*)" if kind == "glb"
                    else "glTF Files (*.gltf);;All files (*.*)")
        except Exception:
            print(traceback.format_exc())
            self._win.status_label.setText("Save failed — see console")

    def _save_png(self, data_url: str, dims: str):
        """Grab the viewer widget at the requested resolution and save as PNG."""
        try:
            w_str, h_str = dims.split("x")
            target_w, target_h = int(w_str), int(h_str)
        except Exception:
            target_w, target_h = 1920, 1080

        path, _ = QFileDialog.getSaveFileName(
            self._win, "Save Render",
            f"render_{dims}.png",
            "PNG Image (*.png);;All files (*.*)"
        )
        if not path:
            return

        # grab() captures the actual rendered WebGL frame — true render, not screenshot
        raw_pix = self._win.web_view.grab()
        if raw_pix.isNull():
            self._win.status_label.setText("Render failed — viewer not ready")
            return

        # scale up to target resolution with high quality
        final_pix = raw_pix.scaled(
            target_w, target_h,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        if final_pix.save(path, "PNG"):
            self._win.status_label.setText(f"Render saved: {os.path.basename(path)}")
        else:
            self._win.status_label.setText("Render save failed")

    def _save_text(self, text: str, default_name: str, ffilter: str):
        path, _ = QFileDialog.getSaveFileName(
            self._win, "Export Model", default_name, ffilter
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self._win.status_label.setText(f"Exported: {os.path.basename(path)}")

    def _save_binary_dataurl(self, data_url: str, default_name: str, ffilter: str):
        _, b64 = data_url.split(",", 1)
        b64 += "=" * (-len(b64) % 4)
        raw = base64.b64decode(b64)
        path, _ = QFileDialog.getSaveFileName(
            self._win, "Export Model", default_name, ffilter
        )
        if path:
            with open(path, "wb") as f:
                f.write(raw)
            self._win.status_label.setText(f"Exported: {os.path.basename(path)}")

    # ── mesh command channel (future extensibility) ───────────────
    @QtSlot(str, str)
    def meshCommand(self, cmd: str, payload: str):
        """Receive mesh operation commands from JS (reserved for future use).
        cmd: 'smooth_lo' | 'smooth_hi' | 'decimate' | 'reset'
        payload: extra data (e.g. decimate ratio as string)
        """
        # Currently all mesh ops run client-side in Three.js.
        # This slot is here so the bridge channel is fully wired
        # and future server-side ops (trimesh re-processing) can
        # be triggered without changing the JS interface.
        self._win.status_label.setText(f"Mesh op: {cmd}")


# ══════════════════════════════════════════════════════════════════════════════

class ElToWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(890, 760)
        self.setMinimumSize(640, 380)
        self.setStyleSheet(self._build_style())

        self.model             = None
        self.rembg_session     = None
        self.device            = None
        self.current_image_path = None
        # QPixmap of the (possibly cropped) image used as thumbnail / source
        self._current_pixmap   = None
        self.mesh_paths        = {"obj": "", "glb": "", "gltf": ""}
        self.model_loader      = None
        self.generator         = None
        self._generating       = False   # explicit flag — more reliable than thread.isRunning()

        self._build_ui()
        self._show_placeholder()
        self._start_model_load()
        # allow dropping files anywhere on the main window
        self.setAcceptDrops(True)

    # ── stylesheet ────────────────────────────────────────────────
    def _build_style(self):
        return f"""
        QMainWindow, QWidget {{ background-color:{BG}; color:{TEXT}; }}

        QGroupBox {{
            font-weight:600; color:{TEXT_DIM}; font-size:10px;
            border:none; margin-top:4px; padding:0px;
            background-color:transparent;
        }}
        QGroupBox::title {{
            subcontrol-origin:margin; subcontrol-position:top left;
            left:0px; top:0px; padding:0 0;
            background-color:transparent; color:{TEXT_DIM};
        }}

        QPushButton {{
            background-color:{BG_RAISED}; color:{TEXT};
            border:1px solid {BORDER}; border-radius:5px;
            padding:5px 8px; font-size:11px; font-weight:600;
            min-height:26px;
        }}
        QPushButton:hover   {{ border-color:{ACCENT}; background-color:{BORDER_SOFT}; }}
        QPushButton:pressed {{ background-color:{BORDER}; }}
        QPushButton:disabled {{ color:{TEXT_FAINT}; border-color:{BORDER_SOFT}; background-color:{BG_RAISED}; }}

        QPushButton#btnPrimary {{
            background-color:{BG_RAISED}; color:{ACCENT}; border:1.5px solid {ACCENT};
        }}
        QPushButton#btnPrimary:hover    {{ background-color:{ACCENT_SOFT}; border-color:{ACCENT_HOVER}; color:{ACCENT_HOVER}; }}
        QPushButton#btnPrimary:pressed  {{ background-color:{ACCENT}; color:#052330; border-color:{ACCENT}; }}
        QPushButton#btnPrimary:disabled {{ background-color:{BG_RAISED}; color:{TEXT_FAINT}; border-color:{BORDER_SOFT}; }}

        QPushButton#btnOutput {{
            background-color:{BG_RAISED}; color:{WARN}; border:1.5px solid {WARN};
        }}
        QPushButton#btnOutput:hover    {{ background-color:{WARN_SOFT}; border-color:{WARN_HOVER}; color:{WARN_HOVER}; }}
        QPushButton#btnOutput:pressed  {{ background-color:{WARN}; color:#2b1a06; border-color:{WARN}; }}
        QPushButton#btnOutput:disabled {{ background-color:{BG_RAISED}; color:{TEXT_FAINT}; border-color:{BORDER_SOFT}; }}

        QPushButton#btnEdit {{
            background-color:{BG_RAISED}; color:{TEXT_DIM};
            border:1px solid {BORDER_SOFT}; border-radius:5px;
            padding:5px 8px; font-size:11px; font-weight:600;
            min-height:26px;
        }}
        QPushButton#btnEdit:hover   {{ border-color:{ACCENT}; color:{ACCENT}; }}
        QPushButton#btnEdit:pressed {{ background-color:{BORDER}; }}

        QComboBox {{
            background-color:{BG_RAISED}; border:1px solid {BORDER};
            border-radius:4px; padding:3px 6px; min-height:24px; font-size:11px;
        }}
        QComboBox:hover {{ border-color:{ACCENT}; }}
        QComboBox QAbstractItemView {{
            background-color:{BG_RAISED}; color:{TEXT};
            selection-background-color:{ACCENT}; selection-color:#052330;
            border:1px solid {BORDER};
        }}

        QLabel {{ color:{TEXT}; font-size:11px; }}
        QLabel#appSubtitle  {{ color:{TEXT_DIM}; font-size:9px; }}
        QLabel#fieldCaption {{ color:{TEXT_DIM}; font-size:9px; }}
        QLabel#statusLabel  {{ color:{TEXT_DIM}; font-size:9px; }}
        QLabel#thumb {{
            background-color:{BG_RAISED}; border:1px dashed {BORDER};
            border-radius:6px; color:{TEXT_FAINT}; font-size:10px;
        }}

        QSplitter::handle       {{ background-color:{BORDER_SOFT}; width:3px; }}
        QSplitter::handle:hover {{ background-color:{ACCENT}; }}
        """

    # ── root layout ───────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(3)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_viewer_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([SIDEBAR_DEFAULT_WIDTH, self.width() - SIDEBAR_DEFAULT_WIDTH])
        self._splitter = splitter

        root.addWidget(splitter)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_splitter"):
            total = self._splitter.width()
            sb    = max(SIDEBAR_MIN_WIDTH, self._splitter.sizes()[0])
            self._splitter.setSizes([sb, max(0, total - sb)])

    # ── sidebar ───────────────────────────────────────────────────
    def _build_sidebar(self):
        sidebar = QWidget()
        sidebar.setMinimumWidth(SIDEBAR_MIN_WIDTH)
        sidebar.setMaximumWidth(320)
        sidebar.setStyleSheet(f"QWidget {{ background-color:{BG_PANEL}; }}")

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(10)

        # Header
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel(APP_NAME)
        title.setFont(QFont("Segoe UI", 12, QFont.Bold))
        subtitle = QLabel(APP_TAGLINE)
        subtitle.setObjectName("appSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        layout.addLayout(title_box)

        sep_top = QFrame()
        sep_top.setFixedHeight(1)
        sep_top.setStyleSheet(f"background-color:{BORDER_SOFT};")
        layout.addWidget(sep_top)

        # ── INPUT ─────────────────────────────────────────────────
        input_group = QGroupBox("INPUT")
        ig = QVBoxLayout(input_group)
        ig.setSpacing(6)
        ig.setContentsMargins(0, 14, 0, 0)

        self.thumb_label = QLabel("Import / Drag / Paste here")
        self.thumb_label.setObjectName("thumb")
        self.thumb_label.setAlignment(Qt.AlignCenter)
        self.thumb_label.setMinimumHeight(80)
        self.thumb_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.thumb_label.setWordWrap(True)
        # accept file drops directly on the thumbnail
        self.thumb_label.setAcceptDrops(True)
        self.thumb_label.dragEnterEvent  = self._thumb_drag_enter
        self.thumb_label.dragMoveEvent   = self._thumb_drag_move
        self.thumb_label.dropEvent       = self._thumb_drop
        ig.addWidget(self.thumb_label)

        # Import + Edit row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        self.btn_import = QPushButton("Import Image")
        self.btn_import.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_import.clicked.connect(self.select_image)
        btn_row.addWidget(self.btn_import)

        self.btn_edit = QPushButton("✎ Edit")
        self.btn_edit.setObjectName("btnEdit")
        self.btn_edit.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.btn_edit.setVisible(False)          # hidden until image loaded
        self.btn_edit.clicked.connect(self.open_crop_dialog)
        btn_row.addWidget(self.btn_edit)

        ig.addLayout(btn_row)
        layout.addWidget(input_group)

        # ── GENERATE ──────────────────────────────────────────────
        gen_group = QGroupBox("GENERATE")
        gg = QVBoxLayout(gen_group)
        gg.setSpacing(6)
        gg.setContentsMargins(0, 14, 0, 0)

        res_caption = QLabel("Quality")
        res_caption.setObjectName("fieldCaption")
        gg.addWidget(res_caption)

        # Radio-style quality selector
        self._res_btns = []
        for i, (label, _) in enumerate(RESOLUTIONS):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setObjectName("resBtn")
            btn.setStyleSheet(f"""
                QPushButton#resBtn {{
                    background:{BG_RAISED}; color:{TEXT_DIM};
                    border:1px solid {BORDER_SOFT}; border-radius:5px;
                    padding:4px 6px; font-size:10px; font-weight:600;
                    min-height:22px; text-align:left;
                }}
                QPushButton#resBtn:checked {{
                    background:rgba(56,189,248,0.12);
                    color:{ACCENT}; border-color:{ACCENT};
                }}
                QPushButton#resBtn:hover:!checked {{
                    border-color:{ACCENT}; color:{TEXT};
                }}
            """)
            # add radio dot via text
            btn.setText(f"  {label}")
            btn.clicked.connect(lambda checked, idx=i: self._select_resolution(idx))
            self._res_btns.append(btn)
            gg.addWidget(btn)

        # default = Balanced (index 1)
        self._current_res_idx = 1
        self._res_btns[1].setChecked(True)
        self._res_btns[1].setText(f"● Balanced")
        self._res_btns[0].setText(f"  Fast")
        self._res_btns[2].setText(f"  High")
        self._res_btns[3].setText(f"  Ultra")

        # ── Mesh post-processing — horizontal radio buttons ──────
        _mb_style = f"""
            QPushButton {{
                background:{BG_RAISED}; color:{TEXT_DIM};
                border:1px solid {BORDER_SOFT}; border-radius:4px;
                padding:3px 0; font-size:9px; font-weight:600;
                min-height:20px;
            }}
            QPushButton:checked {{
                background:rgba(56,189,248,0.14);
                color:{ACCENT}; border-color:{ACCENT};
            }}
            QPushButton:hover:!checked {{
                border-color:{ACCENT}; color:{TEXT};
            }}
        """
        _DEC_VALS = [("Off", 100), ("10%", 10), ("25%", 25),
                     ("50%", 50), ("75%", 75), ("100%", 100)]
        _SMO_VALS = [("Off", 0), ("10%", 2), ("25%", 5),
                     ("50%", 10), ("75%", 15), ("100%", 20)]

        # helper to build a captioned row of radio buttons
        def _make_radio_row(caption, values, default_idx):
            cap = QLabel(caption)
            cap.setObjectName("fieldCaption")
            gg.addWidget(cap)
            row = QHBoxLayout()
            row.setSpacing(3)
            btns = []
            for label, _ in values:
                b = QPushButton(label)
                b.setCheckable(True)
                b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                b.setStyleSheet(_mb_style)
                row.addWidget(b)
                btns.append(b)
            def _sel(idx, btn_list=btns):
                for i, bb in enumerate(btn_list):
                    bb.setChecked(i == idx)
            for i, b in enumerate(btns):
                b.clicked.connect(lambda _, ii=i, s=_sel: s(ii))
            btns[default_idx].setChecked(True)
            gg.addLayout(row)
            return btns

        self._dec_btns = _make_radio_row("Decimate", _DEC_VALS, 0)  # default=Off
        self._smo_btns = _make_radio_row("Smooth",   _SMO_VALS, 0)  # default=Off
        self._dec_vals = [v for _, v in _DEC_VALS]
        self._smo_vals = [v for _, v in _SMO_VALS]

        # ── Generate button ────────────────────────────────────────
        self.btn_generate = QPushButton("Generate 3D Model")
        self.btn_generate.setObjectName("btnPrimary")
        self.btn_generate.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_generate.setEnabled(False)
        self.btn_generate.clicked.connect(self.generate_3d_model)
        gg.addWidget(self.btn_generate)

        # spinner + status text on the same row, below the button
        spinner_row = QHBoxLayout()
        spinner_row.setSpacing(6)
        spinner_row.setContentsMargins(0, 2, 0, 0)

        self._spinner = CircularSpinner(size=22)
        self._spinner.hide()
        spinner_row.addWidget(self._spinner, 0, Qt.AlignVCenter)

        self.progress_text = QLabel("Loading model...")
        self.progress_text.setStyleSheet(f"color:{TEXT_DIM}; font-size:9px;")
        self.progress_text.setWordWrap(True)
        self.progress_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        spinner_row.addWidget(self.progress_text, 1)

        gg.addLayout(spinner_row)

        layout.addWidget(gen_group)

        # ── OUTPUT ────────────────────────────────────────────────
        out_group = QGroupBox("OUTPUT")
        og = QVBoxLayout(out_group)
        og.setSpacing(4)
        og.setContentsMargins(0, 14, 0, 0)

        # shared style — identical to quality radio buttons
        _out_btn_style = f"""
            QPushButton {{
                background:{BG_RAISED}; color:{TEXT_DIM};
                border:1px solid {BORDER_SOFT}; border-radius:5px;
                padding:4px 6px; font-size:10px; font-weight:600;
                min-height:22px; text-align:left;
            }}
            QPushButton:hover:enabled {{
                border-color:{ACCENT}; color:{TEXT};
            }}
            QPushButton:disabled {{
                color:{TEXT_FAINT}; border-color:{BORDER_SOFT};
                background:{BG_RAISED};
            }}
        """

        self.btn_save_obj = QPushButton("  Save OBJ")
        self.btn_save_obj.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_save_obj.setEnabled(False)
        self.btn_save_obj.setToolTip("Save model as OBJ")
        self.btn_save_obj.setStyleSheet(_out_btn_style)
        self.btn_save_obj.clicked.connect(lambda: self._save_mesh("obj"))
        og.addWidget(self.btn_save_obj)

        self.btn_save_glb = QPushButton("  Save GLB")
        self.btn_save_glb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_save_glb.setEnabled(False)
        self.btn_save_glb.setToolTip("Save model as GLB (binary GLTF)")
        self.btn_save_glb.setStyleSheet(_out_btn_style)
        self.btn_save_glb.clicked.connect(lambda: self._save_mesh("glb"))
        og.addWidget(self.btn_save_glb)

        self.btn_save_gltf = QPushButton("  Save GLTF")
        self.btn_save_gltf.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_save_gltf.setEnabled(False)
        self.btn_save_gltf.setToolTip("Save model as GLTF")
        self.btn_save_gltf.setStyleSheet(_out_btn_style)
        self.btn_save_gltf.clicked.connect(lambda: self._save_mesh("gltf"))
        og.addWidget(self.btn_save_gltf)

        self.btn_open_glb = QPushButton("  Open .glb")
        self.btn_open_glb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_open_glb.setEnabled(True)
        self.btn_open_glb.setToolTip("Open a GLB file in the viewer")
        self.btn_open_glb.setStyleSheet(_out_btn_style)
        self.btn_open_glb.clicked.connect(lambda: self.open_mesh_file("glb"))
        og.addWidget(self.btn_open_glb)

        self.btn_open_gltf = QPushButton("  Open .gltf")
        self.btn_open_gltf.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_open_gltf.setEnabled(True)
        self.btn_open_gltf.setToolTip("Open a GLTF file in the viewer")
        self.btn_open_gltf.setStyleSheet(_out_btn_style)
        self.btn_open_gltf.clicked.connect(lambda: self.open_mesh_file("gltf"))
        og.addWidget(self.btn_open_gltf)

        layout.addWidget(out_group)

        # Footer
        layout.addSpacerItem(QSpacerItem(20, 4, QSizePolicy.Minimum, QSizePolicy.Expanding))

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color:{BORDER_SOFT};")
        layout.addWidget(sep)

        self.status_label = QLabel("Starting up...")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.device_label = QLabel("Device: detecting...")
        self.device_label.setStyleSheet(f"color:{TEXT_FAINT}; font-size:8px;")
        layout.addWidget(self.device_label)

        return sidebar

    # ── viewer panel ──────────────────────────────────────────────
    def _build_viewer_panel(self):
        panel = QFrame()
        panel.setStyleSheet(f"background-color:{BG};")
        v = QVBoxLayout(panel)
        v.setContentsMargins(8, 8, 8, 8)

        self.web_view = QWebEngineView()
        self.web_view.setMinimumSize(400, 300)
        self.web_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.web_view.setStyleSheet(
            f"background-color:{BG}; border:1px solid {BORDER}; border-radius:6px;"
        )
        page = NoPrintWebPage(self.web_view)
        self.web_view.setPage(page)

        # Allow local file access so setUrl(file://) works correctly
        settings = page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, True)

        # QWebChannel — exposes ViewerBridge as 'bridge' in JavaScript
        self._bridge  = ViewerBridge(self)
        self._channel = QWebChannel(page)
        self._channel.registerObject("bridge", self._bridge)
        page.setWebChannel(self._channel)

        v.addWidget(self.web_view)
        return panel

    # ── model loading ─────────────────────────────────────────────
    def _start_model_load(self):
        self.btn_generate.setEnabled(False)
        self.progress_text.setText("Loading model...")
        self.model_loader = ModelLoader()
        self.model_loader.loaded.connect(self.on_model_loaded)
        self.model_loader.failed.connect(self.on_model_load_failed)
        # keep thread alive: connect finished to a cleanup that delays GC
        self.model_loader.finished.connect(self._on_model_loader_finished)
        self.model_loader.start()

    def _on_model_loader_finished(self):
        """Called when ModelLoader thread exits — safe to release."""
        # Schedule deletion on the next event-loop tick, not immediately
        if self.model_loader:
            self.model_loader.deleteLater()
            self.model_loader = None

    def on_model_loaded(self, model, session, device):
        self.model         = model
        self.rembg_session = session
        self.device        = device
        self.device_label.setText(f"Device: {device}")
        self.progress_text.setText("Model ready")
        self.status_label.setText("Import an image to begin")
        self._update_generate_enabled()

    def on_model_load_failed(self, error_traceback):
        print(error_traceback)
        first = error_traceback.strip().splitlines()[-1] if error_traceback.strip() else "unknown error"
        self.progress_text.setText("Model failed to load")
        self.status_label.setText(f"Error: {first}")

    def _update_generate_enabled(self):
        ready = bool(self.model) and bool(self.current_image_path) and not self._generating
        self.btn_generate.setEnabled(ready)

    def _is_generating(self):
        return self._generating

    # ── image import ──────────────────────────────────────────────
    def select_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Input Image", "",
            "Image files (*.png *.jpg *.jpeg *.webp *.bmp);;All files (*.*)"
        )
        if not path:
            return
        self._load_image_path(path)

    # ── drag & drop on thumbnail ──────────────────────────────────
    def _thumb_drag_enter(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()
            self.thumb_label.setStyleSheet(
                f"background-color:#1e2a2e; border:1.5px dashed {ACCENT};"
                f" border-radius:6px; color:{ACCENT}; font-size:10px;"
            )
        else:
            e.ignore()

    def _thumb_drag_move(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def _thumb_drop(self, e):
        # restore default style
        self.thumb_label.setStyleSheet("")
        md = e.mimeData()
        if md.hasUrls():
            for url in md.urls():
                local = url.toLocalFile()
                if local and os.path.isfile(local):
                    ext = os.path.splitext(local)[1].lower()
                    if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
                        self._load_image_path(local)
                        e.acceptProposedAction()
                        return
        if md.hasImage():
            pix = QPixmap(md.imageData())
            if not pix.isNull():
                self._from_pixmap_no_path(pix)
                e.acceptProposedAction()

    # ── paste from clipboard (Ctrl+V) ────────────────────────────
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_V and (e.modifiers() & Qt.ControlModifier):
            self._paste_from_clipboard()
        else:
            super().keyPressEvent(e)

    def _paste_from_clipboard(self):
        cb  = QApplication.clipboard()
        md  = cb.mimeData()
        if md.hasImage():
            pix = QPixmap(cb.image())
            if not pix.isNull():
                self._from_pixmap_no_path(pix)
                return
        if md.hasUrls():
            for url in md.urls():
                local = url.toLocalFile()
                if local and os.path.isfile(local):
                    ext = os.path.splitext(local)[1].lower()
                    if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                        self._load_image_path(local)
                        return
        if md.hasText():
            text = md.text().strip()
            if os.path.isfile(text):
                self._load_image_path(text)

    def _from_pixmap_no_path(self, pix: QPixmap):
        """Handle an image that has no file path (clipboard / drag-image)."""
        tmp = tempfile.NamedTemporaryFile(
            suffix=".png", prefix="elto_paste_", delete=False
        )
        tmp.close()
        pix.save(tmp.name, "PNG")
        self.current_image_path = tmp.name
        self._apply_pixmap(pix)

    # ── window-level drag & drop (drop anywhere on the window) ───
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self._thumb_drop(e)

    def _load_image_path(self, path: str):
        """Set current_image_path and update thumbnail from a file path."""
        self.current_image_path = path
        pix = QPixmap(path)
        self._apply_pixmap(pix)

    def _apply_pixmap(self, pix: QPixmap):
        """Update thumbnail + internal state with a (possibly cropped) QPixmap."""
        self._current_pixmap = pix
        if not pix.isNull():
            scaled = pix.scaled(
                self.thumb_label.width() - 8,
                self.thumb_label.height() - 8,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            self.thumb_label.setText("")
            self.thumb_label.setPixmap(scaled)
        else:
            self.thumb_label.setText(os.path.basename(self.current_image_path or ""))

        # Show Edit button now that an image exists
        self.btn_edit.setVisible(True)

        self.mesh_paths = {"obj": "", "glb": "", "gltf": ""}
        # disable export buttons until next generation finishes
        self.btn_save_obj.setEnabled(False)
        self.btn_save_glb.setEnabled(False)
        self.btn_save_gltf.setEnabled(False)
        self._show_placeholder()
        self.status_label.setText("Image ready - click Generate")
        self._update_generate_enabled()

    # ── crop ──────────────────────────────────────────────────────
    def open_crop_dialog(self):
        if not self._current_pixmap or self._current_pixmap.isNull():
            return
        dlg = CropDialog(self._current_pixmap, self)
        dlg.cropped.connect(self._on_cropped)
        dlg.exec()

    def _on_cropped(self, cropped_pix: QPixmap):
        """Save cropped image to a temp file and reload as the active image."""
        tmp = tempfile.NamedTemporaryFile(
            suffix=".png", prefix="elto_crop_", delete=False
        )
        tmp.close()
        cropped_pix.save(tmp.name, "PNG")
        self.current_image_path = tmp.name
        self._apply_pixmap(cropped_pix)

    # ── resolution selector ───────────────────────────────────────
    def _select_resolution(self, idx: int):
        labels = [r[0] for r in RESOLUTIONS]
        for i, btn in enumerate(self._res_btns):
            if i == idx:
                btn.setChecked(True)
                btn.setText(f"● {labels[i]}")
            else:
                btn.setChecked(False)
                btn.setText(f"  {labels[i]}")
        self._current_res_idx = idx

    # ── batch processing ──────────────────────────────────────────
    # ── generation ────────────────────────────────────────────────
    def generate_3d_model(self):
        if not self.current_image_path or not self.model or self._is_generating():
            return
        self._generating = True
        resolution = RESOLUTIONS[self._current_res_idx][1]

        self.btn_import.setEnabled(False)
        self.btn_edit.setEnabled(False)
        self.btn_generate.setEnabled(False)
        self.btn_save_obj.setEnabled(False)
        self.btn_save_glb.setEnabled(False)
        self.btn_save_gltf.setEnabled(False)
        self.progress_text.setText("Starting...")
        self.status_label.setText("Generating...")
        self._spinner.start()

        # read from radio button groups
        dec_idx        = next((i for i,b in enumerate(self._dec_btns) if b.isChecked()), 0)
        smo_idx        = next((i for i,b in enumerate(self._smo_btns) if b.isChecked()), 0)
        decimate_ratio = self._dec_vals[dec_idx] / 100.0
        smooth_iter    = self._smo_vals[smo_idx]

        self.generator = MeshGenerator(
            self.model, self.rembg_session, self.device,
            self.current_image_path, resolution,
            decimate_ratio=decimate_ratio,
            smooth_iter=smooth_iter,
        )
        self.generator.progress.connect(self.on_generate_progress)
        self.generator.finished_ok.connect(self.on_generate_finished)
        self.generator.failed.connect(self.on_generate_failed)
        self.generator.finished.connect(self._on_generator_finished)
        self.generator.start()

    def _on_generator_finished(self):
        """Thread exited — safe to schedule deletion."""
        if self.generator:
            self.generator.deleteLater()
            self.generator = None

    def on_generate_progress(self, value, message):
        self.progress_text.setText(f"{message}  ({value}%)")

    def on_generate_finished(self, paths):
        self._spinner.stop()
        self._generating = False          # clear flag immediately
        self.mesh_paths = paths
        # Note: self.generator will be cleared by _on_generator_finished
        # but we clear it here too so _is_generating() returns False immediately
        self.btn_import.setEnabled(True)
        self.btn_edit.setEnabled(True)
        self.btn_save_obj.setEnabled(bool(paths.get("obj")))
        self.btn_save_glb.setEnabled(bool(paths.get("glb")))
        self.btn_save_gltf.setEnabled(bool(paths.get("gltf")))
        self.progress_text.setText("Done ✓")
        self.status_label.setText("Done - drag to orbit, scroll to zoom")
        self._update_generate_enabled()   # now works correctly

        # Display: prefer GLB (has vertex colors) → GLTF → OBJ fallback
        glb_path  = paths.get("glb",  "")
        gltf_path = paths.get("gltf", "")
        obj_path  = paths.get("obj",  "")

        if glb_path and os.path.exists(glb_path):
            self._display_model_glb(glb_path, "glb")
        elif gltf_path and os.path.exists(gltf_path):
            self._display_model_glb(gltf_path, "gltf")
        elif obj_path and os.path.exists(obj_path):
            self.status_label.setText("Done (OBJ fallback — colours may be missing)")
            self._display_model(obj_path)
        else:
            self.status_label.setText("Generation done but no preview file found")

    def on_generate_failed(self, error_traceback):
        self._spinner.stop()
        self._generating = False          # clear flag immediately
        # Note: self.generator cleared by _on_generator_finished
        self.btn_import.setEnabled(True)
        self.btn_edit.setEnabled(True)
        first = error_traceback.strip().splitlines()[-1] if error_traceback.strip() else "unknown error"
        self.progress_text.setText("Generation failed")
        self.status_label.setText(f"Error: {first}")
        self._update_generate_enabled()

    # ── save generated mesh ───────────────────────────────────────
    def _save_mesh(self, kind: str):
        """Export the generated mesh with correct texture handling per format."""
        src = self.mesh_paths.get(kind, "")
        if not src or not os.path.exists(src):
            self.status_label.setText(f".{kind.upper()} not available")
            return

        filters = {
            "obj":  "OBJ Files (*.obj);;All files (*.*)",
            "glb":  "GLB Files (*.glb);;All files (*.*)",
            "gltf": "glTF Files (*.gltf);;All files (*.*)",
        }
        dest, _ = QFileDialog.getSaveFileName(
            self, f"Save {kind.upper()}", f"model.{kind}", filters[kind]
        )
        if not dest:
            return

        try:
            if kind == "obj":
                # OBJ: copy as-is (no texture embedding possible in plain OBJ)
                shutil.copy2(src, dest)

            elif kind == "glb":
                # GLB: re-export via trimesh so vertex colors are baked as
                # a proper PBR material texture inside the binary GLTF.
                # trimesh already stored vertex colors on the mesh — just
                # re-export to GLB which embeds everything in one file.
                import trimesh as _trimesh
                mesh = _trimesh.load(src, force="mesh")
                # bake vertex colors into a texture if the mesh has them
                if hasattr(mesh.visual, "vertex_colors") and mesh.visual.vertex_colors is not None:
                    # convert to a UV-baked texture for better GLB compatibility
                    mesh.visual = mesh.visual.to_texture()
                mesh.export(dest)

            elif kind == "gltf":
                # GLTF: export mesh + separate PNG texture file next to it
                import trimesh as _trimesh
                mesh = _trimesh.load(src, force="mesh")
                dest_dir  = os.path.dirname(dest)
                dest_base = os.path.splitext(os.path.basename(dest))[0]
                tex_name  = dest_base + "_texture.png"
                tex_path  = os.path.join(dest_dir, tex_name)

                if hasattr(mesh.visual, "vertex_colors") and mesh.visual.vertex_colors is not None:
                    mesh.visual = mesh.visual.to_texture()

                # export GLTF — trimesh writes texture as a separate file
                mesh.export(dest)
                # if trimesh wrote an embedded texture, also save standalone PNG
                if hasattr(mesh.visual, "material") and hasattr(mesh.visual.material, "image"):
                    img = mesh.visual.material.image
                    if img is not None:
                        img.save(tex_path)
                        self.status_label.setText(
                            f"Saved: {os.path.basename(dest)} + {tex_name}"
                        )
                        return

            self.status_label.setText(f"Saved: {os.path.basename(dest)}")

        except Exception:
            print(traceback.format_exc())
            self.status_label.setText("Save failed — see console")

    # ── viewer ────────────────────────────────────────────────────
    def _show_placeholder(self):
        html = (PLACEHOLDER_HTML
                .replace("__BG__", BG)
                .replace("__THREE_SCRIPTS__", _build_three_scripts_block()))
        self.web_view.setHtml(html, QUrl("https://elto.local/"))

    def _display_model(self, obj_path):
        try:
            with open(obj_path, "r", encoding="utf-8") as f:
                obj_text = f.read()
            obj_b64    = base64.b64encode(obj_text.encode("utf-8")).decode("ascii")
            image_name = html_lib.escape(os.path.basename(self.current_image_path or "model"))
            html       = self._build_viewer_html(obj_b64, image_name)

            # Write to a temp HTML file and load via URL to avoid setHtml() size limits
            tmp_dir  = os.path.dirname(obj_path)           # same tmpdir as the model
            html_out = os.path.join(tmp_dir, "viewer.html")
            with open(html_out, "w", encoding="utf-8") as f:
                f.write(html)
            self.web_view.setUrl(QUrl.fromLocalFile(html_out))
        except Exception:
            print(traceback.format_exc())
            self.status_label.setText("Could not display model")

    def _display_model_glb(self, file_path: str, kind: str):
        """Display a GLB or GLTF file in the viewer using GLTFLoader.
        Reuses _build_gltf_viewer_html — same pipeline as Open .glb button.
        """
        try:
            mime = "model/gltf-binary" if kind == "glb" else "model/gltf+json"
            with open(file_path, "rb") as f:
                raw = f.read()
            b64_data = base64.b64encode(raw).decode("ascii")
            data_url = f"data:{mime};base64,{b64_data}"
            file_label = html_lib.escape(os.path.basename(file_path))
            html = self._build_gltf_viewer_html(data_url, file_label)

            # write to temp html next to the model file, load via file:// URL
            tmp_dir  = os.path.dirname(file_path)
            html_out = os.path.join(tmp_dir, "viewer_glb.html")
            with open(html_out, "w", encoding="utf-8") as f:
                f.write(html)
            self.web_view.setUrl(QUrl.fromLocalFile(html_out))
        except Exception:
            print(traceback.format_exc())
            self.status_label.setText("Could not display GLB model")

    def _build_viewer_html(self, obj_b64, image_name):
        template = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<style>
*{margin:0;padding:0;box-sizing:border-box;}
html,body{overflow:hidden;background:VBGV;color:VTEXTV;font-family:'Segoe UI',sans-serif;user-select:none;touch-action:none;}
.glass{
  background:rgba(14,14,18,0.60);
  backdrop-filter:blur(18px) saturate(200%);
  -webkit-backdrop-filter:blur(18px) saturate(200%);
  border:1px solid rgba(255,255,255,0.10);border-radius:12px;
}
#info{position:absolute;top:12px;left:110px;padding:6px 12px;z-index:20;}
#info h1{font-size:11px;margin:0 0 1px;color:VACCENTV;font-weight:700;letter-spacing:.4px;}
#info p{font-size:9px;color:VTEXT_DIMV;margin:0;}
#toolbar{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);display:flex;gap:6px;z-index:20;padding:6px 10px;align-items:center;}
#toolbar button{background:rgba(255,255,255,0.07);color:VTEXTV;border:1px solid rgba(255,255,255,0.13);border-radius:7px;padding:5px 13px;cursor:pointer;font-size:10px;font-weight:600;transition:background .15s,border-color .15s;white-space:nowrap;}
#toolbar button:hover{background:rgba(56,189,248,0.18);border-color:VACCENTV;color:VACCENTV;}
#toolbar button.active{background:rgba(56,189,248,0.22);border-color:VACCENTV;color:VACCENTV;}
#toolbar .sep{width:1px;height:20px;background:rgba(255,255,255,0.1);margin:0 2px;}
#btn-export-gl{color:#f6ad55;border-color:rgba(246,173,85,0.35);}
#btn-export-gl:hover{background:rgba(246,173,85,0.18)!important;border-color:#f6ad55!important;color:#f6ad55!important;}
#render-popup{display:none;position:absolute;bottom:60px;left:50%;transform:translateX(-50%);z-index:30;padding:8px 10px;flex-direction:column;gap:4px;min-width:170px;}
#render-popup.open{display:flex;}
#render-popup button{background:rgba(255,255,255,0.07);color:VTEXTV;border:1px solid rgba(255,255,255,0.13);border-radius:7px;padding:7px 14px;cursor:pointer;font-size:10px;font-weight:600;transition:background .15s;text-align:left;}
#render-popup button:hover{background:rgba(56,189,248,0.18);border-color:VACCENTV;color:VACCENTV;}
#menu{position:absolute;top:12px;right:12px;width:215px;z-index:20;padding:12px 10px;flex-direction:column;gap:2px;display:flex;transition:opacity .2s,transform .2s;}
#menu.hidden{opacity:0;pointer-events:none;transform:translateX(14px);}
.menu-section{margin-bottom:6px;}
.menu-label{font-size:8px;font-weight:700;letter-spacing:.8px;color:rgba(255,255,255,0.3);text-transform:uppercase;padding:0 4px 5px 4px;}
.menu-row{display:flex;align-items:center;justify-content:space-between;padding:5px 6px;border-radius:7px;cursor:pointer;transition:background .12s;gap:4px;}
.menu-row:hover{background:rgba(255,255,255,0.07);}
.menu-row>span:first-child{font-size:10px;color:VTEXTV;flex:1;}
.menu-row .val{font-size:9px;color:VTEXT_DIMV;font-variant-numeric:tabular-nums;min-width:28px;text-align:right;}
.menu-row input[type=range]{width:60px;height:3px;accent-color:VACCENTV;cursor:pointer;background:transparent;flex-shrink:0;}
.menu-row input[type=color]{width:22px;height:22px;border:none;background:none;cursor:pointer;padding:0;flex-shrink:0;}
.toggle{width:28px;height:15px;border-radius:8px;border:1px solid rgba(255,255,255,0.2);position:relative;background:rgba(255,255,255,0.08);cursor:pointer;flex-shrink:0;transition:background .2s;}
.toggle.on{background:VACCENTV;border-color:VACCENTV;}
.toggle::after{content:'';position:absolute;top:2px;left:2px;width:9px;height:9px;border-radius:50%;background:#fff;transition:transform .2s;}
.toggle.on::after{transform:translateX(13px);}
#wasd-hint{position:absolute;bottom:60px;right:14px;z-index:20;padding:7px 11px;font-size:9px;color:rgba(255,255,255,0.28);line-height:1.8;}
#load{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;z-index:30;color:VTEXT_DIMV;font-size:11px;padding:18px 24px;}
#load .bar{width:140px;height:2px;background:VBORDERV;border-radius:1px;overflow:hidden;margin-top:8px;}
#load .fill{width:30%;height:100%;background:VACCENTV;animation:pulse 1.1s ease-in-out infinite;}
@keyframes pulse{0%{transform:translateX(-100%);}100%{transform:translateX(400%);}}
#spc-hint{position:absolute;top:12px;left:50%;transform:translateX(-50%);z-index:20;padding:4px 12px;font-size:9px;color:rgba(255,255,255,0.35);display:none;}

/* ── Light Wheel ── */
#light-wheel{
  position:absolute;top:12px;left:12px;z-index:25;
  width:90px;height:90px;cursor:pointer;user-select:none;
}
#lw-canvas{display:block;}
#lw-label{
  text-align:center;font-size:8px;font-weight:700;letter-spacing:.6px;
  color:rgba(255,255,255,0.35);text-transform:uppercase;margin-top:2px;
}
</style></head>
<body>
<div id="info" class="glass"><h1>VAPP_NAMEV</h1><p>VIMAGE_NAMEV</p></div>

<div id="spc-hint" class="glass">Space + drag — pan camera</div>

<!-- ── Light Wheel — top-left draggable sun dial ── -->
<div id="light-wheel">
  <canvas id="lw-canvas" width="90" height="90"></canvas>
  <div id="lw-label">Light</div>
</div>

<div id="menu" class="glass">
  <div class="menu-section">
    <div class="menu-label">Light</div>
    <div class="menu-row"><span>Intensity</span><input type="range" id="sl-intensity" min="0" max="4" step="0.05" value="1.4"><span class="val" id="lbl-intensity">1.40</span></div>
    <div class="menu-row"><span>Color</span><input type="color" id="col-light" value="#ffffff"></div>
  </div>
  <div class="menu-section">
    <div class="menu-label">Texture</div>
    <div class="menu-row" id="btn-tex"><span>Add Texture</span><span class="val" style="color:VACCENTV">pick</span></div>
    <div class="menu-row" id="btn-clr-tex" style="display:none"><span>Clear Texture</span></div>
    <div class="menu-row"><span>Roughness</span><input type="range" id="sl-rough" min="0" max="1" step="0.02" value="0.75"><span class="val" id="lbl-rough">0.75</span></div>
    <div class="menu-row"><span>Metalness</span><input type="range" id="sl-metal" min="0" max="1" step="0.02" value="0.05"><span class="val" id="lbl-metal">0.05</span></div>
  </div>
  <div class="menu-section">
    <div class="menu-label">Subject &nbsp;<span style="font-size:7px;color:VTEXT_DIMV;font-weight:400;text-transform:none">L=move · R=rotate</span></div>
    <div class="menu-row"><span>Move Subject</span><div class="toggle" id="tog-move"></div></div>
    <div class="menu-row"><span>Auto Rotate</span><div class="toggle" id="tog-autorot"></div></div>
    <div class="menu-row"><span>Rot Speed</span><input type="range" id="sl-rotspeed" min="0.1" max="5" step="0.1" value="1"><span class="val" id="lbl-rotspeed">1.0</span></div>
  </div>
  <div class="menu-section">
    <div class="menu-label">Camera — WASD + Space</div>
    <div class="menu-row"><span>WASD Mode</span><div class="toggle on" id="tog-wasd"></div></div>
    <div class="menu-row"><span>Speed</span><input type="range" id="sl-camspeed" min="0.005" max="0.5" step="0.005" value="0.05"><span class="val" id="lbl-camspeed">0.05</span></div>
  </div>
  <div class="menu-section">
    <div class="menu-label">Mesh Tools</div>
    <div class="menu-row" id="btn-mt-smooth-lo"><span>Smooth (light)</span><span class="val" style="color:VACCENTV">apply</span></div>
    <div class="menu-row" id="btn-mt-smooth-hi"><span>Smooth (strong)</span><span class="val" style="color:VACCENTV">apply</span></div>
    <div class="menu-row">
      <span>Decimate</span>
      <input type="range" id="sl-mt-decimate" min="5" max="95" step="5" value="50">
      <span class="val" id="lbl-mt-decimate">50%</span>
    </div>
    <div class="menu-row" id="btn-mt-decimate"><span>Apply Decimate</span><span class="val" style="color:VACCENTV">apply</span></div>
    <div class="menu-row" id="btn-mt-reset"><span>Reset Mesh</span><span class="val" style="color:#f87171">reset</span></div>
  </div>
</div>

<div id="toolbar" class="glass">
  <button id="btn-wire">Wireframe</button>
  <button id="btn-reset">Reset View</button>
  <button id="btn-render">Render PNG</button>
  <div class="sep"></div>
  <button id="btn-export-gl">Export OBJ</button>
  <button id="btn-menu-tog" title="Show/Hide Panel">&#9776;</button>
</div>

<div id="render-popup" class="glass">
  <button id="rq-720">720p — Fast</button>
  <button id="rq-1080">1080p — Medium</button>
  <button id="rq-2k">2K — High</button>
  <button id="rq-4k">4K — Ultra</button>
</div>

<div id="wasd-hint" class="glass">
  W/S — forward/back &nbsp; A/D — strafe<br>
  Q/E — up/down &nbsp;&nbsp;&nbsp;&nbsp;&nbsp; Space+drag — pan
</div>

<div id="load" class="glass"><div>Loading model…</div><div class="bar"><div class="fill"></div></div></div>
<input type="file" id="fi-tex" accept="image/*" style="display:none">

VTHREE_SCRIPTSV
VWEBCHANNEL_SCRIPTV
<script>
(function(){
'use strict';

/* ── prevent browser zoom ── */
document.addEventListener('wheel',function(e){if(e.ctrlKey)e.preventDefault();},{passive:false});
document.addEventListener('keydown',function(e){if(e.ctrlKey&&(e.key==='+'||e.key==='-'||e.key==='='||e.key==='0'))e.preventDefault();});

var OBJ_B64="VOBJ_B64V";
function decodeObj(b64){
  var bin=atob(b64),bytes=new Uint8Array(bin.length);
  for(var i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);
  return new TextDecoder('utf-8').decode(bytes);
}

/* ── state ── */
var scene,camera,renderer,controls;
var currentObject=null,meshes=[];
var keyLight,fillLight,rimLight,hemi;
var lightRotOn=false,lightAngle=0;
var autoRotOn=false,autoRotSpeed=0.005;
var wireframeOn=false;
var moveSubjectOn=false;
var wasdOn=true,camSpeed=0.05;
var spaceDown=false;
var keys={};
var menuVisible=true,renderPopupOpen=false;
var _isDragL=false,_isDragR=false,_isSpcDrag=false;
var _prev={x:0,y:0};

/* ── init ── */
function init(){
  if(!window.THREE||!THREE.OrbitControls||!THREE.OBJLoader){
    document.getElementById('load').innerHTML='<div style="color:#f87171">three.js failed to load.</div>';return;
  }

  scene=new THREE.Scene();
  scene.background=new THREE.Color(VBG_HEXV);
  /* subtle fog to remove hard edge at background */
  scene.fog=new THREE.FogExp2(VBG_HEXV,0.04);

  camera=new THREE.PerspectiveCamera(45,innerWidth/innerHeight,0.01,500);
  camera.position.set(2,2,2);

  /* high-quality renderer */
  renderer=new THREE.WebGLRenderer({
    antialias:true,
    preserveDrawingBuffer:true,
    powerPreference:'high-performance'
  });
  renderer.setSize(innerWidth,innerHeight);
  renderer.setPixelRatio(Math.min(devicePixelRatio,3));
  renderer.physicallyCorrectLights=true;
  renderer.outputEncoding=THREE.sRGBEncoding;
  renderer.toneMapping=THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure=1.1;
  renderer.shadowMap.enabled=true;
  renderer.shadowMap.type=THREE.PCFSoftShadowMap;
  document.body.appendChild(renderer.domElement);

  controls=new THREE.OrbitControls(camera,renderer.domElement);
  controls.enableDamping=true;controls.dampingFactor=0.07;
  controls.minDistance=0.1;controls.maxDistance=200;

  /* ── 3-point lighting setup ── */
  hemi=new THREE.HemisphereLight(0xffffff,0x303040,0.5);
  scene.add(hemi);

  keyLight=new THREE.DirectionalLight(0xfff8f0,1.8);
  keyLight.position.set(4,6,3);
  keyLight.castShadow=true;
  keyLight.shadow.mapSize.width=2048;
  keyLight.shadow.mapSize.height=2048;
  keyLight.shadow.camera.near=0.1;
  keyLight.shadow.camera.far=50;
  keyLight.shadow.bias=-0.0005;
  keyLight.shadow.normalBias=0.02;
  scene.add(keyLight);

  fillLight=new THREE.DirectionalLight(0xe8f0ff,0.6);
  fillLight.position.set(-4,-1,-3);
  scene.add(fillLight);

  rimLight=new THREE.DirectionalLight(0xffffff,0.4);
  rimLight.position.set(0,3,-5);
  scene.add(rimLight);

  /* ground plane to receive shadows */
  var ground=new THREE.Mesh(
    new THREE.PlaneGeometry(20,20),
    new THREE.ShadowMaterial({opacity:0.18})
  );
  ground.rotation.x=-Math.PI/2;ground.position.y=-0.01;
  ground.receiveShadow=true;scene.add(ground);

  var grid=new THREE.GridHelper(6,24,0x2a2a2a,0x1a1a1a);
  grid.position.y=-0.005;scene.add(grid);

  loadModel();animate();bindUI();
  addEventListener('resize',onResize);
  addEventListener('keydown',onKey);
  addEventListener('keyup',onKeyUp);
}

/* ── load OBJ ── */
function frameObject(o){
  var box=new THREE.Box3().setFromObject(o);
  var sz=box.getSize(new THREE.Vector3());
  var c=box.getCenter(new THREE.Vector3());
  o.position.sub(c);
  var m=Math.max(sz.x,sz.y,sz.z)||1,d=m*2.0;
  camera.near=m/200;camera.far=m*80;
  camera.position.set(d*0.8,d*0.6,d*0.8);
  camera.updateProjectionMatrix();
  controls.target.set(0,0,0);controls.update();
}

function loadModel(){
  try{
    var obj=new THREE.OBJLoader().parse(decodeObj(OBJ_B64));
    meshes=[];
    obj.traverse(function(c){
      if(c.isMesh){
        c.geometry.computeVertexNormals();
        c.geometry.computeBoundingBox();
        c.castShadow=true;c.receiveShadow=true;
        c.material=new THREE.MeshStandardMaterial({
          color:0x8ab4c2,
          metalness:0.05,
          roughness:0.65,
          envMapIntensity:0.8,
          side:THREE.DoubleSide
        });
        meshes.push(c);
      }
    });
    /* TripoSR exports Z-up OBJ — rotate geometry -90° around X to get Y-up.
       Done per-geometry so normals stay correct and bbox is accurate. */
    obj.traverse(function(c){
      if(c.isMesh){
        c.geometry.applyMatrix4(new THREE.Matrix4().makeRotationX(-Math.PI/2));
        c.geometry.computeVertexNormals();
      }
    });
    frameObject(obj);
    /* sit model on ground — shift so bbox bottom touches y=0 */
    var bbox=new THREE.Box3().setFromObject(obj);
    obj.position.y += -bbox.min.y;
    currentObject=obj;
    scene.add(obj);
    document.getElementById('load').style.display='none';
  }catch(e){
    document.getElementById('load').innerHTML='<div style="color:#f87171">Failed: '+e.message+'</div>';
  }
}

/* ── keyboard ── */
function onKey(e){
  keys[e.code]=true;
  if(e.code==='Space'){spaceDown=true;e.preventDefault();}
}
function onKeyUp(e){
  keys[e.code]=false;
  if(e.code==='Space')spaceDown=false;
}

/* ── WASD ── */
var _dir=new THREE.Vector3(),_right=new THREE.Vector3(),_up=new THREE.Vector3(0,1,0);
function applyWASD(){
  if(!wasdOn)return;
  camera.getWorldDirection(_dir);_right.crossVectors(_dir,_up).normalize();
  var s=camSpeed;
  if(keys['KeyW']||keys['ArrowUp'])   {camera.position.addScaledVector(_dir, s);controls.target.addScaledVector(_dir, s);}
  if(keys['KeyS']||keys['ArrowDown']) {camera.position.addScaledVector(_dir,-s);controls.target.addScaledVector(_dir,-s);}
  if(keys['KeyA']||keys['ArrowLeft']) {camera.position.addScaledVector(_right,-s);controls.target.addScaledVector(_right,-s);}
  if(keys['KeyD']||keys['ArrowRight']){camera.position.addScaledVector(_right, s);controls.target.addScaledVector(_right, s);}
  if(keys['KeyQ']){camera.position.y+=s;controls.target.y+=s;}
  if(keys['KeyE']){camera.position.y-=s;controls.target.y-=s;}
}

/* ── Light Wheel — draggable sun-dial in top-left ── */
function initLightWheel(){
  var canvas=document.getElementById('lw-canvas');
  if(!canvas)return;
  var ctx=canvas.getContext('2d');
  var W=90,H=90,cx=W/2,cy=H/2,R=32,rDot=7;
  /* azimuth in XZ plane (0=front), elevation fixed at 50° */
  var _az=Math.PI*0.25;   /* initial angle */
  var _el=Math.PI/3;      /* ~60° elevation */
  var _dragging=false;

  function _dotPos(){
    return {x:cx+Math.cos(_az)*R, y:cy-Math.sin(_az)*R*0.5};
  }

  function _draw(){
    ctx.clearRect(0,0,W,H);
    /* outer ring */
    ctx.beginPath();ctx.arc(cx,cy,R,0,Math.PI*2);
    ctx.strokeStyle='rgba(255,255,255,0.12)';ctx.lineWidth=1.5;ctx.stroke();
    /* tick marks every 45° */
    for(var t=0;t<8;t++){
      var ta=t*Math.PI/4;
      var x1=cx+Math.cos(ta)*(R-4),y1=cy-Math.sin(ta)*(R-4)*0.5;
      var x2=cx+Math.cos(ta)*(R+1),y2=cy-Math.sin(ta)*(R+1)*0.5;
      ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);
      ctx.strokeStyle='rgba(255,255,255,0.18)';ctx.lineWidth=1;ctx.stroke();
    }
    /* line from center to dot */
    var dp=_dotPos();
    ctx.beginPath();ctx.moveTo(cx,cy);ctx.lineTo(dp.x,dp.y);
    ctx.strokeStyle='rgba(56,189,248,0.5)';ctx.lineWidth=1.5;ctx.stroke();
    /* center cross */
    ctx.beginPath();ctx.arc(cx,cy,3,0,Math.PI*2);
    ctx.fillStyle='rgba(255,255,255,0.25)';ctx.fill();
    /* sun dot */
    var grad=ctx.createRadialGradient(dp.x,dp.y,0,dp.x,dp.y,rDot);
    grad.addColorStop(0,'#fffbe0');grad.addColorStop(0.5,'#f6ad55');grad.addColorStop(1,'rgba(246,173,85,0)');
    ctx.beginPath();ctx.arc(dp.x,dp.y,rDot,0,Math.PI*2);
    ctx.fillStyle=grad;ctx.fill();
    ctx.beginPath();ctx.arc(dp.x,dp.y,rDot-1,0,Math.PI*2);
    ctx.strokeStyle='rgba(255,200,80,0.9)';ctx.lineWidth=1.5;ctx.stroke();
  }

  function _apply(){
    var dist=7;
    keyLight.position.set(
      dist*Math.cos(_el)*Math.cos(_az),
      dist*Math.sin(_el),
      dist*Math.cos(_el)*Math.sin(_az)
    );
    _draw();
  }

  function _posToAngle(ex,ey){
    return Math.atan2(-(ey-cy)*2,ex-cx);
  }

  canvas.addEventListener('mousedown',function(e){
    var r=canvas.getBoundingClientRect();
    var dp=_dotPos();
    var mx=e.clientX-r.left,my=e.clientY-r.top;
    if(Math.hypot(mx-dp.x,my-dp.y)<=rDot+6){_dragging=true;e.stopPropagation();}
  });
  window.addEventListener('mousemove',function(e){
    if(!_dragging)return;
    var r=canvas.getBoundingClientRect();
    _az=_posToAngle(e.clientX-r.left,e.clientY-r.top);
    _apply();
  });
  window.addEventListener('mouseup',function(){_dragging=false;});

  /* scroll on wheel to change elevation */
  canvas.addEventListener('wheel',function(e){
    e.preventDefault();
    _el=Math.max(0.05,Math.min(Math.PI/2-0.05,_el-e.deltaY*0.005));
    _apply();
  },{passive:false});

  _apply();   /* set initial position */
}

/* ── mouse drag (subject move + spacebar pan) ── */
function setupDrag(){
  var el=renderer.domElement;
  el.addEventListener('contextmenu',function(e){e.preventDefault();});

  el.addEventListener('mousedown',function(e){
    _prev={x:e.clientX,y:e.clientY};
    if(spaceDown){
      /* Space + LMB = pan camera regardless of mode */
      _isSpcDrag=true;
      controls.enabled=false;
      el.style.cursor='grabbing';
      return;
    }
    if(moveSubjectOn){
      if(e.button===0){_isDragL=true;controls.enabled=false;el.style.cursor='grabbing';}
      if(e.button===2){_isDragR=true;controls.enabled=false;}
    }
  });

  el.addEventListener('mousemove',function(e){
    var dx=(e.clientX-_prev.x);
    var dy=(e.clientY-_prev.y);
    _prev={x:e.clientX,y:e.clientY};

    if(_isSpcDrag){
      /* pan: move camera + target sideways/up */
      camera.getWorldDirection(_dir);_right.crossVectors(_dir,_up).normalize();
      var panS=camSpeed*0.5;
      camera.position.addScaledVector(_right,-dx*panS*0.05);
      controls.target.addScaledVector(_right,-dx*panS*0.05);
      camera.position.y+=dy*panS*0.05;
      controls.target.y+=dy*panS*0.05;
      return;
    }

    if(!currentObject)return;
    if(_isDragL){
      /* translate on camera-plane */
      camera.getWorldDirection(_dir);_right.crossVectors(_dir,_up).normalize();
      var ts=0.003;
      currentObject.position.addScaledVector(_right,dx*ts);
      currentObject.position.y-=dy*ts;
    }else if(_isDragR){
      /* rotate freely: dx=horizontal spin, dy=tilt — simple and reliable */
      currentObject.rotation.y += dx * 0.01;
      currentObject.rotation.x += dy * 0.01;
    }
  });

  el.addEventListener('mouseup',function(e){
    if(e.button===0){_isDragL=false;_isSpcDrag=false;}
    if(e.button===2){_isDragR=false;}
    if(!_isDragL&&!_isSpcDrag)controls.enabled=!moveSubjectOn||true;
    if(moveSubjectOn)controls.enabled=false;
    el.style.cursor=moveSubjectOn?'grab':'default';
  });
}

/* ── WebChannel bridge ── */
var _bridge=null;
function _initBridge(cb){
  if(_bridge){cb(_bridge);return;}
  if(typeof QWebChannel==='undefined'){
    setTimeout(function(){_initBridge(cb);},50);return;
  }
  new QWebChannel(qt.webChannelTransport,function(ch){
    _bridge=ch.objects.bridge;cb(_bridge);
  });
}

/* ── render PNG — signal Python to grab the widget ── */
function renderPNG(w,h){
  _initBridge(function(b){b.saveFile('png','',w+'x'+h);});
}

function closeRenderPopup(){
  document.getElementById('render-popup').classList.remove('open');
  document.getElementById('btn-render').classList.remove('active');
  renderPopupOpen=false;
}

function exportOBJ(){
  _initBridge(function(b){b.saveFile('obj',decodeObj(OBJ_B64),'');});
}

/* ── animate ── */
function animate(){
  requestAnimationFrame(animate);
  applyWASD();
  if(lightRotOn){
    lightAngle+=0.008;
    var r=6;
    keyLight.position.set(Math.cos(lightAngle)*r,5,Math.sin(lightAngle)*r);
  }
  if(autoRotOn&&currentObject)currentObject.rotation.y+=autoRotSpeed;
  if(!moveSubjectOn&&!_isSpcDrag)controls.update();
  renderer.render(scene,camera);
}

/* ── helpers ── */
function toggleWireframe(){
  if(!currentObject)return;
  wireframeOn=!wireframeOn;
  document.getElementById('btn-wire').classList.toggle('active',wireframeOn);
  meshes.forEach(function(m){m.material.wireframe=wireframeOn;});
}
function resetCamera(){if(currentObject)frameObject(currentObject);else controls.reset();}
function setToggle(id,v){document.getElementById(id).classList.toggle('on',v);}
function onResize(){
  camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();
  renderer.setSize(innerWidth,innerHeight);
}

/* ── texture: load via Image with correct UV settings ── */
function applyTexture(file){
  var reader=new FileReader();
  reader.onload=function(ev){
    var img=new Image();
    img.onload=function(){
      var tex=new THREE.Texture(img);
      tex.encoding=THREE.sRGBEncoding;
      tex.flipY=true;                          /* OBJ UVs are bottom-up */
      tex.wrapS=THREE.RepeatWrapping;
      tex.wrapT=THREE.RepeatWrapping;
      tex.minFilter=THREE.LinearMipMapLinearFilter;
      tex.magFilter=THREE.LinearFilter;
      tex.anisotropy=renderer.capabilities.getMaxAnisotropy();
      tex.needsUpdate=true;
      meshes.forEach(function(m){
        m.material.map=tex;
        m.material.color.set(0xffffff);        /* clear base color tint */
        m.material.needsUpdate=true;
      });
    };
    img.src=ev.target.result;
  };
  reader.readAsDataURL(file);
}
function clearTexture(){
  meshes.forEach(function(m){
    m.material.map=null;
    m.material.color.set(0x8ab4c2);
    m.material.needsUpdate=true;
  });
  document.getElementById('btn-clr-tex').style.display='none';
}

/* ── bind UI ── */
function bindUI(){
  setupDrag();
  initLightWheel();

  document.getElementById('btn-wire').addEventListener('click',toggleWireframe);
  document.getElementById('btn-reset').addEventListener('click',resetCamera);
  document.getElementById('btn-export-gl').addEventListener('click',exportOBJ);

  /* menu toggle */
  document.getElementById('btn-menu-tog').addEventListener('click',function(){
    menuVisible=!menuVisible;
    document.getElementById('menu').classList.toggle('hidden',!menuVisible);
    this.classList.toggle('active',menuVisible);
  });

  /* render popup */
  var popup=document.getElementById('render-popup');
  document.getElementById('btn-render').addEventListener('click',function(e){
    e.stopPropagation();renderPopupOpen=!renderPopupOpen;
    popup.classList.toggle('open',renderPopupOpen);
    this.classList.toggle('active',renderPopupOpen);
  });
  document.getElementById('rq-720') .addEventListener('click',function(){renderPNG(1280,720); closeRenderPopup();});
  document.getElementById('rq-1080').addEventListener('click',function(){renderPNG(1920,1080);closeRenderPopup();});
  document.getElementById('rq-2k')  .addEventListener('click',function(){renderPNG(2560,1440);closeRenderPopup();});
  document.getElementById('rq-4k')  .addEventListener('click',function(){renderPNG(3840,2160);closeRenderPopup();});
  document.addEventListener('click',function(e){
    if(renderPopupOpen&&!popup.contains(e.target)&&e.target.id!=='btn-render')closeRenderPopup();
  });

  /* intensity */
  document.getElementById('sl-intensity').addEventListener('input',function(){
    var v=+this.value;keyLight.intensity=v;fillLight.intensity=v*0.35;rimLight.intensity=v*0.22;
    document.getElementById('lbl-intensity').textContent=v.toFixed(2);
  });
  /* color */
  document.getElementById('col-light').addEventListener('input',function(){
    keyLight.color.set(this.value);fillLight.color.set(this.value);
  });

  /* intensity */
  /* roughness */
  document.getElementById('sl-rough').addEventListener('input',function(){
    var v=+this.value;document.getElementById('lbl-rough').textContent=v.toFixed(2);
    meshes.forEach(function(m){m.material.roughness=v;m.material.needsUpdate=true;});
  });
  /* metalness */
  document.getElementById('sl-metal').addEventListener('input',function(){
    var v=+this.value;document.getElementById('lbl-metal').textContent=v.toFixed(2);
    meshes.forEach(function(m){m.material.metalness=v;m.material.needsUpdate=true;});
  });
  /* texture */
  document.getElementById('btn-tex').addEventListener('click',function(){document.getElementById('fi-tex').click();});
  document.getElementById('fi-tex').addEventListener('change',function(){
    if(!this.files[0])return;
    applyTexture(this.files[0]);
    document.getElementById('btn-clr-tex').style.display='flex';
    this.value='';
  });
  document.getElementById('btn-clr-tex').addEventListener('click',clearTexture);
  /* move subject */
  document.getElementById('tog-move').addEventListener('click',function(){
    moveSubjectOn=!moveSubjectOn;setToggle('tog-move',moveSubjectOn);
    controls.enabled=!moveSubjectOn;
    renderer.domElement.style.cursor=moveSubjectOn?'grab':'default';
    _isDragL=false;_isDragR=false;
  });
  /* auto rotate */
  document.getElementById('tog-autorot').addEventListener('click',function(){autoRotOn=!autoRotOn;setToggle('tog-autorot',autoRotOn);});
  /* rot speed */
  document.getElementById('sl-rotspeed').addEventListener('input',function(){
    autoRotSpeed=+this.value*0.005;document.getElementById('lbl-rotspeed').textContent=(+this.value).toFixed(1);
  });
  /* WASD */
  document.getElementById('tog-wasd').addEventListener('click',function(){
    wasdOn=!wasdOn;setToggle('tog-wasd',wasdOn);
    document.getElementById('wasd-hint').style.opacity=wasdOn?'1':'0.3';
  });
  /* cam speed */
  document.getElementById('sl-camspeed').addEventListener('input',function(){
    camSpeed=+this.value;document.getElementById('lbl-camspeed').textContent=(+this.value).toFixed(3);
  });

  /* spacebar visual hint */
  addEventListener('keydown',function(e){if(e.code==='Space'){document.getElementById('spc-hint').style.display='block';}});
  addEventListener('keyup',  function(e){if(e.code==='Space'){document.getElementById('spc-hint').style.display='none';}});

  /* ── Mesh Tools ── */
  /* store original geometry per mesh so we can reset */
  var _origGeoms=[];
  function _backupGeoms(){
    if(_origGeoms.length)return;   /* backup only once */
    meshes.forEach(function(m){
      _origGeoms.push(m.geometry.clone());
    });
  }

  /* Laplacian smooth approximation in Three.js (position averaging) */
  function _smoothMesh(iterations){
    if(!currentObject||!meshes.length)return;
    _backupGeoms();
    meshes.forEach(function(m){
      var pos=m.geometry.attributes.position;
      if(!pos)return;
      var idx=m.geometry.index;
      for(var it=0;it<iterations;it++){
        /* build adjacency sum */
        var newPos=new Float32Array(pos.count*3);
        var cnt=new Int32Array(pos.count);
        var addEdge=function(a,b){
          newPos[a*3  ]+=pos.getX(b);newPos[a*3+1]+=pos.getY(b);newPos[a*3+2]+=pos.getZ(b);cnt[a]++;
          newPos[b*3  ]+=pos.getX(a);newPos[b*3+1]+=pos.getY(a);newPos[b*3+2]+=pos.getZ(a);cnt[b]++;
        };
        if(idx){
          for(var i=0;i<idx.count;i+=3){
            var a=idx.getX(i),b=idx.getX(i+1),c=idx.getX(i+2);
            addEdge(a,b);addEdge(b,c);addEdge(c,a);
          }
        }
        var lamb=0.5;
        for(var v=0;v<pos.count;v++){
          if(cnt[v]>0){
            pos.setXYZ(v,
              pos.getX(v)*(1-lamb)+newPos[v*3  ]/cnt[v]*lamb,
              pos.getY(v)*(1-lamb)+newPos[v*3+1]/cnt[v]*lamb,
              pos.getZ(v)*(1-lamb)+newPos[v*3+2]/cnt[v]*lamb
            );
          }
        }
        pos.needsUpdate=true;
      }
      m.geometry.computeVertexNormals();
    });
  }

  /* Simple decimate: merge nearby vertices (vertex clustering approximation) */
  function _decimateMesh(ratio){
    if(!currentObject||!meshes.length)return;
    _backupGeoms();
    /* We can't do true quadric decimation in plain Three.js without a library,
       so we apply a visible reduction via merging by rounding vertex positions */
    meshes.forEach(function(m){
      var pos=m.geometry.attributes.position;
      if(!pos)return;
      var box=new THREE.Box3().setFromBufferAttribute(pos);
      var sz=box.getSize(new THREE.Vector3());
      var gridSize=Math.max(sz.x,sz.y,sz.z)*(1-ratio)*0.5+0.001;
      /* round positions to grid */
      for(var v=0;v<pos.count;v++){
        pos.setXYZ(v,
          Math.round(pos.getX(v)/gridSize)*gridSize,
          Math.round(pos.getY(v)/gridSize)*gridSize,
          Math.round(pos.getZ(v)/gridSize)*gridSize
        );
      }
      pos.needsUpdate=true;
      m.geometry.computeVertexNormals();
    });
  }

  function _resetMesh(){
    if(!_origGeoms.length||!meshes.length)return;
    meshes.forEach(function(m,i){
      if(_origGeoms[i]){
        m.geometry.dispose();
        m.geometry=_origGeoms[i].clone();
        m.geometry.computeVertexNormals();
      }
    });
    _origGeoms=[];
  }

  document.getElementById('btn-mt-smooth-lo').addEventListener('click',function(){_smoothMesh(2);});
  document.getElementById('btn-mt-smooth-hi').addEventListener('click',function(){_smoothMesh(8);});
  document.getElementById('sl-mt-decimate').addEventListener('input',function(){
    document.getElementById('lbl-mt-decimate').textContent=this.value+'%';
  });
  document.getElementById('btn-mt-decimate').addEventListener('click',function(){
    var r=document.getElementById('sl-mt-decimate').value/100;
    _decimateMesh(r);
  });
  document.getElementById('btn-mt-reset').addEventListener('click',_resetMesh);
}

init();
})();
</script>
</body></html>"""
        return (template
                .replace("VTHREE_SCRIPTSV", _build_three_scripts_block())
                .replace("VWEBCHANNEL_SCRIPTV", _build_webchannel_script())
                .replace("VBGV",        BG)
                .replace("VBG_HEXV",    "0x" + BG.lstrip("#"))
                .replace("VTEXTV",      TEXT)
                .replace("VTEXT_DIMV",  TEXT_DIM)
                .replace("VFAINTV",     TEXT_FAINT)
                .replace("VBORDERV",    BORDER)
                .replace("VRAISEDV",    BG_RAISED)
                .replace("VACCENTV",    ACCENT)
                .replace("VAPP_NAMEV",  APP_NAME)
                .replace("VIMAGE_NAMEV", image_name)
                .replace("VOBJ_B64V",   obj_b64))

    # ── export ────────────────────────────────────────────────────
    def export_model(self):
        if not self.mesh_paths.get("obj"):
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export 3D Model", "model.glb",
            "GLB Files (*.glb);;OBJ Files (*.obj);;glTF Files (*.gltf);;All files (*.*)"
        )
        if not path:
            return
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext not in ("glb", "obj", "gltf"):
            ext = "glb"
        source = self.mesh_paths.get(ext)
        if not source:
            self.status_label.setText(f".{ext} was not generated")
            return
        try:
            shutil.copy2(source, path)
            self.status_label.setText(f"Exported: {os.path.basename(path)}")
        except Exception:
            print(traceback.format_exc())
            self.status_label.setText("Export failed")

    # ── open GLB / GLTF inside viewer ────────────────────────────
    def open_mesh_file(self, kind):
        existing  = self.mesh_paths.get(kind, "")
        start_dir = os.path.dirname(existing) if existing else ""
        filter_str = ("GLB Files (*.glb);;All files (*.*)" if kind == "glb"
                      else "glTF Files (*.gltf);;All files (*.*)")
        path, _ = QFileDialog.getOpenFileName(
            self, f"Open .{kind} file", start_dir, filter_str
        )
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "rb") as f:
                raw = f.read()
            b64_data   = base64.b64encode(raw).decode("ascii")
            mime       = "model/gltf-binary" if kind == "glb" else "model/gltf+json"
            data_url   = f"data:{mime};base64,{b64_data}"
            file_label = html_lib.escape(os.path.basename(path))
            html       = self._build_gltf_viewer_html(data_url, file_label)

            # Write to temp file and load via URL to avoid setHtml() size limits
            tmp_html = os.path.join(tempfile.gettempdir(), "elto_gltf_viewer.html")
            with open(tmp_html, "w", encoding="utf-8") as f:
                f.write(html)
            self.web_view.setUrl(QUrl.fromLocalFile(tmp_html))
            self.status_label.setText(f"Viewing: {os.path.basename(path)}")
        except Exception:
            print(traceback.format_exc())
            self.status_label.setText("Could not open file for viewing")

    def _build_gltf_viewer_html(self, data_url, file_label):
        gltf_loader_local = _read_vendor_file("GLTFLoader.js")
        if gltf_loader_local:
            gltf_loader_src = "<script>" + gltf_loader_local + "</script>"
        else:
            base = "https://unpkg.com/three@0.128.0"
            gltf_loader_src = (
                f'<script src="{base}/examples/js/loaders/GLTFLoader.js"></script>'
            )
        three_block = _build_three_scripts_block()
        template = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<style>
*{margin:0;padding:0;box-sizing:border-box;}
html,body{overflow:hidden;background:VBGV;color:VTEXTV;font-family:'Segoe UI',sans-serif;user-select:none;touch-action:none;}
.glass{
  background:rgba(14,14,18,0.58);
  backdrop-filter:blur(16px) saturate(180%);
  -webkit-backdrop-filter:blur(16px) saturate(180%);
  border:1px solid rgba(255,255,255,0.09);border-radius:12px;
}
#info{position:absolute;top:12px;left:110px;padding:6px 12px;z-index:20;}
#info h1{font-size:11px;margin:0 0 1px;color:VACCENTV;font-weight:700;letter-spacing:.4px;}
#info p{font-size:9px;color:VTEXT_DIMV;margin:0;}
/* ── Light Wheel ── */
#light-wheel{position:absolute;top:12px;left:12px;z-index:25;width:90px;height:90px;cursor:pointer;user-select:none;}
#lw-canvas{display:block;}
#lw-label{text-align:center;font-size:8px;font-weight:700;letter-spacing:.6px;color:rgba(255,255,255,0.35);text-transform:uppercase;margin-top:2px;}
#toolbar{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);display:flex;gap:6px;z-index:20;padding:6px 10px;align-items:center;}
#toolbar button{background:rgba(255,255,255,0.07);color:VTEXTV;border:1px solid rgba(255,255,255,0.13);border-radius:7px;padding:5px 13px;cursor:pointer;font-size:10px;font-weight:600;transition:background .15s,border-color .15s;white-space:nowrap;}
#toolbar button:hover{background:rgba(56,189,248,0.18);border-color:VACCENTV;color:VACCENTV;}
#toolbar button.active{background:rgba(56,189,248,0.22);border-color:VACCENTV;color:VACCENTV;}
#toolbar .sep{width:1px;height:20px;background:rgba(255,255,255,0.1);margin:0 2px;}
#btn-export-gl{color:#f6ad55;border-color:rgba(246,173,85,0.35);}
#btn-export-gl:hover{background:rgba(246,173,85,0.18)!important;border-color:#f6ad55!important;color:#f6ad55!important;}
#render-popup{display:none;position:absolute;bottom:60px;left:50%;transform:translateX(-50%);z-index:30;padding:8px 10px;flex-direction:column;gap:4px;min-width:160px;}
#render-popup.open{display:flex;}
#render-popup button{background:rgba(255,255,255,0.07);color:VTEXTV;border:1px solid rgba(255,255,255,0.13);border-radius:7px;padding:6px 12px;cursor:pointer;font-size:10px;font-weight:600;transition:background .15s;text-align:left;}
#render-popup button:hover{background:rgba(56,189,248,0.18);border-color:VACCENTV;color:VACCENTV;}
#menu{position:absolute;top:12px;right:12px;width:210px;z-index:20;padding:12px 10px;flex-direction:column;gap:2px;display:flex;transition:opacity .2s,transform .2s;}
#menu.hidden{opacity:0;pointer-events:none;transform:translateX(12px);}
.menu-section{margin-bottom:6px;}
.menu-label{font-size:8px;font-weight:700;letter-spacing:.8px;color:rgba(255,255,255,0.3);text-transform:uppercase;padding:0 4px 4px 4px;}
.menu-row{display:flex;align-items:center;justify-content:space-between;padding:5px 6px;border-radius:7px;cursor:pointer;transition:background .12s;}
.menu-row:hover{background:rgba(255,255,255,0.07);}
.menu-row span{font-size:10px;color:VTEXTV;}
.menu-row .val{font-size:9px;color:VTEXT_DIMV;font-variant-numeric:tabular-nums;min-width:26px;text-align:right;}
.menu-row input[type=range]{width:64px;height:3px;accent-color:VACCENTV;cursor:pointer;background:transparent;}
.menu-row input[type=color]{width:22px;height:22px;border:none;background:none;cursor:pointer;padding:0;}
.toggle{width:28px;height:15px;border-radius:8px;border:1px solid rgba(255,255,255,0.2);position:relative;background:rgba(255,255,255,0.08);cursor:pointer;flex-shrink:0;transition:background .2s;}
.toggle.on{background:VACCENTV;border-color:VACCENTV;}
.toggle::after{content:'';position:absolute;top:2px;left:2px;width:9px;height:9px;border-radius:50%;background:#fff;transition:transform .2s;}
.toggle.on::after{transform:translateX(13px);}
#wasd-hint{position:absolute;bottom:60px;right:14px;z-index:20;padding:6px 10px;font-size:9px;color:rgba(255,255,255,0.25);line-height:1.7;}
#load{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;z-index:30;color:VTEXT_DIMV;font-size:11px;padding:18px 24px;}
#load .bar{width:140px;height:2px;background:VBORDERV;border-radius:1px;overflow:hidden;margin-top:8px;}
#load .fill{width:30%;height:100%;background:VACCENTV;animation:pulse 1.1s ease-in-out infinite;}
@keyframes pulse{0%{transform:translateX(-100%);}100%{transform:translateX(400%);}}
</style></head>
<body>
<div id="info" class="glass"><h1>VAPP_NAMEV</h1><p>VFILE_LABELV</p></div>
<div id="light-wheel"><canvas id="lw-canvas" width="90" height="90"></canvas><div id="lw-label">Light</div></div>
<div id="menu" class="glass">
  <div class="menu-section">
    <div class="menu-label">Light</div>
    <div class="menu-row"><span>Light Rotation</span><div class="toggle" id="tog-light"></div></div>
    <div class="menu-row"><span>Intensity</span><input type="range" id="sl-intensity" min="0" max="3" step="0.05" value="1.4"><span class="val" id="lbl-intensity">1.40</span></div>
    <div class="menu-row"><span>Color</span><input type="color" id="col-light" value="#ffffff"></div>
  </div>
  <div class="menu-section">
    <div class="menu-label">Texture</div>
    <div class="menu-row" id="btn-tex"><span>Add Texture</span><span class="val">click</span></div>
    <div class="menu-row" id="btn-clr-tex" style="display:none"><span>Clear Texture</span></div>
    <div class="menu-row"><span>Roughness</span><input type="range" id="sl-rough" min="0" max="1" step="0.02" value="0.75"><span class="val" id="lbl-rough">0.75</span></div>
    <div class="menu-row"><span>Metalness</span><input type="range" id="sl-metal" min="0" max="1" step="0.02" value="0.05"><span class="val" id="lbl-metal">0.05</span></div>
  </div>
  <div class="menu-section">
    <div class="menu-label">Subject  <span style="font-size:8px;color:VTEXT_DIMV;font-weight:400;text-transform:none">(L=move · R=rotate)</span></div>
    <div class="menu-row"><span>Move Subject</span><div class="toggle" id="tog-move"></div></div>
    <div class="menu-row"><span>Auto Rotate</span><div class="toggle" id="tog-autorot"></div></div>
    <div class="menu-row"><span>Rot Speed</span><input type="range" id="sl-rotspeed" min="0.1" max="5" step="0.1" value="1"><span class="val" id="lbl-rotspeed">1.0</span></div>
  </div>
  <div class="menu-section">
    <div class="menu-label">Camera — WASD</div>
    <div class="menu-row"><span>WASD Mode</span><div class="toggle on" id="tog-wasd"></div></div>
    <div class="menu-row"><span>Speed</span><input type="range" id="sl-camspeed" min="0.01" max="0.5" step="0.01" value="0.08"><span class="val" id="lbl-camspeed">0.08</span></div>
  </div>
</div>
<div id="toolbar" class="glass">
  <button id="btn-wire">Wireframe</button>
  <button id="btn-reset">Reset View</button>
  <button id="btn-render">Render PNG</button>
  <div class="sep"></div>
  <button id="btn-export-gl">Export OBJ</button>
  <button id="btn-menu-tog" title="Show/Hide Panel">&#9776;</button>
</div>
<div id="render-popup" class="glass">
  <button id="rq-low">Low  (720p)</button>
  <button id="rq-med">Medium (1080p)</button>
  <button id="rq-high">High (2160p)</button>
</div>
<div id="wasd-hint" class="glass">W/S — forward/back &nbsp; A/D — strafe<br>Q/E — up/down</div>
<div id="load" class="glass"><div>Loading model…</div><div class="bar"><div class="fill"></div></div></div>
<input type="file" id="fi-tex" accept="image/*" style="display:none">
VTHREE_SCRIPTSV
VGLTF_LOADERV
VWEBCHANNEL_SCRIPTV
<script>
(function(){
'use strict';
document.addEventListener('wheel',function(e){if(e.ctrlKey)e.preventDefault();},{passive:false});
document.addEventListener('keydown',function(e){if(e.ctrlKey&&(e.key==='+'||e.key==='-'||e.key==='='||e.key==='0'))e.preventDefault();});

/* ── WebChannel bridge ── */
var _bridge=null;
function _initBridge(cb){
  if(_bridge){cb(_bridge);return;}
  if(typeof QWebChannel==='undefined'){setTimeout(function(){_initBridge(cb);},50);return;}
  new QWebChannel(qt.webChannelTransport,function(ch){_bridge=ch.objects.bridge;cb(_bridge);});
}

var DATA_URL="VDATA_URLV";
var scene,camera,renderer,controls,currentObject=null;
var keyLight,fillLight;
var wireframeOn=false,lightRotOn=false,lightAngle=0;
var autoRotOn=false,autoRotSpeed=0.005;
var moveSubjectOn=false,wasdOn=true,camSpeed=0.08;
var menuVisible=true,renderPopupOpen=false;
var keys={};
var _isDragL=false,_isDragR=false,_prev={x:0,y:0};

function init(){
  if(!window.THREE||!THREE.OrbitControls){
    document.getElementById('load').innerHTML='<div style="color:#f87171">three.js failed to load.</div>';return;
  }
  if(!THREE.GLTFLoader){
    document.getElementById('load').innerHTML='<div style="color:#f87171">GLTFLoader not available.</div>';return;
  }
  scene=new THREE.Scene();scene.background=new THREE.Color(VBG_HEXV);
  camera=new THREE.PerspectiveCamera(50,innerWidth/innerHeight,0.01,2000);
  camera.position.set(2,2,2);
  renderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});
  renderer.setSize(innerWidth,innerHeight);renderer.setPixelRatio(Math.min(devicePixelRatio,2));
  renderer.outputEncoding=THREE.sRGBEncoding;renderer.shadowMap.enabled=true;
  document.body.appendChild(renderer.domElement);
  controls=new THREE.OrbitControls(camera,renderer.domElement);
  controls.enableDamping=true;controls.dampingFactor=0.08;
  scene.add(new THREE.HemisphereLight(0xffffff,0x2a2a2a,0.6));
  keyLight=new THREE.DirectionalLight(0xffffff,1.4);keyLight.position.set(3,4,2);keyLight.castShadow=true;scene.add(keyLight);
  fillLight=new THREE.DirectionalLight(0xffffff,0.55);fillLight.position.set(-3,-1,-2);scene.add(fillLight);
  var grid=new THREE.GridHelper(4,20,0x2a2a2a,0x1a1a1a);grid.position.y=-0.001;scene.add(grid);
  loadModel();animate();bindUI();
  addEventListener('resize',onResize);
  addEventListener('keydown',function(e){keys[e.code]=true;});
  addEventListener('keyup',function(e){keys[e.code]=false;});
}

function frameObject(o){
  var box=new THREE.Box3().setFromObject(o);
  var sz=box.getSize(new THREE.Vector3());var c=box.getCenter(new THREE.Vector3());
  o.position.sub(c);var m=Math.max(sz.x,sz.y,sz.z)||1,d=m*1.7;
  camera.near=m/100;camera.far=m*50;camera.position.set(d,d*0.75,d);
  camera.updateProjectionMatrix();controls.target.set(0,0,0);controls.update();
}

function loadModel(){
  new THREE.GLTFLoader().load(DATA_URL,function(g){
    var obj=g.scene||g.scenes[0];
    frameObject(obj);currentObject=obj;scene.add(obj);
    document.getElementById('load').style.display='none';
  },undefined,function(e){
    document.getElementById('load').innerHTML='<div style="color:#f87171">Failed: '+(e.message||e)+'</div>';
  });
}

var _dir=new THREE.Vector3(),_right=new THREE.Vector3(),_up=new THREE.Vector3(0,1,0);
function applyWASD(){
  if(!wasdOn)return;
  camera.getWorldDirection(_dir);_right.crossVectors(_dir,_up).normalize();
  var s=camSpeed;
  if(keys['KeyW']||keys['ArrowUp'])   {camera.position.addScaledVector(_dir, s);controls.target.addScaledVector(_dir, s);}
  if(keys['KeyS']||keys['ArrowDown']) {camera.position.addScaledVector(_dir,-s);controls.target.addScaledVector(_dir,-s);}
  if(keys['KeyA']||keys['ArrowLeft']) {camera.position.addScaledVector(_right,-s);controls.target.addScaledVector(_right,-s);}
  if(keys['KeyD']||keys['ArrowRight']){camera.position.addScaledVector(_right, s);controls.target.addScaledVector(_right, s);}
  if(keys['KeyQ']){camera.position.y+=s;controls.target.y+=s;}
  if(keys['KeyE']){camera.position.y-=s;controls.target.y-=s;}
}

function setupSubjectDrag(){
  var el=renderer.domElement;
  el.addEventListener('contextmenu',function(e){e.preventDefault();});
  el.addEventListener('mousedown',function(e){
    if(!moveSubjectOn)return;
    if(e.button===0){_isDragL=true;controls.enabled=false;}
    if(e.button===2){_isDragR=true;controls.enabled=false;}
    _prev={x:e.clientX,y:e.clientY};
  });
  el.addEventListener('mousemove',function(e){
    if(!currentObject)return;
    var dx=(e.clientX-_prev.x)*0.005,dy=(e.clientY-_prev.y)*0.005;
    if(_isDragL){currentObject.position.x+=dx;currentObject.position.y-=dy;}
    else if(_isDragR){currentObject.rotation.y+=dx*2;currentObject.rotation.x+=dy*2;}
    _prev={x:e.clientX,y:e.clientY};
  });
  el.addEventListener('mouseup',function(e){
    if(e.button===0)_isDragL=false;if(e.button===2)_isDragR=false;
  });
}

function renderPNG(scale){
  var w=Math.round(innerWidth*scale),h=Math.round(innerHeight*scale);
  renderer.setSize(w,h);renderer.setPixelRatio(1);renderer.render(scene,camera);
  var url=renderer.domElement.toDataURL('image/png');
  renderer.setSize(innerWidth,innerHeight);renderer.setPixelRatio(Math.min(devicePixelRatio,2));
  _initBridge(function(b){b.saveFile('png',url,w+'x'+h);});
}

function toggleWireframe(){
  if(!currentObject)return;wireframeOn=!wireframeOn;
  document.getElementById('btn-wire').classList.toggle('active',wireframeOn);
  currentObject.traverse(function(c){if(c.isMesh)c.material.wireframe=wireframeOn;});
}
function resetCamera(){if(currentObject)frameObject(currentObject);else controls.reset();}
function setToggle(id,v){document.getElementById(id).classList.toggle('on',v);}
function onResize(){camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight);}

function animate(){
  requestAnimationFrame(animate);applyWASD();
  if(lightRotOn){lightAngle+=0.012;keyLight.position.set(Math.cos(lightAngle)*5,4,Math.sin(lightAngle)*5);}
  if(autoRotOn&&currentObject)currentObject.rotation.y+=autoRotSpeed;
  if(!moveSubjectOn)controls.update();
  renderer.render(scene,camera);
}

function applyTexture(url){
  if(!currentObject)return;
  var tex=new THREE.TextureLoader().load(url);
  currentObject.traverse(function(c){if(c.isMesh){c.material.map=tex;c.material.needsUpdate=true;}});
}
function clearTexture(){
  if(!currentObject)return;
  currentObject.traverse(function(c){if(c.isMesh){c.material.map=null;c.material.needsUpdate=true;}});
  document.getElementById('btn-clr-tex').style.display='none';
}

/* ── Light Wheel (same as OBJ viewer) ── */
function initLightWheel(){
  var canvas=document.getElementById('lw-canvas');
  if(!canvas)return;
  var ctx=canvas.getContext('2d');
  var W=90,H=90,cx=W/2,cy=H/2,R=32,rDot=7;
  var _az=Math.PI*0.25,_el=Math.PI/3,_dragging=false;
  function _dotPos(){return{x:cx+Math.cos(_az)*R,y:cy-Math.sin(_az)*R*0.5};}
  function _draw(){
    ctx.clearRect(0,0,W,H);
    ctx.beginPath();ctx.arc(cx,cy,R,0,Math.PI*2);
    ctx.strokeStyle='rgba(255,255,255,0.12)';ctx.lineWidth=1.5;ctx.stroke();
    for(var t=0;t<8;t++){var ta=t*Math.PI/4;ctx.beginPath();ctx.moveTo(cx+Math.cos(ta)*(R-4),cy-Math.sin(ta)*(R-4)*0.5);ctx.lineTo(cx+Math.cos(ta)*(R+1),cy-Math.sin(ta)*(R+1)*0.5);ctx.strokeStyle='rgba(255,255,255,0.18)';ctx.lineWidth=1;ctx.stroke();}
    var dp=_dotPos();
    ctx.beginPath();ctx.moveTo(cx,cy);ctx.lineTo(dp.x,dp.y);ctx.strokeStyle='rgba(56,189,248,0.5)';ctx.lineWidth=1.5;ctx.stroke();
    ctx.beginPath();ctx.arc(cx,cy,3,0,Math.PI*2);ctx.fillStyle='rgba(255,255,255,0.25)';ctx.fill();
    var grad=ctx.createRadialGradient(dp.x,dp.y,0,dp.x,dp.y,rDot);grad.addColorStop(0,'#fffbe0');grad.addColorStop(0.5,'#f6ad55');grad.addColorStop(1,'rgba(246,173,85,0)');
    ctx.beginPath();ctx.arc(dp.x,dp.y,rDot,0,Math.PI*2);ctx.fillStyle=grad;ctx.fill();
    ctx.beginPath();ctx.arc(dp.x,dp.y,rDot-1,0,Math.PI*2);ctx.strokeStyle='rgba(255,200,80,0.9)';ctx.lineWidth=1.5;ctx.stroke();
  }
  function _apply(){
    var dist=7;
    keyLight.position.set(dist*Math.cos(_el)*Math.cos(_az),dist*Math.sin(_el),dist*Math.cos(_el)*Math.sin(_az));
    _draw();
  }
  canvas.addEventListener('mousedown',function(e){var r=canvas.getBoundingClientRect();var dp=_dotPos();var mx=e.clientX-r.left,my=e.clientY-r.top;if(Math.hypot(mx-dp.x,my-dp.y)<=rDot+6){_dragging=true;e.stopPropagation();}});
  window.addEventListener('mousemove',function(e){if(!_dragging)return;var r=canvas.getBoundingClientRect();_az=Math.atan2(-(e.clientY-r.top-cy)*2,e.clientX-r.left-cx);_apply();});
  window.addEventListener('mouseup',function(){_dragging=false;});
  canvas.addEventListener('wheel',function(e){e.preventDefault();_el=Math.max(0.05,Math.min(Math.PI/2-0.05,_el-e.deltaY*0.005));_apply();},{passive:false});
  _apply();
}

function bindUI(){
  setupSubjectDrag();
  initLightWheel();
  document.getElementById('btn-wire').addEventListener('click',toggleWireframe);
  document.getElementById('btn-reset').addEventListener('click',resetCamera);
  document.getElementById('btn-export-gl').addEventListener('click',function(){
    _initBridge(function(b){b.saveFile('glb',DATA_URL,'');});
  });
  document.getElementById('btn-menu-tog').addEventListener('click',function(){
    menuVisible=!menuVisible;
    document.getElementById('menu').classList.toggle('hidden',!menuVisible);
    this.classList.toggle('active',menuVisible);
  });
  var popup=document.getElementById('render-popup');
  document.getElementById('btn-render').addEventListener('click',function(e){
    e.stopPropagation();renderPopupOpen=!renderPopupOpen;
    popup.classList.toggle('open',renderPopupOpen);this.classList.toggle('active',renderPopupOpen);
  });
  document.getElementById('rq-low') .addEventListener('click',function(){renderPNG(720 /innerHeight);popup.classList.remove('open');renderPopupOpen=false;});
  document.getElementById('rq-med') .addEventListener('click',function(){renderPNG(1080/innerHeight);popup.classList.remove('open');renderPopupOpen=false;});
  document.getElementById('rq-high').addEventListener('click',function(){renderPNG(2160/innerHeight);popup.classList.remove('open');renderPopupOpen=false;});
  document.addEventListener('click',function(){if(renderPopupOpen){popup.classList.remove('open');renderPopupOpen=false;document.getElementById('btn-render').classList.remove('active');}});
  document.getElementById('tog-light').addEventListener('click',function(){lightRotOn=!lightRotOn;setToggle('tog-light',lightRotOn);});
  document.getElementById('sl-intensity').addEventListener('input',function(){keyLight.intensity=+this.value;document.getElementById('lbl-intensity').textContent=(+this.value).toFixed(2);});
  document.getElementById('col-light').addEventListener('input',function(){keyLight.color.set(this.value);fillLight.color.set(this.value);});
  document.getElementById('sl-rough').addEventListener('input',function(){
    var v=+this.value;document.getElementById('lbl-rough').textContent=v.toFixed(2);
    if(currentObject)currentObject.traverse(function(c){if(c.isMesh){c.material.roughness=v;c.material.needsUpdate=true;}});
  });
  document.getElementById('sl-metal').addEventListener('input',function(){
    var v=+this.value;document.getElementById('lbl-metal').textContent=v.toFixed(2);
    if(currentObject)currentObject.traverse(function(c){if(c.isMesh){c.material.metalness=v;c.material.needsUpdate=true;}});
  });
  document.getElementById('btn-tex').addEventListener('click',function(){document.getElementById('fi-tex').click();});
  document.getElementById('fi-tex').addEventListener('change',function(){
    if(!this.files[0])return;applyTexture(URL.createObjectURL(this.files[0]));
    document.getElementById('btn-clr-tex').style.display='flex';
  });
  document.getElementById('btn-clr-tex').addEventListener('click',clearTexture);
  document.getElementById('tog-move').addEventListener('click',function(){
    moveSubjectOn=!moveSubjectOn;setToggle('tog-move',moveSubjectOn);
    controls.enabled=!moveSubjectOn;
    renderer.domElement.style.cursor=moveSubjectOn?'grab':'default';
    _isDragL=false;_isDragR=false;
  });
  document.getElementById('tog-autorot').addEventListener('click',function(){autoRotOn=!autoRotOn;setToggle('tog-autorot',autoRotOn);});
  document.getElementById('sl-rotspeed').addEventListener('input',function(){autoRotSpeed=+this.value*0.005;document.getElementById('lbl-rotspeed').textContent=(+this.value).toFixed(1);});
  document.getElementById('tog-wasd').addEventListener('click',function(){wasdOn=!wasdOn;setToggle('tog-wasd',wasdOn);document.getElementById('wasd-hint').style.opacity=wasdOn?'1':'0.3';});
  document.getElementById('sl-camspeed').addEventListener('input',function(){camSpeed=+this.value;document.getElementById('lbl-camspeed').textContent=(+this.value).toFixed(2);});
}
init();
})();
</script>
</body></html>"""
        return (template
                .replace("VTHREE_SCRIPTSV",      three_block)
                .replace("VGLTF_LOADERV",         gltf_loader_src)
                .replace("VWEBCHANNEL_SCRIPTV",   _build_webchannel_script())
                .replace("VBGV",             BG)
                .replace("VBG_HEXV",         "0x" + BG.lstrip("#"))
                .replace("VTEXTV",           TEXT)
                .replace("VTEXT_DIMV",       TEXT_DIM)
                .replace("VFAINTV",          TEXT_FAINT)
                .replace("VBORDERV",         BORDER)
                .replace("VRAISEDV",         BG_RAISED)
                .replace("VACCENTV",         ACCENT)
                .replace("VAPP_NAMEV",       APP_NAME)
                .replace("VFILE_LABELV",     file_label)
                .replace("VDATA_URLV",       data_url))

    # ── close ─────────────────────────────────────────────────────
    def closeEvent(self, event):
        """Gracefully stop all running threads before closing."""
        # Collect every thread that might be running
        workers = [self.model_loader, self.generator]

        for worker in workers:
            if worker is None:
                continue
            if worker.isRunning():
                worker.quit()           # ask thread's event-loop to exit
                if not worker.wait(5000):   # wait up to 5 s
                    worker.terminate()      # force-kill if still running
                    worker.wait(1000)

        # Disconnect signals to prevent callbacks firing after destruction
        try:
            if self.model_loader:
                self.model_loader.loaded.disconnect()
                self.model_loader.failed.disconnect()
        except Exception:
            pass
        try:
            if self.generator:
                self.generator.progress.disconnect()
                self.generator.finished_ok.disconnect()
                self.generator.failed.disconnect()
        except Exception:
            pass

        event.accept()


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

def _find_icon():
    for name in ("icon.ico", "icon.png"):
        path = os.path.join(SCRIPT_DIR, name)
        if os.path.exists(path):
            return path
    return None


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.App.1")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    icon_path = _find_icon()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))

    window = ElToWindow()
    if icon_path:
        window.setWindowIcon(QIcon(icon_path))
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
