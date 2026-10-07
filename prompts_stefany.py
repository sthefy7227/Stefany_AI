# -*- coding: utf-8 -*-
"""
prompts_stefany.py — Arma los prompts (idénticos a los del dataset de
entrenamiento) y llama al modelo A TRAVÉS del cliente HTTP hacia el
servidor de Modal (cliente_stefany.generar) -- en el equipo del usuario no
hay GPU; la generación ocurre en el servidor.

Requiere llamar primero a configurar_cliente(cliente) una vez que la app
ya se conectó al servidor.
"""

import re
import random
import unicodedata
from datetime import datetime, date, timezone, timedelta

import memoria_local

MESES_ES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
            'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
DIAS_ES = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']
def _zona_del_equipo():
    """Zona horaria del computador donde corre la aplicación.

    Antes estaba fija en -05:00 (Bogotá) y escrita en dos archivos distintos.
    Es una aplicación de escritorio: la hora que importa es la del equipo de
    quien la usa, no la de la ciudad donde se desarrolló. Si alguien la evalúa
    desde otro huso, las fechas y los eventos deben cuadrar con SU reloj.
    """
    zona = datetime.now().astimezone().tzinfo
    return zona if zona is not None else timezone(timedelta(hours=-5))


ZONA_LOCAL = _zona_del_equipo()

# Nombre anterior, conservado para no romper los imports existentes.
ZONA_BOGOTA = ZONA_LOCAL

_cliente = None  # se inyecta con configurar_cliente()
_forzar_base = False  # se inyecta con configurar_modo_prueba() -- ver más abajo


def configurar_cliente(cliente):
    global _cliente
    _cliente = cliente


def configurar_modo_prueba(forzar_base):
    """Equivalente al selector 'Modelo base / Modelo ajustado' que ya
    tenías: si forzar_base=True, incluso analizar_correo() (que
    normalmente SIEMPRE usa el adaptador) genera sin él -- para poder
    comparar calidad como hacías antes."""
    global _forzar_base
    _forzar_base = forzar_base


def _sin_acentos(texto):
    return ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')


def formatear_fecha_legible(iso_str):
    if not iso_str:
        return "(sin fecha)"
    try:
        solo_fecha = len(iso_str) == 10
        dt = datetime.fromisoformat(iso_str)
        dia_semana = DIAS_ES[dt.weekday()]
        mes = MESES_ES[dt.month - 1]
        base = f"{dia_semana} {dt.day} de {mes} de {dt.year}"
        if solo_fecha:
            return base
        hora = dt.strftime("%I:%M %p").lstrip("0")
        hora = hora.replace("AM", "a.m.").replace("PM", "p.m.")
        return f"{base}, {hora}"
    except Exception:
        return iso_str


def formatear_fecha_correo(fecha_raw):
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(fecha_raw)
        dt = dt.astimezone(ZONA_BOGOTA)
        return formatear_fecha_legible(dt.isoformat())
    except Exception:
        return fecha_raw


def obtener_fecha_hoy_texto():
    ahora = datetime.now(ZONA_BOGOTA)
    return f"{DIAS_ES[ahora.weekday()]} {ahora.day} de {MESES_ES[ahora.month - 1]} de {ahora.year}"


def preferencias_para_prompt():
    prefs = memoria_local.cargar_preferencias()
    if not prefs:
        return "(El usuario no ha definido preferencias personales aún.)"
    return "\n".join(f"- {p}" for p in prefs)


SYSTEM_PROMPT_ANALISIS = """Eres Stefany, un asistente personal de gestión de tiempo. Vas a recibir UN solo correo electrónico (de una cuenta del usuario) junto con el calendario completo de TODAS sus cuentas conectadas.

FECHA DE HOY (real, úsala como referencia para todo razonamiento temporal): {fecha_hoy}

PREFERENCIAS PERSONALES DEL USUARIO (tenlas en cuenta si son relevantes): {preferencias}

Responde en dos partes:

1. RESUMEN: escribe de 1 a 2 oraciones con la idea principal del correo. Si menciona una fecha explícita o una fecha límite implícita, inclúyela tal como aparece en el correo, sin inventarla ni cambiarla.

2. Bajo el título "📊 Análisis:" (SOLO si aplica; si no aplica, NO escribas este bloque):
   - Si el correo menciona un compromiso, fecha límite o reunión que NO está en el calendario → explica el hallazgo (cuenta, fecha, hora) y termina con: "¿Te gustaría que lo agende en el calendario de [cuenta]? (sí/no)"
   - Si el correo confirma un compromiso que se cruza en horario con un evento YA existente en el calendario (misma cuenta u otra) → explica el conflicto y termina con: "¿Cancelo '[evento existente]' o continúo con '[evento nuevo]'? Puedo redactar el correo de cancelación."
   - Si el correo informa un cambio real (hora, lugar o documentos) sobre un evento YA agendado → explica el cambio y termina con: "Te recomiendo actualizar el evento en el calendario con este cambio."
   - Si el compromiso mencionado YA está agendado igual, o el correo es informativo/publicitario/de confirmación sin cruces de horario, NO escribas ningún bloque de análisis; el resumen es suficiente.

REGLAS ESTRICTAS:
- NUNCA inventes ni cambies un día, mes o año; cópialos exactamente como aparecen en el correo o el calendario.
- El alias de cuenta es exactamente el texto que aparece en "(cuenta: ...)" -- cópialo tal cual si lo necesitas mencionar.
- NO repitas el asunto ni el remitente del correo en tu respuesta.
- Responde siempre en español."""


def user_msg_correo_individual(alias, asunto, remitente, cuerpo, contexto_calendario,
                               hilo_anterior="", remitente_anterior=""):
    mensaje = (
        "Analiza este correo junto con mi calendario completo (todas las cuentas "
        "conectadas). Dime si contiene algún compromiso, fecha límite o reunión que "
        "no esté en el calendario, o si genera un conflicto de horario con algún "
        "evento existente.\n\n"
        f"Correo (cuenta: {alias}):\n"
        f"  Asunto: {asunto}\n"
        f"  De: {remitente}\n"
        f"  Contenido: {cuerpo}\n\n"
    )
    # Solo cuando el correo es respuesta dentro de un hilo: "no me es posible
    # reunirme el lunes en la sesión pactada" no se entiende sin el mensaje
    # anterior. Va antes del calendario y marcado como contexto.
    if hilo_anterior:
        mensaje += (f"MENSAJE ANTERIOR DEL HILO (solo contexto, NO lo resumas):\n"
                    f"  De: {remitente_anterior}\n"
                    f"  Contenido: {hilo_anterior}\n\n")
    return mensaje + f"EVENTOS EN CALENDARIO (todas las cuentas):\n{contexto_calendario}\n"


def analizar_correo(correo, contexto_calendario_texto):
    """Analiza un correo; la generación ocurre en el servidor de Modal vía HTTP."""
    system = SYSTEM_PROMPT_ANALISIS.format(
        fecha_hoy=obtener_fecha_hoy_texto(),
        preferencias=preferencias_para_prompt(),
    )
    user = user_msg_correo_individual(
        alias=correo['cuenta'], asunto=correo['asunto'], remitente=correo['remitente'],
        cuerpo=correo['cuerpo'], contexto_calendario=contexto_calendario_texto,
        hilo_anterior=correo.get('cuerpo_hilo_anterior', ''),
        remitente_anterior=correo.get('remitente_hilo_anterior', ''),
    )
    mensajes = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return _cliente.generar(mensajes, usar_adaptador=not _forzar_base, max_new_tokens=300, temperature=0.3)


# La fecha va en el prompt, no en el texto fijo: sin ella el modelo respondía
# "hoy es miércoles 26 de julio de 2022" —la fecha que recuerda de su
# entrenamiento base— cuando se le preguntaba qué día era.
PLANTILLA_SYSTEM_CONVERSACIONAL = (
    "Eres Stefany, un asistente personal de gestión de tiempo. Usa siempre la "
    "palabra 'asistente' para referirte a ti misma -- NUNCA 'asistenta'. "
    "\n\nFECHA Y HORA ACTUALES del equipo del usuario: {fecha_hoy}, {hora_ahora}. "
    "Úsalas cuando la pregunta tenga que ver con el tiempo (hoy, mañana, esta "
    "semana, cuánto falta para algo). No uses ninguna otra fecha.\n"
    "NO repitas la fecha ni la hora si la pregunta no va de eso, y NUNCA "
    "cierres la respuesta con una coletilla del tipo '¿en qué más puedo "
    "ayudarte hoy, martes 6 de octubre a las 3:14 p.m.?'. Termina de forma "
    "natural, como en una conversación normal.\n\n"
    "Responde de forma normal, natural y útil, como lo haría cualquier "
    "asistente conversacional. No inventes que revisaste correos o el "
    "calendario, ni que hiciste una acción (crear, editar, enviar), a menos "
    "que el usuario te lo haya pedido explícitamente en este mensaje y esa "
    "acción se haya ejecutado de verdad. Responde siempre en español."
)


def system_conversacional():
    ahora = datetime.now(ZONA_BOGOTA)
    hora = ahora.strftime("%I:%M %p").lstrip("0").replace("AM", "a.m.").replace("PM", "p.m.")
    return PLANTILLA_SYSTEM_CONVERSACIONAL.format(
        fecha_hoy=obtener_fecha_hoy_texto(), hora_ahora=hora)

SALUDOS_FIJOS = [
    "¡Hola! Soy Stefany, tu asistente personal de gestión de tiempo. ¿En qué puedo ayudarte hoy?",
    "¡Hola! Soy Stefany. Puedo revisar tus correos, tu calendario, o agendar y editar eventos por ti. ¿Qué necesitas?",
    "¡Hola, qué gusto! Soy Stefany, tu asistente de tiempo. ¿En qué te apoyo hoy?",
    "¡Hola! Aquí Stefany, lista para ayudarte con tus correos y tu calendario. ¿Por dónde empezamos?",
]


def _es_saludo_puro(texto):
    texto_norm = _sin_acentos(texto.lower().strip().rstrip('!.¡'))
    saludos = {'hola', 'buenas', 'buenos dias', 'buenas tardes', 'buenas noches',
               'hey', 'hi', 'hello', 'que tal', 'ola'}
    return texto_norm in saludos


_OFRECE_AYUDA = re.compile(
    r'\b(?:ayudar|ayudarte|ayudarle|ayudo|algo m[áa]s|en qu[ée]\s+(?:m[áa]s\s+)?puedo|'
    r'necesitas|puedo hacer por ti)\b', re.IGNORECASE)
_MARCA_TEMPORAL = re.compile(
    r'\b(?:hoy|lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo)\b'
    r'|a\s+las\s+\d{1,2}[:.]\d{2}', re.IGNORECASE)


def _quitar_coletilla_fecha(texto):
    """Quita el '¿...hoy martes 6 de octubre a las 3:14 p.m.?' del final.

    El modelo base lo añadía a casi TODAS las respuestas, porque el prompt le
    da la fecha y la hora y él las encaja en la despedida. Pedírselo en el
    prompt ayuda pero no basta, así que además se recorta por código.

    Se corta a partir del último '¿' y solo si esa pregunta final ofrece ayuda
    Y menciona el día o la hora, y solo si queda algo antes: así no se toca
    una respuesta que de verdad iba sobre la fecha ("hoy es martes 6"), ni
    una pregunta final legítima que no repita el día.
    """
    t = (texto or "").strip()
    pos = t.rfind("¿")
    if pos <= 0:
        return t
    cola = t[pos:]
    if _OFRECE_AYUDA.search(cola) and _MARCA_TEMPORAL.search(cola):
        recortado = t[:pos].strip()
        if recortado:
            return recortado
    return t


def responder_conversacion(mensaje_usuario, historial=None):
    if _es_saludo_puro(mensaje_usuario):
        return random.choice(SALUDOS_FIJOS)

    mensajes = [{"role": "system", "content": system_conversacional()}]
    if historial:
        for turno in historial[-4:]:
            rol = "user" if turno.get("rol") == "usuario" else "assistant"
            mensajes.append({"role": rol, "content": turno.get("texto", "")[:400]})
    mensajes.append({"role": "user", "content": mensaje_usuario})
    # El chat usa el modelo BASE a propósito, no el adaptador.
    #
    # Se probó al revés y salió peor: el adaptador está especializado en
    # analizar UN correo y producir el bloque "📊 Análisis:", y al preguntarle
    # algo corriente ignoraba el prompt del sistema. Preguntarle "¿qué día es
    # hoy?" devolvía una fecha de su entrenamiento base (2017, 2022) aunque la
    # fecha real estuviera en el prompt.
    #
    # Especializar un adaptador tiene ese coste: mejora su tarea y empeora
    # todo lo demás. Para conversar, el modelo base responde mejor.
    texto = _cliente.generar(mensajes, usar_adaptador=False,
                             max_new_tokens=250, temperature=0.7)
    return _quitar_coletilla_fecha(texto)


def generar_correo_cancelacion(titulo_evento, destinatario):
    mensajes = [
        {"role": "system", "content": "Eres un asistente que redacta correos profesionales en español."},
        {"role": "user", "content": (
            f"Redacta un correo breve y cordial para cancelar la siguiente reunión/evento: "
            f"\"{titulo_evento}\".\nEl correo debe tener:\n"
            f"- Asunto en la primera línea con el prefijo \"Asunto: \"\n"
            f"- Cuerpo del correo en las siguientes líneas\n"
            f"- Tono formal y profesional\n- Máximo 4 líneas en el cuerpo"
        )},
    ]
    texto = _cliente.generar(mensajes, usar_adaptador=False, max_new_tokens=200, temperature=0.7)

    lineas = texto.strip().split('\n')
    asunto, cuerpo = "", ""
    for linea in lineas:
        if linea.lower().startswith("asunto:"):
            asunto = linea.split(":", 1)[1].strip()
        elif linea.strip():
            cuerpo += linea + "\n"
    return {
        'asunto': asunto or f"Cancelación: {titulo_evento}",
        'cuerpo': cuerpo.strip(),
        'destinatario': destinatario or "",
    }

# ---------------------------------------------------------------
# Tarea SEPARADA: redactar la respuesta a un correo
# ---------------------------------------------------------------
# Es una tarea distinta a la de resumir y tiene su propio system prompt:
# pedirle al modelo un resumen y un borrador en la misma salida sería mezclar
# dos cosas incompatibles en un solo texto.
#
# El adaptador v2 se entrenó con 220 ejemplos de esta tarea, en primera
# persona y registro formal.

SYSTEM_PROMPT_RESPUESTA = """Eres Stefany, un asistente personal de gestión de tiempo. Vas a redactar, EN NOMBRE DEL USUARIO, la respuesta a un correo que él recibió.

FECHA DE HOY (real, úsala como referencia para todo razonamiento temporal): {fecha_hoy}

Reglas de redacción:
- Escribe en PRIMERA PERSONA, como si fuera el propio usuario quien responde.
- Usa un registro formal y cordial. Nada coloquial.
- Responde SOLO a lo que el correo pide. No añadas compromisos, fechas ni datos que no aparezcan en el correo original.
- Si el correo pide confirmar algo, confírmalo. Si pide enviar algo, indica cuándo se enviará usando únicamente los plazos mencionados en el correo.
- Máximo 5 líneas.
- Empieza con un saludo y termina con una despedida breve.
- NO inventes nombres, cargos ni datos de contacto.
- Escribe siempre en español, con ortografía y acentuación correctas.

Devuelve únicamente el cuerpo del correo, sin asunto y sin comillas."""


def redactar_respuesta(correo, instruccion_extra="", agenda=""):
    """Pide al modelo el borrador de respuesta a un correo.

    instruccion_extra permite regenerar con un matiz ("más corta", "declinando
    la invitación") sin cambiar el prompt base.
    """
    system = SYSTEM_PROMPT_RESPUESTA.format(fecha_hoy=obtener_fecha_hoy_texto())

    peticion = "Redacta mi respuesta a este correo."
    if instruccion_extra:
        peticion += f"\n\nIndicación adicional: {instruccion_extra}"

    usuario = (
        f"{peticion}\n\n"
        f"Correo recibido:\n"
        f"  Asunto: {correo.get('asunto', '')}\n"
        f"  De: {correo.get('remitente', '')}\n"
        f"  Contenido: {correo.get('cuerpo', '')}\n"
    )
    # La disponibilidad sale del calendario real (router._agenda_para_responder).
    # Va en el mensaje del usuario, no en el system prompt, para no cambiar el
    # formato con el que se entrenó el adaptador.
    if agenda:
        usuario += (f"\nMI AGENDA PARA ESA FECHA: {agenda}\n"
                    "Si mi agenda dice que NO estoy libre, no afirmes lo contrario: "
                    "dilo y propón otro momento. Si dice que no se ha podido comprobar, "
                    "NO confirmes disponibilidad: responde que lo revisaré y confirmaré.\n")

    mensajes = [{"role": "system", "content": system},
                {"role": "user", "content": usuario}]
    # Temperatura algo más alta que en el análisis: al pulsar "Regenerar" la
    # respuesta tiene que salir distinta.
    return _cliente.generar(mensajes, usar_adaptador=not _forzar_base,
                            max_new_tokens=320, temperature=0.45)


def resumir_categoria(peticion):
    """Síntesis de una categoría a partir de los resúmenes individuales.

    Usa el modelo BASE, no el adaptador: el adaptador está afinado para
    analizar UN correo y producir el bloque '📊 Análisis:', y aquí se le pide
    algo distinto. Forzarlo con el adaptador le haría intentar el formato
    aprendido.
    """
    system = ("Eres un asistente que resume bandejas de correo. Respondes con "
              "UNA sola frase breve, en español, sin viñetas y sin inventar "
              "información que no esté en el texto que te dan.")
    mensajes = [{"role": "system", "content": system},
                {"role": "user", "content": peticion}]
    return _cliente.generar(mensajes, usar_adaptador=False,
                            max_new_tokens=90, temperature=0.3)
