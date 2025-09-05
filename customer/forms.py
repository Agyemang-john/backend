from django import forms
from userauths.models import Profile

class ProfileForm(forms.ModelForm):
    date_of_birth = forms.DateField(widget=forms.DateInput(attrs={'placeholder': "e.g: 22-11-02", "class":"form-control"}))
    profile_image = forms.FileField(widget=forms.FileInput(attrs={"class":"form-control"}))

    class Meta:
        model = Profile
        fields =  ('date_of_birth','profile_image','country','mobile','address','gender','newsletter_subscription')
    
    def __init__(self, *args, **kwargs):
        super(ProfileForm, self).__init__(*args, **kwargs)
        for field in self.fields:
            self.fields[field].widget.attrs['class'] = 'form-control my-2'