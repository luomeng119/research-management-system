"""Real Poppler regression for scanned PDFs; generated pages are synthetic."""
import shutil

import pytest
from PIL import Image, JpegImagePlugin  # noqa: F401 - registers Pillow PDF encoder

from app.ocr.recognizer import pdf_to_images


@pytest.mark.skipif(not shutil.which('pdftoppm'), reason='local Poppler required')
def test_scanned_pdf_returns_ordered_usable_pages_without_source_artifacts(tmp_path):
    source = tmp_path / 'synthetic-scan.pdf'
    first = Image.new('RGB', (200, 150), 'white')
    second = Image.new('RGB', (200, 150), 'blue')
    first.save(source, save_all=True, append_images=[second])

    pages = pdf_to_images(source)

    assert len(pages) == 2
    assert pages[0].getpixel((20, 20)) == (255, 255, 255)
    red, green, blue = pages[1].getpixel((20, 20))
    assert red < 10 and green < 10 and blue > 240
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.skipif(not all(shutil.which(cmd) for cmd in ('pdftotext', 'pdftoppm', 'tesseract')),
                    reason='local Poppler and Tesseract required')
@pytest.mark.parametrize('fixture', ['synthetic-text.pdf', 'synthetic-scan.pdf'])
@pytest.mark.parametrize('kind', ['invoice', 'payment'])
def test_pdf_staged_with_part_suffix_uses_real_pdf_recognition(tmp_path, fixture, kind):
    from pathlib import Path
    from app.ocr.recognizer import recognize_file, recognize_payment

    staged = tmp_path / 'validated-upload.part'
    shutil.copyfile(Path(__file__).parent / 'fixtures/ocr' / fixture, staged)
    result = (recognize_file if kind == 'invoice' else recognize_payment)(staged)
    fields = result['fields'] if kind == 'invoice' else result
    assert float(fields['amount']) == 318.42
    assert fields['date' if kind == 'invoice' else 'pay_date'] == '2026-09-10'
    assert 'SYNTHETIC' in result['text' if kind == 'invoice' else 'ocr_text']


@pytest.mark.parametrize('kind', ['invoice', 'payment'])
def test_forged_pdf_header_does_not_report_recognition_success(tmp_path, kind):
    from app.ocr.recognizer import recognize_file, recognize_payment

    staged = tmp_path / 'invalid.part'
    staged.write_bytes(b'%PDF-this is not a valid PDF')
    result = (recognize_file if kind == 'invoice' else recognize_payment)(staged)
    fields = result.get('fields', {}) if kind == 'invoice' else result
    assert not fields.get('amount')
    assert not result.get('text' if kind == 'invoice' else 'ocr_text')
