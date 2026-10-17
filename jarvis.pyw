import os
import sys
import time
import io
import re
import json
import asyncio
import webbrowser
import datetime
import urllib.parse
import urllib.request
import urllib.error
import subprocess
import threading
import sounddevice as sd
import numpy as np
import soundfile as sf
import edge_tts
import pygame
import ollama
import pyautogui
import pyperclip
import tkinter as tk
from tkinter import scrolledtext

# Importar motor de PowerPoint
try:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
except ImportError:
    pass

# ========================================================
# --- CONFIGURACIÓN GENERAL ---
# ========================================================
VOZ_JARVIS = "es-ES-AlvaroNeural"  # Tono formal tipo JARVIS
UMBRAL_VOZ = 1400                 # Umbral seguro contra ventiladores
MEMORIA_CONVERSACION = []         # Memoria de corto plazo
ULTIMA_PRESENTACION = None        # Guarda última presentación
ES_MAC = sys.platform == "darwin"
ES_WINDOWS = sys.platform == "win32"

pygame.mixer.init()
pygame.mixer.music.set_volume(1.0)

# ========================================================
# --- DETECCIÓN UNIVERSAL DE ESCRITORIO (MAC / WINDOWS) ---
# ========================================================
def obtener_ruta_escritorio():
    """Detecta el escritorio real en Windows (con OneDrive) o en Mac."""
    if ES_WINDOWS:
        try:
            import winreg
            clave = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
            ruta, _ = winreg.QueryValueEx(clave, "Desktop")
            return os.path.expandvars(ruta)
        except Exception:
            onedrive_desktop = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop")
            if os.path.exists(onedrive_desktop):
                return onedrive_desktop
            return os.path.join(os.path.expanduser("~"), "Desktop")
    else:
        # En Mac / Linux siempre es ~/Desktop
        return os.path.join(os.path.expanduser("~"), "Desktop")

def abrir_archivo_universal(ruta):
    """Abre archivos con la app predeterminada en Windows o Mac."""
    if ES_WINDOWS:
        try:
            os.startfile(ruta)
        except Exception:
            subprocess.Popen(f'start "" "{ruta}"', shell=True)
    elif ES_MAC:
        subprocess.Popen(["open", ruta])
    else:
        subprocess.Popen(["xdg-open", ruta])

# ========================================================
# --- CALIBRACIÓN AUTOMÁTICA DEL MICRÓFONO ---
# ========================================================
def calibrar_umbral():
    fs = 16000
    try:
        muestra = sd.rec(int(0.8 * fs), samplerate=fs, channels=1, dtype='int16')
        sd.wait()
        ruido_base = np.max(np.abs(muestra))
        nuevo = max(1400, int(ruido_base * 2.2))
        return min(nuevo, 6000)
    except Exception:
        return 1500

# ========================================================
# --- MOTOR DE TRANSCRIPCIÓN (GOOGLE EN MEMORIA) ---
# ========================================================
def transcribir_audio(audio_np, fs=16000):
    try:
        buffer = io.BytesIO()
        sf.write(buffer, audio_np, fs, format='FLAC')
        flac_data = buffer.getvalue()

        url = "http://www.google.com/speech-api/v2/recognize?client=chromium&lang=es-ES&key=AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw"
        req = urllib.request.Request(
            url,
            data=flac_data,
            headers={
                "Content-Type": f"audio/x-flac; rate={fs}",
                "User-Agent": "Mozilla/5.0"
            }
        )
        with urllib.request.urlopen(req, timeout=12) as response:
            respuesta_cruda = response.read().decode('utf-8')
            for linea in respuesta_cruda.split("\n"):
                if not linea.strip():
                    continue
                data = json.loads(linea)
                if "result" in data and len(data["result"]) > 0:
                    alternativas = data["result"][0].get("alternative", [])
                    if alternativas:
                        return alternativas[0].get("transcript", "").lower()
        return ""
    except Exception:
        return ""

# ========================================================
# --- VOZ (TEXT-TO-SPEECH EN MEMORIA RAM) ---
# ========================================================
def hablar(texto, gui=None):
    if gui:
        gui.ocupado = True
        gui.agregar_log("JARVIS", texto)
        gui.cambiar_estado("🔊 RESPONDIENDO...", "#00FFAA")

    async def generar_audio_bytes():
        communicate = edge_tts.Communicate(texto, VOZ_JARVIS)
        audio_stream = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_stream.extend(chunk["data"])
        return bytes(audio_stream)

    try:
        audio_bytes = asyncio.run(generar_audio_bytes())
        buffer = io.BytesIO(audio_bytes)

        try:
            pygame.mixer.music.load(buffer, "mp3")
        except Exception:
            import tempfile
            ruta = os.path.join(tempfile.gettempdir(), f"j_{time.time_ns()}.mp3")
            with open(ruta, "wb") as f:
                f.write(audio_bytes)
            pygame.mixer.music.load(ruta)

        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)

        pygame.mixer.music.unload()
        time.sleep(0.4)

    except Exception as e:
        if gui:
            gui.agregar_log("SISTEMA", f"Error de voz: {e}")

# ========================================================
# --- OÍDO CON DETECCIÓN DE SILENCIO ---
# ========================================================
def escuchar_orden(gui=None):
    fs = 16000
    bloque_segundos = 0.3
    bloque_muestras = int(fs * bloque_segundos)

    silencio_corte = 0.9
    espera_inicio_max = 5.0
    tiempo_max_seguridad = 18.0

    if gui:
        gui.cambiar_estado("🔴 ESCUCHANDO...", "#FF3344")

    fragmentos = []
    usuario_hablo = False
    segundos_silencio = 0.0
    segundos_espera = 0.0
    tiempo_acumulado = 0.0

    try:
        while True:
            bloque = sd.rec(bloque_muestras, samplerate=fs, channels=1, dtype='int16')
            sd.wait()
            fragmentos.append(bloque)
            tiempo_acumulado += bloque_segundos

            volumen_bloque = np.max(np.abs(bloque))

            if volumen_bloque > UMBRAL_VOZ:
                if not usuario_hablo and gui:
                    gui.cambiar_estado("🎙️ CAPTANDO TU VOZ...", "#00FFAA")
                usuario_hablo = True
                segundos_silencio = 0.0
            else:
                if usuario_hablo:
                    segundos_silencio += bloque_segundos
                    if segundos_silencio >= silencio_corte:
                        break
                else:
                    segundos_espera += bloque_segundos
                    if segundos_espera >= espera_inicio_max:
                        return ""

            if tiempo_acumulado >= tiempo_max_seguridad:
                break

        if gui:
            gui.cambiar_estado("⚡ PROCESANDO TU ORDEN...", "#FFB800")

        audio_completo = np.concatenate(fragmentos, axis=0)
        texto = transcribir_audio(audio_completo, fs)
        if texto and gui:
            gui.agregar_log("TÚ", texto)
        return texto

    except Exception as e:
        if gui:
            gui.agregar_log("SISTEMA", f"Error en micrófono: {e}")
        return ""

# ========================================================
# --- PARSEADOR DE POWERPOINT ---
# ========================================================
def parsear_presentacion_texto(texto_crudo, tema_solicitado):
    lineas = texto_crudo.strip().split("\n")
    titulo = f"Presentación: {tema_solicitado.title()}"
    subtitulo = "Análisis e información destacada"
    diapositivas = []
    diapositiva_actual = None

    for linea in lineas:
        l = linea.strip()
        if not l:
            continue

        if l.upper().startswith("TITULO:") or l.upper().startswith("TÍTULO:"):
            titulo = l.split(":", 1)[1].strip()
        elif l.upper().startswith("SUBTITULO:") or l.upper().startswith("SUBTÍTULO:"):
            subtitulo = l.split(":", 1)[1].strip()
        elif l.upper().startswith("DIAPOSITIVA:") or l.upper().startswith("DIAPOSITIVA "):
            if diapositiva_actual and diapositiva_actual["puntos"]:
                diapositivas.append(diapositiva_actual)
            tit_clean = l.split(":", 1)[1].strip() if ":" in l else l
            diapositiva_actual = {"titulo": tit_clean, "puntos": [], "guion": ""}
        elif (l.startswith("-") or l.startswith("•") or l.startswith("*")) and diapositiva_actual is not None:
            punto = re.sub(r'^[-•*]\s*', '', l).strip()
            if punto:
                diapositiva_actual["puntos"].append(punto)
        elif (l.upper().startswith("GUION:") or l.upper().startswith("GUIÓN:")) and diapositiva_actual is not None:
            diapositiva_actual["guion"] = l.split(":", 1)[1].strip()

    if diapositiva_actual and diapositiva_actual["puntos"]:
        diapositivas.append(diapositiva_actual)

    if len(diapositivas) < 2:
        diapositivas = [
            {
                "titulo": f"1. Introducción: {tema_solicitado.title()}",
                "puntos": [f"Conceptos y fundamentos esenciales sobre {tema_solicitado}.", "Contexto general y relevancia."],
                "guion": f"Comenzamos con la introducción sobre {tema_solicitado}."
            },
            {
                "titulo": "2. Desarrollo Principal",
                "puntos": ["Análisis de los elementos clave.", "Aspectos destacados y trascendencia."],
                "guion": "Avanzando en el tema, exploramos los puntos más destacados."
            }
        ]

    return {"titulo": titulo, "subtitulo": subtitulo, "diapositivas": diapositivas}

# ========================================================
# --- SUPERPODER POWERPOINT ---
# ========================================================
def crear_presentacion_powerpoint(tema, gui=None):
    global ULTIMA_PRESENTACION
    hablar(f"Entendido señor, estoy investigando y diseñando la presentación sobre {tema}. Un momento por favor.", gui)
    if gui:
        gui.cambiar_estado("🎨 DISEÑANDO DIAPOSITIVAS...", "#FFB800")

    prompt_pptx = f"""Eres un experto orador y creador de presentaciones ejecutivas.
Crea el contenido de 4 diapositivas profesionales sobre el tema: "{tema}".

Responde ÚNICAMENTE con este formato de texto:

TITULO: (Título sobre {tema})
SUBTITULO: (Subtítulo elegante)

DIAPOSITIVA: 1. Introducción y Contexto
- Primer punto clave
- Segundo punto clave
GUION: Explicación oral de dos oraciones.

DIAPOSITIVA: 2. Desarrollo y Puntos Principales
- Primer punto clave
- Segundo punto clave
GUION: Explicación oral de dos oraciones.

DIAPOSITIVA: 3. Impacto y Relevancia
- Primer punto clave
- Segundo punto clave
GUION: Explicación oral de dos oraciones.

DIAPOSITIVA: 4. Conclusiones y Legado
- Primer punto clave
- Segundo punto clave
GUION: Explicación oral de cierre.
"""

    try:
        respuesta = ollama.chat(
            model='llama3.2',
            messages=[{'role': 'user', 'content': prompt_pptx}],
            options={'temperature': 0.7}
        )
        datos = parsear_presentacion_texto(respuesta['message']['content'].strip(), tema)
    except Exception as e:
        if gui:
            gui.agregar_log("SISTEMA", f"Aviso de IA: {e}")
        datos = parsear_presentacion_texto("", tema)

    try:
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)

        COLOR_FONDO = RGBColor(11, 15, 25)
        COLOR_CIAN = RGBColor(0, 229, 255)
        COLOR_TEXTO = RGBColor(241, 245, 249)
        COLOR_GRIS = RGBColor(148, 163, 184)

        slide_layout = prs.slide_layouts[6]
        slide_portada = prs.slides.add_slide(slide_layout)
        slide_portada.background.fill.solid()
        slide_portada.background.fill.fore_color.rgb = COLOR_FONDO

        tx_box = slide_portada.shapes.add_textbox(Inches(1.5), Inches(2.2), Inches(10.33), Inches(3.0))
        tf = tx_box.text_frame
        tf.word_wrap = True

        p_tit = tf.paragraphs[0]
        p_tit.text = datos.get("titulo", tema.upper())
        p_tit.font.name = "Arial"
        p_tit.font.size = Pt(44)
        p_tit.font.bold = True
        p_tit.font.color.rgb = COLOR_CIAN
        p_tit.alignment = PP_ALIGN.CENTER

        p_sub = tf.add_paragraph()
        p_sub.text = datos.get("subtitulo", "Presentación de Alto Nivel")
        p_sub.font.name = "Arial"
        p_sub.font.size = Pt(20)
        p_sub.font.color.rgb = COLOR_GRIS
        p_sub.space_before = Pt(20)
        p_sub.alignment = PP_ALIGN.CENTER

        for d in datos.get("diapositivas", []):
            slide = prs.slides.add_slide(slide_layout)
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = COLOR_FONDO

            box_header = slide.shapes.add_textbox(Inches(1.0), Inches(0.8), Inches(11.33), Inches(1.0))
            tf_h = box_header.text_frame
            p_h = tf_h.paragraphs[0]
            p_h.text = d.get("titulo", "Punto Clave")
            p_h.font.name = "Arial"
            p_h.font.size = Pt(28)
            p_h.font.bold = True
            p_h.font.color.rgb = COLOR_CIAN

            box_content = slide.shapes.add_textbox(Inches(1.0), Inches(2.0), Inches(11.33), Inches(4.8))
            tf_c = box_content.text_frame
            tf_c.word_wrap = True

            puntos = d.get("puntos", [])
            for idx, punto in enumerate(puntos):
                p_c = tf_c.paragraphs[0] if idx == 0 else tf_c.add_paragraph()
                p_c.text = f"•  {punto}"
                p_c.font.name = "Arial"
                p_c.font.size = Pt(20)
                p_c.font.color.rgb = COLOR_TEXTO
                p_c.space_after = Pt(22)

        escritorio = obtener_ruta_escritorio()
        tema_seguro = re.sub(r'[^a-zA-Z0-9_]', '_', tema)[:25]
        nombre_archivo = f"Presentacion_{tema_seguro}.pptx"
        ruta_completa = os.path.join(escritorio, nombre_archivo)
        prs.save(ruta_completa)

        ULTIMA_PRESENTACION = {"ruta": ruta_completa, "datos": datos}
        if gui:
            gui.agregar_log("SISTEMA", f"📊 Presentación guardada en:\n{ruta_completa}")

        abrir_archivo_universal(ruta_completa)
        return ULTIMA_PRESENTACION

    except Exception as e:
        if gui:
            gui.agregar_log("SISTEMA", f"Error creando PowerPoint: {e}")
        hablar("Ocurrió un error al compilar el archivo de diapositivas, señor.", gui)
        return None

def realizar_exposicion(gui=None):
    global ULTIMA_PRESENTACION
    if not ULTIMA_PRESENTACION:
        hablar("No tengo ninguna presentación en memoria para exponer, señor.", gui)
        return

    datos = ULTIMA_PRESENTACION["datos"]
    diapositivas = datos.get("diapositivas", [])

    hablar(f"Iniciando la exposición sobre: {datos.get('titulo', 'el tema')}.", gui)
    time.sleep(1.0)

    for i, d in enumerate(diapositivas, 1):
        titulo = d.get("titulo", f"Diapositiva {i}")
        guion = d.get("guion", "")
        pyautogui.press('right')
        time.sleep(0.5)
        hablar(f"{titulo}. {guion}", gui)
        time.sleep(0.8)

    hablar("Con esto concluye la exposición de hoy, señor.", gui)

# ========================================================
# --- SUPERPODER SPOTIFY Y WHATSAPP ---
# ========================================================
def reproducir_en_spotify(busqueda):
    if ES_WINDOWS:
        os.system('start spotify:')
    elif ES_MAC:
        os.system('open -a Spotify')
    time.sleep(1.2)
    
    # Atajo Ctrl+K en Windows o Cmd+K en Mac
    modificador = 'command' if ES_MAC else 'ctrl'
    pyautogui.hotkey(modificador, 'k')
    time.sleep(0.4)
    pyperclip.copy(busqueda)
    pyautogui.hotkey(modificador, 'v')
    time.sleep(0.7)
    pyautogui.press('enter')

def enfocar_whatsapp_web():
    webbrowser.open("https://web.whatsapp.com")
    time.sleep(3.0)

def buscar_chat_whatsapp_web(contacto):
    enfocar_whatsapp_web()
    pyautogui.press('esc')
    time.sleep(0.2)
    mod = 'command' if ES_MAC else 'ctrl'
    pyautogui.hotkey(mod, 'alt', 'n')
    time.sleep(0.8)
    pyperclip.copy(contacto)
    pyautogui.hotkey(mod, 'v')
    time.sleep(1.2)
    pyautogui.press('enter')
    time.sleep(0.8)

def enviar_mensaje_whatsapp_web(contacto, mensaje):
    buscar_chat_whatsapp_web(contacto)
    pyperclip.copy(mensaje)
    mod = 'command' if ES_MAC else 'ctrl'
    pyautogui.hotkey(mod, 'v')
    time.sleep(0.4)
    pyautogui.press('enter')

# ========================================================
# --- CONTROL DE SISTEMA UNIVERSAL (WINDOWS Y MAC) ---
# ========================================================
def ejecutar_en_sistema(comando, gui=None):
    """Ejecuta en PowerShell (Windows) o en Bash/Zsh (Mac)."""
    if gui:
        gui.agregar_log("SISTEMA", f"⚙️ Ejecutando orden:\n{comando}")

    try:
        if ES_WINDOWS:
            script_ps = f"""
$Escritorio = [Environment]::GetFolderPath('Desktop')
$Documentos = [Environment]::GetFolderPath('MyDocuments')
$Descargas  = Join-Path $env:USERPROFILE 'Downloads'
{comando}
"""
            res = subprocess.run(["powershell", "-NoProfile", "-Command", script_ps], capture_output=True, text=True, timeout=20)
        else:
            # En Mac ejecuta en la terminal Zsh nativa
            res = subprocess.run(comando, shell=True, executable="/bin/zsh", capture_output=True, text=True, timeout=20)

        salida = res.stdout.strip()
        error = res.stderr.strip()
        if res.returncode != 0 and error:
            return False, error
        return True, salida
    except Exception as e:
        return False, str(e)

# ========================================================
# --- CEREBRO AGENTE DE CONTROL TOTAL ---
# ========================================================
def razonar_y_ejecutar(orden, gui):
    global MEMORIA_CONVERSACION
    gui.cambiar_estado("🧠 PENSANDO...", "#FFB800")

    sistema_op = "macOS" if ES_MAC else "Windows"
    prompt_sistema = f"""Eres JARVIS, el sistema de IA con CONTROL TOTAL de la computadora ({sistema_op}) del usuario.
Responde en formato JSON puro:
{{
    "voz": "Frase breve y formal en español llamando 'señor' al usuario.",
    "comando": "Comando de terminal para {sistema_op} si pide alguna acción en el equipo (o null si es solo charla).",
    "crear_presentacion": "Tema para diapositivas si el usuario lo pidió (o null si no pidió diapositivas)."
}}"""

    try:
        mensajes = [{'role': 'system', 'content': prompt_sistema}]
        for m in MEMORIA_CONVERSACION[-6:]:
            mensajes.append(m)
        mensajes.append({'role': 'user', 'content': orden})

        response = ollama.chat(
            model='llama3.2',
            format='json',
            messages=mensajes,
            options={'temperature': 0.6}
        )
        
        datos = json.loads(response['message']['content'])
        voz_jarvis = datos.get("voz", "A su servicio, señor.")
        comando_sys = datos.get("comando", None)
        tema_pptx = datos.get("crear_presentacion", None)

        if tema_pptx and str(tema_pptx).strip().lower() not in ["null", "none", "", "false"]:
            pres = crear_presentacion_powerpoint(str(tema_pptx).strip(), gui)
            if pres:
                hablar(f"He diseñado y abierto la presentación sobre {tema_pptx} en su pantalla, señor.", gui)
                return

        if comando_sys and str(comando_sys).strip().lower() not in ["null", "none", "", "false"]:
            exito, salida = ejecutar_en_sistema(str(comando_sys).strip(), gui)
            if not exito:
                hablar("Disculpe señor, no pude completar la acción en el sistema.", gui)
                return

        MEMORIA_CONVERSACION.append({'role': 'user', 'content': orden})
        MEMORIA_CONVERSACION.append({'role': 'assistant', 'content': voz_jarvis})
        hablar(voz_jarvis, gui)

    except Exception as e:
        gui.agregar_log("SISTEMA", f"Error en cerebro: {e}")
        hablar("Disculpe señor, ocurrió una interferencia en mis subrutinas.", gui)

# ========================================================
# --- PROCESADOR DE ACCIONES ---
# ========================================================
def procesar_comandos(orden, gui):
    orden = orden.replace(",", "").replace(".", "").strip()
    for pref in ["oye jarvis ", "hey jarvis ", "ok jarvis ", "jarvis ", "yarbis ", "yarbiss ", "yarvis "]:
        if orden.startswith(pref):
            orden = orden[len(pref):].strip()
            break

    # PowerPoint
    es_pedido_pptx = any(w in orden for w in ["diapositiva", "diapositivas", "presentacion", "presentación", "powerpoint", "power point"])
    es_accion_crear = any(v in orden for v in ["haz", "crea", "prepara", "arma", "muestra", "muéstrame", "dame", "saca"])

    if es_pedido_pptx and es_accion_crear:
        tema = orden
        for v in ["hazme ", "créame ", "preparame ", "haz ", "crea ", "prepara ", "arma ", "muéstrame ", "muestra ", "dame "]:
            tema = tema.replace(v, " ")
        for ref in ["en power point", "en powerpoint", "powerpoint", "power point", "unas diapositivas", "una diapositiva", "diapositivas", "diapositiva", "una presentación", "presentación", "presentacion"]:
            tema = tema.replace(ref, " ")

        exponer_de_una = any(exp in tema for exp in [" y haz la exposición", " y exponla", " y presenta", " y expón"])
        for exp in [" y haz la exposición", " y exponla", " y presenta", " y expón"]:
            tema = tema.replace(exp, "").strip()

        tema = re.sub(r'\s+', ' ', tema).strip()
        for con in ["acerca de ", "sobre ", "de la vida de ", "de la ", "del ", "de "]:
            if tema.startswith(con):
                tema = tema[len(con):].strip()
                break

        if len(tema) > 2:
            pres = crear_presentacion_powerpoint(tema, gui)
            if pres:
                if exponer_de_una:
                    realizar_exposicion(gui)
                else:
                    hablar(f"He diseñado y abierto la presentación sobre {tema} en su pantalla, señor.", gui)
            return True

    if any(f in orden for f in ["haz la exposición", "expón las diapositivas", "expón la presentación"]):
        realizar_exposicion(gui)
        return True

    # Minimizar
    if any(f in orden for f in ["minimízate", "minimizate", "minimiza", "ocúltate", "ocultate"]):
        hablar("Ocultando interfaz, señor. Seguiré atento en segundo plano.", gui)
        gui.minimizar()
        return True

    # WhatsApp
    if any(frase in orden for frase in ["abre whatsapp", "abrir whatsapp", "abre whatsapp web"]):
        hablar("Abriendo WhatsApp Web, señor.", gui)
        enfocar_whatsapp_web()
        return True

    activadores_msg = ["mándale un mensaje a ", "mandale un mensaje a ", "envíale un mensaje a ", "manda un mensaje a ", "envía un mensaje a "]
    for act in activadores_msg:
        if act in orden:
            resto = orden.split(act, 1)[1].replace(" por whatsapp", "").strip()
            conectores = [" que diga ", " diciendo ", " que dice "]
            contacto, mensaje = None, None
            for con in conectores:
                if con in resto:
                    partes = resto.split(con, 1)
                    contacto, mensaje = partes[0].strip(), partes[1].strip()
                    break
            if not contacto:
                contacto = resto.strip()
            for rel in ["mi ", "al ", "a "]:
                if contacto.startswith(rel):
                    contacto = contacto[len(rel):].strip()

            if contacto:
                if not mensaje:
                    hablar(f"¿Qué mensaje desea enviarle a {contacto}, señor?", gui)
                    mensaje = escuchar_orden(gui)
                    if not mensaje:
                        hablar("Operación cancelada, señor.", gui)
                        return True
                hablar(f"Enviando mensaje a {contacto}, señor.", gui)
                enviar_mensaje_whatsapp_web(contacto, mensaje)
                return True

    # Spotify
    if "spotify" in orden:
        termino = orden.replace("en la app de spotify", "").replace("en spotify", "").replace("spotify", "").strip()
        for p in ["pon la canción de", "pon una canción de", "pon música de", "reproduce a", "reproduce", "pon"]:
            if termino.startswith(p):
                termino = termino[len(p):].strip()
                break
        if termino in ["", "una canción", "musica", "música", "algo"]:
            hablar("Reanudando música en Spotify, señor.", gui)
            pyautogui.press('playpause')
            return True
        hablar(f"Reproduciendo {termino} en Spotify de inmediato, señor.", gui)
        reproducir_en_spotify(termino)
        return True

    # Controles Multimedia
    if any(frase in orden for frase in ["pausa la música", "pausar la música", "reanuda la música"]):
        pyautogui.press('playpause')
        hablar("Música en pausa o reanudada, señor.", gui)
        return True

    if any(frase in orden for frase in ["siguiente canción", "pasa la canción"]):
        pyautogui.press('nexttrack')
        hablar("Siguiente canción, señor.", gui)
        return True

    if "hora" in orden:
        hablar(f"Son las {datetime.datetime.now().strftime('%I:%M %p')}, señor.", gui)
        return True
    elif "fecha" in orden or "qué día es" in orden:
        hablar(f"Hoy es {datetime.datetime.now().strftime('%d de %B del %Y')}, señor.", gui)
        return True

    elif any(p in orden for p in ["apágate", "salir"]):
        hablar("Apagando sistemas locales. Que tenga un buen día, señor.", gui)
        sys.exit()

    return False

# ========================================================
# --- INTERFAZ GRÁFICA STARK ---
# ========================================================
class JarvisGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("J.A.R.V.I.S. - Sistema de Control y Presentaciones")
        self.root.geometry("540x680")
        self.root.minsize(540, 680)
        self.root.maxsize(540, 680)
        self.root.configure(bg="#0B0E14")
        self.root.bind("<Map>", self._al_restaurar_ventana)

        self.ocupado = True
        self.manos_libres = True

        lbl_titulo = tk.Label(root, text="J . A . R . V . I . S", font=("Consolas", 20, "bold"), fg="#00E5FF", bg="#0B0E14")
        lbl_titulo.pack(pady=(18, 5))

        lbl_sub = tk.Label(root, text="SISTEMA UNIVERSAL (WINDOWS & MACOS)", font=("Consolas", 8), fg="#4B5E78", bg="#0B0E14")
        lbl_sub.pack(pady=(0, 10))

        self.canvas = tk.Canvas(root, width=150, height=150, bg="#0B0E14", highlightthickness=0)
        self.canvas.pack(pady=5)
        self.canvas.create_oval(10, 10, 140, 140, outline="#162235", width=4)
        self.anillo = self.canvas.create_oval(25, 25, 125, 125, outline="#00E5FF", width=3)
        self.nucleo = self.canvas.create_oval(50, 50, 100, 100, fill="#00E5FF", outline="")

        self.lbl_estado = tk.Label(root, text="⚙️ CALIBRANDO MICRÓFONO...", font=("Consolas", 10, "bold"), fg="#FFB800", bg="#0B0E14")
        self.lbl_estado.pack(pady=(10, 10))

        frame_btn = tk.Frame(root, bg="#0B0E14")
        frame_btn.pack(pady=5)

        self.btn_hablar = tk.Button(
            frame_btn, text="🎙️ HABLAR MANUALMENTE", font=("Consolas", 10, "bold"),
            bg="#00E5FF", fg="#0B0E14", activebackground="#00B8CC", relief="flat",
            padx=15, pady=8, cursor="hand2", command=self.hablar_manualmente
        )
        self.btn_hablar.pack(side="left", padx=5)

        self.btn_toggle = tk.Button(
            frame_btn, text="🟢 MANOS LIBRES: ON", font=("Consolas", 10, "bold"),
            bg="#162235", fg="#00FFAA", relief="flat", padx=10, pady=8,
            cursor="hand2", command=self.toggle_manos_libres
        )
        self.btn_toggle.pack(side="left", padx=5)

        frame_log = tk.Frame(root, bg="#111622", padx=5, pady=5)
        frame_log.pack(padx=20, pady=(10, 15), fill="both", expand=True)

        self.txt_log = scrolledtext.ScrolledText(
            frame_log, wrap=tk.WORD, font=("Consolas", 9),
            bg="#111622", fg="#E0E6ED", insertbackground="white", relief="flat"
        )
        self.txt_log.pack(fill="both", expand=True)
        self.txt_log.tag_config("JARVIS", foreground="#00E5FF", font=("Consolas", 9, "bold"))
        self.txt_log.tag_config("TÚ", foreground="#FFB800", font=("Consolas", 9, "bold"))
        self.txt_log.tag_config("SISTEMA", foreground="#70829B", font=("Consolas", 9, "italic"))

        threading.Thread(target=self._arranque_secuencia, daemon=True).start()

    def _al_restaurar_ventana(self, event):
        if event.widget == self.root:
            self.root.geometry("540x680")
            self.root.update_idletasks()

    def _arranque_secuencia(self):
        global UMBRAL_VOZ
        time.sleep(0.4)
        UMBRAL_VOZ = calibrar_umbral()
        self.agregar_log("SISTEMA", f"Micrófono calibrado (Filtro: {UMBRAL_VOZ}).")

        hablar("Sistemas universales en línea. A su servicio, señor.", self)

        self.ocupado = False
        self.cambiar_estado("🟢 MANOS LIBRES ACTIVO // DI 'JARVIS'", "#00E5FF")
        threading.Thread(target=self._hilo_manos_libres, daemon=True).start()

    def traer_al_frente(self):
        self.root.after(0, self._traer_al_frente_ui)

    def _traer_al_frente_ui(self):
        self.root.deiconify()
        self.root.state('normal')
        self.root.geometry("540x680")
        self.root.attributes('-topmost', True)
        self.root.update_idletasks()
        self.root.attributes('-topmost', False)
        self.root.lift()
        self.root.focus_force()

    def minimizar(self):
        self.root.after(0, self.root.iconify)

    def toggle_manos_libres(self):
        self.manos_libres = not self.manos_libres
        if self.manos_libres:
            self.btn_toggle.config(text="🟢 MANOS LIBRES: ON", fg="#00FFAA")
            self.cambiar_estado("🟢 MANOS LIBRES ACTIVO // DI 'JARVIS'", "#00E5FF")
        else:
            self.btn_toggle.config(text="🔴 MANOS LIBRES: OFF", fg="#FF3344")
            self.cambiar_estado("MODO MANUAL // PRESIONA EL BOTÓN", "#70829B")

    def cambiar_estado(self, texto, color):
        self.root.after(0, lambda: self._actualizar_estado_ui(texto, color))

    def _actualizar_estado_ui(self, texto, color):
        self.lbl_estado.config(text=texto, fg=color)
        self.canvas.itemconfig(self.anillo, outline=color)
        self.canvas.itemconfig(self.nucleo, fill=color)

    def agregar_log(self, autor, mensaje):
        self.root.after(0, lambda: self._agregar_log_ui(autor, mensaje))

    def _agregar_log_ui(self, autor, mensaje):
        self.txt_log.insert(tk.END, f"[{autor}]: ", autor)
        self.txt_log.insert(tk.END, f"{mensaje}\n\n")
        self.txt_log.see(tk.END)

    def hablar_manualmente(self):
        if not self.ocupado:
            threading.Thread(target=lambda: self._iniciar_sesion_conversacion(None), daemon=True).start()

    def _iniciar_sesion_conversacion(self, primera_orden=None):
        self.ocupado = True
        orden_actual = primera_orden

        if not orden_actual:
            hablar("¿Sí, señor?", self)
            orden_actual = escuchar_orden(self)

        while orden_actual:
            despedidas = ["gracias", "listo", "adiós", "adios", "chao", "hasta luego", "nada más", "nada mas", "eso es todo", "ya es todo"]
            if any(p in orden_actual for p in despedidas):
                hablar("A su servicio, señor. Estaré atento si me necesita.", self)
                break

            ejecutado = procesar_comandos(orden_actual, self)
            if not ejecutado:
                razonar_y_ejecutar(orden_actual, self)

            time.sleep(0.3)
            self.cambiar_estado("💬 CONVERSACIÓN ACTIVA (TE ESCUCHO...)", "#00FFAA")
            orden_actual = escuchar_orden(self)

            if not orden_actual:
                break

        self.ocupado = False
        if self.manos_libres:
            self.cambiar_estado("🟢 MANOS LIBRES ACTIVO // DI 'JARVIS'", "#00E5FF")
        else:
            self.cambiar_estado("MODO MANUAL // PRESIONA EL BOTÓN", "#70829B")

    def _hilo_manos_libres(self):
        fs = 16000
        duracion_fragmento = 2.0

        while True:
            if not self.manos_libres or self.ocupado:
                time.sleep(0.3)
                continue

            try:
                audio_np = sd.rec(int(duracion_fragmento * fs), samplerate=fs, channels=1, dtype='int16')
                sd.wait()

                if not self.manos_libres or self.ocupado:
                    continue

                if np.max(np.abs(audio_np)) < UMBRAL_VOZ:
                    continue

                texto = transcribir_audio(audio_np, fs)
                if not texto:
                    continue

                variantes = ["jarvis", "yarvis", "yarbiss", "yarbis", "chavis", "arvis", "servis"]
                desperto = any(v in texto for v in variantes)

                if desperto:
                    self.traer_al_frente()
                    self.cambiar_estado("⚡ ¡JARVIS ACTIVADO!", "#00FFAA")

                    texto_limpio = texto
                    for v in variantes:
                        texto_limpio = texto_limpio.replace(v, "")
                    texto_limpio = texto_limpio.strip()

                    palabras_vacias = ["", "hola", "oye", "hey", "ok", "buenas", "hola cómo estás"]
                    if texto_limpio in palabras_vacias or len(texto_limpio.split()) <= 1:
                        primera_orden = None
                    else:
                        self.agregar_log("TÚ", texto)
                        primera_orden = texto

                    self._iniciar_sesion_conversacion(primera_orden)

            except Exception:
                time.sleep(0.5)

# ========================================================
# --- ARRANQUE DE LA APLICACIÓN ---
# ========================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = JarvisGUI(root)
    root.mainloop()
