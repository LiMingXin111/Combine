"""Validate the structural JSON Schema constraints used by registered tools."""
import math
import re


def validate(value, schema, root=None, depth=0):
    if depth > 24:
        return "参数嵌套过深"
    if schema is True:
        return ""
    if schema is False:
        return "参数不允许"
    if not isinstance(schema, dict):
        return "工具参数定义无效"
    root = schema if root is None else root
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/"):
            return "不支持远程参数引用"
        target = root
        try:
            for part in reference[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            return "参数引用不存在"
        return validate(value, target, root, depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            results = [not validate(value, option, root, depth + 1) for option in schema[key]]
            if (key == "anyOf" and not any(results)) or (key == "oneOf" and sum(results) != 1) or (key == "allOf" and not all(results)):
                return "参数不符合工具定义"
    kinds = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
             "boolean": isinstance(value, bool), "null": value is None,
             "integer": isinstance(value, int) and not isinstance(value, bool),
             "number": isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)}
    types = schema.get("type")
    if types and not any(kinds.get(t, False) for t in (types if isinstance(types, list) else [types])):
        return "参数类型不正确"
    if "enum" in schema and value not in schema["enum"]:
        return "参数不在允许值内"
    if isinstance(value, dict):
        if any(key not in value for key in schema.get("required", [])):
            return "缺少必填参数"
        properties = schema.get("properties", {})
        for key, item in value.items():
            child = properties.get(key, schema.get("additionalProperties", True))
            error = validate(item, child, root, depth + 1)
            if error:
                return f"{key}: {error}"
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10000):
            return "参数数组长度不正确"
        for item in value:
            error = validate(item, schema.get("items", True), root, depth + 1)
            if error:
                return error
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1_000_000):
            return "参数文本长度不正确"
    if kinds["number"]:
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            return "参数数值超出范围"
    return ""
