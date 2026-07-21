import hashlib
import json

def get_hash(d):
    return hashlib.sha256(json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()

# Simulate translation run
flattened_translate = {
    "translation_backend": "llm",
    "target_language": "fr",
    "instructions": ""
}
# _with_database_llm_settings adds these:
hydrated_translate = {
    **flattened_translate,
    "llm_provider_configs": {},
    "llm_default_model": "some_model",
    "request_timeout_seconds": 600
}

# The artifact is registered with `hydrated_translate` and requested_settings_hash = get_hash(flattened_translate)
existing_settings_hash = get_hash(hydrated_translate)
requested_settings_hash = get_hash(flattened_translate)

print(f"Original Translate Run:")
print(f"flattened (unhydrated): {get_hash(flattened_translate)}")
print(f"hydrated: {existing_settings_hash}")
print(f"requested_settings_hash: {requested_settings_hash}")

# Now generate_audio runs
# settings = stage_settings.get("translate")
settings_gen_audio = {
    "translation_backend": "llm",
    "target_language": "fr",
    "instructions": ""
}
expected_settings_hash = get_hash(settings_gen_audio)

print(f"\nGenerate Audio Run:")
print(f"expected_settings_hash: {expected_settings_hash}")

# raw_settings_match logic:
raw_settings_match = bool(
    existing_settings_hash == expected_settings_hash
    or requested_settings_hash == expected_settings_hash
)

print(f"raw_settings_match: {raw_settings_match}")

# if not raw_settings_match:
if not raw_settings_match:
    print("HYDRATING settings because raw_settings_match is False")
    hydrated_gen_audio = {
        **settings_gen_audio,
        "llm_provider_configs": {},
        "llm_default_model": "some_model",
        "request_timeout_seconds": 600
    }
    expected_hashes = {expected_settings_hash, get_hash(hydrated_gen_audio)}
else:
    expected_hashes = {expected_settings_hash}

# settings_match logic
settings_match = (
    existing_settings_hash in expected_hashes
    or requested_settings_hash == expected_settings_hash
)
print(f"settings_match: {settings_match}")
