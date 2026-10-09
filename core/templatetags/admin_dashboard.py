from django import template

from core.admin_dashboard import dashboard_for

register = template.Library()


@register.simple_tag(takes_context=True)
def admin_dashboard(context):
    """{% admin_dashboard as dash %} — stats for templates/admin/index.html.
    ?refresh=1 on the admin home recomputes instead of serving the cache."""
    request = context['request']
    return dashboard_for(request.user, refresh=request.GET.get('refresh') == '1')
