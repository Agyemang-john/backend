from django import forms
from product.models import *
from tinymce.widgets import TinyMCE


class ProductReviewForm(forms.ModelForm):
    review = forms.CharField(widget=forms.Textarea(attrs={'placeholder': 'Write review'}))

    class Meta:
        model = ProductReview
        fields = ['review', 'rating']

