import json
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types
import streamlit as st
from PIL import Image

from config import (
    DEFAULT_MODEL,
    DEFAULT_PROMPT_INSTRUCTION,
    DEFAULT_TAGS,
    DEFAULT_TONE,
    STATIC_MODEL_OPTIONS,
    ANTI_SLOP_INSTRUCTION,
    SOURCE_INSTRUCTION,
    OPTIONAL_SOURCES_INSTRUCTION,
    NEWS_SEARCH_INSTRUCTION,
)
from image import render_card_image


# --- Env loader ---
def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file()


# --- Helpers ---
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


def safe_model_dump(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    return None


def extract_search_log(response: Any) -> Dict[str, Any]:
    log: Dict[str, Any] = {"status": "unknown", "queries": [], "sources": []}
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        log["status"] = "not_used"
        return log

    found_signal = False
    for candidate in candidates:
        grounding = safe_model_dump(getattr(candidate, "grounding_metadata", None))
        if grounding is None:
            candidate_dict = safe_model_dump(candidate)
            if candidate_dict:
                grounding = candidate_dict.get("grounding_metadata") or candidate_dict.get(
                    "groundingMetadata"
                )
                grounding = safe_model_dump(grounding)
        if not grounding:
            continue

        for query in grounding.get("web_search_queries") or []:
            if query and query not in log["queries"]:
                log["queries"].append(query)
        for query in grounding.get("retrieval_queries") or []:
            if query and query not in log["queries"]:
                log["queries"].append(query)

        chunks = grounding.get("grounding_chunks") or grounding.get("groundingChunks") or []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                chunk = safe_model_dump(chunk) or {}
            web = chunk.get("web") or {}
            if not isinstance(web, dict):
                web = safe_model_dump(web) or {}
            url = (web.get("uri") or "").strip()
            title = (web.get("title") or url).strip()
            if url or title:
                log["sources"].append({"title": title, "url": url})

        if log["queries"] or log["sources"]:
            found_signal = True

    log["status"] = "used" if found_signal else "not_used"
    return log


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
        "topic": st.session_state.get("topic", ""),
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
    ARCHIVE_FILE.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    return post_id


# --- Tone profiles ---
def load_tone_profiles() -> Dict[str, str]:
    path = Path(__file__).parent / "tone_profiles.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return {"Клубный (Базовый)": DEFAULT_TONE}


# --- Prompt builder ---
def build_prompt(
    topic: str,
    tags: str,
    tone: str,
    instruction: str,
    use_search: bool,
    refinement: str,
    current_content: Optional[Dict[str, Any]],
) -> str:
    source_line = SOURCE_INSTRUCTION if use_search else OPTIONAL_SOURCES_INSTRUCTION
    refinement = refinement.strip()
    refinement_line = f"\nУточнение: {refinement}\n" if refinement else ""
    current_block = ""
    if refinement and current_content:
        current_json = json.dumps(current_content, ensure_ascii=False, indent=2)
        current_block = (
            "\nТекущий черновик (переработай с учётом уточнения, сохраняя факты):\n"
            f"{current_json}\n"
        )

    # Archive context to avoid repeats
    archive = load_archive()
    history_block = ""
    if archive:
        recent = archive[-20:]
        topics = [f"- {p.get('title', '')} ({p.get('rubric', '')})" for p in recent]
        history_block = f"\nРанее мы уже делали посты (избегай повторов):\n" + "\n".join(topics) + "\n"

    return f"""{instruction}

{ANTI_SLOP_INSTRUCTION}

Тон: {tone}
{source_line}
{history_block}
{refinement_line}
{current_block}
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


# --- Generation ---
def run_generation(topic: str, refinement: str, use_current: bool) -> None:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        st.error("GEMINI_API_KEY не найден в окружении или .env")
        return

    with st.spinner("Генерируем контент..."):
        try:
            client = genai.Client(api_key=api_key)
            current_content = None
            if use_current:
                current_content = {
                    "card": {
                        "title": st.session_state.get("card_title", ""),
                        "rubric": st.session_state.get("card_rubric", ""),
                    },
                    "essence": st.session_state.get("post_essence", ""),
                    "post_body": st.session_state.get("post_body", ""),
                    "sources": st.session_state.get("sources", []),
                    "hashtags": st.session_state.get("post_hashtags", ""),
                }
            prompt = build_prompt(
                topic,
                st.session_state["tags_input"],
                st.session_state["tone_of_voice"],
                st.session_state["custom_instruction"],
                st.session_state["use_google_search"],
                refinement,
                current_content,
            )
            selected_model = st.session_state["model_name"]
            if st.session_state.get("use_custom_model"):
                custom = st.session_state.get("custom_model_name", "").strip()
                if custom:
                    selected_model = custom

            config = None
            if st.session_state["use_google_search"]:
                config = types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                )
            response = client.models.generate_content(
                model=selected_model,
                contents=prompt,
                config=config,
            )
            text = response.text or ""
            st.session_state["last_response"] = text
            st.session_state["search_log"] = extract_search_log(response)
            data = extract_json(text)

            card = data.get("card", {}) if isinstance(data, dict) else {}
            st.session_state["card_title"] = str(card.get("title", "")).strip()
            st.session_state["card_rubric"] = str(card.get("rubric", "")).strip()
            essence = str(data.get("essence", "")).strip()
            st.session_state["post_essence"] = essence
            st.session_state["card_lead"] = essence
            st.session_state["post_body"] = str(data.get("post_body", "")).strip()
            st.session_state["sources"] = coerce_sources(data.get("sources"))
            st.session_state["post_hashtags"] = coerce_hashtags(data.get("hashtags"))
            st.session_state["image_query"] = str(data.get("image_query", "")).strip()
            st.session_state["has_content"] = True
        except Exception as exc:
            st.session_state["has_content"] = False
            st.error(f"Ошибка генерации: {exc}")
            if st.session_state.get("last_response"):
                with st.expander("Ответ модели"):
                    st.code(st.session_state["last_response"])


# --- News search ---
def search_news_topics(query: str = "") -> List[Dict[str, str]]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return []

    client = genai.Client(api_key=api_key)
    prompt = NEWS_SEARCH_INSTRUCTION
    if query.strip():
        prompt += f"\n\nДополнительный фокус поиска: {query.strip()}"

    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    selected_model = st.session_state.get("model_name", "gemini-2.5-flash")
    response = client.models.generate_content(
        model=selected_model,
        contents=prompt,
        config=config,
    )
    text = response.text or ""

    # Parse JSON array from response
    cleaned = text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        topics = json.loads(cleaned)
        if isinstance(topics, list):
            return topics
    except json.JSONDecodeError:
        pass
    return []


# --- Sync helpers ---
def sync_card_lead_to_post():
    st.session_state["post_essence"] = st.session_state.get("card_lead", "")


def sync_post_to_card():
    st.session_state["card_lead"] = st.session_state.get("post_essence", "")


# --- Model list ---
@st.cache_data(show_spinner=False)
def get_model_options(api_key: str) -> List[str]:
    if not api_key:
        return STATIC_MODEL_OPTIONS
    try:
        client = genai.Client(api_key=api_key)
        names: List[str] = []
        for model in client.models.list():
            name = getattr(model, "name", "") or ""
            if "gemini" not in name or "embedding" in name:
                continue
            if name.startswith("models/"):
                name = name.split("/", 1)[1]
            supported = getattr(model, "supported_actions", None) or getattr(
                model, "supported_generation_methods", None
            )
            if supported and "generateContent" not in supported:
                continue
            names.append(name)
        names = sorted(set(names))
        return names or STATIC_MODEL_OPTIONS
    except Exception:
        return STATIC_MODEL_OPTIONS


# --- Post formatter ---
def format_post_text(
    rubric: str,
    title: str,
    essence: str,
    body: str,
    sources: List[Dict[str, str]],
    hashtags: str,
) -> str:
    parts = [f"🎸 {rubric.upper()}", f"🔥 **{title.upper()}**"]
    if essence:
        parts.append(f"\n{essence}\n")
    if body:
        parts.append(f"\n{body}\n")
    if sources:
        lines = [f"🔗 {s['title']}: {s['url']}" for s in sources if s.get("url")]
        if lines:
            parts.append("\n📚 **Источники:**\n" + "\n".join(lines) + "\n")
    if hashtags:
        parts.append(f"\n{hashtags}")
    return "\n".join(parts).strip()


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(page_title="Клуб Павла Сидоренко", page_icon="🎸", layout="wide")

if "card_title" not in st.session_state:
    st.session_state.update(
        {
            "topic": "",
            "tags_input": DEFAULT_TAGS,
            "tone_of_voice": DEFAULT_TONE,
            "custom_instruction": DEFAULT_PROMPT_INSTRUCTION,
            "model_name": DEFAULT_MODEL,
            "use_custom_model": False,
            "custom_model_name": "",
            "use_google_search": True,
            "search_log": {"status": "unknown", "queries": [], "sources": []},
            "card_title": "",
            "card_rubric": "",
            "card_lead": "",
            "post_essence": "",
            "post_body": "",
            "post_hashtags": "",
            "image_query": "",
            "sources": [],
            "logo_bytes": None,
            "has_content": False,
            "last_response": "",
            "refinement": "",
            "card_theme": "warm",
            "news_suggestions": [],
        }
    )
if "card_lead" not in st.session_state:
    st.session_state["card_lead"] = st.session_state.get("post_essence", "")
if "refinement" not in st.session_state:
    st.session_state["refinement"] = ""

st.title("🎸 Закрытый клуб Павла Сидоренко")
st.caption("Генератор карточек и постов для гитарного клуба")

tone_profiles = load_tone_profiles()

col_settings, col_card, col_post = st.columns([1.2, 1, 1.2], gap="large")

# --- Settings column ---
with col_settings:
    st.subheader("Тема и генерация")
    topic = st.text_input(
        "Тема поста",
        value=st.session_state["topic"],
        placeholder="Напр: Wes Montgomery и техника октав",
    )
    st.session_state["topic"] = topic

    generate_clicked = st.button("Сгенерировать", type="primary", disabled=not topic)

    # --- News suggestions ---
    with st.expander("🔍 Найти интересные темы", expanded=not st.session_state["has_content"]):
        news_query = st.text_input(
            "Фокус поиска (необязательно)",
            placeholder="Напр: педали овердрайв, джаз 60-х, полуакустики...",
            key="news_query",
        )
        search_news_clicked = st.button("Найти темы")

        if search_news_clicked:
            with st.spinner("Ищем интересные темы..."):
                try:
                    suggestions = search_news_topics(news_query)
                    st.session_state["news_suggestions"] = suggestions
                except Exception as exc:
                    st.error(f"Ошибка поиска: {exc}")
                    st.session_state["news_suggestions"] = []

        if st.session_state.get("news_suggestions"):
            for i, s in enumerate(st.session_state["news_suggestions"]):
                topic_text = s.get("topic", "")
                hook = s.get("hook", "")
                rubric = s.get("rubric", "")
                col_topic, col_btn = st.columns([4, 1])
                with col_topic:
                    st.markdown(f"**{rubric}** · {topic_text}")
                    if hook:
                        st.caption(hook)
                with col_btn:
                    if st.button("→", key=f"pick_{i}", help="Использовать эту тему"):
                        st.session_state["topic"] = topic_text
                        st.rerun()

    st.text_area(
        "Уточнение для перегенерации",
        key="refinement",
        height=80,
        placeholder="Например: добавь про педали, сделай короче, упомяни конкретные альбомы...",
    )
    refine_clicked = st.button(
        "Перегенерировать с уточнением",
        disabled=not topic or not st.session_state["refinement"].strip(),
    )

    with st.expander("Настройки ИИ"):
        # Tone profile selector
        tone_name = st.selectbox("Тоновый профиль", list(tone_profiles.keys()))
        if tone_name:
            st.session_state["tone_of_voice"] = tone_profiles[tone_name]

        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        model_options = get_model_options(api_key)
        if st.session_state.get("model_name") not in model_options:
            st.session_state["model_name"] = model_options[0]

        st.selectbox("Модель", model_options, key="model_name")
        st.checkbox("Кастомная модель", key="use_custom_model")
        if st.session_state["use_custom_model"]:
            st.text_input("ID модели", key="custom_model_name", placeholder="gemini-3-pro")

        st.checkbox("Google Search", key="use_google_search")
        st.text_area("Тон изложения", key="tone_of_voice", height=120)
        st.text_area("Инструкция для ИИ", key="custom_instruction", height=220)
        st.text_area("Теги", key="tags_input", height=80)

    with st.expander("Логотип и тема"):
        upload = st.file_uploader("Логотип (PNG/JPG)", type=["png", "jpg", "jpeg", "webp"])
        if upload is not None:
            st.session_state["logo_bytes"] = upload.getvalue()
        if st.button("Сбросить логотип"):
            st.session_state["logo_bytes"] = None

        st.selectbox(
            "Тема карточки",
            ["warm", "dark", "blue"],
            format_func=lambda x: {"warm": "🟤 Тёплая (винтаж)", "dark": "⚫ Тёмная (клуб)", "blue": "🔵 Блюзовая"}[x],
            key="card_theme",
        )

    if generate_clicked:
        run_generation(topic, "", False)
    if refine_clicked:
        run_generation(topic, st.session_state["refinement"], True)

# --- Card column ---
with col_card:
    st.subheader("Карточка")
    logo_image: Optional[Image.Image] = None
    if st.session_state.get("logo_bytes"):
        try:
            logo_image = Image.open(BytesIO(st.session_state["logo_bytes"]))
        except Exception:
            logo_image = None

    card_image = render_card_image(
        st.session_state["card_title"],
        st.session_state["card_rubric"],
        st.session_state["card_lead"],
        logo_image,
        theme=st.session_state.get("card_theme", "warm"),
    )
    st.image(card_image, use_container_width=True)

    col_dl, col_save = st.columns(2)
    with col_dl:
        buffer = BytesIO()
        card_image.save(buffer, format="PNG")
        st.download_button(
            "Скачать PNG",
            data=buffer.getvalue(),
            file_name="sidorenko-card.png",
            mime="image/png",
        )
    with col_save:
        if st.button("Сохранить в архив", disabled=not st.session_state["has_content"]):
            post_data = {
                "title": st.session_state["card_title"],
                "rubric": st.session_state["card_rubric"],
                "essence": st.session_state["post_essence"],
                "body": st.session_state["post_body"],
                "sources": st.session_state["sources"],
                "hashtags": st.session_state["post_hashtags"],
                "image_query": st.session_state.get("image_query", ""),
            }
            post_id = save_to_archive(post_data, card_image)
            st.success(f"Сохранено: {post_id}")

    with st.expander("Редактировать карточку"):
        st.text_input("Рубрика", key="card_rubric")
        st.text_area("Заголовок", key="card_title", height=100)
        st.text_area("Мини-лид", key="card_lead", height=80, on_change=sync_card_lead_to_post)

    # Image query hint
    if st.session_state.get("image_query"):
        st.info(f"🔍 Запрос для изображения: {st.session_state['image_query']}")

# --- Post column ---
with col_post:
    st.subheader("Текст для Telegram")
    if st.session_state["has_content"]:
        st.text_area("Лид-абзац", key="post_essence", height=80, on_change=sync_post_to_card)
        st.text_area("Основной текст", key="post_body", height=240)

        if st.session_state["sources"]:
            st.markdown("**Ссылки:**")
            for item in st.session_state["sources"]:
                title = item.get("title") or "Источник"
                url = item.get("url") or ""
                if url:
                    st.markdown(f"- [{title}]({url})")
                else:
                    st.markdown(f"- {title}")

        st.text_area("Хештеги", key="post_hashtags", height=80)

        post_text = format_post_text(
            st.session_state["card_rubric"],
            st.session_state["card_title"],
            st.session_state["post_essence"],
            st.session_state["post_body"],
            st.session_state["sources"],
            st.session_state["post_hashtags"],
        )
        st.text_area("Готовый текст для копирования", value=post_text, height=220)
    else:
        st.info("Заполните тему и нажмите «Сгенерировать».")

# --- Search log ---
with col_settings:
    if st.session_state["use_google_search"] and st.session_state["has_content"]:
        log = st.session_state.get("search_log", {})
        status = log.get("status", "unknown")
        if status == "used":
            st.success("Google Search: использован")
        elif status == "not_used":
            st.warning("Google Search: не использован моделью")
        else:
            st.info("Google Search: нет данных")

        with st.expander("Лог Google Search"):
            queries = log.get("queries", [])
            sources = log.get("sources", [])
            if queries:
                st.markdown("**Запросы:**")
                for q in queries:
                    st.markdown(f"- {q}")
            if sources:
                st.markdown("**Источники:**")
                for s in sources:
                    title = s.get("title") or "Источник"
                    url = s.get("url") or ""
                    if url:
                        st.markdown(f"- [{title}]({url})")
                    else:
                        st.markdown(f"- {title}")
