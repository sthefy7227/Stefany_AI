# -*- coding: utf-8 -*-
"""
extraccion_stefany.py — Obtener el texto REAL de un correo.

Reemplaza a procesar_html_bs4(), que aplanaba el documento entero con
get_text(' ') y se traía menús, pies de página, historiales citados y
caracteres invisibles.

Medido sobre 15 correos reales:
  - Un recibo de pago gastaba 414 tokens en 276 caracteres invisibles
    (U+034F, U+200C) que no son espacios, así que re.sub(r'\\s+') no los tocaba.
  - get_text(' ') partía palabras: "r eciban", "¿ T e encanta", porque el HTML
    tenía etiquetas en mitad de la palabra.
  - Un hilo de 3 mensajes ocupaba 1.134 tokens; 669 eran el mismo historial
    repetido siete veces dentro de blockquotes anidados.
  - Los tres mensajes traían un text/plain impecable que el código ignoraba,
    porque prefería siempre el HTML.

No usa el modelo: es todo parsing y expresiones regulares.
"""

import re
import unicodedata
from email.header import decode_header, make_header

from bs4 import BeautifulSoup

# Caracteres de ancho cero y similares. No son espacios en blanco, así que
# \s no los captura, pero el tokenizador sí los cobra.
_INVISIBLES = dict.fromkeys(map(ord, (
    '\u200b'  # zero width space
    '\u200c'  # zero width non-joiner   (relleno típico de preheaders)
    '\u200d'  # zero width joiner
    '\u2060'  # word joiner
    '\ufeff'  # BOM
    '\u034f'  # combining grapheme joiner
    '\u00ad'  # soft hyphen
    '\u180e'  # mongolian vowel separator
)), None)

# Etiquetas que separan bloques de texto: ahí sí hace falta un salto de línea.
_BLOQUE = ('p', 'div', 'br', 'li', 'tr', 'table', 'blockquote', 'section',
           'article', 'header', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6')

# Etiquetas que nunca son contenido.
_BASURA = ('script', 'style', 'meta', 'noscript', 'head', 'link', 'title')

# Contenedores de texto citado en las respuestas, según el cliente de correo.
_SELECTORES_CITA = (
    'blockquote', '.gmail_quote', '.gmail_extra', '#divRplyFwdMsg',
    '.OutlookMessageHeader', '.moz-cite-prefix', '[id^="reply"]',
)

# Cabecera de cita: Gmail la parte en dos o tres líneas, así que se busca
# sobre el texto unido y no línea por línea.
_CITA = re.compile(
    r'(El\s.{0,200}?escribi[oó]\s*:'
    r'|On\s.{0,200}?wrote\s*:'
    r'|-{2,}\s*Mensaje original\s*-{2,}'
    r'|-{2,}\s*Forwarded message\s*-{2,}'
    r'|De:\s.{0,120}?\nEnviado el:)',
    re.IGNORECASE | re.DOTALL,
)

_FIRMA = re.compile(r'\n\s*(?:--|\*--\*|__|—)\s*\n')

# Pies de página de envíos masivos: a partir de aquí no hay contenido útil.
_PIE = re.compile(
    r'(si no deseas recibir|para darte de baja|darse de baja|cancelar la suscripci'
    r'|unsubscribe|ver en el navegador|view in browser|pol[ií]tica de privacidad'
    r'|este correo (?:fue|ha sido) enviado|no responda a este correo'
    r'|todos los derechos reservados|VIGILADA MINEDUCACI)',
    re.IGNORECASE,
)


_URL = re.compile(r'https?://\S+|www\.\S+')
_SOLO_SIMBOLOS = re.compile(r'^[\W_]+$')


def palabras_utiles(texto):
    """Cuenta solo palabras de verdad: sin URLs, hashtags ni signos sueltos.

    Un correo de la UAN cuyo contenido íntegro va dentro de imágenes remotas
    extraía 22 "palabras" que eran siete enlaces de redes sociales y un
    hashtag. Con el conteo ingenuo el correo parecía tener contenido y nunca
    se marcaba como vacío, así que jamás habría disparado el OCR — siendo
    justo el caso que lo necesita.
    """
    limpio = _URL.sub(' ', texto or '')
    return [p for p in limpio.split()
            if not p.startswith('#') and not _SOLO_SIMBOLOS.match(p)]


def decodificar_encabezado(valor):
    """Decodifica encabezados MIME (=?UTF-8?B?...?=) y los mal codificados.

    Sin esto llegaba "Extensi?n Ingenieria de Sistemas UAN" al prompt y a la
    interfaz.
    """
    if not valor:
        return ''
    try:
        return str(make_header(decode_header(valor)))
    except Exception:
        return valor


def quitar_invisibles(texto):
    return (texto or '').translate(_INVISIBLES)


def normalizar_espacios(texto):
    texto = re.sub(r'[ \t\xa0]+', ' ', texto or '')
    texto = re.sub(r' *\n *', '\n', texto)
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    return texto.strip()


def _cortar_en(texto, patron):
    m = patron.search(texto)
    return texto[:m.start()].rstrip() if m else texto


def limpiar_texto_plano(texto):
    """Limpia una parte text/plain: quita cita, firma y pie."""
    texto = quitar_invisibles((texto or '').replace('\r\n', '\n'))

    lineas = []
    for linea in texto.split('\n'):
        if linea.lstrip().startswith('>'):
            break                      # empieza el historial citado
        lineas.append(linea)
    texto = '\n'.join(lineas)

    texto = _cortar_en(texto, _CITA)
    texto = _cortar_en(texto, _FIRMA)
    texto = _cortar_en(texto, _PIE)
    texto = re.sub(r'\*(.+?)\*', r'\1', texto)   # negritas de texto plano
    return normalizar_espacios(texto)


def limpiar_html(html):
    """Extrae el texto de una parte text/html sin traerse el andamiaje."""
    if not html:
        return ''

    soup = BeautifulSoup(html, 'html.parser')

    for etiqueta in soup(_BASURA):
        etiqueta.decompose()

    # Historial citado: fuera entero.
    for selector in _SELECTORES_CITA:
        try:
            for nodo in soup.select(selector):
                nodo.decompose()
        except Exception:
            continue

    # Preheaders ocultos: el texto que el cliente muestra en la vista previa y
    # que en el correo va escondido, normalmente con relleno invisible.
    #
    # OJO con el tamaño: hay plantillas de mercadeo (por ejemplo, de Cisco)
    # que meten el cuerpo ENTERO dentro de un contenedor con
    # display:none. Descartarlo a ciegas dejaba el correo en cero palabras. Un
    # preheader de verdad son unas pocas decenas de palabras; si el nodo tiene
    # más, es el contenido.
    for nodo in soup.find_all(style=True):
        atributos = getattr(nodo, 'attrs', None)
        if not isinstance(atributos, dict):
            continue
        estilo = str(atributos.get('style') or '').replace(' ', '').lower()
        if not ('display:none' in estilo or 'max-height:0' in estilo or 'font-size:0' in estilo):
            continue
        if len(nodo.get_text(' ', strip=True).split()) <= 60:
            nodo.decompose()

    # Aquí está la diferencia con get_text(' '): se marca el salto SOLO en las
    # etiquetas de bloque y se une el resto sin separador, para no partir las
    # palabras que el HTML corta con etiquetas en línea ("r eciban").
    for etiqueta in soup.find_all(_BLOQUE):
        etiqueta.insert_before('\n')
        etiqueta.insert_after('\n')
    for celda in soup.find_all('td'):
        celda.insert_after(' ')

    texto = soup.get_text('')
    texto = quitar_invisibles(texto)
    texto = _cortar_en(texto, _CITA)
    texto = _cortar_en(texto, _PIE)
    return normalizar_espacios(texto)


def _recorrer_partes(payload, acumulador):
    """Recorre el árbol MIME que devuelve la API de Gmail."""
    import base64

    tipo = payload.get('mimeType', '')
    cuerpo = payload.get('body', {})
    datos = cuerpo.get('data')

    if datos and tipo in ('text/plain', 'text/html'):
        try:
            texto = base64.urlsafe_b64decode(datos).decode('utf-8', errors='ignore')
        except Exception:
            texto = ''
        clave = 'plain' if tipo == 'text/plain' else 'html'
        acumulador[clave] = (acumulador.get(clave) or '') + texto

    if tipo.startswith('image/'):
        acumulador['adjuntas'].append({
            'id_adjunto': cuerpo.get('attachmentId'),
            'nombre': payload.get('filename', ''),
            'tipo': tipo,
            'tamano': cuerpo.get('size', 0),
        })

    for parte in payload.get('parts', []) or []:
        _recorrer_partes(parte, acumulador)


def extraer_cuerpo(payload, fragmento=''):
    """Devuelve (texto_limpio, info).

    info = {
        'origen':            'plain' | 'html' | 'fragmento' | 'vacio',
        'imagenes_adjuntas': [...],   # van dentro del correo: OCR sin avisar a nadie
        'imagenes_remotas':  int,     # están en un servidor: descargarlas AVISA
        'vacio':             bool,
    }

    Prefiere text/plain sobre text/html. El código anterior hacía lo contrario
    ('return html_data or plain_data'), y en el hilo de prácticas eso
    significaba elegir 6.522 caracteres de HTML con el historial repetido
    siete veces, existiendo un text/plain de 300 caracteres con el mensaje
    nuevo y nada más.
    """
    acumulador = {'plain': '', 'html': '', 'adjuntas': []}
    _recorrer_partes(payload or {}, acumulador)

    texto_plano = limpiar_texto_plano(acumulador['plain'])
    texto_html = limpiar_html(acumulador['html'])

    # El text/plain solo gana si de verdad trae contenido: hay correos que lo
    # incluyen vacío o con un "Ver este correo en tu navegador" y nada más.
    if len(palabras_utiles(texto_plano)) >= 8:
        texto, origen = texto_plano, 'plain'
    elif texto_html:
        texto, origen = texto_html, 'html'
    elif texto_plano:
        texto, origen = texto_plano, 'plain'
    else:
        texto, origen = '', 'vacio'

    # Se guardan las URLs, no solo el número: hacen falta para poder
    # descargarlas SI el usuario pide leer las imágenes.
    remotas = []
    if acumulador['html']:
        for url in re.findall(r'<img[^>]+src=["\'](https?://[^"\']+)', acumulador['html'], re.I):
            if url not in remotas:
                remotas.append(url)

    vacio = len(palabras_utiles(texto)) < 8
    if vacio and fragmento:
        texto = normalizar_espacios(quitar_invisibles(fragmento))
        origen = 'fragmento'

    return texto, {
        'origen': origen,
        'imagenes_adjuntas': acumulador['adjuntas'],
        'imagenes_remotas': remotas,
        'n_remotas': len(remotas),
        'vacio': vacio,
    }
