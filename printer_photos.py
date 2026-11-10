"""Validated, local printer portraits independent of printer firmware."""
import hashlib
import io
import pathlib
import re
import warnings
from PIL import Image, ImageOps

UPLOAD_LIMIT = 5 * 1024 * 1024
PHOTO_NAME = re.compile(r'[a-f0-9]{64}\.webp')

def photo_url(printer):
    name = printer.get('photo', '')
    return '/printer-photos/' + name if isinstance(name, str) and PHOTO_NAME.fullmatch(name) else ''

def prepare_photo(raw):
    if not 0 < len(raw) <= UPLOAD_LIMIT:
        raise ValueError('Choose a photo up to 5 MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ('JPEG', 'PNG', 'WEBP'):
                    raise ValueError('Choose a JPEG, PNG or WebP photo')
                if image.width * image.height > 16000000:
                    raise ValueError('Choose a photo with at most 16 megapixels')
                image.load()
                image = ImageOps.exif_transpose(image)
                image.thumbnail((512, 512))
                image = image.convert('RGBA' if image.mode in ('RGBA', 'LA') or 'transparency' in image.info else 'RGB')
                output = io.BytesIO()
                image.save(output, format='WEBP', quality=88)
                return output.getvalue()
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ValueError('Choose a smaller photo') from None
    except (OSError, SyntaxError):
        raise ValueError('This file is not a valid JPEG, PNG or WebP photo') from None

def store_photo(data_dir, raw):
    data = prepare_photo(raw)
    name = hashlib.sha256(data).hexdigest() + '.webp'
    folder = pathlib.Path(data_dir) / 'printer-photos'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = folder / name
    if not target.exists():
        with target.open('xb') as file:
            file.write(data)
    return name
