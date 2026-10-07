# -*- coding: utf-8 -*-
"""
memoria_local.py — Memoria del asistente (perfil, preferencias, cachés y
pendientes), guardada en el computador del usuario -- coherente con la
decisión de que los datos personales no salgan de la máquina del usuario
salvo para la consulta puntual que los necesite.
"""

import json
import os

import rutas_stefany as rutas

# Empaquetada, esta carpeta va a %LOCALAPPDATA%\StefanyApp; en desarrollo se
# queda junto al código. Ver rutas_stefany.py.
CARPETA_DATOS = os.path.join(rutas.carpeta_datos(), "datos_stefany")
RUTA_PREFERENCIAS = os.path.join(CARPETA_DATOS, "preferencias_usuario.json")


def _asegurar_carpeta():
    os.makedirs(CARPETA_DATOS, exist_ok=True)


def _escribir_json(ruta, contenido):
    """Escritura atómica con reintentos (ver persistencia._guardar).

    En carpetas sincronizadas el archivo puede estar bloqueado, y escribir
    directamente encima deja el JSON corrupto si algo se interrumpe. La caché
    de resúmenes representa horas de llamadas al modelo: no se puede perder
    por un choque con el sincronizador.
    """
    import tempfile, time
    _asegurar_carpeta()
    for intento in range(5):
        temporal = None
        try:
            descriptor, temporal = tempfile.mkstemp(
                dir=os.path.dirname(ruta) or ".", prefix=".tmp_", suffix=".json")
            with os.fdopen(descriptor, "w", encoding="utf-8") as f:
                json.dump(contenido, f, ensure_ascii=False, indent=2)
            os.replace(temporal, ruta)
            return True
        except PermissionError:
            if temporal and os.path.exists(temporal):
                try: os.remove(temporal)
                except OSError: pass
            time.sleep(0.25 * (intento + 1))
        except Exception:
            if temporal and os.path.exists(temporal):
                try: os.remove(temporal)
                except OSError: pass
            raise
    print(f"⚠️ No se pudo escribir {ruta}: archivo bloqueado por otro programa.")
    return False



# ---------------------------------------------------------------
# Correos ya vistos — ELIMINADO el 2-oct-2026
# ---------------------------------------------------------------
# Aquí vivían cargar_correos_vistos(), guardar_correos_vistos(),
# marcar_correos_vistos() y ya_fue_visto(), que escribían
# datos_stefany/correos_vistos.json.
#
# Se quitaron porque eran código muerto: el archivo se escribía en cada
# revisión y NADIE lo leía nunca (ya_fue_visto() no se llamaba desde ningún
# sitio). Quien cumple de verdad "no reanalizar correos ya procesados" es la
# caché de resúmenes de más abajo (resumen_en_cache / guardar_resumen): es la
# que hace que la primera revisión de 480 correos tarde 1.527 s y las
# siguientes 24 s.
#
# Tener dos mecanismos para lo mismo, con uno de ellos decorativo, solo
# complica explicar el sistema. El criterio de "procesado" sigue siendo el
# mismo y sigue aplicándose DESPUÉS de tener el resumen en mano: si Modal
# falla, no se guarda nada y el correo se reintenta en la siguiente revisión.

# ---------------------------------------------------------------
# Preferencias personales
# ---------------------------------------------------------------

# ---------------------------------------------------------------
# Perfil estructurado
# ---------------------------------------------------------------
# Antes las preferencias eran una lista de frases sueltas. El onboarding
# necesita campos concretos que el CÓDIGO pueda usar (hora por defecto,
# cuenta preferida, remitentes importantes...), no texto para el modelo.
# Las frases sueltas siguen existiendo como 'preferencias_libres' y son lo
# único del perfil que llega al prompt.
RUTA_PERFIL = os.path.join(CARPETA_DATOS, "perfil_usuario.json")

# Valores válidos (el onboarding y "Mis preferencias" escriben estos):
#   categorias_importantes: personal, laboral, academico, invitaciones, transaccional,
#                           otros (este último solo marca que 'otros_importantes'
#                           tiene texto: no sube la categoría "otros" del clasificador)
#   horario.dias:           0 = lunes ... 6 = domingo
#   horario.inicio / fin:   "HH:MM" en 24 h
#   reuniones.franja:       manana, tarde, indiferente, depende
#   reuniones.duracion_min: 15, 30, 45, 60, "depende" (= 1 h) o None (sin responder)
PERFIL_POR_DEFECTO = {
    "version": 1,
    "nombre": "",
    "onboarding_completado": False,
    "onboarding_pospuesto": False,    # pulsó "Dejar para después": no se vuelve a abrir solo
    "cuentas": {},                    # {"correo@x.com": {"apodo": "Universidad"}}
    "cuenta_preferida_agendar": "",
    "categorias_importantes": [],
    "otros_importantes": "",
    "remitentes_importantes": [],     # correos completos o dominios ("@uan.edu.co")
    "horario": {"dias": [], "inicio": "", "fin": ""},
    "reuniones": {"franja": "", "duracion_min": None},
    "preferencias_libres": [],
}

HORA_POR_DEFECTO_SIN_PERFIL = 9.0
HORA_REUNIONES_TARDE = 14.0


def _perfil_con_defectos(datos):
    """Completa con los valores por defecto lo que falte (perfiles de versiones anteriores)."""
    import copy
    perfil = copy.deepcopy(PERFIL_POR_DEFECTO)
    for clave, valor in (datos or {}).items():
        if isinstance(perfil.get(clave), dict) and isinstance(valor, dict):
            perfil[clave].update(valor)
        else:
            perfil[clave] = valor
    return perfil


# Cada tarjeta de correo consulta el apodo de su cuenta: con cientos de
# correos eran cientos de lecturas del archivo. Se relee solo si cambió.
_cache_perfil = {"clave": None, "perfil": None}


def cargar_perfil():
    import copy
    if os.path.exists(RUTA_PERFIL):
        try:
            clave = (RUTA_PERFIL, os.path.getmtime(RUTA_PERFIL), os.path.getsize(RUTA_PERFIL))
            if _cache_perfil["clave"] != clave:
                with open(RUTA_PERFIL, encoding="utf-8") as f:
                    _cache_perfil["perfil"] = _perfil_con_defectos(json.load(f))
                _cache_perfil["clave"] = clave
            # Copia: quien la reciba puede modificarla sin tocar la caché.
            return copy.deepcopy(_cache_perfil["perfil"])
        except (OSError, ValueError) as e:
            _cache_perfil["clave"] = None
            # No se sobrescribe: un perfil corrupto se puede recuperar a mano,
            # uno sobrescrito con valores vacíos no.
            print(f"⚠️ No se pudo leer el perfil ({e}); se usan valores por defecto.")
            return _perfil_con_defectos({})

    # Migración: las frases del formato anterior pasan a preferencias_libres.
    perfil = _perfil_con_defectos({})
    if os.path.exists(RUTA_PREFERENCIAS):
        try:
            with open(RUTA_PREFERENCIAS, encoding="utf-8") as f:
                perfil["preferencias_libres"] = json.load(f).get("preferencias", [])
            guardar_perfil(perfil)
            print(f"📦 Preferencias migradas al perfil ({len(perfil['preferencias_libres'])}).")
        except (OSError, ValueError) as e:
            print(f"⚠️ No se pudieron migrar las preferencias antiguas: {e}")
    return perfil


def guardar_perfil(perfil):
    _cache_perfil["clave"] = None
    return _escribir_json(RUTA_PERFIL, _perfil_con_defectos(perfil))


def actualizar_perfil(**campos):
    perfil = cargar_perfil()
    perfil.update(campos)
    guardar_perfil(perfil)
    return perfil


def necesita_onboarding():
    """True la primera vez que se abre la app (ni completado ni pospuesto)."""
    perfil = cargar_perfil()
    return not perfil.get("onboarding_completado") and not perfil.get("onboarding_pospuesto")


# "Otros" del onboarding se guarda también como preferencia libre, que es lo
# único del perfil que llega al modelo. Se marca con este prefijo para poder
# reemplazarla si la usuaria cambia la respuesta.
PREFIJO_OTROS_IMPORTANTES = "También considero importantes estos correos: "


def fijar_otros_importantes(perfil, texto):
    """Actualiza 'otros_importantes' y su preferencia libre asociada (en el dict, sin guardar)."""
    texto = (texto or "").strip()
    perfil["otros_importantes"] = texto
    libres = [p for p in perfil.get("preferencias_libres") or []
              if not p.startswith(PREFIJO_OTROS_IMPORTANTES)]
    if texto:
        libres.append(PREFIJO_OTROS_IMPORTANTES + texto)
    perfil["preferencias_libres"] = libres
    return perfil


# --- Cuentas: apodos y cuenta preferida ---

def apodo_cuenta(alias):
    return ((cargar_perfil().get("cuentas") or {}).get(alias) or {}).get("apodo", "").strip()


def etiqueta_cuenta(alias):
    """'Universidad · usuario@uan.edu.co', o solo el correo si no tiene apodo.

    El correo nunca se omite donde hay espacio: el apodo lo acompaña.
    """
    apodo = apodo_cuenta(alias)
    return f"{apodo} · {alias}" if apodo else alias


def cuenta_preferida(cuentas_conectadas):
    """Cuenta para lo que se agenda desde el CHAT (no para eventos de correos).

    Si la preferida ya no está conectada, o no se eligió ninguna, se usa la
    primera conectada, como antes de existir el perfil.
    """
    cuentas = list(cuentas_conectadas or [])
    preferida = cargar_perfil().get("cuenta_preferida_agendar", "")
    if preferida in cuentas:
        return preferida
    return cuentas[0] if cuentas else ""


# --- Valores por defecto al agendar ---

def _hora_a_float(hhmm):
    try:
        h, m = str(hhmm).split(":")
        return int(h) + int(m) / 60
    except (ValueError, AttributeError):
        return None


def hora_por_defecto():
    """Hora (float, 24 h) para un evento que no trae hora.

    Tarde → 2:00 p.m. En cualquier otro caso (mañana, indiferente, depende o
    sin responder) → el inicio del horario de trabajo/estudio, y si tampoco
    lo hay, 9:00 a.m. como siempre.
    """
    perfil = cargar_perfil()
    if (perfil.get("reuniones") or {}).get("franja") == "tarde":
        return HORA_REUNIONES_TARDE
    inicio = _hora_a_float((perfil.get("horario") or {}).get("inicio"))
    return inicio if inicio is not None else HORA_POR_DEFECTO_SIN_PERFIL


# ---------------------------------------------------------------
# Pendientes de agendar ("memoria progresiva")
# ---------------------------------------------------------------
# Cuando hay un cruce de horario, Stefany propone otro espacio por correo. Esa
# propuesta NO se agenda: queda aquí como pendiente hasta que la otra persona
# conteste. En cada revisión vuelve a aparecer ("recuerda que tienes pendiente
# agendar esta sesión con X") y caduca sola cuando pasa la fecha propuesta.
RUTA_PENDIENTES = os.path.join(CARPETA_DATOS, "pendientes_agenda.json")


def cargar_pendientes(incluir_caducados=False):
    import datetime as _dt
    if not os.path.exists(RUTA_PENDIENTES):
        return []
    try:
        with open(RUTA_PENDIENTES, encoding="utf-8") as f:
            pendientes = json.load(f).get("pendientes", [])
    except (OSError, ValueError) as e:
        print(f"⚠️ No se pudieron leer los pendientes de agenda ({e}).")
        return []
    if incluir_caducados:
        return pendientes
    from prompts_stefany import ZONA_LOCAL   # dentro: prompts_stefany importa este módulo
    hoy = _dt.datetime.now(ZONA_LOCAL).date()

    def vigente(p):
        try:
            fecha = _dt.date.fromisoformat(p.get("fecha_iso") or "")
        except ValueError:
            return False
        # Los recordatorios de renovación aparecen el día en que terminan las
        # repeticiones y se quedan una semana más: si caducaran ese mismo día,
        # bastaría no abrir la app para perderlos.
        margen = 7 if p.get("clase") == "renovacion" else 0
        return fecha + _dt.timedelta(days=margen) >= hoy

    vigentes = [p for p in pendientes if vigente(p)]
    if len(vigentes) != len(pendientes):
        _escribir_json(RUTA_PENDIENTES, {"pendientes": vigentes})
    return vigentes


def guardar_pendiente(pendiente):
    """Añade (o actualiza) un pendiente. La clave es correo + fecha + hora."""
    import datetime as _dt
    pendientes = cargar_pendientes(incluir_caducados=True)
    clave = (pendiente.get("id_correo"), pendiente.get("fecha_iso"), pendiente.get("hora_texto"))
    pendientes = [p for p in pendientes
                  if (p.get("id_correo"), p.get("fecha_iso"), p.get("hora_texto")) != clave]
    pendiente = dict(pendiente)
    from prompts_stefany import ZONA_LOCAL
    pendiente.setdefault("creado", _dt.datetime.now(ZONA_LOCAL).isoformat(timespec="seconds"))
    pendientes.append(pendiente)
    _escribir_json(RUTA_PENDIENTES, {"pendientes": pendientes})
    return pendiente


# ---------------------------------------------------------------
# Caché de las síntesis por categoría
# ---------------------------------------------------------------
# La frase de cada acordeón ("Se recibieron dos correos de GitHub…") se
# regeneraba en CADA revisión, aunque la categoría tuviera los mismos correos:
# 7 a 10 llamadas al modelo de más por revisión. Se guarda con la lista de
# correos que la produjo, así que solo se regenera si esa lista cambia.
RUTA_RESUMENES_CATEGORIA = os.path.join(CARPETA_DATOS, "resumenes_categorias.json")
LIMITE_RESUMENES_CATEGORIA = 60


def _clave_categoria(categoria, ids):
    import hashlib
    huella = hashlib.md5("|".join(sorted(ids)).encode("utf-8")).hexdigest()[:16]
    return f"{categoria}:{huella}"


def resumen_categoria_en_cache(categoria, ids):
    if not os.path.exists(RUTA_RESUMENES_CATEGORIA):
        return None
    try:
        with open(RUTA_RESUMENES_CATEGORIA, encoding="utf-8") as f:
            return json.load(f).get(_clave_categoria(categoria, ids))
    except (OSError, ValueError):
        return None


def guardar_resumen_categoria(categoria, ids, texto):
    datos = {}
    if os.path.exists(RUTA_RESUMENES_CATEGORIA):
        try:
            with open(RUTA_RESUMENES_CATEGORIA, encoding="utf-8") as f:
                datos = json.load(f)
        except (OSError, ValueError):
            datos = {}
    datos[_clave_categoria(categoria, ids)] = texto
    if len(datos) > LIMITE_RESUMENES_CATEGORIA:      # se conservan las más recientes
        datos = dict(list(datos.items())[-LIMITE_RESUMENES_CATEGORIA:])
    _escribir_json(RUTA_RESUMENES_CATEGORIA, datos)
    return texto


def guardar_sugerencias_pendientes(acciones):
    """Guarda las sugerencias de agendar que salieron en una revisión.

    Si la usuaria no las atiende, siguen ahí: el botón "Pendientes" las
    muestra aunque el correo ya no esté sin leer o la revisión sea de otro
    día. Se borran al agendarlas, al descartarlas o cuando pasa su fecha.
    """
    guardados = 0
    for accion in acciones or []:
        if not accion.get("fecha_iso"):
            continue
        guardar_pendiente({
            "id": f"sugerencia-{accion.get('id')}-{accion['fecha_iso']}-{accion.get('hora_texto', '')}",
            "clase": "sugerencia",
            "id_correo": accion.get("id"),
            "cuenta": accion.get("cuenta_destino") or accion.get("cuenta", ""),
            "persona": accion.get("remitente", ""),
            "remitente_email": accion.get("remitente_email", ""),
            "titulo": accion.get("titulo_evento") or accion.get("titulo", ""),
            "fecha_iso": accion["fecha_iso"],
            "hora_texto": accion.get("hora_texto", ""),
            "origen": "sugerido en una revisión",
            "explicacion": accion.get("explicacion", ""),
        })
        guardados += 1
    return guardados


def eliminar_pendiente(id_pendiente):
    pendientes = cargar_pendientes(incluir_caducados=True)
    quedan = [p for p in pendientes if p.get("id") != id_pendiente]
    if len(quedan) != len(pendientes):
        _escribir_json(RUTA_PENDIENTES, {"pendientes": quedan})
    return quedan


HORARIO_POR_DEFECTO = {"dias": [0, 1, 2, 3, 4], "inicio": 8.0, "fin": 17.0}


def horario_laboral():
    """(días, hora de inicio, hora de fin) del perfil, con L-V 8:00–17:00 por defecto.

    Se usa para buscar espacios libres: sin perfil hay que proponer algo
    razonable en vez de no proponer nada.
    """
    horario = cargar_perfil().get("horario") or {}
    dias = list(horario.get("dias") or HORARIO_POR_DEFECTO["dias"])
    inicio = _hora_a_float(horario.get("inicio"))
    fin = _hora_a_float(horario.get("fin"))
    return (dias,
            HORARIO_POR_DEFECTO["inicio"] if inicio is None else inicio,
            HORARIO_POR_DEFECTO["fin"] if fin is None else fin)


def aviso_fuera_de_horario(fecha, hora=None):
    """Texto de aviso si el evento cae fuera del horario del perfil, o "".

    Solo avisa: nunca impide agendar. Sin horario en el perfil, no dice nada.
    """
    from prompts_stefany import DIAS_ES
    from calendario_consulta_stefany import _hora_float_a_texto

    horario = cargar_perfil().get("horario") or {}
    dias = horario.get("dias") or []
    if fecha is not None and dias and fecha.weekday() not in dias:
        return f"⚠️ Es {DIAS_ES[fecha.weekday()]}, un día fuera de tu horario habitual."

    inicio, fin = _hora_a_float(horario.get("inicio")), _hora_a_float(horario.get("fin"))
    if hora is not None and inicio is not None and fin is not None and not (inicio <= hora < fin):
        return (f"⚠️ A las {_hora_float_a_texto(hora)}, fuera de tu horario "
                f"({_hora_float_a_texto(inicio)} – {_hora_float_a_texto(fin)}).")
    return ""


def duracion_por_defecto_horas():
    minutos = (cargar_perfil().get("reuniones") or {}).get("duracion_min")
    return minutos / 60 if isinstance(minutos, (int, float)) and minutos > 0 else 1.0


# ---------------------------------------------------------------
# Preferencias libres (antes: la única forma de preferencia)
# ---------------------------------------------------------------
# Mismas funciones de siempre, ahora guardadas dentro del perfil. Quien las
# usa (prompts_stefany, router_stefany) no necesita cambiar.

def cargar_preferencias():
    return list(cargar_perfil().get("preferencias_libres") or [])


def guardar_preferencias(lista_preferencias):
    perfil = cargar_perfil()
    perfil["preferencias_libres"] = list(lista_preferencias)
    guardar_perfil(perfil)


def agregar_preferencia(texto):
    prefs = cargar_preferencias()
    texto = texto.strip()
    if texto and texto not in prefs:
        prefs.append(texto)
        guardar_preferencias(prefs)
    return prefs


def eliminar_preferencia(indice):
    prefs = cargar_preferencias()
    if 0 <= indice < len(prefs):
        prefs.pop(indice)
        guardar_preferencias(prefs)
    return prefs


def preferencias_como_texto():
    prefs = cargar_preferencias()
    if not prefs:
        return "(El usuario todavía no ha guardado preferencias personales)"
    return "\n".join(f"- {p}" for p in prefs)


# NOTA: no se siembran preferencias de ejemplo la primera vez que corre:
# un usuario nuevo no debería arrancar con preferencias
# inventadas ("clase de danzas los viernes") que no son suyas.


# ---------------------------------------------------------------
# Caché de resúmenes
# ---------------------------------------------------------------
# Un correo ya resumido no necesita volver a pasar por el modelo. Esto es lo
# que hace que la primera revisión de una bandeja con cientos de no leídos sea
# cara UNA vez, y las siguientes casi gratis.

RUTA_RESUMENES = os.path.join(CARPETA_DATOS, "resumenes_correos.json")

LIMITE_RESUMENES = 2000  # se conservan los más recientes


def cargar_resumenes():
    if os.path.exists(RUTA_RESUMENES):
        try:
            with open(RUTA_RESUMENES, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def guardar_resumenes(resumenes):
    _escribir_json(RUTA_RESUMENES, resumenes)


def resumen_en_cache(alias, msg_id):
    return cargar_resumenes().get(alias, {}).get(msg_id)


def guardar_resumen(alias, msg_id, datos):
    """datos: {'resumen': str, 'analisis': str}"""
    resumenes = cargar_resumenes()
    de_la_cuenta = resumenes.setdefault(alias, {})
    de_la_cuenta[msg_id] = datos
    # Recorte por inserción (los dict de Python conservan el orden), no con
    # set(): un set no tiene orden y el
    # recorte terminaba descartando entradas al azar.
    if len(de_la_cuenta) > LIMITE_RESUMENES:
        sobrantes = len(de_la_cuenta) - LIMITE_RESUMENES
        for clave in list(de_la_cuenta.keys())[:sobrantes]:
            del de_la_cuenta[clave]
    guardar_resumenes(resumenes)
