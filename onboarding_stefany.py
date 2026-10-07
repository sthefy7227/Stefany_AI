# -*- coding: utf-8 -*-
"""
onboarding_stefany.py — Asistente de primer inicio.

Primero se conectan las cuentas de Google; después, las preguntas de
personalización. Todo son formularios: el modelo NO interviene. Cada
respuesta se guarda en el perfil (memoria_local) en cuanto se avanza, así que
"Dejar para después" conserva lo que ya se contestó.

La misma clase sirve para "Mis preferencias": abrir el asistente desde
un paso concreto con el perfil ya relleno.
"""

import re
from collections import Counter

import flet as ft

import memoria_local
import oauth_stefany as oauth
import gmail_calendar_stefany as gc
import clasificador_stefany as clasificador


PASOS = ["cuentas", "nombre", "tus_cuentas", "importantes", "remitentes",
         "horario", "reuniones", "duracion", "listo"]

CATEGORIAS = [
    ("personal",      "👤 Personas importantes para mí"),
    ("laboral",       "💼 Trabajo"),
    ("academico",     "🎓 Universidad / estudios"),
    ("invitaciones",  "📅 Invitaciones y reuniones"),
    ("transaccional", "💰 Facturas y asuntos financieros"),
    ("otros",         "⭐ Otros"),
]

# "Académico" en vez de "Universidad": abarca colegio, posgrado, cursos y
# diplomados, no solo el pregrado.
APODOS_SUGERIDOS = ["Personal", "Académico", "Trabajo"]

DIAS_CORTOS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
OPCIONES_DIAS = {"lv": [0, 1, 2, 3, 4], "ls": [0, 1, 2, 3, 4, 5]}
OPCIONES_HORARIO = {"07:00-16:00": "7:00 a. m. – 4:00 p. m.",
                    "08:00-17:00": "8:00 a. m. – 5:00 p. m.",
                    "09:00-18:00": "9:00 a. m. – 6:00 p. m."}

FRANJAS = [("manana", "🌅 Mañana"), ("tarde", "🌇 Tarde"),
           ("indiferente", "⚖️ Me es indiferente"), ("depende", "🕐 Depende del día")]
DURACIONES = [("15", "15 minutos"), ("30", "30 minutos"), ("45", "45 minutos"),
              ("60", "1 hora"), ("depende", "Depende del tipo de reunión")]

PATRON_REMITENTE = re.compile(r'^(?:[\w.+-]+@[\w-]+(?:\.[\w-]+)+|@[\w-]+(?:\.[\w-]+)+)$')


def _horas_cada_media_hora():
    """[("05:00", "5:00 a. m."), ...] hasta las 22:00, para el horario personalizado."""
    opciones = []
    for minutos in range(5 * 60, 22 * 60 + 1, 30):
        h, m = divmod(minutos, 60)
        sufijo = "a. m." if h < 12 else "p. m."
        h12 = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
        opciones.append((f"{h:02d}:{m:02d}", f"{h12}:{m:02d} {sufijo}"))
    return opciones


def remitentes_frecuentes(cuentas, por_cuenta=50, maximo=8):
    """Remitentes humanos que más escriben a estas cuentas, para sugerirlos.

    Se descartan los automáticos (noreply, notificaciones), los masivos
    (List-Unsubscribe), las entidades (bancos, comercios) y las propias cuentas.
    """
    propias = {c.lower() for c in cuentas}
    conteo = Counter()
    for alias in cuentas:
        try:
            correos = gc.leer_correos_cuenta(alias, limite=por_cuenta, solo_no_leidos=False)
        except Exception as e:
            print(f"⚠️ No se pudieron leer remitentes de [{alias}]: {e}")
            continue
        for correo in correos:
            remitente = (correo.get("remitente_email") or "").lower().strip()
            if (not remitente or remitente in propias or correo.get("lista_baja")
                    or clasificador._es_remitente_automatico(remitente)
                    or clasificador._es_entidad(clasificador._dominio(remitente))):
                continue
            conteo[remitente] += 1
    return [r for r, _ in conteo.most_common(maximo)]


class AsistentePerfil:
    """Diálogo por pasos. 'app' es la AppStefany (page y paleta)."""

    def __init__(self, app, desde_paso="cuentas", al_cerrar=None, modo_edicion=False):
        """modo_edicion=True (desde "Mis preferencias"): una sola pregunta, con
        Cancelar / Guardar, y nada se escribe en disco hasta pulsar Guardar."""
        self.app = app
        self.page = app.page
        self.al_cerrar = al_cerrar
        self.modo_edicion = modo_edicion
        self.perfil = memoria_local.cargar_perfil()
        self.indice = PASOS.index(desde_paso) if desde_paso in PASOS else 0
        self._sugerencias = None            # remitentes frecuentes (se cargan una vez)
        self._cargando_sugerencias = False

        self.texto_progreso = ft.Text("", size=11)
        self.barra_progreso = ft.ProgressBar(value=0, bar_height=4, border_radius=2)
        self.titulo = ft.Text("", size=18, weight=ft.FontWeight.BOLD)
        self.cuerpo = ft.Column(spacing=10, scroll=ft.ScrollMode.AUTO, tight=True)

        self.boton_despues = ft.TextButton("Dejar para después", on_click=self._dejar_para_despues)
        self.boton_atras = ft.OutlinedButton("Atrás", on_click=lambda e: self._ir(-1))
        self.boton_siguiente = ft.FilledButton("Siguiente", on_click=lambda e: self._ir(+1))

        if modo_edicion:
            self.boton_despues = ft.TextButton("Cancelar", on_click=lambda e: self._cerrar())
            self.boton_siguiente = ft.FilledButton("Guardar", on_click=lambda e: self._guardar_y_cerrar())
            acciones = [self.boton_despues, self.boton_siguiente]
            cabecera = [self.titulo]
        else:
            acciones = [self.boton_despues, self.boton_atras, self.boton_siguiente]
            cabecera = [self.texto_progreso, self.barra_progreso, self.titulo]

        self.dlg = ft.AlertDialog(
            modal=True,
            title=ft.Column(cabecera, spacing=6, tight=True),
            content=ft.Container(content=self.cuerpo, width=540, height=360),
            actions=acciones,
        )

    # ------------------------------------------------------------------
    # Navegación
    # ------------------------------------------------------------------
    def abrir(self):
        self._pintar_paso()
        self.page.show_dialog(self.dlg)

    def _paso(self):
        return PASOS[self.indice]

    def _guardar(self):
        memoria_local.guardar_perfil(self.perfil)

    def _ir(self, delta):
        if self.modo_edicion:
            # Enter en un campo = Guardar (no hay "siguiente pregunta").
            if delta > 0:
                self._guardar_y_cerrar()
            return
        if self._paso() == "listo" and delta > 0:
            self.perfil["onboarding_completado"] = True
            self.perfil["onboarding_pospuesto"] = False
            self._guardar()
            self._cerrar()
            return
        self._guardar()
        self.indice = max(0, min(len(PASOS) - 1, self.indice + delta))
        self._pintar_paso()
        self.dlg.update()

    def _dejar_para_despues(self, e=None):
        # Lo contestado hasta aquí se conserva; el asistente no vuelve a
        # abrirse solo, pero la pantalla de inicio ofrece retomarlo.
        self.perfil["onboarding_pospuesto"] = True
        self._guardar()
        self._cerrar()

    def _guardar_y_cerrar(self):
        self._guardar()
        self._cerrar()

    def _cerrar(self):
        self.page.pop_dialog()
        if self.al_cerrar:
            self.al_cerrar()

    def _pintar_paso(self):
        paso = self._paso()
        total = len(PASOS) - 1                      # "listo" no cuenta como pregunta
        numero = min(self.indice + 1, total)
        self.texto_progreso.value = "¡Todo listo!" if paso == "listo" else f"Paso {numero} de {total}"
        self.texto_progreso.color = self.app.paleta["texto_secundario"]
        self.barra_progreso.value = self.indice / total
        self.barra_progreso.color = self.app.paleta["accento"]

        self.cuerpo.controls.clear()
        getattr(self, f"_paso_{paso}")()

        if self.modo_edicion:
            return

        self.boton_atras.visible = self.indice > 0
        self.boton_despues.visible = paso != "listo"
        self.boton_siguiente.content = "Empezar" if paso == "listo" else "Siguiente"
        self.boton_siguiente.disabled = paso == "cuentas" and not gc.cuentas_conectadas

    def _repintar(self):
        """Repinta el paso actual (tras un cambio que muestra u oculta campos)."""
        self._pintar_paso()
        try:
            self.dlg.update()
        except Exception:
            pass

    def _ayuda(self, texto):
        return ft.Text(texto, size=12, color=self.app.paleta["texto_secundario"])

    # ------------------------------------------------------------------
    # Paso 1 — Cuentas de Google
    # ------------------------------------------------------------------
    def _paso_cuentas(self):
        self.titulo.value = "¡Hola! Soy Stefany 👋"
        self.cuerpo.controls.append(self._ayuda(
            f"Para empezar, conecta las cuentas de Google cuyo correo y calendario quieres "
            f"que revise. Puedes conectar hasta {oauth.MAXIMO_CUENTAS}. Se abrirá tu "
            f"navegador para iniciar sesión."))

        cuentas = list(gc.cuentas_conectadas)
        if cuentas:
            for alias in cuentas:
                self.cuerpo.controls.append(ft.Row(
                    [ft.Icon(ft.Icons.CHECK_CIRCLE, color=self.app.paleta["estado_ok"], size=18),
                     ft.Text(alias, size=13)], spacing=8))
        else:
            self.cuerpo.controls.append(ft.Text("Todavía no hay ninguna cuenta conectada.", size=12))

        lleno = len(cuentas) >= oauth.MAXIMO_CUENTAS
        self.estado_conexion = ft.Text(
            f"Llegaste al máximo de {oauth.MAXIMO_CUENTAS} cuentas." if lleno else "", size=11)
        self.boton_conectar = ft.FilledButton(
            "＋ Conectar cuenta de Google" if not cuentas else "＋ Conectar otra cuenta",
            disabled=lleno, on_click=self._conectar_cuenta)
        self.cuerpo.controls.extend([self.boton_conectar, self.estado_conexion])

    def _conectar_cuenta(self, e):
        self.boton_conectar.disabled = True
        self.boton_conectar.content = "Revisa tu navegador..."
        self.estado_conexion.value = "Inicia sesión en la pestaña que se abrió y acepta los permisos."
        self.estado_conexion.color = self.app.paleta["texto_secundario"]
        self.dlg.update()
        self.app._lanzar_hilo(self._conectar_cuenta_hilo)

    def _conectar_cuenta_hilo(self):
        mensaje, color = "", self.app.paleta["estado_ok"]
        try:
            alias, _ = oauth.conectar_cuenta_google()
            gc.actualizar_cuentas_conectadas(oauth.construir_cuentas_conectadas())
            mensaje = f"✅ {alias} conectada."
            self._sugerencias = None        # hay cuentas nuevas: recalcular remitentes
        except oauth.OAuthStefanyError as ex:
            mensaje, color = f"⚠️ {ex}", "#ef4444"
        except Exception as ex:
            import traceback
            traceback.print_exc()
            mensaje, color = f"⚠️ No se pudo completar la conexión ({type(ex).__name__}).", "#ef4444"
        if self._paso() == "cuentas":
            self._pintar_paso()
            self.estado_conexion.value, self.estado_conexion.color = mensaje, color
            try:
                self.dlg.update()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Paso 2 — Nombre
    # ------------------------------------------------------------------
    def _paso_nombre(self):
        self.titulo.value = "¿Cómo quieres que te llamemos?"
        campo = ft.TextField(
            label="Tu nombre", value=self.perfil.get("nombre", ""), autofocus=True,
            on_change=lambda e: self.perfil.__setitem__("nombre", e.control.value.strip()),
            on_submit=lambda e: self._ir(+1))
        self.cuerpo.controls.extend([self._ayuda("Así te saludará Stefany."), campo])

    # ------------------------------------------------------------------
    # Paso 3 — Apodos y cuenta preferida
    # ------------------------------------------------------------------
    def _paso_tus_cuentas(self):
        self.titulo.value = "Tus cuentas"
        cuentas = list(gc.cuentas_conectadas)
        if not cuentas:
            self.cuerpo.controls.append(self._ayuda(
                "No hay cuentas conectadas. Puedes volver al primer paso o seguir y "
                "configurarlas más tarde."))
            return

        apodos = self.perfil.setdefault("cuentas", {})
        self.cuerpo.controls.append(self._ayuda(
            "Ponle un apodo a cada cuenta para reconocerla de un vistazo "
            "(se mostrará junto al correo)."))
        for alias in cuentas:
            campo = ft.TextField(label="Apodo", value=(apodos.get(alias) or {}).get("apodo", ""),
                                 width=170, dense=True)

            def fijar(valor, a=alias, c=campo):
                apodos.setdefault(a, {})["apodo"] = (valor or "").strip()
                if c.value != valor:
                    c.value = valor
                    c.update()

            campo.on_change = lambda e, f=fijar: f(e.control.value)
            sugerencias = ft.Row([ft.Chip(label=s, on_click=lambda e, s=s, f=fijar: f(s))
                                  for s in APODOS_SUGERIDOS], spacing=4, wrap=True)
            self.cuerpo.controls.append(ft.Column(
                [ft.Text(alias, size=13, weight=ft.FontWeight.W_600),
                 ft.Row([campo, sugerencias], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)],
                spacing=4, tight=True))

        if self.perfil.get("cuenta_preferida_agendar") not in cuentas:
            self.perfil["cuenta_preferida_agendar"] = cuentas[0]
        self.cuerpo.controls.extend([
            ft.Divider(height=8),
            ft.Text("¿En qué cuenta agendamos lo que pidas en el chat?", size=14, weight=ft.FontWeight.W_600),
            self._ayuda("Los eventos que vengan de un correo se agendan siempre en la cuenta a la que llegó."),
            ft.RadioGroup(
                value=self.perfil["cuenta_preferida_agendar"],
                on_change=lambda e: self.perfil.__setitem__("cuenta_preferida_agendar", e.control.value),
                content=ft.Column([ft.Radio(value=a, label=a) for a in cuentas], spacing=0, tight=True)),
        ])

    # ------------------------------------------------------------------
    # Paso 4 — Correos importantes
    # ------------------------------------------------------------------
    def _paso_importantes(self):
        self.titulo.value = "¿Qué correos consideras más importantes?"
        self.cuerpo.controls.append(self._ayuda(
            "Aparecerán primero y cerrados, para mantener limpia la vista inicial. "
            "Puedes marcar varios."))
        elegidas = self.perfil.setdefault("categorias_importantes", [])

        def cambiar(clave, marcado):
            if marcado and clave not in elegidas:
                elegidas.append(clave)
            elif not marcado and clave in elegidas:
                elegidas.remove(clave)
                if clave == "otros":
                    memoria_local.fijar_otros_importantes(self.perfil, "")
            if clave == "otros":
                self._repintar()

        for clave, etiqueta in CATEGORIAS:
            self.cuerpo.controls.append(ft.Checkbox(
                label=etiqueta, value=clave in elegidas,
                on_change=lambda e, c=clave: cambiar(c, e.control.value)))

        if "otros" in elegidas:
            self.cuerpo.controls.append(ft.TextField(
                label="¿Qué otros correos?", hint_text="Por ejemplo: los de mi EPS o de mi arrendador",
                value=self.perfil.get("otros_importantes", ""), dense=True,
                on_change=lambda e: memoria_local.fijar_otros_importantes(self.perfil, e.control.value)))

    # ------------------------------------------------------------------
    # Paso 5 — Remitentes importantes
    # ------------------------------------------------------------------
    def _paso_remitentes(self):
        self.titulo.value = "¿Hay algún remitente cuyos correos quieras considerar especialmente importantes?"
        remitentes = self.perfil.setdefault("remitentes_importantes", [])
        if not hasattr(self, "_quiere_remitentes"):
            self._quiere_remitentes = "si" if remitentes else ""

        def elegir(valor):
            self._quiere_remitentes = valor
            if valor == "no":
                remitentes.clear()
            self._repintar()

        self.cuerpo.controls.append(ft.RadioGroup(
            value=self._quiere_remitentes, on_change=lambda e: elegir(e.control.value),
            content=ft.Row([ft.Radio(value="si", label="Sí"), ft.Radio(value="no", label="No")])))

        if self._quiere_remitentes != "si":
            return

        self.cuerpo.controls.append(self._ayuda(
            "Escribe un correo (profesor@uan.edu.co) o un dominio completo (@uan.edu.co). "
            "Sus correos siempre saldrán destacados."))
        aviso = ft.Text("", size=11, color="#ef4444")
        campo = ft.TextField(label="Correo o @dominio", dense=True, expand=True)

        def agregar(valor):
            valor = (valor or "").strip().lower()
            if not PATRON_REMITENTE.match(valor):
                aviso.value = "Escribe un correo completo o un dominio que empiece por @."
                aviso.update()
                return
            if valor not in remitentes:
                remitentes.append(valor)
            self._repintar()

        campo.on_submit = lambda e: agregar(campo.value)
        self.cuerpo.controls.extend([
            ft.Row([campo, ft.FilledButton("Añadir", on_click=lambda e: agregar(campo.value))],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            aviso,
        ])

        if remitentes:
            def quitar(valor):
                remitentes.remove(valor)
                self._repintar()
            self.cuerpo.controls.append(ft.Row(
                [ft.Chip(label=r, on_delete=lambda e, r=r: quitar(r)) for r in remitentes],
                wrap=True, spacing=4))

        # Sugerencias: remitentes frecuentes de las cuentas conectadas.
        if self._sugerencias is None:
            self.cuerpo.controls.append(self._ayuda("Buscando remitentes frecuentes en tu bandeja..."))
            if not self._cargando_sugerencias and gc.cuentas_conectadas:
                self._cargando_sugerencias = True
                self.app._lanzar_hilo(self._cargar_sugerencias_hilo)
        else:
            pendientes = [s for s in self._sugerencias if s not in remitentes]
            if pendientes:
                self.cuerpo.controls.extend([
                    self._ayuda("Sugerencias (quienes más te escriben):"),
                    ft.Row([ft.Chip(label=s, leading=ft.Icon(ft.Icons.ADD, size=14),
                                    on_click=lambda e, s=s: agregar(s)) for s in pendientes],
                           wrap=True, spacing=4),
                ])

    def _cargar_sugerencias_hilo(self):
        try:
            self._sugerencias = remitentes_frecuentes(list(gc.cuentas_conectadas))
        except Exception as e:
            print(f"⚠️ No se pudieron calcular sugerencias de remitentes: {e}")
            self._sugerencias = []
        self._cargando_sugerencias = False
        if self._paso() == "remitentes":
            self._repintar()

    # ------------------------------------------------------------------
    # Paso 6 — Horario de trabajo o estudio
    # ------------------------------------------------------------------
    def _paso_horario(self):
        self.titulo.value = "¿Cuál es tu horario habitual de trabajo o estudio?"
        horario = self.perfil.setdefault("horario", {"dias": [], "inicio": "", "fin": ""})
        dias = horario.setdefault("dias", [])

        # --- Días ---
        if not hasattr(self, "_opcion_dias"):
            self._opcion_dias = next((k for k, v in OPCIONES_DIAS.items() if v == sorted(dias)),
                                     "otro" if dias else "")

        def elegir_dias(valor):
            self._opcion_dias = valor
            if valor in OPCIONES_DIAS:
                horario["dias"] = list(OPCIONES_DIAS[valor])
            self._repintar()

        self.cuerpo.controls.extend([
            ft.Text("Días", size=14, weight=ft.FontWeight.W_600),
            ft.RadioGroup(value=self._opcion_dias, on_change=lambda e: elegir_dias(e.control.value),
                          content=ft.Row([ft.Radio(value="lv", label="Lunes a viernes"),
                                          ft.Radio(value="ls", label="Lunes a sábado"),
                                          ft.Radio(value="otro", label="Otro")], wrap=True)),
        ])
        if self._opcion_dias == "otro":
            def marcar_dia(i, marcado):
                actuales = set(horario.get("dias") or [])
                actuales.add(i) if marcado else actuales.discard(i)
                horario["dias"] = sorted(actuales)
            self.cuerpo.controls.append(ft.Row(
                [ft.Checkbox(label=d, value=i in (horario.get("dias") or []),
                             on_change=lambda e, i=i: marcar_dia(i, e.control.value))
                 for i, d in enumerate(DIAS_CORTOS)], wrap=True, spacing=0))

        # --- Horas ---
        rango_actual = f"{horario.get('inicio', '')}-{horario.get('fin', '')}"
        if not hasattr(self, "_opcion_horas"):
            self._opcion_horas = (rango_actual if rango_actual in OPCIONES_HORARIO
                                  else ("personalizado" if horario.get("inicio") else ""))

        def elegir_horas(valor):
            self._opcion_horas = valor
            if valor in OPCIONES_HORARIO:
                horario["inicio"], horario["fin"] = valor.split("-")
            self._repintar()

        self.cuerpo.controls.extend([
            ft.Text("Horario", size=14, weight=ft.FontWeight.W_600),
            ft.RadioGroup(value=self._opcion_horas, on_change=lambda e: elegir_horas(e.control.value),
                          content=ft.Column([ft.Radio(value=k, label=v) for k, v in OPCIONES_HORARIO.items()]
                                            + [ft.Radio(value="personalizado", label="Personalizado")],
                                            spacing=0, tight=True)),
        ])
        if self._opcion_horas == "personalizado":
            opciones = [ft.DropdownOption(key=k, text=t) for k, t in _horas_cada_media_hora()]

            def fijar(clave, valor):
                horario[clave] = valor

            self.cuerpo.controls.append(ft.Row([
                ft.Dropdown(label="Desde", options=opciones, value=horario.get("inicio") or None, width=150,
                            dense=True, on_select=lambda e: fijar("inicio", e.control.value)),
                ft.Dropdown(label="Hasta", options=list(opciones), value=horario.get("fin") or None, width=150,
                            dense=True, on_select=lambda e: fijar("fin", e.control.value)),
            ], spacing=10))

    # ------------------------------------------------------------------
    # Paso 7 — Franja de reuniones
    # ------------------------------------------------------------------
    def _paso_reuniones(self):
        self.titulo.value = "¿En qué horario prefieres tener reuniones?"
        reuniones = self.perfil.setdefault("reuniones", {"franja": "", "duracion_min": None})
        self.cuerpo.controls.extend([
            self._ayuda("Se usa como hora por defecto cuando un evento no dice a qué hora es."),
            ft.RadioGroup(value=reuniones.get("franja", ""),
                          on_change=lambda e: reuniones.__setitem__("franja", e.control.value),
                          content=ft.Column([ft.Radio(value=k, label=v) for k, v in FRANJAS],
                                            spacing=0, tight=True)),
        ])

    # ------------------------------------------------------------------
    # Paso 8 — Duración de reuniones
    # ------------------------------------------------------------------
    def _paso_duracion(self):
        self.titulo.value = "¿Cuánto suelen durar tus reuniones?"
        reuniones = self.perfil.setdefault("reuniones", {"franja": "", "duracion_min": None})
        actual = reuniones.get("duracion_min")
        valor = str(actual) if actual else ""

        def elegir(v):
            # "depende" se guarda tal cual: al agendar cuenta como 1 hora.
            reuniones["duracion_min"] = int(v) if v.isdigit() else "depende"

        self.cuerpo.controls.extend([
            self._ayuda("Es la duración con la que se agendan los eventos que no dicen cuándo terminan."),
            ft.RadioGroup(value=valor, on_change=lambda e: elegir(e.control.value),
                          content=ft.Column([ft.Radio(value=k, label=v) for k, v in DURACIONES],
                                            spacing=0, tight=True)),
        ])

    # ------------------------------------------------------------------
    # Paso final — Resumen
    # ------------------------------------------------------------------
    def _paso_listo(self):
        nombre = self.perfil.get("nombre") or ""
        self.titulo.value = f"¡Listo{', ' + nombre if nombre else ''}! 🎉"
        self.cuerpo.controls.append(self._ayuda("Así quedó tu perfil:"))
        for etiqueta, valor in resumen_perfil(self.perfil):
            self.cuerpo.controls.append(ft.Row(
                [ft.Text(etiqueta, size=12, weight=ft.FontWeight.W_600, width=170),
                 ft.Text(valor, size=12, expand=True)], vertical_alignment=ft.CrossAxisAlignment.START))
        self.cuerpo.controls.append(self._ayuda(
            "Lo que no hayas contestado usa valores por defecto. Podrás cambiarlo cuando quieras."))


def _con_apodo(perfil, alias):
    apodo = (((perfil.get("cuentas") or {}).get(alias or "") or {}).get("apodo") or "").strip()
    return f"{apodo} · {alias}" if alias and apodo else (alias or "")


def resumen_perfil(perfil):
    """[(etiqueta, texto)] legible del perfil, para el paso final y "Mis preferencias"."""
    sin = "Sin responder"
    cuentas = perfil.get("cuentas") or {}
    apodos = ", ".join(f"{c['apodo']} ({a})" for a, c in cuentas.items() if (c or {}).get("apodo")) or sin

    categorias = dict(CATEGORIAS)
    importantes = [categorias[c] for c in perfil.get("categorias_importantes") or [] if c in categorias]
    if perfil.get("otros_importantes"):
        importantes = [i for i in importantes if not i.startswith("⭐")] + [f"⭐ {perfil['otros_importantes']}"]

    horario = perfil.get("horario") or {}
    dias = horario.get("dias") or []
    dias_txt = ("Lunes a viernes" if dias == OPCIONES_DIAS["lv"] else
                "Lunes a sábado" if dias == OPCIONES_DIAS["ls"] else
                ", ".join(DIAS_CORTOS[d] for d in dias))
    horas = dict(_horas_cada_media_hora())
    horas_txt = (f"{horas.get(horario['inicio'], horario['inicio'])} – {horas.get(horario.get('fin', ''), horario.get('fin', ''))}"
                 if horario.get("inicio") else "")
    horario_txt = " · ".join(t for t in (dias_txt, horas_txt) if t) or sin

    reuniones = perfil.get("reuniones") or {}
    franja = dict(FRANJAS).get(reuniones.get("franja"), sin)
    duracion = reuniones.get("duracion_min")
    duracion_txt = dict(DURACIONES).get(str(duracion), sin) if duracion else sin

    return [
        ("Nombre", perfil.get("nombre") or sin),
        ("Apodos", apodos),
        ("Agendar desde el chat en", _con_apodo(perfil, perfil.get("cuenta_preferida_agendar")) or sin),
        ("Correos importantes", ", ".join(importantes) or sin),
        ("Remitentes importantes", ", ".join(perfil.get("remitentes_importantes") or []) or "Ninguno"),
        ("Horario", horario_txt),
        ("Reuniones", franja),
        ("Duración habitual", duracion_txt),
    ]


# ----------------------------------------------------------------------
# "Mis preferencias": ver, editar y eliminar lo guardado
# ----------------------------------------------------------------------

# Qué pregunta edita cada fila del resumen.
PASO_DE_FILA = {
    "Nombre": "nombre",
    "Apodos": "tus_cuentas",
    "Agendar desde el chat en": "tus_cuentas",
    "Correos importantes": "importantes",
    "Remitentes importantes": "remitentes",
    "Horario": "horario",
    "Reuniones": "reuniones",
    "Duración habitual": "duracion",
}


class VistaPreferencias:
    """Resumen del perfil con ✏️ por fila y la lista de preferencias libres.

    Editar abre UNA pregunta del asistente (modo edición); al guardar o
    cancelar se vuelve a este resumen, ya actualizado.
    """

    def __init__(self, app, al_cerrar=None):
        self.app = app
        self.page = app.page
        self.al_cerrar = al_cerrar
        self.cuerpo = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, tight=True)
        self.dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([ft.Text("⚙️ Mis preferencias", size=18, weight=ft.FontWeight.BOLD, expand=True),
                          ft.IconButton(icon=ft.Icons.CLOSE, icon_size=18, tooltip="Cerrar",
                                        on_click=lambda e: self._cerrar())],
                         vertical_alignment=ft.CrossAxisAlignment.CENTER),
            content=ft.Container(content=self.cuerpo, width=560, height=420),
            actions=[ft.TextButton("Repetir todas las preguntas", on_click=lambda e: self._repetir_todo()),
                     ft.FilledButton("Cerrar", on_click=lambda e: self._cerrar())],
        )

    def abrir(self):
        self._pintar()
        self.page.show_dialog(self.dlg)

    def _cerrar(self):
        self.page.pop_dialog()
        if self.al_cerrar:
            self.al_cerrar()

    def _secundario(self, texto, **kw):
        return ft.Text(texto, size=12, color=self.app.paleta["texto_secundario"], **kw)

    def _pintar(self):
        perfil = memoria_local.cargar_perfil()
        self.cuerpo.controls.clear()

        # --- Perfil ---
        for etiqueta, valor in resumen_perfil(perfil):
            paso = PASO_DE_FILA.get(etiqueta)
            self.cuerpo.controls.append(ft.Row(
                [ft.Text(etiqueta, size=12, weight=ft.FontWeight.W_600, width=170),
                 ft.Text(valor, size=12, expand=True),
                 ft.IconButton(icon=ft.Icons.EDIT_OUTLINED, icon_size=16, tooltip=f"Editar: {etiqueta}",
                               on_click=lambda e, p=paso: self._editar(p))],
                vertical_alignment=ft.CrossAxisAlignment.CENTER))

        # --- Preferencias libres ---
        libres = perfil.get("preferencias_libres") or []
        self.cuerpo.controls.extend([
            ft.Divider(height=12),
            ft.Text("Preferencias libres", size=14, weight=ft.FontWeight.W_600),
            self._secundario("Frases que Stefany tiene en cuenta al analizar tus correos. "
                             "También se guardan al escribir en el chat \"recuerda que...\"."),
        ])
        if not libres:
            self.cuerpo.controls.append(self._secundario("No hay ninguna guardada.", italic=True))
        for indice, frase in enumerate(libres):
            self.cuerpo.controls.append(ft.Row(
                [ft.Text(f"• {frase}", size=12, expand=True),
                 ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, icon_size=16, tooltip="Eliminar",
                               on_click=lambda e, i=indice: self._eliminar_libre(i))],
                vertical_alignment=ft.CrossAxisAlignment.CENTER))

        campo = ft.TextField(label="Añadir una preferencia", dense=True, expand=True,
                             hint_text="Por ejemplo: no me agendes nada los domingos")
        campo.on_submit = lambda e: self._agregar_libre(campo.value)
        self.cuerpo.controls.append(ft.Row(
            [campo, ft.OutlinedButton("Añadir", on_click=lambda e: self._agregar_libre(campo.value))],
            vertical_alignment=ft.CrossAxisAlignment.CENTER))

    def _repintar(self):
        self._pintar()
        try:
            self.dlg.update()
        except Exception:
            pass

    def _editar(self, paso):
        # Se cierra el resumen y se abre la pregunta; al terminar, vuelve el resumen.
        self.page.pop_dialog()
        AsistentePerfil(self.app, paso, modo_edicion=True,
                        al_cerrar=lambda: VistaPreferencias(self.app, self.al_cerrar).abrir()).abrir()

    def _repetir_todo(self):
        self.page.pop_dialog()
        AsistentePerfil(self.app, "nombre",
                        al_cerrar=lambda: VistaPreferencias(self.app, self.al_cerrar).abrir()).abrir()

    def _agregar_libre(self, texto):
        if (texto or "").strip():
            memoria_local.agregar_preferencia(texto)
            self._repintar()

    def _eliminar_libre(self, indice):
        perfil = memoria_local.cargar_perfil()
        libres = perfil.get("preferencias_libres") or []
        if not 0 <= indice < len(libres):
            return
        if libres[indice].startswith(memoria_local.PREFIJO_OTROS_IMPORTANTES):
            # Es la de "⭐ Otros": se desmarca también en correos importantes.
            memoria_local.fijar_otros_importantes(perfil, "")
            perfil["categorias_importantes"] = [c for c in perfil.get("categorias_importantes") or []
                                                if c != "otros"]
        else:
            libres.pop(indice)
        memoria_local.guardar_perfil(perfil)
        self._repintar()
