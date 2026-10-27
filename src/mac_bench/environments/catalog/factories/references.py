"""Source-backed parameter references, including links inside owned child rows.

Derived on demand: existing catalog revisions and native creation payloads are
unchanged. Unknown/dynamic targets remain explicit, never ordinary text fields.
"""

from copy import deepcopy


def escape(value):
    return str(value).replace("~", "~0").replace("/", "~1")


class ReferenceContracts:
    def __init__(self, registry):
        self.registry = registry
        self.entities = registry.entities
        self.bindings = [
            self.entities.binding(r["binding"])
            for r in registry.coverage()["bindings"]
            if r.get("binding")
        ]
        self.cache = {}

    def targets(self, source, *, native=None, entities=()):
        return [
            {"entity": b["entity"], "binding": b["id"]}
            for b in self.bindings
            if b["scope"] == source["scope"]
            and (
                (native is not None and b["native_resource"] == native)
                or b["entity"] in entities
            )
        ]

    def describe(self, spec):
        if spec["id"] in self.cache:
            return deepcopy(self.cache[spec["id"]])
        source = self.entities.binding(spec["binding"])
        recipe = spec.get("recipe", {})
        result = {}
        inline_inputs = set()

        def add(
            path,
            targets,
            identity="auto",
            required=False,
            nullable=True,
            evidence=None,
            **extra,
        ):
            if identity == "auto":
                identity = None
                if len(targets) == 1:
                    contract = self.entities.resolve(
                        targets[0]["entity"], binding=targets[0]["binding"]
                    )["contract"]
                    fields = contract.get("identity", {}).get("fields", [])
                    identity = fields[0] if len(fields) == 1 else None
            result[path] = {
                "path": path,
                "targets": targets,
                "identity": identity,
                "required": bool(required),
                "nullable": bool(nullable),
                "evidence": evidence,
                **extra,
            }

        if recipe.get("kind") == "frappe":

            def document(name, prefix, ancestors=()):
                if name in ancestors:
                    raise ValueError("Recursive native child table: " + name)
                for field in recipe["documents"][name]["fields"]:
                    if field.get("read_only"):
                        continue
                    path = prefix + "/" + escape(field["fieldname"])
                    kind = field["fieldtype"]
                    if kind in {"Table", "Table MultiSelect"}:
                        document(field["options"], path + "/*", (*ancestors, name))
                    elif kind in {"Link", "Dynamic Link"}:
                        add(
                            path,
                            self.targets(source, native=field.get("options"))
                            if kind == "Link"
                            else [],
                            identity="name",
                            required=field.get("reqd"),
                            nullable=not field.get("reqd"),
                            evidence={
                                "doctype": name,
                                "field": field["fieldname"],
                                "native_type": kind,
                                "native_target": field.get("options"),
                            },
                            dynamic_target_field=field.get("options")
                            if kind == "Dynamic Link"
                            else None,
                        )

            document(recipe["doctype"], "/values")
        elif recipe.get("kind") == "pretix":
            op = spec["operations"]["create"]
            operation = self.entities.resolve(
                op["entity"], binding=op["binding"], operation=op["operation"]
            )["contract"]
            inputs = operation.get("input", {})
            library = inputs.get("library")

            def serializer(name, prefix, ancestors=()):
                name = name.removeprefix("schema/")
                if name in ancestors:
                    raise ValueError("Recursive serializer input: " + name)
                fields = self.entities.type_definition(library, name)["types"][
                    name
                ].get("fields", {})

                def field_contract(field, path):
                    if field.get("read_only"):
                        return
                    kind = field.get("type")
                    if field.get("schema"):
                        inline_inputs.add(path)
                        serializer(
                            field["schema"],
                            path
                            + (
                                "/*"
                                if field.get("many") or kind == "ListSerializer"
                                else ""
                            ),
                            (*ancestors, name),
                        )
                    elif kind in {
                        "ListField",
                        "ManyRelatedField",
                        "MultipleChoiceField",
                    }:
                        field_contract(
                            field.get("child") or field.get("child_relation") or {},
                            path + "/*",
                        )
                    elif field.get("target_model"):
                        add(
                            path,
                            self.targets(source, native=field["target_model"]),
                            identity="id"
                            if kind == "PrimaryKeyRelatedField"
                            else field.get("slug_field"),
                            required=field.get("required"),
                            nullable=field.get("allow_null"),
                            evidence={
                                "serializer": name,
                                "native_type": kind,
                                "native_target": field["target_model"],
                            },
                        )

                for key, field in fields.items():
                    field_contract(field, prefix + "/" + escape(key))

            if library:
                for name in inputs.get("serializers", []):
                    serializer(name, "/values")
        else:
            # Match native entity/owned-component contracts to actual DTO input
            # properties. No suffix-based guessing that a string is an ID.
            root = spec["parameters"]

            def shapes(schema, seen=()):
                if "$ref" in schema:
                    ref = schema["$ref"]
                    if ref in seen:
                        return []
                    target = root
                    for part in ref.split("/")[1:]:
                        target = target[part.replace("~1", "/").replace("~0", "~")]
                    return shapes(target, (*seen, ref))
                return [schema] + [
                    s
                    for key in ("anyOf", "oneOf", "allOf")
                    for child in schema.get(key, [])
                    for s in shapes(child, seen)
                ]

            def entity(binding, schema, prefix, ancestors=()):
                if binding["id"] in ancestors:
                    return
                contract = self.entities.resolve(
                    binding["entity"], binding=binding["id"]
                )["contract"]
                properties = {
                    k: v
                    for shape in shapes(schema)
                    for k, v in shape.get("properties", {}).items()
                }
                for key, field in contract["fields"].items():
                    ref = field.get("reference", {})
                    if not ref:
                        continue
                    wire = (
                        key + "Id"
                        if recipe.get("kind") == "twenty" and key + "Id" in properties
                        else key
                    )
                    if wire not in properties:
                        continue
                    path = prefix + "/" + escape(wire)
                    targets = self.targets(binding, entities=ref.get("targets", []))
                    if ref.get("kind") == "owned_component":
                        if len(targets) == 1:
                            child = self.entities.binding(targets[0]["binding"])
                            for shape in shapes(properties[wire]):
                                if "items" in shape:
                                    entity(
                                        child,
                                        shape["items"],
                                        path + "/*",
                                        (*ancestors, binding["id"]),
                                    )
                    elif ref.get("kind") == "association":
                        required = any(
                            wire in s.get("required", []) for s in shapes(schema)
                        )
                        add(
                            path,
                            targets,
                            required=required,
                            nullable=not required,
                            evidence={
                                "binding": binding["id"],
                                "field": key,
                                "reference": ref,
                            },
                        )

            for shape in shapes(root):
                if "values" in shape.get("properties", {}):
                    entity(source, shape["properties"]["values"], "/values")
        # Explicit projections are authoritative for wire identity and parent
        # scope fields; they also cover lifecycle factories without normal DTOs.
        for slot, projection in recipe.get("parent_projection", {}).items():
            parent = spec["parents"][slot]
            path = "/" + "/".join(escape(k) for k in projection["path"])
            if recipe.get('kind') == 'pretix' and path in inline_inputs:
                # Older captured factories inferred an ID projection from the
                # read relation even when the write serializer embeds a body
                # (e.g. Order.invoice_address). Keep the actual write contract.
                continue
            add(
                path,
                [{k: parent[k] for k in ("entity", "binding")}],
                identity=projection["identity"],
                required=parent["required"]
                or result.get(path, {}).get("required", False),
                nullable=result.get(path, {}).get("nullable", not parent["required"]),
                evidence={
                    "factory": spec["id"],
                    "parent_slot": slot,
                    "native_field": result.get(path, {}).get("evidence"),
                },
                parent_slot=slot,
            )
        self.cache[spec["id"]] = list(result.values())
        return deepcopy(self.cache[spec["id"]])
