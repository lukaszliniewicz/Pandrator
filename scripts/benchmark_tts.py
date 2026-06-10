#!/usr/bin/env python3
import os
import sys
import time
import subprocess
import socket
import requests
import psutil
from datetime import datetime
from pydub import AudioSegment

# Add pandrator module directory to system path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pandrator.logic import tts_handler, text_preprocessor, audio_processor

BENCHMARK_TEXT = """STAVE  ONE.
MARLEY’S GHOST.
Marley was dead: to begin with. There is no doubt whatever about that. The register of his burial was signed by the clergyman, the clerk, the undertaker, and the chief mourner. Scrooge signed it: and Scrooge’s name was good upon ’Change, for anything he chose to put his hand to. Old Marley was as dead as a door-nail.

Mind! I don’t mean to say that I know, of my own knowledge, what there is particularly dead about a door-nail. I might have been inclined, myself, to regard a coffin-nail as the deadest piece of ironmongery in the trade. But the wisdom of our ancestors is in the simile; and my unhallowed hands shall not disturb it, or the Country’s done for. You will therefore permit me to repeat, emphatically, that Marley was as dead as a door-nail.

Scrooge knew he was dead? Of course he did. How could it be otherwise? Scrooge and he were partners for I don’t know how many years. Scrooge was his sole executor, his sole administrator, his sole assign, his sole residuary legatee, his sole friend, and sole mourner. And even Scrooge was not so dreadfully cut up by the sad event, but that he was an excellent man of business on the very day of the funeral, and solemnised it with an undoubted bargain.

The mention of Marley’s funeral brings me back to the point I started from. There is no doubt that Marley was dead. This must be distinctly understood, or nothing wonderful can come of the story I am going to relate. If we were not perfectly convinced that Hamlet’s Father died before the play began, there would be nothing more remarkable in his taking a stroll at night, in an easterly wind, upon his own ramparts, than there would be in any other middle-aged gentleman rashly turning out after dark in a breezy spot—say Saint Paul’s Churchyard for instance—literally to astonish his son’s weak mind.

Scrooge never painted out Old Marley’s name. There it stood, years afterwards, above the warehouse door: Scrooge and Marley. The firm was known as Scrooge and Marley. Sometimes people new to the business called Scrooge Scrooge, and sometimes Marley, but he answered to both names. It was all the same to him.

Oh! But he was a tight-fisted hand at the grindstone, Scrooge! a squeezing, wrenching, grasping, scraping, clutching, covetous, old sinner! Hard and sharp as flint, from which no steel had ever struck out generous fire; secret, and self-contained, and solitary as an oyster. The cold within him froze his old features, nipped his pointed nose, shrivelled his cheek, stiffened his gait; made his eyes red, his thin lips blue; and spoke out shrewdly in his grating voice. A frosty rime was on his head, and on his eyebrows, and his wiry chin. He carried his own low temperature always about with him; he iced his office in the dog-days; and didn’t thaw it one degree at Christmas.

External heat and cold had little influence on Scrooge. No warmth could warm, no wintry weather chill him. No wind that blew was bitterer than he, no falling snow was more intent upon its purpose, no pelting rain less open to entreaty. Foul weather didn’t know where to have him. The heaviest rain, and snow, and hail, and sleet, could boast of the advantage over him in only one respect. They often “came down” handsomely, and Scrooge never did.

Nobody ever stopped him in the street to say, with gladsome looks, “My dear Scrooge, how are you? When will you come to see me?” No beggars implored him to bestow a trifle, no children asked him what it was o’clock, no man or woman ever once in all his life inquired the way to such and such a place, of Scrooge. Even the blind men’s dogs appeared to know him; and when they saw him coming on, would tug their owners into doorways and up courts; and then would wag their tails as though they said, “No eye at all is better than an evil eye, dark master!”

But what did Scrooge care! It was the very thing he liked. To edge his way along the crowded paths of life, warning all human sympathy to keep its distance, was what the knowing ones call “nuts” to Scrooge.

Once upon a time—of all the good days in the year, on Christmas Eve—old Scrooge sat busy in his counting-house. It was cold, bleak, biting weather: foggy withal: and he could hear the people in the court outside, go wheezing up and down, beating their hands upon their breasts, and stamping their feet upon the pavement stones to warm them. The city clocks had only just gone three, but it was quite dark already—it had not been light all day—and candles were flaring in the windows of the neighbouring offices, like ruddy smears upon the palpable brown air. The fog came pouring in at every chink and keyhole, and was so dense without, that although the court was of the narrowest, the houses opposite were mere phantoms. To see the dingy cloud come drooping down, obscuring everything, one might have thought that Nature lived hard by, and was brewing on a large scale.

The door of Scrooge’s counting-house was open that he might keep his eye upon his clerk, who in a dismal little cell beyond, a sort of tank, was copying letters. Scrooge had a very small fire, but the clerk’s fire was so very much smaller that it looked like one coal. But he couldn’t replenish it, for Scrooge kept the coal-box in his own room; and so surely as the clerk came in with the shovel, the master predicted that it would be necessary for them to part. Wherefore the clerk put on his white comforter, and tried to warm himself at the candle; in which effort, not being a man of a strong imagination, he failed.

“A merry Christmas, uncle! God save you!” cried a cheerful voice. It was the voice of Scrooge’s nephew, who came upon him so quickly that this was the first intimation he had of his approach.

“Bah!” said Scrooge, “Humbug!”

He had so heated himself with rapid walking in the fog and frost, this nephew of Scrooge’s, that he was all in a glow; his face was ruddy and handsome; his eyes sparkled, and his breath smoked again.

“Christmas a humbug, uncle!” said Scrooge’s nephew. “You don’t mean that, I am sure?”

“I do,” said Scrooge. “Merry Christmas! What right have you to be merry? What reason have you to be merry? You’re poor enough.”

“Come, then,” returned the nephew gaily. “What right have you to be dismal? What reason have you to be morose? You’re rich enough.”

Scrooge having no better answer ready on the spur of the moment, said, “Bah!” again; and followed it up with “Humbug.”

“Don’t be cross, uncle!” said the nephew.

“What else can I be,” returned the uncle, “when I live in such a world of fools as this? Merry Christmas! Out upon merry Christmas! What’s Christmas time to you but a time for paying bills without money; a time for finding yourself a year older, but not an hour richer; a time for balancing your books and having every item in ’em through a round dozen of months presented dead against you? If I could work my will,” said Scrooge indignantly, “every idiot who goes about with ‘Merry Christmas’ on his lips, should be boiled with his own pudding, and buried with a stake of holly through his heart. He should!”

“Uncle!” pleaded the nephew.

“Nephew!” returned the uncle sternly, “keep Christmas in your own way, and let me keep it in mine.”

“Keep it!” repeated Scrooge’s nephew. “But you don’t keep it.”

“Let me leave it alone, then,” said Scrooge. “Much good may it do you! Much good it has ever done you!”

“There are many things from which I might have derived good, by which I have not profited, I dare say,” returned the nephew. “Christmas among the rest. But I am sure I have always thought of Christmas time, when it has come round—apart from the veneration due to its sacred name and origin, if anything belonging to it can be apart from that—as a good time; a kind, forgiving, charitable, pleasant time; the only time I know of, in the long calendar of the year, when men and women seem by one consent to open their shut-up hearts freely, and to think of people below them as if they really were fellow-passengers to the grave, and not another race of creatures bound on other journeys. And therefore, uncle, though it has never put a scrap of gold or silver in my pocket, I believe that it has done me good, and will do me good; and I say, God bless it!”

The clerk in the Tank involuntarily applauded. Becoming immediately sensible of the impropriety, he poked the fire, and extinguished the last frail spark for ever.

“Let me hear another sound from you,” said Scrooge, “and you’ll keep your Christmas by losing your situation! You’re quite a powerful speaker, sir,” he added, turning to his nephew. “I wonder you don’t go into Parliament.”

“Don’t be angry, uncle. Come! Dine with us to-morrow.”

Scrooge said that he would see him—yes, indeed he did. He went the whole length of the expression, and said that he would see him in that extremity first.

“But why?” cried Scrooge’s nephew. “Why?”

“Why did you get married?” said Scrooge.

“Because I fell in love.”

“Because you fell in love!” growled Scrooge, as if that were the only one thing in the world more ridiculous than a merry Christmas. “Good afternoon!”

“Nay, uncle, but you never came to see me before that happened. Why give it as a reason for not coming now?”

“Good afternoon,” said Scrooge.

“I want nothing from you; I ask nothing of you; why cannot we be friends?”

“Good afternoon,” said Scrooge.

“I am sorry, with all my heart, to find you so resolute. We have never had any quarrel, to which I have been a party. But I have made the trial in homage to Christmas, and I’ll keep my Christmas humour to the last. So A Merry Christmas, uncle!”

“Good afternoon!” said Scrooge.

“And A Happy New Year!”

“Good afternoon!” said Scrooge."""

VOICE_FILE_PATH = r"E:\Pandrator\Pandrator\tts_voices\sample_male_new.wav"
if not os.path.exists(VOICE_FILE_PATH):
    VOICE_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tts_voices", "sample_male_new.wav"))

REFERENCE_TRANSCRIPT = "The window was open, granted, but the room is on the second floor. Anyway, you may dismiss the window. I remember the old lady saying there was a bar across it, and that nobody could have squeezed through."

OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Outputs", "benchmark"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

BASE_DIR = r"E:\Pandrator"
PIXI_EXE = os.path.join(BASE_DIR, "bin", "pixi.exe")


def get_backend_env():
    """Build the environment dictionary referencing E:/Pandrator cache and pixi paths."""
    env = os.environ.copy()
    pixi_home = os.path.join(BASE_DIR, '.pixi-home')
    pixi_cache = os.path.join(BASE_DIR, '.pixi-cache')
    rattler_cache = os.path.join(pixi_cache, 'rattler')
    pip_cache = os.path.join(pixi_cache, 'pip')
    local_temp = os.path.join(pixi_cache, 'tmp')

    os.makedirs(pixi_home, exist_ok=True)
    os.makedirs(pixi_cache, exist_ok=True)
    os.makedirs(rattler_cache, exist_ok=True)
    os.makedirs(pip_cache, exist_ok=True)
    os.makedirs(local_temp, exist_ok=True)

    env['PIXI_HOME'] = pixi_home
    env['PIXI_CACHE_DIR'] = pixi_cache
    env['RATTLER_CACHE_DIR'] = rattler_cache
    env['PIP_CACHE_DIR'] = pip_cache
    env['TMP'] = local_temp
    env['TEMP'] = local_temp
    env['TMPDIR'] = local_temp

    local_cache_root = os.path.join(BASE_DIR, 'cache')
    env['XDG_CACHE_HOME'] = local_cache_root
    env['HF_HOME'] = os.path.join(local_cache_root, 'huggingface')
    env['HF_HUB_CACHE'] = os.path.join(local_cache_root, 'huggingface', 'hub')
    env['HUGGINGFACE_HUB_CACHE'] = os.path.join(local_cache_root, 'huggingface', 'hub')
    env['TRANSFORMERS_CACHE'] = os.path.join(local_cache_root, 'huggingface', 'transformers')
    env['TORCH_HOME'] = os.path.join(local_cache_root, 'torch')
    env['TTS_HOME'] = os.path.join(local_cache_root, 'tts')

    return env


def resolve_espeak_paths():
    """Resolve local path to espeak-ng."""
    candidate_roots = [
        os.environ.get('ProgramFiles', r'C:\Program Files'),
        os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
    ]
    for root in candidate_roots:
        if not root:
            continue
        dll_path = os.path.join(root, 'eSpeak NG', 'libespeak-ng.dll')
        data_path = os.path.join(root, 'eSpeak NG', 'espeak-ng-data')
        if os.path.exists(dll_path):
            return dll_path, data_path if os.path.exists(data_path) else ''
    return '', ''


def kill_process_tree(proc):
    """Recursively kill a process and all of its children."""
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

    gone, alive = psutil.wait_procs(children + [parent], timeout=5)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass


def kill_processes_on_port(port):
    """Scan and kill any process currently listening on a specific port."""
    print(f"Checking for processes on port {port}...")
    try:
        connections = psutil.net_connections()
    except Exception as e:
        print(f"Could not retrieve connections: {e}")
        return

    seen_pids = set()
    for conn in connections:
        if conn.laddr and conn.laddr.port == port:
            if conn.pid and conn.pid not in seen_pids and conn.pid != 0:
                seen_pids.add(conn.pid)
                print(f"Port {port} is occupied by PID {conn.pid}. Terminating...")
                try:
                    p = psutil.Process(conn.pid)
                    kill_process_tree(p)
                except Exception as e:
                    print(f"Failed to terminate process {conn.pid}: {e}")


def start_backend(service):
    """Start the server process for the specified backend."""
    env = get_backend_env()
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

    if service in ["XTTS", "VoxCPM", "FishS2"]:
        port = 8020
    elif service == "Chatterbox":
        port = 8040
    elif service == "Voxtral":
        port = 8000
    elif service == "Kokoro":
        port = 8880
    else:
        print(f"Error: Unknown service {service}")
        return None

    # Force kill any process on the target port
    kill_processes_on_port(port)

    print(f"Starting {service} server...")
    process = None

    if service == "XTTS":
        cwd = os.path.join(BASE_DIR, "xtts2_api")
        cmd = ["cmd", "/c", "run.bat", "--backend", "cuda", "--pixi-path", PIXI_EXE]
        env['XTTS_USE_DEEPSPEED'] = 'false'
        log_file = open(os.path.join(cwd, "xtts_benchmark.log"), "w", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
        process.log_file = log_file

    elif service == "VoxCPM":
        cwd = os.path.join(BASE_DIR, "voxcpm_fastapi")
        cmd = ["cmd", "/c", "run.bat", "--pixi-path", PIXI_EXE]
        log_file = open(os.path.join(cwd, "voxcpm_benchmark.log"), "w", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
        process.log_file = log_file

    elif service == "FishS2":
        cwd = os.path.join(BASE_DIR, "fishs2-cpp-fastapi")
        cmd = ["cmd", "/c", "run.bat", "--pixi-path", PIXI_EXE]
        log_file = open(os.path.join(cwd, "fishs2_benchmark.log"), "w", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
        process.log_file = log_file

    elif service == "Chatterbox":
        cwd = os.path.join(BASE_DIR, "chatterbox-fastapi")
        cmd = ["cmd", "/c", "run.bat", "--pixi-path", PIXI_EXE]
        log_file = open(os.path.join(cwd, "chatterbox_benchmark.log"), "w", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
        process.log_file = log_file

    elif service == "Voxtral":
        cwd = os.path.join(BASE_DIR, "voxtral-fastapi")
        cmd = [
            'powershell',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            os.path.join(cwd, 'run.ps1'),
            '-ProjectRoot',
            cwd,
            '-BindHost',
            '127.0.0.1',
            '-Port',
            '8000',
            '-Model',
            'gguf',
        ]
        log_file = open(os.path.join(cwd, "voxtral_benchmark.log"), "w", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
        process.log_file = log_file

    elif service == "Kokoro":
        cwd = os.path.join(BASE_DIR, "Kokoro-FastAPI")
        manifest_path = os.path.join(BASE_DIR, "envs", "kokoro_api_server_installer", "pixi.toml")
        cmd = [
            PIXI_EXE,
            'run',
            '--manifest-path',
            manifest_path,
            '--executable',
            'python',
            '-m',
            'uvicorn',
            'api.src.main:app',
            '--host',
            '127.0.0.1',
            '--port',
            '8880',
        ]
        env['PYTHONUTF8'] = '1'
        env['USE_GPU'] = 'true'
        env['USE_ONNX'] = 'false'
        env['MODEL_DIR'] = 'src/models'
        env['VOICES_DIR'] = 'src/voices/v1_0'
        env['WEB_PLAYER_PATH'] = os.path.join(cwd, 'web')
        env['PYTHONPATH'] = f"{cwd};{os.path.join(cwd, 'api')}"

        dll_path, data_path = resolve_espeak_paths()
        if dll_path:
            env['PHONEMIZER_ESPEAK_LIBRARY'] = dll_path
        if data_path:
            env['PHONEMIZER_ESPEAK_DATA'] = data_path
            env['ESPEAK_DATA_PATH'] = data_path

        log_file = open(os.path.join(cwd, "kokoro_benchmark.log"), "w", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
        process.log_file = log_file

    return process


def check_connection(service):
    """Check connection for the specified service."""
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


def wait_until_online(service, process, timeout=120):
    """Poll the server health check until online."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if process.poll() is not None:
            print(f"Error: {service} server process exited early with code {process.poll()}")
            return False

        if check_connection(service):
            print(f"{service} server is online.")
            return True

        time.sleep(2)
    print(f"Timeout: {service} server failed to come online within {timeout}s.")
    return False


def stop_backend(service, process):
    """Stop the specified backend server process and clean up the port."""
    print(f"Stopping {service} server...")
    if process:
        try:
            kill_process_tree(process)
        except Exception as e:
            print(f"Error killing process tree: {e}")

        if hasattr(process, "log_file") and process.log_file:
            try:
                process.log_file.close()
            except Exception:
                pass

    if service in ["XTTS", "VoxCPM", "FishS2"]:
        port = 8020
    elif service == "Chatterbox":
        port = 8040
    elif service == "Voxtral":
        port = 8000
    elif service == "Kokoro":
        port = 8880
    else:
        return

    kill_processes_on_port(port)


def main():
    print("TTS Benchmark Tool starting...")
    if not os.path.exists(VOICE_FILE_PATH):
        print(f"Error: Reference voice file not found at '{VOICE_FILE_PATH}'")
        sys.exit(1)

    print(f"Using reference voice: {VOICE_FILE_PATH}")

    # Benchmark services sequentially
    services = ["Kokoro", "Voxtral", "Chatterbox", "FishS2", "VoxCPM", "XTTS"]

    for service in services:
        print(f"\n========================================")
        print(f"Benchmarking service: {service}")
        print(f"========================================")

        process = None
        try:
            process = start_backend(service)
            if not process:
                print(f"Failed to start {service} server process.")
                continue

            # Give Chatterbox more time to sync/install if it's the first run
            timeout = 300 if service == "Chatterbox" else 120
            if not wait_until_online(service, process, timeout=timeout):
                print(f"Could not connect to {service} server. Skipping.")
                continue

            # Determine settings, model, voice, and chunk size
            if service == "XTTS":
                max_len = 200
                model_name = "tts_models/multilingual/multi-dataset/xtts_v2"
                print("Uploading reference voice to XTTS...")
                voice_id = tts_handler.upload_speaker_voice(
                    VOICE_FILE_PATH,
                    base_url=tts_handler.XTTS_API_BASE_URL,
                    service="XTTS",
                    voice_id="sample_male_new"
                )
                print(f"XTTS reference voice uploaded successfully. Voice ID: {voice_id}")
                voice_label = "sample_male_new"
            elif service == "VoxCPM":
                max_len = 300
                model_name = "openbmb/VoxCPM2"
                print("Uploading reference voice to VoxCPM...")
                voice_id = tts_handler.upload_speaker_voice(
                    VOICE_FILE_PATH,
                    base_url=tts_handler.VOXCPM_API_BASE_URL,
                    service="VoxCPM",
                    voice_id="sample_male_new",
                    prompt_text=REFERENCE_TRANSCRIPT
                )
                print(f"VoxCPM reference voice uploaded. Voice ID: {voice_id}")
                voice_label = "sample_male_new"
            elif service == "FishS2":
                max_len = 350
                model_name = "fishaudio/s2-pro"
                print("Uploading reference voice to FishS2...")
                voice_id = tts_handler.upload_speaker_voice(
                    VOICE_FILE_PATH,
                    base_url=tts_handler.FISHS2_API_BASE_URL,
                    service="FishS2",
                    voice_id="sample_male_new",
                    prompt_text=REFERENCE_TRANSCRIPT
                )
                print(f"FishS2 reference voice uploaded. Voice ID: {voice_id}")
                voice_label = "sample_male_new"
            elif service == "Chatterbox":
                max_len = 350
                model_name = "chatterbox-turbo"
                print("Uploading reference voice to Chatterbox...")
                voice_id = tts_handler.upload_speaker_voice(
                    VOICE_FILE_PATH,
                    base_url=tts_handler.CHATTERBOX_API_BASE_URL,
                    service="Chatterbox",
                    voice_id="sample_male_new",
                    prompt_text=REFERENCE_TRANSCRIPT
                )
                print(f"Chatterbox reference voice uploaded. Voice ID: {voice_id}")
                voice_label = "sample_male_new"
            elif service == "Voxtral":
                max_len = 300
                model_name = "auto"
                voice_id = "neutral_male"
                voice_label = "neutral_male"
            elif service == "Kokoro":
                max_len = 350
                model_name = "kokoro"
                voice_id = "am_adam"
                voice_label = "am_adam"

            print(f"Configuring text preprocessing with max sentence length = {max_len}")
            pre_settings = {
                "max_sentence_length": max_len,
                "enable_sentence_splitting": True,
                "enable_sentence_appending": True,
                "remove_diacritics": False,
                "remove_quotation_marks": False,
                "disable_paragraph_detection": False,
                "language": "en",
                "tts_service": service
            }

            sentences = text_preprocessor.preprocess_text(BENCHMARK_TEXT, pre_settings)
            print(f"Text preprocessed successfully into {len(sentences)} sentences.")

            # Build synthesis tts_settings dict
            tts_settings = {
                "service": service,
                "speed": 1.0,
                "language": "en",
                "speaker": voice_id,
                "xtts_model": model_name,
                # VoxCPM options
                "voxcpm_cfg_value": 1.5,
                "voxcpm_inference_timesteps": 15,
                "voxcpm_normalize": False,
                "voxcpm_denoise": False,
                "voxcpm_retry_badcase": True,
                "voxcpm_retry_badcase_max_times": 3,
                "voxcpm_retry_badcase_ratio_threshold": 6.0,
                "voxcpm_min_len": 2,
                "voxcpm_max_len": 4096,
                # FishS2 options
                "fishs2_temperature": 0.7,
                "fishs2_top_p": 0.7,
                "fishs2_chunk_length": max_len,
                "fishs2_latency": "balanced",
                "fishs2_normalize": True,
                "fishs2_prosody_volume": 0.0,
                "fishs2_normalize_loudness": True,
                # Voxtral options
                "voxtral_max_frames": 1024,
                "voxtral_euler_steps": 8,
                "voxtral_chunk": False,
                "voxtral_max_chunk_chars": 500,
                "voxtral_chunk_silence_ms": 0,
                "voxtral_strip_quotes": False,
                "voxtral_strip_diacritics": False,
                "voxtral_level_audio": False,
                # Chatterbox options
                "chatterbox_temperature": 0.8,
                "chatterbox_repetition_penalty": 1.2,
                "chatterbox_min_p": 0.05,
                "chatterbox_top_p": 0.95,
                "chatterbox_top_k": 1000,
                "chatterbox_exaggeration": 0.5,
                "chatterbox_cfg_weight": 0.5,
                "chatterbox_norm_loudness": True,
            }

            audio_segments = []
            synthesis_times = []

            synthesis_start = time.time()
            for idx, sentence_dict in enumerate(sentences, 1):
                text = sentence_dict["original_sentence"]
                print(f"Generating sentence {idx}/{len(sentences)} ({len(text)} chars)...")

                sent_start = time.time()
                audio_data = tts_handler.text_to_audio(
                    text,
                    tts_settings,
                    max_attempts=3
                )
                sent_elapsed = time.time() - sent_start

                if not audio_data:
                    print(f"Warning: Failed to generate audio for sentence {idx}. Skipping.")
                    continue

                synthesis_times.append(sent_elapsed)

                # Apply fade
                audio_data = audio_processor.apply_fade(audio_data, 75, 75)

                # Add silence
                silence_to_add = 1000 if sentence_dict.get("paragraph", "no") == "yes" else 300
                audio_data += AudioSegment.silent(duration=silence_to_add)

                audio_segments.append(audio_data)
                print(f"  Sentence {idx} generated in {sent_elapsed:.2f}s (Audio len: {len(audio_data)/1000:.2f}s)")

            synthesis_total_elapsed = time.time() - synthesis_start

            if not audio_segments:
                print(f"Error: No audio segments generated for {service}.")
                continue

            print(f"Stitching audio segments...")
            final_audio = AudioSegment.empty()
            for segment in audio_segments:
                final_audio += segment

            timestamp = datetime.now().strftime("%H_%M_%S")
            filename = f"{service.lower()}_{voice_label}_{timestamp}.wav"
            output_path = os.path.join(OUTPUT_DIR, filename)

            print(f"Saving final audio to {output_path}...")
            final_audio.export(output_path, format="wav")
            print(f"Benchmark completed for {service}!")
            print(f"  Total synthesis time: {synthesis_total_elapsed:.2f}s")
            if synthesis_times:
                print(f"  Average time per sentence: {sum(synthesis_times)/len(synthesis_times):.2f}s")
            print(f"  Total audio duration: {len(final_audio)/1000:.2f}s")
            if len(final_audio) > 0:
                print(f"  Real-time factor (RTF): {synthesis_total_elapsed / (len(final_audio)/1000):.4f}")
        except Exception as e:
            print(f"An error occurred while benchmarking {service}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if process:
                stop_backend(service, process)
            else:
                # Fallback to make sure port is clean anyway
                if service in ["XTTS", "VoxCPM", "FishS2"]:
                    kill_processes_on_port(8020)
                elif service == "Chatterbox":
                    kill_processes_on_port(8040)
                elif service == "Voxtral":
                    kill_processes_on_port(8000)
                elif service == "Kokoro":
                    kill_processes_on_port(8880)

            print("Waiting 5 seconds for VRAM and resources to settle...")
            time.sleep(5)

    print("\nBenchmark run completed successfully!")


if __name__ == "__main__":
    main()
