### Download Model

```Bash
git lfs install
git clone https://huggingface.co/stabilityai/TripoSR
```

### Model_Folder

```
model/
├── config.yaml
└── model.ckpt
```



### Elto_Folder
```
Elto_Folder/
├── ElTo.py
├── build
│   └── elto
│       ├── base_library.zip
│       └── qt.conf
├── dino_vitb16_config.json
|
|
├── elto
│   ├── system.cpython-311.pyc
│   ├── utils.cpython-311.pyc
│   ├── bake_texture.py
│   ├── models
│   │   │   ├── isosurface.cpython-311.pyc
│   │   │   ├── nerf_renderer.cpython-311.pyc
│   │   │   └── network_utils.cpython-311.pyc
│   │   ├── isosurface.py
│   │   ├── nerf_renderer.py
│   │   ├── network_utils.py
│   │   ├── tokenizers
│   │   │   │   ├── image.cpython-311.pyc
│   │   │   │   └── triplane.cpython-311.pyc
│   │   │   ├── image.py
│   │   │   └── triplane.py
│   │   └── transformer
│   │       │   ├── attention.cpython-311.pyc
│   │       │   └── transformer_1d.cpython-311.pyc
│   │       ├── attention.py
│   │       ├── basic_transformer_block.py
│   │       └── transformer_1d.py
│   ├── system.py
│   └── utils.py
├── elto.spec
├── icon.ico
├── model
│   ├── config.yaml
│   └── model.ckpt
├── requirements.txt
└── vendor
    ├── GLTFLoader.js
    ├── OBJLoader.js
    ├── OrbitControls.js
    ├── TransformControls.js
    ├── qwebchannel.js
    └── three.min.js
```