"""
Product questions and answers (the "Customer questions" block on a product page).

Anyone signed in can ask. The seller's team and shoppers who received the
product can answer. Posts go live at once, except text carrying phone numbers,
links or other off-platform contact (chat/moderation.py), which is refused.
Shoppers can report a post; REPORT_HIDE_THRESHOLD reports hide it until staff look.
"""
from django.conf import settings
from django.db import models

REPORT_HIDE_THRESHOLD = 3


class ProductQuestion(models.Model):
    PUBLISHED = 'published'
    HIDDEN = 'hidden'
    STATUS_CHOICES = [(PUBLISHED, 'Published'), (HIDDEN, 'Hidden')]

    product = models.ForeignKey('product.Product', on_delete=models.CASCADE, related_name='questions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='product_questions')
    text = models.CharField(max_length=300)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PUBLISHED)
    # Denormalised so listing a product's questions needs no COUNT per row.
    answer_count = models.PositiveIntegerField(default=0)
    seller_answered = models.BooleanField(default=False)
    report_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['product', 'status', '-answer_count', '-created_at'], name='qa_q_product_idx'),
            models.Index(fields=['product', 'status', 'seller_answered'], name='qa_q_unanswered_idx'),
        ]

    def __str__(self):
        return self.text[:60]


class ProductAnswer(models.Model):
    PUBLISHED = 'published'
    HIDDEN = 'hidden'
    STATUS_CHOICES = [(PUBLISHED, 'Published'), (HIDDEN, 'Hidden')]

    question = models.ForeignKey(ProductQuestion, on_delete=models.CASCADE, related_name='answers')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='product_answers')
    # Set when answered on behalf of the store from the seller dashboard.
    vendor = models.ForeignKey('vendor.Vendor', on_delete=models.CASCADE, null=True, blank=True, related_name='qa_answers')
    is_verified_buyer = models.BooleanField(default=False)
    text = models.CharField(max_length=1000)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PUBLISHED)
    helpful_count = models.PositiveIntegerField(default=0)
    report_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['question', 'status', '-helpful_count'], name='qa_a_question_idx'),
        ]

    @property
    def is_seller(self):
        return self.vendor_id is not None

    def __str__(self):
        return self.text[:60]


class AnswerVote(models.Model):
    answer = models.ForeignKey(ProductAnswer, on_delete=models.CASCADE, related_name='votes')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['answer', 'user'], name='uniq_answer_vote')]


class QAReport(models.Model):
    REASONS = [
        ('spam', 'Spam or advertising'),
        ('offensive', 'Offensive or abusive'),
        ('off_topic', 'Not about this product'),
        ('wrong', 'Wrong or misleading'),
        ('other', 'Something else'),
    ]
    question = models.ForeignKey(ProductQuestion, on_delete=models.CASCADE, null=True, blank=True, related_name='reports')
    answer = models.ForeignKey(ProductAnswer, on_delete=models.CASCADE, null=True, blank=True, related_name='reports')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    reason = models.CharField(max_length=12, choices=REASONS)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['question', 'user'], condition=models.Q(question__isnull=False),
                                    name='uniq_qa_report_question'),
            models.UniqueConstraint(fields=['answer', 'user'], condition=models.Q(answer__isnull=False),
                                    name='uniq_qa_report_answer'),
            models.CheckConstraint(
                condition=(models.Q(question__isnull=False, answer__isnull=True)
                           | models.Q(question__isnull=True, answer__isnull=False)),
                name='qa_report_one_target'),
        ]
