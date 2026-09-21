"""分析器核心逻辑测试。"""

from app.analyzer import analyze


def paths(report, direction):
    return [v["path"] for v in report[direction]]


class TestType:
    def test_type_narrowing_breaks_backward(self):
        old = {"type": ["string", "integer"]}
        new = {"type": "string"}
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        assert report["forward_compatible"] is True
        v = report["backward_violations"][0]
        assert v["rule"] == "type"
        assert v["path"] == "$"
        assert v["counterexample"] == 0

    def test_integer_to_number_is_compatible(self):
        # integer ⊆ number：旧数据（整数）都能被新 schema（number）接受
        report = analyze({"type": "integer"}, {"type": "number"})
        assert report["backward_compatible"] is True
        # 反向不行：0.5 不是整数
        assert report["forward_compatible"] is False
        assert report["forward_violations"][0]["counterexample"] == 0.5


class TestRequired:
    def test_required_added_breaks_backward(self):
        old = {"type": "object", "properties": {"name": {"type": "string"}}}
        new = {
            "type": "object",
            "required": ["email"],
            "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
        }
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        v = report["backward_violations"][0]
        assert v["rule"] == "required"
        assert v["path"] == "$.email"
        assert v["counterexample"] == {}
        # 向前兼容：新数据多了 email，但旧 schema 允许额外属性
        assert report["forward_compatible"] is True

    def test_required_removed_breaks_forward(self):
        old = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
        new = {"type": "object", "properties": {"id": {"type": "integer"}}}
        report = analyze(old, new)
        assert report["backward_compatible"] is True
        assert report["forward_compatible"] is False
        assert report["forward_violations"][0]["path"] == "$.id"


class TestEnum:
    def test_enum_narrowed(self):
        old = {"type": "string", "enum": ["a", "b", "c"]}
        new = {"type": "string", "enum": ["a", "b"]}
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        assert report["backward_violations"][0]["counterexample"] == "c"
        assert report["forward_compatible"] is True

    def test_enum_widened(self):
        old = {"enum": ["a", "b"]}
        new = {"enum": ["a", "b", "c"]}
        report = analyze(old, new)
        assert report["backward_compatible"] is True
        assert report["forward_compatible"] is False

    def test_enum_added_where_none_before(self):
        report = analyze({"type": "string"}, {"type": "string", "enum": ["a", "b"]})
        assert report["backward_compatible"] is False
        assert report["backward_violations"][0]["rule"] == "enum"


class TestArray:
    def test_items_narrowed(self):
        old = {"type": "array", "items": {"type": ["string", "integer"]}}
        new = {"type": "array", "items": {"type": "string"}}
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        v = report["backward_violations"][0]
        assert v["path"] == "$[*]"
        assert v["rule"] == "type"

    def test_max_items_added(self):
        old = {"type": "array", "items": {"type": "integer"}}
        new = {"type": "array", "items": {"type": "integer"}, "maxItems": 2}
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        v = report["backward_violations"][0]
        assert v["rule"] == "maxItems"
        assert v["counterexample"] == [0, 0, 0]

    def test_min_items_added(self):
        old = {"type": "array"}
        new = {"type": "array", "minItems": 1}
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        assert report["backward_violations"][0]["counterexample"] == []

    def test_items_constraint_added(self):
        old = {"type": "array"}
        new = {"type": "array", "items": {"type": "string"}}
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        assert report["backward_violations"][0]["rule"] == "items"


class TestObject:
    def test_additional_properties_false(self):
        old = {"type": "object", "properties": {"a": {"type": "integer"}}}
        new = {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "additionalProperties": False,
        }
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        assert report["backward_violations"][0]["rule"] == "additionalProperties"

    def test_property_removed_with_closed_schema(self):
        old = {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
            "additionalProperties": False,
        }
        new = {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "additionalProperties": False,
        }
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        v = report["backward_violations"][0]
        assert v["path"] == "$.b"

    def test_nested_property_path(self):
        old = {"type": "object", "properties": {"user": {"type": "object", "properties": {"age": {"type": ["integer", "string"]}}}}}
        new = {"type": "object", "properties": {"user": {"type": "object", "properties": {"age": {"type": "integer"}}}}}
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        assert report["backward_violations"][0]["path"] == "$.user.age"


class TestCombinators:
    def test_anyof_branch_removed(self):
        old = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        new = {"anyOf": [{"type": "string"}]}
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        assert report["forward_compatible"] is True

    def test_allof_constraints_merged_on_source(self):
        # source 的 allOf 合并后 minLength=5，acceptor 要求 minLength=3 → 兼容
        old = {"allOf": [{"type": "string"}, {"minLength": 5}]}
        new = {"type": "string", "minLength": 3}
        report = analyze(old, new)
        assert report["backward_compatible"] is True

    def test_allof_on_acceptor(self):
        old = {"type": "string", "minLength": 1}
        new = {"allOf": [{"type": "string"}, {"minLength": 3}]}
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        assert report["backward_violations"][0]["rule"] == "minLength"

    def test_oneof_relaxed_to_anyof(self):
        old = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        new = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        report = analyze(old, new)
        assert report["backward_compatible"] is True


class TestNumericString:
    def test_minimum_raised(self):
        report = analyze({"type": "integer"}, {"type": "integer", "minimum": 10})
        assert report["backward_compatible"] is False
        v = report["backward_violations"][0]
        assert v["rule"] == "minimum"
        assert v["counterexample"] == 9

    def test_maxlength_shortened(self):
        report = analyze({"type": "string", "maxLength": 10}, {"type": "string", "maxLength": 5})
        assert report["backward_compatible"] is False
        assert report["backward_violations"][0]["counterexample"] == "xxxxxx"

    def test_pattern_added(self):
        report = analyze({"type": "string"}, {"type": "string", "pattern": "^\\d+$"})
        assert report["backward_compatible"] is False
        assert report["backward_violations"][0]["rule"] == "pattern"


class TestRefAndSamples:
    def test_local_ref_resolved(self):
        old = {
            "type": "object",
            "properties": {"pet": {"$ref": "#/definitions/pet"}},
            "definitions": {"pet": {"type": ["string", "integer"]}},
        }
        new = {
            "type": "object",
            "properties": {"pet": {"$ref": "#/definitions/pet"}},
            "definitions": {"pet": {"type": "string"}},
        }
        report = analyze(old, new)
        assert report["backward_compatible"] is False
        assert report["backward_violations"][0]["path"] == "$.pet"

    def test_samples_validated_against_both(self):
        old = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
        new = {
            "type": "object",
            "required": ["id", "email"],
            "properties": {"id": {"type": "integer"}, "email": {"type": "string"}},
        }
        report = analyze(old, new, samples=[{"id": 1}, {"id": 2, "email": "a@b.c"}])
        results = report["sample_results"]
        assert len(results) == 2
        assert results[0]["valid_against_old"] is True
        assert results[0]["valid_against_new"] is False
        assert results[1]["valid_against_new"] is True

    def test_boolean_schemas(self):
        report = analyze({}, True)
        assert report["backward_compatible"] is True
        report2 = analyze(True, {"type": "string"})
        assert report2["backward_compatible"] is False

    def test_identical_schemas_fully_compatible(self):
        schema = {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "minimum": 0},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        }
        report = analyze(schema, schema)
        assert report["backward_compatible"] is True
        assert report["forward_compatible"] is True
        assert "完全兼容" in report["summary"]
