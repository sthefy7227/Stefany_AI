# -*- coding: utf-8 -*-
"""
persistencia.py — Guarda el historial de conversaciones del chat
localmente, una por archivo JSON, en ./conversaciones/. Esto es
independiente de memoria_local.py (que guarda preferencias y correos
vistos) -- este módulo solo guarda lo que se ve en el panel izquierdo
de la app.
"""

import json
import os
import tempfile
import time
import uuid
from datetime import datetime

import rutas_stefany as rutas

CARPETA_CONVERSACIONES = os.path.join(rutas.carpeta_datos(), "conversaciones")


def _asegurar_carpeta():
    os.makedirs(CARPETA_CONVERSACIONES, exist_ok=True)


def _ruta(conv_id):
    return os.path.join(CARPETA_CONVERSACIONES, f"{conv_id}.json")


def _guardar(conversacion):
    """Escritura atómica y con reintentos.

    Dos problemas reales: (1) si el proyecto vive en una carpeta sincronizada
    (OneDrive, Drive), el sincronizador puede tener el archivo abierto y
    Windows devuelve PermissionError; (2) escribir directamente sobre el
    archivo dejaba un JSON corrupto —y la conversación perdida— si el proceso
    se interrumpía a mitad.

    Se escribe en un temporal y se reemplaza de golpe (os.replace es atómico),
    reintentando si el archivo está bloqueado.
    """
    _asegurar_carpeta()
    destino = _ruta(conversacion["id"])

    for intento in range(5):
        temporal = None
        try:
            descriptor, temporal = tempfile.mkstemp(
                dir=CARPETA_CONVERSACIONES, prefix=".tmp_", suffix=".json")
            with os.fdopen(descriptor, "w", encoding="utf-8") as f:
                json.dump(conversacion, f, ensure_ascii=False, indent=2)
            os.replace(temporal, destino)
            return
        except PermissionError:
            if temporal and os.path.exists(temporal):
                try:
                    os.remove(temporal)
                except OSError:
                    pass
            time.sleep(0.25 * (intento + 1))
        except Exception:
            if temporal and os.path.exists(temporal):
                try:
                    os.remove(temporal)
                except OSError:
                    pass
            raise

    print(f"⚠️ No se pudo guardar la conversación {conversacion['id']}: "
          f"el archivo está bloqueado por otro programa (¿OneDrive sincronizando?). "
          f"La app sigue funcionando, pero este mensaje no quedó en el historial.")


def nueva_conversacion():
    conversacion = {
        "id": str(uuid.uuid4()),
        "titulo": "Nueva conversación",
        "creada": datetime.now().isoformat(),
        "mensajes": [],
    }
    _guardar(conversacion)
    return conversacion


def listar_conversaciones():
    """Devuelve [{'id':..., 'titulo':...}, ...] de más reciente a más
    antigua (según la fecha de creación)."""
    _asegurar_carpeta()
    conversaciones = []
    for nombre_archivo in os.listdir(CARPETA_CONVERSACIONES):
        if not nombre_archivo.endswith(".json"):
            continue
        try:
            with open(os.path.join(CARPETA_CONVERSACIONES, nombre_archivo), encoding="utf-8") as f:
                data = json.load(f)
            conversaciones.append({
                "id": data["id"],
                "titulo": data.get("titulo", "Conversación"),
                "creada": data.get("creada", ""),
            })
        except Exception:
            continue
    conversaciones.sort(key=lambda c: c["creada"], reverse=True)
    return conversaciones


def cargar_conversacion(conv_id):
    with open(_ruta(conv_id), encoding="utf-8") as f:
        return json.load(f)


def agregar_mensaje(conversacion, rol, texto, modelo=None, datos=None):
    """rol: 'usuario' | 'asistente'. Modifica `conversacion` en memoria Y
    la guarda en disco. Si es el primer mensaje del usuario, autogenera
    el título de la conversación a partir de ese texto."""
    # La interfaz pinta msg["hora"], pero nadie la escribía: ese campo nunca
    # llegó a mostrarse.
    mensaje = {"rol": rol, "texto": texto, "hora": datetime.now().strftime("%I:%M %p").lstrip("0")}
    if modelo:
        mensaje["modelo"] = modelo
    # 'datos' lleva la estructura de la revisión de correos (grupos,
    # categorías, señales) para que la burbuja se pinte como acordeón en vez
    # de como texto plano. Es una vista LIGERA: nunca contiene los cuerpos.
    if datos:
        mensaje["datos"] = datos
    conversacion["mensajes"].append(mensaje)

    if rol == "usuario" and conversacion.get("titulo") == "Nueva conversación":
        titulo = texto.strip().replace("\n", " ")
        conversacion["titulo"] = (titulo[:40] + "…") if len(titulo) > 40 else titulo

    # La interfaz guarda SU copia en memoria de la conversación, y esa copia
    # no ve el estado de sesión que el router escribe en disco mientras
    # responde. Guardarla tal cual borraba la acción pendiente ("¿Confirmas
    # agendar...?"), así que el "sí" siguiente no encontraba nada que
    # confirmar. El estado de sesión lo gestiona solo el router: se conserva
    # el que haya en disco.
    try:
        en_disco = cargar_conversacion(conversacion["id"])
        if "estado_sesion" in en_disco:
            conversacion["estado_sesion"] = en_disco["estado_sesion"]
    except (OSError, ValueError, KeyError):
        pass

    _guardar(conversacion)
    return conversacion


def eliminar_conversacion(conv_id):
    ruta = _ruta(conv_id)
    if os.path.exists(ruta):
        os.remove(ruta)


# ---------------------------------------------------------------
# Estado de sesión POR CONVERSACIÓN
# ---------------------------------------------------------------
# Antes el estado (acción pendiente, datos pendientes, último tema) era un
# dict global del módulo router: si dejabas una confirmación a medias en una
# conversación, abrías otra y escribías "sí", se ejecutaba la acción de la
# anterior. Ahora cada conversación tiene el suyo y sobrevive a cerrar la app.

ESTADO_VACIO = {
    "accion_pendiente": None,
    "datos_pendientes": {},
    "ultimo_tema": None,
    "ultimo_evento_referenciado": None,
}


# El estado de sesión lleva fechas de Python (date/datetime): al agendar desde
# el chat se guarda 'fecha_resuelta' mientras se espera el "sí". json.dump no
# sabe escribirlas ("Object of type date is not JSON serializable") y el
# mensaje fallaba entero. Se guardan marcadas y se reconstruyen al leer, así el
# router sigue recibiendo un date y no un texto.
def _estado_a_json(valor):
    from datetime import date
    if isinstance(valor, datetime):
        return {"__datetime__": valor.isoformat()}
    if isinstance(valor, date):
        return {"__date__": valor.isoformat()}
    if isinstance(valor, dict):
        return {k: _estado_a_json(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_estado_a_json(v) for v in valor]
    return valor


def _estado_desde_json(valor):
    from datetime import date
    if isinstance(valor, dict):
        if set(valor) == {"__datetime__"}:
            return datetime.fromisoformat(valor["__datetime__"])
        if set(valor) == {"__date__"}:
            return date.fromisoformat(valor["__date__"])
        return {k: _estado_desde_json(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_estado_desde_json(v) for v in valor]
    return valor


def cargar_estado_sesion(conv_id):
    if not conv_id:
        return dict(ESTADO_VACIO)
    try:
        conversacion = cargar_conversacion(conv_id)
    except Exception:
        return dict(ESTADO_VACIO)
    estado = dict(ESTADO_VACIO)
    estado.update(_estado_desde_json(conversacion.get("estado_sesion") or {}))
    return estado


def guardar_estado_sesion(conv_id, estado):
    if not conv_id:
        return
    try:
        conversacion = cargar_conversacion(conv_id)
    except Exception:
        return
    conversacion["estado_sesion"] = _estado_a_json(estado or dict(ESTADO_VACIO))
    _guardar(conversacion)
