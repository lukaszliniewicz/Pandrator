import subprocess
import sys
import unittest
from importlib import resources
from pathlib import Path

from pandrator_manager.components.builtin import BUILTIN_COMPONENTS
from pandrator_manager.provider_policy import ProviderMetadata, provider_metadata_for


class ManagerProviderPolicyTests(unittest.TestCase):
    def test_packaged_policy_is_byte_identical_to_application_policy(self):
        canonical = Path(__file__).parents[1] / "pandrator" / "provider_policy.json"
        packaged = resources.files("pandrator_manager").joinpath("provider_policy.json")

        self.assertEqual(canonical.read_bytes(), packaged.read_bytes())

    def test_primary_local_tts_components_are_exactly_the_policy_primaries(self):
        local_tts = {
            definition.id
            for definition in BUILTIN_COMPONENTS
            if definition.service_key and definition.service_key.startswith("tts.")
        }
        primary = {
            definition.id
            for definition in BUILTIN_COMPONENTS
            if definition.catalogue_role == "primary"
            and definition.service_key
            and definition.service_key.startswith("tts.")
        }

        self.assertEqual(
            {"audio_cpp", "xtts", "silero", "kokoro", "voxtral"},
            primary,
        )
        self.assertEqual(10, len(local_tts))

    def test_compatibility_components_expose_replacement_component_metadata(self):
        expected = {
            "qwen_tts": "audio_cpp",
            "fish_speech": "audio_cpp",
            "voxcpm": "audio_cpp",
            "chatterbox": "audio_cpp",
            "magpie": "audio_cpp",
        }
        definitions = {definition.id: definition for definition in BUILTIN_COMPONENTS}

        self.assertEqual(
            set(expected),
            {
                definition.id
                for definition in BUILTIN_COMPONENTS
                if definition.catalogue_role == "compatibility"
            },
        )
        for component_id, replacement in expected.items():
            self.assertEqual("compatibility", definitions[component_id].catalogue_role)
            self.assertEqual(
                replacement, definitions[component_id].replacement_component_id
            )

    def test_unknown_component_metadata_defaults_to_primary_without_replacement(self):
        self.assertEqual(ProviderMetadata(), provider_metadata_for("pandrator"))
        self.assertEqual(ProviderMetadata(), provider_metadata_for("unknown"))

    def test_registry_ids_actions_and_service_definitions_are_unchanged(self):
        expected_actions = {
            "pandrator": ("install", "update", "repair", "remove", "start", "stop"),
            "audio_cpp": ("install", "update", "repair", "remove", "start", "stop"),
            "xtts": ("install", "update", "repair", "remove", "start", "stop"),
            "voxcpm": ("install", "update", "repair", "remove", "start", "stop"),
            "fish_speech": ("install", "update", "repair", "remove", "start", "stop"),
            "voxtral": ("install", "update", "repair", "remove", "start", "stop"),
            "kokoro": ("install", "update", "repair", "remove", "start", "stop"),
            "silero": ("install", "update", "repair", "remove", "start", "stop"),
            "crispasr": ("install", "update", "repair", "remove"),
            "rvc": ("install", "update", "repair", "remove", "start", "stop"),
            "chatterbox": ("install", "update", "repair", "remove", "start", "stop"),
            "qwen_tts": ("install", "update", "repair", "remove", "start", "stop"),
            "magpie": ("install", "update", "repair", "remove", "start", "stop"),
            "xtts_finetuning": ("remove",),
        }
        expected_service_keys = {
            "pandrator": None,
            "audio_cpp": "tts.audio_cpp",
            "xtts": "tts.xtts",
            "voxcpm": "tts.voxcpm",
            "fish_speech": "tts.fish_speech",
            "voxtral": "tts.voxtral",
            "kokoro": "tts.kokoro",
            "silero": "tts.silero",
            "crispasr": None,
            "rvc": "audio.rvc",
            "chatterbox": "tts.chatterbox",
            "qwen_tts": "tts.qwen",
            "magpie": "tts.magpie",
            "xtts_finetuning": None,
        }
        definitions = {definition.id: definition for definition in BUILTIN_COMPONENTS}

        self.assertEqual(set(expected_actions), set(definitions))
        for component_id, actions in expected_actions.items():
            self.assertEqual(actions, definitions[component_id].supported_actions)
            self.assertEqual(
                expected_service_keys[component_id],
                definitions[component_id].service_key,
            )

    def test_policy_module_does_not_import_pandrator_application(self):
        script = """
import builtins

real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "pandrator" or name.startswith("pandrator."):
        raise AssertionError(f"unexpected application import: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from pandrator_manager.provider_policy import provider_metadata_for

assert provider_metadata_for("qwen_tts").replacement_component_id == "audio_cpp"
"""
        subprocess.run([sys.executable, "-c", script], check=True)


if __name__ == "__main__":
    unittest.main()
