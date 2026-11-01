from django.contrib import admin

from .models import ProductAnswer, ProductQuestion, QAReport


@admin.action(description="Publish selected")
def publish(modeladmin, request, queryset):
    queryset.update(status='published', report_count=0)


@admin.action(description="Hide selected")
def hide(modeladmin, request, queryset):
    queryset.update(status='hidden')


class AnswerInline(admin.TabularInline):
    model = ProductAnswer
    extra = 0
    fields = ('text', 'user', 'vendor', 'is_verified_buyer', 'status', 'helpful_count', 'report_count')
    raw_id_fields = ('user', 'vendor')


@admin.register(ProductQuestion)
class ProductQuestionAdmin(admin.ModelAdmin):
    list_display = ('id', 'text', 'product', 'user', 'status', 'answer_count', 'seller_answered', 'report_count', 'created_at')
    list_filter = ('status', 'seller_answered')
    search_fields = ('text', 'product__title', 'user__email')
    raw_id_fields = ('product', 'user')
    actions = [publish, hide]
    inlines = [AnswerInline]
    ordering = ('-report_count', '-created_at')


@admin.register(ProductAnswer)
class ProductAnswerAdmin(admin.ModelAdmin):
    list_display = ('id', 'text', 'question', 'user', 'vendor', 'status', 'helpful_count', 'report_count', 'created_at')
    list_filter = ('status', 'is_verified_buyer')
    search_fields = ('text', 'user__email')
    raw_id_fields = ('question', 'user', 'vendor')
    actions = [publish, hide]
    ordering = ('-report_count', '-created_at')


@admin.register(QAReport)
class QAReportAdmin(admin.ModelAdmin):
    list_display = ('id', 'reason', 'question', 'answer', 'user', 'created_at')
    list_filter = ('reason',)
    raw_id_fields = ('question', 'answer', 'user')
