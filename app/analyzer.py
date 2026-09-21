"""JSON Schema 兼容性分析核心逻辑。

兼容性定义（与主流 Schema Registry 一致）：

- 向后兼容（backward）：新 schema 能读取旧 schema 产出的数据，
  即「旧 schema 接受的实例集合」⊆「新 schema 接受的实例集合」。
- 向前兼容（forward）：旧 schema 能读取新 schema 产出的数据，
  即「新 schema 接受的实例集合」⊆「旧 schema 接受的实例集合」。

两个方向归结为同一个判定：source 接受的实例是否都被 acceptor 接受。
本模块通过递归的结构化比较给出答案，并在不满足时生成最小反例
（一个被 source 接受、却被 acceptor 拒绝的实例）及其 JSON 路径。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

import jsonschema

MAX_DEPTH = 40
MAX_VIOLATIONS = 50

#: 反例缺失的哨兵值（None 本身可能是合法反例，如 null 实例）
_MISSING = object()

#: 各 JSON 类型的最小实例。number 取 0.5 以区别于 integer。
_MINIMAL_BY_TYPE = {
    "null": None,
    "boolean": False,
    "integer": 0,
    "number": 0.5,
    "string": "",
    "array": [],
    "object": {},
}

#: 构造反例时的候选实例
_CANDIDATES = [
    None, False, True, 0, 1, -1, 0.5, "", "x", "lorem",
    [], [0], [None, None], {}, {"a": 1},
]

#: 不参与比较的关键字（遇到时记入 notes）
_IGNORED_KEYWORDS = (
    "not", "if", "then", "else", "patternProperties", "contains",
    "propertyNames", "dependentRequired", "dependentSchemas",
    "multipleOf", "format",
)


@dataclass
class Violation:
    """一条兼容性违规：路径 + 规则 + 原因 + 最小反例。"""

    path: str
    rule: str
    reason: str
    counterexample: Any = _MISSING

    def to_dict(self) -> dict:
        d = {"path": self.path, "rule": self.rule, "reason": self.reason}
        if self.counterexample is not _MISSING:
            d["counterexample"] = self.counterexample
        return d


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _json_equal(x: Any, y: Any) -> bool:
    """JSON 值相等：1 与 1.0 相等，但 true 与 1 不相等。"""
    if isinstance(x, bool) != isinstance(y, bool):
        return False
    try:
        return bool(x == y)
    except Exception:
        return False


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _types_of(schema: Any) -> Optional[set]:
    """schema 明确允许的类型集合；无法确定时返回 None（可能接受任意类型）。

    只从 type / enum / const 推断——properties、items 等关键字并不限制类型。
    """
    if isinstance(schema, bool):
        return None if schema else set()
    if not isinstance(schema, dict):
        return None
    t = schema.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    if "const" in schema:
        return {_json_type(schema["const"])}
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return {_json_type(v) for v in schema["enum"]}
    return None


def _accepts_type(types: Optional[set], t: str) -> bool:
    """types 为 None 表示未知（按可能接受处理）；integer 被 number 覆盖。"""
    if types is None:
        return True
    return t in types or (t == "integer" and "number" in types)


def _child_path(path: str, prop: str) -> str:
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", prop):
        return f"{path}.{prop}"
    return f"{path}[{json.dumps(prop, ensure_ascii=False)}]"


def _validates(schema: Any, instance: Any) -> bool:
    try:
        jsonschema.validate(instance, schema)
        return True
    except jsonschema.ValidationError:
        return False
    except Exception:
        return True  # 引用无法解析等情况，乐观处理


def _resolve_pointer(root: Any, ref: str) -> Any:
    """解析本地 JSON Pointer 引用（#/...）。"""
    if ref == "#":
        return root
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    node = root
    for raw in ref[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        else:
            return None
    return node


class _Resolver:
    """针对单个 schema 文档的本地 $ref 解析器（带循环保护）。"""

    def __init__(self, root: Any):
        self.root = root

    def resolve(self, schema: Any, seen: frozenset = frozenset()) -> Any:
        while isinstance(schema, dict) and "$ref" in schema:
            ref = schema["$ref"]
            if ref in seen:
                return {k: v for k, v in schema.items() if k != "$ref"} or {}
            seen = seen | {ref}
            target = _resolve_pointer(self.root, ref)
            if target is None:
                return {k: v for k, v in schema.items() if k != "$ref"} or {}
            siblings = {k: v for k, v in schema.items() if k != "$ref"}
            if siblings and isinstance(target, dict):
                merged = dict(target)
                merged.update(siblings)
                schema = merged
            else:
                schema = target
        return schema


class _Ctx:
    """收集分析过程中的提示信息（自动去重）。"""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self._seen: set[str] = set()

    def note(self, msg: str) -> None:
        if msg not in self._seen:
            self._seen.add(msg)
            self.notes.append(msg)


# ---------------------------------------------------------------------------
# allOf 合并（用于 source 侧：交集约束合并为等效 schema，减少误报）
# ---------------------------------------------------------------------------

def _merge_two(x: Any, y: Any) -> Any:
    if x is True or x == {}:
        return y
    if y is True or y == {}:
        return x
    if not isinstance(x, dict) or not isinstance(y, dict):
        return x
    out = dict(x)
    for k, v in y.items():
        if k not in out:
            out[k] = v
        elif k == "required" and isinstance(out[k], list) and isinstance(v, list):
            out[k] = sorted(set(out[k]) | set(v))
        elif k == "properties" and isinstance(out[k], dict) and isinstance(v, dict):
            merged = dict(out[k])
            for p, sub in v.items():
                merged[p] = _merge_two(merged[p], sub) if p in merged else sub
            out[k] = merged
        elif k == "type":
            tx = out[k] if isinstance(out[k], list) else [out[k]]
            ty = v if isinstance(v, list) else [v]
            inter = [t for t in tx if t in ty]
            out[k] = inter[0] if len(inter) == 1 else inter
        elif k in ("minimum", "minLength", "minItems", "minProperties"):
            if isinstance(out[k], (int, float)) and isinstance(v, (int, float)):
                out[k] = max(out[k], v)
        elif k in ("maximum", "maxLength", "maxItems", "maxProperties"):
            if isinstance(out[k], (int, float)) and isinstance(v, (int, float)):
                out[k] = min(out[k], v)
        elif k == "enum" and isinstance(out[k], list) and isinstance(v, list):
            out[k] = [e for e in out[k] if any(_json_equal(e, w) for w in v)]
        elif k == "items":
            out[k] = _merge_two(out[k], v)
        elif k == "allOf" and isinstance(out[k], list) and isinstance(v, list):
            out[k] = out[k] + v
        # 其余关键字保留第一个（best effort）
    return out


def _merge_all(parts: list) -> Any:
    result: Any = {}
    for part in parts:
        result = _merge_two(result, part)
    return result


# ---------------------------------------------------------------------------
# 最小实例构造
# ---------------------------------------------------------------------------

def minimal_instance(schema: Any, resolver: Optional[_Resolver] = None, depth: int = 0) -> Any:
    """构造满足 schema 的最小实例（best effort）。"""
    if depth > MAX_DEPTH:
        return None
    if resolver is not None:
        schema = resolver.resolve(schema)
    if not isinstance(schema, dict):
        return None  # true/空 schema → 任意实例，取 null；false → 退化为 null
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    if isinstance(schema.get("allOf"), list) and schema["allOf"]:
        parts = [{k: v for k, v in schema.items() if k != "allOf"}]
        parts += [resolver.resolve(m) if resolver else m for m in schema["allOf"]]
        return minimal_instance(_merge_all(parts), resolver, depth + 1)
    for key in ("anyOf", "oneOf"):
        if isinstance(schema.get(key), list) and schema[key]:
            return minimal_instance(schema[key][0], resolver, depth + 1)

    types = _types_of(schema)
    t = None
    if types:
        for cand in ("object", "array", "string", "integer", "number", "boolean", "null"):
            if _accepts_type(types, cand):
                t = cand
                break
    if t is None:
        if "properties" in schema or "required" in schema:
            t = "object"
        elif "items" in schema:
            t = "array"
        else:
            return None

    if t == "object":
        return _minimal_object(schema, resolver, depth)
    if t == "array":
        n = schema.get("minItems", 0)
        n = n if isinstance(n, int) and 0 <= n <= 5 else 0
        item_schema = schema.get("items", {})
        return [minimal_instance(item_schema, resolver, depth + 1) for _ in range(n)]
    if t == "string":
        n = schema.get("minLength", 0)
        return "x" * n if isinstance(n, int) and 0 < n <= 64 else ""
    if t in ("integer", "number"):
        lo = schema.get("minimum")
        if isinstance(lo, (int, float)) and not isinstance(lo, bool):
            if t == "integer":
                import math
                return int(math.ceil(lo))
            return lo
        return 0
    return _MINIMAL_BY_TYPE[t]


def _minimal_object(schema: Any, resolver: Optional[_Resolver], depth: int = 0) -> dict:
    """满足 schema 的最小对象：只包含 required 属性。"""
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    obj = {}
    for p in schema.get("required") or []:
        if isinstance(p, str):
            obj[p] = minimal_instance(props.get(p, {}), resolver, depth + 1)
    return obj


def _instance_violating(schema: Any, resolver: _Resolver) -> Any:
    """找一个不满足 schema 的候选实例；找不到返回 _MISSING。"""
    resolved = resolver.resolve(schema)
    for cand in _CANDIDATES:
        if not _validates(resolved, cand):
            return cand
    return _MISSING


# ---------------------------------------------------------------------------
# 数值 / 字符串边界
# ---------------------------------------------------------------------------

def _bounds(schema: Any):
    """提取数值上下界，返回 ((lo, lo_inclusive), (hi, hi_inclusive))。"""
    lo = (None, True)
    hi = (None, True)
    if not isinstance(schema, dict):
        return lo, hi

    def _num(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    if _num(schema.get("minimum")):
        lo = (schema["minimum"], schema.get("exclusiveMinimum") is not True)
    if _num(schema.get("exclusiveMinimum")):
        v = schema["exclusiveMinimum"]
        if lo[0] is None or v > lo[0] or (v == lo[0] and lo[1]):
            lo = (v, False)
    if _num(schema.get("maximum")):
        hi = (schema["maximum"], schema.get("exclusiveMaximum") is not True)
    if _num(schema.get("exclusiveMaximum")):
        v = schema["exclusiveMaximum"]
        if hi[0] is None or v < hi[0] or (v == hi[0] and hi[1]):
            hi = (v, False)
    return lo, hi


# ---------------------------------------------------------------------------
# 核心比较器
# ---------------------------------------------------------------------------

class _Checker:
    """判断 source 接受的实例是否都被 acceptor 接受，并收集违规。"""

    def __init__(self, acceptor_root: Any, source_root: Any):
        self.ra = _Resolver(acceptor_root)
        self.rs = _Resolver(source_root)
        self.ctx = _Ctx()

    # -- 主入口 -----------------------------------------------------------

    def violations(self, acceptor: Any, source: Any, path: str = "$", depth: int = 0) -> list:
        out: list[Violation] = []
        if depth > MAX_DEPTH:
            self.ctx.note("schema 嵌套过深或存在循环引用，超出部分未完全检查")
            return out

        a = self.ra.resolve(acceptor)
        s = self.rs.resolve(source)

        # 布尔 schema 规约
        if a is True:
            return out
        if s is False:
            return out  # source 不接受任何实例，必然被包含
        if a is False:
            out.append(Violation(
                path, "boolean_schema",
                "acceptor 为 false，拒绝所有实例，而 source 允许实例存在",
                minimal_instance(s, self.rs),
            ))
            return out
        if s is True:
            s = {}
        if not isinstance(a, dict) or not isinstance(s, dict):
            return out

        # source 的 allOf 是交集约束：合并为等效 schema 再比较，减少误报
        s = self._effective_source(s)

        for key in _IGNORED_KEYWORDS:
            if key in a or key in s:
                self.ctx.note(f"关键字 '{key}' 未参与比较，相关约束可能被忽略")

        out += self._check_enum(a, s, path)
        out += self._check_type(a, s, path)
        out += self._check_numeric(a, s, path)
        out += self._check_string(a, s, path)
        out += self._check_object(a, s, path, depth)
        out += self._check_array(a, s, path, depth)
        out += self._check_combinators(a, s, path, depth)
        return out

    def _effective_source(self, s: dict) -> dict:
        for _ in range(10):
            if isinstance(s.get("allOf"), list) and s["allOf"]:
                parts = [{k: v for k, v in s.items() if k != "allOf"}]
                parts += [self.rs.resolve(m) for m in s["allOf"]]
                s = _merge_all(parts)
            else:
                break
        return s

    # -- 枚举 / 常量 -------------------------------------------------------

    def _check_enum(self, a: dict, s: dict, path: str) -> list:
        a_vals = _enum_values(a)
        if a_vals is None:
            return []
        s_vals = _enum_values(s)
        if s_vals is not None:
            excluded = [v for v in s_vals if not any(_json_equal(v, av) for av in a_vals)]
            if excluded:
                return [Violation(
                    path, "enum",
                    f"source 允许的枚举值 {json.dumps(excluded[0], ensure_ascii=False)} "
                    f"不在 acceptor 的枚举范围内",
                    excluded[0],
                )]
            return []
        # source 无枚举约束：找一个 source 接受但不在 acceptor 枚举内的值
        for cand in [minimal_instance(s, self.rs)] + _CANDIDATES:
            if _validates(s, cand) and not any(_json_equal(cand, av) for av in a_vals):
                return [Violation(
                    path, "enum",
                    "acceptor 限定了枚举/常量取值，而 source 允许其他值",
                    cand,
                )]
        return []

    # -- 类型 --------------------------------------------------------------

    def _check_type(self, a: dict, s: dict, path: str) -> list:
        a_types = _types_of(a)
        if not a_types:
            return []
        s_types = _types_of(s)
        if s_types is None:
            # source 未约束类型：找一个 acceptor 不接受、但 source 接受的实例
            for t in ("integer", "string", "boolean", "null", "array", "object", "number"):
                if not _accepts_type(a_types, t):
                    inst = _MINIMAL_BY_TYPE[t]
                    if _validates(s, inst):
                        return [Violation(
                            path, "type",
                            f"source 未约束类型，可产生 acceptor 不接受的 '{t}' 类型实例",
                            inst,
                        )]
            return []
        for t in sorted(s_types):
            if not _accepts_type(a_types, t):
                return [Violation(
                    path, "type",
                    f"source 允许类型 '{t}'，acceptor 只允许 {sorted(a_types)}",
                    _MINIMAL_BY_TYPE.get(t),
                )]
        return []

    # -- 数值 --------------------------------------------------------------

    def _check_numeric(self, a: dict, s: dict, path: str) -> list:
        if not _accepts_type(_types_of(s), "number") and not _accepts_type(_types_of(s), "integer"):
            return []
        if not _accepts_type(_types_of(a), "number") and not _accepts_type(_types_of(a), "integer"):
            return []
        (a_lo, a_lo_inc), (a_hi, a_hi_inc) = _bounds(a)
        (s_lo, s_lo_inc), (s_hi, s_hi_inc) = _bounds(s)
        out = []
        if a_lo is not None:
            if s_lo is None:
                out.append(Violation(
                    path, "minimum",
                    f"acceptor 新增下界 {a_lo}，source 允许更小的数值", a_lo - 1))
            elif s_lo < a_lo or (s_lo == a_lo and s_lo_inc and not a_lo_inc):
                out.append(Violation(
                    path, "minimum",
                    f"acceptor 的下界 {a_lo} 严于 source 的下界 {s_lo}", s_lo))
        if a_hi is not None:
            if s_hi is None:
                out.append(Violation(
                    path, "maximum",
                    f"acceptor 新增上界 {a_hi}，source 允许更大的数值", a_hi + 1))
            elif s_hi > a_hi or (s_hi == a_hi and s_hi_inc and not a_hi_inc):
                out.append(Violation(
                    path, "maximum",
                    f"acceptor 的上界 {a_hi} 严于 source 的上界 {s_hi}", s_hi))
        return out

    # -- 字符串 ------------------------------------------------------------

    def _check_string(self, a: dict, s: dict, path: str) -> list:
        if not _accepts_type(_types_of(s), "string") or not _accepts_type(_types_of(a), "string"):
            return []
        out = []
        a_min = a.get("minLength")
        s_min = s.get("minLength")
        if isinstance(a_min, int) and a_min > (s_min if isinstance(s_min, int) else 0):
            n = s_min if isinstance(s_min, int) else 0
            out.append(Violation(
                path, "minLength",
                f"acceptor 要求最小长度 {a_min}，source 允许长度 {n}", "x" * n))
        a_max = a.get("maxLength")
        s_max = s.get("maxLength")
        if isinstance(a_max, int) and (not isinstance(s_max, int) or a_max < s_max):
            out.append(Violation(
                path, "maxLength",
                f"acceptor 要求最大长度 {a_max}，source 允许更长字符串", "x" * (a_max + 1)))
        a_pat = a.get("pattern")
        if isinstance(a_pat, str) and a_pat != s.get("pattern"):
            ce = _MISSING
            try:
                rx = re.compile(a_pat)
                for cand in ["", "x", "a", "0", " ", "xyz", "lorem ipsum"]:
                    if not rx.search(cand) and _validates(s, cand):
                        ce = cand
                        break
            except re.error:
                pass
            v = Violation(path, "pattern", f"acceptor 新增/收紧了 pattern 约束 {a_pat!r}")
            if ce is not _MISSING:
                v.counterexample = ce
            out.append(v)
        return out

    # -- 对象 --------------------------------------------------------------

    def _check_object(self, a: dict, s: dict, path: str, depth: int) -> list:
        if not _accepts_type(_types_of(s), "object") or not _accepts_type(_types_of(a), "object"):
            return []
        out = []
        a_props = a.get("properties") if isinstance(a.get("properties"), dict) else {}
        s_props = s.get("properties") if isinstance(s.get("properties"), dict) else {}
        a_req = {p for p in (a.get("required") or []) if isinstance(p, str)}
        s_req = {p for p in (s.get("required") or []) if isinstance(p, str)}

        # 1. 新增必填字段
        for prop in sorted(a_req - s_req):
            out.append(Violation(
                _child_path(path, prop), "required",
                f"acceptor 将属性 '{prop}' 标记为必填，而 source 允许其缺失",
                _minimal_object(s, self.rs, depth),
            ))

        # 2. 已有属性的子 schema 递归比较
        for prop, s_sub in s_props.items():
            child = _child_path(path, prop)
            if prop in a_props:
                out += self.violations(a_props[prop], s_sub, child, depth + 1)
            else:
                ap = a.get("additionalProperties", True)
                if ap is False:
                    ce = _minimal_object(s, self.rs, depth)
                    ce[prop] = minimal_instance(s_sub, self.rs, depth + 1)
                    out.append(Violation(
                        child, "additionalProperties",
                        f"acceptor 不允许属性 '{prop}'（additionalProperties: false），source 允许",
                        ce,
                    ))
                elif ap is not True:
                    out += self.violations(ap, s_sub, child, depth + 1)

        # 3. additionalProperties 整体收紧
        a_ap = a.get("additionalProperties", True)
        s_ap = s.get("additionalProperties", True)
        s_extra = [p for p in s_props if p not in a_props]
        if a_ap is False and not s_extra and s_ap is not False:
            ce = _minimal_object(s, self.rs, depth)
            ce["extraProp"] = None
            out.append(Violation(
                f"{path}.*", "additionalProperties",
                "acceptor 设置 additionalProperties: false，source 允许任意额外属性",
                ce,
            ))
        elif isinstance(a_ap, dict) and s_ap is not False:
            out += self.violations(a_ap, s_ap if isinstance(s_ap, dict) else {},
                                   f"{path}.*", depth + 1)

        # 4. 属性数量约束
        a_minp, s_minp = a.get("minProperties"), s.get("minProperties")
        if isinstance(a_minp, int) and a_minp > (s_minp if isinstance(s_minp, int) else 0):
            out.append(Violation(
                path, "minProperties",
                f"acceptor 要求至少 {a_minp} 个属性，source 允许更少",
                _minimal_object(s, self.rs, depth)))
        a_maxp, s_maxp = a.get("maxProperties"), s.get("maxProperties")
        if isinstance(a_maxp, int) and (not isinstance(s_maxp, int) or a_maxp < s_maxp):
            ce = _minimal_object(s, self.rs, depth)
            i = 0
            while len(ce) < a_maxp + 1:
                key = f"extra{i}"
                i += 1
                if key not in ce:
                    ce[key] = None
            out.append(Violation(
                path, "maxProperties",
                f"acceptor 最多允许 {a_maxp} 个属性，source 允许更多", ce))
        return out

    # -- 数组 --------------------------------------------------------------

    def _check_array(self, a: dict, s: dict, path: str, depth: int) -> list:
        if not _accepts_type(_types_of(s), "array") or not _accepts_type(_types_of(a), "array"):
            return []
        out = []
        s_items = s.get("items")
        a_items = a.get("items")
        item_path = f"{path}[*]"
        if s_items is not None:
            if a_items is not None:
                out += self.violations(a_items, s_items, item_path, depth + 1)
            # acceptor 无 items 约束 → 无违规
        elif a_items not in (None, True, {}):
            ce = _instance_violating(a_items, self.ra)
            if ce is not _MISSING:
                out.append(Violation(
                    item_path, "items",
                    "acceptor 约束了数组元素，而 source 对元素无约束", [ce]))

        a_min = a.get("minItems")
        s_min = s.get("minItems")
        if isinstance(a_min, int) and a_min > (s_min if isinstance(s_min, int) else 0):
            n = s_min if isinstance(s_min, int) else 0
            item = minimal_instance(s_items, self.rs, depth + 1) if s_items is not None else None
            out.append(Violation(
                path, "minItems",
                f"acceptor 要求至少 {a_min} 个元素，source 允许 {n} 个",
                [item] * n))
        a_max = a.get("maxItems")
        s_max = s.get("maxItems")
        if isinstance(a_max, int) and (not isinstance(s_max, int) or a_max < s_max):
            item = minimal_instance(s_items, self.rs, depth + 1) if s_items is not None else None
            out.append(Violation(
                path, "maxItems",
                f"acceptor 最多允许 {a_max} 个元素，source 允许更多",
                [item] * (a_max + 1)))
        if a.get("uniqueItems") is True and s.get("uniqueItems") is not True:
            item = minimal_instance(s_items, self.rs, depth + 1) if s_items is not None else None
            out.append(Violation(
                path, "uniqueItems",
                "acceptor 要求数组元素唯一，source 允许重复", [item, item]))
        return out

    # -- 组合关键字 ----------------------------------------------------------

    def _check_combinators(self, a: dict, s: dict, path: str, depth: int) -> list:
        out = []
        # acceptor 的 allOf：每个成员都是独立约束
        a_all = a.get("allOf")
        if isinstance(a_all, list):
            for sub in a_all:
                out += self.violations(self.ra.resolve(sub), s, path, depth + 1)

        # source 的 anyOf/oneOf 是并集：acceptor 必须包含每个分支。
        # 逐分支检查后，source 整体不再参与 acceptor 并集判定（避免误报）。
        s_is_union = False
        for key in ("anyOf", "oneOf"):
            s_sub = s.get(key)
            if isinstance(s_sub, list) and s_sub:
                s_is_union = True
                for sub in s_sub:
                    out += self.violations(a, self.rs.resolve(sub), path, depth + 1)

        if s_is_union:
            return out

        # acceptor 的 anyOf/oneOf 是并集：source 需被某一分支完全包含
        for key in ("anyOf", "oneOf"):
            a_sub = a.get(key)
            if isinstance(a_sub, list) and a_sub:
                if key == "oneOf":
                    self.ctx.note("acceptor 的 oneOf 按 anyOf 近似比较")
                branch_results = [
                    self.violations(self.ra.resolve(sub), s, path, depth + 1)
                    for sub in a_sub
                ]
                if all(branch_results):
                    out.append(Violation(
                        path, key,
                        f"source 未被 acceptor 的 {key} 任一分支完全包含",
                        minimal_instance(s, self.rs),
                    ))
        return out


def _enum_values(schema: Any) -> Optional[list]:
    if not isinstance(schema, dict):
        return None
    if "const" in schema:
        return [schema["const"]]
    if isinstance(schema.get("enum"), list):
        return schema["enum"]
    return None


# ---------------------------------------------------------------------------
# 样例校验
# ---------------------------------------------------------------------------

def _validation_errors(schema: Any, instance: Any) -> list:
    if isinstance(schema, bool):
        return [] if schema else ["schema 为 false，拒绝所有实例"]
    try:
        validator = jsonschema.validators.validator_for(schema)(schema)
        errors = []
        for e in validator.iter_errors(instance):
            loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
            errors.append(f"{loc}: {e.message}")
        return errors
    except Exception as exc:
        return [f"校验失败: {exc}"]


def _check_samples(samples: list, old_schema: Any, new_schema: Any) -> list:
    results = []
    for i, sample in enumerate(samples):
        old_errors = _validation_errors(old_schema, sample)
        new_errors = _validation_errors(new_schema, sample)
        results.append({
            "index": i,
            "valid_against_old": not old_errors,
            "valid_against_new": not new_errors,
            "old_errors": old_errors[:5],
            "new_errors": new_errors[:5],
        })
    return results


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------

def _dedup(violations: list) -> list:
    seen = set()
    out = []
    for v in violations:
        key = (v.path, v.rule, v.reason)
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _summary(backward_ok: bool, forward_ok: bool) -> str:
    if backward_ok and forward_ok:
        return "完全兼容：新旧 schema 双向兼容"
    if backward_ok:
        return "仅向后兼容：新 schema 可读取旧数据，但旧 schema 无法读取全部新数据"
    if forward_ok:
        return "仅向前兼容：旧 schema 可读取新数据，但新 schema 无法读取全部旧数据"
    return "不兼容：两个方向均存在破坏性变更"


def analyze(old_schema: Any, new_schema: Any, samples: Optional[list] = None) -> dict:
    """比较 old_schema 与 new_schema，返回完整兼容性报告。"""
    # 向后兼容：旧数据必须被新 schema 接受（old ⊆ new）
    backward_checker = _Checker(acceptor_root=new_schema, source_root=old_schema)
    backward = _dedup(backward_checker.violations(new_schema, old_schema))

    # 向前兼容：新数据必须被旧 schema 接受（new ⊆ old）
    forward_checker = _Checker(acceptor_root=old_schema, source_root=new_schema)
    forward = _dedup(forward_checker.violations(old_schema, new_schema))

    notes = sorted(set(backward_checker.ctx.notes + forward_checker.ctx.notes))
    if len(backward) > MAX_VIOLATIONS:
        backward = backward[:MAX_VIOLATIONS]
        notes.append(f"向后兼容违规过多，仅展示前 {MAX_VIOLATIONS} 条")
    if len(forward) > MAX_VIOLATIONS:
        forward = forward[:MAX_VIOLATIONS]
        notes.append(f"向前兼容违规过多，仅展示前 {MAX_VIOLATIONS} 条")

    backward_ok = not backward
    forward_ok = not forward
    return {
        "backward_compatible": backward_ok,
        "forward_compatible": forward_ok,
        "summary": _summary(backward_ok, forward_ok),
        "backward_violations": [v.to_dict() for v in backward],
        "forward_violations": [v.to_dict() for v in forward],
        "sample_results": _check_samples(samples or [], old_schema, new_schema),
        "notes": notes,
    }
