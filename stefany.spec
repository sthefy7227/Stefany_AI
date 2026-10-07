# -*- mode: python ; coding: utf-8 -*-
"""
stefany.spec — Construcción del ejecutable.

    pyinstaller stefany.spec --noconfirm

Resultado: dist/Stefany.exe — UN SOLO ARCHIVO, que es lo que se puede subir a
una web. La versión en carpeta obligaba a copiar también '_internal' (ahí va
python311.dll); copiar solo el .exe daba "Failed to load Python DLL".

Tres paquetes cargan archivos en tiempo de EJECUCIÓN, no por import, así que
PyInstaller no los detecta y hay que recogerlos a mano. Si faltan, el .exe se
construye sin errores y falla al usarse:

  - flet   -> archivo de datos con la lista de iconos. Sin él la app ni abre:
              FileNotFoundError en flet/controls/material/icons.py
  - rapidocr_onnxruntime -> modelos .onnx (~16 MB). Sin ellos la app abre pero
              falla al pulsar "Leer las imágenes".
  - googleapiclient -> construye los clientes con build('gmail','v1').
"""

import os
from PyInstaller.utils.hooks import collect_all

bloque_cipher = None

datos, binarios, ocultos = [], [], []


def recoger(paquete, obligatorio=True):
    global datos, binarios, ocultos
    try:
        d, b, o = collect_all(paquete)
        datos += d
        binarios += b
        ocultos += o
        print(f"[stefany.spec] {paquete}: {len(d)} datos, {len(b)} binarios, {len(o)} modulos")
        return True
    except Exception as e:
        nivel = "ERROR" if obligatorio else "aviso"
        print(f"[stefany.spec] {nivel}: no se pudo recoger '{paquete}' ({e})")
        return False


recoger('flet')                                     # imprescindible
recoger('rapidocr_onnxruntime', obligatorio=False)  # sin esto, no hay OCR
recoger('onnxruntime', obligatorio=False)
# tzlocal da el nombre IANA de la zona ('America/Bogota') para la API de
# Calendar; en Windows lo resuelve con los datos de 'tzdata', que son archivos
# y no imports. Sin ellos el .exe no falla, pero crea los eventos sin zona.
recoger('tzlocal', obligatorio=False)
recoger('tzdata', obligatorio=False)

datos += [
    ('credentials.json', '.'),
    ('assets', 'assets'),
]

ocultos += [
    'googleapiclient.discovery',
    'googleapiclient.http',
    'googleapiclient.discovery_cache',
    'googleapiclient.discovery_cache.base',
    'google.auth.transport.requests',
    'google_auth_oauthlib.flow',
    'google.oauth2.credentials',
    'bs4',
    'PIL.Image',
]

a = Analysis(
    ['app_stefany.py'],
    pathex=[],
    binaries=binarios,
    datas=datos,
    hiddenimports=ocultos,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # unsloth, torch y transformers viven en el SERVIDOR de Modal, no aqui.
    excludes=['torch', 'transformers', 'unsloth', 'peft', 'modal',
              'matplotlib', 'scipy', 'pandas', 'tkinter', 'IPython'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=bloque_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=bloque_cipher)

# Un solo archivo: se pasan binarios, zipfiles y datas al EXE en vez de COLLECT.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Stefany',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # ponlo en True si necesitas ver el error al arrancar
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/logo.ico' if os.path.exists('assets/logo.ico') else None,
)
