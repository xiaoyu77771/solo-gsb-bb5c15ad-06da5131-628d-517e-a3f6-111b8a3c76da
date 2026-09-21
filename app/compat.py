"""JSON Schema 兼容性分析引擎。

核心思路
========
兼容性方向：

* 向后兼容（backward）：新 schema 必须接受旧 schema 接受的全部实例
  （``new ⊇ old``）。反例 = 旧版合法、新版拒绝的文档。
* 向前兼容（forward）：旧 schema 必须接受新 schema 接受的全部实例
  （``old ⊇ new``）。反例 = 新版合法、旧版拒绝的文档。

因此只需实现一个方向的检查 ``find_violations(source, target)``：
递归比较类型、required、enum、数组、对象与组合关键字，为每一种
"source 允许但 target 拒绝" 的情况构造最小反例，再交换入参即可得到
另一方向。

反例会用 ``jsonschema`` 做一次最终校验（source 合法且 target 拒绝），
保证返回的 counterexample 真实有效；启发式检查（pattern、multipleOf、
anyOf 覆盖等）若无法构造出经过验证的反例，则降级为 warning。
"""
from __future__ import annotations

import copy
import math
from collections import OrderedDict
from typing import Any

from jsonschema.validators import validator_for

# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------

PRIMITIVE_TYPES = ("null", "boolean", "object", "array", "number", "string", "integer")
_MISSING = object()


def is_schema(obj: Any) -> bool:
    return obj is True or obj is False or isinstance(obj, dict)


def as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    return [value]


def type_set(schema: Any) -> set[str] | None:
    """返回显式声明的类型集合；未声明 type 返回 None（任意类型）。"""
    if not isinstance(schema, dict) or "type" not in schema:
        return None
    return set(as_list(schema["type"]))


def could_be(schema: Any, json_type: str) -> bool:
    """schema 是否可能校验通过该 JSON 类型的实例。"""
    ts = type_set(schema)
    if ts is None:
        # 未声明 type 时，按结构关键字做保守推断
        if isinstance(schema, dict):
            if json_type == "object" and ("properties" in schema or "required" in schema
                                          or "additionalProperties" in schema
                                          or "patternProperties" in schema or "minProperties" in schema):
                return True
            if json_type == "array" and ("items" in schema or "prefixItems" in schema
                                         or "minItems" in schema):
                return True
        return True
    if json_type == "number" and "integer" in ts:
        return True
    return json_type in ts


def member_of_enum(value: Any, enum: list) -> bool:
    """枚举成员判断，兼容 list/dict 等不可哈希值。"""
    try:
        return value in enum
    except TypeError:
        import json
        key = json.dumps(value, sort_keys=True, default=str)
        return any(json.dumps(e, sort_keys=True, default=str) == key for e in enum)


# ---------------------------------------------------------------------------
# $ref 预解析（支持文档内 #/... 引用，带环保护）
# ---------------------------------------------------------------------------

def resolve_refs(schema: Any, root: Any = None, seen: frozenset[str] = frozenset()) -> Any:
    if root is None:
        root = schema
    if schema is True or schema is False:
        return schema
    if not isinstance(schema, dict):
        return schema

    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/") and ref not in seen:
        node: Any = root
        ok = True
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                ok = False
                break
        if ok:
            return resolve_refs(copy.deepcopy(node), root, seen | {ref})
        # 无法解析的引用按"无约束"处理，避免误报
        return True

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if isinstance(value, dict):
            out[key] = resolve_refs(value, root, seen)
        elif isinstance(value, list):
            out[key] = [
                resolve_refs(item, root, seen) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# allOf 浅合并（effective schema）
# ---------------------------------------------------------------------------

def merge_schemas(a: Any, b: Any) -> Any:
    if b is True:
        return a
    if b is False or a is False:
        return False
    if a is True:
        return copy.deepcopy(b)

    merged = copy.deepcopy(a)
    for key, value in b.items():
        if key == "required":
            merged["required"] = sorted(set(merged.get("required", [])) | set(value))
        elif key == "type" and "type" in merged:
            inter = type_set(merged) & type_set({"type": value})
            merged["type"] = sorted(inter) if inter else []
        elif key in ("properties", "patternProperties") and isinstance(value, dict):
            section = dict(merged.get(key, {}))
            for sub_key, sub_schema in value.items():
                if sub_key in section and is_schema(section[sub_key]) and is_schema(sub_schema):
                    section[sub_key] = merge_schemas(section[sub_key], sub_schema)
                else:
                    section[sub_key] = copy.deepcopy(sub_schema)
            merged[key] = section
        elif key == "enum" and "enum" in merged:
            merged["enum"] = [e for e in merged["enum"] if member_of_enum(e, value)]
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def effective(schema: Any) -> Any:
    """把 allOf 分支合并进当前 schema（递归），返回不含 allOf 的等价结构。"""
    if not isinstance(schema, dict):
        return schema
    base = {k: copy.deepcopy(v) for k, v in schema.items() if k != "allOf"}
    result: Any = base if base else True
    for branch in schema.get("allOf", []):
        result = merge_schemas(result, effective(branch))
    return result


# ---------------------------------------------------------------------------
# 最小合法实例生成
# ---------------------------------------------------------------------------

def _lower_bound(schema: dict) -> tuple[float | None, bool]:
    """返回 (下界, 是否排他)，兼容 draft-04 的布尔 exclusiveMinimum。"""
    minimum = schema.get("minimum")
    exclusive = schema.get("exclusiveMinimum")
    if isinstance(exclusive, bool):
        return minimum, exclusive
    if isinstance(exclusive, (int, float)):
        return float(exclusive), True
    return (float(minimum) if minimum is not None else None), False


def _min_number(schema: dict, integer: bool) -> int | float:
    bound, exclusive = _lower_bound(schema)
    if bound is None:
        value = 0
    elif integer:
        value = int(math.ceil(bound))
        if exclusive and value_eq(value, bound):
            value += 1
    else:
        value = bound + (1 if exclusive else 0)
    multiple = schema.get("multipleOf")
    if isinstance(multiple, (int, float)) and integer:
        value = int(math.ceil(value / multiple)) * int(multiple)
    return value


def value_eq(a: float, b: float) -> bool:
    return abs(a - b) < 1e-9


def normalize_items(schema: dict) -> tuple[list | None, Any, bool]:
    """把不同 draft 的数组定义归一为 (前缀元组, 其余元素 schema, 是否显式声明其余约束)。

    * draft-04：``items`` 为列表 -> 前缀；``additionalItems`` -> 其余
    * 2019-09+：``prefixItems`` -> 前缀；``items`` -> 其余
    * ``items`` 为 dict -> 齐次数组

    第三个返回值区分"其余元素约束被显式声明（可能为 false）"与"未声明"。
    """
    if "prefixItems" in schema:
        prefix = schema.get("prefixItems")
        return (prefix if isinstance(prefix, list) else None), schema.get("items", True), "items" in schema
    items = schema.get("items", _MISSING)
    if isinstance(items, list):
        return items, schema.get("additionalItems", True), "additionalItems" in schema
    if items is _MISSING:
        return None, True, False
    return None, items, True


def minimal_array(schema: dict) -> list:
    prefix, rest, _ = normalize_items(schema)
    n = schema.get("minItems", 0) or 0
    arr: list[Any] = []
    if prefix:
        for i in range(max(n, len(prefix))):
            if i < len(prefix):
                arr.append(minimal_instance(prefix[i]))
            else:
                arr.append(minimal_instance(rest))
    else:
        element_schema = rest
        arr = [copy.deepcopy(minimal_instance(element_schema)) for _ in range(n)]
    return arr


def minimal_object(schema: dict) -> dict:
    obj: dict[str, Any] = {}
    for name in schema.get("required", []):
        sub = schema.get("properties", {}).get(name)
        obj[name] = minimal_instance(sub) if is_schema(sub) else None
    min_props = schema.get("minProperties", 0) or 0
    while len(obj) < min_props:
        obj[extra_key(schema, obj)] = None
    return obj


def minimal_instance(schema: Any) -> Any:
    """生成满足 schema 的一个最小实例（尽力而为）。"""
    if schema is True or not is_schema(schema):
        return {}
    if schema is False:
        return None
    if "const" in schema:
        return copy.deepcopy(schema["const"])
    if schema.get("enum"):
        return copy.deepcopy(schema["enum"][0])
    if isinstance(schema.get("anyOf"), list) and schema["anyOf"]:
        return minimal_instance(schema["anyOf"][0])
    if isinstance(schema.get("oneOf"), list) and schema["oneOf"]:
        return minimal_instance(schema["oneOf"][0])
    if "allOf" in schema:
        return minimal_instance(effective(schema))

    ts = type_set(schema)
    first_type: str
    if ts:
        first_type = sorted(ts)[0]
    elif isinstance(schema, dict) and ("properties" in schema or "required" in schema):
        first_type = "object"
    elif isinstance(schema, dict) and ("items" in schema or "prefixItems" in schema):
        first_type = "array"
    else:
        first_type = "object"

    if first_type == "null":
        return None
    if first_type == "boolean":
        return False
    if first_type == "integer":
        return _min_number(schema, integer=True)
    if first_type == "number":
        return _min_number(schema, integer=False)
    if first_type == "string":
        return "x" * (schema.get("minLength", 0) or 0)
    if first_type == "array":
        return minimal_array(schema)
    return minimal_object(schema)


def typed_fragment(schema: Any, json_type: str) -> Any:
    """构造一个指定类型、尽量满足 schema 其他约束的最小取值。"""
    if isinstance(schema, dict):
        for value in schema.get("enum", []):
            if json_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
                return value
            if json_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
            if json_type == "string" and isinstance(value, str):
                return value
            if json_type == "boolean" and isinstance(value, bool):
                return value
            if json_type == "null" and value is None:
                return value
        if json_type in ("integer", "number"):
            return _min_number(schema, integer=(json_type == "integer"))
        if json_type == "string":
            return "x" * (schema.get("minLength", 0) or 0)
    return {"null": None, "boolean": False, "object": {}, "array": [],
            "number": 0, "integer": 0, "string": ""}[json_type]


# ---------------------------------------------------------------------------
# 路径（schema 层级 token，与最终 JSON Pointer 渲染分离）
# ---------------------------------------------------------------------------

# ("p", name)          properties/<name>
# ("items",)           items（齐次数组元素）
# ("tup", i)           prefixItems|items 列表的第 i 个前缀元素
# ("ap",)              additionalProperties（匿名额外键）
# ("apn", name)        由指定属性名落到 additionalProperties 约束
# ("pp", pattern)      patternProperties/<pattern>
# ("kw", kw, i)        anyOf/oneOf/allOf 第 i 个分支（透明，不产生嵌套）
# ("not",)             not（透明）

def render_path(path: tuple) -> str:
    parts: list[str] = []
    for frame in path:
        kind = frame[0]
        if kind == "p":
            parts += ["properties", frame[1]]
        elif kind == "items":
            parts.append("items")
        elif kind == "tup":
            parts += ["__TUPLE__", str(frame[1])]
        elif kind == "ap":
            parts.append("additionalProperties")
        elif kind == "apn":
            parts += ["additionalProperties", frame[1]]
        elif kind == "pp":
            parts += ["patternProperties", frame[1]]
        elif kind == "kw":
            parts += [frame[1], str(frame[2])]
        elif kind == "not":
            parts.append("not")
    return "#/" + "/".join(escape_pointer(p) for p in parts) if parts else "#"


def escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def walk_schema(root: Any, path: tuple) -> tuple[Any, list]:
    """沿路径走到 source schema，返回 (终点 schema, [(frame, 进入前 schema)])。"""
    schema = effective(root)
    stack: list[tuple] = []
    for frame in path:
        kind = frame[0]
        stack.append((frame, schema))
        if kind == "p":
            schema = schema.get("properties", {}).get(frame[1], True)
        elif kind == "items":
            _, rest, _ = normalize_items(schema)
            schema = rest
        elif kind == "tup":
            i = frame[1]
            prefix, rest, _ = normalize_items(schema)
            schema = prefix[i] if prefix and i < len(prefix) else rest
        elif kind in ("ap", "apn"):
            schema = schema.get("additionalProperties", True)
        elif kind == "pp":
            schema = schema.get("patternProperties", {}).get(frame[1], True)
        elif kind == "kw":
            schema = schema.get(frame[1], [True])[frame[2]]
        elif kind == "not":
            schema = schema.get("not", True)
        schema = effective(schema)
    return schema, stack


def extra_key(schema: dict, taken: dict | None = None) -> str:
    taken = taken or {}
    names = set(schema.get("properties", {}).keys()) | set(taken.keys())
    candidate = "__extra__"
    while candidate in names:
        candidate = "_" + candidate
    return candidate


def pattern_sample_key(pattern: str) -> str:
    """从简单正则里猜一个能匹配的键名，无法推断时退回 'x'。"""
    import re
    literals = []
    for match in re.finditer(r"[A-Za-z0-9_]+", pattern):
        literals.append(match.group(0))
    for candidate in literals + ["x", "ab", "key"]:
        try:
            if re.search(pattern, candidate):
                return candidate
        except re.error:
            break
    return "x"


def embed_fragment(root: Any, path: tuple, fragment: Any) -> tuple[Any, str]:
    """把位于 path 的违规片段向上包裹成一份完整文档，返回 (文档, 片段指针)。"""
    _, stack = walk_schema(root, path)
    value = fragment
    pointer_parts: list[str] = []

    for frame, before in reversed(stack):
        kind = frame[0]
        if kind in ("kw", "not"):
            continue  # 组合关键字不引入新的文档层级
        if kind == "p":
            obj = minimal_object(before if isinstance(before, dict) else {})
            obj[frame[1]] = value
            value = obj
            pointer_parts.insert(0, frame[1])
        elif kind == "items":
            arr = minimal_array(before) if isinstance(before, dict) else []
            minimum = before.get("minItems", 0) if isinstance(before, dict) else 0
            prefix, rest, _ = normalize_items(before) if isinstance(before, dict) else (None, True, False)
            prefix_len = len(prefix) if prefix else 0
            # items 帧代表"其余元素"位置：必须先补齐前缀，再追加一个 rest 元素
            while len(arr) < prefix_len:
                arr.append(minimal_instance(prefix[len(arr)]))
            rest_index = max(len(arr), prefix_len, minimum)
            while len(arr) < rest_index:
                arr.append(minimal_instance(rest))
            arr.append(value)
            value = arr
            pointer_parts.insert(0, str(rest_index))
        elif kind == "tup":
            i = frame[1]
            arr = minimal_array(before) if isinstance(before, dict) else []
            prefix, rest, _ = normalize_items(before) if isinstance(before, dict) else (None, True, False)
            while len(arr) <= i:
                j = len(arr)
                if prefix and j < len(prefix):
                    arr.append(minimal_instance(prefix[j]))
                else:
                    arr.append(minimal_instance(rest))
            arr[i] = value
            value = arr
            pointer_parts.insert(0, str(i))
        elif kind == "ap":
            obj = minimal_object(before if isinstance(before, dict) else {})
            key = extra_key(before if isinstance(before, dict) else {}, obj)
            obj[key] = value
            value = obj
            pointer_parts.insert(0, key)
        elif kind == "apn":
            obj = minimal_object(before if isinstance(before, dict) else {})
            obj[frame[1]] = value
            value = obj
            pointer_parts.insert(0, frame[1])
        elif kind == "pp":
            obj = minimal_object(before if isinstance(before, dict) else {})
            key = pattern_sample_key(frame[1])
            obj[key] = value
            value = obj
            pointer_parts.insert(0, key)

    pointer = "#" + ("" if not pointer_parts else "/" + "/".join(escape_pointer(p) for p in pointer_parts))
    return value, pointer


# ---------------------------------------------------------------------------
# 分析器
# ---------------------------------------------------------------------------

def collect_tuple_keywords(root: Any) -> dict[str, str]:
    """遍历 schema，返回 path_key -> "items"|"prefixItems" 的映射。

    path_key 是 tuple path（以 tup 帧序列表达），用于把 render_path 输出中的
    ``__TUPLE__`` 占位符替换成 schema 实际使用的关键字（draft-04/06/07 元组
    使用列表形式的 ``items``，2019-09+ 使用 ``prefixItems``）。
    """
    mapping: dict[str, str] = {}

    def walk(schema: Any, tup_prefix: tuple) -> None:
        schema = effective(schema)
        if not isinstance(schema, dict):
            return
        prefix, _, _ = normalize_items(schema)
        if prefix is not None:
            keyword = "items" if "items" in schema and isinstance(schema["items"], list) else "prefixItems"
            for i, sub in enumerate(prefix):
                tup_path = tup_prefix + f"/{i}"
                mapping[tup_path] = keyword
                walk(sub, tup_prefix + f"/{i}")
        if isinstance(schema.get("items"), dict):
            walk(schema["items"], _strip_tail(tup_prefix))
        for name, sub in (schema.get("properties") or {}).items():
            walk(sub, tup_prefix)
        for name, sub in (schema.get("patternProperties") or {}).items():
            walk(sub, tup_prefix)
        if isinstance(schema.get("additionalProperties"), dict):
            walk(schema["additionalProperties"], tup_prefix)
        for kw in ("anyOf", "oneOf", "allOf"):
            for sub in schema.get(kw, []) or []:
                walk(sub, tup_prefix)

    def _strip_tail(tup_prefix: str) -> str:
        # 进入齐次 items 时，tup 前缀语义结束；保留外层路径即可
        return tup_prefix.rsplit("/", 1)[0] if "/" in tup_prefix else ""

    walk(root, "")
    return mapping


def resolve_tuple_placeholders(rendered: str, mapping: dict[str, str]) -> str:
    """把渲染路径中的 __TUPLE__/<i> 替换为实际关键字。

    mapping 的键形如 "/0/1"（沿嵌套元组层叠的索引序列）。当遇到非元组
    层级（properties 等）时重置索引序列。
    """
    segments = rendered.split("/")
    out: list[str] = []
    tup_trail = ""
    in_tuple = False
    for seg in segments:
        if seg == "__TUPLE__":
            in_tuple = True
            continue
        if in_tuple and seg.isdigit():
            tup_trail += "/" + seg
            out.append(mapping.get(tup_trail, "prefixItems"))
            out.append(seg)
            continue
        in_tuple = False
        tup_trail = ""
        out.append(seg)
    return "/".join(out)


class IssueBuilder:
    def __init__(self, source_root: Any, target_root: Any, source_validator, target_validator):
        self.source_root = source_root
        self.target_root = target_root
        self.source_validator = source_validator
        self.target_validator = target_validator
        self.tuple_keywords = collect_tuple_keywords(source_root)
        self.raw: list[dict] = []

    def _render(self, path: tuple) -> str:
        rendered = render_path(path)
        return resolve_tuple_placeholders(rendered, self.tuple_keywords)

    def verified_witness(self, candidate: Any) -> bool:
        try:
            source_ok = self.source_validator.is_valid(candidate)
            target_bad = not self.target_validator.is_valid(candidate)
            return source_ok and target_bad
        except (RecursionError, Exception):  # noqa: BLE001
            return False

    def add(self, path: tuple, code: str, message: str, reason: str,
            fragment: Any = None, heuristic: bool = False,
            candidate: Any = _MISSING) -> None:
        """记录一条问题。

        * 普通问题：fragment 是位于 path 的违规片段，自动包裹成完整文档并校验；
        * 启发式问题（heuristic=True）：candidate 是完整文档，经验证成立才算
          error，否则降级为 warning 且不附反例。
        """
        if heuristic:
            if candidate is not _MISSING and self.verified_witness(candidate):
                self.raw.append({
                    "path": self._render(path), "code": code, "severity": "error",
                    "message": message, "reason": reason,
                    "counterexample": candidate, "counterexample_path": "#",
                })
            else:
                self.raw.append({
                    "path": self._render(path), "code": code, "severity": "warning",
                    "message": message,
                    "reason": reason + "（未能自动构造出经过验证的反例，标记为潜在风险）",
                    "counterexample": None, "counterexample_path": None,
                })
            return

        counterexample = None
        counterexample_path = None
        if fragment is not _MISSING:
            doc, pointer = embed_fragment(self.source_root, path, fragment)
            if self.verified_witness(doc):
                counterexample, counterexample_path = doc, pointer
            else:
                # 结构上构造的反例理论上必然成立；校验失败说明受其他关键字约束，
                # 保留问题但不附可能误导的反例。
                pass
        self.raw.append({
            "path": self._render(path), "code": code, "severity": "error",
            "message": message, "reason": reason,
            "counterexample": counterexample,
            "counterexample_path": counterexample_path,
        })


def find_violations(source_root: Any, target_root: Any) -> list[dict]:
    """返回 source 合法但 target 拒绝的所有结构性问题。"""
    source_validator = make_validator(source_root)
    target_validator = make_validator(target_root)
    builder = IssueBuilder(source_root, target_root, source_validator, target_validator)
    _check(effective(source_root), effective(target_root), (), builder,
           source_root, target_root, source_validator, target_validator)
    return builder.raw


def validator_class_for(schema: Any):
    """返回与 schema 风格匹配的 jsonschema 校验器类。

    有 ``$schema`` 时按其声明选择；没有时做形态推断（数组形式的 ``items``
    是 draft-04/06/07 元组写法，2020-12 中应是 ``prefixItems``），避免
    jsonschema 把列表 items 错误地按 2020-12 语义处理。
    """
    from jsonschema import Draft4Validator, Draft7Validator

    if isinstance(schema, dict) and isinstance(schema.get("$schema"), str):
        return validator_for(schema)
    if _uses_legacy_dialect(schema, frozenset()):
        # 布尔型 exclusiveMinimum/Maximum 仅 draft-04 合法
        if _has_boolean_exclusive(schema, frozenset()):
            return Draft4Validator
        return Draft7Validator
    return validator_for(schema)


def _has_boolean_exclusive(schema: Any, seen: frozenset[int]) -> bool:
    if not isinstance(schema, dict) or id(schema) in seen:
        return False
    seen |= {id(schema)}
    if isinstance(schema.get("exclusiveMinimum"), bool):
        return True
    if isinstance(schema.get("exclusiveMaximum"), bool):
        return True
    for value in schema.values():
        if isinstance(value, dict) and _has_boolean_exclusive(value, seen):
            return True
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and _has_boolean_exclusive(item, seen):
                    return True
    return False


def make_validator(schema: Any):
    """构造校验器；schema 本身非法时抛出 jsonschema.SchemaError。"""
    cls = validator_class_for(schema)
    cls.check_schema(schema)  # 非法 schema 在此抛出 SchemaError
    return cls(schema)


def _uses_legacy_dialect(schema: Any, seen: frozenset[int]) -> bool:
    if not isinstance(schema, dict):
        return False
    if id(schema) in seen:
        return False
    seen |= {id(schema)}
    # 数组形式 items 是 draft-04/06/07 的元组写法
    if isinstance(schema.get("items"), list):
        return True
    # 布尔型 exclusiveMinimum/Maximum 是 draft-04 写法
    if isinstance(schema.get("exclusiveMinimum"), bool):
        return True
    if isinstance(schema.get("exclusiveMaximum"), bool):
        return True
    for key in ("properties", "patternProperties", "definitions", "$defs"):
        section = schema.get(key)
        if isinstance(section, dict):
            if any(_uses_legacy_dialect(sub, seen) for sub in section.values() if isinstance(sub, dict)):
                return True
    for key in ("items", "additionalProperties", "not", "additionalItems", "contains"):
        sub = schema.get(key)
        if isinstance(sub, dict) and _uses_legacy_dialect(sub, seen):
            return True
    for key in ("prefixItems", "items"):
        sub = schema.get(key)
        if isinstance(sub, list):
            if any(_uses_legacy_dialect(item, seen) for item in sub if isinstance(item, dict)):
                return True
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key, []) or []:
            if isinstance(sub, dict) and _uses_legacy_dialect(sub, seen):
                return True
    return False


def _true_fragment(source_at_path: Any) -> Any:
    """source 合法、target=false 时使用的具体片段。

    沿路径向下时 source 可能是 true（无约束），此时必须给出一个具体值，
    否则 JSON 中是 null 这种具体值也会被 target=false 拒绝——所以统一
    用 minimal_instance（true -> {}）。
    """
    return minimal_instance(source_at_path)


def _check(old: Any, new: Any, path: tuple, builder: IssueBuilder,
           source_root: Any, target_root: Any, source_validator, target_validator) -> None:
    old = effective(old)
    new = effective(new)

    if old is False:
        return  # source 不接受任何实例，蕴含式真空成立
    if new is True:
        return  # target 接受一切
    if new is False:
        fragment = _true_fragment(old)
        builder.add(path, "schema_forbids_all",
                    "目标 schema 在此处为 false，拒绝所有实例",
                    "旧版该位置允许实例，新版直接禁止（schema 为 false）",
                    fragment=fragment)
        return
    if old is True:
        old = {}
    if not isinstance(old, dict) or not isinstance(new, dict):
        return

    # ---- enum / const -----------------------------------------------------
    _check_enum_const(old, new, path, builder)

    # ---- type -------------------------------------------------------------
    _check_types(old, new, path, builder, source_validator, target_validator)

    # ---- object -----------------------------------------------------------
    if could_be(old, "object") and could_be(new, "object"):
        _check_object(old, new, path, builder, source_root, target_root,
                      source_validator, target_validator)

    # ---- array ------------------------------------------------------------
    if could_be(old, "array") and could_be(new, "array"):
        _check_array(old, new, path, builder)

    # ---- number / integer -------------------------------------------------
    if (could_be(old, "number") and could_be(new, "number")
            and (type_set(old) is None or type_set(new) is None
                 or type_set(old) & (type_set(new) or set()) & {"integer", "number"}
                 or ("number" in (type_set(new) or set()) and "integer" in (type_set(old) or set())))):
        _check_number(old, new, path, builder)

    # ---- string -----------------------------------------------------------
    if could_be(old, "string") and could_be(new, "string"):
        _check_string(old, new, path, builder)

    # ---- 组合关键字 anyOf / oneOf / not -----------------------------------
    _check_composition(old, new, path, builder, source_validator, target_validator)


def _check_enum_const(old: dict, new: dict, path: tuple, builder: IssueBuilder) -> None:
    if isinstance(new.get("enum"), list):
        new_enum = new["enum"]
        if isinstance(old.get("enum"), list):
            for value in old["enum"]:
                if not member_of_enum(value, new_enum):
                    builder.add(path, "enum_value_removed",
                                f"枚举值 {jsonish(value)} 在新版中已被移除",
                                f"旧版枚举包含 {jsonish(value)}，新版枚举不再包含该值，"
                                f"使用该值的旧数据会校验失败",
                                fragment=value)
        else:
            # source 无枚举约束：尝试找一个 source 合法但不在新枚举中的值
            for candidate_type in ("string", "integer", "boolean", "object", "array", "null"):
                if not could_be(old, candidate_type):
                    continue
                if candidate_type == "string":
                    value = "__not_in_enum__"
                elif candidate_type == "integer":
                    value = next((n for n in range(-1000, 1001)
                                  if not member_of_enum(n, new_enum)), 0)
                elif candidate_type == "boolean":
                    value = False if not member_of_enum(False, new_enum) else True
                elif candidate_type == "object":
                    value = {"__not_in_enum__": True}
                elif candidate_type == "array":
                    value = ["__not_in_enum__"]
                else:
                    value = None
                if member_of_enum(value, new_enum):
                    continue
                doc, _ = embed_fragment(builder.source_root, path, value)
                if builder.verified_witness(doc):
                    builder.add(path, "enum_added",
                                "新版新增了枚举约束，旧版允许的部分取值会被拒绝",
                                f"新版限定取值为 {jsonish(new_enum)}，旧版并无此限制",
                                fragment=value)
                    return

    if "const" in new:
        const_value = new["const"]
        if isinstance(old.get("enum"), list):
            for value in old["enum"]:
                if value != const_value:
                    builder.add(path, "const_added",
                                f"新版要求常量 {jsonish(const_value)}，旧版允许其他取值",
                                f"旧版枚举允许 {jsonish(value)}，新版 const 仅允许 {jsonish(const_value)}",
                                fragment=value)
                    return
        elif "const" in old:
            if old["const"] != const_value:
                builder.add(path, "const_changed",
                            f"常量从 {jsonish(old['const'])} 变为 {jsonish(const_value)}",
                            f"旧版 const 为 {jsonish(old['const'])}，新版为 {jsonish(const_value)}，"
                            f"旧数据无法通过新版校验",
                            fragment=old["const"])
        else:
            # 旧版不限制常量：尝试构造一个满足旧版其余约束、但不等于 const 的值
            candidates = _candidate_values(old)
            for value in candidates:
                if value == const_value:
                    continue
                doc, _ = embed_fragment(builder.source_root, path, value)
                if builder.verified_witness(doc):
                    builder.add(path, "const_added",
                                f"新版要求常量 {jsonish(const_value)}",
                                f"旧版不限制为常量 {jsonish(const_value)}，"
                                f"取值 {jsonish(value)} 的旧文档会被拒绝",
                                fragment=value)
                    return
            builder.add(path, "const_added",
                        f"新版要求常量 {jsonish(const_value)}",
                        f"旧版不限制为常量 {jsonish(const_value)}，其他取值可能被拒绝",
                        heuristic=True, candidate=minimal_instance(old))


def _candidate_values(old: dict) -> list:
    """为旧版 schema 生成若干可能不等于某个 const 的候选值。"""
    candidates: list = []
    ts = type_set(old)
    candidate_types = sorted(ts) if ts else ["string", "integer", "boolean", "object", "array", "null"]
    for candidate_type in candidate_types:
        if candidate_type == "number" and "integer" in candidate_types:
            continue
        candidates.append(typed_fragment(old, candidate_type))
    return candidates


def jsonish(value: Any) -> str:
    import json
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


def _check_types(old: dict, new: dict, path: tuple, builder: IssueBuilder,
                 source_validator, target_validator) -> None:
    old_types = type_set(old)
    new_types = type_set(new)
    if old_types is None and new_types is None:
        return
    if old_types is None:
        # source 任意类型：取一个 target 不接受的类型做探测
        for candidate_type in PRIMITIVE_TYPES:
            if candidate_type in new_types:
                continue
            fragment = typed_fragment(old, candidate_type)
            doc, _ = embed_fragment(builder.source_root, path, fragment)
            if builder.verified_witness(doc):
                builder.add(path, "type_restricted",
                            f"新版将类型限制为 {sorted(new_types)}，旧版允许任意类型",
                            f"旧版未声明 type，类型 {candidate_type} 的数据在新版会被拒绝",
                            fragment=fragment)
                return
        return
    if new_types is None:
        return
    removed = old_types - new_types
    # integer 是 number 的特化，单独标红更精确
    if "integer" in removed and "number" in new_types:
        removed.discard("integer")
    for removed_type in sorted(removed):
        fragment = typed_fragment(old, removed_type)
        builder.add(path, "type_removed",
                    f"新版不再接受类型 {removed_type}",
                    f"旧版允许类型 {removed_type}，新版仅允许 {sorted(new_types)}，"
                    f"该类型的旧数据会校验失败",
                    fragment=fragment)


def _check_object(old: dict, new: dict, path: tuple, builder: IssueBuilder,
                  source_root, target_root, source_validator, target_validator) -> None:
    old_props = old.get("properties", {})
    new_props = new.get("properties", {})
    old_ap = old.get("additionalProperties", True)
    new_ap = new.get("additionalProperties", True)

    # ---- 新增 required ----------------------------------------------------
    old_required = set(old.get("required", []))
    for name in new.get("required", []):
        if name in old_required:
            continue
        fragment = minimal_object(old)
        fragment.pop(name, None)
        builder.add(path, "required_added",
                    f"字段 {name!r} 在新版中变为必填",
                    f"旧版中 {name!r} 可以缺省，新版 required 包含该字段，"
                    f"缺少它的旧文档会校验失败",
                    fragment=fragment)

    # ---- 同名属性：递归比较 -----------------------------------------------
    for name in sorted(set(old_props) & set(new_props)):
        _check(old_props[name], new_props[name], path + (("p", name),), builder,
               source_root, target_root, source_validator, target_validator)

    # ---- 新版新增的属性：受旧版 additionalProperties 管制 -------------------
    for name in sorted(set(new_props) - set(old_props)):
        if old_ap is True:
            continue  # 旧版允许任意额外字段，新增可选属性不破坏兼容
        if old_ap is False:
            continue  # 旧版数据不可能带这个键，新增定义不收紧
        _check(old_ap, new_props[name], path + (("apn", name),), builder,
               source_root, target_root, source_validator, target_validator)

    # ---- 旧版有、新版删除的属性：受新版 additionalProperties 管制 -----------
    for name in sorted(set(old_props) - set(new_props)):
        if new_ap is True:
            continue
        if new_ap is False:
            fragment = minimal_object(old)
            fragment[name] = minimal_instance(old_props[name])
            builder.add(path, "property_no_longer_allowed",
                        f"字段 {name!r} 在新版中被移除且 additionalProperties 为 false",
                        f"旧版定义了 {name!r}，新版既未声明该属性也不允许额外字段，"
                        f"携带该字段的旧文档会校验失败",
                        fragment=fragment)
        else:
            _check(old_props[name], new_ap, path + (("apn", name),), builder,
                   source_root, target_root, source_validator, target_validator)

    # ---- additionalProperties 收紧 ----------------------------------------
    if new_ap is not True and not (old_ap is False):
        if old_ap is True and new_ap is False:
            fragment = minimal_object(old)
            fragment[extra_key(old, fragment)] = 1
            builder.add(path, "additional_properties_forbidden",
                        "新版将 additionalProperties 设为 false，禁止额外字段",
                        "旧版允许任意额外字段，新版会拒绝携带未声明字段的旧文档",
                        fragment=fragment)
        elif old_ap is True and is_schema(new_ap) and new_ap is not False:
            _check(True, new_ap, path + (("ap",),), builder,
                   source_root, target_root, source_validator, target_validator)
        elif is_schema(old_ap) and is_schema(new_ap):
            _check(old_ap, new_ap, path + (("ap",),), builder,
                   source_root, target_root, source_validator, target_validator)

    # ---- patternProperties（尽力而为）-------------------------------------
    old_pp = old.get("patternProperties", {})
    new_pp = new.get("patternProperties", {})
    for pattern, sub_schema in new_pp.items():
        if pattern in old_pp:
            _check(old_pp[pattern], sub_schema, path + (("pp", pattern),), builder,
                   source_root, target_root, source_validator, target_validator)
        elif old_ap is True:
            _check(True, sub_schema, path + (("pp", pattern),), builder,
                   source_root, target_root, source_validator, target_validator)

    # ---- minProperties / maxProperties ------------------------------------
    old_min = old.get("minProperties", 0) or 0
    new_min = new.get("minProperties", 0) or 0
    if new_min > old_min:
        fragment = minimal_object(old)
        while len(fragment) < new_min and len(fragment) < new_min + 5:
            fragment[extra_key(old, fragment)] = 1
        # fragment 故意只构造 old_min 水平：取一个旧版合法的小对象
        small = minimal_object(old)
        builder.add(path, "min_properties_increased",
                    f"新版要求至少 {new_min} 个属性（旧版 {old_min}）",
                    f"minProperties 从 {old_min} 提高到 {new_min}，属性更少的旧文档会被拒绝",
                    fragment=small)
    old_max = old.get("maxProperties")
    new_max = new.get("maxProperties")
    if new_max is not None and (old_max is None or new_max < old_max):
        fragment = minimal_object(old)
        target_size = max(new_max + 1, old_min)
        while len(fragment) < target_size:
            fragment[extra_key(old, fragment)] = 1
        doc, _ = embed_fragment(builder.source_root, path, fragment)
        if builder.verified_witness(doc):
            builder.add(path, "max_properties_decreased",
                        f"新版最多允许 {new_max} 个属性（旧版 {old_max}）",
                        f"maxProperties 从 {old_max} 降低到 {new_max}，属性更多的旧文档会被拒绝",
                        fragment=fragment)


def _check_array(old: dict, new: dict, path: tuple, builder: IssueBuilder) -> None:
    old_prefix, old_rest, old_rest_explicit = normalize_items(old)
    new_prefix, new_rest, new_rest_explicit = normalize_items(new)

    def recurse(s1, s2, frame):
        _check(s1, s2, path + frame, builder,
               builder.source_root, builder.target_root,
               builder.source_validator, builder.target_validator)

    # ---- 元素 / 前缀元素递归比较 ------------------------------------------
    if old_prefix is None and new_prefix is None:
        # 齐次数组 -> 齐次数组（或无 items 声明，rest 默认为 True）
        recurse(old_rest, new_rest, (("items",),))
    elif old_prefix is None and new_prefix is not None:
        # 旧版齐次/无约束 -> 新版元组
        for i, sub in enumerate(new_prefix):
            recurse(old_rest, sub, (("tup", i),))
        if new_rest_explicit:
            # 新版对超出前缀的元素有显式约束（含 false）
            recurse(old_rest, new_rest, (("items",),))
    elif old_prefix is not None and new_prefix is None:
        # 旧版元组 -> 新版齐次/无约束
        for i, sub in enumerate(old_prefix):
            recurse(sub, new_rest, (("tup", i),))
        if old_rest_explicit:
            recurse(old_rest, new_rest, (("items",),))
    else:
        # 元组 -> 元组：逐位置比较
        for i in range(max(len(old_prefix), len(new_prefix))):
            if i < len(old_prefix) and i < len(new_prefix):
                recurse(old_prefix[i], new_prefix[i], (("tup", i),))
            elif i < len(new_prefix):
                # 新版多出的前缀位置，受旧版 rest（可能是 false）管制
                recurse(old_rest, new_prefix[i], (("tup", i),))
            else:
                # 旧版多出的前缀位置，受新版 rest 管制
                recurse(old_prefix[i], new_rest, (("tup", i),))
        if old_rest_explicit and new_rest_explicit:
            recurse(old_rest, new_rest, (("items",),))

    # ---- minItems / maxItems ----------------------------------------------
    old_min = old.get("minItems", 0) or 0
    new_min = new.get("minItems", 0) or 0
    if new_min > old_min:
        fragment = minimal_array(old)  # 旧版最小长度数组，在新版可能过短
        builder.add(path, "min_items_increased",
                    f"新版要求数组至少 {new_min} 个元素（旧版 {old_min}）",
                    f"minItems 从 {old_min} 提高到 {new_min}，更短的旧数组会被拒绝",
                    fragment=fragment)
    old_max = old.get("maxItems")
    new_max = new.get("maxItems")
    if new_max is not None and (old_max is None or new_max < old_max):
        fragment = minimal_array(old)
        needed = max(new_max + 1, old_min)
        prefix, rest, _ = normalize_items(old)
        while len(fragment) < needed:
            j = len(fragment)
            if prefix and j < len(prefix):
                fragment.append(minimal_instance(prefix[j]))
            else:
                fragment.append(minimal_instance(rest))
        doc, _ = embed_fragment(builder.source_root, path, fragment)
        if builder.verified_witness(doc):
            builder.add(path, "max_items_decreased",
                        f"新版数组最多允许 {new_max} 个元素（旧版 {old_max}）",
                        f"maxItems 从 {old_max} 降低到 {new_max}，更长的旧数组会被拒绝",
                        fragment=fragment)

    # ---- uniqueItems ------------------------------------------------------
    if new.get("uniqueItems") is True and old.get("uniqueItems") is not True:
        prefix, rest, _ = normalize_items(old)
        element_schema = prefix[0] if prefix else rest
        value = minimal_instance(element_schema)
        candidate = [copy.deepcopy(value), copy.deepcopy(value)]
        doc, _ = embed_fragment(builder.source_root, path, candidate)
        if builder.verified_witness(doc):
            builder.add(path, "unique_items_added",
                        "新版要求数组元素唯一（uniqueItems）",
                        "旧版允许重复元素，开启 uniqueItems 后含重复元素的旧数组会被拒绝",
                        fragment=candidate)
        else:
            builder.add(path, "unique_items_added",
                        "新版要求数组元素唯一（uniqueItems）",
                        "旧版允许重复元素，开启 uniqueItems 可能拒绝部分旧数组",
                        heuristic=True, candidate=doc)



def _check_number(old: dict, new: dict, path: tuple, builder: IssueBuilder) -> None:
    old_lo, old_lo_ex = _lower_bound(old)
    new_lo, new_lo_ex = _lower_bound(new)

    # ---- 下界提高 / 排他化 -------------------------------------------------
    if new_lo is not None:
        if old_lo is None:
            candidate = new_lo - 1
        elif new_lo > old_lo or (new_lo == old_lo and new_lo_ex and not old_lo_ex):
            candidate = new_lo if (new_lo == old_lo and new_lo_ex) else new_lo - 1
        else:
            candidate = None
        if candidate is not None:
            doc, _ = embed_fragment(builder.source_root, path, candidate)
            if builder.verified_witness(doc):
                builder.add(path, "minimum_increased",
                            f"新版下界收紧为 {fmt_bound(new_lo, new_lo_ex)}",
                            f"最小值约束提高，旧版允许的 {candidate} 在新版会被拒绝",
                            fragment=candidate)

    # ---- 上界降低 / 排他化 -------------------------------------------------
    old_hi, old_hi_ex = _upper_bound(old)
    new_hi, new_hi_ex = _upper_bound(new)
    if new_hi is not None:
        if old_hi is None:
            candidate = new_hi + 1
        elif new_hi < old_hi or (new_hi == old_hi and new_hi_ex and not old_hi_ex):
            candidate = new_hi if (new_hi == old_hi and new_hi_ex) else new_hi + 1
        else:
            candidate = None
        if candidate is not None:
            doc, _ = embed_fragment(builder.source_root, path, candidate)
            if builder.verified_witness(doc):
                builder.add(path, "maximum_decreased",
                            f"新版上界收紧为 {fmt_bound(new_hi, new_hi_ex)}",
                            f"最大值约束降低，旧版允许的 {candidate} 在新版会被拒绝",
                            fragment=candidate)

    # ---- 新增整数约束 / multipleOf（启发式）--------------------------------
    if type_set(old) is None and type_set(new) and "integer" in type_set(new):
        doc, _ = embed_fragment(builder.source_root, path, 1.5)
        builder.add(path, "integer_required",
                    "新版要求 integer，旧版允许小数",
                    "旧版未限制为整数，小数值在新版会被拒绝",
                    heuristic=True, candidate=doc)

    if "multipleOf" in new and "multipleOf" not in old:
        step = new["multipleOf"]
        probe_values = [0.5, 1.5, 0.1, step / 2 if isinstance(step, (int, float)) else 0.5]
        for value in probe_values:
            doc, _ = embed_fragment(builder.source_root, path, value)
            if builder.verified_witness(doc):
                builder.add(path, "multiple_of_added",
                            f"新版要求数值是 {step} 的整数倍",
                            f"旧版无 multipleOf 约束，{value} 等不能被 {step} 整除的值会被拒绝",
                            fragment=value)
                return
        builder.add(path, "multiple_of_added",
                    f"新版新增 multipleOf: {step} 约束",
                    f"旧版无 multipleOf 约束，部分旧值可能无法被 {step} 整除",
                    heuristic=True, candidate=minimal_instance(old))


def _upper_bound(schema: dict) -> tuple[float | None, bool]:
    maximum = schema.get("maximum")
    exclusive = schema.get("exclusiveMaximum")
    if isinstance(exclusive, bool):
        return (float(maximum) if maximum is not None else None), exclusive
    if isinstance(exclusive, (int, float)):
        return float(exclusive), True
    return (float(maximum) if maximum is not None else None), False


def fmt_bound(value: float, exclusive: bool) -> str:
    rendered = str(int(value)) if float(value).is_integer() else str(value)
    return f"{'>' if exclusive else '>='} {rendered}"


def _check_string(old: dict, new: dict, path: tuple, builder: IssueBuilder) -> None:
    old_min = old.get("minLength", 0) or 0
    new_min = new.get("minLength", 0) or 0
    if new_min > old_min:
        fragment = "a" * max(new_min - 1, old_min)
        doc, _ = embed_fragment(builder.source_root, path, fragment)
        if builder.verified_witness(doc):
            builder.add(path, "min_length_increased",
                        f"新版要求字符串最短 {new_min}（旧版 {old_min}）",
                        f"minLength 从 {old_min} 提高到 {new_min}，更短的旧字符串会被拒绝",
                        fragment=fragment)
    old_max = old.get("maxLength")
    new_max = new.get("maxLength")
    if new_max is not None and (old_max is None or new_max < old_max):
        fragment = "a" * max(new_max + 1, old_min)
        doc, _ = embed_fragment(builder.source_root, path, fragment)
        if builder.verified_witness(doc):
            builder.add(path, "max_length_decreased",
                        f"新版字符串最长 {new_max}（旧版 {old_max}）",
                        f"maxLength 从 {old_max} 降低到 {new_max}，更长的旧字符串会被拒绝",
                        fragment=fragment)
    if new.get("pattern") and new.get("pattern") != old.get("pattern"):
        builder.add(path, "pattern_changed",
                    f"新版正则约束为 {new.get('pattern')!r}",
                    f"旧版 pattern 为 {old.get('pattern')!r}，新版正则可能拒绝部分旧字符串",
                    heuristic=True, candidate=minimal_instance(old))


def _check_composition(old: dict, new: dict, path: tuple, builder: IssueBuilder,
                       source_validator, target_validator) -> None:
    # ---- 新版引入 anyOf / oneOf：source 实例必须能命中至少一个分支 ----------
    for keyword in ("anyOf", "oneOf"):
        branches = new.get(keyword)
        if not isinstance(branches, list) or not branches:
            continue
        probes = [minimal_instance(old)]
        for old_keyword in ("anyOf", "oneOf"):
            for branch in old.get(old_keyword, []) or []:
                if is_schema(branch):
                    probes.append(minimal_instance(branch))
        hit = False
        for candidate in probes:
            doc, _ = embed_fragment(builder.source_root, path, candidate)
            if builder.verified_witness(doc):
                builder.add(path, f"{keyword}_branch_missing",
                            f"新版 {keyword} 没有任何分支能接受该旧版实例",
                            f"旧版允许的实例无法匹配新版 {keyword} 的任何分支，"
                            f"数据需要满足其中一个分支才合法",
                            fragment=candidate)
                hit = True
                break
        if not hit:
            builder.add(path, f"{keyword}_added",
                        f"新版新增 {keyword} 组合约束",
                        f"所有旧版实例都必须能匹配 {keyword} 的某个分支，"
                        f"结构上无法保证这一点",
                        heuristic=True, candidate=probes[0])

    # ---- 旧版 anyOf / oneOf 分支在新版整体下是否仍然合法 --------------------
    for keyword in ("anyOf", "oneOf"):
        for i, branch in enumerate(old.get(keyword, []) or []):
            if not is_schema(branch):
                continue
            candidate = minimal_instance(branch)
            doc, _ = embed_fragment(builder.source_root, path, candidate)
            if builder.verified_witness(doc):
                builder.add(path, f"{keyword}_branch_rejected",
                            f"旧版 {keyword} 第 {i} 个分支允许的实例被新版拒绝",
                            f"旧版 {keyword}[{i}] 的合法实例无法通过新版整体校验，"
                            f"该分支数据升级后会失败",
                            fragment=candidate)

    # ---- 新版新增 not ------------------------------------------------------
    if is_schema(new.get("not")) and new.get("not") != old.get("not"):
        candidate = minimal_instance(new["not"])  # 满足 not 内层 -> 必被 not 拒绝
        doc, _ = embed_fragment(builder.source_root, path, candidate)
        if builder.verified_witness(doc):
            builder.add(path, "not_added",
                        "新版新增 not 约束，旧版允许的部分实例被明确禁止",
                        "满足新版 not 内层 schema 的实例在旧版合法，却会被新版拒绝",
                        fragment=candidate)
        else:
            builder.add(path, "not_added",
                        "新版新增 not 约束",
                        "旧版部分实例可能命中新版 not 而被拒绝",
                        heuristic=True, candidate=minimal_instance(old))


# ---------------------------------------------------------------------------
# 样例驱动分析
# ---------------------------------------------------------------------------

def check_samples(samples: list | None, old: Any, new: Any) -> tuple[list[dict], list[dict]]:
    issues: list[dict] = []
    results: list[dict] = []
    if not samples:
        return issues, results

    old_validator = make_validator(old)
    new_validator = make_validator(new)
    for index, sample in enumerate(samples):
        old_errors = sorted(old_validator.iter_errors(sample), key=lambda e: list(e.absolute_path))
        new_errors = sorted(new_validator.iter_errors(sample), key=lambda e: list(e.absolute_path))
        old_ok, new_ok = not old_errors, not new_errors
        result = {"index": index, "valid_against_old": old_ok, "valid_against_new": new_ok}
        if old_ok and not new_ok:
            err = new_errors[0]
            result["direction"] = "backward"
            pointer = "#/" + "/".join(escape_pointer(str(p)) for p in err.absolute_path) \
                if err.absolute_path else "#"
            issues.append({
                "path": pointer, "code": "sample_rejected_by_new", "severity": "error",
                "message": f"提供的样例 #{index} 无法通过新版 schema 校验",
                "reason": f"新版校验错误：{err.message}",
                "counterexample": sample, "counterexample_path": pointer,
            })
        elif new_ok and not old_ok:
            err = old_errors[0]
            result["direction"] = "forward"
            pointer = "#/" + "/".join(escape_pointer(str(p)) for p in err.absolute_path) \
                if err.absolute_path else "#"
            issues.append({
                "path": pointer, "code": "sample_rejected_by_old", "severity": "error",
                "message": f"提供的样例 #{index} 无法通过旧版 schema 校验",
                "reason": f"旧版校验错误：{err.message}",
                "counterexample": sample, "counterexample_path": pointer,
            })
        elif not old_ok and not new_ok:
            result["direction"] = None
        else:
            result["direction"] = None
        results.append(result)
    return issues, results


# ---------------------------------------------------------------------------
# 顶层分析入口
# ---------------------------------------------------------------------------

DIRECTION_META = OrderedDict([
    ("backward", "向后兼容：新 schema 接受旧 schema 的全部实例（新数据读旧数据）"),
    ("forward", "向前兼容：旧 schema 接受新 schema 的全部实例（旧数据读新数据）"),
])


def analyze(old_schema: Any, new_schema: Any, samples: list | None = None) -> dict:
    old_resolved = resolve_refs(old_schema)
    new_resolved = resolve_refs(new_schema)

    backward = find_violations(old_resolved, new_resolved)
    forward = find_violations(new_resolved, old_resolved)

    sample_issues, sample_results = check_samples(samples, old_resolved, new_resolved)
    for issue in sample_issues:
        direction = "backward" if issue["code"] == "sample_rejected_by_new" else "forward"
        (backward if direction == "backward" else forward).append(issue)

    def decorate(issues: list[dict], direction: str) -> list[dict]:
        decorated = []
        for issue in issues:
            enriched = {"direction": direction, **issue}
            decorated.append(enriched)
        return decorated

    all_issues = sorted(
        decorate(backward, "backward") + decorate(forward, "forward"),
        key=lambda i: (i["direction"], i["path"], i["code"]),
    )

    def summary(issues: list[dict]) -> dict:
        return {
            "errors": sum(1 for i in issues if i["severity"] == "error"),
            "warnings": sum(1 for i in issues if i["severity"] == "warning"),
            "total": len(issues),
        }

    return {
        "backward_compatible": all(i["severity"] != "error" for i in backward),
        "forward_compatible": all(i["severity"] != "error" for i in forward),
        "directions": dict(DIRECTION_META),
        "summary": {
            "backward": summary(backward),
            "forward": summary(forward),
            "total": len(all_issues),
            "errors": sum(1 for i in all_issues if i["severity"] == "error"),
            "warnings": sum(1 for i in all_issues if i["severity"] == "warning"),
        },
        "issues": all_issues,
        "sample_results": sample_results,
    }
