"""Распознаёт скан официальной публикации закона (PDF без текстового слоя) в текст по страницам.

    python scripts/import/ocr_pdf.py fz456.pdf fz456_ocr.txt tools/tessdata

Скан 456-ФЗ: http://publication.pravo.gov.ru/file/pdf?eoNumber=0001202512150018
Tesseract встроен в PyMuPDF; языки распознавания — tools/tessdata (scripts/setup.py --ocr).
Дальше текст разбирается на статьи: scripts/import/struct_456fz.py.
"""
import sys

import pymupdf

pdf, out, tess = sys.argv[1], sys.argv[2], sys.argv[3]
doc = pymupdf.open(pdf)
pages = []
for i, page in enumerate(doc):
    tp = page.get_textpage_ocr(language="rus", dpi=300, full=True, tessdata=tess)
    pages.append(f"=== {i + 1}\n" + page.get_text("text", textpage=tp))
    print(i + 1, file=sys.stderr)
open(out, "w", encoding="utf-8").write("\n".join(pages))
