#!/usr/bin/env python3
"""
TTS Benchmark Script for Pandrator.

Starts each backend, synthesises Stave One of A Christmas Carol using the
same settings Pandrator itself would use, saves the output WAV, then shuts
the backend down before moving to the next one.

Services benchmarked (in order):
  Kokoro GPU   – am_adam voice, USE_GPU=true
  Kokoro CPU   – am_adam voice, USE_GPU=false
  Voxtral      – neutral_male voice (GGUF)
  Chatterbox GPU (multilingual) – sample_male_new cloned voice
  Chatterbox CPU (multilingual) – sample_male_new cloned voice
  FishS2       – sample_male_new cloned voice
  VoxCPM       – sample_male_new cloned voice
  XTTS         – sample_male_new cloned voice
"""

import os
import sys
import time
import subprocess
import psutil
from pydub import AudioSegment

# Force UTF-8 output so Unicode chars in print() don't crash on Windows cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Add repo root so we can import pandrator.* modules
# ---------------------------------------------------------------------------
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pandrator.logic import tts_handler, text_preprocessor, audio_processor

# ---------------------------------------------------------------------------
# Benchmark text (Stave One, A Christmas Carol)
# ---------------------------------------------------------------------------
BENCHMARK_TEXT = """Stave One.
Marley's Ghost.
Marley was dead: to begin with. There is no doubt whatever about that. The register of his burial was signed by the clergyman, the clerk, the undertaker, and the chief mourner. Scrooge signed it: and Scrooge's name was good upon 'Change, for anything he chose to put his hand to. Old Marley was as dead as a door-nail.

Mind! I don't mean to say that I know, of my own knowledge, what there is particularly dead about a door-nail. I might have been inclined, myself, to regard a coffin-nail as the deadest piece of ironmongery in the trade. But the wisdom of our ancestors is in the simile; and my unhallowed hands shall not disturb it, or the Country's done for. You will therefore permit me to repeat, emphatically, that Marley was as dead as a door-nail.

Scrooge knew he was dead? Of course he did. How could it be otherwise? Scrooge and he were partners for I don't know how many years. Scrooge was his sole executor, his sole administrator, his sole assign, his sole residuary legatee, his sole friend, and sole mourner. And even Scrooge was not so dreadfully cut up by the sad event, but that he was an excellent man of business on the very day of the funeral, and solemnised it with an undoubted bargain.

The mention of Marley's funeral brings me back to the point I started from. There is no doubt that Marley was dead. This must be distinctly understood, or nothing wonderful can come of the story I am going to relate. If we were not perfectly convinced that Hamlet's Father died before the play began, there would be nothing more remarkable in his taking a stroll at night, in an easterly wind, upon his own ramparts, than there would be in any other middle-aged gentleman rashly turning out after dark in a breezy spot—say Saint Paul's Churchyard for instance—literally to astonish his son's weak mind.

Scrooge never painted out Old Marley's name. There it stood, years afterwards, above the warehouse door: Scrooge and Marley. The firm was known as Scrooge and Marley. Sometimes people new to the business called Scrooge Scrooge, and sometimes Marley, but he answered to both names. It was all the same to him.

Oh! But he was a tight-fisted hand at the grindstone, Scrooge! a squeezing, wrenching, grasping, scraping, clutching, covetous, old sinner! Hard and sharp as flint, from which no steel had ever struck out generous fire; secret, and self-contained, and solitary as an oyster. The cold within him froze his old features, nipped his pointed nose, shrivelled his cheek, stiffened his gait; made his eyes red, his thin lips blue; and spoke out shrewdly in his grating voice. A frosty rime was on his head, and on his eyebrows, and his wiry chin. He carried his own low temperature always about with him; he iced his office in the dog-days; and didn't thaw it one degree at Christmas.

External heat and cold had little influence on Scrooge. No warmth could warm, no wintry weather chill him. No wind that blew was bitterer than he, no falling snow was more intent upon its purpose, no pelting rain less open to entreaty. Foul weather didn't know where to have him. The heaviest rain, and snow, and hail, and sleet, could boast of the advantage over him in only one respect. They often "came down" handsomely, and Scrooge never did.

Nobody ever stopped him in the street to say, with gladsome looks, "My dear Scrooge, how are you? When will you come to see me?" No beggars implored him to bestow a trifle, no children asked him what it was o'clock, no man or woman ever once in all his life inquired the way to such and such a place, of Scrooge. Even the blind men's dogs appeared to know him; and when they saw him coming on, would tug their owners into doorways and up courts; and then would wag their tails as though they said, "No eye at all is better than an evil eye, dark master!"

But what did Scrooge care! It was the very thing he liked. To edge his way along the crowded paths of life, warning all human sympathy to keep its distance, was what the knowing ones call "nuts" to Scrooge.

Once upon a time—of all the good days in the year, on Christmas Eve—old Scrooge sat busy in his counting-house. It was cold, bleak, biting weather: foggy withal: and he could hear the people in the court outside, go wheezing up and down, beating their hands upon their breasts, and stamping their feet upon the pavement stones to warm them. The city clocks had only just gone three, but it was quite dark already—it had not been light all day—and candles were flaring in the windows of the neighbouring offices, like ruddy smears upon the palpable brown air. The fog came pouring in at every chink and keyhole, and was so dense without, that although the court was of the narrowest, the houses opposite were mere phantoms. To see the dingy cloud come drooping down, obscuring everything, one might have thought that Nature lived hard by, and was brewing on a large scale.

The door of Scrooge's counting-house was open that he might keep his eye upon his clerk, who in a dismal little cell beyond, a sort of tank, was copying letters. Scrooge had a very small fire, but the clerk's fire was so very much smaller that it looked like one coal. But he couldn't replenish it, for Scrooge kept the coal-box in his own room; and so surely as the clerk came in with the shovel, the master predicted that it would be necessary for them to part. Wherefore the clerk put on his white comforter, and tried to warm himself at the candle; in which effort, not being a man of a strong imagination, he failed.

"A merry Christmas, uncle! God save you!" cried a cheerful voice. It was the voice of Scrooge's nephew, who came upon him so quickly that this was the first intimation he had of his approach.

"Bah!" said Scrooge, "Humbug!"

He had so heated himself with rapid walking in the fog and frost, this nephew of Scrooge's, that he was all in a glow; his face was ruddy and handsome; his eyes sparkled, and his breath smoked again.

"Christmas a humbug, uncle!" said Scrooge's nephew. "You don't mean that, I am sure?"

"I do," said Scrooge. "Merry Christmas! What right have you to be merry? What reason have you to be merry? You're poor enough."

"Come, then," returned the nephew gaily. "What right have you to be dismal? What reason have you to be morose? You're rich enough."

Scrooge having no better answer ready on the spur of the moment, said, "Bah!" again; and followed it up with "Humbug."

"Don't be cross, uncle!" said the nephew.

"What else can I be," returned the uncle, "when I live in such a world of fools as this? Merry Christmas! Out upon merry Christmas! What's Christmas time to you but a time for paying bills without money; a time for finding yourself a year older, but not an hour richer; a time for balancing your books and having every item in 'em through a round dozen of months presented dead against you? If I could work my will," said Scrooge indignantly, "every idiot who goes about with 'Merry Christmas' on his lips, should be boiled with his own pudding, and buried with a stake of holly through his heart. He should!"

"Uncle!" pleaded the nephew.

"Nephew!" returned the uncle sternly, "keep Christmas in your own way, and let me keep it in mine."

"Keep it!" repeated Scrooge's nephew. "But you don't keep it."

"Let me leave it alone, then," said Scrooge. "Much good may it do you! Much good it has ever done you!"

"There are many things from which I might have derived good, by which I have not profited, I dare say," returned the nephew. "Christmas among the rest. But I am sure I have always thought of Christmas time, when it has come round—apart from the veneration due to its sacred name and origin, if anything belonging to it can be apart from that—as a good time; a kind, forgiving, charitable, pleasant time; the only time I know of, in the long calendar of the year, when men and women seem by one consent to open their shut-up hearts freely, and to think of people below them as if they really were fellow-passengers to the grave, and not another race of creatures bound on other journeys. And therefore, uncle, though it has never put a scrap of gold or silver in my pocket, I believe that it has done me good, and will do me good; and I say, God bless it!"

The clerk in the Tank involuntarily applauded. Becoming immediately sensible of the impropriety, he poked the fire, and extinguished the last frail spark for ever.

"Let me hear another sound from you," said Scrooge, "and you'll keep your Christmas by losing your situation! You're quite a powerful speaker, sir," he added, turning to his nephew. "I wonder you don't go into Parliament."

"Don't be angry, uncle. Come! Dine with us to-morrow."

Scrooge said that he would see him—yes, indeed he did. He went the whole length of the expression, and said that he would see him in that extremity first.

"But why?" cried Scrooge's nephew. "Why?"

"Why did you get married?" said Scrooge.

"Because I fell in love."

"Because you fell in love!" growled Scrooge, as if that were the only one thing in the world more ridiculous than a merry Christmas. "Good afternoon!"

"Nay, uncle, but you never came to see me before that happened. Why give it as a reason for not coming now?"

"Good afternoon," said Scrooge.

"I want nothing from you; I ask nothing of you; why cannot we be friends?"

"Good afternoon," said Scrooge.

"I am sorry, with all my heart, to find you so resolute. We have never had any quarrel, to which I have been a party. But I have made the trial in homage to Christmas, and I'll keep my Christmas humour to the last. So A Merry Christmas, uncle!"

"Good afternoon!" said Scrooge.

"And A Happy New Year!"

"Good afternoon!" said Scrooge."""

# ---------------------------------------------------------------------------
# Reference voice & transcript (used by Chatterbox, FishS2, VoxCPM, XTTS)
# ---------------------------------------------------------------------------
VOICE_FILE_PATH = r"E:\Pandrator\Pandrator\tts_voices\sample_male_new.wav"
if not os.path.exists(VOICE_FILE_PATH):
    VOICE_FILE_PATH = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "tts_voices", "sample_male_new.wav")
    )

# Transcript of the reference voice sample (from voice_library.json)
REFERENCE_TRANSCRIPT = (
    "The window was open, granted, but the room is on the second floor. "
    "Anyway, you may dismiss the window. I remember the old lady saying there was a bar across it, "
    "and that nobody could have squeezed through."
)

OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Outputs", "benchmark"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

BASE_DIR = r"E:\Pandrator"
PIXI_EXE = os.path.join(BASE_DIR, "bin", "pixi.exe")

# ---------------------------------------------------------------------------
# Audio processing defaults (mirror Pandrator's AudioProcessingSettings defaults)
# ---------------------------------------------------------------------------
FADE_IN_MS = 75
FADE_OUT_MS = 75
SILENCE_BETWEEN_SENTENCES_MS = 300
SILENCE_FOR_PARAGRAPHS_MS = 1000


# ---------------------------------------------------------------------------
# Service definitions
# ---------------------------------------------------------------------------
# Each entry is a dict with:
#   id           – unique label used in filenames
#   service      – Pandrator service name (passed to tts_handler)
#   port         – port the backend listens on
#   max_len      – max_sentence_length for Pandrator's text preprocessor
#   voice_id     – speaker id / preset passed to the API
#   voice_label  – used in the output filename
#   model_name   – value for tts_settings["xtts_model"]
#   use_ref_voice– True if this service needs the reference WAV cloned
#   extra_tts    – dict of additional tts_settings overrides (optional)
#   startup_env  – dict of extra env vars injected when starting the backend
#   timeout      – how long (s) to wait for the server to come online

SERVICES = [
    {
        "id": "kokoro_gpu",
        "service": "Kokoro",
        "port": 8880,
        "max_len": 200,
        "voice_id": "am_adam",
        "voice_label": "am_adam_gpu",
        "model_name": "kokoro",
        "use_ref_voice": False,
        "extra_tts": {},
        "startup_env": {"USE_GPU": "true", "USE_ONNX": "false"},
        "timeout": 120,
    },
    {
        "id": "kokoro_cpu",
        "service": "Kokoro",
        "port": 8880,
        "max_len": 200,
        "voice_id": "am_adam",
        "voice_label": "am_adam_cpu",
        "model_name": "kokoro",
        "use_ref_voice": False,
        "extra_tts": {},
        "startup_env": {"USE_GPU": "false", "USE_ONNX": "false"},
        "timeout": 180,
    },
    {
        "id": "voxtral",
        "service": "Voxtral",
        "port": 8000,
        "max_len": 200,
        "voice_id": "neutral_male",
        "voice_label": "neutral_male",
        "model_name": "auto",
        "use_ref_voice": False,
        "extra_tts": {
            "voxtral_max_frames": 1024,
            "voxtral_euler_steps": 8,
            "voxtral_chunk": False,
            "voxtral_max_chunk_chars": 500,
            "voxtral_chunk_silence_ms": 0,
            "voxtral_strip_quotes": False,
            "voxtral_strip_diacritics": False,
            "voxtral_level_audio": False,
        },
        "startup_env": {},
        "timeout": 120,
    },
    {
        "id": "chatterbox_gpu",
        "service": "Chatterbox",
        "port": 8040,
        "max_len": 200,
        "voice_id": "sample_male_new",
        "voice_label": "sample_male_new_gpu",
        "model_name": "chatterbox-multilingual",
        "use_ref_voice": True,
        "extra_tts": {
            "chatterbox_temperature": 0.8,
            "chatterbox_repetition_penalty": 1.2,
            "chatterbox_min_p": 0.05,
            # Pandrator sets top_p=1.0 when chatterbox-multilingual is selected
            "chatterbox_top_p": 1.0,
            "chatterbox_top_k": 1000,
            "chatterbox_exaggeration": 0.5,
            "chatterbox_cfg_weight": 0.5,
            "chatterbox_norm_loudness": True,
        },
        # No CHATTERBOX_DEVICE override → server defaults to cuda if available
        "startup_env": {},
        "timeout": 300,
    },
    {
        "id": "chatterbox_cpu",
        "service": "Chatterbox",
        "port": 8040,
        "max_len": 200,
        "voice_id": "sample_male_new",
        "voice_label": "sample_male_new_cpu",
        "model_name": "chatterbox-multilingual",
        "use_ref_voice": True,
        "extra_tts": {
            "chatterbox_temperature": 0.8,
            "chatterbox_repetition_penalty": 1.2,
            "chatterbox_min_p": 0.05,
            # Pandrator sets top_p=1.0 when chatterbox-multilingual is selected
            "chatterbox_top_p": 1.0,
            "chatterbox_top_k": 1000,
            "chatterbox_exaggeration": 0.5,
            "chatterbox_cfg_weight": 0.5,
            "chatterbox_norm_loudness": True,
        },
        # Force CPU inference via server env var
        "startup_env": {"CHATTERBOX_DEVICE": "cpu"},
        "timeout": 300,
    },
    {
        "id": "fishs2",
        "service": "FishS2",
        "port": 8020,
        "max_len": 200,
        "voice_id": "sample_male_new",
        "voice_label": "sample_male_new",
        "model_name": "fishaudio/s2-pro",
        "use_ref_voice": True,
        "extra_tts": {
            # fishs2_chunk_length is clamped to 300 max inside tts_handler;
            # keep it at the app_state default of 200 to match Pandrator defaults.
            "fishs2_temperature": 0.7,
            "fishs2_top_p": 0.7,
            "fishs2_chunk_length": 200,
            "fishs2_latency": "balanced",
            "fishs2_normalize": True,
            "fishs2_prosody_volume": 0.0,
            "fishs2_normalize_loudness": True,
        },
        "startup_env": {},
        "timeout": 120,
    },
    {
        "id": "voxcpm",
        "service": "VoxCPM",
        "port": 8020,
        "max_len": 200,
        "voice_id": "sample_male_new",
        "voice_label": "sample_male_new",
        "model_name": "openbmb/VoxCPM2",
        "use_ref_voice": True,
        "extra_tts": {
            "voxcpm_cfg_value": 1.5,
            "voxcpm_inference_timesteps": 15,
            "voxcpm_normalize": False,
            "voxcpm_denoise": False,
            "voxcpm_retry_badcase": True,
            "voxcpm_retry_badcase_max_times": 3,
            "voxcpm_retry_badcase_ratio_threshold": 6.0,
            "voxcpm_min_len": 2,
            "voxcpm_max_len": 4096,
        },
        "startup_env": {},
        "timeout": 120,
    },
    {
        "id": "xtts",
        "service": "XTTS",
        "port": 8020,
        "max_len": 200,
        "voice_id": "sample_male_new",
        "voice_label": "sample_male_new",
        "model_name": "tts_models/multilingual/multi-dataset/xtts_v2",
        "use_ref_voice": True,
        "extra_tts": {},
        "startup_env": {"XTTS_USE_DEEPSPEED": "false"},
        "timeout": 120,
    },
]


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def get_backend_env(extra: dict | None = None):
    """Build env dict with all E:/Pandrator cache/path overrides."""
    env = os.environ.copy()
    pixi_home = os.path.join(BASE_DIR, ".pixi-home")
    pixi_cache = os.path.join(BASE_DIR, ".pixi-cache")
    rattler_cache = os.path.join(pixi_cache, "rattler")
    pip_cache = os.path.join(pixi_cache, "pip")
    local_temp = os.path.join(pixi_cache, "tmp")

    for d in [pixi_home, pixi_cache, rattler_cache, pip_cache, local_temp]:
        os.makedirs(d, exist_ok=True)

    env.update({
        "PIXI_HOME": pixi_home,
        "PIXI_CACHE_DIR": pixi_cache,
        "RATTLER_CACHE_DIR": rattler_cache,
        "PIP_CACHE_DIR": pip_cache,
        "TMP": local_temp,
        "TEMP": local_temp,
        "TMPDIR": local_temp,
    })

    local_cache_root = os.path.join(BASE_DIR, "cache")
    env.update({
        "XDG_CACHE_HOME": local_cache_root,
        "HF_HOME": os.path.join(local_cache_root, "huggingface"),
        "HF_HUB_CACHE": os.path.join(local_cache_root, "huggingface", "hub"),
        "HUGGINGFACE_HUB_CACHE": os.path.join(local_cache_root, "huggingface", "hub"),
        "TRANSFORMERS_CACHE": os.path.join(local_cache_root, "huggingface", "transformers"),
        "TORCH_HOME": os.path.join(local_cache_root, "torch"),
        "TTS_HOME": os.path.join(local_cache_root, "tts"),
    })

    if extra:
        env.update(extra)

    return env


def resolve_espeak_paths():
    """Return (dll_path, data_path) for eSpeak-NG if installed."""
    candidate_roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    for root in candidate_roots:
        if not root:
            continue
        dll = os.path.join(root, "eSpeak NG", "libespeak-ng.dll")
        data = os.path.join(root, "eSpeak NG", "espeak-ng-data")
        if os.path.exists(dll):
            return dll, (data if os.path.exists(data) else "")
    return "", ""


def kill_process_tree(proc):
    """Recursively terminate a process and all children."""
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for child in children:
        try:
            child.terminate()
        except Exception:
            pass
    try:
        parent.terminate()
    except Exception:
        pass
    _, alive = psutil.wait_procs(children + [parent], timeout=5)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass


def kill_processes_on_port(port):
    """Kill any process listening on *port*."""
    print(f"  Checking for processes on port {port}...")
    try:
        connections = psutil.net_connections()
    except Exception as exc:
        print(f"  Could not enumerate connections: {exc}")
        return
    seen = set()
    for conn in connections:
        if conn.laddr and conn.laddr.port == port and conn.pid and conn.pid not in seen:
            seen.add(conn.pid)
            print(f"  Port {port} occupied by PID {conn.pid}. Terminating...")
            try:
                kill_process_tree(psutil.Process(conn.pid))
            except Exception as exc:
                print(f"  Failed to terminate PID {conn.pid}: {exc}")


# ---------------------------------------------------------------------------
# Backend lifecycle
# ---------------------------------------------------------------------------

def start_backend(svc: dict):
    """Launch the server process for the given service definition."""
    port = svc["port"]
    service = svc["service"]
    kill_processes_on_port(port)

    env = get_backend_env(svc.get("startup_env") or {})
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    print(f"Starting {svc['id']} server...")
    process = None

    if service == "XTTS":
        cwd = os.path.join(BASE_DIR, "xtts2_api")
        cmd = ["cmd", "/c", "run.bat", "--backend", "cuda", "--pixi-path", PIXI_EXE]
        log_path = os.path.join(cwd, f"xtts_benchmark_{svc['id']}.log")
        log_file = open(log_path, "w", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
        process.log_file = log_file

    elif service == "VoxCPM":
        cwd = os.path.join(BASE_DIR, "voxcpm_fastapi")
        cmd = ["cmd", "/c", "run.bat", "--pixi-path", PIXI_EXE]
        log_path = os.path.join(cwd, f"voxcpm_benchmark_{svc['id']}.log")
        log_file = open(log_path, "w", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
        process.log_file = log_file

    elif service == "FishS2":
        cwd = os.path.join(BASE_DIR, "fishs2-cpp-fastapi")
        cmd = ["cmd", "/c", "run.bat", "--pixi-path", PIXI_EXE]
        log_path = os.path.join(cwd, f"fishs2_benchmark_{svc['id']}.log")
        log_file = open(log_path, "w", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
        process.log_file = log_file

    elif service == "Chatterbox":
        cwd = os.path.join(BASE_DIR, "chatterbox-fastapi")
        # Pass --backend explicitly: run.bat uses setlocal so env vars don't propagate;
        # the CLI arg is the correct mechanism to select cpu vs cuda.
        chatterbox_backend = "cpu" if (svc.get("startup_env") or {}).get("CHATTERBOX_DEVICE") == "cpu" else "cuda"
        cmd = ["cmd", "/c", "run.bat", "--backend", chatterbox_backend, "--pixi-path", PIXI_EXE]
        log_path = os.path.join(cwd, f"chatterbox_benchmark_{svc['id']}.log")
        log_file = open(log_path, "w", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
        process.log_file = log_file

    elif service == "Voxtral":
        cwd = os.path.join(BASE_DIR, "voxtral-fastapi")
        cmd = [
            "powershell",
            "-ExecutionPolicy", "Bypass",
            "-File", os.path.join(cwd, "run.ps1"),
            "-ProjectRoot", cwd,
            "-BindHost", "127.0.0.1",
            "-Port", "8000",
            "-Model", "gguf",
        ]
        log_path = os.path.join(cwd, f"voxtral_benchmark_{svc['id']}.log")
        log_file = open(log_path, "w", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
        process.log_file = log_file

    elif service == "Kokoro":
        cwd = os.path.join(BASE_DIR, "Kokoro-FastAPI")
        manifest_path = os.path.join(BASE_DIR, "envs", "kokoro_api_server_installer", "pixi.toml")
        cmd = [
            PIXI_EXE, "run",
            "--manifest-path", manifest_path,
            "--executable", "python",
            "-m", "uvicorn",
            "api.src.main:app",
            "--host", "127.0.0.1",
            "--port", "8880",
        ]
        env["PYTHONUTF8"] = "1"
        env["USE_ONNX"] = "false"
        env["MODEL_DIR"] = "src/models"
        env["VOICES_DIR"] = "src/voices/v1_0"
        env["WEB_PLAYER_PATH"] = os.path.join(cwd, "web")
        env["PYTHONPATH"] = f"{cwd};{os.path.join(cwd, 'api')}"
        # USE_GPU is already set from svc["startup_env"] via get_backend_env above

        dll_path, data_path = resolve_espeak_paths()
        if dll_path:
            env["PHONEMIZER_ESPEAK_LIBRARY"] = dll_path
        if data_path:
            env["PHONEMIZER_ESPEAK_DATA"] = data_path
            env["ESPEAK_DATA_PATH"] = data_path

        log_path = os.path.join(cwd, f"kokoro_benchmark_{svc['id']}.log")
        log_file = open(log_path, "w", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
        process.log_file = log_file

    else:
        print(f"  ERROR: Unknown service '{service}'")
        return None

    return process


def check_connection(service: str) -> bool:
    """Return True if the server for *service* is responding."""
    if service == "XTTS":
        return tts_handler.check_xtts_connection()
    elif service == "VoxCPM":
        return tts_handler.check_voxcpm_connection()
    elif service == "FishS2":
        return tts_handler.check_fishs2_connection()
    elif service == "Chatterbox":
        return tts_handler.check_chatterbox_connection()
    elif service == "Voxtral":
        return tts_handler.check_voxtral_connection()
    elif service == "Kokoro":
        return tts_handler.check_kokoro_connection()
    return False


def wait_until_online(svc: dict, process, timeout: int = 120) -> bool:
    """Poll the server health endpoint until it responds or times out."""
    service = svc["service"]
    start = time.time()
    while time.time() - start < timeout:
        if process.poll() is not None:
            print(f"  ERROR: {svc['id']} server exited early (code {process.poll()}).")
            return False
        if check_connection(service):
            print(f"  {svc['id']} server is online.")
            return True
        time.sleep(2)
    print(f"  Timeout: {svc['id']} server did not come online within {timeout}s.")
    return False


def stop_backend(svc: dict, process):
    """Stop the server and clean up its port."""
    print(f"  Stopping {svc['id']} server...")
    if process:
        try:
            kill_process_tree(process)
        except Exception as exc:
            print(f"  Error killing process tree: {exc}")
        if hasattr(process, "log_file") and process.log_file:
            try:
                process.log_file.close()
            except Exception:
                pass
    kill_processes_on_port(svc["port"])


# ---------------------------------------------------------------------------
# Single benchmark run
# ---------------------------------------------------------------------------

def run_benchmark(svc: dict):
    """Run the full benchmark for one service definition. Returns True on success."""
    print(f"\n{'='*50}")
    print(f"Benchmarking: {svc['id']}")
    print(f"{'='*50}")

    service = svc["service"]
    process = None
    try:
        process = start_backend(svc)
        if not process:
            print(f"  SKIP: could not start server for {svc['id']}.")
            return False

        if not wait_until_online(svc, process, timeout=svc["timeout"]):
            print(f"  SKIP: {svc['id']} never came online.")
            return False

        # ------------------------------------------------------------------
        # Upload reference voice (for voice-cloning backends)
        # ------------------------------------------------------------------
        voice_id = svc["voice_id"]
        if svc["use_ref_voice"]:
            base_url_map = {
                "XTTS": tts_handler.XTTS_API_BASE_URL,
                "VoxCPM": tts_handler.VOXCPM_API_BASE_URL,
                "FishS2": tts_handler.FISHS2_API_BASE_URL,
                "Chatterbox": tts_handler.CHATTERBOX_API_BASE_URL,
            }
            base_url = base_url_map.get(service, tts_handler.XTTS_API_BASE_URL)
            print(f"  Uploading reference voice to {svc['id']}...")

            # XTTS does not need prompt_text; others do
            if service == "XTTS":
                voice_id = tts_handler.upload_speaker_voice(
                    VOICE_FILE_PATH,
                    base_url=base_url,
                    service=service,
                    voice_id="sample_male_new",
                )
            else:
                voice_id = tts_handler.upload_speaker_voice(
                    VOICE_FILE_PATH,
                    base_url=base_url,
                    service=service,
                    voice_id="sample_male_new",
                    prompt_text=REFERENCE_TRANSCRIPT,
                )
            print(f"  Reference voice uploaded. Voice ID: {voice_id}")

        # ------------------------------------------------------------------
        # Build tts_settings exactly as Pandrator does:
        # Pass state.tts.__dict__ — here we replicate all relevant fields
        # with their app_state defaults, then apply service-specific overrides.
        # ------------------------------------------------------------------
        tts_settings = {
            # Core routing fields
            "service": service,
            "speaker": voice_id,
            "language": "en",
            "speed": 1.0,
            # xtts_model drives the model selection for all services (not just XTTS)
            "xtts_model": svc["model_name"],
            # XTTS advanced flags (all off by default, matching app_state defaults)
            "temperature": 0.75,
            "length_penalty": 1.0,
            "repetition_penalty": 5.0,
            "top_k": 50,
            "top_p": 0.85,
            "do_sample": True,
            "num_beams": 1,
            "enable_text_splitting": True,
            "stream_chunk_size": 100,
            "gpt_cond_len": 12,
            "gpt_cond_chunk_len": 4,
            "max_ref_len": 12,
            "sound_norm_refs": False,
            "overlap_wav_len": 1024,
            "xtts_send_temperature": False,
            "xtts_send_length_penalty": False,
            "xtts_send_repetition_penalty": False,
            "xtts_send_top_k": False,
            "xtts_send_top_p": False,
            "xtts_send_do_sample": False,
            "xtts_send_num_beams": False,
            "xtts_send_stream_chunk_size": False,
            "xtts_send_enable_text_splitting": False,
            "xtts_send_gpt_cond_len": False,
            "xtts_send_gpt_cond_chunk_len": False,
            "xtts_send_max_ref_len": False,
            "xtts_send_sound_norm_refs": False,
            "xtts_send_overlap_wav_len": False,
            # VoxCPM defaults
            "voxcpm_cfg_value": 1.5,
            "voxcpm_inference_timesteps": 15,
            "voxcpm_normalize": False,
            "voxcpm_denoise": False,
            "voxcpm_retry_badcase": True,
            "voxcpm_retry_badcase_max_times": 3,
            "voxcpm_retry_badcase_ratio_threshold": 6.0,
            "voxcpm_min_len": 2,
            "voxcpm_max_len": 4096,
            # FishS2 defaults (chunk_length default in app_state is 200)
            "fishs2_temperature": 0.7,
            "fishs2_top_p": 0.7,
            "fishs2_chunk_length": 200,
            "fishs2_latency": "balanced",
            "fishs2_normalize": True,
            "fishs2_prosody_volume": 0.0,
            "fishs2_normalize_loudness": True,
            # Voxtral defaults
            "voxtral_max_frames": 1024,
            "voxtral_euler_steps": 8,
            "voxtral_chunk": False,
            "voxtral_max_chunk_chars": 500,
            "voxtral_chunk_silence_ms": 0,
            "voxtral_strip_quotes": False,
            "voxtral_strip_diacritics": False,
            "voxtral_level_audio": False,
            # Chatterbox defaults
            "chatterbox_temperature": 0.8,
            "chatterbox_repetition_penalty": 1.2,
            "chatterbox_min_p": 0.05,
            "chatterbox_top_p": 0.95,
            "chatterbox_top_k": 1000,
            "chatterbox_exaggeration": 0.5,
            "chatterbox_cfg_weight": 0.5,
            "chatterbox_norm_loudness": True,
            # openai_audio_instructions not used
            "openai_audio_instructions": "",
        }
        # Apply service-specific overrides
        tts_settings.update(svc.get("extra_tts") or {})

        # ------------------------------------------------------------------
        # Preprocess text using Pandrator's native text_preprocessor
        # ------------------------------------------------------------------
        max_len = svc["max_len"]
        pre_settings = {
            "max_sentence_length": max_len,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": True,
            "remove_diacritics": False,
            "remove_quotation_marks": False,
            "disable_paragraph_detection": True,
            "language": "en",
            "tts_service": service,
        }
        sentences = text_preprocessor.preprocess_text(BENCHMARK_TEXT, pre_settings)
        print(f"  Text preprocessed into {len(sentences)} sentences (max_len={max_len}).")

        # ------------------------------------------------------------------
        # Synthesis loop – mirrors Pandrator's _generate_sentence_audio
        # ------------------------------------------------------------------
        audio_segments = []
        synthesis_times = []

        synthesis_start_wall = time.time()

        for idx, sentence_dict in enumerate(sentences, 1):
            text = sentence_dict["original_sentence"]
            print(f"  Sentence {idx}/{len(sentences)} ({len(text)} chars)...", flush=True)

            sent_start = time.time()
            audio_data = tts_handler.text_to_audio(text, tts_settings, max_attempts=3)
            sent_elapsed = time.time() - sent_start

            if not audio_data:
                print(f"  WARNING: sentence {idx} failed; skipping.")
                continue

            synthesis_times.append(sent_elapsed)

            # Apply fade (Pandrator default: enable_fade=True, 75ms/75ms)
            audio_data = audio_processor.apply_fade(audio_data, FADE_IN_MS, FADE_OUT_MS)

            # Add silence (matches Pandrator's AudioProcessingSettings defaults)
            silence_ms = (
                SILENCE_FOR_PARAGRAPHS_MS
                if sentence_dict.get("paragraph", "no") == "yes"
                else SILENCE_BETWEEN_SENTENCES_MS
            )
            audio_data += AudioSegment.silent(duration=silence_ms)

            audio_segments.append(audio_data)
            print(f"    -> done in {sent_elapsed:.2f}s  (audio: {len(audio_data)/1000:.2f}s)", flush=True)

        synthesis_total = time.time() - synthesis_start_wall

        if not audio_segments:
            print(f"  ERROR: no audio segments produced for {svc['id']}.")
            return False

        # ------------------------------------------------------------------
        # Stitch & save
        # ------------------------------------------------------------------
        print("  Stitching audio segments...")
        final_audio = AudioSegment.empty()
        for seg in audio_segments:
            final_audio += seg

        # Use total synthesis duration (rounded to 1 decimal) in the filename
        duration_str = f"{synthesis_total:.1f}s"
        filename = f"{svc['id']}_{duration_str}.wav"
        output_path = os.path.join(OUTPUT_DIR, filename)
        print(f"  Saving -> {output_path}")
        final_audio.export(output_path, format="wav")

        # ------------------------------------------------------------------
        # Stats
        # ------------------------------------------------------------------
        audio_dur = len(final_audio) / 1000.0
        print(f"\n  [OK] Benchmark complete for {svc['id']}")
        print(f"    Sentences:          {len(audio_segments)}/{len(sentences)}")
        print(f"    Total synth time:   {synthesis_total:.2f}s")
        if synthesis_times:
            print(f"    Avg per sentence:   {sum(synthesis_times)/len(synthesis_times):.2f}s")
        print(f"    Audio duration:     {audio_dur:.2f}s")
        if audio_dur > 0:
            print(f"    RTF:                {synthesis_total / audio_dur:.4f}")
        print(f"    Output:             {filename}")
        return True

    except Exception as exc:
        import traceback
        print(f"  ERROR during {svc['id']} benchmark: {exc}")
        traceback.print_exc()
        return False

    finally:
        if process:
            stop_backend(svc, process)
        else:
            kill_processes_on_port(svc["port"])
        print("  Waiting 5 s for VRAM/RAM to settle...")
        time.sleep(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("TTS Benchmark Tool starting...")
    if not os.path.exists(VOICE_FILE_PATH):
        print(f"ERROR: Reference voice not found at '{VOICE_FILE_PATH}'")
        sys.exit(1)
    print(f"Reference voice: {VOICE_FILE_PATH}")
    print(f"Output directory: {OUTPUT_DIR}\n")

    results = {}
    for svc in SERVICES:
        ok = run_benchmark(svc)
        results[svc["id"]] = "OK" if ok else "FAILED"

    print("\n" + "="*50)
    print("Benchmark summary:")
    for name, status in results.items():
        print(f"  {name:35s} {status}")
    print("="*50)


if __name__ == "__main__":
    main()
