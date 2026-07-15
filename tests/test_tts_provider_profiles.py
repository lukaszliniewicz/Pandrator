import unittest

from pandrator.logic import tts_handler, tts_provider_profiles


class TTSProviderProfileTests(unittest.TestCase):
    def test_profiles_are_unique_and_directly_runnable_by_supported_adapters(self):
        profiles = tts_provider_profiles.list_tts_provider_profiles()
        profile_ids = [profile["id"] for profile in profiles]

        self.assertEqual(len(profile_ids), len(set(profile_ids)))
        self.assertGreaterEqual(len(profiles), 15)
        for profile in profiles:
            self.assertIn(profile["adapter"], {"openai_compatible", "generic_json"})
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
        self.assertEqual(profiles["styletts2-salad"]["api_base"], "http://127.0.0.1:4321")
        self.assertEqual(profiles["styletts2-salad"]["request_fields"]["voice"], "")
        self.assertEqual(profiles["piper-native-http"]["speech_path"], "/")
        self.assertEqual(profiles["chatterbox-brioch"]["api_base"], "http://127.0.0.1:5001")
        self.assertEqual(profiles["cosyvoice-jianchang512"]["api_base"], "http://127.0.0.1:9233")
        self.assertEqual(profiles["voxtral-vllm-omni"]["api_base"], "http://127.0.0.1:8091")
        self.assertEqual(profiles["qwen3-second-state"]["api_base"], "http://127.0.0.1:8000")
        self.assertEqual(profiles["open-unified-tts"]["api_base"], "http://127.0.0.1:8765")
        self.assertEqual(profiles["pandrator-xtts2-api"]["api_base"], "http://127.0.0.1:8020")
        self.assertEqual(profiles["pandrator-chatterbox-fastapi"]["api_base"], "http://127.0.0.1:8040")
        self.assertEqual(profiles["pandrator-kobold-qwen-fastapi"]["api_base"], "http://127.0.0.1:8042")
        self.assertEqual(profiles["pandrator-kobold-qwen-fastapi"]["models"], ["qwen3-tts"])
        self.assertNotIn("styletts2-sillytavern", profiles)
        self.assertNotIn("voxcpm-nanovllm", profiles)

    def test_profile_catalog_returns_deep_copies(self):
        profiles = tts_provider_profiles.list_tts_provider_profiles()
        profiles[0]["models"].append("changed")

        fresh_profiles = tts_provider_profiles.list_tts_provider_profiles()
        self.assertNotIn("changed", fresh_profiles[0]["models"])

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
