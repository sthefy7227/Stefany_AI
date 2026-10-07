# -*- coding: utf-8 -*-
"""
cliente_stefany.py — Cliente HTTP optimizado para el servidor en Modal.
"""

import requests


class ClienteStefanyError(Exception):
    pass


class ClienteStefany:
    def __init__(self, url_base, api_key, timeout=180):
        self.url_base = url_base.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = timeout
        self._headers = {
            "X-API-Key": self.api_key,
            "User-Agent": "StefanyClient/1.0",
            "Content-Type": "application/json",
        }

    def estado(self):
        """Estado del servicio, o None si no se pudo consultar.

        Devuelve {'status', 'modelo_caliente', 'contenedores'}: el servicio
        responde siempre desde una función de CPU, pero la GPU puede estar
        apagada. La interfaz necesita esa diferencia para no decir "Conectado"
        cuando la primera consulta va a tardar un minuto cargando el modelo.
        """
        try:
            r = requests.get(f"{self.url_base}/estado", headers=self._headers, timeout=10)
            if r.status_code != 200:
                return None
            return r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"Error de conexión en estado: {e}")
            return None

    def verificar_estado(self):
        """True si el servicio responde (sin mirar si la GPU está caliente)."""
        return self.estado() is not None

    def calentar(self):
        """Pide al servidor que cargue el modelo en la GPU, sin esperar a que termine.

        Es una optimización, no un requisito: si falla, la primera consulta
        simplemente tarda más. Por eso no lanza excepciones.
        """
        try:
            r = requests.post(f"{self.url_base}/calentar", headers=self._headers, timeout=15)
            return r.status_code == 200
        except requests.RequestException as e:
            print(f"No se pudo precalentar el modelo: {e}")
            return False

    def generar(self, mensajes, usar_adaptador=True, max_new_tokens=300, temperature=0.3):
        cuerpo = {
            "mensajes": mensajes,
            "usar_adaptador": usar_adaptador,
            "max_new_tokens": max_new_tokens,
            "temperature": temperature,
        }
        try:
            r = requests.post(f"{self.url_base}/generar", json=cuerpo,
                              headers=self._headers, timeout=self.timeout)
        except requests.exceptions.Timeout:
            raise ClienteStefanyError("El servidor tardó demasiado en responder. Intenta de nuevo.")
        except requests.exceptions.ConnectionError:
            raise ClienteStefanyError("No se pudo conectar al servidor. Revisa tu conexión.")
        except requests.RequestException as e:
            raise ClienteStefanyError(f"Error de red: {e}")

        if r.status_code == 401:
            raise ClienteStefanyError("Clave de API incorrecta.")
        if r.status_code != 200:
            raise ClienteStefanyError(f"El servidor respondió con un error ({r.status_code}).")

        try:
            data = r.json()
            return data["texto"]
        except (ValueError, KeyError):
            raise ClienteStefanyError("Respuesta inválida del servidor.")
