"""Character library: reusable characters (name, description, picture) shared across films.

A character lives in director/library/<slug>/ :
  character.json    slug, name, description, image (stored filename or None), created
  image.<ext>       the picture, extension chosen from its magic bytes (png, jpg, webp)
Stdlib only.
"""
import json, re, shutil, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / 'library'

SLUG = re.compile(r'[a-z0-9-]+')
MAX_IMAGE = 8 * 1024 * 1024


class LibraryError(Exception):
    pass


# ---------------------------------------------------------------- storage

def safe(slug):
    """ROOT/<slug>, rejecting anything that is not a full slug match before any filesystem access."""
    if not SLUG.fullmatch(slug or ''):
        raise LibraryError('bad character name')
    return ROOT / slug


def image_ext(data):
    """Extension from the magic bytes ('png'/'jpg'/'webp'), never from the filename; '' if not an image."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    return ''


def list_all():
    out = []
    if not ROOT.exists():
        return out
    for f in ROOT.glob('*/character.json'):
        try:
            out.append(json.loads(f.read_text()))
        except ValueError:
            continue
    return sorted(out, key=lambda c: c.get('name', '').lower())


def get(slug):
    f = safe(slug) / 'character.json'
    if not f.is_file():
        raise LibraryError('no such character')
    return json.loads(f.read_text())


def image_path(slug):
    c = get(slug)
    if not c.get('image'):
        return None
    return safe(slug) / c['image']


def delete(slug):
    f = safe(slug)
    if not f.is_dir():
        raise LibraryError('no such character')
    shutil.rmtree(f)
    return {'deleted': slug}


def slugify(name):
    slug = base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40] or 'character'
    n = 2
    while (ROOT / slug).exists():
        slug, n = f'{base}-{n}', n + 1
    return slug


def save(name, description, image_bytes=None, filename=''):
    if not isinstance(name, str) or not isinstance(description, str):
        raise LibraryError('name and description must be strings')
    name, description = name.strip(), description.strip()
    if not 1 <= len(name) <= 60:
        raise LibraryError('name must be 1-60 chars')
    if not 1 <= len(description) <= 1500:
        raise LibraryError('description must be 1-1500 chars')
    ext = ''
    if image_bytes is not None:
        if not isinstance(image_bytes, bytes):
            raise LibraryError('image must be bytes')
        if not image_bytes or len(image_bytes) > MAX_IMAGE:
            raise LibraryError('image empty or over 8 MB')
        ext = image_ext(image_bytes)
        if not ext:
            raise LibraryError('image is not png, jpg or webp')
    slug = slugify(name)
    d = ROOT / slug
    d.mkdir(parents=True)
    try:
        image = None
        if image_bytes is not None:
            (d / f'image.{ext}').write_bytes(image_bytes)
            image = f'image.{ext}'
        character = {'slug': slug, 'name': name, 'description': description,
                     'image': image, 'created': time.time()}
        tmp = d / 'character.json.tmp'
        tmp.write_text(json.dumps(character, indent=1))
        tmp.replace(d / 'character.json')
    except OSError:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return character
