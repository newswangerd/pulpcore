import re
from urllib.parse import urljoin

from django.core.exceptions import FieldDoesNotExist
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.plumbing import (
    build_basic_type,
    build_parameter_type,
    force_instance,
    resolve_regex_path_parameter,
)
from rest_framework.schemas.utils import get_pk_description
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter
from rest_framework import serializers
from rest_framework.generics import GenericAPIView
from rest_framework.views import APIView

from pulpcore.app.models import RepositoryVersion


class PulpAutoSchema(AutoSchema):
    """Pulp Auto Schema."""

    method_mapping = {
        "get": "read",
        "post": "create",
        "put": "update",
        "patch": "partial_update",
        "delete": "delete",
    }

    def get_tags(self):
        """Generate tags."""
        tokenized_path = []
        pulp_tag_name = getattr(self.view, "pulp_tag_name", False)
        if pulp_tag_name:
            return [pulp_tag_name]
        if getattr(self.view, "parent_viewset", None):
            tokenized_path.extend(self.view.parent_viewset.endpoint_pieces())
        if getattr(self.view, "endpoint_pieces", None):
            tokenized_path.extend(self.view.endpoint_pieces())
        if not tokenized_path:
            tokenized_path = self._tokenize_path()
            if not tokenized_path and getattr(self.view, "get_view_name", None):
                tokenized_path.extend(self.view.get_view_name().split())

        subpath = "/".join(tokenized_path)
        operation_keys = subpath.replace("pulp/api/v3/", "").split("/")
        operation_keys = [i.title() for i in operation_keys]
        tags = operation_keys
        if len(operation_keys) > 2:
            del operation_keys[-2]
        if len(operation_keys) > 1:
            operation_keys[0] = "{key}:".format(key=operation_keys[0])
        tags = [" ".join(operation_keys)]

        return tags

    def _get_serializer_name(self, serializer, direction):
        """
        Get serializer name.
        """
        name = super()._get_serializer_name(serializer, direction)
        if direction == "request":
            name = name[:-7]
        elif direction == "response" and "Response" not in name:
            name = name + "Response"
        return name

    def map_parsers(self):
        """
        Get request parsers.
        """
        parsers = super().map_parsers()
        serializer = force_instance(self.get_request_serializer())
        for field_name, field in getattr(serializer, "fields", {}).items():
            if isinstance(field, serializers.FileField) and self.method in ("PUT", "PATCH", "POST"):
                return ["multipart/form-data", "application/x-www-form-urlencoded"]
        return parsers

    def _get_request_body(self):
        """Get request body."""
        request_body = super()._get_request_body()
        if request_body:
            request_body["required"] = True
        return request_body

    def _resolve_path_parameters(self, variables):
        """Resolve path parameters."""
        model = getattr(getattr(self.view, "queryset", None), "model", None)
        parameters = []

        for variable in variables:
            schema = build_basic_type(OpenApiTypes.STR)
            description = ""

            resolved_parameter = resolve_regex_path_parameter(
                self.path_regex, variable, self.map_renderers("format"),
            )

            if resolved_parameter:
                schema = resolved_parameter["schema"]
            elif model:
                try:
                    model_field = model._meta.get_field(variable)
                    schema = self._map_model_field(model_field, direction=None)
                    # strip irrelevant meta data
                    irrelevant_field_meta = ["readOnly", "writeOnly", "nullable", "default"]
                    schema = {k: v for k, v in schema.items() if k not in irrelevant_field_meta}
                    if "description" not in schema and model_field.primary_key:
                        description = get_pk_description(model, model_field)
                except FieldDoesNotExist:
                    pass

            parameters.append(
                build_parameter_type(
                    name=variable,
                    location=OpenApiParameter.PATH,
                    description=description,
                    schema=schema,
                )
            )

        return parameters

    def _map_serializer_field(self, field, direction):
        """Map serializer field."""
        mapped = super()._map_serializer_field(field, direction)
        if "additionalProperties" in str(mapped):
            del mapped["additionalProperties"]
        return mapped


class PulpSchemaGenerator(SchemaGenerator):
    """Pulp Schema Generator."""

    @staticmethod
    def get_parameter_slug_from_model(model, prefix):
        """Returns a path parameter name for the resource associated with the model.
        Args:
            model (django.db.models.Model): The model for which a path parameter name is needed
            prefix (str): Optional prefix to add to the slug
        Returns:
            str: *pulp_href where * is the model name in all lower case letters
        """
        slug = "%s_href" % "_".join(
            [part.lower() for part in re.findall("[A-Z][^A-Z]*", model.__name__)]
        )
        if prefix:
            return "{}_{}".format(prefix, slug)
        else:
            return slug

    @staticmethod
    def get_pk_path_param_name_from_model(model):
        """Returns a specific name for the primary key of a model.

        Args:
            model (django.db.models.Model): The model for which a path parameter name is needed

        Returns:
            str: *_pk where * is the model name in all lower case letters
        """
        return "%s_pk" % "_".join(
            [part.lower() for part in re.findall("[A-Z][^A-Z]*", model.__name__)]
        )

    def convert_endpoint_path_params(self, path, view):
        """Replaces all 'pulp_id' path parameters with a specific name for the primary key.
        This method is used to ensure that the primary key name is consistent between nested
        endpoints. get_endpoints() returns paths that use 'pulp_id' for the top level path and a
        specific name for the nested paths. e.g.: repository_pk.
        This ensures that when the endpoints are sorted, the parent endpoint appears before the
        endpoints nested under it.
        Returns:
            path(str): The modified path.
        """
        if not hasattr(view, "queryset") or view.queryset is None:
            if hasattr(view, "model"):
                resource_model = view.model
            else:
                return path
        else:
            resource_model = view.queryset.model
        if resource_model:
            prefix_ = None
            if issubclass(resource_model, RepositoryVersion):
                prefix_ = view.parent_viewset.endpoint_name
            param_name = self.get_parameter_slug_from_model(resource_model, prefix_)
            resource_path = "%s}/" % path.rsplit(sep="}", maxsplit=1)[0]
            path = path.replace(resource_path, "{%s}" % param_name)
        return path

    def parse(self, request, public):
        """ Iterate endpoints generating per method path operations. """
        result = {}
        self._initialise_endpoints()

        # Adding plugin filter
        plugins = None
        # /pulp/api/v3/docs/api.json?bindings&plugin=pulp_file
        if "bindings" in request.query_params:
            plugins = [request.query_params["plugin"]]

        is_public = None if public else request
        for path, path_regex, method, view in self._get_paths_and_endpoints(is_public):
            plugin = view.__module__.split(".")[0]
            if plugins and plugin not in plugins:  # plugin filter
                continue

            if not self.has_view_permissions(path, method, view):
                continue

            if "docs" in path or "status" in path:
                continue

            if not self.contains_serializer(view):
                continue

            # Converting path params
            path = self.convert_endpoint_path_params(path, view)

            schema = view.schema

            # beware that every access to schema yields a fresh object (descriptor pattern)
            operation = schema.get_operation(path, path_regex, method, self.registry)

            # operation was manually removed via @extend_schema
            if not operation:
                continue

            # operationId as actions [list, read, sync, modify, create, delete, ...]
            if "bindings" in request.query_params:
                action_name = getattr(view, "action", schema.method.lower())
                if schema.method.lower() == "get" and schema._is_list_view():
                    operation["operationId"] = "list"
                elif action_name not in schema.method_mapping:
                    action = action_name.replace("destroy", "delete").replace("retrieve", "read")
                    operation["operationId"] = action
                else:
                    operation["operationId"] = schema.method_mapping[schema.method.lower()]

            # Adding query parameters
            if "parameters" in operation and schema.method.lower() == "get":
                fields_paramenter = build_parameter_type(
                    name="fields",
                    schema={"type": "string"},
                    location=OpenApiParameter.QUERY,
                    description="A list of fields to include in the response.",
                )
                operation["parameters"].append(fields_paramenter)
                not_fields_paramenter = build_parameter_type(
                    name="exclude_fields",
                    schema={"type": "string"},
                    location=OpenApiParameter.QUERY,
                    description="A list of fields to exclude from the response.",
                )
                operation["parameters"].append(not_fields_paramenter)

            # Normalise path for any provided mount url.
            if path.startswith("/"):
                path = path[1:]

            if not path.startswith("{"):
                path = urljoin(self.url or "/", path)

            result.setdefault(path, {})
            result[path][method.lower()] = operation

        return result

    def contains_serializer(self, view):
        """Check if view contains serializer."""
        if isinstance(view, GenericAPIView):
            return True
        if isinstance(view, APIView):
            if callable(getattr(view, "get_serializer", None)):
                return True
            if callable(getattr(view, "get_serializer_class", None)):
                return True
            if hasattr(view, "serializer_class"):
                return True
        return False

    def get_schema(self, request=None, public=False):
        """ Generate a OpenAPI schema. """
        result = super().get_schema(request, public)
        # Basically I'm doing it to get pulp logo at redoc page
        result["info"]["x-logo"] = {
            "url": "https://pulp.plan.io/attachments/download/517478/pulp_logo_word_rectangle.svg"
        }
        # Adding current host as server (it will provide a default value for the bindings)
        result["servers"] = [{"url": request.build_absolute_uri("/")}]
        return result
