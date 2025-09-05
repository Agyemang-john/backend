from django.db import models
from userauths.models import User
from django.utils import timezone
import secrets
from vendor.models import *
# from paystack import Paystack

# Create your models here.
class UserWallet(models.Model):
    user = models.OneToOneField(User,null=True ,on_delete=models.CASCADE)
    currency = models.CharField(max_length=50, default='GHS')
    created_at = models.DateTimeField(default=timezone.now, null=True)

    def __str__(self):
        return self.user.__str__()
    
class Payment(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, blank=True, null=True)
    amount = models.PositiveIntegerField()
    ref = models.CharField(max_length=200)
    email = models.EmailField()
    verified = models.BooleanField(default=False)
    date_created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-date_created',)
    
    def __str__(self):
        return f"Payments: {self.amount}"
    
    def save(self, *args, **kwargs):
        while not self.ref:
            ref = secrets.token_urlsafe(50)
            object_with_similar_ref = Payment.objects.filter(ref=ref)
            if not object_with_similar_ref:
                self.ref = ref
        super().save(*args, **kwargs)

class Feature(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name

class Plan(models.Model):
    name = models.CharField(max_length=100)
    price = models.FloatField()
    interval = models.CharField(max_length=20)  # 'monthly' or 'annually'
    paystack_plan_id = models.CharField(max_length=100)  # ID from Paystack
    features = models.ManyToManyField(Feature, related_name='plans')

    def __str__(self):
        return self.name

class Subscription(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, null=True)
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE)
    start_date = models.DateTimeField(auto_now_add=True)
    end_date = models.DateTimeField()
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user.username} - {self.plan.name}"