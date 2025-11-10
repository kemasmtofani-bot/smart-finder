import os, platform
import tempfile
import fitz  # PyMuPDF
import docx
import pytesseract
from pdf2image import convert_from_path
from PIL import Image
import openai
import requests
import streamlit as st
from dotenv import load_dotenv

# --- KONFIGURASI ---
# Memuat file .env untuk mengambil API key
load_dotenv()

# Ambil API key OpenAI dari environment variable
openai.api_key = os.getenv('OPENAI_API_KEY')

# --- Konfigurasi untuk OCR dan PDF processing ---
# set tesseract cmd otomatis
import pytesseract
if platform.system().lower() == "windows":
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
else:
    pytesseract.pytesseract.tesseract_cmd = "tesseract"  # di Streamlit Cloud


POPPLER_PATH = r"C:\Poppler\Library\bin"  # ubah sesuai lokasi Poppler kamu


# --- FUNGSI PEMBACA FILE ---
def extract_text_from_docx(path):
    doc = docx.Document(path)
    text = "\n".join([p.text for p in doc.paragraphs])
    return {1: text}

# coba import PyMuPDF
try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except Exception:
    HAS_PYMUPDF = False
    from pdfminer.high_level import extract_text as pdfminer_extract_text

# fungsi ekstrak pdf
def extract_text_from_pdf(path):
    pages = {}
    if HAS_PYMUPDF:
        doc = fitz.open(path)
        for i, page in enumerate(doc):
            pages[i + 1] = page.get_text("text")
    else:
        # pdfminer menghasilkan 1 string, simpan sebagai 1 halaman agar tidak crash
        text = pdfminer_extract_text(path) or ""
        pages[1] = text
    return pages


def extract_text_from_scanned_pdf(path):
    pages = convert_from_path(path, 300, poppler_path=POPPLER_PATH)
    result = {}

    # Tentukan folder sementara yang aman untuk menyimpan gambar
    temp_dir = "C:\\temp_images"
    os.makedirs(temp_dir, exist_ok=True)  # Membuat folder sementara jika belum ada

    for i, page in enumerate(pages):
        # Menyimpan gambar sementara dalam folder yang bisa diakses
        temp_image_path = os.path.join(temp_dir, f"temp_page_{i + 1}.jpg")
        page.save(temp_image_path, "JPEG")

        # Menggunakan pytesseract untuk OCR pada gambar yang disimpan
        text = pytesseract.image_to_string(Image.open(temp_image_path), lang="ind+eng")
        result[i + 1] = text

        # Menghapus gambar sementara setelah digunakan
        os.remove(temp_image_path)

    return result


def extract_text_auto(path):
    if path.lower().endswith(".docx"):
        return extract_text_from_docx(path)
    elif path.lower().endswith(".pdf"):
        text_pages = extract_text_from_pdf(path)
        # Jika hasil PDF kosong (misal hasil scan), pakai OCR
        if not any(text_pages.values()):
            st.warning(f"⚠️ Tidak ada teks yang ditemukan di PDF, menggunakan OCR untuk {path}")
            return extract_text_from_scanned_pdf(path)
        return text_pages
    else:
        st.warning(f"⚠️ Format file {path} tidak didukung!")
        return {1: ""}


# --- FUNGSI PENCARIAN ---
def search_keyword_in_pages(keyword, pages):
    results = []
    for page_num, text in pages.items():
        if keyword.lower() in text.lower():
            start = text.lower().find(keyword.lower())
            end = start + len(keyword)
            snippet_start = max(0, start - 40)  # menambah buffer 40 karakter sebelum keyword
            snippet_end = min(len(text), end + 60)  # menambah buffer 60 karakter setelah keyword
            snippet = text[snippet_start:snippet_end].replace("\n", " ")
            results.append((page_num, snippet))
    return results


# --- FUNGSI PENCARIAN INTERNET DENGAN SERPAPI (Fokus pada Standarisasi) ---
def search_internet_standard(keyword):
    """Melakukan pencarian keyword di internet untuk standar terkait menggunakan SerpAPI"""
    # Ambil API key SerpAPI dari environment variable
    api_key = os.getenv('SERPAPI_API_KEY')

    if not api_key:
        st.error("API Key SerpAPI tidak ditemukan. Pastikan API Key diatur dengan benar.")
        return []

    # Mencari di sumber yang relevan untuk standar seperti IEEE, IEC, NEMA, SNI
    query = f"{keyword} site:ieeexplore.ieee.org OR site:iec.ch OR site:sni.or.id"
    # Menambahkan pencarian Google yang lebih umum dengan kata kunci 'standardisasi terkait'
    query_google = f"standardisasi terkait {keyword}"

    # URL untuk SerpAPI (pencarian khusus standar)
    url_standard = f"https://serpapi.com/search?q={query}&engine=google&api_key={api_key}"

    # URL untuk pencarian umum (mencari kata kunci 'standardisasi terkait')
    url_google = f"https://serpapi.com/search?q={query_google}&engine=google&api_key={api_key}"

    try:
        # Mengirim permintaan ke SerpAPI untuk pencarian standar
        response_standard = requests.get(url_standard)
        response_google = requests.get(url_google)

        data_standard = response_standard.json()
        data_google = response_google.json()

        # Ambil hasil pencarian dari sumber yang relevan untuk standar
        results = []
        for result in data_standard.get('organic_results', []):
            title = result.get('title', 'No title')
            link = result.get('link', 'No link')
            snippet = result.get('snippet', 'No snippet')
            results.append((title, snippet, link))

        # Ambil hasil pencarian dari Google (pencarian umum)
        for result in data_google.get('organic_results', []):
            title = result.get('title', 'No title')
            link = result.get('link', 'No link')
            snippet = result.get('snippet', 'No snippet')
            results.append((title, snippet, link))

        return results
    except Exception as e:
        st.error(f"Terjadi kesalahan saat mencari di internet: {e}")
        return []


# --- FUNGSI CHATGPT DENGAN FEW-SHOT LEARNING ---
def query_openai(question, context):
    """Mengirimkan pertanyaan ke OpenAI GPT-3 untuk dijawab berdasarkan konteks dokumen"""

    # Contoh prompt dengan few-shot learning untuk memperjelas konteks
    prompt = f"""
    Berikut adalah beberapa contoh tanya jawab yang berkaitan dengan dokumen:

    **Contoh 1:**
    Pertanyaan: Apa itu SCADA?
    Jawaban: SCADA (Supervisory Control and Data Acquisition) adalah sistem yang digunakan untuk mengawasi dan mengontrol proses industri secara jarak jauh.

    **Contoh 2:**
    Pertanyaan: Apa fungsi dari gateway SCADA?
    Jawaban: Gateway SCADA bertanggung jawab untuk menghubungkan sistem SCADA dengan perangkat lain dan memastikan komunikasi yang lancar antara perangkat dan server.

    **Konteks:**
    {context}

    **Pertanyaan:**
    {question}

    **Jawaban:**
    """

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",  # Bisa menggunakan "gpt-4" jika akses ada
            messages=[
                {"role": "system", "content": "Anda adalah asisten yang membantu menjawab pertanyaan terkait dokumen."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=150,
            temperature=0.7
        )

        return response['choices'][0]['message']['content'].strip()
    except Exception as e:
        st.error(f"Terjadi kesalahan saat memanggil API OpenAI: {e}")
        return "Terjadi kesalahan dalam pemrosesan pertanyaan."


# --- UI STREAMLIT ---
st.set_page_config(page_title="Smart Finder", layout="wide")
st.title("📂 Smart Finder")

# Menambahkan tab untuk ChatGPT
tabs = st.radio("Pilih Tab", ("Pencarian Dokumen", "Tanya Jawab"))

# Variabel untuk menyimpan konteks gabungan dari dokumen
context = ""

# Mengunggah dokumen dan memasukkan keyword
uploaded_files = st.file_uploader(
    "📁 Unggah satu atau beberapa dokumen (PDF/DOCX):",
    type=["pdf", "docx"],
    accept_multiple_files=True
)

keyword = st.text_input("🔍 Masukkan keyword atau pertanyaan:")

# Membuat dua kolom untuk hasil pencarian
col1, col2 = st.columns(2)

if uploaded_files and keyword:
    with st.spinner("🔄 Memproses dokumen..."):
        results = []
        context = ""  # Reset context setiap kali dokumen diunggah
        for uploaded_file in uploaded_files:
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(uploaded_file.name)[1]) as tmp:
                tmp.write(uploaded_file.read())
                tmp_path = tmp.name

            pages = extract_text_auto(tmp_path)
            found = search_keyword_in_pages(keyword, pages)

            # Menggabungkan teks dari seluruh dokumen untuk menjadi konteks ChatGPT
            for page_num, snippet in found:
                context += f"Halaman {page_num}: {snippet}\n"

            if found:
                results.append((uploaded_file.name, found))

        if results:
            with col1:
                st.subheader("🔍 Hasil Pencarian di Dokumen")
                for file_name, found in results:
                    st.markdown(f"### 📘 {file_name}")
                    for page_num, snippet in found:
                        st.markdown(f"- **Halaman {page_num}:** ...{snippet}...")
                st.divider()

        # Pencarian di Internet untuk Standarisasi
        internet_results = search_internet_standard(keyword)

        if internet_results:
            with col2:
                st.subheader("🔍 Hasil Pencarian Standarisasi Terkait")
                for title, snippet, link in internet_results:
                    st.markdown(f"**{title}**\n{snippet}\n[Link]({link})")
                st.divider()
        else:
            with col2:
                st.warning("❌ Tidak ada hasil pencarian untuk standarisasi terkait.")

# Fitur Tanya Jawab
if tabs == "Tanya Jawab":
    st.subheader("💬 Tanya Jawab tentang Dokumen")
    if keyword and context:  # Mengecek apakah ada input dan konteks
        with st.spinner("🔄 Memproses pertanyaan..."):
            # Panggil query_openai dengan keyword dan context
            answer = query_openai(keyword, context)

            # Periksa apakah response berhasil dan tidak None
            if answer:
                st.markdown(f"**Jawaban:** {answer}")
            else:
                st.warning("Tidak dapat memberikan jawaban. Pastikan dokumen sudah diproses dengan benar.")
    else:
        if not context:
            st.info("Silakan unggah dokumen terlebih dahulu untuk memulai tanya jawab.")
        elif not keyword:
            st.info("Masukkan pertanyaan untuk mendapatkan jawaban berdasarkan dokumen yang diunggah.")

