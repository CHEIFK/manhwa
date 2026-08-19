#!/usr/bin/env python3
"""
Manwa Manager — Production Server, Download Manager & Reader
"""

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, unquote
import collections
import hashlib
import io
import json
import mimetypes
import os
import random
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests
from bs4 import BeautifulSoup
from PIL import Image

# -----------------------------------------------------------------------------
# Global Paths & Constants
# -----------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and (Path(meipass) / "index.html").exists():
        WEB = Path(meipass)
    else:
        WEB = Path(sys.executable).resolve().parent
else:
    WEB = Path(__file__).resolve().parent

CONFIG_DIR = Path.home() / ".config" / "ManwaManager"
CONFIG_PATH = CONFIG_DIR / "config.json"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
MAX_RETRIES = 4
BASE_RETRY_DELAY = 1.2
NETWORK_CONNECT_TIMEOUT = 10
NETWORK_READ_TIMEOUT = 25
TIMEOUT = (NETWORK_CONNECT_TIMEOUT, NETWORK_READ_TIMEOUT)

MAX_ACTIVE_SERIES = 1
ALLOWED_WORKERS = [4, 8, 12, 16, 20, 24]
DEFAULT_WORKERS = 12
ALLOWED_SPEED_LIMITS = [0, 1, 2, 5, 10, 20, 50]  # 0 = Unlimited (MB/s)
DEFAULT_SPEED_LIMIT = 0

BROWSER_PROFILES = [
    "safari17_0",
    "chrome110",
    "chrome116",
    "edge101",
    "safari15_5",
]

# -----------------------------------------------------------------------------
# Configuration Management
# -----------------------------------------------------------------------------

ROOT = None
_CONFIG_LOCK = threading.Lock()


def load_config():
    with _CONFIG_LOCK:
        try:
            if CONFIG_PATH.is_file():
                return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[Config] Error loading config: {e}")
        return {}


def save_config(library_path=None, workers=None, speed_limit=None):
    with _CONFIG_LOCK:
        try:
            current = {}
            if CONFIG_PATH.is_file():
                try:
                    current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                except Exception:
                    current = {}

            if library_path is not None:
                current["library_path"] = str(library_path)
            if workers is not None and workers in ALLOWED_WORKERS:
                current["download_workers"] = int(workers)
            if speed_limit is not None and speed_limit in ALLOWED_SPEED_LIMITS:
                current["download_speed_limit"] = int(speed_limit)

            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
            return current
        except Exception as e:
            print(f"[Config] Error saving config: {e}")
            return {}


def get_current_settings():
    config = load_config()
    workers = config.get("download_workers", DEFAULT_WORKERS)
    if workers not in ALLOWED_WORKERS:
        workers = DEFAULT_WORKERS

    speed_limit = config.get("download_speed_limit", DEFAULT_SPEED_LIMIT)
    if speed_limit not in ALLOWED_SPEED_LIMITS:
        speed_limit = DEFAULT_SPEED_LIMIT

    return {
        "library_path": str(ROOT) if ROOT else "",
        "download_workers": int(workers),
        "download_speed_limit": int(speed_limit),
        "allowed_workers": ALLOWED_WORKERS,
        "allowed_speed_limits": ALLOWED_SPEED_LIMITS,
    }


def choose_folder(initial=None):
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            title="Choose Manwa Library Folder",
            initialdir=str(initial or Path.home()),
            mustexist=False,
        )
        root.destroy()
        return Path(selected).expanduser().resolve() if selected else None
    except Exception:
        return None


def setup_library():
    global ROOT
    config = load_config()
    saved = config.get("library_path")
    if saved:
        candidate = Path(saved).expanduser().resolve()
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            ROOT = candidate
        except Exception:
            ROOT = None

    if ROOT is None:
        workspace_box = Path("/workspaces/manhwa/box")
        if workspace_box.is_dir():
            default = workspace_box.resolve()
        else:
            default = (Path.home() / "Manwa Library").resolve()

        selected = choose_folder(default)
        ROOT = selected or default
        ROOT.mkdir(parents=True, exist_ok=True)
        save_config(library_path=ROOT)

    print(f"[Library] Root directory set to: {ROOT}")
    return ROOT


def change_library_folder():
    global ROOT
    selected = choose_folder(ROOT)
    if not selected:
        return False
    try:
        selected.mkdir(parents=True, exist_ok=True)
        ROOT = selected.resolve()
        save_config(library_path=ROOT)
        LibraryScanner.invalidate()
        return True
    except Exception as e:
        print(f"[Library] Failed to change folder: {e}")
        return False


# -----------------------------------------------------------------------------
# Global Token-Bucket Rate Limiter
# -----------------------------------------------------------------------------

class GlobalRateLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_check = time.monotonic()
        self.tokens = 0.0

    def acquire(self, num_bytes, limit_mbps, cancel_event=None):
        if limit_mbps is None or limit_mbps <= 0:
            return  # Unlimited / Maximum speed

        limit_bytes_per_sec = limit_mbps * 1024 * 1024
        max_burst = limit_bytes_per_sec * 1.5

        while True:
            if cancel_event and cancel_event.is_set():
                return

            with self.lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self.last_check)
                self.last_check = now
                self.tokens = min(max_burst, self.tokens + elapsed * limit_bytes_per_sec)

                if self.tokens >= num_bytes:
                    self.tokens -= num_bytes
                    return

                needed = num_bytes - self.tokens
                sleep_time = min(needed / limit_bytes_per_sec, 0.4)

            time.sleep(sleep_time)


RATE_LIMITER = GlobalRateLimiter()


# -----------------------------------------------------------------------------
# Thread-Safe Isolated HTTP Client with Cloudflare Resilience
# -----------------------------------------------------------------------------

_THREAD_LOCAL = threading.local()


def get_worker_session(impersonate="safari17_0"):
    current = getattr(_THREAD_LOCAL, "session", None)
    current_imp = getattr(_THREAD_LOCAL, "impersonate", None)
    if current is None or current_imp != impersonate:
        if current is not None:
            try:
                current.close()
            except Exception:
                pass
        session = requests.Session(impersonate=impersonate)
        session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        _THREAD_LOCAL.session = session
        _THREAD_LOCAL.impersonate = impersonate
        return session
    return current


def close_worker_session():
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass
        _THREAD_LOCAL.session = None
        _THREAD_LOCAL.impersonate = None


def safe_request(url, headers=None, referer=None, cancel_event=None, max_retries=MAX_RETRIES):
    last_err = None

    for attempt in range(1, max_retries + 1):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Request cancelled")

        profile = BROWSER_PROFILES[(attempt - 1) % len(BROWSER_PROFILES)]
        session = get_worker_session(impersonate=profile)

        req_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if referer:
            req_headers["Referer"] = referer
        if headers:
            req_headers.update(headers)

        try:
            r = session.get(
                url,
                headers=req_headers,
                timeout=TIMEOUT,
            )

            is_cf = (r.status_code == 403 or r.status_code == 503) and ("Just a moment" in r.text or "Cloudflare" in r.text)
            if is_cf:
                close_worker_session()
                delay = BASE_RETRY_DELAY * attempt + random.uniform(0.5, 1.2)
                if attempt < max_retries:
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Cloudflare bot protection challenge for {url}")

            if r.status_code == 200:
                return r

            if r.status_code in (429, 503, 403):
                close_worker_session()
                delay = BASE_RETRY_DELAY * (2 ** (attempt - 1)) + random.uniform(0.5, 1.5)
                if attempt < max_retries:
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"HTTP {r.status_code} (Rate limited or forbidden) from {url}")

            if r.status_code == 404:
                raise RuntimeError(f"HTTP 404 Not Found: {url}")

            delay = BASE_RETRY_DELAY * attempt + random.uniform(0.1, 0.5)
            if attempt < max_retries:
                time.sleep(delay)
                continue
            raise RuntimeError(f"HTTP {r.status_code} returned for {url}")

        except Exception as e:
            last_err = e
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Request cancelled")

            close_worker_session()

            if attempt >= max_retries:
                raise RuntimeError(f"Request failed after {max_retries} attempts ({url}): {e}")

            delay = BASE_RETRY_DELAY * attempt + random.uniform(0.2, 0.8)
            time.sleep(delay)

    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


# -----------------------------------------------------------------------------
# Safe Helpers & Parsing
# -----------------------------------------------------------------------------

def safe_name(name):
    name = re.sub(r"""[<>:"/\\|?*\x00-\x1f]""", "_", str(name or ""))
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name[:180] or "Unknown Series"


def image_files(folder):
    if not folder.is_dir():
        return []
    return sorted(
        [
            p.name for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXT and not p.name.startswith(".")
        ],
        key=lambda x: (len(x), x.lower()),
    )


def series_meta_path(folder):
    return folder / ".series.json"


def load_series_meta(folder):
    path = series_meta_path(folder)
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_series_meta(folder, data):
    try:
        series_meta_path(folder).write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[Meta] Failed to save series meta in {folder}: {e}")


def find_existing_series(series_url):
    if not ROOT or not ROOT.exists():
        return None

    normalized = series_url.rstrip("/").lower()

    for folder in ROOT.iterdir():
        if not folder.is_dir():
            continue
        meta = load_series_meta(folder)
        saved_url = str(meta.get("url", "")).rstrip("/").lower()
        if saved_url and saved_url == normalized:
            return folder

    return None


def detect_series_name(series_url, cancel_event=None):
    r = safe_request(series_url, cancel_event=cancel_event, max_retries=3)
    soup = BeautifulSoup(r.text, "html.parser")

    candidates = [
        soup.select_one(".post-title h1"),
        soup.select_one("h1.entry-title"),
        soup.select_one(".story-info-right h1"),
        soup.select_one(".manga-info-top h1"),
        soup.select_one("h1"),
        soup.select_one("title"),
    ]

    for node in candidates:
        if node:
            text = node.get_text(" ", strip=True)
            text = re.sub(r"\s+", " ", text)
            if text:
                text = re.sub(r"\s*[-|]\s*(Toonily|Manga|Manhwa|Read Manhua|Webtoon).*?$", "", text, flags=re.I)
                return safe_name(text)

    path_part = urlparse(series_url).path.rstrip("/").split("/")[-1]
    return safe_name(path_part or "Unknown Series")


def fetch_series_chapters(series_url, cancel_event=None):
    r = safe_request(series_url, cancel_event=cancel_event, max_retries=3)
    soup = BeautifulSoup(r.text, "html.parser")

    chapter_elements = soup.select("li.wp-manga-chapter")
    if not chapter_elements:
        chapter_elements = soup.select("ul.row-content-chapter li, ul.main-version-list li, .chapter-list li")

    chapters = []

    for el in chapter_elements:
        a = el.find("a")
        if not a or not a.get("href"):
            continue

        title = a.get_text(strip=True)
        url = a["href"].strip()

        m = re.search(r"/chapter-([\d\.]+)/?", url, re.IGNORECASE)
        if m:
            ch_num_str = m.group(1)
            ch_num = float(ch_num_str) if "." in ch_num_str else int(ch_num_str)
        else:
            m2 = re.search(r"Chapter\s+([\d\.]+)", title, re.IGNORECASE)
            if m2:
                ch_num_str = m2.group(1)
                ch_num = float(ch_num_str) if "." in ch_num_str else int(ch_num_str)
            else:
                ch_num_str = title
                ch_num = 999999

        release_date = el.find("span", class_="chapter-release-date")
        date_str = release_date.get_text(strip=True) if release_date else "Unknown"

        chapters.append({
            "num_str": str(ch_num_str),
            "num": ch_num,
            "title": title,
            "url": url,
            "date": date_str,
        })

    chapters.sort(key=lambda c: c["num"])
    return chapters


def fetch_chapter_images(chapter_url, cancel_event=None):
    r = safe_request(chapter_url, cancel_event=cancel_event, max_retries=3)
    soup = BeautifulSoup(r.text, "html.parser")

    rc = soup.find("div", class_="reading-content")
    if not rc:
        rc = soup.find("div", class_="readerarea") or soup.find("div", id="readerarea") or soup.find("div", class_="entry-content")

    if not rc:
        rc = soup.select_one(".page-break") or soup.select_one(".wp-manga-chapter-img")

    container = rc if rc else soup
    img_urls = []
    for img in container.find_all("img"):
        src = (
            img.get("data-src")
            or img.get("src")
            or img.get("data-lazy-src")
            or img.get("data-original")
        )
        if src:
            src = src.strip()
            if src.startswith("http") and not any(skip in src for skip in ["logo", "avatar", "icon", "banner"]):
                img_urls.append(src)

    seen = set()
    unique_imgs = []
    for u in img_urls:
        if u not in seen:
            seen.add(u)
            unique_imgs.append(u)

    if not unique_imgs:
        raise ValueError("No reading content images found on chapter page")

    return unique_imgs


def download_image(img_url, dest_path, referer, cancel_event=None, speed_limit_mbps=0):
    if cancel_event and cancel_event.is_set():
        raise RuntimeError("Download cancelled")

    r = safe_request(img_url, referer=referer, cancel_event=cancel_event, max_retries=MAX_RETRIES)
    content = r.content

    if not content or len(content) == 0:
        raise ValueError(f"Empty response received for {img_url}")

    # Rate limiting if configured
    if speed_limit_mbps and speed_limit_mbps > 0:
        RATE_LIMITER.acquire(len(content), speed_limit_mbps, cancel_event)

    try:
        img = Image.open(io.BytesIO(content))
        img.verify()
    except Exception as img_err:
        raise ValueError(f"Corrupt image received: {img_err}")

    tmp_path = dest_path.with_suffix(dest_path.suffix + f".tmp_{time.time_ns()}")
    try:
        with open(tmp_path, "wb") as f:
            f.write(content)
        tmp_path.replace(dest_path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise

    return len(content), hashlib.md5(content).hexdigest()


# -----------------------------------------------------------------------------
# Fast Caching Library Scanner
# -----------------------------------------------------------------------------

class LibraryScanner:
    _cache = None
    _cache_time = 0
    _lock = threading.Lock()

    @classmethod
    def invalidate(cls):
        with cls._lock:
            cls._cache = None
            cls._cache_time = 0

    @classmethod
    def scan(cls, force=False):
        global ROOT
        with cls._lock:
            now = time.time()
            if not force and cls._cache is not None and (now - cls._cache_time < 3.0):
                return cls._cache

            if not ROOT or not ROOT.exists():
                cls._cache = []
                cls._cache_time = now
                return cls._cache

            result = []
            try:
                for folder in sorted([p for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")], key=lambda p: p.name.lower()):
                    chapters = []
                    total = 0

                    for ch in sorted([p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")], key=lambda p: p.name.lower()):
                        files = image_files(ch)
                        if files:
                            chapters.append({
                                "name": ch.name,
                                "path": ch.name,
                                "pages": len(files),
                                "files": files,
                            })
                            total += len(files)

                    direct_files = image_files(folder)
                    if direct_files and not chapters:
                        chapters.append({
                            "name": "Chapter 1",
                            "path": "",
                            "pages": len(direct_files),
                            "files": direct_files,
                        })
                        total += len(direct_files)

                    result.append({
                        "name": folder.name,
                        "chapters": len(chapters),
                        "pages": total,
                    })

                cls._cache = result
                cls._cache_time = now
            except Exception as e:
                print(f"[Scanner] Error during scan: {e}")
                if cls._cache is None:
                    cls._cache = []

            return cls._cache


def manga(name):
    global ROOT
    folder = (ROOT / name).resolve()

    if not folder.is_relative_to(ROOT) or not folder.is_dir():
        return {"error": "Not found"}

    chapters = []
    for ch in sorted([p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")], key=lambda p: p.name.lower()):
        files = image_files(ch)
        if files:
            chapters.append({
                "name": ch.name,
                "path": ch.name,
                "pages": len(files),
                "files": files,
            })

    direct_files = image_files(folder)
    if direct_files and not chapters:
        chapters.append({
            "name": "Chapter 1",
            "path": "",
            "pages": len(direct_files),
            "files": direct_files,
        })

    return {"name": folder.name, "chapters": chapters}


def delete_folder(name):
    global ROOT
    try:
        folder = (ROOT / name).resolve()
        if not folder.is_relative_to(ROOT) or folder == ROOT:
            return False, "Invalid folder path."
        if not folder.exists() or not folder.is_dir():
            return False, "Folder does not exist."
        shutil.rmtree(folder)
        LibraryScanner.invalidate()
        return True, None
    except Exception as e:
        return False, str(e)


# -----------------------------------------------------------------------------
# Download Job Model & DownloadManager
# -----------------------------------------------------------------------------

class DownloadJob:
    def __init__(self, job_id, url, requested_name=None):
        self.job_id = job_id
        self.url = url.strip()
        self.requested_name = requested_name.strip() if requested_name else None
        self.series_name = self.requested_name or "Pending Detection..."
        self.dest_dir = None

        self.status = "queued"
        self.progress = 0
        self.chapter_progress = 0
        self.current_chapter = ""
        self.completed_chapters = 0
        self.total_chapters = 0
        self.downloaded_bytes = 0
        self.speed_bps = 0
        self.workers = DEFAULT_WORKERS
        self.message = "Queued in download manager..."
        self.error = None

        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.updated_at = self.created_at
        self.cancel_event = threading.Event()

        # Rolling window speed tracking: deque of (monotonic_time, total_bytes)
        self.speed_samples = collections.deque(maxlen=12)
        self.last_byte_time = time.monotonic()

    def record_bytes(self, total_bytes):
        now = time.monotonic()
        self.downloaded_bytes = total_bytes
        self.speed_samples.append((now, total_bytes))
        self.last_byte_time = now

        if len(self.speed_samples) >= 2:
            t0, b0 = self.speed_samples[0]
            t1, b1 = self.speed_samples[-1]
            dt = t1 - t0
            if dt > 0.1:
                self.speed_bps = max(0.0, (b1 - b0) / dt)
            else:
                self.speed_bps = 0.0

    def check_idle_speed(self):
        if time.monotonic() - self.last_byte_time > 1.8:
            self.speed_bps = 0.0

    def update(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")

    def to_dict(self):
        if self.status in ("starting", "detecting", "fetching", "downloading"):
            self.check_idle_speed()

        return {
            "job_id": self.job_id,
            "url": self.url,
            "series_name": self.series_name,
            "status": self.status,
            "progress": self.progress,
            "chapter_progress": self.chapter_progress,
            "current_chapter": self.current_chapter,
            "completed_chapters": self.completed_chapters,
            "total_chapters": self.total_chapters,
            "downloaded_bytes": self.downloaded_bytes,
            "speed_bps": self.speed_bps,
            "workers": self.workers,
            "message": self.message,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DownloadManager:
    def __init__(self):
        self.jobs = collections.OrderedDict()
        self.queue = collections.deque()
        self.lock = threading.Lock()
        self.active_jobs = set()
        self.running = True

        self.dispatcher_thread = threading.Thread(target=self._dispatcher_loop, daemon=True, name="DownloadDispatcher")
        self.dispatcher_thread.start()

    def add_job(self, url, requested_name=None):
        url = url.strip()
        with self.lock:
            norm_url = url.rstrip("/").lower()
            for job in self.jobs.values():
                if job.status in ("queued", "starting", "detecting", "fetching", "downloading"):
                    if job.url.rstrip("/").lower() == norm_url:
                        return job.to_dict(), True

            job_id = hashlib.sha1(f"{url}-{time.time_ns()}".encode()).hexdigest()[:12]
            job = DownloadJob(job_id, url, requested_name)
            self.jobs[job_id] = job
            self.queue.append(job_id)
            self._prune_history_locked()
            return job.to_dict(), False

    def cancel_job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            job.cancel_event.set()
            if job.status == "queued":
                if job_id in self.queue:
                    self.queue.remove(job_id)
                job.update(status="cancelled", message="Cancelled by user.", speed_bps=0)
            else:
                job.update(message="Cancelling download...", speed_bps=0)
            return True

    def retry_job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status not in ("failed", "cancelled"):
                return False
            job.cancel_event = threading.Event()
            job.update(
                status="queued",
                progress=0,
                chapter_progress=0,
                speed_bps=0,
                message="Re-queued for download...",
                error=None,
            )
            self.queue.append(job_id)
            return True

    def clear_job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job.status in ("completed", "failed", "cancelled"):
                del self.jobs[job_id]
                return True
            return False

    def clear_all_finished(self):
        with self.lock:
            finished_ids = [
                jid for jid, j in self.jobs.items()
                if j.status in ("completed", "failed", "cancelled")
            ]
            for jid in finished_ids:
                del self.jobs[jid]
            return len(finished_ids)

    def get_all_jobs(self):
        with self.lock:
            return {jid: j.to_dict() for jid, j in self.jobs.items()}

    def _prune_history_locked(self):
        finished_keys = [
            jid for jid, j in self.jobs.items()
            if j.status in ("completed", "failed", "cancelled")
        ]
        while len(finished_keys) > 30:
            oldest = finished_keys.pop(0)
            del self.jobs[oldest]

    def _dispatcher_loop(self):
        while self.running:
            job_to_run = None
            with self.lock:
                if len(self.active_jobs) < MAX_ACTIVE_SERIES and len(self.queue) > 0:
                    job_id = self.queue.popleft()
                    job = self.jobs.get(job_id)
                    if job and not job.cancel_event.is_set():
                        job_to_run = job
                        self.active_jobs.add(job_id)
                        job.update(status="starting", message="Starting download...")

            if job_to_run:
                worker_thread = threading.Thread(
                    target=self._run_job_wrapper,
                    args=(job_to_run,),
                    daemon=True,
                    name=f"Worker-{job_to_run.job_id}"
                )
                worker_thread.start()
            else:
                time.sleep(0.3)

    def _run_job_wrapper(self, job):
        try:
            self._execute_download(job)
        except Exception as e:
            if job.cancel_event.is_set():
                job.update(status="cancelled", message="Download cancelled.", speed_bps=0)
            else:
                print(f"[Downloader] Error in job {job.job_id}: {e}")
                job.update(status="failed", error=str(e), message=f"Failed: {e}", speed_bps=0)
        finally:
            with self.lock:
                self.active_jobs.discard(job.job_id)
            LibraryScanner.invalidate()

    def _execute_download(self, job):
        global ROOT
        if not ROOT:
            setup_library()

        ROOT.mkdir(parents=True, exist_ok=True)

        if job.cancel_event.is_set():
            job.update(status="cancelled", message="Download cancelled.", speed_bps=0)
            return

        # Read actual configured settings for this job
        settings = get_current_settings()
        workers = settings["download_workers"]
        speed_limit = settings["download_speed_limit"]
        job.workers = workers

        print(f"[Downloader] Job {job.job_id} ({job.series_name}): Starting with {workers} workers, speed limit: {speed_limit or Unlimited} MB/s")

        job.update(status="detecting", message="Detecting series...")

        existing_dir = find_existing_series(job.url)
        resumed = False

        if existing_dir:
            base_output_dir = existing_dir
            series_name = existing_dir.name
            resumed = True
        else:
            detected = safe_name(job.requested_name) if job.requested_name else detect_series_name(job.url, job.cancel_event)
            series_name = detected
            base_output_dir = ROOT / series_name
            base_output_dir.mkdir(parents=True, exist_ok=True)

            meta = load_series_meta(base_output_dir)
            existing_url = str(meta.get("url", "")).rstrip("/").lower()

            if existing_url and existing_url != job.url.rstrip("/").lower():
                suffix = 2
                orig_name = series_name
                while (ROOT / f"{orig_name} ({suffix})").exists():
                    suffix += 1
                series_name = f"{orig_name} ({suffix})"
                base_output_dir = ROOT / series_name
                base_output_dir.mkdir(parents=True, exist_ok=True)

        job.dest_dir = base_output_dir
        job.series_name = series_name
        save_series_meta(base_output_dir, {
            "url": job.url,
            "name": series_name,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

        if job.cancel_event.is_set():
            job.update(status="cancelled", message="Download cancelled.", speed_bps=0)
            return

        job.update(status="fetching", message="Finding chapters...")
        chapters = fetch_series_chapters(job.url, job.cancel_event)

        if not chapters:
            raise RuntimeError("No readable chapters found on the series page.")

        total_chapters = len(chapters)
        job.update(
            status="downloading",
            total_chapters=total_chapters,
            completed_chapters=0,
            message=f"Found {total_chapters} chapters" + (" · resuming" if resumed else ""),
            speed_bps=0,
            downloaded_bytes=0,
        )

        total_images = 0
        total_bytes = 0
        failed_pages = []
        consecutive_chapter_failures = 0

        for chapter_index, ch in enumerate(chapters, start=1):
            if job.cancel_event.is_set():
                job.update(status="cancelled", message="Download cancelled.", speed_bps=0)
                return

            ch_num = ch["num"]
            if isinstance(ch_num, int) or (isinstance(ch_num, float) and ch_num.is_integer()):
                ch_folder_name = f"Chapter-{int(ch_num):02d}"
            else:
                ch_folder_name = f"Chapter-{ch["num_str"]}"

            ch_dir = base_output_dir / ch_folder_name
            ch_dir.mkdir(parents=True, exist_ok=True)

            job.update(
                current_chapter=ch_folder_name,
                message=f"Reading {ch_folder_name}...",
                chapter_progress=0,
            )

            try:
                img_urls = fetch_chapter_images(ch["url"], job.cancel_event)
                consecutive_chapter_failures = 0
                print(f"[Downloader] {job.series_name} - {ch_folder_name}: Found {len(img_urls)} image URLs. Concurrency: {workers}")
            except Exception as e:
                print(f"[Downloader] {job.series_name} - {ch_folder_name} error fetching images: {e}")
                failed_pages.append({"chapter": ch_folder_name, "url": ch["url"], "error": str(e)})
                consecutive_chapter_failures += 1

                if consecutive_chapter_failures >= 5 and total_images == 0:
                    raise RuntimeError("Remote site is blocking chapter images (Cloudflare / 403).")

                overall_prog = int((chapter_index / total_chapters) * 100)
                job.update(
                    progress=overall_prog,
                    completed_chapters=chapter_index,
                    message=f"{ch_folder_name}: Skipped ({e})",
                )
                continue

            if not img_urls:
                continue

            def download_one(item):
                if job.cancel_event.is_set():
                    return item[0], 0, "Cancelled", True

                idx, img_url = item
                url_path = img_url.split("?")[0]
                ext = os.path.splitext(url_path)[1] or ".png"
                ext = ext.lower()
                if ext not in IMAGE_EXT:
                    ext = ".jpg"

                filename = f"{idx:03d}{ext}"
                file_path = ch_dir / filename

                if file_path.exists() and file_path.stat().st_size > 0:
                    try:
                        data = file_path.read_bytes()
                        img = Image.open(io.BytesIO(data))
                        img.verify()
                        return idx, len(data), None, True
                    except Exception:
                        pass

                try:
                    size, _ = download_image(img_url, file_path, ch["url"], job.cancel_event, speed_limit_mbps=speed_limit)
                    return idx, size, None, False
                except Exception as err:
                    return idx, 0, str(err), False

            completed_in_chapter = 0

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(download_one, (idx, img_url))
                    for idx, img_url in enumerate(img_urls, start=1)
                ]

                for future in as_completed(futures):
                    if job.cancel_event.is_set():
                        executor.shutdown(wait=False, cancel_futures=True)
                        job.update(status="cancelled", message="Download cancelled.", speed_bps=0)
                        return

                    try:
                        idx, size, error, skipped = future.result()
                    except Exception as e:
                        idx, size, error, skipped = 0, 0, str(e), False

                    completed_in_chapter += 1
                    if error and error != "Cancelled":
                        failed_pages.append({
                            "chapter": ch_folder_name,
                            "index": idx,
                            "error": error,
                        })
                    else:
                        total_images += 1
                        total_bytes += size

                    job.record_bytes(total_bytes)

                    chapter_prog = int(completed_in_chapter / len(img_urls) * 100)
                    overall_prog = int(((chapter_index - 1) + completed_in_chapter / len(img_urls)) / total_chapters * 100)

                    job.update(
                        progress=min(100, overall_prog),
                        chapter_progress=min(100, chapter_prog),
                        message=f"{ch_folder_name}: {completed_in_chapter}/{len(img_urls)}",
                    )

            job.update(completed_chapters=chapter_index)

        report = {
            "series_url": job.url,
            "series_name": series_name,
            "chapters": total_chapters,
            "images": total_images,
            "bytes": total_bytes,
            "failed_count": len(failed_pages),
            "failed_pages": failed_pages,
            "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        report_file = base_output_dir / "download_report.json"
        try:
            report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[Report] Failed to write report: {e}")

        job.update(
            status="completed",
            progress=100,
            chapter_progress=100,
            completed_chapters=total_chapters,
            speed_bps=0,
            message="Download complete",
        )


DOWNLOAD_MANAGER = DownloadManager()


# -----------------------------------------------------------------------------
# HTTP Request Handler
# -----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if "200 -" not in (format % args):
            sys.stderr.write(f"[Server] {format % args}\n")

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        global ROOT
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        try:
            if path == "/api/library":
                return self.send_json(LibraryScanner.scan())

            if path.startswith("/api/manga/"):
                name = path[len("/api/manga/"):]
                return self.send_json(manga(name))

            if path == "/api/downloads":
                return self.send_json(DOWNLOAD_MANAGER.get_all_jobs())

            if path == "/api/settings":
                return self.send_json(get_current_settings())

            if path.startswith("/file/"):
                rel = path[len("/file/"):].split("/")
                if len(rel) >= 2 and ROOT:
                    target = ROOT.joinpath(*rel).resolve()
                    if target.is_relative_to(ROOT) and target.is_file():
                        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                        data = target.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", mime)
                        self.send_header("Cache-Control", "public, max-age=86400")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                return self.send_error(404, "File not found")

            if path in {"/", "/index.html"}:
                target = WEB / "index.html"
            else:
                target = (WEB / path.lstrip("/")).resolve()

            if target.is_file() and target.is_relative_to(WEB):
                mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_error(404, "Resource not found")

        except Exception as e:
            self.send_error(500, str(e))

    def do_POST(self):
        global ROOT
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        try:
            if path == "/api/settings":
                payload = self.read_json()
                workers = payload.get("download_workers")
                speed_limit = payload.get("download_speed_limit")

                if workers is not None and workers not in ALLOWED_WORKERS:
                    return self.send_json({"error": f"Invalid workers count. Must be one of {ALLOWED_WORKERS}."}, 400)

                if speed_limit is not None and speed_limit not in ALLOWED_SPEED_LIMITS:
                    return self.send_json({"error": f"Invalid speed limit. Must be one of {ALLOWED_SPEED_LIMITS}."}, 400)

                save_config(workers=workers, speed_limit=speed_limit)
                return self.send_json({"ok": True, "settings": get_current_settings()})

            if path == "/api/library-folder":
                ok = change_library_folder()
                return self.send_json({"ok": ok, "library_path": str(ROOT)})

            if path == "/api/download":
                payload = self.read_json()
                url = str(payload.get("url", "")).strip()
                name = str(payload.get("name", "")).strip()

                if not url.startswith(("http://", "https://")):
                    return self.send_json({"error": "Please enter a valid HTTP/HTTPS URL."}, 400)

                job_dict, is_existing = DOWNLOAD_MANAGER.add_job(url, name or None)
                return self.send_json({
                    "ok": True,
                    "job": job_dict,
                    "is_existing": is_existing,
                })

            if path == "/api/download/cancel":
                payload = self.read_json()
                job_id = str(payload.get("job_id", "")).strip()
                ok = DOWNLOAD_MANAGER.cancel_job(job_id)
                return self.send_json({"ok": ok})

            if path == "/api/download/retry":
                payload = self.read_json()
                job_id = str(payload.get("job_id", "")).strip()
                ok = DOWNLOAD_MANAGER.retry_job(job_id)
                return self.send_json({"ok": ok})

            if path == "/api/download/clear":
                payload = self.read_json()
                job_id = payload.get("job_id")
                if job_id:
                    ok = DOWNLOAD_MANAGER.clear_job(str(job_id))
                else:
                    ok = DOWNLOAD_MANAGER.clear_all_finished()
                return self.send_json({"ok": bool(ok)})

            if path == "/api/delete-folder":
                payload = self.read_json()
                name = str(payload.get("name", "")).strip()
                if not name:
                    return self.send_json({"error": "Folder name required"}, 400)
                ok, err = delete_folder(name)
                if not ok:
                    return self.send_json({"error": err or "Failed to delete folder"}, 400)
                return self.send_json({"ok": True, "name": name})

            return self.send_error(404, "Endpoint not found")

        except Exception as e:
            return self.send_json({"error": str(e)}, 500)


# -----------------------------------------------------------------------------
# Main Application Entrypoint
# -----------------------------------------------------------------------------

def start_server(port=8765):
    setup_library()

    print("📚 Manwa Manager")
    print(f"Library: {ROOT}")
    print(f"Browser: http://127.0.0.1:{port}")
    print(f"Codespaces: port 8765")
    print("Press Ctrl+C to stop.")

    def open_browser():
        try:
            import webbrowser
            time.sleep(0.6)
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            pass

    threading.Thread(target=open_browser, daemon=True, name="BrowserLauncher").start()

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down gracefully...")
    finally:
        server.server_close()


if __name__ == "__main__":
    start_server()
