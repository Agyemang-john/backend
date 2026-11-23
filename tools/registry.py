"""
tools/registry.py
The paid seller tools, in one place. Each tool is switched on per plan by a
field on payments.SubscriptionPlan: a boolean flag, or a monthly allowance
(0 = not included). Everything else reads this list: the permission check
(tools/access.py), the plan summary the Seller Centre uses to show locks and
upgrade prompts, and the Seller tools page.

Adding a tool: add the plan field, then one entry here.
"""
from dataclasses import dataclass

from django.core.cache import cache


@dataclass(frozen=True)
class Tool:
    key: str
    name: str
    summary: str
    path: str                 # Seller Centre page
    flag: str = ''            # boolean plan field
    allowance: str = ''       # monthly-allowance plan field (0 = not included)


TOOLS = [
    Tool('bulk_edit', 'Bulk edit', 'Change prices, stock and cost of many products at once, or by CSV.',
         '/tools/bulk-edit', flag='can_bulk_edit'),
    Tool('stock_forecast', 'Stock forecast', 'Days of stock left for every product, a reorder list and alerts '
         'before you run out.', '/tools/stock', flag='can_use_stock_forecast'),
    Tool('listing_doctor', 'Listing doctor', 'A quality score for every listing and AI rewrites of titles and '
         'descriptions.', '/tools/listing-doctor', allowance='listing_assistant_monthly_limit'),
    Tool('profit_report', 'Profit report', 'Your real profit per product and month after fees, refunds, ads '
         'and your cost price.', '/tools/profit', flag='can_use_profit_report'),
    Tool('price_insights', 'Price insights', 'What other sellers charge for the same item, buy box wins and '
         'category price ranges.', '/tools/pricing', flag='can_use_price_insights'),
    Tool('auto_repricer', 'Auto-repricer', 'Keep prices competitive automatically, never below your minimum.',
         '/tools/pricing', flag='can_use_auto_repricer'),
    Tool('demand_insights', 'What shoppers want', 'What shoppers search for in your categories, including searches '
         'that found nothing: products to add next.', '/tools/demand', flag='can_use_demand_insights'),
    Tool('promotions', 'Promotions autopilot', 'Win back lapsed customers, thank first-time buyers with a coupon, '
         'and run a sale every week, automatically.', '/tools/promotions', flag='can_use_promotions'),
    Tool('broadcasts', 'Messages to followers', 'Tell your followers and past buyers about new arrivals and sales, '
         'in the app and by email.', '/tools/broadcasts', allowance='broadcasts_monthly_limit'),
    Tool('labels', 'Labels & pick lists', 'Print shipping labels, packing slips and a pick list for many orders at '
         'once.', '/tools/labels', flag='can_print_labels'),
    Tool('chat_tools', 'Chat helpers', 'Saved replies, an away message for when you are closed, and how fast you '
         'answer shoppers.', '/tools/chat', flag='can_use_chat_tools'),
    Tool('store_link', 'Store link & page', 'Your own short link and QR code, and a branded store page with an '
         'announcement and featured products.', '/tools/store', flag='can_use_store_link'),
    Tool('api', 'API & stock sync', 'Connect your own systems: keep stock and prices in sync, read orders, and get '
         'webhooks when orders come in.', '/tools/api', flag='can_use_api'),
]
BY_KEY = {t.key: t for t in TOOLS}
TIER_ORDER = ['free', 'basic', 'pro', 'enterprise']


def included(plan, key):
    """True when this plan includes the tool."""
    tool = BY_KEY[key]
    if plan is None:
        return False
    if tool.flag:
        return bool(getattr(plan, tool.flag, False))
    return (getattr(plan, tool.allowance, 0) or 0) > 0


def allowance(plan, key):
    """Monthly allowance of an allowance-based tool (0 = none)."""
    tool = BY_KEY[key]
    return (getattr(plan, tool.allowance, 0) or 0) if (plan and tool.allowance) else 0


def cheapest_plans():
    """{tool key: name of the cheapest public plan that includes it} for upgrade prompts (cached 10 min)."""
    found = cache.get('tools:cheapest_plans')
    if found is not None:
        return found
    from payments.models import SubscriptionPlan
    plans = sorted(SubscriptionPlan.objects.filter(is_active=True),
                   key=lambda p: (TIER_ORDER.index(p.tier) if p.tier in TIER_ORDER else 9, p.price))
    found = {}
    for tool in TOOLS:
        plan = next((p for p in plans if included(p, tool.key)), None)
        found[tool.key] = plan.name if plan else None
    cache.set('tools:cheapest_plans', found, 600)
    return found


def summary(vendor):
    """Every tool with whether this store has it (for locks, upgrade prompts and the tools page)."""
    from payments.entitlements import plan_for_vendor
    from tools.models import ToolUsage

    plan = plan_for_vendor(vendor)
    cheapest = cheapest_plans()
    out = []
    for tool in TOOLS:
        item = {'key': tool.key, 'name': tool.name, 'summary': tool.summary, 'path': tool.path,
                'included': included(plan, tool.key), 'upgrade_plan': cheapest.get(tool.key)}
        if tool.allowance:
            limit = allowance(plan, tool.key)
            item['allowance'] = limit
            item['used'] = ToolUsage.used(vendor, tool.key) if limit else 0
        out.append(item)
    return out
