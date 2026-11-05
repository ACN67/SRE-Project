"""Render the frozen main-2 architecture and data-flow diagram."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets/main2_system_architecture.png"
WIDTH, HEIGHT = 2400, 1440
SCALE = 150

COLORS = {
    "background": "#F7F9FA",
    "data": "#D9EAF7",
    "build": "#DDEEDB",
    "index": "#F3E1B8",
    "query": "#E5DDF3",
    "answer": "#F8D9C9",
    "eval": "#F3D6D6",
    "web": "#D4EFEF",
    "ink": "#1D2A33",
    "muted": "#52616B",
    "line": "#38515F",
    "gold": "#8C3D3D",
    "green": "#2C6B45",
    "teal": "#287A77",
}


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "arialbd.ttf" if bold else "arial.ttf"
    paths = (
        Path(r"C:\Windows\Fonts") / name,
        Path("/usr/share/fonts/truetype/dejavu") / (
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        ),
    )
    for path in paths:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


FONT_TITLE = font(48, bold=True)
FONT_SUBTITLE = font(24)
FONT_SECTION = font(25, bold=True)
FONT_BOX = font(23, bold=True)
FONT_BODY = font(18)
FONT_SMALL = font(16)
FONT_BOTTOM = font(21, bold=True)


def px(value: float) -> int:
    return round(value * SCALE)


def box(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    width: float,
    height: float,
    title: str,
    body: str = "",
    color: str = "data",
) -> None:
    left, top = px(x), px(y)
    right, bottom = px(x + width), px(y + height)
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=14,
        fill=COLORS[color],
        outline=COLORS["line"],
        width=3,
    )
    center = (left + right) // 2
    draw.text(
        (center, top + 17),
        title,
        font=FONT_BOX,
        fill=COLORS["ink"],
        anchor="ma",
    )
    if body:
        draw.multiline_text(
            (center, top + 55),
            body,
            font=FONT_BODY,
            fill=COLORS["muted"],
            anchor="ma",
            align="center",
            spacing=6,
        )


def arrow(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    dashed: bool = False,
    color: str = "line",
) -> None:
    x1, y1, x2, y2 = px(start[0]), px(start[1]), px(end[0]), px(end[1])
    fill = COLORS[color]
    if dashed:
        dx, dy = x2 - x1, y2 - y1
        length = max((dx * dx + dy * dy) ** 0.5, 1)
        for offset in range(0, int(length), 22):
            a = offset / length
            b = min((offset + 12) / length, 1)
            draw.line(
                (x1 + dx * a, y1 + dy * a, x1 + dx * b, y1 + dy * b),
                fill=fill,
                width=4,
            )
    else:
        draw.line((x1, y1, x2, y2), fill=fill, width=4)
    draw.polygon(
        (
            (x2, y2),
            (x2 - 16, y2 - 9),
            (x2 - 16, y2 + 9),
        ),
        fill=fill,
    )


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (WIDTH, HEIGHT), COLORS["background"])
    draw = ImageDraw.Draw(image)

    draw.text(
        (42, 34),
        "SRE-Project main-2 | Deterministic Memory Mainline",
        font=FONT_TITLE,
        fill=COLORS["ink"],
    )
    draw.text(
        (44, 96),
        "Deterministic extraction, review and state resolution; BGE + BM25 retrieval; "
        "DeepSeek only generates final answers",
        font=FONT_SUBTITLE,
        fill=COLORS["muted"],
    )

    draw.rounded_rectangle(
        (px(0.2), px(1.3), px(10.5), px(4.68)),
        radius=22,
        fill="#F3F7F4",
    )
    draw.rounded_rectangle(
        (px(10.75), px(1.3), px(14.8), px(4.68)),
        radius=22,
        fill="#F6F8F7",
    )
    draw.rounded_rectangle(
        (px(0.2), px(4.95), px(14.8), px(8.85)),
        radius=22,
        fill="#F5F8FA",
    )
    draw.text(
        (px(0.42), px(1.42)),
        "OFFLINE MEMORY BUILD",
        font=FONT_SECTION,
        fill=COLORS["muted"],
    )
    draw.text(
        (px(10.94), px(1.42)),
        "WEB PRESENTATION",
        font=FONT_SECTION,
        fill=COLORS["muted"],
    )
    draw.text(
        (px(0.42), px(5.05)),
        "ONLINE QUERY AND GOLD-ISOLATED EVALUATION",
        font=FONT_SECTION,
        fill=COLORS["muted"],
    )

    build_boxes = (
        (
            0.42,
            1.85,
            1.55,
            2.15,
            "LoCoMo Conversation",
            "4 dev + 2 test\nraw turns and dates",
            "data",
        ),
        (
            2.22,
            1.85,
            1.55,
            2.15,
            "Window Planner",
            "deterministic\ncheckpoint windows",
            "build",
        ),
        (
            4.02,
            1.85,
            1.72,
            2.15,
            "Auto Extraction v10",
            "utterance facts\nspeaker / time / attrs",
            "build",
        ),
        (
            5.99,
            1.85,
            1.55,
            2.15,
            "Auto Review v1",
            "source overlap\nnegation / date checks",
            "build",
        ),
        (
            7.79,
            1.85,
            1.25,
            2.15,
            "Reviewed Store",
            "approved Memory\nstrict schema 2.1",
            "build",
        ),
        (
            9.29,
            1.85,
            1.05,
            2.15,
            "Hybrid Index",
            "BGE + BM25\nstable candidates",
            "index",
        ),
    )
    for item in build_boxes:
        box(draw, *item)
    for start, end in (
        ((1.97, 2.92), (2.22, 2.92)),
        ((3.77, 2.92), (4.02, 2.92)),
        ((5.74, 2.92), (5.99, 2.92)),
        ((7.54, 2.92), (7.79, 2.92)),
        ((9.04, 2.92), (9.29, 2.92)),
    ):
        arrow(draw, start, end)
    draw.text(
        (px(4.18), px(4.17)),
        "No provider call in extraction, review, publication, or indexing",
        font=FONT_BODY,
        fill=COLORS["green"],
    )
    draw.rounded_rectangle(
        (px(7.86), px(4.25), px(10.45), px(4.47)),
        radius=10,
        fill="#FFF7E8",
        outline="#8B6B3F",
        width=2,
    )
    draw.text(
        (px(9.15), px(4.36)),
        "Historical comparison: DeepSeek extraction / review / state",
        font=FONT_SMALL,
        fill="#6D542F",
        anchor="mm",
    )

    box(
        draw,
        10.98,
        1.82,
        3.53,
        0.70,
        "Memory Browser",
        "filter, inspect, source jump, JSON export",
        "web",
    )
    box(
        draw,
        10.98,
        2.70,
        3.53,
        0.70,
        "Three-method Console",
        "Full Text / Vector RAG / Dynamic",
        "web",
    )
    box(
        draw,
        10.98,
        3.58,
        3.53,
        0.70,
        "Explainability Panels",
        "candidates, decisions, evidence, status",
        "web",
    )
    arrow(draw, (11.70, 2.52), (11.70, 2.70), color="teal")
    arrow(draw, (11.70, 3.40), (11.70, 3.58), color="teal")

    box(draw, 0.42, 5.55, 1.50, 1.22, "Question", "optional as_of", "query")
    box(draw, 2.19, 5.55, 1.62, 1.22, "Rule Analysis", "intent / entity / time", "query")
    box(draw, 4.07, 5.55, 1.55, 1.22, "Ordered Candidates", "base 8, max 12", "index")
    box(draw, 5.91, 6.13, 1.72, 0.98, "Vector RAG", "direct evidence context", "query")
    box(draw, 5.91, 5.08, 1.72, 0.90, "Dynamic", "deterministic state", "query")
    box(
        draw,
        7.94,
        6.09,
        1.48,
        1.11,
        "Evidence Answer",
        "DeepSeek Flash\nthinking high",
        "answer",
    )
    box(
        draw,
        7.94,
        5.02,
        1.48,
        0.93,
        "State Resolution",
        "RETAIN / EXCLUDE\nUNRESOLVED",
        "build",
    )
    arrow(draw, (1.92, 6.16), (2.19, 6.16))
    arrow(draw, (3.81, 6.16), (4.07, 6.16))
    arrow(draw, (5.62, 6.16), (5.91, 6.62))
    arrow(draw, (5.62, 6.16), (5.91, 5.53))
    arrow(draw, (7.63, 6.62), (7.94, 6.64))
    arrow(draw, (7.63, 5.53), (7.94, 5.49))
    arrow(draw, (8.68, 5.95), (8.68, 6.09))

    box(
        draw,
        9.75,
        5.48,
        1.35,
        1.35,
        "Prediction Lock",
        "write + SHA verify\nbefore Gold",
        "eval",
    )
    box(
        draw,
        11.32,
        5.48,
        1.55,
        1.35,
        "Official Scoring",
        "LoCoMo composite\ntext F1",
        "eval",
    )
    box(
        draw,
        13.10,
        5.48,
        1.50,
        1.35,
        "Evidence + State",
        "Recall@5/@10\nstate subset",
        "eval",
    )
    arrow(draw, (9.42, 6.15), (9.75, 6.15))
    arrow(draw, (11.10, 6.15), (11.32, 6.15))
    arrow(draw, (12.87, 6.15), (13.10, 6.15))
    draw.text(
        (px(11.85), px(7.0)),
        "Gold remains sealed until all predictions pass byte-bound verification.",
        font=FONT_BODY,
        fill=COLORS["gold"],
        anchor="ma",
    )

    arrow(draw, (13.76, 5.48), (12.74, 4.28), dashed=True, color="teal")
    draw.text((px(13.05), px(4.72)), "results", font=FONT_SMALL, fill=COLORS["teal"])

    draw.rounded_rectangle(
        (px(0.42), px(8.05), px(14.52), px(8.62)),
        radius=14,
        fill="#EAF0F3",
        outline="#8AA0AA",
        width=2,
    )
    draw.text(
        (px(7.47), px(8.34)),
        "Frozen contracts: schema 2.1 | source-grounded Memory | same ordered candidates | "
        "provider calls only for final answers | immutable fingerprints",
        font=FONT_BOTTOM,
        fill=COLORS["ink"],
        anchor="mm",
    )

    image.save(OUTPUT, format="PNG", optimize=True)
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
