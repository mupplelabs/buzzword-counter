import json
import sys
import os

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"

# Force Windows Terminal to support UTF-8 BEFORE any libraries cache sys.stderr (like Flask/logging)
if sys.stdout and hasattr(sys.stdout, 'reconfigure') and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr and hasattr(sys.stderr, 'reconfigure') and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

import re
import argparse
import queue
import threading
import base64
import ssl
import string
import speech_recognition as sr
from flask import Flask, Response, render_template_string, request, redirect, url_for
import io

# Fix for MacOS SSL certificate issues when downloading the Whisper model
ssl._create_default_https_context = ssl._create_unverified_context

app = Flask(__name__)

import logging

class WinError6Filter(logging.Filter):
    def filter(self, record):
        is_winerror6 = False
        if record.exc_info and "WinError 6" in str(record.exc_info[1]):
            is_winerror6 = True
        elif "WinError 6" in record.getMessage():
            is_winerror6 = True
            
        if is_winerror6:
            # Hijack the log record to print a clean message instead of a messy traceback
            record.msg = "🔄 AI threads successfully shut down and disconnected."
            record.args = ()
            record.exc_info = None
            record.levelname = "INFO"
            record.levelno = logging.INFO
        return True

# Suppress messy internal tracebacks from RealtimeSTT when forcefully shutting down pipes on Windows
logging.getLogger().addFilter(WinError6Filter())



# GLOBAL SSL BYPASS FOR MULTIPROCESSING
# Windows spawns child processes that bypass the __main__ block.
# We must evaluate insecure mode globally so child processes (like RealtimeSTT workers) inherit it.
global_insecure = "--insecure" in sys.argv
global_verbose = "--verbose" in sys.argv
global_log_transcript = "--log-transcript" in sys.argv
if os.path.exists("config.json"):
    try:
        with open("config.json", "r") as f:
            cfg = json.load(f)
            if cfg.get("insecure", False):
                global_insecure = True
            if cfg.get("verbose", False):
                global_verbose = True
            if cfg.get("log_transcript", False):
                global_log_transcript = True
    except:
        pass

# Setup transcript logging
TRANSCRIPTS_DIR = "transcripts"
if global_log_transcript and not os.path.exists(TRANSCRIPTS_DIR):
    os.makedirs(TRANSCRIPTS_DIR)

def log_to_transcript(text_chunk):
    if not global_log_transcript:
        return
    import datetime
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    filepath = os.path.join(TRANSCRIPTS_DIR, f"transcript_{today_str}.md")
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {text_chunk}\n")

# Setup stopwords
STOPWORDS_FILE = "stopwords.txt"
DEFAULT_STOPWORDS = {
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it", "for", "not", "on", "with", "he", "as", "you", "do", "at", 
    "this", "but", "his", "by", "from", "they", "we", "say", "her", "she", "or", "an", "will", "my", "one", "all", "would", "there", "their", "what", 
    "so", "up", "out", "if", "about", "who", "get", "which", "go", "me", "when", "make", "can", "like", "time", "no", "just", "him", "know", "take", 
    "people", "into", "year", "your", "good", "some", "could", "them", "see", "other", "than", "then", "now", "look", "only", "come", "its", "over", "think", "also",
    "der", "die", "das", "und", "in", "den", "von", "zu", "dem", "mit", "für", "ist", "auf", "des", "eine", "ein", "sich", "nicht", "auch", "es",
    "als", "an", "nach", "wie", "im", "bei", "werden", "aus", "sie", "oder", "um", "hat", "wir", "noch", "zur", "über", "daß", "so", "dann", "nur",
    "war", "was", "wird", "sein", "dass", "kann", "haben", "mehr", "sind", "ich", "einen", "zum", "vor", "bis", "einem", "aber", "man", "durch", "wenn", "wieder",
    "die", "der", "das", "ein", "eine", "ja", "nein", "oh", "hm", "äh", "okay", "ok"
}
if not os.path.exists(STOPWORDS_FILE):
    with open(STOPWORDS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(list(DEFAULT_STOPWORDS))))

def get_stopwords():
    if os.path.exists(STOPWORDS_FILE):
        with open(STOPWORDS_FILE, "r", encoding="utf-8") as f:
            return set(line.strip().lower() for line in f if line.strip())
    return DEFAULT_STOPWORDS

if global_insecure:
    import urllib3
    import requests
    import httpx
    import warnings
    
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    
    # Patch requests
    old_req = requests.Session.request
    def new_req(*args, **kwargs):
        kwargs['verify'] = False
        return old_req(*args, **kwargs)
    requests.Session.request = new_req
    
    # Patch httpx
    old_httpx = httpx.Client.__init__
    def new_httpx(self, *args, **kwargs):
        kwargs['verify'] = False
        old_httpx(self, *args, **kwargs)
    httpx.Client.__init__ = new_httpx

BUZZWORDS_FILE = 'buzzwords.json'

if os.path.exists(BUZZWORDS_FILE):
    try:
        with open(BUZZWORDS_FILE, 'r') as f:
            buzzwords_dict = json.load(f)
    except Exception as e:
        print(f"⚠️ Warning: {BUZZWORDS_FILE} is corrupted or invalid. Falling back to defaults.")
        buzzwords_dict = None

if not os.path.exists(BUZZWORDS_FILE) or buzzwords_dict is None:
    buzzwords_dict = {
        "Artificial Intelligence": 0,
        "Machine Learning": 0,
        "Deep Learning": 0,
        "AI": 0,
        "KI": 0,
        "Künstliche Intelligenz": 0
    }

def save_buzzwords():
    with open(BUZZWORDS_FILE, 'w') as f:
        json.dump(buzzwords_dict, f, indent=4)

# Hidden aliases for when Whisper mishears short words (especially isolated German)
PHONETIC_ALIASES = {
    "ka'i": "KI",
    "k.e": "KI",
    "kai": "KI",
    "cut you": "KI",
    "die planning": "Deep Learning",
    "Maschinenlearning": "Machine Learning",
    "Maschin-Learning": "Machine Learning",
    "Maschi": "Machine",
    "Maschin": "Machine"    
}

# Create robust HTML IDs for each buzzword
def make_safe_id(word):
    return "id_" + base64.b64encode(word.encode()).decode('utf-8').replace('+', '_').replace('/', '-').replace('=', '')

# Pub/Sub setup for multiple clients
clients_lock = threading.Lock()
clients = set()

def notify_clients(data):
    with clients_lock:
        for client_queue in clients.copy():
            try:
                client_queue.put_nowait(data)
            except queue.Full:
                pass

# Device management globals
current_device_index = None
device_changed = False
global_recorder = None
current_language = "auto"
WHISPER_LANGUAGES = {
    "af": "Afrikaans", "am": "Amharic", "ar": "Arabic", "as": "Assamese", "az": "Azerbaijani", 
    "ba": "Bashkir", "be": "Belarusian", "bg": "Bulgarian", "bn": "Bengali", "bo": "Tibetan", 
    "br": "Breton", "bs": "Bosnian", "ca": "Catalan", "cs": "Czech", "cy": "Welsh", 
    "da": "Danish", "el": "Greek", "es": "Spanish", "et": "Estonian", "eu": "Basque", 
    "fa": "Persian", "fi": "Finnish", "fo": "Faroese", "fr": "French", "gl": "Galician", 
    "gu": "Gujarati", "ha": "Hausa", "haw": "Hawaiian", "he": "Hebrew", "hi": "Hindi", 
    "hr": "Croatian", "ht": "Haitian", "hu": "Hungarian", "hy": "Armenian", "id": "Indonesian", 
    "is": "Icelandic", "it": "Italian", "ja": "Japanese", "jw": "Javanese", "ka": "Georgian", 
    "kk": "Kazakh", "km": "Khmer", "kn": "Kannada", "ko": "Korean", "la": "Latin", 
    "lb": "Luxembourgish", "ln": "Lingala", "lo": "Lao", "lt": "Lithuanian", "lv": "Latvian", 
    "mg": "Malagasy", "mi": "Maori", "mk": "Macedonian", "ml": "Malayalam", "mn": "Mongolian", 
    "mr": "Marathi", "ms": "Malay", "mt": "Maltese", "my": "Myanmar", "ne": "Nepali", 
    "nl": "Dutch", "nn": "Nynorsk", "no": "Norwegian", "oc": "Occitan", "pa": "Punjabi", 
    "pl": "Polish", "ps": "Pashto", "pt": "Portuguese", "ro": "Romanian", "ru": "Russian", 
    "sa": "Sanskrit", "sd": "Sindhi", "si": "Sinhala", "sk": "Slovak", "sl": "Slovenian", 
    "sn": "Shona", "so": "Somali", "sq": "Albanian", "sr": "Serbian", "su": "Sundanese", 
    "sv": "Swedish", "sw": "Swahili", "ta": "Tamil", "te": "Telugu", "tg": "Tajik", 
    "th": "Thai", "tk": "Turkmen", "tl": "Tagalog", "tr": "Turkish", "tt": "Tatar", 
    "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek", "vi": "Vietnamese", "yi": "Yiddish", 
    "yo": "Yoruba", "zh": "Chinese"
}

def classic_audio_listener():
    global current_device_index, device_changed
    import wave
    import numpy as np
    try:
        from faster_whisper import WhisperModel
        FASTER_WHISPER_AVAILABLE = True
    except ImportError:
        FASTER_WHISPER_AVAILABLE = False

    """Listens to microphone in the background and queues updates."""
    recognizer = sr.Recognizer()
    
    if FASTER_WHISPER_AVAILABLE:
        print("⚡ Using Faster-Whisper engine for ultra-fast transcription!")
        faster_model = WhisperModel("small", device="auto", compute_type="default")
    else:
        print("🐢 Using standard OpenAI Whisper engine. (Run 'pip install faster-whisper' to upgrade!)")
        
    while True:
        device_changed = False
        try:
            with sr.Microphone(device_index=current_device_index) as source:
                dev_name = "System Default" if current_device_index is None else f"Device {current_device_index}"
                print(f"🎙️  Calibrating {dev_name} for ambient noise... Please stay quiet.")
                recognizer.adjust_for_ambient_noise(source, duration=3)
                recognizer.energy_threshold += 150
                recognizer.dynamic_energy_threshold = False
                print(f"✅ Calibration complete! Listening on {dev_name}...")
                
                while not device_changed:
                    try:
                        # timeout=1 makes it wake up every second to check if the user selected a new device
                        audio = recognizer.listen(source, timeout=1)
                        
                        if FASTER_WHISPER_AVAILABLE:
                            wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
                            with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
                                frames = wf.readframes(wf.getnframes())
                                audio_array = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                            
                            segments, info = faster_model.transcribe(audio_array, language=current_language if current_language != 'auto' else None, condition_on_previous_text=False)
                            text = " ".join([segment.text for segment in segments]).lower()
                        else:
                            text = recognizer.recognize_whisper(
                                audio, 
                                model="small", 
                                language=current_language if current_language != 'auto' else None,
                                condition_on_previous_text=False
                            ).lower()
                            
                        if global_verbose:
                            print(f"Recognized: {text}")  
                        log_to_transcript(text)
                        
                        # Ignore extreme repetition loops
                        if "very very very" in text:
                            continue
                        
                        clean_text = text.translate(str.maketrans('', '', string.punctuation))
                        
                        # Normalize known mishearings
                        for alias, real_word in PHONETIC_ALIASES.items():
                            clean_alias = alias.lower().translate(str.maketrans('', '', string.punctuation))
                            if clean_alias and clean_alias in clean_text:
                                pattern = r'\b' + re.escape(clean_alias) + r'\b'
                                real_clean = real_word.lower().translate(str.maketrans('', '', string.punctuation))
                                clean_text = re.sub(pattern, real_clean, clean_text)

                        updated = False
                        for word in list(buzzwords_dict.keys()):
                            clean_word = word.lower().translate(str.maketrans('', '', string.punctuation))
                            if clean_word:
                                pattern = r'\b' + re.escape(clean_word) + r'\b'
                                match_count = len(re.findall(pattern, clean_text))
                                if match_count > 0:
                                    buzzwords_dict[word] += match_count
                                    updated = True
                        
                        if updated:
                            save_buzzwords()
                            notify_clients(buzzwords_dict.copy())
                            
                    except sr.WaitTimeoutError:
                        # Safe timeout if no speech detected, loops back to check device_changed
                        continue
                    except (sr.UnknownValueError, sr.RequestError):
                        continue
        except OSError as e:
            print(f"⚠️ Microphone hardware error: {e}")
            print("Retrying in 2 seconds...")
            import time
            time.sleep(2)
        except Exception as e:
            print(f"❌ FATAL ERROR in Classic Engine: {e}")
            print("Shutting down server...")
            os._exit(1)


def realtimestt_audio_listener():
    global current_device_index, device_changed, global_recorder
    import torch
    if hasattr(torch.hub, "_check_repo_is_trusted"):
        torch.hub._check_repo_is_trusted = lambda *a, **k: True
        
    from RealtimeSTT import AudioToTextRecorder
    
    while True:
        device_changed = False
        dev_name = "System Default" if current_device_index is None else f"Device {current_device_index}"
        print(f"🎙️ Starting RealtimeSTT on {dev_name}...")
        
        current_utterance_matches = {}
        
        def process_text_chunk(text):
            clean_text = text.lower().translate(str.maketrans('', '', string.punctuation))
            
            if "very very very" in clean_text:
                return
                
            for alias, real_word in PHONETIC_ALIASES.items():
                clean_alias = alias.lower().translate(str.maketrans('', '', string.punctuation))
                if clean_alias and clean_alias in clean_text:
                    pattern = r'\b' + re.escape(clean_alias) + r'\b'
                    real_clean = real_word.lower().translate(str.maketrans('', '', string.punctuation))
                    clean_text = re.sub(pattern, real_clean, clean_text)
                    
            updated = False
            for word in list(buzzwords_dict.keys()):
                clean_word = word.lower().translate(str.maketrans('', '', string.punctuation))
                if clean_word:
                    pattern = r'\b' + re.escape(clean_word) + r'\b'
                    match_count = len(re.findall(pattern, clean_text))
                    
                    previous_count = current_utterance_matches.get(word, 0)
                    if match_count > previous_count:
                        diff = match_count - previous_count
                        buzzwords_dict[word] += diff
                        current_utterance_matches[word] = match_count
                        updated = True
            
            if updated:
                save_buzzwords()
                notify_clients(buzzwords_dict.copy())
                
        try:
            with AudioToTextRecorder(
                model="small",
                language=current_language if current_language != "auto" else "",
                device="cuda",
                input_device_index=current_device_index,
                enable_realtime_transcription=True,
                on_realtime_transcription_update=process_text_chunk,
                realtime_model_type="tiny.en" if current_language == "en" else "tiny",
                silero_use_onnx=False,
                spinner=False
            ) as recorder:
                global_recorder = recorder
                print(f"✅ Ready! Listening on {dev_name} (RealtimeSTT)...")
                
                while not device_changed:
                    text = recorder.text()
                    if text:
                        if global_verbose:
                            print(f"Recognized: {text}")
                        log_to_transcript(text)
                        process_text_chunk(text)
                    current_utterance_matches.clear()
                    
        except Exception as e:
            error_msg = str(e).lower()
            if "ssl" in error_msg or "connecterror" in error_msg or "cuda out of memory" in error_msg:
                print(f"❌ FATAL ERROR in RealtimeSTT Engine: {e}")
                print("Shutting down server...")
                os._exit(1)
            else:
                print(f"⚠️ Microphone glitched: {e}")
                print("Retrying in 2 seconds...")
                import time
                time.sleep(2)
        finally:
            global_recorder = None

# 2. Frontend HTML & CSS with Rolling Analog Wheel Effect
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Retro Buzzword Counter</title>
    <style>
        body {
            background-color: #1a1a1a;
            color: #e0e0e0;
            font-family: 'Courier New', Courier, monospace;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding-top: 50px;
        }
        h1 {
            color: #f39c12;
            text-shadow: 0 0 10px rgba(243, 156, 18, 0.5);
            letter-spacing: 2px;
        }
        .controls {
            margin-bottom: 20px;
            display: flex;
            gap: 15px;
            align-items: center;
        }
        button, .btn {
            background-color: #34495e;
            color: #ecf0f1;
            border: 2px solid #2c3e50;
            padding: 10px 20px;
            font-size: 16px;
            font-family: inherit;
            cursor: pointer;
            border-radius: 4px;
            text-decoration: none;
        }
        button:hover, .btn:hover {
            background-color: #2c3e50;
        }
        input[type="text"] {
            background-color: #2c3e50;
            color: white;
            border: 2px solid #34495e;
            border-radius: 4px;
            padding: 10px;
            font-size: 16px;
            font-family: inherit;
        }
        .container {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 25px;
            max-width: 800px;
            margin-top: 10px;
        }
        .card {
            background: #2c3e50;
            padding: 20px;
            border-radius: 8px;
            border: 4px solid #34495e;
            box-shadow: 0 8px 16px rgba(0,0,0,0.5);
            display: flex;
            justify-content: space-between;
            align-items: center;
            width: 340px;
        }
        .label-container {
            display: flex;
            flex-direction: column;
            gap: 5px;
            max-width: 60%;
        }
        .label {
            font-size: 16px;
            font-weight: bold;
            text-transform: uppercase;
            color: #ecf0f1;
            word-wrap: break-word;
        }
        .remove-btn {
            padding: 2px 8px;
            font-size: 12px;
            background: #c0392b;
            color: white;
            border: none;
            border-radius: 3px;
            cursor: pointer;
            align-self: flex-start;
        }
        .remove-btn:hover {
            background: #e74c3c;
        }
        /* --- Analog Wheel Counter Styling --- */
        .odometer {
            display: inline-flex;
            background: #111;
            padding: 4px 6px;
            border-radius: 4px;
            border: 3px solid #000;
            box-shadow: inset 0 0 10px #000;
        }
        .digit-container {
            height: 40px;
            width: 24px;
            overflow: hidden;
            position: relative;
            background: linear-gradient(#222, #111 50%, #222);
            margin: 0 1px;
            border-radius: 3px;
            border-bottom: 1px solid #444;
        }
        .digit-strip {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            transition: transform 0.6s cubic-bezier(0.25, 1, 0.5, 1);
            display: flex;
            flex-direction: column;
        }
        .digit {
            height: 40px;
            line-height: 40px;
            text-align: center;
            font-size: 28px;
            font-weight: bold;
            color: #fff;
            text-shadow: 0 1px 2px rgba(0,0,0,0.8);
        }
        
        /* --- Hamburger Menu --- */
        .menu-container { position: relative; display: inline-block; }
        .hamburger-btn {
            background-color: #2c3e50; color: white; border: 2px solid #34495e; border-radius: 4px;
            padding: 10px 15px; font-size: 16px; cursor: pointer; transition: all 0.2s;
        }
        .hamburger-btn:hover { background-color: #34495e; }
        .menu-content {
            display: none; position: absolute; background-color: #2c3e50; min-width: 320px;
            box-shadow: 0px 8px 16px 0px rgba(0,0,0,0.6); z-index: 100; border-radius: 8px;
            padding: 15px; flex-direction: column; gap: 15px; top: 50px; left: 0; border: 2px solid #34495e;
        }
        .menu-content.show { display: flex; }
    </style>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/wordcloud2.js/1.2.2/wordcloud2.min.js"></script>
</head>
<body>

    <h1>🎙️ LIVE BUZZWORD ODOMETER</h1>
    <p style="color: #7f8c8d;">Listening via microphone... Say one of your keywords!</p>
    
    <div class="controls">
        <div class="menu-container">
            <button class="hamburger-btn" title="Settings" style="background-color: #34495e; padding: 10px 15px; font-size: 20px;" onclick="document.getElementById('dropdownMenu').classList.toggle('show')">⚙️</button>
            <div id="dropdownMenu" class="menu-content">
                <form method="POST" action="/set_device" style="margin:0; width:100%;">
                    <label style="font-size:12px; color:#bdc3c7; margin-bottom:5px; display:block; text-transform:uppercase; font-weight:bold;">Audio Input Device</label>
                    <select name="device_index" onchange="this.form.submit()" style="width:100%; box-sizing: border-box; background-color: #1a252f; color: white; border: 1px solid #34495e; border-radius: 4px; padding: 10px; font-size: 14px; cursor:pointer;">
                        <option value="default" {% if current_device is none %}selected{% endif %}>🎙️ Default System Mic</option>
                        {% for idx, name in devices %}
                        <option value="{{ idx }}" {% if current_device == idx %}selected{% endif %}>🎙️ {{ name[:40] }}{% if name|length > 40 %}...{% endif %}</option>
                        {% endfor %}
                    </select>
                </form>
                
                <form method="POST" action="/set_language" style="margin:15px 0 15px 0; width:100%;">
                    <label style="font-size:12px; color:#bdc3c7; margin-bottom:5px; display:block; text-transform:uppercase; font-weight:bold;">Recognition Language</label>
                    <select name="language" onchange="this.form.submit()" style="width:100%; box-sizing: border-box; background-color: #1a252f; color: white; border: 1px solid #34495e; border-radius: 4px; padding: 10px; font-size: 14px; cursor:pointer;">
                        <option value="auto" {% if current_language == 'auto' %}selected{% endif %}>🌍 Auto-Detect (Mixed)</option>
                        <option value="en" {% if current_language == 'en' %}selected{% endif %}>🇬🇧 English</option>
                        <option value="de" {% if current_language == 'de' %}selected{% endif %}>🇩🇪 German</option>
                        <option disabled>──────────</option>
                        {% for code, name in languages.items() %}
                            <option value="{{ code }}" {% if current_language == code %}selected{% endif %}>{{ name }}</option>
                        {% endfor %}
                    </select>
                </form>
                <button id="enable-sound" style="width:100%; box-sizing: border-box; margin:0; background-color: #2980b9; color: white; border: none; border-radius: 4px; padding: 10px; font-size: 14px; cursor: pointer; transition: background-color 0.2s;">🔇 Enable Sound Effects</button>
                <div style="margin-top: 5px;">
                    <label style="font-size:12px; color:#bdc3c7; margin-bottom:5px; display:block; text-transform:uppercase; font-weight:bold;">Presentation Title</label>
                    <input type="text" id="pres-title" placeholder="🎙️ LIVE BUZZWORD ODOMETER" style="width:100%; box-sizing: border-box; background-color: #1a252f; color: white; border: 1px solid #34495e; border-radius: 4px; padding: 10px; font-size: 14px;">
                </div>
            </div>
        </div>
        
        <button onclick="openPresentation()" class="btn" title="Open Presentation Mode" style="background-color: #34495e; border-color: #2c3e50; padding: 10px 15px; font-size: 20px;">📺</button>
        <button onclick="openTotals()" class="btn" title="Open Totals Mode" style="background-color: #34495e; border-color: #2c3e50; padding: 10px 15px; font-size: 20px;">∑</button>
        <form method="POST" action="/reset" style="margin:0;">
            <button type="submit" class="btn" title="Reset All Counters to Zero" style="background-color: #34495e; border-color: #2c3e50; padding: 10px 15px; font-size: 20px;">🔄</button>
        </form>
        <button type="button" onclick="document.getElementById('confirmModal').style.display='flex'" class="btn" title="Remove All Buzzwords" style="background-color: #34495e; border-color: #2c3e50; padding: 10px 15px; font-size: 20px;">🗑️</button>
        <button type="button" onclick="openWordCloud()" class="btn" title="Generate Word Cloud" style="background-color: #34495e; border-color: #2c3e50; padding: 10px 15px; font-size: 20px;">☁️</button>
        <form method="POST" action="/add_word" style="display:flex; gap:10px;">
            <input type="text" name="word" placeholder="Add a new buzzword..." required>
            <button type="submit" class="btn">➕ Add</button>
        </form>
    </div>

    <div class="container">
        {% for b in buzzwords_data %}
        <div class="card">
            <div class="label-container">
                <span class="label">{{ b.original }}</span>
                <form method="POST" action="/remove_word" style="margin:0;">
                    <input type="hidden" name="word" value="{{ b.original }}">
                    <button type="submit" class="remove-btn">✖ Remove</button>
                </form>
                <a href="/embed/{{ b.original }}" target="_blank" style="font-size:12px; color:#3498db; text-decoration:none; margin-top:5px;">🔗 Get Embed Link</a>
            </div>
            <div class="odometer" id="odo-{{ b.safe_id }}">
                <div class="digit-container"><div class="digit-strip" id="{{ b.safe_id }}-d3"></div></div>
                <div class="digit-container"><div class="digit-strip" id="{{ b.safe_id }}-d2"></div></div>
                <div class="digit-container"><div class="digit-strip" id="{{ b.safe_id }}-d1"></div></div>
                <div class="digit-container"><div class="digit-strip" id="{{ b.safe_id }}-d0"></div></div>
            </div>
        </div>
        {% endfor %}
    </div>

    <script>
        let soundEnabled = false;
        let audioCtx = null;
        const previousValues = {};

        document.getElementById('enable-sound').addEventListener('click', function(e) {
            if (!audioCtx) {
                audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            }
            if (audioCtx.state === 'suspended') {
                audioCtx.resume();
            }
            soundEnabled = !soundEnabled;
            if (soundEnabled) {
                e.target.innerText = "🔊 Disable Sound Effects";
                e.target.style.backgroundColor = "#e74c3c";
            } else {
                e.target.innerText = "🔇 Enable Sound Effects";
                e.target.style.backgroundColor = "#2980b9";
            }
        });

        function playClick() {
            if (!soundEnabled || !audioCtx) return;
            const oscillator = audioCtx.createOscillator();
            const gainNode = audioCtx.createGain();
            oscillator.connect(gainNode);
            gainNode.connect(audioCtx.destination);
            
            // Synthetic mechanical click sound
            oscillator.type = 'square';
            oscillator.frequency.setValueAtTime(150, audioCtx.currentTime);
            oscillator.frequency.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + 0.05);
            
            gainNode.gain.setValueAtTime(0.5, audioCtx.currentTime);
            gainNode.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + 0.05);
            
            oscillator.start();
            oscillator.stop(audioCtx.currentTime + 0.05);
        }

        const buzzwordsData = {{ buzzwords_data|tojson }};
        buzzwordsData.forEach(b => {
            previousValues[b.original] = 0;
            ['d0', 'd1', 'd2', 'd3'].forEach(digitId => {
                const strip = document.getElementById(`${b.safe_id}-${digitId}`);
                if (strip) {
                    for (let i = 0; i <= 9; i++) {
                        const div = document.createElement('div');
                        div.className = 'digit';
                        div.innerText = i;
                        strip.appendChild(div);
                    }
                }
            });
        });

        function updateOdometer(word, value) {
            if (previousValues[word] === undefined) return; // Ignore words this tab doesn't know about yet
            
            if (value > previousValues[word]) {
                // Play click if value increased
                playClick();
            }
            previousValues[word] = value;

            const strVal = String(value).padStart(4, '0');
            const d3 = parseInt(strVal[0]); 
            const d2 = parseInt(strVal[1]); 
            const d1 = parseInt(strVal[2]); 
            const d0 = parseInt(strVal[3]); 

            const b = buzzwordsData.find(item => item.original === word);
            if (!b) return;
            const safeWord = b.safe_id;

            const elD3 = document.getElementById(`${safeWord}-d3`);
            const elD2 = document.getElementById(`${safeWord}-d2`);
            const elD1 = document.getElementById(`${safeWord}-d1`);
            const elD0 = document.getElementById(`${safeWord}-d0`);
            
            if (elD3) elD3.style.transform = `translateY(-${d3 * 40}px)`;
            if (elD2) elD2.style.transform = `translateY(-${d2 * 40}px)`;
            if (elD1) elD1.style.transform = `translateY(-${d1 * 40}px)`;
            if (elD0) elD0.style.transform = `translateY(-${d0 * 40}px)`;
        }

        buzzwordsData.forEach(b => updateOdometer(b.original, 0));

        const eventSource = new EventSource("/stream");
        eventSource.onmessage = function(event) {
            const data = JSON.parse(event.data);
            for (const [word, count] of Object.entries(data)) {
                if (count !== previousValues[word]) {
                    updateOdometer(word, count);
                }
            }
        };

        // Close dropdown when clicking outside
        window.onclick = function(event) {
            if (!event.target.closest('.menu-container')) {
                var dropdowns = document.getElementsByClassName("menu-content");
                for (var i = 0; i < dropdowns.length; i++) {
                    var openDropdown = dropdowns[i];
                    if (openDropdown.classList.contains('show')) {
                        openDropdown.classList.remove('show');
                    }
                }
            }
        }

        function openPresentation() {
            let title = document.getElementById('pres-title').value.trim();
            if (!title) {
                title = "🎙️ LIVE BUZZWORD ODOMETER";
            }
            window.open("/present?title=" + encodeURIComponent(title), "_blank");
        }

        function openTotals() {
            let title = document.getElementById('pres-title').value.trim();
            if (!title) {
                title = "🎙️ LIVE BUZZWORD ODOMETER";
            }
            window.open("/totals?title=" + encodeURIComponent(title), "_blank");
        }
    </script>

    <!-- Word Cloud Modal -->
    <div id="cloudModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.9); z-index:9999; justify-content:center; align-items:center; flex-direction:column;">
        <h2 style="color:#e67e22; margin-bottom: 20px;">☁️ Today's Transcript Word Cloud</h2>
        <div style="background: white; border-radius: 8px; padding: 10px;">
            <canvas id="cloudCanvas" width="800" height="500"></canvas>
        </div>
        <button type="button" onclick="document.getElementById('cloudModal').style.display='none'" class="btn" style="margin-top:20px; background:#e74c3c; border-color:#c0392b;">Close</button>
        <div id="cloudLoading" style="color:white; margin-top:10px; display:none;">Generating cloud...</div>
    </div>

    <script>
        function openWordCloud() {
            document.getElementById('cloudModal').style.display='flex';
            document.getElementById('cloudLoading').style.display='block';
            fetch('/api/wordcloud')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('cloudLoading').style.display='none';
                    if (data.length === 0) {
                        alert("No transcript data available for today. Please speak to the microphone first with --log-transcript enabled.");
                        document.getElementById('cloudModal').style.display='none';
                        return;
                    }
                    WordCloud(document.getElementById('cloudCanvas'), { 
                        list: data,
                        fontFamily: 'Courier New, monospace',
                        weightFactor: function (size) {
                            return Math.pow(size, 0.5) * 15; // Scale down massively so huge words fit
                        },
                        color: 'random-dark',
                        backgroundColor: '#ffffff',
                        rotateRatio: 0.5
                    });
                })
                .catch(e => {
                    document.getElementById('cloudLoading').style.display='none';
                    alert("Error generating word cloud: " + e);
                });
        }
    </script>

        <div id="confirmModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); z-index:9999; justify-content:center; align-items:center;">
        <div style="background:#2c3e50; padding:30px; border-radius:12px; border:4px solid #34495e; text-align:center; max-width:400px; box-shadow:0 10px 30px rgba(0,0,0,0.7);">
            <h2 style="margin-top:0; color:#f39c12;">Are you sure?</h2>
            <p style="font-size:18px; margin-bottom:25px;">You are about to completely delete all buzzwords from the list. This cannot be undone.</p>
            <div style="display:flex; justify-content:center; gap:20px;">
                <button type="button" onclick="document.getElementById('confirmModal').style.display='none'" class="btn" style="background:#7f8c8d; border-color:#95a5a6;">Cancel</button>
                <form method="POST" action="/remove_all" style="margin:0;">
                    <button type="submit" class="btn" style="background:#c0392b; border-color:#e74c3c;">🗑️ Delete All</button>
                </form>
            </div>
        </div>
    </div>
</body>
</html>
"""

EMBED_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <style>
        body { 
            margin: 0; padding: 0; background: transparent; overflow: hidden; 
            display: flex; justify-content: center; align-items: center; height: 100vh; 
        }
        .odometer {
            display: inline-flex; background: #111; padding: 4px 6px; border-radius: 4px; border: 3px solid #000; box-shadow: inset 0 0 10px #000;
        }
        .digit-container {
            height: 40px; width: 24px; overflow: hidden; position: relative; background: linear-gradient(#222, #111 50%, #222); margin: 0 1px; border-radius: 3px; border-bottom: 1px solid #444;
        }
        .digit-strip {
            position: absolute; top: 0; left: 0; width: 100%; transition: transform 0.6s cubic-bezier(0.25, 1, 0.5, 1); display: flex; flex-direction: column;
        }
        .digit {
            height: 40px; line-height: 40px; text-align: center; font-size: 28px; font-weight: bold; color: #fff; text-shadow: 0 1px 2px rgba(0,0,0,0.8); font-family: 'Courier New', Courier, monospace;
        }
    </style>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/wordcloud2.js/1.2.2/wordcloud2.min.js"></script>
</head>
<body>
    <div class="odometer" id="odo-{{ safe_id }}">
        <div class="digit-container"><div class="digit-strip" id="{{ safe_id }}-d3"></div></div>
        <div class="digit-container"><div class="digit-strip" id="{{ safe_id }}-d2"></div></div>
        <div class="digit-container"><div class="digit-strip" id="{{ safe_id }}-d1"></div></div>
        <div class="digit-container"><div class="digit-strip" id="{{ safe_id }}-d0"></div></div>
    </div>
    <script>
        const safeWord = "{{ safe_id }}";
        const targetWord = "{{ word }}";
        let previousValue = 0;

        ['d0', 'd1', 'd2', 'd3'].forEach(digitId => {
            const strip = document.getElementById(`${safeWord}-${digitId}`);
            if (strip) {
                for (let i = 0; i <= 9; i++) {
                    const div = document.createElement('div');
                    div.className = 'digit';
                    div.innerText = i;
                    strip.appendChild(div);
                }
            }
        });

        function updateOdometer(value) {
            const strVal = String(value).padStart(4, '0');
            const d3 = parseInt(strVal[0]); 
            const d2 = parseInt(strVal[1]); 
            const d1 = parseInt(strVal[2]); 
            const d0 = parseInt(strVal[3]); 
            
            document.getElementById(`${safeWord}-d3`).style.transform = `translateY(-${d3 * 40}px)`;
            document.getElementById(`${safeWord}-d2`).style.transform = `translateY(-${d2 * 40}px)`;
            document.getElementById(`${safeWord}-d1`).style.transform = `translateY(-${d1 * 40}px)`;
            document.getElementById(`${safeWord}-d0`).style.transform = `translateY(-${d0 * 40}px)`;
        }

        updateOdometer(0);

        const eventSource = new EventSource("/stream");
        eventSource.onmessage = function(event) {
            const data = JSON.parse(event.data);
            let count = 0;
            if (targetWord === "__TOTAL__") {
                for (const v of Object.values(data)) count += v;
            } else if (data[targetWord] !== undefined) {
                count = data[targetWord];
            } else {
                return;
            }
            
            if (count !== previousValue) {
                updateOdometer(count);
                previousValue = count;
            }
        };
    </script>
</body>
</html>
"""

TOTALS_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Total Buzzwords</title>
    <style>
        body { 
            margin: 0; padding: 0; background: #1a1a1a; 
            display: flex; flex-direction: column; justify-content: center; align-items: center; min-height: 100vh; 
            font-family: 'Courier New', Courier, monospace; overflow-x: hidden; color: #e0e0e0; box-sizing: border-box; padding: 20px;
        }
        h1 {
            color: #f39c12; text-shadow: 0 0 10px rgba(243, 156, 18, 0.5);
            letter-spacing: 2px; margin-top: 0; margin-bottom: 25px; font-size: 2.5em; text-align: center;
        }
        .card {
            background: #2c3e50; padding: 40px; border-radius: 15px; border: 5px solid #34495e;
            box-shadow: 0 15px 30px rgba(0,0,0,0.7); display: flex; flex-direction: column;
            align-items: center; justify-content: center; gap: 20px; width: 100%; max-width: 450px;
        }
        .label {
            font-size: 32px; font-weight: bold; text-transform: uppercase; color: #ecf0f1; text-align: center;
        }
        .odometer {
            display: inline-flex; background: #111; padding: 15px 20px; border-radius: 10px; border: 5px solid #000; box-shadow: inset 0 0 25px #000;
        }
        .digit-container {
            height: 70px; width: 48px; overflow: hidden; position: relative; background: linear-gradient(#222, #111 50%, #222); margin: 0 4px; border-radius: 6px; border-bottom: 3px solid #444;
        }
        .digit-strip {
            position: absolute; top: 0; left: 0; width: 100%; transition: transform 0.6s cubic-bezier(0.25, 1, 0.5, 1); display: flex; flex-direction: column;
        }
        .digit {
            height: 70px; line-height: 70px; text-align: center; font-size: 48px; font-weight: bold; color: #fff; text-shadow: 0 3px 6px rgba(0,0,0,0.8);
        }
    </style>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/wordcloud2.js/1.2.2/wordcloud2.min.js"></script>
</head>
<body>
    <h1>{{ title }}</h1>
    <div class="card">
        <span class="label">TOTAL COUNT</span>
        <div class="odometer" id="odo-total">
            <div class="digit-container"><div class="digit-strip" id="total-d3"></div></div>
            <div class="digit-container"><div class="digit-strip" id="total-d2"></div></div>
            <div class="digit-container"><div class="digit-strip" id="total-d1"></div></div>
            <div class="digit-container"><div class="digit-strip" id="total-d0"></div></div>
        </div>
        <a href="/embed_total" target="_blank" style="font-size:14px; color:#3498db; text-decoration:none; margin-top:5px;">🔗 Get Embed Link</a>
    </div>

    <script>
        ['d0', 'd1', 'd2', 'd3'].forEach(digitId => {
            const strip = document.getElementById(`total-${digitId}`);
            if (strip) {
                for (let i = 0; i <= 9; i++) {
                    const div = document.createElement('div');
                    div.className = 'digit';
                    div.innerText = i;
                    strip.appendChild(div);
                }
            }
        });

        let currentTotal = 0;

        function updateOdometer(value) {
            const strVal = String(value).padStart(4, '0');
            const d3 = parseInt(strVal[0]); 
            const d2 = parseInt(strVal[1]); 
            const d1 = parseInt(strVal[2]); 
            const d0 = parseInt(strVal[3]); 

            const elD3 = document.getElementById('total-d3');
            const elD2 = document.getElementById('total-d2');
            const elD1 = document.getElementById('total-d1');
            const elD0 = document.getElementById('total-d0');
            
            if (elD3) elD3.style.transform = `translateY(-${d3 * 70}px)`;
            if (elD2) elD2.style.transform = `translateY(-${d2 * 70}px)`;
            if (elD1) elD1.style.transform = `translateY(-${d1 * 70}px)`;
            if (elD0) elD0.style.transform = `translateY(-${d0 * 70}px)`;
        }

        updateOdometer(0);

        const eventSource = new EventSource("/stream");
        eventSource.onmessage = function(event) {
            const data = JSON.parse(event.data);
            let sum = 0;
            for (const count of Object.values(data)) {
                sum += count;
            }
            if (sum !== currentTotal) {
                currentTotal = sum;
                updateOdometer(sum);
            }
        };
    </script>
</body>
</html>
"""

PRESENTATION_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{{ title }}</title>
    <style>
        body {
            background-color: #1a1a1a;
            color: #e0e0e0;
            font-family: 'Courier New', Courier, monospace;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 20px;
            margin: 0;
            min-height: 100vh;
            box-sizing: border-box;
            overflow-x: hidden;
        }
        h1 {
            color: #f39c12;
            text-shadow: 0 0 10px rgba(243, 156, 18, 0.5);
            letter-spacing: 2px;
            margin-top: 0;
            margin-bottom: 25px;
            font-size: 2em;
            text-align: center;
        }
        .container {
            display: grid;
            justify-content: center;
            align-content: center;
            gap: 25px;
            width: 100%;
            box-sizing: border-box;
        }
        .card {
            background: #2c3e50;
            padding: 20px;
            border-radius: 12px;
            border: 4px solid #34495e;
            box-shadow: 0 12px 24px rgba(0,0,0,0.6);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 15px;
            box-sizing: border-box;
        }
        .label {
            font-size: 24px;
            font-weight: bold;
            text-transform: uppercase;
            color: #ecf0f1;
            text-align: center;
        }
        /* --- Scaled Up Analog Wheel Counter --- */
        .odometer {
            display: inline-flex; background: #111; padding: 8px 10px; border-radius: 6px; border: 4px solid #000; box-shadow: inset 0 0 15px #000;
        }
        .digit-container {
            height: 50px; width: 30px; overflow: hidden; position: relative; background: linear-gradient(#222, #111 50%, #222); margin: 0 2px; border-radius: 4px; border-bottom: 2px solid #444;
        }
        .digit-strip {
            position: absolute; top: 0; left: 0; width: 100%; transition: transform 0.6s cubic-bezier(0.25, 1, 0.5, 1); display: flex; flex-direction: column;
        }
        .digit {
            height: 50px; line-height: 50px; text-align: center; font-size: 34px; font-weight: bold; color: #fff; text-shadow: 0 2px 4px rgba(0,0,0,0.8);
        }
    </style>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/wordcloud2.js/1.2.2/wordcloud2.min.js"></script>
</head>
<body>
    <h1>{{ title }}</h1>

    <div class="container">
        {% for b in buzzwords_data %}
        <div class="card">
            <span class="label">{{ b.original }}</span>
            <div class="odometer" id="odo-{{ b.safe_id }}">
                <div class="digit-container"><div class="digit-strip" id="{{ b.safe_id }}-d3"></div></div>
                <div class="digit-container"><div class="digit-strip" id="{{ b.safe_id }}-d2"></div></div>
                <div class="digit-container"><div class="digit-strip" id="{{ b.safe_id }}-d1"></div></div>
                <div class="digit-container"><div class="digit-strip" id="{{ b.safe_id }}-d0"></div></div>
            </div>
        </div>
        {% endfor %}
    </div>

    <script>
        const previousValues = {};
        const buzzwordsData = {{ buzzwords_data|tojson }};
        
        buzzwordsData.forEach(b => {
            previousValues[b.original] = 0;
            ['d0', 'd1', 'd2', 'd3'].forEach(digitId => {
                const strip = document.getElementById(`${b.safe_id}-${digitId}`);
                if (strip) {
                    for (let i = 0; i <= 9; i++) {
                        const div = document.createElement('div');
                        div.className = 'digit';
                        div.innerText = i;
                        strip.appendChild(div);
                    }
                }
            });
        });

        function updateOdometer(word, value) {
            if (previousValues[word] === undefined) return;
            previousValues[word] = value;

            const strVal = String(value).padStart(4, '0');
            const d3 = parseInt(strVal[0]); 
            const d2 = parseInt(strVal[1]); 
            const d1 = parseInt(strVal[2]); 
            const d0 = parseInt(strVal[3]); 

            const b = buzzwordsData.find(item => item.original === word);
            if (!b) return;
            const safeWord = b.safe_id;

            const elD3 = document.getElementById(`${safeWord}-d3`);
            const elD2 = document.getElementById(`${safeWord}-d2`);
            const elD1 = document.getElementById(`${safeWord}-d1`);
            const elD0 = document.getElementById(`${safeWord}-d0`);
            
            if (elD3) elD3.style.transform = `translateY(-${d3 * 50}px)`;
            if (elD2) elD2.style.transform = `translateY(-${d2 * 50}px)`;
            if (elD1) elD1.style.transform = `translateY(-${d1 * 50}px)`;
            if (elD0) elD0.style.transform = `translateY(-${d0 * 50}px)`;
        }

        function optimizeGrid() {
            const container = document.querySelector('.container');
            const N = document.querySelectorAll('.card').length;
            if (N === 0) return;

            let cols;
            if (N === 1) cols = 1;
            else if (N < 6) cols = 2;
            else if (N <= 12) cols = 3;
            else if (N <= 16) cols = 4;
            else cols = 5;

            // Use a fixed width per item (or responsive if screen is small)
            // 280px perfectly fits 5 cards with 25px gaps in a 1920px width.
            container.style.gridTemplateColumns = `repeat(${cols}, min(280px, 18vw))`;
        }

        window.addEventListener('resize', optimizeGrid);
        optimizeGrid();

        buzzwordsData.forEach(b => updateOdometer(b.original, 0));

        const eventSource = new EventSource("/stream");
        eventSource.onmessage = function(event) {
            const data = JSON.parse(event.data);
            for (const [word, count] of Object.entries(data)) {
                if (count !== previousValues[word]) {
                    updateOdometer(word, count);
                }
            }
        };
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    b_data = [{"original": word, "safe_id": make_safe_id(word)} for word in buzzwords_dict.keys()]
    try:
        raw_devices = sr.Microphone.list_microphone_names()
        devices = []
        seen_names = set()
        for idx, name in enumerate(raw_devices):
            name_lower = name.lower()
            if "output" in name_lower or "speaker" in name_lower:
                continue
            if name not in seen_names:
                seen_names.add(name)
                devices.append((idx, name.strip()))
    except Exception:
        devices = []
    return render_template_string(HTML_TEMPLATE, buzzwords_data=b_data, devices=devices, current_device=current_device_index, languages=WHISPER_LANGUAGES, current_language=current_language)

@app.route('/set_device', methods=['POST'])
def set_device():
    global current_device_index, device_changed, global_recorder
    idx_str = request.form.get('device_index')
    if idx_str == "default" or idx_str is None:
        current_device_index = None
    else:
        current_device_index = int(idx_str)
    
    device_changed = True
    if global_recorder:
        global_recorder.shutdown()
        
    return redirect(url_for('index'))

@app.route('/set_language', methods=['POST'])
def set_language():
    global current_language, device_changed, global_recorder
    lang = request.form.get('language')
    if lang:
        current_language = lang
        
        # Save to config.json
        import os, json
        try:
            config_data = {}
            if os.path.exists("config.json"):
                with open("config.json", "r") as f:
                    config_data = json.load(f)
            config_data["language"] = current_language
            with open("config.json", "w") as f:
                json.dump(config_data, f, indent=4)
        except Exception as e:
            print(f"⚠️ Failed to save language to config: {e}")
            
    device_changed = True
    if global_recorder:
        global_recorder.shutdown()
    return redirect(url_for('index'))

@app.route('/embed/<word>')
def embed(word):
    if word not in buzzwords_dict:
        return f"Word '{word}' not found in buzzwords list.", 404
    return render_template_string(EMBED_TEMPLATE, word=word, safe_id=make_safe_id(word))

@app.route('/present')
def present():
    title = request.args.get('title', '🎙️ LIVE BUZZWORD ODOMETER')
    b_data = [{"original": word, "safe_id": make_safe_id(word)} for word in buzzwords_dict.keys()]
    return render_template_string(PRESENTATION_TEMPLATE, buzzwords_data=b_data, title=title)

@app.route('/add_word', methods=['POST'])
def add_word():
    word = request.form.get('word', '').strip()
    if word and word not in buzzwords_dict:
        buzzwords_dict[word] = 0
        save_buzzwords()
    return redirect(url_for('index'))

@app.route('/remove_word', methods=['POST'])
def remove_word():
    word = request.form.get('word', '').strip()
    if word in buzzwords_dict:
        del buzzwords_dict[word]
        save_buzzwords()
    return redirect(url_for('index'))

@app.route('/reset', methods=['POST'])
def reset_counters():
    for word in buzzwords_dict.keys():
        buzzwords_dict[word] = 0
    save_buzzwords()
    notify_clients(buzzwords_dict.copy())
    return redirect(url_for('index'))

@app.route('/remove_all', methods=['POST'])
def remove_all():
    buzzwords_dict.clear()
    save_buzzwords()
    notify_clients(buzzwords_dict.copy())
    return redirect(url_for('index'))

@app.route('/totals')
def totals():
    title = request.args.get('title', '🎙️ TOTAL BUZZWORDS')
    return render_template_string(TOTALS_TEMPLATE, title=title)

@app.route('/embed_total')
def embed_total():
    return render_template_string(EMBED_TEMPLATE, word="__TOTAL__", safe_id="total")

import collections

@app.route('/api/wordcloud')
def get_wordcloud_data():
    import datetime
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    filepath = os.path.join(TRANSCRIPTS_DIR, f"transcript_{today_str}.md")
    
    if not os.path.exists(filepath):
        return json.dumps([])
        
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
        
    # Remove timestamps like [15:32:01]
    content = re.sub(r'\[\d{2}:\d{2}:\d{2}\]', '', content)
    
    # Strip punctuation and lowercase
    import string
    content = content.lower().translate(str.maketrans('', '', string.punctuation))
    
    words = content.split()
    stopwords = get_stopwords()
    
    filtered_words = [w for w in words if w not in stopwords and len(w) > 2]
    
    # Count frequencies
    counter = collections.Counter(filtered_words)
    top_words = counter.most_common(50)
    
    # Format for wordcloud2.js (array of [word, weight])
    return json.dumps([[word, count] for word, count in top_words])

@app.route('/stream')
def stream():
    """Streams data updates to the client using Server-Sent Events (SSE)."""
    def event_stream():
        client_queue = queue.Queue()
        with clients_lock:
            clients.add(client_queue)
        
        try:
            # Always send initial status on connection
            yield f"data: {json.dumps(buzzwords_dict)}\n\n"
            while True:
                # Block until the background microphone thread posts an update
                data = client_queue.get()
                yield f"data: {json.dumps(data)}\n\n"
        finally:
            with clients_lock:
                clients.discard(client_queue)

    return Response(event_stream(), mimetype="text/event-stream")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Buzzword Counter")
    parser.add_argument("--engine", choices=["classic", "realtimestt"], help="Audio engine to use")
    parser.add_argument("--insecure", action="store_true", help="Disable SSL certificate verification for corporate proxies")
    parser.add_argument("--verbose", action="store_true", help="Print real-time transcriptions to the terminal")
    parser.add_argument("--log-transcript", action="store_true", help="Log transcriptions to a markdown file")
    args = parser.parse_args()
    
    # 1. Config file
    config_engine = "classic"
    config_insecure = False
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r") as f:
                config = json.load(f)
                config_engine = config.get("engine", "classic")
                config_insecure = config.get("insecure", False)
                if "language" in config:
                    current_language = config["language"]
        except Exception as e:
            print(f"⚠️ Error reading config.json: {e}")
            
    # 2. CLI Override
    final_engine = args.engine if args.engine else config_engine
    use_insecure = args.insecure or config_insecure
    
    print(f"🚀 Starting Buzzword Counter with '{final_engine}' engine!")
    
    # 3. Apply SSL Bypasses if insecure mode is triggered
    pass
    
    if final_engine == "realtimestt":
        threading.Thread(target=realtimestt_audio_listener, daemon=True).start()
    else:
        threading.Thread(target=classic_audio_listener, daemon=True).start()
        
    app.run(debug=False, port=5000)
