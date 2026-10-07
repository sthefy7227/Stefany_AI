# -*- coding: utf-8 -*-
"""
analisis_stefany.py — Verificación de conflictos de horario por código
y saneamiento anti-alucinación / anti-fuga. Nada de esto usa el modelo -- es aritmética
y comparación de texto contra los datos reales del calendario/correo.
"""

import re
from datetime import date, timedelta

from prompts_stefany import MESES_ES, ZONA_BOGOTA
import gmail_calendar_stefany as gc
import clasificador_stefany as clasificador

# Los alias de cuenta son direcciones de correo completas (oauth usa
# getProfile().emailAddress), así que el patrón tiene que aceptar '@', '.',
# '+' y '-'. Con \w+ no matcheaba NADA en producción: por eso el "sí" a
# "¿Te gustaría que lo agende?" no hacía nada. Estaba escrito de tres formas
# distintas en este archivo, dos de ellas mal; ahora hay una sola.
PATRON_ALIAS = r'\[([\w.@+-]+)\]'

# TAREA B — Los marcadores eran cadenas EXACTAS, y el modelo escribe el
# formato del entrenamiento con erratas: "EVENTOs EN CALENDRIO (todasa las
# cuenttas)" no coincide con "EVENTOS EN CALENDARIO", así que la fuga pasaba
# entera. Ahora son expresiones regulares tolerantes a la deformación.
PATRONES_FUGA = [
    re.compile(p, re.IGNORECASE) for p in (
        r'evento?s?\s*_?\s*en\s*_?\s*calend\w*',
        r'correos?\s+recientes',
        r'analiza\s+est[ea]\s+correo',
        r'\(?\s*no\s+hay\s+eventos',
        r'no\s+exist\w+\s+(?:cualquier|eventos)',
        r'fecha\s+de\s+hoy',
        r'preferencias\s+personales',
        r'correo\s*\(\s*cuenta',
    )
]

# El bloque de análisis también llega deformado: "AnálISIS:", "🔬 AnálISIS:",
# "Análsis:". Si el corte falla, TODO queda dentro del resumen y las
# validaciones del análisis nunca se aplican.
PATRON_ANALISIS = re.compile(r'[^\w\s]{0,3}\s*an[aá]l\w{0,3}sis\s*:', re.IGNORECASE)


def _resolver_fecha_texto(fecha_texto):
    import datetime as _dt
    m = re.search(r'(\d{1,2})\s+de\s+(' + '|'.join(MESES_ES) + r')', fecha_texto, re.IGNORECASE)
    if not m:
        return None
    dia = int(m.group(1))
    mes = MESES_ES.index(m.group(2).lower()) + 1
    hoy = _dt.datetime.now(ZONA_BOGOTA).date()
    for anio in (hoy.year, hoy.year + 1):
        try:
            candidata = date(anio, mes, dia)
        except ValueError:
            continue
        if candidata >= hoy - timedelta(days=3):
            return candidata
    try:
        return date(hoy.year, mes, dia)
    except ValueError:
        return None


def _resolver_horas_texto(texto):
    horas = []
    for h, mi, ampm in re.findall(r'(\d{1,2}):(\d{2})\s*(a\.m\.|p\.m\.)', texto, re.IGNORECASE):
        h, mi = int(h), int(mi)
        ampm = ampm.lower()
        if ampm == 'p.m.' and h != 12:
            h += 12
        if ampm == 'a.m.' and h == 12:
            h = 0
        horas.append(h + mi / 60)
    return horas


def _eventos_en_fecha(fecha_buscada, eventos_por_cuenta):
    import datetime as _dt
    resultado = []
    for alias, eventos in eventos_por_cuenta.items():
        for e in eventos:
            try:
                inicio_dt = _dt.datetime.fromisoformat(e['inicio'])
            except Exception:
                continue
            if inicio_dt.date() != fecha_buscada:
                continue
            try:
                fin_dt = _dt.datetime.fromisoformat(e['fin'])
            except Exception:
                fin_dt = inicio_dt + timedelta(hours=1)
            resultado.append({
                'alias': alias, 'titulo': e['titulo'], 'id': e.get('id'),
                # La descripción dice de qué correo salió el evento: es la
                # forma más fiable de reconocerlo al llegar una cancelación.
                'descripcion': e.get('descripcion', ''),
                # El lugar actual hace falta para el CU3: sin él no se puede
                # distinguir un cambio de sitio de una simple repetición.
                'lugar': e.get('lugar', ''),
                'inicio_h': inicio_dt.hour + inicio_dt.minute / 60,
                'fin_h': fin_dt.hour + fin_dt.minute / 60,
            })
    return resultado


def _fecha_envio(correo):
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(correo.get('fecha', '')).astimezone(ZONA_BOGOTA).date()
    except Exception:
        return None


def _primera_fecha_explicita(texto, referencia):
    """La primera fecha escrita en el texto (cualquier formato reconocido), con año.

    El año se elige respecto a la fecha de ENVÍO: un correo del 5 de septiembre
    que habla del "1 de septiembre" se refiere a ese año, no al siguiente. Solo
    si la fecha queda más de 30 días antes del envío se pasa al año siguiente
    (un correo de diciembre que menciona el 10 de enero).
    """
    hallazgos = []
    for tipo, patron in _PATRONES_FECHA:
        for m in patron.finditer(texto or ""):
            g = m.groups()
            if tipo == 'dm':
                dia, mes = int(g[0]), _MESES_PATRON[g[1].lower()]
            elif tipo == 'md':
                dia, mes = int(g[1]), _MESES_PATRON[g[0].lower()]
            elif tipo == 'num':
                dia, mes = int(g[0]), int(g[1])      # día/mes, como en Colombia
            else:
                dia, mes = int(g[1]), int(g[0])
            hallazgos.append((m.start(), dia, mes))
    for _, dia, mes in sorted(hallazgos):
        for anio in (referencia.year, referencia.year + 1):
            try:
                candidata = date(anio, mes, dia)
            except ValueError:
                break
            if candidata >= referencia - timedelta(days=30):
                return candidata
    return None


def _fecha_relativa(texto, referencia):
    """'hoy', 'mañana', 'pasado mañana' contados desde la referencia."""
    t = clasificador._normalizar(texto)
    if re.search(r'\bpasado\s+manana\b', t):
        return referencia + timedelta(days=2)
    # "en la mañana", "esta mañana", "de la mañana" hablan de la hora, no del día.
    if re.search(r'(?<!\bla )(?<!\besta )\bmanana\b', t):
        return referencia + timedelta(days=1)
    if re.search(r'\b(hoy|esta\s+tarde|esta\s+noche)\b', t):
        return referencia
    return None


def _fecha_dia_semana(texto, referencia):
    """'el viernes', 'este jueves', 'próximo lunes': el siguiente desde la referencia.

    Se exige el artículo delante para no tomar "atención de lunes a viernes"
    de un pie de página como la fecha de un compromiso.
    """
    from prompts_stefany import DIAS_ES
    t = clasificador._normalizar(texto)
    for idx, dia in enumerate(DIAS_ES):
        # Entre el artículo y el día caben varias palabras: "el día lunes",
        # "el SIGUIENTE viernes", "el OTRO viernes", "del próximo martes".
        # Caso real: "Solicitud de reunión para el siguiente viernes" no se
        # reconocía ('siguiente' no estaba), así que el correo se quedaba sin
        # fecha y el borrador de respuesta se comprometía SIN comprobar el
        # calendario, que a esa hora estaba ocupado.
        if re.search(r'\b(?:el|del|este|esta|proximo|proxima)'
                     r'(?:\s+(?:dia|siguiente|proximo|proxima|otro|entrante))*\s+'
                     + clasificador._normalizar(dia) + r'\b', t):
            return referencia + timedelta(days=((idx - referencia.weekday()) % 7) or 7)
    return None


def fecha_referida_correo(correo, texto_analisis=""):
    """Día del compromiso del que habla un correo, o None si no se puede saber.

    Orden: fecha escrita en el análisis → fecha escrita en el correo →
    palabras relativas ('hoy', 'mañana', 'el viernes').

    Lo relativo se cuenta desde la fecha en que se ENVIÓ el correo, no desde
    la revisión: "presentan hoy" en un correo del martes habla del martes,
    aunque se lea el sábado. El modelo no puede saberlo (el prompt no lleva la
    fecha de envío) y escribía "se hará hoy".
    """
    import datetime as _dt
    envio = _fecha_envio(correo)
    referencia = envio or _dt.datetime.now(ZONA_BOGOTA).date()
    texto_correo = f"{correo.get('asunto', '')} {correo.get('cuerpo') or correo.get('fragmento') or ''}"

    for texto in (texto_analisis, texto_correo):
        fecha = _primera_fecha_explicita(texto, referencia)
        if fecha:
            return fecha
    if envio is None:
        return None
    for resolver in (_fecha_relativa, _fecha_dia_semana):
        for texto in (texto_correo, texto_analisis):
            fecha = resolver(texto, envio)
            if fecha:
                return fecha
    return None


_PATRON_ENLACE_REUNION = re.compile(
    r'\b(?:meet\.google\.com/\S+|zoom\.us/\S+|teams\.microsoft\.com/\S+|'
    r'google\s+meet|zoom|microsoft\s+teams)\b', re.IGNORECASE)
_PATRON_PERIODICIDAD = re.compile(
    r'\b(?:todos\s+los|todas\s+las|cada)\s+\w+(?:\s+y\s+\w+)?', re.IGNORECASE)


# Eventos que se repiten ("Todos los viernes, 3:00 a 4:00 p.m.").
DIAS_RRULE = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
_MARCA_PERIODICIDAD = r'(?:todos\s+los|todas\s+las|cada|los)\s+'


def periodicidad_en_texto(texto):
    """{'freq', 'byday', 'texto'} si el evento se repite, o None.

    Distingue "los viernes" (se repite) de "el viernes" (una sola vez): esa
    's' es toda la diferencia y es lo que dicen los afiches.
    """
    from prompts_stefany import DIAS_ES
    t = clasificador._normalizar(texto or "")

    if re.search(r'\b(?:todos\s+los\s+dias|cada\s+dia|diariamente|a\s+diario)\b', t):
        return {"freq": "DAILY", "byday": [], "texto": "todos los días"}

    dias = []
    for idx, dia in enumerate(DIAS_ES):
        nombre = clasificador._normalizar(dia)
        # "todos los viernes", "cada martes", "los lunes y miércoles"
        if re.search(_MARCA_PERIODICIDAD + r'(?:\w+\s+(?:y\s+)?){0,2}' + nombre + r'\b', t):
            dias.append(idx)
    if not dias:
        return None

    nombres = " y ".join(DIAS_ES[i] for i in dias)
    return {"freq": "WEEKLY", "byday": [DIAS_RRULE[i] for i in dias],
            "texto": f"todos los {nombres}"}


def primera_fecha_recurrente(periodicidad, hora=None, desde=None):
    """Primer día en que toca ese evento, contando desde hoy."""
    import datetime as _dt
    ahora = _dt.datetime.now(ZONA_BOGOTA)
    inicio = desde or ahora.date()
    if periodicidad.get("freq") == "DAILY":
        # Si hoy ya pasó la hora, empieza mañana.
        if hora is not None and inicio == ahora.date() and hora < ahora.hour + ahora.minute / 60:
            return inicio + timedelta(days=1)
        return inicio

    objetivos = {DIAS_RRULE.index(d) for d in periodicidad.get("byday", [])}
    for salto in range(0, 8):
        fecha = inicio + timedelta(days=salto)
        if fecha.weekday() not in objetivos:
            continue
        if salto == 0 and hora is not None and hora < ahora.hour + ahora.minute / 60:
            continue      # hoy es el día, pero la hora ya pasó
        return fecha
    return None


def regla_recurrencia(periodicidad, hasta):
    """Texto RRULE para Google Calendar, con fin (para no repetir para siempre)."""
    partes = [f"FREQ={periodicidad['freq']}"]
    if periodicidad.get("byday"):
        partes.append("BYDAY=" + ",".join(periodicidad["byday"]))
    partes.append(f"UNTIL={hasta.strftime('%Y%m%d')}T235959Z")
    return ["RRULE:" + ";".join(partes)]


def sumar_un_mes(fecha):
    mes = fecha.month + 1
    anio = fecha.year + (mes > 12)
    mes = mes - 12 if mes > 12 else mes
    dia = min(fecha.day, [31, 29 if anio % 4 == 0 and (anio % 100 or anio % 400 == 0) else 28,
                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mes - 1])
    return date(anio, mes, dia)


def datos_clave_de_imagen(texto_ocr):
    """Lo concreto que trae el texto leído de una imagen: cuándo y dónde.

    El modelo tiende a resumir el texto de presentación del correo y a dejar
    fuera lo que estaba en el afiche ("Todos los viernes, 3:00 a 4:00 p.m.,
    Google Meet"), que es justo lo que la usuaria necesita. Esto lo saca por
    reglas para poder añadirlo al resumen.
    """
    if not texto_ocr:
        return ""
    trozos, vistos = [], set()

    def agregar(valor):
        clave = clasificador._normalizar(valor).strip()
        if clave and clave not in vistos:
            vistos.add(clave)
            trozos.append(valor.strip())

    for linea in texto_ocr.split("\n"):
        limpia = " ".join(linea.split())
        if not limpia:
            continue
        m = _PATRON_PERIODICIDAD.search(limpia)
        if m:
            agregar(m.group(0))
        if horas_en_texto(limpia):
            agregar(limpia if len(limpia) <= 40 else limpia[:40].rsplit(" ", 1)[0])
        elif _fechas_mencionadas(limpia):
            agregar(limpia if len(limpia) <= 40 else limpia[:40].rsplit(" ", 1)[0])
        m = _PATRON_ENLACE_REUNION.search(limpia)
        if m:
            agregar(m.group(0))
    return " · ".join(trozos[:4])


def horas_en_texto(texto):
    """Horas (float, 24 h) escritas en lenguaje natural, en orden de aparición.

    "2pm", "2 pm", "2:30 p. m.", "14:00", "a las 2 de la tarde", "10 de la
    mañana". _resolver_horas_texto() solo entiende "2:00 p.m.", que es como
    escribe el modelo, pero no como escriben las personas.
    """
    texto = (texto or "").lower()
    hallazgos = []
    cubierto = []      # tramos ya leídos como rango, para no releerlos mal

    # Rangos: en "3:00 a 4:00 p.m." el p.m. vale para las DOS horas. Sin esto,
    # el afiche del club se leía como las 3 de la MAÑANA.
    for m in re.finditer(r'\b(\d{1,2})(?::(\d{2}))?\s*(?:a|-|–|hasta)\s*'
                         r'(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b\.?', texto):
        h1, mi1, h2, mi2, ampm = m.groups()
        h1, h2 = int(h1), int(h2)
        mi1, mi2 = int(mi1 or 0), int(mi2 or 0)
        if h1 > 12 or h2 > 12 or mi1 > 59 or mi2 > 59:
            continue
        if ampm == 'p':
            h2 += 0 if h2 == 12 else 12
            h1 += 0 if h1 == 12 else (12 if h1 <= h2 - 12 or h1 >= h2 else 12)
        elif ampm == 'a':
            h1 = 0 if h1 == 12 else h1
            h2 = 0 if h2 == 12 else h2
        hallazgos.append((m.start(), h1 + mi1 / 60))
        hallazgos.append((m.start() + 1, h2 + mi2 / 60))
        cubierto.append((m.start(), m.end()))

    def dentro_de_rango(pos):
        return any(ini <= pos < fin for ini, fin in cubierto)

    for m in re.finditer(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\b\.?', texto):
        if dentro_de_rango(m.start()):
            continue
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if h > 12 or mi > 59:
            continue
        if m.group(3) == 'p' and h != 12:
            h += 12
        if m.group(3) == 'a' and h == 12:
            h = 0
        hallazgos.append((m.start(), h + mi / 60))

    for m in re.finditer(r'\b(\d{1,2})(?::(\d{2}))?\s+de\s+la\s+(mañana|manana|tarde|noche)\b', texto):
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if h > 12 or mi > 59:
            continue
        if m.group(3) in ('tarde', 'noche') and h != 12:
            h += 12
        hallazgos.append((m.start(), h + mi / 60))

    # 24 h ("14:00"), salvo que ya se haya leído con a. m./p. m. o "de la tarde".
    usados = {pos for pos, _ in hallazgos}
    for m in re.finditer(r'\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*[ap]\.?\s*m\b)(?!\s+de\s+la\b)', texto):
        if m.start() not in usados and not dentro_de_rango(m.start()):
            hallazgos.append((m.start(), int(m.group(1)) + int(m.group(2)) / 60))

    return [h for _, h in sorted(hallazgos)]


# ---------------------------------------------------------------
# Nombre sugerido para un evento que viene de un correo
# ---------------------------------------------------------------
# Antes el evento se llamaba igual que el asunto, y hay asuntos que no dicen
# nada ("Solicitud"). Si el asunto es genérico se construye un nombre con el
# tipo de evento, con quién y para qué; si es claro, se respeta.
_PREFIJOS_RESPUESTA = re.compile(r'^\s*(?:(?:re|rv|fw|fwd|reenv|res)\s*:\s*)+', re.IGNORECASE)
_ASUNTOS_GENERICOS = {
    "solicitud", "consulta", "pregunta", "hola", "informacion", "duda", "dudas", "favor",
    "urgente", "importante", "asunto", "mensaje", "correo", "reunion", "cita", "recordatorio",
    "saludo", "saludos", "buenos dias", "buenas tardes", "buenas noches", "peticion",
    "requerimiento", "seguimiento", "pendiente", "sin asunto", "(sin asunto)", "novedad",
}
_TIPOS_EVENTO = [
    (r'\bsustentaci', "Sustentación"), (r'\bentrevista', "Entrevista"), (r'\bcita\b', "Cita"),
    (r'\bclase\b', "Clase"), (r'\bcharla', "Charla"), (r'\btaller', "Taller"),
    (r'\basesor', "Asesoría"), (r'\btutori', "Tutoría"), (r'\breun', "Reunión"),
    (r'\bencontr', "Encuentro"),
]
# "Les escribo PARA informarles que..." no es el propósito de la reunión.
_VERBOS_DE_AVISO = ("informar", "comunicar", "contar", "recordar", "avisar", "saludar",
                    "notificar", "confirmar", "preguntar", "saber", "solicitar")
_CONECTORES_NOMBRE = {"de", "del", "la", "las", "los", "y"}
_SEGUNDOS_NOMBRES = {
    "maria", "jose", "pablo", "andres", "alejandro", "alejandra", "fernando", "fernanda",
    "camila", "paola", "andrea", "david", "felipe", "esteban", "sofia", "valentina",
    "carolina", "patricia", "luis", "antonio", "manuel", "isabel", "lucia", "eduardo",
    "alberto", "ernesto", "alduver", "stefany", "juliana", "daniela", "sebastian", "mauricio",
}


# ---------------------------------------------------------------
# Lugar del evento (RF8 y CU3)
# ---------------------------------------------------------------
# Sin esto, un correo que decía "nos cambiamos al salón 302" solo se podía
# informar: el evento se creaba sin sitio y no había nada que actualizar.
# Se exige una de estas palabras para no tomar "en el correo", "en la mañana"
# o "en la dirección que te envié" como si fueran un lugar.
_PALABRAS_LUGAR = (r'sal[oó]n|aula|auditorio|laboratorio|sala|oficina|edificio|bloque|'
                   r'sede|campus|biblioteca|cafeter[ií]a|coliseo|consultorio|piso')

# "en el laboratorio de sistemas", "en el salón 302", "en la sala B".
_PATRON_LUGAR_CON_PREPOSICION = re.compile(
    r'\b(?:en|desde)\s+(?:el|la|los|las)\s+'
    r'((?:' + _PALABRAS_LUGAR + r')\b[^,.;:\n]{0,45})', re.IGNORECASE)

# "Lugar: laboratorio 2", "Ubicación: Auditorio Mayor", "Sitio: sala B".
_PATRON_LUGAR_ETIQUETADO = re.compile(
    r'\b(?:lugar|ubicaci[oó]n|sitio|direcci[oó]n)\s*:\s*([^,.;\n]{2,60})', re.IGNORECASE)

# Sin preposición: "nuevo salón 302", "Salón: 401".
_PATRON_LUGAR_SUELTO = re.compile(
    r'\b((?:' + _PALABRAS_LUGAR + r')\s*(?:n[.º°]?\s*)?[\wº°-]{1,20})', re.IGNORECASE)


def lugar_en_texto(texto):
    """El sitio donde ocurre algo, tal y como lo escribe el correo, o "".

    Se prefiere un lugar físico; si no hay ninguno, vale un enlace de reunión
    (Meet, Zoom, Teams), que en una reunión virtual ES el lugar.
    """
    if not texto:
        return ""
    for patron in (_PATRON_LUGAR_ETIQUETADO, _PATRON_LUGAR_CON_PREPOSICION,
                   _PATRON_LUGAR_SUELTO):
        m = patron.search(texto)
        if m:
            # Las palabras de cierre ("a las", "el viernes") no son del lugar.
            hallado = re.split(r'\s+(?:a\s+las?|el|los|para|con|desde)\b',
                               m.group(1).strip(), maxsplit=1)[0]
            hallado = " ".join(hallado.split()).strip(" -–:")
            # "Ubicación: https://meet.google.com/abc-def" se cortaba en el
            # primer punto y quedaba en "https://meet": un enlace se toma
            # entero o no se toma.
            if "://" in hallado or "www." in hallado:
                enlace = _PATRON_ENLACE_REUNION.search(texto)
                return enlace.group(0)[:120] if enlace else hallado[:120]
            if len(hallado) >= 3:
                return hallado[:80]
    m = _PATRON_ENLACE_REUNION.search(texto)
    return m.group(0)[:80] if m else ""


def _asunto_limpio(asunto):
    return _PREFIJOS_RESPUESTA.sub("", asunto or "").strip()


def _asunto_generico(asunto):
    norm = clasificador._normalizar(asunto).strip(" .:!¡¿?")
    return not norm or norm in _ASUNTOS_GENERICOS


def _nombre_corto_remitente(remitente):
    """'robinson alduver buitrago perez <r@x.com>' → 'Robinson Buitrago'."""
    nombre = (remitente or "").split("<")[0].strip().strip('"').strip()
    if not nombre or "@" in nombre:
        return ""
    partes = [p for p in re.findall(r"[^\W\d_]+", nombre) if p.lower() not in _CONECTORES_NOMBRE]
    if len(partes) >= 4:
        elegidas = [partes[0], partes[2]]       # primer nombre + primer apellido
    elif len(partes) == 3:
        # "Omar Torres Ladino" (nombre + 2 apellidos) o "Ana María Pérez"
        # (2 nombres + apellido): no hay forma segura de saberlo; se mira si la
        # segunda palabra es un segundo nombre frecuente. El campo es editable.
        segundo_es_nombre = clasificador._normalizar(partes[1]) in _SEGUNDOS_NOMBRES
        elegidas = [partes[0], partes[2] if segundo_es_nombre else partes[1]]
    else:
        elegidas = partes
    return " ".join(p.capitalize() for p in elegidas)


def _proposito(texto):
    """'...nos reunamos el sábado a las 2pm para ultimar detalles de la ceremonia' → 'ultimar detalles de la ceremonia'."""
    norm = clasificador._normalizar(texto)
    # Se busca DESPUÉS de la palabra de evento, para no tomar "le escribo para...".
    inicio = min([m.start() for patron, _ in _TIPOS_EVENTO for m in [re.search(patron, norm)] if m] or [0])
    for m in re.finditer(r'\b(?:para|sobre|acerca de|con el fin de|con el objetivo de)\s+([^.\n;?!,]{4,90})',
                         texto[inicio:], re.IGNORECASE):
        frase = m.group(1).strip()
        if clasificador._normalizar(frase).startswith(_VERBOS_DE_AVISO):
            continue
        # Fecha y hora ya van en el evento: no se repiten en el nombre.
        frase = re.split(r'\s+(?:el|este|pr[oó]ximo|a las|hoy|ma[ñn]ana|en la tarde|en la mañana)\b',
                         frase, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if len(frase) >= 4:
            return frase[:70].rsplit(" ", 1)[0] if len(frase) > 70 else frase
    return ""


def titulo_sugerido_evento(correo):
    """Nombre para el evento de un correo. Solo reglas, sin modelo."""
    asunto = _asunto_limpio(correo.get("asunto", ""))
    if not _asunto_generico(asunto):
        return asunto[:100]

    texto = f"{correo.get('cuerpo') or correo.get('fragmento') or ''}"
    norm = clasificador._normalizar(f"{asunto} {texto}")
    tipo = next((nombre for patron, nombre in _TIPOS_EVENTO if re.search(patron, norm)), "Reunión")
    quien = _nombre_corto_remitente(correo.get("remitente", ""))
    para = _proposito(texto)

    titulo = f"{tipo} con {quien}" if quien else tipo
    if para:
        titulo += f": {para}"
    return titulo


def fecha_en_palabras(fecha):
    from prompts_stefany import DIAS_ES
    return f"{DIAS_ES[fecha.weekday()]} {fecha.day} de {MESES_ES[fecha.month - 1]}"


def fecha_ya_paso(fecha):
    import datetime as _dt
    return fecha is not None and fecha < _dt.datetime.now(ZONA_BOGOTA).date()


def ya_paso(fecha, hora=None):
    """True si ese compromiso ya ocurrió, mirando también la HORA.

    Caso real: "¡Empezamos en 10 minutos!" llegó a las 10:50 para las 11:00 y
    a las 3 de la tarde seguía ofreciéndose para agendar. Sin hora conocida un
    compromiso de hoy se considera vigente: puede ser más tarde.
    """
    import datetime as _dt
    if fecha is None:
        return False
    ahora = _dt.datetime.now(ZONA_BOGOTA)
    if fecha < ahora.date():
        return True
    if fecha == ahora.date() and hora is not None:
        return hora < ahora.hour + ahora.minute / 60
    return False


# Palabras que no identifican a un evento concreto: "Electiva III clase hoy
# viernes" y "Electiva III" deben coincidir por 'electiva', no por 'clase'.
_PALABRAS_GENERICAS = {
    'clase', 'clases', 'sesion', 'reunion', 'hoy', 'manana', 'para', 'sobre',
    'lunes', 'martes', 'miercoles', 'jueves', 'viernes', 'sabado', 'domingo',
    'semana', 'grupo', 'todos', 'nuestra', 'nuestro', 'esta', 'este', 'desde',
    'hasta', 'correo', 'importante', 'aviso', 'cancelada', 'cancelado',
} | {m.lower() for m in MESES_ES}
_ROMANOS = {'i', 'ii', 'iii', 'iv', 'v', 'vi', 'vii', 'viii', 'ix', 'x'}


def _palabras_clave(texto):
    palabras = re.findall(r'\w+', clasificador._normalizar(texto))
    clave = {p for p in palabras if len(p) >= 4 and p not in _PALABRAS_GENERICAS and not p.isdigit()}
    return clave, {p for p in palabras if p in _ROMANOS}


def fechas_candidatas_correo(correo, texto_extra=""):
    """Todas las fechas que podría estar señalando un correo, en orden de confianza.

    Un mismo correo puede apuntar a varias: el asunto "Re: Reunión hoy" y el
    cuerpo "no me es posible reunirme el día lunes". Antes se tomaba la
    primera y se perdía la buena; ahora se devuelven todas y quien llama
    decide (por ejemplo, quedándose con la que sí tiene un evento).
    """
    envio = _fecha_envio(correo)
    referencia = envio or _hoy()
    cuerpo = correo.get('cuerpo') or correo.get('fragmento') or ''
    asunto = correo.get('asunto', '')
    candidatas = []

    def agregar(fecha):
        if fecha is not None and fecha not in candidatas:
            candidatas.append(fecha)

    # Primero el cuerpo: el asunto suele venir heredado del hilo ("Re: ...").
    for texto in (cuerpo, texto_extra, asunto):
        agregar(_primera_fecha_explicita(texto, referencia))
    if envio is not None:
        for texto in (cuerpo, texto_extra, asunto):
            agregar(_fecha_dia_semana(texto, envio))
        for texto in (cuerpo, texto_extra, asunto):
            agregar(_fecha_relativa(texto, envio))
    return candidatas


def _hoy():
    import datetime as _dt
    return _dt.datetime.now(ZONA_BOGOTA).date()


def buscar_evento_cancelado(correo, eventos_por_cuenta):
    """El evento del calendario que un correo de cancelación anula, o None.

    Se prueban TODAS las fechas que menciona el correo y se elige aquella en
    la que hay un evento parecido: así "no me es posible reunirme el día
    lunes" gana sobre el "hoy" heredado del asunto.

    Para reconocer el evento se usan las palabras del asunto Y el nombre de
    quien escribe: los eventos creados desde un correo se llaman "Reunión con
    Robinson Buitrago…". Si el título del evento lleva numeral romano, el
    asunto también tiene que llevarlo ('Electiva II' no es 'Electiva III').
    Ante la duda no devuelve nada: borrar el evento equivocado es peor.
    """
    señas = _senas_del_correo(correo)
    for fecha in fechas_candidatas_correo(correo, correo.get('cuerpo_hilo_anterior', '')):
        if fecha_ya_paso(fecha):
            continue
        mejor, mejor_puntaje = None, 0
        for ev in _eventos_en_fecha(fecha, eventos_por_cuenta or {}):
            puntaje = _puntaje_evento(señas, ev)
            if puntaje > mejor_puntaje:
                mejor, mejor_puntaje = ev, puntaje
        if mejor:
            return dict(mejor, fecha=fecha)
    return None


def _senas_del_correo(correo):
    """Con qué se reconoce el evento del que habla un correo."""
    asunto = _asunto_limpio(correo.get('asunto', ''))
    clave_asunto, romanos_asunto = _palabras_clave(asunto)
    clave_persona, _ = _palabras_clave(_nombre_corto_remitente(correo.get('remitente', '')))
    return {
        'claves': clave_asunto | clave_persona,
        'romanos': romanos_asunto,
        'remitente': (correo.get('remitente_email') or '').lower().strip(),
        'asunto_norm': clasificador._normalizar(asunto).strip(),
    }


def _puntaje_evento(señas, ev):
    """Cuánto se parece un evento del calendario a lo que dice el correo (0 = nada).

    De más fiable a menos: la descripción del evento guarda de qué correo
    salió; luego las palabras del asunto o del nombre de quien escribe; y por
    último un título igual, para casos como "Reunión hoy", donde todas las
    palabras son genéricas.
    """
    if not ev.get('id'):
        return 0
    clave_ev, romanos_ev = _palabras_clave(ev['titulo'])
    if not romanos_ev <= señas['romanos']:
        return 0
    if señas['remitente'] and señas['remitente'] in (ev.get('descripcion') or '').lower():
        return 100
    if clave_ev & señas['claves']:
        return 10 + len(clave_ev & señas['claves'])
    titulo_norm = clasificador._normalizar(ev['titulo']).strip()
    if titulo_norm and señas['asunto_norm'] and (titulo_norm == señas['asunto_norm']
                                                 or titulo_norm in señas['asunto_norm']
                                                 or señas['asunto_norm'] in titulo_norm):
        return 5
    return 0


def buscar_evento_relacionado(correo, eventos_por_cuenta):
    """El evento del calendario del que habla un correo, sea cual sea su día (CU3).

    A diferencia de la cancelación, un correo que reprograma algo suele
    mencionar la fecha NUEVA, no la del evento que hay que mover. Por eso aquí
    se miran todos los eventos próximos y se elige el más parecido; a igual
    parecido, el más cercano en el tiempo.
    """
    import datetime as _dt
    señas = _senas_del_correo(correo)
    hoy = _hoy()
    mejor, mejor_puntaje, mejor_fecha = None, 0, None

    for alias, eventos in (eventos_por_cuenta or {}).items():
        for e in eventos:
            try:
                inicio = _dt.datetime.fromisoformat(e['inicio'])
            except (KeyError, ValueError):
                continue
            if inicio.date() < hoy:
                continue
            try:
                fin = _dt.datetime.fromisoformat(e['fin'])
            except (KeyError, ValueError):
                fin = inicio + timedelta(hours=1)
            candidato = {
                'alias': alias, 'titulo': e.get('titulo', ''), 'id': e.get('id'),
                'descripcion': e.get('descripcion', ''),
                # Sin el lugar actual, un correo que repetía el sitio que el
                # evento ya tenía se leía como un cambio de lugar.
                'lugar': e.get('lugar', ''),
                'inicio_h': inicio.hour + inicio.minute / 60,
                'fin_h': fin.hour + fin.minute / 60,
                'fecha': inicio.date(),
            }
            puntaje = _puntaje_evento(señas, candidato)
            if puntaje > mejor_puntaje or (puntaje and puntaje == mejor_puntaje
                                           and candidato['fecha'] < mejor_fecha):
                mejor, mejor_puntaje, mejor_fecha = candidato, puntaje, candidato['fecha']
    return mejor


def _solapan(ini1, fin1, ini2, fin2):
    return ini1 < fin2 and ini2 < fin1


def verificar_conflicto_horario(texto_analisis, eventos_por_cuenta):
    if "📊 Análisis:" not in texto_analisis:
        return texto_analisis

    resumen, analisis = texto_analisis.split("📊 Análisis:", 1)

    m_fecha = re.search(r'\d{1,2}\s+de\s+(?:' + '|'.join(MESES_ES) + r')', analisis, re.IGNORECASE)
    if not m_fecha:
        return texto_analisis
    fecha_hallazgo = _resolver_fecha_texto(m_fecha.group(0))
    if not fecha_hallazgo:
        return texto_analisis

    horas = _resolver_horas_texto(analisis)
    if not horas:
        return texto_analisis
    hora_ini_nuevo = horas[0]
    hora_fin_nuevo = horas[1] if len(horas) > 1 else hora_ini_nuevo + 1

    m_alias = re.search(PATRON_ALIAS, analisis)
    alias_origen = m_alias.group(1) if m_alias else "tu cuenta"

    eventos_ese_dia = _eventos_en_fecha(fecha_hallazgo, eventos_por_cuenta)
    conflicto_real = next(
        (ev for ev in eventos_ese_dia
         if _solapan(hora_ini_nuevo, hora_fin_nuevo, ev['inicio_h'], ev['fin_h'])),
        None
    )

    dice_conflicto = "¿cancelo" in analisis.lower()

    if dice_conflicto and conflicto_real is None:
        analisis_corregido = (
            f"- [{alias_origen}] Este compromiso no está registrado en el calendario y no "
            f"se cruza con ningún evento existente.\n"
            f"¿Te gustaría que lo agende en el calendario de {alias_origen}? (sí/no)"
        )
        return f"{resumen.strip()}\n\n📊 Análisis:\n{analisis_corregido}"

    if not dice_conflicto and conflicto_real is not None and "¿te gustaría que lo agende" in analisis.lower():
        analisis_corregido = (
            f"- [{alias_origen}] Este compromiso se cruza con un evento ya agendado el "
            f"{m_fecha.group(0)}:\n"
            f"- [{conflicto_real['alias']}] '{conflicto_real['titulo']}' ya está agendado.\n"
            f"¿Cancelo '{conflicto_real['titulo']}' o continúo con este nuevo compromiso? "
            f"Puedo redactar el correo de cancelación."
        )
        return f"{resumen.strip()}\n\n📊 Análisis:\n{analisis_corregido}"

    return texto_analisis


# Antes solo se reconocía "12 de septiembre". El comprobante del Banco de
# Bogotá dice "Septiembre 12 del 2026 - 2:03 p. m.", así que un resumen que
# citaba bien "12 de septiembre" se descartaba por fecha inventada. Ahora las
# fechas se comparan como (día, mes), vengan escritas como vengan.
_MESES_PATRON = {
    'enero': 1, 'ene': 1, 'january': 1, 'jan': 1,
    'febrero': 2, 'feb': 2, 'february': 2,
    'marzo': 3, 'mar': 3, 'march': 3,
    'abril': 4, 'abr': 4, 'april': 4, 'apr': 4,
    'mayo': 5, 'may': 5,
    'junio': 6, 'jun': 6, 'june': 6,
    'julio': 7, 'jul': 7, 'july': 7,
    'agosto': 8, 'ago': 8, 'august': 8, 'aug': 8,
    'septiembre': 9, 'setiembre': 9, 'sept': 9, 'sep': 9, 'september': 9,
    'octubre': 10, 'oct': 10, 'october': 10,
    'noviembre': 11, 'nov': 11, 'november': 11,
    'diciembre': 12, 'dic': 12, 'december': 12, 'dec': 12,
}
_MES = r'(' + '|'.join(sorted(_MESES_PATRON, key=len, reverse=True)) + r')\.?'
_PATRONES_FECHA = (
    # 12 de septiembre · 12 septiembre · 12 sep
    ('dm', re.compile(r'\b(\d{1,2})\s+(?:de\s+)?' + _MES + r'(?!\w)', re.IGNORECASE)),
    # Septiembre 12 del 2026 · Sep 12
    ('md', re.compile(r'(?<!\w)' + _MES + r'\s+(\d{1,2})\b', re.IGNORECASE)),
    # 12/09/2026 · 12-09-26 · 12.09.2026
    ('num', re.compile(r'\b(\d{1,2})[/.-](\d{1,2})[/.-](?:\d{4}|\d{2})\b')),
    # 2026-09-12 · 2026/09/12
    ('iso', re.compile(r'\b\d{4}[/.-](\d{1,2})[/.-](\d{1,2})\b')),
)


def _fechas_mencionadas(texto):
    """Fechas del texto como conjunto de (día, mes)."""
    fechas = set()

    def agregar(dia, mes):
        if 1 <= dia <= 31 and 1 <= mes <= 12:
            fechas.add((dia, mes))

    for tipo, patron in _PATRONES_FECHA:
        for g in patron.findall(texto or ""):
            if tipo == 'dm':
                agregar(int(g[0]), _MESES_PATRON[g[1].lower()])
            elif tipo == 'md':
                agregar(int(g[1]), _MESES_PATRON[g[0].lower()])
            elif tipo == 'num':
                # En Colombia es día/mes; si ambos caben, se aceptan los dos
                # órdenes (mejor dejar pasar que descartar un buen resumen).
                agregar(int(g[0]), int(g[1]))
                agregar(int(g[1]), int(g[0]))
            else:
                agregar(int(g[1]), int(g[0]))
    return fechas


def _menciona_fecha_inventada(texto, correo):
    """True si el texto cita fechas y NINGUNA aparece en el correo real."""
    fechas = _fechas_mencionadas(texto)
    if not fechas:
        return False
    texto_correo = (f"{correo.get('asunto', '')} {correo.get('cuerpo', '')} "
                    f"{correo.get('fragmento', '')} {correo.get('cuerpo_hilo_anterior', '')}")
    return not (fechas & _fechas_mencionadas(texto_correo))


_DIA_SEMANA = r'(lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo)'
_FECHA_ES = r'(\d{1,2})\s+de\s+(' + '|'.join(MESES_ES) + r')(?:\s+del?\s+(\d{4}))?'
_PATRON_DIA_ANTES = re.compile(_DIA_SEMANA + r'(\s*,?\s*)' + _FECHA_ES, re.IGNORECASE)
_PATRON_DIA_DESPUES = re.compile(_FECHA_ES + r'(\s*[(,]?\s*)' + _DIA_SEMANA + r'\b', re.IGNORECASE)


def _corregir_dia_semana(texto):
    """Corrige el día de la semana que el modelo pone mal junto a una fecha.

    Caso real: "12 de septiembre (lunes)" cuando el 12 de septiembre de 2026
    es sábado. La fecha era correcta; tirar el resumen entero por el día de
    la semana era perder información buena. Se corrige con el calendario real.
    """
    from prompts_stefany import DIAS_ES

    def dia_correcto(dia, mes, anio, escrito):
        try:
            if anio:
                fecha = date(int(anio), MESES_ES.index(mes.lower()) + 1, int(dia))
            else:
                fecha = _resolver_fecha_texto(f"{dia} de {mes}")
        except ValueError:
            fecha = None
        if fecha is None:
            return escrito
        correcto = DIAS_ES[fecha.weekday()]
        if clasificador._normalizar(correcto) != clasificador._normalizar(escrito):
            print(f"📅 Día de la semana corregido: {escrito} {dia} de {mes} → {correcto}")
        return correcto.capitalize() if escrito[:1].isupper() else correcto

    texto = _PATRON_DIA_ANTES.sub(
        lambda m: dia_correcto(m.group(3), m.group(4), m.group(5), m.group(1))
        + m.group(2) + m.group(0)[len(m.group(1)) + len(m.group(2)):],
        texto)
    texto = _PATRON_DIA_DESPUES.sub(
        lambda m: m.group(0)[:m.start(5) - m.start(0)]
        + dia_correcto(m.group(1), m.group(2), m.group(3), m.group(5)),
        texto)
    return texto


_PATRON_ISO_COMPLETO = re.compile(
    r'\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?')


def _convertir_fechas_iso(texto):
    """'2026-11-27T23:59:00-05:00' → 'viernes 27 de noviembre de 2026, 11:59 p.m.'

    Antes una fecha ISO bastaba para descartar el resumen entero. Convertida,
    pasa por las mismas comprobaciones de fecha y hora que el resto: si no
    está en el correo (suele venir copiada del calendario del prompt), se
    descarta igual.
    """
    from prompts_stefany import formatear_fecha_legible
    import datetime as _dt

    def convertir(m):
        try:
            dt = _dt.datetime.fromisoformat(m.group(0).replace('Z', '+00:00'))
            if dt.tzinfo is not None:
                dt = dt.astimezone(ZONA_BOGOTA)
            return formatear_fecha_legible(dt.replace(tzinfo=None).isoformat())
        except ValueError:
            return m.group(0)

    return _PATRON_ISO_COMPLETO.sub(convertir, texto)


def _normalizar_fechas_modelo(texto):
    texto = _corregir_dia_semana(_convertir_fechas_iso(texto or ""))
    # "2:03 p.m.." al final de una frase: el punto de la abreviatura basta.
    return re.sub(r'([ap]\.\s?m\.)\.', r'\1', texto)


_PATRON_HORA = re.compile(
    r'\b(\d{1,2})(?::(\d{2}))?\s*(?:a\.?\s?m\b\.?|p\.?\s?m\b\.?|m\.)'   # 7 p.m., 2:03 p. m., 12 m.
    r'|\b(\d{1,2}):(\d{2})\b',                                           # 19:00
    re.IGNORECASE
)


def _horas_mencionadas(texto):
    """Horas como (hora % 12, minutos).

    Se ignora a.m./p.m. a propósito: "19:00" y "7:00 p.m." son la misma hora,
    y es preferible dejar pasar un a.m. por p.m. que descartar un buen resumen.
    Lo que se busca atrapar son horas que no están en el correo en absoluto.
    """
    horas = set()
    for h1, m1, h2, m2 in _PATRON_HORA.findall(texto or ""):
        h, m = (h1, m1) if h1 else (h2, m2)
        if int(h) <= 24 and int(m or 0) < 60:
            horas.add((int(h) % 12, int(m or 0)))
    return horas


def _menciona_hora_inventada(texto, correo):
    """True si el texto cita una hora que no aparece en el correo real.

    Caso real: la cancelación de una clase se resumió como "la hora de inicio
    cambia de 19:00 p.m. a 12:00 p.m."; ninguna de las dos estaba en el correo
    (la primera venía del calendario incluido en el prompt).
    """
    horas = _horas_mencionadas(texto)
    if not horas:
        return False
    texto_correo = (f"{correo.get('asunto', '')} {correo.get('cuerpo', '')} "
                    f"{correo.get('fragmento', '')} {correo.get('cuerpo_hilo_anterior', '')}")
    return not horas <= _horas_mencionadas(texto_correo)


# Frases de las plantillas del bloque de análisis. Si aparecen en el RESUMEN
# es que el modelo escribió el análisis sin su encabezado.
_FRASES_PLANTILLA_ANALISIS = re.compile(
    r'te recomiendo actualizar el evento|¿?\s*te gustar[ií]a que lo agende|¿\s*cancelo\b|'
    r'cambio real sobre un evento|evento ya agendado',
    re.IGNORECASE
)


def _quitar_plantillas_del_resumen(resumen):
    lineas = [l for l in resumen.split('\n') if not _FRASES_PLANTILLA_ANALISIS.search(l)]
    return '\n'.join(lineas).strip()


def _texto_del_correo(correo):
    return clasificador._normalizar(
        f"{correo.get('asunto', '')} {correo.get('cuerpo', '')} {correo.get('fragmento', '')} "
        f"{correo.get('cuerpo_hilo_anterior', '')}")


# E — Lugares. Caso real: el correo no decía dónde y el resumen añadió "en el
# laboratorio de sistemas". Se quita la frase de lugar si esa palabra no está
# en el correo; el resto del resumen (fecha, hora, tema) se conserva.
_PATRON_LUGAR = re.compile(
    r',?\s*\ben\s+(?:el|la|los|las|un|una)\s+'
    r'(laboratorio|sal[oó]n|aula|auditorio|oficina|sala|biblioteca|bloque|edificio|sede|'
    r'cafeter[ií]a|coliseo|plazoleta|consultorio)s?\b[^.,;:\n]*',
    re.IGNORECASE)


def _quitar_lugares_inventados(texto, correo):
    texto_correo = _texto_del_correo(correo)

    def quitar(m):
        lugar = clasificador._normalizar(m.group(1))
        if re.search(r'\b' + re.escape(lugar), texto_correo):
            return m.group(0)
        print(f'✂️ Lugar inventado quitado para «{correo.get("asunto", "")[:40]}»: {m.group(0).strip()!r}')
        return ""

    limpio = _PATRON_LUGAR.sub(quitar, texto)
    return re.sub(r'\s+([.,;:])', r'\1', limpio).strip()


# F — Confirmaciones. Caso real: el correo PREGUNTABA ("quisiera saber si es
# posible que nos reunamos...") y el resumen afirmó "Queda fijada la cita".
# Solo se corrige si el correo pregunta y no confirma nada él mismo.
_MARCAS_PETICION = re.compile(
    r'es posible|quisiera saber|podriamos|podemos reunirnos|le parece|te parece|seria posible|'
    r'tendria disponibilidad|tiene disponibilidad|\?')
# Si el propio correo afirma que algo quedó fijado ("¿Tienen dudas? La
# sustentación queda programada para el lunes"), el resumen tiene razón.
_MARCAS_CONFIRMACION_EN_CORREO = re.compile(
    r'confirmo|confirmamos|queda(?:n)?\s+(?:fijad|confirmad|programad|agendad|establecid)'
    r'|quedo\s+(?:fijad|confirmad|programad|agendad)|se\s+(?:ha\s+)?(?:fij|confirm|program|agend)(?:ado|o)\b')
_PATRON_AFIRMA_CONFIRMADO = re.compile(
    r'\b(?:queda(?:n)?\s+(?:fijad|confirmad|programad|agendad|establecid)[ao]s?'
    r'|se\s+(?:ha\s+)?(?:fij|confirm|program|agend)(?:ado|ó))\s+',
    re.IGNORECASE)


def _corregir_confirmacion_inventada(texto, correo):
    texto_correo = _texto_del_correo(correo)
    if not _MARCAS_PETICION.search(texto_correo) or _MARCAS_CONFIRMACION_EN_CORREO.search(texto_correo):
        return texto

    def reemplazo(m):
        inicio_frase = m.start() == 0 or texto[:m.start()].rstrip().endswith(('.', ':', '\n'))
        print(f'✂️ Confirmación inventada corregida para «{correo.get("asunto", "")[:40]}»: {m.group(0).strip()!r}')
        return "Propone " if inicio_frase else "propone "

    return _PATRON_AFIRMA_CONFIRMADO.sub(reemplazo, texto)


def _aplicar_cancelacion(resumen, analisis, correo):
    """Correos que cancelan un evento: el análisis del modelo se ignora.

    Qué hacer con el calendario lo decide router_stefany.acciones_calendario()
    por reglas. Y si el resumen ni siquiera menciona la cancelación, no sirve:
    se sustituye por uno local.
    """
    texto_correo = correo.get('cuerpo') or correo.get('fragmento') or ''
    if not clasificador.anuncia_cancelacion(correo.get('asunto', ''), texto_correo):
        return resumen, analisis
    if not re.search(r'cancel|suspend|no habr|no tendr|no hay|interrupci|no podr|no pued',
                     clasificador._normalizar(resumen)):
        resumen = f'Avisa que no se realizará: «{_asunto_limpio(correo.get("asunto", ""))}».'
    return resumen, ""


# El modelo a veces responde en inglés pese a que el prompt exige español
# (visto en producción: un correo de CrewAI resumido como "How far up AI
# ladder does your company stand?"). Dos de estas palabras sueltas bastan
# para delatarlo sin falsos positivos en español.
_PALABRAS_INGLESAS = {'the', 'your', 'this', 'how', 'are', 'with', 'and',
                      'you', 'from', 'that', 'have', 'about', 'their'}


def _parece_otro_idioma(texto):
    palabras = set(re.findall(r'\b[a-z]+\b', texto.lower()))
    return len(palabras & _PALABRAS_INGLESAS) >= 2


def _cortar_fugas(texto):
    """Corta el texto en la primera fuga del prompt de entrenamiento."""
    posiciones = []
    for patron in PATRONES_FUGA:
        m = patron.search(texto)
        if m:
            posiciones.append(m.start())
    return texto[:min(posiciones)].rstrip() if posiciones else texto


def _separar_analisis(texto):
    """Divide en (resumen, analisis) aceptando el encabezado deformado."""
    m = PATRON_ANALISIS.search(texto)
    if not m:
        return texto, ""
    return texto[:m.start()], texto[m.end():]


# TAREA C — Hay dos clases de problema y antes se trataban igual:
#   INVENCIÓN  → el texto afirma algo falso. Se descarta entero.
#   FORMATO    → el texto puede estar bien, solo es largo o mal partido.
#                Se recorta, no se tira.
MOTIVOS_INVENCION = {
    "cita una cuenta que no existe",
    "cita una fecha que no está en el correo",
    "cita una hora que no está en el correo",
    "fuga de fecha ISO",
    "no está en español",
    "arrastra el prompt de entrenamiento",
}


def _motivos_invalidez(texto, correo, verificar_fechas=True, verificar_horas=False):
    """Devuelve la lista de motivos por los que un texto no es fiable.

    Las horas solo se verifican en el RESUMEN: el análisis puede citar con
    razón la hora de un evento del calendario al explicar un cruce.
    """
    motivos = []
    if not texto or len(texto) < 12:
        return ["vacío o demasiado corto"]

    aliases_reales = set(gc.cuentas_conectadas.keys())
    if not set(re.findall(PATRON_ALIAS, texto)) <= aliases_reales:
        motivos.append("cita una cuenta que no existe")
    if re.search(r'\d{4}-\d{2}-\d{2}T\d{2}', texto):
        motivos.append("fuga de fecha ISO")
    if any(p.search(texto) for p in PATRONES_FUGA):
        motivos.append("arrastra el prompt de entrenamiento")
    if verificar_fechas and _menciona_fecha_inventada(texto, correo):
        motivos.append("cita una fecha que no está en el correo")
    if verificar_horas and _menciona_hora_inventada(texto, correo):
        motivos.append("cita una hora que no está en el correo")
    if _parece_otro_idioma(texto) and not _parece_otro_idioma(
            f"{correo.get('asunto', '')} {correo.get('cuerpo', '')}"):
        motivos.append("no está en español")

    # Formato: van al final para que un motivo de invención tenga prioridad.
    if len(texto) > 320:
        motivos.append("demasiado largo")
    if texto.count('\n') > 2:
        motivos.append("demasiadas líneas")
    return motivos


def _recortar(texto, maximo=320):
    """Recorta un resumen largo respetando el final de la frase.

    Un resumen de 500 caracteres suele tener la información correcta en las
    primeras frases; descartarlo entero y poner 'Correo de X sobre Y' era
    perder contenido bueno por un problema de formato.
    """
    texto = ' '.join(texto.split('\n')[:3]).strip()
    if len(texto) <= maximo:
        return texto
    corte = texto[:maximo]
    for signo in ('. ', '? ', '! ', '; '):
        pos = corte.rfind(signo)
        if pos > 80:
            return corte[:pos + 1].strip()
    return corte.rsplit(' ', 1)[0].strip() + '...'


def _resumen_respaldo(correo):
    remitente_corto = correo.get('remitente', '').split('<')[0].strip() or correo.get('remitente', '')
    return f'Correo de {remitente_corto} sobre "{correo.get("asunto", "")}".'


def _sanear_texto_modelo(texto_modelo, correo):
    texto = _cortar_fugas(texto_modelo)
    resumen, analisis = _separar_analisis(texto)
    resumen = _normalizar_fechas_modelo(_quitar_plantillas_del_resumen(resumen.strip()))
    resumen = _corregir_confirmacion_inventada(_quitar_lugares_inventados(resumen, correo), correo)
    analisis = _normalizar_fechas_modelo(analisis.strip())
    if not resumen:
        resumen = _resumen_respaldo(correo)

    motivos = _motivos_invalidez(resumen, correo, verificar_horas=True)
    invencion = [m for m in motivos if m in MOTIVOS_INVENCION]

    if invencion:
        print(f'🛡️ Resumen descartado ({invencion[0]}) para «{correo.get("asunto","")[:40]}»')
        print(f'   ↳ texto: {resumen[:200]!r}')
        resumen = _resumen_respaldo(correo)
    elif motivos:
        # Solo problemas de formato: se recorta y se vuelve a comprobar.
        recortado = _recortar(resumen)
        if [m for m in _motivos_invalidez(recortado, correo, verificar_horas=True)
                if m in MOTIVOS_INVENCION]:
            resumen = _resumen_respaldo(correo)
        else:
            print(f'✂️ Resumen recortado ({motivos[0]}) para «{correo.get("asunto","")[:40]}»')
            resumen = recortado

    if analisis:
        lineas_guion = [l for l in analisis.split('\n') if l.strip().startswith('-')]
        if lineas_guion and len(lineas_guion) > 2:
            analisis = ""
        elif [m for m in _motivos_invalidez(analisis, correo) if m in MOTIVOS_INVENCION]:
            analisis = ""

    resumen, analisis = _aplicar_cancelacion(resumen, analisis, correo)

    return f"{resumen}\n\n📊 Análisis:\n{analisis}" if analisis else resumen


# TAREA A — La validación solo corría al ESCRIBIR en caché. Los resúmenes
# generados antes de que existiera seguían mostrándose tal cual, con cuentas
# inventadas y bloques de calendario alucinados. Ahora también se validan al
# LEER, con las comprobaciones que no necesitan el cuerpo del correo (que en
# ese momento aún no se ha descargado).
def sanear_resumen_guardado(datos, correo):
    """Revalida una entrada de caché. Devuelve (datos, se_modifico)."""
    resumen = (datos or {}).get("resumen", "")
    analisis = (datos or {}).get("analisis", "")

    # Las entradas antiguas se guardaron con el corte de fugas y la división
    # del bloque de análisis ya hechos (mal): si el modelo escribió
    # "📉 AnálISIS:" o "EVENTOs EN CALENDRIO", eso quedó DENTRO del resumen.
    # Hay que volver a cortar y a partir antes de validar.
    resumen = _cortar_fugas(resumen)
    resumen_nuevo, analisis_extra = _separar_analisis(resumen)
    reestructurado = False
    if analisis_extra.strip():
        resumen = resumen_nuevo.strip()
        analisis = (analisis_extra.strip() + "\n" + analisis).strip()
        reestructurado = True

    resumen_fechas = _normalizar_fechas_modelo(resumen)
    analisis_fechas = _normalizar_fechas_modelo(analisis)
    if (resumen_fechas, analisis_fechas) != (resumen, analisis):
        resumen, analisis = resumen_fechas, analisis_fechas
        reestructurado = True

    # Sin cuerpo descargado no se pueden verificar las fechas: se omite esa
    # comprobación en vez de dar por inventada una fecha que sí estaba.
    motivos = _motivos_invalidez(resumen, correo, verificar_fechas=False)
    invencion = [m for m in motivos if m in MOTIVOS_INVENCION]

    cambiado = reestructurado
    if invencion:
        print(f'🛡️ Resumen en caché descartado ({invencion[0]}) para «{correo.get("asunto","")[:40]}»')
        resumen = _resumen_respaldo(correo)
        analisis = ""
        cambiado = True
    elif motivos:
        resumen = _recortar(resumen)
        cambiado = True

    if analisis and [m for m in _motivos_invalidez(analisis, correo, verificar_fechas=False)
                     if m in MOTIVOS_INVENCION]:
        analisis = ""
        cambiado = True

    # Plantillas en el resumen y cancelaciones: se pueden revisar sin el
    # cuerpo (la cancelación se busca en el asunto y el fragmento). Las horas
    # no, por la misma razón que las fechas.
    resumen_limpio = _quitar_plantillas_del_resumen(resumen) or _resumen_respaldo(correo)
    resumen_limpio, analisis_limpio = _aplicar_cancelacion(resumen_limpio, analisis, correo)
    if (resumen_limpio, analisis_limpio) != (resumen, analisis):
        resumen, analisis = resumen_limpio, analisis_limpio
        cambiado = True

    return {"resumen": resumen, "analisis": analisis}, cambiado


def contexto_calendario_completo(eventos_por_cuenta):
    lineas = []
    for alias, eventos in eventos_por_cuenta.items():
        for e in eventos:
            lineas.append(f"- [{alias}] {e['titulo']} | Inicio: {e['inicio']} | Fin: {e['fin']}")
    if not lineas:
        return "(No hay eventos próximos en ninguna cuenta)"
    return "\n" + "\n".join(lineas) + "\n"


def extraer_datos_cancelacion(texto_analisis):
    m_titulo = re.search(r"¿[Cc]ancelo\s+'([^']+)'\s+o\s+contin[uú]o", texto_analisis)
    if not m_titulo:
        return None
    titulo = m_titulo.group(1)
    for linea in texto_analisis.split('\n'):
        if titulo in linea:
            m_alias = re.search(PATRON_ALIAS, linea)
            if m_alias:
                return {'titulo': titulo, 'alias': m_alias.group(1)}
    return {'titulo': titulo, 'alias': None}


def extraer_datos_evento(texto_respuesta):
    datos = {}
    m = re.search(PATRON_ALIAS, texto_respuesta)
    if m:
        datos['cuenta'] = m.group(1)
    m = re.search(
        r'(\d{1,2}\s+de\s+(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|'
        r'septiembre|octubre|noviembre|diciembre))',
        texto_respuesta, re.IGNORECASE
    )
    if m:
        datos['fecha_texto'] = m.group(1)
    m = re.search(r'(\d{1,2}:\d{2}\s*(?:a\.m\.|p\.m\.))', texto_respuesta, re.IGNORECASE)
    if m:
        datos['hora_texto'] = m.group(1)
    m = re.search(r'(?:sobre|acerca de|para tratar|de)\s+([^,.]+)', texto_respuesta, re.IGNORECASE)
    if m:
        datos['tema'] = m.group(1).strip()[:60]
    return datos if 'cuenta' in datos else None


def limpiar_borrador(texto):
    """Deja el borrador de respuesta listo para mostrar.

    El modelo a veces devuelve el texto entre comillas, con un 'Asunto:'
    delante o con una coletilla explicando lo que hizo, aunque el prompt lo
    prohíba. Aquí se quita todo eso.
    """
    if not texto:
        return ""

    texto = _cortar_fugas(texto.strip())

    # Comillas envolviendo el correo entero
    if len(texto) > 2 and texto[0] in '"“«' and texto[-1] in '"”»':
        texto = texto[1:-1].strip()

    # Línea de asunto que el prompt pide no incluir
    texto = re.sub(r'^\s*(asunto|subject)\s*:.*\n+', '', texto, flags=re.IGNORECASE)

    # Coletillas del asistente antes o después del correo
    texto = re.sub(r'^\s*(aqu[ií] (te dejo|tienes)|esta (ser[ií]a|es) (mi|la) respuesta)'
                   r'[^\n]*\n+', '', texto, flags=re.IGNORECASE)
    texto = re.sub(r'\n+\s*(espero que (te sirva|sea de ayuda)|¿(necesitas|quieres) que'
                   r'[^\n]*)\s*$', '', texto, flags=re.IGNORECASE)

    lineas = [l.rstrip() for l in texto.split('\n')]
    while lineas and not lineas[0].strip():
        lineas.pop(0)
    while lineas and not lineas[-1].strip():
        lineas.pop()
    return '\n'.join(lineas).strip()
