import os
import platform
import tempfile
import json

import docx
import pytesseract
from pdf2image import convert_from_path
from PIL import Image
import requests
import streamlit as st
from dotenv import load_dotenv

# =========================
# 1. Konfigurasi secrets
# =========================

# Memuat .env saat running lokal
load_dotenv()


def get_secret(name: str, default: str = "") -> str:
    """
    Ambil secret dari Streamlit Cloud (st.secrets),
    kalau tidak ada pakai environment variable / .env.
    """
    try:
        return st.secrets[name]
    except Exception:
        return os.getenv(name, default)


GROQ_API_KEY = get_secret("GROQ_API_KEY")
SERPAPI_API_KEY = get_secret("SERPAPI_API_KEY")

# =========================
# 2. Konfigurasi OCR
# =========================

if platform.system().lower() == "windows":
    # SESUAIKAN path Tesseract dan Poppler di Windows
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    POPPLER_PATH = r"C:\Poppler\Library\bin"
else:
    # Di Linux / Streamlit Cloud biasanya paket tesseract sudah ada di PATH
    pytesseract.pytesseract.tesseract_cmd = "tesseract"
    POPPLER_PATH = None  # convert_from_path akan pakai poppler dari sistem


# =========================
# 3. Backend PDF (PyMuPDF / pdfminer)
# =========================

try:
    import fitz  # PyMuPDF

    HAS_PYMUPDF = True
except Exception:
    HAS_PYMUPDF = False
    from pdfminer.high_level import extract_text as pdfminer_extract_text


# =========================
# 4. Fungsi pembaca file (DOCX / PDF / scanned PDF)
# =========================

def extract_text_from_docx(path: str) -> dict:
    """Ekstrak teks dari DOCX, dikembalikan sebagai dict {1: full_text}."""
    doc = docx.Document(path)
    text = "\n".join(p.text for p in doc.paragraphs)
    return {1: text}


def extract_text_from_pdf(path: str) -> dict:
    """Ekstrak teks dari PDF normal (bukan hasil scan)."""
    pages = {}
    if HAS_PYMUPDF:
        doc = fitz.open(path)
        for i, page in enumerate(doc):
            text = page.get_text("text") or ""
            pages[i + 1] = text
    else:
        text = pdfminer_extract_text(path) or ""
        pages[1] = text
    return pages


def extract_text_from_scanned_pdf(path: str) -> dict:
    """Ekstrak teks dari PDF hasil scan dengan OCR (pytesseract)."""
    if POPPLER_PATH:
        images = convert_from_path(path, 300, poppler_path=POPPLER_PATH)
    else:
        images = convert_from_path(path, 300)

    result = {}
    with tempfile.TemporaryDirectory() as temp_dir:
        for i, page in enumerate(images, start=1):
            temp_image_path = os.path.join(temp_dir, f"page_{i}.jpg")
            page.save(temp_image_path, "JPEG")
            text = pytesseract.image_to_string(Image.open(temp_image_path), lang="ind+eng")
            result[i] = text or ""
    return result


def extract_text_auto(path: str) -> dict:
    """Deteksi tipe file dan ekstrak teks per halaman."""
    lower = path.lower()
    if lower.endswith(".docx"):
        return extract_text_from_docx(path)
    elif lower.endswith(".pdf"):
        pages = extract_text_from_pdf(path)
        # jika kosong (scan-only) → pakai OCR
        if not any(pages.values()):
            st.warning(f"Tidak ada teks terbaca di PDF, menggunakan OCR untuk {os.path.basename(path)}.")
            return extract_text_from_scanned_pdf(path)
        return pages
    else:
        st.warning(f"Format file tidak didukung: {os.path.basename(path)}")
        return {1: ""}


# =========================
# 5. Pencarian lokal dalam dokumen
# =========================

def search_keyword_in_pages(keyword: str, pages: dict):
    """Cari keyword di setiap halaman, kembalikan list (page_num, snippet)."""
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
# 6. Pencarian internet (standarisasi)
# =========================

def search_internet_standard(keyword: str):
    """
    Pencarian standar teknis terkait keyword menggunakan SerpAPI + Google.
    Fokus ke IEC, IEEE, SNI, NEMA + query 'standardisasi terkait <keyword>'.
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
# 7. Tanya jawab dengan Groq (Llama3 + fallback)
# =========================

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# Model utama & cadangan yang AKTIF di Groq (per Oktober 2025)
GROQ_PRIMARY_MODEL = "llama-3.3-70b-versatile"
GROQ_FALLBACK_MODEL = "llama-3.1-8b-instant"


def call_groq(model_name: str, question: str, context: str) -> str:
    """Memanggil Groq dengan model tertentu dan mengembalikan jawaban teks."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY belum diatur. Isi dulu di Secrets atau .env.")

    prompt = f"""
Berikut adalah konteks dokumen teknis PLN:

{context}

Jawab pertanyaan berikut hanya berdasarkan konteks di atas.
Jika jawabannya tidak ditemukan di konteks, jawab dengan jujur bahwa informasi tidak tersedia.

Pertanyaan: {question}
Jawaban dalam bahasa Indonesia yang jelas dan singkat:
"""

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
    }

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": "Anda adalah asisten teknis sistem tenaga listrik yang menjawab berdasarkan dokumen."
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 400,
        "temperature": 0.3,
    }

    resp = requests.post(GROQ_ENDPOINT, headers=headers, data=json.dumps(payload), timeout=60)
    data = resp.json()

    if "error" in data:
        # Naikkan sebagai exception supaya bisa di-handle fallback
        raise RuntimeError(data["error"].get("message", "Error tidak dikenal dari Groq."))

    return data["choices"][0]["message"]["content"].strip()


def query_openai(question: str, context: str) -> str:
    """
    Fungsi wrapper untuk tanya jawab.
    Nama 'query_openai' dipertahankan untuk kompatibilitas, tapi backend-nya Groq.
    Pakai model utama, kalau gagal → fallback.
    """
    try:
        return call_groq(GROQ_PRIMARY_MODEL, question, context)
    except Exception as e:
        # Tampilkan peringatan tapi tetap coba model cadangan
        st.warning(f"Model utama error: {e}. Mencoba model cadangan...")
        try:
            return call_groq(GROQ_FALLBACK_MODEL, question, context)
        except Exception as e2:
            return f"Model fallback juga gagal: {e2}"


# =========================
# 8. UI Streamlit
# =========================

st.set_page_config(page_title="Smart Finder", layout="wide")
st.title("📂 Smart Finder")

# Pilihan mode
tab = st.radio("Pilih mode", ("Pencarian Dokumen", "Tanya Jawab"))

# Upload dokumen
uploaded_files = st.file_uploader(
    "Unggah satu atau beberapa dokumen (PDF/DOCX):",
    type=["pdf", "docx"],
    accept_multiple_files=True,
)

# Satu input untuk keyword / pertanyaan
query = st.text_input("Masukkan keyword atau pertanyaan:")

# Kolom untuk tab Pencarian
col_local, col_web = st.columns(2)

# Konteks global untuk tanya jawab
context = ""

# =========================
# 9. Proses dokumen & pencarian
# =========================

if uploaded_files and query:
    with st.spinner("Memproses dokumen..."):
        results = []
        context_parts = []

        for uploaded_file in uploaded_files:
            suffix = os.path.splitext(uploaded_file.name)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(uploaded_file.read())
                temp_path = tmp.name

            pages = extract_text_auto(temp_path)
            found = search_keyword_in_pages(query, pages)

            # kumpulkan konteks untuk tanya jawab
            for page_num, snippet in found:
                context_parts.append(
                    f"Dokumen: {uploaded_file.name}, Halaman {page_num}: {snippet}"
                )

            if found:
                results.append((uploaded_file.name, found))

        context = "\n".join(context_parts)

        # =========================
        # Mode Pencarian Dokumen
        # =========================
        if tab == "Pencarian Dokumen":
            # Hasil di dokumen (kolom kiri)
            with col_local:
                st.subheader("🔍 Hasil Pencarian di Dokumen")
                if results:
                    for file_name, found in results:
                        st.markdown(f"### 📘 {file_name}")
                        for page_num, snippet in found:
                            st.markdown(f"- Halaman {page_num}: ...{snippet}...")
                else:
                    st.warning("Tidak ada hasil di dokumen untuk keyword tersebut.")

            # Hasil standar di internet (kolom kanan)
            with col_web:
                st.subheader("🌐 Hasil Pencarian Standarisasi Terkait (Internet)")
                internet_results = search_internet_standard(query)
                if internet_results:
                    for title, snippet, link in internet_results[:10]:
                        st.markdown(f"**{title}**")
                        st.write(snippet)
                        st.markdown(f"[Link]({link})")
                        st.markdown("---")
                else:
                    st.warning("Tidak ada hasil pencarian untuk standarisasi terkait.")

# =========================
# 10. Mode Tanya Jawab
# =========================

if tab == "Tanya Jawab":
    st.subheader("💬 Tanya Jawab berbasis dokumen yang diunggah")

    if not uploaded_files:
        st.info("Unggah dokumen terlebih dahulu.")
    elif not query:
        st.info("Masukkan pertanyaan di kotak input di atas.")
    else:
        if not context:
            st.warning("Belum ada konteks yang cocok dengan pertanyaan Anda di dokumen.")
        with st.spinner("Menghasilkan jawaban dari Groq..."):
            answer = query_openai(query, context)
            st.markdown("**Jawaban:**")
            st.write(answer)

# =========================
# 11. Footer Kredit
# =========================

st.markdown("""
<hr style="margin-top: 50px; margin-bottom: 10px;">
<div style="text-align: center; color: gray; font-size: 14px;">
    created by: <b>kemasmtofani</b> @ PLN Puslitbang
</div>
""", unsafe_allow_html=True)
