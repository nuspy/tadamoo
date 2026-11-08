"""A parameter of a skill is set by the person, by a Python expression or by the model from a description; the ones the model sets are set in ONE call at the start, and
the ones that depend on later steps are left to the script."""
from datetime import datetime

import pytest

from tadamoo.personal_skills import distill, expressions
from tadamoo.personal_skills.values import check_param, check_spec, resolve

NOW = datetime(2026, 11, 8, 14, 30, 5)


def spec(name, **kw):
    return {"name": name, "type": "text", "label": name, **kw}


class TestExpressions:
    def test_the_moment_and_what_is_made_of_it(self):
        assert expressions.evaluate("now()", NOW) == NOW
        assert expressions.evaluate('now().strftime("%Y-%m-%d %H:%M")', NOW) == "2026-11-08 14:30"
        assert expressions.evaluate("today() - timedelta(days=1)", NOW).isoformat() == "2026-11-07"
        assert expressions.evaluate('params["city"].title() + "!"', NOW, {"city": "rome"}) == "Rome!"
        assert expressions.evaluate("[1, 2, 3][1] + round(2.6)", NOW) == 5

    @pytest.mark.parametrize("bad", ["__import__('os').system('x')", "open('f')", "now().__class__", "2 ** 100", "'a' * 100000000", "lambda: 1", "[x for x in range(3)]",
                                     "params.__dict__", "os.getcwd()", "now(", "", "x = 1", "eval('1')", "(1).real"])
    def test_nothing_else_is_allowed(self, bad):
        with pytest.raises(expressions.ExpressionError):
            expressions.evaluate(bad, NOW, {})

    def test_a_result_that_is_not_plain_data_is_refused_and_a_runtime_failure_is_said(self):
        with pytest.raises(expressions.ExpressionError, match="must be text"):
            expressions.evaluate("timedelta(days=1)", NOW)
        with pytest.raises(expressions.ExpressionError, match="ZeroDivisionError"):
            expressions.evaluate("1 / 0", NOW)
        with pytest.raises(expressions.ExpressionError, match="KeyError"):
            expressions.evaluate('params["nobody"]', NOW, {})


class TestTheParameterCard:
    def test_an_expression_is_tried_and_what_it_gives_fits_the_type_or_is_refused(self):
        assert check_param(spec("when", type="datetime", source="python", expr="now()"), NOW) == {"ok": True, "value": "2026-11-08T14:30:05", "error": ""}
        assert check_param(spec("day", type="date", source="python", expr="today() - timedelta(days=1)"), NOW)["value"] == "2026-11-07"
        bad = check_param(spec("n", type="number", source="python", expr='"abc"'), NOW)
        assert not bad["ok"] and "number" in bad["error"]
        assert not check_param(spec("x", source="python", expr="import os"), NOW)["ok"]

    def test_a_definition_is_checked_for_its_mode(self):
        assert check_spec(spec("a", source="python", expr="now()", type="datetime")) == []
        assert check_spec(spec("a", source="python", expr='params["b"].upper()')) == []                       # reads another parameter: tried when the run has it
        assert any("does not work" in p for p in check_spec(spec("a", source="python", expr="open('f')")))
        assert any("describe" in p for p in check_spec(spec("a", source="llm")))
        assert check_spec(spec("a", source="llm", describe="the date of the run")) == []
        assert any("set by" in p for p in check_spec(spec("a", source="telepathy")))
        assert any("when is" in p for p in check_spec(spec("a", when="tomorrow")))


class TestAtTheStart:
    def test_a_python_parameter_is_worked_out_at_each_start_and_the_person_can_still_override_it(self):
        specs = [spec("when", type="datetime", source="python", expr="now()"), spec("tomorrow", type="date", source="python", expr="today() + timedelta(days=1)")]
        values, errors = resolve(specs, {}, NOW)
        assert errors == {} and values["when"]["value"] == "2026-11-08T14:30:05" and values["tomorrow"]["value"] == "2026-11-09"
        given, _ = resolve(specs, {"tomorrow": "2030-01-02"}, NOW)
        assert given["tomorrow"]["value"] == "2030-01-02"

    def test_a_python_parameter_can_read_the_ones_before_it(self):
        specs = [spec("city", default="rome"), spec("shout", source="python", expr='params["city"].upper()')]
        values, errors = resolve(specs, {}, NOW)
        assert errors == {} and values["shout"]["value"] == "ROME"

    def test_every_parameter_the_model_sets_is_set_in_one_call_with_what_is_known(self):
        seen = []

        def llm(items, known):
            seen.append((items, known))
            return {"mood": "calm", "n": "7"}
        specs = [spec("city", default="rome"), spec("mood", source="llm", describe="how the day feels"), spec("n", type="number", source="llm", describe="a number between 1 and 9")]
        values, errors = resolve(specs, {}, NOW, llm=llm)
        assert errors == {} and len(seen) == 1
        assert [i["name"] for i in seen[0][0]] == ["mood", "n"] and seen[0][1] == {"city": "rome"}
        assert values["mood"]["value"] == "calm" and values["n"]["value"] == 7

    def test_a_value_the_person_gave_is_not_asked_of_the_model_and_no_call_is_made_when_nothing_is_left(self):
        calls = []
        specs = [spec("mood", source="llm", describe="how the day feels")]
        values, _ = resolve(specs, {"mood": "tired"}, NOW, llm=lambda *a: calls.append(a) or {})
        assert values["mood"]["value"] == "tired" and calls == []

    def test_when_the_model_fails_or_answers_wrong_the_parameter_is_missing_and_the_reason_is_given(self):
        specs = [spec("n", type="number", source="llm", describe="a number")]
        _, errors = resolve(specs, {}, NOW, llm=lambda items, known: {"n": "many"})
        assert "does not fit" in errors["n"]

        def down(items, known):
            raise RuntimeError("no model")
        _, errors = resolve(specs, {}, NOW, llm=down)
        assert "could not set it" in errors["n"]
        values, errors = resolve(specs, {}, NOW)                                                          # nobody to ask: it stays missing for the run to say
        assert errors == {} and values["n"].get("missing") is True

    def test_a_parameter_that_depends_on_a_later_step_is_not_set_at_the_start(self):
        specs = [spec("answer", source="llm", describe="what step three found", when="later", after="s3"), spec("city", default="rome")]
        values, errors = resolve(specs, {}, NOW, llm=lambda *a: pytest.fail("not at the start"))
        assert errors == {} and values["answer"] == {"type": "text", "value": None, "later": True} and values["city"]["value"] == "rome"


class TestTheScriptIsToldHowEachParameterIsSet:
    def prompt(self, params):
        return distill.script_prompt("x", "", params, [{"n": 1, "text": "do it", "raw": "do it"}], [], None)[1]["content"]

    def test_the_start_values_are_used_as_they_come_and_the_later_ones_are_set_in_the_script(self):
        text = self.prompt([{"name": "when", "type": "datetime", "source": "python", "expr": "now()", "value_seen": None},
                            {"name": "found", "type": "text", "source": "llm", "describe": "what step s3 found", "when": "later", "after": "s3", "value_seen": None},
                            {"name": "delay", "type": "number", "source": "python", "expr": "30", "when": "later", "value_seen": None}])
        assert "Given at the start (by this Python expression: now()): use params.when as it comes" in text
        assert "NOT given at the start (after step s3)" in text and "make one ai() call at that point" in text
        assert "so write that expression in the script" in text
