# -*- coding: utf-8 -*-
"""
Stefany — Interfaz de escritorio en Flet

Requisitos:
    pip install flet==0.86.5 requests google-auth-oauthlib google-auth google-api-python-client

Para correrla:
    python app_stefany.py
"""

import os
import random
import re
import flet as ft

from cliente_stefany import ClienteStefany, ClienteStefanyError
import persistencia as p
import oauth_stefany as oauth
import gmail_calendar_stefany as gc
import prompts_stefany as prompts
import sys
import rutas_stefany as rutas
import router_stefany
import memoria_local
import onboarding_stefany


# ---------------------------------------------------------------
# Configuración Servidor Modal (Conexión automática)
# ---------------------------------------------------------------
URL_SERVIDOR_MODAL = "https://sthefyrojas7227--servidor-stefany-v4-fastapi-app.modal.run"
# Clave de DISTRIBUCIÓN: viaja dentro del .exe a propósito, para que
# cualquiera con el enlace pueda usar la app sin que se le pida nada. No se
# ofusca porque ofuscarla no protegería (se extrae del binario igual); solo
# evita que quien encuentre la URL del endpoint la use sin más.
# Tiene que coincidir con el Secret 'stefany-clave-api' de Modal:
#     modal secret create stefany-clave-api CLAVE_STEFANY=<esta misma cadena>
# El gasto lo protegen max_containers=3, el tope de tokens y el límite de
# gasto del panel de Modal, no esta clave.
CLAVE_API_STEFANY = "k9S-e-flSt2z3Yy1MJgyYL9hEAMeteiAjJizNUhmbF8"


# ---------------------------------------------------------------
# Imagen de la mascota/avatar de Stefany (pantalla de bienvenida)
# ---------------------------------------------------------------
# Empaquetada, los assets viven dentro del .exe (sys._MEIPASS), no junto al
# código fuente. rutas_stefany resuelve las dos situaciones.
CARPETA_ASSETS = os.path.join(rutas.carpeta_recursos(), "assets")
RUTA_AVATAR_BIENVENIDA = os.path.join(CARPETA_ASSETS, "avatar_bienvenida.png")
RUTA_LOGO = os.path.join(CARPETA_ASSETS, "logo.png")


# ---------------------------------------------------------------
# Paleta
# ---------------------------------------------------------------
PALETAS = {
    "dark": {
        "fondo_app":        "#0f1115",
        "fondo_sidebar":     "#161923",
        "fondo_area":        "#0f1115",
        "fondo_topbar":      "#161923",
        "fondo_input":       "#1c1f2b",
        "borde_input":       "#2c3554",
        "burbuja_usuario":   "#1d4ed8",
        "burbuja_asistente": "#1c1f2b",
        "texto_usuario":     "#ffffff",
        "texto_asistente":   "#e8e9ee",
        "texto_secundario":  "#8b90a3",
        "hover_fila":        "#1f2330",
        "accento":           "#1d4ed8",
        "accento_hover":     "#1739ad",
        "estado_ok":         "#22c55e",
        "estado_error":      "#ef4444",
        "avatar_asistente":  "#7c3aed",
        "borde_sutil":       "#20232f",
        "decorativo":        "#20232f",
        "texto_input":       "#f2f3f7",
        "boton_cuentas":     "#232842",
    },
    "light": {
        "fondo_app":        "#eaf1fc",
        "fondo_sidebar":     "#dbe7f9",
        "fondo_area":        "#eaf1fc",
        "fondo_topbar":      "#ffffff",
        "fondo_input":       "#ffffff",
        "borde_input":       "#c3d6fb",
        "burbuja_usuario":   "#1d4ed8",
        "burbuja_asistente": "#ffffff",
        "texto_usuario":     "#ffffff",
        "texto_asistente":   "#1f2330",
        "texto_secundario":  "#6b7280",
        "hover_fila":        "#cfe0f8",
        "accento":           "#1d4ed8",
        "accento_hover":     "#1739ad",
        "estado_ok":         "#16a34a",
        "estado_error":      "#dc2626",
        "avatar_asistente":  "#7c3aed",
        "borde_sutil":       "#e6e8ee",
        "decorativo":        "#bfd7f6",
        "texto_input":       "#1f2330",
        "boton_cuentas":     "#bcd6f5",
    },
}

COLOR_ICONO_SUGERENCIA = "#1d4ed8"

# Mensajes mientras el modelo trabaja: en primera persona, variados y según
# lo que se pidió. Antes era un único "Estamos pensando..." fijo.
MENSAJES_PROGRESO = {
    "correo": [
        "Revisando tu bandeja...", "Leyendo tus correos...",
        "Analizando lo que llegó...", "Organizando lo que encuentro...",
    ],
    "calendario": [
        "Consultando tu calendario...", "Revisando tus compromisos...",
        "Comparando fechas y horas...",
    ],
    "redaccion": [
        "Redactando...", "Buscando las palabras...", "Puliendo la redacción...",
    ],
    "general": [
        "Pensando...", "Analizando tu pregunta...", "Buscando la respuesta...",
        "Dame un momento...", "Revisando lo que sé...",
    ],
}

SUGERENCIAS_INICIO = [
    {"icono": ft.Icons.MAIL_OUTLINE, "texto": "Revisa mis correos", "color": COLOR_ICONO_SUGERENCIA},
    {"icono": ft.Icons.CALENDAR_MONTH, "texto": "¿Qué compromisos tengo hoy?", "color": COLOR_ICONO_SUGERENCIA},
    {"icono": ft.Icons.SCHEDULE, "texto": "Agenda una reunión mañana a las 3pm", "color": COLOR_ICONO_SUGERENCIA},
]


def _icono_conversacion(titulo):
    t = titulo.lower()
    if "correo" in t or "mail" in t:
        return ft.Icons.MAIL_OUTLINE
    if any(palabra in t for palabra in ("evento", "agenda", "reunión", "reunion", "compromiso")):
        return ft.Icons.CALENDAR_MONTH
    return ft.Icons.CHAT_BUBBLE_OUTLINE


class AppStefany:
    def __init__(self, page: ft.Page):
        self.page = page
        # La app abre en tema CLARO. Decisión de la autora: es lo que espera
        # quien la ve por primera vez; quien prefiera el oscuro lo cambia con
        # el botón de la barra superior.
        self.tema_actual = "light"
        self.paleta = PALETAS[self.tema_actual]

        self.cliente = None
        self.conversacion_actual = None
        self.modelo_seleccionado = "Ajustado"  # fijo -- ya no es seleccionable desde la UI

        page.title = "Stefany — Asistente de gestión de tiempo"
        page.window.width = 1060
        page.window.height = 680
        page.window.min_width = 820
        page.window.min_height = 520
        if os.path.exists(RUTA_LOGO):
            page.window.icon = RUTA_LOGO
        page.padding = 0
        page.bgcolor = self.paleta["fondo_app"]

        # Cuentas de Google ya conectadas en sesiones anteriores
        gc.actualizar_cuentas_conectadas(oauth.construir_cuentas_conectadas())

        self._construir_layout()
        self._refrescar_lista_conversaciones()
        self._refrescar_boton_pendientes()
        self._mostrar_estado_vacio()
        self.page.update()

        # Conexión automática transparente en segundo plano
        self._lanzar_hilo(self._conectar_automatico)

        # Primer inicio: conectar cuentas y preguntas de personalización.
        if memoria_local.necesita_onboarding():
            self._abrir_onboarding("cuentas")

    def _abrir_onboarding(self, desde_paso=None):
        """Abre el asistente de perfil. Sin paso indicado, retoma donde tiene sentido."""
        if desde_paso is None:
            desde_paso = "nombre" if gc.cuentas_conectadas else "cuentas"

        def al_cerrar():
            # El saludo y la tarjeta "Completa tu perfil" dependen del perfil.
            if not self.conversacion_actual:
                self._mostrar_estado_vacio()

        onboarding_stefany.AsistentePerfil(self, desde_paso, al_cerrar=al_cerrar).abrir()

    def _refrescar_boton_pendientes(self):
        try:
            cantidad = router_stefany.contar_pendientes()
        except Exception:
            cantidad = 0
        self.texto_boton_pendientes.value = f"Pendientes ({cantidad})" if cantidad else "Pendientes"
        try:
            # .page lanza excepción si el control todavía no está en la ventana.
            self.texto_boton_pendientes.update()
        except Exception:
            pass

    def _abrir_pendientes(self, e=None):
        """Lo que quedó sin agendar: propuestas enviadas y sugerencias no atendidas."""
        paleta = self.paleta
        acciones = router_stefany.acciones_pendientes(solo_propuestas=False)

        if acciones:
            contenido = ft.Column([self._fila_calendario(a) for a in acciones],
                                  spacing=0, tight=True, scroll=ft.ScrollMode.AUTO)
        else:
            contenido = ft.Column(
                [ft.Text("No tienes nada pendiente por agendar.", size=12,
                         color=paleta["texto_secundario"])], tight=True)

        def cerrar(ev):
            self.page.pop_dialog()
            self._refrescar_boton_pendientes()

        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Row([ft.Text("📌 Pendientes por agendar", size=18, weight=ft.FontWeight.BOLD,
                                  expand=True),
                          ft.IconButton(icon=ft.Icons.CLOSE, icon_size=18, tooltip="Cerrar",
                                        on_click=cerrar)],
                         vertical_alignment=ft.CrossAxisAlignment.CENTER),
            content=ft.Container(content=contenido, width=560, height=420),
            actions=[ft.FilledButton("Cerrar", on_click=cerrar)],
        ))

    def _abrir_preferencias(self, e=None):
        """Ver, editar y eliminar lo guardado en el perfil."""
        def al_cerrar():
            if not self.conversacion_actual:
                self._mostrar_estado_vacio()

        onboarding_stefany.VistaPreferencias(self, al_cerrar=al_cerrar).abrir()

    # =================================================================
    # LAYOUT PRINCIPAL
    # =================================================================
    def _construir_layout(self):
        paleta = self.paleta

        # ----------------- Panel izquierdo (sidebar) -----------------
        self.boton_nueva = ft.Button(
            content=ft.Row(
                [ft.Icon(ft.Icons.ADD, size=16, color="#ffffff"),
                 ft.Text("Nueva conversación", color="#ffffff", weight=ft.FontWeight.BOLD, size=13)],
                spacing=6, alignment=ft.MainAxisAlignment.CENTER,
            ),
            bgcolor=paleta["accento"], height=40, on_click=self._nueva_conversacion,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)),
        )

        self.label_conversaciones = ft.Text(
            "CONVERSACIONES", size=10, weight=ft.FontWeight.BOLD, color=paleta["texto_secundario"]
        )

        self.lista_conversaciones_col = ft.Column(spacing=2, scroll=ft.ScrollMode.AUTO, expand=True)

        self.icono_boton_cuentas = ft.Icon(ft.Icons.PERSON_OUTLINE, size=16, color=paleta["accento"])
        self.texto_boton_cuentas = ft.Text("Cuentas de Google", size=12, weight=ft.FontWeight.W_600, color=paleta["accento"])
        self.boton_cuentas = ft.Container(
            content=ft.Row(
                [self.icono_boton_cuentas, self.texto_boton_cuentas],
                spacing=6, alignment=ft.MainAxisAlignment.CENTER,
            ),
            height=36, bgcolor=paleta["boton_cuentas"], border_radius=10,
            on_click=self._abrir_ventana_cuentas, ink=True,
            alignment=ft.Alignment.CENTER,
        )

        # Todo lo que quedó por agendar (propuestas enviadas y sugerencias no
        # atendidas) vive aquí, no al final de cada revisión.
        self.icono_boton_pendientes = ft.Icon(ft.Icons.PENDING_ACTIONS, size=16, color=paleta["accento"])
        self.texto_boton_pendientes = ft.Text("Pendientes", size=12, weight=ft.FontWeight.W_600,
                                              color=paleta["accento"])
        self.boton_pendientes = ft.Container(
            content=ft.Row([self.icono_boton_pendientes, self.texto_boton_pendientes],
                           spacing=6, alignment=ft.MainAxisAlignment.CENTER),
            height=36, bgcolor=paleta["boton_cuentas"], border_radius=10,
            on_click=self._abrir_pendientes, ink=True, alignment=ft.Alignment.CENTER,
        )

        self.icono_boton_preferencias = ft.Icon(ft.Icons.TUNE, size=16, color=paleta["accento"])
        self.texto_boton_preferencias = ft.Text("Mis preferencias", size=12, weight=ft.FontWeight.W_600,
                                                color=paleta["accento"])
        self.boton_preferencias = ft.Container(
            content=ft.Row(
                [self.icono_boton_preferencias, self.texto_boton_preferencias],
                spacing=6, alignment=ft.MainAxisAlignment.CENTER,
            ),
            height=36, bgcolor=paleta["boton_cuentas"], border_radius=10,
            on_click=self._abrir_preferencias, ink=True,
            alignment=ft.Alignment.CENTER,
        )

        contenido_sidebar = ft.Container(
            content=ft.Column(
                [self.boton_nueva, ft.Container(height=4), self.label_conversaciones,
                 self.lista_conversaciones_col, self.boton_pendientes,
                 self.boton_preferencias, self.boton_cuentas],
                spacing=8, expand=True,
            ),
            padding=14, expand=True,
        )

        self.panel_izq = ft.Container(
            content=contenido_sidebar,
            width=250, bgcolor=paleta["fondo_sidebar"],
        )

        # ----------------- Barra superior -----------------
        self.boton_tema = ft.IconButton(
            icon=ft.Icons.LIGHT_MODE_OUTLINED if self.tema_actual == "dark" else ft.Icons.DARK_MODE_OUTLINED,
            icon_color=paleta["texto_asistente"], on_click=self._alternar_tema, tooltip="Cambiar tema",
        )

        self.pill_estado = ft.Text(
            "●  Conectando...", color="#f59e0b", size=12, weight=ft.FontWeight.BOLD
        )

        self.barra_superior = ft.Container(
            content=ft.Row(
                [self.boton_tema, ft.Container(expand=True), self.pill_estado],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            height=56, bgcolor=paleta["fondo_topbar"], padding=ft.Padding.symmetric(horizontal=18),
        )

        # ----------------- Área de mensajes -----------------
        self.area_mensajes_col = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=4)
        self.area_mensajes = ft.Container(
            content=self.area_mensajes_col, expand=True, bgcolor=paleta["fondo_area"], padding=10,
        )

        # ----------------- Barra inferior -----------------
        self.campo_mensaje = ft.TextField(
            # Arranca bloqueado: se abre cuando la barra superior dice "Conectado".
            disabled=True,
            hint_text="Conectando con el servidor...", border=ft.InputBorder.NONE,
            expand=True, text_size=13, on_submit=self._enviar_mensaje, content_padding=10,
            color=paleta["texto_input"], hint_style=ft.TextStyle(color=paleta["texto_secundario"]),
        )
        self.boton_enviar = ft.IconButton(
            icon=ft.Icons.SEND_ROUNDED, icon_color="#ffffff", bgcolor=paleta["accento"],
            on_click=self._enviar_mensaje, disabled=True,
        )
        self.caja_input = ft.Container(
            content=ft.Row([self.campo_mensaje, self.boton_enviar], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            border_radius=20, bgcolor=paleta["fondo_input"], border=ft.Border.all(2, paleta["borde_input"]),
            padding=ft.Padding.only(left=16, right=6, top=4, bottom=4),
            margin=ft.Margin.only(left=18, right=18, top=14, bottom=2),
        )
        # El resumen y el análisis los escribe un modelo de lenguaje, y en la
        # monografía se defiende justo eso: es una ayuda que hay que
        # verificar, no una fuente de verdad. Conviene que se lea siempre.
        self.aviso_ia = ft.Text(
            "Stefany es una IA y puede cometer errores. Verifica la información importante.",
            size=10, color=paleta["texto_secundario"],
            text_align=ft.TextAlign.CENTER,
        )
        self.barra_inferior = ft.Container(
            content=ft.Column(
                [self.caja_input,
                 ft.Container(content=self.aviso_ia, alignment=ft.Alignment.CENTER,
                              padding=ft.Padding.only(top=2, bottom=8))],
                spacing=0,
            ),
            bgcolor=paleta["fondo_area"],
        )

        panel_der = ft.Column(
            [self.barra_superior, self.area_mensajes, self.barra_inferior], spacing=0, expand=True
        )

        self.page.add(
            ft.Row(
                [self.panel_izq, ft.VerticalDivider(width=1, color=paleta["borde_sutil"]), panel_der],
                expand=True, spacing=0,
            )
        )

    # =================================================================
    # PANTALLA DE BIENVENIDA (estado vacío)
    # =================================================================
    def _mostrar_estado_vacio(self):
        paleta = self.paleta
        self.area_mensajes_col.controls.clear()

        avatar_control = self._crear_avatar_bienvenida()

        sugerencias_col = ft.Column(
            [self._tarjeta_sugerencia(s["icono"], s["texto"], s["color"]) for s in SUGERENCIAS_INICIO],
            spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )

        perfil = memoria_local.cargar_perfil()
        nombre = (perfil.get("nombre") or "").strip()
        saludo = f"¿En qué te ayudo hoy, {nombre}?" if nombre else "¿En qué te ayudo hoy?"

        controles = [
            avatar_control,
            ft.Container(height=8),
            ft.Text(saludo, size=18, weight=ft.FontWeight.BOLD, color=paleta["texto_asistente"]),
            ft.Text("Prueba con alguna de estas:", size=12, color=paleta["texto_secundario"]),
            ft.Container(height=6),
            sugerencias_col,
        ]
        # Quien pulsó "Dejar para después" puede retomarlo desde aquí.
        if not perfil.get("onboarding_completado"):
            controles += [
                ft.Container(height=6),
                self._tarjeta_accion(ft.Icons.AUTO_AWESOME, "Personaliza a Stefany: te faltan algunas preguntas",
                                     "#7c3aed", lambda e: self._abrir_onboarding()),
            ]

        centro = ft.Column(controles, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6)

        self.area_mensajes_col.controls.append(
            ft.Container(content=centro, alignment=ft.Alignment.TOP_CENTER, expand=True, padding=ft.Padding.only(top=40))
        )
        if self.area_mensajes_col.page:
            self.area_mensajes_col.update()

    def _crear_avatar_bienvenida(self):
        paleta = self.paleta
        if os.path.exists(RUTA_AVATAR_BIENVENIDA):
            return ft.Container(
                content=ft.Image(src=RUTA_AVATAR_BIENVENIDA, width=140, height=140, fit=ft.BoxFit.COVER),
                width=140, height=140, border_radius=70, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            )
        return ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.IMAGE_OUTLINED, size=32, color=paleta["texto_secundario"]),
                    ft.Text(
                        "Coloca tu imagen en\nassets/avatar_bienvenida.png", size=9,
                        color=paleta["texto_secundario"], text_align=ft.TextAlign.CENTER,
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=4,
            ),
            width=140, height=140, border_radius=70, bgcolor=paleta["fondo_input"],
            border=ft.Border.all(2, paleta["borde_sutil"]), alignment=ft.Alignment.CENTER,
        )

    def _tarjeta_sugerencia(self, icono, texto, color_icono):
        return self._tarjeta_accion(icono, texto, color_icono, lambda e, t=texto: self._usar_sugerencia(t))

    def _tarjeta_accion(self, icono, texto, color_icono, al_pulsar):
        paleta = self.paleta
        return ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Icon(icono, size=16, color="#ffffff"), width=34, height=34,
                        border_radius=17, bgcolor=color_icono, alignment=ft.Alignment.CENTER,
                    ),
                    ft.Text(texto, size=13, color=paleta["texto_asistente"], expand=True),
                    ft.Icon(ft.Icons.ARROW_FORWARD, size=16, color=paleta["texto_secundario"]),
                ],
                spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=420, height=56, border_radius=14, bgcolor=paleta["fondo_input"],
            border=ft.Border.all(1, paleta["borde_input"]), padding=ft.Padding.symmetric(horizontal=12),
            on_click=al_pulsar, ink=True,
        )

    def _usar_sugerencia(self, texto):
        self.campo_mensaje.value = texto
        self.campo_mensaje.update()
        self._enviar_mensaje(None)

    # =================================================================
    # CONVERSACIONES (panel izquierdo)
    # =================================================================
    def _refrescar_lista_conversaciones(self):
        self.lista_conversaciones_col.controls.clear()
        actual_id = self.conversacion_actual.get("id") if self.conversacion_actual else None
        for conv in p.listar_conversaciones():
            self.lista_conversaciones_col.controls.append(
                self._fila_conversacion(conv, conv["id"] == actual_id)
            )
        if self.lista_conversaciones_col.page:
            self.lista_conversaciones_col.update()

    def _fila_conversacion(self, conv, seleccionada):
        paleta = self.paleta
        fila = ft.Row(
            [
                ft.Icon(_icono_conversacion(conv["titulo"]), size=15, color=paleta["texto_secundario"]),
                ft.Container(
                    content=ft.Text(
                        conv["titulo"], size=13, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS,
                        weight=ft.FontWeight.BOLD if seleccionada else ft.FontWeight.NORMAL,
                        color=paleta["accento"] if seleccionada else paleta["texto_asistente"],
                    ),
                    expand=True, padding=ft.Padding.symmetric(vertical=7),
                    on_click=lambda e, cid=conv["id"]: self._abrir_conversacion(cid),
                ),
                ft.IconButton(
                    icon=ft.Icons.DELETE_OUTLINE, icon_size=15, icon_color=paleta["texto_secundario"],
                    on_click=lambda e, cid=conv["id"], t=conv["titulo"]: self._confirmar_eliminar(cid, t),
                ),
            ],
            spacing=6,
        )
        separador = ft.Container(
            height=2 if seleccionada else 1,
            bgcolor=paleta["accento"] if seleccionada else paleta["borde_sutil"],
            margin=ft.Margin.symmetric(horizontal=4, vertical=2),
        )
        return ft.Column([fila, separador], spacing=0)

    def _nueva_conversacion(self, e=None):
        self.conversacion_actual = p.nueva_conversacion()
        self._refrescar_lista_conversaciones()
        self._renderizar_mensajes()

    def _abrir_conversacion(self, conv_id):
        self.conversacion_actual = p.cargar_conversacion(conv_id)
        self._refrescar_lista_conversaciones()
        self._renderizar_mensajes()

    def _confirmar_eliminar(self, conv_id, titulo):
        def eliminar(e):
            p.eliminar_conversacion(conv_id)
            if self.conversacion_actual and self.conversacion_actual.get("id") == conv_id:
                self.conversacion_actual = None
                self._mostrar_estado_vacio()
            self.page.pop_dialog()
            self._refrescar_lista_conversaciones()

        dlg = ft.AlertDialog(
            modal=True, title=ft.Text("Eliminar conversación"),
            content=ft.Text(f'¿Eliminar "{titulo}"?\nEsta acción no se puede deshacer.'),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda e: self.page.pop_dialog()),
                ft.Button("Eliminar", bgcolor="#dc2626", color="#ffffff", on_click=eliminar),
            ],
        )
        self.page.show_dialog(dlg)

    # =================================================================
    # MENSAJES / BURBUJAS
    # =================================================================
    def _renderizar_mensajes(self):
        if not self.conversacion_actual or not self.conversacion_actual.get("mensajes"):
            self._mostrar_estado_vacio()
            return
        self.area_mensajes_col.controls.clear()
        for msg in self.conversacion_actual["mensajes"]:
            self.area_mensajes_col.controls.append(self._burbuja_mensaje(msg))
        if self.area_mensajes_col.page:
            self.area_mensajes_col.update()

    def _avatar_asistente(self):
        paleta = self.paleta
        if os.path.exists(RUTA_LOGO):
            return ft.Container(
                content=ft.Image(src=RUTA_LOGO, width=30, height=30, fit=ft.BoxFit.COVER),
                width=30, height=30, border_radius=15, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            )
        return ft.CircleAvatar(
            content=ft.Text("S", color="#ffffff", weight=ft.FontWeight.BOLD, size=13),
            bgcolor=paleta["avatar_asistente"], radius=15,
        )

    def _avatar_usuario(self):
        paleta = self.paleta
        return ft.CircleAvatar(
            content=ft.Text("🙂", size=15),
            bgcolor=paleta["fondo_input"], radius=15,
        )

    def _burbuja_mensaje(self, msg):
        paleta = self.paleta
        es_usuario = msg["rol"] == "usuario"

        # Si el mensaje trae la estructura de una revisión de correos, se
        # pinta como acordeón jerárquico en vez de como un bloque de texto.
        if not es_usuario and msg.get("datos", {}).get("tipo") == "revision_correos":
            return self._tarjeta_revision(msg["datos"], msg.get("hora"))

        color_fondo = paleta["burbuja_usuario"] if es_usuario else paleta["burbuja_asistente"]
        color_texto = paleta["texto_usuario"] if es_usuario else paleta["texto_asistente"]

        columna_contenido = [ft.Text(msg["texto"], size=13, color=color_texto, selectable=True)]
        if not es_usuario:
            columna_contenido.insert(
                0, ft.Text("Stefany", size=10, weight=ft.FontWeight.BOLD, color=paleta["texto_secundario"])
            )

        burbuja = ft.Container(
            content=ft.Column(columna_contenido, spacing=4, tight=True),
            bgcolor=color_fondo, border_radius=14, padding=12, width=440,
            border=None if es_usuario else ft.Border.all(1, paleta["borde_sutil"]),
        )

        avatar = self._avatar_usuario() if es_usuario else self._avatar_asistente()
        if es_usuario:
            fila_burbuja = ft.Row(
                [burbuja, avatar], alignment=ft.MainAxisAlignment.END,
                vertical_alignment=ft.CrossAxisAlignment.END, spacing=8,
            )
        else:
            fila_burbuja = ft.Row(
                [avatar, burbuja], alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.END, spacing=8,
            )

        controles = [fila_burbuja]
        hora = msg.get("hora")
        if hora:
            texto_hora = ft.Text(hora, size=10, color=paleta["texto_secundario"])
            fila_hora = ft.Row(
                [texto_hora],
                alignment=ft.MainAxisAlignment.END if es_usuario else ft.MainAxisAlignment.START,
            )
            controles.append(
                ft.Container(content=fila_hora, padding=ft.Padding.only(left=0 if es_usuario else 44, right=44 if es_usuario else 0))
            )

        return ft.Container(
            content=ft.Column(controles, spacing=1, tight=True),
            padding=ft.Padding.symmetric(horizontal=14, vertical=4),
        )

    # =================================================================
    # ENVÍO DE MENSAJES
    # =================================================================
    def _enviar_mensaje(self, e):
        texto = (self.campo_mensaje.value or "").strip()
        if not texto:
            return
        if not self.cliente:
            self._reintentar_conexion()
            return
        if not gc.cuentas_conectadas:
            self._abrir_ventana_cuentas()
            return

        if self.conversacion_actual is None:
            self.conversacion_actual = p.nueva_conversacion()
            self._refrescar_lista_conversaciones()

        modelo = self.modelo_seleccionado  # siempre "Ajustado" -- ya no es elegible desde la UI
        prompts.configurar_modo_prueba(forzar_base=False)
        historial = [{"rol": m["rol"], "texto": m["texto"]} for m in self.conversacion_actual.get("mensajes", [])]

        p.agregar_mensaje(self.conversacion_actual, "usuario", texto)
        self._renderizar_mensajes()
        self.campo_mensaje.value = ""
        self.campo_mensaje.update()
        self.boton_enviar.disabled = True
        self.boton_enviar.update()
        self._mostrar_progreso(mensaje=texto)

        self._lanzar_hilo(self._llamar_modelo_hilo, texto, historial, modelo)

    def _llamar_modelo_hilo(self, texto, historial, modelo):
        datos = None
        try:
            respuesta = router_stefany.asistente_multicuenta(
                texto, historial=historial, progreso=self._actualizar_progreso,
                conversacion_id=self.conversacion_actual["id"],
            )
            # La revisión de correos devuelve estructura; el resto, texto.
            if isinstance(respuesta, dict):
                datos = respuesta
                respuesta = respuesta.get("texto", "")
        except ClienteStefanyError as ex:
            respuesta = f"⚠️ {ex}"
        except Exception as ex:
            import traceback
            traceback.print_exc()
            respuesta = f"⚠️ Ocurrió un error inesperado: {ex}"

        self._quitar_progreso()
        modelo_guardado = "base" if modelo == "Base" else "ajustado"
        p.agregar_mensaje(self.conversacion_actual, "asistente", respuesta,
                          modelo=modelo_guardado, datos=datos)
        self._renderizar_mensajes()
        self._refrescar_lista_conversaciones()
        self._refrescar_boton_pendientes()
        # Si se perdió el servidor mientras respondía, el chat sigue bloqueado.
        self.boton_enviar.disabled = not self.cliente
        self.boton_enviar.update()

    # =================================================================
    # INDICADOR DE PROGRESO
    # =================================================================
    # =================================================================
    # REPINTADO DESDE HILOS DE TRABAJO
    # =================================================================
    def _despertar_interfaz(self):
        """Obliga a Flet a mandar a la ventana lo que se acaba de actualizar.

        CAUSA del "no se actualiza hasta que minimizo y restauro" (Flet
        0.86.5): `control.update()` termina en
        `FletSocketServer.send_message()`, que hace `send_queue.put_nowait()`
        sobre una **asyncio.Queue**. Llamado desde un hilo de trabajo
        (`page.run_thread` usa `run_in_executor`, o sea un hilo de verdad),
        el paquete entra en la cola pero el consumidor `await queue.get()`
        NO se despierta: asyncio.Queue no es thread-safe. El dato se queda
        ahí hasta que algo ajeno despierta el bucle de eventos — un clic,
        cambiar de tema o minimizar y restaurar la ventana. Por eso el
        progreso aparecía de golpe y con retraso.

        `call_soon_threadsafe` escribe en la tubería interna del bucle, que
        es la forma soportada de despertarlo desde otro hilo; al despertar,
        vacía la cola y la ventana se pinta.
        """
        try:
            self.page.session.connection.loop.call_soon_threadsafe(lambda: None)
        except Exception:
            # Sin bucle (pruebas, cierre de la app) no hay nada que despertar.
            pass

    def _lanzar_hilo(self, funcion, *args):
        """Lanza trabajo en segundo plano y garantiza que lo que pinte se vea.

        Sustituye a `page.run_thread()` en toda la app: al terminar, despierta
        el bucle para que los cambios hechos desde el hilo lleguen a la
        ventana (ver `_despertar_interfaz`).
        """
        def envoltura(*a):
            try:
                funcion(*a)
            finally:
                self._despertar_interfaz()

        self.page.run_thread(envoltura, *args)

    def _tema_progreso(self, mensaje=""):
        texto = (mensaje or "").lower()
        if any(p in texto for p in ("correo", "email", "bandeja", "mensaje")):
            return "correo"
        if any(p in texto for p in ("calendario", "agenda", "evento", "reunión",
                                    "reunion", "cita", "compromiso")):
            return "calendario"
        if any(p in texto for p in ("responde", "redacta", "escribe", "borrador")):
            return "redaccion"
        return "general"

    def _texto_progreso_inicial(self, mensaje=""):
        """Primer mensaje del indicador, según lo que pidió el usuario.

        Antes salía siempre "Estamos revisando tus correos...", incluso al
        preguntar cualquier otra cosa: era el único texto que existía y decía
        algo que no estaba pasando. Ahora, además, habla en primera persona y
        va cambiando mientras espera (ver _rotar_progreso).
        """
        return random.choice(MENSAJES_PROGRESO[self._tema_progreso(mensaje)])

    def _rotar_progreso(self, tema):
        """Cambia el mensaje cada pocos segundos mientras no haya fase real.

        Un texto fijo durante un minuto parece que la aplicación se colgó. En
        cuanto el router informa de una fase concreta ("Leyendo los correos
        importantes…"), la rotación se detiene: lo real manda sobre lo vago.
        """
        import time
        mensajes = [m for m in MENSAJES_PROGRESO[tema]]
        random.shuffle(mensajes)
        indice = 0
        while getattr(self, "_rotacion_progreso", 0) == self._rotacion_id:
            time.sleep(3.5)
            if getattr(self, "_rotacion_progreso", 0) != self._rotacion_id:
                return
            if not getattr(self, "burbuja_progreso", None):
                return
            indice = (indice + 1) % len(mensajes)
            self._actualizar_progreso(mensajes[indice], rotando=True)

    def _mostrar_progreso(self, reiniciar=True, mensaje=""):
        """Burbuja viva mientras el sistema trabaja. Antes no había nada entre
        el envío y la respuesta, y con una bandeja grande la ventana parecía
        congelada durante minutos.

        reiniciar=False la reconstruye conservando el texto y el avance
        actuales: se usa al cambiar de tema, porque _renderizar_mensajes()
        repinta el área desde los mensajes GUARDADOS y esta burbuja, que es
        temporal, se perdía.
        """
        paleta = self.paleta
        if reiniciar:
            self._estado_progreso = {"texto": self._texto_progreso_inicial(mensaje),
                                     "hechos": 0, "total": 0}
            self._rotacion_id = getattr(self, "_rotacion_id", 0) + 1
            self._rotacion_progreso = self._rotacion_id
            self._lanzar_hilo(self._rotar_progreso, self._tema_progreso(mensaje))
        estado = getattr(self, "_estado_progreso", None) or {
            "texto": "Pensando...", "hechos": 0, "total": 0}

        self.texto_progreso = ft.Text(
            estado["texto"], size=13, color=paleta["texto_asistente"],
        )
        self.barra_progreso = ft.ProgressBar(
            width=240, height=4, color=paleta["accento"], bgcolor=paleta["borde_sutil"],
        )
        if estado["total"]:
            self.barra_progreso.value = estado["hechos"] / estado["total"]
            texto_contador = f"{estado['hechos']} de {estado['total']}"
        else:
            self.barra_progreso.value = None
            texto_contador = ""
        self.contador_progreso = ft.Text(texto_contador, size=11, color=paleta["texto_secundario"])

        burbuja = ft.Container(
            content=ft.Column(
                [self.texto_progreso, self.barra_progreso, self.contador_progreso],
                spacing=6, tight=True,
            ),
            bgcolor=paleta["burbuja_asistente"], border_radius=14, padding=12, width=440,
            border=ft.Border.all(1, paleta["borde_sutil"]),
        )
        self.burbuja_progreso = ft.Container(
            content=ft.Row(
                [self._avatar_asistente(), burbuja],
                alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.END, spacing=8,
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=4),
        )
        self.area_mensajes_col.controls.append(self.burbuja_progreso)
        if self.area_mensajes_col.page:
            self.area_mensajes_col.update()

    def _actualizar_progreso(self, texto, hechos=0, total=0, rotando=False):
        """Lo llama el router en cada fase real del proceso."""
        if not rotando:
            # Hay una fase real: se corta la rotación de mensajes genéricos.
            self._rotacion_progreso = None
        self._estado_progreso = {"texto": texto, "hechos": hechos, "total": total}
        if not getattr(self, "burbuja_progreso", None):
            return
        try:
            self.texto_progreso.value = texto
            if total:
                self.barra_progreso.value = hechos / total
                self.contador_progreso.value = f"{hechos} de {total}"
            else:
                self.barra_progreso.value = None   # indeterminada
                self.contador_progreso.value = ""
            # Actualizar cada control por separado no bastaba: los cambios no
            # llegaban a la ventana hasta que algo forzaba un repintado
            # completo (por eso se veían al cambiar de tema). page.update()
            # envía todo lo pendiente de una vez.
            self.texto_progreso.update()
            self.barra_progreso.update()
            self.contador_progreso.update()
            if self.page:
                self.page.update()
            # ...y page.update() tampoco bastaba: deja el paquete en una cola
            # asyncio que nadie consume hasta que el bucle despierta. Esta es
            # la pieza que faltaba para que el avance se vea EN VIVO y no al
            # minimizar la ventana.
            self._despertar_interfaz()
        except Exception:
            pass

    def _quitar_progreso(self):
        self._rotacion_progreso = None      # detiene la rotación de mensajes
        burbuja = getattr(self, "burbuja_progreso", None)
        if burbuja and burbuja in self.area_mensajes_col.controls:
            self.area_mensajes_col.controls.remove(burbuja)
        self.burbuja_progreso = None

    # =================================================================
    # VISTA JERÁRQUICA DE CORREOS (resumen general → categoría → correo)
    # =================================================================
    LIMITE_PLEGADO = 15   # cuántos correos se muestran de golpe al abrir una
                          # categoría secundaria; el resto va tras "ver más"

    def _tarjeta_revision(self, datos, hora=None):
        paleta = self.paleta
        bloques = [self._cabecera_revision(datos)]

        destacados = datos.get("destacados", [])
        if destacados:
            bloques.append(self._bloque_destacados(destacados))

        for grupo in datos.get("grupos", []):
            bloques.append(self._acordeon_categoria(grupo))

        # Aunque no haya nada que decidir hay que decirlo: los flujos A1 del
        # CU1 y del CU4 piden informar que se revisó y no se encontró nada.
        bloque_cal = self._bloque_calendario(datos.get("acciones_calendario") or [],
                                             hubo_correos=bool(datos.get("total")))
        if bloque_cal:
            bloques.append(bloque_cal)

        boton_marcar = self._boton_marcar_leidos(datos)
        if boton_marcar:
            bloques.append(boton_marcar)

        segundos = datos.get("segundos")
        if segundos:
            bloques.append(
                ft.Text(f"Análisis completado en {segundos} s", size=10,
                        color=paleta["texto_secundario"])
            )
        if hora:
            bloques.append(ft.Text(hora, size=10, color=paleta["texto_secundario"]))

        return ft.Container(
            content=ft.Row(
                [
                    self._avatar_asistente(),
                    ft.Column(bloques, spacing=8, tight=True, width=560),
                ],
                alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.START, spacing=8,
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=4),
        )

    def _cabecera_revision(self, datos):
        """NIVEL 1 — el resumen general de toda la bandeja."""
        paleta = self.paleta
        total = datos.get("total", 0)
        hilos = datos.get("total_hilos", total)

        # Si se pidió un día concreto (N5), no son "sin leer": son los de ese día.
        dia = datos.get("dia_consultado", "")
        if dia:
            from datetime import date as _date
            import analisis_stefany as _analisis
            try:
                titulo = f"Tienes {total} correos del {_analisis.fecha_en_palabras(_date.fromisoformat(dia))}"
            except ValueError:
                titulo = f"Tienes {total} correos"
        else:
            titulo = f"Tienes {total} correos sin leer"
        if hilos != total:
            titulo += f"  ·  {hilos} conversaciones"

        fichas = []
        iconos_senal = {
            "remitente_importante": ("⭐", "de remitentes importantes"),
            "urgente": ("🔴", "urgentes"),
            "requiere_respuesta": ("↩️", "requieren respuesta"),
            "propone_evento": ("📅", "con posible evento"),
            "tiene_fecha_limite": ("⏳", "con fecha límite"),
        }
        for clave, cantidad in datos.get("conteo_senales", {}).items():
            icono, texto = iconos_senal.get(clave, ("•", clave))
            fichas.append(
                ft.Container(
                    content=ft.Text(f"{icono} {cantidad} {texto}", size=11,
                                    color=paleta["texto_asistente"]),
                    bgcolor=paleta["fondo_input"], border_radius=12,
                    padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                )
            )

        contenido = [
            ft.Text("Stefany", size=10, weight=ft.FontWeight.BOLD, color=paleta["texto_secundario"]),
            ft.Text(titulo, size=15, weight=ft.FontWeight.BOLD, color=paleta["texto_asistente"]),
        ]
        if fichas:
            contenido.append(ft.Row(fichas, spacing=6, wrap=True))

        return ft.Container(
            content=ft.Column(contenido, spacing=8, tight=True),
            bgcolor=paleta["burbuja_asistente"], border_radius=14, padding=12,
            border=ft.Border.all(1, paleta["borde_sutil"]),
        )

    # =================================================================
    # RESPUESTA SUGERIDA
    # =================================================================
    def _zona_respuesta(self, correo):
        """Botón "Generar respuesta" y, cuando hay borrador, el editor.

        Flujo: Generar → aparece el borrador EDITABLE → Enviar (con diálogo de
        confirmación) o Reformular.
        """
        paleta = self.paleta

        campo = ft.TextField(
            multiline=True, min_lines=4, max_lines=12, visible=False,
            text_size=12, border_color=paleta["borde_sutil"],
            bgcolor=paleta["fondo_input"], color=paleta["texto_asistente"],
            label="Borrador de respuesta (puedes editarlo)",
        )
        estado = ft.Text("", size=11, color=paleta["texto_secundario"], visible=False)
        # Aviso de cruce con el calendario: el borrador podía afirmar "ese día
        # tengo un espacio libre" con un evento a esa misma hora.
        aviso_agenda = ft.Text("", size=11, color="#f59e0b", visible=False)

        boton_generar = ft.TextButton(
            "✍️ Generar respuesta",
            style=ft.ButtonStyle(color=paleta["accento"]),
        )
        boton_enviar = ft.FilledButton("Enviar respuesta", visible=False)
        boton_reformular = ft.TextButton("Reformular", visible=False)
        boton_borrador = ft.TextButton("Guardar como borrador", visible=False)

        fila_acciones = ft.Row(
            [boton_generar, boton_enviar, boton_reformular, boton_borrador],
            spacing=6, wrap=True,
        )

        def bloquear(valor, texto=""):
            for b in (boton_generar, boton_enviar, boton_reformular, boton_borrador):
                b.disabled = valor
                b.update()
            if texto:
                estado.value = texto
                estado.visible = True
                estado.update()

        def generar(e, instruccion=""):
            bloquear(True, "Redactando la respuesta...")
            self._lanzar_hilo(self._generar_respuesta_hilo, correo, instruccion,
                                 campo, estado, boton_generar, boton_enviar,
                                 boton_reformular, boton_borrador, aviso_agenda)

        def reformular(e):
            generar(e, instruccion="Redáctala de otra forma, cambiando el enfoque.")

        def guardar_borrador(e):
            bloquear(True, "Guardando el borrador en Gmail...")
            self._lanzar_hilo(self._enviar_respuesta_hilo, correo, campo.value,
                                 True, estado, boton_generar, boton_enviar,
                                 boton_reformular, boton_borrador)

        def confirmar_envio(e):
            # Única acción irreversible de la aplicación: se confirma con
            # destinatario y asunto a la vista.
            destinatario = correo.get("remitente", "")
            asunto = correo.get("asunto", "")

            def enviar_de_verdad(ev):
                self.page.pop_dialog()
                bloquear(True, "Enviando...")
                self._lanzar_hilo(self._enviar_respuesta_hilo, correo, campo.value,
                                     False, estado, boton_generar, boton_enviar,
                                     boton_reformular, boton_borrador)

            self.page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text("¿Enviar esta respuesta?"),
                content=ft.Column([
                    ft.Text(f"Para: {destinatario}", size=12,
                            weight=ft.FontWeight.BOLD),
                    ft.Text(f"Asunto: Re: {asunto}", size=12),
                    ft.Text(aviso_agenda.value, size=11, color="#f59e0b",
                            visible=bool(aviso_agenda.value)),
                    ft.Divider(height=12),
                    ft.Text((campo.value or "")[:400], size=11,
                            color=paleta["texto_secundario"]),
                    ft.Divider(height=12),
                    ft.Text("El correo saldrá de tu cuenta y no se puede deshacer.",
                            size=11, color=paleta["texto_secundario"]),
                ], tight=True, spacing=4, width=420, scroll=ft.ScrollMode.AUTO),
                actions=[
                    ft.TextButton("Cancelar", on_click=lambda ev: self.page.pop_dialog()),
                    ft.TextButton("Mejor guardar como borrador",
                                  on_click=lambda ev: (self.page.pop_dialog(),
                                                       guardar_borrador(ev))),
                    ft.FilledButton("Sí, enviar", on_click=enviar_de_verdad),
                ],
            ))

        boton_generar.on_click = generar
        boton_reformular.on_click = reformular
        boton_borrador.on_click = guardar_borrador
        boton_enviar.on_click = confirmar_envio

        return ft.Column([fila_acciones, aviso_agenda, campo, estado], spacing=6, tight=True)

    def _generar_respuesta_hilo(self, correo, instruccion, campo, estado,
                                b_generar, b_enviar, b_reformular, b_borrador,
                                aviso_agenda=None):
        try:
            resultado = router_stefany.sugerir_respuesta(
                correo["cuenta"], correo["id"], instruccion)
        except Exception as ex:
            resultado = {"ok": False, "texto": f"No se pudo generar: {ex}"}

        if aviso_agenda is not None:
            try:
                aviso_agenda.value = resultado.get("aviso_agenda", "") or ""
                aviso_agenda.visible = bool(aviso_agenda.value)
                aviso_agenda.update()
            except Exception:
                pass

        try:
            if resultado.get("ok"):
                campo.value = resultado["texto"]
                campo.visible = True
                campo.update()
                estado.value = "Revisa el borrador antes de enviarlo."
                b_generar.content = "Generar de nuevo"
                for b in (b_enviar, b_reformular, b_borrador):
                    b.visible = True
            else:
                estado.value = f"⚠️ {resultado.get('texto', 'Error')}"
            estado.visible = True
            for b in (b_generar, b_enviar, b_reformular, b_borrador):
                b.disabled = False
                b.update()
            estado.update()
        except Exception:
            pass

    def _enviar_respuesta_hilo(self, correo, texto, solo_borrador, estado,
                               b_generar, b_enviar, b_reformular, b_borrador):
        try:
            resultado = router_stefany.enviar_respuesta(
                correo["cuenta"], correo["id"], texto, solo_borrador=solo_borrador)
        except Exception as ex:
            resultado = {"ok": False, "mensaje": f"Error: {ex}"}

        try:
            estado.value = ("✅ " if resultado.get("ok") else "⚠️ ") + resultado.get("mensaje", "")
            estado.visible = True
            estado.update()
            for b in (b_generar, b_enviar, b_reformular, b_borrador):
                b.disabled = False
                b.update()
        except Exception:
            pass

    # =================================================================
    # ANÁLISIS DE CALENDARIO (bloque final)
    # =================================================================
    def _bloque_calendario(self, acciones, hubo_correos=True):
        """Lo que hay que decidir sobre el calendario, al final de todo.

        Tipos: compromisos sin agendar, cruces de horario, eventos que un
        correo cancela o cambia, y compromisos a los que les falta un dato.
        La comprobación del cruce la hizo el código con aritmética real sobre
        el calendario, no el modelo.

        Sin acciones el bloque NO desaparece: dice que se revisó y no se
        encontró nada (CU1 · A1 y CU4 · A1). Antes no se pintaba nada y no
        había forma de distinguir "todo en orden" de "no lo revisé".
        """
        if not acciones and not hubo_correos:
            return None

        paleta = self.paleta
        filas = [
            ft.Row([ft.Text("📅", size=15),
                    ft.Text("Análisis de tu calendario", size=13,
                            weight=ft.FontWeight.BOLD, color=paleta["texto_asistente"]),
                    ], spacing=8)
        ]
        if not acciones:
            filas.append(
                ft.Container(
                    content=ft.Text(
                        "✅ Contrasté tus correos con el calendario de todas tus cuentas: "
                        "no encontré compromisos nuevos sin agendar ni cruces de horario.",
                        size=12, color=paleta["texto_asistente"], selectable=True),
                    padding=ft.Padding.only(right=14, top=8, bottom=12),
                )
            )
        for accion in acciones:
            filas.append(self._fila_calendario(accion))

        return ft.Container(
            content=ft.Column(filas, spacing=0, tight=True),
            bgcolor=paleta["burbuja_asistente"], border_radius=14,
            padding=ft.Padding.only(left=14, right=0, top=12, bottom=0),
            border=ft.Border.all(1, paleta["borde_sutil"]),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )

    def _campo_fila(self, etiqueta, valor, ancho):
        """Campo de texto de una fila del calendario.

        Los colores van explícitos porque la app no usa un tema oscuro de
        Flet: sin ellos el texto sale oscuro sobre fondo oscuro.
        """
        paleta = self.paleta
        return ft.TextField(
            label=etiqueta, value=valor, dense=True, text_size=12, width=ancho,
            color=paleta["texto_asistente"], bgcolor=paleta["fondo_input"],
            border_color=paleta["borde_sutil"], focused_border_color=paleta["accento"],
            label_style=ft.TextStyle(color=paleta["texto_secundario"]),
            cursor_color=paleta["texto_asistente"])

    def _fila_calendario(self, accion):
        paleta = self.paleta
        estado = ft.Text("", size=11, color=paleta["texto_secundario"], visible=False)

        encabezado = [
            ft.Text({"agendar": "🟢", "cancelar": "🔴", "pendiente": "🕓",
                     "pasado": "🕘", "actualizar": "🔵",
                     "incompleto": "🟡"}.get(accion["tipo"], "🟠"), size=13),
            ft.Text(accion.get("titulo", ""), size=12, weight=ft.FontWeight.BOLD,
                    color=paleta["texto_asistente"], expand=True,
                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
        ]
        contenido = [ft.Row(encabezado, spacing=6)]

        if accion.get("remitente"):
            # La dirección va junto al nombre, igual que en la tarjeta del
            # correo: aquí se decide si agendar algo, y de quién viene importa.
            de = accion["remitente"]
            if accion.get("remitente_email"):
                de += f"  ·  {accion['remitente_email']}"
            contenido.append(ft.Text(f"De: {de}", size=11, selectable=True,
                                     color=paleta["texto_secundario"]))
        # Aquí el espacio es estrecho: "[Universidad]" en lugar del correo, y
        # el correo completo al pasar el cursor.
        explicacion, correos_citados = accion.get("explicacion", ""), []
        for alias in set(re.findall(r'\[([\w.@+-]+@[\w.-]+)\]', explicacion)):
            apodo = memoria_local.apodo_cuenta(alias)
            if apodo:
                explicacion = explicacion.replace(f"[{alias}]", f"[{apodo}]")
                correos_citados.append(f"{apodo}: {alias}")
        contenido.append(ft.Text(explicacion, size=12, color=paleta["texto_asistente"],
                                 selectable=True,
                                 tooltip="\n".join(correos_citados) or None))
        if accion.get("aviso_horario"):
            contenido.append(ft.Text(accion["aviso_horario"], size=11, color="#f59e0b"))
        if accion.get("libre_texto"):
            # Alternativa calculada con tu horario y tu calendario (CU4).
            contenido.append(ft.Text(f"🟢 {accion['libre_texto']}", size=11,
                                     color=paleta["estado_ok"]))

        if accion["tipo"] in ("agendar", "conflicto", "pendiente", "incompleto"):
            # El asunto no siempre sirve de nombre ("Solicitud"): se sugiere
            # uno por reglas y la usuaria puede cambiarlo antes de agendar.
            # Colores explícitos, como el campo de "generar respuesta": la app no
            # usa un tema oscuro de Flet, así que sin ellos el texto salía
            # oscuro sobre el fondo oscuro y no se veía.
            campo_titulo = self._campo_fila(
                "Nombre del evento",
                accion.get("titulo_evento") or accion.get("titulo", ""), 420)

            # CU2 · A3 — lo que el correo no dice lo escribe la usuaria aquí
            # mismo, en vez de tener que abrir el chat o Google Calendar.
            falta = accion.get("falta") or []
            campo_fecha = (self._campo_fila("Día (ej. 25 de septiembre)", "", 260)
                           if "el día" in falta else None)
            campo_hora = (self._campo_fila("Hora (ej. 3:00 p.m.)", "", 180)
                          if "la hora" in falta else None)

            # En un conflicto la explicación ya dice con qué se cruza; el botón
            # deja claro que se agenda igualmente.
            if accion.get("clase") == "renovacion":
                etiqueta_boton = "Renovar un mes más"
            elif accion.get("recurrencia"):
                etiqueta_boton = f"Agendar {accion['recurrencia'].get('texto', '')} (un mes)"
            else:
                etiqueta_boton = {"agendar": "Agendar",
                                  "conflicto": "Agendar de todos modos",
                                  "pendiente": "Ya confirmaron: agendar",
                                  "incompleto": "Completar y agendar"}[accion["tipo"]]
            boton = ft.FilledButton(etiqueta_boton)

            def agendar(e, a=accion, b=boton, st=estado, c=campo_titulo,
                        cf=campo_fecha, ch=campo_hora):
                if b.disabled:
                    return          # Enter en el campo mientras ya se está agendando
                extra = {"titulo_evento": (c.value or "").strip()}
                if cf is not None:
                    extra["fecha_texto"] = (cf.value or "").strip()
                if ch is not None:
                    extra["hora_texto"] = (ch.value or "").strip()
                # Sin el dato que falta no se agenda nada: se dice qué falta.
                vacios = [campo.label for campo in (cf, ch)
                          if campo is not None and not (campo.value or "").strip()]
                if vacios:
                    st.value = "⚠️ Escribe " + " y ".join(vacios).lower() + " para poder agendarlo."
                    st.visible = True
                    try:
                        st.update()
                    except Exception:
                        pass
                    return
                b.disabled = True
                b.content = "Renovando..." if a.get("clase") == "renovacion" else "Agendando..."
                b.update()
                hilo = (self._renovar_pendiente_hilo if a.get("clase") == "renovacion"
                        else self._agendar_hilo)
                self._lanzar_hilo(hilo, dict(a, **extra), b, st)

            boton.on_click = agendar
            campo_titulo.on_submit = agendar
            contenido.append(campo_titulo)
            faltantes = [campo for campo in (campo_fecha, campo_hora) if campo is not None]
            if faltantes:
                for campo in faltantes:
                    campo.on_submit = agendar
                contenido.append(ft.Row(faltantes, spacing=8, wrap=True))

            botones = [boton]
            if accion["tipo"] == "pendiente":
                descartar = ft.TextButton("Descartar")

                def descartar_pendiente(e, a=accion, b=descartar, bo=boton, st=estado):
                    b.disabled = bo.disabled = True
                    b.update()
                    bo.update()
                    self._lanzar_hilo(self._descartar_pendiente_hilo, a, b, st)

                descartar.on_click = descartar_pendiente
                botones.append(descartar)
            contenido.append(ft.Row(botones, spacing=6))
        elif accion["tipo"] == "actualizar" and not accion.get("solo_aviso"):
            # CU3 — modificar un evento existente: siempre con confirmación,
            # mostrando el antes y el después. Puede cambiar el horario, el
            # lugar o los dos; lo que no cambia se conserva.
            # Una casilla por campo, con el valor actual y el propuesto. Si el
            # sistema acierta en uno y se equivoca en otro (lee bien el salón
            # pero mal la fecha), se desmarca ese y se aplica el resto: antes
            # era todo o nada y la única salida era cancelar.
            casillas = {}
            for cambio in (accion.get("cambios") or []):
                casilla = ft.Checkbox(
                    label=f"{cambio['etiqueta']}:  {cambio['antes']}  →  {cambio['despues']}",
                    value=True,
                    label_style=ft.TextStyle(size=12, color=paleta["texto_asistente"]),
                    fill_color=paleta["accento"], check_color="#ffffff",
                )
                casillas[cambio["campo"]] = casilla
                contenido.append(casilla)

            boton = ft.FilledButton("Actualizar en el calendario")

            def pedir_confirmacion_cambio(e, a=accion, b=boton, st=estado, cs=casillas):
                marcados = [campo for campo, casilla in cs.items() if casilla.value]
                if cs and not marcados:
                    st.value = "⚠️ Marca al menos un cambio, o cancela."
                    st.visible = True
                    try:
                        st.update()
                    except Exception:
                        pass
                    return

                def confirmar(ev):
                    self.page.pop_dialog()
                    b.disabled = True
                    b.content = "Actualizando..."
                    b.update()
                    # Sin casillas (fila de una conversación guardada antes de
                    # que existieran) se aplica todo, como se hacía entonces.
                    self._lanzar_hilo(self._actualizar_evento_hilo,
                                      dict(a, aplicar=marcados if cs else None), b, st)

                # El diálogo repite SOLO lo marcado y nombra lo que se queda
                # igual: lo último que se lee antes de tocar el calendario
                # tiene que ser exactamente lo que va a pasar.
                if cs:
                    detalle = [f"· {c['etiqueta']}: {c['antes']} → {c['despues']}"
                               for c in (a.get("cambios") or []) if c["campo"] in marcados]
                    sin_tocar = [c["etiqueta"].lower() for c in (a.get("cambios") or [])
                                 if c["campo"] not in marcados]
                else:
                    detalle, sin_tocar = [], []
                    if a.get("fecha_iso"):
                        detalle.append(f"· Fecha y hora: {a.get('fecha_texto', '')} "
                                       f"a las {a.get('hora_texto', '')}")
                    if a.get("lugar_nuevo"):
                        detalle.append(f"· Lugar: {a.get('lugar_actual') or 'sin lugar'} "
                                       f"→ {a['lugar_nuevo']}")
                aviso = ("\n\nNo se tocará: " + ", ".join(sin_tocar) + ".") if sin_tocar else ""

                self.page.show_dialog(ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Actualizar el evento"),
                    content=ft.Text(
                        f"¿Actualizar '{a.get('evento_existente', '')}' en el calendario de "
                        f"{a.get('cuenta_evento', '')}?\n\n" + "\n".join(detalle)
                        + "\n\nLa duración se conserva." + aviso,
                        size=12,
                    ),
                    actions=[
                        ft.TextButton("Cancelar", on_click=lambda ev: self.page.pop_dialog()),
                        ft.FilledButton("Sí, actualizar", on_click=confirmar),
                    ],
                ))

            boton.on_click = pedir_confirmacion_cambio
            contenido.append(ft.Row([boton], spacing=6))
        elif accion["tipo"] == "cancelar":
            boton = ft.FilledButton("Eliminar del calendario")

            def pedir_confirmacion(e, a=accion, b=boton, st=estado):
                def confirmar(ev):
                    self.page.pop_dialog()
                    b.disabled = True
                    b.content = "Eliminando..."
                    b.update()
                    self._lanzar_hilo(self._cancelar_evento_hilo, a, b, st)

                self.page.show_dialog(ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Eliminar del calendario"),
                    content=ft.Text(
                        f"¿Eliminar '{a.get('evento_existente', '')}' del calendario de "
                        f"{a.get('cuenta_evento', '')}?\n\nSolo se elimina esa sesión; "
                        f"si es un evento recurrente, las demás se conservan.",
                        size=12,
                    ),
                    actions=[
                        ft.TextButton("Cancelar", on_click=lambda ev: self.page.pop_dialog()),
                        ft.Button("Eliminar", bgcolor="#dc2626", color="#ffffff",
                                  on_click=confirmar),
                    ],
                ))

            boton.on_click = pedir_confirmacion
            contenido.append(ft.Row([boton], spacing=6))
        contenido.append(estado)
        return ft.Container(
            content=ft.Column(contenido, spacing=4, tight=True),
            padding=ft.Padding.only(right=14, top=10, bottom=10),
            border=ft.Border.only(top=ft.BorderSide(1, paleta["borde_sutil"])),
        )

    def _agendar_hilo(self, accion, boton, estado):
        try:
            resultado = router_stefany.agendar_desde_correo(
                accion["cuenta"], accion["id"], accion.get("analisis", ""),
                fecha_iso=accion.get("fecha_iso"),
                # La hora de la fila ya viene resuelta por código en todos los
                # casos; el análisis del modelo solo se usa si falta.
                hora_texto=accion.get("hora_texto") or None,
                titulo=accion.get("titulo_evento"),
                id_pendiente=accion.get("id_pendiente"),
                remitente_email=accion.get("remitente_email"),
                recurrencia=accion.get("recurrencia"),
                # CU2 · A3 — el día que faltaba, escrito por la usuaria.
                fecha_texto=accion.get("fecha_texto"),
                lugar=accion.get("lugar_nuevo"))
        except Exception as ex:
            resultado = {"ok": False, "mensaje": f"Error: {ex}"}
        try:
            estado.value = ("✅ " if resultado.get("ok") else "⚠️ ") + resultado.get("mensaje", "")
            estado.visible = True
            estado.update()
            if resultado.get("ok"):
                boton.content = "Agendado"
            else:
                boton.content = "Reintentar"
                boton.disabled = False
            boton.update()
        except Exception:
            pass

    def _renovar_pendiente_hilo(self, accion, boton, estado):
        try:
            resultado = router_stefany.renovar_pendiente(accion.get("id_pendiente"))
        except Exception as ex:
            resultado = {"ok": False, "mensaje": f"Error: {ex}"}
        try:
            estado.value = ("✅ " if resultado.get("ok") else "⚠️ ") + resultado.get("mensaje", "")
            estado.visible = True
            estado.update()
            boton.content = "Renovado" if resultado.get("ok") else "Reintentar"
            boton.disabled = bool(resultado.get("ok"))
            boton.update()
        except Exception:
            pass

    def _descartar_pendiente_hilo(self, accion, boton, estado):
        try:
            resultado = router_stefany.descartar_pendiente(accion.get("id_pendiente"))
        except Exception as ex:
            resultado = {"ok": False, "mensaje": f"Error: {ex}"}
        try:
            estado.value = ("✅ " if resultado.get("ok") else "⚠️ ") + resultado.get("mensaje", "")
            estado.visible = True
            estado.update()
            boton.content = "Descartado"
            boton.update()
        except Exception:
            pass

    def _actualizar_evento_hilo(self, accion, boton, estado):
        # Solo se aplica lo que quedó marcado en la fila. Para un campo
        # desmarcado se manda el valor ACTUAL del evento (fecha y hora van
        # juntas al calendario, así que no basta con omitir una).
        aplicar = accion.get("aplicar")
        if aplicar is None:
            aplicar = ["fecha", "hora", "lugar"]

        fecha_iso = hora_texto = None
        if "fecha" in aplicar or "hora" in aplicar:
            fecha_iso = (accion.get("fecha_iso") if "fecha" in aplicar
                         else accion.get("fecha_actual_iso"))
            hora_texto = (accion.get("hora_texto") if "hora" in aplicar
                          else accion.get("hora_actual_texto"))
        lugar = accion.get("lugar_nuevo") if "lugar" in aplicar else None

        try:
            resultado = router_stefany.actualizar_evento_desde_correo(
                accion["cuenta_evento"], accion["id_evento"], fecha_iso,
                hora_texto, accion.get("duracion_horas", 1.0), lugar=lugar)
        except Exception as ex:
            resultado = {"ok": False, "mensaje": f"Error: {ex}"}
        try:
            estado.value = ("✅ " if resultado.get("ok") else "⚠️ ") + resultado.get("mensaje", "")
            estado.visible = True
            estado.update()
            boton.content = "Actualizado" if resultado.get("ok") else "Reintentar"
            boton.disabled = bool(resultado.get("ok"))
            boton.update()
        except Exception:
            pass

    def _cancelar_evento_hilo(self, accion, boton, estado):
        try:
            resultado = router_stefany.cancelar_evento_desde_correo(
                accion["cuenta_evento"], accion["id_evento"],
                accion.get("evento_existente", ""))
        except Exception as ex:
            resultado = {"ok": False, "mensaje": f"Error: {ex}"}
        try:
            estado.value = ("✅ " if resultado.get("ok") else "⚠️ ") + resultado.get("mensaje", "")
            estado.visible = True
            estado.update()
            if resultado.get("ok"):
                boton.content = "Eliminado"
            else:
                boton.content = "Reintentar"
                boton.disabled = False
            boton.update()
        except Exception:
            pass

    def _boton_marcar_leidos(self, datos):
        """Marcar leídos toca la bandeja real, así que lo decide el usuario.

        Automatizarlo significaría marcar cientos de correos de golpe en la
        primera revisión, y deshacerlo a mano sería un suplicio.
        """
        ids_por_cuenta = datos.get("ids_mostrados") or {}
        total = sum(len(v) for v in ids_por_cuenta.values())
        if not total or datos.get("ya_marcados"):
            return None

        paleta = self.paleta
        etiqueta = ft.Text(f"Marcar como leídos en Gmail ({total})", size=12,
                           weight=ft.FontWeight.BOLD, color=paleta["accento"])
        contenedor = ft.Container(
            content=ft.Row([ft.Icon(ft.Icons.MARK_EMAIL_READ, size=16,
                                    color=paleta["accento"]), etiqueta],
                           spacing=8, tight=True),
            padding=ft.Padding.symmetric(horizontal=12, vertical=9),
            border_radius=10, border=ft.Border.all(1, paleta["borde_sutil"]),
            ink=True,
        )

        def al_pulsar(e):
            if getattr(contenedor, "_ocupado", False):
                return
            contenedor._ocupado = True
            etiqueta.value = "Marcando..."
            etiqueta.color = self.paleta["texto_secundario"]
            etiqueta.update()
            self._lanzar_hilo(self._marcar_leidos_hilo, ids_por_cuenta,
                                 etiqueta, contenedor)

        contenedor.on_click = al_pulsar
        return contenedor

    def _marcar_leidos_hilo(self, ids_por_cuenta, etiqueta, contenedor):
        try:
            marcados, errores = router_stefany.marcar_revision_como_leida(ids_por_cuenta)
        except Exception as ex:
            marcados, errores = 0, [str(ex)]
        try:
            if errores:
                etiqueta.value = f"No se pudo: {errores[0][:160]}"
                etiqueta.size = 11
            else:
                etiqueta.value = f"✓ {marcados} marcados como leídos"
            etiqueta.update()
        except Exception:
            pass
        contenedor._ocupado = False

    def _bloque_destacados(self, destacados):
        """NIVEL 1.5 — lo que hay que ver sin abrir nada.

        Con todos los acordeones cerrados, esta es la única parte siempre
        visible. Por eso va corta y ordenada por urgencia real.
        """
        paleta = self.paleta
        filas = [
            ft.Row(
                [
                    ft.Text("⭐", size=14),
                    ft.Text("Necesitan tu atención", size=13, weight=ft.FontWeight.BOLD,
                            color=paleta["texto_asistente"], expand=True),
                ],
                spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        ]
        for correo in destacados:
            filas.append(self._fila_correo(correo, mostrar_categoria=True))

        return ft.Container(
            content=ft.Column(filas, spacing=0, tight=True),
            bgcolor=paleta["burbuja_asistente"], border_radius=14,
            padding=ft.Padding.only(left=14, right=0, top=12, bottom=0),
            border=ft.Border.all(2, paleta["accento"]),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )

    def _resumir_correo_hilo(self, correo, texto_resumen, enlace, contenedor):
        """Pide al modelo el resumen de UN correo concreto, en segundo plano."""
        try:
            datos = router_stefany.resumir_correo(correo["cuenta"], correo["id"])
            resumen = datos.get("resumen", "")
        except ClienteStefanyError as ex:
            resumen = f"⚠️ {ex}"
        except Exception as ex:
            resumen = f"⚠️ No se pudo generar el resumen: {ex}"

        try:
            if resumen:
                texto_resumen.value = resumen
                texto_resumen.visible = True
                texto_resumen.update()
            enlace.value = ""
            enlace.update()
        except Exception:
            pass
        contenedor._ocupado = False

    def _acordeon_categoria(self, grupo):
        """NIVEL 2 — una categoría plegable con sus correos dentro."""
        paleta = self.paleta
        estado = {"abierto": grupo.get("desplegado", False),
                  "mostrados": self.LIMITE_PLEGADO}

        contenedor_correos = ft.Column(spacing=0, tight=True, visible=estado["abierto"])
        flecha = ft.Icon(
            ft.Icons.EXPAND_MORE if estado["abierto"] else ft.Icons.CHEVRON_RIGHT,
            size=18, color=paleta["texto_secundario"],
        )

        def pintar_correos():
            contenedor_correos.controls.clear()
            # Síntesis de la categoría: una frase antes de la lista, para
            # saber qué hay dentro sin leer correo por correo.
            sintesis = grupo.get("sintesis")
            if sintesis:
                contenedor_correos.controls.append(
                    ft.Container(
                        content=ft.Text(sintesis, size=12, italic=True,
                                        color=paleta["texto_asistente"]),
                        bgcolor=paleta["fondo_input"],
                        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                    )
                )
            correos = grupo.get("correos", [])
            # Las categorías importantes se muestran completas al abrirlas; el
            # resto, por tandas, para no enterrar lo que sí importa. Todos los
            # acordeones arrancan cerrados, así que el criterio es 'importante'.
            limite = len(correos) if grupo.get("importante") else estado["mostrados"]
            for correo in correos[:limite]:
                contenedor_correos.controls.append(self._fila_correo(correo))
            restantes = len(correos) - limite
            if restantes > 0:
                contenedor_correos.controls.append(
                    ft.Container(
                        content=ft.Text(f"Ver los {restantes} restantes", size=12,
                                        weight=ft.FontWeight.BOLD, color=paleta["accento"]),
                        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                        on_click=lambda e: ver_mas(), ink=True,
                    )
                )

        def ver_mas():
            estado["mostrados"] += self.LIMITE_PLEGADO
            pintar_correos()
            contenedor_correos.update()

        def alternar(e):
            estado["abierto"] = not estado["abierto"]
            contenedor_correos.visible = estado["abierto"]
            flecha.icon = ft.Icons.EXPAND_MORE if estado["abierto"] else ft.Icons.CHEVRON_RIGHT
            flecha.update()
            contenedor_correos.update()

        pintar_correos()

        cabecera = ft.Container(
            content=ft.Row(
                [
                    flecha,
                    ft.Text(grupo["icono"], size=15),
                    ft.Text(grupo["etiqueta"], size=13, weight=ft.FontWeight.BOLD,
                            color=paleta["texto_asistente"], expand=True),
                    ft.Container(
                        content=ft.Text(str(grupo["cantidad"]), size=11,
                                        weight=ft.FontWeight.BOLD, color=paleta["texto_secundario"]),
                        bgcolor=paleta["fondo_input"], border_radius=10,
                        padding=ft.Padding.symmetric(horizontal=9, vertical=3),
                    ),
                ],
                spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=11),
            on_click=alternar, ink=True,
        )

        return ft.Container(
            content=ft.Column([cabecera, contenedor_correos], spacing=0, tight=True),
            bgcolor=paleta["burbuja_asistente"], border_radius=14,
            border=ft.Border.all(1, paleta["borde_sutil"]),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )

    def _fila_correo(self, correo, mostrar_categoria=False):
        """NIVEL 3 — el correo individual con su resumen."""
        paleta = self.paleta

        nombre_remitente = correo.get("remitente", "")
        correo_remitente = correo.get("remitente_email", "")
        encabezado = [
            ft.Text(nombre_remitente[:34], size=12, weight=ft.FontWeight.BOLD,
                    color=paleta["texto_asistente"], no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS, expand=True,
                    # El nombre se recorta a 34 caracteres: al pasar el cursor
                    # se ve entero, junto con la dirección.
                    tooltip=(f"{nombre_remitente} <{correo_remitente}>"
                             if correo_remitente else nombre_remitente) or None),
        ]
        if correo.get("hilo_n", 1) > 1:
            encabezado.append(
                ft.Text(f"💬 {correo['hilo_n']}", size=10, color=paleta["texto_secundario"])
            )
        for senal in correo.get("senales", []):
            encabezado.append(
                ft.Container(
                    content=ft.Text(f"{senal['icono']} {senal['etiqueta']}", size=9,
                                    color=paleta["texto_asistente"]),
                    bgcolor=paleta["fondo_input"], border_radius=8,
                    padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                )
            )

        contenido = [
            ft.Row(encabezado, spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ]
        # La dirección real, debajo del nombre y seleccionable para copiarla.
        # Saber si algo llegó de "notificaciones@banco.com" o de una persona
        # cambia la lectura del correo, y el nombre para mostrar lo escribe
        # quien envía: puede decir cualquier cosa. También resuelve los casos
        # en que el nombre llega ilegible ("Extensi?n Ingenieria") pero
        # la dirección se lee perfectamente.
        if correo_remitente and correo_remitente.lower() != nombre_remitente.lower():
            contenido.append(
                ft.Text(f"✉️ {correo_remitente}", size=10, selectable=True,
                        color=paleta["texto_secundario"], no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS)
            )
        contenido.append(
            ft.Text(correo.get("asunto", ""), size=12, color=paleta["texto_asistente"],
                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)
        )
        texto_resumen = ft.Text(
            correo.get("resumen", ""), size=12, color=paleta["texto_secundario"],
            selectable=True, visible=bool(correo.get("resumen")),
        )
        contenido.append(texto_resumen)

        # La tarjeta muestra SOLO el resumen. El análisis del modelo (con su
        # "¿Te gustaría que lo agende?") ya no se enseña aquí: lo que haya que
        # decidir sobre el calendario está en el bloque "Análisis de tu
        # calendario", con la fecha y la cuenta verificadas por código.

        # El resumen del modelo solo se generó para los correos que lo
        # ameritaban. Para el resto, este enlace lo pide a demanda: así el
        # usuario puede profundizar en cualquier correo sin que la revisión
        # completa haya gastado tokens en los 480 de la bandeja.
        # Si el correo no tenía texto pero sí imágenes, lo útil no es pedir
        # otro resumen del vacío: es leer las imágenes.
        # El enlace aparece SIEMPRE que haya imágenes con contenido, no solo
        # cuando el cuerpo está vacío: el caso real es un correo con texto de
        # presentación cuyos datos (día, hora, enlace) están en la imagen.
        n_img = (correo.get("n_adjuntas", 0) or 0) + (correo.get("n_remotas", 0) or 0)
        tiene_imagenes = bool(n_img and correo.get("id") and correo.get("cuenta"))
        if tiene_imagenes:
            # Aquí se pintan las acciones de calendario que aparezcan DESPUÉS
            # de leer la imagen (el compromiso estaba dentro del afiche).
            # Nace VISIBLE aunque vacío: un Column vacío no ocupa nada,
            # pero uno que nace oculto dentro de una tarjeta ya renderizada no
            # se repintaba al rellenarlo, y la fila para agendar no aparecía.
            zona_calendario = ft.Column(spacing=0, tight=True)
            contenido.append(self._enlace_leer_imagenes(correo, texto_resumen, zona_calendario))
            contenido.append(zona_calendario)

        # Antes esto era inalcanzable para los correos con imágenes, porque la
        # rama de arriba devolvía la fila ya terminada: un correo con imagen
        # que pedía respuesta se quedaba sin el botón de responder.
        if ("requiere_respuesta" in (correo.get("claves_senal") or [])
                and correo.get("id") and correo.get("cuenta")):
            contenido.append(self._zona_respuesta(correo))

        if tiene_imagenes:
            # Con imágenes, lo útil es leerlas (enlace de arriba), no pedir
            # otro resumen del mismo texto.
            return self._envolver_fila(contenido, mostrar_categoria)

        ya_tiene_resumen_llm = correo.get("origen") in ("modelo", "cache")
        if not ya_tiene_resumen_llm and correo.get("id") and correo.get("cuenta"):
            enlace = ft.Text(
                "Ver resumen", size=11, weight=ft.FontWeight.BOLD,
                color=paleta["accento"],
            )
            contenedor_enlace = ft.Container(
                content=enlace, on_click=None, ink=False,
                padding=ft.Padding.only(top=2, bottom=2),
            )

            def pedir_resumen(e, c=correo, txt=texto_resumen,
                              lnk=enlace, cont=contenedor_enlace):
                if getattr(cont, "_ocupado", False):
                    return
                cont._ocupado = True
                lnk.value = "Generando resumen..."
                lnk.color = self.paleta["texto_secundario"]
                lnk.update()
                self._lanzar_hilo(self._resumir_correo_hilo, c, txt, lnk, cont)

            contenedor_enlace.on_click = pedir_resumen
            contenedor_enlace.ink = True
            contenido.append(contenedor_enlace)
        # La cuenta de destino tiene que verse: con varias cuentas conectadas,
        # saber si algo llegó a la universitaria o a la personal cambia lo
        # urgente que es. El dato ya venía en la estructura y no se pintaba.
        partes_pie = []
        if mostrar_categoria and correo.get("categoria"):
            partes_pie.append(correo["categoria"])
        if correo.get("cuenta"):
            # "📥 Universidad · usuario@uan.edu.co": el apodo acompaña al
            # correo, no lo sustituye.
            partes_pie.append(f"📥 {memoria_local.etiqueta_cuenta(correo['cuenta'])}")
        if correo.get("fecha"):
            partes_pie.append(correo["fecha"])
        contenido.append(
            ft.Text("  ·  ".join(partes_pie), size=10,
                    color=paleta["texto_secundario"], no_wrap=False)
        )

        return self._envolver_fila(contenido, mostrar_categoria)

    def _envolver_fila(self, contenido, mostrar_categoria):
        return ft.Container(
            content=ft.Column(contenido, spacing=4, tight=True),
            padding=ft.Padding.only(
                left=0 if mostrar_categoria else 34, right=14, top=8, bottom=10),
            border=ft.Border.only(top=ft.BorderSide(1, self.paleta["borde_sutil"])),
        )

    def _enlace_leer_imagenes(self, correo, texto_resumen, zona_calendario=None):
        """Enlace para leer con OCR las imágenes de un correo.

        Si hay imágenes REMOTAS se pide confirmación antes, porque
        descargarlas le avisa al remitente de que el correo fue abierto.
        """
        paleta = self.paleta
        n_adj = correo.get("n_adjuntas", 0) or 0
        n_rem = correo.get("n_remotas", 0) or 0
        total = n_adj + n_rem

        plural = "las" if total > 1 else "la"
        imagen = "imágenes" if total > 1 else "imagen"
        texto_enlace = (f"🖼️ Leer {plural} {total} {imagen} de este correo"
                        if correo.get("vacio") else
                        f"🖼️ Este correo trae {total} {imagen}: "
                        f"leer lo que {'dicen' if total > 1 else 'dice'}")
        enlace = ft.Text(texto_enlace, size=11,
                         weight=ft.FontWeight.BOLD, color=paleta["accento"])
        contenedor = ft.Container(content=enlace, ink=True,
                                  padding=ft.Padding.only(top=4, bottom=2))

        def lanzar(incluir_remotas):
            contenedor._ocupado = True
            enlace.value = "Leyendo imágenes..."
            enlace.color = self.paleta["texto_secundario"]
            enlace.update()
            self._lanzar_hilo(self._leer_imagenes_hilo, correo, incluir_remotas,
                                 texto_resumen, enlace, contenedor, zona_calendario)

        def al_pulsar(e):
            if getattr(contenedor, "_ocupado", False):
                return
            if n_rem == 0:
                lanzar(False)      # solo adjuntas: no avisa a nadie
                return

            def aceptar(ev):
                self.page.pop_dialog()
                lanzar(True)

            def solo_adjuntas(ev):
                self.page.pop_dialog()
                lanzar(False)

            acciones = [ft.TextButton("Cancelar", on_click=lambda ev: self.page.pop_dialog())]
            if n_adj:
                acciones.append(ft.TextButton(f"Solo las {n_adj} adjuntas",
                                              on_click=solo_adjuntas))
            acciones.append(ft.FilledButton("Descargar y leer", on_click=aceptar))

            self.page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text("Leer las imágenes"),
                content=ft.Text(
                    f"Este correo tiene {n_rem} imágenes alojadas en el servidor "
                    f"de quien lo envió. Descargarlas le confirma que abriste el "
                    f"correo.\n\n¿Quieres continuar?",
                    size=12,
                ),
                actions=acciones,
            ))

        contenedor.on_click = al_pulsar
        return contenedor

    def _leer_imagenes_hilo(self, correo, incluir_remotas, texto_resumen,
                            enlace, contenedor, zona_calendario=None):
        def progreso(t):
            try:
                enlace.value = t
                enlace.update()
                # El OCR tarda segundos: sin despertar el bucle, "Leyendo 3
                # imágenes..." no se vería hasta el final.
                self._despertar_interfaz()
            except Exception:
                pass
        try:
            datos = router_stefany.leer_imagenes_correo(
                correo["cuenta"], correo["id"],
                incluir_remotas=incluir_remotas, progreso=progreso)
            resumen = datos.get("resumen", "")
            leidas = datos.get("imagenes_leidas", 0)
            acciones = datos.get("acciones_calendario") or []
        except Exception as ex:
            resumen, leidas, acciones = f"⚠️ No se pudieron leer las imágenes: {ex}", 0, []

        # Si dentro de la imagen había un compromiso, se ofrece agendarlo aquí
        # mismo: el bloque del calendario ya se pintó antes de leerla.
        # Antes esto fallaba en silencio: el único aviso era un print a
        # una consola que en el .exe no existe. Ahora el fallo se cuenta en la
        # propia tarjeta, y el repintado tiene una alternativa.
        fallo = ""
        if zona_calendario is not None and acciones:
            try:
                nuevas = [ft.Text("📅 Encontrado en la imagen", size=11,
                                  weight=ft.FontWeight.BOLD,
                                  color=self.paleta["texto_asistente"])]
                for accion in acciones:
                    nuevas.append(self._fila_calendario(accion))
                zona_calendario.controls.clear()
                zona_calendario.controls.extend(nuevas)
                zona_calendario.visible = True
            except Exception as ex:
                fallo = f"⚠️ No pude preparar la fila del calendario: {ex}"

            if not fallo:
                repintado = False
                try:
                    zona_calendario.update()
                    repintado = True
                except Exception as ex:
                    fallo = f"⚠️ No pude repintar el calendario: {ex}"
                # Segunda vía: forzar el envío de todo lo pendiente de la
                # página (mismo recurso que con la burbuja de progreso).
                try:
                    if self.page:
                        self.page.update()
                        repintado = True
                except Exception as ex:
                    if not repintado:
                        fallo = f"⚠️ No pude repintar la ventana: {ex}"
                if repintado:
                    fallo = ""
        if fallo:
            # En una consola cp1252 el emoji hace saltar UnicodeEncodeError, y
            # en el .exe empaquetado no hay consola: el print no puede tumbar
            # el hilo justo cuando estamos informando de un fallo.
            try:
                print(fallo)
            except Exception:
                pass

        try:
            if resumen:
                texto_resumen.value = f"{resumen}\n{fallo}".strip() if fallo else resumen
                texto_resumen.visible = True
                texto_resumen.update()
            enlace.value = f"✓ {leidas} imágenes leídas" if leidas else ""
            enlace.color = self.paleta["texto_secundario"]
            enlace.update()
        except Exception:
            pass
        contenedor._ocupado = False

    # =================================================================
    # CONEXIÓN CON EL SERVIDOR (Modal Labs - Automático)
    # =================================================================
    # =================================================================
    # CONEXIÓN CON EL SERVIDOR (Modal Labs - Automático)
    # =================================================================
    def _bloquear_chat(self, bloqueado, motivo=""):
        """Sin servidor no se puede responder: se bloquea la entrada.

        Antes se podía escribir y enviar mientras la barra decía "Conectando",
        y el mensaje fallaba. Ahora el campo se desactiva y dice por qué.
        """
        self.campo_mensaje.disabled = bloqueado
        self.boton_enviar.disabled = bloqueado
        self.campo_mensaje.hint_text = motivo or "Escribe tu mensaje a Stefany..."
        for control in (self.campo_mensaje, self.boton_enviar):
            try:
                control.update()
            except Exception:
                pass

    def _conectar_automatico(self, reintentos=True):
        import time
        while True:
            if self._intentar_conexion():
                return
            if not reintentos:
                return
            # Bucle, no llamada recursiva: una caída larga del servidor
            # acabaría desbordando la pila.
            time.sleep(8)

    def _intentar_conexion(self):
        cliente = ClienteStefany(URL_SERVIDOR_MODAL, CLAVE_API_STEFANY)
        info = cliente.estado()
        ok = info is not None

        # Servidor vivo pero clave rechazada: reintentar no arregla nada, así
        # que se dice y se para. El valor por defecto es True para no romper
        # contra un servidor desplegado ANTES de que /estado informara de esto.
        if ok and info.get("clave_ok", True) is False:
            self.pill_estado.value = "●  Clave de API no válida"
            self.pill_estado.color = self.paleta["estado_error"]
            self.pill_estado.tooltip = (
                "El servidor responde, pero no acepta la clave de esta copia de la "
                "aplicación. Hay que volver a desplegar el servidor con la misma clave.")
            self._bloquear_chat(True, "El servidor no acepta la clave de esta aplicación.")
            try:
                self.pill_estado.update()
            except Exception:
                pass
            return True      # no tiene sentido reintentar cada 8 s

        if ok:
            self.cliente = cliente
            prompts.configurar_cliente(cliente)
            # El servicio y el MODELO son dos cosas distintas: el primero
            # responde siempre (función de CPU), el segundo puede estar
            # apagado. Antes la barra decía "Conectado" en los dos casos y la
            # primera consulta se quedaba ~1 minuto sin ninguna explicación.
            caliente = bool(info.get("modelo_caliente"))
            self._pintar_estado_servidor(caliente)
            # Precalentar la GPU nada más conectar. Ya estamos en un hilo
            # aparte y el servidor responde al instante, así que no bloquea.
            # Cuesta ~5 min de T4 por apertura aunque no se use la app
            # (scaledown_window=300), a cambio de quitar los ~24 s de espera
            # de la primera consulta.
            self._bloquear_chat(False)
            cliente.calentar()
            if not caliente:
                self._lanzar_hilo(self._vigilar_calentamiento)
            return True

        self.pill_estado.value = "●  Desconectado — reintentando..."
        self.pill_estado.color = self.paleta["estado_error"]
        self.pill_estado.tooltip = None
        self._bloquear_chat(True, "Esperando al servidor para poder responderte...")
        try:
            self.pill_estado.update()
        except Exception:
            pass
        return False

    def _pintar_estado_servidor(self, caliente):
        """Barra superior: distingue el servicio del modelo."""
        if caliente:
            self.pill_estado.value = "●  Conectado"
            self.pill_estado.color = self.paleta["estado_ok"]
            self.pill_estado.tooltip = "El modelo está cargado y responde al instante."
        else:
            self.pill_estado.value = "●  Conectado — preparando el modelo..."
            self.pill_estado.color = "#f59e0b"
            self.pill_estado.tooltip = (
                "Ya puedes escribir. El modelo tarda alrededor de un minuto en "
                "cargarse la primera vez, así que esa consulta irá más lenta.")
        try:
            self.pill_estado.update()
        except Exception:
            pass

    def _vigilar_calentamiento(self):
        """Espera a que la GPU termine de cargar el modelo y lo refleja arriba.

        El precalentado (B3) tarda ~60 s. Sin esto, el aviso "preparando el
        modelo" se quedaría puesto para siempre aunque ya estuviera listo.
        Consulta /estado, que es una función de CPU: no enciende nada.
        """
        import time
        for _ in range(18):              # hasta unos 3 minutos
            time.sleep(10)
            if not self.cliente:
                return
            info = self.cliente.estado()
            if info and info.get("modelo_caliente"):
                self._pintar_estado_servidor(True)
                self._despertar_interfaz()
                return
        # Si pasados los 3 minutos sigue frío, no se cambia el aviso: es
        # preferible dejarlo puesto que afirmar que está listo sin saberlo.

    def _reintentar_conexion(self):
        """Intenta reconectar automáticamente en segundo plano (por ejemplo si el
        usuario intenta enviar un mensaje mientras la app sigue desconectada)."""
        self.pill_estado.value = "●  Conectando..."
        self.pill_estado.color = "#f59e0b"
        self.pill_estado.update()
        self._lanzar_hilo(self._conectar_automatico)

    # =================================================================
    # CUENTAS DE GOOGLE (OAuth local)
    # =================================================================
    def _abrir_ventana_cuentas(self, e=None):
        paleta = self.paleta
        self.lista_cuentas_col = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, height=260)
        self.label_estado_cuentas = ft.Text("", size=11)
        # El tope se dice SIEMPRE, no solo al llegar a él: si solo aparece
        # cuando ya no se puede conectar, el usuario descubre el límite
        # justo en el momento en que le estorba.
        self.label_tope_cuentas = ft.Text("", size=11, weight=ft.FontWeight.BOLD,
                                          color=paleta["texto_secundario"])
        self.boton_conectar_cuenta = ft.Button(
            "＋ Conectar cuenta de Google", bgcolor=paleta["accento"], color="#ffffff",
            on_click=self._conectar_cuenta_google,
        )

        self.dlg_cuentas = ft.AlertDialog(
            modal=True,
            # El diálogo es modal: sin una X explícita no hay forma de salir
            # si el usuario solo quería mirar qué cuentas tiene conectadas.
            title=ft.Row(
                [
                    ft.Text("Cuentas de Google", expand=True),
                    ft.IconButton(
                        icon=ft.Icons.CLOSE, icon_size=18,
                        tooltip="Cerrar",
                        on_click=lambda e: self.page.pop_dialog(),
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            content=ft.Column(
                [
                    ft.Text(
                        "Stefany podrá leer y gestionar el correo y calendario de estas cuentas.",
                        size=11, color=paleta["texto_secundario"],
                    ),
                    self.label_tope_cuentas,
                    self.lista_cuentas_col, self.label_estado_cuentas,
                ],
                tight=True, spacing=10, width=380,
            ),
            actions=[
                ft.TextButton("Cerrar", on_click=lambda e: self.page.pop_dialog()),
                self.boton_conectar_cuenta,
            ],
        )
        self.page.show_dialog(self.dlg_cuentas)
        self._refrescar_lista_cuentas()

    def _refrescar_lista_cuentas(self):
        paleta = self.paleta
        self.lista_cuentas_col.controls.clear()
        try:
            cuentas = oauth.cargar_cuentas_guardadas()
        except Exception as ex:
            self.label_estado_cuentas.value = f"⚠️ {ex}"
            self.label_estado_cuentas.color = "#ef4444"
            self.label_estado_cuentas.update()
            return

        if not cuentas:
            self.lista_cuentas_col.controls.append(
                ft.Text("Todavía no hay ninguna cuenta conectada.", size=12, color=paleta["texto_secundario"])
            )
        else:
            for alias in cuentas:
                self.lista_cuentas_col.controls.append(self._fila_cuenta(alias))

        # El contador se ve siempre; el botón se apaga al llegar al tope, en
        # vez de dejar pulsar y que el error salga tras abrir el navegador.
        lleno = len(cuentas) >= oauth.MAXIMO_CUENTAS
        self.label_tope_cuentas.value = (
            f"{len(cuentas)} de {oauth.MAXIMO_CUENTAS} cuentas conectadas"
            + ("  ·  máximo alcanzado" if lleno else ""))
        self.label_tope_cuentas.color = ("#f59e0b" if lleno else paleta["texto_secundario"])
        self.boton_conectar_cuenta.disabled = lleno
        if lleno:
            self.label_estado_cuentas.value = (
                f"Desconecta una cuenta para poder añadir otra. El tope existe porque "
                f"el calendario de todas tus cuentas viaja en cada consulta al modelo.")
            self.label_estado_cuentas.color = paleta["texto_secundario"]
        try:
            self.label_tope_cuentas.update()
            self.boton_conectar_cuenta.update()
            self.label_estado_cuentas.update()
        except Exception:
            pass
        self.lista_cuentas_col.update()

    def _fila_cuenta(self, alias):
        paleta = self.paleta
        return ft.Container(
            content=ft.Row(
                [
                    ft.CircleAvatar(content=ft.Text(alias[0].upper(), color="#ffffff"), bgcolor=paleta["avatar_asistente"]),
                    ft.Text(memoria_local.etiqueta_cuenta(alias), size=13,
                            color=paleta["texto_asistente"], expand=True),
                    ft.OutlinedButton("Desconectar", on_click=lambda e, a=alias: self._desconectar_cuenta(a)),
                ],
                alignment=ft.MainAxisAlignment.START, vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=10,
            ),
            bgcolor=paleta["fondo_input"], border_radius=10, padding=10,
        )

    def _conectar_cuenta_google(self, e):
        self.boton_conectar_cuenta.disabled = True
        self.boton_conectar_cuenta.content = "Revisa tu navegador..."
        self.label_estado_cuentas.value = "Se abrió una pestaña en tu navegador -- inicia sesión y acepta los permisos."
        self.label_estado_cuentas.color = self.paleta["texto_secundario"]
        self.boton_conectar_cuenta.update()
        self.label_estado_cuentas.update()
        self._lanzar_hilo(self._conectar_cuenta_hilo)

    def _conectar_cuenta_hilo(self):
        try:
            alias, _ = oauth.conectar_cuenta_google()
            self.label_estado_cuentas.value = f"✅ {alias} conectada."
            self.label_estado_cuentas.color = self.paleta["estado_ok"]
            gc.actualizar_cuentas_conectadas(oauth.construir_cuentas_conectadas())
            self._refrescar_lista_cuentas()
        except oauth.OAuthStefanyError as ex:
            self.label_estado_cuentas.value = f"⚠️ {ex}"
            self.label_estado_cuentas.color = "#ef4444"
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self.label_estado_cuentas.value = f"⚠️ No se pudo completar la conexión ({type(ex).__name__}). Revisa la consola."
            self.label_estado_cuentas.color = "#ef4444"

        self.boton_conectar_cuenta.disabled = False
        self.boton_conectar_cuenta.content = "＋ Conectar cuenta de Google"
        self.boton_conectar_cuenta.update()
        self.label_estado_cuentas.update()

    def _desconectar_cuenta(self, alias):
        oauth.eliminar_cuenta(alias)
        gc.actualizar_cuentas_conectadas(oauth.construir_cuentas_conectadas())
        self._refrescar_lista_cuentas()

    # =================================================================
    # TEMA CLARO/OSCURO
    # =================================================================
    def _alternar_tema(self, e):
        nuevo = "light" if self.tema_actual == "dark" else "dark"
        self.tema_actual = nuevo
        self.paleta = paleta = PALETAS[nuevo]

        self.page.bgcolor = paleta["fondo_app"]
        self.panel_izq.bgcolor = paleta["fondo_sidebar"]
        self.label_conversaciones.color = paleta["texto_secundario"]
        self.boton_nueva.bgcolor = paleta["accento"]
        self.boton_cuentas.bgcolor = paleta["boton_cuentas"]
        self.icono_boton_cuentas.color = paleta["accento"]
        self.texto_boton_cuentas.color = paleta["accento"]
        self.boton_preferencias.bgcolor = paleta["boton_cuentas"]
        self.icono_boton_preferencias.color = paleta["accento"]
        self.texto_boton_preferencias.color = paleta["accento"]
        self.boton_pendientes.bgcolor = paleta["boton_cuentas"]
        self.icono_boton_pendientes.color = paleta["accento"]
        self.texto_boton_pendientes.color = paleta["accento"]

        self.barra_superior.bgcolor = paleta["fondo_topbar"]
        self.boton_tema.icon = ft.Icons.LIGHT_MODE_OUTLINED if nuevo == "dark" else ft.Icons.DARK_MODE_OUTLINED
        self.boton_tema.icon_color = paleta["texto_asistente"]

        self.area_mensajes.bgcolor = paleta["fondo_area"]
        self.barra_inferior.bgcolor = paleta["fondo_area"]
        self.caja_input.bgcolor = paleta["fondo_input"]
        self.caja_input.border = ft.Border.all(2, paleta["borde_input"])
        self.campo_mensaje.color = paleta["texto_input"]
        self.campo_mensaje.hint_style = ft.TextStyle(color=paleta["texto_secundario"])
        self.boton_enviar.bgcolor = paleta["accento"]

        self._refrescar_lista_conversaciones()

        # _renderizar_mensajes() repinta desde los mensajes guardados, así que
        # borraría la burbuja de progreso si hay un análisis en curso.
        habia_progreso = getattr(self, "burbuja_progreso", None) is not None
        self.burbuja_progreso = None
        self._renderizar_mensajes()
        if habia_progreso:
            self._mostrar_progreso(reiniciar=False)

        self.page.update()


def main(page: ft.Page):
    AppStefany(page)


if __name__ == "__main__":
    # Empaquetada como .exe, estas tres cosas evitan los fallos que solo
    # aparecen en un equipo o perfil nuevo (ver rutas_stefany.py).
    rutas.migrar_datos_antiguos()
    rutas.limpiar_extraccion_flet()

    if not rutas.es_unica_instancia():
        # Se avisa con una ventana, no solo por consola: empaquetada con
        # console=False no hay dónde imprimir, y la app se cerraba en
        # silencio sin que el usuario supiera por qué.
        print("⚠️ Stefany ya está abierta.")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "Stefany ya está abierta en esta sesión.\n\n"
                "Busca la ventana en la barra de tareas.",
                "Stefany", 0x40)
        except Exception:
            pass
        sys.exit(0)

    try:
        ft.run(main)
    except Exception as error_arranque:
        # El WinError 183 al extraer el cliente de Flet se resuelve solo
        # borrando la extracción a medias y volviendo a intentarlo.
        if "183" in str(error_arranque) or "ya existe" in str(error_arranque):
            print("⚠️ Extracción de Flet incompleta; limpiando y reintentando...")
            import shutil as _sh
            _sh.rmtree(os.path.join(os.path.expanduser("~"), ".flet"),
                       ignore_errors=True)
            ft.run(main)
        else:
            raise