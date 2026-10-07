# -*- coding: utf-8 -*-
"""
calendario_consulta_stefany.py — Consulta directa de calendario para una
fecha. Solo aritmética/regex + lectura del calendario
local ya conectado -- no usa el modelo.
"""

import re
from datetime import date, datetime, timedelta

from prompts_stefany import MESES_ES, DIAS_ES, ZONA_BOGOTA, formatear_fecha_legible
import gmail_calendar_stefany as gc
from analisis_stefany import _eventos_en_fecha

PALABRAS_FECHA_RELATIVA = {'pasado mañana': 2, 'mañana': 1, 'hoy': 0}


def _palabra_suelta(texto, palabra):
    """Coincidencia por palabra completa, no por subcadena.

    Buscar 'hoy' con 'in' lo encontraba dentro de 'ahoyado' o 'hoyo', y los
    nombres de día dentro de otras palabras. Eso hacía que consultas normales
    se interpretaran como preguntas sobre una fecha concreta.
    """
    return re.search(r'(?<!\w)' + re.escape(palabra) + r'(?!\w)', texto) is not None


def _sin_tildes(texto):
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')


def _extraer_fecha_de_pregunta(mensaje):
    # Sin tildes a ambos lados: "el miercoles", "el sabado" o "manana" escritos
    # sin tilde no se reconocían y Stefany preguntaba "¿Para qué día es?".
    texto = _sin_tildes(mensaje.lower())
    hoy = datetime.now(ZONA_BOGOTA).date()

    for frase, delta in PALABRAS_FECHA_RELATIVA.items():
        if _palabra_suelta(texto, _sin_tildes(frase)):
            return hoy + timedelta(days=delta)

    for idx, dia_nombre in enumerate(DIAS_ES):
        if _palabra_suelta(texto, _sin_tildes(dia_nombre)):
            delta_dias = (idx - hoy.weekday()) % 7
            delta_dias = delta_dias or 7
            return hoy + timedelta(days=delta_dias)

    m = re.search(r'(\d{1,2})\s+de\s+(' + '|'.join(MESES_ES) + r')', texto, re.IGNORECASE)
    if m:
        dia = int(m.group(1))
        mes = MESES_ES.index(m.group(2).lower()) + 1
        for anio in (hoy.year, hoy.year + 1):
            try:
                candidata = date(anio, mes, dia)
            except ValueError:
                continue
            if candidata >= hoy:
                return candidata
    return None


PALABRAS_FECHA_PASADA = {'anteayer': -2, 'antier': -2, 'ayer': -1, 'hoy': 0}


def extraer_fecha_de_consulta_correos(mensaje):
    """Fecha de la que habla una petición sobre CORREOS ("los del 15 de junio").

    Se diferencia de _extraer_fecha_de_pregunta() en que aquí las fechas miran
    al PASADO: los correos ya llegaron. "15 de junio" es el 15 de junio más
    reciente, no el del año que viene.
    """
    from datetime import date

    texto = _sin_tildes(mensaje.lower())
    hoy = datetime.now(ZONA_BOGOTA).date()

    for frase, delta in PALABRAS_FECHA_PASADA.items():
        if _palabra_suelta(texto, frase):
            return hoy + timedelta(days=delta)

    # "el lunes pasado", "el lunes" → el más reciente ya ocurrido
    for idx, dia_nombre in enumerate(DIAS_ES):
        if _palabra_suelta(texto, _sin_tildes(dia_nombre)):
            atras = (hoy.weekday() - idx) % 7
            return hoy - timedelta(days=atras or 7)

    m = re.search(r'(\d{1,2})\s+de\s+(' + '|'.join(MESES_ES) + r')(?:\s+de[l]?\s+(\d{4}))?',
                  texto, re.IGNORECASE)
    if m:
        dia, mes = int(m.group(1)), MESES_ES.index(m.group(2).lower()) + 1
        if m.group(3):
            try:
                return date(int(m.group(3)), mes, dia)
            except ValueError:
                return None
        for anio in (hoy.year, hoy.year - 1):      # la más reciente ya pasada
            try:
                candidata = date(anio, mes, dia)
            except ValueError:
                continue
            if candidata <= hoy:
                return candidata

    m = re.search(r'\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b', texto)
    if m:
        dia, mes = int(m.group(1)), int(m.group(2))
        anio = int(m.group(3) or hoy.year)
        anio += 2000 if anio < 100 else 0
        try:
            return date(anio, mes, dia)
        except ValueError:
            return None
    return None


def _hora_float_a_texto(hora24):
    h_int = int(hora24)
    # Antes cualquier fracción se mostraba como ":30"; con el horario del
    # perfil ya puede haber 7:15 o 8:45.
    minutos = f"{int(round((hora24 % 1) * 60)):02d}"
    if h_int == 0:
        return f"12:{minutos} a.m."
    elif h_int < 12:
        return f"{h_int}:{minutos} a.m."
    elif h_int == 12:
        return f"12:{minutos} p.m."
    else:
        return f"{h_int - 12}:{minutos} p.m."


def consultar_calendario_fecha(fecha_consulta, cuenta_filtrada=None):
    hoy = datetime.now(ZONA_BOGOTA).date()
    fecha_legible = formatear_fecha_legible(fecha_consulta.isoformat())

    if fecha_consulta < hoy:
        return (f"El {fecha_legible} ya pasó -- por ahora solo puedo consultar el "
                f"calendario para hoy en adelante.")

    dias_necesarios = max((fecha_consulta - hoy).days + 2, 1)
    if cuenta_filtrada:
        eventos_por_cuenta = {cuenta_filtrada: gc.leer_eventos_cuenta(cuenta_filtrada, cantidad=50, dias=dias_necesarios)}
    else:
        eventos_por_cuenta = {a: gc.leer_eventos_cuenta(a, cantidad=50, dias=dias_necesarios) for a in gc.cuentas_conectadas}

    eventos_ese_dia = _eventos_en_fecha(fecha_consulta, eventos_por_cuenta)

    if not eventos_ese_dia:
        return f"No tienes ningún compromiso registrado para el {fecha_legible}."

    lineas = [f"Para el {fecha_legible} tienes:"]
    for ev in sorted(eventos_ese_dia, key=lambda e: e['inicio_h']):
        hora_ini = _hora_float_a_texto(ev['inicio_h'])
        hora_fin = _hora_float_a_texto(ev['fin_h'])
        lineas.append(f"- [{ev['alias']}] {ev['titulo']} ({hora_ini} a {hora_fin})")
    return "\n".join(lineas)


def buscar_espacio_libre(desde_fecha, desde_hora, eventos_por_cuenta,
                         duracion_horas=None, paso_horas=0.5, dias_max=14):
    """Primer hueco libre a partir de esa fecha y hora, o None.

    Solo dentro del horario del perfil (días y horas): no propone un sábado ni
    las 7 de la tarde aunque estén libres. Avanza de media en media hora y
    descarta cualquier hueco que se cruce con un evento de CUALQUIER cuenta
    conectada. Todo es aritmética sobre el calendario: el modelo no interviene.

    Devuelve (fecha, hora_inicio) o None si no encuentra nada en 'dias_max'.
    """
    from datetime import datetime, timedelta

    import memoria_local
    from analisis_stefany import _eventos_en_fecha, _solapan

    if desde_fecha is None:
        return None
    if duracion_horas is None:
        duracion_horas = memoria_local.duracion_por_defecto_horas()

    dias_laborales, inicio_jornada, fin_jornada = memoria_local.horario_laboral()
    ahora = datetime.now(ZONA_BOGOTA)
    hoy = ahora.date()
    if desde_fecha < hoy:
        desde_fecha, desde_hora = hoy, None    # el correo proponía algo ya pasado

    for salto in range(dias_max + 1):
        fecha = desde_fecha + timedelta(days=salto)
        if fecha < hoy or fecha.weekday() not in dias_laborales:
            continue

        hora = inicio_jornada
        if salto == 0 and desde_hora is not None:
            hora = max(hora, desde_hora)          # el mismo día, después de la hora propuesta
        if fecha == hoy:
            hora = max(hora, ahora.hour + ahora.minute / 60)
        # Se redondea hacia arriba al siguiente múltiplo del paso (9:10 → 9:30).
        hora = -(-hora // paso_horas) * paso_horas

        ocupados = _eventos_en_fecha(fecha, eventos_por_cuenta or {})
        while hora + duracion_horas <= fin_jornada:
            if not any(_solapan(hora, hora + duracion_horas, ev["inicio_h"], ev["fin_h"])
                       for ev in ocupados):
                return fecha, hora
            hora += paso_horas
    return None


def resolver_inicio_fin(fecha_texto, hora_texto, duracion_horas=None, fecha=None):
    """Convierte '14 de octubre' + '3:00 p.m.' en fechas ISO con zona horaria.

    Devuelve (inicio_iso, fin_iso) o (None, None) si no se puede resolver.
    Si el año no está en el texto, se elige el más cercano hacia adelante:
    un correo de diciembre que menciona 'el 5 de enero' habla del año
    siguiente, no del que ya pasó.

    Si se pasa 'fecha' (un date ya resuelto), se usa tal cual, con su año, y
    el texto de la fecha se ignora.

    Sin hora ni duración, se usan las del perfil (horario de reuniones y
    duración habitual); sin perfil, 9:00 a.m. y 1 hora como siempre.
    """
    import re
    from datetime import datetime, timedelta

    import memoria_local
    from prompts_stefany import MESES_ES, ZONA_BOGOTA

    if duracion_horas is None:
        duracion_horas = memoria_local.duracion_por_defecto_horas()

    if fecha is not None:
        dia, mes = fecha.day, fecha.month
    else:
        if not fecha_texto:
            return None, None
        m = re.search(r'(\d{1,2})\s+de\s+(' + '|'.join(MESES_ES) + r')',
                      fecha_texto, re.IGNORECASE)
        if not m:
            return None, None
        dia = int(m.group(1))
        mes = MESES_ES.index(m.group(2).lower()) + 1

    por_defecto = memoria_local.hora_por_defecto()
    hora, minuto = int(por_defecto), int(round((por_defecto % 1) * 60))
    if hora_texto:
        mh = re.search(r'(\d{1,2}):(\d{2})\s*(a\.m\.|p\.m\.)', hora_texto, re.IGNORECASE)
        if mh:
            hora, minuto = int(mh.group(1)), int(mh.group(2))
            sufijo = mh.group(3).lower()
            if sufijo == 'p.m.' and hora != 12:
                hora += 12
            if sufijo == 'a.m.' and hora == 12:
                hora = 0

    if fecha is not None:
        inicio = datetime(fecha.year, mes, dia, hora, minuto, tzinfo=ZONA_BOGOTA)
        return inicio.isoformat(), (inicio + timedelta(hours=duracion_horas)).isoformat()

    ahora = datetime.now(ZONA_BOGOTA)
    for anio in (ahora.year, ahora.year + 1):
        try:
            inicio = datetime(anio, mes, dia, hora, minuto, tzinfo=ZONA_BOGOTA)
        except ValueError:
            continue
        if inicio >= ahora - timedelta(days=2):
            fin = inicio + timedelta(hours=duracion_horas)
            return inicio.isoformat(), fin.isoformat()
    return None, None
