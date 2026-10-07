# -*- coding: utf-8 -*-
"""
router_stefany.py — El "cerebro" de orquestación completo, corriendo
localmente. Solo llama al servidor remoto (a través de prompts_stefany,
que a su vez usa cliente_stefany) para las DOS cosas que de verdad
necesitan el modelo: analizar un correo y responder conversación general.
Todo lo demás -- enrutar, leer Gmail/Calendar, agendar, editar, verificar
conflictos -- es código puro, corre en el computador del usuario.
"""

import re
import time
import unicodedata
from datetime import datetime

import gmail_calendar_stefany as gc
import memoria_local
import prompts_stefany as prompts
import analisis_stefany as analisis
import calendario_consulta_stefany as calcons
import persistencia
import ocr_stefany as ocr
import clasificador_stefany as clasificador

from prompts_stefany import ZONA_BOGOTA, formatear_fecha_legible
from calendario_consulta_stefany import _extraer_fecha_de_pregunta, _hora_float_a_texto


def _sin_acentos(texto):
    return ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')


def _contiene_palabra(texto_norm, palabras):
    return any(re.search(r'\b' + re.escape(p) + r'\b', texto_norm) for p in palabras)


def detectar_cuenta_mencionada(mensaje_usuario):
    texto = mensaje_usuario.lower()
    for alias in gc.cuentas_conectadas:
        if alias.lower() in texto:
            return alias

    # Por apodo del perfil: "agéndalo en mi correo académico" → la cuenta con
    # apodo "Académico". Palabra completa y sin tildes, para que "Personal" no
    # coincida dentro de "personalizado".
    texto_norm = _sin_acentos(texto)
    for alias in gc.cuentas_conectadas:
        apodo = _sin_acentos(memoria_local.apodo_cuenta(alias).lower())
        if apodo and re.search(r'(?<!\w)' + re.escape(apodo) + r'(?!\w)', texto_norm):
            return alias

    for alias in gc.cuentas_conectadas:
        alias_lower = alias.lower()
        alias_sin_numeros = re.sub(r'\d+$', '', alias_lower)
        if alias_sin_numeros and len(alias_sin_numeros) >= 3 and alias_sin_numeros in texto:
            return alias
    return None


# Detectar confirmaciones con _contiene_palabra() era peligroso: bastaba que
# el mensaje contuviera 'si', 'claro' o 'listo' en cualquier parte para
# ejecutar la acción pendiente. "No, claro que no" confirmaba. Ahora se exige
# que el mensaje SEA una confirmación, no que la contenga.

PALABRAS_AFIRMATIVAS = {
    'si', 'sip', 'ok', 'okay', 'vale', 'dale', 'hazlo', 'crealo', 'agendalo',
    'confirmo', 'confirmado', 'acepto', 'procede', 'adelante', 'correcto',
    'exacto', 'claro', 'listo', 'perfecto', 'obvio', 'porfa', 'porfavor',
}

PALABRAS_NEGATIVAS = {
    'no', 'nop', 'nope', 'nunca', 'cancela', 'cancelar', 'dejalo', 'olvidalo',
    'omite', 'omitir', 'mejor', 'todavia', 'aun', 'espera', 'despues',
}

_RELLENO = {'por', 'favor', 'gracias', 'que', 'lo', 'la', 'eso', 'esta',
            'bien', 'de', 'una', 'ya', 'pues', 'me', 'parece'}


def _palabras_normalizadas(texto):
    limpio = _sin_acentos(texto.lower().strip())
    limpio = re.sub(r"[^\w\s]", " ", limpio)
    return [p for p in limpio.split() if p]


def es_negacion(texto):
    """Se evalúa ANTES que es_confirmacion(): ante la duda, no se actúa."""
    palabras = _palabras_normalizadas(texto)
    if not palabras or len(palabras) > 6:
        return False
    return bool(PALABRAS_NEGATIVAS & set(palabras))


def es_confirmacion(texto):
    """Solo cuenta si el mensaje ES una confirmación y nada más.

    'sí', 'ok', 'dale, hazlo' → sí.
    'sí, pero antes muéstrame el borrador' → no (es una petición).
    'no, claro que no' → no (la negación se comprueba primero).
    """
    palabras = _palabras_normalizadas(texto)
    if not palabras or len(palabras) > 4:
        return False
    if PALABRAS_NEGATIVAS & set(palabras):
        return False
    significativas = [p for p in palabras if p not in _RELLENO]
    if not significativas:
        return False
    return all(p in PALABRAS_AFIRMATIVAS for p in significativas)


def es_instruccion_preferencia(texto):
    texto_norm = _sin_acentos(texto.lower())
    disparadores = ['recuerda que', 'recuerdalo', 'agrega a mis preferencias',
                     'agrega esta preferencia', 'guarda esta preferencia',
                     'ten en cuenta que', 'anota que', 'apunta que',
                     'toma en cuenta que']
    return any(d in texto_norm for d in disparadores)


def quiere_todas_las_cuentas(texto):
    texto_norm = _sin_acentos(texto.lower())
    disparadores = ['todas mis cuentas', 'todas las cuentas', 'en todas',
                     'los 3 calendarios', 'las 3 cuentas', 'en mis 3 cuentas',
                     'en cada cuenta', 'en todos los calendarios']
    return any(d in texto_norm for d in disparadores)


def _construir_iso_evento(datos_pendientes, duracion_horas=None, hora_por_defecto=None):
    # Sin hora ni duración: las del perfil. Sin perfil: 9:00 a.m. y 1 h.
    if duracion_horas is None:
        duracion_horas = memoria_local.duracion_por_defecto_horas()
    if hora_por_defecto is None:
        hora_por_defecto = memoria_local.hora_por_defecto()

    fecha_evento = datos_pendientes.get('fecha_resuelta')
    if not fecha_evento:
        fecha_evento = analisis._resolver_fecha_texto(datos_pendientes.get('fecha_texto', ''))
    if not fecha_evento:
        return None, None, False

    horas = datos_pendientes.get('horas_resueltas')
    if horas is None:
        horas = analisis._resolver_horas_texto(datos_pendientes.get('hora_texto', ''))
    hora_default_usada = not horas
    hora_inicio = horas[0] if horas else hora_por_defecto
    h_i, m_i = int(hora_inicio), int(round((hora_inicio % 1) * 60))

    inicio = datetime(fecha_evento.year, fecha_evento.month, fecha_evento.day,
                       h_i, m_i, tzinfo=ZONA_BOGOTA)
    if len(horas) > 1:
        h_f, m_f = int(horas[1]), int(round((horas[1] % 1) * 60))
        fin = datetime(fecha_evento.year, fecha_evento.month, fecha_evento.day,
                        h_f, m_f, tzinfo=ZONA_BOGOTA)
    else:
        # Con timedelta: una reunión de 11:30 p.m. y 1 h termina al día
        # siguiente, en vez de fallar con "hour must be in 0..23".
        from datetime import timedelta
        fin = inicio + timedelta(hours=duracion_horas)
    return inicio.isoformat(), fin.isoformat(), hora_default_usada


def _cuenta_por_defecto():
    """Cuenta para lo que se pide en el chat sin nombrar ninguna: la preferida del perfil."""
    return memoria_local.cuenta_preferida(gc.cuentas_conectadas.keys())


def _texto_hora_por_defecto():
    return _hora_float_a_texto(memoria_local.hora_por_defecto())


def _aviso_horario_chat(fecha, horas):
    """'\\n⚠️ Es domingo, un día fuera de tu horario habitual.' o '' (solo informa)."""
    hora = horas[0] if horas else memoria_local.hora_por_defecto()
    aviso = memoria_local.aviso_fuera_de_horario(fecha, hora)
    return f"\n{aviso}" if aviso else ""


def _eventos_por_cuenta_para(fecha):
    """Eventos de TODAS las cuentas conectadas hasta esa fecha, por cuenta."""
    hoy = datetime.now(ZONA_BOGOTA).date()
    dias = max((fecha - hoy).days + 2, 1)
    return {alias: gc.leer_eventos_cuenta(alias, cantidad=50, dias=dias)
            for alias in gc.cuentas_conectadas}


def _aviso_cruce_chat(fecha, horas, cuenta_destino):
    """CU4 por la vía del chat: avisa si ya hay algo a esa hora, o '' si está libre.

    El botón "Agendar" de un correo comprobaba el cruce contra el calendario
    real, pero una orden escrita ("agéndame una reunión mañana a las 3")
    creaba el evento **sin mirar nada**: quedaban dos compromisos a la misma
    hora sin un solo aviso. El cruce se busca en TODAS las cuentas, no solo
    en la de destino, que es justo lo que pide el CU4.

    Solo informa: la decisión sigue siendo del usuario, que responde sí o no.
    """
    inicio = horas[0] if horas else memoria_local.hora_por_defecto()
    if horas and len(horas) > 1:
        fin = horas[1]
    else:
        fin = inicio + memoria_local.duracion_por_defecto_horas()

    try:
        eventos_por_cuenta = _eventos_por_cuenta_para(fecha)
    except Exception as e:
        # Sin calendario no se puede comprobar; mejor seguir sin aviso que
        # romper el agendado.
        print(f"⚠️ No pude comprobar cruces antes de agendar: {e}")
        return ""

    cruces = [ev for ev in analisis._eventos_en_fecha(fecha, eventos_por_cuenta)
              if analisis._solapan(inicio, fin, ev["inicio_h"], ev["fin_h"])]
    if not cruces:
        return ""

    detalle = "; ".join(
        f"'{ev['titulo']}' de {_hora_float_a_texto(ev['inicio_h'])} a "
        f"{_hora_float_a_texto(ev['fin_h'])} en [{memoria_local.etiqueta_cuenta(ev['alias'])}]"
        for ev in cruces[:2])
    aviso = (f"\n⚠️ Ojo: ese horario se cruza con {detalle}. "
             f"Si confirmas, te quedarán los dos a la misma hora.")

    # Igual que en la fila de conflicto de un correo, se ofrece una salida.
    _, _, libre_texto = _espacio_libre(fecha, inicio, eventos_por_cuenta)
    if libre_texto:
        aviso += f"\n🟢 {libre_texto} — dime si prefieres esa hora."
    return aviso


def es_solicitud_agendar_directo(texto):
    texto_norm = _sin_acentos(texto.lower())
    verbos_agendar = ['agenda', 'agendame', 'agendar', 'programa', 'programar',
                       'crea un evento', 'crear evento', 'agrega un evento',
                       'agregar evento', 'anade un evento', 'pon un evento',
                       'crea una reunion', 'crear una reunion']
    return _contiene_palabra(texto_norm, verbos_agendar)


def es_solicitud_editar_directo(texto):
    texto_norm = _sin_acentos(texto.lower())
    verbos_editar = ['edita', 'editar', 'modifica', 'modificar', 'cambia',
                      'cambiar', 'actualiza', 'actualizar', 'corrige', 'corregir',
                      'mueve', 'mover', 'reprograma', 'reprogramar']
    return _contiene_palabra(texto_norm, verbos_editar)


_VERBOS_REDACTAR = ['redacta', 'redactame', 'redactar', 'escribe', 'escribeme',
                    'escribir', 'manda', 'mandame', 'mandar', 'envia', 'enviame',
                    'enviar', 'prepara', 'preparame', 'preparar']


def es_solicitud_correo_cancelacion(texto):
    """"Redacta un correo de cancelación para X".

    Se comprueba ANTES que cancelar el evento y antes de enrutar a la bandeja:
    la frase lleva la palabra "correo", que si no mandaría a revisar la
    bandeja entera.
    """
    t = _sin_acentos(texto.lower())
    if not _contiene_palabra(t, _VERBOS_REDACTAR):
        return False
    if not _contiene_palabra(t, ['correo', 'correos', 'email', 'mail', 'mensaje']):
        return False
    # Caso real: "redacta un correo para X que diga que NO PUEDO IR a la cita
    # de uñas hoy, que me disculpe". No encajaba en ninguna forma de la lista
    # ('no puedo' no estaba) y el mensaje acababa en la rama de "revisar la
    # bandeja", porque lleva la palabra "correo". Se cubren ahora las formas
    # con las que de verdad se dice esto.
    return bool(re.search(
        r'cancel|avis|posterg|aplaz|reprogram|disculp|excus|lament|'
        r'no\s+(?:podre|puedo|voy|ire|asistire|alcanzo|lograre)|'
        r'no\s+(?:me\s+)?(?:es\s+posible|sera\s+posible)|'
        r'no\s+(?:podre|puedo)\s+(?:ir|asistir|llegar)', t))


def es_solicitud_cancelar_directo(texto):
    """"Cancela la reunión de las 8pm", "elimina el evento de mañana".

    Faltaba por completo: escribirlo en el chat caía en la rama genérica del
    calendario y contestaba "¿Para qué día necesitas que revise o agende
    eso?", aunque el evento estuviera a la vista.
    """
    texto_norm = _sin_acentos(texto.lower())
    verbos = ['cancela', 'cancelame', 'cancelar', 'elimina', 'eliminame',
              'eliminar', 'borra', 'borrame', 'borrar', 'quita', 'quitame',
              'quitar']
    if not _contiene_palabra(texto_norm, verbos):
        return False
    # Redactar el correo de cancelación es otra cosa: no toca el calendario.
    return not es_solicitud_correo_cancelacion(texto)


def _extraer_destinatario(texto):
    """La dirección de correo escrita en un texto, o ''.

    Cada tramo tras un punto exige al menos un carácter (`(?:\\.[\\w-]+)+`)
    para no tragarse el punto final de la frase: la descripción del evento
    dice "…desde un correo de profe@uan.edu.co." y con `[\\w.-]+` el borrador
    salía dirigido a "profe@uan.edu.co." — una dirección inválida.
    """
    m = re.search(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+', texto or '')
    return m.group(0).lower() if m else ''


def _filtrar_eventos_por_mensaje(eventos, mensaje):
    """Acota los eventos de un día con la hora o las palabras del mensaje.

    "la reunión de las 8pm" tiene que quedarse con la de las 8, no con las
    tres del día. Si un filtro no deja nada, se ignora: es preferible
    preguntar cuál de varios que decir que no hay ninguno.
    """
    horas = _extraer_horas_libres(mensaje)
    if horas:
        por_hora = []
        for e in eventos:
            try:
                ini = datetime.fromisoformat(e['inicio'])
            except (KeyError, ValueError):
                continue
            if abs((ini.hour + ini.minute / 60) - horas[0]) < 0.25:
                por_hora.append(e)
        if por_hora:
            return por_hora

    claves, _ = analisis._palabras_clave(mensaje)
    if claves:
        # Ordenados por CUÁNTAS palabras coinciden, no por el primero que
        # coincida: "la cita de uñas" comparte 'cita' con "Cita médica" y
        # 'cita' + 'unas' con "Cita de uñas". Quedarse con el primero elegía
        # el equivocado según el orden del calendario.
        con_puntaje = []
        for e in eventos:
            comunes = analisis._palabras_clave(e.get('titulo', ''))[0] & claves
            if comunes:
                con_puntaje.append((len(comunes), e))
        if con_puntaje:
            con_puntaje.sort(key=lambda par: par[0], reverse=True)
            return [e for _, e in con_puntaje]
    return eventos


def _extraer_horas_libres(texto):
    horas = []
    patron = r'(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?'
    for h, mi, ap in re.findall(patron, texto, re.IGNORECASE):
        h = int(h)
        mi = int(mi) if mi else 0
        if ap.lower() == 'p' and h != 12:
            h += 12
        if ap.lower() == 'a' and h == 12:
            h = 0
        horas.append(h + mi / 60)
    return horas


def _extraer_titulo_evento_directo(texto):
    tope = r'(?=\s+(?:hoy\b|mañana\b|a las\b|el\s+\d)|$)'
    patrones = [
        r'que diga\s+([^,]+?)' + tope, r'que se llame\s+([^,]+?)' + tope,
        r'llamad[oa]\s+([^,]+?)' + tope, r'titulad[oa]\s+([^,]+?)' + tope,
        r'con el t[íi]tulo\s+([^,]+?)' + tope,
        r'(cita\s+con\s+[^,]+?|reuni[oó]n\s+con\s+[^,]+?)(?=\s+para\b|\s+el\b|\s+hoy\b|\s+mañana\b|\s+a las\b|$)',
    ]
    for patron in patrones:
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            return m.group(1).strip().rstrip('.').capitalize()
    return None


def _extraer_nuevo_titulo_edicion(texto):
    patrones = [
        r'ponle el nombre de\s+(.+)$', r'ponle de nombre\s+(.+)$',
        r'que se llame\s+(.+)$', r'ren[oó]mbra(?:lo|la)\s+(?:a\s+)?(.+)$',
        r'll[aá]mal[oa]\s+(.+)$', r'cambia(?:le)? el nombre a\s+(.+)$',
        r'cambia(?:le)? el t[íi]tulo a\s+(.+)$',
    ]
    for patron in patrones:
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            return m.group(1).strip().rstrip('.').capitalize()
    return None


def _extraer_nueva_hora_edicion(texto):
    patrones_intro = [
        r'cambia(?:le)? la hora a\s+(.+)$', r'mu[eé]ve(?:lo|la)\s+a\s+(.+)$',
        r'p[aá]sa(?:lo|la)\s+a\s+(.+)$', r'nueva hora\s*:?\s*(.+)$',
    ]
    for patron in patrones_intro:
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            horas = _extraer_horas_libres(m.group(1))
            if horas:
                return horas
    return None


# "Muévelo al lunes", "pásala para el 25 de septiembre", "cambia la reunión
# del viernes al lunes". Los patrones concretos van primero; el último es
# general y por eso solo se acepta si de verdad sale una fecha de él.
_PATRONES_DESTINO_EDICION = [
    r'cambia(?:le)?\s+la\s+fecha\s+(?:a|al|para)\s+(.+)$',
    r'nueva\s+fecha\s*:?\s*(.+)$',
    r'mu[eé]ve(?:lo|la)\s+(?:al|para|a)\s+(.+)$',
    r'p[aá]sa(?:lo|la)\s+(?:al|para|a)\s+(.+)$',
    r'reprogr[aá]ma(?:lo|la)\s+(?:al|para|a)\s+(.+)$',
    r'(?:mueve|mover|cambia|cambiar|pasa|pasar|reprograma|reprogramar)\b.{0,60}?'
    r'\s+(?:al|para\s+el|para\s+la|para)\s+(.+)$',
]


def _clausula_destino_edicion(texto):
    """(fecha nueva, texto sin esa cláusula) para "muévelo al lunes".

    Devolver también el texto recortado es lo que permite distinguir las DOS
    fechas de "cambia la reunión del viernes al lunes": la del evento que hay
    que buscar y la nueva. Antes solo existía una y la orden era imposible.
    """
    for patron in _PATRONES_DESTINO_EDICION:
        m = re.search(patron, texto, re.IGNORECASE)
        if not m:
            continue
        crudo = m.group(1).strip()
        # "lunes" a secas no lo reconoce el buscador de días (exige artículo,
        # para no confundir "de lunes a viernes" de un pie de página).
        fecha = _fecha_escrita_a_mano(crudo) or _fecha_escrita_a_mano(f"el {crudo}")
        if fecha:
            return fecha, texto[:m.start(1)].strip()
    return None, texto


def _extraer_nueva_fecha_edicion(texto):
    return _clausula_destino_edicion(texto)[0]


def _extraer_nuevo_lugar_edicion(texto):
    """El nuevo sitio de un evento dicho en el chat (RF8), o None."""
    patrones = [
        r'cambia(?:le)?\s+el\s+(?:lugar|sitio|sal[oó]n|aula)\s+(?:a|al|por)\s+(.+)$',
        r'ponle\s+(?:de|como)\s+(?:lugar|sitio)\s+(.+)$',
        r'nuevo\s+(?:lugar|sitio|sal[oó]n|aula)\s*:?\s*(.+)$',
        r'(?:lugar|sitio)\s*:\s*(.+)$',
    ]
    for patron in patrones:
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            return " ".join(m.group(1).split()).strip(' .').strip()
    return None


def _buscar_eventos_para_editar(fecha_evento, cuenta_filtrada=None):
    hoy = datetime.now(ZONA_BOGOTA).date()
    dias_necesarios = max((fecha_evento - hoy).days + 2, 1)

    if cuenta_filtrada:
        eventos_por_cuenta = {cuenta_filtrada: gc.leer_eventos_cuenta(cuenta_filtrada, cantidad=50, dias=dias_necesarios)}
    else:
        eventos_por_cuenta = {a: gc.leer_eventos_cuenta(a, cantidad=50, dias=dias_necesarios) for a in gc.cuentas_conectadas}

    encontrados = []
    for alias, eventos in eventos_por_cuenta.items():
        for e in eventos:
            try:
                inicio_dt = datetime.fromisoformat(e['inicio'])
            except Exception:
                continue
            if inicio_dt.date() == fecha_evento:
                encontrados.append(e)
    return sorted(encontrados, key=lambda e: e['inicio'])


def _preparar_confirmacion_edicion(evento, nuevo_titulo, nuevas_horas,
                                   nueva_fecha=None, nuevo_lugar=None):
    """Deja lista la edición y pide confirmación (RF8: nombre, fecha, hora o lugar)."""
    global estado_sesion

    if not nuevo_titulo and not nuevas_horas and not nueva_fecha and not nuevo_lugar:
        estado_sesion['accion_pendiente'] = 'esperando_titulo_evento'
        estado_sesion['datos_pendientes'] = {'evento_a_editar': evento, 'modo': 'editar_generico'}
        estado_sesion['ultimo_tema'] = 'calendario'
        return (f'Encontré "{evento["titulo"]}" ese día. ¿Qué le cambio? '
                f'(ej. "ponle de nombre reunión con Melisa", "cambia la hora a las 6 pm", '
                f'"muévelo al lunes" o "cambia el lugar a laboratorio de sistemas")')

    inicio_dt = datetime.fromisoformat(evento['inicio'])
    fin_dt = datetime.fromisoformat(evento['fin'])
    duracion = fin_dt - inicio_dt

    # Mover el evento de día conserva su hora; cambiarle la hora conserva su
    # día. Si llegan los dos, se aplican sobre la misma base.
    base_dt = inicio_dt
    if nueva_fecha is not None:
        base_dt = inicio_dt.replace(year=nueva_fecha.year, month=nueva_fecha.month,
                                    day=nueva_fecha.day)

    nueva_fecha_inicio_iso, nueva_fecha_fin_iso = None, None
    if nuevas_horas:
        h_i, m_i = int(nuevas_horas[0]), int(round((nuevas_horas[0] % 1) * 60))
        nueva_inicio_dt = base_dt.replace(hour=h_i, minute=m_i)
        if len(nuevas_horas) > 1:
            h_f, m_f = int(nuevas_horas[1]), int(round((nuevas_horas[1] % 1) * 60))
            nueva_fin_dt = base_dt.replace(hour=h_f, minute=m_f)
        else:
            nueva_fin_dt = nueva_inicio_dt + duracion
        nueva_fecha_inicio_iso = nueva_inicio_dt.isoformat()
        nueva_fecha_fin_iso = nueva_fin_dt.isoformat()
    elif nueva_fecha is not None:
        nueva_fecha_inicio_iso = base_dt.isoformat()
        nueva_fecha_fin_iso = (base_dt + duracion).isoformat()

    estado_sesion['accion_pendiente'] = 'editar_evento'
    estado_sesion['datos_pendientes'] = {
        'evento_a_editar': evento,
        'nuevo_titulo': nuevo_titulo,
        'nueva_fecha_inicio_iso': nueva_fecha_inicio_iso,
        'nueva_fecha_fin_iso': nueva_fecha_fin_iso,
        'nuevo_lugar': nuevo_lugar,
    }
    estado_sesion['ultimo_tema'] = 'calendario'
    estado_sesion['ultimo_evento_referenciado'] = evento

    cambios = []
    if nuevo_titulo:
        cambios.append(f'nombre → "{nuevo_titulo}"')
    if nueva_fecha is not None:
        cambios.append(f"fecha → {analisis.fecha_en_palabras(nueva_fecha)}")
    if nuevas_horas:
        detalle = _hora_float_a_texto(nuevas_horas[0])
        if len(nuevas_horas) > 1:
            detalle += f" a {_hora_float_a_texto(nuevas_horas[1])}"
        cambios.append(f"hora → {detalle}")
    if nuevo_lugar:
        cambios.append(f'lugar → "{nuevo_lugar}"')
    return (f'¿Confirmas editar "{evento["titulo"]}" ({", ".join(cambios)}) '
            f'en [{evento["cuenta"]}]? (sí/no)')


def _resolver_solicitud_editar_directo(mensaje_usuario):
    global estado_sesion

    # La fecha NUEVA se saca primero y se aparta del texto: en "cambia la
    # reunión del viernes al lunes" hay dos fechas, y sin separarlas se
    # buscaba el evento en el día equivocado.
    nueva_fecha, texto_evento = _clausula_destino_edicion(mensaje_usuario)
    nuevo_lugar = _extraer_nuevo_lugar_edicion(mensaje_usuario)

    fecha_evento = _extraer_fecha_de_pregunta(texto_evento)
    if not fecha_evento:
        estado_sesion['ultimo_tema'] = 'calendario'
        return "¿De qué día es el evento que quieres editar?"

    cuenta_filtrada = detectar_cuenta_mencionada(mensaje_usuario)
    encontrados = _buscar_eventos_para_editar(fecha_evento, cuenta_filtrada)
    estado_sesion['ultimo_tema'] = 'calendario'

    if not encontrados:
        fecha_legible = formatear_fecha_legible(fecha_evento.isoformat())
        return f"No encontré ningún evento el {fecha_legible} para editar."

    if len(encontrados) > 1:
        estado_sesion['accion_pendiente'] = 'esperando_seleccion_evento'
        estado_sesion['datos_pendientes'] = {
            'candidatos': encontrados,
            'nuevo_titulo': _extraer_nuevo_titulo_edicion(mensaje_usuario),
            'nuevas_horas': _extraer_nueva_hora_edicion(mensaje_usuario),
            'nueva_fecha': nueva_fecha,
            'nuevo_lugar': nuevo_lugar,
        }
        lineas = ["Encontré varios eventos ese día, ¿cuál quieres editar? Respóndeme con el número:"]
        for idx, e in enumerate(encontrados, 1):
            inicio_dt = datetime.fromisoformat(e['inicio'])
            hora_txt = _hora_float_a_texto(inicio_dt.hour + inicio_dt.minute / 60)
            lineas.append(f"{idx}. [{e['cuenta']}] {e['titulo']} ({hora_txt})")
        return "\n".join(lineas)

    nuevo_titulo = _extraer_nuevo_titulo_edicion(mensaje_usuario)
    nuevas_horas = _extraer_nueva_hora_edicion(mensaje_usuario)
    return _preparar_confirmacion_edicion(encontrados[0], nuevo_titulo, nuevas_horas,
                                          nueva_fecha, nuevo_lugar)


def _resolver_seleccion_evento(mensaje_usuario):
    global estado_sesion
    datos = estado_sesion['datos_pendientes']
    candidatos = datos.get('candidatos', [])
    m = re.search(r'\d+', mensaje_usuario)
    if not m or not (1 <= int(m.group()) <= len(candidatos)):
        return f"No entendí cuál -- respóndeme con un número del 1 al {len(candidatos)}."
    evento = candidatos[int(m.group()) - 1]
    if datos.get('modo') == 'eliminar':
        return _preparar_confirmacion_eliminacion(evento)
    return _preparar_confirmacion_edicion(evento, datos.get('nuevo_titulo'),
                                          datos.get('nuevas_horas'),
                                          datos.get('nueva_fecha'),
                                          datos.get('nuevo_lugar'))


def _preparar_confirmacion_eliminacion(evento):
    """Deja lista la eliminación y pide confirmación. Nunca borra sin un 'sí'."""
    global estado_sesion
    estado_sesion['accion_pendiente'] = 'eliminar_evento'
    estado_sesion['datos_pendientes'] = {'evento_a_eliminar': evento}
    estado_sesion['ultimo_tema'] = 'calendario'
    estado_sesion['ultimo_evento_referenciado'] = evento
    return (f'¿Confirmas que elimine "{evento["titulo"]}" del '
            f'{formatear_fecha_legible(evento["inicio"])} en '
            f'[{memoria_local.etiqueta_cuenta(evento["cuenta"])}]? (sí/no)\n'
            f'Si se repite, solo borro esa sesión; las demás se quedan.')


def _resolver_solicitud_cancelar_directo(mensaje_usuario):
    """Elimina del calendario un evento pedido por el chat.

    Sin fecha en el mensaje se entiende HOY: "cancela la reunión de las 8pm"
    se dice mirando la agenda del día, no pensando en una fecha.
    """
    global estado_sesion
    estado_sesion['ultimo_tema'] = 'calendario'

    fecha = _extraer_fecha_de_pregunta(mensaje_usuario) or datetime.now(ZONA_BOGOTA).date()
    cuenta_filtrada = detectar_cuenta_mencionada(mensaje_usuario)
    encontrados = _buscar_eventos_para_editar(fecha, cuenta_filtrada)
    if not encontrados:
        return (f"No encontré ningún evento el "
                f"{formatear_fecha_legible(fecha.isoformat())} para cancelar.")

    candidatos = _filtrar_eventos_por_mensaje(encontrados, mensaje_usuario)
    if len(candidatos) > 1:
        estado_sesion['accion_pendiente'] = 'esperando_seleccion_evento'
        estado_sesion['datos_pendientes'] = {'candidatos': candidatos, 'modo': 'eliminar'}
        lineas = ["¿Cuál quieres cancelar? Respóndeme con el número:"]
        for idx, e in enumerate(candidatos, 1):
            ini = datetime.fromisoformat(e['inicio'])
            lineas.append(f"{idx}. [{memoria_local.etiqueta_cuenta(e['cuenta'])}] "
                          f"{e['titulo']} ({_hora_float_a_texto(ini.hour + ini.minute / 60)})")
        return "\n".join(lineas)

    return _preparar_confirmacion_eliminacion(candidatos[0])


def _preparar_borrador_cancelacion(titulo, destinatario, cuenta):
    """Redacta el correo que avisa de la cancelación y lo deja listo.

    No lo envía ni lo guarda todavía: eso ocurre si la usuaria confirma, y
    aun entonces solo se crea un BORRADOR en Gmail (regla 1).
    """
    global estado_sesion
    borrador = prompts.generar_correo_cancelacion(titulo, destinatario)
    borrador['cuenta'] = cuenta or _cuenta_por_defecto()
    estado_sesion['accion_pendiente'] = 'confirmar_borrador_correo'
    estado_sesion['datos_pendientes'] = borrador
    estado_sesion['ultimo_tema'] = 'calendario'

    destino = borrador.get('destinatario') or "(lo pones tú en Gmail)"
    return (f"Te preparé este correo:\n\n"
            f"Para: {destino}\n"
            f"Asunto: {borrador['asunto']}\n\n"
            f"{borrador['cuerpo']}\n\n"
            f"¿Lo guardo como borrador en Gmail? (sí/no). Yo no envío correos: "
            f"lo revisas allí y lo mandas tú.")


def _resolver_solicitud_correo_cancelacion(mensaje_usuario):
    """"Redacta un correo de cancelación para X" desde el chat.

    Esto ya funcionaba a partir de un correo recibido (fila de conflicto),
    pero no si se pedía directamente por el chat.
    """
    global estado_sesion
    estado_sesion['ultimo_tema'] = 'calendario'

    destinatario = _extraer_destinatario(mensaje_usuario)
    reciente = estado_sesion.get('ultimo_evento_referenciado')
    evento = None

    # 1) El evento del que se acaba de hablar, SI el mensaje lo nombra. Va
    #    primero a propósito: después de cancelarlo ya no está en el
    #    calendario, así que buscarlo por fecha encontraría otro evento de ese
    #    día. Caso real: tras borrar "Cita de uñas", el mensaje "redacta un
    #    correo… a la cita de uñas hoy" terminaba apuntando a "Estar pendiente
    #    de la cita médica", porque las dos llevan la palabra 'cita'.
    if reciente:
        claves_mensaje, _ = analisis._palabras_clave(mensaje_usuario)
        claves_evento, _ = analisis._palabras_clave(reciente.get('titulo', ''))
        if claves_mensaje & claves_evento:
            evento = reciente

    # 2) Si no se reconoce, se busca en el calendario por el día que diga el
    #    mensaje; y si no dice ninguno, el evento reciente sirve de respaldo.
    if evento is None:
        fecha = _extraer_fecha_de_pregunta(mensaje_usuario)
        if fecha or not reciente:
            dia = fecha or datetime.now(ZONA_BOGOTA).date()
            encontrados = _buscar_eventos_para_editar(
                dia, detectar_cuenta_mencionada(mensaje_usuario))
            if encontrados:
                evento = _filtrar_eventos_por_mensaje(encontrados, mensaje_usuario)[0]
        else:
            evento = reciente

    if not evento:
        return ("¿De qué reunión quieres que avise? Dime el día y la hora "
                "(por ejemplo \"la de hoy a las 3pm\") y te lo redacto.")

    # El correo de quien originó el evento quedó en su descripción al agendarlo.
    destinatario = destinatario or _extraer_destinatario(evento.get('descripcion', ''))
    return _preparar_borrador_cancelacion(evento.get('titulo', ''), destinatario,
                                          evento.get('cuenta'))


def _resolver_titulo_pendiente(mensaje_usuario):
    global estado_sesion
    datos = estado_sesion['datos_pendientes']
    modo = datos.get('modo')

    if modo == 'editar_generico':
        evento = datos['evento_a_editar']
        nuevo_titulo = _extraer_nuevo_titulo_edicion(mensaje_usuario)
        nuevas_horas = _extraer_nueva_hora_edicion(mensaje_usuario)
        nueva_fecha = _extraer_nueva_fecha_edicion(mensaje_usuario)
        nuevo_lugar = _extraer_nuevo_lugar_edicion(mensaje_usuario)
        if not nuevo_titulo and not nuevas_horas and not nueva_fecha and not nuevo_lugar:
            nuevo_titulo = mensaje_usuario.strip().capitalize()
        return _preparar_confirmacion_edicion(evento, nuevo_titulo, nuevas_horas,
                                              nueva_fecha, nuevo_lugar)

    titulo = mensaje_usuario.strip()
    datos['titulo'] = titulo
    estado_sesion['accion_pendiente'] = 'crear_evento'
    estado_sesion['ultimo_tema'] = 'calendario'

    fecha_legible = formatear_fecha_legible(datos['fecha_resuelta'].isoformat())
    horas = datos.get('horas_resueltas') or []
    if horas:
        detalle_hora = f" de {_hora_float_a_texto(horas[0])}"
        if len(horas) > 1:
            detalle_hora += f" a {_hora_float_a_texto(horas[1])}"
    else:
        detalle_hora = f" (sin hora puntual -- lo agendaré a las {_texto_hora_por_defecto()} si confirmas)"
    return (f'¿Confirmas agendar "{titulo}" el {fecha_legible}{detalle_hora} '
            f'en [{memoria_local.etiqueta_cuenta(datos["cuenta"])}]? (sí/no)'
            + _aviso_horario_chat(datos['fecha_resuelta'], horas)
            + _aviso_cruce_chat(datos['fecha_resuelta'], horas, datos['cuenta']))


def _resolver_solicitud_agendar_directo(mensaje_usuario):
    global estado_sesion

    fecha_evento = _extraer_fecha_de_pregunta(mensaje_usuario)
    if not fecha_evento:
        estado_sesion['ultimo_tema'] = 'calendario'
        return ("¿Para qué día es el evento? Dime la fecha (por ejemplo "
                "\"hoy\", \"mañana\" o \"el 20 de agosto\") y lo agendo.")

    horas = _extraer_horas_libres(mensaje_usuario)
    titulo = _extraer_titulo_evento_directo(mensaje_usuario)
    # RF8 -- el lugar tambien se puede decir al crear ("en el salon 302").
    lugar = _extraer_nuevo_lugar_edicion(mensaje_usuario) or analisis.lugar_en_texto(mensaje_usuario)
    cuenta_filtrada = detectar_cuenta_mencionada(mensaje_usuario)
    cuenta_destino = cuenta_filtrada or _cuenta_por_defecto()
    estado_sesion['ultimo_tema'] = 'calendario'

    if not titulo:
        estado_sesion['accion_pendiente'] = 'esperando_titulo_evento'
        estado_sesion['datos_pendientes'] = {
            'cuenta': cuenta_destino, 'fecha_resuelta': fecha_evento,
            'horas_resueltas': horas, 'lugar': lugar,
            'descripcion': 'Creado por Stefany a partir de una instrucción directa.',
            'modo': 'crear_nuevo',
        }
        return "No logré identificar el nombre del evento. ¿Me confirmas cómo debo llamarlo?"

    estado_sesion['accion_pendiente'] = 'crear_evento'
    estado_sesion['datos_pendientes'] = {
        'cuenta': cuenta_destino, 'titulo': titulo,
        'fecha_resuelta': fecha_evento, 'horas_resueltas': horas,
        'lugar': lugar,
        'descripcion': 'Creado por Stefany a partir de una instrucción directa.',
    }

    fecha_legible = formatear_fecha_legible(fecha_evento.isoformat())
    if horas:
        detalle_hora = f" de {_hora_float_a_texto(horas[0])}"
        if len(horas) > 1:
            detalle_hora += f" a {_hora_float_a_texto(horas[1])}"
    else:
        detalle_hora = f" (sin hora puntual -- lo agendaré a las {_texto_hora_por_defecto()} si confirmas)"
    return (f'¿Confirmas agendar "{titulo}" el {fecha_legible}{detalle_hora} '
            f'en [{memoria_local.etiqueta_cuenta(cuenta_destino)}]? (sí/no)'
            + _aviso_horario_chat(fecha_evento, horas)
            + _aviso_cruce_chat(fecha_evento, horas, cuenta_destino))


def _recordar_evento_creado(alias, titulo, inicio_iso):
    global estado_sesion
    try:
        fecha_evento = datetime.fromisoformat(inicio_iso).date()
        candidatos = _buscar_eventos_para_editar(fecha_evento, alias)
        for e in candidatos:
            if e['titulo'] == titulo:
                estado_sesion['ultimo_evento_referenciado'] = e
                return
    except Exception:
        pass


def _es_seguimiento_de_edicion(mensaje_usuario):
    if not estado_sesion.get('ultimo_evento_referenciado'):
        return False
    return bool(_extraer_nuevo_titulo_edicion(mensaje_usuario) or _extraer_nueva_hora_edicion(mensaje_usuario))


estado_sesion = {
    'accion_pendiente': None,
    'datos_pendientes': {},
    'ultimo_tema': None,
    'ultimo_evento_referenciado': None,
}


def _resolver_confirmacion(mensaje_usuario):
    global estado_sesion
    accion = estado_sesion['accion_pendiente']
    datos = estado_sesion['datos_pendientes']
    tema_previo = estado_sesion.get('ultimo_tema')
    estado_sesion = {'accion_pendiente': None, 'datos_pendientes': {}, 'ultimo_tema': tema_previo,
                      'ultimo_evento_referenciado': estado_sesion.get('ultimo_evento_referenciado')}

    if accion == 'crear_evento':
        inicio_iso, fin_iso, hora_default_usada = _construir_iso_evento(datos)
        if not inicio_iso:
            return ("⚠️ No logré determinar con certeza la fecha del compromiso. "
                    "¿Puedes indicarme la fecha exacta?")

        if quiere_todas_las_cuentas(mensaje_usuario):
            aliases_destino = list(gc.cuentas_conectadas.keys())
        else:
            aliases_destino = [datos.get('cuenta') or _cuenta_por_defecto()]

        links = []
        for alias in aliases_destino:
            link = gc.crear_evento_calendar(
                alias=alias, titulo=datos.get('titulo', 'Nuevo evento'),
                fecha_inicio_iso=inicio_iso, fecha_fin_iso=fin_iso,
                descripcion=datos.get('descripcion', ''),
                lugar=datos.get('lugar', ''),
            )
            if link:
                links.append((alias, link))

        aviso_hora = ("\n(No encontré una hora puntual, así que lo agendé a las "
                       f"{_texto_hora_por_defecto()} -- dime si quieres que lo mueva a otra hora.)") if hora_default_usada else ""

        if not links:
            return "⚠️ No pude crear el evento. Verifica que la(s) cuenta(s) estén conectadas."
        if len(links) == 1:
            alias, link = links[0]
            _recordar_evento_creado(alias, datos.get('titulo', 'Nuevo evento'), inicio_iso)
            return f"✅ Evento creado correctamente en [{memoria_local.etiqueta_cuenta(alias)}].\nPuedes verlo aquí: {link}{aviso_hora}"
        detalle = "\n".join(f"- [{alias}]: {link}" for alias, link in links)
        return f"✅ Evento creado en {len(links)} cuentas:\n{detalle}{aviso_hora}"

    elif accion == 'editar_evento':
        evento = datos['evento_a_editar']
        link = gc.editar_evento_calendar(
            alias=evento['cuenta'], event_id=evento['id'],
            titulo=datos.get('nuevo_titulo'),
            fecha_inicio_iso=datos.get('nueva_fecha_inicio_iso'),
            fecha_fin_iso=datos.get('nueva_fecha_fin_iso'),
            lugar=datos.get('nuevo_lugar'),
        )
        if link:
            evento_actualizado = dict(evento)
            if datos.get('nuevo_titulo'):
                evento_actualizado['titulo'] = datos['nuevo_titulo']
            if datos.get('nuevo_lugar'):
                evento_actualizado['lugar'] = datos['nuevo_lugar']
            if datos.get('nueva_fecha_inicio_iso'):
                evento_actualizado['inicio'] = datos['nueva_fecha_inicio_iso']
            if datos.get('nueva_fecha_fin_iso'):
                evento_actualizado['fin'] = datos['nueva_fecha_fin_iso']
            estado_sesion['ultimo_evento_referenciado'] = evento_actualizado
            return f"✅ Evento actualizado correctamente en [{evento['cuenta']}].\nPuedes verlo aquí: {link}"
        return "⚠️ No pude editar el evento. Intenta de nuevo."

    elif accion == 'eliminar_evento':
        evento = datos['evento_a_eliminar']
        if not gc.eliminar_evento_calendar(evento['cuenta'], evento['id']):
            return "⚠️ No pude eliminar el evento. Intenta de nuevo."

        estado_sesion['ultimo_evento_referenciado'] = evento
        estado_sesion['ultimo_tema'] = 'calendario'
        respuesta = (f'🗑️ Eliminé "{evento["titulo"]}" de '
                     f'[{memoria_local.etiqueta_cuenta(evento["cuenta"])}].')

        # Cancelar la reunión y avisar a quien venía son dos cosas distintas:
        # la segunda se ofrece, no se hace sola.
        destinatario = _extraer_destinatario(evento.get('descripcion', ''))
        if destinatario:
            estado_sesion['accion_pendiente'] = 'redactar_cancelacion'
            estado_sesion['datos_pendientes'] = {
                'titulo': evento['titulo'], 'destinatario': destinatario,
                'cuenta': evento['cuenta'],
            }
            respuesta += f"\n¿Quieres que redacte un correo avisando a {destinatario}? (sí/no)"
        else:
            respuesta += ("\nSi quieres avisar a alguien, dime: \"redacta un correo "
                          "de cancelación para correo@ejemplo.com\".")
        return respuesta

    elif accion == 'redactar_cancelacion':
        return _preparar_borrador_cancelacion(
            datos.get('titulo', ''), datos.get('destinatario', ''), datos.get('cuenta', ''))

    elif accion == 'confirmar_borrador_correo':
        # Antes esto ENVIABA el correo. Ahora solo deja un borrador en Gmail:
        # el usuario lo revisa y decide. El sistema nunca envía nada.
        alias = datos.get('cuenta') or _cuenta_por_defecto()
        id_borrador = gc.crear_borrador(
            alias=alias,
            destinatario=datos.get('destinatario', ''),
            asunto=datos.get('asunto', ''),
            cuerpo=datos.get('cuerpo', ''),
            id_hilo=datos.get('hilo'),
        )
        if id_borrador:
            return (f"✅ Dejé el borrador en [{alias}]. Ábrelo en Gmail, revísalo "
                    f"y envíalo tú cuando estés de acuerdo — yo no envío correos.")
        return "⚠️ No pude crear el borrador. Verifica que la cuenta esté conectada."

    return "Entendido."


# ---------------------------------------------------------------
# Ensamblado: correos por cuenta + calendario completo
# ---------------------------------------------------------------

def revisar_correos_y_calendario(mensaje_usuario, limite=None, progreso=None):
    """Revisión completa de la bandeja, en cuatro fases.

    1. Metadatos de TODOS los no leídos (barato, sin cuerpos, sin modelo).
    2. Colapsar hilos y clasificar localmente (instantáneo, sin modelo).
    3. Descargar el cuerpo y llamar al modelo SOLO donde aporta.
    4. Marcar como vistos únicamente los que se procesaron bien.

    'progreso' es un callable(texto, hechos, total) que la interfaz usa para
    mostrar en qué va el proceso en vez de un spinner mudo.
    """
    global estado_sesion

    inicio = time.time()

    def avisar(texto, hechos=0, total=0):
        if progreso:
            try:
                progreso(texto, hechos, total)
            except Exception:
                pass

    cuenta_filtrada = detectar_cuenta_mencionada(mensaje_usuario)
    # N5 — "revísame los correos del 15 de junio": se trae SOLO ese día, leídos
    # y no leídos. Sin fecha, el comportamiento de siempre (todos los no leídos).
    fecha_pedida = calcons.extraer_fecha_de_consulta_correos(mensaje_usuario)

    # ---------- FASE 1: metadatos de toda la bandeja ----------
    avisar("Revisando tu bandeja...")

    def _descarga(hechos, total):
        avisar("Revisando tu bandeja...", hechos, total)

    if cuenta_filtrada:
        correos_por_cuenta = {cuenta_filtrada: gc.leer_correos_cuenta(
            cuenta_filtrada, limite=limite, progreso=_descarga, fecha=fecha_pedida)}
        eventos_por_cuenta = {cuenta_filtrada: gc.leer_eventos_cuenta(cuenta_filtrada, cantidad=50, dias=30)}
    else:
        correos_por_cuenta = gc.leer_correos_todas_cuentas(limite=limite, progreso=_descarga,
                                                            fecha=fecha_pedida)
        eventos_por_cuenta = gc.leer_eventos_todas_cuentas(cantidad=50, dias=30)

    dia_texto = f" del {analisis.fecha_en_palabras(fecha_pedida)}" if fecha_pedida else ""
    total_sin_leer = sum(len(c) for c in correos_por_cuenta.values())
    if total_sin_leer == 0:
        destino = f"la cuenta [{cuenta_filtrada}]" if cuenta_filtrada else "ninguna de tus cuentas"
        sin_leer = "" if fecha_pedida else " sin leer"
        return {
            "tipo": "revision_correos", "total": 0, "total_hilos": 0,
            "grupos": [], "conteo_senales": {}, "segundos": round(time.time() - inicio, 1),
            "texto": f"No tienes correos{sin_leer}{dia_texto} en {destino}.",
        }

    # ---------- FASE 2: hilos + clasificación (sin modelo) ----------
    avisar("Organizando lo que encontré...")
    todos = [c for lista in correos_por_cuenta.values() for c in lista]
    hilos = clasificador.agrupar_por_hilo(todos)
    # El perfil (remitentes y categorías importantes) se lee UNA vez por revisión.
    perfil = memoria_local.cargar_perfil()
    pares = [(correo, clasificador.clasificar(correo, perfil)) for correo in hilos]

    # ---------- FASE 3: resumir solo lo que lo amerita ----------
    pendientes = []
    for correo, clasif in pares:
        cacheado = memoria_local.resumen_en_cache(correo["cuenta"], correo["id"])
        if cacheado:
            # Se revalida al LEER, no solo al escribir: los resúmenes
            # guardados antes de que existieran estas comprobaciones traían
            # cuentas inventadas y bloques de calendario alucinados, y se
            # seguían mostrando intactos.
            saneado, cambiado = analisis.sanear_resumen_guardado(cacheado, correo)
            if cambiado:
                saneado = {**cacheado, **saneado}
                memoria_local.guardar_resumen(correo["cuenta"], correo["id"], saneado)
            correo["resumen"] = saneado.get("resumen", "")
            correo["analisis"] = saneado.get("analisis", "")
            correo["origen_resumen"] = "cache"
            correo["cuerpo_vacio"] = cacheado.get("vacio", False)
            correo["imagenes_adjuntas"] = [None] * cacheado.get("n_adjuntas", 0)
            correo["imagenes_remotas"] = [None] * cacheado.get("n_remotas", 0)
        elif clasif["necesita_llm"] or clasif["senales"]:
            # Un correo con señal (urgente, requiere respuesta, fecha límite)
            # va a aparecer arriba en 'destacados', así que sí merece resumen
            # aunque su categoría por sí sola no lo pidiera.
            pendientes.append((correo, clasif))
        else:
            correo["resumen"] = clasificador.resumen_local(correo, clasif)
            correo["analisis"] = ""
            correo["origen_resumen"] = "local"

    # El cuerpo completo se descarga SOLO de los que van al modelo.
    if pendientes:
        avisar("Leyendo los correos importantes...", 0, len(pendientes))
        por_cuenta = {}
        for correo, _ in pendientes:
            por_cuenta.setdefault(correo["cuenta"], []).append(correo)
        for alias, lista in por_cuenta.items():
            try:
                gc.hidratar_cuerpos(
                    alias, lista,
                    progreso=lambda h, t: avisar("Leyendo los correos importantes...", h, t),
                )
                # Contexto del hilo para los que son respuesta (D).
                gc.hidratar_hilo_anterior(alias, lista)
            except Exception as e:
                print(f"⚠️ Error descargando cuerpos de [{alias}]: {e}")

    contexto_calendario_texto = analisis.contexto_calendario_completo(eventos_por_cuenta)
    global _ultimo_contexto_calendario, _ultimos_eventos
    _ultimo_contexto_calendario = contexto_calendario_texto
    _ultimos_eventos = eventos_por_cuenta
    hallazgos = []
    accion_pendiente_detectada = None
    procesados_ok = {}

    # Los correos van al modelo DE UNO EN UNO.
    #
    # Se probó procesarlos por lotes y salió PEOR: 391 s frente a 281 s
    # sobre la misma bandeja. Medido, no estimado. La razón es que en un lote
    # todas las secuencias generan hasta que la más larga termina: un correo
    # que habría parado a los 80 tokens sigue ocupando la GPU hasta los 300 de
    # su compañero de lote, y además hay que rellenar todos los prompts hasta
    # la longitud del mayor (los reales van de 1.049 a 1.692 tokens). Lo que
    # se gana compartiendo la lectura de los pesos se pierde ahí.
    for indice, (correo, clasif) in enumerate(pendientes, 1):
        avisar("Resumiendo tus correos...", indice, len(pendientes))
        try:
            texto_modelo = prompts.analizar_correo(correo, contexto_calendario_texto)
            texto_modelo = analisis._sanear_texto_modelo(texto_modelo, correo)
            texto_modelo = analisis.verificar_conflicto_horario(texto_modelo, eventos_por_cuenta)
        except Exception as e:
            # Si el modelo falla, el correo NO se pierde ni se da por
            # procesado: se muestra con su resumen local y se reintentará
            # en la próxima revisión.
            print(f"⚠️ Falló el análisis de {correo['id']}: {e}")
            correo["resumen"] = clasificador.resumen_local(correo, clasif)
            correo["analisis"] = ""
            correo["origen_resumen"] = "error"
            continue

        if "📊 Análisis:" in texto_modelo:
            resumen, analisis_texto = texto_modelo.split("📊 Análisis:", 1)
            resumen, analisis_texto = resumen.strip(), analisis_texto.strip()
            hallazgos.append(analisis_texto)

            if accion_pendiente_detectada is None:
                if "¿cancelo" in analisis_texto.lower():
                    datos_cancel = analisis.extraer_datos_cancelacion(analisis_texto)
                    if datos_cancel:
                        accion_pendiente_detectada = ('conflicto', datos_cancel)
                elif ("¿te gustaría que lo agende" in analisis_texto.lower()
                      and not clasificador.anuncia_cancelacion(
                          correo.get("asunto", ""), correo.get("cuerpo", ""), resumen)):
                    datos_evento = analisis.extraer_datos_evento(analisis_texto) or {}
                    fecha_evento = analisis.fecha_referida_correo(correo, analisis_texto)
                    if fecha_evento and not analisis.fecha_ya_paso(fecha_evento):
                        datos_evento['tema'] = correo['asunto']
                        datos_evento['cuenta'] = correo['cuenta']   # nunca la del modelo
                        datos_evento['fecha_texto'] = (
                            f"{fecha_evento.day} de {prompts.MESES_ES[fecha_evento.month - 1]}")
                        accion_pendiente_detectada = ('agendar', datos_evento)
        else:
            resumen, analisis_texto = texto_modelo.strip(), ""

        correo["resumen"] = resumen
        correo["analisis"] = analisis_texto
        correo["origen_resumen"] = "modelo"

        # Solo AQUÍ, con el resumen ya en mano, se considera procesado.
        # Se guarda también el estado de las imágenes: en las revisiones
        # siguientes el cuerpo NO se descarga (viene de caché), así que sin
        # esto la interfaz dejaba de ofrecer "Leer las imágenes" en cuanto el
        # correo se cacheaba una vez.
        memoria_local.guardar_resumen(correo["cuenta"], correo["id"], {
            "resumen": resumen, "analisis": analisis_texto,
            "vacio": correo.get("cuerpo_vacio", False),
            "n_adjuntas": _contar_imagenes_utiles(correo)[0],
            "n_remotas": _contar_imagenes_utiles(correo)[1],
        })
        procesados_ok.setdefault(correo["cuenta"], []).append(correo["id"])

    # ---------- FASE 4: cierre ----------
    # Aquí se marcaban los correos en correos_vistos.json, un archivo que
    # nadie leía nunca (eliminado el 2-oct-2026). Lo que de verdad evita
    # reanalizar es la caché de resúmenes, que ya se escribió arriba y SOLO
    # para los correos que salieron bien: si Modal falla, no se guarda nada y
    # el correo se reintenta en la siguiente revisión.
    # procesados_ok se conserva porque alimenta la métrica 'llamadas_modelo'.
    avisar("Casi termino...")

    # Todos los correos mostrados son candidatos a marcarse como leídos en
    # Gmail; la decisión es del usuario (botón en la tarjeta).
    ids_mostrados = {}
    for correo, _ in pares:
        ids_mostrados.setdefault(correo["cuenta"], []).append(correo["id"])

    if MARCAR_LEIDOS_AUTOMATICAMENTE:
        marcar_revision_como_leida(ids_mostrados)

    # ---------- Armado del resultado ----------
    for correo, _ in pares:
        correo["fecha_legible"] = prompts.formatear_fecha_correo(correo.get("fecha", ""))

    grupos_crudos = clasificador.agrupar_por_categoria(pares, perfil.get("categorias_importantes"))

    # Resumen por categoría: una llamada al modelo por acordeón, sobre los
    # resúmenes ya calculados.
    avisar("Resumiendo cada categoría...", 0, len(grupos_crudos))
    sintesis = {}
    for indice, grupo in enumerate(grupos_crudos, 1):
        avisar("Resumiendo cada categoría...", indice, len(grupos_crudos))
        sintesis[grupo["categoria"]] = resumen_por_categoria(grupo)

    grupos = _vista_grupos(grupos_crudos)
    for grupo in grupos:
        grupo["sintesis"] = sintesis.get(grupo["categoria"], "")

    lista_destacados = _vista_correos(clasificador.destacados(pares))
    # En una consulta por fecha solo se muestra lo que sale de ESOS correos:
    # mezclar pendientes de otros días era confuso. Todo lo no atendido queda
    # en el botón "Pendientes" de la barra lateral.
    acciones = acciones_calendario(pares, eventos_por_cuenta)
    if not fecha_pedida:
        acciones = acciones_pendientes() + acciones
    memoria_local.guardar_sugerencias_pendientes(
        [a for a in acciones if a["tipo"] in ("agendar", "conflicto")])

    if accion_pendiente_detectada:
        tipo, datos = accion_pendiente_detectada
        if tipo == 'agendar':
            estado_sesion['accion_pendiente'] = 'crear_evento'
            estado_sesion['datos_pendientes'] = {
                'cuenta':      datos.get('cuenta', cuenta_filtrada or ''),
                'titulo':      datos.get('tema', 'Nuevo evento'),
                'fecha_texto': datos.get('fecha_texto', ''),
                'hora_texto':  datos.get('hora_texto', ''),
                'descripcion': 'Creado por Stefany desde correo.',
            }
        elif tipo == 'conflicto':
            borrador = prompts.generar_correo_cancelacion(datos['titulo'], "")
            borrador['cuenta'] = datos.get('alias') or cuenta_filtrada or ''
            estado_sesion['accion_pendiente'] = 'confirmar_borrador_correo'
            estado_sesion['datos_pendientes'] = borrador

    return {
        "tipo":           "revision_correos",
        "total":          total_sin_leer,
        "total_hilos":    len(pares),
        "grupos":         grupos,
        "destacados":     lista_destacados,
        "ids_mostrados":  ids_mostrados,
        "acciones_calendario": acciones,
        "ya_marcados":    MARCAR_LEIDOS_AUTOMATICAMENTE,
        "conteo_senales": clasificador.conteo_por_senal(pares),
        "hallazgos":      hallazgos,
        "llamadas_modelo": len(procesados_ok and [i for l in procesados_ok.values() for i in l] or []),
        "segundos":       round(time.time() - inicio, 1),
        "texto":          _resultado_como_texto(pares, grupos, total_sin_leer, hallazgos,
                                                dia_texto),
        "dia_consultado": fecha_pedida.isoformat() if fecha_pedida else "",
    }



# Contexto de la última revisión: lo reutiliza el resumen bajo demanda para no
# volver a consultar el calendario por un solo correo.
_ultimo_contexto_calendario = None
_ultimos_eventos = {}



# Marcar correos como leídos actúa sobre la bandeja REAL. Se deja en manos del
# usuario (un botón en la tarjeta) en vez de hacerlo automáticamente: en la
# primera revisión serían cientos de correos de golpe y deshacerlo a mano es
# un dolor. Cambia a True si prefieres que ocurra solo al terminar.
MARCAR_LEIDOS_AUTOMATICAMENTE = False

# Cuántas filas "propone un encuentro pero falta un dato" (CU2 · A3) se
# muestran como máximo en una revisión. Son las menos accionables de todas,
# así que van al final del bloque y con tope.
MAXIMO_FILAS_INCOMPLETAS = 3


def marcar_revision_como_leida(ids_por_cuenta):
    """Marca en Gmail los correos indicados. Devuelve (marcados, errores).

    ids_por_cuenta: {alias: [id, ...]}
    """
    marcados, errores = 0, []
    for alias, ids in (ids_por_cuenta or {}).items():
        try:
            marcados += gc.marcar_leidos(alias, ids)
        except Exception as e:
            errores.append(f"{alias}: {e}")
    return marcados, errores


def leer_imagenes_correo(cuenta, msg_id, incluir_remotas=False, progreso=None):
    """Lee el texto de las imágenes de un correo y rehace su resumen.

    incluir_remotas=False (por defecto) solo lee las imágenes que viajan
    dentro del correo. Las alojadas en el servidor del remitente exigen que el
    usuario lo pida: descargarlas le confirma que abrió el correo.

    Devuelve {'resumen', 'analisis', 'texto_ocr', 'imagenes_leidas'}.
    """
    def avisar(t):
        if progreso:
            try:
                progreso(t)
            except Exception:
                pass

    if not ocr.hay_ocr_disponible():
        return {"resumen": "El lector de imágenes no está instalado "
                           "(pip install rapidocr-onnxruntime).",
                "analisis": "", "texto_ocr": "", "imagenes_leidas": 0}

    correos = gc.leer_correos_por_id(cuenta, [msg_id])
    if not correos:
        return {"resumen": "No pude recuperar este correo.", "analisis": "",
                "texto_ocr": "", "imagenes_leidas": 0}
    correo = correos[0]

    imagenes = []
    for adjunta in correo.get("imagenes_adjuntas", []):
        if ocr.parece_decorativa(adjunta.get("nombre", ""), adjunta.get("tamano", 0)):
            continue
        avisar("Descargando las imágenes del correo...")
        datos = gc.descargar_adjunto(cuenta, msg_id, adjunta.get("id_adjunto"))
        if datos:
            imagenes.append((adjunta.get("nombre") or "adjunta", datos))

    if incluir_remotas:
        for url in correo.get("imagenes_remotas", []):
            if ocr.parece_decorativa(url=url):
                continue
            if len(imagenes) >= ocr.MAX_IMAGENES:
                break
            avisar("Descargando las imágenes del correo...")
            datos = ocr.descargar_imagen_remota(url)
            if datos:
                imagenes.append((url.rsplit('/', 1)[-1][:40], datos))

    if not imagenes:
        return {"resumen": "No encontré imágenes con contenido legible en este correo.",
                "analisis": "", "texto_ocr": "", "imagenes_leidas": 0}

    avisar(f"Leyendo {len(imagenes)} imágenes...")
    texto_ocr = ocr.leer_imagenes(imagenes)
    if not texto_ocr.strip():
        return {"resumen": "Las imágenes de este correo no contienen texto legible.",
                "analisis": "", "texto_ocr": "", "imagenes_leidas": len(imagenes)}

    # El texto del OCR pasa a ser el cuerpo del correo, y a partir de ahí el
    # flujo es el mismo de siempre: el modelo resume y el saneador valida
    # contra ese texto (así una fecha leída de la imagen ya NO se considera
    # inventada).
    # El texto de la imagen va ETIQUETADO y con sitio garantizado: antes se
    # pegaba al final y, con un cuerpo largo, el recorte a 1500 caracteres se
    # lo comía justo a él, que es lo que trae los datos del evento.
    cuerpo_previo = (correo.get("cuerpo", "") or "").strip()[:700]
    correo["cuerpo"] = (f"{cuerpo_previo}\n\nTEXTO DENTRO DE LAS IMÁGENES "
                        f"(aquí suelen estar la fecha, la hora y el lugar):\n"
                        f"{texto_ocr.strip()[:800]}").strip()
    correo["cuerpo_vacio"] = False

    avisar("Generando el resumen...")
    contexto = _ultimo_contexto_calendario
    if contexto is None:
        eventos = {cuenta: gc.leer_eventos_cuenta(cuenta, cantidad=50, dias=30)}
        contexto = analisis.contexto_calendario_completo(eventos)
    else:
        eventos = _ultimos_eventos

    try:
        texto = prompts.analizar_correo(correo, contexto)
        texto = analisis._sanear_texto_modelo(texto, correo)
        texto = analisis.verificar_conflicto_horario(texto, eventos or {})
    except Exception as e:
        return {"resumen": f"Leí las imágenes pero falló el resumen: {e}",
                "analisis": "", "texto_ocr": texto_ocr,
                "imagenes_leidas": len(imagenes)}

    if "📊 Análisis:" in texto:
        resumen, analisis_texto = texto.split("📊 Análisis:", 1)
        resultado = {"resumen": resumen.strip(), "analisis": analisis_texto.strip()}
    else:
        resultado = {"resumen": texto.strip(), "analisis": ""}

    # El modelo suele resumir el texto de presentación y dejar fuera lo que
    # estaba en el afiche. Si es así, se añade por código lo concreto.
    datos_imagen = analisis.datos_clave_de_imagen(texto_ocr)
    if datos_imagen and not (set(analisis.horas_en_texto(resultado["resumen"]))
                             & set(analisis.horas_en_texto(texto_ocr))):
        resultado["resumen"] = f"{resultado['resumen']}\n🖼️ En la imagen: {datos_imagen}"

    memoria_local.guardar_resumen(cuenta, msg_id, resultado)

    # El bloque del calendario se arma durante la revisión, y esta lectura
    # ocurre después: si la imagen traía un compromiso ("todos los viernes a
    # las 3"), hay que ofrecer agendarlo aquí mismo.
    correo["analisis"] = resultado["analisis"]
    try:
        perfil = memoria_local.cargar_perfil()
        resultado["acciones_calendario"] = acciones_calendario(
            [(correo, clasificador.clasificar(correo, perfil))], eventos or {})
    except Exception as e:
        print(f"⚠️ No se pudieron calcular las acciones de calendario tras el OCR: {e}")
        resultado["acciones_calendario"] = []

    resultado["texto_ocr"] = texto_ocr
    resultado["imagenes_leidas"] = len(imagenes)
    return resultado



def acciones_calendario(pares, eventos_por_cuenta=None):
    """Reúne, de todos los análisis, lo que hay que decidir sobre el calendario.

    Tres tipos de hallazgo:
      - 'agendar'   → un compromiso real que no está en el calendario.
      - 'conflicto' → un compromiso que se cruza con un evento ya agendado.
      - 'cancelar'  → el correo cancela un evento que SÍ está en el calendario.

    Las cancelaciones se deciden por reglas antes de mirar el análisis: el
    modelo resumía "se cancela la clase" y a continuación ofrecía agendarla.
    Si el evento cancelado no está en el calendario, no se sugiere nada.
    Tampoco se sugiere nada sobre fechas que ya pasaron.

    La verificación del cruce NO la hace el modelo: la recalcula
    analisis_stefany.verificar_conflicto_horario() con aritmética real sobre
    los eventos del calendario, y corrige al modelo si se equivoca. Por eso la
    explicación que ve la usuaria ("revisé tu agenda y ese espacio está
    libre") es un dato comprobado, no una afirmación del modelo.
    """
    acciones = []
    for correo, clasif in pares:
        # Antes de exigir análisis: un correo de cancelación puede no traerlo.
        if _es_cancelacion(correo):
            evento = analisis.buscar_evento_cancelado(correo, eventos_por_cuenta)
            if evento:
                acciones.append({
                    "tipo": "cancelar",
                    "id": correo["id"],
                    "cuenta": correo["cuenta"],
                    "cuenta_evento": evento["alias"],
                    "id_evento": evento["id"],
                    "evento_existente": evento["titulo"],
                    "titulo": correo.get("asunto", "")[:80],
                    "remitente": correo.get("remitente", "").split("<")[0].strip(),
                    "explicacion": (
                        f"[{evento['alias']}] Este correo cancela '{evento['titulo']}', "
                        f"que está en tu calendario el "
                        f"{evento['fecha'].day} de {prompts.MESES_ES[evento['fecha'].month - 1]}."
                    ),
                })
            continue

        # CU3 — Cambio sobre un evento que ya existe (hora, lugar, documentos).
        if _es_cambio(correo):
            cambio = _accion_actualizar(correo, eventos_por_cuenta)
            if cambio:
                acciones.append(cambio)
                continue

        texto = correo.get("analisis") or ""
        if not texto:
            continue

        bajo = texto.lower()
        datos = analisis.extraer_datos_evento(texto)

        # La fecha la resuelve el código, contando "hoy" desde el día en que
        # se envió el correo. Sin fecha, o con una fecha ya pasada, no se
        # sugiere nada: agendar algo que ya ocurrió no sirve, y un botón que
        # termina en "No pude resolver la fecha" tampoco.
        fecha, hora_correo = _compromiso_del_correo(correo, texto)
        if analisis.ya_paso(fecha, hora_correo):
            pasada = _accion_pasada(correo, fecha, hora_correo)
            if pasada:
                acciones.append(pasada)
            continue

        if "¿te gustaría que lo agende" in bajo:
            if fecha is None:
                continue
            datos = datos or {}
            # La cuenta es SIEMPRE la del correo, nunca la que escriba el
            # modelo: un correo que llegó a una cuenta salía con la dirección
            # de otra cuenta conectada (copiada del
            # calendario del prompt) y se habría agendado en esa.
            explicacion = re.sub(analisis.PATRON_ALIAS, f'[{correo["cuenta"]}]',
                                 _explicacion_calendario(texto))
            # La hora resuelta por el código ("18:00", "6pm"…), no solo el
            # formato "6:00 p.m." que escribe a veces el modelo: sin ella la
            # fila se quedaba sin hora y dos eventos distintos parecían el mismo.
            hora_fila = hora_correo if hora_correo is not None else memoria_local.hora_por_defecto()
            aviso_horario = memoria_local.aviso_fuera_de_horario(fecha, hora_fila)
            acciones.append({
                "tipo": "agendar",
                "id": correo["id"],
                "cuenta": correo["cuenta"],
                "cuenta_destino": correo["cuenta"],
                "titulo": correo.get("asunto", "")[:80],
                "titulo_evento": analisis.titulo_sugerido_evento(correo),
                "remitente": correo.get("remitente", "").split("<")[0].strip(),
                "remitente_email": correo.get("remitente_email", ""),
                "fecha_iso": fecha.isoformat(),
                "fecha_texto": f"{fecha.day} de {prompts.MESES_ES[fecha.month - 1]}",
                "hora_texto": (_hora_float_a_texto(hora_correo) if hora_correo is not None
                               else datos.get("hora_texto", "")),
                "explicacion": _explicacion_con_fecha(explicacion, fecha),
                "aviso_horario": aviso_horario,
                "analisis": texto,
            })

        elif "¿cancelo" in bajo:
            # CU4 — El cruce se comprueba contra el calendario real, y la fila
            # lleva fecha y hora resueltas: sin ellas, "Agendar de todos modos"
            # terminaba en "No pude resolver la fecha del evento".
            fecha_c, hora_c = _compromiso_del_correo(correo, texto)
            cruces = _eventos_que_cruzan(fecha_c, hora_c, eventos_por_cuenta)
            if not cruces:
                # El modelo dice que hay cruce pero el calendario no lo
                # confirma: se deja para la segunda pasada (agendar por reglas).
                continue
            hora_txt = _hora_float_a_texto(hora_c)
            # CU4 — "el asistente puede generar una propuesta de acción":
            # se busca el siguiente hueco libre dentro del horario del perfil.
            libre_fecha, libre_hora, libre_texto = _espacio_libre(fecha_c, hora_c, eventos_por_cuenta)
            acciones.append({
                "tipo": "conflicto",
                "libre_iso": libre_fecha.isoformat() if libre_fecha else "",
                "libre_hora": _hora_float_a_texto(libre_hora) if libre_hora is not None else "",
                "libre_texto": libre_texto,
                "id": correo["id"],
                "cuenta": correo["cuenta"],
                "cuenta_destino": correo["cuenta"],
                "titulo": correo.get("asunto", "")[:80],
                "titulo_evento": analisis.titulo_sugerido_evento(correo),
                "remitente": correo.get("remitente", "").split("<")[0].strip(),
                "remitente_email": correo.get("remitente_email", ""),
                "fecha_iso": fecha_c.isoformat(),
                "fecha_texto": f"{fecha_c.day} de {prompts.MESES_ES[fecha_c.month - 1]}",
                "hora_texto": hora_txt,
                "evento_existente": cruces[0]["titulo"],
                "cuenta_existente": cruces[0]["alias"],
                # hora_txt ya termina en punto ("5:00 p.m."): no se añade otro.
                "explicacion": (f"[{correo['cuenta']}] Propone un compromiso el "
                                f"{analisis.fecha_en_palabras(fecha_c)} a las {hora_txt} "
                                + " ".join(_texto_cruce(ev) for ev in cruces[:2])),
                "aviso_horario": memoria_local.aviso_fuera_de_horario(fecha_c, hora_c),
                "analisis": texto,
            })

    # D — Segunda pasada POR REGLAS: correos que proponen un encuentro con
    # fecha y hora, a los que el modelo no les escribió análisis. Caso real:
    # "quisiera saber si es posible que nos reunamos el próximo sábado a las
    # 2pm" salió sin bloque de análisis y, por tanto, sin botón Agendar.
    con_accion = {a["id"] for a in acciones}
    incompletas = []
    for correo, clasif in pares:
        if correo["id"] not in con_accion:
            accion = _accion_agendar_por_reglas(correo, clasif, eventos_por_cuenta)
            if not accion:
                continue
            # Las filas "falta un dato" (CU2 · A3) van aparte y con tope: son
            # las menos accionables, y en una bandeja de 480 correos podrían
            # enterrar los compromisos que sí están completos.
            if accion["tipo"] == "incompleto":
                incompletas.append(accion)
            else:
                acciones.append(accion)

    return _fusionar_repetidos(acciones) + incompletas[:MAXIMO_FILAS_INCOMPLETAS]


def _fusionar_repetidos(acciones):
    """N2 y N3 — un evento por fila, y aviso si dos distintos coinciden.

    Dos correos del mismo evento (la invitación y el recordatorio) proponían
    dos filas idénticas. Se fusionan si coinciden cuenta, fecha y hora Y sus
    nombres comparten alguna palabra que los identifique. Si coinciden en
    horario pero son eventos distintos, se conservan los dos y se avisa del
    choque: eso es lo que pide CU4.
    """
    resultado, por_hueco = [], {}
    for accion in acciones:
        if accion["tipo"] not in ("agendar", "conflicto") or not accion.get("fecha_iso"):
            resultado.append(accion)
            continue

        hueco = (accion.get("cuenta_destino"), accion["fecha_iso"], accion.get("hora_texto"))
        nombre = accion.get("titulo_evento") or accion.get("titulo", "")
        clave_nueva, _ = analisis._palabras_clave(nombre)

        gemela = None
        for anterior in por_hueco.get(hueco, []):
            clave_previa, _ = analisis._palabras_clave(
                anterior.get("titulo_evento") or anterior.get("titulo", ""))
            if clave_nueva & clave_previa:
                gemela = anterior
                break

        if gemela is not None:
            gemela["correos_relacionados"] = gemela.get("correos_relacionados", 1) + 1
            gemela["explicacion"] = re.sub(r"\s*\(mencionado en \d+ correos\)$", "",
                                           gemela["explicacion"])
            gemela["explicacion"] += f" (mencionado en {gemela['correos_relacionados']} correos)"
            continue

        for otro in por_hueco.get(hueco, []):
            aviso = (f"⚠️ Coincide en horario con '"
                     f"{otro.get('titulo_evento') or otro.get('titulo', '')}', que también propones.")
            if aviso not in accion["explicacion"]:
                accion["explicacion"] += f" {aviso}"

        por_hueco.setdefault(hueco, []).append(accion)
        resultado.append(accion)
    return resultado


def _accion_recurrente(correo, periodicidad, hora, eventos_por_cuenta):
    """Fila para un evento que se repite: se agenda UN MES y luego se pregunta."""
    primera = analisis.primera_fecha_recurrente(periodicidad, hora)
    if primera is None:
        return None
    hasta = analisis.sumar_un_mes(primera)
    cuenta = correo["cuenta"]
    hora_txt = _hora_float_a_texto(hora)

    return {
        "tipo": "agendar",
        "origen": "reglas",
        "recurrencia": {"freq": periodicidad["freq"], "byday": periodicidad["byday"],
                        "hasta": hasta.isoformat(), "texto": periodicidad["texto"]},
        "id": correo["id"],
        "cuenta": cuenta,
        "cuenta_destino": cuenta,
        "titulo": correo.get("asunto", "")[:80],
        "titulo_evento": analisis.titulo_sugerido_evento(correo),
        "remitente": correo.get("remitente", "").split("<")[0].strip(),
        "remitente_email": correo.get("remitente_email", ""),
        "fecha_iso": primera.isoformat(),
        "fecha_texto": f"{primera.day} de {prompts.MESES_ES[primera.month - 1]}",
        "hora_texto": hora_txt,
        "explicacion": (f"[{cuenta}] Se repite {periodicidad['texto']} a las {hora_txt} "
                        f"Empezaría el {analisis.fecha_en_palabras(primera)} y se agendaría "
                        f"un mes, hasta el {analisis.fecha_en_palabras(hasta)}."),
        "aviso_horario": memoria_local.aviso_fuera_de_horario(primera, hora),
        "analisis": "",
    }


def _accion_agendar_por_reglas(correo, clasif, eventos_por_cuenta):
    """Fila "Agendar" construida sin el modelo, o None.

    Exige: señal de posible evento, que no sea masivo, promoción ni
    cancelación, una fecha y una hora que el código resuelva, que la fecha no
    haya pasado y que no haya ya un evento a esa hora en la cuenta del correo.
    """
    clasif = clasif or {}
    if ("propone_evento" not in (clasif.get("senales") or [])
            or clasif.get("es_masivo")
            or clasif.get("categoria") in ("promocional", "redes_sociales")
            or _es_cancelacion(correo)):
        return None

    # En las revisiones siguientes el cuerpo no se descarga (viene de caché),
    # así que lo leído en la imagen se recupera de la línea que el propio
    # código escribió en el resumen ("🖼️ En la imagen: ..."). Del resumen solo
    # se toma esa línea: el resto lo escribió el modelo.
    datos_imagen = re.search(r'🖼️ En la imagen:\s*(.+)', correo.get("resumen", "") or "")
    texto_correo = (f"{correo.get('asunto', '')}\n"
                    f"{correo.get('cuerpo') or correo.get('fragmento') or ''}\n"
                    f"{datos_imagen.group(1) if datos_imagen else ''}")
    fecha = analisis.fecha_referida_correo(correo)
    horas = analisis.horas_en_texto(texto_correo)

    # "Todos los viernes, 3:00 a 4:00 p.m." no tiene una fecha concreta,
    # pero sí se puede agendar: como evento que se repite.
    periodicidad = analisis.periodicidad_en_texto(texto_correo)
    if periodicidad and horas:
        return _accion_recurrente(correo, periodicidad, horas[0], eventos_por_cuenta)

    if fecha is None or not horas:
        # CU2 · A3 — "el sistema identifica una posible reunión, pero la
        # información disponible no permite programarla: el asistente solicita
        # al usuario la información faltante". Antes esto devolvía None y el
        # correo desaparecía del bloque del calendario sin decir nada.
        return _accion_incompleta(correo, clasif, fecha, horas)
    if analisis.ya_paso(fecha, horas[0]):
        return _accion_pasada(correo, fecha, horas[0])

    inicio = horas[0]
    fin = inicio + memoria_local.duracion_por_defecto_horas()
    cuenta = correo["cuenta"]
    cruces = [ev for ev in analisis._eventos_en_fecha(fecha, eventos_por_cuenta or {})
              if analisis._solapan(inicio, fin, ev["inicio_h"], ev["fin_h"])]
    if any(ev["alias"] == cuenta for ev in cruces):
        return None      # ya hay algo a esa hora en esa cuenta: probablemente ya está agendado

    hora_txt = _hora_float_a_texto(inicio)
    explicacion = (f"[{cuenta}] Propone un encuentro el {analisis.fecha_en_palabras(fecha)} "
                   f"a las {hora_txt}, que no está en tu calendario.")
    if cruces:
        otro = cruces[0]
        explicacion += (f" Se cruza con '{otro['titulo']}' en "
                        f"[{memoria_local.etiqueta_cuenta(otro['alias'])}].")

    return {
        "tipo": "agendar",
        "origen": "reglas",
        "id": correo["id"],
        "cuenta": cuenta,
        "cuenta_destino": cuenta,
        "titulo": correo.get("asunto", "")[:80],
        "titulo_evento": analisis.titulo_sugerido_evento(correo),
        "remitente": correo.get("remitente", "").split("<")[0].strip(),
        "remitente_email": correo.get("remitente_email", ""),
        "fecha_iso": fecha.isoformat(),
        "fecha_texto": f"{fecha.day} de {prompts.MESES_ES[fecha.month - 1]}",
        "hora_texto": hora_txt,
        "explicacion": explicacion,
        "aviso_horario": memoria_local.aviso_fuera_de_horario(fecha, inicio),
        "analisis": "",
    }


def _explicacion_con_fecha(explicacion, fecha):
    """Cambia 'hoy'/'mañana' del análisis por la fecha real del compromiso.

    El modelo escribe "se hará hoy" porque no conoce la fecha de envío del
    correo; leído días después, ese "hoy" es falso.
    """
    en_palabras = analisis.fecha_en_palabras(fecha)
    relativa = r'(?:pasado\s+mañana|hoy|(?<!\bla )(?<!\besta )mañana)\b'
    nueva = re.sub(r'\bde\s+' + relativa, f'del {en_palabras}', explicacion, flags=re.IGNORECASE)
    nueva = re.sub(r'\b' + relativa, f'el {en_palabras}', nueva, flags=re.IGNORECASE)
    if f"{fecha.day} de {prompts.MESES_ES[fecha.month - 1]}" not in nueva:
        nueva = f"{nueva.rstrip()} Fecha: {en_palabras}."
    return nueva


def _es_cambio(correo):
    return clasificador.anuncia_cambio(
        correo.get("asunto", ""),
        correo.get("cuerpo") or correo.get("fragmento") or "",
        correo.get("cuerpo_hilo_anterior", ""),
    )


def _accion_actualizar(correo, eventos_por_cuenta):
    """Fila 🔵 para CU3: el correo cambia un evento que ya está en el calendario.

    El evento se busca entre TODOS los próximos (el correo suele decir la
    fecha nueva, no la del evento). Se ofrece el botón cuando cambia la hora,
    la fecha o el LUGAR; si solo anuncia documentos adjuntos, se informa,
    porque no hay ningún campo del evento que corregir.
    """
    evento = analisis.buscar_evento_relacionado(correo, eventos_por_cuenta)
    if not evento:
        return None

    texto_correo = (f"{correo.get('asunto', '')}\n"
                    f"{correo.get('cuerpo') or correo.get('fragmento') or ''}")
    horas = analisis.horas_en_texto(texto_correo)
    fechas = [f for f in analisis.fechas_candidatas_correo(correo) if not analisis.fecha_ya_paso(f)]
    nueva_fecha = fechas[0] if fechas else evento["fecha"]
    nueva_hora = horas[0] if horas else None
    # RF8 y CU3 — el lugar también se puede aplicar al calendario, no solo
    # informar. Se compara normalizado para no proponer un cambio cuando el
    # correo repite el sitio que el evento ya tenía.
    nuevo_lugar = analisis.lugar_en_texto(texto_correo)
    lugar_actual = evento.get("lugar", "") or ""
    cambia_lugar = bool(nuevo_lugar) and (
        clasificador._normalizar(nuevo_lugar) != clasificador._normalizar(lugar_actual))

    cambia_hora = nueva_hora is not None and abs(nueva_hora - evento["inicio_h"]) > 1e-6
    cambia_fecha = nueva_fecha != evento["fecha"]

    # La hora ya termina en punto ("8:00 a.m."): no se añade otro.
    explicacion = (f"[{memoria_local.etiqueta_cuenta(evento['alias'])}] Este correo cambia "
                   f"'{evento['titulo']}', que está el {analisis.fecha_en_palabras(evento['fecha'])} "
                   f"a las {_hora_float_a_texto(evento['inicio_h'])}")
    explicacion = explicacion.rstrip(".")
    accion = {
        "tipo": "actualizar",
        "id": correo["id"],
        "cuenta": correo["cuenta"],
        "cuenta_evento": evento["alias"],
        "id_evento": evento["id"],
        "evento_existente": evento["titulo"],
        "titulo": correo.get("asunto", "")[:80],
        "remitente": correo.get("remitente", "").split("<")[0].strip(),
        "duracion_horas": max(evento["fin_h"] - evento["inicio_h"], 0.25),
        "lugar_actual": lugar_actual,
        "analisis": correo.get("analisis", ""),
    }

    # El estado ACTUAL del evento viaja en la fila: hace falta para poder
    # aplicar unos campos y dejar los otros como están.
    accion["fecha_actual_iso"] = evento["fecha"].isoformat()
    accion["hora_actual_texto"] = _hora_float_a_texto(evento["inicio_h"])

    if cambia_hora or cambia_fecha or cambia_lugar:
        # Lo que NO cambia se manda como None y el evento lo conserva.
        if cambia_hora or cambia_fecha:
            accion.update({
                "fecha_iso": nueva_fecha.isoformat(),
                "fecha_texto": analisis.fecha_en_palabras(nueva_fecha),
                "hora_texto": _hora_float_a_texto(nueva_hora if nueva_hora is not None
                                                  else evento["inicio_h"]),
                "aviso_horario": memoria_local.aviso_fuera_de_horario(
                    nueva_fecha, nueva_hora if nueva_hora is not None else evento["inicio_h"]),
            })
        if cambia_lugar:
            accion["lugar_nuevo"] = nuevo_lugar

        # Campo a campo, con el antes y el después, en vez de una frase
        # corrida. Así se ve de un vistazo si el sistema se equivocó en UNO
        # de los campos, y la interfaz puede dejar aplicar solo los correctos:
        # antes era todo o nada y había que fiarse de la detección entera.
        accion["cambios"] = []
        if cambia_fecha:
            accion["cambios"].append({
                "campo": "fecha", "etiqueta": "Fecha",
                "antes": analisis.fecha_en_palabras(evento["fecha"]),
                "despues": analisis.fecha_en_palabras(nueva_fecha)})
        if cambia_hora:
            accion["cambios"].append({
                "campo": "hora", "etiqueta": "Hora",
                "antes": _hora_float_a_texto(evento["inicio_h"]),
                "despues": _hora_float_a_texto(nueva_hora)})
        if cambia_lugar:
            accion["cambios"].append({
                "campo": "lugar", "etiqueta": "Lugar",
                "antes": lugar_actual or "sin lugar",
                "despues": nuevo_lugar})

        accion["explicacion"] = (
            f"{explicacion}. Esto es lo que entendí de este correo; "
            f"desmarca lo que no corresponda antes de aplicarlo:")
    else:
        # Documentos adjuntos o un recordatorio sin datos nuevos: no hay
        # ningún campo del evento que corregir.
        accion["explicacion"] = (f"{explicacion}. Anuncia un cambio que no afecta a la fecha, "
                                 f"la hora ni el lugar (documentos o detalles): "
                                 f"revísalo en el correo.")
        accion["solo_aviso"] = True
    return accion


def actualizar_evento_desde_correo(cuenta_evento, id_evento, fecha_iso, hora_texto,
                                   duracion_horas=1.0, lugar=None):
    """Aplica a un evento existente el cambio que anuncia un correo (CU3).

    Puede mover la fecha y la hora, cambiar el lugar, o las dos cosas: lo que
    no llega se manda como None y el evento lo conserva. Un correo que solo
    cambia el salón ya no obliga a tocar el horario.

    Lo dispara el botón "Actualizar", siempre tras el diálogo de confirmación.
    """
    from datetime import date, datetime, timedelta

    inicio = None
    if fecha_iso:
        try:
            fecha = date.fromisoformat(fecha_iso)
        except (TypeError, ValueError):
            return {"ok": False, "mensaje": "No pude resolver la nueva fecha."}

        inicio_iso, _ = calcons.resolver_inicio_fin("", hora_texto, fecha=fecha)
        if not inicio_iso:
            return {"ok": False, "mensaje": "No pude resolver la nueva hora."}
        inicio = datetime.fromisoformat(inicio_iso)

    if inicio is None and not lugar:
        return {"ok": False, "mensaje": "No hay ningún cambio que aplicar al evento."}

    fin = inicio + timedelta(hours=duracion_horas or 1.0) if inicio else None
    link = gc.editar_evento_calendar(
        cuenta_evento, id_evento,
        fecha_inicio_iso=inicio.isoformat() if inicio else None,
        fecha_fin_iso=fin.isoformat() if fin else None,
        lugar=lugar)
    if link:
        partes = []
        if inicio:
            partes.append(f"ahora es el {prompts.formatear_fecha_legible(inicio.isoformat())}")
        if lugar:
            partes.append(f'el lugar es "{lugar}"')
        return {"ok": True, "mensaje": "Actualizado: " + " y ".join(partes) + "."}
    return {"ok": False, "mensaje": "No se pudo actualizar el evento."}


def _compromiso_del_correo(correo, texto_analisis=""):
    """(fecha, hora de inicio) del compromiso que menciona un correo, o (None, None)."""
    fecha = analisis.fecha_referida_correo(correo, texto_analisis)
    horas = (analisis._resolver_horas_texto(texto_analisis)
             or analisis.horas_en_texto(
                 f"{correo.get('asunto', '')}\n{correo.get('cuerpo') or correo.get('fragmento') or ''}"))
    return fecha, (horas[0] if horas else None)


def _eventos_que_cruzan(fecha, hora_inicio, eventos_por_cuenta, duracion_horas=None):
    """Eventos del calendario que se solapan con ese día y hora. Aritmética, no modelo (CU4)."""
    if fecha is None or hora_inicio is None:
        return []
    fin = hora_inicio + (duracion_horas or memoria_local.duracion_por_defecto_horas())
    return [ev for ev in analisis._eventos_en_fecha(fecha, eventos_por_cuenta or {})
            if analisis._solapan(hora_inicio, fin, ev["inicio_h"], ev["fin_h"])]


def _accion_pasada(correo, fecha, hora):
    """Fila 🕘 informativa: el compromiso era HOY pero ya pasó (N1).

    Solo para hoy: lo de días anteriores no se muestra, sería ruido.
    """
    import datetime as _dt
    if fecha != _dt.datetime.now(ZONA_BOGOTA).date():
        return None
    titulo = analisis.titulo_sugerido_evento(correo)
    detalle = f" a las {_hora_float_a_texto(hora)}" if hora is not None else ""
    return {
        "tipo": "pasado",
        "id": correo["id"],
        "cuenta": correo["cuenta"],
        "titulo": correo.get("asunto", "")[:80],
        "remitente": correo.get("remitente", "").split("<")[0].strip(),
        "explicacion": (f"[{correo['cuenta']}] '{titulo}' era hoy{detalle}: "
                        f"ya pasó, no tiene sentido agendarlo."),
        "analisis": "",
    }


def _accion_incompleta(correo, clasif, fecha, horas):
    """Fila 🟡 para el flujo alternativo A3 del CU2: falta un dato para agendar.

    El correo propone un encuentro, pero no dice el día, la hora, o ninguno
    de los dos. En vez de callar (que era lo que hacía antes), se enseña lo
    que sí se sabe y se pide lo que falta, con un campo para escribirlo.

    Se exige que el correo pida respuesta o nombre a la usuaria: 'posible
    evento' salta también en avisos generales, y sin ese filtro el bloque del
    calendario se llenaría de filas que no llevan a ninguna parte.
    """
    senales = set((clasif or {}).get("senales") or [])
    if not ({"requiere_respuesta", "remitente_importante", "urgente"} & senales):
        return None

    falta = []
    if fecha is None:
        falta.append("el día")
    if not horas:
        falta.append("la hora")
    if not falta:
        return None

    titulo = analisis.titulo_sugerido_evento(correo)
    sabido = []
    if fecha is not None:
        sabido.append(f"sería el {analisis.fecha_en_palabras(fecha)}")
    if horas:
        sabido.append(f"sería a las {_hora_float_a_texto(horas[0])}")
    detalle = f" ({' y '.join(sabido)})" if sabido else ""

    return {
        "tipo": "incompleto",
        "origen": "reglas",
        "id": correo["id"],
        "cuenta": correo["cuenta"],
        "cuenta_destino": correo["cuenta"],
        "titulo": correo.get("asunto", "")[:80],
        "titulo_evento": titulo,
        "remitente": correo.get("remitente", "").split("<")[0].strip(),
        "remitente_email": correo.get("remitente_email", ""),
        "fecha_iso": fecha.isoformat() if fecha is not None else "",
        "hora_texto": _hora_float_a_texto(horas[0]) if horas else "",
        "falta": falta,
        "explicacion": (f"[{correo['cuenta']}] Parece proponer un encuentro{detalle}, "
                        f"pero no dice {' ni '.join(falta)}. "
                        f"Complétalo aquí y lo agendo, o pregúntaselo respondiendo el correo."),
        "analisis": "",
    }


def _espacio_libre(fecha, hora, eventos_por_cuenta):
    """(fecha, hora, texto) del primer hueco libre tras un cruce, o (None, None, "")."""
    hueco = calcons.buscar_espacio_libre(fecha, hora, eventos_por_cuenta)
    if not hueco:
        return None, None, ""
    f, h = hueco
    fin = h + memoria_local.duracion_por_defecto_horas()
    # Las horas ya terminan en punto ("9:00 a.m."): no se añade otro.
    return f, h, (f"Tu siguiente espacio libre: {analisis.fecha_en_palabras(f)}, de "
                  f"{_hora_float_a_texto(h)} a {_hora_float_a_texto(fin)}")


def _texto_cruce(evento):
    return (f"Se cruza con '{evento['titulo']}' de {_hora_float_a_texto(evento['inicio_h'])} "
            f"a {_hora_float_a_texto(evento['fin_h'])} en "
            f"[{memoria_local.etiqueta_cuenta(evento['alias'])}].")


def _es_cancelacion(correo):
    """Mira el correo y el resumen, NUNCA el análisis: la plantilla de
    conflicto dice "¿Cancelo '...'?" y daría falsos positivos."""
    return clasificador.anuncia_cancelacion(
        correo.get("asunto", ""),
        correo.get("cuerpo") or correo.get("fragmento") or "",
        correo.get("resumen", ""),
    )


def cancelar_evento_desde_correo(cuenta_evento, id_evento, titulo_evento):
    """Elimina del calendario el evento que un correo canceló.

    Lo dispara el botón "Eliminar del calendario", SIEMPRE tras el diálogo de
    confirmación de la interfaz.
    """
    if gc.eliminar_evento_calendar(cuenta_evento, id_evento):
        return {"ok": True, "mensaje": f"Eliminado '{titulo_evento}' de [{cuenta_evento}]."}
    return {"ok": False, "mensaje": "No se pudo eliminar el evento del calendario."}


def _explicacion_calendario(texto_analisis):
    """Saca del análisis la explicación, sin la pregunta final.

    La pregunta ('¿Te gustaría que lo agende...?') se sustituye por botones en
    la interfaz, así que repetirla en el texto sobraría.
    """
    lineas = []
    for linea in (texto_analisis or "").split("\n"):
        limpia = linea.strip()
        if not limpia:
            continue
        if limpia.startswith("¿") or limpia.lower().startswith("te recomiendo"):
            continue
        lineas.append(limpia.lstrip("- ").strip())
    return " ".join(lineas)


def resumen_por_categoria(grupo, progreso=None):
    """Una frase que sintetiza toda una categoría.

    Se construye a partir de los RESÚMENES ya calculados, no de los correos
    completos: cuesta una llamada al modelo por categoría en vez de una por
    correo. Para una bandeja de 480 correos eso son 7 u 8 llamadas, no 480.
    """
    correos = grupo.get("correos", [])
    if len(correos) < 2:
        return ""

    # Si la categoría tiene exactamente los mismos correos que la última
    # vez, la síntesis no puede haber cambiado: se reutiliza.
    ids = [c.get("id", "") for c, _ in correos]
    cacheado = memoria_local.resumen_categoria_en_cache(grupo["categoria"], ids)
    if cacheado is not None:
        return cacheado

    # Como mucho 25 resúmenes: más no cambia la síntesis y sí el coste.
    lineas = []
    for correo, _clasif in correos[:25]:
        remitente = correo.get("remitente", "").split("<")[0].strip()
        resumen = (correo.get("resumen") or "")[:140]
        lineas.append(f"- {remitente}: {resumen}")

    peticion = (
        f"Estos son los resúmenes de {len(correos)} correos de la categoría "
        f"'{grupo['etiqueta']}':\n\n" + "\n".join(lineas) +
        "\n\nEscribe UNA sola frase, de máximo 30 palabras, que resuma lo que hay "
        "en esta categoría. Menciona los remitentes o asuntos más repetidos. "
        "No inventes nada que no esté arriba. No uses viñetas. Responde en español."
    )

    try:
        texto = prompts.resumir_categoria(peticion)
    except Exception as e:
        print(f"⚠️ No se pudo resumir la categoría '{grupo['etiqueta']}': {e}")
        return ""

    texto = (texto or "").strip().split("\n")[0].strip()
    # Una síntesis que cita una fecha que no está en ningún resumen es
    # invención: se descarta antes que mostrarla.
    if len(texto) < 15 or len(texto) > 300:
        texto = ""
    # Se guarda incluso si quedó vacía: así tampoco se reintenta en cada
    # revisión una categoría cuya síntesis el modelo no supo escribir.
    return memoria_local.guardar_resumen_categoria(grupo["categoria"], ids, texto)


def sugerir_respuesta(cuenta, msg_id, instruccion_extra=""):
    """Redacta el borrador de respuesta a un correo concreto.

    Lo dispara el botón "Sugerir respuesta" de la interfaz. No envía nada ni
    crea nada en Gmail: solo devuelve el texto para que la usuaria lo lea.
    """
    correos = gc.leer_correos_por_id(cuenta, [msg_id])
    if not correos:
        return {"ok": False, "texto": "No pude recuperar este correo."}
    correo = correos[0]

    # El modelo no conoce el calendario al redactar, y escribía "ese día tengo
    # un espacio libre" aunque hubiera un evento a esa hora. Se le pasa la
    # disponibilidad REAL, y la interfaz avisa del cruce antes de enviar.
    agenda, aviso_agenda = _agenda_para_responder(correo, cuenta)

    try:
        texto = prompts.redactar_respuesta(correo, instruccion_extra, agenda=agenda)
    except Exception as e:
        return {"ok": False, "texto": f"No se pudo redactar la respuesta: {e}"}

    texto = analisis.limpiar_borrador(texto)
    if not texto:
        return {"ok": False, "texto": "El modelo no devolvió una respuesta utilizable."}

    return {"ok": True, "texto": texto,
            "destinatario": correo.get("remitente", ""),
            "asunto": correo.get("asunto", ""),
            "aviso_agenda": aviso_agenda}


def _agenda_para_responder(correo, cuenta):
    """(frase para el modelo, aviso para la interfaz) sobre la disponibilidad real."""
    fecha, hora = _compromiso_del_correo(correo)

    if fecha is None or hora is None:
        # Si el correo propone verse pero no se pudo resolver CUÁNDO, callar
        # es lo peor que se puede hacer: sin dato de agenda el modelo contesta
        # "me comprometo a reservar el espacio" sin que nadie haya mirado el
        # calendario. Caso real: "para el siguiente viernes a las 7pm", con
        # ese viernes ocupado. Mejor no comprometerse que prometer en falso.
        texto = (f"{correo.get('asunto', '')}\n"
                 f"{correo.get('cuerpo') or correo.get('fragmento') or ''}")
        parece_cita = bool(analisis.horas_en_texto(texto)
                           or analisis.fechas_candidatas_correo(correo))
        if not parece_cita:
            return "", ""
        return ("no se ha podido comprobar. NO afirmes que estoy libre ni te "
                "comprometas a asistir: responde que lo revisaré y confirmaré.",
                "⚠️ No pude identificar con certeza la fecha y la hora que propone este "
                "correo, así que NO comprobé tu calendario. El borrador evita "
                "comprometerse a propósito: revísalo antes de enviarlo.")

    eventos = _ultimos_eventos
    if not eventos:
        try:
            eventos = {cuenta: gc.leer_eventos_cuenta(cuenta, cantidad=50, dias=30)}
        except Exception as e:
            print(f"⚠️ No pude consultar el calendario para el borrador: {e}")
            return "", ""

    cuando = f"el {analisis.fecha_en_palabras(fecha)} a las {_hora_float_a_texto(hora)}"
    cruces = _eventos_que_cruzan(fecha, hora, eventos)
    if not cruces:
        return f"{cuando} tengo la agenda libre.", ""

    ev = cruces[0]
    libre_fecha, libre_hora, libre_texto = _espacio_libre(fecha, hora, eventos)
    # Las horas ya terminan en punto ("9:00 p.m."): no se añade otro.
    agenda = (f"{cuando} NO estoy libre: ya tengo '{ev['titulo']}' "
              f"de {_hora_float_a_texto(ev['inicio_h'])} a {_hora_float_a_texto(ev['fin_h'])}")
    if libre_fecha:
        agenda += (f" Mi siguiente espacio libre es el {analisis.fecha_en_palabras(libre_fecha)} "
                   f"a las {_hora_float_a_texto(libre_hora)}: propónselo como alternativa.")
    aviso = (f"⚠️ A esa hora ya tienes '{ev['titulo']}' "
             f"({_hora_float_a_texto(ev['inicio_h'])} a {_hora_float_a_texto(ev['fin_h'])}) en "
             f"[{memoria_local.etiqueta_cuenta(ev['alias'])}]. Revisa el borrador antes de enviarlo.")
    if libre_texto:
        aviso += f" {libre_texto}"
    return agenda, aviso


def _registrar_pendiente_si_propone_otra_hora(correo, cuenta, texto_enviado, como_borrador):
    """Si la respuesta propone el espacio libre por el cruce, se anota como pendiente.

    No se agenda nada: la otra persona todavía tiene que aceptar. En las
    siguientes revisiones Stefany lo recuerda ("pendiente agendar…").
    """
    fecha, hora = _compromiso_del_correo(correo)
    eventos = _ultimos_eventos or {}
    if not _eventos_que_cruzan(fecha, hora, eventos):
        return None

    libre_fecha, libre_hora, _ = _espacio_libre(fecha, hora, eventos)
    if libre_fecha is None:
        return None

    # Solo si la respuesta realmente menciona esa hora.
    if libre_hora not in analisis.horas_en_texto(texto_enviado or ""):
        return None

    return memoria_local.guardar_pendiente({
        "id": f"{correo.get('id')}-{libre_fecha.isoformat()}-{libre_hora}",
        "id_correo": correo.get("id"),
        "cuenta": cuenta,
        "persona": correo.get("remitente", "").split("<")[0].strip().strip('"'),
        "remitente_email": correo.get("remitente_email", ""),
        "titulo": analisis.titulo_sugerido_evento(correo),
        "fecha_iso": libre_fecha.isoformat(),
        "hora_texto": _hora_float_a_texto(libre_hora),
        "origen": "borrador guardado" if como_borrador else "respuesta enviada",
    })


def acciones_pendientes(solo_propuestas=True):
    """Filas 🕓 de lo que quedó pendiente de agendar.

    solo_propuestas=True (el bloque del calendario): únicamente los espacios
    que la usuaria propuso por correo y siguen sin respuesta.
    solo_propuestas=False (botón "Pendientes"): también las sugerencias de
    revisiones anteriores que no atendió.
    """
    acciones = []
    for p in memoria_local.cargar_pendientes():
        if solo_propuestas and p.get("clase") == "sugerencia":
            continue
        from datetime import date
        try:
            fecha = date.fromisoformat(p["fecha_iso"])
        except (KeyError, ValueError):
            continue
        persona = f" con {p['persona']}" if p.get("persona") else ""
        acciones.append({
            "tipo": "pendiente",
            "id": p.get("id_correo", ""),
            "id_pendiente": p.get("id"),
            "cuenta": p.get("cuenta", ""),
            "cuenta_destino": p.get("cuenta", ""),
            "titulo": p.get("titulo", "")[:80],
            "titulo_evento": p.get("titulo", ""),
            "remitente": p.get("persona", ""),
            "remitente_email": p.get("remitente_email", ""),
            "clase": p.get("clase", ""),
            "recurrencia": p.get("recurrencia"),
            "fecha_iso": p["fecha_iso"],
            "fecha_texto": f"{fecha.day} de {prompts.MESES_ES[fecha.month - 1]}",
            "hora_texto": p.get("hora_texto", ""),
            # La hora ya termina en punto ("10:00 a.m."): no se añade otro.
            "explicacion": (
                (f"[{p.get('cuenta', '')}] Las repeticiones de '{p.get('titulo', '')}' "
                 f"({(p.get('recurrencia') or {}).get('texto', '')}) terminan el "
                 f"{analisis.fecha_en_palabras(fecha)}. ¿Renovar otro mes?")
                if p.get("clase") == "renovacion" else
                (f"[{p.get('cuenta', '')}] '{p.get('titulo', '')}' el "
                 f"{analisis.fecha_en_palabras(fecha)} a las {p.get('hora_texto', '')} "
                 f"Sugerido en una revisión anterior y sin agendar.")
                if p.get("clase") == "sugerencia" else
                (f"[{p.get('cuenta', '')}] Propusiste{persona} el "
                 f"{analisis.fecha_en_palabras(fecha)} a las {p.get('hora_texto', '')} "
                 f"({p.get('origen', '')}). Sigue sin agendar.")),
            "aviso_horario": "",
            "analisis": "",
            "origen": "pendiente",
        })
    return acciones


def renovar_pendiente(id_pendiente):
    """Vuelve a agendar otro mes un evento que se repetía."""
    from datetime import date
    pendiente = next((p for p in memoria_local.cargar_pendientes(incluir_caducados=True)
                      if p.get("id") == id_pendiente), None)
    if not pendiente or not pendiente.get("recurrencia"):
        return {"ok": False, "mensaje": "No encontré los datos de ese evento."}

    recurrencia = dict(pendiente["recurrencia"])
    horas = analisis.horas_en_texto(pendiente.get("hora_texto", ""))
    try:
        desde = date.fromisoformat(recurrencia["hasta"])
    except (KeyError, ValueError):
        return {"ok": False, "mensaje": "No pude resolver desde cuándo renovar."}

    primera = analisis.primera_fecha_recurrente(recurrencia, horas[0] if horas else None,
                                                desde=desde)
    if primera is None:
        return {"ok": False, "mensaje": "No pude calcular la siguiente fecha."}
    recurrencia["hasta"] = analisis.sumar_un_mes(primera).isoformat()

    memoria_local.eliminar_pendiente(id_pendiente)
    return agendar_desde_correo(
        pendiente.get("cuenta", ""), pendiente.get("id_correo", ""), "",
        fecha_iso=primera.isoformat(), hora_texto=pendiente.get("hora_texto", ""),
        titulo=pendiente.get("titulo", "Evento"),
        remitente_email=pendiente.get("remitente_email", ""), recurrencia=recurrencia)


def contar_pendientes():
    return len(memoria_local.cargar_pendientes())


def descartar_pendiente(id_pendiente):
    memoria_local.eliminar_pendiente(id_pendiente)
    return {"ok": True, "mensaje": "Pendiente descartado."}


def enviar_respuesta(cuenta, msg_id, texto, solo_borrador=True):
    """Envía la respuesta o la deja como borrador en Gmail.

    solo_borrador=True  → crea un borrador dentro del hilo del correo.
    solo_borrador=False → ENVÍA el correo de verdad.

    El envío real solo ocurre cuando la usuaria pulsa el botón y confirma en
    el diálogo. Sigue sin existir ningún camino por el que una palabra escrita
    en el chat dispare un envío: eso es lo que se desactivó a propósito.
    """
    correos = gc.leer_correos_por_id(cuenta, [msg_id])
    if not correos:
        return {"ok": False, "mensaje": "No pude recuperar el correo original."}
    correo = correos[0]

    destinatario = gc._extraer_correo(correo.get("remitente", ""))
    if not destinatario:
        return {"ok": False, "mensaje": "No pude determinar la dirección del destinatario."}

    asunto = correo.get("asunto", "")
    if not asunto.lower().startswith("re:"):
        asunto = f"Re: {asunto}"

    # El hilo y el Message-ID hacen que la respuesta quede DENTRO de la
    # conversación. Sin ellos el correo llegaba suelto, con asunto "Re: ..."
    # pero fuera del hilo.
    id_hilo = correo.get("hilo")
    id_mensaje = correo.get("message_id") or None

    if solo_borrador:
        id_borrador = gc.crear_borrador(cuenta, destinatario, asunto, texto,
                                        id_hilo=id_hilo,
                                        id_mensaje_respondido=id_mensaje)
        if not id_borrador:
            return {"ok": False, "mensaje": "No se pudo crear el borrador."}
        mensaje = f"Borrador guardado en Gmail para {destinatario}."
    else:
        if not gc._enviar_correo_gmail_real(cuenta, destinatario, asunto, texto,
                                            id_hilo=id_hilo,
                                            id_mensaje_respondido=id_mensaje):
            return {"ok": False, "mensaje": "No se pudo enviar el correo."}
        mensaje = f"Correo enviado a {destinatario}."

    # Si la respuesta propone otro horario por un cruce, queda anotado como
    # pendiente hasta que la otra persona conteste.
    pendiente = _registrar_pendiente_si_propone_otra_hora(correo, cuenta, texto, solo_borrador)
    if pendiente:
        mensaje += (f" Anotado como pendiente: agendar '{pendiente['titulo']}' el "
                    f"{pendiente['fecha_iso']} a las {pendiente['hora_texto']} cuando confirmen.")
    return {"ok": True, "mensaje": mensaje, "pendiente": bool(pendiente)}


def _fecha_escrita_a_mano(texto):
    """Convierte lo que la usuaria escribe en un campo de fecha en un date.

    Acepta los mismos formatos que un correo ("25 de septiembre", "25/09",
    "mañana", "el viernes"), contados desde hoy. Devuelve None si no hay nada
    reconocible, para poder decírselo en vez de agendar en una fecha inventada.
    """
    if not (texto or "").strip():
        return None
    hoy = datetime.now(ZONA_BOGOTA).date()
    return (analisis._primera_fecha_explicita(texto, hoy)
            or analisis._fecha_relativa(texto, hoy)
            or analisis._fecha_dia_semana(texto, hoy))


def _hora_normalizada(texto):
    """Pasa '3pm', '15:00' o '3 de la tarde' al formato '3:00 p.m.'.

    calendario_consulta.resolver_inicio_fin() solo entiende ese formato: sin
    esto, una hora escrita a mano se ignoraba en silencio y el evento se
    creaba a la hora por defecto del perfil.
    """
    texto = (texto or "").strip()
    if not texto:
        return ""
    if re.search(r'\d{1,2}:\d{2}\s*[ap]\.\s*m\.', texto, re.IGNORECASE):
        return texto
    horas = analisis.horas_en_texto(texto)
    return _hora_float_a_texto(horas[0]) if horas else ""


def agendar_desde_correo(cuenta, msg_id, texto_analisis, fecha_iso=None, hora_texto=None,
                         titulo=None, id_pendiente=None, remitente_email=None,
                         recurrencia=None, fecha_texto=None, lugar=None):
    """Crea el evento que el análisis detectó. Lo dispara el botón "Agendar".

    Antes, la única forma de agendar era responder "sí" en el chat, y eso
    dependía de que el estado de sesión siguiera vivo y de que la detección de
    confirmación acertara. Un botón junto al correo elimina las dos
    dependencias.
    """
    # Del análisis solo se toma la hora. La fecha llega resuelta (fecha_iso) y
    # la cuenta es la del correo.
    datos = analisis.extraer_datos_evento(texto_analisis or "") or {}

    # fecha_iso llega ya resuelta desde acciones_calendario() (con "hoy"
    # contado desde el envío del correo). Buscarla otra vez en el análisis
    # fallaba cada vez que el modelo escribía "hoy" en lugar de una fecha.
    fecha = None
    if fecha_iso:
        from datetime import date
        fecha = date.fromisoformat(fecha_iso)
    elif fecha_texto:
        # CU2 · A3 — la fecha la escribió la usuaria en la fila porque el
        # correo no la decía.
        fecha = _fecha_escrita_a_mano(fecha_texto)
        if fecha is None:
            return {"ok": False,
                    "mensaje": ("No entendí esa fecha. Escríbela como "
                                "\"25 de septiembre\", \"25/09\" o \"el viernes\".")}
    if fecha is not None and analisis.fecha_ya_paso(fecha):
        return {"ok": False, "mensaje": "La fecha de este compromiso ya pasó."}

    # El evento va al calendario de la cuenta a la que LLEGÓ el correo.
    cuenta_destino = cuenta

    # Solo se consulta Gmail si falta algo: normalmente la fila ya trae el
    # nombre del evento y el correo del remitente.
    titulo = (titulo or "").strip()
    remitente = (remitente_email or "").strip()
    lugar = (lugar or "").strip()
    if not titulo or not remitente or not lugar:
        correos = gc.leer_correos_por_id(cuenta, [msg_id]) or []
        if correos:
            titulo = titulo or (analisis.titulo_sugerido_evento(correos[0]) or "")
            remitente = remitente or correos[0].get("remitente_email", "")
            # RF8 — el evento nace con el sitio que menciona el correo, para
            # que después haya algo que actualizar si lo cambian.
            lugar = lugar or analisis.lugar_en_texto(
                f"{correos[0].get('asunto', '')}\n{correos[0].get('cuerpo', '')}")
        titulo = titulo or datos.get("tema") or "Nuevo evento"

    # hora_texto llega resuelta en las filas creadas por reglas (no hay
    # análisis); si la escribió la usuaria, se normaliza antes.
    hora_final = _hora_normalizada(hora_texto) or hora_texto or datos.get("hora_texto", "")
    inicio_iso, fin_iso = calcons.resolver_inicio_fin(
        datos.get("fecha_texto", ""), hora_final, fecha=fecha)
    if not inicio_iso:
        return {"ok": False, "mensaje": "No pude resolver la fecha del evento."}

    # El remitente queda en la descripción: cuando esa misma persona escriba
    # cancelando, el evento se reconoce sin depender del título.
    descripcion = "Creado por Stefany desde un correo"
    descripcion += f" de {remitente}." if remitente else "."

    # Evento que se repite: se agenda un mes y se deja un recordatorio
    # para preguntar si se renueva.
    reglas = None
    if recurrencia:
        from datetime import date as _date
        hasta = _date.fromisoformat(recurrencia["hasta"])
        reglas = analisis.regla_recurrencia(recurrencia, hasta)
        descripcion += f" Se repite {recurrencia.get('texto', '')} hasta el {hasta.isoformat()}."

    creado = gc.crear_evento_calendar(cuenta_destino, titulo, inicio_iso, fin_iso,
                                      descripcion=descripcion, recurrencia=reglas,
                                      lugar=lugar)
    if creado:
        if id_pendiente:
            memoria_local.eliminar_pendiente(id_pendiente)   # ya dejó de estar pendiente
        legible = prompts.formatear_fecha_legible(inicio_iso)
        if recurrencia:
            memoria_local.guardar_pendiente({
                "id": f"renovar-{cuenta_destino}-{titulo}-{recurrencia['hasta']}",
                "clase": "renovacion",
                "id_correo": msg_id,
                "cuenta": cuenta_destino,
                "persona": "",
                "remitente_email": remitente,
                "titulo": titulo,
                "fecha_iso": recurrencia["hasta"],
                "hora_texto": hora_texto or "",
                "recurrencia": recurrencia,
                "origen": "evento que se repite",
            })
            return {"ok": True,
                    "mensaje": (f"Agendado: '{titulo}' {recurrencia.get('texto', '')} desde el "
                                f"{legible}, durante un mes. Te lo recordaré al terminar por si "
                                f"quieres renovarlo.")}
        donde = f" en {lugar}" if lugar else ""
        return {"ok": True,
                "mensaje": f"Agendado: '{titulo}' el {legible}{donde} en [{cuenta_destino}]."}
    return {"ok": False, "mensaje": "No se pudo crear el evento en el calendario."}


def sugerir_respuesta_placeholder():
    pass


def resumir_correo(cuenta, msg_id):
    """Genera el resumen de UN correo concreto, a petición del usuario.

    La revisión completa solo pasa por el modelo los correos que lo ameritan.
    Esto permite pedir el resumen de cualquier otro sin haber gastado tokens
    en los 480 de la bandeja.

    Devuelve {'resumen': str, 'analisis': str}.
    """
    cacheado = memoria_local.resumen_en_cache(cuenta, msg_id)
    if cacheado:
        correos_meta = gc.leer_correos_por_id(cuenta, [msg_id])
        if correos_meta:
            saneado, cambiado = analisis.sanear_resumen_guardado(cacheado, correos_meta[0])
            if cambiado:
                memoria_local.guardar_resumen(cuenta, msg_id, saneado)
            return saneado
        return cacheado

    correos = gc.leer_correos_por_id(cuenta, [msg_id])
    if not correos:
        return {"resumen": "No pude recuperar este correo.", "analisis": ""}
    correo = correos[0]

    contexto = _ultimo_contexto_calendario
    if contexto is None:
        eventos = {cuenta: gc.leer_eventos_cuenta(cuenta, cantidad=50, dias=30)}
        contexto = analisis.contexto_calendario_completo(eventos)
    else:
        eventos = _ultimos_eventos

    texto = prompts.analizar_correo(correo, contexto)
    texto = analisis._sanear_texto_modelo(texto, correo)
    texto = analisis.verificar_conflicto_horario(texto, eventos or {})

    if "📊 Análisis:" in texto:
        resumen, analisis_texto = texto.split("📊 Análisis:", 1)
        resultado = {"resumen": resumen.strip(), "analisis": analisis_texto.strip()}
    else:
        resultado = {"resumen": texto.strip(), "analisis": ""}

    memoria_local.guardar_resumen(cuenta, msg_id, resultado)
    return resultado


def _contar_imagenes_utiles(correo):
    """(adjuntas, remotas) que vale la pena leer con OCR.

    No se cuentan logos, firmas ni píxeles de rastreo: si no se filtran, casi
    todo correo institucional parecería tener imágenes con contenido.
    """
    adjuntas = [i for i in (correo.get("imagenes_adjuntas") or [])
                if not isinstance(i, dict)
                or not ocr.parece_decorativa(i.get("nombre", ""), i.get("tamano", 0))]
    remotas = [u for u in (correo.get("imagenes_remotas") or [])
               if not isinstance(u, str) or not ocr.parece_decorativa(url=u)]
    return len(adjuntas), len(remotas)


def _vista_correos(pares):
    """Pasa [(correo, clasificacion), ...] a diccionarios ligeros para la UI.

    El diccionario interno de un correo lleva el cuerpo completo, y los cuerpos
    reales contienen claves dinámicas, códigos estudiantiles y datos de
    terceros. Eso NO debe terminar en el JSON del historial.
    """
    salida = []
    for correo, clasif in pares:
        nombre = correo.get("remitente", "").split("<")[0].strip().strip('"')
        salida.append({
            "id":        correo.get("id"),
            "cuenta":    correo.get("cuenta"),
            "asunto":    correo.get("asunto", "(sin asunto)"),
            "remitente": nombre or correo.get("remitente_email", ""),
            # La dirección real, aparte del nombre para mostrar. Antes se
            # perdía aquí: la tarjeta solo enseñaba "Bienestar Universitario"
            # y no había forma de saber desde qué dirección llegó. Es
            # metadato, no cuerpo, así que no rompe la regla 3.
            "remitente_email": correo.get("remitente_email", ""),
            "fecha":     correo.get("fecha_legible", ""),
            "resumen":   correo.get("resumen", ""),
            "analisis":  correo.get("analisis", ""),
            "origen":    correo.get("origen_resumen", ""),
            "hilo_n":    correo.get("mensajes_en_hilo", 1),
            "vacio":     correo.get("cuerpo_vacio", False),
            "n_adjuntas": _contar_imagenes_utiles(correo)[0],
            "n_remotas": _contar_imagenes_utiles(correo)[1],
            "categoria": clasificador.CATEGORIAS[clasif["categoria"]]["etiqueta"],
            "senales":   [
                {"icono": clasificador.SENALES[s]["icono"],
                 "etiqueta": clasificador.SENALES[s]["etiqueta"]}
                for s in clasif["senales"]
            ],
            # Las claves crudas: la interfaz las necesita para saber qué
            # correo lleva el botón de respuesta.
            "claves_senal": list(clasif["senales"]),
        })
    return salida


def _vista_grupos(grupos):
    """Convierte los grupos en una estructura ligera y serializable.

    El diccionario interno de un correo lleva el cuerpo completo, y los cuerpos
    reales contienen claves dinámicas, códigos estudiantiles y datos de
    terceros. Eso NO debe terminar guardado en el JSON del historial de
    conversaciones: aquí se deja solo lo que la interfaz necesita pintar.
    """
    salida = []
    for grupo in grupos:
        correos = _vista_correos(grupo["correos"])
        salida.append({
            "categoria":  grupo["categoria"],
            "etiqueta":   grupo["etiqueta"],
            "icono":      grupo["icono"],
            "cantidad":   grupo["cantidad"],
            "desplegado": grupo["desplegado"],
            # La interfaz lo usa para decidir si, al abrir el acordeón, se
            # muestran todos los correos o por tandas.
            "importante": grupo.get("importante", False),
            "correos":    correos,
        })
    return salida


def _resultado_como_texto(pares, grupos, total, hallazgos, dia_texto=""):
    """Versión en texto plano del resultado.

    Se conserva para dos cosas: que el historial guardado siga siendo legible
    aunque cambie la interfaz, y que cualquier parte del sistema que espere un
    string siga funcionando.
    """
    estado = "" if dia_texto else " sin leer"
    lineas = [f"Tienes {total} correos{estado}{dia_texto} ({len(pares)} conversaciones)."]
    for grupo in grupos:
        lineas.append(f"\n{grupo['icono']} {grupo['etiqueta']} — {grupo['cantidad']}")
        for correo in grupo["correos"]:
            marcas = " ".join(s["icono"] for s in correo["senales"])
            lineas.append(f"  • {correo['asunto']} {marcas}".rstrip())
            if correo.get("resumen"):
                lineas.append(f"    {correo['resumen']}")
    if hallazgos:
        lineas.append("\n📊 Análisis:\n" + "\n\n".join(hallazgos))
    return "\n".join(lineas)


# ---------------------------------------------------------------
# Enrutador principal
# ---------------------------------------------------------------

def asistente_multicuenta(mensaje_usuario, historial=None, limite=None, progreso=None,
                          conversacion_id=None):
    """Punto de entrada del asistente.

    El estado (acción pendiente, datos, último tema) se carga de la
    conversación indicada y se guarda al terminar: antes era un dict global,
    así que un "sí" en una conversación podía disparar la acción pendiente de
    otra — o de una sesión anterior.
    """
    global estado_sesion
    estado_sesion = persistencia.cargar_estado_sesion(conversacion_id)
    try:
        return _asistente_multicuenta(mensaje_usuario, historial=historial,
                                      limite=limite, progreso=progreso)
    finally:
        persistencia.guardar_estado_sesion(conversacion_id, estado_sesion)


def _asistente_multicuenta(mensaje_usuario, historial=None, limite=None, progreso=None):
    global estado_sesion

    if es_instruccion_preferencia(mensaje_usuario):
        memoria_local.agregar_preferencia(mensaje_usuario)
        return (f'📌 Preferencia guardada: "{mensaje_usuario}"\n'
                f"La tendré en cuenta de ahora en adelante para mis recomendaciones.")

    accion_actual = estado_sesion['accion_pendiente']
    if accion_actual == 'esperando_seleccion_evento':
        return _resolver_seleccion_evento(mensaje_usuario)
    if accion_actual == 'esperando_titulo_evento':
        return _resolver_titulo_pendiente(mensaje_usuario)
    if accion_actual:
        # La negación va PRIMERO: si el mensaje contiene cualquier señal de
        # rechazo, no se ejecuta nada, aunque también traiga un 'claro'.
        if es_negacion(mensaje_usuario):
            tema_previo = estado_sesion.get('ultimo_tema')
            estado_sesion = {'accion_pendiente': None, 'datos_pendientes': {}, 'ultimo_tema': tema_previo,
                              'ultimo_evento_referenciado': estado_sesion.get('ultimo_evento_referenciado')}
            return "Entendido, no se realizó ninguna acción. ¿En qué más puedo ayudarte?"
        elif es_confirmacion(mensaje_usuario):
            return _resolver_confirmacion(mensaje_usuario)
    elif es_confirmacion(mensaje_usuario):
        # Un "sí" sin nada pendiente NO va al modelo: se inventaba la acción
        # ("La reunión está programada...") sin haber creado nada.
        return ("No tengo ninguna acción pendiente de confirmar, así que no hice nada. "
                "Si quieres agendar algo, dime qué, qué día y a qué hora.")

    texto_norm = _sin_acentos(mensaje_usuario.lower())
    palabras_correo = ['correo', 'correos', 'mail', 'bandeja', 'inbox', 'revisa', 'revisame']
    palabras_calendario = ['evento', 'eventos', 'calendario', 'agenda',
                            'reunion', 'reuniones', 'compromiso', 'compromisos',
                            'cita', 'pendiente', 'pendientes']

    menciona_correo = _contiene_palabra(texto_norm, palabras_correo)
    menciona_calendario = _contiene_palabra(texto_norm, palabras_calendario)

    # Va ANTES del enrutado por palabras: "redacta un correo de cancelación"
    # menciona "correo" y si no se iría a revisar la bandeja entera.
    if es_solicitud_correo_cancelacion(mensaje_usuario):
        return _resolver_solicitud_correo_cancelacion(mensaje_usuario)

    if menciona_calendario and es_solicitud_cancelar_directo(mensaje_usuario):
        return _resolver_solicitud_cancelar_directo(mensaje_usuario)

    if menciona_calendario and es_solicitud_editar_directo(mensaje_usuario):
        return _resolver_solicitud_editar_directo(mensaje_usuario)

    if menciona_calendario and es_solicitud_agendar_directo(mensaje_usuario):
        return _resolver_solicitud_agendar_directo(mensaje_usuario)

    if not menciona_calendario and not menciona_correo and _es_seguimiento_de_edicion(mensaje_usuario):
        evento = estado_sesion['ultimo_evento_referenciado']
        nuevo_titulo = _extraer_nuevo_titulo_edicion(mensaje_usuario)
        nuevas_horas = _extraer_nueva_hora_edicion(mensaje_usuario)
        return _preparar_confirmacion_edicion(evento, nuevo_titulo, nuevas_horas)

    if not menciona_correo and not menciona_calendario and estado_sesion.get('ultimo_tema') == 'calendario':
        if _extraer_fecha_de_pregunta(mensaje_usuario):
            menciona_calendario = True

    if menciona_calendario and not menciona_correo:
        fecha_pregunta = _extraer_fecha_de_pregunta(mensaje_usuario)
        if fecha_pregunta:
            cuenta_filtrada = detectar_cuenta_mencionada(mensaje_usuario)
            estado_sesion['ultimo_tema'] = 'calendario'
            return calcons.consultar_calendario_fecha(fecha_pregunta, cuenta_filtrada)
        estado_sesion['ultimo_tema'] = 'calendario'
        return ("¿Para qué día necesitas que revise o agende eso? Dime la fecha "
                "(por ejemplo \"el 20 de agosto\" o \"el viernes\") y lo hago.")

    if not (menciona_correo or menciona_calendario):
        estado_sesion['ultimo_tema'] = 'conversacion'
        return prompts.responder_conversacion(mensaje_usuario, historial=historial)

    estado_sesion['ultimo_tema'] = 'correos'
    return revisar_correos_y_calendario(mensaje_usuario, limite=limite, progreso=progreso)
