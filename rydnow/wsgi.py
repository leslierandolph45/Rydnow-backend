"""
WSGI config for rydnow project.

It exposes the WSGI callable as a module-level variable named ``application``.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'rydnow.settings')

# ✅ Auto-run migrations on startup (free plan friendly!)
try:
    import django
    from django.core.management import call_command
    django.setup()
    call_command('migrate', '--noinput')
except Exception as e:
    print(f"Migration note: {e}")

application = get_wsgi_application()
