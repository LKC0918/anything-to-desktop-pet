#!/usr/bin/env python3
"""Read-only pet asset audit and inert Scratch archive extraction.

Requires Python 3.9+. `audit` also requires Pillow. Never modifies input assets,
executes Scratch code, removes backgrounds, or rasterizes SVG costumes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
MAX_MEMBER = 64 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
MAX_FRAMES = 4096
MAX_PIXELS = 40_000_000


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def new_output(path):
    if path.exists() or path.is_symlink():
        raise ValueError(f'Output already exists; choose a new directory: {path}')
    path.mkdir(parents=True, exist_ok=False)
    return path


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def natural_key(path):
    return [(1, int(s)) if s.isdigit() else (0, s.casefold())
            for s in re.split(r'(\d+)', path.name)]


def audit(args):
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError as exc:
        raise ValueError('audit requires Pillow in this Python environment.') from exc
    if args.output.exists() or args.output.is_symlink():
        raise ValueError(f'Output already exists: {args.output}')
    root = args.input.resolve(strict=True)
    paths = sorted((p for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
                   key=natural_key) if root.is_dir() else [root]
    if not paths or any(p.suffix.lower() not in IMAGE_EXTS for p in paths):
        raise ValueError('Provide PNG/JPG/GIF/WebP or a directory containing these files.')
    records, thumbs, warnings = [], [], []
    for path in paths:
        source_hash = sha256(path)
        with Image.open(path) as source:
            frame_count = getattr(source, 'n_frames', 1)
            if len(records) + frame_count > MAX_FRAMES:
                raise ValueError(f'Audit is limited to {MAX_FRAMES} frames; select one sequence.')
            for index in range(frame_count):
                source.seek(index)  # Pillow composites animated GIF disposal frames.
                if source.width * source.height > MAX_PIXELS:
                    raise ValueError(f'Frame exceeds {MAX_PIXELS} pixels: {path}')
                raw_duration = source.info.get('duration')
                original_alpha = 'A' in source.getbands() or 'transparency' in source.info
                rgba = source.convert('RGBA')
                alpha = rgba.getchannel('A')
                lo, hi = alpha.getextrema()
                bbox = alpha.getbbox()
                edge = bool(bbox and (bbox[0] == 0 or bbox[1] == 0 or
                                      bbox[2] == rgba.width or bbox[3] == rgba.height))
                notes = []
                if lo == 255:
                    notes.append('opaque: any visible checkerboard/solid background is baked in; inspect sheet')
                elif hi == 0:
                    notes.append('fully transparent: no visible subject')
                if edge:
                    notes.append('visible pixels touch frame edge: possible clipping; inspect intentionally')
                if frame_count > 1 and (raw_duration is None or raw_duration == 0):
                    notes.append('animation duration is missing or zero; choose timing explicitly')
                records.append({'source': str(path), 'source_sha256': source_hash,
                                'frame_index': index, 'source_frame_count': frame_count,
                                'format': source.format, 'width': rgba.width, 'height': rgba.height,
                                'duration_ms_raw': raw_duration, 'source_has_alpha_channel_or_key': original_alpha,
                                'alpha_range': [lo, hi], 'has_transparent_pixels': lo < 255,
                                'alpha_bbox': list(bbox) if bbox else None,
                                'edge_touch_candidate': edge, 'warnings': notes})
                thumb = ImageOps.contain(rgba, (136, 136), Image.Resampling.LANCZOS)
                thumbs.append(thumb)
    sizes = {(r['width'], r['height']) for r in records}
    if len(sizes) > 1:
        warnings.append('Mixed canvas sizes: preserve source pivots; do not auto-fit each frame independently.')
    warnings.append('A contact sheet does not prove motion quality or alpha edge quality at desktop scale.')
    if len(records) == 1:
        warnings.append('Single still image: no source animation frames were supplied.')
    out = new_output(args.output)
    sheets = []
    for offset in range(0, len(records), 32):
        chunk = records[offset:offset + 32]
        sheet = Image.new('RGB', (4 * 292, math.ceil(len(chunk) / 4) * 188), '#c7c7c7')
        draw = ImageDraw.Draw(sheet)
        for position, row in enumerate(chunk):
            x, y = (position % 4) * 292, (position // 4) * 188
            for dx, color in [(0, '#f6f6f6'), (144, '#242424')]:
                draw.rectangle((x + dx, y, x + dx + 143, y + 143), fill=color)
                thumb = thumbs[offset + position]
                sheet.paste(thumb, (x + dx + (144 - thumb.width) // 2,
                                   y + (144 - thumb.height) // 2), thumb)
            # ASCII labels keep the helper independent of installed CJK fonts.
            label = f"#{offset + position:04d} file={paths.index(Path(row['source'])):03d} frame={row['frame_index']}"
            draw.text((x + 4, y + 147), label, fill='black')
            draw.text((x + 4, y + 163),
                      f"{row['width']}x{row['height']}  ms={row['duration_ms_raw']}  alpha={row['alpha_range']}",
                      fill='black')
        name = f'contact-sheet-{len(sheets) + 1:03d}.png'
        sheet.save(out / name)
        sheets.append(name)
    result = {'mode': 'audit', 'input': str(root), 'file_order': [str(p) for p in paths],
              'frame_count': len(records), 'contact_sheets': sheets, 'warnings': warnings, 'frames': records}
    write_json(out / 'inventory.json', result)
    emit({'output': str(out.resolve()), 'frame_count': len(records), 'contact_sheets': sheets})
    return 0


def safe_member(name):
    path = PurePosixPath(name)
    return bool(name and '\\' not in name and not path.is_absolute() and
                '..' not in path.parts and not re.match(r'^[A-Za-z]:', name))


def member_map(archive):
    infos = archive.infolist()
    if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_TOTAL:
        raise ValueError('Archive exceeds 10000 entries or 512 MiB uncompressed.')
    found, folded = {}, set()
    for info in infos:
        if not safe_member(info.filename):
            raise ValueError(f'Unsafe archive member: {info.filename!r}')
        key = info.filename.casefold()
        if key in folded:
            raise ValueError(f'Duplicate archive member: {info.filename!r}')
        if info.file_size > MAX_MEMBER or info.flag_bits & 1:
            raise ValueError(f'Archive member exceeds 64 MiB or is encrypted: {info.filename!r}')
        folded.add(key)
        found[info.filename] = info
    return found


def targets_of(project):
    if not isinstance(project, dict):
        raise ValueError('Scratch JSON must contain an object.')
    if 'targets' in project:
        targets = project['targets']
    else:
        # Scratch 2 children also contain variable/list monitors, which are not sprites.
        targets = [project] + [c for c in project.get('children', [])
                               if isinstance(c, dict) and 'objName' in c]
    if not isinstance(targets, list) or not all(isinstance(t, dict) for t in targets):
        raise ValueError('Invalid Scratch targets list.')
    return targets


def candidates_for(item, kind, layer='base'):
    if layer == 'text':
        filename, numeric = item.get('textLayerMD5'), item.get('textLayerID')
    elif kind == 'costumes':
        filename = item.get('md5ext') or item.get('baseLayerMD5')
        numeric = item.get('baseLayerID')
    else:
        filename = item.get('md5ext') or item.get('md5')
        numeric = item.get('soundID')
    if not filename and item.get('assetId') and item.get('dataFormat'):
        filename = f"{item['assetId']}.{item['dataFormat']}"
    if filename and (not isinstance(filename, str) or not safe_member(filename)):
        raise ValueError(f'Unsafe Scratch asset reference: {filename!r}')
    result = [filename] if filename else []
    ext = PurePosixPath(filename).suffix if filename else ''
    if isinstance(numeric, int) and numeric >= 0 and ext:
        result.append(f'{numeric}{ext}')
    return result


def scratch(args):
    root = args.archive.resolve(strict=True)
    if args.output and (args.output.exists() or args.output.is_symlink()):
        raise ValueError(f'Output already exists: {args.output}')
    manifest = {'mode': 'scratch', 'archive': str(root), 'archive_sha256': sha256(root),
                'targets': [], 'errors': []}
    payloads = {}
    with zipfile.ZipFile(root) as archive:
        members = member_map(archive)
        json_name = next((name for name in ('project.json', 'sprite.json') if name in members), None)
        if not json_name or members[json_name].file_size > 16 * 1024 * 1024:
            raise ValueError('Missing root project.json/sprite.json, or JSON exceeds 16 MiB.')
        project = json.loads(archive.read(json_name).decode('utf-8-sig'))
        targets = targets_of(project)
        names = [t.get('name', t.get('objName', 'Stage')) for t in targets]
        if args.target is not None and names.count(args.target) != 1:
            raise ValueError(f'--target must match exactly one target. Available names: {names!r}')
        for target_index, (target, name) in enumerate(zip(targets, names)):
            if args.target is not None and name != args.target:
                continue
            result_target = {'target_index': target_index, 'name': name,
                             'is_stage': target.get('isStage', name == 'Stage'),
                             'costumes': [], 'sounds': []}
            for kind in ('costumes', 'sounds'):
                items = target.get(kind, [])
                if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                    raise ValueError(f'Invalid {kind} list for target {name!r}')
                for index, item in enumerate(items):
                    row = {'index': index, 'name': item.get('name', item.get('costumeName', item.get('soundName'))),
                           'md5ext': item.get('md5ext', item.get('baseLayerMD5', item.get('md5'))),
                           'bitmapResolution': item.get('bitmapResolution'),
                           'rotationCenterX': item.get('rotationCenterX'),
                           'rotationCenterY': item.get('rotationCenterY'),
                           'source_metadata': item, 'files': []}
                    layers = ['base', 'text'] if kind == 'costumes' and item.get('textLayerMD5') else ['base']
                    for layer in layers:
                        candidates = candidates_for(item, kind, layer)
                        source = next((p for p in candidates if p in members and not members[p].is_dir()), None)
                        if source is None:
                            manifest['errors'].append({'target': name, 'kind': kind, 'index': index,
                                                       'layer': layer, 'missing_candidates': candidates})
                            continue
                        if source not in payloads:
                            payloads[source] = archive.read(source)
                        data = payloads[source]
                        ext = PurePosixPath(source).suffix.lower()
                        ext = ext if re.fullmatch(r'\.[a-z0-9]{1,8}', ext) else '.bin'
                        destination = f'target-{target_index:03d}/{kind}/{index:04d}-{layer}{ext}'
                        row['files'].append({'archive_member': source, 'output_file': destination,
                                             'source_sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
                    result_target[kind].append(row)
            manifest['targets'].append(result_target)
    if manifest['errors']:
        emit(manifest)
        return 2  # No output directory is created when resources are missing.
    if args.output:
        out = new_output(args.output)
        for target in manifest['targets']:
            for kind in ('costumes', 'sounds'):
                for item in target[kind]:
                    for resource in item['files']:
                        destination = out / resource['output_file']
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(payloads[resource['archive_member']])
        write_json(out / 'inventory.json', manifest)
        emit({'output': str(out.resolve()), 'targets': [t['name'] for t in manifest['targets']],
              'resources': sum(len(i['files']) for t in manifest['targets']
                               for k in ('costumes', 'sounds') for i in t[k])})
    else:
        emit(manifest)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    a = sub.add_parser('audit', help='Audit images and render paired light/dark contact sheets (Pillow).')
    a.add_argument('input', type=Path, help='One image, or a directory of frames (non-recursive, natural order).')
    a.add_argument('--output', type=Path, required=True, help='New directory for inventory.json and sheets.')
    a.set_defaults(function=audit)
    s = sub.add_parser('scratch', help='Inventory/extract inert Scratch .sb2/.sb3/.sprite2/.sprite3 media.')
    s.add_argument('archive', type=Path)
    s.add_argument('--target', help='Exact unique sprite/stage name; omit to include all targets.')
    s.add_argument('--output', type=Path, help='New directory; omit to print inventory without extraction.')
    s.set_defaults(function=scratch)
    args = parser.parse_args()
    try:
        return args.function(args)
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
