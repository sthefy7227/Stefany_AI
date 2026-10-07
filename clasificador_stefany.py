# -*- coding: utf-8 -*-
"""
clasificador_stefany.py — Clasificación de correos SIN modelo.

Decide categoría y señales a partir de encabezados, remitente y asunto (más
el fragmento del cuerpo si está disponible). Corre en microsegundos y no
gasta un solo token, así que puede aplicarse a la bandeja completa por
grande que sea.

El modelo solo se usa después, y solo sobre los correos que este módulo
marca como dignos de resumen.

Diseño validado contra 15 correos reales de la usuaria. Hallazgo clave:
dentro del mismo dominio universitario, 'List-Unsubscribe' separa limpiamente
el correo institucional masivo (mercadeo) del correo humano (profesores,
coordinación). Los encabezados 'Precedence' y 'Auto-Submitted' NO aparecen
en la práctica, así que no se usan como señal principal.
"""

import re
import unicodedata

# ---------------------------------------------------------------
# Categorías (una sola por correo — define el acordeón donde cae)
# ---------------------------------------------------------------
CATEGORIAS = {
    "academico":      {"etiqueta": "Académico",      "icono": "🎓", "prioridad": 2},
    "laboral":        {"etiqueta": "Laboral",        "icono": "💼", "prioridad": 2},
    "personal":       {"etiqueta": "Personal",       "icono": "✉️", "prioridad": 1},
    "transaccional":  {"etiqueta": "Transaccional",  "icono": "🔔", "prioridad": 3},
    "promocional":    {"etiqueta": "Promocional",    "icono": "📢", "prioridad": 5},
    "redes_sociales": {"etiqueta": "Redes sociales", "icono": "👥", "prioridad": 4},
    "otros":          {"etiqueta": "Otros",          "icono": "📄", "prioridad": 6},
}

# Categorías cuyo contenido merece un resumen generado por el modelo.
# Las demás se resumen con una plantilla local (ver resumen_local()).
CATEGORIAS_CON_RESUMEN_LLM = {"academico", "laboral", "personal", "transaccional"}


# ---------------------------------------------------------------
# Vocabularios
# ---------------------------------------------------------------
DOMINIOS_REDES = (
    "linkedin.com", "facebook.com", "facebookmail.com", "instagram.com",
    "twitter.com", "x.com", "tiktok.com", "youtube.com", "pinterest.com",
    "discord.com", "reddit.com", "threads.net", "snapchat.com",
)

PREFIJOS_AUTOMATICOS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "notificacion", "notification", "notifications", "alerta", "alerts",
    "invoice", "billing", "facturacion", "automatico", "mailer-daemon",
)

# Bancos, billeteras y comercios. Escriben desde direcciones con nombres
# cambiantes ("tusinformes@tubienestarfinanciero.infobancolombia.com") y a
# veces sin List-Unsubscribe, así que ni el prefijo ni el encabezado bastan.
# Se busca DENTRO de cada parte del dominio: 'infobancolombia' coincide.
ENTIDADES = (
    "bancolombia", "bancodebogota", "davivienda", "daviplata", "nequi", "bbva",
    "colpatria", "scotiabank", "bancodeoccidente", "bancopopular", "avvillas",
    "bancocajasocial", "bancofalabella", "falabella", "itau", "movii",
    "lulobank", "nubank", "rappi", "mercadolibre",
)

# Frases de gancho comercial. Solo se usan con ENTIDADES: en un correo
# humano "descubre" o "conoce" no significan publicidad.
PALABRAS_GANCHO = (
    "conocelo", "conocela", "descubre", "entra ya", "te invitamos",
    "no te lo pierdas", "aprovecha", "pocas personas",
)

# Buzones genéricos de empresa. Una pregunta en sus correos es retórica
# ("si tu plata hablara, ¿qué te diría?"), no una pregunta a responder.
BUZONES_GENERICOS = (
    "info", "comunicaciones", "noticias", "contacto", "servicio",
    "newsletter", "marketing", "mercadeo", "promociones", "novedades", "clientes",
)

PALABRAS_TRANSACCIONAL = (
    "factura", "recibo", "receipt", "invoice", "statement", "comprobante",
    "clave dinamica", "codigo de verificacion",
    "verification code", "restablecer", "contrasena", "password",
    "alerta de seguridad", "inicio de sesion", "suscripcion", "renovacion",
    "confirmacion de compra", "extracto", "cobro", "tu pedido",
    # NOTA: se quitaron 'envio', 'pago', 'pedido' y 'transaccion' a secas.
    # Son demasiado genéricas: 'envío las evaluaciones' en un correo humano
    # hacía que se clasificara como transaccional, y eso partió un hilo real
    # de prácticas en dos categorías distintas.
)

PALABRAS_PROMOCIONAL = (
    "descuento", "oferta", "promocion", "cupon", "rebaja", "black friday",
    "ultimas horas", "solo por hoy", "aprovecha", "gratis", "sorteo",
    "no te lo pierdas", "envio gratis", "% off", "newsletter", "boletin",
)

PALABRAS_ACADEMICO = (
    "clase", "curso", "matricula", "semestre", "nota", "calificacion",
    "sustentacion", "tesis", "trabajo de grado", "docente", "profesor",
    "universidad", "biblioteca", "electiva", "asignatura", "parcial",
    "examen", "estudiante", "facultad", "beca", "inscripcion", "taller",
    "conferencia", "seminario", "diplomado", "certificado", "aprendizaje",
)

PALABRAS_LABORAL = (
    "practica", "pasantia", "contrato", "jefe", "empresa", "nomina",
    "entrevista", "hoja de vida", "vacante", "cargo", "reunion de trabajo",
    "cliente", "proyecto", "informe", "acta", "carta aval", "empleo",
)

DOMINIOS_ACADEMICOS = (".edu.co", ".edu", ".ac.uk", ".edu.mx", ".edu.ar")

# ---------------------------------------------------------------
# Señales (varias por correo — definen si se muestra destacado)
# ---------------------------------------------------------------
SENALES = {
    "remitente_importante": {"etiqueta": "Remitente importante", "icono": "⭐"},
    "requiere_respuesta": {"etiqueta": "Requiere respuesta", "icono": "↩️"},
    "tiene_fecha_limite": {"etiqueta": "Fecha límite",       "icono": "⏳"},
    "propone_evento":     {"etiqueta": "Posible evento",     "icono": "📅"},
    "urgente":            {"etiqueta": "Urgente",            "icono": "🔴"},
}

PATRONES_RESPUESTA = (
    "quedo atento", "quedo atenta", "me confirmas", "confirmar asistencia",
    "favor confirmar", "espero tu respuesta", "agradezco tu respuesta",
    "podrias", "puedes enviar", "necesito que", "me puedes", "responder este",
    "agradezco su colaboracion", "quedo pendiente", "me avisas",
    # Correo real sin signo de interrogación: "quisiera saber si es posible
    # que nos reunamos... Agradezco su pronta respuesta".
    "pronta respuesta", "quisiera saber si", "es posible que", "me confirma",
    "nos confirma", "me indica", "me indique", "quedo atento a su", "quedo atenta a su",
)

PATRONES_FECHA_LIMITE = (
    "a mas tardar", "fecha limite", "plazo", "vence", "antes del",
    "antes de las", "ultimo dia", "deadline", "hasta el", "cierre de",
    "se cierra", "expira",
)

PATRONES_EVENTO = (
    "reunion", "nos vemos", "cita", "sesion", "conferencia", "invitacion",
    "te esperamos", "los espero", "encuentro", "asamblea", "sustentacion",
    "convocatoria", "agenda", "charla",
    # Verbos: "quisiera saber si es posible que nos reunamos el sábado".
    "reunamos", "reunirnos", "nos reunimos", "encontrarnos", "nos encontremos", "vernos",
)

PATRONES_URGENTE = (
    "urgente", "inmediato", "prioritario", "importante:", "recordatorio",
    "ultimo aviso", "accion requerida", "hoy mismo",
)


def _normalizar(texto):
    """Minúsculas sin tildes: 'Práctica' y 'practica' deben coincidir."""
    texto = (texto or "").lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


_cache_patrones = {}


def _patron(vocabulario):
    """Compila un vocabulario en un patrón de PALABRAS COMPLETAS.

    Antes se comparaba por subcadena, y eso producía errores caros: 'sesion'
    está dentro de 'inicio de sesión', así que todo aviso de acceso a una
    cuenta quedaba marcado como posible evento. Igual pasaba con 'envio'
    dentro de 'envío las evaluaciones'.
    """
    clave = id(vocabulario)
    if clave not in _cache_patrones:
        alternativas = '|'.join(re.escape(p) for p in sorted(vocabulario, key=len, reverse=True))
        # El sufijo opcional mantiene los plurales: exigir palabra exacta hacía
        # que 'sustentaciones' dejara de coincidir con 'sustentacion'.
        _cache_patrones[clave] = re.compile(r'(?<!\w)(?:' + alternativas + r')(?:es|s)?(?!\w)')
    return _cache_patrones[clave]


def _contiene(texto_norm, vocabulario):
    return bool(_patron(vocabulario).search(texto_norm))


# 'sesión' es palabra de evento ("sesión de terapia") y también de seguridad
# ("inicio de sesión"). Se eliminan estas expresiones del texto ANTES de
# buscar señales de evento.
_NO_SON_EVENTO = (
    'inicio de sesion', 'iniciar sesion', 'cerrar sesion', 'nueva sesion',
    'sesion iniciada', 'sesion cerrada', 'tu sesion',
)

# Avisos de seguridad: no son eventos, son cosas que conviene mirar hoy.
PATRONES_SEGURIDAD = (
    'alerta de seguridad', 'inicio de sesion', 'actividad sospechosa',
    'acceso no autorizado', 'cambio de contrasena', 'restablecer contrasena',
    'verificacion en dos pasos', 'dispositivo nuevo', 'accion necesaria',
    'cuenta suspendida', 'clave dinamica',
)


# Cancelaciones: el modelo resumía bien "se cancela la clase" pero luego
# ofrecía agendarla. Se exige que el verbo vaya junto a un sustantivo de
# evento, así "cancelar la suscripción" o "pago cancelado" (en Colombia,
# pagado) no cuentan. Caso real: "hoy no tendremos nuestra sesión".
_SUSTANTIVO_EVENTO = (
    r'(?:clase|sesion|reunion|encuentro|tutoria|asesoria|charla|conferencia|'
    r'taller|cita|sustentacion|evento|laboratorio|parcial|examen|seminario)(?:es|s)?'
)
PATRONES_CANCELACION = [
    re.compile(p) for p in (
        # "no tendremos nuestra sesión", "no habrá clase", "no hay reunión".
        # Entre ambos solo caben artículos y posesivos: con texto libre,
        # "no hay cambios en la clase" contaba como cancelación.
        r'\bno\s+(?:tendremos|tendran|habra|hay|habria|realizaremos|dictare|'
        r'podre\s+dictar|se\s+realizara|se\s+hara)'
        r'(?:\s+(?:hoy|la|el|las|los|nuestra|nuestro|nuestras|nuestros|su|sus|esta|este))*\s+'
        + _SUSTANTIVO_EVENTO + r'\b',
        # "se cancela la reunión", "suspendemos el taller"
        r'\b(?:cancel|suspend)\w*\b[^.\n]{0,25}?\b' + _SUSTANTIVO_EVENTO + r'\b',
        # "Clase cancelada", "la sesión de hoy queda suspendida"
        r'\b' + _SUSTANTIVO_EVENTO + r'\b[^.\n]{0,40}?\b(?:cancelad|suspendid)[ao]s?\b',
        # "no me es posible reunirme el lunes", "no podré asistir a la sesión"
        r'\bno\s+(?:me\s+es\s+posible|sera\s+posible|podre|puedo|podremos|podra|pude)\b'
        r'[^.\n]{0,40}?\b(?:reunir\w*|asistir|atender|acompan\w*|vernos|dictar|'
        + _SUSTANTIVO_EVENTO + r')\b',
    )
]


# CU3 — Cambios sobre un evento que YA existe: hora, lugar o documentos.
# Se comprueba DESPUÉS de la cancelación: "se cancela y se reprograma" es una
# cancelación, no un cambio.
PATRONES_CAMBIO = [
    re.compile(p) for p in (
        r'\b(?:se\s+)?(?:reprograma\w*|reagenda\w*|reubica\w*|traslada\w*|adelanta\w*|aplaza\w*|posterga\w*)\b',
        r'\bcambi\w+\s+(?:de\s+)?(?:hora|horario|fecha|dia|lugar|salon|aula|sede|enlace|link)\b',
        r'\bnuev[oa]\s+(?:hora|horario|fecha|lugar|salon|aula|sede|enlace|link)\b',
        r'\b(?:ahora|ya)\s+(?:sera|es|sereis|queda)\s+(?:a\s+las|el|en)\b',
        r'\bpasa(?:mos)?\s+(?:a|para)\s+(?:las|el)\b',
        r'\bse\s+(?:mueve|corre|mover[aá])\s+(?:a|para)\b',
        r'\ben\s+lugar\s+de\s+(?:las|el)\b',
    )
]


def anuncia_cambio(*textos):
    """True si el correo anuncia un CAMBIO sobre un evento existente (CU3)."""
    texto = _normalizar(" ".join(t or "" for t in textos))
    if anuncia_cancelacion(texto):
        return False
    return any(p.search(texto) for p in PATRONES_CAMBIO)


def anuncia_cancelacion(*textos):
    """True si alguno de los textos anuncia que un evento NO se va a realizar."""
    texto = _normalizar(" ".join(t or "" for t in textos))
    for frase in _NO_SON_EVENTO:
        texto = texto.replace(frase, ' ')
    return any(p.search(texto) for p in PATRONES_CANCELACION)


def _es_remitente_automatico(correo_remitente):
    usuario = (correo_remitente or "").split("@")[0]
    return any(p in usuario for p in PREFIJOS_AUTOMATICOS)


def _es_entidad(dominio):
    return any(e in parte for parte in (dominio or "").lower().split(".") for e in ENTIDADES)


def _es_remitente_importante(remitente, lista, es_masivo):
    """¿Está este remitente en la lista del perfil?

    Un correo completo coincide siempre. Un dominio ("@uan.edu.co") coincide
    con él y sus subdominios, pero NO con envíos masivos: si no, el mercadeo
    institucional de la universidad inundaría Destacados.
    """
    remitente = (remitente or "").lower().strip()
    if not remitente:
        return False
    dominio = _dominio(remitente)
    for entrada in lista or []:
        entrada = (entrada or "").lower().strip()
        if not entrada:
            continue
        if entrada.startswith("@"):
            d = entrada[1:]
            if not es_masivo and (dominio == d or dominio.endswith("." + d)):
                return True
        elif remitente == entrada:
            return True
    return False


def _es_buzon_generico(correo_remitente):
    usuario = (correo_remitente or "").split("@")[0].lower()
    return any(b in usuario for b in BUZONES_GENERICOS)


def _dominio(correo_remitente):
    return (correo_remitente or "").split("@")[-1]


def _dominio_en(dominio, lista):
    """Coincidencia por dominio real, no por subcadena.

    'x.com' in 'netflix.com' era True, así que Netflix quedaba clasificado
    como red social. Ahora solo cuenta si es el dominio o un subdominio suyo.
    """
    dominio = (dominio or "").lower()
    return any(dominio == d or dominio.endswith('.' + d) for d in lista)


# ---------------------------------------------------------------
# Clasificación
# ---------------------------------------------------------------

def clasificar(correo, perfil=None):
    """Devuelve {categoria, senales, prioridad, es_masivo, necesita_llm}.

    'correo' es el diccionario que produce gmail_calendar_stefany:
    necesita asunto, remitente_email, lista_baja y (si existe) cuerpo o
    fragmento. Funciona igual con solo los encabezados y el fragmento, que
    es lo que permite clasificar la bandeja completa antes de descargar
    ningún cuerpo.

    'perfil' (memoria_local.cargar_perfil()) aplica lo contestado en el
    onboarding: remitentes y categorías importantes. Se recibe como
    parámetro para que este módulo siga sin leer archivos.
    """
    perfil = perfil or {}
    asunto = _normalizar(correo.get("asunto", ""))
    texto = _normalizar(correo.get("cuerpo") or correo.get("fragmento") or "")
    remitente = correo.get("remitente_email", "")
    dominio = _dominio(remitente)

    # Un correo con 'List-Unsubscribe' es un envío masivo por definición:
    # ninguna persona pone un enlace de baja en un correo que escribe a mano.
    es_masivo = bool(correo.get("lista_baja"))
    es_automatico = _es_remitente_automatico(remitente)
    es_entidad = _es_entidad(dominio)

    asunto_y_texto = f"{asunto} {texto}"

    # --- Categoría (el orden importa: lo más específico primero) ---
    if _dominio_en(dominio, DOMINIOS_REDES):
        categoria = "redes_sociales"

    elif es_masivo and not _contiene(asunto, PALABRAS_TRANSACCIONAL):
        # Masivo + no transaccional = promocional, incluso desde un dominio
        # universitario (los correos de mercadeo de la UAN caen aquí).
        categoria = "promocional"

    elif es_entidad:
        # Antes de las palabras académicas y laborales: "tu informe llegó"
        # de Bancolombia se clasificaba como Laboral por 'informe'. Ante la
        # duda va a Transaccional, que sí se resume: mandarlo a Promocional
        # podría esconder un comprobante de transferencia real.
        if _contiene(asunto_y_texto, PALABRAS_TRANSACCIONAL) or _contiene(asunto_y_texto, PATRONES_SEGURIDAD):
            categoria = "transaccional"
        elif _contiene(asunto_y_texto, PALABRAS_GANCHO) or _contiene(asunto_y_texto, PALABRAS_PROMOCIONAL):
            categoria = "promocional"
        else:
            categoria = "transaccional"

    elif _contiene(asunto_y_texto, PALABRAS_TRANSACCIONAL):
        categoria = "transaccional"

    elif _contiene(asunto_y_texto, PALABRAS_PROMOCIONAL):
        categoria = "promocional"

    elif any(dominio.endswith(d) for d in DOMINIOS_ACADEMICOS) or _contiene(asunto_y_texto, PALABRAS_ACADEMICO):
        # Si además habla de prácticas o contrato, pesa más lo laboral.
        categoria = "laboral" if _contiene(asunto_y_texto, PALABRAS_LABORAL) else "academico"

    elif _contiene(asunto_y_texto, PALABRAS_LABORAL):
        categoria = "laboral"

    elif es_automatico or es_masivo:
        categoria = "otros"

    else:
        categoria = "personal"

    # --- Señales ---
    senales = []
    es_difusion = es_masivo or categoria in ("promocional", "redes_sociales")

    # Solo un humano puede esperar respuesta. Un envío masivo nunca la espera,
    # aunque venga de una dirección con nombre de persona y lleve signos de
    # interrogación (el caso de la publicidad institucional).
    # Una entidad (banco, billetera, comercio) nunca espera respuesta por
    # correo: ni sus avisos de fraude ni sus campañas.
    if not es_automatico and not es_difusion and not es_entidad:
        # OJO: en la primera pasada el cuerpo viene vacío (solo hay fragmento).
        # Mirar únicamente 'cuerpo' hacía que esta señal no se disparara casi
        # nunca: 0 de 480 correos reales.
        texto_visible = correo.get("cuerpo") or correo.get("fragmento") or ""
        # El signo de interrogación SOLO cuenta como señal si además el correo
        # parece escrito por una persona. Los avisos de seguridad de los bancos
        # están llenos de preguntas retóricas ("¿Sabías que nunca te pediremos
        # tus claves?") y salían marcados como "requiere respuesta".
        hay_pregunta = "?" in texto_visible
        parece_humano = (
            not es_masivo and not es_automatico
            and not _es_buzon_generico(remitente)
            and categoria in ("personal", "laboral", "academico")
        )
        if _contiene(asunto_y_texto, PATRONES_RESPUESTA) or (hay_pregunta and parece_humano):
            senales.append("requiere_respuesta")

    if _contiene(asunto_y_texto, PATRONES_FECHA_LIMITE):
        senales.append("tiene_fecha_limite")

    texto_sin_accesos = asunto_y_texto
    for frase in _NO_SON_EVENTO:
        texto_sin_accesos = texto_sin_accesos.replace(frase, ' ')
    # Un correo que cancela una clase menciona 'sesión' o 'clase', pero no
    # propone ningún evento: al contrario, lo quita.
    if _contiene(texto_sin_accesos, PATRONES_EVENTO) and not anuncia_cancelacion(asunto_y_texto):
        senales.append("propone_evento")

    # Un aviso de seguridad merece verse hoy, aunque sea transaccional.
    if _contiene(asunto_y_texto, PATRONES_SEGURIDAD):
        if "urgente" not in senales:
            senales.append("urgente")

    # 'Última oportunidad' es urgencia de marketing, no urgencia real:
    # la señal no se aplica a categorías masivas.
    if not es_difusion and _contiene(asunto_y_texto, PATRONES_URGENTE):
        senales.append("urgente")

    # Un correo masivo que anuncia una fecha o un evento SÍ puede importar
    # (un curso que la usuaria está tomando, una sesión reprogramada). No se
    # esconde entre la publicidad: se muestra en una franja propia.
    relevante_pese_a_difusion = bool(
        es_difusion and {"tiene_fecha_limite", "propone_evento"} & set(senales)
    )

    # --- Perfil (onboarding) ---
    remitente_importante = _es_remitente_importante(
        remitente, perfil.get("remitentes_importantes"), es_masivo)
    if remitente_importante:
        senales.insert(0, "remitente_importante")

    importantes = set(perfil.get("categorias_importantes") or [])
    # "📅 Invitaciones y reuniones" no es una categoría sino la señal de evento.
    categoria_importante = (categoria in importantes
                            or ("invitaciones" in importantes and "propone_evento" in senales))

    # --- Prioridad final ---
    # La categoría da la prioridad base; tener señales la sube (número más
    # bajo = se muestra primero y sin plegar).
    prioridad = CATEGORIAS[categoria]["prioridad"]
    if senales and not es_difusion:
        prioridad = max(1, prioridad - len(senales))
    if categoria_importante:
        prioridad = max(1, prioridad - 1)
    if remitente_importante:
        prioridad = 1

    return {
        "categoria":    categoria,
        "senales":      senales,
        "prioridad":    prioridad,
        "es_masivo":    es_masivo,
        "es_automatico": es_automatico,
        "relevante_pese_a_difusion": relevante_pese_a_difusion,
        "remitente_importante": remitente_importante,
        "categoria_importante": categoria_importante,
        # Un remitente importante siempre se resume con el modelo, aunque su
        # categoría por sí sola no lo pidiera.
        "necesita_llm": (categoria in CATEGORIAS_CON_RESUMEN_LLM or relevante_pese_a_difusion
                         or remitente_importante),
    }


def resumen_local(correo, clasificacion):
    """Resumen SIN modelo para los correos que no lo justifican.

    Un correo de publicidad no gana nada con un resumen generado por un LLM
    de 8B: el remitente y el asunto ya dicen todo lo que el usuario necesita
    para decidir si lo abre. Esto ahorra la mayoría de las llamadas.
    """
    nombre = correo.get("remitente", "").split("<")[0].strip().strip('"')
    nombre = nombre or correo.get("remitente_email", "alguien")
    asunto = correo.get("asunto", "(sin asunto)")

    categoria = clasificacion["categoria"]
    if categoria == "promocional":
        return f"Promoción de {nombre}: {asunto}"
    if categoria == "redes_sociales":
        return f"Notificación de {nombre}: {asunto}"
    return f"{nombre} — {asunto}"


def agrupar_por_hilo(correos):
    """Colapsa cada conversación en su mensaje MÁS RECIENTE.

    Si hay tres mensajes sin leer del mismo hilo, el usuario no necesita tres
    resúmenes: necesita saber en qué quedó la conversación. Se conserva el
    último y se anota cuántos hay sin leer, lo que además ahorra llamadas al
    modelo. Requiere 'hilo' y 'fecha_ms' (ambos vienen de Gmail).
    """
    por_hilo = {}
    for correo in correos:
        clave = correo.get("hilo") or correo.get("id")
        actual = por_hilo.get(clave)
        if actual is None or correo.get("fecha_ms", 0) > actual.get("fecha_ms", 0):
            correo = dict(correo)
            correo["mensajes_en_hilo"] = (actual or {}).get("mensajes_en_hilo", 0) + 1
            por_hilo[clave] = correo
        else:
            actual["mensajes_en_hilo"] = actual.get("mensajes_en_hilo", 1) + 1

    return sorted(por_hilo.values(), key=lambda c: c.get("fecha_ms", 0), reverse=True)


# Grupos sintéticos: no son categorías, son franjas que sacan correos de su
# categoría cuando lo que importa es la acción, no el tema.
GRUPO_REQUIEREN_RESPUESTA = {
    "categoria": "requieren_respuesta",
    "etiqueta":  "Requieren respuesta",
    "icono":     "↩️",
    "prioridad": 0.5,
}

GRUPO_POSIBLE_INTERES = {
    "categoria": "posible_interes",
    "etiqueta":  "Podría interesarte",
    "icono":     "👀",
    "prioridad": 3.5,
}


def agrupar_por_categoria(correos_clasificados, categorias_importantes=()):
    """Agrupa para la vista de acordeones y ordena por prioridad.

    Recibe [(correo, clasificacion), ...] y devuelve una lista de grupos
    listos para pintar, del más importante al menos importante.

    'categorias_importantes' (del perfil): esos acordeones van justo después
    de "Requieren respuesta" y arrancan ABIERTOS.
    """
    categorias_importantes = set(categorias_importantes or ())
    grupos = {}
    rescatados = []
    por_responder = []

    for correo, clasif in correos_clasificados:
        if "requiere_respuesta" in clasif["senales"]:
            por_responder.append((correo, clasif))
        elif clasif.get("relevante_pese_a_difusion"):
            rescatados.append((correo, clasif))
        else:
            grupos.setdefault(clasif["categoria"], []).append((correo, clasif))

    resultado = []
    for categoria, elementos in grupos.items():
        elementos.sort(key=lambda par: par[1]["prioridad"])
        info = CATEGORIAS[categoria]
        importante = categoria in categorias_importantes
        resultado.append({
            "categoria":  categoria,
            "etiqueta":   info["etiqueta"],
            "icono":      info["icono"],
            # Importantes: entre "Requieren respuesta" (0,5) y el resto (≥1),
            # conservando su orden relativo.
            "prioridad":  0.6 + info["prioridad"] / 100 if importante else info["prioridad"],
            "cantidad":   len(elementos),
            "importante": importante,
            # TODOS los acordeones arrancan cerrados, también los de las
            # categorías importantes. Lo urgente ya está arriba, en
            # 'destacados', y abrir una categoría con decenas de correos hacía
            # que la primera vista fuera un muro de texto. Ser "importante"
            # sigue decidiendo el ORDEN (prioridad), no la apertura.
            "desplegado": False,
            "correos":    elementos,
        })

    if por_responder:
        por_responder.sort(key=lambda par: par[1]["prioridad"])
        resultado.append({
            **GRUPO_REQUIEREN_RESPUESTA,
            "cantidad":   len(por_responder),
            "desplegado": False,
            "correos":    por_responder,
        })

    if rescatados:
        rescatados.sort(key=lambda par: par[1]["prioridad"])
        resultado.append({
            **GRUPO_POSIBLE_INTERES,
            "cantidad":   len(rescatados),
            "desplegado": False,
            "correos":    rescatados,
        })

    resultado.sort(key=lambda g: g["prioridad"])
    return resultado


def conteo_por_senal(correos_clasificados):
    """Cifras para el resumen general (nivel 1 de la jerarquía)."""
    conteo = {clave: 0 for clave in SENALES}
    for _, clasif in correos_clasificados:
        for senal in clasif["senales"]:
            conteo[senal] += 1
    return {k: v for k, v in conteo.items() if v}


def destacados(correos_clasificados, maximo=8):
    """Los correos que el usuario debe ver SIN tener que abrir nada.

    Con los acordeones cerrados por defecto, esta es la única parte siempre
    visible: tiene que ser corta y estar bien ordenada, o deja de servir.
    """
    con_senal = [
        (correo, clasif) for correo, clasif in correos_clasificados
        if clasif["senales"] and (clasif["categoria"] not in ("promocional", "redes_sociales")
                                  or clasif.get("remitente_importante"))
    ]

    peso = {"urgente": 0, "remitente_importante": 0.5, "requiere_respuesta": 1,
            "tiene_fecha_limite": 2, "propone_evento": 3}

    def orden(par):
        _, clasif = par
        mejor = min((peso.get(s, 9) for s in clasif["senales"]), default=9)
        # A igual urgencia, primero lo de las categorías importantes del perfil.
        return (mejor, not clasif.get("categoria_importante"), -len(clasif["senales"]),
                clasif["prioridad"])

    con_senal.sort(key=orden)
    return con_senal[:maximo]
