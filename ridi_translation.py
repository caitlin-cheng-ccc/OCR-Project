import os
import time
import hashlib
import threading
import tkinter as tk #GUI
from dataclasses import dataclass
from typing import Optional, Dict

import mss
from PIL import Image, ImageOps
import pytesseract
import deepl

#OCR
TESS_LANG = "kor"
OCR_INTERVAL_SEC = 0.5
MIN_TEXT_CHARS = 10
DEEPL_TARGET = "EN-US"
DEEPL_SOURCE = "KO"

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

def preprocess(pil_image: Image.Image) -> Image.Image:
    img = pil_image.convert("L")
    img = img.resize((img.width * 2, img.height * 2), Image.Resampling.BICUBIC)
    img = ImageOps.autocontrast(img)
    return img

def cheap_region_hash(pil_img: Image.Image) -> str:
    w, h = pil_img.size
    thumb = pil_img.resize((max(40, w // 10), max(40, h //10)), Image.Resampling.BICUBIC)
    return hashlib.sha256(thumb.tobytes()).hexdigest()

def normalize_ocr_text(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)

@dataclass
class CaptureRegion:
    left: int
    top: int
    width: int
    height: int
    
class RegionSelector:
    def __init__(self, parent: tk.Tk):
        self.parent = parent
        self.result: Optional[CaptureRegion] = None
        
        self.win = tk.Toplevel(parent)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        
        #fullscreen
        self.win.geometry(f"{self.win.winfo_screenwidth()}x{self.win.winfo_screenheight()}+0+0")
        
        #slight dark overlay
        self.win.attributes("-alpha", 0.25)
        self.win.configure(bg="black")
        
        self.canvas = tk.Canvas(self.win, cursor="cross", bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        self.start_x = 0
        self.start_y = 0
        self.rect_id = None
        
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        
        #Escape cancel
        self.win.bind("<Escape>", self.cancel)
        
    def cancel(self, event=None):
        self.result = None
        self.win.destroy()
        
    def on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x, self.start_y, event.x, event.y,
            outline="red", width=3
        )
        
    def on_drag(self, event):
        if not self.rect_id:
            return
        self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)
        
    def on_release(self, event):
        x1, y1 = self.start_x, self.start_y
        x2, y2 = event.x, event.y
        left = min(x1, x2)
        top = min(y1, y2)
        right = max(x1, x2)
        bottom = max(y1, y2)
        
        width = max(1, right - left)
        height = max(1, bottom - top)
        
        if width < 40 or height < 40:
            self.result = None
        else:
            self.result = CaptureRegion(left=left, top=top, width=width, height=height)
            
        self.win.destroy()
    
    def select(self) -> Optional[CaptureRegion]:
        self.win.deiconify()
        self.win.focus_force()
        self.win.grab_set()
        self.parent.wait_window(self.win)
        return self.result
    
class RidiTranslatorApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("RIDIBOOKS KO->EN (OCR + DeepL)")
        
        #always on top overlay
        self.root.attributes("-topmost", True)
        
        key = os.environ.get("DEEPL_AUTH_KEY")
        if not key:
            raise RuntimeError("Missing DEEPL_AUTH_KEY environment variable.")
        
        self.translator = deepl.Translator(key)
        
        self.region: Optional[CaptureRegion] = None
        self.running = False
        self.worker: Optional[threading.Thread] = None
        
        self.last_hash: Optional[str] = None
        self.last_ocr: str = ""
        self.cache: Dict[str, str] = {}
        
        #UI
        topbar = tk.Frame(self.root)
        topbar.pack(fill="x")
        
        self.btn_select = tk.Button(topbar, text="Select Region", command=self.select_region)
        self.btn_select.pack(side="left", padx=6, pady=6)
        
        self.btn_start = tk.Button(topbar, text="Start", command=self.start)
        self.btn_start.pack(side="left", padx=6, pady=6)
        
        self.btn_stop = tk.Button(topbar, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6, pady=6)
        
        self.status = tk.StringVar(value="Select a region to begin.")
        tk.Label(self.root, textvariable=self.status, anchor="w").pack(fill="x", padx=6)
        
        self.text = tk.Text(self.root, wrap="word", font=("Segoe UI", 12))
        self.text.pack(fill="both", expand=True, padx=6, pady=6)
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
    def on_close(self):
        self.stop()
        self.root.destroy()
        
    def set_translation(self, s: str):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", s)
        
    def select_region(self):
        self.status.set("Drag to select the reading area. Press ESC to cancel.")
        selector = RegionSelector(self.root)
        region = selector.select()
        if not region:
            self.status.set("region selection cancelled.")
            return
        self.region = region
        self.last_hash = None
        self.lact_ocr = ""
        self.cache.clear()
        self.status.set(f"Region set: left={region.left}, top={region.top}, "
                        f"w={region.width}, h={region.height}")
        
    def start(self):
        if not self.region:
            self.status.set("Please Select Region first.")
            return
        if self.running:
            return
        self.running = True
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.status.set("Running... scroll the page to update translations")
        
        self.worker = threading.Thread(target=self.loop, daemon=True)
        self.worker.start()
        
    def stop(self):
        if not self.running:
            return
        self.running = False
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.status.set("Stopped.")
        
    def loop(self):
        assert self.region is not None
        region_dict = {
            "left": self.region.left,
            "top": self.region.top,
            "width": self.region.width,
            "height": self.region.height,
        }
        
        with mss.mss() as sct:
            while self.running:
                raw = sct.grab(region_dict)
                img = Image.frombytes("RGB", raw.size, raw.rgb)
                
                h = cheap_region_hash(img)
                if h == self.last_hash:
                    time.sleep(OCR_INTERVAL_SEC)
                    continue
                self.last_hash = h
                
                pre = preprocess(img)
                ocr_raw = pytesseract.image_to_string(pre, lang=TESS_LANG, config="--psm 6")
                ocr_text = normalize_ocr_text(ocr_raw)
                
                if len(ocr_text) < MIN_TEXT_CHARS:
                    self.root.after(0, self.status.set, "OCR: not enough tetx (or noise).")
                    time.sleep(OCR_INTERVAL_SEC)
                    continue
                
                if ocr_text == self.last_ocr:
                    self.root.after(0, self.status.set, "OCR: changed pixels, same text.")
                    time.sleep(OCR_INTERVAL_SEC)
                    continue
                self.last_ocr = ocr_text
                
                if ocr_text in self.cache:
                    translated = self.cache[ocr_text]
                    self.root.after(0, self.status.set, "Translated (cached).")
                else:
                    try:
                        result = self.translator.translate_text(
                            ocr_text,
                            source_lang=DEEPL_SOURCE,
                            target_lang=DEEPL_TARGET
                        )
                        translated = result.text
                        self.cache[ocr_text] = translated
                        self.root.after(0, self.status.set, "Translated via DeepL.")
                    except Exception as e:
                        translated = f"[DeepL error]\n{e}\n\n--- OCR ---\n{ocr_text}"
                        self.root.after(0, self.status.set, "DeepL error (see output).")
                        
                self.root.after(0, self.set_translation, translated)
                time.sleep(OCR_INTERVAL_SEC)
    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    RidiTranslatorApp().run()