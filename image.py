import textwrap
from pathlib import Path
from typing import List, Optional
from PIL import Image, ImageDraw, ImageFont

# --- Theme Configuration ---
THEMES = {
    "warm": {
        "bg_color": (255, 248, 235),
        "pattern_color": (240, 228, 205),
        "text_main": (60, 40, 20),
        "text_secondary": (100, 70, 40),
        "accent": (180, 100, 30),
        "line_color": (60, 40, 20),
        "rubric_color": (120, 70, 30),
        "logo_text": (60, 40, 20, 255),
        "logo_accent": (180, 100, 30, 255),
    },
    "dark": {
        "bg_color": (25, 25, 30),
        "pattern_color": (40, 40, 48),
        "text_main": (255, 245, 225),
        "text_secondary": (200, 185, 160),
        "accent": (218, 165, 32),
        "line_color": (218, 165, 32),
        "rubric_color": (218, 165, 32),
        "logo_text": (255, 245, 225, 255),
        "logo_accent": (218, 165, 32, 255),
    },
    "blue": {
        "bg_color": (15, 25, 55),
        "pattern_color": (25, 40, 75),
        "text_main": (255, 255, 255),
        "text_secondary": (180, 200, 230),
        "accent": (100, 160, 255),
        "line_color": (100, 160, 255),
        "rubric_color": (100, 160, 255),
        "logo_text": (255, 255, 255, 255),
        "logo_accent": (100, 160, 255, 255),
    },
}


def get_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    font_pairs = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"),
        ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/segoeui.ttf"),
    ]
    for bold_path, reg_path in font_pairs:
        primary = bold_path if bold else reg_path
        if Path(primary).exists():
            return ImageFont.truetype(primary, size=size)
        secondary = reg_path if bold else bold_path
        if Path(secondary).exists():
            return ImageFont.truetype(secondary, size=size)
    return ImageFont.load_default()


def create_default_logo(
    text_color: tuple = (60, 40, 20, 255),
    accent_color: tuple = (180, 100, 30, 255),
) -> Image.Image:
    width, height = 520, 200
    img = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)

    # Club name
    font_size = 36
    font = get_font(font_size, bold=True)
    draw.text((20, 20), "ЗАКРЫТЫЙ КЛУБ", font=font, fill=text_color)

    name_font = get_font(42, bold=True)
    draw.text((20, 62), "ПАВЛА", font=name_font, fill=accent_color)
    draw.text((20, 108), "СИДОРЕНКО", font=name_font, fill=accent_color)

    # Guitar string lines (decorative)
    for i in range(6):
        y = 140 + i * 8
        line_alpha = 255 - i * 30
        line_color = (*accent_color[:3], line_alpha)
        draw.line([(20, y), (width - 40, y)], fill=line_color, width=1)

    return img


def wrap_text(
    text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw
) -> List[str]:
    words = text.split()
    if not words:
        return [""]

    lines: List[str] = []
    current = words[0]
    for word in words[1:]:
        test = f"{current} {word}"
        if draw.textlength(test, font=font) <= max_width:
            current = test
        else:
            lines.append(current)
            current = word
    lines.append(current)

    if len(lines) == 1 and draw.textlength(lines[0], font=font) > max_width:
        lines = textwrap.wrap(text, width=max(8, int(max_width / 12)))
    return lines


def render_card_image(
    title: str,
    rubric: str,
    essence: str,
    logo_image: Optional[Image.Image],
    theme: str = "warm",
) -> Image.Image:
    if theme not in THEMES:
        theme = "warm"
    t = THEMES[theme]

    size = 1080
    img = Image.new("RGB", (size, size), t["bg_color"])
    draw = ImageDraw.Draw(img)

    # Dot pattern background
    spacing = 56
    radius = 2
    for y in range(40, size, spacing):
        for x in range(40, size, spacing):
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=t["pattern_color"],
            )

    # Logo
    if logo_image is None:
        logo_image = create_default_logo(
            text_color=t["logo_text"],
            accent_color=t["logo_accent"],
        )

    logo = logo_image.convert("RGBA")
    logo.thumbnail((280, 160), Image.LANCZOS)
    logo_x = size - logo.width - 50
    logo_y = 50
    img.paste(logo, (logo_x, logo_y), logo)

    # Divider line
    line_width = 180
    line_height = 6
    line_x = (size - line_width) // 2
    line_y = 250
    draw.rectangle(
        (line_x, line_y, line_x + line_width, line_y + line_height),
        fill=t["line_color"],
    )

    # Rubric
    rubric_font = get_font(30)
    rubric_text = rubric.upper() if rubric else "РУБРИКА"
    rubric_bbox = draw.textbbox((0, 0), rubric_text, font=rubric_font)
    rubric_w = rubric_bbox[2] - rubric_bbox[0]
    draw.text(
        ((size - rubric_w) / 2, line_y + 22),
        rubric_text,
        font=rubric_font,
        fill=t["rubric_color"],
    )

    # Title
    title_text = title.upper() if title else "ЗАГОЛОВОК"
    title_font_size = 96
    if len(title_text) > 80:
        title_font_size = 44
    elif len(title_text) > 60:
        title_font_size = 54
    elif len(title_text) > 45:
        title_font_size = 64
    elif len(title_text) > 25:
        title_font_size = 76

    title_font = get_font(title_font_size)
    max_width = size - 180
    title_lines = wrap_text(title_text, title_font, max_width, draw)
    title_line_height = int(title_font_size * 1.1)
    title_total = len(title_lines) * title_line_height

    # Lead / essence
    lead_text = (essence or "КРАТКАЯ СУТЬ").strip()
    lead_font = get_font(36, bold=False)
    lead_lines = wrap_text(lead_text, lead_font, max_width, draw)
    lead_line_height = int(36 * 1.4)
    lead_total = len(lead_lines) * lead_line_height

    content_top = 320
    content_bottom = 880
    min_gap = 28
    available = content_bottom - content_top

    lead_room = available - title_total - min_gap
    if lead_room < lead_line_height:
        lead_lines = []
        lead_total = 0
    else:
        max_lines = min(len(lead_lines), int(lead_room // lead_line_height))
        lead_lines = lead_lines[:max_lines]
        lead_total = len(lead_lines) * lead_line_height

    title_start_y = content_top
    lead_start_y = (
        content_bottom - lead_total if lead_total else content_top + title_total + min_gap
    )

    for idx, line in enumerate(title_lines):
        lw = draw.textlength(line, font=title_font)
        x = (size - lw) / 2
        y = title_start_y + idx * title_line_height
        draw.text((x, y), line, font=title_font, fill=t["text_main"])

    for idx, line in enumerate(lead_lines):
        lw = draw.textlength(line, font=lead_font)
        x = (size - lw) / 2
        y = lead_start_y + idx * lead_line_height
        draw.text((x, y), line, font=lead_font, fill=t["text_secondary"])

    # Accent bar
    accent_width = 200
    accent_height = 10
    accent_x = (size - accent_width) // 2
    accent_y = 905
    draw.rectangle(
        (accent_x, accent_y, accent_x + accent_width, accent_y + accent_height),
        fill=t["accent"],
    )

    return img
