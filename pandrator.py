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
from num2words import num2words
import ffmpeg
from pdftextract import XPdf
import regex
import hasami
import argparse
import concurrent.futures
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TIT2, TALB, TPE1, TCON
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.flac import Picture
from mutagen.id3 import PictureType
import base64
from PIL import Image
import yt_dlp

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

class TextPreprocessor:
    def __init__(self, language_var, max_sentence_length, enable_sentence_splitting, 
                 enable_sentence_appending, remove_diacritics, disable_paragraph_detection, tts_service):
        self.language_var = language_var
        self.max_sentence_length = max_sentence_length
        self.enable_sentence_splitting = enable_sentence_splitting
        self.enable_sentence_appending = enable_sentence_appending
        self.remove_diacritics = remove_diacritics
        self.disable_paragraph_detection = disable_paragraph_detection
        self.tts_service = tts_service
        self.chunk_size = 20000

    def preprocess_text(self, text, pdf_preprocessed, source_file, disable_paragraph_detection):
        if len(text) > self.chunk_size:
            processed_sentences = self.parallel_preprocess_text(text, pdf_preprocessed, source_file, disable_paragraph_detection.get())
        else:
            processed_sentences = self.sequential_preprocess_text(text, pdf_preprocessed, source_file, disable_paragraph_detection.get())

        processed_sentences = self.merge_consecutive_chapters(processed_sentences)

        return processed_sentences

    def parallel_preprocess_text(self, text, pdf_preprocessed, source_file, disable_paragraph_detection):
        chunks = self.split_text_into_chunks(text)
        
        args = (
            self.language_var.get(),
            self.max_sentence_length.get(),
            self.enable_sentence_splitting.get(),
            self.enable_sentence_appending.get(),
            self.remove_diacritics.get(),
            self.tts_service.get()
        )

        with concurrent.futures.ProcessPoolExecutor() as executor:
            # Use enumerate to keep track of the original chunk order
            future_to_index = {executor.submit(self.process_chunk, chunk, pdf_preprocessed, source_file, disable_paragraph_detection, *args): i 
                            for i, chunk in enumerate(chunks)}
            
            # Create a list to store results in the correct order
            processed_chunks = [None] * len(chunks)
            
            for future in concurrent.futures.as_completed(future_to_index):
                index = future_to_index[future]
                processed_chunks[index] = future.result()

        # Flatten the list of processed sentences
        all_processed_sentences = [sentence for chunk in processed_chunks for sentence in chunk]

        # Renumber the sentences
        for i, sentence in enumerate(all_processed_sentences, start=1):
            sentence['sentence_number'] = str(i)

        return all_processed_sentences

    @staticmethod
    def process_chunk(chunk, pdf_preprocessed, source_file, disable_paragraph_detection, language, max_sentence_length, 
                      enable_sentence_splitting, enable_sentence_appending, remove_diacritics, tts_service):
        # Normalize newlines to LF and replace carriage returns with LF
        chunk = re.sub(r'\r\n?', '\n', chunk)

        paragraph_breaks = []

        if not disable_paragraph_detection:
            if pdf_preprocessed:
                paragraph_breaks = list(re.finditer(r'\n', chunk))
            elif not pdf_preprocessed and source_file.endswith(".pdf"):
                chunk = TextPreprocessor.preprocess_text_pdf(chunk)
            elif source_file.endswith("_edited.txt"):
                paragraph_breaks = list(re.finditer(r'\n', chunk))
            else:
                chunk = re.sub(r'(?<!\n)\n(?!\n)', ' ', chunk)
                paragraph_breaks = list(re.finditer(r'\n', chunk))

        # Replace tabs with spaces
        chunk = re.sub(r'\t', ' ', chunk)

        if remove_diacritics:
            chunk = ''.join(char for char in chunk if not unicodedata.combining(char))
            chunk = unidecode(chunk)

        # Additional preprocessing step to handle chapters, section titles, etc.
        chunk = re.sub(r'(^|\n+)([^\n.!?]+)(?=\n+|$)', r'\1\2.', chunk)

        sentences = TextPreprocessor.split_into_sentences(chunk, language, tts_service)

        processed_sentences = []

        for sentence in sentences:
            if not sentence.strip():  # Skip empty sentences
                continue

            is_paragraph = False
            for match in paragraph_breaks:
                preceding_text = chunk[match.start()-15:match.start()]
                sentence_end = sentence[-15:]
                if TextPreprocessor.calculate_similarity(preceding_text, sentence_end) >= 0.8:
                    is_paragraph = True
                    break

            is_chapter = False
            if "[[Chapter]]" in sentence:
                is_chapter = True
                sentence = sentence.replace("[[Chapter]]", "").strip()
                is_paragraph = True  # Mark chapter sentences as paragraphs

            sentence_dict = {
                "original_sentence": sentence,
                "paragraph": "yes" if is_paragraph else "no",
                "chapter": "yes" if is_chapter else "no",
                "split_part": None
            }

            if enable_sentence_splitting:
                split_sentences = TextPreprocessor.split_long_sentences(sentence_dict, max_sentence_length)
                processed_sentences.extend(split_sentences)
            else:
                processed_sentences.append(sentence_dict)

        if enable_sentence_appending:
            processed_sentences = TextPreprocessor.append_short_sentences(processed_sentences, max_sentence_length)

        split_sentences = []
        for sentence_dict in processed_sentences:
            split_sentences.extend(TextPreprocessor.split_long_sentences_2(sentence_dict, max_sentence_length))

        return split_sentences

    def split_text_into_chunks(self, text):
        chunks = []
        total_length = len(text)
        target_chunk_size = total_length // 4
        start = 0

        while start < total_length:
            # Find the next paragraph break after the target chunk size
            end = start + target_chunk_size
            next_para_break = text.find('\n\n', end)
            
            if next_para_break == -1:
                # If no paragraph break is found, this is the last chunk
                chunks.append(text[start:])
                break
            
            # Find the last sentence end before the paragraph break
            last_sentence_end = max(
                text.rfind('. ', start, next_para_break),
                text.rfind('! ', start, next_para_break),
                text.rfind('? ', start, next_para_break)
            )
            
            if last_sentence_end == -1 or last_sentence_end <= start:
                # If no sentence end is found, use the paragraph break
                end = next_para_break + 2
            else:
                # Use the last sentence end + 2 to include the period and space
                end = last_sentence_end + 2
            
            chunks.append(text[start:end])
            start = end

        return chunks

    def sequential_preprocess_text(self, text, pdf_preprocessed, source_file, disable_paragraph_detection):
        return self.process_chunk(text, pdf_preprocessed, source_file, disable_paragraph_detection,
                                  self.language_var.get(), self.max_sentence_length.get(),
                                  self.enable_sentence_splitting.get(), self.enable_sentence_appending.get(),
                                  self.remove_diacritics.get(), self.tts_service.get())

    @staticmethod
    def preprocess_text_pdf(text, remove_double_newlines=False):
        text = regex.sub(r'\r\n|\r', '\n', text)
        text = regex.sub(r'[\x00-\x09\x0B-\x1F\x7F]', '', text)
        
        if remove_double_newlines:
            text = regex.sub(r'(?<![.!?])\n\n', ' ', text)
        else:
            text = regex.sub(r'\n$(?<!\n[ \t]*\n)|(?<!\n[ \t]*)\n(?![ \t]*\n)', ' ', text)
        
        text = regex.sub(r'[ \\t]*\\n[ \\t]*\\n[ \\t]*(?:\\n[ \\t]*){0,2}', '\\n', text)
        text = regex.sub(r' {2,}', ' ', text)
        text = regex.sub(r'(?m)^[ \\t]+', '', text)
        
        return text

    @staticmethod
    def split_into_sentences(text, language, tts_service):
        if tts_service == "XTTS":
            if language == "zh-cn":
                return TextPreprocessor.split_chinese_sentences(text)
            elif language == "ja":
                return hasami.segment_sentences(text)
            else:
                splitter = SentenceSplitter(language=language)
                return splitter.split(text)
        else:  # Silero
            silero_to_simple_lang_codes = {
                "German (v3)": "de", "English (v3)": "en", "English Indic (v3)": "en",
                "Spanish (v3)": "es", "French (v3)": "fr", "Indic (v3)": "hi",
                "Russian (v3.1)": "ru", "Tatar (v3)": "tt", "Ukrainian (v3)": "uk",
                "Uzbek (v3)": "uz", "Kalmyk (v3)": "xal"
            }
            language = silero_to_simple_lang_codes.get(language, "en")
            splitter = SentenceSplitter(language=language)
            return splitter.split(text)

    @staticmethod
    def split_chinese_sentences(text):
        end_punctuation = '。！？…'
        segments = re.split(f'([{end_punctuation}])', text)
        sentences = [''.join(segments[i:i+2]).strip() for i in range(0, len(segments), 2) if segments[i]]
        return sentences

    @staticmethod
    def calculate_similarity(str1, str2):
        return difflib.SequenceMatcher(None, str1, str2).ratio()

    @staticmethod
    def split_long_sentences(sentence_dict, max_sentence_length):
        sentence = sentence_dict["original_sentence"]
        paragraph = sentence_dict["paragraph"]

        if len(sentence) <= max_sentence_length:
            # Return a copy of the original dictionary preserving all values
            return [sentence_dict.copy()]

        punctuation_marks = ['，', '；', '：', '。', '！', '？'] if sentence_dict.get("language") == "zh-cn" else [',', ':', ';', '–']
        conjunction_marks = [' and ', ' or ', 'which'] if sentence_dict.get("language") != "zh-cn" else []
        min_distance = 10 if sentence_dict.get("language") == "zh-cn" else 30

        best_split_index = TextPreprocessor.find_best_split_index(sentence, punctuation_marks, conjunction_marks, min_distance, max_sentence_length)

        if best_split_index is None:
            return [sentence_dict.copy()]

        first_part = sentence[:best_split_index].strip()
        second_part = sentence[best_split_index:].strip()

        # Create copies of the original dictionary for each part and update only relevant fields
        first_part_dict = sentence_dict.copy()
        first_part_dict.update({
            "original_sentence": first_part,
            "split_part": 0,
            "paragraph": "no"
        })

        second_part_dict = sentence_dict.copy()
        second_part_dict.update({
            "original_sentence": second_part,
            "split_part": 1,
            "paragraph": paragraph
        })

        return [first_part_dict, second_part_dict]


    @staticmethod
    def find_best_split_index(sentence, punctuation_marks, conjunction_marks, min_distance, max_sentence_length):
        best_split_index = None
        min_diff = float('inf')

        for mark in punctuation_marks:
            indices = [i for i, c in enumerate(sentence) if c == mark]
            for index in indices:
                if min_distance <= index <= len(sentence) - min_distance:
                    if not (mark == ',' and index > 0 and index < len(sentence) - 1 and 
                            sentence[index-1].isdigit() and sentence[index+1].isdigit()):
                        diff = abs(index - len(sentence) // 2)
                        if diff < min_diff:
                            min_diff = diff
                            best_split_index = index + 1

        if best_split_index is None:
            for mark in conjunction_marks:
                index = sentence.find(mark)
                if min_distance <= index <= len(sentence) - min_distance:
                    best_split_index = index
                    break

        return best_split_index

    @staticmethod
    def split_long_sentences_2(sentence_dict, max_sentence_length):
        sentence = sentence_dict["original_sentence"]
        paragraph = sentence_dict["paragraph"]
        split_part = sentence_dict["split_part"]

        if len(sentence) <= max_sentence_length:
            return [sentence_dict]

        punctuation_marks = ['，', '；', '：', '。', '！', '？'] if sentence_dict.get("language") == "zh-cn" else [',', ':', ';', '–']
        conjunction_marks = [' and ', ' or ', 'which'] if sentence_dict.get("language") != "zh-cn" else []
        min_distance = 10 if sentence_dict.get("language") == "zh-cn" else 30

        best_split_index = TextPreprocessor.find_best_split_index(sentence, punctuation_marks, conjunction_marks, min_distance, max_sentence_length)

        if best_split_index is None:
            return [sentence_dict]

        first_part = sentence[:best_split_index].strip()
        second_part = sentence[best_split_index:].strip()

        split_sentences = []

        split_part_prefix = "0" if split_part is None else str(split_part)

        # Preserve other fields by making a copy of sentence_dict for both parts
        first_part_dict = sentence_dict.copy()
        first_part_dict.update({
            "original_sentence": first_part,
            "split_part": split_part_prefix + "a",
            "paragraph": "no"
        })
        split_sentences.append(first_part_dict)

        if len(second_part) > max_sentence_length:
            second_part_dict = sentence_dict.copy()
            if split_part_prefix == "0" and paragraph == "yes":
                second_part_dict.update({
                    "original_sentence": second_part,
                    "split_part": "1a",
                    "paragraph": "yes"
                })
                split_sentences.extend(TextPreprocessor.split_long_sentences_2(second_part_dict, max_sentence_length))
            else:
                second_part_dict.update({
                    "original_sentence": second_part,
                    "split_part": split_part_prefix + "b",
                    "paragraph": "no" if split_part_prefix == "0" else paragraph
                })
                split_sentences.extend(TextPreprocessor.split_long_sentences_2(second_part_dict, max_sentence_length))
        else:
            second_part_dict = sentence_dict.copy()
            second_part_dict.update({
                "original_sentence": second_part,
                "split_part": split_part_prefix + "b",
                "paragraph": paragraph
            })
            split_sentences.append(second_part_dict)

        return split_sentences


    @staticmethod
    def append_short_sentences(sentence_dicts, max_sentence_length):
        appended_sentences = []
        i = 0
        while i < len(sentence_dicts):
            current_sentence = sentence_dicts[i]

            # Chapter sentences are never modified
            if current_sentence.get("chapter") == "yes":
                appended_sentences.append(current_sentence)
                i += 1
                continue

            # Paragraph sentences: attempt to append to the previous sentence
            if current_sentence.get("paragraph") == "yes":
                if i > 0:
                    prev_sentence = appended_sentences[-1]
                    if prev_sentence.get("chapter") != "yes":
                        combined_text = prev_sentence["original_sentence"] + ' ' + current_sentence["original_sentence"]
                        if len(combined_text) <= max_sentence_length:
                            prev_sentence["original_sentence"] = combined_text
                            prev_sentence["paragraph"] = "yes"
                            i += 1
                            continue
                # If we can't append to the previous sentence, add current sentence as is
                appended_sentences.append(current_sentence)
                i += 1
                continue

            # Try to append to the previous sentence
            if i > 0:
                prev_sentence = appended_sentences[-1]
                if (prev_sentence.get("chapter") != "yes" and
                    prev_sentence.get("paragraph") != "yes"):
                    combined_text = prev_sentence["original_sentence"] + ' ' + current_sentence["original_sentence"]
                    if len(combined_text) <= max_sentence_length:
                        prev_sentence["original_sentence"] = combined_text
                        i += 1
                        continue

            # Try to prepend to the next sentence
            if i < len(sentence_dicts) - 1:
                next_sentence = sentence_dicts[i + 1]
                if (next_sentence.get("chapter") != "yes" and
                    next_sentence.get("paragraph") != "yes"):
                    combined_text = current_sentence["original_sentence"] + ' ' + next_sentence["original_sentence"]
                    if len(combined_text) <= max_sentence_length:
                        # Modify the next sentence and skip it
                        next_sentence["original_sentence"] = combined_text
                        i += 2
                        appended_sentences.append(next_sentence)
                        continue

            # If no appending or prepending occurred, add the current sentence as is
            appended_sentences.append(current_sentence)
            i += 1

        return appended_sentences


    @staticmethod
    def convert_digits_to_words(sentence, language):
        def replace_numbers(match):
            number = match.group(0)
            try:
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

                num2words_lang = silero_to_num2words_lang.get(language, "en")
                return num2words(int(number), lang=num2words_lang)
            except ValueError:
                return number

        return re.sub(r'\d+', replace_numbers, sentence)

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
    
    @staticmethod
    def merge_consecutive_chapters(sentences):
        merged_sentences = []
        i = 0

        while i < len(sentences):
            current_sentence = sentences[i]
            
            # Check if the current sentence is marked as a chapter
            if current_sentence.get("chapter") == "yes":
                merged_sentence_text = current_sentence["original_sentence"].strip()
                
                # Look ahead to merge consecutive chapter sentences
                i += 1
                while i < len(sentences) and sentences[i].get("chapter") == "yes":
                    next_sentence_text = sentences[i]["original_sentence"].strip()
                    
                    # Ensure each sentence ends with punctuation
                    if not merged_sentence_text.endswith(('.', '!', '?')):
                        merged_sentence_text += "."
                    
                    merged_sentence_text += " " + next_sentence_text
                    i += 1

                # After merging, mark the sentence as both a chapter and a paragraph
                merged_sentences.append({
                    "original_sentence": merged_sentence_text.strip(),
                    "paragraph": "yes",  # Mark it as a paragraph
                    "chapter": "yes",     # Retain chapter marking
                    "split_part": None
                })
            else:
                # If it's not a chapter, just add the sentence as-is
                merged_sentences.append(current_sentence)
                i += 1

        return merged_sentences


class TTSOptimizerGUI:
    def __init__(self, master):
        self.master = master
        master.title("Pandrator")
        ctk.set_appearance_mode("dark")  # Set the appearance mode to dark
        self.timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        width = master.winfo_screenwidth()
        height = master.winfo_screenheight()
        geometry = str(width) + "x" + str(height)
        master.geometry(geometry)
        
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
        self.external_server_url = ctk.StringVar()
        self.use_external_server = ctk.BooleanVar(value=False)
        self.external_server_address = ctk.StringVar()
        self.external_server_address.trace_add("write", self.populate_speaker_dropdown)
        self.enable_dubbing = ctk.BooleanVar(value=False)
        self.server_connected = False
        self.remove_double_newlines = ctk.BooleanVar(value=False)
        self.advanced_settings_switch = None
        self.tts_voices_folder = "tts_voices"
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
        self.original_language = ctk.StringVar(value="English")
        self.target_language = ctk.StringVar(value="en")
        self.enable_chain_of_thought = ctk.BooleanVar(value=False)
        self.enable_glossary = ctk.BooleanVar(value=False)
        self.translation_model = ctk.StringVar(value="sonnet")
        self.anthropic_api_key = ctk.StringVar()
        self.openai_api_key = ctk.StringVar()
        self.deepl_api_key = ctk.StringVar()
        self.gemini_api_key = ctk.StringVar()
        self.openrouter_api_key = ctk.StringVar()
        self.selected_video_file = ctk.StringVar()
        self.video_file_selection_label = None
        self.whisperx_language = ctk.StringVar(value="English")
        self.whisperx_model = ctk.StringVar(value="large-v3")
        self.language_var = ctk.StringVar(value="en")
        self.selected_speaker = ctk.StringVar(value="")
        self.rvc_models_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rvc_models")
        os.makedirs(self.rvc_models_dir, exist_ok=True)
        self.rvc_models = self.get_rvc_models()
        self.rvc_models_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rvc_models")
        os.makedirs(self.rvc_models_dir, exist_ok=True)
        self.rvc_models = self.get_rvc_models()
        self.top_k = ctk.StringVar(value="50")
        self.top_p = ctk.StringVar(value="0.9")
        self.temperature = ctk.StringVar(value="0.7")
        self.stop_repetition = ctk.StringVar(value="10")
        self.kvcache = ctk.StringVar(value="0")
        self.sample_batch_size = ctk.StringVar(value="8")
        self.metadata = {"title": "", "album": "", "artist": "", "genre": "", "language": ""}
        self.metadata_title = ctk.StringVar()
        self.metadata_album = ctk.StringVar()
        self.metadata_artist = ctk.StringVar()
        self.metadata_genre = ctk.StringVar()
        self.metadata_language = ctk.StringVar()
        # Bind keyboard and mouse events
        self.master.bind("<space>", self.handle_keyboard_event)
        self.master.bind("<m>", self.mark_sentences_for_regeneration)
        self.master.bind("<M>", self.mark_sentences_for_regeneration)
        self.master.bind("<Button-3>", self.mark_sentences_for_regeneration)
        self.load_metadata()  # Load metadata on startup
        self.current_sentence = None
        self.previous_sentence = None
        self.whisper_languages = [
        'Afrikaans', 'Albanian', 'Amharic', 'Arabic', 'Armenian', 'Assamese', 'Azerbaijani', 'Bashkir', 'Basque', 
        'Belarusian', 'Bengali', 'Bosnian', 'Breton', 'Bulgarian', 'Burmese', 'Cantonese', 'Castilian', 'Catalan', 
        'Chinese', 'Croatian', 'Czech', 'Danish', 'Dutch', 'English', 'Estonian', 'Faroese', 'Finnish', 'Flemish', 
        'French', 'Galician', 'Georgian', 'German', 'Greek', 'Gujarati', 'Haitian', 'Haitian Creole', 'Hausa', 
        'Hawaiian', 'Hebrew', 'Hindi', 'Hungarian', 'Icelandic', 'Indonesian', 'Italian', 'Japanese', 'Javanese', 
        'Kannada', 'Kazakh', 'Khmer', 'Korean', 'Lao', 'Latin', 'Latvian', 'Letzeburgesch', 'Lingala', 'Lithuanian', 
        'Luxembourgish', 'Macedonian', 'Malagasy', 'Malay', 'Malayalam', 'Maltese', 'Maori', 'Marathi', 'Moldavian', 
        'Moldovan', 'Mongolian', 'Myanmar', 'Nepali', 'Norwegian', 'Nynorsk', 'Occitan', 'Panjabi', 'Pashto', 
        'Persian', 'Polish', 'Portuguese', 'Punjabi', 'Pushto', 'Romanian', 'Russian', 'Sanskrit', 'Serbian', 
        'Shona', 'Sindhi', 'Sinhala', 'Sinhalese', 'Slovak', 'Slovenian', 'Somali', 'Spanish', 'Sundanese', 
        'Swahili', 'Swedish', 'Tagalog', 'Tajik', 'Tamil', 'Tatar', 'Telugu', 'Thai', 'Tibetan', 'Turkish', 
        'Turkmen', 'Ukrainian', 'Urdu', 'Uzbek', 'Valencian', 'Vietnamese', 'Welsh', 'Yiddish', 'Yoruba'
    ]
        self.main_frame = ctk.CTkFrame(master)
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # Configure columns to have equal weight AND uniform width
        self.main_frame.grid_columnconfigure(0, weight=1, uniform="group1") # Uniform group
        self.main_frame.grid_columnconfigure(1, weight=1, uniform="group1") # Same uniform group

        # Create left and right frames
        self.left_frame = ctk.CTkFrame(self.main_frame)
        self.right_frame = ctk.CTkFrame(self.main_frame)

        self.left_frame.grid(row=0, column=0, sticky="nsew")
        self.right_frame.grid(row=0, column=1, sticky="nsew")

        # Make sure the main frame's row expands
        self.main_frame.grid_rowconfigure(0, weight=1)

        # Inside left_frame: Use grid for the scrollable frame
        self.left_scrollable_frame = ctk.CTkScrollableFrame(self.left_frame)
        self.left_scrollable_frame.grid(row=0, column=0, sticky="nsew")  # Use grid and sticky
        self.left_frame.grid_rowconfigure(0, weight=1)  # Let the scrollable frame expand vertically
        self.left_frame.grid_columnconfigure(0, weight=1)  # Let the scrollable frame expand horizontally

        self.tabview = ctk.CTkTabview(self.left_scrollable_frame)
        self.tabview.pack(fill=tk.BOTH, expand=True, padx=3, pady=5)

        # Create tabs
        self.create_session_tab()
        self.create_text_processing_tab()
        self.create_audio_processing_tab()
        self.create_api_keys_tab()
        self.create_logs_tab()
        self.create_train_xtts_tab()

        # Create Generated Sentences section in right frame
        self.create_generated_sentences_section()

        # Additional setup
        #self.update_tts_service()
        self.toggle_advanced_tts_settings()
        self.text_preprocessor = TextPreprocessor(self.language_var, self.max_sentence_length,
                                                  self.enable_sentence_splitting, self.enable_sentence_appending,
                                                  self.remove_diacritics, self.disable_paragraph_detection, self.tts_service)
        self.initialize_rvc()

    def create_session_tab(self):
        self.session_tab = self.tabview.add("Session")
        self.session_tab.grid_columnconfigure(0, weight=1, uniform="session_columns")
        self.session_tab.grid_columnconfigure(1, weight=1, uniform="session_columns")
        self.session_tab.grid_columnconfigure(2, weight=1, uniform="session_columns")
        self.session_tab.grid_columnconfigure(3, weight=1, uniform="session_columns")

        self.session_name_label = ctk.CTkLabel(self.session_tab, text="Untitled Session", font=ctk.CTkFont(size=20, weight="bold"))
        self.session_name_label.grid(row=0, column=0, columnspan=4, padx=5, pady=5, sticky=tk.W)

        # Session Section
        ctk.CTkLabel(self.session_tab, text="Session", font=ctk.CTkFont(size=14, weight="bold")).grid(row=1, column=0, columnspan=4, padx=10, pady=10, sticky=tk.W)

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

        # Source File Section
        ctk.CTkLabel(self.session_tab, text="Source File", font=ctk.CTkFont(size=14, weight="bold")).grid(row=3, column=0, columnspan=4, padx=10, pady=10, sticky=tk.W)

        source_file_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        source_file_frame.grid(row=4, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        source_file_frame.grid_columnconfigure(0, weight=1)
        source_file_frame.grid_columnconfigure(1, weight=1)
        source_file_frame.grid_columnconfigure(2, weight=2)

        self.select_file_button = ctk.CTkButton(source_file_frame, text="Select File", command=self.select_file)
        self.select_file_button.grid(row=0, column=0, padx=10, pady=(10, 10), sticky=tk.EW)

        self.paste_text_button = ctk.CTkButton(source_file_frame, text="Paste or Write", command=self.paste_text)
        self.paste_text_button.grid(row=0, column=1, padx=10, pady=(10, 10), sticky=tk.EW)

        self.download_from_url_button = ctk.CTkButton(source_file_frame, text="Download from URL", command=self.download_from_url)
        self.download_from_url_button.grid(row=0, column=2, padx=5, pady=(10, 10), sticky=tk.EW)  # Added URL button

        self.selected_file_label = ctk.CTkLabel(source_file_frame, text="No file selected")
        self.selected_file_label.grid(row=0, column=3, padx=10, pady=(10, 10), sticky=tk.W)

        # TTS Settings Section
        ctk.CTkLabel(self.session_tab, text="TTS Settings", font=ctk.CTkFont(size=14, weight="bold")).grid(row=5, column=0, columnspan=4, padx=10, pady=10, sticky=tk.W)

        session_settings_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        session_settings_frame.grid(row=6, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        session_settings_frame.grid_columnconfigure(0, weight=1)
        session_settings_frame.grid_columnconfigure(1, weight=1)
        session_settings_frame.grid_columnconfigure(2, weight=1)
        session_settings_frame.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(session_settings_frame, text="TTS Service:").grid(row=2, column=0, padx=10, pady=5, sticky=tk.W)
        self.tts_service_dropdown = ctk.CTkOptionMenu(session_settings_frame, variable=self.tts_service, values=["XTTS", "Silero"], command=self.update_tts_service)
        self.tts_service_dropdown.grid(row=2, column=1, padx=10, pady=5, sticky=tk.EW)
        
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
        self.external_server_url_entry.grid_remove()

        ctk.CTkLabel(session_settings_frame, text="Language:").grid(row=6, column=0, padx=10, pady=5, sticky=tk.W)
        self.language_dropdown = ctk.CTkComboBox(
            session_settings_frame,
            variable=self.language_var,
            values=["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"]
        )
        self.language_dropdown.grid(row=6, column=1, padx=10, pady=5, sticky=tk.EW)

        self.language_var.trace_add("write", self.on_language_selected)

        ctk.CTkLabel(session_settings_frame, text="Speaker Voice:").grid(row=7, column=0, padx=10, pady=5, sticky=tk.W)
        self.speaker_dropdown = ctk.CTkOptionMenu(session_settings_frame, variable=self.selected_speaker, values=[])
        self.speaker_dropdown.grid(row=7, column=1, padx=10, pady=5, sticky=tk.EW)

        self.upload_new_voices_button = ctk.CTkButton(session_settings_frame, text="Upload New Voices", command=self.upload_speaker_voice)
        self.upload_new_voices_button.grid(row=7, column=2, padx=10, pady=(10, 10), sticky=tk.EW)
        self.sample_length = ctk.StringVar(value="3")
        self.sample_length_dropdown = ctk.CTkOptionMenu(session_settings_frame, variable=self.sample_length, values=[str(i) for i in range(3, 13)])
        self.sample_length_dropdown.grid(row=7, column=3, padx=10, pady=5, sticky=tk.EW)
        self.sample_length_dropdown.grid_remove()

        ctk.CTkLabel(session_settings_frame, text="Speed:").grid(row=8, column=0, padx=10, pady=5, sticky=tk.W)
        speed_slider = ctk.CTkSlider(session_settings_frame, from_=0.2, to=2.0, number_of_steps=180, variable=self.xtts_speed)
        speed_slider.grid(row=8, column=1, columnspan=2, padx=10, pady=5, sticky=tk.EW)

        self.speed_value_label = ctk.CTkLabel(session_settings_frame, text=f"Speed: {self.xtts_speed.get():.2f}")
        self.speed_value_label.grid(row=8, column=3, padx=10, pady=5, sticky=tk.W)

        speed_slider.configure(command=self.update_speed_label)
        self.show_advanced_tts_settings = ctk.BooleanVar(value=False)
        self.advanced_settings_switch = ctk.CTkSwitch(session_settings_frame, text="Advanced TTS Settings", variable=self.show_advanced_tts_settings, command=self.toggle_advanced_tts_settings)
        self.advanced_settings_switch.grid(row=9, column=0, padx=5, pady=5, sticky=tk.W)

        self.create_xtts_advanced_settings_frame()

        # Dubbing Section
        self.dubbing_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        self.dubbing_frame.grid(row=7, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
        self.dubbing_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.dubbing_frame.grid_remove()
        ctk.CTkLabel(self.dubbing_frame, text="Dubbing", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, columnspan=4, padx=10, pady=10, sticky=tk.W)

        # Transcription Options Frame
        self.transcription_frame = ctk.CTkFrame(self.dubbing_frame, fg_color="gray20", corner_radius=10)
        self.transcription_frame.grid(row=1, column=0, columnspan=5, padx=10, pady=(10, 5), sticky=tk.EW)
        self.transcription_frame.grid_columnconfigure((0, 1, 2, 3, 4), weight=1)

        ctk.CTkLabel(self.transcription_frame, text="Transcription Options:", font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, columnspan=5, padx=10, pady=(5, 5), sticky=tk.W)

        ctk.CTkLabel(self.transcription_frame, text="Language:").grid(row=1, column=0, padx=10, pady=5, sticky=tk.W)
        self.whisperx_language_dropdown = ctk.CTkComboBox(self.transcription_frame, variable=self.whisperx_language, values=self.whisper_languages)
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
        self.original_language_dropdown = ctk.CTkComboBox(self.translation_frame, variable=self.original_language, values=self.whisper_languages)
        self.original_language_dropdown.grid(row=2, column=1, padx=10, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.translation_frame, text="To:").grid(row=2, column=2, padx=10, pady=5, sticky=tk.W)
        self.target_language_dropdown = ctk.CTkOptionMenu(self.translation_frame, variable=self.target_language, values=["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"])
        self.target_language_dropdown.grid(row=2, column=3, padx=10, pady=5, sticky=tk.W)

        self.enable_chain_of_thought_switch = ctk.CTkSwitch(self.translation_frame, text="Enable chain-of-thought (more tokens)", variable=self.enable_chain_of_thought)
        self.enable_chain_of_thought_switch.grid(row=3, column=0, columnspan=2, padx=10, pady=5, sticky=tk.W)

        self.enable_glossary_switch = ctk.CTkSwitch(self.translation_frame, text="Enable glossary", variable=self.enable_glossary)
        self.enable_glossary_switch.grid(row=3, column=2, columnspan=2, padx=10, pady=5, sticky=tk.W)

        self.enable_correction = ctk.BooleanVar(value=False)
        self.enable_correction_switch = ctk.CTkSwitch(self.translation_frame, text="Correct transcription with LLM", variable=self.enable_correction)
        self.enable_correction_switch.grid(row=4, column=0, columnspan=2, padx=10, pady=5, sticky=tk.W)

        self.custom_prompt_button = ctk.CTkButton(self.translation_frame, text="Custom Prompt", command=self.open_custom_prompt_window)
        self.custom_prompt_button.grid(row=4, column=2, columnspan=2, padx=10, pady=5, sticky=tk.W)

        # Add a variable to store the custom prompt
        self.custom_correction_prompt = ctk.StringVar(value="")

        # In the create_train_xtts_tab method or wherever the translation model dropdown is initialized
        ctk.CTkLabel(self.translation_frame, text="Translation/Correction Model:").grid(row=5, column=0, padx=10, pady=5, sticky=tk.W)
        self.translation_model_dropdown = ctk.CTkOptionMenu(self.translation_frame, variable=self.translation_model, values=[
            "haiku", "sonnet", "sonnet thinking", "gpt-4o-mini", "gpt-4o", 
            "gemini-flash", "gemini-flash-thinking", "deepseek-r1", "qwq-32b",
            "deepl", "local"
        ], width=150)
        self.translation_model_dropdown.grid(row=5, column=1, padx=10, pady=5, sticky=tk.W)
        self.translation_model.trace_add("write", self.on_translation_model_change)

        # Video File Selection (for SRT input)
        self.video_file_selection_frame = ctk.CTkFrame(self.dubbing_frame, fg_color="gray20", corner_radius=10)
        self.video_file_selection_frame.grid(row=3, column=0, columnspan=5, padx=10, pady=(10, 5), sticky=tk.EW)
        self.video_file_selection_frame.grid_columnconfigure((0, 1, 2), weight=1)
        self.video_file_selection_frame.grid_remove()

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

        self.only_transcribe_button = ctk.CTkButton(self.dubbing_buttons_frame, text="Only Transcribe", command=lambda: self.only_transcribe(equalize_after=True))
        self.only_transcribe_button.grid(row=0, column=2, padx=5, pady=5, sticky=tk.EW)

        self.only_translate_button = ctk.CTkButton(self.dubbing_buttons_frame, text="Only Translate", command=lambda: self.only_translate(equalize_after=True))
        self.only_translate_button.grid(row=0, column=3, padx=5, pady=5, sticky=tk.EW)


        # Output Options Section
        self.output_options_label = ctk.CTkLabel(self.session_tab, text="Output Options", font=ctk.CTkFont(size=14, weight="bold"))
        self.output_options_label.grid(row=8, column=0, columnspan=6, padx=10, pady=10, sticky=tk.W)

        self.output_options_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        self.output_options_frame.grid(row=9, column=0, columnspan=6, padx=3, pady=(0, 20), sticky=tk.EW)
        for i in range(6):
            self.output_options_frame.grid_columnconfigure(i, weight=1)

        ctk.CTkLabel(self.output_options_frame, width=70, text="Format:").grid(row=0, column=0, padx=3, pady=5, sticky=tk.EW)
        self.output_format = ctk.StringVar(value="m4b")
        self.format_dropdown = ctk.CTkOptionMenu(self.output_options_frame, variable=self.output_format, values=["m4b", "opus", "mp3", "wav"], width=70)
        self.format_dropdown.grid(row=0, column=1, padx=3, pady=5, sticky=tk.W)

        ctk.CTkLabel(self.output_options_frame, width=70, text="Bitrate:").grid(row=0, column=2, padx=3, pady=5, sticky=tk.W)
        self.bitrate = ctk.StringVar(value="64k")
        self.bitrate_dropdown = ctk.CTkOptionMenu(self.output_options_frame, variable=self.bitrate, values=["16k", "32k", "64k", "128k", "196k", "312k"], width=70)
        self.bitrate_dropdown.grid(row=0, column=3, padx=3, pady=5, sticky=tk.W)

        self.upload_cover_button = ctk.CTkButton(self.output_options_frame, text="Upload Cover", command=self.upload_cover)
        self.upload_cover_button.grid(row=0, column=4, padx=3, pady=5, sticky=tk.EW)

        self.metadata_button = ctk.CTkButton(self.output_options_frame, text="Metadata", command=self.open_metadata_window)
        self.metadata_button.grid(row=0, column=5, padx=3, pady=5, sticky=tk.EW)

        # Generation Section
        generation_label = ctk.CTkLabel(self.session_tab, text="Generation", font=ctk.CTkFont(size=14, weight="bold"))
        generation_label.grid(row=10, column=0, padx=10, pady=10, sticky=tk.W)

        generation_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        generation_frame.grid(row=11, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
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

    def open_metadata_window(self):
        self.load_metadata()
        metadata_window = ctk.CTkToplevel(self.master)
        metadata_window.title("Metadata")
        metadata_window.geometry("400x300")
        metadata_window.grab_set()
        metadata_window.transient(self.master)

        self.metadata_title.set(self.metadata.get("title", ""))
        self.metadata_album.set(self.metadata.get("album", ""))
        self.metadata_artist.set(self.metadata.get("artist", ""))
        self.metadata_genre.set(self.metadata.get("genre", ""))
        self.metadata_language.set(self.metadata.get("language", ""))


        ctk.CTkLabel(metadata_window, text="Title:").grid(row=0, column=0, padx=10, pady=5, sticky=tk.W)
        ctk.CTkEntry(metadata_window, textvariable=self.metadata_title).grid(row=0, column=1, padx=10, pady=5, sticky=tk.EW)

        ctk.CTkLabel(metadata_window, text="Album:").grid(row=1, column=0, padx=10, pady=5, sticky=tk.W)
        self.metadata_album = ctk.StringVar(value=self.session_name.get())
        ctk.CTkEntry(metadata_window, textvariable=self.metadata_album).grid(row=1, column=1, padx=10, pady=5, sticky=tk.EW)

        ctk.CTkLabel(metadata_window, text="Artist:").grid(row=2, column=0, padx=10, pady=5, sticky=tk.W)
        ctk.CTkEntry(metadata_window, textvariable=self.metadata_artist).grid(row=2, column=1, padx=10, pady=5, sticky=tk.EW)

        ctk.CTkLabel(metadata_window, text="Genre:").grid(row=3, column=0, padx=10, pady=5, sticky=tk.W)
        ctk.CTkEntry(metadata_window, textvariable=self.metadata_genre).grid(row=3, column=1, padx=10, pady=5, sticky=tk.EW)

        ctk.CTkLabel(metadata_window, text="Language:").grid(row=4, column=0, padx=10, pady=5, sticky=tk.W)
        ctk.CTkEntry(metadata_window, textvariable=self.metadata_language).grid(row=4, column=1, padx=10, pady=5, sticky=tk.EW)

    def open_custom_prompt_window(self):
        prompt_window = ctk.CTkToplevel(self.master)
        prompt_window.title("Custom Correction Prompt")
        prompt_window.geometry("600x400")
        prompt_window.transient(self.master)
        prompt_window.grab_set()

        ctk.CTkLabel(prompt_window, text="Enter additional context for correction (e.g., proper names, terminology):", wraplength=550).pack(padx=10, pady=(10, 5))

        prompt_text = ctk.CTkTextbox(prompt_window, width=580, height=300)
        prompt_text.pack(padx=10, pady=5)
        
        # If there's an existing prompt, show it
        if self.custom_correction_prompt.get():
            prompt_text.insert("1.0", self.custom_correction_prompt.get())

        def save_prompt():
            self.custom_correction_prompt.set(prompt_text.get("1.0", "end-1c"))
            prompt_window.destroy()

        ctk.CTkButton(prompt_window, text="Save", command=save_prompt).pack(pady=10)

        def save_and_close():
            # Save metadata to self.metadata before closing
            self.metadata["title"] = self.metadata_title.get()
            self.metadata["album"] = self.metadata_album.get()
            self.metadata["artist"] = self.metadata_artist.get()
            self.metadata["genre"] = self.metadata_genre.get()
            self.metadata["language"] = self.metadata_language.get()
            self.save_metadata()
            metadata_window.grab_release()
            metadata_window.destroy()

        ctk.CTkButton(metadata_window, text="Save", command=save_and_close).grid(row=5, column=0, columnspan=2, pady=20)

        metadata_window.grid_columnconfigure(1, weight=1)

    def save_metadata(self):
        session_dir = os.path.join("Outputs", self.session_name.get())
        os.makedirs(session_dir, exist_ok=True)
        metadata_file = os.path.join(session_dir, "metadata.json")
        with open(metadata_file, "w") as f:
            json.dump(self.metadata, f)

    def load_metadata(self):
        try:
            session_dir = os.path.join("Outputs", self.session_name.get())
            metadata_file = os.path.join(session_dir, "metadata.json")
            with open(metadata_file, "r") as f:
                self.metadata = json.load(f)

                # Update the StringVars here if they exist
                if hasattr(self, "metadata_title"):
                    self.metadata_title.set(self.metadata.get("title", ""))
                    self.metadata_album.set(self.metadata.get("album", ""))
                    self.metadata_artist.set(self.metadata.get("artist", ""))
                    self.metadata_genre.set(self.metadata.get("genre", ""))
                    self.metadata_language.set(self.metadata.get("language", ""))

        except FileNotFoundError:
            pass

    def create_text_processing_tab(self):
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

    def create_audio_processing_tab(self):
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


    def create_api_keys_tab(self):
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
        
        # Gemini API Key (new)
        ctk.CTkLabel(self.api_keys_tab, text="Gemini API Key:").grid(row=4, column=0, padx=10, pady=10, sticky=tk.W)
        gemini_entry = ctk.CTkEntry(self.api_keys_tab, textvariable=self.gemini_api_key, width=300)
        gemini_entry.grid(row=4, column=1, padx=10, pady=10, sticky=tk.W)
        ctk.CTkButton(self.api_keys_tab, text="Save", command=lambda: self.save_api_key("GEMINI_API_KEY", self.gemini_api_key.get())).grid(row=4, column=2, padx=10, pady=10)
        
        # Openrouter API Key (new)
        ctk.CTkLabel(self.api_keys_tab, text="Openrouter API Key:").grid(row=5, column=0, padx=10, pady=10, sticky=tk.W)
        openrouter_entry = ctk.CTkEntry(self.api_keys_tab, textvariable=self.openrouter_api_key, width=300)
        openrouter_entry.grid(row=5, column=1, padx=10, pady=10, sticky=tk.W)
        ctk.CTkButton(self.api_keys_tab, text="Save", command=lambda: self.save_api_key("OPENROUTER_API_KEY", self.openrouter_api_key.get())).grid(row=5, column=2, padx=10, pady=10)

    def get_rvc_models(self):
        if os.path.exists(self.rvc_models_dir):
            return [folder for folder in os.listdir(self.rvc_models_dir) 
                    if os.path.isdir(os.path.join(self.rvc_models_dir, folder))]
        return []

    def create_logs_tab(self):
        self.logs_tab = self.tabview.add("Logs")
        self.logs_tab.grid_columnconfigure(0, weight=1)
        self.logs_tab.grid_rowconfigure(0, weight=1)

        self.logs_text = ctk.CTkTextbox(self.logs_tab, width=200, height=500)
        self.logs_text.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.update_logs_button = ctk.CTkButton(self.logs_tab, text="Update Logs", command=self.update_logs)
        self.update_logs_button.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="w")
        self.log_update_interval = 60000  # Update every 60 seconds
        self.master.after(0, self.update_logs)

    def create_train_xtts_tab(self):
        self.train_xtts_tab = self.tabview.add("Train XTTS")
        self.train_xtts_tab.grid_columnconfigure(0, weight=1)
        self.train_xtts_tab.grid_columnconfigure(1, weight=1)
        
        # Let's use helper functions to organize the UI into logical sections
        def create_section_header(parent, title, row, pady=(15, 5)):
            """Helper to create consistent section headers"""
            label = ctk.CTkLabel(parent, text=title, font=ctk.CTkFont(size=14, weight="bold"))
            label.grid(row=row, column=0, columnspan=3, padx=10, pady=pady, sticky="w")
            return label
        
        def create_tooltip(widget, message):
            """Helper to create consistent tooltips"""
            CTkToolTip(widget, message=message)
        
        current_row = 0
        
        # 1. Source Audio Section
        create_section_header(self.train_xtts_tab, "Source Audio", current_row)
        current_row += 1
        
        # Source audio path
        source_label = ctk.CTkLabel(self.train_xtts_tab, text="Path to source audio:")
        source_label.grid(row=current_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(source_label, "Select a WAV file or folder containing WAV files for training")
        
        self.source_audio_path = ctk.StringVar()
        self.source_audio_entry = ctk.CTkEntry(self.train_xtts_tab, textvariable=self.source_audio_path, width=300)
        self.source_audio_entry.grid(row=current_row, column=1, padx=10, pady=5, sticky="ew")
        ctk.CTkButton(self.train_xtts_tab, text="Browse", command=self.browse_source_audio).grid(row=current_row, column=2, padx=10, pady=5)
        current_row += 1
        
        # 2. Model Configuration Section
        create_section_header(self.train_xtts_tab, "Model Configuration", current_row)
        current_row += 1
        
        # Create model config frame
        model_frame = ctk.CTkFrame(self.train_xtts_tab)
        model_frame.grid(row=current_row, column=0, columnspan=3, padx=10, pady=5, sticky="ew")
        model_frame.grid_columnconfigure(1, weight=1)
        current_row += 1
        
        model_row = 0
        
        # Model name
        model_label = ctk.CTkLabel(model_frame, text="Model name:")
        model_label.grid(row=model_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(model_label, "Name for your trained model. This will be used to identify the model in the models folder")
        
        self.model_name = ctk.StringVar()
        ctk.CTkEntry(model_frame, textvariable=self.model_name, width=300).grid(row=model_row, column=1, padx=10, pady=5, sticky="ew")
        model_row += 1
        
        # Language Selection
        lang_label = ctk.CTkLabel(model_frame, text="Model language:")
        lang_label.grid(row=model_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(lang_label, "The language of your training data. This will be used for transcription and synthesis")
        
        self.model_language = ctk.StringVar(value="en")
        languages = ["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko"]
        ctk.CTkOptionMenu(model_frame, variable=self.model_language, values=languages).grid(
            row=model_row, column=1, padx=10, pady=5, sticky="ew")
        model_row += 1
        
        # Whisper Model Selection
        whisper_label = ctk.CTkLabel(model_frame, text="Whisper Model:")
        whisper_label.grid(row=model_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(whisper_label, "Model used for transcription. Larger models are more accurate but slower and use more memory")
        
        self.whisper_model = ctk.StringVar(value="large-v3")
        whisper_models = ["medium", "medium.en", "large-v2", "large-v3"]
        ctk.CTkOptionMenu(model_frame, variable=self.whisper_model, values=whisper_models).grid(
            row=model_row, column=1, padx=10, pady=5, sticky="ew")
        model_row += 1
        
        # Sample Rate Selection
        sample_rate_label = ctk.CTkLabel(model_frame, text="Sample Rate:")
        sample_rate_label.grid(row=model_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(sample_rate_label, "Audio sample rate. 22050 Hz is recommended for XTTS training")
        
        self.sample_rate = ctk.IntVar(value=22050)
        ctk.CTkOptionMenu(model_frame, variable=self.sample_rate, values=["22050", "44100"]).grid(
            row=model_row, column=1, padx=10, pady=5, sticky="ew")
        
        # 3. Training Configuration Section
        create_section_header(self.train_xtts_tab, "Training Configuration", current_row)
        current_row += 1
        
        # Create training config frame
        training_frame = ctk.CTkFrame(self.train_xtts_tab)
        training_frame.grid(row=current_row, column=0, columnspan=3, padx=10, pady=5, sticky="ew")
        training_frame.grid_columnconfigure(1, weight=1)
        current_row += 1
        
        training_row = 0
        
        # Maximum Training Segment Duration
        duration_label = ctk.CTkLabel(training_frame, text="Maximum training segment duration:")
        duration_label.grid(row=training_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(duration_label, "Maximum duration in seconds for each training segment")
        
        self.max_duration = ctk.StringVar(value="11")
        ctk.CTkOptionMenu(training_frame, variable=self.max_duration, values=["9", "10", "11", "12", "13", "14"]).grid(
            row=training_row, column=1, padx=10, pady=5, sticky="ew")
        training_row += 1
        
        # Maximum Text Length
        text_length_label = ctk.CTkLabel(training_frame, text="Maximum training segment text length:")
        text_length_label.grid(row=training_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(text_length_label, "Maximum number of characters for each training segment")
        
        self.max_text_length = ctk.StringVar(value="200")
        ctk.CTkOptionMenu(training_frame, variable=self.max_text_length, values=["160", "200", "250"]).grid(
            row=training_row, column=1, padx=10, pady=5, sticky="ew")
        training_row += 1
        
        # Training/Evaluation Split
        split_label = ctk.CTkLabel(training_frame, text="Training/Evaluation split ratio:")
        split_label.grid(row=training_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(split_label, "Ratio of data used for training vs. evaluation (e.g., 9_1 means 90% training, 10% evaluation)")
        
        self.training_split = ctk.StringVar(value="9_1")
        ctk.CTkOptionMenu(training_frame, variable=self.training_split, values=["6_4", "7_3", "8_2", "9_1"]).grid(
            row=training_row, column=1, padx=10, pady=5, sticky="ew")
        training_row += 1
        
        # Method Proportion
        method_label = ctk.CTkLabel(training_frame, text="Maximise/Punctuation methods ratio:")
        method_label.grid(row=training_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(method_label, "Ratio between different text segmentation strategies")
        
        self.method_proportion = ctk.StringVar(value="6_4")
        ctk.CTkOptionMenu(training_frame, variable=self.method_proportion, values=["4_5", "5_5", "6_4", "7_3"]).grid(
            row=training_row, column=1, padx=10, pady=5, sticky="ew")
        training_row += 1
        
        # Sample generation method
        sample_method_label = ctk.CTkLabel(training_frame, text="Sample generation method:")
        sample_method_label.grid(row=training_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(sample_method_label, "Method for preparing training samples:\n"
                    "Mixed - Balanced approach\n"
                    "Maximise Punctuation - Prioritizes sentences with varied punctuation\n"
                    "Punctuation - Only uses sentences with punctuation")
        
        self.sample_method = ctk.StringVar(value="Mixed")
        ctk.CTkOptionMenu(training_frame, variable=self.sample_method, 
                        values=["Mixed", "Maximise Punctuation", "Punctuation"]).grid(
            row=training_row, column=1, padx=10, pady=5, sticky="ew")
        training_row += 1
        
        # Alignment Model
        alignment_label = ctk.CTkLabel(training_frame, text="Custom Alignment Model (Optional):")
        alignment_label.grid(row=training_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(alignment_label, "Optional: Huggingface name of a custom alignment model. Leave empty to use the default model")
        
        self.alignment_model = ctk.StringVar(value="")
        ctk.CTkEntry(training_frame, textvariable=self.alignment_model, width=300).grid(
            row=training_row, column=1, padx=10, pady=5, sticky="ew")
        
        # 4. Voice Sample Options Section
        create_section_header(self.train_xtts_tab, "Voice Sample Options", current_row)
        current_row += 1
        
        # Create voice sample frame
        voice_sample_frame = ctk.CTkFrame(self.train_xtts_tab)
        voice_sample_frame.grid(row=current_row, column=0, columnspan=3, padx=10, pady=5, sticky="ew")
        voice_sample_frame.grid_columnconfigure(1, weight=1)
        current_row += 1
        
        sample_row = 0
        
        # Voice Sample Mode
        sample_mode_label = ctk.CTkLabel(voice_sample_frame, text="Voice Sample Mode:")
        sample_mode_label.grid(row=sample_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(sample_mode_label, "Basic: 2 files in main directory\n"
                    "Extended: specialized samples by characteristic\n"
                    "Dynamic: samples with internal variation")
        
        self.voice_sample_mode = ctk.StringVar(value="basic")
        sample_mode_dropdown = ctk.CTkOptionMenu(voice_sample_frame, variable=self.voice_sample_mode, 
                                            values=["basic", "extended", "dynamic"],
                                            command=self.update_voice_sample_options)
        sample_mode_dropdown.grid(row=sample_row, column=1, padx=10, pady=5, sticky="ew")
        sample_row += 1
        
        # Number of Voice Samples (only relevant for extended/dynamic modes)
        self.voice_samples_label = ctk.CTkLabel(voice_sample_frame, text="Number of Voice Samples:")
        self.voice_samples_label.grid(row=sample_row, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(self.voice_samples_label, "Number of reference voice samples to save (only for extended/dynamic modes)")
        
        self.voice_samples_count = ctk.StringVar(value="3")
        self.voice_samples_dropdown = ctk.CTkOptionMenu(voice_sample_frame, variable=self.voice_samples_count, 
                                                    values=["3", "4"])
        self.voice_samples_dropdown.grid(row=sample_row, column=1, padx=10, pady=5, sticky="ew")
        sample_row += 1
        
        # Only use complete sentences option
        self.voice_sample_only_sentence = ctk.BooleanVar(value=False)
        self.complete_sentences_switch = ctk.CTkSwitch(voice_sample_frame, 
                                                text="Only use complete sentences for voice samples", 
                                                variable=self.voice_sample_only_sentence)
        self.complete_sentences_switch.grid(row=sample_row, column=0, columnspan=2, padx=10, pady=5, sticky="w")
        create_tooltip(self.complete_sentences_switch, "Only select samples that start with capital letters and end with punctuation")
        
        # Initially hide options that are only relevant for extended/dynamic modes
        if self.voice_sample_mode.get() == "basic":
            self.voice_samples_label.grid_remove()
            self.voice_samples_dropdown.grid_remove()
        
        # 5. Training Parameters Section
        create_section_header(self.train_xtts_tab, "Training Parameters", current_row)
        current_row += 1
        
        # Create params frame with sliders
        params_frame = ctk.CTkFrame(self.train_xtts_tab)
        params_frame.grid(row=current_row, column=0, columnspan=3, padx=10, pady=5, sticky="ew")
        params_frame.grid_columnconfigure(1, weight=1)
        current_row += 1
        
        params_row = 0
        
        def create_slider_with_label(parent, label_text, variable, from_val, to_val, steps, row, tooltip_text=None):
            """Helper function to create a consistent slider with label and value display"""
            label = ctk.CTkLabel(parent, text=label_text)
            label.grid(row=row, column=0, padx=10, pady=5, sticky="w")
            
            if tooltip_text:
                create_tooltip(label, tooltip_text)
                
            slider = ctk.CTkSlider(parent, from_=from_val, to=to_val, number_of_steps=steps, variable=variable)
            slider.grid(row=row, column=1, padx=10, pady=5, sticky="ew")
            
            value_label = ctk.CTkLabel(parent, textvariable=variable)
            value_label.grid(row=row, column=2, padx=10, pady=5)
            
            return label, slider, value_label
        
        # Epochs
        self.epochs = ctk.IntVar(value=6)
        create_slider_with_label(
            params_frame, "Epochs:", self.epochs, 1, 100, 99, params_row,
            "Number of training cycles. More epochs can improve quality but may lead to overfitting"
        )
        params_row += 1
        
        # Batches
        self.batches = ctk.IntVar(value=2)
        create_slider_with_label(
            params_frame, "Batches:", self.batches, 1, 10, 9, params_row,
            "Number of samples processed together. Larger batch sizes can speed up training but use more memory"
        )
        params_row += 1
        
        # Gradient Accumulation
        self.gradient = ctk.IntVar(value=1)
        create_slider_with_label(
            params_frame, "Gradient Accumulation Levels:", self.gradient, 1, 100, 19, params_row,
            "Number of batches to accumulate before updating model weights. Higher values can simulate larger batch sizes with less memory"
        )
        
        # 6. Audio Preprocessing Section
        create_section_header(self.train_xtts_tab, "Audio Preprocessing", current_row)
        current_row += 1
        
        # Create preprocessing frame with toggles
        preprocess_frame = ctk.CTkFrame(self.train_xtts_tab)
        preprocess_frame.grid(row=current_row, column=0, columnspan=3, padx=10, pady=5, sticky="ew")
        current_row += 1
        
        # Create two columns for better layout
        preprocess_frame.grid_columnconfigure(0, weight=1)
        preprocess_frame.grid_columnconfigure(1, weight=1)
        
        # Left column - first row
        # Denoise
        self.enable_denoise = ctk.BooleanVar(value=False)
        denoise_switch = ctk.CTkSwitch(preprocess_frame, text="Denoise (DeepFilterNet3)", variable=self.enable_denoise)
        denoise_switch.grid(row=0, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(denoise_switch, "Apply AI-based noise removal to clean up audio")
        
        # Right column - first row
        # Breath Removal
        self.enable_breath_removal = ctk.BooleanVar(value=False)
        breath_switch = ctk.CTkSwitch(preprocess_frame, text="Remove breath sounds", variable=self.enable_breath_removal)
        breath_switch.grid(row=0, column=1, padx=10, pady=5, sticky="w")
        create_tooltip(breath_switch, "Apply breath removal preprocessing to improve voice quality")
        
        # De-ess (second row, left)
        self.enable_dess = ctk.BooleanVar(value=False)
        dess_switch = ctk.CTkSwitch(preprocess_frame, text="De-ess", variable=self.enable_dess)
        dess_switch.grid(row=1, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(dess_switch, "Reduce sibilance (harsh 's' sounds) in audio")
        
        # Normalize and Compress in separate frames
        normalize_frame = ctk.CTkFrame(preprocess_frame)
        normalize_frame.grid(row=2, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        
        self.enable_normalize = ctk.BooleanVar(value=False)
        normalize_switch = ctk.CTkSwitch(normalize_frame, text="Normalize", variable=self.enable_normalize)
        normalize_switch.grid(row=0, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(normalize_switch, "Normalize audio levels to a standard loudness")
        
        self.lufs_value = ctk.StringVar(value="-16")
        lufs_entry = ctk.CTkEntry(normalize_frame, textvariable=self.lufs_value, width=50)
        lufs_entry.grid(row=0, column=1, padx=10, pady=5, sticky="w")
        create_tooltip(lufs_entry, "Target LUFS level (industry standard is -16 to -14)")
        
        ctk.CTkLabel(normalize_frame, text="LUFS").grid(row=0, column=2, padx=5, pady=5, sticky="w")
        
        # Compress
        compress_frame = ctk.CTkFrame(preprocess_frame)
        compress_frame.grid(row=3, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        
        self.enable_compress = ctk.BooleanVar(value=False)
        compress_switch = ctk.CTkSwitch(compress_frame, text="Compress", variable=self.enable_compress)
        compress_switch.grid(row=0, column=0, padx=10, pady=5, sticky="w")
        create_tooltip(compress_switch, "Apply dynamic range compression to even out volume levels")
        
        self.compress_profile = ctk.StringVar(value="neutral")
        compress_menu = ctk.CTkOptionMenu(compress_frame, variable=self.compress_profile, values=["male", "female", "neutral"])
        compress_menu.grid(row=0, column=1, padx=10, pady=5, sticky="w")
        create_tooltip(compress_menu, "Compression profile optimized for different voice types")
        
        # 7. Training Control Section
        create_section_header(self.train_xtts_tab, "Training Control", current_row, pady=(20, 10))
        current_row += 1
        
        # Training button with status
        self.train_button = ctk.CTkButton(
            self.train_xtts_tab, 
            text="Start Training", 
            command=self.start_xtts_training,
            height=40,
            font=ctk.CTkFont(size=14, weight="bold")
        )
        self.train_button.grid(row=current_row, column=0, columnspan=3, padx=20, pady=10, sticky="ew")
        current_row += 1
        
        # Progress bar for training
        self.training_progress = ctk.CTkProgressBar(self.train_xtts_tab)
        self.training_progress.grid(row=current_row, column=0, columnspan=3, padx=20, pady=(0, 10), sticky="ew")
        self.training_progress.set(0)  # Initially set to 0
        current_row += 1
        
        # Training status label
        self.training_status = ctk.StringVar(value="Ready to train")
        status_label = ctk.CTkLabel(
            self.train_xtts_tab, 
            textvariable=self.training_status,
            font=ctk.CTkFont(size=12)
        )
        status_label.grid(row=current_row, column=0, columnspan=3, padx=10, pady=(0, 10))
        
        # Update the update_voice_sample_options method if it doesn't exist
        if not hasattr(self, 'update_voice_sample_options'):
            def update_voice_sample_options(self, choice=None):
                """Update visibility of voice sample options based on selected mode"""
                if self.voice_sample_mode.get() == "basic":
                    self.voice_samples_label.grid_remove()
                    self.voice_samples_dropdown.grid_remove()
                else:
                    self.voice_samples_label.grid()
                    self.voice_samples_dropdown.grid()
            
            self.update_voice_sample_options = update_voice_sample_options

    def create_generated_sentences_section(self):
        generated_sentences_frame = ctk.CTkFrame(self.right_frame, fg_color="transparent")
        generated_sentences_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        ctk.CTkLabel(generated_sentences_frame, text="Generated Sentences", font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(0, 10))

        # Top buttons
        top_button_frame = ctk.CTkFrame(generated_sentences_frame)
        top_button_frame.pack(fill=tk.X, pady=(0, 10))

        self.play_button = ctk.CTkButton(top_button_frame, text="Play", command=self.toggle_playback, fg_color="#2e8b57", hover_color="#3cb371")
        self.play_button.pack(side=tk.LEFT, padx=(0, 5), expand=True, fill=tk.X)

        ctk.CTkButton(top_button_frame, text="Play as Playlist", command=self.play_sentences_as_playlist).pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)

        ctk.CTkButton(top_button_frame, text="Stop", command=self.stop_playback).pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)

        ctk.CTkButton(top_button_frame, text="Save Output", command=self.save_output).pack(side=tk.LEFT, padx=(5, 0), expand=True, fill=tk.X)

        # Create a frame to hold both listboxes vertically
        listboxes_frame = ctk.CTkFrame(generated_sentences_frame)
        listboxes_frame.pack(fill=tk.BOTH, expand=True, pady=10)

        # Main listbox (Generated Sentences)
        main_listbox_frame = ctk.CTkFrame(listboxes_frame)
        main_listbox_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 5))

        ctk.CTkLabel(main_listbox_frame, text="All Sentences", font=ctk.CTkFont(size=12, weight="bold")).pack(pady=(0, 5))

        self.playlist_listbox = tk.Listbox(
            main_listbox_frame,
            bg="#444444",
            fg="#FFFFFF",
            font=("Helvetica", 9),
            selectbackground="#4B0082",
            selectforeground="#FFFFFF",
            selectborderwidth=0,
            activestyle="none",
            highlightthickness=0,
            bd=0,
            relief=tk.FLAT
        )
        self.playlist_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        main_scrollbar = ctk.CTkScrollbar(main_listbox_frame, orientation="vertical", command=self.playlist_listbox.yview)
        main_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.playlist_listbox.configure(yscrollcommand=main_scrollbar.set)

        # Marked listbox (Marked for Regeneration)
        marked_listbox_frame = ctk.CTkFrame(listboxes_frame)
        marked_listbox_frame.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True, pady=(5, 0))

        ctk.CTkLabel(marked_listbox_frame, text="Marked for Regeneration", font=ctk.CTkFont(size=12, weight="bold")).pack(pady=(0, 5))

        self.marked_listbox = tk.Listbox(
            marked_listbox_frame,
            bg="#444444",
            fg="#FFFFFF",
            font=("Helvetica", 9),
            selectbackground="#4B0082",
            selectforeground="#FFFFFF",
            selectborderwidth=0,
            activestyle="none",
            highlightthickness=0,
            bd=0,
            relief=tk.FLAT
        )
        self.marked_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        marked_scrollbar = ctk.CTkScrollbar(marked_listbox_frame, orientation="vertical", command=self.marked_listbox.yview)
        marked_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.marked_listbox.configure(yscrollcommand=marked_scrollbar.set)

        # Bottom buttons
        bottom_button_frame = ctk.CTkFrame(generated_sentences_frame)
        bottom_button_frame.pack(fill=tk.X, pady=(10, 0))

        ctk.CTkButton(bottom_button_frame, text="Mark", command=self.mark_for_regeneration).pack(side=tk.LEFT, padx=(0, 5), expand=True, fill=tk.X)
        ctk.CTkButton(bottom_button_frame, text="Unmark", command=self.unmark_for_regeneration).pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        ctk.CTkButton(bottom_button_frame, text="Regenerate", command=self.regenerate_selected_sentence).pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        ctk.CTkButton(bottom_button_frame, text="Regenerate All", command=self.regenerate_all_sentences).pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        ctk.CTkButton(bottom_button_frame, text="Remove", command=self.remove_selected_sentences).pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        ctk.CTkButton(bottom_button_frame, text="Edit", command=self.edit_selected_sentence).pack(side=tk.LEFT, padx=(5, 0), expand=True, fill=tk.X)


    def create_xtts_advanced_settings_frame(self):
        self.xtts_advanced_settings_frame = ctk.CTkFrame(self.session_tab, fg_color="gray20", corner_radius=10)
        self.xtts_advanced_settings_frame.grid(row=7, column=0, columnspan=4, padx=10, pady=(0, 20), sticky=tk.EW)
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

    def show_preprocessing_popup(self):
        self.preprocessing_popup = ctk.CTkToplevel(self.master)
        self.preprocessing_popup.title("Preprocessing")
        self.preprocessing_popup.geometry("300x100")
        self.preprocessing_popup.transient(self.master)
        self.preprocessing_popup.grab_set()
        
        message = ctk.CTkLabel(self.preprocessing_popup, text="Preprocessing text.\nThis may take several minutes...")
        message.pack(expand=True)

        # Center the popup
        self.preprocessing_popup.update_idletasks()
        width = self.preprocessing_popup.winfo_width()
        height = self.preprocessing_popup.winfo_height()
        x = (self.preprocessing_popup.winfo_screenwidth() // 2) - (width // 2)
        y = (self.preprocessing_popup.winfo_screenheight() // 2) - (height // 2)
        self.preprocessing_popup.geometry('{}x{}+{}+{}'.format(width, height, x, y))

    def close_preprocessing_popup(self):
        if hasattr(self, 'preprocessing_popup'):
            self.preprocessing_popup.grab_release()
            self.preprocessing_popup.destroy()

    def browse_source_audio(self):
        choice = messagebox.askquestion("Input Type", "Do you want to select a folder?", icon='question')
        
        if choice == 'yes':
            # Folder selection
            path = filedialog.askdirectory(title="Select Audio Folder")
        else:
            # File selection
            filetypes = [
                ("Audio Files", "*.wav *.mp3 *.ogg *.flac *.m4a *.aac *.wma *.aiff"),
                ("All Files", "*.*")
            ]
            path = filedialog.askopenfilename(title="Select Audio File", filetypes=filetypes)
        
        if path:
            self.source_audio_path.set(path)
            self.source_audio_entry.delete(0, tk.END)
            self.source_audio_entry.insert(0, path)
            print(f"Selected path: {path}")

    def start_xtts_training(self):
        if not self.source_audio_path.get() or not self.model_name.get():
            CTkMessagebox(title="Error", message="Please provide both source audio path and model name.", icon="cancel")
            return

        self.training_status.set("Training in progress...")
        self.train_button.configure(state="disabled")

        # Start the training process in a separate thread
        threading.Thread(target=self.run_xtts_training, daemon=True).start()
              
    def update_voice_sample_options(self, *args):
        mode = self.voice_sample_mode.get()
        if mode == "basic":
            self.voice_samples_label.grid_remove()
            self.voice_samples_dropdown.grid_remove()
        else:  # extended or dynamic
            self.voice_samples_label.grid()
            self.voice_samples_dropdown.grid()

    def run_xtts_training(self):
        try:
            easy_xtts_trainer_dir = os.path.abspath("../easy_xtts_trainer")
            source_path = self.source_audio_path.get()
            
            # Verify we have a session name
            if not self.model_name.get():
                raise ValueError("Model name is required")
                
            command = [
                "../conda/Scripts/conda.exe", "run", "-p", "../conda/envs/easy_xtts_trainer", '--no-capture-output', "python", "easy_xtts_trainer.py",
                "--input", source_path,  # Input path must be first
                "--source-language", self.model_language.get(),
                "--whisper-model", self.whisper_model.get(),
                "--session", self.model_name.get(),  # Session name is required
                "--epochs", str(self.epochs.get()),
                "--gradient", str(self.gradient.get()),
                "--batch", str(self.batches.get()),
                "--sample-method", self.sample_method.get().lower().replace(" ", "-"),
                "--sample-rate", str(self.sample_rate.get()),
                "--max-audio-time", str(float(self.max_duration.get())),
                "--max-text-length", str(int(self.max_text_length.get())),
                "--method-proportion", self.method_proportion.get(),
                "--training-proportion", self.training_split.get(),
                "--voice-sample-mode", self.voice_sample_mode.get()
            ]

            # Add voice samples count if not in basic mode
            if self.voice_sample_mode.get() != "basic":
                command.extend(["--voice-samples", self.voice_samples_count.get()])
            
            # Add the sentence-only option if enabled
            if self.voice_sample_only_sentence.get():
                command.append("--voice-sample-only-sentence")

            # Add alignment model parameter if provided
            if self.alignment_model.get().strip():
                command.extend(["--align-model", self.alignment_model.get().strip()])

            # Add optional preprocessing arguments
            if self.enable_denoise.get():
                command.append("--denoise")
                
            # Add breath removal if enabled
            if self.enable_breath_removal.get():
                command.append("--breath")

            if self.enable_normalize.get():
                command.extend(["--normalize", self.lufs_value.get().strip('-')])  # Remove the minus sign

            if self.enable_compress.get():
                command.extend(["--compress", self.compress_profile.get()])

            if self.enable_dess.get():
                command.append("--dess")

            # Log the full command for debugging
            logging.info(f"Executing command: {' '.join(command)}")

            # Change to the easy_xtts_trainer directory
            os.chdir(easy_xtts_trainer_dir)

            # Run the command and capture the output
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True)
            
            for line in process.stdout:
                logging.info(f"XTTS Training: {line.strip()}")
            
            process.wait()

            if process.returncode == 0:
                self.copy_trained_model()
                self.training_status.set("Training completed successfully.")
            else:
                self.training_status.set("Training failed. Check the log for details.")

        except Exception as e:
            logging.error(f"Error during XTTS training: {str(e)}")
            self.training_status.set(f"Error during training: {str(e)}")

        finally:
            # Change back to the original directory
            os.chdir(os.path.dirname(os.path.abspath(__file__)))
            self.master.after(0, lambda: self.train_button.configure(state="normal"))

    def copy_trained_model(self):
        try:
            model_name = self.model_name.get()
            source_dir = os.path.abspath(f"../easy_xtts_trainer/{model_name}/models")
            target_dir = os.path.abspath(f"../xtts-api-server/xtts_models/{model_name}")

            # Create the target directory if it doesn't exist
            os.makedirs(target_dir, exist_ok=True)

            # Check for the existence of the XTTS model folder
            xtts_folder = None
            for attempt in range(10):  # Try 10 times
                for folder in os.listdir(source_dir):
                    if folder.startswith("xtts"):
                        xtts_folder = folder
                        break
                if xtts_folder:
                    break
                time.sleep(5)  # Wait for 5 seconds before the next attempt
                logging.info(f"Attempt {attempt + 1}: Waiting for XTTS model folder to appear...")

            if not xtts_folder:
                raise FileNotFoundError("XTTS model folder not found after multiple attempts")

            # Copy all files and directories except the 'run' folder
            source_xtts_dir = os.path.join(source_dir, xtts_folder)
            for item in os.listdir(source_xtts_dir):
                if item != 'run':
                    s = os.path.join(source_xtts_dir, item)
                    d = os.path.join(target_dir, item)
                    if os.path.isdir(s):
                        shutil.copytree(s, d, dirs_exist_ok=True)
                    else:
                        shutil.copy2(s, d)

            logging.info(f"Trained model copied to {target_dir}")
            self.training_status.set("Training completed and model copied successfully.")
        except Exception as e:
            error_msg = f"Error copying trained model: {str(e)}"
            logging.error(error_msg)
            self.training_status.set(error_msg)

    def mark_for_regeneration(self):
        selected_indices = self.playlist_listbox.curselection()
        for index in selected_indices:
            sentence = self.playlist_listbox.get(index)
            if sentence not in self.marked_listbox.get(0, tk.END):
                self.marked_listbox.insert(tk.END, sentence)
                sentence_number = sentence.split(']')[0][1:]  # Extract sentence number
                self.mark_sentence_in_json(sentence_number)

    def unmark_for_regeneration(self):
        selected_indices = self.marked_listbox.curselection()
        for index in reversed(selected_indices):
            sentence = self.marked_listbox.get(index)
            self.marked_listbox.delete(index)
            sentence_number = sentence.split(']')[0][1:]  # Extract sentence number
            self.unmark_sentence_in_json(sentence_number)


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

        # Files to check for and remove (EXCLUDING Sentence_wavs)
        files_to_remove = [
            "final_output.mp4", "original_audio.wav", "aligned_audio.wav", 
            "amplified_dubbed_audio.wav", "mixed_audio.wav"
        ] + [f for f in os.listdir(session_dir) if f.endswith("_final.mp4") or f.endswith("_equalized.srt")]

        files_not_removed = []
        for file_pattern in files_to_remove:
            filepath = os.path.join(session_dir, file_pattern)
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                    logging.info(f"Removed file: {filepath}")
                except OSError as e:
                    files_not_removed.append(filepath)
                    logging.error(f"Could not remove {filepath}: {e}")

        if files_not_removed:
            message = "Could not remove the following files. Please close any programs using them and try again:\n" + "\n".join(files_not_removed)
            CTkMessagebox(title="File Removal Error", message=message, icon="warning")
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
            process = subprocess.Popen(
                subdub_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace'
            )
            for line in process.stdout:
                print(line, end='')  # Optionally, integrate with your GUI's output
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
            process = subprocess.Popen(
                equalize_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace'
            )
            for line in process.stdout:
                print(line, end='')  # Optionally, integrate with your GUI's output
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

        # Add the equalized subtitles to the synced video using FFmpeg
        output_video_path = os.path.join(session_dir, f"{session_name}_final.mp4")
        ffmpeg_command = [
            "ffmpeg",
            "-y",  # Overwrite output file if it exists
            "-i", synced_video_path,
            "-i", equalized_srt_path,
            "-c", "copy",
            "-c:s", "mov_text",
            "-metadata:s:s:0", "language=eng",  # Optional: Set subtitle language
            output_video_path
        ]

        logging.info(f"Executing FFmpeg command to add subtitles: {' '.join(ffmpeg_command)}")

        try:
            ffmpeg_process = subprocess.Popen(
                ffmpeg_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace'
            )
            for line in ffmpeg_process.stdout:
                print(line, end='')  # Optionally, integrate with your GUI's output
                logging.info(line.strip())
            ffmpeg_process.wait()
            if ffmpeg_process.returncode != 0:
                raise subprocess.CalledProcessError(ffmpeg_process.returncode, ffmpeg_command)
            logging.info("Subtitles have been successfully embedded into the final video.")

            # Notify the user of success
            CTkMessagebox(
                title="Success",
                message=f"Dubbing and subtitles have been added. The final video is available at:\n{output_video_path}"
            )

        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to add subtitles: {e.output}")
            CTkMessagebox(title="Error", message=f"Failed to add subtitles: {e.output}")
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
        if not self.wait_for_new_file(session_dir, pre_speech_blocks_files, "_speech_blocks.json", "Speech block generation"):
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

    def only_transcribe(self, equalize_after=False):
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

        video_file = os.path.join(session_dir, video_files[0])
        video_filename = os.path.splitext(os.path.basename(video_file))[0]

        logging.info(f"Starting transcription process for video file: {video_file}")

        # Create a WAV file in the session directory
        wav_file = os.path.join(session_dir, f"{video_filename}.wav")

        logging.info(f"WAV file will be created at: {wav_file}")

        try:
            # Convert video to WAV using FFmpeg
            ffmpeg_command = [
                "ffmpeg",
                "-i", video_file,
                "-vn",
                "-acodec", "pcm_s16le",
                "-ac", "1",
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

            if not os.path.exists(wav_file) or os.path.getsize(wav_file) == 0:
                raise FileNotFoundError(f"WAV file is missing or empty: {wav_file}")

            use_int8 = False
            try:
                if torch_available and torch.cuda.is_available():
                    gpu_name = torch.cuda.get_device_name(0).lower()
                    pascal_gpus = ['1060', '1070', '1080', '1660', '1650']
                    use_int8 = any(gpu in gpu_name for gpu in pascal_gpus)
            except Exception as e:
                logging.info(f"GPU check skipped: {str(e)}")
                pass

            def run_whisperx_command(command):
                try:
                    whisperx_process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                    stdout, stderr = whisperx_process.communicate()
                    
                    logging.info(f"Whisperx stdout: {stdout}")
                    if stderr:
                        logging.error(f"Whisperx stderr: {stderr}")
                    
                    if whisperx_process.returncode != 0:
                        if "expects each tensor to be equal size" in stderr:
                            return False
                        raise subprocess.CalledProcessError(whisperx_process.returncode, command, stderr)
                    return True
                except subprocess.CalledProcessError as e:
                    if "expects each tensor to be equal size" in str(e.stderr):
                        return False
                    raise e

            whisperx_command = [
                "../conda/Scripts/conda.exe", "run", "-p", "../conda/envs/whisperx_installer", "--no-capture-output",
                "python", "-m", "whisperx",
                wav_file,
                "--model", self.whisperx_model.get(),
                "--language", self.whisperx_language.get(),
                "--output_format", "srt",
                "--output_dir", session_dir
            ]

            if use_int8:
                whisperx_command.extend(["--compute_type", "int8"])

            logging.info(f"Executing initial transcription command: {' '.join(whisperx_command)}")

            if not run_whisperx_command(whisperx_command):
                logging.info("Initial transcription failed. Retrying with batch_size 1")
                whisperx_command.extend(["--batch_size", "1"])
                logging.info(f"Executing fallback transcription command: {' '.join(whisperx_command)}")
                
                if not run_whisperx_command(whisperx_command):
                    raise Exception("Transcription failed even with batch_size 1")

            logging.info("Transcription completed successfully.")

            # The initial transcribed SRT file
            output_srt = os.path.join(session_dir, f"{video_filename}.srt")

            if not os.path.exists(output_srt):
                raise FileNotFoundError(f"Expected SRT file not found: {output_srt}")

            logging.info(f"SRT file created: {output_srt}")
            current_srt = output_srt  # Keep track of the current SRT file to use

            # If correction is enabled, run it before equalization
            if self.enable_correction.get():
                correction_command = [
                    "python",
                    os.path.abspath("../Subdub/subdub.py"),
                    "-i", output_srt,
                    "-session", session_dir,
                    "-task", "correct",
                    "-context"
                ]
                
                # Add model selection for correction - use the same model as translation
                translation_model = self.translation_model.get()
                
                if translation_model == "deepl":
                    correction_command.extend(["-llmapi", "deepl"])
                elif translation_model == "local":
                    correction_command.extend(["-llmapi", "local"])
                # Handle new model options
                elif translation_model == "sonnet thinking":
                    correction_command.extend(["-llm-model", "sonnet", "-thinking"])
                elif translation_model == "gemini-flash":
                    correction_command.extend(["-llm-model", "gemini-flash"])
                elif translation_model == "gemini-flash-thinking":
                    correction_command.extend(["-llm-model", "gemini-flash-thinking"])
                elif translation_model == "deepseek-r1":
                    correction_command.extend(["-llm-model", "deepseek-r1"])
                elif translation_model == "qwq-32b":
                    correction_command.extend(["-llm-model", "qwq-32b"])
                else:
                    correction_command.extend(["-llm-model", translation_model])

                if self.custom_correction_prompt.get().strip():
                    correction_command.extend(["-correct_prompt", self.custom_correction_prompt.get().strip()])

                try:
                    process = subprocess.Popen(correction_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
                    for line in process.stdout:
                        print(line, end='')
                        logging.info(line.strip())
                    process.wait()
                    if process.returncode != 0:
                        raise subprocess.CalledProcessError(process.returncode, correction_command)
                    
                    # Normalize filename by removing spaces
                    normalized_filename = video_filename.replace(" ", "")
                    corrected_srt = os.path.join(session_dir, f"{normalized_filename}_{self.whisperx_language.get()}_corrected.srt")
                    if not os.path.exists(corrected_srt):
                        raise FileNotFoundError(f"Corrected SRT file not found: {corrected_srt}")
                    
                    output_srt = corrected_srt  # Use the corrected file for subsequent steps
                    logging.info("Correction completed successfully.")
                    logging.info(f"Using corrected file for subsequent steps: {output_srt}")
                except Exception as e:
                    logging.error(f"Correction failed: {str(e)}")
                    CTkMessagebox(title="Correction Failed", message=f"Transcription completed but correction failed: {str(e)}", icon="warning")
                    # Keep using the uncorrected version if correction fails
                    current_srt = output_srt

            if equalize_after:
                equalize_command = [
                    "python",
                    os.path.abspath("../Subdub/subdub.py"),
                    "-i", current_srt,  # Use the most recent SRT file (corrected or original)
                    "-task", "equalize"
                ]

                try:
                    process = subprocess.Popen(equalize_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
                    for line in process.stdout:
                        print(line, end='')
                        logging.info(line.strip())
                    process.wait()
                    if process.returncode == 0:
                        logging.info("Equalization completed successfully.")
                        CTkMessagebox(title="Processing Complete", message="All processing steps completed successfully.", icon="info")
                    else:
                        logging.error("Equalization process failed.")
                        CTkMessagebox(title="Processing Partial", message="Previous steps completed but equalization failed.", icon="warning")
                except Exception as e:
                    logging.error(f"Equalization failed: {str(e)}")
                    CTkMessagebox(title="Processing Partial", message=f"Previous steps completed but equalization failed: {str(e)}", icon="warning")
            else:
                CTkMessagebox(title="Processing Complete", message="Processing completed successfully.", icon="info")

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
            if os.path.exists(wav_file):
                os.remove(wav_file)
                logging.info(f"WAV file removed: {wav_file}")
            else:
                logging.warning(f"WAV file not found for removal: {wav_file}")

    def only_translate(self, equalize_after=False):
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
        enable_chain_of_thought = self.enable_chain_of_thought.get()
        enable_glossary = self.enable_glossary.get()
        translation_model = self.translation_model.get()

        subdub_command = [
            "python",
            os.path.abspath("../Subdub/subdub.py"),
            "-i", most_recent_srt,
            "-session", session_dir,
            "-sl", original_language,
            "-tl", target_language,
            "-task", "translate",
            "-context" 
        ]

        if translation_model == "deepl":
            subdub_command.extend(["-llmapi", "deepl"])
        elif translation_model == "local":
            subdub_command.extend(["-llmapi", "local"])
        # Handle new model options
        elif translation_model == "sonnet thinking":
            subdub_command.extend(["-llm-model", "sonnet", "-thinking"])
        elif translation_model == "gemini-flash":
            subdub_command.extend(["-llm-model", "gemini-flash"])
        elif translation_model == "gemini-flash-thinking":
            subdub_command.extend(["-llm-model", "gemini-flash-thinking"])
        elif translation_model == "deepseek-r1":
            subdub_command.extend(["-llm-model", "deepseek-r1"])
        elif translation_model == "qwq-32b":
            subdub_command.extend(["-llm-model", "qwq-32b"])
        else:
            subdub_command.extend(["-llm-model", translation_model])
        
        if self.enable_chain_of_thought.get(): 
            subdub_command.append("-cot")
        if enable_glossary and translation_model not in ["deepl"]:
            subdub_command.append("-glossary")

        # Add correction if enabled
        if self.enable_correction.get():
            subdub_command.append("-correct")
            # Add custom prompt if provided
            if self.custom_correction_prompt.get().strip():
                subdub_command.extend(["-correct_prompt", self.custom_correction_prompt.get().strip()])

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

            if equalize_after:
                # Find the translated SRT file (it will have the target language code in its name)
                translated_srt_files = [f for f in os.listdir(session_dir) if f.lower().endswith('.srt') and f != os.path.basename(most_recent_srt)]
                if translated_srt_files:
                    translated_srt = os.path.join(session_dir, translated_srt_files[-1])
                    equalize_command = [
                        "python",
                        os.path.abspath("../Subdub/subdub.py"),
                        "-i", translated_srt,
                        "-task", "equalize"
                    ]

                    try:
                        process = subprocess.Popen(equalize_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
                        for line in process.stdout:
                            print(line, end='')
                            logging.info(line.strip())
                        process.wait()
                        if process.returncode == 0:
                            logging.info("Equalization completed successfully.")
                            CTkMessagebox(title="Processing Complete", message="Translation and equalization completed successfully.", icon="info")
                        else:
                            logging.error("Equalization process failed.")
                            CTkMessagebox(title="Processing Partial", message="Translation completed but equalization failed.", icon="warning")
                    except Exception as e:
                        logging.error(f"Equalization failed: {str(e)}")
                        CTkMessagebox(title="Processing Partial", message=f"Translation completed but equalization failed: {str(e)}", icon="warning")
            else:
                CTkMessagebox(title="Translation Complete", message="Subtitles have been translated successfully.")

        except subprocess.CalledProcessError as e:
            logging.error(f"Translation failed: {str(e)}")
            CTkMessagebox(title="Error", message=f"Translation failed: {str(e)}")
        except Exception as e:
            logging.error(f"An unexpected error occurred: {str(e)}")
            CTkMessagebox(title="Error", message=f"An unexpected error occurred: {str(e)}")

    def generate_speech_blocks(self):
        session_name = self.session_name.get()
        session_dir = os.path.abspath(os.path.join("Outputs", session_name))

        # Find the most recent SRT file in the session directory
        srt_files = [f for f in os.listdir(session_dir) if f.lower().endswith('.srt')]
        if not srt_files:
            CTkMessagebox(title="No SRT File", message="No SRT file found in the session folder. Please add an SRT file or perform transcription of a video first.", icon="warning")
            return

        most_recent_srt = max([os.path.join(session_dir, f) for f in srt_files], key=os.path.getmtime)

        subdub_command = [
            "python",
            os.path.abspath("../Subdub/subdub.py"),
            "-i", most_recent_srt,
            "-session", session_dir,
            "-task", "speech_blocks"
        ]

        logging.info(f"Executing speech blocks generation command: {' '.join(subdub_command)}")

        try:
            process = subprocess.Popen(subdub_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            for line in process.stdout:
                print(line, end='')  # Print to console
                logging.info(line.strip())  # Log the output
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, subdub_command)
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
            
            # Get the target path in the session directory
            video_filename = os.path.basename(video_file)
            destination_path = os.path.join(session_dir, video_filename)
            
            # Only copy if the file isn't already in the session directory
            if os.path.dirname(os.path.abspath(video_file)) != os.path.abspath(session_dir):
                os.makedirs(session_dir, exist_ok=True)
                shutil.copy(video_file, destination_path)
            
            # Update the selected video file entry with the path
            self.selected_video_file.set(destination_path)


    def on_translation_model_change(self, *args):
        model = self.translation_model.get()
        if model == "deepl":
            self.enable_chain_of_thought_switch.configure(state="disabled")
            self.enable_glossary_switch.configure(state="disabled")
            self.enable_chain_of_thought.set(False)
            self.enable_glossary.set(False)
        else:
            self.enable_chain_of_thought_switch.configure(state="normal")
            # For Gemini and other non-LLM models, glossary might not be applicable
            if model in ["gemini-flash", "gemini-flash-thinking"]:
                self.enable_glossary_switch.configure(state="disabled")
                self.enable_glossary.set(False)
            else:
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
            filetypes=[("Supported Files", "*.txt *.srt *.pdf *.epub *.docx *.mobi *.mp4 *.mkv *.webm *.avi *.mov"),
                    ("All files", "*.*")]
        )
        
        if self.pre_selected_source_file.lower().endswith((".mp4", ".mkv", ".webm", ".avi", ".mov", ".srt")):
            self.output_options_label.grid_remove()
            self.output_options_frame.grid_remove()
        else:
            self.output_options_label.grid()
            self.output_options_frame.grid()

        if self.pre_selected_source_file.lower().endswith(".pdf"):
            response = CTkMessagebox(
                title="PDF Preprocessing",
                message="Would you like to manually crop the PDF (for example to remove headers and footers) or delete unneeded pages? Please click on Save PDF when you finish editing.",
                icon="question",
                option_1="Ok",
                option_2="Skip"
            ).get()
            
            if response == "Ok":
                source_dir = os.path.dirname(self.pre_selected_source_file)
                source_filename = os.path.splitext(os.path.basename(self.pre_selected_source_file))[0]
                cropped_filename = f"{source_filename}_cropped.pdf"
                cropped_filepath = os.path.join(source_dir, cropped_filename)
                
                try:
                    # Run PyCropPDF
                    logging.info("Starting PyCropPDF GUI...")
                    process = subprocess.Popen([
                        "python", os.path.join("PyCropPDF", "pycroppdf.py"),
                        "--input", os.path.abspath(self.pre_selected_source_file),
                        "--save-to", source_dir,
                        "--save-as", cropped_filename
                    ])
                    
                    # Wait for the PyCropPDF process to complete
                    process.wait()
                    
                    # Check if the cropped PDF file exists and has content
                    if os.path.exists(cropped_filepath) and os.path.getsize(cropped_filepath) > 0:
                        logging.info(f"Cropped PDF saved as: {cropped_filepath}")
                        self.pre_selected_source_file = cropped_filepath
                    else:
                        logging.error(f"Cropped PDF file not found or empty: {cropped_filepath}")
                        CTkMessagebox(title="Error", 
                                    message="Cropped PDF file not found or empty. Using original PDF.", 
                                    icon="warning")
                    
                except Exception as e:
                    logging.error(f"Error running PyCropPDF: {str(e)}")
                    CTkMessagebox(title="Error", 
                                message=f"Error running PyCropPDF: {str(e)}", 
                                icon="cancel")
                    return

        # Continue with the existing file processing logic
        if self.pre_selected_source_file:
            file_name = os.path.basename(self.pre_selected_source_file)
            truncated_file_name = file_name[:15] + "..." if len(file_name) > 15 else file_name
            self.selected_file_label.configure(text=truncated_file_name)

            session_name = self.session_name.get()
            session_dir = os.path.join("Outputs", session_name)
            os.makedirs(session_dir, exist_ok=True)

            # Check if the selected file is from the current session directory
            is_from_session = os.path.dirname(os.path.abspath(self.pre_selected_source_file)) == os.path.abspath(session_dir)

            # Only remove old files if the selected file is from outside the session directory
            if not is_from_session:
                for ext in [".txt", ".srt", ".pdf", ".epub", ".docx", ".mobi"]:
                    for file in os.listdir(session_dir):
                        if file.lower().endswith(ext):
                            os.remove(os.path.join(session_dir, file))
                shutil.copy(self.pre_selected_source_file, session_dir)
                            
            if self.pre_selected_source_file.lower().endswith((".epub")):
                self.process_epub_file(self.pre_selected_source_file)
                # Check if an edited version exists
                edited_filename = os.path.splitext(file_name)[0] + "_edited.txt"
                edited_path = os.path.join(session_dir, edited_filename)
                if os.path.exists(edited_path):
                    self.source_file = edited_path
                else:
                    self.source_file = os.path.join(session_dir, os.path.splitext(file_name)[0] + ".txt")

            elif self.pre_selected_source_file.lower().endswith((".docx", ".mobi")):
                # Convert docx/mobi to txt using ebook-convert
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
                    # If failed, try with ebook-convert.exe from Calibre Portable
                    calibre_portable_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'Calibre Portable', 'Calibre', 'ebook-convert.exe'))
                    if run_ebook_convert([calibre_portable_path, self.pre_selected_source_file, txt_path]):
                        self.master.after(0, self.review_extracted_text, txt_path)
                    else:
                        messagebox.showerror("Error", "Failed to convert using both default and Calibre Portable ebook-convert.")
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
                self.start_generation_button.configure(state=tk.NORMAL)

        else:
            self.pre_selected_source_file = None
            self.selected_file_label.configure(text="No file selected")
            self.dubbing_frame.grid_remove()
            self.video_file_selection_frame.grid_remove()
            self.start_generation_button.configure(state=tk.NORMAL)

        self.pdf_preprocessed = False  # Reset the flag

    def hide_output_section(self):
        if hasattr(self, 'output_options_label'):
            self.output_options_label.grid_remove()
        if hasattr(self, 'output_options_frame'):
            self.output_options_frame.grid_remove()

    def show_output_section(self):
        if hasattr(self, 'output_options_label'):
            self.output_options_label.grid()
        if hasattr(self, 'output_options_frame'):
            self.output_options_frame.grid()

    def download_from_url(self):
        if not self.session_name.get():
            CTkMessagebox(title="No Session", message="Please create or load a session before downloading from URL.", icon="info")
            return

        session_name = self.session_name.get()
        self.session_dir = os.path.join("Outputs", session_name)
        os.makedirs(self.session_dir, exist_ok=True)

        def show_download_popup():
            popup = ctk.CTkToplevel(self.master)
            popup.title("Download from URL")
            popup.geometry("400x150")
            popup.transient(self.master)
            popup.grab_set()

            ctk.CTkLabel(popup, text="Enter YouTube URL:").pack(pady=(20, 5))
            url_entry = ctk.CTkEntry(popup, width=300)
            url_entry.pack(pady=5)

            def start_download():
                url = url_entry.get()
                popup.destroy()  # Close the popup *before* starting the download
                threading.Thread(target=self.download_video, args=(url,), daemon=True).start()

            ctk.CTkButton(popup, text="Download", command=start_download).pack(pady=20)

            # Center the popup
            popup.update_idletasks()
            width = popup.winfo_width()
            height = popup.winfo_height()
            x = (popup.winfo_screenwidth() // 2) - (width // 2)
            y = (popup.winfo_screenheight() // 2) - (height // 2)
            popup.geometry('{}x{}+{}+{}'.format(width, height, x, y))

        show_download_popup()

    def download_video(self, url):
        try:
            ydl_opts = {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'outtmpl': {
                    'default': os.path.join(self.session_dir, '%(title)s.%(ext)s')
                },
                'quiet': True,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise ValueError("Could not extract video info")

                video_title = info['title']
                sanitized_title = ''.join(e for e in video_title if e.isalnum() or e in ['-', '_', ' '])
                ydl_opts['outtmpl']['default'] = os.path.join(self.session_dir, f'{sanitized_title}.%(ext)s')
                
                ydl.download([url])

            downloaded_files = os.listdir(self.session_dir)
            video_file = next((f for f in downloaded_files if f.startswith(sanitized_title)), None)

            if not video_file:
                raise FileNotFoundError("Downloaded video file not found in the session folder.")

            destination_path = os.path.join(self.session_dir, video_file)
            self.master.after(0, lambda: self.load_downloaded_video(destination_path))

        except yt_dlp.utils.DownloadError as e:
            if "requested format not available" in str(e).lower():
                error_msg = "The selected video format is not available. Try a different format or video."
            else:
                error_msg = f"Download error: {e}"
            print(error_msg)
            self.master.after(0, lambda error=error_msg: self.show_error_message(error))
        except Exception as e:
            error_msg = f"An unexpected error occurred during download: {e}"
            print(error_msg)
            self.master.after(0, lambda error=error_msg: self.show_error_message(error))

    def load_api_keys_from_env(self):
        # Load API keys from environment variables if they exist
        self.anthropic_api_key.set(os.environ.get('ANTHROPIC_API_KEY', ''))
        self.openai_api_key.set(os.environ.get('OPENAI_API_KEY', ''))
        self.deepl_api_key.set(os.environ.get('DEEPL_API_KEY', ''))
        self.gemini_api_key.set(os.environ.get('GEMINI_API_KEY', ''))
        self.openrouter_api_key.set(os.environ.get('OPENROUTER_API_KEY', ''))

    def load_downloaded_video(self, destination_path):
        self.pre_selected_source_file = destination_path
        file_name = os.path.basename(self.pre_selected_source_file)
        truncated_file_name = file_name[:15] + "..." if len(file_name) > 15 else file_name
        self.selected_file_label.configure(text=truncated_file_name)

        self.source_file = ""  # For video files, this isn't used, but prevents potential issues

        self.hide_output_section()  # Hide output section for downloaded videos
        self.dubbing_frame.grid()
        self.toggle_transcription_widgets(True)

        self.selected_video_file.set(self.pre_selected_source_file)
        self.video_file_selection_frame.grid_remove()

        CTkMessagebox(title="Download Complete", message="Video downloaded successfully!", icon="info")

    def show_error_message(self, error_message):
        CTkMessagebox(title="Error", message=f"An error occurred: {error_message}", icon="cancel")


    def process_epub_file(self, epub_path):
        try:
            book = epub.read_epub(epub_path)
            chapters = []
            all_html_content = ""
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    filename = item.get_name()
                    if "cover" not in filename.lower() and "toc" not in filename.lower():
                        content = item.get_content().decode('utf-8')
                        all_html_content += content
                        chapters.append(self.extract_chapter_text(content, all_html_content))

            combined_text = "\n\n".join(chapters)
            session_name = self.session_name.get()
            session_dir = os.path.join("Outputs", session_name)
            os.makedirs(session_dir, exist_ok=True)
            txt_filename = os.path.splitext(os.path.basename(epub_path))[0] + ".txt"
            txt_path = os.path.join(session_dir, txt_filename)
            with open(txt_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(combined_text)

            self.source_file = txt_path
            self.selected_file_label.configure(text=txt_filename)

            # Display the converted text for review
            self.master.after(0, self.review_extracted_text, txt_path)

        except Exception as e:
            messagebox.showerror("Error", f"Failed to process EPUB file: {str(e)}")

    def extract_chapter_text(self, html_content, all_html_content=""):
        chapter_text = ""
        soup = BeautifulSoup(html_content, 'html.parser')
        all_soup = BeautifulSoup(all_html_content, 'html.parser')

        all_h1_tags = all_soup.find_all('h1')

        def is_valid_heading(tag):
            ignore_words = ['contents', 'illustrations', 'bibliography', 'toc', 'spis', 'table of contents']
            text = tag.get_text().lower()
            attrs = ' '.join([' '.join(tag.get('class', [])), tag.get('id', '')]).lower()
            return not any(word in text or word in attrs for word in ignore_words)

        def is_chapter_p_tag(tag):
            chapter_markers = ["chapter", "section", "book", "part", "volume"]
            attrs = ' '.join([' '.join(tag.get('class', [])), tag.get('id', '')]).lower()
            return any(marker in attrs for marker in chapter_markers)

        def is_chapter_blockquote(text):
            chapter_markers = ["chapter", "volume", "preface", "prologue", "epilogue", "introduction", "acknowledgments"]
            text_lower = text.lower().strip()
            return any(text_lower.startswith(marker) for marker in chapter_markers) and len(text) <= 70

        def clean_text(text):
            return ' '.join(text.split())  # Remove extra spaces

        def extract_text_from_tag(tag):
            # Remove <br>, <i>, and <b> tags
            for elem in tag(['br', 'i', 'b']):
                elem.unwrap()
            
            if tag.name in ['blockquote', 'p']:
                spans = tag.find_all('span', recursive=False)
                if spans:
                    return clean_text(' '.join(span.get_text().strip() for span in spans if span.get_text().strip()))
                else:
                    return clean_text(tag.get_text().strip())
            elif tag.name == 'span':
                return clean_text(tag.get_text().strip())
            return ""

        if not soup.find(['h1', 'h2', 'h3']):
            # No header tags found, use the new extraction method
            current_text = ""
            last_processed_text = ""
            for tag in soup.find_all(['blockquote', 'p', 'span']):
                text = extract_text_from_tag(tag)
                if not text:
                    continue

                if tag.name in ['blockquote', 'p']:
                    if current_text:
                        if current_text.strip() != last_processed_text:
                            chapter_text += current_text.strip() + "\n\n"
                            last_processed_text = current_text.strip()
                        current_text = ""
                    
                    if text != last_processed_text:
                        if tag.name == 'blockquote' and is_chapter_blockquote(text):
                            chapter_text += "[[Chapter]]" + text + "\n\n"
                        else:
                            chapter_text += text + "\n\n"
                        last_processed_text = text
                elif tag.name == 'span':
                    if text != last_processed_text:
                        if current_text:
                            current_text += " "
                        current_text += text

            # Add any remaining text
            if current_text and current_text.strip() != last_processed_text:
                chapter_text += current_text.strip() + "\n\n"

        elif len(all_h1_tags) < 2:  # Less than two h1 tags in the entire EPUB
            for tag in soup.find_all(['h2', 'h3', 'p']):
                if tag.name == 'h2' and is_valid_heading(tag):
                    chapter_title = clean_text(tag.get_text().strip())
                    chapter_text += "[[Chapter]]" + chapter_title + "\n\n"
                    next_sibling = tag.find_next_sibling()
                    if next_sibling and next_sibling.name == 'h3' and is_valid_heading(next_sibling):
                        subtitle = clean_text(next_sibling.get_text().strip())
                        chapter_text += "[[Chapter]]" + subtitle + "\n\n"
                elif tag.name == 'p':
                    text = clean_text(tag.get_text().strip())
                    if is_chapter_p_tag(tag):
                        chapter_text += "[[Chapter]]" + text + "\n\n"
                    else:
                        chapter_text += text + "\n\n"

        else:  # Two or more h1 tags
            for tag in soup.find_all(['h1', 'h2', 'p']):
                if tag.name == 'h1' and is_valid_heading(tag):
                    chapter_title = clean_text(tag.get_text().strip())
                    chapter_text += "[[Chapter]]" + chapter_title + "\n\n"
                    next_sibling = tag.find_next_sibling()
                    if next_sibling and next_sibling.name == 'h2' and is_valid_heading(next_sibling):
                        subtitle = clean_text(next_sibling.get_text().strip())
                        chapter_text += "[[Chapter]]" + subtitle + "\n\n"
                elif tag.name == 'p':
                    text = clean_text(tag.get_text().strip())
                    if is_chapter_p_tag(tag):
                        chapter_text += "[[Chapter]]" + text + "\n\n"
                    else:
                        chapter_text += text + "\n\n"

        return chapter_text

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
                
                # Show output section for pasted text
                if hasattr(self, 'output_options_label'):
                    self.output_options_label.grid()
                if hasattr(self, 'output_options_frame'):
                    self.output_options_frame.grid()
                
                # Hide dubbing-related elements
                self.dubbing_frame.grid_remove()
                self.video_file_selection_frame.grid_remove()
                
                # Enable the Start Generation button
                self.start_generation_button.configure(state=tk.NORMAL)
                
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
        preprocessed_text = self.text_preprocessor.preprocess_text_pdf(raw_text)
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
        window_width = 1000  # Increased from 800 to 1000
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
            os.makedirs(session_dir, exist_ok=True)

            file_name = os.path.basename(file_path)
            raw_text_filename = os.path.splitext(file_name)[0] + "_raw_text.txt"
            raw_text_path = os.path.join(session_dir, raw_text_filename)

            with open(raw_text_path, "w", encoding="utf-8", newline='\n') as file:
                file.write(text)

            if self.remove_double_newlines.get():
                preprocessed_filename = os.path.splitext(file_name)[0] + "_preprocessed.txt"
                preprocessed_path = os.path.join(session_dir, preprocessed_filename)
                text = self.text_preprocessor.preprocess_text_pdf(text, remove_double_newlines=True)
                with open(preprocessed_path, "w", encoding="utf-8", newline='\n') as file:
                    file.write(text)
                with open(preprocessed_path, "r", encoding="utf-8") as file:
                    updated_text = file.read()
            else:
                text = self.text_preprocessor.preprocess_text_pdf(text, remove_double_newlines=False)
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

        def add_chapter_marker():
            current_position = text_widget.index(tk.INSERT)
            text_widget.insert(current_position, "[[Chapter]]")

        button_frame = ctk.CTkFrame(top_frame)
        button_frame.pack(side=tk.RIGHT)

        cancel_button = ctk.CTkButton(button_frame, text="Cancel", command=cancel_import)
        cancel_button.pack(side=tk.LEFT, padx=(0, 10))

        accept_button = ctk.CTkButton(button_frame, text="Accept", command=accept_text)
        accept_button.pack(side=tk.LEFT, padx=(0, 10))

        add_chapter_button = ctk.CTkButton(button_frame, text="Add Chapter Marker", command=add_chapter_marker)
        add_chapter_button.pack(side=tk.LEFT)


    def toggle_external_server(self):
        if self.use_external_server.get():
            self.external_server_url_entry.grid()
        else:
            self.external_server_url_entry.grid_remove()
            self.external_server_connected = False

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
            self.session_name_label.configure(text=new_session_name)
            self.playlist_listbox.delete(0, tk.END)
            self.marked_listbox.delete(0, tk.END)
            self.source_file = ""  # Clear the source_file
            self.selected_file_label.configure(text="No file selected") # Reset the label
            self.progress_bar.set(0)
            self.remaining_time_label.configure(text="N/A")
            self.stop_flag = False
            self.pre_selected_source_file = None # Clear the pre-selected file
            self.metadata = {"title": "", "album": "", "artist": "", "genre": "", "language": ""} # Clear metadata for a new session
            self.save_metadata()  # Save empty metadata


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
            self.advanced_settings_switch.grid()  # Show advanced settings for XTTS
            self.xtts_advanced_settings_frame.grid_remove()  # Hide XTTS advanced settings initially
            self.xtts_model_label.grid()
            self.xtts_model_dropdown.grid()

        else:  # Silero
            self.connect_to_server_button.grid_remove()
            self.use_external_server_switch.grid_remove()
            self.external_server_url_entry.grid_remove()

            self.external_server_connected = False
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
        for listbox in [self.playlist_listbox, self.marked_listbox]:
            selected_indices = listbox.curselection()
            for index in reversed(selected_indices):
                sentence = listbox.get(index)
                listbox.delete(index)
                
                # Remove from the other listbox if present
                other_listbox = self.marked_listbox if listbox == self.playlist_listbox else self.playlist_listbox
                for i in range(other_listbox.size()):
                    if other_listbox.get(i) == sentence:
                        other_listbox.delete(i)
                        break

        # Update the JSON file to reflect the changes
        self.update_json_after_removal()

    def update_json_after_removal(self):
        session_name = self.session_name.get()
        session_directory = os.path.join("Outputs", session_name)
        json_filename = os.path.join(session_directory, f"{session_name}_sentences.json")

        processed_sentences = self.load_json(json_filename)
        remaining_sentences = list(self.playlist_listbox.get(0, tk.END))

        updated_sentences = [s for s in processed_sentences if f"[{s['sentence_number']}] {s.get('processed_sentence', s['original_sentence'])}" in remaining_sentences]

        # Renumber the sentences
        for i, sentence in enumerate(updated_sentences, start=1):
            sentence['sentence_number'] = str(i)

        self.save_json(updated_sentences, json_filename)



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
            self.generate_dubbing_audio()
        else:
            if os.path.exists(json_filename):
                self.resume_generation()
            else:
                if not self.source_file:
                    CTkMessagebox(title="Error", message="Please select a source file.", icon="cancel")
                    return
                
                def preprocess_and_start():
                    # Show preprocessing pop-up
                    self.master.after(0, self.show_preprocessing_popup)
                    
                    with open(self.source_file, 'r', encoding='utf-8') as file:
                        text = file.read()
                    
                    preprocessed_sentences = self.text_preprocessor.preprocess_text(text, self.pdf_preprocessed, self.source_file, self.disable_paragraph_detection)
                    os.makedirs(session_dir, exist_ok=True)
                    self.save_json(preprocessed_sentences, json_filename)
                    
                    # Close preprocessing pop-up
                    self.master.after(0, self.close_preprocessing_popup)
                    
                    # Start the optimization process from the beginning
                    total_sentences = len(preprocessed_sentences)
                    self.optimization_thread = threading.Thread(target=self.start_optimisation, args=(total_sentences, 0))
                    self.optimization_thread.start()
                
                # Run preprocessing in a separate thread
                threading.Thread(target=preprocess_and_start, daemon=True).start()

    def check_server_connection(self):
        try:
            if self.tts_service.get() == "XTTS":
                if self.use_external_server.get() and self.external_server_connected:
                    url = f"{self.external_server_url.get()}/docs"
                else:
                    url = "http://localhost:8020/docs"
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

    def toggle_advanced_tts_settings(self):
        if self.tts_service.get() == "XTTS":
            advanced_visible = self.show_advanced_tts_settings.get()
            if advanced_visible:
                self.xtts_advanced_settings_frame.grid()
            else:
                self.xtts_advanced_settings_frame.grid_remove()

        else: # Silero, no advanced settings
            return

        # Common Row Shifting
        row_offset = 1 if advanced_visible else -1
        if hasattr(self, 'dubbing_frame') and self.dubbing_frame.winfo_ismapped():
            self.dubbing_frame.grid(row=self.dubbing_frame.grid_info()["row"] + row_offset)
        if hasattr(self, 'output_options_label'):
            self.output_options_label.grid(row=self.output_options_label.grid_info()["row"] + row_offset)
        if hasattr(self, 'output_options_frame'):
            self.output_options_frame.grid(row=self.output_options_frame.grid_info()["row"] + row_offset)
        if hasattr(self, 'generation_label'):
            self.generation_label.grid(row=self.generation_label.grid_info()["row"] + row_offset)
        if hasattr(self, 'generation_frame'):
            self.generation_frame.grid(row=self.generation_frame.grid_info()["row"] + row_offset)
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
                        logging.info("Applying RVC processing")
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
    
        # Check if this is a dubbing workflow - check both source and pre_selected for srt and video files
        is_dubbing_workflow = (self.pre_selected_source_file and 
            self.pre_selected_source_file.lower().endswith(
                (".srt", ".mp4", ".mkv", ".webm", ".avi", ".mov")
            ))

        # Calculate total generation time
        total_generation_time = sum(sentence_generation_times)
        formatted_time = str(datetime.timedelta(seconds=int(total_generation_time)))

        if is_dubbing_workflow:
            CTkMessagebox(
                title="Generation Finished", 
                message=(f"Speech generation completed!\n\n"
                        f"Total Generation Time: {formatted_time}\n\n"
                        "Click 'Add Dubbing to Video' to create the final dubbed video with subtitles "
                        "once you reviewed the generated audio and are happy with the results."),
                icon="info"
            )
        else:
            # Regular workflow - save concatenated audio
            session_name = self.session_name.get()
            output_format = self.output_format.get()
            session_dir = os.path.join("Outputs", session_name)
            default_output_path = os.path.join(session_dir, f"{session_name}.{output_format}")
            
            final_output_path = self.save_output(auto_path=default_output_path)
            
            if final_output_path:
                logging.info(f"The output file has been saved as {final_output_path}")
                CTkMessagebox(
                    title="Generation Finished", 
                    message=f"Generation completed!\n\nTotal Generation Time: {formatted_time}",
                    icon="info"
                )
            else:
                logging.warning("Failed to save the output file")

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
        chapter = sentence_dict.get("chapter", "no")  # Get the chapter marker
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
                    "chapter": chapter,
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
                    "chapter": chapter,  # Preserve chapter information
                    "split_part": split_part
                })
                self.save_json(processed_sentences_list, f"{os.path.splitext(self.source_file)[0]}_prompt_{prompt_number}.json")

                if self.unload_model_after_sentence.get():  # Check if unloading the model is enabled
                    self.unload_model()

            processed_sentence = {
                "sentence_number": sentence_number,
                "original_sentence": original_sentence,
                "paragraph": paragraph,
                "chapter": chapter,  # Include the chapter marker
                "processed_sentence": processed_sentences_list[-1]["text"],  # Get the processed sentence from the last prompt
                "split_part": split_part,
                "tts_generated": "no"
            }
        else: #LLM processing NOT enabled
            processed_sentence = {
                "sentence_number": sentence_number,
                "original_sentence": original_sentence,
                "paragraph": paragraph,
                "chapter": chapter,
                "split_part": split_part,
                "tts_generated": "no"
            }
            if "processed_sentence" in sentence_dict: #If there's already a processed sentence (from a loaded session, perhaps?)
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
        marked_sentence_numbers = {int(self.marked_listbox.get(i).split(']')[0][1:]) for i in range(self.marked_listbox.size())}

        for sentence_dict in data:
            split_part = sentence_dict.get("split_part")
            paragraph = sentence_dict.get("paragraph", "no")
            chapter = sentence_dict.get("chapter", "no")
            sentence_number = str(sentence_counter)
            sentence_counter += 1
            
            numbered_sentence = {
                "sentence_number": sentence_number,
                "paragraph": paragraph,
                "chapter": chapter,
                "split_part": split_part,
                "original_sentence": sentence_dict.get("original_sentence"),
                "processed_sentence": sentence_dict.get("processed_sentence"),
                "tts_generated": sentence_dict.get("tts_generated", "no"),
                "marked": int(sentence_number) in marked_sentence_numbers
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
            
            #if language != "en":
            #text = text.rstrip('.')
            
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

    def initialize_rvc(self):
        global rvc_functionality_available
        if rvc_functionality_available:
            try:
                self.rvc_inference = RVCInference(
                    models_dir=self.rvc_models_dir,
                    device="cuda:0" if torch.cuda.is_available() else "cpu"
                )
                self.rvc_inference.set_models_dir(self.rvc_models_dir)
                logging.info(f"RVC initialized successfully. Using device: {self.rvc_inference.device}")
                logging.info(f"CUDA available: {torch.cuda.is_available()}")
                if torch.cuda.is_available():
                    logging.info(f"GPU: {torch.cuda.get_device_name(0)}")
            except Exception as e:
                logging.error(f"Failed to initialize RVC: {str(e)}")
                rvc_functionality_available = False
            
            # Update UI based on RVC availability
            if hasattr(self, 'enable_rvc_switch'):
                if not rvc_functionality_available:
                    self.enable_rvc.set(False)
                    self.enable_rvc_switch.configure(state="disabled")
                else:
                    self.enable_rvc_switch.configure(state="normal")
            
            if hasattr(self, 'rvc_model_dropdown'):
                if not rvc_functionality_available:
                    self.rvc_model_dropdown.configure(state="disabled")
                else:
                    self.rvc_model_dropdown.configure(state="normal")

    def refresh_rvc_models(self):
        global rvc_functionality_available
        if rvc_functionality_available:
            self.rvc_inference.set_models_dir(self.rvc_models_dir)
        self.rvc_models = self.get_rvc_models()
        
        if hasattr(self, 'rvc_model_dropdown'):
            self.rvc_model_dropdown.configure(values=self.rvc_models)
            if self.rvc_models:
                self.rvc_model_dropdown.set(self.rvc_models[0])
            else:
                self.rvc_model_dropdown.set("")
        logging.info(f"RVC models refreshed, found {len(self.rvc_models)} models")

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
        display_text = f"[{sentence_number}] {sentence_text}"
        
        # Check if the sentence is already in the playlist
        for i in range(self.playlist_listbox.size()):
            if self.playlist_listbox.get(i).startswith(f"[{sentence_number}]"):
                self.playlist_listbox.delete(i)
                self.playlist_listbox.insert(i, display_text)
                return

        # If not found, insert the new sentence
        self.playlist_listbox.insert(tk.END, display_text)

    def play_selected_sentence(self):
        if pygame.mixer.get_init() is None:
            pygame.mixer.init()
            
        if self.channel is None:
            self.channel = pygame.mixer.Channel(0)
            
        selected_index_main = self.playlist_listbox.curselection()
        selected_index_marked = self.marked_listbox.curselection()
        
        if selected_index_main:
            selected_sentence = self.playlist_listbox.get(selected_index_main[0])
            source_listbox = self.playlist_listbox
        elif selected_index_marked:
            selected_sentence = self.marked_listbox.get(selected_index_marked[0])
            source_listbox = self.marked_listbox
        else:
            messagebox.showinfo("No Selection", "Please select a sentence to play.")
            return

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

            selected_index = self.marked_listbox.curselection()
            if not selected_index:
                selected_index = self.playlist_listbox.curselection()
                source_listbox = self.playlist_listbox
            else:
                source_listbox = self.marked_listbox

            if selected_index:
                selected_sentence = source_listbox.get(selected_index[0])
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

                    sentence_dict["original_sentence"] = sentence_text
                    sentence_dict["processed_sentence"] = sentence_text

                    processed_sentences[original_sentence_index] = sentence_dict
                    self.save_json(processed_sentences, json_filename)
                    logging.info(f"Updated sentence {sentence_number} in JSON file before regeneration: {json_filename}")

                    if self.source_file.endswith(".srt"):
                        self.enable_sentence_splitting.set(False)
                        self.enable_sentence_appending.set(False)
                        self.silence_length.set(0)
                        self.paragraph_silence_length.set(0)

                    processed_sentence = self.optimise_sentence(sentence_dict, original_sentence_index, session_dir)

                    if processed_sentence is not None:
                        audio_data = self.tts_to_audio(processed_sentence["processed_sentence"] if "processed_sentence" in processed_sentence else processed_sentence["original_sentence"])

                        if audio_data is not None:
                            if self.enable_rvc.get():
                                audio_data = self.process_with_rvc(audio_data)

                            if self.enable_fade.get():
                                audio_data = self.apply_fade(audio_data, self.fade_in_duration.get(), self.fade_out_duration.get())

                            if not self.source_file.endswith(".srt"):
                                silence_length = self.get_silence_length(processed_sentence)
                                if silence_length > 0:
                                    audio_data += AudioSegment.silent(duration=silence_length)

                            sentence_output_filename = os.path.join(session_dir, "Sentence_wavs", f"{self.session_name.get()}_sentence_{sentence_number}.wav")
                            audio_data.export(sentence_output_filename, format="wav")
                            logging.info(f"Regenerated audio for sentence {sentence_number}: {sentence_output_filename}")

                            new_sentence = f"[{sentence_number}] {processed_sentence['processed_sentence'] if 'processed_sentence' in processed_sentence else processed_sentence['original_sentence']}"
                            self.update_listboxes_after_regeneration(selected_sentence, new_sentence)

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

    def update_listboxes_after_regeneration(self, old_sentence, new_sentence):
        # Update the main playlist listbox
        for i in range(self.playlist_listbox.size()):
            if self.playlist_listbox.get(i) == old_sentence:
                self.playlist_listbox.delete(i)
                self.playlist_listbox.insert(i, new_sentence)
                break

        # Update the marked listbox
        for i in range(self.marked_listbox.size()):
            if self.marked_listbox.get(i) == old_sentence:
                self.marked_listbox.delete(i)
                self.marked_listbox.insert(i, new_sentence)
                break


    def regenerate_all_sentences(self):
        try:
            if self.tts_service.get() == "XTTS":
                self.apply_xtts_settings_silently()

            marked_sentences = list(self.marked_listbox.get(0, tk.END))
            if not marked_sentences:
                marked_sentences = list(self.playlist_listbox.get(0, tk.END))

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

            total_sentences = len(marked_sentences)

            for index, sentence in enumerate(marked_sentences):
                match = re.match(r'^\[(\d+)\]\s(.+)$', sentence)
                if match:
                    sentence_number = match.group(1)
                    sentence_text = match.group(2)

                    sentence_dict = next((s for s in processed_sentences if str(s["sentence_number"]) == sentence_number), None)
                    if sentence_dict:
                        sentence_dict["original_sentence"] = sentence_text
                        sentence_dict["processed_sentence"] = sentence_text

                        if self.source_file.endswith(".srt"):
                            self.enable_sentence_splitting.set(False)
                            self.enable_sentence_appending.set(False)
                            self.silence_length.set(0)
                            self.paragraph_silence_length.set(0)

                        processed_sentence = self.optimise_sentence(sentence_dict, int(sentence_number) - 1, session_dir)

                        if processed_sentence is not None:
                            audio_data = self.tts_to_audio(processed_sentence["processed_sentence"] if "processed_sentence" in processed_sentence else processed_sentence["original_sentence"])

                            if audio_data is not None:
                                if self.enable_rvc.get():
                                    audio_data = self.process_with_rvc(audio_data)

                                if self.enable_fade.get():
                                    audio_data = self.apply_fade(audio_data, self.fade_in_duration.get(), self.fade_out_duration.get())

                                if not self.source_file.endswith(".srt"):
                                    silence_length = self.get_silence_length(processed_sentence)
                                    if silence_length > 0:
                                        audio_data += AudioSegment.silent(duration=silence_length)

                                sentence_output_filename = os.path.join(session_dir, "Sentence_wavs", f"{session_name}_sentence_{sentence_number}.wav")
                                audio_data.export(sentence_output_filename, format="wav")

                                new_sentence = f"[{sentence_number}] {processed_sentence['processed_sentence'] if 'processed_sentence' in processed_sentence else processed_sentence['original_sentence']}"
                                self.update_listboxes_after_regeneration(sentence, new_sentence)

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
            self.marked_listbox.delete(0, tk.END)
            CTkMessagebox(title="Regeneration Complete", message="All marked sentences have been regenerated.", icon="info")

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

    def highlight_playing_sentence(self, sentence):
        try:
            # Highlight only in the playlist_listbox
            listbox = self.playlist_listbox  # Directly target the playlist listbox
            for i in range(listbox.size()):
                if listbox.get(i) == sentence:
                    listbox.selection_clear(0, tk.END)
                    listbox.selection_set(i)
                    listbox.see(i)
                    return  # Stop searching after finding the sentence

        except Exception as e:
            logging.error(f"Error highlighting sentence: {str(e)}")

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
                    self.previous_sentence = self.current_sentence  # Store the previous sentence
                    self.current_sentence = sentence  # Store the current sentence
                    # Highlight the playing sentence
                    self.highlight_playing_sentence(sentence)
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

    def handle_keyboard_event(self, event):
        logging.info(f"Keyboard event received: {event.keysym}")
        if event.keysym == "space":
            logging.info("Space key pressed")
            if self.playing:
                self.stop_playback()
            else:
                self.play_sentences_as_playlist()
        elif event.keysym.lower() == "m":
            self.mark_sentences_for_regeneration(event)

    def mark_sentences_for_regeneration(self, event=None):
        logging.info("Attempting to mark sentence(s) for regeneration")

        def mark_sentence(sentence):
            if sentence and sentence not in self.marked_listbox.get(0, tk.END):
                self.marked_listbox.insert(tk.END, sentence)
                sentence_number = sentence.split(']')[0][1:]
                self.mark_sentence_in_json(sentence_number)
                logging.info(f"Marked sentence {sentence_number} for regeneration")

        # Determine which sentences to mark based on the event
        if event and event.type == '4':  # Right-click event
            sentences_to_mark = [self.current_sentence, self.previous_sentence]
            logging.info("Right-click detected, marking current and previous sentences")
        elif event and event.keysym.lower() == 'm':  # 'M' key press event
            sentences_to_mark = [self.current_sentence]
            logging.info("'M' key pressed, marking current sentence")
        else:  # Default behavior (e.g., when called without an event)
            sentences_to_mark = [self.current_sentence]
            logging.info("No specific event, marking current sentence")

        # Mark the determined sentences
        for sentence in sentences_to_mark:
            if sentence:
                mark_sentence(sentence)
            else:
                logging.info(f"No {'current' if sentence == self.current_sentence else 'previous'} sentence to mark")

        # Update the GUI if any sentences were marked
        if any(sentences_to_mark):
            self.master.update_idletasks()

    def mark_sentence_in_json(self, sentence_number):
        json_filename = os.path.join("Outputs", self.session_name.get(), f"{self.session_name.get()}_sentences.json")
        data = self.load_json(json_filename)
        for sentence in data:
            if sentence["sentence_number"] == sentence_number:
                sentence["marked"] = True
                break
        self.save_json(data, json_filename)

    def unmark_sentence_in_json(self, sentence_number):
        json_filename = os.path.join("Outputs", self.session_name.get(), f"{self.session_name.get()}_sentences.json")
        data = self.load_json(json_filename)
        for sentence in data:
            if sentence["sentence_number"] == sentence_number:
                sentence["marked"] = False
                break
        self.save_json(data, json_filename)

    def load_session(self):
        session_folder = filedialog.askdirectory(initialdir="Outputs")
        if session_folder:
            session_name = os.path.basename(session_folder)
            json_filename = os.path.join(session_folder, f"{session_name}_sentences.json")
            if os.path.exists(json_filename):
                self.session_name.set(session_name)
                self.session_name_label.configure(text=session_name)
                processed_sentences = self.load_json(json_filename)
                self.playlist_listbox.delete(0, tk.END)
                self.marked_listbox.delete(0, tk.END)
                self.load_metadata()
                for sentence_dict in processed_sentences:
                    sentence_number = sentence_dict.get("sentence_number")
                    sentence_text = sentence_dict.get("processed_sentence") if sentence_dict.get("processed_sentence") else sentence_dict.get("original_sentence")
                    if sentence_text and sentence_dict.get("tts_generated") == "yes":
                        display_text = f"[{sentence_number}] {sentence_text}"
                        self.playlist_listbox.insert(tk.END, display_text)
                        if sentence_dict.get("marked", False):  # Use the "marked" attribute directly
                            self.marked_listbox.insert(tk.END, display_text)

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
            source_listbox = self.playlist_listbox

            if not selected_index:
                selected_index = self.marked_listbox.curselection()
                source_listbox = self.marked_listbox

            if selected_index:
                selected_sentence = source_listbox.get(selected_index[0])
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
                    edit_window.attributes('-topmost', True)

                    sentence_textbox = ctk.CTkTextbox(edit_window, width=600, height=100, wrap="word")
                    sentence_textbox.insert("1.0", sentence_text)
                    sentence_textbox.pack(padx=10, pady=10)

                    def save_edited_sentence():
                        edited_sentence = sentence_textbox.get("1.0", "end-1c")
                        self.update_sentence_in_json(sentence_number, edited_sentence)
                        new_sentence = f"[{sentence_number}] {edited_sentence}"
                        self.update_listboxes_after_edit(selected_sentence, new_sentence)
                        edit_window.destroy()
                        logging.info(f"Edited sentence {sentence_number}: {edited_sentence}")

                    def discard_changes():
                        edit_window.destroy()
                        logging.info(f"Discarded changes for sentence {sentence_number}")

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

    def update_listboxes_after_edit(self, old_sentence, new_sentence):
        for listbox in [self.playlist_listbox, self.marked_listbox]:
            for i in range(listbox.size()):
                if listbox.get(i) == old_sentence:
                    listbox.delete(i)
                    listbox.insert(i, new_sentence)
                    break

    def get_silence_length(self, processed_sentence):
        if processed_sentence.get("paragraph", "no") == "yes":
            return self.paragraph_silence_length.get()
        elif processed_sentence.get("split_part") is not None:
            if isinstance(processed_sentence.get("split_part"), str):
                if processed_sentence.get("split_part") in ["0a", "0b", "1a"]:
                    return self.silence_length.get() // 4
                elif processed_sentence.get("split_part") == "1b":
                    return self.silence_length.get()
            elif isinstance(processed_sentence.get("split_part"), int):
                if processed_sentence.get("split_part") == 0:
                    return self.silence_length.get() // 4
                elif processed_sentence.get("split_part") == 1:
                    return self.silence_length.get()
        return self.silence_length.get()

    def upload_cover(self):
        file_path = filedialog.askopenfilename(filetypes=[("Image files", "*.jpg *.jpeg *.png")])
        if file_path:
            self.cover_image_path = file_path
            messagebox.showinfo("Cover Image", "Cover image uploaded successfully.")
            self.upload_cover_button.configure(text="Cover Uploaded")  # Provide feedback to user

    def save_output(self, auto_path=None):
        session_name = self.session_name.get()
        output_format = self.output_format.get()
        bitrate = self.bitrate.get()

        if auto_path:
            output_path = auto_path
        else:
            output_path = filedialog.asksaveasfilename(
                initialdir="Outputs",
                initialfile=f"{session_name}.{output_format}",
                filetypes=[(f"{output_format.upper()} Files", f"*.{output_format}")],
                defaultextension=f".{output_format}"
            )

        if output_path:
            session_dir = os.path.join("Outputs", session_name)
            json_filename = os.path.join(session_dir, f"{session_name}_sentences.json")
            processed_sentences = self.load_json(json_filename)

            wav_files = []
            chapters = []
            current_time = 0

            for sentence_dict in processed_sentences:
                sentence_number = int(sentence_dict["sentence_number"])
                wav_filename = os.path.join(session_dir, "Sentence_wavs", f"{session_name}_sentence_{sentence_number}.wav")
                if os.path.exists(wav_filename):
                    wav_files.append(wav_filename)
                    
                    # Collect chapter information
                    if sentence_dict.get("chapter") == "yes":
                        audio = AudioSegment.from_wav(wav_filename)
                        chapters.append((current_time / 1000, sentence_dict.get("original_sentence", "")))
                    
                    current_time += len(AudioSegment.from_wav(wav_filename))

            with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding='utf-8') as temp_file:
                for wav_file in wav_files:
                    temp_file.write(f"file '{os.path.abspath(wav_file)}'\n")
                input_list_path = temp_file.name

            ffmpeg_command = [
                "ffmpeg",
                "-f", "concat",
                "-safe", "0",
                "-i", input_list_path,
                "-y"
            ]

            if output_format == "wav":
                ffmpeg_command += ["-c:a", "pcm_s16le"]
            elif output_format in ["mp3", "m4b", "opus"]:
                codec = "libmp3lame" if output_format == "mp3" else "aac" if output_format == "m4b" else "libopus"
                ffmpeg_command += ["-c:a", codec, "-b:a", bitrate]

            ffmpeg_command.append(output_path)

            try:
                subprocess.run(ffmpeg_command, check=True, stderr=subprocess.PIPE, universal_newlines=True)

                # Apply Metadata and Cover Art
                self.save_metadata_and_cover(output_path, output_format)

                # Add chapters if the output format is m4b
                if output_format == "m4b" and chapters:
                    self.add_chapters_to_m4b(output_path, chapters)

                if not auto_path:
                    messagebox.showinfo("Output Saved", f"The output file has been saved as {output_path}")
                return output_path

            except subprocess.CalledProcessError as e:
                error_message = f"FFmpeg exited with a non-zero code: {e.returncode}\n\nError output:\n{e.stderr}"
                logging.error(error_message)
                if not auto_path:
                    messagebox.showerror("FFmpeg Error", error_message)
            except Exception as e:
                error_message = f"An unexpected error occurred: {str(e)}"
                logging.error(error_message)
                if not auto_path:
                    messagebox.showerror("Error", error_message)
            finally:
                if os.path.exists(input_list_path):
                    os.remove(input_list_path)

        return None

    def save_metadata_and_cover(self, output_path, output_format):
        def optimize_image(image_path, target_format='JPEG', max_size=(500, 500)):
            with Image.open(image_path) as img:
                if img.mode == 'RGBA':
                    img = img.convert('RGB')
                img.thumbnail(max_size)
                bio = io.BytesIO()
                img.save(bio, format=target_format)
                return bio.getvalue()

        if output_format == "wav":
            return

        metadata = self.metadata


        if output_format == "mp3":
            audio = MP3(output_path, ID3=ID3)
            
            audio.tags = ID3()
            if metadata["title"]:
                audio.tags.add(TIT2(encoding=3, text=metadata["title"]))
            if metadata["album"]:
                audio.tags.add(TALB(encoding=3, text=metadata["album"]))
            if metadata["artist"]:
                audio.tags.add(TPE1(encoding=3, text=metadata["artist"]))
            if metadata["genre"]:
                audio.tags.add(TCON(encoding=3, text=metadata["genre"]))
            
            if hasattr(self, 'cover_image_path') and os.path.exists(self.cover_image_path):
                cover_data = optimize_image(self.cover_image_path)
                audio.tags.add(
                    APIC(
                        encoding=3,
                        mime='image/jpeg',
                        type=3,
                        desc='Cover',
                        data=cover_data
                    )
                )
            audio.save()

        elif output_format == "m4b":
            audio = MP4(output_path)
            for key, value in metadata.items():
                if value:
                    if key == "title":
                        audio["\xa9nam"] = [value]
                    elif key == "album":
                        audio["\xa9alb"] = [value]
                    elif key == "artist":
                        audio["\xa9ART"] = [value]
                    elif key == "genre":
                        audio["\xa9gen"] = [value]
            
            if hasattr(self, 'cover_image_path') and os.path.exists(self.cover_image_path):
                with open(self.cover_image_path, "rb") as f:
                    cover_data = f.read()
                audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG if self.cover_image_path.lower().endswith('.jpg') or self.cover_image_path.lower().endswith('.jpeg') else MP4Cover.FORMAT_PNG)]
            audio.save()

        elif output_format == "opus":
            audio = OggOpus(output_path)
            for key, value in metadata.items():
                if value:
                    audio[key] = value
            
            if hasattr(self, 'cover_image_path') and os.path.exists(self.cover_image_path):
                cover_data = optimize_image(self.cover_image_path)
                picture = Picture()
                picture.data = cover_data
                picture.type = PictureType.COVER_FRONT
                picture.mime = "image/jpeg"
                picture.width = 500
                picture.height = 500
                picture.depth = 24
                encoded_data = base64.b64encode(picture.write())
                audio["metadata_block_picture"] = [encoded_data.decode("ascii")]
            audio.save()

    def add_chapters_to_m4b(self, file_path, chapters):
        logging.info(f"Starting to add chapters to {file_path}")
        logging.info(f"Number of chapters to add: {len(chapters)}")

        # Create a temporary file for chapter metadata
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt", encoding='utf-8') as temp_file:
            temp_file.write(";FFMETADATA1\n")  # Add the required header
            for i, (time, title) in enumerate(chapters):
                start_time = int(time * 1000)
                end_time = int(chapters[i+1][0] * 1000) if i < len(chapters) - 1 else 9223372036854775807
                temp_file.write(f"\n[CHAPTER]\nTIMEBASE=1/1000\nSTART={start_time}\nEND={end_time}\ntitle={title}\n")
            chapter_file = temp_file.name

        logging.info(f"Chapter metadata file created: {chapter_file}")
        
        # Log the content of the chapter file for debugging
        with open(chapter_file, 'r', encoding='utf-8') as f:
            logging.debug(f"Chapter file contents:\n{f.read()}")

        # Use FFmpeg to add chapters to the M4B file
        ffmpeg_command = [
            "ffmpeg",
            "-i", file_path,
            "-i", chapter_file,
            "-map", "0",
            "-map_chapters", "1",
            "-c", "copy",
            "-y",
            f"{file_path}.temp.m4b"
        ]

        logging.info(f"FFmpeg command: {' '.join(ffmpeg_command)}")

        try:
            result = subprocess.run(ffmpeg_command, check=True, stderr=subprocess.PIPE, universal_newlines=True)
            logging.info(f"FFmpeg output:\n{result.stderr}")
            
            os.replace(f"{file_path}.temp.m4b", file_path)
            logging.info(f"Successfully added chapters to {file_path}")

            # Re-apply metadata and cover art after adding chapters
            self.save_metadata_and_cover(file_path, "m4b")
            logging.info("Metadata and cover art re-applied")

            # Verify that chapters were added
            verify_command = ["ffprobe", "-i", file_path, "-show_chapters", "-v", "quiet", "-print_format", "json"]
            verify_result = subprocess.run(verify_command, check=True, stdout=subprocess.PIPE, universal_newlines=True)
            chapters_info = json.loads(verify_result.stdout)
            logging.info(f"Chapters in the final file: {json.dumps(chapters_info, indent=2)}")

            if len(chapters_info.get('chapters', [])) == len(chapters):
                logging.info("All chapters were successfully added")
            else:
                logging.warning(f"Mismatch in chapter count. Expected: {len(chapters)}, Found: {len(chapters_info.get('chapters', []))}")

        except subprocess.CalledProcessError as e:
            error_message = f"FFmpeg exited with a non-zero code while adding chapters: {e.returncode}\n\nError output:\n{e.stderr}"
            logging.error(error_message)
            messagebox.showerror("FFmpeg Error", error_message)
        except Exception as e:
            error_message = f"An unexpected error occurred while adding chapters: {str(e)}"
            logging.error(error_message)
            messagebox.showerror("Error", error_message)
        finally:
            if os.path.exists(chapter_file):
                os.remove(chapter_file)
                logging.info(f"Temporary chapter file removed: {chapter_file}")

    def update_output_options(self, *args):
        output_format = self.output_format.get()
        if output_format == "wav":
            self.bitrate_dropdown.configure(state="disabled")
            self.upload_cover_button.configure(state="disabled")
            self.album_name_entry.configure(state="disabled")
            self.artist_name_entry.configure(state="disabled")
            self.genre_entry.configure(state="disabled")
        else:
            self.bitrate_dropdown.configure(state="normal")
            self.upload_cover_button.configure(state="normal")
            self.album_name_entry.configure(state="normal")
            self.artist_name_entry.configure(state="normal")
            self.genre_entry.configure(state="normal")

        if output_format not in ["mp3", "m4b"]:
            self.upload_cover_button.configure(state="disabled")

def main():
    logging.info("Pandrator application starting")
    
    parser = argparse.ArgumentParser(description="Pandrator TTS Optimizer")
    parser.add_argument("-connect", action="store_true", help="Connect to a TTS service on launch")
    parser.add_argument("-xtts", action="store_true", help="Connect to XTTS")
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
        elif args.silero:
            logging.info("Connecting to Silero")
            gui.tts_service.set("Silero")
            gui.connect_to_server()
            gui.update_tts_service()

    logging.info("Starting main event loop")
    root.mainloop()
    logging.info("Pandrator application exiting")

if __name__ == "__main__":
    main()
