"""Set up the multi-camera recorder on a new Windows PC (run by 安装.bat with Python 3.14).

Checks the drivers and SDKs, creates .venv-recorder and .venv-zed, creates desktop shortcuts and runs
offline self-tests. Python packages come from install\\wheels when that folder is present (no internet
needed), otherwise they are downloaded: pinned versions from PyPI, pyzed from Stereolabs. Safe to run
again: existing environments are rebuilt only with --rebuild. A report is written to install\\install-report.txt.
"""
import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request

APP = Path(__file__).resolve().parent.parent          # ...\mvsdk-python
WHEELS = APP / 'install' / 'wheels'
DOWNLOADS = APP / 'install' / 'downloads'
REPORT = APP / 'install' / 'install-report.txt'
# Online installs use the versions the recorder was tested with.
PINNED = dict(pillow='pillow==12.3.0', numpy='numpy==2.5.4', cython='cython==3.3.0')
PYZED_URL = 'https://download.stereolabs.com/zedsdk/5.5/whl/win_amd64/pyzed-5.5-cp{v}-cp{v}-win_amd64.whl'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
lines = []


def say(text=''):
    print(text, flush=True)
    lines.append(text)


def check(label, ok, detail, fix=None):
    say(f'  [{"通过" if ok else "缺少"}] {label}：{detail}')
    if not ok and fix:
        say(f'         处理方法：{fix}')
    return ok


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW, **kwargs)


def find_ffmpeg():
    for folder in (os.environ.get('FFMPEG_DIR'), APP / 'ffmpeg' / 'bin', APP.parent / 'ffmpeg' / 'bin'):
        if folder and (Path(folder) / 'ffmpeg.exe').is_file():
            return Path(folder) / 'ffmpeg.exe'
    found = shutil.which('ffmpeg')
    return Path(found) if found else None


def checks():
    say('一、检查电脑环境')
    results = {}
    results['python'] = check('Python', sys.version_info[:2] == (3, 14) and sys.maxsize > 2**32,
                              f'{sys.version.split()[0]}（{8 * ctypes.sizeof(ctypes.c_void_p)} 位）{sys.executable}',
                              '安装 64 位 Python 3.14，再重新运行 安装.bat')
    smi = shutil.which('nvidia-smi')
    gpu = run([smi, '--query-gpu=name,driver_version', '--format=csv,noheader']).stdout.strip() if smi else ''
    results['gpu'] = check('NVIDIA 显卡与驱动', bool(gpu), gpu or '未找到 nvidia-smi',
                           '安装 NVIDIA 显卡驱动；ZED 和 GPU 编码都需要 NVIDIA 显卡')
    try:
        ctypes.WinDLL('MVCAMSDK_X64')
        mv = True
    except OSError:
        mv = False
    results['mindvision'] = check('MindVision SDK', mv, '已找到 MVCAMSDK_X64.dll' if mv else '未找到 MVCAMSDK_X64.dll',
                                  '安装 MindVision（迈德威视）工业相机 SDK 后重启')
    wrapper = APP / 'mvsdk' / 'mvsdk.py'
    results['mvsdk'] = check('MindVision Python 接口', wrapper.is_file(), str(wrapper) if wrapper.is_file() else '未找到 mvsdk 文件夹',
                             f'把 mvsdk 文件夹（含 mvsdk.py）放到 {APP}')
    zed_root = Path(os.environ.get('ZED_SDK_ROOT_DIR', r'C:\Program Files (x86)\ZED SDK'))
    header = zed_root / 'include' / 'sl' / 'Camera.hpp'
    version = None
    if header.is_file():
        text = header.read_text(encoding='utf-8', errors='replace')
        parts = [re.search(rf'#define ZED_SDK_{k}_VERSION\s+(\d+)', text) for k in ('MAJOR', 'MINOR', 'PATCH')]
        version = '.'.join(m[1] for m in parts if m)
    results['zed'] = check('ZED SDK 5.5', bool(version) and version.startswith('5.5'), f'{version or "未安装"}（{zed_root}）',
                           '安装 ZED SDK 5.5（Windows，CUDA 13，TensorRT 10），装完重启')
    ffmpeg = find_ffmpeg()
    ffprobe = ffmpeg.with_name('ffprobe.exe') if ffmpeg else None
    results['ffmpeg'] = check('FFmpeg', bool(ffmpeg) and ffprobe.is_file(), str(ffmpeg) if ffmpeg else '未找到',
                              '安装 FFmpeg，并把它的 bin 文件夹加入系统 PATH（或放到程序目录的 ffmpeg\\bin）')
    if ffmpeg:
        nvenc = run([str(ffmpeg), '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=320x240:rate=30',
                     '-frames:v', '1', '-c:v', 'h264_nvenc', '-f', 'null', '-'], stdin=subprocess.DEVNULL, timeout=30)
        check('GPU 视频编码（NVENC）', nvenc.returncode == 0, '可用' if nvenc.returncode == 0 else '不可用，录像会改用 CPU 编码（更慢）',
              '确认 NVIDIA 驱动正常、FFmpeg 为带 nvenc 的完整版，笔记本请接电源')
    return results, ffmpeg


def offline():
    return WHEELS.is_dir() and any(WHEELS.glob('*.whl'))


def download_pyzed():
    url = PYZED_URL.format(v=f'{sys.version_info[0]}{sys.version_info[1]}')
    target = DOWNLOADS / url.rsplit('/', 1)[1]
    if not target.is_file():
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        say(f'  下载 {url}')
        partial = target.with_suffix('.partial')
        try:
            with urllib.request.urlopen(url, timeout=60) as response, open(partial, 'wb') as out:
                shutil.copyfileobj(response, out)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f'下载 ZED Python 接口失败：{exc}（检查网络；或确认 ZED SDK 为 5.5、Python 为 3.14）')
        partial.replace(target)
    say(f'  pyzed：{target.name}，sha256 {hashlib.sha256(target.read_bytes()).hexdigest()}')
    return str(target)


def make_venv(name, packages, rebuild):
    venv = APP / name
    python = venv / 'Scripts' / 'python.exe'
    if rebuild and venv.exists():
        shutil.rmtree(venv)
    if not python.is_file():
        result = run([sys.executable, '-m', 'venv', str(venv)])
        if result.returncode:
            raise RuntimeError(f'创建 {name} 失败：{result.stderr[-500:]}')
    if offline():
        source = ['--no-index', '--find-links', str(WHEELS), *packages]
    else:
        source = [download_pyzed() if p == 'pyzed' else PINNED[p] for p in packages]
    result = run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', *source])
    if result.returncode:
        raise RuntimeError(f'{name} 安装依赖失败：{result.stdout[-800:]}{result.stderr[-800:]}')
    say(f'  [完成] {name}：{", ".join(packages)}')
    return python


def shortcut(path, target, arguments, workdir, description):
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK_PATH);"
              "$s.TargetPath=$env:LNK_TARGET;$s.Arguments=$env:LNK_ARGS;$s.WorkingDirectory=$env:LNK_DIR;"
              "$s.Description=$env:LNK_DESC;$s.Save()")
    env = dict(os.environ, LNK_PATH=str(path), LNK_TARGET=str(target), LNK_ARGS=arguments, LNK_DIR=str(workdir), LNK_DESC=description)
    result = run(['powershell', '-NoProfile', '-Command', script], env=env)
    return result.returncode == 0


def desktop():
    result = run(['powershell', '-NoProfile', '-Command', "[Environment]::GetFolderPath('Desktop')"])
    folder = result.stdout.strip()
    return Path(folder) if folder and Path(folder).is_dir() else Path.home() / 'Desktop'


def self_test(recorder, zed_python, results):
    say('四、离线自检（不连接相机）')
    tests = ['test_zed']
    if results['ffmpeg']:
        tests += ['test_process_worker', 'test_export']
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        env['PATH'] = str(ffmpeg.parent) + os.pathsep + env.get('PATH', '')
    result = run([str(recorder), '-B', '-m', 'unittest', *tests], cwd=str(APP), env=env, timeout=900)
    summary = [line for line in result.stderr.splitlines() if line.startswith(('Ran ', 'OK', 'FAILED'))]
    check('程序自检 ' + ' '.join(tests), result.returncode == 0, '；'.join(summary) or result.stderr[-300:],
          '把 install\\install-report.txt 发给维护人员')
    if not results['ffmpeg']:
        say('  （未找到 FFmpeg，跳过 MP4 与导出相关自检）')
    if results['zed']:
        listing = run([str(zed_python), '-B', str(APP / 'zed_worker.py'), '--list'], cwd=str(APP), timeout=60)
        check('ZED Python 接口', listing.returncode == 0 and listing.stdout.strip().startswith('['),
              (listing.stdout.strip() or listing.stderr.strip()[-200:]) + '（列出当前连接的 ZED）',
              'ZED SDK 装完后需要重启电脑')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rebuild', action='store_true', help='delete and recreate both Python environments')
    parser.add_argument('--no-tests', action='store_true')
    parser.add_argument('--no-desktop', action='store_true', help='do not create desktop shortcuts (installation tests)')
    args = parser.parse_args()
    say(f'多相机录像台安装 · 程序位置 {APP}')
    say()
    results, _ = checks()
    if not results['python']:
        raise SystemExit('Python 版本不对，安装中止。')
    say()
    say('二、创建 Python 环境（' + ('使用随包附带的离线安装包）' if offline() else '联网下载：PyPI 与 Stereolabs）'))
    recorder = make_venv('.venv-recorder', ['pillow'], args.rebuild)
    zed_python = make_venv('.venv-zed', ['pillow', 'numpy', 'cython', 'pyzed'], args.rebuild)
    say()
    say('三、创建快捷方式')
    pythonw = recorder.with_name('pythonw.exe')
    launcher = APP / 'launch_recorder.pyw'
    targets = [] if args.no_desktop else [
        (desktop() / '多相机录像台.lnk', f'"{launcher}"', '多相机录像台'),
        (desktop() / '多相机录像台（模拟演示）.lnk', f'"{launcher}" --demo', '多相机录像台模拟演示，不连接相机')]
    targets += [(APP.parent / '多相机录像台.lnk', f'"{launcher}"', '多相机录像台'),
               (APP / '模拟演示.lnk', f'"{launcher}" --demo', '多相机录像台模拟演示，不连接相机')]
    for path, arguments, description in targets:
        check(path.name, shortcut(path, pythonw, arguments, APP, description), str(path))
    say()
    if not args.no_tests:
        self_test(recorder, zed_python, results)
        say()
    missing = [name for name, ok in (('NVIDIA 驱动', results['gpu']), ('MindVision SDK', results['mindvision']),
                                     ('MindVision Python 接口（mvsdk 文件夹）', results['mvsdk']),
                                     ('ZED SDK 5.5', results['zed']), ('FFmpeg', results['ffmpeg'])) if not ok]
    if missing:
        say('安装完成，但还缺：' + '、'.join(missing) + '。按上面的处理方法装好后，再运行一次 安装.bat 检查。')
    else:
        say('全部就绪。双击桌面上的“多相机录像台”即可使用；没接相机时可先打开“模拟演示”。')


if __name__ == '__main__':
    try:
        main()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            say(str(exc.code))
        raise
    except Exception as exc:
        say(f'安装失败：{exc}')
        raise SystemExit(1)
    finally:
        REPORT.write_text('\n'.join(lines) + '\n', encoding='utf-8')
