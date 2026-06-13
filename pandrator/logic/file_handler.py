import os
import re
import shutil
import subprocess
import datetime

def _extract_chapter_text(html_content, all_html_content=""):
    from bs4 import BeautifulSoup

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

def extract_text_from_epub(epub_path: str, remove_footnotes: bool = False, filter_citations: bool = True) -> str:
    """Extracts and combines text from all documents in an EPUB file using robust heuristics."""
    from .source_cleaning.deterministic import extract_clean_epub
    return extract_clean_epub(epub_path, remove_footnotes=remove_footnotes, filter_citations=filter_citations)

def extract_text_from_pdf(pdf_path: str) -> str:
    """Returns a page-delimited native-text fallback using PyMuPDF."""
    import fitz

    document = fitz.open(pdf_path)
    try:
        return "\f".join(page.get_text("text") for page in document)
    finally:
        document.close()

def convert_doc_to_text(doc_path: str, output_txt_path: str) -> bool:
    """Converts a document (e.g., .docx, .mobi) to a text file using ebook-convert."""
    def run_ebook_convert(command):
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    if run_ebook_convert(["ebook-convert", doc_path, output_txt_path]):
        return True
    else:
        # Fallback to a possible Calibre Portable installation relative to the project root
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
        portable_candidates = [
            os.path.join(project_root, 'Calibre Portable', 'Calibre', 'ebook-convert.exe'),
            os.path.join(project_root, 'Calibre Portable', 'PFiles64', 'Calibre2', 'ebook-convert.exe'),
        ]

        for calibre_portable_path in portable_candidates:
            if os.path.exists(calibre_portable_path) and run_ebook_convert([calibre_portable_path, doc_path, output_txt_path]):
                return True
    return False

def download_video_from_url(url: str, output_dir: str) -> str:
    """Downloads a video from a URL (e.g., YouTube) and returns the file path."""
    import yt_dlp

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
        'quiet': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("Could not extract video info from URL.")
        
        # Download the video
        ydl.download([url])
        
        # Return the expected final path
        return ydl.prepare_filename(info)

def save_pasted_text(text: str, session_dir: str, mark_paragraphs_multiple_newlines: bool) -> str:
    """Saves pasted text to a file in the session directory."""
    os.makedirs(session_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"pasted_text_{timestamp}.txt"
    file_path = os.path.join(session_dir, filename)
    
    if mark_paragraphs_multiple_newlines:
        processed_text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)
    else:
        processed_text = re.sub(r'(?<!\n)\n(?!\n)', '\n\n', text)
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(processed_text)
    
    return file_path
