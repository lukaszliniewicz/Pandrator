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

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

class TTSOptimizerGUI:
    def __init__(self, master):
        self.master = master
        master.title("Pandrator")
        pygame.mixer.init()
        self.channel = pygame.mixer.Channel(0)
        self.enable_tts_evaluation = ctk.BooleanVar(value=False)
        self.stop_flag = False
        self.delete_session_flag = False
        self.server_connected = False
        self.tts_voices_folder = "tts_voices"
        if not os.path.exists(self.tts_voices_folder):
            os.makedirs(self.tts_voices_folder)

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
        self.rvc_model_path = ctk.StringVar()
        self.rvc_index_path = ctk.StringVar()
        self.enable_llm_processing = ctk.BooleanVar(value=False)
        self.playlist_stopped = False
        self.target_mos_value = ctk.StringVar(value="2.9")
        self.max_attempts = ctk.IntVar(value=5)
        self.paused = False
        self.playing = False
        self.session_name = ctk.StringVar()

        # Layout
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        # Get the screen resolution
        screen_width = self.master.winfo_screenwidth()
        screen_height = self.master.winfo_screenheight()

        # Calculate the desired frame height (85% of the screen height)
        frame_height = int(screen_height * 0.85)

        # Create the main scrollable frame with the calculated height
        self.main_scrollable_frame = ctk.CTkScrollableFrame(master, width=680, height=frame_height)
        self.main_scrollable_frame.grid(row=0, column=0, padx=10, pady=10, sticky=tk.NSEW)
        self.main_scrollable_frame.grid_columnconfigure(0, weight=1)
        self.main_scrollable_frame.grid_rowconfigure(0, weight=1)

        # Tabs
        self.tabview = ctk.CTkTabview(self.main_scrollable_frame)
        self.tabview.grid(row=0, column=0, padx=10, pady=10, sticky=tk.NSEW)

        # Session Tab
        self.session_tab = self.tabview.add("Session")
        self.session_tab.grid_columnconfigure(0, weight=1)
        self.session_tab.grid_columnconfigure(1, weight=1)
        self.session_tab.grid_columnconfigure(2, weight=1)
        self.session_name_label = ctk.CTkLabel(self.session_tab, text="Untitled Session", font=ctk.CTkFont(size=20, weight="bold"))
        self.session_name_label.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)
        
        self.api_connection_label = ctk.CTkLabel(self.session_tab, text="API Connection", font=ctk.CTkFont(size=14, weight="bold"))
        self.api_connection_label.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)
        
        ctk.CTkButton(self.session_tab, text="Load LLM Models", command=self.load_models).grid(row=2, column=0, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(self.session_tab, text="Upload Speaker Voice", command=self.upload_speaker_voice).grid(row=2, column=1, padx=5, pady=5, sticky=tk.EW)
        
        self.session_label = ctk.CTkLabel(self.session_tab, text="Session", font=ctk.CTkFont(size=14, weight="bold"))
        self.session_label.grid(row=3, column=0, columnspan=2, padx=5, pady=5, sticky=tk.W)
        
        ctk.CTkButton(self.session_tab, text="New Session", command=self.new_session, fg_color="#2e8b57", hover_color="#3cb371").grid(row=4, column=0, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(self.session_tab, text="Load Session", command=self.load_session).grid(row=4, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(self.session_tab, text="Delete Session", command=self.delete_session, fg_color="dark red", hover_color="red").grid(row=4, column=2, padx=5, pady=5, sticky=tk.EW)

        self.selected_file_label = ctk.CTkLabel(self.session_tab, text="No file selected")
        self.select_file_button = ctk.CTkButton(self.session_tab, text="Select Source File", command=self.select_file)
        self.select_file_button.grid(row=5, column=0, padx=5, pady=5, sticky=tk.EW)
        self.selected_file_label.grid(row=5, column=1, padx=5, pady=5, sticky=tk.W)

        self.language = ctk.StringVar(value="en")
        ctk.CTkLabel(self.session_tab, text="Language:").grid(row=6, column=0, padx=5, pady=5, sticky=tk.W)
        self.language_dropdown = ctk.CTkOptionMenu(self.session_tab, variable=self.language, values=["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"])
        self.language_dropdown.grid(row=6, column=1, padx=5, pady=5, sticky=tk.EW)

        self.selected_speaker = ctk.StringVar(value="")
        ctk.CTkLabel(self.session_tab, text="Speaker Voice:").grid(row=7, column=0, padx=5, pady=5, sticky=tk.W)
        self.speaker_dropdown = ctk.CTkOptionMenu(self.session_tab, variable=self.selected_speaker, values=[])
        self.speaker_dropdown.grid(row=7, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkButton(self.session_tab, text="Start Generation", command=self.start_optimisation_thread, fg_color="#2e8b57", hover_color="#3cb371").grid(row=8, column=0, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(self.session_tab, text="Stop Generation", command=self.stop_generation).grid(row=8, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(self.session_tab, text="Resume Generation", command=self.resume_generation).grid(row=9, column=0, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(self.session_tab, text="Cancel Generation", command=self.cancel_generation, fg_color="dark red", hover_color="red").grid(row=9, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkLabel(self.session_tab, text="Progress:").grid(row=10, column=0, padx=5, pady=5, sticky=tk.W)
        self.progress_label = ctk.CTkLabel(self.session_tab, text="0.00%")
        self.progress_label.grid(row=10, column=1, padx=5, pady=5, sticky=tk.W)
        self.progress_bar = ctk.CTkProgressBar(self.session_tab)
        self.progress_bar.grid(row=11, column=0, columnspan=4, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkLabel(self.session_tab, text="Estimated Remaining Time:").grid(row=12, column=0, padx=5, pady=5, sticky=tk.W)
        self.remaining_time_label = ctk.CTkLabel(self.session_tab, text="N/A")
        self.remaining_time_label.grid(row=12, column=1, padx=5, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.session_tab, text="Generated Sentences", font=ctk.CTkFont(size=14, weight="bold")).grid(row=13, column=0, padx=5, pady=5, sticky=tk.W)
        
        self.play_button = ctk.CTkButton(self.session_tab, text="Play", command=self.toggle_playback, fg_color="#2e8b57", hover_color="#3cb371")
        self.play_button.grid(row=14, column=0, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(self.session_tab, text="Stop", command=self.stop_playback).grid(row=14, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(self.session_tab, text="Play as Playlist", command=self.play_sentences_as_playlist).grid(row=14, column=2, padx=5, pady=5, sticky=tk.EW)

        self.playlist_listbox = tk.Listbox(
            self.session_tab,
            bg="#333333",  
            fg="#FFFFFF",  
            font=("Helvetica", 9),  
            selectbackground="#555555",  
            selectforeground="#FFFFFF",  
            selectborderwidth=0,  
            activestyle="none",  
            highlightthickness=0,  
            bd=0,  
            relief=tk.FLAT
            )        
        self.playlist_listbox.grid(row=15, column=0, columnspan=4, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkButton(self.session_tab, text="Regenerate", command=self.regenerate_selected_sentence).grid(row=16, column=0, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(self.session_tab, text="Remove", command=self.remove_selected_sentences).grid(row=16, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(self.session_tab, text="Save Output", command=self.save_output).grid(row=16, column=2, padx=5, pady=5, sticky=tk.EW)
        
    
        # Text Processing Tab
        self.text_processing_tab = self.tabview.add("Text Processing")
        self.text_processing_tab.grid_columnconfigure(0, weight=1)
        self.text_processing_tab.grid_columnconfigure(1, weight=1)

        ctk.CTkSwitch(self.text_processing_tab, text="Split Long Sentences", variable=self.enable_sentence_splitting).grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(self.text_processing_tab, text="Max Sentence Length:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.text_processing_tab, textvariable=self.max_sentence_length).grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)

        ctk.CTkSwitch(self.text_processing_tab, text="Append Short Sentences", variable=self.enable_sentence_appending).grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkSwitch(self.text_processing_tab, text="Remove Diacritics", variable=self.remove_diacritics).grid(row=3, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkSwitch(self.text_processing_tab, text="Enable LLM Processing", variable=self.enable_llm_processing).grid(row=4, column=0, padx=5, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.text_processing_tab, text="First Prompt").grid(row=5, column=0, padx=5, pady=5, sticky=tk.W)
        self.first_prompt_text = ctk.CTkTextbox(self.text_processing_tab, height=100, width=500, wrap="word")
        self.first_prompt_text.insert("0.0", self.first_optimisation_prompt.get())
        self.first_prompt_text.grid(row=6, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkSwitch(self.text_processing_tab, text="Enable First Prompt", variable=self.enable_first_prompt).grid(row=7, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkSwitch(self.text_processing_tab, text="Enable Evaluation", variable=self.enable_first_evaluation).grid(row=7, column=1, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(self.text_processing_tab, text="First Prompt Model:").grid(row=8, column=0, padx=5, pady=5, sticky=tk.W)
        self.first_prompt_model_dropdown = ctk.CTkOptionMenu(self.text_processing_tab, variable=self.first_prompt_model, values=["default"])
        self.first_prompt_model_dropdown.grid(row=8, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkSwitch(self.text_processing_tab, text="Enable Second Prompt", variable=self.enable_second_prompt).grid(row=9, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(self.text_processing_tab, text="Second Prompt").grid(row=10, column=0, padx=5, pady=5, sticky=tk.W)
        self.second_prompt_text = ctk.CTkTextbox(self.text_processing_tab, height=100, wrap="word")
        self.second_prompt_text.insert("0.0", self.second_optimisation_prompt.get())
        self.second_prompt_text.grid(row=11, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkSwitch(self.text_processing_tab, text="Enable Evaluation", variable=self.enable_second_evaluation).grid(row=12, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(self.text_processing_tab, text="Second Prompt Model:").grid(row=13, column=0, padx=5, pady=5, sticky=tk.W)
        self.second_prompt_model_dropdown = ctk.CTkOptionMenu(self.text_processing_tab, variable=self.second_prompt_model, values=["default"])
        self.second_prompt_model_dropdown.grid(row=13, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkSwitch(self.text_processing_tab, text="Enable Third Prompt", variable=self.enable_third_prompt).grid(row=14, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(self.text_processing_tab, text="Third Prompt").grid(row=15, column=0, padx=5, pady=5, sticky=tk.W)
        self.third_prompt_text = ctk.CTkTextbox(self.text_processing_tab, height=100, wrap="word")
        self.third_prompt_text.insert("0.0", self.third_optimisation_prompt.get())
        self.third_prompt_text.grid(row=16, column=0, columnspan=2, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkSwitch(self.text_processing_tab, text="Enable Evaluation", variable=self.enable_third_evaluation).grid(row=17, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(self.text_processing_tab, text="Third Prompt Model:").grid(row=18, column=0, padx=5, pady=5, sticky=tk.W)
        self.third_prompt_model_dropdown = ctk.CTkOptionMenu(self.text_processing_tab, variable=self.third_prompt_model, values=["default"])
        self.third_prompt_model_dropdown.grid(row=18, column=1, padx=5, pady=5, sticky=tk.EW)

        # Audio Processing Tab
        self.audio_processing_tab = self.tabview.add("Audio Processing")
        self.audio_processing_tab.grid_columnconfigure(0, weight=1)
        self.audio_processing_tab.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.audio_processing_tab, text="RVC", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkSwitch(self.audio_processing_tab, text="Enable RVC", variable=self.enable_rvc).grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkButton(self.audio_processing_tab, text="Select RVC Model", command=self.select_rvc_model).grid(row=2, column=0, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkButton(self.audio_processing_tab, text="Select RVC Index", command=self.select_rvc_index).grid(row=3, column=0, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkLabel(self.audio_processing_tab, text="Appended Silence", font=ctk.CTkFont(size=14, weight="bold")).grid(row=4, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(self.audio_processing_tab, text="Length:").grid(row=5, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.audio_processing_tab, textvariable=self.silence_length).grid(row=5, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkLabel(self.audio_processing_tab, text="Length Paragraph:").grid(row=6, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.audio_processing_tab, textvariable=self.paragraph_silence_length).grid(row=6, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkLabel(self.audio_processing_tab, text="Fade", font=ctk.CTkFont(size=14, weight="bold")).grid(row=7, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkSwitch(self.audio_processing_tab, text="Enable Fade-in and Fade-out", variable=self.enable_fade).grid(row=8, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(self.audio_processing_tab, text="Fade-in Duration:").grid(row=9, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.audio_processing_tab, textvariable=self.fade_in_duration).grid(row=9, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkLabel(self.audio_processing_tab, text="Fade-out Duration:").grid(row=10, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.audio_processing_tab, textvariable=self.fade_out_duration).grid(row=10, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkLabel(self.audio_processing_tab, text="Evaluation", font=ctk.CTkFont(size=14, weight="bold")).grid(row=11, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkSwitch(self.audio_processing_tab, text="Enable Evaluation", variable=self.enable_tts_evaluation).grid(row=12, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(self.audio_processing_tab, text="Target MOS Value:").grid(row=13, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.audio_processing_tab, textvariable=self.target_mos_value).grid(row=13, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkLabel(self.audio_processing_tab, text="Max Attempts:").grid(row=14, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.audio_processing_tab, textvariable=self.max_attempts).grid(row=14, column=1, padx=5, pady=5, sticky=tk.EW)

        ctk.CTkLabel(self.audio_processing_tab, text="Output", font=ctk.CTkFont(size=14, weight="bold")).grid(row=15, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkLabel(self.audio_processing_tab, text="Format:").grid(row=16, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.audio_processing_tab, textvariable=self.output_format).grid(row=16, column=1, padx=5, pady=5, sticky=tk.EW)
        ctk.CTkLabel(self.audio_processing_tab, text="Bitrate:").grid(row=17, column=0, padx=5, pady=5, sticky=tk.W)
        ctk.CTkEntry(self.audio_processing_tab, textvariable=self.bitrate).grid(row=17, column=1, padx=5, pady=5, sticky=tk.EW)

        self.sentence_audio_data = {}  # Dictionary to store sentence audio data

        self.populate_speaker_dropdown()
        self.set_speaker_folder()

    def select_file(self):
        self.source_file = filedialog.askopenfilename(filetypes=[("Text files", "*.txt")])
        if self.source_file:
            file_name = os.path.basename(self.source_file)
            self.selected_file_label.configure(text=file_name)
            
            session_name = self.session_name.get()
            if session_name:
                session_dir = os.path.join("Outputs", session_name)
                os.makedirs(session_dir, exist_ok=True)
                
                # Remove the old text file from the session directory
                txt_files = [file for file in os.listdir(session_dir) if file.endswith(".txt")]
                for file in txt_files:
                    os.remove(os.path.join(session_dir, file))
                
                shutil.copy(self.source_file, session_dir)
                self.source_file = os.path.join(session_dir, file_name)
            
    def set_speaker_folder(self):
        speaker_folder_path = os.path.abspath(self.tts_voices_folder)
        data = {"speaker_folder": speaker_folder_path}
        try:
            response = requests.post("http://localhost:8020/set_speaker_folder", json=data)
            if response.status_code == 200:
                print(f"Speaker folder set to: {speaker_folder_path}")
                self.server_connected = True
            else:
                print(f"Error {response.status_code}: Failed to set speaker folder.")
                self.server_connected = False
        except requests.exceptions.ConnectionError:
            print("Server is offline. Retrying in 5 seconds...")
            self.server_connected = False
            self.master.after(10000, self.set_speaker_folder)

    def populate_speaker_dropdown(self):
        wav_files = [f for f in os.listdir(self.tts_voices_folder) if f.endswith(".wav")]
        speakers = [os.path.splitext(f)[0] for f in wav_files]
        self.speaker_dropdown.configure(values=speakers)

    def check_server_connection(self):
        if not self.server_connected:
            messagebox.showerror("Error", "Server is offline. Cannot start generation.")
            return False
        return True

    def upload_speaker_voice(self):
        wav_file = filedialog.askopenfilename(filetypes=[("WAV files", "*.wav")])
        if wav_file:
            speaker_name = os.path.splitext(os.path.basename(wav_file))[0]
            destination_path = os.path.join(self.tts_voices_folder, f"{speaker_name}.wav")
            shutil.copy(wav_file, destination_path)
            self.populate_speaker_dropdown()  # Refresh the speaker dropdown after uploading
            messagebox.showinfo("Speaker Voice Uploaded", f"The speaker voice '{speaker_name}' has been uploaded successfully.")

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

    def stop_generation(self):
        self.stop_flag = True
        messagebox.showinfo("Generation Stopped", "The generation process will stop after completing the current sentence.")

    def resume_generation(self):
        if self.session_name.get():
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
                messagebox.showerror("Error", "Session JSON file not found.")
        else:
            messagebox.showerror("Error", "Please load a session before resuming generation.")

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

                messagebox.showinfo("Generation Canceled", "The generation process has been canceled, and the session JSON file and WAV files have been removed.")
            except Exception as e:
                messagebox.showerror("Cancellation Error", f"An error occurred while canceling the generation: {str(e)}")
        else:
            messagebox.showinfo("No Generation in Progress", "There is no generation process currently running.")

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
                self.stop_playback()  # Stop any ongoing playback
                pygame.mixer.quit()  # Quit the pygame mixer
                time.sleep(1)  # Wait for a short duration before deleting files
                
                try:
                    shutil.rmtree(session_dir)  # Delete the session directory and its contents
                    messagebox.showinfo("Session Deleted", f"The session '{session_name}' has been deleted.")
                except Exception as e:
                    messagebox.showerror("Deletion Error", f"An error occurred while deleting the session: {str(e)}")

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
                self.first_prompt_model_dropdown.configure(values=model_names)
                self.second_prompt_model_dropdown.configure(values=model_names)
                self.third_prompt_model_dropdown.configure(values=model_names)
                messagebox.showinfo("Models Loaded", "LLM models loaded successfully.")
            else:
                messagebox.showerror("Error", "Failed to load models from the API.")
        except requests.exceptions.ConnectionError:
            messagebox.showerror("Error", "Failed to connect to the LLM API.")

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

        selected_indices = self.playlist_listbox.curselection()
        if not selected_indices:
            messagebox.showerror("Error", "Please select at least one sentence to remove.")
            return

        for index in reversed(selected_indices):
            sentence_text = self.playlist_listbox.get(index)
            sentence_index = index + 1
            wav_filename = os.path.join(session_directory, "Sentence_wavs", f"{session_name}_sentence{sentence_index}.wav")

            self.playlist_listbox.delete(index)

            if sentence_text in self.sentence_audio_data:
                del self.sentence_audio_data[sentence_text]

            if os.path.exists(wav_filename):
                os.remove(wav_filename)

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
        if not self.source_file:
            messagebox.showerror("Error", "Please select a source file.")
            return

        session_name = self.session_name.get()
        if not session_name:
            messagebox.showerror("Error", "Please enter a session name.")
            return

        if not self.check_server_connection():
            return

        # Read the file content into 'text' variable
        with open(self.source_file, 'r', encoding='utf-8') as file:
            text = file.read()
        
        # Preprocess the text and split it into sentences
        preprocessed_sentences = self.preprocess_text(text)
        
        # Save the preprocessed sentences as original sentences to the JSON file
        session_dir = os.path.join("Outputs", self.session_name.get())
        session_dir = f"Outputs/{session_name}"
        os.makedirs(session_dir, exist_ok=True)
        json_filename = os.path.join(session_dir, f"{self.session_name.get()}_sentences.json")
        self.save_json(preprocessed_sentences, json_filename)
        
        # Calculate the total number of sentences
        total_sentences = len(preprocessed_sentences)
        
        # Create a new thread for the optimization process
        self.optimization_thread = threading.Thread(target=self.start_optimisation, args=(total_sentences,))
        self.optimization_thread.start()

    def session_name_exists(self, session_name):
        session_dir = os.path.join("Outputs", session_name)
        return os.path.exists(session_dir)
        
    def pause_playback(self):
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.pause()
        else:
            pygame.mixer.music.unpause()

    def preprocess_text(self, text):
        # Replace single newlines, carriage returns, and tabs with spaces
        text = re.sub(r'(?<!\n)[\n\r\t](?!\n)', ' ', text)
        
        # Find positions of paragraph breaks
        paragraph_breaks = list(re.finditer(r'(?<=[^\n\r\t])[\n\r\t]{2,}', text))
        
        if self.remove_diacritics.get():
            text = ''.join(char for char in text if not unicodedata.combining(char))
            text = unidecode(text)
        
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

    def split_long_sentences(self, sentence_dict):
        sentence = sentence_dict["original_sentence"]
        paragraph = sentence_dict["paragraph"]

        if len(sentence) <= self.max_sentence_length.get():
            return [{"original_sentence": sentence, "split_part": None, "paragraph": paragraph}]

        punctuation_marks = [',', ':', ';']
        min_distance = 30
        best_split_index = None
        min_diff = float('inf')

        for mark in punctuation_marks:
            indices = [i for i, c in enumerate(sentence) if c == mark]
            for index in indices:
                if min_distance <= index <= len(sentence) - min_distance:
                    diff = abs(index - len(sentence) // 2)
                    if diff < min_diff:
                        min_diff = diff
                        best_split_index = index

        if best_split_index is None:
            return [{"original_sentence": sentence, "split_part": None, "paragraph": paragraph}]

        first_part = sentence[:best_split_index + 1].strip()
        second_part = sentence[best_split_index + 1:].strip()

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

        punctuation_marks = [',', ':', ';']
        min_distance = 30
        best_split_index = None
        min_diff = float('inf')

        for mark in punctuation_marks:
            indices = [i for i, c in enumerate(sentence) if c == mark]
            for index in indices:
                if min_distance <= index <= len(sentence) - min_distance:
                    diff = abs(index - len(sentence) // 2)
                    if diff < min_diff:
                        min_diff = diff
                        best_split_index = index

        if best_split_index is None:
            return [sentence_dict]

        first_part = sentence[:best_split_index + 1].strip()
        second_part = sentence[best_split_index + 1:].strip()

        split_sentences = []
        if split_part is None:
            split_part_prefix = 0
        else:
            split_part_prefix = split_part

        split_sentences.append({
            "original_sentence": first_part,
            "split_part": f"{split_part_prefix}a",
            "paragraph": "no"
        })

        if len(second_part) > self.max_sentence_length.get():
            if isinstance(split_part_prefix, int):
                if split_part_prefix == 0 and paragraph == "yes":
                    split_sentences.append({
                        "original_sentence": second_part,
                        "split_part": "1",
                        "paragraph": "yes"
                    })
                else:
                    split_sentences.append({
                        "original_sentence": second_part,
                        "split_part": "1",
                        "paragraph": "no"
                    })
            else:
                if (split_part_prefix.endswith("b") or split_part_prefix == "1") and paragraph == "yes":
                    split_sentences.append({
                        "original_sentence": second_part,
                        "split_part": f"{split_part_prefix}b",
                        "paragraph": "yes"
                    })
                else:
                    split_sentences.append({
                        "original_sentence": second_part,
                        "split_part": f"{split_part_prefix}b",
                        "paragraph": "no"
                    })

            split_sentences.extend(self.split_long_sentences_2({
                "original_sentence": second_part,
                "split_part": f"{split_part_prefix}b" if isinstance(split_part_prefix, str) else "1",
                "paragraph": "yes" if (isinstance(split_part_prefix, int) and split_part_prefix == 0 and paragraph == "yes") or
                                    (isinstance(split_part_prefix, str) and (split_part_prefix.endswith("b") or split_part_prefix == "1") and paragraph == "yes") else "no"
            }))
        else:
            split_sentences.append({
                "original_sentence": second_part,
                "split_part": f"{split_part_prefix}b" if isinstance(split_part_prefix, str) else "1",
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
        session_name = self.session_name.get()
        session_dir = f"Outputs/{session_name}"
        os.makedirs(session_dir, exist_ok=True)
        os.makedirs(os.path.join(session_dir, "Sentence_wavs"), exist_ok=True)

        final_audio = AudioSegment.empty()

        json_filename = os.path.join(session_dir, f"{self.session_name.get()}_sentences.json")
        preprocessed_sentences = self.load_json(json_filename)

        # Initialize lists to store sentence generation times and MOS scores
        sentence_generation_times = []
        mos_scores = []

        # Reset the cancel_flag and delete_session_flag at the start of the generation
        self.cancel_flag = False
        self.delete_session_flag = False

        for sentence_dict in preprocessed_sentences[current_sentence:]:
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

            if sentence_dict.get("tts_generated") == "yes":
                current_sentence += 1
                continue

            sentence_start_time = time.time()

            try:
                # Optimize the sentence
                processed_sentence = self.optimise_sentence(sentence_dict, current_sentence, session_dir)

                if processed_sentence is None or processed_sentence["original_sentence"] == "":
                    current_sentence += 1
                    continue

                # Update the processed_sentence in the preprocessed_sentences list
                if self.enable_llm_processing.get() and "processed_sentence" in processed_sentence:
                    preprocessed_sentences[current_sentence]["processed_sentence"] = processed_sentence["processed_sentence"]

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
                    sentence_output_filename = os.path.join(session_dir, "Sentence_wavs", f"{session_name}_sentence{current_sentence + 1}.wav")
                    best_audio.export(sentence_output_filename, format="wav")

                    # Append the audio to the final concatenated audio
                    final_audio += best_audio

                    # Update the tts_generated flag for the current sentence in the preprocessed_sentences list
                    preprocessed_sentences[current_sentence]["tts_generated"] = "yes"

                # Save the updated preprocessed sentences to the JSON file
                self.save_json(preprocessed_sentences, json_filename)

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

        # Save the final concatenated audio file
        output_filename = os.path.join(session_dir, f"{session_name}.{self.output_format.get()}")
        if self.output_format.get() in ['mp3', 'opus']:
            final_audio.export(output_filename, format=self.output_format.get(), bitrate=self.bitrate.get())
        else:
            final_audio.export(output_filename, format=self.output_format.get())

        # Calculate the average MOS score
        if mos_scores:
            average_mos_score = sum(mos_scores) / len(mos_scores)
            print(f"Average MOS Score: {average_mos_score:.2f}")

    def update_progress_bar(self, progress_percentage):
        self.progress_bar.set(progress_percentage / 100)  # Set the progress bar value
        self.progress_label.configure(text=f"{progress_percentage:.2f}%")  # Update the progress percentage label

    def optimise_sentence(self, sentence_dict, current_sentence, session_dir):
        original_sentence = sentence_dict["original_sentence"]
        paragraph = sentence_dict["paragraph"]
        split_part = sentence_dict.get("split_part")

        if not original_sentence:  # Skip processing if sentence is empty
            return None

        json_filename = os.path.join(session_dir, f"{self.session_name.get()}_sentences.json")

        if os.path.exists(json_filename):
            processed_sentences = self.load_json(json_filename)
            if len(processed_sentences) > current_sentence:
                processed_sentence = processed_sentences[current_sentence]
                if self.enable_llm_processing.get() and processed_sentence.get("processed_sentence") is not None:
                    return processed_sentence
        else:
            processed_sentences = []

        if self.enable_llm_processing.get():  # Check if LLM processing is enabled
            # Remove the paragraph placeholder before sending the text to the API
            original_sentence = original_sentence.replace('<PARAGRAPH_BREAK>', '')

            prompts = [
                (self.first_optimisation_prompt, self.enable_first_evaluation.get(), self.first_prompt_model.get(), 1)
            ]
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

            processed_sentence = {
                "original_sentence": original_sentence,
                "paragraph": paragraph,
                "processed_sentence": processed_sentences_list[-1]["text"],
                "split_part": split_part,
                "tts_generated": "no"
            }
        else:
            processed_sentence = {
                "original_sentence": original_sentence,
                "paragraph": paragraph,
                "split_part": split_part,
                "tts_generated": "no"
            }

        if current_sentence < len(processed_sentences):
            processed_sentences[current_sentence] = processed_sentence
        else:
            processed_sentences.append(processed_sentence)
        self.save_json(processed_sentences, json_filename)

        return processed_sentence

    def split_into_sentences(self, text):
        language = self.language.get()
        splitter = SentenceSplitter(language=language)
        sentences = splitter.split(text)
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
            
            sentence_number = str(sentence_counter)
            sentence_counter += 1
            
            numbered_sentence = {
                "sentence_number": sentence_number,
                "paragraph": paragraph,
                "split_part": split_part,
                "original_sentence": sentence_dict.get("original_sentence"),
                "processed_sentence": sentence_dict.get("processed_sentence"),
                "tts_generated": sentence_dict.get("tts_generated", "no")
            }
            numbered_data.append(numbered_sentence)
        
        with open(filename, 'w') as f:
            json.dump(numbered_data, f, indent=2)
            
    def tts_to_audio(self, text):
        language = self.language.get()
        speaker = self.selected_speaker.get()
        speaker_wav = f"{speaker}.wav"  # Append ".wav" to the speaker name
        
        best_audio = None
        best_mos = -1
        
        for attempt in range(self.max_attempts.get()):
            try:
                data = {
                    "text": text,
                    "speaker_wav": speaker_wav,  # Pass only the speaker WAV file name
                    "language": language
                }
                response = requests.post("http://localhost:8020/tts_to_audio/", json=data)
                
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
                    print(f"Error {response.status_code}: Failed to convert text to audio.")
            except Exception as e:
                print(f"Error in tts_to_audio: {str(e)}")
        
        return best_audio
    def add_silence(self, audio_segment, silence_length_ms):
        silence = AudioSegment.silent(duration=silence_length_ms)
        return audio_segment + silence

    def select_rvc_model(self):
        self.rvc_model_path.set(filedialog.askopenfilename(filetypes=[("Model files", "*.pth")]))

    def select_rvc_index(self):
        self.rvc_index_path.set(filedialog.askopenfilename(filetypes=[("Index files", "*")]))

    def process_with_rvc(self, audio_segment):
        try:
            rvc_model_path = self.rvc_model_path.get()
            rvc_index_path = self.rvc_index_path.get() 

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_input_file:
                temp_input_path = temp_input_file.name
                audio_segment.export(temp_input_path, format="wav")

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_output_file:
                temp_output_path = temp_output_file.name

            api_url = "http://127.0.0.1:8000/infer"
            payload = {
                "input_path": temp_input_path,
                "output_path": temp_output_path,
                "pth_path": rvc_model_path,
                "index_path": rvc_index_path,
                "clean_audio": True,
                "clean_strength": 0.3
            }
            response = requests.post(api_url, json=payload)

            if response.status_code == 200:
                processed_audio_data = AudioSegment.from_file(temp_output_path, format="wav")
                return processed_audio_data
            else:
                logging.error(f"RVC Processing Error: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logging.error(f"RVC Processing Error: {str(e)}")
            return None

        finally:
            if os.path.exists(temp_input_path):
                os.unlink(temp_input_path)
            if os.path.exists(temp_output_path):
                os.unlink(temp_output_path)


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
        if self.channel.get_busy():
            self.channel.stop()
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
        self.playlist_stopped = True
        self.paused = False
        self.playing = False
        self.master.after(10, self.update_play_button_text, "Play")  # Schedule button text update

    def update_play_button_text(self, text):
        if self.play_button is not None:
            self.play_button.configure(text=text)

    def update_playlist(self, processed_sentence):
        sentence_text = processed_sentence["processed_sentence"] if self.enable_llm_processing.get() and "processed_sentence" in processed_sentence else processed_sentence["original_sentence"]
        if sentence_text not in self.playlist_listbox.get(0, tk.END):
            self.playlist_listbox.insert(tk.END, sentence_text)

    def play_selected_sentence(self):
        selected_index = self.playlist_listbox.curselection()
        if selected_index:
            selected_sentence = self.playlist_listbox.get(selected_index)
            sentence_index = self.playlist_listbox.get(0, tk.END).index(selected_sentence)
            session_name = self.session_name.get()
            session_dir = os.path.join("Outputs", session_name)
            wav_filename = os.path.join(session_dir, "Sentence_wavs", f"{session_name}_sentence{sentence_index + 1}.wav")
            if os.path.exists(wav_filename):
                print("WAV file exists")
                sound = pygame.mixer.Sound(wav_filename)
                self.channel.play(sound)
                self.current_sentence = selected_sentence
                self.playing = True
                self.master.after(10, self.update_play_button_text, "Pause")
                self.master.after(math.ceil(sound.get_length() * 1000), self.stop_playback)
            else:
                print("WAV file does not exist")
                messagebox.showinfo("Audio Not Available", "The audio for the selected sentence is not available.")

    def regenerate_selected_sentence(self):
        selected_index = self.playlist_listbox.curselection()
        if selected_index:
            selected_sentence = self.playlist_listbox.get(selected_index)
            session_dir = os.path.join("Outputs", self.session_name.get())
            json_filename = os.path.join(session_dir, f"{self.session_name.get()}_sentences.json")
            processed_sentences = self.load_json(json_filename)
            sentence_dict = next((s for s in processed_sentences if s["processed_sentence"] == selected_sentence or s["original_sentence"] == selected_sentence), None)
            if sentence_dict:
                original_sentence_index = processed_sentences.index(sentence_dict)
                processed_sentence = self.optimise_sentence(sentence_dict, current_sentence=original_sentence_index, session_dir=session_dir)
                if self.enable_llm_processing.get():
                    audio_data = self.tts_to_audio(processed_sentence["processed_sentence"])
                else:
                    audio_data = self.tts_to_audio(processed_sentence["original_sentence"])

                if audio_data is not None:
                    if self.enable_rvc.get():
                        audio_data = self.process_with_rvc(audio_data)

                    if self.enable_fade.get():
                        audio_data = self.apply_fade(audio_data, self.fade_in_duration.get(), self.fade_out_duration.get())

                    silence_length = self.silence_length.get()
                    if processed_sentence.get("split_part") == 0:
                        silence_length = self.silence_length.get() // 4
                    elif processed_sentence.get("split_part") is None and processed_sentence.get("paragraph", "no") == "yes":
                        silence_length = self.paragraph_silence_length.get()
                    if silence_length > 0:
                        audio_data += AudioSegment.silent(duration=silence_length)

                    sentence_output_filename = os.path.join(session_dir, "Sentence_wavs", f"{self.session_name.get()}_sentence{sentence_dict['sentence_number']}.wav")
                    audio_data.export(sentence_output_filename, format="wav")

                    self.sentence_audio_data[processed_sentence["processed_sentence"] if self.enable_llm_processing.get() else processed_sentence["original_sentence"]] = audio_data
                    self.playlist_listbox.delete(selected_index)
                    self.playlist_listbox.insert(selected_index, processed_sentence["processed_sentence"] if self.enable_llm_processing.get() else processed_sentence["original_sentence"])

                    sentence_dict["processed_sentence"] = processed_sentence["processed_sentence"]
                    sentence_dict["original_sentence"] = processed_sentence["original_sentence"]
                    processed_sentences[original_sentence_index] = sentence_dict
                    self.save_json(processed_sentences, json_filename)

    def play_sentences_as_playlist(self):
        sentences = self.playlist_listbox.get(0, tk.END)
        print(f"Sentences in playlist: {sentences}")
        if sentences:
            if pygame.mixer.get_init() is None:
                pygame.mixer.init()
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
            print("Playlist is empty")
            messagebox.showinfo("Playlist Empty", "There are no sentences in the playlist.")
 
    def play_next_sentence_in_playlist(self):
        if not self.playlist_stopped and 0 <= self.playlist_index < self.playlist_listbox.size():
            sentence = self.playlist_listbox.get(self.playlist_index)
            sentence_index = self.playlist_listbox.get(0, tk.END).index(sentence)
            session_name = self.session_name.get()
            session_dir = os.path.join("Outputs", session_name)
            wav_filename = os.path.join(session_dir, "Sentence_wavs", f"{session_name}_sentence{sentence_index + 1}.wav")
            print(f"Next sentence in playlist: {sentence}")
            print(f"Sentence index: {sentence_index}")
            print(f"WAV filename: {wav_filename}")
            if os.path.exists(wav_filename):
                print("WAV file exists")
                pygame.mixer.music.load(wav_filename)
                pygame.mixer.music.play()
                self.current_sentence = sentence
                self.playlist_index += 1
                self.master.after(100, self.check_playlist_playback)
            else:
                print("WAV file does not exist")
                self.playlist_index += 1
                self.play_next_sentence_in_playlist()
        else:
            print("Reached the end of the playlist or playback stopped")
            self.stop_playback()

    def check_playlist_playback(self):
        if not self.paused and not self.playlist_stopped and pygame.mixer.music.get_busy():
            self.master.after(100, self.check_playlist_playback)
        elif not self.paused and not self.playlist_stopped and not pygame.mixer.music.get_busy():
            # This call was causing the playlist to advance too quickly after a resume
            # Adding a condition to ensure it's called only when it's the appropriate time to move to the next sentence
            self.play_next_sentence_in_playlist()
            
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
                    sentence_text = sentence_dict.get("original_sentence")
                    if sentence_text:
                        self.playlist_listbox.insert(tk.END, sentence_text)
                        wav_filename = os.path.join(session_folder, "Sentence_wavs", f"{session_name}_sentence{sentence_number}.wav")
                        if os.path.exists(wav_filename):
                            audio_data = AudioSegment.from_wav(wav_filename)
                            self.sentence_audio_data[sentence_text] = audio_data

                # Load the text file from the session directory
                txt_files = [file for file in os.listdir(session_folder) if file.endswith(".txt")]
                if txt_files:
                    self.source_file = os.path.join(session_folder, txt_files[0])
                    file_name = os.path.basename(self.source_file)
                    self.selected_file_label.configure(text=file_name)
                else:
                    self.source_file = ""
                    self.selected_file_label.configure(text="No file selected")

                messagebox.showinfo("Session Loaded", f"The session '{session_name}' has been loaded.")
            else:
                messagebox.showerror("Error", "Session JSON file not found.")
                 
    def save_output(self):
        session_name = self.session_name.get()
        output_format = self.output_format.get()

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

            sentence_wavs = [os.path.join("Outputs", session_name, "Sentence_wavs", f"{session_name}_sentence{i + 1}.wav") for i in range(self.playlist_listbox.size())]

            final_audio = AudioSegment.empty()

            for wav_file in sentence_wavs:
                if os.path.exists(wav_file):
                    audio_data = AudioSegment.from_file(wav_file, format="wav")
                    final_audio += audio_data

            if output_format == "wav":
                final_audio.export(output_path, format="wav")
            elif output_format == "mp3":
                final_audio.export(output_path, format="mp3", bitrate=self.bitrate.get())
            elif output_format == "opus":
                final_audio.export(output_path, format="opus", bitrate=self.bitrate.get())

            messagebox.showinfo("Output Saved", f"The output file has been saved as {output_filename}")


def main():
    root = ctk.CTk()
    root.iconbitmap("pandrator.ico")
    gui = TTSOptimizerGUI(root)
    root.mainloop()

if __name__ == "main":
    logging.debug("Script started")
main()
