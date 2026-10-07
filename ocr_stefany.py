# -*- coding: utf-8 -*-
"""
ocr_stefany.py — Leer el texto que vive dentro de las imágenes de un correo.

Por qué hace falta: en la bandeja real de la usuaria, los correos de la
universidad llegan con TODO el contenido dentro de imágenes. Medido sobre sus
correos, cuatro de dieciséis tienen cero palabras útiles y nueve imágenes
remotas cada uno. Hasta ahora el sistema solo veía remitente y asunto, y
decidía con eso.

Dos reglas de diseño:

1. OCR CONDICIONAL. No se ejecuta nunca "por si acaso": solo cuando la
   extracción de texto no encontró contenido. Un correo con texto normal no
   paga ni un milisegundo.

2. LAS IMÁGENES REMOTAS NO SE DESCARGAN SOLAS. Casi todas las imágenes de los
   correos están alojadas en servidores del remitente, así que descargarlas le
   confirma que el correo fue abierto: es el mecanismo de los píxeles de
   rastreo. Las adjuntas (cid:) ya viajan dentro del correo y se pueden leer
   sin avisar a nadie; las remotas requieren que el usuario lo pida.

Motor: RapidOCR (onnxruntime). Se eligió sobre Tesseract porque se instala con
pip, sin binarios externos, lo que importa para empaquetar la app como .exe.
Pesa unos 16 MB con los modelos incluidos y funciona sin conexión.
"""

import io
import re

# Tamaño mínimo para considerar que una imagen puede tener contenido. Por
# debajo son iconos, píxeles de rastreo, separadores y botones de redes.
ANCHO_MINIMO = 200
ALTO_MINIMO = 120
BYTES_MINIMOS = 6 * 1024

# Tope por correo: leer nueve banners no aporta más que leer los tres grandes,
# y evita que un correo tarde medio minuto.
MAX_IMAGENES = 4
MAX_BYTES_DESCARGA = 5 * 1024 * 1024
TIEMPO_LIMITE = 10

_motor = None
_error_motor = None


def hay_ocr_disponible():
    """True si RapidOCR está instalado. La app funciona sin él."""
    return _cargar_motor() is not None


def _cargar_motor():
    """Carga perezosa: importar RapidOCR tarda unos segundos, y la mayoría de
    las sesiones no necesitan OCR."""
    global _motor, _error_motor
    if _motor is not None or _error_motor is not None:
        return _motor
    try:
        from rapidocr_onnxruntime import RapidOCR
        _motor = RapidOCR()
    except Exception as e:
        _error_motor = e
        print(f"⚠️ OCR no disponible ({type(e).__name__}). "
              f"Instálalo con: pip install rapidocr-onnxruntime")
    return _motor


def _ordenar_por_posicion(resultado):
    """Ordena las cajas de texto como se leen: por filas y luego izquierda a
    derecha. RapidOCR las devuelve en orden de detección, y concatenarlas tal
    cual producía 'Sesion 16 de octubre' en vez de 'Sesion 1  6 de octubre'.
    """
    cajas = []
    for caja, texto, confianza in resultado:
        if confianza < 0.5 or not texto.strip():
            continue
        ys = [p[1] for p in caja]
        xs = [p[0] for p in caja]
        cajas.append((min(ys), min(xs), texto.strip()))

    cajas.sort(key=lambda c: (round(c[0] / 20), c[1]))

    lineas, fila_actual, y_actual = [], [], None
    for y, _x, texto in cajas:
        if y_actual is None or abs(y - y_actual) <= 20:
            fila_actual.append(texto)
            y_actual = y if y_actual is None else y_actual
        else:
            lineas.append('  '.join(fila_actual))
            fila_actual, y_actual = [texto], y
    if fila_actual:
        lineas.append('  '.join(fila_actual))
    return '\n'.join(lineas)


def leer_imagen(datos_imagen):
    """Devuelve el texto de UNA imagen, o '' si no se pudo leer."""
    motor = _cargar_motor()
    if motor is None or not datos_imagen:
        return ''

    try:
        from PIL import Image
        import numpy as np
        imagen = Image.open(io.BytesIO(datos_imagen)).convert('RGB')
        if imagen.width < ANCHO_MINIMO or imagen.height < ALTO_MINIMO:
            return ''      # icono, separador o píxel de rastreo
        resultado, _ = motor(np.array(imagen))
        if not resultado:
            return ''
        return _ordenar_por_posicion(resultado)
    except Exception as e:
        print(f"⚠️ No se pudo leer una imagen ({type(e).__name__}): {e}")
        return ''


def parece_decorativa(nombre='', tamano=0, url=''):
    """Descarta logos, banners de firma, iconos de redes y píxeles de rastreo
    antes de gastar tiempo en leerlos."""
    if tamano and tamano < BYTES_MINIMOS:
        return True
    pista = f"{nombre} {url}".lower()
    return bool(re.search(
        r'logo|icon|ico_|spacer|pixel|track|beacon|footer|header|firma|'
        r'signature|facebook|instagram|twitter|linkedin|youtube|whatsapp|'
        r'social|badge|boton|button|arrow|bullet|divider|separator',
        pista))


def leer_imagenes(imagenes):
    """imagenes: [(etiqueta, bytes), ...] → texto unido.

    Se detiene al llegar a MAX_IMAGENES.
    """
    textos = []
    for etiqueta, datos in imagenes[:MAX_IMAGENES]:
        texto = leer_imagen(datos)
        if texto and len(texto.split()) >= 3:
            textos.append(texto)
        else:
            print(f"   · sin texto legible en {etiqueta}")
    return '\n\n'.join(textos)


def descargar_imagen_remota(url):
    """Descarga una imagen alojada en el servidor del remitente.

    IMPORTANTE: esto le confirma al remitente que el correo fue abierto. Solo
    debe llamarse cuando el usuario lo ha pedido explícitamente.
    """
    try:
        import requests
        respuesta = requests.get(url, timeout=TIEMPO_LIMITE, stream=True,
                                 headers={'User-Agent': 'Mozilla/5.0'})
        respuesta.raise_for_status()
        if 'image' not in respuesta.headers.get('Content-Type', ''):
            return None
        datos = b''
        for trozo in respuesta.iter_content(8192):
            datos += trozo
            if len(datos) > MAX_BYTES_DESCARGA:
                return None
        return datos
    except Exception as e:
        print(f"⚠️ No se pudo descargar {url[:60]}... ({type(e).__name__})")
        return None
