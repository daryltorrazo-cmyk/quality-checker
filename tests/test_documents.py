import base64
import io
import unittest
import zipfile

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from credit_checker.documents import drafts, extract_upload, read_text


def upload(text, filename="letter.txt", kind="letter"):
    content = text.encode() if isinstance(text, str) else text
    return extract_upload({"filename": filename, "kind": kind,
                           "content_base64": base64.b64encode(content).decode()}, "test-agent")


def pdf_with_text(text=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if text:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO(); writer.write(buf)
    return buf.getvalue()


class DocumentTests(unittest.TestCase):
    def test_text_preserved_and_missing_bureau_not_invented(self):
        text = 'Please investigate:\n1. SOME BANK (Inquiry dated 05/10/2026)'
        result = upload(text)
        self.assertEqual(result['text'], text)
        self.assertEqual(result['draft']['inquiries'][0]['bureau'], '')
        self.assertEqual(result['draft']['inquiries'][0]['date'], '2026-05-10')
        self.assertTrue(result['notes'])

    def test_bureau_headings_and_duplicates_preserved(self):
        text = 'Experian\n1. SOME BANK (Inquiry dated 05/10/2026)\n2. SOME BANK (Inquiry dated 05/10/2026)'
        result = upload(text)
        self.assertEqual(len(result['draft']['inquiries']), 2)
        self.assertEqual(result['draft']['inquiries'][0]['bureau'], 'EX')

    def test_structured_source_includes_accounts(self):
        text = 'INQUIRY | TU | SOME BANK | 2026-05-10\nACCOUNT | EQ | SOME BANK | 2026-05-12 | open | positive | credit_card'
        result = upload(text, 'source.txt', 'source')
        self.assertEqual(len(result['draft']['accounts']), 1)
        self.assertEqual(result['draft']['accounts'][0]['bureau'], 'EQ')

    def test_unrecognized_dates_need_manual_review(self):
        result = upload('Some lender inquiry on 10 May 2026')
        self.assertEqual(result['draft']['inquiries'], [])
        self.assertTrue(any('unparsed' in note for note in result['notes']))

    def test_csv_source_inquiries_alone_warns_about_accounts(self):
        result = upload('bureau,name,date\nTU,SOME BANK,2026-05-10', 'report.csv', 'source')
        self.assertTrue(any('accounts' in note for note in result['notes']))

    def test_docx_extracts_body_tables_and_header(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Experian</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>1. SOME BANK (Inquiry dated 05/10/2026)</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
            archive.writestr('word/header1.xml', '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Header included</w:t></w:r></w:p></w:hdr>')
        result = upload(buf.getvalue(), 'letter.docx')
        self.assertIn('Header included', result['text'])
        self.assertEqual(result['draft']['inquiries'][0]['bureau'], 'EX')

    def test_text_pdf_and_scan_detection(self):
        text, missing = read_text('letter.pdf', pdf_with_text('INQUIRY | TU | SOME BANK | 2026-05-10'))
        self.assertIn('SOME BANK', text)
        self.assertEqual(missing, [])
        scan = upload(pdf_with_text(), 'scan.pdf')
        self.assertEqual(scan['unreadable_pages'], [1])
        self.assertTrue(any('OCR' in note for note in scan['notes']))

    def test_reject_invalid_file_and_xml_entities(self):
        for name, content in [('file.doc', b'x'), ('file.pdf', b'not a pdf'), ('file.docx', b'bad zip'), ('file.txt', b'\xff'), ('file.txt', b'')]:
            with self.assertRaises(ValueError): read_text(name, content)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            archive.writestr('word/document.xml', '<!DOCTYPE doc [<!ENTITY x "bad">]><doc/>')
        with self.assertRaises(ValueError): read_text('file.docx', buf.getvalue())

    def test_invalid_encoding_and_oversize_rejected(self):
        with self.assertRaises(ValueError):
            extract_upload({'filename':'x.txt','kind':'letter','content_base64':'!'}, 'agent')
        with self.assertRaises(ValueError): read_text('x.txt', b'x' * (10*1024*1024+1))


if __name__ == '__main__':
    unittest.main()
