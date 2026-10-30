"""The admin API's OpenAPI description (docs/api/admin-v1.yaml), as UAT-CAT-05 holds the running
install's answers to it: each response's status must be one the operation documents, and its body
of the documented schema. The schemas use a small part of OpenAPI 3.0 (type, nullable, enum,
required, properties, additionalProperties, items, $ref), and that part is what is checked."""

import re
from pathlib import Path

import yaml

from .install import REPO

DESCRIPTION = REPO / "docs" / "api" / "admin-v1.yaml"
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


class Description:
    def __init__(self, path=DESCRIPTION):
        self.doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        self.base = "/colvenna/admin/v1"

    def ref(self, node):
        while isinstance(node, dict) and "$ref" in node:
            target = self.doc
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target[part]
            node = target
        return node

    def operation(self, method, path):
        """(template, operation) for a request path (below or with the API's base), or (None, None)."""
        path = path.split("?", 1)[0]
        if path.startswith(self.base):
            path = path[len(self.base):]
        for template, item in self.doc["paths"].items():
            pattern = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$"
            if re.match(pattern, path) and method.lower() in item:
                return template, item[method.lower()]
        return None, None

    def check(self, method, path, status, body):
        """The problems of one answer, [] when it is as documented."""
        template, op = self.operation(method, path)
        if op is None:
            return [f"{method} {path}: no such operation in the description"]
        responses = op.get("responses", {})
        response = responses.get(str(status))
        if response is None:
            return [f"{method} {template}: status {status} is not documented ({', '.join(responses)})"]
        response = self.ref(response)
        schema = (((response.get("content") or {}).get("application/json") or {}).get("schema"))
        if schema is None:
            return [] if body in (None, "", {}) else [f"{method} {template} {status}: a body where none is documented"]
        return [f"{method} {template} {status}: {p}" for p in self.validate(body, schema, "$")]

    def validate(self, value, schema, where):
        schema = self.ref(schema)
        if value is None:
            return [] if schema.get("nullable") else [f"{where}: null, not nullable"]
        kind = schema.get("type")
        problems = []
        if kind == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return [f"{where}: {type(value).__name__}, not integer"]
        elif kind == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return [f"{where}: {type(value).__name__}, not number"]
        elif kind in TYPES and not isinstance(value, TYPES[kind]):
            return [f"{where}: {type(value).__name__}, not {kind}"]
        if "enum" in schema and value not in schema["enum"]:
            problems.append(f"{where}: {value!r} not one of {schema['enum']}")
        if kind == "object" or (kind is None and isinstance(value, dict) and "properties" in schema):
            props = schema.get("properties", {})
            for name in schema.get("required", []):
                if name not in value:
                    problems.append(f"{where}: no {name}")
            extra = schema.get("additionalProperties", True)
            for name, v in value.items():
                if name in props:
                    problems += self.validate(v, props[name], f"{where}.{name}")
                elif extra is False:
                    problems.append(f"{where}: {name} is not documented")
                elif isinstance(extra, dict):
                    problems += self.validate(v, extra, f"{where}.{name}")
        if kind == "array" and "items" in schema:
            for i, v in enumerate(value):
                problems += self.validate(v, schema["items"], f"{where}[{i}]")
        return problems

    def operations(self):
        """Every documented (method, template, action)."""
        out = []
        for template, item in self.doc["paths"].items():
            for method, op in item.items():
                if isinstance(op, dict) and "x-colvenna-action" in op:
                    out.append((method.upper(), template, op["x-colvenna-action"]))
        return out
