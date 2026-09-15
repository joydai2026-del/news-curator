import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("retained_translation_pipeline", ROOT / "scripts/retained_translation_pipeline.py")
module = importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(module)


def test_queue_limit_is_bounded_before_any_network_command():
    with pytest.raises(SystemExit):
        module.main(["translate", "--limit", "1001"])


def test_export_rejects_invalid_locale_before_any_network_command(tmp_path):
    with pytest.raises(ValueError, match="locale"):
        module.main(["export", "--locale", "fr", "--output", str(tmp_path / "x.json")])


def test_localized_projection_row_requires_the_original_retained_identity_fields():
    with pytest.raises(ValueError, match="queue row"):
        module._item({"story_id": "story:x"})
