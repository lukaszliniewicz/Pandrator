import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from pandrator.web import capabilities
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from tests.web_test_support import prepare_web_test_data_root


class GpuCapabilityTests(unittest.TestCase):
    def test_native_memory_values_and_malformed_values_keep_existing_fallback(self):
        for raw, expected in (
            (0, 0), (8192, 8192), ("8192", 8192), (4.5, 4),
            (None, 0), (-1, 0), ("bad", 0), (object(), 0),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    expected,
                    capabilities._device("fixture", vram_mb=raw, source="fixture")["vram_mb"],
                )

    def test_linux_drm_probe_reads_amd_vram(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "card1" / "device"
            device.mkdir(parents=True)
            (device / "vendor").write_text("0x1002\n", encoding="ascii")
            (device / "device").write_text("0x67df\n", encoding="ascii")
            (device / "mem_info_vram_total").write_text(str(8 * 1024 * 1024 * 1024), encoding="ascii")

            with mock.patch.object(capabilities.sys, "platform", "linux"), mock.patch.object(
                capabilities, "_linux_pci_name", return_value="AMD Radeon RX 480"
            ):
                devices = capabilities._probe_linux_drm(root)

        self.assertEqual(1, len(devices))
        self.assertEqual("AMD Radeon RX 480", devices[0]["name"])
        self.assertEqual(8192, devices[0]["vram_mb"])
        self.assertEqual("0x67df", devices[0]["device_id"])

    def test_probe_gpu_merges_linux_drm_memory_with_vulkan_identity(self):
        drm = capabilities._device(
            "AMD GPU (0x67df)",
            vendor_id="0x1002",
            device_id="0x67df",
            vram_mb=8192,
            source="linux-drm",
        )
        vulkan = capabilities._device(
            "AMD Radeon RX 480 Graphics (RADV POLARIS10)",
            vendor_id="0x1002",
            device_id="0x67df",
            source="vulkan",
            apis=["vulkan"],
        )

        with mock.patch.object(capabilities.sys, "platform", "linux"), mock.patch.object(
            capabilities, "_probe_nvidia_smi", return_value=[]
        ), mock.patch.object(
            capabilities, "_probe_linux_drm", return_value=[drm]
        ), mock.patch.object(capabilities, "_probe_windows_video_controllers", return_value=[]), mock.patch.object(
            capabilities, "_probe_macos_displays", return_value=[]
        ), mock.patch.object(capabilities, "_probe_vulkan", return_value=[vulkan]):
            result = capabilities.probe_gpu()

        self.assertTrue(result["available"])
        self.assertEqual(1, len(result["devices"]))
        device = result["devices"][0]
        self.assertEqual("AMD Radeon RX 480 Graphics (RADV POLARIS10)", device["name"])
        self.assertEqual("AMD", device["vendor"])
        self.assertEqual(8192, device["vram_mb"])
        self.assertEqual(["linux-drm", "vulkan"], device["sources"])
        self.assertEqual(["vulkan"], device["apis"])

    def test_vulkan_probe_ignores_cpu_renderers(self):
        summary = """
GPU0:
    vendorID           = 0x1002
    deviceID           = 0x67df
    deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
    deviceName         = AMD Radeon RX 480 Graphics (RADV POLARIS10)
GPU1:
    vendorID           = 0x10005
    deviceID           = 0x0000
    deviceType         = PHYSICAL_DEVICE_TYPE_CPU
    deviceName         = llvmpipe (LLVM 22.1.8, 256 bits)
"""
        completed = mock.Mock(stdout=summary)
        with mock.patch.object(capabilities.shutil, "which", return_value="vulkaninfo"), mock.patch.object(
            capabilities.subprocess, "run", return_value=completed
        ):
            devices = capabilities._probe_vulkan()

        self.assertEqual(1, len(devices))
        self.assertEqual("AMD", devices[0]["vendor"])
        self.assertEqual("0x67df", devices[0]["device_id"])

    def test_burn_encoder_profiles_require_matching_gpu_and_render_node(self):
        gpu = {"devices": [{"vendor": "AMD"}]}
        supported = {"libx264", "libx265", "h264_vaapi", "h264_nvenc", "h264_amf"}
        render_node = Path("/dev/dri/renderD128")
        with mock.patch.object(capabilities, "ffmpeg_video_encoder_ids", return_value=supported), mock.patch.object(
            capabilities.sys, "platform", "linux"
        ), mock.patch.object(capabilities.Path, "glob", return_value=iter([render_node])):
            profiles = capabilities.probe_burn_video_encoders("ffmpeg", gpu)

        self.assertEqual(["libx264", "libx265", "h264_vaapi"], [item["id"] for item in profiles])

    def test_burn_encoder_discovery_never_initializes_hardware(self):
        gpu = {"devices": [{"vendor": "AMD"}]}
        completed = mock.Mock(stdout=" V..... libx264 Software H.264\n V..... h264_vaapi VA-API H.264\n V..... hevc_vaapi VA-API HEVC\n")
        with mock.patch.object(capabilities.sys, "platform", "linux"), mock.patch.object(
            capabilities.Path, "glob", return_value=iter([Path("/dev/dri/renderD128")])
        ), mock.patch.object(capabilities.subprocess, "run", return_value=completed) as run:
            profiles = capabilities.probe_burn_video_encoders("ffmpeg", gpu)

        self.assertEqual(["libx264", "h264_vaapi", "hevc_vaapi"], [item["id"] for item in profiles])
        run.assert_called_once_with(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=8, check=True,
        )

    def test_windows_probe_ignores_virtual_display_adapters(self):
        payload = (
            '[{"Name":"Parsec Virtual Display Adapter","PNPDeviceID":"ROOT\\\\PARSEC","AdapterRAM":0},'
            '{"Name":"AMD Radeon RX 480","PNPDeviceID":"PCI\\\\VEN_1002&DEV_67DF","AdapterRAM":4293918720}]'
        )
        completed = mock.Mock(stdout=payload)
        with mock.patch.object(capabilities.os, "name", "nt"), mock.patch.object(
            capabilities, "_probe_windows_display_devices", return_value=[]
        ), mock.patch.object(
            capabilities.shutil, "which", return_value="powershell"
        ), mock.patch.object(capabilities.subprocess, "run", return_value=completed):
            devices = capabilities._probe_windows_video_controllers()

        self.assertEqual(1, len(devices))
        self.assertEqual("AMD Radeon RX 480", devices[0]["name"])
        self.assertEqual("AMD", devices[0]["vendor"])


class CapabilityCacheTests(unittest.TestCase):
    STABLE_CAPABILITIES: ClassVar[dict[str, object]] = {
        "ffmpeg": {
            "available": True,
            "path": "ffmpeg",
            "burn_path": "ffmpeg",
            "burn_video_encoders": [],
        },
        "gpu": {"available": False, "devices": []},
        "pycroppdf": {"available": True, "local_only": True},
        "recording": {
            "browser_required": True,
            "normalization_available": True,
        },
        "stt": {"crispasr": False, "models": {}},
        "rvc": {"available": False},
        "services": {"rvc": False},
        "operations": {
            "record_voice": True,
            "transcribe_voice": False,
        },
    }

    def test_api_reuses_stable_probe_and_force_refreshes_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare_web_test_data_root(directory)
            bootstrap = BootstrapTokenStore()
            token = bootstrap.issue()
            app = create_app(
                data_root=directory,
                testing=True,
                bootstrap_tokens=bootstrap,
                capability_ttl_seconds=60,
            )
            database = app.extensions["pandrator"]["database"]
            try:
                client = app.test_client()
                client.post("/api/v1/auth/bootstrap", json={"token": token})
                with mock.patch.object(
                    capabilities,
                    "probe_stable_capabilities",
                    return_value=self.STABLE_CAPABILITIES,
                ) as probe:
                    first = client.get("/api/v1/capabilities").get_json()
                    second = client.get("/api/v1/capabilities").get_json()
                    remote = client.get(
                        "/api/v1/capabilities",
                        environ_overrides={"REMOTE_ADDR": "203.0.113.5"},
                    ).get_json()
                    refreshed = client.get(
                        "/api/v1/capabilities?refresh=true"
                    ).get_json()

                self.assertEqual(2, probe.call_count)
                self.assertFalse(first["_meta"]["cache_hit"])
                self.assertTrue(second["_meta"]["cache_hit"])
                self.assertFalse(refreshed["_meta"]["cache_hit"])
                self.assertEqual("local", first["mode"])
                self.assertTrue(first["operations"]["reveal_folder"])
                self.assertFalse(first["recording"]["secure_context_required"])
                self.assertEqual("remote", remote["mode"])
                self.assertFalse(remote["operations"]["reveal_folder"])
                self.assertFalse(remote["operations"]["pycroppdf_fallback"])
                self.assertTrue(remote["recording"]["secure_context_required"])
            finally:
                database.dispose()


if __name__ == "__main__":
    unittest.main()


class SttRoutingCapabilityTests(unittest.TestCase):
    def _stt(self, *, installed=True, version="0.8.40", ffmpeg=True):
        from pandrator.logic import audio_cpp_assets
        from pandrator.logic.dubbing import qwen_alignment, qwen_asr
        from pandrator.logic.dubbing.stt_backends import CrispASRRuntimeStatus

        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            runtime = CrispASRRuntimeStatus(installed, "fake-crispasr" if installed else "", version, ("cpu",), "fixture")
            with (
                mock.patch.object(capabilities, "probe_crispasr_runtime", return_value=runtime),
                mock.patch.object(capabilities, "probe_gpu", return_value={"devices": []}),
                mock.patch.object(capabilities, "probe_burn_video_encoders", return_value=[]),
                mock.patch.object(capabilities.shutil, "which", return_value="fake-ffmpeg" if ffmpeg else None),
                mock.patch.object(capabilities, "_crispasr_model_cached", return_value=False),
                mock.patch.object(capabilities, "crispasr_install_preferences", return_value={"configured": True, "engine": "parakeet", "quantization": "q4_k"}),
                mock.patch.object(qwen_asr, "capabilities", return_value={"cached_assets": {}, "word_timing": "native_or_canary"}),
                mock.patch.object(qwen_alignment, "capabilities", return_value={"id": "qwen3-forced-aligner", "available": False}),
                mock.patch.object(audio_cpp_assets, "availability", return_value={"models": {"htdemucs": {"available": True}}}),
            ):
                return capabilities.probe_stable_capabilities(paths)["stt"]

    def test_auto_is_policy_and_runtime_availability_does_not_claim_weights(self):
        stt = self._stt()
        self.assertEqual("auto", stt["default_engine"])
        self.assertEqual("parakeet", stt["installer_preferences"]["engine"])
        self.assertNotIn("auto", stt["models"])
        policy = stt["policies"]["auto"]
        self.assertEqual("policy", policy["kind"])
        self.assertTrue(policy["available"])
        self.assertEqual(["parakeet", "qwen3", "whisper"], policy["strategy"])
        self.assertNotIn("installed", policy)
        self.assertFalse(policy["recognition_failure_fallback"])
        self.assertTrue(all(not model["installed"] for model in stt["models"].values()))
        self.assertIn("htdemucs", stt["audio_cpp_tools"]["models"])
        self.assertFalse(self._stt(installed=False)["policies"]["auto"]["available"])

    def test_recognizer_and_aligner_support_are_distinct_operations(self):
        stt = self._stt()
        qwen = stt["models"]["qwen3"]
        recognition = qwen["language_support"]
        alignment = stt["forced_aligners"][0]["language_support"]
        self.assertEqual("asr", recognition["operation"])
        self.assertEqual("crispasr:qwen3", recognition["native_route"])
        self.assertEqual(30, len(recognition["languages"]))
        self.assertIn("ar", recognition["languages"])
        self.assertEqual("alignment", alignment["operation"])
        self.assertEqual(11, len(alignment["languages"]))
        self.assertNotIn("ar", alignment["languages"])
        self.assertEqual("unknown", stt["models"]["moss"]["language_support"]["coverage"])
        self.assertEqual(100, len(stt["models"]["whisper"]["language_support"]["languages"]))
        self.assertEqual(25, len(stt["models"]["parakeet"]["language_support"]["languages"]))
        self.assertFalse(qwen["requires_explicit_language_for_timestamps"])
        self.assertTrue(qwen["requires_resolved_language_for_timestamps"])

    def test_recognizer_language_metadata_and_coverage_share_one_lookup(self):
        native = capabilities.supported_stt_languages
        with mock.patch.object(capabilities, "supported_stt_languages", wraps=native) as lookup:
            stt = self._stt()
        self.assertEqual(
            sorted(capabilities.MODELS),
            sorted(call.args[0] for call in lookup.call_args_list),
        )
        for engine, record in stt["models"].items():
            with self.subTest(engine=engine):
                languages = native(engine)
                expected = list(languages) if languages is not None else None
                self.assertEqual(expected, record["supported_languages"])
                self.assertEqual(
                    "exact" if languages is not None else "unknown",
                    record["language_support"]["coverage"],
                )

    def test_detector_requirements_are_honest_and_do_not_raise_whisper_minimum(self):
        detector = self._stt()["language_detector"]
        self.assertTrue(detector["available"])
        self.assertEqual(15000, detector["max_sample_ms"])
        self.assertEqual(120, detector["timeout_seconds"])
        self.assertEqual("cpu", detector["compute_backend"])
        self.assertEqual(0.70, detector["confidence_threshold"])
        self.assertTrue(detector["confidence_threshold_provisional"])
        self.assertEqual("ggml-tiny.bin", detector["model"]["filename"])
        self.assertEqual(77691713, detector["model"]["size"])
        self.assertEqual(64, len(detector["model"]["sha256"]))
        old = self._stt(version="0.8.35")
        self.assertFalse(old["language_detector"]["available"])
        self.assertTrue(old["models"]["whisper"]["language_detection"])
        self.assertFalse(old["models"]["parakeet"]["language_detection"])
        self.assertFalse(old["models"]["qwen3"]["language_detection"])
        self.assertFalse(old["models"]["qwen3"]["available"])
        self.assertFalse(self._stt(ffmpeg=False)["language_detector"]["available"])
