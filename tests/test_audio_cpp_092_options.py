import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pandrator.logic.audio_cpp_parameters import (
    AUDIO_CPP_REQUEST_PARAMETERS_BY_FAMILY,
    request_parameters_for_family,
    validate_model_options,
)
from pandrator.logic.audio_cpp_speech_payload import (
    _audio_cpp_selected_model_options,
    _build_audio_cpp_audio_payload,
)

_ALIAS_CASES = (
    ("chatterbox", "s3gen_cfg_rate", "flow_guidance_scale", "number", 0.5),
    ("chatterbox_turbo", "max_new_tokens", "max_tokens", "integer", 2),
    ("pocket_tts", "max_steps", "max_frames", "integer", 0),
    ("pocket_tts", "max_tokens", "text_chunk_tokens", "integer", 2),
    ("f5_tts", "cfg_strength", "guidance_scale", "number", 2.0),
    ("firered_audio", "max_new_audio_steps", "max_audio_steps", "integer", 2),
    ("firered_audio", "min_new_audio_steps", "min_audio_steps", "integer", 2),
    ("firered_audio", "max_new_text_tokens", "max_text_tokens", "integer", 2),
    ("firered_audio", "max_new_tokens", "max_tokens", "integer", 2),
    ("index_tts2", "duration_factor", "duration_scale", "number", 2.0),
    ("inflect_v2", "speaking_rate", "speed", "number", 2.0),
    ("sanotts", "speaking_rate", "speed", "number", 2.0),
    ("sopro_tts", "max_seconds", "max_duration_sec", "number", 2.0),
    ("sopro_tts", "min_seconds", "min_duration_sec", "number", 2.0),
    ("sopro_tts", "ref_seconds", "reference_duration_sec", "number", 2.0),
    ("zipvoice", "t_shift", "shift", "number", 0.5),
    ("zipvoice", "lang", "language", "string", "en"),
    ("moss_tts_v15", "tokens", "duration_frames", "integer", 10),
)


def _metadata_for_family(family: str) -> dict[str, object]:
    requests: list[dict[str, object]] = []
    for case_family, _legacy, native, option_type, value in _ALIAS_CASES:
        if case_family != family:
            continue
        native_type = {"number": "float", "integer": "int"}.get(option_type, option_type)
        option: dict[str, object] = {"name": native, "type": native_type}
        if option_type in {"number", "integer"}:
            option.update({"min": 0, "max": 10})
        if option_type == "string":
            option["values"] = ["en", "fr"]
        option["default"] = value
        requests.append(option)
    if family == "f5_tts":
        requests[0].update({"min": 0.25, "max": 3.0, "default": 1.0})
    return {"options": {"request": requests}}


def _payload_with_options(
    family: str, options: dict[str, object], *, selected: bool
) -> dict[str, object]:
    model = f"{family}_compat_test"
    settings: dict[str, object] = {"model": model}
    if selected:
        settings["audio_cpp_model_settings"] = {model: options}
    else:
        settings["audio_cpp_options"] = options

    def selected_model_options(
        current_settings: dict[str, object], current_model: str, current_family: str
    ) -> dict[str, object] | None:
        return _audio_cpp_selected_model_options(
            current_settings,
            current_model,
            current_family,
            validate_options=validate_model_options,
        )

    def load_compiler():
        return lambda text, *_: SimpleNamespace(
            input=text,
            instructions="",
            capabilities={},
            request_options={},
        )

    with (
        patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata",
            side_effect=_metadata_for_family,
        ),
        patch("pandrator.logic.audio_cpp_speech_payload._package_metadata", return_value={}),
    ):
        return _build_audio_cpp_audio_payload(
            "Hello",
            settings,
            {},
            model_metadata=lambda _model, _endpoint: {
                "family": family,
                "voice_mode": "cloning",
            },
            model_language=lambda *_args: "",
            selected_model_options=selected_model_options,
            validate_options=validate_model_options,
            get_performance_compiler=load_compiler,
        )


class AudioCpp092OptionCompatibilityTests(unittest.TestCase):
    def test_current_inventory_has_one_public_name_for_every_alias_pair(self):
        for family, legacy, native, _option_type, _value in _ALIAS_CASES:
            with self.subTest(family=family, legacy=legacy):
                parameters = request_parameters_for_family(family)
                retained_manual_key = legacy in AUDIO_CPP_REQUEST_PARAMETERS_BY_FAMILY.get(
                    family, {}
                )
                public_name = (
                    legacy
                    if retained_manual_key or family == "zipvoice" and legacy == "lang"
                    else native
                )
                hidden_name = native if public_name == legacy else legacy
                self.assertIn(public_name, parameters)
                self.assertNotIn(hidden_name, parameters)

    def test_native_descriptors_have_one_public_name_and_validate_old_aliases(self):
        with patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata",
            side_effect=_metadata_for_family,
        ):
            for family, legacy, native, _option_type, value in _ALIAS_CASES:
                with self.subTest(family=family, legacy=legacy):
                    parameters = request_parameters_for_family(family)
                    retained_manual_key = legacy in AUDIO_CPP_REQUEST_PARAMETERS_BY_FAMILY.get(
                        family, {}
                    )
                    public_name = (
                        legacy
                        if retained_manual_key or family == "zipvoice" and legacy == "lang"
                        else native
                    )
                    hidden_name = native if public_name == legacy else legacy
                    self.assertIn(public_name, parameters)
                    self.assertNotIn(hidden_name, parameters)

                    if retained_manual_key:
                        self.assertEqual(
                            AUDIO_CPP_REQUEST_PARAMETERS_BY_FAMILY[family][legacy],
                            parameters[legacy],
                        )

                    expected_value = value
                    old_input = {legacy: value}
                    new_input = {native: value}
                    self.assertEqual(
                        {public_name: expected_value},
                        validate_model_options(family, old_input),
                    )
                    self.assertEqual(
                        {public_name: expected_value},
                        validate_model_options(family, new_input),
                    )
                    self.assertEqual({legacy: value}, old_input)
                    self.assertEqual({native: value}, new_input)

    def test_legacy_bag_and_selected_canonical_map_build_the_same_payload(self):
        # F5 stays usable even though its native splitter cannot be disabled.
        expected = _payload_with_options("f5_tts", {"guidance_scale": 2.0}, selected=True)
        for options, selected in (
            ({"cfg_strength": 2.0}, False),
            ({"cfg_strength": 2.0}, True),
            ({"guidance_scale": 2.0}, True),
        ):
            self.assertEqual(expected, _payload_with_options("f5_tts", options, selected=selected))

    def test_zipvoice_language_is_a_lang_option_for_both_saved_spellings(self):
        parameters = request_parameters_for_family("zipvoice")
        self.assertIn("lang", parameters)
        self.assertNotIn("language", parameters)
        self.assertEqual({"lang": "en"}, validate_model_options("zipvoice", {"lang": "en"}))
        self.assertEqual(
            {"lang": "en"},
            validate_model_options("zipvoice", {"language": "en"}),
        )
        self.assertEqual(
            _payload_with_options("zipvoice", {"language": "en"}, selected=False),
            _payload_with_options("zipvoice", {"lang": "en"}, selected=True),
        )
        self.assertEqual(
            _payload_with_options("zipvoice", {"language": "en"}, selected=False),
            _payload_with_options("zipvoice", {"language": "en"}, selected=True),
        )

    def test_alias_conflicts_fail_and_empty_aliases_do_not_erase_values(self):
        native_options = _metadata_for_family("f5_tts")
        with patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata",
            return_value=native_options,
        ):
            with self.assertRaisesRegex(ValueError, "Conflicting.*cfg_strength.*guidance_scale"):
                validate_model_options("f5_tts", {"cfg_strength": 1.0, "guidance_scale": 2.0})

            for options in (
                {"cfg_strength": None, "guidance_scale": 2.0},
                {"cfg_strength": "", "guidance_scale": 2.0},
                {"cfg_strength": 2.0, "guidance_scale": None},
            ):
                with self.subTest(options=options):
                    self.assertEqual(
                        {"guidance_scale": 2.0},
                        validate_model_options("f5_tts", options),
                    )

        payload = _payload_with_options(
            "f5_tts",
            {"cfg_strength": None, "guidance_scale": 2.0},
            selected=False,
        )
        self.assertEqual(2.0, payload["options"]["guidance_scale"])

    def test_alias_values_still_obey_type_and_native_bounds(self):
        with patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata",
            return_value=_metadata_for_family("f5_tts"),
        ):
            with self.assertRaisesRegex(ValueError, "must be a number"):
                validate_model_options("f5_tts", {"cfg_strength": True})
            with self.assertRaisesRegex(ValueError, "at most 3.0"):
                validate_model_options("f5_tts", {"cfg_strength": 3.5})

        with patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata",
            return_value=_metadata_for_family("firered_audio"),
        ):
            with self.assertRaisesRegex(ValueError, "must be an integer"):
                validate_model_options("firered_audio", {"max_new_tokens": True})
            with self.assertRaisesRegex(ValueError, "at most 10"):
                validate_model_options("firered_audio", {"max_new_tokens": 11})

    def test_unknown_and_other_reserved_fields_remain_rejected(self):
        with patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata",
            side_effect=_metadata_for_family,
        ):
            for family, options in (
                ("f5_tts", {"language": "en"}),
                ("f5_tts", {"instruction": "Speak warmly."}),
                ("zipvoice", {"instruction": "Speak warmly."}),
                ("zipvoice", {"unknown_option": True}),
            ):
                with self.subTest(family=family, options=options):
                    with self.assertRaisesRegex(ValueError, "Unknown"):
                        validate_model_options(family, options)

    def test_chatterbox_greedy_remains_a_bool_without_injecting_a_default(self):
        with patch(
            "pandrator.logic.audio_cpp_parameters.family_metadata",
            return_value=_metadata_for_family("chatterbox"),
        ):
            parameters = request_parameters_for_family("chatterbox")
            self.assertEqual({"type": "boolean"}, parameters["greedy"])
            self.assertEqual(
                {"greedy": False}, validate_model_options("chatterbox", {"greedy": False})
            )
            self.assertEqual({}, validate_model_options("chatterbox", {}))
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                validate_model_options("chatterbox", {"greedy": "true"})
            with self.assertRaisesRegex(ValueError, "Unknown"):
                validate_model_options("chatterbox", {"greediness": True})
            payload = _payload_with_options("chatterbox", {"greedy": False}, selected=True)
            self.assertEqual({"greedy": False, "text_chunk_size": 128}, payload["options"])


if __name__ == "__main__":
    unittest.main()
