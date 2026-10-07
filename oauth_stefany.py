# -*- coding: utf-8 -*-
"""
oauth_stefany.py — Conexión de cuentas de Google DESDE el cliente de
escritorio.

Requisitos:
    pip install google-auth-oauthlib google-auth google-api-python-client

Necesita, junto a este archivo:
    credentials.json   -- la credencial OAuth de tipo Escritorio creada en
                           Google Cloud Console (manual técnico, sección 10.3).

Guarda un token por cuenta conectada en la carpeta ./tokens/, para no
tener que volver a pedir permiso cada vez que se abre la app (mientras el
token siga vigente o se pueda refrescar).
"""

import os
import json
import threading

# FIX: google-auth-oauthlib a veces rechaza el token justo después de que
# el usuario ya aprobó todo en el navegador, porque Google devuelve los
# scopes en un orden/formato distinto al pedido y la librería lo trata
# como error por defecto. Esta variable relaja esa validación (es el fix
# estándar para este problema, no afecta qué permisos se piden de verdad).
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# gmail.modify permite leer, marcar como leído/no leído y crear borradores.
# Sustituye a gmail.readonly (lo incluye) y a gmail.send (que ya no hace
# falta: el sistema NUNCA envía correos, solo deja borradores en Gmail).
#
# Advertencia honesta: no existe un scope de Google que permita crear
# borradores pero prohíba enviar. gmail.modify y gmail.compose permiten
# ambas cosas. La garantía de "nunca envía solo" la da el código
# (gmail_calendar_stefany.enviar_correo_gmail está desactivada), no el
# permiso.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]

import rutas_stefany as rutas

# credentials.json viaja DENTRO del paquete (solo lectura); los tokens son
# datos del usuario y van a su carpeta personal.
RUTA_CREDENCIALES = rutas.recurso("credentials.json")
CARPETA_TOKENS = os.path.join(rutas.carpeta_datos(), "tokens")

# Tope de cuentas de Google conectadas a la vez.
#
# No es un capricho: la revisión mete el calendario de TODAS las cuentas
# (hasta 50 eventos de cada una, 30 días) dentro del prompt de CADA correo, y
# el servidor solo admite 4.096 tokens de contexto (3.796 de entrada). Cada
# evento ocupa unos 40 tokens, así que 5 cuentas con 15 eventos cada una ya
# rondan los 3.000. Pasado ese punto el servidor recorta el prompt POR LA
# IZQUIERDA y se come el system prompt sin avisar de nada.
MAXIMO_CUENTAS = 5


class OAuthStefanyError(Exception):
    pass


# FIX: si el usuario hace doble clic en "Conectar cuenta de Google" (por
# ejemplo porque el navegador tardó en abrirse y pareció que no pasó
# nada), antes se disparaban DOS flujos de OAuth al mismo tiempo, en dos
# puertos distintos, y ninguno terminaba bien -- de ahí el error "None".
# Este candado bloquea intentos simultáneos con un mensaje claro.
_candado_conexion = threading.Lock()


def _ruta_token(alias):
    os.makedirs(CARPETA_TOKENS, exist_ok=True)
    return os.path.join(CARPETA_TOKENS, f"{alias}.json")


def _ruta_token_antiguo(alias):
    """Formato anterior (.pickle). Ya no se lee: deserializar un pickle
    alterado ejecuta código arbitrario, y además esos tokens quedaron
    inválidos al cambiar los scopes."""
    return os.path.join(CARPETA_TOKENS, f"{alias}.pickle")


def conectar_cuenta_google():
    """
    Abre el navegador del usuario para que inicie sesión con su cuenta de
    Google y apruebe los permisos. BLOQUEA hasta que el usuario termina el
    flujo (o lo cancela) -- por eso siempre se debe llamar desde un hilo
    aparte, nunca desde el hilo principal de la interfaz.

    Devuelve (alias, credenciales) donde alias es el correo real de la
    cuenta que se acaba de conectar (se usa como identificador; el usuario
    puede asignarle después un apodo más corto).
    """
    if not os.path.exists(RUTA_CREDENCIALES):
        raise OAuthStefanyError(
            f"No encontré '{RUTA_CREDENCIALES}' junto a la aplicación. "
            f"Copia ahí el credentials.json de la credencial 'asistente-credenciales'."
        )

    # El tope se comprueba aquí y no en la interfaz porque hay dos caminos
    # para conectar (el onboarding y la ventana de cuentas) y así uno solo
    # puede saltárselo.
    conectadas = cargar_cuentas_guardadas()
    if len(conectadas) >= MAXIMO_CUENTAS:
        raise OAuthStefanyError(
            f"Ya tienes {len(conectadas)} cuentas conectadas, que es el máximo "
            f"({MAXIMO_CUENTAS}). Desconecta una para añadir otra."
        )

    if not _candado_conexion.acquire(blocking=False):
        raise OAuthStefanyError(
            "Ya hay una conexión en curso en otra ventana/pestaña -- termínala "
            "(o ciérrala y vuelve a intentar) antes de darle clic de nuevo."
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(RUTA_CREDENCIALES, SCOPES)
        return _ejecutar_flujo(flow)
    finally:
        _candado_conexion.release()


def _ejecutar_flujo(flow):
    try:
        # run_local_server abre el navegador y levanta un servidor local
        # temporal (puerto 0 = uno libre cualquiera) que captura la
        # respuesta de Google automáticamente -- el usuario nunca ve ni
        # copia códigos.
        credenciales = flow.run_local_server(port=0, prompt="consent")
    except Exception as e:
        raise OAuthStefanyError(f"Falló el flujo de OAuth ({type(e).__name__}): {e}")

    # FIX: la API "oauth2 v2" (.userinfo().get()) necesita el scope
    # 'userinfo.email', que no está en SCOPES -- por eso daba 401. En vez
    # de agregar un scope nuevo (que además haría que Google pida un
    # permiso extra en la pantalla de consentimiento), se usa
    # Gmail.getProfile(), que devuelve el correo de la cuenta y solo
    # necesita el scope gmail.readonly que ya tenías.
    try:
        servicio_gmail = build("gmail", "v1", credentials=credenciales)
        perfil = servicio_gmail.users().getProfile(userId="me").execute()
        alias = perfil.get("emailAddress", "cuenta_sin_correo")
    except Exception as e:
        raise OAuthStefanyError(
            f"La cuenta se autenticó pero no pude leer su correo ({type(e).__name__}): {e}."
        )

    _guardar_token(alias, credenciales)
    return alias, credenciales


def _guardar_token(alias, credenciales):
    with open(_ruta_token(alias), "w", encoding="utf-8") as f:
        f.write(credenciales.to_json())


def cuentas_por_reconectar():
    """Cuentas cuyo token existe pero ya no sirve.

    Ocurre en dos casos: tokens del formato antiguo (.pickle) y tokens
    guardados con menos permisos de los que ahora necesita la app. En vez de
    fallar con un 403 críptico a mitad de una revisión, la app puede avisar
    de entrada qué hay que reconectar.
    """
    pendientes = []
    if not os.path.isdir(CARPETA_TOKENS):
        return pendientes

    for nombre in os.listdir(CARPETA_TOKENS):
        if nombre.endswith(".pickle"):
            pendientes.append(nombre[:-len(".pickle")])
        elif nombre.endswith(".json"):
            alias = nombre[:-len(".json")]
            try:
                with open(os.path.join(CARPETA_TOKENS, nombre), encoding="utf-8") as f:
                    datos = json.load(f)
            except Exception:
                pendientes.append(alias)
                continue
            if set(SCOPES) - set(datos.get("scopes") or []):
                pendientes.append(alias)

    # Una cuenta con .pickle y .json a la vez ya está migrada.
    migradas = {n[:-len(".json")] for n in os.listdir(CARPETA_TOKENS)
                if n.endswith(".json")}
    return sorted({a for a in pendientes if a not in migradas} |
                  {a for a in pendientes if a in migradas and
                   a not in _alias_con_scopes_completos()})


def _alias_con_scopes_completos():
    completos = set()
    if not os.path.isdir(CARPETA_TOKENS):
        return completos
    for nombre in os.listdir(CARPETA_TOKENS):
        if not nombre.endswith(".json"):
            continue
        try:
            with open(os.path.join(CARPETA_TOKENS, nombre), encoding="utf-8") as f:
                datos = json.load(f)
        except Exception:
            continue
        if not (set(SCOPES) - set(datos.get("scopes") or [])):
            completos.add(nombre[:-len(".json")])
    return completos


def cargar_cuentas_guardadas():
    """
    Lee todas las cuentas conectadas anteriormente (carpeta ./tokens/),
    refrescando el token si ya venció pero aún se puede renovar sin volver
    a pasar por el navegador. Devuelve {alias: credenciales}.

    Se omiten las cuentas cuyo token ya no se puede refrescar, las que están
    en el formato antiguo (.pickle) y las que no tienen todos los permisos
    que la app necesita ahora: todas esas hay que reconectarlas con
    conectar_cuenta_google(). cuentas_por_reconectar() dice cuáles son.
    """
    cuentas = {}
    if not os.path.isdir(CARPETA_TOKENS):
        return cuentas

    for nombre_archivo in os.listdir(CARPETA_TOKENS):
        if not nombre_archivo.endswith(".json"):
            continue
        alias = nombre_archivo[:-len(".json")]
        ruta = os.path.join(CARPETA_TOKENS, nombre_archivo)
        try:
            with open(ruta, encoding="utf-8") as f:
                datos = json.load(f)
            credenciales = Credentials.from_authorized_user_info(datos, SCOPES)
        except Exception:
            continue

        # Un token guardado con permisos viejos no se amplía solo: Google
        # exige pasar de nuevo por la pantalla de consentimiento.
        faltantes = set(SCOPES) - set(datos.get("scopes") or [])
        if faltantes:
            print(f"⚠️ [{alias}] necesita reconectarse: faltan permisos "
                  f"({', '.join(s.rsplit('/', 1)[-1] for s in faltantes)}).")
            continue

        if credenciales and credenciales.expired and credenciales.refresh_token:
            try:
                credenciales.refresh(Request())
                _guardar_token(alias, credenciales)
            except Exception:
                continue  # el usuario revocó el acceso u otro error -- se omite

        if credenciales and credenciales.valid:
            cuentas[alias] = credenciales

    return cuentas


def eliminar_cuenta(alias):
    """Olvida una cuenta conectada localmente (borra su token guardado).
    NOTA: esto NO revoca el permiso del lado de Google -- para que la app
    deje de tener acceso por completo, el usuario también debe
    revocarlo en https://myaccount.google.com/permissions."""
    for ruta in (_ruta_token(alias), _ruta_token_antiguo(alias)):
        if os.path.exists(ruta):
            os.remove(ruta)


def construir_cuentas_conectadas():
    """
    Construye el mismo diccionario {alias: {'gmail': service, 'calendar':
    service}} a partir de las credenciales guardadas LOCALMENTE
    (cargar_cuentas_guardadas()). Se llama una vez al
    iniciar la app (y de nuevo si el usuario conecta una cuenta nueva).
    """
    cuentas = {}
    for alias, credenciales in cargar_cuentas_guardadas().items():
        cuentas[alias] = {
            'gmail':    build('gmail', 'v1', credentials=credenciales),
            'calendar': build('calendar', 'v3', credentials=credenciales),
        }
    return cuentas
