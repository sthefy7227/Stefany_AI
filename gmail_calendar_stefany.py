# -*- coding: utf-8 -*-
"""
gmail_calendar_stefany.py — Todas las llamadas a Gmail y Google Calendar,
hechas localmente desde la aplicación de escritorio. La conexión/autenticación de cuentas vive en
oauth_stefany.py; este módulo solo USA las cuentas ya conectadas
(cuentas_conectadas, construida con oauth_stefany.construir_cuentas_conectadas()).
"""

import re
import time
import random
import base64
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
import extraccion_stefany as extraccion

# Se importa de prompts_stefany en vez de redefinirla: estaba escrita en dos
# archivos y podían quedar desincronizadas.
from prompts_stefany import ZONA_LOCAL
ZONA_BOGOTA = ZONA_LOCAL


def _nombre_zona_horaria():
    """Nombre IANA de la zona del equipo, para la API de Calendar.

    Google acepta 'America/Bogota', 'Europe/Madrid', etc. Estaba fijo en
    'America/Bogota', así que un usuario en otro huso habría creado sus
    eventos con hora colombiana.
    """
    try:
        import tzlocal
        return str(tzlocal.get_localzone())
    except Exception:
        pass
    try:
        # Python 3.9+: el nombre viene en la propia zona si se conoce
        clave = getattr(datetime.now().astimezone().tzinfo, "key", None)
        if clave:
            return clave
    except Exception:
        pass
    # Sin nombre IANA, se manda el desfase: Google también lo acepta dentro
    # del propio dateTime ISO, así que el evento queda correcto igualmente.
    return None

# Diccionario global {alias: {'gmail': service, 'calendar': service}} --
# se llena al iniciar la app con oauth_stefany.construir_cuentas_conectadas()
# y se reasigna cada vez que se conecta/desconecta una cuenta.
cuentas_conectadas = {}


def actualizar_cuentas_conectadas(nuevas_cuentas):
    global cuentas_conectadas
    cuentas_conectadas = nuevas_cuentas


# ---------------------------------------------------------------
# Limpieza de correos (Reemplazo de Regex por Beautiful Soup)
# ---------------------------------------------------------------

def procesar_html_bs4(html_content):
    if not html_content:
        return ""

    soup = BeautifulSoup(html_content, 'html.parser')

    # Eliminar scripts, estilos y metadata que no aportan información al modelo
    for element in soup(["script", "style", "meta", "noscript"]):
        element.decompose()

    # Extraer texto limpio separando con espacios
    texto_limpio = soup.get_text(separator=' ', strip=True)
    
    # Limpiar espacios múltiples y saltos de línea excesivos
    texto_limpio = re.sub(r'\s+', ' ', texto_limpio).strip()

    # Detectar correos cuyo contenido principal sea una imagen
    palabras = texto_limpio.split()
    if len(palabras) < 20:
        imagenes = soup.find_all('img')
        tiene_imagen_relevante = False
        
        for img in imagenes:
            alt_text = img.get('alt', '').strip()
            # Si la imagen tiene un atributo 'alt' descriptivo, lo añadimos al texto
            if alt_text and len(alt_text.split()) > 2:
                texto_limpio += f" [Imagen: {alt_text}]"
                tiene_imagen_relevante = True
            else:
                # Heurística para descartar píxeles de rastreo invisibles (ej. 1x1)
                width = img.get('width', '100').replace('px', '').replace('%', '')
                height = img.get('height', '100').replace('px', '').replace('%', '')
                try:
                    if float(width) > 10 and float(height) > 10:
                        tiene_imagen_relevante = True
                except ValueError:
                    tiene_imagen_relevante = True

        # Si tras procesar las imágenes sigue habiendo muy poco texto, y hay imágenes visuales relevantes
        if len(texto_limpio.split()) < 20 and tiene_imagen_relevante:
            return "Tipo de contenido: correo de imagen"

    return texto_limpio


# ---------------------------------------------------------------
# Lectura de correos y eventos
# ---------------------------------------------------------------

def _mapa_encabezados(headers):
    """Convierte la lista de encabezados de Gmail en un dict en minúsculas.
    Gmail no garantiza mayúsculas consistentes ('List-Unsubscribe' vs
    'list-unsubscribe'), y el clasificador necesita buscarlos sin fallar."""
    return {h['name'].lower(): h['value'] for h in headers}


def _acortar_remitente(texto, limite=120):
    """Recorta el remitente SIN partir la dirección de correo.

    Con el corte a 120 caracteres a secas salía
    'notificacionesbdb@bancodebogota.ne' (sin la 't' final), lo que además
    hace irreconocible el dominio para el clasificador.
    """
    texto = (texto or "").strip()
    if len(texto) <= limite:
        return texto
    m = re.search(r'<([^>]+)>\s*$', texto)
    if m:
        direccion = m.group(0)
        nombre = texto[:m.start()].strip()
        espacio = limite - len(direccion) - 1
        if espacio > 3:
            return f"{nombre[:espacio].rstrip()} {direccion}"
        return direccion[:limite]
    return texto[:limite]


def _extraer_correo(texto_remitente):
    """Saca la dirección de 'Nombre <correo@dominio.com>'. El dominio es una
    de las señales más fiables para clasificar sin gastar modelo."""
    m = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', texto_remitente or '')
    return m.group(0).lower() if m else ''


def _listar_ids(gmail_service, consulta, limite=None):
    """Lista IDs paginando. limite=None significa TODOS los que haya.

    Antes se pedía una sola página sin filtro, así que nunca se pasaba de los
    mensajes más recientes.
    """
    ids = []
    token = None
    while True:
        por_pedir = 100 if limite is None else min(100, limite - len(ids))
        if por_pedir <= 0:
            break
        respuesta = gmail_service.users().messages().list(
            userId='me',
            q=consulta,
            maxResults=por_pedir,
            pageToken=token,
        ).execute()
        ids.extend(m['id'] for m in respuesta.get('messages', []))
        token = respuesta.get('nextPageToken')
        if not token:
            break
    return ids if limite is None else ids[:limite]


TAMANO_LOTE = 20      # peticiones por batch; Gmail las ejecuta EN PARALELO, y
                      # el límite de concurrencia por usuario es bajo. Con 100
                      # devuelve 429 "Too many concurrent requests" de una.
PAUSA_ENTRE_LOTES = 0.35   # segundos
MAX_REINTENTOS = 4


def _descargar_detalles(gmail_service, ids, formato='full', progreso=None):
    """Descarga mensajes por lotes, con reintento ante límite de tasa.

    formato='metadata' trae encabezados + fragmento del cuerpo y pesa muy
    poco; 'full' trae el mensaje completo (uno de los correos reales de
    prueba pesa 4,6 MB). Por eso la revisión usa metadata para TODA la
    bandeja y full solo para los correos que se van a resumir.

    Un 429 no es un error definitivo: es Gmail pidiendo que se baje el ritmo.
    Esos mensajes se reintentan con espera creciente y lotes más pequeños, en
    vez de darlos por perdidos.
    """
    detalles = {}
    pendientes = list(ids)
    tamano = TAMANO_LOTE
    total = len(ids)

    for intento in range(MAX_REINTENTOS):
        if not pendientes:
            break

        fallidos = []

        def _callback(request_id, response, exception):
            if exception is None:
                detalles[request_id] = response
                return
            estado = getattr(getattr(exception, 'resp', None), 'status', None)
            if estado in (403, 429, 500, 502, 503):
                fallidos.append(request_id)      # transitorio: se reintenta
            else:
                print(f"⚠️ No se pudo descargar el mensaje {request_id}: {exception}")

        for inicio in range(0, len(pendientes), tamano):
            bloque = pendientes[inicio:inicio + tamano]
            batch = gmail_service.new_batch_http_request(callback=_callback)
            for msg_id in bloque:
                if formato == 'metadata':
                    peticion = gmail_service.users().messages().get(
                        userId='me', id=msg_id, format='metadata',
                        metadataHeaders=['From', 'To', 'Subject', 'Date', 'List-Unsubscribe'],
                    )
                else:
                    peticion = gmail_service.users().messages().get(
                        userId='me', id=msg_id, format='full',
                    )
                batch.add(peticion, request_id=msg_id)

            try:
                batch.execute()
            except Exception as e:
                # Si revienta el lote entero, todos sus mensajes se reintentan.
                print(f"⚠️ Lote fallido ({type(e).__name__}); se reintentará.")
                fallidos.extend(bloque)

            if progreso:
                try:
                    progreso(len(detalles), total)
                except Exception:
                    pass
            time.sleep(PAUSA_ENTRE_LOTES)

        pendientes = fallidos
        if pendientes:
            # Espera exponencial con algo de azar, y lotes a la mitad: es la
            # forma estándar de responder a un límite de tasa.
            espera = (2 ** intento) + random.random()
            tamano = max(5, tamano // 2)
            print(f"⏳ Gmail pidió bajar el ritmo. Reintentando {len(pendientes)} "
                  f"mensajes en {espera:.1f}s (lotes de {tamano})...")
            time.sleep(espera)

    if pendientes:
        print(f"⚠️ Quedaron {len(pendientes)} mensajes sin descargar tras "
              f"{MAX_REINTENTOS} intentos. Se mostrarán en la próxima revisión.")

    return detalles


def _cuerpo_desde_payload(payload):
    """Selecciona y decodifica el cuerpo del mensaje (prefiere HTML sobre texto
    plano). La extracción del texto útil la hace extraccion_stefany."""
    def obtener_data_cuerpo(partes):
        html_data = None
        plain_data = None
        for p in partes:
            if p.get('mimeType') == 'text/html' and 'data' in p.get('body', {}):
                html_data = p['body']['data']
            elif p.get('mimeType') == 'text/plain' and 'data' in p.get('body', {}):
                plain_data = p['body']['data']
            elif 'parts' in p:
                nested = obtener_data_cuerpo(p['parts'])
                if nested:
                    return nested
        return html_data or plain_data

    data = None
    if 'parts' in payload:
        data = obtener_data_cuerpo(payload['parts'])
    elif 'data' in payload.get('body', {}):
        data = payload['body']['data']

    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
    except Exception:
        return ""


def leer_correos_cuenta(alias, limite=None, solo_no_leidos=True, progreso=None, fecha=None):
    """PASADA 1 — trae TODOS los correos no leídos, solo con metadatos.

    No descarga los cuerpos: encabezados y un fragmento de texto. Así una
    bandeja de 500 sin leer se puede traer y clasificar completa sin
    descargar megabytes ni gastar un solo token.

    limite=None significa "todos". El parámetro existe para poder acotar si
    la API empieza a devolver errores de cuota.
    """
    if alias not in cuentas_conectadas:
        print(f"⚠️ La cuenta '{alias}' no está conectada.")
        return []

    gmail_service = cuentas_conectadas[alias]['gmail']
    consulta = "in:inbox is:unread" if solo_no_leidos else "in:inbox"
    if fecha is not None:
        # Un día concreto: leídos y no leídos. Gmail interpreta after/before
        # con la zona horaria de la cuenta, así que se pide un día de margen a
        # cada lado y el filtro fino se hace luego con la fecha local.
        desde, hasta = fecha - timedelta(days=1), fecha + timedelta(days=2)
        consulta = (f"in:inbox after:{desde.strftime('%Y/%m/%d')} "
                    f"before:{hasta.strftime('%Y/%m/%d')}")

    ids = _listar_ids(gmail_service, consulta, limite)
    if not ids:
        return []

    detalles = _descargar_detalles(gmail_service, ids, formato='metadata', progreso=progreso)

    correos = []
    for msg_id in ids:
        detalle = detalles.get(msg_id)
        if not detalle:
            continue

        encabezados = _mapa_encabezados(detalle['payload'].get('headers', []))
        # Sin decodificar llegaba "Extensi?n Ingenieria de Sistemas UAN" al
        # prompt y a la interfaz.
        remitente = extraccion.decodificar_encabezado(encabezados.get('from', 'Desconocido'))
        asunto = extraccion.decodificar_encabezado(encabezados.get('subject', 'Sin asunto'))

        correos.append({
            'cuenta':          alias,
            'id':              msg_id,
            'hilo':            detalle.get('threadId', msg_id),
            'asunto':          asunto[:120],
            'remitente':       _acortar_remitente(remitente),
            'remitente_email': _extraer_correo(remitente),
            'destinatario':    encabezados.get('to', 'Desconocido')[:120],
            'fecha':           encabezados.get('date', '')[:40],
            # internalDate (ms desde epoch) lo pone Gmail y es fiable; el
            # encabezado 'Date' lo escribe el remitente y puede venir mal
            # formado o con zona horaria rara. Se usa para ordenar hilos.
            'fecha_ms':        int(detalle.get('internalDate', 0)),
            # El fragmento permite al clasificador ver algo del contenido sin
            # descargar el cuerpo completo.
            'fragmento':       detalle.get('snippet', '')[:300],
            'cuerpo':          '',          # se llena en hidratar_cuerpos()
            'cuerpo_vacio':    False,
            'no_leido':        'UNREAD' in detalle.get('labelIds', []),
            'etiquetas':       detalle.get('labelIds', []),
            # Encabezado que usa el clasificador para separar envío masivo de
            # correo humano: es la señal más fiable que hay.
            'lista_baja':      encabezados.get('list-unsubscribe', ''),
        })

    if fecha is not None:
        # Filtro fino con la hora local: la consulta a Gmail trae un día de
        # margen a cada lado para no perder correos por la zona horaria.
        correos = [c for c in correos
                   if datetime.fromtimestamp(c['fecha_ms'] / 1000, ZONA_BOGOTA).date() == fecha]

    return correos


def hidratar_hilo_anterior(alias, correos):
    """Trae el mensaje ANTERIOR del hilo para los correos que son respuesta.

    Sin esto, "no me es posible reunirme el día lunes en la sesión pactada" se
    analizaba sin saber qué sesión era: solo se mira el mensaje más reciente de
    cada hilo. Solo se consulta en los hilos con más de un mensaje, que son
    pocos, y se guarda recortado en 'cuerpo_hilo_anterior'.
    """
    if alias not in cuentas_conectadas or not correos:
        return correos

    servicio = cuentas_conectadas[alias]['gmail']
    for correo in correos:
        if (correo.get('mensajes_en_hilo') or 1) < 2 or not correo.get('hilo'):
            continue
        try:
            hilo = servicio.users().threads().get(
                userId='me', id=correo['hilo'], format='full').execute()
        except Exception as e:
            print(f"⚠️ No se pudo leer el hilo de {correo.get('id')}: {e}")
            continue

        actual_ms = int(correo.get('fecha_ms') or 0)
        previos = [m for m in hilo.get('messages', [])
                   if int(m.get('internalDate', 0)) < actual_ms]
        if not previos:
            continue
        anterior = max(previos, key=lambda m: int(m.get('internalDate', 0)))
        cuerpo, _ = extraccion.extraer_cuerpo(anterior.get('payload', {}),
                                              fragmento=anterior.get('snippet', ''))
        encabezados = _mapa_encabezados(anterior.get('payload', {}).get('headers', []))
        correo['cuerpo_hilo_anterior'] = (cuerpo or '')[:600]
        correo['remitente_hilo_anterior'] = _acortar_remitente(
            extraccion.decodificar_encabezado(encabezados.get('from', '')))
    return correos


def hidratar_cuerpos(alias, correos, progreso=None):
    """PASADA 2 — descarga el cuerpo completo SOLO de los correos indicados.

    Modifica los diccionarios en sitio y devuelve la lista. Se llama después
    de clasificar, con los pocos correos que de verdad van a pasar por el
    modelo.
    """
    if alias not in cuentas_conectadas or not correos:
        return correos

    gmail_service = cuentas_conectadas[alias]['gmail']
    ids = [c['id'] for c in correos]
    detalles = _descargar_detalles(gmail_service, ids, formato='full', progreso=progreso)

    for correo in correos:
        detalle = detalles.get(correo['id'])
        if not detalle:
            continue
        cuerpo, info = extraccion.extraer_cuerpo(
            detalle['payload'], fragmento=correo.get('fragmento', ''))
        correo['cuerpo'] = cuerpo[:1500]
        # Antes: 'if not body: continue' — el correo desaparecía sin avisar.
        # Ahora se conserva y se marca: un cuerpo vacío casi siempre significa
        # "el contenido está en una imagen", y eso es lo que dispara el OCR.
        correo['cuerpo_vacio'] = info['vacio']
        correo['origen_cuerpo'] = info['origen']
        correo['imagenes_adjuntas'] = info['imagenes_adjuntas']
        correo['imagenes_remotas'] = info['imagenes_remotas']

    return correos


def leer_correos_por_id(alias, ids):
    """Trae correos concretos, con cuerpo completo, a partir de sus IDs.

    Lo usa el resumen bajo demanda: el usuario pide el resumen de UN correo
    que la revisión no procesó, y no tiene sentido recorrer la bandeja entera
    para encontrarlo.
    """
    if alias not in cuentas_conectadas or not ids:
        return []

    gmail_service = cuentas_conectadas[alias]['gmail']
    detalles = _descargar_detalles(gmail_service, list(ids), formato='full')

    correos = []
    for msg_id in ids:
        detalle = detalles.get(msg_id)
        if not detalle:
            continue
        encabezados = _mapa_encabezados(detalle['payload'].get('headers', []))
        remitente = extraccion.decodificar_encabezado(encabezados.get('from', 'Desconocido'))
        cuerpo, _info = extraccion.extraer_cuerpo(
            detalle['payload'], fragmento=detalle.get('snippet', ''))
        correos.append({
            'cuenta':          alias,
            'id':              msg_id,
            'hilo':            detalle.get('threadId', msg_id),
            'asunto':          extraccion.decodificar_encabezado(encabezados.get('subject', 'Sin asunto'))[:120],
            'remitente':       _acortar_remitente(remitente),
            'remitente_email': _extraer_correo(remitente),
            'destinatario':    encabezados.get('to', 'Desconocido')[:120],
            'fecha':           encabezados.get('date', '')[:40],
            'fecha_ms':        int(detalle.get('internalDate', 0)),
            # Message-ID del correo original: es lo que hace que la respuesta
            # se enganche a la conversación en vez de llegar suelta.
            'message_id':      encabezados.get('message-id', ''),
            'fragmento':       detalle.get('snippet', '')[:300],
            'cuerpo':          cuerpo[:1500],
            'cuerpo_vacio':    _info['vacio'],
            # Sin estos dos campos el OCR recibía el correo sin saber qué
            # imágenes tiene, así que siempre respondía "no encontré imágenes
            # con contenido legible" — el botón nunca pudo funcionar.
            'imagenes_adjuntas': _info['imagenes_adjuntas'],
            'imagenes_remotas':  _info['imagenes_remotas'],
            'no_leido':        'UNREAD' in detalle.get('labelIds', []),
            'etiquetas':       detalle.get('labelIds', []),
            'lista_baja':      encabezados.get('list-unsubscribe', ''),
        })
    return correos


def _normalizar_hora_evento(valor_iso):
    """Convierte el dateTime de un evento a la hora LOCAL del equipo, sin
    importar en qué zona horaria haya venido la respuesta de Google."""
    if valor_iso is None or len(valor_iso) == 10:
        return valor_iso
    try:
        dt = datetime.fromisoformat(valor_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZONA_BOGOTA)
        return dt.astimezone(ZONA_BOGOTA).isoformat()
    except Exception:
        return valor_iso


def leer_eventos_cuenta(alias, cantidad=5, dias=7):
    if alias not in cuentas_conectadas:
        print(f"⚠️ La cuenta '{alias}' no está conectada.")
        return []

    calendar_service = cuentas_conectadas[alias]['calendar']
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=dias)
    # Desde la medianoche local, no desde este instante: con timeMin=now una
    # clase de hoy que ya empezó no aparecía, y no se podía detectar que un
    # correo la cancelaba.
    inicio_hoy = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)

    events_result = calendar_service.events().list(
        calendarId='primary',
        timeMin=inicio_hoy.isoformat(),
        timeMax=end.isoformat(),
        maxResults=cantidad,
        singleEvents=True,
        orderBy='startTime'
    ).execute()

    eventos = []
    for event in events_result.get('items', []):
        start    = event['start'].get('dateTime', event['start'].get('date'))
        end_time = event['end'].get('dateTime',   event['end'].get('date'))
        eventos.append({
            'cuenta':      alias,
            'id':          event['id'],
            'titulo':      event.get('summary', 'Sin título'),
            'inicio':      _normalizar_hora_evento(start),
            'fin':         _normalizar_hora_evento(end_time),
            'descripcion': event.get('description', ''),
            # Necesario para CU3: sin el lugar actual no se puede saber si el
            # correo anuncia un cambio de sitio o repite el que ya estaba.
            'lugar':       event.get('location', ''),
        })
    return eventos


def leer_correos_todas_cuentas(limite=None, solo_no_leidos=True, progreso=None, fecha=None):
    """PASADA 1 sobre todas las cuentas conectadas."""
    todos = {}
    for alias in cuentas_conectadas:
        try:
            todos[alias] = leer_correos_cuenta(
                alias, limite=limite, solo_no_leidos=solo_no_leidos,
                progreso=progreso, fecha=fecha,
            )
        except Exception as e:
            # Que una cuenta falle (token vencido, cuota) no debe tumbar la
            # revisión de las demás.
            print(f"⚠️ Error leyendo la cuenta '{alias}': {e}")
            todos[alias] = []
    return todos


def leer_eventos_todas_cuentas(cantidad=5, dias=7):
    todos = {}
    for alias in cuentas_conectadas:
        todos[alias] = leer_eventos_cuenta(alias, cantidad, dias)
    return todos


# ---------------------------------------------------------------
# Acciones: crear/editar evento, enviar correo
# ---------------------------------------------------------------

def crear_evento_calendar(alias, titulo, fecha_inicio_iso, fecha_fin_iso, descripcion="",
                          recurrencia=None, lugar=""):
    if alias not in cuentas_conectadas:
        print(f"⚠️ La cuenta '{alias}' no está conectada.")
        return None

    evento = {
        'summary': titulo,
        'description': descripcion,
        # El dateTime ISO ya lleva el desfase horario, así que el evento queda
        # correcto aunque no se envíe el nombre de la zona.
        'start': ({'dateTime': fecha_inicio_iso, 'timeZone': _nombre_zona_horaria()}
                  if _nombre_zona_horaria() else {'dateTime': fecha_inicio_iso}),
        'end':   ({'dateTime': fecha_fin_iso, 'timeZone': _nombre_zona_horaria()}
                  if _nombre_zona_horaria() else {'dateTime': fecha_fin_iso}),
    }
    # El lugar es parte del evento para la monografía (RF8 y CU3): sin él,
    # un correo que dice "nos vemos en el laboratorio de sistemas" creaba un
    # evento sin sitio y después no había nada que actualizar.
    if lugar:
        evento['location'] = lugar
    # Eventos que se repiten ("todos los viernes"): lista de reglas RRULE.
    if recurrencia:
        evento['recurrence'] = list(recurrencia)
    try:
        resultado = cuentas_conectadas[alias]['calendar'].events().insert(
            calendarId='primary', body=evento
        ).execute()
        link = resultado.get('htmlLink', '')
        print(f"✅ Evento '{titulo}' creado en [{alias}]: {link}")
        return link
    except Exception as e:
        print(f"❌ Error al crear evento: {e}")
        return None


def editar_evento_calendar(alias, event_id, titulo=None, fecha_inicio_iso=None,
                           fecha_fin_iso=None, lugar=None):
    """Modifica un evento existente: nombre, fecha, hora y/o lugar (RF8).

    Solo se envían los campos que llegan distintos de None, así que cambiar
    el lugar no toca el horario y mover la hora no borra el sitio.
    """
    if alias not in cuentas_conectadas:
        print(f"⚠️ La cuenta '{alias}' no está conectada.")
        return None

    cuerpo = {}
    if titulo is not None:
        cuerpo['summary'] = titulo
    if lugar is not None:
        cuerpo['location'] = lugar
    if fecha_inicio_iso is not None:
        cuerpo['start'] = {'dateTime': fecha_inicio_iso}
        if _nombre_zona_horaria():
            cuerpo['start']['timeZone'] = _nombre_zona_horaria()
    if fecha_fin_iso is not None:
        cuerpo['end'] = {'dateTime': fecha_fin_iso}
        if _nombre_zona_horaria():
            cuerpo['end']['timeZone'] = _nombre_zona_horaria()

    if not cuerpo:
        # Un patch vacío devuelve el evento intacto, así que la interfaz
        # decía "Actualizado" sin haber cambiado nada.
        print("⚠️ No hay ningún campo que cambiar en el evento.")
        return None

    try:
        resultado = cuentas_conectadas[alias]['calendar'].events().patch(
            calendarId='primary', eventId=event_id, body=cuerpo
        ).execute()
        link = resultado.get('htmlLink', '')
        print(f"✅ Evento actualizado en [{alias}]: {link}")
        return link
    except Exception as e:
        print(f"❌ Error al editar evento: {e}")
        return None


def eliminar_evento_calendar(alias, event_id):
    """Elimina UN evento. Solo se llama desde el diálogo de confirmación.

    Los eventos se leen con singleEvents=True, así que en una clase recurrente
    el id es el de esa sesión concreta: se borra ese día, no la serie entera.
    Google lo deja en la papelera del calendario durante 30 días.
    """
    if alias not in cuentas_conectadas:
        print(f"⚠️ La cuenta '{alias}' no está conectada.")
        return False
    try:
        cuentas_conectadas[alias]['calendar'].events().delete(
            calendarId='primary', eventId=event_id
        ).execute()
        print(f"🗑️ Evento {event_id} eliminado de [{alias}]")
        return True
    except Exception as e:
        print(f"❌ Error al eliminar evento: {e}")
        return False


def descargar_adjunto(alias, msg_id, id_adjunto):
    """Descarga los bytes de un adjunto.

    A diferencia de un archivo .eml, el JSON de la API de Gmail NO trae los
    bytes de los adjuntos: solo un attachmentId con el que hay que pedirlos
    aparte. Esto solo aplica a imágenes que viajan dentro del correo, así que
    descargarlas no le avisa de nada al remitente.
    """
    if alias not in cuentas_conectadas or not id_adjunto:
        return None
    try:
        respuesta = cuentas_conectadas[alias]['gmail'].users().messages().attachments().get(
            userId='me', messageId=msg_id, id=id_adjunto,
        ).execute()
        return base64.urlsafe_b64decode(respuesta['data'])
    except Exception as e:
        print(f"⚠️ No se pudo descargar el adjunto de {msg_id}: {e}")
        return None


def marcar_leidos(alias, ids):
    """Quita la etiqueta UNREAD en Gmail. Requiere el scope gmail.modify.

    Se hace por lotes de 1000 (límite de batchModify) y devuelve cuántos se
    marcaron. Es una acción sobre la bandeja REAL del usuario, así que nunca
    debe dispararse sin que él lo haya pedido.
    """
    if alias not in cuentas_conectadas or not ids:
        return 0
    servicio = cuentas_conectadas[alias]['gmail']
    marcados = 0
    for inicio in range(0, len(ids), 1000):
        bloque = list(ids)[inicio:inicio + 1000]
        try:
            servicio.users().messages().batchModify(
                userId='me',
                body={'ids': bloque, 'removeLabelIds': ['UNREAD']},
            ).execute()
            marcados += len(bloque)
        except Exception as e:
            print(f"❌ No se pudieron marcar como leídos en [{alias}]: {e}")
    return marcados


def marcar_no_leidos(alias, ids):
    """Vuelve a poner la etiqueta UNREAD. Es el deshacer de marcar_leidos()."""
    if alias not in cuentas_conectadas or not ids:
        return 0
    servicio = cuentas_conectadas[alias]['gmail']
    marcados = 0
    for inicio in range(0, len(ids), 1000):
        bloque = list(ids)[inicio:inicio + 1000]
        try:
            servicio.users().messages().batchModify(
                userId='me',
                body={'ids': bloque, 'addLabelIds': ['UNREAD']},
            ).execute()
            marcados += len(bloque)
        except Exception as e:
            print(f"❌ No se pudieron marcar como no leídos en [{alias}]: {e}")
    return marcados


def crear_borrador(alias, destinatario, asunto, cuerpo, id_hilo=None,
                   id_mensaje_respondido=None):
    """Crea un borrador en Gmail. NO lo envía.

    Es la única forma en que este sistema escribe un correo. El usuario lo
    revisa en Gmail y decide si lo manda. Si se pasa id_hilo, el borrador
    queda dentro de la conversación correspondiente.

    Devuelve el id del borrador, o None si falló.
    """
    if alias not in cuentas_conectadas:
        print(f"⚠️ La cuenta '{alias}' no está conectada.")
        return None

    mensaje = MIMEText(cuerpo, _charset='utf-8')
    mensaje['to'] = destinatario or ''
    mensaje['subject'] = asunto or ''
    if id_mensaje_respondido:
        mensaje['In-Reply-To'] = id_mensaje_respondido
        mensaje['References'] = id_mensaje_respondido

    crudo = base64.urlsafe_b64encode(mensaje.as_bytes()).decode()
    cuerpo_peticion = {'message': {'raw': crudo}}
    if id_hilo:
        cuerpo_peticion['message']['threadId'] = id_hilo

    try:
        resultado = cuentas_conectadas[alias]['gmail'].users().drafts().create(
            userId='me', body=cuerpo_peticion,
        ).execute()
        print(f"✅ Borrador creado en [{alias}] (id {resultado.get('id')})")
        return resultado.get('id')
    except Exception as e:
        print(f"❌ No se pudo crear el borrador en [{alias}]: {e}")
        return None


def enviar_correo_gmail(alias, destinatario, asunto, cuerpo):
    """⚠️ NO USAR. Envía un correo de verdad, sin vuelta atrás.

    Ninguna parte del sistema debe llamar a esta función: el flujo correcto es
    crear_borrador(), que deja el correo en Gmail para que el usuario lo revise
    y decida. Se conserva solo porque el día que exista un botón "Enviar" en la
    interfaz, con confirmación explícita del usuario, este es el código.

    Para evitar que una confirmación mal detectada ("claro", "listo") termine
    mandando un correo, hay que pasar permitir_envio=True a propósito.
    """
    raise RuntimeError(
        "enviar_correo_gmail() está desactivada. Usa crear_borrador(): "
        "el sistema nunca envía correos sin que el usuario lo haga desde Gmail."
    )


def _enviar_correo_gmail_real(alias, destinatario, asunto, cuerpo,
                              id_hilo=None, id_mensaje_respondido=None):
    """Envía el correo de verdad. Solo lo llama router.enviar_respuesta().

    Con id_hilo e id_mensaje_respondido la respuesta queda DENTRO de la
    conversación original. Sin ellos, Gmail la entregaba como un mensaje
    nuevo con asunto "Re: ...", fuera del hilo: el destinatario perdía el
    contexto y el sistema no cumplía "responder" en el sentido del RF9.
    """
    if alias not in cuentas_conectadas:
        print(f"⚠️ La cuenta '{alias}' no está conectada.")
        return False

    mensaje = MIMEText(cuerpo, _charset='utf-8')
    mensaje['to'] = destinatario
    mensaje['subject'] = asunto
    if id_mensaje_respondido:
        mensaje['In-Reply-To'] = id_mensaje_respondido
        mensaje['References'] = id_mensaje_respondido

    raw = base64.urlsafe_b64encode(mensaje.as_bytes()).decode()
    cuerpo_peticion = {'raw': raw}
    if id_hilo:
        cuerpo_peticion['threadId'] = id_hilo

    try:
        cuentas_conectadas[alias]['gmail'].users().messages().send(
            userId='me', body=cuerpo_peticion
        ).execute()
        print(f"✅ Correo enviado desde [{alias}] a {destinatario}")
        return True
    except Exception as e:
        print(f"❌ Error al enviar correo: {e}")
        return False
