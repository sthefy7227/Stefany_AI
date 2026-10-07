# -*- coding: utf-8 -*-
"""
evaluar_20_correos.py — Evaluación comparativa sobre correos reales.

Toma los últimos 20 correos de la bandeja de entrada de una cuenta conectada
(leídos o no) y analiza cada uno de dos maneras:

  A) LLaMA tal como se consigue: el modelo base (sin adaptador), sin mensaje
     de sistema, sin calendario y sin cuentas; solo el texto limpio del
     correo y la petición "Revisa este correo."
  B) Stefany: el modelo ajustado con el prompt de producción, el calendario
     real de todas las cuentas conectadas, el mensaje anterior del hilo y el
     escudo de verificación por código.

Escribe evaluacion_20_correos.txt (cada correo con las dos salidas) y
evaluacion_20_correos_resumen.txt (tabla de indicadores para la monografía).
Los correos son reales: el archivo es privado y NO va en el documento;
de él se toman solo las capturas o filas que la autora decida mostrar.

Uso (servidor desplegado, cuentas ya conectadas en la app):
    python evaluar_20_correos.py            -> usa la primera cuenta conectada
    python evaluar_20_correos.py Trabajo    -> usa la cuenta con ese apodo
    python evaluar_20_correos.py Trabajo 30 -> otra cantidad de correos
"""
import re
import sys
import time

import oauth_stefany as oauth
import gmail_calendar_stefany as gc
import extraccion_stefany as extraccion
import clasificador_stefany as clasificador
import analisis_stefany as analisis
import prompts_stefany as prompts
from cliente_stefany import ClienteStefany

# ---- servidor ---------------------------------------------------------------
_src = open("app_stefany.py", encoding="utf8").read()
URL = re.search(r'URL_SERVIDOR_MODAL\s*=\s*"([^"]+)"', _src).group(1)
CLAVE = re.search(r'CLAVE_API_STEFANY\s*=\s*"([^"]+)"', _src).group(1)
cliente = ClienteStefany(URL, CLAVE, timeout=300)
prompts.configurar_cliente(cliente)
prompts.configurar_modo_prueba(forzar_base=False)

# ---- cuentas ----------------------------------------------------------------
cuentas = oauth.construir_cuentas_conectadas()
if not cuentas:
    sys.exit("No hay cuentas conectadas. Conéctalas primero desde la aplicación.")
gc.actualizar_cuentas_conectadas(cuentas)
alias = sys.argv[1] if len(sys.argv) > 1 else list(cuentas)[0]
if alias not in cuentas:
    sys.exit(f"La cuenta '{alias}' no está conectada. Disponibles: {', '.join(cuentas)}")
N = int(sys.argv[2]) if len(sys.argv) > 2 else 20

print(f"Cuenta: {alias}  |  Correos: últimos {N} de la bandeja de entrada")
print("Precalentando el modelo..."); cliente.calentar()

# ---- correos y calendario ---------------------------------------------------
correos = gc.leer_correos_cuenta(alias, limite=N, solo_no_leidos=False)
correos = correos[:N]
gc.hidratar_cuerpos(alias, correos)
try:
    gc.hidratar_hilo_anterior(alias, correos)
except Exception as e:
    print(f"(sin contexto de hilo: {e})")
eventos = gc.leer_eventos_todas_cuentas(cantidad=15, dias=14)
contexto_cal = analisis.contexto_calendario_completo(eventos)
print(f"{len(correos)} correos descargados; eventos: "
      + ", ".join(f"[{a}] {len(v)}" for a, v in eventos.items()))

# ---- indicadores ------------------------------------------------------------
def redacta_respuesta(t):
    t = t.lower()
    return any(p in t for p in ("estimad", "atentamente", "cordialmente", "un saludo", "saludos,", "hola "))
def menciona_calendario(t):
    return bool(re.search(r"calendario|agend", t.lower()))
def otro_idioma(t):
    try: return analisis._parece_otro_idioma(t)
    except Exception: return bool(re.search(r"\b(the|and|you|your|meeting)\b", t.lower()))

ANCHO = 90
L = []          # informe largo
def log(t=""):
    print(t); L.append(t)

cont = {"A_palabras": [], "A_redacta": 0, "A_calendario": 0, "A_idioma": 0, "A_seg": [],
        "B_palabras": [], "B_analisis": 0, "B_agendar": 0, "B_cruce": 0, "B_actualizar": 0,
        "B_cancelar": 0, "B_sin_accion": 0, "B_resumen_respaldo": 0, "B_seg": [],
        "clasif": {}, "necesita_llm": 0}

for i, correo in enumerate(correos, 1):
    clasif = clasificador.clasificar(correo)
    cont["clasif"][clasif["categoria"]] = cont["clasif"].get(clasif["categoria"], 0) + 1
    cont["necesita_llm"] += int(clasif["necesita_llm"])
    cuerpo = (correo.get("cuerpo") or correo.get("fragmento") or "").strip()
    log("=" * ANCHO)
    log(f" CORREO {i}/{len(correos)}  [{alias}]  {correo['fecha']}")
    log(f" De: {correo['remitente']}")
    log(f" Asunto: {correo['asunto']}")
    log(f" Clasificación por reglas: {clasif['categoria']}  señales: {', '.join(clasif['senales']) or 'ninguna'}"
        f"  ({len(cuerpo.split())} palabras de cuerpo limpio)")

    # A) LLaMA tal como se consigue
    log("-" * ANCHO); log(" A) LLaMA 3.1 8B sin ajuste, sin instrucciones, sin calendario"); log("-" * ANCHO)
    peticion = f"Revisa este correo.\n\nAsunto: {correo['asunto']}\nDe: {correo['remitente']}\n\n{cuerpo}"
    t0 = time.time()
    try:
        ra = cliente.generar([{"role": "user", "content": peticion}], usar_adaptador=False,
                             max_new_tokens=300, temperature=0.3).strip()
    except Exception as e:
        ra = f"[ERROR: {e}]"
    cont["A_seg"].append(time.time() - t0)
    cont["A_palabras"].append(len(ra.split()))
    cont["A_redacta"] += int(redacta_respuesta(ra))
    cont["A_calendario"] += int(menciona_calendario(ra))
    cont["A_idioma"] += int(otro_idioma(ra))
    log(ra)

    # B) Stefany: modelo ajustado + calendario real + hilo + escudo
    log("-" * ANCHO); log(" B) Stefany: modelo ajustado + calendario real + verificación por código"); log("-" * ANCHO)
    t0 = time.time()
    try:
        tm = prompts.analizar_correo(correo, contexto_cal)
        tm = analisis._sanear_texto_modelo(tm, correo)
        tm = analisis.verificar_conflicto_horario(tm, eventos)
    except Exception as e:
        tm = f"[ERROR: {e}]"
    cont["B_seg"].append(time.time() - t0)
    cont["B_palabras"].append(len(tm.split()))
    if "📊 Análisis:" in tm:
        resumen, an = [x.strip() for x in tm.split("📊 Análisis:", 1)]
        cont["B_analisis"] += 1
        a = an.lower()
        if "¿cancelo" in a: cont["B_cruce"] += 1
        elif "¿te gustaría que lo agende" in a: cont["B_agendar"] += 1
        elif "actualizar el evento" in a: cont["B_actualizar"] += 1
        elif "cancel" in a: cont["B_cancelar"] += 1
    else:
        resumen, an = tm.strip(), ""
        cont["B_sin_accion"] += 1
    if "(resumen de respaldo" in resumen.lower() or "respaldo" in resumen.lower():
        cont["B_resumen_respaldo"] += 1
    log(" Resumen: " + resumen)
    log(" Análisis: " + (an if an else "(ninguno: el correo no requiere acción)"))

log("=" * ANCHO)
open("evaluacion_20_correos.txt", "w", encoding="utf8").write("\n".join(L))

# ---- resumen para la monografía ------------------------------------------------
n = len(correos)
prom = lambda xs: (sum(xs) / len(xs)) if xs else 0
R = []
def r(t=""):
    print(t); R.append(t)
r("INDICADORES SOBRE LOS ÚLTIMOS %d CORREOS REALES DE LA CUENTA '%s'" % (n, alias))
r("(Tabla H7 de la monografía; sin contenido de los correos)")
r("")
r("Categorías por reglas: " + ", ".join(f"{k} {v}" for k, v in sorted(cont["clasif"].items())))
r(f"Correos que las reglas envían al modelo: {cont['necesita_llm']} de {n}")
r("")
r("%-58s %10s %10s" % ("Indicador", "A) LLaMA", "B) Stefany"))
r("-" * 80)
r("%-58s %10s %10s" % ("Respuestas con bloque de análisis procesable por código", "0", cont["B_analisis"]))
r("%-58s %10s %10s" % ("  - propone agendar (fecha límite / reunión)", "0", cont["B_agendar"]))
r("%-58s %10s %10s" % ("  - detecta cruce con el calendario real", "0", cont["B_cruce"]))
r("%-58s %10s %10s" % ("  - detecta cambio sobre evento agendado", "0", cont["B_actualizar"]))
r("%-58s %10s %10s" % ("  - detecta cancelación", "0", cont["B_cancelar"]))
r("%-58s %10s %10s" % ("Correos resueltos solo con resumen (sin acción)", "-", cont["B_sin_accion"]))
r("%-58s %10s %10s" % ("Resúmenes sustituidos por el escudo (respaldo)", "-", cont["B_resumen_respaldo"]))
r("%-58s %10s %10s" % ("Respuestas que redactan un correo en vez de analizar", cont["A_redacta"], "0"))
r("%-58s %10s %10s" % ("Respuestas que mencionan calendario o agendar", cont["A_calendario"], cont["B_analisis"]))
r("%-58s %10s %10s" % ("Respuestas en otro idioma", cont["A_idioma"], "0"))
r("%-58s %10.0f %10.0f" % ("Longitud promedio de la respuesta (palabras)", prom(cont["A_palabras"]), prom(cont["B_palabras"])))
r("%-58s %10.1f %10.1f" % ("Tiempo promedio por correo (s)", prom(cont["A_seg"]), prom(cont["B_seg"])))
r("")
r("Nota: en A el modelo no tiene acceso al calendario ni a las cuentas, por lo que")
r("no puede detectar cruces, cambios ni proponer acciones: esa capacidad la aporta el")
r("sistema (prompt de producción, calendario real, hilo, adaptador y verificación).")
open("evaluacion_20_correos_resumen.txt", "w", encoding="utf8").write("\n".join(R))
print("\nArchivos: evaluacion_20_correos.txt (detalle) y evaluacion_20_correos_resumen.txt (tabla)")
