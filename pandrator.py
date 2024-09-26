import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
import re
import json
import threading
import requests
import logging
import time
import datetime
from pydub import AudioSegment
import io
import os
import subprocess
from unidecode import unidecode
import unicodedata
import tempfile
import difflib
from sentence_splitter import SentenceSplitter
import pygame
import shutil
from CTkMessagebox import CTkMessagebox
import ctypes
import math
import platform
from CTkToolTip import CTkToolTip
import pysrt
from num2words import num2words
import ffmpeg
from pdftextract import XPdf
import regex
import hasami
import argparse

# Conditional imports for torch and RVC
try:
    import torch
    torch_available = True
except ImportError:
    torch_available = False

try:
    from rvc_python.infer import RVCInference
    rvc_available = True
except ImportError:
    rvc_available = False

rvc_functionality_available = torch_available and rvc_available

silero_languages = [
    {"name": "German (v3)", "code": "v3_de.pt"},
    {"name": "English (v3)", "code": "v3_en.pt"},
    {"name": "English Indic (v3)", "code": "v3_en_indic.pt"},
    {"name": "Spanish (v3)", "code": "v3_es.pt"},
    {"name": "French (v3)", "code": "v3_fr.pt"},
    {"name": "Indic (v3)", "code": "v3_indic.pt"},
    {"name": "Russian (v3.1)", "code": "v3_1_ru.pt"},
    {"name": "Tatar (v3)", "code": "v3_tt.pt"},
    {"name": "Ukrainian (v3)", "code": "v3_ua.pt"},
    {"name": "Uzbek (v3)", "code": "v3_uz.pt"},
    {"name": "Kalmyk (v3)", "code": "v3_xal.pt"}
]

class TTSOptimizerGUI:
    def __init__(self, master):
        self.master = master
        master.title("Pandrator")
        self.timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        # Set up logging
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(logs_dir, exist_ok=True)
        self.log_file_path = os.path.join(logs_dir, f"pandrator_{self.timestamp}.log")
        
        logger = logging.getLogger()  # Get the root logger
        logger.setLevel(logging.DEBUG)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

        file_handler = logging.FileHandler(self.log_file_path, mode='w')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
        # Log the absolute path of the log file
        logging.info(f"Log file created at: {self.log_file_path}")
        self.channel = None
        self.playlist_index = None
        self.previous_tts_service = None
        self.enable_tts_evaluation = ctk.BooleanVar(value=False)
        self.stop_flag = False
        self.pdf_preprocessed = False
        self.delete_session_flag = False
        self.pre_selected_source_file = None
        self.external_server_connected = False
        self.use_external_server_voicecraft = ctk.BooleanVar(value=False)
        self.external_server_url_voicecraft = ctk.StringVar()
        self.external_server_url = ctk.StringVar()
        self.use_external_server = ctk.BooleanVar(value=False)
        self.external_server_address = ctk.StringVar()
        self.external_server_address.trace_add("write", self.populate_speaker_dropdown)
        self.enable_dubbing = ctk.BooleanVar(value=False)
        self.server_connected = False
        self.external_server_connected_voicecraft = False
        self.remove_double_newlines = ctk.BooleanVar(value=False)
        self.advanced_settings_switch = None
        self.tts_voices_folder = "tts_voices"
        if not os.path.exists(self.tts_voices_folder):
            os.makedirs(self.tts_voices_folder)
        self.unload_model_after_sentence = ctk.BooleanVar(value=False)
        self.source_file = ""
        self.first_optimisation_prompt = ctk.StringVar(value="Your task is to spell out abbreviations and titles and convert Roman numerals to English words in the sentence(s) you are given. For example: Prof. to Professor, Dr. to Doctor, et. al. to et alia, etc. to et cetera, Section III to Section Three, Chapter V to Chapter Five and so on. Don't change ANYTHING ELSE and output ONLY the complete processed text. If no adjustments are necessary, just output the sentence(s) without changing or appending ANYTHING. Include ABSOLUTELY NO comments, NO acknowledgments, NO explanations, NO notes and so on. This is your text: ")
        self.second_optimisation_prompt = ctk.StringVar(value="Your task is to analyze a text fragment carefully and correct punctuation. Also, correct any misspelled words and possible OCR artifacts based on context. If there is a number that looks out of place because it could have been a page number captured by OCR and doesn't fit in the context, remove it. Don't change ANYTHING ELSE and output ONLY the complete processed text (even if no changes were made). No comments, acknowledgments, explanations or notes. This is your text: ")
        self.third_optimisation_prompt = ctk.StringVar(value="Your task is to spell difficult FOREIGN, NON-ENGLISH words phonetically. Don't alter ANYTHING ELSE in the text - English words remain the same. Don't do anything else, don't add anything, don't include any comments, explanations, notes or acknowledgments. Example: Jiyu means freedom in Japanese becomes jeeyou means freedom in Japanese - jiyu is spelled phonetically as a Japanese word, the rest is not changed. This is your text: ")
        self.enable_first_evaluation = ctk.BooleanVar(value=False)
        self.enable_second_evaluation = ctk.BooleanVar(value=False)
        self.enable_third_evaluation = ctk.BooleanVar(value=False)
        self.enable_first_prompt = ctk.BooleanVar(value=True)
        self.enable_second_prompt = ctk.BooleanVar(value=False)
        self.enable_third_prompt = ctk.BooleanVar(value=False)
        self.silence_length = ctk.IntVar(value=750)
        self.paragraph_silence_length = ctk.IntVar(value=2000)
        self.output_format = ctk.StringVar(value="opus")
        self.bitrate = ctk.StringVar(value="64k")
        self.first_prompt_model = ctk.StringVar(value="default")
        self.second_prompt_model = ctk.StringVar(value="default")
        self.third_prompt_model = ctk.StringVar(value="default")
        self.loaded_model = None
        self.enable_sentence_splitting = ctk.BooleanVar(value=True)
        self.max_sentence_length = ctk.IntVar(value=160)
        self.enable_sentence_appending = ctk.BooleanVar(value=True)
        self.remove_diacritics = ctk.BooleanVar(value=False)
        self.enable_fade = ctk.BooleanVar(value=True)
        self.fade_in_duration = ctk.IntVar(value=75)
        self.fade_out_duration = ctk.IntVar(value=75)
        self.enable_rvc = ctk.BooleanVar(value=False)
        self.enable_llm_processing = ctk.BooleanVar(value=False)
        self.enable_first_prompt = ctk.BooleanVar(value=self.enable_llm_processing.get())
        self.playlist_stopped = False
        self.target_mos_value = ctk.StringVar(value="2.9")
        self.max_attempts = ctk.IntVar(value=5)
        self.paused = False
        self.playing = False
        self.session_name = ctk.StringVar()
        self.tts_service = ctk.StringVar(value="XTTS")
        self.mark_paragraphs_multiple_newlines = ctk.BooleanVar(value=False)
        self.xtts_temperature = ctk.DoubleVar(value=0.75)
        self.xtts_length_penalty = ctk.DoubleVar(value=1.0)
        self.xtts_repetition_penalty = ctk.DoubleVar(value=5.0)
        self.xtts_top_k = ctk.IntVar(value=50)
        self.xtts_top_p = ctk.DoubleVar(value=0.85)
        self.xtts_speed = ctk.DoubleVar(value=1.0)
        self.xtts_enable_text_splitting = ctk.BooleanVar(value=True)
        self.xtts_stream_chunk_size = ctk.IntVar(value=100)
        self.enable_translation = ctk.BooleanVar(value=False)
        self.original_language = ctk.StringVar(value="en")
        self.target_language = ctk.StringVar(value="en")
        self.enable_translation_evaluation = ctk.BooleanVar(value=False)
        self.enable_glossary = ctk.BooleanVar(value=False)
        self.translation_model = ctk.StringVar(value="sonnet")
        self.anthropic_api_key = ctk.StringVar()
        self.openai_api_key = ctk.StringVar()
        self.deepl_api_key = ctk.StringVar()
        self.selected_video_file = ctk.StringVar()
        self.video_file_selection_label = None
        self.enable_rvc = ctk.BooleanVar(value=False)
        self.whisperx_language = ctk.StringVar(value="en")
        self.whisperx_model = ctk.StringVar(value="small")
        
        if rvc_functionality_available:
            self.rvc_inference = RVCInference(device="cuda:0" if torch.cuda.is_available() else "cpu")
            current_dir = os.path.dirname(os.path.abspath(__file__))
            self.rvc_models_dir = os.path.join(current_dir, "rvc_models")
            os.makedirs(self.rvc_models_dir, exist_ok=True)
            self.rvc_inference.set_models_dir(self.rvc_models_dir)
            self.rvc_models = []  # Initialize as an empty list
            self.refresh_rvc_models()  # This will populate self.rvc_models
        else:
            self.rvc_inference = None
            self.rvc_models_dir = None
            self.rvc_models = []


        # Layout
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        # Get the screen resolution
        screen_width = self.master.winfo_screenwidth()
        screen_height = self.master.winfo_screenheight()
        frame_height = int(screen_height * 0.90)

        # Create the main scrollable frame with the calculated height
        self.main_scrollable_frame = ctk.CTkScrollableFrame(master, width=750, height=frame_height)
        self.main_scrollable_frame.grid(row=0, column=0, padx=10, pady=10, sticky=tk.NSEW)
        self.main_scrollable_frame.grid_columnconfigure(0, weight=1)
        self.main_scrollable_frame.grid_rowconfigure(0, weight=1)

        # Tabs
        self.tabview = ctk.CTkTabview(self.main_scrollable_frame)
        self.tabview.grid(row=0, column=0, padx=10, pady=10, sticky=tk.NSEW)

        # Session Tab
        self.session_tab = self.tabview.add("Session")
        self.session_tab.grid_columnconfigure(0, weight=1, uniform="session_columns")
        self.session_tab.grid_columnconfigure(1, weight=1, uniform="session_columns")
        self.session_tab.grid_columnconfigure(2, weight=1, uniform="session_columns")
        self.session_tab.grid_columnconfigure(3, weight=1, uniform="session_columns")
        self.session_name_label = ctk.CTkLabel(self.session_tab, text="Untitled Session", font=ctk.CTkFont(size=20, weight="bold"))
        self.session_name_label.grid(row=0, column=0, columnspan=4, padx=5, pady=5, sticky=tk.W)

        # Session Section
        self.session_label = ctk.CTkLabel(self.session_tab, text="Session", font=ctk.CTkFont(size=14, weight="bold"))
        self.session_label.grid(row=1, column=0, columnspan=4, padx=10, pady=10, sticky=tk.W)

        session_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        session_frame.grid(row=2, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        session_frame.grid_columnconfigure(0, weight=1)
        session_frame.grid_columnconfigure(1, weight=1)
        session_frame.grid_columnconfigure(2, weight=1)
        session_frame.grid_columnconfigure(3, weight=1)

        ctk.CTkButton(session_frame, text="New Session", command=self.new_session, fg_color="#2e8b57", hover_color="#3cb371").grid(row=0, column=0, padx=10, pady=(10, 10), sticky=tk.EW)
        ctk.CTkButton(session_frame, text="Load Session", command=self.load_session).grid(row=0, column=1, padx=10, pady=(10, 10), sticky=tk.EW)
        ctk.CTkButton(session_frame, text="Delete Session", command=self.delete_session, fg_color="dark red", hover_color="red").grid(row=0, column=3, padx=10, pady=(10, 10), sticky=tk.EW)
        ctk.CTkButton(session_frame, text="View Session Folder", command=self.view_session_folder).grid(row=0, column=2, padx=10, pady=(10, 10), sticky=tk.EW)

        # Session Settings Section
        ctk.CTkLabel(self.session_tab, text="TTS Settings", font=ctk.CTkFont(size=14, weight="bold")).grid(row=3, column=0, columnspan=4, padx=10, pady=10, sticky=tk.W)

        session_settings_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        session_settings_frame.grid(row=4, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        session_settings_frame.grid_columnconfigure(0, weight=1)
        session_settings_frame.grid_columnconfigure(1, weight=1)
        session_settings_frame.grid_columnconfigure(2, weight=1)
        session_settings_frame.grid_columnconfigure(3, weight=1)

        self.selected_file_label = ctk.CTkLabel(session_settings_frame, text="No file selected")
        self.select_file_button = ctk.CTkButton(session_settings_frame, text="Select Source File", command=self.select_file)
        self.select_file_button.grid(row=0, column=0, padx=10, pady=(10, 5), sticky=tk.EW)
        self.selected_file_label.grid(row=0, column=1, columnspan=3, padx=10, pady=(10, 5), sticky=tk.W)

        self.paste_text_button = ctk.CTkButton(session_settings_frame, text="Paste or Write Text", command=self.paste_text)
        self.paste_text_button.grid(row=0, column=1, padx=10, pady=(10, 5), sticky=tk.EW)
        self.selected_file_label.grid(row=0, column=2, columnspan=2, padx=10, pady=(10, 5), sticky=tk.W)

        ctk.CTkLabel(session_settings_frame, text="TTS Service:").grid(row=2, column=0, padx=10, pady=5, sticky=tk.W)
        self.tts_service_dropdown = ctk.CTkOptionMenu(session_settings_frame, variable=self.tts_service, values=["XTTS", "VoiceCraft", "Silero"], command=self.update_tts_service)
        self.tts_service_dropdown.grid(row=2, column=1, padx=10, pady=5, sticky=tk.EW)

        self.voicecraft_model = ctk.StringVar(value="330M_TTSEnhanced")
        self.voicecraft_model_label = ctk.CTkLabel(session_settings_frame, text="VoiceCraft Model:")
        self.voicecraft_model_label.grid(row=3, column=0, padx=10, pady=5, sticky=tk.W)
        self.voicecraft_model_dropdown = ctk.CTkOptionMenu(session_settings_frame, variable=self.voicecraft_model, values=["830M_TTSEnhanced", "330M_TTSEnhanced"])
        self.voicecraft_model_dropdown.grid(row=3, column=1, padx=10, pady=5, sticky=tk.EW)
        self.voicecraft_model_label.grid_remove()  # Hide the VoiceCraft model label initially
        self.voicecraft_model_dropdown.grid_remove()  # Hide the VoiceCraft model dropdown initially
        
        self.xtts_model = ctk.StringVar(value="")
        self.xtts_model_label = ctk.CTkLabel(session_settings_frame, text="XTTS Model:")
        self.xtts_model_label.grid(row=3, column=0, padx=10, pady=5, sticky=tk.W)
        self.xtts_model_dropdown = ctk.CTkOptionMenu(session_settings_frame, variable=self.xtts_model, values=[], command=self.on_xtts_model_change)
        self.xtts_model_dropdown.grid(row=3, column=1, padx=10, pady=5, sticky=tk.EW)

        self.connect_to_server_button = ctk.CTkButton(session_settings_frame, text="Connect to Server", command=self.connect_to_server)
        self.connect_to_server_button.grid(row=2, column=2, columnspan=2, padx=10, pady=5, sticky=tk.EW)

        self.use_external_server_switch = ctk.CTkSwitch(session_settings_frame, text="Use an external server", variable=self.use_external_server, command=self.toggle_external_server)
        self.use_external_server_switch.grid(row=4, column=0, padx=10, pady=5, sticky=tk.W)
        self.external_server_url_entry = ctk.CTkEntry(session_settings_frame, textvariable=self.external_server_url)
        self.external_server_url_entry.grid(row=4, column=1, columnspan=3, padx=10, pady=5, sticky=tk.EW)
        self.external_server_url_entry.grid_remove()  # Hide the entry field initially

        self.use_external_server_voicecraft_switch = ctk.CTkSwitch(session_settings_frame, text="Use an external server", variable=self.use_external_server_voicecraft, command=self.toggle_external_server)
        self.use_external_server_voicecraft_switch.grid(row=5, column=0, padx=10, pady=5, sticky=tk.W)
        self.use_external_server_voicecraft_switch.grid_remove()  # Hide the switch initially
        self.external_server_url_entry_voicecraft = ctk.CTkEntry(session_settings_frame, textvariable=self.external_server_url_voicecraft)
        self.external_server_url_entry_voicecraft.grid(row=5, column=1, columnspan=3, padx=10, pady=5, sticky=tk.EW)
        self.external_server_url_entry_voicecraft.grid_remove()  # Hide the entry field initially

        self.language_var = ctk.StringVar(value="en")
        ctk.CTkLabel(session_settings_frame, text="Language:").grid(row=6, column=0, padx=10, pady=5, sticky=tk.W)
        self.language_dropdown = ctk.CTkComboBox(
            session_settings_frame,
            variable=self.language_var,
            values=["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"]
        )
        self.language_dropdown.grid(row=6, column=1, padx=10, pady=5, sticky=tk.EW)

        self.language_var.trace_add("write", self.on_language_selected)

        self.selected_speaker = ctk.StringVar(value="")
        ctk.CTkLabel(session_settings_frame, text="Speaker Voice:").grid(row=7, column=0, padx=10, pady=5, sticky=tk.W)
        self.speaker_dropdown = ctk.CTkOptionMenu(session_settings_frame, variable=self.selected_speaker, values=[])
        self.speaker_dropdown.grid(row=7, column=1, padx=10, pady=5, sticky=tk.EW)

        self.upload_new_voices_button = ctk.CTkButton(session_settings_frame, text="Upload New Voices", command=self.upload_speaker_voice)
        self.upload_new_voices_button.grid(row=7, column=2, padx=10, pady=(10, 10), sticky=tk.EW)
        self.sample_length = ctk.StringVar(value="3")
        self.sample_length_dropdown = ctk.CTkOptionMenu(session_settings_frame, variable=self.sample_length, values=[str(i) for i in range(3, 13)])
        self.sample_length_dropdown.grid(row=7, column=3, padx=10, pady=5, sticky=tk.EW)
        self.sample_length_dropdown.grid_remove()  # Hide the dropdown initially

        #ctk.CTkLabel(session_settings_frame, text="Playback Speed:").grid(row=8, column=0, padx=10, pady=5, sticky=tk.W)
        #self.playback_speed = ctk.DoubleVar(value=1.0)

        # Create a list of values for the dropdown menu
        #values = [str(value) for value in [0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.35, 1.4, 1.45, 1.5]]

        #self.playback_speed_dropdown = ctk.CTkComboBox(session_settings_frame, values=values, variable=self.playback_speed)
        #self.playback_speed_dropdown.grid(row=8, column=1, columnspan=3, padx=10, pady=5, sticky=tk.EW)
        # Speed Slider
        ctk.CTkLabel(session_settings_frame, text="Speed:").grid(row=8, column=0, padx=10, pady=5, sticky=tk.W)
        speed_slider = ctk.CTkSlider(session_settings_frame, from_=0.2, to=2.0, number_of_steps=180, variable=self.xtts_speed)
        speed_slider.grid(row=8, column=1, columnspan=2, padx=10, pady=5, sticky=tk.EW)

        # Add a label to display the current speed value
        self.speed_value_label = ctk.CTkLabel(session_settings_frame, text=f"Speed: {self.xtts_speed.get():.2f}")
        self.speed_value_label.grid(row=8, column=3, padx=10, pady=5, sticky=tk.W)

        # Update the speed value label when the slider changes
        speed_slider.configure(command=self.update_speed_label)
        self.show_advanced_tts_settings = ctk.BooleanVar(value=False)
        self.advanced_settings_switch = ctk.CTkSwitch(session_settings_frame, text="Advanced TTS Settings", variable=self.show_advanced_tts_settings, command=self.toggle_advanced_tts_settings)
        self.advanced_settings_switch.grid(row=9, column=0, padx=5, pady=5, sticky=tk.W)

        # Advanced TTS Settings Frame
        self.advanced_tts_settings_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        self.advanced_tts_settings_frame.grid(row=5, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        self.advanced_tts_settings_frame.grid_columnconfigure(0, weight=1)
        self.advanced_tts_settings_frame.grid_columnconfigure(1, weight=1)
        self.advanced_tts_settings_frame.grid_remove()  # Hide the frame initially

        ctk.CTkLabel(self.advanced_tts_settings_frame, text="Top K:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.top_k = ctk.StringVar(value="0")
        ctk.CTkEntry(self.advanced_tts_settings_frame, textvariable=self.top_k).grid(row=0, column=1, padx=5, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.advanced_tts_settings_frame, text="Top P:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        self.top_p = ctk.StringVar(value="0.9")
        ctk.CTkEntry(self.advanced_tts_settings_frame, textvariable=self.top_p).grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.advanced_tts_settings_frame, text="Temperature:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        self.temperature = ctk.StringVar(value="1.0")
        ctk.CTkEntry(self.advanced_tts_settings_frame, textvariable=self.temperature).grid(row=2, column=1, padx=5, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.advanced_tts_settings_frame, text="Stop Repetition:").grid(row=3, column=0, padx=5, pady=5, sticky=tk.W)
        self.stop_repetition = ctk.StringVar(value="3")
        ctk.CTkEntry(self.advanced_tts_settings_frame, textvariable=self.stop_repetition).grid(row=3, column=1, padx=5, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.advanced_tts_settings_frame, text="KV Cache:").grid(row=4, column=0, padx=5, pady=5, sticky=tk.W)
        self.kvcache = ctk.StringVar(value="1")
        ctk.CTkEntry(self.advanced_tts_settings_frame, textvariable=self.kvcache).grid(row=4, column=1, padx=5, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.advanced_tts_settings_frame, text="Sample Batch Size:").grid(row=5, column=0, padx=5, pady=5, sticky=tk.W)
        self.sample_batch_size = ctk.StringVar(value="1")
        ctk.CTkEntry(self.advanced_tts_settings_frame, textvariable=self.sample_batch_size).grid(row=5, column=1, padx=5, pady=5, sticky=tk.W)

        # Generation Section
        generation_label = ctk.CTkLabel(self.session_tab, text="Generation", font=ctk.CTkFont(size=14, weight="bold"))
        generation_label.grid(row=8, column=0, padx=10, pady=10, sticky=tk.W)

        generation_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        generation_frame.grid(row=9, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        generation_frame.grid_columnconfigure(0, weight=1)
        generation_frame.grid_columnconfigure(1, weight=1)
        generation_frame.grid_columnconfigure(2, weight=1)
        generation_frame.grid_columnconfigure(3, weight=1)

        self.start_generation_button = ctk.CTkButton(generation_frame, text="Start Generation", command=self.start_optimisation_thread, fg_color="#2e8b57", hover_color="#3cb371")
        self.start_generation_button.grid(row=0, column=0, padx=10, pady=(5, 20), sticky=tk.EW)
        ctk.CTkButton(generation_frame, text="Stop Generation", command=self.stop_generation).grid(row=0, column=2, padx=10, pady=(5, 20), sticky=tk.EW)
        ctk.CTkButton(generation_frame, text="Resume Generation", command=self.resume_generation).grid(row=0, column=1, padx=10, pady=(5, 20), sticky=tk.EW)
        ctk.CTkButton(generation_frame, text="Cancel Generation", command=self.cancel_generation, fg_color="dark red", hover_color="red").grid(row=0, column=3, padx=10, pady=(5, 20), sticky=tk.EW)

        ctk.CTkLabel(generation_frame, text="Progress:").grid(row=2, column=0, padx=10, pady=5, sticky=tk.W)
        self.progress_label = ctk.CTkLabel(generation_frame, text="0.00%")
        self.progress_label.grid(row=2, column=1, padx=10, pady=5, sticky=tk.W)
        self.progress_bar = ctk.CTkProgressBar(generation_frame)
        self.progress_bar.grid(row=1, column=0, columnspan=4, padx=10, pady=5, sticky=tk.EW)
        ctk.CTkLabel(generation_frame, text="Estimated Remaining Time:").grid(row=2, column=2, padx=10, pady=(5), sticky=tk.W)
        self.remaining_time_label = ctk.CTkLabel(generation_frame, text="N/A")
        self.remaining_time_label.grid(row=2, column=3, padx=10, pady=(5), sticky=tk.W)

        # Modify the dubbing frame creation
        self.dubbing_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        self.dubbing_frame.grid(row=7, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        self.dubbing_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.dubbing_frame.grid_remove()  # Hide the dubbing frame by default
        ctk.CTkLabel(self.dubbing_frame, text="Dubbing", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, columnspan=4, padx=10, pady=10, sticky=tk.W)

        # Transcription Options Frame
        self.transcription_frame = ctk.CTkFrame(self.dubbing_frame, fg_color="gray20", corner_radius=10)
        self.transcription_frame.grid(row=1, column=0, columnspan=5, padx=10, pady=(10, 5), sticky=tk.EW)
        self.transcription_frame.grid_columnconfigure((0, 1, 2, 3, 4), weight=1)

        ctk.CTkLabel(self.transcription_frame, text="Transcription Options:", font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, columnspan=5, padx=10, pady=(5, 5), sticky=tk.W)

        ctk.CTkLabel(self.transcription_frame, text="Language:").grid(row=1, column=0, padx=10, pady=5, sticky=tk.W)
        self.whisperx_language_dropdown = ctk.CTkOptionMenu(self.transcription_frame, variable=self.whisperx_language, values=["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"])
        self.whisperx_language_dropdown.grid(row=1, column=1, padx=10, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.transcription_frame, text="Model:").grid(row=1, column=2, padx=10, pady=5, sticky=tk.W)
        self.whisperx_model_dropdown = ctk.CTkOptionMenu(self.transcription_frame, variable=self.whisperx_model, values=["small", "small.en", "medium", "medium.en", "large-v2", "large-v3"])
        self.whisperx_model_dropdown.grid(row=1, column=3, padx=10, pady=5, sticky=tk.W)

        # Translation Options Frame
        self.translation_frame = ctk.CTkFrame(self.dubbing_frame, fg_color="gray20", corner_radius=10)
        self.translation_frame.grid(row=2, column=0, columnspan=5, padx=10, pady=(10, 5), sticky=tk.EW)
        self.translation_frame.grid_columnconfigure((0, 1, 2, 3, 4), weight=1)

        ctk.CTkLabel(self.translation_frame, text="Translation Options:", font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, columnspan=5, padx=10, pady=(5, 5), sticky=tk.W)

        self.enable_translation_switch = ctk.CTkSwitch(self.translation_frame, text="Translate subtitles", variable=self.enable_translation)
        self.enable_translation_switch.grid(row=1, column=0, columnspan=2, padx=10, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.translation_frame, text="From:").grid(row=2, column=0, padx=10, pady=5, sticky=tk.W)
        self.original_language_dropdown = ctk.CTkOptionMenu(self.translation_frame, variable=self.original_language, values=["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"])
        self.original_language_dropdown.grid(row=2, column=1, padx=10, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.translation_frame, text="To:").grid(row=2, column=2, padx=10, pady=5, sticky=tk.W)
        self.target_language_dropdown = ctk.CTkOptionMenu(self.translation_frame, variable=self.target_language, values=["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"])
        self.target_language_dropdown.grid(row=2, column=3, padx=10, pady=5, sticky=tk.W)

        self.enable_translation_evaluation_switch = ctk.CTkSwitch(self.translation_frame, text="Enable evaluation", variable=self.enable_translation_evaluation)
        self.enable_translation_evaluation_switch.grid(row=3, column=0, columnspan=2, padx=10, pady=5, sticky=tk.W)

        self.enable_glossary_switch = ctk.CTkSwitch(self.translation_frame, text="Enable glossary", variable=self.enable_glossary)
        self.enable_glossary_switch.grid(row=3, column=2, columnspan=2, padx=10, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.translation_frame, text="Translation Model:").grid(row=4, column=0, padx=10, pady=5, sticky=tk.W)
        self.translation_model_dropdown = ctk.CTkOptionMenu(self.translation_frame, variable=self.translation_model, values=["haiku", "sonnet", "gpt-4o-mini", "gpt-4o", "deepl", "local"], width=150)
        self.translation_model_dropdown.grid(row=4, column=1, padx=10, pady=5, sticky=tk.W)
        self.translation_model.trace_add("write", self.on_translation_model_change)

        # Video File Selection (for SRT input)
        self.video_file_selection_frame = ctk.CTkFrame(self.dubbing_frame, fg_color="gray20", corner_radius=10)
        self.video_file_selection_frame.grid(row=3, column=0, columnspan=5, padx=10, pady=(10, 5), sticky=tk.EW)
        self.video_file_selection_frame.grid_columnconfigure((0, 1, 2), weight=1)
        self.video_file_selection_frame.grid_remove()  # Hide initially

        ctk.CTkLabel(self.video_file_selection_frame, text="Video File:").grid(row=0, column=0, padx=10, pady=5, sticky=tk.W)
        self.selected_video_file_entry = ctk.CTkEntry(self.video_file_selection_frame, textvariable=self.selected_video_file, state="readonly")
        self.selected_video_file_entry.grid(row=0, column=1, padx=10, pady=5, sticky=tk.EW)
        self.select_video_button = ctk.CTkButton(self.video_file_selection_frame, text="Select Video", command=self.select_video_file)
        self.select_video_button.grid(row=0, column=2, padx=10, pady=5, sticky=tk.E)

        # Dubbing Generation Buttons
        self.dubbing_buttons_frame = ctk.CTkFrame(self.dubbing_frame, fg_color="gray20", corner_radius=10)
        self.dubbing_buttons_frame.grid(row=4, column=0, columnspan=5, padx=10, pady=(10, 5), sticky=tk.EW)
        self.dubbing_buttons_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.generate_dubbing_audio_button = ctk.CTkButton(self.dubbing_buttons_frame, text="Generate Dubbing Audio", fg_color="#2e8b57", hover_color="#3cb371", command=self.generate_dubbing_audio)
        self.generate_dubbing_audio_button.grid(row=0, column=0, padx=5, pady=5, sticky=tk.EW)

        self.add_dubbing_to_video_button = ctk.CTkButton(self.dubbing_buttons_frame, text="Add Dubbing to Video", command=self.add_dubbing_to_video)
        self.add_dubbing_to_video_button.grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)

        self.only_transcribe_button = ctk.CTkButton(self.dubbing_buttons_frame, text="Only Transcribe", command=self.only_transcribe)
        self.only_transcribe_button.grid(row=0, column=2, padx=5, pady=5, sticky=tk.EW)

        self.only_translate_button = ctk.CTkButton(self.dubbing_buttons_frame, text="Only Translate", command=self.only_translate)
        self.only_translate_button.grid(row=0, column=3, padx=5, pady=5, sticky=tk.EW)

    
        # Generated Sentences Section
        ctk.CTkLabel(self.session_tab, text="Generated Sentences", font=ctk.CTkFont(size=14, weight="bold")).grid(row=14, column=0, columnspan=4, padx=10, pady=(20, 10), sticky=tk.W)

        generated_sentences_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        generated_sentences_frame.grid(row=15, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        generated_sentences_frame.grid_columnconfigure((0, 1, 2), weight=1)

        # Top buttons
        self.play_button = ctk.CTkButton(generated_sentences_frame, text="Play", command=self.toggle_playback, fg_color="#2e8b57", hover_color="#3cb371")
        self.play_button.grid(row=0, column=0, padx=(10, 5), pady=(10, 5), sticky=tk.EW)

        ctk.CTkButton(generated_sentences_frame, text="Play as Playlist", command=self.play_sentences_as_playlist).grid(row=0, column=1, padx=5, pady=(10, 5), sticky=tk.EW)

        ctk.CTkButton(generated_sentences_frame, text="Stop", command=self.stop_playback).grid(row=0, column=2, padx=(5, 10), pady=(10, 5), sticky=tk.EW)

        # Create a frame to hold the Listbox and Scrollbar
        listbox_frame = ctk.CTkFrame(generated_sentences_frame, fg_color="#444444")
        listbox_frame.grid(row=1, column=0, columnspan=3, padx=10, pady=10, sticky=tk.NSEW)
        listbox_frame.grid_columnconfigure(0, weight=1)
        listbox_frame.grid_rowconfigure(0, weight=1)

        # Create the Listbox
        self.playlist_listbox = tk.Listbox(
            listbox_frame,
            bg="#444444",
            fg="#FFFFFF",
            font=("Helvetica", 9),
            selectbackground="#555555",
            selectforeground="#FFFFFF",
            selectborderwidth=0,
            activestyle="none",
            highlightthickness=0,
            bd=0,
            relief=tk.FLAT,
            height=10,
        )
        self.playlist_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Create the Scrollbar
        scrollbar = ctk.CTkScrollbar(listbox_frame, orientation="vertical", command=self.playlist_listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Configure the Listbox to use the Scrollbar
        self.playlist_listbox.configure(yscrollcommand=scrollbar.set)

        # Bottom buttons
        button_frame = ctk.CTkFrame(generated_sentences_frame, fg_color="transparent")
        button_frame.grid(row=2, column=0, columnspan=3, padx=10, pady=(5, 10), sticky=tk.EW)
        button_frame.grid_columnconfigure((0, 1, 2, 3, 4), weight=1)

        ctk.CTkButton(button_frame, text="Regenerate", command=self.regenerate_selected_sentence).grid(row=0, column=0, padx=(0, 5), pady=5, sticky=tk.EW)
        ctk.CTkButton(button_frame, text="Regenerate All", command=self.regenerate_all_sentences).grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(button_frame, text="Remove", command=self.remove_selected_sentences).grid(row=0, column=2, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(button_frame, text="Edit", command=self.edit_selected_sentence).grid(row=0, column=3, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(button_frame, text="Save Output", command=self.save_output).grid(row=0, column=4, padx=(5, 0), pady=5, sticky=tk.EW)
        # Text Processing Tab
        self.text_processing_tab = self.tabview.add("Text Processing")
        self.text_processing_tab.grid_columnconfigure(0, weight=1)
        self.text_processing_tab.grid_columnconfigure(1, weight=1)

        # General Text Processing Settings
        ctk.CTkLabel(self.text_processing_tab, text="General Text Processing Settings", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, columnspan=2, padx=10, pady=10, sticky=tk.W)

        general_settings_frame = ctk.CTkFrame(self.text_processing_tab, fg_color="gray20", corner_radius=10)
        general_settings_frame.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 20), sticky=tk.EW)
        general_settings_frame.grid_columnconfigure(0, weight=1)
        general_settings_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkSwitch(general_settings_frame, text="Split Long Sentences", variable=self.enable_sentence_splitting).grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(general_settings_frame, text="Max Sentence Length:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(general_settings_frame, textvariable=self.max_sentence_length).grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)
        ctk.CTkSwitch(general_settings_frame, text="Append Short Sentences", variable=self.enable_sentence_appending).grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkSwitch(general_settings_frame, text="Remove Diacritics", variable=self.remove_diacritics).grid(row=3, column=0, padx=5, pady=5, sticky=tk.W)
        self.disable_paragraph_detection = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(general_settings_frame, text="Disable Paragraph Detection", variable=self.disable_paragraph_detection).grid(row=4, column=0, padx=5, pady=5, sticky=tk.W)
        # LLM Processing
        ctk.CTkLabel(self.text_processing_tab, text="LLM Processing", font=ctk.CTkFont(size=14, weight="bold")).grid(row=2, column=0, columnspan=2, padx=10, pady=10, sticky=tk.W)

        llm_processing_frame = ctk.CTkFrame(self.text_processing_tab, fg_color="gray20", corner_radius=10)
        llm_processing_frame.grid(row=3, column=0, columnspan=2, padx=10, pady=(0, 20), sticky=tk.EW)
        llm_processing_frame.grid_columnconfigure(0, weight=1)
        llm_processing_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkSwitch(llm_processing_frame, text="Enable LLM Processing", variable=self.enable_llm_processing).grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkButton(llm_processing_frame, text="Load LLM Models", command=self.load_models).grid(row=0, column=1, padx=10, pady=(10, 10), sticky=tk.EW)
        ctk.CTkSwitch(llm_processing_frame, text="Unload LLM Model After Each Sentence", variable=self.unload_model_after_sentence).grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)

        # First Prompt
        ctk.CTkLabel(self.text_processing_tab, text="First Prompt", font=ctk.CTkFont(size=14, weight="bold")).grid(row=4, column=0, columnspan=2, padx=10, pady=10, sticky=tk.W)

        first_prompt_frame = ctk.CTkFrame(self.text_processing_tab, fg_color="gray20", corner_radius=10)
        first_prompt_frame.grid(row=5, column=0, columnspan=2, padx=10, pady=(0, 20), sticky=tk.EW)
        first_prompt_frame.grid_columnconfigure(0, weight=1)
        first_prompt_frame.grid_columnconfigure(1, weight=1)

        self.first_prompt_text = ctk.CTkTextbox(first_prompt_frame, height=100, width=500, wrap="word")
        self.first_prompt_text.insert("0.0", self.first_optimisation_prompt.get())
        self.first_prompt_text.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkSwitch(first_prompt_frame, text="Enable First Prompt", variable=self.enable_first_prompt).grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkSwitch(first_prompt_frame, text="Enable Evaluation", variable=self.enable_first_evaluation).grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(first_prompt_frame, text="First Prompt Model:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        self.first_prompt_model_dropdown = ctk.CTkOptionMenu(first_prompt_frame, variable=self.first_prompt_model, values=["default"])
        self.first_prompt_model_dropdown.grid(row=2, column=1, padx=5, pady=5, sticky=tk.EW)

        # Second Prompt
        ctk.CTkLabel(self.text_processing_tab, text="Second Prompt", font=ctk.CTkFont(size=14, weight="bold")).grid(row=6, column=0, columnspan=2, padx=10, pady=10, sticky=tk.W)

        second_prompt_frame = ctk.CTkFrame(self.text_processing_tab, fg_color="gray20", corner_radius=10)
        second_prompt_frame.grid(row=7, column=0, columnspan=2, padx=10, pady=(0, 20), sticky=tk.EW)
        second_prompt_frame.grid_columnconfigure(0, weight=1)
        second_prompt_frame.grid_columnconfigure(1, weight=1)

        self.second_prompt_text = ctk.CTkTextbox(second_prompt_frame, height=100, wrap="word")
        self.second_prompt_text.insert("0.0", self.second_optimisation_prompt.get())
        self.second_prompt_text.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkSwitch(second_prompt_frame, text="Enable Second Prompt", variable=self.enable_second_prompt).grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkSwitch(second_prompt_frame, text="Enable Evaluation", variable=self.enable_second_evaluation).grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(second_prompt_frame, text="Second Prompt Model:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        self.second_prompt_model_dropdown = ctk.CTkOptionMenu(second_prompt_frame, variable=self.second_prompt_model, values=["default"])
        self.second_prompt_model_dropdown.grid(row=2, column=1, padx=5, pady=5, sticky=tk.EW)

        # Third Prompt
        ctk.CTkLabel(self.text_processing_tab, text="Third Prompt", font=ctk.CTkFont(size=14, weight="bold")).grid(row=8, column=0, columnspan=2, padx=10, pady=10, sticky=tk.W)

        third_prompt_frame = ctk.CTkFrame(self.text_processing_tab, fg_color="gray20", corner_radius=10)
        third_prompt_frame.grid(row=9, column=0, columnspan=2, padx=10, pady=(0, 20), sticky=tk.EW)
        third_prompt_frame.grid_columnconfigure(0, weight=1)
        third_prompt_frame.grid_columnconfigure(1, weight=1)

        self.third_prompt_text = ctk.CTkTextbox(third_prompt_frame, height=100, wrap="word")
        self.third_prompt_text.insert("0.0", self.third_optimisation_prompt.get())
        self.third_prompt_text.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkSwitch(third_prompt_frame, text="Enable Third Prompt", variable=self.enable_third_prompt).grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkSwitch(third_prompt_frame, text="Enable Evaluation", variable=self.enable_third_evaluation).grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(third_prompt_frame, text="Third Prompt Model:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        self.third_prompt_model_dropdown = ctk.CTkOptionMenu(third_prompt_frame, variable=self.third_prompt_model, values=["default"])
        self.third_prompt_model_dropdown.grid(row=2, column=1, padx=5, pady=5, sticky=tk.EW)

        # Audio Processing Tab
        self.audio_processing_tab = self.tabview.add("Audio Processing")
        self.audio_processing_tab.grid_columnconfigure(0, weight=1)
        self.audio_processing_tab.grid_columnconfigure(1, weight=1)

        # Appended Silence Section
        ctk.CTkLabel(self.audio_processing_tab, text="Appended Silence", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, columnspan=4, padx=10, pady=10, sticky=tk.W)

        silence_frame = ctk.CTkFrame(self.audio_processing_tab, fg_color="gray20", corner_radius=10)
        silence_frame.grid(row=1, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        silence_frame.grid_columnconfigure(0, weight=1)
        silence_frame.grid_columnconfigure(1, weight=1)
        silence_frame.grid_columnconfigure(2, weight=1)
        silence_frame.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(silence_frame, text="Length:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(silence_frame, textvariable=self.silence_length).grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkLabel(silence_frame, text="Length Paragraph:").grid(row=0, column=2, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(silence_frame, textvariable=self.paragraph_silence_length).grid(row=0, column=3, padx=5, pady=5, sticky=tk.EW)

        # RVC Section
        ctk.CTkLabel(self.audio_processing_tab, text="RVC", font=ctk.CTkFont(size=14, weight="bold")).grid(row=2, column=0, columnspan=3, padx=10, pady=10, sticky=tk.W)

        rvc_frame = ctk.CTkFrame(self.audio_processing_tab, fg_color="gray20", corner_radius=10)
        rvc_frame.grid(row=3, column=0, columnspan=3, padx=10, pady=(0, 20), sticky=tk.EW)
        rvc_frame.grid_columnconfigure(0, weight=1)
        rvc_frame.grid_columnconfigure(1, weight=1)
        rvc_frame.grid_columnconfigure(2, weight=1)

        ctk.CTkSwitch(rvc_frame, text="Enable RVC", variable=self.enable_rvc).grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)

        ctk.CTkLabel(rvc_frame, text="RVC Model:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        self.rvc_model_dropdown = ctk.CTkOptionMenu(rvc_frame, values=self.rvc_models)
        self.rvc_model_dropdown.grid(row=1, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(rvc_frame, text="Refresh Models", command=self.refresh_rvc_models).grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkButton(rvc_frame, text="Upload New Model", command=self.upload_rvc_model).grid(row=2, column=1, padx=5, pady=5, sticky=tk.W)

        # Advanced RVC Settings
        advanced_rvc_frame = ctk.CTkFrame(rvc_frame, fg_color="gray30", corner_radius=10)
        advanced_rvc_frame.grid(row=3, column=0, columnspan=3, padx=5, pady=5, sticky=tk.EW)
        advanced_rvc_frame.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(advanced_rvc_frame, text="Advanced RVC Settings", font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)

        # Pitch
        self.rvc_pitch = ctk.IntVar(value=0)
        ctk.CTkLabel(advanced_rvc_frame, text="Pitch:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(advanced_rvc_frame, textvariable=self.rvc_pitch, width=60).grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)

        # Filter Radius
        self.rvc_filter_radius = ctk.IntVar(value=3)
        ctk.CTkLabel(advanced_rvc_frame, text="Filter Radius:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(advanced_rvc_frame, textvariable=self.rvc_filter_radius, width=60).grid(row=2, column=1, padx=5, pady=5, sticky=tk.W)

        # Index Rate
        self.rvc_index_rate = ctk.DoubleVar(value=0.3)
        ctk.CTkLabel(advanced_rvc_frame, text="Index Rate:").grid(row=3, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(advanced_rvc_frame, textvariable=self.rvc_index_rate, width=60).grid(row=3, column=1, padx=5, pady=5, sticky=tk.W)

        # Volume Envelope
        self.rvc_volume_envelope = ctk.DoubleVar(value=1.0)
        ctk.CTkLabel(advanced_rvc_frame, text="Volume Envelope:").grid(row=4, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(advanced_rvc_frame, textvariable=self.rvc_volume_envelope, width=60).grid(row=4, column=1, padx=5, pady=5, sticky=tk.W)

        # Protect
        self.rvc_protect = ctk.DoubleVar(value=0.3)
        ctk.CTkLabel(advanced_rvc_frame, text="Protect:").grid(row=5, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(advanced_rvc_frame, textvariable=self.rvc_protect, width=60).grid(row=5, column=1, padx=5, pady=5, sticky=tk.W)

        # F0 Method
        self.rvc_f0_method = ctk.StringVar(value="rmvpe")
        ctk.CTkLabel(advanced_rvc_frame, text="F0 Method:").grid(row=6, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkOptionMenu(advanced_rvc_frame, variable=self.rvc_f0_method, values=["rmvpe", "crepe", "harvest"]).grid(row=6, column=1, padx=5, pady=5, sticky=tk.W)

        # Fade Section
        ctk.CTkLabel(self.audio_processing_tab, text="Fade", font=ctk.CTkFont(size=14, weight="bold")).grid(row=4, column=0, columnspan=4, padx=10, pady=10, sticky=tk.W)

        fade_frame = ctk.CTkFrame(self.audio_processing_tab, fg_color="gray20", corner_radius=10)
        fade_frame.grid(row=5, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        fade_frame.grid_columnconfigure(0, weight=1)
        fade_frame.grid_columnconfigure(1, weight=1)
        fade_frame.grid_columnconfigure(2, weight=1)
        fade_frame.grid_columnconfigure(3, weight=1)

        ctk.CTkSwitch(fade_frame, text="Enable Fade-in and Fade-out", variable=self.enable_fade).grid(row=0, column=0, columnspan=4, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(fade_frame, text="Fade-in Duration:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(fade_frame, textvariable=self.fade_in_duration).grid(row=1, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkLabel(fade_frame, text="Fade-out Duration:").grid(row=1, column=2, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(fade_frame, textvariable=self.fade_out_duration).grid(row=1, column=3, padx=5, pady=5, sticky=tk.EW)

        # Evaluation Section
        ctk.CTkLabel(self.audio_processing_tab, text="Evaluation", font=ctk.CTkFont(size=14, weight="bold")).grid(row=6, column=0, columnspan=5, padx=10, pady=10, sticky=tk.W)

        evaluation_frame = ctk.CTkFrame(self.audio_processing_tab, fg_color="gray20", corner_radius=10)
        evaluation_frame.grid(row=7, column=0, columnspan=5, padx=10, pady=(0, 20), sticky=tk.EW)
        evaluation_frame.grid_columnconfigure(0, weight=1)
        evaluation_frame.grid_columnconfigure(1, weight=1)
        evaluation_frame.grid_columnconfigure(2, weight=1)
        evaluation_frame.grid_columnconfigure(3, weight=1)
        evaluation_frame.grid_columnconfigure(4, weight=1)

        ctk.CTkSwitch(evaluation_frame, text="Enable Evaluation", variable=self.enable_tts_evaluation).grid(row=0, column=0, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkLabel(evaluation_frame, text="Target MOS Value:").grid(row=0, column=1, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(evaluation_frame, textvariable=self.target_mos_value).grid(row=0, column=2, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkLabel(evaluation_frame, text="Max Attempts:").grid(row=0, column=3, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(evaluation_frame, textvariable=self.max_attempts).grid(row=0, column=4, padx=5, pady=5, sticky=tk.EW)

        # Output Section
        ctk.CTkLabel(self.audio_processing_tab, text="Output", font=ctk.CTkFont(size=14, weight="bold")).grid(row=8, column=0, columnspan=4, padx=10, pady=10, sticky=tk.W)

        output_frame = ctk.CTkFrame(self.audio_processing_tab, fg_color="gray20", corner_radius=10)
        output_frame.grid(row=9, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        output_frame.grid_columnconfigure(0, weight=1)
        output_frame.grid_columnconfigure(1, weight=1)
        output_frame.grid_columnconfigure(2, weight=1)
        output_frame.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(output_frame, text="Format:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(output_frame, textvariable=self.output_format).grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkLabel(output_frame, text="Bitrate:").grid(row=0, column=2, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(output_frame, textvariable=self.bitrate).grid(row=0, column=3, padx=5, pady=5, sticky=tk.EW)

        # API Keys Tab
        self.api_keys_tab = self.tabview.add("API Keys")
        self.api_keys_tab.grid_columnconfigure(0, weight=1)
        self.api_keys_tab.grid_columnconfigure(1, weight=1)

        # Add a note explaining the API key storage and potential need for restart
        note_text = ("Note: API keys are saved as environment variables. "
                    "If they don't work immediately, please close Pandrator and "
                    "the launcher, then open them again.")
        note_label = ctk.CTkLabel(self.api_keys_tab, text=note_text, wraplength=600, justify="left")
        note_label.grid(row=0, column=0, columnspan=3, padx=10, pady=(10, 20), sticky="w")

        # Anthropic API Key
        ctk.CTkLabel(self.api_keys_tab, text="Anthropic API Key:").grid(row=1, column=0, padx=10, pady=10, sticky=tk.W)
        anthropic_entry = ctk.CTkEntry(self.api_keys_tab, textvariable=self.anthropic_api_key, width=300)
        anthropic_entry.grid(row=1, column=1, padx=10, pady=10, sticky=tk.W)
        ctk.CTkButton(self.api_keys_tab, text="Save", command=lambda: self.save_api_key("ANTHROPIC_API_KEY", self.anthropic_api_key.get())).grid(row=1, column=2, padx=10, pady=10)

        # OpenAI API Key
        ctk.CTkLabel(self.api_keys_tab, text="OpenAI API Key:").grid(row=2, column=0, padx=10, pady=10, sticky=tk.W)
        openai_entry = ctk.CTkEntry(self.api_keys_tab, textvariable=self.openai_api_key, width=300)
        openai_entry.grid(row=2, column=1, padx=10, pady=10, sticky=tk.W)
        ctk.CTkButton(self.api_keys_tab, text="Save", command=lambda: self.save_api_key("OPENAI_API_KEY", self.openai_api_key.get())).grid(row=2, column=2, padx=10, pady=10)

        # DeepL API Key
        ctk.CTkLabel(self.api_keys_tab, text="DeepL API Key:").grid(row=3, column=0, padx=10, pady=10, sticky=tk.W)
        deepl_entry = ctk.CTkEntry(self.api_keys_tab, textvariable=self.deepl_api_key, width=300)
        deepl_entry.grid(row=3, column=1, padx=10, pady=10, sticky=tk.W)
        ctk.CTkButton(self.api_keys_tab, text="Save", command=lambda: self.save_api_key("DEEPL_API_KEY", self.deepl_api_key.get())).grid(row=3, column=2, padx=10, pady=10)

        # Logs Tab
        self.logs_tab = self.tabview.add("Logs")
        self.logs_tab.grid_columnconfigure(0, weight=1)
        self.logs_tab.grid_rowconfigure(0, weight=1)

        self.logs_text = ctk.CTkTextbox(self.logs_tab, width=200, height=500)
        self.logs_text.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.update_logs_button = ctk.CTkButton(self.logs_tab, text="Update Logs", command=self.update_logs)
        self.update_logs_button.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="w")
        self.log_update_interval = 60000  # Update every 60 seconds
        self.master.after(0, self.update_logs)

        self.sentence_audio_data = {}  # Dictionary to store sentence audio data
        self.create_xtts_advanced_settings_frame()

        #self.populate_speaker_dropdown()

    def save_api_key(self, key_name, key_value):
        system = platform.system()

        if system == "Windows":
            # For Windows
            subprocess.run(['setx', key_name, key_value], check=True)
        elif system in ["Linux", "Darwin"]:  # Darwin is for macOS
            # For Linux and macOS
            home = os.path.expanduser("~")
            with open(f"{home}/.bashrc", "a") as bashrc:
                bashrc.write(f'\nexport {key_name}="{key_value}"')
        else:
            messagebox.showerror("Error", "Unsupported operating system")
            return

        # Set the environment variable for the current session
        os.environ[key_name] = key_value

        messagebox.showinfo("Success", f"{key_name} has been saved and is now accessible.")

    def update_logs(self):
        try:
            with open(self.log_file_path, "r") as log_file:
                logs = log_file.read()
                if logs:
                    self.logs_text.delete("1.0", tk.END)
                    self.logs_text.insert(tk.END, logs)
                    self.logs_text.see(tk.END)
                else:
                    self.logs_text.insert(tk.END, f"Log file is empty: {self.log_file_path}\n")
                    logging.warning(f"Log file is empty: {self.log_file_path}")
        except FileNotFoundError:
            self.logs_text.insert(tk.END, f"Log file not found: {self.log_file_path}\n")
            logging.error(f"Log file not found: {self.log_file_path}")
        except Exception as e:
            self.logs_text.insert(tk.END, f"Error reading log file: {str(e)}\n")
            logging.error(f"Error reading log file: {str(e)}")
        
        self.master.after(self.log_update_interval, self.update_logs)

    def show_video_selection_input(self):
        if not self.video_file_selection_label:
            self.video_file_selection_label = ctk.CTkLabel(self.dubbing_frame, text="Video File Selection:", font=ctk.CTkFont(size=14, weight="bold"))
            self.video_file_selection_label.grid(row=8, column=0, columnspan=4, padx=10, pady=(10, 5), sticky=tk.W)
        
        if not hasattr(self, 'selected_video_file_entry'):
            self.selected_video_file_entry = ctk.CTkEntry(self.dubbing_frame, textvariable=self.selected_video_file, state="readonly")
            self.selected_video_file_entry.grid(row=9, column=0, columnspan=3, padx=10, pady=5, sticky=tk.EW)
        if not hasattr(self, 'select_video_button'):
            self.select_video_button = ctk.CTkButton(self.dubbing_frame, text="Select Video", command=self.select_video_file)
            self.select_video_button.grid(row=9, column=3, padx=10, pady=5, sticky=tk.E)
            CTkToolTip(self.select_video_button, message="Choose a video file for transcription and dubbing")

    def toggle_transcription_widgets(self, show):
        if show:
            self.transcription_frame.grid()
        else:
            self.transcription_frame.grid_remove()

    def translate_subtitles(self):
        session_name = self.session_name.get()
        session_dir = os.path.abspath(os.path.join("Outputs", session_name))

        # Find the SRT file in the session directory
        srt_files = [f for f in os.listdir(session_dir) if f.lower().endswith('.srt')]
        if not srt_files:
            CTkMessagebox(title="Error", message="No SRT file found in the session folder.")
            return

        srt_file = os.path.join(session_dir, srt_files[0])

        original_language = self.original_language.get()
        target_language = self.target_language.get()
        enable_evaluation = self.enable_translation_evaluation.get()
        enable_glossary = self.enable_glossary.get()
        translation_model = self.translation_model.get()

        subdub_command = [
            "python",
            os.path.abspath("../Subdub/subdub.py"),
            "-i", srt_file,
            "-session", session_dir,
            "-sl", original_language,
            "-tl", target_language,
            "-task", "translate"
        ]

        if translation_model == "deepl":
            subdub_command.extend(["-llmapi", "deepl"])
        else:
            subdub_command.extend(["-llm-model", translation_model])
            if enable_evaluation:
                subdub_command.append("-evaluate")
            if enable_glossary:
                subdub_command.append("-glossary")

        logging.info(f"Executing translation command: {' '.join(subdub_command)}")

        try:
            process = subprocess.Popen(subdub_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            for line in process.stdout:
                print(line, end='')  # Print to console
                logging.info(line.strip())  # Log the output
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, subdub_command)
            logging.info("Translation process completed successfully.")
            CTkMessagebox(title="Translation Complete", message="Subtitles have been translated successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Translation failed: {str(e)}")
            CTkMessagebox(title="Error", message=f"Translation failed: {str(e)}")

    def remove_video_selection_input(self):
        if hasattr(self, 'selected_video_file_entry'):
            self.selected_video_file_entry.grid_remove()
        if hasattr(self, 'select_video_button'):
            self.select_video_button.grid_remove()
        if self.video_file_selection_label:
            self.video_file_selection_label.grid_remove()

    def synchronize_and_save(self):
        session_name = self.session_name.get()
        session_dir = os.path.abspath(os.path.join("Outputs", session_name))
        sentence_wavs_dir = os.path.join(session_dir, "Sentence_wavs")

        if not os.path.exists(sentence_wavs_dir) or not os.listdir(sentence_wavs_dir):
            CTkMessagebox(title="TTS Generation Required", message="Please perform TTS first (click 'Start Generation' below) before synchronizing and saving.")
            return

        # Check if the required elements are present in the session folder
        video_files = [f for f in os.listdir(session_dir) if f.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov'))]
        speech_blocks_files = [f for f in os.listdir(session_dir) if f.lower().endswith('_speech_blocks.json')]
        wav_files = [f for f in os.listdir(os.path.join(session_dir, "Sentence_wavs")) if f.lower().endswith('.wav')]

        if not video_files or not speech_blocks_files or not wav_files:
            CTkMessagebox(title="Missing Elements", message="Please check if all elements needed for synchronization are in the session folder.")
            return

        # Run Subdub with the sync task
        subdub_command = [
            "python",
            os.path.abspath("../Subdub/subdub.py"),
            "-session", session_dir,
            "-task", "sync"
        ]

        logging.info(f"Executing synchronization command: {' '.join(subdub_command)}")

        try:
            process = subprocess.Popen(subdub_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            for line in process.stdout:
                print(line, end='')  # Print to console
                logging.info(line.strip())  # Log the output
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, subdub_command)
            logging.info("Synchronization process completed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Synchronization failed: {str(e)}")
            CTkMessagebox(title="Error", message=f"Synchronization failed: {str(e)}")
            return

        # Monitor the session folder for a new video file
        synced_video_path = None
        timeout = 3600  # Timeout after 1 hour
        start_time = time.time()
        while time.time() - start_time < timeout:
            time.sleep(1)
            new_video_files = [f for f in os.listdir(session_dir) if f.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov')) and f not in video_files]
            if new_video_files:
                synced_video_path = os.path.join(session_dir, new_video_files[0])
                logging.info(f"Synchronized video file found: {synced_video_path}")
                break

        if synced_video_path:
            CTkMessagebox(title="Synchronization Complete", message=f"Synchronization has been completed. The synchronized video is available in the session folder: {synced_video_path}")
        else:
            logging.warning("Timeout: Synchronized video file not found.")
            CTkMessagebox(title="Timeout", message="Synchronized video file not found within the timeout period.")

    def select_video_file(self):
        video_file = filedialog.askopenfilename(filetypes=[("Video Files", "*.mp4;*.mkv;*.webm;*.avi;*.mov")])
        if video_file:
            session_name = self.session_name.get()
            session_dir = os.path.join("Outputs", session_name)
            
            # Ensure the session directory exists
            os.makedirs(session_dir, exist_ok=True)
            
            # Get the filename of the selected video
            video_filename = os.path.basename(video_file)
            
            # Copy the video file to the session directory
            destination_path = os.path.join(session_dir, video_filename)
            shutil.copy(video_file, destination_path)
            
            # Update the selected video file entry
            self.selected_video_file.set(destination_path)

    def refresh_audio_tracks(self):
        video_file = self.selected_video_file.get()
        if video_file:
            try:
                probe = ffmpeg.probe(video_file)
                audio_tracks = [str(stream["index"]) for stream in probe["streams"] if stream["codec_type"] == "audio"]
                self.audio_track_dropdown.configure(values=audio_tracks)
                if audio_tracks:
                    self.selected_audio_track.set(audio_tracks[0])
            except ffmpeg.Error as e:
                messagebox.showerror("FFmpeg Error", f"An error occurred while probing the video file: {str(e)}")


    def add_dubbing_to_video(self):
        if not self.session_name.get():
            CTkMessagebox(title="No Session", message="Please create or load a session before adding dubbing to video.", icon="info")
            return

        session_name = self.session_name.get()
        session_dir = os.path.abspath(os.path.join("Outputs", session_name))

        # Check if the required elements are present in the session folder
        video_files = [f for f in os.listdir(session_dir) if f.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov'))]
        speech_blocks_files = [f for f in os.listdir(session_dir) if f.lower().endswith('_speech_blocks.json')]
        wav_files = [f for f in os.listdir(os.path.join(session_dir, "Sentence_wavs")) if f.lower().endswith('.wav')]

        if not video_files or not speech_blocks_files or not wav_files:
            CTkMessagebox(title="Missing Elements", message="Please check if all elements needed for synchronization are in the session folder.")
            return

        # Run Subdub with the sync task
        subdub_command = [
            "python",
            os.path.abspath("../Subdub/subdub.py"),
            "-session", session_dir,
            "-task", "sync"
        ]

        logging.info(f"Executing synchronization command: {' '.join(subdub_command)}")

        try:
            process = subprocess.Popen(subdub_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, encoding='utf-8', errors='replace')
            for line in process.stdout:
                print(line, end='')
                logging.info(line.strip())
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, subdub_command)
            logging.info("Synchronization process completed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Synchronization failed: {str(e)}")
            CTkMessagebox(title="Error", message=f"Synchronization failed: {str(e)}")
            return
        except Exception as e:
            logging.error(f"An unexpected error occurred during synchronization: {str(e)}")
            CTkMessagebox(title="Error", message=f"An unexpected error occurred during synchronization: {str(e)}")
            return

        # Find the synced video file
        synced_video_files = [f for f in os.listdir(session_dir) if f.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov')) and f not in video_files]
        if not synced_video_files:
            CTkMessagebox(title="Error", message="Synced video file not found.")
            return
        synced_video_path = os.path.join(session_dir, synced_video_files[0])

        # Find the most recent SRT file
        srt_files = [f for f in os.listdir(session_dir) if f.lower().endswith('.srt')]
        if not srt_files:
            CTkMessagebox(title="Error", message="No SRT file found in the session folder.")
            return
        most_recent_srt = max([os.path.join(session_dir, f) for f in srt_files], key=os.path.getmtime)

        # Run Subdub with the equalize task
        equalize_command = [
            "python",
            os.path.abspath("../Subdub/subdub.py"),
            "-i", most_recent_srt,
            "-task", "equalize"
        ]

        logging.info(f"Executing equalization command: {' '.join(equalize_command)}")

        try:
            process = subprocess.Popen(equalize_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, encoding='utf-8', errors='replace')
            for line in process.stdout:
                print(line, end='')
                logging.info(line.strip())
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, equalize_command)
            logging.info("Equalization process completed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Equalization failed: {str(e)}")
            CTkMessagebox(title="Error", message=f"Equalization failed: {str(e)}")
            return
        except Exception as e:
            logging.error(f"An unexpected error occurred during equalization: {str(e)}")
            CTkMessagebox(title="Error", message=f"An unexpected error occurred during equalization: {str(e)}")
            return

        # Find the equalized SRT file
        equalized_srt_files = [f for f in os.listdir(session_dir) if f.lower().endswith('_equalized.srt')]
        if not equalized_srt_files:
            CTkMessagebox(title="Error", message="Equalized SRT file not found.")
            return
        equalized_srt_path = os.path.join(session_dir, equalized_srt_files[0])

        # Add the equalized subtitles to the synced video
        output_video_path = os.path.join(session_dir, f"{session_name}_final.mp4")
        ffmpeg_command = [
            "ffmpeg",
            "-i", synced_video_path,
            "-i", equalized_srt_path,
            "-c", "copy",
            "-c:s", "mov_text",
            output_video_path
        ]

        logging.info(f"Executing FFmpeg command to add subtitles: {' '.join(ffmpeg_command)}")

        try:
            result = subprocess.run(ffmpeg_command, check=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
            logging.info("Subtitles added successfully.")
            CTkMessagebox(title="Success", message=f"Dubbing and subtitles have been added. The final video is available at: {output_video_path}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to add subtitles: {e.stderr}")
            CTkMessagebox(title="Error", message=f"Failed to add subtitles: {e.stderr}")
        except Exception as e:
            logging.error(f"An unexpected error occurred while adding subtitles: {str(e)}")
            CTkMessagebox(title="Error", message=f"An unexpected error occurred while adding subtitles: {str(e)}")

    def generate_dubbing_audio(self):
        if not self.session_name.get():
            CTkMessagebox(title="No Session", message="Please create or load a session before generating dubbing audio.", icon="info")
            return

        session_name = self.session_name.get()
        session_dir = os.path.abspath(os.path.join("Outputs", session_name))

        # Step 1: Transcription (if necessary)
        initial_srt_files = set(f for f in os.listdir(session_dir) if f.lower().endswith('.srt'))
        if not initial_srt_files:
            if self.pre_selected_source_file.lower().endswith((".mp4", ".mkv", ".webm", ".avi", ".mov")):
                self.only_transcribe()
                if not self.wait_for_new_file(session_dir, initial_srt_files, ".srt", "Transcription"):
                    return
            else:
                CTkMessagebox(title="Error", message="No SRT file found and no video file selected. Please select a video file or add an SRT file to the session folder.", icon="cancel")
                return
        else:
            logging.info(f"Skipping transcription as SRT file(s) already exist: {initial_srt_files}")

        # Step 2: Translation (if enabled and necessary)
        if self.enable_translation.get():
            pre_translation_srt_files = set(f for f in os.listdir(session_dir) if f.lower().endswith('.srt'))
            translated_srt = next((f for f in pre_translation_srt_files if f.split('.')[-2].lower() in ['en', 'es', 'fr', 'de', 'it', 'pt', 'pl', 'tr', 'ru', 'nl', 'cs', 'ar', 'zh-cn', 'ja', 'hu', 'ko', 'hi'] or f.endswith('_eval.srt')), None)
            if not translated_srt:
                self.only_translate()
                if not self.wait_for_new_file(session_dir, pre_translation_srt_files, ".srt", "Translation"):
                    return
            else:
                logging.info(f"Skipping translation as a translated or evaluated SRT file already exists: {translated_srt}")

        # Step 3: Generate speech blocks
        pre_speech_blocks_files = set(os.listdir(session_dir))
        self.generate_speech_blocks()
        if not self.wait_for_new_file(session_dir, pre_speech_blocks_files, "_sentences.json", "Speech block generation"):
            return

        # Step 4: Perform TTS generation
        self.start_optimisation_thread()

    def wait_for_new_file(self, directory, initial_files, file_extension, step_name, timeout=3600):
        start_time = time.time()
        while time.time() - start_time < timeout:
            current_files = set(f for f in os.listdir(directory) if f.lower().endswith(file_extension))
            new_files = current_files - initial_files
            if new_files:
                logging.info(f"New {file_extension} file(s) detected after {step_name}: {new_files}")
                return True
            time.sleep(1)
        
        CTkMessagebox(title="Timeout", message=f"Timeout: No new {file_extension} file detected after {step_name}.")
        return False

    def only_transcribe(self):
        if not self.session_name.get():
            CTkMessagebox(title="No Session", message="Please create or load a session before transcribing.", icon="info")
            return

        session_name = self.session_name.get()
        session_dir = os.path.abspath(os.path.join("Outputs", session_name))

        # Find the video file in the session directory
        video_files = [f for f in os.listdir(session_dir) if f.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov'))]
        
        if not video_files:
            CTkMessagebox(title="Error", message="No video file found in the session folder.")
            return

        video_file = os.path.join(session_dir, video_files[0])  # Use the first video file found
        video_filename = os.path.splitext(os.path.basename(video_file))[0]  # Get the video filename without extension

        logging.info(f"Starting transcription process for video file: {video_file}")

        # Create a WAV file in the session directory
        wav_file = os.path.join(session_dir, f"{video_filename}.wav")

        logging.info(f"WAV file will be created at: {wav_file}")

        try:
            # Convert video to WAV using FFmpeg
            ffmpeg_command = [
                "ffmpeg",
                "-i", video_file,
                "-vn",  # Disable video
                "-acodec", "pcm_s16le",  # Audio codec
                "-ar", "16000",  # Audio sample rate
                "-ac", "1",  # Mono audio
                wav_file
            ]

            logging.info(f"Executing FFmpeg command: {' '.join(ffmpeg_command)}")

            ffmpeg_process = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            stdout, stderr = ffmpeg_process.communicate()

            if ffmpeg_process.returncode != 0:
                logging.error(f"FFmpeg command failed. Return code: {ffmpeg_process.returncode}")
                logging.error(f"FFmpeg stderr: {stderr}")
                raise subprocess.CalledProcessError(ffmpeg_process.returncode, ffmpeg_command, stderr)

            logging.info("Video successfully converted to WAV.")

            # Check if the WAV file was created and has content
            if not os.path.exists(wav_file) or os.path.getsize(wav_file) == 0:
                raise FileNotFoundError(f"WAV file is missing or empty: {wav_file}")

            # Transcription using the WAV file
            output_srt = os.path.join(session_dir, f"{video_filename}.srt")
            whisperx_command = [
                "python",
                "-m", "whisperx",  # Use -m to run whisperx as a module
                wav_file,
                "--model", self.whisperx_model.get(),
                "--language", self.whisperx_language.get(),
                "--output_format", "srt",
                "--output_dir", session_dir
            ]

            logging.info(f"Executing transcription command: {' '.join(whisperx_command)}")

            whisperx_process = subprocess.Popen(whisperx_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            stdout, stderr = whisperx_process.communicate()

            logging.info(f"Whisperx stdout: {stdout}")
            if stderr:
                logging.error(f"Whisperx stderr: {stderr}")

            if whisperx_process.returncode != 0:
                raise subprocess.CalledProcessError(whisperx_process.returncode, whisperx_command, stderr)

            logging.info("Transcription completed successfully.")

            # The output SRT file will have the same name as the WAV file
            output_srt = os.path.join(session_dir, f"{video_filename}.srt")

            # Verify that the SRT file was created
            if not os.path.exists(output_srt):
                raise FileNotFoundError(f"Expected SRT file not found: {output_srt}")

            logging.info(f"SRT file created: {output_srt}")

            CTkMessagebox(title="Transcription Complete", message="Transcription has been completed successfully.")

        except subprocess.CalledProcessError as e:
            logging.error(f"Command failed: {e.cmd}")
            logging.error(f"Return code: {e.returncode}")
            logging.error(f"Output: {e.output}")
            CTkMessagebox(title="Error", message=f"FFmpeg or Transcription failed: {str(e)}")
        except FileNotFoundError as e:
            logging.error(str(e))
            CTkMessagebox(title="Error", message=str(e))
        except Exception as e:
            logging.error(f"An unexpected error occurred: {str(e)}")
            CTkMessagebox(title="Error", message=f"An unexpected error occurred: {str(e)}")
        finally:
            # Optionally, remove the WAV file if you don't need it anymore
            if os.path.exists(wav_file):
                os.remove(wav_file)
                logging.info(f"WAV file removed: {wav_file}")
            else:
                logging.warning(f"WAV file not found for removal: {wav_file}")

    def only_translate(self):
        if not self.session_name.get():
            CTkMessagebox(title="No Session", message="Please create or load a session before translating.", icon="info")
            return

        session_name = self.session_name.get()
        session_dir = os.path.abspath(os.path.join("Outputs", session_name))

        # Find the most recent SRT file in the session directory
        srt_files = [f for f in os.listdir(session_dir) if f.lower().endswith('.srt')]
        if not srt_files:
            CTkMessagebox(title="No SRT File", message="No SRT file found in the session folder. Please add an SRT file or perform transcription of a video first.", icon="warning")
            return

        most_recent_srt = max([os.path.join(session_dir, f) for f in srt_files], key=os.path.getmtime)

        original_language = self.original_language.get()
        target_language = self.target_language.get()
        enable_evaluation = self.enable_translation_evaluation.get()
        enable_glossary = self.enable_glossary.get()
        translation_model = self.translation_model.get()

        subdub_command = [
            "python",
            os.path.abspath("../Subdub/subdub.py"),
            "-i", most_recent_srt,
            "-session", session_dir,
            "-sl", original_language,
            "-tl", target_language,
            "-task", "translate"
        ]

        if translation_model == "deepl":
            subdub_command.extend(["-llmapi", "deepl"])
        elif translation_model == "local":
            subdub_command.extend(["-llmapi", "local"])
        else:
            subdub_command.extend(["-llm-model", translation_model])
        
        if enable_evaluation and translation_model != "deepl":
            subdub_command.append("-evaluate")
        if enable_glossary and translation_model != "deepl":
            subdub_command.append("-glossary")

        logging.info(f"Executing translation command: {' '.join(subdub_command)}")

        try:
            process = subprocess.Popen(subdub_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            for line in process.stdout:
                print(line, end='')  # Print to console
                logging.info(line.strip())  # Log the output
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, subdub_command)
            logging.info("Translation process completed successfully.")
            CTkMessagebox(title="Translation Complete", message="Subtitles have been translated successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Translation failed: {str(e)}")
            CTkMessagebox(title="Error", message=f"Translation failed: {str(e)}")

    def generate_speech_blocks(self):
        session_name = self.session_name.get()
        session_dir = os.path.abspath(os.path.join("Outputs", session_name))

        # Find the most recent SRT file (original or translated)
        srt_files = [f for f in os.listdir(session_dir) if f.lower().endswith('.srt')]
        if not srt_files:
            CTkMessagebox(title="Error", message="No SRT file found in the session folder.")
            return

        most_recent_srt = max([os.path.join(session_dir, f) for f in srt_files], key=os.path.getmtime)

        # Generate speech blocks using Subdub
        subdub_speech_blocks_command = [
            "python",
            os.path.abspath("../Subdub/subdub.py"),
            "-session", session_dir,
            "-i", most_recent_srt,
            "-task", "speech_blocks"
        ]

        logging.info(f"Executing speech blocks generation command: {' '.join(subdub_speech_blocks_command)}")

        try:
            process = subprocess.Popen(subdub_speech_blocks_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            for line in process.stdout:
                print(line, end='')  # Print to console
                logging.info(line.strip())  # Log the output
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, subdub_speech_blocks_command)
            logging.info("Speech blocks generation completed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Speech block generation failed: {str(e)}")
            CTkMessagebox(title="Error", message=f"Speech block generation failed: {str(e)}")
            return

        speech_blocks_file = os.path.join(session_dir, f"{os.path.splitext(os.path.basename(most_recent_srt))[0]}_speech_blocks.json")

        # Wait for the speech blocks file to be generated
        timeout = 60  # 60 seconds timeout
        start_time = time.time()
        while not os.path.exists(speech_blocks_file):
            if time.time() - start_time > timeout:
                CTkMessagebox(title="Error", message="Timeout: Speech blocks file was not generated.")
                return
            time.sleep(1)  # Wait for 1 second before checking again

        try:
            with open(speech_blocks_file, 'r', encoding='utf-8') as f:
                speech_blocks = json.load(f)
            logging.info(f"Successfully loaded speech blocks from {speech_blocks_file}")
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse speech blocks file: {str(e)}")
            CTkMessagebox(title="Error", message=f"Failed to parse speech blocks file: {str(e)}")
            return

        pandrator_sentences = []
        for block in speech_blocks:
            pandrator_sentences.append({
                "sentence_number": str(block["number"]),
                "original_sentence": block["text"],
                "tts_generated": "no",
            })

        json_filename = os.path.join(session_dir, f"{session_name}_sentences.json")
        self.save_json(pandrator_sentences, json_filename)
        logging.info(f"Saved Pandrator sentences to {json_filename}")

    def select_video_file(self):
        video_file = filedialog.askopenfilename(filetypes=[("Video Files", "*.mp4;*.mkv;*.webm;*.avi;*.mov")])
        if video_file:
            session_name = self.session_name.get()
            session_dir = os.path.join("Outputs", session_name)
            
            # Ensure the session directory exists
            os.makedirs(session_dir, exist_ok=True)
            
            # Get the filename of the selected video
            video_filename = os.path.basename(video_file)
            
            # Copy the video file to the session directory
            destination_path = os.path.join(session_dir, video_filename)
            shutil.copy(video_file, destination_path)
            
            # Update the selected video file entry
            self.selected_video_file.set(destination_path)


    def on_translation_model_change(self, *args):
        if self.translation_model.get() == "deepl":
            self.enable_translation_evaluation_switch.configure(state="disabled")
            self.enable_glossary_switch.configure(state="disabled")
            self.enable_translation_evaluation.set(False)
            self.enable_glossary.set(False)
        else:
            self.enable_translation_evaluation_switch.configure(state="normal")
            self.enable_glossary_switch.configure(state="normal")
    
    def toggle_dubbing_frame(self):
        if self.enable_dubbing.get():
            self.dubbing_frame.grid()  # Show the dubbing frame
            if not self.source_file.endswith(".srt"):
                # Hide the video selection input if a video file is chosen
                self.select_video_button.grid_remove()
                self.selected_video_file_entry.grid_remove()
        else:
            self.dubbing_frame.grid_remove()  # Hide the dubbing frame
    
    def select_file(self):
        if not self.session_name.get():
            CTkMessagebox(title="No Session", message="Please create or load a session before selecting a file.", icon="info")
            return

        self.pre_selected_source_file = filedialog.askopenfilename(
            title="Select Source File",
            filetypes=[("Text, SRT, PDF, EPUB, and Video files", "*.txt *.srt *.pdf *.epub *.mp4 *.mkv *.webm *.avi *.mov"),
                    ("All files", "*.*")]
        )
        if self.pre_selected_source_file:
            file_name = os.path.basename(self.pre_selected_source_file)
            truncated_file_name = file_name[:70] + "..." if len(file_name) > 70 else file_name
            self.selected_file_label.configure(text=truncated_file_name)

            session_name = self.session_name.get()
            session_dir = os.path.join("Outputs", session_name)
            os.makedirs(session_dir, exist_ok=True)

            # Remove old text, srt, pdf, or epub files from the session directory
            for ext in [".txt", ".srt", ".pdf", ".epub"]:
                for file in os.listdir(session_dir):
                    if file.lower().endswith(ext):
                        os.remove(os.path.join(session_dir, file))

            if self.pre_selected_source_file.lower().endswith((".epub", ".docx", ".mobi")):
                # Convert epub to txt using ebook-convert
                txt_filename = os.path.splitext(file_name)[0] + ".txt"
                txt_path = os.path.join(session_dir, txt_filename)
                
                def run_ebook_convert(command):
                    try:
                        subprocess.run(command, check=True)
                        return True
                    except subprocess.CalledProcessError:
                        return False

                # Try the default ebook-convert command
                if run_ebook_convert(["ebook-convert", self.pre_selected_source_file, txt_path]):
                    self.master.after(0, self.review_extracted_text, txt_path)
                else:
                    # If failed, try with ebook-convert.exe from one folder up
                    calibre_portable_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'Calibre Portable', 'Calibre', 'ebook-convert.exe'))
                    if run_ebook_convert([calibre_portable_path, self.pre_selected_source_file, txt_path]):
                        self.master.after(0, self.review_extracted_text, txt_path)
                    else:
                        messagebox.showerror("Error", "Failed to convert epub to txt using both default and Calibre Portable paths.")
                        self.pre_selected_source_file = None
                        self.selected_file_label.configure(text="No file selected")

            elif self.pre_selected_source_file.lower().endswith(".pdf"):
                # Extract text from PDF file
                pdf = XPdf(self.pre_selected_source_file)
                extracted_text = pdf.to_text()

                # Save the raw extracted text
                raw_text_filename = os.path.splitext(file_name)[0] + "_raw_text.txt"
                raw_text_path = os.path.join(session_dir, raw_text_filename)
                with open(raw_text_path, "w", encoding="utf-8", newline='\n') as file:
                    file.write(extracted_text)

                self.master.after(0, self.show_pdf_options, raw_text_path, session_dir, file_name)

            else:
                shutil.copy(self.pre_selected_source_file, session_dir)
                self.source_file = os.path.join(session_dir, file_name)
 
            # Handle dubbing-related UI elements
            if self.pre_selected_source_file.lower().endswith((".mp4", ".mkv", ".webm", ".avi", ".mov")):
                self.dubbing_frame.grid()
                self.video_file_selection_frame.grid_remove()
                self.only_transcribe_button.configure(state=tk.NORMAL)
                self.only_translate_button.configure(state=tk.DISABLED)
                self.toggle_transcription_widgets(True)  # Show transcription widgets
            elif self.pre_selected_source_file.lower().endswith(".srt"):
                self.dubbing_frame.grid()
                self.video_file_selection_frame.grid()
                self.only_transcribe_button.configure(state=tk.DISABLED)
                self.only_translate_button.configure(state=tk.NORMAL)
                self.toggle_transcription_widgets(False)  # Hide transcription widgets
            else:
                self.dubbing_frame.grid_remove()
                self.video_file_selection_frame.grid_remove()
                self.toggle_transcription_widgets(False)  # Hide transcription widgets

            # Disable the Start Generation button for video and SRT files
            if self.pre_selected_source_file.lower().endswith((".mp4", ".mkv", ".webm", ".avi", ".mov", ".srt")):
                self.start_generation_button.configure(state=tk.DISABLED)

        else:
            self.pre_selected_source_file = None
            self.selected_file_label.configure(text="No file selected")
            self.dubbing_frame.grid_remove()
            self.video_file_selection_frame.grid_remove()
            self.start_generation_button.configure(state=tk.NORMAL)

        self.pdf_preprocessed = False  # Reset the flag

    def toggle_transcription_widgets(self, show):
        if show:
            self.transcription_frame.grid()
        else:
            self.transcription_frame.grid_remove()

    def paste_text(self):
        if not self.session_name.get():
            CTkMessagebox(title="No Session", message="Please create or load a session before pasting text.", icon="info")
            return

        paste_window = ctk.CTkToplevel(self.master)
        paste_window.title("Paste Text")
        paste_window.geometry("600x450")
        paste_window.transient(self.master)  # Set the main window as the parent
        paste_window.grab_set()  # Make the window modal
        paste_window.focus_set()  # Give focus to the paste window

        text_widget = ctk.CTkTextbox(paste_window, width=580, height=350)
        text_widget.pack(padx=10, pady=10)

        mark_paragraphs_multiple_newlines = ctk.BooleanVar(value=False)
        paragraph_toggle = ctk.CTkSwitch(paste_window, text="Mark paragraphs only for multiple new lines", variable=mark_paragraphs_multiple_newlines)
        paragraph_toggle.pack(padx=10, pady=(0, 10))

        def save_pasted_text():
            pasted_text = text_widget.get("1.0", tk.END).strip()
            if pasted_text:
                session_name = self.session_name.get()
                session_dir = os.path.join("Outputs", session_name)
                os.makedirs(session_dir, exist_ok=True)
                
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"pasted_text_{timestamp}.txt"
                file_path = os.path.join(session_dir, filename)
                
                if mark_paragraphs_multiple_newlines.get():
                    # Preserve multiple newlines, replace single newlines with spaces
                    processed_text = re.sub(r'(?<!\n)\n(?!\n)', ' ', pasted_text)
                else:
                    # Convert single newlines to double newlines
                    processed_text = re.sub(r'(?<!\n)\n(?!\n)', '\n\n', pasted_text)
                
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(processed_text)
                
                self.source_file = file_path
                truncated_filename = filename[:70] + "..." if len(filename) > 70 else filename
                self.selected_file_label.configure(text=truncated_filename)
                
                paste_window.destroy()
                CTkMessagebox(title="Text Saved", message=f"The pasted text has been saved as '{filename}' in the session folder.", icon="info")
            else:
                CTkMessagebox(title="Empty Text", message="Please paste some text before saving.", icon="warning")

        save_button = ctk.CTkButton(paste_window, text="Save", command=save_pasted_text)
        save_button.pack(pady=10)

        # Wait for the window to be destroyed before returning
        paste_window.wait_window()

    def show_pdf_options(self, raw_text_path, session_dir, file_name):
        with open(raw_text_path, "r", encoding="utf-8") as file:
            raw_text = file.read()
        preprocessed_text = self.preprocess_text_pdf(raw_text)
        preprocessed_filename = os.path.splitext(file_name)[0] + "_preprocessed.txt"
        preprocessed_path = os.path.join(session_dir, preprocessed_filename)
        with open(preprocessed_path, "w", encoding="utf-8", newline='\n') as file:
            file.write(preprocessed_text)
        self.master.after(0, self.review_extracted_text, preprocessed_path)

    def review_extracted_text(self, file_path):
        review_window = ctk.CTkToplevel(self.master)
        review_window.title("Review Extracted Text")
        review_window.transient(self.master)  # Set the main window as the parent

        # Get the screen resolution
        screen_width = review_window.winfo_screenwidth()
        screen_height = review_window.winfo_screenheight()
        window_width = 800
        window_height = int(screen_height * 0.90)  # Set the window height to 90% of the screen height
        x = (screen_width // 2) - (window_width // 2)
        y = (screen_height // 2) - (window_height // 2)
        review_window.geometry(f"{window_width}x{window_height}+{x}+{y}")  # Set the window size and position

        top_frame = ctk.CTkFrame(review_window)
        top_frame.pack(padx=10, pady=(10, 0), fill=tk.X)

        def toggle_newline_handling():
            if not self.remove_double_newlines.get():
                # If the switch is turned off after being turned on, remove the processed file
                session_name = self.session_name.get()
                session_dir = os.path.join("Outputs", session_name)
                file_name = os.path.basename(file_path)
                preprocessed_filename = os.path.splitext(file_name)[0] + "_preprocessed.txt"
                preprocessed_path = os.path.join(session_dir, preprocessed_filename)
                if os.path.exists(preprocessed_path):
                    os.remove(preprocessed_path)
            update_text()

        ctk.CTkSwitch(top_frame, text="Remove Double Newlines (try if paragraphs are not rendered correctly)", 
                    variable=self.remove_double_newlines, command=toggle_newline_handling).pack(side=tk.LEFT)

        text_widget = ctk.CTkTextbox(review_window, width=window_width-20, height=window_height-100)

        def update_text():
            text_widget.delete("1.0", tk.END)
            text_widget.insert("1.0", "Processing text...")
            text_widget.update()

            with open(file_path, "r", encoding="utf-8") as file:
                text = file.read()

            session_name = self.session_name.get()
            session_dir = os.path.join("Outputs", session_name)
            os.makedirs(session_dir, exist_ok=True)  # Create the session directory if it doesn't exist

            file_name = os.path.basename(file_path)
            raw_text_filename = os.path.splitext(file_name)[0] + "_raw_text.txt"
            raw_text_path = os.path.join(session_dir, raw_text_filename)

            with open(raw_text_path, "w", encoding="utf-8", newline='\n') as file:
                file.write(text)

            if self.remove_double_newlines.get():
                preprocessed_filename = os.path.splitext(file_name)[0] + "_preprocessed.txt"
                preprocessed_path = os.path.join(session_dir, preprocessed_filename)
                text = self.preprocess_text_pdf(text, remove_double_newlines=True)
                with open(preprocessed_path, "w", encoding="utf-8", newline='\n') as file:
                    file.write(text)
                with open(preprocessed_path, "r", encoding="utf-8") as file:
                    updated_text = file.read()
            else:
                text = self.preprocess_text_pdf(text, remove_double_newlines=False)
                with open(raw_text_path, "w", encoding="utf-8", newline='\n') as file:
                    file.write(text)
                with open(raw_text_path, "r", encoding="utf-8") as file:
                    updated_text = file.read()

            text_widget.delete("1.0", tk.END)
            text_widget.insert("1.0", updated_text)

        update_text()  # Initial update of the text widget

        text_widget.pack(padx=10, pady=(10, 10))

        def accept_text():
            session_name = self.session_name.get()
            session_dir = os.path.join("Outputs", session_name)
            file_name = f"{session_name}_edited.txt"
            edited_text_path = os.path.join(session_dir, file_name)
            with open(edited_text_path, "w", encoding="utf-8", newline='\n') as file:
                file.write(text_widget.get("1.0", "end-1c"))  # Exclude the trailing newline character

            self.source_file = edited_text_path
            review_window.destroy()
            self.pdf_preprocessed = True  # Set the flag to indicate that the text has been preprocessed

        def cancel_import():
            self.pre_selected_source_file = None
            self.selected_file_label.configure(text="No file selected")
            review_window.destroy()

        button_frame = ctk.CTkFrame(top_frame)
        button_frame.pack(side=tk.RIGHT)

        cancel_button = ctk.CTkButton(button_frame, text="Cancel", command=cancel_import)
        cancel_button.pack(side=tk.LEFT, padx=(0, 10))

        accept_button = ctk.CTkButton(button_frame, text="Accept", command=accept_text)
        accept_button.pack(side=tk.LEFT)
        
    def preprocess_text_pdf(self, text, remove_double_newlines=False):
        # Normalize new lines to LF (\\n)
        text = regex.sub(r'\r\n|\r', '\n', text)
        
        # Step 1: Remove specific characters
        text = regex.sub(r'[\x00-\x09\x0B-\x1F\x7F]', '', text)
        
        if remove_double_newlines:
            # Remove double newlines only if there is no sentence-ending punctuation before them
            text = regex.sub(r'(?<![.!?])\n\n', ' ', text)
        else:
            # Remove all single newlines and replace them with spaces
            text = regex.sub(r'\n$(?<!\n[ \t]*\n)|(?<!\n[ \t]*)\n(?![ \t]*\n)', ' ', text)
        
        # Step 3: Replace all double, triple, and quadruple new lines with a single new line
        text = regex.sub(r'[ \\t]*\\n[ \\t]*\\n[ \\t]*(?:\\n[ \\t]*){0,2}', '\\n', text)
        
        # Condense multiple spaces to one
        text = regex.sub(r' {2,}', ' ', text)
        
        # Remove all spaces and tabs at the beginning of all lines
        text = regex.sub(r'(?m)^[ \\t]+', '', text)
        
        return text

    def toggle_external_server(self):
        if self.use_external_server.get():
            self.external_server_url_entry.grid()
        else:
            self.external_server_url_entry.grid_remove()
            self.external_server_connected = False

        if self.use_external_server_voicecraft.get():
            self.external_server_url_entry_voicecraft.grid()
        else:
            self.external_server_url_entry_voicecraft.grid_remove()
            self.external_server_connected_voicecraft = False

    def connect_to_server(self):
        if self.tts_service.get() == "XTTS":
            if self.use_external_server.get():
                external_server_url = self.external_server_url.get()
                try:
                    response = requests.get(f"{external_server_url}/docs")
                    if response.status_code == 200:
                        self.external_server_connected = True
                        self.populate_speaker_dropdown()
                        self.populate_xtts_models()
                    else:
                        CTkMessagebox(title="Error", message=f"Failed to connect to the external XTTS server. Status code: {response.status_code}", icon="cancel")
                except requests.exceptions.RequestException as e:
                    CTkMessagebox(title="Error", message=f"Failed to connect to the external XTTS server: {str(e)}", icon="cancel")
            else:
                try:
                    speaker_folder_path = os.path.abspath(self.tts_voices_folder)
                    data = {"speaker_folder": speaker_folder_path}
                    response = requests.post("http://localhost:8020/set_speaker_folder", json=data)
                    if response.status_code == 200:
                        print(f"Speaker folder set to: {speaker_folder_path}")
                        response = requests.get("http://localhost:8020/docs")
                        if response.status_code == 200:
                            self.populate_speaker_dropdown()
                            self.populate_xtts_models()
                        else:
                            CTkMessagebox(title="Error", message=f"Failed to connect to the local XTTS server. Status code: {response.status_code}", icon="cancel")
                    else:
                        CTkMessagebox(title="Error", message=f"Failed to set speaker folder. Status code: {response.status_code}", icon="cancel")
                except requests.exceptions.RequestException as e:
                    CTkMessagebox(title="Error", message=f"Failed to connect to the local XTTS server: {str(e)}", icon="cancel")
        elif self.tts_service.get() == "VoiceCraft":
            if self.use_external_server_voicecraft.get():
                external_server_url = self.external_server_url_voicecraft.get()
                try:
                    response = requests.get(f"{external_server_url}/docs")
                    if response.status_code == 200:
                        self.external_server_connected_voicecraft = True
                        self.populate_speaker_dropdown()
                    else:
                        CTkMessagebox(title="Error", message=f"Failed to connect to the external VoiceCraft server. Status code: {response.status_code}", icon="cancel")
                except requests.exceptions.RequestException as e:
                    CTkMessagebox(title="Error", message=f"Failed to connect to the external VoiceCraft server: {str(e)}", icon="cancel")
            else:
                try:
                    response = requests.get("http://localhost:8245/docs")
                    if response.status_code == 200:
                        self.external_server_connected_voicecraft = False
                        self.populate_speaker_dropdown()
                    else:
                        CTkMessagebox(title="Error", message=f"Failed to connect to the local VoiceCraft server. Status code: {response.status_code}", icon="cancel")
                except requests.exceptions.RequestException as e:
                    CTkMessagebox(title="Error", message=f"Failed to connect to the local VoiceCraft server: {str(e)}", icon="cancel")

    def populate_speaker_dropdown(self):
        if self.tts_service.get() == "XTTS":
            if self.use_external_server.get() and self.external_server_connected:
                external_server_url = self.external_server_url.get()
                try:
                    response = requests.get(f"{external_server_url}/speakers_list")
                    if response.status_code == 200:
                        speakers = response.json()
                        self.speaker_dropdown.configure(values=speakers)
                        if speakers:
                            self.selected_speaker.set(speakers[0])
                    else:
                        messagebox.showerror("Error", f"Failed to fetch speakers from the external server. Status code: {response.status_code}")
                except requests.exceptions.RequestException as e:
                    messagebox.showerror("Error", f"Failed to connect to the external server: {str(e)}")
            else:
                try:
                    response = requests.get("http://localhost:8020/speakers_list")
                    if response.status_code == 200:
                        speakers = response.json()
                        self.speaker_dropdown.configure(values=speakers)
                        if speakers:
                            self.selected_speaker.set(speakers[0])
                    else:
                        messagebox.showerror("Error", f"Failed to fetch speakers from the local server. Status code: {response.status_code}")
                except requests.exceptions.RequestException as e:
                    messagebox.showerror("Error", f"Failed to connect to the local server: {str(e)}")
        elif self.tts_service.get() == "VoiceCraft":
            voicecraft_voices_folder = os.path.join(self.tts_voices_folder, "VoiceCraft")
            wav_files = [f for f in os.listdir(voicecraft_voices_folder) if f.endswith(".wav")]
            txt_files = [f for f in os.listdir(voicecraft_voices_folder) if f.endswith(".txt")]
            speakers = [os.path.splitext(f)[0] for f in sorted(wav_files) if os.path.splitext(f)[0] + ".txt" in txt_files]
            self.speaker_dropdown.configure(values=speakers)
            if speakers:
                self.selected_speaker.set(speakers[0])
        else:  # Silero
            try:
                language_name = self.language_var.get()  # Use self.language_var.get() instead of self.language.get()
                language_code = next((lang["code"] for lang in silero_languages if lang["name"] == language_name), None)
                if language_code:
                    response = requests.get(f"http://localhost:8001/tts/speakers")
                    if response.status_code == 200:
                        speakers = [speaker["name"] for speaker in response.json()]
                        self.speaker_dropdown.configure(values=speakers)
                        if speakers:
                            self.selected_speaker.set(speakers[0])
                    else:
                        messagebox.showerror("Error", "Failed to fetch Silero speakers.")
                else:
                    messagebox.showerror("Error", "Invalid language selected.")
            except requests.exceptions.ConnectionError:
                messagebox.showerror("Error", "Failed to connect to the Silero API.")

    def on_language_selected(self, *args):
        print("on_language_selected method called")
        selected_language = self.language_var.get()

        if self.tts_service.get() == "Silero":
            selected_language_code = next((lang["code"] for lang in silero_languages if lang["name"] == selected_language), None)
            if selected_language_code:
                try:
                    response = requests.post("http://localhost:8001/tts/language", json={"id": selected_language_code})
                    if response.status_code == 200:
                        self.populate_speaker_dropdown()
                    else:
                        messagebox.showerror("Error", "Failed to set Silero language.")
                except requests.exceptions.ConnectionError:
                    messagebox.showerror("Error", "Failed to connect to the Silero API.")

    def upload_speaker_voice(self):
        if self.tts_service.get() == "VoiceCraft":
            wav_file = filedialog.askopenfilename(filetypes=[("WAV files", "*.wav")])
            txt_file = filedialog.askopenfilename(filetypes=[("Text files", "*.txt")])
            if wav_file and txt_file:
                speaker_name = os.path.splitext(os.path.basename(wav_file))[0]
                voicecraft_folder = os.path.join(self.tts_voices_folder, "VoiceCraft")
                os.makedirs(voicecraft_folder, exist_ok=True)
                wav_destination_path = os.path.join(voicecraft_folder, f"{speaker_name}.wav")
                txt_destination_path = os.path.join(voicecraft_folder, f"{speaker_name}.txt")
                shutil.copy(wav_file, wav_destination_path)
                shutil.copy(txt_file, txt_destination_path)
                self.populate_speaker_dropdown()  # Refresh the speaker dropdown after uploading
                CTkMessagebox(title="Speaker Voice Uploaded", message=f"The speaker voice '{speaker_name}' has been uploaded successfully.", icon="info")
        else:
            wav_file = filedialog.askopenfilename(filetypes=[("WAV files", "*.wav")])
            if wav_file:
                speaker_name = os.path.splitext(os.path.basename(wav_file))[0]
                destination_path = os.path.join(self.tts_voices_folder, f"{speaker_name}.wav")
                shutil.copy(wav_file, destination_path)
                self.populate_speaker_dropdown()  # Refresh the speaker dropdown after uploading
                CTkMessagebox(title="Speaker Voice Uploaded", message=f"The speaker voice '{speaker_name}' has been uploaded successfully.", icon="info")

    def new_session(self):
        new_session_name = ctk.CTkInputDialog(text="Enter a name for the new session:", title="New Session").get_input()
        if new_session_name:
            if self.session_name_exists(new_session_name):
                overwrite = messagebox.askyesno("Session Exists", f"A session with the name '{new_session_name}' already exists. Do you want to overwrite it?")
                if not overwrite:
                    return
                else:
                    # Clear the existing session data
                    session_dir = os.path.join("Outputs", new_session_name)
                    json_filename = os.path.join(session_dir, f"{new_session_name}_sentences.json")
                    if os.path.exists(json_filename):
                        os.remove(json_filename)
                    sentence_wavs_dir = os.path.join(session_dir, "Sentence_wavs")
                    if os.path.exists(sentence_wavs_dir):
                        shutil.rmtree(sentence_wavs_dir)
                        os.makedirs(sentence_wavs_dir)  # Re-create the empty "Sentence_wavs" folder
                    self.playlist_listbox.delete(0, tk.END)
                    self.sentence_audio_data.clear()
                    self.progress_bar.set(0)
                    self.remaining_time_label.configure(text="N/A")

            self.session_name.set(new_session_name)
            self.session_name_label.configure(text=new_session_name)  # Update the session name label
            self.playlist_listbox.delete(0, tk.END)
            self.source_file = ""
            self.selected_file_label.configure(text="No file selected")  # Reset the selected file label
            self.progress_bar.set(0)
            self.remaining_time_label.configure(text="N/A")
            
            # Reset the stop_flag when creating a new session
            self.stop_flag = False

            # Copy the pre-selected source file to the new session folder
            if self.pre_selected_source_file:
                session_dir = os.path.join("Outputs", new_session_name)
                os.makedirs(session_dir, exist_ok=True)
                file_name = os.path.basename(self.pre_selected_source_file)
                destination_path = os.path.join(session_dir, file_name)
                shutil.copy(self.pre_selected_source_file, destination_path)
                self.source_file = destination_path
                self.selected_file_label.configure(text=file_name)

    def stop_generation(self):
        self.stop_flag = True
        CTkMessagebox(title="Generation Stopped", message="The generation process will stop after completing the current sentence.", icon="info")

    def resume_generation(self):
        if self.session_name.get():
            if self.tts_service.get() == "XTTS":
                self.apply_xtts_settings_silently()

            session_dir = f"Outputs/{self.session_name.get()}"
            json_filename = os.path.join(session_dir, f"{self.session_name.get()}_sentences.json")
            if os.path.exists(json_filename):
                preprocessed_sentences = self.load_json(json_filename)
                total_sentences = len(preprocessed_sentences)
                current_sentence = next((i for i, s in enumerate(preprocessed_sentences) if s.get("tts_generated") == "no"), total_sentences)
                self.stop_flag = False

                # Create a new thread for the optimization process
                self.optimization_thread = threading.Thread(target=self.start_optimisation, args=(total_sentences, current_sentence))
                self.optimization_thread.start()
            else:
                CTkMessagebox(title="Error", message="Session JSON file not found.", icon="cancel")
        else:
            CTkMessagebox(title="Error", message="Please load a session before resuming generation.", icon="cancel")

    def cancel_generation(self):
        if hasattr(self, 'optimization_thread') and self.optimization_thread.is_alive():
            # Set a flag to indicate that the generation should be canceled
            self.cancel_flag = True

            # Interrupt the thread
            res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(self.optimization_thread.ident), ctypes.py_object(SystemExit))
            if res > 1:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(self.optimization_thread.ident, 0)
                print('Exception raise failure')

            # Stop the playback before deleting files
            self.stop_playback()
            pygame.mixer.quit()  # Quit the pygame mixer
            time.sleep(1)  # Wait for a short duration before deleting files

            try:
                # Remove the JSON file and WAV files inside the "Sentence_wavs" folder
                session_dir = os.path.join("Outputs", self.session_name.get())
                json_filename = os.path.join(session_dir, f"{self.session_name.get()}_sentences.json")
                sentence_wavs_dir = os.path.join(session_dir, "Sentence_wavs")

                if os.path.exists(json_filename):
                    os.remove(json_filename)

                if os.path.exists(sentence_wavs_dir):
                    shutil.rmtree(sentence_wavs_dir)
                    os.makedirs(sentence_wavs_dir)  # Re-create the empty "Sentence_wavs" folder

                # Clear the playlist
                self.playlist_listbox.delete(0, tk.END)
                self.sentence_audio_data.clear()

                # Reset progress bar and remaining time label
                self.progress_bar.set(0)
                self.remaining_time_label.configure(text="N/A")

                CTkMessagebox(title="Generation Canceled", message="The generation process has been canceled, and the session JSON file and WAV files have been removed.", icon="info")
            except Exception as e:
                CTkMessagebox(title="Cancellation Error", message=f"An error occurred while canceling the generation: {str(e)}", icon="cancel")
        else:
            CTkMessagebox(title="No Generation in Progress", message="There is no generation process currently running.", icon="info")

    def delete_session(self):
        if hasattr(self, 'optimization_thread') and self.optimization_thread.is_alive():
            self.delete_session_flag = True  # Set the delete_session_flag
            answer = messagebox.askyesno("Generation in Progress", "A generation is currently in progress. Do you want to stop it and delete the session?")
            if answer:
                self.stop_playback()  # Stop the playback before deleting the session
                self.show_delete_confirmation()
            else:
                self.delete_session_flag = False  # Reset the flag if the user cancels
                return
        else:
            self.stop_playback()  # Stop the playback before deleting the session
            self.show_delete_confirmation()

    def show_delete_confirmation(self):
        session_name = self.session_name.get()
        if session_name:
            messagebox = CTkMessagebox(title="Confirm Delete", message=f"Are you sure you want to delete the session '{session_name}'?", option_1="Yes", option_2="No")
            response = messagebox.get()

            if response == "Yes":
                self.delete_session_files()
                self.master.after(0, self.reset_ui_elements)  # Call reset_ui_elements after the message box is closed
            else:
                return
        else:
            messagebox.showinfo("No Session", "There is no session to delete.")

    def delete_session_files(self):
        session_name = self.session_name.get()
        if session_name:
            session_dir = os.path.join("Outputs", session_name)
            if os.path.exists(session_dir):
                # Check if the mixer is initialized
                if pygame.mixer.get_init() is not None:
                    self.stop_playback()  # Stop any ongoing playback
                    pygame.mixer.quit()  # Quit the pygame mixer
                    time.sleep(1)  # Wait for a short duration before deleting files

                try:
                    shutil.rmtree(session_dir)  # Delete the session directory and its contents
                    CTkMessagebox(title="Session Deleted", message=f"The session '{session_name}' has been deleted.", icon="info")
                except Exception as e:
                    CTkMessagebox(title="Deletion Error", message=f"An error occurred while deleting the session: {str(e)}", icon="cancel")

    def reset_ui_elements(self):
        self.session_name.set("")
        self.session_name_label.configure(text="Untitled Session")
        self.selected_file_label.configure(text="No file selected")
        self.playlist_listbox.delete(0, tk.END)
        self.sentence_audio_data.clear()
        self.progress_bar.set(0)
        self.remaining_time_label.configure(text="N/A")

    def load_models(self):
        try:
            response = requests.get("http://127.0.0.1:5000/v1/internal/model/list")
            if response.status_code == 200:
                model_names = response.json()["model_names"]
                model_names.sort()  # Sort the model names in alphabetical order

                # Update the dropdown values and pre-select the first model
                self.first_prompt_model_dropdown.configure(values=model_names)
                self.second_prompt_model_dropdown.configure(values=model_names)
                self.third_prompt_model_dropdown.configure(values=model_names)

                if model_names:
                    self.first_prompt_model.set(model_names[0])
                    self.second_prompt_model.set(model_names[0])
                    self.third_prompt_model.set(model_names[0])

                CTkMessagebox(title="Models Loaded", message="LLM models loaded successfully.", icon="info")
            else:
                CTkMessagebox(title="Error", message="Failed to load models from the API.", icon="cancel")
        except requests.exceptions.ConnectionError:
            CTkMessagebox(title="Error", message="Failed to connect to the LLM API.", icon="cancel")
 
    def update_tts_service(self, event=None):
        if not hasattr(self, 'xtts_advanced_settings_frame'):
            self.create_xtts_advanced_settings_frame()

        if self.tts_service.get() == "XTTS":
            self.connect_to_server_button.grid()
            self.use_external_server_switch.grid()
            if self.use_external_server.get():
                self.external_server_url_entry.grid()
            else:
                self.external_server_url_entry.grid_remove()
            self.use_external_server_voicecraft_switch.grid_remove()
            self.external_server_url_entry_voicecraft.grid_remove()
            self.voicecraft_model_dropdown.grid_remove()
            self.voicecraft_model_label.grid_remove()
            self.advanced_settings_switch.grid()  # Show advanced settings for XTTS
            self.xtts_advanced_settings_frame.grid_remove()  # Hide XTTS advanced settings initially
            self.xtts_model_label.grid()
            self.xtts_model_dropdown.grid()

        elif self.tts_service.get() == "VoiceCraft":
            self.connect_to_server_button.grid()
            self.use_external_server_switch.grid_remove()
            self.external_server_url_entry.grid_remove()
            self.use_external_server_voicecraft_switch.grid()
            if self.use_external_server_voicecraft.get():
                self.external_server_url_entry_voicecraft.grid()
            else:
                self.external_server_url_entry_voicecraft.grid_remove()
            self.voicecraft_model_dropdown.grid()
            self.voicecraft_model_label.grid()
            self.advanced_settings_switch.grid()  # Show advanced settings for VoiceCraft
            self.xtts_model_label.grid_remove()
            self.xtts_model_dropdown.grid_remove()

        else:  # Silero
            self.connect_to_server_button.grid_remove()
            self.use_external_server_switch.grid_remove()
            self.external_server_url_entry.grid_remove()
            self.use_external_server_voicecraft_switch.grid_remove()
            self.external_server_url_entry_voicecraft.grid_remove()
            self.external_server_connected = False
            self.external_server_connected_voicecraft = False
            self.voicecraft_model_dropdown.grid_remove()
            self.voicecraft_model_label.grid_remove()
            self.advanced_settings_switch.grid_remove()  # Hide advanced settings for Silero
            self.xtts_model_label.grid_remove()
            self.xtts_model_dropdown.grid_remove()

        self.update_language_dropdown()

    def populate_xtts_models(self):
        try:
            if self.use_external_server.get() and self.external_server_connected:
                url = f"{self.external_server_url.get()}/get_models_list"
            else:
                url = "http://localhost:8020/get_models_list"
            
            response = requests.get(url)
            if response.status_code == 200:
                models = response.json()
                self.xtts_model_dropdown.configure(values=models)
                if models:
                    self.xtts_model.set(models[0])
                    self.switch_xtts_model(models[0])
            else:
                messagebox.showerror("Error", f"Failed to fetch XTTS models. Status code: {response.status_code}")
        except requests.exceptions.RequestException as e:
            messagebox.showerror("Error", f"Failed to fetch XTTS models: {str(e)}")

    def switch_xtts_model(self, model_name):
        try:
            if self.use_external_server.get() and self.external_server_connected:
                url = f"{self.external_server_url.get()}/switch_model"
            else:
                url = "http://localhost:8020/switch_model"
            
            data = {"model_name": model_name}
            response = requests.post(url, json=data)
            if response.status_code == 200:
                print(f"Switched to XTTS model: {model_name}")
            elif response.status_code == 400:
                response_json = response.json()
                if "detail" in response_json and "already loaded in memory" in response_json["detail"]:
                    print(f"XTTS model {model_name} is already loaded.")
                else:
                    print(f"Failed to switch XTTS model. Status code: {response.status_code}")
                    print(f"Response: {response.text}")
            else:
                print(f"Failed to switch XTTS model. Status code: {response.status_code}")
                print(f"Response: {response.text}")
        except requests.exceptions.RequestException as e:
            print(f"Failed to switch XTTS model: {str(e)}")

    def update_speaker_dropdown_state(self):
        if self.is_custom_model:
            self.speaker_dropdown.configure(state="disabled")
        else:
            self.speaker_dropdown.configure(state="normal")

    def on_xtts_model_change(self, model_name):
        self.switch_xtts_model(model_name)            

    def toggle_playback(self):
        if self.playing:
            if self.paused:
                if self.channel.get_busy():
                    self.channel.unpause()
                else:
                    pygame.mixer.music.unpause()
                self.paused = False
                self.master.after(10, self.update_play_button_text, "Pause")
                # Added to prevent immediately jumping to the next item in the playlist
                if not self.channel.get_busy():
                    # Ensure that the check_playlist_playback loop is restarted only if we are in playlist mode and not just playing a single sentence
                    self.master.after(100, self.check_playlist_playback)
            else:
                if self.channel.get_busy():
                    self.channel.pause()
                else:
                    pygame.mixer.music.pause()
                self.paused = True
                self.master.after(10, self.update_play_button_text, "Resume")
        else:  # self.playing is False
            self.play_selected_sentence()
            self.playing = True  # Set self.playing to True
            self.paused = False  # Set self.paused to False
            self.master.after(10, self.update_play_button_text, "Pause")

    def remove_selected_sentences(self):
        session_name = self.session_name.get()
        session_directory = os.path.join("Outputs", session_name)
        json_filename = os.path.join(session_directory, f"{session_name}_sentences.json")

        selected_indices = self.playlist_listbox.curselection()
        if not selected_indices:
            messagebox.showerror("Error", "Please select at least one sentence to remove.")
            return

        processed_sentences = self.load_json(json_filename)

        for index in reversed(selected_indices):
            sentence_text = self.playlist_listbox.get(index)
            sentence_index = self.playlist_listbox.get(0, tk.END).index(sentence_text)

            # Find the sentence in the processed_sentences list and remove it
            sentence_dict = next((s for s in processed_sentences if s["sentence_number"] == str(sentence_index + 1)), None)
            if sentence_dict:
                processed_sentences.remove(sentence_dict)

                # Delete the corresponding WAV file
                wav_filename = os.path.join(session_directory, "Sentence_wavs", f"{session_name}_sentence_{sentence_index + 1}.wav")
                if os.path.exists(wav_filename):
                    os.remove(wav_filename)

            self.playlist_listbox.delete(index)

        # Update the sentence numbers in the processed_sentences list
        for i, sentence_dict in enumerate(processed_sentences, start=1):
            sentence_dict["sentence_number"] = str(i)

        # Save the updated processed_sentences list to the JSON file
        self.save_json(processed_sentences, json_filename)

    def fetch_speakers_list(self):
        try:
            response = requests.get("http://localhost:8020/speakers_list")
            if response.status_code == 200:
                speakers = response.json()
                self.speaker_dropdown.configure(values=speakers)
                messagebox.showinfo("Speakers Loaded", "Speakers loaded successfully.")
            else:
                messagebox.showerror("Error", "Failed to fetch the list of speakers from the TTS server.")
        except requests.exceptions.ConnectionError:
            messagebox.showerror("Error", "Failed to connect to the TTS server.")

    def load_model(self, model_name):
        if model_name == "default":
            return

        url = "http://127.0.0.1:5000/v1/internal/model/load"
        headers = {"Content-Type": "application/json"}
        data = {
            "model_name": model_name,
            "args": {},
            "settings": {}
        }
        response = requests.post(url, headers=headers, json=data, verify=False)
        if response.status_code != 200:
            CTkMessagebox(title="Error", message=f"Failed to load model: {model_name}", icon="cancel")
            
    def load_json(self, filename):
        with open(filename, 'r') as f:
            data = json.load(f)
        return data

    def update_remaining_time_label(self, estimated_remaining_time):
        formatted_time = str(datetime.timedelta(seconds=int(estimated_remaining_time)))
        self.remaining_time_label.configure(text=f"{formatted_time}")

    def start_optimisation_thread(self):
        session_name = self.session_name.get()
        if not session_name:
            CTkMessagebox(title="Error", message="Please create or load a session first.", icon="cancel")
            return

        session_dir = os.path.join("Outputs", session_name)
        json_filename = os.path.join(session_dir, f"{session_name}_sentences.json")

        if not self.check_server_connection():
            return

        if self.enable_dubbing.get():
            # Call the new dubbing method
            self.generate_dubbing_audio()
        else:
            if os.path.exists(json_filename):
                # If JSON exists, start directly from TTS generation
                self.resume_generation()
            else:
                # If JSON doesn't exist, start from the beginning of the pipeline
                if not self.source_file:
                    CTkMessagebox(title="Error", message="Please select a source file.", icon="cancel")
                    return
                
                with open(self.source_file, 'r', encoding='utf-8') as file:
                    text = file.read()
                
                preprocessed_sentences = self.preprocess_text(text)
                os.makedirs(session_dir, exist_ok=True)
                self.save_json(preprocessed_sentences, json_filename)
                
                # Start the optimization process from the beginning
                total_sentences = len(preprocessed_sentences)
                self.optimization_thread = threading.Thread(target=self.start_optimisation, args=(total_sentences, 0))
                self.optimization_thread.start()

    def check_server_connection(self):
        try:
            if self.tts_service.get() == "XTTS":
                if self.use_external_server.get() and self.external_server_connected:
                    url = f"{self.external_server_url.get()}/docs"
                else:
                    url = "http://localhost:8020/docs"
            elif self.tts_service.get() == "VoiceCraft":
                if self.use_external_server_voicecraft.get() and self.external_server_connected_voicecraft:
                    url = f"{self.external_server_url_voicecraft.get()}/docs"
                else:
                    url = "http://localhost:8245/docs"
            else:  # Silero
                url = "http://localhost:8001/docs"

            response = requests.get(url)

            if response.status_code == 200:
                return True
            else:
                messagebox.showerror("Error", f"{self.tts_service.get()} server returned status code {response.status_code}. Cannot start generation.")
                return False
        except requests.exceptions.RequestException as e:
            if self.tts_service.get() == "XTTS" and self.use_external_server.get():
                messagebox.showerror("Error", f"Failed to connect to the external XTTS server:\n{str(e)}")
            elif self.tts_service.get() == "VoiceCraft" and self.use_external_server_voicecraft.get():
                messagebox.showerror("Error", f"Failed to connect to the external VoiceCraft server:\n{str(e)}")
            else:
                messagebox.showerror("Error", f"Failed to connect to {self.tts_service.get()} server:\n{str(e)}")
            return False

    def session_name_exists(self, session_name):
        session_dir = os.path.join("Outputs", session_name)
        return os.path.exists(session_dir)
        
    def pause_playback(self):
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.pause()
        else:
            pygame.mixer.music.unpause()

    def preprocess_text(self, text):
        if not self.source_file.endswith(".srt"):
            # Normalize newlines to LF and replace carriage returns with LF
            text = re.sub(r'\r\n?', '\n', text)

            paragraph_breaks = []  # Initialize paragraph_breaks as an empty list

            if not self.disable_paragraph_detection.get() and not self.source_file.endswith(".srt"):
                if self.pdf_preprocessed:
                    # For preprocessed PDFs, consider sentences followed by a single newline as paragraphs
                    paragraph_breaks = list(re.finditer(r'\n', text))
                elif not self.pdf_preprocessed and self.source_file.endswith(".pdf"):
                    # For raw PDF files, perform additional preprocessing
                    text = self.preprocess_text_pdf(text)
                elif self.source_file.endswith("_edited.txt"):
                    # For manually edited text, consider a single newline as a paragraph break
                    paragraph_breaks = list(re.finditer(r'\n', text))
                else:
                    # For regular text files, convert single newlines to spaces
                    text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)

                    # Mark sentences followed by a single newline as paragraph sentences
                    paragraph_breaks = list(re.finditer(r'\n', text))

            # Replace tabs with spaces
            text = re.sub(r'\t', ' ', text)

        if self.remove_diacritics.get():
            text = ''.join(char for char in text if not unicodedata.combining(char))
            text = unidecode(text)

        # Check if the source file is an srt file
        if self.source_file.endswith(".srt"):
            # Remove <b></b>, <i></i>, and <> tags from subtitle text
            text = re.sub(r'<[/]?[bi]>', '', text)
            text = re.sub(r'<>', '', text)
            # Parse the srt file and extract subtitle information
            subtitles = pysrt.open(self.source_file)

            processed_sentences = []
            for subtitle in subtitles:
                start_time = subtitle.start.to_time().strftime("%H:%M:%S.%f")
                end_time = subtitle.end.to_time().strftime("%H:%M:%S.%f")
                text = subtitle.text.replace("\n", " ")

                sentence_dict = {
                    "original_sentence": text,
                    "paragraph": "no",
                    "split_part": None,
                    "start": start_time,
                    "end": end_time,
                    "tts_generated": "no"
                }

                processed_sentences.append(sentence_dict)

            return processed_sentences
        else:
            # Additional preprocessing step to handle chapters, section titles, etc.
            text = re.sub(r'(^|\n+)([^\n.!?]+)(?=\n+|$)', r'\1\2.', text)

            # Use split_into_sentences method for sentence splitting
            sentences = self.split_into_sentences(text)

            processed_sentences = []

            for sentence in sentences:
                if not sentence.strip():  # Skip empty sentences
                    continue

                is_paragraph = False
                for match in paragraph_breaks:
                    preceding_text = text[match.start()-15:match.start()]
                    sentence_end = sentence[-15:]
                    if self.calculate_similarity(preceding_text, sentence_end) >= 0.8:
                        is_paragraph = True
                        break

                # Use num2words to convert digits to words for Silero
                if self.tts_service.get() == "Silero":
                    sentence = self.convert_digits_to_words(sentence)

                sentence_dict = {
                    "original_sentence": sentence,
                    "paragraph": "yes" if is_paragraph else "no",
                    "split_part": None  # Initialize split_part as None
                }

                # Split long sentences
                if self.enable_sentence_splitting.get():
                    split_sentences = self.split_long_sentences(sentence_dict)
                    processed_sentences.extend(split_sentences)
                else:
                    processed_sentences.append(sentence_dict)

            # Append short sentences
            if self.enable_sentence_appending.get():
                processed_sentences = self.append_short_sentences(processed_sentences)

            # Split long sentences recursively
            split_sentences = []
            for sentence_dict in processed_sentences:
                split_sentences.extend(self.split_long_sentences_2(sentence_dict))

            return split_sentences

    def toggle_advanced_tts_settings(self):
        if self.tts_service.get() == "VoiceCraft":
            if self.show_advanced_tts_settings.get():
                self.advanced_tts_settings_frame.grid()
            else:
                self.advanced_tts_settings_frame.grid_remove()
        elif self.tts_service.get() == "XTTS":
            if self.show_advanced_tts_settings.get():
                self.xtts_advanced_settings_frame.grid()
            else:
                self.xtts_advanced_settings_frame.grid_remove()
        else:
            self.advanced_tts_settings_frame.grid_remove()
            self.xtts_advanced_settings_frame.grid_remove()

    def create_xtts_advanced_settings_frame(self):
        self.xtts_advanced_settings_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        self.xtts_advanced_settings_frame.grid(row=6, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        self.xtts_advanced_settings_frame.grid_columnconfigure(0, weight=1)
        self.xtts_advanced_settings_frame.grid_columnconfigure(1, weight=1)

        # Add Stream Chunk Size
        ctk.CTkLabel(self.xtts_advanced_settings_frame, text="Stream Chunk Size:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.xtts_advanced_settings_frame, textvariable=self.xtts_stream_chunk_size).grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkLabel(self.xtts_advanced_settings_frame, text="Temperature:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.xtts_advanced_settings_frame, textvariable=self.xtts_temperature).grid(row=1, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkLabel(self.xtts_advanced_settings_frame, text="Length Penalty:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.xtts_advanced_settings_frame, textvariable=self.xtts_length_penalty).grid(row=2, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkLabel(self.xtts_advanced_settings_frame, text="Repetition Penalty:").grid(row=3, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.xtts_advanced_settings_frame, textvariable=self.xtts_repetition_penalty).grid(row=3, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkLabel(self.xtts_advanced_settings_frame, text="Top K:").grid(row=4, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.xtts_advanced_settings_frame, textvariable=self.xtts_top_k).grid(row=4, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkLabel(self.xtts_advanced_settings_frame, text="Top P:").grid(row=5, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.xtts_advanced_settings_frame, textvariable=self.xtts_top_p).grid(row=5, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkSwitch(self.xtts_advanced_settings_frame, text="Enable Text Splitting", variable=self.xtts_enable_text_splitting).grid(row=7, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)

        # Add the Apply button
        apply_button = ctk.CTkButton(self.xtts_advanced_settings_frame, text="Apply", command=self.apply_xtts_settings)
        apply_button.grid(row=8, column=0, columnspan=2, padx=5, pady=10, sticky=tk.EW)

        self.xtts_advanced_settings_frame.grid_remove()  # Hide the frame initially

    def apply_xtts_settings(self):
        settings = {
            "stream_chunk_size": int(self.xtts_stream_chunk_size.get()),
            "temperature": float(self.xtts_temperature.get()),
            "speed": float(self.xtts_speed.get()),
            "length_penalty": float(self.xtts_length_penalty.get()),
            "repetition_penalty": float(self.xtts_repetition_penalty.get()),
            "top_p": float(self.xtts_top_p.get()),
            "top_k": int(self.xtts_top_k.get()),
            "enable_text_splitting": self.xtts_enable_text_splitting.get()
        }

        try:
            if self.use_external_server.get() and self.external_server_connected:
                url = f"{self.external_server_url.get()}/set_tts_settings"
            else:
                url = "http://localhost:8020/set_tts_settings"

            response = requests.post(url, json=settings)
            if response.status_code == 200:
                messagebox.showinfo("Success", "XTTS settings updated successfully.")
            else:
                messagebox.showerror("Error", f"Failed to update XTTS settings. Status code: {response.status_code}")
        except requests.exceptions.RequestException as e:
            messagebox.showerror("Error", f"Failed to connect to the XTTS server: {str(e)}")

    def apply_xtts_settings_silently(self):
        settings = {
            "stream_chunk_size": int(self.xtts_stream_chunk_size.get()),
            "temperature": float(self.xtts_temperature.get()),
            "speed": float(self.xtts_speed.get()),
            "length_penalty": float(self.xtts_length_penalty.get()),
            "repetition_penalty": float(self.xtts_repetition_penalty.get()),
            "top_p": float(self.xtts_top_p.get()),
            "top_k": int(self.xtts_top_k.get()),
            "enable_text_splitting": self.xtts_enable_text_splitting.get()
        }

        try:
            if self.use_external_server.get() and self.external_server_connected:
                url = f"{self.external_server_url.get()}/set_tts_settings"
            else:
                url = "http://localhost:8020/set_tts_settings"

            response = requests.post(url, json=settings)
            if response.status_code != 200:
                logging.error(f"Failed to update XTTS settings. Status code: {response.status_code}")
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to connect to the XTTS server: {str(e)}")

    def update_speed_label(self, value):
        self.speed_value_label.configure(text=f"Speed: {float(value):.2f}")

    def convert_digits_to_words(self, sentence):

        def replace_numbers(match):
            number = match.group(0)
            try:
                # Get the selected Silero language
                silero_language_name = self.language_var.get()
                
                # Map Silero language names to num2words language codes
                silero_to_num2words_lang = {
                    "German (v3)": "de",
                    "English (v3)": "en",
                    "English Indic (v3)": "en",
                    "Spanish (v3)": "es",
                    "French (v3)": "fr",
                    "Indic (v3)": "hi",
                    "Russian (v3.1)": "ru",
                    "Tatar (v3)": "tt",
                    "Ukrainian (v3)": "uk",
                    "Uzbek (v3)": "uz",
                    "Kalmyk (v3)": "xal"
                }

                # Get the corresponding num2words language code
                num2words_lang = silero_to_num2words_lang.get(silero_language_name, "en")

                return num2words(int(number), lang=num2words_lang)
            except ValueError:
                return number

        return re.sub(r'\d+', replace_numbers, sentence)

    def synchronize_audio(self, processed_sentences, session_name):
        final_audio = AudioSegment.empty()
        current_time = datetime.datetime.strptime("00:00:00.000", "%H:%M:%S.%f")
        session_dir = os.path.join("Outputs", session_name)

        for sentence_dict in processed_sentences:
            sentence_number = int(sentence_dict["sentence_number"])
            wav_filename = os.path.join(session_dir, "Sentence_wavs", f"{session_name}_sentence_{sentence_number}.wav")
            if os.path.exists(wav_filename):
                audio_data = AudioSegment.from_file(wav_filename, format="wav")

                start_time = sentence_dict["start"]
                end_time = sentence_dict["end"]

                # Parse the start and end times
                start_time_obj = datetime.datetime.strptime(start_time, "%H:%M:%S.%f")
                end_time_obj = datetime.datetime.strptime(end_time, "%H:%M:%S.%f")

                # Get the actual duration of the sped-up audio file
                generated_audio_duration = len(audio_data) / 1000

                # Check if the current time is ahead of the subtitle's start time
                if current_time > start_time_obj:
                    # The audio should start immediately
                    final_audio += audio_data
                    current_time += datetime.timedelta(seconds=generated_audio_duration)
                else:
                    # Calculate the silence duration needed to match the subtitle's start time
                    silence_duration = (start_time_obj - current_time).total_seconds() * 1000
                    if silence_duration > 0:
                        silence = AudioSegment.silent(duration=silence_duration)
                        final_audio += silence
                    final_audio += audio_data
                    current_time = start_time_obj + datetime.timedelta(seconds=generated_audio_duration)

        return final_audio


    def split_long_sentences(self, sentence_dict):

        sentence = sentence_dict["original_sentence"]

        paragraph = sentence_dict["paragraph"]



        if len(sentence) <= self.max_sentence_length.get():

            return [{"original_sentence": sentence, "split_part": None, "paragraph": paragraph}]



        # Check if the language is set to Chinese

        if self.language_var.get() == "zh-cn":

            # Chinese full-width punctuation marks

            punctuation_marks = ['，', '；', '：', '。', '！', '？']

            min_distance = 10  # Adjusted for Chinese characters

        else:

            # Original punctuation for other languages

            punctuation_marks = [',', ':', ';', '–']

            conjunction_marks = [' and ', ' or ', 'which']

            min_distance = 30



        best_split_index = None

        min_diff = float('inf')



        for mark in punctuation_marks:

            indices = [i for i, c in enumerate(sentence) if c == mark]

            for index in indices:

                if min_distance <= index <= len(sentence) - min_distance:

                    # Check if the comma is not between two digits (avoiding splitting numbers like 28,000)

                    if not (mark == ',' and index > 0 and index < len(sentence) - 1 and 

                            sentence[index-1].isdigit() and sentence[index+1].isdigit()):

                        diff = abs(index - len(sentence) // 2)

                        if diff < min_diff:

                            min_diff = diff

                            best_split_index = index + 1

        if best_split_index is None and self.language_var.get() != "zh-cn":

            # Only check for conjunctions in non-Chinese text

            for mark in conjunction_marks:

                index = sentence.find(mark)

                if min_distance <= index <= len(sentence) - min_distance:

                    best_split_index = index

                    break



        if best_split_index is None:

            return [{"original_sentence": sentence, "split_part": None, "paragraph": paragraph}]





        first_part = sentence[:best_split_index].strip()

        second_part = sentence[best_split_index:].strip()



        return [

            {"original_sentence": first_part, "split_part": 0, "paragraph": "no"},

            {"original_sentence": second_part, "split_part": 1, "paragraph": paragraph}

        ]



    def split_long_sentences_2(self, sentence_dict):

        sentence = sentence_dict["original_sentence"]

        paragraph = sentence_dict["paragraph"]

        split_part = sentence_dict["split_part"]



        if len(sentence) <= self.max_sentence_length.get():

            return [sentence_dict]



        # Check if the language is set to Chinese

        if self.language_var.get() == "zh-cn":

            # Chinese full-width punctuation marks

            punctuation_marks = ['，', '；', '：', '。', '！', '？']

            min_distance = 10  # Adjusted for Chinese characters

        else:

            # Original punctuation for other languages

            punctuation_marks = [',', ':', ';', '–']

            conjunction_marks = [' and ', ' or ', 'which']

            min_distance = 30



        best_split_index = None

        min_diff = float('inf')



        for mark in punctuation_marks:

            indices = [i for i, c in enumerate(sentence) if c == mark]

            for index in indices:

                if min_distance <= index <= len(sentence) - min_distance:

                    # For non-Chinese, check if the comma is not between two digits

                    if self.language_var.get() != "zh-cn" and mark == ',':

                        if index > 0 and index < len(sentence) - 1 and sentence[index-1].isdigit() and sentence[index+1].isdigit():

                            continue

                    diff = abs(index - len(sentence) // 2)

                    if diff < min_diff:

                        min_diff = diff

                        best_split_index = index + 1



        if best_split_index is None and self.language_var.get() != "zh-cn":

            # Only check for conjunctions in non-Chinese text

            for mark in conjunction_marks:

                index = sentence.find(mark)

                if min_distance <= index <= len(sentence) - min_distance:

                    best_split_index = index

                    break



        if best_split_index is None:

            return [sentence_dict]





        first_part = sentence[:best_split_index].strip()

        second_part = sentence[best_split_index:].strip()



        split_sentences = []

        if split_part is None:

            split_part_prefix = "0"

        else:

            split_part_prefix = str(split_part)



        split_sentences.append({

            "original_sentence": first_part,

            "split_part": split_part_prefix + "a",

            "paragraph": "no"

        })



        if len(second_part) > self.max_sentence_length.get():

            if split_part_prefix == "0" and paragraph == "yes":

                split_sentences.extend(self.split_long_sentences_2({

                    "original_sentence": second_part,

                    "split_part": "1a",

                    "paragraph": "yes"

                }))

            else:

                split_sentences.extend(self.split_long_sentences_2({

                    "original_sentence": second_part,

                    "split_part": split_part_prefix + "b",

                    "paragraph": "no" if split_part_prefix == "0" else paragraph

                }))

        else:

            split_sentences.append({

                "original_sentence": second_part,

                "split_part": split_part_prefix + "b",

                "paragraph": paragraph

            })



        return split_sentences

    def append_short_sentences(self, sentence_dicts):
        appended_sentences = []
        i = 0
        while i < len(sentence_dicts):
            current_sentence = sentence_dicts[i]

            if current_sentence["paragraph"] == "no":
                if i > 0:
                    prev_sentence = appended_sentences[-1]
                    if prev_sentence["paragraph"] == "no":
                        combined_text = prev_sentence["original_sentence"] + ' ' + current_sentence["original_sentence"]
                        if len(combined_text) <= self.max_sentence_length.get():
                            prev_sentence["original_sentence"] = combined_text
                            i += 1
                            continue
                if i < len(sentence_dicts) - 1:
                    next_sentence = sentence_dicts[i + 1]
                    combined_text = current_sentence["original_sentence"] + ' ' + next_sentence["original_sentence"]
                    if len(combined_text) <= self.max_sentence_length.get():
                        current_sentence["original_sentence"] = combined_text
                        if next_sentence["paragraph"] == "yes":
                            current_sentence["paragraph"] = "yes"
                        i += 2
                        appended_sentences.append(current_sentence)
                        continue
            else:  # current_sentence["paragraph"] == "yes"
                if i > 0:
                    prev_sentence = appended_sentences[-1]
                    if prev_sentence["paragraph"] == "no":
                        combined_text = prev_sentence["original_sentence"] + ' ' + current_sentence["original_sentence"]
                        if len(combined_text) <= self.max_sentence_length.get():
                            prev_sentence["original_sentence"] = combined_text
                            prev_sentence["paragraph"] = "yes"
                            i += 1
                            continue

            appended_sentences.append(current_sentence)
            i += 1

        return appended_sentences

    def start_optimisation(self, total_sentences, current_sentence=0):
        if self.tts_service.get() == "XTTS":
            self.apply_xtts_settings_silently()
        session_name = self.session_name.get()
        session_dir = f"Outputs/{session_name}"
        os.makedirs(session_dir, exist_ok=True)
        os.makedirs(os.path.join(session_dir, "Sentence_wavs"), exist_ok=True)

        json_filename = os.path.join(session_dir, f"{self.session_name.get()}_sentences.json")

        # Initialize lists to store sentence generation times and MOS scores
        sentence_generation_times = []
        mos_scores = []

        # Reset the cancel_flag and delete_session_flag at the start of the generation
        self.cancel_flag = False
        self.delete_session_flag = False

        for sentence_index in range(current_sentence, total_sentences):
            while self.paused:
                time.sleep(1)

            # Check if the generation should be canceled
            if self.cancel_flag:
                break

            # Check if the generation should be deleted
            if self.delete_session_flag:
                messagebox.showinfo("Generation Stopped", "The generation process has been stopped due to session deletion.")
                break

            # Check if the generation should be stopped
            if self.stop_flag:
                messagebox.showinfo("Generation Stopped", "The generation process has been stopped.")
                break

            # Load the preprocessed_sentences from the JSON file
            preprocessed_sentences = self.load_json(json_filename)

            # Check if the sentence has already been processed
            if sentence_index < len(preprocessed_sentences) and preprocessed_sentences[sentence_index].get("tts_generated") == "yes":
                current_sentence += 1
                continue

            sentence_start_time = time.time()

            try:
                # If the sentence index is out of range, create a new sentence_dict
                if sentence_index >= len(preprocessed_sentences):
                    sentence_dict = {
                        "sentence_number": str(sentence_index + 1),
                        "tts_generated": "no"
                    }
                else:
                    sentence_dict = preprocessed_sentences[sentence_index]

                if self.source_file.endswith(".srt"):
                    # Disable sentence splitting, appending, and silence appending for srt files
                    self.enable_sentence_splitting.set(False)
                    self.enable_sentence_appending.set(False)
                    self.silence_length.set(0)
                    self.paragraph_silence_length.set(0)

                # Optimize the sentence
                processed_sentence = self.optimise_sentence(sentence_dict, sentence_index, session_dir)

                if processed_sentence is None or processed_sentence["original_sentence"] == "":
                    current_sentence += 1
                    continue

                # Update the processed_sentence in the current sentence_dict
                if self.enable_llm_processing.get() and "processed_sentence" in processed_sentence:
                    sentence_dict["processed_sentence"] = processed_sentence["processed_sentence"]

                # Save the updated sentence_dict to the JSON file
                self.save_sentence_to_json(preprocessed_sentences, json_filename, sentence_index, sentence_dict)

                best_audio = None
                best_mos = -1

                for attempt in range(self.max_attempts.get()):
                    # Generate audio for the processed sentence
                    if self.enable_llm_processing.get() and "processed_sentence" in processed_sentence:
                        audio_data = self.tts_to_audio(processed_sentence["processed_sentence"])
                    else:
                        audio_data = self.tts_to_audio(processed_sentence["original_sentence"])

                    if audio_data is not None:
                        if self.enable_tts_evaluation.get():
                            if self.enable_llm_processing.get() and "processed_sentence" in processed_sentence:
                                mos_score = self.evaluate_tts(processed_sentence["processed_sentence"], audio_data)
                            else:
                                mos_score = self.evaluate_tts(processed_sentence["original_sentence"], audio_data)
                            if mos_score is not None:
                                mos_scores.append(mos_score)
                                if mos_score > best_mos:
                                    best_audio = audio_data
                                    best_mos = mos_score

                                if mos_score >= float(self.target_mos_value.get()):
                                    break
                        else:
                            best_audio = audio_data
                            break
                    else:
                        print(f"Error generating audio for sentence: {processed_sentence['original_sentence']}")

                if best_audio is not None:                  
                    # Apply RVC if enabled
                    if self.enable_rvc.get():
                        best_audio = self.process_with_rvc(best_audio)

                    # Apply fade in/out if enabled
                    if self.enable_fade.get():
                        best_audio = self.apply_fade(best_audio, self.fade_in_duration.get(), self.fade_out_duration.get())

                    if not self.source_file.endswith(".srt"):
                        if processed_sentence.get("paragraph", "no") == "yes":
                            silence_length = self.paragraph_silence_length.get()
                        elif processed_sentence.get("split_part") is not None:
                            if isinstance(processed_sentence.get("split_part"), str):
                                if processed_sentence.get("split_part") in ["0a", "0b", "1a"]:
                                    silence_length = self.silence_length.get() // 4
                                elif processed_sentence.get("split_part") == "1b":
                                    silence_length = self.silence_length.get()
                            elif isinstance(processed_sentence.get("split_part"), int):
                                if processed_sentence.get("split_part") == 0:
                                    silence_length = self.silence_length.get() // 4
                                elif processed_sentence.get("split_part") == 1:
                                    silence_length = self.silence_length.get()
                        else:
                            silence_length = self.silence_length.get()

                        if silence_length > 0:
                            best_audio += AudioSegment.silent(duration=silence_length)

                    # Update the playlist in the GUI
                    self.master.after(0, self.update_playlist, processed_sentence)

                    # Save the individual sentence WAV file
                    sentence_output_filename = os.path.join(session_dir, "Sentence_wavs", f"{session_name}_sentence_{processed_sentence['sentence_number']}.wav")
                    best_audio.export(sentence_output_filename, format="wav")

                    # Update the tts_generated flag for the current sentence in the preprocessed_sentences list
                    sentence_dict["tts_generated"] = "yes"

            except Exception as e:
                print(f"Error processing sentence: {str(e)}")

            sentence_end_time = time.time()
            sentence_processing_time = sentence_end_time - sentence_start_time

            # Add the sentence processing time to the list
            sentence_generation_times.append(sentence_processing_time)

            current_sentence += 1

            # Update the progress bar
            progress_percentage = (current_sentence / total_sentences) * 100
            self.master.after(0, self.update_progress_bar, progress_percentage)

            # Calculate the estimated remaining time based on the average generation time of preceding sentences
            if sentence_generation_times:
                average_generation_time = sum(sentence_generation_times) / len(sentence_generation_times)
                estimated_remaining_time = (total_sentences - current_sentence) * average_generation_time
            else:
                estimated_remaining_time = 0

            # Update the remaining time label
            self.master.after(0, self.update_remaining_time_label, estimated_remaining_time)
    # Save the final concatenated audio file only if the source file is not an srt file
        if not self.source_file.endswith(".srt"):
            session_name = self.session_name.get()
            output_format = self.output_format.get()
            bitrate = self.bitrate.get()

            session_dir = os.path.join("Outputs", session_name)
            output_path = os.path.join(session_dir, f"{session_name}.{output_format}")

            wav_files = []
            for sentence_dict in preprocessed_sentences:
                sentence_number = int(sentence_dict["sentence_number"])
                wav_filename = os.path.join(session_dir, "Sentence_wavs", f"{session_name}_sentence_{sentence_number}.wav")
                if os.path.exists(wav_filename):
                    wav_files.append(wav_filename)

            with tempfile.NamedTemporaryFile(mode="w", delete=False) as temp_file:
                for wav_file in wav_files:
                    temp_file.write(f"file '{os.path.abspath(wav_file)}'\n")
                input_list_path = temp_file.name

            ffmpeg_command = [
                "ffmpeg",
                "-f", "concat",
                "-safe", "0",
                "-i", input_list_path,
                "-y"  # '-y' option to overwrite output files without asking
            ]

            # Adjust the codec and bitrate based on the output format
            if output_format == "wav":
                ffmpeg_command.extend(["-c:a", "pcm_s16le"])
            elif output_format == "mp3":
                ffmpeg_command.extend(["-c:a", "libmp3lame", "-b:a", bitrate])
            elif output_format == "opus":
                ffmpeg_command.extend(["-c:a", "libopus", "-b:a", bitrate])

            # Append the output path without quotes
            ffmpeg_command.append(output_path)

            print("FFmpeg Command:")
            print(" ".join(ffmpeg_command))

            try:
                subprocess.run(ffmpeg_command, check=True, stderr=subprocess.PIPE, universal_newlines=True)
                logging.info(f"The output file has been saved as {output_path}")
            except subprocess.CalledProcessError as e:
                error_message = f"FFmpeg exited with a non-zero code: {e.returncode}\n\nError output:\n{e.stderr}"
                logging.error(error_message)
            except Exception as e:
                error_message = f"An unexpected error occurred: {str(e)}"
                logging.error(error_message)
            finally:
                if os.path.exists(input_list_path):
                    os.remove(input_list_path)
        # Check if dubbing is enabled and the source file is an SRT file
        if self.enable_dubbing.get() and self.source_file.endswith(".srt"):
            self.start_dubbing()

        # Calculate the total generation time
        total_generation_time = sum(sentence_generation_times)
        formatted_time = str(datetime.timedelta(seconds=int(total_generation_time)))

        # Display the message box with generation information
        CTkMessagebox(title="Generation Finished", message=f"Generation completed!\n\nTotal Generation Time: {formatted_time}", icon="info")

    def save_sentence_to_json(self, preprocessed_sentences, json_filename, sentence_index, sentence_dict):
        # Update the tts_generated flag for the current sentence
        sentence_dict["tts_generated"] = "yes"

        preprocessed_sentences[sentence_index] = sentence_dict
        with open(json_filename, "w") as f:
            json.dump(preprocessed_sentences, f, indent=2)
        logging.info(f"Updated sentence {sentence_dict['sentence_number']} in JSON file: {json_filename}")

    def update_progress_bar(self, progress_percentage):
        self.progress_bar.set(progress_percentage / 100)  # Set the progress bar value
        self.progress_label.configure(text=f"{progress_percentage:.2f}%")  # Update the progress percentage label

    def optimise_sentence(self, sentence_dict, current_sentence, session_dir):
        sentence_number = sentence_dict["sentence_number"]
        original_sentence = sentence_dict["original_sentence"]
        paragraph = sentence_dict.get("paragraph", "no")
        split_part = sentence_dict.get("split_part")

        if not original_sentence:  # Skip processing if sentence is empty
            return None

        json_filename = os.path.join(session_dir, f"{self.session_name.get()}_sentences.json")

        if os.path.exists(json_filename):
            processed_sentences = self.load_json(json_filename)
            if len(processed_sentences) > current_sentence:
                processed_sentence = processed_sentences[current_sentence]
                if processed_sentence.get("processed_sentence") is not None:
                    # Use the processed_sentence if it exists, regardless of LLM processing
                    original_sentence = processed_sentence["processed_sentence"]
        else:
            processed_sentences = []

        if self.enable_llm_processing.get():  # Check if LLM processing is enabled
            if not self.enable_first_prompt.get() and not self.enable_second_prompt.get() and not self.enable_third_prompt.get():
                # If no prompt is enabled, skip optimization and return the original sentence
                processed_sentence = {
                    "sentence_number": sentence_number,
                    "original_sentence": original_sentence,
                    "paragraph": paragraph,
                    "split_part": split_part,
                    "tts_generated": "no"
                }
                if "processed_sentence" in sentence_dict:
                    processed_sentence["processed_sentence"] = sentence_dict["processed_sentence"]
                return processed_sentence

            # Remove the paragraph placeholder before sending the text to the API
            original_sentence = original_sentence.replace('<PARAGRAPH_BREAK>', '')

            prompts = []
            if self.enable_first_prompt.get():  # Only include first prompt if enabled
                prompts.append((self.first_optimisation_prompt, self.enable_first_evaluation.get(), self.first_prompt_model.get(), 1))
            if self.enable_second_prompt.get():  # Only include second prompt if enabled
                prompts.append((self.second_optimisation_prompt, self.enable_second_evaluation.get(), self.second_prompt_model.get(), 2))
            if self.enable_third_prompt.get():  # Only include third prompt if enabled
                prompts.append((self.third_optimisation_prompt, self.enable_third_evaluation.get(), self.third_prompt_model.get(), 3))

            processed_sentences_list = []
            for prompt, evaluate, model_name, prompt_number in prompts:
                if model_name != self.loaded_model:
                    self.load_model(model_name)
                    self.loaded_model = model_name
                processed_sentence = self.call_llm_api(original_sentence, prompt.get(), evaluate=evaluate)
                processed_sentences_list.append({
                    "text": processed_sentence,
                    "paragraph": paragraph,
                    "split_part": split_part
                })
                self.save_json(processed_sentences_list, f"{os.path.splitext(self.source_file)[0]}_prompt_{prompt_number}.json")

                if self.unload_model_after_sentence.get():  # Check if unloading the model is enabled
                    self.unload_model()

            processed_sentence = {
                "sentence_number": sentence_number,
                "original_sentence": original_sentence,
                "paragraph": paragraph,
                "processed_sentence": processed_sentences_list[-1]["text"],
                "split_part": split_part,
                "tts_generated": "no"
            }
        else:
            processed_sentence = {
                "sentence_number": sentence_number,
                "original_sentence": original_sentence,
                "paragraph": paragraph,
                "split_part": split_part,
                "tts_generated": "no"
            }
            if "processed_sentence" in sentence_dict:
                processed_sentence["processed_sentence"] = sentence_dict["processed_sentence"]

        if current_sentence < len(processed_sentences):
            processed_sentences[current_sentence] = processed_sentence
        else:
            processed_sentences.append(processed_sentence)
        self.save_json(processed_sentences, json_filename)

        return processed_sentence

    def split_into_sentences(self, text):
        if self.tts_service.get() == "XTTS":
            language = self.language_var.get()
            if language == "zh-cn":
                sentences = self.split_chinese_sentences(text)
            elif language == "ja":
                sentences = hasami.segment_sentences(text)
            else:
                splitter = SentenceSplitter(language=language)
                sentences = splitter.split(text)
        else:  # Silero
            silero_language_name = self.language_var.get()
            silero_to_simple_lang_codes = {
                "German (v3)": "de",
                "English (v3)": "en",
                "English Indic (v3)": "en",
                "Spanish (v3)": "es",
                "French (v3)": "fr",
                "Indic (v3)": "hi",
                "Russian (v3.1)": "ru",
                "Tatar (v3)": "tt",
                "Ukrainian (v3)": "uk",
                "Uzbek (v3)": "uz",
                "Kalmyk (v3)": "xal"
            }
            language = silero_to_simple_lang_codes.get(silero_language_name, "en")
            splitter = SentenceSplitter(language=language)
            sentences = splitter.split(text)
        return sentences

    def split_chinese_sentences(self, text):
        # Define punctuation that ends a sentence
        end_punctuation = '。！？…'
        
        # Split the text into segments ending with punctuation
        segments = re.split(f'([{end_punctuation}])', text)
        
        # Combine segments and punctuation, and filter out empty strings
        sentences = [''.join(segments[i:i+2]).strip() for i in range(0, len(segments), 2) if segments[i]]
        
        return sentences
    def calculate_similarity(self, str1, str2):
        return difflib.SequenceMatcher(None, str1, str2).ratio()

    def call_llm_api(self, text, prompt, evaluate=False):
        if not text or not prompt:
            return ""

        if not evaluate:
            return self.make_api_request(text, prompt)
        else:
            # For evaluation, make two attempts and evaluate
            result1 = self.make_api_request(text, prompt)
            result2 = self.make_api_request(text, prompt)
            return self.evaluate_and_choose(text, prompt, result1, result2)

    def make_api_request(self, text, user_prompt):
        # Remove newline and tab characters from the text
        text = text.replace('\n', ' ').replace('\t', ' ')

        # Endpoint for OpenAI Chat Completions
        url = "http://127.0.0.1:5000/v1/chat/completions"
        headers = {"Content-Type": "application/json"}

        data = {
            "mode": "instruct",
            "max_new_tokens": 1500,
            "temperature": 0.4,
            "top_p": 0.9,
            "min_p": 0,
            "top_k": 20,
            "repetition_penalty": 1.15,
            "presence_penalty": 0,
            "frequency_penalty": 0,
            "typical_p": 1,
            "tfs": 1,
            "mirostat_mode": 0,
            "mirostat_tau": 5,
            "mirostat_eta": 0.1,
            "seed": -1,
            "truncate": 2500,
            "messages": [
                {"role": "user", "content": f"{user_prompt}{text}"}
            ]
        }

        # Log the API request
        logging.info(f"API Request: {url}")
        logging.info(f"Request Headers: {headers}")
        logging.info(f"Request Data: {data}")

        response = requests.post(url, headers=headers, json=data, verify=False)

        # Log the API response
        logging.info(f"API Response: {response.status_code}")
        logging.info(f"Response Content: {response.text}")

        if response.status_code == 200:
            response_data = response.json()
            if 'choices' in response_data and len(response_data['choices']) > 0:
                messages = response_data['choices'][0]['message']['content']
                return messages
            else:
                logging.warning("No choices returned in the response.")
                return ""
        else:
            logging.error(f"API request failed with status code {response.status_code} and response: {response.text}")
            return ""

    def evaluate_and_choose(self, text, original_prompt, result1, result2):
        # Remove "This is your text:" from the original prompt
        cleaned_prompt = original_prompt.replace("This is your text:", "").strip()
        evaluation_prompt = f"A language model was asked to perform this task twice: '{cleaned_prompt}'. This was the text to process: '{text}'. This was result 1: '{result1}'. This was result 2: '{result2}' Which is better? Output ONLY the digit 1 or 2 and nothing else. No explanations, acknowledgments, notes, comments."
        evaluation_result = self.make_api_request(evaluation_prompt, "")
        
        # Check the first 20 characters of the response
        first_20_chars = evaluation_result.strip()[:20]
        if '1' in first_20_chars and '2' not in first_20_chars:
            return result1
        elif '2' in first_20_chars and '1' not in first_20_chars:
            return result2
        else:
            # Default to result1 if neither or both digits are found
            return result1

    def evaluate_tts(self, text, audio_data):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_file.write(audio_data.export(format="wav").read())
            temp_file_path = temp_file.name

            url = "http://127.0.0.1:8356/predict"
            with open(temp_file_path, "rb") as audio_file:
                files = {"audio_file": audio_file}
                data = {
                    "pretrained_model": "nisqa_tts.tar",
                }

                try:
                    response = requests.post(url, files=files, data=data)
                    response.raise_for_status()  # Raise an exception for 4xx or 5xx status codes
                    result = response.json()
                    mos_pred = result["mos"]
                except requests.exceptions.RequestException as e:
                    error_message = f"Failed to get MOS score from NISQA API. Error: {str(e)}"
                    raise ValueError(error_message)

        os.remove(temp_file_path)
        return mos_pred
 
    def save_json(self, data, filename):
        numbered_data = []
        sentence_counter = 1
        
        for sentence_dict in data:
            split_part = sentence_dict.get("split_part")
            paragraph = sentence_dict.get("paragraph", "no")
            start = sentence_dict.get("start")  # Add this line
            end = sentence_dict.get("end")  # Add this line
            
            sentence_number = str(sentence_counter)
            sentence_counter += 1
            
            numbered_sentence = {
                "sentence_number": sentence_number,
                "paragraph": paragraph,
                "split_part": split_part,
                "original_sentence": sentence_dict.get("original_sentence"),
                "processed_sentence": sentence_dict.get("processed_sentence"),
                "tts_generated": sentence_dict.get("tts_generated", "no"),
                "start": start,  # Add this line
                "end": end  # Add this line
            }
            numbered_data.append(numbered_sentence)
        
        with open(filename, 'w') as f:
            json.dump(numbered_data, f, indent=2)

    def update_language_dropdown(self, event=None):
        if self.tts_service.get() == "XTTS":
            languages = ["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"]
            self.language_dropdown.configure(values=languages, state="normal")  # Enable the language dropdown
            self.language_var.set("en")
            self.upload_new_voices_button.configure(state=tk.NORMAL)  # Enable the button for XTTS
            self.populate_speaker_dropdown()  # Update the speaker dropdown with XTTS speakers
            self.sample_length_dropdown.grid_remove()  # Hide the "Sample Length" dropdown

        elif self.tts_service.get() == "VoiceCraft":
            self.language_dropdown.configure(values=["English"], state="disabled")  # Disable the language dropdown
            self.language_var.set("English")
            self.upload_new_voices_button.configure(state=tk.NORMAL)  # Enable the button for VoiceCraft
            self.populate_speaker_dropdown()  # Update the speaker dropdown with VoiceCraft speakers
            self.sample_length_dropdown.grid()  # Show the "Sample Length" dropdown

        else:  # Silero
            language_names = [lang["name"] for lang in silero_languages]
            self.language_dropdown.configure(values=language_names, state="normal")  # Enable the language dropdown
            self.language_var.set("English (v3)")
            self.upload_new_voices_button.configure(state=tk.DISABLED)  # Disable the button for Silero
            self.sample_length_dropdown.grid_remove()  # Hide the "Sample Length" dropdown

            selected_language_name = self.language_var.get()
            selected_language_code = next((lang["code"] for lang in silero_languages if lang["name"] == selected_language_name), None)

            if selected_language_code:
                try:
                    response = requests.post("http://localhost:8001/tts/language", json={"id": selected_language_code})
                    if response.status_code == 200:
                        # Fetch the updated list of speakers for the selected language
                        self.master.after(1000, self.populate_speaker_dropdown)
                    else:
                        messagebox.showerror("Error", "Failed to set Silero language.")
                except requests.exceptions.ConnectionError:
                    messagebox.showerror("Error", "Failed to connect to the Silero API.")
                    
    def tts_to_audio(self, text):
        best_audio = None
        best_mos = -1
        if self.tts_service.get() == "XTTS":
            language = self.language_dropdown.get()
            speaker = self.selected_speaker.get()
            
            # Remove the period at the end of the sentence if the language is not English
            if language != "en":
                text = text.rstrip('.')
            
            speaker_path = os.path.join(self.tts_voices_folder, speaker)
            if os.path.isfile(speaker_path):
                speaker_arg = speaker
            else:
                speaker_arg = speaker

            for attempt in range(self.max_attempts.get()):
                try:
                    data = {
                        "text": text,
                        "speaker_wav": speaker_arg,
                        "language": language
                    }

                    print(f"Request data: {data}")
                    if self.external_server_connected:
                        external_server_url = self.external_server_url.get()
                        response = requests.post(f"{external_server_url}/tts_to_audio/", json=data)
                    else:
                        response = requests.post("http://localhost:8020/tts_to_audio/", json=data)
                    print(f"Response status code: {response.status_code}")
                    if response.status_code == 200:
                        audio_data = io.BytesIO(response.content)
                        audio = AudioSegment.from_file(audio_data, format="wav")

                        if self.enable_tts_evaluation.get():
                            mos_score = self.evaluate_tts(text, audio)
                            if mos_score is not None:
                                if mos_score > best_mos:
                                    best_audio = audio
                                    best_mos = mos_score

                                if mos_score >= float(self.target_mos_value.get()):
                                    return best_audio
                        else:
                            return audio
                    else:
                        print(f"Error {response.status_code}: Failed to convert text to audio.")
                except Exception as e:
                    print(f"Error in tts_to_audio: {str(e)}")
        elif self.tts_service.get() == "VoiceCraft":
            speaker = self.selected_speaker.get()
            wav_file = os.path.join(self.tts_voices_folder, "VoiceCraft", f"{speaker}.wav")
            txt_file = os.path.join(self.tts_voices_folder, "VoiceCraft", f"{speaker}.txt")

            selected_model = self.voicecraft_model.get()
            if selected_model == "330M_TTSEnhanced":
                model_name = "VoiceCraft_gigaHalfLibri330M_TTSEnhanced_max16s"
            else:
                model_name = "VoiceCraft_830M_TTSEnhanced"
            
            for attempt in range(self.max_attempts.get()):
                try:
                    if self.use_external_server_voicecraft.get() and self.external_server_connected_voicecraft:
                        url = f"{self.external_server_url_voicecraft.get()}/generate"
                    else:
                        url = "http://localhost:8245/generate"

                    files = {
                        "audio": open(wav_file, "rb"),
                        "transcript": open(txt_file, "rb")
                    }
                    data = {
                        "target_text": text,
                        "time": float(self.sample_length.get()),
                        "save_to_file": False,
                        "model_name": model_name  # Pass the selected model name
                    }

                    if self.show_advanced_tts_settings.get():
                        data["top_k"] = int(self.top_k.get())
                        data["top_p"] = float(self.top_p.get())
                        data["temperature"] = float(self.temperature.get())
                        data["stop_repetition"] = int(self.stop_repetition.get())
                        data["kvcache"] = int(self.kvcache.get())
                        data["sample_batch_size"] = int(self.sample_batch_size.get())

                    response = requests.post(url, files=files, data=data)

                    if response.status_code == 200:
                        audio_bytes = response.content
                        audio_data = io.BytesIO(audio_bytes)
                        audio = AudioSegment.from_file(audio_data, format="wav")

                        if self.enable_tts_evaluation.get():
                            mos_score = self.evaluate_tts(text, audio)
                            if mos_score > best_mos:
                                best_audio = audio
                                best_mos = mos_score

                            if mos_score >= float(self.target_mos_value.get()):
                                return best_audio
                        else:
                            return audio
                    else:
                        print(f"Error {response.status_code}: Failed to convert text to audio using VoiceCraft.")
                except Exception as e:
                    print(f"Error in tts_to_audio (VoiceCraft): {str(e)}")
        else:  # Silero
            speaker = self.selected_speaker.get()
            language = self.language_var.get()  # Replace self.language.get() with self.language_var.get()
            for attempt in range(self.max_attempts.get()):
                try:
                    data = {
                        "speaker": speaker,
                        "text": text,
                        "session": ""
                    }
                    url = "http://localhost:8001/tts/generate"
                    print(f"Making POST request to: {url}")  # Add this line
                    response = requests.post(url, json=data)
                    print(f"Response status code: {response.status_code}")  # Add this line

                    if response.status_code == 200:
                        audio_data = io.BytesIO(response.content)
                        audio = AudioSegment.from_file(audio_data, format="wav")

                        if self.enable_tts_evaluation.get():
                            mos_score = self.evaluate_tts(text, audio)
                            if mos_score > best_mos:
                                best_audio = audio
                                best_mos = mos_score

                            if mos_score >= float(self.target_mos_value.get()):
                                return best_audio
                        else:
                            return audio
                    else:
                        print(f"Error {response.status_code}: Failed to convert text to audio using Silero.")
                except Exception as e:
                    print(f"Error in tts_to_audio (Silero): {str(e)}")

        return best_audio
        
    def add_silence(self, audio_segment, silence_length_ms):
        silence = AudioSegment.silent(duration=silence_length_ms)
        return audio_segment + silence

    def refresh_rvc_models(self):
        self.rvc_inference.set_models_dir(self.rvc_models_dir)
        self.rvc_models = [folder for folder in os.listdir(self.rvc_models_dir) 
                        if os.path.isdir(os.path.join(self.rvc_models_dir, folder))]
        
        if hasattr(self, 'rvc_model_dropdown'):
            self.rvc_model_dropdown.configure(values=self.rvc_models)
            if self.rvc_models:
                self.rvc_model_dropdown.set(self.rvc_models[0])
            else:
                self.rvc_model_dropdown.set("")

    def upload_rvc_model(self):
        pth_file = filedialog.askopenfilename(filetypes=[("Model files", "*.pth")])
        if not pth_file:
            return
        
        index_file = filedialog.askopenfilename(filetypes=[("Index files", "*.*")])
        if not index_file:
            return
        
        model_name = os.path.splitext(os.path.basename(pth_file))[0]
        model_dir = os.path.join(self.rvc_models_dir, model_name)
        os.makedirs(model_dir, exist_ok=True)
        
        shutil.copy(pth_file, os.path.join(model_dir, f"{model_name}.pth"))
        
        index_ext = os.path.splitext(index_file)[1]
        shutil.copy(index_file, os.path.join(model_dir, f"{model_name}{index_ext}"))
        
        self.refresh_rvc_models()
        messagebox.showinfo("Model Uploaded", f"Model '{model_name}' has been uploaded successfully.")

    def process_with_rvc(self, audio_segment):
        if not self.enable_rvc.get():
            return audio_segment

        try:
            model_name = self.rvc_model_dropdown.get()
            
            if not model_name:
                raise ValueError("No RVC model selected")

            # Load the RVC model
            self.rvc_inference.load_model(model_name)

            # Set RVC parameters
            self.rvc_inference.set_params(
                f0up_key=self.rvc_pitch.get(),
                f0method=self.rvc_f0_method.get(),
                index_rate=self.rvc_index_rate.get(),
                filter_radius=self.rvc_filter_radius.get(),
                resample_sr=40000,  # Set this to 40000 to enable resampling in RVC
                rms_mix_rate=self.rvc_volume_envelope.get(),
                protect=self.rvc_protect.get()
            )

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_input_file:
                temp_input_path = temp_input_file.name
                audio_segment.export(temp_input_path, format="wav")

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_output_file:
                temp_output_path = temp_output_file.name

            # Process the audio
            self.rvc_inference.infer_file(temp_input_path, temp_output_path)

            # Load the processed audio
            processed_audio_data = AudioSegment.from_wav(temp_output_path)

            return processed_audio_data

        except Exception as e:
            logging.error(f"RVC Processing Error: {str(e)}")
            return audio_segment  # Return original audio if processing fails

        finally:
            # Clean up temporary files
            for path in [temp_input_path, temp_output_path]:
                if 'path' in locals() and os.path.exists(path):
                    os.unlink(path)

            # Unload the model to free up memory
            self.rvc_inference.unload_model()


    def apply_fade(self, audio_data, fade_in_duration, fade_out_duration):
        audio_data = audio_data.fade_in(fade_in_duration).fade_out(fade_out_duration)
        return audio_data
        
    def unload_model(self):
        url = "http://127.0.0.1:5000/v1/internal/model/unload"
        headers = {"Content-Type": "application/json"}
        response = requests.post(url, headers=headers, verify=False)
        if response.status_code != 200:
            CTkMessagebox(title="Error", message="Failed to unload the model.", icon="cancel")
        else:
            self.loaded_model = None

    def stop_playback(self):
        if self.channel is not None and self.channel.get_busy():
            self.channel.stop()
        if pygame.mixer.get_init() is not None:
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()  # Unload the music file
        self.playlist_stopped = True
        self.paused = False
        self.playing = False
        self.master.after(10, self.update_play_button_text, "Play")  # Schedule button text update

    def update_play_button_text(self, text):
        if self.play_button is not None:
            self.play_button.configure(text=text)

    def update_playlist(self, processed_sentence):
        sentence_text = processed_sentence["processed_sentence"] if self.enable_llm_processing.get() and "processed_sentence" in processed_sentence else processed_sentence["original_sentence"]
        sentence_number = int(processed_sentence["sentence_number"])
        if sentence_text not in self.playlist_listbox.get(0, tk.END):
            # Add the sentence number to the displayed text
            display_text = f"[{sentence_number}] {sentence_text}"
        display_text = f"[{sentence_number}] {sentence_text}"
        self.playlist_listbox.insert(int(sentence_number) - 1, display_text)

    def play_selected_sentence(self):
        if pygame.mixer.get_init() is None:
            pygame.mixer.init()
            
        if self.channel is None:
            self.channel = pygame.mixer.Channel(0)
            
        selected_index = self.playlist_listbox.curselection()
        if selected_index:
            selected_sentence = self.playlist_listbox.get(selected_index)
            # Extract the sentence number and text using a regular expression
            match = re.match(r'^\[(\d+)\]\s(.+)$', selected_sentence)
            if match:
                sentence_number = match.group(1)
                sentence_text = match.group(2)
            else:
                sentence_number = None
                sentence_text = selected_sentence
            
            session_name = self.session_name.get()
            session_dir = os.path.join("Outputs", session_name)
            json_filename = os.path.join(session_dir, f"{session_name}_sentences.json")
            processed_sentences = self.load_json(json_filename)
            
            sentence_dict = next((s for s in processed_sentences if str(s["sentence_number"]) == sentence_number), None)

            if sentence_dict:
                wav_filename = os.path.join(session_dir, "Sentence_wavs", f"{session_name}_sentence_{sentence_number}.wav")

                if os.path.exists(wav_filename):
                    sound = pygame.mixer.Sound(wav_filename)
                    self.channel.play(sound)
                    self.current_sentence = sentence_text
                    self.playing = True
                    self.master.after(10, self.update_play_button_text, "Pause")
                    self.master.after(math.ceil(sound.get_length() * 1000), self.stop_playback)
                    self.master.after(math.ceil(sound.get_length() * 1000), sound.stop)  # Stop the sound after playback
                else:
                    messagebox.showinfo("Audio Not Available", "The audio for the selected sentence is not available.")
            else:
                messagebox.showinfo("Sentence Not Found", "The selected sentence was not found in the JSON file.")


    def view_session_folder(self):
        session_name = self.session_name.get()
        if session_name:
            session_dir = os.path.join("Outputs", session_name)
            if os.path.exists(session_dir):
                if platform.system() == "Windows":
                    os.startfile(session_dir)
                elif platform.system() == "Darwin":
                    subprocess.Popen(["open", session_dir])
                else:
                    subprocess.Popen(["xdg-open", session_dir])
            else:
                CTkMessagebox(title="Session Folder Not Found", message=f"The session folder for '{session_name}' does not exist.", icon="info")
        else:
            CTkMessagebox(title="No Session", message="There is no active session.", icon="info")

    def regenerate_selected_sentence(self):
        try:
            if self.tts_service.get() == "XTTS":
                self.apply_xtts_settings_silently()

            selected_index = self.playlist_listbox.curselection()
            if selected_index:
                selected_sentence = self.playlist_listbox.get(selected_index)
                # Extract the sentence number and text using a regular expression
                match = re.match(r'^\[(\d+)\]\s(.+)$', selected_sentence)
                if match:
                    sentence_number = match.group(1)
                    sentence_text = match.group(2)
                else:
                    sentence_number = None
                    sentence_text = selected_sentence
                
                session_dir = os.path.join("Outputs", self.session_name.get())
                json_filename = os.path.join(session_dir, f"{self.session_name.get()}_sentences.json")
                processed_sentences = self.load_json(json_filename)
                sentence_dict = next((s for s in processed_sentences if str(s["sentence_number"]) == sentence_number), None)
                if sentence_dict:
                    original_sentence_index = processed_sentences.index(sentence_dict)
                    sentence_number = int(sentence_dict["sentence_number"])

                    # Update the sentence_dict with the original sentence text (without the number)
                    sentence_dict["original_sentence"] = sentence_text
                    sentence_dict["processed_sentence"] = sentence_text

                    processed_sentences[original_sentence_index] = sentence_dict
                    self.save_json(processed_sentences, json_filename)
                    logging.info(f"Updated sentence {sentence_number} in JSON file before regeneration: {json_filename}")

                    if self.source_file.endswith(".srt"):
                        # Disable sentence splitting, appending, and silence appending for srt files
                        self.enable_sentence_splitting.set(False)
                        self.enable_sentence_appending.set(False)
                        self.silence_length.set(0)
                        self.paragraph_silence_length.set(0)

                    # Optimize the sentence
                    processed_sentence = self.optimise_sentence(sentence_dict, original_sentence_index, session_dir)

                    if processed_sentence is not None:
                        # Generate audio using the processed sentence
                        audio_data = self.tts_to_audio(processed_sentence["processed_sentence"] if "processed_sentence" in processed_sentence else processed_sentence["original_sentence"])

                        if audio_data is not None:
                            # Apply RVC if enabled
                            if self.enable_rvc.get():
                                audio_data = self.process_with_rvc(audio_data)

                            # Apply fade in/out if enabled
                            if self.enable_fade.get():
                                audio_data = self.apply_fade(audio_data, self.fade_in_duration.get(), self.fade_out_duration.get())

                            if not self.source_file.endswith(".srt"):
                                if processed_sentence.get("paragraph", "no") == "yes":
                                    silence_length = self.paragraph_silence_length.get()
                                elif processed_sentence.get("split_part") is not None:
                                    if isinstance(processed_sentence.get("split_part"), str):
                                        if processed_sentence.get("split_part") in ["0a", "0b", "1a"]:
                                            silence_length = self.silence_length.get() // 4
                                        elif processed_sentence.get("split_part") == "1b":
                                            silence_length = self.silence_length.get()
                                    elif isinstance(processed_sentence.get("split_part"), int):
                                        if processed_sentence.get("split_part") == 0:
                                            silence_length = self.silence_length.get() // 4
                                        elif processed_sentence.get("split_part") == 1:
                                            silence_length = self.silence_length.get()
                                else:
                                    silence_length = self.silence_length.get()

                                if silence_length > 0:
                                    audio_data += AudioSegment.silent(duration=silence_length)

                            sentence_output_filename = os.path.join(session_dir, "Sentence_wavs", f"{self.session_name.get()}_sentence_{sentence_number}.wav")
                            audio_data.export(sentence_output_filename, format="wav")
                            logging.info(f"Regenerated audio for sentence {sentence_number}: {sentence_output_filename}")

                            # Update the listbox entry with the regenerated sentence (including the number)
                            self.playlist_listbox.delete(selected_index)
                            self.playlist_listbox.insert(selected_index, f"[{sentence_number}] {sentence_text}")

                            # Set the "tts_generated" flag to "yes" after successful regeneration
                            sentence_dict["tts_generated"] = "yes"
                            self.save_json(processed_sentences, json_filename)

                        else:
                            logging.error(f"Failed to generate audio for sentence {sentence_number}")

                    else:
                        logging.error(f"Failed to process sentence {sentence_number}")

                else:
                    logging.warning(f"Sentence not found in JSON file")

            else:
                logging.warning("No sentence selected for regeneration")

        except Exception as e:
            logging.error(f"Error regenerating sentence: {str(e)}")

    def regenerate_all_sentences(self):
        try:
            if self.tts_service.get() == "XTTS":
                self.apply_xtts_settings_silently()

            session_name = self.session_name.get()
            session_dir = os.path.join("Outputs", session_name)
            json_filename = os.path.join(session_dir, f"{session_name}_sentences.json")

            if not os.path.exists(json_filename):
                CTkMessagebox(title="Error", message="Session JSON file not found.", icon="cancel")
                return

            processed_sentences = self.load_json(json_filename)

            progress_window = ctk.CTkToplevel(self.master)
            progress_window.title("Regenerating All Sentences")
            progress_bar = ctk.CTkProgressBar(progress_window)
            progress_bar.pack(padx=20, pady=20)
            progress_label = ctk.CTkLabel(progress_window, text="0%")
            progress_label.pack(pady=10)

            total_sentences = len(processed_sentences)

            for index, sentence_dict in enumerate(processed_sentences):
                sentence_number = sentence_dict["sentence_number"]
                original_sentence = sentence_dict["original_sentence"]

                # Update the sentence_dict with the original sentence text
                sentence_dict["processed_sentence"] = original_sentence

                if self.source_file.endswith(".srt"):
                    self.enable_sentence_splitting.set(False)
                    self.enable_sentence_appending.set(False)
                    self.silence_length.set(0)
                    self.paragraph_silence_length.set(0)

                # Optimize the sentence
                processed_sentence = self.optimise_sentence(sentence_dict, index, session_dir)

                if processed_sentence is not None:
                    # Generate audio using the processed sentence
                    audio_data = self.tts_to_audio(processed_sentence["processed_sentence"] if "processed_sentence" in processed_sentence else processed_sentence["original_sentence"])

                    if audio_data is not None:
                        # Apply RVC if enabled
                        if self.enable_rvc.get():
                            audio_data = self.process_with_rvc(audio_data)

                        # Apply fade in/out if enabled
                        if self.enable_fade.get():
                            audio_data = self.apply_fade(audio_data, self.fade_in_duration.get(), self.fade_out_duration.get())

                        if not self.source_file.endswith(".srt"):
                            if processed_sentence.get("paragraph", "no") == "yes":
                                silence_length = self.paragraph_silence_length.get()
                            elif processed_sentence.get("split_part") is not None:
                                if isinstance(processed_sentence.get("split_part"), str):
                                    if processed_sentence.get("split_part") in ["0a", "0b", "1a"]:
                                        silence_length = self.silence_length.get() // 4
                                    elif processed_sentence.get("split_part") == "1b":
                                        silence_length = self.silence_length.get()
                                elif isinstance(processed_sentence.get("split_part"), int):
                                    if processed_sentence.get("split_part") == 0:
                                        silence_length = self.silence_length.get() // 4
                                    elif processed_sentence.get("split_part") == 1:
                                        silence_length = self.silence_length.get()
                            else:
                                silence_length = self.silence_length.get()

                            if silence_length > 0:
                                audio_data += AudioSegment.silent(duration=silence_length)

                        sentence_output_filename = os.path.join(session_dir, "Sentence_wavs", f"{session_name}_sentence_{sentence_number}.wav")
                        audio_data.export(sentence_output_filename, format="wav")

                        # Update the listbox entry with the regenerated sentence
                        self.playlist_listbox.delete(index)
                        self.playlist_listbox.insert(index, f"[{sentence_number}] {processed_sentence['processed_sentence']}")

                        # Set the "tts_generated" flag to "yes" after successful regeneration
                        sentence_dict["tts_generated"] = "yes"
                        self.save_json(processed_sentences, json_filename)

                    else:
                        logging.error(f"Failed to generate audio for sentence {sentence_number}")

                else:
                    logging.error(f"Failed to process sentence {sentence_number}")

                # Update progress
                progress = (index + 1) / total_sentences
                progress_bar.set(progress)
                progress_label.configure(text=f"{progress:.0%}")
                progress_window.update()

            progress_window.destroy()
            CTkMessagebox(title="Regeneration Complete", message="All sentences have been regenerated.", icon="info")

        except Exception as e:
            logging.error(f"Error regenerating all sentences: {str(e)}")
            CTkMessagebox(title="Error", message=f"An error occurred while regenerating sentences: {str(e)}", icon="cancel")

    def play_sentences_as_playlist(self):
        if pygame.mixer.get_init() is None:
            pygame.mixer.init()
            
        self.channel = pygame.mixer.Channel(0)
            
        sentences = self.playlist_listbox.get(0, tk.END)
        if sentences:
            selected_index = self.playlist_listbox.curselection()
            if selected_index:
                self.playlist_index = selected_index[0]
            else:
                self.playlist_index = 0
            self.playlist_stopped = False
            self.playing = True
            self.master.after(10, self.update_play_button_text, "Pause")  # Schedule button text update
            self.play_next_sentence_in_playlist()
        else:
            messagebox.showinfo("Playlist Empty", "There are no sentences in the playlist.")

    def play_next_sentence_in_playlist(self):
        if not self.playlist_stopped and 0 <= self.playlist_index < self.playlist_listbox.size():
            sentence = self.playlist_listbox.get(self.playlist_index)
            # Extract the sentence number and text from the listbox entry
            match = re.match(r'^\[(\d+)\]\s(.+)$', sentence)
            if match:
                sentence_number = match.group(1)
                sentence_text = match.group(2)
            else:
                sentence_number = None
                sentence_text = sentence

            session_name = self.session_name.get()
            session_dir = os.path.join("Outputs", session_name)
            json_filename = os.path.join(session_dir, f"{session_name}_sentences.json")
            processed_sentences = self.load_json(json_filename)

            sentence_dict = next((s for s in processed_sentences if str(s["sentence_number"]) == sentence_number), None)

            if sentence_dict:
                wav_filename = os.path.join(session_dir, "Sentence_wavs", f"{session_name}_sentence_{sentence_number}.wav")

                if os.path.exists(wav_filename):
                    if pygame.mixer.get_init() is None:
                        pygame.mixer.init()
                    if self.channel is None:
                        self.channel = pygame.mixer.Channel(0)
                    sound = pygame.mixer.Sound(wav_filename)
                    self.channel.play(sound)
                    self.current_sentence = sentence_text
                    sound_length = sound.get_length()
                    self.master.after(math.ceil(sound_length * 1000), self.play_next_sentence_callback)
                else:
                    self.playlist_index += 1
                    self.play_next_sentence_in_playlist()
            else:
                self.playlist_index += 1
                self.play_next_sentence_in_playlist()
        else:
            self.stop_playback()

    def play_next_sentence_callback(self):
        if not self.playlist_stopped and not self.paused:
            self.playlist_index += 1
            self.play_next_sentence_in_playlist()

    def check_playlist_playback(self):
        if not self.paused and not self.playlist_stopped and self.channel.get_busy():
            self.master.after(100, self.check_playlist_playback)
 
    def load_session(self):
        session_folder = filedialog.askdirectory(initialdir="Outputs")
        if session_folder:
            session_name = os.path.basename(session_folder)
            json_filename = os.path.join(session_folder, f"{session_name}_sentences.json")
            if os.path.exists(json_filename):
                self.session_name.set(session_name)
                self.session_name_label.configure(text=session_name)  # Update the session name label
                processed_sentences = self.load_json(json_filename)
                self.playlist_listbox.delete(0, tk.END)
                for sentence_dict in processed_sentences:
                    sentence_number = sentence_dict.get("sentence_number")
                    sentence_text = sentence_dict.get("processed_sentence") if sentence_dict.get("processed_sentence") else sentence_dict.get("original_sentence")
                    if sentence_text and sentence_dict.get("tts_generated") == "yes":
                        display_text = f"[{sentence_number}] {sentence_text}"
                        self.playlist_listbox.insert(tk.END, display_text)

                # Load the text file from the session directory
                txt_files = [file for file in os.listdir(session_folder) if file.endswith(".txt")]
                if txt_files:
                    self.source_file = os.path.join(session_folder, txt_files[0])
                    file_name = os.path.basename(self.source_file)
                    truncated_file_name = file_name[:70] + "..." if len(file_name) > 70 else file_name
                    self.selected_file_label.configure(text=truncated_file_name)
                else:
                    self.source_file = ""
                    self.selected_file_label.configure(text="No file selected")

                messagebox.showinfo("Session Loaded", f"The session '{session_name}' has been loaded.")
            else:
                messagebox.showerror("Error", "Session JSON file not found.")

    def update_sentence_in_json(self, sentence_number, edited_sentence):
        try:
            session_dir = os.path.join("Outputs", self.session_name.get())
            json_filename = os.path.join(session_dir, f"{self.session_name.get()}_sentences.json")
            processed_sentences = self.load_json(json_filename)
            
            sentence_changed = False
            
            for sentence_dict in processed_sentences:
                if sentence_dict["sentence_number"] == sentence_number:
                    if "processed_sentence" in sentence_dict:
                        if sentence_dict["processed_sentence"] != edited_sentence:
                            sentence_dict["processed_sentence"] = edited_sentence
                            sentence_changed = True
                    else:
                        if sentence_dict["original_sentence"] != edited_sentence:
                            sentence_dict["original_sentence"] = edited_sentence
                            sentence_changed = True
                    break
            
            if sentence_changed:
                self.save_json(processed_sentences, json_filename)
                logging.info(f"Updated sentence {sentence_number} in JSON file: {json_filename}")
            else:
                logging.info(f"No changes made to sentence {sentence_number}")
        
        except Exception as e:
            logging.error(f"Error updating sentence {sentence_number} in JSON file: {str(e)}")

    def edit_selected_sentence(self):
        try:
            selected_index = self.playlist_listbox.curselection()
            if selected_index:
                selected_sentence = self.playlist_listbox.get(selected_index)
                # Extract the sentence number and text using a regular expression
                match = re.match(r'^\[(\d+)\]\s(.+)$', selected_sentence)
                if match:
                    sentence_number = match.group(1)
                    sentence_text = match.group(2)
                else:
                    sentence_number = None
                    sentence_text = selected_sentence
                
                session_dir = os.path.join("Outputs", self.session_name.get())
                json_filename = os.path.join(session_dir, f"{self.session_name.get()}_sentences.json")
                processed_sentences = self.load_json(json_filename)
                sentence_dict = next((s for s in processed_sentences if str(s["sentence_number"]) == sentence_number), None)
                if sentence_dict:
                    edit_window = ctk.CTkToplevel(self.master)
                    edit_window.title("Edit Sentence")

                    sentence_entry = ctk.CTkEntry(edit_window, width=400)
                    sentence_entry.insert(0, sentence_text)  # Insert the extracted sentence text
                    sentence_entry.pack(padx=10, pady=10)

                    def save_edited_sentence():
                        edited_sentence = sentence_entry.get()
                        self.update_sentence_in_json(sentence_number, edited_sentence)
                        # Update the listbox entry with the edited sentence
                        self.playlist_listbox.delete(selected_index)
                        self.playlist_listbox.insert(selected_index, f"[{sentence_number}] {edited_sentence}")
                        edit_window.destroy()
                        logging.info(f"Edited sentence {sentence_number}: {edited_sentence}")

                    def discard_changes():
                        edit_window.destroy()
                        logging.info(f"Discarded changes for sentence {sentence_number}")

                    if self.source_file.endswith(".srt"):
                        # Disable sentence splitting, appending, and silence appending for srt files
                        self.enable_sentence_splitting.set(False)
                        self.enable_sentence_appending.set(False)
                        self.silence_length.set(0)
                        self.paragraph_silence_length.set(0)

                    save_button = ctk.CTkButton(edit_window, text="Save", command=save_edited_sentence)
                    save_button.pack(side=tk.LEFT, padx=10, pady=10)

                    discard_button = ctk.CTkButton(edit_window, text="Discard", command=discard_changes)
                    discard_button.pack(side=tk.LEFT, padx=10, pady=10)

                else:
                    messagebox.showinfo("Sentence Not Found", "The selected sentence was not found in the JSON file.")

            else:
                logging.warning("No sentence selected for editing")

        except Exception as e:
            logging.error(f"Error editing sentence: {str(e)}")

    def save_output(self):
        session_name = self.session_name.get()
        output_format = self.output_format.get()
        bitrate = self.bitrate.get()

        # Open a file dialog to choose the output directory and file name
        output_path = filedialog.asksaveasfilename(
            initialdir="Outputs",
            initialfile=f"{session_name}.{output_format}",
            filetypes=[(f"{output_format.upper()} Files", f"*.{output_format}")],
            defaultextension=f".{output_format}"
        )

        if output_path:
            output_directory = os.path.dirname(output_path)
            output_filename = os.path.basename(output_path)

            session_dir = os.path.join("Outputs", session_name)
            json_filename = os.path.join(session_dir, f"{session_name}_sentences.json")
            processed_sentences = self.load_json(json_filename)

            if self.source_file.endswith(".srt"):
                # Synchronize the audio segments based on subtitle timings for srt files
                final_audio = self.synchronize_audio(processed_sentences, session_name)
                if output_format == "wav":
                    final_audio.export(output_path, format="wav")
                elif output_format == "mp3":
                    final_audio.export(output_path, format="mp3", bitrate=bitrate)
                elif output_format == "opus":
                    final_audio.export(output_path, format="opus", bitrate=bitrate)
            else:
                # Concatenate the audio segments using FFmpeg for non-srt files
                wav_files = []
                for sentence_dict in processed_sentences:
                    sentence_number = int(sentence_dict["sentence_number"])
                    wav_filename = os.path.join(session_dir, "Sentence_wavs", f"{session_name}_sentence_{sentence_number}.wav")
                    if os.path.exists(wav_filename):
                        wav_files.append(wav_filename)

                with tempfile.NamedTemporaryFile(mode="w", delete=False) as temp_file:
                    for wav_file in wav_files:
                        temp_file.write(f"file '{os.path.abspath(wav_file)}'\n")
                    input_list_path = temp_file.name

                ffmpeg_command = [
                    "ffmpeg",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", input_list_path,
                    "-y"  # '-y' option to overwrite output files without asking
                ]

                # Adjust the codec and bitrate based on the output format
                if output_format == "wav":
                    ffmpeg_command.extend(["-c:a", "pcm_s16le"])
                elif output_format == "mp3":
                    ffmpeg_command.extend(["-c:a", "libmp3lame", "-b:a", bitrate])
                elif output_format == "opus":
                    ffmpeg_command.extend(["-c:a", "libopus", "-b:a", bitrate])

                # Append the output path without quotes
                ffmpeg_command.append(output_path)

                print("FFmpeg Command:")
                print(" ".join(ffmpeg_command))

                try:
                    subprocess.run(ffmpeg_command, check=True, stderr=subprocess.PIPE, universal_newlines=True)
                    messagebox.showinfo("Output Saved", f"The output file has been saved as {output_filename}")
                except subprocess.CalledProcessError as e:
                    error_message = f"FFmpeg exited with a non-zero code: {e.returncode}\n\nError output:\n{e.stderr}"
                    logging.error(error_message)
                    messagebox.showerror("FFmpeg Error", error_message)
                except Exception as e:
                    error_message = f"An unexpected error occurred: {str(e)}"
                    logging.error(error_message)
                    messagebox.showerror("Error", error_message)
                finally:
                    if os.path.exists(input_list_path):
                        os.remove(input_list_path)

def main():
    logging.info("Pandrator application starting")
    
    parser = argparse.ArgumentParser(description="Pandrator TTS Optimizer")
    parser.add_argument("-connect", action="store_true", help="Connect to a TTS service on launch")
    parser.add_argument("-xtts", action="store_true", help="Connect to XTTS")
    parser.add_argument("-voicecraft", action="store_true", help="Connect to VoiceCraft")
    parser.add_argument("-silero", action="store_true", help="Connect to Silero")
    args = parser.parse_args()
    
    logging.info(f"Command line arguments: {args}")

    root = ctk.CTk()
    try:
        root.iconbitmap("pandrator.ico")
    except tk.TclError as e:
        logging.warning(f"Icon file 'pandrator.ico' not found. Proceeding without setting the window icon. Error: {str(e)}")

    gui = TTSOptimizerGUI(root)
    logging.info("GUI initialized")

    if args.connect:
        if args.xtts:
            logging.info("Connecting to XTTS")
            gui.tts_service.set("XTTS")
            gui.connect_to_server()
        elif args.voicecraft:
            logging.info("Connecting to VoiceCraft")
            gui.tts_service.set("VoiceCraft")
            gui.connect_to_server()
        elif args.silero:
            logging.info("Connecting to Silero")
            gui.tts_service.set("Silero")
            gui.connect_to_server()

    logging.info("Starting main event loop")
    root.mainloop()
    logging.info("Pandrator application exiting")

if __name__ == "__main__":
    main()
