"""Logging, command execution, networking, and Windows dependency operations."""

import ctypes
import ctypes.util
import hashlib
import logging
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from collections import deque
import zipfile
from datetime import datetime

import requests

try:
    import winreg
except ImportError:
    winreg = None

try:
    from packaging.specifiers import SpecifierSet as PackagingSpecifierSet
except ImportError:
    PackagingSpecifierSet = None

from .constants import (
    CALIBRE_BUNDLED_CALIBRE_SUBDIR,
    CALIBRE_BUNDLED_DIRNAME,
    CALIBRE_BUNDLED_EBOOK_CONVERT_RELATIVE_PATH,
    CALIBRE_WIN64_MSI_URL,
    CALIBRE_WIN64_MSI_SHA512,
    ESPEAK_NG_DATA_DIR_RELATIVE_PATH,
    ESPEAK_NG_DLL_RELATIVE_PATH,
    ESPEAK_NG_MSI_SHA256,
    ESPEAK_NG_MSI_URL,
    FFMPEG_BUNDLED_RELATIVE_PATH,
    FFMPEG_SUBTITLES_WINDOWS_ZIP_URL,
    FFMPEG_SUBTITLES_WINDOWS_ZIP_SHA256,
    PIXI_CACHE_DIRNAME,
    PIXI_TEMP_SUBDIRNAME,
    XTTS_FINETUNING_TORCH_INDEX_URL,
    XTTS_FINETUNING_TORCH_PACKAGE_SPECS,
)
DEFAULT_COMMAND_TIMEOUT_SECONDS = 2 * 60 * 60
COMMAND_OUTPUT_TAIL_LINES = 4000


class OperationsMixin:
    def initialize_logging(self):
        """Initialize robust file, console, and GUI logging."""
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        os.makedirs(pandrator_path, exist_ok=True)
        logs_path = os.path.join(pandrator_path, 'Logs')
        os.makedirs(logs_path, exist_ok=True)

        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_filename = os.path.join(logs_path, f'pandrator_installation_log_{current_time}.log')

        logger = logging.getLogger()
        logger.setLevel(logging.DEBUG)

        for handler in list(logger.handlers):
            if getattr(handler, '_pandrator_managed_handler', False):
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass

        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

        file_handler = logging.FileHandler(self.log_filename, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        file_handler._pandrator_managed_handler = True

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        console_handler._pandrator_managed_handler = True

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        if hasattr(self, "create_gui_log_handler"):
            gui_handler = self.create_gui_log_handler()
            gui_handler.setLevel(logging.INFO)
            gui_handler.setFormatter(formatter)
            gui_handler._pandrator_managed_handler = True
            logger.addHandler(gui_handler)

        if hasattr(self, "open_log_from_tab_button"):
            self.open_log_from_tab_button.setEnabled(True)

        logging.info(f"Logging initialized. Writing to: {self.log_filename}")

    def configure_tls_certificates(self, force=False):
        if self.tls_configured and not force:
            return

        self.tls_configured = True

        for env_name in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE'):
            value = os.environ.get(env_name)
            if value and not os.path.exists(value):
                logging.warning(f"Ignoring invalid {env_name} path: {value}")
                os.environ.pop(env_name, None)

        try:
            import certifi

            ca_bundle = certifi.where()
            if ca_bundle and os.path.exists(ca_bundle):
                os.environ['SSL_CERT_FILE'] = ca_bundle
                os.environ['REQUESTS_CA_BUNDLE'] = ca_bundle
                os.environ['CURL_CA_BUNDLE'] = ca_bundle
                self.ca_bundle_path = ca_bundle
                logging.info(f"Configured TLS certificate bundle: {ca_bundle}")
            else:
                logging.warning("certifi did not provide a usable certificate bundle path.")
        except Exception as e:
            logging.warning(f"Could not configure TLS certificate bundle via certifi: {str(e)}")

    def is_certificate_error(self, error):
        error_text = str(error).lower()
        return (
            'certificate verify failed' in error_text
            or 'sslcertverificationerror' in error_text
            or 'unable to get local issuer certificate' in error_text
        )

    def download_verified_file(
        self,
        url,
        destination,
        expected_digest,
        *,
        hash_name='sha256',
        timeout=(30, 600),
        chunk_size=1024 * 1024,
    ):
        """Stream a remote artifact to disk and fail closed on identity mismatch."""
        self.configure_tls_certificates()
        expected = str(expected_digest or '').strip().lower()
        try:
            digest = hashlib.new(hash_name)
        except ValueError as error:
            raise ValueError(f"Unsupported artifact hash algorithm: {hash_name}") from error
        if not expected or len(expected) != digest.digest_size * 2:
            raise ValueError(f"A valid {hash_name} digest is required for {url}.")

        os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
        try:
            with requests.get(
                url,
                stream=True,
                timeout=timeout,
                verify=self.ca_bundle_path if self.ca_bundle_path else True,
            ) as response:
                response.raise_for_status()
                with open(destination, 'wb') as handle:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        digest.update(chunk)
            actual = digest.hexdigest().lower()
            if not secrets.compare_digest(actual, expected):
                raise RuntimeError(
                    f"Downloaded artifact checksum mismatch for {url}. "
                    f"Expected {expected}, got {actual}."
                )
            return actual
        except Exception:
            try:
                os.remove(destination)
            except FileNotFoundError:
                pass
            raise

    def shutdown_logging(self):
        logger = logging.getLogger()
        for handler in list(logger.handlers):
            if getattr(handler, '_pandrator_managed_handler', False):
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass

    def get_network_subprocess_env(self):
        env = self.get_external_subprocess_env()
        if self.ca_bundle_path and os.path.exists(self.ca_bundle_path):
            env['SSL_CERT_FILE'] = self.ca_bundle_path
            env['REQUESTS_CA_BUNDLE'] = self.ca_bundle_path
            env['CURL_CA_BUNDLE'] = self.ca_bundle_path
            env['GIT_SSL_CAINFO'] = self.ca_bundle_path
        return env

    def get_external_subprocess_env(self, base_env=None):
        """Remove PyInstaller's private library path before invoking host tools.

        A Linux bundle must use its private libraries itself, but exporting that
        path to Git, Pixi, FFmpeg, or a service process can make those programs
        load ABI-incompatible bundled libraries.
        """
        env = dict(os.environ if base_env is None else base_env)
        if not sys.platform.startswith('linux'):
            return env

        original_library_path = env.pop('LD_LIBRARY_PATH_ORIG', None)
        if original_library_path is not None:
            if original_library_path:
                env['LD_LIBRARY_PATH'] = original_library_path
            else:
                env.pop('LD_LIBRARY_PATH', None)
            return env

        bundle_root = str(getattr(sys, '_MEIPASS', '') or '')
        current_library_path = env.get('LD_LIBRARY_PATH', '')
        if not bundle_root or not current_library_path:
            return env

        normalized_bundle_root = os.path.normcase(os.path.abspath(bundle_root))
        retained_paths = []
        for entry in current_library_path.split(os.pathsep):
            if not entry:
                continue
            normalized_entry = os.path.normcase(os.path.abspath(entry))
            if normalized_entry == normalized_bundle_root or normalized_entry.startswith(
                normalized_bundle_root + os.sep
            ):
                continue
            retained_paths.append(entry)

        if retained_paths:
            env['LD_LIBRARY_PATH'] = os.pathsep.join(retained_paths)
        else:
            env.pop('LD_LIBRARY_PATH', None)
        return env

    def is_admin(self):
        """Check if the current process has admin privileges."""
        if os.name != 'nt':
            return False

        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    def install_pytorch_for_xtts_finetuning(self, pandrator_path, env_name):
        logging.info(f"Installing PyTorch for XTTS Fine-tuning in {env_name}...")
        try:
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                ['python', '-m', 'pip', 'install']
                + list(XTTS_FINETUNING_TORCH_PACKAGE_SPECS)
                + ['--index-url', XTTS_FINETUNING_TORCH_INDEX_URL]
            )
            logging.info("PyTorch for XTTS Fine-tuning installed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install PyTorch for XTTS Fine-tuning in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def get_hidden_subprocess_kwargs(self):
        """Return subprocess kwargs that hide transient console windows on Windows."""
        if os.name != 'nt':
            return {}

        kwargs = {}

        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        if creationflags:
            kwargs['creationflags'] = creationflags

        startupinfo_factory = getattr(subprocess, 'STARTUPINFO', None)
        startf_use_showwindow = getattr(subprocess, 'STARTF_USESHOWWINDOW', 0)
        if startupinfo_factory and startf_use_showwindow:
            startupinfo = startupinfo_factory()
            startupinfo.dwFlags |= startf_use_showwindow
            startupinfo.wShowWindow = getattr(subprocess, 'SW_HIDE', 0)
            kwargs['startupinfo'] = startupinfo

        return kwargs

    @staticmethod
    def _stream_command_output(stream, label, output, state):
        try:
            for line in iter(stream.readline, ''):
                output.append(line)
                state['lines'] += 1
                logging.debug("[%s] %s", label, line.rstrip())
        finally:
            stream.close()

    @staticmethod
    def _command_output_text(output, state):
        text = ''.join(output)
        omitted = max(0, state['lines'] - len(output))
        if omitted:
            return f"[... {omitted} earlier output line(s) omitted ...]\n{text}"
        return text

    def run_command(
        self,
        command,
        use_shell=False,
        cwd=None,
        env=None,
        log_errors=True,
        timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ):
        command_display = command if isinstance(command, str) else ' '.join(command)
        try:
            subprocess_kwargs = self.get_hidden_subprocess_kwargs()
            env = self.get_external_subprocess_env(env)
            if use_shell:
                shell_command = command if isinstance(command, str) else (
                    subprocess.list2cmdline(command) if os.name == 'nt' else shlex.join(command)
                )
                process = subprocess.Popen(
                    shell_command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=True,
                    cwd=cwd,
                    env=env,
                    encoding='utf-8',
                    errors='replace',
                    bufsize=1,
                    **subprocess_kwargs,
                )
            else:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    encoding='utf-8',
                    errors='replace',
                    bufsize=1,
                    **subprocess_kwargs,
                )

            stdout_tail = deque(maxlen=COMMAND_OUTPUT_TAIL_LINES)
            stderr_tail = deque(maxlen=COMMAND_OUTPUT_TAIL_LINES)
            stdout_state = {'lines': 0}
            stderr_state = {'lines': 0}
            stdout_thread = threading.Thread(
                target=self._stream_command_output,
                args=(process.stdout, 'stdout', stdout_tail, stdout_state),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=self._stream_command_output,
                args=(process.stderr, 'stderr', stderr_tail, stderr_state),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                terminator = getattr(self, 'terminate_process_tree', None)
                if callable(terminator):
                    terminator(process)
                elif process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                stdout_thread.join(timeout=10)
                stderr_thread.join(timeout=10)
                stdout = self._command_output_text(stdout_tail, stdout_state)
                stderr = self._command_output_text(stderr_tail, stderr_state)
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)

            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)
            stdout = self._command_output_text(stdout_tail, stdout_state)
            stderr = self._command_output_text(stderr_tail, stderr_state)

            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)

            logging.info(f"Command executed: {command_display}")

            return stdout, stderr
        except subprocess.CalledProcessError as e:
            log = logging.error if log_errors else logging.debug
            log(f"Error executing command: {command_display}")
            log(f"Error message: {str(e)}")
            log(f"STDOUT: {e.stdout}")
            log(f"STDERR: {e.stderr}")
            raise
        except subprocess.TimeoutExpired as e:
            log = logging.error if log_errors else logging.debug
            log(f"Command timed out after {timeout} seconds: {command_display}")
            log(f"STDOUT: {e.output}")
            log(f"STDERR: {e.stderr}")
            raise

    def check_program_installed(self, program):
        return shutil.which(program) is not None

    def get_bundled_calibre_executable(self, pandrator_path):
        return os.path.join(pandrator_path, CALIBRE_BUNDLED_EBOOK_CONVERT_RELATIVE_PATH)

    def get_bundled_ffmpeg_executable(self, pandrator_path):
        return os.path.join(pandrator_path, FFMPEG_BUNDLED_RELATIVE_PATH)

    def get_local_temp_dir(self, pandrator_path):
        preferred_temp_dir = os.path.join(
            pandrator_path,
            PIXI_CACHE_DIRNAME,
            PIXI_TEMP_SUBDIRNAME,
        )
        try:
            os.makedirs(preferred_temp_dir, exist_ok=True)
            return preferred_temp_dir
        except OSError as e:
            logging.warning(
                "Could not create local temp directory at %s (%s). Falling back to system TEMP.",
                preferred_temp_dir,
                e,
            )
            return tempfile.gettempdir()

    def ffmpeg_supports_subtitles_filter(self, ffmpeg_path):
        if not ffmpeg_path or not os.path.exists(ffmpeg_path):
            return False

        try:
            process = subprocess.run(
                [
                    ffmpeg_path,
                    '-hide_banner',
                    '-v',
                    'error',
                    '-h',
                    'filter=subtitles',
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                **self.get_hidden_subprocess_kwargs(),
            )
        except Exception as e:
            logging.warning(f"Could not probe FFmpeg subtitles filter support at {ffmpeg_path}: {e}")
            return False

        output = (process.stdout or '').lower()
        return (
            "unknown filter 'subtitles'" not in output
            and 'no such filter' not in output
        )

    def _find_ffmpeg_exe_in_extracted_archive(self, extracted_root):
        bin_candidate = ''
        any_candidate = ''

        for root, _dirs, files in os.walk(extracted_root):
            for file_name in files:
                if file_name.lower() != 'ffmpeg.exe':
                    continue

                candidate = os.path.join(root, file_name)
                if not any_candidate:
                    any_candidate = candidate

                if os.path.basename(root).lower() == 'bin':
                    bin_candidate = candidate
                    break

            if bin_candidate:
                break

        return bin_candidate or any_candidate

    def ensure_bundled_ffmpeg_with_subtitles(self, pandrator_path):
        if os.name != 'nt':
            logging.info(
                "Skipping bundled Windows FFmpeg setup on non-Windows; "
                "Linux FFmpeg should be provided by the Pixi environment."
            )
            return True

        bundled_ffmpeg_path = self.get_bundled_ffmpeg_executable(pandrator_path)
        if self.ffmpeg_supports_subtitles_filter(bundled_ffmpeg_path):
            logging.info(
                "Bundled FFmpeg already supports subtitle burning at %s",
                bundled_ffmpeg_path,
            )
            return True

        logging.info("Installing bundled FFmpeg build with libass subtitle support...")
        self.configure_tls_certificates()

        temp_root = tempfile.mkdtemp(
            prefix='pandrator_ffmpeg_',
            dir=self.get_local_temp_dir(pandrator_path),
        )
        logging.info("Using temporary FFmpeg download directory: %s", temp_root)
        temp_archive_path = os.path.join(temp_root, 'ffmpeg_libass.zip')
        extracted_dir = os.path.join(temp_root, 'extract')
        os.makedirs(extracted_dir, exist_ok=True)

        try:
            self.download_verified_file(
                FFMPEG_SUBTITLES_WINDOWS_ZIP_URL,
                temp_archive_path,
                FFMPEG_SUBTITLES_WINDOWS_ZIP_SHA256,
                timeout=(30, 300),
            )

            with zipfile.ZipFile(temp_archive_path, 'r') as archive:
                archive.extractall(extracted_dir)

            extracted_ffmpeg = self._find_ffmpeg_exe_in_extracted_archive(extracted_dir)
            if not extracted_ffmpeg or not os.path.exists(extracted_ffmpeg):
                logging.warning(
                    "Downloaded FFmpeg archive did not contain ffmpeg.exe. "
                    "Subtitle burning may remain unavailable."
                )
                return False

            if not self.ffmpeg_supports_subtitles_filter(extracted_ffmpeg):
                logging.warning(
                    "Downloaded FFmpeg binary does not expose the subtitles filter. "
                    "Subtitle burning may remain unavailable."
                )
                return False

            os.makedirs(os.path.dirname(bundled_ffmpeg_path), exist_ok=True)
            temp_target_path = f"{bundled_ffmpeg_path}.tmp"
            shutil.copy2(extracted_ffmpeg, temp_target_path)
            os.replace(temp_target_path, bundled_ffmpeg_path)

            if not self.ffmpeg_supports_subtitles_filter(bundled_ffmpeg_path):
                logging.warning(
                    "Bundled FFmpeg copy completed but subtitles filter probe failed at %s.",
                    bundled_ffmpeg_path,
                )
                return False

            logging.info(
                "Bundled FFmpeg with subtitle filter support installed at %s",
                bundled_ffmpeg_path,
            )
            return True
        except Exception as e:
            logging.warning(
                "Could not install bundled FFmpeg with subtitle support: %s",
                e,
            )
            return False
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def check_calibre_available(self, pandrator_path=None):
        if self.check_program_installed('ebook-convert'):
            return True

        if self.check_program_installed('calibre'):
            return True

        if pandrator_path:
            bundled_calibre_exe = self.get_bundled_calibre_executable(pandrator_path)
            if os.path.exists(bundled_calibre_exe):
                return True

        return False

    def install_chocolatey(self):
        """Install Chocolatey using PowerShell's Invoke-WebRequest (no deprecated WebClient).

        Returns True on success, False otherwise.  Requires elevated process.
        """
        if os.name != 'nt':
            logging.info("Skipping Chocolatey installation on non-Windows.")
            return False

        logging.info("Installing Chocolatey...")
        try:
            ps_script = """
    $ProgressPreference = 'SilentlyContinue'
    $ErrorActionPreference = 'Stop'
    $installer = Join-Path $env:TEMP 'choco_install.ps1'
    Invoke-WebRequest -Uri 'https://community.chocolatey.org/install.ps1' -OutFile $installer
    powershell -ExecutionPolicy Bypass -File $installer
    Remove-Item $installer -Force -ErrorAction SilentlyContinue
    """
            process = subprocess.Popen(
                ["powershell", "-Command", ps_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **self.get_hidden_subprocess_kwargs(),
            )
            stdout, stderr = process.communicate()

            if process.returncode == 0:
                logging.info("Chocolatey installed successfully.")
                # Enable global confirmation
                subprocess.run(
                    ["powershell", "-Command", "choco feature enable -n=allowGlobalConfirmation"],
                    check=True,
                    capture_output=True,
                    text=True,
                    **self.get_hidden_subprocess_kwargs(),
                )
                logging.info("Global confirmation enabled for Chocolatey.")
                # Refresh env vars so choco.exe is on PATH for subsequent calls
                self.refresh_environment_variables()
                return True
            else:
                logging.error(f"Failed to install Chocolatey. Exit code: {process.returncode}")
                logging.error(f"STDOUT: {stdout}")
                logging.error(f"STDERR: {stderr}")
                return False
        except Exception as e:
            logging.error(f"An error occurred during Chocolatey installation: {str(e)}")
            logging.error(traceback.format_exc())
            return False

    def refresh_environment_variables(self):
        """Refresh environment variables from the Windows registry for the current process.

        Reads machine and user-level environment variables from the registry and injects
        them into os.environ AND the current process environment block via
        SetEnvironmentVariableW. This ensures child processes spawned by subprocess
        inherit updated values without rebooting or broadcasting WM_SETTINGCHANGE.
        """
        if os.name != 'nt' or winreg is None:
            logging.debug("Skipping Windows registry environment refresh on non-Windows.")
            return

        try:
            logging.info("Refreshing environment variables from registry...")

            def _expand_registry_value(value, value_type):
                if value_type != winreg.REG_EXPAND_SZ:
                    return value

                try:
                    return winreg.ExpandEnvironmentStrings(value)
                except OSError:
                    return os.path.expandvars(value)

            def _read_registry_env(
                key_path,
                root=winreg.HKEY_LOCAL_MACHINE,
                merge_path_with_existing=False,
            ):
                try:
                    with winreg.OpenKey(
                        root, key_path,
                        0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
                    ) as key:
                        i = 0
                        while True:
                            try:
                                name, value, value_type = winreg.EnumValue(key, i)
                                if value_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                                    i += 1
                                    continue

                                value = _expand_registry_value(value, value_type)

                                if merge_path_with_existing and name.lower() == 'path':
                                    existing_path = os.environ.get('Path') or os.environ.get('PATH')
                                    if existing_path and value:
                                        value = f"{existing_path}{os.pathsep}{value}"
                                    elif existing_path and not value:
                                        value = existing_path

                                os.environ[name] = value
                                ctypes.windll.kernel32.SetEnvironmentVariableW(
                                    name, value
                                )
                                i += 1
                            except OSError:
                                break
                except Exception as e:
                    logging.warning(
                        f"Could not read env vars from {key_path}: {e}"
                    )

            # Machine-level environment variables
            _read_registry_env(
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
            )
            # User-level environment variables
            _read_registry_env(
                r"Environment",
                root=winreg.HKEY_CURRENT_USER,
                merge_path_with_existing=True,
            )

            # If COMSPEC is unexpanded (e.g. "%SystemRoot%\\system32\\cmd.exe"),
            # shell=True subprocess calls raise FileNotFoundError.
            comspec = os.environ.get('COMSPEC') or os.environ.get('ComSpec')
            if comspec:
                comspec = os.path.expandvars(comspec)

            if not comspec or not os.path.exists(comspec):
                system_root = os.environ.get('SystemRoot', r'C:\Windows')
                fallback_comspec = os.path.join(system_root, 'System32', 'cmd.exe')
                if os.path.exists(fallback_comspec):
                    comspec = fallback_comspec

            if comspec:
                os.environ['COMSPEC'] = comspec
                ctypes.windll.kernel32.SetEnvironmentVariableW('COMSPEC', comspec)

            logging.info("Environment variables refreshed from registry.")
        except Exception as e:
            logging.error(f"Failed to refresh environment variables: {str(e)}")
            logging.error(traceback.format_exc())
            raise

    def install_dependencies(self, pandrator_path, allow_system_install=True):
        if os.name != 'nt':
            if self.check_calibre_available(pandrator_path):
                logging.info("Calibre ebook-convert detected for optional MOBI import.")
            else:
                logging.info(
                    "Calibre ebook-convert was not detected. "
                    "MOBI import will require Calibre to be installed separately."
                )
            return True

        return self.install_calibre(
            pandrator_path,
            allow_system_install=allow_system_install,
        )

    def show_calibre_installation_message(self):
        message = (
            "MOBI import requires Calibre's ebook-convert.\n"
            "Install Calibre separately if MOBI import is needed.\n"
            "Windows: https://calibre-ebook.com/download_windows\n"
            "Linux: https://calibre-ebook.com/download_linux"
        )

        if self.headless:
            logging.warning(message)
            print(message)
            return

        self.notify_warning("Calibre Installation Required", message)

    def install_with_chocolatey(self, package_name, args=""):
        if os.name != 'nt':
            logging.info(f"Skipping Chocolatey package installation for {package_name} on non-Windows.")
            return False

        logging.info(f"Attempting to install {package_name} with Chocolatey...")

        try:
            extra_args = shlex.split(args, posix=False) if args else []
        except ValueError as e:
            logging.warning(
                f"Unable to parse Chocolatey arguments '{args}': {e}. Falling back to basic split."
            )
            extra_args = args.split()

        # First, try using 'choco' command
        process = None
        try:
            process = subprocess.Popen(
                ['choco', 'install', package_name, '-y', *extra_args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **self.get_hidden_subprocess_kwargs(),
            )

            stdout, stderr = process.communicate(timeout=600)
            logging.info(stdout)

            if process.returncode == 0:
                logging.info(f"{package_name} installed successfully using 'choco' command.")
                return True

            logging.warning(
                f"Chocolatey 'choco' command exited with code {process.returncode}. STDERR: {stderr}"
            )
        except subprocess.TimeoutExpired:
            if process is not None:
                process.kill()
                process.communicate()
            logging.warning(f"Chocolatey install for {package_name} timed out using 'choco' command.")
        except Exception as e:
            logging.error(f"Error using 'choco' command: {str(e)}")

        # If 'choco' command fails, try using the Chocolatey executable directly
        process = None
        try:
            program_data = os.path.expandvars(os.environ.get('ProgramData', r'C:\ProgramData'))
            choco_exe = os.path.join(program_data, 'chocolatey', 'bin', 'choco.exe')
            if os.path.exists(choco_exe):
                process = subprocess.Popen(
                    [choco_exe, 'install', package_name, '-y', *extra_args],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    **self.get_hidden_subprocess_kwargs(),
                )

                stdout, stderr = process.communicate(timeout=600)
                logging.info(stdout)

                if process.returncode == 0:
                    logging.info(f"{package_name} installed successfully using Chocolatey executable.")
                    return True

                logging.warning(
                    f"Chocolatey executable exited with code {process.returncode}. STDERR: {stderr}"
                )
            else:
                logging.error(f"Chocolatey executable not found at: {choco_exe}")
        except subprocess.TimeoutExpired:
            if process is not None:
                process.kill()
                process.communicate()
            logging.warning(
                f"Chocolatey install for {package_name} timed out using Chocolatey executable."
            )
        except Exception as e:
            logging.error(f"Error using Chocolatey executable: {str(e)}")

        logging.error(f"Failed to install {package_name} using Chocolatey.")
        return False

    def install_calibre_portable(self, pandrator_path):
        if os.name != 'nt':
            logging.info("Skipping bundled Calibre MSI extraction on non-Windows.")
            return False

        bundled_calibre_exe = self.get_bundled_calibre_executable(pandrator_path)
        if os.path.exists(bundled_calibre_exe):
            logging.info(f"Bundled Calibre executable already available at {bundled_calibre_exe}")
            return True

        logging.info("Installing bundled Calibre fallback from direct MSI download...")
        self.reporter.status("Installing bundled Calibre fallback...")

        self.configure_tls_certificates()

        temp_root = tempfile.mkdtemp(
            prefix='pandrator_calibre_',
            dir=self.get_local_temp_dir(pandrator_path),
        )
        logging.info("Using temporary Calibre download directory: %s", temp_root)
        temp_msi_path = os.path.join(temp_root, 'calibre.msi')
        temp_extract_dir = os.path.join(temp_root, 'extract')
        extracted_calibre_dir = os.path.join(temp_extract_dir, 'PFiles64', 'Calibre2')

        try:
            self.download_verified_file(
                CALIBRE_WIN64_MSI_URL,
                temp_msi_path,
                CALIBRE_WIN64_MSI_SHA512,
                hash_name='sha512',
                timeout=(30, 300),
            )

            os.makedirs(temp_extract_dir, exist_ok=True)
            process = subprocess.Popen(
                ['msiexec', '/a', temp_msi_path, '/qn', f'TARGETDIR={temp_extract_dir}'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **self.get_hidden_subprocess_kwargs(),
            )
            stdout, stderr = process.communicate(timeout=900)

            if process.returncode != 0:
                logging.warning(
                    "MSI extraction for bundled Calibre failed with code %s. STDOUT: %s STDERR: %s",
                    process.returncode,
                    stdout,
                    stderr,
                )
                return False

            extracted_ebook_convert = os.path.join(extracted_calibre_dir, 'ebook-convert.exe')
            if not os.path.exists(extracted_ebook_convert):
                logging.warning(
                    "Bundled Calibre extraction completed but ebook-convert.exe was not found at %s",
                    extracted_ebook_convert,
                )
                return False

            bundled_calibre_root = os.path.join(pandrator_path, CALIBRE_BUNDLED_DIRNAME)
            bundled_calibre_dir = os.path.join(
                bundled_calibre_root,
                CALIBRE_BUNDLED_CALIBRE_SUBDIR,
            )

            os.makedirs(bundled_calibre_root, exist_ok=True)
            if os.path.exists(bundled_calibre_dir):
                shutil.rmtree(bundled_calibre_dir)

            shutil.copytree(extracted_calibre_dir, bundled_calibre_dir)

            if not os.path.exists(bundled_calibre_exe):
                logging.warning(
                    "Bundled Calibre copy completed but executable is missing at %s",
                    bundled_calibre_exe,
                )
                return False

            self.run_command([bundled_calibre_exe, '--version'], log_errors=False)
            logging.info(f"Bundled Calibre installed successfully at {bundled_calibre_dir}")
            return True
        except subprocess.TimeoutExpired:
            logging.warning("Timed out while extracting bundled Calibre MSI.")
            return False
        except Exception as e:
            logging.warning(f"Bundled Calibre installation failed: {e}")
            return False
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def install_calibre(self, pandrator_path, allow_system_install=True):
        """Install Calibre. Prefers system install, then bundles a local fallback."""
        if os.name != 'nt':
            logging.info("Skipping Calibre installation on non-Windows; Calibre is optional for MOBI import.")
            return self.check_calibre_available(pandrator_path)

        logging.info("Checking installation for Calibre")
        if self.check_calibre_available(pandrator_path):
            logging.info("Calibre is already installed.")
            return True

        logging.info("Installing Calibre...")

        if allow_system_install:
            winget_exe = os.path.join(
                os.environ.get('LOCALAPPDATA', r'C:\Program Files\WindowsApps'),
                'Microsoft.DesktopAppInstaller_8wekyb3d8bbwe',
                'winget.exe',
            )
            winget_alt = r'C:\Program Files (x86)\Microsoft\WinGet\winget.exe'

            winget_cmd = None
            if self.check_program_installed('winget'):
                winget_cmd = 'winget'
            elif os.path.exists(winget_exe):
                winget_cmd = winget_exe
            elif os.path.exists(winget_alt):
                winget_cmd = winget_alt

            if winget_cmd:
                try:
                    self.reporter.status("Installing Calibre via winget...")
                    process = subprocess.Popen(
                        [
                            winget_cmd,
                            'install',
                            '--id',
                            'calibre',
                            '--accept-package-agreements',
                            '--accept-source-agreements',
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        **self.get_hidden_subprocess_kwargs(),
                    )
                    stdout, stderr = process.communicate(timeout=600)
                    if process.returncode == 0:
                        logging.info("Calibre installed via winget.")
                        self.refresh_environment_variables()
                        if self.check_calibre_available(pandrator_path):
                            return True
                        logging.warning(
                            "Calibre installed via winget but not detected. Continuing with fallback options."
                        )
                    else:
                        logging.warning(
                            f"winget calibre install returned {process.returncode}: {stderr}"
                        )
                except subprocess.TimeoutExpired:
                    logging.warning(
                        "winget calibre install timed out, falling back to other methods."
                    )
                except Exception as e:
                    logging.warning(
                        f"winget calibre install failed: {e}, falling back to other methods."
                    )

            if self.install_with_chocolatey('calibre'):
                self.refresh_environment_variables()
                if self.check_calibre_available(pandrator_path):
                    logging.info("Calibre installed successfully via Chocolatey.")
                    return True
                logging.warning(
                    "Calibre installation via Chocolatey completed but executable was not detected."
                )
        else:
            logging.info("Skipping system-wide Calibre installation (requires admin).")

        if self.install_calibre_portable(pandrator_path):
            return True

        self.show_calibre_installation_message()
        return False

    def resolve_espeak_paths(self):
        if os.name != 'nt':
            return self.resolve_non_windows_espeak_paths()

        candidate_roots = [
            os.environ.get('ProgramFiles', r'C:\Program Files'),
            os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
        ]

        candidates = []
        seen = set()
        for root in candidate_roots:
            if not root:
                continue
            dll_path = os.path.join(root, ESPEAK_NG_DLL_RELATIVE_PATH)
            data_path = os.path.join(root, ESPEAK_NG_DATA_DIR_RELATIVE_PATH)
            key = (dll_path.lower(), data_path.lower())
            if key in seen:
                continue
            seen.add(key)
            candidates.append((dll_path, data_path))

        for dll_path, data_path in candidates:
            if os.path.exists(dll_path):
                resolved_data_path = data_path if os.path.exists(data_path) else ''
                return dll_path, resolved_data_path

        return '', ''

    def resolve_non_windows_espeak_paths(self):
        library_candidates = [
            os.environ.get('PHONEMIZER_ESPEAK_LIBRARY', ''),
            '/usr/lib64/libespeak-ng.so.1',
            '/usr/lib64/libespeak-ng.so',
            '/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1',
            '/usr/lib/x86_64-linux-gnu/libespeak-ng.so',
            '/usr/lib/aarch64-linux-gnu/libespeak-ng.so.1',
            '/usr/lib/aarch64-linux-gnu/libespeak-ng.so',
            '/usr/lib/libespeak-ng.so.1',
            '/usr/lib/libespeak-ng.so',
            '/usr/local/lib/libespeak-ng.so.1',
            '/usr/local/lib/libespeak-ng.so',
            '/opt/homebrew/lib/libespeak-ng.dylib',
            '/usr/local/lib/libespeak-ng.dylib',
        ]

        detected_library = ctypes.util.find_library('espeak-ng')
        if detected_library:
            library_candidates.append(detected_library)

        data_candidates = [
            os.environ.get('PHONEMIZER_ESPEAK_DATA', ''),
            os.environ.get('ESPEAK_DATA_PATH', ''),
            '/usr/share/espeak-ng-data',
            '/usr/lib/x86_64-linux-gnu/espeak-ng-data',
            '/usr/lib/aarch64-linux-gnu/espeak-ng-data',
            '/usr/lib64/espeak-ng-data',
            '/usr/lib/espeak-ng-data',
            '/usr/local/share/espeak-ng-data',
            '/opt/homebrew/share/espeak-ng-data',
        ]

        library_path = ''
        seen_libraries = set()
        for candidate in library_candidates:
            if not candidate:
                continue
            normalized = os.path.normcase(os.path.abspath(candidate))
            if normalized in seen_libraries:
                continue
            seen_libraries.add(normalized)
            if os.path.exists(candidate):
                library_path = candidate
                break

        data_path = ''
        seen_data_paths = set()
        for candidate in data_candidates:
            if not candidate:
                continue
            normalized = os.path.normcase(os.path.abspath(candidate))
            if normalized in seen_data_paths:
                continue
            seen_data_paths.add(normalized)
            if os.path.isdir(candidate):
                data_path = candidate
                break

        return library_path, data_path

    def install_espeak_ng_direct(self, pandrator_path=None):
        if os.name != 'nt':
            dll_path, data_path = self.resolve_espeak_paths()
            if dll_path or data_path:
                logging.info(
                    "Detected host eSpeak NG paths for Kokoro. Library: %s; data: %s",
                    dll_path or 'not detected',
                    data_path or 'not detected',
                )
            else:
                logging.info(
                    "Host eSpeak NG was not detected. "
                    "Kokoro will rely on the Pixi-installed espeakng-loader dependency."
                )
            logging.info(
                "Skipping eSpeak NG MSI installation on non-Windows."
            )
            return True

        dll_path, _ = self.resolve_espeak_paths()
        if dll_path:
            logging.info(f"eSpeak NG is already available at {dll_path}")
            return True

        logging.info("Installing eSpeak NG from direct MSI download...")
        self.configure_tls_certificates()

        temp_root = tempfile.mkdtemp(
            prefix='pandrator_espeak_',
            dir=self.get_local_temp_dir(pandrator_path) if pandrator_path else tempfile.gettempdir(),
        )
        logging.info("Using temporary eSpeak NG download directory: %s", temp_root)
        temp_msi_path = os.path.join(temp_root, 'espeak-ng.msi')

        try:
            self.download_verified_file(
                ESPEAK_NG_MSI_URL,
                temp_msi_path,
                ESPEAK_NG_MSI_SHA256,
                timeout=(30, 180),
            )

            process = subprocess.Popen(
                ['msiexec', '/i', temp_msi_path, '/qn', '/norestart', 'ALLUSERS=1'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **self.get_hidden_subprocess_kwargs(),
            )
            stdout, stderr = process.communicate(timeout=600)

            if process.returncode not in (0, 3010):
                logging.warning(
                    f"eSpeak NG MSI installation failed with exit code {process.returncode}. "
                    f"STDOUT: {stdout} STDERR: {stderr}"
                )
                return False

            self.refresh_environment_variables()
            dll_path, data_path = self.resolve_espeak_paths()
            if dll_path:
                logging.info(
                    f"eSpeak NG installed successfully. DLL: {dll_path}; "
                    f"Data path: {data_path or 'not detected'}"
                )
                return True

            logging.warning(
                "eSpeak NG installer finished but libespeak-ng.dll was not detected. "
                "Kokoro runtime may rely on espeakng-loader fallback."
            )
            return False
        except subprocess.TimeoutExpired:
            logging.warning("Timed out while installing eSpeak NG MSI.")
            return False
        except Exception as e:
            logging.warning(f"Could not install eSpeak NG automatically: {str(e)}")
            return False
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
