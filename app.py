import os
import platform
import tempfile

import docx
import pytesseract
from pdf2image import convert_from_path
from PIL import Image
import requests
import streamlit as st
from dotenv import load_dotenv

# Jika pakai openai==0.28 (interface lama)
import openai

# =========================
# 1. Konfigurasi API Key
# =========================

# Muat .env (untuk lokal)
load_dotenv()


def get_secret(name: str, default: str = "") -> str:
    """
    Ambil secret dengan prioritas:
    1. st.secrets (Streamlit Cloud)
    2. environment variable (.env atau OS)
    """
    try:
        return st.secrets[name]
    except Exception:
        return os.getenv(name, default)


OPENAI_API_KEY = get_secret("OPENAI_API_KEY")
SERPAPI_API_KEY = get_secret("SERPAPI_API_KEY")

# Set API key untuk openai (versi lama)
openai.api_key = OPENAI_API_KEY

# =========================
# 2. Konfigurasi OCR
# =========================

# Tesseract
if platform.system().lower() == "windows":
    # Sesuaikan jika instalasi Tesseract di lokasi lain
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    POPPLER_PATH = r"C:\Poppler\Library\bin"  # untuk pdf2image di Windows
else:
    # Di Linux / Streamlit Cloud, gunakan tesseract dari sistem (via apt.txt)
    pytesseract.pytesseract.tesseract_cmd = "tesseract"
    POPPLER_PATH = None  # pdf2image akan mencari poppler di PATH (poppler-utils dari apt.txt)

# =========================
# 3. Import PDF Backend
# =========================

try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except Exception:
    HAS_PYMUPDF = False
    from pdfminer.high_level import extract_text as pdfminer_extract_text


# =========================
# 4. Fungsi Pembaca File
# =========================

def extract_text_from_docx(path: str):
    doc = docx.Document(path)
    text = "\n".join(p.text for p in doc.paragraphs)
    return {1: text}


def extract_text_from_pdf(path: str):
    pages = {}
    if HAS_PYMUPDF:
        doc = fitz.open(path)
        for i, page in enumerate(doc):
            text = page.get_text("text") or ""
            pages[i + 1] = text
    else:
        # Fallback: pdfminer menghasilkan satu string panjang
        text = pdfminer_extract_text(path) or ""
        pages[1] = text
    return pages


def extract_text_from_scanned_pdf(path: str):
    # Konversi PDF -> image per halaman
    if POPPLER_PATH:
        images = convert_from_path(path, 300, poppler_path=POPPLER_PATH)
    else:
        images = convert_from_path(path, 300)

    result = {}
    # TemporaryDirectory supaya aman di semua OS
    with tempfile.TemporaryDirectory() as temp_dir:
        for i, page in enumerate(images, start=1):
            temp_image_path = os.path.join(temp_dir, f"page_{i}.jpg")
            page.save(temp_image_path, "JPEG")

            text = pytesseract.image_to_string(Image.open(temp_image_path), lang="ind+eng")
            result[i] = text or ""

    return result


def extract_text_auto(path: str):
    lower = path.lower()
    if lower.endswith(".docx"):
        return extract_text_from_docx(path)
    elif lower.endswith(".pdf"):
        pages = extract_text_from_pdf(path)
        # Jika semua halaman kosong, coba OCR
        if not any(pages.values()):
            st.warning(f"Tidak ada teks terbaca di PDF, menggunakan OCR untuk {os.path.basename(path)}.")
            return extract_text_from_scanned_pdf(path)
        return pages
    else:
        st.warning(f"Format file tidak didukung: {os.path.basename(path)}")
        return {1: ""}


# =========================
# 5. Fungsi Pencarian Lokal
# =========================

def search_keyword_in_pages(keyword: str, pages: dict):
    results = []
    k = keyword.lower().strip()
    if not k:
        return results

    for page_num, text in pages.items():
        t_lower = text.lower()
        if k in t_lower:
            start = t_lower.find(k)
            end = start + len(k)
            snippet_start = max(0, start - 40)
            snippet_end = min(len(text), end + 60)
            snippet = text[snippet_start:snippet_end].replace("\n", " ")
            results.append((page_num, snippet))
    return results


# =========================
# 6. Pencarian Internet (Standarisasi)
# =========================

def search_internet_standard(keyword: str):
    """
    Pencarian standar terkait keyword:
    - Situs: IEEE, IEC, SNI, NEMA
    - Query umum: 'standardisasi terkait <keyword>'
    """
    if not SERPAPI_API_KEY:
        st.info("SERPAPI_API_KEY belum diatur. Kolom pencarian internet akan kosong.")
        return []

    query_sites = (
        f"{keyword} site:ieeexplore.ieee.org OR site:iec.ch OR site:sni.or.id OR site:nema.org"
    )
    query_generic = f"standardisasi terkait {keyword}"

    urls = [
        f"https://serpapi.com/search?q={query_sites}&engine=google&api_key={SERPAPI_API_KEY}",
        f"https://serpapi.com/search?q={query_generic}&engine=google&api_key={SERPAPI_API_KEY}",
    ]

    results = []

    try:
        for url in urls:
            resp = requests.get(url, timeout=20)
            data = resp.json()
            for item in data.get("organic_results", []):
                title = item.get("title", "No title")
                link = item.get("link", "No link")
                snippet = item.get("snippet", "")
                results.append((title, snippet, link))
    except Exception as e:
        st.error(f"Terjadi kesalahan saat mencari di internet: {e}")
        return []

    # Hilangkan duplikat berdasarkan link
    unique = {}
    for title, snippet, link in results:
        if link and link not in unique:
            unique[link] = (title, snippet, link)

    return list(unique.values())


# =========================
# 7. Tanya Jawab dengan OpenAI (berbasis dokumen)
# =========================

def query_openai(question: str, context: str):
    """
    Mengirim pertanyaan ke OpenAI (gpt-3.5-turbo)
    Menggunakan API lama (openai==0.28) dengan ChatCompletion
    """
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY belum diatur. Isi dulu di Secrets atau .env."

    prompt = f"""
Berikut adalah beberapa contoh tanya jawab yang berkaitan dengan dokumen:

Contoh 1:
Pertanyaan: Apa itu SCADA?
Jawaban: SCADA (Supervisory Control and Data Acquisition) adalah sistem yang digunakan untuk mengawasi dan mengontrol proses industri secara jarak jauh.

Contoh 2:
Pertanyaan: Apa fungsi dari gateway SCADA?
Jawaban: Gateway SCADA bertanggung jawab untuk menghubungkan sistem SCADA dengan perangkat lain dan memastikan komunikasi yang lancar antara perangkat dan server.

Konteks:
{context}

Pertanyaan:
{question}

Jawaban (gunakan hanya informasi dari konteks di atas, jika tidak ada di konteks katakan tidak ditemukan):
"""

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": "Anda adalah asisten yang menjawab berdasarkan isi dokumen teknis yang diberikan."
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
            temperature=0.3,
        )

        return response["choices"][0]["message"]["content"].strip()
    except Exception as e:
        st.error(f"Terjadi kesalahan saat memanggil API OpenAI: {e}")
        return "Terjadi kesalahan dalam pemrosesan pertanyaan."


# =========================
# 8. UI Streamlit
# =========================

st.set_page_config(page_title="Smart Finder", layout="wide")
st.title("Smart Finder")

tab = st.radio("Pilih mode", ("Pencarian Dokumen", "Tanya Jawab"))

uploaded_files = st.file_uploader(
    "Unggah satu atau beberapa dokumen (PDF/DOCX):",
    type=["pdf", "docx"],
    accept_multiple_files=True,
)

query = st.text_input("Masukkan keyword atau pertanyaan:")

col_local, col_web = st.columns(2)

# Context untuk Tanya Jawab (dibangun dari hasil pencarian)
context = ""

if uploaded_files and query:
    with st.spinner("Memproses dokumen..."):
        results = []
        context = ""

        for uploaded_file in uploaded_files:
            suffix = os.path.splitext(uploaded_file.name)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(uploaded_file.read())
                temp_path = tmp.name

            pages = extract_text_auto(temp_path)
            found = search_keyword_in_pages(query, pages)

            # Bangun konteks untuk tab Tanya Jawab
            for page_num, snippet in found:
                context += f"Dokumen: {uploaded_file.name}, Halaman {page_num}: {snippet}\n"

            if found:
                results.append((uploaded_file.name, found))

        if tab == "Pencarian Dokumen":
            with col_local:
                st.subheader("Hasil Pencarian di Dokumen")
                if results:
                    for file_name, found in results:
                        st.markdown(f"### {file_name}")
                        for page_num, snippet in found:
                            st.markdown(f"- Halaman {page_num}: ...{snippet}...")
                else:
                    st.warning("Tidak ada hasil di dokumen untuk keyword tersebut.")

            with col_web:
                st.subheader("Hasil Pencarian Standarisasi Terkait (Internet)")
                internet_results = search_internet_standard(query)
                if internet_results:
                    for title, snippet, link in internet_results[:10]:
                        st.markdown(f"{title}\n\n{snippet}\n\n[Link]({link})")
                else:
                    st.warning("Tidak ada hasil pencarian untuk standarisasi terkait.")

if tab == "Tanya Jawab":
    st.subheader("Tanya Jawab berbasis dokumen yang diunggah")
    if not uploaded_files:
        st.info("Unggah dokumen terlebih dahulu.")
    elif not query:
        st.info("Masukkan pertanyaan di kotak input di atas.")
    else:
        with st.spinner("Menghasilkan jawaban..."):
            answer = query_openai(query, context)
            st.markdown(f"Jawaban:\n\n{answer}")
