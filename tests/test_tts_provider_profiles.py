import unittest

from pandrator.logic import tts_handler, tts_provider_profiles


class TTSProviderProfileTests(unittest.TestCase):
    def test_profiles_are_unique_and_directly_runnable_by_supported_adapters(self):
        profiles = tts_provider_profiles.list_tts_provider_profiles()
        profile_ids = [profile["id"] for profile in profiles]

        self.assertEqual(len(profile_ids), len(set(profile_ids)))
        self.assertGreaterEqual(len(profiles), 15)
        for profile in profiles:
            self.assertIn(
                profile["adapter"],
                {
                    "openai_compatible",
                    "audio_cpp",
                    "generic_json",
                    "elevenlabs_native",
                    "azure_speech",
                },
            )
            self.assertTrue(profile["api_base"].startswith(("http://", "https://")))
            self.assertTrue(profile["speech_path"].startswith("/"))
            self.assertTrue(profile["request_fields"]["text"])
            self.assertTrue(profile["models"] or profile.get("models_are_manual"))
            self.assertTrue(profile["source_url"].startswith("https://"))

    def test_primary_source_corrected_profiles_use_expected_contracts(self):
        profiles = {
            profile["id"]: profile
            for profile in tts_provider_profiles.list_tts_provider_profiles()
        }

        self.assertTrue(profiles["azure-openai-v1"]["models_are_manual"])
        self.assertEqual(profiles["azure-openai-v1"]["models"], [])
        self.assertEqual(
            profiles["styletts2-salad"]["api_base"], "http://127.0.0.1:4321"
        )
        self.assertEqual(profiles["styletts2-salad"]["request_fields"]["voice"], "")
        self.assertEqual(profiles["piper-native-http"]["speech_path"], "/")
        self.assertEqual(
            profiles["chatterbox-brioch"]["api_base"], "http://127.0.0.1:5001"
        )
        self.assertEqual(
            profiles["cosyvoice-jianchang512"]["api_base"], "http://127.0.0.1:9233"
        )
        self.assertEqual(
            profiles["voxtral-vllm-omni"]["api_base"], "http://127.0.0.1:8091"
        )
        self.assertEqual(
            profiles["qwen3-second-state"]["api_base"], "http://127.0.0.1:8000"
        )
        self.assertEqual(
            profiles["open-unified-tts"]["api_base"], "http://127.0.0.1:8765"
        )
        self.assertEqual(
            profiles["pandrator-xtts2-api"]["api_base"], "http://127.0.0.1:8020"
        )
        self.assertEqual(
            profiles["pandrator-chatterbox-fastapi"]["api_base"],
            "http://127.0.0.1:8040",
        )
        self.assertEqual(
            profiles["pandrator-kobold-qwen-fastapi"]["api_base"],
            "http://127.0.0.1:8042",
        )
        self.assertEqual(
            profiles["pandrator-kobold-qwen-fastapi"]["models"], ["qwen3-tts"]
        )
        self.assertNotIn("styletts2-sillytavern", profiles)
        self.assertNotIn("voxcpm-nanovllm", profiles)

    def test_audio_cpp_profile_is_external_dynamic_and_keyless(self):
        profile = tts_provider_profiles.get_tts_provider_profile(
            "audio-cpp-experimental"
        )

        self.assertIsNotNone(profile)
        self.assertEqual("audio_cpp", profile["adapter"])
        self.assertEqual("http://127.0.0.1:8080", profile["api_base"])
        self.assertEqual("/v1/audio/speech", profile["speech_path"])
        self.assertEqual("/v1/models", profile["models_path"])
        self.assertEqual("/v1/audio/voices", profile["voices_path"])
        self.assertIn("qwen3_tts_1_7b_base_q8_0", profile["models"])
        self.assertIn("fireredtts3_base_q8_0", profile["models"])
        qwen_models = {
            item["id"]: item
            for item in profile["model_catalog"]
            if item["id"].startswith("qwen3_tts_")
        }
        self.assertEqual(
            300, qwen_models["qwen3_tts_1_7b_base_q8_0"]["recommended_chunk_characters"]
        )
        self.assertEqual(
            300,
            qwen_models["qwen3_tts_1_7b_customvoice_q8_0"][
                "recommended_chunk_characters"
            ],
        )
        self.assertFalse(profile["models_are_manual"])
        self.assertFalse(profile["credential_required"])
        self.assertTrue(profile["direct_http"])

    def test_azure_speech_profile_has_static_models_voices_and_safe_pricing(self):
        profile = tts_provider_profiles.get_tts_provider_profile(
            "azure-speech-mai-voice-2"
        )

        self.assertIsNotNone(profile)
        self.assertEqual("Azure Speech · MAI Voice 2", profile["name"])
        self.assertEqual("azure_speech", profile["adapter"])
        self.assertEqual(
            "https://YOUR-REGION.tts.speech.microsoft.com", profile["api_base"]
        )
        self.assertEqual("/cognitiveservices/v1", profile["speech_path"])
        self.assertEqual("subscription-key", profile["auth_mode"])
        self.assertTrue(profile["direct_http"])
        self.assertTrue(profile["credential_required"])
        self.assertEqual("AZURE_SPEECH_KEY", profile["api_key_env"])
        self.assertEqual(["MAI-Voice-2", "MAI-Voice-2-Flash"], profile["models"])
        self.assertEqual("MAI-Voice-2", profile["default_model"])
        self.assertEqual("en-US-Ethan:MAI-Voice-2", profile["default_voice"])
        self.assertEqual(
            {
                "azure_speech_style": "",
                "azure_speech_style_degree": 1.0,
                "azure_speech_output_format": "audio-24khz-160kbitrate-mono-mp3",
            },
            profile["settings"],
        )
        self.assertIn("en-US-Ethan:MAI-Voice-2", profile["voices"])
        self.assertIn("en-US-Ethan:MAI-Voice-2-Flash", profile["voices"])
        self.assertNotIn("en-US-Grant:MAI-Voice-2-Flash", profile["voices"])
        self.assertEqual(
            profile["voice_catalogues"]["MAI-Voice-2-Flash"],
            [
                voice
                for voice in profile["voices"]
                if voice.endswith(":MAI-Voice-2-Flash")
            ],
        )
        self.assertEqual([], profile["generation_prompt_models"])
        for model, pricing in profile["pricing"].items():
            self.assertIn(model, profile["models"])
            self.assertEqual({"unit": "characters", "source": "Azure pricing"}, pricing)

    def test_azure_profile_metadata_round_trips_through_custom_provider(self):
        profile = tts_provider_profiles.get_tts_provider_profile(
            "azure-speech-mai-voice-2"
        )
        adapter_config = dict(profile)
        success, providers, _, message = tts_handler.save_provider(
            {"provider_configs": []},
            provider_name=profile["name"],
            provider_type=profile["provider"],
            api_base="https://eastus.tts.speech.microsoft.com",
            api_key="azure-secret",
            models=profile["models"],
            voices=profile["voices"],
            supports_prebuilt_voices=profile["supports_prebuilt_voices"],
            provider_id=profile["id"],
            adapter_config=adapter_config,
        )

        self.assertTrue(success, message)
        saved = providers[0]
        self.assertEqual("azure_speech", saved["adapter"])
        self.assertEqual(profile["model_catalog"], saved["model_catalog"])
        self.assertEqual(profile["voice_metadata"], saved["voice_metadata"])
        self.assertEqual(profile["pricing"], saved["pricing"])
        self.assertEqual([], saved["generation_prompt_models"])
        self.assertEqual("AZURE_SPEECH_KEY", saved["api_key_env"])

    def test_profile_catalog_returns_deep_copies(self):
        profiles = tts_provider_profiles.list_tts_provider_profiles()
        profiles[0]["models"].append("changed")

        fresh_profiles = tts_provider_profiles.list_tts_provider_profiles()
        self.assertNotIn("changed", fresh_profiles[0]["models"])

        azure = next(
            profile
            for profile in fresh_profiles
            if profile["id"] == "azure-speech-mai-voice-2"
        )
        azure["voice_metadata"]["MAI-Voice-2:en-US-Ethan:MAI-Voice-2"]["styles"].append(
            "changed"
        )
        fresh_azure = tts_provider_profiles.get_tts_provider_profile(
            "azure-speech-mai-voice-2"
        )
        self.assertNotIn(
            "changed",
            fresh_azure["voice_metadata"]["MAI-Voice-2:en-US-Ethan:MAI-Voice-2"][
                "styles"
            ],
        )

    def test_profile_id_persists_with_saved_custom_provider(self):
        profile = tts_provider_profiles.get_tts_provider_profile("styletts2-salad")
        adapter_config = dict(profile)
        adapter_config["profile_id"] = profile["id"]

        success, providers, _, message = tts_handler.save_provider(
            {"provider_configs": []},
            provider_name=profile["name"],
            provider_type=profile["provider"],
            api_base=profile["api_base"],
            models=profile["models"],
            voices=profile["voices"],
            supports_prebuilt_voices=profile["supports_prebuilt_voices"],
            adapter_config=adapter_config,
        )

        self.assertTrue(success, message)
        self.assertEqual(providers[0]["profile_id"], "styletts2-salad")
        self.assertEqual(providers[0]["speech_path"], "/generate")

    def test_profile_catalogs_do_not_gain_unrelated_openai_defaults(self):
        profile = tts_provider_profiles.get_tts_provider_profile("pandrator-xtts2-api")
        adapter_config = dict(profile)
        adapter_config["profile_id"] = profile["id"]

        success, providers, _, message = tts_handler.save_provider(
            {"provider_configs": []},
            provider_name=profile["name"],
            provider_type=profile["provider"],
            api_base=profile["api_base"],
            models=profile["models"],
            voices=[],
            adapter_config=adapter_config,
        )

        self.assertTrue(success, message)
        self.assertEqual(providers[0]["voices"], [])
        settings = {
            "service": tts_handler.OPENAI_COMPAT_SERVICE,
            "openai_audio_endpoint": providers[0]["id"],
            "provider_configs": providers,
        }
        self.assertEqual(tts_handler.get_openai_audio_voices_fallback(settings), [])


if __name__ == "__main__":
    unittest.main()
