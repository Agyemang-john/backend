from datetime import timedelta

from django import forms
from django.contrib import admin
from django.core.cache import cache
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from . import collector
from .models import DailyMetric, Experiment


class ExperimentForm(forms.ModelForm):
    class Meta:
        model = Experiment
        fields = '__all__'

    def clean_variants(self):
        variants = self.cleaned_data['variants']
        if not isinstance(variants, list) or len(variants) < 2:
            raise forms.ValidationError('Give at least two variants, e.g. [{"key": "control", "weight": 50}, {"key": "b", "weight": 50}].')
        for v in variants:
            if not isinstance(v, dict) or not str(v.get('key', '')).replace('_', '').isalnum():
                raise forms.ValidationError('Each variant needs a "key" of letters, digits or _.')
            if int(v.get('weight', 0)) <= 0:
                raise forms.ValidationError('Weights must be positive.')
        return variants


@admin.register(Experiment)
class ExperimentAdmin(admin.ModelAdmin):
    form = ExperimentForm
    list_display = ('key', 'name', 'status', 'goal', 'started_at', 'ended_at')
    list_filter = ('status',)
    readonly_fields = ('results_table',)

    def save_model(self, request, obj, form, change):
        if obj.status == Experiment.RUNNING and not obj.started_at:
            obj.started_at = timezone.now()
        if obj.status == Experiment.STOPPED and not obj.ended_at:
            obj.ended_at = timezone.now()
        super().save_model(request, obj, form, change)
        cache.delete('an:experiments')

    @admin.display(description='Results (conversion = goal reached after first exposure)')
    def results_table(self, obj):
        if not obj.pk:
            return '—'
        rows = collector.results(obj)

        def p(v):
            if v is None:
                return '—'
            verdict = 'significant' if v < 0.05 else 'not significant yet'
            return f"{v:.3f} ({verdict})"
        return format_html(
            '<table><tr><th>Variant</th><th>Exposed</th><th>Converted</th><th>Rate</th><th>Lift vs control</th><th>p-value</th></tr>{}</table>',
            format_html_join('', '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}%</td><td>{}</td><td>{}</td></tr>',
                             ((r['variant'], r['exposed'], r['converted'], r['rate'],
                               f"{r['lift']}%" if r.get('lift') is not None else '—', p(r.get('p_value'))) for r in rows)))


@admin.register(DailyMetric)
class DailyMetricAdmin(admin.ModelAdmin):
    list_display = ('date', 'name', 'experiment', 'variant', 'visitors', 'events')
    list_filter = ('name', 'experiment')
    date_hierarchy = 'date'
    change_list_template = 'admin/analytics/dailymetric/change_list.html'

    def changelist_view(self, request, extra_context=None):
        since = timezone.localdate() - timedelta(days=29)
        totals = {name: 0 for name in collector.FUNNEL}
        for name, visitors in (DailyMetric.objects.filter(date__gte=since, experiment='', name__in=collector.FUNNEL)
                               .values_list('name', 'visitors')):
            totals[name] += visitors
        steps, prev = [], None
        for name in collector.FUNNEL:
            v = totals[name]
            steps.append({'name': name.replace('_', ' '), 'visitors': v,
                          'rate': f"{100 * v / prev:.1f}%" if prev else '—'})
            prev = v or prev
        extra_context = {**(extra_context or {}), 'funnel': steps}
        return super().changelist_view(request, extra_context=extra_context)
