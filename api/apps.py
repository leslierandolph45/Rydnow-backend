from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.dispatch import receiver
from django.contrib.auth.models import User
import os




class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'
    verbose_name = 'API'


    def ready(self):
        create_superuser()




def create_superuser():
    username = os.environ.get('DJANGO_ADMIN_USERNAME', 'admin')
    email = os.environ.get('DJANGO_ADMIN_EMAIL', 'admin@example.com')
    password = os.environ.get('DJANGO_ADMIN_PASSWORD', 'Admin@123')


    if not User.objects.filter(username=username).exists():
        User.objects.create_superuser(
            username=username,
            email=email,
            password=password
        )
        print(f"✅ Superuser created: {username}")
    else:
        print(f"ℹ️ Superuser already exists: {username}")
