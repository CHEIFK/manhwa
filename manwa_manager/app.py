#!/usr/bin/env python3
"""
Manwa Manager
Single local server + downloader + reader.

Run:
    python3 app.py

Then open:
    http://127.0.0.1:8765
"""

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, unquote
import hashlib
import io
import json
import mimetypes
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests
from bs4 import BeautifulSoup
from PIL import Image


WEB = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".config" / "ManwaManager"
CONFIG_PATH = CONFIG_DIR / "config.json"
ROOT = None
REPORT_PATH = None

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
MAX_RETRIES = 5
RETRY_DELAY = 2.0
TIMEOUT = 30

DOWNLOADS = {}
DOWNLOAD_LOCK = threading.Lock()


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(library_path):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps({"library_path": str(library_path)}, indent=2),
        encoding="utf-8",
    )


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
    except Exception as e:
        print(f"Folder picker unavailable: {e}")
        return None


def setup_library():
    global ROOT, REPORT_PATH
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
        # Convenient default, but the user can choose anything on first launch.
        default = Path.home() / "Manwa Library"
        selected = choose_folder(default)
        ROOT = selected or default.resolve()
        ROOT.mkdir(parents=True, exist_ok=True)
        save_config(ROOT)

    REPORT_PATH = ROOT / "download_report.txt"


def change_library_folder():
    global ROOT, REPORT_PATH
    selected = choose_folder(ROOT)
    if not selected:
        return False
    selected.mkdir(parents=True, exist_ok=True)
    ROOT = selected.resolve()
    REPORT_PATH = ROOT / "download_report.txt"
    save_config(ROOT)
    return True


def get_session():
    return requests.Session()


def image_files(folder):
    return sorted(
        [
            p.name for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXT
        ],
        key=lambda x: (len(x), x.lower()),
    )


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name[:180] or "Unknown Series"


def series_meta_path(folder):
    return folder / ".series.json"


def load_series_meta(folder):
    path = series_meta_path(folder)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_series_meta(folder, data):
    series_meta_path(folder).write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def find_existing_series(series_url):
    """Return the folder belonging to this exact series URL, if it exists."""
    if not ROOT.exists():
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


def library():
    result = []
    ROOT.mkdir(parents=True, exist_ok=True)

    for folder in sorted(
        [p for p in ROOT.iterdir() if p.is_dir()],
        key=lambda p: p.name.lower(),
    ):
        chapters = []
        total = 0

        for ch in sorted(
            [p for p in folder.iterdir() if p.is_dir()],
            key=lambda p: p.name.lower(),
        ):
            files = image_files(ch)
            if files:
                chapters.append({
                    "name": ch.name,
                    "path": ch.name,
                    "pages": len(files),
                    "files": files,
                })
                total += len(files)

        result.append({
            "name": folder.name,
            "chapters": len(chapters),
            "pages": total,
        })

    return result


def manga(name):
    folder = (ROOT / name).resolve()

    if not folder.is_relative_to(ROOT) or not folder.is_dir():
        return {"error": "Not found"}

    chapters = []

    for ch in sorted(
        [p for p in folder.iterdir() if p.is_dir()],
        key=lambda p: p.name.lower(),
    ):
        files = image_files(ch)
        if files:
            chapters.append({
                "name": ch.name,
                "path": ch.name,
                "pages": len(files),
                "files": files,
            })

    return {"name": folder.name, "chapters": chapters}


def update_download(job_id, **values):
    with DOWNLOAD_LOCK:
        DOWNLOADS.setdefault(job_id, {}).update(values)


def fetch_series_chapters(session, series_url):
    r = session.get(series_url, impersonate="chrome120", timeout=TIMEOUT)

    if r.status_code != 200:
        raise RuntimeError(f"Failed to fetch series page: HTTP {r.status_code}")

    soup = BeautifulSoup(r.text, "html.parser")
    chapter_elements = soup.select("li.wp-manga-chapter")

    chapters = []

    for el in chapter_elements:
        a = el.find("a")
        if not a or not a.get("href"):
            continue

        title = a.get_text(strip=True)
        url = a["href"].strip()

        m = re.search(r"/chapter-([\d\.]+)/?", url)
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
            "num_str": ch_num_str,
            "num": ch_num,
            "title": title,
            "url": url,
            "date": date_str,
        })

    chapters.sort(key=lambda c: c["num"])
    return chapters


def detect_series_name(session, series_url):
    r = session.get(series_url, impersonate="chrome120", timeout=TIMEOUT)

    if r.status_code != 200:
        raise RuntimeError(f"Failed to fetch series page: HTTP {r.status_code}")

    soup = BeautifulSoup(r.text, "html.parser")

    candidates = [
        soup.select_one(".post-title h1"),
        soup.select_one("h1.entry-title"),
        soup.select_one("h1"),
        soup.select_one("title"),
    ]

    for node in candidates:
        if node:
            text = node.get_text(" ", strip=True)
            text = re.sub(r"\s+", " ", text)
            if text:
                text = re.sub(r"\s*[-|]\s*(Toonily|Manga|Manhwa).*?$", "", text, flags=re.I)
                return safe_name(text)

    return safe_name(urlparse(series_url).path.rstrip("/").split("/")[-1])


def fetch_chapter_images(session, chapter_url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(
                chapter_url,
                impersonate="chrome120",
                timeout=TIMEOUT,
            )

            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                rc = soup.find("div", class_="reading-content")

                if not rc:
                    raise ValueError("No reading-content div found on chapter page")

                img_urls = []

                for img in rc.find_all("img"):
                    src = (
                        img.get("data-src")
                        or img.get("src")
                        or img.get("data-lazy-src")
                    )

                    if src:
                        src = src.strip()
                        if src.startswith("http"):
                            img_urls.append(src)

                return img_urls

            print(f"  Attempt {attempt}: HTTP {r.status_code}")

        except Exception as e:
            print(f"  Attempt {attempt} failed: {e}")

        time.sleep(RETRY_DELAY * attempt)

    raise RuntimeError(
        f"Failed to fetch chapter images after {MAX_RETRIES} attempts"
    )


def download_image(session, img_url, dest_path, referer):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            headers = {"Referer": referer}

            r = session.get(
                img_url,
                headers=headers,
                impersonate="chrome120",
                timeout=TIMEOUT,
            )

            if r.status_code == 200 and len(r.content) > 0:
                content = r.content

                try:
                    img = Image.open(io.BytesIO(content))
                    img.verify()
                except Exception as img_err:
                    raise ValueError(
                        f"Corrupt image data received: {img_err}"
                    )

                tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

                with open(tmp_path, "wb") as f:
                    f.write(content)

                tmp_path.replace(dest_path)

                return len(content), hashlib.md5(content).hexdigest()

            print(
                f"    Attempt {attempt}: HTTP {r.status_code}, "
                f"length={len(r.content)}"
            )

        except Exception as e:
            print(f"    Attempt {attempt} failed: {e}")

        time.sleep(RETRY_DELAY * attempt)

    raise RuntimeError(
        f"Failed to download image after {MAX_RETRIES} attempts: {img_url}"
    )


def run_downloader(job_id, series_url, requested_name=None):
    try:
        session = get_session()
        ROOT.mkdir(parents=True, exist_ok=True)

        update_download(
            job_id,
            status="detecting",
            message="Detecting series...",
            progress=0,
        )

        existing_dir = find_existing_series(series_url)

        if existing_dir:
            # Same URL = same series folder. Resume it; never create a copy.
            base_output_dir = existing_dir
            series_name = existing_dir.name
            resumed = True
        else:
            series_name = safe_name(requested_name) if requested_name else detect_series_name(
                session, series_url
            )

            base_output_dir = ROOT / series_name
            base_output_dir.mkdir(parents=True, exist_ok=True)

            # Same display name but different URL = keep completely separate.
            meta = load_series_meta(base_output_dir)
            existing_url = str(meta.get("url", "")).rstrip("/").lower()

            if existing_url and existing_url != series_url.rstrip("/").lower():
                suffix = 2
                original_name = series_name

                while (ROOT / f"{original_name} ({suffix})").exists():
                    suffix += 1

                series_name = f"{original_name} ({suffix})"
                base_output_dir = ROOT / series_name
                base_output_dir.mkdir(parents=True, exist_ok=True)

            resumed = False

        save_series_meta(
            base_output_dir,
            {
                "url": series_url,
                "name": series_name,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

        update_download(
            job_id,
            status="fetching",
            series=series_name,
            message="Finding chapters...",
        )

        chapters = fetch_series_chapters(session, series_url)

        if not chapters:
            raise RuntimeError("No chapters found on the series page.")

        total_chapters = len(chapters)

        update_download(
            job_id,
            status="downloading",
            series=series_name,
            chapters=total_chapters,
            completed_chapters=0,
            message=(
                f"Found {total_chapters} chapters"
                + (" · resuming existing series" if resumed else "")
            ),
            speed_bps=0,
            downloaded_bytes=0,
            workers=12,
        )

        total_images = 0
        total_bytes = 0
        failed = []
        speed_started = time.monotonic()
        speed_last_bytes = 0
        speed_last_time = speed_started

        for chapter_index, ch in enumerate(chapters, start=1):
            ch_num = ch["num"]

            if isinstance(ch_num, int) or (
                isinstance(ch_num, float) and ch_num.is_integer()
            ):
                ch_folder_name = f"Chapter-{int(ch_num):02d}"
            else:
                ch_folder_name = f"Chapter-{ch['num_str']}"

            ch_dir = base_output_dir / ch_folder_name
            ch_dir.mkdir(parents=True, exist_ok=True)

            update_download(
                job_id,
                current_chapter=ch_folder_name,
                message=f"Reading {ch_folder_name}...",
                chapter_progress=0,
            )

            try:
                img_urls = fetch_chapter_images(session, ch["url"])
            except Exception as e:
                failed.append({"url": ch["url"], "error": str(e)})
                continue

            # Download multiple pages at the same time.
            # 12 workers is a good balance between speed and server load.
            WORKERS = 12

            def download_one(item):
                idx, img_url = item

                url_path = img_url.split("?")[0]
                ext = os.path.splitext(url_path)[1] or ".png"
                ext = ext.lower()

                if ext not in IMAGE_EXT:
                    ext = ".jpg"

                filename = f"{idx:03d}{ext}"
                file_path = ch_dir / filename

                # Reuse already-valid pages.
                if file_path.exists() and file_path.stat().st_size > 0:
                    try:
                        data = file_path.read_bytes()
                        img = Image.open(io.BytesIO(data))
                        img.verify()
                        return idx, len(data), None, True
                    except Exception:
                        pass

                try:
                    size, _ = download_image(
                        session,
                        img_url,
                        file_path,
                        ch["url"],
                    )
                    return idx, size, None, False
                except Exception as e:
                    return idx, 0, str(e), False

            completed_in_chapter = 0

            with ThreadPoolExecutor(max_workers=WORKERS) as executor:
                futures = [
                    executor.submit(download_one, (idx, img_url))
                    for idx, img_url in enumerate(img_urls, start=1)
                ]

                for future in as_completed(futures):
                    idx, size, error, skipped = future.result()

                    completed_in_chapter += 1

                    if error:
                        failed.append({
                            "url": img_urls[idx - 1],
                            "file": str(ch_dir / f"{idx:03d}"),
                            "error": error,
                        })
                    else:
                        total_images += 1
                        total_bytes += size

                    chapter_progress = int(
                        completed_in_chapter / len(img_urls) * 100
                    )

                    overall_progress = int(
                        ((chapter_index - 1) + completed_in_chapter / len(img_urls))
                        / total_chapters
                        * 100
                    )

                    now = time.monotonic()
                    elapsed = max(now - speed_last_time, 0.001)
                    delta_bytes = total_bytes - speed_last_bytes
                    current_speed = delta_bytes / elapsed

                    # Smoothly refresh speed about every completion.
                    speed_last_bytes = total_bytes
                    speed_last_time = now

                    update_download(
                        job_id,
                        progress=overall_progress,
                        chapter_progress=chapter_progress,
                        speed_bps=current_speed,
                        downloaded_bytes=total_bytes,
                        workers=WORKERS,
                        message=(
                            f"{ch_folder_name}: "
                            f"{completed_in_chapter}/{len(img_urls)}"
                        ),
                    )

            update_download(
                job_id,
                completed_chapters=chapter_index,
            )

        report = {
            "series_url": series_url,
            "series_name": series_name,
            "chapters": total_chapters,
            "images": total_images,
            "bytes": total_bytes,
            "failed": failed,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        report_file = base_output_dir / "download_report.json"
        report_file.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(
                f"Series: {series_name}\n"
                f"URL: {series_url}\n"
                f"Chapters: {total_chapters}\n"
                f"Images: {total_images}\n"
                f"Size: {total_bytes / (1024 * 1024):.2f} MB\n"
                f"Failed: {len(failed)}\n"
            )

        update_download(
            job_id,
            status="complete",
            progress=100,
            message="Download complete",
            series=series_name,
            completed_chapters=total_chapters,
        )

    except Exception as e:
        update_download(
            job_id,
            status="error",
            message=str(e),
        )


class Handler(BaseHTTPRequestHandler):
    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode()

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        try:
            if path == "/api/library":
                return self.send_json(library())

            if path.startswith("/api/manga/"):
                name = path[len("/api/manga/"):]
                return self.send_json(manga(name))

            if path == "/api/downloads":
                with DOWNLOAD_LOCK:
                    return self.send_json(DOWNLOADS)

            if path == "/api/settings":
                return self.send_json({"library_path": str(ROOT)})

            if path.startswith("/file/"):
                rel = path[len("/file/"):].split("/")

                if len(rel) >= 3:
                    target = ROOT.joinpath(*rel).resolve()

                    if target.is_relative_to(ROOT) and target.is_file():
                        data = target.read_bytes()

                        self.send_response(200)
                        self.send_header(
                            "Content-Type",
                            mimetypes.guess_type(target.name)[0]
                            or "application/octet-stream",
                        )
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header(
                            "Content-Length",
                            str(len(data)),
                        )
                        self.end_headers()
                        self.wfile.write(data)
                        return

                return self.send_error(404)

            if path in {"/", "/index.html"}:
                target = WEB / "index.html"
            else:
                target = WEB / path.lstrip("/")

            if target.is_file():
                data = target.read_bytes()

                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    mimetypes.guess_type(str(target))
                    [0]
                    or "application/octet-stream",
                )
                self.send_header(
                    "Content-Length",
                    str(len(data)),
                )
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_error(404)

        except Exception as e:
            self.send_error(500, str(e))

    def do_POST(self):
        path = unquote(urlparse(self.path).path)

        try:
            if path == "/api/library-folder":
                ok = change_library_folder()
                return self.send_json({"ok": ok, "library_path": str(ROOT)})

            if path == "/api/download":
                payload = self.read_json()
                url = str(payload.get("url", "")).strip()
                name = str(payload.get("name", "")).strip()

                if not url.startswith(("http://", "https://")):
                    return self.send_json(
                        {"error": "Please enter a valid series URL."},
                        400,
                    )

                job_id = hashlib.sha1(
                    f"{url}-{time.time_ns()}".encode()
                ).hexdigest()[:12]

                with DOWNLOAD_LOCK:
                    DOWNLOADS[job_id] = {
                        "status": "queued",
                        "progress": 0,
                        "message": "Queued...",
                        "url": url,
                    }

                thread = threading.Thread(
                    target=run_downloader,
                    args=(job_id, url, name or None),
                    daemon=True,
                )
                thread.start()

                return self.send_json({
                    "ok": True,
                    "job_id": job_id,
                })

            return self.send_error(404)

        except Exception as e:
            return self.send_json({"error": str(e)}, 500)


if __name__ == "__main__":
    setup_library()

    print("📚 Manwa Manager")
    print(f"Library: {ROOT}")
    print("Browser: http://127.0.0.1:8765")
    print("Codespaces: port 8765")
    print("Press Ctrl+C to stop.")

    try:
        import webbrowser
        webbrowser.open("http://127.0.0.1:8765")
    except Exception:
        pass

    ThreadingHTTPServer(
        ("0.0.0.0", 8765),
        Handler,
    ).serve_forever()
