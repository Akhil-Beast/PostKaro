"""The commands behind run.py."""
import argparse
import json
import shutil
import subprocess
import sys
import webbrowser

from . import __version__, db, kit, paths, service, settings


def open_browser(url):
    """Open a link in the user's browser (xdg-open on Linux, open on macOS)."""
    try:
        if sys.platform.startswith('linux') and shutil.which('xdg-open'):
            subprocess.Popen(['xdg-open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        if sys.platform == 'darwin':
            subprocess.Popen(['open', url])
            return True
        return webbrowser.open(url)
    except OSError:
        return False


def cmd_serve(a):
    if service.running():
        print(f'PostKaro is already running: {paths.base_url()}')
        return
    if sys.stdout is None:                       # pythonw (Windows): no console, log to a file
        sys.stdout = sys.stderr = open(paths.data_dir() / 'postkaro.log', 'a', encoding='utf-8')
    from . import server
    print(f'PostKaro {__version__} on {paths.base_url()}  (Ctrl+C stops it)')
    server.serve()


def _route(path):
    if not path:
        return ''
    bid = kit.import_kit(path)
    return f'post/{bid}'


def cmd_open(a):
    route = _route(a.path)
    if not service.start_background():
        print(f'[X] PostKaro did not start. Run  python run.py serve  to see why '
              f'(log: {paths.data_dir() / "postkaro.log"}).')
        sys.exit(1)
    link = paths.page_link(route)
    if a.no_browser or not open_browser(link):
        print(f'Open this link in your browser:\n{link}')
    else:
        print(f'Opened PostKaro in your browser. Link (only works on this laptop):\n{link}')


def cmd_link(a):
    """Print the link (for ClipKaro): add the project, make sure the helper runs, print one line of JSON."""
    route = _route(a.path)
    ok = service.start_background()
    print(json.dumps({'ok': ok, 'link': paths.page_link(route), 'running': ok}))


def cmd_post_now(a):
    """For ClipKaro's live clipping: add a project and post every clip on every connected platform at once
    (the "Post now" button for the whole batch, without opening the page). Prints one line of JSON."""
    bid = kit.import_kit(a.path)
    if a.dry:
        rows = db.rows("SELECT p.platform, c.no FROM posts p JOIN clips c ON c.id=p.clip_id WHERE c.batch_id=? "
                       "AND c.removed=0 AND p.enabled=1 AND p.state='draft'", (bid,))
        print(json.dumps({'ok': True, 'dry': True, 'batch': bid,
                          'would_post': [{'clip': r['no'], 'platform': r['platform']} for r in rows]}))
        return
    running = service.start_background()
    from . import server
    res = server.schedule(None, {'batch': bid, 'now': True})
    print(json.dumps({'ok': running, 'batch': bid, 'scheduled': res['scheduled'], 'skipped': res['skipped'],
                      'link': paths.page_link('queue')}))


def cmd_status(a):
    s = settings.load()
    print(f'PostKaro {__version__}  ·  {"running" if service.running() else "not running"} on {paths.base_url()}'
          f'  ·  starts at login: {"yes" if service.installed() else "no"}')
    from . import vault
    v = vault.status()
    print(f'Logins kept in: {v["where"]}' + (f'  ({v["problem"]})' if v['problem'] else ''))
    accs = db.accounts()
    if not accs:
        print('No accounts connected yet: python run.py open, then the Accounts page.')
    for x in accs:
        mark = {'ok': 'ok', 'warn': '!', 'broken': 'X'}.get(x['status'], '?')
        print(f"  [{mark}] {settings.NAMES[x['platform']]:<9} {x['name']}{'' if x['active'] else ' (not in use)'}"
              f"{'  - ' + x['problem'] if x['problem'] else ''}")
    counts = {r['state']: r['n'] for r in db.rows('SELECT state, COUNT(*) n FROM posts GROUP BY state')}
    waiting = sum(counts.get(k, 0) for k in db.ACTIVE)
    print(f"Queue: {waiting} waiting, {counts.get('missed', 0)} missed, {counts.get('live', 0)} live, "
          f"{counts.get('failed', 0)} failed  (time zone {s['timezone']})")


def cmd_check(a):
    from . import worker
    for x in db.accounts():
        r = worker.check_account(x)
        print(f"{settings.NAMES[x['platform']]:<9} {x['name']}: {r['status']}{' - ' + r['problem'] if r['problem'] else ''}")


def cmd_service(a):
    if a.action == 'on':
        print(service.install())
    elif a.action == 'off':
        print(service.uninstall())
    else:
        print('starts at login:', 'yes' if service.installed() else 'no', '· running:', service.running())


def main(argv=None):
    p = argparse.ArgumentParser(prog='python run.py', description='PostKaro: post your clips from this laptop.')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('serve', help='run the page and the background helper (normally started by itself)')
    o = sub.add_parser('open', help='open the page (optionally adding a ClipKaro project folder or post.json)')
    o.add_argument('path', nargs='?')
    o.add_argument('--no-browser', action='store_true', help='only print the link')
    lk = sub.add_parser('link', help='add a project and print its link as JSON (used by ClipKaro)')
    lk.add_argument('path', nargs='?')
    pn = sub.add_parser('post-now', help='(for ClipKaro live clipping) add a project and post all its clips now')
    pn.add_argument('path')
    pn.add_argument('--dry', action='store_true', help='only show what would be posted')
    sub.add_parser('status', help='accounts, queue and helper status')
    sub.add_parser('check', help='check every connected login now')
    sv = sub.add_parser('service', help='start PostKaro by itself at login: on / off / status')
    sv.add_argument('action', choices=['on', 'off', 'status'])
    a = p.parse_args(argv)
    try:
        {'serve': cmd_serve, 'open': cmd_open, 'link': cmd_link, 'status': cmd_status, 'check': cmd_check,
         'service': cmd_service, 'post-now': cmd_post_now}[a.cmd](a)
    except ValueError as e:
        print(f'[X] {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()

