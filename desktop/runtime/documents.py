"""Read-only local adapters. Macros, scripts and external refresh never execute."""
from __future__ import annotations
from pathlib import Path
from xml.etree import ElementTree as ET
import csv
import hashlib
import io
import json
import zipfile


def xml_text(data):
    root = ET.fromstring(data)
    return ' '.join(n.text for n in root.iter() if n.tag.rsplit('}', 1)[-1] in ['t', 'text'] and n.text)


def decode(data):
    for encoding in ['utf-8-sig', 'utf-16', 'gb18030']:
        try: return data.decode(encoding)
        except UnicodeError: pass
    raise ValueError('The text encoding could not be read. Save a UTF-8 copy locally.')


def read_document(path: Path):
    suffix = path.suffix.lower()
    parts, tables, issues = [], [], []
    def part(text, location):
        if text.strip(): parts.append({'text': text.strip(), 'location': location})
    if suffix in ['.txt', '.md', '.markdown', '.json', '.csv', '.tsv', '.log', '.html', '.htm']:
        text = decode(path.read_bytes())
        if suffix in ['.html', '.htm']:
            from html.parser import HTMLParser
            class Plain(HTMLParser):
                def __init__(self): super().__init__(); self.data = []; self.skip = 0
                def handle_starttag(self, tag, attrs):
                    if tag in ['script', 'style']: self.skip += 1
                def handle_endtag(self, tag):
                    if tag in ['script', 'style'] and self.skip: self.skip -= 1
                def handle_data(self, data):
                    if not self.skip: self.data.append(data)
            parser = Plain(); parser.feed(text); text = '\n'.join(parser.data)
        if suffix in ['.csv', '.tsv']:
            rows = list(csv.reader(io.StringIO(text), delimiter='\t' if suffix == '.tsv' else ','))
            if rows: tables.append({'name': path.stem, 'columns': rows[0], 'rows': rows[1:]})
        part(text, 'document')
    elif suffix in ['.docx', '.docm', '.pptx', '.pptm', '.xlsx', '.xlsm']:
        with zipfile.ZipFile(path) as z:
            if sum(info.file_size for info in z.infolist()) > 256 * 1024 * 1024:
                raise ValueError('The decompressed document exceeds the safe adapter limit (256 MB).')
            if suffix in ['.docx', '.docm']:
                root = ET.fromstring(z.read('word/document.xml'))
                for number, paragraph in enumerate(root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'), 1):
                    part(''.join(n.text or '' for n in paragraph.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t')), 'paragraph ' + str(number))
                if any(n.startswith('word/media/') for n in z.namelist()): issues.append('Embedded pictures were not interpreted; import the picture separately for local vision.')
            elif suffix in ['.pptx', '.pptm']:
                import re
                names = sorted([n for n in z.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)], key=lambda n: int(re.search(r'(\d+)\.xml', n).group(1)))
                for number, name in enumerate(names, 1):
                    part(xml_text(z.read(name)), 'slide ' + str(number))
                    note = 'ppt/notesSlides/notesSlide' + str(number) + '.xml'
                    if note in z.namelist(): part(xml_text(z.read(note)), 'slide ' + str(number) + ' notes')
                if any(n.startswith('ppt/media/') for n in z.namelist()): issues.append('Embedded pictures were not interpreted; import the picture separately for local vision.')
            else:
                ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                strings = []
                if 'xl/sharedStrings.xml' in z.namelist():
                    strings = [''.join(t.text or '' for t in si.findall('.//m:t', ns)) for si in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si', ns)]
                workbook = ET.fromstring(z.read('xl/workbook.xml'))
                relationships = {r.attrib['Id']: r.attrib['Target'].lstrip('/') for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
                for sheet in workbook.findall('m:sheets/m:sheet', ns):
                    target = relationships[sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']]
                    name = target if target.startswith('xl/') else 'xl/' + target
                    rows = []
                    for row in ET.fromstring(z.read(name)).findall('m:sheetData/m:row', ns):
                        values = []
                        for cell in row.findall('m:c', ns):
                            v = cell.find('m:v', ns); value = v.text or '' if v is not None else ''
                            if cell.get('t') == 's' and value: value = strings[int(value)]
                            if cell.get('t') == 'inlineStr': value = ''.join(t.text or '' for t in cell.findall('.//m:t', ns))
                            formula = cell.find('m:f', ns)
                            if formula is not None:
                                if not value: issues.append(sheet.attrib['name'] + '!' + cell.get('r', '') + ': formula has no saved result; it was not evaluated.')
                                part('Formula: =' + (formula.text or '') + '; saved result: ' + value, sheet.attrib['name'] + '!' + cell.get('r', ''))
                            else: part(value, sheet.attrib['name'] + '!' + cell.get('r', ''))
                            import re
                            letters = re.match(r'[A-Z]+', cell.get('r', 'A1')).group()
                            col = 0
                            for letter in letters: col = col * 26 + ord(letter) - 64
                            while len(values) < col: values.append('')
                            values[col - 1] = value
                        rows.append(values)
                    if rows: tables.append({'name': sheet.attrib['name'], 'columns': rows[0], 'rows': rows[1:]})
    elif suffix == '.pdf':
        try:
            from pypdf import PdfReader
            pdf = PdfReader(str(path))
        except ImportError: raise ValueError('The local PDF adapter is not installed.')
        if pdf.is_encrypted: raise ValueError('This PDF is encrypted. Unlock a copy locally before importing.')
        for index, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ''
            if text.strip(): part(text, 'page ' + str(index))
            else:
                try:
                    import pypdfium2 as pdfium
                    document = pdfium.PdfDocument(str(path))
                    bitmap = document[index - 1].render(scale=2)
                    image = bitmap.to_pil()
                    text = ocr(image)
                    part(text, 'page ' + str(index) + ' OCR')
                except Exception as error: issues.append('Page ' + str(index) + ': ' + str(error))
    elif suffix in ['.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff', '.gif']:
        try:
            from PIL import Image, ImageSequence
            with Image.open(path) as image:
                for index, frame in enumerate(ImageSequence.Iterator(image), 1):
                    try: part(ocr(frame.convert('RGB')), 'image frame ' + str(index) + ' OCR')
                    except Exception as error: issues.append('Frame ' + str(index) + ': ' + str(error))
            issues.append('OCR extracts text. Scene and chart understanding requires the Local AI vision capability.')
        except ImportError: issues.append('The local image adapter is not installed.')
    else:
        raise ValueError('This format is not yet supported by an installed local adapter. Save a DOCX, XLSX, PPTX, PDF or text copy locally.')
    text = '\n\n'.join('[' + p['location'] + ']\n' + p['text'] for p in parts)
    return {'status': 'partial' if issues else 'ready' if text else 'empty', 'parts': parts, 'text': text, 'tables': tables, 'issues': list(dict.fromkeys(issues))}


_ocr = None
def ocr(image):
    global _ocr
    try:
        import numpy as np
        import onnxruntime
        onnxruntime.disable_telemetry_events()
        from rapidocr_onnxruntime import RapidOCR
    except ImportError: raise ValueError('Local OCR resources are missing. No cloud OCR fallback is permitted.')
    if _ocr is None: _ocr = RapidOCR()
    result, _ = _ocr(np.asarray(image))
    return '\n'.join(item[1] for item in result or [])
