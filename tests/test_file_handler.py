import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from pandrator.logic.file_handler import download_video_from_url, extract_text_from_pdf


class FileHandlerContractTests(unittest.TestCase):
    def test_native_pdf_retains_blank_page_delimiters_and_closes_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pages.pdf"
            with pymupdf.open() as document:
                document.new_page().insert_text((72, 72), "First page.")
                document.new_page()
                document.new_page().insert_text((72, 72), "Third page.")
                document.save(path)
            reader = pymupdf.open(path)
            with patch("pymupdf.open", return_value=reader):
                text = extract_text_from_pdf(str(path))
            self.assertEqual("First page.\n\f\fThird page.\n", text)
            self.assertTrue(reader.is_closed)

    def test_video_downloader_preserves_options_and_expected_filename(self):
        url = "https://example.invalid/video"
        with patch("yt_dlp.YoutubeDL") as constructor:
            downloader = constructor.return_value.__enter__.return_value
            info = {"title": "Fixture", "ext": "mp4"}
            downloader.extract_info.return_value = info
            downloader.prepare_filename.return_value = os.path.join("output", "Fixture.mp4")
            result = download_video_from_url(url, "output")
        constructor.assert_called_once_with(
            {
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "outtmpl": os.path.join("output", "%(title)s.%(ext)s"),
                "noplaylist": True,
                "restrictfilenames": True,
                "quiet": True,
                "no_warnings": True,
            }
        )
        downloader.extract_info.assert_called_once_with(url, download=False)
        downloader.download.assert_called_once_with([url])
        downloader.prepare_filename.assert_called_once_with(info)
        self.assertEqual(os.path.join("output", "Fixture.mp4"), result)


if __name__ == "__main__":
    unittest.main()
