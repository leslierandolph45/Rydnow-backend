from django.contrib import admin, messages
from django.utils.html import format_html

from .models import RiderDocument

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _file_url(obj):
    """Return the file URL, or None when the file is missing from storage."""
    try:
        return obj.file.url if obj.file else None
    except Exception:  # storage not configured / file not on disk
        return None


class RiderDocumentPreviewMixin:
    @admin.display(description="Preview")
    def preview_thumb(self, obj):
        url = _file_url(obj)
        if not url:
            return "No file"
        if url.lower().split("?")[0].endswith(IMAGE_EXTS):
            return format_html(
                '<a href="{0}" target="_blank" rel="noopener">'
                '<img src="{0}" style="height:56px;width:56px;object-fit:cover;border-radius:8px" alt=""></a>',
                url,
            )
        return format_html('<a href="{}" target="_blank" rel="noopener">PDF</a>', url)

    @admin.display(description="Document")
    def preview_large(self, obj):
        url = _file_url(obj)
        if not url:
            return "File not found in storage."
        if url.lower().split("?")[0].endswith(IMAGE_EXTS):
            return format_html(
                '<a href="{0}" target="_blank" rel="noopener">'
                '<img src="{0}" style="max-width:480px;max-height:480px;border-radius:10px" alt=""></a>',
                url,
            )
        return format_html('<a href="{}" target="_blank" rel="noopener">Open PDF</a>', url)

    @admin.display(description="File")
    def open_file(self, obj):
        url = _file_url(obj)
        if not url:
            return "Missing"
        return format_html('<a href="{}" target="_blank" rel="noopener">Open in new tab</a>', url)

    @admin.action(description="Approve selected documents")
    def approve_documents(self, request, queryset):
        updated = queryset.update(status=RiderDocument.DocumentStatus.APPROVED)
        self.message_user(request, f"{updated} document(s) approved.", messages.SUCCESS)
