from django import forms
from vendor.models import Vendor, OpeningHour
from userauths.validators import allow_only_images_validator
from django import forms
from bootstrap_datepicker_plus.widgets import DateTimePickerInput
from django import forms
from product.models import Product, ProductImages, Variants, VariantImage




class VendorForm(forms.ModelForm):
    name = forms.CharField(widget=forms.TextInput(attrs={'placeholder': 'Store name'}))
    license = forms.FileField(widget=forms.FileInput(attrs={'placeholder': 'license', 'style': 'padding-top: 0rem; line-height: 3.2;'}), validators=[allow_only_images_validator])

    class Meta:
        model = Vendor
        fields = ['name','contact','license','email']

    def __init__(self, *args, **kwargs):
        super(VendorForm, self).__init__(*args, **kwargs)
        for field in self.fields:
            self.fields[field].widget.attrs['class'] = 'form-control my-2'



class OpeningHourForm(forms.ModelForm):
    class Meta:
        model = OpeningHour
        fields = ['day', 'from_hour', 'to_hour', 'is_closed']

    def __init__(self, *args, **kwargs):
        super(OpeningHourForm, self).__init__(*args, **kwargs)
        self.fields['day'].widget.attrs['class'] = 'form-control'
        self.fields['from_hour'].widget.attrs['class'] = 'form-control'
        self.fields['to_hour'].widget.attrs['class'] = 'form-control'
        self.fields['is_closed'].widget.attrs['class'] = 'form-check-input'


class AddProductForm(forms.ModelForm):
    title = forms.CharField(widget=forms.TextInput(attrs={'placeholder': "Product Title", "class":"form-control"}))
    description = forms.CharField(widget=forms.Textarea(attrs={'placeholder': "Product Description", "class":"form-control"}))
    price = forms.CharField(widget=forms.NumberInput(attrs={'placeholder': "Sale Price", "class":"form-control"}))
    old_price = forms.CharField(widget=forms.NumberInput(attrs={'placeholder': "Old Price", "class":"form-control"}))
    type = forms.CharField(widget=forms.TextInput(attrs={'placeholder': "Type of product e.g organic cream", "class":"form-control"}))
    total_quantity = forms.CharField(widget=forms.NumberInput(attrs={'placeholder': "How many are in stock?", "class":"form-control"}))
    life = forms.CharField(widget=forms.TextInput(attrs={'placeholder': "How long would this product live?", "class":"form-control"}))
    mfd = forms.DateTimeField(widget=forms.DateTimeInput(attrs={'placeholder': "e.g: 22-11-02", "class":"form-control"}))
    tags = forms.CharField(widget=forms.TextInput(attrs={'placeholder': "Tags", "class":"form-control"}))
    image = forms.ImageField(widget=forms.FileInput(attrs={"class":"form-control"}))

    class Meta:
        model = Product
        fields = [
            'title',
            'image',
            'description',
            'price',
            'old_price',
            'specifications',
            'total_quantity',
            'life',
            'mfd',
            'tags',
            'sub_category',
        ]

        widgets = {
        'mdf': DateTimePickerInput
    }
        

class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            'title',
            'sub_category',
            'vendor',
            'variant',
            'status', 
            'image', 
            'video', 
            'price', 
            'old_price', 
            'description', 
            'specifications', 
            'brand', 
            'total_quantity', 
            'weight', 
            'volume', 
            'life', 
            'mfd', 
        ]

class ProductImagesForm(forms.ModelForm):
    class Meta:
        model = ProductImages
        fields = ['title', 'images']

class VariantsForm(forms.ModelForm):
    class Meta:
        model = Variants
        fields = ['title', 'size', 'color', 'image', 'quantity', 'price']

class VariantImageForm(forms.ModelForm):
    class Meta:
        model = VariantImage
        fields = ['images']