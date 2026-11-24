"""EMF charts drawn to PNG with Pillow: enough of GDI for Excel's charts, no new dependency."""
import io

from PIL import Image

from emf_render import render_emf
from word_fixtures import emf_brush, emf_chart, emf_font, emf_pen, emf_polygon16, emf_polyline16, emf_select, emf_text, emf_world


def _image(data, max_px=1600):
    return Image.open(io.BytesIO(render_emf(data, max_px=max_px))).convert("RGB")


def test_a_red_line_is_drawn_where_its_points_are():
    image = _image(emf_chart([emf_pen(1, (255, 0, 0)), emf_select(1), emf_polyline16([(20, 100), (380, 100)])]))

    on_line = image.getpixel((200, 100))
    assert on_line[0] > 200 and on_line[1] < 80 and on_line[2] < 80
    assert image.getpixel((200, 20)) == (255, 255, 255)


def test_a_filled_polygon_is_filled_with_its_brush():
    image = _image(emf_chart([emf_brush(2, (0, 0, 255)), emf_select(2),
                              emf_polygon16([(100, 50), (300, 50), (300, 150), (100, 150)])]))

    assert image.getpixel((200, 100)) == (0, 0, 255)


def test_text_is_drawn_at_its_bounds():
    image = _image(emf_chart([emf_text("12,97", (150, 80, 250, 120))]))

    inside = [image.getpixel((x, y)) for x in range(150, 250) for y in range(80, 120)]
    outside = [image.getpixel((x, y)) for x in range(10, 100) for y in range(10, 60)]
    assert any(sum(p) < 300 for p in inside)
    assert all(p == (255, 255, 255) for p in outside)


def test_not_an_emf_gives_none():
    assert render_emf(b"\x89PNG\r\n\x1a\n" + bytes(100)) is None


def test_the_long_side_is_at_most_max_px():
    image = _image(emf_chart([emf_pen(1, (0, 0, 0)), emf_select(1), emf_polyline16([(0, 0), (3999, 999)])],
                             width=4000, height=1000), max_px=1600)

    assert max(image.size) == 1600


def _dark_box(image):
    dark = [(x, y) for x in range(image.width) for y in range(image.height) if sum(image.getpixel((x, y))) < 300]
    xs, ys = [x for x, _ in dark], [y for _, y in dark]
    return min(xs), min(ys), max(xs), max(ys)


def test_a_text_whose_font_turns_it_is_drawn_upright_and_stays_inside_its_box():
    # SKDU Tw III Grafik 2 prints "73,80" sideways inside its bar: its font has escapement 900.
    image = _image(emf_chart([emf_font(3, height=20, escapement=900), emf_select(3),
                              emf_text("73,80", (190, 40, 214, 160))]))

    left, top, right, bottom = _dark_box(image)
    assert bottom - top > right - left
    assert 186 <= left and right <= 218 and 36 <= top and bottom <= 164


def test_a_short_label_in_a_narrow_box_stays_level():
    # Roman quarter labels ("II", "III") sit in boxes taller than wide; their font is level.
    image = _image(emf_chart([emf_font(3, height=20), emf_select(3), emf_text("73,80", (190, 40, 214, 160))]))

    left, top, right, bottom = _dark_box(image)
    assert right - left > bottom - top


def test_a_text_turned_by_the_world_transform_is_drawn_upright():
    # The real SKDU Tw III Grafik 2: "73,80" is level in its font, turned by the world transform
    # (0, -1, 1, 0), bold, inside its bar.
    image = _image(emf_chart([emf_font(3, height=20, weight=700), emf_select(3),
                              emf_world(0.0, -1.0, 1.0, 0.0, 214, 160), emf_text("73,80", (190, 40, 214, 160))]))

    left, top, right, bottom = _dark_box(image)
    assert bottom - top > right - left


def test_a_large_emf_is_drawn_at_the_output_size_never_at_its_full_size(monkeypatch):
    # SPE's largest EMF is 10653 × 2688 px (~86 MB as RGB) on a 512 MB Render instance.
    import emf_render
    from PIL import Image as PILImage

    sizes = []
    real_new = PILImage.new

    def new(mode, size, *args, **kwargs):
        sizes.append(size)
        return real_new(mode, size, *args, **kwargs)

    monkeypatch.setattr(emf_render.Image, "new", new)

    image = _image(emf_chart([emf_pen(1, (0, 0, 0)), emf_select(1), emf_polyline16([(0, 0), (7999, 1999)])],
                             width=8000, height=2000), max_px=1600)

    assert max(max(size) for size in sizes) <= 1600
    left, top, right, bottom = _dark_box(image)
    assert left <= 2 and right >= 1597 and bottom >= 397           # the line still crosses the whole chart
