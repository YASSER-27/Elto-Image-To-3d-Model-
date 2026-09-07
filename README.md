<img width="1500" height="500" alt="showcase" src="https://github.com/user-attachments/assets/ababe397-c05f-4fb2-920e-dfe9c6fefe51" />


# ElTo — Image to 3D Model Converter

ElTo is a desktop application built with Python and PySide6 that converts 2D images into textured 3D models using AI (TripoSR). 

It features an integrated 3D viewport powered by Three.js, automatic background removal, built-in mesh post-processing, and flexible export options.

---

## Features

- Fast AI Reconstruction: Powered by the TripoSR deep learning architecture.
- Interactive 3D Viewer: Built-in WebGL viewer using Three.js to orbit, pan, zoom, and inspect generated models.
- Built-in Image Editor: Free-form crop and position tuning before generating.
- Automatic Background Removal: Powered by rembg to separate subjects cleanly from backgrounds.
- Mesh Post-Processing: Built-in mesh decimation and Laplacian smoothing options.
- Multiple Export Formats: Export models directly to .obj, .glb, and .gltf formats.
- Flexible Input: Drag and drop images or paste directly from the clipboard (Ctrl + V).

<img width="1680" height="1011" alt="image" src="https://github.com/user-attachments/assets/496ecf9f-4380-4947-98f9-89229520c1d9" />


<img width="1680" height="1014" alt="image" src="https://github.com/user-attachments/assets/429c6759-f57a-4d85-8124-2ebfd794e57c" />


---

## Installation & Setup

### Prerequisites
Python 3.9 or higher and a CUDA-compatible GPU are recommended for optimal performance.

### 1. Clone the repository
```bash
git clone [https://github.com/YASSER-27/Elto-Image-To-3d-Model.git](https://github.com/YASSER-27/Elto-Image-To-3d-Model.git)
cd Elto-Image-To-3d-Model

```
2. Create a virtual environment

```Bash
python -m venv venv
# On Windows:
venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate
```
3. Install dependencies

```Bash
pip install -r requirements.txt
```
4. Download Model
```Bash
git lfs install
git clone https://huggingface.co/stabilityai/TripoSR
```
### Usage
Run the main application script:

```
python ElTo.py
```

-Import an image by clicking "Import Image", dropping an image into the preview area, or pasting from clipboard (Ctrl + V).

-Click "Edit" to crop or adjust the image if needed.

-Configure target settings and adjust decimate or smoothing sliders.

-Click "Generate 3D Model" to process the image.

-Export your final result using "Save OBJ", "Save GLB", or "Save GLTF".

| Format | Description |
| :--- | :--- |
| **.glb** | Binary format including vertex colors and textures. Ideal for web deployment and game engines. |
| **.gltf** | JSON-based 3D structure file. Easy to parse and edit. |
| **.obj** | Universal geometry format supported by Blender, Maya, 3ds Max, and other 3D software. |

#### MIT License.
