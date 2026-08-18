#!/usr/bin/env python3
"""
Generate box/library.json for the Manwa storage repository.
Scans all manga folders and chapter folders inside box/, counts image files,
and produces a formatted library.json compatible with the Manwa reader.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Supported image extensions (case-insensitive)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}

# Files or folders to explicitly ignore inside box/
IGNORED_NAMES = {"library.json", "readme.md", ".git", ".github", "scripts"}


def natural_sort_key(s: str):
    """
    Sort alphanumeric strings naturally/numerically.
    E.g. Chapter-00, Chapter-01, Chapter-02 ... Chapter-10
    """
    parts = re.split(r"(\d+)", str(s))
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def load_existing_metadata(library_file: Path):
    """
    Load existing titles from library.json if present to preserve custom display names.
    Returns (manga_titles_dict, chapter_titles_dict).
    """
    manga_titles = {}
    chapter_titles = {}
    if library_file.is_file():
        try:
            with open(library_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            manga_list = data if isinstance(data, list) else data.get("manga", [])
            for m in manga_list:
                m_folder = m.get("folder")
                m_name = m.get("name")
                if m_folder and m_name:
                    manga_titles[m_folder] = m_name
                for c in m.get("chapters", []):
                    c_folder = c.get("folder")
                    c_name = c.get("name")
                    if m_folder and c_folder and c_name:
                        chapter_titles[(m_folder, c_folder)] = c_name
        except Exception as e:
            print(f"Notice: Could not parse existing {library_file} ({e}), will default to folder names.", file=sys.stderr)
    return manga_titles, chapter_titles


def count_images_in_dir(chapter_dir: Path) -> int:
    """
    Count image files directly inside chapter_dir without inspecting content.
    """
    count = 0
    try:
        for entry in os.scandir(chapter_dir):
            if entry.is_file() and not entry.name.startswith("."):
                ext = Path(entry.name).suffix.lower()
                if ext in IMAGE_EXTENSIONS:
                    count += 1
    except OSError as err:
        print(f"Warning: Could not read directory {chapter_dir}: {err}", file=sys.stderr)
    return count


def get_local_metadata(directory: Path) -> dict:
    """
    Check for optional metadata file in the folder (e.g. metadata.json, manga.json, chapter.json, info.json).
    """
    for filename in ("metadata.json", "manga.json", "chapter.json", "info.json"):
        meta_path = directory / filename
        if meta_path.is_file():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass
    return {}


def scan_manga_library(box_dir: Path, output_file: Path) -> dict:
    """
    Scan box directory and build the library dictionary.
    """
    if not box_dir.is_dir():
        print(f"Error: box directory not found at {box_dir}", file=sys.stderr)
        return {"manga": []}

    existing_manga_titles, existing_chapter_titles = load_existing_metadata(output_file)

    manga_entries = []

    # Find all direct child directories inside box/
    manga_dirs = [
        d for d in box_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name.lower() not in IGNORED_NAMES
    ]
    manga_dirs.sort(key=lambda d: natural_sort_key(d.name))

    for manga_dir in manga_dirs:
        manga_folder = manga_dir.name
        local_manga_meta = get_local_metadata(manga_dir)
        manga_name = (
            local_manga_meta.get("name")
            or existing_manga_titles.get(manga_folder)
            or manga_folder
        )

        chapter_dirs = [
            d for d in manga_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ]
        chapter_dirs.sort(key=lambda d: natural_sort_key(d.name))

        chapters = []
        for chapter_dir in chapter_dirs:
            chapter_folder = chapter_dir.name
            local_chapter_meta = get_local_metadata(chapter_dir)
            chapter_name = (
                local_chapter_meta.get("name")
                or existing_chapter_titles.get((manga_folder, chapter_folder))
                or chapter_folder
            )

            page_count = count_images_in_dir(chapter_dir)

            chapters.append({
                "name": chapter_name,
                "folder": chapter_folder,
                "pages": page_count
            })

        manga_entries.append({
            "name": manga_name,
            "folder": manga_folder,
            "chapters": chapters
        })

    return {"manga": manga_entries}


def main():
    repo_root = Path(__file__).resolve().parent.parent
    default_box = repo_root / "box"
    default_output = default_box / "library.json"

    parser = argparse.ArgumentParser(description="Generate box/library.json for Manwa storage.")
    parser.add_argument("--box-dir", type=Path, default=default_box, help="Path to box directory")
    parser.add_argument("--output", type=Path, default=default_output, help="Path to output library.json")

    args = parser.parse_args()

    box_dir = args.box_dir.resolve()
    output_file = args.output.resolve()

    print(f"Scanning box directory: {box_dir}")
    library_data = scan_manga_library(box_dir, output_file)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    json_content = json.dumps(library_data, indent=2, ensure_ascii=False) + "\n"

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(json_content)

    total_manga = len(library_data["manga"])
    total_chapters = sum(len(m["chapters"]) for m in library_data["manga"])
    total_pages = sum(sum(c["pages"] for c in m["chapters"]) for m in library_data["manga"])

    print(f"Successfully generated {output_file}")
    print(f"Summary: {total_manga} manga, {total_chapters} chapters, {total_pages} total pages")


if __name__ == "__main__":
    main()
