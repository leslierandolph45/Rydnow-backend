from django.contrib.auth import get_user_model
from django.db.models.signals import post_migrate
from django.dispatch import receiver
from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'

    def ready(self):
        from .dispatch import connect_signals
        connect_signals()


@receiver(post_migrate)
def create_default_admin(sender, **kwargs):
    User = get_user_model()
    username = 'rydnow_admin'
    email = 'admin@rydnow.com'
    password = 'admin12377'  # ⚠️ CHANGE THIS to YOUR password!
    
    if not User.objects.filter(username=username).exists():
        User.objects.create_superuser(
            username=username,
            email=email,
            password=password
        )
        print(f"✅ Admin user '{username}' created successfully!")
    else:
        print(f"ℹ️ Admin user '{username}' already exists")
