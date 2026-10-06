"""将已确认封面派生为平台比例；完整保留前景，不改原图或交付清单。"""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image, ImageEnhance, ImageFilter, ImageOps


OUTPUT_ROOT = Path(__file__).resolve().parents[2] / 'runtime/publish/prepared-covers'


def prepare_cover_assets(source: Path) -> dict[str, Path]:
    original = Path(source).read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    with Image.open(BytesIO(original)) as opened:
        image = ImageOps.exif_transpose(opened).convert('RGB')
        image.load()

    output_dir = OUTPUT_ROOT / digest
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for kind, size in {'portrait': (1080, 1440), 'landscape': (1440, 1080)}.items():
        background = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
        background = background.filter(ImageFilter.GaussianBlur(radius=40))
        background = ImageEnhance.Brightness(background).enhance(0.65)
        foreground = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
        background.paste(foreground, ((size[0] - foreground.width) // 2,
                                      (size[1] - foreground.height) // 2))
        destination = output_dir / (kind + '.jpg')
        with NamedTemporaryFile(dir=output_dir, prefix=kind + '-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
        try:
            background.save(temporary, format='JPEG', quality=92, subsampling=0)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        outputs[kind] = destination
    return outputs
