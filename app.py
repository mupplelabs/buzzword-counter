import json
import queue
import threading
import base64
import ssl
import string
import speech_recognition as sr
from flask import Flask, Response, render_template_string, request, redirect, url_for
import io
import wave
import numpy as np

try:
    from faster_whisper import WhisperModel
    FASTER_WHISPER_AVAILABLE = True
except ImportError:
    FASTER_WHISPER_AVAILABLE = False

# Fix for MacOS SSL certificate issues when downloading the Whisper model
ssl._create_default_https_context = ssl._create_unverified_context

app = Flask(__name__)

# 1. Define tracked buzzwords
buzzwords_dict = {
    "Artificial Intelligence": 0,
    "Machine Learning": 0,
    "Deep Learning": 0,
    "AI": 0,
    "KI": 0,
    "Künstliche Intelligenz": 0
}

# Hidden aliases for when Whisper mishears short words (especially isolated German)
PHONETIC_ALIASES = {
    "ka'i": "KI",
    "k.e": "KI",
    "kai": "KI",
    "cut you": "KI",
    "die planning": "Deep Learning"
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

def audio_listener():
    """Listens to microphone in the background and queues updates."""
    recognizer = sr.Recognizer()
    with sr.Microphone() as source:
        print("🎙️  Calibrating microphone for ambient noise... Please stay quiet for a few seconds.")
        recognizer.adjust_for_ambient_noise(source, duration=3)
        # Bump the threshold slightly to completely ignore background hums and static
        recognizer.energy_threshold += 150
        # Disable dynamic adjustment so the library doesn't slowly erode our safety buffer back down to 0 over time
        recognizer.dynamic_energy_threshold = False
        print("✅ Calibration complete! Listening for buzzwords...")
        
        if FASTER_WHISPER_AVAILABLE:
            print("⚡ Using Faster-Whisper engine for ultra-fast transcription!")
            faster_model = WhisperModel("small", device="auto", compute_type="default")
        else:
            print("🐢 Using standard OpenAI Whisper engine. (Run 'pip install faster-whisper' to upgrade!)")
        
        while True:
            try:
                audio = recognizer.listen(source)
                
                if FASTER_WHISPER_AVAILABLE:
                    # Get 16kHz wav bytes, bypass buggy PyAV by manually extracting PCM frames into a NumPy array
                    wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
                    with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
                        frames = wf.readframes(wf.getnframes())
                        audio_array = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                    
                    segments, info = faster_model.transcribe(audio_array, condition_on_previous_text=False)
                    text = " ".join([segment.text for segment in segments]).lower()
                else:
                    text = recognizer.recognize_whisper(
                        audio, 
                        model="small", 
                        condition_on_previous_text=False
                    ).lower()
                    
                print(f"Recognized: {text}")  # Debug print to see what whisper hears
                
                # Also ignore extreme repetition loops (common Whisper bug on noise)
                if "very very very" in text:
                    continue
                
                clean_text = text.translate(str.maketrans('', '', string.punctuation))
                
                # Normalize known mishearings
                for alias, real_word in PHONETIC_ALIASES.items():
                    clean_alias = alias.lower().translate(str.maketrans('', '', string.punctuation))
                    if clean_alias in clean_text:
                        clean_text = clean_text.replace(clean_alias, real_word.lower().translate(str.maketrans('', '', string.punctuation)))

                updated = False
                # Make a copy of keys to safely iterate while the main thread might modify it
                for word in list(buzzwords_dict.keys()):
                    clean_word = word.lower().translate(str.maketrans('', '', string.punctuation))
                    if clean_word and clean_word in clean_text:
                        match_count = clean_text.count(clean_word)
                        buzzwords_dict[word] += match_count
                        updated = True
                
                if updated:
                    # Push current snapshot to all web apps
                    notify_clients(buzzwords_dict.copy())
            except (sr.UnknownValueError, sr.RequestError):
                pass

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
    </style>
</head>
<body>

    <h1>🎙️ LIVE BUZZWORD ODOMETER</h1>
    <p style="color: #7f8c8d;">Listening via microphone... Say one of your keywords!</p>
    
    <div class="controls">
        <button id="enable-sound">Enable Sound Effects 🔇</button>
        <a href="/present" target="_blank" class="btn" style="background-color: #27ae60; border-color: #2ecc71;">📺 Presentation Mode</a>
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
            e.target.innerText = soundEnabled ? "Disable Sound Effects 🔊" : "Enable Sound Effects 🔇";
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
                previousValues[word] = value;
            }

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
    </script>
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
            if (data[targetWord] !== undefined) {
                const count = data[targetWord];
                if (count !== previousValue) {
                    updateOdometer(count);
                    previousValue = count;
                }
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
            padding-top: 50px;
            margin: 0;
            min-height: 100vh;
        }
        h1 {
            color: #f39c12;
            text-shadow: 0 0 10px rgba(243, 156, 18, 0.5);
            letter-spacing: 2px;
            margin-bottom: 50px;
            font-size: 3.5em;
            text-align: center;
        }
        .container {
            display: flex;
            flex-wrap: wrap;
            justify-content: center;
            gap: 40px;
            max-width: 1400px;
            padding: 0 20px;
        }
        .card {
            background: #2c3e50;
            padding: 30px;
            border-radius: 12px;
            border: 4px solid #34495e;
            box-shadow: 0 12px 24px rgba(0,0,0,0.6);
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 20px;
            min-width: 280px;
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
            height: 60px; width: 36px; overflow: hidden; position: relative; background: linear-gradient(#222, #111 50%, #222); margin: 0 2px; border-radius: 4px; border-bottom: 2px solid #444;
        }
        .digit-strip {
            position: absolute; top: 0; left: 0; width: 100%; transition: transform 0.6s cubic-bezier(0.25, 1, 0.5, 1); display: flex; flex-direction: column;
        }
        .digit {
            height: 60px; line-height: 60px; text-align: center; font-size: 42px; font-weight: bold; color: #fff; text-shadow: 0 2px 4px rgba(0,0,0,0.8);
        }
    </style>
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
            
            if (elD3) elD3.style.transform = `translateY(-${d3 * 60}px)`;
            if (elD2) elD2.style.transform = `translateY(-${d2 * 60}px)`;
            if (elD1) elD1.style.transform = `translateY(-${d1 * 60}px)`;
            if (elD0) elD0.style.transform = `translateY(-${d0 * 60}px)`;
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
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    b_data = [{"original": word, "safe_id": make_safe_id(word)} for word in buzzwords_dict.keys()]
    return render_template_string(HTML_TEMPLATE, buzzwords_data=b_data)

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
    return redirect(url_for('index'))

@app.route('/remove_word', methods=['POST'])
def remove_word():
    word = request.form.get('word', '').strip()
    if word in buzzwords_dict:
        del buzzwords_dict[word]
    return redirect(url_for('index'))

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
    threading.Thread(target=audio_listener, daemon=True).start()
    app.run(debug=False, port=5000)
