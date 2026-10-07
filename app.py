import modal

# Versiones FIJAS. Sin fijar, cualquier cambio en la imagen (o una
# reconstrucción de Modal) instalaba lo último publicado, y Unsloth suele
# romperse entre versiones. Son las que tenía la imagen que funcionaba con el
# adaptador v3, leídas con 'pip freeze' dentro de ella el 12-sep-2026 (el
# listado completo está en requisitos_servidor_congelados.txt).
# python_version también se fija: debian_slim() toma por defecto la versión
# de Python del equipo que despliega, así que desplegar desde otro computador
# cambiaba la imagen.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "unsloth==2026.9.4",
        "unsloth_zoo==2026.9.3",
        "torch==2.10.0",
        "torchvision==0.25.0",
        "triton==3.6.0",
        "xformers==0.0.35",
        "bitsandbytes==0.50.2",
        "transformers==5.5.0",
        "tokenizers==0.22.2",
        "peft==0.20.0",
        "accelerate==1.15.0",
        "trl==0.24.0",
        "torchao==0.18.0",
        "safetensors==0.8.0",
        "huggingface_hub==1.31.0",
        "fastapi[standard]==0.141.1",
    )
)

app = modal.App("servidor-stefany-v4", image=image)
hf_secret = modal.Secret.from_name("huggingface-secret")

# Clave de DISTRIBUCIÓN, no un secreto.
#
# La decisión fue que cualquiera con el enlace (jurados, compañeros) pueda
# usar la app sin que se le pida una clave. Eso obliga a embeberla en el
# cliente, y lo que va dentro de un .exe se puede extraer: ofuscarla (base64 y
# demás) no protegería nada, solo daría una falsa sensación de seguridad.
#
# Lo que SÍ hace esta clave es impedir que alguien que encuentre la URL del
# endpoint la use sin más. "ProyectoStefany" se adivinaba; esta no.
#
# La protección REAL del gasto está en el servidor, no en la clave:
#   - max_containers=3 en la clase de GPU (varias personas a la vez, pero un
#     tope duro de GPUs encendidas).
#   - TOPE_MAX_NEW_TOKENS: el cliente manda max_new_tokens y antes se usaba
#     tal cual, así que se podían pedir miles de tokens por petición.
#   - El límite de gasto del panel de Modal (se configura allí, no aquí).
#
# El valor vive en un Secret de Modal para no quedar escrito en este archivo:
#   modal secret create stefany-clave-api CLAVE_STEFANY=<la clave>
clave_secret = modal.Secret.from_name("stefany-clave-api")

# Tope de tokens por petición. La app pide 300 para analizar un correo y 320
# para redactar; 400 deja margen sin permitir que nadie pida 4.000.
TOPE_MAX_NEW_TOKENS = 400

# Longitud máxima de contexto (prompt + respuesta) que maneja el servidor.
# Medido sobre correos reales: el prompt de un correo suelto va de 1.049 a
# 1.692 tokens, así que el valor anterior (1024) truncaba SIEMPRE. 4096 deja
# margen para el calendario completo.
LONGITUD_MAXIMA = 4096


# ---------------------------------------------------------------
# 1. Función ligera en CPU para verificación instantánea
# ---------------------------------------------------------------
@app.function()
def ver_estado_cpu():
    """Estado del servicio.

    Antes devolvía siempre {"status": "ok"} desde una función de CPU, así que
    la interfaz mostraba "Conectado" aunque la GPU estuviera apagada y la
    primera consulta fuera a tardar ~24 segundos cargando el modelo. Ahora se
    distingue el servicio (siempre disponible) del modelo (puede estar frío).
    """
    contenedores = 0
    try:
        contenedores = ModeloServidor().generar_texto_gpu.get_current_stats().num_total_runners
    except Exception:
        pass
    return {"status": "ok",
            "modelo_caliente": contenedores > 0,
            "contenedores": contenedores}


@app.function(timeout=60 * 10)
def calentar():
    """Carga el modelo en la GPU sin generar nada.

    La interfaz la llama al arrancar: así el arranque en frío ocurre mientras
    el usuario conecta cuentas, y no en medio de la primera consulta.
    """
    ModeloServidor().generar_texto_gpu.remote(
        mensajes=[{"role": "user", "content": "hola"}],
        max_new_tokens=1, temperature=0.1,
    )
    return {"status": "caliente"}


# ---------------------------------------------------------------
# 2. Clase serverless en GPU para la inferencia con Llama 3.1 + LoRA
# ---------------------------------------------------------------
@app.cls(
    gpu="T4",
    secrets=[hf_secret],
    timeout=600,
    scaledown_window=300,
    # Tope duro de GPUs encendidas a la vez. Con 1 habría cola si dos
    # personas usan la app durante la sustentación; sin tope, una ráfaga de
    # peticiones podría encender muchas T4 y disparar el gasto.
    max_containers=3,
)
class ModeloServidor:

    @modal.enter()
    def cargar_modelo(self):
        # Unsloth tiene que importarse ANTES que peft/transformers: parchea
        # transformers al importarse y, si llega tarde, avisa en cada arranque
        # y no aplica sus optimizaciones. Va aquí dentro y no al principio del
        # archivo porque 'modal deploy' ejecuta app.py también en el equipo
        # local, donde unsloth no está instalado (ni hay GPU).
        import unsloth  # noqa: F401
        from unsloth import FastLanguageModel
        import os
        from huggingface_hub import login
        from peft import PeftModel

        # El repo del adaptador es PRIVADO: sin token, Hugging Face responde
        # 404 (no 401) para no revelar que el repo existe. Por eso conviene
        # fallar aquí con un mensaje claro en vez de tres pasos más adelante.
        claves = [k for k in os.environ if "HF" in k.upper() or "HUGGING" in k.upper()]
        print(f"🔑 Variables de entorno relacionadas con HF: {claves}")

        hf_token = (
            os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGING_FACE_HUB_TOKEN")
            or os.environ.get("HUGGINGFACE_TOKEN")
        )
        if hf_token:
            login(token=hf_token)
            from huggingface_hub import whoami
            print(f"✅ Autenticado en Hugging Face como: {whoami()['name']}")
        else:
            raise RuntimeError(
                "No llegó ningún token de Hugging Face al contenedor. "
                f"Variables vistas: {claves}. Revisa el secret 'huggingface-secret'."
            )

        print("🔄 Cargando modelo base Llama 3.1 8B 4-bit...")
        self.model, self.tokenizer = FastLanguageModel.from_pretrained(
            model_name="unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit",
            max_seq_length=LONGITUD_MAXIMA,
            load_in_4bit=True,
        )

        RUTA_ADAPTADOR = "Stefany75/lora_stefanyAI_v3"
        print(f"🔄 Cargando adaptador LoRA desde {RUTA_ADAPTADOR}...")
        self.model = PeftModel.from_pretrained(self.model, RUTA_ADAPTADOR)
        FastLanguageModel.for_inference(self.model)

        print("✅ Modelo y adaptador cargados con éxito.")

    @modal.method()
    def generar_texto_gpu(self, mensajes, max_new_tokens=300, temperature=0.3, usar_adaptador=True):
        import time
        import torch

        inicio = time.time()

        prompt = self.tokenizer.apply_chat_template(
            mensajes, tokenize=False, add_generation_prompt=True
        )

        # El prompt tiene que caber JUNTO con la respuesta, no solo él.
        limite_entrada = LONGITUD_MAXIMA - max_new_tokens

        ids_completos = self.tokenizer(prompt, return_tensors="pt")["input_ids"]
        tokens_entrada = ids_completos.shape[1]
        truncado = tokens_entrada > limite_entrada

        # Si toca recortar, se recorta por la IZQUIERDA. El final del prompt
        # lleva el marcador <|start_header_id|>assistant<|end_header_id|>; sin
        # él el modelo continúa escribiendo el prompt en vez de responder.
        ids = ids_completos[:, -limite_entrada:] if truncado else ids_completos
        ids = ids.to("cuda")
        inputs = {"input_ids": ids, "attention_mask": torch.ones_like(ids)}
        input_len = ids.shape[1]

        def _run():
            with torch.no_grad():
                return self.model.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    temperature=temperature,
                    do_sample=True,
                    # repetition_penalty SUAVE (1.05). Se probó sin él y los
                    # resúmenes salieron empobrecidos (sin fechas ni horas).
                    # No volver a 1.15: ver explicación abajo.
                    repetition_penalty=1.05,
                    # no_repeat_ngram_size ELIMINADO (no reintroducir).
                    #
                    # Historia: estaban en 1.15 y 3 desde la primera versión, y eran la
                    # causa real de las palabras mutiladas que se atribuían al
                    # adaptador: "Sostentaciones" por "Sustentaciones",
                    # "septibre" por "septiembre", "nmeros" por "números".
                    #
                    # no_repeat_ngram_size=3 PROHÍBE repetir cualquier
                    # secuencia de tres tokens. Pero resumir un correo exige
                    # repetir sus nombres, fechas y cifras: al estar prohibido,
                    # el modelo se ve obligado a deformar la palabra.
                    # repetition_penalty penaliza reutilizar tokens ya vistos,
                    # incluidos los del propio correo, y de ahí la pérdida de
                    # acentos.
                    #
                    # Ambos sirven para que un modelo no se atasque repitiendo
                    # frases; en una tarea de resumen extractivo hacen daño.
                    pad_token_id=self.tokenizer.eos_token_id,
                )

        if usar_adaptador:
            outputs = _run()
        else:
            with self.model.disable_adapter():
                outputs = _run()

        texto = self.tokenizer.decode(outputs[0][input_len:], skip_special_tokens=True)

        if truncado:
            print(f"⚠️ Prompt recortado: {tokens_entrada} tokens > límite de {limite_entrada}")

        return {
            "texto": texto.strip(),
            "tokens_entrada": int(tokens_entrada),
            "tokens_salida": int(outputs.shape[1] - input_len),
            "truncado": bool(truncado),
            "segundos": round(time.time() - inicio, 1),
        }


# ---------------------------------------------------------------
# 3. Aplicación ASGI FastAPI unificada
# ---------------------------------------------------------------
@app.function(secrets=[clave_secret])
@modal.asgi_app()
def fastapi_app():
    import hmac
    import os

    from fastapi import FastAPI, Header, HTTPException

    web_app = FastAPI()

    def clave_valida(recibida):
        """True si la clave coincide. No lanza: solo responde.

        Comparación en tiempo constante: `!=` corta en el primer carácter
        distinto, y esa diferencia de tiempo permite adivinar la clave
        carácter a carácter. compare_digest siempre tarda lo mismo.
        """
        esperada = os.environ.get("CLAVE_STEFANY", "")
        return bool(esperada) and hmac.compare_digest(str(recibida or ""), esperada)

    def comprobar_clave(recibida):
        """Exige la clave: corta la petición si no es válida."""
        if not os.environ.get("CLAVE_STEFANY", ""):
            # Mejor fallar que servir con la clave vacía si el Secret no llegó.
            raise HTTPException(status_code=500,
                                detail="El servidor no tiene configurada su clave.")
        if not clave_valida(recibida):
            raise HTTPException(status_code=401, detail="Clave de API inválida.")

    @web_app.get("/estado")
    def estado(x_api_key: str = Header(None)):
        # Llama a la función ligera CPU para responder instantáneamente
        info = ver_estado_cpu.remote()
        # La clave aquí se INFORMA, no se exige. Este endpoint es el que mira
        # la barra superior de la app, y antes no comprobaba nada: con una
        # clave equivocada la barra decía "Conectado" igual y el fallo solo
        # aparecía al enviar el primer mensaje, que es lo único que pasa por
        # /generar. Informarlo deja que la app lo diga de entrada.
        # Sigue sin exigirse para que el estado del servicio se pueda
        # consultar aunque la clave esté mal: es lo que permite distinguir
        # "servidor caído" de "clave incorrecta".
        info["clave_ok"] = clave_valida(x_api_key)
        return info

    @web_app.post("/calentar")
    def calentar_endpoint(x_api_key: str = Header(None)):
        # La app lo llama al abrir, para que los ~24 s del arranque en frío
        # ocurran mientras la usuaria mira la pantalla de inicio y no en su
        # primera consulta. spawn() NO espera: responde al instante y la carga
        # sigue en segundo plano. Pide la clave porque enciende una GPU.
        comprobar_clave(x_api_key)
        calentar.spawn()
        return {"status": "calentando"}

    @web_app.post("/generar")
    def generar(data: dict, x_api_key: str = Header(None)):
        comprobar_clave(x_api_key)

        mensajes = data.get("mensajes", [])
        usar_adaptador = data.get("usar_adaptador", True)
        # El cliente propone, el servidor decide. Antes se usaba el valor
        # recibido tal cual: una petición podía pedir miles de tokens y la GPU
        # los generaba. La temperatura también se acota para que no llegue un
        # valor absurdo que degenere la salida.
        try:
            max_new_tokens = int(data.get("max_new_tokens", 300))
        except (TypeError, ValueError):
            max_new_tokens = 300
        max_new_tokens = max(16, min(max_new_tokens, TOPE_MAX_NEW_TOKENS))
        try:
            temperature = float(data.get("temperature", 0.3))
        except (TypeError, ValueError):
            temperature = 0.3
        temperature = max(0.0, min(temperature, 1.5))

        servidor = ModeloServidor()
        resultado = servidor.generar_texto_gpu.remote(
            mensajes=mensajes,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            usar_adaptador=usar_adaptador,
        )

        # Incluye texto + tokens_entrada, tokens_salida, truncado y segundos.
        # El cliente actual solo lee ["texto"], así que no se rompe nada.
        return resultado

    return web_app