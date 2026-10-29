"""Build a zip that moves the recorder to another PC (run with any Python 3 on this PC).

Contents: program source and docs, offline wheels (install\\wheels), 安装.bat and 安装说明.txt at the top.
Not included: Python environments (rebuilt on the target), logs, presets, recordings, audit data, FFmpeg, SDKs.
Output: <camera-sdk-review>\\dist\\多相机录像台_<version>_<date>.zip
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import zipfile

APP = Path(__file__).resolve().parent.parent
TOP = '多相机录像台'
EXCLUDE_DIRS = {'.venv-recorder', '.venv-zed', '__pycache__', 'logs', 'presets', 'ffmpeg', '.git', '.idea', '.vscode', 'build', 'dist'}
EXCLUDE_FILES = {'startup-error.log', 'install-report.txt'}
EXCLUDE_SUFFIXES = {'.pyc', '.lnk', '.tmp', '.partial'}


def included(path):
    relative = path.relative_to(APP)
    if any(part in EXCLUDE_DIRS for part in relative.parts[:-1]):
        return False
    return path.is_file() and path.name not in EXCLUDE_FILES and path.suffix.lower() not in EXCLUDE_SUFFIXES


def main():
    version = re.search(r"APP_VERSION = '([^']+)'", (APP / 'research_data.py').read_text(encoding='utf-8'))[1]
    wheels = sorted((APP / 'install' / 'wheels').glob('*.whl'))
    names = {w.name.split('-')[0].lower() for w in wheels}
    missing = {'pillow', 'numpy', 'cython', 'pyzed'} - names
    if missing:
        raise SystemExit(f'install\\wheels 缺少：{", ".join(sorted(missing))}')
    files = sorted(p for p in APP.rglob('*') if included(p))
    out_dir = APP.parent / 'dist'
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d')
    target = out_dir / f'{TOP}_{version}_{stamp}.zip'
    if target.exists():
        target = out_dir / f'{TOP}_{version}_{datetime.now():%Y%m%d_%H%M%S}.zip'
    manifest = dict(app_version=version, built_at=datetime.now().astimezone().isoformat(), files={})
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            relative = path.relative_to(APP).as_posix()
            archive.write(path, f'{TOP}/mvsdk-python/{relative}')
            manifest['files'][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        archive.write(APP / 'install' / '安装.bat', f'{TOP}/安装.bat')
        archive.write(APP / 'install' / '安装说明.txt', f'{TOP}/安装说明.txt')
        archive.writestr(f'{TOP}/mvsdk-python/install/package-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    size = target.stat().st_size / 1e6
    print(f'已生成 {target}（{size:.1f} MB，{len(files)} 个文件）')
    return target


if __name__ == '__main__':
    main()
