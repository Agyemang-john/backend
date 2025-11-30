# forms.py
from django import forms

class ContactReplyForm(forms.Form):
    message = forms.CharField(
        label="Send a Reply to Customer",
        widget=forms.Textarea(attrs={
            "rows": 6,
            "style": "width: 90%; font-size: 14px; padding: 10px;",
            "placeholder": "Type your reply here...",
        }),
        help_text="This message will be emailed to the customer and saved as an admin note."
    )
