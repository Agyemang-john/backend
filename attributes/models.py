"""
Structured product attributes ("RAM: 8 GB", "Material: Cotton").

Staff define attributes once and attach them to categories or sub-categories;
a product gets the attributes of its sub-category plus those of the
sub-category's parent category. Sellers fill values in; shoppers filter by
them (with live counts), see them as a specs table and compare products.

Values are stored typed (option / number / boolean / text) so filtering and
counting run as indexed SQL rather than string matching on HTML.
"""
from django.db import models
from django.utils.text import slugify


class AttributeDefinition(models.Model):
    CHOICE = 'choice'      # pick one or more from AttributeOption (filterable)
    NUMBER = 'number'      # e.g. 6.5 (inches), range filter
    BOOLEAN = 'boolean'    # yes / no
    TEXT = 'text'          # free text, shown and searchable but not filterable
    TYPE_CHOICES = [(CHOICE, 'Choice'), (NUMBER, 'Number'), (BOOLEAN, 'Yes / No'), (TEXT, 'Text')]

    name = models.CharField(max_length=60)
    slug = models.SlugField(max_length=60, unique=True,
                            help_text="Used in filter URLs (?a_<slug>=...). Lowercase, no spaces.")
    type = models.CharField(max_length=8, choices=TYPE_CHOICES, default=CHOICE)
    unit = models.CharField(max_length=16, blank=True, help_text="Shown after numbers, e.g. GB, cm, W.")
    multi = models.BooleanField(default=False, help_text="Choice only: product may have several values (e.g. features).")
    filterable = models.BooleanField(default=True, help_text="Show as a filter on listing pages.")
    required = models.BooleanField(default=False, help_text="Sellers are asked to fill it in.")
    help_text = models.CharField(max_length=160, blank=True)
    position = models.PositiveSmallIntegerField(default=100, help_text="Lower comes first.")
    categories = models.ManyToManyField('product.Category', blank=True, related_name='attribute_definitions',
                                        help_text="Applies to every sub-category of these categories.")
    sub_categories = models.ManyToManyField('product.Sub_Category', blank=True, related_name='attribute_definitions')

    class Meta:
        ordering = ('position', 'name')

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:60]
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.unit})" if self.unit else self.name


class AttributeOption(models.Model):
    definition = models.ForeignKey(AttributeDefinition, on_delete=models.CASCADE, related_name='options')
    value = models.CharField(max_length=80)
    slug = models.SlugField(max_length=80)
    position = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ('position', 'value')
        constraints = [
            models.UniqueConstraint(fields=['definition', 'slug'], name='uniq_attribute_option_slug'),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.value)[:80] or 'option'
        super().save(*args, **kwargs)

    def __str__(self):
        return self.value


class ProductAttributeValue(models.Model):
    product = models.ForeignKey('product.Product', on_delete=models.CASCADE, related_name='attribute_values')
    definition = models.ForeignKey(AttributeDefinition, on_delete=models.CASCADE, related_name='values')
    option = models.ForeignKey(AttributeOption, on_delete=models.CASCADE, null=True, blank=True, related_name='values')
    value_number = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    value_bool = models.BooleanField(null=True, blank=True)
    value_text = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        constraints = [
            # One row per option for choice attributes, one row otherwise (option NULL).
            models.UniqueConstraint(fields=['product', 'definition', 'option'], nulls_distinct=False,
                                    name='uniq_product_attribute_value'),
        ]
        indexes = [
            # Filters: "products having option X" and facet counts per option.
            models.Index(fields=['option', 'product'], name='attr_value_option_idx'),
            models.Index(fields=['definition', 'value_number', 'product'], name='attr_value_number_idx'),
            models.Index(fields=['definition', 'value_bool', 'product'], name='attr_value_bool_idx'),
        ]

    def display(self):
        d = self.definition
        if d.type == AttributeDefinition.CHOICE:
            return self.option.value if self.option_id else ''
        if d.type == AttributeDefinition.NUMBER:
            if self.value_number is None:
                return ''
            num = self.value_number.normalize()
            text = f"{num:f}"
            return f"{text} {d.unit}".strip()
        if d.type == AttributeDefinition.BOOLEAN:
            return '' if self.value_bool is None else ('Yes' if self.value_bool else 'No')
        return self.value_text
