"""PostKaro's single entry point. Always runs inside the project's .venv (made by install.py).

    python run.py open [folder | post.json]   open the page (and add a ClipKaro project to it)
    python run.py status                      accounts, queue, helper
    python run.py check                       check every connected login now
    python run.py service on|off|status       start PostKaro by itself at login (install.py turns it on)
    python run.py serve                       run the page and helper in this terminal (normally automatic)
    python run.py link [folder | post.json]   (for ClipKaro) add a project and print its link

Windows: use  python  instead of  python3 .
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / '.venv'
PY = VENV / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
PYW = VENV / 'Scripts' / 'pythonw.exe'


def find_ffmpeg():
    """ffmpeg makes the thumbnails. Windows: winget puts it on the PATH of NEW terminals only, so find it
    for this one too. macOS: Homebrew's "ffmpeg-full" (the one ClipKaro uses) is kept off the PATH, and
    a login item doesn't see Homebrew's folder at all: add them."""
    import glob
    import shutil
    if sys.platform == 'darwin':
        for d in ('/opt/homebrew/opt/ffmpeg-full/bin', '/usr/local/opt/ffmpeg-full/bin'):
            if os.path.isfile(os.path.join(d, 'ffmpeg')):
                os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')
                return
        if not shutil.which('ffmpeg'):
            for d in ('/opt/homebrew/bin', '/usr/local/bin'):
                if os.path.isfile(os.path.join(d, 'ffmpeg')):
                    os.environ['PATH'] = os.environ.get('PATH', '') + os.pathsep + d
                    return
        return
    if os.name != 'nt' or shutil.which('ffmpeg'):
        return
    local = os.environ.get('LOCALAPPDATA', '')
    places = [os.path.join(local, 'Microsoft', 'WinGet', 'Links'),
              os.path.join(os.environ.get('ProgramFiles', ''), 'WinGet', 'Links')]
    places += glob.glob(os.path.join(local, 'Microsoft', 'WinGet', 'Packages', 'Gyan.FFmpeg*', '*', 'bin'))
    for d in places:
        if os.path.isfile(os.path.join(d, 'ffmpeg.exe')):
            os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')
            return


def utf8_output():
    """Clip titles are often Hindi: on Windows, Python's output and files would otherwise use the old
    Windows code page and stop on the first Hindi letter. The child process gets UTF-8 mode; this one
    (already running) writes its output as UTF-8."""
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass                                  # pythonw (no console): cli.py logs to a file


def in_venv():
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


if __name__ == '__main__':
    find_ffmpeg()
    utf8_output()
    if not in_venv():
        if not PY.exists():
            print('PostKaro is not set up yet. Run:  python install.py   (python3 on macOS/Linux)')
            sys.exit(1)
        sys.exit(subprocess.call([str(PY), str(ROOT / 'run.py'), *sys.argv[1:]]))
    sys.path.insert(0, str(ROOT))
    from postkaro.cli import main
    main(sys.argv[1:])
