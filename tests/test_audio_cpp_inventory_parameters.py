import unittest
from unittest.mock import patch

from pandrator.logic.audio_cpp_parameters import (
    request_parameters_for_family,
    validate_model_options,
)


def _metadata(*options: dict[str, object]) -> dict[str, object]:
    return {"options": {"request": list(options)}}


class AudioCppInventoryParameterTests(unittest.TestCase):
    def test_safe_native_descriptors_map_types_and_bounds(self):
        fixture = _metadata(
            {"name": "native_int", "type": "int", "default": 2, "min": 1, "max": 4},
            {
                "name": "native_float",
                "type": "float",
                "default": 0.5,
                "min": 0.1,
                "max": 0.9,
            },
            {"name": "native_bool", "type": "bool", "default": True},
            {
                "name": "native_enum",
                "type": "enum",
                "values": ["fast", "slow"],
                "default": "fast",
            },
            {"name": "native_string", "type": "string", "default": "hello"},
        )

        with patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata",
            return_value=fixture,
        ):
            parameters = request_parameters_for_family("fixture")

        self.assertEqual(
            {"type": "integer", "default": 2, "minimum": 1, "maximum": 4},
            parameters["native_int"],
        )
        self.assertEqual(
            {"type": "number", "default": 0.5, "minimum": 0.1, "maximum": 0.9},
            parameters["native_float"],
        )
        self.assertEqual({"type": "boolean", "default": True}, parameters["native_bool"])
        self.assertEqual(
            {"type": "string", "enum": ["fast", "slow"], "default": "fast"},
            parameters["native_enum"],
        )
        self.assertEqual(
            {"type": "string", "default": "hello"}, parameters["native_string"]
        )

    def test_reserved_and_non_scalar_inventory_fields_are_omitted(self):
        reserved = (
            "instruction",
            "instruct",
            "instructions",
            "reference_text",
            "reference_language",
            "prompt_text",
            "voice_clone_text",
            "voice_id",
            "voice",
            "speaker",
            "language",
            "template_name",
            "no_ref",
            "source_text",
            "target_text",
            "multi_reference_cond",
            "source_audio",
            "target_voice",
            "phonemes",
            "return_video",
            "path",
            "audio_path",
        )
        fixture_options = [
            {"name": name, "type": "string"} for name in reserved
        ] + [
            {"name": "unsafe_name-", "type": "string"},
            {"name": "UpperCase", "type": "string"},
        ]
        fixture_options.extend(
            {"name": f"unsafe_{index}", "type": native_type}
            for index, native_type in enumerate(
                ("path", "audio_path", "string_list", "array", "object", "objects")
            )
        )

        with patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata",
            return_value=_metadata(*fixture_options),
        ):
            parameters = request_parameters_for_family("fixture")

        self.assertEqual({}, parameters)

    def test_manual_descriptors_override_derived_values_and_aliases_remain(self):
        fixture = _metadata(
            {
                "name": "temperature",
                "type": "float",
                "default": 0.1,
                "min": 0,
                "max": 1,
            },
            {"name": "native_extra", "type": "int", "default": 3},
        )
        with patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata",
            return_value=fixture,
        ):
            parameters = request_parameters_for_family("qwen3_tts")
            fish_parameters = request_parameters_for_family("fish_audio")

        self.assertEqual(
            {"type": "number", "default": 0.9, "exclusive_minimum": 0},
            parameters["temperature"],
        )
        self.assertEqual(3, parameters["native_extra"]["default"])
        self.assertIn("text_chunk_mode", fish_parameters)

    def test_invalid_defaults_are_dropped(self):
        fixture = _metadata(
            {"name": "bad_default_int", "type": "int", "default": 1.5},
            {"name": "bad_default_number", "type": "float", "default": "nope"},
            {"name": "bad_default_enum", "type": "enum", "values": ["a"], "default": "b"},
            {"name": "bad_default_string", "type": "string", "default": "x\x00y"},
            {"name": "bad_default_bounds", "type": "int", "default": 9, "max": 4},
        )
        with patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata",
            return_value=fixture,
        ):
            parameters = request_parameters_for_family("fixture")

        for name in (
            "bad_default_int",
            "bad_default_number",
            "bad_default_enum",
            "bad_default_string",
            "bad_default_bounds",
        ):
            with self.subTest(name=name):
                self.assertNotIn("default", parameters[name])

    def test_generic_string_limits_and_unknown_options_are_validated(self):
        fixture = _metadata({"name": "native_string", "type": "string"})
        with patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata",
            return_value=fixture,
        ):
            self.assertEqual(
                {"native_string": "ok"},
                validate_model_options("fixture", {"native_string": "ok"}),
            )
            with self.assertRaisesRegex(ValueError, "at most 1200"):
                validate_model_options("fixture", {"native_string": "x" * 1201})
            with self.assertRaisesRegex(ValueError, "NUL"):
                validate_model_options("fixture", {"native_string": "x\x00y"})
            with self.assertRaisesRegex(ValueError, "Unknown"):
                validate_model_options("fixture", {"unknown": 1})

    def test_unknown_family_has_no_options(self):
        with patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata", return_value={}
        ):
            self.assertEqual({}, request_parameters_for_family("unknown_family"))
            with self.assertRaisesRegex(ValueError, "Unknown"):
                validate_model_options("unknown_family", {"temperature": 0.8})


if __name__ == "__main__":
    unittest.main()
