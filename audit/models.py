from django.conf import settings
from django.db import models


class AuditEntry(models.Model):
    """
    One recorded change to a sensitive record (audit/registry.py lists which
    models and fields). Append-only: the admin can read but not edit or delete.
    """
    CREATE, UPDATE, DELETE = 'create', 'update', 'delete'
    ACTION_CHOICES = [(CREATE, 'Created'), (UPDATE, 'Changed'), (DELETE, 'Deleted')]

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    actor_label = models.CharField(max_length=160, help_text="Who, kept even if the user is deleted.")
    via = models.CharField(max_length=16, help_text="admin | api | system")
    action = models.CharField(max_length=6, choices=ACTION_CHOICES)
    model = models.CharField(max_length=60, db_index=True)        # app_label.ModelName
    object_id = models.CharField(max_length=40)
    object_label = models.CharField(max_length=200)
    changes = models.JSONField(default=dict)                         # {field: [old, new]}
    ip = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=['model', 'object_id', '-created_at'], name='audit_object_idx'),
            models.Index(fields=['actor', '-created_at'], name='audit_actor_idx'),
        ]
        verbose_name_plural = 'Audit log'

    def __str__(self):
        return f"{self.actor_label} {self.action} {self.model} {self.object_id}"
