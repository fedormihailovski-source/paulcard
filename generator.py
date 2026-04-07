"""Content generation logic — no Streamlit dependency."""

import json
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types
from PIL import Image

from config import (
    DEFAULT_MODEL,
    DEFAULT_PROMPT_INSTRUCTION,
    DEFAULT_TAGS,
    DEFAULT_TONE,
    ANTI_SLOP_INSTRUCTION,
    SOURCE_INSTRUCTION,
    OPTIONAL_SOURCES_INSTRUCTION,
    NEWS_SEARCH_INSTRUCTION,
)
from image import render_card_image

# --- Archive ---
ARCHIVE_DIR = Path(__file__).parent / "archive"
ARCHIVE_FILE = ARCHIVE_DIR / "posts.json"
IMAGES_DIR = ARCHIVE_DIR / "images"


def ensure_archive():
    ARCHIVE_DIR.mkdir(exist_ok=True)
    IMAGES_DIR.mkdir(exist_ok=True)
    if not ARCHIVE_FILE.exists():
        ARCHIVE_FILE.write_text("[]", encoding="utf-8")


def load_archive() -> List[Dict[str, Any]]:
    ensure_archive()
    try:
        return json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def save_to_archive(post: Dict[str, Any], card_image: Image.Image) -> str:
    ensure_archive()
    archive = load_archive()
    from datetime import datetime

    now = datetime.now()
    post_id = f"{now.strftime('%y%m%d')}_{len(archive) + 1:02d}"
    image_path = IMAGES_DIR / f"{post_id}.png"
    card_image.save(str(image_path), format="PNG")

    entry = {
        "id": post_id,
        "date": now.isoformat(),
        "topic": post.get("topic", ""),
        "title": post.get("title", ""),
        "rubric": post.get("rubric", ""),
        "essence": post.get("essence", ""),
        "body": post.get("body", ""),
        "sources": post.get("sources", []),
        "hashtags": post.get("hashtags", ""),
        "image_query": post.get("image_query", ""),
        "image_path": str(image_path),
    }
    archive.append(entry)
    ARCHIVE_FILE.write_text(
        json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return post_id


# --- JSON parsing ---
def extract_json(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    return json.loads(cleaned)


def extract_json_array(text: str) -> List[Dict[str, Any]]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    result = json.loads(cleaned)
    return result if isinstance(result, list) else []


def coerce_sources(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    sources: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        if title or url:
            sources.append({"title": title, "url": url})
    return sources


def coerce_hashtags(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(tag).strip() for tag in value if str(tag).strip())
    if isinstance(value, str):
        return value.strip()
    return ""


# --- Prompt builder ---
def build_prompt(
    topic: str,
    tone: str = DEFAULT_TONE,
    tags: str = DEFAULT_TAGS,
    use_search: bool = True,
) -> str:
    source_line = SOURCE_INSTRUCTION if use_search else OPTIONAL_SOURCES_INSTRUCTION

    archive = load_archive()
    history_block = ""
    if archive:
        recent = archive[-20:]
        topics = [f"- {p.get('title', '')} ({p.get('rubric', '')})" for p in recent]
        history_block = (
            "\nРанее мы уже делали посты (избегай повторов):\n"
            + "\n".join(topics)
            + "\n"
        )

    return f"""{DEFAULT_PROMPT_INSTRUCTION}

{ANTI_SLOP_INSTRUCTION}

Тон: {tone}
{source_line}
{history_block}
Тема: "{topic}".
Теги: {tags}

Верни JSON:
{{
  "card": {{ "title": "ЗАГОЛОВОК", "rubric": "РУБРИКА" }},
  "essence": "Краткая суть — главный инсайт",
  "post_body": "Основной текст поста",
  "sources": [ {{ "title": "Название", "url": "https://..." }} ],
  "hashtags": ["#тег1", "#тег2"],
  "image_query": "english search query for relevant image"
}}""".strip()


# --- Gemini client ---
def _get_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY не задан")
    return genai.Client(api_key=api_key)


# --- Generate post ---
def generate_post(
    topic: str,
    tone: str = DEFAULT_TONE,
    model: str = DEFAULT_MODEL,
    theme: str = "warm",
) -> Dict[str, Any]:
    """Generate a post + card image. Returns dict with all fields + 'card_image' (PIL Image)."""
    client = _get_client()
    prompt = build_prompt(topic, tone=tone)

    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    response = client.models.generate_content(
        model=model, contents=prompt, config=config
    )
    text = response.text or ""
    data = extract_json(text)

    card = data.get("card", {}) if isinstance(data, dict) else {}
    title = str(card.get("title", "")).strip()
    rubric = str(card.get("rubric", "")).strip()
    essence = str(data.get("essence", "")).strip()
    post_body = str(data.get("post_body", "")).strip()
    sources = coerce_sources(data.get("sources"))
    hashtags = coerce_hashtags(data.get("hashtags"))
    image_query = str(data.get("image_query", "")).strip()

    card_image = render_card_image(title, rubric, essence, None, theme=theme)

    result = {
        "topic": topic,
        "title": title,
        "rubric": rubric,
        "essence": essence,
        "body": post_body,
        "sources": sources,
        "hashtags": hashtags,
        "image_query": image_query,
        "card_image": card_image,
    }

    # Save to archive
    save_to_archive(result, card_image)

    return result


# --- Search news topics ---
def search_news(query: str = "", model: str = DEFAULT_MODEL) -> List[Dict[str, str]]:
    """Search for interesting topics. Returns list of {topic, hook, rubric}."""
    client = _get_client()
    prompt = NEWS_SEARCH_INSTRUCTION
    if query.strip():
        prompt += f"\n\nДополнительный фокус поиска: {query.strip()}"

    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    response = client.models.generate_content(
        model=model, contents=prompt, config=config
    )
    return extract_json_array(response.text or "")


# --- Format for Telegram (HTML) ---
def format_tg_post(post: Dict[str, Any]) -> str:
    """Format post as Telegram HTML message."""
    rubric = post.get("rubric", "").upper()
    title = post.get("title", "").upper()
    essence = post.get("essence", "")
    body = post.get("body", "")
    sources = post.get("sources", [])
    hashtags = post.get("hashtags", "")

    parts = [f"🎸 <b>{_esc(rubric)}</b>"]
    parts.append(f"\n<b>{_esc(title)}</b>")

    if essence:
        parts.append(f"\n{_esc(essence)}")

    if body:
        parts.append(f"\n{_esc(body)}")

    if sources:
        links = []
        for s in sources:
            t = s.get("title", "Источник")
            u = s.get("url", "")
            if u:
                links.append(f'🔗 <a href="{u}">{_esc(t)}</a>')
            else:
                links.append(f"🔗 {_esc(t)}")
        if links:
            parts.append("\n📚 <b>Источники:</b>\n" + "\n".join(links))

    if hashtags:
        parts.append(f"\n{_esc(hashtags)}")

    return "\n".join(parts)


def _esc(text: str) -> str:
    """Escape HTML special chars for Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Card image to bytes ---
def card_to_bytes(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
