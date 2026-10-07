# -*- coding: utf-8 -*-
"""
rutas_stefany.py — Dónde lee y dónde escribe la aplicación.

Ejecutada con `python app_stefany.py`, la app escribía en el directorio actual.
Dentro de un .exe eso rompe de dos formas:

  - Los datos del usuario (tokens, conversaciones, caché) acabarían junto al
    ejecutable. Si está en "Archivos de programa", Windows no deja escribir y
    la app no arranca; y si el usuario mueve el .exe, pierde todo.
  - Los archivos que vienen DENTRO del paquete (credentials.json, el logo) no
    están en el directorio actual, sino en una carpeta temporal que PyInstaller
    crea al vuelo y expone en sys._MEIPASS.

Este módulo separa las dos cosas: recursos empaquetados (solo lectura) y datos
del usuario (escritura).
"""

import os
import shutil
import sys

NOMBRE_APP = "StefanyApp"


def esta_empaquetada():
    """True si corre dentro de un ejecutable de PyInstaller."""
    return getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')


def carpeta_recursos():
    """De dónde se leen los archivos incluidos en el paquete."""
    if esta_empaquetada():
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def recurso(nombre):
    """Ruta a un archivo empaquetado (credentials.json, logo...)."""
    return os.path.join(carpeta_recursos(), nombre)


def carpeta_datos():
    """Dónde se guardan los datos del usuario.

    Windows:  %LOCALAPPDATA%\\StefanyApp
    macOS:    ~/Library/Application Support/StefanyApp
    Linux:    ~/.local/share/StefanyApp

    En desarrollo (sin empaquetar) se sigue usando el directorio actual, para
    no obligar a reconectar cuentas ni perder la caché mientras se programa.
    """
    if not esta_empaquetada():
        return os.path.abspath(".")

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")

    destino = os.path.join(base, NOMBRE_APP)
    os.makedirs(destino, exist_ok=True)
    return destino


def ruta_datos(*partes):
    """Ruta dentro de la carpeta de datos, creando los directorios."""
    completa = os.path.join(carpeta_datos(), *partes)
    carpeta = completa if not os.path.splitext(completa)[1] else os.path.dirname(completa)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    return completa


def migrar_datos_antiguos():
    """Mueve los datos que quedaron junto al script a la carpeta definitiva.

    Solo corre una vez y solo si la app está empaquetada: evita que al pasar
    de ejecutar con Python a usar el .exe se pierdan las cuentas conectadas y
    la caché de resúmenes (que representa horas de llamadas al modelo).
    """
    if not esta_empaquetada():
        return

    destino_base = carpeta_datos()
    marca = os.path.join(destino_base, ".migrado")
    if os.path.exists(marca):
        return

    origen_base = os.path.dirname(sys.executable)
    for nombre in ("tokens", "conversaciones", "datos_stefany"):
        origen = os.path.join(origen_base, nombre)
        destino = os.path.join(destino_base, nombre)
        if os.path.isdir(origen) and not os.path.isdir(destino):
            try:
                shutil.copytree(origen, destino)
                print(f"📦 Datos migrados: {nombre}")
            except Exception as e:
                print(f"⚠️ No se pudo migrar {nombre}: {e}")

    try:
        with open(marca, "w", encoding="utf-8") as f:
            f.write("ok")
    except Exception:
        pass


# ---------------------------------------------------------------
# Arranque de Flet
# ---------------------------------------------------------------

def limpiar_extraccion_flet():
    """Elimina extracciones a medias del cliente de escritorio de Flet.

    Flet descomprime su cliente en ~/.flet/client/ usando una carpeta temporal
    ('...nil0') que después renombra al nombre definitivo. Si el destino ya
    existe —porque un intento anterior se interrumpió o porque se abrieron dos
    copias a la vez— el renombrado falla con:

        WinError 183: No se puede crear un archivo que ya existe

    y la aplicación no arranca. Se da sobre todo en la PRIMERA ejecución en un
    equipo o perfil nuevo, que es justo el caso de quien descarga el .exe.
    """
    carpeta = os.path.join(os.path.expanduser("~"), ".flet", "client")
    if not os.path.isdir(carpeta):
        return
    try:
        for nombre in os.listdir(carpeta):
            # Las temporales llevan un sufijo aleatorio tras '.nil'
            if ".nil" in nombre:
                ruta = os.path.join(carpeta, nombre)
                shutil.rmtree(ruta, ignore_errors=True)
                print(f"🧹 Limpiada extracción incompleta de Flet: {nombre}")
    except Exception as e:
        print(f"⚠️ No se pudo revisar la carpeta de Flet: {e}")


_cerrojo_instancia = None


def es_unica_instancia():
    """True si no hay ya otra copia de la app abierta POR ESTE USUARIO.

    El primer intento usaba un puerto TCP como cerrojo, y eso estaba mal: los
    puertos son de toda la máquina, no de cada sesión. Con dos usuarios
    conectados al mismo equipo, el segundo veía el puerto ocupado por el
    primero, creía que ya había una copia abierta y se cerraba en silencio.

    Ahora el cerrojo es un archivo dentro de la carpeta de datos del usuario,
    que es distinta para cada perfil.
    """
    global _cerrojo_instancia
    ruta = os.path.join(carpeta_datos(), ".cerrojo")
    try:
        _cerrojo_instancia = open(ruta, "w")
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(_cerrojo_instancia.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_cerrojo_instancia.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    except Exception:
        # Ante cualquier otro problema con el cerrojo, mejor dejar abrir la
        # app que impedirlo: el cerrojo es una comodidad, no un requisito.
        return True
