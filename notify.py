"""Best-effort notifications when a long batch ends: an optional webhook and the desktop's notify-send. Stdlib only.

Off unless configured: set notify_url (config.json / DIRECTOR_NOTIFY_URL / --notify-url; http/https,
ntfy style: the body is POSTed as text) and/or have `notify-send` on PATH. Failures are swallowed;
a notification must never break or hold up a render batch.
"""
import shutil, subprocess, urllib.request

import config

MAX_BODY = 500


def send(title, body):
    """Send (title, body) to every configured channel; never raises."""
    title, body = str(title), str(body)[:MAX_BODY]
    url = str(config.value('notify_url') or '').strip()
    if url.startswith(('http://', 'https://')):
        try:
            req = urllib.request.Request(url, body.encode('utf-8'), {
                'Title': title.encode('ascii', 'ignore').decode(), 'Content-Type': 'text/plain; charset=utf-8'})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass
    exe = shutil.which('notify-send')
    if exe:
        try:
            subprocess.run([exe, '--app-name=FreeVideo Director', title, body], timeout=5, capture_output=True)
        except Exception:
            pass
