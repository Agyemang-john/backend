from django.apps import AppConfig


class ToolsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tools'
    verbose_name = 'Seller tools'

    def ready(self):
        from django.core.cache import cache
        from django.db.models.signals import post_delete, post_save

        from payments.models import SubscriptionPlan

        def forget_cheapest(**kwargs):
            # Upgrade prompts name the cheapest plan with each tool; plans changed.
            cache.delete('tools:cheapest_plans')

        post_save.connect(forget_cheapest, sender=SubscriptionPlan, dispatch_uid='tools_plan_saved')
        post_delete.connect(forget_cheapest, sender=SubscriptionPlan, dispatch_uid='tools_plan_deleted')

        # Seller API webhooks: "order.updated" whenever a saved order's status changes.
        from django.db.models.signals import post_init

        from order.models import Order

        def remember_status(instance, **kwargs):
            instance._tools_status = instance.__dict__.get('status')

        def status_changed(instance, created, **kwargs):
            before = getattr(instance, '_tools_status', None)
            instance._tools_status = instance.status
            if created or before is None or before == instance.status or not instance.is_ordered:
                return
            from tools.webhooks import order_event_later
            order_event_later(instance.pk, 'order.updated')

        post_init.connect(remember_status, sender=Order, dispatch_uid='tools_order_status_init')
        post_save.connect(status_changed, sender=Order, dispatch_uid='tools_order_status_saved')
