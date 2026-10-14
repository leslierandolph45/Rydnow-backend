from django.db.models.signals import post_migrate
from django.dispatch import receiver
from django.contrib.auth.models import User
import os


@receiver(post_migrate)
def create_default_admin(sender, **kwargs):
    username = os.environ.get('ADMIN_USERNAME', 'admin')
    email = os.environ.get('ADMIN_EMAIL', 'admin@rydnow.com')
    password = os.environ.get('ADMIN_PASSWORD', 'RydnowAdmin2026!')
    
    if not User.objects.filter(username=username).exists():
        User.objects.create_superuser(
            username=username,
            email=email,
            password=password
        )
        print(f"✅ Admin user '{username}' created successfully!")
    else:
        print(f"ℹ️ Admin user '{username}' already exists.")
