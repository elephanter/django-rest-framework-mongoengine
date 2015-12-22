from collections import OrderedDict

from django.utils import six
from django.utils.encoding import smart_text
from django.utils.translation import ugettext_lazy as _

from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.fields import empty

from bson import ObjectId, DBRef
from bson.errors import InvalidId

from mongoengine.base import get_document
from mongoengine.base.common import _document_registry
from mongoengine.errors import ValidationError as MongoValidationError, NotRegistered

from mongoengine.queryset import QuerySet, QuerySetManager
from mongoengine import EmbeddedDocument, Document, fields as me_fields
from mongoengine.errors import DoesNotExist


class ObjectIdField(serializers.Field):
    """ Field for ObjectId values """

    def to_internal_value(self, value):
        try:
            return ObjectId(smart_text(value))
        except InvalidId:
            raise serializers.ValidationError("'%s' is not a valid ObjectId" % value)

    def to_representation(self, value):
        return smart_text(value)


class DocumentField(serializers.Field):
    """ Replacement of DRF ModelField.

    Keeps track of underlying mognoengine field.

    Used by DocumentSerializers to map unknown fields.

    NB: This is not DocumentField from previous releases. For previous behaviour see GenericField
    """

    def __init__(self, model_field, **kwargs):
        self.model_field = model_field
        super(DocumentField, self).__init__(**kwargs)

    def get_attribute(self, obj):
        return obj

    def to_internal_value(self, data):
        """ convert input to python value.

        Uses mongoengine field's ``to_python()``.
        """
        return self.model_field.to_python(data)

    def to_representation(self, obj):
        """ convert value to representation.

        DRF ModelField uses ``value_to_string`` for this purpose. Mongoengine fields do not have such method.

        This implementation uses ``django.utils.encoding.smart_text`` to convert everything to text, while keeping json-safe types intact.

        NB: The argument is whole object, instead of attribute value. This is upstream feature.
        Probably because the field can be represented by a complicated method with nontrivial way to extract data.
        """
        value = self.model_field.__get__(obj, None)
        return smart_text(value, strings_only=True)

    def run_validators(self, value):
        """ validate value.

        Uses mongoengine field's ``validate()``
        """
        try:
            self.model_field.validate(value)
        except MongoValidationError as e:
            raise ValidationError(e.message)
        super(DocumentField, self).run_validators(value)


class GenericEmbeddedField(serializers.Field):
    """ Field for generic embedded documents.

    Serializes like DictField with additional item ``_cls``.
    """
    default_error_messages = {
        'not_a_dict': serializers.DictField.default_error_messages['not_a_dict'],
        'not_a_doc': _('Expected an EmbeddedDocument but got type "{input_type}".'),
        'undefined_model': _('Document `{doc_cls}` has not been defined.'),
        'missing_class': _('Provided data has not `_cls` item.')
    }

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            self.fail('not_a_dict', input_type=type(data).__name__)
        try:
            doc_name = data['_cls']
            doc_cls = get_document(doc_name)
        except KeyError:
            self.fail('missing_class')
        except NotRegistered:
            self.fail('undefined_model', doc_cls=doc_name)

        return doc_cls(**data)

    def to_representation(self, doc):
        if not isinstance(doc, EmbeddedDocument):
            self.fail('not_a_doc', input_type=type(doc).__name__)
        data = { '_cls': doc.__class__.__name__}
        for field_name in doc._fields:
            if not hasattr(doc, field_name):
                continue
            data[field_name] = getattr(doc, field_name)
        return data


class GenericField(serializers.Field):
    """ Field for generic values.

    Recursively traverses lists and dicts.
    Primitive values are serialized using ``django.utils.encoding.smart_text`` (keeping json-safe intact).
    Embedded documents handled using temporary GenericEmbeddedField.

    No validation performed.

    Note: it will not work properly if a value contains some complex elements.
    """
    embedded_doc_field = GenericEmbeddedField

    def to_representation(self, value):
        return self.represent_data(value)

    def represent_data(self, data):
        if isinstance(data, EmbeddedDocument):
            field = GenericEmbeddedField()
            return field.to_representation(data)
        elif isinstance(data, dict):
            return dict([(key, self.represent_data(val)) for key, val in data.items()])
        elif isinstance(data, list):
            return [self.represent_data(value) for value in data]
        elif data is None:
            return None
        else:
            return smart_text(data, strings_only=True)

    def to_internal_value(self, value):
        return self.parse_data(value)

    def parse_data(self, data):
        if isinstance(data, dict):
            if '_cls' in data:
                field = GenericEmbeddedField()
                return field.to_internal_value(data)
            else:
                return dict([(key, self.parse_data(val)) for key, val in data.items()])
        elif isinstance(data, list):
            return [self.parse_data(value) for value in data]
        else:
            return data


class AttributedDocumentField(DocumentField):
    def get_attribute(self, instance):
        return serializers.Field.get_attribute(self, instance)


class GenericEmbeddedDocumentField(GenericEmbeddedField, AttributedDocumentField):
    """ Field for GenericEmbeddedDocumentField.

    Used internally by ``DocumentSerializer``.
    """
    pass


class DynamicField(GenericField, AttributedDocumentField):
    """ Field for DynamicDocuments.

    Used internally by ``DynamicDocumentSerializer``.
    """
    pass


class ReferenceField(serializers.Field):
    """ Field for References.

    Should be specified with ``model`` or ``queryset`` argument pointing to referenced model.

    Internal value: DBRef.

    Representation: ``str(id)``, or ``{ _id: str(id) }`` (for compatibility with GenericReference).

    Validation checks existance of referenced object.
    """
    default_error_messages = {
        'invalid_input': _('Invalid input. Expected `str` or `{ _id: str }`.'),
        'invalid_id': _('Cannot parse "{pk_value}" as {pk_type}.'),
        'not_found': _('Document with id={pk_value} does not exist.'),
    }
    queryset = None

    pk_field_class = ObjectIdField
    """ Serializer field class used to handle object ids. Override it in derived class if you have other type of ids."""

    def __init__(self, model=None, queryset=None, **kwargs):
        if model is not None:
            self.queryset = model.objects
        elif queryset is not None:
            self.queryset = queryset
        else:
            self.queryset = None

        self.pk_field = self.pk_field_class()

        assert self.queryset is not None or kwargs.get('read_only', None), (
            'Reference field must provide a `queryset` or `model` argument, '
            'or set read_only=`True`.'
        )
        super(ReferenceField,self).__init__(**kwargs)

    def run_validation(self, data=empty):
        # We force empty strings to None values for relational fields.
        if data == '':
            data = None
        return super(ReferenceField,self).run_validation(data)

    def get_queryset(self):
        queryset = self.queryset
        if isinstance(queryset, (QuerySet, QuerySetManager)):
            queryset = queryset.all()
        return queryset

    @property
    def choices(self):
        queryset = self.get_queryset()
        if queryset is None:
            # Ensure that field.choices returns something sensible
            # even when accessed with a read-only field.
            return {}

        return OrderedDict([
            (
                six.text_type(self.to_representation(item)),
                self.display_value(item)
            )
            for item in queryset
        ])

    @property
    def grouped_choices(self):
        return self.choices

    def display_value(self, instance):
        return six.text_type(instance)

    def parse_id(self,value):
        try:
            return self.pk_field.to_internal_value(value)
        except:
            self.fail('invalid_id', pk_value=value, pk_type=self.pk_field_class.__name__)

    def to_internal_value(self, value):
        if isinstance(value, dict):
            try:
                doc_id = self.parse_id(value['_id'])
            except KeyError:
                self.fail('invalid_input')
        else:
            doc_id = self.parse_id(value)

        try:
            return self.get_queryset().only('id').get(id=doc_id).to_dbref()
        except DoesNotExist:
            self.fail('not_found', pk_value=doc_id)

    def to_representation(self, value):
        assert isinstance(value, (Document, DBRef))
        doc_id = value.id
        return self.pk_field.to_representation(doc_id)


class GenericReferenceField(serializers.Field):
    """ Field for GenericReferences.

    Internal value: Document, retrieved with only id field. The mongengine does not support DBRef here.

    Representation: ``{ _cls: str, _id: str }``.

    Validation checks existance of given class and existance of referenced model.
    """

    pk_field_class = ObjectIdField
    """ Serializer field class used to handle object ids. Override it in derived class if you have other type of ids."""

    default_error_messages = {
        'not_a_dict': serializers.DictField.default_error_messages['not_a_dict'],
        'missing_items': _('Expected a dict with `_cls` and `_id` items.'),
        'invalid_id': _('Cannot parse "{pk_value}" as {pk_type}.'),
        'undefined_model': _('Document `{doc_cls}` has not been defined.'),
        'undefined_collecion': _('No document defined for collection `{collection}`.'),
        'not_found': _('Document with id={pk_value} does not exist.'),
    }

    def __init__(self, **kwargs):
        self.pk_field = self.pk_field_class()
        super(GenericReferenceField,self).__init__(**kwargs)

    def parse_id(self,value):
        try:
            return self.pk_field.to_internal_value(value)
        except:
            self.fail('invalid_id', pk_value=repr(value), pk_type=self.pk_field_class.__name__)

    def to_internal_value(self, value):
        if not isinstance(value, dict):
            self.fail('not_a_dict', input_type=type(value).__name__)
        try:
            doc_name = value['_cls']
            doc_id = value['_id']
        except KeyError:
            self.fail('missing_items')
        try:
            doc_cls = get_document(doc_name)
        except NotRegistered:
            self.fail('undefined_model', doc_cls = doc_name)

        try:
            doc_id = self.pk_field.to_internal_value(doc_id)
        except:
            self.fail('invalid_id', pk_value=repr(doc_id), pk_type=self.pk_field_class.__name__)

        try:
            return doc_cls.objects.only('id').get(id=doc_id)
        except DoesNotExist:
            self.fail('not_found', pk_value=doc_id)


    def to_representation(self, value):
        assert isinstance(value, (Document, DBRef))
        if isinstance(value, Document):
            doc_id = value.id
            doc_cls = value.__class__.__name__
        if isinstance(value, DBRef): # hard case
            doc_id = value.id
            doc_collection = value.collection
            class_match = [ k for k,v in _document_registry.items() if v._get_collection_name() == doc_collection ]
            if len(class_match) != 1:
                self.fail('unmapped_collection', collection=doc_collection)
            doc_cls = class_match[0]
        return { '_cls': doc_cls, '_id': self.pk_field.to_representation(doc_id) }


class MongoValidatingField(object):
    # uses attribute mongo_field to validate value
    mongo_field = me_fields.BaseField

    def run_validators(self, value):
        try:
            self.mongo_field().validate(value)
        except MongoValidationError as e:
            raise ValidationError(e.message)
        super(MongoValidatingField, self).run_validators(value)


class GeoPointField(MongoValidatingField, serializers.Field):
    """ Field for 2D point values.

    Internal value and representation: ``[ x, y ]``

    Validation is delegated to mongoengine field.
    """
    default_error_messages = {
        'not_a_list': _("Points must be a list of coordinates, instead got {input_value}."),
        'not_2d': _("Point value must be a two-dimensional coordinates, instead got {input_value}."),
        'not_float': _("Point coordinates must be float or int values, instead got {input_value}."),
    }

    mongo_field = me_fields.GeoPointField

    def to_internal_value(self, value):
        if not isinstance(value, list):
            self.fail('not_a_list', input_value=repr(value))
        if len(value) != 2:
            self.fail('not_2d', input_value=repr(value))
        try:
            return [ float(value[0]), float(value[1]) ]
        except ValueError:
            self.fail('not_float', input_value=repr(value))

    def to_representation(self, value):
        return list(value)


class GeoJSONField(MongoValidatingField, serializers.Field):
    """ Field for GeoJSON values.

    Shouldbe specified with argument ``geo_type`` referencing to GeoJSON geometry type ('Point', 'LineSting', etc)

    Internal value: ``[ coordinates ]`` (as required by mongoengine fields).

    Representation: ``{ 'type': str, 'coordinates': [ coords ] }`` (GeoJSON geometry format).

    Validation: delegated to corresponding mongoengine field.
    """

    default_error_messages = {
        'invalid_type': _("Geometry must be a geojson geometry or a geojson coordinates, got {input_value}."),
        'invalid_geotype': _("Geometry expected to be '{exp_type}', got {geo_type}."),
    }
    valid_geo_types = {
        'Point': me_fields.PointField,
        'LineString': me_fields.LineStringField,
        'Polygon': me_fields.PolygonField,
        'MultiPoint': me_fields.MultiPointField,
        'MultiLineString': me_fields.MultiLineStringField,
        'MultiPolygon': me_fields.MultiPolygonField
    }

    def __init__(self, geo_type, *args, **kwargs):
        assert geo_type in self.valid_geo_types
        self.mongo_field = self.valid_geo_types[geo_type]
        super(GeoJSONField, self).__init__(*args, **kwargs)

    def to_internal_value(self, value):
        if isinstance(value, list):
            return value
        if not isinstance(value, dict) or 'coordinates' not in value or 'type' not in value:
            self.fail('invalid_type', input_value=repr(value))
        if value['type'] != self.mongo_field._type:
            self.fail('invalid_geotype', geo_type=repr(value['type']), exp_type=self.mongo_field._type)
        return value['coordinates']

    def to_representation(self, value):
        return { 'type': self.mongo_field._type, 'coordinates': value}
