# Copyright (c) 2016 Ansible, Inc.
# All Rights Reserved.

# Django
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ObjectDoesNotExist

# Django REST Framework
from rest_framework import serializers

# AWX
from awx.main.models import Credential

__all__ = ['BooleanNullField', 'CharNullField', 'ChoiceNullField', 'VerbatimField']


class NullFieldMixin(object):
    """
    Mixin to prevent shortcutting validation when we want to allow null input,
    but coerce the resulting value to another type.
    """

    def validate_empty_values(self, data):
        (is_empty_value, data) = super(NullFieldMixin, self).validate_empty_values(data)
        if is_empty_value and data is None:
            return (False, data)
        return (is_empty_value, data)


class BooleanNullField(NullFieldMixin, serializers.BooleanField):
    """
    Custom boolean field that allows null and empty string as False values.
    """

    def __init__(self, **kwargs):
        kwargs['allow_null'] = True
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        return bool(super().to_internal_value(data))


class CharNullField(NullFieldMixin, serializers.CharField):
    """
    Custom char field that allows null as input and coerces to an empty string.
    """

    def __init__(self, **kwargs):
        kwargs['allow_null'] = True
        super(CharNullField, self).__init__(**kwargs)

    def to_internal_value(self, data):
        return super(CharNullField, self).to_internal_value(data or '')


class ChoiceNullField(NullFieldMixin, serializers.ChoiceField):
    """
    Custom choice field that allows null as input and coerces to an empty string.
    """

    def __init__(self, **kwargs):
        kwargs['allow_null'] = True
        super(ChoiceNullField, self).__init__(**kwargs)

    def to_internal_value(self, data):
        return super(ChoiceNullField, self).to_internal_value(data or '')


class VerbatimField(serializers.Field):
    """
    Custom field that passes the value through without changes.
    """

    def to_internal_value(self, data):
        return data

    def to_representation(self, value):
        return value


class DeprecatedCredentialField(serializers.IntegerField):
    def __init__(self, **kwargs):
        kwargs['allow_null'] = True
        kwargs['default'] = None
        kwargs['min_value'] = 1
        kwargs.setdefault('help_text', 'This resource has been deprecated and will be removed in a future release')
        super(DeprecatedCredentialField, self).__init__(**kwargs)

    def to_internal_value(self, pk):
        try:
            pk = int(pk)
        except ValueError:
            self.fail('invalid')
        try:
            Credential.objects.get(pk=pk)
        except ObjectDoesNotExist:
            raise serializers.ValidationError(_('Credential {} does not exist').format(pk))
        return pk
