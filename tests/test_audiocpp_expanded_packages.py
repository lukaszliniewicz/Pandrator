"""Expanded audio.cpp package definitions remain pinned and offline."""

from pandrator_manager.components.audiocpp import (
    AUDIO_CPP_MODEL_REVISION,
    AUDIO_CPP_VERSION,
    MODEL_PACKAGES,
    SUPPORTED_MODEL_IDS,
)

EXPANDED_PACKAGES = {
    "qwen3_tts_0_6b_base_q8_0": {
        "family": "qwen3_tts",
        "target_directory": "Qwen3-TTS-12Hz-0.6B-Base-GGUF",
        "file": "qwen3-tts-12hz-0.6b-base-q8_0.gguf",
        "sha256": "771420bd20ff5f35407b4fa9cf9c5461e153800d3d772ef51c9febc0a520855d",
        "task": "clon",
    },
    "chatterbox_turbo_q8_0": {
        "family": "chatterbox_turbo",
        "target_directory": "Chatterbox-Turbo-GGUF",
        "file": "chatterbox-turbo-q8_0.gguf",
        "sha256": "6eed51ff0b2993fec67db6211dca92de5819d83ac50513ba7c065c467eb75910",
        "task": "tts",
    },
    "supertonic_3_q8_0": {
        "family": "supertonic",
        "target_directory": "Supertonic-3-GGUF",
        "file": "supertonic-3-q8_0.gguf",
        "sha256": "af814486a0bc9513fb36afabd9b1155ad14fb2c36a107ac6ffe62ea9adafb662",
        "task": "tts",
    },
    "fireredtts3_instruct_q8_0": {
        "family": "fireredtts3",
        "target_directory": "FireRedTTS3-Instruct-GGUF",
        "file": "fireredtts3-instruct-q8_0.gguf",
        "sha256": "04cd0ff6624bbfdc6b39e2dd1a0068f9e9769d1c217bf7343e28f3415eb2859e",
        "task": "tts",
    },
}

LEGACY_PACKAGE_IDS = (
    "qwen3_tts_1_7b_base_q8_0",
    "qwen3_tts_1_7b_customvoice_q8_0",
    "qwen3_tts_1_7b_voicedesign_q8_0",
    "fish_audio_s2_pro_q8_0",
    "voxcpm2_q8_0",
    "magpie_tts_q8_0",
    "chatterbox_q8_0",
    "omnivoice_q8_0",
    "pocket_tts_english_q8_0",
    "fireredtts3_base_q8_0",
    "breeze_tts_2_q8_0",
)

LEGACY_PACKAGE_SNAPSHOTS = {
    "qwen3_tts_1_7b_base_q8_0": (
        "qwen3_tts",
        "Qwen3-TTS-12Hz-1.7B-Base-GGUF",
        ("qwen3-tts-12hz-1.7b-base-q8_0_v2.gguf",),
        ("b55e06c7890d43c208d15aed8b4ed3f18215f295e47d5960e061b15bff338ab0",),
        "tts",
        "offline",
        None,
        None,
    ),
    "qwen3_tts_1_7b_customvoice_q8_0": (
        "qwen3_tts",
        "Qwen3-TTS-12Hz-1.7B-CustomVoice-GGUF",
        ("qwen3-tts-12hz-1.7b-customvoice-q8_0.gguf",),
        ("3cfaac8e9f13554f6daea3c5e0c53fede71ef5500cbaae7445e5fc3a5bb12e72",),
        "tts",
        "offline",
        None,
        None,
    ),
    "qwen3_tts_1_7b_voicedesign_q8_0": (
        "qwen3_tts",
        "Qwen3-TTS-12Hz-1.7B-VoiceDesign-GGUF",
        ("qwen3-tts-12hz-1.7b-voicedesign-q8_0.gguf",),
        ("1bcef9a8c021072fca40e00498e00af9091fbe6d3ae4f87567cfee885d6c7554",),
        "vdes",
        "offline",
        None,
        None,
    ),
    "fish_audio_s2_pro_q8_0": (
        "fish_audio",
        "Fish-Audio-S2-Pro-GGUF",
        ("fish-audio-s2-pro-q8_0.gguf",),
        ("4ffc169447b7a26df8bf49e8637adb4000bfa763a22c018b6c03968564259d0b",),
        "tts",
        "offline",
        None,
        None,
    ),
    "voxcpm2_q8_0": (
        "voxcpm2",
        "VoxCPM2-GGUF",
        ("voxcpm2-q8_0.gguf",),
        ("c8e01ab4416011e12a28f24ede298a1aa5ce64b43f8e8aaad53b1e2fe7c96432",),
        "tts",
        "offline",
        None,
        None,
    ),
    "magpie_tts_q8_0": (
        "magpie_tts",
        "MagpieTTS-Multilingual-357M-GGUF",
        ("magpie-tts-multilingual-357m-q8_0.gguf",),
        ("c762503a80f9af75db33379763b923c5c8a00ca79374e1e0d4051e6d68151377",),
        "tts",
        "offline",
        None,
        None,
    ),
    "chatterbox_q8_0": (
        "chatterbox",
        "Chatterbox-GGUF",
        ("chatterbox-q8_0.gguf",),
        ("d586dd1aa59613cab8046176fb7ca5ba191c02a9b10ffa5b0d892ed22b470656",),
        "clon",
        "offline",
        None,
        None,
    ),
    "omnivoice_q8_0": (
        "omnivoice",
        "OmniVoice-GGUF",
        ("omnivoice-q8_0.gguf",),
        ("2f4be637278043c6842de5b85d681532030e9eb6ffe0f8b0e320f68238e3da8b",),
        "tts",
        "offline",
        None,
        None,
    ),
    "pocket_tts_english_q8_0": (
        "pocket_tts",
        "PocketTTS-GGUF/english",
        ("pocket-tts-english-q8_0.gguf", "embeddings/alba.safetensors"),
        (
            "0315406421d515d9ffbde49ed998832ff2962562ef8abde440c85fa0a27d8b2a",
            "69c32db63ca56843d994f81f343f62e0bf2d73f7e4c9bc73e44bb1110b1d8845",
        ),
        "tts",
        "offline",
        {"language": "english"},
        {"language": "english"},
    ),
    "fireredtts3_base_q8_0": (
        "fireredtts3",
        "FireRedTTS3-Base-GGUF",
        ("fireredtts3-base-q8_0.gguf",),
        ("68acd5bce0d87a53bb5b88255c65e19df4cbc6017b4bab0824e96f1e2351c3a7",),
        "clon",
        "offline",
        None,
        None,
    ),
    "breeze_tts_2_q8_0": (
        "breeze_tts",
        "Breeze-TTS-2-GGUF",
        ("breeze-tts-2-q8_0.gguf",),
        ("0de52d61560f9f6b2dfeca79f9100f8fce0c2b17c52ec30622e23e150df1ad88",),
        "tts",
        "offline",
        None,
        None,
    ),
}


def _snapshot(package):
    return (
        package.family,
        package.target_directory,
        package.files,
        package.sha256,
        package.task,
        package.mode,
        package.load_options,
        package.session_options,
    )


def test_expanded_packages_are_single_file_offline_configs():
    assert set(EXPANDED_PACKAGES) <= set(SUPPORTED_MODEL_IDS) - set(LEGACY_PACKAGE_IDS)

    for package_id, expected in EXPANDED_PACKAGES.items():
        package = MODEL_PACKAGES[package_id]
        assert package.id == package_id
        assert package.family == expected["family"]
        assert package.target_directory == expected["target_directory"]
        assert package.files == (expected["file"],)
        assert package.config_path == (
            f"models/{expected['target_directory']}/{expected['file']}"
        )
        assert package.task == expected["task"]
        assert package.mode == "offline"
        assert package.load_options is None
        assert package.session_options is None
        assert len(package.files) == len(package.sha256) == 1
        assert package.sha256 == (expected["sha256"],)
        assert len(package.sha256[0]) == 64
        assert all(character in "0123456789abcdef" for character in package.sha256[0])


def test_existing_packages_and_audio_cpp_pins_remain_unchanged():
    assert AUDIO_CPP_VERSION == "0.8.1"
    assert AUDIO_CPP_MODEL_REVISION == "dc6fecccc2b0c6bdda0a8b2f38fa61394fee0b9c"
    assert tuple(MODEL_PACKAGES)[:15] == (*LEGACY_PACKAGE_IDS, *EXPANDED_PACKAGES)
    assert tuple(SUPPORTED_MODEL_IDS)[:15] == (*LEGACY_PACKAGE_IDS, *EXPANDED_PACKAGES)

    for package_id, expected in LEGACY_PACKAGE_SNAPSHOTS.items():
        assert _snapshot(MODEL_PACKAGES[package_id]) == expected
