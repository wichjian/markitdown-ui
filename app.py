import base64
import json
import os
import re
import string
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file, Response

# ── Configuration ──────────────────────────────────────────────────────────────
_IS_WIN         = sys.platform == "win32"
MARKITDOWN      = str(Path.home() / ".local" / "bin" / "markitdown.exe") if _IS_WIN else "markitdown"
DEFAULT_OUTPUT  = str(Path.home() / "Downloads") if _IS_WIN else tempfile.gettempdir()
TESSDATA_PREFIX = str(Path.home() / "tessdata") if _IS_WIN else "/usr/share/tessdata"
TESSERACT_CMD   = r"C:\Program Files\Tesseract-OCR\tesseract.exe" if _IS_WIN else "/usr/bin/tesseract"
IMAGE_EXTS      = {'.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.gif', '.webp'}
DOC_EXTS        = {'.doc', '.docx'}
GEMINI_MODELS   = ("gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-3.5-flash", "gemini-2.5-flash")
MIME_MAP        = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.tiff': 'image/tiff', '.tif': 'image/tiff',
    '.bmp': 'image/bmp', '.gif': 'image/gif', '.webp': 'image/webp',
}
# cp1252: Windows maps 0x80-0x9F to special chars (ใช้แก้ mojibake แบบ mixed)
_CP1252 = {
    '€': 0x80, '‚': 0x82, 'ƒ': 0x83, '„': 0x84,
    '…': 0x85, '†': 0x86, '‡': 0x87, 'ˆ': 0x88,
    '‰': 0x89, 'Š': 0x8a, '‹': 0x8b, 'Œ': 0x8c,
    'Ž': 0x8e, '‘': 0x91, '’': 0x92, '“': 0x93,
    '”': 0x94, '•': 0x95, '–': 0x96, '—': 0x97,
    '˜': 0x98, '™': 0x99, 'š': 0x9a, '›': 0x9b,
    'œ': 0x9c, 'ž': 0x9e, 'Ÿ': 0x9f,
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024


def _check_auth(req) -> bool:
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        user, pwd = decoded.split(":", 1)
        return user == os.environ.get("BASIC_AUTH_USER", "") and pwd == os.environ.get("BASIC_AUTH_PASS", "")
    except Exception:
        return False


@app.before_request
def require_auth():
    if not _check_auth(request):
        return Response("Authentication required", 401,
                        {"WWW-Authenticate": 'Basic realm="MarkItDown UI"'})


# ── Text helpers ───────────────────────────────────────────────────────────────
def _is_mojibake(text: str) -> bool:
    return 'à¸' in text or 'à¹' in text


def _strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        text = re.sub(r'^```[^\n]*\n?', '', text)
        text = re.sub(r'\n?```$', '', text.strip())
    return text


def _fix_thai_spaces(text: str) -> str:
    return re.sub(r'(?<=[฀-๿]) +(?=[฀-๿])', '', text)


def _fix_split_numbers(text: str) -> str:
    """แก้ตัวเลขที่ PDF ใส่ช่องว่างกลางหลัก: '1 51' → '151'"""
    return re.sub(r'(\s{3,})([1-9]) (\d{1,2})(?=[\s|]|$)',
                  lambda m: m.group(1) + m.group(2) + m.group(3), text)


def _has_cid(text: str) -> bool:
    return bool(re.search(r'\(cid:\d+\)', text))


def _fix_mojibake(text: str) -> tuple:
    """แก้ mojibake: UTF-8 bytes ที่ถูก decode เป็น Latin-1 หรือ cp1252
    คืน (fixed_text, was_fixed)"""
    if not _is_mojibake(text):
        return text, False
    # Latin-1: bytes 0x00-0xFF map 1:1 to U+0000-U+00FF
    try:
        return text.encode('latin-1').decode('utf-8'), True
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    # cp1252: Windows maps 0x80-0x9F to special chars (€ ‚ „ … ‹ › etc.)
    try:
        return text.encode('cp1252').decode('utf-8'), True
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    # Mixed: แปลง cp1252-special chars กลับเป็น byte ก่อน decode UTF-8
    try:
        raw = bytearray(
            ord(c) if ord(c) <= 0xFF else _CP1252.get(c, 0x3F) for c in text)
        result = bytes(raw).decode('utf-8', errors='replace')
        if sum(1 for c in result if 'ก' <= c <= '๿') > 10:
            return result, True
    except Exception:
        pass
    return text, False


def _thai_ratio(text: str) -> float:
    """สระ+วรรณยุต / พยัญชนะ — ปกติ > 0.40 ถ้า Thai สมบูรณ์"""
    consonants = sum(1 for c in text if 'ก' <= c <= 'ฮ')
    if consonants < 30:
        return 1.0
    combining = sum(1 for c in text if 'ะ' <= c <= '๎')
    return combining / consonants


# ── Gemini core ────────────────────────────────────────────────────────────────
def _gemini_call(prompt: str, text: str = "", tag: str = "",
                 timeout: int = 60, img_b64: str = None, mime_type: str = None):
    """เรียก Gemini API (text หรือ vision) คืน result string หรือ None"""
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        parts = ([{"inlineData": {"mimeType": mime_type, "data": img_b64}}, {"text": prompt}]
                 if img_b64 else [{"text": prompt + text}])
        payload = json.dumps({
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0}
        }).encode("utf-8")
        for model in GEMINI_MODELS:
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent?key={api_key}")
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"})
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        data = json.loads(resp.read())
                    result = _strip_code_fence(
                        data["candidates"][0]["content"]["parts"][0]["text"].strip())
                    return result or None
                except Exception as em:
                    if "429" in str(em) and attempt == 0:
                        print(f"[{tag}] {model}: 429, retry 15s...", flush=True)
                        time.sleep(15)
                        continue
                    print(f"[{tag}] {model}: {em}", flush=True)
                    break
    except Exception as e:
        print(f"[{tag} ERROR] {e}", flush=True)
    return None


# ── AI functions ───────────────────────────────────────────────────────────────
def gemini_vision_to_md(input_path: str) -> str:
    """Gemini Vision อ่านรูปภาพ → Markdown คืน str ('' ถ้าไม่สำเร็จ)"""
    mime_type = MIME_MAP.get(Path(input_path).suffix.lower(), 'image/png')
    try:
        with open(input_path, 'rb') as f:
            img_b64 = base64.b64encode(f.read()).decode('utf-8')
    except Exception as e:
        print(f"[vision] read error: {e}", flush=True)
        return ""
    prompt = (
        "อ่านข้อความทั้งหมดจากรูปภาพนี้ แปลงเป็น Markdown\n"
        "กฎ: รักษาโครงสร้างตาราง (|) | ตัวเลขทุกตัวถูกต้อง | CODE เช่น A2024 | ภาษาไทยสมบูรณ์ไม่ขาดตัวอักษร\n"
        "คืนเฉพาะ Markdown ไม่ต้องอธิบาย ไม่ใส่ ```markdown``` wrapper"
    )
    return _gemini_call(prompt, tag="vision", img_b64=img_b64, mime_type=mime_type) or ""


def ai_fix_mojibake_image_text(text: str) -> str:
    """Gemini decode mojibake → Thai ที่ถูกต้อง"""
    prompt = (
        "ข้อความต่อไปนี้คือ mojibake: ภาษาไทย UTF-8 ที่ถูก decode ผิดเป็น Latin-1\n"
        "รูปแบบ: à¸ หรือ à¹ นำหน้าตัวอักษรที่ decode ผิด เช่น 'à¸à' = ก, 'à¸£' = ร, 'à¹à¸£à¸·à¹à¸­à¸' = เรื่อง\n"
        "ช่วย decode กลับเป็นภาษาไทยที่ถูกต้องตามบริบท แก้ไขตามความหมายของประโยคให้สมบูรณ์\n"
        "กฎ: รักษา Markdown (|, -, #) | ตัวเลขทุกตัวถูกต้อง | ภาษาอังกฤษเหมือนเดิม\n"
        "คืนเฉพาะข้อความที่แก้ไขแล้ว ไม่ต้องอธิบาย ไม่ต้องใส่ ```markdown``` wrapper\n\n"
    )
    return _gemini_call(prompt, text, "mojibake") or text


def ai_proofread_image_text(text: str) -> str:
    """แก้ OCR errors + จัดรูปแบบ Markdown (1 API call)"""
    prompt = (
        "ข้อความต่อไปนี้ได้จาก OCR (Tesseract) อ่านจากรูปภาพ อาจมีตัวอักษรผิดพลาด\n"
        "งาน: (1) แก้ไขข้อความให้ถูกต้องสมบูรณ์ตามบริบทของประโยค (2) จัดรูปแบบเป็น Markdown ที่เหมาะสม\n"
        "แก้ไข: คำผิดตามบริบท | ตัวอักษรขาด/เกิน | สระ/วรรณยุตผิดที่ | ช่องว่างระหว่างตัวอักษรไทย\n"
        "จัด Markdown: หัวข้อ (#/##) | ย่อหน้า | รายการ (-) | ตาราง (|) ถ้ามีโครงสร้างนั้นจริง\n"
        "กฎ: ตัวเลขทุกตัวถูกต้อง | CODE (A2024 ฯลฯ) | ภาษาอังกฤษเหมือนเดิม | ห้ามเพิ่มเนื้อหาใหม่\n"
        "คืนเฉพาะ Markdown ที่แก้ไขแล้ว ไม่ต้องอธิบาย ไม่ต้องใส่ ```markdown``` wrapper\n\n"
    )
    return _gemini_call(prompt, text, "proofread") or text


def ai_fix_thai(text: str) -> str:
    """แก้ Thai encoding errors จาก PDF (ตัวอักษรสลับที่/สระหาย)"""
    prompt = (
        "แก้ไขข้อความภาษาไทยที่มีปัญหาจาก PDF encoding (ตัวอักษรสลับที่/สระหาย)\n"
        "ตัวอย่าง: ตน้→ต้น, มากกวา่→มากกว่า, ขึน้→ขึ้น, จา นวน→จำนวน, ตวั→ตัว, ผใู้→ผู้ใ\n"
        "กฎ: รักษา Markdown (|, -, #) | ตัวเลขทุกตัว | CODE (A2024 ฯลฯ) | ภาษาอังกฤษเหมือนเดิม\n"
        "ห้ามลบหรือเพิ่มพยัญชนะ — สลับที่เท่านั้น\n"
        "คืนเฉพาะข้อความที่แก้ไขแล้ว ไม่ต้องอธิบาย ไม่ต้องใส่ ```markdown``` wrapper\n\n"
    )
    return _gemini_call(prompt, text, "fix_thai", timeout=30) or text


# ── OCR / Extraction ───────────────────────────────────────────────────────────
def image_to_md_ocr(input_path: str) -> str:
    """Tesseract OCR รูปภาพ คืน str ('' ถ้าอ่านไม่ได้)"""
    try:
        import pytesseract
        from PIL import Image
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
        os.environ["TESSDATA_PREFIX"] = TESSDATA_PREFIX
        img = Image.open(input_path)
        text = pytesseract.image_to_string(img, lang="tha+eng", config="--psm 6")
        return _fix_thai_spaces(text).strip()
    except Exception:
        return ""


def _ocr_page(page) -> str:
    """Tesseract OCR หน้าเดียว (รองรับ Thai font ที่ไม่มี Unicode map)"""
    try:
        import fitz
        import pytesseract
        from PIL import Image
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
        os.environ["TESSDATA_PREFIX"] = TESSDATA_PREFIX
        # ส่ง raw pixels ให้ PIL ตรงๆ ไม่ต้องบีบอัด/คลาย PNG (ภาพ 300dpi ใหญ่)
        pix = page.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72))
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return _fix_thai_spaces(
            pytesseract.image_to_string(img, lang="tha+eng", config="--psm 6")).strip()
    except Exception as e:
        print(f"[pdf] ocr page error: {e}", flush=True)
        return ""


def _vision_page(page) -> str:
    """Gemini Vision อ่านหน้าเดียว — สำหรับหน้า scan/กราฟ/ไดอะแกรม/ตารางที่วิธีอื่นอ่านไม่ได้"""
    try:
        import fitz
        pix = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72))
        img_b64 = base64.b64encode(pix.tobytes("png")).decode("utf-8")
    except Exception as e:
        print(f"[pdf] vision render error: {e}", flush=True)
        return ""
    prompt = (
        "อ่านเนื้อหาทั้งหมดจากภาพหน้าเอกสารนี้ แปลงเป็น Markdown\n"
        "กฎ: รักษาโครงสร้างตาราง (|) | ตัวเลขทุกตัวถูกต้อง | CODE เช่น A2024 | ภาษาไทยสมบูรณ์\n"
        "ถ้าเป็นกราฟ/ไดอะแกรม/รูปภาพ ให้สรุปเนื้อหาเป็น blockquote (>) พร้อมข้อมูล/ตัวเลขที่อ่านได้\n"
        "คืนเฉพาะ Markdown ไม่ต้องอธิบาย ไม่ใส่ ```markdown``` wrapper"
    )
    return _gemini_call(prompt, tag="vision-page", img_b64=img_b64, mime_type="image/png") or ""


def doc_to_md(input_path: str, output_path: str) -> tuple:
    """แปลง .doc/.docx → text ด้วย mammoth (fallback: win32com Word)"""
    try:
        import mammoth
        with open(input_path, 'rb') as f:
            result = mammoth.extract_raw_text(f)
        text = result.value.strip()
        if text:
            with open(output_path, 'w', encoding='utf-8-sig') as fw:
                fw.write(text)
            return True, "mammoth"
    except Exception:
        pass
    try:
        import win32com.client
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        doc = None
        try:
            doc = word.Documents.Open(str(Path(input_path).resolve()))
            text = doc.Content.Text.strip()
        finally:
            if doc:
                doc.Close(False)
            word.Quit()
        if text:
            with open(output_path, 'w', encoding='utf-8-sig') as fw:
                fw.write(text)
            return True, "word-com"
    except Exception:
        pass
    return False, ""


def _ai_fix_pages(parts: list) -> tuple:
    """ai_fix_thai เฉพาะหน้าที่ต้องซ่อม (text/ocr) โดยรวมหน้าติดกันเป็น chunk ≤ 8000 ตัวอักษร
    parts = [(เนื้อหา, needs_ai)] คืน (content, ai_used)"""
    out, ai_used = [], False
    i = 0
    while i < len(parts):
        content, needs = parts[i]
        if not needs:
            out.append(content)
            i += 1
            continue
        buf, size, j = [content], len(content), i + 1
        while j < len(parts) and parts[j][1] and size + len(parts[j][0]) <= 8000:
            buf.append(parts[j][0])
            size += len(parts[j][0])
            j += 1
        raw = "\n\n---\n\n".join(buf)
        fixed = ai_fix_thai(raw)
        if fixed != raw:
            ai_used = True
        out.append(fixed)
        i = j
    return "\n\n---\n\n".join(out), ai_used


def pdf_to_md_per_page(input_path: str) -> tuple:
    """แปลง PDF ทีละหน้า เลือกวิธีที่เหมาะกับแต่ละหน้า:
    1) text layer (pymupdf4llm — ตารางจากพิกัด geometry)
    2) Tesseract OCR (หน้า scan/font ไม่มี Unicode map)
    3) Gemini Vision (หน้ากราฟ/ไดอะแกรม/รูป หรือ OCR อ่านไม่ออก)
    แล้ว ai_fix_thai เป็น chunk ตามขอบเขตหน้า คืน (content, method) หรือ (None, None)"""
    import fitz
    import pymupdf4llm
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    doc = fitz.open(input_path)
    total = len(doc)
    parts = []  # (เนื้อหา, needs_ai)
    used = {"text": 0, "ocr": 0, "vision": 0, "skip": 0}
    # ดึง text layer ครั้งเดียวทั้งเล่ม (เร็วกว่าเรียกทีละหน้า ซึ่งวิเคราะห์ font ซ้ำทุกครั้ง)
    try:
        chunks = pymupdf4llm.to_markdown(doc, page_chunks=True, show_progress=False)
    except Exception:
        chunks = None
    for i, page in enumerate(doc):
        n = i + 1
        if chunks is not None:
            txt = chunks[i].get("text", "")
        else:
            try:
                txt = pymupdf4llm.to_markdown(doc, pages=[i], show_progress=False)
            except Exception:
                txt = ""
        txt, _ = _fix_mojibake(txt)
        txt = txt.strip()
        has_images = bool(page.get_images(full=True))
        # หน้าที่เนื้อหาหลักเป็นรูป (scan/กราฟ/ไดอะแกรม): text layer สั้นแต่มีรูป
        mostly_image = has_images and len(txt) < 200
        # text layer ใช้ได้: ไม่ mojibake/cid, ยาวพอ, พยัญชนะไม่ขยะ
        # (สระ/วรรณยุกต์หายไม่เป็นไร — ai_fix_thai กู้ได้ ratio จึงตั้งต่ำแค่กันขยะ)
        if txt and len(txt) >= 40 and not _is_mojibake(txt) and not _has_cid(txt) \
                and _thai_ratio(txt) >= 0.15 and not mostly_image:
            parts.append((_fix_split_numbers(txt), True))
            used["text"] += 1
            print(f"[pdf] page {n}/{total}: text", flush=True)
            continue
        # หน้ารูป + มี api_key: ไป Vision เลย ไม่เสีย OCR ฟรี (ผล OCR ถูกใช้แค่ fallback)
        ocr = None if (mostly_image and api_key) else _ocr_page(page)
        if ocr and len(ocr) >= 40 and _thai_ratio(ocr) >= 0.30 and not mostly_image:
            parts.append((ocr, True))
            used["ocr"] += 1
            print(f"[pdf] page {n}/{total}: ocr", flush=True)
            continue
        if api_key and (has_images or (ocr and len(ocr) >= 40)):
            vis = _vision_page(page)
            if vis.strip():
                parts.append((vis.strip(), False))
                used["vision"] += 1
                print(f"[pdf] page {n}/{total}: vision", flush=True)
                continue
        # ไม่มีวิธีไหนได้คุณภาพ → ใช้อันที่ยาวที่สุดเท่าที่มี
        if ocr is None:
            ocr = _ocr_page(page)
        best = ocr if len(ocr) > len(txt) else txt
        if best:
            parts.append((best, True))
            used["ocr" if best is ocr else "text"] += 1
            print(f"[pdf] page {n}/{total}: fallback", flush=True)
        else:
            parts.append((f"*[หน้า {n}: ไม่สามารถอ่านเนื้อหาได้]*", False))
            used["skip"] += 1
            print(f"[pdf] page {n}/{total}: skip", flush=True)
    doc.close()
    if not any(p for p, _ in parts):
        return None, None
    content, ai_used = _ai_fix_pages(parts)
    summary = " ".join(f"{k}:{v}" for k, v in used.items() if v)
    method = f"per-page({summary})" + ("+ai" if ai_used else "")
    return content, method


_TOKEN_FILE = re.compile(r'^[0-9a-f]{32}_')


def _cleanup_old_outputs(max_age_sec: int = 3600):
    """ลบไฟล์ output เก่า ({token}_xxx) ใน temp dir ที่อายุเกินกำหนด"""
    now = time.time()
    try:
        for p in Path(tempfile.gettempdir()).iterdir():
            if _TOKEN_FILE.match(p.name):
                try:
                    if now - p.stat().st_mtime > max_age_sec:
                        p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


# ── Flask routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", default_output=DEFAULT_OUTPUT)


@app.route("/convert", methods=["POST"])
def convert():
    _cleanup_old_outputs()
    url = request.form.get("url", "").strip()
    env = os.environ.copy()
    env["PATH"] = str(Path.home() / ".local" / "bin") + ";" + env.get("PATH", "")
    token = uuid.uuid4().hex
    tmp_dir = tempfile.gettempdir()

    if url:
        slug = url.rstrip("/").split("/")[-1] or "output"
        slug = slug.split("?")[0]
        if "." in slug:
            slug = slug.rsplit(".", 1)[0]
        if not slug:
            slug = "output"
        fname = slug + ".md"
        output_file = os.path.join(tmp_dir, f"{token}_{fname}")
        result = subprocess.run(
            [MARKITDOWN, url, "-o", output_file],
            capture_output=True, text=True, env=env
        )
        if result.returncode == 0 and os.path.exists(output_file) and os.path.getsize(output_file) > 0:
            return jsonify({"success": True, "filename": fname,
                            "download_url": f"/download/{token}/{fname}"})
        return jsonify({"success": False,
                        "error": result.stderr.strip() or "แปลง URL ไม่สำเร็จ"})

    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"success": False, "error": "ไม่พบไฟล์หรือ URL"})

    f = request.files["file"]
    suffix = Path(f.filename).suffix.lower()
    stem = Path(f.filename).stem
    fname = stem + ".md"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    try:
        output_file = os.path.join(tmp_dir, f"{token}_{fname}")
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")

        # ── PDF ────────────────────────────────────────────────────────────────
        if suffix == ".pdf":
            content, method = pdf_to_md_per_page(tmp_path)
            if not content:
                return jsonify({"success": False, "error": "ไม่สามารถดึงข้อความจาก PDF ได้"})
            with open(output_file, "w", encoding="utf-8-sig") as fw:
                fw.write(content)
            return jsonify({"success": True, "filename": fname,
                            "download_url": f"/download/{token}/{fname}", "method": method})

        # ── Image ──────────────────────────────────────────────────────────────
        if suffix in IMAGE_EXTS:
            method = ""
            print(f"[image] {stem}{suffix} api_key={'YES' if api_key else 'NO'}", flush=True)

            # Step 1: OCR (Tesseract — ฟรี ไม่ใช้ API)
            content = image_to_md_ocr(tmp_path)
            if content:
                method = "ocr"
                print(f"[image] OCR OK len={len(content)}", flush=True)
            elif api_key:
                # Fallback: Gemini Vision
                content = gemini_vision_to_md(tmp_path).strip()
                if content:
                    method = "gemini-vision"
                    print(f"[image] vision OK len={len(content)}", flush=True)

            if not content:
                return jsonify({"success": False, "error": "ไม่สามารถอ่านข้อความจากรูปภาพได้"})

            # Step 2: AI แก้ OCR errors + format Markdown (Vision ดีอยู่แล้ว ไม่ต้อง)
            if api_key and method == "ocr":
                print("[image] step2: ai correct+format...", flush=True)
                corrected = ai_proofread_image_text(content)
                if corrected and corrected.strip() and corrected != content:
                    content = corrected
                    method = "ocr+ai"

            # Step 3: Save
            with open(output_file, "w", encoding="utf-8-sig") as fw:
                fw.write(content)
            print(f"[image] done method={method}", flush=True)
            return jsonify({"success": True, "filename": fname,
                            "download_url": f"/download/{token}/{fname}", "method": method})

        # ── Word ───────────────────────────────────────────────────────────────
        if suffix in DOC_EXTS:
            result = subprocess.run(
                [MARKITDOWN, tmp_path, "-o", output_file],
                capture_output=True, text=True, env=env
            )
            content = ""
            if result.returncode == 0 and os.path.exists(output_file):
                content = open(output_file, encoding="utf-8-sig").read()
            if content.strip():
                method = "markitdown"
            else:
                ok, method = doc_to_md(tmp_path, output_file)
                if not ok:
                    return jsonify({"success": False,
                                    "error": "ไม่สามารถอ่านไฟล์ Word ได้ (ต้องการ Microsoft Word หรือไฟล์ .docx)"})
            return jsonify({"success": True, "filename": fname,
                            "download_url": f"/download/{token}/{fname}", "method": method})

        # ── Others ─────────────────────────────────────────────────────────────
        result = subprocess.run(
            [MARKITDOWN, tmp_path, "-o", output_file],
            capture_output=True, text=True, env=env
        )
        if result.returncode == 0 and os.path.exists(output_file):
            return jsonify({"success": True, "filename": fname,
                            "download_url": f"/download/{token}/{fname}"})
        return jsonify({"success": False,
                        "error": result.stderr.strip() or "แปลงไฟล์ไม่สำเร็จ"})

    finally:
        os.unlink(tmp_path)


@app.route("/download/<token>/<filename>")
def download_file(token, filename):
    if not re.match(r'^[0-9a-f]{32}$', token):
        return "Not found", 404
    safe = Path(filename).name
    path = os.path.join(tempfile.gettempdir(), f"{token}_{safe}")
    if not os.path.exists(path):
        return "File not found", 404
    return send_file(path, as_attachment=True, download_name=safe)


@app.route("/browse")
def browse():
    path = request.args.get("path", "").strip()
    p = Path(path) if path else Path.home()
    if not p.exists() or not p.is_dir():
        p = Path.home()
    entries = []
    try:
        for item in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if item.is_dir():
                try:
                    item.stat()
                    entries.append({"name": item.name, "path": str(item)})
                except (PermissionError, OSError):
                    pass
    except PermissionError:
        pass
    parent = str(p.parent) if str(p.parent) != str(p) else None
    return jsonify({"path": str(p), "entries": entries, "parent": parent})


@app.route("/browse/roots")
def browse_roots():
    drives = [{"name": f"{l}:\\", "path": f"{l}:\\"}
              for l in string.ascii_uppercase if os.path.exists(f"{l}:\\")]
    return jsonify({"drives": drives})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5100))
    print(f"  MarkItDown UI  ->  http://127.0.0.1:{port}")
    app.run(debug=False, host="0.0.0.0", port=port)
