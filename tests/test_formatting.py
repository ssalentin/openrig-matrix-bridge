from openrig_matrix_bridge.formatting import markdown_to_matrix_html


def test_renders_gfm_table_as_matrix_html():
    rendered = markdown_to_matrix_html(
        "## Status\n\n| Agent | Status |\n|---|---|\n| Advisor | bereit |"
    )

    assert "<h2>Status</h2>" in rendered
    assert "<table>" in rendered
    assert "<td>Advisor</td>" in rendered


def test_escapes_raw_html():
    rendered = markdown_to_matrix_html("<script>alert(1)</script>")

    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered


def test_does_not_embed_remote_markdown_images():
    rendered = markdown_to_matrix_html("![secret](https://tracker.example/pixel.png)")

    assert "<img" not in rendered
    assert "Bild nicht eingebettet" in rendered
