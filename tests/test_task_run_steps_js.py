"""JS tests for task run steps timeline rendering."""


class TestStepsTimelineJS:
    """Validate the steps timeline rendering function exists and follows patterns."""

    def test_render_steps_function_exists(self):
        with open("static/js/tasks.js") as f:
            src = f.read()
        assert "_renderStepsTimeline" in src, "Missing _renderStepsTimeline function in tasks.js"

    def test_steps_badge_shown_for_has_steps(self):
        with open("static/js/tasks.js") as f:
            src = f.read()
        assert "has_steps" in src, "Frontend should reference has_steps field"

    def test_fetches_steps_from_api(self):
        with open("static/js/tasks.js") as f:
            src = f.read()
        assert "/steps" in src, "Frontend should fetch steps from steps endpoint"

    def test_renders_tool_names(self):
        with open("static/js/tasks.js") as f:
            src = f.read()
        assert "step.tool" in src, "Frontend should render tool names from step data"
