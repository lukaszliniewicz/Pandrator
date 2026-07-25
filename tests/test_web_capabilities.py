import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandrator.web import capabilities
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from tests.web_test_support import prepare_web_test_data_root


class GpuCapabilityTests(unittest.TestCase):
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
        ), mock.patch.object(capabilities.os, "name", "posix"), mock.patch.object(
            capabilities.Path, "glob", return_value=iter([render_node])
        ):
            profiles = capabilities.probe_burn_video_encoders("ffmpeg", gpu)

        self.assertEqual(["libx264", "libx265", "h264_vaapi"], [item["id"] for item in profiles])

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
    STABLE_CAPABILITIES = {
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
