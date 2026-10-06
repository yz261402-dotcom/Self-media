"""封面派生只读原图，按平台比例完整包含前景并隔离输出。"""
import hashlib
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, UnidentifiedImageError


COLORS = [(240, 30, 30), (30, 220, 30), (30, 30, 240), (240, 220, 30)]


def marked_image(path, *, size=(180, 320), orientation=None):
    image = Image.new('RGB', size, (125, 135, 145))
    draw = ImageDraw.Draw(image)
    width, height = size
    for box, color in zip([(0, 0, 25, 25), (width-26, 0, width-1, 25),
                           (0, height-26, 25, height-1),
                           (width-26, height-26, width-1, height-1)], COLORS):
        draw.rectangle(box, fill=color)
    if orientation:
        exif = image.getexif()
        exif[274] = orientation
        image.save(path, quality=98, exif=exif)
    else:
        image.save(path)


def module(monkeypatch, tmp_path):
    from app.publishing import cover_assets
    monkeypatch.setattr(cover_assets, 'OUTPUT_ROOT', tmp_path / 'prepared-covers')
    return cover_assets


def assert_corners(path, expected, *, source_size=(180, 320)):
    with Image.open(path) as image:
        scale = min(image.width/source_size[0], image.height/source_size[1])
        width, height = round(source_size[0]*scale), round(source_size[1]*scale)
        left, top = (image.width-width)//2, (image.height-height)//2
        points = [(left+round(10*scale), top+round(10*scale)),
                  (left+width-round(10*scale), top+round(10*scale)),
                  (left+round(10*scale), top+height-round(10*scale)),
                  (left+width-round(10*scale), top+height-round(10*scale))]
        for point, color in zip(points, expected):
            actual = image.getpixel(point)
            assert max(abs(a-b) for a, b in zip(actual, color)) < 35
        assert sum(image.getpixel((0, image.height//2))) > 30  # 同源背景补满，不留黑边。


def test_cover_assets_preserve_source_corners_and_platform_dimensions(monkeypatch, tmp_path):
    source = tmp_path / 'source.png'
    marked_image(source)
    original = source.read_bytes()
    cover_assets = module(monkeypatch, tmp_path)

    outputs = cover_assets.prepare_cover_assets(source)

    assert set(outputs) == {'portrait', 'landscape'}
    assert source.read_bytes() == original
    digest = hashlib.sha256(original).hexdigest()
    for kind, size in [('portrait', (1080, 1440)), ('landscape', (1440, 1080))]:
        path = outputs[kind]
        assert isinstance(path, Path)
        assert path == cover_assets.OUTPUT_ROOT / digest / (kind + '.jpg')
        with Image.open(path) as image:
            assert image.size == size
            assert image.format == 'JPEG' and image.mode == 'RGB'
        assert_corners(path, COLORS)


def test_cover_assets_apply_exif_orientation_without_changing_source(monkeypatch, tmp_path):
    source = tmp_path / 'rotated.jpg'
    marked_image(source, size=(320, 180), orientation=6)
    original = source.read_bytes()

    outputs = module(monkeypatch, tmp_path).prepare_cover_assets(source)

    assert source.read_bytes() == original
    for path in outputs.values():
        assert_corners(path, [COLORS[2], COLORS[0], COLORS[3], COLORS[1]])
        with Image.open(path) as image:
            assert image.getexif().get(274, 1) == 1


def test_same_filename_different_bytes_cannot_overwrite_another_cover(monkeypatch, tmp_path):
    cover_assets = module(monkeypatch, tmp_path)
    source = tmp_path / 'source.png'
    marked_image(source)
    first = cover_assets.prepare_cover_assets(source)
    first_bytes = {kind: path.read_bytes() for kind, path in first.items()}
    Image.new('RGB', (180, 320), 'white').save(source)

    second = cover_assets.prepare_cover_assets(source)

    assert first['portrait'].parent != second['portrait'].parent
    assert all(path.read_bytes() == first_bytes[kind] for kind, path in first.items())


def test_identical_source_bytes_produce_deterministic_assets(monkeypatch, tmp_path):
    cover_assets = module(monkeypatch, tmp_path)
    source = tmp_path / 'source.png'
    marked_image(source)
    first = cover_assets.prepare_cover_assets(source)
    first_bytes = {kind: path.read_bytes() for kind, path in first.items()}
    renamed = tmp_path / 'another-name.png'
    renamed.write_bytes(source.read_bytes())

    second = cover_assets.prepare_cover_assets(renamed)

    assert second == first
    assert all(path.read_bytes() == first_bytes[kind] for kind, path in second.items())


def test_invalid_source_does_not_create_finished_assets(monkeypatch, tmp_path):
    cover_assets = module(monkeypatch, tmp_path)
    source = tmp_path / 'not-image.png'
    source.write_bytes(b'not an image')

    with pytest.raises(UnidentifiedImageError):
        cover_assets.prepare_cover_assets(source)

    assert not list(cover_assets.OUTPUT_ROOT.rglob('*.jpg'))
