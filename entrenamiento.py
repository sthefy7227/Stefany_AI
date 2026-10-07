# -*- coding: utf-8 -*-
"""
entrenamiento.py — Reentrenamiento del adaptador LoRA en Modal.

CÓMO SE USA
-----------
1. Subir el dataset al volumen (una sola vez, o cada vez que cambie):

       modal volume create datos-stefany
       modal volume put datos-stefany dataset_v3_train.jsonl /dataset_v3_train.jsonl
       modal volume put datos-stefany dataset_v3_val.jsonl   /dataset_v3_val.jsonl

2. Lanzar el entrenamiento:

       modal run --detach entrenamiento.py

   Con --detach sigue aunque cierres la terminal. Sin él, cerrar la terminal
   cancela el trabajo (y deja de cobrar).

3. Ver el progreso:

       modal app logs entrenamiento-stefany


POR QUÉ ESTE ARCHIVO ESTÁ SEPARADO DE app.py
--------------------------------------------
Son dos aplicaciones distintas de Modal. El servidor de inferencia
("servidor-stefany-v4") pide gpu="T4" y está DESPLEGADO, esperando peticiones.
Este entrenamiento pide gpu="A10G" y es EFÍMERO: arranca, entrena, guarda y
apaga la máquina. Tocar uno no afecta al otro, y no hay forma de que la GPU
cara se quede encendida por descuido.


QUÉ SE CORRIGE RESPECTO A LA CORRIDA ANTERIOR
---------------------------------------------
1. max_length de 768 a 2048. Medido con el tokenizador real: el 86,6 % de los
   ejemplos anteriores superaba 768, se truncaban por la derecha y se perdía
   el 91 % de los tokens de respuesta. 735 ejemplos de 1.400 se entrenaron sin
   ninguna respuesta que predecir. En el dataset nuevo, el ejemplo más largo
   mide 1.211 tokens: con 2048 no se trunca ninguno.

2. Pérdida solo sobre la respuesta. Antes se calculaba sobre el texto entero,
   incluido el system prompt, que es el 68 % de cada ejemplo y era idéntico en
   1.027 de 1.400. Por eso la loss de 0,107 no significaba lo que parecía.

3. Se entrena desde el modelo BASE, no encima del adaptador anterior, que
   arrastra la memorización que queremos eliminar.

4. El adaptador nuevo va a un repositorio distinto (v2), para poder volver
   atrás si algo sale mal.
"""

import modal

# Sin versiones fijas, a propósito.
#
# El primer intento las fijó y falló: trl 0.12.2 exige transformers<4.47.0 y
# se había fijado 4.47.1. Encontrar la combinación exacta a ciegas cuesta una
# reconstrucción de imagen por intento.
#
# Esta es la MISMA lista que usa el servidor de inferencia (app.py), que lleva
# semanas cargando el modelo sin problemas, más trl y datasets. Al instalarlas
# todas en una sola orden, pip resuelve el conjunto completo y elige versiones
# compatibles entre sí.
#
# A cambio se pierde reproducibilidad: una reconstrucción futura puede traer
# versiones distintas. Por eso la función imprime al arrancar las versiones
# que realmente se instalaron: con ese dato sí se pueden fijar después, sobre
# una combinación comprobada en vez de adivinada.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "unsloth",
        "torch",
        "peft",
        "transformers",
        "trl",
        "datasets",
        "accelerate",
        "bitsandbytes",
        "huggingface_hub",
    )
)

app = modal.App("entrenamiento-stefany", image=image)
volumen = modal.Volume.from_name("datos-stefany", create_if_missing=True)
hf_secret = modal.Secret.from_name("huggingface-secret")

MODELO_BASE = "unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit"
REPO_DESTINO = "Stefany75/lora_stefanyAI_v3"   # v1 y v2 se conservan intactos

LONGITUD_MAXIMA = 2048
LOTE = 2
ACUMULACION = 4            # lote efectivo = 8

# Ajustes CONTRA LA MEMORIZACIÓN.
#
# La corrida anterior (2 épocas, lr 2e-4, sin dropout) terminó con loss 0,0374
# y reproducía las respuestas esperadas carácter por carácter. Eso no es
# aprender la tarea: es aprender las plantillas. Ante un correo real que no
# encajaba en ninguna, el modelo degeneraba (perdía acentos, inventaba
# sufijos, saltaba al portugués).
#
# Las tres van en la misma dirección: menos pasadas sobre los mismos datos,
# pasos más pequeños y algo de ruido en los pesos.
EPOCAS = 1
TASA_APRENDIZAJE = 1e-4
DROPOUT_LORA = 0.05

# Loss esperada: entre 0,3 y 0,8. Si vuelve a bajar de 0,15, sigue
# memorizando y no conviene desplegar el adaptador.

# Marcador donde empieza la respuesta del asistente en la plantilla de chat de
# Llama 3.1. Todo lo anterior se enmascara y no cuenta para la pérdida.
MARCADOR_RESPUESTA = "<|start_header_id|>assistant<|end_header_id|>\n\n"
MARCADOR_INSTRUCCION = "<|start_header_id|>user<|end_header_id|>\n\n"


@app.function(
    gpu="A10G",          # solo para ESTE entrenamiento; el servidor sigue en T4
    volumes={"/datos": volumen},
    secrets=[hf_secret],
    timeout=60 * 60 * 4,  # 4 horas de margen sobre la hora y media prevista
)
def entrenar(subir_a_hub: bool = True):
    import os
    import json
    import torch
    import importlib.metadata as meta

    # Qué versiones se instalaron de verdad. Si la corrida sale bien, estas
    # son las que hay que fijar para poder repetirla.
    print("📦 Versiones instaladas:")
    for paquete in ("torch", "unsloth", "unsloth_zoo", "transformers", "trl",
                    "peft", "datasets", "accelerate", "bitsandbytes"):
        try:
            print(f"     {paquete:16} {meta.version(paquete)}")
        except Exception:
            print(f"     {paquete:16} (no encontrado)")

    from unsloth import FastLanguageModel
    try:
        from unsloth.chat_templates import train_on_responses_only
        hay_ayudante_mascara = True
    except ImportError:
        # Si esta versión de unsloth no trae el ayudante, se usa el collator
        # de trl, que hace lo mismo: enmascarar todo lo anterior al marcador
        # de respuesta.
        train_on_responses_only = None
        hay_ayudante_mascara = False
        print("⚠️ unsloth.chat_templates.train_on_responses_only no disponible; "
              "se usará DataCollatorForCompletionOnlyLM de trl.")
    from datasets import Dataset
    from trl import SFTTrainer, SFTConfig
    from huggingface_hub import login

    token = (os.environ.get("HF_TOKEN")
             or os.environ.get("HUGGING_FACE_HUB_TOKEN")
             or os.environ.get("HUGGINGFACE_TOKEN"))
    if not token:
        raise RuntimeError("No llegó el token de Hugging Face al contenedor.")
    login(token=token)

    # ---------- Datos ----------
    def leer(ruta):
        with open(ruta, encoding="utf-8") as f:
            return [json.loads(linea) for linea in f]

    train = leer("/datos/dataset_v3_train.jsonl")
    val = leer("/datos/dataset_v3_val.jsonl")
    print(f"📚 Entrenamiento: {len(train)} ejemplos | Validación: {len(val)}")

    # ---------- Modelo ----------
    print(f"🔄 Cargando el modelo BASE (no el adaptador anterior)...")
    modelo, tokenizador = FastLanguageModel.from_pretrained(
        model_name=MODELO_BASE,
        max_seq_length=LONGITUD_MAXIMA,
        load_in_4bit=True,
        dtype=None,
    )

    modelo = FastLanguageModel.get_peft_model(
        modelo,
        r=16,
        lora_alpha=16,
        lora_dropout=DROPOUT_LORA,
        bias="none",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth",
        random_state=3407,
    )

    def formatear(ejemplos):
        textos = [
            tokenizador.apply_chat_template(m, tokenize=False, add_generation_prompt=False)
            for m in ejemplos["messages"]
        ]
        return {"text": textos}

    ds_train = Dataset.from_list(train).map(formatear, batched=True)
    ds_val = Dataset.from_list(val).map(formatear, batched=True)

    # Comprobación de longitudes ANTES de entrenar: es exactamente lo que no
    # se hizo la vez anterior y costó la corrida.
    largos = [len(tokenizador(t)["input_ids"]) for t in ds_train["text"]]
    excedidos = sum(1 for x in largos if x > LONGITUD_MAXIMA)
    print(f"📏 Tokens por ejemplo: media {sum(largos)/len(largos):.0f} | "
          f"máx {max(largos)} | superan {LONGITUD_MAXIMA}: {excedidos}")
    if excedidos:
        raise RuntimeError(
            f"{excedidos} ejemplos superan {LONGITUD_MAXIMA} tokens y se truncarían. "
            f"Sube LONGITUD_MAXIMA o revisa el dataset antes de gastar la GPU.")

    # ---------- Entrenamiento ----------
    entrenador = SFTTrainer(
        model=modelo,
        tokenizer=tokenizador,
        train_dataset=ds_train,
        eval_dataset=ds_val,
        args=SFTConfig(
            dataset_text_field="text",
            max_seq_length=LONGITUD_MAXIMA,
            packing=False,
            per_device_train_batch_size=LOTE,
            gradient_accumulation_steps=ACUMULACION,
            num_train_epochs=EPOCAS,
            learning_rate=TASA_APRENDIZAJE,
            warmup_ratio=0.03,
            lr_scheduler_type="linear",
            logging_steps=10,
            eval_strategy="epoch",
            save_strategy="epoch",
            optim="adamw_8bit",
            weight_decay=0.01,
            seed=3407,
            fp16=not torch.cuda.is_bf16_supported(),
            bf16=torch.cuda.is_bf16_supported(),
            output_dir="/datos/salida_entrenamiento",
            report_to="none",
        ),
    )

    # LA CORRECCIÓN MÁS IMPORTANTE: la pérdida se calcula SOLO sobre la
    # respuesta del asistente. Sin esto, el 68 % de cada ejemplo es el system
    # prompt (idéntico en casi todos) y la métrica mide memorización de un
    # bloque constante en vez de aprendizaje de la tarea.
    if hay_ayudante_mascara:
        entrenador = train_on_responses_only(
            entrenador,
            instruction_part=MARCADOR_INSTRUCCION,
            response_part=MARCADOR_RESPUESTA,
        )
    else:
        from trl import DataCollatorForCompletionOnlyLM
        entrenador.data_collator = DataCollatorForCompletionOnlyLM(
            response_template=MARCADOR_RESPUESTA,
            tokenizer=tokenizador,
        )

    # Verificación de la máscara. Se hace de dos formas según el camino:
    # con el ayudante de unsloth las etiquetas ya están en el dataset; con el
    # collator de trl se generan al formar el lote, así que hay que pedirle un
    # lote de muestra.
    try:
        if hay_ayudante_mascara:
            etiquetas = entrenador.train_dataset[0]["labels"]
        else:
            lote = entrenador.data_collator([entrenador.train_dataset[0]])
            etiquetas = lote["labels"][0].tolist()

        visibles = sum(1 for x in etiquetas if x != -100)
        porcentaje = 100 * visibles / len(etiquetas)
        print(f"🎭 Máscara: {visibles} de {len(etiquetas)} tokens cuentan para la "
              f"pérdida ({porcentaje:.1f} %).")
        print("   Debe rondar el 10-25 %. Si sale ~100 %, la máscara NO se aplicó "
              "y se estaría repitiendo el error de la corrida anterior.")
        if porcentaje > 60:
            raise RuntimeError(
                f"La máscara no se aplicó ({porcentaje:.0f} % de tokens visibles). "
                f"Se entrenaría sobre el prompt entero.")
        if visibles == 0:
            raise RuntimeError(
                "La máscara ocultó TODO: el marcador de respuesta no coincide "
                "con la plantilla de chat. Revisa MARCADOR_RESPUESTA.")
    except RuntimeError:
        raise
    except Exception as e:
        print(f"⚠️ No se pudo verificar la máscara ({type(e).__name__}: {e}).")
        print("   Continúa bajo tu riesgo: vigila que la loss NO baje de 0,2, "
              "porque eso indicaría que está memorizando el prompt otra vez.")

    print("🚀 Entrenando...")
    resultado = entrenador.train()
    loss = resultado.training_loss
    print(f"✅ Entrenamiento terminado. Loss final: {loss:.4f}")
    print("   (Se calcula solo sobre las respuestas, así que es interpretable.)")
    if loss < 0.15:
        print("⚠️ AVISO: una loss tan baja indica que el modelo está memorizando "
              "las respuestas en vez de aprender la tarea. Revísalo con "
              "'modal run entrenamiento.py::probar' ANTES de desplegarlo: si las "
              "respuestas salen idénticas a las esperadas, no lo uses.")
    elif loss > 1.5:
        print("⚠️ AVISO: loss alta. Puede faltar entrenamiento; considera 2 épocas.")
    else:
        print("   Rango esperado: el modelo generaliza en vez de copiar.")

    metricas = entrenador.evaluate()
    print(f"📊 Validación: {metricas}")

    # ---------- Guardado ----------
    ruta_local = "/datos/lora_stefanyAI_v3"
    modelo.save_pretrained(ruta_local)
    tokenizador.save_pretrained(ruta_local)
    volumen.commit()
    print(f"💾 Adaptador guardado en el volumen: {ruta_local}")

    if subir_a_hub:
        print(f"☁️ Subiendo a {REPO_DESTINO}...")
        modelo.push_to_hub(REPO_DESTINO, token=token, private=True)
        tokenizador.push_to_hub(REPO_DESTINO, token=token, private=True)
        print(f"✅ Subido. Para usarlo, cambia RUTA_ADAPTADOR en app.py a:")
        print(f'   RUTA_ADAPTADOR = "{REPO_DESTINO}"')

    return {"loss": resultado.training_loss, "validacion": metricas,
            "repo": REPO_DESTINO}


@app.function(
    gpu="A10G",
    volumes={"/datos": volumen},
    secrets=[hf_secret],
    timeout=60 * 20,
)
def probar(n: int = 8):
    """Prueba rápida del adaptador recién entrenado.

        modal run entrenamiento.py::probar

    Genera respuestas para ejemplos del conjunto de validación y las muestra
    junto a la esperada. Sirve para ver si copia, si inventa fechas o si
    escribe análisis donde no toca, antes de desplegarlo.
    """
    import os, json, torch
    from unsloth import FastLanguageModel

    modelo, tokenizador = FastLanguageModel.from_pretrained(
        model_name="/datos/lora_stefanyAI_v3",
        max_seq_length=LONGITUD_MAXIMA,
        load_in_4bit=True,
    )
    FastLanguageModel.for_inference(modelo)

    with open("/datos/dataset_v3_val.jsonl", encoding="utf-8") as f:
        val = [json.loads(l) for l in f]

    for ejemplo in val[:n]:
        mensajes = ejemplo["messages"][:2]
        esperada = ejemplo["messages"][2]["content"]
        prompt = tokenizador.apply_chat_template(
            mensajes, tokenize=False, add_generation_prompt=True)
        entradas = tokenizador(prompt, return_tensors="pt").to("cuda")
        with torch.no_grad():
            salida = modelo.generate(**entradas, max_new_tokens=300,
                                     temperature=0.3, do_sample=True)
        generada = tokenizador.decode(
            salida[0][entradas["input_ids"].shape[1]:], skip_special_tokens=True)

        print("=" * 78)
        print(f"TIPO: {ejemplo['meta']['tipo']}")
        print(f"--- ESPERADA ---\n{esperada}")
        print(f"--- GENERADA ---\n{generada.strip()}")


@app.local_entrypoint()
def main():
    resultado = entrenar.remote()
    print("\n" + "=" * 60)
    print("RESULTADO:", resultado)
    print("Siguiente paso: revisa las muestras con")
    print("   modal run entrenamiento.py::probar")
