"""Config loading: DEFAULTS deep-merged under a hand-edited config.yaml."""
import textwrap

from minidriver.config import DEFAULTS, load_config


def test_missing_file_yields_defaults():
    cfg = load_config("/no/such/config.yaml")
    assert cfg == DEFAULTS
    # A copy, not the shared dict: a caller mutating it must not poison DEFAULTS.
    cfg["network"]["country"] = "US"
    assert DEFAULTS["network"]["country"] != "US"


def test_partial_file_deep_merges_over_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent("""
        network:
          country: US
    """))
    cfg = load_config(path)
    assert cfg["network"]["country"] == "US"
    # Untouched sibling keys survive the merge rather than being wiped.
    assert cfg["network"]["interface"] == DEFAULTS["network"]["interface"]
    assert cfg["paths"]["data_dir"] == DEFAULTS["paths"]["data_dir"]


def test_home_networks_from_file_replace_default(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent("""
        network:
          home_networks:
            - ssid: home
              psk: secret
              priority: 10
    """))
    cfg = load_config(path)
    assert cfg["network"]["home_networks"] == [
        {"ssid": "home", "psk": "secret", "priority": 10}]


def test_invalid_yaml_yields_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("::: not valid yaml :::\n  - broken")
    assert load_config(path) == DEFAULTS


def test_non_mapping_file_yields_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a\n- list\n")
    assert load_config(path) == DEFAULTS
