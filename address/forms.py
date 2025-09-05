# from django import forms

# from address.models import Address, Region, Town, Location

# class AddressForm(forms.ModelForm):
#     class Meta:
#         model = Address
#         fields =  ('first_name','last_name','mobile','email','address','status')
#         widgets = {
#             'first_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'First name'}),
#             'last_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Last name'}),
#             'mobile': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Mobile'}),
#             'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'Email'}),
#             'address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Address'}),
#             'status': forms.CheckboxInput(attrs={'class': 'form-check-input', 'placeholder': 'Make this default address'})
#         }

# class LocationForm(forms.ModelForm):
#     class Meta:
#         model = Location
#         fields =  ('country','region','town')

    
#     #Region
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         self.fields['region'].queryset = Region.objects.none()
#         for field in self.fields:
#             self.fields[field].widget.attrs.update({'class': 'form-control mt-2 w-100', 'placeholder': 'Region'})
        

#         if 'country' in self.data:
#             try:
#                 country_id = int(self.data.get('country'))
#                 self.fields['region'].queryset = Region.objects.filter(country_id=country_id).order_by('name')
#             except (ValueError, TypeError):
#                 pass  # invalid input from the client; ignore and fallback to empty Region queryset
#         elif self.instance.pk:
#             self.fields['region'].queryset = self.instance.country.region_set.order_by('name')

#     #Town
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         self.fields['town'].queryset = Region.objects.none()
#         for field in self.fields:
#             self.fields[field].widget.attrs['class'] = 'form-control mt-2 w-100'

#         if 'region' in self.data:
#             try:
#                 region_id = int(self.data.get('region'))
#                 self.fields['town'].queryset = Town.objects.filter(region_id=region_id).order_by('name')
#             except (ValueError, TypeError):
#                 pass  # invalid input from the client; ignore and fallback to empty Region queryset
#         elif self.instance.pk:
#             self.fields['town'].queryset = self.instance.region.town_set.order_by('name')


